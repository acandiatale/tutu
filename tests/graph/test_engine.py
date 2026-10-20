"""Graph engine (LangGraph) vs inline engine: same steps, same report, same journal (+ `graph_node` events)."""
from __future__ import annotations

import threading

import pytest

from sbmig.cli import run_exit_code
from sbmig.core.models import GAV, BuildError, CaseType, ErrorKind, Phase
from sbmig.core.workspace import Workspace
from sbmig.graph import NODES, build_graph, run_graph, stream_graph

from ..loop.fakes import (
    FakeBuildRunner,
    FakeFixer,
    FakeRewriteRunner,
    bad_build,
    journal_events,
    make_profile,
    make_project,
    msa_plan,
    ok_build,
    write_fix,
)
from ..loop.test_orchestrator import files_build, make_orch


def new_ws(tmp_path, name: str) -> Workspace:
    root = tmp_path / name
    root.mkdir()
    w = Workspace.from_input(make_project(root), root / "out", run_id="t-run")
    w.git_init()
    return w


def run_engine(cfg, tmp_path, engine: str, *, name: str | None = None, stop_after=None, **kw):
    cfg = cfg.model_copy(deep=True)
    cfg.loop.engine = engine
    ws = new_ws(tmp_path, name or engine)
    build = kw.pop("build", None) or (lambda w: FakeBuildRunner(w, fn=files_build))
    orch = make_orch(cfg, ws, build=build(ws), rewrite=FakeRewriteRunner(ws), **kw)
    report = orch.run(stop_after=stop_after)
    return orch, ws, report


TIME_KEYS = {"started_at", "finished_at", "created_at", "timestamp", "duration_s", "commit"}   # + git SHAs (time-stamped)


def _scrub(x):
    if isinstance(x, dict):
        return {k: _scrub(v) for k, v in x.items() if k not in TIME_KEYS}
    if isinstance(x, list):
        return [_scrub(v) for v in x]
    return x


def comparable(report) -> dict:
    """The report minus wall-clock values (timestamps, timings) and run-dir paths (`outputs`)."""
    return _scrub(report.model_dump(mode="json", exclude={"timings_s", "outputs"}))


def comparable_events(ws, *, drop_graph: bool = True) -> list[dict]:
    out = []
    for e in journal_events(ws):
        if drop_graph and e["event"] == "graph_node":
            continue
        out.append({"event": e["event"], "phase": e.get("phase"), "reason": e.get("reason")})
    return out


# ---------------------------------------------------------------------------------------- (a) identical reports
def test_graph_and_inline_produce_the_same_report(cfg, tmp_path):
    _, ws_g, rg = run_engine(cfg, tmp_path, "graph")
    _, ws_i, ri = run_engine(cfg, tmp_path, "inline")
    assert rg.success and ri.success
    assert [p.phase for p in rg.phases] == [Phase.JDK21, Phase.SPRING, Phase.RESTRUCTURE, Phase.VERIFY]
    assert comparable(rg) == comparable(ri)
    assert set(rg.timings_s) == set(ri.timings_s)
    assert rg.flags == ri.flags and run_exit_code(rg) == run_exit_code(ri) == 0
    assert comparable_events(ws_g) == comparable_events(ws_i)
    assert not any(e["event"] == "graph_node" for e in journal_events(ws_i))


def test_graph_and_inline_same_report_on_msa_plan(cfg, tmp_path):
    kw = {"plan": msa_plan(), "profile": make_profile(CaseType.MSA),
          "build": lambda w: FakeBuildRunner(w, fn=lambda *_: ok_build())}
    _, ws_g, rg = run_engine(cfg, tmp_path, "graph", **kw)
    _, ws_i, ri = run_engine(cfg, tmp_path, "inline", **kw)
    assert rg.success and comparable(rg) == comparable(ri)
    assert comparable_events(ws_g) == comparable_events(ws_i)
    phase_nodes = [e["phase"] for e in journal_events(ws_g) if e["event"] == "graph_node" and e["node"] == "phase"]
    assert phase_nodes == [p.phase.value for p in rg.phases]          # one node invocation per phase group


