"""Reactive recipe rules (`recipe_rules.yml`): map error patterns to builtin OpenRewrite recipes."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Optional

import yaml
from pydantic import BaseModel, Field

from sbmig.core.models import ErrorCluster, ErrorKind, Phase, RecipeRef

DEFAULT_RULES_PATH = Path(__file__).with_name("recipe_rules.yml")


class RuleMatch(BaseModel):
    signature_regex: Optional[str] = None
    message_regex: Optional[str] = None
    symbol_regex: Optional[str] = None


class RecipeRule(BaseModel):
    id: str
    match: RuleMatch
    recipes: list[str]
    artifact: Optional[str] = None
    kinds: list[ErrorKind] = Field(default_factory=lambda: [ErrorKind.COMPILE, ErrorKind.TEST_COMPILE])
    phases: list[Phase] = Field(default_factory=list)          # empty = any phase
    once_per_run: bool = True

    def matches(self, cluster: ErrorCluster, phase: Optional[Phase] = None) -> bool:
        if self.kinds and cluster.kind not in self.kinds:
            return False
        if phase is not None and self.phases and phase not in self.phases:
            return False
        m = self.match
        for e in cluster.errors:
            if m.signature_regex and (re.search(m.signature_regex, e.signature or "")
                                      or re.search(m.signature_regex, e.signature_generic or "")):
                return True
            if m.message_regex and re.search(m.message_regex, e.message or ""):
                return True
            if m.symbol_regex and re.search(m.symbol_regex, e.symbol or ""):
                return True
        return False

    def refs(self) -> list[RecipeRef]:
        return [RecipeRef(id=r, source="builtin", artifact=self.artifact) for r in self.recipes]


class RecipeRules:
    def __init__(self, rules: Iterable[RecipeRule] = ()):
        self.rules: list[RecipeRule] = list(rules)

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "RecipeRules":
        p = Path(path) if path else DEFAULT_RULES_PATH
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return cls(RecipeRule.model_validate(r) for r in data.get("rules", []))

    @classmethod
    def default(cls) -> "RecipeRules":
        return cls.load(DEFAULT_RULES_PATH)

    def matching_rules(self, cluster: ErrorCluster, phase: Optional[Phase] = None) -> list[RecipeRule]:
        return [r for r in self.rules if r.matches(cluster, phase)]

    def match(self, cluster: ErrorCluster, phase: Optional[Phase] = None) -> list[RecipeRef]:
        out: list[RecipeRef] = []
        for rule in self.matching_rules(cluster, phase):
            for ref in rule.refs():
                if not any(x.id == ref.id for x in out):
                    out.append(ref)
        return out

    def once_per_run_ids(self) -> set[str]:
        return {r for rule in self.rules if rule.once_per_run for r in rule.recipes}

    def ref_for(self, recipe_id: str) -> Optional[RecipeRef]:
        for rule in self.rules:
            if recipe_id in rule.recipes:
                return RecipeRef(id=recipe_id, source="builtin", artifact=rule.artifact)
        return None
