"""v2.1b edit notes (sbmig.script.notes): notes generated from a unified diff (`lines` "+42–47" / "~12" / "-3",
Korean `change` sentences), the LLM's notes merged with the diff (lines attached by path, short paths by basename,
unmentioned files appended, never empty when the diff touched a file), `answer_notes`, and the edits' `desc`
(parsing, the synthesized script's comments, `summarize_edits`)."""
from __future__ import annotations

from sbmig.core.models import Edit, EditNote
from sbmig.script import check_script, edits_script
from sbmig.script.edits import edit_from_item, edits_from_json, summarize_edits
from sbmig.script.notes import (
    answer_notes,
    describe_language,
    merge_notes,
    note_from_text,
    notes_from_diff,
    notes_from_json,
    same_path,
)

POM_ADD = "".join(f"+    <line{i}/>\n" for i in range(6))
DIFF3 = (
    "diff --git a/module/comp-jar/pom.xml b/module/comp-jar/pom.xml\n"
    "index 1111111..2222222 100644\n"
    "--- a/module/comp-jar/pom.xml\n"
    "+++ b/module/comp-jar/pom.xml\n"
    "@@ -40,3 +40,9 @@\n"
    "   <dependencies>\n"
    "     <dependency/>\n" + POM_ADD +
    "   </dependencies>\n"
    "diff --git a/src/main/java/a/Foo.java b/src/main/java/a/Foo.java\n"
    "index 3333333..4444444 100644\n"
    "--- a/src/main/java/a/Foo.java\n"
    "+++ b/src/main/java/a/Foo.java\n"
    "@@ -10,5 +10,5 @@ class Foo {\n"
    " package a;\n"
    " \n"
    "-import javax.annotation.PostConstruct;\n"
    "+import jakarta.annotation.PostConstruct;\n"
    " \n"
    " class Foo {}\n"
    "diff --git a/src/main/java/a/Bar.java b/src/main/java/a/Bar.java\n"
    "index 5555555..6666666 100644\n"
    "--- a/src/main/java/a/Bar.java\n"
    "+++ b/src/main/java/a/Bar.java\n"
    "@@ -1,4 +1,3 @@\n"
    " package a;\n"
    " \n"
    "-import javax.xml.bind.JAXB;\n"
    " class Bar {}\n")


# ---------------------------------------------------------------------------
# notes_from_diff
# ---------------------------------------------------------------------------
def test_three_file_diff_added_changed_removed():
    notes = notes_from_diff(DIFF3)
    assert [(n.path, n.lines, n.change, n.source) for n in notes] == [
        ("module/comp-jar/pom.xml", "+42–47", "pom.xml 42–47행 추가 (6줄)", "diff"),
        ("src/main/java/a/Foo.java", "~12", "Foo.java 12행 수정", "diff"),
        ("src/main/java/a/Bar.java", "-3", "Bar.java 3행 삭제", "diff")]
    assert all(n.where == "" for n in notes)


def test_several_runs_in_one_hunk_and_several_hunks():
    diff = ("--- a/A.java\n+++ b/A.java\n"
            "@@ -1,7 +1,7 @@\n"
            " a\n-b\n-c\n+B\n+C\n+D\n d\n e\n-f\n g\n"
            "@@ -20,2 +21,3 @@\n x\n+y\n z\n"
            "\\ No newline at end of file\n")
    [n] = notes_from_diff(diff)
    assert n.path == "A.java" and n.lines == "~2–4, -6, +22"
    assert n.change == "A.java 2–4행 수정 (3줄), 6행 삭제, 22행 추가"


def test_new_deleted_renamed_binary_and_mode_only_files():
    diff = ("diff --git a/n/New.java b/n/New.java\nnew file mode 100644\nindex 0000000..1111111\n"
            "--- /dev/null\n+++ b/n/New.java\n@@ -0,0 +1,3 @@\n+a\n+b\n+c\n"
            "diff --git a/old/Gone.java b/old/Gone.java\ndeleted file mode 100644\n"
            "--- a/old/Gone.java\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-x\n-y\n"
            "diff --git a/a/Old.java b/b/Neu.java\nsimilarity index 90%\nrename from a/Old.java\nrename to b/Neu.java\n"
            "--- a/a/Old.java\n+++ b/b/Neu.java\n@@ -1,1 +1,1 @@\n-class Old {}\n+class Neu {}\n"
            "diff --git a/img/logo.png b/img/logo.png\nindex 1..2 100644\nBinary files a/img/logo.png and b/img/logo.png "
            "differ\n"
            "diff --git a/run.sh b/run.sh\nold mode 100644\nnew mode 100755\n")
    notes = notes_from_diff(diff)
    assert [(n.path, n.lines, n.change) for n in notes] == [
        ("n/New.java", "+1–3", "New.java 새 파일 생성 (3줄)"),
        ("old/Gone.java", "-1–2", "Gone.java 파일 삭제 (2줄)"),
        ("b/Neu.java", "~1", "Old.java → Neu.java 이동, 1행 수정"),
        ("img/logo.png", "", "logo.png 바이너리 파일 변경"),
        ("run.sh", "", "run.sh 파일 권한 변경")]


