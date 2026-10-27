"""LangGraph engine: intake → analyze → plan → phase (×N) → finalize → END.

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

NODES = ("intake", "analyze", "plan", "phase", "finalize")
# one graph step per node invocation; MSA plans may repeat a phase per target (transform@<svc>, verify@<svc>), so the
# number of `phase` invocations grows with the services — keep generous headroom (LangGraph's default is 25)
RECURSION_LIMIT = 1000


class MigrationState(TypedDict, total=False):
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
    v = state.get("stop_after")
    return Phase(v) if v else None


def _report(orch: Orchestrator) -> RunReport:
    report = orch.report
    if report is None:
        raise RuntimeError("graph node called outside an orchestrator run (orch.report is None)")
    return report


def _summary(orch: Orchestrator, report: RunReport) -> dict:
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
    """State update after a step that returned False: abort or stop_after."""
    if ok:
        return {"stopped": False, "aborted": None}
    if "aborted" in report.flags:
        return {"stopped": False, "aborted": orch.abort_reason or "aborted"}
    return {"stopped": True, "aborted": None}


def _journal_node(orch: Orchestrator, node: str, phase: str | None = None) -> None:
    orch.ws.journal({"event": "graph_node", "node": node, "phase": phase})


def _staged(name: str | Callable[[MigrationState], str], fn: Callable[[MigrationState], dict]
            ) -> Callable[[MigrationState], dict]:
    """The node `fn` inside its log stage (`name`, or `name(state)`); the stage is reset however the node ends."""
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
    """Compiled `StateGraph(MigrationState)` driving `orch`'s steps. The returned graph is bound to `orch`; it
    must run inside an orchestrator run (`orch.run()`, or `run_graph` / `stream_graph`, which open one)."""
    holder: dict[str, Any] = {"groups": [], "planned": False}

    def intake(state: MigrationState) -> dict:
        holder["groups"] = []
        holder["planned"] = False
        _journal_node(orch, "intake", Phase.INTAKE.value)
        report = _report(orch)
        ok = orch.step_intake(report, _stop_phase(state))
        return {**_halted(orch, report, ok), "phases": [], "phase": Phase.INTAKE.value,
                "summary": _summary(orch, report)}

    def analyze(state: MigrationState) -> dict:
        _journal_node(orch, "analyze", Phase.ANALYZE.value)
        report = _report(orch)
        ok = orch.step_analyze(report, _stop_phase(state))
        return {**_halted(orch, report, ok), "phase": Phase.ANALYZE.value, "summary": _summary(orch, report)}

    def plan(state: MigrationState) -> dict:
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
        _journal_node(orch, "finalize", None)
        report = _report(orch)
        if holder["planned"]:                 # the inline engine runs step_finish only after the phase loop
            orch.step_finish(report, _stop_phase(state))
        return {"phase": None, "summary": _summary(orch, report)}

    def after_step(state: MigrationState) -> str:
        return "finalize" if state.get("stopped") or state.get("aborted") else "next"

    def after_plan(state: MigrationState) -> str:
        if state.get("stopped") or state.get("aborted"):
            return "finalize"
        return "phase" if holder["groups"] else "finalize"

    def after_phase(state: MigrationState) -> str:
        return "phase" if holder["groups"] and not (state.get("stopped") or state.get("aborted")) else "finalize"

    def phase_stage(state: MigrationState) -> str:
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
    return MigrationState(run_id=orch.ws.run_id, stop_after=stop_after.value if stop_after is not None else None,
                          dry_run=bool(dry_run), phases=[], phase=None, aborted=None, stopped=False, summary={})


def _guard(orch: Orchestrator, dry_run: bool) -> Any:
    """Inside `orch.run()` the run guard is already open → no-op; called directly → open one (report + finalize)."""
    return nullcontext(orch.report) if orch.running else orch._run_guard(dry_run)


def run_graph(orch: Orchestrator, stop_after: Phase | None = None, dry_run: bool = False) -> RunReport:
    """Run the migration through the graph. Inside `orch.run()` (engine ``graph``) this is the engine body and
    every exception propagates to `run()`'s guard; called directly it opens the same guard itself. The report is
    `orch.report`."""
    with _guard(orch, dry_run):
        graph = build_graph(orch)
        graph.invoke(_initial_state(orch, stop_after, dry_run), config={"recursion_limit": RECURSION_LIMIT})
    return _report(orch)


def stream_graph(orch: Orchestrator, stop_after: Phase | None = None,
                 dry_run: bool = False) -> Iterator[tuple[str, dict]]:
    """Run the migration through the graph, yielding ``(node_name, state_update)`` after each node (LangGraph
    ``stream_mode="updates"``). The final, finalized report is `orch.report` once the iterator is exhausted (or
    closed early: the guard still finalizes it)."""
    with _guard(orch, dry_run):
        graph = build_graph(orch)
        for chunk in graph.stream(_initial_state(orch, stop_after, dry_run), stream_mode="updates",
                                  config={"recursion_limit": RECURSION_LIMIT}):
            for node, update in chunk.items():
                yield node, dict(update or {})
