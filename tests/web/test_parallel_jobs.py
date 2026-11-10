"""v2.1z-ae (2026-11-10, the user: "3명까지는 빌드파드가 돌아가는 구조 … 나머지는 대기하라고 알림"): up to `SBMIG_JOB_MAX_PARALLEL`
build Jobs at a time in the Job modes (code default 1, in-process always 1). `_FifoTurns` became a FIFO line plus numbered slots
(strict FIFO, the lowest free number from 1, `occupy` for restart recovery whatever the limit); a Job run carries its slot in its env
(`SBMIG_JOB_SLOT`), in `control/state.json` (`slot`, written with `creating`) and in `status()`; a waiting run reports its place
(`queue`), every status answer the picture (`parallel`), and the SSE stream re-sends `status` when a waiting run moves up or starts."""
from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sbmig.core.config import PACKAGE_ROOT
from sbmig.web import contract, create_app
from sbmig.web.contract import OWNER_LABEL, RUN_LABEL
from sbmig.web.k8s import K8sClient, K8sError, job_name
from sbmig.web.localjob import LocalJobClient
from sbmig.web.runs import RunManager, _FifoTurns

from .test_jobmode_hardening import TEMPLATE, ZIP, Cluster, _journal, _mgr, _seed, _state, _wait, pod_writes

CFG = ["artifacts.local-e2e.yml"]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    shutil.copy(PACKAGE_ROOT / "config" / "artifacts.local-e2e.yml", repo / "config" / "artifacts.local-e2e.yml")
    (repo / "config" / "default.yml").write_text("x: 1\n")
    return repo


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setenv("SBMIG_JOB_POLL_S", "0.05")
    monkeypatch.delenv(contract.MAX_PARALLEL_ENV, raising=False)


@pytest.fixture
def managers():
    """Every manager a test builds is shut down at the end: no watcher thread outlives its test."""
    made: list[RunManager] = []
    yield made
    for mgr in made:
        mgr.shutdown("test teardown")
        for run in list(mgr.runs.values()):
            run.done.wait(10)


def _env(manifest: dict) -> dict[str, str]:
    return {e["name"]: e.get("value") for e in manifest["spec"]["template"]["spec"]["containers"][0]["env"]}


def _rid(manifest: dict) -> str:
    return manifest["metadata"]["labels"][RUN_LABEL]


def _slots_by_run(cluster: Cluster) -> dict[str, str | None]:
    return {_rid(m): _env(m).get(contract.SLOT_ENV) for m in cluster.created}


def _finish(cluster: Cluster, out: Path, rid: str) -> None:
    """The pod of `rid` writes its result and exits."""
    pod_writes(out / rid, rid, attempt=1)
    cluster.jobs[job_name(rid)]["phases"] = ["Succeeded"]


def _hanging(out: Path) -> Cluster:
    """Jobs that run until the test lets them end (`_finish`)."""
    return Cluster(out, phases=["Running"], pod_attempt=False)


def _seed_slot(out: Path, rid: str, state: str, slot: int | None, accepted_at: str = "2026-11-09T01:00:00Z") -> None:
    _seed(out, rid, state, accepted_at=accepted_at, owner=f"o-{rid}")
    cs = _state(out, rid)
    cs.slot = slot
    cs.write(out / rid)


# ---- _FifoTurns
def _enter(turns: _FifoTurns, key: str, got: list, release: dict[str, threading.Event]) -> threading.Thread:
    release[key] = threading.Event()

    def body() -> None:
        with turns.turn(key) as slot:
            got.append((key, slot))
            release[key].wait(10)
    t = threading.Thread(target=body, daemon=True)
    t.start()
    return t


def test_three_slots_the_fourth_waits_and_takes_the_freed_number():
    turns = _FifoTurns(3)
    for k in "abcd":
        turns.join(k)
    got: list[tuple[str, int]] = []
    release: dict[str, threading.Event] = {}
    threads = [_enter(turns, k, got, release) for k in "dcba"]          # started in reverse: the line decides, not the threads
    assert _wait(lambda: len(got) == 3)
    assert got == [("a", 1), ("b", 2), ("c", 3)]
    assert turns.counts() == (3, 1) and turns.position("d") == 1 and turns.position("a") is None
    assert [turns.slot_of(k) for k in "abcd"] == [1, 2, 3, None]
    release["b"].set()
    assert _wait(lambda: len(got) == 4)
    assert got[3] == ("d", 2) and turns.counts() == (3, 0)                # the freed number is reused
    for k in "acd":
        release[k].set()
    for t in threads:
        t.join(5)
    assert turns.counts() == (0, 0)


