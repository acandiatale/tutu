"""`sbmig.report.changes.collect_code_changes`: the net change per file between the commit right after the
automatic conversion and the final tree (`RunReport.code_changes`), on a real temporary git workspace."""
from __future__ import annotations

import logging
import os

import pytest

from sbmig.core.models import CaseType, EditNote, ErrorEntry, FixTry, RunReport, utcnow
from sbmig.core.workspace import GitError, Workspace
from sbmig.report import changes as ch
from sbmig.report.changes import collect_code_changes, first_sentences

FOO = "src/main/java/com/x/Foo.java"
BAR = "src/main/java/com/x/Bar.java"


@pytest.fixture()
def ws(tmp_path) -> Workspace:
    src = tmp_path / "proj"
    (src / "src/main/java/com/x").mkdir(parents=True)
    (src / "pom.xml").write_text("<project>\n  <artifactId>demo</artifactId>\n</project>\n", encoding="utf-8")
    (src / FOO).write_text("class Foo {\n  int a = 1;\n  int b = 2;\n}\n", encoding="utf-8")
    (src / BAR).write_text("class Bar {\n}\n", encoding="utf-8")
    w = Workspace.from_input(src, tmp_path / "out", run_id="t-cc")
    w.git_init()
    return w


def report() -> RunReport:
    return RunReport(run_id="t-cc", project_name="demo", case_type=CaseType.SPRING_BOOT, started_at=utcnow())


def a_try(n: int, files: list[str], *, result="improved", analysis="", edits=(), commit="") -> FixTry:
    return FixTry(n=n, round=n, source="llm-new", applied="script", files=files, result=result, analysis=analysis,
                  edits=list(edits), commit=commit)


def entry(case: str, *tries: FixTry, eid: str = "") -> ErrorEntry:
    return ErrorEntry(id=eid or f"e-{case}", case_id=case, tries=list(tries))


def test_a_file_changed_by_two_tries_is_one_entry_with_the_net_diff(ws):
    base = ws.head()
    ws.write(FOO, "class Foo {\n  int a = 10;\n  int b = 2;\n}\n")
    ws.commit("fix r1")
    ws.write(FOO, "class Foo {\n  int a = 10;\n  int b = 20;\n}\n")
    ws.commit("fix r2")
    r = report()
    collect_code_changes(ws, r, base)
    assert [c.path for c in r.code_changes] == [FOO]
    [c] = r.code_changes
    assert (c.status, c.added, c.removed, c.truncated) == ("modified", 2, 2, False)
    assert c.diff.startswith("@@") and "diff --git" not in c.diff and "+++ " not in c.diff and "index " not in c.diff
    assert "-  int a = 1;" in c.diff and "+  int b = 20;" in c.diff and "+  int b = 2;" not in c.diff
    assert r.code_changes_total == 1 and r.code_changes_base == base


def test_a_change_that_was_rolled_back_does_not_appear(ws):
    base = ws.head()
    ws.write(BAR, "class Bar { broken }\n")
    ws.commit("fix r1 try 1")
    ws.revert_to(base)
    ws.write(FOO, "class Foo {\n}\n")
    ws.commit("fix r1 try 2")
    r = report()
    collect_code_changes(ws, r, base)
    assert [c.path for c in r.code_changes] == [FOO]


def test_order_java_then_poms_then_the_rest_and_statuses(ws):
    base = ws.head()
    ws.write("module/pom.xml", "<project/>\n")
    ws.write("src/main/resources/app.properties", "a=1\n")
    ws.write("pom.xml", "<project>\n</project>\n")
    ws.write("src/main/java/com/x/Aaa.java", "class Aaa {}\n")
    ws.delete(BAR)
    ws.write(FOO, "class Foo {}\n")
    ws.commit("fixes")
    r = report()
    collect_code_changes(ws, r, base)
    assert [(c.path, c.status) for c in r.code_changes] == [
        ("src/main/java/com/x/Aaa.java", "added"), (BAR, "deleted"), (FOO, "modified"),
        ("module/pom.xml", "added"), ("pom.xml", "modified"), ("src/main/resources/app.properties", "added")]


def test_a_moved_file_is_the_old_path_deleted_and_the_new_path_added(ws):
    base = ws.head()
    ws.move(BAR, "src/main/java/com/y/Bar.java")
    ws.commit("move")
    r = report()
    collect_code_changes(ws, r, base)
    assert [(c.path, c.status) for c in r.code_changes] == [(BAR, "deleted"), ("src/main/java/com/y/Bar.java", "added")]


def test_caps_diff_lines_diff_files_and_listed_files(ws, monkeypatch):
    monkeypatch.setattr(ch, "DIFF_FILES", 2)
    monkeypatch.setattr(ch, "MAX_FILES", 3)
    monkeypatch.setattr(ch, "DIFF_LINES", 5)
    base = ws.head()
    for i in range(4):
        ws.write(f"src/main/java/com/x/N{i}.java", "".join(f"line {k}\n" for k in range(10)))
    ws.commit("many")
    r = report()
    collect_code_changes(ws, r, base)
    assert r.code_changes_total == 4 and len(r.code_changes) == 3
    first, second, third = r.code_changes
    assert first.truncated and len(first.diff.splitlines()) == 5 and first.added == 10
    assert second.diff and second.truncated
    assert third.diff == "" and not third.truncated and third.added == 10


