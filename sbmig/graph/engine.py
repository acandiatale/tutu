"""이관 한 번을 intake -> analyze -> plan -> phase(N번) -> finalize ->
remediation 노드 순서로 실행하는 LangGraph 엔진 모듈.
기본 엔진(``loop.engine: graph``)이며 노드마다 오케스트레이터의 ``step_*`` 메서드 하나를 호출하므로 inline
엔진과 보고서, 저널, 종료 코드가 같다(테스트로 보장).
- 그래프 상태에는 작은 JSON 값만 두고 RunReport, 단계 묶음, 수정 루프 같은 무거운 객체는 오케스트레이터에 둔다
- 빌드 게이트를 지키기 위해 노드는 오류를 처리하지 않고 중단과 예외를 오케스트레이터의 run guard로 그대로 넘긴다
- 마지막 ``remediation`` 노드는 이관이 BUILD SUCCESS로 끝났고 ``remediation.after_migration``이 켜졌을 때만
  취약점 조치 그래프(``sbmig.remediations.chain``)를 호출한다. 그 결과는 이관의 성공 여부와 종료 코드를
  바꾸지 않고 예외도 밖으로 던지지 않는다
- 진입점은 build_graph, run_graph(CLI), stream_graph(웹 UI의 SSE)이다.
  웹 UI의 중지는 노드 사이에서만 반영된다

LangGraph engine: intake → analyze → plan → phase (×N) → finalize → remediation → END (flow `full`); flow
`remediation`: intake → remediation → finalize → END (`REMEDIATION_NODES`).

2026-11-11: the `remediation` node chains the separate vulnerability-remediation graph (`sbmig.remediations.chain`)
after a migration whose final build succeeded, when `remediation.after_migration` is on; otherwise it returns at
once. It runs before the run guard's `_finalize` (report.md, result.zip, KB), so its summary lands on
`RunReport.remediation` and the report renders it; it never changes the migration's verdict and never raises.

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

from sbmig.core.models import FLOW_FULL, FLOW_REMEDIATION, Phase, PhaseResult, RunReport
from sbmig.core.stage import reset_stage, set_stage
from sbmig.core.workspace import STOP_REQUEST_FILE as _STOP_REQUEST_FILE
from sbmig.core.workspace import STOP_REQUEST_REASON as _STOP_REQUEST_REASON

if TYPE_CHECKING:
    from sbmig.loop.orchestrator import Orchestrator

log = logging.getLogger("sbmig.graph")

# 웹 UI가 이 노드 이름으로 진행 상황을 그린다
NODES = ("intake", "analyze", "plan", "phase", "finalize", "remediation")
# 실행 방식이 remediation일 때 이 순서로 실행되는 노드 / the nodes of flow `remediation`, in order
REMEDIATION_NODES = ("intake", "remediation", "finalize")
# one graph step per node invocation; MSA plans may repeat a phase per target (transform@<svc>, verify@<svc>), so the
# number of `phase` invocations grows with the services — keep generous headroom (LangGraph's default is 25)
# MSA는 서비스마다 phase를 반복하므로 LangGraph 기본값 25로는 부족해서 넉넉하게 둔다
RECURSION_LIMIT = 1000


class MigrationState(TypedDict, total=False):
    """그래프 상태를 담는 클래스. run_id, stop_after, dry_run, 남은 단계 묶음 이름, 현재 단계, 중단 이유,
    중지 여부, UI용 요약 같은 작은 JSON 값만 담는다.
    노드가 갱신하고 stream_graph가 웹 UI로 전달한다."""
    run_id: str
    stop_after: str | None          # Phase value or None
    dry_run: bool
    phases: list[str]                  # remaining phase-group names (MSA: repeated per service)
    phase: str | None               # the phase group just run / being run
    aborted: str | None             # abort reason (intake / analyze) — phase aborts raise instead
    stopped: bool                      # `stop_after` reached
    summary: dict                      # small JSON-able digest for the UI
    remediation: dict                  # chained remediation {status, reason} (only when it ran / was skipped)


# ---------------------------------------------------------------------------- helpers
def _stop_phase(state: MigrationState) -> Phase | None:
    """상태의 stop_after 문자열을 Phase로 바꾼다. 없으면 None을 반환한다."""
    v = state.get("stop_after")
    return Phase(v) if v else None


def _report(orch: Orchestrator) -> RunReport:
    """오케스트레이터의 현재 RunReport를 반환한다.
    실행 밖에서 노드를 호출하면 실수를 막기 위해 RuntimeError를 던진다."""
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
    """False를 반환한 단계 뒤의 상태를 만든다.
    보고서 플래그에 aborted가 있으면 중단(aborted), 아니면 stop_after에 따른 중지(stopped)로 표시한다.

    State update after a step that returned False: abort or stop_after."""
    if ok:
        return {"stopped": False, "aborted": None}
    if "aborted" in report.flags:
        return {"stopped": False, "aborted": orch.abort_reason or "aborted"}
    return {"stopped": True, "aborted": None}


def _journal_node(orch: Orchestrator, node: str, phase: str | None = None) -> None:
    """노드 시작을 저널에 기록한다(event graph_node)."""
    orch.ws.journal({"event": "graph_node", "node": node, "phase": phase})


# Job 모드에서는 웹 파드가 다른 파드의 프로세스에 신호를 보낼 수 없으므로 공유 실행 폴더의 이 파일로 중지를
# 요청하고, 엔진은 노드 사이에서 이 파일을 확인한다
# k8s Job mode: the web pod asks a run in another pod to stop by writing this file into the shared run directory;
# the engine checks it between nodes — same semantics as the in-process stop (no final build, report flagged aborted)
STOP_REQUEST_FILE = _STOP_REQUEST_FILE
STOP_REQUEST_REASON = _STOP_REQUEST_REASON


def stop_requested(orch: Orchestrator) -> bool:
    """실행 폴더에 Job 모드의 중지 요청 파일 ``stop.request``가 있는지 확인한다. 예외를 던지지 않는다.

    Is the run directory's `stop.request` present (the Job-mode stop request)? Never raises."""
    try:
        fn = getattr(orch.ws, "stop_requested", None)
        return bool(fn()) if callable(fn) else (orch.ws.run_dir / STOP_REQUEST_FILE).is_file()
    except OSError:  # an unreadable run dir is not a stop request
        return False


