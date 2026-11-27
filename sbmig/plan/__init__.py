"""이관 계획(변환 -> 재구성 -> 구조 검토 -> 검증 빌드)을 규칙으로 세우는 패키지.
레시피 카탈로그도 여기서 관리한다.

The orchestrator calls ``build_plan`` in the plan step after analyze; the result is ``plan.json``.
Contents: ``build_plan`` (planner.py), ``default_catalog`` / ``RecipeCatalog`` (catalog.py), ``recipes_cli``
(``sbmig recipes``, imported on first use) and version comparison (versions.py). Recipe ids live only in
``knowledge/recipes/catalog.yml``."""
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
    # 계획 단계에서 typer를 괜히 import하지 않게, recipes_cli는 필요할 때만 가져온다
    if name == "recipes_cli":
        from .catalog_cli import recipes_cli
        return recipes_cli
    raise AttributeError(f"module 'sbmig.plan' has no attribute {name!r}")
