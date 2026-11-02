"""v2.1z-d (2026-11-02, business decision: ADF projects are not converted): two or more ADF evidence families stop the run at
analyze with "ADF 프로젝트라 변환이 불가능합니다" (`intake.adf_policy: stop` — flag `adf-project`, exit 3, the message + sizing as the
Handover's first line, journal `adf_project`, "## ADF sizing" with the evidence, no Maven / LLM call); `continue` reports only
and runs on; one family is a trace (`adf-traces`, a note, the run goes on)."""
from __future__ import annotations

from pathlib import Path

from sbmig.analyze import analyze_project
from sbmig.core.workspace import Workspace
from sbmig.report.handover import build_handover
from sbmig.report.markdown import render_markdown
from sbmig.report.result_line import report_exit_code, report_result_line

from .fakes import FakeBuildRunner, journal_events, ok_build
from .test_orchestrator import make_orch

NS = 'xmlns="http://maven.apache.org/POM/4.0.0"'
BC4J = 'xmlns="http://xmlns.oracle.com/bc4j"'
AF = 'xmlns:af="http://xmlns.oracle.com/adf/faces/rich"'
POM = (f'<?xml version="1.0" encoding="UTF-8"?>\n<project {NS}>\n  <modelVersion>4.0.0</modelVersion>\n'
       "  <groupId>com.example</groupId>\n  <artifactId>s10web</artifactId>\n  <version>1.0</version>\n"
       "  <packaging>war</packaging>\n  <properties><maven.compiler.source>1.8</maven.compiler.source>"
       "<maven.compiler.target>1.8</maven.compiler.target></properties>\n"
       "  <dependencies><dependency><groupId>javax.servlet</groupId><artifactId>javax.servlet-api</artifactId>"
       "<version>3.1.0</version><scope>provided</scope></dependency></dependencies>\n</project>\n")
WEBXML = '<?xml version="1.0"?><web-app version="3.1"/>'
BASE = {".gitignore": "target/\n", "pom.xml": POM, "src/main/webapp/WEB-INF/web.xml": WEBXML,
        "DEV/s10/src/view/Util.java": "package view;\npublic class Util {}\n"}
FAM_A = {"DEV/s10/src/model/Emp.xml": f'<Entity {BC4J} Name="Emp" DBObjectName="EMP"/>',
         "DEV/s10/src/model/EmpView.xml": f'<ViewObject {BC4J} Name="EmpView"/>',
         "DEV/s10/src/model/HRModule.xml": f'<AppModule {BC4J} Name="HRModule"/>'}
FAM_B = {"DEV/s10/src/model/EmpImpl.java": "package model;\nimport oracle.jbo.server.EntityImpl;\npublic class EmpImpl {}\n"}
FAM_C = {"DEV/s10/adfmsrc/model/adfm.xml": '<MetadataDirectory xmlns="http://xmlns.oracle.com/adfm/metainf"/>'}
FAM_D = {"DEV/s10/public_html/main.jspx": f'<jsp:root {AF}/>',
         "DEV/s10/public_html/WEB-INF/adfc-config.xml": '<adfc-config xmlns="http://xmlns.oracle.com/adf/controller">'
                                                        '<view id="main"><page>/main.jspx</page></view></adfc-config>'}
FIRST = ("ADF project (Oracle JDeveloper / ADF): conversion is not possible — ADF 프로젝트라 변환이 불가능합니다 (evidence: bc4j, "
         "java, model, faces; 1 entities / 1 tables, 1 view objects, 1 application modules, 0 page definitions, 1 task flows, "
         "1 pages, 1 of 2 java files on ADF)")


def _ws(tmp_path: Path, *parts: dict[str, str]) -> Workspace:
    src = tmp_path / "src" / "proj"
    for files in parts:
        for rel, text in files.items():
            p = src / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
    w = Workspace.from_input(src, tmp_path / "out", run_id="t-run")
    w.git_init()
    return w


def _run(cfg, ws):
    calls = {"builds": 0}

    def build(w, call):
        calls["builds"] += 1
        return ok_build()
    orch = make_orch(cfg, ws, analyzer=analyze_project, build=FakeBuildRunner(ws, fn=build))
    return orch, orch.run(), calls


