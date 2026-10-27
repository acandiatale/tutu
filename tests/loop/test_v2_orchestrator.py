"""v2 orchestrator (flow-v2): transform → restructure → structure review → verify (final build + script fix loop);
the review's script is committed and reported."""
from __future__ import annotations

import pytest

from sbmig.core.interfaces import LLMResponse
from sbmig.core.models import (
    CaseType,
    Phase,
    ReviewIssue,
    StructureReview,
)
from sbmig.loop import Orchestrator, parse_stop_after
from sbmig.loop.orchestrator import _BudgetedLLM
from sbmig.triage import Triager

from .fakes import (
    FakeBuildRunner,
    FakeFixer,
    FakeKB,
    FakeRestructurer,
    FakeReviewer,
    FakeRewriteRunner,
    FakeScriptRunner,
    bad_build,
    case_of,
    compile_error,
    journal_events,
    make_profile,
    ok_build,
    v2_msa_plan,
    v2_plan,
    write_script,
)

FOO = "src/main/java/com/x/Foo.java"
BAR = "src/main/java/com/x/Bar.java"
FIXED = "class X { ok }\n"


def files_build(ws, call):
    errs = [compile_error(f) for f in (FOO, BAR) if "broken" in ws.read(f)]
    return bad_build(*errs) if errs else ok_build()


def fix_all(ws, cluster, ctx):
    return write_script({e.file: FIXED for e in cluster.errors if e.file}, analysis="fixed the case")


class FakeLLM:
    def __init__(self):
        self.calls = 0

    def complete(self, messages, model=None, **kw):
        self.calls += 1
        return LLMResponse(content="{}", model="m-review", input_tokens=10, output_tokens=5)


def make_v2(cfg, ws, *, plan=None, profile=None, build=None, fixer=None, kb=None, rewrite=None, restructurer=None,
            reviewer=None, runner=None, llm=None, **kw) -> Orchestrator:
    kwargs = dict(analyzer=lambda w, c: profile or make_profile(), planner=lambda p, c, k: plan or v2_plan(),
                  restructurer=restructurer or FakeRestructurer(), rewrite_runner=rewrite or FakeRewriteRunner(ws),
                  build_runner=build or FakeBuildRunner(ws, fn=files_build), triager=Triager(cfg),
                  fixer=fixer or FakeFixer(script_fn=fix_all), kb=kb if kb is not None else FakeKB(),
                  llm=llm if llm is not None else FakeLLM(), script_runner=runner or FakeScriptRunner(),
                  reviewer=reviewer if reviewer is not None else FakeReviewer())
    kwargs.update(kw)
    return Orchestrator(cfg, ws, **kwargs)


def subjects(ws) -> list[str]:
    return ws._git("log", "--format=%s").stdout.splitlines()[::-1]


# ------------------------------------------------------------------------------------------ the flow
@pytest.mark.parametrize("engine", ["graph", "inline"])
def test_v2_plan_runs_transform_restructure_review_verify_in_order(cfg, ws, engine):
    cfg.loop.engine = engine
    rw = FakeRewriteRunner(ws, effect=lambda w, refs: (w.write("rewritten.txt", "x") or ["rewritten.txt"]))
    reviewer = FakeReviewer()
    build = FakeBuildRunner(ws, fn=files_build)
    orch = make_v2(cfg, ws, rewrite=rw, reviewer=reviewer, build=build)
    report = orch.run()
    assert report.success and report.final_build.ok
    assert [p.phase for p in report.phases] == [Phase.TRANSFORM, Phase.RESTRUCTURE, Phase.VERIFY]
    assert len(rw.runs) == 1 and rw.runs[0]["label"] == "transform.rewrite"
    subj = subjects(ws)
    i_rw, i_rs = subj.index("rewrite: transform.rewrite"), subj.index("restructure: restructure.apply")
    i_fix = next(i for i, x in enumerate(subj) if x.startswith("fix(script:"))
    assert i_rw < i_rs < i_fix
    # the review saw the restructured tree and the restructure notes
    assert reviewer.calls[0]["head"] == ws._git("rev-list", "-n1", "--grep=restructure: restructure.apply",
                                                 "HEAD").stdout.strip()
    assert any("restructured" in n for n in reviewer.calls[0]["notes"]) and len(reviewer.calls[0]["results"]) == 1
    assert report.structure_review is not None and report.structure_review.verdict == "ok"
    # final clean package failed → the loop reused it as build 1, fixed the case, built once, final build again
    assert [c["label"] for c in build.calls] == ["verify", "verify-2", "verify"]
    [att] = report.phases[-1].attempts
    assert att.script_mode == "new" and att.cluster_id == f"case:{case_of('compile|x')}"
    ev = [e["event"] for e in journal_events(ws)]
    assert ev.index("rewrite") < ev.index("restructure") < ev.index("structure_review") < ev.index("verify")


