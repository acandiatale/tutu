"""Review 2026-11-18, W2 + R1 (Main and Worker must run the same code and configuration; an image update must not strand
runs) and W6 (a remediation-resume status file without an attempt number is not the result of a later attempt).

* `sbmig_version()` = package version + a digest of the running tree: equal for the same tree, different for another;
* the config digests and the version a Worker checks are the ones the Main that CREATED the Job recorded with `creating`
  (the zip's sha256 stays the intake pin); a state file without them falls back to `request.json`;
* a mismatch says what to do in this deployment's words (`IMAGE_DIFFERS_HINT`), and the page gets a Korean sentence;
* a resume removes the previous resume status file before it is queued, and every Worker status carries the attempt the
  Worker was started for (`SBMIG_JOB_ATTEMPT`)."""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

from sbmig.web import contract, worker
from sbmig.web.contract import RESUME_STATE_FILE, RESUME_STATUS_FILE, ControlState, JobStatus, RequestRecord
from sbmig.web.runs import RunManager, resume_job_name

from .test_jobmode_hardening import TEMPLATE, ZIP, Cluster
from .test_remediation_resume import (
    ACTION,
    CFG,
    ResumeCluster,
    _env,
    _resume_setup,
    _Sigs,
    finished_run,
    resume_state,
)

STALE_VERSION = "0.1.0+000000000000"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    from sbmig.core.config import PACKAGE_ROOT
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


# ---- 1. a real build identity
def _tree(root: Path, body: str = "x = 1\n") -> Path:
    (root / "sbmig" / "core").mkdir(parents=True)
    (root / "sbmig" / "__init__.py").write_text("")
    (root / "sbmig" / "core" / "a.py").write_text(body)
    (root / "config").mkdir()
    (root / "config" / "default.yml").write_text("a: 1\n")
    (root / "knowledge" / "notes").mkdir(parents=True)
    (root / "knowledge" / "notes" / "n.md").write_text("note\n")
    return root


def test_the_version_carries_a_digest_of_the_running_tree():
    v = contract.sbmig_version()
    from sbmig import __version__
    base, _, digest = v.partition("+")
    assert base == __version__ and len(digest) == contract.BUILD_DIGEST_LEN and int(digest, 16) >= 0
    assert contract.sbmig_version() == v                                 # stable within the process


def test_the_digest_is_deterministic_and_tells_two_builds_apart(tmp_path):
    a = contract.build_digest(_tree(tmp_path / "a"))
    b = contract.build_digest(_tree(tmp_path / "b"))
    assert a == b                                                        # same files, other place: same build
    c = contract.build_digest(_tree(tmp_path / "c", body="x = 2\n"))
    assert c != a
    # a shipped yml or a knowledge file counts too
    d = _tree(tmp_path / "d")
    (d / "knowledge" / "notes" / "n.md").write_text("changed\n")
    assert contract.build_digest(d) != a
    e = _tree(tmp_path / "e")
    (e / "config" / "artifacts.yml").write_text("company: 1\n")
    assert contract.build_digest(e) != a


def test_the_digest_ignores_env_out_caches_and_other_files(tmp_path):
    a = contract.build_digest(_tree(tmp_path / "a"))
    f = _tree(tmp_path / "f")
    (f / ".env").write_text("LLM_API_KEY=x\n")
    (f / "config" / ".env").write_text("x\n")
    (f / "sbmig" / "__pycache__").mkdir()
    (f / "sbmig" / "__pycache__" / "a.cpython-311.pyc").write_bytes(b"\0")
    (f / "sbmig" / "core" / "notes.txt").write_text("not python\n")
    (f / "config" / "settings.nexus.example.xml").write_text("<x/>")
    (f / "out" / "run").mkdir(parents=True)
    (f / "out" / "run" / "sbmig.log").write_text("log\n")
    (f / "knowledge" / "out").mkdir()
    (f / "knowledge" / "out" / "x.md").write_text("x\n")
    assert contract.build_digest(f) == a


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs a file the user cannot read")
def test_an_unreadable_file_does_not_break_the_digest(tmp_path):
    g = _tree(tmp_path / "g")
    p = g / "sbmig" / "core" / "a.py"
    p.chmod(0)
    try:
        assert len(contract.build_digest(g)) == contract.BUILD_DIGEST_LEN
    finally:
        p.chmod(0o644)


# ---- 2. the pins of the Main that creates the Job
def _stale_request(rd: Path, rid: str, repo: Path) -> None:
    """request.json as an older image's Main wrote it at intake: other default.yml digest, other version."""
    rec = RequestRecord.read(rd)
    rec.config_digests = {**contract.config_digests(repo, [CFG]), "default.yml": "0" * 64}
    rec.sbmig_version = STALE_VERSION
    rec.write(rd)


