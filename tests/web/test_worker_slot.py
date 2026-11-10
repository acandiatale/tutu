"""v2.1z-ae: up to N build Jobs at once share one `~/.m2` PVC, so each slot gets its own Maven local repository. Main passes
`SBMIG_JOB_SLOT` (1-based); the Worker leaves slot 1 (and no slot) byte-identical to before and gives slot k >= 2 the sibling
`<base>-slot<k>` through `SBMIG_BUILD__LOCAL_REPO` in the CHILD's environment only, after the environment-pin check. It never
fails a run over this: an unresolvable base or an uncreatable / unwritable directory -> a pod-local repository under the temp
directory (`pod_local: true` + note; review 2026-11-10), and only when that fails too (or the slot number is bad) the shared
repository + a note. Journal event `worker_slot` {slot, local_repo, shared, pod_local?, note?} only when `SBMIG_JOB_SLOT` is set."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from sbmig.web import contract, worker

REPO_ROOT = Path(__file__).resolve().parents[2]
SECRET = "abcdef123456"
RUN_ID = "20261110-130000-cafe"


@pytest.fixture(autouse=True)
def pod_tmp(tmp_path, monkeypatch):
    """The pod-local fallback lives under tempfile.gettempdir(): point it into the test's own folder."""
    root = tmp_path / "pod-tmp"
    root.mkdir()
    monkeypatch.setattr(worker.tempfile, "gettempdir", lambda: str(root))
    return root


@pytest.fixture()
def setup(tmp_path):
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / "config" / "default.yml").write_text("llm:\n  provider: fake\n", encoding="utf-8")
    (repo / "config" / "artifacts.yml").write_text("artifacts: {}\n", encoding="utf-8")
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
    environ = {"SBMIG_SOURCE_ZIP": str(zip_path), "SBMIG_OUT": str(out), "SBMIG_RUN_ID": RUN_ID,
               "SBMIG_APP_DIR": str(repo), "SBMIG_PYTHON": "py", "SBMIG_KB_DIR": str(tmp_path / "kb"),
               "LLM_APP_TOKEN": SECRET}
    return {"tmp": tmp_path, "repo": repo, "out": out, "run_dir": run_dir, "zip": zip_path, "env": environ,
            "record": record}


def fake_child(rc: int = 0, calls: list | None = None):
    def run(cmd, cwd, env):
        if calls is not None:
            calls.append((cmd, cwd, env))
        rd = Path(env["SBMIG_OUT"]) / env["SBMIG_RUN_ID"]
        (rd / "sbmig.log").write_text(f"SBMIG-RESULT status=success rc={rc}\n", encoding="utf-8")
        return rc
    return run


class Resolver:
    """An injected base resolver: records its calls, answers `base` or raises `exc`."""

    def __init__(self, base: Path | None = None, exc: BaseException | None = None):
        self.base, self.exc, self.calls = base, exc, []

    def __call__(self, configs, paths_yml):
        self.calls.append((list(configs), Path(paths_yml)))
        if self.exc is not None:
            raise self.exc
        return self.base


def journal(run_dir: Path) -> list[dict]:
    return [json.loads(ln) for ln in (run_dir / "journal.jsonl").read_text(encoding="utf-8").splitlines()]


def slot_events(run_dir: Path) -> list[dict]:
    return [e for e in journal(run_dir) if e["event"] == worker.SLOT_EVENT]


def expected_cmd(setup) -> list[str]:
    return ["py", "-m", "sbmig.cli", "run", str(setup["zip"]),
            "--config", str(setup["repo"] / "config" / "artifacts.yml"),
            "--config", str(setup["run_dir"] / "worker-paths.yml"),
            "--out", str(setup["out"]), "--run-id", RUN_ID]


