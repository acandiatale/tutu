"""OpenRewrite 레시피를 적용하는 변환 단계 패키지. rewrite-maven-plugin을 한 번 실행하고, 빌드는 하지 않는다.

Public API (docs/INTERFACES.md):
    OpenRewriteRunner(ws, cfg, kb=None).run(recipes, *, extra_recipe_files=(), module=None, dry_run=False, label="")
    compose_rewrite_yaml(recipes, *, extra_files=(), target=None) -> str
    resolve_recipe_artifact_versions(cfg, mvn_env, *, artifacts=None, cache_path=None, work_dir=None) -> {"g:a": v}

The orchestrator calls ``OpenRewriteRunner.run`` (run and retries) in the plan's ``transform.rewrite`` step,
and the changes become the ``rewrite: …`` commit. ``compose_rewrite_yaml`` writes this run's rewrite.yml;
``resolve_recipe_artifact_versions`` takes the recipe artifacts' versions from the recipe BOM. Recipe ids
are kept in ``knowledge/recipes/catalog.yml``; this package uses the ids it is given as they are. The recipe
BOM is pinned to 3.37.0 (``openrewrite.recipe_bom_version``): 3.38.0 is inconsistent on Maven Central.
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
