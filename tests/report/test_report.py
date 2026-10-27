from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

from sbmig.core.models import (GAV, BuildError, BuildResult, CaseType, DecisionKind, ErrorKind, FixAttempt,
                               IterationRecord, LLMUsage, MigrationPlan, Phase, PhaseResult, PlanStep, RewriteResult,
                               RunReport, ScriptRunResult, StepKind, TriageDecision)
from sbmig.core.workspace import Workspace
from sbmig.report import console_summary, group_by_file, render_markdown, write_report


def sample_report(success: bool = False) -> RunReport:
    t0 = datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc)
    att = FixAttempt(id="spring-1-1", phase=Phase.SPRING, iteration=1, cluster_id="case:abcd1234",
                     decision=TriageDecision(cluster_id="case:abcd1234", kind=DecisionKind.SCRIPT,
                                             rationale="new script | llm"),
                     commit="0123456789abcdef", model="gpt-x", outcome="no_change", analysis="tried | renaming",
                     script_mode="new", script_case_id="abcd1234", script_version=1,
                     llm_usage=LLMUsage(model="gpt-x", calls=1, input_tokens=10, output_tokens=5))
    att2 = att.model_copy(update={"id": "spring-1-2", "cluster_id": "case:c2", "script_case_id": "c2",
                                  "script_mode": "reuse", "script_version": 2, "outcome": "improved", "analysis": "",
                                  "script_result": ScriptRunResult(status="ok", changed_files=["pom.xml"])})
    att3 = att.model_copy(update={"id": "spring-1-3", "cluster_id": "case:c3", "script_case_id": "c3",
                                  "script_mode": "augment", "script_version": 3, "outcome": "build_ok",
                                  "script_result": ScriptRunResult(status="error", error="KeyError: 'file'")})
    att4 = att.model_copy(update={"id": "spring-1-4", "cluster_id": "c4", "decision": TriageDecision(
        cluster_id="c4", kind=DecisionKind.ESCALATE, rationale="no script"), "model": "", "script_mode": None,
        "script_case_id": "", "script_version": 0, "advisory_ids": ["hystrix"]})
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
                     final_build=BuildResult(ok=success, goal="clean package", command=[], exit_code=1, duration_s=1,
                                             log_path="/logs/verify.log"),
                     timings_s={"total": 125.0}, outputs={"project_dir": "/out/p"})


def test_markdown_sections():
    md = render_markdown(sample_report())
    for s in ("# Migration report — demo", "❌ NOT BUILDING", "## Error list", "### `src/A.java`",
              "L3: cannot find symbol — symbol: `class Foo` — location: class A", "artifact a:b:1", "module core",
              "(no file)", "## Manual follow-ups", "web.xml not migrated", "## Flags", "-Dmaven.test.skip=true",
              "`custom-flag`", "## What we changed", "`spring.loop` — spring loop", "| spring | r.A | 2 | yes |",
              "no: boom", "## Notes", "plan: plan note", "spring: spring note", "## Outputs", "/logs/verify.log",
              "2m 05s", "3 call(s)",
              "| SBMIG-RESULT | `SBMIG-RESULT run=run-1 exit=2 build=FAIL unresolved=5 blocked=0 open=0 resolved=0 "
              "llm=3 attn=unresolved:5,test-compile-skipped` |"):
        assert s in md, s
    # v2.1: the phases / attempts / escalation tables are gone (the error list and fixes by case replace them)
    for gone in ("## Knowledge base", "## Phases", "## Fix attempts", "## Escalated clusters", "## Unresolved errors",
                 "Restructure / deterministic edits:", "script:abcd1234", "needs a human"):
        assert gone not in md, gone
    ok = render_markdown(sample_report(success=True).model_copy(update={"timings_s": {}}))
    assert "✅ BUILD SUCCESS" in ok and "The final build passed." in ok and "## Knowledge base" not in ok
    assert "`SBMIG-RESULT run=run-1 exit=0 build=ok" in ok


def test_markdown_minimal():
    r = RunReport(run_id="r", project_name="p", case_type=CaseType.UNKNOWN,
                  started_at=datetime.now(timezone.utc))
    md = render_markdown(r)
    assert "No structured errors" in md and "| Flags |" not in md and "## Manual follow-ups" not in md
    assert "## What we changed" not in md


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


# ---------------------------------------------------------------- handover (round 4)
def _handover_report() -> RunReport:
    from sbmig.core.models import AdvisoryFinding, HandoverSummary, VersionChange

    rep = sample_report()
    rep.advisories = [AdvisoryFinding(advisory_id="sleuth", library="sleuth", status="eol", title="Sleuth is gone",
                                      policy="guide", detected_version="3.1.9", outcome="still-present",
                                      replacement="io.micrometer:micrometer-tracing-bridge-brave",
                                      followup="move to Micrometer Tracing", docs=["https://x/doc"])]
    co = VersionChange(key="org.apache.commons:commons-lang3", before="3.12.0", after="3.12.0",
                       before_source="managed", reason="carried over (no longer managed)", carried_over=True)
    rep.handover = HandoverSummary(parent_before="org.springframework.boot:spring-boot-starter-parent:2.7.18",
                                   parent_after="com.company:company-parent:4.1.0", java_before="1.8",
                                   java_after="21", changes=[co], carried_over=[co], added=["a:b:1 (mandatory)"],
                                   removed=["c:d:2"], from_here=["Sleuth is gone: move to Micrometer Tracing",
                                                                 "web.xml not migrated"])
    return rep


def test_handover_section_first_and_tables():
    md = render_markdown(_handover_report())
    i = md.index("## Handover — what we replaced, what is yours")
    assert md.index("| Run id |") < i < md.index("## Error list")
    assert md[:i].count("\n## ") == 0                               # first section after the header table
    for s in ("### Versions replaced", "- parent: `org.springframework.boot:spring-boot-starter-parent:2.7.18` → "
              "`com.company:company-parent:4.1.0`", "- java: `1.8` → `21`", "| what | before | after | why |",
              "| ⚠ dependency `org.apache.commons:commons-lang3` | 3.12.0 (managed) | 3.12.0 |",
              "Added: `a:b:1 (mandatory)`", "Removed: `c:d:2`", "### Libraries needing your attention",
              "| library | as-is | status | what we did | outcome | what you do next | docs |",
              "| **sleuth** | 3.1.9 | eol | built with the as-is version pinned | ⚠ still present | "
              "move to Micrometer Tracing → `io.micrometer:micrometer-tracing-bridge-brave` | [1](https://x/doc) |",
              "### From here on, your team", "1. Sleuth is gone: move to Micrometer Tracing",
              "2. web.xml not migrated"):
        assert s in md, s


def test_handover_nothing_to_say():
    from sbmig.core.models import HandoverSummary

    md = render_markdown(sample_report())
    assert "## Handover" in md and "No version changes and no library advisories recorded" in md
    rep = sample_report()
    rep.handover = HandoverSummary()
    md = render_markdown(rep)
    sec = md[md.index("## Handover"):md.index("## Error list")]
    assert "No version changes and no library advisories recorded" in sec and "###" not in sec


def test_handover_console_and_flag_help():
    from sbmig.report.markdown import flag_help

    console = Console(record=True, width=160)
    console_summary(_handover_report(), console)
    text = console.export_text()
    assert "advisory sleuth → still-present" in text and "[guide]" not in text
    assert "handover: 1 versions replaced, 1 carried over, 2 items for the owners" in text
    assert flag_help("managed-versions-carried-over") is None          # a v1 handler flag: no help text any more
