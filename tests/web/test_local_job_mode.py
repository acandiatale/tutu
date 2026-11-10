"""2026-11-05 (the user: "로컬 개발에서는 파드 생성이 들어가면 안 돌거니까 … 개발 모드일 때는 파드 생성 없이"): `SBMIG_WEB_MODE=local-job`
runs the Job-mode flow with a local subprocess as the pod — and `k8s-job` outside a cluster falls back to it with one warning.
The subprocess here is a tiny script that writes what `sbmig run` writes (journal, log, report) and honours stop.request."""
from __future__ import annotations

import json
import logging
import shutil
import stat
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sbmig.core.config import PACKAGE_ROOT
from sbmig.web import create_app
from sbmig.web.localjob import LocalJobClient
from sbmig.web.runs import RunManager

from .test_app import _manager, _sse, _start

TEMPLATE = Path(__file__).resolve().parents[2] / "deploy" / "k8s" / "81-web-job.template.yaml"
FAKE_RUN = r'''
import json, os, sys, time, datetime
args = sys.argv[1:]
out = args[args.index("--out") + 1]; rid = args[args.index("--run-id") + 1]
rd = os.path.join(out, rid); os.makedirs(rd, exist_ok=True)
ts = lambda: datetime.datetime.now(datetime.timezone.utc).isoformat()
def j(ev):
    with open(os.path.join(rd, "journal.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": ts(), **ev}) + "\n")
with open(os.path.join(rd, "sbmig.log"), "a", encoding="utf-8") as fh:
    fh.write("2026-11-05 22:00:00,000 INFO [intake] fake run " + " ".join(args) + "\n")
for node, phase in (("intake", "intake"), ("analyze", "analyze"), ("plan", "plan")):
    j({"event": "graph_node_end", "node": node, "phase": phase, "phases": ["verify"] if node == "plan" else None})
    if os.path.isfile(os.path.join(rd, "stop.request")):
        j({"event": "stop_request", "node": node}); j({"event": "graph_node_end", "node": "finalize", "phase": None, "aborted": "stopped by user"})
        open(os.path.join(rd, "report.md"), "w").write("# aborted\n"); sys.exit(3)
    time.sleep(float(os.environ.get("FAKE_STEP_S", "0.05")))
if os.environ.get("FAKE_WAIT_FOR_STOP") == "1":
    for _ in range(200):
        if os.path.isfile(os.path.join(rd, "stop.request")):
            j({"event": "stop_request", "node": "phase"}); open(os.path.join(rd, "report.md"), "w").write("# aborted\n"); sys.exit(3)
        time.sleep(0.05)
j({"event": "graph_node_end", "node": "phase", "phase": "verify", "phases": []})
j({"event": "graph_node_end", "node": "finalize", "phase": None})
open(os.path.join(rd, "report.md"), "w").write("# report\n\nBUILD SUCCESS\n")
open(os.path.join(rd, "report.json"), "w").write(json.dumps({"success": True, "blocked": [], "unresolved": [], "llm_usage": {"calls": 1}}))
with open(os.path.join(rd, "sbmig.log"), "a", encoding="utf-8") as fh:
    fh.write("SBMIG-RESULT ok fake\n")
sys.exit(int(os.environ.get("FAKE_RC", "0")))
'''


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    shutil.copy(PACKAGE_ROOT / "config" / "artifacts.local-e2e.yml", repo / "config" / "artifacts.local-e2e.yml")
    (repo / "config" / "default.yml").write_text("x: 1\n")
    return repo


