"""Review 2026-11-10 (finding 3): the Worker compares its own environment with the pins the Main that created the Job
wrote into control/state.json (`env_pins` + `env_pin_policy`) and the per-run values with request.json. refuse -> exit 3
before the child runs; warn -> journal `worker_env_differs` and run; off / no pins / no state -> no check. Names only:
never a value, never a pin hash in the journal, the status file or the pod log."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sbmig.web import contract, worker

RUN_ID = "20261110-090000-beef"
SECRET = "tok-abcdef123456"
DEFAULT_YML = ('llm:\n  provider: fake\n  base_url: "${LLM_BASE_URL}"\n  model: "${LLM_MODEL:-m0}"\n'
               "  api_key_env: LLM_API_KEY\n# a commented ${COMMENTED_OUT} placeholder is not read\n")
ARTIFACTS_YML = 'artifacts:\n  repository: "${NEXUS_URL}"\n  password: "${NEXUS_PASSWORD}"\n'
# the configuration both pods should share: placeholders of the two ymls, two overrides, the KB location
SHARED = {"LLM_BASE_URL": "http://gateway.example/v1", "LLM_MODEL": "model-same", "NEXUS_URL": "http://nexus.example/r",
          "NEXUS_PASSWORD": "nexus-pw-123456", "LLM_APP_TOKEN": SECRET, "SBMIG_BUILD__MVN": "/opt/maven/bin/mvn"}
MAIN_KB = {"SBMIG_KB__DIR": "/data/kb-v2"}            # the web pod's override ...
WORKER_KB = {"SBMIG_KB_DIR": "/data/kb-v2"}           # ... arrives in the Job pod under the Worker's own name


@pytest.fixture()
def setup(tmp_path):
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / "config" / "default.yml").write_text(DEFAULT_YML, encoding="utf-8")
    (repo / "config" / "artifacts.yml").write_text(ARTIFACTS_YML, encoding="utf-8")
    out = tmp_path / "out"
    run_dir = out / RUN_ID
    run_dir.mkdir(parents=True)
    zip_path = run_dir / "source.zip"
    zip_path.write_bytes(b"PK\x03\x04 fake zip")
    record = contract.RequestRecord(
        run_id=RUN_ID, accepted_at=contract.utc_now(), upload="app.zip", configs=["artifacts.yml"],
        zip_sha256=contract.sha256_file(zip_path), config_digests=contract.config_digests(repo, ["artifacts.yml"]),
        sbmig_version=contract.sbmig_version(), mode="k8s-job", attempt=1)
    record.write(run_dir)
    run_env = {"SBMIG_SOURCE_ZIP": str(zip_path), "SBMIG_OUT": str(out), "SBMIG_RUN_ID": RUN_ID,
               "SBMIG_APP_DIR": str(repo), "SBMIG_PYTHON": "py"}
    return {"repo": repo, "run_dir": run_dir, "record": record, "run_env": run_env}


def pin(setup, main_env: dict | None, policy: str | None = contract.ENV_PIN_REFUSE, *, pins: dict | None = None) -> dict:
    """Write control/state.json as the Main that creates the Job would; returns the pins written."""
    if pins is None and main_env is not None:
        pins = contract.env_pins(setup["repo"], ["artifacts.yml"], main_env)
    state = contract.ControlState(run_id=RUN_ID).transition(contract.RunState.CREATING, job="sbmig-x", env_pins=pins,
                                                            env_pin_policy=policy)
    state.write(setup["run_dir"])
    return pins or {}


def worker_env(setup, **extra) -> dict:
    return {**setup["run_env"], **SHARED, **WORKER_KB, **extra}


def fake_child(rc: int = 0, calls: list | None = None):
    def run(cmd, cwd, env):
        if calls is not None:
            calls.append((cmd, cwd, env))
        rd = Path(env["SBMIG_OUT"]) / env["SBMIG_RUN_ID"]
        (rd / "sbmig.log").write_text(f"SBMIG-RESULT status=success rc={rc}\n", encoding="utf-8")
        return rc
    return run


def journal(run_dir: Path) -> list[dict]:
    return [json.loads(ln) for ln in (run_dir / "journal.jsonl").read_text(encoding="utf-8").splitlines()]


def outputs(setup, captured) -> list[str]:
    rd = setup["run_dir"]
    texts = [captured.out, captured.err, (rd / "journal.jsonl").read_text(encoding="utf-8")]
    if (rd / "job-status.json").exists():
        texts.append((rd / "job-status.json").read_text(encoding="utf-8"))
    return texts


def assert_no_values_or_hashes(texts: list[str], values: list[str], *pin_sets: dict) -> None:
    hashes = {h for pins in pin_sets for h in pins.values()}
    assert hashes, "the test must pin something"
    for text in texts:
        for v in values:
            assert v not in text
        for h in hashes:
            assert h not in text and h[:10] not in text


# ---- 1. same environment on both pods

def test_identical_environment_runs(setup, capsys):
    pin(setup, {**SHARED, **MAIN_KB})
    calls: list = []
    assert worker.main(environ=worker_env(setup), run_child=fake_child(0, calls)) == 0
    assert len(calls) == 1
    events = journal(setup["run_dir"])
    assert [e["event"] for e in events] == ["worker_started", "worker_finished"]
    assert events[0]["env_pin"] == contract.ENV_PIN_REFUSE
    assert "WARNING" not in capsys.readouterr().out
    assert contract.JobStatus.read(setup["run_dir"]).exit_code == 0


# ---- 2. refuse: the three kinds of difference

DIFFS = [
    pytest.param({"LLM_MODEL": "model-main-zz9"}, {"LLM_MODEL": "model-worker-zz9"},
                 "values differ: LLM_MODEL", ["model-main-zz9", "model-worker-zz9"], id="value"),
    pytest.param({"SBMIG_SCRIPT__TIMEOUT_SECONDS": "4711"}, {},
                 "set in the web pod only: SBMIG_SCRIPT__TIMEOUT_SECONDS", ["4711"], id="main-only"),
    pytest.param({}, {"SBMIG_BUILD__JAVA_HOME": "/opt/other-jdk-77"},
                 "set in the Job pod only: SBMIG_BUILD__JAVA_HOME", ["/opt/other-jdk-77"], id="worker-only"),
]


@pytest.mark.parametrize("main_extra,worker_extra,expected,values", DIFFS)
def test_refuse_names_the_keys_and_nothing_else(setup, capsys, main_extra, worker_extra, expected, values):
    main_pins = pin(setup, {**SHARED, **MAIN_KB, **main_extra})
    env = worker_env(setup, **worker_extra)
    calls: list = []
    assert worker.main(environ=env, run_child=fake_child(0, calls)) == 3
    assert not calls
    status = json.loads((setup["run_dir"] / "job-status.json").read_text(encoding="utf-8"))
    assert status["exit_code"] == 3 and status["run_id"] == RUN_ID and status["attempt"] == 1
    assert status["error"].startswith("environment differs from the web pod's: ") and expected in status["error"]
    for hint in ("restart the web pod", "ConfigMap sbmig-env", "ConfigMap sbmig-job-template",
                 "SBMIG_JOB_ENV_PIN=warn (or off)"):
        assert hint in status["error"]
    events = journal(setup["run_dir"])
    assert [e["event"] for e in events] == ["worker_refused"]
    assert events[0]["reason"] == status["error"]
    captured = capsys.readouterr()
    assert "REFUSED: environment differs" in captured.out
    worker_pins = contract.env_pins(setup["repo"], ["artifacts.yml"], env)
    assert_no_values_or_hashes(outputs(setup, captured), values, main_pins, worker_pins)


def test_refuse_lists_every_category_in_one_sentence(setup):
    pin(setup, {**SHARED, **MAIN_KB, "LLM_MODEL": "m-a", "SBMIG_SCRIPT__X": "1"})
    env = worker_env(setup, LLM_MODEL="m-b", SBMIG_BUILD__Y="2")
    assert worker.main(environ=env, run_child=fake_child(0)) == 3
    error = contract.JobStatus.read(setup["run_dir"]).error
    assert error.startswith("environment differs from the web pod's: values differ: LLM_MODEL; "
                            "set in the web pod only: SBMIG_SCRIPT__X; set in the Job pod only: SBMIG_BUILD__Y — ")
    assert error.endswith(worker.ENV_PIN_HINT)


# ---- 3. warn: the same differences are journalled and the run goes on

@pytest.mark.parametrize("main_extra,worker_extra,expected,values", DIFFS)
def test_warn_journals_names_and_runs(setup, capsys, main_extra, worker_extra, expected, values):
    main_pins = pin(setup, {**SHARED, **MAIN_KB, **main_extra}, contract.ENV_PIN_WARN)
    env = worker_env(setup, **worker_extra, SBMIG_FAIL_JOB_ON_BUILD_FAIL="1")
    calls: list = []
    assert worker.main(environ=env, run_child=fake_child(2, calls)) == 2      # the exit code follows the child
    assert len(calls) == 1
    events = journal(setup["run_dir"])
    assert [e["event"] for e in events] == ["worker_env_differs", "worker_started", "worker_finished"]
    differs = events[0]
    assert differs["policy"] == "warn" and differs["run_id"] == RUN_ID and differs["run_values"] == []
    name = expected.split(": ")[1]
    category = {"values differ": "differs", "set in the web pod only": "main_only",
                "set in the Job pod only": "worker_only"}[expected.split(": ")[0]]
    assert differs[category] == [name]
    assert all(differs[k] == [] for k in ("differs", "main_only", "worker_only") if k != category)
    assert "check_error" not in differs
    assert events[1]["env_pin"] == "warn"
    assert contract.JobStatus.read(setup["run_dir"]).exit_code == 2
    captured = capsys.readouterr()
    assert f"[worker] WARNING: environment differs from the web pod's: {expected}" in captured.out
    worker_pins = contract.env_pins(setup["repo"], ["artifacts.yml"], env)
    assert_no_values_or_hashes(outputs(setup, captured), values, main_pins, worker_pins)


# ---- 4. no check: policy off, no pins, no control state

@pytest.mark.parametrize("arrange", ["off", "no-pins", "no-state"])
def test_no_check_without_pins_or_with_policy_off(setup, arrange):
    main_env = {**SHARED, **MAIN_KB, "LLM_MODEL": "model-main"}
    if arrange == "off":
        pin(setup, main_env, contract.ENV_PIN_OFF)
    elif arrange == "no-pins":
        pin(setup, None, contract.ENV_PIN_REFUSE, pins=None)
    env = worker_env(setup, LLM_MODEL="model-worker", SBMIG_BUILD__Y="2")
    calls: list = []
    assert worker.main(environ=env, run_child=fake_child(0, calls)) == 0
    assert len(calls) == 1
    events = journal(setup["run_dir"])
    assert [e["event"] for e in events] == ["worker_started", "worker_finished"]
    assert "env_pin" not in events[0]


def test_check_environment_returns_none_when_nothing_to_check(setup):
    record = setup["record"]
    assert worker.check_environment(record, None, worker_env(setup), setup["repo"]) is None
    state = contract.ControlState(run_id=RUN_ID, env_pins=None, env_pin_policy="refuse")
    assert worker.check_environment(record, state, worker_env(setup), setup["repo"]) is None
    state = contract.ControlState(run_id=RUN_ID, env_pins={"LLM_MODEL": "0" * 16}, env_pin_policy=" OFF ")
    assert worker.check_environment(record, state, worker_env(setup), setup["repo"]) is None


# ---- 5. pins without a policy (or with an unknown one) = refuse

@pytest.mark.parametrize("policy", [None, "", "maybe"])
def test_pins_without_a_known_policy_refuse(setup, policy):
    pin(setup, {**SHARED, **MAIN_KB, "LLM_MODEL": "model-main"}, policy)
    calls: list = []
    assert worker.main(environ=worker_env(setup), run_child=fake_child(0, calls)) == 3
    assert not calls
    assert "values differ: LLM_MODEL" in contract.JobStatus.read(setup["run_dir"]).error


# ---- 6. secret-looking names, per-pod names and the KB alias

def test_secrets_and_record_dirs_never_count(setup, capsys):
    main_env = {**SHARED, **MAIN_KB, "LLM_APP_TOKEN": "main-token-999999", "NEXUS_PASSWORD": "main-pw-999999",
                "LLM_API_KEY": "main-key-999999", "SBMIG_LLM__RECORD_DIR": "/data/llm-records",
                "LLM_RECORD_DIR": "/data/records-main"}
    main_pins = pin(setup, main_env)
    assert not {"LLM_APP_TOKEN", "NEXUS_PASSWORD", "LLM_API_KEY", "SBMIG_LLM__RECORD_DIR", "LLM_RECORD_DIR"} & set(main_pins)
    env = worker_env(setup, LLM_APP_TOKEN="worker-token-888888", NEXUS_PASSWORD="worker-pw-888888",
                     LLM_RECORD_DIR="/data/records-worker")
    calls: list = []
    assert worker.main(environ=env, run_child=fake_child(0, calls)) == 0
    assert len(calls) == 1
    assert [e["event"] for e in journal(setup["run_dir"])] == ["worker_started", "worker_finished"]
    captured = capsys.readouterr()
    for text in outputs(setup, captured):
        for name in ("LLM_APP_TOKEN", "NEXUS_PASSWORD", "LLM_API_KEY", "RECORD_DIR"):
            assert name not in text


def test_secrets_stay_out_of_a_refusal_for_another_key(setup, capsys):
    main_pins = pin(setup, {**SHARED, **MAIN_KB, "LLM_MODEL": "m-main", "NEXUS_PASSWORD": "main-pw-999999"})
    env = worker_env(setup, LLM_MODEL="m-worker", NEXUS_PASSWORD="worker-pw-888888", LLM_APP_TOKEN="worker-token-888888")
    assert worker.main(environ=env, run_child=fake_child(0)) == 3
    error = contract.JobStatus.read(setup["run_dir"]).error
    assert error.startswith("environment differs from the web pod's: values differ: LLM_MODEL — ")
    captured = capsys.readouterr()
    for text in outputs(setup, captured):
        assert "NEXUS_PASSWORD" not in text and "LLM_APP_TOKEN" not in text
    assert_no_values_or_hashes(outputs(setup, captured),
                               ["main-pw-999999", "worker-pw-888888", "worker-token-888888", "m-main", "m-worker"],
                               main_pins, contract.env_pins(setup["repo"], ["artifacts.yml"], env))


def test_kb_dir_alias_matches(setup):
    pin(setup, {**SHARED, "SBMIG_KB__DIR": "/x"})
    calls: list = []
    assert worker.main(environ={**worker_env(setup), "SBMIG_KB_DIR": "/x"}, run_child=fake_child(0, calls)) == 0
    assert len(calls) == 1


def test_kb_dir_missing_in_the_job_pod_is_named(setup):
    pin(setup, {**SHARED, "SBMIG_KB__DIR": "/x"})
    env = {**setup["run_env"], **SHARED}                 # neither SBMIG_KB_DIR nor SBMIG_KB__DIR in the Job pod
    calls: list = []
    assert worker.main(environ=env, run_child=fake_child(0, calls)) == 3
    assert not calls
    assert "set in the web pod only: SBMIG_KB__DIR" in contract.JobStatus.read(setup["run_dir"]).error


def test_kb_dir_alias_with_another_value_differs(setup):
    pin(setup, {**SHARED, "SBMIG_KB__DIR": "/x"})
    assert worker.main(environ={**worker_env(setup), "SBMIG_KB_DIR": "/y"}, run_child=fake_child(0)) == 3
    assert "values differ: SBMIG_KB__DIR" in contract.JobStatus.read(setup["run_dir"]).error


# ---- 7. per-run values against request.json

BAD_RUN_VALUES = [
    pytest.param({"SBMIG_CONFIG": "other.yml"}, {}, "SBMIG_CONFIG", id="config-other-file"),
    pytest.param({"SBMIG_CONFIG": "artifacts.yml default.yml"}, {}, "SBMIG_CONFIG", id="config-extra-file"),
    pytest.param({"SBMIG_STOP_AFTER": "restructure"}, {}, "SBMIG_STOP_AFTER", id="stop-after-set"),
    pytest.param({}, {"stop_after": "restructure"}, "SBMIG_STOP_AFTER", id="stop-after-missing"),
    pytest.param({"SBMIG_LLM_PROVIDER": "fake"}, {"llm_provider": "company"}, "SBMIG_LLM_PROVIDER", id="provider"),
]


def _rewrite_record(setup, changes: dict) -> None:
    if changes:
        data = {**setup["record"].to_json(), **changes}
        contract.RequestRecord.from_json(data).write(setup["run_dir"])


@pytest.mark.parametrize("env_extra,record_changes,name", BAD_RUN_VALUES)
def test_run_values_refused(setup, capsys, env_extra, record_changes, name):
    _rewrite_record(setup, record_changes)
    pin(setup, {**SHARED, **MAIN_KB})
    calls: list = []
    assert worker.main(environ=worker_env(setup, **env_extra), run_child=fake_child(0, calls)) == 3
    assert not calls
    error = contract.JobStatus.read(setup["run_dir"]).error
    assert error.startswith(name) and "environment differs" not in error and "restart the web pod" in error
    if name == "SBMIG_CONFIG":
        assert "than request.json (artifacts.yml)" in error
    else:
        # names only: the per-run values themselves are not echoed
        for v in [*env_extra.values(), *record_changes.values()]:
            assert v not in error
    assert journal(setup["run_dir"])[-1]["event"] == "worker_refused"


@pytest.mark.parametrize("env_extra,record_changes,name", BAD_RUN_VALUES)
def test_run_values_warned(setup, capsys, env_extra, record_changes, name):
    _rewrite_record(setup, record_changes)
    pin(setup, {**SHARED, **MAIN_KB}, contract.ENV_PIN_WARN)
    calls: list = []
    assert worker.main(environ=worker_env(setup, **env_extra), run_child=fake_child(0, calls)) == 0
    assert len(calls) == 1
    differs = journal(setup["run_dir"])[0]
    assert differs["event"] == "worker_env_differs" and differs["run_values"] == [name]
    assert differs["differs"] == differs["main_only"] == differs["worker_only"] == []
    assert f"[worker] WARNING: {name}" in capsys.readouterr().out


def test_equivalent_run_values_pass(setup, tmp_path):
    repo = setup["repo"]
    _rewrite_record(setup, {"stop_after": "restructure", "llm_provider": "fake"})
    pin(setup, {**SHARED, **MAIN_KB})
    spellings = [None, "artifacts.yml", str(repo / "config" / "artifacts.yml"),
                 str(repo / "config" / ".." / "config" / "artifacts.yml"), "config/artifacts.yml"]
    link = tmp_path / "repo-link"
    try:
        os.symlink(repo, link, target_is_directory=True)
        spellings.append(str(link / "config" / "artifacts.yml"))
    except (OSError, NotImplementedError):
        pass                                             # no symlinks on this machine: the other spellings still run
    for spelling in spellings:
        extra = {"SBMIG_STOP_AFTER": "restructure", "SBMIG_LLM_PROVIDER": "fake"}
        if spelling is not None:
            extra["SBMIG_CONFIG"] = spelling
        calls: list = []
        assert worker.main(environ=worker_env(setup, **extra), run_child=fake_child(0, calls)) == 0, spelling
        assert len(calls) == 1
        assert "worker_env_differs" not in {e["event"] for e in journal(setup["run_dir"])}


def test_empty_run_values_equal_missing_ones(setup):
    pin(setup, {**SHARED, **MAIN_KB})
    env = worker_env(setup, SBMIG_STOP_AFTER="", SBMIG_LLM_PROVIDER="", SBMIG_CONFIG="  ")
    calls: list = []
    assert worker.main(environ=env, run_child=fake_child(0, calls)) == 0
    assert len(calls) == 1


# ---- 8. a check that breaks: fail closed under refuse, go on under warn

def _boom(*_a, **_k):
    raise RuntimeError("leaky-value-31337 should never be shown")


def test_failing_check_refuses(setup, capsys, monkeypatch):
    pin(setup, {**SHARED, **MAIN_KB})
    monkeypatch.setattr(contract, "env_pins", _boom)
    calls: list = []
    assert worker.main(environ=worker_env(setup), run_child=fake_child(0, calls)) == 3
    assert not calls
    error = contract.JobStatus.read(setup["run_dir"]).error
    assert error.startswith("the environment check failed: RuntimeError — ")
    for text in outputs(setup, capsys.readouterr()):
        assert "leaky-value-31337" not in text


def test_failing_check_warns_and_runs(setup, capsys, monkeypatch):
    pin(setup, {**SHARED, **MAIN_KB}, contract.ENV_PIN_WARN)
    monkeypatch.setattr(contract, "env_pins", _boom)
    calls: list = []
    assert worker.main(environ=worker_env(setup), run_child=fake_child(0, calls)) == 0
    assert len(calls) == 1
    events = journal(setup["run_dir"])
    assert [e["event"] for e in events] == ["worker_env_differs", "worker_started", "worker_finished"]
    assert events[0]["check_error"] == "RuntimeError" and events[0]["policy"] == "warn"
    for text in outputs(setup, capsys.readouterr()):
        assert "leaky-value-31337" not in text


def test_unreadable_control_state_refuses(setup, monkeypatch):
    pin(setup, {**SHARED, **MAIN_KB}, contract.ENV_PIN_WARN)

    def broken(cls, run_dir):
        raise RecursionError("deeply nested")
    monkeypatch.setattr(contract.ControlState, "read", classmethod(broken))
    calls: list = []
    assert worker.main(environ=worker_env(setup), run_child=fake_child(0, calls)) == 3
    assert not calls
    assert "the environment check failed: RecursionError" in contract.JobStatus.read(setup["run_dir"]).error


def test_corrupt_pins_refuse(setup):
    pin(setup, None, contract.ENV_PIN_REFUSE, pins=None)
    state_file = setup["run_dir"] / "control" / "state.json"
    data = json.loads(state_file.read_text(encoding="utf-8"))
    data["env_pins"] = "not-a-mapping"
    state_file.write_text(json.dumps(data), encoding="utf-8")
    calls: list = []
    assert worker.main(environ=worker_env(setup), run_child=fake_child(0, calls)) == 3
    assert not calls
    assert "the environment check failed: ValueError" in contract.JobStatus.read(setup["run_dir"]).error