def test_graph_and_inline_same_report_on_failure(cfg, tmp_path):
    from ..loop.fakes import compile_error
    cfg = cfg.model_copy(deep=True)
    cfg.loop.max_llm_calls = 1
    build = lambda w: FakeBuildRunner(w, fn=lambda *_: bad_build(compile_error()))
    reports = [run_engine(cfg, tmp_path, engine, build=build,
                          fixer=FakeFixer(fn=lambda w, c, ctx: write_fix(c.file, "class F { broken 2 }")))[2]
               for engine in ("graph", "inline")]
    rg, ri = reports
    assert not rg.success and comparable(rg) == comparable(ri)
    assert run_exit_code(rg) == run_exit_code(ri) == 2


# ---------------------------------------------------------------------------------------- (b) stop_after
@pytest.mark.parametrize("stop", [Phase.INTAKE, Phase.ANALYZE, Phase.PLAN, Phase.JDK21])
def test_stop_after_same_in_both_engines(cfg, tmp_path, stop):
    _, ws_g, rg = run_engine(cfg, tmp_path, "graph", stop_after=stop)
    _, ws_i, ri = run_engine(cfg, tmp_path, "inline", stop_after=stop)
    assert f"stopped-after-{stop.value}" in rg.flags and rg.flags == ri.flags
    assert comparable(rg) == comparable(ri)
    assert comparable_events(ws_g) == comparable_events(ws_i)
    nodes = [e["node"] for e in journal_events(ws_g) if e["event"] == "graph_node"]
    if stop == Phase.ANALYZE:
        assert nodes == ["intake", "analyze", "finalize"] and rg.plan is None
    if stop == Phase.JDK21:                       # later groups are journaled as phase_skipped by `phase` nodes
        assert [p.phase for p in rg.phases] == [Phase.JDK21]
        assert any(e["event"] == "phase_skipped" for e in journal_events(ws_g))


# ---------------------------------------------------------------------------------------- (c) aborts
def test_mvn_not_found_abort_same_in_both_engines(cfg, tmp_path, monkeypatch):
    import sbmig.build as b
    monkeypatch.setattr(b, "maven_available", lambda *a, **k: False)
    reports = {}
    for engine in ("graph", "inline"):
        c = cfg.model_copy(deep=True)
        c.loop.engine = engine
        ws = new_ws(tmp_path, engine)
        orch = make_orch(c, ws)
        orch._build_runner = None
        reports[engine] = (orch, ws, orch.run())
    (og, wg, rg), (_, _, ri) = reports["graph"], reports["inline"]
    assert "mvn-not-found" in rg.flags and "aborted" in rg.flags and rg.flags == ri.flags
    assert rg.phases == [] and run_exit_code(rg) == run_exit_code(ri) == 3
    assert og.abort_reason and "not runnable" in og.abort_reason
    assert [e["node"] for e in journal_events(wg) if e["event"] == "graph_node"] == ["intake", "finalize"]


def test_build_loop_abort_propagates_out_of_the_graph(cfg, tmp_path):
    parent = cfg.artifacts.parent
    perr = BuildError(kind=ErrorKind.PARENT_RESOLUTION, file="pom.xml",
                      message=f"Non-resolvable parent POM for com.x:demo:1: Could not find artifact "
                              f"{parent.key}:pom:{parent.version} in nexus (https://nexus.example/repo)",
                      artifact=GAV(group_id=parent.group_id, artifact_id=parent.artifact_id, version=parent.version),
                      signature=f"parent_resolution|{parent.key}")
    build = lambda w: FakeBuildRunner(w, fn=lambda *_: bad_build(perr))
    _, ws_g, rg = run_engine(cfg, tmp_path, "graph", build=build)
    _, ws_i, ri = run_engine(cfg, tmp_path, "inline", build=build)
    assert "aborted:parent-not-found" in rg.flags and rg.flags == ri.flags
    assert run_exit_code(rg) == run_exit_code(ri) == 3
    assert comparable(rg) == comparable(ri) and comparable_events(ws_g) == comparable_events(ws_i)
    nodes = [e["node"] for e in journal_events(ws_g) if e["event"] == "graph_node"]
    assert nodes == ["intake", "analyze", "plan", "phase"]       # _RunAborted left the graph: no finalize node
    assert journal_events(ws_g)[-1]["event"] == "report"         # ... but run() still finalized the report

    # called directly (outside run()), run_graph opens the guard itself and handles the abort the same way
    c = cfg.model_copy(deep=True)
    ws = new_ws(tmp_path, "direct")
    orch = make_orch(c, ws, build=build(ws), rewrite=FakeRewriteRunner(ws))
    rd = run_graph(orch)
    assert rd is orch.report and "aborted:parent-not-found" in rd.flags and not orch.running