def test_main_records_config_digests_and_version_with_creating(tmp_path, repo, managers):
    out = tmp_path / "out"
    cluster = Cluster(out)
    mgr = RunManager(out, repo, mode="k8s-job", job_client=cluster, job_template=TEMPLATE)
    managers.append(mgr)
    zp = tmp_path / "p.zip"
    zp.write_bytes(ZIP)
    rid = mgr.start_file(zp, "p.zip", [CFG], None, None)
    assert mgr.runs[rid].done.wait(10)
    cs = ControlState.read(out / rid)
    assert cs.config_digests == contract.config_digests(repo, [CFG])
    assert cs.sbmig_version == contract.sbmig_version()
    # the Job env names the attempt (W6)
    assert _env(cluster.created[0])[contract.ATTEMPT_ENV] == "1"


def test_a_resume_of_a_run_an_older_image_made_is_accepted_when_both_pods_agree_now(tmp_path, repo, managers):
    out = tmp_path / "out"
    rid = "old-20261111-010000-aaaaaa"
    finished_run(out, rid, repo)
    _stale_request(out / rid, rid, repo)
    cluster = ResumeCluster(out, hang=True)
    mgr = RunManager(out, repo, mode="k8s-job", job_client=cluster, job_template=TEMPLATE)
    managers.append(mgr)
    mgr.resume_remediation(rid)
    assert _wait(lambda: len(cluster.created) == 1)
    cs = resume_state(out, rid)
    assert cs.config_digests == contract.config_digests(repo, [CFG]) and cs.sbmig_version == contract.sbmig_version()
    # the resume Worker (same image as this Main) runs it
    env = {"SBMIG_SOURCE_ZIP": str(out / rid / "source.zip"), "SBMIG_OUT": str(out), "SBMIG_RUN_ID": rid,
           "SBMIG_APP_DIR": str(repo), "SBMIG_PYTHON": "py", **{k: v for k, v in _env(cluster.created[0]).items()
                                                               if k in (contract.ACTION_ENV, contract.ATTEMPT_ENV)}}
    calls: list = []
    cs.env_pins = None                                    # this test is about the config / version pins only
    cs.write(out / rid, RESUME_STATE_FILE)
    assert worker.main(environ=env, run_child=lambda *a: calls.append(a) or 0, install_signals=_Sigs()) == 0
    assert calls
    cluster.finish(resume_job_name(rid, 2))
    assert mgr.resumes[rid].done.wait(10)


def _migration_worker(tmp_path: Path, repo: Path, *, state: ControlState | None) -> tuple[dict, Path]:
    out = tmp_path / "out"
    rid = "p-20261118-101010-abcdef"
    rd = out / rid
    rd.mkdir(parents=True)
    (rd / "source.zip").write_bytes(ZIP)
    RequestRecord(run_id=rid, accepted_at="2026-11-18T01:00:00Z", upload="p.zip", configs=[CFG],
                  zip_sha256=contract.sha256_file(rd / "source.zip"),
                  config_digests={**contract.config_digests(repo, [CFG]), "default.yml": "0" * 64},
                  sbmig_version=STALE_VERSION, mode="k8s-job").write(rd)
    if state is not None:
        state.write(rd)
    env = {"SBMIG_SOURCE_ZIP": str(rd / "source.zip"), "SBMIG_OUT": str(out), "SBMIG_RUN_ID": rid,
           "SBMIG_APP_DIR": str(repo), "SBMIG_PYTHON": "py", "SBMIG_CONFIG": str(repo / "config" / CFG)}
    return env, rd


def test_a_run_that_waited_across_a_web_pod_restart_runs_on_the_creating_mains_pins(tmp_path, repo):
    state = ControlState(run_id="p-20261118-101010-abcdef", state="creating", job="sbmig-p",
                         config_digests=contract.config_digests(repo, [CFG]), sbmig_version=contract.sbmig_version())
    env, rd = _migration_worker(tmp_path, repo, state=state)
    calls: list = []
    assert worker.main(environ=env, run_child=lambda *a: calls.append(a) or 0, install_signals=_Sigs()) == 0
    assert calls


def test_the_zip_stays_pinned_from_intake(tmp_path, repo):
    state = ControlState(run_id="p-20261118-101010-abcdef", state="creating", job="sbmig-p",
                         config_digests=contract.config_digests(repo, [CFG]), sbmig_version=contract.sbmig_version())
    env, rd = _migration_worker(tmp_path, repo, state=state)
    (rd / "source.zip").write_bytes(b"PK other bytes")
    assert worker.main(environ=env, run_child=lambda *a: 0, install_signals=_Sigs()) == 3
    assert "source zip changed since intake" in JobStatus.read(rd).error


