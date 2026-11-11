"""2026-11-11 (wave 4): a chained remediation that ended AWAITING_APPROVAL / AWAITING_CONFIRMATION / STOPPED in its build pod is
resumed in a NEW build pod — a new attempt of the same run with the action `remediation-resume`. RunManager side: the Job (name
`<migration job>-rem<attempt>`, the run label + an owner token of its own, env `SBMIG_WORKER_ACTION`), the same FIFO line and slots
as uploads, one resume per run, restart recovery, stop. Worker side: the action runs `sbmig remediation-resume` (chain.resume), not
`sbmig run`, and writes `remediation-job-status.json`. The migration's report / result.zip / state / web-status never change."""
from __future__ import annotations

import json
import shutil
import signal
import sys
import time
from pathlib import Path

import pytest

from sbmig.core.config import PACKAGE_ROOT
from sbmig.remediations.models import StateConflict
from sbmig.web import contract, worker
from sbmig.web.contract import OWNER_LABEL, RESUME_STATE_FILE, RESUME_STATUS_FILE, RUN_LABEL, ControlState, JobStatus
from sbmig.web.k8s import job_name
from sbmig.web.localjob import LocalJobClient
from sbmig.web.runs import RunManager, resume_job_name

from .test_jobmode_hardening import TEMPLATE, ZIP, Cluster, pod_writes

CFG = "artifacts.local-e2e.yml"
ACTION = contract.ACTION_REMEDIATION_RESUME
MIGRATION_FILES = ("report.md", "report.json", "result.zip", "job-status.json", "web-status.json", "control/state.json",
                   "control/request.json")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    shutil.copy(PACKAGE_ROOT / "config" / CFG, repo / "config" / CFG)
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
        for run in list(mgr.runs.values()) + list(mgr.resumes.values()):
            run.done.wait(10)


