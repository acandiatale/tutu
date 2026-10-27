"""v2.1d report side (docs/INTERFACES.md "v2.1d"): `report.operator_checks` rendered first in the Handover
("운영자 확인 필요 (메이저 업그레이드 N건)" / "Operator checks (N major upgrades)"), the first "from here on" line, the
Fixes-by-case marker, `SBMIG-RESULT … major=<n>` (only when n > 0), the flag help and the report.json round trip.
The orchestrator side (which checks are built, `still_applied`) is tests/loop/test_v21d_versions.py."""
from __future__ import annotations

import json
from types import SimpleNamespace

from sbmig.core.models import DependencyChange, ErrorEntry, FixTry, OperatorCheck, RunReport
from sbmig.report import build_handover, render_markdown, write_report
from sbmig.report.handover import operator_checks_item
from sbmig.report.markdown import FLAG_HELP, handover_lines
from sbmig.report.result_line import report_result_line, result_line

from .test_handover import EFFECTIVE, as_is_profile, final_tree, make_report

LLM_CHECK = OperatorCheck(
    change=DependencyChange(path="xxx-back/pom.xml", key="org.sitemesh:sitemesh", before="2.4.2", after="3.2.1",
                            kind="major"),
    case_id="abc1234567", source="fix r3 try 2", commit="1a2b3c4d5e6f",
    guide="- web.xml 의 SiteMeshFilter 등록을 확인\n- decorators.xml 을 sitemesh3.xml 로 옮겼는지 확인",
    guide_source="llm", files=["xxx-back/pom.xml", "xxx-boot/src/main/resources/sitemesh3.xml"])
SBMIG_CHECK = OperatorCheck(
    kind="library_replaced",
    change=DependencyChange(path="pom.xml", key="opensymphony:oscache", before="2.4.1", after="1.0.0",
                            kind="replaced", replaced_by="com.example:cache-jakarta", approved=True,
                            section="dependencyManagement"),
    source="review", commit="9f8e7d6c5b4a", guide="opensymphony:oscache → com.example:cache-jakarta: check the cache "
                                                  "configuration", guide_source="sbmig")


def report_with_checks(tmp_path, cfg, checks=(LLM_CHECK, SBMIG_CHECK), **kw) -> RunReport:
    report = make_report(operator_checks=list(checks),
                         flags=["tests-not-compiled"] + (["major-upgrade"] if checks else []), **kw)
    report.handover = build_handover(as_is_profile(), final_tree(tmp_path), report, effective=EFFECTIVE, cfg=cfg)
    return report


def test_korean_handover_starts_with_the_operator_checks(tmp_path, cfg):
    md = render_markdown(report_with_checks(tmp_path, cfg), language="ko")
    start = md.index("### 운영자 확인 필요 (메이저 업그레이드 2건)")
    assert md.index("## 인수인계") < start < md.index("### 교체된 버전")
    block = md[start:md.index("### 교체된 버전")]
    assert ("- **org.sitemesh:sitemesh 2.4.2 → 3.2.1** — `xxx-back/pom.xml` (dependencies), 케이스 `abc1234567`, "
            "fix r3 try 2, 커밋 `1a2b3c4`\n"
            "  - web.xml 의 SiteMeshFilter 등록을 확인\n"
            "  - decorators.xml 을 sitemesh3.xml 로 옮겼는지 확인\n"
            "  - 가이드 출처: LLM\n") in block
    assert ("- **opensymphony:oscache 2.4.1 → com.example:cache-jakarta 1.0.0** — `pom.xml` (dependencyManagement), "
            "구조 점검, 커밋 `9f8e7d6` — 승인됨 (libraries.yml)\n") in block
    assert "  - 가이드 출처: sbmig 자동 생성" in block
    assert "빌드는 통과했지만" in block
    # "from here on": the pointer comes first
    todo = md[md.index("### 이후 담당 팀이 할 일"):]
    assert todo.split("\n")[2] == '1. 운영자 확인 필요: 메이저 업그레이드 2건 — 위 "운영자 확인 필요" 참조'


def test_english_rendering_and_the_flag_help(tmp_path, cfg):
    report = report_with_checks(tmp_path, cfg)
    md = render_markdown(report, language="en")
    assert md.index("### Operator checks (2 major upgrades)") < md.index("### Versions replaced")
    assert ("- **org.sitemesh:sitemesh 2.4.2 → 3.2.1** — `xxx-back/pom.xml` (dependencies), case `abc1234567`, "
            "fix r3 try 2, commit `1a2b3c4`") in md
    assert "structure review, commit `9f8e7d6` — approved (libraries.yml)" in md
    assert "  - guide source: LLM" in md and "  - guide source: sbmig (synthesized)" in md
    assert '1. operator checks: 2 major upgrade(s) — see "Operator checks" above' in md
    assert f"- `major-upgrade`: {FLAG_HELP['major-upgrade']}" in md
    assert report.handover.from_here[0] == operator_checks_item(2)


