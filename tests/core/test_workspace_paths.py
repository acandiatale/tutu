"""Round 10: workspace paths are POSIX on every OS (B2), only Maven build output named `target` is excluded (B4),
build-generated files outside target/ never make a fix attempt "dirty" (B8), the encoding cache is bounded (B7)."""
from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path, PureWindowsPath

import pytest

from sbmig.core import workspace as wsmod
from sbmig.core.workspace import Workspace, _relative_posix, is_build_output_path, posix_rel

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


# ------------------------------------------------------------------------------------------------ B2 POSIX paths
def test_relative_posix_on_windows_paths():
    root = PureWindowsPath(r"C:\out\run-1\project")
    assert _relative_posix(PureWindowsPath(r"C:\out\run-1\project\src\main\java\A.java"), root) == \
        "src/main/java/A.java"
    assert _relative_posix(PureWindowsPath(r"c:\OUT\run-1\Project\pom.xml"), root) == "pom.xml"   # case-insensitive
    assert _relative_posix(PureWindowsPath(r"D:\other\A.java"), root) is None
    assert posix_rel(PureWindowsPath(r"module\src\main\java\A.java")) == "module/src/main/java/A.java"


def _tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


TREE = {
    "pom.xml": "<project/>",
    "src/main/java/com/x/target/A.java": "package com.x.target; class A {}",
    "src/main/java/com/x/B.java": "class B {}",
    "target/classes/com/x/B.class": "x",
    "target/generated-sources/G.java": "class G {}",
    "module/pom.xml": "<project/>",
    "module/src/main/java/m/M.java": "class M {}",
    "module/target/classes/m/M.class": "x",
    "module/target/m.jar": "x",
    "APP/com/x/target/Legacy.java": "class Legacy {}",       # non-standard source root: no pom.xml beside it
    "docs/target/readme.txt": "kept",
    "src/it/p1/pom.xml": "<project/>",
    "src/it/p1/target/out.txt": "under src: kept",
    "web/한글/페이지.jsp": "<%@ page %>",
}


def test_rel_glob_and_walk_return_posix_paths(tmp_path):
    root = _tree(tmp_path / "project", TREE)
    ws = Workspace(root, tmp_path / "run", "t")
    assert ws.rel(root / "src" / "main" / "java" / "com" / "x" / "B.java") == "src/main/java/com/x/B.java"
    assert ws.rel(Path("module") / "pom.xml") == "module/pom.xml"
    assert ws.rel("/elsewhere/x.txt") == "/elsewhere/x.txt"
    for rel in ws.glob("*"):
        assert "\\" not in rel
    assert ws.glob("*.jsp") == ["web/한글/페이지.jsp"]


def test_windows_backslash_rel_dir_is_posix(monkeypatch):
    """os.walk / os.path.relpath give "a\\b" on Windows; walk() hands out "a/b"."""
    monkeypatch.setattr(wsmod.os, "sep", "\\")
    assert posix_rel("module\\src\\main") == "module/src/main"


# ------------------------------------------------------------------------------------------------ B4 target rule
def test_is_build_output_path_rule(tmp_path):
    root = _tree(tmp_path / "p", TREE)
    yes = ["target", "target/classes/com/x/B.class", "module/target/m.jar", "module\\target\\classes\\m\\M.class"]
    no = ["src/main/java/com/x/target/A.java", "APP/com/x/target/Legacy.java", "docs/target/readme.txt",
          "src/it/p1/target/out.txt", "module/src/main/java/m/M.java", "targets/x", "pom.xml"]
    for rel in yes:
        assert is_build_output_path(rel, root), rel
    for rel in no:
        assert not is_build_output_path(rel, root), rel
    # without a root only the src/ rule applies
    assert is_build_output_path("APP/com/x/target/Legacy.java") and not is_build_output_path("a/src/b/target/C.java")


def test_leftover_output_without_pom_is_still_output(tmp_path):
    """A module whose pom.xml the restructure moved away keeps its old target/: Maven's bookkeeping dirs mark it."""
    root = _tree(tmp_path / "p", {"pom.xml": "<project/>", "old-mod/target/maven-status/x.lst": "x",
                                  "old-mod/target/generated-sources/annotations/QFoo.java": "class QFoo {}",
                                  "src/main/java/a/target/maven-status/odd.txt": "under src: source"})
    ws = Workspace(root, tmp_path / "run", "t")
    assert ws.is_build_output("old-mod/target/generated-sources/annotations/QFoo.java")
    assert ws.java_files() == []
    assert ws.glob("*.txt") == ["src/main/java/a/target/maven-status/odd.txt"]


