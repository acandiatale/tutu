"""`sbmig.report.codediff.write_code_diff`: the code-change view's artifact (`<run dir>/codediff/`) on real temporary
git workspaces, plus its hook in the orchestrator's finalize."""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import pytest

from sbmig.core.workspace import GitError, Workspace
from sbmig.report import codediff as cd
from sbmig.report.codediff import move_groups, similarity, write_code_diff


def make_ws(tmp_path: Path, files: dict[str, bytes | str], run_id: str = "t-cd") -> Workspace:
    src = tmp_path / "proj"
    src.mkdir()
    for rel, data in files.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    w = Workspace.from_input(src, tmp_path / "out", run_id=run_id)
    w.git_init()
    return w


def put(ws: Workspace, rel: str, data: bytes | str) -> None:
    p = ws.root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)


def move(ws: Workspace, src: str, dst: str) -> None:
    ws.move(src, dst)


def load(ws: Workspace) -> tuple[dict, bytes]:
    out = ws.run_dir / "codediff"
    index = json.loads((out / "index.json").read_text(encoding="utf-8"))
    patch = (out / "patch.bin").read_bytes()
    check_artifact(ws, index, patch)
    return index, patch


def check_artifact(ws: Workspace, index: dict, patch: bytes) -> None:
    """The invariants every artifact keeps."""
    files = index["files"]
    assert [f["id"] for f in files] == list(range(len(files)))
    assert [f["path"] for f in files] == sorted(f["path"] for f in files)
    for f in files:
        if f["patch"] is not None:
            off, length = f["patch"]
            chunk = patch[off:off + length]
            assert chunk.startswith(b"@@ ") and len(chunk) == length
            assert b"\ndiff --git" not in chunk and not chunk.startswith(b"diff --git")
            assert f["omitted"] == ""
        else:
            assert f["omitted"] in ("", "binary", "too_large", "cap", "unavailable")
    s = index["summary"]
    assert s["modified"] + s["added"] + s["deleted"] + s["moved"] == s["files"] == index["files_total"]
    assert s["moved_only"] <= s["moved"]
    if index["files_total"] == len(files):
        assert s["added_lines"] == sum(f["added"] for f in files if not f["binary"])
        assert s["removed_lines"] == sum(f["removed"] for f in files if not f["binary"])
        assert s["binary"] == sum(1 for f in files if f["binary"])
    summary = json.loads((ws.run_dir / "codediff" / "summary.json").read_text(encoding="utf-8"))
    assert summary == {k: v for k, v in index.items() if k != "files"}
    assert sorted(os.listdir(ws.run_dir / "codediff")) == ["index.json", "patch.bin", "summary.json"]


def hunks(index: dict, patch: bytes, path: str) -> bytes:
    f = by_path(index, path)
    off, length = f["patch"]
    return patch[off:off + length]


def by_path(index: dict, path: str) -> dict:
    [f] = [f for f in index["files"] if f["path"] == path]
    return f


def events(ws: Workspace, name: str) -> list[dict]:
    lines = ws.journal_path.read_text(encoding="utf-8").splitlines()
    return [e for e in map(json.loads, lines) if e["event"] == name]


NUMBERED = "".join(f"line {i}\n" for i in range(1, 41))


