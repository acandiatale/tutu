"""sbmig.llm — adapter seam for in-house LLM gateways and coding agents (docs/ADAPTERS.md).

Tier 1 (LLM): `llm.provider: plugin` + `llm.plugin: "pkg.module:attr"` (or an entry point in `sbmig.llm_adapters`)
    → an `LLMAdapterLike` (`complete(request: LLMRequest) -> LLMResponse`, wrapped in `AdapterClient`) or an
    `LLMClientLike` (message-based, used as is).
Tier 2 (fixer): `fix.plugin: "pkg.module:attr"` → a `FixerLike` (`propose` [+ `apply`]) replacing LLMFixer.
"""
from .adapter import AdapterClient, KwargFilterClient, LLMAdapter, coerce_response, is_request_adapter
from .registry import (
    FIXER_ENTRY_POINT_GROUP,
    LLM_ENTRY_POINT_GROUP,
    ApplyDelegatingFixer,
    PluginLoadError,
    instantiate,
    load_entry_point,
    load_plugin,
    resolve_fixer_plugin,
    resolve_llm_plugin,
    wrap_llm,
)

__all__ = [
    "AdapterClient", "LLMAdapter", "KwargFilterClient", "coerce_response", "is_request_adapter",
    "PluginLoadError", "load_plugin", "load_entry_point", "instantiate", "resolve_llm_plugin",
    "resolve_fixer_plugin", "wrap_llm", "ApplyDelegatingFixer", "LLM_ENTRY_POINT_GROUP", "FIXER_ENTRY_POINT_GROUP",
]
