"""Lazy resolution of collaborators from other workstreams + null/lazy stand-ins."""
from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Any, Callable, Optional

from sbmig.core.models import HarvestResult, KBMatch, KBRecipe

log = logging.getLogger("sbmig.loop")


class MissingComponentError(RuntimeError):
    """A collaborator package (another workstream) is missing or broken."""


def lazy_attr(module: str, attr: str, workstream: str) -> Any:
    try:
        mod = importlib.import_module(module)
    except Exception as e:
        raise MissingComponentError(
            f"{module}.{attr} (workstream {workstream}) cannot be imported: {type(e).__name__}: {e}") from e
    try:
        return getattr(mod, attr)
    except AttributeError as e:
        raise MissingComponentError(
            f"{module} (workstream {workstream}) does not expose '{attr}' yet — see docs/INTERFACES.md") from e


class NullKnowledgeBase:
    """KnowledgeBaseLike that knows nothing (used with --no-kb or when sbmig.kb is unavailable)."""

    def match(self, cluster, ws, profile) -> KBMatch:
        return KBMatch()

    def active_recipe_files(self) -> list[Path]:
        return []

    def active_recipe_ids(self) -> list[str]:
        return []

    def record_case(self, case) -> str:
        return case.id

    def credit_recipe(self, recipe_id: str, success: bool, project_fingerprint: str) -> None:
        return None

    def harvest(self, phase_result, profile, ws, llm=None) -> HarvestResult:
        return HarvestResult()

    def list_recipes(self, status: Optional[str] = None) -> list[KBRecipe]:
        return []

    def promote(self, recipe_id: str) -> KBRecipe:  # pragma: no cover - never used by the loop
        raise KeyError(recipe_id)

    def reject(self, recipe_id: str, reason: str = "") -> KBRecipe:  # pragma: no cover
        raise KeyError(recipe_id)

    def reindex(self) -> int:
        return 0


class LazyFixer:
    """FixerLike proxy that creates the real fixer (and its LLM client) only when first needed."""

    def __init__(self, factory: Callable[[], Any]):
        self._factory = factory
        self._fixer: Any = None

    def _get(self) -> Any:
        if self._fixer is None:
            self._fixer = self._factory()
        return self._fixer

    def propose(self, ws, cluster, ctx):
        return self._get().propose(ws, cluster, ctx)

    def apply(self, ws, changes):
        return self._get().apply(ws, changes)


def basic_profile_summary(profile) -> str:
    if profile is None:
        return ""
    libs = ", ".join(sorted({lib.name for lib in profile.libraries})[:20])
    return (f"project {profile.name}: case {profile.case_type.value}, java {profile.java_version or '?'}, "
            f"spring boot {profile.spring_boot_version or '-'}, spring {profile.spring_version or '-'}, "
            f"{len(profile.poms)} pom(s){', multi-module' if profile.is_multi_module else ''}; libs: {libs or '-'}")


def profile_summary(profile) -> str:
    try:
        fn = importlib.import_module("sbmig.fix").profile_summary  # type: ignore[attr-defined]
        return fn(profile)
    except Exception:
        return basic_profile_summary(profile)
