"""v2.1z-ak: a framework project's reduced run (scope jdk-jakarta) in the result line, report.md and the abort reasons."""
from __future__ import annotations

import pytest

from sbmig.core.models import FrameworkEvidence, FrameworkUse, MigrationPlan, PhaseResult, Phase
from sbmig.report import render_markdown
from sbmig.report.i18n import tr_line
from sbmig.report.markdown import FRAMEWORK_FULL_TITLE, FRAMEWORK_TITLE, framework_lines, flag_help
from sbmig.report.result_line import report_result_line, result_line, run_scope

from .test_abort_reasons_ko import DETACHED
from .test_handover import as_is_profile, make_report

FW = "com.example.fw:example-fw-core"
DIRECT = FrameworkEvidence(source="direct", artifact=FW, version="1.2.0", where="pom.xml")
PARENT = FrameworkEvidence(source="parent", artifact=FW, version="1.2.0", where="com.example:shop-web",
                           parent="com.example.fw:example-fw-parent:1.2.0")
BUNDLED = FrameworkEvidence(source="bundled", artifact="example-fw-core-1.2.0.jar",
                            where="src/main/webapp/WEB-INF/lib/example-fw-core-1.2.0.jar")
TRANSITIVE = FrameworkEvidence(source="transitive", artifact=FW, version="1.2.0", where="com.example:shop-web",
                               via=["com.example.lib:shared-lib:3.0", f"{FW}:1.2.0"])


def _use(*evidence, scope="jdk-jakarta", unchecked=(), reason=""):
    return FrameworkUse(patterns=["example-fw-core"], evidence=list(evidence), unchecked=list(unchecked), scope=scope,
                        reason=reason or f"framework artifact {FW} found → scope {scope}")


def _report(use, *, scope="jdk-jakarta", **kw):
    prof = as_is_profile()
    prof.framework = use
    plan = MigrationPlan(profile_fingerprint="f", case_type=prof.case_type, steps=[], scope=scope)
    flags = ["tests-not-compiled"] + (["framework-project"] if use is not None and use.found else [])
    return make_report(profile=prof, plan=plan, flags=flags, **kw)


# ---------------------------------------------------------------------------------------------- result line
def test_result_line_token_position():
    line = result_line("r", 0, True, major=2, scope="jdk-jakarta", flow="remediation", remediation="SUCCEEDED")
    assert line.endswith(" major=2 scope=jdk-jakarta flow=remediation remediation=SUCCEEDED")
    assert " scope=" not in result_line("r", 0, True, scope="full")
    assert " scope=" not in result_line("r", 0, True)
    assert result_line("r", 0, True, scope="jdk-jakarta").endswith("attn=- scope=jdk-jakarta")


def test_report_result_line_reduced_and_full():
    reduced = _report(_use(DIRECT), remediation={"status": "SUCCEEDED"})
    line = report_result_line(reduced, 0)
    assert " scope=jdk-jakarta remediation=SUCCEEDED" in line and line.isascii()
    full = _report(_use(DIRECT, scope="full", reason="framework.scope=full → full migration"), scope="full")
    assert " scope=" not in report_result_line(full, 0)
    assert " scope=" not in report_result_line(make_report(), 0)


def test_scope_before_the_plan_comes_from_the_profile():
    r = _report(_use(DIRECT))
    r.plan = None
    assert run_scope(r) == "jdk-jakarta"
    r.profile.framework = None
    assert run_scope(r) is None


def test_framework_project_is_not_an_attention_flag():
    line = report_result_line(_report(_use(DIRECT)), 0)
    assert "framework" not in line.split(" attn=", 1)[1].split()[0]


