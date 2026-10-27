"""Decision engine (ARCHITECTURE §4, ERROR-HANDLING §1/§3; v2: docs/INTERFACES.md "Script fix loop (L)").

v2 (flow-v2) / v2.1 — one decision per case (L2) and round, the ladder of `sbmig.triage.scripts`:

    run-wide blocked registry → pinned `block` case action → KB search: the stored script (no LLM)
    → blocked check (cfg.loop.blocked_policy, counted in LLM tries) → LLM unavailable
    → ladder exhausted (repeated_failure: stored 1 + LLM 3) → LLM budget (`loop.max_llm_calls`, 0 = no limit)
    → SCRIPT augment | new

plus masking (`plan_iteration`). A BLOCKED decision never calls the LLM; the fix loop records a BlockedItem for it and
keeps working on everything else. A stored, proven script of the case is reused even when the LLM is unavailable or
its budget is spent — only the steps that need the LLM are blocked.

Round 10 rules kept: the per-case limits count only SCORED attempts of the SAME cluster (`same_cluster`: same cluster
id — a case's id is stable across rounds —, or same file, also after the restructure move, with an overlapping
signature); `pending` never counts. A case pinned to `block` by a person (`CaseHit.pinned`) is obeyed first."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from sbmig.core.config import Config
from sbmig.core.models import BlockReason, DecisionKind, ErrorCluster, FixAttempt, KBMatch, ProjectProfile, TriageDecision

from .blocked import BlockedCandidate, classify_blocked
from .cluster import FILE_KINDS, RESOLUTION_KINDS, cluster_label
from .scripts import LLM_MODES, ScriptStep, plan_script_step, script_attempts

log = logging.getLogger("sbmig.triage")

SUCCESS_OUTCOMES = frozenset({"improved", "build_ok"})
# scored failures: what the per-cluster limits count (round 10: no "pending" — an attempt applied this round and not
# built yet proves nothing; "inconclusive" = the scoring build could not show the cluster's errors, counts as tried)
FAILED_OUTCOMES = frozenset({"no_change", "regressed", "reverted", "apply_failed", "inconclusive"})
# some targeted errors gone, the rest remain: progress, not a failure (v2: the next script attempt of the case is an
# augment of it — the per-case limit is script.max_augments_per_case)
PROGRESS_OUTCOMES = frozenset({"partial"})
# attempts that produced nothing usable (no script / it did not run / nothing applied / llm-error)
GARBAGE_OUTCOMES = frozenset({"apply_failed"})
# a pinned `block` action whose block_reason is free text (not a BlockReason value): the generic library-side reason
PINNED_BLOCK_FALLBACK = BlockReason.EXTERNAL_API_INCOMPATIBLE


def blocked_rationale(reason: BlockReason, detail: str) -> str:
    return f"blocked: {reason.value} — {detail}"


def _posix(path: Optional[str]) -> str:
    p = (path or "").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def same_cluster(cluster: ErrorCluster, attempt: FixAttempt,
                 canon: Optional[Callable[[Optional[str]], str]] = None) -> bool:
    """Was `attempt` made on this cluster? Same cluster id, or — the cluster id changes when the restructure moves
    the file — the same file (`canon` maps a moved path to its current one, `BlockedRegistry.canonical_file`) with
    an overlapping signature. An attempt on ANOTHER file that shares an L1 signature is not the same cluster."""
    if attempt.cluster_id == cluster.id:
        return True
    norm = canon or _posix
    files = {norm(f) for f in [cluster.file, *(e.file for e in cluster.errors)] if f}
    if not files:
        return False
    a_files = {norm(e.file) for e in attempt.cluster_errors if e.file}
    if not files & a_files:
        return False
    return bool(set(cluster.signatures) & set(attempt.cluster_signatures))


def same_cluster_attempts(cluster: ErrorCluster, attempts: Iterable[FixAttempt],
                          canon: Optional[Callable[[Optional[str]], str]] = None) -> list[FixAttempt]:
    return [a for a in attempts if same_cluster(cluster, a, canon)]


@dataclass
class IterationItem:
    cluster: ErrorCluster
    skip: Optional[TriageDecision] = None          # SKIP decision when masked
    masked_by: list[str] = field(default_factory=list)


def related_attempts(cluster: ErrorCluster, attempts: Iterable[FixAttempt]) -> list[FixAttempt]:
    """Same cluster id or ANY shared signature (other files included) — hint material only since round 10; the
    limits use `same_cluster_attempts`."""
    sigs = set(cluster.signatures)
    return [a for a in attempts if a.cluster_id == cluster.id or (sigs and sigs & set(a.cluster_signatures))]


def _modules(c: ErrorCluster) -> set[str]:
    """The modules a cluster's errors are in (a v2 case may span modules; its `module` is then None)."""
    mods = {e.module for e in c.errors if e.module}
    return mods or ({c.module} if c.module else set())