def _wait(cond, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def _env(manifest: dict) -> dict[str, str]:
    return {e["name"]: e.get("value") for e in manifest["spec"]["template"]["spec"]["containers"][0]["env"]}


def finished_run(out: Path, rid: str, repo: Path | None = None) -> dict[str, bytes]:
    """A migration run a Job finished with BUILD SUCCESS and a chained remediation left waiting; returns its artefacts' bytes."""
    rd = out / rid
    rd.mkdir(parents=True)
    (rd / "source.zip").write_bytes(ZIP)
    contract.RequestRecord(run_id=rid, accepted_at="2026-11-11T01:00:00Z", upload="p.zip", configs=[CFG],
                           zip_sha256=contract.sha256_file(rd / "source.zip"),
                           config_digests=contract.config_digests(repo, [CFG]) if repo else {},
                           sbmig_version=contract.sbmig_version(), mode="k8s-job").write(rd)
    ControlState(run_id=rid, state="finished", owner="mig-owner", job=job_name(rid), exit_code=0, slot=1).write(rd)
    pod_writes(rd, rid, attempt=1)
    (rd / "result.zip").write_bytes(b"PK\x05\x06" + b"\1" * 18)
    (rd / "web-status.json").write_text(json.dumps({"run_id": rid, "exit_code": 0, "success": True}))
    (rd / "sbmig.log").write_text("2026-11-11 01:00:00,000 INFO x: SBMIG-RESULT ok run=" + rid + "\n")
    (rd / "remediation").mkdir()
    (rd / "remediation" / "state.json").write_text(json.dumps({"id": rid, "status": "AWAITING_APPROVAL"}))
    return snapshot(rd)


def snapshot(rd: Path) -> dict[str, bytes]:
    return {n: (rd / n).read_bytes() for n in MIGRATION_FILES}


def resume_state(out: Path, rid: str) -> ControlState:
    cs = ControlState.read(out / rid, RESUME_STATE_FILE)
    assert cs is not None
    return cs


def pod_resumes(rd: Path, exit_code: int = 0) -> None:
    """What the resume Worker leaves on the volume: its own status file for the attempt Main recorded."""
    cs = ControlState.read(rd, RESUME_STATE_FILE)
    JobStatus(exit_code=exit_code, run_id=rd.name, attempt=cs.attempt, action=ACTION).write(rd, RESUME_STATUS_FILE)


class ResumeCluster(Cluster):
    """The fake cluster of the Job-mode tests; a resume Job's "pod" writes the resume status file on its first Running
    (unless `hang`, then the test ends it with `finish`). Migration Jobs never write here (pod_attempt False)."""

    def __init__(self, out_dir: Path, phases: list[str] | None = None, hang: bool = False):
        super().__init__(out_dir, phases=phases or ["Pending", "Running", "Running", "Succeeded"], pod_attempt=False)
        self.hang = hang
        self.manifests: dict[str, dict] = {}

    def create_job(self, manifest: dict) -> str:
        name = super().create_job(manifest)
        self.manifests[name] = manifest
        return name

    def job_state(self, name: str):
        st = super().job_state(name)
        j = self.jobs.get(name)
        m = self.manifests.get(name)
        if st.phase == "Running" and j is not None and m is not None and not j.get("resumed") and not self.hang \
                and _env(m).get(contract.ACTION_ENV) == ACTION:
            j["resumed"] = True
            pod_resumes(self.out_dir / m["metadata"]["labels"][RUN_LABEL])
        return st

    def finish(self, name: str, resume: bool = True) -> None:
        rid = self.manifests[name]["metadata"]["labels"][RUN_LABEL]
        if resume:
            pod_resumes(self.out_dir / rid)
        else:
            pod_writes(self.out_dir / rid, rid, attempt=1)
        self.jobs[name]["phases"] = ["Succeeded"]


def _mgr(tmp_path: Path, repo: Path, cluster, managers, **kw) -> RunManager:
    mgr = RunManager(tmp_path / "out", repo, mode="k8s-job", job_client=cluster, job_template=TEMPLATE, **kw)
    managers.append(mgr)
    return mgr


# ---- dispatch
def test_resume_creates_a_job_with_the_action_its_own_name_and_owner_and_leaves_the_migration_alone(tmp_path, repo, managers):
    out = tmp_path / "out"
    rid = "web-20261111-010000-aaaaaa"
    before = finished_run(out, rid)
    (out / rid / "stop.request").write_text("left over from the migration\n")
    cluster = ResumeCluster(out)
    mgr = _mgr(tmp_path, repo, cluster, managers)
    info = mgr.resume_remediation(rid)
    assert info["attempt"] == 2 and info["active"] is True and info["job"] == resume_job_name(rid, 2)
    assert not (out / rid / "stop.request").exists()                  # the migration's leftover would stop the new attempt
    assert mgr.resumes[rid].done.wait(10)
    [m] = cluster.created
    name = resume_job_name(rid, 2)
    assert m["metadata"]["name"] == name != job_name(rid) and name.endswith("-rem2") and len(name) <= 63
    cs = resume_state(out, rid)
    assert m["metadata"]["labels"][RUN_LABEL] == rid and m["metadata"]["labels"][OWNER_LABEL] == cs.owner != "mig-owner"
    assert m["spec"]["template"]["metadata"]["labels"][OWNER_LABEL] == cs.owner
    env = _env(m)
    assert env[contract.ACTION_ENV] == ACTION and env[contract.SLOT_ENV] == "1" and env["SBMIG_RUN_ID"] == rid
    assert cs.state == "finished" and cs.exit_code == 0 and cs.attempt == 2 and cs.action == ACTION and cs.job == name
    assert cs.env_pins is not None and cs.env_pin_policy == "refuse" and cs.slot == 1
    # the migration's artefacts and verdict are untouched
    assert snapshot(out / rid) == before
    st = mgr.status(rid)
    assert st["state"] == "finished" and st["exit_code"] == 0 and st["success"] is True and st["attempt"] == 1
    assert st["remediation_job"]["state"] == "finished" and st["remediation_job"]["verified"] is True
    assert st["remediation_job"]["exit_code"] == 0 and st["remediation_job"]["active"] is False
    events = [json.loads(x) for x in (out / rid / "journal.jsonl").read_text().splitlines()]
    created = [e for e in events if e["event"] == "job_created"]
    assert created and created[0]["action"] == ACTION and created[0]["job"] == name and created[0]["attempt"] == 2
    assert any(e["event"] == "remediation_resume_queued" for e in events)
    assert mgr.parallel() == {"slots": 1, "running": 0, "waiting": 0}


def test_resume_refusals(tmp_path, repo, managers):
    out = tmp_path / "out"
    rid = "web-20261111-010000-bbbbbb"
    finished_run(out, rid)
    cluster = ResumeCluster(out, hang=True)
    mgr = _mgr(tmp_path, repo, cluster, managers)
    with pytest.raises(StateConflict):
        mgr.resume_remediation("web-20261111-010000-nonexx")            # unknown run: no request.json
    running = "web-20261111-010000-cccccc"
    finished_run(out, running)
    ControlState(run_id=running, state="running", owner="o", job=job_name(running)).write(out / running)
    with pytest.raises(StateConflict) as ei:
        mgr.resume_remediation(running)                                 # the migration has not finished
    assert "not finished" in str(ei.value)
    mgr.resume_remediation(rid)
    assert _wait(lambda: len(cluster.created) == 1)
    with pytest.raises(StateConflict) as ei:
        mgr.resume_remediation(rid)                                     # one active resume per run
    assert "already" in str(ei.value)
    assert mgr.remediation_active(rid) and len(cluster.created) == 1
    cluster.finish(resume_job_name(rid, 2))
    assert mgr.resumes[rid].done.wait(10) and not mgr.remediation_active(rid)
    info = mgr.resume_remediation(rid)                                  # a later resume is attempt 3 with a new name
    assert info["attempt"] == 3 and info["job"] == resume_job_name(rid, 3)
    cluster.hang = False
    assert _wait(lambda: len(cluster.created) == 2)
    cluster.finish(resume_job_name(rid, 3))
    assert mgr.resumes[rid].done.wait(10)
    inproc = RunManager(tmp_path / "out2", repo, mode="in-process")
    managers.append(inproc)
    with pytest.raises(StateConflict):
        inproc.resume_remediation(rid)


def test_a_resume_waits_in_the_same_line_as_uploads_and_takes_a_slot(tmp_path, repo, managers):
    out = tmp_path / "out"
    rid = "web-20261111-010000-dddddd"
    before = finished_run(out, rid)
    cluster = ResumeCluster(out, phases=["Running"], hang=True)
    mgr = _mgr(tmp_path, repo, cluster, managers)
    first = mgr.start(ZIP, "a.zip", [CFG])
    assert _wait(lambda: mgr.status(first)["state"] == "running" and len(cluster.created) == 1)
    mgr.resume_remediation(rid)
    later = mgr.start(ZIP, "b.zip", [CFG])
    st = mgr.status(rid)
    assert st["state"] == "finished"                                   # the migration stays finished while its resume waits
    assert st["remediation_job"]["state"] == "queued" and st["remediation_job"]["queue"] == {"position": 1, "ahead": 0}
    assert mgr.status(later).get("queue") == {"position": 2, "ahead": 1}
    assert mgr.parallel() == {"slots": 1, "running": 1, "waiting": 2}
    cluster.finish(job_name(first), resume=False)
    assert _wait(lambda: len(cluster.created) == 2)
    assert cluster.created[1]["metadata"]["name"] == resume_job_name(rid, 2)      # FIFO: the resume before the later upload
    assert _wait(lambda: mgr.status(rid)["remediation_job"]["state"] == "running")
    assert mgr.status(rid)["remediation_job"]["slot"] == 1 and mgr.status(later)["queue"]["position"] == 1
    cluster.finish(resume_job_name(rid, 2))
    assert _wait(lambda: len(cluster.created) == 3)
    assert cluster.created[2]["metadata"]["labels"][RUN_LABEL] == later
    assert mgr.resumes[rid].done.wait(10)
    cluster.finish(job_name(later), resume=False)
    assert mgr.runs[later].done.wait(10)
    assert snapshot(out / rid) == before


def test_restart_reattaches_to_a_recorded_resume_job(tmp_path, repo, managers):
    out = tmp_path / "out"
    rid = "web-20261111-010000-eeeeee"
    before = finished_run(out, rid)
    name = resume_job_name(rid, 2)
    ControlState(run_id=rid, state="running", attempt=2, owner="resume-owner", job=name, slot=2,
                 action=ACTION).write(out / rid, RESUME_STATE_FILE)
    cluster = ResumeCluster(out, phases=["Running", "Running", "Succeeded"])
    cluster.add(name, {RUN_LABEL: rid, OWNER_LABEL: "resume-owner"})
    cluster.manifests[name] = {"metadata": {"name": name, "labels": {RUN_LABEL: rid}},
                               "spec": {"template": {"spec": {"containers": [{"env": [
                                   {"name": contract.ACTION_ENV, "value": ACTION}]}]}}}}
    mgr = _mgr(tmp_path, repo, cluster, managers, max_parallel=3)
    assert mgr.recovered == [f"{rid}#{ACTION}"]
    run = mgr.resumes[rid]
    assert run.slot == 2 and run.job_name == name and rid not in mgr.runs
    assert run.done.wait(10)
    assert cluster.created == []                                        # adopted, never created again
    cs = resume_state(out, rid)
    assert cs.state == "finished" and cs.exit_code == 0
    assert snapshot(out / rid) == before
    assert mgr.status(rid)["remediation_job"]["verified"] is True


def test_restart_creates_a_queued_resume_and_local_job_marks_a_running_one_lost(tmp_path, repo, managers):
    out = tmp_path / "out"
    rid = "web-20261111-010000-ffffff"
    finished_run(out, rid)
    ControlState(run_id=rid, state="queued", attempt=2, action=ACTION).write(out / rid, RESUME_STATE_FILE)
    cluster = ResumeCluster(out)
    mgr = _mgr(tmp_path, repo, cluster, managers)
    assert mgr.resumes[rid].done.wait(10)
    assert [m["metadata"]["name"] for m in cluster.created] == [resume_job_name(rid, 2)]
    assert resume_state(out, rid).state == "finished"

    out2 = tmp_path / "out2"
    rid2 = "web-20261111-010000-gggggg"
    before = finished_run(out2, rid2)
    ControlState(run_id=rid2, state="running", attempt=2, action=ACTION, job=resume_job_name(rid2, 2)).write(
        out2 / rid2, RESUME_STATE_FILE)
    local = RunManager(out2, repo, mode="local-job", job_client=LocalJobClient(repo))
    managers.append(local)
    assert local.recovered == [f"{rid2}#{ACTION}"]
    assert resume_state(out2, rid2).state == "lost" and snapshot(out2 / rid2) == before
    assert local.status(rid2)["state"] == "finished" and local.status(rid2)["remediation_job"]["state"] == "finished"


def test_stop_a_waiting_resume_leaves_the_line_and_a_running_one_gets_only_stop_request(tmp_path, repo, managers):
    out = tmp_path / "out"
    rid = "web-20261111-010000-hhhhhh"
    finished_run(out, rid)
    cluster = ResumeCluster(out, phases=["Running"], hang=True)
    mgr = _mgr(tmp_path, repo, cluster, managers)
    first = mgr.start(ZIP, "a.zip", [CFG])
    assert _wait(lambda: len(cluster.created) == 1)
    mgr.resume_remediation(rid)
    assert mgr.stop_remediation(rid) == "withdrawn"
    assert mgr.resumes[rid].done.wait(10)
    assert resume_state(out, rid).state == "stopped" and len(cluster.created) == 1
    assert mgr.parallel()["waiting"] == 0
    cluster.finish(job_name(first), resume=False)
    assert mgr.runs[first].done.wait(10)
    # running: stop.request only, the Job is never deleted by the web pod
    mgr.resume_remediation(rid)
    assert _wait(lambda: len(cluster.created) == 2 and mgr.status(rid)["remediation_job"]["state"] == "running")
    assert _wait(lambda: resume_state(out, rid).state in ("created", "running"))
    assert mgr.stop_remediation(rid) == "requested"
    assert (out / rid / "stop.request").is_file()
    assert mgr.status(rid)["remediation_job"]["stop_requested"] is True
    time.sleep(0.3)
    assert cluster.deleted == []
    cluster.finish(resume_job_name(rid, 3))
    assert mgr.resumes[rid].done.wait(10)
    assert resume_state(out, rid).state == "finished" and cluster.deleted == []
    assert mgr.stop_remediation(rid) is None


def test_local_job_runs_the_resume_action_as_a_subprocess(tmp_path, repo, managers):
    out = tmp_path / "out"
    rid = "web-20261111-010000-iiiiii"
    before = finished_run(out, rid)
    script = tmp_path / "fake_worker.py"
    script.write_text(
        "import json, os, sys\n"
        "rd = os.path.join(os.environ['SBMIG_OUT'], os.environ['SBMIG_RUN_ID'])\n"
        "cs = json.load(open(os.path.join(rd, 'control', 'remediation-resume.json')))\n"
        "json.dump({'exit_code': 0, 'run_id': os.environ['SBMIG_RUN_ID'], 'attempt': cs['attempt'],\n"
        "           'action': os.environ.get('SBMIG_WORKER_ACTION')},\n"
        "          open(os.path.join(rd, 'remediation-job-status.json'), 'w'))\n", encoding="utf-8")
    client = LocalJobClient(repo, command_factory=lambda env: [sys.executable, str(script)], echo=False)
    mgr = RunManager(out, repo, mode="local-job", job_client=client)
    managers.append(mgr)
    mgr.resume_remediation(rid)
    assert mgr.resumes[rid].done.wait(15)
    status = JobStatus.read(out / rid, RESUME_STATUS_FILE)
    assert status.action == ACTION and status.attempt == 2
    assert resume_state(out, rid).state == "finished"
    assert mgr.status(rid)["remediation_job"]["verified"] is True
    assert snapshot(out / rid) == before


# ---- the Worker's action
def _resume_setup(tmp_path: Path, attempt: int = 2, pins: dict | None = None, policy: str | None = None) -> dict:
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True, exist_ok=True)
    (repo / "config" / "default.yml").write_text("x: 1\n")
    (repo / "config" / CFG).write_text("artifacts: {}\n")
    out = tmp_path / "out"
    rid = "web-20261111-020000-wwwwww"
    before = finished_run(out, rid, repo)
    rd = out / rid
    (rd / "source.zip").write_bytes(b"PK changed after intake")           # the resume never reads the migration zip
    ControlState(run_id=rid, state="running", attempt=attempt, job=resume_job_name(rid, attempt), action=ACTION,
                 env_pins=pins, env_pin_policy=policy).write(rd, RESUME_STATE_FILE)
    env = {"SBMIG_SOURCE_ZIP": str(rd / "source.zip"), "SBMIG_OUT": str(out), "SBMIG_RUN_ID": rid, "SBMIG_APP_DIR": str(repo),
           "SBMIG_PYTHON": "py", contract.ACTION_ENV: ACTION}
    return {"rd": rd, "rid": rid, "env": env, "before": before, "repo": repo}


