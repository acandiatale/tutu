"""codediff: the code-change record lists only files the migration itself changed (review finding R3).

Leftovers of the final build that are neither tracked nor ignored (a shade plugin's `dependency-reduced-pom.xml`,
`node/`, `node_modules/`, jars copied beside the sources) are not "added" files; uncommitted changes to tracked files
stay; a file the migration created AND committed is listed as added."""
from __future__ import annotations

import json

from sbmig.report.codediff import write_code_diff

from .test_codediff import by_path, events, load, make_ws, put


def test_untracked_build_leftovers_are_not_listed(tmp_path, cfg):
    ws = make_ws(tmp_path, {"pom.xml": "<project/>\n", "web/pom.xml": "<project/>\n", "src/A.java": "class A {}\n"})
    put(ws, "src/A.java", "class A { int x; }\n")
    ws.commit("fix: A")
    put(ws, "dependency-reduced-pom.xml", "<project><x/></project>\n")
    put(ws, "web/node/node", b"\x7fELF binary")
    put(ws, "web/node_modules/left-pad/index.js", "module.exports = 1;\n")
    put(ws, "web/lib/web-1.0.jar", b"PK\x03\x04jar")
    put(ws, "src/main/webapp/WEB-INF/lib/copied.jar", b"PK\x03\x04jar2")
    write_code_diff(ws, cfg)
    index, _ = load(ws)
    assert [f["path"] for f in index["files"]] == ["src/A.java"]
    assert index["summary"]["added"] == 0
    [ev] = events(ws, "code_diff")
    assert ev["untracked_left_out"] == 5


def test_uncommitted_change_to_a_tracked_file_stays(tmp_path, cfg):
    ws = make_ws(tmp_path, {"pom.xml": "<project/>\n", "a.txt": "a\n", "gone.txt": "g\n"})
    put(ws, "a.txt", "b\n")
    (ws.root / "gone.txt").unlink()
    write_code_diff(ws, cfg)
    index, _ = load(ws)
    assert by_path(index, "a.txt")["status"] == "modified" and by_path(index, "gone.txt")["status"] == "deleted"
    assert "untracked_left_out" not in events(ws, "code_diff")[0]


def test_a_file_the_migration_created_and_committed_is_added(tmp_path, cfg):
    ws = make_ws(tmp_path, {"pom.xml": "<project/>\n"})
    put(ws, "app-boot/pom.xml", "<project><artifactId>app-boot</artifactId></project>\n")
    put(ws, "app-boot/lib/vendor.jar", b"PK\x03\x04vendor")          # a bundled jar wired by the restructure
    ws.commit("restructure: boot module")
    write_code_diff(ws, cfg)
    index, _ = load(ws)
    assert by_path(index, "app-boot/pom.xml")["status"] == "added"
    assert by_path(index, "app-boot/lib/vendor.jar")["status"] == "added"
    summary = json.loads((ws.run_dir / "codediff" / "summary.json").read_text(encoding="utf-8"))
    assert summary["summary"]["added"] == 2
