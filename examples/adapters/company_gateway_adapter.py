"""Example tier-1 adapter: an in-house, OpenAI-ish LLM gateway behind a custom `X-Api-Key` header.

Wire it in (config yml):

    llm:
      provider: plugin
      plugin: examples.adapters.company_gateway_adapter:make_adapter
      api_key_env: COMPANY_LLM_KEY            # the key stays in the environment; sbmig hands it over in memory
      plugin_options:
        base_url: https://llm-gateway.internal.example/api
        default_model: internal-coder-m
        models: {fix: internal-coder-l, fix_escalation: internal-coder-xl, plan_advisor: internal-small,
                 structure_review: internal-coder-l}
        timeout: 120
        max_retries: 3
        team: platform-migration              # sent as X-Team for chargeback

Replace the request/response mapping in `_to_body` / `_from_body` with your gateway's real schema.
Only httpx is used; no vendor SDK.
"""
from __future__ import annotations

import logging
import os
import random
import time
from typing import Any, Callable, Optional

import httpx

from sbmig.core.interfaces import LLMRequest, LLMResponse
from sbmig.llm import LLMAdapter

log = logging.getLogger("company_gateway_adapter")

RETRY_STATUS = {408, 429, 500, 502, 503, 504}


class GatewayError(RuntimeError):
    def __init__(self, status: Optional[int], message: str):
        self.status = status
        super().__init__(message)


class CompanyGatewayAdapter(LLMAdapter):
    """`complete(request)` → POST {base_url}/v1/chat/completions with `X-Api-Key`; retries 429/5xx."""

    def __init__(self, base_url: str, api_key: str, *, default_model: str = "internal-coder-m",
                 models: Optional[dict[str, str]] = None, timeout: float = 120.0, max_retries: int = 3,
                 team: Optional[str] = None, transport: Optional[httpx.BaseTransport] = None,
                 sleep: Callable[[float], None] = time.sleep):
        if not base_url:
            raise ValueError("company gateway: plugin_options.base_url is required")
        if not api_key:
            raise ValueError("company gateway: no API key (set the env var named by llm.api_key_env, "
                             "or COMPANY_LLM_KEY)")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key                      # never logged, never written
        self.model = default_model
        self.models = dict(models or {})
        self.max_retries = max(0, int(max_retries))
        self.team = team
        self._sleep = sleep
        self._http = httpx.Client(timeout=timeout, transport=transport)

    def __repr__(self) -> str:
        return f"CompanyGatewayAdapter(base_url={self.base_url!r}, model={self.model!r})"

    @property
    def name(self) -> str:
        return "company-gateway"

    # -------------------------------------------------------------- mapping
    def pick_model(self, request: LLMRequest) -> str:
        """Role routing table wins over sbmig's model (so the gateway team controls cost per purpose)."""
        return self.models.get(request.role) or request.model or self.model

    def _to_body(self, request: LLMRequest, model: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
        }
        if request.max_tokens:
            body["max_tokens"] = request.max_tokens
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.stop:
            body["stop"] = request.stop
        # non-secret routing / audit context (run_id, phase, cluster_id …)
        body["metadata"] = {"purpose": request.role, **{k: str(v) for k, v in request.metadata.items()}}
        return body

    def _from_body(self, data: dict[str, Any], model: str) -> LLMResponse:
        choice = (data.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content")
        if not isinstance(content, str):
            raise GatewayError(200, f"gateway response without message content: {str(data)[:200]}")
        usage = data.get("usage") or {}
        return LLMResponse(content=content, model=data.get("model") or model,
                           input_tokens=int(usage.get("prompt_tokens") or 0),
                           output_tokens=int(usage.get("completion_tokens") or 0),
                           finish_reason=choice.get("finish_reason"))

    def _headers(self, request: LLMRequest) -> dict[str, str]:
        h = {"Content-Type": "application/json", "X-Api-Key": self._api_key, "X-Purpose": request.role}
        if self.team:
            h["X-Team"] = self.team
        return h

    # -------------------------------------------------------------- LLMAdapterLike
    def complete(self, request: LLMRequest) -> LLMResponse:
        model = self.pick_model(request)
        url = f"{self.base_url}/v1/chat/completions"
        body, headers = self._to_body(request, model), self._headers(request)
        last: Optional[GatewayError] = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._http.post(url, json=body, headers=headers)
            except httpx.TransportError as exc:
                last = GatewayError(None, f"transport error: {type(exc).__name__}")
            else:
                if resp.status_code < 300:
                    return self._from_body(resp.json(), model)
                last = GatewayError(resp.status_code, f"gateway HTTP {resp.status_code}: {resp.text[:200]}")
                if resp.status_code not in RETRY_STATUS:
                    raise last                       # sbmig turns this into LLMError (→ llm_unavailable if repeated)
            if attempt < self.max_retries:
                delay = min(30.0, 2 ** attempt) + random.uniform(0, 0.5)
                log.warning("gateway %s, retry %d/%d in %.1fs", last, attempt + 1, self.max_retries, delay)
                self._sleep(delay)
        assert last is not None
        raise last

    def close(self) -> None:
        self._http.close()


def make_adapter(options: dict, cfg: Any = None) -> CompanyGatewayAdapter:
    """Factory referenced by `llm.plugin`. `options` = llm.plugin_options (+ "api_key" from llm.api_key_env)."""
    opts = dict(options or {})
    api_key = opts.pop("api_key", None) or os.environ.get("COMPANY_LLM_KEY", "")
    default_model = opts.get("default_model")
    if not default_model and cfg is not None and "${" not in (cfg.llm.model or "${"):
        default_model = cfg.llm.model
    return CompanyGatewayAdapter(
        base_url=opts.get("base_url") or os.environ.get("COMPANY_LLM_BASE_URL", ""),
        api_key=api_key,
        default_model=default_model or "internal-coder-m",
        models=opts.get("models"),
        timeout=float(opts.get("timeout", 120)),
        max_retries=int(opts.get("max_retries", 3)),
        team=opts.get("team"),
        transport=opts.get("transport"),            # tests only: httpx.MockTransport
    )
