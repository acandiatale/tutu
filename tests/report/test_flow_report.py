"""v2.1z-aj: the `flow=` field of the SBMIG-RESULT line (printed only for the `remediation` flow, just before
`remediation=`) and the short report.md of a remediation-only run."""
from __future__ import annotations

import pytest

from sbmig.core.models import CaseType, RunReport, utcnow
from sbmig.report.markdown import render_markdown
from sbmig.report.result_line import report_exit_code, report_result_line, result_line


def _report(**kw) -> RunReport:
    return RunReport(run_id="r-1", project_name="app", case_type=CaseType.UNKNOWN, started_at=utcnow(), **kw)


def test_flow_field_only_for_the_remediation_flow():
    assert result_line("r", 0, True) == result_line("r", 0, True, flow="full") == result_line("r", 0, True, flow=None)
    assert " flow=" not in result_line("r", 0, True, remediation="SUCCEEDED", flow="full")
    assert result_line("r", 0, True, remediation="SUCCEEDED", flow="remediation").endswith(
        " attn=- flow=remediation remediation=SUCCEEDED")


def test_full_report_line_is_unchanged():
    full = _report(success=True)
    assert report_result_line(full) == ("SBMIG-RESULT run=r-1 exit=0 build=ok unresolved=0 blocked=0 open=0 resolved=0 "
                                        "llm=0 attn=-")


def test_exit_codes_of_the_remediation_flow():
    ok = _report(flow="remediation", success=True, remediation={"status": "SUCCEEDED"})
    waiting = _report(flow="remediation", remediation={"status": "AWAITING_APPROVAL"})
    not_ready = _report(flow="remediation", flags=["remediation:not-ready", "aborted"],
                        remediation={"status": "skipped", "reason": "not_ready", "missing": ["IVAS_BASE_URL is not set"]})
    assert [report_exit_code(r) for r in (ok, waiting, not_ready)] == [0, 2, 3]
    assert report_result_line(not_ready) == ("SBMIG-RESULT run=r-1 exit=3 build=FAIL unresolved=0 blocked=0 open=0 "
                                             "resolved=0 llm=0 attn=aborted,remediation:not-ready flow=remediation "
                                             "remediation=skipped")


def test_short_report_for_a_not_ready_run():
    r = _report(flow="remediation", flags=["remediation:not-ready", "aborted"],
                manual_followups=["run aborted: vulnerability remediation not started (not ready): IVAS_BASE_URL is not set"],
                remediation={"status": "skipped", "reason": "not_ready", "dir": "remediation",
                             "missing": ["IVAS_BASE_URL is not set"]})
    en = render_markdown(r, "en")
    assert en.splitlines()[0] == "# Vulnerability remediation report — app"
    assert "- Not run: `not_ready`" in en and "IVAS_BASE_URL is not set" in en
    assert "Full remediation report" not in en and "Handover" not in en
    ko = render_markdown(r, "ko")
    assert ko.splitlines()[0] == "# 취약점 조치 보고서 — app"
    assert "- 실행하지 않았습니다(사유: `not_ready`)." in ko


def test_short_report_for_a_run_refused_before_the_remediation():
    r = _report(flow="remediation", flags=["mvn-not-found", "aborted"],
                manual_followups=["run aborted: 'mvn' is not runnable (install Maven or set build.mvn)"])
    en = render_markdown(r, "en").splitlines()
    assert en[2] == "**Not started:** 'mvn' is not runnable (install Maven or set build.mvn)"
    assert render_markdown(r, "ko").splitlines()[2].startswith("**시작하지 못했습니다.** ")


def test_no_path_line_while_the_remediation_report_does_not_exist():
    # a run waiting for approval has no remediation/report.md yet (real-Maven run 2026-11-12): the short report must not
    # point at a file that is not there
    r = _report(flow="remediation", remediation={"status": "AWAITING_APPROVAL", "reason": "pending_approval",
                                                 "dir": "remediation", "files": {}})
    assert "remediation/report.md" not in render_markdown(r, "en")
    assert "remediation/report.md" not in render_markdown(r, "ko")


def test_path_line_when_the_report_exists_and_the_section_does_not_name_it(monkeypatch):
    import sbmig.report.markdown as md

    r = _report(flow="remediation", remediation={"status": "INCOMPLETE", "reason": "manual_review", "dir": "remediation",
                                                 "files": {"report_md": "remediation/report.md"}})
    monkeypatch.setattr(md, "remediation_lines", lambda report, lang="en": ["## x", "", "- no file named here", ""])
    assert "Full remediation report: `remediation/report.md`" in render_markdown(r, "en")
    assert "자세한 조치 결과는 `remediation/report.md`에 있습니다." in render_markdown(r, "ko")


def _rem(status, reason="", **kw):
    return _report(flow="remediation", remediation={"status": status, "reason": reason, "reasons": [reason] if reason else [],
                                                    **kw})


@pytest.mark.parametrize("rem,build", [
    (_rem("SUCCEEDED", baseline_build_ok=True, delivery={"build_ok": True}), "ok"),
    (_rem("INCOMPLETE", "inhouse_artifact", baseline_build_ok=True, delivery={"build_ok": True}), "ok"),
    (_rem("AWAITING_APPROVAL", "pending_approval", baseline_build_ok=True), "ok"),
    (_rem("AWAITING_CONFIRMATION", "upload_uncertain", baseline_build_ok=True), "ok"),
    (_rem("STOPPED", "stop_requested", baseline_build_ok=True), "ok"),
    (_rem("FAILED", "resolution_mismatch", baseline_build_ok=True, delivery={"build_ok": True}), "ok"),
    (_rem("STOPPED", "stop_requested"), "FAIL"),                              # stopped before the baseline build
    (_rem("FAILED", "no_pom"), "FAIL"),                                       # the remediation's intake refused it
    (_rem("FAILED", "baseline_build_failed", baseline_build_ok=False), "FAIL"),
    (_rem("FAILED", "candidate_build_failed", baseline_build_ok=True, delivery={"build_ok": False}), "FAIL"),
    (_rem("INCOMPLETE", "residual_findings", baseline_build_ok=True, delivery={"build_ok": False}), "FAIL"),
    (_rem("skipped", "not_ready"), "FAIL"),
    (_report(flow="remediation", flags=["aborted", "unsupported-build-tool:gradle"]), "FAIL"),
])
def test_build_field_of_the_remediation_flow(rem, build):
    assert f" build={build} " in report_result_line(rem)


def test_build_field_of_a_full_run_still_follows_success():
    assert " build=FAIL " in report_result_line(_report(success=False, remediation={"status": "SUCCEEDED",
                                                                                    "baseline_build_ok": True}))
