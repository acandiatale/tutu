"""v2.1e (docs/INTERFACES.md "v2.1e — KB learning records"): the fix loop tells the KB why a stored script failed
(`kb.cases.credit(…, reason=, round=, n=, errors_before=, errors_left=, errors_new=, files=)`), an augment's new
version records its lineage (`add_script(…, parent=, reason=)`), the journal's `script_credit` carries the reason, and
the try's 수정 notes are asked for with `errors.snippet_lines` (`try_notes(…, max_lines=)`, old signature still
works)."""
from __future__ import annotations

from sbmig.core.models import Phase
from sbmig.loop import BuildFixLoop, LoopBudget
from sbmig.loop.entries import RunErrors
from sbmig.triage import Triager

from .fakes import (
    FakeBuildRunner,
    FakeEditApplier,
    FakeErrorList,
    FakeFixer,
    FakeKB,
    FakeScriptRunner,
    answer,
    bad_build,
    case_of,
    compile_error,
    journal_events,
    make_profile,
    ok_build,
    write_edit,
    write_script,
)

FOO = "src/main/java/com/x/Foo.java"
L2 = "compile|cannot_find_symbol|clientIp"
CASE = case_of(L2)
FIXED = "class X { ok }\n"


def broken_build(*files: str):
    def fn(ws, call):
        errs = [compile_error(f, l2=L2) for f in files if ws.exists(f) and "broken" in ws.read(f)]
        return bad_build(*errs) if errs else ok_build()
    return fn


def make_loop(cfg, ws, fixer, *, kb, errors=None) -> BuildFixLoop:
    return BuildFixLoop(cfg, ws, FakeBuildRunner(ws, fn=broken_build(FOO)), Triager(cfg), fixer, kb, make_profile(),
                        budget=LoopBudget.from_config(cfg), script_runner=FakeScriptRunner(),
                        edit_applier=FakeEditApplier(), errors=RunErrors(errors) if errors is not None else None)


def test_a_reused_script_that_changed_nothing_is_credited_with_why_and_the_augment_records_its_parent(cfg, ws):
    kb, el = FakeKB(), FakeErrorList()
    kb.cases.seed(CASE, write_script({"notes.txt": "old idea\n"}).code, successes=3, projects=("p1", "p2"))
    fixer = FakeFixer(scripts=[answer(code=write_script({FOO: FIXED}).code, solution="rename the call")])
    pr = make_loop(cfg, ws, fixer, kb=kb, errors=el).run(Phase.VERIFY, "package", 4)
    assert pr.ok
    reuse, aug = pr.attempts
    assert (reuse.source, reuse.outcome, aug.source, aug.script_version, aug.outcome) == \
        ("kb-script", "no_change", "llm-augment", 2, "build_ok")
    first, second = kb.cases.credit_runs
    # the stored v1: no_change — the reason in words (not the reuse banner), the try (round 1, try 1), counts, files
    assert (first["case"], first["version"], first["result"], first["round"], first["n"]) == \
        (CASE, 1, "no_change", 1, 1)
    assert first["reason"].startswith(f"no_change: 1 of 1 targeted error(s) remained — {FOO}")
    assert "reused stored v1" not in first["reason"]
    assert (first["errors_before"], first["errors_left"], first["errors_new"], first["files"]) == \
        (1, 1, 0, ["notes.txt"])
    assert first["run_id"] == ws.run_id and first["project"] == "demo"
    # the augment's v2: stored with its parent and why it was made; its success carries no reason
    [(cid, sc, kw)] = kb.cases.added
    assert (cid, sc.version, kw["source"], kw["parent"]) == (CASE, 2, "llm-augment", 1)
    assert kw["reason"] == f"v1: {first['reason']}" and sc.parent == 1 and sc.reason == kw["reason"]
    assert (second["version"], second["result"], second["reason"], second["round"], second["n"]) == \
        (2, "build_ok", "", 2, 2)
    assert second["files"] == [FOO] and second["errors_left"] == 0
    # the journal's script_credit lines carry the reason
    credits = [e for e in journal_events(ws) if e["event"] == "script_credit"]
    assert [(e["version"], e["outcome"]) for e in credits] == [(1, "no_change"), (2, "build_ok")]
    assert credits[0]["reason"] == first["reason"] and credits[1]["reason"] == ""
    # the error list's tries agree with the run records (what `sbmig kb reindex` rebuilds them from)
    t1, t2 = el.tries_of(CASE)
    assert (t1.round, t1.n, t1.script_version, t2.round, t2.n, t2.script_version) == (1, 1, 1, 2, 2, 2)


