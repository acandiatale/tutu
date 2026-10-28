"""회사 LLM 게이트웨이 클라이언트 — 평범하게 ``requests.post(url,
headers=, json=, timeout=)`` 한 번(기본 provider `company`). 흐름상 위치: `make_llm_client(cfg)` 가 만든다.
수정 루프(LLMFixer)·구조 검토·운영자 가이드(verify_guide)의 LLM 호출이 모두 여기를 지나고, `sbmig llm-probe`
와 fleet preflight 는 `probe_llm` 으로 작은 호출 하나를 보낸다. 설정은 환경변수 / `.env` 뿐: LLM_BASE_URL(전체
엔드포인트, 그대로 씀), LLM_API_KEY(bearer 토큰), LLM_MODEL, 선택 LLM_RESPONSE_PATH. 본문은 `{model, messages,
**llm.payload_extra}`(기본 `need_origin`) — 게이트웨이는 messages 만 받고(tools 없음), max_tokens /
temperature / stop 은 보내지 않는다(게이트웨이 소관). 왜 이렇게 단순한가: 게이트웨이 503 오류를 겪은 뒤 사용자
요청 — yml 프로필·LangChain·httpx 설정·세션·커넥션 풀·TLS 설정 없이, 다른 사내 에이전트가 손으로 하는 그대로
둔다. 새 기능을 붙이고 싶어도 이 모양은 유지할 것. 지킬 것: 키는 헤더에만 넣고 repr·로그·기록에 남기지 않는다.
실패 문구(round 10)는 운영자가 콘솔에서 옮겨 적고 루프가 분류한다 — 전송 장애는 `cause=<예외 클래스>: <ASCII
메시지>`, 빈 답 + `finish_reason=length` 는 요청 거절. 재시도는 전송 오류·RETRY_STATUS 만 `max_retries`
번(Retry-After 우선 최대 120초, 없으면 1·2·4…초), 호출 하나의 벽시계 상한은 기본 `timeout + 30초`.

The company LLM gateway, the plain way: ``requests.post(url, headers=, json=, timeout=)``.

Everything comes from the environment / ``.env`` (read by ``load_config``):

    LLM_BASE_URL   the full endpoint URL (http or https, used verbatim)
    LLM_API_KEY    bearer token
    LLM_MODEL      model name (default gpt-6-luna)
    LLM_RESPONSE_PATH   optional dot path to the reply text when auto-detection fails (e.g. data.result.content)

Request:  POST $LLM_BASE_URL, headers {"Authorization": "Bearer $LLM_API_KEY"} (+ llm.extra_headers),
          json {"model": ..., "messages": [{"role", "content"}, ...], **llm.payload_extra}   (default extra: need_origin)
Response: the reply text is auto-detected (choices[0].message.content, content, response, data.content, ...) or
          taken from the configured path; token usage from a ``usage`` object when present.

Generation parameters (max_tokens / temperature / stop) are NOT sent: the gateway owns them. ``role`` / ``metadata``
are accepted and ignored (RecordingClient journals them). No sessions, no connection pools, no TLS knobs: exactly
what the other in-house agents do by hand.

Failure texts (round 10; ``sbmig.loop.fixloop`` classifies them, operators retype them from the console):

* transport failures name the cause on the retry and give-up lines and in the ``LLMError``:
  ``cause=<ExceptionClass>: <first 120 chars of the innermost message>`` (ASCII; non-ASCII runs such as a Korean
  WinError text become ``?``) — e.g. ``cause=ConnectionError: Failed to establish a new connection: [WinError 10061] ?``;
* an empty answer with ``finish_reason=length`` (a reasoning model spent the gateway's max_tokens before writing) is a
  request rejection that says ``finish_reason=length``, never "set llm.response_path".

``probe_llm(cfg)`` is the one small call shared by ``sbmig llm-probe`` and the fleet preflight (``ProbeResult.line()``
is the short, retypeable ``LLM-PROBE …`` line).


"""
from __future__ import annotations

import contextvars
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import requests

from sbmig.core.interfaces import ChatMessage, LLMResponse

from .client import RETRY_STATUS, LLMError, _retry_after_seconds, extract_reply, messages_hash

try:                                            # round 10 (sbmig.fix.client): LLM calls in flight, for the watchdog
    from .client import llm_call_in_flight as _in_flight
