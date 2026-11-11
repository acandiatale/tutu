"""2026-11-10 (the user: "일단 굽자" — the company artifacts.yml is baked into the image): what the k8s manifests promise about
configuration. `sbmig-config` carries only settings.xml; the machine paths of a PC-tested yml are overridden by literal
`SBMIG_<SECTION>__<KEY>` env lines in each manifest; every tuning key of `sbmig-env` reaches the Job pod through an explicit
`configMapKeyRef` in the Worker template (it has no `envFrom`); the image build can refuse a context without the company yml.
Review 2026-11-10: the Worker compares its environment with the web pod's (`sbmig.web.contract.env_pins`) — the manifests as
shipped, and with every documented key switched on, must pass that check, and a ConfigMap edit after the web pod started must not."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from sbmig.core import config as config_mod
from sbmig.core.config import PACKAGE_ROOT, load_config
from sbmig.web import contract

K8S = PACKAGE_ROOT / "deploy" / "k8s"
CONFIGMAP = K8S / "20-configmap.yaml"
WEB = K8S / "40-deployment.yaml"
BUILD_JOB = K8S / "80-build-job.template.yaml"
WEB_JOB = K8S / "81-web-job.template.yaml"

# container paths of the image / the volumes: what a PC-tested yml must not decide inside a pod
PATHS = {"SBMIG_BUILD__SETTINGS_FILE": "/etc/sbmig/settings.xml", "SBMIG_BUILD__JAVA_HOME": "/opt/java21",
         "SBMIG_BUILD__MVN": "mvn", "SBMIG_BUILD__LOCAL_REPO": "null", "SBMIG_SCRIPT__PYTHON": "null"}
# sbmig-env keys (active or commented) the Job pod needs: config overrides, the LLM endpoint values, proxies
FORWARDED = re.compile(r"^\s*#?\s*(SBMIG_[A-Z0-9_]+__[A-Z0-9_]+|LLM_[A-Z_]+|[A-Z]+_PROXY):", re.M)
WEB_ONLY = {"LLM_RECORD_DIR"}        # in-process diagnostics only; the Worker template may not carry it
# env pin simulation: config/artifacts.yml is git-ignored, the local e2e config stands in for the baked company file
PIN_CONFIGS = ["artifacts.local-e2e.yml"]
COMMENTED_KEY = re.compile(r"^\s*#\s*([A-Z][A-Z0-9_]+:\s.*)$")      # `# KEY: "value"   # explanation` (YAML once the `#` is gone)
GROUP_HEADER = re.compile(r"^\s*# ── (\d)\.")
NO_DIFF = {"differs": [], "main_only": [], "worker_only": []}


def _docs(path: Path) -> list[dict]:
    return [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]


def _container(path: Path) -> dict:
    doc = next(d for d in _docs(path) if d["kind"] in ("Deployment", "Job"))
    containers = doc["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 1
    return containers[0]


def _literals(path: Path) -> dict[str, str]:
    return {e["name"]: e["value"] for e in _container(path).get("env") or [] if "value" in e}


def _env_doc_text() -> str:
    text = CONFIGMAP.read_text(encoding="utf-8")
    return text[text.index("name: sbmig-env"):]


def test_config_configmap_carries_only_settings_xml():
    cm = next(d for d in _docs(CONFIGMAP) if d["metadata"]["name"] == "sbmig-config")
    assert sorted(cm["data"]) == ["settings.xml"]
    env = next(d for d in _docs(CONFIGMAP) if d["metadata"]["name"] == "sbmig-env")
    assert env["data"]["SBMIG_WEB_CONFIGS"] == "artifacts.yml"      # the baked file, by name under /app/config
    # container paths are per manifest (80 reads sbmig-env through envFrom and has its own kb dir / record dir)
    assert not [k for k in env["data"] if k in {*PATHS, "SBMIG_KB__DIR", "SBMIG_LLM__RECORD_DIR"}]
    assert all(isinstance(v, str) for v in env["data"].values())


@pytest.mark.parametrize("path", [WEB, BUILD_JOB, WEB_JOB], ids=lambda p: p.name)
def test_every_manifest_overrides_the_machine_paths_and_mounts_settings_only(path: Path):
    c = _container(path)
    names = [e["name"] for e in c["env"]]
    assert len(names) == len(set(names)), sorted(n for n in names if names.count(n) > 1)
    lit = _literals(path)
    assert {k: lit.get(k) for k in PATHS} == PATHS
    mounts = {m["mountPath"]: m for m in c["volumeMounts"]}
    assert mounts["/etc/sbmig/settings.xml"]["subPath"] == "settings.xml"
    assert not [p for p in mounts if p.endswith("artifacts.yml")]        # baked, not mounted


def test_web_pod_also_pins_kb_dir_and_neutralizes_a_pc_record_dir():
    lit = _literals(WEB)
    assert lit["SBMIG_KB__DIR"] == "/data/kb-v2"
    assert lit["SBMIG_LLM__RECORD_DIR"] == "${LLM_RECORD_DIR:-}"       # k8s expands only $(VAR): sbmig sees the placeholder


def test_job_templates_leave_kb_dir_and_record_dir_to_their_launchers():
    """80: run-job.sh writes its own paths yml (KB checkout in the workspace) — an env override would beat it. 81: the Worker gets
    the KB through SBMIG_KB_DIR and turns records off unless the switch is set."""
    for path in (BUILD_JOB, WEB_JOB):
        names = {e["name"] for e in _container(path)["env"]}
        assert not ({"SBMIG_KB__DIR", "SBMIG_LLM__RECORD_DIR", "LLM_RECORD_DIR"} & names), path.name
    assert _literals(BUILD_JOB)["SBMIG_CONFIG"] == "/app/config/artifacts.yml"
    assert _literals(WEB_JOB)["SBMIG_KB_DIR"] == "/data/kb-v2"
    assert "SBMIG_CONFIG" not in _literals(WEB_JOB)                      # Main passes it per run
    text = WEB_JOB.read_text(encoding="utf-8")
    assert re.search(r"^\s*# - \{name: SBMIG_WORKER_LLM_RECORDS, value: \"1\"\}\n\s*# - \{name: SBMIG_LLM__RECORD_DIR,", text, re.M)


def test_every_tuning_key_of_sbmig_env_reaches_the_job_pod():
    """The Worker template has no envFrom: a key added to sbmig-env does nothing for a Job pod until the template lists it."""
    keys = set(FORWARDED.findall(_env_doc_text())) - WEB_ONLY
    assert {"LLM_BASE_URL", "LLM_MODEL", "NO_PROXY", "SBMIG_SCRIPT__MAX_LLM_TRIES_PER_CASE", "SBMIG_BUILD__REPOSITORY_URL",
            "SBMIG_LOOP__PHASES__VERIFY__MAX_ITERATIONS"} <= keys
    refs = {}
    for e in _container(WEB_JOB)["env"]:
        ref = (e.get("valueFrom") or {}).get("configMapKeyRef")
        if ref:
            assert ref["name"] == "sbmig-env" and ref["key"] == e["name"] and ref.get("optional") is True, e["name"]
            refs[e["name"]] = ref
    assert sorted(keys - set(refs)) == []
    # and nothing is forwarded that the ConfigMap does not document
    documented = set(re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]+):", _env_doc_text(), re.M))
    assert sorted(set(refs) - documented) == []


def test_every_override_name_in_the_manifests_is_a_config_key(monkeypatch):
    names = set(re.findall(r"SBMIG_[A-Z0-9_]+__[A-Z0-9_]+", _env_doc_text()))
    for path in (WEB, BUILD_JOB, WEB_JOB):
        names |= set(re.findall(r"SBMIG_[A-Z0-9_]+__[A-Z0-9_]+", path.read_text(encoding="utf-8")))
    assert len(names) >= 25
    for key in [k for k in config_mod.os.environ if k.startswith("SBMIG_") and "__" in k]:
        monkeypatch.delenv(key)
    for name in names:
        monkeypatch.setenv(name, "x")
    assert config_mod._warn_unknown_keys(config_mod._env_override_dict(), label="env: ") == []


def test_web_pod_env_beats_a_pc_tested_yml(tmp_path: Path, monkeypatch):
    """The baked artifacts.yml is the file tested on the PC: Windows paths in it must lose to the manifest's env."""
    pc = tmp_path / "pc.yml"
    pc.write_text("build:\n  settings_file: 'C:\\Users\\me\\.m2\\settings.xml'\n  java_home: 'C:/Program Files/Java/jdk-21'\n"
                  "  mvn: 'C:\\tools\\maven\\bin\\mvn.cmd'\n  local_repo: 'D:\\m2\\repository'\n"
                  "script:\n  python: 'C:\\work\\.venv\\Scripts\\python.exe'\n"
                  "kb:\n  dir: '..\\..\\kb-v2'\nllm:\n  record_dir: 'C:\\work\\llm-records'\n", encoding="utf-8")
    for name, value in _literals(WEB).items():
        if name.startswith("SBMIG_") and "__" in name:
            monkeypatch.setenv(name, value)
    monkeypatch.setenv("LLM_RECORD_DIR", "")
    cfg = load_config(PACKAGE_ROOT / "config" / "artifacts.local-e2e.yml", pc)
    assert (cfg.kb.dir, cfg.build.settings_file, cfg.build.java_home, cfg.build.mvn) == \
        ("/data/kb-v2", "/etc/sbmig/settings.xml", "/opt/java21", "mvn")
    assert cfg.build.local_repo is None and cfg.script.python is None and not cfg.llm.record_dir
    monkeypatch.setenv("LLM_RECORD_DIR", "/data/llm-records")
    assert load_config(PACKAGE_ROOT / "config" / "artifacts.local-e2e.yml", pc).llm.record_dir == "/data/llm-records"


