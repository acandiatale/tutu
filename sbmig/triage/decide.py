"""Decision engine (ARCHITECTURE §4, ERROR-HANDLING §1/§3):

    run-wide blocked registry → pinned case action (block | handler | recipe) → HANDLER > KB_RECIPE > KB_PATCH
    > blocked check (cfg.loop.blocked_policy) > LLM_FIX → BLOCKED(repeated_failure | budget_exhausted | llm_unavailable)

plus masking. A BLOCKED decision never calls the LLM; the build-fix loop records a BlockedItem for it and keeps
working on everything else.

Round 10 (2026-10-26): the per-cluster limits (`max_llm_attempts_per_cluster`, `blocked_policy`, the repeated-failure
limit) count only SCORED failures of the SAME cluster (`same_cluster`: same cluster id, or same file — after the
restructure move too — with an overlapping signature). L1 signatures carry no file, so before this the same
`cannot find symbol class X` in five files let the attempts on the first files count against the later ones (only
three got an LLM call, the rest were blocked `repeated_failure`). `partial` is progress (not a failure); `pending`
(applied, not yet built) is not counted. A case pinned by a person (`CaseHit.pinned`) is obeyed first."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from sbmig.core.config import Config
from sbmig.core.models import (BlockReason, DecisionKind, ErrorCluster, FixAttempt, KBMatch, Phase, ProjectProfile,
                               RecipeRef, TriageDecision)

from .blocked import BlockedCandidate, classify_blocked
from .cluster import FILE_KINDS, RESOLUTION_KINDS, cluster_label
from .rules import RecipeRules

log = logging.getLogger("sbmig.triage")

SUCCESS_OUTCOMES = frozenset({"improved", "build_ok"})
# scored failures: what the per-cluster limits count (round 10: no "pending" — an attempt applied this round and not
# built yet proves nothing; "inconclusive" = the scoring build could not show the cluster's errors, counts as tried)
FAILED_OUTCOMES = frozenset({"no_change", "regressed", "reverted", "apply_failed", "inconclusive"})
# some targeted errors gone, the rest remain: progress, not a failure — but a deterministic option (handler, recipe,
# KB patch) that only got this far has done what it can and is not chosen again
PROGRESS_OUTCOMES = frozenset({"partial"})
# LLM attempts that produced nothing usable (no patches parsed / nothing applied / llm-error)
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


def plan_iteration(clusters: list[ErrorCluster]) -> list[IterationItem]:
    """Masking: resolution-type clusters (parent/pom/dependency/plugin resolution) mask compile and test-compile
    clusters of the same module (or of every module when the resolution error has no module)."""
    resolution = [c for c in clusters if c.kind in RESOLUTION_KINDS]
    out: list[IterationItem] = []
    for c in clusters:
        if c.kind in FILE_KINDS and resolution:
            maskers = [r for r in resolution if r.module is None or r.module == c.module]
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


def recipe_refs_for(recipe_ids: list[str], kb_match: Optional[KBMatch], rules: RecipeRules) -> list[RecipeRef]:
    out: list[RecipeRef] = []
    for rid in recipe_ids:
        ref: Optional[RecipeRef] = None
        if kb_match is not None:
            for ids, files in ((kb_match.recipe_ids, kb_match.recipe_files),
                               (kb_match.candidate_recipe_ids, kb_match.candidate_recipe_files)):
                if rid in ids:
                    i = ids.index(rid)
                    ref = RecipeRef(id=rid, source="kb", file=files[i] if len(files) == len(ids) else None)
                    break
        if ref is None:
            ref = rules.ref_for(rid) or RecipeRef(id=rid, source="builtin")
        if not any(r.id == ref.id for r in out):
            out.append(ref)
    return out


class DecisionEngine:
    def __init__(self, cfg: Config, registry, rules: RecipeRules):
        self.cfg = cfg
        self.registry = registry
        self.rules = rules
        self.recipes_run_by_phase: dict[Phase, set[str]] = {}
        self.recipes_run_any: set[str] = set()
        # run-wide state attached by the build-fix loop (see Triager.attach_run)
        self.blocked_registry = None            # sbmig.loop.blocked_registry.BlockedRegistry
        self.ws = None                          # Workspace: project-class checks for the blocked rules
        self.llm_unavailable: Optional[str] = None
        # cluster id → why it was blocked (read by the loop to build the BlockedItem)
        self.blocked_info: dict[str, BlockedCandidate] = {}

    # -- bookkeeping from the loop/orchestrator ------------------------
    def mark_recipes_run(self, recipe_ids: Iterable[str], phase: Optional[Phase] = None) -> None:
        ids = set(recipe_ids)
        self.recipes_run_any |= ids
        if phase is not None:
            self.recipes_run_by_phase.setdefault(phase, set()).update(ids)

    def _excluded_recipes(self, phase: Phase, failed: list[FixAttempt]) -> set[str]:
        out = set(self.recipes_run_by_phase.get(phase, set()))
        out |= self.recipes_run_any & self.rules.once_per_run_ids()
        for a in failed:
            if a.decision.kind == DecisionKind.KB_RECIPE:
                out |= set(a.decision.recipe_ids)
        return out

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

    def _pinned_decision(self, cluster: ErrorCluster, kb_match: KBMatch, tried_handlers: set, excluded: set[str],
                         notes: list[str]) -> Optional[TriageDecision]:
        """The decision a pinned case action dictates, or None (nothing pinned / already tried → the normal order).
        `block` wins over `handler` over `recipe`; an unknown handler id is a note, never an error."""
        hits = self._pinned_hits(cluster, kb_match)
        if not hits:
            return None
        for h, errs in hits:
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
            return self._blocked(cluster, cand, " (pinned case action: no handler, no LLM)")
        for h, _errs in hits:
            a = h.action
            if a.kind != "handler" or not a.handler_id:
                continue
            hd = self.registry.get(a.handler_id) if self.registry is not None else None
            if hd is None:
                note = f"pinned handler '{a.handler_id}' of case {h.case_id} is not registered — ignored"
                log.warning("%s", note)
                notes.append(note)
                continue
            if hd.id in tried_handlers:
                notes.append(f"pinned handler '{hd.id}' of case {h.case_id} already tried without effect")
                continue
            return TriageDecision(cluster_id=cluster.id, kind=DecisionKind.HANDLER, handler_id=hd.id,
                                  rationale=f"pinned case action of case {h.case_id}: handler '{hd.id}' — "
                                            f"{hd.description}")
        for h, _errs in hits:
            a = h.action
            if a.kind != "recipe":
                continue
            ids = [r for r in dict.fromkeys(a.recipe_ids or []) if r and r not in excluded]
            if not ids:
                if a.recipe_ids:
                    notes.append(f"pinned recipe(s) of case {h.case_id} already ran: {', '.join(a.recipe_ids)}")
                continue
            return TriageDecision(cluster_id=cluster.id, kind=DecisionKind.KB_RECIPE, recipe_ids=ids,
                                  rationale=f"pinned case action of case {h.case_id}: recipe(s) {', '.join(ids)}")
        return None

    # -- main ----------------------------------------------------------
    def _blocked(self, cluster: ErrorCluster, cand: BlockedCandidate, extra: str = "") -> TriageDecision:
        self.blocked_info[cluster.id] = cand
        return TriageDecision(cluster_id=cluster.id, kind=DecisionKind.BLOCKED,
                              rationale=blocked_rationale(cand.reason, cand.detail()) + extra)

    def decide(self, cluster: ErrorCluster, kb_match: Optional[KBMatch], ctx, profile: ProjectProfile) -> TriageDecision:
        kb_match = kb_match or KBMatch()
        prev = same_cluster_attempts(cluster, ctx.previous_attempts, self._canon())
        failed = [a for a in prev if a.outcome in FAILED_OUTCOMES]
        # deterministic options that ran to a partial result are not chosen again either
        tried_any = [a for a in prev if a.outcome in FAILED_OUTCOMES or a.outcome in PROGRESS_OUTCOMES]
        label = cluster_label(cluster)
        self.blocked_info.pop(cluster.id, None)

        # 0. already blocked earlier in this run (any phase) → no handler re-runs, no LLM
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

        # 0b. a case action a person pinned (`sbmig kb cases pin --action …`): block | handler | recipe
        tried = {a.decision.handler_id for a in tried_any if a.decision.kind == DecisionKind.HANDLER}
        pin_notes: list[str] = []
        d = self._pinned_decision(cluster, kb_match, tried, self._excluded_recipes(ctx.phase, tried_any), pin_notes)
        if d is not None:
            return d
        pin_note = ("; " + "; ".join(pin_notes)) if pin_notes else ""

        # 1. deterministic handler
        candidates = list(self.registry.matching(cluster, profile, self.cfg)) if self.registry is not None else []
        for hid in kb_match.handler_ids:
            h = self.registry.get(hid) if self.registry is not None else None
            if h is not None and all(c.id != h.id for c in candidates):
                candidates.append(h)
        for h in candidates:
            if h.id in tried:
                continue
            why = f"deterministic handler '{h.id}' matches {label}: {h.description}"
            if tried:
                why += f" (already tried without effect: {', '.join(sorted(t for t in tried if t))})"
            return TriageDecision(cluster_id=cluster.id, kind=DecisionKind.HANDLER, handler_id=h.id,
                                  rationale=why + pin_note)

        # 2. KB active recipes ∪ reactive recipe rules, then KB candidate recipes
        excluded = self._excluded_recipes(ctx.phase, tried_any)
        active: list[str] = []
        if self.cfg.kb.use_active_recipes:
            active += kb_match.recipe_ids
        rule_hits = self.rules.matching_rules(cluster, ctx.phase)
        for rule in rule_hits:
            active += rule.recipes
        fresh = [r for i, r in enumerate(active) if r not in excluded and r not in active[:i]]
        source = "active KB recipe(s)/recipe rule(s)"
        if not fresh and self.cfg.kb.use_active_recipes:
            fresh = [r for r in dict.fromkeys(kb_match.candidate_recipe_ids) if r not in excluded]
            source = "KB candidate recipe(s)"
        if fresh:
            rules_txt = f" [rules: {', '.join(r.id for r in rule_hits)}]" if rule_hits else ""
            return TriageDecision(cluster_id=cluster.id, kind=DecisionKind.KB_RECIPE, recipe_ids=fresh,
                                  rationale=f"{source} for {label}: {', '.join(fresh)}{rules_txt}{pin_note}")

        # 3. KB patch replay
        if self.cfg.kb.use_patch_replay and kb_match.replayable:
            tried_cases = {cid for a in tried_any if a.decision.kind == DecisionKind.KB_PATCH
                           for cid in a.decision.kb_case_ids}
            cases = [c for c in kb_match.replayable if c.id not in tried_cases]
            if cases:
                c = cases[0]
                return TriageDecision(cluster_id=cluster.id, kind=DecisionKind.KB_PATCH, kb_case_ids=[c.id],
                                      rationale=f"replay KB case {c.id} (same signature, search text still present)"
                                                + (f": {c.summary}" if c.summary else ""))

        llm_failed = [a for a in failed if a.decision.kind == DecisionKind.LLM_FIX]
        garbage = [a for a in llm_failed if a.outcome in GARBAGE_OUTCOMES]

        # 4. blocked check: cannot be fixed inside the project → blocked_policy[reason] LLM attempts, then BLOCKED
        policy_note = ""
        try:
            cand = classify_blocked(cluster, profile, self.cfg, ws=self.ws)
        except Exception:  # a classifier bug must never stop the loop
            cand = None
        if cand is not None:
            allowed = int(self.cfg.loop.blocked_policy.get(cand.reason.value, 0))
            if len(llm_failed) >= allowed:
                extra = (f" (after {len(llm_failed)} LLM attempt(s))" if llm_failed else
                         " (policy: no LLM attempt for this reason)")
                return self._blocked(cluster, cand, extra)
            policy_note = (f"; looks like {cand.reason.value} ({cand.detail()}) — LLM attempt "
                           f"{len(llm_failed) + 1}/{allowed} before blocking")

        # 5. LLM (bounded) → BLOCKED
        if self.llm_unavailable:
            return self._blocked(cluster, BlockedCandidate(
                reason=BlockReason.LLM_UNAVAILABLE, errors=list(cluster.errors),
                evidence=[f"LLM unavailable ({self.llm_unavailable}); no deterministic fix for {label}"]))
        max_garbage = max(1, int(self.cfg.loop.max_llm_attempts_per_cluster))
        limit = max(max_garbage, self.cfg.loop.stall_limit + 1)
        if ctx.phase == Phase.VERIFY:        # final verify loop: released repeated_failure clusters get more tries
            extra = max(0, int(getattr(self.cfg.loop, "verify_extra_llm_attempts", 0)))
            max_garbage, limit = max_garbage + extra, limit + extra
        if len(garbage) >= max_garbage or len(llm_failed) >= limit:
            why = (f"{len(garbage)} LLM attempt(s) on {label} produced no usable patch (limit {max_garbage})"
                   if len(garbage) >= max_garbage else
                   f"{len(llm_failed)} LLM attempt(s) on {label} did not resolve it (limit {limit})")
            return self._blocked(cluster, BlockedCandidate(
                reason=BlockReason.REPEATED_FAILURE, errors=list(cluster.errors),
                evidence=[why + "; needs a human"]))
        if ctx.llm_calls_used >= ctx.llm_calls_budget:
            return self._blocked(cluster, BlockedCandidate(
                reason=BlockReason.BUDGET_EXHAUSTED, errors=list(cluster.errors),
                evidence=[f"LLM budget exhausted ({ctx.llm_calls_used}/{ctx.llm_calls_budget} calls) and no "
                          f"deterministic fix for {label}"]))
        why = f"no handler/recipe/KB patch for {label} → LLM fix"
        if llm_failed:
            why += f" (attempt {len(llm_failed) + 1}/{limit}, previous attempts included in the prompt)"
        if ctx.stall_level:
            why += f"; stall level {ctx.stall_level}"
        return TriageDecision(cluster_id=cluster.id, kind=DecisionKind.LLM_FIX, rationale=why + policy_note + pin_note)