def test_the_rewrite_gets_only_the_step_recipes_and_the_kb_no_learning_calls(cfg, ws):
    kb = FakeKB()
    rw = FakeRewriteRunner(ws)
    report = make_v2(cfg, ws, kb=kb, rewrite=rw).run()
    assert report.success
    assert rw.runs[0]["files"] == []                             # no KB recipe files ride along
    ev = journal_events(ws)
    assert not {e["event"] for e in ev} & {"handlers_preemptive", "harvest", "kb_credit"}
    plan_ev = next(e for e in ev if e["event"] == "plan")
    assert "pre_handlers" not in plan_ev


def test_error_cases_reach_the_kb(cfg, ws):
    kb = FakeKB()
    report = make_v2(cfg, ws, kb=kb).run()
    assert report.success and kb.appended
    errors, attempts = kb.appended[0]
    assert errors and attempts[0].fix_method == "script" and attempts[0].resolver_id.startswith("script:")


def test_final_build_is_always_attempted_when_the_scripts_fail(cfg, ws):
    cfg.script.max_augments_per_case = 1
    n = {"i": 0}

    def useless(w, c, ctx):
        n["i"] += 1
        return write_script({"notes.txt": str(n["i"])})
    build = FakeBuildRunner(ws, fn=files_build)
    report = make_v2(cfg, ws, build=build, fixer=FakeFixer(script_fn=useless)).run()
    assert not report.success and build.calls[-1]["label"] == "verify"
    assert [c["goal"] for c in build.calls][0] == build.calls[-1]["goal"] == "clean package"
    assert [b.reason.value for b in report.blocked] == ["repeated_failure"]
    assert report.final_build is not None and not report.final_build.ok


@pytest.mark.parametrize("engine", ["graph", "inline"])
def test_stop_after_transform(cfg, ws, engine):
    cfg.loop.engine = engine
    restr = FakeRestructurer()
    reviewer = FakeReviewer()
    report = make_v2(cfg, ws, restructurer=restr, reviewer=reviewer).run(stop_after=parse_stop_after("transform"))
    assert [p.phase for p in report.phases] == [Phase.TRANSFORM]
    assert "stopped-after-transform" in report.flags and restr.calls == [] and reviewer.calls == []
    assert any(e["event"] == "phase_skipped" for e in journal_events(ws))


# ------------------------------------------------------------------------------------------ structure review
def test_review_script_is_committed_journaled_and_reported(cfg, ws):
    rv = StructureReview(verdict="issues", issues=[ReviewIssue(severity="critical", file="pom.xml", problem="no parent"),
                                                   ReviewIssue(severity="warning", problem="duplicate dependency")])
    reviewer = FakeReviewer(review=rv, files={"pom.xml": "<project><parent>base</parent></project>\n"})
    kb = FakeKB()
    report = make_v2(cfg, ws, reviewer=reviewer, kb=kb).run()
    assert report.success
    sha = report.structure_review.commit
    assert [r.commit for r in kb.reviews] == [sha]                      # recorded after the commit
    assert sha and "review: fix critical structure issues (1)" in subjects(ws)
    assert ws._git("log", "-1", "--format=%s", sha).stdout.strip() == "review: fix critical structure issues (1)"
    ev = next(e for e in journal_events(ws) if e["event"] == "structure_review")
    assert (ev["verdict"], ev["issues"], ev["critical"], ev["script_status"], ev["commit"]) == ("issues", 2, 1, "ok",
                                                                                                  sha)
    assert any("structure review issues (2 issue(s), 1 critical); script ok" in n for n in report.phases[1].notes)


def test_review_note_names_the_commit_instead_of_not_committed_yet(cfg, ws):
    """The reviewer writes its note before the orchestrator commits (`… — not committed yet`); once committed, the
    report, the journal and reviews.jsonl say `committed <sha>` (2026-10-26 e2e: euckr-legacy showed both)."""
    rv = StructureReview(verdict="issues", issues=[ReviewIssue(severity="critical", file="pom.xml", problem="x")],
                         note="review script changed 1 file(s): pom.xml — not committed yet")
    reviewer = FakeReviewer(review=rv, files={"pom.xml": "<project><parent>base</parent></project>\n"})
    report = make_v2(cfg, ws, reviewer=reviewer).run()
    sha = report.structure_review.commit
    assert sha and report.structure_review.note == f"review script changed 1 file(s): pom.xml — committed {sha[:10]}"
    ev = next(e for e in journal_events(ws) if e["event"] == "structure_review")
    assert "not committed yet" not in ev["note"] and sha[:10] in ev["note"]


