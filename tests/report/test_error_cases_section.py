"""report.md "Build errors and how each was handled" (error cases, 2026-10-23): counts, row order, the 40-row cap,
the LLM text-replace line, escaping, Korean rendering, no section without records."""
from __future__ import annotations

import json
from pathlib import Path

from sbmig.core.models import ErrorCaseRecord, ReplacePair
from sbmig.core.workspace import Workspace
from sbmig.report import render_markdown, write_report
from sbmig.report.markdown import ERROR_CASE_MAX_ROWS

from .test_report import sample_report

EN_TITLE = "## Build errors and how each was handled"
KO_TITLE = "## 빌드 오류별 처리 내역"


def _case(i: str, status: str, **kw) -> ErrorCaseRecord:
    kw.setdefault("message", f"message {i}")
    return ErrorCaseRecord(id=i, status=status, **kw)


def cases() -> list[ErrorCaseRecord]:
    """One record per status, deliberately in the reverse of the table order."""
    return [
        _case("e-ind", "resolved_indirectly", file="src/main/java/a/C.java", message="incompatible types",
              fix_method="plan_step", resolved_by="step:spring.rewrite"),
        _case("e-llm", "resolved", file="src/main/java/a/A.java", line=3,
              message="package javax.persistence does not exist", fix_method="llm", resolver_id="gpt-x",
              description="LLM `gpt-x`: Replaced the javax import with jakarta. Then tidied the imports.",
              change_kind="text_replace", replace_rule_candidate=True, attempt_count=1, llm_attempts=1,
              replace_pairs=[ReplacePair(old="javax.persistence", new="jakarta.persistence", count=2)]),
        _case("e-hdl", "resolved", file="pom.xml", message="lombok too old", fix_method="handler",
              resolver_id="lombok-version-floor", attempt_count=1,
              description="handler `lombok-version-floor`: raised lombok to 1.18.36. More text."),
        _case("e-kb", "resolved", file="src/main/java/a/D.java", line=7, message="cannot find symbol",
              fix_method="kb_patch", resolver_id="case-12", description="replayed the stored patch", attempt_count=1),
        _case("e-nv", "not_visible", module="core", message="weird"),
        _case("e-blk", "blocked", artifact="com.acme:legacy:1.0", message="could not resolve",
              block_reason="artifact_not_found", suggestion="publish com.acme:legacy:1.0 to the company Nexus"),
        _case("e-unr", "unresolved", file="src/main/java/a/B.java", line=9, message="cannot find symbol Foo",
              failure_reason="`llm` attempt `spring-1-1` `no_change`: the targeted error(s) were still there after "
                             "the rebuild", attempt_count=2, llm_attempts=2),
    ]


COUNTS = {"resolved": 3, "resolved_indirectly": 1, "blocked": 1, "unresolved": 1, "not_visible": 1,
          "llm_text_replace": 1}


def report_with(records: list[ErrorCaseRecord], counts: dict[str, int] | None = None, success: bool = False):
    r = sample_report(success=success)
    r.error_cases = records
    r.error_case_counts = dict(COUNTS if counts is None else counts)
    return r


def section(md: str, title: str = EN_TITLE) -> str:
    start = md.index(title)
    nxt = md.find("\n## ", start + len(title))
    return md[start: nxt if nxt != -1 else len(md)]


def rows(sec: str) -> list[str]:
    lines = [ln for ln in sec.splitlines() if ln.startswith("| ")]
    return lines[1:]                                                     # without the header


