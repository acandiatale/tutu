"""Fixtures for KB tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.core.config import Config
from sbmig.kb import FileKnowledgeBase


@pytest.fixture()
def kb_cfg(cfg: Config, tmp_path: Path) -> Config:
    """A temp KB (``<tmp>/kb``) with the config defaults."""
    return cfg.model_copy(update={"kb": cfg.kb.model_copy(update={"dir": str(tmp_path / "kb")})})


@pytest.fixture()
def kb(kb_cfg: Config) -> FileKnowledgeBase:
    return FileKnowledgeBase(kb_cfg)
