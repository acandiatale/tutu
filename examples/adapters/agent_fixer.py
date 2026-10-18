"""Example tier-2 adapter: hand each error cluster to an in-house coding agent instead of sbmig's LLMFixer.

Wire it in (config yml):

    fix:
      plugin: examples.adapters.agent_fixer:make_fixer
      plugin_options:
        agent: code-migrator          # whatever your framework needs
        max_files: 6

sbmig still owns everything around the fix: clustering, triage, deterministic handlers, KB, budgets, git
commits/rollbacks, rebuilds and the report. The agent only answers "what should change for this cluster?".

`MyAgent.run(task)` is a stand-in — replace it with a call into your agent framework.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sbmig.core.interfaces import FixContext
from sbmig.core.models import ErrorCluster, FileChange, FixProposal, LLMUsage
from sbmig.core.workspace import Workspace
from sbmig.fix import ContextPacker, PatchApplier

log = logging.getLogger("agent_fixer")


class MyAgent:
    """STUB — replace with your framework. Contract used below:

        run(task: dict) -> {"analysis": str, "confidence": float,
                            "changes": [{"path": str, "search": str, "replace": str}, ...],
                            "usage": {"model": str, "calls": int, "input_tokens": int, "output_tokens": int}}

    task = {"instructions": str, "context": str (markdown), "files": [paths], "cluster_id": str,
            "project_root": str (read-only view; do NOT edit files directly — return changes)}"""

    def __init__(self, name: str = "code-migrator", llm: Any = None):
        self.name = name
        self.llm = llm                     # sbmig's LLMClientLike (None when no provider is configured)

    def run(self, task: dict) -> dict:     # pragma: no cover - replaced by the real agent
        return {"analysis": f"{self.name}: stub agent, no changes", "confidence": 0.0, "changes": []}


INSTRUCTIONS = (
    "Fix the build errors below for a migration to JDK 21 + Spring Boot 4.1. Return minimal edits as "
    "search/replace pairs; `search` must be copied exactly from the current file and match once. Fix only this "
    "cluster; do not reformat unrelated code."
)


class AgentFixer:
    """FixerLike: propose() packs context → agent → FixProposal. apply() uses sbmig's atomic PatchApplier
    (omit it and sbmig adds the same delegation automatically)."""

    def __init__(self, cfg: Any, agent: MyAgent, *, max_files: int = 6, notes_dir: Optional[str] = None):
        self.cfg = cfg
        self.agent = agent
        self.max_files = max_files
        self.packer = ContextPacker(cfg, notes_dir)    # same errors/files/hints sbmig's own LLM gets

    def propose(self, ws: Workspace, cluster: ErrorCluster, ctx: FixContext) -> FixProposal:
        packed = self.packer.pack(ws, cluster, ctx, ctx.profile_summary)
        task = {
            "instructions": INSTRUCTIONS,
            "context": packed.render(),
            "files": packed.files_included[: self.max_files],
            "cluster_id": cluster.id,
            "phase": ctx.phase.value,
            "iteration": ctx.iteration,
            "escalate": ctx.use_escalation_model,
            "project_root": str(ws.root),
        }
        try:
            out = self.agent.run(task) or {}
        except Exception as exc:
            # "llm-error:" analyses are counted as transport failures → llm_unavailable after repeats
            return FixProposal(changes=[], analysis=f"llm-error: agent failed: {type(exc).__name__}: {exc}",
                               confidence=0.0, model=self.agent.name)
        changes = [FileChange(path=c["path"], op="modify", search=c["search"], replace=c.get("replace", ""))
                   for c in out.get("changes") or [] if c.get("path") and c.get("search")]
        u = out.get("usage") or {}
        return FixProposal(
            changes=changes,
            analysis=str(out.get("analysis") or ""),
            confidence=float(out.get("confidence", 0.5)),
            model=str(u.get("model") or self.agent.name),
            llm_usage=LLMUsage(model=str(u.get("model") or self.agent.name), calls=int(u.get("calls", 1)),
                               input_tokens=int(u.get("input_tokens", 0)),
                               output_tokens=int(u.get("output_tokens", 0))),
        )

    def apply(self, ws: Workspace, changes: list[FileChange]) -> tuple[list[FileChange], list[str]]:
        return PatchApplier(ws).apply(changes)


def make_fixer(options: dict, cfg: Any, llm: Any = None, notes_dir: Optional[str] = None) -> AgentFixer:
    """Factory referenced by `fix.plugin`. options = fix.plugin_options; llm = sbmig's client or None."""
    opts = dict(options or {})
    agent = MyAgent(opts.get("agent", "code-migrator"), llm=llm)
    return AgentFixer(cfg, agent, max_files=int(opts.get("max_files", 6)), notes_dir=notes_dir)
