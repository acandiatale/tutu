"""sbmig.plan — the rule-based v2 MigrationPlan (transform → restructure → structure review → verify) and the recipe
catalog (`knowledge/recipes/catalog.yml`).

`recipes_cli` (typer sub-app, mounted by sbmig/cli.py as `sbmig recipes`) is imported lazily."""
from typing import Any

from .catalog import GATES, CatalogEntry, RecipeCatalog, default_catalog
from .planner import (
    ADVISOR_CATALOG,
    ADVISOR_COVERAGE,
    ADVISOR_GATES,
    advise_plan,
    advisor_whitelist,
    build_plan,
    recipe_ref,
    spring_recipes,
    transform_recipes,
    v2_recipe_scope,
)
from .versions import compare_versions, parse_version, version_lt

__all__ = ["ADVISOR_CATALOG", "ADVISOR_COVERAGE", "ADVISOR_GATES", "build_plan", "advise_plan", "advisor_whitelist",
           "recipe_ref", "spring_recipes", "transform_recipes", "v2_recipe_scope", "compare_versions",
           "parse_version", "version_lt", "CatalogEntry", "RecipeCatalog", "default_catalog", "GATES", "recipes_cli"]


def __getattr__(name: str) -> Any:
    if name == "recipes_cli":
        from .catalog_cli import recipes_cli
        return recipes_cli
    raise AttributeError(f"module 'sbmig.plan' has no attribute {name!r}")