def test_an_old_state_file_falls_back_to_the_request_pins_and_says_what_to_do(tmp_path, repo):
    state = ControlState(run_id="p-20261118-101010-abcdef", state="creating", job="sbmig-p")   # no creation-time pins
    env, rd = _migration_worker(tmp_path, repo, state=state)
    calls: list = []
    assert worker.main(environ=env, run_child=lambda *a: calls.append(a) or 0, install_signals=_Sigs()) == 3
    assert not calls
    error = JobStatus.read(rd).error
    assert "config default.yml differs between the web pod and the build pod" in error
    assert f"sbmig version differs: web pod {STALE_VERSION}, build pod {contract.sbmig_version()}" in error
    assert error.endswith(contract.IMAGE_DIFFERS_HINT) and error.count(contract.IMAGE_DIFFERS_HINT) == 1
    assert "ConfigMap" not in error


def test_the_page_gets_the_korean_sentence_for_a_refused_job(tmp_path, repo, managers):
    out = tmp_path / "out"
    rid = "p-20261118-101010-abcdef"
    rd = out / rid
    rd.mkdir(parents=True)
    RequestRecord(run_id=rid, accepted_at="2026-11-18T01:00:00Z", upload="p.zip", configs=[CFG], zip_sha256="0" * 64,
                  config_digests={}, sbmig_version=STALE_VERSION, mode="k8s-job").write(rd)
    error = f"sbmig version differs: web pod {STALE_VERSION}, build pod x; {contract.IMAGE_DIFFERS_HINT}"
    ControlState(run_id=rid, state="finished", exit_code=3, error=error).write(rd)
    mgr = RunManager(out, repo, mode="k8s-job", job_client=Cluster(out), job_template=TEMPLATE)
    managers.append(mgr)
    st = mgr.status(rid)
    assert st["error"] == error and st["abort_reason_ko"] == contract.IMAGE_DIFFERS_KO


# ---- W6: a resume status file without an attempt
def test_a_resume_removes_the_previous_resume_status_before_it_is_queued(tmp_path, repo, managers):
    out = tmp_path / "out"
    rid = "web-20261111-010000-dddddd"
    finished_run(out, rid, repo)
    rd = out / rid
    # attempt 2 ended long ago; an older Worker refused it without writing an attempt number
    ControlState(run_id=rid, state="finished", attempt=2, job=resume_job_name(rid, 2), action=ACTION,
                 exit_code=3).write(rd, RESUME_STATE_FILE)
    JobStatus(exit_code=3, run_id=rid, attempt=None, error="refused", action=ACTION).write(rd, RESUME_STATUS_FILE)
    cluster = ResumeCluster(out, phases=["Pending", "Running"], hang=True)       # attempt 3's pod keeps running
    mgr = RunManager(out, repo, mode="k8s-job", job_client=cluster, job_template=TEMPLATE)
    managers.append(mgr)
    info = mgr.resume_remediation(rid)
    assert info["attempt"] == 3
    assert not (rd / RESUME_STATUS_FILE).exists()
    assert _wait(lambda: len(cluster.created) == 1)
    assert _env(cluster.created[0])[contract.ATTEMPT_ENV] == "3"
    time.sleep(0.5)
    assert not mgr.resumes[rid].done.is_set()            # the running attempt 3 is not taken for finished (nor deleted)
    assert not cluster.deleted and not mgr.resumes[rid].verified
    cluster.finish(resume_job_name(rid, 3))
    assert mgr.resumes[rid].done.wait(10)
    assert mgr.status(rid)["remediation_job"]["exit_code"] == 0


def test_a_worker_refusing_before_it_knows_its_state_writes_the_attempt_it_was_started_for(tmp_path):
    s = _resume_setup(tmp_path, attempt=4)
    (s["rd"] / "control" / RESUME_STATE_FILE).unlink()
    env = {**s["env"], contract.ATTEMPT_ENV: "4"}
    assert worker.main(environ=env, run_child=lambda *a: 0, install_signals=_Sigs()) == 3
    status = JobStatus.read(s["rd"], RESUME_STATUS_FILE)
    assert status.attempt == 4 and status.matches(s["rid"], 4) and not status.matches(s["rid"], 5)


def test_a_migration_worker_without_request_json_writes_the_attempt_too(tmp_path):
    out = tmp_path / "out"
    rid = "p-20261118-101010-abcdef"
    (out / rid).mkdir(parents=True)
    env = {"SBMIG_SOURCE_ZIP": str(out / rid / "source.zip"), "SBMIG_OUT": str(out), "SBMIG_RUN_ID": rid,
           contract.ATTEMPT_ENV: "1"}
    assert worker.main(environ=env, run_child=lambda *a: 0, install_signals=_Sigs()) == 3
    assert JobStatus.read(out / rid).attempt == 1
