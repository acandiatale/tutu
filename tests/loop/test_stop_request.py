"""2026-11-05 (k8s Job mode review): the web's stop request (stop.request in the run dir) is honoured between BUILD ROUNDS of the
fix loop — not only between graph nodes — so a stop during a long verify phase does not wait 40 minutes (and get the Job deleted
after the grace, losing the report). The loop leaves without another LLM ask; finalize marks the run aborted, exit 3."""
from __future__ import annotations

from sbmig.cli import run_exit_code
from sbmig.core.models import BuildError, ErrorKind

from .fakes import FakeBuildRunner, FakeFixer, bad_build, journal_events
from .test_orchestrator import make_orch


def test_stop_request_between_build_rounds_leaves_the_loop_without_an_llm_ask(cfg, ws):
    err = BuildError(kind=ErrorKind.COMPILE, file="src/main/java/A.java", line=1, message="cannot find symbol X")

    def build(w, c):
        w.stop_request_path.write_text("stop\n", encoding="utf-8")     # the web pod's request lands during this build
        return bad_build(err)
    runner = FakeBuildRunner(ws, fn=build)
    fixer = FakeFixer(script_fn=lambda *a, **k: __import__("pytest").fail("the LLM must not be asked after a stop request"))
    report = make_orch(cfg, ws, build=runner, fixer=fixer).run()
    assert "aborted" in report.flags and run_exit_code(report) == 3
    assert any("stop requested (stop.request)" in n for pr in report.phases for n in pr.notes)
    ev = journal_events(ws)
    # one logical build round (the runner's own skip-fallback may call the fake twice), then the loop leaves; finalize aborts
    # BEFORE the final build (no second `build` event, the abort precedes graph_node_end of finalize)
    assert [e["iteration"] for e in ev if e["event"] == "build"] == [1]
    assert any(e["event"] == "stop_request" and e.get("where") == "fix loop" for e in ev)
    kinds = [e["event"] for e in ev]
    assert "abort" in kinds and "build" not in kinds[kinds.index("abort"):]       # nothing built after the abort
