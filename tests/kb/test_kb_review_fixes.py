"""Review fixes of the multi-writer KB (2026-11-10: up to three pods on one NFS volume), one block per defect.

1. ``_confirm_new`` never writes a fresh ``case.json`` over one the server says exists: it lists the folder and reads
   by ``open()`` again for up to ``CONFIRM_SECONDS`` and raises ``CaseUnreadable`` when it still cannot read it; an
   empty / damaged file is repaired as before; an empty file is "not there yet" for a lock-free reader.
2. Breaking a stale lock is serialized by ``.lock.breaker``; a lock that changed under the breaker is put back
   exclusively; real processes breaking crashed locks never hold the lock twice.
3. A stale lock that cannot be moved is waited for, never spun on.
4. ``_claim_script`` falls back only for "no hard links" errors and never replaces a name that exists.
5. ``_release`` never removes a lock that does not read with our token.
6. ``keep_alive`` keeps a long section's lock fresh; a non-positive timeout means the default.
7. A clock probe slowed by a stall is discarded.
9. ``import --replace``: a KB folder re-created by a pod between the two renames → the old KB is put back.

Local disk only: the NFS cache behaviour itself is simulated (``_read`` answering "missing"), not reproduced.
"""
from __future__ import annotations

import errno
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

from sbmig.core.models import ErrorEntry, ErrorKind
from sbmig.errors import CaseStore, case_id_for
from sbmig.errors import cases as casesmod
from sbmig.errors.cases import CaseUnreadable
from sbmig.kb import FileKnowledgeBase
from sbmig.kb import lock as lockmod
from sbmig.kb.lock import BREAKER_NAME, LOCK_NAME, kb_lock, keep_alive
from sbmig.loop.entries import CaseScripts

REPO = Path(__file__).resolve().parents[2]
L2 = "compile|package_does_not_exist|jakarta.xml.bind"
CID = case_id_for(L2)
OTHER_L2 = "compile|cannot_find_symbol|ReviewOther"
CODE1 = "def fix(p):\n    p.log('v1')\n"
CODE2 = "def fix(p):\n    p.log('v2')\n"


def entry(eid: str, l2: str, project: str, run_id: str) -> ErrorEntry:
    return ErrorEntry(id=eid, run_id=run_id, project=project, case_id=case_id_for(l2), l2=l2,
                      kind=ErrorKind.COMPILE, file="src/A.java", line=3, message="cannot find symbol",
                      seen="compile", status="open")


def msgs(caplog, level: int = logging.DEBUG) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= level]


def foreign_lock(root: Path, *, name: str = LOCK_NAME, age: float = 0.0, host: str = "other-host",
                 token: str = "theirs", pid: int = 4242) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    p = root / name
    p.write_text(json.dumps({"pid": pid, "host": host, "at": "2026-11-10T00:00:00", "token": token}))
    t = time.time() - age
    os.utime(p, (t, t))
    return p


def token_of(p: Path) -> str | None:
    try:
        return json.loads(p.read_text()).get("token")
    except (OSError, ValueError):
        return None


def blind_reader(store: CaseStore, monkeypatch, seconds: float | None) -> dict[str, int]:
    """``store._read`` answers "missing" for ``seconds`` (None: always) — an NFS client trusting a cached negative
    entry for a case.json another pod wrote."""
    real = store._read
    until = None if seconds is None else time.monotonic() + seconds
    seen = {"n": 0}

    def read(case_id, **kw):
        seen["n"] += 1
        if until is None or time.monotonic() < until:
            return None
        return real(case_id, **kw)
    monkeypatch.setattr(store, "_read", read)
    return seen


