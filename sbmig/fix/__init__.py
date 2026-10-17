"""sbmig.fix — LLM fix module (see docs/INTERFACES.md for the public entry points).

    make_llm_client(cfg) -> LLMClientLike             provider switch: openai_compatible | anthropic | fake
    LLMFixer(cfg, client, notes_dir=None)             FixerLike: propose(ws, cluster, ctx) / apply(ws, changes)
    PatchApplier(ws).apply(changes)                   -> (applied, errors), atomic per file
    parse_fix_response(text)                          -> ParsedResponse(changes, analysis, need_files, need_grep, …)
"""
from .apply import BlockFailure, PatchApplier
from .client import (
    AnthropicClient,
    FakeLLMClient,
    LLMConfigError,
    LLMError,
    OpenAICompatibleClient,
    RecordingClient,
    load_replay_responses,
    make_llm_client,
    messages_hash,
    usage_from,
)
from .context import ContextPacker, NoteEntry, PackedContext, load_notes_index
from .fixer import LLMFixer, load_prompts, profile_summary
from .protocol import ParsedResponse, parse_fix_response, render_protocol_instructions

__all__ = [
    "make_llm_client", "LLMFixer", "PatchApplier", "parse_fix_response", "FakeLLMClient", "ContextPacker",
    "load_notes_index", "AnthropicClient", "OpenAICompatibleClient", "RecordingClient", "LLMError", "LLMConfigError",
    "usage_from", "messages_hash", "load_replay_responses", "ParsedResponse", "render_protocol_instructions",
    "PackedContext", "NoteEntry", "BlockFailure", "profile_summary", "load_prompts",
]