def test_strict_fifo_among_several_waiters():
    turns = _FifoTurns(2)
    keys = list("abcdef")
    for k in keys:
        turns.join(k)
    got: list[tuple[str, int]] = []
    release: dict[str, threading.Event] = {}
    threads = [_enter(turns, k, got, release) for k in reversed(keys)]
    assert _wait(lambda: len(got) == 2)
    assert [k for k, _ in got] == ["a", "b"]
    assert [turns.position(k) for k in "cdef"] == [1, 2, 3, 4]
    for i, k in enumerate(keys):
        release[k].set()
        want = min(len(keys), i + 3)
        assert _wait(lambda want=want: len(got) == want)
    assert [k for k, _ in got] == keys                                   # nobody overtook
    assert {s for _, s in got} == {1, 2}
    for t in threads:
        t.join(5)


def test_occupy_ignores_the_limit_and_keeps_numbers_unique():
    turns = _FifoTurns(1)
    turns.join("w")
    assert turns.occupy("x", prefer=2) == 2
    assert turns.occupy("y", prefer=2) == 1                              # 2 is taken: the lowest free one
    assert turns.occupy("z") == 3 and turns.occupy("x", prefer=7) == 2   # a holder keeps its number
    assert turns.occupy("v", prefer="junk") == 4                         # an unreadable recorded number is ignored
    assert turns.counts() == (4, 1) and turns.position("w") == 1
    got: list[tuple[str, int]] = []
    release: dict[str, threading.Event] = {}
    t = _enter(turns, "w", got, release)
    for k in "xyz":
        turns.leave(k)
        assert not _wait(lambda: bool(got), timeout=0.15)                # still at or above the limit of 1
    turns.leave("v")
    assert _wait(lambda: got == [("w", 1)])
    release["w"].set()
    t.join(5)


def test_occupy_takes_a_waiting_key_out_of_the_line_and_turn_yields_its_slot_at_once():
    turns = _FifoTurns(1)
    turns.join("a")
    turns.join("b")
    assert turns.occupy("b", prefer=5) == 5 and turns.position("b") is None and turns.position("a") == 1
    turns.join("b")                                                      # holding a slot: joining again is a no-op
    assert turns.counts() == (1, 1)
    with turns.turn("b") as slot:                                        # over the limit, not the head: no waiting
        assert slot == 5
    assert turns.slot_of("b") is None and turns.counts() == (0, 1)


def test_one_slot_is_the_old_one_at_a_time():
    turns = _FifoTurns()
    assert turns.slots == 1
    for k in "xy":
        turns.join(k)
    got: list[tuple[str, int]] = []
    release: dict[str, threading.Event] = {}
    ty = _enter(turns, "y", got, release)
    tx = _enter(turns, "x", got, release)
    assert _wait(lambda: got == [("x", 1)])
    assert not _wait(lambda: len(got) > 1, timeout=0.2) and turns.position("y") == 1
    release["x"].set()
    assert _wait(lambda: got == [("x", 1), ("y", 1)])
    release["y"].set()
    tx.join(5)
    ty.join(5)


# ---- the limit
def test_the_default_is_one_job_at_a_time(tmp_path: Path, repo: Path, managers):
    out = tmp_path / "out"
    cluster = _hanging(out)
    mgr = _mgr(tmp_path, repo, cluster)
    managers.append(mgr)
    assert mgr.max_parallel == 1
    a = mgr.start(ZIP, "a.zip", CFG)
    assert _wait(lambda: len(cluster.created) == 1)
    b = mgr.start(ZIP, "b.zip", CFG)
    assert not _wait(lambda: len(cluster.created) > 1, timeout=0.3)
    st = mgr.status(b)
    assert st["state"] == "queued" and st["queue"] == {"position": 1, "ahead": 0} and "slot" not in st
    assert st["parallel"] == {"slots": 1, "running": 1, "waiting": 1}
    _finish(cluster, out, a)
    assert _wait(lambda: len(cluster.created) == 2)
    assert _slots_by_run(cluster) == {a: "1", b: "1"}
    _finish(cluster, out, b)
    assert mgr.runs[a].done.wait(10) and mgr.runs[b].done.wait(10)
    assert mgr.runs[a].exit_code == 0 and mgr.runs[b].exit_code == 0


