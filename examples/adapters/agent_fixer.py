"""Example tier-2 adapter: hand each error case to an in-house coding agent instead of sbmig's LLMFixer.

Wire it in (config yml):

    fix:
      plugin: examples.adapters.agent_fixer:make_fixer
      plugin_options:
        agent: code-migrator          # whatever your framework needs

The seam (`ScriptFixerLike`): `propose_script(ws, cluster, ctx) -> ScriptProposal`. The agent gets the same packed
case context sbmig's own LLM gets (`ContextPacker.pack_script`, rendered as markdown) and answers in the script
protocol — `<analysis>…</analysis>` + `<script>def fix(p): …</script>` — which `sbmig.fix.script_protocol` parses.
sbmig owns everything around it: clustering by case, the static check of the script, running it in a separate process
against the project folder only, commits / rollbacks, rebuilds, the KB script store and the report. The agent never
edits files itself.

`MyAgent.run(task)` is a stand-in — replace it with a call into your agent framework (or an HTTP endpoint).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sbmig.core.interfaces import FixContext
from sbmig.core.models import ErrorCluster, LLMUsage, ScriptProposal
from sbmig.core.workspace import Workspace
from sbmig.fix import ContextPacker, parse_script_response
from sbmig.fix.fixer import script_allowed_imports, script_helper_api_doc

log = logging.getLogger("agent_fixer")


class MyAgent:
    """STUB — replace with your framework. Contract used below:

        run(task: dict) -> {"answer": "<analysis>…</analysis><script>def fix(p): …</script>",
                            "usage": {"model": str, "calls": int, "input_tokens": int, "output_tokens": int}}

    task = {"instructions": str, "context": str (markdown), "mode": "new" | "augment", "base_script": str,
            "case_id": str, "project_root": str (read-only view; do NOT edit files — return a script)}"""

    def __init__(self, name: str = "code-migrator", llm: Any = None):
        self.name = name
        self.llm = llm                     # sbmig's LLMClientLike (None when no provider is configured)

    def run(self, task: dict) -> dict:     # pragma: no cover - replaced by the real agent
        return {"answer": f"<analysis>{self.name}: stub agent, no script</analysis>"}


INSTRUCTIONS = (
    "Fix every build error of this case (JDK 21 + Spring Boot 4.1 migration) with ONE Python fix script: answer "
    "with <analysis>what broke and why</analysis> and <script> holding a module-level `def fix(p)`. The script edits "
    "the project only through the helper API `p` below, may import only {imports}, is idempotent, and with mode "
    "\"augment\" is the COMPLETE new version of `base_script`.\n\n{helper_api}"
)


class AgentFixer:
    """ScriptFixerLike: propose_script() packs the case → agent → ScriptProposal (never raises)."""

    def __init__(self, cfg: Any, agent: MyAgent, *, notes_dir: Optional[str] = None):
        self.cfg = cfg
        self.agent = agent
        self.packer = ContextPacker(cfg, notes_dir)    # same errors/files/hints sbmig's own LLM gets
        extra = tuple(cfg.script.extra_allowed_imports) if cfg is not None else ()
        self.instructions = INSTRUCTIONS.format(imports=", ".join(script_allowed_imports(extra)),
                                                helper_api=script_helper_api_doc(extra) or "(see sbmig.script)")

    def propose_script(self, ws: Workspace, cluster: ErrorCluster, ctx: FixContext) -> ScriptProposal:
        base = ctx.script_base
        try:
            packed = self.packer.pack_script(ws, cluster, ctx, ctx.profile_summary)
            out = self.agent.run({
                "instructions": self.instructions,
                "context": packed.render(),
                "mode": "augment" if base is not None else "new",
                "base_script": base.code if base is not None else "",
                "case_id": ctx.case_id or cluster.case_id or cluster.id,
                "escalate": ctx.use_escalation_model,
                "project_root": str(ws.root),
            }) or {}
        except Exception as exc:
            # "llm-error:" analyses are counted as transport failures → llm_unavailable after repeats
            return ScriptProposal(analysis=f"llm-error: agent failed: {type(exc).__name__}: {exc}",
                                  model=self.agent.name, error=f"agent failed ({type(exc).__name__})")
        answer = str(out.get("answer") or "")
        parsed = parse_script_response(answer)
        u = out.get("usage") or {}
        model = str(u.get("model") or self.agent.name)
        return ScriptProposal(
            code=parsed.code, analysis=parsed.analysis, model=model, raw_response=answer,
            error="" if parsed.code else (parsed.problem or "no <script> block"),
            llm_usage=LLMUsage(model=model, calls=int(u.get("calls", 1)), input_tokens=int(u.get("input_tokens", 0)),
                               output_tokens=int(u.get("output_tokens", 0))))


def make_fixer(options: dict, cfg: Any, llm: Any = None, notes_dir: Optional[str] = None) -> AgentFixer:
    """Factory referenced by `fix.plugin`. options = fix.plugin_options; llm = sbmig's client or None."""
    opts = dict(options or {})
    return AgentFixer(cfg, MyAgent(opts.get("agent", "code-migrator"), llm=llm), notes_dir=notes_dir)
