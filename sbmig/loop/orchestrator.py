"""Orchestrator: intake → analyze → plan → phases (rewrite / handlers / build-fix loop / restructure) → verify → report.

The orchestrator owns the loop. A phase is green only on BUILD SUCCESS, and the final `mvn clean package` runs
whenever `loop.always_final_verify` is set — even after failures or budget exhaustion.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional

from sbmig.core.config import Config
from sbmig.core.models import (BuildError, BuildResult, CaseType, ErrorKind, HarvestResult, LLMUsage,
                               MigrationPlan, Phase, PhaseResult, PlanStep, ProjectProfile, RecipeRef, RunReport,
                               StepKind, utcnow)
from sbmig.core.workspace import Workspace

from .budget import LoopBudget
from .components import LazyFixer, MissingComponentError, NullKnowledgeBase, lazy_attr
from .fixloop import BuildFixLoop

log = logging.getLogger("sbmig.loop")

_MISSING_RECIPE_RE = re.compile(r"(?i)(recipe(?:s|\(s\))? (?:were )?not found|could not find recipe|no recipe(?:s)? (?:found|named)|"
                                r"unknown recipe|recipe\s+\S+\s+(?:does not exist|not found))")
_PHASE_ORDER = [Phase.INTAKE, Phase.ANALYZE, Phase.PLAN, Phase.JDK21, Phase.SPRING, Phase.RESTRUCTURE,
                Phase.VERIFY, Phase.REPORT]


class Orchestrator:
    def __init__(self, cfg: Config, ws: Workspace, *, analyzer: Optional[Callable] = None,
                 planner: Optional[Callable] = None, pre_handlers: Optional[Callable] = None,
                 restructurer: Any = None, rewrite_runner: Any = None, build_runner: Any = None,
                 triager: Any = None, handlers: Any = None, fixer: Any = None, kb: Any = None, llm: Any = None,
                 report_writer: Optional[Callable] = None, clock: Callable[[], float] = time.monotonic):
        self.cfg = cfg
        self.ws = ws
        self.clock = clock
        self._analyzer = analyzer
        self._planner = planner
        self._pre_handlers = pre_handlers
        self._restructurer = restructurer
        self._rewrite_runner = rewrite_runner
        self._build_runner = build_runner
        self._triager = triager
        self._handlers = handlers
        self._fixer = fixer
        self._kb = kb
        self._llm = llm
        self._report_writer = report_writer
        self.budget = LoopBudget.from_config(cfg, clock=clock)
        self.report: Optional[RunReport] = None
        self.profile: Optional[ProjectProfile] = None
        self.plan: Optional[MigrationPlan] = None
        self.loop: Optional[BuildFixLoop] = None
        self._handlers_done: set[str] = set()
        self._jdk_pre_done = False
        self._spring_pre_done = False
        self._verify_results: list[BuildResult] = []
        self._phase_failed = False
        self._dry_run = False
        self._pre_ids: list[str] = []
        self._pending_notes: list[str] = []
        self._last_spring_rewrite = -1

    # ------------------------------------------------------------------ lazy collaborators
    @property
    def analyzer(self) -> Callable:
        if self._analyzer is None:
            self._analyzer = lazy_attr("sbmig.analyze", "analyze_project", "A (analyze)")
        return self._analyzer

    @property
    def planner(self) -> Callable:
        if self._planner is None:
            self._planner = lazy_attr("sbmig.plan", "build_plan", "A (plan)")
        return self._planner

    @property
    def pre_handlers(self) -> Callable:
        if self._pre_handlers is None:
            try:
                self._pre_handlers = lazy_attr("sbmig.plan", "pre_handlers_for", "A (plan)")
            except MissingComponentError as e:
                log.warning("%s — no pre-emptive handlers", e)
                self._pre_handlers = lambda profile, cfg: []
        return self._pre_handlers

    @property
    def restructurer(self) -> Any:
        if self._restructurer is None:
            self._restructurer = lazy_attr("sbmig.restructure", "Restructurer", "B (restructure)")()
        return self._restructurer

    @property
    def rewrite_runner(self) -> Any:
        if self._rewrite_runner is None:
            cls = lazy_attr("sbmig.rewrite", "OpenRewriteRunner", "C (rewrite)")
            self._rewrite_runner = cls(self.ws, self.cfg, self.kb)
        return self._rewrite_runner

    @property
    def build_runner(self) -> Any:
        if self._build_runner is None:
            self._build_runner = lazy_attr("sbmig.build", "MavenRunner", "C (build)")(self.ws, self.cfg)
        return self._build_runner

    @property
    def handlers(self) -> Any:
        if self._handlers is None:
            reg = getattr(self._triager, "registry", None) if self._triager is not None else None
            if reg is None:
                from sbmig.triage import HandlerRegistry
                reg = HandlerRegistry.default(self.cfg)
            self._handlers = reg
        return self._handlers

    @property
    def triager(self) -> Any:
        if self._triager is None:
            from sbmig.triage import Triager
            self._triager = Triager(self.cfg, registry=self.handlers)
        return self._triager

    @property
    def kb(self) -> Any:
        if self._kb is None:
            try:
                self._kb = lazy_attr("sbmig.kb", "FileKnowledgeBase", "F (kb)")(self.cfg)
            except MissingComponentError as e:
                log.warning("%s — running without a knowledge base", e)
                self.ws.journal({"event": "warning", "message": str(e)})
                self._kb = NullKnowledgeBase()
        return self._kb

    def llm_client(self) -> Any:
        if self._llm is None:
            self._llm = lazy_attr("sbmig.fix", "make_llm_client", "D (fix)")(self.cfg)
        return self._llm

    @property
    def fixer(self) -> Any:
        if self._fixer is None:
            def make() -> Any:
                cls = lazy_attr("sbmig.fix", "LLMFixer", "D (fix)")
                return cls(self.cfg, self.llm_client())
            self._fixer = LazyFixer(make)
        return self._fixer

    # ------------------------------------------------------------------ run
    def run(self, stop_after: Optional[Phase] = None, dry_run: bool = False) -> RunReport:
        self._dry_run = dry_run
        t0 = self.clock()
        report = RunReport(run_id=self.ws.run_id, project_name=self.ws.root.name, case_type=CaseType.UNKNOWN,
                           started_at=utcnow())
        self.report = report
        if dry_run:
            report.flags.append("dry-run")
        try:
            self._run(report, stop_after, dry_run)
        except MissingComponentError as e:
            log.error("%s", e)
            self._abort(report, f"missing component: {e}")
        except Exception as e:  # never lose the report because of a bug
            log.exception("run failed")
            self._abort(report, f"internal error: {type(e).__name__}: {e}")
        finally:
            report.timings_s["total"] = round(self.clock() - t0, 3)
            self._finalize(report)
        return report

    def _abort(self, report: RunReport, why: str) -> None:
        report.flags.append("aborted")
        report.manual_followups.append(f"run aborted: {why}")
        self.ws.journal({"event": "abort", "reason": why})

    def _stop(self, stop_after: Optional[Phase], phase: Phase) -> bool:
        if stop_after is not None and stop_after == phase:
            assert self.report is not None
            self.report.flags.append(f"stopped-after-{phase.value}")
            self.ws.journal({"event": "stop_after", "phase": phase.value})
            return True
        return False

    def _timed(self, key: str, t0: float) -> None:
        assert self.report is not None
        self.report.timings_s[key] = round(self.report.timings_s.get(key, 0.0) + self.clock() - t0, 3)

    def _run(self, report: RunReport, stop_after: Optional[Phase], dry_run: bool) -> None:
        # 1. intake ------------------------------------------------------------
        t = self.clock()
        self.ws.git_init()
        self._preflight(report)
        self._timed("intake", t)
        if self._stop(stop_after, Phase.INTAKE):
            return
        # 2. analyze -----------------------------------------------------------
        t = self.clock()
        profile = self.analyzer(self.ws, self.cfg)
        self.profile = profile
        report.profile = profile
        report.case_type = profile.case_type
        report.project_name = profile.name or report.project_name
        self.ws.save_model("profile.json", profile)
        self.ws.journal({"event": "analyze", "case_type": profile.case_type.value, "poms": len(profile.poms),
                         "build_tool": profile.build_tool, "java": profile.java_version,
                         "spring_boot": profile.spring_boot_version})
        self._timed("analyze", t)
        if profile.build_tool != "maven":
            self._abort(report, f"build tool '{profile.build_tool}' is not supported (Maven only)")
            return
        if not profile.poms:
            self._abort(report, "no pom.xml found in the project")
            return
        if self._stop(stop_after, Phase.ANALYZE):
            return
        # 3. plan --------------------------------------------------------------
        t = self.clock()
        plan = self.planner(profile, self.cfg, self.kb)
        self.plan = plan
        report.plan = plan
        self.ws.save_model("plan.json", plan)
        pre_ids = list(self.pre_handlers(profile, self.cfg) or [])
        self._pre_ids = pre_ids
        self.ws.journal({"event": "plan", "steps": [s.id for s in plan.steps], "pre_handlers": pre_ids,
                         "notes": plan.notes})
        for n in plan.notes:
            if n.startswith("followup:"):
                report.manual_followups.append(n[len("followup:"):].strip())
        self._timed("plan", t)
        if self._stop(stop_after, Phase.PLAN):
            return
        # 4. phases ------------------------------------------------------------
        self.loop = BuildFixLoop(self.cfg, self.ws, self.build_runner, self.triager, self.handlers, self.fixer,
                                 self.kb, self._rewrite_runner_or_none(), profile, plan_notes=plan.notes,
                                 budget=self.budget, dry_run=dry_run)
        steps = list(plan.steps)
        if self.cfg.loop.always_final_verify and not any(s.kind == StepKind.BUILD for s in steps):
            steps.append(PlanStep(id="verify.build", kind=StepKind.BUILD, phase=Phase.VERIFY,
                                  title="final mvn clean package", build_goal="clean package",
                                  reason="always_final_verify"))
        self._last_spring_rewrite = max((i for i, s in enumerate(steps)
                                         if s.phase == Phase.SPRING and s.kind == StepKind.OPENREWRITE), default=-1)
        groups: list[tuple[Phase, list[tuple[int, PlanStep]]]] = []
        for i, s in enumerate(steps):
            if groups and groups[-1][0] == s.phase:
                groups[-1][1].append((i, s))
            else:
                groups.append((s.phase, [(i, s)]))
        stop_idx = _PHASE_ORDER.index(stop_after) if stop_after is not None else None
        for phase, items in groups:
            if stop_idx is not None and _PHASE_ORDER.index(phase) > stop_idx:
                # MSA plans repeat jdk21/spring per service: skip only the phases after `stop_after`
                self.ws.journal({"event": "phase_skipped", "phase": phase.value, "reason": f"stop_after={stop_after.value}"})
                continue
            t = self.clock()
            pr = PhaseResult(phase=phase, step_ids=[s.id for _, s in items], ok=True)
            self.ws.journal({"event": "phase_start", "phase": phase.value, "steps": pr.step_ids})
            self._run_phase(pr, items)
            pr.finished_at = utcnow()
            report.phases.append(pr)
            self.ws.journal({"event": "phase_end", "phase": phase.value, "ok": pr.ok, "build_ok": pr.build_ok,
                             "iterations": pr.iterations, "attempts": len(pr.attempts),
                             "remaining_errors": len(pr.remaining_errors)})
            if not pr.ok and phase != Phase.VERIFY:
                self._phase_failed = True
            if phase != Phase.VERIFY and not pr.skipped:
                self._harvest(report, pr)
            self._timed(phase.value, t)
        if stop_after is not None and stop_after != Phase.VERIFY:
            self._stop(stop_after, stop_after)

    def _rewrite_runner_or_none(self) -> Any:
        try:
            return self.rewrite_runner
        except MissingComponentError as e:
            log.warning("%s", e)
            return None

    # ------------------------------------------------------------------ preflight
    def _preflight(self, report: RunReport) -> None:
        checks: dict[str, Any] = {}
        if self._build_runner is None:
            try:
                avail = lazy_attr("sbmig.build", "maven_available", "C (build)")
                try:
                    ok = avail(self.cfg)
                except TypeError:
                    ok = avail()
                checks["mvn"] = bool(ok)
                if not ok:
                    report.flags.append("mvn-not-found")
                    report.manual_followups.append(f"'{self.cfg.build.mvn}' is not runnable: install Maven or set "
                                                   "build.mvn; every build below will fail")
            except MissingComponentError as e:
                checks["mvn"] = f"unknown ({e})"
        parent = self.cfg.artifacts.parent
        if not parent.group_id or not parent.artifact_id or "CHANGE" in parent.group_id.upper():
            checks["artifacts.parent"] = "not configured"
            report.manual_followups.append("artifacts.parent is not configured: pass the company artifacts yml "
                                           "with --config")
        else:
            checks["artifacts.parent"] = f"{parent.key}:{parent.version or '-'}"
        checks["java_home"] = self.cfg.build.java_home or "(inherited)"
        self.ws.journal({"event": "preflight", **checks})

    # ------------------------------------------------------------------ phases
    def _run_phase(self, pr: PhaseResult, items: list[tuple[int, PlanStep]]) -> None:
        aborted = False
        loop_ran = False
        for idx, step in items:
            if aborted:
                pr.notes.append(f"step {step.id} skipped: phase aborted")
                continue
            self.ws.journal({"event": "step_start", "step": step.id, "kind": step.kind.value,
                             "phase": step.phase.value, "target": step.target})
            ok = True
            if step.kind == StepKind.OPENREWRITE:
                self._maybe_pre_handlers(step, idx)
                ok = self._step_rewrite(pr, step)
                if ok is None:          # missing recipe → programming error
                    aborted, ok = True, False
                    pr.ok = False
            elif step.kind == StepKind.HANDLER:
                self._step_handlers(pr, step)
            elif step.kind == StepKind.BUILD_FIX_LOOP:
                self._maybe_pre_handlers(step, idx)
                loop_ran = True
                ok = self._step_loop(pr, step)
                pr.ok = ok
            elif step.kind in (StepKind.RESTRUCTURE, StepKind.MSA_POM):
                ok = self._step_restructure(pr, step)
                if not ok:
                    aborted = True
                    pr.ok = False
            elif step.kind == StepKind.BUILD:
                ok = self._step_verify(pr, step)
            self.ws.journal({"event": "step_end", "step": step.id, "ok": bool(ok)})
        if pr.phase == Phase.VERIFY:
            pr.ok = pr.build_ok = bool(self._verify_results) and all(r.ok for r in self._verify_results)
        elif not loop_ran and pr.ok:
            pr.build_ok = False

    def _maybe_pre_handlers(self, step: PlanStep, idx: int) -> None:
        ids = getattr(self, "_pre_ids", [])
        if not ids:
            return
        jdk_ids, spring_ids = [], []
        for hid in ids:
            h = self.handlers.get(hid) if self.handlers is not None else None
            if h is None:
                continue
            (spring_ids if getattr(h, "preemptive_phase", Phase.JDK21) == Phase.SPRING else jdk_ids).append(hid)
        if not self._jdk_pre_done and step.phase in (Phase.JDK21, Phase.SPRING, Phase.RESTRUCTURE):
            # JDK21-safe handlers: before the first rewrite/build of the run (normally jdk21.rewrite)
            self._jdk_pre_done = True
            if jdk_ids:
                self._run_handlers(jdk_ids, "pre-emptive (before the first build)")
        if not self._spring_pre_done and step.phase == Phase.SPRING and step.kind == StepKind.BUILD_FIX_LOOP \
                and idx > self._last_spring_rewrite:
            # jakarta-dependent handlers wait until the spring recipes migrated the code
            self._spring_pre_done = True
            if spring_ids:
                self._run_handlers(spring_ids, "pre-emptive (after the jakarta/spring recipes)")
        if not self._spring_pre_done and step.phase == Phase.RESTRUCTURE:
            self._spring_pre_done = True
            if spring_ids:
                self._run_handlers(spring_ids, "pre-emptive (before restructure)")

    def _run_handlers(self, ids: list[str], why: str, pr: Optional[PhaseResult] = None) -> list[str]:
        todo = [h for h in ids if h not in self._handlers_done]
        if not todo:
            return []
        assert self.profile is not None
        done: list[str] = []
        files: list[str] = []
        for hid in todo:
            self._handlers_done.add(hid)
            h = self.handlers.get(hid)
            if h is None:
                log.warning("pre-emptive handler %s is not registered", hid)
                continue
            if self._dry_run:
                done.append(hid)
                continue
            try:
                changes = h.apply(self.ws, None, self.profile, self.cfg) or []
            except Exception as e:
                log.warning("pre-emptive handler %s failed: %s", hid, e)
                self.ws.journal({"event": "handler_failed", "handler": hid, "error": str(e)})
                if self.ws.is_dirty():
                    self.ws.discard_uncommitted()
                continue
            if changes:
                done.append(hid)
                files += [c.path for c in changes]
        sha = None
        if done and not self._dry_run and self.ws.is_dirty():
            sha = self.ws.commit(f"handlers: {', '.join(done)}")
        self.ws.journal({"event": "handlers_preemptive", "why": why, "requested": todo, "changed": done,
                         "files": sorted(set(files)), "commit": sha, "dry_run": self._dry_run})
        if self.report is not None and done:
            target_pr = pr
            note = f"handlers {why}: {', '.join(done)}" + (" (dry-run, not applied)" if self._dry_run else "")
            if target_pr is not None:
                target_pr.notes.append(note)
            else:
                self._pending_notes.append(note)
        return done

    def _flush_notes(self, pr: PhaseResult) -> None:
        if self._pending_notes:
            pr.notes.extend(self._pending_notes)
            self._pending_notes = []

    def _step_handlers(self, pr: PhaseResult, step: PlanStep) -> None:
        ids = step.params.get("handlers") or step.params.get("handler_ids") or [step.id.split(".")[-1]]
        self._run_handlers(list(ids), f"plan step {step.id}", pr)

    def _extra_recipe_files(self, step: PlanStep) -> list[Path]:
        files: list[Path] = [Path(r.file) for r in step.recipes if r.file]
        if self.cfg.kb.use_active_recipes:
            try:
                for f in self.kb.active_recipe_files():
                    if Path(f) not in files:
                        files.append(Path(f))
            except Exception as e:  # pragma: no cover - KB problems never break a run
                log.warning("kb.active_recipe_files failed: %s", e)
        return files

    def _step_rewrite(self, pr: PhaseResult, step: PlanStep) -> Optional[bool]:
        self._flush_notes(pr)
        if self.budget.time_exceeded():
            pr.notes.append(f"{step.id} skipped: run time budget exhausted")
            self._flag("time-budget-exhausted")
            return True
        if not step.recipes:
            return True
        runner = self.rewrite_runner
        pre = self.ws.head()
        res = runner.run(list(step.recipes), extra_recipe_files=self._extra_recipe_files(step), module=step.target,
                         dry_run=self._dry_run, label=step.id)
        pr.rewrite_results.append(res)
        mark = getattr(self.triager, "mark_recipes_run", None)
        if callable(mark):
            mark([r.id for r in step.recipes], step.phase)
        sha = None
        if not self._dry_run and self.ws.is_dirty():
            sha = self.ws.commit(f"rewrite: {step.id}")
        self.ws.journal({"event": "rewrite", "step": step.id, "ok": res.ok, "recipes": res.recipes,
                         "changed_files": len(res.changed_files), "commit": sha if sha != pre else None,
                         "error": res.error, "log": res.log_path, "dry_run": self._dry_run})
        if not res.ok:
            err = res.error or "OpenRewrite run failed"
            if _MISSING_RECIPE_RE.search(err):
                pr.notes.append(f"{step.id}: recipe id not found — {err} (programming/config error, phase aborted)")
                return None
            pr.notes.append(f"{step.id}: OpenRewrite failed ({err}); continuing — the build will show the real "
                            "problem")
            pr.remaining_errors.append(BuildError(kind=ErrorKind.REWRITE, message=err[:500], raw=err,
                                                  module=step.target))
        return True

    def _step_loop(self, pr: PhaseResult, step: PlanStep) -> bool:
        self._flush_notes(pr)
        assert self.loop is not None
        if self.budget.time_exceeded():
            pr.notes.append(f"{step.id} skipped: run time budget exhausted ({self.cfg.loop.max_total_minutes} min)")
            self._flag("time-budget-exhausted")
            return False
        pc = self.cfg.loop.phases.get(step.phase.value)
        goal = step.build_goal or (pc.goal if pc else "compile")
        max_it = step.max_iterations or (pc.max_iterations if pc else 5)
        if self.loop.rewrite_runner is None:
            self.loop.rewrite_runner = self._rewrite_runner_or_none()
        res = self.loop.run(step.phase, goal, max_it, target=step.target)
        pr.iterations += res.iterations
        pr.attempts += res.attempts
        pr.iteration_records += res.iteration_records
        pr.rewrite_results += res.rewrite_results
        pr.remaining_errors = [e for e in pr.remaining_errors if e.kind == ErrorKind.REWRITE and not res.build_ok] \
            + res.remaining_errors
        pr.build_ok = res.build_ok
        pr.notes += [f"{step.id}: {n}" for n in res.notes]
        return res.ok

    def _step_restructure(self, pr: PhaseResult, step: PlanStep) -> bool:
        self._flush_notes(pr)
        assert self.profile is not None and self.report is not None
        mode = "msa_pom" if step.kind == StepKind.MSA_POM else step.params.get("mode", "layout")
        if self._dry_run:
            pr.notes.append(f"{step.id}: dry-run → restructure ({mode}) not applied")
            return True
        pre = self.ws.head()
        try:
            res = self.restructurer.apply(self.ws, self.profile, self.cfg, mode=mode)
        except MissingComponentError:
            raise
        except Exception as e:
            log.exception("restructure failed")
            pr.notes.append(f"{step.id}: restructure crashed: {type(e).__name__}: {e}")
            if self.ws.is_dirty():
                self.ws.discard_uncommitted()
            return False
        sha = self.ws.commit(f"restructure: {step.id}") if self.ws.is_dirty() else self.ws.head()
        self.report.manual_followups += [f for f in res.manual_followups if f not in self.report.manual_followups]
        pr.notes += res.notes
        summary = (f"{step.id} ({mode}): {len(res.changed_files)} file(s) changed, {res.moved_files} moved, "
                   f"{len(res.generated_files)} generated; mandatory added: {', '.join(res.mandatory_added) or '-'}; "
                   f"optional bumped: {', '.join(res.optional_bumped) or '-'}")
        pr.notes.append(summary)
        self.ws.journal({"event": "restructure", "step": step.id, "mode": mode, "ok": res.ok,
                         "commit": sha if sha != pre else None, "changed_files": len(res.changed_files),
                         "moved": res.moved_files, "generated": res.generated_files,
                         "mandatory_added": res.mandatory_added, "optional_bumped": res.optional_bumped,
                         "error": res.error})
        if not res.ok:
            pr.notes.append(f"{step.id}: restructure failed: {res.error or 'unknown error'} → skipping the rest of "
                            "the restructure phase (verify still runs)")
        return res.ok

    def _step_verify(self, pr: PhaseResult, step: PlanStep) -> bool:
        self._flush_notes(pr)
        if self._dry_run:
            pr.notes.append(f"{step.id}: dry-run → final build skipped")
            return True
        if not self.cfg.loop.always_final_verify and self._phase_failed:
            pr.notes.append(f"{step.id}: skipped (an earlier phase failed and loop.always_final_verify=false)")
            return False
        skip_tests = bool(self.loop and self.loop.test_compile_skipped)
        label = "verify" + (f"-{step.params['service']}" if step.params.get("service") else "")
        res = self.build_runner.run(step.build_goal or "clean package", module=step.target, label=label,
                                    skip_test_compile=skip_tests)
        self._verify_results.append(res)
        assert self.report is not None
        self.report.final_build = res
        pr.iterations += 1
        pr.remaining_errors += res.errors
        from sbmig.core.models import IterationRecord
        pr.iteration_records.append(IterationRecord(phase=Phase.VERIFY, iteration=pr.iterations, build_ok=res.ok,
                                                    error_count=res.error_count, signatures=res.signature_multiset(),
                                                    build_log=res.log_path))
        if skip_tests:
            pr.notes.append(f"{step.id}: built with -Dmaven.test.skip=true (test-compile-skipped earlier)")
        self.ws.journal({"event": "verify", "step": step.id, "target": step.target, "ok": res.ok,
                         "errors": res.error_count, "log": res.log_path, "skip_test_compile": skip_tests})
        return res.ok

    # ------------------------------------------------------------------ kb
    def _harvest(self, report: RunReport, pr: PhaseResult) -> None:
        if not self.cfg.kb.harvest or self._dry_run or self.profile is None:
            return
        llm = None
        if self.cfg.kb.generate_recipe_candidates:
            try:
                llm = self.llm_client()
            except Exception as e:
                log.info("no LLM for recipe-candidate generation: %s", e)
        try:
            res = self.kb.harvest(pr, self.profile, self.ws, llm)
        except Exception as e:
            log.warning("kb.harvest failed: %s", e)
            pr.notes.append(f"kb harvest failed: {type(e).__name__}: {e}")
            self.ws.journal({"event": "harvest", "phase": pr.phase.value, "error": str(e)})
            return
        if res is None:
            return
        h = report.harvest or HarvestResult()
        h.cases_created += res.cases_created
        h.candidates_created += res.candidates_created
        h.recipes_credited += res.recipes_credited
        h.notes += res.notes
        report.harvest = h
        self.ws.journal({"event": "harvest", "phase": pr.phase.value, "cases": res.cases_created,
                         "candidates": res.candidates_created})

    def _flag(self, flag: str) -> None:
        if self.report is not None and flag not in self.report.flags:
            self.report.flags.append(flag)

    # ------------------------------------------------------------------ finalize
    def _finalize(self, report: RunReport) -> None:
        report.finished_at = utcnow()
        usage = LLMUsage()
        for pr in report.phases:
            for a in pr.attempts:
                usage = usage.add(a.llm_usage)
        usage.calls = max(usage.calls, self.budget.llm_calls)
        report.llm_usage = usage
        if self.loop is not None:
            for f in self.loop.flags:
                if f not in report.flags:
                    report.flags.append(f)
            report.manual_followups += [f for f in self.loop.followups if f not in report.manual_followups]
            if self.loop.credited:
                h = report.harvest or HarvestResult()
                h.recipes_credited += [c for c in self.loop.credited if c not in h.recipes_credited]
                report.harvest = h
        if self.budget.llm_calls >= self.budget.max_llm_calls and self.budget.max_llm_calls > 0:
            self._flag("llm-budget-exhausted")
        report.success = bool(self._verify_results) and all(r.ok for r in self._verify_results)
        # unresolved: what the as-is owner must look at
        unresolved: list[BuildError] = []
        for r in self._verify_results:
            if not r.ok:
                unresolved += r.errors
        if not self._verify_results and report.phases:
            unresolved += report.phases[-1].remaining_errors
        if "test-compile-skipped" in report.flags:
            for pr in report.phases:
                if BuildFixLoop.test_compile_skipped_in(pr):
                    unresolved += [e for e in pr.remaining_errors if e not in unresolved]
        report.unresolved = unresolved
        out = report.outputs
        out["run_dir"] = str(self.ws.run_dir)
        out["project_dir"] = str(self.ws.root)
        out["journal"] = str(self.ws.journal_path)
        out["logs"] = str(self.ws.logs_dir)
        for name in ("profile.json", "plan.json"):
            if (self.ws.run_dir / name).exists():
                out[name.replace(".json", "")] = str(self.ws.run_dir / name)
        try:
            if self.cfg.report.include_diff and (self.ws.root / ".git").exists():
                out["diff"] = str(self.ws.write_patch())
        except Exception as e:
            log.warning("could not write changes.patch: %s", e)
        try:
            if self.cfg.report.zip_result and not self._dry_run:
                out["zip"] = str(self.ws.zip_result())
        except Exception as e:
            log.warning("could not write result.zip: %s", e)
        if "md" in self.cfg.report.formats:
            out["report_md"] = str(self.ws.run_dir / "report.md")
        if "json" in self.cfg.report.formats:
            out["report_json"] = str(self.ws.run_dir / "report.json")
        writer = self._report_writer
        if writer is None:
            from sbmig.report import write_report
            writer = write_report
        try:
            paths = writer(report, self.ws) or {}
            out.update({k: v for k, v in paths.items()})
        except Exception as e:
            log.exception("report writing failed")
            report.flags.append(f"report-failed: {e}")
        self.ws.journal({"event": "report", "success": report.success, "flags": report.flags,
                         "unresolved": len(report.unresolved), "llm_calls": report.llm_usage.calls,
                         "outputs": out})
