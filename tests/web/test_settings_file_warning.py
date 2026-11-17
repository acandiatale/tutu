"""v2.1z-al: at startup the web pod copies the seed `SBMIG_SETTINGS_SEED` (ConfigMap `sbmig-config`, mounted read-only)
to `build.settings_file` (`/data/.m2/settings.xml` on the shared volume, which the Job pods read), and warns once when that
file is still not there. Never fatal."""
from __future__ import annotations

import logging
import os

import pytest

from sbmig.core.config import PACKAGE_ROOT
from sbmig.web import runs
from sbmig.web.runs import RunManager, seed_settings_file

CONFIGS = ["artifacts.local-e2e.yml"]


@pytest.fixture()
def clean_env(monkeypatch):
    monkeypatch.setattr("sbmig.core.config.load_dotenv_files", lambda *a: [])
    monkeypatch.delenv("SBMIG_WEB_MODE", raising=False)
    monkeypatch.delenv("SBMIG_SETTINGS_SEED", raising=False)
    return monkeypatch


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelname == "WARNING" and "Maven settings file" in r.getMessage()]


def test_a_missing_settings_file_is_one_warning_not_a_failure(tmp_path, clean_env, caplog):
    missing = tmp_path / "m2" / "settings.xml"
    clean_env.setenv("SBMIG_BUILD__SETTINGS_FILE", str(missing))
    with caplog.at_level(logging.WARNING, logger="sbmig"):
        RunManager(tmp_path / "out", PACKAGE_ROOT, default_configs=CONFIGS, mode="in-process")
    assert _warnings(caplog) == [f"Maven settings file {missing} is missing — copy settings.xml there before the first run"]


def test_a_present_or_unset_settings_file_says_nothing(tmp_path, clean_env, caplog):
    f = tmp_path / "settings.xml"
    f.write_text("<settings/>\n", encoding="utf-8")
    clean_env.setenv("SBMIG_BUILD__SETTINGS_FILE", str(f))
    with caplog.at_level(logging.WARNING, logger="sbmig"):
        RunManager(tmp_path / "out", PACKAGE_ROOT, default_configs=CONFIGS, mode="in-process")
        clean_env.delenv("SBMIG_BUILD__SETTINGS_FILE")
        RunManager(tmp_path / "out2", PACKAGE_ROOT, default_configs=CONFIGS, mode="in-process")
    assert _warnings(caplog) == []


# ---- the seed copy
SEED = b"<settings><mirrors/></settings>\n"


def _seed(tmp_path, data: bytes = SEED):
    seed = tmp_path / "etc" / "settings.xml"
    seed.parent.mkdir(parents=True, exist_ok=True)
    seed.write_bytes(data)
    return seed


def test_the_seed_is_written_into_a_new_folder(tmp_path, caplog):
    seed, target = _seed(tmp_path), tmp_path / "data" / ".m2" / "settings.xml"
    with caplog.at_level(logging.DEBUG, logger="sbmig"):
        assert seed_settings_file(seed, target) == "written"
    assert target.read_bytes() == SEED
    assert sorted(os.listdir(target.parent)) == ["settings.xml"]           # no temp file left behind
    assert [r.getMessage() for r in caplog.records if r.levelname == "INFO"] == \
        [f"Maven settings {target} written from {seed}"]


def test_a_different_file_is_replaced_atomically(tmp_path, monkeypatch):
    seed, target = _seed(tmp_path), tmp_path / "m2" / "settings.xml"
    target.parent.mkdir()
    target.write_bytes(b"<settings>old</settings>\n")
    replaced: list = []
    real = os.replace
    monkeypatch.setattr(runs.os, "replace", lambda a, b: replaced.append((str(a), str(b))) or real(a, b))
    assert seed_settings_file(seed, target) == "written"
    assert target.read_bytes() == SEED
    (src, dst), = replaced
    assert dst == str(target) and os.path.dirname(src) == str(target.parent) and src != dst   # a temp file beside it
    assert sorted(os.listdir(target.parent)) == ["settings.xml"]


def test_the_same_content_is_not_written(tmp_path, caplog, monkeypatch):
    seed, target = _seed(tmp_path), tmp_path / "m2" / "settings.xml"
    target.parent.mkdir()
    target.write_bytes(SEED)
    monkeypatch.setattr(runs.os, "replace", lambda *a: pytest.fail("an unchanged file was written"))
    with caplog.at_level(logging.DEBUG, logger="sbmig"):
        assert seed_settings_file(seed, target) == "unchanged"
    assert not [r for r in caplog.records if r.levelno >= logging.INFO]
    assert [r for r in caplog.records if r.levelname == "DEBUG" and "unchanged" in r.getMessage()]


def test_a_missing_seed_is_one_warning(tmp_path, caplog):
    seed, target = tmp_path / "nowhere.xml", tmp_path / "m2" / "settings.xml"
    with caplog.at_level(logging.WARNING, logger="sbmig"):
        assert seed_settings_file(seed, target) == "failed"
    (msg,) = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert str(seed) in msg and str(target) in msg and not target.exists()


def test_a_failed_copy_is_one_warning_and_leaves_nothing(tmp_path, caplog, monkeypatch):
    seed, target = _seed(tmp_path), tmp_path / "m2" / "settings.xml"

    def deny(*a):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(runs.os, "replace", deny)
    with caplog.at_level(logging.WARNING, logger="sbmig"):
        assert seed_settings_file(seed, target) == "failed"
    (msg,) = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert str(seed) in msg and str(target) in msg and "Permission denied" in msg
    assert os.listdir(target.parent) == []                                  # the temp file was removed


def test_web_start_copies_the_seed_before_the_missing_file_check(tmp_path, clean_env, caplog):
    seed, target = _seed(tmp_path), tmp_path / "data" / ".m2" / "settings.xml"
    clean_env.setenv("SBMIG_BUILD__SETTINGS_FILE", str(target))
    clean_env.setenv("SBMIG_SETTINGS_SEED", str(seed))
    with caplog.at_level(logging.INFO, logger="sbmig"):
        RunManager(tmp_path / "out", PACKAGE_ROOT, default_configs=CONFIGS, mode="in-process")
    assert target.read_bytes() == SEED and _warnings(caplog) == []
    assert f"Maven settings {target} written from {seed}" in [r.getMessage() for r in caplog.records]


def test_web_start_without_the_variable_copies_nothing(tmp_path, clean_env, caplog):
    _seed(tmp_path)
    target = tmp_path / "data" / ".m2" / "settings.xml"
    clean_env.setenv("SBMIG_BUILD__SETTINGS_FILE", str(target))
    with caplog.at_level(logging.WARNING, logger="sbmig"):
        RunManager(tmp_path / "out", PACKAGE_ROOT, default_configs=CONFIGS, mode="in-process")
    assert not target.parent.exists() and len(_warnings(caplog)) == 1


def test_a_missing_seed_at_web_start_keeps_the_missing_file_warning(tmp_path, clean_env, caplog):
    target = tmp_path / "data" / ".m2" / "settings.xml"
    clean_env.setenv("SBMIG_BUILD__SETTINGS_FILE", str(target))
    clean_env.setenv("SBMIG_SETTINGS_SEED", str(tmp_path / "no-seed.xml"))
    with caplog.at_level(logging.WARNING, logger="sbmig"):
        RunManager(tmp_path / "out", PACKAGE_ROOT, default_configs=CONFIGS, mode="in-process")
    texts = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("no-seed.xml" in t for t in texts) and len(_warnings(caplog)) == 1