# --------------------------------------------------------------------------- 1. _confirm_new
def test_a_case_this_client_sees_late_is_used_never_overwritten(tmp_path, cfg, monkeypatch, caplog):
    kb = tmp_path / "kb"
    a = CaseStore(kb, cfg)
    a.add_script(CID, L2, CODE1, run_id="runA", project="projA")
    a.credit(CID, 1, "improved", project="p2", run_id="r2")
    b = CaseStore(kb, cfg)
    blind_reader(b, monkeypatch, 1.5)                        # longer than the old 1 s confirmation window
    listed: list[str] = []
    real_listdir = os.listdir
    monkeypatch.setattr(casesmod.os, "listdir", lambda p: listed.append(str(p)) or real_listdir(p))
    alive: list[int] = []
    monkeypatch.setattr(lockmod, "keep_alive", lambda root, **kw: alive.append(1) or False)
    with caplog.at_level(logging.INFO, logger="sbmig.errors"):
        s = b.add_script(CID, L2, CODE2, run_id="runB", project="projB")
    assert s.version == 2
    case = a.find(CID)
    assert [(x.version, x.created_by) for x in case.scripts] == [(1, "runA"), (2, "runB")]
    assert case.scripts[0].successes == 1                    # pod A's credit kept
    assert any(p.endswith(CID) for p in listed)              # the folder was listed (attributes fetched again)
    assert alive                                             # the lock was kept alive while waiting
    m = msgs(caplog)
    assert any("created by another writer meanwhile" in x for x in m)
    assert not any("written over it" in x for x in m)


def test_a_case_this_client_never_sees_is_not_overwritten_and_the_write_is_skipped(tmp_path, cfg, monkeypatch,
                                                                                   caplog):
    kb = tmp_path / "kb"
    a = CaseStore(kb, cfg)
    a.add_script(CID, L2, CODE1, run_id="runA", project="projA")
    before = (kb / "cases" / CID / "case.json").read_bytes()
    monkeypatch.setattr(casesmod, "CONFIRM_SECONDS", 0.6)
    b = CaseStore(kb, cfg)
    blind_reader(b, monkeypatch, None)
    with caplog.at_level(logging.WARNING):
        with pytest.raises(CaseUnreadable):
            b.add_script(CID, L2, CODE2, run_id="runB", project="projB")
        # the fix loop's adapter survives it: None → "fix script not stored (kb.cases.add_script failed): v0"
        assert CaseScripts(b).add_script(CID, L2, CODE2, source="llm-new", run_id="runB", project="projB") is None
    assert (kb / "cases" / CID / "case.json").read_bytes() == before
    assert sorted(p.name for p in (kb / "cases" / CID).glob("fix_v*.py")) == ["fix_v1.py"]
    m = msgs(caplog, logging.WARNING)
    assert any("exists on the server but this client could not read it" in x and "nothing is written over it" in x
               for x in m)
    assert any("kb.cases.add_script failed: CaseUnreadable" in x for x in m)
    assert not (kb / LOCK_NAME).exists() and not (kb / BREAKER_NAME).exists()


def test_record_run_skips_only_the_case_it_cannot_read(tmp_path, cfg, monkeypatch, caplog):
    kb = tmp_path / "kb"
    a = CaseStore(kb, cfg)
    a.add_script(CID, L2, CODE1, run_id="runA", project="projA")
    monkeypatch.setattr(casesmod, "CONFIRM_SECONDS", 0.3)
    b = CaseStore(kb, cfg)
    real = b._read
    monkeypatch.setattr(b, "_read", lambda cid, **kw: None if cid == CID else real(cid, **kw))
    with caplog.at_level(logging.WARNING, logger="sbmig.errors"):
        n = b.record_run([entry("e1", L2, "p1", "r1"), entry("e2", OTHER_L2, "p1", "r1")])
    assert n == 2                                            # both lines are in errors.jsonl
    assert a.find(CID).seen == 0 and a.find(case_id_for(OTHER_L2)).seen == 1
    assert any("not updated by this run" in x for x in msgs(caplog, logging.WARNING))
    assert a.find(CID).scripts[0].version == 1               # the unreadable case is untouched


