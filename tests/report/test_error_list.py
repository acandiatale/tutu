"""v2.1 report.md centre: "Error list" (one table per `seen` group) and "Fixes by case" from
`RunReport.error_entries`; counts, the tries cell, diff +/- per file, caps, escaping, Korean, empty lists."""
from __future__ import annotations

import json
from pathlib import Path

from rich.console import Console

from sbmig.core.models import ErrorEntry, ErrorKind, FixTry
from sbmig.core.workspace import Workspace
from sbmig.report import console_summary, render_markdown, write_report
from sbmig.report.errors import (case_rows, diff_stats, open_count, resolved_count, seen_groups, stats_text,
                                 status_counts, try_text)
from sbmig.report.markdown import ERROR_LIST_MAX_ROWS

from .test_report import sample_report

EN_TITLE = "## Error list"
KO_TITLE = "## 에러 목록"
FIXES_EN = "## Fixes by case"
FIXES_KO = "## 케이스별 해결"
POM_DIFF = ("diff --git a/module/comp-jar/pom.xml b/module/comp-jar/pom.xml\n--- a/module/comp-jar/pom.xml\n"
            "+++ b/module/comp-jar/pom.xml\n@@ -10,3 +10,9 @@\n <dependencies>\n+    <dependency>\n"
            "+      <groupId>jakarta.xml.bind</groupId>\n+      <artifactId>jakarta.xml.bind-api</artifactId>\n"
            "+    </dependency>\n-    <!-- old -->\n")
JAVA_DIFF = ("diff --git a/src/A.java b/src/A.java\n--- a/src/A.java\n+++ b/src/A.java\n@@ -1 +1 @@\n"
             "-import javax.x.Y;\n+import jakarta.x.Y;\n")


def _try(n: int, rnd: int, source: str, result: str, **kw) -> FixTry:
    return FixTry(n=n, round=rnd, source=source, result=result, **kw)


def entries() -> list[ErrorEntry]:
    """Deliberately out of pipeline order: fix r2, build, compile ×3, restructure, review."""
    t1 = _try(1, 1, "llm-new", "improved", script_version=1, applied="edits", analysis="JAXB left the JDK",
              solution="declare jakarta.xml.bind-api", files=["module/comp-jar/pom.xml"], diff=POM_DIFF,
              commit="a1b2c3d", llm_calls=1)
    t_bad = _try(1, 1, "llm-new", "regressed", applied="edits", analysis="wrong guess", files=["src/A.java"],
                 diff=JAVA_DIFF)
    t_ok = _try(2, 2, "llm-augment", "build_ok", script_version=2, applied="script", analysis="javax import left",
                solution="rename the import | then compile", files=["src/A.java"], diff=JAVA_DIFF)
    t_edit = _try(1, 2, "llm-edit", "no_change", applied="edits", no_script_reason="generated code differs per project",
                  files=["src/G.java"])
    return [
        ErrorEntry(id="f1", case_id="9999999999", file="src/Late.java", line=4, message="new in round 2",
                   seen="fix r2", status="unresolved", tries=[t_edit]),
        ErrorEntry(id="b1", case_id="5555555555", file="src/B.java", line=9, message="cannot find symbol | x",
                   seen="build", status="unresolved"),
        ErrorEntry(id="c1", case_id="7a52b1cea8", l2="compile|package_does_not_exist|jakarta.xml.bind",
                   kind=ErrorKind.COMPILE, file="a/B.java", line=6, message="package jakarta.xml.bind does not exist",
                   seen="compile", status="resolved", tries=[t1], resolved="r1 try 1",
                   script="cases/7a52b1cea8/fix_v1.py"),
        ErrorEntry(id="c2", case_id="7a52b1cea8", file="a/C.java", line=8,
                   message="package jakarta.xml.bind does not exist", seen="compile", status="resolved", tries=[t1],
                   resolved="r1 try 1", script="cases/7a52b1cea8/fix_v1.py"),
        ErrorEntry(id="c3", case_id="3333333333", file="src/A.java", line=1, message="package javax.x does not exist",
                   seen="compile", status="resolved", tries=[t_bad, t_ok], resolved="r2 try 2"),
        ErrorEntry(id="p1", seen="restructure", status="pom-verify", file="module/comp-jar/pom.xml",
                   note="parent → base-parent 4.0.0"),
        ErrorEntry(id="r1", case_id="4444444444", seen="review", status="resolved", file="module/comp-jar/pom.xml",
                   message="sourceEncoding MS949", resolved="review",
                   tries=[_try(1, 0, "review", "improved", applied="edits", files=["module/comp-jar/pom.xml"],
                               diff=POM_DIFF.replace("<!-- old -->", "x"))]),
    ]


def report_with(es: list[ErrorEntry], **kw):
    r = sample_report(**kw)
    r.error_entries = es
    return r


