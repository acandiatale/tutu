"""v2 script fix loop (docs/INTERFACES.md "Script fix loop (L)"): cases across files, the ladder reuse → augment →
new → BLOCKED, scripts stored before they run and credited after scoring, rollback before the next try, no partial
application of a failed script."""
from __future__ import annotations

from sbmig.core.models import BlockReason, BuildError, DecisionKind, ErrorKind, Phase
from sbmig.loop import BuildFixLoop, LazyFixer, LoopBudget, NullKnowledgeBase
from sbmig.loop.error_tracker import ErrorTracker
from sbmig.triage import Triager

from .fakes import (
    FakeBuildRunner,
    FakeFixer,
    FakeKB,
    FakeScriptRunner,
    bad_build,
    case_of,
    compile_error,
    journal_events,
    make_profile,
    ok_build,
    script,
    write_script,
)

FOO = "src/main/java/com/x/Foo.java"
BAR = "src/main/java/com/x/Bar.java"
BAZ = "src/main/java/com/x/Baz.java"
L2 = "compile|cannot_find_symbol|clientIp"
CASE = case_of(L2)
FIXED = "class X { ok }\n"


def broken_build(*files: str, l2: str = L2):
    """Every listed file still saying `broken` has one error of case `l2`."""
    def fn(ws, call):
        errs = [compile_error(f, l2=l2) for f in files if ws.exists(f) and "broken" in ws.read(f)]
        return bad_build(*errs) if errs else ok_build()
    return fn


def make_loop(cfg, ws, build, fixer=None, *, kb=None, runner=None, profile=None, **kw) -> BuildFixLoop:
    triager = Triager(cfg)
    return BuildFixLoop(cfg, ws, FakeBuildRunner(ws, fn=build), triager,
                        fixer or FakeFixer(), kb if kb is not None else FakeKB(),
                        profile or make_profile(), budget=LoopBudget.from_config(cfg),
                        script_runner=runner or FakeScriptRunner(), **kw)


def subjects(ws) -> list[str]:
    return ws._git("log", "--format=%s").stdout.splitlines()[::-1]


# ------------------------------------------------------------------------------------------ cases across files
def test_one_case_across_files_is_one_script(cfg, ws):
    kb = FakeKB()
    fixer = FakeFixer(scripts=[write_script({FOO: FIXED, BAR: FIXED}, analysis="clientIp moved. Rename it.")])
    loop = make_loop(cfg, ws, broken_build(FOO, BAR), fixer, kb=kb)
    pr = loop.run(Phase.VERIFY, "package", 4)
    assert pr.ok and len(fixer.script_calls) == 1
    cluster, ctx = fixer.script_calls[0]
    assert cluster.id == f"case:{CASE}" and cluster.case_id == CASE and cluster.file is None
    assert sorted(e.file for e in cluster.errors) == [BAR, FOO]
    assert ctx.script_mode == "new" and ctx.case_id == CASE and ctx.signature_generic == L2 and ctx.script_base is None
    [a] = pr.attempts
    assert a.decision.kind == DecisionKind.SCRIPT and a.script_mode == "new" and a.script_case_id == CASE
    assert a.script_version == 1 and a.script_parent is None and a.outcome == "build_ok" and a.applied
    assert sorted(ch.path for ch in a.changes) == [BAR, FOO] and a.script_result.status == "ok"
    assert f"fix(script:{CASE}:v1 new): case:{CASE}" in subjects(ws)
    # stored BEFORE it ran (analysis kept), credited after scoring
    [meta] = kb.scripts.added
    assert meta.version == 1 and meta.analysis == "clientIp moved. Rename it." and meta.project == "demo"
    assert kb.scripts.credits == [(CASE, 1, "build_ok", "demo")]
    ev = [e for e in journal_events(ws) if e["event"] == "attempt"]
    assert ev[0]["script"]["case"] == CASE and ev[0]["script"]["version"] == 1 and ev[0]["decision"] == "script"


