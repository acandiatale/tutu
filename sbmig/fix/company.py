"""회사 LLM 게이트웨이를 호출하는 모듈. 기본 provider `company`가 이 클라이언트다.

The company LLM gateway, the plain way: ``requests.post(url, headers=, json=, timeout=)``.

`make_llm_client(cfg)` builds the client; the fix loop, the structure review and the operator guide all call
the LLM through it. The key goes into the header only, never into a repr, a log or a record. Only transport
errors and `RETRY_STATUS` are retried, `max_retries` times; one call's total time is capped at
`timeout + 30s` by default.

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

Failure texts (``sbmig.loop.fixloop`` classifies them, operators retype them from the console):

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
import random
import re
import threading
import uuid
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import requests

from sbmig.core.interfaces import ChatMessage, LLMResponse
from sbmig.core.scrub import note_secret

from .client import (RETRY_STATUS, LLMError, _retry_after_seconds, extract_reply, messages_hash, named_response,
                     served_log_suffix, served_models_of)

try:                                            # 호출 중임을 watchdog에 알리는 컨텍스트
    from .client import llm_call_in_flight as _in_flight
except ImportError:  # pragma: no cover - 이전 client 모듈
    from contextlib import nullcontext as _in_flight

log = logging.getLogger("sbmig.fix.client")

PostFn = Callable[..., Any]   # requests.post와 같은 형태: post(url, headers=, json=, timeout=) -> Response


class LLMHardDeadline(requests.RequestException):
    """호출 하나가 `hard_deadline_s`를 넘기면 던지는 예외 클래스. 전송 장애로 본다.

    Raised when the gateway never closes the connection."""


CAUSE_CHARS = 120
_NON_ASCII_RUN_RE = re.compile(r"[^\x00-\x7f]+(?:\s+[^\x00-\x7f]+)*")
# urllib3가 메시지 앞에 붙이는 연결 객체 표기를 지운다
# "<urllib3.connection.HTTPConnection object at 0x…>: " (1.x), "HTTPConnection(host='gw', port=80): " (2.x),
# "HTTPConnectionPool(host='gw', port=80): "
_CONN_PREFIX_RE = re.compile(r"^(?:<[^<>]* object at 0x[0-9A-Fa-f]+>|\w*Connection(?:Pool)?\(host='[^']*', "
                             r"port=\d+\)|None):\s*")
_OS_ERROR_RE = re.compile(r"\[(?:WinError|Errno) -?\d+\][^'\"()]*")


def ascii_safe(text: Any, limit: int = CAUSE_CHARS) -> str:
    """텍스트를 운영자가 손으로 다시 입력할 수 있는 ASCII 한 줄로 바꾼다.
    공백은 하나로 합치고, ASCII가 아닌 문자 묶음은 `?`로 바꾼다. `limit`자를 넘으면 자른다.
    회사 Windows PC에서는 복사와 붙여넣기가 안 되어 콘솔 줄을 손으로 다시 입력한다."""
    t = _NON_ASCII_RUN_RE.sub("?", " ".join(str(text).split()))
    return t if len(t) <= limit else t[:max(0, limit - 3)] + "..."


def _exc_text(exc: BaseException) -> str:
    strs = [a for a in getattr(exc, "args", ()) if isinstance(a, str)]
    if len(getattr(exc, "args", ())) > 1 and strs:       # ConnectTimeoutError(<conn>, '…')는 마지막 문자열이 원인이다
        return strs[-1]
    return str(exc)


def transport_cause(exc: BaseException, limit: int = CAUSE_CHARS) -> str:
    """전송 장애를 `<예외 클래스>: <짧은 메시지>` 형식으로 만든다.
    클래스는 바깥 예외의 것을, 메시지는 requests / urllib3가 감싼 가장 안쪽 원인을 쓴다.
    잘리더라도 WinError / Errno 번호는 남긴다.

    Outer classes: `ConnectionError`, `ProxyError`, `SSLError`, `ConnectTimeout`, `ReadTimeout`,
    `LLMHardDeadline`. Example messages: `Failed to establish a new connection: [WinError 10061] ?`,
    `[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: …`. The chain walk stops after 8 levels."""
    texts: list[str] = []
    seen: set[int] = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen and len(seen) < 8:
        seen.add(id(cur))
        texts.append(_exc_text(cur))
        nxt = getattr(cur, "reason", None)                      # urllib3의 MaxRetryError / ProxyError
        if not isinstance(nxt, BaseException):
            nxt = next((a for a in reversed(getattr(cur, "args", ())) if isinstance(a, BaseException)), None)
        cur = nxt if nxt is not None else (cur.__cause__ or cur.__context__)
    msg = next((t for t in reversed(texts) if t.strip()), "")
    msg = msg.strip()
    while _CONN_PREFIX_RE.match(msg):
        msg = _CONN_PREFIX_RE.sub("", msg, count=1)
    os_err = _OS_ERROR_RE.search(" ".join(texts))
    if os_err and os_err.group(0).split("]")[0] not in ascii_safe(msg, limit):
        msg = os_err.group(0)                                    # 잘려도 오류 번호는 남긴다
    name = type(exc).__name__
    return f"{name}: {ascii_safe(msg, limit)}" if msg else name


EMPTY_LENGTH_MESSAGE = ("empty answer with finish_reason=length: request rejected by its size - the model spent the "
                        "gateway's max_tokens (reasoning) before writing a reply; raise max_tokens on the gateway or "
                        "send a smaller prompt")


def finish_reason_of(data: Any) -> Optional[str]:
    """응답 JSON에서 finish_reason을 찾는다. choices[0], 최상위 finish_reason, stop_reason 순서로 확인한다.
    없으면 None을 반환한다."""
    if not isinstance(data, dict):
        return None
    finish = None
    choices = data.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        finish = choices[0].get("finish_reason")
    return finish or data.get("finish_reason") or data.get("stop_reason")


# 여러 빌드 파드가 같은 게이트웨이를 호출한다. 그래서 바쁘다는 응답은 장애로 보지 않고 기다린다
BUSY_WITH_RETRY_AFTER = frozenset({502, 503, 504})   # Retry-After가 있을 때만 바쁨으로 본다. 없으면 일반 5xx로 재시도한다
BUSY_FIRST_DELAY = 2.0          # Retry-After 없는 바쁨의 첫 대기 시간. 두 배씩 늘리고 무작위 지연을 더한다
BUSY_MAX_DELAY = 60.0           # 바쁨 대기 시간의 상한
BUSY_RETRY_AFTER_CAP = 120.0    # Retry-After를 따르되 한 번에 이보다 오래 쉬지 않는다
BUSY_MIN_DELAY = 1.0            # Retry-After가 0이어도 이만큼은 기다린다. 쉬지 않고 반복하지 않게 한다
BUSY_LOG_EVERY = 60.0           # "still busy" 로그를 남기는 간격. 기다린 시간으로 잰다
AUTH_PAUSE_MIN, AUTH_PAUSE_MAX = 1.0, 5.0   # 토큰 교환 직후 401이 오면 다시 교환하기 전에 쉬는 무작위 시간(초)

TOKEN_RACE_WARNING = ("LLM gateway rejected a token right after it was exchanged \u2014 another client may be exchanging "
                      "the same app token (parallel build pods?)")
_TOKEN_RACE_WARNED = False
_TOKEN_RACE_LOCK = threading.Lock()


def _warn_token_race_once() -> None:
    """토큰 교환 직후 401이 왔다는 경고를 프로세스에서 한 번만 남긴다."""
    global _TOKEN_RACE_WARNED
    with _TOKEN_RACE_LOCK:
        if _TOKEN_RACE_WARNED:
            return
        _TOKEN_RACE_WARNED = True
    log.warning(TOKEN_RACE_WARNING)


class TokenManager:
    """사내 GPT 에이전트 게이트웨이의 토큰을 받아 관리하는 클래스.
    ``POST <token_url>``에 앱 토큰을 Bearer로 보내 access token을 받는다. 받은 토큰은 메모리에만 둔다.
    만료 ``margin_s``초 전이거나 401이 오면 다시 받는다. 두 토큰 모두 로그, repr, 기록에 남기지 않는다.

    Answer: `access_token` + `expires_in` (default 3600 when missing / unreadable)."""

    def __init__(self, token_url: str, app_token: str, *, post: Optional[PostFn] = None, timeout: float = 30.0,
                 margin_s: float = 300.0, clock: Callable[[], float] = time.time):
        self.token_url = (token_url or "").strip()
        self._app_token = app_token or ""
        self._post = post or requests.post
        self.timeout = float(timeout)
        self.margin_s = float(margin_s)
        self._clock = clock
        self._lock = threading.Lock()
        self._token = ""
        self._expires_at = 0.0
        self.expires_in = 0.0                   # 마지막 응답의 유효 시간(초), probe가 보여 준다
        self.refreshes = 0

    def __repr__(self) -> str:                  # 토큰이 새어 나가지 않게 한다
        return f"TokenManager(url={self.token_url!r}, refreshes={self.refreshes})"

    def valid(self) -> bool:
        """토큰이 있고 만료까지 ``margin_s``초보다 많이 남았는지 확인한다."""
        return bool(self._token) and self._clock() < self._expires_at - self.margin_s

    def token(self) -> str:
        """요청에 쓸 토큰을 반환한다. 토큰이 없거나 곧 만료되면 먼저 새로 받는다."""
        with self._lock:
            if not self.valid():
                self._refresh_locked()
            return self._token

    def refresh(self) -> str:
        """토큰을 무조건 새로 받는다. probe와 401 뒤에 호출한다."""
        with self._lock:
            self._refresh_locked()
            return self._token

    def invalidate(self, rejected: Optional[str] = None) -> None:
        """다음 ``token()`` 호출이 토큰을 새로 받게 한다.
        ``rejected``를 주면 지금 토큰이 그 거절된 토큰일 때만 버린다.
        다른 호출이 이미 새로 받은 토큰은 버리지 않는다. 또 교환하면 그 토큰이 무효가 될 수 있다."""
        with self._lock:
            if rejected is not None and self._token and self._token != rejected:
                return
            self._token, self._expires_at = "", 0.0

    def _refresh_locked(self) -> None:
        try:
            resp = self._post(self.token_url, headers={"Authorization": f"Bearer {self._app_token}"},
                              timeout=self.timeout)
        except requests.RequestException as exc:
            # 연결 실패도 게이트웨이 장애로 보고 재시도한다. 예외 문구에 앱 토큰이 들어가지 않게 원인 이름만 남긴다
            raise TokenExchangeError(None, f"transport ({type(exc).__name__})",
                                     cause=transport_cause(exc)) from None
        code = resp.status_code
        if code >= 300:
            log.error("llm token exchange HTTP %d (%d character(s) of body, not logged: a token endpoint's answer may "
                      "carry credentials)", code, len(getattr(resp, "text", "") or ""))
            raise TokenExchangeError(code, f"HTTP {code}")
        try:
            data = resp.json()
        except ValueError:
            raise TokenExchangeError(code, "the answer is not JSON") from None
        tok = data.get("access_token") if isinstance(data, dict) else None
        if not tok:
            raise TokenExchangeError(code, "no access_token in the answer")
        try:
            ttl = float(data.get("expires_in") or 3600)
        except (TypeError, ValueError):
            ttl = 3600.0
        self._token, self._expires_at, self.expires_in = str(tok), self._clock() + ttl, ttl
        note_secret(self._token)     # 환경 변수에 없는 비밀 값이다. 출력 마스킹 대상에 등록한다
        self.refreshes += 1
        log.info("llm token refreshed: expires in %.0fs (refreshed again %.0fs before expiry)", ttl, self.margin_s)


# 수정 루프가 전송 장애로 세는 상태 코드는 status=로 쓰고, 나머지(400 포함)는 cause=로 쓴다
_TOKEN_TRANSPORT_STATUS = frozenset({401, 403, 404, 408, 429})


class TokenExchangeError(LLMError):
    """토큰 교환이 실패했을 때 던지는 예외 클래스. 게이트웨이 장애로 보고 채팅 호출처럼 재시도한다.
    메시지에는 상태 코드와 원인 이름만 넣고 응답 본문이나 토큰은 넣지 않는다.

    Covers HTTP errors, connection failures, a non-JSON answer, no `access_token`. The message carries
    `status=<code>` for the statuses `is_llm_transport_error` counts (401 / 403 / 404 / 408 / 429 / 5xx) and
    `cause=…` otherwise."""

    def __init__(self, status: Optional[int], what: str, *, cause: Optional[str] = None):
        if status is not None and (status in _TOKEN_TRANSPORT_STATUS or 500 <= status < 600):
            marker = f"status={status}"
        else:
            marker = f"cause={cause or 'TokenExchangeError'}" if status is None else "cause=TokenExchangeError"
        super().__init__(status, "", f"token exchange failed: {what} ({marker})")
        self.what = what


class CompanyClient:
    """회사 LLM 게이트웨이를 호출하는 클라이언트 클래스(LLMClientLike).
    요청 하나에 `requests.post`를 한 번 호출한다. 실패하면 재시도한 뒤 `LLMError`를 던진다."""
    provider = "company"

    def __init__(self, url: str, api_key: str, model: str, *, payload_extra: Optional[dict] = None,
                 response_path: Optional[str] = None, timeout: float = 180.0, max_retries: int = 3,
                 extra_headers: Optional[dict[str, str]] = None, post: Optional[PostFn] = None,
                 sleep: Callable[[float], None] = time.sleep, heartbeat_s: float = 30.0,
                 hard_deadline_s: Optional[float] = None, gateway: str = "legacy", asset_id: Optional[str] = None,
                 token_manager: Optional[TokenManager] = None, agent_extra: Optional[dict] = None,
                 busy_wait_s: float = 600.0, auth_retries: int = 2,
                 rand: Callable[[float, float], float] = random.uniform):
        self.url = (url or "").strip()
        # 여러 빌드 파드가 같은 게이트웨이를 쓴다. 바쁘다는 응답은 이 시간까지 기다리고, 교환 직후 401이면
        # `auth_retries`번 더 교환한다. `rand`는 무작위 지연 함수이고 테스트에서 고정값을 넣는다
        self.busy_wait_s = max(0.0, float(busy_wait_s or 0))
        self.auth_retries = max(0, int(auth_retries or 0))
        self._rand = rand
        # legacy는 {model, messages, **payload_extra}를 보낸다
        # agent는 사내 GPT 에이전트 게이트웨이다. asset id를 `model`로, 고른 모델을 `params.role`로, 교환한 토큰을 Bearer로 보낸다
        self.gateway = (gateway or "legacy").strip().lower()
        self.asset_id = (asset_id or "").strip() or None
        self._tokens = token_manager
        self.agent_extra = dict(agent_extra or {})
        self.last_session_id: Optional[str] = None   # 마지막으로 받은 게이트웨이 세션 id
        self._api_key = api_key or ""
        self.model = model
        self.payload_extra = dict(payload_extra or {})
        self.response_path = response_path or None
        self.timeout = float(timeout)
        self.max_retries = max(0, int(max_retries))
        self.extra_headers = dict(extra_headers or {})
        self._post = post or requests.post
        self._sleep = sleep
        self.heartbeat_s = float(heartbeat_s)       # 호출이 끝나지 않는 동안 N초마다 로그를 남긴다. 0이면 끈다
        # requests의 timeout은 바이트 사이의 대기만 잰다. 연결을 잡고 조금씩 보내는 게이트웨이면 실행이 몇 시간씩
        # 멈출 수 있다. 전체 시간 상한을 넘으면 호출 스레드를 버리고 전송 장애로 처리한다
        self.hard_deadline_s = float(hard_deadline_s) if hard_deadline_s is not None else self.timeout + 30.0

    def __repr__(self) -> str:                        # 키가 새어 나가지 않게 한다
        return f"CompanyClient(url={self.url!r}, model={self.model!r}, gateway={self.gateway!r})"

    # ---- 실제로 보내는 헤더와 본문
    def headers(self) -> dict[str, str]:
        """요청에 보낼 헤더를 만든다. `Authorization: Bearer <키>`와 `llm.extra_headers`가 들어간다.
        키가 들어 있으니 로그, 기록, 화면에 내보내지 않는다."""
        return self._auth_headers()[0]

    def _auth_headers(self) -> tuple[dict[str, str], str]:
        key = self._tokens.token() if self._tokens is not None else self._api_key
        return {"Authorization": f"Bearer {key}", **self.extra_headers}, key

    def payload(self, messages: list[ChatMessage], model: Optional[str] = None, *, max_tokens: Optional[int] = None,
                temperature: Optional[float] = None, metadata: Optional[dict] = None) -> dict[str, Any]:
        """요청에 보낼 JSON 본문을 만든다.
        - legacy: model, messages, payload_extra를 회사 형식 그대로 보낸다
        - agent: asset id를 `model`로, 고른 모델 이름을 `params.role`로 보낸다. 호출마다 새 trace_id를 넣는다
        - agent의 session_id는 세션을 이어 갈 때만 보낸다. 그 id는 ``metadata["session_id"]``로 받는다

        agent also sends max_tokens / temperature when given; `thread_id` is deprecated and never sent."""
        msgs = [{"role": m.role, "content": m.content} for m in messages]
        if self.gateway != "agent":
            return {"model": model or self.model, "messages": msgs, **self.payload_extra}
        body: dict[str, Any] = {"model": self.asset_id or (model or self.model), "messages": msgs, "stream": False,
                                "async": False, "params": {"role": model or self.model}}
        # session_id가 없으면 게이트웨이가 새 세션을 열고 id를 돌려준다. 이어 갈 때만 그 id를 보낸다
        sid = (metadata or {}).get("session_id") if isinstance(metadata, dict) else None
        if sid:
            body["session_id"] = str(sid)
        body["trace_id"] = uuid.uuid4().hex
        if max_tokens is not None:
            body["max_tokens"] = int(max_tokens)
        if temperature is not None:
            body["temperature"] = float(temperature)
        body.update(self.agent_extra)
        return body

    def asked_name(self, payload: dict[str, Any]) -> str:
        """요청 본문에서 이번 호출이 요청한 모델 이름을 꺼낸다.
        agent 게이트웨이는 `params.role`을, legacy는 `model`을 쓴다. agent의 `model`은 asset id다."""
        if self.gateway == "agent":
            params = payload.get("params")
            role = params.get("role") if isinstance(params, dict) else None
            return str(role or self.model or "")
        return str(payload.get("model") or self.model or "")

    def _label(self, payload: dict[str, Any], role: Optional[str]) -> str:
        """로그에 쓸 라벨을 만든다. agent 모드면 `params.role`을 붙인다."""
        extra = f" params.role={payload.get('params', {}).get('role')}" if self.gateway == "agent" else ""
        return f"role={role or '-'} model={payload['model']}{extra}"

    def post_once(self, payload: dict[str, Any], _sent: Optional[dict] = None) -> Any:
        """주어진 본문 그대로 ``requests.post``를 한 번 호출하고 응답을 반환한다. probe와 재시도 루프가 같이 쓴다.
        401 뒤에 그 토큰만 버릴 수 있게 ``_sent``에 보낸 토큰을 남긴다. 로그에는 쓰지 않는다."""
        headers, key = self._auth_headers()
        if _sent is not None:
            _sent["token"] = key
        return self._post(self.url, headers=headers, json=payload, timeout=self.timeout)

    def _post_with_heartbeat(self, payload: dict[str, Any], label: str, sent: Optional[dict] = None) -> Any:
        """`post_once`를 작업 스레드에서 실행한다. 답을 기다리는 동안 `heartbeat_s`마다 INFO 로그를 한 줄 남긴다.
        그래야 운영자가 느린 답과 멈춘 실행을 구분할 수 있다.
        `hard_deadline_s`가 지나면 스레드를 버리고 `LLMHardDeadline`을 던진다."""
        done = threading.Event()
        t0 = time.monotonic()

        def beat() -> None:
            while not done.wait(self.heartbeat_s):
                log.info("llm call still in flight: %.0fs elapsed (timeout %.0fs) — %s", time.monotonic() - t0,
                         self.timeout, label)

        # 웹 앱은 contextvar로 실행별 로그를 나눈다. 그래서 두 스레드 모두 호출한 쪽 context의 복사본에서 실행한다
        # 그렇지 않으면 heartbeat 줄이 sbmig.log에 남지 않는다
        th = threading.Thread(target=contextvars.copy_context().run, args=(beat,), name="llm-heartbeat", daemon=True)
        if self.heartbeat_s > 0:
            th.start()
        result: dict[str, Any] = {}

        def call() -> None:
            try:
                result["resp"] = self.post_once(payload, sent)
            except BaseException as exc:  # noqa: BLE001 - 호출한 쪽 except에서 다시 던진다
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
        """메시지를 게이트웨이로 보내고 LLMResponse를 반환한다.
        legacy는 `max_tokens` / `temperature` / `stop` / `role` / `metadata`를 받기만 하고 보내지 않는다.
        agent는 `max_tokens`, `temperature`, `metadata["session_id"]`를 보낸다.
        DEBUG 로그에는 프롬프트 해시와 문자 수만 남긴다."""
        payload = self.payload(messages, model, max_tokens=max_tokens, temperature=temperature, metadata=metadata)
        if log.isEnabledFor(logging.DEBUG):
            log.debug("llm request role=%s model=%s prompt_sha256=%s chars=%d", role or "-", payload["model"],
                      messages_hash(messages)[:16], sum(len(m.content) for m in messages))
        with _in_flight():                      # 게이트웨이가 일하는 동안 수정 루프의 watchdog가 경고하지 않는다
            return self._complete(messages, payload, role)

    def _is_busy(self, resp: Any) -> bool:
        """게이트웨이가 바쁘다고 답했는지 확인한다. 429이거나 Retry-After가 붙은 502 / 503 / 504면 바쁨이다."""
        if resp.status_code == 429:
            return True
        return resp.status_code in BUSY_WITH_RETRY_AFTER and _retry_after_seconds(resp) is not None

    def _busy_delay(self, n: int, retry_after: Optional[float]) -> float:
        """n번째 바쁨 응답 뒤에 쉴 시간(초)을 계산한다.
        Retry-After가 있으면 그 값에 0~10%를 더해 기다린다. 없으면 상한이 있는 지수 백오프 값의 절반에서 전부 사이로 기다린다.
        여러 파드가 같은 시점에 다시 몰리지 않게 한다."""
        if retry_after is not None:
            return max(BUSY_MIN_DELAY, min(retry_after, BUSY_RETRY_AFTER_CAP)) * self._rand(1.0, 1.1)
        base = min(BUSY_MAX_DELAY, BUSY_FIRST_DELAY * 2.0 ** min(n, 16))
        return self._rand(base / 2.0, base)

    def _complete(self, messages: list[ChatMessage], payload: dict[str, Any], role: Optional[str]) -> LLMResponse:
        """게이트웨이에 요청을 보내고 실패하면 다시 시도한다.
        전송 오류와 `RETRY_STATUS`의 상태 코드는 다시 시도한다. 그 밖의 HTTP 오류는 바로 `LLMError`를 던진다.
        - 바쁨 응답은 `busy_wait_s`초 동안 `max_retries`와 별도로 재시도한다. 0이면 이 대기를 하지 않는다
        - 401이면 토큰을 새로 받아 한 번 더 보낸다. 그래도 401이면 1~5초 쉬고 `auth_retries`번까지 더 시도한다
        - 포기할 때 예외 메시지에 전송 장애면 `cause=`를, HTTP 오류면 응답 본문 앞부분을 넣는다"""
        t0 = time.monotonic()
        last_status: Optional[int] = None
        last_body = ""
        label = self._label(payload, role)
        chars = sum(len(m.content) for m in messages)
        cause: Optional[str] = None             # 마지막 전송 장애의 `<Class>: <message>`. HTTP 응답을 받으면 None
        token_error: Optional[TokenExchangeError] = None   # 마지막 시도가 토큰 교환에서 실패했다
        auth_retried = False                    # 401 뒤 새 토큰으로 한 번 더 보냈는지
        auth_rounds = 0                         # 새로 교환한 토큰도 401을 받은 뒤 더 시도한 횟수
        total = self.max_retries + 1            # 401 재시도 때만 하나씩 늘어난다
        busy_tries = 0                          # 이 호출의 바쁨 응답 수. `max_retries`에 세지 않는다
        busy_waited = 0.0                       # 바쁨으로 쉰 시간(초). 한도는 `llm.busy_wait_seconds`다
        busy_logged = 0.0                       # 마지막 "busy" 로그를 남긴 시점의 `busy_waited`
        posts = 0
        quiet_start = False                     # 다음 POST는 바쁨 응답 뒤에 다시 보내는 요청이다
        attempt = -1
        while attempt + 1 < total:
            attempt += 1
            wait: Optional[float] = None
            posts += 1
            # 바쁨 재시도의 시작 줄은 DEBUG로 남긴다. 운영자에게는 1분에 한 번 "still busy"만 보인다
            log.log(logging.DEBUG if quiet_start else logging.INFO,
                    "llm call provider=company %s attempt %d/%d → POST %s (prompt %d chars, timeout %.0fs)", label,
                    attempt + 1, self.max_retries + 1, self.url, chars, self.timeout)
            quiet_start = False
            sent: dict[str, str] = {}
            try:
                resp = self._post_with_heartbeat(payload, label, sent)
            except TokenExchangeError as exc:
                # 토큰 교환 실패는 게이트웨이 장애다. 채팅 호출의 전송 장애처럼 다시 시도한다
                token_error, cause = exc, None
                last_status, last_body = exc.status, ""
                log.warning("llm company %s (attempt %d/%d)", exc, attempt + 1, self.max_retries + 1)
            except (requests.RequestException, LLMHardDeadline) as exc:
                token_error = None
                cause = transport_cause(exc)
                last_status, last_body = None, f"{type(exc).__name__}: {exc}"
                log.warning("llm company transport error (attempt %d/%d): %s cause=%s", attempt + 1,
                            self.max_retries + 1, type(exc).__name__, cause)
            else:
                cause, token_error = None, None
                if resp.status_code < 300:
                    try:
                        data = resp.json()
                    except ValueError as exc:
                        raise LLMError(resp.status_code, resp.text[:500], f"invalid JSON from LLM: {exc}") from exc
                    out = self._parse(data, self.asked_name(payload))
                    if busy_tries:
                        log.info("llm gateway answered after waiting %.0fs on busy (%d busy answer(s)) - %s",
                                 busy_waited, busy_tries, label)
                    log.info("llm call provider=company model=%s in=%d out=%d latency=%.1fs finish=%s%s", out.model,
                             out.input_tokens, out.output_tokens, time.monotonic() - t0, out.finish_reason,
                             served_log_suffix(out))
                    return out
                last_status, last_body = resp.status_code, resp.text[:500]
                if resp.status_code == 401 and self._tokens is not None and not auth_retried:
                    auth_retried = True
                    total += 1
                    # 우리가 보낸 토큰일 때만 버린다. 다른 호출이 이미 새 토큰을 받았으면 다시 교환하지 않는다
                    self._tokens.invalidate(sent.get("token"))
                    log.warning("llm company HTTP 401: the access token is refreshed and the call retried once")
                    continue
                if resp.status_code == 401 and self._tokens is not None and auth_rounds < self.auth_retries:
                    # 방금 교환한 토큰도 거절됐다. 다른 파드가 같은 앱 토큰으로 교환해서 무효가 됐을 수 있다
                    # 잠시 쉰 뒤 다시 받는다
                    auth_rounds += 1
                    total += 1
                    _warn_token_race_once()
                    pause = self._rand(AUTH_PAUSE_MIN, AUTH_PAUSE_MAX)
                    log.warning("llm company HTTP 401 again after a fresh token: exchanging once more in %.0fs "
                                "(round %d/%d, llm.auth_retries)", pause, auth_rounds, self.auth_retries)
                    self._sleep(pause)
                    self._tokens.invalidate(sent.get("token"))
                    continue
                if self.busy_wait_s > 0 and self._is_busy(resp):
                    remaining = self.busy_wait_s - busy_waited
                    if remaining > 0:
                        delay = min(self._busy_delay(busy_tries, _retry_after_seconds(resp)), remaining)
                        if busy_tries == 0:
                            log.info("llm gateway busy (HTTP %d): waiting for a free slot, up to %.0fs "
                                     "(llm.busy_wait_seconds) - %s", resp.status_code, self.busy_wait_s, label)
                            busy_logged = 0.0
                        elif busy_waited - busy_logged >= BUSY_LOG_EVERY:
                            log.info("llm gateway still busy: waited %.0fs of %.0fs, last HTTP %d (%d busy answer(s)) - %s",
                                     busy_waited, self.busy_wait_s, resp.status_code, busy_tries, label)
                            busy_logged = busy_waited
                        busy_tries += 1
                        self._sleep(delay)
                        busy_waited += delay
                        attempt -= 1            # 바쁨 응답은 `max_retries` 횟수에 넣지 않는다
                        quiet_start = True
                        continue
                    log.error("llm company giving up: gateway busy for %.0fs (llm.busy_wait_seconds=%.0f, %d busy "
                              "answer(s)) status=%s body=%s", busy_waited, self.busy_wait_s, busy_tries, last_status,
                              ascii_safe(last_body, 300))
                    raise LLMError(last_status, last_body,
                                   f"LLM request failed after {posts} attempt(s) (status={last_status}): {last_body[:200]}")
                if resp.status_code not in RETRY_STATUS:
                    log.error("llm company HTTP %d: %s", resp.status_code, last_body[:300].replace("\n", " "))
                    raise LLMError(resp.status_code, last_body)
                wait = _retry_after_seconds(resp)
                log.warning("llm company HTTP %d (attempt %d/%d)", resp.status_code, attempt + 1,
                            self.max_retries + 1)
            if attempt + 1 < total:
                # Retry-After가 있으면 그대로 따른다. 없으면 지수 대기에 0~25% 무작위 지연을 더해 파드끼리 재시도 시점을 흩는다
                delay = min(wait, 120.0) if wait is not None else min(30.0, 2.0 ** attempt) * self._rand(1.0, 1.25)
                log.info("llm call retrying in %.0fs (attempt %d/%d failed: status=%s%s)", delay, attempt + 1,
                         self.max_retries + 1, last_status, f" cause={cause}" if cause else
                         (" in the token exchange" if token_error is not None else ""))
                self._sleep(delay)
        if token_error is not None:
            log.error("llm company giving up after %d attempt(s): %s", self.max_retries + 1, token_error)
            raise LLMError(last_status, "", f"LLM request failed after {self.max_retries + 1} attempt(s): {token_error}")
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

    @staticmethod
    def _session_of(data: Any) -> Optional[str]:
        """응답에서 세션 id를 찾는다. 최상위 ``session_id``를 먼저 보고, 없으면 ``data`` / ``result`` 아래를 본다.
        끝내 없으면 None을 반환한다."""
        if not isinstance(data, dict):
            return None
        for holder in (data, data.get("data"), data.get("result")):
            if isinstance(holder, dict) and holder.get("session_id"):
                return str(holder["session_id"])
        return None

    def _parse(self, data: Any, model: str) -> LLMResponse:
        """게이트웨이 JSON 응답을 LLMResponse로 바꾼다.
        콘텐츠 필터에 걸린 답과, finish_reason이 length인데 비어 있는 답은 그 요청만 거절된 것으로 본다.
        이때는 `LLMError`(status 200)를 던진다. 내용이 있는 잘린 답은 경고만 남긴다.

        The fix loop's request-rejected rule keeps these status-200 errors out of the
        "LLM unavailable" streak."""
        filtered = content_filter_reason(data)
        if filtered:
            # Azure 형식 게이트웨이는 콘텐츠 필터에 걸리면 HTTP 200에 빈 content를 준다. 장애가 아니라 이 요청의 문제다
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
            # 추론 모델이 답을 쓰기 전에 max_tokens를 다 썼다. 요청 크기 문제라서 요청 거절로 센다
            log.warning("llm company: empty answer, finish_reason=length (output tokens %d)", out_tokens)
            raise LLMError(200, json.dumps(data, ensure_ascii=False)[:500],
                           EMPTY_LENGTH_MESSAGE + (f" (output tokens {out_tokens})" if out_tokens else ""))
        if str(finish or "").lower() == "length":
            log.warning("llm company: answer cut at the gateway's max_tokens (finish_reason=length, output tokens %d)",
                        out_tokens)
        # 모델 이름은 요청한 이름(agent는 role)을 쓴다. 응답의 model과 usage.by_model[].model은 served_models에 둔다
        out = LLMResponse(content=content, model=model or "",
                          input_tokens=int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
                          output_tokens=out_tokens, finish_reason=finish)
        out = named_response(out, model, fallback=self.model, served=served_models_of(data))
        out.session_id = self._session_of(data)
        if out.session_id and out.session_id != self.last_session_id:
            self.last_session_id = out.session_id
            log.info("llm gateway session_id=%s (returned by the gateway; sbmig sends none)", out.session_id)
        return out

    def close(self) -> None:
        return None


def content_filter_reason(data: Any) -> Optional[str]:
    """OpenAI / Azure 형식 응답이 콘텐츠 필터 때문에 비었으면 그 이유를 반환한다. 아니면 None이다.
    HTTP 200이어도 걸러진 답일 수 있다.

    Hits: `choices[0].finish_reason == "content_filter"`, a `content_filter_result` entry with
    `filtered: true` next to an empty message, or a `prompt_filter_results[*].content_filter_results`
    entry with `filtered: true`."""
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
# 연결 확인용 작은 호출: `sbmig llm-probe`와 fleet preflight
# ---------------------------------------------------------------------------
PROBE_PROMPT = "Reply with the single word: pong"
PROBE_SKIP_PROVIDERS = ("fake",)          # 연결할 곳이 없다. preflight가 호출을 건너뛴다


def probe_messages(prompt: str = PROBE_PROMPT, no_system: bool = False) -> list[dict]:
    """probe 요청에 보낼 메시지를 만든다.
    system 한 줄과 "pong"을 요청하는 user 메시지다. `no_system`이면 user 메시지 하나만 보낸다."""
    msgs = [] if no_system else [{"role": "system", "content": "You are a helpful assistant."}]
    return msgs + [{"role": "user", "content": prompt}]


@dataclass
class ProbeResult:
    """probe 호출 하나의 결과를 담는 클래스.
    실패 종류 `kind`는 config, token, transport, http, reply, rejected 중 하나다."""
    ok: bool
    provider: str = ""
    model: str = ""
    url: str = ""
    kind: str = ""
    status: Optional[int] = None
    problem: str = ""                     # ASCII 한 줄: `cause=…`, `HTTP 503: …`, LLMError 문구
    hints: list[str] = field(default_factory=list)
    token: str = ""                           # 토큰 교환 결과, 예: "ok (expires_in …)"
    reply: str = ""
    latency_s: float = 0.0
    payload: Optional[dict] = None        # company provider가 실제로 보낸 JSON 본문
    response: Any = None                  # company provider의 requests.Response, CLI 상세 출력에 쓴다
    data: Any = None                      # 파싱한 JSON 응답, 있을 때만
    client_name: str = ""
    served: list[str] = field(default_factory=list)   # 응답이 알려 준 실제 모델 이름

    @property
    def transient(self) -> bool:
        """시간이 지나면 저절로 나아질 수 있는 실패인지 확인한다. 전송 장애와 5xx / 408 / 429가 그렇다."""
        if self.ok:
            return False
        if self.kind == "transport":
            return True
        return self.kind == "http" and self.status is not None and (self.status >= 500 or self.status in (408, 429))

    def line(self) -> str:
        """운영자가 손으로 다시 입력할 짧은 ASCII 한 줄을 만든다. `LLM-PROBE ok …` 또는 `LLM-PROBE FAIL <kind> …`이다."""
        head = f"provider={self.provider} model={ascii_safe(self.model, 60)}"
        if self.ok:
            return (f"LLM-PROBE ok {head} status={self.status if self.status is not None else '-'} "
                    f"latency={self.latency_s:.1f}s reply={ascii_safe(self.reply, 40)!r}"
                    + (f" served={ascii_safe(','.join(self.served), 80)}" if self.served else ""))
        return (f"LLM-PROBE FAIL {self.kind} {head} status={self.status if self.status is not None else '-'} "
                f"{ascii_safe(self.problem, 200)}")


def probe_llm(cfg: Any, prompt: str = PROBE_PROMPT, *, no_system: bool = False,
              on_request: Optional[Callable[[dict], None]] = None) -> ProbeResult:
    """설정된 provider로 작은 요청 하나를 재시도 없이 보내 연결을 확인한다.
    company는 토큰 교환부터 확인한다. 그다음 실제 실행과 같은 본문으로 `post_once`를 한 번 호출한다.
    다른 provider는 `client.complete(…, role="other")`를 호출한다.
    LLM 문제로는 예외를 던지지 않는다. 결과는 ProbeResult로 반환한다."""
    from sbmig.core.interfaces import ChatMessage

    from .client import LLMConfigError, make_llm_client

    llm = cfg.llm
    res = ProbeResult(ok=False, provider=str(llm.provider), model=str(llm.model), url=str(llm.base_url or ""))
    msgs = [ChatMessage(role=m["role"], content=m["content"]) for m in probe_messages(prompt, no_system)]
    try:
        client = make_llm_client(cfg)
    except (LLMConfigError, ValueError) as e:          # 키 / URL / 모델이 없거나 플러그인을 불러오지 못했다
        res.kind, res.problem = "config", ascii_safe(e, 200)
        res.hints.append("put LLM_BASE_URL / LLM_API_KEY / LLM_MODEL in .env at the repo root (copy .env.example) "
                         "or export them in the shell; `--llm-provider fake` runs without a gateway")
        return res
    inner = getattr(client, "inner", client)
    res.client_name = type(inner).__name__
    t0 = time.monotonic()
    if isinstance(inner, CompanyClient):
        res.url, res.model = inner.url, inner.model
        if inner._tokens is not None:                     # 토큰 교환을 따로 확인한다
            try:
                inner._tokens.refresh()
                res.token = f"ok (expires_in {inner._tokens.expires_in:.0f}s, refreshed {inner._tokens.margin_s:.0f}s before expiry)"
            except Exception as e:  # noqa: BLE001 - probe는 결과로 알리고 예외를 던지지 않는다
                res.latency_s = time.monotonic() - t0
                res.kind, res.problem = "token", f"token exchange: {ascii_safe(e, 200)}"
                res.hints.append("check LLM_TOKEN_URL and the app token (LLM_APP_TOKEN, else LLM_API_KEY): POST <token_url> with "
                                 "`Authorization: Bearer <app token>` must answer {access_token, token_type, expires_in}")
                return res
        payload = res.payload = inner.payload(msgs, metadata={"run_id": "probe"})
        if on_request is not None:
            on_request(payload)
        try:
            resp = inner.post_once(payload)
        except Exception as e:  # noqa: BLE001 - 전송 장애다. cause를 보면 프록시, DNS, 연결 거부, TLS 중 무엇인지 안다
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
        res.ok, res.reply, res.served = True, out.content, list(out.served_models)
        return res
    try:
        out = client.complete(msgs, role="other")
    except LLMError as e:
        res.latency_s = time.monotonic() - t0
        res.status = e.status
        res.kind = "http" if e.status is not None and e.status != 200 else ("reply" if e.status == 200 else "transport")
        res.problem = ascii_safe(e, 200)
        return res
    except Exception as e:  # noqa: BLE001 - 플러그인이나 SDK 클라이언트는 자기 전송 예외를 던진다
        res.latency_s = time.monotonic() - t0
        res.kind, res.problem = "transport", f"cause={transport_cause(e)}"
        return res
    res.latency_s = time.monotonic() - t0
    res.ok, res.reply = True, getattr(out, "content", "") or ""
    res.model = getattr(out, "model", None) or res.model
    res.served = list(getattr(out, "served_models", None) or [])
    return res
