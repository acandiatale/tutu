"""v2.1e (2026-10-27, "as-is 코드와 to-be 코드도"): every 수정 note carries the changed regions of its file —
`EditNote.hunks` (`EditHunk`: lines, before = as-is, after = to-be, truncated) — taken from the applied diff
(`sbmig.script.notes`: `hunks_from_diff`, `notes_from_diff`, `merge_notes`, `attach_hunks`), stored masked and whole
by the error list (`ErrorList.finish_try`, errors.jsonl, the KB's per-error JSON), kept out of the one-line views
(errors.md, the report) and rendered with the code by `note_block` (`kb errors show` / `cases show`)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from sbmig.core.models import BuildError, BuildResult, EditHunk, EditNote, ErrorKind
from sbmig.core.workspace import Workspace
from sbmig.errors import CaseStore, ErrorList
from sbmig.errors.common import note_block, note_text
from sbmig.script.notes import DIFF_CUT_LINE, attach_hunks, hunks_from_diff, merge_notes, notes_from_diff

A_OLD = ("package a;\n\nimport javax.annotation.PostConstruct;\n\nclass A {\n    int x = 1;\n    void m() {}\n"
         "    // remove me 1\n    // remove me 2\n    void n() {}\n}\n")
A_NEW = ("package a;\n\nimport jakarta.annotation.PostConstruct;\n\nclass A {\n    int x = 1;\n    int y = 2;\n"
         "    void m() {}\n    void n() {}\n}\n")
OLD_BODY = "".join(f"    int f{i} = {i};\n" for i in range(12))
RENAMED_OLD = "package old;\n\nclass Old {\n" + OLD_BODY + "}\n"
RENAMED_NEW = "package neu;\n\nclass Old {\n" + OLD_BODY + "}\n"
A = "src/main/java/a/A.java"
KO = "한글/메모.txt"


def _git(root: Path, *args: str) -> str:
    cmd = ["git", "-c", "core.quotepath=false", "-c", "core.autocrlf=false", "-c", "diff.renames=true",
           "-c", "commit.gpgsign=false", *args]
    return subprocess.run(cmd, cwd=root, capture_output=True, check=True).stdout.decode("utf-8", "replace")


@pytest.fixture(scope="module")
def real(tmp_path_factory) -> dict[str, str]:
    """A real `git diff` (through the Workspace, as the fix loop takes it) over every kind of change: `~` / `+` / `-`
    runs in one file, a new file, a deleted file, a rename, a binary file, CRLF, a Korean path, no newline at EOF,
    a long run (max_lines); plus the same diff with git's default quoted paths."""
    base = tmp_path_factory.mktemp("v21e")
    root = base / "proj"
    files = {A: A_OLD.encode(), "src/Gone.java": b"// gone\nclass Gone { int g; }\n",
             "old/Old.java": RENAMED_OLD.encode(), "img/logo.png": b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x01",
             "crlf/W.txt": b"one\r\ntwo\r\nthree\r\n", KO: "가\n나\n".encode(), "nonl.txt": b"a\nb",
             "big.txt": "".join(f"line {i}\n" for i in range(10)).encode()}
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    ws = Workspace(root, base / "run", "r-v21e")
    pre = ws.git_init()
    (root / A).write_bytes(A_NEW.encode())
    (root / "src/New.java").write_bytes(b"package n;\nclass New {}\n")
    (root / "src/Gone.java").unlink()
    (root / "neu").mkdir()
    (root / "neu/Neu.java").write_bytes(RENAMED_NEW.encode())
    (root / "old/Old.java").unlink()
    (root / "img/logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x02")
    (root / "crlf/W.txt").write_bytes(b"one\r\nTWO\r\nthree\r\n")
    (root / KO).write_bytes("가\n다\n".encode())
    (root / "nonl.txt").write_bytes(b"a\nc")
    (root / "big.txt").write_bytes("".join(f"LINE {i}\n" for i in range(10)).encode())
    sha = ws.commit("change")
    return {"diff": _git(root, "diff", "--no-color", pre, sha),
            "ws_diff": ws.diff(pre, sha),
            "quoted": _git(root, "-c", "core.quotepath=true", "diff", "--no-color", pre, sha)}


def H(lines: str, before: str = "", after: str = "", truncated: bool = False) -> EditHunk:
    return EditHunk(lines=lines, before=before, after=after, truncated=truncated)


