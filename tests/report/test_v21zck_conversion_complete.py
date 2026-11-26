"""INTERFACES "v2.1z-ck": a framework run (scope jdk-jakarta) whose conversion is done reads as done in both
renderers and the console summary, also when the build after it fails; everything else reads as before."""
from __future__ import annotations

import io

import pytest
from rich.console import Console

from sbmig.analyze.framework import no_fix_loop_followup
from sbmig.core.models import (
    BlockedItem,
    BlockReason,
    BuildResult,
    DecisionKind,
    FixAttempt,
    Phase,
    PhaseResult,
    PlanStep,
    RecipeRef,
    RewriteResult,
    StepKind,
    TriageDecision,
)
from sbmig.report import guide_ko, markdown, render_markdown, writer
from sbmig.report.guide_ko import head_lines, purpose_lines
from sbmig.report.markdown import result_label
from sbmig.report.result_line import conversion_complete, report_result_line
from sbmig.report.writer import console_summary
from tests.loop.fakes import compile_error

from .test_framework_scope import DIRECT, _report, _use
from .test_handover import make_report

EN_LABEL = "✅ CONVERTED (the build does not pass)"
KO_LABEL = "✅ JDK 21, Jakarta 변환 완료 (변환 뒤 빌드는 실패)"
DOC_SHORT = ("이 문서는 마이그레이션을 요청하신 담당자께 드리는 안내입니다. 무엇을 바꿨는지, 전환 후 무엇을 확인하셔야 "
             "하는지 순서대로 적었습니다.")
DOC_LONG = ("이 문서는 마이그레이션을 요청하신 담당자께 드리는 안내입니다. 무엇을 어떻게 바꿨는지, 전환 후 무엇을 "
            "확인하셔야 하는지 순서대로 적었습니다.")


def _build(ok: bool, errors=()) -> BuildResult:
    return BuildResult(ok=ok, goal="clean package", command=["mvn"], exit_code=0 if ok else 1, duration_s=1.0,
                       log_path="/run/logs/verify.log", errors=list(errors))


def _rewrite(ok: bool = True, dry_run: bool = False) -> RewriteResult:
    return RewriteResult(ok=ok, recipes=["org.openrewrite.java.migrate.UpgradeToJava21"], changed_files=["pom.xml"],
                         dry_run=dry_run)


def _rewrite_step(target=None, recipes=True) -> PlanStep:
    sid = f"transform.rewrite@{target}" if target else "transform.rewrite"
    refs = [RecipeRef(id="org.openrewrite.java.migrate.UpgradeToJava21",
                      artifact="org.openrewrite.recipe:rewrite-migrate-java")] if recipes else []
    return PlanStep(id=sid, kind=StepKind.OPENREWRITE, phase=Phase.TRANSFORM, title="OpenRewrite in one run: Java 21",
                    recipes=refs, target=target)


def _steps(*rewrite: PlanStep) -> list[PlanStep]:
    """The shape of a stored reduced plan: the rewrite step(s), then the one verify build."""
    build = PlanStep(id="verify.build", kind=StepKind.BUILD, phase=Phase.VERIFY, title="final mvn clean package",
                     build_goal="clean package")
    return list(rewrite or (_rewrite_step(),)) + [build]


def _attempt() -> FixAttempt:
    return FixAttempt(id="a1", phase=Phase.VERIFY, iteration=1, cluster_id="case:c1", outcome="no_change",
                      decision=TriageDecision(cluster_id="case:c1", kind=DecisionKind.SCRIPT))


