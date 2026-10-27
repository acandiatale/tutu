"""v2 report parts: the "Structure review" section (en / ko), its Handover follow-ups, script attempts in the fix
attempts table and the error cases, the console line and the `review=` token of the SBMIG-RESULT line."""
from __future__ import annotations

import io
import json
from pathlib import Path

from rich.console import Console

from sbmig.core.models import (DecisionKind, ErrorCaseRecord, FixAttempt, LLMUsage, Phase, PhaseResult, ReviewIssue,
                               RunReport, ScriptRunResult, StructureReview, TriageDecision)
from sbmig.core.workspace import Workspace
from sbmig.report import build_handover, console_summary, render_markdown, write_report
from sbmig.report.handover import REVIEW_FOLLOWUPS_MAX, review_followups
from sbmig.report.i18n import tr_line
from sbmig.report.markdown import review_summary, script_label
from sbmig.report.result_line import report_result_line, result_line
from sbmig.report.writer import review_console_line

from .test_handover import EFFECTIVE, as_is_profile, final_tree, make_report
from .test_report import sample_report

EN_TITLE = "## Structure review"
KO_TITLE = "## 구조 점검"


def review(script: str = "ok", warnings: int = 1, *, commit: str | None = "abcdef1234567890",
           verdict: str = "issues") -> StructureReview:
    ok = script == "ok"
    issues = [ReviewIssue(severity="critical", file="shop-back/pom.xml",
                          problem="the module still inherits spring-boot-starter-parent 2.7.18",
                          fix_hint="parent com.company.platform:comp-jar:4.0.0"),
              ReviewIssue(severity="critical", file="shop-back/pom.xml",
                          problem="(sbmig check) MS949 Java sources in `shop-back` build with UTF-8",
                          fix_hint="set `project.build.sourceEncoding` to MS949")]
    issues += [ReviewIssue(severity="warning", file=f"m{i}/pom.xml", problem=f"explicit jackson version {i}",
                           fix_hint="drop the version") for i in range(warnings)]
    res = None
    if script:
        res = ScriptRunResult(status=script, changed_files=["shop-back/pom.xml"] if ok else [],
                              log=["shop-back/pom.xml: parent comp-jar"], error="" if ok else "NameError: name 'x'")
    return StructureReview(id="run-1:review", run_id="run-1", project="demo", verdict=verdict, model="gpt-x",
                           issues=issues if verdict == "issues" else [], analysis="Checked 3 poms. | One parent wrong.",
                           script="def fix(p):\n    pass\n" if script else "", script_result=res,
                           commit=commit if ok else None, reverted=bool(script) and not ok,
                           llm_usage=LLMUsage(model="gpt-x", calls=1, input_tokens=100, output_tokens=50),
                           note="" if ok else "review script failed (error): NameError: name 'x'")


def review_report(**kw) -> RunReport:
    r = sample_report()
    r.structure_review = review(**kw)
    return r


def section(md: str, title: str = EN_TITLE) -> str:
    start = md.index(title)
    nxt = md.find("\n## ", start + 3)
    return md[start: nxt if nxt != -1 else len(md)]


def test_section_english():
    md = render_markdown(review_report())
    sec = section(md)
    assert "- verdict: `issues` — 2 critical, 1 warning(s); script ok, 1 file(s) changed (commit `abcdef12`)" in sec
    assert "| severity | file | problem | fix hint |" in sec
    assert ("| **critical** | `shop-back/pom.xml` | the module still inherits spring-boot-starter-parent 2.7.18 | "
            "parent com.company.platform:comp-jar:4.0.0 |") in sec
    assert "| warning | `m0/pom.xml` | explicit jackson version 0 | drop the version |" in sec
    assert "- script: run status `ok`, 1 file(s) changed: `shop-back/pom.xml`" in sec
    assert "- commit: `abcdef123456` (revert it to undo the review's changes)" in sec
    assert "- model: `gpt-x`, 1 LLM call(s)" in sec and "Analysis: Checked 3 poms. \\| One parent wrong." in sec
    # header row, and the section sits after the Handover and before every build / fix section
    assert "| Structure review | 2 critical, 1 warning(s); script ok, 1 file(s) changed (commit `abcdef12`) |" in md
    assert md.index("## Handover") < md.index(EN_TITLE) < md.index("## Unresolved errors") < md.index("## Phases")


