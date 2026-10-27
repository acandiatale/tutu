"""Sequential fleet runner: ONE agent migrates N projects one after the other, feeding one shared KB.

Every project's error list and fix scripts go into the KB (``errors.jsonl``, ``cases/<case>/case.json`` +
``fix_v<N>.py``, PLAN-v2.1 §3.4) before the next project starts, so project k+1 runs the script project k stored for
the same case first (stored script → augment → direct edits, PLAN-v2.1 §3.1) and credits (→ proven) the versions
that work for it.

State lives in ``<out>/fleet/state.json`` (:class:`FleetState`), rewritten atomically after every project,
so a killed fleet resumes where it stopped (``sbmig fleet run`` is resumable by default). Projects are identified by
their input path (``manifest.input_key``), not by their name: a renamed or added folder never inherits another
project's result (round 10).

Infrastructure guard (round 10, ``fleet.*``): with ``fleet.preflight`` the Java / Maven / settings / repository / LLM
/ disk checks of :mod:`sbmig.fleet.preflight` run once before the first project. A project whose run ended for an
infrastructure reason (an ``infra:<kind>`` flag — or ``llm-unavailable`` / ``llm-config-error`` / ``mvn-not-found`` /
``jdk-not-21`` — on a run that is not green, or a crash such as a full disk) goes back to ``pending`` with
``error: infra:<kind>``. After ``fleet.infra_failure_limit`` such projects in a row the fleet waits
``fleet.infra_retry_minutes``, re-runs the preflight and resumes (the deferred projects first); after
``fleet.infra_max_waits`` waits without a healthy project in between, at once when ``infra_retry_minutes`` is 0, or
when the re-check finds a configuration error (no Maven, another JDK, settings missing, LLM key rejected) it stops
with one line ``fleet stopped: infrastructure <kind> unavailable since <project> - fix it and run sbmig fleet
again`` (exit 3, state saved; the next run resumes the pending projects). Before each project the free space on the
output drive is checked (``fleet.min_free_gb``, kind ``disk``); after each project ``fleet.cleanup`` deletes the build
output / the project copy (:mod:`sbmig.fleet.cleanup`). Every project logs DEBUG into its own ``<run dir>/sbmig.log``;
``fleet/sbmig.log`` (attached by the CLI) stays at INFO. One ``SBMIG-RESULT …`` line per project
(:mod:`sbmig.report.result_line`).
"""

from __future__ import annotations

import errno
import logging
import tempfile
import time
import traceback
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from sbmig.core.config import Config, _apply_env_overrides, _expand_env, deep_merge
from sbmig.core.models import CaseType, FleetProjectResult, FleetState, RunReport, utcnow
from sbmig.report.errors import status_counts
from sbmig.report.result_line import counts_open_resolved, infra_kind, result_attention, result_line

from .manifest import FleetProject, input_key, load_manifest

log = logging.getLogger("sbmig.fleet")

NO_LLM_CALLS = -1                        # `loop.max_llm_calls: -1` = no LLM call at all (v2.1)

EXIT_ALL_SUCCESS = 0
EXIT_INCOMPLETE = 2
EXIT_STOPPED_ON_ABORT = 3
EXIT_STOPPED_ON_INFRA = 3          # stopped by the infrastructure guard (state saved, re-run resumes)
INFRA_PREFIX = "infra:"
MAX_INFRA_REQUEUES = 2             # a deferred project is re-run at most this often per fleet process
NO_CONSOLE = {"sbmig_console": False}   # fleet/sbmig.log only: the console already got the line via `echo`
CASE_RANK = {
    CaseType.PURE_JAVA: 0,
    CaseType.SERVLET_TOMCAT: 1,
    CaseType.SPRING_LEGACY: 2,
    CaseType.SPRING_BOOT: 3,
    CaseType.MSA: 4,
    CaseType.UNKNOWN: 5,
}

