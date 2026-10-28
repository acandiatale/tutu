"""LLM 클라이언트들: 회사 LLM 게이트웨이(`sbmig.fix.company`, requests.post — 기본값), OpenAI 호환, Anthropic
Messages API, 가짜(재생) 클라이언트, 기록기. 흐름상 위치: 실행 시작 때 오케스트레이터·`sbmig llm-probe`·fleet
preflight 가 `make_llm_client(cfg)` 로 하나를 만들고, 수정 루프(LLMFixer)와 구조 검토가 그것을 쓴다. 핵심:
make_llm_client, LLMError(전송·HTTP·응답 해석 실패), FakeLLMClient + ReplayResponder(`llm.provider: fake` +
`llm.base_url: replay:<dir>` → `<dir>/llm-NNNN.json` 재생), RecordingClient(`llm.record_dir` 에 호출마다
`llm-NNNN.json`), llm_call_in_flight(watchdog 침묵용 카운터), extract_reply. 지킬 것: 키는
환경변수(`llm.api_key_env`, 기본 `LLM_API_KEY`)에서만 읽고 repr·로그·기록에 넣지 않는다. 수정 루프는 LLMError
문구의 `status=` / `cause=` 표식으로 전송 장애를 가르므로 그 형식을 지킨다. OpenAI 호환·Anthropic 은 httpx 로
보내지만, 회사 게이트웨이는 일부러 평범한 requests.post 로 보낸다(company.py 참고).

LLM clients: the company gateway (`sbmig.fix.company`, requests.post — the default), OpenAI-compatible,
Anthropic Messages API, scripted fake, recorder.

All clients implement `sbmig.core.interfaces.LLMClientLike`. Only httpx is used (no vendor SDKs).


"""
from __future__ import annotations

import email.utils
import hashlib
import json
import logging
import os
import random
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Union

import httpx

from sbmig.core.config import BUILTIN_LLM_PROVIDERS, Config
from sbmig.core.interfaces import ChatMessage, LLMClientLike, LLMResponse
from sbmig.core.models import LLMUsage

log = logging.getLogger("sbmig.fix.client")

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529}
DEFAULT_ANTHROPIC_MODEL = "claude-opus-5-5"
ANTHROPIC_VERSION = "2023-06-01"
_DEFAULT_OPENAI_BASE = "http://localhost:8000/v1"     # default.yml fallback; meaningless for anthropic
NO_FIX_RESPONSE = "<analysis>no fix</analysis>"            # analysis only: no <script> → "no script" proposal

# Current Claude generations reject sampling parameters (temperature/top_p/top_k) with HTTP 400.
_ANTHROPIC_NO_SAMPLING_RE = re.compile(
    r"claude-(?:opus-(?:4-[78]|5)|sonnet-5|haiku-5|fable|mythos)", re.IGNORECASE)


class LLMError(RuntimeError):
    """LLM 요청 실패(전송 오류, HTTP 오류, 답을 못 읽음). `status`(HTTP 코드, 전송 오류면 None)와 본문 앞
    500자를 담는다. 수정 루프는 문구의 `status=` / `cause=` 표식으로 전송 장애(쿨다운 → `llm_unavailable`)와
    그 요청만의 거절(400 / 413 / 422, 콘텐츠 필터, finish_reason=length)을 가른다."""
    def __init__(self, status: Optional[int], body_excerpt: str = "", message: str = ""):
        self.status = status
        self.body_excerpt = body_excerpt[:500]
        super().__init__(message or f"LLM request failed (status={status}): {self.body_excerpt}")


class LLMConfigError(ValueError):
    """LLM 설정이 모자람(키 환경변수·URL·모델 없음) — 실행 시작 때 바로 드러나야 하는 설정 오류."""


# --------------------------------------------------------------------------- LLM calls in flight (round 10)
# 수정 루프 watchdog 은 LLM 호출이 진행 중이면 조용히 있는다(클라이언트가 heartbeat·재시도 줄을 따로 남긴다).
# 마이그레이션은 한 번에 하나만 돌기 때문에(웹은 대기열, fleet 은 순차) 프로세스 전역 카운터로 충분하다.
# The fix loop's watchdog stays silent while an LLM HTTP call is in progress (the clients log their own heartbeat /
# retry lines); `LLMFixer._call`, the HTTP clients and `RecordingClient` mark their calls. Process-wide on purpose:
# one migration runs at a time (web runs are queued, the fleet is sequential).
_IN_FLIGHT = 0
_IN_FLIGHT_LOCK = threading.Lock()


