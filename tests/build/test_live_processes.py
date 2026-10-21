"""Shutdown must not leave Maven / OpenRewrite children behind (company run 2026-10-21: the uvicorn shutdown hung
and a java.exe survived it on Windows)."""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

from sbmig.build.mvn import kill_live_processes, live_processes, run_logged


def test_run_logged_registers_the_child_and_kill_live_processes_ends_it(tmp_path: Path):
    cmd = [sys.executable, "-c", "import time; print('up', flush=True); time.sleep(30)"]
    out: dict = {}

    def runner():
        out["res"] = run_logged(cmd, cwd=tmp_path, env={}, log_path=tmp_path / "sleep.log", timeout_s=None)

    th = threading.Thread(target=runner, daemon=True)
    th.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not live_processes():
        time.sleep(0.05)
    assert len(live_processes()) == 1                       # registered while it runs
    t0 = time.monotonic()
    assert kill_live_processes("test shutdown") == 1
    th.join(timeout=15)
    assert not th.is_alive() and time.monotonic() - t0 < 15
    assert out["res"].exit_code != 0 and not out["res"].timed_out
    assert live_processes() == []                           # unregistered on exit
    assert kill_live_processes("nothing left") == 0


def test_finished_child_is_unregistered(tmp_path: Path):
    res = run_logged([sys.executable, "-c", "print('hi')"], cwd=tmp_path, env={}, log_path=tmp_path / "hi.log",
                     timeout_s=30)
    assert res.exit_code == 0 and "hi" in res.text and live_processes() == []
