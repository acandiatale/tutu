"""The fix loop checks a stop request and its time budget before every LLM request (v2.1z-bo), not only between
builds: answers that change nothing trigger no build, so the second real-Maven smoke run saw a stop raised during the
second ask followed by eight more asks. A halt before an ask ends the loop the way a stop / the time budget found
between builds does (same notes, journal events, flags and blocked cases). Fakes only."""
from __future__ import annotations

from sbmig.core.models import BlockReason, Phase
from sbmig.loop import BuildFixLoop, LoopBudget
from sbmig.triage import Triager

from .fakes import (
    FakeBuildRunner,
    FakeFixer,
    FakeKB,
    FakeScriptRunner,
    bad_build,
    compile_error,
    journal_events,
    make_profile,
    ok_build,
    script,
)

FOO = "src/main/java/com/x/Foo.java"
L2 = "compile|cannot_find_symbol|clientIp"


def broken(ws, call):
    if ws.exists(FOO) and "broken" in ws.read(FOO):
        return bad_build(compile_error(FOO, l2=L2))
    return ok_build()


def noop(n: int):
    """Answers whose script runs and changes nothing: the loop asks again on the same build (re-round)."""
    return [script("p.log('looked, nothing to do')") for _ in range(n)]


def make_loop(cfg, ws, fixer, *, budget=None) -> BuildFixLoop:
    runner = FakeBuildRunner(ws, fn=broken)
    loop = BuildFixLoop(cfg, ws, runner, Triager(cfg), fixer, FakeKB(), make_profile(),
                        budget=budget or LoopBudget.from_config(cfg), script_runner=FakeScriptRunner())
    loop.runner_under_test = runner
    return loop


def _stop_after(ws, checks: int):
    """`ws.stop_requested` answers False for the first `checks` calls, True afterwards."""
    n = {"k": 0}

    def stop() -> bool:
        n["k"] += 1
        return n["k"] > checks
    ws.stop_requested = stop                       # type: ignore[method-assign]


def _stopped_like_between_builds(loop, pr, ws) -> None:
    assert not pr.ok and loop.left_on_stop and loop.stopped_by_request
    assert any("stop requested (stop.request)" in n for n in pr.notes)
    events = [e["event"] for e in journal_events(ws)]
    assert "stop_request" in events and "llm_ask_skipped" in events and events[-1] == "loop_end"


def test_a_stop_before_the_first_ask_asks_nothing(cfg, ws):
    ws.write(FOO, "class Foo { broken }\n")
    ws.commit("broken")
    cfg.script.max_llm_tries_per_case = 5
    fixer = FakeFixer(scripts=noop(5))
    _stop_after(ws, 1)                             # the check after the first build passes, the next one stops
    loop = make_loop(cfg, ws, fixer)
    pr = loop.run(Phase.VERIFY, "package", 6)
    assert fixer.script_calls == []
    assert pr.attempts == [] and len(loop.runner_under_test.calls) == 1
    _stopped_like_between_builds(loop, pr, ws)


def test_a_stop_between_two_no_op_asks_ends_the_loop(cfg, ws):
    ws.write(FOO, "class Foo { broken }\n")
    ws.commit("broken")
    cfg.script.max_llm_tries_per_case = 5
    flag = {"stop": False}
    answers = noop(5)

    def answer(ws_, cluster, ctx):
        flag["stop"] = True                        # the stop arrives while the LLM answers
        return answers.pop(0)
    fixer = FakeFixer(script_fn=answer)
    ws.stop_requested = lambda: flag["stop"]       # type: ignore[method-assign]
    loop = make_loop(cfg, ws, fixer)
    pr = loop.run(Phase.VERIFY, "package", 6)
    assert len(fixer.script_calls) == 1 and len(pr.attempts) == 1
    assert len(loop.runner_under_test.calls) == 1                    # no build was made for the no-op answer
    assert not ws.is_dirty()
    _stopped_like_between_builds(loop, pr, ws)


def test_the_time_budget_passing_between_two_asks_ends_the_loop_like_between_builds(cfg, ws):
    ws.write(FOO, "class Foo { broken }\n")
    ws.commit("broken")
    cfg.script.max_llm_tries_per_case = 5
    cfg.loop.max_total_minutes = 10
    now = {"t": 1000.0}
    budget = LoopBudget.from_config(cfg, clock=lambda: now["t"])
    answers = noop(5)

    def answer(ws_, cluster, ctx):
        now["t"] += 11 * 60                        # the run time budget passes during this ask
        return answers.pop(0)
    fixer = FakeFixer(script_fn=answer)
    loop = make_loop(cfg, ws, fixer, budget=budget)
    pr = loop.run(Phase.VERIFY, "package", 6)
    assert len(fixer.script_calls) == 1
    assert "run time budget exhausted: stopping the build-fix loop" in pr.notes
    assert "time-budget-exhausted" in loop.flags
    assert [b.reason for b in pr.blocked] == [BlockReason.BUDGET_EXHAUSTED]
    events = [e["event"] for e in journal_events(ws)]
    assert "budget_exhausted" in events and "llm_ask_skipped" in events
    assert not loop.left_on_stop


def test_the_remediation_loop_stops_asking_after_a_stop_during_an_ask(cfg, tmp_path):
    from tests.remediations.test_fixer import make_tree, run

    cfg.remediation.fix_loop_max_iterations = 6
    cfg.script.max_llm_tries_per_case = 5
    flag = {"stop": False}
    answers = noop(5)

    def answer(ws_, cluster, ctx):
        flag["stop"] = True
        return answers.pop(0)
    res = run(cfg, make_tree(tmp_path), llm_fixer=FakeFixer(script_fn=answer), stop=lambda: flag["stop"])
    assert res.reason == "stop requested" and not res.fixed and res.llm_calls == 1