def test_no_checks_no_block_and_no_pointer(tmp_path, cfg):
    report = report_with_checks(tmp_path, cfg, checks=())
    md = render_markdown(report, language="ko")
    assert "운영자 확인 필요" not in md and "Operator checks" not in md
    assert not any(x.startswith("operator checks:") for x in report.handover.from_here)


def test_checks_without_a_handover_summary_still_render(cfg):
    report = make_report(operator_checks=[LLM_CHECK])       # handover None (the run stopped before analyze)
    lines = handover_lines(report, "en")
    assert lines[2] == "### Operator checks (1 major upgrades)"
    assert '1. operator checks: 1 major upgrade(s) — see "Operator checks" above' in lines


def test_a_shipped_table_approval_names_the_shipped_file(cfg):
    shipped = SBMIG_CHECK.model_copy(deep=True)
    shipped.change.note = "approved by the shipped table: QLRM 4 is the jakarta line"
    lines = handover_lines(make_report(operator_checks=[shipped]), "ko")
    assert any(x.endswith(" — 승인됨 (jakarta_libraries.yml)") for x in lines)


def test_a_check_without_a_guide_says_so(tmp_path, cfg):
    bare = LLM_CHECK.model_copy(update={"guide": "", "guide_source": "sbmig", "case_id": "", "commit": ""})
    md = render_markdown(report_with_checks(tmp_path, cfg, checks=[bare]), language="ko")
    assert "- **org.sitemesh:sitemesh 2.4.2 → 3.2.1** — `xxx-back/pom.xml` (dependencies), fix r3 try 2\n" in md
    assert "  - (가이드 없음)\n  - 가이드 출처: sbmig 자동 생성" in md


def test_result_line_major_token():
    r = make_report(operator_checks=[LLM_CHECK])
    line = report_result_line(r, 0)
    assert line.endswith(" major=1") and line.isascii()
    assert " major=" not in report_result_line(make_report(), 0)
    assert result_line("r", 0, True, review="ok", major=2).endswith("attn=- review=ok major=2")
    assert result_line("r", 0, True, major=0).endswith("attn=-")
    # the flag is an attention code too (fleet summary.csv `attention`, `fleet status --attention`)
    flagged = make_report(operator_checks=[LLM_CHECK], flags=["major-upgrade"])
    assert report_result_line(flagged, 0).endswith("attn=major-upgrade major=1")


def test_report_json_round_trip(tmp_path, cfg):
    report = report_with_checks(tmp_path, cfg)
    ws = SimpleNamespace(run_dir=tmp_path / "run")
    write_report(report, ws, cfg=cfg)
    data = json.loads((tmp_path / "run" / "report.json").read_text(encoding="utf-8"))
    back = RunReport.model_validate(data)
    assert back.operator_checks == [LLM_CHECK, SBMIG_CHECK]
    assert data["operator_checks"][1]["kind"] == "library_replaced"
    assert data["operator_checks"][1]["change"]["replaced_by"] == "com.example:cache-jakarta"


def test_fixes_by_case_marks_a_case_whose_deciding_try_made_a_major_upgrade(cfg):
    t = FixTry(n=1, round=1, source="llm-new", applied="edits", result="improved", analysis="sitemesh 2 is javax",
               solution="raise to 3", major_upgrades=[LLM_CHECK.change], verify="check")
    rolled = t.model_copy(update={"result": "reverted"})
    entries = [ErrorEntry(id="e1", case_id="abc1234567", message="cannot find symbol", status="resolved",
                          resolved="r1 try 1", seen="build", tries=[t]),
               ErrorEntry(id="e2", case_id="def7654321", message="other", status="unresolved", seen="build",
                          tries=[rolled])]
    md_ko = render_markdown(make_report(error_entries=entries), language="ko")
    row = next(ln for ln in md_ko.splitlines() if ln.startswith("| `abc1234567`"))
    assert '⚠ 메이저 (1건, "운영자 확인 필요" 참조)' in row
    other = next(ln for ln in md_ko.splitlines() if ln.startswith("| `def7654321`"))
    assert "⚠" not in other                                   # a rolled-back try is not in the tree
    md_en = render_markdown(make_report(error_entries=entries), language="en")
    assert "| `abc1234567` ⚠ major (1, see Operator checks) " in md_en