class _Sigs:
    def __init__(self):
        self.handler = None

    def __call__(self, handler):
        self.handler = handler
        return lambda: None


def test_worker_action_runs_chain_resume_not_sbmig_run(tmp_path, capsys):
    s = _resume_setup(tmp_path)
    calls: list = []

    def child(cmd, cwd, env):
        calls.append((cmd, env))
        print("SBMIG-REMEDIATION run=x status=SUCCEEDED reason=-")
        return 0
    assert worker.main(environ=s["env"], run_child=child, install_signals=_Sigs()) == 0
    [(cmd, env)] = calls
    assert cmd[:4] == ["py", "-m", "sbmig.cli", "remediation-resume"] and "run" not in cmd[3:4]
    assert cmd[cmd.index("--run-dir") + 1] == str(s["rd"]) and cmd[-1].endswith("worker-paths.yml")
    assert env["SBMIG_REDACT_OUTPUT"] == "1"
    status = JobStatus.read(s["rd"], RESUME_STATUS_FILE)
    assert status.exit_code == 0 and status.attempt == 2 and status.action == ACTION and status.run_id == s["rid"]
    assert snapshot(s["rd"]) == s["before"]                              # job-status.json of attempt 1 untouched
    printed = capsys.readouterr().out
    assert "SBMIG-RESULT" not in printed                                  # the migration's result line is not re-announced
    events = [json.loads(x) for x in (s["rd"] / "journal.jsonl").read_text().splitlines()]
    started = [e for e in events if e["event"] == "worker_started"][0]
    assert started["action"] == ACTION and started["attempt"] == 2
    assert [e for e in events if e["event"] == "worker_finished"][0]["action"] == ACTION


