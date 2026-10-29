"""v2.1i `fix.mode: edits` (docs/INTERFACES.md "v2.1i", the token comparison): the fix loop asks the LLM for edits
only — no fix script is generated, stored, reused, credited or augmented; every try of a case is a fresh ask that
carries the earlier tries (`FixContext.previous_attempts` + `previous_edits`); script.max_augments_per_case asks,
then BLOCKED repeated_failure; the report labels the tries `edits:<case>#<n>` and `SBMIG-RESULT` ends with
` fix=edits`; the KB's error records are unchanged. Script mode stays byte-identical."""
from __future__ import annotations

import logging

import pytest

from sbmig.core.models import Phase, ReviewIssue, StructureReview
from sbmig.core.stage import install_stage_filter
from sbmig.errors.entries import ErrorList
from sbmig.kb import FileKnowledgeBase
from sbmig.loop import BuildFixLoop, LoopBudget
from sbmig.loop.entries import RunErrors
from sbmig.report import render_markdown
from sbmig.report.result_line import report_result_line
from sbmig.triage import Triager

from .fakes import (
    FakeBuildRunner,
    FakeEditApplier,
    FakeErrorList,
    FakeFixer,
    FakeKB,
    FakeReviewer,
    FakeScriptRunner,
    answer,
    bad_build,
    case_of,
    compile_error,
    make_profile,
    ok_build,
    write_edit,
    write_script,
)
from .test_v2_orchestrator import make_v2

FOO = "src/main/java/com/x/Foo.java"
BAR = "src/main/java/com/x/Bar.java"
L2 = "compile|cannot_find_symbol|clientIp"
CASE = case_of(L2)
FIXED = "class X { ok }\n"


def broken_build(*files: str, l2: str = L2):
    def fn(ws, call):
        errs = [compile_error(f, l2=l2) for f in files if ws.exists(f) and "broken" in ws.read(f)]
        return bad_build(*errs) if errs else ok_build()
    return fn


def make_loop(cfg, ws, build, fixer, *, kb=None, runner=None, edits=None, errors=None, real_errors=None
              ) -> BuildFixLoop:
    run_errors = RunErrors(real_errors if real_errors is not None else errors) \
        if (errors is not None or real_errors is not None) else None
    return BuildFixLoop(cfg, ws, FakeBuildRunner(ws, fn=build), Triager(cfg), fixer,
                        kb if kb is not None else FakeKB(), make_profile(), budget=LoopBudget.from_config(cfg),
                        script_runner=runner or FakeScriptRunner(), edit_applier=edits or FakeEditApplier(),
                        errors=run_errors)


def subjects(ws) -> list[str]:
    return ws._git("log", "--format=%s").stdout.splitlines()[::-1]


@pytest.fixture()
def logs(caplog):
    install_stage_filter(caplog.handler)
    with caplog.at_level(logging.DEBUG, logger="sbmig"):
        yield caplog


def lines(caplog) -> list[tuple[str, str]]:
    return [(r.stage, r.getMessage()) for r in caplog.records if r.levelno >= logging.INFO and r.name.startswith("sbmig")]


@pytest.fixture()
def edits_cfg(cfg):
    cfg.fix.mode = "edits"
    cfg.fix.edits_max_asks_per_case = 3                      # the capped behaviour; the default (0) is tested apart
    return cfg