def test_the_environment_sets_the_limit_and_the_argument_wins(tmp_path: Path, repo: Path, monkeypatch, managers):
    monkeypatch.setenv(contract.MAX_PARALLEL_ENV, "3")
    managers.append(m1 := _mgr(tmp_path, repo, Cluster(tmp_path / "out")))
    assert m1.max_parallel == 3 and m1.parallel() == {"slots": 3, "running": 0, "waiting": 0}
    monkeypatch.setenv(contract.MAX_PARALLEL_ENV, "2")
    managers.append(m2 := _mgr(tmp_path, repo, Cluster(tmp_path / "out"), max_parallel=4))
    assert m2.max_parallel == 4
    managers.append(m3 := RunManager(tmp_path / "out3", repo, mode="local-job", job_client=LocalJobClient(repo),
                                     job_template=TEMPLATE))
    assert m3.max_parallel == 2                                          # local-job counts it too


@pytest.mark.parametrize("value", ["abc", "0", "-2", "1.5"])
def test_an_invalid_limit_is_one_with_a_warning(tmp_path: Path, repo: Path, monkeypatch, caplog, managers, value):
    monkeypatch.setenv(contract.MAX_PARALLEL_ENV, value)
    with caplog.at_level(logging.WARNING, logger="sbmig.web"):
        managers.append(mgr := _mgr(tmp_path, repo, Cluster(tmp_path / "out")))
    assert mgr.max_parallel == 1
    assert any(contract.MAX_PARALLEL_ENV in r.getMessage() and repr(value) in r.getMessage() for r in caplog.records)


def test_in_process_always_runs_one_and_says_so(tmp_path: Path, repo: Path, monkeypatch, caplog, managers):
    with caplog.at_level(logging.WARNING, logger="sbmig.web"):
        managers.append(mgr := RunManager(tmp_path / "out", repo, max_parallel=3))
    assert mgr.effective_mode == "in-process" and mgr.max_parallel == 1 and mgr.parallel()["slots"] == 1
    assert any("ignored in in-process mode" in r.getMessage() for r in caplog.records)
    caplog.clear()
    monkeypatch.setenv(contract.MAX_PARALLEL_ENV, "1")
    with caplog.at_level(logging.WARNING, logger="sbmig.web"):
        managers.append(RunManager(tmp_path / "out2", repo))
    assert not [r for r in caplog.records if contract.MAX_PARALLEL_ENV in r.getMessage()]


def test_a_cluster_less_k8s_job_keeps_the_limit_as_local_job(tmp_path: Path, repo: Path, managers):
    managers.append(mgr := RunManager(tmp_path / "out", repo, mode="k8s-job", job_template=TEMPLATE, max_parallel=3))
    assert mgr.effective_mode == "local-job" and mgr.max_parallel == 3


# ---- three Jobs at a time
def test_three_jobs_run_and_the_fourth_waits_for_a_free_slot(tmp_path: Path, repo: Path, managers):
    out = tmp_path / "out"
    cluster = _hanging(out)
    managers.append(mgr := _mgr(tmp_path, repo, cluster, max_parallel=3))
    rids = [mgr.start(ZIP, f"p{i}.zip", CFG) for i in range(4)]
    assert _wait(lambda: len(cluster.created) == 3)
    assert not _wait(lambda: len(cluster.created) > 3, timeout=0.3)
    assert _slots_by_run(cluster) == {rids[0]: "1", rids[1]: "2", rids[2]: "3"}
    fourth = mgr.status(rids[3])
    assert fourth["state"] == "queued" and fourth["queue"] == {"position": 1, "ahead": 0} and "slot" not in fourth
    assert fourth["parallel"] == {"slots": 3, "running": 3, "waiting": 1}
    assert mgr.status("web-unknown")["parallel"] == {"slots": 3, "running": 3, "waiting": 1}     # on every answer
    assert _wait(lambda: all(_state(out, r).state == "running" for r in rids[:3]))
    for i, r in enumerate(rids[:3], start=1):
        st = mgr.status(r)
        assert st["state"] == "running" and st["slot"] == i and "queue" not in st
        assert _state(out, r).slot == i
        assert [e["slot"] for e in _journal(out, r) if e["event"] == "job_created"] == [i]
    _finish(cluster, out, rids[1])                                       # slot 2 frees up
    assert _wait(lambda: len(cluster.created) == 4)
    assert _slots_by_run(cluster)[rids[3]] == "2" and mgr.runs[rids[1]].done.wait(10)
    assert _wait(lambda: mgr.status(rids[3])["state"] == "running")
    assert mgr.status(rids[3])["slot"] == 2 and _state(out, rids[3]).slot == 2
    for r in (rids[0], rids[2], rids[3]):
        _finish(cluster, out, r)
    assert all(mgr.runs[r].done.wait(10) for r in rids)
    assert [mgr.runs[r].exit_code for r in rids] == [0, 0, 0, 0] and mgr.parallel() == {"slots": 3, "running": 0, "waiting": 0}
    assert all(_state(out, r).state == "finished" for r in rids)
    mgr.runs.pop(rids[3])                                                # evicted: the slot comes from control/state.json
    assert mgr.status(rids[3])["slot"] == 2


