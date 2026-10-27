"""v2.1b (2026-10-27): every try's modifications described — `FixTry.edits` (EditNote: path, where, change, lines,
source llm | diff). The LLM's edit `desc` / `<changes>` get the lines from the applied diff, a file the LLM did not
describe (and a reused stored script) gets a note made from the diff; one INFO line `수정: <path> <lines> — <change>`
per note in the case's stage (the diff itself stays DEBUG); the structure review's try the same."""
from __future__ import annotations

import logging

import pytest

from sbmig.core.models import EditHunk, EditNote, Phase, ReviewIssue, StructureReview
from sbmig.core.stage import install_stage_filter
from sbmig.loop import notes as notes_mod
from sbmig.loop.entries import REVIEW_L2, RunErrors
from sbmig.triage.cluster import l2_case_id

from .fakes import FakeErrorList, FakeFixer, FakeKB, FakeReviewer, answer, write_edit, write_script
from .test_v2_orchestrator import make_v2
from .test_v21_loop import BAR, CASE, FIXED, FOO, broken_build, make_loop

FOO_DESC = "clientIp() 호출을 getRemoteAddr() 로 바꿉니다"


@pytest.fixture()
def logs(caplog):
    install_stage_filter(caplog.handler)
    with caplog.at_level(logging.DEBUG, logger="sbmig"):
        yield caplog


def info(caplog) -> list[tuple[str, str]]:
    return [(r.stage, r.getMessage()) for r in caplog.records
            if r.levelno == logging.INFO and r.name.startswith("sbmig")]


def edits_answer():
    """Two edits: Foo described by the LLM (`desc`), Bar not."""
    return answer(edits=[write_edit(FOO, FIXED, desc=FOO_DESC), write_edit(BAR, FIXED)],
                  changes=[{"path": FOO, "where": "class 본문", "change": FOO_DESC}])


def run_edits(cfg, ws):
    el = FakeErrorList()
    pr = make_loop(cfg, ws, broken_build(FOO, BAR), FakeFixer(scripts=[edits_answer()]), errors=el).run(
        Phase.VERIFY, "package", 3)
    return pr, el.tries_of(CASE)


def test_llm_notes_get_the_diff_lines_and_undescribed_files_a_diff_note(cfg, ws, logs):
    pr, [t] = run_edits(cfg, ws)
    assert pr.ok and t.applied == "edits" and t.result == "build_ok"
    foo = next(n for n in t.edits if n.path == FOO)
    bar = next(n for n in t.edits if n.path == BAR)
    assert (foo.source, foo.where, foo.change, foo.lines) == ("llm", "class 본문", FOO_DESC, "~1")
    assert bar.source == "diff" and bar.lines == "~1" and bar.change          # never a bare diff
    assert len(t.edits) == 2                                                    # Foo once (<changes> = its desc)
    stage = f"fix r1 {CASE}"
    out = info(logs)
    assert (stage, f"수정: {FOO} ~1 (class 본문) — {FOO_DESC}") in out
    assert any(st == stage and m.startswith(f"수정: {BAR} ~1 — ") for st, m in out)
    applied = next(i for i, (st, m) in enumerate(out) if m.startswith("적용(edits): "))
    first_note = next(i for i, (st, m) in enumerate(out) if m.startswith("수정: "))
    assert applied < first_note                                                 # 적용 → 수정 lines
    assert not any("+class X { ok }" in m for _, m in out)                      # the diff stays at DEBUG
    assert any(r.levelno == logging.DEBUG and r.getMessage().startswith("diff:\n") for r in logs.records)


def test_a_reused_stored_script_gets_its_notes_from_the_diff(cfg, ws, logs):
    kb, el = FakeKB(), FakeErrorList()
    kb.cases.seed(CASE, write_script({FOO: FIXED}).code)
    pr = make_loop(cfg, ws, broken_build(FOO), FakeFixer(scripts=[]), kb=kb, errors=el).run(Phase.VERIFY, "package", 3)
    [t] = el.tries_of(CASE)
    assert pr.ok and (t.source, t.applied) == ("kb-script", "script")
    [n] = t.edits
    assert (n.path, n.lines, n.source) == (FOO, "~1", "diff") and n.change
    assert any(st == f"fix r1 {CASE}" and m == f"수정: {FOO} ~1 — {n.change}" for st, m in info(logs))


