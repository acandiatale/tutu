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