@pytest.mark.parametrize("breakage", ["no_control", "other_action", "no_chain"])
def test_worker_action_refusals_write_only_the_resume_status(tmp_path, breakage):
    s = _resume_setup(tmp_path)
    if breakage == "no_control":
        (s["rd"] / "control" / RESUME_STATE_FILE).unlink()
    elif breakage == "other_action":
        cs = ControlState.read(s["rd"], RESUME_STATE_FILE)
        cs.action = None
        cs.write(s["rd"], RESUME_STATE_FILE)
    else:
        shutil.rmtree(s["rd"] / "remediation")
    calls: list = []
    assert worker.main(environ=s["env"], run_child=lambda *a: calls.append(a) or 0, install_signals=_Sigs()) == 3
    assert not calls
    status = JobStatus.read(s["rd"], RESUME_STATUS_FILE)
    assert status.exit_code == 3 and status.error and status.action == ACTION
    assert snapshot(s["rd"]) == s["before"]


def test_worker_unknown_action_writes_no_status_file(tmp_path):
    s = _resume_setup(tmp_path)
    env = {**s["env"], contract.ACTION_ENV: "format-disk"}
    assert worker.main(environ=env, run_child=lambda *a: 0, install_signals=_Sigs()) == 3
    assert not (s["rd"] / RESUME_STATUS_FILE).exists() and snapshot(s["rd"]) == s["before"]


