"""Round 9c: workspace git is never interactive (a global commit.gpgsign / hook / credential prompt hung a run on
Windows) and has a hard timeout."""
from __future__ import annotations

import subprocess

import pytest

from sbmig.core import workspace as wsmod
from sbmig.core.workspace import GitError, Workspace


@pytest.fixture()
def ws(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    return Workspace(root, tmp_path / "run", "t")


def test_git_calls_disable_prompts_hooks_and_signing(ws, monkeypatch):
    seen: dict = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return subprocess.CompletedProcess(cmd, 0, stdout="abc123\n", stderr="")

    monkeypatch.setattr(wsmod.subprocess, "run", fake_run)
    assert ws._git("rev-parse", "HEAD").stdout.strip() == "abc123"
    cmd, kw = seen["cmd"], seen["kw"]
    assert cmd[0] == "git" and cmd[-2:] == ["rev-parse", "HEAD"]
    assert "commit.gpgsign=false" in cmd and any(c.startswith("core.hooksPath=") for c in cmd)
    assert kw["stdin"] is subprocess.DEVNULL and kw["timeout"] == wsmod.GIT_TIMEOUT_S
    env = kw["env"]
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_EDITOR"] == "true" and env["GCM_INTERACTIVE"] == "never"


def test_git_timeout_is_a_clear_error(ws, monkeypatch):
    def hang(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 0))

    monkeypatch.setattr(wsmod.subprocess, "run", hang)
    monkeypatch.setattr(wsmod, "GIT_TIMEOUT_S", 1.0)
    with pytest.raises(GitError, match="timed out after 1s .* blocking git"):
        ws._git("status", "--porcelain")


def test_git_output_is_decoded_as_utf8_not_the_console_code_page(ws, monkeypatch):
    """Company run 2026-10-21 (Windows, cp949 console): `text=True` decoded git's UTF-8 output with cp949, the
    decode died in subprocess's reader thread and `stdout` came back as None → "could not write changes.patch:
    data must be str, not NoneType". git now runs in binary mode and sbmig decodes."""
    seen: dict = {}

    def fake_run(cmd, **kw):
        seen["kw"] = kw
        return subprocess.CompletedProcess(cmd, 0, stdout="가 ".encode("utf-8") + b"\xb0\xa1\n", stderr=b"")

    monkeypatch.setattr(wsmod.subprocess, "run", fake_run)
    out = ws._git("status").stdout
    assert "text" not in seen["kw"] and "encoding" not in seen["kw"]        # binary: no locale decoding anywhere
    assert out.startswith("가 ") and out.endswith("\n") and "�" in out  # cp949 bytes → replacement, no crash
    assert ws.diff_bytes("abc") == "가 ".encode("utf-8") + b"\xb0\xa1\n"      # raw bytes for the patch

    def dead_reader(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout=None, stderr=None)

    monkeypatch.setattr(wsmod.subprocess, "run", dead_reader)
    assert ws._git("status").stdout == "" and ws.diff_bytes("abc") == b""


def test_write_patch_keeps_legacy_encoded_bytes(ws):
    """An MS949 source in the diff stays byte-exact in changes.patch (it is not re-encoded as UTF-8)."""
    import shutil
    if shutil.which("git") is None:  # pragma: no cover - CI images without git
        pytest.skip("git not installed")
    (ws.root / "a.txt").write_text("hello\n", encoding="utf-8")
    ws.git_init()
    (ws.root / "Legacy.java").write_bytes("// 한글 주석\nclass Legacy {}\n".encode("cp949"))
    ws.commit("legacy file")
    patch = ws.write_patch()
    data = patch.read_bytes()
    assert "한글 주석".encode("cp949") in data and b"Legacy.java" in data
    assert ws.diff(ws.first_commit()).count("�") > 0 or "Legacy" in ws.diff(ws.first_commit())


