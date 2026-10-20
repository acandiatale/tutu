"""Round 9 (error catalog L5): the second LLM transport failure in a row pauses the loop once per run
(`llm.cooldown_seconds`) and retries; only a second streak marks the LLM unavailable. 0 = the old behaviour."""
from __future__ import annotations

from sbmig.core.models import BlockReason, FixProposal, LLMUsage, Phase
from sbmig.loop import BuildFixLoop, LoopBudget
from sbmig.triage import HandlerRegistry, Triager

from .fakes import (
    FakeBuildRunner,
    FakeFixer,
    FakeKB,
    FakeRewriteRunner,
    bad_build,
    compile_error,
    journal_events,
    ok_build,
    write_fix,
)

FOO = "src/main/java/com/x/Foo.java"
DOWN = FixProposal(changes=[], analysis="llm-error: LLM request failed after 4 attempt(s) (status=503): busy",
                   model="m", llm_usage=LLMUsage(calls=1))


def make_loop(cfg, ws, profile, *, build, fixer, budget=None):
    t = Triager(cfg, registry=HandlerRegistry())
    return BuildFixLoop(cfg, ws, build, t, t.registry, fixer, FakeKB(), FakeRewriteRunner(ws), profile,
                        budget=budget or LoopBudget.from_config(cfg))


def _recording_fixer(loop_ref: list, answers: list):
    """Returns `answers` in order (the last repeats); records loop.llm_unavailable at each call."""
    seen: list = []

    def fn(w, c, ctx):
        seen.append((loop_ref[0].llm_unavailable, [a.id for a in ctx.previous_attempts]))
        a = answers.pop(0) if len(answers) > 1 else answers[0]
        return a(w) if callable(a) else a
    return FakeFixer(fn=fn), seen


def test_cooldown_then_one_failure_still_available_then_unavailable(cfg, ws, profile, no_llm_cooldown_sleep):
    assert cfg.llm.cooldown_seconds == 60                                       # the shipped default
    ref: list = []
    fixer, seen = _recording_fixer(ref, [DOWN])
    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, script=[bad_build(compile_error(FOO))]), fixer=fixer)
    ref.append(loop)
    pr = loop.run(Phase.SPRING, "compile", 6)
    # F, F → cool-down (one pause of 60 s) → F (still available) → F → unavailable
    assert len(fixer.contexts) == 4
    assert no_llm_cooldown_sleep == [60]
    assert [u for u, _ in seen] == [None, None, None, None]                     # available before each call
    assert loop.llm_unavailable and "status=503" in loop.llm_unavailable
    assert loop.llm_cooldown_used and {"llm-cooldown", "llm-unavailable"} <= set(loop.flags)
    # the cool-down round does not count against max_llm_attempts_per_cluster: blocked as llm_unavailable,
    # not repeated_failure, and the third call did not see the two cooled-down attempts
    assert [b.reason for b in pr.blocked] == [BlockReason.LLM_UNAVAILABLE]
    assert seen[2][1] == [] and len(seen[3][1]) == 1
    ev = journal_events(ws)
    cd = [e for e in ev if e["event"] == "llm_cooldown"]
    assert len(cd) == 1 and cd[0]["seconds"] == 60 and cd[0]["phase"] == "spring" and cd[0]["clusters"]
    assert "status=503" in cd[0]["reason"]
    un = [e for e in ev if e["event"] == "llm_unavailable"]
    assert len(un) == 1 and un[0]["cooldown_used"] is True
    assert ev.index(cd[0]) < ev.index(un[0])
    notes = [n for a in pr.attempts for n in a.notes]
    assert any(n.startswith("LLM cool-down: 2 transport/config failures in a row") for n in notes)


def test_cooldown_then_success_keeps_the_llm(cfg, ws, profile, no_llm_cooldown_sleep):
    ref: list = []
    fixer, _seen = _recording_fixer(ref, [DOWN, DOWN, write_fix(FOO, "class Foo { fixed }\n")])

    def build(w, call):
        return ok_build() if "fixed" in w.read(FOO) else bad_build(compile_error(FOO))

    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build), fixer=fixer)
    ref.append(loop)
    pr = loop.run(Phase.SPRING, "compile", 6)
    assert pr.build_ok and loop.llm_unavailable is None and pr.blocked == []
    assert no_llm_cooldown_sleep == [60] and "llm-cooldown" in loop.flags and "llm-unavailable" not in loop.flags
    assert len(fixer.contexts) == 3


def test_cooldown_zero_is_the_old_behaviour(cfg, ws, profile, no_llm_cooldown_sleep):
    cfg.llm.cooldown_seconds = 0
    fixer = FakeFixer(fn=lambda w, c, ctx: DOWN)
    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, script=[bad_build(compile_error(FOO))]), fixer=fixer)
    pr = loop.run(Phase.SPRING, "compile", 6)
    assert len(fixer.contexts) == 2 and loop.llm_unavailable
    assert no_llm_cooldown_sleep == [] and "llm-cooldown" not in loop.flags
    assert not any(e["event"] == "llm_cooldown" for e in journal_events(ws))
    assert [b.reason for b in pr.blocked] == [BlockReason.LLM_UNAVAILABLE]


def test_cooldown_skipped_when_the_time_budget_would_run_out(cfg, ws, profile, no_llm_cooldown_sleep):
    budget = LoopBudget(max_llm_calls=60, max_minutes=0.5, clock=lambda: 0.0)     # 30 s left < 60 s cool-down
    fixer = FakeFixer(fn=lambda w, c, ctx: DOWN)
    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, script=[bad_build(compile_error(FOO))]),
                     fixer=fixer, budget=budget)
    pr = loop.run(Phase.SPRING, "compile", 6)
    assert len(fixer.contexts) == 2 and loop.llm_unavailable and no_llm_cooldown_sleep == []
    assert not loop.llm_cooldown_used and "llm-cooldown" not in loop.flags
    assert any("cool-down skipped" in n for a in pr.attempts for n in a.notes)


def test_injected_sleep_is_used(cfg, ws, profile, no_llm_cooldown_sleep):
    cfg.llm.cooldown_seconds = 5
    mine: list = []
    fixer = FakeFixer(fn=lambda w, c, ctx: DOWN)
    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, script=[bad_build(compile_error(FOO))]), fixer=fixer)
    loop._sleep = mine.append
    loop.run(Phase.SPRING, "compile", 6)
    assert mine == [5] and no_llm_cooldown_sleep == []
