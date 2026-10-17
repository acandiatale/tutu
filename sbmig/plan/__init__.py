"""sbmig.plan — rule-based MigrationPlan + pre-emptive handler selection."""
from .planner import ADVISOR_CATALOG, advise_plan, build_plan, recipe_ref, spring_recipes
from .pre_handlers import HANDLER_IDS, pre_handler_reasons, pre_handlers_for
from .versions import compare_versions, parse_version, version_lt

__all__ = ["ADVISOR_CATALOG", "build_plan", "pre_handlers_for", "pre_handler_reasons", "advise_plan", "recipe_ref",
           "spring_recipes", "HANDLER_IDS", "compare_versions", "parse_version", "version_lt"]
