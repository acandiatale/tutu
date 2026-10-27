"""v2.1 orchestrator (docs/v2/PLAN-v2.1.md §3): transform.compile puts every error on the error list (seen compile,
no fixing); the restructure's pom corrections are pom-verify entries the build verifies; the build adds only NEW
errors; the review's findings are entries and its script goes into the KB; finalize writes the list, the report's
entries and the KB; every log line carries its stage."""
from __future__ import annotations

import logging

import pytest

from sbmig.core.interfaces import RestructureResult
from sbmig.core.models import Phase, ReviewIssue, StructureReview
from sbmig.core.stage import install_stage_filter
from sbmig.loop.entries import REVIEW_L2
from sbmig.triage.cluster import l2_case_id

from .fakes import (
    FakeBuildRunner,
    FakeErrorList,
    FakeFixer,
    FakeKB,
    FakeReviewer,
    answer,
    bad_build,
    compile_error,
    journal_events,
    ok_build,
    v2_plan,
    write_edit,
)
from .test_v2_orchestrator import BAR, FIXED, FOO, make_v2

NEW = "src/main/java/com/x/New.java"


@pytest.fixture()
def logs(caplog):
    install_stage_filter(caplog.handler)
    with caplog.at_level(logging.DEBUG, logger="sbmig"):
        yield caplog


def lines(caplog) -> list[tuple[str, str]]:
    return [(r.stage, r.getMessage()) for r in caplog.records if r.levelno >= logging.INFO]


def staged_build(ws, call):
    """compile: Foo + Bar broken; the build after the restructure: the same two + one NEW error (New.java)."""
    errs = [compile_error(f) for f in (FOO, BAR) if "broken" in ws.read(f)]
    if call["goal"] == "clean package" and not ws.exists("new-fixed.txt"):
        errs.append(compile_error(NEW, msg="package jakarta.xml.bind does not exist", l2="compile|pkg|jaxb"))
    return bad_build(*errs) if errs else ok_build()


def fix_all(w, c, ctx):
    edits = [write_edit(e.file, FIXED) for e in c.errors if e.file != NEW]
    if any(e.file == NEW for e in c.errors):
        edits.append(write_edit("new-fixed.txt", "1"))
    return answer(edits=edits, analysis="the cause.", solution="the fix.")


class PomRestructurer:
    def __init__(self):
        self.calls = []

    def apply(self, ws, profile, cfg, *, mode, params=None):
        self.calls.append(mode)
        ws.write("module/comp-jar/pom.xml", "<project/>\n")
        return RestructureResult(ok=True, changed_files=["pom.xml"], generated_files=["module/comp-jar/pom.xml"],
                                 parents_set=["module/comp-jar/pom.xml: - → com.acme:base-parent:4.0.0"],
                                 mandatory_added=["com.acme:must"], optional_bumped=["com.acme:opt 1.0→4.0.0"],
                                 managed_versions_dropped=["g:a: 1.2 → managed 1.3 (module/comp-jar/pom.xml)"],
                                 notes=["restructured"])


# ------------------------------------------------------------------------------------------ compile + build
@pytest.mark.parametrize("engine", ["graph", "inline"])
def test_compile_records_every_error_and_the_build_adds_only_new_ones(cfg, ws, logs, engine):
    cfg.loop.engine = engine
    build = FakeBuildRunner(ws, fn=staged_build)
    kb = FakeKB()
    report = make_v2(cfg, ws, plan=v2_plan(compile=True), build=build, kb=kb,
                     fixer=FakeFixer(script_fn=fix_all)).run()
    assert report.success
    assert [c["goal"] for c in build.calls][0] == "clean compile" and build.calls[0]["label"] == "compile"
    entries = kb.recorded[0]
    by_seen = {}
    for e in entries:
        by_seen.setdefault(e.seen, []).append(e)
    assert sorted(e.file for e in by_seen["compile"]) == [BAR, FOO]           # every compile error, kept
    assert [e.file for e in by_seen["build"]] == [NEW]                        # the build added only the new one
    assert {e.status for e in entries if e.seen in ("compile", "build")} == {"resolved"}
    out = lines(logs)
    assert ("compile", "mvn clean compile: 2 error(s) → 목록에 2건 (seen=compile)") in out
    assert any(st == "build" and m.startswith("mvn clean package: 3 error(s), 새 에러 1건 추가") for st, m in out)
    assert ("build", "mvn clean package: BUILD SUCCESS") in out or any(
        st == "build" and m.startswith("mvn clean package: BUILD SUCCESS") for st, m in out)
    assert any(st == "report" and m.startswith("error list: ") for st, m in out)
    assert any(st.startswith("fix r1 ") and m.startswith("KB 탐색: ") for st, m in out)
    tr = report.phases[0]
    assert tr.phase == Phase.TRANSFORM and tr.iterations == 1 and any("목록에 2건" in n for n in tr.notes)
    assert any(e["event"] == "compile" and e["added"] == 2 for e in journal_events(ws))


