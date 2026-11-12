"""규칙으로 이관 계획(변환 -> 재구성 -> 구조 검토 -> 검증 빌드)을 세우고 레시피 카탈로그를 관리하는 패키지.
분석 다음 계획 단계에서 오케스트레이터가 ``build_plan``을 호출하고 그 결과가 ``plan.json``이 된다.
- ``build_plan``(planner.py), ``default_catalog`` / ``RecipeCatalog``(catalog.py), ``recipes_cli``(``sbmig
  recipes``), 버전 비교(versions.py)
- 레시피 id는 ``knowledge/recipes/catalog.yml``에서만 관리한다

sbmig.plan — the rule-based v2 MigrationPlan (transform → restructure → structure review → verify) and the recipe
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
    # 계획 단계에서 typer를 불필요하게 import하지 않도록 recipes_cli만 필요할 때 가져온다
    if name == "recipes_cli":
        from .catalog_cli import recipes_cli
        return recipes_cli
    raise AttributeError(f"module 'sbmig.plan' has no attribute {name!r}")
