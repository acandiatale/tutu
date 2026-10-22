"""Per-project LLM cost (2026-10-22, the user: "입출력 토큰으로 gpt api 기준 가격 산정"): usage per model × `llm.pricing`
list prices; unpriced models are listed, never guessed; the report (en / ko), report.json, the console summary and
the fleet summary carry it."""
from __future__ import annotations

from sbmig.core.config import ModelPrice
from sbmig.core.models import FleetProjectResult, FleetState, LLMUsage
from sbmig.fleet.summary import COLUMNS, render_summary_md, render_table, rows, totals
from sbmig.report import render_markdown, write_report
from sbmig.report.cost import compute_llm_cost, fmt_money, price_for

from .test_report import sample_report

PRICING = {"gpt-5.6-terra": ModelPrice(input_per_mtok=2.0, output_per_mtok=12.0, cached_input_per_mtok=0.2),
           "gpt-6-luna": ModelPrice(input_per_mtok=0.1, output_per_mtok=0.5, cached_input_per_mtok=0.01)}


def test_price_lookup_exact_prefix_substring_and_unknown():
    assert price_for("gpt-5.6-terra", PRICING)[0] == "gpt-5.6-terra"
    assert price_for("GPT-5.6-Terra", PRICING)[0] == "gpt-5.6-terra"                 # case-insensitive
    assert price_for("gpt-5.6-terra-2026-08-01", PRICING)[0] == "gpt-5.6-terra"      # dated id → prefix
    assert price_for("company/gpt-6-luna", PRICING)[0] == "gpt-6-luna"               # gateway prefix → substring
    assert price_for("claude-sonnet-5", PRICING) == ("", None)
    assert price_for("", PRICING) == ("", None) and price_for("x", {}) == ("", None)


def test_compute_cost_per_model_and_total(cfg):
    cfg.llm.pricing = dict(PRICING)
    cfg.llm.pricing_currency = "USD"
    cfg.llm.pricing_note = "list prices"
    by = [LLMUsage(model="gpt-5.6-terra", calls=3, input_tokens=1_000_000, output_tokens=100_000),
          LLMUsage(model="gpt-6-luna", calls=2, input_tokens=500_000, output_tokens=50_000),
          LLMUsage(model="claude-sonnet-5", calls=1, input_tokens=10, output_tokens=5),
          LLMUsage(model="", calls=0)]
    c = compute_llm_cost(by, cfg)
    assert [m.model for m in c.by_model] == ["gpt-5.6-terra", "gpt-6-luna", "claude-sonnet-5"]
    assert c.by_model[0].cost == 3.2 and c.by_model[1].cost == 0.075 and c.by_model[2].cost is None
    assert c.total == 3.275 and c.unpriced_models == ["claude-sonnet-5"] and c.currency == "USD"
    assert c.note == "list prices" and not c.usage_missing
    assert fmt_money(c.total) == "$3.2750" and fmt_money(None) == "-" and fmt_money(1.5, "KRW") == "1.5000 KRW"


def test_usage_missing_flag(cfg):
    cfg.llm.pricing = dict(PRICING)
    c = compute_llm_cost([LLMUsage(model="gpt-6-luna", calls=4)], cfg)
    assert c.usage_missing and c.total == 0.0 and c.by_model[0].cost == 0.0


def _priced_report(cfg):
    cfg.llm.pricing = dict(PRICING)
    cfg.llm.pricing_note = "OpenAI list prices as of 2026-10-22"
    r = sample_report(success=True)
    r.llm_usage_by_model = [LLMUsage(model="gpt-5.6-terra", calls=2, input_tokens=200_000, output_tokens=20_000),
                            LLMUsage(model="gpt-6-luna", calls=1, input_tokens=50_000, output_tokens=2_000),
                            LLMUsage(model="in-house-x", calls=1, input_tokens=100, output_tokens=10)]
    r.llm_cost = compute_llm_cost(r.llm_usage_by_model, cfg)
    return r


def test_report_sections_en_and_ko(cfg):
    r = _priced_report(cfg)
    en = render_markdown(r)
    assert "## LLM usage and cost" in en and "| `gpt-5.6-terra` | 2 | 200,000 | 20,000 | $2.0000 / $12.0000 | $0.6400 |" in en
    assert "| **Total** | | | | | **$0.6460** |" in en and "| LLM cost | $0.6460 (+ unpriced: in-house-x)" in en
    assert "No price for `in-house-x`" in en and "OpenAI list prices as of 2026-10-22" in en
    ko = render_markdown(r, language="ko")
    assert "## LLM 사용량과 비용" in ko and "| **합계** | | | | | **$0.6460** |" in ko
    assert "단가 없음: in-house-x" in ko and "## LLM usage and cost" not in ko


def test_report_json_and_console_carry_the_cost(cfg, tmp_path):
    from rich.console import Console

    from sbmig.core.workspace import Workspace
    from sbmig.report import console_summary
    r = _priced_report(cfg)
    src = tmp_path / "src"; (src / "x").mkdir(parents=True); (src / "pom.xml").write_text("<project/>")
    ws = Workspace.from_input(src, tmp_path / "out", run_id="cost-run")
    write_report(r, ws, cfg=cfg)
    import json
    data = json.loads((ws.run_dir / "report.json").read_text(encoding="utf-8"))
    assert data["llm_cost"]["total"] == 0.646 and data["llm_cost"]["unpriced_models"] == ["in-house-x"]
    assert [m["model"] for m in data["llm_usage_by_model"]] == ["gpt-5.6-terra", "gpt-6-luna", "in-house-x"]
    con = Console(record=True, width=200)
    console_summary(r, con)
    assert "LLM cost: $0.6460 (+ unpriced: in-house-x)" in con.export_text()


def test_fleet_summary_has_a_cost_column():
    st = FleetState(fleet_id="f", manifest="m.yml", projects=[
        FleetProjectResult(name="a", input="a", status="success", llm_calls=3, llm_cost=0.646),
        FleetProjectResult(name="b", input="b", status="success", llm_calls=0, llm_cost=None),
        FleetProjectResult(name="c", input="c", status="incomplete", llm_calls=5, llm_cost=1.2),
    ])
    assert "llm_cost" in COLUMNS and COLUMNS.index("llm_cost") == COLUMNS.index("llm_calls") + 1
    rs = rows(st)
    assert [r["llm_cost"] for r in rs] == [0.646, "", 1.2]
    assert totals(st)["llm_cost"] == 1.846
    md = render_summary_md(st)
    assert "LLM calls: 8 (cost 1.8460, see llm.pricing)" in md and "| llm_cost |" in render_table(st)