def test_adf_project_stops_at_analyze_with_the_message_first(cfg, tmp_path):
    ws = _ws(tmp_path, BASE, FAM_A, FAM_B, FAM_C, FAM_D)
    assert cfg.intake.adf_policy == "stop"
    _orch, report, calls = _run(cfg, ws)
    assert "adf-project" in report.flags and "aborted" in report.flags and "adf-traces" not in report.flags
    assert report.profile.adf is not None and report.profile.adf.is_adf and report.profile.adf.families == ["bc4j", "java", "model", "faces"]
    assert calls["builds"] == 0 and report.llm_usage.calls == 0       # no Maven, no LLM
    assert report.success is False and report_exit_code(report) == 3
    line = report_result_line(report, 3)
    assert "exit=3" in line and "adf-project" in line.split("attn=")[1]
    assert report.manual_followups[0] == FIRST
    assert any(f == "run aborted: ADF project — conversion is not possible (ADF 프로젝트라 변환이 불가능합니다; intake.adf_policy: stop)"
               for f in report.manual_followups)
    ev = [e for e in journal_events(ws) if e["event"] == "adf_project"]
    assert len(ev) == 1 and ev[0]["policy"] == "stop" and ev[0]["families"] == ["bc4j", "java", "model", "faces"]
    assert ev[0]["evidence"]["java"] == ["DEV/s10/src/model/EmpImpl.java"] and ev[0]["entities"] == 1 and ev[0]["java_total"] == 2
    assert not [e for e in journal_events(ws) if e["event"] in ("hollow_reactor", "adf_traces")]
    h = build_handover(report.profile, None, report, cfg)
    assert h.from_here and h.from_here[0] == FIRST
    md = render_markdown(report, "en", cfg)
    assert "## ADF sizing" in md and "**ADF project — conversion is not possible** (4 evidence families: bc4j, java, model, faces)" in md
    assert "| entities (BC4J) / tables | 1 / 1 |" in md and "### Evidence" in md and "- **java** (1): `DEV/s10/src/model/EmpImpl.java`" in md
    assert "rewrite" not in md.lower()
    ko = render_markdown(report, "ko", cfg)
    assert "## ADF 규모" in ko and "판정: **ADF 프로젝트라 변환이 불가능합니다** (근거 계열 4개: bc4j, java, model, faces)" in ko
    assert "### 판정 근거 / evidence" in ko and "ADF 프로젝트라 변환이 불가능합니다 (근거: bc4j, java, model, faces; 엔티티 1개" in ko


def test_adf_policy_continue_reports_only_and_runs_on(cfg, tmp_path):
    cfg.intake.adf_policy = "continue"
    ws = _ws(tmp_path, BASE, FAM_A, FAM_B)
    _orch, report, calls = _run(cfg, ws)
    assert "adf-project" in report.flags and "aborted" not in report.flags
    assert calls["builds"] >= 1 and report.final_build is not None       # the flow went on to the builds
    assert report.manual_followups[0].endswith(" — intake.adf_policy: continue (report only, the run goes on; not recommended)")
    assert report.manual_followups[1].startswith("the Maven reactor compiles no sources:")   # the hollow line follows it
    assert next(e for e in journal_events(ws) if e["event"] == "adf_project")["policy"] == "continue"
    assert "hollow-reactor" in report.flags and report_exit_code(report) == 2


def test_one_family_is_a_trace_the_run_goes_on(cfg, tmp_path):
    ws = _ws(tmp_path, BASE, FAM_D)
    _orch, report, calls = _run(cfg, ws)
    assert "adf-traces" in report.flags and "adf-project" not in report.flags and "aborted" not in report.flags
    assert calls["builds"] >= 1
    note = next(f for f in report.manual_followups if f.startswith("ADF traces (one evidence family only — faces: "))
    assert "ADF 흔적: 변환은 계속하되 확인 필요" in note and "0 entities / 0 tables" in note and "1 pages" in note
    assert report.manual_followups[0].startswith("the Maven reactor compiles no sources:")   # the trace note is not first
    ev = [e for e in journal_events(ws) if e["event"] == "adf_traces"]
    assert len(ev) == 1 and ev[0]["families"] == ["faces"] and "policy" not in ev[0]
    md = render_markdown(report, "en", cfg)
    assert "**ADF traces only** (one evidence family: faces)" in md and "- **faces** (2):" in md


def test_a_servlet_project_is_not_touched(cfg, tmp_path):
    ws = _ws(tmp_path, {"pom.xml": POM, "src/main/webapp/WEB-INF/web.xml": WEBXML,
                        "src/main/java/a/Hello.java": "package a;\npublic class Hello {}\n",
                        "src/main/resources/adf-config.xml": "<adf-config/>"})
    _orch, report, _calls = _run(cfg, ws)
    assert "adf-project" not in report.flags and "adf-traces" not in report.flags and report.profile.adf is None
    assert not [e for e in journal_events(ws) if e["event"] in ("adf_project", "adf_traces")]
    assert "## ADF sizing" not in render_markdown(report, "en", cfg)
