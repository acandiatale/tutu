"""v2.1z-ac (2026-11-09, the deployment team's spec): Main is production-safe in `SBMIG_WEB_MODE=k8s-job` — the inputs are pinned at
intake (`control/request.json`), the control state is written before every step (`control/state.json`: queued → creating → created
→ running → finished | stopped | lost | failed), an uncertain create answer adopts the Job instead of creating a second one, only a
`job-status.json` of *this* run and attempt counts as the result, a stop deletes only a Job carrying this request's owner label and
waits for it to go, a restarted Main picks up every unfinished run from disk, and a bad template / missing RBAC verb refuses the start."""
from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path

import pytest
import yaml

import sbmig.web.runs as runs_mod
from sbmig.core.config import PACKAGE_ROOT
from sbmig.web import contract
from sbmig.web.contract import OWNER_LABEL, RUN_LABEL, ControlState, JobStatus, RequestRecord, RunState
from sbmig.web.k8s import JobState, K8sClient, K8sError, job_name, validate_worker_template
from sbmig.web.localjob import LocalJobClient
from sbmig.web.runs import LOCAL_LOST, RunManager

TEMPLATE = Path(__file__).resolve().parents[2] / "deploy" / "k8s" / "81-web-job.template.yaml"
ZIP = b"PK\x05\x06" + b"\0" * 18


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


def pod_writes(run_dir: Path, run_id: str, attempt: int | None = 1, exit_code: int = 0) -> None:
    """What the Worker leaves on the shared volume when it ends."""
    (run_dir / "report.md").write_text("# report\n", encoding="utf-8")
    (run_dir / "report.json").write_text(json.dumps({"success": exit_code == 0, "blocked": [], "unresolved": []}), encoding="utf-8")
    JobStatus(exit_code=exit_code, run_id=run_id, attempt=attempt).write(run_dir)


class Cluster:
    """A fake cluster that keeps Jobs by name with their labels; each Job's phases are scripted, and the "pod" writes its outputs
    the first time its Job reports Running (when `pod_attempt` is not False)."""
    namespace = "ns"

    def __init__(self, out_dir: Path, phases: list[str] | None = None, pod_attempt: int | None | bool = 1):
        self.out_dir = out_dir
        self.phases = list(phases or ["Creating", "Pending", "Running", "Succeeded"])
        self.pod_attempt = pod_attempt
        self.jobs: dict[str, dict] = {}
        self.created: list[dict] = []
        self.deleted: list[str] = []
        self.create_mode: str | None = None          # None | "conflict" | "transport-created" | "transport-lost"
        self.polls_after_delete = 0

    def add(self, name: str, labels: dict[str, str], phases: list[str] | None = None) -> None:
        self.jobs[name] = {"labels": dict(labels), "phases": list(phases or self.phases), "pod_done": False}

    def create_job(self, manifest: dict) -> str:
        name = manifest["metadata"]["name"]
        self.created.append(manifest)
        mode, self.create_mode = self.create_mode, None
        if mode == "transport-lost":
            raise K8sError(f"POST /apis/batch/v1/namespaces/ns/jobs: ConnectionError: connection reset ({name})")
        if name in self.jobs:
            raise K8sError("POST /apis/batch/v1/namespaces/ns/jobs: HTTP 409: AlreadyExists")
        self.add(name, manifest["metadata"].get("labels") or {})
        if mode == "conflict":                       # an earlier request reached the API, its answer was lost: now 409
            raise K8sError("POST /apis/batch/v1/namespaces/ns/jobs: HTTP 409: AlreadyExists")
        if mode == "transport-created":
            raise K8sError("POST /apis/batch/v1/namespaces/ns/jobs: ConnectionError: read timed out")
        return name

    def job_state(self, name: str) -> JobState:
        j = self.jobs.get(name)
        if j is None:
            return JobState("Missing", message="job not found")
        if j.get("deleting"):
            self.polls_after_delete += 1
        phase = j["phases"].pop(0) if len(j["phases"]) > 1 else j["phases"][0]
        if phase == "Missing":
            self.jobs.pop(name, None)
            return JobState("Missing", message="job not found")
        if phase == "Running" and not j["pod_done"] and self.pod_attempt is not False:
            j["pod_done"] = True
            rid = j["labels"][RUN_LABEL]
            pod_writes(self.out_dir / rid, rid, attempt=self.pod_attempt)   # type: ignore[arg-type]
        return JobState(phase, pod=None if phase == "Creating" else f"{name}-p", exit_code=0 if phase == "Succeeded" else None,
                        labels=dict(j["labels"]))

    def delete_job(self, name: str) -> None:
        self.deleted.append(name)
        if name in self.jobs:                        # the API accepts the delete; the Job lingers for two more polls
            self.jobs[name].update(deleting=True, phases=["Running", "Running", "Missing"])


