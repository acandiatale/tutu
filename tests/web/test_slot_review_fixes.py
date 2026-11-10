"""Review 2026-11-10 of the parallel Job slots (v2.1z-ae): two live pods never use the same slot number, and the slot a run
holds in memory is the one its pod was started with.

(1) a Job run keeps its slot until its terminal control state was written (or the write failed);
(2) recovery honours a recorded slot even when another recovered run recorded the same one;
(3) any exception of the API client is "API unavailable" — it never fails the run nor frees the slot; the client turns
    an unreadable 2xx body into K8sError;
(4) a Failed / Succeeded Job whose pod is still running or terminating has not ended;
(5) a stop on a waiting run takes it out of the line at once;
(6) `_await_status` leaves promptly on shutdown."""
from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from pathlib import Path

import pytest

from sbmig.core.config import PACKAGE_ROOT
from sbmig.web import contract
from sbmig.web.contract import OWNER_LABEL, RUN_LABEL, ControlState
from sbmig.web.k8s import JobState, K8sClient, K8sError, job_name
from sbmig.web.runs import RunManager, _FifoTurns, _LeftLine

from .test_jobmode_hardening import TEMPLATE, ZIP, Cluster, _seed, _state, _wait, pod_writes

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
    made: list[RunManager] = []
    yield made
    for mgr in made:
        mgr.shutdown("test teardown")
        for run in list(mgr.runs.values()):
            run.done.wait(10)


def _mgr(tmp_path: Path, repo: Path, cluster, managers, **kw) -> RunManager:
    m = RunManager(tmp_path / "out", repo, mode="k8s-job", job_client=cluster, job_template=TEMPLATE, **kw)
    managers.append(m)
    return m


def _env(m: dict) -> dict[str, str]:
    return {e["name"]: e.get("value") for e in m["spec"]["template"]["spec"]["containers"][0]["env"]}


def _rid(m: dict) -> str:
    return m["metadata"]["labels"][RUN_LABEL]


def _finish(cluster: Cluster, out: Path, rid: str) -> None:
    pod_writes(out / rid, rid, attempt=1)
    cluster.jobs[job_name(rid)]["phases"] = ["Succeeded"]


# ---- (1) the terminal control state is written while the slot is still held
def test_the_slot_is_held_until_the_terminal_state_is_on_disk(tmp_path: Path, repo: Path, monkeypatch, managers):
    out = tmp_path / "out"
    cluster = Cluster(out, phases=["Running"], pod_attempt=False)
    order: list[tuple[str, str]] = []
    real_create = cluster.create_job

    def create(manifest):
        order.append(("create", _rid(manifest)))
        return real_create(manifest)
    cluster.create_job = create                                    # type: ignore[method-assign]
    mgr = _mgr(tmp_path, repo, cluster, managers)
    held_at_write: dict[str, int | None] = {}
    real_write = ControlState.write

    def write(self, run_dir):
        if self.terminal:
            held_at_write[self.run_id] = mgr._turns.slot_of(self.run_id)
            order.append(("terminal", self.run_id))
        return real_write(self, run_dir)
    monkeypatch.setattr(ControlState, "write", write)
    a = mgr.start(ZIP, "a.zip", CFG)
    b = mgr.start(ZIP, "b.zip", CFG)
    assert _wait(lambda: len(cluster.created) == 1)
    _finish(cluster, out, a)
    assert _wait(lambda: len(cluster.created) == 2)
    assert held_at_write[a] == 1                                   # written while slot 1 was still a's
    assert order.index(("terminal", a)) < order.index(("create", b))
    assert _state(out, a).state == "finished" and mgr.runs[a].done.wait(5)
    _finish(cluster, out, b)
    assert mgr.runs[b].done.wait(10) and held_at_write[b] == 1


