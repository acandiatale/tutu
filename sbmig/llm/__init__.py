"""sbmig.llm — adapter seam for in-house LLM gateways and coding agents (docs/ADAPTERS.md).

Tier 1 (LLM): `llm.provider: plugin` + `llm.plugin: "pkg.module:attr"` (or an entry point in `sbmig.llm_adapters`)
    → an `LLMAdapterLike` (`complete(request: LLMRequest) -> LLMResponse`, wrapped in `AdapterClient`) or an
    `LLMClientLike` (message-based, used as is).
Tier 2 (fixer): `fix.plugin: "pkg.module:attr"` → a `FixerLike` (`propose` [+ `apply`]) replacing LLMFixer.
LangChain (`llm.provider: langchain`): `ChatPostModel` (the gateway as a BaseChatModel), `LangChainClient`
    (LLMClientLike over any BaseChatModel), `resolve_langchain_model(cfg)` — imported lazily from `.langchain`.
"""
from typing import Any

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
    "ChatPostModel", "LangChainClient", "resolve_langchain_model",
]

_LANGCHAIN_EXPORTS = ("ChatPostModel", "LangChainClient", "resolve_langchain_model")


def __getattr__(name: str) -> Any:
    # lazy: importing langchain_core (and sbmig.fix) only when the LangChain tier is used
    if name in _LANGCHAIN_EXPORTS:
        from . import langchain as _lc

        return getattr(_lc, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
