"""Fix-loop mechanics (v2: fix scripts): build gate, rounds, stall / escalation, regression revert, budgets,
apply failures, test-compile fallback, masking, dry run. The script ladder itself: test_v2_script_loop.py."""
from __future__ import annotations

from sbmig.core.models import GAV, DecisionKind, ErrorKind, Phase, ScriptProposal
from sbmig.loop import BuildFixLoop, LoopBudget
from sbmig.triage import Triager

from .fakes import (FakeBuildRunner, FakeFixer, FakeKB, FakeScriptRunner, bad_build, case_of,
                    compile_error, journal_events, ok_build, script, write_script)

FOO = "src/main/java/com/x/Foo.java"
BAR = "src/main/java/com/x/Bar.java"


def files_build(ws, call):
    """Each file still containing 'broken' contributes one compile error (one case: L2 compile|x)."""
    errs = [compile_error(f) for f in (FOO, BAR) if "broken" in ws.read(f)]
    return bad_build(*errs) if errs else ok_build()


def two_case_build(ws, call):
    """Foo and Bar are different cases (L2 per file)."""
    errs = [compile_error(f, l2=f"compile|x|{f.rsplit('/', 1)[-1]}") for f in (FOO, BAR) if "broken" in ws.read(f)]
    return bad_build(*errs) if errs else ok_build()


def make_loop(cfg, ws, profile, *, build=None, fixer=None, kb=None, triager=None, budget=None, dry_run=False,
              runner=None):
    triager = triager or Triager(cfg)
    return BuildFixLoop(cfg, ws, build or FakeBuildRunner(ws, fn=files_build), triager,
                        fixer or FakeFixer(), kb or FakeKB(), profile,
                        plan_notes=["note"], budget=budget or LoopBudget.from_config(cfg), dry_run=dry_run,
                        script_runner=runner or FakeScriptRunner())


def fix_case(content="class X { fixed }\n"):
    return lambda w, c, ctx: write_script({e.file: content for e in c.errors if e.file})


def test_script_fix_then_success(cfg, ws, profile):
    fixer = FakeFixer(script_fn=fix_case())
    build = FakeBuildRunner(ws, fn=files_build)
    loop = make_loop(cfg, ws, profile, build=build, fixer=fixer)
    pr = loop.run(Phase.VERIFY, "package", 5)
    assert pr.ok and pr.build_ok
    assert len(build.calls) == 2                      # build → one script for the case (both files) → build OK
    [a] = pr.attempts
    assert a.outcome == "build_ok" and a.commit and a.applied and a.cluster_id == f"case:{case_of('compile|x')}"
    assert pr.remaining_errors == []
    assert build.calls[1]["head"] == a.commit         # the build ran on the committed fix
    ev = journal_events(ws)
    assert sum(e["event"] == "build" for e in ev) >= pr.iterations
    assert {"loop_start", "triage", "decision", "attempt", "attempt_scored", "loop_end"} <= {e["event"] for e in ev}
    ctx = fixer.contexts[0]
    assert ctx.plan_notes == ["note"] and "spring_boot" in ctx.profile_summary and not ctx.use_escalation_model
    assert loop.budget.llm_calls == 1


def test_build_after_last_round_and_max_iterations(cfg, ws, profile):
    n = {"i": 0}

    def fix(w, c, ctx):
        n["i"] += 1
        return write_script({e.file: f"class X {{ broken {n['i']} }}\n" for e in c.errors})

    build = FakeBuildRunner(ws, fn=files_build)
    loop = make_loop(cfg, ws, profile, build=build, fixer=FakeFixer(script_fn=fix))
    pr = loop.run(Phase.VERIFY, "package", 2)
    assert not pr.ok
    assert len(build.calls) == 3                 # 2 fix rounds + the build that scores the last round
    assert all(a.outcome != "pending" for a in pr.attempts)
    assert any("max iterations" in n for n in pr.notes)
    assert len(pr.remaining_errors) == 2


def test_stall_escalation_model_and_repeated_failure(cfg, ws, profile):
    n = {"i": 0}

    def fix(w, c, ctx):
        n["i"] += 1
        return write_script({"notes.txt": f"v{n['i']}\n"})

    cfg.loop.stall_limit = 1
    cfg.script.max_augments_per_case = 3
    fixer = FakeFixer(script_fn=fix)
    loop = make_loop(cfg, ws, profile, fixer=fixer)
    pr = loop.run(Phase.VERIFY, "package", 10)
    levels = [r.stall_level for r in pr.iteration_records]
    assert levels[:3] == [0, 1, 2]
    assert [c.use_escalation_model for c in fixer.contexts] == [False, True, True]     # stall_limit 1
    assert fixer.contexts[1].previous_attempts and fixer.contexts[1].script_base.version == 1
    blocked = [d for r in pr.iteration_records for d in r.decisions if d.kind == DecisionKind.BLOCKED]
    assert blocked and "repeated_failure" in blocked[0].rationale and "needs a human" in blocked[0].rationale
    assert not pr.ok
    assert pr.blocked and pr.blocked[0].reason.value == "repeated_failure" and pr.blocked[0].attempts == 3
    assert any(e["event"] == "stall" for e in journal_events(ws))
    assert any("escalation model" in n for a in pr.attempts for n in a.notes)


