"""Fixtures for fleet tests: tiny projects on disk + synthetic RunReports (no Maven, no LLM)."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from sbmig.core.config import Config


def make_project(root: Path, name: str) -> Path:
    p = root / name
    p.mkdir(parents=True, exist_ok=True)
    (p / "pom.xml").write_text(f"<project><artifactId>{name}</artifactId></project>\n")
    return p


def make_zip(root: Path, name: str) -> Path:
    z = root / f"{name}.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr(f"{name}/pom.xml", f"<project><artifactId>{name}</artifactId></project>\n")
    return z


@pytest.fixture()
def fleet_cfg(cfg: Config, tmp_path: Path) -> Config:
    return cfg.model_copy(update={"kb": cfg.kb.model_copy(update={"dir": str(tmp_path / "kb")})})


@pytest.fixture()
def projects_dir(tmp_path: Path) -> Path:
    d = tmp_path / "projects"
    for n in ("alpha", "beta", "gamma"):
        make_project(d, n)
    return d
