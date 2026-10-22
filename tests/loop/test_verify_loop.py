"""Final verify loop (2026-10-22, company run: `package org.hibernate.annotations.common.util.impl does not exist`
surfaced only in the final `clean package`, because `plan.parent_phase: spring` leaves no package-goal loop before
verify). A failed final build gets one last build-fix loop (`loop.phases.verify`) and the final build is repeated;
clusters blocked earlier for repeated_failure are released with `loop.verify_extra_llm_attempts` more LLM attempts
(escalation model); policy / budget blocks never trigger the loop; the KB harvests the verify phase."""
from __future__ import annotations

import pytest

from sbmig.core.models import DecisionKind, ErrorKind, Phase, StepKind
from sbmig.core.workspace import Workspace

from .fakes import (
    FakeBuildRunner,
    FakeFixer,
    FakeKB,
    FakeRestructurer,
    bad_build,
    boot_plan,
    compile_error,
    journal_events,
    make_project,
    ok_build,
    write_fix,
)
from .test_orchestrator import BAR, FOO, files_build, make_orch

DAO = "src/main/java/com/x/Dao.java"
HCANN = "package org.hibernate.annotations.common.util.impl does not exist"


@pytest.fixture()
def ws(tmp_path):
    src = make_project(tmp_path)
    (src / BAR).write_text("class Bar { ok }\n")                 # one compile cluster (Foo) keeps the counting simple
    (src / DAO).write_text("class Dao { hcann }\n")
    w = Workspace.from_input(src, tmp_path / "out", run_id="t-run")
    w.git_init()
    return w


def company_plan():
    """jdk21 / spring loops + verify, no restructure loop — what `plan.parent_phase: spring` produces."""
    plan = boot_plan()
    plan.steps = [s for s in plan.steps if not (s.kind == StepKind.BUILD_FIX_LOOP and s.phase == Phase.RESTRUCTURE)]
    return plan


def package_only_build(ws, call):
    """Foo's compile error on every goal; the HCANN error only when packaging (while Dao still says `hcann`)."""
    errs = [compile_error(FOO)] if "broken" in ws.read(FOO) else []
    if "package" in call["goal"] and ws.exists(DAO) and "hcann" in ws.read(DAO):
        errs.append(compile_error(DAO, msg=HCANN))
    return bad_build(*errs) if errs else ok_build()


def test_failed_final_build_gets_a_last_fix_loop_and_is_repeated(cfg, ws):
    kb = FakeKB()
    build = FakeBuildRunner(ws, fn=package_only_build)
    fixer = FakeFixer(fn=lambda w, c, ctx: write_fix(c.file, "class X { ok }\n"))
    report = make_orch(cfg, ws, plan=company_plan(), build=build, kb=kb, fixer=fixer).run()
    assert report.success and report.final_build is not None and report.final_build.ok
    labels = [c["label"] for c in build.calls]
    # final build failed → loop builds `package` (fails, fixed, passes) → final `clean package` again
    assert labels[-4:] == ["verify", "verify-1", "verify-2", "verify"]
    assert [c["goal"] for c in build.calls[-4:]] == ["clean package", "package", "package", "clean package"]
    vp = report.phases[-1]
    assert vp.phase == Phase.VERIFY and vp.ok and vp.build_ok
    assert [a.decision.kind for a in vp.attempts] == [DecisionKind.LLM_FIX] and vp.attempts[0].applied
    assert fixer.contexts[-1].phase == Phase.VERIFY and fixer.contexts[-1].use_escalation_model
    assert any("final build repeated after the fix loop" in n for n in vp.notes)
    ev = journal_events(ws)
    start = [e for e in ev if e["event"] == "verify_loop_start"]
    assert len(start) == 1 and start[0]["errors"] == 1 and start[0]["releasable"] == [] and start[0]["goal"] == "package"
    assert [(e["build_ok"], e["released"]) for e in ev if e["event"] == "verify_loop_end"] == [(True, [])]
    assert [e["ok"] for e in ev if e["event"] == "verify"] == [True]       # journaled once, for the final result
    assert kb.harvests[-1] == Phase.VERIFY                       # the fix is learned like any phase fix
    assert report.unresolved == [] and report.blocked == []