def test_an_empty_case_json_is_not_there_yet_for_a_lock_free_reader(tmp_path, cfg, caplog):
    kb = tmp_path / "kb"
    (kb / "cases" / CID).mkdir(parents=True)
    (kb / "cases" / CID / "case.json").write_bytes(b"")     # another pod's probe between its create and unlink
    with caplog.at_level(logging.DEBUG, logger="sbmig.errors"):
        assert CaseStore(kb, cfg).find(CID) is None
    assert not msgs(caplog, logging.WARNING)
    assert any("is empty" in x for x in msgs(caplog))


def test_an_empty_case_json_that_stays_empty_is_repaired(tmp_path, cfg, caplog):
    kb = tmp_path / "kb"
    folder = kb / "cases" / CID
    folder.mkdir(parents=True)
    (folder / "case.json").write_bytes(b"")                 # a probe that crashed between create and unlink
    t0 = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="sbmig.errors"):
        assert CaseStore(kb, cfg).add_script(CID, L2, CODE1).version == 1
    assert time.monotonic() - t0 < 10
    assert CaseStore(kb, cfg).find(CID).scripts[0].version == 1
    assert any("stays empty" in x and "a fresh one is written over it" in x for x in msgs(caplog, logging.WARNING))


def test_a_damaged_case_json_is_repaired_after_the_short_window(tmp_path, cfg, caplog):
    kb = tmp_path / "kb"
    folder = kb / "cases" / CID
    folder.mkdir(parents=True)
    (folder / "case.json").write_text("{not json")
    t0 = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="sbmig.errors"):
        assert CaseStore(kb, cfg).add_script(CID, L2, CODE1).version == 1
    assert time.monotonic() - t0 < 10                        # REPAIR_SECONDS, not CONFIRM_SECONDS
    assert any("a fresh one is written over it" in x for x in msgs(caplog, logging.WARNING))


# --------------------------------------------------------------------------- 2. breakers serialized
def test_a_live_breaker_of_another_pod_keeps_others_from_breaking(tmp_path, caplog):
    stale = foreign_lock(tmp_path, age=1000, token="L1")
    foreign_lock(tmp_path, name=BREAKER_NAME, token="their-breaker")       # fresh: that pod is breaking right now
    t0 = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="sbmig.kb"):
        assert lockmod._acquire(tmp_path, 0.6) is None
    assert time.monotonic() - t0 < 5
    assert token_of(stale) == "L1" and token_of(tmp_path / BREAKER_NAME) == "their-breaker"
    assert any("going on without it" in x for x in msgs(caplog))


@pytest.mark.parametrize("kind", ["old-other-host", "dead-pid-this-host"])
def test_a_crashed_breaker_expires(tmp_path, caplog, kind):
    foreign_lock(tmp_path, age=1000, token="L1")
    if kind == "old-other-host":
        foreign_lock(tmp_path, name=BREAKER_NAME, age=100, token="crashed")
    else:
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        foreign_lock(tmp_path, name=BREAKER_NAME, host=socket.gethostname(), pid=p.pid, token="crashed")
    with caplog.at_level(logging.WARNING, logger="sbmig.kb"), kb_lock(tmp_path, 5) as held:
        assert held and token_of(tmp_path / LOCK_NAME) not in ("L1", None)
    assert not (tmp_path / BREAKER_NAME).exists()
    m = msgs(caplog)
    assert any("breaker left by" in x and "expired" in x for x in m) and any("was stale" in x for x in m)


def test_a_live_breaker_of_this_host_never_expires_by_age(tmp_path):
    foreign_lock(tmp_path, age=1000, token="L1")
    foreign_lock(tmp_path, name=BREAKER_NAME, age=1000, host=socket.gethostname(), pid=os.getpid(), token="mine")
    assert lockmod._acquire(tmp_path, 0.5) is None
    assert token_of(tmp_path / LOCK_NAME) == "L1"


