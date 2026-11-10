"""2026-11-10 (company k8s run): an Oracle ADF project rejected at analyze (sbmig exit 3) made the Worker exit 3, the Job ended
`Failed`, and the page marked the '파드' (pod) step as failed although the pod had done its work. The pod step's meaning is
"did the pod do its job": `completed` when this run / attempt's job-status.json is accepted without `error` (whatever the exit
code), `failed` only when the Job ended Failed with no verified status within the grace (the pod died) or when the verified
status carries the Worker's own `error` (refusal, sbmig not started). The raw Kubernetes phase stays in `status()["job"]`."""
from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path

import pytest

from sbmig.core.config import PACKAGE_ROOT
from sbmig.web.contract import OWNER_LABEL, RUN_LABEL, JobStatus
from sbmig.web.k8s import JobState, job_name

from .test_jobmode_hardening import ZIP, Cluster, _job_events, _mgr, _seed, _state, _wait

EXPLAINED = "Failed is the Worker's exit code"
REFUSAL = "source zip changed since intake (sha256 0123456789ab… ≠ pinned 0123456789cd…)"


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


def _rejected(run_dir: Path, run_id: str, *, error: str | None = None) -> None:
    """What the Worker leaves when sbmig stopped the run at analyze (ADF project: exit 3) — or, with `error`, when the Worker
    refused the request before sbmig ran."""
    if error is None:
        (run_dir / "report.md").write_text("# report\n\nADF 프로젝트라 변환이 불가능합니다\n", encoding="utf-8")
        (run_dir / "report.json").write_text(json.dumps({"success": False, "blocked": [], "unresolved": []}), encoding="utf-8")
    JobStatus(exit_code=3, run_id=run_id, attempt=1, error=error).write(run_dir)


def _closing(run) -> list[dict]:
    return [d["update"] for k, d in run.history if k == "node" and d["node"] == "job" and d["update"]["state"] in ("completed", "failed")]


def _explained(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if EXPLAINED in r.getMessage()]


def test_failed_phase_seen_before_the_status_file_closes_the_pod_step_as_completed(tmp_path: Path, repo: Path, monkeypatch, caplog):
    monkeypatch.setenv("SBMIG_JOB_STATUS_GRACE_S", "5")
    out = tmp_path / "out"
    cluster = Cluster(out, phases=["Running", "Failed"], pod_attempt=False)    # the container exits 3, the Job turns Failed
    mgr = _mgr(tmp_path, repo, cluster)
    with caplog.at_level(logging.INFO, logger="sbmig.web"):
        rid = mgr.start(ZIP, "adf.zip", ["artifacts.local-e2e.yml"])
        run = mgr.runs[rid]
        assert _wait(lambda: mgr.status(rid).get("job", {}).get("phase") == "Failed")
        time.sleep(0.2)
        assert not run.done.is_set() and "failed" not in _job_events(run)        # the raw phase is not the pod step's verdict
        _rejected(out / rid, rid)                                               # NFS shows the Worker's file late
        assert run.done.wait(10)
    events = _job_events(run)
    assert "failed" not in events and events.count("completed") == 1 and events[-1] == "completed"
    (done,) = _closing(run)
    assert done == {"state": "completed", "job": job_name(rid), "pod": f"{job_name(rid)}-p", "exit_code": 3, "phase": "Failed"}
    st = mgr.status(rid)
    assert run.exit_code == 3 and st["verified"] is True and st["job"]["phase"] == "Failed" and st["success"] is False
    assert _state(out, rid).state == "finished"
    (line,) = _explained(caplog)
    assert f"run {rid}: job {job_name(rid)}: {EXPLAINED} 3 (job-status.json verified)" in line