EXPECTED = {
    A: [H("~3", "import javax.annotation.PostConstruct;", "import jakarta.annotation.PostConstruct;"),
        H("+7", after="    int y = 2;"),
        H("-8–9", before="    // remove me 1\n    // remove me 2")],
    "src/New.java": [H("+1–2", after="package n;\nclass New {}")],
    "src/Gone.java": [H("-1–2", before="// gone\nclass Gone { int g; }")],
    "neu/Neu.java": [H("~1", "package old;", "package neu;")],
    "img/logo.png": [],
    "crlf/W.txt": [H("~2", "two", "TWO")],
    KO: [H("~2", "나", "다")],
    "nonl.txt": [H("~2", "b", "c")],
    "big.txt": [H("~1–10", "\n".join(f"line {i}" for i in range(10)), "\n".join(f"LINE {i}" for i in range(10)))],
}


# ---------------------------------------------------------------------------------------- hunks_from_diff
def test_hunks_from_a_real_git_diff(real):
    assert "rename from old/Old.java" in real["diff"] and "Binary files" in real["diff"]
    assert "\\ No newline at end of file" in real["diff"] and "\r\n" in real["diff"]
    got = hunks_from_diff(real["diff"])
    assert got == EXPECTED
    assert hunks_from_diff(real["ws_diff"]) == EXPECTED                       # the Workspace's own diff
    assert all("\r" not in h.before + h.after for hs in got.values() for h in hs)
    # the quoted Korean path (git's default core.quotepath) is read back to the same key
    assert hunks_from_diff(real["quoted"])[KO] == EXPECTED[KO] and '"a/\\355' in real["quoted"]


def test_hunk_lines_are_spelled_like_the_note_lines(real):
    notes = notes_from_diff(real["diff"])
    assert {n.path for n in notes} == set(EXPECTED)
    for n in notes:
        assert n.hunks == EXPECTED[n.path]
        assert n.lines == ", ".join(h.lines for h in n.hunks)                 # the same runs, the same spelling


def test_max_lines_cuts_each_side_and_marks_the_hunk(real):
    got = hunks_from_diff(real["diff"], max_lines=3)
    [big] = got["big.txt"]
    assert big.truncated and big.lines == "~1–10"
    assert big.before == "line 0\nline 1\nline 2\n… (+7 lines)" and big.after == "LINE 0\nLINE 1\nLINE 2\n… (+7 lines)"
    assert got[A] == EXPECTED[A]                                              # short hunks untouched
    assert next(n for n in notes_from_diff(real["diff"], max_lines=3) if n.path == "big.txt").hunks == [big]
    assert hunks_from_diff(real["diff"], max_lines=0) == EXPECTED == hunks_from_diff(real["diff"], max_lines="x")


def test_hand_written_edges():
    # a mode-only change and a pure rename have no hunks; several runs of one hunk in order
    diff = ("diff --git a/run.sh b/run.sh\nold mode 100644\nnew mode 100755\n"
            "diff --git a/p/X.java b/q/X.java\nsimilarity index 100%\nrename from p/X.java\nrename to q/X.java\n"
            "diff --git a/M.java b/M.java\n--- a/M.java\n+++ b/M.java\n@@ -1,7 +1,7 @@\n a\n-b\n-c\n+B\n+C\n+D\n d\n e\n-f\n g\n"
            "@@ -20,2 +21,3 @@\n x\n+y\n z\n")
    got = hunks_from_diff(diff)
    assert got["run.sh"] == [] and got["q/X.java"] == []
    assert got["M.java"] == [H("~2–4", "b\nc", "B\nC\nD"), H("-6", before="f"), H("+22", after="y")]
    # an added line that reads like a header stays code; trailing spaces and empty lines are kept as written
    tricky = "--- a/t.txt\n+++ b/t.txt\n@@ -1,1 +1,3 @@\n-old  \n+--- a/x\n+\n+  new\n"
    assert hunks_from_diff(tricky)["t.txt"] == [H("~1–3", "old  ", "--- a/x\n\n  new")]
    # a diff cut by errors.diff_chars: the run it cut ends with the marker line (not `truncated`)
    cut = "--- a/c.txt\n+++ b/c.txt\n@@ -1,2 +1,4 @@\n-a\n+A\n+B part\n…[120 more characters]"
    assert hunks_from_diff(cut)["c.txt"] == [H("~1–2", "a", f"A\nB part\n{DIFF_CUT_LINE}")]
    # nothing readable → {}
    assert hunks_from_diff("") == {} and hunks_from_diff("garbage\n@@ nonsense") == {} and hunks_from_diff(None) == {}


