"""sbmig.loop — orchestrator + build-fix loop (the orchestrator owns the loop; done = BUILD SUCCESS)."""
from .budget import LoopBudget
from .components import LazyFixer, MissingComponentError, NullKnowledgeBase, lazy_attr
from .fixloop import BuildFixLoop
from .orchestrator import Orchestrator

# DryRun mode: `Orchestrator(...).run(dry_run=True)` / `BuildFixLoop(..., dry_run=True)` — decisions are computed and
# journaled but nothing is applied.
__all__ = ["Orchestrator", "BuildFixLoop", "LoopBudget", "NullKnowledgeBase", "LazyFixer", "MissingComponentError",
           "lazy_attr"]
