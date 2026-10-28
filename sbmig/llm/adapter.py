"""1단(tier 1) 어댑터 감싸개: 사내 `LLMAdapterLike`(LLMRequest 하나 → LLMResponse 하나)를 sbmig 안에서 쓰는
메시지 기반 `LLMClientLike` 로 바꿔 준다. 흐름상 위치: 플러그인을 불러온 `sbmig.llm.registry.wrap_llm` 이 여기
클래스로 감싸고, 그 뒤로는 수정 루프·구조 검토가 내장 클라이언트와 똑같이 쓴다. 핵심: AdapterClient(요청 객체
생성 + 응답 검증 + 예외 → LLMError), KwargFilterClient(`role` / `metadata` 를 못 받는 클라이언트의 키워드
걸러내기), LLMAdapter(플러그인 작성자용 선택 베이스 클래스), coerce_response, is_request_adapter. 지킬 것:
어댑터가 던진 예외는 모두 `status=… cause=<예외 클래스>` 문구가 든 `LLMError` 로 바꾼다 — 수정 루프는 이
명시적 표식으로만 전송 장애를 가려낸다(round 10). 이상한
반환값은 오류 문장을 답변(content)으로 넘기지 않고 예외로 만든다.

Adapter tier: wrap an in-house `LLMAdapterLike` (one LLMRequest in, one LLMResponse out) as an LLMClientLike.

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
    """사내 게이트웨이 어댑터용 편의 베이스 클래스: `complete(request)` 만 구현하면 된다.
    실패하면 예외를 던질 것 — sbmig 가 LLMError 로 바꾸므로 수정
    루프는 멈추지 않고, 반복되면 "LLM 사용 불가" 로 처리한다.

    Convenience base class for in-house gateway adapters.

    Subclass it, implement `complete(request)` and point `llm.plugin` at the class (or at a factory returning an
    instance). Raise an exception on failure — sbmig converts it to `LLMError` so the build-fix loop can mark the
    LLM as unavailable after repeated failures instead of crashing."""

    adapter_kind = "request"

    @property
    def name(self) -> str:
        """로그·오류 문구에 쓰는 어댑터 이름(기본은 클래스 이름)."""
        return type(self).__name__

    @abc.abstractmethod
    def complete(self, request: LLMRequest) -> LLMResponse:  # pragma: no cover - abstract
        """요청 하나를 게이트웨이로 보내고 LLMResponse 를 돌려준다(재시도·인증·타임아웃은 플러그인 몫). 오류
        문장을 content 로 돌려주지 말고 예외를 던질 것."""
        ...

    def close(self) -> None:
        return None


def is_request_adapter(obj: Any) -> bool:
    """`obj.complete` 가 LLMRequest 하나를 받는 어댑터(1단)면 True, 메시지 목록을 받는 클라이언트면 False.
    판별 순서: LLMAdapter 하위 클래스 → `adapter_kind` 속성 → `complete` 첫 인자(이름 `request` / 타입
    LLMRequest) → 그 밖은 클라이언트.

    True when `obj.complete` takes one LLMRequest (adapter tier), False when it is message-based (client tier).

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
    """플러그인이 돌려준 값(LLMResponse / dict / str / `.content` 를 가진 객체)을 LLMResponse 로 맞춘다.
    그 밖의 값은 LLMError(오류 문장이 답변처럼 흘러가지 않게). 빈 `model` 은 요청한 모델로 채운다.

    LLMResponse | dict | str | object with `.content` → LLMResponse; anything else → LLMError."""
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
    """LLMAdapterLike 위의 LLMClientLike: 호출마다 LLMRequest
    하나를 만들고 답을 검증한다(sbmig 전체가 이 객체를 쓴다).

    LLMClientLike over an LLMAdapterLike. Builds one LLMRequest per call and validates the answer."""

    def __init__(self, adapter: Any, *, default_model: Optional[str] = None, base_metadata: Optional[dict] = None):
        if not callable(getattr(adapter, "complete", None)):
            raise TypeError(f"{type(adapter).__name__} has no complete(request) method")
        self.adapter = adapter
        self.default_model = default_model or None
        self.base_metadata = dict(base_metadata or {})
        self.model = self.default_model or str(getattr(adapter, "model", "") or "")

    @property
    def name(self) -> str:
        """어댑터의 `name`(문자열일 때), 아니면 어댑터 클래스 이름."""
        n = getattr(self.adapter, "name", None)
        return n if isinstance(n, str) and n else type(self.adapter).__name__

    def __repr__(self) -> str:
        return f"AdapterClient({self.name}, default_model={self.default_model!r})"

    def build_request(self, messages: list[ChatMessage], *, model: Optional[str] = None,
                      max_tokens: Optional[int] = None, temperature: Optional[float] = None,
                      stop: Optional[list[str]] = None, role: Optional[str] = None,
                      metadata: Optional[dict] = None) -> LLMRequest:
        """sbmig 의 메시지 호출 인자를 LLMRequest 하나로. 모르는 role(예: `verify_guide`)은 `other` 로 바꾸고
        원래 값은 `metadata["role_detail"]` 에 남긴다. metadata
        에는 run_id·project 같은 문맥만 담는다(비밀 금지)."""
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
        """어댑터를 한 번 부른다. LLMError 는 그대로, 다른 예외는 `status=` / `cause=` 표식이 든 LLMError 로
        바꿔 던진다(수정 루프가 이 표식으로 전송 장애 → 쿨다운 → `llm_unavailable` 을 판정한다)."""
        from sbmig.fix.client import LLMError

        request = self.build_request(messages, model=model, max_tokens=max_tokens, temperature=temperature,
                                     stop=stop, role=role, metadata=metadata)
        try:
            raw = self.adapter.complete(request)
        except LLMError:
            raise
        except Exception as exc:     # surface as LLMError so callers treat it as a transport failure
            status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
            status = status if isinstance(status, int) else None
            # 수정 루프는 문구 속 `status=` / `cause=` 표식만 보고 장애를 가른다 — 이 문구 형식을 바꾸지 말 것
            # round 10: the fix loop classifies by explicit markers — `status=<code>` and `cause=<ExceptionClass>`
            raise LLMError(status, str(exc)[:500],
                           f"LLM adapter {self.name} failed (status={status}, cause={type(exc).__name__}): "
                           f"{exc}") from exc
        return coerce_response(raw, request.model or self.model or self.name, source=f"adapter {self.name}")

    def close(self) -> None:
        """어댑터에 `close()` 가 있으면 부른다."""
        fn = getattr(self.adapter, "close", None)
        if callable(fn):
            fn()


