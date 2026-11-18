"""`RunReport.code_changes` through the orchestrator: the base commit is taken after the restructure (before the
structure review and the first verify build), and the fix loop's net changes end up in the report."""
from __future__ import annotations

import pytest

from sbmig.core.models import ReviewIssue, StructureReview
from sbmig.loop import parse_stop_after

from .fakes import FakeReviewer, FakeRewriteRunner, journal_events, v2_plan
from .test_orchestrator_framework import JSP, JSP_TEXT, fake_fw, reduced_orch  # noqa: F401 - fake_fw is a fixture
from .test_v2_orchestrator import BAR, FIXED, FOO, make_v2, subjects


def commit_of(ws, subject: str) -> str:
    return ws._git("rev-list", "-n1", f"--grep={subject}", "--fixed-strings", "HEAD").stdout.strip()


def base_events(ws) -> list[dict]:
    return [e for e in journal_events(ws) if e["event"] == "code_changes_base"]


@pytest.mark.parametrize("engine", ["graph", "inline"])
def test_base_is_the_restructure_commit_and_the_fix_loop_changes_are_listed(cfg, ws, engine):
    cfg.loop.engine = engine
    rw = FakeRewriteRunner(ws, effect=lambda w, refs: (w.write("rewritten.txt", "x") or ["rewritten.txt"]))
    report = make_v2(cfg, ws, rewrite=rw).run()
    assert report.success
    restructure = commit_of(ws, "restructure: restructure.apply")
    assert report.code_changes_base == restructure
    [ev] = base_events(ws)
    assert ev["commit"] == restructure and ev["step"] == "restructure.review"
    paths = [c.path for c in report.code_changes]
    assert paths == [BAR, FOO] and report.code_changes_total == 2
    # the automatic conversion (rewrite, restructure) is not in the list
    assert "rewritten.txt" not in paths and "module/boot-jar/pom.xml" not in paths
    foo = report.code_changes[1]
    assert foo.status == "modified" and f"+{FIXED.strip()}" in foo.diff and foo.reasons == ["fixed the case"]
    assert any(s.startswith("fix(script:") for s in subjects(ws))


def test_the_review_fix_is_part_of_the_code_changes(cfg, ws):
    rv = StructureReview(verdict="issues", issues=[ReviewIssue(severity="critical", file="pom.xml", problem="x")])
    reviewer = FakeReviewer(review=rv, files={"pom.xml": "<project><parent>base</parent></project>\n"})
    report = make_v2(cfg, ws, reviewer=reviewer).run()
    assert report.code_changes_base == commit_of(ws, "restructure: restructure.apply")
    assert [c.path for c in report.code_changes] == [BAR, FOO, "pom.xml"]


def test_a_run_without_the_review_takes_the_base_before_the_first_build(cfg, ws):
    cfg.review.enabled = False
    report = make_v2(cfg, ws, plan=v2_plan(review=False)).run()
    [ev] = base_events(ws)
    assert ev["step"] == "verify.build" and report.code_changes_base == commit_of(ws, "restructure: restructure.apply")
    assert [c.path for c in report.code_changes] == [BAR, FOO]


def test_a_run_stopped_before_the_review_has_no_code_changes(cfg, ws):
    report = make_v2(cfg, ws).run(stop_after=parse_stop_after("transform"))
    assert report.code_changes == [] and report.code_changes_base == "" and report.code_changes_total == 0
    assert base_events(ws) == []


def test_a_dry_run_has_no_code_changes(cfg, ws):
    report = make_v2(cfg, ws).run(dry_run=True)
    assert report.code_changes == [] and report.code_changes_base == "" and base_events(ws) == []


def test_a_framework_scope_run_takes_the_base_after_the_jsp_rename(cfg, ws, fake_fw):
    ws.write(JSP, JSP_TEXT)
    ws.commit("jsp")
    orch, *_ = reduced_orch(cfg, ws)
    report = orch.run()
    jsp_commit = commit_of(ws, "transform: JSP javax")
    [ev] = base_events(ws)
    assert ev["step"] == "verify.build" and ev["commit"] == jsp_commit
    # the automatic JSP rename is part of the conversion, not of the code changes
    assert report.code_changes_base == jsp_commit and report.code_changes == []


def test_a_run_aborted_before_the_review_has_no_base_even_after_the_final_build(cfg, ws, monkeypatch):
    orch = make_v2(cfg, ws)

    def boom(pr, step):
        ws.write("half-written.txt", "left behind\n")
        raise RuntimeError("simulated crash in the restructure")
    monkeypatch.setattr(orch, "_step_restructure", boom)
    report = orch.run()
    assert any(f.startswith("aborted") for f in report.flags)
    assert any(e["event"] == "phase_start" and e.get("after_abort") for e in journal_events(ws))   # the build ran
    assert base_events(ws) == [] and report.code_changes == [] and report.code_changes_base == ""
