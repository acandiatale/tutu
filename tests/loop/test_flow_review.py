"""Regressions from the 2026-10-20 end-to-end flow review (docs/PLAN.md "round 7a"), v2 flow: the nodes take the
right path in the odd cases — a stored script reused without an LLM, the final build after an internal abort, LLM
transport classification, attempt ids across re-rounds, no harvest in v2, `stop_after` validation."""
from __future__ import annotations

import pytest

from sbmig.core.models import FixProposal, LLMUsage, Phase
from sbmig.core.workspace import Workspace
from sbmig.loop import MissingComponentError, parse_stop_after
from sbmig.loop.fixloop import is_llm_transport_error

from .fakes import (FakeBuildRunner, FakeFixer, FakeKB, FakeRestructurer, case_of, make_project, write_fix,
                    write_script)
from .test_orchestrator import BAR, FOO, files_build, make_orch


@pytest.fixture()
def ws(tmp_path):
    w = Workspace.from_input(make_project(tmp_path), tmp_path / "out", run_id="t-run")
    w.git_init()
    return w


# ------------------------------------------------------------------ B1 (v2): a stored script needs no LLM
def test_stored_script_is_reused_without_an_llm(cfg, ws, monkeypatch):
    cfg = cfg.model_copy(deep=True)
    cfg.llm.provider = "company"
    cfg.llm.base_url = "http://gw.invalid/v1/chat"
    monkeypatch.delenv("LLM_API_KEY", raising=False)          # → make_llm_client raises LLMConfigError
    monkeypatch.setenv("SBMIG_ENV_FILE", str(ws.run_dir / "none.env"))
    kb = FakeKB()
    kb.scripts.seed(case_of("compile|x"), write_script({FOO: "class Foo { ok }\n", BAR: "class Bar { ok }\n"}).code)
    orch = make_orch(cfg, ws, kb=kb)
    orch._fixer = None          # the production default: LazyFixer(LLMFixer(cfg, make_llm_client(cfg)))
    orch._llm = None
    report = orch.run()
    reuse = [a for pr in report.phases for a in pr.attempts if a.script_mode == "reuse"]
    assert reuse and all(a.applied for a in reuse)
    assert report.success and report.llm_usage.calls == 0


# ------------------------------------------------------------------ B2: internal error after plan → final build still runs
def test_internal_error_still_attempts_the_final_build(cfg, ws):
    class Boom(FakeRestructurer):
        def apply(self, *a, **k):
            raise MissingComponentError("sbmig.restructure.Restructurer cannot be imported")
    build = FakeBuildRunner(ws, fn=files_build)
    orch = make_orch(cfg, ws, build=build, restructurer=Boom())
    report = orch.run()
    assert "aborted" in report.flags and any("missing component" in f for f in report.manual_followups)
    assert build.calls[-1]["label"] == "verify" and build.calls[-1]["goal"] == "clean package"
    assert report.final_build is not None and report.phases[-1].phase == Phase.VERIFY
    assert any("after the run aborted" in n for n in report.phases[-1].notes)


def test_no_final_build_after_abort_when_disabled_or_dry_run(cfg, ws):
    class Boom(FakeRestructurer):
        def apply(self, *a, **k):
            raise MissingComponentError("sbmig.restructure.Restructurer cannot be imported")
    cfg = cfg.model_copy(deep=True)
    cfg.loop.always_final_verify = False
    build = FakeBuildRunner(ws, fn=files_build)
    orch = make_orch(cfg, ws, build=build, restructurer=Boom())
    report = orch.run()
    assert "aborted" in report.flags and "verify" not in [c["label"] for c in build.calls]
    assert report.final_build is None and report.unresolved == []
    orch._dry_run, orch.cfg.loop.always_final_verify = True, True
    orch._final_build_after_abort(report)
    assert "verify" not in [c["label"] for c in build.calls]


# ------------------------------------------------------------------ B3: transport vs. request/response classification
@pytest.mark.parametrize("analysis,transport", [
    ("llm-error: invalid JSON from LLM: Expecting value: line 1 column 1 (char 0)", True),      # HTML 200 from a proxy
    ("llm-error: llm.response_path 'data.x' not found in the response (KeyError: 'x')", True),  # config error
    ("llm-error: cannot find the reply text in the response; set LLM_RESPONSE_PATH", True),
    ("llm-error: LLM request failed after 4 attempt(s) (status=503): Service Unavailable", True),
    ("llm-error: LLM request failed (status=400): {\"error\":\"maximum context length exceeded\"}", False),
    ("llm-error: LLM request failed (status=413): payload too large", False),
    ("llm-error: the model did not follow the protocol", False),
    ("llm-error: content filtered by the gateway (finish_reason=content_filter); this request only", False),  # round 9
    ("fixed it", False),
])
def test_transport_classification(analysis, transport):
    assert is_llm_transport_error(analysis) is transport


# ------------------------------------------------------------------ B4: attempt ids stay unique across re-rounds
def test_reround_keeps_attempt_ids_unique(cfg, ws):
    calls = {"n": 0}

    def fx(w, c, ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            return FixProposal(changes=[], analysis="need more", model="fake", llm_usage=LLMUsage(model="fake", calls=1))
        return write_fix(None, "class X { ok }\n")
    report = make_orch(cfg, ws, fixer=FakeFixer(fn=fx)).run()
    ids = [a.id for a in report.phases[-1].attempts]
    assert report.success and len(ids) == 2 and len(ids) == len(set(ids))


# ------------------------------------------------------------------ R1 / R5 (v2): no KB recipe credit, no harvest
def test_v2_never_harvests_even_when_kb_harvest_is_on(cfg, ws):
    cfg = cfg.model_copy(deep=True)
    cfg.kb.harvest = cfg.kb.generate_recipe_candidates = True
    kb = FakeKB()
    report = make_orch(cfg, ws, kb=kb).run()
    assert report.success and kb.harvests == [] and kb.credits == [] and report.harvest is None


# ------------------------------------------------------------------ N1: stop_after validation (CLI / web / api)
def test_parse_stop_after():
    assert parse_stop_after(None) is None and parse_stop_after("") is None
    assert parse_stop_after("transform") is Phase.TRANSFORM
    assert parse_stop_after(" Spring ") is Phase.SPRING and parse_stop_after(Phase.VERIFY) is Phase.VERIFY
    for bad in ("report", "nope", 3):
        with pytest.raises(ValueError, match="stop_after must be one of: intake, analyze, plan, transform, jdk21, "
                                             "spring, restructure, verify"):
            parse_stop_after(bad)


# ------------------------------------------------------------------ N5: a reused orchestrator starts clean
def test_orchestrator_reuse_starts_from_a_clean_state(cfg, ws):
    orch = make_orch(cfg, ws)
    first = orch.run(stop_after=Phase.TRANSFORM)
    assert first.final_build is None
    second = orch.run()
    assert second is not first and second.final_build is not None and second.success
    assert len(second.phases) == 3 and "stopped-after-transform" not in second.flags
