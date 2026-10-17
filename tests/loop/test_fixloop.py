from __future__ import annotations

from sbmig.core.models import DecisionKind, ErrorKind, FileChange, FixProposal, KBCase, KBMatch, LLMUsage, Phase
from sbmig.loop import BuildFixLoop, LoopBudget
from sbmig.triage import HandlerRegistry, Triager
from sbmig.triage.handlers import Handler

from .fakes import (FakeBuildRunner, FakeFixer, FakeKB, FakeRewriteRunner, bad_build, compile_error, journal_events,
                    ok_build, write_fix)

FOO = "src/main/java/com/x/Foo.java"
BAR = "src/main/java/com/x/Bar.java"


def files_build(ws, call):
    """Each file still containing 'broken' contributes one compile error."""
    errs = [compile_error(f) for f in (FOO, BAR) if "broken" in ws.read(f)]
    return bad_build(*errs) if errs else ok_build()


def make_loop(cfg, ws, profile, *, build=None, fixer=None, kb=None, triager=None, rewrite=None, budget=None,
              dry_run=False):
    triager = triager or Triager(cfg, registry=HandlerRegistry())
    return BuildFixLoop(cfg, ws, build or FakeBuildRunner(ws, fn=files_build), triager, triager.registry,
                        fixer or FakeFixer(), kb or FakeKB(), rewrite or FakeRewriteRunner(ws), profile,
                        plan_notes=["note"], budget=budget or LoopBudget.from_config(cfg), dry_run=dry_run)


def test_llm_fix_then_success(cfg, ws, profile):
    fixer = FakeFixer(fn=lambda w, c, ctx: write_fix(c.file, "class X { fixed }\n"))
    build = FakeBuildRunner(ws, fn=files_build)
    loop = make_loop(cfg, ws, profile, build=build, fixer=fixer)
    pr = loop.run(Phase.SPRING, "test-compile", 5)
    assert pr.ok and pr.build_ok
    assert len(build.calls) == 2                      # build → fix both clusters → build OK
    assert [a.outcome for a in pr.attempts] == ["build_ok", "build_ok"]
    assert all(a.commit and a.applied for a in pr.attempts)
    assert pr.remaining_errors == []
    assert build.calls[1]["head"] == pr.attempts[-1].commit       # the build ran on the committed fix
    ev = journal_events(ws)
    assert sum(e["event"] == "build" for e in ev) >= pr.iterations
    assert {"loop_start", "triage", "decision", "attempt", "attempt_scored", "loop_end"} <= {e["event"] for e in ev}
    ctx = fixer.contexts[0]
    assert ctx.plan_notes == ["note"] and "spring_boot" in ctx.profile_summary and not ctx.use_escalation_model
    assert loop.budget.llm_calls == 2


def test_build_after_last_round_and_max_iterations(cfg, ws, profile):
    n = {"i": 0}

    def fix(w, c, ctx):
        n["i"] += 1
        return write_fix(c.file, f"class X {{ broken {n['i']} }}\n")

    build = FakeBuildRunner(ws, fn=files_build)
    cfg.loop.max_clusters_per_iteration = 1
    loop = make_loop(cfg, ws, profile, build=build, fixer=FakeFixer(fn=fix))
    pr = loop.run(Phase.JDK21, "compile", 2)
    assert not pr.ok
    assert len(build.calls) == 3                 # 2 fix rounds + the build that scores the last round
    assert all(a.outcome != "pending" for a in pr.attempts)
    assert any("max iterations" in n for n in pr.notes)
    assert len(pr.remaining_errors) == 2