def run_once(setup, env: dict, resolver: Resolver | None = None, rc: int = 0):
    calls: list = []
    code = worker.main(environ=env, run_child=fake_child(rc, calls), repo_resolver=resolver or Resolver())
    assert len(calls) == 1
    cmd, cwd, child = calls[0]
    return code, cmd, cwd, child


# ---- 1. no slot / slot 1: nothing changes

@pytest.mark.parametrize("local_repo", [None, "null", "/opt/m2/repository"])
def test_no_slot_child_env_and_command_unchanged(setup, capsys, local_repo):
    env = dict(setup["env"])
    if local_repo is not None:
        env[worker.LOCAL_REPO_ENV] = local_repo
    resolver = Resolver(exc=AssertionError("the resolver must not be called without a slot"))
    code, cmd, cwd, child = run_once(setup, env, resolver)
    assert code == 0 and cwd == setup["repo"]
    assert child == worker.child_environ(env)               # exactly today's child environment
    assert child.get(worker.LOCAL_REPO_ENV) == local_repo
    assert cmd == expected_cmd(setup)
    assert not resolver.calls
    assert [e["event"] for e in journal(setup["run_dir"])] == ["worker_started", "worker_finished"]
    assert "job slot" not in capsys.readouterr().out


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_slot_is_no_slot(setup, blank):
    env = {**setup["env"], contract.SLOT_ENV: blank}
    resolver = Resolver(exc=AssertionError("not called"))
    code, cmd, _, child = run_once(setup, env, resolver)
    assert code == 0 and child == worker.child_environ(env) and cmd == expected_cmd(setup)
    assert not resolver.calls and slot_events(setup["run_dir"]) == []


def test_slot_one_is_unchanged_and_journalled(setup, capsys):
    env = {**setup["env"], contract.SLOT_ENV: "1", worker.LOCAL_REPO_ENV: "null"}
    resolver = Resolver(exc=AssertionError("not called"))
    code, cmd, _, child = run_once(setup, env, resolver)
    assert code == 0
    assert child == worker.child_environ(env) and child[worker.LOCAL_REPO_ENV] == "null"
    assert cmd == expected_cmd(setup)
    assert not resolver.calls
    events = journal(setup["run_dir"])
    assert [e["event"] for e in events] == ["worker_started", worker.SLOT_EVENT, "worker_finished"]
    data = {k: v for k, v in events[1].items() if k not in ("ts", "event")}
    assert data == {"slot": 1, "local_repo": None, "shared": False}
    assert "[worker] job slot 1: the Maven local repository is unchanged" in capsys.readouterr().out


# ---- 2. slot k >= 2: its own repository beside the base

def test_slot_two_from_the_workers_own_override(setup, capsys):
    base = setup["tmp"] / "repo-m2" / "repository"
    env = {**setup["env"], contract.SLOT_ENV: "2", worker.LOCAL_REPO_ENV: str(base)}
    original = dict(env)
    resolver = Resolver(exc=AssertionError("the Worker's own override wins"))
    code, cmd, _, child = run_once(setup, env, resolver)
    want = base.parent / "repository-slot2"
    assert code == 0 and cmd == expected_cmd(setup)
    assert child[worker.LOCAL_REPO_ENV] == str(want) and want.is_dir()
    assert env == original                                    # the Worker's own environment is untouched
    assert {k: v for k, v in child.items() if k != worker.LOCAL_REPO_ENV} == \
        {k: v for k, v in worker.child_environ(env).items() if k != worker.LOCAL_REPO_ENV}
    assert not resolver.calls
    (ev,) = slot_events(setup["run_dir"])
    assert {k: v for k, v in ev.items() if k not in ("ts", "event")} == \
        {"slot": 2, "local_repo": str(want), "shared": False}
    assert f"[worker] job slot 2: own Maven local repository {want}" in capsys.readouterr().out