def test_regression_is_reverted(cfg, ws, profile):
    def build_fn(w, call):
        text = w.read(FOO)
        if "regress" in text:
            return bad_build(compile_error(FOO), *[compile_error(FOO, f"new error {i}", sig=f"new{i}", l2=f"n|{i}")
                                                   for i in range(3)])
        if "broken" in text:
            return bad_build(compile_error(FOO))
        return ok_build()

    fixer = FakeFixer(scripts=[write_script({FOO: "class Foo { broken regress }\n"}),
                               write_script({FOO: "class Foo { ok }\n"})])
    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build_fn), fixer=fixer)
    pr = loop.run(Phase.VERIFY, "package", 5)
    first = pr.attempts[0]
    assert first.outcome == "reverted" and any("regressed" in n for n in first.notes)
    assert pr.ok and pr.attempts[1].outcome == "build_ok"
    # a reverted attempt is not rolled back again; the augment ran on the reverted tree
    assert not any(n.startswith("rolled back") for n in first.notes)
    rev = [e for e in journal_events(ws) if e["event"] == "revert"]
    assert rev and rev[0]["attempts"] == [first.id]
    assert "regress" not in ws.read(FOO)


def test_regression_not_reverted_when_disabled(cfg, ws, profile):
    cfg.loop.revert_on_regression = False

    def build_fn(w, call):
        if "regress" in w.read(FOO):
            return bad_build(compile_error(FOO), compile_error(FOO, "new", sig="new", l2="n|x"))
        return bad_build(compile_error(FOO))

    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build_fn),
                     fixer=FakeFixer(scripts=[write_script({FOO: "class Foo { regress }\n"})]))
    pr = loop.run(Phase.VERIFY, "package", 1)
    assert pr.attempts[0].outcome == "regressed"


def test_only_latest_regressed_attempt_is_reverted(cfg, ws, profile):
    """Two cases in one round: the first regresses, the second (latest commit) improves → no revert."""
    def build_fn(w, call):
        errs = []
        if "broken" in w.read(BAR):
            errs.append(compile_error(BAR, l2="compile|x|bar"))
        errs += [compile_error(FOO, l2="compile|x|foo", line=n) for n in (1, 2)]   # 2 errors: Foo's case first
        if "regress" in w.read(FOO):
            errs.append(compile_error(FOO, "brand new", sig="brand-new", l2="compile|y"))
        return bad_build(*errs)

    def fix(w, c, ctx):
        f = c.errors[0].file
        return write_script({f: "class Foo { regress }\n" if f == FOO else "class Bar { ok }\n"})
    cfg.loop.max_clusters_per_iteration = 2
    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build_fn), fixer=FakeFixer(script_fn=fix))
    pr = loop.run(Phase.VERIFY, "package", 1)
    by = {a.cluster_errors[0].file: a for a in pr.attempts}
    assert by[BAR].outcome == "improved"
    assert by[FOO].outcome == "regressed" and any("not reverted" in n for n in by[FOO].notes)


def test_llm_budget_exhaustion(cfg, ws, profile):
    budget = LoopBudget(max_llm_calls=1, max_minutes=60)
    fixer = FakeFixer(script_fn=fix_case("class X { broken again }\n"))
    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=two_case_build), fixer=fixer, budget=budget)
    pr = loop.run(Phase.VERIFY, "package", 5)
    assert not pr.ok and budget.llm_calls == 1
    blocked = [d for r in pr.iteration_records for d in r.decisions if d.kind == DecisionKind.BLOCKED]
    assert any("budget_exhausted" in d.rationale for d in blocked)
    assert {b.reason.value for b in pr.blocked} == {"budget_exhausted"} and len(pr.blocked) == 2
    assert any("only blocked" in n for n in pr.notes)


def test_no_script_and_propose_exception_are_apply_failed(cfg, ws, profile):
    cfg.script.max_augments_per_case = 2

    def fn(w, c, ctx):
        if c.errors[0].file == FOO:
            raise RuntimeError("llm down")
        return ScriptProposal(code="", analysis="I cannot", error="no <script> block", model="m")
    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=two_case_build), fixer=FakeFixer(script_fn=fn))
    pr = loop.run(Phase.VERIFY, "package", 3)
    assert {a.outcome for a in pr.attempts} == {"apply_failed"}
    assert any("llm down" in e for a in pr.attempts for e in a.apply_errors)
    assert any("LLM answered with neither a script nor edits: no <script> block" in e
               for a in pr.attempts for e in a.apply_errors)
    # counted even when empty / failed; nothing applied → asked again on the same build (no rebuild), then blocked
    assert loop.budget.llm_calls == 4 and pr.iterations == 1
    assert {b.reason.value for b in pr.blocked} == {"repeated_failure"} and len(pr.blocked) == 2
    assert not ws.is_dirty() and loop.llm_unavailable is None     # failures were not consecutive transport errors