@contextmanager
def llm_call_in_flight() -> Iterator[None]:
    """이 블록 동안 "LLM 호출 중" 카운터를 올린다(중첩 가능, 스레드 안전). 루프 watchdog 이 이 값을 본다."""
    global _IN_FLIGHT
    with _IN_FLIGHT_LOCK:
        _IN_FLIGHT += 1
    try:
        yield
    finally:
        with _IN_FLIGHT_LOCK:
            _IN_FLIGHT -= 1


def llm_calls_in_flight() -> int:
    """지금 진행 중인 LLM 호출 수(겹친 감싸개는 각각 센다).

    How many LLM calls are in progress right now (nested wrappers count once each)."""
    return _IN_FLIGHT


def usage_from(resp: LLMResponse) -> LLMUsage:
    """응답 하나의 사용량(호출 1회 + 입력 / 출력 토큰)."""
    return LLMUsage(model=resp.model, calls=1, input_tokens=resp.input_tokens, output_tokens=resp.output_tokens)


def messages_hash(messages: list[ChatMessage]) -> str:
    """메시지 목록의 sha256(프롬프트 해시) — 기록·저널·DEBUG 로그에 프롬프트 본문 대신 남기는 식별자."""
    data = json.dumps([m.model_dump() for m in messages], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _retry_after_seconds(resp: httpx.Response) -> Optional[float]:
    """응답의 `Retry-After`(초 또는 HTTP 날짜) / `retry-after-ms` 헤더 → 기다릴 초(없거나 못 읽으면 None)."""
    val = resp.headers.get("retry-after")
    if not val:
        ms = resp.headers.get("retry-after-ms")
        if ms:
            try:
                return float(ms) / 1000.0
            except ValueError:
                return None
        return None
    try:
        return max(0.0, float(val))
    except ValueError:
        try:
            dt = email.utils.parsedate_to_datetime(val)
            return max(0.0, dt.timestamp() - time.time())
        except (TypeError, ValueError):
            return None


# ---------------------------------------------------------------------------
# HTTP base
# ---------------------------------------------------------------------------
class _HTTPClient:
    """httpx 기반 HTTP 클라이언트의 공통 부분(OpenAI 호환·Anthropic): 재시도·백오프·로그. 회사 게이트웨이는
    이것을 쓰지 않는다(company.py, requests)."""
    provider = "http"

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str],
        model: str,
        *,
        timeout: float = 180.0,
        max_retries: int = 3,
        extra_headers: Optional[dict[str, str]] = None,
        default_max_tokens: int = 8000,
        default_temperature: Optional[float] = 0.0,
        transport: Optional[httpx.BaseTransport] = None,
        sleep: Callable[[float], None] = time.sleep,
        backoff_base: float = 1.0,
        backoff_max: float = 30.0,
    ):
        self.base_url = (base_url or "").strip()
        self._api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self.extra_headers = dict(extra_headers or {})
        self.default_max_tokens = default_max_tokens
        self.default_temperature = default_temperature
        self._sleep = sleep
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self._http = httpx.Client(timeout=timeout, transport=transport)

    def __repr__(self) -> str:                        # never leak the key
        return f"{type(self).__name__}(base_url={self.base_url!r}, model={self.model!r})"

    def close(self) -> None:
        """httpx 연결을 닫는다."""
        self._http.close()

    # subclasses
    def _url(self) -> str: ...
    def _headers(self) -> dict[str, str]: ...
    def _body(self, messages, model, max_tokens, temperature, stop) -> dict[str, Any]: ...
    def _parse(self, data: dict[str, Any], model: str) -> LLMResponse: ...

    def _backoff(self, attempt: int) -> float:
        d = min(self.backoff_max, self.backoff_base * (2 ** attempt))
        return d + random.uniform(0, d * 0.25 + 0.1)

    def complete(
        self,
        messages: list[ChatMessage],
        *,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        stop: Optional[list[str]] = None,
        role: Optional[str] = None,
        metadata: Optional[dict] = None,
        **_ignored: Any,
    ) -> LLMResponse:
        """메시지를 provider 형식으로 바꿔 보내고 LLMResponse 를 돌려준다. DEBUG 로그에는 프롬프트 해시와 글자
        수만 남긴다(본문·키 없음)."""
        model = model or self.model
        max_tokens = max_tokens or self.default_max_tokens
        temperature = self.default_temperature if temperature is None else temperature
        body = self._body(messages, model, max_tokens, temperature, stop)
        url, headers = self._url(), {**self._headers(), **self.extra_headers}
        if log.isEnabledFor(logging.DEBUG):
            log.debug("llm request role=%s model=%s prompt_sha256=%s chars=%d", role or "-", model,
                      messages_hash(messages)[:16], sum(len(m.content) for m in messages))
        with llm_call_in_flight():
            return self._complete(model, body, url, headers)

    def _complete(self, model: str, body: dict[str, Any], url: str, headers: dict[str, str]) -> LLMResponse:
        """재시도 루프: 전송 오류와 RETRY_STATUS(408 / 429 / 5xx 등)는 `max_retries` 번까지 다시(Retry-After
        가 있으면 그만큼 최대 120초, 없으면 지수 백오프 + 지터), 그 밖의 HTTP 오류는 즉시 LLMError. 다
        실패하면 `status=… cause=…` 를 담은 LLMError."""
        t0 = time.monotonic()
        last_status: Optional[int] = None
        last_body = ""
        cause: Optional[str] = None             # the exception class of the last transport failure (round 10)
        for attempt in range(self.max_retries + 1):
            wait: Optional[float] = None
            try:
                resp = self._http.post(url, json=body, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_status, last_body, cause = None, f"{type(exc).__name__}: {exc}", type(exc).__name__
                log.warning("llm %s transport error (attempt %d/%d): %s", self.provider, attempt + 1,
                            self.max_retries + 1, type(exc).__name__)
            else:
                cause = None
                if resp.status_code < 300:
                    try:
                        data = resp.json()
                    except ValueError as exc:
                        raise LLMError(resp.status_code, resp.text[:500], f"invalid JSON from LLM: {exc}") from exc
                    out = self._parse(data, model)
                    log.info("llm call provider=%s model=%s in=%d out=%d latency=%.1fs finish=%s", self.provider,
                             out.model, out.input_tokens, out.output_tokens, time.monotonic() - t0,
                             out.finish_reason)
                    return out
                last_status, last_body = resp.status_code, resp.text[:500]
                if resp.status_code not in RETRY_STATUS:
                    log.error("llm %s HTTP %d: %s", self.provider, resp.status_code,
                              last_body[:300].replace("\n", " "))
                    raise LLMError(resp.status_code, last_body)
                wait = _retry_after_seconds(resp)
                log.warning("llm %s HTTP %d (attempt %d/%d)", self.provider, resp.status_code, attempt + 1,
                            self.max_retries + 1)
            if attempt < self.max_retries:
                delay = min(wait, 120.0) if wait is not None else self._backoff(attempt)
                self._sleep(delay)
        log.error("llm %s giving up after %d attempt(s): status=%s body=%s", self.provider, self.max_retries + 1,
                  last_status, last_body[:300].replace("\n", " "))
        how = f"status={last_status}" + (f", cause={cause}" if cause else "")
        raise LLMError(last_status, last_body,
                       f"LLM request failed after {self.max_retries + 1} attempt(s) ({how}): {last_body[:200]}")


# ---------------------------------------------------------------------------
# OpenAI-compatible
# ---------------------------------------------------------------------------
class OpenAICompatibleClient(_HTTPClient):
    """OpenAI 호환 `/v1/chat/completions` 클라이언트(httpx,
    Bearer 키). base_url 에 `/vN` 이 없으면 `/v1` 을 붙인다."""
    provider = "openai_compatible"

    def _url(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        if not re.search(r"/v\d+[a-z]*$", base):
            base += "/v1"
        return base + "/chat/completions"

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    def _body(self, messages, model, max_tokens, temperature, stop) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": max_tokens,
        }
        if temperature is not None:
            body["temperature"] = temperature
        if stop:
            body["stop"] = stop
        return body

    def _parse(self, data: dict[str, Any], model: str) -> LLMResponse:
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(200, json.dumps(data)[:500], "LLM response has no choices")
        ch = choices[0]
        msg = ch.get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):                 # some gateways return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        usage = data.get("usage") or {}
        return LLMResponse(
            content=content or "",
            model=data.get("model") or model,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            finish_reason=ch.get("finish_reason"),
        )


