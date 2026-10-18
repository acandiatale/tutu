"""Adapter tier: wrap an in-house `LLMAdapterLike` (one LLMRequest in, one LLMResponse out) as an LLMClientLike.

    AdapterClient(adapter, default_model=None, base_metadata=None)   -> LLMClientLike used everywhere in sbmig
    LLMAdapter                                                        optional ABC for plugin authors
"""
from __future__ import annotations

import abc
import inspect
import logging
from typing import Any, Optional, get_args

from sbmig.core.interfaces import ChatMessage, LLMRequest, LLMResponse, LLMRole

log = logging.getLogger("sbmig.llm")

KNOWN_ROLES: tuple[str, ...] = tuple(get_args(LLMRole))


def _llm_error(message: str, body: str = "") -> Exception:
    from sbmig.fix.client import LLMError  # lazy: sbmig.fix imports sbmig.llm lazily too

    return LLMError(None, body, message)


class LLMAdapter(abc.ABC):
    """Convenience base class for in-house gateway adapters.

    Subclass it, implement `complete(request)` and point `llm.plugin` at the class (or at a factory returning an
    instance). Raise an exception on failure — sbmig converts it to `LLMError` so the build-fix loop can mark the
    LLM as unavailable after repeated failures instead of crashing."""

    adapter_kind = "request"

    @property
    def name(self) -> str:
        return type(self).__name__

    @abc.abstractmethod
    def complete(self, request: LLMRequest) -> LLMResponse:  # pragma: no cover - abstract
        ...

    def close(self) -> None:
        return None


def is_request_adapter(obj: Any) -> bool:
    """True when `obj.complete` takes one LLMRequest (adapter tier), False when it is message-based (client tier).

    Order: LLMAdapter subclass → explicit `adapter_kind` attribute ("request" | "client") → signature of
    `complete` (first positional parameter named `request` or annotated `LLMRequest`) → client."""
    if isinstance(obj, LLMAdapter):
        return True
    kind = getattr(obj, "adapter_kind", None)
    if kind == "request":
        return True
    if kind in ("client", "messages"):
        return False
    complete = getattr(obj, "complete", None)
    if not callable(complete):
        return False
    try:
        sig = inspect.signature(complete)
    except (TypeError, ValueError):
        return False
    params = [p for p in sig.parameters.values()
              if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)]
    if not params:
        return False
    first = params[0]
    ann = first.annotation
    if ann is LLMRequest:
        return True
    if isinstance(ann, str) and ann.split(".")[-1].strip("'\"") == "LLMRequest":
        return True
    return first.name in ("request", "req", "llm_request")


def coerce_response(raw: Any, fallback_model: str, source: str = "adapter") -> LLMResponse:
    """LLMResponse | dict | str | object with `.content` → LLMResponse; anything else → LLMError."""
    if isinstance(raw, LLMResponse):
        resp = raw
    elif isinstance(raw, str):
        resp = LLMResponse(content=raw, model=fallback_model)
    elif isinstance(raw, dict):
        if not isinstance(raw.get("content"), str):
            raise _llm_error(f"LLM {source} returned a dict without a string 'content'", repr(raw)[:500])
        data = {k: raw[k] for k in ("content", "model", "input_tokens", "output_tokens", "finish_reason")
                if raw.get(k) is not None}
        data.setdefault("model", fallback_model)
        try:
            resp = LLMResponse.model_validate(data)
        except Exception as exc:
            raise _llm_error(f"LLM {source} returned an invalid response dict: {exc}", repr(raw)[:500]) from exc
    elif raw is not None and isinstance(getattr(raw, "content", None), str):
        resp = LLMResponse(
            content=raw.content,
            model=str(getattr(raw, "model", "") or fallback_model),
            input_tokens=int(getattr(raw, "input_tokens", 0) or 0),
            output_tokens=int(getattr(raw, "output_tokens", 0) or 0),
            finish_reason=getattr(raw, "finish_reason", None),
        )
    else:
        raise _llm_error(f"LLM {source} returned {type(raw).__name__}, expected LLMResponse "
                         "(or a dict / object with a string 'content')", repr(raw)[:500])
    if not resp.model:
        resp = resp.model_copy(update={"model": fallback_model})
    return resp


