"""`Triager` (TriagerLike): clustering + masking + decisions."""
from __future__ import annotations

from typing import Iterable, Optional

from sbmig.core.config import Config
from sbmig.core.models import (BuildError, ErrorCluster, KBMatch, Phase, ProjectProfile, RecipeRef,
                               TriageDecision)

from .cluster import cluster_errors
from .decide import DecisionEngine, IterationItem, plan_iteration, recipe_refs_for
from .handlers.base import HandlerRegistry
from .rules import RecipeRules


class Triager:
    def __init__(self, cfg: Config, registry: Optional[HandlerRegistry] = None,
                 rules: Optional[RecipeRules] = None):
        self.cfg = cfg
        self.registry = registry if registry is not None else HandlerRegistry.default(cfg)
        self.rules = rules if rules is not None else RecipeRules.default()
        self.engine = DecisionEngine(cfg, self.registry, self.rules)

    # TriagerLike ---------------------------------------------------------
    def cluster(self, errors: list[BuildError], profile: Optional[ProjectProfile] = None) -> list[ErrorCluster]:
        return cluster_errors(errors, profile)

    def decide(self, cluster: ErrorCluster, kb_match: Optional[KBMatch], ctx, profile: ProjectProfile) -> TriageDecision:
        return self.engine.decide(cluster, kb_match, ctx, profile)

    # extras used by the build-fix loop -------------------------------------
    def plan_iteration(self, clusters: list[ErrorCluster]) -> list[IterationItem]:
        return plan_iteration(clusters)

    def mark_recipes_run(self, recipe_ids: Iterable[str], phase: Optional[Phase] = None) -> None:
        self.engine.mark_recipes_run(recipe_ids, phase)

    def recipe_refs(self, decision: TriageDecision, kb_match: Optional[KBMatch] = None) -> list[RecipeRef]:
        return recipe_refs_for(decision.recipe_ids, kb_match, self.rules)