# ------------------------------------------------------------------------------------------ one ask, edits applied
def test_the_first_try_is_an_llm_ask_whose_edits_are_applied_and_nothing_is_stored(edits_cfg, ws, logs):
    kb, el, runner, edits = FakeKB(), FakeErrorList(), FakeScriptRunner(), FakeEditApplier()
    # a fixer that ignores fix_mode and still sends a script: the loop neither stores nor runs it
    fixer = FakeFixer(scripts=[answer(code=write_script({FOO: FIXED}).code, edits=[write_edit(FOO, FIXED)],
                                      analysis="clientIp was removed.", solution="Use getRemoteAddr.")])
    pr = make_loop(edits_cfg, ws, broken_build(FOO), fixer, kb=kb, runner=runner, edits=edits,
                   errors=el).run(Phase.VERIFY, "package", 3)
    assert pr.ok
    [a] = pr.attempts
    assert (a.source, a.script_mode, a.edits_applied, a.script_version, a.outcome) == \
        ("llm-edit", "new", 1, 0, "build_ok")
    assert a.script_code == "" and any("script was ignored (fix.mode edits" in n for n in a.notes)
    assert runner.runs == [] and len(edits.calls) == 1
    assert kb.cases.added == [] and kb.cases.credits == []                 # nothing stored, nothing credited
    [ctx] = fixer.contexts
    assert (ctx.fix_mode, ctx.script_mode, ctx.script_base, ctx.previous_attempts, ctx.previous_edits) == \
        ("edits", "new", None, [], {})
    assert f"fix(edits:{CASE}#1): case:{CASE}" in subjects(ws)
    [t] = el.tries_of(CASE)
    assert (t.n, t.source, t.script_version, t.applied, t.result) == (1, "llm-edit", 0, "edits", "build_ok")
    out = lines(logs)
    stage = f"fix r1 {CASE}"
    assert (stage, "KB 탐색: fix.mode edits — 저장 스크립트 사용 안 함 → LLM 에 수정 요청 (1/3)") in out
    answer_line = next(m for st, m in out if st == stage and m.startswith("LLM 답변 ("))
    assert answer_line.endswith("edits 1") and "script" not in answer_line


# ------------------------------------------------------------------------------------------ re-asks + the budget
def test_failed_tries_are_carried_into_the_next_ask_then_the_case_is_blocked(edits_cfg, ws, logs):
    edits_cfg.loop.stall_limit = 99                                         # escalation by the ask count alone
    el = FakeErrorList()
    n = {"i": 0}

    def useless(w, c, ctx):
        n["i"] += 1
        return answer(edits=[write_edit(FOO, f"class Foo {{ still broken {n['i']} }}\n", desc=f"try {n['i']}")],
                      analysis=f"cause {n['i']}.")
    fixer = FakeFixer(script_fn=useless)
    pr = make_loop(edits_cfg, ws, broken_build(FOO), fixer, errors=el).run(Phase.VERIFY, "package", 50)
    limit = edits_cfg.fix.edits_max_asks_per_case
    assert not pr.ok and n["i"] == limit == 3
    assert [a.source for a in pr.attempts] == ["llm-edit"] * limit
    assert [a.outcome for a in pr.attempts] == ["no_change"] * limit
    assert [b.reason.value for b in pr.blocked] == ["repeated_failure"]
    # every ask is `new` without a base; the 2nd carries try 1 — its outcome, why, its diff, that it was rolled back
    ctxs = fixer.contexts
    assert {(c.fix_mode, c.script_mode, c.script_base) for c in ctxs} == {("edits", "new", None)}
    assert [len(c.previous_attempts) for c in ctxs] == [0, 1, 2]
    first = pr.attempts[0]
    assert ctxs[1].previous_attempts[0].id == first.id and ctxs[1].previous_attempts[0].outcome == "no_change"
    text = ctxs[1].previous_edits[first.id]
    assert text.startswith(f"edits:{CASE}#1 (attempt {first.id}) → no_change: 1 of 1 targeted error(s) remained")
    assert "its changes were rolled back before this request (not in the tree now)" in text
    assert "applied diff:" in text and "+class Foo { still broken 1 }" in text
    assert list(ctxs[2].previous_edits) == [a.id for a in pr.attempts[:2]]
    # the stronger model from the third ask on (script mode: the second augment)
    assert [c.use_escalation_model for c in ctxs] == [False, False, True]
    assert any(nt == "escalation model requested (ask 3 of the case)" for nt in pr.attempts[2].notes)
    # the error list: one try per ask, numbered, no script version
    assert [(t.n, t.source, t.script_version, t.applied) for t in el.tries_of(CASE)] == \
        [(i, "llm-edit", 0, "edits") for i in range(1, limit + 1)]
    assert [args[0] for args, _ in el.of("block")] == [CASE]
    out = lines(logs)
    assert any(m == "KB 탐색: fix.mode edits — 저장 스크립트 사용 안 함 → LLM 에 다시 수정 요청 (2/3, 앞 시도 1건 포함)"
               for _st, m in out)
    assert any(m == "KB 탐색: fix.mode edits — 저장 스크립트 사용 안 함 → 한도 도달 (LLM 3회) → blocked"
               for _st, m in out)
    assert any(m.startswith(f"롤백: edits:{CASE}#1 ({first.id}, no_change) 되돌림") for _st, m in out)
    assert f"fix(edits:{CASE}#3): case:{CASE}" in subjects(ws)


