"""sbmig.llm — 사내 LLM 게이트웨이·사내 코딩 에이전트를 끼우는 어댑터 접점(플러그인 seam, docs/ADAPTERS.md).
1단(tier 1, LLM): `llm.provider: plugin` + `llm.plugin`(또는 entry point `sbmig.llm_adapters`) 으로, 요청
하나를 받아 응답 하나를 돌려주는 어댑터(`complete(request: LLMRequest) -> LLMResponse`, AdapterClient 로
감쌈)나 메시지 기반 클라이언트를 끼운다. 2단(tier 2, fixer): `fix.plugin` 으로 LLMFixer 대신
`propose_script(ws, cluster, ctx) -> ScriptProposal` 을 가진 사내 에이전트를 끼운다. 내장 provider(`company`
기본, `openai_compatible`, `anthropic`, `fake`)는 `sbmig.fix.client.make_llm_client` 가 만들고, 이 패키지는
내장이 아닌 것만 맡는다. 불변조건 4: 이 두 계약은 바꾸지 않는다 — 회사의 LLM 게이트웨이·에이전트 프레임워크가
새 HTTP 클라이언트 없이 여기에 붙는다.

sbmig.llm — adapter seam for in-house LLM gateways and coding agents (docs/ADAPTERS.md).

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
