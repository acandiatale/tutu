"""에이전트 하나가 프로젝트 N개를 차례로 이관하면서 KB 하나를 함께 채우는 일괄 실행 러너 모듈.
``sbmig fleet run|resume``이 :meth:`FleetRunner.run`을 호출하고, 프로젝트마다 ``sbmig run``과 같은
오케스트레이터를 실행한다.
- 프로젝트 k의 오류 목록과 수정 스크립트가 다음 프로젝트 전에 KB에
  들어가므로 k+1은 같은 케이스에서 저장된 스크립트부터 사용한다
- ``<out>/fleet/state.json``을 프로젝트마다 원자적으로 다시 쓰므로 프로세스가 죽어도 멈춘 곳부터 이어서 실행한다
- 이름이 바뀐 폴더가 다른 프로젝트의 성공을 물려받지 않도록 프로젝트는 입력 경로(``input_key``)로 찾는다
- 인프라 원인으로 끝난 프로젝트는 실패가 아니라 ``pending``으로 되돌린다. ``infra_failure_limit``개가 연달아
  나오면 기다렸다가 다시 점검하고, 그래도 안 되면 ``fleet stopped: …`` 한 줄을 남기고 중단한다(exit 3)
- 상태 행의 ``llm_calls`` / 토큰 / ``llm_cost``는 그 프로젝트의 모든 시도 합계이고, 나머지 숫자는 마지막 시도의 값이다

Sequential fleet runner: ONE agent migrates N projects one after the other, feeding one shared KB.

Every project's error list and fix scripts go into the KB (``errors.jsonl``, ``cases/<case>/case.json`` +
``fix_v<N>.py``, PLAN-v2.1 §3.4) before the next project starts, so project k+1 runs the script project k stored for
the same case first (stored script → augment → direct edits, PLAN-v2.1 §3.1) and credits (→ proven) the versions
that work for it.

State lives in ``<out>/fleet/state.json`` (:class:`FleetState`), rewritten atomically after every project,
so a killed fleet resumes where it stopped (``sbmig fleet run`` is resumable by default). Projects are identified by
their input path (``manifest.input_key``), not by their name: a renamed or added folder never inherits another
project's result (round 10).

The numbers of a state row (2026-10-28, A9): ``llm_calls``, ``llm_input_tokens``, ``llm_output_tokens``
and ``llm_cost`` sum EVERY attempt of the project (a retry adds to them, so ``fleet.max_llm_calls_total`` and
the fleet's ``llm_calls_total`` / ``llm_cost_total`` count the calls of earlier attempts too); every other
count (``exit_code``, ``blocked`` / ``blocked_reasons``, ``unresolved``, ``error_counts``, ``flags``,
``advisories``, ``carried_over``) belongs to the last attempt and is cleared when an attempt starts, so a
retry that crashes shows 0 / empty, not the previous attempt's numbers. The ``SBMIG-RESULT`` line's ``llm=``
is the calls of that run (0 for a crash: no report). A row whose input disappeared is ``skipped`` with the
reason whenever this fleet run would start it (pending, ``running``, infra-deferred, incomplete / aborted
under ``--retry-failed``, A8).

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
# 뒤의 어려운 프로젝트가 KB를 더 많이 물려받도록 ``fleet.order: simple-first``는 쉬운 종류부터 실행한다
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
    """일괄 실행 ID ``fleet-YYYYmmdd-HHMMSS``를 만든다.
    이 ID는 실행 ID의 앞부분이 되고, ``sbmig clean --fleet``이 이 접두어로 해당 일괄 실행의 폴더를 고른다."""
    return "fleet-" + datetime.now().strftime("%Y%m%d-%H%M%S")


def exit_code_for(report: RunReport) -> int:
    """보고서의 종료 코드(0 / 2 / 3)를 반환한다.
    규칙은 단일 ``sbmig run``과 같다(:func:`sbmig.cli.run_exit_code`)."""
    from sbmig.cli import run_exit_code

    return run_exit_code(report)


def _status_for(code: int) -> str:
    return {0: "success", 3: "aborted"}.get(code, "incomplete")


UNSUPPORTED_FLAG = "unsupported-build-tool:"


def unsupported_build_tool(report: RunReport) -> Optional[str]:
    """접수 단계가 Maven 프로젝트가 아니라고 거부했으면 그 빌드 도구 이름을 반환한다(플래그
    ``unsupported-build-tool:<tool>``).
    일괄 실행에서는 실패가 아니라 건너뜀으로 처리하므로 다시 시도하지 않고 ``--stop-on-abort``의 이유도 되지 않는다.

    `<tool>` when intake rejected the project as not-Maven (round 7b flag `unsupported-build-tool:<tool>`): in a
    fleet that is a *skip* (no retry, never a reason to stop on abort), not a failed migration (round 9)."""
    return next((f[len(UNSUPPORTED_FLAG):] for f in report.flags if f.startswith(UNSUPPORTED_FLAG)), None)


def infra_kind_for_exception(exc: BaseException) -> Optional[str]:
    """예외로 끝난 실행의 인프라 종류를 반환한다.
    디스크 가득 참은 ``disk``, requests / LLM 전송 오류는 ``llm``, mvn 실행 파일이 없으면 ``maven``이다.
    그 밖에는 실제 실패로 보고 None을 반환한다.

    The infrastructure kind of a crash: a full disk, the LLM transport, a Maven executable that vanished."""
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
    """성공하지 못한 실행의 ``infra:<kind>`` 값을 반환한다.
    성공한 실행은 도중에 LLM이 잠시 끊겼어도 성공으로 남긴다.

    `infra:<kind>` (and the older equivalent flags) of a run that did not end green; a green run keeps its success
    even when the LLM was unavailable for a while."""
    return infra_kind(report.flags) if code != EXIT_ALL_SUCCESS else None


class FleetRunner:
    """매니페스트의 프로젝트를 차례로 실행하는 클래스.
    기본 동작은 프로젝트를 ``<out>/<run_id>/project``로 복사하고 공유 KB로 오케스트레이터 전체를 실행하는 것이다.
    ``run_project`` / ``kb`` / ``analyze`` / ``preflight`` / ``disk_check`` / ``sleep``은 테스트에서 교체할 수 있다.

    Run the projects of a manifest sequentially (see module docstring).

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
        """일괄 실행 전체가 사용하는 FileKnowledgeBase 하나를 반환한다.
        하나를 나눠 쓰면 캐시가 유지되므로 프로젝트마다 KB를 다시 읽지 않는다.

        One FileKnowledgeBase for the whole fleet: its caches (known record ids, parsed case files) stay warm
        across projects. Everything is written to files immediately, so a new instance per project would see the
        same KB — sharing just avoids re-reading it 500 times."""
        if self._kb is None:
            from sbmig.kb import FileKnowledgeBase

            self._kb = FileKnowledgeBase(self.cfg)
        return self._kb

    # ------------------------------------------------------------------ state
    def load_state(self, projects: list[FleetProject]) -> FleetState:
        """``state.json``을 읽고 매니페스트 항목마다 결과 행을 연결한다.
        파일이 없거나 깨졌으면 새 일괄 실행으로 시작한다.
        - 행은 이름이 아니라 입력 경로로 연결한다
        - 입력이 사라졌는데 이번에 실행할 행(pending, 남아 있는 ``running``, 인프라로 미룬 행,
          ``--retry-failed``의 incomplete / aborted)은 이유와 함께 ``skipped``로 바꾼다
        - 실행하지 않을 행(``success``, 재시도 없이 남겨 두는 실패)은 그대로 둔다.
          입력이 다시 생기면 ``pending``으로 되돌리지만 Maven이 아니라서 거부된 행은 ``skipped``로 남긴다"""
        state: Optional[FleetState] = None
        if self.state_path.exists():
            try:
                state = FleetState.model_validate_json(self.state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                log.warning("fleet state %s unreadable (%s); starting a new fleet", self.state_path, e)
        if state is None:
            state = FleetState(fleet_id=new_fleet_id(), manifest=str(self.manifest_path))
        # 이름으로 연결하면 한글 폴더(slug ``project``)나 이름이 바뀐 폴더가 다른 프로젝트의 success를
        # 물려받으므로 입력 경로를 키로 쓴다. 같은 입력이 두 번 있으면 결과를 순서대로 나눠 갖는다
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
            # 입력이 없으면 실행해도 중단으로 끝나므로 이번에 실행할 행은 이유와 함께 건너뛴다.
            # 실행하지 않는 행은 결과를 그대로 둔다
            # input missing (A8): every row this run would start — pending, a `running` left by a dead
            # fleet, an infra deferral, incomplete / aborted under --retry-failed — is skipped with the
            # reason instead of crashing into `aborted`; a success / a kept failure never runs (result stays)
            if not proj.ok:
                if self._action(r) not in ("done", "keep"):
                    r.status, r.error = "skipped", proj.error
            elif r.status == "skipped" and not (r.error or "").startswith("unsupported build tool"):
                r.status, r.error = "pending", None      # the input is back; an intake rejection stays skipped
        state.manifest = str(self.manifest_path)
        state.finished_at = None
        return state

    def save_state(self) -> None:
        """도중에 죽어도 ``state.json``이 반쯤 쓰인 채 남지 않도록 상태를 원자적으로 다시 쓴다.
        일괄 실행 전체의 LLM 호출 수와 비용 합계도 여기서 다시 계산한다."""
        from sbmig.kb.store import atomic_write

        assert self.state is not None
        self.state.updated_at = utcnow()
        self.state.llm_calls_total = sum(p.llm_calls for p in self.state.projects)
        self.state.llm_cost_total = round(sum(p.llm_cost or 0.0 for p in self.state.projects), 6)
        atomic_write(self.state_path, self.state.model_dump_json(indent=2) + "\n")

    # ------------------------------------------------------------------ ordering
    def _case_type(self, proj: FleetProject) -> CaseType:
        """``simple-first`` 순서에 쓸 케이스 종류를 알아낸다.
        입력을 건드리지 않도록 임시 폴더에서 분석만 실행한다."""
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
        """매니페스트 항목에 연결된 상태 행을 반환한다."""
        return self._results[id(proj)]

    def _order(self, projects: list[FleetProject], state: FleetState) -> list[FleetProject]:
        """``fleet.order``에 따라 실행 순서를 정한다. ``manifest``는 매니페스트 순서, ``simple-first``는
        쉬운 종류부터다(순수 자바, 서블릿, 스프링, 부트, MSA).
        분석한 종류는 상태에 캐시한다. 분석이 실패해도 순서 힌트일 뿐이므로 계속 진행한다."""
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
        """상태 행으로 이번에 할 일을 정한다.
        ``success``는 done, ``skipped``는 skip, ``incomplete`` / ``aborted``는 ``fleet.retry_failed``면
        retry 아니면 keep, 그 밖(``pending``, 남아 있는 ``running``)은 run이다."""
        if r.status == "success":
            return "done"
        if r.status == "skipped":
            return "skip"
        if r.status in ("incomplete", "aborted"):
            return "retry" if self.cfg.fleet.retry_failed else "keep"
        return "run"  # pending, or running (the fleet process died during this project)

    # ------------------------------------------------------------------ per-project config
    def project_config(self, proj: FleetProject, remaining_llm: Optional[int]) -> Config:
        """일괄 실행 설정 위에 프로젝트별 ``configs`` / ``overrides``를 덮어써서 프로젝트 하나의 설정을 만든다.
        KB는 하나여야 하므로 ``kb.dir``은 항상 일괄 실행의 값을 쓴다.
        LLM 호출 상한은 남은 예산으로 자르고, 예산을 다 썼으면 -1(LLM 호출 없음)로 둔다."""
        data = self.cfg.model_dump(mode="json")
        from sbmig.core.config import _read_yaml, _warn_unknown_keys   # the same duplicate-key / unknown-key WARNINGs as load_config
        for c in proj.configs:
            data = deep_merge(data, _expand_env(_read_yaml(Path(c))))
        if proj.overrides:
            data = deep_merge(data, _expand_env(proj.overrides))
        if proj.configs or proj.overrides:
            _warn_unknown_keys(data, label=f"fleet project {proj.name}: ")
        if proj.configs or proj.overrides:
            data = _apply_env_overrides(data)
        # 학습이 프로젝트 사이에 이어지려면 KB가 하나여야 하므로 프로젝트 설정의 kb.dir은 무시한다
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
        """``fleet.max_llm_calls_total``에서 지금까지 쓴 호출 수를 뺀 나머지를 반환한다.
        예산이 0 이하(무제한)면 None을 반환한다."""
        total = self.cfg.fleet.max_llm_calls_total
        if total <= 0:
            return None
        assert self.state is not None
        return max(0, total - sum(p.llm_calls for p in self.state.projects))

    # ------------------------------------------------------------------ run
    def _default_run_project(self, proj: FleetProject, cfg: Config, out_dir: Path, run_id: str) -> RunReport:
        """입력을 ``<out>/<run_id>/project``로 복사하고 공유 KB로 오케스트레이터를 실행한다."""
        from sbmig.core.workspace import Workspace
        from sbmig.loop import Orchestrator

        ws = Workspace.from_input(proj.input, out_dir, run_id=run_id)
        return Orchestrator(cfg, ws, kb=self.kb).run()

    def _run_id(self, index: int, proj: FleetProject, attempt: int) -> str:
        """실행 ID ``<fleet id>-<매니페스트 순번>-<이름>``을 만든다. 재시도에는 ``-r<n>``을 붙인다.
        앞선 시도의 실행 폴더를 덮어쓰지 않도록 폴더가 이미 있으면 ``-2`` 같은 번호를 붙인다."""
        assert self.state is not None
        base = f"{self.state.fleet_id}-{index:03d}-{proj.name}" + (f"-r{attempt}" if attempt > 1 else "")
        rid, n = base, 1
        while (self.out_dir / rid).exists():
            n += 1
            rid = f"{base}-{n}"
        return rid

    def fill_result(self, r: FleetProjectResult, report: RunReport, code: int) -> None:
        """끝난 실행의 보고서를 상태 행에 옮긴다.
        종료 코드 0은 success, 3은 aborted, 그 밖은 incomplete이다.
        실행별 숫자는 이번 실행 값으로 덮어쓰고 LLM 사용량은 앞선 시도에 더하므로 시도마다 한 번만 호출해야 한다.

        Copy a finished run's report into its state row. The per-run counts (blocked, unresolved, error list,
        flags) are this run's; the LLM usage (calls, tokens, cost) is ADDED to the attempts before it (A9, see
        the module docstring) — call it once per attempt."""
        r.exit_code = code
        r.status = _status_for(code)  # type: ignore[assignment]
        tool = unsupported_build_tool(report)
        if tool is not None:
            r.status = "skipped"
            r.error = f"unsupported build tool: {tool} (only Maven projects are migrated)"
        r.case_type = report.case_type
        # 덮어쓰면 재시도가 앞선 시도의 호출 수를 지워 예산과 비용 합계가 줄어들므로 더한다
        r.llm_calls += report.llm_usage.calls
        r.llm_input_tokens += report.llm_usage.input_tokens
        r.llm_output_tokens += report.llm_usage.output_tokens
        cost = report.llm_cost.total if getattr(report, "llm_cost", None) is not None else None
        if cost is not None:                          # an unpriced run (None) keeps the earlier cost
            r.llm_cost = (r.llm_cost or 0.0) + cost
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

    @staticmethod
    def _reset_run_counters(r: FleetProjectResult) -> None:
        """새 시도를 시작할 때 앞선 시도의 실행별 숫자(종료 코드, 차단, 미해결, 오류 목록, 플래그 등)를 비운다.
        시도가 비정상 종료하면 보고서가 없으므로 이전 시도의 숫자가 새 결과처럼 보이지 않게 하기 위해서다.
        LLM 사용량은 모든 시도의 합계이므로 그대로 둔다.

        A new attempt starts from empty per-run counts: a crash / interrupt produces no report, so it shows
        0 / empty instead of the previous attempt's numbers (A9). The LLM usage is cumulative and stays."""
        r.exit_code = None
        r.blocked, r.blocked_reasons = 0, []
        r.unresolved = 0
        r.error_counts = {}
        r.flags, r.advisories = [], []
        r.carried_over = 0

    # ------------------------------------------------------------------ output helpers
    def _say(self, msg: str) -> None:
        """운영자용 진행 줄을 화면(``echo``)과 ``fleet/sbmig.log``에 함께 기록한다.
        화면에는 ``echo``가 이미 출력하므로 로그 줄은 ``NO_CONSOLE``로 콘솔 핸들러를 건너뛴다.

        A progress line for the operator (console via `echo`) and for fleet/sbmig.log."""
        self.echo(msg)
        if not self._echo_logs:
            log.info("%s", msg, extra=NO_CONSOLE)

    # ------------------------------------------------------------------ infrastructure guard
    def _preflight_checks(self) -> Optional[list]:
        """사전 점검을 실행하고 결과 줄을 출력한다.
        ``fleet.preflight``가 꺼져 있거나 ``run_project``가 주입되었으면 점검하지 않고 None을 반환한다.

        The checks (printed), or None when no preflight runs (fleet.preflight off / custom runner)."""
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
        """프로젝트를 시작하기 전에 디스크를 점검한다. ``fleet.min_free_gb``가 0 이하면 None을 반환한다."""
        if self._disk_check is not None:
            return self._disk_check()
        if (self.cfg.fleet.min_free_gb or 0) <= 0:
            return None
        from .preflight import check_disk

        return check_disk(self.out_dir, self.cfg.fleet.min_free_gb)

    def _stop_infra(self, kind: str, since: str) -> None:
        """인프라 문제로 일괄 실행을 중단하고 ``fleet stopped: …`` 한 줄을 상태 메모와 화면에 남긴다(종료 코드 3).
        원인을 고친 뒤 같은 명령을 다시 실행하면 미뤄 둔 프로젝트부터 이어서 실행한다."""
        assert self.state is not None
        line = f"fleet stopped: infrastructure {kind} unavailable since {since} - fix it and run sbmig fleet again"
        self.stopped_on_infra = kind
        self.state.notes.append(line)
        self._say(line)

    def _infra_pause(self, kind: str, since: str, *, transient: bool = True) -> bool:
        """``infra_retry_minutes``만큼 기다린 뒤 다시 점검해서 통과하면 True를 반환한다.
        대기 횟수가 ``infra_max_waits``를 넘었거나, 대기 시간이 0분이거나, 다시 점검했을 때 설정 오류가 나오면 False를
        반환한다. 대기 중에 창을 닫아도 이어서 실행할 수 있도록 기다리기 전에 상태와 요약을 저장한다.

        Wait `infra_retry_minutes`, re-check, resume (True) — or stop the fleet (False, the one-line message)."""
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
            # 사전 점검이 꺼져 있으면 디스크만 다시 점검할 수 있으므로 나머지는 기다린 것만으로 다시 시작한다
            if checks is None:                  # fleet.preflight off: only the disk can be re-checked
                disk = self._disk()
                checks = [disk] if disk is not None else []
                what = "fleet.preflight is off (disk re-checked only)"
            bad = [c for c in checks if getattr(c, "failed", False)]
            if not bad:
                self._say(f"fleet: {what} - resuming")
                return True
            # 다시 점검해도 실패했으면 새로 본 종류를 쓰고, 하나라도 설정 오류면 더 기다리지 않고 중단한다
            kind = bad[0].kind or kind
            transient = all(getattr(c, "transient", False) for c in bad)
        self._stop_infra(kind, since)
        return False

    def _initial_preflight(self) -> bool:
        """첫 프로젝트 전에 사전 점검을 실행한다.
        실패하면 인프라 대기로 넘어가고, 설정 오류면 기다리지 않고 중단한다."""
        checks = self._preflight_checks()
        bad = [c for c in checks or [] if getattr(c, "failed", False)]
        if not bad:
            return True
        return self._infra_pause(bad[0].kind or "unknown", "preflight",
                                 transient=all(getattr(c, "transient", False) for c in bad))

    # ------------------------------------------------------------------ per project
    def _attach_run_log(self, run_dir: Path) -> Optional[Path]:
        """이 프로젝트 전용 ``<run dir>/sbmig.log``(DEBUG) 핸들러를 붙인다.
        일괄 실행은 엄격히 순차적이므로 스레드 필터 없이 붙여도 다른 프로젝트의 줄이 섞이지 않는다.

        `<run dir>/sbmig.log` (DEBUG) for this project only — detached after it (round 10)."""
        try:
            from sbmig.cli import attach_file_log

            return attach_file_log(run_dir / "sbmig.log")
        except Exception as e:  # pragma: no cover - logging must never stop a fleet
            log.warning("fleet: per-run log not attached: %s", e)
            return None

    @staticmethod
    def _detach_run_log(path: Optional[Path]) -> None:
        """핸들러가 쌓여 파일이 열린 채 남지 않도록 프로젝트가 끝나면 실행 로그 핸들러를 뗀다."""
        if path is None:
            return
        try:
            from sbmig.cli import detach_file_log

            detach_file_log(path)
        except Exception as e:  # pragma: no cover
            log.warning("fleet: per-run log not detached: %s", e)

    def _cleanup(self, r: FleetProjectResult) -> None:
        """보고서와 ``result.zip``을 쓴 뒤 ``fleet.cleanup``에 따라 디스크를 정리한다.
        실패해도 경고만 남긴다."""
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
        """일괄 실행 전체를 실행한다.
        매니페스트 읽기 -> 상태 연결 -> 순서 정하기(``--dry-run``은 여기서 끝) -> 사전 점검 -> 프로젝트마다
        디스크 점검, LLM 예산, 실행, 결과 기록, ``SBMIG-RESULT`` 줄, 정리, 요약 -> 인프라 대기나 중단 판단.
        상태는 프로젝트 시작 전과 끝난 뒤에 저장하므로 도중에 죽으면 ``running``이 남아 다음에 다시 실행한다."""
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
        # 실행할 프로젝트가 있을 때만 사전 점검을 하고, 모두 끝났으면 요약만 다시 쓴다
        if any(a not in ("done", "skip", "keep") for _i, _r, a in self.planned) and not self._initial_preflight():
            self.save_state()
            self._write_summaries()
            return state
        ran = 0
        total = len(ordered)
        pos_of = {id(p): i for i, p in enumerate(ordered, 1)}
        # 인프라 대기 뒤 미뤄 둔 프로젝트를 앞에 다시 넣기 위해 deque를 쓴다
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
            # 디스크가 부족하면 인프라 대기 후 같은 프로젝트를 큐 앞에 다시 넣는다
            disk = self._disk()
            if disk is not None and getattr(disk, "failed", False):
                self._say(disk.line())
                if not self._infra_pause("disk", proj.name):
                    break
                queue.appendleft(proj)
                continue
            # LLM 예산을 다 써도 project_config가 LLM 없이 실행하도록 설정하므로 메모만 남기고 실행한다
            remaining = self._remaining_llm()
            if remaining == 0 and f"llm-budget-exhausted:{proj.name}" not in state.notes:
                state.notes.append(f"llm-budget-exhausted:{proj.name}")
            # 재시도면 실행 ID에 ``-r<n>``이 붙도록 attempt를 늘린다
            if r.status in ("incomplete", "aborted", "running") or (r.error or "").startswith(INFRA_PREFIX):
                r.attempt += 1
            r.status, r.error = "running", None
            r.started_at, r.finished_at = utcnow(), None
            self._reset_run_counters(r)          # A9: a crash must not show the last attempt's numbers
            run_id = self._run_id(index_of[id(proj)], proj, r.attempt)
            r.run_id = run_id
            r.out_dir = str(self.out_dir / run_id)
            # 여기서 프로세스가 죽어도 다음에 이 프로젝트를 다시 실행하도록 시작 전에 ``running``으로 저장한다
            self.save_state()
            self._say(f"[{pos}/{total}] {proj.name}: running ({run_id})")
            t0 = time.monotonic()
            kind: Optional[str] = None
            build_ok = False
            reported = False                     # a report came back: the result line carries open= / resolved=
            run_llm = 0                          # this attempt's LLM calls (r.llm_calls: every attempt, A9)
            run_log = self._attach_run_log(self.out_dir / run_id)
            try:
                pcfg = self.project_config(proj, remaining)
                runner = self._run_project or self._default_run_project
                report = runner(proj, pcfg, self.out_dir, run_id)
                code = exit_code_for(report)
                self.fill_result(r, report, code)
                build_ok, reported = bool(report.success), True
                run_llm = report.llm_usage.calls
                kind = report_infra_kind(report, code) if r.status != "skipped" else None
            except KeyboardInterrupt:
                # Ctrl-C면 다음에 다시 실행하도록 이 프로젝트를 pending(interrupted)으로 저장하고 예외를 다시 던진다
                r.status, r.error = "pending", "interrupted"
                r.duration_s = round(time.monotonic() - t0, 1)
                self.save_state()
                raise
            except Exception as e:
                # 예외로 끝난 실행은 중단(exit 3)으로 기록한다. 인프라 원인이면 아래에서 pending으로 바뀐다
                log.exception("fleet: project %s crashed", proj.name)
                r.status, r.exit_code = "aborted", 3
                tb = traceback.format_exception_only(type(e), e)[-1].strip()
                r.error = f"crash: {tb}"[:500]
                kind = infra_kind_for_exception(e)
            finally:
                self._detach_run_log(run_log)
            # 인프라 원인이면 실패로 세지 않고 인프라가 돌아왔을 때 다시 실행하도록 pending으로 둔다
            if kind:
                r.status, r.error = "pending", INFRA_PREFIX + kind     # re-run when the infrastructure is back
            r.finished_at = utcnow()
            r.duration_s = round(time.monotonic() - t0, 1)
            ran += 1
            self.save_state()
            o_err, r_err = counts_open_resolved(r.error_counts)
            # SBMIG-RESULT의 llm=은 이번 실행의 호출 수이고, 상태 행은 모든 시도의 합계이다
            self._say(result_line(run_id, r.exit_code, build_ok, unresolved=r.unresolved, blocked=r.blocked,
                                  llm_calls=run_llm, attention=result_attention(r),
                                  open_errors=o_err if reported else None, resolved_errors=r_err if reported else None))
            llm_all = f" (all attempts {r.llm_calls})" if r.llm_calls != run_llm else ""
            self._say(
                f"[{pos}/{total}] {proj.name}: {r.status} (exit {r.exit_code}, {r.duration_s:.0f}s, "
                f"llm {run_llm}{llm_all}, errors open {o_err} / resolved {r_err}, blocked {r.blocked}, "
                f"unresolved {r.unresolved})"
                + (f" - {r.error}" if kind else "")
            )
            # cleanup_run은 보고서와 result.zip이 다 쓰인 것을 전제로 하므로 그 뒤에 정리하고,
            # ``fleet.reindex_every`` 프로젝트마다 KB를 재색인한다
            self._cleanup(r)
            if fc.reindex_every and ran % fc.reindex_every == 0:
                try:
                    if hasattr(self.kb, "reindex"):
                        self.kb.reindex()
                except Exception as e:  # pragma: no cover
                    log.warning("fleet: kb reindex failed: %s", e)
            self._write_summaries()
            # 인프라 원인이 ``infra_failure_limit``개 연달아 나오면 기다렸다가 다시 점검하고, 미룬
            # 프로젝트를 원래 순서로 큐 앞에 넣는다. 같은 프로젝트는 MAX_INFRA_REQUEUES번까지만 넣는다
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
            # 정상 인프라에서 프로젝트가 실행되었으므로 연속 실패 수와 대기 횟수를 0으로 되돌린다
            streak.clear()
            self._waits = 0                     # a project ran on healthy infrastructure
            if r.status == "aborted" and fc.stop_on_abort:
                self.stopped_on_abort = True
                state.notes.append(f"stopped on abort of {proj.name} (fleet.stop_on_abort)")
                break
        # 모든 행이 끝난 상태이고 중단되지 않았으면 일괄 실행 완료 시각을 기록한다
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
        """요약 파일(summary.md / .csv / .json, learning.md)을 다시 기록한다. 실패해도 경고만 남기고 계속 진행한다."""
        from .summary import write_summaries

        try:
            write_summaries(self.state, self.fleet_dir, self.cfg, kb=self._kb_or_none())
        except Exception as e:  # summaries must never stop the fleet
            log.warning("fleet: writing summaries failed: %s", e)

    def _kb_or_none(self) -> Any:
        """learning.md에 쓸 KB를 반환한다. ``run_project``만 주입되어 KB가 없으면 None을 반환한다."""
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
        """일괄 실행의 종료 코드를 반환한다.
        인프라 문제로 중단했으면 3이고, 그 밖은 :func:`fleet_exit_code`를 따른다."""
        if self.stopped_on_infra:
            return EXIT_STOPPED_ON_INFRA
        return fleet_exit_code(self.state, self.stopped_on_abort)


def fleet_exit_code(state: Optional[FleetState], stopped_on_abort: bool = False) -> int:
    """상태로부터 종료 코드를 정한다.
    ``--stop-on-abort``로 중단했으면 3, 모두 ``success``면 0, 그 밖(건너뜀 포함)은 2이다."""
    if stopped_on_abort:
        return EXIT_STOPPED_ON_ABORT
    if state is not None and state.projects and all(p.status == "success" for p in state.projects):
        return EXIT_ALL_SUCCESS
    return EXIT_INCOMPLETE