# ------------------------------------------------------------------------------------------ basics
def test_an_in_place_change_with_the_configured_context(tmp_path, cfg):
    ws = make_ws(tmp_path, {"pom.xml": "<project/>\n", "src/A.txt": NUMBERED})
    put(ws, "src/A.txt", NUMBERED.replace("line 20\n", "line twenty\n"))
    cfg.report.code_diff_context = 3
    summary = write_code_diff(ws, cfg)
    index, patch = load(ws)
    [f] = index["files"]
    assert f == {"id": 0, "path": "src/A.txt", "old_path": None, "status": "modified", "changed": True,
                 "binary": False, "added": 1, "removed": 1, "encoding": "utf-8", "patch": f["patch"], "omitted": ""}
    h = hunks(index, patch, "src/A.txt")
    assert h.startswith(b"@@ -17,7 +17,7 @@")
    assert b" line 17\n" in h and b" line 23\n" in h and b"\n line 16\n" not in h and b"line 24\n" not in h
    assert b"-line 20\n+line twenty\n" in h
    assert index["context"] == 3 and index["zip_root"] == "project" and index["version"] == 1
    assert index["run_id"] == "t-cd" and index["base"] == ws.first_commit()
    assert summary == index["summary"] and summary["modified"] == 1 and summary["files"] == 1
    [ev] = events(ws, "code_diff")
    assert ev["files"] == 1 and ev["added_lines"] == 1 and ev["removed_lines"] == 1 and ev["moved"] == 0
    assert "seconds" in ev


def test_nothing_changed_writes_no_record(tmp_path, cfg):
    # review 2026-11-18, R4: an empty index made the page show an empty block (this test pinned the empty index)
    ws = make_ws(tmp_path, {"pom.xml": "<project/>\n"})
    assert write_code_diff(ws, cfg) is None
    assert not (ws.run_dir / "codediff").exists()
    assert events(ws, "code_diff_skipped")[-1]["reason"] == "the run changed no file"


def test_a_restructure_move_with_some_files_changed(tmp_path, cfg):
    ws = make_ws(tmp_path, {
        "pom.xml": "<project/>\n",
        "src/com/x/A.java": "class A {\n  int a;\n}\n",
        "src/com/x/B.java": "class B {\n  int b;\n  int c;\n  int d;\n}\n",
        "src/com/x/sub/C.java": "class C {}\n",
        "app.properties": "a=1\n",
    })
    move(ws, "src/com/x/A.java", "demo-back/src/main/java/com/x/A.java")
    move(ws, "src/com/x/B.java", "demo-back/src/main/java/com/x/B.java")
    move(ws, "src/com/x/sub/C.java", "demo-back/src/main/java/com/x/sub/C.java")
    move(ws, "app.properties", "demo-back/app.properties")
    put(ws, "demo-back/src/main/java/com/x/B.java", "class B {\n  int b;\n  int c2;\n  int d;\n}\n")
    ws.commit("restructure")
    write_code_diff(ws, cfg)
    index, patch = load(ws)
    a = by_path(index, "demo-back/src/main/java/com/x/A.java")
    assert (a["status"], a["changed"], a["old_path"], a["patch"], a["omitted"]) == \
        ("moved", False, "src/com/x/A.java", None, "")
    b = by_path(index, "demo-back/src/main/java/com/x/B.java")
    assert (b["status"], b["changed"], b["old_path"], b["added"], b["removed"]) == \
        ("moved", True, "src/com/x/B.java", 1, 1)
    hb = hunks(index, patch, b["path"])
    assert b"-  int c;\n+  int c2;\n" in hb and b" class B {\n" in hb
    assert by_path(index, "demo-back/app.properties")["old_path"] == "app.properties"
    assert index["moves"] == [{"from": "src/", "to": "demo-back/src/main/java/", "files": 3},
                              {"from": "", "to": "demo-back/", "files": 1}]
    assert index["moves_total"] == 2
    s = index["summary"]
    assert (s["files"], s["moved"], s["moved_only"], s["deleted"], s["added"]) == (4, 4, 3, 0, 0)