def _swap_inside_replace(monkeypatch, path: Path, new_token: str) -> None:
    """Right inside the breaker's rename: the judged lock is released and a fresh one (``new_token``) taken."""
    real = os.replace
    done = {"swap": False}

    def hooked(src, dst, *a, **kw):
        if str(src) == str(path) and not done["swap"]:
            done["swap"] = True
            os.unlink(path)
            foreign_lock(path.parent, token=new_token, age=-100)   # live: never stale within the wait
        return real(src, dst, *a, **kw)
    monkeypatch.setattr(lockmod.os, "replace", hooked)


def test_a_lock_taken_under_the_breaker_is_put_back_not_removed(tmp_path, monkeypatch):
    p = foreign_lock(tmp_path, age=1000, token="L1")
    _swap_inside_replace(monkeypatch, p, "L2")
    assert lockmod._acquire(tmp_path, 0.5) is None           # L2 is live: never broken, never taken over
    assert token_of(p) == "L2"
    assert not [x for x in tmp_path.iterdir() if ".stale-" in x.name or x.name == BREAKER_NAME]


def test_a_put_back_never_overwrites_a_lock_created_meanwhile(tmp_path, monkeypatch, caplog):
    p = foreign_lock(tmp_path, age=1000, token="L1")
    _swap_inside_replace(monkeypatch, p, "L2")
    real_link = os.link

    def link(src, dst, *a, **kw):
        if str(dst) == str(p):
            foreign_lock(tmp_path, token="L3", age=-100)     # a third writer took the free name first
        return real_link(src, dst, *a, **kw)
    monkeypatch.setattr(lockmod.os, "link", link)
    with caplog.at_level(logging.WARNING, logger="sbmig.kb"):
        assert lockmod._acquire(tmp_path, 0.5) is None
    assert token_of(p) == "L3"                               # the newest lock stands; no replace over it
    assert any("could not be put back" in x for x in msgs(caplog))
    assert not [x for x in tmp_path.iterdir() if ".stale-" in x.name]


def test_an_empty_lock_moved_by_mistake_is_put_back(tmp_path, monkeypatch):
    p = tmp_path / LOCK_NAME
    p.write_bytes(b"")
    old = time.time() - 1000
    os.utime(p, (old, old))
    real = os.replace
    done = {"swap": False}

    def hooked(src, dst, *a, **kw):
        if str(src) == str(p) and not done["swap"]:
            done["swap"] = True
            os.unlink(p)
            p.write_bytes(b"")                               # a NEW owner between its create and its payload write
        return real(src, dst, *a, **kw)
    monkeypatch.setattr(lockmod.os, "replace", hooked)
    assert lockmod._acquire(tmp_path, 0.5) is None
    assert p.exists() and p.read_bytes() == b""


_STRESS = r"""
import json, os, sys, time
from pathlib import Path
from sbmig.kb.lock import kb_lock
root, out, i, n, go = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], int(sys.argv[4]), Path(sys.argv[5])
while not go.exists():
    time.sleep(0.002)
held = overlap = 0
for _ in range(n):
    with kb_lock(root, 30) as h:
        if not h:
            continue
        held += 1
        try:
            fd = os.open(str(root / "inside"), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            overlap += 1
            continue
        os.close(fd)
        time.sleep(0.002)
        os.unlink(str(root / "inside"))
(out / f"w{i}.json").write_text(json.dumps({"held": held, "overlap": overlap}))
"""

_CRASHER = r"""
import json, os, random, socket, sys, time
from pathlib import Path
root, dead, go, until = Path(sys.argv[1]), int(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4])
while not go.exists():
    time.sleep(0.002)
planted = 0
while not until.exists():
    try:
        fd = os.open(str(root / ".lock"), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError:
        time.sleep(0.001)
        continue
    os.write(fd, json.dumps({"pid": dead, "host": socket.gethostname(), "token": f"dead-{planted}"}).encode())
    os.close(fd)
    planted += 1
    time.sleep(random.uniform(0.002, 0.01))
print(planted)
"""


