"""sbmig.plan — the rule-based v2 MigrationPlan (transform → restructure → structure review → verify), the recipe
catalog and the v1 pre-emptive handler selection (no longer planned in v2; kept until PLAN-v2 step 4).

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
from .pre_handlers import HANDLER_IDS, pre_handler_reasons, pre_handlers_for
from .versions import compare_versions, parse_version, version_lt

__all__ = ["ADVISOR_CATALOG", "ADVISOR_COVERAGE", "ADVISOR_GATES", "build_plan", "pre_handlers_for",
           "pre_handler_reasons", "advise_plan", "advisor_whitelist", "recipe_ref", "spring_recipes",
           "transform_recipes", "v2_recipe_scope", "HANDLER_IDS", "compare_versions", "parse_version", "version_lt",
           "CatalogEntry", "RecipeCatalog", "default_catalog", "GATES", "recipes_cli"]


def __getattr__(name: str) -> Any:
    if name == "recipes_cli":
        from .catalog_cli import recipes_cli
        return recipes_cli
    raise AttributeError(f"module 'sbmig.plan' has no attribute {name!r}")
