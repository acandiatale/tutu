"""2026-11-10 (the user: "일단 굽자" — the company artifacts.yml is baked into the image): what the k8s manifests promise about
configuration. `sbmig-config` carries only settings.xml; the machine paths of a PC-tested yml are overridden by literal
`SBMIG_<SECTION>__<KEY>` env lines in each manifest; every tuning key of `sbmig-env` reaches the Job pod through an explicit
`configMapKeyRef` in the Worker template (it has no `envFrom`); the image build can refuse a context without the company yml."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from sbmig.core import config as config_mod
from sbmig.core.config import PACKAGE_ROOT, load_config

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