def plan_iteration(clusters: list[ErrorCluster]) -> list[IterationItem]:
    """Masking: resolution-type clusters (parent/pom/dependency/plugin resolution) mask compile and test-compile
    clusters of the same module (or of every module when the resolution error has no module). A case spanning
    modules is masked when one of its modules has such a problem (the pom is fixed first)."""
    resolution = [c for c in clusters if c.kind in RESOLUTION_KINDS]
    out: list[IterationItem] = []
    for c in clusters:
        if c.kind in FILE_KINDS and resolution:
            mods = _modules(c)
            maskers = [r for r in resolution if r.module is None or r.module in mods]
            if maskers:
                names = ", ".join(cluster_label(r) for r in maskers[:3])
                out.append(IterationItem(c, TriageDecision(
                    cluster_id=c.id, kind=DecisionKind.SKIP,
                    rationale=f"masked this iteration by pom/resolution problem(s) in the same module: {names} — "
                              "compile errors usually disappear once the pom is fixed"),
                    [r.id for r in maskers]))
                continue
        out.append(IterationItem(c))
    return out


class DecisionEngine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        # run-wide state attached by the build-fix loop (see Triager.attach_run)
        self.blocked_registry = None            # sbmig.loop.blocked_registry.BlockedRegistry
        self.ws = None                          # Workspace: project-class checks for the blocked rules
        self.llm_unavailable: Optional[str] = None
        self.cases: Any = None                  # v2.1: the KB's case store (`kb.cases` / CaseScripts), None = no KB
        # cluster id → why it was blocked (read by the loop to build the BlockedItem)
        self.blocked_info: dict[str, BlockedCandidate] = {}
        # cluster id → the ladder step of its last decision (read by the loop: what to run / ask for, the KB line)
        self.script_steps: dict[str, ScriptStep] = {}

    def _canon(self) -> Optional[Callable[[Optional[str]], str]]:
        fn = getattr(self.blocked_registry, "canonical_file", None)
        return fn if callable(fn) else None

    # -- pinned case actions (round 10) ----------------------------------
    @staticmethod
    def _pinned_hits(cluster: ErrorCluster, kb_match: KBMatch) -> list[Any]:
        """Case hits whose action a person pinned (`CaseHit.pinned`) and whose errors are (still) in this cluster."""
        def ek(e) -> tuple:
            return (e.signature, _posix(e.file), e.line, e.message)
        mine = {ek(e) for e in cluster.errors}
        out = []
        for h in list(getattr(kb_match, "case_hits", None) or []):
            if not getattr(h, "pinned", False) or getattr(h, "action", None) is None:
                continue
            errs = [e for e in (getattr(h, "errors", None) or []) if ek(e) in mine]
            if errs or not getattr(h, "errors", None):
                out.append((h, errs or list(cluster.errors)))
        return out

    def _pinned_block(self, cluster: ErrorCluster, kb_match: KBMatch) -> Optional[TriageDecision]:
        """A case a person pinned to `block` → BLOCKED with their reason / text (round 10). A pinned `script` version
        is the store's `best()` choice (`kb.cases`), not a decision here."""
        for h, errs in self._pinned_hits(cluster, kb_match):
            a = h.action
            if a.kind != "block":
                continue
            raw = (a.block_reason or "").strip()
            try:
                reason = BlockReason(raw)
                text = ""
            except ValueError:
                reason, text = PINNED_BLOCK_FALLBACK, raw
            why = f"pinned by case {h.case_id}" + (f" ({h.name})" if h.name else "") + (f": {text}" if text else "")
            sugg = " ".join(x for x in (text, (a.suggestion or "").strip()) if x)
            cand = BlockedCandidate(reason=reason, errors=list(errs), evidence=[why], case_id=h.case_id,
                                    suggestion=sugg or f"a person pinned case {h.case_id} to `block`: handle it by hand "
                                                       "(see `sbmig kb cases show " + h.case_id + "`)")
            return self._blocked(cluster, cand, " (pinned case action: no script, no LLM)")
        return None

    # -- main ----------------------------------------------------------
    def _blocked(self, cluster: ErrorCluster, cand: BlockedCandidate, extra: str = "") -> TriageDecision:
        self.blocked_info[cluster.id] = cand
        return TriageDecision(cluster_id=cluster.id, kind=DecisionKind.BLOCKED,
                              rationale=blocked_rationale(cand.reason, cand.detail()) + extra)

    def decide(self, cluster: ErrorCluster, kb_match: Optional[KBMatch], ctx, profile: ProjectProfile) -> TriageDecision:
        """v2: SCRIPT (with the ladder step in `script_steps[cluster.id]`), BLOCKED or ESCALATE (no case id)."""
        kb_match = kb_match or KBMatch()
        prev = same_cluster_attempts(cluster, ctx.previous_attempts, self._canon())
        label = cluster_label(cluster)
        self.blocked_info.pop(cluster.id, None)
        self.script_steps.pop(cluster.id, None)

        # 0. already blocked earlier in this run (any phase) → no script, no LLM
        reg = self.blocked_registry
        if reg is not None:
            item = reg.covering(cluster)
            if item is not None:
                cand = BlockedCandidate(reason=item.reason, required_type=item.required_type,
                                        found_type=item.found_type, owner_class=item.owner_class,
                                        evidence=[f"already blocked earlier in this run ({item.id})"],
                                        errors=list(cluster.errors))
                self.blocked_info[cluster.id] = cand
                return TriageDecision(cluster_id=cluster.id, kind=DecisionKind.BLOCKED,
                                      rationale=f"blocked: {item.reason.value} — already blocked earlier in this run "
                                                f"({item.id}, {item.phase.value}); skipped")

        # 0b. a person pinned the case to `block` (`sbmig kb cases pin --action block…`)
        d = self._pinned_block(cluster, kb_match)
        if d is not None:
            return d

        # 1. the script ladder: a proven stored version first — no LLM call, so neither the blocked policy nor the
        #    LLM budget / availability stops it
        step = plan_script_step(cluster, prev, self.cases, self.cfg)
        self.script_steps[cluster.id] = step          # also for a BLOCKED decision: its KB search line (the log)
        if not step.case_id:
            return TriageDecision(cluster_id=cluster.id, kind=DecisionKind.ESCALATE,
                                  rationale=f"escalated: {label} has no case id (no errors)")
        if step.mode == "reuse":
            return TriageDecision(cluster_id=cluster.id, kind=DecisionKind.SCRIPT, rationale=step.why)

        # 2. blocked check: cannot be fixed inside the project → blocked_policy[reason] LLM script attempts, then BLOCKED
        llm_failed = [a for a in script_attempts(prev) if a.script_mode in LLM_MODES and a.outcome in FAILED_OUTCOMES]
        policy_note = ""
        try:
            cand = classify_blocked(cluster, profile, self.cfg, ws=self.ws)
        except Exception:  # a classifier bug must never stop the loop
            cand = None
        if cand is not None:
            allowed = int(self.cfg.loop.blocked_policy.get(cand.reason.value, 0))
            if len(llm_failed) >= allowed:
                extra = (f" (after {len(llm_failed)} LLM script attempt(s))" if llm_failed else
                         " (policy: no LLM script for this reason)")
                return self._blocked(cluster, cand, extra)
            policy_note = (f"; looks like {cand.reason.value} ({cand.detail()}) — LLM script attempt "
                           f"{len(llm_failed) + 1}/{allowed} before blocking")

        # 3. the LLM steps (augment / new) → BLOCKED when the LLM cannot be asked
        if self.llm_unavailable:
            return self._blocked(cluster, BlockedCandidate(
                reason=BlockReason.LLM_UNAVAILABLE, errors=list(cluster.errors),
                evidence=[f"LLM unavailable ({self.llm_unavailable}); no stored script left to reuse for {label}"]))
        if step.mode is None:
            return self._blocked(cluster, BlockedCandidate(
                reason=BlockReason.REPEATED_FAILURE, errors=list(cluster.errors),
                evidence=[step.why + "; needs a human"]))
        # v2.1: 0 = no limit, > 0 = a cap, -1 = no LLM call at all (the fleet's total is used up)
        if ctx.llm_calls_budget < 0 or (ctx.llm_calls_budget > 0 and ctx.llm_calls_used >= ctx.llm_calls_budget):
            spent = ("fleet" if ctx.llm_calls_budget < 0 else f"{ctx.llm_calls_used}/{ctx.llm_calls_budget} calls")
            return self._blocked(cluster, BlockedCandidate(
                reason=BlockReason.BUDGET_EXHAUSTED, errors=list(cluster.errors),
                evidence=[f"LLM budget exhausted ({spent}) and no stored script left to reuse for {label}"]))
        why = step.why
        if ctx.stall_level:
            why += f"; stall level {ctx.stall_level}"
        return TriageDecision(cluster_id=cluster.id, kind=DecisionKind.SCRIPT, rationale=why + policy_note)