def converted(*, built: bool = False, name: str = "Example", fix_loop: bool = False, attempts: bool = False,
              errors: bool = True, blocked: bool = False):
    """A reduced run whose one OpenRewrite run succeeded; the final build passed or failed."""
    errs = [compile_error("src/main/java/com/x/Foo.java", msg="cannot find symbol")] if errors and not built else []
    phases = [PhaseResult(phase=Phase.TRANSFORM, ok=True, rewrite_results=[_rewrite()]),
              PhaseResult(phase=Phase.VERIFY, ok=built, build_ok=built, attempts=[_attempt()] if attempts else [])]
    followups = [] if built or fix_loop else [no_fix_loop_followup(len(errs))]
    r = _report(_use(DIRECT, name=name, fix_loop=fix_loop), success=built, phases=phases,
                final_build=_build(built, errs), unresolved=errs, manual_followups=followups)
    r.plan.steps = _steps()
    if blocked:
        r.blocked = [BlockedItem(id="blk-1", reason=BlockReason.ARTIFACT_NOT_FOUND, phase=Phase.VERIFY,
                                 cluster_id="compile:1")]
    return r


def _console(r) -> str:
    buf = io.StringIO()
    console_summary(r, Console(file=buf, width=300, force_terminal=False, color_system=None))
    return buf.getvalue().split("\n")[1]


# ---------------------------------------------------------------------------------------------- the rule
def _turn_off(r, what: str) -> None:
    if what == "remediation flow":
        r.flow = "remediation"
    elif what == "full scope":
        r.plan.scope = "full"
    elif what == "no plan":
        r.plan = None
    elif what.startswith("flag "):
        r.flags.append(what[5:])
    elif what == "no final build":
        r.final_build = None
    elif what == "no rewrite result":
        r.phases[0].rewrite_results = []
    elif what == "only a dry run":
        r.phases[0].rewrite_results = [_rewrite(dry_run=True)]
    elif what == "one failed rewrite result":
        r.phases.append(PhaseResult(phase=Phase.TRANSFORM, rewrite_results=[_rewrite(ok=False)]))
    else:
        raise AssertionError(what)


OFF = ["remediation flow", "full scope", "no plan", "flag aborted", "flag aborted:time-budget", "flag mvn-not-found",
       "flag rewrite-failed", "flag hollow-reactor", "no final build", "no rewrite result", "only a dry run",
       "one failed rewrite result"]


@pytest.mark.parametrize("built", [False, True], ids=["build-failed", "build-passed"])
def test_rule_holds_on_objects_and_dicts(built):
    r = converted(built=built)
    assert conversion_complete(r) is True
    assert conversion_complete(r.model_dump(mode="json")) is True


@pytest.mark.parametrize("what", OFF)
def test_rule_turns_off_one_condition_at_a_time(what):
    r = converted()
    _turn_off(r, what)
    assert conversion_complete(r) is False
    assert conversion_complete(r.model_dump(mode="json")) is False


@pytest.mark.parametrize("flags", ["rewrite-failed", "", 5, None, "missing"],
                         ids=["string", "empty-string", "number", "none", "missing-key"])
def test_rule_on_a_dict_with_unreadable_flags(flags):
    d = converted().model_dump(mode="json")
    if flags == "missing":
        del d["flags"]
    else:
        d["flags"] = flags
    assert conversion_complete(d) is False


def _both(r) -> tuple[bool, bool]:
    return conversion_complete(r), conversion_complete(r.model_dump(mode="json"))


def test_rule_counts_one_result_per_planned_rewrite_step():
    r = converted()
    r.plan.steps = _steps(_rewrite_step("svc-a"), _rewrite_step("svc-b"))
    assert _both(r) == (False, False)           # the second service's step recorded no result
    r.phases[0].rewrite_results.append(_rewrite())
    assert _both(r) == (True, True)


def test_rule_does_not_count_a_rewrite_step_without_recipes():
    r = converted()
    r.plan.steps = _steps(_rewrite_step("svc-a"), _rewrite_step("svc-b", recipes=False))
    assert _both(r) == (True, True)


def test_rule_needs_one_result_when_no_rewrite_step_is_planned():
    r = converted()
    r.plan.steps = [s for s in r.plan.steps if s.kind != StepKind.OPENREWRITE]
    assert _both(r) == (True, True)
    r.phases[0].rewrite_results = []
    assert _both(r) == (False, False)


