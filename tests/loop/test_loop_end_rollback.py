"""Review 2026-11-18 L3: when the fix loop ends, a case's last applied try that did not help (no_change / inconclusive)
is rolled back before the repeated final build, so it is not delivered. A partial try stays whatever the state of its
case (blocked or not): it removed some errors and no next try comes, so taking it out only leaves more errors."""
from __future__ import annotations

from sbmig.core.models import Phase

from .fakes import FakeFixer, journal_events, write_script
from .test_v2_script_loop import BAR, FIXED, FOO, broken_build, make_loop


def test_no_change_try_left_by_the_round_cap_is_rolled_back(cfg, ws):
    fixer = FakeFixer(script_fn=lambda w, c, ctx: write_script({"notes.txt": "useless\n"}))
    loop = make_loop(cfg, ws, broken_build(FOO), fixer)
    pr = loop.run(Phase.VERIFY, "package", 1)                 # one round: the loop ends on the cap, case not blocked
    assert not pr.ok and not pr.blocked
    [a] = pr.attempts
    assert a.outcome == "no_change"
    assert not ws.exists("notes.txt")
    assert "rolled back before the final build (it did not help)" in a.notes
    assert loop.end_rollbacks == 1
    ev = [e for e in journal_events(ws) if e["event"] == "script_rollback"]
    assert ev and ev[-1]["where"] == "loop end" and ev[-1]["attempt"] == a.id


def test_partial_try_of_a_blocked_case_stays(cfg, ws):
    cfg.script.max_llm_tries_per_case = 1                     # the case blocks after its one LLM try
    fixer = FakeFixer(script_fn=lambda w, c, ctx: write_script({FOO: FIXED}))      # fixes FOO, not BAR → partial
    loop = make_loop(cfg, ws, broken_build(FOO, BAR), fixer)
    pr = loop.run(Phase.VERIFY, "package", 6)
    assert not pr.ok and pr.blocked
    [a] = pr.attempts
    assert a.outcome == "partial"
    assert ws.read(FOO) == FIXED                              # kept: the case ended BLOCKED
    assert loop.end_rollbacks == 0


def test_partial_try_of_an_unblocked_case_at_the_round_cap_stays(cfg, ws):
    fixer = FakeFixer(script_fn=lambda w, c, ctx: write_script({FOO: FIXED}))
    loop = make_loop(cfg, ws, broken_build(FOO, BAR), fixer)
    pr = loop.run(Phase.VERIFY, "package", 1)                 # one round: the loop ends on the cap, case not blocked
    assert not pr.ok and not pr.blocked
    [a] = pr.attempts
    assert a.outcome == "partial"
    assert ws.read(FOO) == FIXED                              # kept: it removed an error and no next try comes
    assert "rolled back before the final build (it did not help)" not in a.notes
    assert loop.end_rollbacks == 0
    assert not [e for e in journal_events(ws) if e["event"] == "script_rollback"]


def test_build_ok_leaves_nothing_to_roll_back(cfg, ws):
    fixer = FakeFixer(script_fn=lambda w, c, ctx: write_script({FOO: FIXED}))
    loop = make_loop(cfg, ws, broken_build(FOO), fixer)
    pr = loop.run(Phase.VERIFY, "package", 3)
    assert pr.ok and ws.read(FOO) == FIXED and loop.end_rollbacks == 0
