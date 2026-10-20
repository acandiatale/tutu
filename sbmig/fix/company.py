"""The company LLM gateway, the plain way: ``requests.post(url, headers=, json=, timeout=)``.

Everything comes from the environment / ``.env`` (read by ``load_config``):

    LLM_BASE_URL   the full endpoint URL (http or https, used verbatim)
    LLM_API_KEY    bearer token
    LLM_MODEL      model name (default gpt-6-luna)
    LLM_RESPONSE_PATH   optional dot path to the reply text when auto-detection fails (e.g. data.result.content)

Request:  POST $LLM_BASE_URL, headers {"Authorization": "Bearer $LLM_API_KEY"} (+ llm.extra_headers),
          json {"model": ..., "messages": [{"role", "content"}, ...], **llm.payload_extra}   (default extra: need_orgin)
Response: the reply text is auto-detected (choices[0].message.content, content, response, data.content, ...) or
          taken from the configured path; token usage from a ``usage`` object when present.

Generation parameters (max_tokens / temperature / stop) are NOT sent: the gateway owns them. ``role`` / ``metadata``
are accepted and ignored (RecordingClient journals them). No sessions, no connection pools, no TLS knobs: exactly
what the other in-house agents do by hand.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Optional

import requests

from sbmig.core.interfaces import ChatMessage, LLMResponse

from .client import RETRY_STATUS, LLMError, _retry_after_seconds, extract_reply, messages_hash

log = logging.getLogger("sbmig.fix.client")

PostFn = Callable[..., Any]   # requests.post-compatible: post(url, headers=, json=, timeout=) -> Response


class CompanyClient:
    provider = "company"

    def __init__(self, url: str, api_key: str, model: str, *, payload_extra: Optional[dict] = None,
                 response_path: Optional[str] = None, timeout: float = 180.0, max_retries: int = 3,
                 extra_headers: Optional[dict[str, str]] = None, post: Optional[PostFn] = None,
                 sleep: Callable[[float], None] = time.sleep):
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

    def __repr__(self) -> str:                        # never leak the key
        return f"CompanyClient(url={self.url!r}, model={self.model!r})"

    # ---- the two things that go over the wire
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", **self.extra_headers}

    def payload(self, messages: list[ChatMessage], model: Optional[str] = None) -> dict[str, Any]:
        return {"model": model or self.model,
                "messages": [{"role": m.role, "content": m.content} for m in messages],
                **self.payload_extra}

    def post_once(self, payload: dict[str, Any]) -> Any:
        """One ``requests.post`` with the exact call shape; returns the ``requests.Response``."""
        return self._post(self.url, headers=self.headers(), json=payload, timeout=self.timeout)

    # ---- LLMClientLike
    def complete(self, messages: list[ChatMessage], *, model: Optional[str] = None, max_tokens: Optional[int] = None,
                 temperature: Optional[float] = None, stop: Optional[list[str]] = None, role: Optional[str] = None,
                 metadata: Optional[dict] = None, **_ignored: Any) -> LLMResponse:
        payload = self.payload(messages, model)
        if log.isEnabledFor(logging.DEBUG):
            log.debug("llm request role=%s model=%s prompt_sha256=%s chars=%d", role or "-", payload["model"],
                      messages_hash(messages)[:16], sum(len(m.content) for m in messages))
        t0 = time.monotonic()
        last_status: Optional[int] = None
        last_body = ""
        for attempt in range(self.max_retries + 1):
            wait: Optional[float] = None
            try:
                resp = self.post_once(payload)
            except requests.RequestException as exc:
                last_status, last_body = None, f"{type(exc).__name__}: {exc}"
                log.warning("llm company transport error (attempt %d/%d): %s", attempt + 1, self.max_retries + 1,
                            type(exc).__name__)
            else:
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
                self._sleep(min(wait, 120.0) if wait is not None else min(30.0, 2.0 ** attempt))
        log.error("llm company giving up after %d attempt(s): status=%s body=%s", self.max_retries + 1, last_status,
                  last_body[:300].replace("\n", " "))
        raise LLMError(last_status, last_body,
                       f"LLM request failed after {self.max_retries + 1} attempt(s) (status={last_status}): {last_body[:200]}")

    def _parse(self, data: Any, model: str) -> LLMResponse:
        filtered = content_filter_reason(data)
        if filtered:
            # Azure-style gateways answer HTTP 200 with finish_reason=content_filter and a null message.content when
            # the prompt or the reply tripped the content filter: this request's problem, not an outage (the fix
            # loop's request-rejected rule keeps it out of the "LLM unavailable" streak)
            raise LLMError(200, json.dumps(data, ensure_ascii=False)[:500],
                           f"content filtered by the gateway ({filtered}); this request only, not an outage")
        content = extract_reply(data, self.response_path)
        usage = data.get("usage") if isinstance(data, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        finish = None
        if isinstance(data, dict):
            choices = data.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                finish = choices[0].get("finish_reason")
            finish = finish or data.get("finish_reason") or data.get("stop_reason")
        return LLMResponse(content=content, model=(data.get("model") if isinstance(data, dict) else None) or model,
                           input_tokens=int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
                           output_tokens=int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
                           finish_reason=finish)

    def close(self) -> None:
        return None


def content_filter_reason(data: Any) -> Optional[str]:
    """Why an OpenAI / Azure-style response carries no answer because of the content filter, else None:
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