# ---------------------------------------------------------------------------------------------- round 10 (B3)
def test_git_calls_carry_windows_safe_config(ws, monkeypatch):
    seen: dict = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(wsmod.subprocess, "run", fake_run)
    ws._git("status", "--porcelain")
    cmd = seen["cmd"]
    pairs = [cmd[i + 1] for i, c in enumerate(cmd) if c == "-c"]
    for kv in ("core.longpaths=true", "core.autocrlf=false", "core.quotepath=false", "safe.directory=*",
               "commit.gpgsign=false"):
        assert kv in pairs
    assert cmd[-2:] == ["status", "--porcelain"] and seen["kw"]["creationflags"] == 0
    monkeypatch.setattr(wsmod, "_on_windows", lambda: True)
    ws._git("status")
    assert seen["kw"]["creationflags"] == 0x08000000          # CREATE_NO_WINDOW: Ctrl+Break never reaches git


def _real_git():
    import shutil
    if shutil.which("git") is None:  # pragma: no cover
        pytest.skip("git not installed")


def test_korean_file_names_are_listed_verbatim(ws):
    _real_git()
    (ws.root / "pom.xml").write_text("<project/>")
    ws.git_init()
    base = ws.head()
    d = ws.root / "src/main/resources/한글"
    d.mkdir(parents=True)
    (d / "메시지.properties").write_text("a=b\n", encoding="utf-8")
    (ws.root / "pom.xml").write_text("<project><x/></project>")
    assert ws.changed_files_since(base) == ["pom.xml", "src/main/resources/한글/메시지.properties"]
    sha = ws.commit("k")
    assert ws.changed_between(base, sha) == ["pom.xml", "src/main/resources/한글/메시지.properties"]
    assert "한글/메시지.properties" in ws.diff(base, sha, stat=True)


