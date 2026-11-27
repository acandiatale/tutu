"""사내 LLM 어댑터(`LLMAdapterLike`)를 sbmig가 쓰는 메시지 기반 클라이언트(`LLMClientLike`)로 감싸는 모듈.

`sbmig.llm.registry.wrap_llm` wraps a plugin in these classes; from then on the fix loop and the structure
review use it like a built-in client. Every exception an adapter raises becomes an `LLMError` whose text
carries `status=… cause=<exception class>`: the fix loop tells transport failures apart by that text only.
An odd return value becomes an exception, never an error sentence passed on as the answer.

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
    from sbmig.fix.client import LLMError  # 순환 import를 피하려고 늦게 import한다. sbmig.fix도 sbmig.llm을 늦게 import한다

    return LLMError(None, body, message)


class LLMAdapter(abc.ABC):
    """사내 게이트웨이 어댑터가 상속하는 베이스 클래스. `complete(request)`만 구현하면 된다.
    `llm.plugin`에 이 클래스나 인스턴스를 만드는 팩토리를 지정한다.
    실패하면 예외를 던지면 된다. sbmig가 LLMError로 바꾸므로 수정 루프는 멈추지 않는다."""

    adapter_kind = "request"

    @property
    def name(self) -> str:
        """로그와 오류 문구에 쓸 어댑터 이름을 반환한다. 기본값은 클래스 이름이다."""
        return type(self).__name__

    @abc.abstractmethod
    def complete(self, request: LLMRequest) -> LLMResponse:  # pragma: no cover - abstract
        """요청 하나를 게이트웨이로 보내고 LLMResponse를 반환한다.
        재시도, 인증, 타임아웃은 플러그인이 맡는다. 실패하면 오류 문장을 content로 반환하지 말고 예외를 던진다."""
        ...

    def close(self) -> None:
        return None


def is_request_adapter(obj: Any) -> bool:
    """`obj.complete`가 LLMRequest 하나를 받는 어댑터인지 판단한다. 메시지 목록을 받는 클라이언트면 False다.
    LLMAdapter 하위 클래스인지, `adapter_kind` 속성, `complete`의 첫 인자를 차례로 본다.

    First positional parameter named `request` / `req` / `llm_request` or annotated `LLMRequest`."""
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
    """플러그인의 반환값을 LLMResponse로 맞춘다. `model`이 비어 있으면 `fallback_model`로 채운다.
    LLMResponse, dict, str, `.content`가 있는 객체를 받는다. 그 밖의 값이면 LLMError를 던진다."""
    if isinstance(raw, LLMResponse):
        resp = raw
    elif isinstance(raw, str):
        resp = LLMResponse(content=raw, model=fallback_model)
    elif isinstance(raw, dict):
        if not isinstance(raw.get("content"), str):
            raise _llm_error(f"LLM {source} returned a dict without a string 'content'", repr(raw)[:500])
        data = {k: raw[k] for k in ("content", "model", "input_tokens", "output_tokens", "finish_reason",
                                    "served_models") if raw.get(k) is not None}
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
    """LLMAdapterLike를 감싸 LLMClientLike로 쓰게 하는 클래스. 호출마다 LLMRequest를 만들고 응답을 검증한다."""

    def __init__(self, adapter: Any, *, default_model: Optional[str] = None, base_metadata: Optional[dict] = None):
        if not callable(getattr(adapter, "complete", None)):
            raise TypeError(f"{type(adapter).__name__} has no complete(request) method")
        self.adapter = adapter
        self.default_model = default_model or None
        self.base_metadata = dict(base_metadata or {})
        self.model = self.default_model or str(getattr(adapter, "model", "") or "")

    @property
    def name(self) -> str:
        """어댑터의 `name`을 반환한다. 문자열이 아니면 어댑터 클래스 이름을 반환한다."""
        n = getattr(self.adapter, "name", None)
        return n if isinstance(n, str) and n else type(self.adapter).__name__

    def __repr__(self) -> str:
        return f"AdapterClient({self.name}, default_model={self.default_model!r})"

    def build_request(self, messages: list[ChatMessage], *, model: Optional[str] = None,
                      max_tokens: Optional[int] = None, temperature: Optional[float] = None,
                      stop: Optional[list[str]] = None, role: Optional[str] = None,
                      metadata: Optional[dict] = None) -> LLMRequest:
        """메시지 호출 인자로 LLMRequest를 만든다.
        ``LLMRole``에 없는 role은 `other`로 바꾼다. 원래 값은 `metadata["role_detail"]`에 남긴다.
        metadata에는 run_id, project 같은 문맥 정보만 담는다. 비밀 값은 넣지 않는다.

        `verify_guide` is a role of its own in `LLMRole`."""
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
        """어댑터를 한 번 호출한다.
        LLMError는 그대로 던진다. 다른 예외는 `status=` / `cause=` 문구가 든 LLMError로 바꿔 던진다.
        응답의 model은 요청한 이름으로 맞춘다."""
        from sbmig.fix.client import LLMError

        request = self.build_request(messages, model=model, max_tokens=max_tokens, temperature=temperature,
                                     stop=stop, role=role, metadata=metadata)
        try:
            raw = self.adapter.complete(request)
        except LLMError:
            raise
        except Exception as exc:     # LLMError로 바꾼다. 호출한 쪽은 전송 장애로 처리한다
            status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
            status = status if isinstance(status, int) else None
            # 이 문구 형식은 바꾸지 않는다. 수정 루프가 `status=` / `cause=`로 장애를 구분한다
            raise LLMError(status, str(exc)[:500],
                           f"LLM adapter {self.name} failed (status={status}, cause={type(exc).__name__}): "
                           f"{exc}") from exc
        from sbmig.fix.client import named_response
        # 모델 이름은 요청한 이름을 쓴다. 플러그인이 알려 준 실제 모델 이름은 served_models로 옮긴다
        resp = coerce_response(raw, request.model or self.model or self.name, source=f"adapter {self.name}")
        return named_response(resp, request.model, fallback=self.model or self.name)

    def close(self) -> None:
        """어댑터에 `close()`가 있으면 호출한다."""
        fn = getattr(self.adapter, "close", None)
        if callable(fn):
            fn()


class KwargFilterClient:
    """sbmig가 넘기는 키워드 인자를 모두 받지 못하는 메시지 기반 플러그인 클라이언트를 감싸는 클래스.
    `role` / `metadata`처럼 받지 못하는 키워드는 버린다. 그대로 넘기면 TypeError가 난다."""

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
        """이 래퍼가 필요한지 반환한다.
        `complete`가 가변 키워드 인자를 받지 않고, `role`과 `metadata` 중 하나라도 받지 못하면 True를 반환한다."""
        try:
            params = inspect.signature(inner.complete).parameters.values()
        except (TypeError, ValueError):
            return False
        names = {p.name for p in params}
        has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params)
        return not has_var_kw and not {"role", "metadata"} <= names

    def complete(self, messages: list[ChatMessage], **kw: Any) -> LLMResponse:
        """클라이언트가 받는 키워드만 넘겨서 호출한다. 반환값은 LLMResponse로 맞춘다."""
        if self._accepted is not None:
            kw = {k: v for k, v in kw.items() if k in self._accepted}
        from sbmig.fix.client import named_response
        raw = self.inner.complete(messages, **kw)
        fallback = self.model or type(self.inner).__name__
        resp = coerce_response(raw, kw.get("model") or fallback, source=f"client {type(self.inner).__name__}")
        return named_response(resp, kw.get("model") or self.model, fallback=fallback)

    def close(self) -> None:
        """안쪽 클라이언트에 `close()`가 있으면 호출한다."""
        fn = getattr(self.inner, "close", None)
        if callable(fn):
            fn()


__all__ = ["AdapterClient", "LLMAdapter", "KwargFilterClient", "is_request_adapter", "coerce_response",
           "KNOWN_ROLES"]