def test_stall_detection_escalation_model_and_escalate(cfg, ws, profile):
    n = {"i": 0}

    def fix(w, c, ctx):
        n["i"] += 1
        return write_fix(c.file, f"class X {{ broken v{n['i']} }}\n")

    cfg.loop.max_clusters_per_iteration = 1
    fixer = FakeFixer(fn=fix)
    loop = make_loop(cfg, ws, profile, fixer=fixer)
    pr = loop.run(Phase.SPRING, "compile", 10)
    levels = [r.stall_level for r in pr.iteration_records]
    assert levels[:4] == [0, 1, 2, 3]
    assert [c.use_escalation_model for c in fixer.contexts][:3] == [False, False, True]
    assert fixer.contexts[1].previous_attempts            # previous failed attempt fed back
    blocked = [d for r in pr.iteration_records for d in r.decisions if d.kind == DecisionKind.BLOCKED]
    assert blocked and "repeated_failure" in blocked[0].rationale and "needs a human" in blocked[0].rationale
    assert not pr.ok
    assert pr.blocked and pr.blocked[0].reason.value == "repeated_failure" and pr.blocked[0].attempts == 3
    assert any(e["event"] == "stall" for e in journal_events(ws))
    assert any("escalation model" in n for a in pr.attempts for n in a.notes)


def test_regression_is_reverted(cfg, ws, profile):
    original = ws.read(FOO)

    def build_fn(w, call):
        text = w.read(FOO)
        if "regress" in text:
            return bad_build(compile_error(FOO), *[compile_error(FOO, f"new error {i}", sig=f"new{i}") for i in range(3)])
        if "broken" in text:
            return bad_build(compile_error(FOO))
        return ok_build()

    proposals = [write_fix(FOO, "class Foo { broken regress }\n"), write_fix(FOO, "class Foo { ok }\n")]
    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build_fn), fixer=FakeFixer(proposals))
    pr = loop.run(Phase.SPRING, "compile", 5)
    first = pr.attempts[0]
    assert first.outcome == "reverted" and any("regressed" in n for n in first.notes)
    assert pr.ok
    assert pr.attempts[1].outcome == "build_ok"
    ev = journal_events(ws)
    rev = [e for e in ev if e["event"] == "revert"]
    assert rev and rev[0]["attempts"] == [first.id]
    # the second fix was built from the reverted tree
    assert original != ws.read(FOO) and "regress" not in ws.read(FOO)


def test_regression_not_reverted_when_disabled(cfg, ws, profile):
    cfg.loop.revert_on_regression = False

    def build_fn(w, call):
        if "regress" in w.read(FOO):
            return bad_build(compile_error(FOO), compile_error(FOO, "new", sig="new"))
        return bad_build(compile_error(FOO))

    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build_fn),
                     fixer=FakeFixer([write_fix(FOO, "class Foo { regress }\n")]))
    pr = loop.run(Phase.SPRING, "compile", 1)
    assert pr.attempts[0].outcome == "regressed"


def test_only_latest_regressed_attempt_is_reverted(cfg, ws, profile):
    """Two attempts in one round: the first regresses, the second (latest) improves → no revert."""
    def build_fn(w, call):
        errs = []
        if "broken" in w.read(BAR):
            errs.append(compile_error(BAR))
        foo = w.read(FOO)
        errs.append(compile_error(FOO))
        if "regress" in foo:
            errs.append(compile_error(FOO, "brand new", sig="brand-new"))
        return bad_build(*errs)

    fixer = FakeFixer(fn=lambda w, c, ctx: write_fix(c.file, "class Foo { regress }\n" if c.file == FOO
                                                     else "class Bar { ok }\n"))
    cfg.loop.max_clusters_per_iteration = 2
    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build_fn), fixer=fixer)
    pr = loop.run(Phase.SPRING, "compile", 1)
    outcomes = {a.cluster_signatures[0].split("|")[-1]: a for a in pr.attempts}
    foo, bar = outcomes[FOO], outcomes[BAR]
    # bar commit is the latest and improved → foo cannot be reverted alone
    assert bar.outcome == "improved"
    assert foo.outcome == "regressed" and any("not reverted" in n for n in foo.notes)