def test_worker_action_env_pins_refuse_into_the_resume_status(tmp_path):
    s = _resume_setup(tmp_path, pins={"SBMIG_BUILD__MVN": "0000000000000000"}, policy="refuse")
    assert worker.main(environ=s["env"], run_child=lambda *a: 0, install_signals=_Sigs()) == 3
    status = JobStatus.read(s["rd"], RESUME_STATUS_FILE)
    assert "SBMIG_BUILD__MVN" in status.error and status.attempt == 2
    assert snapshot(s["rd"]) == s["before"]


def test_worker_action_sigterm_and_slot_repository(tmp_path):
    s = _resume_setup(tmp_path)
    sigs = _Sigs()
    seen: dict = {}

    def child(cmd, cwd, env):
        seen.update(env)
        sigs.handler(signal.SIGTERM, None)
        raise AssertionError("not reached")
    env = {**s["env"], contract.SLOT_ENV: "2", worker.LOCAL_REPO_ENV: str(tmp_path / "m2" / "repository")}
    assert worker.main(environ=env, run_child=child, install_signals=sigs) == 3
    assert seen[worker.LOCAL_REPO_ENV].endswith("repository-slot2")
    status = JobStatus.read(s["rd"], RESUME_STATUS_FILE)
    assert status.exit_code == 3 and "SIGTERM" in status.error and "remediation resume" in status.error
    assert "not cancelled" in status.error and snapshot(s["rd"]) == s["before"]


