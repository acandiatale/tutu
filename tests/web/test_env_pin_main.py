"""Review 2026-11-10 (finding 3, Main side): the Main that creates a Job pins the environment values the configuration reads
(`env_pins`: name → sha256 prefix, never a secret-looking name, never a value) and its policy `SBMIG_JOB_ENV_PIN` into
`control/state.json`, in the same `creating` transition that is written before `create_job` is called. The policy defaults to
`refuse` in k8s-job and `off` in local-job; a queued run carried across a restart is pinned by the Main that creates its Job,
an adopted run keeps the pins already on disk."""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
from pathlib import Path

import pytest

import sbmig.web.runs as runs_mod
from sbmig.core.config import PACKAGE_ROOT
from sbmig.web import contract
from sbmig.web.contract import RUN_LABEL, ControlState
from sbmig.web.k8s import job_name
from sbmig.web.runs import RunManager

from .test_jobmode_hardening import TEMPLATE, ZIP, Cluster, _mgr, _state, _wait

CONFIG = "artifacts.local-e2e.yml"
SECRET = "tok-abcdefghij123456"                     # 20 characters, under a secret-looking name


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Like test_jobmode_hardening's repo, but default.yml reads two placeholders: one the Main pins and one secret-looking
    name it must never pin."""
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    shutil.copy(PACKAGE_ROOT / "config" / CONFIG, repo / "config" / CONFIG)
    (repo / "config" / "default.yml").write_text(
        'x: 1\nllm:\n  model: "${LLM_MODEL:-fallback-model}"\n  app_token: "${LLM_APP_TOKEN:-}"\n', encoding="utf-8")
    return repo


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SBMIG_JOB_POLL_S", "0.05")
    monkeypatch.delenv(contract.ENV_PIN_POLICY_ENV, raising=False)     # a developer shell must not choose the policy


def _h(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:contract.ENV_PIN_HASH_LEN]


def _capture_creates(cluster: Cluster, out: Path, repo: Path) -> list[dict]:
    """Wrap `cluster.create_job`: record what control/state.json holds ON DISK at the moment the Job is created, and the pins
    this process's environment gives at that same moment."""
    seen: list[dict] = []
    real = cluster.create_job

    def create_job(manifest: dict) -> str:
        rid = manifest["metadata"]["labels"][RUN_LABEL]
        seen.append({"run_id": rid,
                     "disk": contract.read_json(out / rid / contract.CONTROL_DIR / contract.STATE_FILE),
                     "expected": contract.env_pins(repo, [CONFIG])})
        return real(manifest)
    cluster.create_job = create_job
    return seen


def _state_text(out: Path, rid: str) -> str:
    return (out / rid / contract.CONTROL_DIR / contract.STATE_FILE).read_text(encoding="utf-8")


# ---- 1. k8s-job default: refuse, pins written with the intent, before create_job
def test_k8s_job_pins_the_environment_in_the_creating_state_before_the_job_is_created(tmp_path: Path, repo: Path, monkeypatch):
    monkeypatch.setenv("SBMIG_SCRIPT__MAX_LLM_TRIES_PER_CASE", "7")
    monkeypatch.setenv("LLM_MODEL", "some-role")
    out = tmp_path / "out"
    cluster = Cluster(out, phases=["Running", "Succeeded"])
    seen = _capture_creates(cluster, out, repo)
    mgr = _mgr(tmp_path, repo, cluster)
    assert mgr.env_pin_policy == contract.ENV_PIN_REFUSE
    rid = mgr.start(ZIP, "p.zip", [CONFIG])
    assert mgr.runs[rid].done.wait(10) and mgr.runs[rid].exit_code == 0
    assert len(seen) == 1 and seen[0]["run_id"] == rid
    disk = seen[0]["disk"]
    assert disk is not None and disk["state"] == "creating" and disk["job"] == job_name(rid)
    assert disk["env_pin_policy"] == "refuse" and isinstance(disk["env_pins"], dict)
    assert disk["env_pins"] == seen[0]["expected"] == contract.env_pins(mgr.repo_root, [CONFIG])
    assert {"SBMIG_SCRIPT__MAX_LLM_TRIES_PER_CASE", "LLM_MODEL"} <= set(disk["env_pins"])
    assert disk["env_pins"]["LLM_MODEL"] == _h("some-role") and disk["env_pins"]["SBMIG_SCRIPT__MAX_LLM_TRIES_PER_CASE"] == _h("7")