def test_an_api_outage_on_one_slot_holds_that_slot_only(tmp_path: Path, repo: Path, monkeypatch, managers):
    monkeypatch.setenv("SBMIG_JOB_API_GRACE_S", "0.1")
    out = tmp_path / "out"
    cluster = Cluster(out, phases=["Running", "Succeeded"])
    down = {"on": True}
    real = cluster.job_state

    def job_state(name):
        if down["on"] and cluster.created and name == cluster.created[0]["metadata"]["name"]:
            raise K8sError("GET jobs: ConnectionError: connection refused")
        return real(name)
    cluster.job_state = job_state
    managers.append(mgr := _mgr(tmp_path, repo, cluster, max_parallel=2))
    first = mgr.start(ZIP, "a.zip", CFG)
    assert _wait(lambda: len(cluster.created) == 1)
    second = mgr.start(ZIP, "b.zip", CFG)
    assert mgr.runs[second].done.wait(10) and mgr.runs[second].exit_code == 0     # the other slot finishes on its own
    run = mgr.runs[first]
    assert not run.done.is_set() and run.slot == 1 and mgr.parallel()["running"] == 1
    assert _wait(lambda: "keeps its slot 1" in (mgr.status(first)["job"].get("note") or ""))
    third = mgr.start(ZIP, "c.zip", CFG)                                 # takes the freed slot 2, never the silent slot 1
    assert mgr.runs[third].done.wait(10) and _slots_by_run(cluster)[third] == "2"
    assert not run.done.is_set()
    down["on"] = False
    assert run.done.wait(10) and run.exit_code == 0 and _state(out, first).state == "finished"
    events = [e["event"] for e in _journal(out, first)]
    assert events.count("job_api_unavailable") == 1 and events.count("job_api_back") == 1


# ---- restart recovery
def test_recovery_reoccupies_the_recorded_slot_before_a_queued_run_takes_one(tmp_path: Path, repo: Path, managers):
    out = tmp_path / "out"
    # the queued run was accepted FIRST: in acceptance order it would join before the running one re-occupies its number
    _seed_slot(out, "web-q", "queued", None, accepted_at="2026-11-09T01:00:00Z")
    _seed_slot(out, "web-r", "running", 1, accepted_at="2026-11-09T02:00:00Z")
    cluster = _hanging(out)
    cluster.add(job_name("web-r"), {RUN_LABEL: "web-r", OWNER_LABEL: "o-web-r"})
    managers.append(mgr := _mgr(tmp_path, repo, cluster, max_parallel=2))
    assert mgr.recovered == ["web-r", "web-q"] and mgr.runs["web-r"].slot == 1
    assert _wait(lambda: len(cluster.created) == 1)
    assert _slots_by_run(cluster) == {"web-q": "2"} and _state(out, "web-q").slot == 2
    assert mgr.status("web-r")["slot"] == 1 and mgr.parallel() == {"slots": 2, "running": 2, "waiting": 0}
    for r in ("web-r", "web-q"):
        _finish(cluster, out, r)
    assert mgr.runs["web-r"].done.wait(10) and mgr.runs["web-q"].done.wait(10)
    assert _state(out, "web-r").slot == 1 and _state(out, "web-r").state == "finished"   # the recorded number survives


def test_recovery_with_a_recorded_slot_two_leaves_slot_one_to_the_queue(tmp_path: Path, repo: Path, managers):
    out = tmp_path / "out"
    _seed_slot(out, "web-a", "running", 2)
    _seed_slot(out, "web-b", "queued", None, accepted_at="2026-11-09T03:00:00Z")
    cluster = _hanging(out)
    cluster.add(job_name("web-a"), {RUN_LABEL: "web-a", OWNER_LABEL: "o-web-a"})
    managers.append(mgr := _mgr(tmp_path, repo, cluster, max_parallel=3))
    assert mgr.runs["web-a"].slot == 2 and mgr.status("web-a")["slot"] == 2
    assert _wait(lambda: len(cluster.created) == 1) and _slots_by_run(cluster) == {"web-b": "1"}
    c = mgr.start(ZIP, "c.zip", CFG)                                     # a new upload: the next free number
    assert _wait(lambda: len(cluster.created) == 2) and _slots_by_run(cluster)[c] == "3"
    for r in ("web-a", "web-b", c):
        _finish(cluster, out, r)
    assert all(mgr.runs[r].done.wait(10) for r in ("web-a", "web-b", c))


