"""Round 9 (operators): a long mvn / rewrite run logs "still running" lines so a slow build is not mistaken for a hang."""
from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from sbmig.build import mvn


@pytest.mark.skipif(os.name == "nt", reason="uses sh")
def test_run_logged_heartbeat_and_timeout(tmp_path: Path, monkeypatch, caplog):
    monkeypatch.setattr(mvn, "HEARTBEAT_S", 0.1)
    log_path = tmp_path / "logs" / "build.log"
    with caplog.at_level(logging.INFO, logger="sbmig.build"):
        res = mvn.run_logged(["sh", "-c", "echo a; sleep 0.35; echo b"], cwd=tmp_path, env=dict(os.environ),
                             log_path=log_path, timeout_s=10)
    assert res.exit_code == 0 and not res.timed_out and "b" in res.text
    beats = [r for r in caplog.records if "still running" in r.getMessage()]
    assert beats and "sh still running:" in beats[0].getMessage() and str(log_path) in beats[0].getMessage()
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="sbmig.build"):
        res = mvn.run_logged(["sh", "-c", "sleep 5"], cwd=tmp_path, env=dict(os.environ),
                             log_path=tmp_path / "logs" / "slow.log", timeout_s=0.3)
    assert res.timed_out and res.exit_code != 0
    assert any("timeout after" in r.getMessage() for r in caplog.records)



def test_windows_timeout_kills_the_whole_tree(monkeypatch):
    """Round 9c: on Windows a timed-out mvn.cmd must take its java.exe child with it (taskkill /T /F), or the orphan
    keeps the project files locked for the next steps."""
    calls: list[list[str]] = []

    class P:
        pid = 4242
        returncode = None

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(mvn, "IS_WINDOWS", True)
    monkeypatch.setattr(mvn.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or None)
    mvn._kill_group(P())
    assert calls and calls[0][:4] == ["taskkill", "/PID", "4242", "/T"] and "/F" in calls[0]