except ImportError:  # pragma: no cover - older client module
    from contextlib import nullcontext as _in_flight

log = logging.getLogger("sbmig.fix.client")

PostFn = Callable[..., Any]   # requests.post-compatible: post(url, headers=, json=, timeout=) -> Response


class LLMHardDeadline(requests.RequestException):
    """호출 하나가 `hard_deadline_s` 를 넘김(게이트웨이가 연결을 닫지 않음) — 전송 장애로 친다.

    One call outlived `hard_deadline_s` (the gateway never closed the connection); a transport failure."""


CAUSE_CHARS = 120
_NON_ASCII_RUN_RE = re.compile(r"[^\x00-\x7f]+(?:\s+[^\x00-\x7f]+)*")
# "<urllib3.connection.HTTPConnection object at 0x…>: " (urllib3 1.x) / "HTTPConnection(host='gw', port=80): " (2.x) /
# "HTTPConnectionPool(host='gw', port=80): " — noise in front of the part that says what happened
_CONN_PREFIX_RE = re.compile(r"^(?:<[^<>]* object at 0x[0-9A-Fa-f]+>|\w*Connection(?:Pool)?\(host='[^']*', "
                             r"port=\d+\)|None):\s*")
_OS_ERROR_RE = re.compile(r"\[(?:WinError|Errno) -?\d+\][^'\"()]*")


def ascii_safe(text: Any, limit: int = CAUSE_CHARS) -> str:
    """운영자가 옮겨 적을 수 있는 ASCII 한 줄: 공백 정리, 비ASCII 덩어리(한국어 OS 메시지, 긴 대시)는 `?`,
    `limit` 자에서 자름. 회사 Windows PC 는 복사·붙여넣기가 안 되어 콘솔 줄을 손으로 다시 친다.

    One line of ASCII an operator can retype: whitespace collapsed, every non-ASCII run (a Korean OS message, an
    em dash) replaced by `?`, cut at `limit` characters."""
    t = _NON_ASCII_RUN_RE.sub("?", " ".join(str(text).split()))
    return t if len(t) <= limit else t[:max(0, limit - 3)] + "..."


def _exc_text(exc: BaseException) -> str:
    strs = [a for a in getattr(exc, "args", ()) if isinstance(a, str)]
    if len(getattr(exc, "args", ())) > 1 and strs:       # ConnectTimeoutError(<conn>, 'Connection to gw timed out…')
        return strs[-1]
    return str(exc)


def transport_cause(exc: BaseException, limit: int = CAUSE_CHARS) -> str:
    """전송 장애의 `<예외 클래스>: <짧은 메시지>`: 클래스는 바깥 것(ConnectionError, ProxyError, SSLError,
    ConnectTimeout, ReadTimeout, LLMHardDeadline), 메시지는 requests / urllib3 가 감싼 가장 안쪽 이유. 잘려도
    WinError / Errno 번호는 남긴다.

    `<ExceptionClass>: <short message>` for a transport failure: the class is the outer one (`ConnectionError`,
    `ProxyError`, `SSLError`, `ConnectTimeout`, `ReadTimeout`, `LLMHardDeadline`), the message the innermost
    reason requests / urllib3 wrapped (`Failed to establish a new connection: [WinError 10061] ?`,
    `[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: …`) — what used to be cut off after 300 chars."""
    texts: list[str] = []
    seen: set[int] = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen and len(seen) < 8:
        seen.add(id(cur))
        texts.append(_exc_text(cur))
        nxt = getattr(cur, "reason", None)                      # urllib3 MaxRetryError / ProxyError
        if not isinstance(nxt, BaseException):
            nxt = next((a for a in reversed(getattr(cur, "args", ())) if isinstance(a, BaseException)), None)
        cur = nxt if nxt is not None else (cur.__cause__ or cur.__context__)
    msg = next((t for t in reversed(texts) if t.strip()), "")
    msg = msg.strip()
    while _CONN_PREFIX_RE.match(msg):
        msg = _CONN_PREFIX_RE.sub("", msg, count=1)
    os_err = _OS_ERROR_RE.search(" ".join(texts))
    if os_err and os_err.group(0).split("]")[0] not in ascii_safe(msg, limit):
        msg = os_err.group(0)                                    # the error code must survive the cut
    name = type(exc).__name__
    return f"{name}: {ascii_safe(msg, limit)}" if msg else name