def test_failed_review_script_leaves_no_commit(cfg, ws):
    reviewer = FakeReviewer(review=StructureReview(verdict="issues"), files={"pom.xml": "<broken/>"}, status="error")
    report = make_v2(cfg, ws, reviewer=reviewer).run()
    assert report.structure_review.commit is None and not any(s.startswith("review:") for s in subjects(ws))
    assert "<broken/>" not in ws.read("pom.xml")


def test_review_exception_never_stops_the_run(cfg, ws):
    report = make_v2(cfg, ws, reviewer=FakeReviewer(raises=RuntimeError("boom"))).run()
    assert report.success
    assert report.structure_review.verdict == "failed" and "RuntimeError: boom" in report.structure_review.note


def test_missing_review_component_is_a_note(cfg, ws):
    def factory(cfg, llm, runner, kb):
        raise ImportError("No module named 'sbmig.review'")
    report = make_v2(cfg, ws, reviewer=factory).run()
    assert report.success and report.structure_review.verdict == "failed"
    assert "structure reviewer unavailable: ImportError" in report.structure_review.note


def test_reviewer_gets_the_budgeted_llm_runner_and_kb_and_its_calls_count(cfg, ws):
    seen = {}
    llm = FakeLLM()
    kb = FakeKB()

    class Asking:
        def __init__(self, cfg, llm, runner, kb):
            seen.update(cfg=cfg, llm=llm, runner=runner, kb=kb)
            self.llm = llm

        def review(self, ws, profile, cfg, *, run_id="", restructure_notes=(), restructure_results=()):
            self.llm.complete([], model="m-review")
            return StructureReview(verdict="ok", run_id=run_id)
    orch = make_v2(cfg, ws, reviewer=Asking, llm=llm, kb=kb)
    report = orch.run()
    # kb=None: the orchestrator records the review itself, after committing its script (the line names the commit)
    assert isinstance(seen["llm"], _BudgetedLLM) and seen["runner"] is orch.script_runner and seen["kb"] is None
    assert [r.verdict for r in kb.reviews] == ["ok"]
    assert llm.calls == 1
    fix_calls = sum(a.llm_usage.calls for p in report.phases for a in p.attempts)
    assert orch.budget.llm_calls == 1 + fix_calls and report.llm_usage.calls == 1 + fix_calls
    by = {u.model: u for u in report.llm_usage_by_model}
    assert by["m-review"].calls == 1 and by["m-review"].input_tokens == 10


def test_review_skipped_in_a_dry_run_and_when_disabled(cfg, ws, tmp_path):
    reviewer = FakeReviewer()
    report = make_v2(cfg, ws, reviewer=reviewer).run(dry_run=True)
    assert reviewer.calls == [] and report.structure_review.verdict == "skipped"
    assert "dry run" in report.structure_review.note
    from sbmig.core.workspace import Workspace

    from .fakes import make_project
    root = tmp_path / "second"
    root.mkdir()
    ws2 = Workspace.from_input(make_project(root), root / "out", run_id="t2")
    ws2.git_init()
    cfg.review.enabled = False
    report = make_v2(cfg, ws2, reviewer=reviewer).run()
    assert reviewer.calls == [] and report.structure_review.verdict == "skipped" and report.success


# ------------------------------------------------------------------------------------------ MSA
def test_msa_review_once_and_cases_scoped_per_service(cfg, ws):
    A, B = "svc-a/src/main/java/A.java", "svc-b/src/main/java/B.java"
    ws.write(A, "class A { broken }\n")
    ws.write(B, "class B { broken }\n")
    ws.commit("services")

    def build(w, call):
        mod = call["module"]
        errs = [compile_error(f) for f in (A, B) if f.startswith(f"{mod}/") and "broken" in w.read(f)]
        return bad_build(*errs) if errs else ok_build()
    reviewer = FakeReviewer()
    rw = FakeRewriteRunner(ws)
    report = make_v2(cfg, ws, plan=v2_msa_plan(), profile=make_profile(CaseType.MSA), reviewer=reviewer,
                     build=FakeBuildRunner(ws, fn=build), rewrite=rw).run()
    assert report.success and len(reviewer.calls) == 1
    assert [r["module"] for r in rw.runs] == ["svc-a", "svc-b"]
    atts = [a for p in report.phases for a in p.attempts]
    cid = case_of("compile|x")
    # svc-a: a new script (v1, proven by its success); svc-b: its own scope — v1 is reused first (no LLM call; it
    # only touches svc-a's file here → no change), then augmented for svc-b
    assert [(a.cluster_id, a.script_mode, a.script_version) for a in atts] == [
        (f"case:{cid}@svc-a", "new", 1), (f"case:{cid}@svc-b", "reuse", 1), (f"case:{cid}@svc-b", "augment", 2)]
    assert atts[1].apply_errors == ["no file changed"]