def test_different_l2_are_different_cases(cfg, ws):
    def build(ws, call):
        errs = []
        if "broken" in ws.read(FOO):
            errs.append(compile_error(FOO, l2="compile|cannot_find_symbol|a"))
        if "broken" in ws.read(BAR):
            errs.append(compile_error(BAR, l2="compile|cannot_find_symbol|b"))
        return bad_build(*errs) if errs else ok_build()
    fixer = FakeFixer(script_fn=lambda w, c, ctx: write_script({e.file: FIXED for e in c.errors}))
    pr = make_loop(cfg, ws, build, fixer).run(Phase.VERIFY, "package", 3)
    assert pr.ok and len(fixer.script_calls) == 2
    assert {c.case_id for c, _ in fixer.script_calls} == {case_of("compile|cannot_find_symbol|a"),
                                                          case_of("compile|cannot_find_symbol|b")}


def test_targeted_loop_scopes_the_case_id(cfg, ws):
    fixer = FakeFixer(scripts=[write_script({FOO: FIXED})])
    pr = make_loop(cfg, ws, broken_build(FOO), fixer).run(Phase.VERIFY, "package", 3, target="svc-a")
    assert pr.ok
    assert pr.attempts[0].cluster_id == f"case:{CASE}@svc-a" and pr.attempts[0].script_case_id == CASE


# ------------------------------------------------------------------------------------------ the ladder
def test_ladder_new_then_augment_then_blocked_repeated_failure(cfg, ws):
    cfg.script.max_augments_per_case = 2
    kb = FakeKB()
    n = {"i": 0}

    def useless(w, c, ctx):                      # changes a file, never the targeted ones → no_change
        n["i"] += 1
        return write_script({"notes.txt": f"try {n['i']}\n"}, analysis=f"attempt {n['i']}")
    fixer = FakeFixer(script_fn=useless)
    loop = make_loop(cfg, ws, broken_build(FOO, BAR), fixer, kb=kb)
    pr = loop.run(Phase.VERIFY, "package", 8)
    assert not pr.ok
    modes = [ctx.script_mode for _c, ctx in fixer.script_calls]
    assert modes == ["new", "augment", "augment"]
    bases = [ctx.script_base.version for _c, ctx in fixer.script_calls[1:]]
    assert bases == [1, 2]
    base = fixer.script_calls[1][1].script_base
    assert base.outcome == "no_change" and base.status == "ok" and base.changed_files == ["notes.txt"]
    assert len(base.remaining_errors) == 2 and any("v1" in h for h in base.history)
    atts = [a for a in pr.attempts if a.decision.kind == DecisionKind.SCRIPT]
    assert [(a.script_mode, a.script_version, a.script_parent, a.outcome) for a in atts] == [
        ("new", 1, None, "no_change"), ("augment", 2, 1, "no_change"), ("augment", 3, 2, "no_change")]
    # every version measured from the same base: v1 and v2 were rolled back before the next try
    assert "rolled back before v2" in atts[0].notes and "rolled back before v3" in atts[1].notes
    assert not any(n.startswith("rolled back") for n in atts[2].notes)
    assert ws.read("notes.txt") == "try 3\n"
    [blk] = pr.blocked
    assert blk.reason == BlockReason.REPEATED_FAILURE and "max_augments_per_case" in " ".join(blk.notes)
    assert [m.parent for m in kb.scripts.added] == [None, 1, 2]
    assert kb.scripts.credits == [(CASE, 1, "no_change", "demo"), (CASE, 2, "no_change", "demo"),
                                  (CASE, 3, "no_change", "demo")]
    assert loop.budget.llm_calls == 3


def test_reuse_of_a_proven_version_needs_no_llm(cfg, ws):
    kb = FakeKB()
    code = write_script({FOO: FIXED, BAR: FIXED}).code
    kb.scripts.seed(CASE, code, successes=3, projects=("p1", "p2"), analysis="stored: rename clientIp")

    def no_llm(w, c, ctx):
        raise AssertionError("the LLM must not be asked")
    fixer = FakeFixer(script_fn=no_llm)
    loop = make_loop(cfg, ws, broken_build(FOO, BAR), fixer, kb=kb)
    pr = loop.run(Phase.VERIFY, "package", 3)
    assert pr.ok and fixer.script_calls == [] and loop.budget.llm_calls == 0
    [a] = pr.attempts
    assert a.script_mode == "reuse" and a.script_version == 1 and a.analysis == "stored: rename clientIp"
    assert a.llm_usage.calls == 0 and a.model == ""
    assert f"fix(script:{CASE}:v1 reuse): case:{CASE}" in subjects(ws)
    assert kb.scripts.credits == [(CASE, 1, "build_ok", "demo")]
    assert kb.scripts.added == []                     # nothing new stored