def test_intake_keeps_source_packages_named_target(tmp_path):
    src = _tree(tmp_path / "asis", TREE)
    ws = Workspace.from_input(src, tmp_path / "out", run_id="r")
    have = {p.relative_to(ws.root).as_posix() for p in ws.root.rglob("*") if p.is_file()}
    assert "src/main/java/com/x/target/A.java" in have and "APP/com/x/target/Legacy.java" in have
    assert "docs/target/readme.txt" in have and "src/it/p1/target/out.txt" in have
    assert not any(h.startswith(("target/", "module/target/")) for h in have)
    # the same tree as a zip (single top folder)
    z = tmp_path / "asis.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for rel, text in TREE.items():
            zf.writestr(f"asis/{rel}", text)
    wz = Workspace.from_input(z, tmp_path / "out", run_id="z")
    assert (wz.root / "src/main/java/com/x/target/A.java").is_file() and not (wz.root / "module/target").exists()


def test_glob_and_java_files_see_target_packages(tmp_path):
    ws = Workspace(_tree(tmp_path / "p", TREE), tmp_path / "run", "t")
    assert ws.java_files() == ["APP/com/x/target/Legacy.java", "module/src/main/java/m/M.java",
                               "src/main/java/com/x/B.java", "src/main/java/com/x/target/A.java"]
    assert "target/generated-sources/G.java" not in ws.java_files()
    assert ws.pom_files() == ["module/pom.xml", "pom.xml", "src/it/p1/pom.xml"]
    assert ws.glob("target") == ["APP/com/x/target", "docs/target", "src/it/p1/target",
                                 "src/main/java/com/x/target"]
    assert ws.is_build_output("module/target/m.jar") and not ws.is_build_output(ws.root / "docs/target/readme.txt")


def test_zip_result_keeps_target_packages_drops_build_output(tmp_path):
    ws = Workspace(_tree(tmp_path / "project", TREE), tmp_path / "run", "t")
    with zipfile.ZipFile(ws.zip_result()) as zf:
        names = set(zf.namelist())
    assert "project/src/main/java/com/x/target/A.java" in names and "project/APP/com/x/target/Legacy.java" in names
    assert "project/web/한글/페이지.jsp" in names
    assert not any(n.startswith(("project/target/", "project/module/target/", "project/.git/")) for n in names)


def _git_ls(root: Path) -> set[str]:
    out = subprocess.run(["git", "-c", "core.quotepath=false", "ls-files", "-z"], cwd=root, capture_output=True,
                         check=True).stdout.decode("utf-8")
    return {x for x in out.split("\0") if x}


