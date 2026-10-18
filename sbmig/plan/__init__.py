"""sbmig.plan — rule-based MigrationPlan, pre-emptive handler selection and the recipe catalog.

`recipes_cli` (typer sub-app, mounted by sbmig/cli.py as `sbmig recipes`) is imported lazily."""
from typing import Any

from .catalog import GATES, CatalogEntry, RecipeCatalog, default_catalog
from .planner import (
    ADVISOR_CATALOG,
    ADVISOR_COVERAGE,
    ADVISOR_GATES,
    advise_plan,
    build_plan,
    recipe_ref,
    spring_recipes,
)
from .pre_handlers import HANDLER_IDS, pre_handler_reasons, pre_handlers_for
from .versions import compare_versions, parse_version, version_lt

__all__ = ["ADVISOR_CATALOG", "ADVISOR_COVERAGE", "ADVISOR_GATES", "build_plan", "pre_handlers_for",
           "pre_handler_reasons", "advise_plan", "recipe_ref", "spring_recipes", "HANDLER_IDS", "compare_versions",
           "parse_version", "version_lt", "CatalogEntry", "RecipeCatalog", "default_catalog", "GATES",
           "recipes_cli"]


def __getattr__(name: str) -> Any:
    if name == "recipes_cli":
        from .catalog_cli import recipes_cli
        return recipes_cli
    raise AttributeError(f"module 'sbmig.plan' has no attribute {name!r}")
