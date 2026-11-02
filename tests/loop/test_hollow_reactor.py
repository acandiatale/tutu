"""v2.1z-c (2026-11-02): a hollow reactor (the poms' roots hold no sources, the tree holds them elsewhere) is flagged at
analyze, named first in the Handover, journaled, and makes the run NOT a success (exit 2, `attn=hollow-reactor`) even
though every build passes; the soft case (`sources-outside-reactor`) is a follow-up only."""
from __future__ import annotations

from pathlib import Path

from sbmig.analyze import analyze_project
from sbmig.core.workspace import Workspace
from sbmig.report.handover import build_handover
from sbmig.report.result_line import report_exit_code, report_result_line

from .fakes import FakeBuildRunner, journal_events, ok_build
from .test_orchestrator import make_orch

NS = 'xmlns="http://maven.apache.org/POM/4.0.0"'
JAVA = "package com.example.hollow;\npublic class HelloServlet {}\n"
WEBXML = '<?xml version="1.0"?><web-app version="3.1"/>'
POM = (f'<?xml version="1.0" encoding="UTF-8"?>\n<project {NS}>\n  <modelVersion>4.0.0</modelVersion>\n'
       "  <groupId>com.example</groupId>\n  <artifactId>hollow</artifactId>\n  <version>1.0</version>\n"
       "  <packaging>war</packaging>\n  <properties><maven.compiler.source>1.8</maven.compiler.source>"
       "<maven.compiler.target>1.8</maven.compiler.target></properties>\n"
       "  <dependencies><dependency><groupId>javax.servlet</groupId><artifactId>javax.servlet-api</artifactId>"
       "<version>3.1.0</version><scope>provided</scope></dependency></dependencies>\n</project>\n")
HOLLOW = {".gitignore": "target/\n", "pom.xml": POM, "src/main/webapp/WEB-INF/web.xml": WEBXML,
          "DEV/s10/src/com/example/hollow/HelloServlet.java": JAVA,
          "DEV/s10/public_html/WEB-INF/web.xml": WEBXML, "DEV/s10/public_html/index.jsp": "<%= 1 %>",
          "STATIC/css/app.css": "body{}", "DB/schema.sql": "-- sql"}


def _ws(tmp_path: Path, files: dict[str, str]) -> Workspace:
    src = tmp_path / "src" / "proj"
    for rel, text in files.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    w = Workspace.from_input(src, tmp_path / "out", run_id="t-run")
    w.git_init()
    return w


def _run(cfg, ws):
    orch = make_orch(cfg, ws, analyzer=analyze_project, build=FakeBuildRunner(ws, fn=lambda w, call: ok_build()))
    return orch, orch.run()


def test_hollow_reactor_is_flagged_first_and_fails_the_run(cfg, tmp_path):
    ws = _ws(tmp_path, HOLLOW)
    _orch, report = _run(cfg, ws)
    assert "hollow-reactor" in report.flags and "sources-outside-reactor" not in report.flags
    assert report.final_build is not None and report.final_build.ok          # every build passed …
    assert report.success is False                                           # … and still not a success
    assert report_exit_code(report) == 2
    line = report_result_line(report, 2)
    # `build=` mirrors `success` (as for restructure-failed): mvn passed, the deliverable did not
    assert "exit=2 build=FAIL" in line and "hollow-reactor" in line.split("attn=")[1]
    first = report.manual_followups[0]
    assert first.startswith("the Maven reactor compiles no sources: 1 java, 1 jsp, 1 web files lie outside its modules")
    assert "(DEV: java 1, jsp 1; STATIC: web 1)" in first and "unresolved" not in first
    ev = [e for e in journal_events(ws) if e["event"] == "hollow_reactor"]
    assert len(ev) == 1 and ev[0]["inside"] == {"java": 0, "jsp": 0, "web": 0}
    assert ev[0]["outside"] == {"java": 1, "jsp": 1, "web": 1} and ev[0]["war"] is True
    assert ev[0]["outside_by_dir"] == {"DEV": {"java": 1, "jsp": 1}, "STATIC": {"web": 1}}
    # the Handover's "from here on" list opens with it
    h = build_handover(report.profile, None, report, cfg)
    assert h.from_here and h.from_here[0] == first


def test_a_healthy_tree_has_neither_flag(cfg, tmp_path):
    ws = _ws(tmp_path, {"pom.xml": POM, "src/main/java/com/example/hollow/HelloServlet.java": JAVA,
                        "src/main/webapp/WEB-INF/web.xml": WEBXML, "src/main/webapp/index.jsp": "x"})
    _orch, report = _run(cfg, ws)
    assert "hollow-reactor" not in report.flags and "sources-outside-reactor" not in report.flags
    assert report.success and not [e for e in journal_events(ws) if e["event"] == "hollow_reactor"]
    assert not any(f.startswith("the Maven reactor compiles no sources") for f in report.manual_followups)


def test_more_java_outside_is_only_a_followup(cfg, tmp_path):
    ws = _ws(tmp_path, {"pom.xml": POM, "src/main/java/com/example/hollow/HelloServlet.java": JAVA,
                        "src/main/webapp/WEB-INF/web.xml": WEBXML, "src/main/webapp/index.jsp": "x",
                        "old/com/example/hollow/A.java": JAVA, "old/com/example/hollow/B.java": JAVA})
    _orch, report = _run(cfg, ws)
    assert "sources-outside-reactor" in report.flags and "hollow-reactor" not in report.flags
    assert report.success and report_exit_code(report) == 0
    assert any(f.startswith("sources outside the Maven reactor: 2 java files") for f in report.manual_followups)
    assert [e for e in journal_events(ws) if e["event"] == "sources_outside_reactor"]