@pytest.mark.parametrize("local_repo", ["null", None, "", "~"])
def test_slot_three_from_the_resolver(setup, local_repo):
    """`"null"` is what the shipped Job template sets; `""` and `~` also load as YAML null."""
    env = {**setup["env"], contract.SLOT_ENV: "3"}
    if local_repo is not None:
        env[worker.LOCAL_REPO_ENV] = local_repo
    resolver = Resolver(base=setup["tmp"] / "m2" / "repository")
    code, _, _, child = run_once(setup, env, resolver)
    want = setup["tmp"] / "m2" / "repository-slot3"
    assert code == 0 and child[worker.LOCAL_REPO_ENV] == str(want) and want.is_dir()
    # the resolver sees the configs the child gets, in the child's order (the Worker's paths file last)
    assert resolver.calls == [([str(setup["repo"] / "config" / "artifacts.yml")], setup["run_dir"] / "worker-paths.yml")]
    if local_repo is None:
        assert worker.LOCAL_REPO_ENV not in env
    else:
        assert env[worker.LOCAL_REPO_ENV] == local_repo
    assert slot_events(setup["run_dir"])[0]["local_repo"] == str(want)


def test_a_placeholder_in_the_override_is_left_to_the_resolver(setup):
    """`${HOME}/…` is expanded by the child's load_config; the Worker never uses the unexpanded text as a path."""
    env = {**setup["env"], contract.SLOT_ENV: "2", worker.LOCAL_REPO_ENV: "${HOME}/.m2/repository"}
    resolver = Resolver(base=setup["tmp"] / "home" / ".m2" / "repository")
    _, _, _, child = run_once(setup, env, resolver)
    assert child[worker.LOCAL_REPO_ENV] == str(setup["tmp"] / "home" / ".m2" / "repository-slot2")
    assert len(resolver.calls) == 1


def test_a_relative_base_is_taken_from_the_childs_working_directory(setup):
    env = {**setup["env"], contract.SLOT_ENV: "2", worker.LOCAL_REPO_ENV: "m2/repository"}
    _, _, cwd, child = run_once(setup, env)
    want = setup["repo"] / "m2" / "repository-slot2"
    assert cwd == setup["repo"] and child[worker.LOCAL_REPO_ENV] == str(want) and want.is_dir()


def test_slot_local_repo_shape():
    assert worker.slot_local_repo(Path("/home/sbmig/.m2/repository"), 2) == Path("/home/sbmig/.m2/repository-slot2")
    assert worker.slot_local_repo(Path("/data/m2/repo/"), 3) == Path("/data/m2/repo-slot3")


# ---- 3. fallbacks: a pod-local repository first (never shared), the shared repository only when that fails too

@pytest.mark.parametrize("local_repo", ["null", None])
def test_resolver_failure_falls_back_to_a_pod_local_repository(setup, capsys, pod_tmp, local_repo):
    env = {**setup["env"], contract.SLOT_ENV: "2", "SBMIG_FAIL_JOB_ON_BUILD_FAIL": "1"}
    if local_repo is not None:
        env[worker.LOCAL_REPO_ENV] = local_repo
    resolver = Resolver(exc=RuntimeError("config broken near secret-ish-value-777"))
    code, cmd, _, child = run_once(setup, env, resolver, rc=2)
    want = pod_tmp / "sbmig-m2-slot2"
    assert code == 2                                          # the child's exit code still counts
    assert child[worker.LOCAL_REPO_ENV] == str(want) and want.is_dir()
    assert {k: v for k, v in child.items() if k != worker.LOCAL_REPO_ENV} == \
        {k: v for k, v in worker.child_environ(env).items() if k != worker.LOCAL_REPO_ENV}
    assert env.get(worker.LOCAL_REPO_ENV) == local_repo       # the Worker's own environment is untouched
    assert cmd == expected_cmd(setup)
    assert contract.JobStatus.read(setup["run_dir"]).exit_code == 2
    (ev,) = slot_events(setup["run_dir"])
    assert ev["slot"] == 2 and ev["local_repo"] == str(want) and ev["shared"] is False and ev["pod_local"] is True
    assert ev["note"] == "pod-local repository: the base Maven local repository could not be resolved (RuntimeError)"
    out = capsys.readouterr().out
    assert "[worker] WARNING: job slot 2: pod-local repository: the base Maven local repository could not be resolved " \
           f"(RuntimeError); the run uses the pod-local Maven local repository {want} and downloads all its dependencies " \
           "again" in out
    assert "shared Maven local repository" not in out
    for text in (out, (setup["run_dir"] / "journal.jsonl").read_text(encoding="utf-8")):
        assert "secret-ish-value-777" not in text             # the type name only, never the message


