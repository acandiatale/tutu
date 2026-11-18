"""v2.1z-au: the web API of the code-change view. The artifact `<run dir>/codediff/` (index.json, summary.json,
patch.bin) and result.zip are built by hand here, exactly as docs/INTERFACES.md "v2.1z-au" describes them; the
writer (`sbmig/report/codediff.py`) is not imported."""
from __future__ import annotations

import json
import logging
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sbmig.web import codediff, contract, create_app
from sbmig.web.app import OPEN_ENV, TOKEN_ENV

RUN = "run-cd"
MOD_LINES = 6000
FOO = "demo-back/src/main/java/com/example/Foo.java"

# (path, fields, hunk bytes or None, final file bytes or None)
FILES: list[tuple[str, dict, bytes | None, bytes | None]] = [
    ("../evil.txt", {"status": "added", "added": 1}, b"@@ -0,0 +1 @@\n+x\n", b"x\n"),
    ("a/Added.java", {"status": "added", "added": 2},
     b"@@ -0,0 +1,2 @@\n+line1\n+line2\n\\ No newline at end of file\n", b"line1\nline2"),
    ("a/Context.java", {"status": "modified", "added": 1, "removed": 1},
     b"@@ -1,2 +1,2 @@\n-a\n+b\n end\n\\ No newline at end of file\n", b"b\nend"),
    ("a/Crlf.java", {"status": "modified", "added": 1, "removed": 1},
     b"@@ -1,2 +1,2 @@\n a\r\n-b\r\n+c\r\n", b"a\r\nc\r\n"),
    ("a/Damaged.java", {"status": "modified", "added": 1, "removed": 1}, b"garbage\n+x\n", b"x\n"),
    ("a/Ko.java", {"status": "modified", "added": 1, "removed": 1, "encoding": "cp949"},
     "@@ -1 +1 @@ class K\n-// 한글\n+// 한국어 주석\n".encode("cp949"), "// 한국어 주석\n".encode("cp949")),
    ("a/Long.java", {"status": "modified", "added": 1, "removed": 0},
     b"@@ -1,0 +2 @@\n+" + b"x" * 6000 + b"\n", b"a\n" + b"x" * 6000 + b"\n"),
    ("a/Moved.java", {"status": "moved", "old_path": "b/Moved.java", "changed": False, "omitted": ""}, None,
     b"same\n"),
    ("a/NoMember.java", {"status": "modified", "added": 1, "removed": 1}, b"@@ -1 +1 @@\n-a\n+b\n", None),
    ("a/Past.java", {"status": "modified", "added": 1}, "PAST", b"x\n"),
    ("a/big.txt", {"status": "modified", "omitted": "too_large", "added": 9000, "removed": 9000}, None, b"big\n"),
    ("a/bin.png", {"status": "modified", "binary": True, "omitted": "binary"}, None, b"\x89PNG\r\n\x00\x01"),
    ("a/cap.txt", {"status": "added", "omitted": "cap", "added": 3}, None, b"1\n2\n3\n"),
    (FOO, {"status": "moved", "old_path": "src/com/example/Foo.java", "changed": True, "added": 2, "removed": 2},
     (b"@@ -2,4 +2,4 @@ public class Foo {\n import a;\n-import javax.servlet.Http;\n+import jakarta.servlet.Http;\n"
      b" class X {\n-  int a;\n\\ No newline at end of file\n+  int a;\n"),
     b"package x;\nimport a;\nimport jakarta.servlet.Http;\nclass X {\n  int a;\n"),
    ("gone/Deleted.java", {"status": "deleted", "removed": 2}, b"@@ -1,2 +0,0 @@\n-x\n-y\n", None),
    ("src/Mod.java", {"status": "modified", "added": 2, "removed": 1},
     (b"@@ -3,3 +3,4 @@ class Mod {\n line 3\n+inserted\n line 4\n line 5\n"
      b"@@ -100 +101,2 @@ void run() {\n-old 100\n+line 101\n+line 102\n"),
     "".join(f"line {i}\n" for i in range(1, MOD_LINES + 1)).encode()),
]
IDX = {path: i for i, (path, *_rest) in enumerate(FILES)}


