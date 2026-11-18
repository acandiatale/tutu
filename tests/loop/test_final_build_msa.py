"""Review 2026-11-18 L5: an MSA run builds one final build per service; when an earlier service's final build failed and a
later one passed, `report.final_build` must keep the failing result (the Korean guide's `_build_failed`, the "last build
log" lines and scripts/e2e.py read it)."""
from __future__ import annotations

from sbmig.core.models import CaseType
from sbmig.report.guide_ko import _build_failed

from .fakes import FakeBuildRunner, FakeFixer, FakeRewriteRunner, bad_build, compile_error, make_profile, ok_build, \
    v2_msa_plan, write_script
from .test_orchestrator import make_orch


def test_failed_service_build_is_not_overwritten_by_a_later_passing_one(cfg, ws):
    cfg.loop.phases["verify"].max_iterations = 1

    def build(w, c):
        if c["module"] == "svc-a":
            return bad_build(compile_error("svc-a/src/main/java/A.java")).model_copy(update={"log_path": "logs/a.log"})
        return ok_build().model_copy(update={"log_path": "logs/b.log"})
    report = make_orch(cfg, ws, plan=v2_msa_plan(), build=FakeBuildRunner(ws, fn=build), rewrite=FakeRewriteRunner(ws),
                       profile=make_profile(CaseType.MSA),
                       fixer=FakeFixer(script_fn=lambda w, c, ctx: write_script({"notes.txt": "x\n"}))).run()
    assert not report.success
    assert report.final_build is not None and not report.final_build.ok
    assert report.final_build.log_path == "logs/a.log"
    assert _build_failed(report)


def test_all_services_pass_keeps_the_last_result(cfg, ws):
    def build(w, c):
        return ok_build().model_copy(update={"log_path": f"logs/{c['module']}.log"})
    report = make_orch(cfg, ws, plan=v2_msa_plan(), build=FakeBuildRunner(ws, fn=build), rewrite=FakeRewriteRunner(ws),
                       profile=make_profile(CaseType.MSA)).run()
    assert report.success and report.final_build.ok and report.final_build.log_path == "logs/svc-b.log"