def test_edits_that_did_not_apply_reach_the_next_ask_as_the_edits_it_asked_for(edits_cfg, ws):
    edits_cfg.fix.edits_max_asks_per_case = 2                               # two asks
    fixer = FakeFixer(scripts=[answer(edits=[{"op": "replace", "path": FOO, "old": "no such text", "new": "ok",
                                              "desc": "rename the call"}]),
                               answer(edits=[write_edit(FOO, FIXED)])])
    pr = make_loop(edits_cfg, ws, broken_build(FOO), fixer).run(Phase.VERIFY, "package", 5)
    assert pr.ok
    failed, fixed = pr.attempts
    assert failed.outcome == "apply_failed" and fixed.outcome == "build_ok"
    text = fixer.contexts[1].previous_edits[failed.id]
    assert text.startswith(f"edits:{CASE}#1 (attempt {failed.id}) → apply_failed: edits error: edit 1: text not found")
    assert "edits it asked for (not applied):" in text
    assert f"- replace {FOO} — rename the call: old 'no such text' → new 'ok'" in text


def test_no_cap_keeps_asking_until_the_rounds_run_out(edits_cfg, ws, logs):
    """fix.edits_max_asks_per_case 0 (the default): the case is never BLOCKED repeated_failure — every round asks again
    until the rounds (max_iterations) run out; the log line says 한도 없음."""
    edits_cfg.fix.edits_max_asks_per_case = 0
    n = {"i": 0}

    def useless(w, c, ctx):
        n["i"] += 1
        return answer(edits=[write_edit(FOO, f"class Foo {{ still broken {n['i']} }}\n", desc=f"try {n['i']}")])
    fixer = FakeFixer(script_fn=useless)
    pr = make_loop(edits_cfg, ws, broken_build(FOO), fixer).run(Phase.VERIFY, "package", 5)
    assert not pr.ok and n["i"] == 5 and pr.blocked == []
    assert [a.source for a in pr.attempts] == ["llm-edit"] * 5
    assert any("한도 없음" in m for _st, m in lines(logs))


def test_an_answer_without_edits_is_a_failed_try(edits_cfg, ws):
    edits_cfg.fix.edits_max_asks_per_case = 1                               # one ask only
    el = FakeErrorList()
    fixer = FakeFixer(scripts=[answer(error="no <edits> block")])
    pr = make_loop(edits_cfg, ws, broken_build(FOO), fixer, errors=el).run(Phase.VERIFY, "package", 5)
    [a] = pr.attempts
    assert a.outcome == "apply_failed" and a.apply_errors == ["LLM answered with no edits: no <edits> block"]
    assert [b.reason.value for b in pr.blocked] == ["repeated_failure"] and len(fixer.contexts) == 1
    [t] = el.tries_of(CASE)
    assert (t.source, t.applied, t.result) == ("llm-edit", "none", "apply_failed")


# ------------------------------------------------------------------------------------------ the KB is not consulted
def test_a_proven_stored_script_is_not_used_in_edits_mode(edits_cfg, ws, logs):
    kb, runner = FakeKB(), FakeScriptRunner()
    kb.cases.seed(CASE, write_script({FOO: FIXED}).code, successes=5, projects=("p1", "p2"))
    fixer = FakeFixer(scripts=[answer(edits=[write_edit(FOO, FIXED)])])
    pr = make_loop(edits_cfg, ws, broken_build(FOO), fixer, kb=kb, runner=runner).run(Phase.VERIFY, "package", 3)
    assert pr.ok and len(fixer.contexts) == 1 and runner.runs == []        # an LLM ask, not the stored script
    assert [a.source for a in pr.attempts] == ["llm-edit"]
    assert kb.cases.credits == [] and kb.cases.added == []
    assert not any("저장된 스크립트 v1" in m for _st, m in lines(logs))