def test_llm_budget_exhaustion(cfg, ws, profile):
    budget = LoopBudget(max_llm_calls=1, max_minutes=60)
    fixer = FakeFixer(fn=lambda w, c, ctx: write_fix(c.file, "class X { broken again }\n"))
    loop = make_loop(cfg, ws, profile, fixer=fixer, budget=budget)
    pr = loop.run(Phase.SPRING, "compile", 5)
    assert not pr.ok and budget.llm_calls == 1
    blocked = [d for r in pr.iteration_records for d in r.decisions if d.kind == DecisionKind.BLOCKED]
    assert any("budget_exhausted" in d.rationale for d in blocked)
    assert "blocked" not in [a.decision.kind.value for a in pr.attempts]
    # both clusters end up blocked(budget_exhausted) → the phase ends without burning iterations
    assert {b.reason.value for b in pr.blocked} == {"budget_exhausted"} and len(pr.blocked) == 2
    assert any("only blocked" in n for n in pr.notes)


def test_apply_failed_and_propose_exception(cfg, ws, profile):
    calls = {"n": 0}

    def fn(w, c, ctx):
        calls["n"] += 1
        if c.file == FOO:
            raise RuntimeError("llm down")
        return FixProposal(changes=[FileChange(path=BAR, op="modify", search="nope", replace="x")], model="m",
                           llm_usage=LLMUsage(calls=0))

    loop = make_loop(cfg, ws, profile, fixer=FakeFixer(fn=fn))
    pr = loop.run(Phase.SPRING, "compile", 3)
    assert {a.outcome for a in pr.attempts} == {"apply_failed"}
    assert any("llm down" in e for a in pr.attempts for e in a.apply_errors)
    # counted even when empty/failed; nothing applied → asked again on the same build (no rebuild), then blocked
    assert loop.budget.llm_calls == 4 and pr.iterations == 1
    assert {b.reason.value for b in pr.blocked} == {"repeated_failure"} and len(pr.blocked) == 2
    assert not ws.is_dirty() and loop.llm_unavailable is None     # failures were not consecutive transport errors


def test_test_compile_fallback(cfg, ws, profile):
    terr = compile_error("src/test/java/T.java", "cannot find symbol", kind=ErrorKind.TEST_COMPILE)

    def build_fn(w, call):
        return ok_build() if call["skip_test_compile"] else bad_build(terr)

    build = FakeBuildRunner(ws, fn=build_fn)
    loop = make_loop(cfg, ws, profile, build=build)          # fixer proposes nothing
    pr = loop.run(Phase.SPRING, "test-compile", 3)
    assert pr.ok and pr.build_ok and loop.test_compile_skipped
    assert "test-compile-skipped" in loop.flags
    assert build.calls[-1]["skip_test_compile"] is True
    assert pr.remaining_errors == [terr]
    assert loop.followups and "T.java" in loop.followups[0]
    # compile-only goal: no fallback
    loop2 = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build_fn))
    assert not loop2.run(Phase.JDK21, "compile", 1).ok
    # disabled by config
    cfg.build.allow_skip_test_compile_fallback = False
    loop3 = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build_fn))
    assert not loop3.run(Phase.SPRING, "package", 1).ok


def test_test_compile_fallback_fails(cfg, ws, profile):
    terr = compile_error("src/test/java/T.java", kind=ErrorKind.TEST_COMPILE)
    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, script=[bad_build(terr)]))
    pr = loop.run(Phase.SPRING, "package", 1)
    assert not pr.ok and any("also failed" in n for n in pr.notes)


