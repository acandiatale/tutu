from __future__ import annotations

import json
from pathlib import Path

import pytest

from sbmig.web import contract, worker

SECRET = "abcdef123456"


@pytest.fixture()
def setup(tmp_path):
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / "config" / "default.yml").write_text("llm:\n  provider: fake\n", encoding="utf-8")
    (repo / "config" / "artifacts.yml").write_text("artifacts: {}\n", encoding="utf-8")
    out = tmp_path / "out"
    run_id = "20261109-120000-abcd"
    run_dir = out / run_id
    run_dir.mkdir(parents=True)
    zip_path = run_dir / "source.zip"
    zip_path.write_bytes(b"PK\x03\x04 fake zip")
    record = contract.RequestRecord(
        run_id=run_id, accepted_at=contract.utc_now(), upload="app.zip", configs=["artifacts.yml"],
        zip_sha256=contract.sha256_file(zip_path), config_digests=contract.config_digests(repo, ["artifacts.yml"]),
        sbmig_version=contract.sbmig_version(), mode="k8s-job", attempt=2)
    record.write(run_dir)
    environ = {"SBMIG_SOURCE_ZIP": str(zip_path), "SBMIG_OUT": str(out), "SBMIG_RUN_ID": run_id,
               "SBMIG_APP_DIR": str(repo), "SBMIG_PYTHON": "py", "SBMIG_KB_DIR": str(tmp_path / "kb"),
               "LLM_APP_TOKEN": SECRET}
    return {"repo": repo, "out": out, "run_id": run_id, "run_dir": run_dir, "zip": zip_path, "env": environ}


def fake_child(rc: int, calls: list | None = None):
    def run(cmd, cwd, env):
        if calls is not None:
            calls.append((cmd, cwd, env))
        rd = Path(env["SBMIG_OUT"]) / env["SBMIG_RUN_ID"]
        (rd / "report.json").write_text('{"status": "ok"}', encoding="utf-8")
        (rd / "sbmig.log").write_text(
            f"start token={env['LLM_APP_TOKEN']}\nSBMIG-RESULT old\nSBMIG-RESULT status=success rc={rc}\n", encoding="utf-8")
        return rc
    return run


def journal(run_dir: Path) -> list[dict]:
    return [json.loads(ln) for ln in (run_dir / "journal.jsonl").read_text(encoding="utf-8").splitlines()]


def test_happy_path(setup, capsys):
    calls: list = []
    rc = worker.main([], environ=setup["env"], run_child=fake_child(0, calls))
    assert rc == 0
    rd = setup["run_dir"]
    status = contract.JobStatus.read(rd)
    assert status and status.exit_code == 0 and status.run_id == setup["run_id"] and status.attempt == 2
    assert status.sbmig_version == contract.sbmig_version() and status.matches(setup["run_id"], 2)
    log = (rd / "sbmig.log").read_text(encoding="utf-8")
    assert SECRET not in log and "token=***" in log
    paths = (rd / "worker-paths.yml").read_text(encoding="utf-8")
    assert 'record_dir: ""' in paths and str(Path(setup["env"]["SBMIG_KB_DIR"])) in paths
    cmd, cwd, env = calls[0]
    assert cwd == setup["repo"] and env["LLM_APP_TOKEN"] == SECRET
    assert cmd == ["py", "-m", "sbmig.cli", "run", str(setup["zip"]),
                   "--config", str(setup["repo"] / "config" / "artifacts.yml"),
                   "--config", str(rd / "worker-paths.yml"),
                   "--out", str(setup["out"]), "--run-id", setup["run_id"]]
    events = [e["event"] for e in journal(rd)]
    assert events == ["worker_started", "worker_finished"]
    fin = journal(rd)[-1]
    assert fin["exit_code"] == 0 and fin["redacted_files"] >= 1
    assert "SBMIG-RESULT status=success rc=0" in capsys.readouterr().out


