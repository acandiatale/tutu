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