def test_a_failed_terminal_write_still_frees_the_slot(tmp_path: Path, repo: Path, monkeypatch, managers, caplog):
    out = tmp_path / "out"
    cluster = Cluster(out, phases=["Running"], pod_attempt=False)
    mgr = _mgr(tmp_path, repo, cluster, managers)
    real_write = ControlState.write
    a_box: dict[str, str] = {}

    def write(self, run_dir):
        if self.terminal and self.run_id == a_box.get("a"):
            raise OSError("volume hiccup")
        return real_write(self, run_dir)
    monkeypatch.setattr(ControlState, "write", write)
    a = mgr.start(ZIP, "a.zip", CFG)
    a_box["a"] = a
    b = mgr.start(ZIP, "b.zip", CFG)
    assert _wait(lambda: len(cluster.created) == 1)
    with caplog.at_level(logging.ERROR, logger="sbmig.web"):
        _finish(cluster, out, a)
        assert mgr.runs[a].done.wait(10)
    assert any("control state finished not written" in r.getMessage() for r in caplog.records)
    assert _state(out, a).state == "running"                       # the next Main would adopt it and read the result
    assert mgr._turns.slot_of(a) is None
    assert _wait(lambda: len(cluster.created) == 2) and _rid(cluster.created[1]) == b
    assert mgr.runs[a].exit_code == 0 and ("finished", {"exit_code": 0, "success": True}) in mgr.runs[a].history


# ---- (2) recovery with a duplicate recorded slot
def test_two_recovered_runs_on_one_recorded_slot_keep_it_and_a_new_run_gets_another(tmp_path: Path, repo: Path, managers,
                                                                                      caplog):
    """Was the reviewer's repro R1: the second recovered run was given another number in memory while its pod used 1, and
    the next upload's Job got slot 1 too."""
    out = tmp_path / "out"
    _seed(out, "web-a", "running", accepted_at="2026-11-09T01:00:00Z", owner="o-a")   # stale: its Job is gone, result on disk
    cs = _state(out, "web-a")
    cs.slot = 1
    cs.write(out / "web-a")
    pod_writes(out / "web-a", "web-a", attempt=1)
    _seed(out, "web-b", "running", accepted_at="2026-11-09T02:00:00Z", owner="o-b")   # really running with SBMIG_JOB_SLOT=1
    cs = _state(out, "web-b")
    cs.slot = 1
    cs.write(out / "web-b")
    cluster = Cluster(out, phases=["Running"], pod_attempt=False)
    cluster.add(job_name("web-b"), {RUN_LABEL: "web-b", OWNER_LABEL: "o-b"})
    with caplog.at_level(logging.WARNING, logger="sbmig.web"):
        mgr = _mgr(tmp_path, repo, cluster, managers, max_parallel=3)
    assert any("web-b" in r.getMessage() and "web-a" in r.getMessage() and "both recorded slot 1" in r.getMessage()
               for r in caplog.records)
    b = mgr.runs["web-b"]
    assert b.slot == 1 and mgr.status("web-b")["slot"] == 1
    assert mgr.runs["web-a"].done.wait(10)
    assert mgr._turns.slot_of("web-b") == 1 and mgr.parallel()["running"] == 1
    rid = mgr.start(ZIP, "p.zip", CFG)
    assert _wait(lambda: len(cluster.created) == 1)
    assert _env(cluster.created[0]).get(contract.SLOT_ENV) == "2"     # slot 1 is b's pod's: never handed out
    assert mgr.runs[rid].slot == 2


def test_parallel_running_counts_both_holders_of_one_number():
    turns = _FifoTurns(2)
    assert turns.occupy("x", prefer=1) == 1 and turns.occupy("y", prefer=1) == 1
    assert turns.counts() == (2, 0)
    turns.leave("x")
    assert turns.occupy("z") == 2                                  # 1 is still y's
    turns.leave("y")
    assert turns.occupy("w") == 1


# ---- (3) a non-K8sError from the client is "API unavailable"
class _Flaky(Cluster):
    bad: str | None = None
    error: type[Exception] = ValueError

    def job_state(self, name: str) -> JobState:
        if name == self.bad:
            raise self.error("Expecting value: line 1 column 1 (char 0)")
        return super().job_state(name)