def test_the_image_build_can_require_the_company_config():
    text = (PACKAGE_ROOT / "deploy" / "docker" / "Dockerfile").read_text(encoding="utf-8")
    copy_at = text.index("COPY --chown=sbmig:sbmig ${APP_DIR}/ /app/")
    arg_at = text.index("ARG REQUIRE_CONFIG=\n")
    assert arg_at > copy_at and 'test -f "/app/config/$f"' in text[arg_at:] and "exit 1" in text[arg_at:]
    # the company yml is neither excluded from the context nor mistaken for a secret file
    for ignore in (PACKAGE_ROOT / ".dockerignore", PACKAGE_ROOT / "deploy" / "docker" / "root.dockerignore.example"):
        lines = [ln.strip() for ln in ignore.read_text(encoding="utf-8").splitlines()]
        assert not [ln for ln in lines if ln and not ln.startswith(("#", "!")) and "artifacts" in ln], ignore.name


# ── env pin (review 2026-11-10): the web pod's environment vs a Job pod's, simulated from the manifests ──────────────────
def _sbmig_env_data() -> dict[str, str]:
    return dict(next(d for d in _docs(CONFIGMAP) if d["metadata"]["name"] == "sbmig-env")["data"])


def _commented_keys() -> dict[str, tuple[str, str]]:
    """Every commented `# KEY: "value"` line of sbmig-env → (group number, documented value)."""
    out: dict[str, tuple[str, str]] = {}
    group = ""
    for line in _env_doc_text().splitlines():
        header = GROUP_HEADER.match(line)
        if header:
            group = header.group(1)
            continue
        m = COMMENTED_KEY.match(line)
        if m:
            ((name, value),) = yaml.safe_load(m.group(1)).items()
            assert isinstance(value, str), (name, value)        # ConfigMap data values are strings
            out[name] = (group, value)
    return out


