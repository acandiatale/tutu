"""sbmig.rewrite — OpenRewrite (rewrite-maven-plugin) runner (workstream C).

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