@pytest.mark.parametrize("error", [ValueError, AttributeError, KeyError])
def test_a_non_k8s_error_from_job_state_keeps_the_slot_and_the_run(tmp_path: Path, repo: Path, managers, error):
    """Was the reviewer's repro R2: the error escaped the watch, the run failed and the next Job started on the same slot."""
    out = tmp_path / "out"
    cluster = _Flaky(out, phases=["Running"], pod_attempt=False)
    cluster.error = error
    mgr = _mgr(tmp_path, repo, cluster, managers, max_parallel=1)
    a = mgr.start(ZIP, "a.zip", CFG)
    assert _wait(lambda: len(cluster.created) == 1)
    cluster.bad = job_name(a)
    b = mgr.start(ZIP, "b.zip", CFG)
    assert not _wait(lambda: len(cluster.created) == 2, timeout=0.8)
    assert not mgr.runs[a].done.is_set() and cluster.deleted == [] and mgr._turns.slot_of(a) == 1
    assert _state(out, a).state == "running"
    cluster.bad = None                                             # the API answers again
    _finish(cluster, out, a)
    assert mgr.runs[a].done.wait(10) and mgr.runs[a].exit_code == 0 and _state(out, a).state == "finished"
    assert _wait(lambda: len(cluster.created) == 2) and _rid(cluster.created[1]) == b


def test_a_non_k8s_error_from_delete_is_retried(tmp_path: Path, repo: Path, monkeypatch, managers):
    monkeypatch.setenv("SBMIG_JOB_STOP_GRACE_S", "0.05")
    monkeypatch.setattr("sbmig.web.runs.DELETE_RETRY_S", 0.05)
    out = tmp_path / "out"
    cluster = Cluster(out, phases=["Running"], pod_attempt=False)
    fails = {"n": 2}
    real_delete = cluster.delete_job

    def delete(name):
        if fails["n"]:
            fails["n"] -= 1
            raise ValueError("bad answer")
        real_delete(name)
    cluster.delete_job = delete                                    # type: ignore[method-assign]
    mgr = _mgr(tmp_path, repo, cluster, managers)
    a = mgr.start(ZIP, "a.zip", CFG)
    assert _wait(lambda: _state(out, a).state == "running")
    assert mgr.stop(a)
    assert mgr.runs[a].done.wait(10)
    assert fails["n"] == 0 and cluster.deleted == [job_name(a)] and _state(out, a).state == "stopped"


class _Resp:
    def __init__(self, status_code: int, body=None, bad_json: bool = False):
        self.status_code = status_code
        self._body = body
        self._bad = bad_json
        self.text = "<html>proxy</html>" if bad_json else json.dumps(body)

    def json(self):
        if self._bad:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._body


class _Session:
    def __init__(self, routes):
        self.routes = routes

    def request(self, method, url, **kw):
        hit = self.routes.get((method, url.split("?")[0]))
        return hit if hit is not None else _Resp(404, {})


BASE = "https://10.0.0.1:443"
JOBS = f"{BASE}/apis/batch/v1/namespaces/ns/jobs"
PODS = f"{BASE}/api/v1/namespaces/ns/pods"
SSAR = f"{BASE}/apis/authorization.k8s.io/v1/selfsubjectaccessreviews"


@pytest.mark.parametrize("job, pods", [
    (_Resp(200, bad_json=True), _Resp(200, {"items": []})),
    (_Resp(200, ["not", "an", "object"]), _Resp(200, {"items": []})),
    (_Resp(200, {"status": {}}), _Resp(200, bad_json=True)),
    (_Resp(200, {"status": {}}), _Resp(200, {"items": "nope"})),
    (_Resp(200, {"status": "odd"}), _Resp(200, {"items": []})),
    (_Resp(200, {"status": {}, "metadata": {"labels": ["x"]}}), _Resp(200, {"items": []})),
])
def test_the_client_turns_an_unreadable_answer_into_k8s_error(job, pods):
    c = K8sClient(BASE, "tok", "ns", session=_Session({("GET", f"{JOBS}/sbmig-r1"): job, ("GET", PODS): pods}))
    with pytest.raises(K8sError):
        c.job_state("sbmig-r1")