def _configmap(all_on: bool) -> dict[str, str]:
    """sbmig-env as shipped, or with every commented key of every group switched on with its documented value."""
    data = _sbmig_env_data()
    if all_on:
        data.update({name: value for name, (_, value) in _commented_keys().items()})
    return data


def _secrets() -> dict[str, str]:
    """Placeholder values for the keys of Secret sbmig-llm (the web pod reads it by envFrom, the Job pod by secretKeyRef)."""
    names = [e["name"] for e in _container(WEB_JOB)["env"] if "secretKeyRef" in (e.get("valueFrom") or {})]
    assert names
    return {n: f"CHANGE_ME_{n}" for n in names}


def _main_env(cm: dict[str, str], secrets: dict[str, str] | None = None) -> dict[str, str]:
    """The web pod's environment: envFrom sbmig-env then the Secret, the Deployment's literal env on top (env beats envFrom)."""
    env = {**cm, **(secrets if secrets is not None else _secrets())}
    env.update(_literals(WEB))
    return env


def _worker_env(cm: dict[str, str], secrets: dict[str, str] | None = None, drop: str | None = None) -> dict[str, str]:
    """A Job pod's environment: the template's literals, each configMapKeyRef whose key the ConfigMap carries, each
    secretKeyRef, plus the per-run values Main adds. `drop` leaves one template entry out (a stale template)."""
    secrets = secrets if secrets is not None else _secrets()
    env = {name: f"<{name.lower()}>" for name in contract.RUN_ENV}
    for e in _container(WEB_JOB)["env"]:
        if e["name"] == drop:
            continue
        ref = e.get("valueFrom") or {}
        if "value" in e:
            env[e["name"]] = e["value"]
        elif "configMapKeyRef" in ref and ref["configMapKeyRef"]["key"] in cm:
            env[e["name"]] = cm[ref["configMapKeyRef"]["key"]]
        elif "secretKeyRef" in ref and ref["secretKeyRef"]["key"] in secrets:
            env[e["name"]] = secrets[ref["secretKeyRef"]["key"]]
    return env


