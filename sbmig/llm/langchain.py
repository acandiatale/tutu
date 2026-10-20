"""LangChain tier (company convention: agents are built on LangGraph / LangChain).

    ChatPostModel(BaseChatModel)              the in-house gateway (`POST url`, Bearer token,
                                              `{"model", "messages", **payload_extra}`) as a LangChain chat model
    LangChainClient(model, default_model)     LLMClientLike over ANY BaseChatModel (role → tags, metadata → metadata)
    resolve_langchain_model(cfg)              `llm.langchain_model` factory / instance, else ChatPostModel from cfg.llm

`make_llm_client` uses these for `llm.provider: langchain`; `RecordingClient` still wraps the result, and
`LLMRequest` / `LLMResponse` / `FixerLike` are unchanged (docs/ADAPTERS.md, "LangChain models").
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)
from langchain_core.messages import ChatMessage as LCChatMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field, PrivateAttr, SecretStr

from sbmig.core.config import Config
from sbmig.core.interfaces import ChatMessage, LLMResponse
from sbmig.fix.client import ChatPostClient, LLMConfigError, LLMError, _unexpanded

log = logging.getLogger("sbmig.llm.langchain")

_ROLE_ALIASES = {"system": "system", "user": "user", "human": "user", "assistant": "assistant", "ai": "assistant",
                 "developer": "system"}


# ---------------------------------------------------------------------------
# message conversion
# ---------------------------------------------------------------------------
def content_text(content: Any) -> str:
    """LangChain message content (str or a list of blocks) → plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type", "text") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return "" if content is None else str(content)


def _lc_role(message: BaseMessage) -> str:
    if isinstance(message, SystemMessage):
        return "system"
    if isinstance(message, HumanMessage):
        return "user"
    if isinstance(message, AIMessage):
        return "assistant"
    if isinstance(message, LCChatMessage):
        return _ROLE_ALIASES.get(message.role.lower(), "user")
    return _ROLE_ALIASES.get(getattr(message, "type", ""), "user")


def to_sbmig_messages(messages: list[BaseMessage]) -> list[ChatMessage]:
    return [ChatMessage(role=_lc_role(m), content=content_text(m.content)) for m in messages]  # type: ignore[arg-type]


def to_lc_messages(messages: list[ChatMessage]) -> list[BaseMessage]:
    out: list[BaseMessage] = []
    for m in messages:
        if m.role == "system":
            out.append(SystemMessage(content=m.content))
        elif m.role == "assistant":
            out.append(AIMessage(content=m.content))
        elif m.role == "user":
            out.append(HumanMessage(content=m.content))
        else:  # pragma: no cover - ChatMessage.role is a Literal today
            out.append(LCChatMessage(role=m.role, content=m.content))
    return out