def test_create_with_an_unreadable_answer_still_returns_the_name_and_can_i_raises():
    s = _Session({("POST", JOBS): _Resp(201, bad_json=True), ("POST", SSAR): _Resp(201, bad_json=True)})
    c = K8sClient(BASE, "tok", "ns", session=s)
    assert c.create_job({"metadata": {"name": "sbmig-r1"}}) == "sbmig-r1"
    with pytest.raises(K8sError, match="not JSON"):
        c.can_i("get", "pods")


# ---- (4) a terminal Job with a pod still running / terminating has not ended
def _job_pods(job_status: dict, pods: list[dict]) -> K8sClient:
    return K8sClient(BASE, "tok", "ns", session=_Session({
        ("GET", f"{JOBS}/sbmig-r1"): _Resp(200, {"metadata": {"labels": {RUN_LABEL: "r1"}}, "status": job_status}),
        ("GET", PODS): _Resp(200, {"items": pods})}))


def _pod(phase: str, deleting: bool = False) -> dict:
    meta = {"name": "sbmig-r1-p", "creationTimestamp": "2026-11-10T00:00:00Z"}
    if deleting:
        meta["deletionTimestamp"] = "2026-11-10T01:00:00Z"
    return {"metadata": meta, "status": {"phase": phase, "containerStatuses": []}}


def test_job_state_counts_a_terminating_pod_of_a_failed_job():
    failed = {"failed": 1, "conditions": [{"type": "Failed", "reason": "DeadlineExceeded"}]}
    st = _job_pods(failed, [_pod("Running", deleting=True)]).job_state("sbmig-r1")
    assert st.phase == "Failed" and st.active_pods == 1 and not st.finished
    st = _job_pods(failed, [_pod("Failed")]).job_state("sbmig-r1")
    assert st.phase == "Failed" and st.active_pods == 0 and st.finished
    st = _job_pods({**failed, "terminating": 1}, []).job_state("sbmig-r1")       # the Job's own count (newer clusters)
    assert st.active_pods == 1 and not st.finished
    st = _job_pods({"succeeded": 1, "active": 1}, [_pod("Succeeded")]).job_state("sbmig-r1")
    assert st.phase == "Succeeded" and not st.finished
    assert JobState("Failed").finished and JobState("Missing").finished          # clients without the count: as before


class _Terminating(Cluster):
    """A's Job turns Failed (activeDeadlineSeconds) while its pod is still terminating, until the test lets it go."""
    target: str | None = None

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.gone = threading.Event()
        self.failed_polls = 0

    def job_state(self, name: str) -> JobState:
        if name == self.target and name in self.jobs and self.jobs[name].get("terminating"):
            labels = dict(self.jobs[name]["labels"])
            self.failed_polls += 1
            return JobState("Failed", pod=f"{name}-p", message="DeadlineExceeded", labels=labels,
                            active_pods=0 if self.gone.is_set() else 1)
        return super().job_state(name)