def _pins(env: dict[str, str]) -> dict[str, str]:
    return contract.env_pins(PACKAGE_ROOT, PIN_CONFIGS, env)


def _compare(main_env: dict[str, str], worker_env: dict[str, str]) -> dict[str, list[str]]:
    return contract.compare_env_pins(_pins(main_env), _pins(worker_env))


@pytest.mark.parametrize("all_on", [False, True], ids=["as-shipped", "every-commented-key-on"])
def test_the_shipped_manifests_pass_their_own_env_pin_check(all_on: bool):
    """Main (40 + sbmig-env + Secret) and a Job pod (81 + the same ConfigMap / Secret) pin the same names to the same values.
    With every commented key switched on the web pod gets more variables than the Job pod (group 1, LLM_RECORD_DIR): none of
    them may be pinned, or every upload would be refused."""
    cm = _configmap(all_on)
    main_env, worker_env = _main_env(cm), _worker_env(cm)
    main, worker = _pins(main_env), _pins(worker_env)
    assert contract.compare_env_pins(main, worker) == NO_DIFF
    # the KB dir: an override in the web pod, the alias SBMIG_KB_DIR in the Job pod
    assert "SBMIG_KB__DIR" in main and "SBMIG_KB__DIR" in worker
    assert "SBMIG_KB__DIR" not in worker_env and "SBMIG_KB_DIR" in worker_env
    for pins in (main, worker):
        assert not [n for n in pins if contract.is_secret_name(n)]
        assert not {"LLM_TOKEN_URL", "LLM_API_KEY", "LLM_APP_TOKEN", "NEXUS_PASSWORD"} & set(pins)
        assert not {"LLM_RECORD_DIR", "SBMIG_LLM__RECORD_DIR"} & set(pins)
        assert not {"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"} & set(pins)
        assert all(re.fullmatch(r"[0-9a-f]{16}", v) for v in pins.values())


def test_every_documented_key_switched_on_is_pinned_on_both_sides():
    shipped, all_on = _pins(_main_env(_configmap(False))), _configmap(True)
    main, worker = _pins(_main_env(all_on)), _pins(_worker_env(all_on))
    assert len(main) >= len(shipped) + 20, (len(shipped), len(main))
    commented = _commented_keys()
    overrides = sorted(n for n, (group, _) in commented.items() if group == "3")
    assert len(overrides) >= 20 and all(n.startswith("SBMIG_") and "__" in n for n in overrides)
    expected = {*overrides, "LLM_RECIPE_MODEL", "LLM_RESPONSE_PATH"}        # group 3 + the group 2 placeholders of default.yml
    assert sorted(expected - set(main)) == [] and sorted(expected - set(worker)) == []
    # group 1 is the web pod's own behaviour (and proxies): never pinned
    web_knobs = {n for n, (group, _) in commented.items() if group == "1"}
    assert {"SBMIG_JOB_ENV_PIN", "SBMIG_JOB_EXIT_GRACE_S", "SBMIG_JOB_API_GRACE_S"} <= web_knobs
    assert sorted(web_knobs & set(main)) == []
    assert "SBMIG_WEB_MODE" not in main and "SBMIG_WEB_MODE" not in worker        # shipped switched on (below), still never pinned