def test_reuse_failure_is_rolled_back_and_augmented_with_its_base(cfg, ws):
    kb = FakeKB()
    stored = write_script({"other.txt": "old idea\n"}).code       # proven elsewhere, useless here
    kb.scripts.seed(CASE, stored, successes=1, projects=("p1",))
    seen = {}

    def augment(w, c, ctx):
        seen["other"] = w.exists("other.txt")      # the prompt is packed on the rolled-back tree
        return write_script({FOO: FIXED, BAR: FIXED}, analysis="the stored script missed Bar")
    fixer = FakeFixer(script_fn=augment)
    pr = make_loop(cfg, ws, broken_build(FOO, BAR), fixer, kb=kb).run(Phase.VERIFY, "package", 4)
    assert pr.ok
    [(_c, ctx)] = fixer.script_calls
    assert ctx.script_mode == "augment" and ctx.script_base.version == 1 and ctx.script_base.code == stored
    assert ctx.script_base.outcome == "no_change" and seen["other"] is False
    reuse, aug = pr.attempts
    assert (reuse.script_mode, reuse.outcome) == ("reuse", "no_change") and "rolled back before v2" in reuse.notes
    assert (aug.script_mode, aug.script_version, aug.script_parent, aug.outcome) == ("augment", 2, 1, "build_ok")
    assert not ws.exists("other.txt")
    assert kb.scripts.credits == [(CASE, 1, "no_change", "demo"), (CASE, 2, "build_ok", "demo")]


def test_augment_base_comes_from_the_store_when_nothing_ran_here(cfg, ws):
    kb = FakeKB()
    kb.scripts.seed(CASE, write_script({"x.txt": "1"}).code, successes=0, projects=(), failures=2)   # unproven
    fixer = FakeFixer(scripts=[write_script({FOO: FIXED})])
    pr = make_loop(cfg, ws, broken_build(FOO), fixer, kb=kb).run(Phase.VERIFY, "package", 3)
    assert pr.ok
    ctx = fixer.script_calls[0][1]
    assert ctx.script_mode == "augment" and ctx.script_base.version == 1 and ctx.script_base.outcome == ""
    assert pr.attempts[0].script_parent == 1 and pr.attempts[0].script_version == 2


def test_partial_attempt_is_rolled_back_and_its_errors_come_back(cfg, ws):
    calls = []

    def fn(w, c, ctx):
        calls.append((ctx.script_mode, sorted(e.file for e in c.errors), w.read(FOO)))
        if len(calls) == 1:
            return write_script({FOO: FIXED})              # only Foo → partial
        return write_script({FOO: FIXED, BAR: FIXED})
    fixer = FakeFixer(script_fn=fn)
    pr = make_loop(cfg, ws, broken_build(FOO, BAR), fixer).run(Phase.VERIFY, "package", 4)
    assert pr.ok
    first, second = pr.attempts
    assert first.outcome == "partial" and "rolled back before v2" in first.notes
    # the augment was asked (and ran) on the rolled-back tree: Foo broken again, both errors in the case
    assert calls[1][0] == "augment" and calls[1][1] == [BAR, FOO] and "broken" in calls[1][2]
    assert second.outcome == "build_ok" and sorted(e.file for e in second.cluster_errors) == [BAR, FOO]
    assert any("was rolled back (reset) before this one" in n for n in second.notes)
    ev = [e for e in journal_events(ws) if e["event"] == "script_rollback"]
    assert ev and ev[0]["attempt"] == first.id and ev[0]["restored_errors"] == 1


def test_rolled_back_errors_are_baseline_not_regressions(cfg, ws):
    """Partial v1, then an augment that fixes nothing in Foo / Bar but touches Foo: the error brought back by the
    rollback is part of the baseline, so the augment is no_change — not regressed by 'new errors in a touched file'."""
    cfg.script.max_augments_per_case = 1
    n = {"i": 0}

    def fn(w, c, ctx):
        n["i"] += 1
        if n["i"] == 1:
            return write_script({FOO: FIXED})
        return script(f"p.write({FOO!r}, p.read({FOO!r}) + '// note\\n')")
    pr = make_loop(cfg, ws, broken_build(FOO, BAR), FakeFixer(script_fn=fn)).run(Phase.VERIFY, "package", 4)
    second = pr.attempts[1]
    assert second.script_mode == "augment" and second.outcome == "no_change"