@pytest.mark.parametrize("steps", ["missing", "transform.rewrite", "no-kind"])
def test_rule_on_a_dict_with_unreadable_plan_steps(steps):
    d = converted().model_dump(mode="json")
    if steps == "missing":
        del d["plan"]["steps"]
    elif steps == "no-kind":
        del d["plan"]["steps"][0]["kind"]
    else:
        d["plan"]["steps"] = steps
    assert conversion_complete(d) is False


@pytest.mark.parametrize("final_build", [{}, False, "x", {"ok": "yes"}], ids=["empty", "false", "string", "ok-string"])
def test_rule_on_a_dict_with_unreadable_final_build(final_build):
    d = converted().model_dump(mode="json")
    d["final_build"] = final_build
    assert conversion_complete(d) is False


def test_rule_ignores_a_dry_run_beside_a_real_one():
    r = converted()
    r.phases[0].rewrite_results.append(_rewrite(ok=False, dry_run=True))
    assert conversion_complete(r) and conversion_complete(r.model_dump(mode="json"))


# ---------------------------------------------------------------------------------------------- labels
def test_labels_in_both_languages():
    r = converted()
    assert result_label(r) == EN_LABEL
    assert result_label(r, "ko") == KO_LABEL
    passed = converted(built=True)
    assert conversion_complete(passed)
    assert result_label(passed) == "✅ BUILD SUCCESS" and result_label(passed, "ko") == "✅ 빌드 성공"


def test_english_head_row_has_one_parenthetical():
    md = render_markdown(converted(), "en")
    assert f"| **Result** | {EN_LABEL} |" in md.split("\n")
    assert "final `mvn clean package`" not in md.split("| SBMIG-RESULT |", 1)[0]
    # the build did fail: the result line and the error sections are unchanged
    assert "exit=2 build=FAIL" in report_result_line(converted())
    assert "## Error list" in md


def test_korean_head_cell():
    lines = head_lines(converted())
    assert lines[2] == f"| **결과** | {KO_LABEL} |"
    passed = head_lines(converted(built=True))
    assert passed[2] == "| **결과** | ✅ 빌드 성공 (최종 `mvn clean package`) |"


# ---------------------------------------------------------------------------------------------- purpose paragraphs
@pytest.mark.parametrize("name", ["Example", ""])
def test_purpose_without_fix_loop(name):
    who = f"{name or '사내'} 프레임워크 프로젝트"
    assert purpose_lines(converted(name=name)) == [
        (f"JDK 21과 Jakarta 변환을 마쳤습니다. {who}는 변환까지가 이번 작업의 범위입니다. 변환 뒤 빌드는 통과하지 "
         "않았고 빌드 오류는 자동으로 고치지 않았습니다. 남은 오류를 아래 「변환 뒤 남은 빌드 오류」에 정리했습니다. "
         "프로젝트 담당자와 프레임워크 담당자가 고쳐 주셔야 합니다."), "", DOC_SHORT, ""]


def test_purpose_without_fix_loop_and_without_error_rows():
    lead = purpose_lines(converted(errors=False))[0]
    assert lead.endswith("빌드 오류는 자동으로 고치지 않았습니다. 「참고」의 마지막 빌드 로그를 봐 주세요. 프로젝트 담당자와 "
                         "프레임워크 담당자가 고쳐 주셔야 합니다.")


@pytest.mark.parametrize("name", ["Example", ""])
def test_purpose_with_fix_loop(name):
    who = f"{name or '사내'} 프레임워크 프로젝트"
    assert purpose_lines(converted(name=name, fix_loop=True, attempts=True)) == [
        (f"JDK 21과 Jakarta 변환을 마쳤습니다. {who}는 변환까지가 이번 작업의 범위입니다. 빌드 오류를 자동으로 고쳐 "
         "보았지만 최종 빌드는 아직 통과하지 않습니다. 아래 「해결하지 못한 오류」를 먼저 봐 주세요. 남은 오류는 프로젝트 "
         "담당자와 프레임워크 담당자가 고쳐 주셔야 합니다."), "", DOC_LONG, ""]


