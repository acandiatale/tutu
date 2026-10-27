"""v2.1b structure review: the critical-issue edits carry their `desc`, a script its `<changes>` list → the review's
answer (`StructureReview.answer.changes`), the "structure review 수정: <file> …" line shows the description, the review
prompt teaches the rule in the report language."""
from __future__ import annotations

import json
import logging

from sbmig.core.models import EditNote
from sbmig.review import parse_review_answer
from sbmig.review.reviewer import system_prompt

from .test_review_edits import REVIEW, SCRIPT, answer, run
from .test_reviewer import make_ws

DESC = "module/comp-jar/pom.xml 의 <properties> 에서 project.build.sourceEncoding 을 MS949 로 설정합니다"
EDITS = [{"op": "pom", "path": "module/comp-jar/pom.xml", "desc": DESC,
          "set_property": {"name": "project.build.sourceEncoding", "value": "MS949"}}]
CHANGES = [{"path": "module/comp-jar/pom.xml", "where": "<properties>", "change": "sourceEncoding 을 MS949 로 설정합니다"}]


def test_parse_desc_and_changes():
    text = answer(edits=EDITS, extra=f"<changes>{json.dumps(CHANGES, ensure_ascii=False)}</changes>\n")
    p = parse_review_answer(text)
    assert p.ok and p.edits[0].desc == DESC and p.changes_described and p.changes_problem == ""
    assert p.changes == [EditNote(path="module/comp-jar/pom.xml", where="<properties>",
                                  change="sourceEncoding 을 MS949 로 설정합니다")]   # the list already names the pom
    # edits with desc, no <changes>: the descs are the notes, but a script without <changes> is "not described"
    p = parse_review_answer(answer(edits=EDITS))
    assert [n.change for n in p.changes] == [DESC] and not p.changes_described
    # a v2 / v2.1 answer: no notes
    assert parse_review_answer(answer()).changes == []
    # edits and changes inside the review JSON object are accepted too
    review = dict(REVIEW, edits=EDITS, changes=CHANGES)
    p = parse_review_answer(f"<review>{json.dumps(review, ensure_ascii=False)}</review>")
    assert p.edits[0].desc == DESC and p.changes_described and p.changes[0].where == "<properties>"
    # the <changes> block never leaks into the review JSON
    p = parse_review_answer(answer(edits=EDITS, extra=f"<changes>{json.dumps(CHANGES)}</changes>")
                            .replace("<review>", "").replace("</review>", ""))
    assert p.ok and [i.severity for i in p.issues] == ["critical", "warning"]


def test_applied_review_edits_log_their_description(tmp_path, cfg, caplog):
    caplog.set_level(logging.INFO, logger="sbmig")
    ws = make_ws(tmp_path)
    text = answer(edits=EDITS, extra=f"<changes>{json.dumps(CHANGES, ensure_ascii=False)}</changes>\n")
    rv, _ = run(cfg, ws, text)
    assert rv.script_result is not None and rv.script_result.status == "ok" and not rv.reverted
    a = rv.answer
    assert a is not None and a.analysis == "MS949 sources, UTF-8 build"          # described: no [sbmig] note
    assert [n.path for n in a.changes] == ["module/comp-jar/pom.xml"] and a.edits[0].desc == DESC
    msgs = [r.getMessage() for r in caplog.records if r.name.startswith("sbmig.review")]
    line = next(m for m in msgs if m.startswith("structure review 수정: module/comp-jar/pom.xml"))
    assert line.startswith("structure review 수정: module/comp-jar/pom.xml (+") and \
        "— <properties>: sourceEncoding 을 MS949 로 설정합니다 … diff " in line
    assert "declare project.build" not in line                                   # the desc, not the solution
    applying = next(m for m in msgs if m.startswith("structure review: applying 1 edit(s)"))
    assert DESC[:40] in applying                                                 # summarize_edits shows the desc


def test_desc_without_changes_is_used_for_the_log_line(tmp_path, cfg, caplog):
    caplog.set_level(logging.INFO, logger="sbmig")
    rv, _ = run(cfg, make_ws(tmp_path), answer(edits=EDITS, script=""))
    assert rv.answer is not None and rv.answer.analysis == "MS949 sources, UTF-8 build"   # no script: nothing missing
    msgs = [r.getMessage() for r in caplog.records if r.name.startswith("sbmig.review")]
    assert any(m.startswith("structure review 수정: module/comp-jar/pom.xml") and f"— {DESC}" in m for m in msgs)


def test_script_without_changes_gets_the_note(tmp_path, cfg):
    rv, _ = run(cfg, make_ws(tmp_path), answer(edits=EDITS, script=SCRIPT))
    assert rv.answer.analysis.endswith("[sbmig] the answer did not describe its changes")


def test_review_prompt_teaches_the_description_rule(cfg):
    cfg.report.language = "ko"
    text = system_prompt(cfg)
    for part in ("business team", "<파일> 의 <위치> 에서 <무엇을> <어떻게>", 'Every edit object also takes `"desc"`',
                 "`<changes>` is REQUIRED", "Write the sentences in Korean (한국어);", '"desc": "shop-back/pom.xml 의'):
        assert part in text, part
    assert "{{" not in text
    cfg.report.language = "en"
    assert "Write the sentences in the language of the project's code comments" in system_prompt(cfg)
    # the example answer of the prompt parses with its descs and changes
    start = text.index("<analysis>원인")
    p = parse_review_answer('<review>{"verdict": "issues", "issues": [{"severity": "critical", "problem": "x"}]}'
                            "</review>\n" + text[start:text.index("</script>", start) + len("</script>")])
    assert p.ok and len(p.edits) == 2 and all(e.desc for e in p.edits) and p.changes_described
