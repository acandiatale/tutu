"""report.md / report.json of a remediation (sbmig/remediations/report.py)."""
from __future__ import annotations

import json

from sbmig.remediations import graph, report
from sbmig.remediations.models import (
    D_PENDING,
    FAILED,
    INCOMPLETE,
    SUCCEEDED,
    BoundaryFinding,
    Residual,
    RoundPlan,
)

from .fakes import (
    FakeIntakeFns,
    FakeMaven,
    FakeScanner,
    Recorder,
    ScriptedPlanner,
    finding,
    make_cfg,
    make_components,
    make_run,
    proposal,
    scan_result,
)


def _run(tmp_path, **kw):
    state, paths = make_run(tmp_path)
    cfg = kw.pop("cfg", None) or make_cfg()
    comps = make_components(**kw)
    rec = Recorder()
    out = graph.run_remediation(state, paths, cfg, comps, save=rec.save, journal=rec.journal, stop=rec.stop)
    return out, paths


def _fixing(**kw):
    return dict(scan=FakeScanner({"baseline": [scan_result(finding())], "final-r1": [scan_result()]}),
                plan=ScriptedPlanner([RoundPlan(round=1, source_tree_hash="", policy_hash="", proposals=[proposal()])]),
                **kw)


def test_succeeded_report(tmp_path):
    out, paths = _run(tmp_path, **_fixing())
    assert out.status == SUCCEEDED
    md = paths.report_md.read_text(encoding="utf-8")
    lines = md.splitlines()
    assert lines[0].startswith("# 취약점 조치 보고서")
    assert "**SUCCEEDED** (성공)" in md and "다음에 할 일" in md
    assert "## 라운드 1" in md and "`example:lib`" in md and "1.0 → 1.1" in md and "자동" in md
    assert "repository:nexus.invalid" in md
    assert f"sha256: `{out.delivery.sha256}`" in md
    assert "최종 검사한 ZIP 과 sha256 이 같은가: 예" in md
    assert "## 바꾸지 않은 것" in md
    data = json.loads(paths.report_json.read_text(encoding="utf-8"))
    assert data["schema"] == report.REPORT_SCHEMA and data["state"]["status"] == SUCCEEDED
    assert data["delivery_matches_scanned_zip"] is True
    assert data["final_scan"]["complete"] is True and data["baseline_scan"]["findings"] == 1
    assert data["rounds"][0]["plan"]["proposals"][0]["to"] == "1.1"


def test_baseline_failure_report(tmp_path):
    out, paths = _run(tmp_path, maven=FakeMaven(outcomes=[False]))
    assert out.status == FAILED
    md = paths.report_md.read_text(encoding="utf-8")
    assert "**FAILED** (실패)" in md and "`baseline_build_failed`" in md
    assert "기준 빌드 실패" in md and "baseline/build.log" in md
    assert "## 전달 ZIP" in md and "없습니다" in md


def test_incomplete_report_lists_residuals_and_boundary(tmp_path):
    plan = ScriptedPlanner([RoundPlan(round=1, source_tree_hash="", policy_hash="", proposals=[proposal()],
                                      residuals=[Residual(ga="example:vendored", version="0.9", origin="vendored",
                                                          reason="manual_review", detail="WEB-INF/lib|jar")])])
    b = [BoundaryFinding(code="maven_wrapper", path="mvnw", action="ignore", detail="not executed")]
    out, paths = _run(tmp_path, **{**_fixing(), "plan": plan, "intake": FakeIntakeFns(boundary=b)})
    assert out.status == INCOMPLETE
    md = paths.report_md.read_text(encoding="utf-8")
    assert "**INCOMPLETE**" in md and "`manual_review`" in md
    assert "`example:vendored`" in md and "WEB-INF/lib\\|jar" in md
    assert "`maven_wrapper` `mvnw` → 기록만" in md


def test_report_is_deterministic_and_has_no_absolute_path(tmp_path):
    out, paths = _run(tmp_path, **_fixing())
    first_md, first_json = paths.report_md.read_bytes(), paths.report_json.read_bytes()
    report.write_report(out, paths)
    assert paths.report_md.read_bytes() == first_md and paths.report_json.read_bytes() == first_json
    for text in (first_md.decode(), first_json.decode()):
        assert str(tmp_path) not in text
        assert str(tmp_path.resolve()) not in text


def test_absolute_paths_in_messages_are_relativized(tmp_path):
    out, paths = _run(tmp_path, **_fixing())
    out.message = f"see {paths.root}/rounds/1/build.log"
    report.write_report(out, paths)
    md = paths.report_md.read_text(encoding="utf-8")
    assert "see rounds/1/build.log" in md and str(paths.root) not in md


def test_summary_is_small_and_json(tmp_path):
    out, _ = _run(tmp_path, **_fixing())
    s = report.summary(out)
    assert s["status"] == SUCCEEDED and s["delivery"]["sha256"] == out.delivery.sha256
    assert set(s) >= {"id", "name", "status", "reason", "reasons", "counts", "round", "next_node"}
    json.dumps(s)