class KwargFilterClient:
    """sbmig 가 넘기는 키워드를 다 받지 못하는(예: `role` / `metadata` 없음) 메시지 기반 플러그인 클라이언트를
    감싼다: 모르는 키워드는 TypeError 를 내는 대신 버린다.

    Wraps a message-based plugin client whose `complete` does not accept every keyword sbmig passes
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
        """이 감싸개가 필요한가: `complete` 에 `**kwargs` 가
        없고 `role`·`metadata` 를 둘 다 받지는 못하면 True."""
        try:
            params = inspect.signature(inner.complete).parameters.values()
        except (TypeError, ValueError):
            return False
        names = {p.name for p in params}
        has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params)
        return not has_var_kw and not {"role", "metadata"} <= names

    def complete(self, messages: list[ChatMessage], **kw: Any) -> LLMResponse:
        """받을 수 있는 키워드만 넘겨 호출하고, 반환값을 LLMResponse 로 맞춘다(coerce_response)."""
        if self._accepted is not None:
            kw = {k: v for k, v in kw.items() if k in self._accepted}
        raw = self.inner.complete(messages, **kw)
        return coerce_response(raw, kw.get("model") or self.model or type(self.inner).__name__,
                               source=f"client {type(self.inner).__name__}")

    def close(self) -> None:
        """안쪽 클라이언트에 `close()` 가 있으면 부른다."""
        fn = getattr(self.inner, "close", None)
        if callable(fn):
            fn()


__all__ = ["AdapterClient", "LLMAdapter", "KwargFilterClient", "is_request_adapter", "coerce_response",
           "KNOWN_ROLES"]
