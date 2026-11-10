"""Several writers on one KB (v2.1z-ae: up to three build pods share one KB directory on an NFS volume).

1. Real processes hammer one KB at the same time through the public CaseStore API (the same NEW case from all of them
   at once, credits on shared and on separate cases, record_run): no lost update, no duplicate or skipped script
   number, no torn file, nothing left behind, nobody went on without the lock.
2. A new case met by two pods: an NFS client may answer "missing" from its cache for a case.json another pod has just
   written — the case is confirmed by exclusive creates (the folder, then case.json) and read again, never
   overwritten blind; a script number is claimed exclusively (never a second v1, never an overwritten file).
3. Lock staleness on one clock: the lock's age is measured on the file system's clock (a probe file's mtime), so a
   pod clock ahead of the server never breaks a fresh lock and a pod clock behind still breaks an old one.

This runs on the local disk: it proves the logic with real processes, not the NFS cache behaviour itself (the
exclusive create / rename semantics the code relies on are what NFS guarantees server-side).
"""
from __future__ import annotations

import json
import logging
import os
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

from sbmig.core.models import ErrorEntry, ErrorKind, FixTry
from sbmig.errors import CaseStore, case_id_for
from sbmig.kb import lock as lockmod
from sbmig.kb.lock import LOCK_NAME, PROBE_PREFIX, kb_lock

REPO = Path(__file__).resolve().parents[2]
PROCS = 4
ROUNDS = 20
EXTRA_EVERY = 4                     # every 4th round a process stores one more script of the shared case
SHARED_L2 = "compile|cannot_find_symbol|ParallelShared"
SHARED = case_id_for(SHARED_L2)
PIN = "block:pinned by process 0 before the others wrote"


def own_l2(i: int) -> str:
    return f"compile|cannot_find_symbol|ParallelOwn{i}"


def script(i: int, tag: str) -> str:
    # long enough that a torn or mixed file could not pass for one writer's script
    return f"def fix(p):\n    p.log('process {i} {tag}')\n" + "".join(f"# {i}-{tag} line {n}\n" for n in range(300))


def make_entry(eid: str, case_l2: str, project: str, run_id: str) -> ErrorEntry:
    return ErrorEntry(id=eid, run_id=run_id, project=project, case_id=case_id_for(case_l2), l2=case_l2,
                      kind=ErrorKind.COMPILE, file="src/A.java", line=3, message="cannot find symbol",
                      seen="compile", status="resolved", resolved=f"{run_id} try 1",
                      tries=[FixTry(n=1, round=1, source="llm-new", analysis="why", solution="how",
                                    applied="script", result="improved")])


