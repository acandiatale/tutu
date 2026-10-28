"""LangGraph 실행 엔진 — 노드 intake → analyze → plan → phase(×N) → finalize → END 로 이관 한 번을 몬다.
회사 관례(에이전트는 LangGraph 그래프)를 따르는 기본 엔진(`loop.engine: graph`)이다. 노드마다 오케스트레이터의
같은 `step_*` 메서드 하나를 부르므로 inline 엔진과 보고서·저널·종료 코드가 똑같다(테스트로 보장). 그래프
상태에는 작은 JSON 값만 두고 무거운 객체(RunReport, 단계 묶음, 수정 루프)는 오케스트레이터에 남긴다. 노드는
오류를 처리하지 않는다 — 중단·예외는 그대로 오케스트레이터의 run guard 로 올라가야 빌드 게이트(불변조건 1)가
지켜진다. 진입점: build_graph, run_graph(CLI), stream_graph(웹
UI 의 SSE). 웹 UI 의 중지(Stop)는 노드 사이에서만 반영된다.

LangGraph engine: intake → analyze → plan → phase (×N) → finalize → END.

Only small, JSON-able values live in the graph state (`MigrationState`); the heavy run objects (the `RunReport`,
the phase groups with their `PlanStep`s, the `BuildFixLoop`) stay on the `Orchestrator` and on a per-run holder
closed over by the node functions. Every node calls one shared `Orchestrator.step_*` method, so the graph engine
and the inline engine (`loop.engine: inline`) produce identical reports, journals and exit codes.

Errors are never handled in a node: `_RunAborted` (a build-fix loop abort), `MissingComponentError` and any other
exception propagate out of the graph to the orchestrator's run guard, exactly as with the inline engine.

v2 (flow-v2): the `phase` node runs whatever phase groups the plan has — normally transform (the one OpenRewrite
run + v2.1's one `mvn clean compile`) → restructure (incl. the structure review step) → verify (final build + fix
loop); `stop_after` accepts every phase of `sbmig.loop.orchestrator.STOP_AFTER_PHASES` (incl. `transform`).

v2.1 (2026-10-27): every node runs inside its log stage (`sbmig.core.stage`: `intake`, `analyze`, `plan`, the phase
group's name, `report` for finalize) — set when the node starts, reset when it ends (LangGraph may run a node in an
executor thread with a copied context; the stage is set inside the node, so it is that thread's); the orchestrator's
steps set the finer stages inside (`rewrite`, `compile`, `restructure`, `review`, `build`, `fix r3 …`).


"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from sbmig.core.models import Phase, PhaseResult, RunReport
from sbmig.core.stage import reset_stage, set_stage

if TYPE_CHECKING:
    from sbmig.loop.orchestrator import Orchestrator

log = logging.getLogger("sbmig.graph")

# 그래프 노드 이름(웹 UI 가 이 이름으로 진행 상황을 그린다).
NODES = ("intake", "analyze", "plan", "phase", "finalize")
# one graph step per node invocation; MSA plans may repeat a phase per target (transform@<svc>, verify@<svc>), so the
# number of `phase` invocations grows with the services — keep generous headroom (LangGraph's default is 25)
# 노드 호출 한 번 = 그래프 한 걸음. MSA 는 서비스마다 phase 를 되풀이하므로(transform@<svc>, verify@<svc>)
# LangGraph 기본값 25 로는 모자라 넉넉히 둔다.
RECURSION_LIMIT = 1000


class MigrationState(TypedDict, total=False):
    """그래프 상태(작은 JSON 값만): run_id, stop_after, dry_run, 남은 단계 묶음 이름, 지금 단계, 중단 이유,
    중지 여부, UI 용 요약. 노드가 갱신하고 stream_graph 가 웹 UI 로 흘려보낸다."""
    run_id: str
    stop_after: str | None          # Phase value or None
    dry_run: bool
    phases: list[str]                  # remaining phase-group names (MSA: repeated per service)
    phase: str | None               # the phase group just run / being run
    aborted: str | None             # abort reason (intake / analyze) — phase aborts raise instead
    stopped: bool                      # `stop_after` reached
    summary: dict                      # small JSON-able digest for the UI


# ---------------------------------------------------------------------------- helpers
def _stop_phase(state: MigrationState) -> Phase | None:
    """상태의 stop_after 문자열을 Phase 로(없으면 None)."""
    v = state.get("stop_after")
    return Phase(v) if v else None


def _report(orch: Orchestrator) -> RunReport:
    """오케스트레이터의 현재 RunReport. 실행 밖에서 노드를 부르면 RuntimeError(실수 방지)."""
    report = orch.report
    if report is None:
        raise RuntimeError("graph node called outside an orchestrator run (orch.report is None)")
    return report


def _summary(orch: Orchestrator, report: RunReport) -> dict:
    """UI 용 작은 요약: 프로젝트 종류, 단계별 결과, LLM 호출 수, 차단·미해결 수."""
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
    """False 를 돌려준 단계 뒤의 상태: 보고서 플래그에 aborted 가
    있으면 중단(aborted), 아니면 stop_after 에 따른 중지(stopped).

    State update after a step that returned False: abort or stop_after."""
    if ok:
        return {"stopped": False, "aborted": None}
    if "aborted" in report.flags:
        return {"stopped": False, "aborted": orch.abort_reason or "aborted"}
    return {"stopped": True, "aborted": None}


def _journal_node(orch: Orchestrator, node: str, phase: str | None = None) -> None:
    """노드 시작을 저널에 남긴다(event graph_node)."""
    orch.ws.journal({"event": "graph_node", "node": node, "phase": phase})


def _staged(name: str | Callable[[MigrationState], str], fn: Callable[[MigrationState], dict]
            ) -> Callable[[MigrationState], dict]:
    """노드 함수를 로그 단계(`name` 또는 `name(state)`) 안에서 돌린다. LangGraph 가 노드를 복사된 컨텍스트의
    실행 스레드에서 돌릴 수 있어서 단계를 노드 안에서 설정하고, 노드가 어떻게 끝나든 되돌린다.

    The node `fn` inside its log stage (`name`, or `name(state)`); the stage is reset however the node ends."""
    def node(state: MigrationState) -> dict:
        token = set_stage(name(state) if callable(name) else name)
        try:
            return fn(state)
        finally:
            reset_stage(token)
    node.__name__ = getattr(fn, "__name__", "node")
    return node


# ---------------------------------------------------------------------------- graph
def build_graph(orch: Orchestrator) -> Any:
    """`orch` 의 단계를 모는 컴파일된 StateGraph(MigrationState). 그래프는 `orch` 에 묶여 있고 오케스트레이터
    실행 안에서만 돈다(`orch.run()`, 또는 직접 실행을 여는 run_graph / stream_graph).

    Compiled `StateGraph(MigrationState)` driving `orch`'s steps. The returned graph is bound to `orch`; it
    must run inside an orchestrator run (`orch.run()`, or `run_graph` / `stream_graph`, which open one)."""
    holder: dict[str, Any] = {"groups": [], "planned": False}

    def intake(state: MigrationState) -> dict:
        """접수 노드: 입력 검사·작업 공간 준비(step_intake). 실패나 stop_after 면 finalize 로 간다."""
        holder["groups"] = []
        holder["planned"] = False
        _journal_node(orch, "intake", Phase.INTAKE.value)
        report = _report(orch)
        ok = orch.step_intake(report, _stop_phase(state))
        return {**_halted(orch, report, ok), "phases": [], "phase": Phase.INTAKE.value,
                "summary": _summary(orch, report)}

    def analyze(state: MigrationState) -> dict:
        """분석 노드: 프로필 만들기(step_analyze)."""
        _journal_node(orch, "analyze", Phase.ANALYZE.value)
        report = _report(orch)
        ok = orch.step_analyze(report, _stop_phase(state))
        return {**_halted(orch, report, ok), "phase": Phase.ANALYZE.value, "summary": _summary(orch, report)}

    def plan(state: MigrationState) -> dict:
        """계획 노드: 계획을 만들고 단계 묶음을 holder 에
        담는다(step_plan). 계획 뒤 중지면 phase 없이 finalize."""
        _journal_node(orch, "plan", Phase.PLAN.value)
        report = _report(orch)
        groups = orch.step_plan(report, _stop_phase(state), bool(state.get("dry_run")))
        if groups is None:                    # stopped after plan
            return {"stopped": True, "phases": [], "phase": Phase.PLAN.value, "summary": _summary(orch, report)}
        holder["groups"] = list(groups)
        holder["planned"] = True
        return {"stopped": False, "phases": [p.value for p, _ in groups], "phase": Phase.PLAN.value,
                "summary": _summary(orch, report)}

    def phase(state: MigrationState) -> dict:
        """단계 노드: 남은 단계 묶음 하나(transform / restructure / verify …)를 실행한다(step_phase).
        중단은 예외로 올라간다."""
        report = _report(orch)
        ph, items = holder["groups"].pop(0)
        _journal_node(orch, "phase", ph.value)
        pr: PhaseResult | None = orch.step_phase(report, ph, items, _stop_phase(state))  # may raise _RunAborted
        out: dict = {"phases": [p.value for p, _ in holder["groups"]], "phase": ph.value,
                     "summary": _summary(orch, report)}
        if pr is None:
            out["summary"]["last"] = {"phase": ph.value, "skipped": "stop_after"}
        return out

    def finalize(state: MigrationState) -> dict:
        """마무리 노드: 계획까지 갔으면 step_finish(최종
        빌드·보고서·KB 기록) — inline 엔진과 같은 조건이다."""
        _journal_node(orch, "finalize", None)
        report = _report(orch)
        if holder["planned"]:                 # the inline engine runs step_finish only after the phase loop
            orch.step_finish(report, _stop_phase(state))
        return {"phase": None, "summary": _summary(orch, report)}

    # 라우팅: 중지(stopped)나 중단(aborted)이면 남은 노드를 건너뛰고 곧장 finalize 로 간다. phase 는 남은 단계
    # 묶음이 없을 때까지 자기 자신으로 돌아온다(MSA 는 서비스마다 한 번씩).
    def after_step(state: MigrationState) -> str:
        return "finalize" if state.get("stopped") or state.get("aborted") else "next"

    def after_plan(state: MigrationState) -> str:
        if state.get("stopped") or state.get("aborted"):
            return "finalize"
        return "phase" if holder["groups"] else "finalize"

    def after_phase(state: MigrationState) -> str:
        return "phase" if holder["groups"] and not (state.get("stopped") or state.get("aborted")) else "finalize"

    def phase_stage(state: MigrationState) -> str:
        """phase 노드의 로그 단계 이름 = 이번에 돌 단계 묶음 이름."""
        return holder["groups"][0][0].value if holder["groups"] else "phase"

    g = StateGraph(MigrationState)
    g.add_node("intake", _staged("intake", intake))
    g.add_node("analyze", _staged("analyze", analyze))
    g.add_node("plan", _staged("plan", plan))
    g.add_node("phase", _staged(phase_stage, phase))
    g.add_node("finalize", _staged("report", finalize))
    g.add_edge(START, "intake")
    g.add_conditional_edges("intake", after_step, {"next": "analyze", "finalize": "finalize"})
    g.add_conditional_edges("analyze", after_step, {"next": "plan", "finalize": "finalize"})
    g.add_conditional_edges("plan", after_plan, {"phase": "phase", "finalize": "finalize"})
    g.add_conditional_edges("phase", after_phase, {"phase": "phase", "finalize": "finalize"})
    g.add_edge("finalize", END)
    return g.compile()


def _initial_state(orch: Orchestrator, stop_after: Phase | None, dry_run: bool) -> MigrationState:
    """그래프의 첫 상태."""
    return MigrationState(run_id=orch.ws.run_id, stop_after=stop_after.value if stop_after is not None else None,
                          dry_run=bool(dry_run), phases=[], phase=None, aborted=None, stopped=False, summary={})


def _guard(orch: Orchestrator, dry_run: bool) -> Any:
    """이미 `orch.run()` 안이면 아무것도 안 함, 직접 불렀으면 run guard 를 연다(보고서 생성 + 무조건 마무리).

    Inside `orch.run()` the run guard is already open → no-op; called directly → open one (report + finalize)."""
    return nullcontext(orch.report) if orch.running else orch._run_guard(dry_run)


def run_graph(orch: Orchestrator, stop_after: Phase | None = None, dry_run: bool = False) -> RunReport:
    """그래프로 이관을 실행한다(CLI 경로). `orch.run()` 안에서는 엔진 본체이고 모든 예외가 run() 의 guard 로
    올라간다. 직접 부르면 같은 guard 를 스스로 연다. 결과는 orch.report.

    Run the migration through the graph. Inside `orch.run()` (engine ``graph``) this is the engine body and
    every exception propagates to `run()`'s guard; called directly it opens the same guard itself. The report is
    `orch.report`."""
    with _guard(orch, dry_run):
        graph = build_graph(orch)
        graph.invoke(_initial_state(orch, stop_after, dry_run), config={"recursion_limit": RECURSION_LIMIT})
    return _report(orch)


def stream_graph(orch: Orchestrator, stop_after: Phase | None = None,
                 dry_run: bool = False) -> Iterator[tuple[str, dict]]:
    """그래프로 이관을 실행하며 노드마다 (노드 이름, 상태 갱신)을 내보낸다(웹 UI 의 SSE `node` 이벤트). 반복이
    끝나거나 일찍 닫혀도 guard 가 보고서를 마무리한다.

    Run the migration through the graph, yielding ``(node_name, state_update)`` after each node (LangGraph
    ``stream_mode="updates"``). The final, finalized report is `orch.report` once the iterator is exhausted (or
    closed early: the guard still finalizes it)."""
    with _guard(orch, dry_run):
        graph = build_graph(orch)
        for chunk in graph.stream(_initial_state(orch, stop_after, dry_run), stream_mode="updates",
                                  config={"recursion_limit": RECURSION_LIMIT}):
            for node, update in chunk.items():
                yield node, dict(update or {})
