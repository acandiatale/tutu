"""Error cases through the real loop / orchestrator (fakes for mvn, LLM, KB): N errors → N records, failed attempts
with their reasons and code, regressions, plan-step attribution, run-dir files, report fields, KB append."""
from __future__ import annotations

import json
import sys
import types

import pytest

from sbmig.core.models import BuildError, ErrorKind, FileChange, FixProposal, LLMUsage, Phase
from sbmig.loop.error_tracker import ErrorTracker

from .fakes import (
    FakeBuildRunner,
    FakeFixer,
    FakeKB,
    FakeRewriteRunner,
    bad_build,
    journal_events,
    ok_build,
    write_fix,
)
from .test_fixloop import make_loop
from .test_orchestrator import make_orch

FOO = "src/main/java/com/x/Foo.java"
BAR = "src/main/java/com/x/Bar.java"


def no_fix(analysis: str = "I cannot fix this") -> FixProposal:
    return FixProposal(changes=[], analysis=analysis, model="fake-model", llm_usage=LLMUsage(model="fake-model", calls=1))


def bad_patch(path: str) -> FixProposal:
    return FixProposal(changes=[FileChange(path=path, op="modify", search="nope();", replace="yes();")],
                       analysis="replace nope() by yes()", model="fake-model",
                       llm_usage=LLMUsage(model="fake-model", calls=1, input_tokens=50, output_tokens=10))


def foo_second_try_fixer(bar=lambda n: no_fix()):
    """Foo: the first proposal does not apply, the second one fixes it. Bar: `bar(n)` (default: never fixed)."""
    n = {FOO: 0, BAR: 0}

    def fn(ws, c, ctx):
        n[c.file] = n.get(c.file, 0) + 1
        if c.file == FOO:
            return bad_patch(FOO) if n[FOO] == 1 else write_fix(FOO, "class Foo { ok }\n", analysis="Foo fixed")
        return bar(n[BAR])
    return FakeFixer(fn=fn)


# --------------------------------------------------------------------------------------------- BuildFixLoop
def test_loop_records_every_error_and_every_attempt(cfg, ws, profile):
    loop = make_loop(cfg, ws, profile, fixer=foo_second_try_fixer(
        bar=lambda n: write_fix(BAR, f"class Bar {{ broken v{n} }}\n", analysis="try")))
    t = ErrorTracker(ws, cfg)
    loop.error_tracker = t
    pr = loop.run(Phase.JDK21, "compile", 5)
    assert not pr.ok
    errors, attempts = t.finalize(loop.blocked.items(), run_id=ws.run_id)
    assert len(errors) == 2                                    # 2 build errors → 2 records
    st = {e.file: e for e in errors}
    foo, bar = st[FOO], st[BAR]
    assert foo.status == "resolved" and foo.targeted and foo.fix_method == "llm" and foo.resolver_id == "fake-model"
    assert [a.outcome for a in foo.attempts] == ["apply_failed", "improved"]
    assert "search not found" in foo.attempts[0].failure and foo.resolved_by == foo.attempts[1].attempt_id
    assert foo.description == "LLM fake-model: Foo fixed"
    assert bar.status == "blocked" and bar.block_reason == "repeated_failure"
    assert bar.failure_reason.startswith("blocked (repeated_failure)")
    assert len(bar.attempts) >= 2 and {a.outcome for a in bar.attempts} == {"no_change"}
    assert bar.problem_errors == [f"{BAR} cannot find symbol"]
    by_id = {a.attempt_id: a for a in attempts}
    assert len(attempts) == len(pr.attempts) == 2 + len(bar.attempts)
    failed = by_id[foo.attempts[0].attempt_id.split(":", 1)[1]]
    assert failed.outcome == "apply_failed" and not failed.applied
    assert "-nope();" in failed.diff and "+yes();" in failed.diff          # the proposal that did not apply
    fixed = by_id[foo.attempts[1].attempt_id.split(":", 1)[1]]
    assert "+class Foo { ok }" in fixed.diff and "-class Foo { broken }" in fixed.diff and "Bar" not in fixed.diff
    assert fixed.error_ids == [foo.id] and fixed.input_tokens == 100
    nochange = by_id[bar.attempts[0].attempt_id.split(":", 1)[1]]
    assert nochange.failure == "the targeted error(s) were still there after the rebuild"
    assert nochange.remaining_errors == [f"{BAR} cannot find symbol"]
    # the loop kept the failed proposal on the attempt itself
    first = next(a for a in pr.attempts if a.outcome == "apply_failed")
    assert first.proposed_changes and first.proposed_changes[0].search == "nope();" and first.changes == []
    assert all(a.base_commit for a in pr.attempts)