def _journal_node_end(orch: Orchestrator, node: str, out: dict) -> None:
    """노드 끝을 저널에 기록한다(event graph_node_end). 예외를 던지지 않는다.
    Job 모드의 웹이 이 줄을 화면의 ``node`` 이벤트로 바꾼다."""
    try:
        orch.ws.journal({"event": "graph_node_end", "node": node, "phase": out.get("phase"),
                         "phases": out.get("phases"), "aborted": out.get("aborted"), "stopped": out.get("stopped")})
    except Exception:  # a journal line never fails a node
        log.debug("graph_node_end journal line for %s not written", node, exc_info=True)


def _staged(name: str | Callable[[MigrationState], str], fn: Callable[[MigrationState], dict],
            orch: Orchestrator | None = None) -> Callable[[MigrationState], dict]:
    """노드 함수를 로그 단계(`name` 또는 `name(state)`) 안에서 실행하는 함수를 만든다.
    LangGraph가 노드를 다른 스레드에서 실행할 수 있으므로 단계를 노드 안에서 설정하고, 노드가 어떻게 끝나든
    되돌린다. ``orch``를 주면 노드가 끝날 때 ``graph_node_end`` 저널 줄을 남긴다.

    The node `fn` inside its log stage (`name`, or `name(state)`); the stage is reset however the node ends. With
    `orch`, a `graph_node_end` journal line follows every node (the Job-mode UI reads it)."""
    node_id = getattr(fn, "__name__", "node")

    def node(state: MigrationState) -> dict:
        token = set_stage(name(state) if callable(name) else name)
        try:
            out = fn(state)
            if orch is not None:
                _journal_node_end(orch, node_id, out or {})
            return out
        except BaseException as e:            # a raising node (phase abort, internal error) still gets its end line (Job-mode UI)
            if orch is not None:
                _journal_node_end(orch, node_id, {"aborted": f"{type(e).__name__}: {e}"[:300]})
            raise
        finally:
            reset_stage(token)
    node.__name__ = node_id
    return node