def test_a_script_run_takes_the_changes_list_not_the_unapplied_edits(cfg, ws):
    """The edits fail → the answer's script runs: its `<changes>` notes are kept, the edits' own `desc` (merged into
    `changes` by the fixer's parser) are not — those edits never applied."""
    el = FakeErrorList()
    bad_edit = {"op": "replace", "path": BAR, "old": "no such text", "new": "x", "desc": "Bar 를 고칩니다"}
    ans = answer(code=write_script({FOO: FIXED}).code, edits=[bad_edit],
                 changes=[{"path": FOO, "where": "본문", "change": "스크립트가 Foo 를 다시 씁니다"},
                          {"path": BAR, "change": "Bar 를 고칩니다"}])
    make_loop(cfg, ws, broken_build(FOO), FakeFixer(scripts=[ans]), errors=el).run(Phase.VERIFY, "package", 3)
    [t] = el.tries_of(CASE)
    assert t.applied == "script"
    assert [(n.path, n.change, n.lines, n.source) for n in t.edits] == [(FOO, "스크립트가 Foo 를 다시 씁니다", "~1", "llm")]


def test_a_try_that_applied_nothing_has_no_notes(cfg, ws):
    cfg.script.max_augments_per_case = 1
    el = FakeErrorList()
    ans = answer(edits=[{"op": "replace", "path": FOO, "old": "no such text", "new": "x", "desc": "never applied"}])
    make_loop(cfg, ws, broken_build(FOO), FakeFixer(scripts=[ans]), errors=el).run(Phase.VERIFY, "package", 3)
    [t] = el.tries_of(CASE)
    assert t.applied == "none" and t.edits == []
    assert all(k.get("edits") == [] for a, k in el.of("finish_try"))


def test_the_real_error_list_shows_the_notes_in_errors_md(cfg, ws):
    from sbmig.errors.entries import ErrorList

    el = ErrorList.for_workspace(ws, cfg, project="demo")
    loop = make_loop(cfg, ws, broken_build(FOO, BAR), FakeFixer(scripts=[edits_answer()]))
    loop.errors = RunErrors(el)
    pr = loop.run(Phase.VERIFY, "package", 3)
    assert pr.ok
    [t] = {id(t): t for e in el.entries for t in e.tries}.values()
    assert [n.path for n in t.edits] == [FOO, BAR]
    md = el.md_path.read_text(encoding="utf-8")
    assert f"- 수정:\n  - {FOO} ~1 (class 본문) — {FOO_DESC}\n  - {BAR} ~1 — " in md
    assert md.index("- 수정:") < md.index("```diff")
    assert f"build_ok ({FOO_DESC}) |" in md                                     # the tries cell


# ------------------------------------------------------------------------------------------ fallback
def test_fallback_without_the_script_helpers(cfg, ws, monkeypatch):
    monkeypatch.setattr(notes_mod, "_helper", lambda name: None)
    pr, [t] = run_edits(cfg, ws)
    assert pr.ok
    assert [(n.path, n.lines, n.source) for n in t.edits] == [(FOO, "~1", "llm"), (BAR, "~1", "diff")]
    assert t.edits[0].change == FOO_DESC and t.edits[1].change == "1줄 추가, 1줄 삭제"


def test_fallback_diff_notes():
    diff = ("diff --git a/pom.xml b/pom.xml\n--- a/pom.xml\n+++ b/pom.xml\n@@ -40,3 +40,9 @@\n a\n b\n"
            + "".join(f"+d{i}\n" for i in range(6)) + " c\n@@ -80,3 +86,2 @@\n x\n-y\n z\n"
            "diff --git a/N.java b/N.java\nnew file mode 100644\n--- /dev/null\n+++ b/N.java\n@@ -0,0 +1,2 @@\n+a\n+b\n"
            "diff --git a/Old.java b/Old.java\ndeleted file mode 100644\n--- a/Old.java\n+++ /dev/null\n"
            "@@ -1 +0,0 @@\n-x\n")
    got = notes_mod._from_diff(diff)
    assert [(n.path, n.lines, n.change) for n in got] == [("pom.xml", "+42–47, -81", "6줄 추가, 1줄 삭제"),
                                                           ("N.java", "+1–2", "새 파일 (2줄)"),
                                                           ("Old.java", "-1", "파일 삭제")]
    merged = notes_mod._merge([EditNote(path="./pom.xml", change="의존성 추가"), EditNote(path="X.java", change="x")],
                              diff)
    assert [(n.path, n.lines, n.source) for n in merged] == [("pom.xml", "+42–47, -81", "llm"), ("X.java", "", "llm"),
                                                             ("N.java", "+1–2", "diff"), ("Old.java", "-1", "diff")]
    assert notes_mod._from_diff("") == [] and notes_mod._merge([], "garbage\n@@ nonsense") == []