# ---------------------------------------------------------------------------------------------- report.md
def test_reduced_block_english():
    r = _report(_use(DIRECT, PARENT, BUNDLED, TRANSITIVE, unchecked=["parent: no as-is effective pom"]),
                remediation={"status": "SUCCEEDED"})
    md = render_markdown(r, "en")
    assert f"## {FRAMEWORK_TITLE}" in md
    assert md.index("| SBMIG-RESULT |") < md.index(f"## {FRAMEWORK_TITLE}") < md.index("## Handover")
    assert "| Scope | JDK 21 and jakarta only (in-house framework project) |" in md
    assert f"- `{FW}` 1.2.0: declared directly in `pom.xml`" in md
    assert (f"- `{FW}` 1.2.0: inherited from the parent pom `com.example.fw:example-fw-parent:1.2.0` "
            "(module `com.example:shop-web`)") in md
    assert "- `example-fw-core-1.2.0.jar`: jar in the tree (`src/main/webapp/WEB-INF/lib/example-fw-core-1.2.0.jar`)" in md
    assert (f"- `{FW}` 1.2.0: brought by another library, path `com.example.lib:shared-lib:3.0 > {FW}:1.2.0` "
            "(module `com.example:shop-web`)") in md
    assert "Done in this run: JDK 21 and jakarta, build error fixes, vulnerability remediation" in md
    assert "Not done: restructure, structure review" in md
    assert "Not checked: parent: no as-is effective pom" in md
    assert "- `framework-project`: the project uses an in-house framework artifact" in md
    assert "## Structure review" not in md and "| Structure review |" not in md


def test_reduced_block_korean():
    r = _report(_use(DIRECT, PARENT, BUNDLED, TRANSITIVE, unchecked=["transitive: dependency:tree failed"]))
    md = render_markdown(r, "ko")
    lines = framework_lines(r, "ko")
    assert lines[0] == "## 사내 프레임워크 프로젝트"
    # v2.1z-as: the scope row moved from the head table to 참고 / 실행 정보; the framework block stays on top
    assert "- 범위: JDK 21과 jakarta만 적용(사내 프레임워크 프로젝트)" in md and "| 범위 |" not in md
    assert "\n".join(lines) in md and md.index("## 사내 프레임워크 프로젝트") < md.index("## 참고")
    assert f"- `{FW}` 1.2.0: `pom.xml`에 직접 선언" in lines
    assert (f"- `{FW}` 1.2.0: 부모 pom `com.example.fw:example-fw-parent:1.2.0`에서 상속(모듈 `com.example:shop-web`)"
            in lines)
    assert "- `example-fw-core-1.2.0.jar`: 프로젝트 안의 jar 파일(`src/main/webapp/WEB-INF/lib/example-fw-core-1.2.0.jar`)" in lines
    assert any(x.startswith(f"- `{FW}` 1.2.0: 다른 라이브러리의 의존성(경로 `com.example.lib:shared-lib:3.0 > ")
               for x in lines)
    # the remediation did not run: not claimed
    assert "이번 실행에서 한 일: JDK 21과 jakarta 적용, 빌드 오류 수정" in lines
    assert ("하지 않은 일: 구조 재구성, 구조 점검. 사내 프레임워크 프로젝트는 이 두 단계를 실행하지 않습니다. 부모 pom과 "
            "프레임워크 버전은 그대로 두었습니다.") in lines
    assert "확인하지 못한 곳: transitive: dependency:tree failed" in lines
    text = "\n".join(lines)
    assert "—" not in text and "→" not in text
    assert not DETACHED.search(text.replace("`", "")), text


def test_parent_without_coordinates():
    e = PARENT.model_copy(update={"parent": None})
    assert f"- `{FW}` 1.2.0: inherited from a parent pom (module `com.example:shop-web`)" in framework_lines(_report(_use(e)))
    assert f"- `{FW}` 1.2.0: 부모 pom에서 상속(모듈 `com.example:shop-web`)" in framework_lines(_report(_use(e)), "ko")