# ---------------------------------------------------------------------------------------- merge / attach
def test_merge_notes_gives_every_matched_llm_note_all_its_file_hunks(real):
    llm = [EditNote(path=A, where="import 줄", change="javax → jakarta"),
           EditNote(path=A, where="필드", change="y 필드 추가"),                  # a second note on the same file
           EditNote(path="w.TXT", change="crlf"),                                 # a short path (unique basename)
           EditNote(path="elsewhere/Z.java", change="not in the diff")]
    out = merge_notes(llm, real["diff"])
    a1, a2, w, z = out[:4]
    assert a1.hunks == a2.hunks == EXPECTED[A] and a1.hunks[0] is not a2.hunks[0]   # copies, not shared objects
    assert (a1.lines, a1.where) == ("~3, +7, -8–9", "import 줄")                    # lines as before
    assert (w.path, w.hunks) == ("crlf/W.txt", EXPECTED["crlf/W.txt"])
    assert z.hunks == [] and z.lines == ""
    rest = {n.path: n.hunks for n in out[4:]}                                       # the diff's own notes
    assert rest == {k: v for k, v in EXPECTED.items() if k not in (A, "crlf/W.txt")}
    assert llm[0].hunks == []                                                       # inputs untouched
    assert merge_notes(llm, real["diff"], max_lines=1)[0].hunks[2].truncated       # the -8–9 run, 2 lines


def test_attach_hunks_fills_only_notes_without_hunks(real):
    own = H("~99", "mine", "mine too")
    notes = [EditNote(path=A, change="x", lines="~3, +7, -8–9", source="diff"),
             EditNote(path="nonl.txt", change="kept", hunks=[own]),                 # already has hunks
             {"path": "big.txt", "change": "dict note"},
             EditNote(path="nowhere.txt", change="no file"),
             42]
    out = attach_hunks(notes, real["diff"], max_lines=2)
    assert out[0].hunks == EXPECTED[A][:2] + [H("-8–9", before="    // remove me 1\n    // remove me 2")]
    assert out[0].path == A and out[0].lines == "~3, +7, -8–9" and out[0] is not notes[0]
    assert out[1].hunks == [own]
    assert isinstance(out[2], EditNote) and out[2].hunks[0].truncated and out[2].hunks[0].before.endswith("(+8 lines)")
    assert out[3].hunks == [] and out[4] == 42
    assert notes[0].hunks == []                                                     # copies
    for diff in ("", "garbage\n", None):                                            # nothing readable: unchanged
        back = attach_hunks(notes, diff)
        assert [getattr(n, "hunks", None) for n in back] == [[], [own], None, [], None]
    assert attach_hunks([], real["diff"]) == [] and attach_hunks(None, real["diff"]) == []


# ---------------------------------------------------------------------------------------- the error list
def _err() -> BuildError:
    l2 = "compile|package_does_not_exist|jakarta.annotation"
    return BuildError(kind=ErrorKind.COMPILE, message="package javax.annotation does not exist", file=A, line=3,
                      signature=l2 + "|L1", signature_generic=l2)


def _build(*errors: BuildError) -> BuildResult:
    return BuildResult(ok=not errors, goal="package", command=["mvn"], exit_code=1 if errors else 0, duration_s=1.0,
                       log_path="/x.log", errors=list(errors))


SECRET_DIFF = ("diff --git a/app.properties b/app.properties\n--- a/app.properties\n+++ b/app.properties\n"
               "@@ -1,2 +1,2 @@\n server.port=8080\n-db.password=oldsecret1\n+db.password=hunter2\n"
               "diff --git a/Long.java b/Long.java\n--- a/Long.java\n+++ b/Long.java\n@@ -1,0 +1,400 @@\n"
               + "".join(f"+    int field{i} = {i}; // a long generated block\n" for i in range(400)))


