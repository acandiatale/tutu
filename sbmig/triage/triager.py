"""`Triager` (TriagerLike): clustering + masking + decisions.

v2 (flow-v2): `cluster` groups by case (L2) across files (`cluster_cases`, scoped by the loop's build target);
`decide` returns SCRIPT / BLOCKED / SKIP / ESCALATE and leaves the script step of a SCRIPT decision in
`script_step(cluster_id)` for the fix loop."""
from __future__ import annotations

from typing import Optional

from sbmig.core.config import Config
from sbmig.core.models import BuildError, ErrorCluster, KBMatch, ProjectProfile, TriageDecision

from .blocked import BlockedCandidate
from .cluster import cluster_cases
from .decide import DecisionEngine, IterationItem, plan_iteration
from .scripts import ScriptStep


class Triager:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.engine = DecisionEngine(cfg)

    # TriagerLike ---------------------------------------------------------
    def cluster(self, errors: list[BuildError], profile: Optional[ProjectProfile] = None,
                scope: Optional[str] = None) -> list[ErrorCluster]:
        """v2: one cluster per case (L2) across files; `scope` = the loop's build target (MSA service)."""
        return cluster_cases(errors, profile, scope=scope)

    def decide(self, cluster: ErrorCluster, kb_match: Optional[KBMatch], ctx, profile: ProjectProfile) -> TriageDecision:
        return self.engine.decide(cluster, kb_match, ctx, profile)

    # extras used by the build-fix loop -------------------------------------
    def plan_iteration(self, clusters: list[ErrorCluster]) -> list[IterationItem]:
        return plan_iteration(clusters)

    # run-wide blocked state (ERROR-HANDLING §1/§3) -----------------------------
    def attach_run(self, blocked_registry=None, ws=None, scripts=None) -> None:
        """Share the run-wide BlockedRegistry, the workspace (project-class checks) and the KB's script store with
        the decision engine."""
        if blocked_registry is not None:
            self.engine.blocked_registry = blocked_registry
        if ws is not None:
            self.engine.ws = ws
        if scripts is not None:
            self.engine.scripts = scripts

    def set_llm_unavailable(self, reason: Optional[str]) -> None:
        self.engine.llm_unavailable = reason

    def blocked_candidate(self, cluster_id: str) -> Optional[BlockedCandidate]:
        """Why the last BLOCKED decision for `cluster_id` was taken (reason, types, owner, covered errors)."""
        return self.engine.blocked_info.get(cluster_id)

    def script_step(self, cluster_id: str) -> Optional[ScriptStep]:
        """v2: the script step (reuse / augment / new) of the last SCRIPT decision for `cluster_id`."""
        return self.engine.script_steps.get(cluster_id)