def test_section_placed_after_unresolved_with_counts_table_and_note():
    md = render_markdown(report_with(cases()))
    assert md.index("## Unresolved errors") < md.index(EN_TITLE) < md.index("## Manual follow-ups")
    sec = section(md)
    assert "7 build error(s) seen in this run, one record each." in sec
    assert "- by status: resolved 3, resolved_indirectly 1, blocked 1, unresolved 1, not_visible 1" in sec
    assert "- fixed by (resolved errors): handler 1, kb_patch 1, llm 1, plan_step 1" in sec
    assert ("- 1 LLM fix(es) were plain text replaces — candidates for a deterministic rule "
            "(`sbmig kb errors replace-candidates`)") in sec
    assert "| status | where | message | how resolved / why not | attempts |" in sec
    assert "`error_cases.csv`" in sec and "`error_cases.jsonl`" in sec and "`fix_attempts.jsonl`" in sec
    assert "`sbmig kb errors`" in sec
    body = rows(sec)
    assert len(body) == 7
    by_id = dict(zip(["unr", "blk", "nv", "llm", "hdl", "kb", "ind"], body))
    assert by_id["unr"] == ("| unresolved | `src/main/java/a/B.java:9` | cannot find symbol Foo | `llm` attempt "
                            "`spring-1-1` `no_change`: the targeted error(s) were still there after the rebuild | "
                            "2 (llm 2) |")
    assert by_id["blk"] == ("| blocked | `com.acme:legacy:1.0` | could not resolve | `artifact_not_found`: publish "
                            "com.acme:legacy:1.0 to the company Nexus | 0 |")
    assert "| not_visible | `core` | weird | the last build stopped before this error's stage — status unknown |" \
        in by_id["nv"]
    # resolved: the description's first sentence (it names the resolver, so no prefix) + the replace pair
    assert ("| LLM `gpt-x`: Replaced the javax import with jakarta. · replace `javax.persistence` → "
            "`jakarta.persistence` | 1 (llm 1) |") in by_id["llm"]
    assert "Then tidied" not in sec
    assert "| handler `lombok-version-floor`: raised lombok to 1.18.36. | 1 |" in by_id["hdl"]
    assert "More text" not in sec
    # the description does not name the resolver → method + resolver first
    assert "| kb_patch `case-12`: replayed the stored patch | 1 |" in by_id["kb"]
    # plan step: resolver from resolved_by "step:<id>"
    assert "| resolved_indirectly | `src/main/java/a/C.java` | incompatible types | plan_step `spring.rewrite` | 0 |" \
        == by_id["ind"]


def test_rows_ordered_unresolved_blocked_not_visible_resolved_indirect():
    sec = section(render_markdown(report_with(cases())))
    statuses = [r.split(" | ")[0][2:] for r in rows(sec)]
    assert statuses == ["unresolved", "blocked", "not_visible", "resolved", "resolved", "resolved",
                        "resolved_indirectly"]
    # stable inside one status: the input order of the three resolved records is kept
    assert [r.split(" | ")[1] for r in rows(sec)][3:6] == ["`src/main/java/a/A.java:3`", "`pom.xml`",
                                                            "`src/main/java/a/D.java:7`"]


def test_capped_at_40_rows_with_more_line_and_unresolved_first():
    many = [_case(f"r{i}", "resolved", file=f"src/R{i}.java", fix_method="llm", resolver_id="m",
                  description="fixed") for i in range(43)]
    many += [_case("u1", "unresolved", file="src/U1.java", failure_reason="gave up"),
             _case("u2", "unresolved", file="src/U2.java", failure_reason="gave up")]
    sec = section(render_markdown(report_with(many, counts={"resolved": 43, "unresolved": 2})))
    body = rows(sec)
    assert len(body) == ERROR_CASE_MAX_ROWS == 40
    assert body[0].startswith("| unresolved | `src/U1.java` |") and body[1].startswith("| unresolved | `src/U2.java` |")
    assert "… 5 more — see `error_cases.csv`" in sec
    assert "45 build error(s) seen in this run" in sec
    assert "- by status: resolved 43, unresolved 2" in sec
    assert "LLM fix(es) were plain text replaces" not in sec                 # no llm_text_replace count
    exact = section(render_markdown(report_with(many[:40], counts={"resolved": 40})))
    assert len(rows(exact)) == 40 and "more — see" not in exact


def test_text_replace_line_only_when_positive_and_counts_fall_back_to_records():
    recs = cases()
    sec = section(render_markdown(report_with(recs, counts={"llm_text_replace": 0})))
    assert "plain text replaces" not in sec
    # no status counts in error_case_counts → counted from the records
    assert "- by status: resolved 3, resolved_indirectly 1, blocked 1, unresolved 1, not_visible 1" in sec
    sec = section(render_markdown(report_with(recs, counts={**COUNTS, "llm_text_replace": 4})))
    assert "- 4 LLM fix(es) were plain text replaces" in sec
    # nothing resolved → no fix-method line
    only_open = [c for c in recs if c.status not in ("resolved", "resolved_indirectly")]
    sec = section(render_markdown(report_with(only_open, counts={})))
    assert "fixed by" not in sec and "- by status: blocked 1, unresolved 1, not_visible 1" in sec


def test_no_section_without_error_cases():
    for lang, title in (("en", EN_TITLE), ("ko", KO_TITLE)):
        for success in (False, True):
            md = render_markdown(report_with([], counts={}, success=success), language=lang)
            assert title not in md
            assert "error_cases.csv" not in md
    md = render_markdown(report_with([], counts=COUNTS))                     # counts alone do not make a section
    assert EN_TITLE not in md


