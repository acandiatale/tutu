"""v2.1z-aq (company cluster, two uploads at once): one build pod stayed in `ContainerCreating` for 28 minutes (a Kubernetes-side
cause: volume attach / mount, image pull, sandbox). Two gaps on our side are closed here:

A. a stop while the Job is `Creating` / `Pending` (no container started, so nobody reads `stop.request`) deletes our Job at once,
   waits until it is gone, ends the run STOPPED and frees the slot (journal `job_deleted_before_start`); an unowned Job is
   never deleted; a stop while `Running` keeps the grace (pinned in test_jobmode_hardening / test_job_mode).
B. past `SBMIG_JOB_PENDING_NOTICE_S` Main finds why the pod does not start (container waiting reason, `PodScheduled=False`,
   the pod's events) and tells the page (`job` node update `pod-pending`, `status().job.pending`); it never fails, frees or
   deletes anything by itself.

Fake Kubernetes clients only; tiny poll intervals; the time-dependent notice rules run on a fake clock."""
from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

import sbmig.web
import sbmig.web.runs as runs_mod
from sbmig.core.config import PACKAGE_ROOT
from sbmig.core.workspace import STOP_REQUEST_FILE
from sbmig.web import contract
from sbmig.web.contract import OWNER_LABEL, RUN_LABEL
from sbmig.web.k8s import POD_WAIT_KO, JobState, K8sClient, job_name, pod_wait_hint_ko, pod_wait_reason
from sbmig.web.runs import STOP_BEFORE_START_ERROR, Run, RunManager, resume_job_name

from .test_jobmode_hardening import TEMPLATE, ZIP, Cluster, _journal, _seed, _state, _wait
from .test_remediation_resume import finished_run, resume_state

