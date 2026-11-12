"""회사 LLM 게이트웨이를 호출하는 클라이언트 모듈.
기본 provider `company`이며 요청마다 ``requests.post(url, headers=, json=, timeout=)``를 한 번 호출한다.
`make_llm_client(cfg)`가 클라이언트를 만들고, 수정 루프, 구조 검토, 운영자 가이드의 LLM 호출이 모두 이
클라이언트를 거친다. `sbmig llm-probe`와 fleet preflight는 `probe_llm`을 쓴다.
- 설정은 환경 변수와 `.env`에서만 읽는다(LLM_BASE_URL, LLM_API_KEY, LLM_MODEL, 선택 LLM_RESPONSE_PATH)
- 본문은 model, messages에 `llm.payload_extra`를 더한 것이고 max_tokens / temperature / stop은 보내지 않는다
- 다른 사내 에이전트와 같은 단순한 호출 형태를 유지한다.
  프로필, LangChain, 세션, 커넥션 풀 설정을 넣지 않는다
- 키는 헤더에만 넣고 repr, 로그, 기록에 남기지 않는다
- 실패 메시지는 운영자가 손으로 옮겨 적고 수정 루프가 분류한다.
  전송 장애는 `cause=<예외 클래스>: <ASCII 메시지>` 형식이다
- 재시도는 전송 오류와 RETRY_STATUS에만 `max_retries`번 하고, 호출 하나에 걸리는 전체 시간은 기본
  `timeout + 30초`로 제한한다

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

from .client import RETRY_STATUS, LLMError, _retry_after_seconds, extract_reply, messages_hash

try:                                            # round 10 (sbmig.fix.client): LLM calls in flight, for the watchdog
    from .client import llm_call_in_flight as _in_flight
except ImportError:  # pragma: no cover - older client module
    from contextlib import nullcontext as _in_flight

log = logging.getLogger("sbmig.fix.client")

PostFn = Callable[..., Any]   # requests.post-compatible: post(url, headers=, json=, timeout=) -> Response


class LLMHardDeadline(requests.RequestException):
    """호출 하나가 `hard_deadline_s`를 넘겼을 때 던지는 예외. 전송 장애로 처리한다.

    One call outlived `hard_deadline_s` (the gateway never closed the connection); a transport failure."""


CAUSE_CHARS = 120
_NON_ASCII_RUN_RE = re.compile(r"[^\x00-\x7f]+(?:\s+[^\x00-\x7f]+)*")
# "<urllib3.connection.HTTPConnection object at 0x…>: " (urllib3 1.x) / "HTTPConnection(host='gw', port=80): " (2.x) /
# "HTTPConnectionPool(host='gw', port=80): " — noise in front of the part that says what happened
_CONN_PREFIX_RE = re.compile(r"^(?:<[^<>]* object at 0x[0-9A-Fa-f]+>|\w*Connection(?:Pool)?\(host='[^']*', "
                             r"port=\d+\)|None):\s*")
_OS_ERROR_RE = re.compile(r"\[(?:WinError|Errno) -?\d+\][^'\"()]*")


def ascii_safe(text: Any, limit: int = CAUSE_CHARS) -> str:
    """텍스트를 운영자가 옮겨 적을 수 있는 ASCII 한 줄로 바꾼다.
    ASCII가 아닌 문자는 `?`로 바꾸고 `limit`자에서 자른다.
    회사 Windows PC에서는 복사와 붙여넣기가 안 되어 콘솔 줄을 손으로 다시 입력하기 때문이다.

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
    """전송 장애를 `<예외 클래스>: <짧은 메시지>` 형식으로 만든다.
    클래스는 바깥 예외의 것을, 메시지는 requests / urllib3가 감싼 가장 안쪽 원인을 쓴다.
    잘리더라도 WinError / Errno 번호는 남긴다.

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
    """응답 JSON에서 finish_reason을 찾는다. choices[0], 최상위 finish_reason, stop_reason 순서로 확인한다."""
    if not isinstance(data, dict):
        return None
    finish = None
    choices = data.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        finish = choices[0].get("finish_reason")
    return finish or data.get("finish_reason") or data.get("stop_reason")


# 여러 빌드 파드가 같은 게이트웨이를 호출하므로 바쁘다는 응답은 장애로 보지 않고 기다린다
BUSY_WITH_RETRY_AFTER = frozenset({502, 503, 504})   # Retry-After가 있을 때만 바쁨으로 보고, 없으면 일반 5xx로 재시도
BUSY_FIRST_DELAY = 2.0          # Retry-After 없는 바쁨의 첫 대기 시간, 두 배씩 늘리며 무작위 지연을 더함
BUSY_MAX_DELAY = 60.0           # 바쁨 대기 시간의 상한
BUSY_RETRY_AFTER_CAP = 120.0    # Retry-After를 따르되 한 번에 이보다 오래 쉬지 않음
BUSY_MIN_DELAY = 1.0            # 빈 루프를 막기 위해 Retry-After가 0이어도 지키는 최소 대기
BUSY_LOG_EVERY = 60.0           # "still busy" 로그를 남기는 간격(대기 시간 기준)
AUTH_PAUSE_MIN, AUTH_PAUSE_MAX = 1.0, 5.0   # 토큰 교환 직후 401이 오면 다시 교환하기 전에 쉬는 무작위 시간(초)

TOKEN_RACE_WARNING = ("LLM gateway rejected a token right after it was exchanged \u2014 another client may be exchanging "
                      "the same app token (parallel build pods?)")
_TOKEN_RACE_WARNED = False
_TOKEN_RACE_LOCK = threading.Lock()


def _warn_token_race_once() -> None:
    """토큰 교환 직후 401이 왔다는 경고를 프로세스에서 한 번만 남긴다.

    Log the "rejected right after the exchange" WARNING once per process."""
    global _TOKEN_RACE_WARNED
    with _TOKEN_RACE_LOCK:
        if _TOKEN_RACE_WARNED:
            return
        _TOKEN_RACE_WARNED = True
    log.warning(TOKEN_RACE_WARNING)


class TokenManager:
    """사내 GPT 에이전트 게이트웨이의 토큰을 받아 관리하는 클래스.
    ``POST <token_url>``에 앱 토큰을 Bearer로 보내 access token을 받는다.
    토큰은 메모리에 두고, 만료 ``margin_s`` 전이거나 401이 오면 다시 받는다.
    토큰과 앱 토큰은 로그, repr, 기록에 남기지 않는다.

    The agent gateway's token exchange: `POST token_url` with the app token as Bearer → `access_token` + `expires_in`,
    cached in memory and refreshed `margin_s` before expiry (or after a 401). Neither token ever reaches a log."""

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
        self.expires_in = 0.0                   # the last answer's TTL (seconds), for the probe
        self.refreshes = 0

    def __repr__(self) -> str:                  # never leak a token
        return f"TokenManager(url={self.token_url!r}, refreshes={self.refreshes})"

    def valid(self) -> bool:
        """토큰이 있고 만료까지 ``margin_s``초보다 많이 남았는지 확인한다. / A token is held and more than `margin_s` from expiry."""
        return bool(self._token) and self._clock() < self._expires_at - self.margin_s

    def token(self) -> str:
        """요청에 쓸 토큰을 반환한다. 토큰이 없거나 곧 만료되면 먼저 새로 받아 온다. / The token to send, refreshed first when (nearly) expired."""
        with self._lock:
            if not self.valid():
                self._refresh_locked()
            return self._token

    def refresh(self) -> str:
        """토큰을 무조건 새로 받는다. / Fetch a fresh token unconditionally (the probe, after a 401)."""
        with self._lock:
            self._refresh_locked()
            return self._token

    def invalidate(self, rejected: Optional[str] = None) -> None:
        """다음 ``token()`` 호출이 토큰을 새로 받게 한다.
        ``rejected``를 주면 지금 토큰이 그 거절된 토큰일 때만 버린다.
        다른 호출이 이미 새로 받은 토큰을 버리고 또 교환하면 서로의 토큰을 무효로 만들 수 있기 때문이다.

        Make the next `token()` fetch again. With `rejected`, only when the held token IS that rejected token: a newer
        token another call of this process already fetched is kept (no second exchange that would kill it)."""
        with self._lock:
            if rejected is not None and self._token and self._token != rejected:
                return
            self._token, self._expires_at = "", 0.0

    def _refresh_locked(self) -> None:
        resp = self._post(self.token_url, headers={"Authorization": f"Bearer {self._app_token}"}, timeout=self.timeout)
        if resp.status_code >= 300:
            log.error("llm token exchange HTTP %d: %s", resp.status_code, ascii_safe(resp.text, 200))
            raise LLMError(resp.status_code, resp.text[:300], f"token exchange failed: HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise LLMError(resp.status_code, resp.text[:300], "token exchange: the answer is not JSON") from exc
        tok = data.get("access_token") if isinstance(data, dict) else None
        if not tok:
            raise LLMError(resp.status_code, "", "token exchange: no access_token in the answer")
        try:
            ttl = float(data.get("expires_in") or 3600)
        except (TypeError, ValueError):
            ttl = 3600.0
        self._token, self._expires_at, self.expires_in = str(tok), self._clock() + ttl, ttl
        note_secret(self._token)     # 환경 변수에 없는 비밀 값이므로 출력 마스킹 대상에 등록
        self.refreshes += 1
        log.info("llm token refreshed: expires in %.0fs (refreshed again %.0fs before expiry)", ttl, self.margin_s)


class CompanyClient:
    """회사 LLM 게이트웨이를 호출하는 클라이언트(LLMClientLike).
    요청 하나는 `requests.post` 한 번이고, 실패하면 재시도한 뒤 LLMError를 던진다."""
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
        # 여러 빌드 파드가 같은 앱 토큰을 쓰므로 바쁨은 이 시간까지 기다리고, 교환 직후 401이면
        # auth_retries번 더 교환한다
        # rand는 무작위 지연 함수이며 테스트에서 고정값을 넣는다
        self.busy_wait_s = max(0.0, float(busy_wait_s or 0))
        self.auth_retries = max(0, int(auth_retries or 0))
        self._rand = rand
        # v2.1z-m: legacy = {model, messages, **payload_extra}; agent = the in-house GPT agent gateway (asset id as
        # `model`, the chosen model name as `params.role`, Bearer = the exchanged access token)
        self.gateway = (gateway or "legacy").strip().lower()
        self.asset_id = (asset_id or "").strip() or None
        self._tokens = token_manager
        self.agent_extra = dict(agent_extra or {})
        self.last_session_id: Optional[str] = None   # v2.1z-n: the gateway's session id seen last (never sent back yet)
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
        # requests의 timeout은 바이트 사이의 대기만 재므로, 연결을 잡고 조금씩 보내는 게이트웨이 때문에
        # 실행이 몇 시간씩 멈출 수 있다
        # 그래서 전체 시간 상한을 넘으면 호출을 버리고 전송 장애로 처리한다
        # wall-clock cap on one call: `requests` timeout only counts silence between bytes, so a gateway that keeps the
        # connection open (keep-alive / trickled chunks) can hang a run for hours; past the deadline the call is
        # abandoned (its thread is left to die) and reported as a transport failure (retry → cool-down → unavailable)
        self.hard_deadline_s = float(hard_deadline_s) if hard_deadline_s is not None else self.timeout + 30.0

    def __repr__(self) -> str:                        # never leak the key
        return f"CompanyClient(url={self.url!r}, model={self.model!r}, gateway={self.gateway!r})"

    # ---- the two things that go over the wire
    def headers(self) -> dict[str, str]:
        """요청에 보낼 헤더(`Authorization: Bearer <키>`와 `llm.extra_headers`)를 만든다.
        키가 들어 있으므로 로그, 기록, 화면에 내보내지 않는다."""
        return self._auth_headers()[0]

    def _auth_headers(self) -> tuple[dict[str, str], str]:
        key = self._tokens.token() if self._tokens is not None else self._api_key
        return {"Authorization": f"Bearer {key}", **self.extra_headers}, key

    def payload(self, messages: list[ChatMessage], model: Optional[str] = None, *, max_tokens: Optional[int] = None,
                temperature: Optional[float] = None, metadata: Optional[dict] = None) -> dict[str, Any]:
        """요청에 보낼 JSON 본문을 만든다.
        - legacy: model, messages, payload_extra를 게이트웨이가 정한 형식 그대로 보낸다
        - agent: asset id를 `model`로, 고른 모델 이름을 `params.role`로 보내고 호출마다 새 trace_id를 넣는다
        - session_id는 세션을 이어 가는 호출만 ``metadata["session_id"]``로 보낸다.
          열린 세션은 모델을 바꿀 수 없으므로 모델이 바뀌면 새 세션을 연다

        legacy: the company contract verbatim; agent: the GPT agent gateway's shape (asset id as `model`, the chosen
        model name as `params.role`, per-call ids, the generation parameters it accepts; no `thread_id` yet)."""
        msgs = [{"role": m.role, "content": m.content} for m in messages]
        if self.gateway != "agent":
            return {"model": model or self.model, "messages": msgs, **self.payload_extra}
        body: dict[str, Any] = {"model": self.asset_id or (model or self.model), "messages": msgs, "stream": False,
                                "async": False, "params": {"role": model or self.model}}
        # v2.1z-n/o (user): no session_id → the gateway opens a session and returns its id; a caller that wants to CONTINUE
        # that conversation passes it back in metadata["session_id"] (the case thread); thread_id is deprecated — never sent
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

    def _label(self, payload: dict[str, Any], role: Optional[str]) -> str:
        """로그에 쓸 라벨을 만든다. / The log label; agent mode adds `params.role`."""
        extra = f" params.role={payload.get('params', {}).get('role')}" if self.gateway == "agent" else ""
        return f"role={role or '-'} model={payload['model']}{extra}"

    def post_once(self, payload: dict[str, Any], _sent: Optional[dict] = None) -> Any:
        """주어진 본문 그대로 ``requests.post``를 한 번 호출한다. probe와 재시도 루프가 함께 쓴다.
        401 뒤에 그 토큰만 버리기 위해 ``_sent``에 보낸 토큰을 남긴다. 로그에는 쓰지 않는다.

        One ``requests.post`` with the exact call shape; returns the ``requests.Response``."""
        headers, key = self._auth_headers()
        if _sent is not None:
            _sent["token"] = key
        return self._post(self.url, headers=headers, json=payload, timeout=self.timeout)

    def _post_with_heartbeat(self, payload: dict[str, Any], label: str, sent: Optional[dict] = None) -> Any:
        """post_once를 작업 스레드에서 실행하고, 답이 없는 동안 `heartbeat_s`마다 INFO 로그를 한 줄 남긴다.
        `hard_deadline_s`가 지나면 스레드를 버리고 LLMHardDeadline을 던진다.

        post_once + an INFO line every `heartbeat_s` while the gateway has not answered (operators can tell a
        slow answer from a hung run; the lines reach <run dir>/sbmig.log and the web UI log panel)."""
        done = threading.Event()
        t0 = time.monotonic()

        def beat() -> None:
            while not done.wait(self.heartbeat_s):
                log.info("llm call still in flight: %.0fs elapsed (timeout %.0fs) — %s", time.monotonic() - t0,
                         self.timeout, label)

        # 웹 앱이 contextvar로 실행별 로그를 나누므로 두 스레드 모두 호출한 쪽 context의 복사본에서 실행한다
        # 그렇지 않으면 heartbeat 줄이 sbmig.log에 남지 않는다
        # both helper threads run in a copy of the caller's context: the web app's per-run log filter attributes
        # records by the contextvar the run thread set, so without this the heartbeat lines never reach sbmig.log
        th = threading.Thread(target=contextvars.copy_context().run, args=(beat,), name="llm-heartbeat", daemon=True)
        if self.heartbeat_s > 0:
            th.start()
        result: dict[str, Any] = {}

        def call() -> None:
            try:
                result["resp"] = self.post_once(payload, sent)
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
        """메시지를 게이트웨이로 보내고 LLMResponse를 반환한다.
        `max_tokens` / `temperature` / `stop` / `role` / `metadata`는 인자로 받기만 하고 보내지 않는다.
        DEBUG 로그에는 프롬프트 해시와 문자 수만 남긴다."""
        payload = self.payload(messages, model, max_tokens=max_tokens, temperature=temperature, metadata=metadata)
        if log.isEnabledFor(logging.DEBUG):
            log.debug("llm request role=%s model=%s prompt_sha256=%s chars=%d", role or "-", payload["model"],
                      messages_hash(messages)[:16], sum(len(m.content) for m in messages))
        with _in_flight():                      # the fix loop's watchdog stays quiet while the gateway is working
            return self._complete(messages, payload, role)

    def _is_busy(self, resp: Any) -> bool:
        """게이트웨이가 바쁘다고 답했는지 확인한다. 429이거나 Retry-After가 붙은 502 / 503 / 504면 바쁨이다.

        The gateway said "busy": 429, or 502 / 503 / 504 carrying `Retry-After`."""
        if resp.status_code == 429:
            return True
        return resp.status_code in BUSY_WITH_RETRY_AFTER and _retry_after_seconds(resp) is not None

    def _busy_delay(self, n: int, retry_after: Optional[float]) -> float:
        """n번째 바쁨 응답 뒤에 쉴 시간(초)을 계산한다.
        Retry-After가 있으면 그 값에 0~10%를 더하고, 없으면 상한이 있는 지수 백오프에 무작위 지연을 더한다.
        여러 파드가 같은 시점에 다시 몰리지 않게 하기 위해서다.

        Seconds to wait after the n-th busy answer: Retry-After (capped) + 0-10 %, else capped exponential backoff
        with equal jitter (half to all of it), so parallel pods do not come back in lockstep."""
        if retry_after is not None:
            return max(BUSY_MIN_DELAY, min(retry_after, BUSY_RETRY_AFTER_CAP)) * self._rand(1.0, 1.1)
        base = min(BUSY_MAX_DELAY, BUSY_FIRST_DELAY * 2.0 ** min(n, 16))
        return self._rand(base / 2.0, base)

    def _complete(self, messages: list[ChatMessage], payload: dict[str, Any], role: Optional[str]) -> LLMResponse:
        """게이트웨이에 요청을 보내고 실패하면 다시 시도한다.
        전송 오류와 RETRY_STATUS는 재시도하고 그 밖의 HTTP 오류는 바로 LLMError를 던진다.
        - 바쁨 응답은 `busy_wait_s`초 동안 max_retries와 별도로 재시도한다. 0이면 재시도하지 않는다
        - 401이면 토큰을 새로 받아 한 번 더 보내고, 그래도 401이면 1~5초 쉬고 `auth_retries`번까지 더 시도한다
        - 포기할 때 예외 메시지에 전송 장애면 `cause=`를, HTTP 오류면 응답 본문 앞부분을 넣는다"""
        t0 = time.monotonic()
        last_status: Optional[int] = None
        last_body = ""
        label = self._label(payload, role)
        chars = sum(len(m.content) for m in messages)
        cause: Optional[str] = None             # `<Class>: <message>` of the last transport failure (None after an HTTP answer)
        auth_retried = False                    # v2.1z-m: one extra attempt after a 401 with a fresh token
        auth_rounds = 0                         # 2026-11-10: further rounds after a 401 that followed a fresh exchange
        total = self.max_retries + 1            # grows by one only when a 401 round happens (other failures keep max_retries)
        busy_tries = 0                          # 2026-11-10: busy answers of this call (they do not use max_retries)
        busy_waited = 0.0                       # seconds slept on busy answers (the llm.busy_wait_seconds budget)
        busy_logged = 0.0                       # busy_waited when the last "busy" line was written
        posts = 0
        quiet_start = False                     # the next POST re-sends after a busy answer
        attempt = -1
        while attempt + 1 < total:
            attempt += 1
            wait: Optional[float] = None
            posts += 1
            # 운영자에게는 1분에 한 번 "still busy"만 보이도록 바쁨 재시도의 시작 줄은 DEBUG로 남긴다
            log.log(logging.DEBUG if quiet_start else logging.INFO,
                    "llm call provider=company %s attempt %d/%d → POST %s (prompt %d chars, timeout %.0fs)", label,
                    attempt + 1, self.max_retries + 1, self.url, chars, self.timeout)
            quiet_start = False
            sent: dict[str, str] = {}
            try:
                resp = self._post_with_heartbeat(payload, label, sent)
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
                    if busy_tries:
                        log.info("llm gateway answered after waiting %.0fs on busy (%d busy answer(s)) - %s",
                                 busy_waited, busy_tries, label)
                    log.info("llm call provider=company model=%s in=%d out=%d latency=%.1fs finish=%s", out.model,
                             out.input_tokens, out.output_tokens, time.monotonic() - t0, out.finish_reason)
                    return out
                last_status, last_body = resp.status_code, resp.text[:500]
                if resp.status_code == 401 and self._tokens is not None and not auth_retried:
                    auth_retried = True
                    total += 1
                    # 다른 호출이 이미 새 토큰을 받았으면 다시 교환하지 않도록, 우리가 보낸 토큰일 때만 버린다
                    self._tokens.invalidate(sent.get("token"))
                    log.warning("llm company HTTP 401: the access token is refreshed and the call retried once")
                    continue
                if resp.status_code == 401 and self._tokens is not None and auth_rounds < self.auth_retries:
                    # 방금 교환한 토큰도 거절됐으면 다른 파드가 같은 앱 토큰으로 교환해 무효가 됐을 수
                    # 있으므로 잠시 쉰 뒤 다시 받는다
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
                        attempt -= 1            # 바쁨 응답은 max_retries 횟수에 넣지 않음
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
                # Retry-After는 그대로 따르고, 지수 대기에는 0~25% 무작위 지연을
                # 더해 여러 파드가 동시에 재시도하지 않게 한다
                delay = min(wait, 120.0) if wait is not None else min(30.0, 2.0 ** attempt) * self._rand(1.0, 1.25)
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

    @staticmethod
    def _session_of(data: Any) -> Optional[str]:
        """응답에서 세션 id를 찾는다. 최상위 ``session_id``를 먼저 보고 없으면 ``data`` / ``result``
        아래에서 찾으며, 끝내 없으면 None을 반환한다.

        The session id the agent gateway returns (top level, or one level under `data` / `result`); None otherwise."""
        if not isinstance(data, dict):
            return None
        for holder in (data, data.get("data"), data.get("result")):
            if isinstance(holder, dict) and holder.get("session_id"):
                return str(holder["session_id"])
        return None

    def _parse(self, data: Any, model: str) -> LLMResponse:
        """게이트웨이 JSON 응답을 LLMResponse로 바꾼다.
        콘텐츠 필터에 걸렸거나 답이 비었는데 finish_reason이 length면 그 요청만 거절된 것으로 보고
        LLMError를 던진다. 잘린 답은 경고만 남긴다."""
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
        out = LLMResponse(content=content, model=(data.get("model") if isinstance(data, dict) else None) or model,
                           input_tokens=int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
                           output_tokens=out_tokens, finish_reason=finish)
        out.session_id = self._session_of(data)
        if out.session_id and out.session_id != self.last_session_id:
            self.last_session_id = out.session_id
            log.info("llm gateway session_id=%s (returned by the gateway; sbmig sends none)", out.session_id)
        return out

    def close(self) -> None:
        return None


def content_filter_reason(data: Any) -> Optional[str]:
    """OpenAI / Azure 형식 응답이 콘텐츠 필터 때문에 비었으면 그 이유를, 아니면 None을 반환한다.
    HTTP 200이어도 걸러진 답일 수 있다.

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
    """probe 요청에 보낼 메시지를 만든다.
    system 한 줄과 "pong"을 요청하는 메시지이고, `--no-system`이면 user 메시지 하나만 보낸다."""
    msgs = [] if no_system else [{"role": "system", "content": "You are a helpful assistant."}]
    return msgs + [{"role": "user", "content": prompt}]