def test_same_named_files_of_two_packages_find_their_own_origin(tmp_path, cfg):
    util_a = "package com.a;\nclass Util {\n  static int alpha() { return 1; }\n  static int beta() { return 2; }\n}\n"
    util_b = "package com.b;\nclass Util {\n  static String gamma() { return \"g\"; }\n  static String delta() { return \"d\"; }\n}\n"
    same = "class Same {}\n"
    ws = make_ws(tmp_path, {"pom.xml": "<project/>\n", "src/com/a/Util.java": util_a, "src/com/b/Util.java": util_b,
                            "src/com/a/Same.java": same, "src/com/b/Same.java": same})
    for pkg in ("a", "b"):
        move(ws, f"src/com/{pkg}/Util.java", f"m/src/main/java/com/{pkg}/Util.java")
        move(ws, f"src/com/{pkg}/Same.java", f"m/src/main/java/com/{pkg}/Same.java")
    put(ws, "m/src/main/java/com/a/Util.java", util_a.replace("return 1", "return 10"))
    put(ws, "m/src/main/java/com/b/Util.java", util_b.replace("\"g\"", "\"G\""))
    ws.commit("migration")                       # the migration commits what it changed
    write_code_diff(ws, cfg)
    index, _ = load(ws)
    for pkg in ("a", "b"):
        u = by_path(index, f"m/src/main/java/com/{pkg}/Util.java")
        assert (u["status"], u["old_path"], u["changed"]) == ("moved", f"src/com/{pkg}/Util.java", True)
        s = by_path(index, f"m/src/main/java/com/{pkg}/Same.java")
        assert (s["status"], s["old_path"], s["changed"]) == ("moved", f"src/com/{pkg}/Same.java", False)
    assert index["summary"]["deleted"] == 0 and index["summary"]["added"] == 0


def test_a_new_file_is_not_paired_with_an_unrelated_vanished_one(tmp_path, cfg):
    old = "class Config {\n  String url = \"jdbc:x\";\n  int pool = 5;\n}\n"
    new = "import java.util.List;\n\npublic record Config(List<String> names, boolean on) {\n  static Config off() { return null; }\n}\n"
    ws = make_ws(tmp_path, {"pom.xml": "<project/>\n", "old/Config.java": old})
    ws.delete("old/Config.java")
    put(ws, "new/Config.java", new)
    ws.commit("migration")                       # the migration commits what it changed
    write_code_diff(ws, cfg)
    index, patch = load(ws)
    assert by_path(index, "new/Config.java")["status"] == "added"
    d = by_path(index, "old/Config.java")
    assert (d["status"], d["old_path"], d["removed"], d["added"]) == ("deleted", None, 4, 0)
    h = hunks(index, patch, "old/Config.java")
    assert h.startswith(b"@@ -1,4 +0,0 @@") and all(line.startswith(b"-") for line in h.splitlines()[1:])


def test_a_moved_and_edited_file_is_paired_by_similarity(tmp_path, cfg):
    body = "".join(f"  int f{i} = {i};\n" for i in range(10))
    ws = make_ws(tmp_path, {"pom.xml": "<project/>\n", "src/x/Thing.java": f"class Thing {{\n{body}}}\n"})
    ws.delete("src/x/Thing.java")
    put(ws, "mod/src/main/java/x/Thing.java", f"class Thing {{\n{body}  int extra = 1;\n}}\n")
    ws.commit("migration")                       # the migration commits what it changed
    write_code_diff(ws, cfg)
    index, _ = load(ws)
    t = by_path(index, "mod/src/main/java/x/Thing.java")
    assert (t["status"], t["old_path"], t["changed"], t["added"], t["removed"]) == \
        ("moved", "src/x/Thing.java", True, 1, 0)


def test_similarity_rule():
    assert similarity(b"a\nb\nc\nd\n", b"a\nb\nx\ny\n") == 0.5
    assert similarity(b"a\n\n\nb\n", b"a\nb\n") == 1.0                  # blank lines do not count
    assert similarity(b"a\na\n", b"a\n") == 0.5                         # a multiset
    assert similarity(b"\0bin", b"\0bin") == 0.0 and similarity(b"", b"") == 0.0