def test_repeated_failure_clusters_are_released_with_extra_attempts(cfg, ws):
    cfg.loop.max_llm_attempts_per_cluster = 1
    cfg.loop.stall_limit = 1                  # limit = max(1, 2) = 2 failed LLM attempts → repeated_failure
    (ws.root / DAO).write_text("class Dao { ok }\n")

    def fix(w, c, ctx):                       # only the final verify loop produces a working patch
        good = ctx.phase == Phase.VERIFY
        return write_fix(c.file, "class X { ok }\n" if good else "class X { broken again }\n")
    fixer = FakeFixer(fn=fix)
    build = FakeBuildRunner(ws, fn=files_build)
    report = make_orch(cfg, ws, plan=company_plan(), build=build, fixer=fixer).run()
    assert report.success and report.blocked == [] and report.unresolved == []
    phases = [c.phase for c in fixer.contexts]
    assert phases[:2] == [Phase.JDK21, Phase.JDK21] and phases[-1] == Phase.VERIFY   # blocked after 2, 1 more at the end
    assert Phase.SPRING not in phases                                               # the spring loop skipped the blocked cluster
    ev = journal_events(ws)
    rel = [e for e in ev if e["event"] == "blocked_released"]
    assert len(rel) == 1 and rel[0]["reason"] == "repeated_failure" and rel[0]["extra_llm_attempts"] == 2
    assert rel[0]["blocked_in"] == "jdk21" and rel[0]["phase"] == "verify" and rel[0]["iteration"] == 1
    assert [e["releasable"] for e in ev if e["event"] == "verify_loop_start"] == [[rel[0]["id"]]]
    assert [e["released"] for e in ev if e["event"] == "verify_loop_end"] == [[rel[0]["id"]]]
    assert any("released blocked" in n and "repeated_failure" in n for n in report.phases[-1].notes)
    assert all(not pr.blocked for pr in report.phases)                               # released items leave every phase


def test_no_release_when_extra_attempts_are_zero(cfg, ws):
    cfg.loop.max_llm_attempts_per_cluster = 1
    cfg.loop.stall_limit = 1
    cfg.loop.verify_extra_llm_attempts = 0
    (ws.root / DAO).write_text("class Dao { ok }\n")
    fixer = FakeFixer(fn=lambda w, c, ctx: write_fix(c.file, "class X { broken again }\n"))
    build = FakeBuildRunner(ws, fn=files_build)
    report = make_orch(cfg, ws, plan=company_plan(), build=build, fixer=fixer).run()
    assert not report.success and [b.reason.value for b in report.blocked] == ["repeated_failure"]
    assert len(fixer.contexts) == 2 and "verify_loop_start" not in {e["event"] for e in journal_events(ws)}
    assert [c["label"] for c in build.calls].count("verify") == 1
    assert any("blocked cluster(s) only (repeated_failure)" in n for n in report.phases[-1].notes)


def test_budget_blocks_never_trigger_the_final_loop(cfg, ws):
    cfg.loop.max_llm_calls = 1
    build = FakeBuildRunner(ws, fn=lambda w, c: bad_build(compile_error()))
    fixer = FakeFixer(fn=lambda w, c, ctx: write_fix(FOO, "class F { broken 2 }"))
    report = make_orch(cfg, ws, plan=company_plan(), build=build, fixer=fixer).run()
    assert not report.success and [b.reason.value for b in report.blocked] == ["budget_exhausted"]
    assert "verify_loop_start" not in {e["event"] for e in journal_events(ws)}
    assert build.calls[-1]["label"] == "verify" and [c["label"] for c in build.calls].count("verify") == 1


def test_final_loop_can_be_disabled(cfg, ws):
    cfg.loop.phases["verify"].max_iterations = 0
    build = FakeBuildRunner(ws, fn=package_only_build)
    report = make_orch(cfg, ws, plan=company_plan(), build=build).run()
    assert not report.success and [e.message for e in report.unresolved] == [HCANN]
    assert "verify_loop_start" not in {e["event"] for e in journal_events(ws)}
    assert [c["label"] for c in build.calls][-1] == "verify" and len(report.phases[-1].attempts) == 0


