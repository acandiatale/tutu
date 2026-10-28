"""v2.1i `fix.mode: edits` in the report: an `llm-edit` try is labelled `edits:<case>#<n>` (error list, Fixes by
case, console), `SBMIG-RESULT … fix=edits` only when the mode is not `script`, and "LLM usage and cost" names the
mode. A script-mode report is byte-identical (the result line operators retype above all)."""
from __future__ import annotations

from rich.console import Console

from sbmig.core.models import ErrorEntry, FixTry, LLMUsage
from sbmig.report import console_summary, render_markdown
from sbmig.report.cost import compute_llm_cost
from sbmig.report.errors import case_rows, deciding_label, edits_label, is_edits_try, tries_text, try_text
from sbmig.report.result_line import report_result_line, result_line

from .test_cost import _priced_report
from .test_error_list import FIXES_EN, entries, report_with, section
from .test_result_line import _report

CASE = "8bb4fd4a4a"
DIFF = ("diff --git a/src/Foo.java b/src/Foo.java\n--- a/src/Foo.java\n+++ b/src/Foo.java\n@@ -1 +1 @@\n"
        "-class Foo { broken }\n+class Foo { ok }\n")


def edits_entries() -> list[ErrorEntry]:
    t1 = FixTry(n=1, round=1, source="llm-edit", result="no_change", applied="edits", analysis="first guess",
                files=["src/Foo.java"], diff=DIFF)
    t2 = FixTry(n=2, round=2, source="llm-edit", result="build_ok", applied="edits", analysis="the cause",
                solution="the fix", files=["src/Foo.java"], diff=DIFF)
    review = FixTry(n=1, round=0, source="review", result="improved", applied="edits", files=["pom.xml"])
    return [ErrorEntry(id="e1", case_id=CASE, file="src/Foo.java", line=1, message="cannot find symbol",
                       seen="build", status="resolved", tries=[t1, t2], resolved="r2 try 2"),
            ErrorEntry(id="r1", case_id="4444444444", file="pom.xml", message="sourceEncoding MS949", seen="review",
                       status="resolved", resolved="review", tries=[review])]


def test_labels():
    t = FixTry(n=3, round=2, source="llm-edit", result="partial", applied="edits")
    assert edits_label(CASE, 3) == f"edits:{CASE}#3" and edits_label("", 0) == "edits:?#?"
    assert is_edits_try(t, "edits") and not is_edits_try(t, "script")
    assert not is_edits_try(FixTry(n=1, round=0, source="review", result="improved"), "edits")
    assert try_text(t, CASE, "edits") == f"edits:{CASE}#3 partial"
    assert try_text(t) == try_text(t, CASE, "script") == "3 llm-edit edits partial"       # script mode unchanged
    assert tries_text([t, t], CASE, "edits") == f"edits:{CASE}#3 partial · edits:{CASE}#3 partial"
    rows = {r.case_id: r for r in case_rows(edits_entries())}
    assert deciding_label(rows[CASE], "edits") == f"edits:{CASE}#2"
    assert deciding_label(rows[CASE], "script") == "" and deciding_label(rows["4444444444"], "edits") == ""


def test_result_line_gets_fix_only_off_the_default():
    base = report_result_line(_report(llm=3, flags=["tests-not-compiled"]), 0)
    assert base == ("SBMIG-RESULT run=run-1 exit=0 build=ok unresolved=0 blocked=0 open=0 resolved=0 llm=3 "
                    "attn=tests-not-compiled")                                   # byte-identical default line
    assert report_result_line(_report(llm=3, flags=["tests-not-compiled"], fix_mode="script"), 0) == base
    assert report_result_line(_report(llm=3, flags=["tests-not-compiled"], fix_mode="edits"), 0) == base + " fix=edits"
    # the new token goes last, after review= / major=
    line = result_line("r", 2, False, review="issues", major=2, fix_mode="edits", open_errors=1, resolved_errors=0)
    assert line.endswith(" review=issues major=2 fix=edits")
    assert result_line("r", 0, True, fix_mode=None) == result_line("r", 0, True, fix_mode="script") == \
        result_line("r", 0, True)


def test_edits_mode_report_sections():
    r = report_with(edits_entries())
    r.fix_mode = "edits"
    md = render_markdown(r)
    assert f"| edits:{CASE}#1 no_change · edits:{CASE}#2 build_ok | r2 try 2 |" in md        # the tries cell
    assert "| 1 review edits improved | review |" in md                                     # the review's try as before
    fx = section(md, FIXES_EN)
    assert "| case | errors | cause | solution | changes (files, +/−) | edits | fix try |" in fx
    assert f"| `edits:{CASE}#2` |" in fx and "fix_vN.py" not in fx
    assert "_fix.mode edits: no fix scripts; `edits:<case>#<n>` is try n of the case in `errors.md`" in fx
    assert "SBMIG-RESULT" in md and " fix=edits` |" in md
    ko = render_markdown(r, language="ko")
    assert "| 케이스 | 에러 | 원인 | 해결 방법 | 변경 파일 (+/−) | 수정 | 수정 시도 |" in ko
    assert "fix.mode edits: 수정 스크립트 없음." in ko


def test_script_mode_report_is_unchanged():
    """The script-mode report of the shared fixture renders exactly as with a report that never heard of fix_mode."""
    r = report_with(entries())
    assert r.fix_mode == "script"
    md = render_markdown(r)
    assert "fix try" not in md and "edits:" not in md and " fix=" not in md and "fix mode" not in md
    assert "| case | errors | cause | solution | changes (files, +/−) | edits | script |" in md
    assert "1 llm-new v1 edits improved" in md and "v1 `cases/7a52b1cea8/fix_v1.py` |" in md


def test_usage_and_cost_names_the_edits_mode(cfg):
    r = _priced_report(cfg)
    en = render_markdown(r)
    assert "fix mode:" not in en                                                # script mode: nothing added
    r.fix_mode = "edits"
    en = render_markdown(r)
    sec = section(en, "## LLM usage and cost")
    assert sec.startswith("## LLM usage and cost\n\nfix mode: edits — no fix scripts generated, stored or reused\n\n"
                          "| Model |")
    ko = section(render_markdown(r, language="ko"), "## LLM 사용량과 비용")
    assert "수정 방식: edits — 수정 스크립트를 만들지도, 저장·재사용하지도 않음" in ko and "fix mode:" not in ko


def test_usage_line_needs_usage():
    """No LLM usage at all → no "LLM usage and cost" section in either mode (nothing to compare)."""
    r = report_with(edits_entries())
    r.fix_mode = "edits"
    r.llm_usage = LLMUsage()
    r.llm_cost = compute_llm_cost([LLMUsage(model="m", calls=0)], None)
    assert "## LLM usage and cost" not in render_markdown(r)


def test_console_summary_shows_the_labels():
    r = report_with(edits_entries())
    r.fix_mode = "edits"
    console = Console(record=True, width=220)
    console_summary(r, console)
    text = console.export_text()
    assert f"edits:{CASE}#1 no_change; edits:{CASE}#2 build_ok" in text and f"edits:{CASE}#2" in text
    assert "fix try" in text and "fix mode: edits (no fix scripts)" in text
    plain = Console(record=True, width=220)
    console_summary(report_with(edits_entries()), plain)
    out = plain.export_text()
    assert "edits:" not in out and "fix mode" not in out and "1 llm-edit edits no_change" in out
