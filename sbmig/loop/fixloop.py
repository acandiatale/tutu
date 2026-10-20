"""The build-fix loop (ARCHITECTURE §3).

Invariant: every applied change is followed by a real build. A loop round is
    build → score previous attempts (revert regressions) → stop on BUILD SUCCESS → cluster → decide → apply+commit
and the loop only exits *after* a build: on success, when the fix rounds are used up (the last round's changes are
still built and scored), when nothing actionable is left, when only blocked clusters remain, or when the run's time
budget is exceeded.

Loop safety (docs/ERROR-HANDLING.md §3): blocked clusters (run-wide `BlockedRegistry`) are skipped in every later
iteration and phase; oscillation (A↔B↔A) counts as a stall and, when it repeats, blocks the involved clusters;
structural kinds (pom_invalid / parent / plugin resolution) appearing after a pom edit revert that attempt regardless
of error counts; two LLM transport/config failures in a row mark the LLM unavailable for the run.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from sbmig.core.config import Config
from sbmig.core.interfaces import FixContext, TriageContext
from sbmig.core.models import (GAV, AdvisoryFinding, BlockReason, BuildError, BuildResult, DecisionKind, ErrorCluster, ErrorKind,
                               FixAttempt, IterationRecord, KBMatch, Phase, PhaseResult, ProjectProfile, RecipeRef,
                               TriageDecision, utcnow)
from sbmig.core.workspace import Workspace

from .blocked_registry import BlockedRegistry, attribute, compose_suggestion, error_key
from .budget import LoopBudget
from .components import profile_summary

log = logging.getLogger("sbmig.loop")

TEST_COMPILING_GOALS = ("test-compile", "package", "verify", "install", "deploy", "test")
TEST_RUNNING_GOALS = ("test", "package", "verify", "install", "deploy")
RESOLUTION_PREFIXES = ("parent_resolution:", "pom_invalid:", "dependency_resolution:", "plugin_resolution:")
# fixer analysis of a transport/config failure (vs. a protocol-garbage response, which is the model's fault)
_LLM_TRANSPORT_RE = re.compile(r"(?i)request failed|status=|\bhttp\b|timed? ?out|timeout|connect|connection|refused|"
                               r"unreachable|ssl|certificate|api[_ ]?key|unauthori[sz]ed|forbidden|\b40[13]\b|\b5\d\d\b|"
                               r"llmconfigerror|not set|config|unknown llm\.provider|missing component|cannot be imported|"
                               r"invalid json|cannot find the reply text|response_path|not found in the response")
# the gateway rejected *this request* for its content (too long, malformed): the prompt's fault, not the wire's —
# such a failure must not count towards "LLM unavailable for the run"
_LLM_REQUEST_REJECTED_RE = re.compile(r"status=4(?:00|13|22)\b")
_BLOCKED_RAT_RE = re.compile(r"blocked:\s*(?P<r>[a-z_]+)")


def is_llm_transport_error(analysis: str) -> bool:
    a = (analysis or "").strip()
    if not a.lower().startswith("llm-error:"):
        return False
    if "did not follow the protocol" in a:
        return False
    if _LLM_REQUEST_REJECTED_RE.search(a):
        return False
    return bool(_LLM_TRANSPORT_RE.search(a))


def build_target_args(profile: Optional[ProjectProfile], target: Optional[str]) -> tuple[Optional[str], list[str]]:
    """(module, extra_args) for building `target`. When a root aggregator pom lists the target among its modules, build
    from the root with `-pl <target> -am` (in-repo library roots get built too, error paths stay root-relative);
    otherwise build inside the target directory."""
    if not target or profile is None:
        return target, []
    t = target.strip("/")
    for p in profile.poms or []:
        if p.path == "pom.xml" and p.modules:
            mods = {m.strip("/").removeprefix("./") for m in p.modules}
            if t in mods:
                return None, ["-pl", t, "-am"]
    return target, []


def _reduced(c: ErrorCluster, errors: list[BuildError]) -> ErrorCluster:
    sigs: list[str] = []
    for e in errors:
        s = e.signature or f"{e.kind.value}|{e.message}"
        if s not in sigs:
            sigs.append(s)
    return c.model_copy(update={"errors": list(errors), "signatures": sigs})


@dataclass
class _Round:
    phase: Phase
    iteration: int
    build: BuildResult
    target: Optional[str]
    stall_level: int
    others: dict
    n: int = 0
    applied_any: bool = False
    pending: list = field(default_factory=list)
    decisions: list = field(default_factory=list)
    recipe_batch: list = field(default_factory=list)
    hopeless: set = field(default_factory=set)
    handlers_applied: set = field(default_factory=set)


@dataclass
class _Pending:
    attempt: FixAttempt
    pre_sha: str
    post_sha: str
    kb_recipe_ids: tuple[str, ...] = ()


def _label(c: ErrorCluster) -> str:
    try:
        from sbmig.triage.cluster import cluster_label
        return cluster_label(c)
    except Exception:  # pragma: no cover - triage is ours, but stay defensive
        return c.id


def _related(cluster: ErrorCluster, attempts: list[FixAttempt]) -> list[FixAttempt]:
    sigs = set(cluster.signatures)
    return [a for a in attempts if a.cluster_id == cluster.id or (sigs and sigs & set(a.cluster_signatures))]


class BuildFixLoop:
    def __init__(self, cfg: Config, ws: Workspace, build_runner: Any, triager: Any, handlers: Any, fixer: Any,
                 kb: Any, rewrite_runner: Any, profile: ProjectProfile, plan_notes: Optional[list[str]] = None,
                 budget: Optional[LoopBudget] = None, *, dry_run: bool = False,
                 history: Optional[list[FixAttempt]] = None, blocked: Optional[BlockedRegistry] = None,
                 locator: Any = None, advisories: Optional[list[AdvisoryFinding]] = None):
        self.cfg = cfg
        self.ws = ws
        self.build_runner = build_runner
        self.triager = triager
        self.handlers = handlers
        self.fixer = fixer
        self.kb = kb
        self.rewrite_runner = rewrite_runner
        self.profile = profile
        self.plan_notes = list(plan_notes or [])
        self.budget = budget or LoopBudget.from_config(cfg)
        self.dry_run = dry_run
        self.history: list[FixAttempt] = history if history is not None else []
        self.flags: list[str] = []
        self.followups: list[str] = []
        self.credited: list[str] = []
        self.escalated: dict[str, TriageDecision] = {}
        self.test_compile_skipped = False
        self.tests_skipped = False               # round 8: only runtime test failures left → packaged with -DskipTests
        self._profile_summary: Optional[str] = None
        # run-wide blocked state (shared by every phase of the run through the orchestrator)
        self.blocked = blocked if blocked is not None else BlockedRegistry()
        self.locator = locator                   # owner class → artifact (sbmig.build.classpath); None = no attribution
        # library advisory findings of the run (round 6): matched per cluster → FixContext.advisory_hints
        self.advisories: list[AdvisoryFinding] = list(advisories or [])
        self.llm_unavailable: Optional[str] = None
        self.aborted: Optional[str] = None       # set when the run must stop (configured parent not resolvable)
        self._llm_streak: list[tuple[ErrorCluster, str]] = []
        self._phase_items: list[str] = []
        self._cur_phase: Phase = Phase.JDK21
        attach = getattr(triager, "attach_run", None)
        if callable(attach):
            try:
                attach(blocked_registry=self.blocked, ws=ws)
            except Exception as e:  # pragma: no cover - a foreign triager
                log.warning("triager.attach_run failed: %s", e)

    # ------------------------------------------------------------------ public
    def run(self, phase: Phase, goal: str, max_iterations: int, target: Optional[str] = None) -> PhaseResult:
        pr = PhaseResult(phase=phase)
        self._phase_items = []
        self._cur_phase = phase
        self.ws.journal({"event": "loop_start", "phase": phase.value, "goal": goal, "max_iterations": max_iterations,
                         "target": target, "dry_run": self.dry_run, "blocked_known": len(self.blocked)})
        prev_build: Optional[BuildResult] = None
        prev_multiset: Optional[dict[str, int]] = None
        window: list[dict[str, int]] = []        # error multisets of the previous builds (oscillation detection)
        oscillations = 0
        stall_level = 0
        pending: list[_Pending] = []
        fix_rounds = 0
        build_no = 0
        last: Optional[BuildResult] = None
        while True:
            build_no += 1
            build = self._build(goal, target, f"{phase.value}-{build_no}", phase, build_no)
            last = build
            pr.iterations = build_no
            reverted = self._score(pr, pending, build, prev_build)
            pending = []
            rec = IterationRecord(phase=phase, iteration=build_no, build_ok=build.ok, error_count=build.error_count,
                                  signatures=build.signature_multiset(), build_log=build.log_path)
            pr.iteration_records.append(rec)
            if build.ok:
                pr.build_ok = True
                break
            if reverted:
                pr.notes.append(f"build {build_no}: regressed attempt(s) reverted → rebuilding the reverted tree")
                continue
            if self._parent_abort(pr, phase, build, build_no):
                break
            multiset = build.signature_multiset()
            stalled = prev_multiset is not None and multiset == prev_multiset
            oscillating = (not stalled) and any(m == multiset for m in window[:-1])
            stall_level = stall_level + 1 if (stalled or oscillating) else 0
            rec.stalled, rec.stall_level = (stalled or oscillating), stall_level
            if stalled:
                self.ws.journal({"event": "stall", "phase": phase.value, "iteration": build_no,
                                 "stall_level": stall_level, "error_count": build.error_count})
            if oscillating:
                oscillations += 1
                self.ws.journal({"event": "oscillation", "phase": phase.value, "iteration": build_no,
                                 "count": oscillations, "stall_level": stall_level, "error_count": build.error_count})
                pr.notes.append(f"build {build_no}: error set equals an earlier one (oscillation #{oscillations}) — "
                                "treated as a stall")
            window = (window + [multiset])[-max(2, self.cfg.loop.oscillation_window):]
            clusters = self.triager.cluster(build.errors, self.profile)
            if oscillating and oscillations >= 2:
                self._block_oscillation(pr, phase, build_no, clusters)
            if self._only_blocked(pr, clusters, build_no):
                break
            if fix_rounds >= max_iterations:
                pr.notes.append(f"max iterations ({max_iterations}) reached with {build.error_count} error(s) left")
                break
            if self.budget.time_exceeded():
                pr.notes.append("run time budget exhausted: stopping the build-fix loop")
                self._flag("time-budget-exhausted")
                self.ws.journal({"event": "budget_exhausted", "phase": phase.value, "kind": "time"})
                self._block_remaining(pr, phase, build_no, clusters, BlockReason.BUDGET_EXHAUSTED,
                                      f"run time budget ({self.cfg.loop.max_total_minutes} min) exhausted before this "
                                      "cluster was fixed")
                break
            applied_any = False
            new_pending: list[_Pending] = []
            while True:
                fix_rounds += 1
                new_pending, applied_any, decisions, llm_tried = self._fix_round(pr, phase, build_no, build, target,
                                                                                 stall_level, clusters)
                rec.decisions += decisions
                rec.cluster_count = len(rec.decisions)
                rec.attempt_ids = [a.id for a in pr.attempts if a.iteration == build_no]
                if self.dry_run or applied_any or not llm_tried or fix_rounds >= max_iterations \
                        or self.budget.time_exceeded() or all(self.blocked.covering(c) for c in clusters):
                    break
                # the LLM produced nothing applicable: the tree is unchanged, so a rebuild would show the same errors —
                # ask again on the same build (bounded by max_llm_attempts_per_cluster, then BLOCKED)
                self.ws.journal({"event": "reround", "phase": phase.value, "iteration": build_no,
                                 "fix_round": fix_rounds + 1})
            pending = new_pending
            if self.dry_run:
                pr.notes.append("dry-run: decisions computed and journaled, nothing applied")
                break
            if not applied_any:
                if self._only_blocked(pr, clusters, build_no):
                    break
                pr.notes.append(f"build {build_no}: no actionable fixes (all clusters skipped/escalated/blocked/failed "
                                "to apply) — rebuilding would not change anything")
                break
            prev_build, prev_multiset = build, multiset

        assert last is not None
        if not pr.build_ok and not self.dry_run and not self.aborted:
            self._maybe_skip_test_compile(pr, phase, goal, target, last)
        if not pr.build_ok and not self.dry_run and not self.aborted:
            self._maybe_skip_tests(pr, phase, goal, target, last)
        if pr.build_ok and not self.test_compile_skipped_in(pr) and not self.tests_skipped_in(pr):
            pr.remaining_errors = []
        elif not pr.remaining_errors:
            pr.remaining_errors = list(last.errors)
        pr.ok = pr.build_ok
        pr.blocked = [it for it in (self.blocked.get(i) for i in self._phase_items) if it is not None]
        pr.finished_at = utcnow()
        self.ws.journal({"event": "loop_end", "phase": phase.value, "ok": pr.ok, "iterations": pr.iterations,
                         "attempts": len(pr.attempts), "remaining_errors": len(pr.remaining_errors),
                         "blocked": [b.id for b in pr.blocked], "llm_calls_used": self.budget.llm_calls,
                         "aborted": self.aborted})
        return pr

    # ------------------------------------------------------------------ blocked bookkeeping
    def _touch_item(self, item_id: str) -> None:
        if item_id not in self._phase_items:
            self._phase_items.append(item_id)

    def _only_blocked(self, pr: PhaseResult, clusters: list[ErrorCluster], build_no: int) -> bool:
        if not clusters:
            return False
        items = [self.blocked.covering(c) for c in clusters]
        if not all(items):
            return False
        for it in items:
            self._touch_item(it.id)
        ids = list(dict.fromkeys(it.id for it in items))
        pr.notes.append(f"build {build_no}: only blocked cluster(s) remain ({len(clusters)} cluster(s), "
                        f"{len(ids)} blocked item(s)) — ending the phase; see 'Blocked' in the report")
        self.ws.journal({"event": "only_blocked", "phase": pr.phase.value, "iteration": build_no,
                         "clusters": [c.id for c in clusters], "blocked": ids})
        return True

    def _candidate_for(self, c: ErrorCluster, d: TriageDecision):
        from sbmig.triage.blocked import BlockedCandidate
        getter = getattr(self.triager, "blocked_candidate", None)
        cand = None
        if callable(getter):
            try:
                cand = getter(c.id)
            except Exception:  # pragma: no cover
                cand = None
        if cand is None:
            m = _BLOCKED_RAT_RE.search(d.rationale or "")
            try:
                reason = BlockReason(m.group("r")) if m else BlockReason.REPEATED_FAILURE
            except ValueError:
                reason = BlockReason.REPEATED_FAILURE
            cand = BlockedCandidate(reason=reason, evidence=[d.rationale], errors=list(c.errors))
        return cand

    def _register_blocked(self, phase: Phase, iteration: int, c: ErrorCluster, cand, rationale: str,
                          target: Optional[str] = None) -> list[BuildError]:
        """Record a BlockedItem for the errors `cand` covers; returns the cluster errors that are NOT blocked."""
        covered = {error_key(e) for e in (cand.errors or c.errors)}
        errs = [e for e in c.errors if error_key(e) in covered] or list(c.errors)
        rest = [e for e in c.errors if error_key(e) not in covered]
        attempts = len(_related(c, self.history))
        reason = cand.reason
        artifact: Optional[GAV] = None
        owner = cand.owner_class
        notes: list[str] = []
        required, found = cand.required_type, cand.found_type
        if reason in (BlockReason.EXTERNAL_JAVAX_API, BlockReason.EXTERNAL_API_INCOMPATIBLE):
            owner, required, found = self._classpath_hints(errs, owner, required, found)
            artifact, owner, notes = attribute(self.locator, self.ws, self.cfg, owner, errs, c.module or target)
        elif reason in (BlockReason.ARTIFACT_NOT_FOUND, BlockReason.PARENT_NOT_FOUND):
            artifact = next((e.artifact for e in errs if e.artifact is not None), None)
        elif getattr(cand, "plugin", None):
            try:
                artifact = GAV.parse(cand.plugin)
            except ValueError:
                artifact = None
        item, created = self.blocked.block(c, reason, phase, errors=errs, iteration=iteration, attempts=attempts,
                                           required_type=required, found_type=found,
                                           owner_class=owner, artifact=artifact, notes=[rationale, *notes])
        if created or not item.suggestion:
            extra = ""
            if reason == BlockReason.PLUGIN_INCOMPATIBLE:
                extra = getattr(cand, "plugin", None) or ""
            elif reason == BlockReason.LLM_UNAVAILABLE:
                extra = self.llm_unavailable or ""
            item.suggestion = compose_suggestion(item, self.cfg, settings_file=self.cfg.build.settings_file,
                                                 repositories=list(getattr(cand, "repositories", []) or []),
                                                 extra=extra)
        self._touch_item(item.id)
        self.ws.journal({"event": "blocked", "phase": phase.value, "iteration": iteration, "id": item.id,
                         "created": created, "reason": reason.value, "cluster": c.id,
                         "signatures": [e.signature for e in errs][:10], "files": item.files[:10],
                         "artifact": item.artifact.coordinates if item.artifact else None,
                         "owner_class": item.owner_class, "required_type": item.required_type,
                         "found_type": item.found_type, "attempts": item.attempts, "suggestion": item.suggestion,
                         "rest_errors": len(rest)})
        return rest

    def _classpath_hints(self, errs: list[BuildError], owner: Optional[str], required: Optional[str],
                         found: Optional[str]) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """Fill gaps with sbmig.build.classpath.owner_fqcn / type_mismatch (G1) when the locator provides them."""
        loc = self.locator
        if loc is None:
            return owner, required, found
        for e in errs[:3]:
            try:
                if owner is None and hasattr(loc, "owner_fqcn"):
                    o = loc.owner_fqcn(e)
                    if o and not o.startswith(("javax.", "jakarta.")):   # a bare mismatch names the type, not an owner
                        owner = o
                if (required is None or found is None) and hasattr(loc, "type_mismatch"):
                    r, f = loc.type_mismatch(e)
                    required, found = required or r, found or f
            except Exception as ex:  # pragma: no cover - attribution is best effort
                log.debug("classpath hints failed: %s", ex)
        return owner, required, found

    def _block_simple(self, phase: Phase, iteration: int, c: ErrorCluster, reason: BlockReason, why: str) -> None:
        from sbmig.triage.blocked import BlockedCandidate
        _blocked, rest = self.blocked.partition(c)
        if not rest:
            return
        cc = _reduced(c, rest)
        self._register_blocked(phase, iteration, cc, BlockedCandidate(reason=reason, evidence=[why], errors=rest),
                               f"blocked: {reason.value} — {why}")

    def _block_remaining(self, pr: PhaseResult, phase: Phase, iteration: int, clusters: list[ErrorCluster],
                         reason: BlockReason, why: str) -> None:
        for c in clusters:
            self._block_simple(phase, iteration, c, reason, why)

    def _block_oscillation(self, pr: PhaseResult, phase: Phase, iteration: int, clusters: list[ErrorCluster]) -> None:
        phase_attempted = {a.cluster_id for a in pr.attempts} | {s for a in pr.attempts for s in a.cluster_signatures}
        involved = [c for c in clusters if c.id in phase_attempted or set(c.signatures) & phase_attempted] or clusters
        why = "fixes keep toggling between the same error sets (A↔B↔A) within loop.oscillation_window"
        pr.notes.append(f"build {iteration}: oscillation repeated → blocking {len(involved)} cluster(s)")
        self._block_remaining(pr, phase, iteration, involved, BlockReason.OSCILLATION, why)

    def _parent_abort(self, pr: PhaseResult, phase: Phase, build: BuildResult, iteration: int) -> bool:
        """The configured in-house parent is not resolvable → nothing can build: abort the run with a clear item."""
        parent = self.cfg.artifacts.parent
        keys = {parent.key, *parent.aliases}
        errs = [e for e in build.errors if e.kind == ErrorKind.PARENT_RESOLUTION
                and e.artifact is not None and e.artifact.key in keys]
        if not errs:
            return False
        from sbmig.triage.blocked import BlockedCandidate, repositories_in
        c = self.triager.cluster(errs, self.profile)[0]
        coords = errs[0].artifact.coordinates
        cand = BlockedCandidate(reason=BlockReason.PARENT_NOT_FOUND, errors=errs,
                                repositories=repositories_in("\n".join(f"{e.message}\n{e.raw}" for e in errs)),
                                evidence=[errs[0].message[:300]])
        self._register_blocked(phase, iteration, c, cand, f"blocked: parent_not_found — configured parent {coords} "
                                                          "is not resolvable; run aborted")
        self.aborted = (f"the configured in-house parent {coords} is not resolvable (settings: "
                        f"{self.cfg.build.settings_file or 'default'}) — fix the repository/settings or "
                        "artifacts.parent and rerun")
        self._flag("aborted:parent-not-found")
        self._followup(f"run aborted: {self.aborted}")
        pr.notes.append(f"build {iteration}: {self.aborted}")
        self.ws.journal({"event": "abort", "phase": phase.value, "iteration": iteration, "reason": self.aborted})
        return True

    @staticmethod
    def test_compile_skipped_in(pr: PhaseResult) -> bool:
        return any("test-compile-skipped" in n for n in pr.notes)

    @staticmethod
    def tests_skipped_in(pr: PhaseResult) -> bool:
        return any("tests-skipped:" in n for n in pr.notes)

    # ------------------------------------------------------------------ build
    def _build(self, goal: str, target: Optional[str], label: str, phase: Phase, iteration: int,
               skip_test_compile: bool = False, skip_tests: bool = False) -> BuildResult:
        module, extra = build_target_args(self.profile, target)
        kw: dict[str, Any] = {"module": module, "label": label, "skip_test_compile": skip_test_compile}
        if extra:
            kw["extra_args"] = extra
        if skip_tests:
            try:
                res = self.build_runner.run(goal, **kw, skip_tests=True)
            except TypeError:  # an older runner without the `skip_tests` option: pass -DskipTests as an argument
                kw["extra_args"] = [*extra, "-DskipTests"]
                res = self.build_runner.run(goal, **kw)
        else:
            res = self.build_runner.run(goal, **kw)
        self.ws.journal({"event": "build", "phase": phase.value, "iteration": iteration, "goal": goal,
                         "target": target, "extra_args": extra, "ok": res.ok, "exit_code": res.exit_code, "errors": res.error_count,
                         "duration_s": res.duration_s, "log": res.log_path, "skip_test_compile": skip_test_compile,
                         "skip_tests": skip_tests})
        return res

    # ------------------------------------------------------------------ scoring
    def _score(self, pr: PhaseResult, pending: list[_Pending], build: BuildResult,
               prev: Optional[BuildResult]) -> bool:
        """Score the previous round's attempts against `build`. Returns True when a revert changed the tree."""
        if not pending:
            return False
        cur = {e.signature for e in build.errors}
        prev_sigs = {e.signature for e in prev.errors} if prev is not None else set()
        total = build.error_count
        ratio = self.cfg.loop.regression_ratio
        struct_kinds = set(self.cfg.loop.structural_regression_kinds)
        prev_kinds = {e.kind.value for e in prev.errors} if prev is not None else set()
        new_struct = sorted({e.kind.value for e in build.errors if e.kind.value in struct_kinds} - prev_kinds)
        structural: set[str] = set()
        for p in pending:
            a = p.attempt
            a.errors_after = total
            if build.ok:
                a.outcome, a.cluster_resolved = "build_ok", True
                continue
            a.cluster_resolved = not (set(a.cluster_signatures) & cur)
        any_resolved = any(p.attempt.cluster_resolved for p in pending)
        for p in pending:
            a = p.attempt
            if build.ok:
                continue
            touched = {c.path for c in a.changes} | {c.new_path for c in a.changes if c.new_path}
            if new_struct and any(t and t.replace("\\", "/").rsplit("/", 1)[-1] == "pom.xml" for t in touched):
                a.outcome = "regressed"
                a.notes.append(f"regressed: structural error kind(s) {', '.join(new_struct)} appeared after this "
                               "attempt edited a pom.xml → reverted regardless of error counts")
                structural.add(a.id)
                continue
            new_in_touched = [e for e in build.errors if e.file and e.file in touched and e.signature not in prev_sigs]
            ratio_regress = (not any_resolved) and total > max(a.errors_before, 1) * ratio
            if not a.cluster_resolved and (new_in_touched or ratio_regress):
                a.outcome = "regressed"
                why = (f"{len(new_in_touched)} new error(s) in touched files" if new_in_touched
                       else f"errors {a.errors_before} → {total} (> ×{ratio})")
                a.notes.append(f"regressed: {why}")
            else:
                a.outcome = "improved" if a.cluster_resolved else "no_change"
        # credit KB recipes
        for p in pending:
            for rid in p.kb_recipe_ids:
                ok = p.attempt.outcome in ("improved", "build_ok")
                try:
                    self.kb.credit_recipe(rid, ok, self.profile.fingerprint)
                    self.credited.append(f"{rid} ({'success' if ok else 'failure'})")
                except Exception as e:  # pragma: no cover - KB problems never break the loop
                    log.warning("kb.credit_recipe(%s) failed: %s", rid, e)
        reverted = False
        if not build.ok and self.cfg.loop.revert_on_regression and any(p.attempt.outcome == "regressed" for p in pending):
            commits: list[str] = []
            for p in pending:
                if p.post_sha not in commits:
                    commits.append(p.post_sha)
            for sha in reversed(commits):
                group = [p for p in pending if p.post_sha == sha]
                if not all(p.attempt.outcome == "regressed" for p in group):
                    break
                self.ws.revert_to(group[0].pre_sha)
                reverted = True
                for p in group:
                    p.attempt.outcome = "reverted"
                    p.attempt.notes.append(f"reverted to {group[0].pre_sha[:10]}")
                self.ws.journal({"event": "revert", "phase": pr.phase.value, "to": group[0].pre_sha,
                                 "commit": sha, "attempts": [p.attempt.id for p in group]})
            # structural regressions are reverted even when later commits of the round depend on them (git revert)
            for p in pending:
                a = p.attempt
                if a.outcome != "regressed" or a.id not in structural:
                    continue
                group = [q for q in pending if q.post_sha == p.post_sha]
                res = self.ws._git("revert", "--no-edit", p.post_sha, check=False)   # noqa: SLF001
                if res.returncode != 0:
                    self.ws._git("revert", "--abort", check=False)                      # noqa: SLF001
                    a.notes.append(f"structural revert of {p.post_sha[:10]} failed: {(res.stderr or '').strip()[:200]}")
                    continue
                reverted = True
                for q in group:
                    q.attempt.outcome = "reverted"
                    q.attempt.notes.append(f"reverted commit {p.post_sha[:10]} (git revert: structural regression)")
                self.ws.journal({"event": "revert", "phase": pr.phase.value, "commit": p.post_sha, "mode": "git-revert",
                                 "structural": new_struct, "attempts": [q.attempt.id for q in group]})
            for p in pending:
                if p.attempt.outcome == "regressed":
                    p.attempt.notes.append("not reverted: later commits that improved things depend on it")
        for p in pending:
            self.ws.journal({"event": "attempt_scored", "phase": pr.phase.value, "attempt": p.attempt.id,
                             "cluster": p.attempt.cluster_id, "outcome": p.attempt.outcome,
                             "cluster_resolved": p.attempt.cluster_resolved, "errors_after": total})
        return reverted

    # ------------------------------------------------------------------ fixing
    def _kb_match(self, cluster: ErrorCluster) -> KBMatch:
        try:
            m = self.kb.match(cluster, self.ws, self.profile)
            return m if m is not None else KBMatch()
        except Exception as e:
            log.warning("kb.match failed for %s: %s", cluster.id, e)
            return KBMatch()

    def _plan(self, clusters: list[ErrorCluster]) -> list[tuple[ErrorCluster, Optional[TriageDecision], list[str]]]:
        planner = getattr(self.triager, "plan_iteration", None)
        if not callable(planner):
            return [(c, None, []) for c in clusters]
        return [(it.cluster, it.skip, list(it.masked_by)) for it in planner(clusters)]

    def _decide(self, phase: Phase, iteration: int, stall_level: int, cluster: ErrorCluster,
                match: KBMatch) -> TriageDecision:
        ctx = TriageContext(phase=phase, iteration=iteration, stall_level=stall_level,
                            previous_attempts=_related(cluster, self.history), llm_calls_used=self.budget.llm_calls,
                            llm_calls_budget=self.budget.max_llm_calls)
        try:
            return self.triager.decide(cluster, match, ctx, self.profile)
        except Exception as e:
            log.exception("triage.decide failed for %s", cluster.id)
            return TriageDecision(cluster_id=cluster.id, kind=DecisionKind.ESCALATE,
                                  rationale=f"triage failed: {type(e).__name__}: {e}")

    def _fix_round(self, pr: PhaseResult, phase: Phase, iteration: int, build: BuildResult, target: Optional[str],
                   stall_level: int, clusters: Optional[list[ErrorCluster]] = None
                   ) -> tuple[list[_Pending], bool, list[TriageDecision], bool]:
        """One fix round. Returns (pending attempts, anything applied, decisions, an LLM attempt was made)."""
        if clusters is None:
            clusters = self.triager.cluster(build.errors, self.profile)
        planned = self._plan(clusters)
        limit = max(1, self.cfg.loop.max_clusters_per_iteration)
        self.ws.journal({"event": "triage", "phase": phase.value, "iteration": iteration,
                         "clusters": [{"id": c.id, "kind": c.kind.value, "file": c.file, "module": c.module,
                                       "errors": len(c.errors), "priority": c.priority} for c in clusters],
                         "masked": [c.id for c, skip, _ in planned if skip is not None]})
        st = _Round(phase=phase, iteration=iteration, build=build, target=target, stall_level=stall_level,
                    others={c.id: _label(c) for c in clusters},
                    n=sum(1 for a in pr.attempts if a.iteration == iteration))   # ids stay unique across re-rounds
        masked: list[tuple[ErrorCluster, TriageDecision, list[str]]] = []
        handled = 0
        llm_before = sum(1 for a in pr.attempts if a.decision.kind == DecisionKind.LLM_FIX)
        for c, skip, by in planned:
            c2 = self._unblocked_part(st, c)
            if c2 is None:
                continue
            if skip is not None:
                masked.append((c2, skip, by))
                continue
            if handled >= limit:
                continue
            if self._handle_cluster(pr, st, c2) != "blocked":
                handled += 1
        # unmask: a masker that cannot be acted upon must not hide compile errors we could fix
        for c, skip, by in masked:
            if by and all(b in st.hopeless for b in by) and handled < limit:
                if self._handle_cluster(pr, st, c, prefix="(unmasked: its pom problem was escalated/blocked) ") \
                        != "blocked":
                    handled += 1
            else:
                st.decisions.append(skip)
                self._journal_decision(st, skip)
        if st.recipe_batch:
            changed = self._run_recipe_batch(pr, st)
            st.applied_any |= changed
            if not changed:
                # recipes did nothing: give those clusters their next option in this same round
                retry, st.recipe_batch = st.recipe_batch, []
                for c, _d, _m in retry:
                    self._handle_cluster(pr, st, c, prefix="(after a no-op recipe run) ", allow_recipes=False)
        llm_tried = sum(1 for a in pr.attempts if a.decision.kind == DecisionKind.LLM_FIX) > llm_before
        return st.pending, st.applied_any, st.decisions, llm_tried

    def _unblocked_part(self, st: "_Round", c: ErrorCluster) -> Optional[ErrorCluster]:
        """Drop the errors the run already blocked; None when nothing is left (decision BLOCKED is recorded)."""
        blocked, rest = self.blocked.partition(c)
        if not blocked:
            return c
        if rest:
            return _reduced(c, rest)
        item = self.blocked.covering(c)
        assert item is not None
        self._touch_item(item.id)
        st.hopeless.add(c.id)
        d = TriageDecision(cluster_id=c.id, kind=DecisionKind.BLOCKED,
                           rationale=f"blocked: {item.reason.value} — already blocked ({item.id}, first seen in "
                                     f"{item.phase.value}); skipped")
        st.decisions.append(d)
        self._journal_decision(st, d)
        return None

    def _journal_decision(self, st: "_Round", d: TriageDecision) -> None:
        self.ws.journal({"event": "decision", "phase": st.phase.value, "iteration": st.iteration,
                         **d.model_dump(mode="json")})

    def _handle_cluster(self, pr: PhaseResult, st: "_Round", c: ErrorCluster, prefix: str = "",
                        allow_recipes: bool = True) -> str:
        """Decide → apply → commit; a deterministic option that changes nothing falls through to the next one.
        Returns "blocked" (nothing left to do for this cluster), "escalated", "skipped", "recipe" or "attempted"."""
        from sbmig.triage.blocked import BlockedCandidate
        m = self._kb_match(c)
        for _ in range(6):
            d = self._decide(st.phase, st.iteration, st.stall_level, c, m)
            if prefix:
                d.rationale = prefix + d.rationale
            forced = None
            if d.kind == DecisionKind.LLM_FIX and self.llm_unavailable:
                forced = BlockedCandidate(reason=BlockReason.LLM_UNAVAILABLE, errors=list(c.errors),
                                          evidence=[f"LLM unavailable ({self.llm_unavailable})"])
            elif d.kind == DecisionKind.LLM_FIX and not self.budget.llm_allowed():
                forced = BlockedCandidate(reason=BlockReason.BUDGET_EXHAUSTED, errors=list(c.errors),
                                          evidence=[f"LLM budget exhausted ({self.budget.llm_calls}/"
                                                    f"{self.budget.max_llm_calls} calls)"])
            if forced is not None:
                d = TriageDecision(cluster_id=c.id, kind=DecisionKind.BLOCKED,
                                   rationale=prefix + f"blocked: {forced.reason.value} — {forced.evidence[0]}")
            if d.kind == DecisionKind.KB_RECIPE and not allow_recipes:
                d = TriageDecision(cluster_id=c.id, kind=DecisionKind.ESCALATE,
                                   rationale=prefix + "escalated: only recipes left and they changed nothing")
            st.decisions.append(d)
            self._journal_decision(st, d)
            if d.kind == DecisionKind.ESCALATE:
                self.escalated[c.id] = d
                st.hopeless.add(c.id)
                return "escalated"
            if d.kind == DecisionKind.BLOCKED:
                cand = forced or self._candidate_for(c, d)
                rest = self._register_blocked(st.phase, st.iteration, c, cand, d.rationale, st.target)
                if not rest:
                    st.hopeless.add(c.id)
                    return "blocked"
                # only part of the cluster is blocked: keep working on the rest of the file
                c = _reduced(c, rest)
                prefix = "(rest of a partially blocked cluster) "
                continue
            if d.kind == DecisionKind.SKIP or self.dry_run:
                return "skipped"
            if d.kind == DecisionKind.KB_RECIPE:
                st.recipe_batch.append((c, d, m))
                return "recipe"
            st.n += 1
            att = self._new_attempt(st.phase, st.iteration, st.n, c, d, st.build)
            pre = self.ws.head()
            if d.kind == DecisionKind.HANDLER:
                self._apply_handler(att, c)
                msg = f"fix(handler:{d.handler_id}): {c.id}"
                if att.outcome == "apply_failed" and d.handler_id in st.handlers_applied:
                    # whole-module handler (source-encoding, strip-utf8-bom …) already fixed this cluster's module in
                    # this iteration: not a failure — wait for the rebuild instead of falling through to the LLM
                    att.outcome, att.apply_errors = "no_change", []
                    att.notes.append(f"handler {d.handler_id} already applied in this iteration (module-wide fix); "
                                     "waiting for the rebuild")
                    self._record(pr, att)
                    return "attempted"
            elif d.kind == DecisionKind.KB_PATCH:
                self._apply_kb_patch(att, c, m)
                msg = f"fix(kb-patch:{','.join(d.kb_case_ids)}): {c.id}"
            else:
                self._apply_llm(att, c, m, st.phase, st.iteration, st.stall_level, st.others)
                msg = f"fix(llm): {c.id} {att.model or 'llm'}"
            if att.outcome != "apply_failed":
                sha = self._commit(msg, pre)
                if sha is None:
                    att.outcome = "apply_failed"
                    att.apply_errors.append("no file changed")
                else:
                    att.applied, att.commit = True, sha
                    st.pending.append(_Pending(att, pre, sha))
                    st.applied_any = True
                    if d.kind == DecisionKind.HANDLER and d.handler_id:
                        st.handlers_applied.add(d.handler_id)
            self._record(pr, att)
            if att.applied or d.kind == DecisionKind.LLM_FIX:
                return "attempted"      # never call the LLM twice for one cluster in one round
        return "attempted"

    def _new_attempt(self, phase: Phase, iteration: int, n: int, c: ErrorCluster, d: TriageDecision,
                     build: BuildResult) -> FixAttempt:
        return FixAttempt(id=f"{phase.value}-{iteration}-{n}", phase=phase, iteration=iteration, cluster_id=c.id,
                          decision=d, errors_before=build.error_count, cluster_signatures=list(c.signatures),
                          cluster_errors=list(c.errors))   # KB harvest needs the errors (L2 signatures, sanitized copies)

    def _record(self, pr: PhaseResult, att: FixAttempt) -> None:
        pr.attempts.append(att)
        self.history.append(att)
        self.ws.journal({"event": "attempt", "phase": att.phase.value, "iteration": att.iteration,
                         "attempt": att.id, "cluster": att.cluster_id, "decision": att.decision.kind.value,
                         "handler": att.decision.handler_id, "recipes": att.decision.recipe_ids,
                         "applied": att.applied, "commit": att.commit, "outcome": att.outcome,
                         "model": att.model, "files": [ch.path for ch in att.changes],
                         "apply_errors": att.apply_errors, "analysis": att.analysis[:500],
                         "advisory_ids": list(att.advisory_ids)})

    def _commit(self, message: str, pre: str) -> Optional[str]:
        if not self.ws.is_dirty():
            return None
        sha = self.ws.commit(message)
        return sha if sha != pre else None

    def _fail(self, att: FixAttempt, why: str) -> None:
        att.outcome = "apply_failed"
        att.apply_errors.append(why)
        try:
            if self.ws.is_dirty():
                self.ws.discard_uncommitted()
        except Exception:  # pragma: no cover
            pass

    def _apply_handler(self, att: FixAttempt, c: ErrorCluster) -> None:
        h = self.handlers.get(att.decision.handler_id) if self.handlers is not None else None
        if h is None:
            return self._fail(att, f"handler '{att.decision.handler_id}' is not registered")
        try:
            att.changes = list(h.apply(self.ws, c, self.profile, self.cfg) or [])
        except Exception as e:
            log.exception("handler %s failed", h.id)
            return self._fail(att, f"handler {h.id} failed: {type(e).__name__}: {e}")
        att.analysis = h.description
        for f in list(getattr(h, "flags", None) or []):
            self._flag(str(f))
        for n in list(getattr(h, "notes", None) or []):
            if n not in att.notes:
                att.notes.append(str(n))
            if self.cfg is not None and str(n).lower().startswith(("install", "followup", "follow-up")):
                self._followup(str(n))
        if not att.changes:
            self._fail(att, f"handler {h.id} found nothing to change")

    def _apply_kb_patch(self, att: FixAttempt, c: ErrorCluster, m: KBMatch) -> None:
        case = next((k for k in m.replayable if k.id in att.decision.kb_case_ids), None)
        if case is None:
            return self._fail(att, "replayable KB case vanished")
        try:
            applied, errs = self.fixer.apply(self.ws, list(case.changes))
        except Exception as e:
            return self._fail(att, f"patch replay failed: {type(e).__name__}: {e}")
        att.changes, att.apply_errors = list(applied), list(errs)
        att.analysis = f"replayed KB case {case.id}: {case.summary}"
        if not applied:
            self._fail(att, "no KB patch block applied")

    def _summary(self) -> str:
        if self._profile_summary is None:
            self._profile_summary = profile_summary(self.profile)
        return self._profile_summary

    def _apply_llm(self, att: FixAttempt, c: ErrorCluster, m: KBMatch, phase: Phase, iteration: int,
                   stall_level: int, others: dict[str, str]) -> None:
        hints = m.hints[: self.cfg.kb.max_hints] if self.cfg.kb.use_hints else []
        other = "\n".join(v for k, v in others.items() if k != c.id)[:4000]
        adv_hints, att.advisory_ids = self._advisory_hints(c)
        ctx = FixContext(phase=phase, iteration=iteration, stall_level=stall_level,
                         profile_summary=self._summary(), plan_notes=self.plan_notes, kb_hints=hints,
                         advisory_hints=adv_hints, previous_attempts=_related(c, self.history),
                         other_errors_summary=other, use_escalation_model=stall_level >= self.cfg.loop.stall_limit)
        try:
            proposal = self.fixer.propose(self.ws, c, ctx)
        except Exception as e:
            self.budget.charge_llm(1)
            log.warning("fixer.propose failed for %s: %s", c.id, e)
            self._fail(att, f"LLM fixer failed: {type(e).__name__}: {e}")
            self._llm_transport_failure(att, c, f"{type(e).__name__}: {e}")
            return
        self.budget.charge_llm(max(1, proposal.llm_usage.calls))
        att.model, att.prompt_hash, att.llm_usage = proposal.model, proposal.prompt_hash, proposal.llm_usage
        att.analysis = proposal.analysis
        if is_llm_transport_error(proposal.analysis):
            self._fail(att, proposal.analysis.strip()[:300])
            self._llm_transport_failure(att, c, proposal.analysis.strip()[len("llm-error:"):].strip()[:300])
            return
        self._llm_streak = []
        if ctx.use_escalation_model:
            att.notes.append(f"escalation model requested (stall level {stall_level})")
        if not proposal.changes:
            why = "LLM proposed no changes"
            if proposal.requested_files:
                why += f" (asked for: {', '.join(proposal.requested_files[:5])})"
            return self._fail(att, why)
        try:
            applied, errs = self.fixer.apply(self.ws, list(proposal.changes))
        except Exception as e:
            return self._fail(att, f"applying LLM patch failed: {type(e).__name__}: {e}")
        att.changes, att.apply_errors = list(applied), list(errs)
        if not applied:
            self._fail(att, "no patch block applied")

    def _advisory_hints(self, c: ErrorCluster) -> tuple[list[str], list[str]]:
        """(hints, advisory ids) of the run's library advisories that match this cluster (sbmig.advisory.hints_for)."""
        if not self.advisories:
            return [], []
        try:
            from sbmig.advisory.hints import hints_for
            root = getattr(self.cfg, "repo_root", None)
            root = Path(root) if isinstance(root, (str, Path)) else None
            hints, ids = hints_for(c.errors, self.advisories, self.profile, notes_root=root)
        except Exception as e:  # pragma: no cover - hints_for never raises; stay defensive about imports
            log.warning("advisory hints unavailable: %s", e)
            return [], []
        if ids:
            log.debug("advisory hints for %s (%s):\n%s", c.id, ", ".join(ids), "\n".join(f"- {h}" for h in hints))
        return list(hints), list(ids)

    def _llm_transport_failure(self, att: FixAttempt, c: ErrorCluster, why: str) -> None:
        """Two transport/config failures in a row → the LLM is unavailable for the rest of the run (L5)."""
        self._llm_streak.append((c, why))
        att.notes.append(f"LLM transport/config failure: {why[:200]}")
        if self.llm_unavailable or len(self._llm_streak) < 2:
            return
        self.llm_unavailable = why[:300] or "LLM transport errors"
        self._flag("llm-unavailable")
        setter = getattr(self.triager, "set_llm_unavailable", None)
        if callable(setter):
            setter(self.llm_unavailable)
        self._followup(f"the LLM was unavailable during this run ({self.llm_unavailable}); clusters that needed it "
                       "are listed as blocked (llm_unavailable) — deterministic handlers and recipes still ran")
        self.ws.journal({"event": "llm_unavailable", "phase": att.phase.value, "iteration": att.iteration,
                         "reason": self.llm_unavailable, "clusters": [x.id for x, _ in self._llm_streak]})
        for x, _w in self._llm_streak:
            self._block_simple(att.phase, att.iteration, x, BlockReason.LLM_UNAVAILABLE,
                               f"LLM unavailable ({self.llm_unavailable})")
        self._llm_streak = []

    def _run_recipe_batch(self, pr: PhaseResult, st: "_Round") -> bool:
        phase, iteration, batch, build, target, pending = (st.phase, st.iteration, st.recipe_batch, st.build,
                                                          st.target, st.pending)
        refs: list[RecipeRef] = []
        files: list[str] = []
        kb_ids: set[str] = set()
        for c, d, m in batch:
            maker = getattr(self.triager, "recipe_refs", None)
            rs = maker(d, m) if callable(maker) else [RecipeRef(id=r) for r in d.recipe_ids]
            for r in rs:
                if not any(x.id == r.id for x in refs):
                    refs.append(r)
            for f in [*m.recipe_files, *m.candidate_recipe_files]:
                if f and f not in files:
                    files.append(f)
            kb_ids |= {r for r in d.recipe_ids if r in m.recipe_ids or r in m.candidate_recipe_ids}
        if self.cfg.kb.use_active_recipes:
            try:
                for f in self.kb.active_recipe_files():
                    if str(f) not in files:
                        files.append(str(f))
            except Exception as e:  # pragma: no cover
                log.warning("kb.active_recipe_files failed: %s", e)
        ids = [r.id for r in refs]
        mark = getattr(self.triager, "mark_recipes_run", None)
        if callable(mark):
            mark(ids, phase)
        pre = self.ws.head()
        error: Optional[str] = None
        try:
            from pathlib import Path
            kwargs = dict(extra_recipe_files=[Path(f) for f in files], module=target,
                          label=f"{phase.value}-{iteration}-kb")
            try:
                # the project does not compile at this point → rewrite:runNoFork (rewrite:run forks `compile` first)
                res = self.rewrite_runner.run(refs, fork=False, **kwargs)
            except TypeError:  # a runner without the `fork` option
                res = self.rewrite_runner.run(refs, **kwargs)
            pr.rewrite_results.append(res)
            if not res.ok:
                error = res.error or "rewrite failed"
        except Exception as e:
            error = f"rewrite run failed: {type(e).__name__}: {e}"
        self.ws.journal({"event": "recipe_batch", "phase": phase.value, "iteration": iteration, "recipes": ids,
                         "clusters": [c.id for c, _, _ in batch], "error": error})
        sha = self._commit(f"fix(recipe): {', '.join(ids)}", pre)
        if sha is None and self.ws.is_dirty():  # pragma: no cover - defensive
            self.ws.discard_uncommitted()
        for c, d, _m in batch:
            st.n += 1
            att = self._new_attempt(phase, iteration, st.n, c, d, build)
            att.analysis = f"OpenRewrite: {', '.join(d.recipe_ids)}"
            if error:
                att.apply_errors.append(error)
            if sha is None:
                att.outcome = "apply_failed"
                att.apply_errors.append("recipes changed no file")
            else:
                att.applied, att.commit = True, sha
                try:
                    att.changes = [_modify(p) for p in self._changed_between(pre, sha)]
                except Exception:  # pragma: no cover
                    att.changes = []
                pending.append(_Pending(att, pre, sha, tuple(r for r in d.recipe_ids if r in kb_ids)))
            self._record(pr, att)
        return sha is not None

    def _changed_between(self, a: str, b: str) -> list[str]:
        out = self.ws._git("diff", "--name-only", a, b).stdout   # noqa: SLF001 - Workspace has no public API for this
        return [l.strip() for l in out.splitlines() if l.strip()]

    # ------------------------------------------------------------------ fallback
    def _maybe_skip_test_compile(self, pr: PhaseResult, phase: Phase, goal: str, target: Optional[str],
                                 last: BuildResult) -> None:
        if not self.cfg.build.allow_skip_test_compile_fallback:
            return
        if not any(g in goal.split() for g in TEST_COMPILING_GOALS):
            return
        if not last.errors or any(e.kind != ErrorKind.TEST_COMPILE for e in last.errors):
            return
        self.ws.journal({"event": "test_compile_fallback", "phase": phase.value, "test_errors": len(last.errors)})
        res = self._build(goal, target, f"{phase.value}-skip-tests", phase, pr.iterations + 1, skip_test_compile=True)
        pr.iterations += 1
        pr.iteration_records.append(IterationRecord(phase=phase, iteration=pr.iterations, build_ok=res.ok,
                                                    error_count=res.error_count,
                                                    signatures=res.signature_multiset(), build_log=res.log_path))
        if not res.ok:
            pr.notes.append("test-compile fallback (-Dmaven.test.skip=true) also failed")
            return
        pr.build_ok = True
        self.test_compile_skipped = True
        self._flag("test-compile-skipped")
        pr.remaining_errors = list(last.errors)
        pr.notes.append(f"test-compile-skipped: main code builds, {len(last.errors)} test compile error(s) remain "
                        "(-Dmaven.test.skip=true)")
        for e in last.errors:
            where = f"{e.file}:{e.line}" if e.file else (e.module or "?")
            self._followup(f"test sources do not compile (build passes only with -Dmaven.test.skip=true): "
                           f"{where} {e.message}")

    def _maybe_skip_tests(self, pr: PhaseResult, phase: Phase, goal: str, target: Optional[str],
                          last: BuildResult) -> None:
        """Round 8: main code compiles and packages, only runtime test failures (TEST_FAILURE) remain — usually an
        externalized setting or infrastructure not reachable at build time. Rebuild with -DskipTests; on success the
        phase passes with flag `tests-skipped` and every failing test as a follow-up (and unresolved at finalize)."""
        if not getattr(self.cfg.build, "allow_skip_tests_fallback", False):
            return
        if not any(g in goal.split() for g in TEST_RUNNING_GOALS):
            return
        if not last.errors or any(e.kind != ErrorKind.TEST_FAILURE for e in last.errors):
            return
        self.ws.journal({"event": "tests_fallback", "phase": phase.value, "failing": len(last.errors)})
        res = self._build(goal, target, f"{phase.value}-skip-tests-run", phase, pr.iterations + 1, skip_tests=True)
        pr.iterations += 1
        pr.iteration_records.append(IterationRecord(phase=phase, iteration=pr.iterations, build_ok=res.ok,
                                                    error_count=res.error_count,
                                                    signatures=res.signature_multiset(), build_log=res.log_path))
        if not res.ok:
            pr.notes.append("tests fallback (-DskipTests) also failed")
            return
        pr.build_ok = True
        self.tests_skipped = True
        self._flag("tests-skipped")
        pr.remaining_errors = list(last.errors)
        pr.notes.append(f"tests-skipped: main code builds and packages, {len(last.errors)} test(s) fail at runtime "
                        "(-DskipTests)")
        for e in last.errors:
            who = e.symbol or e.module or "?"
            self._followup(f"test fails at runtime (packaged with -DskipTests): {who} — {e.message} — likely an "
                           "externalized setting (see Handover 'Properties to set') or infrastructure not reachable "
                           "at build time")

    def _flag(self, flag: str) -> None:
        if flag not in self.flags:
            self.flags.append(flag)

    def _followup(self, text: str) -> None:
        if text not in self.followups:
            self.followups.append(text)


def _modify(path: str):
    from sbmig.core.models import FileChange
    return FileChange(path=path, op="modify")