def test_test_compile_fallback(cfg, ws, profile):
    cfg.build.skip_test_compile = False
    terr = compile_error("src/test/java/T.java", "cannot find symbol", kind=ErrorKind.TEST_COMPILE)

    def build_fn(w, call):
        return ok_build() if call["skip_test_compile"] else bad_build(terr)

    build = FakeBuildRunner(ws, fn=build_fn)
    loop = make_loop(cfg, ws, profile, build=build)          # the fixer proposes no script
    pr = loop.run(Phase.VERIFY, "test-compile", 3)
    assert pr.ok and pr.build_ok and loop.test_compile_skipped
    assert "test-compile-skipped" in loop.flags
    assert build.calls[-1]["skip_test_compile"] is True
    assert pr.remaining_errors == [terr]
    assert loop.followups and "T.java" in loop.followups[0]
    loop2 = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build_fn))       # compile-only goal: no fallback
    assert not loop2.run(Phase.VERIFY, "compile", 1).ok
    cfg.build.allow_skip_test_compile_fallback = False                               # disabled by config
    loop3 = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build_fn))
    assert not loop3.run(Phase.VERIFY, "package", 1).ok


def test_test_compile_fallback_fails(cfg, ws, profile):
    cfg.build.skip_test_compile = False
    terr = compile_error("src/test/java/T.java", kind=ErrorKind.TEST_COMPILE)
    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, script=[bad_build(terr)]))
    pr = loop.run(Phase.VERIFY, "package", 1)
    assert not pr.ok and any("also failed" in n for n in pr.notes)


def test_masking_and_unmasking(cfg, ws, profile):
    dep = compile_error(None, "could not resolve", sig="dep", kind=ErrorKind.DEPENDENCY_RESOLUTION,
                        l2="dependency_resolution|a:b")
    dep = dep.model_copy(update={"module": "demo", "file": None, "artifact": GAV.parse("a:b:1")})
    kb = FakeKB()
    kb.cases.seed(case_of("compile|x"), write_script({FOO: "class Foo { ok }\n"}).code)
    budget = LoopBudget(max_llm_calls=-1, max_minutes=60)    # no LLM: the pom case is blocked → the compile case is unmasked
    build = FakeBuildRunner(ws, script=[bad_build(dep, compile_error(FOO))])
    pr = make_loop(cfg, ws, profile, build=build, kb=kb, budget=budget).run(Phase.VERIFY, "package", 1)
    decisions = pr.iteration_records[0].decisions
    assert any(d.kind == DecisionKind.SCRIPT and "unmasked" in d.rationale and "reuse" in d.rationale
               for d in decisions)
    # with an LLM budget the pom case gets the script and the compile case stays masked
    build = FakeBuildRunner(ws, script=[bad_build(dep, compile_error(FOO))])
    pr = make_loop(cfg, ws, profile, build=build, fixer=FakeFixer(scripts=[script("p.log('pom')")])) \
        .run(Phase.VERIFY, "package", 1)
    assert any(d.kind == DecisionKind.SKIP for d in pr.iteration_records[0].decisions)


def test_dry_run_applies_nothing(cfg, ws, profile):
    head = ws.head()
    fixer = FakeFixer(script_fn=fix_case())
    build = FakeBuildRunner(ws, fn=files_build)
    pr = make_loop(cfg, ws, profile, fixer=fixer, build=build, dry_run=True).run(Phase.VERIFY, "package", 3)
    assert ws.head() == head and not fixer.contexts
    assert len(build.calls) == 1 and pr.attempts == [] and any("dry-run" in n for n in pr.notes)
    assert pr.iteration_records[0].decisions


def test_time_budget_stops_loop(cfg, ws, profile):
    t = {"now": 0.0}
    budget = LoopBudget(max_llm_calls=10, max_minutes=1, clock=lambda: t["now"])

    def fix(w, c, ctx):
        t["now"] += 120
        return write_script({e.file: "class X { broken 2 }\n" for e in c.errors})

    loop = make_loop(cfg, ws, profile, fixer=FakeFixer(script_fn=fix), budget=budget)
    pr = loop.run(Phase.VERIFY, "package", 5)
    assert not pr.ok and "time-budget-exhausted" in loop.flags
    assert budget.time_left() < 0 and budget.time_exceeded()
    assert {b.reason.value for b in pr.blocked} == {"budget_exhausted"}