# ---------------------------------------------------------------------------- remediation (2026-11-11)
def remediation_enabled(cfg: Any) -> bool:
    """``remediation.after_migration``이 켜져 있는지 반환한다. 설정에 그 절이 없으면 False를 반환한다.

    Is `remediation.after_migration` on?"""
    rem = getattr(cfg, "remediation", None)
    return bool(getattr(rem, "after_migration", False)) if rem is not None else False


def migration_build_targets(orch: Any) -> list[str]:
    """이관의 최종 빌드(``verify.build`` 단계)가 실행된 대상 폴더 목록을 반환한다.
    대상이 없는 단계는 루트(``"."``)로 담는다.
    취약점 조치가 같은 대상을 같은 방식으로 빌드하기 위해 사용한다.
    계획을 읽을 수 없으면 빈 목록을 반환한다.

    The target directories of the migration's final builds (`"."` for the root); [] when the plan cannot be read."""
    try:
        steps = orch._verify_steps()
    except Exception:  # an unreadable plan means "the root", as before
        log.debug("migration build targets not read", exc_info=True)
        return []
    return [str(getattr(s, "target", None) or ".") for s in steps]


def _chain_remediation(orch: Orchestrator, report: RunReport, state: MigrationState, *, planned: bool,
                       user_stopped: bool) -> dict | None:
    """취약점 조치를 이어서 실행할 조건이 맞으면 ``chain.run_after_migration``을 호출하고 요약을
    ``report.remediation``에 둔다. 조건이 하나라도 맞지 않으면 아무것도 기록하지 않고 None을 반환한다.
    조건: 설정 켜짐, dry-run 아님, ``stop_after`` 없음, 계획 단계까지 진행, 사용자 중지와 중단 없음, 이관
    성공(``Orchestrator.build_succeeded``).
    이 노드 직전에 ``stop.request``가 생겼으면 조치를 시작하지 않는다.

    Chain the remediation when every condition holds; the summary goes to `report.remediation`. None = not chained."""
    if not remediation_enabled(orch.cfg):
        return None
    if state.get("dry_run") or "dry-run" in report.flags or state.get("stop_after"):
        return None
    if not planned or user_stopped or state.get("aborted") or state.get("stopped") or "aborted" in report.flags:
        return None
    if not orch.build_succeeded(report):
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
                                       build_targets=migration_build_targets(orch))
        report.timings_s["remediation"] = round(orch.clock() - t0, 3)
        if rs is None and not skipped:            # disabled after all (cannot happen with the switch on) — no token
            return None
    summary = chain.summary(rs, orch.ws.run_dir, skipped=skipped)
    report.remediation = summary
    return summary


# ---------------------------------------------------------------------------- graph
def _user_stop_fn(orch: Orchestrator, holder: dict[str, Any]) -> Callable[[str, str | None], dict | None]:
    """노드를 실행하기 전에 ``stop.request``를 확인하는 함수를 만든다.
    파일이 있으면 보고서를 aborted로 표시하고 그 노드를 건너뛰어 finalize로 가는 상태 갱신을, 없으면 None을 반환한다."""

    def user_stop(node: str, phase: str | None) -> dict | None:
        if holder["user_stopped"] or not stop_requested(orch):
            return None
        report = _report(orch)
        holder["user_stopped"] = True
        log.warning("stop requested (%s) — stopping before node %s (no final build)", STOP_REQUEST_FILE, node)
        try:
            orch.ws.journal({"event": "stop_request", "node": node, "file": STOP_REQUEST_FILE})
        except Exception:  # the journal line is a convenience
            log.debug("stop_request journal line not written", exc_info=True)
        if "aborted" not in report.flags:
            orch._abort(report, STOP_REQUEST_REASON)   # the same marking the web's in-process stop uses
        return {"stopped": False, "aborted": orch.abort_reason or STOP_REQUEST_REASON, "phase": phase,
                "summary": _summary(orch, report)}
    return user_stop


