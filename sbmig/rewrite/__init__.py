"""rewrite-maven-plugin을 한 번 실행해 레시피를 적용하는 변환 단계 패키지. 빌드는 하지 않는다.
계획의 ``transform.rewrite`` 단계에서 오케스트레이터가 ``OpenRewriteRunner.run``을 호출하고, 바뀐 내용은
``rewrite: …`` 커밋이 된다.
- ``OpenRewriteRunner``(실행, 재시도), ``compose_rewrite_yaml``(이번 실행용 rewrite.yml 생성),
  ``resolve_recipe_artifact_versions``(레시피 BOM에서 아티팩트 버전 결정)
- 레시피 id는 ``knowledge/recipes/catalog.yml``에서 관리하고 여기서는 받은 id를 그대로 쓴다
- 레시피 BOM은 3.37.0으로 고정한다. 3.38.0은 Maven Central에서 버전이 맞지 않는다

sbmig.rewrite — OpenRewrite (rewrite-maven-plugin) runner (workstream C).

Public API (docs/INTERFACES.md):
    OpenRewriteRunner(ws, cfg, kb=None).run(recipes, *, extra_recipe_files=(), module=None, dry_run=False, label="")
    compose_rewrite_yaml(recipes, *, extra_files=(), target=None) -> str
    resolve_recipe_artifact_versions(cfg, mvn_env, *, artifacts=None, cache_path=None, work_dir=None) -> {"g:a": v}


"""
from .compose import COMPOSITE_RECIPE_ID, RecipeCompositionError, compose_rewrite_yaml, recipe_documents
from .runner import OpenRewriteRunner, recipe_artifact_for
from .versions import RecipeArtifactError, parse_bom_versions, resolve_recipe_artifact_versions

__all__ = [
    "COMPOSITE_RECIPE_ID",
    "OpenRewriteRunner",
    "RecipeArtifactError",
    "RecipeCompositionError",
    "compose_rewrite_yaml",
    "parse_bom_versions",
    "recipe_artifact_for",
    "recipe_documents",
    "resolve_recipe_artifact_versions",
]