def test_uncreatable_directory_falls_back_to_a_pod_local_repository(setup, capsys, pod_tmp):
    base = setup["tmp"] / "m2" / "repository"
    base.parent.mkdir(parents=True)
    (base.parent / "repository-slot2").write_text("a file in the way", encoding="utf-8")
    env = {**setup["env"], contract.SLOT_ENV: "2", worker.LOCAL_REPO_ENV: str(base)}
    code, _, _, child = run_once(setup, env)
    want = pod_tmp / "sbmig-m2-slot2"
    assert code == 0
    assert child[worker.LOCAL_REPO_ENV] == str(want) and want.is_dir()
    (ev,) = slot_events(setup["run_dir"])
    assert ev["shared"] is False and ev["pod_local"] is True and ev["local_repo"] == str(want) and ev["slot"] == 2
    assert ev["note"].startswith(f"pod-local repository: cannot create {base.parent / 'repository-slot2'}")
    out = capsys.readouterr().out
    assert "WARNING: job slot 2: pod-local repository: cannot create" in out
    assert "PVC sbmig-m2" in out


@pytest.mark.parametrize("raw", ["abc", "0", "-1", "2.0", "1e3"])
def test_invalid_slot_is_treated_as_no_slot(setup, capsys, raw):
    env = {**setup["env"], contract.SLOT_ENV: raw}
    resolver = Resolver(exc=AssertionError("not called"))
    code, cmd, _, child = run_once(setup, env, resolver)
    assert code == 0 and child == worker.child_environ(env) and cmd == expected_cmd(setup)
    assert not resolver.calls
    (ev,) = slot_events(setup["run_dir"])
    assert ev["slot"] is None and ev["local_repo"] is None and ev["shared"] is True
    assert "is not a positive integer — treated as no slot" in ev["note"]
    assert "[worker] WARNING: SBMIG_JOB_SLOT=" in capsys.readouterr().out


def test_slot_number():
    assert worker.slot_number({}) is None
    assert worker.slot_number({contract.SLOT_ENV: " "}) is None
    assert worker.slot_number({contract.SLOT_ENV: "1"}) == 1
    assert worker.slot_number({contract.SLOT_ENV: " 3 "}) == 3
    for bad in ("abc", "0", "-2", "2.5"):
        with pytest.raises(ValueError):
            worker.slot_number({contract.SLOT_ENV: bad})


def test_apply_slot_repo_never_raises(tmp_path):
    """Whatever the resolver raises (a plain Exception subclass), the function answers: the pod-local repository when it
    can be made, else the shared repository."""
    for exc in (OSError("x"), ValueError("x"), KeyError("x"), TypeError("x")):
        child: dict = {}
        data = worker.apply_slot_repo(child, {contract.SLOT_ENV: "2"}, [], tmp_path / "p.yml", cwd=tmp_path,
                                      resolver=Resolver(exc=exc), local_root=tmp_path / "pod")
        assert data["shared"] is False and data["pod_local"] is True and type(exc).__name__ in data["note"]
        assert child == {worker.LOCAL_REPO_ENV: str(tmp_path / "pod" / "sbmig-m2-slot2")}
        blocked = tmp_path / "blocked"
        blocked.write_text("a file, not a folder", encoding="utf-8")
        child = {}
        data = worker.apply_slot_repo(child, {contract.SLOT_ENV: "2"}, [], tmp_path / "p.yml", cwd=tmp_path,
                                      resolver=Resolver(exc=exc), local_root=blocked)
        assert child == {} and data["shared"] is True and type(exc).__name__ in data["note"]
        assert "pod-local fallback: cannot create" in data["note"]
        blocked.unlink()