def test_the_shipped_configmap_runs_the_company_operating_mode():
    """The company runs one Job per upload: sbmig-env ships with the mode switched on (user, 2026-11-10 — the web pod had come
    up running migrations itself because the line was a commented "later" step). What the mode needs at startup is in the
    same set of manifests: the template mount and its path, and the company's way to the Kubernetes API — the infra team's
    kubeconfig Secret at /etc/kube/config (same day: those three places had shipped commented too), with the namespace named
    in sbmig-env because a kubeconfig context without one means `default`. The service account of 45 is the commented alternative."""
    assert _sbmig_env_data().get("SBMIG_WEB_MODE") == "k8s-job"
    assert "SBMIG_WEB_MODE" not in _commented_keys()
    spec = next(d for d in _docs(WEB) if d["kind"] == "Deployment")["spec"]["template"]["spec"]
    assert "serviceAccountName" not in spec
    assert "# serviceAccountName: sbmig-web" in WEB.read_text(encoding="utf-8")
    env = {e["name"]: e.get("value") for e in spec["containers"][0]["env"]}
    mounts = {m["name"]: m["mountPath"] for m in spec["containers"][0]["volumeMounts"]}
    assert env["KUBECONFIG"] == mounts["kubeconfig"] + "/config" == "/etc/kube/config"
    secret = next(v for v in spec["volumes"] if v["name"] == "kubeconfig")["secret"]
    assert secret["secretName"] == "agent-kubeconfig" and secret["defaultMode"] == 0o400
    assert spec["securityContext"]["fsGroup"] == spec["securityContext"]["runAsGroup"]     # how uid 1000 reads a 0400 Secret file
    assert _sbmig_env_data().get("SBMIG_K8S_NAMESPACE") == "agent" and "SBMIG_K8S_NAMESPACE" not in _commented_keys()
    # the Job pod calls no Kubernetes API: no kubeconfig, no service account in the template
    assert "kubeconfig" not in WEB_JOB.read_text(encoding="utf-8").lower() and "KUBECONFIG" not in _literals(WEB_JOB)
    assert env["SBMIG_JOB_TEMPLATE"].startswith(mounts["job-template"] + "/")
    volume = next(v for v in spec["volumes"] if v["name"] == "job-template")
    assert volume["configMap"]["name"] == "sbmig-job-template"


def test_the_shipped_configmap_runs_three_build_pods_at_a_time():
    """The user gave the number (2026-11-10: "3명까지는 빌드파드가 돌아가는 구조"): it ships switched on, in the web pod's group,
    and is the web pod's own knob — never pinned, never forwarded to the Job pod (the Job gets its slot number from Main).
    Every slot's Maven repository lives inside the one `sbmig-m2` volume both manifests mount at the same path, and the
    template leaves `SBMIG_BUILD__LOCAL_REPO` unset so the Worker derives the slot's sibling directory itself."""
    data = _sbmig_env_data()
    assert data.get(contract.MAX_PARALLEL_ENV) == "3" and contract.MAX_PARALLEL_ENV not in _commented_keys()
    assert contract.MAX_PARALLEL_ENV not in _pins(_main_env(_configmap(False)))
    names = {e["name"] for e in _container(WEB_JOB)["env"]}
    assert contract.MAX_PARALLEL_ENV not in names
    assert contract.SLOT_ENV in contract.RUN_ENV and contract.SLOT_ENV not in _literals(WEB_JOB)   # Main adds it per Job
    assert _literals(WEB_JOB)["SBMIG_BUILD__LOCAL_REPO"] == "null"
    web = next(d for d in _docs(WEB) if d["kind"] == "Deployment")["spec"]["template"]["spec"]
    job = _docs(WEB_JOB)[0]["spec"]["template"]["spec"]

    def m2(spec: dict) -> tuple[str, str]:
        mount = next(m for m in spec["containers"][0]["volumeMounts"] if m["mountPath"].endswith("/.m2"))
        volume = next(v for v in spec["volumes"] if v["name"] == mount["name"])
        return mount["mountPath"], volume["persistentVolumeClaim"]["claimName"]

    assert m2(web) == m2(job) == ("/home/sbmig/.m2", "sbmig-m2")