@pytest.mark.parametrize("reason,en,ko", [
    ("framework.scope=full → full migration",
     "The full migration ran because the configuration says so (framework.scope: full).",
     "설정(framework.scope: full)에 따라 전체 마이그레이션을 진행했습니다."),
    (f"framework artifact {FW} found only as a transitive dependency and framework.transitive=note → full migration",
     "The artifact arrives only through another library: the full migration ran because the configuration says so "
     "(framework.transitive: note).",
     "다른 라이브러리의 의존성으로만 포함된 아티팩트여서 설정(framework.transitive: note)에 따라 전체 마이그레이션을 진행했습니다."),
])
def test_found_but_full(reason, en, ko):
    r = _report(_use(TRANSITIVE, scope="full", reason=reason), scope="full")
    lines = framework_lines(r)
    assert lines[0] == f"## {FRAMEWORK_FULL_TITLE}" and en in lines
    assert "Not done: restructure" not in "\n".join(lines)
    assert framework_lines(r, "ko")[0] == "## 사내 프레임워크" and ko in framework_lines(r, "ko")
    assert "| Scope |" not in render_markdown(r, "en")


def test_full_run_is_byte_identical_without_a_finding():
    base = make_report()
    for use in (None, _use(scope="full", reason="", unchecked=["parent: no as-is effective pom"])):
        r = _report(use, scope="full")
        ref = _report(None, scope="full")
        for lang in ("en", "ko"):
            assert render_markdown(r, lang) == render_markdown(ref, lang)
    assert framework_lines(base) == []


def test_reduced_jsp_rename_is_not_credited_to_the_restructure():
    note = "transform.jsp_jakarta: JSP javax → jakarta: 3 files, 7 references"
    r = _report(_use(DIRECT))
    r.phases = [PhaseResult(phase=Phase.TRANSFORM, notes=[note])]
    from sbmig.report.markdown import jsp_jakarta_lines
    lines = jsp_jakarta_lines(r)
    assert "by the restructure" not in lines[0] and "renamed by sbmig in JSP / tag files" in lines[0]
    assert "sbmig가 JSP / 태그 파일" in jsp_jakarta_lines(r, "ko")[0]
    r.plan.scope = "full"
    assert "renamed by the restructure" in jsp_jakarta_lines(r)[0]


def test_flag_help_both_languages():
    assert flag_help("framework-project").startswith("the project uses an in-house framework artifact")
    assert flag_help("framework-project", "ko").startswith("사내 프레임워크 아티팩트(framework.artifacts)를")


# ---------------------------------------------------------------------------------------------- abort reason
ABORT = ("Maven cannot resolve the parent pom com.example.fw:example-fw-parent:1.2.0; a framework project keeps its own "
         "parent pom, so the repository must serve it (settings: default). Make the repository serve it and rerun")
ABORT_KO = ("부모 pom을 저장소에서 받지 못했습니다(com.example.fw:example-fw-parent:1.2.0). 프레임워크 프로젝트는 부모 pom을 "
            "바꾸지 않으므로 저장소에서 받을 수 있어야 합니다.")


def test_abort_reason_korean():
    assert tr_line("ko", ABORT) == ABORT_KO
    assert tr_line("ko", f"run aborted: {ABORT}") == f"실행 중단: {ABORT_KO}"
    other = "Maven cannot resolve the parent a:b:1, c:d:2. A framework project keeps its parent."
    assert tr_line("ko", other).startswith("부모 pom을 저장소에서 받지 못했습니다(a:b:1, c:d:2).")
    assert not DETACHED.search(ABORT_KO)
    # the full-scope wording of the fix loop is not taken for a framework project
    full = ("Maven cannot resolve the parent com.example:company-parent:4.0.0, which is the configured version; "
            "check the repository")
    assert tr_line("ko", full) == full