def test_kb_recipe_batch_one_rewrite_per_iteration(cfg, ws, profile):
    def match(c):
        rid = "kb.FixFoo" if c.file == FOO else "kb.FixBar"
        return KBMatch(recipe_ids=[rid], recipe_files=[f"/kb/{rid}.yml"])

    def effect(w, refs):
        for r in refs:
            f = FOO if r.id == "kb.FixFoo" else BAR
            w.write(f, "class Z { fine }\n")
        return [FOO, BAR]

    kb = FakeKB(match=match)
    rw = FakeRewriteRunner(ws, effect=effect)
    loop = make_loop(cfg, ws, profile, kb=kb, rewrite=rw)
    pr = loop.run(Phase.SPRING, "compile", 3)
    assert pr.ok
    assert len(rw.runs) == 1 and sorted(rw.runs[0]["recipes"]) == ["kb.FixBar", "kb.FixFoo"]
    assert rw.runs[0]["label"] == "spring-1-kb" and "/kb/kb.FixFoo.yml" in rw.runs[0]["files"]
    assert {a.decision.kind for a in pr.attempts} == {DecisionKind.KB_RECIPE}
    assert len({a.commit for a in pr.attempts}) == 1
    assert sorted(kb.credits) == [("kb.FixBar", True), ("kb.FixFoo", True)]
    assert loop.credited


def test_kb_recipe_no_change_then_llm(cfg, ws, profile):
    kb = FakeKB(match=lambda c: KBMatch(recipe_ids=["kb.Noop"]))
    rw = FakeRewriteRunner(ws, ok=False, error="boom")
    fixer = FakeFixer(fn=lambda w, c, ctx: write_fix(c.file, "class Y { ok }\n"))
    loop = make_loop(cfg, ws, profile, kb=kb, rewrite=rw, fixer=fixer)
    pr = loop.run(Phase.SPRING, "compile", 3)
    kinds = [a.decision.kind for a in pr.attempts]
    assert kinds[:2] == [DecisionKind.KB_RECIPE, DecisionKind.KB_RECIPE]
    assert pr.attempts[0].outcome == "apply_failed" and "boom" in pr.attempts[0].apply_errors[0]
    assert DecisionKind.LLM_FIX in kinds and pr.ok
    assert len(rw.runs) == 1                          # recipe marked as run → not retried this phase


def test_kb_patch_replay_and_kb_errors(cfg, ws, profile):
    case = KBCase(id="case-7", summary="fix foo",
                  changes=[FileChange(path=FOO, op="modify", search="broken", replace="ok")])
    kb = FakeKB(match=lambda c: KBMatch(replayable=[case]) if c.file == FOO else KBMatch())
    fixer = FakeFixer(fn=lambda w, c, ctx: write_fix(c.file, "class B { ok }\n"))
    pr = make_loop(cfg, ws, profile, kb=kb, fixer=fixer).run(Phase.SPRING, "compile", 3)
    assert pr.ok
    patch = next(a for a in pr.attempts if a.decision.kind == DecisionKind.KB_PATCH)
    assert patch.outcome == "build_ok" and "case-7" in patch.analysis
    # kb.match raising is tolerated
    pr2 = make_loop(cfg, ws, profile, kb=FakeKB(raise_match=True)).run(Phase.SPRING, "compile", 1)
    assert pr2.iterations == 1


class TouchHandler(Handler):
    id = "touch"
    description = "fixes Foo deterministically"

    def matches(self, cluster, profile, cfg):
        return cluster.file == FOO

    def apply(self, ws, cluster=None, profile=None, cfg=None):
        ws.write(FOO, "class Foo { handled }\n")
        return [FileChange(path=FOO, op="modify")]


class NoopHandler(TouchHandler):
    id = "noop"

    def apply(self, ws, cluster=None, profile=None, cfg=None):
        return []


class CrashHandler(TouchHandler):
    id = "crash"

    def apply(self, ws, cluster=None, profile=None, cfg=None):
        ws.write(FOO, "half-written")
        raise RuntimeError("boom")


