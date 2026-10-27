"""Orchestrator: intake → analyze → plan → phases → verify → report.

v2.1 (2026-10-27, docs/v2/PLAN-v2.1.md §3): transform (every OpenRewrite recipe in ONE run, `transform.rewrite`) →
`transform.compile` (`mvn clean compile` once, no fixing: every error goes on the run's error list, seen `compile`)
→ restructure (the company layout; every pom correction it reports — parents set, mandatory added, optional bumped,
managed versions dropped, generated poms — is an entry with status `pom-verify`) → structure review (`StepKind.
REVIEW`: the LLM checks poms / dependencies / modules and fixes critical issues with edits — committed `review: …`;
warnings become `note` entries, a fixed critical issue an entry resolved by `review`, its script is stored in the KB
case `review|structure`) → verify: `mvn clean package` (only NEW errors are added, the pom-verify entries are
verified); when it fails, the fix loop (`loop.phases.verify`, ≤ 50 rounds) works on the open entries case by case and
the final build runs again → finalize: `end_of_run`, `errors.jsonl` / `errors.md`, `report.error_entries`, the KB
gets the run's entries (`kb.record_run`). Every log line carries its stage (`sbmig.core.stage`).

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
from pathlib import PurePosixPath
from typing import Any, Callable, Iterator, Optional

from sbmig.core.config import Config
from sbmig.core.models import (GAV, BlockedItem, BuildError, BuildResult, CaseType, DecisionKind, ErrorKind,
                               IterationRecord, LLMUsage, MigrationPlan, Phase, PhaseResult, PlanStep, ProjectProfile,
                               RunReport, StepKind, utcnow)
from sbmig.core.stage import stage
from sbmig.core.workspace import Workspace

from .blocked_registry import BlockedRegistry, default_locator
from .budget import LoopBudget
from .components import LazyFixer, LazyScriptRunner, MissingComponentError, NullKnowledgeBase, lazy_attr
from .entries import REVIEW_L2, CaseScripts, RunErrors
from .fixloop import NOTES_LOGGED, BuildFixLoop, _restore_build_byproducts, _status_paths, refine_build
from .notes import (answer_notes, fallback_guide, log_major_upgrades, note_line, operator_changes, pom_changed,
                    try_notes)

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
COMPILE_GOAL = "clean compile"            # transform.compile: OpenRewrite's result compiled once, before the restructure


def step_stage(step: PlanStep) -> str:
    """The log stage of a plan step (§3.2): rewrite / compile / restructure / review / build / fix."""
    k = step.kind
    if k == StepKind.OPENREWRITE:
        return "rewrite"
    if k == StepKind.BUILD:
        return "build" if step.phase == Phase.VERIFY else "compile"
    if k in (StepKind.RESTRUCTURE, StepKind.MSA_POM):
        return "restructure"
    if k == StepKind.REVIEW:
        return "review"
    return "fix" if k == StepKind.BUILD_FIX_LOOP else step.phase.value
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


_PHASE_ORDER = [Phase.INTAKE, Phase.ANALYZE, Phase.PLAN, Phase.TRANSFORM, Phase.JDK21, Phase.SPRING,
                Phase.RESTRUCTURE, Phase.VERIFY, Phase.REPORT]
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
    """An LLM client whose calls are charged to the run's `LoopBudget` (v2: the structure review goes through the same
    budget as the fix loop; per-model usage lands in `sink` for the cost table)."""

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
                 planner: Optional[Callable] = None,
                 restructurer: Any = None, rewrite_runner: Any = None, build_runner: Any = None,
                 triager: Any = None, fixer: Any = None, kb: Any = None, llm: Any = None,
                 report_writer: Optional[Callable] = None, clock: Callable[[], float] = time.monotonic,
                 locator: Any = None, effective_capture: Optional[Callable] = None,
                 javax_inventory: Any = None, script_runner: Any = None, reviewer: Any = None,
                 error_list: Optional[Callable[..., Any]] = None, edit_applier: Optional[Callable[..., Any]] = None):
        """v2 collaborators: `script_runner` (ScriptRunnerLike; None = `sbmig.script.ScriptRunner(cfg)` on first use)
        and `reviewer` (a StructureReviewerLike object, or a factory `fn(cfg, llm, runner, kb)`; None =
        `sbmig.review.StructureReviewer`). v2.1: `error_list` = `fn(ws, cfg, project)` → the run's error list (None =
        `sbmig.errors.entries.ErrorList.for_workspace`), `edit_applier` = `fn(ws, edits, *, cfg) -> ScriptRunResult`
        for the LLM's direct edits (None = `sbmig.script.apply_edits`)."""
        self.cfg = cfg
        self.ws = ws
        self.clock = clock
        self._analyzer = analyzer
        self._planner = planner
        self._script_runner = script_runner
        self._reviewer = reviewer
        self._error_list_factory = error_list
        self._edit_applier = edit_applier
        self._restructurer = restructurer
        self._rewrite_runner = rewrite_runner
        self._build_runner = build_runner
        self._triager = triager
        self._fixer = fixer
        self._kb = kb
        self._llm = llm
        self._report_writer = report_writer
        self.budget = LoopBudget.from_config(cfg, clock=clock)
        self.report: Optional[RunReport] = None
        self.profile: Optional[ProjectProfile] = None
        self.plan: Optional[MigrationPlan] = None
        self.loop: Optional[BuildFixLoop] = None
        self.errors = RunErrors(None)               # v2.1: the run's error list (errors.jsonl), set in step_plan
        self._verify_results: list[BuildResult] = []
        self._review_usage: dict[str, LLMUsage] = {}       # v2: structure-review calls, by model
        self._review_commit_pre: str | None = None          # v2.1d: HEAD before the review commit (read-back)
        self._review_guide_source = ""                     # v2.1d: "llm" | "sbmig" — who wrote the review's guide
        self._restructure_results: list[Any] = []          # v2: this run's RestructureResults (structure review input)
        self._restructure_notes: list[str] = []
        self._phase_failed = False
        self._dry_run = False
        self._pending_notes: list[str] = []
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
    def script_runner(self) -> Any:
        """v2: the fix-script runner shared by the fix loop and the structure review (lazy `sbmig.script`)."""
        if self._script_runner is None:
            self._script_runner = LazyScriptRunner(
                lambda: lazy_attr("sbmig.script", "ScriptRunner", "S (script)")(self.cfg))
        return self._script_runner

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
    def triager(self) -> Any:
        if self._triager is None:
            from sbmig.triage import Triager
            self._triager = Triager(self.cfg)
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
        because of a bug), and `_finalize` (report, KB records, outputs) runs no matter what. Used by `run()` for both
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
        self._review_usage = {}
        self._restructure_results = []
        self._restructure_notes = []
        self._mvn_java_problem = self._jdk_problem = ""
        self._after_abort = False
        self._phase_failed = False
        self._pending_notes = []
        self.loop = None
        self.errors = RunErrors(None)
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
            with stage("build"):
                self._final_build_after_abort(report)
        except Exception as e:  # never lose the report because of a bug
            log.exception("run failed")
            self._abort(report, f"internal error: {type(e).__name__}: {e}")
            with stage("build"):
                self._final_build_after_abort(report)
        finally:
            self._running = False
            report.timings_s["total"] = round(self.clock() - t0, 3)
            with stage("report"):
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
        with stage("intake"):
            return self._step_intake(report, stop_after)

    def _step_intake(self, report: RunReport, stop_after: Phase | None) -> bool:
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
        log.info("%s", msg)
        self.ws.journal({"event": "aggregator_generated", "path": path, "artifact_id": plan.artifact_id,
                         "name": plan.name, "modules": plan.modules, "excluded": plan.excluded})
        return True

    def step_analyze(self, report: RunReport, stop_after: Phase | None) -> bool:
        """2. analyze: profile + library advisories. False = stop or abort (not Maven, no pom.xml)."""
        with stage("analyze"):
            return self._step_analyze(report, stop_after)

    def _step_analyze(self, report: RunReport, stop_after: Phase | None) -> bool:
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
        """3. plan: the migration plan, the run's error list and `BuildFixLoop`, and the phase groups (consecutive
        plan steps of the same phase; v2.1: transform (rewrite + compile) → restructure (+ review) → verify; MSA
        plans may repeat a phase per target). None = the run stops after `plan`."""
        with stage("plan"):
            return self._step_plan(report, stop_after, dry_run)

    def _step_plan(self, report: RunReport, stop_after: Phase | None,
                   dry_run: bool) -> list[tuple[Phase, list[tuple[int, PlanStep]]]] | None:
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
        self.ws.journal({"event": "plan", "steps": [s.id for s in plan.steps], "notes": plan.notes})
        for n in plan.notes:
            if n.startswith("followup:"):
                report.manual_followups.append(n[len("followup:"):].strip())
        self._timed("plan", t)
        if self._stop(stop_after, Phase.PLAN):
            return None
        # 4. phases ------------------------------------------------------------
        self._start_error_list(dry_run)
        self.loop = BuildFixLoop(self.cfg, self.ws, self.build_runner, self.triager, self.fixer, self.kb, profile,
                                 plan_notes=plan.notes,
                                 budget=self.budget, dry_run=dry_run, blocked=self.blocked,
                                 locator=self._resolve_locator(), advisories=list(report.advisories or []),
                                 script_runner=self.script_runner, edit_applier=self._edit_applier,
                                 errors=self.errors)
        steps = list(plan.steps)
        if self.cfg.loop.always_final_verify and not any(s.kind == StepKind.BUILD and s.phase == Phase.VERIFY
                                                         for s in steps):
            steps.append(PlanStep(id="verify.build", kind=StepKind.BUILD, phase=Phase.VERIFY,
                                  title="final mvn clean package", build_goal="clean package",
                                  reason="always_final_verify"))
        self._run_steps = list(steps)
        groups: list[tuple[Phase, list[tuple[int, PlanStep]]]] = []
        for i, s in enumerate(steps):
            if groups and groups[-1][0] == s.phase:
                groups[-1][1].append((i, s))
            else:
                groups.append((s.phase, [(i, s)]))
        return groups

    def step_phase(self, report: RunReport, phase: Phase, items: list[tuple[int, PlanStep]],
                   stop_after: Phase | None) -> PhaseResult | None:
        """One phase group: run its steps. Returns the `PhaseResult` (also appended to `report.phases`), or None when
        the group lies after `stop_after` (``phase_skipped``). Raises `_RunAborted` when the fix loop aborted the run."""
        if stop_after is not None and _PHASE_ORDER.index(phase) > _PHASE_ORDER.index(stop_after):
            # MSA plans repeat jdk21/spring per service: skip only the phases after `stop_after`
            self.ws.journal({"event": "phase_skipped", "phase": phase.value, "reason": f"stop_after={stop_after.value}"})
            return None
        t = self.clock()
        pr = PhaseResult(phase=phase, step_ids=[s.id for _, s in items], ok=True)
        self.ws.journal({"event": "phase_start", "phase": phase.value, "steps": pr.step_ids})
        try:
            with stage(phase.value):
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
        self._timed(phase.value, t)
        return pr

    def step_finish(self, report: RunReport, stop_after: Phase | None) -> None:
        """After the last phase group: record `stop_after` when it named a phase that the groups ran."""
        if stop_after is not None and stop_after != Phase.VERIFY:
            self._stop(stop_after, stop_after)

    def _resolve_locator(self) -> Any:
        if self._locator is not None:
            return self._locator or None
        return default_locator() if self._real_maven else None

    # ------------------------------------------------------------------ library advisories (analyze / plan side)
    def _evaluate_advisories(self, profile) -> list:
        """knowledge/libraries/advisories.yml findings for this project (springfox, hystrix, sleuth …). Never
        raises; an unavailable advisory package just means no findings. The finalize side (verify → outcome,
        handover, follow-ups) lives in `_finalize`."""
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
                             "findings": [{"id": f.advisory_id, "policy": f.policy} for f in findings]})
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
            with stage(step_stage(step)):
                ok, aborted = self._run_step(pr, step, aborted)
            self.ws.journal({"event": "step_end", "step": step.id, "ok": bool(ok)})
            if self.loop is not None and self.loop.aborted:
                pr.ok = False
                break
            loop_ran = loop_ran or step.kind == StepKind.BUILD_FIX_LOOP
        if pr.phase == Phase.VERIFY:
            pr.ok = pr.build_ok = bool(self._verify_results) and all(r.ok for r in self._verify_results)
        elif not loop_ran and pr.ok:
            pr.build_ok = False

    def _run_step(self, pr: PhaseResult, step: PlanStep, aborted: bool) -> tuple[Any, bool]:
        """One plan step (inside its log stage). Returns (ok, the phase is aborted)."""
        ok: Any = True
        if step.kind == StepKind.OPENREWRITE:
            ok = self._step_rewrite(pr, step)
            if ok is None:          # missing recipe → programming error
                aborted, ok = True, False
                pr.ok = False
        elif step.kind == StepKind.REVIEW:
            ok = self._step_review(pr, step)
        elif step.kind == StepKind.BUILD_FIX_LOOP:
            # an intermediate fix loop (the v2 planner plans none; a hand-made plan may)
            ok = self._step_loop(pr, step)
            pr.ok = ok
        elif step.kind in (StepKind.RESTRUCTURE, StepKind.MSA_POM):
            ok = self._step_restructure(pr, step)
            if not ok and self._step_mode(step) in NON_FATAL_RESTRUCTURE_MODES:
                pr.notes.append(f"{step.id}: failed — continuing with the rest of the {step.phase.value} phase")
            elif not ok:
                aborted = True
                pr.ok = False
        elif step.kind == StepKind.BUILD and step.phase != Phase.VERIFY:
            ok = self._step_compile(pr, step)
        elif step.kind == StepKind.BUILD:
            ok = self._step_verify(pr, step)
        return ok, aborted

    def _flush_notes(self, pr: PhaseResult) -> None:
        if self._pending_notes:
            pr.notes.extend(self._pending_notes)
            self._pending_notes = []

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
        sha = None
        if not self._dry_run and self.ws.is_dirty():
            sha = self.ws.commit(f"rewrite: {step.id}")
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
        kwargs = dict(dry_run=self._dry_run, label=step.id)
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
    # (Spring Data, Hibernate, method renames) — the fix scripts cover what the build finds; say so in the report
    PARSE_PROBLEMS_FLAG = "rewrite-parse-problems"

    def _report_parse_problems(self, pr: PhaseResult, step: PlanStep, res: Any) -> None:
        files = [str(f) for f in (getattr(res, "parse_problems", None) or [])]
        self.ws.journal({"event": "rewrite_parse_problems", "step": step.id, "files": files,
                         "log": getattr(res, "log_path", None)})
        self._flag(self.PARSE_PROBLEMS_FLAG)
        shown = ", ".join(files[:5]) + (f", … (+{len(files) - 5})" if len(files) > 5 else "")
        pr.notes.append(f"{step.id}: OpenRewrite could not parse {len(files)} source file(s) ({shown}) — recipes "
                        "that need type information skipped them; the fix scripts cover what the build finds")
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

    def _step_compile(self, pr: PhaseResult, step: PlanStep) -> bool:
        """v2.1 `transform.compile` (StepKind.BUILD outside the verify phase): `mvn clean compile` once on
        OpenRewrite's result — no fixing; every error goes on the error list (seen `compile`) and the restructure /
        review / build that follow work from it. Never fails the phase (a failed compile is the expected input)."""
        self._flush_notes(pr)
        if self._dry_run:
            pr.notes.append(f"{step.id}: dry-run → compile skipped")
            return True
        if self.budget.time_exceeded():
            pr.notes.append(f"{step.id} skipped: run time budget exhausted")
            self._flag("time-budget-exhausted")
            return True
        from .fixloop import build_target_args
        module, extra = build_target_args(self.profile, step.target)
        kw: dict[str, Any] = {"extra_args": extra} if extra else {}
        goal = step.build_goal or COMPILE_GOAL
        label = "compile" + (f"-{step.params['service']}" if step.params.get("service") else "")
        t = self.clock()
        before = _status_paths(self.ws)
        try:
            res = refine_build(self.build_runner.run(goal, module=module, label=label,
                                                     skip_test_compile=bool(self.cfg.build.skip_test_compile), **kw))
            _restore_build_byproducts(self.ws, before)
        except Exception as e:  # noqa: BLE001 - a recording step: the verify build shows the real state
            log.warning("mvn %s failed to run: %s: %s — the error list starts at the first build", goal,
                        type(e).__name__, e)
            pr.notes.append(f"{step.id}: mvn {goal} could not run ({type(e).__name__}: {e})")
            return True
        finally:
            self._timed("compile", t)
        added = self.errors.add_build(res, "compile", void=bool(res.timed_out), scope=step.target)
        pr.iterations += 1
        pr.iteration_records.append(IterationRecord(phase=step.phase, iteration=pr.iterations, build_ok=res.ok,
                                                    error_count=res.error_count,
                                                    signatures=res.signature_multiset(), build_log=res.log_path))
        if res.ok:
            line = f"mvn {goal}: BUILD SUCCESS → 목록 0건"
        else:
            line = (f"mvn {goal}: {res.error_count} error(s) → 목록에 {added}건 (seen=compile)"
                    + (" — timed out" if res.timed_out else ""))
        log.info("%s", line)
        pr.notes.append(f"{step.id}: {line}")
        self.ws.journal({"event": "compile", "step": step.id, "goal": goal, "target": step.target, "ok": res.ok,
                         "errors": res.error_count, "added": added, "log": res.log_path})
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
            self._follow_moves(step, pre, sha)
        self._pom_verify_entries(res)
        self.report.manual_followups += [f for f in res.manual_followups if f not in self.report.manual_followups]
        for flag in list(getattr(res, "flags", None) or []):      # 2026-10-23: e.g. web-xml-registered
            self._flag(flag)
        pr.notes += res.notes
        self._restructure_results.append(res)            # v2: the structure review's input
        self._restructure_notes += [f"{step.id}: {n}" for n in res.notes]
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
        self._restructure_notes.append(summary)
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

    # ------------------------------------------------------------------ v2: structure review
    def _make_reviewer(self) -> tuple[Any, str]:
        """(reviewer, why-not) — the injected one, or `sbmig.review.StructureReviewer(cfg, <budgeted LLM>,
        <script runner>, kb)`; (None, reason) when it cannot be built (the review never stops a run)."""
        llm, why = None, ""
        if self.budget.llm_allowed():
            try:
                llm = _BudgetedLLM(self.llm_client(), self.budget, sink=self._review_usage)
            except Exception as e:  # noqa: BLE001 - no LLM: the reviewer says "skipped"
                why = f"no LLM client: {type(e).__name__}: {e}"
                log.info("structure review: %s", why)
        else:
            why = f"LLM budget exhausted ({self.budget.llm_text()})"
        made = self._reviewer
        try:
            if made is not None and not isinstance(made, type) and callable(getattr(made, "review", None)):
                return made, why                    # an instance (a class is a factory, like StructureReviewer)
            factory = made if callable(made) else lazy_attr("sbmig.review", "StructureReviewer", "R (review)")
            # kb=None: the orchestrator records the review (error list + the KB case of its script) after the commit
            return factory(self.cfg, llm, self.script_runner, None), why
        except Exception as e:  # noqa: BLE001 - ImportError / a broken reviewer: noted, the run goes on
            return None, f"structure reviewer unavailable: {type(e).__name__}: {e}"

    def _step_review(self, pr: PhaseResult, step: PlanStep) -> bool:
        """v2 `restructure.review` (StepKind.REVIEW): the LLM checks poms / dependencies / module layout of the
        restructured tree; the script it writes for critical issues runs uncommitted in the reviewer and is committed
        here as `review: fix critical structure issues (<n>)` (revertible). Never stops a run: a failure is a note
        and verdict `failed`; a dry run is verdict `skipped` without an LLM call."""
        from sbmig.core.models import StructureReview
        self._flush_notes(pr)
        assert self.report is not None
        t = self.clock()
        base = dict(id=f"{self.ws.run_id}:review", run_id=self.ws.run_id,
                    project=(self.profile.name if self.profile is not None and self.profile.name else "")
                    or self.report.project_name or self.ws.root.name,
                    case_type=self.profile.case_type if self.profile is not None else CaseType.UNKNOWN)
        rv: Optional[StructureReview] = None
        pre: Optional[str] = None
        if not self._dry_run:
            try:
                pre = self.ws.head()
            except Exception:  # noqa: BLE001 - no git: no diff for the review's entries
                pre = None
        if self._dry_run:
            rv = StructureReview(**base, verdict="skipped", note="dry run: no LLM call, nothing changed")
        elif not getattr(getattr(self.cfg, "review", None), "enabled", True):
            rv = StructureReview(**base, verdict="skipped", note="review.enabled is off")
        else:
            reviewer, why = self._make_reviewer()
            if reviewer is None:
                rv = StructureReview(**base, verdict="failed", note=why)
            else:
                try:
                    rv = reviewer.review(self.ws, self.profile, self.cfg, run_id=self.ws.run_id,
                                         restructure_notes=list(self._restructure_notes),
                                         restructure_results=list(self._restructure_results))
                except Exception as e:  # noqa: BLE001 - the review never stops a run
                    log.warning("structure review failed: %s: %s", type(e).__name__, e)
                    rv = StructureReview(**base, verdict="failed", note=f"review error: {type(e).__name__}: {e}")
                if rv is None:
                    rv = StructureReview(**base, verdict="failed", note="the reviewer returned nothing")
                if why and rv.verdict == "skipped" and not rv.note:
                    rv.note = why
        self._review_llm_usage(rv)
        sha = self._commit_review(pr, step, rv)
        if sha:
            self._review_major_upgrades(rv, self._review_commit_pre or pre, sha)
        self._review_entries(rv, pre, sha)
        self.report.structure_review = rv
        critical = len(rv.critical)
        status = rv.script_result.status if rv.script_result is not None else None
        self.ws.journal({"event": "structure_review", "step": step.id, "verdict": rv.verdict,
                         "issues": len(rv.issues), "critical": critical, "script_status": status, "commit": sha,
                         "note": (rv.note or "")[:300]})
        ans = getattr(rv, "answer", None)
        how = f"edits ({len(ans.edits)})" if ans is not None and ans.edits else "script"   # v2.1: what the fix was
        line = (f"{step.id}: structure review {rv.verdict} ({len(rv.issues)} issue(s), {critical} critical)"
                + (f"; {how} {status}" if status else "") + (f" → commit {sha[:10]}" if sha else "")
                + (f" — {rv.note}" if rv.note else ""))
        pr.notes.append(line[:600])
        log.info("%s", line[:600])
        self._timed("review", t)
        return True

    def _commit_review(self, pr: PhaseResult, step: PlanStep, rv: Any) -> Optional[str]:
        """Commit the review script's changes (`review.commit`); changes without a successful script run are
        discarded (a reviewer must leave the tree as it found it when its script failed)."""
        self._review_commit_pre = None
        if self._dry_run:
            return None
        try:
            if not self.ws.is_dirty():
                return None
            if rv.script_result is not None and rv.script_result.status == "ok":
                pre = self.ws.head()
                self._review_commit_pre = pre
                sha = self.ws.commit(f"review: fix critical structure issues ({len(rv.critical)})",
                                     paths=list(rv.script_result.changed_files or []) or None)
                if sha == pre:
                    return None
                rv.commit = sha
                # the reviewer's note was written before this commit ("… — not committed yet"): say what happened
                note = rv.note or ""
                rv.note = (note.replace(" — not committed yet", f" — committed {sha[:10]}")
                           if " — not committed yet" in note else note)
                return sha
            self.ws.discard_uncommitted()
            pr.notes.append(f"{step.id}: the structure review left uncommitted changes without a successful script "
                            "run — discarded")
        except Exception as e:  # noqa: BLE001 - git trouble: the build shows the tree as it is
            log.warning("structure review commit failed: %s: %s", type(e).__name__, e)
            pr.notes.append(f"{step.id}: committing the review script failed: {type(e).__name__}: {e}")
        return None

    def _review_entries(self, rv: Any, pre: Optional[str], sha: Optional[str]) -> None:
        """v2.1: the review on the error list and in the KB (never raises, nothing for a skipped review / dry run):
        a `note` entry per warning — and per critical issue no committed fix addressed —, an entry resolved by
        `review` per critical issue the committed fix addressed (one try: source review, 원인 / 해결 / edits /
        diff / commit), and the answer's script stored in the KB case `review|structure` (source review). v2.1b:
        the committed fix's 수정 notes (the edits' `desc` / the answer's `<changes>` + the diff's lines,
        `sbmig.loop.notes`) go on every such try and are logged once, one `수정: …` INFO line per note."""
        if self._dry_run or rv is None or rv.verdict == "skipped":
            return
        try:
            ans = getattr(rv, "answer", None)
            diff, files = "", []
            if sha and pre:
                try:
                    diff = self.ws.diff(pre, sha)
                    files = self.ws.changed_between(pre, sha)
                except Exception as e:  # noqa: BLE001 - the commit is there; only its text is missing
                    log.debug("review diff unavailable: %s", e)
            n_edits = len(getattr(ans, "edits", None) or []) if ans is not None else 0
            applied = ("edits" if n_edits else "script") if sha else "none"
            notes: list = []
            if sha:
                notes = try_notes(answer_notes(ans, edits=bool(n_edits)), diff)
                for nt in notes[:NOTES_LOGGED]:
                    log.info("수정: %s", note_line(nt))
                if len(notes) > NOTES_LOGGED:
                    log.info("수정: … %d more (errors.md)", len(notes) - NOTES_LOGGED)
            for issue in rv.issues:
                text = " ".join((issue.problem or "").split()) + (f" — {issue.fix_hint}" if issue.fix_hint else "")
                if issue.severity == "critical" and sha:
                    self.errors.review_fixed(
                        issue.file or "", f"critical: {text}",
                        analysis=(getattr(ans, "analysis", "") if ans is not None else "") or rv.analysis,
                        solution=getattr(ans, "solution", "") if ans is not None else "",
                        no_script_reason=getattr(ans, "no_script_reason", "") if ans is not None else "",
                        applied=applied, files=files, edits=list(notes), diff=diff, commit=sha, result="improved",
                        note=f"fixed by the structure review ({n_edits} edit(s), commit {sha[:10]})",
                        llm_calls=int(rv.llm_usage.calls or 0), seconds=0.0,
                        major_upgrades=list(getattr(rv, "major_upgrades", None) or []),
                        verify=(getattr(ans, "verify", "") if ans is not None else "") or "",
                        verify_source=(self._review_guide_source or ("llm" if ans is not None and ans.verify
                                                                      else "")) if getattr(rv, "major_upgrades", None)
                        else "")
                elif issue.severity == "critical":
                    self.errors.add_note(issue.file or "", f"critical (not fixed by the review): {text}", "review")
                else:
                    self.errors.add_note(issue.file or "", f"warning: {text}", "review")
            code = ((getattr(ans, "code", "") if ans is not None else "") or rv.script or "").strip("\n")
            store = CaseScripts.of(self.kb)
            if code.strip() and rv.critical and store.active:
                from sbmig.triage.cluster import l2_case_id
                cs = store.add_script(l2_case_id(REVIEW_L2), REVIEW_L2, code, source="review", run_id=self.ws.run_id,
                                      project=(self.profile.name if self.profile is not None else "") or
                                      self.ws.root.name,
                                      analysis=(getattr(ans, "analysis", "") if ans is not None else "") or rv.analysis,
                                      solution=getattr(ans, "solution", "") if ans is not None else "")
                if cs is not None:
                    self.ws.journal({"event": "review_script_stored", "case": l2_case_id(REVIEW_L2),
                                     "version": getattr(cs, "version", 0), "status": getattr(cs, "status", "")})
        except Exception as e:  # noqa: BLE001 - recording never stops a run
            log.warning("recording the structure review failed: %s: %s", type(e).__name__, e)

    def _review_major_upgrades(self, rv: Any, pre: str | None, sha: str) -> None:
        """v2.1d: the review commit's major upgrades / library replacements (`sbmig.build.depchanges`) →
        `rv.major_upgrades` with the 운영자 확인 guide (the answer's `<verify>`, else sbmig's synthesized one, stored
        into `rv.answer.verify` — or the note without an answer). Same WARNING / INFO lines as the fix loop (stage
        review). Never raises; nothing is reverted for it."""
        try:
            files: list[str] = []
            try:
                files = list(self.ws.changed_between(pre, sha)) if pre else []
            except Exception:  # noqa: BLE001 - let the read-back look at every pom of the commit
                files = ["pom.xml"]
            if not pom_changed(files):
                return
            changes = operator_changes(self.ws, pre, sha, self.cfg)
            if not changes:
                return
            rv.major_upgrades = changes
            ans = getattr(rv, "answer", None)
            guide = str(getattr(ans, "verify", "") or "").strip() if ans is not None else ""
            source = "llm" if guide else "sbmig"
            if not guide:
                lang = str(getattr(self.cfg.report, "language", "") or "ko").lower()
                guide = fallback_guide(changes, lang if lang in ("en", "ko") else "ko")
                if ans is not None:
                    ans.verify = guide
                else:
                    rv.note = (f"{rv.note}; " if rv.note else "") + "운영자 확인: " + " / ".join(
                        x.strip() for x in guide.splitlines() if x.strip())[:600]
            self._review_guide_source = source
            log_major_upgrades(changes, guide, self.cfg)
            for ch in changes:
                self.ws.journal({"event": "major_upgrade", "step": "review", "change": ch.model_dump(mode="json"),
                                 "guide_source": source})
        except Exception as e:  # noqa: BLE001 - a report aid, never a reason to fail the review
            log.debug("review read-back failed: %s: %s", type(e).__name__, e)

    def _review_llm_usage(self, rv: Any) -> None:
        """The review's LLM calls count toward the run's usage and budget: through the budgeted client (`_review_usage`)
        or — a reviewer with its own client — from `StructureReview.llm_usage`."""
        u = getattr(rv, "llm_usage", None)
        if self._review_usage or u is None or not u.calls:
            return
        self.budget.charge_llm(u.calls)
        key = u.model or self.cfg.llm.model or "?"
        self._review_usage[key] = self._review_usage.get(key, LLMUsage(model=key)).add(u)

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

    _MANAGED_POM_RE = re.compile(r"\(([^()]*pom\.xml)\)\s*$")

    def _pom_verify_entries(self, res: Any) -> None:
        """v2.1: every pom correction the restructure reports → one error-list entry, status `pom-verify` (the next
        build verifies it): parents set (`<pom>: old → new`), mandatory artifacts added and optional ones bumped (the
        root pom), explicit versions dropped for the managed ones (`… (<pom>)`), generated poms."""
        root = "pom.xml"
        items: list[tuple[str, str]] = []
        for e in list(getattr(res, "parents_set", None) or []):
            pom, sep, what = str(e).partition(": ")
            items.append((pom if sep else root, f"parent {what if sep else e}"))
        for k in list(getattr(res, "mandatory_added", None) or []):
            items.append((root, f"mandatory {k} added"))
        for k in list(getattr(res, "optional_bumped", None) or []):
            items.append((root, f"optional {k}"))
        for d in list(getattr(res, "managed_versions_dropped", None) or []):
            m = self._MANAGED_POM_RE.search(str(d))
            items.append((m.group(1) if m else root, f"explicit version dropped: {str(d)[:m.start()].strip() if m else d}"))
        for g in list(getattr(res, "generated_files", None) or []):
            if PurePosixPath(str(g).replace("\\", "/")).name == "pom.xml":
                items.append((str(g).replace("\\", "/"), "generated by the restructure"))
        for pom, note in items:
            self.errors.add_pom_verify(pom, note)
            log.info("%s: %s (pom-verify #%d)", pom, note, self.errors.size())

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
        the as-is versions of the Handover) is never overwritten. None = not available (custom build runner without a
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

        def _run_final() -> BuildResult:
            skip_tests, skip_run = _skip_flags()
            if skip_run:
                try:
                    return self.build_runner.run(goal, module=module, label=label, skip_test_compile=False,
                                                 skip_tests=True, **kw)
                except TypeError:  # an older runner without the `skip_tests` option
                    kw["extra_args"] = [*extra, "-DskipTests"]
                    return self.build_runner.run(goal, module=module, label=label, skip_test_compile=False, **kw)
            return self.build_runner.run(goal, module=module, label=label, skip_test_compile=skip_tests, **kw)

        def _final_build() -> BuildResult:
            before = _status_paths(self.ws)
            res = refine_build(_run_final())           # the refined case keys (sbmig.triage.cluster)
            _restore_build_byproducts(self.ws, before)
            return res

        res = _final_build()
        self._observe_build(goal, res, step.target)
        if not res.ok and self._verify_fix_loop(pr, step, res):
            if self.loop is not None and self.loop.aborted:
                pr.notes.append(f"{step.id}: final fix loop aborted the run ({self.loop.aborted}); the failed build stands")
            else:
                pr.notes.append(f"{step.id}: final build repeated after the fix loop")
                log.info("repeating `mvn %s` after the fix loop", goal)
                res = _final_build()
                self._observe_build(goal, res, step.target)
        self._verify_results.append(res)
        if step.id not in self._verify_done:
            self._verify_done.append(step.id)
        skip_tests, skip_run = _skip_flags()
        assert self.report is not None
        self.report.final_build = res
        pr.iterations += 1
        pr.remaining_errors += res.errors
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
        """The final `mvn clean package` failed → the script fix loop on `loop.phases.verify` (goal `package`), after
        which the caller repeats the final build (invariant 1: the run still ends on a `clean package`). v2: this is
        THE fix loop (no intermediate loops); its build 1 is the failed final build itself (not built again). A case
        blocked earlier stays blocked: its limit is script.max_augments_per_case per run. Returns True when the loop
        ran."""
        pc = self.cfg.loop.phases.get(Phase.VERIFY.value)
        if pc is None or not pc.enabled or pc.max_iterations <= 0 or self.loop is None or self._dry_run \
                or getattr(self, "_after_abort", False):
            return False
        if self.budget.time_exceeded():
            pr.notes.append(f"{step.id}: failed; no final fix loop (run time budget exhausted)")
            return False
        clusters = self.loop._cluster(list(first.errors), step.target)          # noqa: SLF001 - same package
        covering = [self.blocked.covering(c) for c in clusters]
        if clusters and all(it is not None for it in covering):
            reasons = sorted({it.reason.value for it in covering if it is not None})
            pr.notes.append(f"{step.id}: failed with blocked case(s) only ({', '.join(reasons)}) → no fix loop; "
                            "see 'Blocked' in the report")
            return False
        self.ws.journal({"event": "verify_loop_start", "step": step.id, "goal": pc.goal, "target": step.target,
                         "max_iterations": pc.max_iterations, "errors": first.error_count,
                         "clusters": [c.id for c in clusters]})
        log.info("final build failed with %d error(s) in %d case(s) → fix loop (mvn %s per round, up to %d round(s))",
                 first.error_count, len(clusters), pc.goal, pc.max_iterations)
        res = self.loop.run(Phase.VERIFY, pc.goal, pc.max_iterations, target=step.target, first_build=first)
        pr.iterations += res.iterations
        pr.attempts += res.attempts
        pr.iteration_records += res.iteration_records
        pr.rewrite_results += res.rewrite_results
        pr.notes += [f"{step.id} loop: {n}" for n in res.notes]
        if BuildFixLoop.test_compile_skipped_in(res) or BuildFixLoop.tests_skipped_in(res):
            # the L7 / L7b fallback passed the loop: its test errors are what the owners must fix (unresolved)
            pr.remaining_errors += [e for e in res.remaining_errors if e not in pr.remaining_errors]
        self._merge_blocked(pr, res.blocked)
        self.ws.journal({"event": "verify_loop_end", "step": step.id, "build_ok": res.build_ok,
                         "attempts": len(res.attempts), "iterations": res.iterations,
                         "blocked": [b.id for b in res.blocked]})
        return True

    # ------------------------------------------------------------------ kb
    # resolution error kinds (infra:repository at finalize)
    _RESOLUTION_KINDS = frozenset({ErrorKind.DEPENDENCY_RESOLUTION, ErrorKind.PARENT_RESOLUTION,
                                   ErrorKind.PLUGIN_RESOLUTION})

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

    # ------------------------------------------------------------------ v2.1 error list
    def _start_error_list(self, dry_run: bool) -> None:
        """The run's error list (`sbmig.errors`, through `RunErrors`): from the compile step on, every build error,
        pom correction, review finding and fix try. None in a dry run (nothing builds)."""
        self.errors = RunErrors(None)
        if dry_run:
            return
        project = ((self.profile.name if self.profile is not None else "") or
                   (self.report.project_name if self.report is not None else "") or self.ws.root.name)
        self.errors = RunErrors.create(self.ws, self.cfg, project, factory=self._error_list_factory)

    def _observe_build(self, goal: str, res: BuildResult, target: Optional[str]) -> None:
        """A final `mvn clean package` on the error list (only its NEW errors are added; the open entries it no longer
        shows are resolved; the pom-verify entries are verified) + one line: `mvn clean package: 25 error(s), 새 에러
        2건 추가, pom-verify 1건 resolved`."""
        void = bool(res.timed_out) or (not res.ok and not res.errors)
        added = self.errors.add_build(res, "build", void=void, scope=target)
        poms = self.errors.verify_poms(res)
        if res.ok:
            line = f"mvn {goal}: BUILD SUCCESS"
        else:
            line = f"mvn {goal}: {res.error_count} error(s)" + (", timed out" if res.timed_out else "")
            line += f", 새 에러 {added}건 추가" if self.errors.active else ""
        if poms:
            line += f", pom-verify {poms}건 resolved"
        log.info("%s", line)

    def _finalize_errors(self, report: RunReport) -> None:
        """v2.1 finalize: the open entries → unresolved (`end_of_run`), `<run dir>/errors.jsonl` + `errors.md`,
        `report.error_entries` (diffs capped), the KB gets the run's entries (`kb.record_run`; not with `--no-kb`).
        Never fails the run: a problem becomes the flag `errors-failed: <msg>`."""
        el = self.errors
        if not el.active or self._dry_run:
            return

        def flag(text: str) -> None:
            if text not in report.flags:
                report.flags.append(text)

        try:
            counts = el.end_of_run()
            report.outputs.update(el.write())
            report.error_entries = el.to_report()
            for p in el.problems[:3]:
                flag(f"errors-failed: {p}")
            kb_n = None
            rec = getattr(self._kb, "record_run", None) if self._kb is not None else None
            if callable(rec):
                with stage("kb"):
                    try:
                        kb_n = rec(el.entries())
                    except Exception as e:  # noqa: BLE001 - the KB says it never raises; stay defensive
                        log.warning("kb.record_run failed: %s: %s", type(e).__name__, e)
                        flag(f"errors-failed: kb: {type(e).__name__}: {e}")
            self.ws.journal({"event": "error_list", "counts": counts, "entries": len(report.error_entries),
                             "kb_recorded": kb_n})
            log.info("error list: %d entr%s (%s)%s", len(report.error_entries),
                     "y" if len(report.error_entries) == 1 else "ies",
                     ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "-",
                     f"; {kb_n} recorded in the KB" if kb_n is not None else "")
        except Exception as e:  # noqa: BLE001 - never fail the run over the error list
            log.exception("error list failed")
            flag(f"errors-failed: {type(e).__name__}: {e}")

    def _flag(self, flag: str) -> None:
        if self.report is not None and flag not in self.report.flags:
            self.report.flags.append(flag)

    def _finalize_operator_checks(self, report: RunReport) -> None:
        """v2.1d `report.operator_checks` (운영자 확인 필요): one OperatorCheck per major upgrade / library replacement
        of the error list's tries (the fix attempts' records without a list) and of the structure review — only the
        changes still in the final tree (`still_applied`; a rolled-back try is not listed), the same (pom, library,
        version) once (the latest try that made it). Flag `major-upgrade`. Never raises."""
        try:
            checks = self._operator_checks(report)
        except Exception as e:  # noqa: BLE001 - never lose the report over it
            log.warning("operator checks failed: %s: %s", type(e).__name__, e)
            return
        report.operator_checks = checks
        if checks:
            self._flag("major-upgrade")
            log.warning("운영자 확인 필요: 메이저 업그레이드 %d건 (report Handover \"운영자 확인 필요\")", len(checks))
            self.ws.journal({"event": "operator_checks", "count": len(checks),
                             "changes": [c.change.text for c in checks]})

    def _operator_checks(self, report: RunReport) -> list:
        from sbmig.core.models import OperatorCheck

        from .fixloop import ROLLED_BACK_NOTE
        by_commit = {a.commit: a for pr in report.phases for a in pr.attempts if a.commit}
        found: dict[tuple, OperatorCheck] = {}

        def add(ch: Any, **kw: Any) -> None:
            key = (ch.path, ch.key, ch.after)
            found.pop(key, None)                    # the latest try that made the change wins (its guide, commit)
            found[key] = OperatorCheck(kind="library_replaced" if ch.kind == "replaced" else "major_upgrade",
                                       change=ch, **kw)

        rv = report.structure_review
        if rv is not None and rv.commit and not rv.reverted:
            ans = rv.answer
            for ch in rv.major_upgrades or []:
                add(ch, source="review", commit=rv.commit or "", guide=(ans.verify if ans is not None else "") or "",
                    guide_source=self._review_guide_source or ("llm" if ans is not None and ans.verify else "sbmig"))
        tries: list[tuple[str, Any]] = []
        seen: set[tuple] = set()
        for e in list(self.errors.entries() or []):
            for t in getattr(e, "tries", None) or []:
                k = (getattr(e, "case_id", ""), getattr(t, "n", 0), getattr(t, "round", 0))
                if k in seen or not getattr(t, "major_upgrades", None) or getattr(t, "source", "") == "review":
                    continue
                seen.add(k)
                tries.append((str(getattr(e, "case_id", "") or ""), t))
        tries.sort(key=lambda ct: (getattr(ct[1], "round", 0), getattr(ct[1], "n", 0)))
        if tries:
            for case_id, t in tries:
                if getattr(t, "applied", "none") == "none" or t.result in ("regressed", "reverted", "apply_failed") \
                        or ROLLED_BACK_NOTE in (t.note or ""):
                    continue
                att = by_commit.get(t.commit) if t.commit else None
                for ch in t.major_upgrades:
                    add(ch, case_id=case_id, source=f"fix r{t.round} try {t.n}", commit=t.commit or "",
                        guide=t.verify or "",
                        guide_source=(getattr(t, "verify_source", "") or (att.verify_source if att is not None
                                                                           and att.verify_source else "sbmig")),
                        files=list(t.files or []))
        else:                                       # no error list (inactive): the attempts carry the same facts
            for pr in report.phases:
                for a in pr.attempts:
                    if not a.major_upgrades or not a.applied or a.outcome in ("regressed", "reverted",
                                                                               "apply_failed") \
                            or any(n.startswith(ROLLED_BACK_NOTE) for n in a.notes):
                        continue
                    for ch in a.major_upgrades:
                        add(ch, case_id=a.script_case_id, source=f"fix {a.id}", commit=a.commit or "",
                            guide=a.verify, guide_source=a.verify_source or "sbmig",
                            files=list(dict.fromkeys(c.new_path or c.path for c in a.changes)))
        out = []
        for c in found.values():
            if self._still_applied(c.change):
                out.append(c)
        return out

    def _still_applied(self, change: Any) -> bool:
        """`sbmig.build.depchanges.still_applied` (a failure keeps the check: better listed once too often)."""
        try:
            from sbmig.build.depchanges import still_applied
            return bool(still_applied(self.ws, change))
        except Exception as e:  # noqa: BLE001
            log.debug("still_applied(%s) unavailable: %s: %s", getattr(change, "key", "?"), type(e).__name__, e)
            return True

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
        if not any(pr.phase in (Phase.SPRING, Phase.TRANSFORM) for pr in report.phases):
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
        advisory's hint (FixAttempt.advisory_ids; v2: LLM fix scripts — new / augment, a reuse had no prompt —,
        applied, improved / build_ok, cluster not left unresolved). Runs before the Handover is built."""
        good = [a for pr in report.phases for a in pr.attempts
                if a.advisory_ids and a.decision.kind == DecisionKind.SCRIPT and a.applied
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
        """Fix-script attempts (their own model), the plan advisor and the structure review, per model."""
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
        for k, u in self._review_usage.items():
            acc(u, k or self.cfg.llm.model)
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
        for u in self._review_usage.values():      # v2: the structure review
            usage = usage.add(u.model_copy(update={"model": usage.model}))
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
        # a cap (loop.max_llm_calls > 0) reached, or -1 (the fleet's total used up) when a case needed the LLM
        if (self.budget.llm_limited and self.budget.llm_exhausted()) or (
                self.budget.llm_off and any(it.reason.value == "budget_exhausted" for it in self.blocked.items())):
            self._flag("llm-budget-exhausted")
        built = bool(self._verify_results) or any(pr.iterations for pr in report.phases)
        if getattr(self.cfg.build, "skip_test_compile", False) and not self._dry_run and built:
            # only when a build ran (v2 default true: an aborted run before any build carries no such follow-up)
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
        self._finalize_operator_checks(report)
        self._finalize_advisories(report)
        self._finalize_errors(report)
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
