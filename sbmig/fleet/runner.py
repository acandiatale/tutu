"""Sequential fleet runner: ONE agent migrates N projects one after the other, feeding one shared KB.

Every project's fixes are harvested into the KB (cases, labels, ledger, symbol index, recipe candidates)
before the next project starts, so project k+1 already plans with the recipes project k produced, gets the
symbol hints in its LLM prompts, and credits (→ auto-promotes) the candidates that work for it.

State lives in ``<out>/fleet/state.json`` (:class:`FleetState`), rewritten atomically after every project,
so a killed fleet resumes where it stopped (``sbmig fleet run`` is resumable by default).
"""

from __future__ import annotations

import logging
import tempfile
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from sbmig.core.config import Config, _apply_env_overrides, _expand_env, deep_merge
from sbmig.core.models import CaseType, DecisionKind, FleetProjectResult, FleetState, RunReport, utcnow

from .manifest import FleetProject, load_manifest

log = logging.getLogger("sbmig.fleet")

EXIT_ALL_SUCCESS = 0
EXIT_INCOMPLETE = 2
EXIT_STOPPED_ON_ABORT = 3
SUCCESS_OUTCOMES = ("improved", "build_ok")
KB_RECIPE_PREFIX = "com.company.kb."
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
        self.dry_run = dry_run
        self.stopped_on_abort = False
        self.state: Optional[FleetState] = None
        self.planned: list[
            tuple[int, FleetProjectResult, str]
        ] = []  # (index, result, action) of the last run

    # ------------------------------------------------------------------ kb
    @property
    def kb(self) -> Any:
        """One FileKnowledgeBase for the whole fleet: its in-memory index cache stays warm and
        auto-promotions / recorded blocked items are tracked across projects. Everything is written to
        files immediately, so a new instance per project would see the same KB — sharing just avoids
        re-reading the index 500 times."""
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
        by_name = {p.name: p for p in state.projects}
        for proj in projects:
            r = by_name.get(proj.name)
            if r is None:
                r = FleetProjectResult(name=proj.name, input=proj.input)
                state.projects.append(r)
                by_name[proj.name] = r
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

    def _order(self, projects: list[FleetProject], state: FleetState) -> list[FleetProject]:
        if self.cfg.fleet.order != "simple-first":
            return list(projects)
        by_name = {p.name: p for p in state.projects}
        for proj in projects:
            r = by_name[proj.name]
            if proj.ok and r.case_type == CaseType.UNKNOWN and r.status != "success":
                try:
                    r.case_type = self._case_type(proj)
                except Exception as e:  # analysis is only an ordering hint
                    log.warning("fleet: could not analyze %s for ordering: %s", proj.name, e)
        return sorted(projects, key=lambda p: CASE_RANK.get(by_name[p.name].case_type, 5))

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
            data["loop"]["max_llm_calls"] = max(0, min(int(data["loop"]["max_llm_calls"]), remaining_llm))
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

    def _is_kb_recipe(self, rid: str) -> bool:
        if rid.startswith(KB_RECIPE_PREFIX):
            return True
        try:
            return (
                self._kb is not None
                and hasattr(self._kb, "get_recipe")
                and self._kb.get_recipe(rid) is not None
            )
        except Exception:  # pragma: no cover
            return False

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
        r.blocked = len(report.blocked)
        r.blocked_reasons = [
            f"{b.reason.value}:{b.artifact.key if b.artifact else (b.owner_class or '-')}"
            for b in report.blocked
        ]
        r.unresolved = len(report.unresolved)
        r.flags = list(report.flags)
        r.advisories = [f"{f.advisory_id}:{f.outcome}" for f in report.advisories]
        r.carried_over = len(report.handover.carried_over) if report.handover is not None else 0
        h = report.harvest
        r.kb_cases_created = list(h.cases_created) if h else []
        r.kb_candidates_created = list(h.candidates_created) if h else []
        r.kb_recipes_promoted = list(h.recipes_promoted) if h else []
        used: list[str] = []
        for pr in report.phases:
            for a in pr.attempts:
                if not (a.applied and a.outcome in SUCCESS_OUTCOMES):
                    continue
                if a.decision.kind == DecisionKind.KB_RECIPE:
                    used += [x for x in a.decision.recipe_ids if self._is_kb_recipe(x)]
                elif a.decision.kind == DecisionKind.KB_PATCH:
                    used += [f"case:{x}" for x in a.decision.kb_case_ids]
            if pr.build_ok:
                for rr in pr.rewrite_results:
                    used += [
                        rid
                        for rid, files in (rr.recipes_applied or {}).items()
                        if files and self._is_kb_recipe(rid)
                    ]
        r.kb_recipes_used = list(dict.fromkeys(used))
        r.out_dir = report.outputs.get("run_dir") or r.out_dir
        if code == 3 and not r.error:
            r.error = next((m for m in report.manual_followups if m.startswith("run aborted")), None)

    def run(self) -> FleetState:
        projects = load_manifest(self.manifest_path)
        self.state = state = self.load_state(projects)
        ordered = self._order(projects, state)
        by_name = {p.name: p for p in state.projects}
        index_of = {p.name: i for i, p in enumerate(projects, 1)}
        self.planned = [(index_of[p.name], by_name[p.name], self._action(by_name[p.name])) for p in ordered]
        if self.dry_run:
            return state
        self.fleet_dir.mkdir(parents=True, exist_ok=True)
        self.save_state()
        self._write_summaries()
        fc = self.cfg.fleet
        ran = 0
        total = len(ordered)
        for pos, proj in enumerate(ordered, 1):
            r = by_name[proj.name]
            action = self._action(r)
            if action in ("done", "skip", "keep"):
                continue
            if fc.max_projects and ran >= fc.max_projects:
                state.notes.append(
                    f"max_projects={fc.max_projects} reached; {proj.name} and later projects left pending"
                )
                break
            remaining = self._remaining_llm()
            if remaining == 0 and f"llm-budget-exhausted:{proj.name}" not in state.notes:
                state.notes.append(f"llm-budget-exhausted:{proj.name}")
            if r.status in ("incomplete", "aborted", "running"):
                r.attempt += 1
            r.status, r.error = "running", None
            r.started_at, r.finished_at = utcnow(), None
            run_id = self._run_id(index_of[proj.name], proj, r.attempt)
            r.run_id = run_id
            r.out_dir = str(self.out_dir / run_id)
            self.save_state()
            self.echo(f"[{pos}/{total}] {proj.name}: running ({run_id})")
            t0 = time.monotonic()
            try:
                pcfg = self.project_config(proj, remaining)
                runner = self._run_project or self._default_run_project
                report = runner(proj, pcfg, self.out_dir, run_id)
                self.fill_result(r, report, exit_code_for(report))
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
            r.finished_at = utcnow()
            r.duration_s = round(time.monotonic() - t0, 1)
            ran += 1
            self.save_state()
            self.echo(
                f"[{pos}/{total}] {proj.name}: {r.status} (exit {r.exit_code}, {r.duration_s:.0f}s, "
                f"llm {r.llm_calls}, blocked {r.blocked}, kb used {len(r.kb_recipes_used)} / created "
                f"{len(r.kb_candidates_created)} / promoted {len(r.kb_recipes_promoted)})"
            )
            if fc.reindex_every and ran % fc.reindex_every == 0:
                try:
                    if hasattr(self.kb, "reindex"):
                        self.kb.reindex()
                except Exception as e:  # pragma: no cover
                    log.warning("fleet: kb reindex failed: %s", e)
            self._write_summaries()
            if r.status == "aborted" and fc.stop_on_abort:
                self.stopped_on_abort = True
                state.notes.append(f"stopped on abort of {proj.name} (fleet.stop_on_abort)")
                break
        if (
            all(p.status in ("success", "incomplete", "aborted", "skipped") for p in state.projects)
            and not self.stopped_on_abort
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
        return fleet_exit_code(self.state, self.stopped_on_abort)


def fleet_exit_code(state: Optional[FleetState], stopped_on_abort: bool = False) -> int:
    if stopped_on_abort:
        return EXIT_STOPPED_ON_ABORT
    if state is not None and state.projects and all(p.status == "success" for p in state.projects):
        return EXIT_ALL_SUCCESS
    return EXIT_INCOMPLETE