def test_hidden_blocked_cluster_is_released_when_it_surfaces_in_a_later_round(cfg, ws):
    """Reactor order: Bar's error is hidden while Foo fails. Bar was blocked (repeated_failure) in jdk21; the
    restructure step breaks Foo again, so the first verify build shows only Foo. Round 1 fixes Foo, round 2 surfaces
    Bar → its blocked item is released inside the loop and the extra attempt fixes it."""
    cfg.loop.max_llm_attempts_per_cluster = 1
    cfg.loop.stall_limit = 1
    (ws.root / DAO).write_text("class Dao { ok }\n")

    def ordered_build(w, call):                   # Foo first; Bar only once Foo compiles
        if "broken" in w.read(FOO):
            return bad_build(compile_error(FOO))
        if "broken" in w.read(BAR):
            return bad_build(compile_error(BAR))
        return ok_build()

    seen: list[tuple[Phase, str]] = []

    def fix(w, c, ctx):                           # Bar is only fixable in the final loop; Foo always
        seen.append((ctx.phase, c.file))
        good = c.file == FOO or ctx.phase == Phase.VERIFY
        return write_fix(c.file, "class X { ok }\n" if good else "class X { broken again }\n")

    class BreakFoo(FakeRestructurer):
        def apply(self, ws, profile, cfg, *, mode, params=None):
            ws.write(FOO, "class Foo { broken by restructure }\n")
            return super().apply(ws, profile, cfg, mode=mode, params=params)
    (ws.root / BAR).write_text("class Bar { broken }\n")
    plan = boot_plan(max_it=5)                # enough rounds for Bar to be BLOCKED (not just "max iterations") in jdk21
    plan.steps = [s for s in plan.steps if s.id in ("jdk21.loop", "restructure.apply", "verify.build")]
    fixer = FakeFixer(fn=fix)
    build = FakeBuildRunner(ws, fn=ordered_build)
    report = make_orch(cfg, ws, plan=plan, build=build, fixer=fixer, restructurer=BreakFoo()).run()
    assert report.success and report.blocked == [] and report.unresolved == []
    ev = journal_events(ws)
    start = [e for e in ev if e["event"] == "verify_loop_start"]
    assert len(start) == 1 and start[0]["releasable"] == []            # Bar was hidden in the first verify build
    rel = [e for e in ev if e["event"] == "blocked_released"]
    assert len(rel) == 1 and rel[0]["blocked_in"] == "jdk21" and rel[0]["iteration"] == 2
    assert [e["released"] for e in ev if e["event"] == "verify_loop_end"] == [[rel[0]["id"]]]
    assert [f for ph, f in seen if ph == Phase.VERIFY] == [FOO, BAR]
    assert all(not pr.blocked for pr in report.phases)


def test_test_compile_fallback_inside_the_final_loop_carries_into_the_repeated_build(cfg, ws):
    """L7 inside the final loop: only a test-compile error remains and nobody can fix it → the loop's
    -Dmaven.test.skip=true fallback passes, and the repeated final build must use the same flag (the flags are read
    when the build runs, not before the loop)."""
    cfg.loop.max_llm_attempts_per_cluster = 1
    cfg.loop.stall_limit = 1
    cfg.loop.verify_extra_llm_attempts = 0          # 2 failed attempts → blocked inside the loop (3 rounds otherwise)
    (ws.root / DAO).write_text("class Dao { ok }\n")
    (ws.root / FOO).write_text("class Foo { ok }\n")
    test = "src/test/java/com/x/FooTest.java"
    (ws.root / test).parent.mkdir(parents=True)
    (ws.root / test).write_text("class FooTest { junit4 }\n")

    def build(w, call):
        if "package" in call["goal"] and not call["skip_test_compile"] and "junit4" in w.read(test):
            return bad_build(compile_error(test, msg="package org.junit.experimental.categories does not exist",
                                           kind=ErrorKind.TEST_COMPILE))
        return ok_build()
    fixer = FakeFixer(fn=lambda w, c, ctx: write_fix(c.file, "class FooTest { junit4 still }\n"))
    runner = FakeBuildRunner(ws, fn=build)
    report = make_orch(cfg, ws, plan=company_plan(), build=runner, fixer=fixer).run()
    assert report.success and "test-compile-skipped" in report.flags
    last = runner.calls[-1]
    assert last["label"] == "verify" and last["goal"] == "clean package" and last["skip_test_compile"] is True
    assert any("test sources do not compile" in f for f in report.manual_followups)
    assert [b.reason.value for b in report.blocked] == ["repeated_failure"]      # kept: tests-only, test-compile skipped


def test_default_plan_with_restructure_loop_still_gets_the_last_chance(cfg, ws):
    """The restructure loop (goal package) ran out of rounds → verify fails → the final loop still tries once more."""
    cfg.loop.phases["restructure"].max_iterations = 1
    calls = {"n": 0}

    def flaky_package(w, call):               # the package error survives the restructure loop's single round
        calls["n"] += 1
        res = package_only_build(w, call)
        return res
    seen = []

    def fix(w, c, ctx):
        seen.append((ctx.phase, c.file))
        if c.file == DAO and ctx.phase != Phase.VERIFY:
            return write_fix(c.file, "class Dao { hcann still }\n")
        return write_fix(c.file, "class X { ok }\n")
    build = FakeBuildRunner(ws, fn=flaky_package)
    report = make_orch(cfg, ws, plan=boot_plan(), build=build, fixer=FakeFixer(fn=fix)).run()
    assert report.success and (Phase.VERIFY, DAO) in seen and (Phase.RESTRUCTURE, DAO) in seen
    assert [c["label"] for c in build.calls][-1] == "verify" and report.final_build.ok