def test_a_failed_job_whose_pod_still_terminates_keeps_its_slot(tmp_path: Path, repo: Path, monkeypatch, managers):
    monkeypatch.setenv("SBMIG_JOB_EXIT_GRACE_S", "60")
    monkeypatch.setenv("SBMIG_JOB_STATUS_GRACE_S", "0.1")
    out = tmp_path / "out"
    cluster = _Terminating(out, phases=["Running"], pod_attempt=False)
    mgr = _mgr(tmp_path, repo, cluster, managers)
    a = mgr.start(ZIP, "a.zip", CFG)
    b = mgr.start(ZIP, "b.zip", CFG)
    assert _wait(lambda: len(cluster.created) == 1)
    cluster.target = job_name(a)
    cluster.jobs[job_name(a)]["terminating"] = True
    assert _wait(lambda: cluster.failed_polls >= 3)
    assert not _wait(lambda: len(cluster.created) == 2, timeout=0.5)
    assert not mgr.runs[a].done.is_set() and mgr._turns.slot_of(a) == 1 and cluster.deleted == []
    cluster.gone.set()                                             # the pod's termination grace is over
    assert mgr.runs[a].done.wait(10)
    assert _state(out, a).state == "lost" and mgr.runs[a].exit_code == 3
    assert _wait(lambda: len(cluster.created) == 2) and _rid(cluster.created[1]) == b


def test_a_terminating_pod_past_the_exit_grace_gets_its_job_deleted(tmp_path: Path, repo: Path, monkeypatch, managers):
    """Bounded like the exit grace: the Job is deleted (foreground — gone only when its pods are) and waited for."""
    monkeypatch.setenv("SBMIG_JOB_EXIT_GRACE_S", "0.2")
    monkeypatch.setenv("SBMIG_JOB_STATUS_GRACE_S", "0.1")
    out = tmp_path / "out"
    cluster = _Terminating(out, phases=["Running"], pod_attempt=False)
    mgr = _mgr(tmp_path, repo, cluster, managers)
    a = mgr.start(ZIP, "a.zip", CFG)
    assert _wait(lambda: len(cluster.created) == 1)
    cluster.target = job_name(a)
    cluster.jobs[job_name(a)]["terminating"] = True
    real_delete = cluster.delete_job

    def delete(name):                                              # foreground delete: the Job goes once its pod is gone
        cluster.jobs[name]["terminating"] = False
        real_delete(name)
    cluster.delete_job = delete                                    # type: ignore[method-assign]
    assert mgr.runs[a].done.wait(10)
    assert cluster.deleted == [job_name(a)] and mgr._turns.slot_of(a) is None


# ---- (5) a stop on a waiting run leaves the line at once
def test_a_stopped_waiting_run_leaves_the_line_at_once(tmp_path: Path, repo: Path, managers):
    out = tmp_path / "out"
    cluster = Cluster(out, phases=["Running"], pod_attempt=False)
    mgr = _mgr(tmp_path, repo, cluster, managers)
    a = mgr.start(ZIP, "a.zip", CFG)
    b = mgr.start(ZIP, "b.zip", CFG)
    c = mgr.start(ZIP, "c.zip", CFG)
    assert _wait(lambda: len(cluster.created) == 1)
    assert mgr.status(c)["queue"] == {"position": 2, "ahead": 1} and mgr.parallel()["waiting"] == 2
    stream = mgr.events(c)
    kind, first = next(stream)
    assert kind == "status" and first["queue"]["position"] == 2
    assert mgr.stop(b) is True
    assert mgr.runs[b].done.wait(2)                                # at once, not when a ends
    rb = mgr.runs[b]
    assert rb.exit_code == 3 and rb.error == "stopped before the job was created" and rb.slot is None
    assert _state(out, b).state == "stopped" and _state(out, b).exit_code == 3
    assert json.loads((out / b / "web-status.json").read_text())["stop_requested"] is True
    assert ("finished", {"exit_code": 3, "success": None}) in rb.history
    assert mgr.status(c)["queue"] == {"position": 1, "ahead": 0} and mgr.parallel()["waiting"] == 1
    # the stream of the run behind it sees its new place without a reload
    seen = None
    deadline = time.time() + 5
    while time.time() < deadline:
        kind, data = next(stream)
        if kind == "status":
            seen = data
            break
    assert seen is not None and seen["state"] == "queued" and seen["queue"]["position"] == 1
    assert mgr.stop(b) is False and mgr.stop("web-nope") is False
    assert [_rid(m) for m in cluster.created] == [a]
    _finish(cluster, out, a)
    assert _wait(lambda: len(cluster.created) == 2) and _rid(cluster.created[1]) == c