# ---- 4. the value survives the child's yaml.safe_load (SBMIG_<SECTION>__<KEY> overrides are read as YAML)

@pytest.mark.parametrize("dirname", ["plain", "with space", "colon: here", "hash #here", "&anchor", "%pct", "[x]",
                                     "한글 폴더", "quote\"d", "back\\slash"])
def test_child_value_round_trips_through_yaml(setup, dirname):
    base = setup["tmp"] / dirname / "repository"
    env = {**setup["env"], contract.SLOT_ENV: "2", worker.LOCAL_REPO_ENV: json.dumps(str(base))}
    assert yaml.safe_load(env[worker.LOCAL_REPO_ENV]) == str(base)       # the Worker's own value, quoted as an operator would
    _, _, _, child = run_once(setup, env)
    want = setup["tmp"] / dirname / "repository-slot2"
    value = child[worker.LOCAL_REPO_ENV]
    assert yaml.safe_load(value) == str(want) and want.is_dir()
    if dirname == "plain":
        assert value == str(want)                                         # a plain path goes as it is


@pytest.mark.parametrize("text", ["/home/sbmig/.m2/repository-slot2", "C:\\Users\\sbmig\\.m2\\repository-slot2",
                                  "D:/m2/repository-slot3", "/a: b/r", "/a #b/r", "null", "true", "123", "~", "",
                                  "*alias", "!tag", "@at", "`tick", "/tab\there", "/x\u00e9/r", "- item"])
def test_yaml_env_value(text):
    value = worker.yaml_env_value(text)
    assert yaml.safe_load(value) == text
    if value != text:
        assert value == json.dumps(text, ensure_ascii=False)


# ---- 5. the environment-pin check sees the pod's own environment, not the slot's repository

def _pin(setup, main_env: dict, policy: str = contract.ENV_PIN_REFUSE) -> None:
    pins = contract.env_pins(setup["repo"], ["artifacts.yml"], main_env)
    contract.ControlState(run_id=RUN_ID).transition(contract.RunState.CREATING, job="sbmig-x", env_pins=pins,
                                                    env_pin_policy=policy, slot=2).write(setup["run_dir"])


POD = {"SBMIG_BUILD__MVN": "/opt/maven/bin/mvn", "SBMIG_SCRIPT__TIMEOUT_SECONDS": "300"}


@pytest.mark.parametrize("own_override", [False, True])
def test_pins_pass_with_a_slot(setup, tmp_path, own_override):
    """Both pods carry the same SBMIG_BUILD__LOCAL_REPO (the template's "null", or a path); only the child's copy changes."""
    base = tmp_path / "m2" / "repository"
    local_repo = str(base) if own_override else "null"
    shared = {**POD, worker.LOCAL_REPO_ENV: local_repo}
    _pin(setup, {**shared, "SBMIG_KB__DIR": setup["env"]["SBMIG_KB_DIR"]})      # the web pod has no SBMIG_JOB_SLOT
    env = {**setup["env"], **shared, contract.SLOT_ENV: "2"}
    code, _, _, child = run_once(setup, env, Resolver(base=base))
    assert code == 0
    events = journal(setup["run_dir"])
    assert [e["event"] for e in events] == ["worker_started", worker.SLOT_EVENT, "worker_finished"]
    assert events[0]["env_pin"] == contract.ENV_PIN_REFUSE
    assert child[worker.LOCAL_REPO_ENV] == str(base.parent / "repository-slot2")
    assert env[worker.LOCAL_REPO_ENV] == local_repo


