"""Review 2026-11-18 L1 / L4: a stop request (stop.request) around the final build.

L1: a stop that lands while the last planned final build runs, and that build passes, must not turn the run into "aborted, the
final mvn clean package did not run" — every planned final build has a result, so the run finishes as what it is.
L4: a stop that makes the fix loop leave does not cost one more full build of the unchanged tree, and the abort text never says
a build did not run when one did. Both engines (graph, inline) behave the same."""
from __future__ import annotations

import pytest

from sbmig.cli import run_exit_code
from sbmig.core.models import BuildError, ErrorKind
from sbmig.core.workspace import STOP_REQUEST_REASON

from .fakes import FakeBuildRunner, FakeFixer, bad_build, journal_events, ok_build, write_script
from .test_orchestrator import make_orch

ENGINES = ["graph", "inline"]


def _engine(cfg, engine):
    cfg = cfg.model_copy(deep=True)
    cfg.loop.engine = engine
    return cfg


@pytest.mark.parametrize("engine", ENGINES)
def test_stop_during_a_passing_final_build_does_not_abort_the_run(cfg, ws, engine):
    cfg = _engine(cfg, engine)

    def build(w, c):
        if c["label"] == "verify":
            w.stop_request_path.write_text("stop\n", encoding="utf-8")     # lands while the final build runs
        return ok_build()
    runner = FakeBuildRunner(ws, fn=build)
    report = make_orch(cfg, ws, build=runner).run()
    assert "aborted" not in report.flags
    assert report.success and report.final_build is not None and report.final_build.ok
    assert run_exit_code(report) == 0
    assert not any(STOP_REQUEST_REASON in m for m in report.manual_followups)
    ev = journal_events(ws)
    assert not any(e["event"] == "abort" for e in ev)
    late = [e for e in ev if e["event"] == "stop_request" and e.get("ignored")]
    assert late and "final build" in late[0]["ignored"]


@pytest.mark.parametrize("engine", ENGINES)
def test_stop_during_a_passing_final_build_still_skips_the_chained_remediation(cfg, ws, engine):
    """The chained remediation keeps today's skip reason (`stop_requested`) — the run itself is not aborted."""
    cfg = _engine(cfg, engine)
    cfg.remediation.after_migration = True

    def build(w, c):
        if c["label"] == "verify":
            w.stop_request_path.write_text("stop\n", encoding="utf-8")
        return ok_build()
    report = make_orch(cfg, ws, build=FakeBuildRunner(ws, fn=build)).run()
    assert report.success and "aborted" not in report.flags
    ev = journal_events(ws)
    if engine == "graph":
        assert any(e["event"] == "remediation_skipped" and e.get("reason") == "stop_requested" for e in ev)
    assert not any(e["event"] == "remediation_start" for e in ev)


@pytest.mark.parametrize("engine", ENGINES)
def test_stop_that_ends_the_fix_loop_costs_no_second_final_build(cfg, ws, engine):
    cfg = _engine(cfg, engine)
    err = BuildError(kind=ErrorKind.COMPILE, file="src/main/java/A.java", line=1, message="cannot find symbol X")

    def build(w, c):
        w.stop_request_path.write_text("stop\n", encoding="utf-8")
        return bad_build(err)
    runner = FakeBuildRunner(ws, fn=build)
    fixer = FakeFixer(script_fn=lambda *a, **k: pytest.fail("the LLM must not be asked after a stop request"))
    report = make_orch(cfg, ws, build=runner, fixer=fixer).run()
    assert [c["label"] for c in runner.calls if c["label"] == "verify"] == ["verify"]   # one final build only
    assert "aborted" in report.flags and run_exit_code(report) == 3
    assert report.final_build is not None and not report.final_build.ok
    reasons = [m for m in report.manual_followups if m.startswith("run aborted:")]
    assert reasons and not any("did not run" in m for m in reasons)
    assert "fix loop" in reasons[0]


@pytest.mark.parametrize("engine", ENGINES)
def test_stop_after_a_try_that_did_not_help_rebuilds_the_rolled_back_tree(cfg, ws, engine):
    """L3 + L4: the loop leaves on the stop after a try that changed nothing useful; that try is rolled back, so the tree
    differs from the loop's last build and the final build runs once more (on the tree that is delivered)."""
    cfg = _engine(cfg, engine)
    err = BuildError(kind=ErrorKind.COMPILE, file="src/main/java/com/x/Foo.java", line=1, message="cannot find symbol X")
    n = {"builds": 0}

    def build(w, c):
        n["builds"] += 1
        if n["builds"] == 2:                                 # the loop's rebuild after the first try
            w.stop_request_path.write_text("stop\n", encoding="utf-8")
        return bad_build(err)
    runner = FakeBuildRunner(ws, fn=build)
    fixer = FakeFixer(script_fn=lambda w, c, ctx: write_script({"notes.txt": "useless\n"}))
    report = make_orch(cfg, ws, build=runner, fixer=fixer).run()
    labels = [c["label"] for c in runner.calls if c["label"] == "verify"]
    assert labels == ["verify", "verify"]                   # the tree changed by the rollback: built again
    assert not ws.exists("notes.txt")
    assert "aborted" in report.flags
    assert not any("did not run" in m for m in report.manual_followups if m.startswith("run aborted:"))
