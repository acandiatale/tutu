"""Round 10 (B10): stall diagnosis on Windows — no SIGUSR1 there. Ctrl+Break (SIGBREAK) dumps the threads, and a
file named `dump-threads` in the run dir or the start directory does the same from anywhere (also under uvicorn)."""
from __future__ import annotations

import signal
import time

import pytest

from sbmig.core import diag
from sbmig.core.workspace import Workspace


@pytest.fixture(autouse=True)
def _clean_watch_state(monkeypatch):
    monkeypatch.setattr(diag, "_FIXED_DIRS", [])
    monkeypatch.setattr(diag, "_RUN_DIRS", diag.collections.deque(maxlen=diag.MAX_WATCHED_RUN_DIRS))
    monkeypatch.setattr(diag, "_STUCK", {})


def test_trigger_file_writes_dump_next_to_it_and_is_removed(tmp_path):
    (tmp_path / "dump-threads").write_text("")
    written = diag.check_dump_triggers([tmp_path])
    assert len(written) == 1 and written[0].parent == tmp_path and written[0].name.startswith("threads-")
    text = written[0].read_text(encoding="utf-8")
    assert "# sbmig thread dump" in text and "MainThread" in text
    assert not (tmp_path / "dump-threads").exists()
    assert diag.check_dump_triggers([tmp_path]) == []                       # one dump per trigger


def test_explorer_txt_name_is_accepted(tmp_path):
    (tmp_path / "dump-threads.txt").write_text("")                          # hidden extensions in Explorer
    assert len(diag.check_dump_triggers([tmp_path])) == 1


def test_trigger_that_cannot_be_removed_fires_once(tmp_path, monkeypatch):
    t = tmp_path / "dump-threads"
    t.write_text("")
    real_unlink = type(t).unlink

    def locked(self, *a, **kw):
        if self.name == "dump-threads":
            raise PermissionError("open in an editor")
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(type(t), "unlink", locked)
    assert len(diag.check_dump_triggers([tmp_path])) == 1
    assert diag.check_dump_triggers([tmp_path]) == []                       # same mtime → no second dump


def test_workspace_registers_its_run_dir(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    ws = Workspace(root, tmp_path / "run", "t")
    assert ws.run_dir in diag.watched_dirs()
    for i in range(diag.MAX_WATCHED_RUN_DIRS + 2):
        diag.watch_dump_trigger(tmp_path / f"r{i}")
    dirs = diag.watched_dirs()
    assert len(dirs) == diag.MAX_WATCHED_RUN_DIRS and dirs[0] == tmp_path / f"r{diag.MAX_WATCHED_RUN_DIRS + 1}"


def test_watcher_thread_picks_up_a_trigger(tmp_path):
    diag.stop_dump_trigger_watcher()                     # a watcher of an earlier test (CLI setup_logging) polls slowly
    try:
        assert diag.start_dump_trigger_watcher(tmp_path, interval=0.05)
        assert diag._WATCHER.name == "sbmig-dump-trigger" and diag._WATCHER.daemon
        (tmp_path / "dump-threads").write_text("")
        deadline = time.monotonic() + 5
        while not list(tmp_path.glob("threads-*.txt")) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert list(tmp_path.glob("threads-*.txt")) and not (tmp_path / "dump-threads").exists()
    finally:
        diag.stop_dump_trigger_watcher()
    assert diag._WATCHER is None


def test_sigbreak_handler_on_windows(tmp_path, monkeypatch):
    """Simulated Windows: no SIGUSR1, SIGBREAK present → the handler goes on SIGBREAK and writes the usual dump."""
    installed: dict = {}
    monkeypatch.delattr(signal, "SIGUSR1", raising=False)
    monkeypatch.setattr(signal, "SIGBREAK", 21, raising=False)
    monkeypatch.setattr(signal, "signal", lambda sig, handler: installed.update({sig: handler}))
    monkeypatch.setattr(diag, "start_dump_trigger_watcher", lambda *a, **k: True)
    assert diag.install_thread_dump_signal(tmp_path / "dumps") is True
    assert list(installed) == [21]
    installed[21](21, None)
    assert list((tmp_path / "dumps").glob("threads-*.txt"))


def test_posix_keeps_sigusr1(monkeypatch):
    if not hasattr(signal, "SIGUSR1"):  # pragma: no cover - Windows CI
        pytest.skip("POSIX only")
    installed: dict = {}
    monkeypatch.setattr(signal, "signal", lambda sig, handler: installed.update({sig: handler}))
    monkeypatch.setattr(diag, "start_dump_trigger_watcher", lambda *a, **k: True)
    assert diag.install_thread_dump_signal() is True and list(installed) == [signal.SIGUSR1]