def _build_remediation_graph(orch: Orchestrator) -> Any:
    """조치만 하는 실행(``remediation``)의 그래프 intake -> remediation -> finalize를 만든다.
    intake가 실패하면 바로 finalize로 간다.
    finalize 노드는 상태만 표시하고 보고서는 run guard의 마무리(``_finalize``)가 쓴다.

    The `remediation` flow graph: intake → remediation → finalize → END (a failed intake goes straight to finalize)."""
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
    그래프는 `orch`에 묶여 있으므로 오케스트레이터 실행 안에서만 실행해야 한다.
    ``flow``가 ``remediation``이면 intake -> remediation -> finalize 그래프를 만든다.

    Compiled `StateGraph(MigrationState)` driving `orch`'s steps. The returned graph is bound to `orch`; it
    must run inside an orchestrator run (`orch.run()`, or `run_graph` / `stream_graph`, which open one). `flow`
    `remediation` builds the intake → remediation → finalize graph."""
    if (flow or getattr(orch, "flow", FLOW_FULL)) == FLOW_REMEDIATION:
        return _build_remediation_graph(orch)
    holder: dict[str, Any] = {"groups": [], "planned": False, "user_stopped": False}

    user_stop = _user_stop_fn(orch, holder)

    def intake(state: MigrationState) -> dict:
        """입력을 검사하고 작업 공간을 준비한다(step_intake). 실패하거나 stop_after면 finalize로 간다."""
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
        """프로필을 만든다(step_analyze)."""
        if (stop := user_stop("analyze", Phase.ANALYZE.value)) is not None:
            return stop
        _journal_node(orch, "analyze", Phase.ANALYZE.value)
        report = _report(orch)
        ok = orch.step_analyze(report, _stop_phase(state))
        return {**_halted(orch, report, ok), "phase": Phase.ANALYZE.value, "summary": _summary(orch, report)}

    def plan(state: MigrationState) -> dict:
        """계획을 만들고 단계 묶음을 holder에 담는다(step_plan).
        계획 뒤에 중지하면 phase 없이 finalize로 간다."""
        if (stop := user_stop("plan", Phase.PLAN.value)) is not None:
            return {**stop, "phases": []}
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
        """남은 단계 묶음 하나(transform, restructure, verify 등)를 실행한다(step_phase).
        중단은 예외로 넘긴다."""
        report = _report(orch)
        if (stop := user_stop("phase", holder["groups"][0][0].value if holder["groups"] else None)) is not None:
            return {**stop, "phases": [p.value for p, _ in holder["groups"][1:]]}   # the skipped phase is not "remaining"
        ph, items = holder["groups"].pop(0)
        _journal_node(orch, "phase", ph.value)
        pr: PhaseResult | None = orch.step_phase(report, ph, items, _stop_phase(state))  # may raise _RunAborted
        out: dict = {"phases": [p.value for p, _ in holder["groups"]], "phase": ph.value,
                     "summary": _summary(orch, report)}
        if pr is None:
            out["summary"]["last"] = {"phase": ph.value, "skipped": "stop_after"}
        return out

    def finalize(state: MigrationState) -> dict:
        """계획 단계까지 진행했으면 step_finish로 최종 빌드, 보고서, KB 기록을 실행한다.
        조건은 inline 엔진과 같다."""
        _journal_node(orch, "finalize", None)
        report = _report(orch)
        # the inline engine runs step_finish only after the phase loop; a user stop (stop.request) skips the final build —
        # also when the request arrived during the last phase (the fix loop leaves between build rounds, then lands here)
        if not holder["user_stopped"] and stop_requested(orch):
            user_stop("finalize", None)
        if holder["planned"] and not holder["user_stopped"]:
            orch.step_finish(report, _stop_phase(state))
        return {"phase": None, "summary": _summary(orch, report)}

    def remediation(state: MigrationState) -> dict:
        """설정이 켜져 있고 이관이 최종 빌드에 성공했을 때만 취약점 조치 그래프를 호출한다.
        그 밖에는 바로 반환한다.
        조치의 결과나 예외는 이관 결과를 바꾸지 않는다."""
        report = _report(orch)
        try:
            summary = _chain_remediation(orch, report, state, planned=holder["planned"],
                                         user_stopped=holder["user_stopped"])
        except Exception:  # noqa: BLE001 — never let the remediation touch the migration's verdict
            log.exception("vulnerability remediation node failed (the migration result is unchanged)")
            summary = None
        out: dict = {"phase": None}
        if summary is not None:
            out["remediation"] = {k: summary.get(k) for k in ("status", "reason")}
        return out

    # 중지나 중단이면 남은 노드를 건너뛰고 finalize로 간다.
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
        """phase 노드의 로그 단계 이름으로 이번에 실행할 단계 묶음 이름을 반환한다."""
        return holder["groups"][0][0].value if holder["groups"] else "phase"

    g = StateGraph(MigrationState)
    g.add_node("intake", _staged("intake", intake, orch))
    g.add_node("analyze", _staged("analyze", analyze, orch))
    g.add_node("plan", _staged("plan", plan, orch))
    g.add_node("phase", _staged(phase_stage, phase, orch))
    g.add_node("finalize", _staged("report", finalize, orch))
    # 모든 로컬 실행에 불필요한 줄이 쌓이지 않도록 설정이 꺼져 있으면 graph_node_end 줄을 남기지 않는다
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


def _guard(orch: Orchestrator, dry_run: bool, flow: str = FLOW_FULL) -> Any:
    """직접 호출되었으면 run guard를 열어 보고서 생성과 마무리를 보장한다.
    이미 `orch.run()` 안이면 아무것도 하지 않는다.

    Inside `orch.run()` the run guard is already open → no-op; called directly → open one (report + finalize)."""
    return nullcontext(orch.report) if orch.running else orch._run_guard(dry_run, flow=flow)


def _check(orch: Orchestrator, stop_after: Phase | None, dry_run: bool, flow: str | None) -> str:
    """실행 방식(``check_flow``)을 검사한다. 이미 `orch.run()` 안이면 그 실행의 방식을 쓴다."""
    from sbmig.loop.orchestrator import check_flow
    if orch.running:
        return getattr(orch, "flow", FLOW_FULL)
    return check_flow(flow, stop_after, dry_run)


def run_graph(orch: Orchestrator, stop_after: Phase | None = None, dry_run: bool = False,
              flow: str = FLOW_FULL) -> RunReport:
    """그래프로 이관을 실행한다(CLI 경로). 결과는 orch.report에 남는다.
    `orch.run()` 안에서는 모든 예외가 run()의 guard로 넘어가고, 직접 호출하면 같은 guard를 스스로 연다.
    ``flow="remediation"``을 ``stop_after``나 ``dry_run``과 함께 쓰면 ValueError를 던진다.

    Run the migration through the graph. Inside `orch.run()` (engine ``graph``) this is the engine body and
    every exception propagates to `run()`'s guard; called directly it opens the same guard itself. The report is
    `orch.report`. `flow` picks the graph (`full` | `remediation`)."""
    flow = _check(orch, stop_after, dry_run, flow)
    with _guard(orch, dry_run, flow):
        graph = build_graph(orch, flow)
        graph.invoke(_initial_state(orch, stop_after, dry_run), config={"recursion_limit": RECURSION_LIMIT})
    return _report(orch)


def stream_graph(orch: Orchestrator, stop_after: Phase | None = None,
                 dry_run: bool = False, flow: str = FLOW_FULL) -> Iterator[tuple[str, dict]]:
    """그래프로 이관을 실행하며 노드마다 (노드 이름, 상태 갱신)을 내보낸다(웹 UI의 SSE `node` 이벤트).
    반복이 끝나거나 일찍 닫혀도 guard가 보고서를 마무리한다.
    잘못된 ``flow`` 조합은 첫 ``next``에서 ValueError를 던진다.

    Run the migration through the graph, yielding ``(node_name, state_update)`` after each node (LangGraph
    ``stream_mode="updates"``; in flow `full` a `remediation` node that did nothing is not yielded). The final, finalized
    report is `orch.report` once the iterator is exhausted (or closed early: the guard still finalizes it). `flow`
    `remediation` yields intake, remediation and finalize."""
    flow = _check(orch, stop_after, dry_run, flow)
    with _guard(orch, dry_run, flow):
        graph = build_graph(orch, flow)
        for chunk in graph.stream(_initial_state(orch, stop_after, dry_run), stream_mode="updates",
                                  config={"recursion_limit": RECURSION_LIMIT}):
            for node, update in chunk.items():
                # 웹의 노드 목록이 바뀌지 않도록 아무것도 하지 않은 remediation 노드는 내보내지 않는다
                if flow == FLOW_FULL and node == "remediation" and "remediation" not in (update or {}):
                    continue
                yield node, dict(update or {})
