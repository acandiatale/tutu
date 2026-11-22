"""v2.1z-br: deploy/ENV.md names every environment variable the manifests under deploy/k8s set — as an `env` entry, a
ConfigMap / Secret key, or a commented line an operator may switch on."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

K8S = Path(__file__).resolve().parents[2] / "deploy" / "k8s"
ENV_DOC = K8S.parent / "ENV.md"
NOT_A_VARIABLE = {"settings.xml"}          # the ConfigMap sbmig-config key: a file, listed in ENV.md §7 as such


def _names_set_by_manifests() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}

    def walk(node: object, where: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "env" and isinstance(value, list):
                    for entry in value:
                        if isinstance(entry, dict) and "name" in entry:
                            found.setdefault(entry["name"], set()).add(where)
                elif key in ("data", "stringData") and isinstance(value, dict):
                    for name in value:
                        found.setdefault(name, set()).add(f"{where} {key}")
                else:
                    walk(value, where)
        elif isinstance(node, list):
            for item in node:
                walk(item, where)

    for manifest in sorted(K8S.glob("*.yaml")):
        text = manifest.read_text(encoding="utf-8")
        for doc in yaml.safe_load_all(text):
            walk(doc, manifest.name)
        for m in re.finditer(r"#\s*-?\s*\{?\s*name:\s*([A-Z][A-Z0-9_]+)", text):
            found.setdefault(m.group(1), set()).add(f"{manifest.name} (commented env)")
        for m in re.finditer(r"^\s*#\s*([A-Z][A-Z0-9_]+):\s", text, re.M):
            found.setdefault(m.group(1), set()).add(f"{manifest.name} (commented key)")
    return {n: w for n, w in found.items() if n not in NOT_A_VARIABLE}


def test_the_scan_sees_the_kinds_of_entries_it_must_see():
    names = _names_set_by_manifests()
    assert "SBMIG_BUILD__JAVA_HOME" in names          # a container env line
    assert "SBMIG_WEB_MODE" in names                  # a ConfigMap key
    assert "LLM_APP_TOKEN" in names                   # a Secret key
    assert "SBMIG_SOURCE_ZIP_URL" in names            # a commented env line
    assert "SBMIG_JOB_ENV_PIN" in names               # a commented ConfigMap key


def test_env_md_names_every_variable_the_manifests_set():
    doc = ENV_DOC.read_text(encoding="utf-8")
    missing = {n: sorted(w) for n, w in _names_set_by_manifests().items() if not re.search(rf"`{re.escape(n)}`", doc)}
    assert not missing, missing


def test_env_md_names_every_variable_the_web_pod_sets_per_job():
    """The web pod adds these to each Job it creates (`sbmig.web.contract.RUN_ENV`, built in `RunManager._job_env`);
    they appear in no manifest as entries, so the manifest scan above cannot see them."""
    from sbmig.web.contract import RUN_ENV

    doc = ENV_DOC.read_text(encoding="utf-8")
    assert len(RUN_ENV) >= 10
    missing = [n for n in RUN_ENV if not re.search(rf"`{re.escape(n)}`", doc)]
    assert not missing, missing
