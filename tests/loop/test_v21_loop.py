"""v2.1 fix loop (docs/v2/PLAN-v2.1.md §3.1 / §3.2): the LLM answer's edits are applied directly, its script stored
(new: unverified, not run) or run (augment); the stored script → rollback → augment → no_script + edits ladder; every
try on the error list (begin_try → finish_try → add_build → resolve); the log lines and their stage tags."""
from __future__ import annotations

import logging

import pytest

from sbmig.core.models import Phase, ScriptRunResult
from sbmig.core.stage import install_stage_filter
from sbmig.loop import BuildFixLoop, LoopBudget
from sbmig.loop.entries import RunErrors
from sbmig.triage import Triager

from .fakes import (
    FakeBuildRunner,
    FakeEditApplier,
    FakeErrorList,
    FakeFixer,
    FakeKB,
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


def make_loop(cfg, ws, build, fixer, *, kb=None, runner=None, edits=None, errors=None, **kw) -> BuildFixLoop:
    return BuildFixLoop(cfg, ws, FakeBuildRunner(ws, fn=build), Triager(cfg), fixer,
                        kb if kb is not None else FakeKB(), make_profile(), budget=LoopBudget.from_config(cfg),
                        script_runner=runner or FakeScriptRunner(), edit_applier=edits or FakeEditApplier(),
                        errors=RunErrors(errors) if errors is not None else None, **kw)


def subjects(ws) -> list[str]:
    return ws._git("log", "--format=%s").stdout.splitlines()[::-1]


@pytest.fixture()
def logs(caplog):
    """INFO+DEBUG records of sbmig with their stage (`record.stage`, the filter the CLI installs)."""
    install_stage_filter(caplog.handler)
    with caplog.at_level(logging.DEBUG, logger="sbmig"):
        yield caplog


def lines(caplog, level=logging.INFO) -> list[tuple[str, str]]:
    return [(r.stage, r.getMessage()) for r in caplog.records if r.levelno >= level and r.name.startswith("sbmig")]


# ------------------------------------------------------------------------------------------ new: edits + script
def test_new_answer_applies_its_edits_and_stores_the_script_unverified(cfg, ws, logs):
    kb, el, runner, edits = FakeKB(), FakeErrorList(), FakeScriptRunner(), FakeEditApplier()
    code = write_script({FOO: FIXED, BAR: FIXED}).code
    fixer = FakeFixer(scripts=[answer(code=code, edits=[write_edit(FOO, FIXED), write_edit(BAR, FIXED)],
                                      analysis="clientIp was removed. It moved.", solution="Use getRemoteAddr. Done.")])
    loop = make_loop(cfg, ws, broken_build(FOO, BAR), fixer, kb=kb, runner=runner, edits=edits, errors=el)
    pr = loop.run(Phase.VERIFY, "package", 3)
    assert pr.ok
    [a] = pr.attempts
    assert (a.source, a.edits_applied, a.script_version, a.outcome) == ("llm-new", 2, 1, "build_ok")
    assert a.solution == "Use getRemoteAddr. Done." and a.analysis.startswith("clientIp was removed")
    assert runner.runs == [] and len(edits.calls) == 1               # the script was stored, NOT run
    assert f"fix(edits:{CASE} llm-new): case:{CASE}" in subjects(ws)
    [(cid, sc, kw)] = kb.cases.added
    assert (cid, sc.version, sc.status, kw["source"], kw["solution"]) == (CASE, 1, "unverified", "llm-new",
                                                                          "Use getRemoteAddr. Done.")
    assert kb.cases.credits == []                                    # a script that never ran gets no credit
    assert any("stored (unverified), not run: the edits fixed it here" in n for n in a.notes)
    # the error list: one try, filled after the apply and again with the scored result
    [t] = el.tries_of(CASE)
    assert (t.n, t.round, t.source, t.applied, t.result) == (1, 1, "llm-new", "edits", "build_ok")
    assert t.solution == "Use getRemoteAddr. Done." and t.commit == a.commit and sorted(t.files) == [BAR, FOO]
    assert "+class X { ok }" in t.diff and t.llm_calls == 1
    # build 1 (seen fix r1) → the try → build 2 (scores round 1) BEFORE the verdict (resolve)
    assert el.names() == ["add_build", "begin_try", "finish_try", "add_build", "finish_try", "resolve"]
    (a1, k1), (a2, k2) = el.of("add_build")
    assert (a1[1], k1["round"]) == ("fix r1", 0)
    assert a2[1] == "fix r2" and k2["round"] == 1 and k2["void"] is False
    # log lines (§3.2), each in its stage
    out = lines(logs)
    case_stage = f"fix r1 {CASE}"
    assert (case_stage, "2 error(s) in 2 file(s): cannot find symbol") in out
    assert (case_stage, "KB 탐색: 저장된 스크립트 없음 (처음 보는 케이스) → LLM 에 새 수정 요청") in out
    assert any(st == case_stage and m.startswith("LLM 답변 (") and "원인 clientIp was removed." in m
               and "해결 Use getRemoteAddr." in m and "edits 2 / script 있음" in m for st, m in out)
    applied = next(m for st, m in out if st == case_stage and m.startswith("적용(edits): "))
    assert f"{BAR} (+1 −1)" in applied and f"{FOO} (+1 −1)" in applied and f"커밋 {a.commit[:7]}" in applied
    assert any(st == case_stage and m.startswith("diff:\n") and "+class X { ok }" in m for st, m in lines(logs, 0))
    assert any(st == "fix r1" and " open → 1 case(s); 순서: cannot_find_symbol (fix round 1/3)" in m for st, m in out)
    assert ("fix r2", f"build 2: BUILD SUCCESS; resolved 2 (case {CASE} 2/2)") in out
    assert (case_stage, "결과 (build 2): resolved 2/2 → BUILD SUCCESS") in out


def test_failed_edits_fall_back_to_the_answers_script(cfg, ws):
    el = FakeErrorList()
    fixer = FakeFixer(scripts=[answer(code=write_script({FOO: FIXED}).code,
                                      edits=[{"op": "replace", "path": FOO, "old": "no such text", "new": "x"}])])
    runner = FakeScriptRunner()
    pr = make_loop(cfg, ws, broken_build(FOO), fixer, runner=runner, errors=el).run(Phase.VERIFY, "package", 3)
    # the script ran, then once more for the v2.1y idempotency check (same code, same tree, nothing changed again)
    assert pr.ok and len(runner.runs) == 2 and runner.runs[1]["code"] == runner.runs[0]["code"]
    [a] = pr.attempts
    assert a.edits_applied == 0 and a.script_result.status == "ok" and a.outcome == "build_ok"
    assert any("first choice failed: edits error: edit 1: text not found" in n for n in a.notes)
    assert el.tries_of(CASE)[0].applied == "script"


def test_only_a_script_runs_it_and_stores_it_first(cfg, ws):
    kb = FakeKB()
    fixer = FakeFixer(scripts=[answer(code=write_script({FOO: FIXED}).code)])
    pr = make_loop(cfg, ws, broken_build(FOO), fixer, kb=kb).run(Phase.VERIFY, "package", 3)
    [a] = pr.attempts
    assert pr.ok and (a.source, a.edits_applied, a.script_version) == ("llm-new", 0, 1)
    assert kb.cases.credits == [(CASE, 1, "build_ok", "demo")]


def test_no_script_and_no_edits_is_a_declined_try_with_its_reason(cfg, ws):
    """v2.1y (E-M1): an answer with neither a script nor edits is "declined" — outcome blocked, the case BLOCKED
    needs_owner at once (script.max_declines_per_case 1) with the reason as the Korean suggestion; no second ask."""
    cfg.script.max_augments_per_case = 3
    el = FakeErrorList()
    fixer = FakeFixer(scripts=[answer(no_script_reason="the fix depends on business rules", error="no script and no "
                                                                                                  "edits")])
    pr = make_loop(cfg, ws, broken_build(FOO), fixer, errors=el).run(Phase.VERIFY, "package", 3)
    [a] = pr.attempts
    assert a.outcome == "blocked" and "declined: the fix depends on business rules" in a.notes
    assert len(fixer.script_calls) == 1                                 # not asked again
    [t] = el.tries_of(CASE)
    assert (t.source, t.applied, t.result) == ("llm-new", "none", "blocked")
    assert t.no_script_reason == "the fix depends on business rules" and "declined:" in t.note
    assert [args for args, _ in el.of("block")][0][0] == CASE          # the case's entries → blocked
    [b] = pr.blocked
    assert b.reason.value == "needs_owner" and b.suggestion == "the fix depends on business rules — 소유팀 결정 필요"


def test_an_unusable_answer_is_still_a_failed_try(cfg, ws):
    """Not declined: an answer still asking for files (or unparsable / with broken edits) stays apply_failed and the
    ladder asks again."""
    cfg.script.max_augments_per_case = 1
    el = FakeErrorList()
    fixer = FakeFixer(scripts=[answer(analysis="need the pom", error="still requesting context after 2 round(s)")])
    pr = make_loop(cfg, ws, broken_build(FOO), fixer, errors=el).run(Phase.VERIFY, "package", 3)
    [a] = pr.attempts
    assert a.outcome == "apply_failed" and "neither a script nor edits" in a.apply_errors[0]
    assert {b.reason.value for b in pr.blocked} == {"repeated_failure"}


# ------------------------------------------------------------------------------------------ the ladder (§3.1)
def test_stored_script_fails_then_augment_says_no_script_and_gives_edits(cfg, ws, logs):
    kb, el = FakeKB(), FakeErrorList()
    stored = write_script({"notes.txt": "old idea\n"}).code         # proven elsewhere, useless here
    kb.cases.seed(CASE, stored, successes=3, projects=("p1", "p2"))
    seen = {}

    def augment(w, c, ctx):
        seen.update(mode=ctx.script_mode, base=ctx.script_base, notes=w.exists("notes.txt"))
        return answer(no_script_reason="each project names the getter differently",
                      edits=[write_edit(FOO, FIXED)], solution="rename the call in Foo")
    edits = FakeEditApplier()
    loop = make_loop(cfg, ws, broken_build(FOO), FakeFixer(script_fn=augment), kb=kb, edits=edits, errors=el)
    pr = loop.run(Phase.VERIFY, "package", 4)
    assert pr.ok
    reuse, edit = pr.attempts
    assert (reuse.source, reuse.script_version, reuse.outcome) == ("kb-script", 1, "no_change")
    assert "rolled back before attempt verify-2-1" in reuse.notes          # the edits try carries no version
    # the augment prompt got the stored script and what it did; packed on the rolled-back tree
    assert seen["mode"] == "augment" and seen["base"].code == stored and seen["base"].outcome == "no_change"
    assert seen["notes"] is False and not ws.exists("notes.txt")
    assert (edit.source, edit.edits_applied, edit.outcome, edit.script_version) == ("llm-edit", 1, "build_ok", 0)
    assert edit.no_script_reason == "each project names the getter differently"
    assert kb.cases.credits == [(CASE, 1, "no_change", "demo")]            # the stored script's run, nothing else
    assert kb.cases.added == []                                             # no script in the answer: nothing stored
    t1, t2 = el.tries_of(CASE)
    assert (t1.source, t1.script_version, t1.result, t1.applied) == ("kb-script", 1, "no_change", "script")
    assert "rolled back before" in t1.note
    assert (t2.source, t2.applied, t2.result, t2.round) == ("llm-edit", "edits", "build_ok", 2)
    out = lines(logs)
    assert (f"fix r1 {CASE}", "KB 탐색: 저장된 스크립트 v1 (proven, 성공 3 / 실패 0, 프로젝트 2) → 실행") in out
    assert any(st == f"fix r2 {CASE}" and "이번 실행에서 v1 실패 → LLM 보강 요청 (1/3, v1 기준)" in m for st, m in out)
    assert any(st == f"fix r2 {CASE}" and m.startswith("롤백: v1 (verify-1-1, no_change) 되돌림") for st, m in out)
    assert any(st == f"fix r2 {CASE}" and "script 없음 (이유: each project names the getter differently)" in m
               for st, m in out)


def test_augment_with_a_script_runs_the_new_version_and_falls_back_to_edits(cfg, ws):
    kb = FakeKB()
    kb.cases.seed(CASE, write_script({"notes.txt": "x"}).code)
    bad = write_script({"other.txt": "y"}).code + "\nraise_me = 1\n"
    fixer = FakeFixer(scripts=[answer(code="def fix(p):\n    raise KeyError('clientIp')\n",
                                      edits=[write_edit(FOO, FIXED)])])
    pr = make_loop(cfg, ws, broken_build(FOO), fixer, kb=kb).run(Phase.VERIFY, "package", 4)
    assert pr.ok and bad
    reuse, aug = pr.attempts
    assert aug.source == "llm-augment" and aug.script_version == 2 and aug.script_result.status == "error"
    assert aug.edits_applied == 1 and aug.outcome == "build_ok"
    assert any("script v2 did not apply → applying the answer's edits instead" in n for n in aug.notes)
    # v2 credited as the failure it was (right away); the edits' success is not the script's
    assert kb.cases.credits == [(CASE, 1, "no_change", "demo"), (CASE, 2, "apply_failed", "demo")]


def test_one_stored_plus_three_llm_tries_then_blocked(cfg, ws, logs):
    kb, el = FakeKB(), FakeErrorList()
    kb.cases.seed(CASE, write_script({"notes.txt": "stored\n"}).code)
    n = {"i": 0}

    def useless(w, c, ctx):
        n["i"] += 1
        return answer(code=write_script({"notes.txt": f"llm {n['i']}\n"}).code)
    loop = make_loop(cfg, ws, broken_build(FOO), FakeFixer(script_fn=useless), kb=kb, errors=el)
    pr = loop.run(Phase.VERIFY, "package", 50)
    assert not pr.ok and n["i"] == 3
    assert [a.source for a in pr.attempts] == ["kb-script", "llm-augment", "llm-augment", "llm-augment"]
    assert [b.reason.value for b in pr.blocked] == ["repeated_failure"]
    assert [args[0] for args, _ in el.of("block")] == [CASE]
    assert any("한도 도달 (저장본 1 + LLM 3회) → blocked" in m for _st, m in lines(logs))
    assert any(m.startswith("blocked (repeated_failure): ") and m.endswith("→ Handover") for _st, m in lines(logs))


# ------------------------------------------------------------------------------------------ error list + stages
def test_kb_hints_and_the_first_build_reuse(cfg, ws, logs):
    """kb.match hint lines (the case's last solution) reach the prompt; the reused first build is not put on the error
    list again (the orchestrator did it), the next build is (seen `fix r2`, round 1)."""
    from sbmig.core.models import KBMatch
    kb = FakeKB(match=lambda c: KBMatch(hints=[f"case {CASE}: last solution — use getRemoteAddr"]))
    fixer = FakeFixer(scripts=[answer(edits=[write_edit(FOO, FIXED)])])
    el = FakeErrorList()
    first = broken_build(FOO)(ws, {})
    loop = make_loop(cfg, ws, broken_build(FOO), fixer, kb=kb, errors=el)
    assert loop.run(Phase.VERIFY, "package", 3, first_build=first).ok
    assert fixer.contexts[0].kb_hints[0].endswith("use getRemoteAddr")
    assert [(args[1], kw["round"]) for args, kw in el.of("add_build")] == [("fix r2", 1)]
    assert ("fix r1", "build 1 = the failed final build (1 error(s), /tmp/bad.log) — not built again") in lines(logs)


def test_a_reverted_round_is_void_and_its_inconclusive_tries_stay_pending(cfg, ws):
    """A pom edit that breaks the build structurally is reverted; that build is void on the error list (resolves
    nothing) and the rebuild scores again."""
    from sbmig.core.models import BuildError, ErrorKind

    def build(w, call):
        if "bogus" in w.read("pom.xml"):
            return bad_build(BuildError(kind=ErrorKind.POM_INVALID, message="pom broken", module="demo",
                                        signature="pom_invalid|x", signature_generic="pom_invalid|x"))
        return broken_build(FOO)(w, call)
    n = {"i": 0}

    def fix(w, c, ctx):
        n["i"] += 1
        if n["i"] == 1:
            return answer(edits=[write_edit("pom.xml", "<project>bogus</project>\n")])
        return answer(edits=[write_edit(FOO, FIXED)])
    el = FakeErrorList()
    pr = make_loop(cfg, ws, build, FakeFixer(script_fn=fix), errors=el).run(Phase.VERIFY, "package", 5)
    assert pr.ok and pr.attempts[0].outcome == "reverted"
    voids = [kw["void"] for _a, kw in el.of("add_build")]
    assert voids[0] is False and voids[1] is True and voids[-1] is False     # build 2 showed a reverted tree
    assert el.tries_of(CASE)[0].result == "reverted"


def test_stage_tags_for_the_watchdog_milestones(cfg, ws, logs):
    fixer = FakeFixer(scripts=[answer(edits=[write_edit(FOO, FIXED)])])
    make_loop(cfg, ws, broken_build(FOO), fixer).run(Phase.VERIFY, "package", 3)
    marks = [(st, m) for st, m in lines(logs, logging.DEBUG) if m.startswith("→ ")]
    assert (f"fix r1 {CASE}", f"→ KB 탐색 for case:{CASE}") in marks
    assert ("fix r2", "→ mvn package (build 2)") in marks
    assert not any("[verify it" in m for _st, m in lines(logs))       # the stage replaced the old prefixes


def test_the_edit_applier_gets_the_case_errors_and_a_failure_restores_the_tree(cfg, ws):
    cfg.script.max_augments_per_case = 1
    seen = {}

    class Applier(FakeEditApplier):
        def __call__(self, w, edits, *, cfg=None, errors=(), runner=None, timeout_s=None):
            seen["errors"] = [e.file for e in errors]
            w.write(FOO, "half written")
            return ScriptRunResult(status="error", changed_files=[FOO], error="edit 2: text not found in pom.xml")
    fixer = FakeFixer(scripts=[answer(edits=[write_edit(FOO, FIXED), {"op": "replace", "path": "pom.xml",
                                                                        "old": "x", "new": "y"}])])
    pr = make_loop(cfg, ws, broken_build(FOO), fixer, edits=Applier()).run(Phase.VERIFY, "package", 3)
    [a] = pr.attempts
    assert seen["errors"] == [FOO] and a.outcome == "apply_failed" and not a.applied
    assert "edits error: edit 2: text not found in pom.xml" in a.apply_errors[0]
    assert not ws.is_dirty() and "broken" in ws.read(FOO)