def test_a_stop_racing_the_turn_ends_one_way_and_leaks_nothing(tmp_path: Path, repo: Path, managers):
    out = tmp_path / "out"
    cluster = Cluster(out, phases=["Running"], pod_attempt=False)
    mgr = _mgr(tmp_path, repo, cluster, managers)
    for _ in range(15):
        a = mgr.start(ZIP, "a.zip", CFG)
        b = mgr.start(ZIP, "b.zip", CFG)
        assert _wait(lambda a=a: any(_rid(m) == a for m in cluster.created))
        go = threading.Event()
        t = threading.Thread(target=lambda go=go, b=b: (go.wait(5), mgr.stop(b)), daemon=True)
        t.start()
        pod_writes(out / a, a, attempt=1)
        go.set()
        cluster.jobs[job_name(a)]["phases"] = ["Succeeded"]
        t.join(5)
        assert mgr.runs[a].done.wait(10) and mgr.runs[b].done.wait(10)
        assert not any(_rid(m) == b for m in cluster.created)       # neither outcome creates b's Job
        assert mgr.runs[b].error == "stopped before the job was created" and _state(out, b).state == "stopped"
        assert [e for e in mgr.runs[b].history if e[0] == "finished"] == [("finished", {"exit_code": 3, "success": None})]
    assert mgr._turns.counts() == (0, 0)


def test_withdraw_wakes_the_waiter_and_keeps_fifo_for_the_others():
    turns = _FifoTurns(1)
    for k in "abcd":
        turns.join(k)
    got: list[str] = []
    left: list[str] = []
    release = threading.Event()

    def body(k: str) -> None:
        try:
            with turns.turn(k):
                got.append(k)
                if k == "a":
                    release.wait(10)
        except _LeftLine:
            left.append(k)
    threads = [threading.Thread(target=body, args=(k,), daemon=True) for k in "dcba"]
    for t in threads:
        t.start()
    assert _wait(lambda: got == ["a"])
    assert turns.withdraw("c") is True and _wait(lambda: left == ["c"])
    assert turns.position("b") == 1 and turns.position("d") == 2 and turns.counts() == (1, 2)
    assert turns.withdraw("a") is False and turns.withdraw("zz") is False   # holding a slot / unknown: nothing changes
    turns.leave("zz")                                                     # harmless
    release.set()
    for t in threads:
        t.join(5)
    assert got == ["a", "b", "d"] and turns.counts() == (0, 0)


def test_withdraw_before_the_thread_reaches_the_line():
    turns = _FifoTurns(1)
    turns.join("x")
    assert turns.withdraw("x") is True
    with pytest.raises(_LeftLine), turns.turn("x"):
        pass
    assert turns.counts() == (0, 0)
    with turns.turn("x") as slot:                                         # the withdrawal is spent: a new turn works
        assert slot == 1


# ---- (6) shutdown while waiting for the result file
def test_shutdown_does_not_wait_out_the_status_grace(tmp_path: Path, repo: Path, monkeypatch):
    monkeypatch.setenv("SBMIG_JOB_STATUS_GRACE_S", "60")
    out = tmp_path / "out"
    cluster = Cluster(out, phases=["Running", "Succeeded"], pod_attempt=False)   # ends without a job-status.json
    mgr = RunManager(out, repo, mode="k8s-job", job_client=cluster, job_template=TEMPLATE)
    a = mgr.start(ZIP, "a.zip", CFG)
    assert _wait(lambda: mgr.runs[a].job_phase == "Succeeded")
    time.sleep(0.2)
    t0 = time.monotonic()
    mgr.shutdown("t")
    assert mgr.runs[a].done.wait(3) and time.monotonic() - t0 < 3
    run = mgr.runs[a]
    assert run.keep_state and "waiting for the job's result" in (run.error or "")
    assert _state(out, a).state == "running"                      # the next Main adopts it and reads the result