def test_move_groups():
    assert move_groups([("src/com/x/A.java", "back/src/main/java/com/x/A.java"), ("A.txt", "m/A.txt"),
                        ("src/com/y/B.java", "back/src/main/java/com/y/B.java")]) == [
        {"from": "src/", "to": "back/src/main/java/", "files": 2}, {"from": "", "to": "m/", "files": 1}]


def test_added_deleted_binary_and_empty_files(tmp_path, cfg):
    jar = b"PK\x03\x04" + bytes(range(256)) * 4
    ws = make_ws(tmp_path, {"pom.xml": "<project/>\n", "lib/a.jar": jar, "gone.txt": "one\ntwo\n",
                            "gone-empty.txt": "", "lib/same.jar": jar + b"x"})
    put(ws, "lib/a.jar", jar + b"changed")
    put(ws, "lib/new.jar", b"PK\x03\x04\x00\x00new")
    move(ws, "lib/same.jar", "module/lib/same.jar")
    ws.delete("gone.txt")
    ws.delete("gone-empty.txt")
    put(ws, "src/New.java", "class New {}\n")
    put(ws, "src/Empty.txt", "")
    ws.commit("migration")                       # the migration commits what it changed
    write_code_diff(ws, cfg)
    index, patch = load(ws)
    a = by_path(index, "lib/a.jar")
    assert (a["status"], a["binary"], a["patch"], a["omitted"], a["added"], a["removed"]) == \
        ("modified", True, None, "binary", 0, 0)
    assert by_path(index, "lib/new.jar")["omitted"] == "binary"
    s = by_path(index, "module/lib/same.jar")
    assert (s["status"], s["changed"], s["binary"], s["omitted"]) == ("moved", False, True, "")
    g = by_path(index, "gone.txt")
    assert g["status"] == "deleted" and hunks(index, patch, "gone.txt") == b"@@ -1,2 +0,0 @@\n-one\n-two\n"
    e = by_path(index, "gone-empty.txt")
    assert (e["status"], e["patch"], e["omitted"]) == ("deleted", None, "")
    n = by_path(index, "src/New.java")
    assert n["status"] == "added" and hunks(index, patch, "src/New.java") == b"@@ -0,0 +1 @@\n+class New {}\n"
    em = by_path(index, "src/Empty.txt")
    assert (em["status"], em["patch"], em["omitted"], em["added"]) == ("added", None, "", 0)
    assert index["summary"]["binary"] == 3


def test_encodings_line_ends_and_names(tmp_path, cfg):
    cp949 = "// 한글 주석\nclass K {\n  String s = \"값\";\n}\n".encode("cp949")
    crlf = b"a=1\r\nb=2\r\nc=3\r\n"
    ws = make_ws(tmp_path, {"pom.xml": "<project/>\n", "src/K.java": cp949, "conf/app.properties": crlf,
                            "src/NoEol.txt": "x\ny", "docs/설명 파일.txt": "하나\n둘\n"})
    put(ws, "src/K.java", cp949.replace("값".encode("cp949"), "새 값".encode("cp949")))
    put(ws, "conf/app.properties", crlf.replace(b"b=2", b"b=20"))
    put(ws, "src/NoEol.txt", "x\nz")
    move(ws, "docs/설명 파일.txt", "my docs/설명 파일.txt")
    put(ws, "my docs/설명 파일.txt", "하나\n둘\n셋\n")
    ws.commit("migration")                       # the migration commits what it changed
    write_code_diff(ws, cfg)
    index, patch = load(ws)
    k = by_path(index, "src/K.java")
    assert k["encoding"] == "cp949"
    assert "새 값".encode("cp949") in hunks(index, patch, "src/K.java")
    assert b"-b=2\r\n+b=20\r\n" in hunks(index, patch, "conf/app.properties")
    assert b"\\ No newline at end of file" in hunks(index, patch, "src/NoEol.txt")
    m = by_path(index, "my docs/설명 파일.txt")
    assert (m["status"], m["old_path"], m["changed"]) == ("moved", "docs/설명 파일.txt", True)
    assert "+셋\n".encode() in hunks(index, patch, "my docs/설명 파일.txt")
    assert index["moves"] == [{"from": "docs/", "to": "my docs/", "files": 1}]


