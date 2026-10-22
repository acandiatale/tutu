"""Per-run LLM cost (2026-10-22, the user: "프로젝트당 API 가격"): usage per model × `llm.pricing` list prices.

    compute_llm_cost(by_model, cfg) -> LLMCost
    price_for(model, pricing) -> (key, ModelPrice | None)

Pure functions; prices come from the config (company specifics never live in code). A model without a price is
listed as unpriced and left out of the total, never guessed."""
from __future__ import annotations

from typing import Any, Optional

from sbmig.core.models import LLMCost, LLMUsage, ModelCost


def price_for(model: str, pricing: dict[str, Any]) -> tuple[str, Optional[Any]]:
    """The `llm.pricing` entry for `model`: exact id first, else the longest key that is a prefix of the id or is
    contained in it (case-insensitive) — `gpt-5.6-terra-2026-08-01` → `gpt-5.6-terra`."""
    m = (model or "").strip().lower()
    if not m or not pricing:
        return "", None
    keys = {str(k).strip().lower(): k for k in pricing}
    if m in keys:
        return keys[m], pricing[keys[m]]
    best = ""
    for k in keys:
        if k and (m.startswith(k) or k in m) and len(k) > len(best):
            best = k
    return (keys[best], pricing[keys[best]]) if best else ("", None)


def _price_fields(p: Any) -> tuple[float, float]:
    if isinstance(p, dict):
        return float(p.get("input_per_mtok", 0.0)), float(p.get("output_per_mtok", 0.0))
    return float(getattr(p, "input_per_mtok", 0.0)), float(getattr(p, "output_per_mtok", 0.0))


def compute_llm_cost(by_model: list[LLMUsage], cfg: Any) -> LLMCost:
    llm = getattr(cfg, "llm", None)
    pricing = dict(getattr(llm, "pricing", None) or {})
    out = LLMCost(currency=str(getattr(llm, "pricing_currency", "USD") or "USD"),
                  note=str(getattr(llm, "pricing_note", "") or ""))
    total = 0.0
    for u in by_model:
        if not (u.calls or u.input_tokens or u.output_tokens):
            continue
        key, price = price_for(u.model, pricing)
        row = ModelCost(model=u.model or "?", calls=u.calls, input_tokens=u.input_tokens,
                        output_tokens=u.output_tokens, price_key=key)
        if price is not None:
            inp, outp = _price_fields(price)
            row.input_per_mtok, row.output_per_mtok = inp, outp
            row.cost = round(u.input_tokens / 1_000_000 * inp + u.output_tokens / 1_000_000 * outp, 6)
            total += row.cost
        else:
            out.unpriced_models.append(u.model or "?")
        if u.calls and not (u.input_tokens or u.output_tokens):
            out.usage_missing = True
        out.by_model.append(row)
    out.total = round(total, 6)
    return out


def fmt_money(value: Optional[float], currency: str = "USD") -> str:
    if value is None:
        return "-"
    sym = "$" if currency.upper() == "USD" else ""
    return f"{sym}{value:,.4f}" + ("" if sym else f" {currency}")
