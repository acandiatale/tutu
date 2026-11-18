"""S3 (review 2026-11-18): an explicit command-line option (`sbmig run --settings`, `--java-home`, `--llm-provider` …,
i.e. `load_config(overrides=…)`) and a fleet manifest's per-project override win over an `SBMIG_<SECTION>__<KEY>`
environment value; one INFO line says so. Environment values still win over the yml files. The Worker's own child
environment (the slot's Maven repository) is still what the child gets."""
from __future__ import annotations

import logging
from pathlib import Path

from sbmig.core.config import PACKAGE_ROOT, load_config

E2E = PACKAGE_ROOT / "config" / "artifacts.local-e2e.yml"


class _Catch(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def _catch():
    h = _Catch()
    logging.getLogger("sbmig.config").addHandler(h)
    logging.getLogger("sbmig.config").setLevel(logging.INFO)
    return h


def test_an_explicit_override_beats_the_environment_and_says_so(monkeypatch):
    monkeypatch.setenv("SBMIG_BUILD__SETTINGS_FILE", "/env/settings.xml")
    h = _catch()
    try:
        cfg = load_config(E2E, overrides={"build": {"settings_file": "/cli/settings.xml", "java_home": "/cli/jdk"}})
    finally:
        logging.getLogger("sbmig.config").removeHandler(h)
    assert cfg.build.settings_file == "/cli/settings.xml" and cfg.build.java_home == "/cli/jdk"
    said = [ln for ln in h.lines if "SBMIG_BUILD__SETTINGS_FILE" in ln]
    assert said == ["config: build.settings_file given explicitly replaces SBMIG_BUILD__SETTINGS_FILE from the environment"]
    assert not any("JAVA_HOME" in ln for ln in h.lines)              # nothing replaced there: no line


def test_the_environment_still_beats_the_yml_files(monkeypatch, tmp_path: Path):
    yml = tmp_path / "site.yml"
    yml.write_text("build:\n  settings_file: /yml/settings.xml\n  timeout_minutes: 7\n", encoding="utf-8")
    monkeypatch.setenv("SBMIG_BUILD__SETTINGS_FILE", "/env/settings.xml")
    cfg = load_config(E2E, yml)
    assert cfg.build.settings_file == "/env/settings.xml" and cfg.build.timeout_minutes == 7


def test_without_an_explicit_value_the_environment_value_stays(monkeypatch):
    monkeypatch.setenv("SBMIG_BUILD__LOCAL_REPO", "/data/.m2/repository-slot2")
    cfg = load_config(E2E, overrides={"llm": {"provider": "fake"}})
    assert str(cfg.build.local_repo) == "/data/.m2/repository-slot2" and cfg.llm.provider == "fake"


def test_the_cli_settings_option_wins_over_the_env(monkeypatch, tmp_path: Path):
    """The documented PC `.env` recipe sets SBMIG_BUILD__SETTINGS_FILE; `sbmig run --settings` must still win."""
    from typer.testing import CliRunner

    import sbmig.cli as cli
    seen: dict = {}

    def fake_load(configs, overrides=None, repo_root=None):
        cfg = load_config(*(configs or []), overrides=overrides or None, repo_root=repo_root)
        seen["settings"] = cfg.build.settings_file
        raise SystemExit(0)
    monkeypatch.setattr(cli, "_load", fake_load)
    monkeypatch.setenv("SBMIG_BUILD__SETTINGS_FILE", "/env/settings.xml")
    CliRunner().invoke(cli.app, ["run", str(tmp_path), "--config", str(E2E), "--settings", "/cli/settings.xml",
                                 "--out", str(tmp_path / "out")])
    assert seen["settings"] == "/cli/settings.xml"
