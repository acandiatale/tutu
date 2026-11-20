"""The Korean migration report's remediation section reads as sentences (2026-11-12): no raw count keys, the message in
Korean, the English branch unchanged."""
from __future__ import annotations

import types

from sbmig.report.markdown import remediation_lines

REM = {"status": "INCOMPLETE", "reason": "manual_review", "reasons": ["manual_review", "residual_findings"],
       "counts": {"baseline_findings": 17, "fixed": 1, "final_findings": 16, "residual_inhouse": 8, "proposals_pending": 2},
       "message": "final scan incomplete: page_missing",
       "files": {"report_md": "remediation/report.md", "delivery_zip": "remediation/delivery/result.zip"}}


def test_korean_section_has_sentences_not_keys():
    ko = "\n".join(remediation_lines(types.SimpleNamespace(remediation=dict(REM)), "ko"))
    assert "- 결과: 일부만 조치되었습니다. `INCOMPLETE` (사유: 직접 확인 필요, 남은 취약점)" in ko
    assert "- 취약점: 조치 전 17건, 조치 완료 1건, 조치 후 남은 취약점 16건" in ko
    assert "라이브러리 8개" in ko
    assert "승인" not in ko                    # a count an earlier version wrote (proposals_pending) is not shown
    assert "- 상세: 조치 후 검사가 끝나지 않았습니다(사유: 결과 페이지 누락)." in ko
    # v2.1z-bg: the report under the name a person receives from the page; the other files stay run-directory paths
    assert "- 자세한 결과: `remediation-report.md`" in ko and "`remediation/delivery/result.zip`" in ko
    assert "remediation/report.md" not in ko and "아래 remediation-report.md 파일에 있습니다." in ko
    assert "baseline_findings" not in ko and "final scan incomplete" not in ko


def test_korean_section_only_counts_that_exist_and_english_unchanged():
    rem = dict(REM, counts={"baseline_findings": 3}, message="")
    ko = "\n".join(remediation_lines(types.SimpleNamespace(remediation=rem), "ko"))
    assert "- 취약점: 조치 전 3건" in ko and "승인 대기" not in ko and "상세:" not in ko
    en = "\n".join(remediation_lines(types.SimpleNamespace(remediation=dict(REM))))
    assert "- Status: `INCOMPLETE` (`manual_review`)" in en and "- Counts: baseline_findings 17" in en


def test_korean_skipped_section():
    rem = {"status": "skipped", "reason": "not ready", "missing": ["server settings.xml not configured (build.settings_file)"]}
    ko = "\n".join(remediation_lines(types.SimpleNamespace(remediation=rem), "ko"))
    assert "- 실행하지 않았습니다(사유: `not ready`)." in ko and "서버의 settings.xml이 설정되어 있지 않습니다." in ko


def test_file_findings_line_in_both_languages():
    """`remediation.file_components: report`: the file findings are counted apart and named in one line."""
    rem = dict(REM, counts={"baseline_findings": 1, "fixed": 1, "final_findings": 0, "baseline_file_findings": 2,
                            "final_file_findings": 2}, reasons=[], reason="", status="SUCCEEDED", message="")
    ko = "\n".join(remediation_lines(types.SimpleNamespace(remediation=rem), "ko"))
    assert "- 취약점: 조치 전 1건, 조치 완료 1건, 조치 후 남은 취약점 0건" in ko
    assert "- 프로젝트 파일에서 찾은 취약점 2건은 위 개수에 넣지 않고 자세한 결과에 따로 적었습니다." in ko
    en = "\n".join(remediation_lines(types.SimpleNamespace(remediation=rem)))
    assert "- File findings listed apart in the remediation report, not counted above: 2" in en
    for counts in ({"baseline_findings": 1}, {"baseline_findings": 1, "baseline_file_findings": 0}):
        rem2 = dict(rem, counts=counts)
        assert "프로젝트 파일" not in "\n".join(remediation_lines(types.SimpleNamespace(remediation=rem2), "ko"))
        assert "File findings" not in "\n".join(remediation_lines(types.SimpleNamespace(remediation=rem2)))