# ---------------------------------------------------------------------------
# ChatPostModel: the in-house gateway as a LangChain chat model
# ---------------------------------------------------------------------------
class ChatPostModel(BaseChatModel):
    """`POST url` with `{"model", "messages", **payload_extra}` and `Authorization: Bearer <api_key>`.

    Same wire format and retry/backoff/status semantics as provider `chat_post` (it reuses
    `sbmig.fix.client.ChatPostClient`); the reply text comes from `response_path` or is auto-detected
    (`sbmig.fix.client.extract_reply`). `max_tokens` / `temperature` / `stop` are sent only when
    `generation_params` is true (the gateway may reject unknown keys)."""

    url: str
    api_key: SecretStr = Field(repr=False)
    model: str
    payload_extra: dict[str, Any] = Field(default_factory=dict)
    generation_params: bool = False
    response_path: str | None = None
    timeout: float = 180.0
    max_retries: int = 3
    extra_headers: dict[str, str] = Field(default_factory=dict)
    max_tokens: int = 8000                       # sent only with generation_params
    temperature: float | None = 0.0           # sent only with generation_params
    transport: httpx.BaseTransport | None = Field(default=None, repr=False, exclude=True)   # tests
    http_backend: str = "httpx"                      # httpx | requests (requests.post(url, headers=, json=, timeout=))
    verify_ssl: bool | str = True                    # False or a CA bundle path for corporate TLS interception

    _client: ChatPostClient | None = PrivateAttr(default=None)

    @property
    def _llm_type(self) -> str:
        return "sbmig-chat-post"

    @property
    def _identifying_params(self) -> dict[str, Any]:   # never the key
        return {"url": self.url, "model": self.model, "payload_extra": dict(self.payload_extra),
                "generation_params": self.generation_params, "response_path": self.response_path}

    def __repr__(self) -> str:
        return f"ChatPostModel(url={self.url!r}, model={self.model!r})"

    __str__ = __repr__

    def _http_client(self) -> ChatPostClient:
        if self._client is None:
            self._client = ChatPostClient(
                self.url, self.api_key.get_secret_value(), self.model, payload_extra=self.payload_extra,
                generation_params=self.generation_params, response_path=self.response_path,
                timeout=float(self.timeout), max_retries=self.max_retries, extra_headers=self.extra_headers,
                default_max_tokens=self.max_tokens, default_temperature=self.temperature, transport=self.transport,
                http_backend=self.http_backend, verify=self.verify_ssl)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None,
                  run_manager: CallbackManagerForLLMRun | None = None, **kwargs: Any) -> ChatResult:
        model = kwargs.get("model") or self.model
        resp: LLMResponse = self._http_client().complete(
            to_sbmig_messages(messages), model=model, max_tokens=kwargs.get("max_tokens"),
            temperature=kwargs.get("temperature"), stop=stop)   # ChatPostClient drops these unless generation_params
        usage = {"input_tokens": resp.input_tokens, "output_tokens": resp.output_tokens,
                 "total_tokens": resp.input_tokens + resp.output_tokens}
        msg = AIMessage(content=resp.content, usage_metadata=usage,  # type: ignore[arg-type]
                        response_metadata={"model": resp.model, "finish_reason": resp.finish_reason})
        return ChatResult(generations=[ChatGeneration(message=msg)], llm_output={"model": resp.model})


