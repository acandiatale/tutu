"""sbmig.loop — orchestrator + build-fix loop (the orchestrator owns the loop; done = BUILD SUCCESS). v2 (flow-v2):
the fix loop runs LLM fix scripts per case (L2), see `fixloop` / `sbmig.triage.scripts`."""
from .budget import LoopBudget
from .components import LazyFixer, LazyScriptRunner, MissingComponentError, NullKnowledgeBase, ScriptFixerMissing, lazy_attr
from .fixloop import BuildFixLoop
from .orchestrator import STOP_AFTER_PHASES, Orchestrator, parse_stop_after

# DryRun mode: `Orchestrator(...).run(dry_run=True)` / `BuildFixLoop(..., dry_run=True)` — decisions are computed and
# journaled but nothing is applied.
__all__ = ["Orchestrator", "BuildFixLoop", "LoopBudget", "NullKnowledgeBase", "LazyFixer", "MissingComponentError",
           "LazyScriptRunner", "ScriptFixerMissing", "STOP_AFTER_PHASES", "parse_stop_after",
           "lazy_attr"]