def test_section_korean():
    md = render_markdown(review_report(script="error"), language="ko")
    sec = section(md, KO_TITLE)
    assert "- 판정: `issues` — 심각 2건, 경고 1건; 스크립트 error, 변경 되돌림" in sec
    assert "| 심각도 | 파일 | 문제 | 수정 힌트 |" in sec and "| **심각** |" in sec and "| 경고 |" in sec
    assert "- 스크립트: 실행 결과 `error`, 파일 0개 변경" in sec and "- 스크립트의 변경은 되돌렸다" in sec
    assert "- 스크립트 오류: NameError: name 'x'" in sec and "- 메모: review script failed" in sec
    assert "| 구조 점검 | 심각 2건, 경고 1건; 스크립트 error, 변경 되돌림 |" in md
    assert "Structure review" not in md and "verdict:" not in md


def test_verdict_variants_and_no_section_without_a_review():
    assert EN_TITLE not in render_markdown(sample_report())
    assert "| Structure review |" not in render_markdown(sample_report())
    ok = review(script="", verdict="ok")
    assert review_summary(ok) == "ok — no issues" and review_summary(ok, "ko") == "ok — 문제 없음"
    skipped = StructureReview(verdict="skipped", note="review.enabled is off")
    assert review_summary(skipped) == "skipped (review.enabled is off)"
    failed = StructureReview(verdict="failed", note="llm-error: LLMError: 503")
    assert review_summary(failed, "ko") == "실패 — 쓸 수 있는 답이 없어 점검 없이 빌드함 (llm-error: LLMError: 503)"
    r = sample_report()
    r.structure_review = failed
    sec = section(render_markdown(r))
    assert "- verdict: `failed`" in sec and "| severity |" not in sec and "- note: llm-error: LLMError: 503" in sec
    no_script = review(script="")
    assert review_summary(no_script).endswith("; no script")
    r.structure_review = no_script
    assert "- no script for the critical issue(s): they are handed over" in render_markdown(r)
    not_run = no_script.model_copy(update={"script": "def fix(p):\n    pass\n", "note": "script not run: off"})
    assert review_summary(not_run).endswith("; script not run")


def test_handover_followups():
    lines = review_followups(review_report())
    assert lines == [
        "structure review script changed 1 file(s) for 1 critical issue(s): shop-back/pom.xml (commit abcdef12) — "
        "check these pom changes",
        "structure review: critical issue not fixed — shop-back/pom.xml: (sbmig check) MS949 Java sources in "
        "`shop-back` build with UTF-8 → set `project.build.sourceEncoding` to MS949",
        "structure review warning — m0/pom.xml: explicit jackson version 0 → drop the version"]
    failed = review_followups(review_report(script="error", warnings=12))
    assert len(failed) == REVIEW_FOLLOWUPS_MAX
    assert failed[0].startswith("structure review: critical issue not fixed — shop-back/pom.xml: the module")
    assert failed[-1] == "… and 7 more structure review issue(s) — see Structure review"
    assert review_followups(sample_report()) == []
    assert tr_line("ko", lines[0]) == ("구조 점검 스크립트가 심각한 문제 1건 때문에 파일 1개를 바꿈: shop-back/pom.xml "
                                       "(커밋 abcdef12) — 이 pom 변경을 확인할 것")
    assert tr_line("ko", lines[2]).startswith("구조 점검 경고 — m0/pom.xml")
    assert tr_line("ko", failed[-1]) == "… 외 구조 점검 문제 7건 — '구조 점검' 참조"


def test_handover_from_here_lists_the_review(tmp_path, cfg):
    report = make_report(structure_review=review(script="error"))
    h = build_handover(as_is_profile(), final_tree(tmp_path), report, effective=EFFECTIVE, cfg=cfg)
    items = [x for x in h.from_here if x.startswith("structure review")]
    assert len(items) == 3 and items[0].startswith("structure review: critical issue not fixed")
    md = render_markdown(report.model_copy(update={"handover": h}), language="ko")
    assert "구조 점검: 고치지 못한 심각한 문제 — shop-back/pom.xml" in md