# ---------------------------------------------------------------------------
# LangChainClient: LLMClientLike over any BaseChatModel
# ---------------------------------------------------------------------------
class LangChainClient:
    """sbmig's message-based client seam (`LLMClientLike`) over a LangChain `BaseChatModel`.

    `role` → `config["tags"]`, `metadata` → `config["metadata"]`, `stop` honoured; `model` / `max_tokens` /
    `temperature` are passed as invoke kwargs (retried without them on TypeError). Token counts come from
    `AIMessage.usage_metadata`. Exceptions other than `LLMError` are raised as `LLMError` so the build-fix loop
    treats them as transport failures."""

    provider = "langchain"

    def __init__(self, model: Any, default_model: str | None = None):
        if not callable(getattr(model, "invoke", None)):
            raise TypeError(f"{type(model).__name__} is not a LangChain chat model (no invoke())")
        self.chat_model = model
        self.default_model = default_model or None
        self.model = (self.default_model or str(getattr(model, "model", "") or "")
                      or str(getattr(model, "model_name", "") or "") or self.name)

    @property
    def name(self) -> str:
        t = getattr(self.chat_model, "_llm_type", None)
        return t if isinstance(t, str) and t else type(self.chat_model).__name__

    def __repr__(self) -> str:
        return f"LangChainClient({self.chat_model!r}, default_model={self.default_model!r})"

    def complete(self, messages: list[ChatMessage], *, model: str | None = None,
                 max_tokens: int | None = None, temperature: float | None = None,
                 stop: list[str] | None = None, role: str | None = None,
                 metadata: dict | None = None, **_ignored: Any) -> LLMResponse:
        lc_messages = to_lc_messages(messages)
        r = role or "other"
        config: dict[str, Any] = {"tags": [r], "metadata": {**(metadata or {}), "sbmig_role": r},
                                  "run_name": f"sbmig-{r}"}
        gen: dict[str, Any] = {}
        if model and model != self.model:                # role-specific override (recipe / escalation model)
            gen["model"] = model
        if max_tokens is not None:
            gen["max_tokens"] = max_tokens
        if temperature is not None:
            gen["temperature"] = temperature
        stop_list = list(stop) if stop else None
        try:
            try:
                out = self.chat_model.invoke(lc_messages, config=config, stop=stop_list, **gen)
            except TypeError:
                if not gen:
                    raise
                log.debug("chat model %s rejected %s; retrying without them", self.name, sorted(gen))
                out = self.chat_model.invoke(lc_messages, config=config, stop=stop_list)
        except LLMError:
            raise
        except Exception as exc:     # surface as LLMError so callers treat it as a transport failure
            status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
            raise LLMError(status if isinstance(status, int) else None, str(exc)[:500],
                           f"LangChain model {self.name} failed: {type(exc).__name__}: {exc}") from exc
        return self._to_response(out, model or self.model)

    def _to_response(self, out: Any, fallback_model: str) -> LLMResponse:
        content = getattr(out, "content", out)
        usage = getattr(out, "usage_metadata", None) or {}
        meta = getattr(out, "response_metadata", None) or {}
        resp_model = meta.get("model") or meta.get("model_name") or fallback_model or self.name
        finish = meta.get("finish_reason") or meta.get("stop_reason")
        return LLMResponse(content=content_text(content), model=str(resp_model),
                           input_tokens=int(usage.get("input_tokens") or 0),
                           output_tokens=int(usage.get("output_tokens") or 0),
                           finish_reason=str(finish) if finish is not None else None)

    def close(self) -> None:
        fn = getattr(self.chat_model, "close", None)
        if callable(fn):
            try:
                fn()
            except Exception:  # pragma: no cover - closing must never fail a run
                log.debug("closing %s failed", self.name, exc_info=True)


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------
def resolve_langchain_model(cfg: Config, *, transport: httpx.BaseTransport | None = None) -> BaseChatModel:
    """`llm.langchain_model` ("pkg.module:attr": a BaseChatModel instance, a class, or a factory taking `(cfg)` or
    `()`), else a `ChatPostModel` built from `cfg.llm` (base_url, api_key_env, model, payload_extra, ...)."""
    llm = cfg.llm
    spec = (llm.langchain_model or "").strip()
    if spec:
        from .registry import PluginLoadError, instantiate, load_plugin

        obj = load_plugin(spec)
        if isinstance(obj, type) and issubclass(obj, BaseChatModel):
            try:                                         # a model class: built with its own defaults
                built = obj()
            except Exception as exc:
                raise PluginLoadError(f"llm.langchain_model {spec!r} failed to initialise: "
                                      f"{type(exc).__name__}: {exc}") from exc
        else:                                            # an instance, or a factory taking (cfg) or ()
            built = instantiate(obj, [cfg], required_method="invoke", spec=spec)
        if not isinstance(built, BaseChatModel):
            log.warning("llm.langchain_model %s returned %s, not a BaseChatModel; using it through invoke()",
                        spec, type(built).__name__)
        if not callable(getattr(built, "invoke", None)):  # pragma: no cover - instantiate checks it
            raise PluginLoadError(f"llm.langchain_model {spec!r} has no invoke()")
        log.info("langchain model %s → %s", spec, type(built).__name__)
        return built
    if _unexpanded(llm.base_url):
        raise LLMConfigError("llm.base_url is not set (LLM_BASE_URL: the full endpoint URL)")
    if _unexpanded(llm.model):
        raise LLMConfigError("llm.model is not set (LLM_MODEL)")
    key = os.environ.get(llm.api_key_env or "")
    if not key:
        raise LLMConfigError(
            f"LLM provider 'langchain' needs an API key in the environment variable '{llm.api_key_env}' "
            "(set it, set llm.langchain_model to your own model factory, or use llm.provider=fake)")
    return ChatPostModel(url=llm.base_url, api_key=SecretStr(key), model=llm.model,
                         payload_extra=dict(llm.payload_extra or {}),
                         generation_params=llm.payload_generation_params, response_path=llm.response_path,
                         timeout=float(llm.timeout_seconds), max_retries=llm.max_retries,
                         extra_headers=dict(llm.extra_headers or {}), max_tokens=llm.max_output_tokens,
                         http_backend=llm.http_backend, verify_ssl=llm.verify_ssl,
                         temperature=llm.temperature, transport=transport)


__all__ = [
    "ChatPostModel",
    "LangChainClient",
    "content_text",
    "resolve_langchain_model",
    "to_lc_messages",
    "to_sbmig_messages",
]