# ---- 2. no secret name, no value — neither in state.json nor in the log
def test_secret_names_and_pinned_values_never_reach_the_state_file_or_the_log(tmp_path: Path, repo: Path, monkeypatch, caplog):
    monkeypatch.setenv("LLM_APP_TOKEN", SECRET)
    monkeypatch.setenv("LLM_MODEL", "pinned-model-role-x1")
    monkeypatch.setenv("SBMIG_LLM__ESCALATION_MODEL", "pinned-escalation-role-x2")
    out = tmp_path / "out"
    cluster = Cluster(out, phases=["Running", "Succeeded"])
    seen = _capture_creates(cluster, out, repo)
    mgr = _mgr(tmp_path, repo, cluster)
    with caplog.at_level(logging.DEBUG, logger="sbmig.web"):
        rid = mgr.start(ZIP, "p.zip", [CONFIG])
        assert mgr.runs[rid].done.wait(10)
    pins = seen[0]["disk"]["env_pins"]
    assert "LLM_APP_TOKEN" not in pins and not any(contract.is_secret_name(n) for n in pins)
    assert {"LLM_MODEL", "SBMIG_LLM__ESCALATION_MODEL"} <= set(pins)
    values = [v for v in (contract.env_pin_value(n) for n in pins) if v and len(v) >= 6]
    assert "pinned-model-role-x1" in values and "pinned-escalation-role-x2" in values
    for text in (json.dumps(seen[0]["disk"]), _state_text(out, rid)):
        assert SECRET not in text
        assert not [v for v in values if v in text]
    # exactly one INFO line says how many names were pinned and under which policy — no name, no value, no hash
    lines = [r.getMessage() for r in caplog.records if r.name == "sbmig.web"]
    pinned = [m for m in lines if "environment value(s) pinned" in m]
    assert len(pinned) == 1 and f"{len(pins)} environment value(s)" in pinned[0] and "SBMIG_JOB_ENV_PIN=refuse" in pinned[0]
    mine = [SECRET, "pinned-model-role-x1", "pinned-escalation-role-x2", *pins.values()]
    for m in lines:
        assert not [v for v in mine if v in m]
    assert not [n for n in pins if n in pinned[0]]


# ---- 3. SBMIG_JOB_ENV_PIN on the web pod
@pytest.mark.parametrize(("raw", "policy", "pinned"), [
    ("off", "off", False),
    ("warn", "warn", True),
    (" WARN ", "warn", True),
    ("Refuse", "refuse", True),
])
def test_the_policy_variable_is_read_once_and_written_with_the_pins(tmp_path: Path, repo: Path, monkeypatch, raw: str,
                                                                    policy: str, pinned: bool):
    monkeypatch.setenv(contract.ENV_PIN_POLICY_ENV, raw)
    monkeypatch.setenv("LLM_MODEL", "some-role")
    out = tmp_path / "out"
    cluster = Cluster(out, phases=["Running", "Succeeded"])
    seen = _capture_creates(cluster, out, repo)
    mgr = _mgr(tmp_path, repo, cluster)
    assert mgr.env_pin_policy == policy
    monkeypatch.setenv(contract.ENV_PIN_POLICY_ENV, "off" if policy != "off" else "refuse")   # read at startup, not per Job
    rid = mgr.start(ZIP, "p.zip", [CONFIG])
    assert mgr.runs[rid].done.wait(10) and mgr.runs[rid].exit_code == 0
    disk = seen[0]["disk"]
    assert disk["state"] == "creating" and disk["env_pin_policy"] == policy
    if pinned:
        assert disk["env_pins"] == seen[0]["expected"] and "LLM_MODEL" in disk["env_pins"]
    else:
        assert disk["env_pins"] is None
    cs = _state(out, rid)
    assert cs.state == "finished" and cs.env_pin_policy == policy and cs.env_pins == disk["env_pins"]


