"""sbmig.rewrite — 변환(OpenRewrite 1회) 단계의 실행기: rewrite-maven-plugin 을 한 번
돌려 레시피를 적용한다(빌드 없음). 흐름상 위치: 계획의 ``transform.rewrite`` 단계에서 오케스트레이터가
``OpenRewriteRunner.run`` 을 부르고, 바뀐 것은 커밋 ``rewrite: …`` 가 된다. 핵심 진입점:
``OpenRewriteRunner``(실행·재시도), ``compose_rewrite_yaml``(이번 실행용 rewrite.yml 합성),
``resolve_recipe_artifact_versions``(레시피 BOM 에서 레시피 아티팩트 버전 결정). 결정: v2 는 Java 21 / jakarta
/ Boot 4 레시피만 돌린다(레시피 id 의 원본은 ``knowledge/recipes/catalog.yml``, 여기서는 받은 id 를 그대로
쓴다). 레시피 BOM 은 3.37.0 고정 — 3.38.0 은 Maven Central 에서 버전이 맞지 않는다(불변조건 5).

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
