"""Orchestrator: intake → analyze → plan → phases (rewrite / handlers / build-fix loop / restructure) → verify → report.

The orchestrator owns the loop. A phase is green only on BUILD SUCCESS, and the final `mvn clean package` runs
whenever `loop.always_final_verify` is set — even after failures, blocked clusters or budget exhaustion.

Blocked clusters (docs/ERROR-HANDLING.md) live in one run-wide `BlockedRegistry` shared by every phase: a cluster
blocked in the spring phase is skipped by restructure and listed in the report's "Blocked" section; the run only
stops early (``aborted:*`` flags, exit code 3) when nothing can build at all (no Maven, not a Maven project, the
configured in-house parent is not resolvable).
"""
from __future__ import annotations

import logging
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from sbmig.core.config import Config
from sbmig.core.models import (GAV, BlockedItem, BuildError, BuildResult, CaseType, DecisionKind, ErrorKind,
                               HarvestResult, LLMUsage, MigrationPlan, Phase, PhaseResult, PlanStep, ProjectProfile,
                               RunReport, StepKind, utcnow)
from sbmig.core.workspace import Workspace

from .blocked_registry import BlockedRegistry, default_locator
from .budget import LoopBudget
from .components import LazyFixer, MissingComponentError, NullKnowledgeBase, lazy_attr
from .fixloop import BuildFixLoop

log = logging.getLogger("sbmig.loop")

_MISSING_RECIPE_RE = re.compile(r"(?i)(recipe(?:s|\(s\))? (?:were )?not found|could not find recipe|no recipe(?:s)? (?:found|named)|"
                                r"unknown recipe|recipe\s+\S+\s+(?:does not exist|not found))")
_PARENT_UNRESOLVABLE_RE = re.compile(r"(?i)non-resolvable parent|parent pom|failed to read artifact descriptor")
_DEFAULT_MODE = {StepKind.MSA_POM: "msa_pom", StepKind.RESTRUCTURE: "layout"}
ALWAYS_PREEMPTIVE = ("strip-utf8-bom", "source-encoding")   # inserted at the front → source-encoding runs first
NON_FATAL_RESTRUCTURE_MODES = frozenset({"optional_bump"})   # a failure is noted, the phase continues


def _first_sentence(text: str, limit: int = 160) -> str:
    """First sentence of an LLM analysis, whitespace-collapsed, ≤ limit chars (Handover note)."""
    t = " ".join((text or "").split())
    m = re.match(r"(.+?[.!?])(\s|$)", t)
    t = m.group(1) if m else t
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


class _RunAborted(Exception):
    """Internal: stop the run (no further phases, no verify) — the report is still written."""


_PHASE_ORDER = [Phase.INTAKE, Phase.ANALYZE, Phase.PLAN, Phase.JDK21, Phase.SPRING, Phase.RESTRUCTURE,
                Phase.VERIFY, Phase.REPORT]