def test_an_unknown_policy_value_falls_back_to_the_default_with_a_warning(tmp_path: Path, repo: Path, monkeypatch, caplog):
    monkeypatch.setenv(contract.ENV_PIN_POLICY_ENV, "bogus")
    out = tmp_path / "out"
    with caplog.at_level(logging.WARNING, logger="sbmig.web"):
        mgr = _mgr(tmp_path, repo, Cluster(out))
    assert mgr.env_pin_policy == contract.ENV_PIN_REFUSE
    warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and contract.ENV_PIN_POLICY_ENV in r.getMessage()]
    assert len(warns) == 1 and "'bogus'" in warns[0] and "refuse" in warns[0]
    # an empty value is simply the default, no warning
    caplog.clear()
    monkeypatch.setenv(contract.ENV_PIN_POLICY_ENV, "  ")
    with caplog.at_level(logging.WARNING, logger="sbmig.web"):
        mgr2 = _mgr(tmp_path, repo, Cluster(out))
    assert mgr2.env_pin_policy == contract.ENV_PIN_REFUSE
    assert not [r for r in caplog.records if contract.ENV_PIN_POLICY_ENV in r.getMessage()]


# ---- 4. local-job: off by default (the local Worker inherits this process's environment)
def test_local_job_defaults_to_off_and_writes_no_pins(tmp_path: Path, repo: Path, monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "some-role")
    out = tmp_path / "out"
    cluster = Cluster(out, phases=["Running", "Succeeded"])
    seen = _capture_creates(cluster, out, repo)
    mgr = RunManager(out, repo, mode="local-job", job_client=cluster, job_template=TEMPLATE)
    assert mgr.effective_mode == "local-job" and mgr.env_pin_policy == contract.ENV_PIN_OFF
    rid = mgr.start(ZIP, "p.zip", [CONFIG])
    assert mgr.runs[rid].done.wait(10) and mgr.runs[rid].exit_code == 0
    assert seen[0]["disk"]["state"] == "creating" and seen[0]["disk"]["env_pin_policy"] == "off"
    assert seen[0]["disk"]["env_pins"] is None and _state(out, rid).env_pins is None
    # an explicit value is honoured in local-job too
    monkeypatch.setenv(contract.ENV_PIN_POLICY_ENV, "warn")
    assert RunManager(tmp_path / "out2", repo, mode="local-job", job_client=cluster, job_template=TEMPLATE).env_pin_policy == "warn"


def test_k8s_job_that_degrades_to_local_job_on_a_developer_pc_uses_the_local_default(tmp_path: Path, repo: Path, monkeypatch):
    """The default follows the EFFECTIVE mode: `SBMIG_WEB_MODE=k8s-job` outside a cluster runs local subprocesses."""
    monkeypatch.setattr(runs_mod, "in_cluster_available", lambda: False)
    monkeypatch.setattr(runs_mod, "cluster_host_present", lambda: False)
    mgr = RunManager(tmp_path / "out", repo, mode="k8s-job", job_template=TEMPLATE)
    assert mgr.mode == "k8s-job" and mgr.effective_mode == "local-job" and mgr.env_pin_policy == contract.ENV_PIN_OFF


# ---- 5. computing the pins fails: the Job is still created, without pins
def test_a_failure_to_compute_the_pins_never_fails_the_job_creation(tmp_path: Path, repo: Path, monkeypatch, caplog):
    def boom(*a, **kw):
        raise RuntimeError("config unreadable")
    out = tmp_path / "out"
    cluster = Cluster(out, phases=["Running", "Succeeded"])
    real_create = cluster.create_job
    disk_at_create: list[dict | None] = []

    def create_job(manifest):
        rid = manifest["metadata"]["labels"][RUN_LABEL]
        disk_at_create.append(contract.read_json(out / rid / contract.CONTROL_DIR / contract.STATE_FILE))
        return real_create(manifest)
    cluster.create_job = create_job
    mgr = _mgr(tmp_path, repo, cluster)
    monkeypatch.setattr(contract, "env_pins", boom)
    with caplog.at_level(logging.WARNING, logger="sbmig.web"):
        rid = mgr.start(ZIP, "p.zip", [CONFIG])
        assert mgr.runs[rid].done.wait(10)
    assert len(cluster.created) == 1 and mgr.runs[rid].exit_code == 0 and mgr.runs[rid].error is None
    assert disk_at_create[0]["state"] == "creating" and disk_at_create[0]["env_pins"] is None
    assert disk_at_create[0]["env_pin_policy"] == "refuse"
    warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "not pinned" in r.getMessage()]
    assert len(warns) == 1 and rid in warns[0] and "RuntimeError" in warns[0]
    cs = _state(out, rid)
    assert cs.state == "finished" and cs.env_pins is None


