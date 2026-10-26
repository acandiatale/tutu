"""The advisory tests exercise the recipe policy too: v2 (flow-v2) turns `advisories.allow_recipes` off by default
(advisories are LLM hints + Handover only), so these tests opt back in — the policy machinery stays for a company
config that enables it. Tests about the off switch set it to False themselves."""
from __future__ import annotations

import pytest

from sbmig.core.config import Config


@pytest.fixture()
def cfg(cfg: Config) -> Config:
    cfg.advisories.allow_recipes = True
    return cfg