def test_processes_breaking_crashed_locks_never_hold_it_twice(tmp_path):
    root, out, go, until = tmp_path / "kb", tmp_path / "out", tmp_path / "go", tmp_path / "until"
    root.mkdir()
    out.mkdir()
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()                                              # a pid that is dead on this host
    env = {**os.environ, "PYTHONPATH": str(REPO) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    workers = [subprocess.Popen([sys.executable, "-c", _STRESS, str(root), str(out), str(i), "40", str(go)],
                                cwd=str(REPO), env=env) for i in range(4)]
    crasher = subprocess.Popen([sys.executable, "-c", _CRASHER, str(root), str(gone.pid), str(go), str(until)],
                               cwd=str(REPO), env=env, stdout=subprocess.PIPE, text=True)
    try:
        go.write_text("go")
        for w in workers:
            assert w.wait(120) == 0
        until.write_text("stop")
        planted = int(crasher.communicate(timeout=30)[0].strip() or 0)
    finally:
        for p in workers + [crasher]:
            if p.poll() is None:
                p.kill()
    results = [json.loads((out / f"w{i}.json").read_text()) for i in range(4)]
    assert sum(r["overlap"] for r in results) == 0
    assert sum(r["held"] for r in results) == 160            # nobody went on without the lock
    assert planted > 0                                       # the breakers really had crashed locks to break
    assert not (root / BREAKER_NAME).exists() and not [x for x in root.iterdir() if ".stale-" in x.name]


# --------------------------------------------------------------------------- 3. no busy loop
def test_a_stale_lock_that_cannot_be_moved_is_waited_for_not_spun_on(tmp_path, monkeypatch, caplog):
    p = foreign_lock(tmp_path, age=1000, token="L1")
    calls = [0]
    real = os.replace

    def deny(src, dst, *a, **kw):
        if str(src) == str(p):
            calls[0] += 1
            raise PermissionError(errno.EACCES, "Permission denied")
        return real(src, dst, *a, **kw)
    monkeypatch.setattr(lockmod.os, "replace", deny)
    t0 = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="sbmig.kb"):
        assert lockmod._acquire(tmp_path, 1.0) is None
    assert time.monotonic() - t0 < 5 and 1 <= calls[0] < 40
    m = msgs(caplog)
    assert len([x for x in m if "cannot be moved aside" in x]) == 1
    assert any("going on without it" in x for x in m)
    assert token_of(p) == "L1" and not (tmp_path / BREAKER_NAME).exists()


# --------------------------------------------------------------------------- 4. _claim_script
@pytest.mark.parametrize("code", [errno.EIO, errno.ESTALE, errno.EACCES, errno.ENOSPC])
def test_a_real_link_error_is_raised_and_overwrites_nothing(tmp_path, cfg, monkeypatch, code):
    st = CaseStore(tmp_path / "kb", cfg)
    d = st.case_dir(CID)
    d.mkdir(parents=True)
    (d / "fix_v1.py").write_text("print('another pod v1')\n")

    def bad(*a, **kw):
        raise OSError(code, os.strerror(code))
    monkeypatch.setattr(casesmod.os, "link", bad)
    with pytest.raises(OSError) as ei:
        st._claim_script(CID, 1, b"print('mine')\n")
    assert ei.value.errno == code
    assert (d / "fix_v1.py").read_text() == "print('another pod v1')\n"
    assert not [p for p in d.iterdir() if p.name.endswith(".tmp")]


@pytest.mark.parametrize("err", [OSError(errno.EPERM, "Operation not permitted"),
                                 OSError(errno.EOPNOTSUPP, "Operation not supported"),
                                 OSError(errno.EMLINK, "Too many links"),
                                 NotImplementedError("no links")])
