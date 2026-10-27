"""Fixtures for KB tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.core.config import Config
from sbmig.core.models import CaseType, ProjectProfile
from sbmig.core.workspace import Workspace
from sbmig.kb import FileKnowledgeBase

from .helpers import SECURITY_SRC


@pytest.fixture()
def kb_cfg(cfg: Config, tmp_path: Path) -> Config:
    """A temp KB (``<tmp>/kb``) with the config defaults (v2)."""
    return cfg.model_copy(update={"kb": cfg.kb.model_copy(update={"dir": str(tmp_path / "kb")})})


@pytest.fixture()
def v2_kb_cfg(kb_cfg: Config) -> Config:
    """The same temp KB (the name the v2 script tests use)."""
    return kb_cfg


@pytest.fixture()
def kb(kb_cfg: Config) -> FileKnowledgeBase:
    return FileKnowledgeBase(kb_cfg)


@pytest.fixture()
def ws(tmp_path: Path) -> Workspace:
    root = tmp_path / "proj"
    (root / "src/main/java/com/example/config").mkdir(parents=True)
    (root / "src/main/java/com/example/config/SecurityConfig.java").write_text(SECURITY_SRC)
    (root / "pom.xml").write_text("<project><artifactId>demo</artifactId></project>\n")
    return Workspace(root, tmp_path / "run", "t-run")


@pytest.fixture()
def profile(ws: Workspace) -> ProjectProfile:
    return ProjectProfile(root=str(ws.root), name="demo", case_type=CaseType.SPRING_BOOT, fingerprint="fp-demo")