@dataclass
class ProbeResult:
    """probe 호출 하나의 결과를 담는 클래스.
    실패 종류 `kind`는 config | transport | http | reply | rejected 중 하나다.

    What one probe call showed. `kind` (when not ok): config | transport | http | reply | rejected."""
    ok: bool
    provider: str = ""
    model: str = ""
    url: str = ""
    kind: str = ""
    status: Optional[int] = None
    problem: str = ""                     # one ASCII line: `cause=…` / `HTTP 503: …` / the LLMError text
    hints: list[str] = field(default_factory=list)
    token: str = ""                           # v2.1z-m: the token exchange's outcome ("ok (expires_in …)")
    reply: str = ""
    latency_s: float = 0.0
    payload: Optional[dict] = None        # company provider: the exact JSON body that was sent
    response: Any = None                  # company provider: the requests.Response (verbose CLI output)
    data: Any = None                      # parsed JSON answer, when there was one
    client_name: str = ""

    @property
    def transient(self) -> bool:
        """설정 오류가 아니라 시간이 지나면 저절로 나아질 수 있는 실패인지 확인한다.

        A failure that may go away by itself (gateway down / overloaded, network) — not a configuration error."""
        if self.ok:
            return False
        if self.kind == "transport":
            return True
        return self.kind == "http" and self.status is not None and (self.status >= 500 or self.status in (408, 429))

    def line(self) -> str:
        """운영자가 옮겨 적을 짧은 ASCII 한 줄을 만든다.

        `LLM-PROBE ok …` / `LLM-PROBE FAIL <kind> …`: short, ASCII, retypeable."""
        head = f"provider={self.provider} model={ascii_safe(self.model, 60)}"
        if self.ok:
            return (f"LLM-PROBE ok {head} status={self.status if self.status is not None else '-'} "
                    f"latency={self.latency_s:.1f}s reply={ascii_safe(self.reply, 40)!r}")
        return (f"LLM-PROBE FAIL {self.kind} {head} status={self.status if self.status is not None else '-'} "
                f"{ascii_safe(self.problem, 200)}")


def probe_llm(cfg: Any, prompt: str = PROBE_PROMPT, *, no_system: bool = False,
              on_request: Optional[Callable[[dict], None]] = None) -> ProbeResult:
    """설정된 provider로 작은 요청 하나를 재시도 없이 보내 연결을 확인한다.
    company는 실제 실행과 같은 본문으로 `post_once`를 한 번 호출한다.
    LLM 문제로는 예외를 던지지 않고 결과를 ProbeResult로 반환한다.

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
        if inner._tokens is not None:                     # v2.1z-m: the token exchange is a step of its own
            try:
                inner._tokens.refresh()
                res.token = f"ok (expires_in {inner._tokens.expires_in:.0f}s, refreshed {inner._tokens.margin_s:.0f}s before expiry)"
            except Exception as e:  # noqa: BLE001 - the probe reports, never raises
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