def test_compile_is_skipped_in_a_dry_run_and_stop_after_transform_keeps_it(cfg, ws):
    build = FakeBuildRunner(ws, fn=staged_build)
    from sbmig.loop import parse_stop_after
    report = make_v2(cfg, ws, plan=v2_plan(compile=True), build=build).run(stop_after=parse_stop_after("transform"))
    assert [c["label"] for c in build.calls] == ["compile"] and "stopped-after-transform" in report.flags
    assert {e.seen for e in report.error_entries} == {"compile"} and {e.status for e in report.error_entries} == {
        "unresolved"}


def test_a_dry_run_has_no_error_list(cfg, ws):
    build = FakeBuildRunner(ws, fn=staged_build)
    report = make_v2(cfg, ws, plan=v2_plan(compile=True), build=build).run(dry_run=True)
    assert build.calls == [] and report.error_entries == [] and not (ws.run_dir / "errors.jsonl").exists()


# ------------------------------------------------------------------------------------------ restructure: pom-verify
def test_restructure_pom_corrections_are_pom_verify_entries_the_build_verifies(cfg, ws, logs):
    el = FakeErrorList()
    report = make_v2(cfg, ws, restructurer=PomRestructurer(), error_list=lambda w, c, p: el).run()
    assert report.success
    added = [args for args, _ in el.of("add_pom_verify")]
    assert added == [("module/comp-jar/pom.xml", "parent - → com.acme:base-parent:4.0.0"),
                     ("pom.xml", "mandatory com.acme:must added"), ("pom.xml", "optional com.acme:opt 1.0→4.0.0"),
                     ("module/comp-jar/pom.xml", "explicit version dropped: g:a: 1.2 → managed 1.3"),
                     ("module/comp-jar/pom.xml", "generated by the restructure")]
    out = lines(logs)
    assert ("restructure", "module/comp-jar/pom.xml: parent - → com.acme:base-parent:4.0.0 (pom-verify #1)") in out
    # every final build verifies them (the fake resolves them on an ok build)
    assert len(el.of("verify_poms")) == 2
    assert any(st == "build" and m == "mvn clean package: BUILD SUCCESS, pom-verify 5건 resolved" for st, m in out)
    # finalize: end_of_run → write → to_report, once
    names = el.names()
    assert names[-3:] == ["end_of_run", "write", "to_report"]


def test_the_real_list_resolves_pom_verify_on_a_green_build(cfg, ws):
    report = make_v2(cfg, ws, restructurer=PomRestructurer()).run()
    poms = [e for e in report.error_entries if e.seen == "restructure"]
    assert len(poms) == 5 and {(e.status, e.resolved) for e in poms} == {("resolved", "build")}