def test_handler_decisions(cfg, ws, profile):
    t = Triager(cfg, registry=HandlerRegistry([CrashHandler(), NoopHandler(), TouchHandler()]))
    fixer = FakeFixer(fn=lambda w, c, ctx: write_fix(c.file, "class B { ok }\n"))
    pr = make_loop(cfg, ws, profile, triager=t, fixer=fixer).run(Phase.SPRING, "compile", 5)
    assert pr.ok
    foo = [a for a in pr.attempts if a.cluster_signatures[0].endswith(FOO)]
    assert [a.decision.handler_id for a in foo] == ["crash", "noop", "touch"]
    assert [a.outcome for a in foo] == ["apply_failed", "apply_failed", "build_ok"]
    log = ws._git("log", "--format=%s").stdout
    assert "fix(handler:touch)" in log and "fix(llm)" in log
    # unregistered handler id
    from sbmig.core.models import TriageDecision

    class Fixed:
        registry = HandlerRegistry()

        def cluster(self, errors, profile):
            return t.cluster(errors, profile)

        def decide(self, cluster, kb_match, ctx, profile):
            return TriageDecision(cluster_id=cluster.id, kind=DecisionKind.HANDLER, handler_id="ghost")

    loop = BuildFixLoop(cfg, ws, FakeBuildRunner(ws, script=[bad_build(compile_error())]), Fixed(), HandlerRegistry(),
                        FakeFixer(), FakeKB(), None, profile)
    pr = loop.run(Phase.SPRING, "compile", 2)
    assert pr.attempts[0].outcome == "apply_failed" and "not registered" in pr.attempts[0].apply_errors[0]


def test_masking_and_unmasking(cfg, ws, profile):
    dep = compile_error(None, "could not resolve", sig="dep", kind=ErrorKind.DEPENDENCY_RESOLUTION)
    dep = dep.model_copy(update={"module": "demo", "file": None})
    from sbmig.core.models import GAV
    dep.artifact = GAV.parse("a:b:1")
    build = FakeBuildRunner(ws, script=[bad_build(dep, compile_error(FOO))])
    budget = LoopBudget(max_llm_calls=0, max_minutes=60)        # pom cluster → escalate → unmask
    t = Triager(cfg, registry=HandlerRegistry([TouchHandler()]))
    pr = make_loop(cfg, ws, profile, build=build, triager=t, budget=budget).run(Phase.SPRING, "compile", 1)
    decisions = pr.iteration_records[0].decisions
    assert any(d.kind == DecisionKind.HANDLER and "unmasked" in d.rationale for d in decisions)
    # with LLM budget the compile cluster stays masked
    build = FakeBuildRunner(ws, script=[bad_build(dep, compile_error(FOO))])
    pr = make_loop(cfg, ws, profile, build=build, triager=t).run(Phase.SPRING, "compile", 1)
    assert any(d.kind == DecisionKind.SKIP for d in pr.iteration_records[0].decisions)


def test_dry_run_applies_nothing(cfg, ws, profile):
    head = ws.head()
    fixer = FakeFixer(fn=lambda w, c, ctx: write_fix(c.file, "x"))
    build = FakeBuildRunner(ws, fn=files_build)
    pr = make_loop(cfg, ws, profile, fixer=fixer, build=build, dry_run=True).run(Phase.SPRING, "compile", 3)
    assert ws.head() == head and not fixer.applied and not fixer.contexts
    assert len(build.calls) == 1 and pr.attempts == [] and any("dry-run" in n for n in pr.notes)
    assert pr.iteration_records[0].decisions


def test_time_budget_stops_loop(cfg, ws, profile):
    t = {"now": 0.0}
    budget = LoopBudget(max_llm_calls=10, max_minutes=1, clock=lambda: t["now"])

    def fix(w, c, ctx):
        t["now"] += 120
        return write_fix(c.file, "class X { broken 2 }\n")

    loop = make_loop(cfg, ws, profile, fixer=FakeFixer(fn=fix), budget=budget)
    pr = loop.run(Phase.SPRING, "compile", 5)
    assert not pr.ok and "time-budget-exhausted" in loop.flags
    assert budget.time_left() < 0 and budget.time_exceeded() and not budget.llm_allowed() is False
