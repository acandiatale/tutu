"""Library-conditional plan recipes (`knowledge/recipes/library_recipes.yml`, round 10).

Plans without the Spring Boot 4 umbrella (spring_legacy, MSA library roots, servlet_tomcat, pure_java) get the
Spring Batch / Hibernate / Thymeleaf migrations the umbrella would have run, when the analyzer detected the library.
`library_recipe_sets()` exposes the rules as recipe sets named `library.<library>` so `sbmig recipes check` /
`sbmig recipes sets` treat them like the config sets (catalog entries list `library.<library>` in `sets`).
Never raises: a missing / broken table means no library recipes (logged).
"""
from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field

from sbmig.core.config import PACKAGE_ROOT
from sbmig.core.models import CaseType

log = logging.getLogger("sbmig.plan.library_recipes")

DEFAULT_LIBRARY_RECIPES_PATH = PACKAGE_ROOT / "knowledge" / "recipes" / "library_recipes.yml"
SET_PREFIX = "library."


class Caveat(BaseModel):
    artifact: str                       # "group:artifact" declared by the as-is poms
    note: str


class LibraryRecipeRule(BaseModel):
    library: str                        # LibraryUsage name (sbmig.analyze.libraries rule)
    cases: list[CaseType] = Field(default_factory=list)
    recipes: list[str] = Field(default_factory=list)
    note: str = ""
    caveats: list[Caveat] = Field(default_factory=list)

    @property
    def set_name(self) -> str:
        return SET_PREFIX + self.library


@lru_cache(maxsize=4)
def _load(path: str, mtime: float) -> tuple[LibraryRecipeRule, ...]:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return tuple(LibraryRecipeRule.model_validate(r) for r in data.get("rules") or [])
    except Exception as e:  # a broken table must never break planning
        log.warning("library recipe table %s unreadable (%s: %s); no library recipes", path, type(e).__name__, e)
        return ()


def library_recipe_rules(path: Optional[Path] = None) -> tuple[LibraryRecipeRule, ...]:
    p = Path(path or DEFAULT_LIBRARY_RECIPES_PATH)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return ()
    return _load(str(p.resolve()), mtime)


def library_recipe_sets(path: Optional[Path] = None) -> dict[str, list[str]]:
    """`library.<library>` → recipe ids (one set per rule; a library listed twice merges in file order)."""
    out: dict[str, list[str]] = {}
    for r in library_recipe_rules(path):
        ids = out.setdefault(r.set_name, [])
        ids += [i for i in r.recipes if i not in ids]
    return out


def rules_for(case: CaseType, libs: set[str], path: Optional[Path] = None) -> list[LibraryRecipeRule]:
    """Rules whose library is detected and whose `cases` include the spring.rewrite case (empty `cases` = never:
    a rule must say where it applies, the Boot umbrella cases already run these recipes)."""
    return [r for r in library_recipe_rules(path) if r.library in libs and case in r.cases and r.recipes]


__all__ = [
    "DEFAULT_LIBRARY_RECIPES_PATH",
    "SET_PREFIX",
    "LibraryRecipeRule",
    "library_recipe_rules",
    "library_recipe_sets",
    "rules_for",
]
