"""Ant projects are refused at intake with the company's notice (docs/INTERFACES.md "v2.1z-aj", bullet "Ant"):
`intake.ant_to_maven` is false by default and the abort reason people see is exactly `intake.ant_message` — the ERROR
line, the first line of report.md and the journal's `abort` event; the technical detail (which build.xml) is a second
line. Nothing is synthesized and Maven is never called. Applies to both flows; the remediation flow refuses an Ant
project even with the synthesis switched on (it never edits the project)."""
from __future__ import annotations

import logging
import sys
import types
from pathlib import Path

import pytest

from sbmig.cli import run_exit_code
from sbmig.core.workspace import Workspace
from sbmig.report.markdown import render_markdown

from .fakes import FakeBuildRunner, journal_events
from .test_orchestrator import make_orch

NOTICE = "Maven 변환 후 신청 부탁드립니다."


def _ant_tree(tmp_path: Path) -> Path:
    src = tmp_path / "src" / "legacy-web"
    for rel, text in {"build.xml": "<project name=\"legacy-web\" default=\"war\"/>\n",
                      "src/com/x/A.java": "package com.x;\nclass A {}\n",
                      "WebContent/WEB-INF/web.xml": "<web-app/>\n"}.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return src


@pytest.fixture()
def no_synthesis(monkeypatch):
    """The pom synthesis must not run: a stub `sbmig.analyze.ant` that fails when called."""
    calls: list = []

    def synthesize_pom(ws, cfg, **kw):
        calls.append(ws)
        raise AssertionError("pom synthesis must not run")
    mod = types.ModuleType("sbmig.analyze.ant")
    mod.synthesize_pom = synthesize_pom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sbmig.analyze.ant", mod)
    return calls


def _boom(*a, **k):
    raise AssertionError("must not run for a refused project")


def test_default_is_refusal_with_the_company_notice(cfg):
    assert cfg.intake.ant_to_maven is False and cfg.intake.ant_message == NOTICE


@pytest.mark.parametrize("engine", ["inline", "graph"])
def test_full_flow_refuses_ant_with_the_notice(cfg, tmp_path, no_synthesis, caplog, engine):
    cfg = cfg.model_copy(deep=True)
    cfg.loop.engine = engine
    ws = Workspace.from_input(_ant_tree(tmp_path), tmp_path / "out", run_id="t-ant")
    ws.git_init()
    build = FakeBuildRunner(ws)
    orch = make_orch(cfg, ws, build=build, analyzer=_boom)
    captured: list = []
    orch._capture_effective = lambda report: captured.append(True)
    with caplog.at_level(logging.INFO, logger="sbmig"):
        report = orch.run()
    assert run_exit_code(report) == 3 and "aborted" in report.flags and "unsupported-build-tool:ant" in report.flags
    assert no_synthesis == [] and build.calls == [] and not captured and not ws.exists("pom.xml")
    assert report.pom_synthesis is None and "pom-synthesized" not in report.flags
    assert report.manual_followups[0] == f"run aborted: {NOTICE}"
    assert report.manual_followups[1] == ("Ant build file build.xml found and no pom.xml: the Ant → Maven conversion is "
                                          "switched off (intake.ant_to_maven: false)")
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert errors == [f"run aborted: {NOTICE}"]
    ev = journal_events(ws)
    assert [e["reason"] for e in ev if e["event"] == "abort"] == [NOTICE]
    tool = [e for e in ev if e["event"] == "build_tool"]
    assert tool[0]["ok"] is False and tool[0]["reason"] == NOTICE and tool[0]["build_file"] == "build.xml"
    assert "build.xml" in tool[0]["detail"]
    md = (ws.run_dir / "report.md").read_text(encoding="utf-8").splitlines()
    assert md[0].startswith("# ") and md[2] == f"**{NOTICE}**"
    assert md[4].startswith("_Ant build file build.xml found")
    ko = render_markdown(report, "ko").splitlines()
    assert ko[2] == f"**{NOTICE}**"
    assert ko[4] == ("_Ant 빌드 파일(build.xml)만 있고 pom.xml은 없습니다. Ant → Maven 변환은 꺼져 있습니다"
                     "(intake.ant_to_maven: false)._")


def test_the_notice_comes_from_the_config(cfg, tmp_path, no_synthesis):
    cfg = cfg.model_copy(deep=True)
    cfg.intake.ant_message = "Please convert the build to Maven first."
    ws = Workspace.from_input(_ant_tree(tmp_path), tmp_path / "out", run_id="t-ant")
    ws.git_init()
    report = make_orch(cfg, ws, analyzer=_boom).run()
    assert report.manual_followups[0] == "run aborted: Please convert the build to Maven first."


@pytest.mark.parametrize("engine", ["inline", "graph"])
@pytest.mark.parametrize("switch", [False, True])
def test_remediation_flow_refuses_ant_even_with_the_synthesis_on(cfg, tmp_path, no_synthesis, monkeypatch, engine,
                                                                switch):
    from sbmig.loop import Orchestrator
    from sbmig.remediations import chain
    monkeypatch.setattr(chain, "run_only", _boom)
    cfg = cfg.model_copy(deep=True)
    cfg.loop.engine = engine
    cfg.intake.ant_to_maven = switch
    ws = Workspace.from_input(_ant_tree(tmp_path), tmp_path / "out", run_id="t-ant-rem")
    build = FakeBuildRunner(ws)
    report = Orchestrator(cfg, ws, build_runner=build).run(flow="remediation")
    assert run_exit_code(report) == 3 and "unsupported-build-tool:ant" in report.flags
    assert no_synthesis == [] and build.calls == [] and not ws.exists("pom.xml") and not (ws.root / ".git").exists()
    assert report.manual_followups[0] == f"run aborted: {NOTICE}"
    assert "remediation flow builds the project as uploaded" in report.manual_followups[1]
    assert report.remediation is None
    md = (ws.run_dir / "report.md").read_text(encoding="utf-8").splitlines()
    assert md[2] == f"**{NOTICE}**" and "only the vulnerability remediation" in "\n".join(md)
    assert [e["reason"] for e in journal_events(ws) if e["event"] == "abort"] == [NOTICE]
    if engine == "graph":
        assert [e["node"] for e in journal_events(ws) if e["event"] == "graph_node_end"] == ["intake", "finalize"]
