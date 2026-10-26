"""Fixtures for KB tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.core.config import Config
from sbmig.core.models import CaseType, ProjectProfile
from sbmig.core.workspace import Workspace
from sbmig.kb import FileKnowledgeBase

from .helpers import SECURITY_SRC, V1_KB_FLAGS


# v2 (flow-v2) switched the v1 learning paths off by default: `kb_cfg` turns them on explicitly for the v1 KB tests
# (helpers.V1_KB_FLAGS); `v2_kb_cfg` keeps the v2 defaults (fix scripts, catalog action `script` / `hint`).
@pytest.fixture()
def kb_cfg(cfg: Config, tmp_path: Path) -> Config:
    """A temp KB with the v1 learning switches on (the v1 KB tests)."""
    return cfg.model_copy(update={"kb": cfg.kb.model_copy(update={"dir": str(tmp_path / "kb"), **V1_KB_FLAGS}),
                                  "advisories": cfg.advisories.model_copy(update={"ledger": True})})


@pytest.fixture()
def v2_kb_cfg(cfg: Config, tmp_path: Path) -> Config:
    """A temp KB with the v2 defaults (v1 learning paths off)."""
    return cfg.model_copy(update={"kb": cfg.kb.model_copy(update={"dir": str(tmp_path / "kb")})})


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
