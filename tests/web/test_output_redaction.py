"""2026-11-10 (review: "the child's output goes to the pod log as it is and the files are redacted after the run — a
secret printed during the run is exposed before the redaction"): what the web serves while a run is going.

- `sbmig.log` / `journal.jsonl` are redacted as they are written (the hook of `sbmig.core.scrub`), so they stay live;
- every other result file is redacted by the pass at the end of the run and is neither listed nor served before it
  (HTTP 409);
- the pass runs in Main for an in-process run (when output redaction is on) and for a Job that ended without telling
  (the pod died before its own pass) — never for a Job that may still be writing."""
from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from sbmig.core import scrub
from sbmig.web import contract, create_app
from sbmig.web.redact import install_output_redaction
from sbmig.web.runs import LIVE_FILES, RunManager

from .test_app import _manager, _start, _zip_bytes
from .test_job_mode import TEMPLATE, FakeJobClient, FakePod, repo  # noqa: F401 — `repo` is a fixture

SECRET = "tok-1a2b3c4d5e6f"


class HeldJobClient(FakeJobClient):
    """Running until `release` is set; then the pod writes its results (`pod`) or the Job just fails (`pod=None`)."""

    def __init__(self, out_dir: Path, pod: FakePod | None) -> None:
        super().__init__(phases=["Pending", "Running"], pod=None, out_dir=out_dir)
        self.release = threading.Event()
        self._end, self._ended = pod, False

    def job_state(self, name: str):
        if self.release.is_set() and not self._ended:
            self._ended = True
            if self._end is not None:
                self.pod = self._end
            else:
                self.phases = ["Failed"]
        return super().job_state(name)


@pytest.fixture
def job_env(monkeypatch):
    monkeypatch.setenv("SBMIG_JOB_POLL_S", "0.05")
    monkeypatch.setenv("SBMIG_JOB_TEMPLATE", str(TEMPLATE))


@pytest.fixture
def scrubbing(monkeypatch):
    """Output redaction as an entry point installs it, with one secret in the environment."""
    old = sys.excepthook, threading.excepthook
    noted = set(scrub._noted)
    monkeypatch.setenv("MY_API_KEY", SECRET)
    install_output_redaction()
    yield
    scrub.set_output_scrubber(None)
    sys.excepthook, threading.excepthook = old
    scrub._noted.clear()
    scrub._noted.update(noted)


def _wait_running(mgr: RunManager, rid: str) -> None:
    deadline = time.time() + 5
    while mgr.status(rid)["state"] != "running" and time.time() < deadline:
        time.sleep(0.02)
    assert mgr.status(rid)["state"] == "running"


def _leaky_files(run_dir: Path) -> Path:
    """What a pod leaves on the volume mid-run: files write-time redaction does not reach."""
    (run_dir / "logs").mkdir(exist_ok=True)
    build = run_dir / "logs" / "build-001.log"
    build.write_text(f"[INFO] mvn -Dnexus.password={SECRET} clean package\n", encoding="utf-8")
    (run_dir / "errors.md").write_text(f"# errors\n\nauth failed for {SECRET}\n", encoding="utf-8")
    (run_dir / "sbmig.log").write_text("INFO [intake] started\n", encoding="utf-8")
    return build


def test_result_files_wait_for_the_end_of_a_job_run(tmp_path, repo, job_env):  # noqa: F811
    jc = HeldJobClient(tmp_path / "out", FakePod())
    client = TestClient(create_app(tmp_path / "out", repo, mode="k8s-job", job_client=jc))
    mgr = _manager(client)
    rid = _start(client)
    _wait_running(mgr, rid)
    _leaky_files(tmp_path / "out" / rid)
    st = client.get(f"/api/runs/{rid}").json()
    assert "sbmig.log" in st["files"] and "journal.jsonl" in st["files"] and "errors.md" not in st["files"]
    r = client.get(f"/api/runs/{rid}/files/errors.md")
    assert r.status_code == 409 and "once the run has finished" in r.json()["error"] and SECRET not in r.text
    assert client.get(f"/api/runs/{rid}/files/sbmig.log").status_code == 200      # redacted as it is written: stays live
    assert client.get(f"/api/runs/{rid}/files/journal.jsonl").status_code == 200
    assert mgr.file_withheld(rid, "errors.md") and mgr.file_withheld(rid, "result.zip")
    assert not any(mgr.file_withheld(rid, n) for n in LIVE_FILES)
    jc.release.set()
    assert mgr.runs[rid].done.wait(10)
    st = client.get(f"/api/runs/{rid}").json()
    assert st["state"] == "finished" and "report.md" in st["files"] and "errors.md" in st["files"]
    assert client.get(f"/api/runs/{rid}/files/report.md").status_code == 200
    assert not mgr.file_withheld(rid, "errors.md")


def test_a_job_that_died_without_telling_gets_mains_pass(tmp_path, repo, job_env, monkeypatch):  # noqa: F811
    """The pod was killed before the Worker's own pass: Main cleans the run dir with the secrets it knows, once the
    Job is confirmed ended and before a file is served."""
    monkeypatch.setenv("MY_API_KEY", SECRET)
    jc = HeldJobClient(tmp_path / "out", None)
    mgr = RunManager(tmp_path / "out", repo, mode="k8s-job", job_client=jc, job_template=TEMPLATE)
    rid = mgr.start(_zip_bytes(), "p.zip", ["artifacts.local-e2e.yml"])
    _wait_running(mgr, rid)
    build = _leaky_files(tmp_path / "out" / rid)
    jc.release.set()
    run = mgr.runs[rid]
    assert run.done.wait(10)
    assert run.verified is False and run.job_phase == "Failed" and run.exit_code == 3
    assert SECRET not in build.read_text(encoding="utf-8") and "-Dnexus.password=***" in build.read_text(encoding="utf-8")
    assert SECRET not in (tmp_path / "out" / rid / "errors.md").read_text(encoding="utf-8")
    assert not mgr.file_withheld(rid, "errors.md") and "errors.md" in mgr.status(rid)["files"]


