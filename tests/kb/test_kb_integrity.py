"""KB integrity (round 10, C2 / C3 / C9): torn jsonl lines, the cross-process lock, stale in-memory indexes,
`kb import --replace` swap + backup, the crash-safe recipe move, streamed stats."""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest

from sbmig.core.models import ErrorCaseRecord, KBRecipe, ResolutionRecord
from sbmig.kb import ErrorCaseStore, FileKnowledgeBase
from sbmig.kb.jsonl import append_lines, corrupt_path, parse_record, repair_tail
from sbmig.kb.ledger import Ledger
from sbmig.kb.lock import LOCK_NAME, holding, kb_lock, pid_alive

from .helpers import make_case, recipe_yaml

REPO = Path(__file__).resolve().parents[2]


def rec(eid: str, l2: str = "compile|x|Foo") -> ErrorCaseRecord:
    return ErrorCaseRecord(id=eid, signature_generic=l2, message="m")


# --------------------------------------------------------------------------- torn lines (C2)
def test_torn_last_line_is_moved_aside_and_later_records_survive(tmp_path, caplog):
    """review-arch/torn.py: a write killed half-way used to swallow the next record and count the torn id."""
    s = ErrorCaseStore(tmp_path)
    s.append([rec("A")], [])
    line = rec("B", "compile|x|Bar").model_dump_json()
    with open(s.errors_path, "a", encoding="utf-8") as fh:
        fh.write(line[: len(line) // 2])                       # crash mid-write of B
    assert "B" not in ErrorCaseStore(tmp_path)._known_ids(s.errors_path)   # a torn id is not "stored"
    s2 = ErrorCaseStore(tmp_path)
    with caplog.at_level(logging.WARNING, logger="sbmig.kb"):
        assert s2.append_counts([rec("B", "compile|x|Bar"), rec("C", "compile|x|Baz")], []) == (2, 0)
    assert [e.id for e in s2.iter_errors()] == ["A", "B", "C"]
    assert corrupt_path(s.errors_path).read_text(encoding="utf-8") == line[: len(line) // 2] + "\n"
    assert any("torn last line" in r.getMessage() for r in caplog.records)
    assert s.errors_path.read_bytes().endswith(b"\n") and s2.count_errors() == 3


def test_a_whole_record_without_newline_keeps_its_place(tmp_path):
    p = tmp_path / "errors.jsonl"
    p.write_text(rec("A").model_dump_json() + "\n" + rec("B").model_dump_json(), encoding="utf-8")
    assert repair_tail(p) is True and not corrupt_path(p).exists()
    assert [e.id for e in ErrorCaseStore(tmp_path).iter_errors()] == ["A", "B"]
    assert repair_tail(p) is False                                   # nothing to do any more


def test_glued_line_from_before_round_10_is_salvaged(tmp_path):
    """The old bug: torn B + whole C on one line. C is read back (and counted), B is not."""
    p = tmp_path / "errors.jsonl"
    b, c = rec("B").model_dump_json(), rec("C").model_dump_json()
    p.write_text(rec("A").model_dump_json() + "\n" + b[:40] + c + "\n", encoding="utf-8")
    s = ErrorCaseStore(tmp_path)
    assert [e.id for e in s.iter_errors()] == ["A", "C"]
    assert s._known_ids(p) == {"A", "C"}
    assert parse_record(b[:40] + c, ErrorCaseRecord).id == "C" and parse_record(b[:40], ErrorCaseRecord) is None


def test_legacy_lines_read_as_schema_1(tmp_path):
    data = json.loads(rec("A").model_dump_json())
    data.pop("schema_version")
    data.pop("agent_version")
    (tmp_path / "errors.jsonl").write_text(json.dumps(data) + "\n" + rec("B").model_dump_json() + "\n")
    got = {e.id: e.schema_version for e in ErrorCaseStore(tmp_path).iter_errors()}
    assert got == {"A": 1, "B": 2}


def test_ledger_append_is_torn_line_safe(tmp_path):
    led = Ledger(tmp_path / "ledger.jsonl")
    r1 = ResolutionRecord(id="r1", project="p", resolution="unresolved", outcome="unresolved")
    led.append([r1])
    with open(led.path, "a", encoding="utf-8") as fh:
        fh.write('{"id":"r2","proj')
    assert led.count() == 1
    led.append([r1.model_copy(update={"id": "r3"})])
    assert [r.id for r in led.iter()] == ["r1", "r3"] and corrupt_path(led.path).exists()


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


def test_a_live_lock_is_waited_for_then_the_write_goes_on_without_it(tmp_path, caplog):
    p = _foreign_lock(tmp_path, future=True)                  # fresh, another host: never stale within the wait
    s = ErrorCaseStore(tmp_path, lock_timeout=0.4)
    t0 = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="sbmig.kb"):
        assert s.append([rec("A")], []) == 1                  # never fails the run
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


def test_a_fleet_kb_reloads_an_index_another_process_wrote(kb_cfg):
    """Two FileKnowledgeBase objects on one KB (fleet + web server): the second one used to save its stale
    in-memory index over the first one's case."""
    kb1, kb2 = FileKnowledgeBase(kb_cfg), FileKnowledgeBase(kb_cfg)
    kb2._load_index()                                          # loaded before kb1 wrote
    a = kb1.record_case(make_case(signatures=["L1-a"], signatures_generic=["L2-a"]))
    b = kb2.record_case(make_case(signatures=["L1-b"], signatures_generic=["L2-b"],
                                  summary="another fix", changes=[]))
    idx = json.loads(kb1.index_path.read_text())
    assert idx["l1"]["L1-a"] == [a] and idx["l1"]["L1-b"] == [b]
    assert kb1._load_index()["l1"].get("L1-b") == [b]         # kb1 sees kb2's write too


# --------------------------------------------------------------------------- import --replace, recipe move (C9)
def _seeded(kb_cfg, sub: str) -> FileKnowledgeBase:
    kb = FileKnowledgeBase(kb_cfg, kb_dir=Path(kb_cfg.kb.dir).parent / sub)
    kb.record_case(make_case())
    kb.error_case_store().append([rec(f"{sub}-e")], [])
    return kb


def test_import_replace_swaps_in_a_new_kb_and_keeps_a_backup(kb_cfg, tmp_path):
    other = _seeded(kb_cfg, "other")
    zp = other.export_zip(tmp_path / "other.zip")
    local = _seeded(kb_cfg, "kb")
    (local.root / "cases" / ".gitkeep").write_text("")
    (local.root / "notes.txt").write_text("mine")
    counters = local.import_zip(zp, merge=False)
    backups = sorted(local.root.parent.glob("kb.bak-*"))
    assert len(backups) == 1 and counters["backup"] == str(backups[0])
    assert (backups[0] / "notes.txt").read_text() == "mine" and not (backups[0] / LOCK_NAME).exists()
    assert [e.id for e in local.error_case_store().iter_errors()] == ["other-e"]
    assert not (local.root / "notes.txt").exists() and (local.root / "cases" / ".gitkeep").exists()
    assert not list(local.root.parent.glob(".kb.import-*")) and not (local.root / LOCK_NAME).exists()


def test_a_failed_import_replace_leaves_the_kb_untouched(kb_cfg, tmp_path, monkeypatch):
    other = _seeded(kb_cfg, "other")
    zp = other.export_zip(tmp_path / "other.zip")
    local = _seeded(kb_cfg, "kb")

    def boom(self, path, merge):
        raise RuntimeError("disk full")

    monkeypatch.setattr(FileKnowledgeBase, "_import_merge", boom)
    with pytest.raises(RuntimeError):
        local.import_zip(zp, merge=False)
    assert [e.id for e in local.error_case_store().iter_errors()] == ["kb-e"]
    assert not list(local.root.parent.glob("kb.bak-*")) and not list(local.root.parent.glob(".kb.import-*"))


def test_move_recipe_writes_the_new_meta_before_moving(kb, monkeypatch):
    r = kb.add_recipe(recipe_yaml("MoveMe"), KBRecipe(id="MoveMe", path="", signatures=["sig"]))
    import sbmig.kb.store as store_mod

    real = store_mod.os.replace
    seen: list[bool] = []

    def crash(src, dst):
        if not str(dst).endswith("MoveMe.yml"):
            return real(src, dst)                              # atomic writes (the meta) go through
        seen.append(Path(dst).with_name("MoveMe.meta.json").exists())
        raise OSError("killed")                                # the process dies while moving the yml

    monkeypatch.setattr(store_mod.os, "replace", crash)
    with pytest.raises(OSError):
        kb.promote(r.id)
    monkeypatch.setattr(store_mod.os, "replace", real)
    assert seen == [True]                                      # the destination meta came first
    back = kb.get_recipe("MoveMe")
    assert back is not None and back.status == "candidate"    # the old state is intact (orphan meta ignored)
    assert kb.promote("MoveMe").status == "active"
    assert [x.status for x in kb.list_recipes() if x.id == "MoveMe"] == ["active"]


def test_export_of_a_missing_kb_creates_nothing(kb, tmp_path):
    out = kb.export_zip(tmp_path / "empty.zip")
    assert zipfile.ZipFile(out).namelist() == [] and not any(kb.root.glob("*.json"))


def test_stats_counts_without_listing_everything(kb, monkeypatch):
    kb.record_case(make_case())
    kb.error_case_store().append([rec("A"), rec("B")], [])
    st = kb.stats()
    assert st["cases"] == 1 and st["error_cases"] == 2 and st["cases_reused"] == 0
