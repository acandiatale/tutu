"""LLM 클라이언트를 모은 모듈. 회사 게이트웨이(`sbmig.fix.company`, 기본값), OpenAI 호환, Anthropic,
가짜(기록 재생) 클라이언트와 호출 기록기를 제공한다.
오케스트레이터, `sbmig llm-probe`, fleet preflight가 `make_llm_client(cfg)`로 클라이언트를 만들고 수정
루프와 구조 검토가 사용한다.
- 키는 환경 변수(`llm.api_key_env`, 기본 `LLM_API_KEY`)에서만 읽고 repr, 로그, 기록에 넣지 않는다
- 수정 루프가 LLMError 메시지의 `status=` / `cause=`로 전송 장애를 구분하므로 이 형식을 유지한다
- OpenAI 호환과 Anthropic은 httpx를 쓰고, 회사 게이트웨이는 일부러 단순한 requests.post를 쓴다

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
    """LLM 요청이 실패했을 때(전송 오류, HTTP 오류, 응답 해석 실패) 던지는 예외.
    `status`와 응답 본문 앞 500자를 담는다.
    수정 루프는 메시지의 `status=` / `cause=`로 전송 장애와 그 요청만
    거절된 경우(400 / 413 / 422, 콘텐츠 필터 등)를 구분한다."""
    def __init__(self, status: Optional[int], body_excerpt: str = "", message: str = ""):
        self.status = status
        self.body_excerpt = body_excerpt[:500]
        super().__init__(message or f"LLM request failed (status={status}): {self.body_excerpt}")


class LLMConfigError(ValueError):
    """LLM 설정(키 환경 변수, URL, 모델)이 빠졌을 때 던지는 예외.
    실행을 시작할 때 바로 드러나야 하는 오류다."""


# --------------------------------------------------------------------------- LLM calls in flight (round 10)
# LLM 호출 중에는 클라이언트가 heartbeat와 재시도 로그를 따로 남기므로 수정 루프 watchdog은 조용히 있는다
# 프로세스당 이관은 한 번에 하나만 실행되므로 프로세스 전역 카운터로 충분하다
# The fix loop's watchdog stays silent while an LLM HTTP call is in progress (the clients log their own heartbeat /
# retry lines); `LLMFixer._call`, the HTTP clients and `RecordingClient` mark their calls. Process-wide on purpose:
# one migration runs at a time per process — in Job mode every migration running at once is its own process (its own
# pod, its own counter); the in-process web server queues its runs and the fleet is sequential.
_IN_FLIGHT = 0
_IN_FLIGHT_LOCK = threading.Lock()


@contextmanager
def llm_call_in_flight() -> Iterator[None]:
    """이 블록이 실행되는 동안 진행 중인 LLM 호출 수를 늘려 둔다. 중첩할 수 있고 스레드에 안전하다."""
    global _IN_FLIGHT
    with _IN_FLIGHT_LOCK:
        _IN_FLIGHT += 1
    try:
        yield
    finally:
        with _IN_FLIGHT_LOCK:
            _IN_FLIGHT -= 1


def llm_calls_in_flight() -> int:
    """진행 중인 LLM 호출 수를 반환한다. 중첩된 블록은 각각 센다.

    How many LLM calls are in progress right now (nested wrappers count once each)."""
    return _IN_FLIGHT


def usage_from(resp: LLMResponse) -> LLMUsage:
    """응답 하나의 사용량(호출 1회와 입력 / 출력 토큰 수)을 반환한다."""
    return LLMUsage(model=resp.model, calls=1, input_tokens=resp.input_tokens, output_tokens=resp.output_tokens)


def messages_hash(messages: list[ChatMessage]) -> str:
    """메시지 목록의 sha256 해시를 계산한다. 기록, 저널, DEBUG 로그에 프롬프트 본문 대신 남긴다."""
    data = json.dumps([m.model_dump() for m in messages], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _retry_after_seconds(resp: httpx.Response) -> Optional[float]:
    """`Retry-After` / `retry-after-ms` 헤더에서 기다릴 시간(초)을 구한다.
    헤더가 없거나 읽지 못하면 None을 반환한다."""
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
    """httpx 기반 HTTP 클라이언트의 공통 부분(재시도, 백오프, 로그)을 담은 클래스.
    회사 게이트웨이 클라이언트는 쓰지 않는다."""
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
        """httpx 클라이언트의 연결을 닫는다."""
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
        """메시지를 provider 형식으로 바꿔 보내고 LLMResponse를 반환한다.
        DEBUG 로그에는 프롬프트 해시와 문자 수만 남긴다."""
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
        """요청을 보내고 실패하면 다시 시도한다.
        전송 오류와 RETRY_STATUS는 `max_retries`번까지 재시도하고, 다른 HTTP 오류는 바로 LLMError를 던진다.
        대기 시간은 Retry-After가 있으면 그 값(최대 120초), 없으면 지수 백오프에 무작위 지연을 더한 값이다."""
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
    """OpenAI 호환 chat completions API를 호출하는 클라이언트. base_url에 API 버전 경로가 없으면 붙인다."""
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
    """점으로 구분된 경로(`choices.0.message.content`)를 따라 dict / list 안의 값을 꺼낸다.
    경로가 없으면 KeyError / IndexError가 난다."""
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
    """게이트웨이 JSON 응답에서 답변 텍스트를 꺼낸다.
    `llm.response_path`가 있으면 그 경로를, 없으면 알려진 응답 형태를 차례로 확인한다.
    찾지 못하면 `sbmig llm-probe`로 경로를 확인하라는 안내와 함께 LLMError를 던진다.

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
    """Anthropic Messages API를 호출하는 클라이언트.
    Anthropic 검증 실행에 쓰고, 회사 실행의 기본값은 `company`다."""
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
        """이 모델이 temperature 파라미터를 받는지 확인한다.
        최신 Claude 모델은 샘플링 파라미터를 HTTP 400으로 거절한다."""
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
    """테스트와 오프라인 실행에 쓰는 가짜 LLM 클라이언트.
    응답 목록, 함수 `fn(messages)`, ReplayResponder, None 중 하나로 답한다.

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
        """미리 정해 둔 답을 반환한다. 토큰 수는 문자 수 / 4로 추정한다.
        record_dir가 있으면 실제 클라이언트처럼 호출을 기록한다."""
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


_RECORD_TRIES = 10000               # numbers tried past ones another writer took (never spins forever)


class _Recorder:
    """LLM 호출을 `llm-NNNN.json` 파일로 하나씩 기록하는 클래스.
    번호는 디렉토리의 기존 파일 다음부터 매긴다.
    키와 헤더는 담지 않는다. 파일을 배타적으로 만들므로 여러 파드가 같은 번호를 골라도 덮어쓰지 않는다."""
    def __init__(self, record_dir: str | Path, ws: Any = None):
        self.dir = Path(record_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.ws = ws
        self._n = len(list(self.dir.glob("llm-*.json")))

    def write(self, messages: list[ChatMessage], resp: Optional[LLMResponse], latency: float,
              error: Optional[str] = None, *, role: Optional[str] = None,
              metadata: Optional[dict] = None) -> Optional[Path]:
        """호출 하나를 파일로 기록하고, ws가 있으면 `llm_call` 저널 이벤트를 남긴다.
        기록 파일을 쓰지 못하면 WARNING을 남기고 None을 반환한다.
        저널 기록이 실패해도 호출은 실패하지 않는다."""
        rec = {
            "messages": [m.model_dump() for m in messages],
            "response": resp.content if resp else None,
            "session_id": getattr(resp, "session_id", None) if resp else None,   # v2.1z-o: the gateway's session
            "model": resp.model if resp else None,
            "usage": usage_from(resp).model_dump() if resp else None,
            "finish_reason": resp.finish_reason if resp else None,
            "latency": round(latency, 3),
            "prompt_hash": messages_hash(messages),
            "error": error,
            "role": role,
            "metadata": _jsonable(metadata) if metadata else None,   # caller-supplied context only, never secrets
        }
        text = json.dumps(rec, ensure_ascii=False, indent=2)
        # exists()로 확인한 뒤에 쓰면 다른 프로세스와 같은 번호를 고를 수 있으므로 배타적 생성으로 번호를 잡는다
        for _ in range(_RECORD_TRIES):
            self._n += 1
            path = self.dir / f"llm-{self._n:04d}.json"
            made = False
            try:
                with open(path, "x", encoding="utf-8") as fh:
                    made = True
                    fh.write(text)
                break
            except FileExistsError:
                continue
            except OSError as e:
                # 답은 이미 받았으므로 기록 실패를 호출 실패로 보지 않고, 경고만 남긴 뒤 쓰다 만 파일을 지운다
                log.warning("LLM record %s not written (%s: %s) — the call itself is not affected", path,
                            type(e).__name__, e)
                if made:
                    try:
                        path.unlink()
                    except OSError:
                        pass
                return None
        else:  # pragma: no cover - ten thousand numbers taken in a row
            log.warning("LLM record not written: %d numbers in a row were taken in %s", _RECORD_TRIES, self.dir)
            return None
        if self.ws is not None:
            try:
                self.ws.journal({"event": "llm_call", "record": str(path), "role": role, "model": rec["model"],
                                 "usage": rec["usage"], "latency": rec["latency"], "error": error})
            except Exception:  # pragma: no cover - journaling must never break a call
                log.debug("journal failed", exc_info=True)
        return path


class RecordingClient:
    """다른 클라이언트를 감싸 호출마다 `llm-NNNN.json`을 `llm.record_dir`에 기록하는 클래스.
    실패한 호출도 기록한다.
    이 기록은 그대로 재생 입력으로 쓸 수 있고, 운영자가 문제를 알릴 때 공유하는 파일이기도 하다.

    Wraps another client and writes `llm-<n>.json` per call into record_dir (and journals via ws)."""

    def __init__(self, inner: LLMClientLike, record_dir: str | Path, ws: Any = None):
        self.inner = inner
        self.model = getattr(inner, "model", "")
        self._rec = _Recorder(record_dir, ws)

    @property
    def record_dir(self) -> Path:
        """LLM 요청과 응답을 기록하는 디렉토리."""
        return self._rec.dir

    def complete(self, messages: list[ChatMessage], **kw: Any) -> LLMResponse:
        """안쪽 클라이언트를 호출하고 성공과 실패를 모두 기록한다. 예외는 그대로 다시 던진다.
        받은 답을 잃거나 원래 예외를 가리지 않도록 기록 실패는 WARNING만 남긴다."""
        role, metadata = kw.get("role"), kw.get("metadata")
        t0 = time.monotonic()
        try:
            with llm_call_in_flight():
                resp = self.inner.complete(messages, **kw)
        except Exception as exc:
            self._write(messages, None, time.monotonic() - t0, error=str(exc)[:500], role=role, metadata=metadata)
            raise
        self._write(messages, resp, time.monotonic() - t0, role=role, metadata=metadata)
        return resp

    def _write(self, *args: Any, **kw: Any) -> None:
        try:
            self._rec.write(*args, **kw)
        except Exception as e:  # noqa: BLE001 - a record that cannot be written never fails the call
            log.warning("LLM record not written (%s: %s) — the call itself is not affected", type(e).__name__, e)

    def close(self) -> None:
        """안쪽 클라이언트에 `close()`가 있으면 호출한다."""
        fn = getattr(self.inner, "close", None)
        if callable(fn):
            fn()


def load_replay_responses(record_dir: str | Path) -> list[str]:
    """기록된 세션의 답변 텍스트를 파일 순서대로 읽어 반환한다.
    순서대로 재생하기 위해 FakeLLMClient에 넣는다.

    Responses of a recorded session (llm-*.json, in order) — feed to FakeLLMClient for replay."""
    return [e.response for e in load_replay_entries(record_dir)]


class ReplayEntry:
    """기록된 답 하나(`llm-NNNN.json`)를 담는 클래스.
    `response`와 선택 항목 `replay_role`, `match` 정규식을 가진다.
    role이 fix인 답은 fix_escalation 호출에도 답하지만 verify_guide 호출에는 답하지 않는다.
    기록기가 쓴 `role` 키는 답을 고를 때 쓰지 않는다.

    One recorded answer (`llm-NNNN.json`): `response` text, optional `replay_role` (fix | structure_review |
    verify_guide | …; "fix" also answers fix_escalation calls, never verify_guide ones — the operator guide call is
    answered by a `replay_role: verify_guide` entry or an entry without a role) and optional `match` regex searched
    in the request's non-system messages. (The recorder's own `role` key is informational and never selects.)"""

    def __init__(self, response: str, *, role: Optional[str] = None, match: Optional[str] = None, name: str = ""):
        self.response = response
        self.role = role or None
        self.match = match or None
        self.name = name
        self._rx = re.compile(match, re.MULTILINE) if match else None

    def matches(self, text: str, role: Optional[str]) -> bool:
        """이 답이 요청에 맞는지 확인한다. role이 맞아야 하고, `match`가 있으면 요청 본문에서 찾아져야 한다."""
        if self.role and self.role != (role or "") and not (self.role == "fix" and role == "fix_escalation"):
            return False
        return self._rx is None or bool(self._rx.search(text))


def load_replay_entries(record_dir: str | Path) -> list[ReplayEntry]:
    """재생 디렉토리에서 `response`가 있는 `llm-*.json`을 모두 파일 이름 순으로 읽는다.
    직접 만든 픽스처는 `match` / `replay_role`로 요청 내용에 맞는 답을 고른다.
    e2e 픽스처의 `match: "case: <case_id>"`는 수정 프롬프트의 `case: <id>` 줄을 찾는다.

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
    """요청마다 맞는 기록된 답을 골라 주는 클래스.
    role과 `match`가 맞는 항목 중 아직 쓰지 않은 첫 항목을 반환한다.
    다 썼으면 마지막 항목을 반복하고, 맞는 항목이 없으면 경고를 남기고 "no fix"로 답한다.

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
        """이 요청에 맞는 기록된 답 텍스트를 반환한다."""
        # system 프롬프트는 모든 요청에서 같아 구분에 쓸 수 없으므로 빼고 찾는다
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
def agent_url(base_url: str, asset_id: Optional[str]) -> str:
    """``LLM_BASE_URL``의 ``{asset_id}`` / ``${LLM_ASSET_ID}`` 플레이스홀더를 에셋 id로 채운다.
    플레이스홀더가 없으면 그대로 두고, 있는데 id가 없으면 LLMConfigError를 던진다.

    Fill a `{asset_id}` / `${LLM_ASSET_ID}` placeholder in the gateway URL from the asset id; verbatim without one."""
    url = base_url or ""
    if "{asset_id}" in url or "${LLM_ASSET_ID}" in url:
        if not asset_id:
            raise LLMConfigError("LLM_BASE_URL names {asset_id} but LLM_ASSET_ID is not set")
        url = url.replace("{asset_id}", asset_id).replace("${LLM_ASSET_ID}", asset_id)
    return url


def _unexpanded(v: Optional[str]) -> bool:
    return not v or "${" in v


def make_llm_client(cfg: Config, *, ws: Any = None, transport: Optional[httpx.BaseTransport] = None) -> LLMClientLike:
    """설정에 맞는 LLM 클라이언트를 만든다. 실행마다 한 번 호출한다.
    `company`(기본)는 CompanyClient(LLM_BASE_URL로
    requests.post, `Authorization: Bearer <LLM_API_KEY>`), `openai_compatible` / `anthropic` → httpx
    클라이언트, `fake`는 FakeLLMClient를 쓴다.
    `plugin`이나 내장이 아닌 이름은 `sbmig.llm.resolve_llm_plugin`에 맡긴다.
    - `fake`에 `base_url: replay:<dir>`을 주면 기록을 재생한다
    - `llm.record_dir`가 있으면 RecordingClient로 감싼다
    - 키, URL, 모델이 없으면 LLMConfigError를 던진다

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
        # 키는 환경 변수에서만 읽고 오류 메시지에도 변수 이름만 쓴다.
        # .env는 load_config가 환경 변수로 넣어 준다
        key = os.environ.get(llm.api_key_env or "")
        # v2.1z-q: with a token exchange (llm.token_url) the APP token is the credential — LLM_API_KEY is then optional
        token_url_set = llm.provider == "company" and not _unexpanded(str(getattr(llm, "token_url", "") or ""))
        app_env = str(getattr(llm, "app_token_env", "") or "")
        if not key and token_url_set and app_env and os.environ.get(app_env):
            key = os.environ.get(app_env, "")
        if not key:
            if token_url_set:
                raise LLMConfigError(
                    f"LLM provider '{llm.provider}' with a token exchange needs the app token in the environment variable "
                    f"'{app_env or llm.api_key_env}' (or '{llm.api_key_env}'); set it, or use llm.provider=fake")
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
            from .company import TokenManager

            gateway = (str(getattr(llm, "gateway", "legacy") or "legacy")).strip().lower()
            if gateway not in ("legacy", "agent"):
                raise LLMConfigError(f"llm.gateway must be legacy or agent, not {gateway!r} (LLM_GATEWAY)")
            asset_id = None if _unexpanded(str(getattr(llm, "asset_id", "") or "")) else str(llm.asset_id).strip()
            if gateway == "agent" and not asset_id:
                raise LLMConfigError("LLM_ASSET_ID is not set (.env or environment): llm.gateway=agent sends the gateway asset "
                                     "id as the top-level `model`")
            tokens = None
            token_url = str(getattr(llm, "token_url", "") or "")
            if not _unexpanded(token_url) and token_url.strip():
                app_env = str(getattr(llm, "app_token_env", "") or "")
                app_token = (os.environ.get(app_env, "") if app_env else "") or key
                tokens = TokenManager(token_url, app_token, timeout=min(30.0, float(llm.timeout_seconds)),
                                      margin_s=float(getattr(llm, "token_refresh_margin_s", 300) or 300))
            # v2.1z-q: the agent gateway's URL carries the asset id in its path (…/agents/<asset id>/chat/completions) — a
            # `{asset_id}` (or `${LLM_ASSET_ID}`) placeholder in LLM_BASE_URL is filled from LLM_ASSET_ID, so the id lives once
            url = agent_url(llm.base_url, asset_id)
            client = CompanyClient(url, key, llm.model, payload_extra=llm.payload_extra,
                                   response_path=llm.response_path or None, timeout=float(llm.timeout_seconds),
                                   max_retries=llm.max_retries, extra_headers=llm.extra_headers, gateway=gateway,
                                   asset_id=asset_id, token_manager=tokens,
                                   agent_extra=dict(getattr(llm, "agent_extra", None) or {}),
                                   # 여러 파드가 게이트웨이를 같이 쓰므로 429는 기다리고, 새로 받은 토큰이
                                   # 401이면 토큰을 다시 받는다
                                   busy_wait_s=float(llm.busy_wait_seconds), auth_retries=int(llm.auth_retries))
        else:  # pragma: no cover - non-builtin providers are routed to sbmig.llm above
            raise LLMConfigError(f"unknown llm.provider: {llm.provider}")
    if llm.record_dir:
        client = RecordingClient(client, cfg.resolve_path(llm.record_dir), ws=ws)
    return client