def test_regressed_attempt_keeps_its_introduced_errors(cfg, ws, profile):
    ws.write(BAR, "class Bar { ok }\n")
    ws.commit("bar ok")

    def build(w, call):
        text = w.read(FOO)
        errs = []
        if "broken" in text:
            errs.append(BuildError(kind=ErrorKind.COMPILE, message="cannot find symbol", file=FOO, line=1,
                                   module="demo", signature="compile|cannot find symbol", signature_generic="c|x"))
        if "oops" in text:
            errs.append(BuildError(kind=ErrorKind.COMPILE, message="';' expected", file=FOO, line=2, module="demo",
                                   signature="compile|semicolon", signature_generic="c|y"))
        return bad_build(*errs) if errs else ok_build()

    proposals = [write_fix(FOO, "class Foo { broken oops }\n", analysis="first try"),
                 write_fix(FOO, "class Foo { ok }\n", analysis="second try")]
    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build), fixer=FakeFixer(proposals))
    t = ErrorTracker(ws, cfg)
    loop.error_tracker = t
    pr = loop.run(Phase.SPRING, "compile", 5)
    assert pr.ok
    regressed = pr.attempts[0]
    assert regressed.outcome == "reverted" and [e.message for e in regressed.introduced_errors] == ["';' expected"]
    errors, attempts = t.finalize([], run_id=ws.run_id)
    # the reverted round's build is void: the introduced error lives on the attempt, not as an error record
    assert [e.file for e in errors] == [FOO] and errors[0].status == "resolved" and errors[0].builds_present == 2
    assert [a.outcome for a in errors[0].attempts] == ["reverted", "build_ok"]
    rec = next(a for a in attempts if a.outcome == "reverted")
    assert rec.introduced_errors == [f"{FOO}:2 ';' expected"]
    assert rec.failure.startswith("regressed: 1 new error(s) in touched files — introduced:")
    assert rec.failure.endswith("(reverted)") and "+class Foo { broken oops }" in rec.diff
    assert rec.remaining_errors == [f"{FOO}:1 cannot find symbol"]


def test_tracker_failure_never_breaks_the_loop(cfg, ws, profile):
    class Broken:
        def __getattr__(self, name):
            def boom(*a, **k):
                raise RuntimeError("tracker down")
            return boom

    fixer = FakeFixer(fn=lambda w, c, ctx: write_fix(c.file, "class X { fixed }\n"))
    loop = make_loop(cfg, ws, profile, fixer=fixer)
    loop.error_tracker = Broken()
    pr = loop.run(Phase.SPRING, "compile", 5)
    assert pr.ok and [a.outcome for a in pr.attempts] == ["build_ok", "build_ok"]


# --------------------------------------------------------------------------------------------- Orchestrator
class RecordingKB(FakeKB):
    def __init__(self):
        super().__init__()
        self.appended: list[tuple[list, list]] = []

    def append_error_cases(self, errors, attempts):
        self.appended.append((list(errors), list(attempts)))
        return len(errors)


@pytest.fixture()
def fake_kb_module(monkeypatch):
    """Stand-in for `sbmig.kb.error_cases` (written by the KB workstream): marks resolved LLM fixes as text-replace
    candidates and fills `project`, so the test sees the prepared records flow into report / files / KB."""
    calls: dict[str, list] = {"prepare": [], "jsonl": [], "csv": []}
    mod = types.ModuleType("sbmig.kb.error_cases")

    def prepare_error_cases(errors, attempts, profile, ws, cfg):
        calls["prepare"].append((len(errors), len(attempts)))
        out = []
        for e in errors:
            upd = {"project": profile.name, "diff_excerpt": "excerpt"}
            if e.status == "resolved" and e.fix_method == "llm":
                upd["replace_rule_candidate"] = True
            out.append(e.model_copy(update=upd))
        return out, [a.model_copy(update={"project": profile.name}) for a in attempts]

    def write_error_cases_jsonl(records, path):
        calls["jsonl"].append(path.name)
        path.write_text("".join(r.model_dump_json() + "\n" for r in records), encoding="utf-8")
        return path

    def write_error_cases_csv(records, path):
        calls["csv"].append(path.name)
        path.write_text("id,status\n" + "".join(f"{r.id},{r.status}\n" for r in records), encoding="utf-8-sig")
        return path

    mod.prepare_error_cases = prepare_error_cases
    mod.write_error_cases_jsonl = write_error_cases_jsonl
    mod.write_error_cases_csv = write_error_cases_csv
    monkeypatch.setitem(sys.modules, "sbmig.kb.error_cases", mod)
    return calls