def test_options_and_explicit_configs(setup):
    calls: list = []
    env = {**setup["env"], "SBMIG_CONFIG": "artifacts.yml /x/extra.yml", "SBMIG_STOP_AFTER": "restructure",
           "SBMIG_LLM_PROVIDER": "fake"}
    worker.main(environ=env, run_child=fake_child(0, calls))
    cmd = calls[0][0]
    assert cmd[cmd.index("--config") + 1] == str(setup["repo"] / "config" / "artifacts.yml")
    assert "/x/extra.yml" in cmd
    assert cmd[-4:] == ["--stop-after", "restructure", "--llm-provider", "fake"]


def test_llm_records_kept(setup):
    env = {**setup["env"], contract.LLM_RECORDS_ENV: "1"}
    env.pop("SBMIG_KB_DIR")
    assert worker.main(environ=env, run_child=fake_child(0)) == 0
    paths = (setup["run_dir"] / "worker-paths.yml").read_text(encoding="utf-8")
    assert "record_dir" not in paths and "kb:" not in paths


@pytest.mark.parametrize("child_rc,flag,expected", [(2, None, 0), (2, "1", 2), (3, None, 3), (1, "1", 0)])
def test_exit_codes(setup, child_rc, flag, expected):
    env = dict(setup["env"])
    if flag:
        env["SBMIG_FAIL_JOB_ON_BUILD_FAIL"] = flag
    assert worker.main(environ=env, run_child=fake_child(child_rc)) == expected
    assert contract.JobStatus.read(setup["run_dir"]).exit_code == child_rc


def test_missing_request_refused(setup):
    (setup["run_dir"] / "control" / "request.json").unlink()
    calls: list = []
    assert worker.main(environ=setup["env"], run_child=fake_child(0, calls)) == 3
    assert not calls
    status = contract.JobStatus.read(setup["run_dir"])
    assert status.exit_code == 3 and "request.json missing" in status.error and status.run_id == setup["run_id"]
    assert journal(setup["run_dir"])[-1]["event"] == "worker_refused"


def test_changed_zip_refused(setup):
    setup["zip"].write_bytes(b"PK other content")
    calls: list = []
    assert worker.main(environ=setup["env"], run_child=fake_child(0, calls)) == 3
    assert not calls
    status = json.loads((setup["run_dir"] / "job-status.json").read_text(encoding="utf-8"))
    assert status["exit_code"] == 3 and "source zip changed" in status["error"] and status["attempt"] == 2
    last = journal(setup["run_dir"])[-1]
    assert last["event"] == "worker_refused" and "source zip changed" in last["reason"]


def test_changed_config_refused(setup):
    (setup["repo"] / "config" / "artifacts.yml").write_text("changed: true\n", encoding="utf-8")
    assert worker.main(environ=setup["env"], run_child=fake_child(0)) == 3
    assert "artifacts.yml differs" in contract.JobStatus.read(setup["run_dir"]).error


@pytest.mark.parametrize("exc", [OSError("no such interpreter"), KeyboardInterrupt()])
def test_child_exception_still_writes_status(setup, exc):
    def boom(cmd, cwd, env):
        raise exc
    assert worker.main(environ=setup["env"], run_child=boom) == 3
    status = contract.JobStatus.read(setup["run_dir"])
    assert status.exit_code == 3 and status.attempt == 2 and "could not run sbmig" in status.error
    assert [e["event"] for e in journal(setup["run_dir"])] == ["worker_started", "worker_finished"]


def test_required_env_missing(setup, capsys):
    env = dict(setup["env"])
    env.pop("SBMIG_RUN_ID")
    assert worker.main(environ=env, run_child=fake_child(0)) == 3
    assert "SBMIG_RUN_ID" in capsys.readouterr().err
    assert not (setup["run_dir"] / "job-status.json").exists()


def test_default_run_child_streams(tmp_path, capfd):
    import sys
    rc = worker.default_run_child([sys.executable, "-c", "print('hello'); import sys; sys.exit(5)"], tmp_path, {})
    assert rc == 5
    assert "hello" in capfd.readouterr().out