RunProject = Callable[[FleetProject, Config, Path, str], RunReport]


def new_fleet_id() -> str:
    return "fleet-" + datetime.now().strftime("%Y%m%d-%H%M%S")


def exit_code_for(report: RunReport) -> int:
    from sbmig.cli import run_exit_code

    return run_exit_code(report)


def _status_for(code: int) -> str:
    return {0: "success", 3: "aborted"}.get(code, "incomplete")


UNSUPPORTED_FLAG = "unsupported-build-tool:"


def unsupported_build_tool(report: RunReport) -> Optional[str]:
    """`<tool>` when intake rejected the project as not-Maven (round 7b flag `unsupported-build-tool:<tool>`): in a
    fleet that is a *skip* (no retry, never a reason to stop on abort), not a failed migration (round 9)."""
    return next((f[len(UNSUPPORTED_FLAG):] for f in report.flags if f.startswith(UNSUPPORTED_FLAG)), None)


def infra_kind_for_exception(exc: BaseException) -> Optional[str]:
    """The infrastructure kind of a crash: a full disk, the LLM transport, a Maven executable that vanished."""
    if isinstance(exc, OSError) and (exc.errno == errno.ENOSPC or getattr(exc, "winerror", None) in (39, 112)):
        return "disk"
    try:
        import requests

        if isinstance(exc, requests.RequestException):
            return "llm"
    except ImportError:  # pragma: no cover
        pass
    if type(exc).__name__ in ("LLMError", "LLMHardDeadline"):
        return "llm"
    if isinstance(exc, FileNotFoundError) and "mvn" in str(exc).lower():
        return "maven"
    return None


def report_infra_kind(report: RunReport, code: int) -> Optional[str]:
    """`infra:<kind>` (and the older equivalent flags) of a run that did not end green; a green run keeps its success
    even when the LLM was unavailable for a while."""
    return infra_kind(report.flags) if code != EXIT_ALL_SUCCESS else None