@needs_git
def test_gitignore_tracks_target_packages_not_build_output(tmp_path):
    ws = Workspace(_tree(tmp_path / "project", TREE), tmp_path / "run", "t")
    ws.git_init()
    ignore = (ws.root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ignore[:3] == ["target/", "!**/src/**/target/", "!**/src/**/target/**"]
    assert "!/APP/com/x/target/" in ignore and "!/docs/target/" in ignore and "!/docs/target/**" in ignore
    tracked = _git_ls(ws.root)
    assert {"src/main/java/com/x/target/A.java", "APP/com/x/target/Legacy.java", "docs/target/readme.txt",
            "src/it/p1/target/out.txt", "web/한글/페이지.jsp"} <= tracked
    assert not any(t.startswith(("target/", "module/target/")) for t in tracked)
    # a build writes module/target/classes and a new class lands in a `target` package: only the latter counts
    (ws.root / "module/target/classes/m/New.class").write_text("x")
    assert not ws.is_dirty()
    new_src = ws.root / "module/src/main/java/m/target/T.java"
    new_src.parent.mkdir(parents=True)
    new_src.write_text("class T {}")
    assert ws.is_dirty() and ws.changed_files_since(ws.head()) == ["module/src/main/java/m/target/T.java"]
    # revert_to cleans the untracked source but keeps build output
    ws.revert_to(ws.head())
    assert not new_src.exists() and (ws.root / "module/target/classes/m/New.class").exists()


@needs_git
def test_project_gitignore_with_target_globs_still_tracks_target_packages(tmp_path):
    files = dict(TREE)
    files[".gitignore"] = "**/target/**\ntarget\n*.log\n"
    ws = Workspace(_tree(tmp_path / "project", files), tmp_path / "run", "t")
    ws.git_init()
    tracked = _git_ls(ws.root)
    assert {"src/main/java/com/x/target/A.java", "APP/com/x/target/Legacy.java"} <= tracked
    assert not any(t.startswith(("target/", "module/target/")) for t in tracked)


# ------------------------------------------------------------------------------------------------ B8 generated
@needs_git
def test_generated_files_outside_target_are_ignored_unless_in_the_as_is_tree(tmp_path):
    ws = Workspace(_tree(tmp_path / "project", {"pom.xml": "<project/>", "src/main/java/A.java": "class A {}",
                                                 "m/pom.xml": "<project/>"}), tmp_path / "run", "t")
    ws.git_init()
    ignore = (ws.root / ".gitignore").read_text(encoding="utf-8").splitlines()
    for line in (".flattened-pom.xml", "**/src/main/generated/", "**/src/test/generated/", ".apt_generated/"):
        assert line in ignore
    for rel in (".flattened-pom.xml", "m/.flattened-pom.xml", "src/main/generated/com/x/QFoo.java",
                "m/src/test/generated/QBar.java", ".apt_generated/QBaz.java"):
        p = ws.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("generated")
    assert not ws.is_dirty()


@needs_git
def test_as_is_generated_sources_stay_tracked(tmp_path):
    ws = Workspace(_tree(tmp_path / "project", {"pom.xml": "<project/>",
                                                 "src/main/generated/com/x/QFoo.java": "class QFoo {}"}),
                   tmp_path / "run", "t")
    ws.git_init()
    ignore = (ws.root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "**/src/main/generated/" not in ignore and ".flattened-pom.xml" in ignore
    assert "src/main/generated/com/x/QFoo.java" in _git_ls(ws.root)
    (ws.root / "src/main/generated/com/x/QFoo.java").write_text("class QFoo { int changed; }")
    assert ws.is_dirty()


# ------------------------------------------------------------------------------------------------ B7 bounded cache
def test_encoding_cache_is_bounded_lru(tmp_path, monkeypatch):
    cache = wsmod._LRU(3)
    monkeypatch.setattr(wsmod, "_ENCODING_CACHE", cache)
    files = []
    for i in range(5):
        f = tmp_path / f"f{i}.txt"
        f.write_bytes("한글".encode("cp949"))
        files.append(f)
        assert wsmod.detect_encoding(f) == "cp949"
    assert len(cache) == 3
    wsmod.detect_encoding(files[2])                       # touch → most recent
    f5 = tmp_path / "f5.txt"
    f5.write_text("x")
    wsmod.detect_encoding(f5)
    keys = list(cache._d)
    assert str(files[2].resolve()) in keys and str(files[3].resolve()) not in keys and len(keys) == 3
    assert wsmod.ENCODING_CACHE_MAX == 20_000


def test_v21_run_dir_layout(tmp_path):
    """PLAN-v2.1 §3.5: logs/ holds every Maven / OpenRewrite / deptree / effective-pom file; no other directories
    besides project/ (merge-conflicts/ only after a merge that dropped copies)."""
    from sbmig.core.workspace import RUN_DIR_FILES, Workspace, logs_dir
    from sbmig.fleet.cleanup import KEEP_FILES

    ws = Workspace(tmp_path / "p", tmp_path / "run", "run-1")
    assert logs_dir(ws) == ws.logs_dir == ws.run_dir / "logs" and ws.logs_dir.is_dir()

    class View:                                    # a stand-in with only run_dir
        run_dir = tmp_path / "other"
    assert logs_dir(View()) == tmp_path / "other" / "logs"
    assert set(RUN_DIR_FILES) == {"errors.jsonl", "errors.md", "report.md", "report.json", "sbmig.log",
                                  "changes.patch", "journal.jsonl", "plan.json", "profile.json", "result.zip"}
    assert set(RUN_DIR_FILES) == set(KEEP_FILES)
    assert ws.new_log_path("deptree-root").parent == ws.logs_dir
    assert sorted(p.name for p in ws.run_dir.iterdir() if p.is_dir()) == ["logs"]
