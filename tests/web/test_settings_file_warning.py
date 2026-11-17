"""v2.1z-al: the web pod warns once at startup when the default configs name a `build.settings_file` that is not there,
or that is an empty file (in the cluster `/data/.m2/settings.xml` is a plain file the operator copies onto the data
volume). Never fatal."""
from __future__ import annotations

import logging

import pytest

from sbmig.core.config import PACKAGE_ROOT
from sbmig.web.runs import RunManager

CONFIGS = ["artifacts.local-e2e.yml"]


@pytest.fixture()
def clean_env(monkeypatch):
    monkeypatch.setattr("sbmig.core.config.load_dotenv_files", lambda *a: [])
    monkeypatch.delenv("SBMIG_WEB_MODE", raising=False)
    return monkeypatch


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelname == "WARNING" and "Maven settings file" in r.getMessage()]


def test_a_missing_settings_file_is_one_warning_not_a_failure(tmp_path, clean_env, caplog):
    missing = tmp_path / "m2" / "settings.xml"
    clean_env.setenv("SBMIG_BUILD__SETTINGS_FILE", str(missing))
    with caplog.at_level(logging.WARNING, logger="sbmig"):
        RunManager(tmp_path / "out", PACKAGE_ROOT, default_configs=CONFIGS, mode="in-process")
    assert _warnings(caplog) == [f"Maven settings file {missing} is missing — copy settings.xml there before the first "
                                 "run (deploy/README.md §3)"]


def test_an_empty_settings_file_is_one_warning(tmp_path, clean_env, caplog):
    empty = tmp_path / "m2" / "settings.xml"
    empty.parent.mkdir()
    empty.write_bytes(b"")
    clean_env.setenv("SBMIG_BUILD__SETTINGS_FILE", str(empty))
    with caplog.at_level(logging.WARNING, logger="sbmig"):
        RunManager(tmp_path / "out", PACKAGE_ROOT, default_configs=CONFIGS, mode="in-process")
    assert _warnings(caplog) == [f"Maven settings file {empty} is empty — copy settings.xml there before the first run "
                                 "(deploy/README.md §3)"]


def test_a_present_or_unset_settings_file_says_nothing(tmp_path, clean_env, caplog):
    f = tmp_path / "settings.xml"
    f.write_text("<settings/>\n", encoding="utf-8")
    clean_env.setenv("SBMIG_BUILD__SETTINGS_FILE", str(f))
    with caplog.at_level(logging.WARNING, logger="sbmig"):
        RunManager(tmp_path / "out", PACKAGE_ROOT, default_configs=CONFIGS, mode="in-process")
        clean_env.delenv("SBMIG_BUILD__SETTINGS_FILE")
        RunManager(tmp_path / "out2", PACKAGE_ROOT, default_configs=CONFIGS, mode="in-process")
    assert _warnings(caplog) == []