def test_the_real_kb_records_the_run_and_keeps_its_scripts_untouched(edits_cfg, ws, tmp_path):
    """FileKnowledgeBase + the real error list: a stored script of the case stays as it was (not run, not credited,
    no new version); errors.jsonl gets the run's entries with the llm-edit try (script_version 0); reindex is fine."""
    kb = FileKnowledgeBase(edits_cfg, kb_dir=tmp_path / "kb")
    kb.cases.add_script(CASE, L2, write_script({FOO: FIXED}).code, source="llm-new", project="p0")
    el = ErrorList.for_workspace(ws, edits_cfg)
    el.add_build(broken_build(FOO)(ws, {}), "build")
    fixer = FakeFixer(scripts=[answer(edits=[write_edit(FOO, FIXED)], solution="use getRemoteAddr")])
    pr = make_loop(edits_cfg, ws, broken_build(FOO), fixer, kb=kb, real_errors=el).run(Phase.VERIFY, "package", 3)
    assert pr.ok and len(fixer.contexts) == 1
    el.end_of_run()
    assert kb.record_run(el.entries) == 1
    [e] = list(kb.cases.iter_errors(case=CASE))
    assert (e.status, e.resolved, e.script) == ("resolved", "r1 try 1", "")
    [t] = e.tries
    assert (t.n, t.source, t.script_version, t.applied, t.result, t.solution) == \
        (1, "llm-edit", 0, "edits", "build_ok", "use getRemoteAddr")
    case = kb.cases.find(CASE)
    assert [(s.version, s.status, s.successes, s.failures, s.runs) for s in case.scripts] == \
        [(1, "unverified", 0, 0, [])]
    assert sorted(p.name for p in (tmp_path / "kb" / "cases" / CASE).glob("fix_v*.py")) == ["fix_v1.py"]
    assert kb.reindex() >= 1                                              # an llm-edit try (v0) does not choke it
    assert [(s.version, s.runs) for s in kb.cases.find(CASE).scripts] == [(1, [])]


# ------------------------------------------------------------------------------------------ orchestrator + report
def _critical_review():
    rv = StructureReview(verdict="issues", issues=[
        ReviewIssue(severity="critical", file="pom.xml", problem="sourceEncoding MS949", fix_hint="UTF-8")])
    code = "def fix(p):\n    p.write('pom.xml', '<project>utf8</project>')\n"
    return FakeReviewer(review=rv, files={"pom.xml": "<project>utf8</project>\n"}, code=code)


def _fix_with_edits(w, c, ctx):
    return answer(edits=[write_edit(e.file, FIXED) for e in c.errors if e.file])


def test_the_run_report_names_the_mode_and_the_result_line_ends_with_fix_edits(edits_cfg, ws):
    kb = FakeKB()
    report = make_v2(edits_cfg, ws, kb=kb, reviewer=_critical_review(),
                     fixer=FakeFixer(script_fn=_fix_with_edits)).run()
    assert report.success and report.fix_mode == "edits"
    line = report_result_line(report)
    assert line.endswith(" fix=edits") and line.count(" fix=") == 1
    assert kb.cases.added == []                                           # not even the review's script
    md = render_markdown(report)
    assert "fix mode: edits — no fix scripts generated, stored or reused" in md
    assert f"`SBMIG-RESULT run={ws.run_id} " in md and " fix=edits`" in md
    case = next(e.case_id for e in report.error_entries if any(t.source == "llm-edit" for t in e.tries))
    assert f"| edits:{case}#1 build_ok | r1 try 1 |" in md                 # the error list's tries cell
    assert "| edits | fix try |" in md and f"| `edits:{case}#1` |" in md   # Fixes by case: the deciding try
    assert "- script in the answer: not stored (fix.mode edits)" in md     # the review's script was not stored
    ko = render_markdown(report, language="ko")
    assert "수정 방식: edits — 수정 스크립트를 만들지도, 저장·재사용하지도 않음" in ko and "| 수정 | 수정 시도 |" in ko