def test_improved_attempt_is_kept_when_the_case_shows_up_again(cfg, ws):
    """javac shows the next files of a case once the first are fixed: the improved v1 stays, the augment runs on
    top of it."""
    ws.write(BAZ, "class Baz { broken }\n")
    ws.commit("add Baz")


    def build(w, call):
        errs = [compile_error(f, l2=L2) for f in (FOO, BAR) if "broken" in w.read(f)]
        if not errs and "broken" in w.read(BAZ):        # Baz is only reported once Foo / Bar compile
            errs = [compile_error(BAZ, l2=L2)]
        return bad_build(*errs) if errs else ok_build()
    fixer = FakeFixer(script_fn=lambda w, c, ctx: write_script({e.file: FIXED for e in c.errors}))
    pr = make_loop(cfg, ws, build, fixer).run(Phase.VERIFY, "package", 4)
    assert pr.ok
    first, second = pr.attempts
    assert first.outcome == "improved" and not any(n.startswith("rolled back") for n in first.notes)
    assert second.script_mode == "augment" and second.script_parent == 1 and ws.read(FOO) == FIXED


# ------------------------------------------------------------------------------------------ failed runs
def test_rejected_error_and_timeout_apply_nothing_and_leave_a_clean_tree(cfg, ws):
    cfg.script.max_augments_per_case = 3
    answers = [script("pass  # reject"),                                         # static check refuses it
               script(f"p.write({FOO!r}, 'half')\nraise KeyError('clientIp')"),    # raises after a write
               script(f"p.write({FOO!r}, 'half')  # timeout"),                     # killed after a write
               write_script({FOO: FIXED})]
    kb = FakeKB()
    fixer = FakeFixer(scripts=answers)
    loop = make_loop(cfg, ws, broken_build(FOO), fixer, kb=kb)
    pr = loop.run(Phase.VERIFY, "package", 6)
    assert pr.ok
    atts = pr.attempts
    assert [(a.script_result.status, a.outcome) for a in atts] == [
        ("rejected", "apply_failed"), ("error", "apply_failed"), ("timeout", "apply_failed"), ("ok", "build_ok")]
    assert "KeyError" in atts[1].apply_errors[0] and atts[0].notes and "static check" in atts[0].notes[0]
    assert not any(a.applied for a in atts[:3]) and not ws.is_dirty() and ws.read(FOO) == FIXED
    # the failed versions keep their lineage and were credited as failures right away
    assert [(a.script_mode, a.script_version, a.script_parent) for a in atts] == [
        ("new", 1, None), ("augment", 2, 1), ("augment", 3, 2), ("augment", 4, 3)]
    assert [c[2] for c in kb.scripts.credits] == ["apply_failed", "apply_failed", "apply_failed", "build_ok"]
    # the augment saw the base's failure
    assert fixer.script_calls[2][1].script_base.status == "error"
    assert "KeyError" in fixer.script_calls[2][1].script_base.error
    # all on one build: nothing applied → the next rung on the same build (re-round), no rebuild in between
    assert [e["event"] for e in journal_events(ws)].count("reround") == 3


def test_ok_script_that_changes_nothing_is_apply_failed(cfg, ws):
    cfg.script.max_augments_per_case = 0
    fixer = FakeFixer(scripts=[script("p.log('looked, nothing to do')")])
    pr = make_loop(cfg, ws, broken_build(FOO), fixer).run(Phase.VERIFY, "package", 3)
    [a] = pr.attempts
    assert a.outcome == "apply_failed" and a.apply_errors == ["no file changed"]
    assert a.script_result.log == ["looked, nothing to do"]
    assert [b.reason for b in pr.blocked] == [BlockReason.REPEATED_FAILURE]


def test_identical_augment_is_not_run_again(cfg, ws):
    cfg.script.max_augments_per_case = 1
    same = write_script({"n.txt": "1\n"})
    fixer = FakeFixer(scripts=[same, same.model_copy()])
    runner = FakeScriptRunner()
    pr = make_loop(cfg, ws, broken_build(FOO), fixer, runner=runner).run(Phase.VERIFY, "package", 4)
    assert len(runner.runs) == 1
    assert "identical to its base v1" in pr.attempts[1].apply_errors[0]


