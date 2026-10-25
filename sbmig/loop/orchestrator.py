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
from sbmig.core.models import (BlockReason, GAV, BlockedItem, BuildError, BuildResult, CaseType, DecisionKind, ErrorKind,
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
# preflight (round 10): `mvn -v` detail → Java major; Maven that cannot start because of JAVA_HOME (not "install Maven")
_JAVA_VERSION_RE = re.compile(r"(?i)java version:?\s*\"?(?P<v>[0-9][0-9._+-]*)")
_JAVA_RUNTIME_RE = re.compile(r"(?i)runtime:\s*(?P<home>[^,;]+)")
_JAVA_HOME_PROBLEM_RE = re.compile(r"(?i)JAVA_HOME|UnsupportedClassVersionError|Unable to locate a Java Runtime|"
                                   r"java: (?:command )?not found|'java' is not recognized|no java runtime")
# infrastructure classification at finalize (round 10, fleet circuit breaker): a resolution error that points at the
# repository / network rather than at the project
_INFRA_REPO_RE = re.compile(r"(?i)could not transfer|failed to transfer|connection refused|connection reset|timed out|"
                            r"unknown ?host|no route to host|pkix path building failed|"
                            r"(?:status code|return code is):?\s*(?:401|403|5\d\d)\b|"
                            r"was cached in the local repository")
# a full disk during a build (javac / Maven / git on Windows and POSIX; "디스크 공간이 부족" = the Korean Windows text)
_INFRA_DISK_RE = re.compile(r"(?i)no space left on device|not enough space on the disk|disk quota exceeded|"
                            r"디스크 공간이 부족|ENOSPC")
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


def parse_renames(out: str) -> dict[str, str]:
    """`git diff --name-status -M -z` output → {old path: new path} of the renames (`R<score>` entries)."""
    toks = out.split("\0")
    moves: dict[str, str] = {}
    i = 0
    while i < len(toks):
        st = toks[i].strip()
        if not st:
            i += 1
            continue
        if st[0] in "RC" and i + 2 < len(toks):          # rename / copy: two paths follow
            if st[0] == "R" and toks[i + 1] and toks[i + 2]:
                moves[toks[i + 1]] = toks[i + 2]
            i += 3
        else:
            i += 2
    return moves


def _aborted(flags: list[str]) -> bool:
    return any(f == "aborted" or f.startswith("aborted:") for f in flags)


_PHASE_ORDER = [Phase.INTAKE, Phase.ANALYZE, Phase.PLAN, Phase.JDK21, Phase.SPRING, Phase.RESTRUCTURE,
                Phase.VERIFY, Phase.REPORT]
STOP_AFTER_PHASES = tuple(p for p in _PHASE_ORDER if p != Phase.REPORT)


def parse_stop_after(value: Any) -> Phase | None:
    """`--stop-after` / `stop_after=` of the CLI, the web UI and `sbmig.api.migrate`: a phase name (case-insensitive)
    before `report`; None / '' = run everything. ValueError names the accepted values."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    text = value.value if isinstance(value, Phase) else str(value).strip().lower()
    try:
        phase = Phase(text)
    except ValueError:
        phase = None
    if phase is None or phase not in STOP_AFTER_PHASES:
        raise ValueError(f"stop_after must be one of: {', '.join(p.value for p in STOP_AFTER_PHASES)}")
    return phase


class _BudgetedLLM:
    """An LLM client whose calls are charged to the run's `LoopBudget` (recipe-candidate generation in the KB
    harvest goes through the same budget as the fix loop)."""

    def __init__(self, inner: Any, budget: LoopBudget, sink: Optional[dict[str, LLMUsage]] = None):
        self._inner, self._budget, self._sink = inner, budget, sink

    def complete(self, *args: Any, **kwargs: Any) -> Any:
        self._budget.charge_llm(1)
        res = self._inner.complete(*args, **kwargs)
        if self._sink is not None:          # per-model usage for the cost table (recipe model calls)
            model = str(getattr(res, "model", "") or kwargs.get("model") or "")
            u = LLMUsage(model=model, calls=1, input_tokens=int(getattr(res, "input_tokens", 0) or 0),
                         output_tokens=int(getattr(res, "output_tokens", 0) or 0))
            self._sink[model] = self._sink.get(model, LLMUsage(model=model)).add(u)
        return res

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class Orchestrator:
    def __init__(self, cfg: Config, ws: Workspace, *, analyzer: Optional[Callable] = None,
                 planner: Optional[Callable] = None, pre_handlers: Optional[Callable] = None,
                 restructurer: Any = None, rewrite_runner: Any = None, build_runner: Any = None,
                 triager: Any = None, handlers: Any = None, fixer: Any = None, kb: Any = None, llm: Any = None,
                 report_writer: Optional[Callable] = None, clock: Callable[[], float] = time.monotonic,
                 locator: Any = None, effective_capture: Optional[Callable] = None,
                 javax_inventory: Any = None):
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
        self.error_tracker: Any = None              # sbmig.loop.error_tracker.ErrorTracker of the run (2026-10-23)
        self._handlers_done: set[str] = set()
        self._jdk_pre_done = False
        self._spring_pre_done = False
        self._verify_results: list[BuildResult] = []
        self._harvest_usage: dict[str, LLMUsage] = {}      # recipe-model calls of the KB harvest, by model
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
        # report.javax_api_inventory: `fn(ws, cfg, profile) -> list[JavaxApiJar]` (sbmig.build.deptree, spawns mvn);
        # None = the real one with the real Maven only, False = off — tests pass a fake
        self._javax_inventory = javax_inventory
        self._running = False
        self.abort_reason: str | None = None   # first `_abort` reason of the current run
        self._run_steps: list[PlanStep] = []   # the run's steps incl. an appended verify.build (step_plan)
        self._verify_done: list[str] = []      # verify step ids whose final build has a result (invariant 1, round 10)

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
        # per-run state (an orchestrator is normally used once; a second run must not see the first one's results)
        self._verify_results = []
        self._verify_done = []
        self._run_steps = []
        self._mvn_java_problem = self._jdk_problem = ""
        self._after_abort = False
        self._phase_failed = False
        self._pending_notes = []
        self.loop = None
        self.error_tracker = None
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
            self._final_build_after_abort(report)
        except Exception as e:  # never lose the report because of a bug
            log.exception("run failed")
            self._abort(report, f"internal error: {type(e).__name__}: {e}")
            self._final_build_after_abort(report)
        finally:
            self._running = False
            report.timings_s["total"] = round(self.clock() - t0, 3)
            self._finalize(report)

    def _verify_steps(self) -> list[PlanStep]:
        """The run's final-build steps (MSA without an aggregator: one per service); `verify.build` when the plan had
        none and `loop.always_final_verify` is set (as `step_plan` appends it)."""
        steps = list(self._run_steps or (self.plan.steps if self.plan is not None else []))
        out = [s for s in steps if s.kind == StepKind.BUILD and s.phase == Phase.VERIFY]
        if not out and self.plan is not None and self.cfg.loop.always_final_verify:
            out = [PlanStep(id="verify.build", kind=StepKind.BUILD, phase=Phase.VERIFY, title="final mvn clean package",
                            build_goal="clean package", reason="always_final_verify")]
        return out

    def _verify_missing(self) -> list[PlanStep]:
        return [s for s in self._verify_steps() if s.id not in self._verify_done]

    def _final_build_after_abort(self, report: RunReport) -> None:
        """Invariant 1 (build gate): a run that died of an internal error / missing component after planning still
        ends with the final `mvn clean package` attempt, so report.md lists the real unresolved errors of the tree
        as it was left. Deliberate aborts (`_RunAborted`: no Maven, unresolvable parent) build nothing — the build
        would fail for the very reason the run aborted. Round 10: every final-build step without a result runs — an
        MSA run whose svc-a verify passed before svc-b's step crashed still builds svc-b."""
        if self._dry_run or self.plan is None or not self.cfg.loop.always_final_verify:
            return
        steps = self._verify_missing()
        if not steps:
            return
        self._after_abort = True              # the build only: no last fix loop on top of what just crashed
        pr = PhaseResult(phase=Phase.VERIFY, step_ids=[s.id for s in steps], ok=False)
        pr.notes.append("final build after the run aborted (best effort: earlier phases are incomplete)")
        self.ws.journal({"event": "phase_start", "phase": Phase.VERIFY.value, "steps": pr.step_ids,
                         "after_abort": True})
        try:
            for s in steps:
                self._step_verify(pr, s)
        except Exception as e:   # the build runner itself failed: keep the abort report
            log.warning("final build after the abort failed: %s", e)
            pr.notes.append(f"final build failed: {type(e).__name__}: {e}")
        pr.ok = pr.build_ok = bool(self._verify_results) and all(r.ok for r in self._verify_results)
        pr.finished_at = utcnow()
        report.phases.append(pr)
        self.ws.journal({"event": "phase_end", "phase": Phase.VERIFY.value, "ok": pr.ok, "build_ok": pr.build_ok,
                         "iterations": pr.iterations, "attempts": 0, "remaining_errors": len(pr.remaining_errors),
                         "blocked": [], "after_abort": True})

    def _abort(self, report: RunReport, why: str) -> None:
        if "aborted" not in report.flags:
            report.flags.append("aborted")
        report.manual_followups.append(f"run aborted: {why}")
        if self.abort_reason is None:
            self.abort_reason = why
        log.error("run aborted: %s", why)          # one greppable line (round 10)
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
            if self._mvn_java_problem:
                self._abort(report, f"'{self.cfg.build.mvn}' cannot start: {self._mvn_java_problem} — set "
                                    f"build.java_home (or JAVA_HOME) to a JDK {self._java_major_wanted()} home")
            else:
                self._abort(report, f"'{self.cfg.build.mvn}' is not runnable (install Maven or set build.mvn)")
            return False
        if "jdk-not-21" in report.flags:
            self._abort(report, self._jdk_problem)
            return False
        if not self._check_build_tool(report):
            self._timed("intake", t)
            return False
        if not self._intake_layout(report):
            self._timed("intake", t)
            return False
        self._capture_effective(report)
        self._timed("intake", t)
        return not self._stop(stop_after, Phase.INTAKE)

    def _check_build_tool(self, report: RunReport) -> bool:
        """Catalog S4 at intake: only Maven projects; Gradle / Ant / no build abort before any Maven call.
        `step_analyze` keeps the same check (on `profile.build_tool`) as a fallback."""
        try:
            from sbmig.analyze import detect_build_tool
        except ImportError:  # pragma: no cover - analyze component missing; step_analyze still checks
            return True
        det = detect_build_tool(self.ws)
        if det.tool != "maven":
            report.flags.append(f"unsupported-build-tool:{det.tool}")
            self._abort(report, det.reason)
            self.ws.journal({"event": "build_tool", "tool": det.tool, "ok": False, "reason": det.reason})
            return False
        self.ws.journal({"event": "build_tool", "tool": "maven", "ok": True})
        return True

    def _intake_layout(self, report: RunReport) -> bool:
        """2026-10-23 (company case: APP/BATCH, APP/WEB, APP/EAR with no root pom): sibling poms that depend on each
        other get a generated root aggregator pom (`layout.generate_aggregator`), so they build in one reactor and the
        restructure merges them; a root pom that is an EAR packaging modules missing from the tree (a run on APP/EAR
        alone) is rejected — an EAR holds no code, and its WAR dependency must never reach the back module."""
        try:
            from sbmig.analyze.aggregator import ear_without_modules, plan_aggregator, write_aggregator
        except ImportError:  # pragma: no cover
            return True
        why = ear_without_modules(self.ws)
        if why:
            report.flags.append("ear-without-modules")
            self._abort(report, why)
            self.ws.journal({"event": "ear_without_modules", "reason": why})
            return False
        if not getattr(self.cfg.layout, "generate_aggregator", True):
            return True
        plan = plan_aggregator(self.ws)
        if plan is None:
            return True
        try:
            path = write_aggregator(self.ws, plan)
            self.ws.commit(f"intake: generated root aggregator pom ({', '.join(plan.modules)})")
        except Exception as e:  # never lose the run over this: the analysis then sees no root pom as before
            log.warning("could not write the aggregator pom: %s", e)
            report.manual_followups.append(f"sibling poms without a root pom: the aggregator pom could not be written ({type(e).__name__}: {e}) — add a root pom.xml listing {', '.join(plan.modules)}")
            return True
        if "aggregator-generated" not in report.flags:
            report.flags.append("aggregator-generated")
        msg = (f"no aggregator pom: sbmig generated {path} ({plan.artifact_id}, modules {', '.join(plan.modules)}) — "
               f"{plan.reason}; the as-is build of each module stays as it was")
        if plan.excluded:
            msg += f"; not linked to them and left out: {', '.join(plan.excluded)}"
        report.manual_followups.append(msg)
        log.info("intake: %s", msg)
        self.ws.journal({"event": "aggregator_generated", "path": path, "artifact_id": plan.artifact_id,
                         "name": plan.name, "modules": plan.modules, "excluded": plan.excluded})
        return True

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
                         "spring_boot": profile.spring_boot_version,
                         "properties_external": len(getattr(getattr(profile, "properties", None), "external", None)
                                                    or [])})
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
        try:    # X9: repositories declared inside the poms — a note, a follow-up when no settings mirror covers them
            from sbmig.build.mvn import repository_notes
            for n in repository_notes(profile.poms, self.cfg):
                if n not in plan.notes:
                    plan.notes.append(n)
                (log.warning if n.startswith("followup:") else log.info)("%s", n)
        except Exception as e:  # noqa: BLE001 - never fail planning over a note
            log.debug("repository notes skipped: %s", e)
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
        self._start_error_tracker(dry_run)
        steps = list(plan.steps)
        if self.cfg.loop.always_final_verify and not any(s.kind == StepKind.BUILD for s in steps):
            steps.append(PlanStep(id="verify.build", kind=StepKind.BUILD, phase=Phase.VERIFY,
                                  title="final mvn clean package", build_goal="clean package",
                                  reason="always_final_verify"))
        self._run_steps = list(steps)
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
        try:
            self._run_phase(pr, items)
        except BaseException as e:
            # round 10: an exception inside a phase (git timeout, a bug) must not lose what the phase did — svc-a's
            # green verify of an MSA run stays in the report next to the abort
            pr.ok = False
            pr.notes.append(f"phase interrupted: {type(e).__name__}: {e}")
            pr.finished_at = utcnow()
            report.phases.append(pr)        # its blocked items are in the run-wide registry (report.blocked)
            self.ws.journal({"event": "phase_end", "phase": phase.value, "ok": False, "build_ok": pr.build_ok,
                             "iterations": pr.iterations, "attempts": len(pr.attempts),
                             "remaining_errors": len(pr.remaining_errors), "blocked": [b.id for b in pr.blocked],
                             "interrupted": f"{type(e).__name__}: {e}"[:300]})
            raise
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
            if pr.iterations:
                self._credit_plan_recipes(pr)
            else:   # no build-fix loop in this phase (loop.phases.<p>.enabled=false): nothing proves success/failure
                log.info("phase %s ran no build: KB recipes are not credited", phase.value)
            self._harvest(report, pr)
        elif phase == Phase.VERIFY and pr.attempts:   # the final verify loop fixed something → the KB learns it too
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
                    res = avail(self.cfg)
                except TypeError:
                    res = avail()
                # maven_available returns (ok, detail); test fakes return a bare bool — a tuple is always truthy,
                # so `bool(res)` alone never flagged a missing Maven (found while writing the handbook, 2026-10-21)
                ok, detail = (bool(res[0]), str(res[1] or "")) if isinstance(res, tuple) else (bool(res), "")
                checks["mvn"] = ok
                if detail:
                    checks["mvn_detail"] = detail
                if not ok:
                    report.flags.append("mvn-not-found")
                    if _JAVA_HOME_PROBLEM_RE.search(detail):     # Maven is there; its Java is not (round 10)
                        self._mvn_java_problem = " ".join(detail.split())[:200]
                        report.manual_followups.append(
                            f"'{self.cfg.build.mvn}' cannot start: {self._mvn_java_problem} — set build.java_home "
                            f"(or JAVA_HOME) to a JDK {self._java_major_wanted()} home ({self._java_home_text()}); "
                            "every build below would fail")
                    else:
                        report.manual_followups.append(f"'{self.cfg.build.mvn}' is not runnable"
                                                       + (f" ({detail})" if detail else "")
                                                       + ": install Maven or set build.mvn; every build below will "
                                                         "fail")
                else:
                    self._check_java_major(report, detail, checks)
            except MissingComponentError as e:
                checks["mvn"] = f"unknown ({e})"
        parent = self.cfg.artifacts.parent
        if not parent.group_id or not parent.artifact_id or "CHANGE" in parent.group_id.upper():
            checks["artifacts.parent"] = "not configured"
            report.manual_followups.append("artifacts.parent is not configured: pass the company artifacts yml "
                                           "with --config")
        else:
            checks["artifacts.parent"] = f"{parent.key}:{parent.version or '-'}"
        for name in ("boot_parent", "comp_parent"):               # round 9b: the module role parents
            spec = getattr(self.cfg.artifacts, name, None)
            if spec is not None:
                checks[f"artifacts.{name}"] = f"{spec.key}:{spec.version or '-'}"
        checks["java_home"] = self.cfg.build.java_home or "(inherited)"
        self.ws.journal({"event": "preflight", **checks})

    _mvn_java_problem = ""
    _jdk_problem = ""

    def _java_major_wanted(self) -> int:
        return int(getattr(self.cfg.build, "require_java_major", 0) or 0) or 21

    def _java_home_text(self) -> str:
        import os
        home = self.cfg.build.java_home or os.environ.get("JAVA_HOME") or ""
        return (f"build.java_home={home}" if self.cfg.build.java_home else
                f"JAVA_HOME={home}" if home else "JAVA_HOME not set: mvn uses the java on PATH")

    def _check_java_major(self, report: RunReport, detail: str, checks: dict[str, Any]) -> None:
        """Round 10 (A10): `mvn -v` must report `build.require_java_major` (21) — a JDK 17 on JAVA_HOME made every
        build of a company run fail for a reason no fix loop can change. Unparseable / absent version → no check."""
        want = int(getattr(self.cfg.build, "require_java_major", 0) or 0)
        m = _JAVA_VERSION_RE.search(detail or "")
        if want <= 0 or m is None:
            return
        ver = m.group("v").strip(".,")
        parts = re.split(r"[._+-]", ver)
        try:
            major = int(parts[1]) if parts[0] == "1" and len(parts) > 1 else int(parts[0])
        except ValueError:
            return
        checks["java_major"] = major
        if major == want:
            return
        rt = _JAVA_RUNTIME_RE.search(detail or "")
        where = f"runtime {rt.group('home').strip()}" if rt else self._java_home_text()
        # one line, the abort's follow-up (`run aborted: Maven runs on Java 17.0.9 (…), not Java 21: …`)
        self._jdk_problem = (f"Maven runs on Java {ver} ({where}), not Java {want}: set build.java_home (or JAVA_HOME) "
                             f"to a JDK {want} home and rerun")
        report.flags.append("jdk-not-21")
        log.error("preflight: %s", self._jdk_problem)

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
                self._run_handlers(jdk_ids, "pre-emptive (before the first build)", phase=step.phase)
        if not self._spring_pre_done and step.phase == Phase.SPRING and step.kind == StepKind.BUILD_FIX_LOOP \
                and idx > self._last_spring_rewrite:
            # jakarta-dependent handlers wait until the spring recipes migrated the code
            self._spring_pre_done = True
            if spring_ids:
                self._run_handlers(spring_ids, "pre-emptive (after the jakarta/spring recipes)", phase=step.phase)
        if not self._spring_pre_done and step.phase == Phase.RESTRUCTURE:
            self._spring_pre_done = True
            if spring_ids:
                self._run_handlers(spring_ids, "pre-emptive (before restructure)", phase=step.phase)

    def _run_handlers(self, ids: list[str], why: str, pr: Optional[PhaseResult] = None, *,
                      step_id: Optional[str] = None, phase: Optional[Phase] = None) -> list[str]:
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
            # error cases: errors gone before the next build with no fix attempt are credited to this step
            self._note_step(step_id or f"handlers:{','.join(done)}", phase or (pr.phase if pr else Phase.JDK21))
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
        self._run_handlers(list(ids), f"plan step {step.id}", pr, step_id=step.id, phase=step.phase)

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
        clean_before = self._dry_run or not self.ws.is_dirty()
        res = self._run_rewrite_once(runner, step)
        # catalog X8: the recipe's own resolver could not download Maven metadata → literal `4.0.x` versions
        unresolved = self._unresolved_versions(res)
        if unresolved and self.cfg.openrewrite.download_retry > 0 and clean_before:
            wait_s = max(0, int(self.cfg.openrewrite.download_retry_wait_s))
            self.ws.revert_to(pre)                 # drop the half-resolved result; tree is clean again
            self.ws.journal({"event": "rewrite_unresolved_versions", "step": step.id, "poms": unresolved,
                             "retry": True, "wait_s": wait_s, "log": res.log_path})
            log.warning("%s: literal versions left after the rewrite (%s) → retrying once in %ss", step.id,
                        self._fmt_unresolved(unresolved), wait_s)
            if wait_s:
                getattr(self, "_sleep", time.sleep)(wait_s)
            res = self._run_rewrite_once(runner, step)
            unresolved = self._unresolved_versions(res)
        if unresolved:
            self._report_unresolved_versions(pr, step, res, unresolved)
        pr.rewrite_results.append(res)
        mark = getattr(self.triager, "mark_recipes_run", None)
        if callable(mark):
            mark([r.id for r in step.recipes], step.phase)
        sha = None
        if not self._dry_run and self.ws.is_dirty():
            sha = self.ws.commit(f"rewrite: {step.id}")
        if sha and sha != pre:
            self._note_step(step.id, step.phase)
        self.ws.journal({"event": "rewrite", "step": step.id, "ok": res.ok, "recipes": res.recipes,
                         "changed_files": len(res.changed_files), "commit": sha if sha != pre else None,
                         "error": res.error, "log": res.log_path, "dry_run": self._dry_run})
        if getattr(res, "parse_problems", None):
            self._report_parse_problems(pr, step, res)
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

    # catalog X8: literal versions the rewrite leaves when its resolver could not read maven-metadata.xml
    UNRESOLVED_VERSIONS_FLAG = "rewrite-unresolved-versions"
    _POM_VALUE_RE = re.compile(r"<([\w.\-]+)>([^<>]*)</\1>")
    _UNRESOLVED_VERSION_RE = re.compile(r"^(?:\d+(?:\.\d+)*\.x|latest\.(?:release|integration|patch))$")

    @staticmethod
    def _fmt_unresolved(unresolved: dict[str, list[str]]) -> str:
        return "; ".join(f"{p}: {', '.join(vs)}" for p, vs in unresolved.items())

    def _run_rewrite_once(self, runner: Any, step: PlanStep) -> Any:
        # in-repo reactor siblings (shared library roots) only resolve when the plugin runs from the aggregator root
        from .fixloop import build_target_args
        module, reactor_args = build_target_args(self.profile, step.target)
        kwargs = dict(extra_recipe_files=self._extra_recipe_files(step), dry_run=self._dry_run, label=step.id)
        try:
            return runner.run(list(step.recipes), module=module, extra_args=reactor_args, **kwargs)
        except TypeError:                      # a runner without `extra_args` (fakes / older implementations)
            return runner.run(list(step.recipes), module=step.target, **kwargs)

    def _unresolved_versions(self, res: Any) -> dict[str, list[str]]:
        """pom path → literal unresolved versions (`4.0.x`, `latest.release`, …) a successful, non-dry rewrite
        left in the changed poms (every pom when the runner reports no changed files)."""
        if self._dry_run or not getattr(res, "ok", False):
            return {}
        changed = list(getattr(res, "changed_files", None) or [])
        poms = [p for p in changed if p == "pom.xml" or p.endswith("/pom.xml")] if changed else self.ws.pom_files()
        out: dict[str, list[str]] = {}
        for p in sorted(dict.fromkeys(poms)):
            try:
                text = self.ws.read(p)
            except (OSError, UnicodeDecodeError):
                continue
            found = list(dict.fromkeys(m.group(2).strip() for m in self._POM_VALUE_RE.finditer(text)
                                       if self._UNRESOLVED_VERSION_RE.match(m.group(2).strip())))
            if found:
                out[p] = found
        return out

    def _report_unresolved_versions(self, pr: PhaseResult, step: PlanStep, res: Any,
                                    unresolved: dict[str, list[str]]) -> None:
        """Keep the result (the build loop shows what breaks), but say why: flag, phase note, follow-up."""
        self.ws.journal({"event": "rewrite_unresolved_versions", "step": step.id, "poms": unresolved,
                         "retry": False, "log": getattr(res, "log_path", None)})
        self._flag(self.UNRESOLVED_VERSIONS_FLAG)
        what = self._fmt_unresolved(unresolved)
        pr.notes.append(f"{step.id}: {self.UNRESOLVED_VERSIONS_FLAG} — the recipe run left literal versions "
                        f"({what}); the build will fail on dependency resolution")
        versions = ", ".join(f"`{v}`" for v in dict.fromkeys(v for vs in unresolved.values() for v in vs))
        fu = (f"the recipe run could not download Maven metadata (proxy / rate limit): literal versions {versions} "
              f"remain in {', '.join(unresolved)} — re-run sbmig later or set the versions by hand")
        if self.report is not None and fu not in self.report.manual_followups:
            self.report.manual_followups.append(fu)

    # 2026-10-22 (company run): files the rewrite plugin could not parse never see the type-attributed recipes
    # (Spring Data, Hibernate, method renames) — the source handlers / fix loop cover them; say so in the report
    PARSE_PROBLEMS_FLAG = "rewrite-parse-problems"

    def _report_parse_problems(self, pr: PhaseResult, step: PlanStep, res: Any) -> None:
        files = [str(f) for f in (getattr(res, "parse_problems", None) or [])]
        self.ws.journal({"event": "rewrite_parse_problems", "step": step.id, "files": files,
                         "log": getattr(res, "log_path", None)})
        self._flag(self.PARSE_PROBLEMS_FLAG)
        shown = ", ".join(files[:5]) + (f", … (+{len(files) - 5})" if len(files) > 5 else "")
        pr.notes.append(f"{step.id}: OpenRewrite could not parse {len(files)} source file(s) ({shown}) — recipes "
                        "that need type information skipped them; the jakarta-namespace-sweep / "
                        "spring-data-crud-repository handlers and the fix loop cover what the build finds")
        log.warning("%s: rewrite parse problems in %d file(s): %s", step.id, len(files), shown)

    def _check_parent_in_rewrite(self, pr: PhaseResult, step: PlanStep, err: str) -> None:
        """OpenRewrite failed because the configured in-house parent does not resolve → abort like the build loop."""
        keys = self.cfg.artifacts.parent_keys()                # root parent + boot_parent / comp_parent (round 9b)
        if self.loop is None or not _PARENT_UNRESOLVABLE_RE.search(err) or not any(k in err for k in keys):
            return
        key = next(k for k in keys if k in err)
        spec = next((s for s in self.cfg.artifacts.parent_specs() if key in (s.key, *s.aliases)),
                    self.cfg.artifacts.parent)
        m = re.search(re.escape(key) + r":(?:pom:)?([\w.\-]+)", err)
        gav = GAV.parse(key + (f":{m.group(1)}" if m else (f":{spec.version}" if spec.version else "")))
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
            self._restructure_failed(step, mode, f"{type(e).__name__}: {e}")
            return False
        sha = self.ws.commit(f"restructure: {step.id}") if self.ws.is_dirty() else self.ws.head()
        if sha != pre:
            self._note_step(step.id, step.phase)
            self._follow_moves(step, pre, sha)
        self.report.manual_followups += [f for f in res.manual_followups if f not in self.report.manual_followups]
        for flag in list(getattr(res, "flags", None) or []):      # 2026-10-23: e.g. web-xml-registered
            self._flag(flag)
        pr.notes += res.notes
        merged = list(getattr(res, "merged_modules", None) or [])
        kept_boot = getattr(res, "kept_boot_module", None)
        conflicts = list(getattr(res, "merge_conflicts", None) or [])
        seen = {(c.path, c.kept_from) for c in self.report.merge_conflicts}
        for c in conflicts:
            if (c.path, c.kept_from) not in seen:
                seen.add((c.path, c.kept_from))
                self.report.merge_conflicts.append(c)
        if self.loop is not None:          # round 9: the restructure-phase fix loop shows dropped copies to the LLM
            self.loop.merge_conflicts = list(self.report.merge_conflicts)
        ph_files = list(getattr(res, "placeholder_files", None) or [])
        ph_keys = list(dict.fromkeys(getattr(res, "placeholders_written", None) or []))
        self._merge_placeholders(ph_files, ph_keys)
        if ph_files or ph_keys:
            self.ws.journal({"event": "placeholders", "step": step.id, "files": ph_files, "keys": len(ph_keys)})
        summary = (f"{step.id} ({mode}): {len(res.changed_files)} file(s) changed, {res.moved_files} moved, "
                   f"{len(res.generated_files)} generated; mandatory added: {', '.join(res.mandatory_added) or '-'}; "
                   f"optional bumped: {', '.join(res.optional_bumped) or '-'}; placeholders: {len(ph_keys)}")
        pr.notes.append(summary)
        if merged or kept_boot or conflicts:
            differing = sum(1 for c in conflicts if not c.identical)
            pr.notes.append(f"{step.id}: merged modules: {', '.join(merged) or '-'}; kept boot module: "
                            f"{kept_boot or '-'}; merge conflicts: {len(conflicts)} ({differing} differing)")
        self.ws.journal({"event": "restructure", "step": step.id, "mode": mode, "ok": res.ok,
                         "commit": sha if sha != pre else None, "changed_files": len(res.changed_files),
                         "moved": res.moved_files, "generated": res.generated_files,
                         "mandatory_added": res.mandatory_added, "optional_bumped": res.optional_bumped,
                         "merged_modules": merged, "kept_boot_module": kept_boot,
                         "merge_conflicts": len(conflicts), "error": res.error})
        if not res.ok:
            pr.notes.append(f"{step.id}: restructure failed: {res.error or 'unknown error'} → skipping the rest of "
                            "the restructure phase (verify still runs)")
            self._restructure_failed(step, mode, res.error or "unknown error")
        return res.ok

    def _follow_moves(self, step: PlanStep, pre: str, sha: str) -> None:
        """Round 10 (A4): the files the step moved (`git diff -M` renames) → the run-wide blocked registry re-keys
        them, so a file blocked before the move stays blocked at `{name}-back/src/…`. Best effort: a git problem
        only means the moved errors are looked at again."""
        try:
            res = self.ws._git("diff", "--name-status", "-M", "-z", pre, sha, check=False)   # noqa: SLF001
            moves = parse_renames(res.stdout or "") if res.returncode == 0 else {}
        except Exception as e:  # noqa: BLE001 - never fail the restructure over the bookkeeping
            log.debug("restructure moves of %s unavailable: %s", step.id, e)
            return
        if not moves:
            return
        n = self.blocked.note_moves(moves)
        self.ws.journal({"event": "restructure_moves", "step": step.id, "moved": len(moves), "blocked_rekeyed": n})

    def _restructure_failed(self, step: PlanStep, mode: str, why: str) -> None:
        """Round 9c: a failed / crashed restructure step must not look like a successful migration — the final build
        may pass on the as-is tree, but the company layout / parents were not produced. Flag `restructure-failed`
        (→ `success=False`, exit 2) and say so in the Handover."""
        assert self.report is not None
        self.ws.journal({"event": "restructure_failed", "step": step.id, "mode": mode, "error": why[:500]})
        if mode == "optional_bump":          # optional in-house bumps are best effort: a follow-up, not a failure
            self.report.manual_followups.append(f"optional artifact bump step {step.id} failed: {why[:300]} — bump "
                                                "the in-house optional artifacts by hand")
            return
        self._flag("restructure-failed")
        self.report.manual_followups.append(
            f"restructure step {step.id} ({mode}) failed: {why[:300]} — the tree keeps the as-is layout / parents; "
            "the final build ran on it, so BUILD SUCCESS here does not mean the migration is complete")

    def _merge_placeholders(self, files: list[str], keys: list[str]) -> None:
        """RunReport.properties = profile inventory + the placeholders every restructure step wrote (dedupe, ordered)."""
        assert self.report is not None
        base = self.report.properties
        if base is None:
            base = getattr(self.profile, "properties", None)
        if base is None:
            from sbmig.core.models import PropertyInventory
            base = PropertyInventory()
        merged_files = list(base.placeholder_files) + [f for f in files if f not in base.placeholder_files]
        merged_keys = list(base.placeholders_written)
        for k in keys:
            if k not in merged_keys:
                merged_keys.append(k)
        self.report.properties = base.model_copy(update={"placeholder_files": merged_files,
                                                         "placeholders_written": merged_keys})

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
        if mode in ("layout", "msa_pom", "boot_pom") and self.cfg.layout.drop_managed_versions != "off":
            params.setdefault("effective_pom", self._effective_pom_after)   # round 9 / 9c, zero-arg callable
        if takes_params:
            return r.apply(self.ws, self.profile, self.cfg, mode=mode, params=params)
        return r.apply(self.ws, self.profile, self.cfg, mode=mode)

    EFFECTIVE_AFTER_FILE = "depmgmt-after-restructure.json"

    def _effective_pom_after(self) -> Optional[dict]:
        """Round 9 (`layout.drop_managed_versions`): the effective dependencyManagement of the tree *after* the
        restructurer set the in-house parent, captured into a separate file so the as-is capture (`depmgmt.json`,
        used by the carry-over handler) is never overwritten. None = not available (custom build runner without a
        capture, a capture that cannot take a file name, Maven failure) → the restructurer skips the cleanup."""
        fn = self._effective_capture
        if fn is None:
            if not self._real_maven:
                self.ws.journal({"event": "effective_depmgmt_after", "skipped": "custom build runner"})
                return None
            try:
                fn = lazy_attr("sbmig.build.effective", "capture_effective_depmgmt", "G1 (build)")
            except MissingComponentError as e:
                self.ws.journal({"event": "effective_depmgmt_after", "skipped": str(e)})
                return None
        try:
            load = lazy_attr("sbmig.build.effective", "load_effective", "G1 (build)")
            path = fn(self.ws, self.cfg, name=self.EFFECTIVE_AFTER_FILE)
            data = load(self.ws, name=self.EFFECTIVE_AFTER_FILE) if path is not None else None
        except TypeError as e:               # a capture / loader without `name` → never clobber depmgmt.json
            self.ws.journal({"event": "effective_depmgmt_after", "skipped": f"capture without name=: {e}"})
            return None
        except Exception as e:
            log.warning("effective dependencyManagement (after restructure) capture failed: %s", e)
            self.ws.journal({"event": "effective_depmgmt_after", "ok": False, "error": f"{type(e).__name__}: {e}"})
            return None
        self.ws.journal({"event": "effective_depmgmt_after", "ok": bool(data), "path": str(path) if path else None})
        return data or None

    def _step_verify(self, pr: PhaseResult, step: PlanStep) -> bool:
        self._flush_notes(pr)
        if self._dry_run:
            pr.notes.append(f"{step.id}: dry-run → final build skipped")
            return True
        if not self.cfg.loop.always_final_verify and self._phase_failed:
            pr.notes.append(f"{step.id}: skipped (an earlier phase failed and loop.always_final_verify=false)")
            return False
        label = "verify" + (f"-{step.params['service']}" if step.params.get("service") else "")
        from .fixloop import build_target_args
        module, extra = build_target_args(self.profile, step.target)
        kw: dict[str, Any] = {"extra_args": extra} if extra else {}
        goal = step.build_goal or "clean package"

        def _skip_flags() -> tuple[bool, bool]:
            # read at build time: the final fix loop may have triggered the test-compile / tests fallback (L7 / L7b);
            # round 10: per build target — svc-a's fallback does not change how svc-b's final build runs
            tc, ts = self._loop_skips(step.target)
            skip_tests = bool(getattr(self.cfg.build, "skip_test_compile", False)) or tc
            # round 8: only runtime test failures were left → the final verify packages with -DskipTests too
            # (skip_test_compile wins when both apply: -Dmaven.test.skip=true already implies no test run)
            skip_run = ts and not skip_tests
            return skip_tests, skip_run

        def _final_build() -> BuildResult:
            skip_tests, skip_run = _skip_flags()
            if skip_run:
                try:
                    return self.build_runner.run(goal, module=module, label=label, skip_test_compile=False,
                                                 skip_tests=True, **kw)
                except TypeError:  # an older runner without the `skip_tests` option
                    kw["extra_args"] = [*extra, "-DskipTests"]
                    return self.build_runner.run(goal, module=module, label=label, skip_test_compile=False, **kw)
            return self.build_runner.run(goal, module=module, label=label, skip_test_compile=skip_tests, **kw)

        res = _final_build()
        self._observe_final("verify-final", res, step.target)
        if not res.ok and self._verify_fix_loop(pr, step, res):
            if self.loop is not None and self.loop.aborted:
                pr.notes.append(f"{step.id}: final fix loop aborted the run ({self.loop.aborted}); the failed build stands")
            else:
                pr.notes.append(f"{step.id}: final build repeated after the fix loop")
                log.info("verify: repeating `mvn %s` after the final fix loop", goal)
                res = _final_build()
                self._observe_final("verify-final-2", res, step.target)
        self._verify_results.append(res)
        if step.id not in self._verify_done:
            self._verify_done.append(step.id)
        skip_tests, skip_run = _skip_flags()
        assert self.report is not None
        self.report.final_build = res
        pr.iterations += 1
        pr.remaining_errors += res.errors
        from sbmig.core.models import IterationRecord
        pr.iteration_records.append(IterationRecord(phase=Phase.VERIFY, iteration=pr.iterations, build_ok=res.ok,
                                                    error_count=res.error_count, signatures=res.signature_multiset(),
                                                    build_log=res.log_path))
        if skip_tests and getattr(self.cfg.build, "skip_test_compile", False):
            pr.notes.append(f"{step.id}: built with -Dmaven.test.skip=true (build.skip_test_compile: tests are never "
                            "compiled in this configuration)")
        elif skip_tests:
            pr.notes.append(f"{step.id}: built with -Dmaven.test.skip=true (test-compile-skipped earlier)")
        if skip_run:
            pr.notes.append(f"{step.id}: built with -DskipTests (tests-skipped earlier: runtime test failures only)")
        self.ws.journal({"event": "verify", "step": step.id, "target": step.target, "ok": res.ok,
                         "errors": res.error_count, "log": res.log_path, "skip_test_compile": skip_tests,
                         "skip_tests": skip_run})
        return res.ok

    def _loop_skips(self, target: Optional[str]) -> tuple[bool, bool]:
        """(test-compile skipped, tests skipped) of the fix loop's fallbacks for a build of `target`."""
        lp = self.loop
        if lp is None:
            return False, False
        fn = getattr(lp, "skips_for", None)
        if callable(fn):
            try:
                tc, ts = fn(target)
                return bool(tc), bool(ts)
            except Exception:  # pragma: no cover - a foreign loop object
                pass
        return bool(getattr(lp, "test_compile_skipped", False)), bool(getattr(lp, "tests_skipped", False))

    def _verify_fix_loop(self, pr: PhaseResult, step: PlanStep, first: BuildResult) -> bool:
        """The final `mvn clean package` failed → one last build-fix loop on `loop.phases.verify` (goal `package`),
        after which the caller repeats the final build (invariant 1: the run still ends on a `clean package`).
        Company runs (`plan.parent_phase: spring`) have no package-goal loop before verify, so without this a
        packaging-only error (or a compile error the spring loop ran out of rounds on) had no fix attempt at all.
        Clusters blocked earlier for `repeated_failure` / `oscillation` are released and get
        `loop.verify_extra_llm_attempts` more LLM attempts (`decide.py`, escalation model when configured); policy
        blocks (javax API, missing artifact, …) and budget blocks stay. Returns True when the loop ran."""
        pc = self.cfg.loop.phases.get(Phase.VERIFY.value)
        if pc is None or not pc.enabled or pc.max_iterations <= 0 or self.loop is None or self._dry_run \
                or getattr(self, "_after_abort", False):
            return False
        if self.budget.time_exceeded():
            pr.notes.append(f"{step.id}: failed; no final fix loop (run time budget exhausted)")
            return False
        from sbmig.triage.cluster import cluster_errors
        clusters = cluster_errors(list(first.errors), self.profile)
        extra = max(0, int(getattr(self.cfg.loop, "verify_extra_llm_attempts", 0)))
        releasable = {BlockReason.REPEATED_FAILURE, BlockReason.OSCILLATION} if extra > 0 else set()
        covering = [self.blocked.covering(c) for c in clusters]
        if clusters and all(it is not None and it.reason not in releasable for it in covering):
            reasons = sorted({it.reason.value for it in covering if it is not None})
            pr.notes.append(f"{step.id}: failed with blocked cluster(s) only ({', '.join(reasons)}) → no final fix "
                            f"loop; see 'Blocked' in the report")
            return False
        releasable_ids = list(dict.fromkeys(it.id for it in covering if it is not None and it.reason in releasable))
        self.ws.journal({"event": "verify_loop_start", "step": step.id, "goal": pc.goal, "target": step.target,
                         "max_iterations": pc.max_iterations, "errors": first.error_count,
                         "clusters": [c.id for c in clusters], "releasable": releasable_ids,
                         "extra_llm_attempts": extra})
        log.info("verify: final build failed with %d error(s) → last build-fix loop (mvn %s, up to %d round(s)%s)",
                 first.error_count, pc.goal, pc.max_iterations,
                 f", {len(releasable_ids)} blocked cluster(s) eligible for release" if releasable_ids else "")
        self.loop.released_ids = []
        if self.loop.rewrite_runner is None:
            self.loop.rewrite_runner = self._rewrite_runner_or_none()
        res = self.loop.run(Phase.VERIFY, pc.goal, pc.max_iterations, target=step.target)
        pr.iterations += res.iterations
        pr.attempts += res.attempts
        pr.iteration_records += res.iteration_records
        pr.rewrite_results += res.rewrite_results
        pr.notes += [f"{step.id} loop: {n}" for n in res.notes]
        released = list(self.loop.released_ids)
        if released:        # a released item is no longer blocked: drop its row from the earlier phases as well
            phases = list(self.report.phases) if self.report is not None else []
            for ph in [*phases, pr]:
                ph.blocked = [b for b in ph.blocked if b.id not in released]
        self._merge_blocked(pr, res.blocked)
        self.ws.journal({"event": "verify_loop_end", "step": step.id, "build_ok": res.build_ok,
                         "attempts": len(res.attempts), "iterations": res.iterations, "released": released,
                         "blocked": [b.id for b in res.blocked]})
        return True

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
        todo: list[str] = []
        for res in pr.rewrite_results:
            for rid, files in (getattr(res, "recipes_applied", None) or {}).items():
                if rid in kb_ids and files and rid not in todo:
                    todo.append(rid)
        skip = None if pr.build_ok or not todo else Orchestrator._resolution_only_failure(pr)
        if skip:
            # the phase failed on dependency resolution (proxy / rate limit / missing artifact), not on the code
            # the recipes wrote → a failure credit would punish a candidate for something it did not cause
            log.info("phase %s failed on dependency resolution (%s): KB recipes not credited", pr.phase.value, skip)
            self.ws.journal({"event": "kb_credit_skipped", "phase": pr.phase.value, "recipes": todo, "reason": skip})
            return
        credited: list[str] = []
        for rid in todo:
            try:
                kb.credit_recipe(rid, bool(pr.build_ok), fp)
                credited.append(rid)
            except Exception as e:  # pragma: no cover
                log.warning("kb.credit_recipe(%s) failed: %s", rid, e)
        if credited:
            self.ws.journal({"event": "kb_credit", "phase": pr.phase.value, "recipes": credited, "success": pr.build_ok})

    _RESOLUTION_KINDS = frozenset({ErrorKind.DEPENDENCY_RESOLUTION, ErrorKind.PARENT_RESOLUTION,
                                   ErrorKind.PLUGIN_RESOLUTION})
    _RESOLUTION_BLOCKS = frozenset({"artifact_not_found", "parent_not_found"})

    @staticmethod
    def _resolution_only_failure(pr: PhaseResult) -> Optional[str]:
        """Why a failed phase failed on dependency resolution rather than on code (None = a code failure):
        `rewrite-unresolved-versions` was raised in it, or every remaining error is a resolution error (an
        OpenRewrite run that failed after its download retry counts as one), or — with no code error left — every
        blocked item is `artifact_not_found` / `parent_not_found`."""
        flag = Orchestrator.UNRESOLVED_VERSIONS_FLAG
        if any(flag in n for n in pr.notes):
            return flag
        code_errors = [e for e in pr.remaining_errors
                       if e.kind not in Orchestrator._RESOLUTION_KINDS
                       and not (e.kind == ErrorKind.REWRITE and (e.message or "").startswith("download-retry"))]
        if code_errors:
            return None
        if pr.remaining_errors:
            return "dependency-resolution"
        reasons = {getattr(b.reason, "value", b.reason) for b in pr.blocked}
        if reasons and reasons <= Orchestrator._RESOLUTION_BLOCKS:
            return "dependency-resolution"
        return None

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
        if self.cfg.kb.generate_recipe_candidates and not self.budget.llm_allowed():
            log.info("recipe-candidate generation skipped: LLM budget exhausted (%d/%d calls)",
                     self.budget.llm_calls, self.budget.max_llm_calls)
        elif self.cfg.kb.generate_recipe_candidates:
            try:
                llm = _BudgetedLLM(self.llm_client(), self.budget, sink=self._harvest_usage)
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

    # ------------------------------------------------------------------ error cases (2026-10-23)
    def _start_error_tracker(self, dry_run: bool) -> None:
        """One `ErrorTracker` per run, handed to the loop (docs/INTERFACES.md "Error cases"); none in dry-run or
        with `kb.error_cases: false`."""
        self.error_tracker = None
        if dry_run or self.loop is None or not getattr(self.cfg.kb, "error_cases", False):
            return
        try:
            from .error_tracker import ErrorTracker
            self.error_tracker = ErrorTracker(self.ws, self.cfg)
        except Exception as e:  # noqa: BLE001 - error cases are a by-product, never a reason to fail a run
            log.warning("error tracker unavailable: %s: %s", type(e).__name__, e)
            return
        self.loop.error_tracker = self.error_tracker

    def _note_step(self, step_id: str, phase: Phase) -> None:
        t = self.error_tracker
        if t is None:
            return
        try:
            t.note_step(step_id, phase)
        except Exception as e:  # noqa: BLE001
            log.warning("error tracker note_step(%s) failed: %s", step_id, e)

    def _observe_final(self, label: str, res: BuildResult, target: Optional[str]) -> None:
        t = self.error_tracker
        if t is None:
            return
        try:
            t.observe(Phase.VERIFY, label, res, [], void=bool(res.timed_out) or (not res.ok and not res.errors),
                      target=target)
        except Exception as e:  # noqa: BLE001
            log.warning("error tracker observe(%s) failed: %s", label, e)

    def _finalize_error_cases(self, report: RunReport) -> None:
        """Every build error of the run on its own record + every fix attempt (failures included, with the code it
        applied): `<run dir>/error_cases.jsonl` / `fix_attempts.jsonl` / `error_cases.csv`, the KB's append-only
        `errors.jsonl` / `fix_attempts.jsonl` (when `kb.harvest` and `kb.error_cases`), `report.error_cases`.
        Never fails the run: a problem becomes the flag `error-cases-failed: <msg>`."""
        t = self.error_tracker
        if t is None or self._dry_run:
            return

        def flag(text: str) -> None:
            if text not in report.flags:
                report.flags.append(text)

        try:
            errors, attempts = t.finalize(list(report.blocked), run_id=self.ws.run_id)
            prepare = write_jsonl = write_csv = None
            try:
                from sbmig.kb.error_cases import prepare_error_cases as prepare
                from sbmig.kb.error_cases import write_error_cases_csv as write_csv
                from sbmig.kb.error_cases import write_error_cases_jsonl as write_jsonl
            except ModuleNotFoundError as e:   # the KB part is not installed: raw records, no CSV, no KB
                if not (e.name or "").startswith("sbmig"):
                    raise
                log.warning("error cases: %s not available — writing the raw records only", e.name)
            if prepare is not None:
                errors, attempts = prepare(errors, attempts, self.profile, self.ws, self.cfg)
            out = report.outputs
            run_dir = self.ws.run_dir
            p_err, p_att = run_dir / "error_cases.jsonl", run_dir / "fix_attempts.jsonl"
            if write_jsonl is not None:
                out["error_cases"] = str(write_jsonl(errors, p_err))
                out["fix_attempts"] = str(write_jsonl(attempts, p_att))
            else:
                for path, recs in ((p_err, errors), (p_att, attempts)):
                    path.write_text("".join(r.model_dump_json() + "\n" for r in recs), encoding="utf-8")
                out["error_cases"], out["fix_attempts"] = str(p_err), str(p_att)
            if write_csv is not None:
                out["error_cases_csv"] = str(write_csv(errors, run_dir / "error_cases.csv"))
            report.error_cases = [r.model_copy(update={"diff_excerpt": ""}) for r in errors]
            by_status: dict[str, int] = {}
            for r in errors:
                by_status[r.status] = by_status.get(r.status, 0) + 1
            report.error_case_counts = {**by_status,
                                        "llm_text_replace": sum(1 for r in errors if r.replace_rule_candidate)}
            for p in list(getattr(t, "problems", None) or [])[:3]:
                flag(f"error-cases-failed: {p}")
            kb_n = None
            if prepare is not None and self.cfg.kb.harvest and self.cfg.kb.error_cases and self.profile is not None \
                    and self._kb is not None:          # unsanitized raw records never go into the shared KB
                append = getattr(self._kb, "append_error_cases", None)
                if callable(append):
                    try:
                        kb_n = append(errors, attempts)
                    except Exception as e:  # noqa: BLE001 - the KB store says it never raises; stay defensive
                        log.warning("kb.append_error_cases failed: %s", e)
                        flag(f"error-cases-failed: kb: {type(e).__name__}: {e}")
            self.ws.journal({"event": "error_cases", "count": len(errors), "by_status": by_status,
                             "attempts": len(attempts), "kb_appended": kb_n,
                             "llm_text_replace": report.error_case_counts["llm_text_replace"]})
            log.info("error cases: %d error(s) (%s), %d fix attempt(s)%s", len(errors),
                     ", ".join(f"{k} {v}" for k, v in by_status.items()) or "-", len(attempts),
                     f", {kb_n} appended to the KB" if kb_n is not None else "")
        except Exception as e:  # noqa: BLE001 - never fail the run over the error-case records
            log.exception("error cases failed")
            flag(f"error-cases-failed: {type(e).__name__}: {e}")

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
        test sources that only build with -Dmaven.test.skip=true, or tests that only fail at runtime (tests-skipped)."""
        items: list[BlockedItem] = []
        for it in [*self.blocked.items(), *(b for pr in report.phases for b in pr.blocked)]:
            if all(x.id != it.id for x in items):
                items.append(it)
        if report.success:
            still = {e.signature for e in unresolved}
            kept: list[BlockedItem] = []
            for it in items:
                tests_only = bool(it.errors) and all(e.kind == ErrorKind.TEST_COMPILE for e in it.errors)
                runtime_only = bool(it.errors) and all(e.kind == ErrorKind.TEST_FAILURE for e in it.errors)
                if (tests_only and "test-compile-skipped" in report.flags) or \
                        (runtime_only and "tests-skipped" in report.flags) or (set(it.signatures) & still):
                    kept.append(it)
                else:
                    self.ws.journal({"event": "blocked_resolved", "id": it.id, "reason": it.reason.value,
                                     "note": "not present in the green final build"})
            items = kept
        report.blocked = items

    def _resolve_javax_inventory(self) -> Optional[Callable]:
        if self._javax_inventory is False:
            return None
        if self._javax_inventory is not None:
            return self._javax_inventory
        if not self._real_maven:
            return None
        from sbmig.build.deptree import javax_api_inventory
        return javax_api_inventory

    def _finalize_javax_inventory(self, report: RunReport) -> None:
        """X1b (2026-10-22): after the final build, the javax EE API jars still on the resolved classpath and the
        direct dependency that drags each in (`mvn dependency:tree`) → report section, flag, follow-up, and the
        blocked X1 / X2 items get `via` + a suggestion naming that library. Best effort: never fails the run."""
        if not getattr(self.cfg.report, "javax_api_inventory", True) or self._dry_run:
            return
        if not any(pr.phase == Phase.SPRING for pr in report.phases):
            return                                   # no jakarta migration ran → nothing to look for
        fn = self._resolve_javax_inventory()
        if fn is None:
            return
        try:
            jars = list(fn(self.ws, self.cfg, self.profile) or [])
        except Exception as e:
            log.warning("javax API inventory failed: %s: %s", type(e).__name__, e)
            return
        report.javax_api_jars = jars
        try:
            from .blocked_registry import fill_via
            fill_via(self.blocked.items(), jars, self.cfg)
        except Exception as e:  # pragma: no cover - never lose the report
            log.warning("blocked via attribution failed: %s: %s", type(e).__name__, e)
        vias = list(dict.fromkeys(j.via.coordinates for j in jars if j.via is not None))
        self.ws.journal({"event": "javax_api_inventory", "jars": [j.artifact.coordinates for j in jars],
                         "via": vias})
        if not jars:
            return
        if "javax-api-on-classpath" not in report.flags:
            report.flags.append("javax-api-on-classpath")
        named = ", ".join(f"`{v}`" for v in vias[:3]) + (f" (+{len(vias) - 3} more)" if len(vias) > 3 else "")
        if not named:
            named = "declared directly in the poms (remove them)"
        fu = (f"{len(jars)} javax API jar(s) still on the classpath after the migration (see the report section) — "
              f"the libraries that drag them in need Jakarta builds: {named}")
        if fu not in report.manual_followups:
            report.manual_followups.append(fu)

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

    def _usage_by_model(self, report: RunReport, plan_usage: Optional[LLMUsage]) -> list[LLMUsage]:
        """Fix / escalation attempts (their own model), the plan advisor and the harvest recipe calls, per model."""
        by: dict[str, LLMUsage] = {}

        def acc(u: Optional[LLMUsage], fallback: str) -> None:
            if u is None or not (u.calls or u.input_tokens or u.output_tokens):
                return
            key = (u.model or fallback or "?").strip()
            by[key] = by.get(key, LLMUsage(model=key)).add(u.model_copy(update={"model": key}))
        for pr in report.phases:
            for a in pr.attempts:
                acc(a.llm_usage, a.model or self.cfg.llm.model)
        if plan_usage is not None:
            acc(plan_usage, self.cfg.llm.recipe_model or self.cfg.llm.model)
        for k, u in self._harvest_usage.items():
            acc(u, k or self.cfg.llm.recipe_model or self.cfg.llm.model)
        return list(by.values())

    def _infra_flags(self, report: RunReport) -> None:
        """Round 10 (A14, fleet circuit breaker): `infra:<kind>` when the run ended for an infrastructure reason —
        `infra:llm` (llm-unavailable / llm-config-error, and the run did not succeed), `infra:maven` (mvn-not-found),
        `infra:jdk` (jdk-not-21, or a Maven that cannot start because of JAVA_HOME), `infra:repository` (the
        configured parent is not resolvable, or every remaining error of the failed final build(s) is a dependency /
        parent / plugin resolution error whose text points at the repository or the network: could not / failed to
        transfer, connection refused / reset, timed out, unknown host, no route to host, PKIX, status / return code
        401 / 403 / 5xx, "was cached in the local repository")."""
        flags = report.flags
        add: list[str] = []
        if ("llm-unavailable" in flags or "llm-config-error" in flags) and not report.success:
            add.append("infra:llm")                 # a run that still passed did not end for that reason
        if "mvn-not-found" in flags:
            add.append("infra:jdk" if self._mvn_java_problem else "infra:maven")
        if "jdk-not-21" in flags:
            add.append("infra:jdk")
        if "aborted:parent-not-found" in flags or self._repository_trouble():
            add.append("infra:repository")
        if not report.success and self._disk_full():
            add.append("infra:disk")                # the fleet pauses instead of failing the next projects too
        for f in add:
            if f not in flags:
                flags.append(f)

    def _disk_full(self) -> bool:
        return any(_INFRA_DISK_RE.search(f"{e.message}\n{e.raw}") for r in self._verify_results if not r.ok
                   for e in r.errors)

    def _repository_trouble(self) -> bool:
        failed = [r for r in self._verify_results if not r.ok]
        errs = [e for r in failed for e in r.errors]
        if not failed or not errs:
            return False
        return all(e.kind in self._RESOLUTION_KINDS and _INFRA_REPO_RE.search(f"{e.message}\n{e.raw}")
                   for e in errs)

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
        report.llm_usage_by_model = self._usage_by_model(report, plan_usage)
        try:
            from sbmig.report.cost import compute_llm_cost
            report.llm_cost = compute_llm_cost(report.llm_usage_by_model, self.cfg)
        except Exception as e:  # pragma: no cover - pricing must never fail a run
            log.warning("llm cost not computed: %s", e)
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
        if getattr(self.cfg.build, "skip_test_compile", False) and not self._dry_run:
            self._flag("tests-not-compiled")
            fu = ("test sources were neither compiled nor run (build.skip_test_compile: true): the owning team "
                  "compiles and fixes the tests (`mvn test-compile`) before relying on CI")
            if fu not in report.manual_followups:
                report.manual_followups.append(fu)
        # round 10 (invariant 1, A7): an aborted run is never a success, and every planned final build must have
        # run — an MSA run whose svc-a verify passed before svc-b's step crashed used to say BUILD SUCCESS
        report.success = bool(self._verify_results) and all(r.ok for r in self._verify_results) \
            and "restructure-failed" not in report.flags and not _aborted(report.flags) and not self._verify_missing()
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
        if "tests-skipped" in report.flags:
            for pr in report.phases:
                if BuildFixLoop.tests_skipped_in(pr):
                    unresolved += [e for e in pr.remaining_errors if e not in unresolved]
        if report.properties is None and self.profile is not None:
            report.properties = getattr(self.profile, "properties", None)
        self._finalize_javax_inventory(report)
        try:
            self._finalize_blocked(report, unresolved)
        except Exception as e:  # never lose the report
            log.exception("finalizing the blocked items failed")
            report.flags.append(f"blocked-finalize-failed: {e}")
        report.unresolved = [e for e in unresolved if not self.blocked.is_blocked_error(e)]
        self._infra_flags(report)
        out = report.outputs
        out["run_dir"] = str(self.ws.run_dir)
        out["project_dir"] = str(self.ws.root)
        out["journal"] = str(self.ws.journal_path)
        out["logs"] = str(self.ws.logs_dir)
        for name in ("profile.json", "plan.json"):
            if (self.ws.run_dir / name).exists():
                out[name.replace(".json", "")] = str(self.ws.run_dir / name)
        # round 10 (A13): the KB records and the report come first, the slow patch / zip last — a kill while zipping
        # a 1 GB tree loses nothing that matters; their paths are announced up front and dropped again on failure
        want_diff = bool(self.cfg.report.include_diff and (self.ws.root / ".git").exists())
        want_zip = bool(self.cfg.report.zip_result and not self._dry_run)
        if want_diff:
            out["diff"] = str(self.ws.run_dir / "changes.patch")
        if want_zip:
            out["zip"] = str(self.ws.run_dir / "result.zip")
        if "md" in self.cfg.report.formats:
            out["report_md"] = str(self.ws.run_dir / "report.md")
        if "json" in self.cfg.report.formats:
            out["report_json"] = str(self.ws.run_dir / "report.json")
        self._finalize_advisories(report)
        self._finalize_error_cases(report)
        self._record_run_ledger(report)
        writer = self._report_writer
        if writer is None:
            from sbmig.report import write_report
            writer = lambda r, w: write_report(r, w, cfg=self.cfg)      # noqa: E731 - report.language from the config

        def write() -> None:
            try:
                paths = writer(report, self.ws) or {}
                out.update({k: v for k, v in paths.items()})
            except Exception as e:
                log.exception("report writing failed")
                report.flags.append(f"report-failed: {e}")
        write()
        dropped: list[str] = []
        if want_diff:
            try:
                out["diff"] = str(self.ws.write_patch())
            except Exception as e:
                log.warning("could not write changes.patch: %s", e)
                dropped.append(str(out.pop("diff", "")))
        if want_zip:
            try:
                out["zip"] = str(self.ws.zip_result())
            except Exception as e:
                log.warning("could not write result.zip: %s", e)
                dropped.append(str(out.pop("zip", "")))
        if dropped:                        # the report named a file that does not exist: write it once more
            write()
        self.ws.journal({"event": "report", "success": report.success, "flags": report.flags,
                         "unresolved": len(report.unresolved), "blocked": len(report.blocked),
                         "llm_calls": report.llm_usage.calls,
                         "outputs": out})