# ---------------------------------------------------------------------------------------------- blocked suggestions
def _suggestions():
    from sbmig.loop.fixloop import framework_owner_suggestion as fos
    fw, api = f"{FW}:1.2.0", "javax.servlet:servlet-api:2.5"
    owner = "프로젝트 안에서는 고칠 수 없습니다. 프레임워크 담당자가 jakarta 빌드를 제공해야 합니다. sbmig는 프레임워크 버전을 그대로 둡니다."
    return [
        # (a) fixloop._framework_parent_abort: cand.suggestion (built inside a method, copied here)
        ("parent `com.example.fw:example-fw-parent:1.2.0` is not resolvable with settings `default`: this is a framework "
         f"project ({FW}) and keeps its own parent — make the repository serve it (or fix the settings / mirror) and "
         "rerun; nothing in the project can replace it",
         f"부모 pom을 저장소에서 받지 못했습니다(`com.example.fw:example-fw-parent:1.2.0`, settings `default`). 사내 프레임워크 "
         f"프로젝트({FW})는 부모 pom을 바꾸지 않으므로 저장소가 이 pom을 제공해야 합니다. 저장소에 올리거나 settings 또는 미러 "
         "설정을 고친 뒤 다시 실행해 주세요. 프로젝트 안에서 대신할 방법은 없습니다."),
        # (b) the framework artifact itself is still compiled against javax
        (fos(fw), f"이 프로젝트의 사내 프레임워크 아티팩트가 아직 javax 기준으로 빌드되어 있습니다(`{fw}`). {owner}"),
        (fos(FW, api), f"이 프로젝트의 사내 프레임워크 아티팩트가 아직 javax 기준으로 빌드되어 있습니다(`{FW}`, 필요한 javax "
                       f"API `{api}`). {owner}"),
        # (c) a javax API comes through the framework artifact
        (fos(fw, api, ["com.example:shop-web:1.0", fw, api]),
         f"javax 기준 API가 사내 프레임워크 아티팩트의 의존성으로 포함되어 있습니다(`{api}`, 프레임워크 `{fw}`, 경로 "
         f"com.example:shop-web:1.0 > {fw} > {api}). {owner}"),
        (fos(fw, None, [fw]), f"javax 기준 라이브러리가 사내 프레임워크 아티팩트의 의존성으로 포함되어 있습니다(프레임워크 `{fw}`, 경로 {fw}). "
                              f"{owner}"),
    ]


SUGGESTIONS = _suggestions()


@pytest.mark.parametrize("en,ko", SUGGESTIONS, ids=["parent", "itself", "itself-api", "through", "through-library"])
def test_framework_suggestions_in_korean(en, ko):
    assert tr_line("ko", en) == ko
    assert tr_line("en", en) == en
    assert "—" not in ko and "→" not in ko
    assert not DETACHED.search(ko.replace("`", "")), ko


def test_blocked_table_translates_the_suggestion():
    from sbmig.core.models import BlockedItem, BlockReason
    from sbmig.report.markdown import blocked_rows
    item = BlockedItem(id="b1", reason=BlockReason.EXTERNAL_JAVAX_API, phase=Phase.VERIFY, cluster_id="c",
                       suggestion=SUGGESTIONS[3][0])
    assert SUGGESTIONS[3][1] in "\n".join(blocked_rows([item], "ko"))


# ---------------------------------------------------------------------------------------------- Versions replaced
def _handover(**kw):
    from sbmig.core.models import HandoverSummary
    return HandoverSummary(**kw)


def test_unchanged_rows_are_left_out():
    from sbmig.core.models import VersionChange
    from sbmig.report.markdown import handover_lines
    same = VersionChange(kind="parent", key="com.example.fw:example-fw-parent", before="1.2.0", after="1.2.0",
                         reason="kept")
    r = _report(_use(DIRECT))
    r.handover = _handover(parent_before="com.example.fw:example-fw-parent:1.2.0",
                           parent_after="com.example.fw:example-fw-parent:1.2.0", java_before="1.8", java_after="21",
                           changes=[same], from_here=["x"])
    text = "\n".join(handover_lines(r))
    assert "### Versions replaced" in text and "- java: `1.8` → `21`" in text
    assert "- parent:" not in text and "example-fw-parent` | 1.2.0 | 1.2.0" not in text
    # nothing left: the section goes
    r.handover = _handover(parent_before="g:p:1", parent_after="g:p:1", java_before="21", java_after="21",
                           changes=[same], from_here=["x"])
    text = "\n".join(handover_lines(r))
    assert "### Versions replaced" not in text and "### From here on, your team" in text


