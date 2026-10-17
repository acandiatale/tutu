from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

from sbmig.core.models import (GAV, BuildError, BuildResult, CaseType, DecisionKind, ErrorKind, FixAttempt,
                               HarvestResult, IterationRecord, LLMUsage, MigrationPlan, Phase, PhaseResult,
                               PlanStep, RewriteResult, RunReport, StepKind, TriageDecision)
from sbmig.core.workspace import Workspace
from sbmig.report import console_summary, group_by_file, render_markdown, write_report


def sample_report(success: bool = False) -> RunReport:
    t0 = datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc)
    att = FixAttempt(id="spring-1-1", phase=Phase.SPRING, iteration=1, cluster_id="compile:abcd1234",
                     decision=TriageDecision(cluster_id="compile:abcd1234", kind=DecisionKind.LLM_FIX,
                                             rationale="no handler | llm"),
                     commit="0123456789abcdef", model="gpt-x", outcome="no_change", analysis="tried | renaming",
                     llm_usage=LLMUsage(model="gpt-x", calls=1, input_tokens=10, output_tokens=5))
    att2 = att.model_copy(update={"id": "spring-1-2", "decision": TriageDecision(
        cluster_id="c2", kind=DecisionKind.HANDLER, handler_id="lombok-version-floor"), "outcome": "improved",
        "analysis": ""})
    att3 = att.model_copy(update={"id": "spring-1-3", "decision": TriageDecision(
        cluster_id="c3", kind=DecisionKind.KB_RECIPE, recipe_ids=["org.openrewrite.java.Foo"]), "outcome": "build_ok"})
    att4 = att.model_copy(update={"id": "spring-1-4", "decision": TriageDecision(
        cluster_id="c4", kind=DecisionKind.KB_PATCH, kb_case_ids=["case-1"]), "model": ""})
    errs = [BuildError(kind=ErrorKind.COMPILE, message="cannot find symbol", file="src/A.java", line=3,
                       symbol="class Foo", location="class A"),
            BuildError(kind=ErrorKind.COMPILE, message="incompatible types", file="src/A.java"),
            BuildError(kind=ErrorKind.DEPENDENCY_RESOLUTION, message="could not resolve",
                       artifact=GAV.parse("a:b:1")),
            BuildError(kind=ErrorKind.UNKNOWN, message="weird", module="core"),
            BuildError(kind=ErrorKind.UNKNOWN, message="weirder")]
    pr = PhaseResult(phase=Phase.SPRING, step_ids=["spring.rewrite", "spring.loop"], iterations=3,
                     attempts=[att, att2, att3, att4],
                     rewrite_results=[RewriteResult(ok=True, recipes=["r.A"], changed_files=["x", "y"]),
                                      RewriteResult(ok=False, recipes=["r.B"], error="boom")],
                     iteration_records=[IterationRecord(phase=Phase.SPRING, iteration=1, build_ok=False, error_count=2,
                                                        decisions=[TriageDecision(cluster_id="c9",
                                                                                  kind=DecisionKind.ESCALATE,
                                                                                  rationale="needs a human"),
                                                                   TriageDecision(cluster_id="c9",
                                                                                  kind=DecisionKind.ESCALATE,
                                                                                  rationale="dup")])],
                     notes=["restructure.apply (layout): 3 file(s) changed; mandatory added: a:b", "spring note"])
    plan = MigrationPlan(profile_fingerprint="f", case_type=CaseType.SPRING_BOOT, notes=["plan note"],
                         steps=[PlanStep(id="spring.loop", kind=StepKind.BUILD_FIX_LOOP, phase=Phase.SPRING,
                                         title="spring loop")])
    return RunReport(run_id="run-1", project_name="demo", case_type=CaseType.SPRING_BOOT, started_at=t0,
                     finished_at=t0, success=success, plan=plan, phases=[pr], unresolved=[] if success else errs,
                     manual_followups=["web.xml not migrated"], flags=["test-compile-skipped", "custom-flag"],
                     llm_usage=LLMUsage(model="gpt-x", calls=3, input_tokens=30, output_tokens=15),
                     harvest=HarvestResult(cases_created=["case-9"], recipes_credited=["r.A (success)"], notes=["n"]),
                     final_build=BuildResult(ok=success, goal="clean package", command=[], exit_code=1, duration_s=1,
                                             log_path="/logs/verify.log"),
                     timings_s={"total": 125.0}, outputs={"project_dir": "/out/p"})


def test_markdown_sections():
    md = render_markdown(sample_report())
    for s in ("# Migration report — demo", "❌ NOT BUILDING", "## Unresolved errors", "### `src/A.java`",
              "L3: cannot find symbol — symbol: `class Foo` — location: class A", "artifact a:b:1", "module core",
              "(no file)", "## Manual follow-ups", "web.xml not migrated", "## Flags", "-Dmaven.test.skip=true",
              "`custom-flag`", "## What we changed", "`spring.loop` — spring loop", "| spring | r.A | 2 | yes |",
              "no: boom", "mandatory added", "## Phases", "## Fix attempts", "handler `lombok-version-floor`",
              "`Foo`", "KB case case-1", "01234567", "tried \\| renaming", "## Escalated clusters", "needs a human",
              "## Knowledge base", "case-9", "r.A (success)", "recipes run by triage", "## Notes", "plan: plan note",
              "spring: spring note", "## Outputs", "/logs/verify.log", "2m 05s", "3 call(s)"):
        assert s in md, s
    assert md.count("c9") == 1                                       # escalations de-duplicated
    ok = render_markdown(sample_report(success=True).model_copy(update={"harvest": None, "timings_s": {}}))
    assert "✅ BUILD SUCCESS" in ok and "The final build passed." in ok and "Nothing harvested." in ok


def test_markdown_minimal():
    r = RunReport(run_id="r", project_name="p", case_type=CaseType.UNKNOWN,
                  started_at=datetime.now(timezone.utc))
    md = render_markdown(r)
    assert "No structured errors" in md and "None." in md and "| Flags |" not in md


def test_write_report_roundtrip(tmp_path: Path):
    ws = Workspace(tmp_path / "p", tmp_path / "run", "run-1")
    rep = sample_report()
    paths = write_report(rep, ws)
    assert Path(paths["report_md"]).read_text().startswith("# Migration report")
    back = RunReport.model_validate_json(Path(paths["report_json"]).read_text())
    assert back.unresolved == rep.unresolved and back.phases[0].attempts[0].id == "spring-1-1"
    assert back.outputs["report_md"] == paths["report_md"]


def test_group_by_file_and_console():
    groups = group_by_file(sample_report().unresolved)
    assert list(groups)[0] == "src/A.java" and len(groups["src/A.java"]) == 2
    console = Console(record=True, width=140)
    rep = sample_report()
    rep.outputs["report_md"] = "/out/report.md"
    rep.unresolved = rep.unresolved * 2
    console_summary(rep, console)
    text = console.export_text()
    assert "NOT BUILDING" in text and "unresolved" in text and "follow-up" in text and "/out/report.md" in text