def test_without_hard_links_an_existing_name_is_never_replaced(tmp_path, cfg, monkeypatch, caplog, err):
    st = CaseStore(tmp_path / "kb", cfg)
    d = st.case_dir(CID)
    d.mkdir(parents=True)
    (d / "fix_v1.py").write_text("print('another pod v1')\n")

    def no_link(*a, **kw):
        raise err
    monkeypatch.setattr(casesmod.os, "link", no_link)
    with caplog.at_level(logging.WARNING, logger="sbmig.errors"):
        assert st._claim_script(CID, 1, b"print('mine')\n") == 2
    assert (d / "fix_v1.py").read_text() == "print('another pod v1')\n"
    assert (d / "fix_v2.py").read_bytes() == b"print('mine')\n"
    assert any("already exists" in x for x in msgs(caplog, logging.WARNING))


# --------------------------------------------------------------------------- 5. release
def test_release_leaves_an_empty_lock_alone(tmp_path):
    with kb_lock(tmp_path, 5) as held:
        assert held
        (tmp_path / LOCK_NAME).write_bytes(b"")              # a new owner between its create and payload write
    assert (tmp_path / LOCK_NAME).exists()


def test_an_owner_that_cannot_be_written_drops_the_lock(tmp_path, monkeypatch, caplog):
    real = os.fdopen

    def full(fd, *a, **kw):
        os.close(fd)
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(lockmod.os, "fdopen", full)
    with caplog.at_level(logging.WARNING, logger="sbmig.kb"):
        assert lockmod._acquire(tmp_path, 1.0) is None
    monkeypatch.setattr(lockmod.os, "fdopen", real)
    assert not (tmp_path / LOCK_NAME).exists()
    assert any("could not write the owner" in x for x in msgs(caplog))


# --------------------------------------------------------------------------- 6. heartbeat + timeout
def test_keep_alive_touches_only_our_held_lock(tmp_path, caplog):
    assert keep_alive(tmp_path, force=True) is False         # not held
    with kb_lock(tmp_path, 60):
        p = tmp_path / LOCK_NAME
        old = time.time() - 100
        os.utime(p, (old, old))
        assert keep_alive(tmp_path, force=True) is True and p.stat().st_mtime > old + 50
        assert keep_alive(tmp_path) is False                 # throttled
        data = json.loads(p.read_text())
        p.write_text(json.dumps({**data, "token": "someone-else"}))
        os.utime(p, (old, old))
        with caplog.at_level(logging.WARNING, logger="sbmig.kb"):
            assert keep_alive(tmp_path, force=True) is False
            assert keep_alive(tmp_path, force=True) is False
        assert p.stat().st_mtime < old + 1                   # somebody else's lock is not touched
    assert len([x for x in msgs(caplog) if "no longer ours" in x]) == 1
    assert token_of(tmp_path / LOCK_NAME) == "someone-else"  # and not released


def test_a_long_section_kept_alive_is_never_broken(tmp_path, caplog):
    code = ("import logging, sys; from pathlib import Path\n"
            "logging.basicConfig(level=logging.INFO, format='%(message)s')\n"
            "from sbmig.kb.lock import kb_lock\n"
            "with kb_lock(Path(sys.argv[1]), 1.0) as held:\n"
            "    print('HELD', held)\n")
    env = {**os.environ, "PYTHONPATH": str(REPO) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    with kb_lock(tmp_path, 1.0) as held:                     # a 1 s timeout, a 3 s section
        assert held
        mine = token_of(tmp_path / LOCK_NAME)
        waiter = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)], cwd=str(REPO), env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 3.0:
            keep_alive(tmp_path)
            time.sleep(0.05)
        out = waiter.communicate(timeout=30)[0]
        assert token_of(tmp_path / LOCK_NAME) == mine
    assert "HELD False" in out and "stale" not in out


def test_recount_keeps_the_lock_alive(tmp_path, cfg, monkeypatch):
    s = CaseStore(tmp_path / "kb", cfg)
    s.record_run([entry("e1", L2, "p1", "r1"), entry("e2", OTHER_L2, "p1", "r1")])
    calls: list[Path] = []
    monkeypatch.setattr(lockmod, "keep_alive", lambda root, **kw: calls.append(Path(root)) or False)
    assert s.recount() == 2
    assert len(calls) >= 3 and all(c == tmp_path / "kb" for c in calls)


