"""v2.1z-as: the Korean report.md is a guide for the person who asked for the migration (`sbmig/report/guide_ko.py`).

The English rendering of the same reports is compared with goldens captured from the renderer before the guide existed
(`tests/report/golden/guide_en_*.md`): the hook in `render_markdown` must not change English output by one byte."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from sbmig.core.models import (
    AdvisoryFinding,
    BlockedItem,
    BlockReason,
    BuildError,
    BuildResult,
    CaseType,
    CodeChange,
    ErrorEntry,
    ErrorKind,
    FixTry,
    HandoverSummary,
    LLMCost,
    LLMUsage,
    MigrationPlan,
    ModelCost,
    Phase,
    PhaseResult,
    PlanStep,
    ReviewIssue,
    RewriteResult,
    RunReport,
    StepKind,
    StructureReview,
    VersionChange,
)
from sbmig.report import render_markdown
from sbmig.report.handover import REASON_BOOT, REASON_CARRIED, REASON_MANDATORY, REASON_RECIPE

from .test_handover import as_is_profile

GOLDEN = Path(__file__).parent / "golden"
T0 = datetime(2026, 11, 15, 9, 37, 59, tzinfo=timezone.utc)
T1 = datetime(2026, 11, 15, 9, 38, 33, tzinfo=timezone.utc)
PARENT = "org.springframework.boot:spring-boot-starter-parent:4.1.0"
HYSTRIX = "org.springframework.cloud:spring-cloud-starter-netflix-hystrix"
R4J = "org.springframework.cloud:spring-cloud-starter-circuitbreaker-resilience4j"
CODEC = "module/comp-jar/src/main/java/com/example/ops/util/TokenCodec.java"
TESTS_FU = ("test sources were neither compiled nor run (build.skip_test_compile: true): the owning team compiles and "
            "fixes the tests (`mvn test-compile`) before relying on CI")
CODEC_DIFF = ("@@ -3,8 +3,7 @@\n import java.nio.charset.StandardCharsets;\n-import sun.misc.BASE64Encoder;\n"
              "+import java.util.Base64;\n \n public class TokenCodec {\n")
POM_DIFF = ("@@ -40,6 +40,10 @@\n     </dependency>\n+    <dependency>\n+      <groupId>org.springframework.security"
            "</groupId>\n+      <artifactId>spring-security-access</artifactId>\n+    </dependency>\n")


def hystrix(**kw) -> AdvisoryFinding:
    base = dict(advisory_id="hystrix", library="hystrix", status="eol",
                title="Netflix Hystrix is discontinued (not in any current Spring Cloud train)",
                summary="Hystrix is in maintenance-only mode since 2018.", detected_version="2.2.10.RELEASE",
                artifacts=[HYSTRIX], outcome="still-present", replacement=R4J,
                replacement_version="managed by the Spring Cloud BOM",
                followup="Configure the circuit breakers by name.",
                docs=["https://docs.example.com/r4j", "https://docs.example.com/r4j-start"],
                title_ko="Netflix Hystrix는 지원이 종료되었습니다", summary_ko="Hystrix는 지원이 종료된 라이브러리이고 Spring Boot 4에서는 지원되지 않습니다.",
                followup_ko="서킷 브레이커는 이름별로 설정해 주세요.")
    base.update(kw)
    return AdvisoryFinding(**base)


def jdk_apis(**kw) -> AdvisoryFinding:
    base = dict(advisory_id="jdk-removed-apis", library="jdk-removed-apis", status="incompatible",
                title="JDK internal APIs removed", summary="sun.misc.BASE64Encoder is gone in JDK 21.",
                outcome="replaced", replacement="java.util.Base64", followup="Review the ported JDK API calls.",
                docs=["https://openjdk.org/jeps/320"], llm_fixed_files=[CODEC])
    base.update(kw)
    return AdvisoryFinding(**base)


def carried_row() -> VersionChange:
    return VersionChange(key=HYSTRIX, before="2.2.10.RELEASE", before_source="managed", after="2.2.10.RELEASE",
                         after_source="declared", carried_over=True, reason=REASON_CARRIED,
                         pom="module/comp-jar/pom.xml")


def handover(**kw) -> HandoverSummary:
    h = hystrix()
    j = jdk_apis()
    base = dict(
        parent_after=PARENT, java_before="1.8", java_after="21", spring_boot_after="4.1.0",
        changes=[VersionChange(key="org.springframework:spring-webmvc", before="5.3.31", after=None,
                               after_source="managed", reason=REASON_BOOT),
                 VersionChange(kind="plugin", key="org.apache.maven.plugins:maven-war-plugin", before="3.4.0",
                               after="3.5.1", reason=REASON_RECIPE),
                 carried_row()],
        added=[f"parent {PARENT}", "jakarta.servlet:jakarta.servlet-api",
               f"org.springframework.boot:spring-boot-starter-actuator ({REASON_MANDATORY})"],
        removed=["javax.servlet:javax.servlet-api:4.0.1"],
        carried_over=[carried_row()],
        from_here=[f"{h.title}: {h.followup} (replacement: {R4J} managed by the Spring Cloud BOM)",
                   f"verify {j.library} → {j.replacement} (replaced during the run): {j.followup}",
                   f"code rewritten by the LLM for {j.library}: {CODEC} — review these changes (changes.patch, "
                   "Fixes by case)",
                   f"{HYSTRIX} pinned at 2.2.10.RELEASE — the new parent/Boot BOM no longer manages it: confirm the "
                   "version or replace",
                   "structure review warning — module/comp-jar/pom.xml: spring-batch-core follows the parent's Batch "
                   "line → check the Batch jobs",
                   TESTS_FU, "web.xml not migrated"])
    base.update(kw)
    return HandoverSummary(**base)


def code_changes() -> list[CodeChange]:
    return [CodeChange(path=CODEC, added=4, removed=5,
                       notes=["import 줄에서 `sun.misc.BASE64Encoder`를 `java.util.Base64`로 바꿉니다."],
                       reasons=["`sun.misc.BASE64Encoder`는 JDK 내부 API여서 JDK 21에서 쓸 수 없습니다."],
                       diff=CODEC_DIFF),
            CodeChange(path="module/comp-jar/pom.xml", added=4, removed=0, diff=POM_DIFF, truncated=True,
                       reasons=["Spring Security 7에서 접근 결정 API가 별도 모듈로 분리되었습니다."]),
            CodeChange(path="module/comp-jar/src/main/java/com/example/ops/web/OrderController.java", added=2,
                       removed=2)]


def entry(i: int, status: str = "resolved", file: str = "module/comp-jar/src/main/java/com/example/A.java",
          message: str = "cannot find symbol", **kw) -> ErrorEntry:
    return ErrorEntry(id=f"e{i}", case_id=f"case{i:06d}", l2="x", raw="x", file=file, line=10 + i, message=message,
                      seen="build", status=status, **kw)


def plan() -> MigrationPlan:
    return MigrationPlan(profile_fingerprint="f", case_type=CaseType.SPRING_LEGACY,
                         notes=["case spring_legacy (confidence 0.75)"],
                         steps=[PlanStep(id="transform.rewrite", kind=StepKind.OPENREWRITE, phase=Phase.TRANSFORM,
                                         title="OpenRewrite in one run: Java 21 + jakarta"),
                                PlanStep(id="restructure.apply", kind=StepKind.RESTRUCTURE, phase=Phase.RESTRUCTURE,
                                         title="restructure into module/comp-jar, module/boot-jar with the in-house "
                                               "parent"),
                                PlanStep(id="verify.build", kind=StepKind.BUILD, phase=Phase.VERIFY,
                                         title="final mvn clean package")])


def guide_report(**kw) -> RunReport:
    """A successful full run with every part of the guide filled."""
    phases = [PhaseResult(phase=Phase.TRANSFORM, step_ids=["transform.rewrite"],
                          rewrite_results=[RewriteResult(ok=True, recipes=[
                              "org.openrewrite.java.migrate.UpgradeToJava21",
                              "org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta",
                              "org.openrewrite.java.spring.framework.UpgradeSpringFramework_7_0"],
                              changed_files=["a", "b", "c", "d", "e"])]),
              PhaseResult(phase=Phase.RESTRUCTURE, step_ids=["restructure.apply", "restructure.review"],
                          notes=["restructure: managed-version source: effective pom of the new parent chain"]),
              PhaseResult(phase=Phase.VERIFY, step_ids=["verify.build"])]
    review = StructureReview(verdict="issues", model="gpt-x", issues=[ReviewIssue(
        severity="warning", file="module/comp-jar/pom.xml", problem="spring-batch-core follows the parent's Batch line",
        fix_hint="check the Batch jobs")])
    base = dict(run_id="run-20261115-183759-8bb06b", project_name="ops-console", case_type=CaseType.SPRING_LEGACY,
                started_at=T0, finished_at=T1, success=True, profile=as_is_profile(), plan=plan(), phases=phases,
                advisories=[hystrix(), jdk_apis()], handover=handover(), structure_review=review,
                flags=["tests-not-compiled"],
                manual_followups=[TESTS_FU, "web.xml not migrated",
                                  f"{hystrix().title}: {hystrix().followup}"],
                error_entries=[entry(1), entry(2), entry(3, status="note", message="warning: batch line")],
                code_changes=code_changes(), code_changes_total=5, code_changes_base="abc1234",
                llm_usage=LLMUsage(model="gpt-x", calls=7, input_tokens=52242, output_tokens=2609),
                llm_cost=LLMCost(total=0.0065, by_model=[ModelCost(model="gpt-x", calls=7, input_tokens=52242,
                                                                   output_tokens=2609, input_per_mtok=0.1,
                                                                   output_per_mtok=0.5, cost=0.0065)]),
                final_build=BuildResult(ok=True, goal="clean package", command=[], exit_code=0, duration_s=1,
                                        log_path="/out/run-1/logs/010-build-verify.log"),
                timings_s={"total": 34.0},
                outputs={"run_dir": "/out/run-1", "zip": "/out/run-1/result.zip", "diff": "/out/run-1/changes.patch",
                         "errors_md": "/out/run-1/errors.md", "errors": "/out/run-1/errors.jsonl",
                         "logs": "/out/run-1/logs"})
    base.update(kw)
    return RunReport(**base)


def failed_report(**kw) -> RunReport:
    """A failed build: three open errors (two files), resolved ones, one blocked item."""
    blocked = BlockedItem(id="blk-1", reason=BlockReason.EXTERNAL_JAVAX_API, phase=Phase.VERIFY, cluster_id="c",
                          files=["module/comp-jar/src/main/java/com/example/Legacy.java"], attempts=2,
                          suggestion="ask the library owner for a Jakarta build",
                          errors=[BuildError(kind=ErrorKind.COMPILE, message="incompatible types",
                                             file="module/comp-jar/src/main/java/com/example/Legacy.java", line=5)])
    pdf = "module/comp-jar/src/main/java/com/example/PdfExporter.java"
    entries = [entry(1), entry(2),
               entry(4, status="unresolved", file=pdf, message="package com.lowagie.text does not exist"),
               entry(5, status="unresolved", file=pdf, message="cannot find symbol | Document"),
               entry(6, status="open", file="module/comp-jar/src/main/java/com/example/B.java",
                     message="method does not override\nor implement a method"),
               entry(7, status="blocked", file="module/comp-jar/src/main/java/com/example/Legacy.java",
                     message="incompatible types")]
    base = dict(success=False, blocked=[blocked], error_entries=entries,
                final_build=BuildResult(ok=False, goal="clean package", command=[], exit_code=1, duration_s=1,
                                        log_path="/out/run-1/logs/011-build-verify.log"))
    base.update(kw)
    return guide_report(**base)


def aborted_report(**kw) -> RunReport:
    """A run that stopped at intake: no handover, no build."""
    base = dict(run_id="run-abort", project_name="legacy-batch", case_type=CaseType.UNKNOWN, started_at=T0,
                finished_at=T0, success=False, flags=["mvn-not-found", "aborted"],
                manual_followups=["run aborted: 'mvn' is not runnable (install Maven or set build.mvn)"])
    base.update(kw)
    return RunReport(**base)


def en_goldens() -> dict[str, RunReport]:
    """The reports whose English rendering is pinned in `tests/report/golden/`."""
    return {"success": guide_report(), "failed": failed_report(), "aborted": aborted_report()}


def ko(r: RunReport) -> str:
    return render_markdown(r, "ko")


def head_rows(md: str) -> list[str]:
    """The first-column labels of the head table."""
    lines = md.splitlines()
    i = lines.index("| | |")
    out = []
    for ln in lines[i + 2:]:
        if not ln.startswith("| "):
            break
        out.append(ln.split(" | ", 1)[0][2:])
    return out


def sec(md: str, title: str) -> str:
    """One `## ` section (title line included) up to the next `## `."""
    start = md.index(title)
    nxt = md.find("\n## ", start + 3)
    return md[start: nxt if nxt != -1 else len(md)]


def headings(md: str) -> list[str]:
    return [ln for ln in md.splitlines() if ln.startswith("## ")]


BANNED = ("케이스", "시도", "라운드", "as-is 버전으로 고정", "관리됨", "사유", "인수인계", "수행한 조치")


# ------------------------------------------------------------------------------------------------ English unchanged
def test_english_rendering_is_byte_identical_to_the_goldens():
    for name, r in en_goldens().items():
        golden = (GOLDEN / f"guide_en_{name}.md").read_text(encoding="utf-8")
        assert render_markdown(r, "en") == golden, name
        assert render_markdown(r) == golden and render_markdown(r, "fr") == golden


def test_ko_entry_is_the_guide():
    from sbmig.report.guide_ko import render_guide_ko
    r = guide_report()
    assert render_markdown(r, "ko") == render_guide_ko(r) == render_markdown(r, "KO")


# ------------------------------------------------------------------------------------------------ head table
def test_head_table_holds_exactly_six_rows_in_order():
    md = ko(guide_report())
    assert md.splitlines()[0] == "# 마이그레이션 보고서 — ops-console"
    assert head_rows(md) == ["**결과**", "SBMIG-RESULT", "전환 후", "실행 ID", "시작 / 종료", "플래그"]
    assert "| **결과** | ✅ 빌드 성공 (최종 `mvn clean package`) |" in md
    assert f"| 전환 후 | Java 21, Spring Boot 4.1.0, 부모 pom `{PARENT}` |" in md
    assert "| 시작 / 종료 | 2026-11-15 09:37:59 UTC / 2026-11-15 09:38:33 UTC (34.0초) |" in md
    assert "| 플래그 | `tests-not-compiled` |" in md
    line = next(x for x in md.splitlines() if x.startswith("| SBMIG-RESULT |"))
    assert "`SBMIG-RESULT run=run-20261115-183759-8bb06b exit=0 build=ok" in line
    head = md.split("\n## ", 1)[0]
    for gone in ("에러 목록", "유형", "전환 전", "| LLM", "LLM 비용", "차단", "javax API jar", "구조 점검", "범위"):
        assert gone not in head, gone


def test_head_table_without_flags_and_without_handover():
    md = ko(guide_report(flags=[], success=False, final_build=None))
    assert head_rows(md) == ["**결과**", "SBMIG-RESULT", "전환 후", "실행 ID", "시작 / 종료"]
    result = next(x for x in md.splitlines() if x.startswith("| **결과** |"))
    assert result.startswith("| **결과** | ❌ ") and result.endswith(" (최종 `mvn clean package`) |")
    assert head_rows(ko(aborted_report())) == ["**결과**", "SBMIG-RESULT", "실행 ID", "시작 / 종료", "플래그"]


def test_aborted_run_did_not_fail_a_build():
    md = ko(aborted_report())
    assert "| **결과** | ❌ 실행 중단 (빌드까지 진행하지 못했습니다) |" in md
    assert "mvn clean package" not in md.split("\n## ", 1)[0]
    assert "최종 빌드가 아직 통과하지 않습니다." not in md


def test_whom_the_guide_is_for():
    md = ko(guide_report())
    assert "이 문서는 마이그레이션을 요청하신 담당자께 드리는 안내입니다." in md
    assert md.index("이 문서는") < md.index("## 전환 내용")
    assert "이번 실행은 끝까지 진행하지 못했습니다." in ko(aborted_report())


# ------------------------------------------------------------------------------------------------ order / presence
def test_section_order_of_a_successful_run():
    md = ko(guide_report())
    assert headings(md) == ["## 전환 내용", "## 확인해 주세요", "## 코드 수정 내역", "## 참고"]
    assert "## 해결하지 못한 오류" not in md and "## 진행하지 않은 항목" not in md


def test_section_order_of_a_failed_run():
    md = ko(failed_report())
    assert headings(md) == ["## 전환 내용", "## 확인해 주세요", "## 코드 수정 내역", "## 해결하지 못한 오류 — 3건",
                            "## 진행하지 않은 항목", "## 참고"]


def test_no_section_is_numbered_and_old_sections_are_gone():
    md = ko(failed_report())
    for h in headings(md):
        assert not re.match(r"^## \d", h), h
    for old in ("## 에러 목록", "## 케이스별 해결", "## 구조 점검", "## 수동 후속 조치", "## 플래그", "## 변경 내역",
                "## 메모", "## 산출물", "## 인수인계", "## LLM 사용량과 비용"):
        assert old not in md, old


def test_reference_blocks_are_collapsed():
    md = ko(guide_report())
    ref = sec(md, "## 참고")
    for name in ("실행 정보", "LLM 사용량과 비용", "산출물", "진단용 기록"):
        assert f"<details><summary>{name}</summary>" in ref, name
    assert ref.count("<details>") == ref.count("</details>") == 4
    assert "- 프로젝트 유형: `spring_legacy`" in ref and "- 전환 전: Java 1.8, Spring Boot 2.7.18, pom 1개" in ref
    assert "- 실행 단계: OpenRewrite 변환 → 구조 재구성 → 구조 점검 → 최종 빌드" in ref
    assert "- OpenRewrite 레시피: `org.openrewrite.java.migrate.UpgradeToJava21`" in ref
    assert "- 구조 점검: 심각 0건, 주의 1건" in ref
    assert "- 플래그 `tests-not-compiled`: " in ref
    assert "| `gpt-x` | 7 | 52,242 | 2,609 |" in ref
    assert "- 결과 ZIP: `result.zip`" in ref and "- 오류별 상세 기록: `errors.md`, `errors.jsonl`" in ref
    assert "- 마지막 빌드 로그: `logs/010-build-verify.log`" in ref and "/out/run-1" not in ref
    assert "- plan: case spring_legacy (confidence 0.75)" in ref


# ------------------------------------------------------------------------------------------------ 전환 내용
def test_conversion_table_and_dependency_versions():
    s = sec(ko(guide_report()), "## 전환 내용")
    assert "| 항목 | 변경 전 | 변경 후 |\n|---|---|---|\n| Java | 1.8 | 21 |\n| Spring Boot | 사용하지 않음 | 4.1.0 |\n" \
           f"| 부모 pom | 없음 | `{PARENT}` |" in s
    assert "| 항목 | 변경 전 | 변경 후 | 비고 |" in s
    assert "| 의존성 `org.springframework:spring-webmvc` | 5.3.31 | 부모 pom에서 지정 | Spring Boot 4 전환 |" in s
    assert "| 플러그인 `org.apache.maven.plugins:maven-war-plugin` | 3.4.0 | 3.5.1 | OpenRewrite 레시피 |" in s
    assert (f"| ⚠ 의존성 `{HYSTRIX}` | 2.2.10.RELEASE (부모 pom에서 지정) | 2.2.10.RELEASE (pom에 직접 지정) | "
            "새 부모 pom에 버전이 없어 기존 버전을 유지 |") in s
    assert "\"부모 pom에서 지정\"은 pom에 버전을 적지 않고 부모 pom이나 BOM이 정한 버전을 쓴다는 뜻입니다." in s
    assert ("⚠ 표시한 의존성 1개는 새 부모 pom과 Spring Boot BOM에 버전이 없습니다. 빌드가 되도록 기존에 쓰던 버전을 pom에 "
            "직접 적어 두었으며, Spring Boot 4에서 동작하는지는 확인이 필요합니다.") in s
    assert ("- 추가한 의존성: `jakarta.servlet:jakarta.servlet-api`, "
            "`org.springframework.boot:spring-boot-starter-actuator` (사내 필수)") in s
    assert "- 제거한 의존성: `javax.servlet:javax.servlet-api:4.0.1`" in s
    assert "parent " not in s                                # the parent is in the top table, not in 추가한 의존성
    for word in ("관리됨", "사유", "managed", "as-is"):
        assert word not in s, word


def test_dependency_explanations_only_when_they_apply():
    h = handover(changes=[VersionChange(key="com.example:a", before="1.0", after="2.0", reason=REASON_RECIPE)],
                 carried_over=[])
    s = sec(ko(guide_report(handover=h)), "## 전환 내용")
    assert "\"부모 pom에서 지정\"은" not in s and "⚠" not in s


def test_conversion_without_any_change_is_left_out():
    from sbmig.core.models import HandoverSummary as HS
    assert "## 전환 내용" not in ko(guide_report(handover=HS()))
    assert "## 전환 내용" not in ko(guide_report(handover=None))
    same = HS(java_before="21", java_after="21", parent_before=PARENT, parent_after=PARENT)
    assert "## 전환 내용" not in ko(guide_report(handover=same))


def test_reason_codes_read_as_korean_notes():
    from sbmig.report.guide_ko import REASON_KO, _reason_ko
    assert _reason_ko(REASON_MANDATORY) == "사내 필수" and _reason_ko(REASON_BOOT) == "Spring Boot 4 전환"
    assert _reason_ko("advisory:springfox") == "`springfox` 교체 권고"
    assert _reason_ko("optional (trigger: a:b, c:d)") == "사내 선택 아티팩트로 대체(`a:b`, `c:d`)"
    assert _reason_ko("") == "-"
    for text in REASON_KO.values():
        assert "관리" not in text and "as-is" not in text and "사유" not in text


# ------------------------------------------------------------------------------------------------ 확인해 주세요
def numbered(section: str) -> list[str]:
    """The numbered items of a list section (first lines only)."""
    return [ln for ln in section.splitlines() if re.match(r"^\d+\. ", ln)]


def item_block(section: str, n: int) -> list[str]:
    """Item `n` of a numbered list: its first line and the indented lines under it."""
    lines = section.splitlines()
    i = next(k for k, ln in enumerate(lines) if ln.startswith(f"{n}. "))
    out = [lines[i]]
    for ln in lines[i + 1:]:
        if not ln.startswith(" "):
            break
        out.append(ln)
    return out


def test_check_list_advisories_first_with_sub_bullets():
    s = sec(ko(guide_report()), "## 확인해 주세요")
    first = item_block(s, 1)
    assert first[0] == ("1. **Netflix Hystrix는 지원이 종료되었습니다.** 이번 전환에서는 자동으로 바꾸지 않고 기존 버전(2.2.10.RELEASE)을 그대로 두었습니다. "
                        "아래 「대체할 라이브러리」로 바꾸고 「할 일」을 확인해 주세요.")
    assert first[1:] == [f"   - 대체할 라이브러리: `{R4J}`",
                         "   - 설명: Hystrix는 지원이 종료된 라이브러리이고 Spring Boot 4에서는 지원되지 않습니다.",
                         "   - 할 일: 서킷 브레이커는 이름별로 설정해 주세요.",
                         "   - 문서: [1](https://docs.example.com/r4j), [2](https://docs.example.com/r4j-start)"]
    # no Korean texts: the English title is the lead, the English texts stand under the labels
    second = item_block(s, 2)
    assert second[0] == ("2. **JDK internal APIs removed.** 이번 전환에서 교체했습니다. 아래 「할 일」대로 동작을 확인해 "
                         "주세요.")
    assert second[1:] == ["   - 교체한 라이브러리: `java.util.Base64`",
                          "   - 설명: sun.misc.BASE64Encoder is gone in JDK 21.",
                          "   - 할 일: Review the ported JDK API calls.",
                          f"   - 수정한 파일: `{CODEC}` (아래 「코드 수정 내역」)",
                          "   - 문서: [1](https://openjdk.org/jeps/320)"]


def test_advisory_lead_by_outcome():
    from sbmig.report.guide_ko import advisory_item
    title = "**Netflix Hystrix는 지원이 종료되었습니다.**"
    kept = "이번 전환에서는 자동으로 바꾸지 않고 기존 버전(2.2.10.RELEASE)을 그대로 두었습니다."
    swap = "아래 「대체할 라이브러리」로 바꾸고 「할 일」을 확인해 주세요."
    assert advisory_item(hystrix()).split("\n")[0] == f"{title} {kept} {swap}"
    # no replacement named: only the to-do; no to-do either: nothing to point at
    assert advisory_item(hystrix(replacement=None)).split("\n")[0] == f"{title} {kept} 아래 「할 일」을 확인해 주세요."
    assert advisory_item(hystrix(followup="", followup_ko="")).split("\n")[0] == (
        f"{title} {kept} 아래 「대체할 라이브러리」로 바꿔 주세요.")
    assert advisory_item(hystrix(outcome="replaced")).split("\n")[0] == (
        f"{title} 이번 전환에서 교체했습니다. 아래 「할 일」대로 동작을 확인해 주세요.")
    assert advisory_item(hystrix(outcome="unknown")).split("\n")[0] == (
        f"{title} 전환 후에도 남아 있는지는 확인하지 못했습니다. {kept} {swap}")
    assert advisory_item(hystrix(detected_version=None)).split("\n")[0] == (
        f"{title} 이번 전환에서는 자동으로 바꾸지 않았습니다. {swap}")
    replaced = advisory_item(hystrix(outcome="replaced", replacement_ko="사내 대체 모듈"))
    assert "\n- 교체한 라이브러리: 사내 대체 모듈\n" in replaced and "그대로 두었습니다" not in replaced


def test_advisory_lead_is_the_title_sentence():
    from sbmig.report.guide_ko import advisory_item
    # a title ending in a parenthesis still ends as a sentence
    other = advisory_item(jdk_apis(title_ko="JDK 내부 API를 사용합니다(sun.misc)"))
    assert other.startswith("**JDK 내부 API를 사용합니다(sun.misc).** 이번 전환에서 교체했습니다.")
    # no Korean title: the English one; no title at all: the library name
    assert advisory_item(hystrix(title_ko="")).startswith(
        "**Netflix Hystrix is discontinued (not in any current Spring Cloud train).** ")
    assert advisory_item(hystrix(title_ko="", title="")).startswith("**hystrix.** ")
    # nothing in the lead is guessed from the texts: no case id, no library id in backticks
    assert "`hystrix`" not in advisory_item(hystrix()) and "`jdk-removed-apis`" not in other


def test_advisory_sub_bullets_without_content_are_left_out():
    from sbmig.report.guide_ko import advisory_item
    bare = advisory_item(hystrix(replacement=None, replacement_version=None, summary="", summary_ko="",
                                 followup="", followup_ko="", docs=[]))
    # no 할 일 below: the lead does not point at one
    assert bare == "**Netflix Hystrix는 지원이 종료되었습니다.** 이번 전환에서는 자동으로 바꾸지 않고 기존 버전(2.2.10.RELEASE)을 그대로 두었습니다."
    done = advisory_item(hystrix(outcome="replaced", replacement=None, summary="", summary_ko="", followup="",
                                 followup_ko="", docs=[]))
    assert done == "**Netflix Hystrix는 지원이 종료되었습니다.** 이번 전환에서 교체했습니다. 동작을 확인해 주세요."
    only_docs = advisory_item(hystrix(replacement=None, summary="", summary_ko="", title="", followup="",
                                      followup_ko=""))
    assert only_docs.split("\n")[1:] == ["- 문서: [1](https://docs.example.com/r4j), [2](https://docs.example.com/r4j-start)"]


def test_numbering_continues_after_items_with_sub_bullets():
    markdown_it = __import__("pytest").importorskip("markdown_it")
    s = sec(ko(guide_report()), "## 확인해 주세요")
    nums = [int(x.split(".", 1)[0]) for x in numbered(s)]
    assert nums == list(range(1, len(nums) + 1)) and len(nums) == 5
    tokens = markdown_it.MarkdownIt("commonmark").parse(s)
    assert [t.type for t in tokens].count("ordered_list_open") == 1
    top = [t for t in tokens if t.type == "list_item_open" and t.level == 1]
    assert [t.info for t in top] == [str(n) for n in nums]
    assert [t.type for t in tokens].count("bullet_list_open") == 2      # the two advisories


def test_sub_bullets_follow_the_width_of_a_two_digit_number():
    from sbmig.core.models import HandoverSummary as HS
    lines = [f"web.xml line {i} not migrated" for i in range(9)]
    h = HS(from_here=lines, merge_conflicts=["1 identical duplicate(s) dropped (same content in every module): a.xml"])
    s = sec(ko(guide_report(handover=h, manual_followups=[])), "## 확인해 주세요")
    block = item_block(s, 12)
    assert block[0].startswith("12. **모듈 병합에서 겹친 파일**") and len(block) == 2 and block[1].startswith("    - ")
    markdown_it = __import__("pytest").importorskip("markdown_it")
    tokens = markdown_it.MarkdownIt("commonmark").parse(s)
    assert [t.type for t in tokens].count("ordered_list_open") == 1
    assert [t.info for t in tokens if t.type == "list_item_open" and t.level == 1] == [str(n) for n in range(1, 13)]


def test_llm_fixed_paths_map_to_the_current_path():
    from sbmig.report.guide_ko import current_path
    changed = [CODEC, "module/comp-jar/pom.xml",
               "module/a/src/main/java/com/example/Twin.java", "module/b/src/main/java/com/example/Twin.java"]
    assert current_path(CODEC, changed) == CODEC
    assert current_path("src/main/java/com/example/ops/util/TokenCodec.java", changed) == CODEC     # one match
    assert current_path("src/main/java/com/example/Twin.java", changed) == "src/main/java/com/example/Twin.java"
    assert current_path("src/main/java/com/other/TokenCodec.java", changed) == "src/main/java/com/other/TokenCodec.java"
    assert current_path("x/Gone.java", changed) == "x/Gone.java"
    j = jdk_apis(llm_fixed_files=["src/main/java/com/example/ops/util/TokenCodec.java"])
    s = sec(ko(guide_report(advisories=[j])), "## 확인해 주세요")
    assert f"   - 수정한 파일: `{CODEC}` (아래 「코드 수정 내역」)" in s


def test_check_list_says_nothing_twice():
    md = ko(guide_report())
    s = sec(md, "## 확인해 주세요")
    assert s.count("mvn test-compile") == 1                        # from_here and manual_followups both carry it
    assert s.count("web.xml not migrated") == 1
    assert "Netflix Hystrix is discontinued" not in s              # the advisory's own from_here / follow-up lines
    assert "verify jdk-removed-apis" not in s and "code rewritten by the LLM" not in s
    assert "pinned at" not in s and "고정" not in s                 # the carried hystrix line: said by the advisory
    items = numbered(s)
    assert [int(x.split(".", 1)[0]) for x in items] == list(range(1, len(items) + 1)) and len(items) == 5


def test_other_items_get_a_bold_lead():
    s = sec(ko(guide_report()), "## 확인해 주세요")
    items = numbered(s)
    # the structure review warning, written natively from the stored issue: no arrow, each part a sentence
    assert items[2] == ("3. **구조 점검에서 나온 주의 사항** — `module/comp-jar/pom.xml`: spring-batch-core follows the "
                        "parent's Batch line. check the Batch jobs.")
    assert items[3].startswith("4. **테스트** — ")
    assert items[4] == "5. web.xml not migrated"                  # a line nobody can classify stays as it is


def test_review_lines_without_a_stored_issue_and_for_the_whole_project():
    from sbmig.report.guide_ko import _review_item
    r = guide_report(structure_review=None)
    assert _review_item("structure review warning — a/pom.xml: duplicate entry → remove one", r) == \
        "**구조 점검에서 나온 주의 사항** — `a/pom.xml`: duplicate entry. remove one."
    assert _review_item("structure review warning — (project): modules overlap", r) == \
        "**구조 점검에서 나온 주의 사항** — modules overlap."
    assert _review_item("structure review: critical issue not fixed — b/pom.xml: parent missing → set it", r) is None
    assert _review_item("web.xml not migrated", r) is None


def test_classified_lines_get_their_lead():
    from sbmig.report.guide_ko import _lead_line
    cases = {"test sources do not compile (build passes only with -Dmaven.test.skip=true): A.java:3 x": "테스트",
             "test fails at runtime (packaged with -DskipTests): FooTest — boom": "실행할 때 실패하는 테스트",
             "2 externalized setting(s) need values before the first start — x; see 'Properties to set'": "외부 설정 값",
             "bundled jar `crypto.jar` is referenced by no source / JSP / XML / properties file": "프로젝트에 포함된 jar",
             "JSP javax references remain (3 files) — rename": "JSP에 남은 javax 이름",
             "structure review: critical issue not fixed — b/pom.xml: parent missing → set it":
                 "구조 점검에서 고치지 못한 문제"}
    for line, lead in cases.items():
        assert _lead_line(line, 800).startswith(f"**{lead}** — "), line
    assert _lead_line("something new", 800) == "something new"


def test_carried_pin_without_advisory_is_one_item():
    other = VersionChange(key="com.example:legacy-util", before="1.4", before_source="managed", after="1.4",
                          carried_over=True, reason=REASON_CARRIED)
    h = handover(changes=[other], carried_over=[other],
                 from_here=["com.example:legacy-util pinned at 1.4 — the new parent/Boot BOM no longer manages it: "
                            "confirm the version or replace"])
    s = sec(ko(guide_report(handover=h, advisories=[], manual_followups=[])), "## 확인해 주세요")
    assert ("1. **기존 버전을 유지한 의존성** — `com.example:legacy-util`. 새 부모 pom에 버전이 없어 기존 버전을 pom에 "
            "직접 적었습니다.") in s
    assert "pinned at" not in s


def test_pointers_become_korean_items():
    h = handover(from_here=["2 cluster(s) blocked — see Blocked", "3 unresolved error(s) — see Error list",
                            "1 javax API jar(s) still on the classpath — see that section",
                            "… and 4 more — see Manual follow-ups / Blocked / Error list"])
    s = sec(ko(guide_report(handover=h, advisories=[], manual_followups=[])), "## 확인해 주세요")
    assert "1. **진행하지 않은 항목 2건** — " in s and "2. **해결하지 못한 오류 3건** — 최종 빌드에 남아 있습니다." in s
    assert "3. **javax API jar 1개** — " in s
    assert "4. " not in s and "see " not in s


def test_nothing_to_check_says_so():
    from sbmig.core.models import HandoverSummary as HS
    s = sec(ko(guide_report(handover=HS(), advisories=[], manual_followups=[])), "## 확인해 주세요")
    assert "따로 확인하실 사항은 없습니다." in s and "1. " not in s


def test_merge_conflicts_are_one_item_with_sub_lines():
    h = handover(merge_conflicts=["1 identical duplicate(s) dropped (same content in every module): a.xml"])
    s = sec(ko(guide_report(handover=h)), "## 확인해 주세요")
    block = item_block(s, next(int(x.split(".")[0]) for x in numbered(s) if "**모듈 병합에서 겹친 파일**" in x))
    assert len(block) == 2 and block[1].startswith("   - ")


# ------------------------------------------------------------------------------------------------ 코드 수정 내역
def test_automatic_conversion_lines():
    s = sec(ko(guide_report()), "## 코드 수정 내역")
    auto = s.split("### 자동 변환", 1)[1].split("###", 1)[0]
    assert "- OpenRewrite 레시피로 파일 5개를 변환했습니다: Java 21, javax → jakarta, Spring Framework 7." in auto
    assert "- 프로젝트를 `module/comp-jar`, `module/boot-jar` 구조로 재구성하고 사내 부모 pom을 적용했습니다." in auto
    assert "`changes.patch`" in auto and "```" not in auto


def test_recipe_words():
    from sbmig.report.guide_ko import recipe_words
    ids = ["org.openrewrite.java.migrate.UpgradeToJava21", "x.UpgradeSpringSecurity_6_0", "x.UpgradeSpringSecurity_7_0",
           "x.SpringBatch5To6Migration", "x.UpgradeSpringBoot_4_0", "x.SpringBootProperties_4_1", "x.Custom"]
    assert recipe_words(ids) == ["Java 21", "Spring Security 7", "Spring Batch 6", "Spring Boot 4",
                                 "Spring Boot 4.1 설정 키", "`Custom`"]


def test_changed_files_with_notes_reasons_and_diff():
    s = sec(ko(guide_report()), "## 코드 수정 내역")
    assert "### 직접 수정한 파일 — 5개" in s
    codec = s.split(f"#### `{CODEC}` (+4 −5)", 1)[1].split("####", 1)[0]
    assert "- import 줄에서 `sun.misc.BASE64Encoder`를 `java.util.Base64`로 바꿉니다." in codec
    assert "수정 이유: `sun.misc.BASE64Encoder`는 JDK 내부 API여서 JDK 21에서 쓸 수 없습니다." in codec
    assert "```diff\n" + CODEC_DIFF.rstrip("\n") + "\n```" in codec
    pom = s.split("#### `module/comp-jar/pom.xml` (+4 −0)", 1)[1].split("####", 1)[0]
    assert "diff가 길어 앞부분만 실었습니다." in pom and "diff가 길어" not in codec
    # the file without a diff, and two more files beyond the stored list
    assert "그 밖에 수정한 파일 3개는 diff를 싣지 않았습니다. `changes.patch`에서 볼 수 있습니다." in s
    assert "| 파일 | 변경 |\n|---|---|\n| `module/comp-jar/src/main/java/com/example/ops/web/OrderController.java` | +2 −2 |" in s
    assert "표에 없는 파일 2개도 `changes.patch`에 있습니다." in s
    for word in ("케이스", "case", "round", "라운드", "try", "시도", "r1"):
        assert word not in s, word


def test_diff_with_backticks_gets_a_longer_fence():
    body = "@@ -1 +1 @@\n-/** ```java x``` */\n+/** ````y```` */\n"
    md = ko(guide_report(code_changes=[CodeChange(path="src/A.java", added=1, removed=1, diff=body)],
                         code_changes_total=1))
    assert "`````diff\n" + body.rstrip("\n") + "\n`````" in md


def test_changed_files_without_any_diff_and_added_deleted():
    changes = [CodeChange(path="src/New.java", status="added", added=12),
               CodeChange(path="src/Old.java", status="deleted", removed=7)]
    s = sec(ko(guide_report(code_changes=changes, code_changes_total=2)), "## 코드 수정 내역")
    assert "### 직접 수정한 파일 — 2개" in s and "수정한 파일 2개는 diff를 싣지 않았습니다." in s and "####" not in s
    assert "| `src/New.java` | 새 파일, +12 −0 |" in s and "| `src/Old.java` | 삭제, +0 −7 |" in s


def test_no_code_changes_leaves_the_subsection_out():
    md = ko(guide_report(code_changes=[], code_changes_total=0))
    s = sec(md, "## 코드 수정 내역")
    assert "### 자동 변환" in s and "직접 수정한 파일" not in s
    bare = ko(guide_report(code_changes=[], code_changes_total=0, phases=[]))
    assert "## 코드 수정 내역" not in bare


# ------------------------------------------------------------------------------------------------ 오류 / 진행하지 않은 항목
def test_open_errors_only_grouped_by_file_and_escaped():
    md = ko(failed_report())
    s = sec(md, "## 해결하지 못한 오류")
    assert s.startswith("## 해결하지 못한 오류 — 3건")
    assert "`errors.md`" in s and "`logs/011-build-verify.log`" in s
    rows = [ln for ln in s.splitlines() if ln.startswith("| `")]
    assert rows == ["| `module/comp-jar/src/main/java/com/example/PdfExporter.java` | 14 | "
                    "package com.lowagie.text does not exist |",
                    "| `module/comp-jar/src/main/java/com/example/PdfExporter.java` | 15 | cannot find symbol \\| Document |",
                    "| `module/comp-jar/src/main/java/com/example/B.java` | 16 | method does not override or implement a "
                    "method |"]
    # resolved / note / blocked entries are not listed anywhere in the guide
    assert "A.java" not in md and "warning: batch line" not in md
    assert "Legacy.java` | 17" not in md


def test_blocked_section():
    s = sec(ko(failed_report()), "## 진행하지 않은 항목")
    assert "손대지 않은 항목이 1건 있습니다." in s and "`external_javax_api` (verify)" in s
    assert "<details><summary><code>blk-1</code> 관련 오류 1건</summary>" in s


def test_failed_build_says_so_first():
    md = ko(failed_report())
    under = md.split("| 플래그 |", 1)[1].split("\n\n", 2)[1]
    assert under == "최종 빌드가 아직 통과하지 않습니다. 아래 「해결하지 못한 오류」와 「진행하지 않은 항목」을 먼저 봐 주세요."
    assert md.index("최종 빌드가 아직") < md.index("이 문서는") < md.index("## 전환 내용")
    only_errors = ko(failed_report(blocked=[]))
    assert "아래 「해결하지 못한 오류」를 먼저 봐 주세요." in only_errors
    only_blocked = ko(failed_report(error_entries=[entry(1)]))
    assert "아래 「진행하지 않은 항목」을 먼저 봐 주세요." in only_blocked
    neither = ko(failed_report(error_entries=[], blocked=[]))
    assert "최종 빌드가 아직 통과하지 않습니다. 「참고」의 마지막 빌드 로그를 먼저 봐 주세요." in neither
    assert "최종 빌드가 아직" not in ko(guide_report())


def test_failed_build_puts_errors_and_blocked_items_first():
    fh = ["1 cluster(s) blocked — see Blocked", "3 unresolved error(s) — see Error list"]
    h = handover()
    h.from_here = h.from_here[:4] + fh + h.from_here[4:]
    items = numbered(sec(ko(failed_report(handover=h)), "## 확인해 주세요"))
    assert items[0].startswith("1. **해결하지 못한 오류 3건** — ")
    assert items[1].startswith("2. **진행하지 않은 항목 1건** — ")
    assert items[2].startswith("3. **Netflix Hystrix는 지원이 종료되었습니다.** ")
    # without a handover the pointers are made from the report and still come first
    items = numbered(sec(ko(failed_report(handover=None)), "## 확인해 주세요"))
    assert items[0].startswith("1. **해결하지 못한 오류 3건** — ") and items[1].startswith("2. **진행하지 않은 항목 1건** — ")
    # a successful run keeps the advisories first
    ok = numbered(sec(ko(guide_report(handover=handover(from_here=fh + list(h.from_here)))), "## 확인해 주세요"))
    assert ok[0].startswith("1. **Netflix Hystrix는 지원이 종료되었습니다.** ")


# ------------------------------------------------------------------------------------------------ wording
def test_banned_words_do_not_occur_in_a_normal_run():
    md = ko(guide_report())
    for word in BANNED:
        assert word not in md, word
    assert not re.search(r"할 것[.)]?$", md, re.M)


def test_the_guide_writes_its_own_sentences_politely():
    from sbmig.report import guide_ko
    import ast
    tree = ast.parse(Path(guide_ko.__file__).read_text(encoding="utf-8"))
    docs = {id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and n.body
            and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    literals = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and id(n) not in docs]
    for word in BANNED:
        assert not [x for x in literals if word in x], word
    assert not [x for x in literals if x.rstrip().endswith(("할 것", "할 것."))]


# ------------------------------------------------------------------------------------------------ other paths
def test_aborted_run_keeps_the_reason_visible():
    md = ko(aborted_report())
    assert headings(md) == ["## 확인해 주세요", "## 참고"]
    s = sec(md, "## 확인해 주세요")
    assert "다시 실행하기 전에 확인하실 사항입니다." in s
    assert "1. **실행 중단** — " in s and "mvn" in s
    assert "- 플래그 `mvn-not-found`" in md


def test_ant_refusal_notice_stays_under_the_title_once():
    notice = "Maven 변환 후 신청 부탁드립니다."
    detail = ("Ant build file build.xml found and no pom.xml: the Ant → Maven conversion is switched off "
              "(intake.ant_to_maven: false)")
    r = aborted_report(flags=["unsupported-build-tool:ant", "aborted"],
                       manual_followups=[f"run aborted: {notice}", detail])
    lines = ko(r).splitlines()
    assert lines[2] == f"**{notice}**" and lines[4].startswith("_Ant 빌드 파일(build.xml)만 있고")
    assert "\n".join(lines).count(notice) == 1
    assert "1. " not in sec("\n".join(lines), "## 확인해 주세요")


def test_framework_scope_run():
    from .test_framework_scope import DIRECT, _report, _use
    r = _report(_use(DIRECT))
    md = ko(r)
    # v2.1z-aw: the framework block comes right under the head table, before the purpose sentence
    assert md.index("| 시작 / 종료 |") < md.index("## 사내 프레임워크 프로젝트") < md.index("이 문서는") \
        < md.index("## 확인해 주세요")
    assert "| 범위 |" not in md and "- 범위: JDK 21과 jakarta만 적용(사내 프레임워크 프로젝트)" in sec(md, "## 참고")


def test_remediation_flow_keeps_its_short_report():
    r = guide_report(flow="remediation", remediation={"status": "SUCCEEDED", "files": {"report_md": "remediation/report.md"}})
    md = ko(r)
    assert md.splitlines()[0] == "# 취약점 조치 보고서 — ops-console" and "## 전환 내용" not in md


def test_chained_remediation_section_comes_before_the_reference():
    md = ko(guide_report(remediation={"status": "SUCCEEDED"}))
    assert md.index("## 코드 수정 내역") < md.index("## 취약점 조치") < md.index("## 참고")


def test_minimal_report_never_raises():
    bare = RunReport(run_id="r", project_name="p", case_type=CaseType.UNKNOWN, started_at=T0)
    md = ko(bare)
    assert md.startswith("# 마이그레이션 보고서 — p") and "따로 확인하실 사항은 없습니다." in md
    assert "| 시작 / 종료 | 2026-11-15 09:37:59 UTC / - (-) |" in md