def test_real_replacements_unchanged(tmp_path, cfg):
    from sbmig.report import build_handover
    from sbmig.report.markdown import _versions_replaced_lines, handover_lines

    from .test_handover import EFFECTIVE, final_tree
    report = make_report()
    report.handover = h = build_handover(as_is_profile(), final_tree(tmp_path), report, effective=EFFECTIVE, cfg=cfg)
    assert h.changes and h.parent_before != h.parent_after
    tops = [(n, b, a) for n, b, a in (("parent", h.parent_before, h.parent_after), ("java", h.java_before, h.java_after),
                                      ("spring boot", h.spring_boot_before, h.spring_boot_after)) if b or a]
    full = _versions_replaced_lines(h, tops, list(h.changes), "en")      # everything, nothing filtered
    text = "\n".join(handover_lines(report))
    assert "\n".join(full) in text


# ---------------------------------------------------------------------------------------------- bundled jars
def test_superseded_wording_is_neutral_in_a_reduced_run():
    from sbmig.core.models import JarCoordinate
    from sbmig.report.markdown import bundled_jars_lines
    jar = JarCoordinate(path="w/WEB-INF/lib/spring-webmvc-4.3.0.jar", group_id="org.springframework",
                        artifact_id="spring-webmvc", version="4.3.0", source="pom_properties",
                        note="superseded: org.springframework:spring-webmvc is managed by the target parent chain")
    r = _report(_use(DIRECT))
    r.profile.bundled_jars = [jar]
    ko = "\n".join(bundled_jars_lines(r, "ko"))
    en = "\n".join(bundled_jars_lines(r, "en"))
    assert "추가 안 함 (부모 pom에서 지정)" in ko and "새 부모 pom" not in ko
    assert "superseded (the parent chain manages it)" in en and "target" not in en
    r.plan.scope = "full"
    assert "추가 안 함 (새 부모 pom에서 지정)" in "\n".join(bundled_jars_lines(r, "ko"))


# ---------------------------------------------------------------------------------------------- To-be in a reduced run
FW_PARENT_POM = """<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>
<parent><groupId>com.example.fw</groupId><artifactId>example-fw-parent</artifactId><version>1.0.0</version></parent>
<groupId>com.example</groupId><artifactId>shop</artifactId><version>1.0</version>
<properties><maven.compiler.release>21</maven.compiler.release></properties>
<dependencies><dependency><groupId>com.example.fw</groupId><artifactId>example-fw-core</artifactId>
<version>1.2.0</version></dependency></dependencies></project>"""

BOOT2_POM = """<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>
<parent><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-parent</artifactId>
<version>2.7.18</version></parent>
<groupId>com.example</groupId><artifactId>shop</artifactId><version>1.0</version>
<properties><java.version>21</java.version></properties>
<dependencies><dependency><groupId>com.example.fw</groupId><artifactId>example-fw-core</artifactId>
<version>1.2.0</version></dependency></dependencies></project>"""


def _plain_profile():
    from sbmig.core.models import GAV, CaseType, PomInfo, ProjectProfile
    pom = PomInfo(path="pom.xml", group_id="com.example", artifact_id="shop", version="1.0", is_root=True,
                  parent=GAV.parse("com.example.fw:example-fw-parent:1.0.0"), java_version="1.8")
    return ProjectProfile(root="/as-is", name="shop", case_type=CaseType.SPRING_LEGACY, poms=[pom], root_pom=pom,
                          java_version="1.8")


def _tree(tmp_path, text):
    from sbmig.analyze.analyzer import PathView
    root = tmp_path / "final"
    root.mkdir(parents=True, exist_ok=True)
    (root / "pom.xml").write_text(text, encoding="utf-8")
    return PathView(root)