def test_a_running_state_without_slot_reoccupies_slot_one(tmp_path: Path, repo: Path, managers):
    out = tmp_path / "out"
    _seed(out, "web-old", "running", owner="o-old")
    sp = out / "web-old" / "control" / "state.json"                     # written before v2.1z-ae: no `slot` key at all
    data = json.loads(sp.read_text(encoding="utf-8"))
    data.pop("slot", None)
    sp.write_text(json.dumps(data), encoding="utf-8")
    assert _state(out, "web-old").slot is None
    cluster = _hanging(out)
    cluster.add(job_name("web-old"), {RUN_LABEL: "web-old", OWNER_LABEL: "o-old"})
    managers.append(mgr := _mgr(tmp_path, repo, cluster, max_parallel=3))
    assert mgr.runs["web-old"].slot == 1 and mgr.status("web-old")["slot"] == 1
    _finish(cluster, out, "web-old")
    assert mgr.runs["web-old"].done.wait(10) and mgr.runs["web-old"].exit_code == 0


def test_a_lowered_limit_adopts_every_running_job_and_the_queue_waits(tmp_path: Path, repo: Path, managers):
    out = tmp_path / "out"
    _seed_slot(out, "web-1", "running", 1, accepted_at="2026-11-09T01:00:00Z")
    _seed_slot(out, "web-2", "created", 2, accepted_at="2026-11-09T02:00:00Z")
    _seed_slot(out, "web-3", "queued", None, accepted_at="2026-11-09T03:00:00Z")
    cluster = _hanging(out)
    for r in ("web-1", "web-2"):
        cluster.add(job_name(r), {RUN_LABEL: r, OWNER_LABEL: f"o-{r}"})
    managers.append(mgr := _mgr(tmp_path, repo, cluster))                # the limit went down to 1
    assert mgr.max_parallel == 1 and mgr.recovered == ["web-1", "web-2", "web-3"]
    assert [mgr.runs[r].slot for r in ("web-1", "web-2")] == [1, 2]
    assert mgr.parallel() == {"slots": 1, "running": 2, "waiting": 1}
    assert mgr.status("web-3")["queue"] == {"position": 1, "ahead": 0}
    _finish(cluster, out, "web-1")
    assert mgr.runs["web-1"].done.wait(10)
    assert not _wait(lambda: bool(cluster.created), timeout=0.3)        # one still holds a slot: not below the limit
    _finish(cluster, out, "web-2")
    assert _wait(lambda: len(cluster.created) == 1) and _slots_by_run(cluster) == {"web-3": "1"}
    _finish(cluster, out, "web-3")
    assert mgr.runs["web-3"].done.wait(10) and mgr.runs["web-3"].exit_code == 0


# ---- the SSE stream follows a waiting run
def test_the_stream_of_a_waiting_run_resends_status_when_it_moves_up_and_starts(tmp_path: Path, repo: Path, managers):
    out = tmp_path / "out"
    cluster = _hanging(out)
    managers.append(mgr := _mgr(tmp_path, repo, cluster))
    a = mgr.start(ZIP, "a.zip", CFG)
    assert _wait(lambda: len(cluster.created) == 1)
    b = mgr.start(ZIP, "b.zip", CFG)
    c = mgr.start(ZIP, "c.zip", CFG)
    statuses: list[tuple[str, int | None]] = []

    def consume() -> None:
        for kind, data in mgr.events(c):
            if kind == "status":
                statuses.append((data["state"], (data.get("queue") or {}).get("position")))
            if kind == "end":
                return
    t = threading.Thread(target=consume, daemon=True)
    t.start()
    assert _wait(lambda: statuses == [("queued", 2)])
    _finish(cluster, out, a)
    assert _wait(lambda: statuses == [("queued", 2), ("queued", 1)])
    assert _wait(lambda: len(cluster.created) == 2)
    _finish(cluster, out, b)
    assert _wait(lambda: statuses == [("queued", 2), ("queued", 1), ("running", None)])
    assert _wait(lambda: len(cluster.created) == 3)
    _finish(cluster, out, c)
    t.join(10)
    assert not t.is_alive()
    assert statuses == [("queued", 2), ("queued", 1), ("running", None), ("finished", None)]