@pytest.mark.parametrize("value", [0, -5])
def test_a_non_positive_timeout_means_the_default(tmp_path, monkeypatch, caplog, value):
    monkeypatch.setattr(lockmod, "DEFAULT_TIMEOUT", 0.5)
    p = foreign_lock(tmp_path, token="theirs", age=-100)    # live: with 0 it used to be stale at once
    with caplog.at_level(logging.WARNING, logger="sbmig.kb"):
        with kb_lock(tmp_path, value) as held:
            assert not held
        with kb_lock(tmp_path, value) as held:
            assert not held
    assert token_of(p) == "theirs"
    assert len([x for x in msgs(caplog) if "not positive" in x]) == 1


def test_a_case_store_with_timeout_zero_does_not_break_a_live_lock(tmp_path, cfg, monkeypatch):
    monkeypatch.setattr(lockmod, "DEFAULT_TIMEOUT", 0.5)
    p = foreign_lock(tmp_path / "kb", token="theirs", age=-100)
    CaseStore(tmp_path / "kb", cfg, lock_timeout=0).add_script(CID, L2, CODE1)
    assert token_of(p) == "theirs"


# --------------------------------------------------------------------------- 7. clock probe
def test_a_slow_probe_is_discarded_and_the_previous_offset_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(lockmod, "_PROBE_MAX", 0.05)
    st = lockmod._state(tmp_path)
    st.fs_offset, st.fs_offset_at = 3.0, -1e9               # a measurement long due for renewal
    real_open = os.open

    def stalled(path, *a, **kw):
        if os.path.basename(str(path)).startswith(lockmod.PROBE_PREFIX):
            time.sleep(0.15)                                 # an NFS stall inside the probe
        return real_open(path, *a, **kw)
    monkeypatch.setattr(os, "open", stalled)
    assert lockmod._probe_offset(tmp_path) is None
    assert lockmod._fs_offset(tmp_path) == 3.0
    assert st.fs_offset_at == -1e9                           # not cached: measured again next time
    monkeypatch.setattr(os, "open", real_open)
    assert abs(lockmod._fs_offset(tmp_path)) < 2.0 and st.fs_offset_at > 0
    assert not list(tmp_path.glob(lockmod.PROBE_PREFIX + "*"))


# --------------------------------------------------------------------------- 9. import --replace
def test_import_replace_puts_the_old_kb_back_when_a_pod_recreated_the_folder(kb_cfg, tmp_path, monkeypatch, caplog):
    kb = FileKnowledgeBase(kb_cfg)
    kb.record_run([entry("e1", L2, "p1", "r1")])
    original = kb.cases.errors_path.read_bytes()
    zpath = tmp_path / "export.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("errors.jsonl", entry("e9", OTHER_L2, "p9", "r9").model_dump_json() + "\n")
    root = kb.root
    real = os.replace
    once = {"done": False}

    def hooked(src, dst, *a, **kw):
        r = real(src, dst, *a, **kw)
        if str(src) == str(root) and not once["done"]:      # old KB → .bak: a pod re-creates <kb> right away
            once["done"] = True
            root.mkdir()
            (root / LOCK_NAME).write_text(json.dumps({"pid": 1, "host": "pod-b", "token": "b"}))
        return r
    monkeypatch.setattr("sbmig.kb.store.os.replace", hooked)
    with caplog.at_level(logging.ERROR, logger="sbmig.kb"), pytest.raises(OSError):
        kb.import_zip(zpath, merge=False)
    assert (root / "errors.jsonl").read_bytes() == original  # the previous KB is back in place
    intruders = list(root.parent.glob(root.name + ".intruder-*"))
    assert len(intruders) == 1 and token_of(intruders[0] / LOCK_NAME) == "b"
    assert not list(root.parent.glob(root.name + ".bak-*"))
    assert any("the previous KB is back in" in x and str(intruders[0]) in x for x in msgs(caplog, logging.ERROR))