def _reduced_with_handover(tmp_path, cfg, profile, pom_text, scope="jdk-jakarta"):
    from sbmig.report import build_handover
    profile.framework = _use(DIRECT, scope=scope, reason="framework.scope=full → full migration" if scope == "full"
                             else "")
    r = make_report(profile=profile, plan=MigrationPlan(profile_fingerprint="f", case_type=profile.case_type, steps=[],
                                                        scope=scope),
                    flags=["framework-project"], advisories=[])
    r.handover = build_handover(profile, _tree(tmp_path, pom_text), r, effective={}, cfg=cfg)
    return r


def test_reduced_run_without_boot_claims_no_boot(tmp_path, cfg):
    r = _reduced_with_handover(tmp_path, cfg, _plain_profile(), FW_PARENT_POM)
    assert r.handover.spring_boot_after is None and r.handover.spring_boot_before is None
    for lang in ("en", "ko"):
        md = render_markdown(r, lang)
        to_be = next(x for x in md.splitlines() if x.startswith(("| To-be |", "| 전환 후 |"))).lower()
        assert "spring boot" not in to_be and "java 21" in to_be, to_be
        assert cfg.target.spring_boot_version not in md and "- spring boot:" not in md
    assert '"spring_boot_after": null' in r.model_dump_json() or '"spring_boot_after":null' in r.model_dump_json()
    # the parent is kept: no parent row under Versions replaced, the To-be names the kept parent
    md = render_markdown(r, "en")
    assert "- parent:" not in md and "example-fw-parent:1.0.0" in md.split("| Run id |")[0]


def test_full_run_without_boot_keeps_the_target_fallback(tmp_path, cfg):
    r = _reduced_with_handover(tmp_path, cfg, _plain_profile(), FW_PARENT_POM, scope="full")
    assert r.handover.spring_boot_after == cfg.target.spring_boot_version


def test_reduced_boot2_project_keeps_its_own_line(tmp_path, cfg):
    prof = as_is_profile()
    r = _reduced_with_handover(tmp_path, cfg, prof, BOOT2_POM)
    assert r.handover.spring_boot_before == r.handover.spring_boot_after == "2.7.18"
    md = render_markdown(r, "en")
    assert "- spring boot:" not in md and cfg.target.spring_boot_version not in md
    assert "spring boot 2.7.18" in md.split("| Run id |")[0]


def test_build_error_fixes_only_with_attempts():
    from sbmig.core.models import DecisionKind, FixAttempt, TriageDecision
    r = _report(_use(DIRECT))
    r.phases = [PhaseResult(phase=Phase.VERIFY, ok=True, build_ok=True)]
    assert "Done in this run: JDK 21 and jakarta" in framework_lines(r)
    assert "이번 실행에서 한 일: JDK 21과 jakarta 적용" in framework_lines(r, "ko")
    att = FixAttempt(id="a1", phase=Phase.VERIFY, iteration=1, cluster_id="case:c1", outcome="build_ok",
                     decision=TriageDecision(cluster_id="case:c1", kind=DecisionKind.SCRIPT))
    r.phases = [PhaseResult(phase=Phase.VERIFY, attempts=[att])]
    assert "Done in this run: JDK 21 and jakarta, build error fixes" in framework_lines(r)
    assert "이번 실행에서 한 일: JDK 21과 jakarta 적용, 빌드 오류 수정" in framework_lines(r, "ko")


# ---------------------------------------------------------------------------------------------- reduced-run report texts
KO_SAMPLES = [
    ("scope restored: provided (as-is): jakarta.servlet:jakarta.servlet-api (was compile)",
     "scope를 변경 전과 같은 provided로 되돌렸습니다(jakarta.servlet:jakarta.servlet-api, 레시피 뒤에는 compile)"),
    ("source encoding written: project.build.sourceEncoding=MS949",
     "소스 인코딩을 pom에 기록했습니다(project.build.sourceEncoding=MS949)"),
    ("source encoding written: pom.xml: project.build.sourceEncoding=MS949",
     "소스 인코딩을 pom에 기록했습니다(project.build.sourceEncoding=MS949)"),
    ("test sources were neither compiled nor run (build.skip_test_compile: true): the owning team compiles and fixes "
     "the tests (`mvn test-compile`) before relying on CI",
     "테스트 소스는 컴파일도 실행도 하지 않았습니다(build.skip_test_compile: true). CI에 맡기기 전에 담당 팀이 테스트를 "
     "컴파일하고 고쳐 주세요(`mvn test-compile`)."),
]