def test_status_file_first_then_a_failed_end_closes_the_pod_step_once(tmp_path: Path, repo: Path, monkeypatch, caplog):
    monkeypatch.setenv("SBMIG_JOB_EXIT_GRACE_S", "60")
    out = tmp_path / "out"
    cluster = Cluster(out, phases=["Running"], pod_attempt=False)
    mgr = _mgr(tmp_path, repo, cluster)
    with caplog.at_level(logging.INFO, logger="sbmig.web"):
        rid = mgr.start(ZIP, "adf.zip", ["artifacts.local-e2e.yml"])
        run = mgr.runs[rid]
        assert _wait(lambda: _state(out, rid).state == "running")
        _rejected(out / rid, rid)                                               # the result is on the volume, the pod still runs
        assert _wait(lambda: "ending" in _job_events(run))
        assert not run.done.is_set() and _job_events(run).count("completed") == 1
        cluster.jobs[job_name(rid)]["phases"] = ["Failed"]                      # then the container exits 3
        assert run.done.wait(10)
    events = _job_events(run)
    assert "failed" not in events and events.count("completed") == 1
    assert events.index("completed") < events.index("ending")
    (done,) = _closing(run)
    assert done["exit_code"] == 3 and done["phase"] == "Running" and "message" not in done   # the phase seen when the file came
    st = mgr.status(rid)
    assert run.exit_code == 3 and st["verified"] is True and st["job"]["phase"] == "Failed" and cluster.deleted == []
    assert len(_explained(caplog)) == 1


@pytest.mark.parametrize("container_exit", [137, None])
def test_a_failed_job_without_a_status_file_is_a_pod_failure(tmp_path: Path, repo: Path, caplog, container_exit):
    """conftest: SBMIG_JOB_STATUS_GRACE_S=0.2 — the pod died (OOMKilled / deadline), no job-status.json ever shows."""
    out = tmp_path / "out"
    cluster = Cluster(out, phases=["Running", "Failed"], pod_attempt=False)
    real = cluster.job_state

    def job_state(name):
        st = real(name)
        if st.phase == "Failed":
            st.exit_code, st.message = container_exit, "BackoffLimitExceeded: OOMKilled"
        return st
    cluster.job_state = job_state
    mgr = _mgr(tmp_path, repo, cluster)
    with caplog.at_level(logging.INFO, logger="sbmig.web"):
        rid = mgr.start(ZIP, "p.zip", ["artifacts.local-e2e.yml"])
        run = mgr.runs[rid]
        assert run.done.wait(10)
    events = _job_events(run)
    assert events.count("failed") == 1 and "completed" not in events and events[-1] == "failed"
    (fail,) = _closing(run)
    assert fail == {"state": "failed", "job": job_name(rid), "pod": f"{job_name(rid)}-p", "message": "BackoffLimitExceeded: OOMKilled"}
    st = mgr.status(rid)
    assert st["verified"] is False and st["job"]["phase"] == "Failed" and _explained(caplog) == []
    assert run.exit_code == (container_exit if container_exit is not None else 3)
    assert _state(out, rid).state == "lost"


@pytest.mark.parametrize("order", ["status-first", "phase-first"])
def test_a_verified_status_with_the_workers_error_is_a_pod_failure(tmp_path: Path, repo: Path, monkeypatch, caplog, order):
    """The Worker refused the request (or could not start sbmig): job-status.json carries `error` — `failed` with that error."""
    monkeypatch.setenv("SBMIG_JOB_STATUS_GRACE_S", "5")
    out = tmp_path / "out"
    cluster = Cluster(out, phases=["Running", "Failed"], pod_attempt=False)
    real = cluster.job_state

    def job_state(name):
        st = real(name)
        if order == "status-first" and st.phase == "Running":
            rid = cluster.jobs[name]["labels"][RUN_LABEL]
            _rejected(out / rid, rid, error=REFUSAL)
        return st
    cluster.job_state = job_state
    mgr = _mgr(tmp_path, repo, cluster)
    with caplog.at_level(logging.INFO, logger="sbmig.web"):
        rid = mgr.start(ZIP, "p.zip", ["artifacts.local-e2e.yml"])
        run = mgr.runs[rid]
        if order == "phase-first":
            assert _wait(lambda: mgr.status(rid).get("job", {}).get("phase") == "Failed")
            time.sleep(0.1)
            assert "failed" not in _job_events(run)
            _rejected(out / rid, rid, error=REFUSAL)
        assert run.done.wait(10)
    events = _job_events(run)
    assert events.count("failed") == 1 and "completed" not in events
    (fail,) = _closing(run)
    assert fail["state"] == "failed" and fail["message"] == REFUSAL and fail["exit_code"] == 3 and fail["job"] == job_name(rid)
    assert run.exit_code == 3 and mgr.status(rid)["verified"] is True and REFUSAL in (run.error or "")
    assert _explained(caplog) == []                                             # a refusal is not "the Worker's exit code"