class AdapterClient:
    """LLMClientLike over an LLMAdapterLike. Builds one LLMRequest per call and validates the answer."""

    def __init__(self, adapter: Any, *, default_model: Optional[str] = None, base_metadata: Optional[dict] = None):
        if not callable(getattr(adapter, "complete", None)):
            raise TypeError(f"{type(adapter).__name__} has no complete(request) method")
        self.adapter = adapter
        self.default_model = default_model or None
        self.base_metadata = dict(base_metadata or {})
        self.model = self.default_model or str(getattr(adapter, "model", "") or "")

    @property
    def name(self) -> str:
        n = getattr(self.adapter, "name", None)
        return n if isinstance(n, str) and n else type(self.adapter).__name__

    def __repr__(self) -> str:
        return f"AdapterClient({self.name}, default_model={self.default_model!r})"

    def build_request(self, messages: list[ChatMessage], *, model: Optional[str] = None,
                      max_tokens: Optional[int] = None, temperature: Optional[float] = None,
                      stop: Optional[list[str]] = None, role: Optional[str] = None,
                      metadata: Optional[dict] = None) -> LLMRequest:
        meta = {**self.base_metadata, **(metadata or {})}
        r = role or "other"
        if r not in KNOWN_ROLES:
            meta.setdefault("role_detail", r)
            r = "other"
        return LLMRequest(messages=list(messages), model=model or self.default_model, max_tokens=max_tokens,
                          temperature=temperature, stop=list(stop) if stop else None, role=r, metadata=meta)

    def complete(self, messages: list[ChatMessage], *, model: Optional[str] = None,
                 max_tokens: Optional[int] = None, temperature: Optional[float] = None,
                 stop: Optional[list[str]] = None, role: Optional[str] = None,
                 metadata: Optional[dict] = None, **_ignored: Any) -> LLMResponse:
        from sbmig.fix.client import LLMError

        request = self.build_request(messages, model=model, max_tokens=max_tokens, temperature=temperature,
                                     stop=stop, role=role, metadata=metadata)
        try:
            raw = self.adapter.complete(request)
        except LLMError:
            raise
        except Exception as exc:     # surface as LLMError so callers treat it as a transport failure
            status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
            raise LLMError(status if isinstance(status, int) else None, str(exc)[:500],
                           f"LLM adapter {self.name} failed: {type(exc).__name__}: {exc}") from exc
        return coerce_response(raw, request.model or self.model or self.name, source=f"adapter {self.name}")

    def close(self) -> None:
        fn = getattr(self.adapter, "close", None)
        if callable(fn):
            fn()


class KwargFilterClient:
    """Wraps a message-based plugin client whose `complete` does not accept every keyword sbmig passes
    (e.g. no `role` / `metadata`): unknown keywords are dropped instead of raising TypeError."""

    def __init__(self, inner: Any):
        self.inner = inner
        self.model = str(getattr(inner, "model", "") or "")
        self._accepted: Optional[set[str]] = None
        try:
            params = inspect.signature(inner.complete).parameters.values()
        except (TypeError, ValueError):
            params = None
        if params is not None and not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params):
            self._accepted = {p.name for p in params}

    @staticmethod
    def needed(inner: Any) -> bool:
        try:
            params = inspect.signature(inner.complete).parameters.values()
        except (TypeError, ValueError):
            return False
        names = {p.name for p in params}
        has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params)
        return not has_var_kw and not {"role", "metadata"} <= names

    def complete(self, messages: list[ChatMessage], **kw: Any) -> LLMResponse:
        if self._accepted is not None:
            kw = {k: v for k, v in kw.items() if k in self._accepted}
        raw = self.inner.complete(messages, **kw)
        return coerce_response(raw, kw.get("model") or self.model or type(self.inner).__name__,
                               source=f"client {type(self.inner).__name__}")

    def close(self) -> None:
        fn = getattr(self.inner, "close", None)
        if callable(fn):
            fn()


__all__ = ["AdapterClient", "LLMAdapter", "KwargFilterClient", "is_request_adapter", "coerce_response",
           "KNOWN_ROLES"]