def test_a_deleted_cp949_file_has_its_encoding_from_its_bytes(tmp_path, cfg):
    ws = make_ws(tmp_path, {"pom.xml": "<project/>\n", "src/Old.java": "// 한글\nclass Old {}\n".encode("cp949")})
    ws.delete("src/Old.java")
    write_code_diff(ws, cfg)
    index, _ = load(ws)
    assert by_path(index, "src/Old.java")["encoding"] == "cp949"


def test_uncommitted_changes_are_included_and_git_state_is_untouched(tmp_path, cfg):
    ws = make_ws(tmp_path, {"pom.xml": "<project/>\n", "a.txt": "a\n"})
    put(ws, "a.txt", "b\n")
    put(ws, "untracked.txt", "u\n")
    head, index_bytes = ws.head(), (ws.root / ".git/index").read_bytes()
    status = ws._git("--no-optional-locks", "status", "--porcelain", "-z", "--untracked-files=all").stdout
    write_code_diff(ws, cfg)
    index, _ = load(ws)
    # an uncommitted change to a committed file stays; a file the last commit does not have is a build leftover
    # (review 2026-11-18, R3: this test listed untracked.txt as "added" before)
    assert by_path(index, "a.txt")["status"] == "modified" and [f["path"] for f in index["files"]] == ["a.txt"]
    assert ws.head() == head and (ws.root / ".git/index").read_bytes() == index_bytes
    assert ws._git("--no-optional-locks", "status", "--porcelain", "-z", "--untracked-files=all").stdout == status


def test_build_output_ignored_dirs_and_mode_only_changes_are_not_listed(tmp_path, cfg):
    ws = make_ws(tmp_path, {"pom.xml": "<project/>\n", "mod/target/keep.txt": "as-is target\n",
                            "run.sh": "echo hi\n", "boot/bin/pom.xml": "<project/>\n"})
    put(ws, "mod/pom.xml", "<project/>\n")            # mod/target/ is build output from now on
    put(ws, "mod/target/keep.txt", "changed by a build\n")
    put(ws, "mod/target/classes/X.txt", "out\n")
    os.chmod(ws.root / "run.sh", 0o755)
    ws.ignored_dirs = {"boot/bin"}
    put(ws, "boot/bin/pom.xml", "<project><x/></project>\n")
    ws.commit("migration")                       # the migration commits what it changed
    write_code_diff(ws, cfg)
    index, _ = load(ws)
    assert [f["path"] for f in index["files"]] == ["mod/pom.xml"]


def test_the_output_is_deterministic(tmp_path, cfg):
    ws = make_ws(tmp_path, {"pom.xml": "<project/>\n", "src/a/A.java": "class A {}\n", "src/b/A.java": "class A {}\n",
                            "x.txt": NUMBERED})
    move(ws, "src/a/A.java", "m/a/A.java")
    move(ws, "src/b/A.java", "m/b/A.java")
    put(ws, "x.txt", NUMBERED.replace("line 3\n", "3\n"))
    write_code_diff(ws, cfg)
    first, p1 = load(ws)
    write_code_diff(ws, cfg)
    second, p2 = load(ws)
    first.pop("generated_at"), second.pop("generated_at")
    assert first == second and p1 == p2