@pytest.mark.parametrize("en,ko", KO_SAMPLES, ids=["scope", "encoding", "encoding-pom", "tests"])
def test_korean_samples(en, ko):
    assert tr_line("ko", en) == ko and tr_line("en", en) == en
    assert "—" not in ko and "→" not in ko and not DETACHED.search(ko.replace("`", ""))


def _pom_fix(msg, status="resolved", seen="transform"):
    from sbmig.core.models import ErrorEntry
    return ErrorEntry(id=msg[:12], file="pom.xml", message=msg, seen=seen, status=status,
                      resolved="build" if status == "resolved" else "")


def _clean_reduced(**kw):
    r = _report(_use(DIRECT), **kw)
    r.error_entries = [_pom_fix(KO_SAMPLES[0][0]), _pom_fix(KO_SAMPLES[1][0])]
    return r


def test_transform_heading_and_order():
    from sbmig.report.errors import seen_groups
    from sbmig.report.markdown import SEEN_TITLES, error_list_lines
    assert SEEN_TITLES["transform"] == "transform — pom corrections by sbmig, verified by the next build"
    r = _clean_reduced()
    build = _pom_fix("cannot find symbol", status="open", seen="build").model_copy(update={"case_id": "c1", "l2": "x"})
    r.error_entries = [build, *r.error_entries]
    assert list(seen_groups(r.error_entries)) == ["transform", "build"]
    ko = "\n".join(error_list_lines(r, "ko"))
    assert "### transform — sbmig가 고친 pom, 다음 빌드로 확인 (2)" in ko
    assert KO_SAMPLES[0][1] in ko and "cannot find symbol" in ko


def test_header_row_counts_pom_corrections_apart():
    r = _clean_reduced()
    en, ko = render_markdown(r, "en"), render_markdown(r, "ko")
    assert '| **Error list** | 2 pom correction(s) by sbmig (resolved 2); no build error (see "Error list") |' in en
    # v2.1z-as: the guide has no error-list row; the pom corrections are listed under 자동 변환, no open error
    assert "에러 목록" not in ko and "## 해결하지 못한 오류" not in ko
    auto = ko.split("### 자동 변환", 1)[1].split("\n## ", 1)[0]
    assert "- sbmig가 변환 단계에서 직접 고친 pom:" in auto and f"  - `pom.xml`: {KO_SAMPLES[1][1]} (빌드로 확인)" in auto
    r.error_entries.append(_pom_fix("cannot find symbol", status="unresolved", seen="build").model_copy(
        update={"case_id": "c1", "l2": "x", "raw": "x"}))
    en = render_markdown(r, "en")
    assert '| **Error list** | 1 error(s): unresolved 1; 2 pom correction(s) by sbmig (see "Error list") |' in en
    # a full run keeps today's row
    r.plan.scope = "full"
    assert '| **Error list** | 3 error(s): unresolved 1, resolved 2 (see "Error list") |' in render_markdown(r, "en")


def test_handover_lists_the_pom_corrections():
    from sbmig.report.markdown import handover_lines
    r = _clean_reduced()
    en = "\n".join(handover_lines(r))
    assert "### pom corrections by sbmig" in en
    assert f"- `pom.xml`: {KO_SAMPLES[0][0]} (verified)" in en
    ko = "\n".join(handover_lines(r, "ko"))
    assert "### sbmig가 고친 pom" in ko and f"- `pom.xml`: {KO_SAMPLES[1][1]} (빌드로 확인)" in ko
    r.plan.scope = "full"
    assert "pom corrections by sbmig" not in "\n".join(handover_lines(r))