def test_pins_still_refuse_with_a_slot(setup, tmp_path):
    base = tmp_path / "m2" / "repository"
    _pin(setup, {**POD, "SBMIG_SCRIPT__TIMEOUT_SECONDS": "4711", worker.LOCAL_REPO_ENV: "null",
                 "SBMIG_KB__DIR": setup["env"]["SBMIG_KB_DIR"]})
    env = {**setup["env"], **POD, worker.LOCAL_REPO_ENV: "null", contract.SLOT_ENV: "2"}
    calls: list = []
    resolver = Resolver(base=base)
    assert worker.main(environ=env, run_child=fake_child(0, calls), repo_resolver=resolver) == 3
    assert not calls and not resolver.calls
    assert not (base.parent / "repository-slot2").exists()                     # nothing is set up for a refused run
    assert [e["event"] for e in journal(setup["run_dir"])] == ["worker_refused"]
    error = contract.JobStatus.read(setup["run_dir"]).error
    assert error.startswith("environment differs from the web pod's: values differ: SBMIG_SCRIPT__TIMEOUT_SECONDS — ")
    assert worker.LOCAL_REPO_ENV not in error and contract.SLOT_ENV not in error


# ---- 6. the default resolver: the same config files the child loads

@pytest.fixture()
def config_env(monkeypatch):
    # load_config reads SBMIG_* overrides from os.environ and the .env files: neither belongs in this test
    for name in ("SBMIG_BUILD__LOCAL_REPO", "SBMIG_BUILD__SETTINGS_FILE", "SBMIG_BUILD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("sbmig.core.config.load_dotenv_files", lambda *a: [])
    return monkeypatch


def test_default_resolver_reads_build_local_repo(config_env, tmp_path):
    yml = tmp_path / "site.yml"
    yml.write_text(f"build:\n  local_repo: {json.dumps(str(tmp_path / 'r'))}\n", encoding="utf-8")
    paths_yml = worker.write_paths_yml(tmp_path, {})
    configs = [str(REPO_ROOT / "config" / "artifacts.local-e2e.yml"), str(yml)]
    assert worker.default_repo_resolver(configs, paths_yml) == tmp_path / "r"
    # through base_local_repo with the template's literal "null" in the Worker's env and no resolver given
    assert worker.base_local_repo({worker.LOCAL_REPO_ENV: "null"}, configs, paths_yml) == tmp_path / "r"


def test_default_resolver_follows_the_settings_local_repository(config_env, tmp_path):
    """The shipped shape: the pod's SBMIG_BUILD__LOCAL_REPO="null" wipes build.local_repo (in the child too), so the base
    comes from settings.xml <localRepository>."""
    settings = tmp_path / "settings.xml"
    settings.write_text(f"<settings><localRepository>{tmp_path / 'lr'}</localRepository></settings>", encoding="utf-8")
    yml = tmp_path / "site.yml"
    yml.write_text(f"build:\n  settings_file: {json.dumps(str(settings))}\n  local_repo: {json.dumps(str(tmp_path / 'r'))}\n",
                   encoding="utf-8")
    config_env.setenv(worker.LOCAL_REPO_ENV, "null")
    paths_yml = worker.write_paths_yml(tmp_path, {})
    configs = [str(REPO_ROOT / "config" / "artifacts.local-e2e.yml"), str(yml)]
    assert worker.default_repo_resolver(configs, paths_yml) == tmp_path / "lr"
    child: dict = {}
    data = worker.apply_slot_repo(child, {contract.SLOT_ENV: "2", worker.LOCAL_REPO_ENV: "null"}, configs, paths_yml,
                                  cwd=tmp_path)
    assert data == {"slot": 2, "local_repo": str(tmp_path / "lr-slot2"), "shared": False}
    assert child == {worker.LOCAL_REPO_ENV: str(tmp_path / "lr-slot2")} and (tmp_path / "lr-slot2").is_dir()