def test_odd_diffs_never_raise():
    # a removed line that reads like a file header stays inside its hunk (the hunk counts decide)
    diff = "--- a/x.txt\n+++ b/x.txt\n@@ -1,2 +1,1 @@\n--- a/not-a-file\n keep\n"
    [n] = notes_from_diff(diff)
    assert (n.path, n.lines) == ("x.txt", "-1")
    assert notes_from_diff("") == [] and notes_from_diff("no diff at all\n") == []
    cut = DIFF3[: DIFF3.index("+    <line3/>")]                         # cut by errors.diff_chars
    assert notes_from_diff(cut)[0].lines == "+42–44"
    crlf = DIFF3.replace("\n", "\r\n")
    assert [n.lines for n in notes_from_diff(crlf)] == ["+42–47", "~12", "-3"]
    quoted = 'diff --git "a/\\355\\225\\234.java" "b/\\355\\225\\234.java"\n--- "a/\\355\\225\\234.java"\n' \
             '+++ "b/\\355\\225\\234.java"\n@@ -1 +1 @@\n-a\n+b\n'
    assert [(n.path, n.lines) for n in notes_from_diff(quoted)] == [("한.java", "~1")]
    many = "--- a/M.java\n+++ b/M.java\n@@ -1,20 +1,20 @@\n" + "".join(f"-{i}\n+{i}x\n k\n" for i in range(10))
    [n] = notes_from_diff(many)
    assert n.lines.endswith(", … +2") and n.change.endswith(" 외 2곳")


# ---------------------------------------------------------------------------
# merge_notes
# ---------------------------------------------------------------------------
def test_merge_attaches_lines_and_appends_unmentioned_files():
    llm = [EditNote(path="module/comp-jar/pom.xml", where="<dependencies> 끝", change="jakarta.xml.bind-api 추가"),
           EditNote(path="foo.JAVA", where="import 줄", change="javax 를 jakarta 로")]          # a short path
    out = merge_notes(llm, DIFF3)
    assert [(n.path, n.where, n.lines, n.source) for n in out] == [
        ("module/comp-jar/pom.xml", "<dependencies> 끝", "+42–47", "llm"),
        ("src/main/java/a/Foo.java", "import 줄", "~12", "llm"),                    # the diff's full path
        ("src/main/java/a/Bar.java", "", "-3", "diff")]                             # the LLM did not mention it
    assert out[2].change == "Bar.java 3행 삭제"
    assert llm[1].path == "foo.JAVA" and llm[1].lines == ""                          # inputs untouched


def test_merge_edges():
    # nothing from the LLM: the diff's notes; no diff: the LLM's notes as they are; never empty with a diff
    assert [n.source for n in merge_notes([], DIFF3)] == ["diff"] * 3
    llm = [EditNote(path="a/X.java", change="x"), EditNote(path="", change="general")]
    assert merge_notes(llm, "") == llm
    # a note for a file the diff did not touch stays without lines; duplicates collapse; dict notes are accepted
    out = merge_notes([EditNote(path="other/Y.java", change="y"), EditNote(path="other/Y.java", change="y"),
                       {"path": "comp-jar/pom.xml", "change": "dep"}], DIFF3)
    assert [(n.path, n.lines) for n in out] == [("other/Y.java", ""), ("module/comp-jar/pom.xml", "+42–47"),
                                                ("src/main/java/a/Foo.java", "~12"), ("src/main/java/a/Bar.java", "-3")]
    # an ambiguous short path (two changed poms) is not guessed: the note stays, both poms get diff notes
    two = ("--- a/a/pom.xml\n+++ b/a/pom.xml\n@@ -1 +1,2 @@\n x\n+y\n"
           "--- a/b/pom.xml\n+++ b/b/pom.xml\n@@ -3 +3 @@\n-p\n+q\n")
    out = merge_notes([EditNote(path="pom.xml", change="dependency added")], two)
    assert [(n.path, n.lines, n.source) for n in out] == [("pom.xml", "", "llm"), ("a/pom.xml", "+2", "diff"),
                                                          ("b/pom.xml", "~3", "diff")]
    # ... but an exact root pom.xml next to a module pom is matched exactly
    root = "--- a/pom.xml\n+++ b/pom.xml\n@@ -1 +1 @@\n-a\n+b\n" + two
    assert merge_notes([EditNote(path="pom.xml", change="c")], root)[0].lines == "~1"


