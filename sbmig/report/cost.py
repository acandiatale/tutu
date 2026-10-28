"""실행 한 번의 LLM 비용 — 모델별 사용량(토큰) × ``llm.pricing`` 표의 단가 (사용자 요청 2026-10-22 "프로젝트당
API 가격"). 흐름상 위치: 보고서 단계에서 writer.py 가 ``compute_llm_cost`` 를 불러 report.md 의 "LLM usage and
cost" 를 채운다. 순수 함수뿐이다. 단가는 설정에서만 온다(회사 고유값은 코드에 두지 않는다, CLAUDE.md 불변조건
2). 단가가 없는 모델은 추정하지 않고 "unpriced" 로 따로 적고 합계에서 뺀다.

Per-run LLM cost (2026-10-22, the user: "프로젝트당 API 가격"): usage per model × `llm.pricing` list prices.

    compute_llm_cost(by_model, cfg) -> LLMCost
    price_for(model, pricing) -> (key, ModelPrice | None)

Pure functions; prices come from the config (company specifics never live in code). A model without a price is
listed as unpriced and left out of the total, never guessed."""
from __future__ import annotations

from typing import Any, Optional

from sbmig.core.models import LLMCost, LLMUsage, ModelCost


def price_for(model: str, pricing: dict[str, Any]) -> tuple[str, Optional[Any]]:
    """``model`` 의 ``llm.pricing`` 항목을 찾는다: 정확히 같은 id 먼저, 없으면 id 의 접두어이거나
    id 에 포함되는 가장 긴 키(대소문자 무시). 날짜가 붙은 모델 id
    도 기본 이름의 단가로 잡기 위해서다. 없으면 ``("", None)``.

    The `llm.pricing` entry for `model`: exact id first, else the longest key that is a prefix of the id or is
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
    # 설정에서 읽은 dict 와 ModelPrice 객체 둘 다 받는다(테스트·플러그인이 dict 를 넘기기도 함)
    if isinstance(p, dict):
        return float(p.get("input_per_mtok", 0.0)), float(p.get("output_per_mtok", 0.0))
    return float(getattr(p, "input_per_mtok", 0.0)), float(getattr(p, "output_per_mtok", 0.0))


def compute_llm_cost(by_model: list[LLMUsage], cfg: Any) -> LLMCost:
    """모델별 사용량에 단가를 곱해 실행 비용(LLMCost)을 만든다. 호출 0·토큰 0 인 모델은 건너뛰고,
    호출은 있는데 토큰 수가 안 온 경우(게이트웨이가 usage 를 안 줌) ``usage_missing`` 을 켠다."""
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
        # 회사 게이트웨이는 usage 를 안 돌려줄 수 있다 — 그때 비용 0 이 "공짜" 로 읽히지 않게 표시만 남긴다
        if u.calls and not (u.input_tokens or u.output_tokens):
            out.usage_missing = True
        out.by_model.append(row)
    out.total = round(total, 6)
    return out


def fmt_money(value: Optional[float], currency: str = "USD") -> str:
    """금액 표시: USD 는 ``$1,234.5678``, 다른 통화는 ``1,234.5678 KRW`` 식, 값이 없으면 ``-``."""
    if value is None:
        return "-"
    sym = "$" if currency.upper() == "USD" else ""
    return f"{sym}{value:,.4f}" + ("" if sym else f" {currency}")