# --------------------------------------------------------------------------- the worker (a separate process)
def _worker_main() -> None:
    """One writer: ``python -c '…_worker_main()' <kb> <out> <index> <rounds>`` (cwd = the repo)."""
    kb_dir, out = Path(sys.argv[1]), Path(sys.argv[2])
    i, rounds = int(sys.argv[3]), int(sys.argv[4])
    handler = logging.FileHandler(out / f"log-{i}.txt", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    sb = logging.getLogger("sbmig")
    sb.setLevel(logging.INFO)
    sb.addHandler(handler)
    store = CaseStore(kb_dir, None, lock_timeout=60, script_runs=0)
    contended = [0]
    judge = lockmod._judge

    def counting_judge(*a, **kw):                            # called only when the lock was taken by another
        contended[0] += 1
        return judge(*a, **kw)
    lockmod._judge = counting_judge
    (out / f"ready-{i}").write_text("ready")
    t0 = time.monotonic()
    while not (out / "start").exists():                      # the barrier: everybody starts together
        if time.monotonic() - t0 > 120:
            raise SystemExit("no start signal")
        time.sleep(0.002)
    stored: list[list] = []                                   # [case, version, code]
    credits: list[list] = []                                  # [case, version, result, run_id, round, n]
    entries: list[str] = []
    run = f"r{i}"
    pause = random.Random(i)                                  # a run does other work between KB writes
    began = time.time()

    def credit(case: str, version: int, result: str, rnd: int, n: int) -> None:
        assert store.credit(case, version, result, project=f"p{i}", run_id=run, round=rnd, n=n) is not None
        credits.append([case, version, result, run, rnd, n])

    # the same NEW case from every process at once
    code = script(i, "first")
    s = store.add_script(SHARED, SHARED_L2, code, run_id=run, project=f"p{i}", analysis=f"analysis {i}")
    stored.append([SHARED, s.version, code])
    if i == 0:
        store.set_pin(SHARED, PIN)
    own = case_id_for(own_l2(i))
    code = script(i, "own")
    s = store.add_script(own, own_l2(i), code, run_id=run, project=f"p{i}")
    stored.append([own, s.version, code])
    for k in range(rounds):
        n = k + 1
        credit(SHARED, 1, "improved", 1, n)                  # whoever stored v1: everybody credits it
        time.sleep(pause.uniform(0, 0.004))
        credit(SHARED, stored[0][1], "no_change", 2, n)      # this process's own first version
        time.sleep(pause.uniform(0, 0.004))
        credit(own, 1, "improved", 3, n)                     # a case nobody else writes
        if k % EXTRA_EVERY == 0:
            code = script(i, f"extra-{k}")
            s = store.add_script(SHARED, SHARED_L2, code, run_id=run, project=f"p{i}", source="llm-augment",
                                 parent=1, reason="v1: no_change")
            stored.append([SHARED, s.version, code])
        batch = [make_entry(f"e-{i}-{k}-s", SHARED_L2, f"p{i}", run), make_entry(f"e-{i}-{k}-o", own_l2(i), f"p{i}", run)]
        assert store.record_run(batch) == 2
        entries += [e.id for e in batch]
    (out / f"result-{i}.json").write_text(json.dumps({"stored": stored, "credits": credits, "entries": entries,
                                                      "span": [began, time.time()], "contended": contended[0]}))


def _run_writers(kb: Path, out: Path, procs: int = PROCS, rounds: int = ROUNDS) -> list[dict]:
    out.mkdir(parents=True, exist_ok=True)
    code = "from tests.kb.test_kb_parallel_writers import _worker_main; _worker_main()"
    env = {**os.environ, "PYTHONPATH": str(REPO) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    ps = [subprocess.Popen([sys.executable, "-c", code, str(kb), str(out), str(i), str(rounds)], cwd=str(REPO),
                           env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) for i in range(procs)]
    try:
        t0 = time.monotonic()
        while not all((out / f"ready-{i}").exists() for i in range(procs)):
            assert time.monotonic() - t0 < 60, "the writers never got ready"
            assert all(p.poll() is None for p in ps), "a writer died before the start"
            time.sleep(0.01)
        (out / "start").write_text("go")
        outputs = [p.communicate(timeout=120)[0].decode("utf-8", "replace") for p in ps]
    finally:
        for p in ps:
            if p.poll() is None:
                p.kill()
    for i, p in enumerate(ps):
        assert p.returncode == 0, f"writer {i} failed:\n{outputs[i]}"
    return [json.loads((out / f"result-{i}.json").read_text()) for i in range(procs)]


def test_several_processes_write_one_kb_at_once(tmp_path, cfg):
    kb, out = tmp_path / "kb", tmp_path / "out"
    t0 = time.monotonic()
    results = _run_writers(kb, out)
    elapsed = time.monotonic() - t0
    store = CaseStore(kb, cfg, script_runs=0)

    # scripts: every process's code is stored once, numbers 1..N with no gap and no duplicate, each file whole
    stored = [tuple(x) for r in results for x in r["stored"]]
    by_case: dict[str, list[tuple[int, str]]] = {}
    for case, version, code in stored:
        by_case.setdefault(case, []).append((version, code))
    shared = sorted(by_case[SHARED])
    expected = PROCS * (1 + len(range(0, ROUNDS, EXTRA_EVERY)))
    assert [v for v, _ in shared] == list(range(1, expected + 1))
    for v, code in shared:
        assert store.code(SHARED, v) == code, f"fix_v{v}.py is not the script its writer stored"
    info = store.find(SHARED)
    assert [s.version for s in info.scripts] == list(range(1, expected + 1))
    assert sorted(p.name for p in (kb / "cases" / SHARED).glob("fix_v*.py")) == \
        sorted(f"fix_v{v}.py" for v in range(1, expected + 1))
    for i in range(PROCS):
        own = case_id_for(own_l2(i))
        assert [s.version for s in store.find(own).scripts] == [1]
        assert store.code(own, 1) == script(i, "own")

    # credits: every call counted, every run record kept (no lost update)
    want: dict[tuple[str, int], dict[str, int]] = {}
    runs: dict[tuple[str, int], set] = {}
    for r in results:
        for case, version, result, run, rnd, n in r["credits"]:
            c = want.setdefault((case, version), {"improved": 0, "no_change": 0})
            c[result] += 1
            runs.setdefault((case, version), set()).add((run, rnd, n))
    for (case, version), c in want.items():
        s = next(x for x in store.find(case).scripts if x.version == version)
        assert (s.successes, s.failures) == (c["improved"], c["no_change"]), (case, version)
        assert {(x.run_id, x.round, x.n) for x in s.runs} == runs[(case, version)], (case, version)
    v1 = next(x for x in info.scripts if x.version == 1)
    assert v1.successes == PROCS * ROUNDS

    # errors.jsonl: every line one whole record, every entry once; the case counters saw all of them
    lines = [ln for ln in (kb / "errors.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]
    ids = [ErrorEntry.model_validate_json(ln).id for ln in lines]
    assert sorted(ids) == sorted(e for r in results for e in r["entries"])
    assert len(ids) == PROCS * ROUNDS * 2
    assert (info.seen, info.resolved) == (PROCS * ROUNDS, PROCS * ROUNDS)
    assert sorted(info.projects) == [f"p{i}" for i in range(PROCS)]
    for i in range(PROCS):
        assert store.find(case_id_for(own_l2(i))).seen == ROUNDS
    assert len(list((kb / "cases" / SHARED / "errors").glob("*.json"))) == PROCS * ROUNDS
    for p in (kb / "cases").rglob("*.json"):
        json.loads(p.read_text(encoding="utf-8"))           # every case.json / per-error file parses
    assert store.extra(SHARED).get("pin") == PIN            # a key one writer added survived everybody's rewrites
    assert SHARED in (kb / "cases.md").read_text(encoding="utf-8")
    assert store.validate() == []

    # nothing left behind; nobody broke a lock or went on without it
    names = [p.name for p in kb.rglob("*")]
    assert LOCK_NAME not in names
    assert not [n for n in names if n.startswith((PROBE_PREFIX, LOCK_NAME + ".stale"))]
    assert not [n for n in names if n.endswith(".tmp")]
    logs = "\n".join((out / f"log-{i}.txt").read_text(encoding="utf-8") for i in range(PROCS))
    assert "going on without it" not in logs
    assert "stale" not in logs
    assert not [ln for ln in logs.splitlines() if ln.startswith(("WARNING", "ERROR"))]
    # the writers really ran at the same time and really met on the lock
    spans = [r["span"] for r in results]
    assert max(s for s, _ in spans) < min(e for _, e in spans)
    assert sum(r["contended"] for r in results) > 0
    assert elapsed < 60


# --------------------------------------------------------------------------- 1. a new case met by two pods
L2 = "compile|package_does_not_exist|jakarta.xml.bind"
CID = case_id_for(L2)
CODE1 = "def fix(p):\n    p.log('v1')\n"
CODE2 = "def fix(p):\n    p.log('v2')\n"


def _stale_first_look(store: CaseStore, monkeypatch) -> dict:
    """The first read inside the lock answers "missing" (an NFS client's cached negative lookup)."""
    real = store._read
    seen = {"n": 0}

    def read(case_id, **kw):
        seen["n"] += 1
        return None if seen["n"] == 1 else real(case_id, **kw)
    monkeypatch.setattr(store, "_read", read)
    return seen


def test_a_case_another_pod_just_created_is_read_again_not_overwritten(tmp_path, cfg, monkeypatch, caplog):
    kb = tmp_path / "kb"
    a = CaseStore(kb, cfg)
    a.add_script(CID, L2, CODE1, run_id="r1", project="p1", analysis="why", solution="how")
    a.credit(CID, 1, "improved", project="p2", run_id="r2")
    a.set_pin(CID, "block:kept")
    v1 = (kb / "cases" / CID / "fix_v1.py").read_bytes()
    b = CaseStore(kb, cfg)
    seen = _stale_first_look(b, monkeypatch)
    with caplog.at_level(logging.INFO, logger="sbmig.errors"):
        s = b.add_script(CID, L2, CODE2, run_id="r3", project="p3")
    assert seen["n"] >= 2 and s.version == 2                  # the next number, never a second v1
    case = a.find(CID)
    assert [x.version for x in case.scripts] == [1, 2]
    first = case.scripts[0]
    assert (first.successes, first.status, first.projects) == (1, "proven", ["p2"])
    assert case.last_analysis == "why" and a.extra(CID)["pin"] == "block:kept"
    assert (kb / "cases" / CID / "fix_v1.py").read_bytes() == v1 and a.code(CID, 2) == CODE2
    assert any("created by another writer meanwhile" in r.getMessage() for r in caplog.records)


def test_record_run_counts_into_a_case_read_as_missing(tmp_path, cfg, monkeypatch):
    kb = tmp_path / "kb"
    a = CaseStore(kb, cfg)
    a.add_script(CID, L2, CODE1, run_id="r1", project="p1")
    assert a.record_run([make_entry("e1", L2, "p1", "r1")]) == 1
    b = CaseStore(kb, cfg)
    _stale_first_look(b, monkeypatch)
    assert b.record_run([make_entry("e2", L2, "p2", "r2")]) == 1
    case = a.find(CID)
    assert (case.seen, sorted(case.projects), [x.version for x in case.scripts]) == (2, ["p1", "p2"], [1])


def test_a_folder_with_only_per_error_files_is_a_new_case_at_once(tmp_path, cfg, caplog):
    kb = tmp_path / "kb"
    (kb / "cases" / CID / "errors").mkdir(parents=True)
    (kb / "cases" / CID / "errors" / "r0-x.json").write_text("{}")
    t0 = time.monotonic()
    with caplog.at_level(logging.WARNING):
        assert CaseStore(kb, cfg).add_script(CID, L2, CODE1).version == 1
    assert time.monotonic() - t0 < 0.5 and not caplog.records


def test_a_folder_left_without_case_json_keeps_its_scripts(tmp_path, cfg, caplog):
    kb = tmp_path / "kb"
    folder = kb / "cases" / CID
    folder.mkdir(parents=True)
    (folder / "fix_v1.py").write_text("# a writer stopped before its case.json\n")
    with caplog.at_level(logging.WARNING, logger="sbmig.errors"):
        s = CaseStore(kb, cfg).add_script(CID, L2, CODE1)
    assert s.version == 2 and (folder / "fix_v1.py").read_text() == "# a writer stopped before its case.json\n"
    assert any("holds no case.json" in r.getMessage() for r in caplog.records)
    assert not (folder / "case.json").is_dir()


def test_a_damaged_case_json_is_written_fresh_as_before(tmp_path, cfg, caplog):
    kb = tmp_path / "kb"
    folder = kb / "cases" / CID
    folder.mkdir(parents=True)
    (folder / "case.json").write_text("{not json")
    with caplog.at_level(logging.WARNING, logger="sbmig.errors"):
        assert CaseStore(kb, cfg).add_script(CID, L2, CODE1).version == 1
    assert CaseStore(kb, cfg).find(CID).scripts[0].version == 1
    msgs = [r.getMessage() for r in caplog.records]
    assert any("does not read" in m for m in msgs)


def test_a_script_number_taken_outside_the_lock_is_never_overwritten(tmp_path, cfg, monkeypatch, caplog):
    store = CaseStore(tmp_path / "kb", cfg)
    assert store.add_script(CID, L2, CODE1).version == 1
    monkeypatch.setattr(store, "_next_version", lambda case: 1)      # as if a writer without the lock chose v1 too
    with caplog.at_level(logging.WARNING, logger="sbmig.errors"):
        s = store.add_script(CID, L2, CODE2)
    assert s.version == 2 and store.code(CID, 1) == CODE1 and store.code(CID, 2) == CODE2
    assert [x.version for x in store.find(CID).scripts] == [1, 2]
    assert any("already exists" in r.getMessage() for r in caplog.records)
    assert not [p for p in (tmp_path / "kb" / "cases" / CID).iterdir() if p.name.endswith(".tmp")]


def test_without_hard_links_a_script_is_written_as_before(tmp_path, cfg, monkeypatch):
    def no_link(*a, **kw):
        raise OSError(1, "Operation not permitted")
    monkeypatch.setattr(os, "link", no_link)
    store = CaseStore(tmp_path / "kb", cfg)
    assert [store.add_script(CID, L2, c).version for c in (CODE1, CODE2)] == [1, 2]
    folder = tmp_path / "kb" / "cases" / CID
    assert (folder / "fix_v1.py").read_bytes() == CODE1.encode() and (folder / "fix_v2.py").read_bytes() == CODE2.encode()
    assert not [p for p in folder.iterdir() if p.name.endswith(".tmp")]


# --------------------------------------------------------------------------- 2. lock staleness on one clock
def _foreign_lock(root: Path, *, mtime_offset: float = 0.0, host: str = "other-host", token: str = "theirs") -> Path:
    """A lock of another pod; its mtime (``now + mtime_offset``) stands for the file server's stamp."""
    root.mkdir(parents=True, exist_ok=True)
    p = root / LOCK_NAME
    p.write_text(json.dumps({"pid": 4242, "host": host, "at": "2026-11-10T00:00:00", "token": token}))
    t = time.time() + mtime_offset
    os.utime(p, (t, t))
    return p


def _skew(monkeypatch, seconds: float) -> None:
    real = time.time
    monkeypatch.setattr(lockmod, "_now", lambda: real() + seconds)


def _messages(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records]


def test_a_pod_clock_ahead_never_breaks_a_fresh_lock(tmp_path, monkeypatch, caplog):
    _skew(monkeypatch, 1000.0)                               # this pod's clock 1,000 s ahead of the file server
    p = _foreign_lock(tmp_path)
    with caplog.at_level(logging.INFO, logger="sbmig.kb"):
        assert not lockmod._stale(p, json.loads(p.read_text()), 60)      # the old comparison: 1000 > 60 → broken
        assert abs(lockmod._fs_offset(tmp_path) + 1000.0) < 5
        threading.Timer(0.6, p.unlink).start()                           # the holder releases it
        t0 = time.monotonic()
        with kb_lock(tmp_path, 60) as held:
            waited = time.monotonic() - t0
            assert held and json.loads((tmp_path / LOCK_NAME).read_text())["token"] != "theirs"
    assert waited >= 0.5
    msgs = _messages(caplog)
    assert not [m for m in msgs if "stale" in m or "going on without it" in m]
    assert len([m for m in msgs if "differ by" in m]) == 1                         # said once
    assert not list(tmp_path.glob(PROBE_PREFIX + "*"))


def test_a_pod_clock_behind_still_breaks_an_old_lock(tmp_path, monkeypatch, caplog):
    _skew(monkeypatch, -1000.0)                              # this pod's clock 1,000 s behind the file server
    _foreign_lock(tmp_path, mtime_offset=-120.0)            # a pod died holding it two minutes ago
    t0 = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="sbmig.kb"), kb_lock(tmp_path, 60) as held:
        assert held and json.loads((tmp_path / LOCK_NAME).read_text())["token"] != "theirs"
    assert time.monotonic() - t0 < 5                         # the old comparison: age -880 s → waits 60 s, no lock
    assert any("stale" in m for m in _messages(caplog))
    assert not list(tmp_path.glob(PROBE_PREFIX + "*"))


def test_a_failing_probe_keeps_the_plain_comparison(tmp_path, monkeypatch):
    real_open = os.open

    def no_probe(path, *a, **kw):
        if os.path.basename(str(path)).startswith(PROBE_PREFIX):
            raise PermissionError(13, "probe refused")
        return real_open(path, *a, **kw)
    monkeypatch.setattr(os, "open", no_probe)
    plain = tmp_path / "plain"
    fresh = _foreign_lock(plain)
    assert lockmod._fs_offset(plain) == 0.0
    assert not lockmod._stale(fresh, json.loads(fresh.read_text()), 60)
    old = _foreign_lock(tmp_path / "old", mtime_offset=-120.0)
    assert lockmod._stale(old, json.loads(old.read_text()), 60)
    _skew(monkeypatch, 1000.0)                               # without a probe the pod's own clock decides, as before
    ahead = _foreign_lock(tmp_path / "ahead")
    assert lockmod._stale(ahead, json.loads(ahead.read_text()), 60)
    assert not [p for p in tmp_path.rglob("*") if p.name.startswith(PROBE_PREFIX)]


def test_the_uncontended_path_writes_no_probe_and_a_waiter_probes_once(tmp_path, cfg, monkeypatch):
    calls: list[Path] = []
    real = lockmod._probe_offset
    monkeypatch.setattr(lockmod, "_probe_offset", lambda root: calls.append(Path(root)) or real(root))
    store = CaseStore(tmp_path / "kb", cfg)
    with kb_lock(tmp_path / "kb", 5):
        pass
    store.add_script(CID, L2, CODE1, run_id="r1", project="p1")
    store.credit(CID, 1, "improved", project="p2", run_id="r2")
    store.record_run([make_entry("e1", L2, "p1", "r1")])
    assert calls == []                                       # no contention: not one extra file operation
    p = _foreign_lock(tmp_path / "kb")
    threading.Timer(0.5, p.unlink).start()
    with kb_lock(tmp_path / "kb", 30) as held:
        assert held
    assert len(calls) == 1                                   # the waiter looked many times, measured once
    assert not list((tmp_path / "kb").glob(PROBE_PREFIX + "*"))


def test_a_lock_turning_stale_at_the_deadline_is_broken_not_given_up(tmp_path, caplog):
    # the lock of a pod killed inside its section, seen from its first moment (and stamped a hair late):
    # it turns stale just after the waiter's deadline — the waiter takes it instead of going on without it
    _foreign_lock(tmp_path, mtime_offset=0.5)
    with caplog.at_level(logging.WARNING, logger="sbmig.kb"), kb_lock(tmp_path, 1.0) as held:
        assert held
    msgs = _messages(caplog)
    assert any("stale" in m for m in msgs) and not any("going on without it" in m for m in msgs)


def test_break_leaves_a_lock_taken_after_the_judgement_alone(tmp_path):
    p = _foreign_lock(tmp_path, token="newer")
    lockmod._break(p, {"pid": 4242, "host": "other-host", "token": "judged-stale"})
    assert json.loads(p.read_text())["token"] == "newer"
    assert not [x for x in tmp_path.iterdir() if x.name.startswith(LOCK_NAME + ".stale")]


def test_three_pods_meet_one_new_case_at_once(tmp_path):
    # the user's question in its smallest form: three writers, one new case, two scripts each, at the same moment
    results = _run_writers(tmp_path / "kb", tmp_path / "out", procs=3, rounds=1)
    versions = sorted(x[1] for r in results for x in r["stored"] if x[0] == SHARED)
    assert versions == list(range(1, 7))                       # first + one extra each: 1..6, no duplicate
    store = CaseStore(tmp_path / "kb", None)
    assert [s.version for s in store.find(SHARED).scripts] == list(range(1, 7))
    assert not (tmp_path / "kb" / LOCK_NAME).exists()