def _wait(cond, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def _state(out: Path, rid: str) -> ControlState:
    cs = ControlState.read(out / rid)
    assert cs is not None
    return cs


def _journal(out: Path, rid: str) -> list[dict]:
    return [json.loads(ln) for ln in (out / rid / "journal.jsonl").read_text(encoding="utf-8").splitlines()]


def _mgr(tmp_path: Path, repo: Path, cluster: Cluster, **kw) -> RunManager:
    return RunManager(tmp_path / "out", repo, mode="k8s-job", job_client=cluster, job_template=TEMPLATE, **kw)


# ---- intake and the state sequence
def test_intake_pins_request_and_state_before_the_worker_starts(tmp_path: Path, repo: Path, monkeypatch):
    seen: list[tuple[bool, bool]] = []
    orig = RunManager._worker

    def worker(self, run):
        seen.append(((run.run_dir / "control" / "request.json").is_file(), (run.run_dir / "control" / "state.json").is_file()))
        orig(self, run)
    monkeypatch.setattr(RunManager, "_worker", worker)
    out = tmp_path / "out"
    cluster = Cluster(out)
    mgr = _mgr(tmp_path, repo, cluster)
    rid = mgr.start(ZIP, "my proj.zip", ["artifacts.local-e2e.yml"], stop_after="plan")
    assert mgr.runs[rid].done.wait(10)
    assert seen == [(True, True)]
    rec = RequestRecord.read(out / rid)
    assert rec is not None and rec.run_id == rid and rec.upload == "my_proj.zip" and rec.attempt == 1 and rec.mode == "k8s-job"
    assert rec.zip_sha256 == contract.sha256_file(out / rid / "source.zip") and rec.stop_after == "plan"
    assert rec.config_digests == {"default.yml": contract.sha256_file(repo / "config" / "default.yml"),
                                  "artifacts.local-e2e.yml": contract.sha256_file(repo / "config" / "artifacts.local-e2e.yml")}
    assert rec.sbmig_version == contract.sbmig_version() and rec.accepted_at.endswith("Z")
    # the Job carries the run id and this request's owner token, on the Job and on its pod template
    m = cluster.created[0]
    owner = mgr.runs[rid].owner
    assert owner and m["metadata"]["labels"][OWNER_LABEL] == owner and m["metadata"]["labels"][RUN_LABEL] == rid
    assert m["spec"]["template"]["metadata"]["labels"][OWNER_LABEL] == owner
    assert _state(out, rid).owner == owner


def test_the_state_sequence_on_disk(tmp_path: Path, repo: Path, monkeypatch):
    written: list[str] = []
    orig = ControlState.write

    def write(self, run_dir):
        written.append(self.state)
        return orig(self, run_dir)
    monkeypatch.setattr(ControlState, "write", write)
    out = tmp_path / "out"
    mgr = _mgr(tmp_path, repo, Cluster(out))
    rid = mgr.start(ZIP, "p.zip", ["artifacts.local-e2e.yml"])
    assert mgr.runs[rid].done.wait(10)
    assert written == ["queued", "creating", "created", "running", "finished"]
    cs = _state(out, rid)
    assert cs.state == "finished" and cs.exit_code == 0 and cs.job == job_name(rid)
    st = mgr.status(rid)
    assert st["verified"] is True and st["attempt"] == 1 and st["success"] is True and st["control"] == "finished"
    # evicted from memory: the directory answer reads the control state first
    mgr.runs.pop(rid)
    st = mgr.status(rid)
    assert st["state"] == "finished" and st["exit_code"] == 0 and st["attempt"] == 1 and st["job"]["name"] == job_name(rid)


# ---- an uncertain create answer never creates a second Job
def test_a_409_on_create_adopts_the_existing_job(tmp_path: Path, repo: Path):
    out = tmp_path / "out"
    cluster = Cluster(out, phases=["Running", "Succeeded"])
    cluster.create_mode = "conflict"
    mgr = _mgr(tmp_path, repo, cluster)
    rid = mgr.start(ZIP, "p.zip", ["artifacts.local-e2e.yml"])
    assert mgr.runs[rid].done.wait(10)
    assert len(cluster.jobs) <= 1 and len(cluster.created) == 1 and mgr.runs[rid].exit_code == 0
    events = [e["event"] for e in _journal(out, rid)]
    assert events.count("job_adopted") == 1 and "job_created" not in events
    assert _state(out, rid).state == "finished"


def test_a_transport_error_on_create_adopts_a_running_job_and_fails_when_there_is_none(tmp_path: Path, repo: Path):
    out = tmp_path / "out"
    cluster = Cluster(out, phases=["Running", "Succeeded"])
    cluster.create_mode = "transport-created"
    mgr = _mgr(tmp_path, repo, cluster)
    rid = mgr.start(ZIP, "p.zip", ["artifacts.local-e2e.yml"])
    assert mgr.runs[rid].done.wait(10) and mgr.runs[rid].exit_code == 0
    assert len(cluster.created) == 1 and any(e["event"] == "job_adopted" for e in _journal(out, rid))
    # the request never reached the API: no Job of that name → the original error, state failed, nothing adopted
    cluster.create_mode = "transport-lost"
    rid2 = mgr.start(ZIP, "q.zip", ["artifacts.local-e2e.yml"])
    assert mgr.runs[rid2].done.wait(10) and mgr.runs[rid2].exit_code == 3
    assert "ConnectionError" in (mgr.runs[rid2].error or "") and _state(out, rid2).state == "failed"
    assert not any(e["event"] == "job_adopted" for e in _journal(out, rid2))


# ---- result verification
def test_a_status_file_of_another_attempt_is_ignored_and_the_matching_one_ends_the_run(tmp_path: Path, repo: Path, caplog):
    out = tmp_path / "out"
    cluster = Cluster(out, phases=["Running"], pod_attempt=2)       # a stale result of attempt 2 lands on the volume
    mgr = _mgr(tmp_path, repo, cluster)
    with caplog.at_level(logging.WARNING, logger="sbmig.web"):
        rid = mgr.start(ZIP, "p.zip", ["artifacts.local-e2e.yml"])
        assert _wait(lambda: (out / rid / "job-status.json").is_file())
        time.sleep(0.3)
        assert not mgr.runs[rid].done.is_set()
        JobStatus(exit_code=2, run_id=rid, attempt=1).write(out / rid)
        assert mgr.runs[rid].done.wait(10)
    assert mgr.runs[rid].exit_code == 2 and mgr.status(rid)["verified"] is True
    warns = [r for r in caplog.records if "belongs to another run/attempt" in r.getMessage()]
    assert len(warns) == 1


def test_a_vanished_job_is_lost_and_unverified(tmp_path: Path, repo: Path):
    out = tmp_path / "out"
    mgr = _mgr(tmp_path, repo, Cluster(out, phases=["Running", "Missing"], pod_attempt=False))
    rid = mgr.start(ZIP, "p.zip", ["artifacts.local-e2e.yml"])
    assert mgr.runs[rid].done.wait(10)
    st = mgr.status(rid)
    assert st["exit_code"] == 3 and st["verified"] is False and st.get("success") is None
    assert "disappeared" in st["error"] and "result not verified" in st["error"]
    cs = _state(out, rid)
    assert cs.state == "lost" and cs.exit_code == 3 and "disappeared" in (cs.error or "")


def test_a_container_exit_code_without_a_status_file_is_unverified(tmp_path: Path, repo: Path):
    out = tmp_path / "out"
    mgr = _mgr(tmp_path, repo, Cluster(out, phases=["Running", "Succeeded"], pod_attempt=False))
    rid = mgr.start(ZIP, "p.zip", ["artifacts.local-e2e.yml"])
    assert mgr.runs[rid].done.wait(10)
    st = mgr.status(rid)
    assert st["exit_code"] == 0 and st["verified"] is False and st.get("success") is None and "result not verified" in st["error"]
    assert _state(out, rid).state == "lost"


# ---- stop: ownership before delete, wait for the Job to go
def test_stop_refuses_to_delete_a_job_owned_by_another_request(tmp_path: Path, repo: Path, monkeypatch):
    monkeypatch.setenv("SBMIG_JOB_STOP_GRACE_S", "0.1")
    out = tmp_path / "out"
    cluster = Cluster(out, phases=["Running"], pod_attempt=False)
    mgr = _mgr(tmp_path, repo, cluster)
    rid = mgr.start(ZIP, "p.zip", ["artifacts.local-e2e.yml"])
    assert _wait(lambda: _state(out, rid).state == "running")
    cluster.jobs[job_name(rid)]["labels"][OWNER_LABEL] = "someone-else"
    assert mgr.stop(rid) and mgr.runs[rid].done.wait(10)
    assert cluster.deleted == [] and mgr.runs[rid].exit_code == 3
    assert "is not owned by this request — not deleted" in (mgr.runs[rid].error or "")
    assert _state(out, rid).state == "stopped"


def test_stop_deletes_its_own_job_and_waits_until_it_is_gone(tmp_path: Path, repo: Path, monkeypatch):
    monkeypatch.setenv("SBMIG_JOB_STOP_GRACE_S", "0.1")
    out = tmp_path / "out"
    cluster = Cluster(out, phases=["Running"], pod_attempt=False)
    mgr = _mgr(tmp_path, repo, cluster)
    rid = mgr.start(ZIP, "p.zip", ["artifacts.local-e2e.yml"])
    assert _wait(lambda: _state(out, rid).state == "running")
    assert mgr.stop(rid) and mgr.runs[rid].done.wait(10)
    assert cluster.deleted == [job_name(rid)] and cluster.polls_after_delete >= 3 and job_name(rid) not in cluster.jobs
    assert mgr.runs[rid].exit_code == 3 and "deleted after the grace period" in (mgr.runs[rid].error or "")
    cs = _state(out, rid)
    assert cs.state == "stopped" and cs.exit_code == 3


# ---- restart recovery
def _seed(out: Path, rid: str, state: str, accepted_at: str = "2026-11-09T01:00:00Z", owner: str = "own3r") -> Path:
    rd = out / rid
    rd.mkdir(parents=True)
    (rd / "source.zip").write_bytes(ZIP)
    RequestRecord(run_id=rid, accepted_at=accepted_at, upload="p.zip", configs=["artifacts.local-e2e.yml"],
                  zip_sha256=contract.sha256_file(rd / "source.zip"), config_digests={}, sbmig_version=contract.sbmig_version(),
                  mode="k8s-job").write(rd)
    ControlState(run_id=rid, state=state, owner=owner, job=None if state == "queued" else job_name(rid),
                 accepted_at=accepted_at).write(rd)
    return rd


def test_restart_adopts_a_created_job_and_finishes_it(tmp_path: Path, repo: Path):
    out = tmp_path / "out"
    _seed(out, "web-a", "created")
    cluster = Cluster(out, phases=["Running", "Succeeded"])
    cluster.add(job_name("web-a"), {RUN_LABEL: "web-a", OWNER_LABEL: "own3r"})
    mgr = _mgr(tmp_path, repo, cluster)
    assert mgr.recovered == ["web-a"] and mgr.runs["web-a"].done.wait(10)
    assert cluster.created == [] and mgr.runs["web-a"].exit_code == 0 and mgr.status("web-a")["verified"] is True
    assert any(e["event"] == "job_adopted" for e in _journal(out, "web-a"))
    assert _state(out, "web-a").state == "finished"


def test_restart_reenqueues_queued_runs_in_acceptance_order(tmp_path: Path, repo: Path):
    out = tmp_path / "out"
    _seed(out, "web-1-later", "queued", accepted_at="2026-11-09T02:00:00Z", owner="o-later")
    _seed(out, "web-2-first", "queued", accepted_at="2026-11-09T01:00:00Z", owner="o-first")
    (out / "_uploads" / "control").mkdir(parents=True)              # `_` dirs are never runs
    cluster = Cluster(out, phases=["Running", "Succeeded"])
    mgr = _mgr(tmp_path, repo, cluster)
    assert mgr.recovered == ["web-2-first", "web-1-later"]
    assert all(mgr.runs[r].done.wait(10) for r in mgr.recovered)
    assert [m["metadata"]["labels"][RUN_LABEL] for m in cluster.created] == ["web-2-first", "web-1-later"]
    assert cluster.created[0]["metadata"]["labels"][OWNER_LABEL] == "o-first"           # the owner token survives the restart
    assert all(_state(out, r).state == "finished" for r in mgr.recovered)


def test_restart_in_creating_adopts_an_existing_job_or_creates_one(tmp_path: Path, repo: Path):
    out = tmp_path / "out"
    _seed(out, "web-x", "creating", accepted_at="2026-11-09T01:00:00Z")
    _seed(out, "web-y", "creating", accepted_at="2026-11-09T02:00:00Z")
    cluster = Cluster(out, phases=["Running", "Succeeded"])
    cluster.add(job_name("web-x"), {RUN_LABEL: "web-x", OWNER_LABEL: "own3r"})
    mgr = _mgr(tmp_path, repo, cluster)
    assert all(mgr.runs[r].done.wait(10) for r in ("web-x", "web-y"))
    assert [m["metadata"]["name"] for m in cluster.created] == [job_name("web-y")]
    assert any(e["event"] == "job_adopted" for e in _journal(out, "web-x"))
    assert not (out / "web-y" / "journal.jsonl").is_file() or not any(e["event"] == "job_adopted" for e in _journal(out, "web-y"))
    assert _state(out, "web-x").state == "finished" and _state(out, "web-y").state == "finished"


def test_restart_skips_broken_files_and_terminal_runs(tmp_path: Path, repo: Path):
    out = tmp_path / "out"
    (out / "web-broken" / "control").mkdir(parents=True)
    (out / "web-broken" / "control" / "state.json").write_text("{not json", encoding="utf-8")
    rd = _seed(out, "web-norequest", "created")
    (rd / "control" / "request.json").unlink()
    _seed(out, "web-done", "queued")
    cs = _state(out, "web-done")
    cs.transition(RunState.FINISHED, exit_code=0).write(out / "web-done")
    mgr = _mgr(tmp_path, repo, Cluster(out))
    assert mgr.recovered == [] and mgr.runs == {}


def test_local_job_restart_marks_unfinished_runs_lost(tmp_path: Path, repo: Path):
    out = tmp_path / "out"
    _seed(out, "web-l", "running")
    mgr = RunManager(out, repo, mode="local-job", job_client=LocalJobClient(repo), job_template=TEMPLATE)
    assert mgr.recovered == ["web-l"] and "web-l" not in mgr.runs
    cs = _state(out, "web-l")
    assert cs.state == "lost" and cs.error == LOCAL_LOST and cs.exit_code == 3
    st = mgr.status("web-l")
    assert st["state"] == "finished" and st["exit_code"] == 3 and st["error"] == LOCAL_LOST


def test_directory_status_of_an_unfinished_control_state(tmp_path: Path, repo: Path):
    out = tmp_path / "out"
    mgr = _mgr(tmp_path, repo, Cluster(out))
    _seed(out, "web-q", "queued")
    _seed(out, "web-r", "running")
    assert mgr.status("web-q")["state"] == "queued"
    st = mgr.status("web-r")
    assert st["state"] == "running" and st["job"]["name"] == job_name("web-r") and st["attempt"] == 1 and st["exit_code"] is None


# ---- startup validation
def _template(tmp_path: Path, mutate=None) -> Path:
    job = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    for m in job["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]:
        if m["mountPath"] == "/data":
            m["mountPath"] = str(tmp_path.resolve())                  # the PVC mount holds this test's output dir
    if mutate:
        mutate(job)
    p = tmp_path / f"tpl-{time.monotonic_ns()}.yaml"
    p.write_text(yaml.safe_dump(job), encoding="utf-8")
    return p


class _Rbac:
    namespace = "agent"

    def __init__(self, denied: set[tuple[str, str]] | None = None):
        self.denied = denied or set()
        self.asked: list[tuple[str, str, str]] = []

    def can_i(self, verb: str, resource: str, group: str = "") -> bool:
        self.asked.append((verb, resource, group))
        return (verb, resource) not in self.denied


def _production(monkeypatch, client) -> None:
    monkeypatch.setattr(runs_mod, "in_cluster_available", lambda: True)
    monkeypatch.setattr(K8sClient, "connect", classmethod(lambda cls, **kw: client))


def _container(job: dict) -> dict:
    return job["spec"]["template"]["spec"]["containers"][0]


def test_startup_accepts_a_valid_template_and_checks_every_rbac_verb(tmp_path: Path, repo: Path, monkeypatch):
    rbac = _Rbac()
    _production(monkeypatch, rbac)
    mgr = RunManager(tmp_path / "out", repo, mode="k8s-job", job_template=_template(tmp_path))
    assert mgr._job_client is rbac and mgr._job_template_doc is not None
    assert set(rbac.asked) == {("create", "jobs", "batch"), ("get", "jobs", "batch"), ("list", "jobs", "batch"),
                               ("watch", "jobs", "batch"), ("delete", "jobs", "batch"),
                               ("get", "pods", ""), ("list", "pods", ""), ("watch", "pods", "")}


def test_startup_refuses_an_envfrom_template(tmp_path: Path, repo: Path, monkeypatch):
    _production(monkeypatch, _Rbac())
    tpl = _template(tmp_path, lambda j: _container(j).update(envFrom=[{"secretRef": {"name": "sbmig-llm"}}]))
    with pytest.raises(ValueError, match="MIGRATION_TEMPLATE_INVALID.*envFrom.*explicit"):
        RunManager(tmp_path / "out", repo, mode="k8s-job", job_template=tpl)


def test_startup_refuses_a_template_without_the_pvc_mount(tmp_path: Path, repo: Path, monkeypatch):
    _production(monkeypatch, _Rbac())

    def drop(job):
        c = _container(job)
        c["volumeMounts"] = [m for m in c["volumeMounts"] if m["name"] != "data"]
    with pytest.raises(ValueError, match="MIGRATION_TEMPLATE_INVALID.*exactly one volume mount"):
        RunManager(tmp_path / "out", repo, mode="k8s-job", job_template=_template(tmp_path, drop))


def test_startup_refuses_missing_rbac_verbs(tmp_path: Path, repo: Path, monkeypatch):
    _production(monkeypatch, _Rbac(denied={("create", "jobs"), ("watch", "pods")}))
    with pytest.raises(ValueError, match=r"may not create jobs, watch pods in agent — RBAC: deploy/k8s/45-web-rbac.yaml"):
        RunManager(tmp_path / "out", repo, mode="k8s-job", job_template=_template(tmp_path))


@pytest.mark.parametrize(("mutate", "reason"), [
    (lambda j: j["spec"].update(backoffLimit=2), "backoffLimit"),
    (lambda j: j["spec"].update(parallelism=2), "parallelism"),
    (lambda j: j["spec"]["template"]["spec"].update(restartPolicy="OnFailure"), "restartPolicy"),
    (lambda j: j["spec"]["template"]["spec"].update(initContainers=[{"name": "i", "image": "x"}]), "initContainers"),
    (lambda j: j["spec"]["template"]["spec"]["containers"].append({"name": "side", "image": "x"}), "exactly one container"),
    (lambda j: _container(j).pop("image"), "name and an image"),
    (lambda j: _container(j)["env"].append({"name": "LLM_RECORD_DIR", "value": "/data/rec"}), "LLM_RECORD_DIR"),
    (lambda j: _container(j)["env"].append({"name": "NEXUS_PASSWORD_X", "value": "hunter22"}), "valueFrom"),
    (lambda j: next(m for m in _container(j)["volumeMounts"] if m["name"] == "data").update(subPath="x"), "whole claim"),
    (lambda j: next(m for m in _container(j)["volumeMounts"] if m["name"] == "data").update(readOnly=True), "whole claim"),
    (lambda j: _container(j)["volumeMounts"].append({"name": "m2", "mountPath": "OUT/inner"}), "under the output directory"),
    (lambda j: j.update(kind="Deployment"), "batch/v1 Job"),
])
def test_validate_worker_template_rules(tmp_path: Path, mutate, reason):
    out = tmp_path.resolve() / "out"

    def apply(job):
        mutate(job)
        for m in _container(job).get("volumeMounts") or []:
            m["mountPath"] = m["mountPath"].replace("OUT", str(out))
    with pytest.raises(K8sError, match=f"MIGRATION_TEMPLATE_INVALID.*{reason}"):
        validate_worker_template(_template(tmp_path, apply), out)
    assert validate_worker_template(_template(tmp_path), out)["kind"] == "Job"
    with pytest.raises(K8sError, match="not readable"):
        validate_worker_template(tmp_path / "nope.yaml", out)


# ---- the client: labels and the access review
class _Resp:
    def __init__(self, status_code: int, body: dict | None = None):
        self.status_code = status_code
        self._body = body or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class _Session:
    def __init__(self, routes):
        self.routes = routes
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        hit = self.routes.get((method, url.split("?")[0]))
        return hit(kw) if callable(hit) else (hit or _Resp(404, {}))


def test_job_state_reports_the_job_labels_and_can_i_sends_a_self_subject_access_review():
    base = "https://10.0.0.1:443"
    jobs = f"{base}/apis/batch/v1/namespaces/ns/jobs"
    ssar = f"{base}/apis/authorization.k8s.io/v1/selfsubjectaccessreviews"

    def review(kw):
        attrs = kw["json"]["spec"]["resourceAttributes"]
        return _Resp(201, {"status": {"allowed": not (attrs["verb"] == "delete" and attrs["resource"] == "jobs")}})
    s = _Session({("GET", f"{jobs}/sbmig-r1"): _Resp(200, {"metadata": {"labels": {RUN_LABEL: "r1", OWNER_LABEL: "tok"}}, "status": {}}),
                  ("GET", f"{base}/api/v1/namespaces/ns/pods"): _Resp(200, {"items": []}),
                  ("POST", ssar): review})
    c = K8sClient(base, "tok", "ns", session=s)
    st = c.job_state("sbmig-r1")
    assert st.phase == "Creating" and st.labels == {RUN_LABEL: "r1", OWNER_LABEL: "tok"}
    assert c.job_state("sbmig-none").labels == {}
    assert c.can_i("create", "jobs", "batch") is True and c.can_i("delete", "jobs", "batch") is False
    method, url, kw = s.calls[-1]
    assert (method, url) == ("POST", ssar)
    assert kw["json"]["apiVersion"] == "authorization.k8s.io/v1" and kw["json"]["kind"] == "SelfSubjectAccessReview"
    assert kw["json"]["spec"]["resourceAttributes"] == {"namespace": "ns", "verb": "delete", "resource": "jobs", "group": "batch"}
    s.routes[("POST", ssar)] = _Resp(403, {"message": "forbidden"})
    with pytest.raises(K8sError, match="HTTP 403"):
        c.can_i("get", "pods")


def test_a_status_file_that_appears_after_the_job_ended_is_still_accepted(tmp_path: Path, repo: Path, monkeypatch):
    """NFS attribute caching can show the Worker's job-status.json seconds after the Job turned Succeeded: Main waits
    `SBMIG_JOB_STATUS_GRACE_S` for it instead of calling the result unverified."""
    monkeypatch.setenv("SBMIG_JOB_POLL_S", "0.05")
    monkeypatch.setenv("SBMIG_JOB_STATUS_GRACE_S", "5")
    out = tmp_path / "out"
    cluster = Cluster(out, phases=["Running", "Succeeded"], pod_attempt=False)      # the pod writes nothing by itself
    mgr = _mgr(tmp_path, repo, cluster)
    rid = mgr.start(b"PK\x05\x06" + b"\0" * 18, "p.zip", ["artifacts.local-e2e.yml"])
    assert _wait(lambda: mgr.status(rid).get("job", {}).get("phase") == "Succeeded")
    time.sleep(0.3)
    assert not mgr.runs[rid].done.is_set()                       # waiting for the volume, not finished
    (out / rid / "report.json").write_text(json.dumps({"success": True}), encoding="utf-8")
    JobStatus(exit_code=0, run_id=rid, attempt=1).write(out / rid)   # the late file
    assert mgr.runs[rid].done.wait(5)
    st = mgr.status(rid)
    assert st["exit_code"] == 0 and st["verified"] is True and st["control"] == "finished"