def test_pending_proposal_decision_label(tmp_path):
    scan = FakeScanner({"baseline": [scan_result(finding())]})
    plan = ScriptedPlanner([RoundPlan(round=1, source_tree_hash="", policy_hash="",
                                      proposals=[proposal(decision=D_PENDING, risks=["major"])])])
    out, paths = _run(tmp_path, scan=scan, plan=plan)
    assert not paths.report_md.exists()          # a waiting status writes no report
    report.write_report(out, paths)               # the service may still render one on demand
    md = paths.report_md.read_text(encoding="utf-8")
    assert "승인 대기" in md and "major" in md and "`pending_approval`" in md


def test_report_scrubs_secret_env_values_without_the_output_hook(tmp_path, monkeypatch):
    monkeypatch.setenv("IVAS_PASSWORD", "s3cr3t-report-value")
    state, paths = make_run(tmp_path)
    state.status, state.reason, state.message = FAILED, "internal_error", "boom s3cr3t-report-value"
    state.residuals = [Residual(ga="example:lib", version="1", origin="unmapped", reason="manual_review",
                                detail="https://u:s3cr3t-report-value@nexus.invalid/x")]
    report.write_report(state, paths)
    text = paths.report_md.read_text(encoding="utf-8") + paths.report_json.read_text(encoding="utf-8")
    assert "s3cr3t-report-value" not in text and "nexus.invalid" in text


def test_failed_candidate_included_report_says_so_first(tmp_path):
    out, paths = _run(tmp_path, **_fixing(maven=FakeMaven(outcomes=[True, False])))
    assert out.status == FAILED and out.delivery is not None and not out.delivery.build_ok
    md = paths.report_md.read_text(encoding="utf-8")
    lines = md.splitlines()
    assert lines[0].startswith("# 취약점 조치 보고서")
    head = "\n".join(lines[1:5])
    assert "빌드가 실패한 수정본" in head and "라운드 1" in head and "p-1-1" in head
    assert "rounds/1/build.log" in head and "재검사를 하지 않았습니다" in head and "source.zip" in head
    assert "빌드 성공(build_ok): 아니오" in md and "재검사했는가(scanned): 아니오" in md
    assert "결과 ZIP 은 없고" not in md
    data = json.loads(paths.report_json.read_text(encoding="utf-8"))
    assert data["delivery"]["build_ok"] is False and data["delivery"]["scanned"] is False
    assert data["verified_delivery"] is None
    s = report.summary(out)
    assert s["delivery"]["build_ok"] is False and s["delivery"]["scanned"] is False


def test_failed_candidate_discarded_report_has_no_warning_block(tmp_path):
    out, paths = _run(tmp_path, cfg=make_cfg(failed_candidate="discard"),
                      **_fixing(maven=FakeMaven(outcomes=[True, False])))
    assert out.status == FAILED and out.delivery is None
    md = paths.report_md.read_text(encoding="utf-8")
    assert "빌드가 실패한 수정본" not in md and "결과 ZIP 은 없고" in md
    assert json.loads(paths.report_json.read_text(encoding="utf-8"))["delivery"] is None


def test_succeeded_report_shows_build_ok_and_scanned(tmp_path):
    _out, paths = _run(tmp_path, **_fixing())
    md = paths.report_md.read_text(encoding="utf-8")
    assert "빌드 성공(build_ok): 예" in md and "재검사했는가(scanned): 예" in md
    data = json.loads(paths.report_json.read_text(encoding="utf-8"))
    assert data["delivery"]["build_ok"] is True and data["delivery"]["scanned"] is True


def test_proposal_note_reaches_the_report(tmp_path):
    """A proposal's note (property governs other artifacts / the override must be removed later) is shown under the table."""
    from sbmig.remediations.models import K_MANAGED_OVERRIDE, Proposal, RoundPlan, RoundState, RunPaths, RemediationState
    from sbmig.remediations.models import write_json_atomic
    from sbmig.remediations.report import build_report_json, build_report_md
    paths = RunPaths(tmp_path)
    prop = Proposal(id="p-1-1", round=1, ga="example:lib", from_version="1.0.0", to_version="1.0.1", kind=K_MANAGED_OVERRIDE,
                    decision="approved", note="the owners must remove the override when the parent catches up")
    write_json_atomic(paths.plan(1), RoundPlan(round=1, source_tree_hash="t", policy_hash="p", proposals=[prop]).to_dict())
    state = RemediationState(id="r1", status="INCOMPLETE", rounds=[RoundState(round=1, applied=["p-1-1"])])
    assert "the owners must remove the override" in build_report_md(state, paths)
    plans = build_report_json(state, paths)
    assert "the owners must remove the override" in str(plans)
