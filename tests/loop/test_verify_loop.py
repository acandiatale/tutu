"""The verify loop (v2: THE fix loop). A failed final `clean package` gets the script fix loop (`loop.phases.verify`,
goal `package`, build 1 = the failed final build itself) and the final build is repeated (invariant 1). Nothing is
released from the blocked registry: a case blocked earlier — only possible with a hand-made plan that has an
intermediate loop — stays blocked, and a final build that fails with blocked cases only gets no loop."""
from __future__ import annotations

import pytest

from sbmig.core.models import DecisionKind, ErrorKind, Phase, PlanStep, StepKind
from sbmig.core.workspace import Workspace

from .fakes import (FakeBuildRunner, FakeFixer, FakeKB, bad_build, compile_error, journal_events, make_project,
                    ok_build, v2_plan, write_fix, write_script)
from .test_orchestrator import BAR, FOO, files_build, make_orch

DAO = "src/main/java/com/x/Dao.java"
HCANN = "package org.hibernate.annotations.common.util.impl does not exist"


@pytest.fixture()
def ws(tmp_path):
    src = make_project(tmp_path)
    (src / BAR).write_text("class Bar { ok }\n")                 # one compile file (Foo) keeps the counting simple
    (src / DAO).write_text("class Dao { hcann }\n")
    w = Workspace.from_input(src, tmp_path / "out", run_id="t-run")
    w.git_init()
    return w


def package_only_build(ws, call):
    """Foo's compile error on every goal; the HCANN error only when packaging (while Dao still says `hcann`)."""
    errs = [compile_error(FOO)] if "broken" in ws.read(FOO) else []
    if "package" in call["goal"] and ws.exists(DAO) and "hcann" in ws.read(DAO):
        errs.append(compile_error(DAO, msg=HCANN, l2="compile|package_does_not_exist|org.hibernate"))
    return bad_build(*errs) if errs else ok_build()


def with_jdk21_loop():
    """A hand-made plan with an intermediate loop: the script loop runs in jdk21 too."""
    plan = v2_plan()
    plan.steps.insert(1, PlanStep(id="jdk21.loop", kind=StepKind.BUILD_FIX_LOOP, phase=Phase.JDK21, title="loop",
                                  build_goal="compile", max_iterations=3))
    return plan


def test_failed_final_build_gets_the_script_loop_and_is_repeated(cfg, ws):
    kb = FakeKB()
    build = FakeBuildRunner(ws, fn=package_only_build)
    fixer = FakeFixer(fn=lambda w, c, ctx: write_fix(None, "class X { ok }\n"))
    report = make_orch(cfg, ws, build=build, kb=kb, fixer=fixer).run()
    assert report.success and report.final_build is not None and report.final_build.ok
    # final build failed (2 cases) → loop: build 1 = that build, both cases fixed, `package` passes → final again
    assert [c["label"] for c in build.calls] == ["verify", "verify-2", "verify"]
    assert [c["goal"] for c in build.calls] == ["clean package", "package", "clean package"]
    vp = report.phases[-1]
    assert vp.phase == Phase.VERIFY and vp.ok and vp.build_ok
    assert [a.decision.kind for a in vp.attempts] == [DecisionKind.SCRIPT] * 2 and all(a.applied for a in vp.attempts)
    assert fixer.contexts[-1].phase == Phase.VERIFY and not fixer.contexts[-1].use_escalation_model
    assert any("final build repeated after the fix loop" in n for n in vp.notes)
    ev = journal_events(ws)
    start = [e for e in ev if e["event"] == "verify_loop_start"]
    assert len(start) == 1 and start[0]["errors"] == 2 and start[0]["goal"] == "package"
    assert [e["build_ok"] for e in ev if e["event"] == "verify_loop_end"] == [True]
    assert [e["ok"] for e in ev if e["event"] == "verify"] == [True]       # journaled once, for the final result
    assert report.unresolved == [] and report.blocked == []


@pytest.mark.parametrize("why", ["repeated_failure", "budget_exhausted"])
def test_blocked_cases_are_never_released_and_get_no_loop(cfg, ws, why):
    (ws.root / DAO).write_text("class Dao { ok }\n")
    if why == "repeated_failure":
        cfg.script.max_augments_per_case = 1          # v2.1: one LLM try per case, then repeated_failure
    else:
        cfg.loop.max_llm_calls = 1
    fixer = FakeFixer(fn=lambda w, c, ctx: write_fix(None, "class X { broken again }\n"))
    build = FakeBuildRunner(ws, fn=files_build)
    report = make_orch(cfg, ws, plan=with_jdk21_loop(), build=build, fixer=fixer).run()
    assert not report.success and [b.reason.value for b in report.blocked] == [why]
    assert len(fixer.contexts) == 1
    ev = {e["event"] for e in journal_events(ws)}
    assert "verify_loop_start" not in ev and "blocked_released" not in ev
    assert [c["label"] for c in build.calls].count("verify") == 1
    assert any(f"blocked case(s) only ({why})" in n for n in report.phases[-1].notes)


def test_final_loop_can_be_disabled(cfg, ws):
    cfg.loop.phases["verify"].max_iterations = 0
    build = FakeBuildRunner(ws, fn=package_only_build)
    report = make_orch(cfg, ws, build=build).run()
    assert not report.success and sorted(e.message for e in report.unresolved) == sorted([HCANN, "cannot find symbol"])
    assert "verify_loop_start" not in {e["event"] for e in journal_events(ws)}
    assert [c["label"] for c in build.calls] == ["verify"] and len(report.phases[-1].attempts) == 0


def test_test_compile_fallback_inside_the_loop_carries_into_the_repeated_build(cfg, ws):
    """L7 inside the loop: only a test-compile error remains and no script fixes it → the loop's
    -Dmaven.test.skip=true fallback passes, and the repeated final build must use the same flag (the flags are read
    when the build runs, not before the loop)."""
    cfg.build.skip_test_compile = False
    cfg.script.max_augments_per_case = 1
    (ws.root / DAO).write_text("class Dao { ok }\n")
    (ws.root / FOO).write_text("class Foo { ok }\n")
    test = "src/test/java/com/x/FooTest.java"
    (ws.root / test).parent.mkdir(parents=True)
    (ws.root / test).write_text("class FooTest { junit4 }\n")
    ws.commit("test file")

    def build(w, call):
        if "package" in call["goal"] and not call["skip_test_compile"] and "junit4" in w.read(test):
            return bad_build(compile_error(test, msg="package org.junit.experimental.categories does not exist",
                                           kind=ErrorKind.TEST_COMPILE))
        return ok_build()
    n = {"i": 0}

    def fix(w, c, ctx):
        n["i"] += 1
        return write_script({test: f"class FooTest {{ junit4 still {n['i']} }}\n"})
    runner = FakeBuildRunner(ws, fn=build)
    report = make_orch(cfg, ws, build=runner, fixer=FakeFixer(script_fn=fix)).run()
    assert report.success and "test-compile-skipped" in report.flags
    last = runner.calls[-1]
    assert last["label"] == "verify" and last["goal"] == "clean package" and last["skip_test_compile"] is True
    assert any("test sources do not compile" in f for f in report.manual_followups)
    assert [b.reason.value for b in report.blocked] == ["repeated_failure"]      # kept: tests-only, test-compile skipped