def test_orchestrator_writes_error_cases(cfg, ws, fake_kb_module):
    kb = RecordingKB()
    report = make_orch(cfg, ws, kb=kb, fixer=foo_second_try_fixer(), pre=[]).run()
    assert not report.success
    assert len(report.error_cases) == 2
    st = {e.file: e for e in report.error_cases}
    assert st[FOO].status == "resolved" and st[FOO].fix_method == "llm" and st[FOO].project == "demo"
    assert [a.outcome for a in st[FOO].attempts] == ["apply_failed", "improved"]
    assert st[BAR].status == "blocked" and st[BAR].last_seen == "verify-final-2"
    assert {a.failure for a in st[BAR].attempts} == {"LLM proposed no changes"}
    assert len(st[BAR].attempts) == 4                       # 2 in jdk21 + 2 after the release in the final loop
    assert all(e.diff_excerpt == "" for e in report.error_cases)   # the report copy is slim
    assert report.error_case_counts == {"resolved": 1, "blocked": 1, "llm_text_replace": 1}
    run_dir = ws.run_dir
    assert report.outputs["error_cases"] == str(run_dir / "error_cases.jsonl")
    assert report.outputs["fix_attempts"] == str(run_dir / "fix_attempts.jsonl")
    assert report.outputs["error_cases_csv"] == str(run_dir / "error_cases.csv")
    lines = [json.loads(x) for x in (run_dir / "error_cases.jsonl").read_text().splitlines()]
    assert len(lines) == 2 and {x["diff_excerpt"] for x in lines} == {"excerpt"}
    att_lines = [json.loads(x) for x in (run_dir / "fix_attempts.jsonl").read_text().splitlines()]
    assert len(att_lines) == 6 and any("-nope();" in a["diff"] for a in att_lines)
    assert fake_kb_module["prepare"] == [(2, 6)] and fake_kb_module["csv"] == ["error_cases.csv"]
    assert len(kb.appended) == 1 and len(kb.appended[0][0]) == 2 and len(kb.appended[0][1]) == 6
    ev = [e for e in journal_events(ws) if e["event"] == "error_cases"]
    assert ev and ev[0]["count"] == 2 and ev[0]["attempts"] == 6 and ev[0]["by_status"] == {"resolved": 1,
                                                                                             "blocked": 1}
    assert not any(f.startswith("error-cases-failed") for f in report.flags)
    rep = json.loads((run_dir / "report.json").read_text())
    assert len(rep["error_cases"]) == 2 and rep["error_case_counts"]["blocked"] == 1


def test_plan_step_resolves_an_error_left_by_the_loop(cfg, ws, fake_kb_module):
    def rewrite_effect(w, refs):
        if any("spring" in r.id.lower() for r in refs):
            w.write(BAR, "class Bar { fixed by recipe }\n")
            return [BAR]
        return []

    report = make_orch(cfg, ws, fixer=foo_second_try_fixer(), pre=[],
                       rewrite=FakeRewriteRunner(ws, effect=rewrite_effect)).run()
    assert report.success
    st = {e.file: e for e in report.error_cases}
    assert st[FOO].status == "resolved"
    bar = st[BAR]
    assert bar.status == "resolved_indirectly" and bar.fix_method == "plan_step"
    assert bar.resolved_by == "step:spring.rewrite" and bar.resolved_phase == Phase.SPRING
    assert bar.resolved_at == "spring-1"
    assert report.error_case_counts["resolved_indirectly"] == 1