CFG = ["artifacts.local-e2e.yml"]
PAGE = (Path(sbmig.web.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")


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
    monkeypatch.delenv("SBMIG_JOB_PENDING_NOTICE_S", raising=False)
    monkeypatch.delenv("SBMIG_JOB_STOP_GRACE_S", raising=False)        # the default 2700 s: a deletion here is never the grace's


@pytest.fixture
def managers():
    made: list[RunManager] = []
    yield made
    for mgr in made:
        mgr.shutdown("test teardown")
        for run in list(mgr.runs.values()) + list(mgr.resumes.values()):
            run.done.wait(10)


def _mgr(tmp_path: Path, repo: Path, cluster, managers, **kw) -> RunManager:
    m = RunManager(tmp_path / "out", repo, mode="k8s-job", job_client=cluster, job_template=TEMPLATE, **kw)
    managers.append(m)
    return m


class PodCluster(Cluster):
    """The Job-mode fake cluster whose live Jobs report `self.state` (a pod that has not started) until the test sets it to
    None; then the scripted phases answer. A deleted Job reports Pending once more, then Missing."""

    def __init__(self, out_dir: Path, state: JobState | None, phases: list[str] | None = None):
        super().__init__(out_dir, phases=phases or ["Running"], pod_attempt=False)
        self.state = state
        self.linger = ["Pending", "Missing"]
        self.events: list[dict[str, str]] = []
        self.event_calls: list[str] = []
        self.events_readable = True

    def job_state(self, name: str) -> JobState:
        j = self.jobs.get(name)
        if self.state is not None and j is not None and not j.get("deleting"):
            return replace(self.state, labels=dict(j["labels"]))
        return super().job_state(name)

    def pod_events(self, pod: str) -> list[dict[str, str]]:
        self.event_calls.append(pod)
        return list(self.events)


def _creating() -> JobState:
    return JobState("Creating", None, message="no pod yet")


def _pending(**kw) -> JobState:
    return JobState("Pending", "sbmig-x-p1", message="ContainerCreating",
                    **({"waiting_reason": "ContainerCreating"} | kw))


# ---- A. stop before the pod started
@pytest.mark.parametrize("state", [_creating(), _pending()], ids=["Creating", "Pending"])
def test_a_stop_before_the_pod_started_deletes_the_job_at_once_and_frees_the_slot(tmp_path, repo, managers, state):
    out = tmp_path / "out"
    cluster = PodCluster(out, state)
    mgr = _mgr(tmp_path, repo, cluster, managers)
    first = mgr.start(ZIP, "a.zip", CFG)
    assert _wait(lambda: mgr.runs[first].job_phase == state.phase)
    second = mgr.start(ZIP, "b.zip", CFG)
    assert mgr.status(second)["state"] == "queued"
    assert mgr.stop(first) and mgr.runs[first].done.wait(5)
    run = mgr.runs[first]
    assert cluster.deleted == [job_name(first)] and job_name(first) not in cluster.jobs    # deleted and gone, no 2700 s grace
    assert run.exit_code == 3 and run.error == STOP_BEFORE_START_ERROR
    assert (out / first / STOP_REQUEST_FILE).is_file()                                     # still written first
    cs = _state(out, first)
    assert cs.state == "stopped" and cs.error == STOP_BEFORE_START_ERROR
    ev = [e for e in _journal(out, first) if e["event"] == "job_deleted_before_start"]
    assert len(ev) == 1 and ev[0]["job"] == job_name(first) and ev[0]["phase"] == state.phase and ev[0]["reason"]
    assert ev[0]["run_id"] == first
    st = mgr.status(first)
    assert st["state"] == "finished" and st["stop_requested"] is True and st["exit_code"] == 3
    assert st["stop_reason_ko"] == "빌드 파드가 시작되기 전이라 작업을 바로 정리했습니다." and "pending" not in st["job"]
    assert ("node", {"node": "job", "update": {"state": "stopped-before-start", "job": job_name(first),
                                               "phase": state.phase,
                                               "message_ko": "빌드 파드가 시작되기 전이라 작업을 바로 정리했습니다."}}) in run.history
    # the slot is free: the second upload gets its Job
    assert _wait(lambda: len(cluster.created) == 2)
    assert cluster.created[1]["metadata"]["labels"][RUN_LABEL] == second


def test_a_stop_before_start_refuses_an_unowned_job(tmp_path, repo, managers):
    out = tmp_path / "out"
    cluster = PodCluster(out, _pending())
    mgr = _mgr(tmp_path, repo, cluster, managers)
    rid = mgr.start(ZIP, "a.zip", CFG)
    assert _wait(lambda: mgr.runs[rid].job_phase == "Pending")
    cluster.jobs[job_name(rid)]["labels"][OWNER_LABEL] = "someone-else"
    assert mgr.stop(rid) and mgr.runs[rid].done.wait(5)
    assert cluster.deleted == [] and "is not owned by this request — not deleted" in (mgr.runs[rid].error or "")
    events = [e["event"] for e in _journal(out, rid)]
    assert "job_delete_refused" in events and "job_deleted_before_start" not in events
    assert _state(out, rid).state == "stopped"


def test_a_stop_while_running_keeps_the_grace(tmp_path, repo, managers, monkeypatch):
    monkeypatch.setenv("SBMIG_JOB_STOP_GRACE_S", "60")
    out = tmp_path / "out"
    cluster = PodCluster(out, None, phases=["Running"])
    mgr = _mgr(tmp_path, repo, cluster, managers)
    rid = mgr.start(ZIP, "a.zip", CFG)
    assert _wait(lambda: _state(out, rid).state == "running")
    assert mgr.stop(rid) and _wait(lambda: (out / rid / STOP_REQUEST_FILE).is_file())
    assert not mgr.runs[rid].done.wait(0.4) and cluster.deleted == []
    assert not any(e["event"] == "job_deleted_before_start" for e in _journal(out, rid))


def test_a_pod_that_starts_after_the_stop_request_keeps_the_grace(tmp_path, repo, managers, monkeypatch):
    """The pod was Pending for a while and then started: a stop now is a stop of a running pod (grace, no early delete)."""
    monkeypatch.setenv("SBMIG_JOB_STOP_GRACE_S", "60")
    out = tmp_path / "out"
    cluster = PodCluster(out, _pending())
    mgr = _mgr(tmp_path, repo, cluster, managers)
    rid = mgr.start(ZIP, "a.zip", CFG)
    assert _wait(lambda: mgr.runs[rid].job_phase == "Pending")
    cluster.state = None                                           # the container starts
    assert _wait(lambda: mgr.runs[rid].job_phase == "Running")
    assert mgr.stop(rid) and _wait(lambda: (out / rid / STOP_REQUEST_FILE).is_file())
    assert not mgr.runs[rid].done.wait(0.3) and cluster.deleted == []


def test_an_adopted_job_that_has_not_started_is_deleted_on_stop(tmp_path, repo, managers):
    out = tmp_path / "out"
    _seed(out, "web-ad", "created")
    cluster = PodCluster(out, _pending(waiting_reason="ContainerCreating"))
    cluster.add(job_name("web-ad"), {RUN_LABEL: "web-ad", OWNER_LABEL: "own3r"})
    mgr = _mgr(tmp_path, repo, cluster, managers)
    assert mgr.recovered == ["web-ad"]
    assert _wait(lambda: mgr.runs["web-ad"].job_phase == "Pending")
    assert mgr.stop("web-ad") and mgr.runs["web-ad"].done.wait(5)
    assert cluster.deleted == [job_name("web-ad")] and _state(out, "web-ad").state == "stopped"
    assert any(e["event"] == "job_deleted_before_start" for e in _journal(out, "web-ad"))


def test_a_resume_job_that_has_not_started_is_deleted_and_a_running_one_never(tmp_path, repo, managers):
    out = tmp_path / "out"
    rid = "web-20261117-010000-aaaaaa"
    finished_run(out, rid)
    cluster = PodCluster(out, _pending())
    mgr = _mgr(tmp_path, repo, cluster, managers)
    mgr.resume_remediation(rid)
    name = resume_job_name(rid, 2)
    assert _wait(lambda: mgr.resumes[rid].job_phase == "Pending")
    assert mgr.stop_remediation(rid) == "requested"
    assert mgr.resumes[rid].done.wait(5)
    assert cluster.deleted == [name] and resume_state(out, rid).state == "stopped"
    ev = [e for e in _journal(out, rid) if e["event"] == "job_deleted_before_start"]
    assert len(ev) == 1 and ev[0]["action"] == contract.ACTION_REMEDIATION_RESUME and ev[0]["job"] == name
    assert sum(e["event"] == "stop_request_sent" for e in _journal(out, rid)) == 1   # written once, by stop_remediation
    assert mgr.status(rid)["state"] == "finished"                                    # the migration itself is untouched
    # a running resume pod: stop.request only, never deleted (the grace does not apply to a resume)
    (out / rid / STOP_REQUEST_FILE).unlink()
    cluster.state = None
    cluster.phases = ["Running"]
    mgr.resume_remediation(rid)
    assert _wait(lambda: mgr.resumes[rid].job_phase == "Running")
    mgr.job_stop_grace_s = 0.0
    assert mgr.stop_remediation(rid) == "requested"
    assert not mgr.resumes[rid].done.wait(0.4) and cluster.deleted == [name]


# ---- B. why the pod does not start
NOTICE_CASES = [
    # (id, state kwargs, events, family, reason)
    ("waiting-image", {"waiting_reason": "ImagePullBackOff", "waiting_message": "Back-off pulling image \"reg/x:1\""}, [],
     "image", "ImagePullBackOff"),
    ("waiting-config", {"waiting_reason": "CreateContainerConfigError", "waiting_message": "secret \"sbmig-llm\" not found"},
     [], "config", "CreateContainerConfigError"),
    ("unschedulable", {"sched_reason": "Unschedulable", "sched_message": "0/3 nodes are available: 3 Insufficient cpu."}, [],
     "unschedulable", "Unschedulable"),
    ("multiattach", {}, [{"type": "Warning", "reason": "FailedAttachVolume", "time": "2026-11-17T01:00:02Z",
                          "message": "Multi-Attach error for volume \"pvc-1\" Volume is already exclusively attached to one node"}],
     "multiattach", "FailedAttachVolume"),
    ("mount-nfs", {}, [{"type": "Warning", "reason": "FailedMount", "time": "2026-11-17T01:00:02Z",
                        "message": "MountVolume.SetUp failed for volume \"pvc-1\" : mount failed: exit status 32 nfs"}],
     "mount", "FailedMount"),
    ("sandbox", {}, [{"type": "Warning", "reason": "FailedCreatePodSandBox", "time": "2026-11-17T01:00:02Z",
                      "message": "Failed to create pod sandbox: rpc error: network plugin not ready"}], "sandbox",
     "FailedCreatePodSandBox"),
    ("event-pull-failed", {}, [{"type": "Warning", "reason": "Failed", "time": "2026-11-17T01:00:02Z",
                                "message": "Failed to pull image \"reg/x:1\": not found"}], "image", "Failed"),
    ("event-scheduling", {}, [{"type": "Warning", "reason": "FailedScheduling", "time": "2026-11-17T01:00:02Z",
                               "message": "0/3 nodes are available"}], "unschedulable", "FailedScheduling"),
    ("pulling", {}, [{"type": "Normal", "reason": "Scheduled", "time": "2026-11-17T01:00:00Z", "message": "assigned"},
                     {"type": "Normal", "reason": "Pulling", "time": "2026-11-17T01:00:01Z", "message": "Pulling image \"reg/x:1\""}],
     "pulling", "Pulling"),
    ("nothing", {}, [], "unknown", ""),
]


@pytest.mark.parametrize("kw,events,family,reason", [c[1:] for c in NOTICE_CASES], ids=[c[0] for c in NOTICE_CASES])
def test_the_notice_names_the_reason_after_the_threshold_and_goes_when_the_pod_starts(tmp_path, repo, managers, monkeypatch,
                                                                                     kw, events, family, reason):
    monkeypatch.setenv("SBMIG_JOB_PENDING_NOTICE_S", "0.1")
    monkeypatch.setattr(runs_mod, "PENDING_EVENTS_EVERY_S", 0.0)
    out = tmp_path / "out"
    cluster = PodCluster(out, _pending(**kw))
    cluster.events = events
    mgr = _mgr(tmp_path, repo, cluster, managers)
    rid = mgr.start(ZIP, "a.zip", CFG)
    assert _wait(lambda: "pending" in (mgr.status(rid).get("job") or {}))
    p = mgr.status(rid)["job"]["pending"]
    assert set(p) >= {"seconds", "reason", "message", "hint_ko", "family", "source", "pod", "phase"}
    assert p["family"] == family and p["reason"] == reason and p["pod"] == "sbmig-x-p1" and p["phase"] == "Pending"
    assert p["hint_ko"].startswith("빌드 파드가 아직 시작되지 못했습니다(1분째). ")
    tail = POD_WAIT_KO[family].replace("{kind} {name}", "pod sbmig-x-p1")
    assert p["hint_ko"].endswith(tail)
    if kw:                                           # (1) / (2) found it: no events call needed
        assert cluster.event_calls == []
    run = mgr.runs[rid]
    assert any(k == "node" and d["node"] == "job" and d["update"]["state"] == "pod-pending" for k, d in run.history)
    journal = [e for e in _journal(out, rid) if e["event"] == "job_pod_pending"]
    assert len(journal) == 1 and journal[0]["family"] == family and journal[0]["reason"] == reason
    assert mgr.runs[rid].done.is_set() is False and cluster.deleted == []      # never fails, frees or deletes by itself
    # the container starts: the notice goes, the run goes on
    cluster.state = None
    assert _wait(lambda: "pending" not in (mgr.status(rid).get("job") or {}))
    assert _wait(lambda: any(d["update"].get("state") == "pod-started" for k, d in list(run.history) if k == "node"
                             and d["node"] == "job"))
    assert mgr.status(rid)["state"] == "running"


def _manager_for_unit(tmp_path: Path, repo: Path, managers) -> RunManager:
    return _mgr(tmp_path, repo, PodCluster(tmp_path / "out", None), managers)


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def monotonic(self) -> float:
        return self.t


def _unit_run(tmp_path: Path) -> Run:
    rd = tmp_path / "out" / "web-unit"
    rd.mkdir(parents=True, exist_ok=True)
    return Run("web-unit", rd, rd / "source.zip", job=True)


def test_the_notice_is_resent_only_on_a_new_reason_or_every_five_minutes(tmp_path, repo, managers, monkeypatch, caplog):
    monkeypatch.setenv("SBMIG_JOB_PENDING_NOTICE_S", "120")
    mgr = _manager_for_unit(tmp_path, repo, managers)
    clock = _Clock()
    monkeypatch.setattr(runs_mod, "time", clock)
    client = PodCluster(tmp_path / "out", None)
    client.events = [{"type": "Warning", "reason": "FailedMount", "message": "nfs timed out", "time": "t1"}]
    run = _unit_run(tmp_path)
    caplog.set_level(logging.WARNING, logger="sbmig.web")

    def pushes() -> list[dict]:
        return [d["update"] for k, d in run.history if k == "node" and d["update"]["state"] == "pod-pending"]

    def poll(st: JobState, at: float) -> None:
        clock.t = 1000.0 + at
        mgr._note_pod_waiting(run, client, "sbmig-unit", st)

    st = _pending()
    poll(st, 0)
    poll(st, 119)
    assert pushes() == [] and run.job_pending is None                 # below the threshold: nothing
    poll(st, 125)
    assert len(pushes()) == 1 and pushes()[0]["pending"]["family"] == "mount" and pushes()[0]["pending"]["seconds"] == 125
    poll(st, 200)
    assert len(pushes()) == 1 and run.job_pending["seconds"] == 200   # status() follows, the page is not re-sent
    assert run.job_pending["hint_ko"].startswith("빌드 파드가 아직 시작되지 못했습니다(3분째).")
    poll(st, 125 + 299)
    assert len(pushes()) == 1
    poll(st, 125 + 300)                                                # five minutes: the new waiting time goes out
    assert len(pushes()) == 2 and pushes()[1]["pending"]["seconds"] == 425
    img = _pending(waiting_reason="ErrImagePull", waiting_message="pull access denied")
    poll(img, 430)                                                     # a new reason: at once
    assert len(pushes()) == 3 and pushes()[2]["pending"]["family"] == "image"
    poll(st, 440)                                                      # back to the first reason: sent, not journalled again
    assert len(pushes()) == 4
    journal = [e for e in _journal(tmp_path / "out", "web-unit") if e["event"] == "job_pod_pending"]
    assert [e["reason"] for e in journal] == ["FailedMount", "ErrImagePull"]
    warnings = [r.getMessage() for r in caplog.records if "has not started for" in r.getMessage()]
    assert len(warnings) == 2
    assert warnings[0] == "run web-unit: job sbmig-unit pod sbmig-x-p1 has not started for 125s: FailedMount: nfs timed out"
    # a started pod clears it
    mgr._pod_started(run, "sbmig-unit", replace(st, phase="Running"))
    assert run.job_pending is None and run.history[-1][1]["update"]["state"] == "pod-started"


def test_events_are_asked_at_most_every_interval_and_the_message_is_capped(tmp_path, repo, managers, monkeypatch):
    monkeypatch.setenv("SBMIG_JOB_PENDING_NOTICE_S", "1")
    mgr = _manager_for_unit(tmp_path, repo, managers)
    clock = _Clock()
    monkeypatch.setattr(runs_mod, "time", clock)
    client = PodCluster(tmp_path / "out", None)
    client.events = [{"type": "Warning", "reason": "FailedMount", "message": "x" * 1000, "time": "t1"}]
    run = _unit_run(tmp_path)
    for at in (0, 2, 3, 4, 20):
        clock.t = 1000.0 + at
        mgr._note_pod_waiting(run, client, "sbmig-unit", _pending())
    assert len(client.event_calls) == 2                                # at 2 s and again after PENDING_EVENTS_EVERY_S (15 s)
    assert len(run.job_pending["message"]) == 300


def test_notice_off_with_zero(tmp_path, repo, managers, monkeypatch):
    monkeypatch.setenv("SBMIG_JOB_PENDING_NOTICE_S", "0")
    mgr = _manager_for_unit(tmp_path, repo, managers)
    assert mgr.job_pending_notice_s == 0
    clock = _Clock()
    monkeypatch.setattr(runs_mod, "time", clock)
    client = PodCluster(tmp_path / "out", None)
    run = _unit_run(tmp_path)
    for at in (0, 500, 5000):
        clock.t = 1000.0 + at
        mgr._note_pod_waiting(run, client, "sbmig-unit", _pending(waiting_reason="ErrImagePull"))
    assert run.job_pending is None and run.history == [] and client.event_calls == []


def test_the_default_threshold_is_two_minutes(tmp_path, repo, managers):
    assert _manager_for_unit(tmp_path, repo, managers).job_pending_notice_s == 120


def test_events_not_readable_gives_the_generic_hint(tmp_path, repo, managers, monkeypatch):
    monkeypatch.setenv("SBMIG_JOB_PENDING_NOTICE_S", "1")
    mgr = _manager_for_unit(tmp_path, repo, managers)
    clock = _Clock()
    monkeypatch.setattr(runs_mod, "time", clock)
    client = PodCluster(tmp_path / "out", None)
    client.events_readable = False                                     # the client remembered a 403
    run = _unit_run(tmp_path)
    for at in (0, 5):
        clock.t = 1000.0 + at
        mgr._note_pod_waiting(run, client, "sbmig-unit", _pending())
    assert client.event_calls == [] and run.job_pending["family"] == "unknown"
    assert run.job_pending["hint_ko"].endswith("이유는 kubectl describe pod sbmig-x-p1의 Events에서 볼 수 있습니다.")
    # no pod yet (Creating): the Job's events are where to look
    assert pod_wait_hint_ko("unknown", 61, None, "sbmig-j").endswith("kubectl describe job sbmig-j의 Events에서 볼 수 있습니다.")


def test_startup_checks_the_events_permission_once_and_never_fails(tmp_path, repo, monkeypatch, caplog):
    from .test_jobmode_hardening import _production, _Rbac, _template
    rbac = _Rbac(denied={("list", "events")})
    _production(monkeypatch, rbac)
    caplog.set_level(logging.INFO, logger="sbmig.web")
    mgr = RunManager(tmp_path / "out", repo, mode="k8s-job", job_template=_template(tmp_path))
    try:
        assert rbac.asked.count(("list", "events", "")) == 1 and mgr._events_readable is False
        lines = [r.getMessage() for r in caplog.records if "pod events are not readable" in r.getMessage()]
        assert lines == ["pod events are not readable with this account — the page will not show why a pod does not start"]
    finally:
        mgr.shutdown("test teardown")


# ---- the client
class _Resp:
    def __init__(self, status_code: int, body=None, bad_json: bool = False):
        self.status_code, self._body, self._bad = status_code, body, bad_json
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        if self._bad:
            raise ValueError("not json")
        return self._body


class _Session:
    def __init__(self, answer):
        self.answer = answer
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw.get("params") or {}))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _client(answer) -> tuple[K8sClient, _Session]:
    s = _Session(answer)
    return K8sClient("https://k8s.example:443", "t", "agent", session=s), s