PLUGIN_POM_BEFORE = """<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>
<groupId>com.example</groupId><artifactId>shop</artifactId><version>1.0</version>
<build><plugins><plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-war-plugin</artifactId>
<version>3.5.1</version></plugin></plugins></build></project>"""


def test_reason_is_recipe_without_fix_attempts(tmp_path, cfg):
    from sbmig.core.models import DecisionKind, FixAttempt, PluginInfo, TriageDecision
    prof = _plain_profile()
    prof.poms[0].plugins = [PluginInfo(group_id="org.apache.maven.plugins", artifact_id="maven-war-plugin",
                                       version="3.4.0")]
    r = _reduced_with_handover(tmp_path, cfg, prof, PLUGIN_POM_BEFORE)
    r.phases = [PhaseResult(phase=Phase.VERIFY)]
    from sbmig.report import build_handover
    r.handover = build_handover(prof, _tree(tmp_path, PLUGIN_POM_BEFORE), r, effective={}, cfg=cfg)
    row = next(c for c in r.handover.changes if c.key.endswith("maven-war-plugin"))
    assert row.reason == "recipe"
    ko = render_markdown(r, "ko")
    assert "| 플러그인 `org.apache.maven.plugins:maven-war-plugin` | 3.4.0 | 3.5.1 | OpenRewrite 레시피 |" in ko
    att = FixAttempt(id="a1", phase=Phase.VERIFY, iteration=1, cluster_id="case:c1", outcome="build_ok",
                     decision=TriageDecision(cluster_id="case:c1", kind=DecisionKind.SCRIPT))
    r.phases = [PhaseResult(phase=Phase.VERIFY, attempts=[att])]
    r.handover = build_handover(prof, _tree(tmp_path, PLUGIN_POM_BEFORE), r, effective={}, cfg=cfg)
    assert next(c for c in r.handover.changes if c.key.endswith("maven-war-plugin")).reason == "recipe / fix script"


def test_flag_help_polite_korean():
    for f in ("framework-project", "tests-not-compiled", "rewrite-parse-problems", "bundled-jars"):
        ko = flag_help(f, "ko")
        assert "—" not in ko and ko.rstrip().endswith(("니다.", "주세요.")), (f, ko)


def test_error_list_opening_matches_the_header_row():
    from sbmig.report.markdown import error_list_lines, handover_lines
    r = _clean_reduced()
    assert "No build error in this run. 2 pom correction(s) by sbmig, verified by the build." in error_list_lines(r)
    assert "이번 실행에는 빌드 오류가 없습니다. sbmig가 고친 pom 2건을 빌드로 확인했습니다." in error_list_lines(r, "ko")
    r.error_entries[1] = _pom_fix(KO_SAMPLES[1][0], status="pom-verify")
    assert ("이번 실행에는 빌드 오류가 없습니다. sbmig가 고친 pom은 2건이고 그중 1건은 아직 빌드로 확인하지 못했습니다."
            in error_list_lines(r, "ko"))
    r.error_entries.append(_pom_fix("cannot find symbol", status="unresolved", seen="build").model_copy(
        update={"case_id": "c1", "l2": "x", "raw": "x"}))
    en = "\n".join(error_list_lines(r))
    assert "1 build error(s) in this run, one line each" in en and ": unresolved 1. Besides, 2 pom correction(s) by sbmig." in en
    assert "그 밖에 sbmig가 고친 pom 2건이 있습니다." in "\n".join(error_list_lines(r, "ko"))
    assert "sbmig changed these poms itself in the transform step." in handover_lines(r)
    assert "변환 단계에서 sbmig가 직접 고친 pom입니다." in handover_lines(r, "ko")
    r.plan.scope = "full"
    assert "3 error(s) in this run, one line each" in "\n".join(error_list_lines(r))