class FleetRunner:
    """Run the projects of a manifest sequentially (see module docstring).

    ``run_project(project, cfg, out_dir, run_id) -> RunReport`` can be injected (tests); the default
    copies the project into ``<out>/<run_id>/project`` and runs the full
    :class:`sbmig.loop.Orchestrator` with the shared KB.
    ``analyze(project, cfg) -> CaseType`` is used by ``fleet.order: simple-first`` (cached in the state).
    """

    def __init__(
        self,
        cfg: Config,
        manifest_path: str | Path,
        out_dir: str | Path,
        *,
        run_project: Optional[RunProject] = None,
        kb: Any = None,
        analyze: Optional[Callable[[FleetProject, Config], CaseType]] = None,
        echo: Optional[Callable[[str], None]] = None,
        dry_run: bool = False,
        preflight: Optional[Callable[[], list]] = None,
        disk_check: Optional[Callable[[], Any]] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.cfg = cfg
        self.manifest_path = Path(manifest_path)
        self.out_dir = Path(out_dir).resolve()
        self.fleet_dir = self.out_dir / "fleet"
        self.state_path = self.fleet_dir / "state.json"
        self._run_project = run_project
        self._kb = kb
        self._analyze = analyze
        self.echo = echo or (lambda msg: log.info("%s", msg))
        self._echo_logs = echo is None             # the default echo IS the log; a CLI echo is logged on top
        self.dry_run = dry_run
        self.stopped_on_abort = False
        self.stopped_on_infra: Optional[str] = None   # the infra kind that stopped the fleet
        self.state: Optional[FleetState] = None
        self.planned: list[
            tuple[int, FleetProjectResult, str]
        ] = []  # (index, result, action) of the last run
        # preflight / disk checks: injected (tests) or the real ones (sbmig.fleet.preflight); a custom run_project
        # brings its own infrastructure, so without an injected preflight it runs none
        self._preflight = preflight
        self._disk_check = disk_check
        self._sleep = sleep
        self._waits = 0                           # infrastructure waits since the last healthy project
        self._results: dict[int, FleetProjectResult] = {}     # id(FleetProject) → its result in the state

    # ------------------------------------------------------------------ kb
    @property
    def kb(self) -> Any:
        """One FileKnowledgeBase for the whole fleet: its caches (known record ids, parsed case files) stay warm
        across projects. Everything is written to files immediately, so a new instance per project would see the
        same KB — sharing just avoids re-reading it 500 times."""
        if self._kb is None:
            from sbmig.kb import FileKnowledgeBase

            self._kb = FileKnowledgeBase(self.cfg)
        return self._kb

    # ------------------------------------------------------------------ state
    def load_state(self, projects: list[FleetProject]) -> FleetState:
        state: Optional[FleetState] = None
        if self.state_path.exists():
            try:
                state = FleetState.model_validate_json(self.state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                log.warning("fleet state %s unreadable (%s); starting a new fleet", self.state_path, e)
        if state is None:
            state = FleetState(fleet_id=new_fleet_id(), manifest=str(self.manifest_path))
        # keyed by the input path (round 10): the name is only a label — with the old name keys a Korean folder name
        # (slug `project`) or a renamed folder could inherit another project's `success` and be skipped. State files
        # of earlier rounds stored the same resolved input path, so they match as they are; the same input listed
        # twice takes the results in order.
        pool: dict[str, list[FleetProjectResult]] = {}
        for r in state.projects:
            pool.setdefault(input_key(r.input), []).append(r)
        self._results = {}
        for proj in projects:
            cands = pool.get(input_key(proj.input)) or []
            r = cands.pop(0) if cands else None
            if r is None:
                r = FleetProjectResult(name=proj.name, input=proj.input)
                state.projects.append(r)
            self._results[id(proj)] = r
            r.name = proj.name
            r.input = proj.input
            if not proj.ok:
                if r.status in ("pending", "skipped"):
                    r.status, r.error = "skipped", proj.error
            elif r.status == "skipped" and not (r.error or "").startswith("unsupported build tool"):
                r.status, r.error = "pending", None      # the input is back; an intake rejection stays skipped
        state.manifest = str(self.manifest_path)
        state.finished_at = None
        return state

    def save_state(self) -> None:
        from sbmig.kb.store import atomic_write

        assert self.state is not None
        self.state.updated_at = utcnow()
        self.state.llm_calls_total = sum(p.llm_calls for p in self.state.projects)
        self.state.llm_cost_total = round(sum(p.llm_cost or 0.0 for p in self.state.projects), 6)
        atomic_write(self.state_path, self.state.model_dump_json(indent=2) + "\n")

    # ------------------------------------------------------------------ ordering
    def _case_type(self, proj: FleetProject) -> CaseType:
        if self._analyze is not None:
            return self._analyze(proj, self.cfg)
        from sbmig.analyze import analyze_project
        from sbmig.core.workspace import Workspace

        src = Path(proj.input)
        with tempfile.TemporaryDirectory(prefix="sbmig-fleet-analyze-") as tmp:
            if src.is_dir():
                ws = Workspace(src, Path(tmp) / "run", "fleet-analyze")
            else:
                ws = Workspace.from_input(src, tmp, run_id="fleet-analyze")
            return analyze_project(ws, self.cfg).case_type

    def result_for(self, proj: FleetProject) -> FleetProjectResult:
        return self._results[id(proj)]

    def _order(self, projects: list[FleetProject], state: FleetState) -> list[FleetProject]:
        if self.cfg.fleet.order != "simple-first":
            return list(projects)
        for proj in projects:
            r = self.result_for(proj)
            if proj.ok and r.case_type == CaseType.UNKNOWN and r.status != "success":
                try:
                    r.case_type = self._case_type(proj)
                except Exception as e:  # analysis is only an ordering hint
                    log.warning("fleet: could not analyze %s for ordering: %s", proj.name, e)
        return sorted(projects, key=lambda p: CASE_RANK.get(self.result_for(p).case_type, 5))

    def _action(self, r: FleetProjectResult) -> str:
        if r.status == "success":
            return "done"
        if r.status == "skipped":
            return "skip"
        if r.status in ("incomplete", "aborted"):
            return "retry" if self.cfg.fleet.retry_failed else "keep"
        return "run"  # pending, or running (the fleet process died during this project)

    # ------------------------------------------------------------------ per-project config
    def project_config(self, proj: FleetProject, remaining_llm: Optional[int]) -> Config:
        data = self.cfg.model_dump(mode="json")
        for c in proj.configs:
            with open(c, "r", encoding="utf-8") as fh:
                data = deep_merge(data, _expand_env(yaml.safe_load(fh) or {}))
        if proj.overrides:
            data = deep_merge(data, _expand_env(proj.overrides))
        if proj.configs or proj.overrides:
            data = _apply_env_overrides(data)
        data["kb"]["dir"] = self.cfg.kb.dir  # the KB is fleet-wide
        data["repo_root"] = self.cfg.repo_root
        if remaining_llm is not None:
            # v2.1 `loop.max_llm_calls`: 0 = no limit, > 0 = cap, -1 = no LLM call at all. The fleet's remainder caps
            # an unlimited project; an exhausted fleet budget means no LLM call (-1, noted llm-budget-exhausted)
            per = int(data["loop"].get("max_llm_calls") or 0)
            if per < 0 or remaining_llm <= 0:
                data["loop"]["max_llm_calls"] = NO_LLM_CALLS
            else:
                data["loop"]["max_llm_calls"] = remaining_llm if per == 0 else min(per, remaining_llm)
        return Config.model_validate(data)

    def _remaining_llm(self) -> Optional[int]:
        total = self.cfg.fleet.max_llm_calls_total
        if total <= 0:
            return None
        assert self.state is not None
        return max(0, total - sum(p.llm_calls for p in self.state.projects))

    # ------------------------------------------------------------------ run
    def _default_run_project(self, proj: FleetProject, cfg: Config, out_dir: Path, run_id: str) -> RunReport:
        from sbmig.core.workspace import Workspace
        from sbmig.loop import Orchestrator

        ws = Workspace.from_input(proj.input, out_dir, run_id=run_id)
        return Orchestrator(cfg, ws, kb=self.kb).run()

    def _run_id(self, index: int, proj: FleetProject, attempt: int) -> str:
        assert self.state is not None
        base = f"{self.state.fleet_id}-{index:03d}-{proj.name}" + (f"-r{attempt}" if attempt > 1 else "")
        rid, n = base, 1
        while (self.out_dir / rid).exists():
            n += 1
            rid = f"{base}-{n}"
        return rid

    def fill_result(self, r: FleetProjectResult, report: RunReport, code: int) -> None:
        r.exit_code = code
        r.status = _status_for(code)  # type: ignore[assignment]
        tool = unsupported_build_tool(report)
        if tool is not None:
            r.status = "skipped"
            r.error = f"unsupported build tool: {tool} (only Maven projects are migrated)"
        r.case_type = report.case_type
        r.llm_calls = report.llm_usage.calls
        r.llm_input_tokens = report.llm_usage.input_tokens
        r.llm_output_tokens = report.llm_usage.output_tokens
        r.llm_cost = report.llm_cost.total if getattr(report, "llm_cost", None) is not None else None
        r.blocked = len(report.blocked)
        r.blocked_reasons = [
            f"{b.reason.value}:{b.artifact.key if b.artifact else (b.owner_class or '-')}"
            for b in report.blocked
        ]
        r.unresolved = len(report.unresolved)
        r.error_counts = dict(status_counts(getattr(report, "error_entries", None) or []))   # v2.1 error list
        r.flags = list(report.flags)
        r.advisories = [f"{f.advisory_id}:{f.outcome}" for f in report.advisories]
        r.carried_over = len(report.handover.carried_over) if report.handover is not None else 0
        r.out_dir = report.outputs.get("run_dir") or r.out_dir
        if code == 3 and not r.error:
            r.error = next((m for m in report.manual_followups if m.startswith("run aborted")), None)

    # ------------------------------------------------------------------ output helpers
    def _say(self, msg: str) -> None:
        """A progress line for the operator (console via `echo`) and for fleet/sbmig.log."""
        self.echo(msg)
        if not self._echo_logs:
            log.info("%s", msg, extra=NO_CONSOLE)

    # ------------------------------------------------------------------ infrastructure guard
    def _preflight_checks(self) -> Optional[list]:
        """The checks (printed), or None when no preflight runs (fleet.preflight off / custom runner)."""
        if self._preflight is not None:
            checks = list(self._preflight() or [])
            for c in checks:
                self._say(c.line())
            return checks
        if not self.cfg.fleet.preflight or self._run_project is not None:
            return None
        from .preflight import run_preflight

        return run_preflight(self.cfg, self.out_dir, echo=self._say)

    def _disk(self) -> Any:
        if self._disk_check is not None:
            return self._disk_check()
        if (self.cfg.fleet.min_free_gb or 0) <= 0:
            return None
        from .preflight import check_disk

        return check_disk(self.out_dir, self.cfg.fleet.min_free_gb)

    def _stop_infra(self, kind: str, since: str) -> None:
        assert self.state is not None
        line = f"fleet stopped: infrastructure {kind} unavailable since {since} - fix it and run sbmig fleet again"
        self.stopped_on_infra = kind
        self.state.notes.append(line)
        self._say(line)

    def _infra_pause(self, kind: str, since: str, *, transient: bool = True) -> bool:
        """Wait `infra_retry_minutes`, re-check, resume (True) — or stop the fleet (False, the one-line message)."""
        fc = self.cfg.fleet
        minutes = max(0, int(fc.infra_retry_minutes or 0))
        while transient and minutes > 0 and self._waits < max(0, int(fc.infra_max_waits or 0)):
            self._waits += 1
            self._say(f"fleet: infrastructure {kind} unavailable since {since} - waiting {minutes} min, then "
                      f"re-checking ({self._waits}/{fc.infra_max_waits})")
            self.save_state()
            self._write_summaries()
            self._sleep(minutes * 60.0)
            checks = self._preflight_checks()
            what = "infrastructure checks passed"
            if checks is None:                  # fleet.preflight off: only the disk can be re-checked
                disk = self._disk()
                checks = [disk] if disk is not None else []
                what = "fleet.preflight is off (disk re-checked only)"
            bad = [c for c in checks if getattr(c, "failed", False)]
            if not bad:
                self._say(f"fleet: {what} - resuming")
                return True
            kind = bad[0].kind or kind
            transient = all(getattr(c, "transient", False) for c in bad)
        self._stop_infra(kind, since)
        return False

    def _initial_preflight(self) -> bool:
        checks = self._preflight_checks()
        bad = [c for c in checks or [] if getattr(c, "failed", False)]
        if not bad:
            return True
        return self._infra_pause(bad[0].kind or "unknown", "preflight",
                                 transient=all(getattr(c, "transient", False) for c in bad))

    # ------------------------------------------------------------------ per project
    def _attach_run_log(self, run_dir: Path) -> Optional[Path]:
        """`<run dir>/sbmig.log` (DEBUG) for this project only — detached after it (round 10)."""
        try:
            from sbmig.cli import attach_file_log

            return attach_file_log(run_dir / "sbmig.log")
        except Exception as e:  # pragma: no cover - logging must never stop a fleet
            log.warning("fleet: per-run log not attached: %s", e)
            return None

    @staticmethod
    def _detach_run_log(path: Optional[Path]) -> None:
        if path is None:
            return
        try:
            from sbmig.cli import detach_file_log

            detach_file_log(path)
        except Exception as e:  # pragma: no cover
            log.warning("fleet: per-run log not detached: %s", e)

    def _cleanup(self, r: FleetProjectResult) -> None:
        mode = self.cfg.fleet.cleanup
        if mode == "none" or not r.out_dir:
            return
        from .cleanup import cleanup_run, human

        try:
            res = cleanup_run(Path(r.out_dir), mode)
        except Exception as e:  # cleanup must never stop the fleet
            log.warning("fleet: cleanup of %s failed: %s", r.out_dir, e)
            return
        if res["removed"]:
            log.info("fleet: cleanup %s of %s freed %s (%d path(s))", res["mode"], r.run_id, human(res["freed"]),
                     len(res["removed"]))

    def run(self) -> FleetState:
        projects = load_manifest(self.manifest_path)
        self.state = state = self.load_state(projects)
        ordered = self._order(projects, state)
        index_of = {id(p): i for i, p in enumerate(projects, 1)}
        self.planned = [(index_of[id(p)], self.result_for(p), self._action(self.result_for(p))) for p in ordered]
        if self.dry_run:
            return state
        self.fleet_dir.mkdir(parents=True, exist_ok=True)
        self.save_state()
        self._write_summaries()
        fc = self.cfg.fleet
        if any(a not in ("done", "skip", "keep") for _i, _r, a in self.planned) and not self._initial_preflight():
            self.save_state()
            self._write_summaries()
            return state
        ran = 0
        total = len(ordered)
        pos_of = {id(p): i for i, p in enumerate(ordered, 1)}
        queue = deque(ordered)
        streak: list[str] = []                  # names of the projects in a row that ended for an infra reason
        deferred: list[FleetProject] = []       # set back to pending for an infra reason (this process)
        requeued: dict[int, int] = {}
        while queue:
            proj = queue.popleft()
            r = self.result_for(proj)
            pos = pos_of[id(proj)]
            action = self._action(r)
            if action in ("done", "skip", "keep"):
                continue
            if fc.max_projects and ran >= fc.max_projects:
                state.notes.append(
                    f"max_projects={fc.max_projects} reached; {proj.name} and later projects left pending"
                )
                break
            disk = self._disk()
            if disk is not None and getattr(disk, "failed", False):
                self._say(disk.line())
                if not self._infra_pause("disk", proj.name):
                    break
                queue.appendleft(proj)
                continue
            remaining = self._remaining_llm()
            if remaining == 0 and f"llm-budget-exhausted:{proj.name}" not in state.notes:
                state.notes.append(f"llm-budget-exhausted:{proj.name}")
            if r.status in ("incomplete", "aborted", "running") or (r.error or "").startswith(INFRA_PREFIX):
                r.attempt += 1
            r.status, r.error = "running", None
            r.started_at, r.finished_at = utcnow(), None
            r.error_counts = {}
            run_id = self._run_id(index_of[id(proj)], proj, r.attempt)
            r.run_id = run_id
            r.out_dir = str(self.out_dir / run_id)
            self.save_state()
            self._say(f"[{pos}/{total}] {proj.name}: running ({run_id})")
            t0 = time.monotonic()
            kind: Optional[str] = None
            build_ok = False
            reported = False                     # a report came back: the result line carries open= / resolved=
            run_log = self._attach_run_log(self.out_dir / run_id)
            try:
                pcfg = self.project_config(proj, remaining)
                runner = self._run_project or self._default_run_project
                report = runner(proj, pcfg, self.out_dir, run_id)
                code = exit_code_for(report)
                self.fill_result(r, report, code)
                build_ok, reported = bool(report.success), True
                kind = report_infra_kind(report, code) if r.status != "skipped" else None
            except KeyboardInterrupt:
                r.status, r.error = "pending", "interrupted"
                r.duration_s = round(time.monotonic() - t0, 1)
                self.save_state()
                raise
            except Exception as e:
                log.exception("fleet: project %s crashed", proj.name)
                r.status, r.exit_code = "aborted", 3
                tb = traceback.format_exception_only(type(e), e)[-1].strip()
                r.error = f"crash: {tb}"[:500]
                kind = infra_kind_for_exception(e)
            finally:
                self._detach_run_log(run_log)
            if kind:
                r.status, r.error = "pending", INFRA_PREFIX + kind     # re-run when the infrastructure is back
            r.finished_at = utcnow()
            r.duration_s = round(time.monotonic() - t0, 1)
            ran += 1
            self.save_state()
            o_err, r_err = counts_open_resolved(r.error_counts)
            self._say(result_line(run_id, r.exit_code, build_ok, unresolved=r.unresolved, blocked=r.blocked,
                                  llm_calls=r.llm_calls, attention=result_attention(r),
                                  open_errors=o_err if reported else None, resolved_errors=r_err if reported else None))
            self._say(
                f"[{pos}/{total}] {proj.name}: {r.status} (exit {r.exit_code}, {r.duration_s:.0f}s, "
                f"llm {r.llm_calls}, errors open {o_err} / resolved {r_err}, blocked {r.blocked}, "
                f"unresolved {r.unresolved})"
                + (f" - {r.error}" if kind else "")
            )
            self._cleanup(r)
            if fc.reindex_every and ran % fc.reindex_every == 0:
                try:
                    if hasattr(self.kb, "reindex"):
                        self.kb.reindex()
                except Exception as e:  # pragma: no cover
                    log.warning("fleet: kb reindex failed: %s", e)
            self._write_summaries()
            if kind:
                streak.append(proj.name)
                deferred.append(proj)
                if len(streak) >= max(1, int(fc.infra_failure_limit or 1)):
                    if not self._infra_pause(kind, streak[0]):
                        break
                    again = [p for p in deferred if requeued.get(id(p), 0) < MAX_INFRA_REQUEUES]
                    for p in again:
                        requeued[id(p)] = requeued.get(id(p), 0) + 1
                    queue.extendleft(reversed(again))          # the deferred projects first, in their order
                    deferred = [p for p in deferred if p not in again]
                    streak.clear()
                continue
            streak.clear()
            self._waits = 0                     # a project ran on healthy infrastructure
            if r.status == "aborted" and fc.stop_on_abort:
                self.stopped_on_abort = True
                state.notes.append(f"stopped on abort of {proj.name} (fleet.stop_on_abort)")
                break
        if (
            all(p.status in ("success", "incomplete", "aborted", "skipped") for p in state.projects)
            and not self.stopped_on_abort
            and not self.stopped_on_infra
        ):
            state.finished_at = utcnow()
        self.save_state()
        self._write_summaries()
        return state

    def _write_summaries(self) -> None:
        from .summary import write_summaries

        try:
            write_summaries(self.state, self.fleet_dir, self.cfg, kb=self._kb_or_none())
        except Exception as e:  # summaries must never stop the fleet
            log.warning("fleet: writing summaries failed: %s", e)

    def _kb_or_none(self) -> Any:
        if self._kb is not None:
            return self._kb
        if self._run_project is not None:  # tests / custom runners: only use a KB that was injected
            return None
        try:
            return self.kb
        except Exception:  # pragma: no cover
            return None

    @property
    def exit_code(self) -> int:
        if self.stopped_on_infra:
            return EXIT_STOPPED_ON_INFRA
        return fleet_exit_code(self.state, self.stopped_on_abort)


def fleet_exit_code(state: Optional[FleetState], stopped_on_abort: bool = False) -> int:
    if stopped_on_abort:
        return EXIT_STOPPED_ON_ABORT
    if state is not None and state.projects and all(p.status == "success" for p in state.projects):
        return EXIT_ALL_SUCCESS
    return EXIT_INCOMPLETE