def test_purpose_with_fix_loop_where_to_look():
    both = purpose_lines(converted(fix_loop=True, attempts=True, blocked=True))[0]
    assert "아래 「해결하지 못한 오류」와 「진행하지 않은 항목」을 먼저 봐 주세요." in both
    none = purpose_lines(converted(fix_loop=True, attempts=True, errors=False))[0]
    assert "「참고」의 마지막 빌드 로그를 먼저 봐 주세요." in none
    # the loop is on but made no attempt: no claim that a fix was made
    idle = purpose_lines(converted(fix_loop=True))[0]
    assert "변환 뒤 빌드는 아직 통과하지 않습니다." in idle and "고쳐 보았지만" not in idle


def test_guide_sections_keep_their_titles():
    md = render_markdown(converted(), "ko")
    assert "## 변환 뒤 남은 빌드 오류 — 1건" in md
    md = render_markdown(converted(fix_loop=True, attempts=True), "ko")
    assert "## 해결하지 못한 오류 — 1건" in md


# ---------------------------------------------------------------------------------------------- console
def test_console_line():
    line = _console(converted())
    assert "CONVERTED (the build does not pass)" in line and "NOT BUILDING" not in line
    assert "BUILD SUCCESS" in _console(converted(built=True))


# ---------------------------------------------------------------------------------------------- unchanged runs
def _non_framework_failed():
    return make_report(success=False, final_build=_build(False))


def _rewrite_failed():
    r = converted()
    r.flags.append("rewrite-failed")
    return r


def _aborted():
    r = converted()
    r.flags.append("aborted:time-budget")
    return r


@pytest.mark.parametrize("make", [_non_framework_failed, _rewrite_failed, _aborted],
                         ids=["non-framework", "rewrite-failed", "aborted"])
def test_other_runs_read_exactly_as_before(make, monkeypatch):
    r = make()
    assert not conversion_complete(r)
    now = (render_markdown(r, "en"), render_markdown(r, "ko"), _console(r))
    for mod in (markdown, guide_ko, writer):
        monkeypatch.setattr(mod, "conversion_complete", lambda _r: False)
    assert now == (render_markdown(r, "en"), render_markdown(r, "ko"), _console(r))
    assert "NOT BUILDING" in now[0] and "NOT BUILDING" in now[2] and "변환 완료" not in now[1]


def test_converted_run_differs_only_by_this_change(monkeypatch):
    r = converted()
    en = render_markdown(r, "en")
    for mod in (markdown, guide_ko, writer):
        monkeypatch.setattr(mod, "conversion_complete", lambda _r: False)
    old = render_markdown(r, "en")
    assert old.replace("| **Result** | ❌ NOT BUILDING (final `mvn clean package`) |",
                       f"| **Result** | {EN_LABEL} |") == en


# ---------------------------------------------------------------------------------------------- older lines the contract leans on
def test_jsp_sentence_follows_the_final_build(cfg):
    from sbmig.report.handover import jsp_javax_item
    from sbmig.report.i18n import tr_line
    for built in (True, False):
        r = converted(built=built)
        r.profile.source.jsp_javax_files = 2
        line = jsp_javax_item(r, cfg)
        assert line is not None
        assert ("the build passes" in line) is built
        ko = tr_line("ko", line)
        assert ko != line and ("빌드는 통과하지만" in ko) is built
        assert "Tomcat 10 이상에서 실행하면 실패합니다" in ko
    r = converted()
    r.profile.source.jsp_javax_files = 2
    r.final_build = None
    assert "the build passes" not in jsp_javax_item(r, cfg)


def test_literal_versions_line_in_korean():
    from sbmig.report.i18n import tr_line
    line = ("the recipe run could not download Maven metadata (proxy / rate limit): literal versions `4.0.x`, `2.x` "
            "remain in pom.xml, app/pom.xml — re-run sbmig later or set the versions by hand")
    assert tr_line("ko", line) == ("레시피를 실행할 때 Maven 메타데이터를 받지 못해(프록시나 요청 제한) pom.xml, app/pom.xml에 "
                                   "해석되지 않은 버전 문자열(`4.0.x`, `2.x`)이 남았습니다. 나중에 sbmig를 다시 실행하거나 "
                                   "버전을 직접 정해 주세요.")
