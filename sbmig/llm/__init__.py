"""사내 LLM 게이트웨이와 사내 코딩 에이전트를 sbmig에 연결하는 어댑터 패키지(docs/ADAPTERS.md).

Built-in providers are made by `sbmig.fix.client.make_llm_client`; this package handles only the others. The
two interfaces below never change: the company gateway and agent framework plug in here, without a new HTTP
client.

Tier 1 (LLM): `llm.provider: plugin` + `llm.plugin: "pkg.module:attr"` (or an entry point in `sbmig.llm_adapters`)
    → an `LLMAdapterLike` (`complete(request: LLMRequest) -> LLMResponse`, wrapped in `AdapterClient`) or an
    `LLMClientLike` (message-based, used as is).
Tier 2 (fixer): `fix.plugin: "pkg.module:attr"` → a fixer with `propose_script(ws, cluster, ctx) -> ScriptProposal`
    (the script seam, `ScriptFixerLike`) replacing LLMFixer.
"""
from .adapter import AdapterClient, KwargFilterClient, LLMAdapter, coerce_response, is_request_adapter
from .registry import (
    FIXER_ENTRY_POINT_GROUP,
    LLM_ENTRY_POINT_GROUP,
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
    "resolve_fixer_plugin", "wrap_llm", "LLM_ENTRY_POINT_GROUP", "FIXER_ENTRY_POINT_GROUP",
]
