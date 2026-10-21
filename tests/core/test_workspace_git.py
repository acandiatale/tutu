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