def test_the_default_caps():
    assert (ch.DIFF_FILES, ch.DIFF_LINES, ch.MAX_FILES) == (30, 80, 500)


def test_notes_and_reasons_come_from_kept_tries_only_without_diff_made_notes(ws):
    base = ws.head()
    ws.write(FOO, "class Foo {\n}\n")
    ws.write("pom.xml", "<project>\n</project>\n")
    ws.commit("fix")
    kept1 = a_try(1, [FOO], analysis="The import moved to jakarta. Rewrite it everywhere. More text.",
                  edits=[EditNote(path=FOO, where="import 줄", change="javax 를 jakarta 로 바꿉니다"),
                         EditNote(path=FOO, change="필드 a 와 b 를 지웁니다"),
                         EditNote(path=FOO, change="diff made", source="diff")])
    rolled = a_try(2, [FOO], result="reverted", analysis="wrong idea",
                   edits=[EditNote(path=FOO, change="rolled back note")])
    pom_try = a_try(1, ["pom.xml"], analysis="A dependency was missing.",
                    edits=[EditNote(path="pom.xml", where="<dependencies>", change="<dependencies> 에 추가합니다")])
    r = report()
    r.error_entries = [entry("c1", kept1, rolled, eid="e1"), entry("c1", kept1, rolled, eid="e2"),
                       entry("c2", pom_try)]
    collect_code_changes(ws, r, base)
    foo, pom = r.code_changes
    assert foo.notes == ["import 줄: javax 를 jakarta 로 바꿉니다", "필드 a 와 b 를 지웁니다"]
    assert foo.reasons == ["The import moved to jakarta. Rewrite it everywhere. More text."]
    assert pom.notes == ["<dependencies> 에 추가합니다"] and pom.reasons == ["A dependency was missing."]


def test_reasons_are_cut_at_a_sentence_boundary():
    text = "First sentence here. " + "Second " * 80 + "end."
    assert first_sentences(text, 300) == "First sentence here."
    assert first_sentences("short.", 300) == "short."
    assert len(first_sentences("x" * 500, 300)) <= 301


def test_stored_texts_are_masked_like_errors_md(ws):
    base = ws.head()
    ws.write(FOO, f"class Foo {{ String p = \"{ws.root}/secret\"; }}\n")
    ws.commit("fix")
    r = report()
    r.error_entries = [entry("c1", a_try(1, [FOO], analysis=f"Path {ws.root}/x was wrong."))]
    collect_code_changes(ws, r, base)
    [c] = r.code_changes
    assert str(ws.root) not in c.diff and str(ws.root) not in " ".join(c.reasons)


def test_build_output_and_binary_files_are_left_out(ws):
    base = ws.head()
    ws.abs("lib").mkdir()
    ws.abs("lib/blob.bin").write_bytes(b"\x00\x01\x02binary\x00")
    ws.write(FOO, "class Foo {}\n")
    ws.commit("fix")
    ws.abs("target").mkdir()
    ws.abs("target/Foo.class").write_bytes(b"\xca\xfe\xba\xbe")
    ws.abs("target/classes").mkdir()
    ws.abs("target/classes/app.properties").write_text("copied=1\n", encoding="utf-8")   # text: the path rule drops it
    ws._git("add", "-f", "target/Foo.class", "target/classes/app.properties")
    ws._git("commit", "-q", "-m", "build output tracked")
    r = report()
    collect_code_changes(ws, r, base)
    assert [c.path for c in r.code_changes] == [FOO] and r.code_changes_total == 1


def test_no_base_leaves_the_list_empty(ws):
    r = report()
    collect_code_changes(ws, r, "")
    assert r.code_changes == [] and r.code_changes_total == 0 and r.code_changes_base == ""


def test_a_failing_git_call_leaves_an_empty_list_and_one_warning(ws, monkeypatch, caplog):
    base = ws.head()
    ws.write(FOO, "class Foo {}\n")
    ws.commit("fix")

    def boom(*a, **k):
        raise GitError("git diff failed: simulated")
    monkeypatch.setattr(ws, "numstat_since", boom)
    r = report()
    with caplog.at_level(logging.WARNING, logger="sbmig.report.changes"):
        collect_code_changes(ws, r, base)
    assert r.code_changes == [] and r.code_changes_total == 0 and r.code_changes_base == ""
    assert len([x for x in caplog.records if x.levelno == logging.WARNING]) == 1


def test_an_unknown_base_commit_does_not_raise(ws):
    r = report()
    collect_code_changes(ws, r, "0" * 40)
    assert r.code_changes == [] and r.code_changes_base == ""


@pytest.mark.skipif(os.name != "posix", reason="file modes are a POSIX matter")
def test_a_file_whose_only_change_is_its_mode_is_not_listed(ws):
    # an executable bit is not a code modification: no line changed, nothing to show
    base = ws.head()
    os.chmod(ws.abs(BAR), 0o755)
    ws.write(FOO, "class Foo {\n  int a = 10;\n  int b = 2;\n}\n")
    ws.commit("fix r1")
    r = report()
    collect_code_changes(ws, r, base)
    assert [c.path for c in r.code_changes] == [FOO] and r.code_changes_total == 1