class Orchestrator:
    def __init__(self, cfg: Config, ws: Workspace, *, analyzer: Optional[Callable] = None,
                 planner: Optional[Callable] = None, pre_handlers: Optional[Callable] = None,
                 restructurer: Any = None, rewrite_runner: Any = None, build_runner: Any = None,
                 triager: Any = None, handlers: Any = None, fixer: Any = None, kb: Any = None, llm: Any = None,
                 report_writer: Optional[Callable] = None, clock: Callable[[], float] = time.monotonic,
                 locator: Any = None, effective_capture: Optional[Callable] = None):
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
        self.blocked = BlockedRegistry()            # run-wide: shared by every BuildFixLoop of this run
        # a custom build runner means we are not driving the real Maven → no classpath attribution / effective-pom
        # capture by default (they would spawn mvn); pass `locator` / `effective_capture` explicitly to enable them
        self._real_maven = build_runner is None
        self._locator = locator
        self._effective_capture = effective_capture
        self._running = False
        self.abort_reason: str | None = None   # first `_abort` reason of the current run

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
                spec = getattr(getattr(self.cfg, "fix", None), "plugin", None)
                if spec:                                   # tier 2: in-house coding agent (docs/ADAPTERS.md)
                    try:
                        llm = self.llm_client()
                    except Exception as e:                 # the agent may not need our LLM at all
                        log.info("fix.plugin %s: no sbmig LLM client (%s)", spec, e)
                        llm = None
                    resolve = lazy_attr("sbmig.llm", "resolve_fixer_plugin", "A (llm adapters)")
                    try:
                        fx = resolve(self.cfg, llm)
                    except Exception as e:
                        self.ws.journal({"event": "fixer", "plugin": spec, "error": f"{type(e).__name__}: {e}"})
                        raise
                    self.ws.journal({"event": "fixer", "plugin": spec})
                    return fx
                cls = lazy_attr("sbmig.fix", "LLMFixer", "D (fix)")
                return cls(self.cfg, self.llm_client())
            self._fixer = LazyFixer(make)
        return self._fixer

    # ------------------------------------------------------------------ run
    def run(self, stop_after: Phase | None = None, dry_run: bool = False) -> RunReport:
        """One migration run. `loop.engine` picks the driver of the steps: ``graph`` (default, LangGraph
        `StateGraph`, `sbmig.graph`) or ``inline`` (`_run`). Both call the same `step_*` methods inside the same
        guard (`_run_guard`: abort / missing-component / internal-error handling + `_finalize`), so reports,
        journals and exit codes are identical apart from the graph's additive ``graph_node`` journal events."""
        with self._run_guard(dry_run) as report:
            if self.engine == "graph":
                from sbmig.graph import run_graph
                run_graph(self, stop_after=stop_after, dry_run=dry_run)
            else:
                self._run(report, stop_after, dry_run)
        return report

    @property
    def engine(self) -> str:
        return getattr(getattr(self.cfg, "loop", None), "engine", "inline") or "inline"

    @property
    def running(self) -> bool:
        """True while a `_run_guard` is open (a run is in progress and `self.report` is its report)."""
        return self._running

    @contextmanager
    def _run_guard(self, dry_run: bool = False) -> Iterator[RunReport]:
        """Start a run (fresh `RunReport` on `self.report`) and always finish it: `_RunAborted` ends the run
        quietly, a `MissingComponentError` or any other exception is recorded as an abort (never lose the report
        because of a bug), and `_finalize` (report, ledger, outputs) runs no matter what. Used by `run()` for both
        engines and by `sbmig.graph.run_graph` / `stream_graph` when they are called directly."""
        if self._running:
            raise RuntimeError("Orchestrator run already in progress")
        self._running = True
        self._dry_run = dry_run
        self.abort_reason = None
        t0 = self.clock()
        report = RunReport(run_id=self.ws.run_id, project_name=self.ws.root.name, case_type=CaseType.UNKNOWN,
                           started_at=utcnow())
        self.report = report
        if dry_run:
            report.flags.append("dry-run")
        try:
            yield report
        except _RunAborted:
            pass
        except MissingComponentError as e:
            log.error("%s", e)
            self._abort(report, f"missing component: {e}")
        except Exception as e:  # never lose the report because of a bug
            log.exception("run failed")
            self._abort(report, f"internal error: {type(e).__name__}: {e}")
        finally:
            self._running = False
            report.timings_s["total"] = round(self.clock() - t0, 3)
            self._finalize(report)

    def _abort(self, report: RunReport, why: str) -> None:
        report.flags.append("aborted")
        report.manual_followups.append(f"run aborted: {why}")
        if self.abort_reason is None:
            self.abort_reason = why
        self.ws.journal({"event": "abort", "reason": why})

    def _stop(self, stop_after: Phase | None, phase: Phase) -> bool:
        if stop_after is not None and stop_after == phase:
            assert self.report is not None
            self.report.flags.append(f"stopped-after-{phase.value}")
            self.ws.journal({"event": "stop_after", "phase": phase.value})
            return True
        return False

    def _timed(self, key: str, t0: float) -> None:
        assert self.report is not None
        self.report.timings_s[key] = round(self.report.timings_s.get(key, 0.0) + self.clock() - t0, 3)

    def _run(self, report: RunReport, stop_after: Phase | None, dry_run: bool) -> None:
        """The inline engine (`loop.engine: inline`): the step sequence without LangGraph."""
        if not self.step_intake(report, stop_after):
            return
        if not self.step_analyze(report, stop_after):
            return
        groups = self.step_plan(report, stop_after, dry_run)
        if groups is None:
            return
        for phase, items in groups:
            self.step_phase(report, phase, items, stop_after)
        self.step_finish(report, stop_after)

    # ------------------------------------------------------------------ steps (shared by both engines)
    def step_intake(self, report: RunReport, stop_after: Phase | None) -> bool:
        """1. intake: git snapshot, preflight, effective dependencyManagement. False = stop or abort."""
        t = self.clock()
        self.ws.git_init()
        setter = getattr(self.ws, "set_encoding_candidates", None)
        if callable(setter):
            setter(list(self.cfg.build.encoding_candidates))
        self._preflight(report)
        if "mvn-not-found" in report.flags:
            self._abort(report, f"'{self.cfg.build.mvn}' is not runnable (install Maven or set build.mvn)")
            return False
        self._capture_effective(report)
        self._timed("intake", t)
        return not self._stop(stop_after, Phase.INTAKE)

    def step_analyze(self, report: RunReport, stop_after: Phase | None) -> bool:
        """2. analyze: profile + library advisories. False = stop or abort (not Maven, no pom.xml)."""
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
        report.advisories = self._evaluate_advisories(profile)
        self._timed("analyze", t)
        if profile.build_tool != "maven":
            self._abort(report, f"build tool '{profile.build_tool}' is not supported (Maven only)")
            return False
        if not profile.poms:
            self._abort(report, "no pom.xml found in the project")
            return False
        return not self._stop(stop_after, Phase.ANALYZE)

    def step_plan(self, report: RunReport, stop_after: Phase | None,
                  dry_run: bool) -> list[tuple[Phase, list[tuple[int, PlanStep]]]] | None:
        """3. plan: the migration plan, the run's `BuildFixLoop` and the phase groups (consecutive plan steps of
        the same phase; MSA plans repeat jdk21/spring per service). None = the run stops after `plan`."""
        profile = self.profile
        assert profile is not None
        t = self.clock()
        plan = self._plan_with_advisories(profile, report.advisories)
        plan = self._advise_plan(plan)
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
            return None
        # 4. phases ------------------------------------------------------------
        self.loop = BuildFixLoop(self.cfg, self.ws, self.build_runner, self.triager, self.handlers, self.fixer,
                                 self.kb, self._rewrite_runner_or_none(), profile, plan_notes=plan.notes,
                                 budget=self.budget, dry_run=dry_run, blocked=self.blocked,
                                 locator=self._resolve_locator(), advisories=list(report.advisories or []))
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
        return groups

    def step_phase(self, report: RunReport, phase: Phase, items: list[tuple[int, PlanStep]],
                   stop_after: Phase | None) -> PhaseResult | None:
        """One phase group: run its steps, then credit plan recipes and harvest. Returns the `PhaseResult`
        (also appended to `report.phases`), or None when the group lies after `stop_after` (``phase_skipped``).
        Raises `_RunAborted` when the build-fix loop aborted the run."""
        if stop_after is not None and _PHASE_ORDER.index(phase) > _PHASE_ORDER.index(stop_after):
            # MSA plans repeat jdk21/spring per service: skip only the phases after `stop_after`
            self.ws.journal({"event": "phase_skipped", "phase": phase.value, "reason": f"stop_after={stop_after.value}"})
            return None
        t = self.clock()
        pr = PhaseResult(phase=phase, step_ids=[s.id for _, s in items], ok=True)
        self.ws.journal({"event": "phase_start", "phase": phase.value, "steps": pr.step_ids})
        self._run_phase(pr, items)
        pr.finished_at = utcnow()
        report.phases.append(pr)
        self.ws.journal({"event": "phase_end", "phase": phase.value, "ok": pr.ok, "build_ok": pr.build_ok,
                         "iterations": pr.iterations, "attempts": len(pr.attempts),
                         "remaining_errors": len(pr.remaining_errors), "blocked": [b.id for b in pr.blocked]})
        if self.loop is not None and self.loop.aborted:
            self.ws.journal({"event": "run_aborted", "phase": phase.value, "reason": self.loop.aborted})
            self._timed(phase.value, t)
            raise _RunAborted(self.loop.aborted)
        if not pr.ok and phase != Phase.VERIFY:
            self._phase_failed = True
        if phase != Phase.VERIFY and not pr.skipped:
            self._credit_plan_recipes(pr)
            self._harvest(report, pr)
        self._timed(phase.value, t)
        return pr

    def step_finish(self, report: RunReport, stop_after: Phase | None) -> None:
        """After the last phase group: record `stop_after` when it named a phase that the groups ran."""
        if stop_after is not None and stop_after != Phase.VERIFY:
            self._stop(stop_after, stop_after)

    def _rewrite_runner_or_none(self) -> Any:
        try:
            return self.rewrite_runner
        except MissingComponentError as e:
            log.warning("%s", e)
            return None

    def _resolve_locator(self) -> Any:
        if self._locator is not None:
            return self._locator or None
        return default_locator() if self._real_maven else None

    # ------------------------------------------------------------------ library advisories (analyze / plan side)
    def _evaluate_advisories(self, profile) -> list:
        """knowledge/libraries/advisories.yml findings for this project (springfox, hystrix, sleuth …). Never
        raises; an unavailable advisory package just means no findings. The finalize side (verify → outcome,
        handover, follow-ups, ledger) lives in `_finalize`."""
        adv_cfg = getattr(self.cfg, "advisories", None)
        if adv_cfg is None or not adv_cfg.enabled:
            return []
        try:
            from sbmig.advisory import evaluate_advisories
            findings = list(evaluate_advisories(profile, self.cfg) or [])
        except Exception as e:
            log.warning("library advisories unavailable: %s", e)
            self.ws.journal({"event": "advisories", "error": f"{type(e).__name__}: {e}"})
            return []
        self.ws.journal({"event": "advisories",
                         "findings": [{"id": f.advisory_id, "library": f.library, "status": f.status,
                                       "policy": f.policy, "version": f.detected_version} for f in findings]})
        return findings

    def _plan_with_advisories(self, profile, findings: list):
        """Planner call; findings are passed as `advisories=` when the planner accepts them (fakes in tests and
        older planners do not)."""
        if findings:
            try:
                plan = self.planner(profile, self.cfg, self.kb, advisories=findings)
            except TypeError as e:
                if "advisories" not in str(e):
                    raise
                log.info("planner does not take advisories (%s); planning without them", e)
                plan = self.planner(profile, self.cfg, self.kb)
            self.ws.journal({"event": "advisories_planned",
                             "findings": [{"id": f.advisory_id, "policy": f.policy, "recipes": f.recipes,
                                           "recipe_source": f.recipe_source} for f in findings]})
            return plan
        return self.planner(profile, self.cfg, self.kb)

    def _capture_effective(self, report: RunReport) -> None:
        """Intake: capture the as-is effective dependencyManagement (before any change) for later version carry-over."""
        fn = self._effective_capture
        if fn is None:
            if not self._real_maven:
                self.ws.journal({"event": "effective_depmgmt", "skipped": "custom build runner"})
                return
            try:
                fn = lazy_attr("sbmig.build.effective", "capture_effective_depmgmt", "G1 (build)")
            except MissingComponentError as e:
                self.ws.journal({"event": "effective_depmgmt", "skipped": str(e)})
                return
        try:
            path = fn(self.ws, self.cfg)
        except Exception as e:
            log.warning("effective dependencyManagement capture failed: %s", e)
            self.ws.journal({"event": "effective_depmgmt", "ok": False, "error": f"{type(e).__name__}: {e}"})
            return
        self.ws.journal({"event": "effective_depmgmt", "ok": path is not None, "path": str(path) if path else None})
        if path is not None:
            report.outputs["effective_depmgmt"] = str(path)

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
                if not ok and self._step_mode(step) in NON_FATAL_RESTRUCTURE_MODES:
                    pr.notes.append(f"{step.id}: failed — continuing with the rest of the {step.phase.value} phase")
                elif not ok:
                    aborted = True
                    pr.ok = False
            elif step.kind == StepKind.BUILD:
                ok = self._step_verify(pr, step)
            self.ws.journal({"event": "step_end", "step": step.id, "ok": bool(ok)})
            if self.loop is not None and self.loop.aborted:
                pr.ok = False
                break
        if pr.phase == Phase.VERIFY:
            pr.ok = pr.build_ok = bool(self._verify_results) and all(r.ok for r in self._verify_results)
        elif not loop_ran and pr.ok:
            pr.build_ok = False

    def _maybe_pre_handlers(self, step: PlanStep, idx: int) -> None:
        ids = list(getattr(self, "_pre_ids", []))
        # legacy encodings / BOMs: on JDK 21 `unmappable character` is only a WARNING (the build passes and Korean
        # literals silently become U+FFFD) → always run these idempotent whole-project handlers before the first build
        for hid in ALWAYS_PREEMPTIVE:
            if hid not in ids and self.handlers is not None and self.handlers.get(hid) is not None:
                ids.insert(0, hid)
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
            for f in list(getattr(h, "flags", None) or []):
                self._flag(str(f))
            for n in list(getattr(h, "notes", None) or []):
                self._pending_notes.append(f"handler {hid}: {n}")
                if self.report is not None and str(n).lower().startswith(("install", "follow")) \
                        and n not in self.report.manual_followups:
                    self.report.manual_followups.append(str(n))
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
        # in-repo reactor siblings (shared library roots) only resolve when the plugin runs from the aggregator root
        from .fixloop import build_target_args
        module, reactor_args = build_target_args(self.profile, step.target)
        kwargs = dict(extra_recipe_files=self._extra_recipe_files(step), dry_run=self._dry_run, label=step.id)
        try:
            res = runner.run(list(step.recipes), module=module, extra_args=reactor_args, **kwargs)
        except TypeError:                      # a runner without `extra_args` (fakes / older implementations)
            res = runner.run(list(step.recipes), module=step.target, **kwargs)
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
            self._check_parent_in_rewrite(pr, step, err)
            if _MISSING_RECIPE_RE.search(err):
                pr.notes.append(f"{step.id}: recipe id not found — {err} (programming/config error, phase aborted)")
                return None
            pr.notes.append(f"{step.id}: OpenRewrite failed ({err}); continuing — the build will show the real "
                            "problem")
            pr.remaining_errors.append(BuildError(kind=ErrorKind.REWRITE, message=err[:500], raw=err,
                                                  module=step.target))
        return True

    def _check_parent_in_rewrite(self, pr: PhaseResult, step: PlanStep, err: str) -> None:
        """OpenRewrite failed because the configured in-house parent does not resolve → abort like the build loop."""
        parent = self.cfg.artifacts.parent
        keys = [parent.key, *parent.aliases]
        if self.loop is None or not _PARENT_UNRESOLVABLE_RE.search(err) or not any(k in err for k in keys):
            return
        key = next(k for k in keys if k in err)
        m = re.search(re.escape(key) + r":(?:pom:)?([\w.\-]+)", err)
        gav = GAV.parse(key + (f":{m.group(1)}" if m else (f":{parent.version}" if parent.version else "")))
        e = BuildError(kind=ErrorKind.PARENT_RESOLUTION, message=err.splitlines()[0][:500], raw=err[:2000],
                       artifact=gav, signature=f"parent_resolution|{gav.coordinates}", module=step.target)
        br = BuildResult(ok=False, goal="rewrite", command=[], exit_code=1, duration_s=0.0, log_path="", errors=[e])
        self.loop._parent_abort(pr, step.phase, br, 0)   # noqa: SLF001 - same package
        self._merge_blocked(pr, [it for it in self.blocked.items() if it.reason.value == "parent_not_found"])

    def _merge_blocked(self, pr: PhaseResult, items: list[BlockedItem]) -> None:
        for it in items:
            if all(b.id != it.id for b in pr.blocked):
                pr.blocked.append(it)

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
        self._merge_blocked(pr, res.blocked)
        return res.ok

    def _step_restructure(self, pr: PhaseResult, step: PlanStep) -> bool:
        self._flush_notes(pr)
        assert self.profile is not None and self.report is not None
        mode = self._step_mode(step)
        if self._dry_run:
            pr.notes.append(f"{step.id}: dry-run → restructure ({mode}) not applied")
            return True
        pre = self.ws.head()
        try:
            res = self._call_restructurer(mode, dict(step.params))
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

    @staticmethod
    def _step_mode(step: PlanStep) -> str:
        return str(step.params.get("mode") or _DEFAULT_MODE.get(step.kind, "layout"))

    def _call_restructurer(self, mode: str, params: dict) -> Any:
        assert self.profile is not None
        r = self.restructurer
        try:
            import inspect
            sig = inspect.signature(r.apply)
            takes_params = "params" in sig.parameters or any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values())
        except (TypeError, ValueError):  # pragma: no cover - builtins / mocks
            takes_params = True
        if takes_params:
            return r.apply(self.ws, self.profile, self.cfg, mode=mode, params=params)
        return r.apply(self.ws, self.profile, self.cfg, mode=mode)

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
        from .fixloop import build_target_args
        module, extra = build_target_args(self.profile, step.target)
        kw: dict[str, Any] = {"extra_args": extra} if extra else {}
        res = self.build_runner.run(step.build_goal or "clean package", module=module, label=label,
                                    skip_test_compile=skip_tests, **kw)
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
    def _credit_plan_recipes(self, pr: PhaseResult) -> None:
        """KB recipes (active or candidate) that changed files inside this phase's OpenRewrite runs are credited
        with the phase's build outcome; this is how a candidate earns (or loses) its promotion."""
        kb = getattr(self, "kb", None)
        if kb is None or not hasattr(kb, "list_recipes") or not hasattr(kb, "credit_recipe"):
            return
        try:
            kb_ids = {r.id for r in kb.list_recipes()}
        except Exception:  # pragma: no cover - optional capability
            return
        fp = getattr(getattr(self, "profile", None), "fingerprint", "") or ""
        credited: list[str] = []
        for res in pr.rewrite_results:
            for rid, files in (getattr(res, "recipes_applied", None) or {}).items():
                if rid in kb_ids and files and rid not in credited:
                    try:
                        kb.credit_recipe(rid, bool(pr.build_ok), fp)
                        credited.append(rid)
                    except Exception as e:  # pragma: no cover
                        log.warning("kb.credit_recipe(%s) failed: %s", rid, e)
        if credited:
            self.ws.journal({"event": "kb_credit", "phase": pr.phase.value, "recipes": credited, "success": pr.build_ok})

    def _advise_plan(self, plan):
        """cfg.plan.advisor == "llm": let the (cheap) recipe model review the rule-based plan (additive only)."""
        if self.cfg.plan.advisor != "llm" or self._dry_run or self.profile is None:
            return plan
        try:
            advise = lazy_attr("sbmig.plan", "advise_plan", "A (plan)")
            llm = self.llm_client()
            before = {r.id for st in plan.steps for r in st.recipes}
            plan = advise(plan, self.profile, llm=llm, cfg=self.cfg)
            after = {r.id for st in plan.steps for r in st.recipes}
            self.ws.journal({"event": "plan_advice", "model": self.cfg.llm.recipe_model or self.cfg.llm.model,
                             "added": sorted(after - before),
                             "notes": [n for n in plan.notes if n.startswith("advisor")]})
        except Exception as e:  # the advisor is optional
            log.warning("plan advisor failed: %s", e)
            self.ws.journal({"event": "plan_advice", "error": f"{type(e).__name__}: {e}"})
        return plan

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
        h.recipes_promoted += [r for r in getattr(res, "recipes_promoted", []) if r not in h.recipes_promoted]
        h.ledger_entries += getattr(res, "ledger_entries", 0) or 0
        h.notes += res.notes
        report.harvest = h
        self.ws.journal({"event": "harvest", "phase": pr.phase.value, "cases": res.cases_created,
                         "candidates": res.candidates_created,
                         "promoted": getattr(res, "recipes_promoted", []),
                         "ledger": getattr(res, "ledger_entries", 0)})

    def _record_run_ledger(self, report: RunReport) -> None:
        """Run-level KB ledger lines (unresolved / blocked-only-in-registry) + late auto-promotions."""
        rec = getattr(self._kb, "record_run", None) if self._kb is not None else None
        if not callable(rec) or self._dry_run or self.profile is None or not self.cfg.kb.harvest:
            return
        try:
            res = rec(report, self.profile, self.ws)
        except Exception as e:  # pragma: no cover - KB problems never break the report
            log.warning("kb.record_run failed: %s", e)
            return
        if res is None or not (res.recipes_promoted or res.ledger_entries or res.notes):
            return
        h = report.harvest or HarvestResult()
        h.recipes_promoted += [r for r in res.recipes_promoted if r not in h.recipes_promoted]
        h.ledger_entries += res.ledger_entries
        h.notes += res.notes
        report.harvest = h

    def _flag(self, flag: str) -> None:
        if self.report is not None and flag not in self.report.flags:
            self.report.flags.append(flag)

    # ------------------------------------------------------------------ finalize
    def _finalize_blocked(self, report: RunReport, unresolved: list[BuildError]) -> None:
        """RunReport.blocked = every item of the run (dedupe by id). After a green final build, items whose errors no
        longer show up anywhere (e.g. fixed by the optional bump / restructure) are dropped — unless they belong to
        test sources that only build with -Dmaven.test.skip=true."""
        items: list[BlockedItem] = []
        for it in [*self.blocked.items(), *(b for pr in report.phases for b in pr.blocked)]:
            if all(x.id != it.id for x in items):
                items.append(it)
        if report.success:
            still = {e.signature for e in unresolved}
            kept: list[BlockedItem] = []
            for it in items:
                tests_only = bool(it.errors) and all(e.kind == ErrorKind.TEST_COMPILE for e in it.errors)
                if (tests_only and "test-compile-skipped" in report.flags) or (set(it.signatures) & still):
                    kept.append(it)
                else:
                    self.ws.journal({"event": "blocked_resolved", "id": it.id, "reason": it.reason.value,
                                     "note": "not present in the green final build"})
            items = kept
        report.blocked = items

    def _finalize_advisories(self, report: RunReport) -> None:
        """Library advisories, finalize side: re-detect on the final tree (outcome), add their follow-ups and build
        the Handover (versions replaced + "from here on, your team"). Never raises, never changes the exit code."""
        findings = list(report.advisories or [])
        if findings and not self._dry_run:
            try:
                from sbmig.advisory import verify_advisories
                verified = verify_advisories(findings, self.ws, self.cfg)
                if verified is not None:
                    findings = list(verified)
                    report.advisories = findings
            except Exception as e:
                log.warning("advisory verification failed: %s", e)
                self.ws.journal({"event": "advisories_verified", "error": f"{type(e).__name__}: {e}"})
            self.ws.journal({"event": "advisories_verified",
                             "findings": [{"id": f.advisory_id, "policy": f.policy, "outcome": f.outcome}
                                          for f in findings]})
        if findings:
            self._attribute_llm_fixes(report, findings)
            try:
                from sbmig.advisory import followups_for
                fus = list(followups_for(findings) or [])
            except Exception as e:
                log.warning("advisory follow-ups unavailable: %s", e)
                fus = []
            report.manual_followups += [f for f in dict.fromkeys(fus) if f not in report.manual_followups]
        if self.profile is None:
            return
        effective: dict = {}
        try:
            from sbmig.build.effective import load_effective
            effective = load_effective(self.ws) or {}
        except Exception as e:
            log.debug("as-is effective pom unavailable for the handover: %s", e)
        try:
            from sbmig.report.handover import build_handover
            report.handover = build_handover(self.profile, self.ws, report, effective=effective, cfg=self.cfg)
        except Exception as e:  # pragma: no cover - build_handover never raises
            log.warning("handover failed: %s", e)
            return
        h = report.handover
        self.ws.journal({"event": "handover", "changes": len(h.changes), "carried_over": len(h.carried_over),
                         "from_here": len(h.from_here)})

    @staticmethod
    def _attribute_llm_fixes(report: RunReport, findings: list) -> None:
        """AdvisoryFinding.llm_fixed_files / llm_fix_notes from the run's successful LLM fixes whose prompt carried the
        advisory's hint (FixAttempt.advisory_ids; same predicate as the KB harvest: llm_fix, applied, improved /
        build_ok, cluster not left unresolved). Runs before the Handover is built."""
        good = [a for pr in report.phases for a in pr.attempts
                if a.advisory_ids and a.decision.kind == DecisionKind.LLM_FIX and a.applied
                and a.outcome in ("improved", "build_ok") and a.cluster_resolved is not False]
        for f in findings:
            files: list[str] = []
            notes: list[str] = []
            for a in good:
                if f.advisory_id not in a.advisory_ids:
                    continue
                for ch in a.changes:
                    for p in (ch.path, ch.new_path):
                        p = (p or "").replace("\\", "/").removeprefix("./")
                        if p and p not in files:
                            files.append(p)
                note = _first_sentence(a.analysis, 160)
                if note and note not in notes:
                    notes.append(note)
            f.llm_fixed_files, f.llm_fix_notes = files, notes

    def _finalize(self, report: RunReport) -> None:
        report.finished_at = utcnow()
        usage = LLMUsage()
        for pr in report.phases:
            for a in pr.attempts:
                usage = usage.add(a.llm_usage)
        usage.calls = max(usage.calls, self.budget.llm_calls)
        plan_usage = getattr(getattr(self, "plan", None), "llm_usage", None)
        if plan_usage is not None and plan_usage.calls:
            usage = usage.add(plan_usage)          # plan advisor (recipe model) calls
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
        self._finalize_blocked(report, unresolved)
        report.unresolved = [e for e in unresolved if not self.blocked.is_blocked_error(e)]
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
        self._finalize_advisories(report)
        self._record_run_ledger(report)
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
                         "unresolved": len(report.unresolved), "blocked": len(report.blocked),
                         "llm_calls": report.llm_usage.calls,
                         "outputs": out})
