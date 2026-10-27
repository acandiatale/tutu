"""v2.1 structure review: "검증하고, 문제가 있으면 직접 고쳐라" — the four-part answer next to the review JSON, the
edits applied through `sbmig.script.apply_edits` (the real script process), uncommitted; guards (no sources, poms must
parse), the apply retry, the edits-retry, `StructureReview.answer`, `[review]` log lines."""
from __future__ import annotations

import json
import logging

from sbmig.core.models import ScriptProposal
from sbmig.core.stage import StageFilter
from sbmig.review import parse_review_answer
from sbmig.review.reviewer import APPLY_RETRY_MESSAGE, StructureReviewer

from .test_reviewer import COMP_POM, ROOT_POM, FakeLLM, FakeRunner, make_ws, profile

REVIEW = {"verdict": "issues", "issues": [
    {"severity": "critical", "file": "module/comp-jar/pom.xml", "problem": "no source encoding for MS949 sources",
     "fix_hint": "set project.build.sourceEncoding"},
    {"severity": "warning", "file": "", "problem": "old plugin", "fix_hint": ""}],
    "analysis": "The comp module builds MS949 sources with UTF-8."}
EDITS = [{"op": "pom", "path": "module/comp-jar/pom.xml",
          "set_property": {"name": "project.build.sourceEncoding", "value": "MS949"}}]
SCRIPT = ('def fix(p):\n    pom = p.pom("module/comp-jar/pom.xml")\n'
          '    if pom.set_property("project.build.sourceEncoding", "MS949"):\n        pom.save()\n')


def answer(edits=EDITS, script: str = SCRIPT, review=REVIEW, extra: str = "") -> str:
    out = (f"<review>{json.dumps(review)}</review>\n<analysis>MS949 sources, UTF-8 build</analysis>\n"
           "<solution>declare project.build.sourceEncoding MS949 in the comp module</solution>\n")
    if edits is not None:
        out += f"<edits>{edits if isinstance(edits, str) else json.dumps(edits)}</edits>\n"
    if script:
        out += f"<script>\n{script}</script>\n"
    return out + extra


def run(cfg, ws, *answers, runner=None):
    llm = FakeLLM(*answers)
    rv = StructureReviewer(cfg, llm, runner).review(ws, profile(), cfg, run_id="run-7")
    return rv, llm


def test_parse_the_four_parts_next_to_the_review():
    p = parse_review_answer(answer())
    assert p.ok and len(p.critical) == 1 and p.analysis == REVIEW["analysis"]
    assert p.fix_analysis == "MS949 sources, UTF-8 build" and p.solution.startswith("declare")
    assert [e.op for e in p.edits] == ["pom"] and p.script == SCRIPT and p.edits_problem == ""
    # no <review> tags: the edits list is never taken for the issues
    bare = answer().replace("<review>", "").replace("</review>", "")
    p = parse_review_answer(bare)
    assert p.ok and [i.severity for i in p.issues] == ["critical", "warning"] and len(p.edits) == 1
    # a v2 answer: review + script only
    p = parse_review_answer(answer(edits=None).replace("<analysis>MS949 sources, UTF-8 build</analysis>\n", ""))
    assert p.edits == [] and p.script == SCRIPT and p.fix_analysis == REVIEW["analysis"]
    p = parse_review_answer(answer(edits='[{"op": "pom" "path": 1}]'))
    assert p.ok and p.edits == [] and p.edits_problem.startswith("edits JSON unreadable")


def test_critical_edits_are_applied_uncommitted(tmp_path, cfg, caplog):
    caplog.set_level(logging.INFO, logger="sbmig")
    caplog.handler.addFilter(StageFilter())                         # what the CLI's log handlers carry
    ws = make_ws(tmp_path)
    head = ws.head()
    runner = FakeRunner()
    rv, llm = run(cfg, ws, answer(), runner=runner)
    assert len(llm.calls) == 1 and runner.calls == []               # the edits are the fix; the script is not run
    assert rv.verdict == "issues" and rv.script_result is not None and rv.script_result.status == "ok"
    assert rv.script_result.changed_files == ["module/comp-jar/pom.xml"] and not rv.reverted
    assert ws.head() == head and ws.is_dirty()
    assert "<project.build.sourceEncoding>MS949</project.build.sourceEncoding>" in ws.read("module/comp-jar/pom.xml")
    assert rv.note == "review edits changed 1 file(s): module/comp-jar/pom.xml (1 edit(s)) — not committed yet"
    a = rv.answer
    assert isinstance(a, ScriptProposal) and a.changes == []      # no desc, no <changes>: notes come from the diff
    assert a.analysis == "MS949 sources, UTF-8 build\n[sbmig] the answer did not describe its changes"
    assert a.solution.startswith("declare") and len(a.edits) == 1 and a.code == SCRIPT and a.error == ""
    assert a.llm_usage.calls == 1 and a.model == "gpt-review" and rv.script == SCRIPT
    lines = [(r.stage, r.getMessage()) for r in caplog.records if r.name.startswith("sbmig.review")]
    assert all(st == "review" for st, _ in lines)
    msgs = [m for _, m in lines]
    assert any(m.startswith("structure review: 3 pom, 3 module; LLM 판정: issues (critical 1, warning 1)") for m in msgs)
    assert any(m.startswith("structure review 수정: module/comp-jar/pom.xml (+") and "— declare" in m for m in msgs)