def test_pipes_and_newlines_are_escaped_once():
    rec = _case("p", "resolved", file="src/P.java", line=1, message="bad | thing\nsecond line\twith tab",
                fix_method="llm", resolver_id="gpt-y", description="swapped a|b.\nnext",
                replace_pairs=[ReplacePair(old="a|b", new="c|d"), ReplacePair(old="x", new="y"),
                               ReplacePair(old="p", new="q")])
    blk = _case("b", "blocked", file="src/Q.java", message="m", block_reason="repeated_failure",
                failure_reason="blocked (`repeated_failure`): tried | twice\nstill failing")
    sec = section(render_markdown(report_with([rec, blk], counts={})))
    body = rows(sec)
    assert len(body) == 2 and all(r.endswith(" |") for r in body)
    resolved_row = next(r for r in body if r.startswith("| resolved "))
    assert "| bad \\| thing second line with tab |" in resolved_row
    assert "llm `gpt-y`: swapped a\\|b. · replace `a\\|b` → `c\\|d`, `x` → `y` (+1) |" in resolved_row
    assert "\\\\|" not in sec                                                # never escaped twice
    blocked_row = next(r for r in body if r.startswith("| blocked "))
    # block_reason already in the failure reason → not repeated
    assert "| blocked (`repeated_failure`): tried \\| twice still failing | 0 |" in blocked_row
    assert blocked_row.count("repeated_failure") == 1


def test_blocked_without_failure_reason_uses_suggestion_and_unresolved_fallback():
    sec = section(render_markdown(report_with([
        _case("b", "blocked", message="m", block_reason="external_javax_api", suggestion="ask for a Jakarta build"),
        _case("b2", "blocked", message="m2", block_reason="llm_unavailable"),
        _case("u", "unresolved", message="m3", attempt_count=0)], counts={})))
    assert "| blocked | - | m | `external_javax_api`: ask for a Jakarta build | 0 |" in sec
    assert "| blocked | - | m2 | `llm_unavailable` | 0 |" in sec
    assert "| unresolved | - | m3 | no fix attempt recorded | 0 |" in sec


def test_korean_rendering():
    ko = render_markdown(report_with(cases()), language="ko")
    assert EN_TITLE not in ko and "Build errors" not in ko
    assert ko.index("## 미해결 오류") < ko.index(KO_TITLE) < ko.index("## 수동 후속 조치")
    sec = section(ko, KO_TITLE)
    assert "이번 실행에서 확인한 빌드 오류 7건 (오류마다 기록 1건)." in sec
    # statuses / methods are identifiers: they stay English
    assert "- 상태별: resolved 3, resolved_indirectly 1, blocked 1, unresolved 1, not_visible 1" in sec
    assert "- 해결 수단별 (해결된 오류): handler 1, kb_patch 1, llm 1, plan_step 1" in sec
    assert "- LLM 수정 1건은 단순 텍스트 치환이었다" in sec and "`sbmig kb errors replace-candidates`" in sec
    assert "| 상태 | 위치 | 메시지 | 해결 방법 / 미해결 사유 | 시도 |" in sec
    assert "· 치환 `javax.persistence` → `jakarta.persistence`" in sec
    assert "마지막 빌드가 이 오류의 단계 전에 멈춤 — 상태를 알 수 없음" in sec
    assert "`error_cases.csv`(Excel로 열림)" in sec and "`fix_attempts.jsonl`" in sec
    for en in ("seen in this run", "by status", "fixed by", "plain text replaces", "how resolved / why not",
               "status unknown", "Every error with its full history", "replace `"):
        assert en not in sec, en
    many = [_case(f"r{i}", "unresolved", file=f"src/R{i}.java") for i in range(41)]
    sec = section(render_markdown(report_with(many, counts={}), language="ko"), KO_TITLE)
    assert "… 외 1건 — `error_cases.csv` 참조" in sec and "기록된 수정 시도 없음" in sec


def test_write_report_md_and_json_carry_error_cases(tmp_path):
    rep = report_with(cases())
    ws = Workspace(tmp_path / "p", tmp_path / "run", "run-1")
    paths = write_report(rep, ws)
    assert EN_TITLE in Path(paths["report_md"]).read_text(encoding="utf-8")
    data = json.loads(Path(paths["report_json"]).read_text(encoding="utf-8"))
    assert [e["id"] for e in data["error_cases"]] == [c.id for c in cases()]
    assert data["error_case_counts"] == COUNTS