def test_answer_notes():
    a = answer(edits=[write_edit("pom.xml", "x", desc="의존성 추가"), write_edit("B.java", "y")],
               changes=[{"path": "A.java", "where": "import 줄", "change": "javax → jakarta"}])
    assert [(n.path, n.change) for n in notes_mod.answer_notes(a, edits=True)] == [("A.java", "javax → jakarta"),
                                                                                    ("pom.xml", "의존성 추가")]
    assert [n.path for n in notes_mod.answer_notes(a, edits=False)] == ["A.java"]
    assert notes_mod.answer_notes(None, edits=True) == []


# ------------------------------------------------------------------------------------------ review
def test_the_review_try_carries_the_notes_and_logs_them(cfg, ws, logs):
    rv = StructureReview(verdict="issues", issues=[
        ReviewIssue(severity="critical", file="pom.xml", problem="sourceEncoding MS949", fix_hint="UTF-8")])
    desc = "project.build.sourceEncoding 을 UTF-8 로 바꿉니다"
    reviewer = FakeReviewer(review=rv, files={"pom.xml": "<project>utf8</project>\n"}, descs={"pom.xml": desc})
    el = FakeErrorList()
    report = make_v2(cfg, ws, reviewer=reviewer, error_list=lambda w, c, p: el).run()
    assert report.structure_review.commit
    [t] = el.tries_of(l2_case_id(REVIEW_L2))
    [n] = t.edits
    assert (n.path, n.change, n.source) == ("pom.xml", desc, "llm") and n.lines.startswith("~")
    assert ("review", f"수정: pom.xml {n.lines} — {desc}") in info(logs)
    # v2.1e: the review's notes carry the as-is / to-be code too (the same try_notes path as the fix loop)
    assert n.hunks == [EditHunk(lines="~1", before="<project><artifactId>demo</artifactId></project>",
                                after="<project>utf8</project>")]
    assert not any("as-is" in m for _, m in info(logs))                       # the log keeps the one line


# ------------------------------------------------------------------------------------------ v2.1e hunks
def test_the_fix_try_notes_carry_the_hunks(cfg, ws):
    pr, [t] = run_edits(cfg, ws)
    assert pr.ok
    by = {n.path: n for n in t.edits}
    for path, name in ((FOO, "Foo"), (BAR, "Bar")):                            # an LLM note and a diff note
        assert by[path].hunks == [EditHunk(lines="~1", before=f"class {name} {{ broken }}", after=FIXED.rstrip("\n"))]


def test_the_review_notes_follow_errors_snippet_lines(cfg, ws):
    cfg = cfg.model_copy(deep=True)
    cfg.errors.snippet_lines = 2
    rv = StructureReview(verdict="issues", issues=[ReviewIssue(severity="critical", file="pom.xml", problem="p")])
    pom = "<project>\n" + "".join(f"  <x{i}/>\n" for i in range(6)) + "</project>\n"
    el = FakeErrorList()
    make_v2(cfg, ws, reviewer=FakeReviewer(review=rv, files={"pom.xml": pom}), error_list=lambda w, c, p: el).run()
    [t] = el.tries_of(l2_case_id(REVIEW_L2))
    [h] = t.edits[0].hunks
    assert h.truncated and h.after == "<project>\n  <x0/>\n… (+6 lines)"
    assert h.before == "<project><artifactId>demo</artifactId></project>"


def test_try_notes_threads_max_lines_and_survives_an_old_helper(monkeypatch):
    diff = "--- a/A.java\n+++ b/A.java\n@@ -1,3 +1,3 @@\n-a\n-b\n-c\n+A\n+B\n+C\n"
    [n] = notes_mod.try_notes([], diff, max_lines=1)
    assert n.hunks == [EditHunk(lines="~1–3", before="a\n… (+2 lines)", after="A\n… (+2 lines)", truncated=True)]
    [n] = notes_mod.try_notes([EditNote(path="A.java", change="x")], diff)
    assert n.source == "llm" and n.hunks[0].after == "A\nB\nC"
    # a helper without the keyword (a plugin's older copy): called without it, never the fallback
    seen = []

    def old(llm, d):
        seen.append(d)
        return [EditNote(path="A.java", change="old helper")]
    monkeypatch.setattr(notes_mod, "_helper", lambda name: old)
    assert [n.change for n in notes_mod.try_notes([EditNote(path="A.java", change="x")], diff, max_lines=3)] == \
        ["old helper"] and seen == [diff]