def _script_attempt(**kw) -> FixAttempt:
    base = dict(id="verify-1-1", phase=Phase.VERIFY, iteration=1, cluster_id="case:3f2a9c01de",
                decision=TriageDecision(cluster_id="case:3f2a9c01de", kind=DecisionKind.SCRIPT, rationale="reuse"),
                commit="0011223344556677", outcome="improved", script_mode="augment", script_case_id="3f2a9c01de",
                script_version=2, script_parent=1, script_result=ScriptRunResult(status="ok", changed_files=["a"]))
    base.update(kw)
    return FixAttempt(**base)


def test_script_attempts_in_the_fix_attempts_table():
    r = sample_report()
    failed = _script_attempt(id="verify-1-2", script_mode="new", script_version=0, outcome="apply_failed",
                             script_result=ScriptRunResult(status="timeout", error="killed after 60s"))
    r.phases.append(PhaseResult(phase=Phase.VERIFY, attempts=[_script_attempt(), failed]))
    md = render_markdown(r)
    assert ("| verify/1 | `case:3f2a9c01de` | script | `script:3f2a9c01de:v2` augment (ok) | improved | 00112233 |"
            in md)
    assert "`script:3f2a9c01de` new (timeout) | apply_failed | 00112233 | killed after 60s |" in md
    assert script_label(_script_attempt(script_result=None, script_mode="reuse")) == "`script:3f2a9c01de:v2` reuse"
    # the sample report's own script attempts render beside them
    assert "`script:abcd1234:v1` new" in md and "`script:c2:v2` reuse (ok)" in md


def test_error_case_names_the_resolving_script_version():
    r = sample_report()
    r.error_cases = [
        ErrorCaseRecord(id="e1", status="resolved", file="src/A.java", line=3, message="cannot find symbol",
                        fix_method="script_reuse", resolver_id="script:3f2a9c01de:v3", script_case_id="3f2a9c01de",
                        script_version=3, description="ran the stored script"),
        ErrorCaseRecord(id="e2", status="resolved", file="src/B.java", message="package javax.x does not exist",
                        fix_method="script_augment", case_id="77aa", script_version=4, attempt_count=2,
                        llm_attempts=1),
        ErrorCaseRecord(id="e3", status="resolved", file="pom.xml", message="lombok", fix_method="handler",
                        resolver_id="lombok-version-floor")]
    r.error_case_counts = {"resolved": 3}
    md = render_markdown(r)
    assert "script_reuse `script:3f2a9c01de:v3`: ran the stored script" in md
    assert "script_augment `script:77aa:v4`" in md and "handler `lombok-version-floor`" in md
    assert "- fixed by (resolved errors): script_reuse 1, script_augment 1, handler 1" in md   # old methods last


def test_result_line_review_token_and_console_line():
    r = review_report()
    line = report_result_line(r, 2)
    assert line.endswith(" review=issues") and line.isascii()
    assert "review=" not in report_result_line(sample_report(), 2)
    assert result_line("r", 0, True) == "SBMIG-RESULT run=r exit=0 build=ok unresolved=0 blocked=0 llm=0 attn=-"
    assert result_line("r", 0, True, review="ok").endswith("attn=- review=ok")
    cl = review_console_line(r)
    assert cl == "structure review: 2 critical, 1 warning(s); script ok, 1 file(s) changed (commit abcdef12)"
    assert review_console_line(sample_report()) is None
    buf = io.StringIO()
    console_summary(r, Console(file=buf, width=200))
    assert "structure review: 2 critical" in buf.getvalue()


def test_report_json_carries_the_review(tmp_path: Path):
    ws = Workspace(tmp_path / "p", tmp_path / "run", "run-1")
    paths = write_report(review_report(), ws)
    data = json.loads(Path(paths["report_json"]).read_text(encoding="utf-8"))
    assert data["structure_review"]["verdict"] == "issues"
    assert data["structure_review"]["issues"][0]["severity"] == "critical"
    back = RunReport.model_validate(data)
    assert back.structure_review is not None and back.structure_review.script_result.status == "ok"