# ------------------------------------------------------------------------------------------ review
def test_review_fix_is_an_entry_its_script_goes_into_the_kb_case(cfg, ws):
    rv = StructureReview(verdict="issues", issues=[
        ReviewIssue(severity="critical", file="pom.xml", problem="sourceEncoding MS949", fix_hint="UTF-8"),
        ReviewIssue(severity="warning", file="pom.xml", problem="duplicate dependency g:a")])
    code = "def fix(p):\n    p.write('pom.xml', '<project>utf8</project>')\n"
    reviewer = FakeReviewer(review=rv, files={"pom.xml": "<project>utf8</project>\n"}, code=code)
    kb = FakeKB()
    el = FakeErrorList()
    report = make_v2(cfg, ws, reviewer=reviewer, kb=kb, error_list=lambda w, c, p: el).run()
    assert report.success and report.structure_review.commit
    notes = [(args, kw) for args, kw in el.of("add_note")]
    crit = [(a, k) for a, k in notes if k.get("status") == "open"]
    warn = [(a, k) for a, k in notes if k.get("status", "note") == "note"]
    assert crit[0][0][1] == "critical: sourceEncoding MS949 — UTF-8" and crit[0][1]["l2"] == REVIEW_L2
    assert warn[0][0] == ("pom.xml", "warning: duplicate dependency g:a", "review")
    (bt_args, bt_kw), = [(a, k) for a, k in el.of("begin_try") if a[2] == "review"]
    assert bt_args[1:] == (0, "review") and bt_kw["entry_ids"]
    t = el.tries_of(l2_case_id(REVIEW_L2))[0]
    assert (t.source, t.applied, t.commit) == ("review", "edits", report.structure_review.commit)
    assert t.solution == "set the parent and the encoding." and "+<project>utf8</project>" in t.diff
    (_res_args, res_kw), = [(a, k) for a, k in el.of("resolve") if k.get("entry_ids")]
    assert res_kw["entry_ids"] == bt_kw["entry_ids"]
    [(cid, sc, kw)] = [x for x in kb.cases.added if x[2]["source"] == "review"]
    assert cid == l2_case_id(REVIEW_L2) and kw["source"] == "review" and kw["l2"] == REVIEW_L2
    assert any(e["event"] == "review_script_stored" for e in journal_events(ws))


def test_a_review_without_a_commit_records_its_critical_issue_as_a_note(cfg, ws):
    rv = StructureReview(verdict="issues", issues=[ReviewIssue(severity="critical", file="pom.xml", problem="x")])
    el = FakeErrorList()
    make_v2(cfg, ws, reviewer=FakeReviewer(review=rv), error_list=lambda w, c, p: el).run()
    assert [args[1] for args, _ in el.of("add_note")] == ["critical (not fixed by the review): x"]
    assert el.of("begin_try") == [] or all(a[2] != "review" for a, _ in el.of("begin_try"))


# ------------------------------------------------------------------------------------------ limits + robustness
def test_fifty_rounds_and_no_llm_cap_come_from_the_config(cfg, ws):
    assert cfg.loop.phases["verify"].max_iterations == 50 and cfg.loop.max_llm_calls == 0
    report = make_v2(cfg, ws).run()
    start = next(e for e in journal_events(ws) if e["event"] == "verify_loop_start")
    assert start["max_iterations"] == 50 and report.success and "llm-budget-exhausted" not in report.flags


def test_an_error_list_that_cannot_be_built_never_stops_the_run(cfg, ws, logs):
    def broken(w, c, p):
        raise RuntimeError("disk on fire")
    report = make_v2(cfg, ws, error_list=broken).run()
    assert report.success and report.error_entries == []
    assert any("error list unavailable (RuntimeError: disk on fire)" in m for _st, m in lines(logs))


def test_a_failing_error_list_call_is_a_warning_not_a_crash(cfg, ws, logs):
    class Angry(FakeErrorList):
        def begin_try(self, *a, **k):
            raise ValueError("no")
    report = make_v2(cfg, ws, error_list=lambda w, c, p: Angry()).run()
    assert report.success
    assert any("errors.begin_try failed: ValueError: no" in m for _st, m in lines(logs))


def test_fleet_budget_minus_one_blocks_llm_cases_and_flags(cfg, ws):
    cfg.loop.max_llm_calls = -1
    report = make_v2(cfg, ws, reviewer=FakeReviewer()).run()
    assert not report.success and {b.reason.value for b in report.blocked} == {"budget_exhausted"}
    assert "llm-budget-exhausted" in report.flags
    assert any("LLM budget exhausted (fleet)" in n for b in report.blocked for n in b.notes)
