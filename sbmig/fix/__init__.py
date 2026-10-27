"""sbmig.fix — LLM fix module (see docs/INTERFACES.md for the public entry points).

    make_llm_client(cfg) -> LLMClientLike             provider switch: company | openai_compatible | anthropic | fake
                                                      | plugin / entry-point name (→ sbmig.llm, docs/ADAPTERS.md)
    LLMFixer(cfg, client, notes_dir=None)             ScriptFixerLike: propose_script(ws, cluster, ctx)
                                                      -> FixAnswer (= ScriptProposal): analysis 원인, solution
                                                         해결 방법, edits (apply: sbmig.script.apply_edits), code
                                                         (the script), no_script_reason, error, …
    ContextPacker(cfg).pack_script(ws, cluster, ctx, ctx.profile_summary)
                                                      -> PackedContext; fixer plugins can reuse it for the same
                                                         errors/files/hints our LLM gets (.render() → markdown)
    parse_script_response(text)                       -> ParsedScript(code, analysis, solution, edits, edits_problem,
                                                         no_script_reason, need_files, need_grep, problem, …)
    parse_edits(text)                                 -> (list[Edit], problem) of one <edits> block (lenient JSON)
    ReplayResponder(load_replay_entries(dir))         FakeLLMClient answers picked by `match` / `replay_role`
"""
from .company import CompanyClient
from .client import (
    AnthropicClient,
    FakeLLMClient,
    LLMConfigError,
    LLMError,
    OpenAICompatibleClient,
    extract_reply,
    RecordingClient,
    ReplayEntry,
    ReplayResponder,
    load_replay_entries,
    load_replay_responses,
    make_llm_client,
    messages_hash,
    usage_from,
)
from .context import ContextPacker, NoteEntry, PackedContext, load_notes_index
from .fixer import LLMFixer, load_script_prompts, profile_summary
from .script_protocol import ParsedScript, normalize_path, parse_edits, parse_script_response

__all__ = [
    "make_llm_client", "LLMFixer", "FakeLLMClient", "ContextPacker", "load_notes_index", "AnthropicClient",
    "CompanyClient", "extract_reply", "OpenAICompatibleClient", "RecordingClient", "LLMError", "LLMConfigError",
    "usage_from", "messages_hash", "load_replay_responses", "PackedContext", "NoteEntry", "profile_summary",
    "load_script_prompts", "ParsedScript", "parse_script_response", "parse_edits", "normalize_path", "ReplayEntry",
    "ReplayResponder", "load_replay_entries",
]