def test_a_succeeded_run_closes_the_pod_step_as_completed(tmp_path: Path, repo: Path):
    out = tmp_path / "out"
    mgr = _mgr(tmp_path, repo, Cluster(out))                                    # Creating → Pending → Running (pod writes exit 0) → Succeeded
    rid = mgr.start(ZIP, "p.zip", ["artifacts.local-e2e.yml"])
    run = mgr.runs[rid]
    assert run.done.wait(10)
    events = _job_events(run)
    assert events[0] == "created" and "running" in events and events[-1] == "completed" and "failed" not in events
    (done,) = _closing(run)
    assert done["exit_code"] == 0 and run.exit_code == 0 and mgr.status(rid)["verified"] is True


@pytest.mark.parametrize("file_visible_at_adoption", [True, False])
def test_an_adopted_failed_job_closes_the_pod_step_the_same_way(tmp_path: Path, repo: Path, monkeypatch, caplog,
                                                                 file_visible_at_adoption):
    """A restarted Main adopts a Job that already ended Failed (sbmig exit 3): the shared watch loop closes the pod step once
    as `completed` — with no `phase` when the status file was read before any poll."""
    monkeypatch.setenv("SBMIG_JOB_STATUS_GRACE_S", "5")
    out = tmp_path / "out"
    rd = _seed(out, "web-f", "running")
    if file_visible_at_adoption:
        _rejected(rd, "web-f")
    cluster = Cluster(out, phases=["Failed"], pod_attempt=False)
    cluster.add(job_name("web-f"), {RUN_LABEL: "web-f", OWNER_LABEL: "own3r"})
    with caplog.at_level(logging.INFO, logger="sbmig.web"):
        mgr = _mgr(tmp_path, repo, cluster)
        run = mgr.runs["web-f"]
        if not file_visible_at_adoption:
            assert _wait(lambda: mgr.status("web-f").get("job", {}).get("phase") == "Failed")
            _rejected(rd, "web-f")
        assert run.done.wait(10)
    events = _job_events(run)
    assert events[0] == "created" and events.count("completed") == 1 and "failed" not in events
    (done,) = _closing(run)
    assert done["exit_code"] == 3
    assert ("phase" not in done) if file_visible_at_adoption else (done["phase"] == "Failed")
    assert run.exit_code == 3 and mgr.status("web-f")["verified"] is True and _state(out, "web-f").state == "finished"
    assert len(_explained(caplog)) == 1


def test_close_pod_step_pushes_at_most_once_per_watch(tmp_path: Path, repo: Path):
    out = tmp_path / "out"
    mgr = _mgr(tmp_path, repo, Cluster(out, phases=["Running", "Succeeded"]))
    rid = mgr.start(ZIP, "p.zip", ["artifacts.local-e2e.yml"])
    run = mgr.runs[rid]
    assert run.done.wait(10)
    before = len(run.history)
    mgr._close_pod_step(run, job_name(rid), code=0)
    mgr._close_pod_step(run, job_name(rid), ended=JobState("Failed", message="x"))
    assert len(run.history) == before and len(_closing(run)) == 1
