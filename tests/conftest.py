"""Shared pytest fixtures."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from sbmig.core.config import Config, load_config
from sbmig.core.workspace import Workspace

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "fixtures" / "projects"


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture()
def cfg() -> Config:
    """Config with the public-artifact e2e policy and the fake LLM."""
    return load_config(REPO_ROOT / "config" / "artifacts.local-e2e.yml", repo_root=REPO_ROOT)


@pytest.fixture()
def make_ws(tmp_path: Path):
    """Factory: make_ws(src_dir_or_zip) -> Workspace under tmp_path/out."""
    def _make(src: Path, run_id: str = "test-run") -> Workspace:
        return Workspace.from_input(src, tmp_path / "out", run_id=run_id)
    return _make


@pytest.fixture()
def fixture_project(tmp_path: Path):
    """Factory: fixture_project("boot2") -> Path copy of fixtures/projects/boot2 inside tmp_path."""
    def _copy(name: str) -> Path:
        src = FIXTURES / name
        if not src.exists():
            pytest.skip(f"fixture project {name} not available yet")
        dst = tmp_path / "src" / name
        shutil.copytree(src, dst)
        return dst
    return _copy


@pytest.fixture(autouse=True)
def no_llm_cooldown_sleep(monkeypatch):
    """Round 9: the LLM cool-down (`llm.cooldown_seconds`, default 60 s) never really sleeps in any test; the pauses
    asked for are recorded in the returned list (loop tests assert on it)."""
    slept: list[float] = []
    monkeypatch.setattr("sbmig.loop.fixloop.sleep_seconds", slept.append)
    return slept


class OfflineRepositoryLookup:
    """v2.1d: no test asks a real repository (Maven Central / Nexus) which releases exist — every lookup answers
    "offline". Tests that need facts monkeypatch their own fake; `tests/build/test_repo_versions.py` imports the real
    class at module import time and drives it with a fake fetch, so this patch does not reach it."""

    def __init__(self, cfg=None, *, fetch=None):
        self.cfg = cfg
        self.asked: list[tuple[str, object]] = []

    def repository_url(self):
        return None

    def versions(self, group_id, artifact_id):
        return []

    def pom_text(self, group_id, artifact_id, version):
        return None

    def candidates(self, key, current):
        from sbmig.core.models import RepositoryVersions
        self.asked.append((key, current))
        return RepositoryVersions(key=key, current=current, error="offline (tests)")


@pytest.fixture(autouse=True)
def _offline_repository_lookup(monkeypatch):
    monkeypatch.setattr("sbmig.build.repo_versions.RepositoryLookup", OfflineRepositoryLookup)
    return OfflineRepositoryLookup
