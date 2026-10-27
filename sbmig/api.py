"""Programmatic entry point: run sbmig from your own code / agent framework (docs/ADAPTERS.md).

    from sbmig.api import migrate, exit_code
    report = migrate("path/to/project", configs=["config/artifacts.yml"], llm=MyGatewayAdapter())
    print(report.success, exit_code(report))

Imports are lazy (like the CLI) so `import sbmig.api` is cheap.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Optional, Sequence, Union

if TYPE_CHECKING:  # pragma: no cover
    from sbmig.core.interfaces import KnowledgeBaseLike, LLMAdapterLike, LLMClientLike, ScriptFixerLike
    from sbmig.core.models import RunReport

# `kb=False` here and `sbmig run --no-kb`: the knowledge base is neither read nor fed — the KB object is a
# NullKnowledgeBase (no stored fix script is reused, nothing is recorded); the run's own errors.jsonl / errors.md are
# still written to the run dir. v2.1 has no KB switches left in the config, so nothing is overridden.
NO_KB_OVERRIDES: dict = {}
_NO_KB_OVERRIDES = NO_KB_OVERRIDES          # the pre-round-10 name


def _merge(base: dict, extra: Mapping) -> dict:
    out = dict(base)
    for k, v in extra.items():
        if isinstance(v, Mapping) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def migrate(
    input: Union[str, Path],
    *,
    configs: Sequence[Union[str, Path]] = (),
    out: Union[str, Path] = "out",
    run_id: Optional[str] = None,
    overrides: Optional[dict] = None,
    llm: "LLMClientLike | LLMAdapterLike | None" = None,
    fixer: "ScriptFixerLike | None" = None,
    kb: "KnowledgeBaseLike | None | bool" = None,
    stop_after: Optional[str] = None,
    dry_run: bool = False,
    repo_root: Optional[Union[str, Path]] = None,
    components: Optional[Mapping[str, Any]] = None,
) -> "RunReport":
    """Migrate one project (directory or zip) end to end — same pipeline as `sbmig run`.

    llm        an LLMClientLike (message-based) or an LLMAdapterLike (`complete(request)`, wrapped in
               AdapterClient automatically). None → built from config (`llm.provider`, incl. plugins).
               Recorded to `llm.record_dir` when that is set.
    fixer      a fixer with `propose_script(ws, cluster, ctx) -> ScriptProposal` (ScriptFixerLike) replacing
               LLMFixer (takes precedence over `fix.plugin`).
    kb         None/True → the file knowledge base; False → no KB (like --no-kb); an object → used as is.
    stop_after intake|analyze|plan|transform|restructure (v2; jdk21|spring are v1 phases).
    components advanced: extra Orchestrator collaborators (build_runner, rewrite_runner, restructurer, …).
    Returns the RunReport; `exit_code(report)` maps it to the CLI exit code (0 / 2 / 3)."""
    from sbmig.core.config import load_config
    from sbmig.core.workspace import Workspace
    from sbmig.loop import NullKnowledgeBase, Orchestrator, parse_stop_after

    phase = parse_stop_after(stop_after)        # ValueError names the accepted values
    ov: dict = dict(overrides or {})
    if kb is False:
        ov = _merge(ov, {"kb": NO_KB_OVERRIDES})
    cfg = load_config(*configs, overrides=ov or None, repo_root=Path(repo_root) if repo_root else None)

    client: Any = None
    if llm is not None:
        from sbmig.fix.client import RecordingClient
        from sbmig.llm import wrap_llm

        client = wrap_llm(llm, cfg)
        if cfg.llm.record_dir and not isinstance(client, RecordingClient):
            client = RecordingClient(client, cfg.resolve_path(cfg.llm.record_dir))
    kb_obj: Any = None
    if kb is False:
        kb_obj = NullKnowledgeBase()
    elif kb is not None and kb is not True:
        kb_obj = kb

    ws = Workspace.from_input(input, out, run_id=run_id)
    extra = dict(components or {})
    orch = Orchestrator(cfg, ws, llm=client, fixer=fixer, kb=kb_obj, **extra)
    return orch.run(stop_after=phase, dry_run=dry_run)


def exit_code(report: "RunReport") -> int:
    """0 = final build green and nothing blocked, 2 = finished with blocked/unresolved errors, 3 = aborted."""
    from sbmig.cli import run_exit_code

    return run_exit_code(report)


__all__ = ["migrate", "exit_code", "NO_KB_OVERRIDES"]
