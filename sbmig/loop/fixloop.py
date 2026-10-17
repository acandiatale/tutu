"""The build-fix loop (ARCHITECTURE §3).

Invariant: every applied change is followed by a real build. A loop round is
    build → score previous attempts (revert regressions) → stop on BUILD SUCCESS → cluster → decide → apply+commit
and the loop only exits *after* a build: on success, when the fix rounds are used up (the last round's changes are
still built and scored), when nothing actionable is left, or when the run's time budget is exceeded.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from sbmig.core.config import Config
from sbmig.core.interfaces import FixContext, TriageContext
from sbmig.core.models import (BuildResult, DecisionKind, ErrorCluster, ErrorKind, FixAttempt,
                               IterationRecord, KBMatch, Phase, PhaseResult, ProjectProfile, RecipeRef,
                               TriageDecision, utcnow)
from sbmig.core.workspace import Workspace

from .budget import LoopBudget
from .components import profile_summary

log = logging.getLogger("sbmig.loop")

TEST_COMPILING_GOALS = ("test-compile", "package", "verify", "install", "deploy", "test")
RESOLUTION_PREFIXES = ("parent_resolution:", "pom_invalid:", "dependency_resolution:", "plugin_resolution:")


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
                 history: Optional[list[FixAttempt]] = None):
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
        self._profile_summary: Optional[str] = None

    # ------------------------------------------------------------------ public
    def run(self, phase: Phase, goal: str, max_iterations: int, target: Optional[str] = None) -> PhaseResult:
        pr = PhaseResult(phase=phase)
        self.ws.journal({"event": "loop_start", "phase": phase.value, "goal": goal, "max_iterations": max_iterations,
                         "target": target, "dry_run": self.dry_run})
        prev_build: Optional[BuildResult] = None
        prev_multiset: Optional[dict[str, int]] = None
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
            multiset = build.signature_multiset()
            stalled = prev_multiset is not None and multiset == prev_multiset
            stall_level = stall_level + 1 if stalled else 0
            rec.stalled, rec.stall_level = stalled, stall_level
            if stalled:
                self.ws.journal({"event": "stall", "phase": phase.value, "iteration": build_no,
                                 "stall_level": stall_level, "error_count": build.error_count})
            if fix_rounds >= max_iterations:
                pr.notes.append(f"max iterations ({max_iterations}) reached with {build.error_count} error(s) left")
                break
            if self.budget.time_exceeded():
                pr.notes.append("run time budget exhausted: stopping the build-fix loop")
                self._flag("time-budget-exhausted")
                self.ws.journal({"event": "budget_exhausted", "phase": phase.value, "kind": "time"})
                break
            fix_rounds += 1
            new_pending, applied_any, decisions = self._fix_round(pr, phase, build_no, build, target, stall_level)
            rec.decisions = decisions
            rec.cluster_count = len(decisions)
            rec.attempt_ids = [a.id for a in pr.attempts if a.iteration == build_no]
            pending = new_pending
            if self.dry_run:
                pr.notes.append("dry-run: decisions computed and journaled, nothing applied")
                break
            if not applied_any:
                pr.notes.append(f"build {build_no}: no actionable fixes (all clusters skipped/escalated/failed to "
                                "apply) — rebuilding would not change anything")
                break
            prev_build, prev_multiset = build, multiset

        assert last is not None
        if not pr.build_ok and not self.dry_run:
            self._maybe_skip_test_compile(pr, phase, goal, target, last)
        if pr.build_ok and not self.test_compile_skipped_in(pr):
            pr.remaining_errors = []
        elif not pr.remaining_errors:
            pr.remaining_errors = list(last.errors)
        pr.ok = pr.build_ok
        pr.finished_at = utcnow()
        self.ws.journal({"event": "loop_end", "phase": phase.value, "ok": pr.ok, "iterations": pr.iterations,
                         "attempts": len(pr.attempts), "remaining_errors": len(pr.remaining_errors),
                         "llm_calls_used": self.budget.llm_calls})
        return pr

    @staticmethod
    def test_compile_skipped_in(pr: PhaseResult) -> bool:
        return any("test-compile-skipped" in n for n in pr.notes)

    # ------------------------------------------------------------------ build
    def _build(self, goal: str, target: Optional[str], label: str, phase: Phase, iteration: int,
               skip_test_compile: bool = False) -> BuildResult:
        res = self.build_runner.run(goal, module=target, label=label, skip_test_compile=skip_test_compile)
        self.ws.journal({"event": "build", "phase": phase.value, "iteration": iteration, "goal": goal,
                         "target": target, "ok": res.ok, "exit_code": res.exit_code, "errors": res.error_count,
                         "duration_s": res.duration_s, "log": res.log_path, "skip_test_compile": skip_test_compile})
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
                   stall_level: int) -> tuple[list[_Pending], bool, list[TriageDecision]]:
        clusters = self.triager.cluster(build.errors, self.profile)
        planned = self._plan(clusters)
        limit = max(1, self.cfg.loop.max_clusters_per_iteration)
        self.ws.journal({"event": "triage", "phase": phase.value, "iteration": iteration,
                         "clusters": [{"id": c.id, "kind": c.kind.value, "file": c.file, "module": c.module,
                                       "errors": len(c.errors), "priority": c.priority} for c in clusters],
                         "masked": [c.id for c, skip, _ in planned if skip is not None]})
        st = _Round(phase=phase, iteration=iteration, build=build, target=target, stall_level=stall_level,
                    others={c.id: _label(c) for c in clusters})
        masked: list[tuple[ErrorCluster, TriageDecision, list[str]]] = []
        handled = 0
        for c, skip, by in planned:
            if skip is not None:
                masked.append((c, skip, by))
                continue
            if handled >= limit:
                continue
            handled += 1
            self._handle_cluster(pr, st, c)
        # unmask: a masker that cannot be acted upon must not hide compile errors we could fix
        for c, skip, by in masked:
            if by and all(b in st.hopeless for b in by) and handled < limit:
                handled += 1
                self._handle_cluster(pr, st, c, prefix="(unmasked: its pom problem was escalated) ")
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
        return st.pending, st.applied_any, st.decisions

    def _journal_decision(self, st: "_Round", d: TriageDecision) -> None:
        self.ws.journal({"event": "decision", "phase": st.phase.value, "iteration": st.iteration,
                         **d.model_dump(mode="json")})

    def _handle_cluster(self, pr: PhaseResult, st: "_Round", c: ErrorCluster, prefix: str = "",
                        allow_recipes: bool = True) -> None:
        """Decide → apply → commit; a deterministic option that changes nothing falls through to the next one."""
        m = self._kb_match(c)
        for _ in range(4):
            d = self._decide(st.phase, st.iteration, st.stall_level, c, m)
            if prefix:
                d.rationale = prefix + d.rationale
            if d.kind == DecisionKind.LLM_FIX and not self.budget.llm_allowed():
                d = TriageDecision(cluster_id=c.id, kind=DecisionKind.ESCALATE,
                                   rationale=f"escalated: LLM budget exhausted ({self.budget.llm_calls}/"
                                             f"{self.budget.max_llm_calls} calls)")
            if d.kind == DecisionKind.KB_RECIPE and not allow_recipes:
                d = TriageDecision(cluster_id=c.id, kind=DecisionKind.ESCALATE,
                                   rationale=prefix + "escalated: only recipes left and they changed nothing")
            st.decisions.append(d)
            self._journal_decision(st, d)
            if d.kind == DecisionKind.ESCALATE:
                self.escalated[c.id] = d
                st.hopeless.add(c.id)
                return
            if d.kind == DecisionKind.SKIP or self.dry_run:
                return
            if d.kind == DecisionKind.KB_RECIPE:
                st.recipe_batch.append((c, d, m))
                return
            st.n += 1
            att = self._new_attempt(st.phase, st.iteration, st.n, c, d, st.build)
            pre = self.ws.head()
            if d.kind == DecisionKind.HANDLER:
                self._apply_handler(att, c)
                msg = f"fix(handler:{d.handler_id}): {c.id}"
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
            self._record(pr, att)
            if att.applied or d.kind == DecisionKind.LLM_FIX:
                return          # never call the LLM twice for one cluster in one round

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
                         "apply_errors": att.apply_errors, "analysis": att.analysis[:500]})

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
        ctx = FixContext(phase=phase, iteration=iteration, stall_level=stall_level,
                         profile_summary=self._summary(), plan_notes=self.plan_notes, kb_hints=hints,
                         previous_attempts=_related(c, self.history), other_errors_summary=other,
                         use_escalation_model=stall_level >= self.cfg.loop.stall_limit)
        try:
            proposal = self.fixer.propose(self.ws, c, ctx)
        except Exception as e:
            self.budget.charge_llm(1)
            log.warning("fixer.propose failed for %s: %s", c.id, e)
            return self._fail(att, f"LLM fixer failed: {type(e).__name__}: {e}")
        self.budget.charge_llm(max(1, proposal.llm_usage.calls))
        att.model, att.prompt_hash, att.llm_usage = proposal.model, proposal.prompt_hash, proposal.llm_usage
        att.analysis = proposal.analysis
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

    def _flag(self, flag: str) -> None:
        if flag not in self.flags:
            self.flags.append(flag)

    def _followup(self, text: str) -> None:
        if text not in self.followups:
            self.followups.append(text)


def _modify(path: str):
    from sbmig.core.models import FileChange
    return FileChange(path=path, op="modify")