def test_kb_append_only_with_harvest(cfg, ws, fake_kb_module):
    cfg = cfg.model_copy(deep=True)
    cfg.kb.harvest = False
    kb = RecordingKB()
    report = make_orch(cfg, ws, kb=kb, pre=[]).run()
    assert report.success and kb.appended == []
    assert (ws.run_dir / "error_cases.jsonl").exists() and report.error_cases
    assert {e.status for e in report.error_cases} == {"resolved"}


def test_error_cases_off_and_dry_run(cfg, ws, tmp_path, fake_kb_module):
    off = cfg.model_copy(deep=True)
    off.kb.error_cases = False
    kb = RecordingKB()
    orch = make_orch(off, ws, kb=kb, pre=[])
    report = orch.run()
    assert orch.error_tracker is None and orch.loop.error_tracker is None
    assert report.error_cases == [] and report.error_case_counts == {} and kb.appended == []
    assert not (ws.run_dir / "error_cases.jsonl").exists() and "error_cases" not in report.outputs
    from sbmig.core.workspace import Workspace

    from .fakes import make_project
    ws2 = Workspace.from_input(make_project(tmp_path / "dry"), tmp_path / "dry-out", run_id="t-dry")
    ws2.git_init()
    orch2 = make_orch(cfg, ws2, kb=kb, pre=[])
    report2 = orch2.run(dry_run=True)
    assert orch2.error_tracker is None and report2.error_cases == [] and kb.appended == []
    assert not (ws2.run_dir / "error_cases.jsonl").exists()


def test_prepare_crash_is_a_flag_not_a_failed_run(cfg, ws, fake_kb_module, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(sys.modules["sbmig.kb.error_cases"], "prepare_error_cases", boom)
    report = make_orch(cfg, ws, pre=[]).run()
    assert report.success and "error-cases-failed: RuntimeError: boom" in report.flags
    assert (ws.run_dir / "report.md").exists()


def test_without_the_kb_module_raw_records_are_written(cfg, ws, monkeypatch):
    monkeypatch.setitem(sys.modules, "sbmig.kb.error_cases", None)      # → ModuleNotFoundError on import
    kb = RecordingKB()
    report = make_orch(cfg, ws, kb=kb, pre=[]).run()
    assert report.success and not any(f.startswith("error-cases-failed") for f in report.flags)
    assert (ws.run_dir / "error_cases.jsonl").exists() and (ws.run_dir / "fix_attempts.jsonl").exists()
    assert "error_cases_csv" not in report.outputs and kb.appended == []
    assert len(report.error_cases) == 2 and report.error_case_counts == {"resolved": 2, "llm_text_replace": 0}


def test_msa_targets_are_tracked_per_service(cfg, ws, fake_kb_module):
    from sbmig.core.models import CaseType

    from .fakes import make_profile, msa_plan
    a_file, b_file = "svc-a/src/main/java/A.java", "svc-b/src/main/java/B.java"
    ws.write(a_file, "class A { broken }\n")
    ws.write(b_file, "class B { broken }\n")
    ws.commit("services")

    def build(w, call):
        mod = call["module"]
        files = [f for f in (a_file, b_file) if mod is None or f.startswith(f"{mod}/")]
        errs = [BuildError(kind=ErrorKind.COMPILE, message="cannot find symbol", file=f, module=f.split("/")[0],
                           signature="compile|cannot find symbol", signature_generic="c|x")
                for f in files if "broken" in w.read(f)]
        return bad_build(*errs) if errs else ok_build()

    fixer = FakeFixer(fn=lambda w, c, ctx: write_fix(c.file, "class X { ok }\n"))
    report = make_orch(cfg, ws, plan=msa_plan(), profile=make_profile(CaseType.MSA), pre=[],
                       build=FakeBuildRunner(ws, fn=build), fixer=fixer).run()
    assert report.success
    st = {e.module: e for e in report.error_cases}
    assert set(st) == {"svc-a", "svc-b"} and all(e.status == "resolved" for e in st.values())
    # both services number their attempts jdk21-1-1: the records stay distinct
    assert st["svc-a"].resolved_by != st["svc-b"].resolved_by
    assert st["svc-a"].first_seen == "jdk21-1@svc-a" and st["svc-b"].first_seen == "jdk21-1@svc-b"