@pytest.fixture
def fake_cmd(tmp_path: Path):
    script = tmp_path / "fake_run.py"
    script.write_text(FAKE_RUN, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return lambda env: [sys.executable, str(script), env["SBMIG_SOURCE_ZIP"], "--out", env["SBMIG_OUT"], "--run-id", env["SBMIG_RUN_ID"]]


def _wait(mgr: RunManager, rid: str, state: str, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while mgr.status(rid)["state"] != state and time.time() < deadline:
        time.sleep(0.02)


def test_local_job_mode_runs_the_pod_as_a_subprocess_and_the_ui_sees_the_same_events(tmp_path: Path, repo: Path, fake_cmd, monkeypatch):
    monkeypatch.setenv("SBMIG_JOB_POLL_S", "0.05")
    jc = LocalJobClient(repo, command_factory=fake_cmd)
    client = TestClient(create_app(tmp_path / "out", repo, mode="local-job", job_client=jc))
    rid = _start(client, "name=my%20proj.zip&configs=artifacts.local-e2e.yml")
    events = _sse(client, rid)
    nodes = [d for k, d in events if k == "node"]
    job_states = [d["update"]["state"] for d in nodes if d["node"] == "job"]
    # the pod step closes on the verified job-status.json (2026-11-10): `completed` after the phase, once
    assert job_states[0] == "created" and "running" in job_states and job_states[-2:] == ["succeeded", "completed"]
    closing = [d["update"] for d in nodes if d["node"] == "job" and d["update"]["state"] == "completed"]
    assert len(closing) == 1 and closing[0]["exit_code"] == 0 and closing[0]["phase"] == "Succeeded" and "failed" not in job_states
    assert [d["node"] for d in nodes if d["node"] != "job"] == ["intake", "analyze", "plan", "phase", "finalize"]
    st = [d for k, d in events if k == "status"][-1]
    assert st["state"] == "finished" and st["exit_code"] == 0 and st["mode"] == "local-job" and st["job"]["pod"].startswith("local-")
    run_dir = tmp_path / "out" / rid
    assert (run_dir / "source.zip").is_file() and (run_dir / "job-status.json").is_file() and (run_dir / "job-local.log").is_file()
    assert json.loads((run_dir / "job-status.json").read_text())["exit_code"] == 0
    assert client.get(f"/api/runs/{rid}/files/report.md").text.startswith("# report")
    run = _manager(client).runs[rid]
    assert ("finished", {"exit_code": 0, "success": True}) in run.history


def test_child_output_is_echoed_to_the_console_and_kept_in_the_file(tmp_path: Path, repo: Path, monkeypatch, capfd):
    """The user (2026-11-05): a subprocess shows nothing on the console — so the local pod's output is echoed with a
    `[job <run id>]` prefix (SBMIG_LOCAL_JOB_ECHO=0 turns it off) while job-local.log keeps every line."""
    script = tmp_path / "say.py"
    script.write_text("import sys\nprint('hello from the pod'); print('second line', file=sys.stderr)\n", encoding="utf-8")
    cmd = lambda env: [sys.executable, str(script)]
    for echo, expect in ((True, True), (False, False)):
        jc = LocalJobClient(repo, command_factory=cmd, echo=echo)
        run_dir = tmp_path / "out" / f"r-{int(echo)}"
        name = jc.create_job({"metadata": {"name": f"sbmig-r-{int(echo)}"}, "spec": {"template": {"spec": {"containers": [{"name": "b", "env": [
            {"name": "SBMIG_SOURCE_ZIP", "value": "x.zip"}, {"name": "SBMIG_OUT", "value": str(tmp_path / "out")},
            {"name": "SBMIG_RUN_ID", "value": f"r-{int(echo)}"}]}]}}}})
        deadline = time.time() + 10
        while not jc.job_state(name).finished and time.time() < deadline:
            time.sleep(0.02)
        time.sleep(0.1)                                      # the pump thread drains the pipe
        text = (run_dir / "job-local.log").read_text(encoding="utf-8")
        assert "hello from the pod" in text and "second line" in text
        out = capfd.readouterr().out
        assert (f"[job r-{int(echo)}] hello from the pod" in out) is expect
    # a Korean Windows console (cp949) and a child that writes an em dash and Korean: UTF-8 in the file, replacements on
    # the console, no exception — and the child is told to write UTF-8 at a wide width
    import io
    script.write_text("import os, sys\nprint('— 접수 ✓ ' + os.environ.get('PYTHONIOENCODING', '?') + ' cols=' + os.environ.get('COLUMNS', '?'))\n",
                      encoding="utf-8")
    jc = LocalJobClient(repo, command_factory=cmd, echo=True)
    buf = io.BytesIO()
    cp949 = io.TextIOWrapper(buf, encoding="cp949", errors="strict")
    monkeypatch.setattr(sys, "stdout", cp949)
    name = jc.create_job({"metadata": {"name": "sbmig-r-ko"}, "spec": {"template": {"spec": {"containers": [{"name": "b", "env": [
        {"name": "SBMIG_SOURCE_ZIP", "value": "x.zip"}, {"name": "SBMIG_OUT", "value": str(tmp_path / "out")},
        {"name": "SBMIG_RUN_ID", "value": "r-ko"}]}]}}}})
    deadline = time.time() + 10
    while not jc.job_state(name).finished and time.time() < deadline:
        time.sleep(0.02)
    time.sleep(0.1)
    cp949.flush()
    text = (tmp_path / "out" / "r-ko" / "job-local.log").read_text(encoding="utf-8")
    assert "— 접수 ✓ utf-8 cols=160" in text                                  # the file keeps the child's UTF-8 as is
    shown = buf.getvalue().decode("cp949", errors="replace")
    assert "[job r-ko]" in shown and "접수" in shown and "utf-8" in shown    # the console got the line, unencodable chars replaced
    monkeypatch.setenv("SBMIG_LOCAL_JOB_ECHO", "0")
    assert LocalJobClient(repo).echo is False
    monkeypatch.delenv("SBMIG_LOCAL_JOB_ECHO")
    assert LocalJobClient(repo).echo is True


def test_the_real_command_is_the_worker_the_pod_runs(repo: Path, tmp_path: Path, monkeypatch):
    """v2.1z-ac: the local "pod" runs the same Worker as the Job pod (`python -m sbmig.web.worker`); the run's values travel as
    SBMIG_* env (plus SBMIG_APP_DIR = the repo the Main pinned the config digests from), not as arguments."""
    jc = LocalJobClient(repo)
    cmd = jc._command({"SBMIG_SOURCE_ZIP": "/d/out/r1/source.zip", "SBMIG_OUT": "/d/out", "SBMIG_RUN_ID": "r1",
                       "SBMIG_CONFIG": "/app/config/a.yml /app/config/b.yml", "SBMIG_STOP_AFTER": "plan", "SBMIG_LLM_PROVIDER": "fake"})
    assert cmd == [sys.executable, "-m", "sbmig.web.worker"]
    seen: dict = {}

    class _P:
        pid, stdout = 4242, None

        def poll(self):
            return 0
    def popen(cmd, **kw):
        seen.update(kw["env"])
        return _P()
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(LocalJobClient, "_pump", lambda self, proc, log_path, run_id: None)
    name = jc.create_job({"metadata": {"name": "sbmig-r1"}, "spec": {"template": {"spec": {"containers": [{"name": "b", "env": [
        {"name": "SBMIG_SOURCE_ZIP", "value": "x.zip"}, {"name": "SBMIG_OUT", "value": str(tmp_path / "out")},
        {"name": "SBMIG_RUN_ID", "value": "r1"}, {"name": "SBMIG_STOP_AFTER", "value": "plan"}]}]}}}})
    assert seen["SBMIG_RUN_ID"] == "r1" and seen["SBMIG_STOP_AFTER"] == "plan" and seen["SBMIG_APP_DIR"] == str(repo)
    # the Worker left no job-status.json: the client writes the fallback (with the run id); one the Worker wrote is kept
    st = jc.job_state(name)
    status = tmp_path / "out" / "r1" / "job-status.json"
    assert st.phase == "Succeeded" and json.loads(status.read_text())["run_id"] == "r1"
    status.write_text(json.dumps({"exit_code": 0, "run_id": "r1", "attempt": 1, "sbmig_version": "w"}))
    jc.job_state(name)
    assert json.loads(status.read_text())["sbmig_version"] == "w"


def test_stop_request_reaches_the_local_pod(tmp_path: Path, repo: Path, fake_cmd, monkeypatch):
    monkeypatch.setenv("SBMIG_JOB_POLL_S", "0.05")
    monkeypatch.setenv("FAKE_WAIT_FOR_STOP", "1")
    mgr = RunManager(tmp_path / "out", repo, mode="local-job", job_client=LocalJobClient(repo, command_factory=fake_cmd),
                     job_template=TEMPLATE)
    rid = mgr.start(b"PK\x05\x06" + b"\0" * 18, "p.zip", ["artifacts.local-e2e.yml"])
    _wait(mgr, rid, "running")
    time.sleep(0.3)                                          # let the fake reach its waiting loop
    assert mgr.stop(rid) and mgr.runs[rid].done.wait(10)
    assert mgr.runs[rid].exit_code == 3 and (tmp_path / "out" / rid / "stop.request").is_file()
    assert (tmp_path / "out" / rid / "report.md").read_text().startswith("# aborted")


def test_k8s_job_outside_a_cluster_falls_back_to_local_job_with_a_warning(tmp_path: Path, repo: Path, monkeypatch, caplog):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    with caplog.at_level(logging.WARNING, logger="sbmig.web"):
        mgr = RunManager(tmp_path / "out", repo, mode="k8s-job", job_template=TEMPLATE)
    assert mgr.mode == "k8s-job" and mgr.effective_mode == "local-job"
    assert any("not inside a Kubernetes cluster" in r.getMessage() and "LOCAL subprocesses" in r.getMessage() for r in caplog.records)
    assert isinstance(mgr._job_client_or_fail(), LocalJobClient)
    # with an explicit client (the cluster, or a test fake) nothing degrades
    class _C:
        namespace = "ns"
    mgr2 = RunManager(tmp_path / "out2", repo, mode="k8s-job", job_client=_C(), job_template=TEMPLATE)
    assert mgr2.effective_mode == "k8s-job"


def test_the_pod_settings_of_the_job_template_never_reach_the_local_worker(tmp_path: Path, repo: Path, monkeypatch):
    """2026-11-10 (regression found while reviewing the Job code): the template describes the POD. Its literals are container
    paths (`SBMIG_BUILD__SETTINGS_FILE=/etc/sbmig/settings.xml`, `SBMIG_BUILD__JAVA_HOME=/opt/java21`, `SBMIG_KB_DIR=/data/kb-v2`)
    and its ConfigMap / Secret references have no value outside a cluster — handed to the local Worker they became `""`, and an
    empty `SBMIG_<SECTION>__<KEY>` overrides the config value with None (`load_config` dies on a pydantic error). Only the
    per-run values (`contract.RUN_ENV`) cross; the local Worker's settings are the developer's shell / `.env`."""
    import yaml

    from sbmig.web.contract import RUN_ENV
    template_env = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))["spec"]["template"]["spec"]["containers"][0]["env"]
    names = [e["name"] for e in template_env]
    assert "SBMIG_BUILD__SETTINGS_FILE" in names and "SBMIG_KB_DIR" in names and any("valueFrom" in e for e in template_env)
    for name in names:                                       # hermetic: a developer shell may export some of them
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SBMIG_APP_DIR", "/somewhere/else")   # nor does a stray shell value move the digest folder
    dump = tmp_path / "child-env.json"
    script = tmp_path / "dump_env.py"
    script.write_text("import json, os, sys\njson.dump(dict(os.environ), open(sys.argv[1], 'w'))\n", encoding="utf-8")
    monkeypatch.setenv("SBMIG_JOB_POLL_S", "0.05")
    jc = LocalJobClient(repo, command_factory=lambda env: [sys.executable, str(script), str(dump)], echo=False)
    mgr = RunManager(tmp_path / "out", repo, mode="local-job", job_client=jc, job_template=TEMPLATE)
    rid = mgr.start(b"PK\x05\x06" + b"\0" * 18, "p.zip", ["artifacts.local-e2e.yml"], stop_after="plan")
    assert mgr.runs[rid].done.wait(10)
    child = json.loads(dump.read_text(encoding="utf-8"))
    leaked = sorted(n for n in names if n in child and n not in RUN_ENV)
    assert leaked == [], f"pod settings of the template reached the local Worker: {leaked}"
    assert not [k for k, v in child.items() if k.startswith("SBMIG_") and "__" in k and v == ""]
    assert child["SBMIG_RUN_ID"] == rid and child["SBMIG_OUT"] == str(tmp_path / "out") and child["SBMIG_STOP_AFTER"] == "plan"
    assert child["SBMIG_CONFIG"] == str(repo / "config" / "artifacts.local-e2e.yml")
    assert child["SBMIG_SOURCE_ZIP"].endswith("source.zip") and child["SBMIG_APP_DIR"] == str(repo)