def test_finish_try_keeps_the_hunks_masked_and_whole(tmp_path: Path, cfg):
    cfg2 = cfg.model_copy(update={"errors": cfg.errors.model_copy(update={"diff_chars": 300})})
    root = tmp_path / "run" / "project"
    el = ErrorList(tmp_path / "run", "r1", project="p", cfg=cfg2, roots=[root])
    cid = el.add_build(_build(_err()), "compile")[0].case_id
    el.begin_try(cid, 1, "llm-new")
    notes = merge_notes([EditNote(path="app.properties", where="db 설정", change="비밀번호 키를 바꿉니다")], SECRET_DIFF)
    notes.append({"path": f"{root}/src/B.java", "change": "dict", "hunks": [
        {"lines": "~4", "before": f"see {root}/src/B.java", "after": "String token = \"abcdef123456\";"},
        "not a hunk", {"lines": "~5", "before": None, "after": "ok", "truncated": True}]})
    notes.append({"path": "C.java", "change": "odd hunks", "hunks": "nope"})
    el.finish_try(cid, 1, applied="edits", diff=SECRET_DIFF, edits=notes, result="improved")

    t = el.get_try(cid, 1)
    props, long_, b, c = t.edits
    [hp] = props.hunks
    assert hp.lines == "~2" and "hunter2" not in hp.after and "oldsecret1" not in hp.before
    assert hp.before.startswith("db.password=") and hp.after.startswith("db.password=") and "***" in hp.after
    assert len(long_.hunks[0].after.splitlines()) == 400 and len(long_.hunks[0].after) > 6000   # whole, not capped
    assert len(t.diff) < 400                                                                 # the diff IS capped
    assert b.path == "src/B.java" and [h.lines for h in b.hunks] == ["~4", "~5"]              # the bad item dropped
    assert str(root) not in b.hunks[0].before and "abcdef123456" not in b.hunks[0].after
    assert b.hunks[1] == H("~5", after="ok", truncated=True)
    assert c.hunks == [] and c.change == "odd hunks"                                          # the note stays

    text = el.path.read_text(encoding="utf-8")
    assert "hunter2" not in text and "oldsecret1" not in text and "abcdef123456" not in text
    row = json.loads(text.splitlines()[0])["tries"][0]["edits"]
    assert row[0]["hunks"][0]["lines"] == "~2" and row[1]["hunks"][0]["after"].count("\n") == 399
    [back] = ErrorList.load(el.path)
    assert back.tries[0].edits == t.edits                                                    # round trip

    md = el.md_path.read_text(encoding="utf-8")                                              # one line per note
    assert f"  - {note_text(props)}\n" in md and "as-is" not in md and "field399" not in md and "hunter2" not in md

    rep = el.to_report()                                                                     # the report: no code
    assert all(n.hunks == [] for n in rep[0].tries[0].edits) and el.entries[0].tries[0].edits[0].hunks

    store = CaseStore(tmp_path / "kb", cfg2)                                                 # the KB copies
    store.record_run(el.entries)
    [f] = store.entry_files(cid)
    data = json.loads(f.read_text(encoding="utf-8"))
    assert data["tries"][0]["edits"][0]["hunks"][0]["before"].startswith("db.password=")
    assert data["tries"][0]["edits"][1]["hunks"][0]["after"].count("\n") == 399
    kb_text = store.errors_path.read_text(encoding="utf-8") + f.read_text(encoding="utf-8")
    assert "hunter2" not in kb_text and "abcdef123456" not in kb_text and '"hunks"' in kb_text


def test_finish_try_with_notes_without_hunks_stays_as_before(tmp_path: Path, cfg):
    el = ErrorList(tmp_path / "run", "r1", cfg=cfg)
    cid = el.add_build(_build(_err()), "compile")[0].case_id
    el.begin_try(cid, 1, "llm-new")
    el.finish_try(cid, 1, edits=[{"path": "pom.xml", "lines": "+3", "change": "dep"}])
    [n] = el.get_try(cid, 1).edits
    assert n == EditNote(path="pom.xml", lines="+3", change="dep") and n.hunks == []


# ---------------------------------------------------------------------------------------- note_block
def test_note_block_renders_the_note_line_and_the_code():
    n = EditNote(path=A, lines="~3, +7", where="import 줄", change="javax → jakarta",
                 hunks=[H("~3", "import javax.x;", "import jakarta.x;"), H("+7", after="int y;\nint z;", truncated=True)])
    assert note_block(n) == [
        note_text(n),
        "  as-is (~3):", "```", "import javax.x;", "```",
        "  to-be (~3):", "```", "import jakarta.x;", "```",
        "  to-be (+7):", "```", "int y;", "int z;", "```",
        "  (cut: errors.snippet_lines)"]
    as_dict = n.model_dump()
    assert note_block(as_dict) == note_block(n)                                      # dicts (a loaded JSON) too
    assert note_block({"path": "pom.xml", "lines": "-3"}) == ["pom.xml -3"]           # no hunks: the line alone
    assert note_block({"path": "x", "hunks": [{"before": "a ``` b"}, None, 5]}) == [
        "x", "  as-is:", "````", "a ``` b", "````"]                                    # a fence inside the code
    assert note_block({"path": "x", "hunks": "nope"}) == ["x"]
    assert note_block(n, fence="~~~")[2] == "~~~"