def test_a_failing_script_is_credited_with_its_error_and_the_next_augment_says_so(cfg, ws):
    kb = FakeKB()
    kb.cases.seed(CASE, "def fix(p):\n    raise KeyError('clientIp')\n")
    fixer = FakeFixer(scripts=[answer(code=write_script({FOO: FIXED}).code)])
    pr = make_loop(cfg, ws, fixer, kb=kb).run(Phase.VERIFY, "package", 4)
    assert pr.ok and pr.attempts[0].outcome == "apply_failed"
    first = kb.cases.credit_runs[0]
    assert first["result"] == "apply_failed" and "script error" in first["reason"] and "KeyError" in first["reason"]
    assert first["files"] == [] and first["errors_left"] is None
    [(_cid, _sc, kw)] = kb.cases.added
    assert kw["parent"] == 1 and kw["reason"].startswith("v1: ") and "KeyError" in kw["reason"]


def test_an_augment_from_a_stored_version_takes_the_stores_last_failure(cfg, ws):
    """script.reuse off: the augment extends the stored version, which did not run here — the reason is what the
    store says of it."""
    cfg.script.reuse = False
    kb = FakeKB()
    sc = kb.cases.seed(CASE, write_script({"notes.txt": "x"}).code, status="unverified", successes=0, failures=1)
    sc.last_failure = "regressed: 2 new error(s) in touched files"
    fixer = FakeFixer(scripts=[answer(code=write_script({FOO: FIXED}).code)])
    assert make_loop(cfg, ws, fixer, kb=kb).run(Phase.VERIFY, "package", 3).ok
    [(_cid, _sc, kw)] = kb.cases.added
    assert (kw["parent"], kw["reason"]) == (1, "v1: regressed: 2 new error(s) in touched files")


def test_a_new_script_has_no_parent_and_no_reason(cfg, ws):
    kb = FakeKB()
    fixer = FakeFixer(scripts=[answer(code=write_script({FOO: FIXED}).code)])
    assert make_loop(cfg, ws, fixer, kb=kb).run(Phase.VERIFY, "package", 3).ok
    [(_cid, sc, kw)] = kb.cases.added
    assert (kw["parent"], kw["reason"], sc.parent) == (None, "", None)
    [run] = kb.cases.credit_runs
    assert (run["result"], run["reason"], run["round"], run["n"]) == ("build_ok", "", 1, 1)


def test_an_older_store_without_the_new_keywords_still_gets_its_credit(cfg, ws):
    """A `kb.cases` from before v2.1e: the adapter drops the keywords it does not take; counts as before."""
    kb = FakeKB()
    calls = []

    class OldStore(type(kb.cases)):
        def add_script(self, case_id, l2, code, *, source="llm-new", run_id="", project="", analysis="",
                       solution=""):
            return super().add_script(case_id, l2, code, source=source, run_id=run_id, project=project,
                                      analysis=analysis, solution=solution)

        def credit(self, case_id, version, result, project="", run_id=""):
            calls.append((case_id, version, result))
            return super().credit(case_id, version, result, project=project, run_id=run_id)
    kb.cases = OldStore()
    fixer = FakeFixer(scripts=[answer(code=write_script({FOO: FIXED}).code)])
    assert make_loop(cfg, ws, fixer, kb=kb).run(Phase.VERIFY, "package", 3).ok
    assert calls == [(CASE, 1, "build_ok")] and kb.cases.added[0][1].version == 1


def test_try_notes_get_errors_snippet_lines_and_the_old_signature_still_works(cfg, ws, monkeypatch):
    from sbmig.loop import fixloop

    seen = []
    real = fixloop.try_notes

    def new_style(llm_notes, diff, *, max_lines=0):
        seen.append(max_lines)
        return real(llm_notes, diff)
    cfg.errors.snippet_lines = 7
    monkeypatch.setattr(fixloop, "try_notes", new_style)
    fixer = FakeFixer(scripts=[answer(edits=[write_edit(FOO, FIXED, desc="rename the call")])])
    assert make_loop(cfg, ws, fixer, kb=FakeKB()).run(Phase.VERIFY, "package", 3).ok
    assert seen == [7]

    old_seen = []

    def old_style(llm_notes, diff):
        old_seen.append(diff)
        return real(llm_notes, diff)
    monkeypatch.setattr(fixloop, "try_notes", old_style)
    el = FakeErrorList()
    w2 = ws                                                      # the tree is fixed now: break it again
    w2.write(FOO, "broken again\n")
    w2.commit("break again")
    fixer = FakeFixer(scripts=[answer(edits=[write_edit(FOO, FIXED, desc="rename the call")])])
    assert make_loop(cfg, w2, fixer, kb=FakeKB(), errors=el).run(Phase.VERIFY, "package", 3).ok
    assert len(old_seen) == 1 and "+class X { ok }" in old_seen[0]
    [t] = el.tries_of(CASE)
    assert t.edits and t.edits[0].path == FOO