def section(md: str, title: str) -> str:
    start = md.index(title)
    nxt = md.find("\n## ", start + 3)
    return md[start: nxt if nxt != -1 else len(md)]


# ------------------------------------------------------------------------------------------------ data helpers
def test_counts_groups_and_tries_text():
    es = entries()
    assert dict(status_counts(es)) == {"unresolved": 2, "pom-verify": 1, "resolved": 4}
    assert list(status_counts(es)) == ["unresolved", "pom-verify", "resolved"]
    assert open_count(es) == 2 and resolved_count(es) == 4
    assert list(seen_groups(es)) == ["compile", "restructure", "review", "build", "fix r2"]
    assert [i for i, _ in seen_groups(es)["compile"]] == [1, 2, 3]          # # numbers the entries in that order
    assert [i for i, _ in seen_groups(es)["fix r2"]] == [7]
    assert try_text(es[2].tries[0]) == "1 llm-new v1 edits improved"
    assert try_text(_try(2, 3, "kb-script", "build_ok", script_version=2, applied="script")) == "2 kb-script v2 build_ok"
    assert status_counts([]) == {} and seen_groups([]) == {} and case_rows([]) == []


def test_diff_stats_per_file():
    s = diff_stats(POM_DIFF + JAVA_DIFF)
    assert s == {"module/comp-jar/pom.xml": [4, 1], "src/A.java": [1, 1]}
    assert stats_text(s) == "`module/comp-jar/pom.xml` (+4 −1), `src/A.java` (+1 −1)"
    assert stats_text(s, code=False, minus="-", limit=1) == "module/comp-jar/pom.xml (+4 -1), (+1 more)"
    bare = "@@ -1 +1 @@\n-a\n+b\n+c\n"                                        # no headers: the only listed file
    assert diff_stats(bare, ["x/Y.java"]) == {"x/Y.java": [2, 1]}
    assert diff_stats("", ["pom.xml"]) == {"pom.xml": [0, 0]} and stats_text(diff_stats("", ["pom.xml"])) == "`pom.xml`"
    gone = "diff --git a/old.txt b/old.txt\ndeleted file mode 100644\n--- a/old.txt\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-a\n-b\n"
    assert diff_stats(gone) == {"old.txt": [0, 2]}


def test_case_rows_dedupe_tries_and_pick_the_deciding_one():
    rows = {r.case_id: r for r in case_rows(entries())}
    assert list(rows) == ["7a52b1cea8", "3333333333", "4444444444", "9999999999"]   # no row for a case never tried
    jaxb = rows["7a52b1cea8"]
    assert jaxb.entries == 2 and jaxb.resolved == 2 and len(jaxb.tries) == 1        # one try shared by both entries
    assert jaxb.analysis == "JAXB left the JDK" and jaxb.solution == "declare jakarta.xml.bind-api"
    assert jaxb.changes == {"module/comp-jar/pom.xml": [4, 1]}
    assert jaxb.script == "cases/7a52b1cea8/fix_v1.py" and jaxb.script_version == 1
    javax = rows["3333333333"]                                   # the regressed try is rolled back: not a change
    assert javax.analysis == "javax import left" and javax.changes == {"src/A.java": [1, 1]}
    assert javax.script == "cases/3333333333/fix_v2.py" and javax.script_version == 2
    late = rows["9999999999"]
    assert late.no_script_reason == "generated code differs per project" and late.open == 1 and not late.script


# ------------------------------------------------------------------------------------------------ report.md
def test_error_list_section_english():
    md = render_markdown(report_with(entries()))
    sec = section(md, EN_TITLE)
    assert "7 error(s) in this run, one line each" in sec and "unresolved 2, pom-verify 1, resolved 4." in sec
    heads = [ln for ln in sec.splitlines() if ln.startswith("### ")]
    assert heads == ["### compile — `mvn compile` after OpenRewrite (3)",
                     "### restructure — pom corrections, verified by the next build (1)",
                     "### review — structure review (1)", "### build — first `mvn clean package`, new errors only (1)",
                     "### fix r2 — new errors of round 2's build (1)"]
    assert ("| 1 | `7a52b1cea8` | `a/B.java:6` | package jakarta.xml.bind does not exist | resolved | "
            "1 llm-new v1 edits improved | r1 try 1 |") in sec
    assert ("| 3 | `3333333333` | `src/A.java:1` | package javax.x does not exist | resolved | "
            "1 llm-new edits regressed · 2 llm-augment v2 build_ok | r2 try 2 |") in sec
    assert "| 4 | - | `module/comp-jar/pom.xml` | parent → base-parent 4.0.0 | pom-verify | - | - |" in sec
    assert "| 6 | `5555555555` | `src/B.java:9` | cannot find symbol \\| x | unresolved | - | - |" in sec
    assert "Last build log: `/logs/verify.log`" in sec
    # header row, order of the sections, the dropped v2 tables
    assert "| **Error list** | 7 error(s): unresolved 2, pom-verify 1, resolved 4 (see \"Error list\") |" in md
    assert md.index("## Handover") < md.index(EN_TITLE) < md.index(FIXES_EN)
    for gone in ("## Phases", "## Fix attempts", "## Build errors and how each was handled", "## Unresolved errors",
                 "## Escalated clusters", "error_cases.csv", "fix_attempts.jsonl"):
        assert gone not in md, gone


