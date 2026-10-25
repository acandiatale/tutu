"""LLM clients: the company gateway (`sbmig.fix.company`, requests.post — the default), OpenAI-compatible,
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
NO_FIX_RESPONSE = "<analysis>no fix</analysis><patches></patches>"

# Current Claude generations reject sampling parameters (temperature/top_p/top_k) with HTTP 400.
_ANTHROPIC_NO_SAMPLING_RE = re.compile(
    r"claude-(?:opus-(?:4-[78]|5)|sonnet-5|haiku-5|fable|mythos)", re.IGNORECASE)


class LLMError(RuntimeError):
    def __init__(self, status: Optional[int], body_excerpt: str = "", message: str = ""):
        self.status = status
        self.body_excerpt = body_excerpt[:500]
        super().__init__(message or f"LLM request failed (status={status}): {self.body_excerpt}")


class LLMConfigError(ValueError):
    pass


# --------------------------------------------------------------------------- LLM calls in flight (round 10)
# The fix loop's watchdog stays silent while an LLM HTTP call is in progress (the clients log their own heartbeat /
# retry lines); `LLMFixer._call`, the HTTP clients and `RecordingClient` mark their calls. Process-wide on purpose:
# one migration runs at a time (web runs are queued, the fleet is sequential).
_IN_FLIGHT = 0
_IN_FLIGHT_LOCK = threading.Lock()


@contextmanager
def llm_call_in_flight() -> Iterator[None]:
    global _IN_FLIGHT
    with _IN_FLIGHT_LOCK:
        _IN_FLIGHT += 1
    try:
        yield
    finally:
        with _IN_FLIGHT_LOCK:
            _IN_FLIGHT -= 1


def llm_calls_in_flight() -> int:
    """How many LLM calls are in progress right now (nested wrappers count once each)."""
    return _IN_FLIGHT


def usage_from(resp: LLMResponse) -> LLMUsage:
    return LLMUsage(model=resp.model, calls=1, input_tokens=resp.input_tokens, output_tokens=resp.output_tokens)


def messages_hash(messages: list[ChatMessage]) -> str:
    data = json.dumps([m.model_dump() for m in messages], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _retry_after_seconds(resp: httpx.Response) -> Optional[float]:
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
    """The assistant text inside whatever JSON a gateway returns (`response_path` wins; else known shapes)."""
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
FakeScript = Union[list[Union[str, Exception]], Callable[[list[ChatMessage]], str], None]


class FakeLLMClient:
    """Scripted client for tests / offline runs.

    responses: list (consumed in order, last one repeats; an Exception item is raised), a callable
    `fn(messages) -> str`, or None (always answers "no fix").
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
        used_model = model or self.model
        self.calls.append({"messages": list(messages), "model": used_model, "max_tokens": max_tokens,
                           "temperature": temperature, "stop": stop, "role": role,
                           "metadata": dict(metadata) if metadata else None})
        if self.responses is None:
            text: Union[str, Exception] = NO_FIX_RESPONSE
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
    def __init__(self, record_dir: str | Path, ws: Any = None):
        self.dir = Path(record_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.ws = ws
        self._n = len(list(self.dir.glob("llm-*.json")))

    def write(self, messages: list[ChatMessage], resp: Optional[LLMResponse], latency: float,
              error: Optional[str] = None, *, role: Optional[str] = None, metadata: Optional[dict] = None) -> Path:
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
    """Wraps another client and writes `llm-<n>.json` per call into record_dir (and journals via ws)."""

    def __init__(self, inner: LLMClientLike, record_dir: str | Path, ws: Any = None):
        self.inner = inner
        self.model = getattr(inner, "model", "")
        self._rec = _Recorder(record_dir, ws)

    @property
    def record_dir(self) -> Path:
        return self._rec.dir

    def complete(self, messages: list[ChatMessage], **kw: Any) -> LLMResponse:
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
        fn = getattr(self.inner, "close", None)
        if callable(fn):
            fn()


def load_replay_responses(record_dir: str | Path) -> list[str]:
    """Responses of a recorded session (llm-*.json, in order) — feed to FakeLLMClient for replay."""
    out = []
    for p in sorted(Path(record_dir).glob("llm-*.json")):
        data = json.loads(p.read_text(encoding="utf-8"))
        if data.get("response") is not None:
            out.append(data["response"])
    return out


# ---------------------------------------------------------------------------
# factory
# ---------------------------------------------------------------------------
def _unexpanded(v: Optional[str]) -> bool:
    return not v or "${" in v


def make_llm_client(cfg: Config, *, ws: Any = None, transport: Optional[httpx.BaseTransport] = None) -> LLMClientLike:
    """Build the configured client. `fake` needs no key; `base_url: replay:<dir>` with fake replays recordings.

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
            responses = load_replay_responses(cfg.resolve_path(llm.base_url[len("replay:"):]))
        client = FakeLLMClient(responses)
    else:
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