def test_a_stopped_job_gets_mains_pass(tmp_path, repo, job_env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("MY_API_KEY", SECRET)
    monkeypatch.setenv("SBMIG_JOB_STOP_GRACE_S", "0.2")
    jc = FakeJobClient(phases=["Pending", "Running"], pod=None)        # never ends on its own
    mgr = RunManager(tmp_path / "out", repo, mode="k8s-job", job_client=jc, job_template=TEMPLATE)
    rid = mgr.start(_zip_bytes(), "p.zip", ["artifacts.local-e2e.yml"])
    _wait_running(mgr, rid)
    build = _leaky_files(tmp_path / "out" / rid)
    assert mgr.stop(rid)
    run = mgr.runs[rid]
    assert run.done.wait(10)
    assert jc.deleted and run.job_phase == "Missing"
    assert SECRET not in build.read_text(encoding="utf-8")


def test_a_job_left_running_at_shutdown_is_not_touched(tmp_path, repo, job_env, monkeypatch):  # noqa: F811
    """Its pod may still be writing: the next web process settles it (and the Worker redacts at its end)."""
    monkeypatch.setenv("MY_API_KEY", SECRET)
    jc = FakeJobClient(phases=["Pending", "Running"], pod=None)
    mgr = RunManager(tmp_path / "out", repo, mode="k8s-job", job_client=jc, job_template=TEMPLATE)
    rid = mgr.start(_zip_bytes(), "p.zip", ["artifacts.local-e2e.yml"])
    _wait_running(mgr, rid)
    build = _leaky_files(tmp_path / "out" / rid)
    mgr.shutdown()
    run = mgr.runs[rid]
    assert run.done.wait(10) and run.keep_state and not jc.deleted
    assert SECRET in build.read_text(encoding="utf-8")


def test_a_run_not_in_memory_follows_its_control_state(tmp_path, repo):  # noqa: F811
    mgr = RunManager(tmp_path / "out", repo)
    rd = tmp_path / "out" / "20261110-101010-abcd"
    rd.mkdir(parents=True)
    (rd / "report.md").write_text("# r\n", encoding="utf-8")
    assert not mgr.file_withheld(rd.name, "report.md")              # an old run without a control state: served as before
    state = contract.ControlState(run_id=rd.name, state=contract.RunState.RUNNING.value)
    state.write(rd)
    assert mgr.file_withheld(rd.name, "report.md") and not mgr.file_withheld(rd.name, "sbmig.log")
    assert "report.md" not in mgr.status(rd.name).get("files", [])
    state.transition(contract.RunState.FINISHED, exit_code=0).write(rd)
    assert not mgr.file_withheld(rd.name, "report.md") and "report.md" in mgr.status(rd.name)["files"]


def _body(seen: dict):
    def body(self: RunManager, run) -> SimpleNamespace:
        seen["withheld"] = self.file_withheld(run.run_id, "report.md")
        seen["live"] = self.file_withheld(run.run_id, "sbmig.log")
        seen["listed"] = self.status(run.run_id).get("files", [])
        _leaky_files(run.run_dir).write_text(f"[INFO] -Dkey={SECRET}\n", encoding="utf-8")
        (run.run_dir / "report.md").write_text(f"# report\n\nused {SECRET}\n", encoding="utf-8")
        logging.getLogger("sbmig.fake").info("calling the gateway with %s", SECRET)      # → this run's sbmig.log
        return SimpleNamespace(success=True, blocked=[], flags=[])
    return body


def test_in_process_run_with_redaction_on(tmp_path, repo, monkeypatch, scrubbing):  # noqa: F811
    seen: dict = {}
    monkeypatch.setattr(RunManager, "_run_thread_body", _body(seen))
    client = TestClient(create_app(tmp_path / "out", repo))
    mgr = _manager(client)
    rid = _start(client)
    assert mgr.runs[rid].done.wait(10)
    rd = tmp_path / "out" / rid
    assert seen["withheld"] is True and seen["live"] is False and "report.md" not in seen["listed"]
    log_text = (rd / "sbmig.log").read_text(encoding="utf-8")
    assert "calling the gateway with ***" in log_text and SECRET not in log_text          # at write time
    assert (rd / "logs" / "build-001.log").read_text(encoding="utf-8") == "[INFO] -Dkey=***\n"   # the pass at the end
    r = client.get(f"/api/runs/{rid}/files/report.md")
    assert r.status_code == 200 and SECRET not in r.text and "used ***" in r.text
    for name in ("journal.jsonl", "web-status.json"):
        assert SECRET not in (rd / name).read_text(encoding="utf-8")


def test_in_process_run_without_the_switch_is_unchanged(tmp_path, repo, monkeypatch):  # noqa: F811
    seen: dict = {}
    monkeypatch.setenv("MY_API_KEY", SECRET)
    monkeypatch.setattr(RunManager, "_run_thread_body", _body(seen))
    client = TestClient(create_app(tmp_path / "out", repo))
    rid = _start(client)
    assert _manager(client).runs[rid].done.wait(10)
    assert seen["withheld"] is False and seen["live"] is False
    assert SECRET in (tmp_path / "out" / rid / "logs" / "build-001.log").read_text(encoding="utf-8")