# ---- the CLI seam the Worker calls
def test_cli_remediation_resume_calls_chain_resume_and_prints_its_line(tmp_path, monkeypatch, capsys):
    import typer

    from sbmig import cli
    from sbmig.remediations import chain
    from sbmig.remediations.models import RemediationState
    rd = tmp_path / "out" / "web-20261111-030000-cccccc"
    rd.mkdir(parents=True)
    calls: list = []

    def fake_resume(run_dir, cfg, *, journal, stop, components=None):
        calls.append(Path(run_dir))
        journal({"event": "remediation_resume", "scope": "remediation"})
        assert stop() is False
        return RemediationState(id=rd.name, name="source.zip", created=0.0, status="AWAITING_APPROVAL",
                                reason="pending_approval")
    monkeypatch.setattr(chain, "resume", fake_resume)
    cfgs = [PACKAGE_ROOT / "config" / CFG]
    try:
        # the command function itself (the Typer callback would reconfigure the process's logging for later tests)
        with pytest.raises(typer.Exit) as ei:
            cli.remediation_resume(run_dir=rd, config=cfgs, repo_root=None)
        assert ei.value.exit_code == 0
        printed = capsys.readouterr().out
        assert "SBMIG-REMEDIATION run=web-20261111-030000-cccccc status=AWAITING_APPROVAL reason=pending_approval" in printed
        assert calls == [rd.resolve()]
        ev = json.loads((rd / "journal.jsonl").read_text().splitlines()[0])
        assert ev["event"] == "remediation_resume" and ev["run_id"] == rd.name

        def refuse(*a, **k):
            raise StateConflict("terminal")
        monkeypatch.setattr(chain, "resume", refuse)
        with pytest.raises(typer.Exit) as ei:
            cli.remediation_resume(run_dir=rd, config=cfgs, repo_root=None)
        assert ei.value.exit_code == 3 and "status=REFUSED" in capsys.readouterr().out
    finally:
        cli.detach_file_log(rd.resolve() / "sbmig.log")