def _build(rd: Path, version: int = 1) -> dict:
    """Write codediff/{index.json,summary.json,patch.bin} and result.zip (zip_root "project") into `rd`."""
    d = rd / "codediff"
    d.mkdir(parents=True, exist_ok=True)
    blob, entries = b"", []
    for i, (path, fields, hunks, _final) in enumerate(FILES):
        e = {"id": i, "path": path, "old_path": None, "status": "modified", "changed": True, "binary": False,
             "added": 0, "removed": 0, "encoding": "utf-8", "patch": None, "omitted": ""}
        e.update(fields)
        if hunks == "PAST":
            e["patch"] = [10_000_000, 10]
        elif hunks is not None:
            blob += b"diff --git a/x b/x\n--- a/x\n+++ b/x\n"     # header lines are outside every file's range
            e["patch"] = [len(blob), len(hunks)]
            blob += hunks
        entries.append(e)
    summary = {"files": len(entries), "modified": 9, "added": 4, "deleted": 1, "moved": 2, "moved_only": 1,
               "added_lines": 10, "removed_lines": 8, "binary": 1}
    head = {"version": version, "run_id": rd.name, "generated_at": "2026-11-18T00:00:00Z", "base": "0" * 40,
            "context": 10, "zip_root": "project", "summary": summary,
            "moves": [{"from": "src/", "to": "demo-back/src/main/java/", "files": 1}], "moves_total": 1}
    index = {**head, "files": entries, "files_total": len(entries)}
    (d / "index.json").write_text(json.dumps(index), encoding="utf-8")
    (d / "summary.json").write_text(json.dumps(head), encoding="utf-8")
    (d / "patch.bin").write_bytes(blob)
    with zipfile.ZipFile(rd / "result.zip", "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("project/", b"")
        for path, _fields, _hunks, final in FILES:
            if final is not None:
                zf.writestr("evil.txt" if path.startswith("../") else f"project/{path}", final)
    return index


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    monkeypatch.delenv(OPEN_ENV, raising=False)
    codediff.clear_caches()
    yield
    codediff.clear_caches()


@pytest.fixture
def setup(tmp_path):
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    client = TestClient(create_app(tmp_path / "out", repo))
    rd = tmp_path / "out" / RUN
    rd.mkdir(parents=True)
    (rd / "report.md").write_text("# report\n", encoding="utf-8")
    index = _build(rd)
    return client, rd, index


def _file(client, path: str) -> dict:
    r = client.get(f"/api/runs/{RUN}/codediff/files/{IDX[path]}")
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    return r.json()


def _lines(client, path: str, **q):
    query = "&".join(f"{k}={v}" for k, v in q.items())
    return client.get(f"/api/runs/{RUN}/codediff/files/{IDX[path]}/lines" + (f"?{query}" if query else ""))


def test_index_route_serves_the_stored_index(setup):
    client, _rd, index = setup
    r = client.get(f"/api/runs/{RUN}/codediff")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json() == index


def test_modified_file_hunks_number_both_sides(setup):
    client, *_ = setup
    f = _file(client, "src/Mod.java")
    assert f["id"] == IDX["src/Mod.java"] and f["status"] == "modified" and f["expandable"] is True
    assert f["omitted"] == "" and f["no_newline"] == {"old": False, "new": False}
    h1, h2 = f["hunks"]
    assert (h1["old_start"], h1["old_lines"], h1["new_start"], h1["new_lines"], h1["section"]) == (3, 3, 3, 4, "class Mod {")
    assert h1["lines"] == [[" ", 3, 3, "line 3"], ["+", None, 4, "inserted"], [" ", 4, 5, "line 4"],
                           [" ", 5, 6, "line 5"]]
    # a missing count means 1
    assert (h2["old_start"], h2["old_lines"], h2["new_start"], h2["new_lines"]) == (100, 1, 101, 2)
    assert h2["section"] == "void run() {"
    assert h2["lines"] == [["-", 100, None, "old 100"], ["+", None, 101, "line 101"], ["+", None, 102, "line 102"]]


def test_moved_file_with_old_path_and_no_newline_on_the_old_side(setup):
    client, *_ = setup
    f = _file(client, FOO)
    assert f["status"] == "moved" and f["old_path"] == "src/com/example/Foo.java" and f["changed"] is True
    assert f["no_newline"] == {"old": True, "new": False}
    lines = f["hunks"][0]["lines"]
    assert lines[0] == [" ", 2, 2, "import a;"]
    assert lines[1] == ["-", 3, None, "import javax.servlet.Http;"]
    assert lines[2] == ["+", None, 3, "import jakarta.servlet.Http;"]
    assert lines[3] == [" ", 4, 4, "class X {"]
    assert lines[4] == ["-", 5, None, "  int a;"] and lines[5] == ["+", None, 5, "  int a;"]
    assert f["expandable"] is True


def test_added_and_deleted_files(setup):
    client, *_ = setup
    a = _file(client, "a/Added.java")
    assert a["status"] == "added" and a["no_newline"] == {"old": False, "new": True}
    assert a["hunks"][0]["old_start"] == 0 and a["hunks"][0]["old_lines"] == 0
    assert a["hunks"][0]["lines"] == [["+", None, 1, "line1"], ["+", None, 2, "line2"]]
    d = _file(client, "gone/Deleted.java")
    assert d["status"] == "deleted" and d["expandable"] is False
    assert d["hunks"][0]["lines"] == [["-", 1, None, "x"], ["-", 2, None, "y"]]


def test_no_newline_after_a_context_line_sets_both_sides(setup):
    client, *_ = setup
    f = _file(client, "a/Context.java")
    assert f["no_newline"] == {"old": True, "new": True}
    assert f["hunks"][0]["lines"][-1] == [" ", 2, 2, "end"]


def test_cp949_and_crlf_and_the_5000_character_cut(setup):
    client, *_ = setup
    ko = _file(client, "a/Ko.java")
    assert ko["hunks"][0]["lines"] == [["-", 1, None, "// 한글"], ["+", None, 1, "// 한국어 주석"]]
    assert ko["hunks"][0]["section"] == "class K"
    crlf = _file(client, "a/Crlf.java")
    assert [ln[3] for ln in crlf["hunks"][0]["lines"]] == ["a", "b", "c"]
    long = _file(client, "a/Long.java")
    text = long["hunks"][0]["lines"][0][3]
    assert len(text) == 5001 and text == "x" * 5000 + "…"


def test_omitted_entries_answer_empty_hunks_with_their_fields(setup):
    client, *_ = setup
    for path, omitted in (("a/bin.png", "binary"), ("a/big.txt", "too_large"), ("a/cap.txt", "cap"),
                          ("a/Moved.java", "")):
        f = _file(client, path)
        assert f["hunks"] == [] and f["omitted"] == omitted and f["path"] == path, path
        assert f["no_newline"] == {"old": False, "new": False}
    assert _file(client, "a/bin.png")["binary"] is True
    mv = _file(client, "a/Moved.java")
    assert mv["old_path"] == "b/Moved.java" and mv["changed"] is False and mv["expandable"] is True
    assert _file(client, "a/big.txt")["added"] == 9000


def test_damaged_ranges_are_no_hunks_never_a_500(setup, caplog):
    client, *_ = setup
    with caplog.at_level(logging.WARNING, logger="sbmig.web.codediff"):
        for path in ("a/Damaged.java", "a/Past.java"):
            f = _file(client, path)
            assert f["hunks"] == [] and f["omitted"] == "unreadable", path
            _file(client, path)                                      # asked again: still logged once
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2


def test_expandable_false_without_zip_member_or_for_binary(setup):
    client, rd, _ = setup
    assert _file(client, "a/NoMember.java")["expandable"] is False
    assert _file(client, "a/bin.png")["expandable"] is False
    assert _file(client, "../evil.txt")["expandable"] is False      # a traversal path in the index
    assert _file(client, "a/Crlf.java")["expandable"] is True
    (rd / "result.zip").unlink()
    assert _file(client, "a/Crlf.java")["expandable"] is False
    assert _file(client, "a/Crlf.java")["hunks"]                    # the hunks do not need the ZIP


def test_lines_normal_span_defaults_clamping_and_cap(setup):
    client, *_ = setup
    r = _lines(client, "src/Mod.java", start=10, end=12)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json() == {"start": 10, "end": 12, "total": MOD_LINES, "lines": ["line 10", "line 11", "line 12"]}
    d = _lines(client, "src/Mod.java").json()                       # defaults: 1 … 200
    assert (d["start"], d["end"], len(d["lines"]), d["lines"][0]) == (1, 200, 200, "line 1")
    d = _lines(client, "src/Mod.java", start=50).json()             # end defaults to start + 199
    assert (d["start"], d["end"]) == (50, 249)
    d = _lines(client, "src/Mod.java", start=-5, end=2).json()      # clamped at the top
    assert d == {"start": 1, "end": 2, "total": MOD_LINES, "lines": ["line 1", "line 2"]}
    d = _lines(client, "src/Mod.java", start=MOD_LINES - 1, end=MOD_LINES + 50).json()     # and at the bottom
    assert d["lines"] == [f"line {MOD_LINES - 1}", f"line {MOD_LINES}"] and d["end"] == MOD_LINES
    d = _lines(client, "src/Mod.java", start=1, end=MOD_LINES).json()                     # the per-call cap
    assert (d["start"], d["end"], len(d["lines"])) == (1, 5000, 5000) and d["lines"][-1] == "line 5000"
    d = _lines(client, "src/Mod.java", start=MOD_LINES + 10).json()                       # past the end: empty
    assert d == {"start": MOD_LINES + 10, "end": MOD_LINES + 9, "total": MOD_LINES, "lines": []}
    d = _lines(client, "src/Mod.java", start=20, end=10).json()                           # end before start: empty
    assert d["lines"] == [] and d["end"] == 19


def test_lines_bad_numbers_are_400(setup):
    client, *_ = setup
    for q in ({"start": "abc"}, {"end": "1.5"}, {"start": "1e3"}, {"end": "9" * 40}):
        r = _lines(client, "src/Mod.java", **q)
        assert r.status_code == 400 and "whole number" in r.json()["error"], q


def test_lines_decode_like_the_hunks(setup):
    client, *_ = setup
    assert _lines(client, "a/Ko.java").json()["lines"] == ["// 한국어 주석"]
    assert _lines(client, "a/Crlf.java").json() == {"start": 1, "end": 2, "total": 2, "lines": ["a", "c"]}
    assert _lines(client, "a/Added.java").json()["lines"] == ["line1", "line2"]     # no final newline
    assert _lines(client, "a/Long.java", start=2, end=2).json()["lines"][0].endswith("…")


def test_lines_404s(setup, monkeypatch):
    client, rd, _ = setup
    for path, reason in (("gone/Deleted.java", "deleted"), ("a/bin.png", "binary"), ("../evil.txt", "unsafe path"),
                         ("a/NoMember.java", "not in result.zip")):
        r = _lines(client, path)
        assert r.status_code == 404 and reason in r.json()["reason"], path
    monkeypatch.setattr(codediff, "MAX_MEMBER_BYTES", 100)
    r = _lines(client, "src/Mod.java")
    assert r.status_code == 404 and "larger than" in r.json()["reason"]
    assert _file(client, "src/Mod.java")["expandable"] is False
    monkeypatch.undo()
    (rd / "result.zip").write_bytes(b"PK not a zip")
    codediff.clear_caches()
    r = _lines(client, "src/Mod.java")
    assert r.status_code == 404 and r.json()["reason"] == "result.zip not available"
    (rd / "result.zip").unlink()
    assert _lines(client, "src/Mod.java").status_code == 404


def test_a_traversal_zip_root_is_refused(setup):
    client, rd, index = setup
    index = {**index, "zip_root": "../.."}
    (rd / "codediff" / "index.json").write_text(json.dumps(index), encoding="utf-8")
    codediff.clear_caches()
    r = _lines(client, "src/Mod.java")
    assert r.status_code == 404 and "unsafe path" in r.json()["reason"]
    assert _file(client, "src/Mod.java")["expandable"] is False


def test_route_404s(setup):
    client, rd, _ = setup
    assert client.get("/api/runs/nope/codediff").status_code == 404
    assert client.get("/api/runs/nope/codediff/files/0").status_code == 404
    assert client.get("/api/runs/bad..id!/codediff").status_code in (400, 404)
    r = client.get("/api/runs/.hidden/codediff")
    assert r.status_code == 400 and r.json() == {"error": "bad run id"}
    for n in (str(len(FILES)), "-1", "x", "99999999999"):
        r = client.get(f"/api/runs/{RUN}/codediff/files/{n}")
        assert r.status_code == 404 and r.json()["error"] == "no such file in the code diff", n
        assert client.get(f"/api/runs/{RUN}/codediff/files/{n}/lines").status_code == 404
    # wrong version
    raw = json.loads((rd / "codediff" / "index.json").read_text(encoding="utf-8"))
    (rd / "codediff" / "index.json").write_text(json.dumps({**raw, "version": 2}), encoding="utf-8")
    codediff.clear_caches()
    for url in ("codediff", "codediff/files/0", "codediff/files/0/lines"):
        r = client.get(f"/api/runs/{RUN}/{url}")
        assert r.status_code == 404 and r.json()["error"] == "code diff not available", url
    # unreadable, then missing
    (rd / "codediff" / "index.json").write_text("{not json", encoding="utf-8")
    codediff.clear_caches()
    assert client.get(f"/api/runs/{RUN}/codediff").json()["error"] == "code diff not available"
    (rd / "codediff" / "index.json").unlink()
    r = client.get(f"/api/runs/{RUN}/codediff")
    assert r.status_code == 404 and r.json()["error"] == "code diff not available"


def test_run_without_artifact(tmp_path):
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    client = TestClient(create_app(tmp_path / "out", repo))
    (tmp_path / "out" / "plain").mkdir(parents=True)
    r = client.get("/api/runs/plain/codediff")
    assert r.status_code == 404 and r.json()["error"] == "code diff not available"
    assert "code_diff" not in client.get("/api/runs/plain").json()


def test_409_while_the_final_pass_is_pending_and_status_follows(setup):
    client, rd, _ = setup
    st = client.get(f"/api/runs/{RUN}").json()
    assert st["code_diff"]["files"] == len(FILES) and st["code_diff"]["moved_only"] == 1
    state = contract.ControlState(run_id=RUN, state=contract.RunState.RUNNING.value)
    state.write(rd)
    for url in ("codediff", "codediff/files/0", "codediff/files/0/lines"):
        r = client.get(f"/api/runs/{RUN}/{url}")
        assert r.status_code == 409 and "once the run has finished" in r.json()["error"], url
    assert "code_diff" not in client.get(f"/api/runs/{RUN}").json()
    state.transition(contract.RunState.FINISHED, exit_code=0).write(rd)
    assert client.get(f"/api/runs/{RUN}/codediff").status_code == 200
    assert "code_diff" in client.get(f"/api/runs/{RUN}").json()


def test_status_survives_a_broken_summary(setup):
    client, rd, _ = setup
    s = rd / "codediff" / "summary.json"
    for body in ("{broken", "[]", json.dumps({"version": 2, "summary": {"files": 1}}), json.dumps({"version": 1}),
                 json.dumps({"version": 1, "summary": "x"})):
        s.write_text(body, encoding="utf-8")
        st = client.get(f"/api/runs/{RUN}")
        assert st.status_code == 200 and "code_diff" not in st.json(), body
    s.write_bytes(b"\xff\xfe\x00")
    assert "code_diff" not in client.get(f"/api/runs/{RUN}").json()
    s.unlink()
    assert "code_diff" not in client.get(f"/api/runs/{RUN}").json()
    # a summary without its index.json is not served either
    _build(rd)
    (rd / "codediff" / "index.json").unlink()
    assert "code_diff" not in client.get(f"/api/runs/{RUN}").json()


def test_index_is_parsed_once_per_file_version(setup, monkeypatch):
    client, rd, _ = setup
    calls = []
    real = codediff._index_cache.put
    monkeypatch.setattr(codediff._index_cache, "put", lambda *a: calls.append(1) or real(*a))
    for n in range(5):
        client.get(f"/api/runs/{RUN}/codediff/files/{n}")
    assert len(calls) == 1
    raw = json.loads((rd / "codediff" / "index.json").read_text(encoding="utf-8"))
    raw["files"][0]["path"] = "../evil2.txt"
    (rd / "codediff" / "index.json").write_text(json.dumps(raw) + "\n", encoding="utf-8")   # another size
    assert client.get(f"/api/runs/{RUN}/codediff/files/0").json()["path"] == "../evil2.txt"


def test_parse_hunks_unit():
    hunks, nn = codediff.parse_hunks(b"@@ -1 +1 @@\n-\xff\n+ok\n", "no-such-encoding")
    assert hunks[0]["lines"] == [["-", 1, None, "�"], ["+", None, 1, "ok"]] and nn == {"old": False, "new": False}
    with pytest.raises(ValueError):
        codediff.parse_hunks(b"diff --git a b\n@@ -1 +1 @@\n")
    with pytest.raises(ValueError):
        codediff.parse_hunks(b"@@ -1 +1 @@\n?what\n")
    with pytest.raises(ValueError):
        codediff.parse_hunks(b"")


def test_token_guard_covers_the_codediff_routes(tmp_path):
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    token = "s3cret-token-for-tests"
    client = TestClient(create_app(tmp_path / "out", repo, token=token))
    rd = tmp_path / "out" / RUN
    rd.mkdir(parents=True)
    _build(rd)
    for url in ("codediff", "codediff/files/0", "codediff/files/0/lines"):
        assert client.get(f"/api/runs/{RUN}/{url}").status_code == 401, url
    ok = client.get(f"/api/runs/{RUN}/codediff", headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200