def test_a_job_pod_without_the_kb_dir_is_named():
    cm = _configmap(False)
    worker_env = _worker_env(cm)
    del worker_env["SBMIG_KB_DIR"]
    assert _compare(_main_env(cm), worker_env) == {**NO_DIFF, "main_only": ["SBMIG_KB__DIR"]}


@pytest.mark.parametrize(("all_on", "key"), [(False, "LLM_MODEL"), (True, "SBMIG_LOOP__MAX_TOTAL_MINUTES"),
                                             (True, "LLM_RECIPE_MODEL")])
def test_a_configmap_value_changed_after_the_web_pod_started_differs(all_on: bool, key: str):
    """The web pod read sbmig-env at its start; the Job pod reads the edited ConfigMap when it is created."""
    cm = _configmap(all_on)
    edited = {**cm, key: cm[key] + "-edited"}
    assert _compare(_main_env(cm), _worker_env(edited)) == {**NO_DIFF, "differs": [key]}


def test_a_key_switched_on_after_the_web_pod_started_is_worker_only():
    cm = _configmap(False)
    assert "SBMIG_LOOP__STALL_LIMIT" not in cm
    edited = {**cm, "SBMIG_LOOP__STALL_LIMIT": "3"}
    assert _compare(_main_env(cm), _worker_env(edited)) == {**NO_DIFF, "worker_only": ["SBMIG_LOOP__STALL_LIMIT"]}


@pytest.mark.parametrize(("all_on", "key"), [(False, "LLM_ESCALATION_MODEL"), (True, "SBMIG_SCRIPT__MAX_LLM_TRIES_PER_CASE"),
                                             (True, "LLM_RECIPE_MODEL")])
def test_a_key_the_job_template_does_not_forward_is_main_only(all_on: bool, key: str):
    """A key added to sbmig-env without its configMapKeyRef line in 81 (or a stale sbmig-job-template ConfigMap)."""
    cm = _configmap(all_on)
    assert _compare(_main_env(cm), _worker_env(cm, drop=key)) == {**NO_DIFF, "main_only": [key]}


def test_secrets_are_never_compared():
    """By design: no fingerprint of a credential on the shared volume — a wrong credential fails at the gateway instead."""
    cm = _configmap(False)
    other = {n: v + "-rotated" for n, v in _secrets().items()}
    assert _compare(_main_env(cm), _worker_env(cm, secrets=other)) == NO_DIFF


def test_sbmig_env_header_tells_the_operator_to_restart_the_web_pod():
    text = CONFIGMAP.read_text(encoding="utf-8")
    header = text[text.index("\n---\n"):text.index("name: sbmig-env")]
    assert "rollout restart" in header and "SBMIG_JOB_ENV_PIN" in header
    assert "kubectl -n agent rollout restart deploy/sbmig" in header
    assert "sbmig-job-template" in header                     # a changed Job template needs the same restart
    commented = _commented_keys()
    assert commented["SBMIG_JOB_ENV_PIN"] == ("1", contract.ENV_PIN_REFUSE)


IVAS_KEYS = ("IVAS_BASE_URL", "IVAS_USER_ID", "IVAS_PASSWORD")
AFTER_MIGRATION = "SBMIG_REMEDIATION__AFTER_MIGRATION"


