"""KB integrity (round 10, kept for v2.1): torn-line safe appends to <kb>/errors.jsonl and the cross-process lock."""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from sbmig.core.models import ErrorEntry
from sbmig.errors import CaseStore
from sbmig.kb.jsonl import append_lines, corrupt_path, line_ids, parse_record, repair_tail
from sbmig.kb.lock import LOCK_NAME, holding, kb_lock, pid_alive

REPO = Path(__file__).resolve().parents[2]


def rec(eid: str, l2: str = "compile|x|Foo") -> ErrorEntry:
    return ErrorEntry(id=eid, run_id="r1", l2=l2, message="m", status="unresolved")


# --------------------------------------------------------------------------- torn lines (C2)
def test_torn_last_line_is_moved_aside_and_later_records_survive(tmp_path, caplog, cfg):
    s = CaseStore(tmp_path, cfg)
    s.record_run([rec("A")])
    line = rec("B", "compile|x|Bar").model_dump_json()
    with open(s.errors_path, "a", encoding="utf-8") as fh:
        fh.write(line[: len(line) // 2])                       # crash mid-write of B
    assert "B" not in s._known_ids()                           # a torn id is not "stored"
    with caplog.at_level(logging.WARNING, logger="sbmig.kb"):
        assert s.record_run([rec("B", "compile|x|Bar"), rec("C", "compile|x|Baz")]) == 2
    assert [e.id for e in s.iter_errors()] == ["A", "B", "C"]
    assert corrupt_path(s.errors_path).read_text(encoding="utf-8") == line[: len(line) // 2] + "\n"
    assert any("torn last line" in r.getMessage() for r in caplog.records)
    assert s.errors_path.read_bytes().endswith(b"\n") and s.count_errors() == 3


def test_a_whole_record_without_newline_keeps_its_place(tmp_path, cfg):
    p = tmp_path / "errors.jsonl"
    p.write_text(rec("A").model_dump_json() + "\n" + rec("B").model_dump_json(), encoding="utf-8")
    assert repair_tail(p) is True and not corrupt_path(p).exists()
    assert [e.id for e in CaseStore(tmp_path, cfg).iter_errors()] == ["A", "B"]
    assert repair_tail(p) is False                                   # nothing to do any more


def test_a_glued_line_is_salvaged(tmp_path, cfg):
    """A torn B + a whole C on one line (an append after a crash, before round 10): C is read back, B is not."""
    p = tmp_path / "errors.jsonl"
    b, c = rec("B").model_dump_json(), rec("C").model_dump_json()
    p.write_text(rec("A").model_dump_json() + "\n" + b[:40] + c + "\n", encoding="utf-8")
    s = CaseStore(tmp_path, cfg)
    assert [e.id for e in s.iter_errors()] == ["A", "C"] and s._known_ids() == {"A", "C"}
    assert parse_record(b[:40] + c, ErrorEntry).id == "C" and parse_record(b[:40], ErrorEntry) is None
    assert line_ids(b[:40] + c) == ["C"]


def test_append_lines_fsyncs_one_batch(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(os, "fsync", lambda fd: calls.append(fd))
    assert append_lines(tmp_path / "x.jsonl", ['{"id":"a"}', '{"id":"b"}']) == 2
    assert len(calls) == 1 and (tmp_path / "x.jsonl").read_text() == '{"id":"a"}\n{"id":"b"}\n'


# --------------------------------------------------------------------------- the lock (C3)
def _foreign_lock(root: Path, *, host: str = "other-host", pid: int = 4242, age: float = 0.0,
                  future: bool = False) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    p = root / LOCK_NAME
    p.write_text(json.dumps({"pid": pid, "host": host, "at": "2026-10-26T00:00:00", "token": "theirs"}))
    t = time.time() + (3600 if future else -age)
    os.utime(p, (t, t))
    return p


def test_lock_is_reentrant_and_removed_after_the_outermost_exit(tmp_path):
    with kb_lock(tmp_path, 1) as held:
        assert held and (tmp_path / LOCK_NAME).exists() and holding(tmp_path)
        info = json.loads((tmp_path / LOCK_NAME).read_text())
        assert info["pid"] == os.getpid() and info["host"] == socket.gethostname() and info["token"]
        with kb_lock(tmp_path, 1) as again:
            assert again and json.loads((tmp_path / LOCK_NAME).read_text())["token"] == info["token"]
        assert (tmp_path / LOCK_NAME).exists()
    assert not (tmp_path / LOCK_NAME).exists() and not holding(tmp_path)


def test_a_dead_pid_lock_is_broken_at_once(tmp_path, caplog):
    proc = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    dead = int(proc.stdout.strip())
    assert not pid_alive(dead) and pid_alive(os.getpid())
    _foreign_lock(tmp_path, host=socket.gethostname(), pid=dead)
    t0 = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="sbmig.kb"), kb_lock(tmp_path, 30) as held:
        assert held and json.loads((tmp_path / LOCK_NAME).read_text())["pid"] == os.getpid()
    assert time.monotonic() - t0 < 5 and any("stale" in r.getMessage() for r in caplog.records)


def test_an_old_lock_of_another_host_is_stale(tmp_path):
    _foreign_lock(tmp_path, age=120)
    with kb_lock(tmp_path, 60) as held:
        assert held and json.loads((tmp_path / LOCK_NAME).read_text())["token"] != "theirs"


def test_a_live_lock_is_waited_for_then_the_write_goes_on_without_it(tmp_path, caplog, cfg):
    p = _foreign_lock(tmp_path, future=True)                  # fresh, another host: never stale within the wait
    s = CaseStore(tmp_path, cfg, lock_timeout=0.4)
    t0 = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="sbmig.kb"):
        assert s.record_run([rec("A")]) == 1                  # never fails the run
    assert 0.35 <= time.monotonic() - t0 < 5
    assert any("going on without it" in r.getMessage() for r in caplog.records)
    assert json.loads(p.read_text())["token"] == "theirs"     # their lock is left alone


def test_release_keeps_a_lock_that_is_not_ours(tmp_path):
    with kb_lock(tmp_path, 1):
        (tmp_path / LOCK_NAME).write_text(json.dumps({"pid": 1, "host": "x", "token": "someone-else"}))
    assert json.loads((tmp_path / LOCK_NAME).read_text())["token"] == "someone-else"


def test_threads_of_one_process_are_serialized(tmp_path):
    order: list[str] = []
    entered = threading.Event()

    def other():
        entered.wait(5)
        with kb_lock(tmp_path, 5):
            order.append("other")

    t = threading.Thread(target=other)
    t.start()
    with kb_lock(tmp_path, 5):
        entered.set()
        time.sleep(0.2)
        order.append("main")
    t.join(5)
    assert order == ["main", "other"]


def test_another_process_waits_for_the_lock(tmp_path):
    marker = tmp_path / "held"
    code = ("import sys, time; from pathlib import Path; from sbmig.kb.lock import kb_lock\n"
            "with kb_lock(Path(sys.argv[1]), 30):\n"
            "    Path(sys.argv[2]).write_text('x'); time.sleep(1.0)\n")
    proc = subprocess.Popen([sys.executable, "-c", code, str(tmp_path / "kb"), str(marker)], cwd=str(REPO),
                            env={**os.environ, "PYTHONPATH": str(REPO)})
    try:
        for _ in range(200):
            if marker.exists():
                break
            time.sleep(0.05)
        assert marker.exists(), "the child never took the lock"
        t0 = time.monotonic()
        with kb_lock(tmp_path / "kb", 30) as held:
            waited = time.monotonic() - t0
        assert held and waited >= 0.4
    finally:
        proc.wait(30)