# ------------------------------------------------------------------------------------------ limits
def test_too_large_and_cap(tmp_path, cfg, monkeypatch):
    ws = make_ws(tmp_path, {"pom.xml": "<project/>\n", "a.txt": "a\n", "b.txt": "b\n", "c.txt": NUMBERED})
    put(ws, "a.txt", "A\n")
    put(ws, "b.txt", "B\n")
    put(ws, "c.txt", NUMBERED.replace("line", "LINE"))
    monkeypatch.setattr(cd, "MAX_FILE_PATCH", 200)
    monkeypatch.setattr(cd, "MAX_PATCH_TOTAL", 30)
    write_code_diff(ws, cfg)
    index, patch = load(ws)
    a, b, c = (by_path(index, p) for p in ("a.txt", "b.txt", "c.txt"))
    assert a["patch"] == [0, a["patch"][1]] and a["omitted"] == ""          # 20 bytes, fits
    assert b["patch"] is None and b["omitted"] == "cap"
    assert c["patch"] is None and c["omitted"] == "too_large" and c["added"] == 40
    assert len(patch) == a["patch"][1] <= 30


def test_max_files_keeps_changed_files_first(tmp_path, cfg, monkeypatch):
    files = {"pom.xml": "<project/>\n", **{f"src/F{i}.txt": f"{i}\n" for i in range(4)}}
    ws = make_ws(tmp_path, files)
    for i in range(4):
        move(ws, f"src/F{i}.txt", f"a/F{i}.txt")
    put(ws, "a/F3.txt", "3\nmore\n")
    put(ws, "z.txt", "new\n")
    monkeypatch.setattr(cd, "MAX_FILES", 3)
    ws.commit("migration")                       # the migration commits what it changed
    write_code_diff(ws, cfg)
    index, _ = load(ws)
    assert index["files_total"] == 5 and index["summary"]["files"] == 5
    assert [f["path"] for f in index["files"]] == ["a/F0.txt", "a/F3.txt", "z.txt"]


def test_a_section_count_mismatch_gives_no_patch_rather_than_a_wrong_one(tmp_path, cfg, monkeypatch):
    ws = make_ws(tmp_path, {"pom.xml": "<project/>\n", "a.txt": "a\n", "b.txt": "b\n"})
    put(ws, "a.txt", "A\n")
    put(ws, "b.txt", "B\n")
    real = cd._scan
    monkeypatch.setattr(cd, "_scan", lambda raw: real(raw)[1:])
    write_code_diff(ws, cfg)
    index, patch = load(ws)
    assert [(f["patch"], f["omitted"], f["added"]) for f in index["files"]] == [(None, "unavailable", 1)] * 2
    assert patch == b""


# ------------------------------------------------------------------------------------------ skips / failures
def test_switched_off_writes_nothing(tmp_path, cfg):
    ws = make_ws(tmp_path, {"pom.xml": "<project/>\n"})
    put(ws, "pom.xml", "<project><x/></project>\n")
    cfg.report.code_diff = False
    assert write_code_diff(ws, cfg) is None
    assert not (ws.run_dir / "codediff").exists()
    assert events(ws, "code_diff_skipped")[0]["reason"] == "report.code_diff is false"


def test_without_a_git_repository_nothing_is_written(tmp_path, cfg):
    src = tmp_path / "p"
    src.mkdir()
    (src / "pom.xml").write_text("<project/>\n", encoding="utf-8")
    ws = Workspace.from_input(src, tmp_path / "out", run_id="t-nogit")
    assert write_code_diff(ws, cfg) is None and not (ws.run_dir / "codediff").exists()


def test_a_failing_git_call_leaves_no_artifact(tmp_path, cfg, monkeypatch):
    ws = make_ws(tmp_path, {"pom.xml": "<project/>\n"})
    put(ws, "pom.xml", "<project><x/></project>\n")
    write_code_diff(ws, cfg)                                   # an earlier artifact is removed too
    assert (ws.run_dir / "codediff" / "index.json").is_file()

    def boom(*a, **k):
        raise GitError("git write-tree failed: simulated")
    monkeypatch.setattr(Workspace, "make_tree", boom)
    with pytest.raises(GitError):
        write_code_diff(ws, cfg)
    assert not (ws.run_dir / "codediff").exists()
    assert "simulated" in events(ws, "code_diff_skipped")[-1]["reason"]


