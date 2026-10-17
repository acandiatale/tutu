"""Decision engine (ARCHITECTURE §4, ERROR-HANDLING §1/§3):

    run-wide blocked registry → HANDLER > KB_RECIPE > KB_PATCH > blocked check (cfg.loop.blocked_policy) > LLM_FIX
    → BLOCKED(repeated_failure | budget_exhausted | llm_unavailable)

plus masking. A BLOCKED decision never calls the LLM; the build-fix loop records a BlockedItem for it and keeps
working on everything else."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from sbmig.core.config import Config
from sbmig.core.models import (BlockReason, DecisionKind, ErrorCluster, FixAttempt, KBMatch, Phase, ProjectProfile,
                               RecipeRef, TriageDecision)

from .blocked import BlockedCandidate, classify_blocked
from .cluster import FILE_KINDS, RESOLUTION_KINDS, cluster_label
from .rules import RecipeRules

SUCCESS_OUTCOMES = frozenset({"improved", "build_ok"})
# attempts that are still "pending" were applied this iteration and not yet scored → they count as tried
FAILED_OUTCOMES = frozenset({"no_change", "regressed", "reverted", "apply_failed", "pending"})
# LLM attempts that produced nothing usable (no patches parsed / nothing applied / llm-error)
GARBAGE_OUTCOMES = frozenset({"apply_failed"})


def blocked_rationale(reason: BlockReason, detail: str) -> str:
    return f"blocked: {reason.value} — {detail}"


@dataclass
class IterationItem:
    cluster: ErrorCluster
    skip: Optional[TriageDecision] = None          # SKIP decision when masked
    masked_by: list[str] = field(default_factory=list)


def related_attempts(cluster: ErrorCluster, attempts: Iterable[FixAttempt]) -> list[FixAttempt]:
    sigs = set(cluster.signatures)
    return [a for a in attempts if a.cluster_id == cluster.id or (sigs and sigs & set(a.cluster_signatures))]


def plan_iteration(clusters: list[ErrorCluster]) -> list[IterationItem]:
    """Masking: resolution-type clusters (parent/pom/dependency/plugin resolution) mask compile and test-compile
    clusters of the same module (or of every module when the resolution error has no module)."""
    resolution = [c for c in clusters if c.kind in RESOLUTION_KINDS]
    out: list[IterationItem] = []
    for c in clusters:
        if c.kind in FILE_KINDS and resolution:
            maskers = [r for r in resolution if r.module is None or r.module == c.module]
            if maskers:
                names = ", ".join(cluster_label(r) for r in maskers[:3])
                out.append(IterationItem(c, TriageDecision(
                    cluster_id=c.id, kind=DecisionKind.SKIP,
                    rationale=f"masked this iteration by pom/resolution problem(s) in the same module: {names} — "
                              "compile errors usually disappear once the pom is fixed"),
                    [r.id for r in maskers]))
                continue
        out.append(IterationItem(c))
    return out


def recipe_refs_for(recipe_ids: list[str], kb_match: Optional[KBMatch], rules: RecipeRules) -> list[RecipeRef]:
    out: list[RecipeRef] = []
    for rid in recipe_ids:
        ref: Optional[RecipeRef] = None
        if kb_match is not None:
            for ids, files in ((kb_match.recipe_ids, kb_match.recipe_files),
                               (kb_match.candidate_recipe_ids, kb_match.candidate_recipe_files)):
                if rid in ids:
                    i = ids.index(rid)
                    ref = RecipeRef(id=rid, source="kb", file=files[i] if len(files) == len(ids) else None)
                    break
        if ref is None:
            ref = rules.ref_for(rid) or RecipeRef(id=rid, source="builtin")
        if not any(r.id == ref.id for r in out):
            out.append(ref)
    return out


class DecisionEngine:
    def __init__(self, cfg: Config, registry, rules: RecipeRules):
        self.cfg = cfg
        self.registry = registry
        self.rules = rules
        self.recipes_run_by_phase: dict[Phase, set[str]] = {}
        self.recipes_run_any: set[str] = set()
        # run-wide state attached by the build-fix loop (see Triager.attach_run)
        self.blocked_registry = None            # sbmig.loop.blocked_registry.BlockedRegistry
        self.ws = None                          # Workspace: project-class checks for the blocked rules
        self.llm_unavailable: Optional[str] = None
        # cluster id → why it was blocked (read by the loop to build the BlockedItem)
        self.blocked_info: dict[str, BlockedCandidate] = {}

    # -- bookkeeping from the loop/orchestrator ------------------------
    def mark_recipes_run(self, recipe_ids: Iterable[str], phase: Optional[Phase] = None) -> None:
        ids = set(recipe_ids)
        self.recipes_run_any |= ids
        if phase is not None:
            self.recipes_run_by_phase.setdefault(phase, set()).update(ids)

    def _excluded_recipes(self, phase: Phase, failed: list[FixAttempt]) -> set[str]:
        out = set(self.recipes_run_by_phase.get(phase, set()))
        out |= self.recipes_run_any & self.rules.once_per_run_ids()
        for a in failed:
            if a.decision.kind == DecisionKind.KB_RECIPE:
                out |= set(a.decision.recipe_ids)
        return out

    # -- main ----------------------------------------------------------
    def _blocked(self, cluster: ErrorCluster, cand: BlockedCandidate, extra: str = "") -> TriageDecision:
        self.blocked_info[cluster.id] = cand
        return TriageDecision(cluster_id=cluster.id, kind=DecisionKind.BLOCKED,
                              rationale=blocked_rationale(cand.reason, cand.detail()) + extra)

    def decide(self, cluster: ErrorCluster, kb_match: Optional[KBMatch], ctx, profile: ProjectProfile) -> TriageDecision:
        kb_match = kb_match or KBMatch()
        prev = related_attempts(cluster, ctx.previous_attempts)
        failed = [a for a in prev if a.outcome in FAILED_OUTCOMES]
        label = cluster_label(cluster)
        self.blocked_info.pop(cluster.id, None)

        # 0. already blocked earlier in this run (any phase) → no handler re-runs, no LLM
        reg = self.blocked_registry
        if reg is not None:
            item = reg.covering(cluster)
            if item is not None:
                cand = BlockedCandidate(reason=item.reason, required_type=item.required_type,
                                        found_type=item.found_type, owner_class=item.owner_class,
                                        evidence=[f"already blocked earlier in this run ({item.id})"],
                                        errors=list(cluster.errors))
                self.blocked_info[cluster.id] = cand
                return TriageDecision(cluster_id=cluster.id, kind=DecisionKind.BLOCKED,
                                      rationale=f"blocked: {item.reason.value} — already blocked earlier in this run "
                                                f"({item.id}, {item.phase.value}); skipped")

        # 1. deterministic handler
        tried = {a.decision.handler_id for a in failed if a.decision.kind == DecisionKind.HANDLER}
        candidates = list(self.registry.matching(cluster, profile, self.cfg)) if self.registry is not None else []
        for hid in kb_match.handler_ids:
            h = self.registry.get(hid) if self.registry is not None else None
            if h is not None and all(c.id != h.id for c in candidates):
                candidates.append(h)
        for h in candidates:
            if h.id in tried:
                continue
            why = f"deterministic handler '{h.id}' matches {label}: {h.description}"
            if tried:
                why += f" (already tried without effect: {', '.join(sorted(t for t in tried if t))})"
            return TriageDecision(cluster_id=cluster.id, kind=DecisionKind.HANDLER, handler_id=h.id, rationale=why)

        # 2. KB active recipes ∪ reactive recipe rules, then KB candidate recipes
        excluded = self._excluded_recipes(ctx.phase, failed)
        active: list[str] = []
        if self.cfg.kb.use_active_recipes:
            active += kb_match.recipe_ids
        rule_hits = self.rules.matching_rules(cluster, ctx.phase)
        for rule in rule_hits:
            active += rule.recipes
        fresh = [r for i, r in enumerate(active) if r not in excluded and r not in active[:i]]
        source = "active KB recipe(s)/recipe rule(s)"
        if not fresh and self.cfg.kb.use_active_recipes:
            fresh = [r for r in dict.fromkeys(kb_match.candidate_recipe_ids) if r not in excluded]
            source = "KB candidate recipe(s)"
        if fresh:
            rules_txt = f" [rules: {', '.join(r.id for r in rule_hits)}]" if rule_hits else ""
            return TriageDecision(cluster_id=cluster.id, kind=DecisionKind.KB_RECIPE, recipe_ids=fresh,
                                  rationale=f"{source} for {label}: {', '.join(fresh)}{rules_txt}")

        # 3. KB patch replay
        if self.cfg.kb.use_patch_replay and kb_match.replayable:
            tried_cases = {cid for a in failed if a.decision.kind == DecisionKind.KB_PATCH for cid in a.decision.kb_case_ids}
            cases = [c for c in kb_match.replayable if c.id not in tried_cases]
            if cases:
                c = cases[0]
                return TriageDecision(cluster_id=cluster.id, kind=DecisionKind.KB_PATCH, kb_case_ids=[c.id],
                                      rationale=f"replay KB case {c.id} (same signature, search text still present)"
                                                + (f": {c.summary}" if c.summary else ""))

        llm_failed = [a for a in failed if a.decision.kind == DecisionKind.LLM_FIX]
        garbage = [a for a in llm_failed if a.outcome in GARBAGE_OUTCOMES]

        # 4. blocked check: cannot be fixed inside the project → blocked_policy[reason] LLM attempts, then BLOCKED
        policy_note = ""
        try:
            cand = classify_blocked(cluster, profile, self.cfg, ws=self.ws)
        except Exception:  # a classifier bug must never stop the loop
            cand = None
        if cand is not None:
            allowed = int(self.cfg.loop.blocked_policy.get(cand.reason.value, 0))
            if len(llm_failed) >= allowed:
                extra = (f" (after {len(llm_failed)} LLM attempt(s))" if llm_failed else
                         " (policy: no LLM attempt for this reason)")
                return self._blocked(cluster, cand, extra)
            policy_note = (f"; looks like {cand.reason.value} ({cand.detail()}) — LLM attempt "
                           f"{len(llm_failed) + 1}/{allowed} before blocking")

        # 5. LLM (bounded) → BLOCKED
        if self.llm_unavailable:
            return self._blocked(cluster, BlockedCandidate(
                reason=BlockReason.LLM_UNAVAILABLE, errors=list(cluster.errors),
                evidence=[f"LLM unavailable ({self.llm_unavailable}); no deterministic fix for {label}"]))
        max_garbage = max(1, int(self.cfg.loop.max_llm_attempts_per_cluster))
        limit = max(max_garbage, self.cfg.loop.stall_limit + 1)
        if len(garbage) >= max_garbage or len(llm_failed) >= limit:
            why = (f"{len(garbage)} LLM attempt(s) on {label} produced no usable patch (limit {max_garbage})"
                   if len(garbage) >= max_garbage else
                   f"{len(llm_failed)} LLM attempt(s) on {label} did not resolve it (limit {limit})")
            return self._blocked(cluster, BlockedCandidate(
                reason=BlockReason.REPEATED_FAILURE, errors=list(cluster.errors),
                evidence=[why + "; needs a human"]))
        if ctx.llm_calls_used >= ctx.llm_calls_budget:
            return self._blocked(cluster, BlockedCandidate(
                reason=BlockReason.BUDGET_EXHAUSTED, errors=list(cluster.errors),
                evidence=[f"LLM budget exhausted ({ctx.llm_calls_used}/{ctx.llm_calls_budget} calls) and no "
                          f"deterministic fix for {label}"]))
        why = f"no handler/recipe/KB patch for {label} → LLM fix"
        if llm_failed:
            why += f" (attempt {len(llm_failed) + 1}/{limit}, previous attempts included in the prompt)"
        if ctx.stall_level:
            why += f"; stall level {ctx.stall_level}"
        return TriageDecision(cluster_id=cluster.id, kind=DecisionKind.LLM_FIX, rationale=why + policy_note)
