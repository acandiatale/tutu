"""guide_ko: an aborted run says "빌드까지 진행하지 못했습니다" only when no build ran (review finding R2).

An aborted run that did build (http-blocked in verify, parent-not-found in the fix loop, an internal error after a
build) says that it was stopped and what the last build's result was."""
from __future__ import annotations

from sbmig.core.models import BuildResult

from .test_guide_ko import aborted_report, head_rows, ko


def _build(ok: bool) -> BuildResult:
    return BuildResult(ok=ok, goal="clean package", command=[], exit_code=0 if ok else 1, duration_s=1,
                       log_path="/out/run-1/logs/011-build-verify.log")


def _result(md: str) -> str:
    return next(x for x in md.splitlines() if x.startswith("| **결과** |"))


def test_aborted_after_a_failed_build_names_the_last_build():
    md = ko(aborted_report(flags=["infra:http-blocked", "aborted:http-blocked"], final_build=_build(False)))
    assert _result(md) == "| **결과** | ❌ 실행 중단 (마지막 빌드는 통과하지 않았습니다) |"
    assert "빌드까지 진행하지 못했습니다" not in md
    assert "이번 실행은 끝까지 진행하지 못했습니다." in md
    assert "마지막 빌드는 통과하지 않았습니다. 마지막 빌드 로그는 「참고」에 있습니다." in md
    assert "`/out/run-1/logs/011-build-verify.log`" in md.split("## 참고", 1)[1]


def test_aborted_after_a_passing_build_says_so():
    md = ko(aborted_report(flags=["aborted"], final_build=_build(True)))
    assert _result(md) == "| **결과** | ❌ 실행 중단 (마지막 빌드는 통과했습니다) |"
    assert "마지막 빌드는 통과했습니다. 마지막 빌드 로그는 「참고」에 있습니다." in md
    assert "최종 빌드가 아직 통과하지 않습니다." not in md


def test_aborted_without_a_build_is_unchanged():
    md = ko(aborted_report())
    assert _result(md) == "| **결과** | ❌ 실행 중단 (빌드까지 진행하지 못했습니다) |"
    assert "마지막 빌드는" not in md
    assert head_rows(md) == ["**결과**", "SBMIG-RESULT", "실행 ID", "시작 / 종료", "플래그"]
