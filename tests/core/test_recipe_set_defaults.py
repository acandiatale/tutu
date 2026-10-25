"""The recipe sets of config/default.yml and the `RecipeSetConfig` defaults stay the same (round 10: they drifted —
a Config() built without the yml planned fewer recipes than the catalog describes)."""
from __future__ import annotations

import yaml

from sbmig.core.config import DEFAULT_CONFIG_PATH, RecipeSetConfig


def test_code_defaults_mirror_default_yml_recipe_sets():
    sets = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))["openrewrite"]["recipes"]
    defaults = RecipeSetConfig().model_dump()
    assert {k: v for k, v in defaults.items() if k != "extra"} == sets
