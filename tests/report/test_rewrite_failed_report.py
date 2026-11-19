"""INTERFACES "v2.1z-ay": what both renderers say about a failed conversion and a corrected pom encoding."""
from __future__ import annotations

import pytest

from sbmig.core.models import BuildResult
from sbmig.report import render_markdown
from sbmig.report.markdown import flag_help, framework_lines
from sbmig.report.result_line import report_result_line
from sbmig.rewrite.failure import rewrite_failed_followup

from .test_framework_scope import DIRECT, _report, _use

LINE = rewrite_failed_followup(["jdk21", "jakarta"], "Failed to parse pom: Invalid UTF-8 middle byte 0xce")
LINE_KO = ("자동 변환(OpenRewrite)이 실패해 다음 변환을 적용하지 못했습니다: jdk21, jakarta. 원인: Failed to parse pom: "
           "Invalid UTF-8 middle byte 0xce. 파일은 변환되지 않았고, 빌드는 변환하지 않은 상태로 실행했습니다. 원인을 해결한 "
           "뒤 마이그레이션을 다시 실행해 주세요.")


def build(ok: bool) -> BuildResult:
    return BuildResult(ok=ok, goal="clean package", command=["mvn"], exit_code=0 if ok else 1, duration_s=1.0,
                       log_path="/run/logs/verify.log")


def reduced_failed(built: bool, name: str = "Example"):
    r = _report(_use(DIRECT, name=name), success=False, phases=[], final_build=build(built),
                manual_followups=[LINE, "other follow-up"])
    r.flags.append("rewrite-failed")
    return r


@pytest.mark.parametrize("built", [True, False])
def test_reduced_block_does_not_claim_the_conversion(built):
    r = reduced_failed(built)
    en = framework_lines(r)
    assert "Done in this run: one build" in en
    assert not any("JDK 21 and Jakarta conversion" in x and x.startswith("Done") for x in en)
    assert ("**The JDK 21 and Jakarta conversion failed: nothing was converted** (the reason is the first line of "
            "Manual follow-ups).") in en
    ko = framework_lines(r, "ko")
    assert "이번 실행에서 한 일: 빌드 1회" in ko
    assert "**JDK 21과 Jakarta 변환이 실패해 아무것도 변환되지 않았습니다.** 원인은 「확인해 주세요」의 첫 항목에 있습니다." in ko
    assert not any("JDK 21과 Jakarta 변환," in x for x in ko)


def test_reduced_build_passed_guide_head_lead_and_first_item():
    md = render_markdown(reduced_failed(True), "ko")
    assert "| **결과** | ❌ 변환 실패 (빌드는 통과) (최종 `mvn clean package`) |" in md
    assert ("빌드는 통과했지만 JDK 21과 Jakarta 자동 변환이 실패해 아무것도 변환되지 않았습니다. 그래서 이번 실행은 성공이 "
            "아닙니다. 결과 파일의 코드는 변환 전과 같습니다. 실패 원인은 「확인해 주세요」의 첫 항목에 적었습니다. 원인을 "
            "해결한 뒤 다시 실행해 주세요.") in md
    check = md[md.index("## 확인해 주세요"):]
    first = check.split("\n1. ", 1)[1].split("\n", 1)[0]
    assert first == f"**자동 변환 실패** — {LINE_KO}"
    line = report_result_line(reduced_failed(True))
    assert " exit=2 build=ok " in line and "attn=rewrite-failed" in line
    assert "rewrite-failed" in md and "변환 뒤 빌드가 통과하지 않았습니다" not in md


def test_reduced_build_failed_guide_lead_is_true():
    md = render_markdown(reduced_failed(False), "ko")
    assert "❌ 빌드 실패" in md
    assert ("JDK 21과 Jakarta 자동 변환이 실패해 아무것도 변환되지 않았고, 변환하지 않은 코드로 실행한 빌드도 통과하지 "
            "않았습니다.") in md
    assert "JDK 21과 Jakarta 변환 뒤 빌드가 통과하지 않았습니다" not in md
    assert " build=FAIL " in report_result_line(reduced_failed(False))


def test_english_head_says_not_converted():
    md = render_markdown(reduced_failed(True), "en")
    assert "| **Result** | ❌ NOT CONVERTED (the build passed) (final `mvn clean package`) |" in md
    assert LINE in md


def test_full_run_with_a_failed_conversion_keeps_its_verdict_and_says_it():
    from .test_handover import make_report
    r = make_report(success=True, final_build=build(True), manual_followups=[LINE], flags=["rewrite-failed"])
    md = render_markdown(r, "ko")
    assert "✅ 빌드 성공" in md
    assert ("자동 변환(OpenRewrite)이 실패해 Java 21, Jakarta, Spring 버전 변환 레시피가 적용되지 않았습니다. 그 뒤의 구조 "
            "정리와 빌드 오류 수정은 진행했지만, 레시피가 바꿨어야 할 코드는 바뀌지 않았을 수 있습니다.") in md
    assert " exit=0 build=ok " in report_result_line(r) and "attn=rewrite-failed" in report_result_line(r)
    assert "| **Result** | ✅ BUILD SUCCESS" in render_markdown(r, "en")


def test_flag_help_both_languages():
    assert flag_help("rewrite-failed").startswith("the automatic conversion (OpenRewrite) failed")
    assert flag_help("rewrite-failed", "ko").startswith("자동 변환(OpenRewrite)이 실패해")
    assert flag_help("pom-encoding-corrected").startswith("a pom's XML declaration named another encoding")
    assert flag_help("pom-encoding-corrected", "ko").startswith("pom 파일의 XML 선언에 적힌 인코딩과")


def test_pom_encoding_line_in_the_guide():
    from sbmig.restructure.pom_charset import PomCharsetFix, handover_line
    line = handover_line([PomCharsetFix("pom.xml", "UTF-8", "EUC-KR", "declaration", "EUC-KR")])
    from .test_handover import make_report
    r = make_report(success=True, final_build=build(True), manual_followups=[line], flags=["pom-encoding-corrected"])
    md = render_markdown(r, "ko")
    assert ("**pom 글자 인코딩** — 자동 변환 전에 pom 파일의 글자 인코딩을 바로잡았습니다. `pom.xml`(선언은 UTF-8, 실제 "
            "저장은 EUC-KR, 선언을 EUC-KR로 변경).") in md