def test_script_mode_is_unchanged(cfg, ws):
    """Regression: the default run's report has fix_mode script, the result line has no `fix=`, the fix prompt carries
    no previous edits, the review's script is stored as before."""
    kb = FakeKB()
    fixer = FakeFixer(script_fn=_fix_with_edits)
    report = make_v2(cfg, ws, kb=kb, reviewer=_critical_review(), fixer=fixer).run()
    assert report.success and report.fix_mode == "script"
    line = report_result_line(report)
    assert " fix=" not in line and line.startswith(f"SBMIG-RESULT run={ws.run_id} exit=0 build=ok ")
    assert {(c.fix_mode, c.script_mode) for c in fixer.contexts} == {("script", "new")}
    assert all(c.previous_edits == {} for c in fixer.contexts)
    assert [kw["source"] for _c, _s, kw in kb.cases.added] == ["review"]
    assert any(s.startswith("fix(edits:") and " llm-edit): case:" in s for s in subjects(ws))
    md = render_markdown(report)
    assert "fix mode:" not in md and "edits:" not in md and "fix try" not in md
    assert "| 1 llm-edit edits build_ok | r1 try 1 |" in md and "| edits | script |" in md
    assert "- script for the KB: yes" in md


# ------------------------------------------------------------------------------------------ no script wording
def test_the_texts_an_edits_mode_prompt_gets_say_nothing_about_scripts(edits_cfg, ws):
    """What reaches the next ask (`previous_attempts[*].apply_errors`, `previous_edits`, the sibling hints) never
    words a try as a script — not even when a plugin sent a script anyway."""
    edits_cfg.loop.stall_limit = 99
    sig = "compile|shared|clientIp"                                        # one L1 signature in two cases

    def build(w, call):
        errs = [compile_error(f, sig=sig, l2=l2) for f, l2 in ((FOO, L2), (BAR, "compile|other|x"))
                if "broken" in w.read(f)]
        return bad_build(*errs) if errs else ok_build()
    answers = {FOO: [answer(error="no <edits> block"),
                     answer(code=write_script({FOO: "x"}).code, edits=[write_edit(FOO, "class Foo { broken 2 }\n")]),
                     answer(edits=[write_edit(FOO, FIXED)])],
               BAR: [answer(edits=[write_edit(BAR, FIXED)])]}

    def fix(w, c, ctx):
        return answers[c.errors[0].file].pop(0)
    fixer = FakeFixer(script_fn=fix)
    loop = make_loop(edits_cfg, ws, build, fixer)
    pr = loop.run(Phase.VERIFY, "package", 10)
    assert pr.ok
    foo_ctxs = [ctx for (c, ctx) in fixer.script_calls if c.errors[0].file == FOO]
    assert len(foo_ctxs) == 3
    first = foo_ctxs[1].previous_attempts[0]
    assert first.apply_errors == ["LLM answered with no edits: no <edits> block"]
    assert foo_ctxs[1].previous_edits == {}                                # no edits, no diff: nothing to show
    texts = [*foo_ctxs[2].previous_edits.values(), *(e for ctx in foo_ctxs for a in ctx.previous_attempts
                                                     for e in a.apply_errors)]
    assert texts and not any("script" in t.lower() for t in texts), texts
    # the sibling hint (the same L1 error in the other case) names the try edits:<case>#<n>
    bar_try = next(a for a in pr.attempts if a.cluster_errors[0].file == BAR)
    foo_cluster = next(c for c, _ctx in fixer.script_calls if c.errors[0].file == FOO)
    hints = loop._sibling_hints(foo_cluster)
    assert hints and all("script" not in h for h in hints)
    assert any(f"(edits:{case_of('compile|other|x')}#1)" in h and bar_try.id in h for h in hints)
