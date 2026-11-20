"""v2.1z-bg point 3 in the run report: usage rows are named by the asked model (never `unknown` / `?` / empty), the
served models ride along, and the price is found by the asked name first, then by a served name."""
from __future__ import annotations

from sbmig.core.config import ModelPrice
from sbmig.core.interfaces import LLMResponse
from sbmig.core.models import LLMUsage, ScriptProposal
from sbmig.report.cost import compute_llm_cost

from .fakes import FakeBuildRunner, FakeFixer
from .test_orchestrator import files_build, make_orch


def _cfg(cfg, pricing=None):
    cfg = cfg.model_copy(deep=True)
    cfg.llm.model = "build_error_fixer"
    cfg.llm.escalation_model = "escalation_fixer"
    cfg.llm.cooldown_seconds = 0
    cfg.llm.pricing = {k: ModelPrice(**v) for k, v in (pricing or {}).items()}
    return cfg


def _script(model: str, served=(), calls=1) -> ScriptProposal:
    from .fakes import script_for
    from sbmig.core.models import FileChange
    return ScriptProposal(code=script_for([FileChange(path="*", op="create", content="class X { ok }\n")]),
                          analysis="fixed", model=model,
                          llm_usage=LLMUsage(model=model, calls=calls, input_tokens=100 * calls,
                                             output_tokens=20 * calls, served=list(served)))


def test_placeholder_names_of_a_proposal_and_a_review_fall_back_to_the_asked_model(cfg, ws):
    """An agent-gateway answer as an older client / a plugin reported it (`unknown`), and a review client answer with
    an empty model, end up under the configured model's name — before: rows `unknown` and `?`."""
    cfg = _cfg(cfg)
    fixer = FakeFixer(script_fn=lambda w, c, ctx: _script("unknown", served=["gpt-5.5"]))

    class Inner:
        def complete(self, *a, **k):
            return LLMResponse(content="NONE", model="", input_tokens=1_000, output_tokens=10,
                               served_models=["gpt-5.5"])

    class Reviewer:
        def __init__(self, cfg, llm, runner, kb):
            self.llm = llm

        def review(self, ws, profile, cfg, **kw):
            from sbmig.core.models import StructureReview
            self.llm.complete("x")
            return StructureReview(verdict="ok")
    report = make_orch(cfg, ws, build=FakeBuildRunner(ws, fn=files_build), fixer=fixer, llm=Inner(),
                       reviewer=Reviewer).run()
    names = [u.model for u in report.llm_usage_by_model]
    assert names == ["build_error_fixer"], names
    row = report.llm_usage_by_model[0]
    assert row.served == ["gpt-5.5"] and row.calls >= 2
    assert report.llm_usage.model == "build_error_fixer"
    assert [m.model for m in report.llm_cost.by_model] == ["build_error_fixer"]


def test_a_run_whose_calls_all_failed_names_its_total_by_the_asked_model(cfg, ws):
    """Every ask failed (no answer, no tokens): the budget still counts the calls, and the admin page used to show the
    total under `?` because no row and no total name existed."""
    cfg = _cfg(cfg)
    failed = ScriptProposal(code="", analysis="llm-error: LLM request failed (status=503): busy", model="",
                            llm_usage=LLMUsage(), error="busy")
    fixer = FakeFixer(script_fn=lambda w, c, ctx: failed)
    report = make_orch(cfg, ws, build=FakeBuildRunner(ws, fn=files_build), fixer=fixer).run()
    assert report.llm_usage.calls >= 1
    assert report.llm_usage.model == "build_error_fixer"
    assert all(u.model not in ("", "?", "unknown") for u in report.llm_usage_by_model)


def test_escalation_attempts_keep_their_own_row(cfg, ws):
    cfg = _cfg(cfg)
    answers = iter([_script("build_error_fixer", ["gpt-5.5"]), _script("escalation_fixer", ["gpt-5.6-sol"])])

    def fn(w, c, ctx):
        return next(answers, _script("build_error_fixer", ["gpt-5.5"]))
    report = make_orch(cfg, ws, build=FakeBuildRunner(ws, fn=files_build), fixer=FakeFixer(script_fn=fn)).run()
    by = {u.model: u for u in report.llm_usage_by_model}
    assert by["build_error_fixer"].served == ["gpt-5.5"]
    if "escalation_fixer" in by:
        assert by["escalation_fixer"].served == ["gpt-5.6-sol"]


def test_price_by_asked_name_then_served_name(cfg):
    cfg = _cfg(cfg, {"gpt-5.5": {"input_per_mtok": 1.0, "output_per_mtok": 10.0},
                     "escalation_fixer": {"input_per_mtok": 5.0, "output_per_mtok": 50.0}})
    rows = [LLMUsage(model="build_error_fixer", calls=1, input_tokens=1_000_000, output_tokens=100_000,
                     served=["gpt-5.5"]),
            LLMUsage(model="escalation_fixer", calls=1, input_tokens=1_000_000, output_tokens=0,
                     served=["gpt-5.6-sol"]),
            LLMUsage(model="recipe_helper", calls=1, input_tokens=10, output_tokens=0, served=["gpt-9"])]
    c = compute_llm_cost(rows, cfg)
    by = {m.model: m for m in c.by_model}
    assert by["build_error_fixer"].price_key == "gpt-5.5" and by["build_error_fixer"].cost == 2.0
    assert by["build_error_fixer"].served == ["gpt-5.5"]
    assert by["escalation_fixer"].price_key == "escalation_fixer" and by["escalation_fixer"].cost == 5.0
    assert by["recipe_helper"].cost is None and c.unpriced_models == ["recipe_helper"]
    assert c.total == 7.0


def test_report_tables_print_the_served_model_in_brackets(cfg, ws):
    from sbmig.core.models import CaseType, RunReport
    from sbmig.report.cost import compute_llm_cost as cc
    from sbmig.report.markdown import cost_lines
    cfg = _cfg(cfg, {"gpt-5.5": {"input_per_mtok": 1.0, "output_per_mtok": 10.0}})
    r = RunReport(run_id="r", project_name="p", started_at="2026-11-20T00:00:00Z", case_type=CaseType.UNKNOWN)
    r.llm_usage = LLMUsage(model="build_error_fixer", calls=1, input_tokens=10, output_tokens=1, served=["gpt-5.5"])
    r.llm_usage_by_model = [r.llm_usage]
    r.llm_cost = cc(r.llm_usage_by_model, cfg)
    en = "\n".join(cost_lines(r, "en"))
    ko = "\n".join(cost_lines(r, "ko"))
    assert "| `build_error_fixer` (gpt-5.5) | 1 |" in en
    assert "| `build_error_fixer` (gpt-5.5) | 1 |" in ko
    r.llm_usage_by_model = [LLMUsage(model="m", calls=1, input_tokens=1)]
    r.llm_cost = cc(r.llm_usage_by_model, cfg)
    assert "| `m` | 1 |" in "\n".join(cost_lines(r, "en"))           # unchanged without a served name