def test_same_path_and_answer_notes():
    assert same_path("comp-jar/pom.xml", "module/comp-jar/pom.xml") and same_path("./A/b.java", "a\\B.java")
    assert not same_path("pom.xml", "module/comp-jar/pom.xml") and not same_path("a/b.java", "xa/b.java")
    edits = [Edit(op="delete", path="a/pom.xml", desc="a/pom.xml 삭제"), Edit(op="delete", path="b/C.java", desc="C"),
             Edit(op="delete", path="d/E.java")]                                  # no desc: no note
    changes = [EditNote(path="pom.xml", change="pom 정리")]                      # the only edited pom: described
    assert [(n.path, n.change) for n in answer_notes(changes, edits)] == [("pom.xml", "pom 정리"), ("b/C.java", "C")]
    two = edits + [Edit(op="delete", path="z/pom.xml", desc="z")]                # two poms: the bare name covers none
    assert [n.path for n in answer_notes(changes, two)] == ["pom.xml", "a/pom.xml", "b/C.java", "z/pom.xml"]
    assert answer_notes([], []) == []


def test_notes_from_json_and_text():
    notes, problems = notes_from_json([{"file": "a/B.java", "location": "import 줄", "desc": "javax → jakarta"},
                                       "pom.xml: 의존성 추가", 3, {"path": "x"}])
    assert [(n.path, n.where, n.change) for n in notes] == [("a/B.java", "import 줄", "javax → jakarta"),
                                                            ("pom.xml", "", "의존성 추가")]
    assert problems == ["change 3: not an object (int)", "change 4: no 'where' / 'change' text"]
    assert note_from_text("1. `a/b.xml` — x") == EditNote(path="a/b.xml", change="x")
    assert note_from_text("the imports change") == EditNote(path="", change="the imports change")
    assert note_from_text("  ") is None
    assert notes_from_json("nope")[0] == [EditNote(path="", change="nope")]
    assert "must be a JSON list" in notes_from_json(5)[1][0]


def test_describe_language():
    class R:
        language = "ko"

    class C:
        report = R()
    assert describe_language(C()) == "Korean (한국어)"
    assert "Korean (한국어) when they are Korean or when unsure" in describe_language(None)


# ---------------------------------------------------------------------------
# Edit.desc
# ---------------------------------------------------------------------------
def test_edit_desc_is_parsed_and_never_a_pom_call():
    e, probs = edit_from_item({"op": "pom", "path": "a/pom.xml", "desc": "a/pom.xml 의 <dependencies> 끝에\n  g:a 를 추가합니다",
                               "add_dependency": {"group_id": "g", "artifact_id": "a"}})
    assert probs == [] and e.desc == "a/pom.xml 의 <dependencies> 끝에 g:a 를 추가합니다"
    assert e.pom == {"add_dependency": {"group_id": "g", "artifact_id": "a"}}
    e, _ = edit_from_item({"op": "replace", "path": "B.java", "old": "x", "new": "y", "where": "import 줄",
                           "description": "javax 를 jakarta 로 바꿉니다"})
    assert e.desc == "import 줄: javax 를 jakarta 로 바꿉니다"
    e, _ = edit_from_item({"op": "delete", "path": "x", "summary": "x 를 지웁니다", "where": "x"})
    assert e.desc == "x 를 지웁니다"                                               # where already named
    assert edits_from_json([{"op": "delete", "path": "x"}])[0] == [Edit(op="delete", path="x")]   # desc optional


def test_desc_as_comments_in_the_script_and_in_the_summary():
    tricky = 'a/pom.xml 의 "<parent>" 를 바꿉니다 \\ """ \'\'\' # {x}\r\n 두 번째 줄\x85 끝 ' + "긴 설명 " * 30
    edits = [Edit(op="delete", path="a.txt", desc=tricky),
             Edit(op="pom", path="pom.xml", desc="pom.xml 의 <properties> 에서 java.version 을 21 로 설정합니다",
                  pom={"set_property": {"name": "java.version", "value": "21"}}),
             Edit(op="delete", path="b.txt")]
    code = edits_script(edits)
    assert check_script(code, max_chars=0) == []
    lines = code.split("\n")
    i = lines.index("    # edit 1: delete a.txt")
    block = []
    for ln in lines[i + 1:]:
        if ln == "    try:":
            break
        block.append(ln)
    assert len(block) > 1 and all(ln.startswith("    # ") and len(ln) <= 120 for ln in block)
    assert " ".join(ln[6:] for ln in block) == " ".join(tricky.split())
    assert "    # edit 2: pom pom.xml\n    # pom.xml 의 <properties> 에서 java.version 을 21 로 설정합니다\n    try:" in code
    assert "    # edit 3: delete b.txt\n    try:" in code                         # no desc: no extra line
    assert summarize_edits(edits[1:]) == ("pom pom.xml set_property — pom.xml 의 <properties> 에서 java.version 을 21 로 "
                                          "설정합니다; delete b.txt")
