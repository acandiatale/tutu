"""계획 패키지 — 규칙 기반 v2 계획(변환 → 재구성 → 구조 검토 → 검증 빌드)과 레시피 카탈로그.
흐름상 위치: 분석 다음 계획 단계에서 오케스트레이터가 ``build_plan`` 을 부르고 결과가 ``plan.json`` 이 된다.
핵심 진입점: ``build_plan``(planner.py), ``default_catalog`` / ``RecipeCatalog``(catalog.py),
``recipes_cli``(``sbmig recipes`` — 필요할 때만 늦게 import), 버전 비교(versions.py). 레시피 id 의 유일한
출처는 ``knowledge/recipes/catalog.yml`` 이다(CLAUDE.md 불변조건 5).

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
    # typer 를 계획 단계에서 불필요하게 import 하지 않으려고 recipes_cli 만 늦게 가져온다
    if name == "recipes_cli":
        from .catalog_cli import recipes_cli
        return recipes_cli
    raise AttributeError(f"module 'sbmig.plan' has no attribute {name!r}")
