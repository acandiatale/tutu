"""이관 한 번을 LangGraph 노드 순서대로 실행하는 모듈. 기본 엔진(``loop.engine: graph``)이다.

Node order: `full` = intake -> analyze -> plan -> phase (N times) -> finalize -> remediation; `remediation` =
intake -> remediation -> finalize. Each node calls one ``step_*`` method of the orchestrator, so the report,
the journal and the exit code are the inline engine's. The graph state holds small JSON values only. Nodes
handle no errors (build gate): aborts and exceptions go to the orchestrator's run guard. In `full` the last
``remediation`` node calls ``sbmig.remediations.chain`` only after a BUILD SUCCESS with
``remediation.after_migration`` on; its result never changes the migration's verdict or exit code, and it
raises nothing. Entry points: `build_graph`, `run_graph` (CLI), `stream_graph` (the web UI's SSE). A stop
takes effect between nodes only.

- The remediation node runs before the run guard's `_finalize` (report.md, result.zip, KB), so its summary
  lands on `RunReport.remediation` and the report renders it.
- Heavy run objects (the `RunReport`, the phase groups with their `PlanStep`s, the `BuildFixLoop`) stay on the
  `Orchestrator` and on a per-run holder closed over by the node functions. Graph and inline engines produce
  identical reports, journals and exit codes (pinned by tests).
- `_RunAborted`, `MissingComponentError` and any other exception propagate out of the graph to the run guard.
- The `phase` node runs whatever phase groups the plan has (normally transform -> restructure incl. the
  structure review -> verify); `stop_after` accepts every phase of
  `sbmig.loop.orchestrator.STOP_AFTER_PHASES`.
- Every node runs inside its log stage (`sbmig.core.stage`: `intake`, `analyze`, `plan`, the phase group's
  name, `report` for finalize), set inside the node because LangGraph may run it in an executor thread with
  a copied context; the orchestrator's steps set the finer stages (`rewrite`, `restructure`, `review`,
  `build`, `fix r3 …`).
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from sbmig.core.models import FLOW_FULL, FLOW_REMEDIATION, Phase, PhaseResult, RunReport
from sbmig.core.stage import reset_stage, set_stage
from sbmig.core.workspace import STOP_REQUEST_FILE as _STOP_REQUEST_FILE
from sbmig.core.workspace import STOP_REQUEST_REASON as _STOP_REQUEST_REASON

if TYPE_CHECKING:
    from sbmig.loop.orchestrator import Orchestrator

log = logging.getLogger("sbmig.graph")

# 웹 UI가 이 노드 이름으로 진행 상황을 그린다
NODES = ("intake", "analyze", "plan", "phase", "finalize", "remediation")
# 실행 방식이 remediation일 때 이 순서로 실행되는 노드
REMEDIATION_NODES = ("intake", "remediation", "finalize")
# MSA는 서비스마다 phase를 반복한다. LangGraph 기본값 25로는 모자라서 넉넉하게 둔다
RECURSION_LIMIT = 1000


class MigrationState(TypedDict, total=False):
    """그래프 상태를 담는 클래스. 남은 단계 묶음, 현재 단계, 중단 이유, UI용 요약 같은 작은 JSON 값만 담는다.
    노드가 갱신하고 `stream_graph`가 웹 UI로 넘긴다."""
    run_id: str
    stop_after: str | None          # Phase 값, 없으면 None
    dry_run: bool
    phases: list[str]                  # 남은 단계 묶음 이름, MSA는 서비스마다 반복된다
    phase: str | None               # 방금 실행했거나 실행 중인 단계 묶음
    aborted: str | None             # intake / analyze의 중단 이유, phase의 중단은 예외로 던진다
    stopped: bool                      # `stop_after`에 도달했는지
    summary: dict                      # UI용 작은 요약
    remediation: dict                  # 이어서 실행한 취약점 조치의 {status, reason}, 실행했거나 건너뛰었을 때만


# ---------------------------------------------------------------------------- 도우미
def _stop_phase(state: MigrationState) -> Phase | None:
    """상태의 stop_after 문자열을 Phase로 바꾼다. 없으면 None을 반환한다."""
    v = state.get("stop_after")
    return Phase(v) if v else None


def _report(orch: Orchestrator) -> RunReport:
    """오케스트레이터의 현재 RunReport를 반환한다. 실행 밖에서 노드를 호출하면 RuntimeError를 던진다."""
    report = orch.report
    if report is None:
        raise RuntimeError("graph node called outside an orchestrator run (orch.report is None)")
    return report


def _summary(orch: Orchestrator, report: RunReport) -> dict:
    """UI용 작은 요약(프로젝트 종류, 단계별 결과, LLM 호출 수, 차단과 미해결 수)을 만든다."""
    phases = [{"phase": pr.phase.value, "ok": pr.ok, "build_ok": pr.build_ok, "skipped": pr.skipped,
               "remaining_errors": len(pr.remaining_errors)} for pr in report.phases]
    return {
        "case_type": report.case_type.value,
        "phases": phases,
        "llm_calls": orch.budget.llm_calls,
        "blocked": len(orch.blocked),
        "unresolved": len(report.phases[-1].remaining_errors) if report.phases else 0,
    }


def _halted(orch: Orchestrator, report: RunReport, ok: bool) -> dict:
    """단계가 끝난 뒤의 상태 갱신 값을 만든다. `ok`가 True면 중단도 중지도 아니다.
    `ok`가 False이고 보고서 플래그에 aborted가 있으면 중단(aborted)이다. 없으면 stop_after에 따른 중지(stopped)다."""
    if ok:
        return {"stopped": False, "aborted": None}
    if "aborted" in report.flags:
        return {"stopped": False, "aborted": orch.abort_reason or "aborted"}
    return {"stopped": True, "aborted": None}


def _journal_node(orch: Orchestrator, node: str, phase: str | None = None) -> None:
    """노드 시작을 저널에 `graph_node` 이벤트로 기록한다."""
    orch.ws.journal({"event": "graph_node", "node": node, "phase": phase})


# Job 모드의 중지 요청 파일. 웹 파드는 다른 파드의 프로세스에 신호를 보낼 수 없어서 공유 실행 폴더에 이 파일을 둔다.
# 엔진은 노드 사이에서 이 파일을 확인한다. 동작은 in-process 중지와 같다(최종 빌드 없음, aborted 표시)
STOP_REQUEST_FILE = _STOP_REQUEST_FILE
STOP_REQUEST_REASON = _STOP_REQUEST_REASON


def stop_requested(orch: Orchestrator) -> bool:
    """실행 폴더에 Job 모드의 중지 요청 파일 ``stop.request``가 있는지 확인한다. 예외를 던지지 않는다."""
    try:
        fn = getattr(orch.ws, "stop_requested", None)
        return bool(fn()) if callable(fn) else (orch.ws.run_dir / STOP_REQUEST_FILE).is_file()
    except OSError:  # 실행 폴더를 읽을 수 없으면 중지 요청으로 보지 않는다
        return False


def _journal_node_end(orch: Orchestrator, node: str, out: dict) -> None:
    """노드 끝을 저널에 `graph_node_end` 이벤트로 기록한다. 예외를 던지지 않는다.
    Job 모드의 웹이 이 줄을 화면의 ``node`` 이벤트로 바꾼다."""
    try:
        orch.ws.journal({"event": "graph_node_end", "node": node, "phase": out.get("phase"),
                         "phases": out.get("phases"), "aborted": out.get("aborted"), "stopped": out.get("stopped")})
    except Exception:  # 저널 기록 실패로 노드를 실패시키지 않는다
        log.debug("graph_node_end journal line for %s not written", node, exc_info=True)


def _abort_text(orch: Orchestrator, e: BaseException) -> str:
    """노드에서 던진 예외를 화면의 단계 메모로 쓸 문장으로 바꾼다.
    실행 중단(`_RunAborted`)은 사유만 쓴다. 다른 예외는 `<클래스>: <메시지>`로 쓴다."""
    from sbmig.loop.orchestrator import _RunAborted
    if isinstance(e, _RunAborted):
        return str(e) or getattr(orch, "abort_reason", None) or "aborted"
    return f"{type(e).__name__}: {e}"


def _staged(name: str | Callable[[MigrationState], str], fn: Callable[[MigrationState], dict],
            orch: Orchestrator | None = None) -> Callable[[MigrationState], dict]:
    """노드 함수를 로그 단계(`name` 또는 `name(state)`) 안에서 실행하는 함수를 만든다.
    단계는 노드 안에서 설정하고, 노드가 어떻게 끝나든 되돌린다. LangGraph가 노드를 다른 스레드에서 실행할 수 있다.
    ``orch``를 주면 노드가 끝날 때마다 ``graph_node_end`` 저널 줄을 남긴다. 예외로 끝나도 남긴다."""
    node_id = getattr(fn, "__name__", "node")

    def node(state: MigrationState) -> dict:
        token = set_stage(name(state) if callable(name) else name)
        try:
            out = fn(state)
            if orch is not None:
                _journal_node_end(orch, node_id, out or {})
            return out
        except BaseException as e:            # 예외로 끝난 노드도 끝 줄을 남긴다. Job 모드 UI가 이 줄을 본다
            if orch is not None:
                _journal_node_end(orch, node_id, {"aborted": _abort_text(orch, e)[:300]})
            raise
        finally:
            reset_stage(token)
    node.__name__ = node_id
    return node


# ---------------------------------------------------------------------------- 취약점 조치 이어 실행
def remediation_enabled(cfg: Any) -> bool:
    """``remediation.after_migration``이 켜져 있는지 반환한다. 설정에 그 절이 없으면 False를 반환한다."""
    rem = getattr(cfg, "remediation", None)
    return bool(getattr(rem, "after_migration", False)) if rem is not None else False


def migration_build_targets(orch: Any) -> list[str]:
    """이관의 최종 빌드(``verify.build`` 단계)가 빌드한 폴더 목록을 반환한다. 대상이 없는 단계는 ``"."``이다.
    취약점 조치가 같은 대상을 같은 방식으로 빌드할 때 쓴다. 계획을 읽을 수 없으면 빈 목록을 반환한다."""
    try:
        steps = orch._verify_steps()
    except Exception:  # 계획을 읽을 수 없으면 빈 목록, 즉 루트를 빌드한다
        log.debug("migration build targets not read", exc_info=True)
        return []
    return [str(getattr(s, "target", None) or ".") for s in steps]


def _chain_remediation(orch: Orchestrator, report: RunReport, state: MigrationState, *, planned: bool,
                       user_stopped: bool) -> dict | None:
    """이관이 끝난 뒤 취약점 조치를 이어서 실행하고, 요약을 ``report.remediation``에 둔다.
    조건이 하나라도 맞지 않으면 아무것도 기록하지 않고 None을 반환한다.
    - 조건: 설정 켜짐, dry-run 아님, ``stop_after`` 없음, plan까지 진행, 사용자 중지와 중단 없음, 이관 성공
    - 빌드가 실패한 프레임워크 프로젝트도 ``Orchestrator.remediation_after_failed_build``가 참이면 이어 간다
    - ``baseline_may_fail``은 그런 실행에서 최종 빌드가 실제로 실패했을 때만 True다
    - ``stop.request``가 있으면 조치를 시작하지 않고 건너뛴 것으로 기록한다"""
    if not remediation_enabled(orch.cfg):
        return None
    if state.get("dry_run") or "dry-run" in report.flags or state.get("stop_after"):
        return None
    if not planned or user_stopped or state.get("aborted") or state.get("stopped") or "aborted" in report.flags:
        return None
    # 프레임워크 프로젝트는 빌드가 실패해도 조치를 이어서 실행한다. 오픈소스 라이브러리 검사라서 빌드와 상관없다
    after_failure = False
    if not orch.build_succeeded(report):
        after_failure = orch.remediation_after_failed_build(report)
        if not after_failure:
            return None
    from sbmig.remediations import chain

    skipped: dict[str, Any] = {}

    def journal(event: dict) -> None:
        if event.get("event") == "remediation_skipped":
            skipped.update(event)
        orch.ws.journal(event)

    if stop_requested(orch):
        journal({"event": "remediation_skipped", "reason": "stop_requested", "scope": chain.SCOPE})
        log.warning("vulnerability remediation not started: %s is present", STOP_REQUEST_FILE)
        rs = None
    else:
        _journal_node(orch, "remediation", None)
        t0 = orch.clock()
        rs = chain.run_after_migration(orch.ws.run_dir, orch.ws.root, orch.cfg, run_id=orch.ws.run_id, journal=journal,
                                       stop=lambda: stop_requested(orch),
                                       exclude_dirs=list(getattr(orch.ws, "ignored_dirs", None) or []),
                                       build_targets=migration_build_targets(orch),
                                       baseline_may_fail=after_failure and orch.final_build_failed)
        report.timings_s["remediation"] = round(orch.clock() - t0, 3)
        if rs is None and not skipped:            # 조치가 꺼져 있었다. 설정이 켜져 있으면 생기지 않는다
            return None
    summary = chain.summary(rs, orch.ws.run_dir, skipped=skipped)
    report.remediation = summary
    return summary


# ---------------------------------------------------------------------------- 그래프
def _user_stop_fn(orch: Orchestrator, holder: dict[str, Any]) -> Callable[[str, str | None], dict | None]:
    """노드를 실행하기 전에 ``stop.request``를 확인하는 함수를 만든다.
    파일이 있으면 보고서를 aborted로 표시한다. 그리고 그 노드를 건너뛰고 finalize로 가는 상태 갱신 값을 반환한다.
    파일이 없거나 이미 중지했으면 None을 반환한다."""

    def user_stop(node: str, phase: str | None) -> dict | None:
        if holder["user_stopped"] or not stop_requested(orch):
            return None
        report = _report(orch)
        holder["user_stopped"] = True
        log.warning("stop requested (%s) — stopping before node %s (no final build)", STOP_REQUEST_FILE, node)
        try:
            orch.ws.journal({"event": "stop_request", "node": node, "file": STOP_REQUEST_FILE})
        except Exception:  # 저널 줄은 참고용이다. 실패해도 넘어간다
            log.debug("stop_request journal line not written", exc_info=True)
        if "aborted" not in report.flags:
            orch._abort(report, STOP_REQUEST_REASON)   # 웹의 in-process 중지와 같은 표시를 한다
        return {"stopped": False, "aborted": orch.abort_reason or STOP_REQUEST_REASON, "phase": phase,
                "summary": _summary(orch, report)}
    return user_stop


def _build_remediation_graph(orch: Orchestrator) -> Any:
    """취약점 조치만 하는 실행(``remediation``)의 그래프를 만든다. intake -> remediation -> finalize 순서다.
    intake가 실패하면 바로 finalize로 간다.
    finalize 노드는 상태만 표시한다. 보고서는 run guard의 마무리(``_finalize``)가 쓴다."""
    holder: dict[str, Any] = {"user_stopped": False}
    user_stop = _user_stop_fn(orch, holder)

    def intake(state: MigrationState) -> dict:
        """프로젝트를 바꾸지 않는 접수(사전 점검과 빌드 도구 확인)만 실행한다."""
        if (stop := user_stop("intake", Phase.INTAKE.value)) is not None:
            return {**stop, "phases": []}
        _journal_node(orch, "intake", Phase.INTAKE.value)
        report = _report(orch)
        ok = orch.step_intake(report, None)
        return {**_halted(orch, report, ok), "phases": [], "phase": Phase.INTAKE.value,
                "summary": _summary(orch, report)}

    def remediation(state: MigrationState) -> dict:
        """``Orchestrator.step_remediation``(``chain.run_only``)으로 취약점 조치를 실행한다."""
        if (stop := user_stop("remediation", None)) is not None:
            return stop
        _journal_node(orch, "remediation", None)
        report = _report(orch)
        summary = orch.step_remediation(report)
        out: dict = {"phase": None, "remediation": {k: summary.get(k) for k in ("status", "reason")},
                     "summary": _summary(orch, report)}
        if "aborted" in report.flags:
            out["aborted"] = orch.abort_reason or "aborted"
        return out

    def finalize(state: MigrationState) -> dict:
        """마무리 상태만 표시한다. 보고서는 run guard가 쓴다."""
        _journal_node(orch, "finalize", None)
        return {"phase": None, "summary": _summary(orch, _report(orch))}

    def after_intake(state: MigrationState) -> str:
        return "finalize" if state.get("stopped") or state.get("aborted") else "next"

    g = StateGraph(MigrationState)
    g.add_node("intake", _staged("intake", intake, orch))
    g.add_node("remediation", _staged("remediation", remediation, orch))
    g.add_node("finalize", _staged("report", finalize, orch))
    g.add_edge(START, "intake")
    g.add_conditional_edges("intake", after_intake, {"next": "remediation", "finalize": "finalize"})
    g.add_edge("remediation", "finalize")
    g.add_edge("finalize", END)
    return g.compile()


def build_graph(orch: Orchestrator, flow: str | None = None) -> Any:
    """`orch`의 단계를 실행하는 컴파일된 StateGraph(MigrationState)를 만든다.
    그래프는 `orch`에 묶여 있다. 오케스트레이터 실행 안에서만 실행해야 한다.
    ``flow``가 ``remediation``이면 intake -> remediation -> finalize 그래프를 만든다.

    An orchestrator run = `orch.run()`, or `run_graph` / `stream_graph`, which open one."""
    if (flow or getattr(orch, "flow", FLOW_FULL)) == FLOW_REMEDIATION:
        return _build_remediation_graph(orch)
    holder: dict[str, Any] = {"groups": [], "planned": False, "user_stopped": False}

    user_stop = _user_stop_fn(orch, holder)

    def intake(state: MigrationState) -> dict:
        """입력을 검사하고 작업 공간을 준비한다(`step_intake`). 실패하거나 stop_after면 finalize로 간다."""
        holder["groups"] = []
        holder["planned"] = False
        if (stop := user_stop("intake", Phase.INTAKE.value)) is not None:
            return {**stop, "phases": []}
        _journal_node(orch, "intake", Phase.INTAKE.value)
        report = _report(orch)
        ok = orch.step_intake(report, _stop_phase(state))
        return {**_halted(orch, report, ok), "phases": [], "phase": Phase.INTAKE.value,
                "summary": _summary(orch, report)}

    def analyze(state: MigrationState) -> dict:
        """프로젝트 프로필을 만든다(`step_analyze`)."""
        if (stop := user_stop("analyze", Phase.ANALYZE.value)) is not None:
            return stop
        _journal_node(orch, "analyze", Phase.ANALYZE.value)
        report = _report(orch)
        ok = orch.step_analyze(report, _stop_phase(state))
        return {**_halted(orch, report, ok), "phase": Phase.ANALYZE.value, "summary": _summary(orch, report)}

    def plan(state: MigrationState) -> dict:
        """계획을 만들고 단계 묶음을 holder에 담는다(`step_plan`).
        plan 뒤에 중지하면 phase 없이 finalize로 간다."""
        if (stop := user_stop("plan", Phase.PLAN.value)) is not None:
            return {**stop, "phases": []}
        _journal_node(orch, "plan", Phase.PLAN.value)
        report = _report(orch)
        groups = orch.step_plan(report, _stop_phase(state), bool(state.get("dry_run")))
        if groups is None:                    # plan 뒤에서 중지했다
            return {"stopped": True, "phases": [], "phase": Phase.PLAN.value, "summary": _summary(orch, report)}
        holder["groups"] = list(groups)
        holder["planned"] = True
        return {"stopped": False, "phases": [p.value for p, _ in groups], "phase": Phase.PLAN.value,
                "summary": _summary(orch, report)}

    def phase(state: MigrationState) -> dict:
        """남은 단계 묶음 하나를 실행한다(`step_phase`). transform, restructure, verify 같은 묶음이다.
        중단은 예외로 넘긴다."""
        report = _report(orch)
        if (stop := user_stop("phase", holder["groups"][0][0].value if holder["groups"] else None)) is not None:
            return {**stop, "phases": [p.value for p, _ in holder["groups"][1:]]}   # 건너뛴 단계는 남은 단계에서 뺀다
        ph, items = holder["groups"].pop(0)
        _journal_node(orch, "phase", ph.value)
        pr: PhaseResult | None = orch.step_phase(report, ph, items, _stop_phase(state))  # _RunAborted가 날 수 있다
        out: dict = {"phases": [p.value for p, _ in holder["groups"]], "phase": ph.value,
                     "summary": _summary(orch, report)}
        if pr is None:
            out["summary"]["last"] = {"phase": ph.value, "skipped": "stop_after"}
        return out

    def finalize(state: MigrationState) -> dict:
        """plan까지 진행했으면 `step_finish`로 최종 빌드, 보고서, KB 기록을 실행한다. 조건은 inline 엔진과 같다.
        사용자 중지가 있었으면 최종 빌드를 하지 않는다."""
        _journal_node(orch, "finalize", None)
        report = _report(orch)
        # 마지막 phase 중에 들어온 중지 요청도 여기서 확인한다. 수정 루프는 빌드 라운드 사이에서 멈추고 여기로 온다.
        # 최종 빌드가 모두 끝난 뒤의 요청은 무시한다. 루프를 멈춘 요청은 중단으로 처리한다(inline 엔진과 같은 규칙)
        if not holder["user_stopped"] and stop_requested(orch):
            late = orch.late_stop(report) if holder["planned"] else "aborted"
            if not holder["planned"]:
                user_stop("finalize", None)
            elif late == "aborted":
                holder["user_stopped"] = True
        if holder["planned"] and not holder["user_stopped"]:
            orch.step_finish(report, _stop_phase(state))
        return {"phase": None, "summary": _summary(orch, report)}

    def remediation(state: MigrationState) -> dict:
        """조건이 맞으면 취약점 조치를 이어서 실행한다(`_chain_remediation`). 아니면 바로 반환한다.
        조치의 결과나 예외는 이관 결과를 바꾸지 않는다."""
        report = _report(orch)
        try:
            summary = _chain_remediation(orch, report, state, planned=holder["planned"],
                                         user_stopped=holder["user_stopped"])
        except Exception:  # noqa: BLE001 - 취약점 조치가 이관 결과를 바꾸지 않게 한다
            log.exception("vulnerability remediation node failed (the migration result is unchanged)")
            summary = None
        out: dict = {"phase": None}
        if summary is not None:
            out["remediation"] = {k: summary.get(k) for k in ("status", "reason")}
        return out

    # 중지나 중단이면 남은 노드를 건너뛰고 finalize로 간다
    # phase는 남은 단계 묶음이 없을 때까지 자기 자신으로 돌아온다
    def after_step(state: MigrationState) -> str:
        return "finalize" if state.get("stopped") or state.get("aborted") else "next"

    def after_plan(state: MigrationState) -> str:
        if state.get("stopped") or state.get("aborted"):
            return "finalize"
        return "phase" if holder["groups"] else "finalize"

    def after_phase(state: MigrationState) -> str:
        return "phase" if holder["groups"] and not (state.get("stopped") or state.get("aborted")) else "finalize"

    def phase_stage(state: MigrationState) -> str:
        """phase 노드의 로그 단계 이름으로 쓸, 이번에 실행할 단계 묶음 이름을 반환한다."""
        return holder["groups"][0][0].value if holder["groups"] else "phase"

    g = StateGraph(MigrationState)
    g.add_node("intake", _staged("intake", intake, orch))
    g.add_node("analyze", _staged("analyze", analyze, orch))
    g.add_node("plan", _staged("plan", plan, orch))
    g.add_node("phase", _staged(phase_stage, phase, orch))
    g.add_node("finalize", _staged("report", finalize, orch))
    # 취약점 조치가 꺼져 있으면 remediation 노드는 graph_node_end 줄을 남기지 않는다. 로컬 실행마다 줄이 쌓이지 않게 한다
    g.add_node("remediation", _staged("remediation", remediation, orch if remediation_enabled(orch.cfg) else None))
    g.add_edge(START, "intake")
    g.add_conditional_edges("intake", after_step, {"next": "analyze", "finalize": "finalize"})
    g.add_conditional_edges("analyze", after_step, {"next": "plan", "finalize": "finalize"})
    g.add_conditional_edges("plan", after_plan, {"phase": "phase", "finalize": "finalize"})
    g.add_conditional_edges("phase", after_phase, {"phase": "phase", "finalize": "finalize"})
    g.add_edge("finalize", "remediation")
    g.add_edge("remediation", END)
    return g.compile()


def _initial_state(orch: Orchestrator, stop_after: Phase | None, dry_run: bool) -> MigrationState:
    """그래프의 첫 상태를 만든다."""
    return MigrationState(run_id=orch.ws.run_id, stop_after=stop_after.value if stop_after is not None else None,
                          dry_run=bool(dry_run), phases=[], phase=None, aborted=None, stopped=False, summary={})


def _guard(orch: Orchestrator, dry_run: bool, flow: str = FLOW_FULL, framework_mode: str | None = None) -> Any:
    """직접 호출되었으면 run guard를 열어 보고서 생성과 마무리를 보장한다.
    이미 `orch.run()` 안이면 아무것도 하지 않는 컨텍스트를 반환한다.
    ``framework_mode``는 처리 방법을 줄 때만 넘긴다. 이전 오케스트레이터 대역이 이 인자를 모를 수 있다."""
    if orch.running:
        return nullcontext(orch.report)
    if framework_mode is None:
        return orch._run_guard(dry_run, flow=flow)
    return orch._run_guard(dry_run, flow=flow, framework_mode=framework_mode)


def _check(orch: Orchestrator, stop_after: Phase | None, dry_run: bool, flow: str | None) -> str:
    """실행 방식(``check_flow``)을 검사한다. 이미 `orch.run()` 안이면 그 실행의 방식을 쓴다."""
    from sbmig.loop.orchestrator import check_flow
    if orch.running:
        return getattr(orch, "flow", FLOW_FULL)
    return check_flow(flow, stop_after, dry_run)


def run_graph(orch: Orchestrator, stop_after: Phase | None = None, dry_run: bool = False,
              flow: str = FLOW_FULL, framework_mode: str | None = None) -> RunReport:
    """그래프로 이관을 실행한다(CLI 경로). 결과는 `orch.report`에 남는다.
    `orch.run()` 안에서는 모든 예외가 `run()`의 guard로 넘어간다. 직접 호출하면 같은 guard를 스스로 연다.
    ``flow="remediation"``을 ``stop_after``나 ``dry_run``과 함께 쓰면 ValueError를 던진다.
    ``framework_mode``는 사내 프레임워크 처리 방법이다. 모르는 값이면 ValueError를 던진다."""
    flow = _check(orch, stop_after, dry_run, flow)
    with _guard(orch, dry_run, flow, framework_mode):
        # 접수에서 거절한 작업 공간은 그 사유로 중단하고 노드를 실행하지 않는다
        if orch.refused_at_intake(orch.report):
            return _report(orch)
        graph = build_graph(orch, flow)
        graph.invoke(_initial_state(orch, stop_after, dry_run), config={"recursion_limit": RECURSION_LIMIT})
    return _report(orch)


def stream_graph(orch: Orchestrator, stop_after: Phase | None = None,
                 dry_run: bool = False, flow: str = FLOW_FULL,
                 framework_mode: str | None = None) -> Iterator[tuple[str, dict]]:
    """그래프로 이관을 실행하면서 노드마다 (노드 이름, 상태 갱신)을 내보낸다. 웹 UI의 SSE `node` 이벤트가 된다.
    반복이 끝나거나 일찍 닫혀도 guard가 보고서를 마무리한다. 잘못된 ``flow`` 조합은 첫 ``next``에서 ValueError를 던진다.
    `full`에서 아무것도 하지 않은 remediation 노드는 내보내지 않는다.
    ``framework_mode``는 사내 프레임워크 처리 방법이다. 모르는 값은 첫 ``next``에서 ValueError를 던진다.

    LangGraph ``stream_mode="updates"``; the finalized report is `orch.report`. Inside `orch.run()` the run's
    own `framework_mode` stands and the keyword is ignored."""
    flow = _check(orch, stop_after, dry_run, flow)
    with _guard(orch, dry_run, flow, framework_mode):
        # 접수에서 거절한 작업 공간은 그 사유로 중단하고 노드를 실행하지 않는다
        if orch.refused_at_intake(orch.report):
            return
        graph = build_graph(orch, flow)
        for chunk in graph.stream(_initial_state(orch, stop_after, dry_run), stream_mode="updates",
                                  config={"recursion_limit": RECURSION_LIMIT}):
            for node, update in chunk.items():
                # 웹의 노드 목록이 바뀌지 않도록 아무것도 하지 않은 remediation 노드는 내보내지 않는다
                if flow == FLOW_FULL and node == "remediation" and "remediation" not in (update or {}):
                    continue
                yield node, dict(update or {})