def test_pod_events_reads_newest_first_with_the_field_selector():
    body = {"items": [{"type": "Normal", "reason": "Pulling", "message": "pulling", "lastTimestamp": "2026-11-17T01:00:01Z"},
                      {"type": "Warning", "reason": "FailedMount", "message": "nfs", "lastTimestamp": "2026-11-17T01:00:05Z"},
                      {"type": "Normal", "reason": "Scheduled", "message": "ok", "eventTime": "2026-11-17T01:00:00Z"}]}
    c, s = _client(_Resp(200, body))
    evs = c.pod_events("sbmig-x-p1")
    assert [e["reason"] for e in evs] == ["FailedMount", "Pulling", "Scheduled"]
    method, url, params = s.calls[0]
    assert method == "GET" and url.endswith("/api/v1/namespaces/agent/events")
    assert params == {"fieldSelector": "involvedObject.kind=Pod,involvedObject.name=sbmig-x-p1"}
    assert c.events_readable is True


@pytest.mark.parametrize("answer", [_Resp(403, {"kind": "Status"}), _Resp(200, bad_json=True), _Resp(200, {"items": "x"})],
                         ids=["403", "not-json", "odd-items"])
def test_pod_events_403_or_odd_body_is_remembered(answer):
    c, s = _client(answer)
    assert c.pod_events("p") == [] and c.events_readable is False
    assert c.pod_events("p") == [] and len(s.calls) == 1               # not asked again in this process