# ---------------------------------------------------------------------------
# Reply extraction shared with sbmig.fix.company (gateways with arbitrary JSON shapes)
# ---------------------------------------------------------------------------
_REPLY_PATHS = (
    "choices.0.message.content", "choices.0.text", "message.content", "data.choices.0.message.content",
    "data.content", "data.answer", "data.text", "content", "answer", "response", "output", "output_text",
    "result", "text", "reply", "completion",
)


def _dig(data: Any, path: str) -> Any:
    """점 경로(`choices.0.message.content`)를 따라 dict / list 를 내려간다(없으면 KeyError / IndexError)."""
    cur = data
    for part in path.split("."):
        if isinstance(cur, list):
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            cur = cur[part]
        else:
            raise KeyError(part)
    return cur


def _reply_text(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value
    if isinstance(value, list):                       # content parts [{"type": "text", "text": ...}]
        parts = [b.get("text") if isinstance(b, dict) else b for b in value]
        parts = [x for x in parts if isinstance(x, str)]
        return "\n".join(parts) if parts else None
    if isinstance(value, dict) and isinstance(value.get("content"), str):
        return value["content"]
    return None


def extract_reply(data: Any, response_path: Optional[str] = None) -> str:
    """게이트웨이가 어떤 JSON 모양으로 답하든 그 안의 답변 텍스트를 꺼낸다:
    `llm.response_path`(LLM_RESPONSE_PATH)가 있으면 그 경로, 없으면 알려진 모양(_REPLY_PATHS)을 차례로. 못
    찾으면 LLMError(`sbmig llm-probe` 로 경로를 확인하라는 안내 포함).

    The assistant text inside whatever JSON a gateway returns (`response_path` wins; else known shapes)."""
    if isinstance(data, str):
        return data
    if response_path:
        try:
            text = _reply_text(_dig(data, response_path))
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise LLMError(200, json.dumps(data, ensure_ascii=False)[:500],
                           f"llm.response_path {response_path!r} not found in the response ({exc})") from exc
        if text is None:
            raise LLMError(200, json.dumps(data, ensure_ascii=False)[:500],
                           f"llm.response_path {response_path!r} does not point at text")
        return text
    for path in _REPLY_PATHS:
        try:
            text = _reply_text(_dig(data, path))
        except (KeyError, IndexError, ValueError, TypeError):
            continue
        if text:
            return text
    raise LLMError(200, json.dumps(data, ensure_ascii=False)[:500],
                   "cannot find the reply text in the LLM response; set llm.response_path (see `sbmig llm-probe`)")


# ---------------------------------------------------------------------------
# Anthropic Messages API
# ---------------------------------------------------------------------------
class AnthropicClient(_HTTPClient):
    """Anthropic Messages API 클라이언트(httpx, `x-api-key` 헤더) — Anthropic 검증
    실행용(`config/llm-anthropic.yml`). 회사 실행의 기본은 `company`."""
    provider = "anthropic"

    def __init__(self, base_url: str = "https://api.anthropic.com", api_key: Optional[str] = None,
                 model: str = DEFAULT_ANTHROPIC_MODEL, **kw: Any):
        super().__init__(base_url or "https://api.anthropic.com", api_key, model or DEFAULT_ANTHROPIC_MODEL, **kw)

    def _url(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/v1/messages"):
            return base
        if base.endswith("/v1"):
            base = base[:-3]
        return base + "/v1/messages"

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", "anthropic-version": ANTHROPIC_VERSION}
        if self._api_key:
            h["x-api-key"] = self._api_key
        return h

    @staticmethod
    def accepts_temperature(model: str) -> bool:
        """이 모델이 temperature 를 받는가 — 최신 Claude 세대는 샘플링 파라미터를 HTTP 400 으로 거절한다."""
        return not _ANTHROPIC_NO_SAMPLING_RE.search(model or "")

    def _body(self, messages, model, max_tokens, temperature, stop) -> dict[str, Any]:
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        turns: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "system":
                continue
            if turns and turns[-1]["role"] == m.role:     # merge consecutive same-role turns
                turns[-1]["content"] += "\n\n" + m.content
            else:
                turns.append({"role": m.role, "content": m.content})
        body: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "messages": turns}
        if system:
            body["system"] = system
        if temperature is not None and self.accepts_temperature(model):
            body["temperature"] = temperature
        if stop:
            body["stop_sequences"] = stop
        return body

    def _parse(self, data: dict[str, Any], model: str) -> LLMResponse:
        blocks = data.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
        usage = data.get("usage") or {}
        return LLMResponse(
            content=text,
            model=data.get("model") or model,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            finish_reason=data.get("stop_reason"),
        )


# ---------------------------------------------------------------------------
# Fake + recording
# ---------------------------------------------------------------------------
FakeScript = Union[list[Union[str, Exception]], Callable[[list[ChatMessage]], str], "ReplayResponder", None]


class FakeLLMClient:
    """테스트·오프라인 실행용 가짜 LLM: 목록(순서대로 소비, 마지막이 반복, Exception 항목은 던짐), 함수
    `fn(messages)`, ReplayResponder(기록 재생), None(늘 "no fix").

    Scripted client for tests / offline runs.

    responses: list (consumed in order, last one repeats; an Exception item is raised), a callable
    `fn(messages) -> str`, a `ReplayResponder` (recorded answers selected by role / regex), or None (always answers
    "no fix"). Answers are plain text: fix scripts or structure-review JSON alike.
    
    
    """

    def __init__(self, responses: FakeScript = None, record_dir: Optional[str | Path] = None, model: str = "fake"):
        self.responses = responses
        self.model = model
        self.calls: list[dict[str, Any]] = []
        self._i = 0
        self._recorder = _Recorder(record_dir) if record_dir else None

    def complete(self, messages: list[ChatMessage], *, model: Optional[str] = None, max_tokens: Optional[int] = None,
                 temperature: Optional[float] = None, stop: Optional[list[str]] = None,
                 role: Optional[str] = None, metadata: Optional[dict] = None, **_ignored: Any) -> LLMResponse:
        """정해진 답을 돌려준다(토큰 수는 글자 수 / 4 로 어림). ReplayResponder 에는 role 도 넘긴다.
        record_dir 가 있으면 실제 클라이언트처럼 기록한다."""
        used_model = model or self.model
        self.calls.append({"messages": list(messages), "model": used_model, "max_tokens": max_tokens,
                           "temperature": temperature, "stop": stop, "role": role,
                           "metadata": dict(metadata) if metadata else None})
        if self.responses is None:
            text: Union[str, Exception] = NO_FIX_RESPONSE
        elif isinstance(self.responses, ReplayResponder):
            text = self.responses(list(messages), role=role)
        elif callable(self.responses):
            text = self.responses(list(messages))
        elif not self.responses:
            text = NO_FIX_RESPONSE
        else:
            text = self.responses[min(self._i, len(self.responses) - 1)]
            self._i += 1
        if isinstance(text, Exception):
            raise text
        in_tok = sum(len(m.content) for m in messages) // 4
        resp = LLMResponse(content=text, model=used_model, input_tokens=in_tok, output_tokens=len(text) // 4,
                           finish_reason="stop")
        if self._recorder:
            self._recorder.write(messages, resp, 0.0, role=role, metadata=metadata)
        return resp


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return json.loads(json.dumps(value, default=str))


class _Recorder:
    """`llm-NNNN.json` 을 하나씩 쓰는 기록기(번호는 디렉토리의 기존 파일 다음부터, 이미 있으면 건너뜀).
    메시지·응답·모델·사용량·지연·프롬프트 해시·오류·role·metadata 를 담는다 — 키·헤더는 담지 않는다."""
    def __init__(self, record_dir: str | Path, ws: Any = None):
        self.dir = Path(record_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.ws = ws
        self._n = len(list(self.dir.glob("llm-*.json")))

    def write(self, messages: list[ChatMessage], resp: Optional[LLMResponse], latency: float,
              error: Optional[str] = None, *, role: Optional[str] = None, metadata: Optional[dict] = None) -> Path:
        """호출 하나를 기록하고, ws 가 있으면 `llm_call` 저널
        이벤트를 남긴다(저널 실패는 호출을 깨지 않는다)."""
        self._n += 1
        path = self.dir / f"llm-{self._n:04d}.json"
        while path.exists():
            self._n += 1
            path = self.dir / f"llm-{self._n:04d}.json"
        rec = {
            "messages": [m.model_dump() for m in messages],
            "response": resp.content if resp else None,
            "model": resp.model if resp else None,
            "usage": usage_from(resp).model_dump() if resp else None,
            "finish_reason": resp.finish_reason if resp else None,
            "latency": round(latency, 3),
            "prompt_hash": messages_hash(messages),
            "error": error,
            "role": role,
            "metadata": _jsonable(metadata) if metadata else None,   # caller-supplied context only, never secrets
        }
        path.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        if self.ws is not None:
            try:
                self.ws.journal({"event": "llm_call", "record": str(path), "role": role, "model": rec["model"],
                                 "usage": rec["usage"], "latency": rec["latency"], "error": error})
            except Exception:  # pragma: no cover - journaling must never break a call
                log.debug("journal failed", exc_info=True)
        return path


class RecordingClient:
    """다른 클라이언트를 감싸 호출마다 `llm-NNNN.json` 을 `llm.record_dir` 에 쓴다(실패한 호출도 `error` 와
    함께). 이 기록이 그대로 재생 입력이 된다(`provider: fake` + `base_url: replay:<dir>`). 운영자가 문제를
    알릴 때 공유하는 파일이기도 하다.

    Wraps another client and writes `llm-<n>.json` per call into record_dir (and journals via ws)."""

    def __init__(self, inner: LLMClientLike, record_dir: str | Path, ws: Any = None):
        self.inner = inner
        self.model = getattr(inner, "model", "")
        self._rec = _Recorder(record_dir, ws)

    @property
    def record_dir(self) -> Path:
        """기록 디렉토리."""
        return self._rec.dir

    def complete(self, messages: list[ChatMessage], **kw: Any) -> LLMResponse:
        """안쪽 클라이언트를 부르고 성공·실패 모두 기록한다(예외는 그대로 다시 던진다)."""
        role, metadata = kw.get("role"), kw.get("metadata")
        t0 = time.monotonic()
        try:
            with llm_call_in_flight():
                resp = self.inner.complete(messages, **kw)
        except Exception as exc:
            self._rec.write(messages, None, time.monotonic() - t0, error=str(exc)[:500], role=role, metadata=metadata)
            raise
        self._rec.write(messages, resp, time.monotonic() - t0, role=role, metadata=metadata)
        return resp

    def close(self) -> None:
        """안쪽 클라이언트에 `close()` 가 있으면 부른다."""
        fn = getattr(self.inner, "close", None)
        if callable(fn):
            fn()


def load_replay_responses(record_dir: str | Path) -> list[str]:
    """기록된 세션의 답변 텍스트들(파일 순서) — 위치 기반 재생용으로 FakeLLMClient 에 넣는다.

    Responses of a recorded session (llm-*.json, in order) — feed to FakeLLMClient for replay."""
    return [e.response for e in load_replay_entries(record_dir)]


class ReplayEntry:
    """기록된 답 하나(`llm-NNNN.json`): `response` 텍스트, 선택 `replay_role`(fix | structure_review …; fix 는
    fix_escalation 호출에도 답한다), 선택 `match` 정규식(요청의 system 이 아닌 메시지에서 찾음). 기록기가 쓴
    `role` 키는 참고용일 뿐 선택에 쓰지 않는다.

    One recorded answer (`llm-NNNN.json`): `response` text, optional `replay_role` (fix | structure_review | …;
    "fix" also answers fix_escalation calls) and optional `match` regex searched in the request's non-system
    messages. (The recorder's own `role` key is informational and never selects.)"""

    def __init__(self, response: str, *, role: Optional[str] = None, match: Optional[str] = None, name: str = ""):
        self.response = response
        self.role = role or None
        self.match = match or None
        self.name = name
        self._rx = re.compile(match, re.MULTILINE) if match else None

    def matches(self, text: str, role: Optional[str]) -> bool:
        """이 답이 이 요청에 맞는가: role 이 맞고(`replay_role` 이 없으면 아무 role), `match` 가 있으면
        본문에서 찾아져야 한다."""
        if self.role and self.role != (role or "") and not (self.role == "fix" and role == "fix_escalation"):
            return False
        return self._rx is None or bool(self._rx.search(text))


def load_replay_entries(record_dir: str | Path) -> list[ReplayEntry]:
    """재생 디렉토리의 `llm-*.json` 중 `response` 가 있는 것 전부(파일 이름 순). 손으로 만든 픽스처는
    `match`(정규식) / `replay_role` 로 위치 대신 요청 내용으로 답을 고른다 — e2e 픽스처의 `match: "case:
    <case_id>"` 는 수정 프롬프트 머리말의 `case: <id>` 줄(ContextPacker.pack_script)을 찾는다.

    Every llm-*.json of a replay directory with a `response` (file-name order). Hand-written fixtures may add
    `match` (regex) and / or `replay_role` to pick answers by request instead of by position (`ReplayResponder`)."""
    out: list[ReplayEntry] = []
    for p in sorted(Path(record_dir).glob("llm-*.json")):
        data = json.loads(p.read_text(encoding="utf-8"))
        if data.get("response") is None:
            continue
        out.append(ReplayEntry(str(data["response"]), role=data.get("replay_role") or None,
                               match=data.get("match") or None, name=p.name))
    return out


class ReplayResponder:
    """요청마다 기록된 답을 고르는 재생기: role 이 맞고 `match` 정규식이 user / assistant 메시지에서 찾아지는
    항목 중 아직 안 쓴 첫 항목(파일 순서). 맞는 항목을 다 썼으면 마지막 것이 반복, 맞는 게 없으면 "no fix"
    답(경고 로그). `match` / role 이 없으면 위치 재생과 같다.

    Replays recorded answers chosen per request: the first unused entry (file order) whose `replay_role` fits the
    call's role (entries without one take any role) and whose `match` regex is found in the request's user / assistant
    messages (no `match` = any request); when every matching entry was used, the last matching one repeats; nothing
    matches → the "no fix" answer. Without `match` / role fields this is the positional replay (in order, the last one
    repeats)."""

    def __init__(self, entries: list[ReplayEntry]):
        self.entries = list(entries)
        self.used: set[int] = set()
        self.served: list[str] = []                  # entry names in serve order (tests / debugging)

    def __call__(self, messages: list[ChatMessage], role: Optional[str] = None) -> str:
        """이 요청에 맞는 기록된 답 텍스트(위 규칙)."""
        # system 프롬프트는 빼고 찾는다 — 모든 요청의 system 이 같아서 거기로는 요청이 구분되지 않는다
        text = "\n\n".join(m.content for m in messages if m.role != "system")
        hits = [i for i, e in enumerate(self.entries) if e.matches(text, role)]
        if not hits:
            log.warning("replay: no recorded answer matches this request (role=%s); answering 'no fix'", role or "-")
            return NO_FIX_RESPONSE
        pick = next((i for i in hits if i not in self.used), hits[-1])
        self.used.add(pick)
        self.served.append(self.entries[pick].name)
        return self.entries[pick].response


# ---------------------------------------------------------------------------
# factory
# ---------------------------------------------------------------------------
def _unexpanded(v: Optional[str]) -> bool:
    return not v or "${" in v


def make_llm_client(cfg: Config, *, ws: Any = None, transport: Optional[httpx.BaseTransport] = None) -> LLMClientLike:
    """설정된 LLM 클라이언트를 만든다(실행마다 한 번). `company`(기본) → CompanyClient(LLM_BASE_URL 로
    requests.post, `Authorization: Bearer <LLM_API_KEY>`), `openai_compatible` / `anthropic` → httpx
    클라이언트, `fake` → FakeLLMClient(`base_url: replay:<dir>` 면 기록 재생 — `match` / `replay_role` 이 있는
    항목이 하나라도 있으면 ReplayResponder, 아니면 위치 순), `plugin` 이나 내장이 아닌 이름 →
    `sbmig.llm.resolve_llm_plugin`.
    `llm.record_dir` 가 있으면 RecordingClient 로 감싼다. 키·URL·모델이 없으면 LLMConfigError.

    Build the configured client. `fake` needs no key; `base_url: replay:<dir>` with fake replays recordings.

    `provider: company` (default) → `sbmig.fix.company.CompanyClient`: requests.post to LLM_BASE_URL with the
    bearer token from `api_key_env`; everything else comes from `.env` / config/default.yml.
    `provider: plugin` or a non-builtin provider name → `sbmig.llm.resolve_llm_plugin(cfg)` (the plugin handles
    its own auth; the `api_key_env` value, when set, is handed to the factory as options["api_key"] in memory only).
    Raises `sbmig.llm.PluginLoadError` (a ValueError) when the plugin cannot be loaded."""
    llm = cfg.llm
    client: LLMClientLike
    if llm.provider == "plugin" or llm.provider not in BUILTIN_LLM_PROVIDERS:
        from sbmig.llm import resolve_llm_plugin

        client = resolve_llm_plugin(cfg)
    elif llm.provider == "fake":
        responses: FakeScript = None
        if llm.base_url and llm.base_url.startswith("replay:"):
            entries = load_replay_entries(cfg.resolve_path(llm.base_url[len("replay:"):]))
            # positional replay (older fixtures) unless an entry selects by request (`match` / `replay_role`)
            responses = ReplayResponder(entries) if any(e.match or e.role for e in entries) \
                else [e.response for e in entries]
        client = FakeLLMClient(responses)
    else:
        # 키는 환경변수에서만 읽는다(.env 는 load_config 가 환경으로 올림) — 오류 문구에도 변수 이름만 쓴다
        key = os.environ.get(llm.api_key_env or "")
        if not key:
            raise LLMConfigError(
                f"LLM provider '{llm.provider}' needs an API key in the environment variable "
                f"'{llm.api_key_env}' (set it, or use llm.provider=fake)")
        common = dict(timeout=float(llm.timeout_seconds), max_retries=llm.max_retries,
                      extra_headers=llm.extra_headers, default_max_tokens=llm.max_output_tokens,
                      default_temperature=llm.temperature, transport=transport)
        if llm.provider == "anthropic":
            base = llm.base_url if not _unexpanded(llm.base_url) and llm.base_url != _DEFAULT_OPENAI_BASE \
                else "https://api.anthropic.com"
            model = llm.model if not _unexpanded(llm.model) and llm.model != "default" else DEFAULT_ANTHROPIC_MODEL
            client = AnthropicClient(base, key, model, **common)
        elif llm.provider == "openai_compatible":
            if _unexpanded(llm.base_url):
                raise LLMConfigError("llm.base_url is not set (LLM_BASE_URL)")
            if _unexpanded(llm.model):
                raise LLMConfigError("llm.model is not set (LLM_MODEL)")
            client = OpenAICompatibleClient(llm.base_url, key, llm.model, **common)
        elif llm.provider == "company":
            from .company import CompanyClient

            if _unexpanded(llm.base_url):
                raise LLMConfigError("LLM_BASE_URL is not set (.env or environment: the full gateway URL)")
            if _unexpanded(llm.model):
                raise LLMConfigError("LLM_MODEL is not set (.env or environment)")
            client = CompanyClient(llm.base_url, key, llm.model, payload_extra=llm.payload_extra,
                                   response_path=llm.response_path or None, timeout=float(llm.timeout_seconds),
                                   max_retries=llm.max_retries, extra_headers=llm.extra_headers)
        else:  # pragma: no cover - non-builtin providers are routed to sbmig.llm above
            raise LLMConfigError(f"unknown llm.provider: {llm.provider}")
    if llm.record_dir:
        client = RecordingClient(client, cfg.resolve_path(llm.record_dir), ws=ws)
    return client