def test_failed_edits_are_restored_and_retried_once(tmp_path, cfg):
    ws = make_ws(tmp_path)
    wrong = [{"op": "replace", "path": "pom.xml", "old": "<module>module/front-jar</module>", "new": ""}]
    rv, llm = run(cfg, ws, answer(edits=wrong), answer())
    assert len(llm.calls) == 2
    retry = llm.calls[1]["messages"][-1].content
    assert retry.startswith("Your edits could not be applied: review edits failed (error): edit 1: text not found "
                            "in pom.xml.") and "restored" in retry
    assert APPLY_RETRY_MESSAGE.split("{error}")[1][:30] in retry
    assert rv.script_result.status == "ok" and not rv.reverted and ws.is_dirty()
    assert rv.note.startswith("review edits changed 1 file(s)") and "(second try; first: review edits failed" in rv.note
    assert rv.llm_usage.calls == 2 and rv.answer.llm_usage.calls == 2


def test_failed_edits_twice_leave_the_tree_restored(tmp_path, cfg):
    ws = make_ws(tmp_path)
    wrong = [{"op": "replace", "path": "pom.xml", "old": "<nope/>", "new": ""}]
    rv, llm = run(cfg, ws, answer(edits=wrong))
    assert len(llm.calls) == 2 and rv.reverted and not ws.is_dirty()
    assert rv.script_result.status == "error" and rv.script_result.error == "edit 1: text not found in pom.xml"
    assert rv.note.startswith("review edits failed (error): edit 1: text not found in pom.xml (second try")
    # the corrected answer had no edits at all → the first failure stays, restored
    ws2 = make_ws(tmp_path / "b")
    rv, llm = run(cfg, ws2, answer(edits=wrong), answer(edits=None, script=""))
    assert len(llm.calls) == 2 and rv.reverted and not ws2.is_dirty()
    assert "the corrected answer had no usable edits" in rv.note


def test_unparsable_pom_after_the_edits_is_restored(tmp_path, cfg):
    ws = make_ws(tmp_path)
    breaking = [{"op": "replace", "path": "pom.xml", "old": "</project>", "new": "</projekt>"}]
    rv, llm = run(cfg, ws, answer(edits=breaking), answer(edits=breaking))
    assert len(llm.calls) == 2 and rv.reverted and not ws.is_dirty() and ws.read("pom.xml") == ROOT_POM
    assert "review edits left unparsable pom(s): pom.xml" in rv.note


def test_edits_on_sources_are_refused_before_running(tmp_path, cfg):
    ws = make_ws(tmp_path)
    src = [{"op": "replace", "path": "module/comp-jar/src/main/java/a/A.java", "old": "hi", "new": "hello"}]
    rv, llm = run(cfg, ws, answer(edits=src))
    assert len(llm.calls) == 1 and rv.script_result is None and not ws.is_dirty()
    assert rv.note == ("edits not applied: they change sources (module/comp-jar/src/main/java/a/A.java) — only build "
                       "files may change")


def test_unreadable_edits_next_to_a_critical_issue_are_asked_again(tmp_path, cfg):
    ws = make_ws(tmp_path)
    rv, llm = run(cfg, ws, answer(edits="[{'op': 'pom' 'path': }]"), answer())
    assert len(llm.calls) == 2 and llm.calls[1]["messages"][-1].content.startswith("Your <edits> could not be read: "
                                                                                   "edits JSON unreadable")
    assert rv.script_result.status == "ok" and ws.is_dirty()
    # still unreadable → the readable review of the first answer is kept, the critical issue is handed over
    ws2 = make_ws(tmp_path / "b")
    bad = answer(edits="[{'op': 'pom' 'path': }]", script="")
    rv, llm = run(cfg, ws2, bad, "garbage")
    assert len(llm.calls) == 2 and rv.verdict == "issues" and not ws2.is_dirty()
    assert rv.note.startswith("edits unreadable, no script for the critical issue(s): they are handed over")
    assert rv.answer.error.startswith("edits JSON unreadable")


def test_edits_for_warnings_only_are_not_applied(tmp_path, cfg):
    ws = make_ws(tmp_path)
    warn = {"verdict": "issues", "issues": [{"severity": "warning", "file": "", "problem": "x", "fix_hint": ""}],
            "analysis": "a"}
    rv, _ = run(cfg, ws, answer(review=warn))
    assert rv.note == "edits not applied: the review reported no critical issue" and not ws.is_dirty()
    off = cfg.model_copy(update={"review": cfg.review.model_copy(update={"fix_critical": False})})
    rv = StructureReviewer(off, FakeLLM(answer())).review(ws, profile(), off, run_id="r")
    assert rv.note == "edits not applied: review.fix_critical is off" and not ws.is_dirty()


def test_edits_apply_without_a_script_runner_and_a_v2_script_still_runs(tmp_path, cfg):
    ws = make_ws(tmp_path)
    rv, _ = run(cfg, ws, answer(), runner=None)                    # edits use apply_edits' own runner
    assert rv.script_result.status == "ok" and ws.is_dirty()
    ws2 = make_ws(tmp_path / "b", comp_pom=COMP_POM)
    runner = FakeRunner(edits={"pom.xml": ROOT_POM + "<!-- v2 -->"})
    rv, _ = run(cfg, ws2, answer(edits=None), runner=runner)
    assert len(runner.calls) == 1 and rv.note.startswith("review script changed 1 file(s): pom.xml")
    assert rv.answer.edits == [] and rv.answer.code == SCRIPT