def test_revert_keeps_line_endings_despite_global_autocrlf_and_gitattributes(ws, tmp_path, monkeypatch):
    """Windows: a global core.autocrlf=true (Git for Windows default) or a project `* text=auto eol=crlf` rewrote
    LF files with CRLF on `git reset --hard` (revert_to) — `run.sh` then failed with `$'\\r': command not found`."""
    _real_git()
    gcfg = tmp_path / "global.gitconfig"
    gcfg.write_text("[core]\n\tautocrlf = true\n\teol = crlf\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gcfg))
    (ws.root / ".gitattributes").write_text("* text=auto eol=crlf\n")
    sh = b"#!/bin/sh\necho ok\n"
    bat = b"@echo off\r\necho ok\r\n"
    (ws.root / "run.sh").write_bytes(sh)
    (ws.root / "run.bat").write_bytes(bat)
    ws.git_init()
    assert (ws.root / ".git/info/attributes").read_text().startswith("* -text")
    snap = ws.head()
    (ws.root / "run.sh").write_bytes(b"#!/bin/sh\necho changed\n")
    (ws.root / "run.bat").unlink()
    ws.commit("change")
    ws.revert_to(snap)
    assert (ws.root / "run.sh").read_bytes() == sh and (ws.root / "run.bat").read_bytes() == bat


def test_jars_the_project_gitignore_hides_are_still_snapshotted(tmp_path):
    """Round 10: `*.jar` in the project's .gitignore kept WEB-INF/lib jars out of the snapshots (patch, revert)."""
    from sbmig.core.workspace import Workspace

    src = tmp_path / "src-proj"
    (src / "src/main/webapp/WEB-INF/lib").mkdir(parents=True)
    (src / "src/main/webapp/WEB-INF/lib/legacy.jar").write_bytes(b"PK\x03\x04jar")
    (src / "lib").mkdir()
    (src / "lib/vendor.jar").write_bytes(b"PK\x03\x04v")
    (src / "logs").mkdir()
    (src / "logs/app.log").write_text("x")
    (src / ".gitignore").write_text("*.jar\nlogs/\n")
    (src / "pom.xml").write_text("<project/>")
    ws = Workspace(src, tmp_path / "run", "r1")
    ws.git_init()
    tracked = ws._git("ls-files").stdout.splitlines()
    assert "src/main/webapp/WEB-INF/lib/legacy.jar" in tracked and "lib/vendor.jar" in tracked
    assert "logs/app.log" not in tracked                      # only jars are forced in
    base = ws.head()
    jar = ws.root / "src/main/webapp/WEB-INF/lib/legacy.jar"
    jar.unlink()
    ws.commit("moved away")
    ws.revert_to(base)
    assert jar.is_file()                                       # revert brings the jar back


def test_commit_paths_stages_only_the_given_files_and_build_products_are_set_aside(tmp_path: Path):
    """v2.1b (company run, 2026-10-27): a build copied a module jar into lib/ (outside target/) and `git add -A` in
    the next LLM attempt committed it — the attempt's `files` named the jar. Attempts stage only their own files,
    and whatever a build dirtied is set aside right after it."""
    src = tmp_path / "proj"
    (src / "lib").mkdir(parents=True)
    (src / "pom.xml").write_text("<project/>")
    (src / "lib" / "app-1.0.jar").write_bytes(b"PK\x03\x04old")
    ws = Workspace.from_input(src, tmp_path / "out", run_id="r1")
    base = ws.git_init()
    # a build rewrites the checked-in jar and creates a new one outside target/
    before = ws.status_paths()
    (ws.root / "lib" / "app-1.0.jar").write_bytes(b"PK\x03\x04new")
    (ws.root / "lib" / "app-1.0-SNAPSHOT.jar").write_bytes(b"PK\x03\x04built")
    (ws.root / "target").mkdir(exist_ok=True)
    (ws.root / "target" / "x.class").write_bytes(b"\xca\xfe")
    assert sorted(ws.restore_build_byproducts(before)) == ["lib/app-1.0-SNAPSHOT.jar", "lib/app-1.0.jar"]
    assert (ws.root / "lib" / "app-1.0.jar").read_bytes().endswith(b"old")          # tracked → restored
    assert not ws.is_dirty()                                                        # untracked → excluded
    # an attempt changes pom.xml only; a stray edit elsewhere is not part of its commit
    ws.write("pom.xml", "<project><x/></project>")
    ws.write("README.md", "stray")
    sha = ws.commit("fix", paths=["pom.xml"])
    assert sha != base and ws.changed_between(base, sha) == ["pom.xml"]
    assert ws.is_dirty()                                                            # README.md still uncommitted
    assert ws.commit("nothing", paths=[]) == sha                                    # no paths → no commit


# ---------------------------------------------------------------------------------------------- v2.1d (file_at)
def test_file_at_reads_a_file_as_it_was_at_a_commit(ws):
    """`changes_in_commit` reads the pom before and after a try with `git show <sha>:<path>`."""
    _real_git()
    (ws.root / "m").mkdir()
    (ws.root / "m" / "pom.xml").write_text("<project><version>1</version></project>\n", encoding="utf-8")
    base = ws.git_init()
    (ws.root / "m" / "pom.xml").write_text("<project><version>2</version><!-- 한글 --></project>\n", encoding="utf-8")
    (ws.root / "m" / "새파일.txt").write_text("new\n", encoding="utf-8")
    sha = ws.commit("bump")
    assert ws.file_at(base, "m/pom.xml") == "<project><version>1</version></project>\n"
    assert ws.file_at(sha, "m/pom.xml") == "<project><version>2</version><!-- 한글 --></project>\n"
    assert ws.file_at(sha, "m\\pom.xml") == ws.file_at(sha, "m/pom.xml")     # a Windows-style path works too
    assert ws.file_at(sha, "m/새파일.txt") == "new\n"
    assert ws.file_at(base, "m/새파일.txt") is None                          # did not exist there
    assert ws.file_at("0" * 40, "m/pom.xml") is None                        # unknown commit: None, no raise
    (ws.root / "m" / "legacy.properties").write_bytes("k=한글\n".encode("cp949"))
    sha2 = ws.commit("legacy")
    assert "�" in ws.file_at(sha2, "m/legacy.properties")              # UTF-8 with replacement characters