EMPTY_LENGTH_MESSAGE = ("empty answer with finish_reason=length: request rejected by its size - the model spent the "
                        "gateway's max_tokens (reasoning) before writing a reply; raise max_tokens on the gateway or "
                        "send a smaller prompt")


def finish_reason_of(data: Any) -> Optional[str]:
    """응답 JSON 의 finish_reason(choices[0] → 최상위 finish_reason → stop_reason 순)."""
    if not isinstance(data, dict):
        return None
    finish = None
    choices = data.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        finish = choices[0].get("finish_reason")
    return finish or data.get("finish_reason") or data.get("stop_reason")


class CompanyClient:
    """회사 LLM 게이트웨이 클라이언트(LLMClientLike). 요청 하나 =
    `requests.post(url, headers={Authorization: Bearer …}, json={model, messages, **payload_extra}, timeout)`;
    실패하면 재시도 후 LLMError."""
    provider = "company"

    def __init__(self, url: str, api_key: str, model: str, *, payload_extra: Optional[dict] = None,
                 response_path: Optional[str] = None, timeout: float = 180.0, max_retries: int = 3,
                 extra_headers: Optional[dict[str, str]] = None, post: Optional[PostFn] = None,
                 sleep: Callable[[float], None] = time.sleep, heartbeat_s: float = 30.0,
                 hard_deadline_s: Optional[float] = None):
        self.url = (url or "").strip()
        self._api_key = api_key or ""
        self.model = model
        self.payload_extra = dict(payload_extra or {})
        self.response_path = response_path or None
        self.timeout = float(timeout)
        self.max_retries = max(0, int(max_retries))
        self.extra_headers = dict(extra_headers or {})
        self._post = post or requests.post
        self._sleep = sleep
        self.heartbeat_s = float(heartbeat_s)       # "still in flight" log line every N s while a call blocks (0 = off)
        # 벽시계 상한: requests 의 timeout 은 바이트 사이 침묵만 센다 — 연결을 붙잡고 조금씩 흘리는
        # 게이트웨이는 실행을 몇 시간씩 멈출 수 있어, 상한을 넘으면 호출을 버리고 전송 장애로 처리한다
        # wall-clock cap on one call: `requests` timeout only counts silence between bytes, so a gateway that keeps the
        # connection open (keep-alive / trickled chunks) can hang a run for hours; past the deadline the call is
        # abandoned (its thread is left to die) and reported as a transport failure (retry → cool-down → unavailable)
        self.hard_deadline_s = float(hard_deadline_s) if hard_deadline_s is not None else self.timeout + 30.0

    def __repr__(self) -> str:                        # never leak the key
        return f"CompanyClient(url={self.url!r}, model={self.model!r})"

    # ---- the two things that go over the wire
    def headers(self) -> dict[str, str]:
        """보낼 헤더: `Authorization: Bearer <키>` + `llm.extra_headers`. 이 dict 는 로그·기록·화면에 내보내지
        말 것(키가 들어 있다)."""
        return {"Authorization": f"Bearer {self._api_key}", **self.extra_headers}

    def payload(self, messages: list[ChatMessage], model: Optional[str] = None) -> dict[str, Any]:
        """보낼 JSON 본문: `{model, messages: [{role, content}], **payload_extra}` — 게이트웨이 계약
        그대로(messages 만, tools·생성 파라미터 없음)."""
        return {"model": model or self.model,
                "messages": [{"role": m.role, "content": m.content} for m in messages],
                **self.payload_extra}

    def post_once(self, payload: dict[str, Any]) -> Any:
        """정확히 그 호출 모양의 ``requests.post`` 한 번(probe 와 재시도 루프가 함께 쓴다).

        One ``requests.post`` with the exact call shape; returns the ``requests.Response``."""
        return self._post(self.url, headers=self.headers(), json=payload, timeout=self.timeout)

    def _post_with_heartbeat(self, payload: dict[str, Any], label: str) -> Any:
        """post_once 를 작업 스레드에서 돌리고, 답이 없는 동안 `heartbeat_s` 마다 INFO 한 줄(운영자가 느린
        답과 멈춘 실행을 구별). `hard_deadline_s` 가 지나면 스레드를 버려 두고 LLMHardDeadline 을 던진다.

        post_once + an INFO line every `heartbeat_s` while the gateway has not answered (operators can tell a
        slow answer from a hung run; the lines reach <run dir>/sbmig.log and the web UI log panel)."""
        done = threading.Event()
        t0 = time.monotonic()

        def beat() -> None:
            while not done.wait(self.heartbeat_s):
                log.info("llm call still in flight: %.0fs elapsed (timeout %.0fs) — %s", time.monotonic() - t0,
                         self.timeout, label)

        # 두 스레드 모두 호출한 쪽 contextvars 의 복사본에서 돈다 — 웹 앱의 실행별 로그 필터가 이 값으로 줄을
        # 나누므로, 이것이 없으면 heartbeat 줄이 sbmig.log 에 남지 않는다
        # both helper threads run in a copy of the caller's context: the web app's per-run log filter attributes
        # records by the contextvar the run thread set, so without this the heartbeat lines never reach sbmig.log
        th = threading.Thread(target=contextvars.copy_context().run, args=(beat,), name="llm-heartbeat", daemon=True)
        if self.heartbeat_s > 0:
            th.start()
        result: dict[str, Any] = {}

        def call() -> None:
            try:
                result["resp"] = self.post_once(payload)
            except BaseException as exc:  # noqa: BLE001 - handed back to the caller's except clause
                result["exc"] = exc

        worker = threading.Thread(target=contextvars.copy_context().run, args=(call,), name="llm-call", daemon=True)
        worker.start()
        try:
            worker.join(self.hard_deadline_s if self.hard_deadline_s > 0 else None)
            if worker.is_alive():
                raise LLMHardDeadline(f"LLM call exceeded the hard deadline of {self.hard_deadline_s:.0f}s "
                                      f"(timeout {self.timeout:.0f}s only counts silence between bytes) - {label}")
            if "exc" in result:
                raise result["exc"]
            return result["resp"]
        finally:
            done.set()

    # ---- LLMClientLike
    def complete(self, messages: list[ChatMessage], *, model: Optional[str] = None, max_tokens: Optional[int] = None,
                 temperature: Optional[float] = None, stop: Optional[list[str]] = None, role: Optional[str] = None,
                 metadata: Optional[dict] = None, **_ignored: Any) -> LLMResponse:
        """메시지를 게이트웨이로 보낸다(LLMClientLike). `max_tokens` / `temperature` / `stop` / `role` /
        `metadata` 는 받기만 하고 보내지 않는다(RecordingClient 가 role / metadata 를 기록). DEBUG 로그에는
        프롬프트 해시·글자 수만."""
        payload = self.payload(messages, model)
        if log.isEnabledFor(logging.DEBUG):
            log.debug("llm request role=%s model=%s prompt_sha256=%s chars=%d", role or "-", payload["model"],
                      messages_hash(messages)[:16], sum(len(m.content) for m in messages))
        with _in_flight():                      # the fix loop's watchdog stays quiet while the gateway is working
            return self._complete(messages, payload, role)

    def _complete(self, messages: list[ChatMessage], payload: dict[str, Any], role: Optional[str]) -> LLMResponse:
        """재시도 루프: 시도마다 INFO 한 줄(URL·프롬프트 글자 수·timeout). 전송 오류(`cause=` 계산)와
        RETRY_STATUS(503 등)는 다시(Retry-After 우선 최대 120초, 없으면 1·2·4…초, 최대 30초), 그 밖의 HTTP
        오류는 즉시 LLMError. 포기 문구에는 마지막이 전송 장애면 `cause=`, HTTP 면 본문 앞부분이 들어간다."""
        t0 = time.monotonic()
        last_status: Optional[int] = None
        last_body = ""
        label = f"role={role or '-'} model={payload['model']}"
        chars = sum(len(m.content) for m in messages)
        cause: Optional[str] = None             # `<Class>: <message>` of the last transport failure (None after an HTTP answer)
        for attempt in range(self.max_retries + 1):
            wait: Optional[float] = None
            log.info("llm call provider=company %s attempt %d/%d → POST %s (prompt %d chars, timeout %.0fs)", label,
                     attempt + 1, self.max_retries + 1, self.url, chars, self.timeout)
            try:
                resp = self._post_with_heartbeat(payload, label)
            except (requests.RequestException, LLMHardDeadline) as exc:
                cause = transport_cause(exc)
                last_status, last_body = None, f"{type(exc).__name__}: {exc}"
                log.warning("llm company transport error (attempt %d/%d): %s cause=%s", attempt + 1,
                            self.max_retries + 1, type(exc).__name__, cause)
            else:
                cause = None
                if resp.status_code < 300:
                    try:
                        data = resp.json()
                    except ValueError as exc:
                        raise LLMError(resp.status_code, resp.text[:500], f"invalid JSON from LLM: {exc}") from exc
                    out = self._parse(data, payload["model"])
                    log.info("llm call provider=company model=%s in=%d out=%d latency=%.1fs finish=%s", out.model,
                             out.input_tokens, out.output_tokens, time.monotonic() - t0, out.finish_reason)
                    return out
                last_status, last_body = resp.status_code, resp.text[:500]
                if resp.status_code not in RETRY_STATUS:
                    log.error("llm company HTTP %d: %s", resp.status_code, last_body[:300].replace("\n", " "))
                    raise LLMError(resp.status_code, last_body)
                wait = _retry_after_seconds(resp)
                log.warning("llm company HTTP %d (attempt %d/%d)", resp.status_code, attempt + 1,
                            self.max_retries + 1)
            if attempt < self.max_retries:
                delay = min(wait, 120.0) if wait is not None else min(30.0, 2.0 ** attempt)
                log.info("llm call retrying in %.0fs (attempt %d/%d failed: status=%s%s)", delay, attempt + 1,
                         self.max_retries + 1, last_status, f" cause={cause}" if cause else "")
                self._sleep(delay)
        if cause:
            log.error("llm company giving up after %d attempt(s): status=%s cause=%s", self.max_retries + 1,
                      last_status, cause)
            raise LLMError(last_status, last_body,
                           f"LLM request failed after {self.max_retries + 1} attempt(s) (status={last_status} "
                           f"cause={cause})")
        log.error("llm company giving up after %d attempt(s): status=%s body=%s", self.max_retries + 1, last_status,
                  ascii_safe(last_body, 300))
        raise LLMError(last_status, last_body,
                       f"LLM request failed after {self.max_retries + 1} attempt(s) (status={last_status}): {last_body[:200]}")

    def _parse(self, data: Any, model: str) -> LLMResponse:
        """게이트웨이 JSON → LLMResponse. 콘텐츠 필터에 걸림 → LLMError(이 요청만의 거절, 장애 아님), 빈 답 +
        finish_reason=length → LLMError(요청 크기 문제), 잘린
        답은 경고만. 토큰 사용량은 `usage` 가 있을 때만."""
        filtered = content_filter_reason(data)
        if filtered:
            # Azure-style gateways answer HTTP 200 with finish_reason=content_filter and a null message.content when
            # the prompt or the reply tripped the content filter: this request's problem, not an outage (the fix
            # loop's request-rejected rule keeps it out of the "LLM unavailable" streak)
            raise LLMError(200, json.dumps(data, ensure_ascii=False)[:500],
                           f"content filtered by the gateway ({filtered}); this request only, not an outage")
        finish = finish_reason_of(data)
        usage = data.get("usage") if isinstance(data, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        out_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        try:
            content = extract_reply(data, self.response_path)
        except LLMError:
            if str(finish or "").lower() != "length":
                raise
            content = ""
        if str(finish or "").lower() == "length" and not content.strip():
            # a reasoning model used the gateway's max_tokens before it wrote anything: this request's size is the
            # problem (not the response shape, not the wire) — the fix loop counts it as a request rejection
            log.warning("llm company: empty answer, finish_reason=length (output tokens %d)", out_tokens)
            raise LLMError(200, json.dumps(data, ensure_ascii=False)[:500],
                           EMPTY_LENGTH_MESSAGE + (f" (output tokens {out_tokens})" if out_tokens else ""))
        if str(finish or "").lower() == "length":
            log.warning("llm company: answer cut at the gateway's max_tokens (finish_reason=length, output tokens %d)",
                        out_tokens)
        return LLMResponse(content=content, model=(data.get("model") if isinstance(data, dict) else None) or model,
                           input_tokens=int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
                           output_tokens=out_tokens, finish_reason=finish)

    def close(self) -> None:
        return None


def content_filter_reason(data: Any) -> Optional[str]:
    """OpenAI / Azure 식 응답이 콘텐츠 필터 때문에 답이 없으면
    그 이유, 아니면 None(HTTP 200 이어도 걸러진 답일 수 있다).

    Why an OpenAI / Azure-style response carries no answer because of the content filter, else None:
    `choices[0].finish_reason == "content_filter"`, a `content_filter_result` entry with `filtered: true` next to an
    empty message, or a `prompt_filter_results[*].content_filter_results` entry with `filtered: true`."""
    if not isinstance(data, dict):
        return None
    choices = data.get("choices")
    first = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    if str(first.get("finish_reason") or "").lower() == "content_filter":
        return "finish_reason=content_filter"
    message = first.get("message") if isinstance(first.get("message"), dict) else {}
    empty = not (message.get("content") or first.get("text"))
    for key, results in (("content_filter_result", first.get("content_filter_result")),
                         ("prompt_filter_results", data.get("prompt_filter_results"))):
        entries = results if isinstance(results, list) else [results]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            inner = entry.get("content_filter_results") if key == "prompt_filter_results" else entry
            if not isinstance(inner, dict):
                continue
            hit = [cat for cat, v in inner.items() if isinstance(v, dict) and v.get("filtered") is True]
            if hit and (empty or key == "prompt_filter_results"):
                return f"{key}: {', '.join(sorted(hit))}"
    return None


# ---------------------------------------------------------------------------
# One small call: `sbmig llm-probe` and the fleet preflight (round 10)
# ---------------------------------------------------------------------------
PROBE_PROMPT = "Reply with the single word: pong"
PROBE_SKIP_PROVIDERS = ("fake",)          # nothing to reach: the preflight skips the call


def probe_messages(prompt: str = PROBE_PROMPT, no_system: bool = False) -> list[dict]:
    """probe 요청 메시지: system 한 줄 + "pong" 요청(`sbmig llm-probe --no-system` 이면 user 하나만)."""
    msgs = [] if no_system else [{"role": "system", "content": "You are a helpful assistant."}]
    return msgs + [{"role": "user", "content": prompt}]


@dataclass
class ProbeResult:
    """probe 호출 하나의 결과. 실패 종류 `kind`: config | transport | http | reply | rejected.

    What one probe call showed. `kind` (when not ok): config | transport | http | reply | rejected."""
    ok: bool
    provider: str = ""
    model: str = ""
    url: str = ""
    kind: str = ""
    status: Optional[int] = None
    problem: str = ""                     # one ASCII line: `cause=…` / `HTTP 503: …` / the LLMError text
    hints: list[str] = field(default_factory=list)
    reply: str = ""
    latency_s: float = 0.0
    payload: Optional[dict] = None        # company provider: the exact JSON body that was sent
    response: Any = None                  # company provider: the requests.Response (verbose CLI output)
    data: Any = None                      # parsed JSON answer, when there was one
    client_name: str = ""

    @property
    def transient(self) -> bool:
        """저절로 나아질 수 있는 실패인가(게이트웨이 다운·과부하, 네트워크) — 설정 오류가 아님.

        A failure that may go away by itself (gateway down / overloaded, network) — not a configuration error."""
        if self.ok:
            return False
        if self.kind == "transport":
            return True
        return self.kind == "http" and self.status is not None and (self.status >= 500 or self.status in (408, 429))

    def line(self) -> str:
        """운영자가 옮겨 적는 한 줄(짧은 ASCII).

        `LLM-PROBE ok …` / `LLM-PROBE FAIL <kind> …`: short, ASCII, retypeable."""
        head = f"provider={self.provider} model={ascii_safe(self.model, 60)}"
        if self.ok:
            return (f"LLM-PROBE ok {head} status={self.status if self.status is not None else '-'} "
                    f"latency={self.latency_s:.1f}s reply={ascii_safe(self.reply, 40)!r}")
        return (f"LLM-PROBE FAIL {self.kind} {head} status={self.status if self.status is not None else '-'} "
                f"{ascii_safe(self.problem, 200)}")


def probe_llm(cfg: Any, prompt: str = PROBE_PROMPT, *, no_system: bool = False,
              on_request: Optional[Callable[[dict], None]] = None) -> ProbeResult:
    """설정된 provider 로 작은 요청 하나(재시도 없음 — probe 는 빨리 답하거나 왜 안 되는지 말한다). company 면
    실제 실행과 똑같은 payload 로 `post_once` 한 번, 다른 provider 는 `complete(…, role="other")`. LLM
    문제로는 예외를 내지 않고 ProbeResult 로 돌려준다(`sbmig llm-probe`, fleet preflight).

    Send ONE small request with the configured provider (no retries: a probe answers fast or says why not).

    company provider: a single `CompanyClient.post_once` with the configured timeout (the exact payload the runs
    send); other providers: `client.complete(…, role="other")`. Never raises for an LLM problem."""
    from sbmig.core.interfaces import ChatMessage

    from .client import LLMConfigError, make_llm_client

    llm = cfg.llm
    res = ProbeResult(ok=False, provider=str(llm.provider), model=str(llm.model), url=str(llm.base_url or ""))
    msgs = [ChatMessage(role=m["role"], content=m["content"]) for m in probe_messages(prompt, no_system)]
    try:
        client = make_llm_client(cfg)
    except (LLMConfigError, ValueError) as e:          # missing key / URL / model, plugin not loadable
        res.kind, res.problem = "config", ascii_safe(e, 200)
        res.hints.append("put LLM_BASE_URL / LLM_API_KEY / LLM_MODEL in .env at the repo root (copy .env.example) "
                         "or export them in the shell; `--llm-provider fake` runs without a gateway")
        return res
    inner = getattr(client, "inner", client)
    res.client_name = type(inner).__name__
    t0 = time.monotonic()
    if isinstance(inner, CompanyClient):
        res.url, res.model = inner.url, inner.model
        payload = res.payload = inner.payload(msgs)
        if on_request is not None:
            on_request(payload)
        try:
            resp = inner.post_once(payload)
        except Exception as e:  # noqa: BLE001 - transport-level failure: the cause says proxy / DNS / refused / TLS
            res.latency_s = time.monotonic() - t0
            res.kind, res.problem = "transport", f"cause={transport_cause(e)}"
            res.hints.append("check the URL (the full endpoint, http://...) and the proxy (NO_PROXY=<gateway host> "
                             "in .env); `sbmig llm-probe --bare` sends the same payload with a bare requests.post")
            return res
        res.latency_s = time.monotonic() - t0
        res.response, res.status = resp, resp.status_code
        try:
            res.data = resp.json()
        except ValueError:
            res.data = None
        if resp.status_code >= 300:
            res.kind, res.problem = "http", f"HTTP {resp.status_code}: {ascii_safe(resp.text, 120)}"
            return res
        if res.data is None:
            res.kind, res.problem = "reply", "the answer is not JSON"
            return res
        try:
            out = inner._parse(res.data, inner.model)
        except LLMError as e:
            text = str(e)
            if "finish_reason=length" in text or "content filtered" in text:
                res.kind, res.problem = "rejected", ascii_safe(text, 200)
                res.hints.append("raise max_tokens on the gateway (a reasoning model used it up) or send a smaller "
                                 "prompt" if "finish_reason=length" in text else
                                 "the gateway's content filter rejected this request (not an outage)")
            else:
                from .client import _REPLY_PATHS
                res.kind, res.problem = "reply", ascii_safe(text, 200)
                res.hints.append("auto-detect paths tried: " + ", ".join(_REPLY_PATHS)
                                 + "  -> set LLM_RESPONSE_PATH in .env to the dot path of the text in the JSON above")
            return res
        res.ok, res.reply = True, out.content
        return res
    try:
        out = client.complete(msgs, role="other")
    except LLMError as e:
        res.latency_s = time.monotonic() - t0
        res.status = e.status
        res.kind = "http" if e.status is not None and e.status != 200 else ("reply" if e.status == 200 else "transport")
        res.problem = ascii_safe(e, 200)
        return res
    except Exception as e:  # noqa: BLE001 - plugins / SDK clients raise their own transport errors
        res.latency_s = time.monotonic() - t0
        res.kind, res.problem = "transport", f"cause={transport_cause(e)}"
        return res
    res.latency_s = time.monotonic() - t0
    res.ok, res.reply = True, getattr(out, "content", "") or ""
    res.model = getattr(out, "model", None) or res.model
    return res