def test_pod_events_transport_error_is_no_events_this_time():
    import requests
    c, s = _client(requests.ConnectionError("reset"))
    assert c.pod_events("p") == [] and c.events_readable is True
    c.pod_events("p")
    assert len(s.calls) == 2


def test_job_state_carries_the_waiting_reason_and_the_unschedulable_condition():
    job = {"metadata": {"labels": {RUN_LABEL: "r"}}, "status": {"active": 1}}
    pod = {"metadata": {"name": "p1", "creationTimestamp": "2026-11-17T01:00:00Z"},
           "status": {"phase": "Pending",
                      "conditions": [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable",
                                      "message": "0/3 nodes are available"}],
                      "containerStatuses": [{"state": {"waiting": {"reason": "ErrImagePull", "message": "denied"}}}]}}
    st = K8sClient._job_state_of(job, {"items": [pod]})
    assert st.phase == "Pending" and not st.started and st.message == "ErrImagePull denied"
    assert (st.waiting_reason, st.waiting_message) == ("ErrImagePull", "denied")
    assert (st.sched_reason, st.sched_message) == ("Unschedulable", "0/3 nodes are available")
    assert K8sClient._job_state_of(job, {"items": []}).phase == "Creating"
    running = {"metadata": {"name": "p1"}, "status": {"phase": "Running", "containerStatuses": [{"state": {"running": {}}}]}}
    assert K8sClient._job_state_of(job, {"items": [running]}).started