def test_the_remediation_runs_in_the_build_pod_switched_on():
    """User, 2026-11-11: the remediation runs in the BUILD POD, chained after a migration that ended BUILD SUCCESS. The switch
    ships on in sbmig-env (one place: the web pod reads it by envFrom, the 81 Job pod by its configMapKeyRef line, the 80 Job
    by envFrom) — the same value on both sides of the env pin check, and the loaded config says true in both pods."""
    data = _sbmig_env_data()
    assert data.get(AFTER_MIGRATION) == "true" and AFTER_MIGRATION not in _commented_keys()
    assert not [n for n in (WEB, WEB_JOB, BUILD_JOB) if AFTER_MIGRATION in _literals(n)]     # no second, diverging source
    cm = _configmap(False)
    main_env, worker_env = _main_env(cm), _worker_env(cm)
    assert main_env[AFTER_MIGRATION] == worker_env[AFTER_MIGRATION] == "true"
    main, worker = _pins(main_env), _pins(worker_env)
    assert AFTER_MIGRATION in main and AFTER_MIGRATION in worker and contract.compare_env_pins(main, worker) == NO_DIFF
    # the knobs an operator may want for the build pod are documented next to it and forwarded by 81
    commented = _commented_keys()
    for key in ("SBMIG_REMEDIATION__REQUIRE_APPROVAL", "SBMIG_REMEDIATION__FAILED_CANDIDATE",
                "SBMIG_REMEDIATION__IVAS_ANALYSIS_TIMEOUT_MINUTES"):
        assert commented[key][0] == "3", key
    for name in (WEB, BUILD_JOB):
        refs = next(d for d in _docs(name) if d["kind"] in ("Deployment", "Job"))["spec"]["template"]["spec"]
        assert {"configMapRef": {"name": "sbmig-env"}} in refs["containers"][0]["envFrom"], name.name
    # a ConfigMap value turned off after the web pod started is caught like any other key
    off = {**cm, AFTER_MIGRATION: "false"}
    assert _compare(_main_env(cm), _worker_env(off)) == {**NO_DIFF, "differs": [AFTER_MIGRATION]}


def test_the_switch_loads_as_true_in_the_job_pod(monkeypatch):
    for key in [k for k in config_mod.os.environ if k.startswith("SBMIG_") and "__" in k]:
        monkeypatch.delenv(key)
    for name, value in _worker_env(_configmap(False)).items():
        if name.startswith("SBMIG_") and "__" in name:
            monkeypatch.setenv(name, value)
    assert load_config(PACKAGE_ROOT / "config" / "artifacts.local-e2e.yml").remediation.after_migration is True


def test_the_job_pod_gets_the_ivas_values_optionally_from_the_llm_secret():
    """81 reads the three IVAS keys from the same Secret as its other secrets, optional (a cluster without IVAS still
    migrates — the remediation is skipped); 80 gets them through its envFrom of that Secret. Never pinned: rotating a value
    (or the host) after the web pod started does not stop a Job."""
    refs = {e["name"]: e["valueFrom"]["secretKeyRef"] for e in _container(WEB_JOB)["env"]
            if "secretKeyRef" in (e.get("valueFrom") or {})}
    other = {r["name"] for n, r in refs.items() if n not in IVAS_KEYS}
    assert other == {"sbmig-llm"}
    for key in IVAS_KEYS:
        assert refs[key] == {"name": "sbmig-llm", "key": key, "optional": True}, key
    assert not [k for k in IVAS_KEYS if k in _literals(WEB_JOB) or k in _sbmig_env_data() or k in _commented_keys()]
    job = next(d for d in _docs(BUILD_JOB) if d["kind"] == "Job")["spec"]["template"]["spec"]["containers"][0]
    assert {"secretRef": {"name": "sbmig-llm"}} in job["envFrom"]
    secret_example = next(d for d in _docs(K8S / "30-secret.example.yaml") if d["metadata"]["name"] == "sbmig-llm")
    assert set(IVAS_KEYS) <= set(secret_example["stringData"])
    cm = _configmap(True)
    main, worker = _pins(_main_env(cm)), _pins(_worker_env(cm))
    assert not [n for n in {*main, *worker} if n.startswith("IVAS_")]
    rotated = {**_secrets(), "IVAS_BASE_URL": "https://other.invalid", "IVAS_USER_ID": "someone-else"}
    assert _compare(_main_env(cm), _worker_env(cm, secrets=rotated)) == NO_DIFF


def test_no_override_in_sbmig_env_is_left_empty():
    """`SBMIG_X__Y: ""` is read as YAML null and replaces the yml value with nothing: a key is commented out, never emptied."""
    data = _sbmig_env_data()
    assert not [k for k, v in data.items() if k.startswith("SBMIG_") and "__" in k and not v.strip()]
    assert not [n for n, (_, v) in _commented_keys().items() if n.startswith("SBMIG_") and "__" in n and not v.strip()]