# ---- 6. restart: the queued run is pinned by the Main that creates its Job; the adopted run keeps the pins on disk
def test_a_queued_run_carried_across_a_restart_gets_the_new_mains_pins(tmp_path: Path, repo: Path, monkeypatch):
    monkeypatch.setenv("SBMIG_JOB_EXIT_GRACE_S", "60")
    monkeypatch.setenv("LLM_MODEL", "role-before-restart")
    out = tmp_path / "out"
    cluster = Cluster(out, phases=["Running"])                      # the first pod writes its result and keeps running
    seen = _capture_creates(cluster, out, repo)
    mgr = _mgr(tmp_path, repo, cluster)
    first = mgr.start(ZIP, "a.zip", [CONFIG])
    assert _wait(lambda: (out / first / "job-status.json").is_file())
    second = mgr.start(ZIP, "b.zip", [CONFIG])
    time.sleep(0.2)
    assert mgr.status(second)["state"] == "queued" and len(cluster.created) == 1
    pins_before = _state(out, first).env_pins
    assert pins_before is not None and pins_before["LLM_MODEL"] == _h("role-before-restart")
    mgr.shutdown()
    assert mgr.runs[first].done.wait(10) and mgr.runs[second].done.wait(10)
    assert _state(out, first).state == "running" and _state(out, second).state == "queued"
    assert _state(out, second).env_pins is None                     # nothing is pinned at intake
    # the web pod restarts with a changed ConfigMap
    monkeypatch.setenv("LLM_MODEL", "role-after-restart")
    monkeypatch.setenv("SBMIG_SCRIPT__MAX_LLM_TRIES_PER_CASE", "9")
    cluster.phases = ["Running", "Succeeded"]                       # what the second Job will do
    cluster.jobs[job_name(first)]["phases"] = ["Succeeded"]         # the first pod exits
    mgr2 = _mgr(tmp_path, repo, cluster)
    assert sorted(mgr2.recovered) == sorted([first, second])     # same-second intake: the order falls back to the run ids
    assert mgr2.runs[first].done.wait(10) and mgr2.runs[second].done.wait(10)
    assert [s["run_id"] for s in seen] == [first, second] and len(cluster.created) == 2
    created_second = seen[1]["disk"]
    assert created_second["state"] == "creating" and created_second["env_pin_policy"] == "refuse"
    assert created_second["env_pins"] == seen[1]["expected"] == contract.env_pins(repo, [CONFIG])
    assert created_second["env_pins"]["LLM_MODEL"] == _h("role-after-restart")
    assert created_second["env_pins"]["SBMIG_SCRIPT__MAX_LLM_TRIES_PER_CASE"] == _h("9")
    assert created_second["env_pins"] != pins_before
    # the adopted run keeps what the Main that created its Job wrote
    cs_first = _state(out, first)
    assert cs_first.state == "finished" and cs_first.env_pins == pins_before and cs_first.env_pin_policy == "refuse"
    assert _state(out, second).state == "finished" and _state(out, second).env_pins == created_second["env_pins"]


# ---- 7. the pins survive every later transition
def test_the_pins_survive_every_later_transition(tmp_path: Path, repo: Path, monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "some-role")
    written: list[tuple[str, dict | None, str | None]] = []
    orig = ControlState.write

    def write(self, run_dir):
        written.append((self.state, None if self.env_pins is None else dict(self.env_pins), self.env_pin_policy))
        return orig(self, run_dir)
    monkeypatch.setattr(ControlState, "write", write)
    out = tmp_path / "out"
    mgr = _mgr(tmp_path, repo, Cluster(out, phases=["Running", "Succeeded"]))
    rid = mgr.start(ZIP, "p.zip", [CONFIG])
    assert mgr.runs[rid].done.wait(10) and mgr.runs[rid].exit_code == 0
    assert [w[0] for w in written] == ["queued", "creating", "created", "running", "finished"]
    assert written[0][1:] == (None, None)                           # intake pins nothing
    pins = written[1][1]
    assert pins and "LLM_MODEL" in pins
    assert all(w[1] == pins and w[2] == "refuse" for w in written[1:])
    raw = contract.read_json(out / rid / contract.CONTROL_DIR / contract.STATE_FILE)
    assert raw is not None and raw["state"] == "finished" and raw["env_pins"] == pins and raw["env_pin_policy"] == "refuse"