def test_no_knowledge_base_runs_unstored_scripts_as_v0(cfg, ws):
    fixer = FakeFixer(scripts=[write_script({FOO: FIXED})])
    pr = make_loop(cfg, ws, broken_build(FOO), fixer, kb=NullKnowledgeBase()).run(Phase.VERIFY, "package", 3)
    assert pr.ok and pr.attempts[0].script_version == 0
    assert f"fix(script:{CASE}:v0 new): case:{CASE}" in subjects(ws)
    assert "not stored (no knowledge base)" in pr.attempts[0].notes[0]


def test_many_changed_files_get_a_warning_note(cfg, ws):
    cfg.script.max_files_changed_warn = 1
    fixer = FakeFixer(scripts=[write_script({FOO: FIXED, "a.txt": "1", "b.txt": "2"})])
    pr = make_loop(cfg, ws, broken_build(FOO), fixer).run(Phase.VERIFY, "package", 3)
    assert any("changed 3 files (more than script.max_files_changed_warn = 1)" in n for n in pr.attempts[0].notes)


# ------------------------------------------------------------------------------------------ LLM / budget
def test_budget_exhausted_still_reuses_stored_scripts(cfg, ws):
    cfg.loop.max_llm_calls = 0
    kb = FakeKB()
    l2b = "compile|cannot_find_symbol|other"
    kb.scripts.seed(CASE, write_script({FOO: FIXED}).code)

    def build(w, call):
        errs = []
        if "broken" in w.read(FOO):
            errs.append(compile_error(FOO, l2=L2))
        if "broken" in w.read(BAR):
            errs.append(compile_error(BAR, l2=l2b))
        return bad_build(*errs) if errs else ok_build()
    fixer = FakeFixer(script_fn=lambda *a: (_ for _ in ()).throw(AssertionError("no LLM")))
    pr = make_loop(cfg, ws, build, fixer, kb=kb).run(Phase.VERIFY, "package", 3)
    assert [a.script_mode for a in pr.attempts] == ["reuse"] and pr.attempts[0].outcome == "improved"
    assert [(b.reason, b.files) for b in pr.blocked] == [(BlockReason.BUDGET_EXHAUSTED, [BAR])]
    assert ws.read(FOO) == FIXED


def test_llm_unavailable_still_reuses_stored_scripts(cfg, ws):
    kb = FakeKB()
    kb.scripts.seed(CASE, write_script({FOO: FIXED}).code)
    loop = make_loop(cfg, ws, broken_build(FOO), FakeFixer(), kb=kb)
    loop.llm_unavailable = "gateway down"
    loop.triager.set_llm_unavailable("gateway down")
    assert loop.run(Phase.VERIFY, "package", 3).ok


def test_fixer_without_propose_script_blocks_without_infra_flags(cfg, ws):
    class V1Fixer:
        def propose(self, ws, cluster, ctx):
            raise AssertionError("never called")
    loop = make_loop(cfg, ws, broken_build(FOO), LazyFixer(lambda: V1Fixer()))
    pr = loop.run(Phase.VERIFY, "package", 3)
    assert not pr.ok and [b.reason for b in pr.blocked] == [BlockReason.LLM_UNAVAILABLE]
    assert "fixer-without-scripts" in loop.flags and "llm-unavailable" not in loop.flags
    assert any("propose_script" in f for f in loop.followups)


def test_transport_failures_cool_down_then_block(cfg, ws, no_llm_cooldown_sleep):
    cfg.llm.cooldown_seconds = 5
    fixer = FakeFixer(script_fn=lambda *a: __import__("sbmig.core.models", fromlist=["x"]).ScriptProposal(
        analysis="llm-error: LLM request failed (status=None, cause=ConnectionError: refused)", error="transport"))
    loop = make_loop(cfg, ws, broken_build(FOO), fixer)
    pr = loop.run(Phase.VERIFY, "package", 8)
    assert no_llm_cooldown_sleep == [5] and "llm-unavailable" in loop.flags
    assert [b.reason for b in pr.blocked] == [BlockReason.LLM_UNAVAILABLE]


def test_escalation_model_from_the_second_augment(cfg, ws):
    cfg.script.max_augments_per_case = 2
    n = {"i": 0}

    def fn(w, c, ctx):
        n["i"] += 1
        return write_script({"n.txt": str(n["i"])})
    fixer = FakeFixer(script_fn=fn)
    make_loop(cfg, ws, broken_build(FOO), fixer).run(Phase.VERIFY, "package", 6)
    assert [ctx.use_escalation_model for _c, ctx in fixer.script_calls] == [False, False, True]