def test_internal_error_in_a_node_is_handled_by_run(cfg, tmp_path):
    def boom(p, c, k):
        raise ValueError("bad plan")
    _, _, rg = run_engine(cfg, tmp_path, "graph", planner=boom)
    _, _, ri = run_engine(cfg, tmp_path, "inline", planner=boom)
    assert "aborted" in rg.flags and "internal error: ValueError: bad plan" in rg.manual_followups[-1]
    assert rg.manual_followups == ri.manual_followups and rg.flags == ri.flags


# ---------------------------------------------------------------------------------------- (d) stream_graph
def test_stream_graph_yields_nodes_in_order_and_journals_them(cfg, tmp_path):
    ws = new_ws(tmp_path, "stream")
    orch = make_orch(cfg, ws, build=FakeBuildRunner(ws, fn=files_build), rewrite=FakeRewriteRunner(ws))
    seen = []
    threads = set()
    orig = orch.step_phase

    def spy(*a, **k):
        threads.add(threading.get_ident())
        return orig(*a, **k)
    orch.step_phase = spy
    for node, update in stream_graph(orch):
        seen.append((node, update))
        assert isinstance(update, dict)
    names = [n for n, _ in seen]
    assert names == ["intake", "analyze", "plan", "phase", "phase", "phase", "phase", "finalize"]
    report = orch.report
    assert report is not None and report.success and not orch.running
    assert threads == {threading.get_ident()}            # nodes run in the caller's thread (per-run log filter)
    plan_update = dict(seen)["plan"]
    assert plan_update["phases"] == ["jdk21", "spring", "restructure", "verify"]
    last_phase = [u for n, u in seen if n == "phase"][-1]
    assert last_phase["phases"] == [] and last_phase["phase"] == "verify"
    final = seen[-1][1]["summary"]
    assert final["case_type"] == report.case_type.value and final["llm_calls"] == orch.budget.llm_calls
    assert [p["phase"] for p in final["phases"]] == ["jdk21", "spring", "restructure", "verify"]
    ev = journal_events(ws)
    graph_nodes = [(e["node"], e["phase"]) for e in ev if e["event"] == "graph_node"]
    assert graph_nodes == [("intake", "intake"), ("analyze", "analyze"), ("plan", "plan"), ("phase", "jdk21"),
                           ("phase", "spring"), ("phase", "restructure"), ("phase", "verify"), ("finalize", None)]
    assert ev[-1]["event"] == "report"                   # finalized by the guard after the stream ended


def test_stream_graph_closed_early_still_finalizes(cfg, tmp_path):
    ws = new_ws(tmp_path, "early")
    orch = make_orch(cfg, ws, build=FakeBuildRunner(ws, fn=files_build), rewrite=FakeRewriteRunner(ws))
    it = stream_graph(orch)
    assert next(it)[0] == "intake"
    it.close()
    assert not orch.running and journal_events(ws)[-1]["event"] == "report"
    assert orch.report is not None and orch.report.phases == []


def test_stream_graph_json_able_updates(cfg, tmp_path):
    import json
    ws = new_ws(tmp_path, "json")
    orch = make_orch(cfg, ws, build=FakeBuildRunner(ws, fn=files_build), rewrite=FakeRewriteRunner(ws))
    for _, update in stream_graph(orch, stop_after=Phase.SPRING):
        json.dumps(update)
    assert "stopped-after-spring" in orch.report.flags


# ---------------------------------------------------------------------------------------- (e) graph shape
def test_build_graph_has_the_five_nodes(cfg, tmp_path):
    ws = new_ws(tmp_path, "shape")
    g = build_graph(make_orch(cfg, ws)).get_graph()
    assert set(NODES) <= set(g.nodes)
    mermaid = g.draw_mermaid()
    for n in NODES:
        assert n in mermaid


def test_default_engine_is_graph(cfg, tmp_path):
    assert cfg.loop.engine == "graph"
    orch, ws, _ = run_engine(cfg, tmp_path, "graph")
    assert orch.engine == "graph" and any(e["event"] == "graph_node" for e in journal_events(ws))