# ---- the routes
def test_the_routes_carry_the_parallel_picture(tmp_path: Path, repo: Path, monkeypatch, managers):
    monkeypatch.setenv("SBMIG_JOB_TEMPLATE", str(TEMPLATE))
    out = tmp_path / "out"
    cluster = _hanging(out)
    app = create_app(out, repo, mode="k8s-job", job_client=cluster, max_parallel=2)
    mgr = app.state.manager
    managers.append(mgr)
    client = TestClient(app)
    assert client.get("/api/configs").json()["max_parallel"] == 2
    rids = [mgr.start(ZIP, f"p{i}.zip", CFG) for i in range(3)]
    assert _wait(lambda: len(cluster.created) == 2)
    body = client.get("/api/runs").json()
    assert body["parallel"] == {"slots": 2, "running": 2, "waiting": 1}
    rows = {r["run_id"]: r for r in body["runs"]}
    assert rows[rids[2]]["queue"] == {"position": 1, "ahead": 0} and "slot" not in rows[rids[2]]
    assert rows[rids[0]]["slot"] == 1 and rows[rids[1]]["slot"] == 2 and "queue" not in rows[rids[0]]
    assert all("parallel" not in r for r in body["runs"])
    one = client.get(f"/api/runs/{rids[2]}").json()
    assert one["queue"] == {"position": 1, "ahead": 0} and one["parallel"]["waiting"] == 1
    for r in rids[:2]:
        _finish(cluster, out, r)
    assert _wait(lambda: len(cluster.created) == 3)
    _finish(cluster, out, rids[2])
    assert all(mgr.runs[r].done.wait(10) for r in rids)


def test_the_in_process_routes_say_one_at_a_time(tmp_path: Path, repo: Path):
    client = TestClient(create_app(tmp_path / "out", repo))
    assert client.get("/api/configs").json()["max_parallel"] == 1
    assert client.get("/api/runs").json()["parallel"] == {"slots": 1, "running": 0, "waiting": 0}


# ---- shared clients under several watcher threads
def test_k8s_client_gives_each_thread_its_own_session_unless_one_was_injected():
    c = K8sClient("https://k8s.invalid:6443", "tok", "ns")
    seen: list[object] = []
    ts = [threading.Thread(target=lambda: seen.extend([c.session, c.session])) for _ in range(2)]
    for t in ts:
        t.start()
        t.join(5)
    assert seen[0] is seen[1] and seen[2] is seen[3] and seen[0] is not seen[2]   # one per thread, reused within it
    fake = object()
    c2 = K8sClient("https://k8s.invalid:6443", "tok", "ns", session=fake)
    got: list[object] = []
    t = threading.Thread(target=lambda: got.append(c2.session))
    t.start()
    t.join(5)
    assert got == [fake] and c2.session is fake


def test_the_lazy_job_client_is_built_once_under_concurrent_first_use(tmp_path: Path, repo: Path, monkeypatch, managers):
    built: list[object] = []
    real_init = LocalJobClient.__init__
    gate = threading.Barrier(6)

    def slow_init(self, *a, **kw):
        built.append(self)
        time.sleep(0.05)                                                 # widen the window an unlocked check would lose
        real_init(self, *a, **kw)
    monkeypatch.setattr(LocalJobClient, "__init__", slow_init)
    managers.append(mgr := RunManager(tmp_path / "out", repo, mode="local-job", job_template=TEMPLATE, max_parallel=3))
    got: list[object] = []

    def ask() -> None:
        gate.wait(5)
        got.append(mgr._job_client_or_fail())
    ts = [threading.Thread(target=ask) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(5)
    assert len(built) == 1 and len(got) == 6 and all(g is got[0] for g in got)


def test_the_slot_reaches_the_local_worker_env(tmp_path: Path, repo: Path):
    assert contract.SLOT_ENV in contract.RUN_ENV
    manifest = {"spec": {"template": {"spec": {"containers": [{"name": "build", "env": [
        {"name": contract.SLOT_ENV, "value": "2"}, {"name": "SBMIG_KB_DIR", "value": "/data/kb-v2"}]}]}}}}
    assert LocalJobClient._env_of(manifest) == {contract.SLOT_ENV: "2"}

