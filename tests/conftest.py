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