# ------------------------------------------------------------------------------------------ performance
def test_a_few_thousand_moved_files_take_seconds_and_a_constant_number_of_git_calls(tmp_path, cfg, monkeypatch):
    n = 1500
    files = {"pom.xml": "<project/>\n"}
    for i in range(n):
        files[f"src/com/p{i % 30}/C{i}.java"] = f"package p{i % 30};\nclass C{i} {{\n  int v = {i};\n}}\n"
    ws = make_ws(tmp_path, files)
    # the restructure moves the whole tree; a third of the files also change
    (ws.root / "app/src/main/java").mkdir(parents=True)
    os.rename(ws.root / "src/com", ws.root / "app/src/main/java/com")
    for i in range(0, n, 3):
        put(ws, f"app/src/main/java/com/p{i % 30}/C{i}.java",
            f"package p{i % 30};\nclass C{i} {{\n  int v = {i + 1};\n}}\n")
    ws.commit("restructure")
    calls = {"git": 0, "batch": 0}
    real_bytes, real_cat = Workspace._git_bytes, Workspace._cat_blobs

    def counting_bytes(self, *a, **k):
        calls["git"] += 1
        return real_bytes(self, *a, **k)

    def counting_cat(self, *a, **k):
        calls["batch"] += 1
        return real_cat(self, *a, **k)
    monkeypatch.setattr(Workspace, "_git_bytes", counting_bytes)
    monkeypatch.setattr(Workspace, "_cat_blobs", counting_cat)
    t0 = time.monotonic()
    summary = write_code_diff(ws, cfg)
    took = time.monotonic() - t0
    assert summary["moved"] == n and summary["moved_only"] == n - len(range(0, n, 3))
    assert summary["added"] == summary["deleted"] == 0
    assert calls["git"] <= 20 and calls["batch"] <= 1, calls
    assert took < 60, took
    index, _ = load(ws)
    assert index["moves"] == [{"from": "src/", "to": "app/src/main/java/", "files": n}]


# ------------------------------------------------------------------------------------------ the orchestrator
def _orch(cfg, ws):
    from tests.loop.test_v2_orchestrator import make_v2
    return make_v2(cfg, ws)


@pytest.fixture()
def loop_ws(tmp_path) -> Workspace:
    from tests.loop.fakes import make_project
    w = Workspace.from_input(make_project(tmp_path), tmp_path / "out", run_id="t-run")
    w.git_init()
    return w


def test_finalize_writes_the_artifact_before_the_report_event(cfg, loop_ws):
    report = _orch(cfg, loop_ws).run()
    assert (loop_ws.run_dir / "codediff" / "index.json").is_file()
    names = [json.loads(x)["event"] for x in loop_ws.journal_path.read_text(encoding="utf-8").splitlines()]
    assert names.index("code_diff") < len(names) - 1 and names[-1] == "report"
    assert not any("codediff" in str(v) for v in report.outputs.values())


def test_finalize_goes_on_when_the_code_diff_fails(cfg, loop_ws, monkeypatch, caplog):
    def boom(*a, **k):
        raise GitError("simulated")
    monkeypatch.setattr(cd, "_build", boom)
    with caplog.at_level(logging.WARNING, logger="sbmig.loop"):
        _orch(cfg, loop_ws).run()
    warnings = [r for r in caplog.records if "code diff not written" in r.getMessage()]
    assert len(warnings) == 1
    assert not (loop_ws.run_dir / "codediff").exists()
    assert (loop_ws.run_dir / "report.md").exists()
    names = [json.loads(x)["event"] for x in loop_ws.journal_path.read_text(encoding="utf-8").splitlines()]
    assert names[-1] == "report"


def test_finalize_skips_a_dry_run(cfg, loop_ws):
    _orch(cfg, loop_ws).run(dry_run=True)
    assert not (loop_ws.run_dir / "codediff").exists()