def test_fixes_by_case_section():
    md = render_markdown(report_with(entries()))
    sec = section(md, FIXES_EN)
    assert "| case | errors | cause | solution | changes (files, +/−) | script |" in sec
    assert ("| `7a52b1cea8` package jakarta.xml.bind does not exist | 2/2 resolved | JAXB left the JDK | "
            "declare jakarta.xml.bind-api | `module/comp-jar/pom.xml` (+4 −1) | v1 `cases/7a52b1cea8/fix_v1.py` |") in sec
    assert "| javax import left | rename the import \\| then compile | `src/A.java` (+1 −1) | v2 `cases/3333333333/fix_v2.py` |" in sec
    assert ("| `9999999999` new in round 2 | 0/1 resolved | - | (no solution text) generated code differs per project | "
            "`src/G.java` | no script: generated code differs per project |") in sec
    assert "`<kb>/cases/<case>/fix_vN.py`" in sec


def test_korean_sections():
    md = render_markdown(report_with(entries()), language="ko")
    sec = section(md, KO_TITLE)
    assert "이번 실행의 에러 7건" in sec and "### compile — OpenRewrite 뒤 `mvn compile` (3)" in sec
    assert "### fix r2 — 라운드 2 빌드의 새 에러 (1)" in sec and "| # | 케이스 | 파일:줄 | 메시지 | 상태 | 시도 | 해결 |" in sec
    fx = section(md, FIXES_KO)
    assert "| 케이스 | 에러 | 원인 | 해결 방법 | 수정 (파일, +/−) | 스크립트 |" in fx and "2/2 해결" in fx
    assert "스크립트 없음: generated code differs per project" in fx
    assert "| **에러 목록** | 7건: unresolved 2, pom-verify 1, resolved 4 (\"에러 목록\" 참조) |" in md
    assert EN_TITLE not in md and FIXES_EN not in md


def test_group_cap_points_at_errors_md():
    many = [ErrorEntry(id=f"e{i}", case_id="c", file=f"src/F{i}.java", message=f"m{i}", seen="compile")
            for i in range(ERROR_LIST_MAX_ROWS + 7)]
    sec = section(render_markdown(report_with(many)), EN_TITLE)
    assert f"| {ERROR_LIST_MAX_ROWS} | `c` |" in sec and f"| {ERROR_LIST_MAX_ROWS + 1} | " not in sec
    assert "… 7 more in this group — see `errors.md`" in sec


def test_empty_error_list_falls_back_to_the_unresolved_errors():
    md = render_markdown(sample_report())                              # no entries, 5 unresolved BuildErrors
    sec = section(md, EN_TITLE)
    assert "No error list was kept for this run; 5 error(s) were left after the last build" in sec
    assert "### `src/A.java`" in sec and "L3: cannot find symbol — symbol: `class Foo` — location: class A" in sec
    assert FIXES_EN not in md and "| **Error list** |" not in md
    ok = render_markdown(sample_report(success=True))
    assert "None. The final build passed." in section(ok, EN_TITLE)


def test_review_section_shows_what_the_review_changed():
    from .test_review_report import review

    r = report_with(entries())
    r.structure_review = review()
    sec = section(render_markdown(r), "## Structure review")
    assert "- changed: `module/comp-jar/pom.xml` (+4 −1)" in sec


def test_console_summary_counts_and_fixes(tmp_path: Path):
    console = Console(record=True, width=200)
    r = report_with(entries())
    console_summary(r, console)
    text = console.export_text()
    assert ("error list: 7 - unresolved 2, pom-verify 1, resolved 4 (compile 3, restructure 1, review 1, build 1, "
            "fix r2 1)") in text
    assert "fixes by case (4)" in text and "2/2 resolved" in text and "module/comp-jar/pom.xml (+4 -1)" in text
    assert "unresolved (2)" in text and "src/B.java:9" in text and "phases" not in text


def test_report_json_carries_the_entries(tmp_path: Path):
    ws = Workspace(tmp_path / "p", tmp_path / "run", "run-1")
    paths = write_report(report_with(entries()), ws)
    data = json.loads(Path(paths["report_json"]).read_text(encoding="utf-8"))
    assert [e["id"] for e in data["error_entries"]][:3] == ["f1", "b1", "c1"]
    assert data["error_entries"][2]["tries"][0]["source"] == "llm-new"
    assert "| **Error list** |" in Path(paths["report_md"]).read_text(encoding="utf-8")