# ------------------------------------------------------------------------------------------ dry run, first build
def test_dry_run_decides_without_running_or_asking(cfg, ws):
    fixer = FakeFixer(scripts=[write_script({FOO: FIXED})])
    runner = FakeScriptRunner()
    loop = make_loop(cfg, ws, broken_build(FOO), fixer, runner=runner, dry_run=True)
    pr = loop.run(Phase.VERIFY, "package", 3)
    assert pr.attempts == [] and runner.runs == [] and fixer.script_calls == []
    assert any(e["event"] == "decision" and e["kind"] == "script" for e in journal_events(ws))


def test_first_build_is_reused_as_build_one(cfg, ws):
    first = broken_build(FOO)(ws, {})
    fixer = FakeFixer(scripts=[write_script({FOO: FIXED})])
    loop = make_loop(cfg, ws, broken_build(FOO), fixer)
    pr = loop.run(Phase.VERIFY, "package", 3, first_build=first)
    assert pr.ok and [c["label"] for c in loop.build_runner.calls] == ["verify-2"]
    assert any(e["event"] == "build" and e.get("reused") for e in journal_events(ws))


# ------------------------------------------------------------------------------------------ error cases
def test_tracker_records_script_methods_resolvers_and_fields(cfg, ws):
    kb = FakeKB()
    kb.scripts.seed(CASE, write_script({"other.txt": "x"}).code)          # reuse → no_change
    fixer = FakeFixer(scripts=[script(f"p.write({FOO!r}, {FIXED!r})\np.write({BAR!r}, {FIXED!r})\n"
                                      "p.log('Foo, Bar: clientIp')", analysis="rename clientIp")])
    loop = make_loop(cfg, ws, broken_build(FOO, BAR), fixer, kb=kb)
    loop.error_tracker = ErrorTracker(ws, cfg)
    assert loop.run(Phase.VERIFY, "package", 4).ok
    errors, attempts = loop.error_tracker.finalize([], run_id="t-run")
    by = {a.script_mode: a for a in attempts}
    assert by["reuse"].fix_method == "script_reuse" and by["reuse"].resolver_id == f"script:{CASE}:v1"
    aug = by["augment"]
    assert aug.fix_method == "script_augment" and aug.resolver_id == f"script:{CASE}:v2"
    assert (aug.script_case_id, aug.script_version, aug.script_parent, aug.script_status) == (CASE, 2, 1, "ok")
    assert aug.script_log == ["Foo, Bar: clientIp"] and aug.description == "rename clientIp"
    assert by["reuse"].description == "stored fix"                       # the stored version's analysis
    assert {e.status for e in errors} == {"resolved"}
    for e in errors:
        assert (e.script_case_id, e.script_version, e.fix_method) == (CASE, 2, "script_augment")
        assert [r.script_version for r in e.attempts] == [1, 2]
        assert e.llm_attempts == 1                                        # the reuse made no LLM call


def test_a_rolled_back_attempt_is_never_part_of_a_resolution_chain(cfg, ws):
    n = {"i": 0}

    def fn(w, c, ctx):
        n["i"] += 1
        return write_script({FOO: FIXED} if n["i"] == 1 else {FOO: FIXED, BAR: FIXED})
    loop = make_loop(cfg, ws, broken_build(FOO, BAR), FakeFixer(script_fn=fn))
    loop.error_tracker = ErrorTracker(ws, cfg)
    assert loop.run(Phase.VERIFY, "package", 4).ok
    errors, attempts = loop.error_tracker.finalize([], run_id="t-run")
    second = next(a for a in attempts if a.script_version == 2)
    for e in errors:
        assert e.resolution_attempts == [second.id]


def test_the_case_errors_reach_the_runner(cfg, ws):
    runner = FakeScriptRunner()
    fixer = FakeFixer(scripts=[write_script({FOO: FIXED, BAR: FIXED})])
    make_loop(cfg, ws, broken_build(FOO, BAR), fixer, runner=runner).run(Phase.VERIFY, "package", 3)
    errs = runner.runs[0]["errors"]
    assert all(isinstance(e, BuildError) and e.kind == ErrorKind.COMPILE for e in errs)
    assert sorted(e.file for e in errs) == [BAR, FOO]
