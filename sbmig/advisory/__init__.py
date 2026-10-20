"""Library advisories: EOL / no-longer-managed libraries (springfox, hystrix, sleuth, old Spring Cloud trains …).

Contract (PM, round 4) — the orchestrator, the planner, the report and the fleet depend on exactly these names:

    load_advisories(cfg)                       -> AdvisoryCatalog        knowledge/libraries/advisories.yml (+ extra_files)
    evaluate_advisories(profile, cfg, catalog=None) -> list[AdvisoryFinding]
                                               findings for this project, policies resolved (config overrides,
                                               allow_recipes, recipe availability in the recipe catalog); no plan
                                               knowledge yet (recipe_source stays "none")
    advisory_recipe_refs(findings, cfg, planned_ids, catalog=None) -> list[RecipeRef]
                                               called by the planner for the spring.rewrite step(s): returns refs for
                                               policy=="recipe" findings whose recipes are neither in `planned_ids` nor
                                               covered by a planned composite (RecipeCatalog.covered_by); mutates each
                                               finding in place: recipes / recipe_source ("plan" | "advisory" | "none")
    verify_advisories(findings, view, cfg)     -> list[AdvisoryFinding]
                                               finalize: re-detect libraries on the final tree (`view` = Workspace or
                                               PathView over the migrated project) and set outcome
                                               (replaced | still-present | unknown); never raises
    followups_for(findings)                    -> list[str]   one actionable sentence per finding, not-replaced first
    hints_for(errors, findings, profile=None)  -> (list[str], list[str])   round 6: guidance lines + advisory ids for
                                               the findings whose library / artifacts the cluster's errors mention
                                               (FixContext.advisory_hints, FixAttempt.advisory_ids); never raises
    advisories_cli                             typer sub-app mounted as `sbmig advisories` (list | show | check)

Policies: guide | recipe only. There is deliberately no "block": an advisory never fails the run or changes the
exit code. Everything here must be safe to call with an empty / missing advisories file (→ no findings).

Implementation: sbmig/advisory/{catalog,evaluate,verify,cli}.py (agent A). This module re-exports them and keeps
the import-safe fallbacks below so the orchestrator never breaks when the package is half-built.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

log = logging.getLogger("sbmig.advisory")

if TYPE_CHECKING:  # pragma: no cover
    from sbmig.core.config import Config
    from sbmig.core.models import AdvisoryFinding, ProjectProfile, RecipeRef

try:
    from .catalog import AdvisoryCatalog, load_advisories
    from .evaluate import advisory_recipe_refs, evaluate_advisories, followups_for
    from .verify import verify_advisories
except ImportError as _e:  # pragma: no cover - until agent A lands the modules
    log.debug("sbmig.advisory not fully available yet: %s", _e)

    class AdvisoryCatalog:  # type: ignore[no-redef]
        entries: list = []

        def get(self, advisory_id: str) -> Any:
            return None

        def __len__(self) -> int:
            return 0

    def load_advisories(cfg: "Config") -> "AdvisoryCatalog":  # type: ignore[misc]
        return AdvisoryCatalog()

    def evaluate_advisories(profile: "ProjectProfile", cfg: "Config",
                            catalog: Optional["AdvisoryCatalog"] = None) -> list["AdvisoryFinding"]:  # type: ignore[misc]
        return []

    def advisory_recipe_refs(findings: list["AdvisoryFinding"], cfg: "Config", planned_ids: list[str] | set[str],
                             catalog: Any = None) -> list["RecipeRef"]:  # type: ignore[misc]
        return []

    def verify_advisories(findings: list["AdvisoryFinding"], view: Any, cfg: "Config") -> list["AdvisoryFinding"]:  # type: ignore[misc]
        return list(findings)

    def followups_for(findings: list["AdvisoryFinding"]) -> list[str]:  # type: ignore[misc]
        return [f"{f.title}: {f.followup}".rstrip(": ") for f in findings if f.outcome != "replaced"]

try:
    from .hints import hints_for
except ImportError:  # pragma: no cover
    def hints_for(errors: Any, findings: Any, profile: Any = None, **_kw: Any) -> tuple[list[str], list[str]]:  # type: ignore[misc]
        return [], []

try:
    from .cli import advisories_cli
except ImportError:  # pragma: no cover
    advisories_cli = None  # type: ignore[assignment]

__all__ = ["AdvisoryCatalog", "load_advisories", "evaluate_advisories", "advisory_recipe_refs", "verify_advisories",
           "followups_for", "hints_for", "advisories_cli"]