def test_reason_order_waiting_then_condition_then_events():
    ev = [{"type": "Warning", "reason": "FailedMount", "message": "m", "time": "1"}]
    both = _pending(waiting_reason="ErrImagePull", sched_reason="Unschedulable")
    assert pod_wait_reason(both, ev)[0] == "image"
    assert pod_wait_reason(_pending(sched_reason="Unschedulable", sched_message="x"), ev)[:2] == ("unschedulable", "Unschedulable")
    assert pod_wait_reason(_pending(), ev)[:2] == ("mount", "FailedMount")
    assert pod_wait_reason(_pending(waiting_reason="PodInitializing"), [])[0] == "unknown"


# ---- the page and the texts
def test_the_korean_sentences():
    assert POD_WAIT_KO == {
        "mount": "볼륨을 붙이지 못하고 있습니다. 인프라 담당자에게 문의해 주세요.",
        "multiattach": "볼륨이 다른 노드에 붙어 있어 이 노드에서 쓸 수 없습니다. 인프라 담당자에게 문의해 주세요.",
        "image": "이미지를 받지 못하고 있습니다.",
        "pulling": "이미지를 받는 중입니다. 이미지가 크면 몇 분 걸립니다.",
        "unschedulable": "파드를 띄울 노드가 없습니다. 자원이 부족할 수 있습니다.",
        "sandbox": "노드에서 파드 네트워크를 준비하지 못하고 있습니다. 인프라 담당자에게 문의해 주세요.",
        "config": "파드 설정을 읽지 못하고 있습니다. ConfigMap과 Secret을 확인해 주세요.",
        "unknown": "이유는 kubectl describe {kind} {name}의 Events에서 볼 수 있습니다.",
    }
    assert pod_wait_hint_ko("mount", 28 * 60 + 5, "p", "j") == (
        "빌드 파드가 아직 시작되지 못했습니다(28분째). 볼륨을 붙이지 못하고 있습니다. 인프라 담당자에게 문의해 주세요.")
    for text in POD_WAIT_KO.values():
        assert "—" not in text and "→" not in text


def test_the_page_shows_the_notice_under_the_pod_step_and_the_stop_line():
    assert re.search(r'<div class="rail" id="rail"[^>]*></div>\s*<!--[^>]*-->\s*<div class="podwait" id="podwait" role="status" hidden>',
                     PAGE)
    assert "중단을 누르면 이 실행을 바로 정리합니다." in PAGE
    # the Korean sentences that say '파드' come from the server (hint_ko, message_ko, stop_reason_ko), never page literals
    assert not re.search(r"'[^'\n]*빌드 파드[^'\n]*'", PAGE)
    for token in ("'pod-pending'", "'pod-started'", "'stopped-before-start'", "setPodWait(u.pending)", "u.message_ko",
                  "st.job.pending", "p.hint_ko", "st.stop_reason_ko"):
        assert token in PAGE, token
    # a stopped run still reads "중단되었습니다"
    assert "text: '중단되었습니다'" in PAGE
