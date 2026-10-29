"""v2.1k (2026-10-29): the intake de-duplicates the reactor before any Maven call (commit, journal `reactor_dedupe`,
flag `reactor-duplicate-removed`, plain-words follow-up); a tree without a root pom gets an aggregator also for a
single top pom (`APP/pom.xml` aggregating `../DEV`), and a rootless non-MSA tree stops at analyze (`no-root-pom`)."""
from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.cli import run_exit_code
from sbmig.core.models import CaseType, Phase
from sbmig.core.workspace import Workspace

from .fakes import FakeBuildRunner, journal_events, make_profile
from .test_orchestrator import files_build, make_orch

NS = 'xmlns="http://maven.apache.org/POM/4.0.0"'


def _pom(aid: str, packaging: str = "jar", modules: tuple[str, ...] = (), extra: str = "") -> str:
    mods = ("  <modules>\n" + "".join(f"    <module>{m}</module>\n" for m in modules) + "  </modules>\n"
            if modules else "")
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<project {NS}>\n  <modelVersion>4.0.0</modelVersion>\n'
            f"  <groupId>com.xxx</groupId>\n  <artifactId>{aid}</artifactId>\n  <version>1.0.0-SNAPSHOT</version>\n"
            f"  <packaging>{packaging}</packaging>\n{mods}{extra}</project>\n")


def _ws(tmp_path: Path, files: dict[str, str]) -> Workspace:
    src = tmp_path / "src" / "proj"
    for rel, text in files.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    w = Workspace.from_input(src, tmp_path / "out", run_id="t-run")
    w.git_init()
    return w


COMPANY = {
    "pom.xml": _pom("xxx-root", "pom", ("APP", "DEV")),
    "APP/pom.xml": _pom("xxx-app", "pom", ("../DEV", "./EAR", "./BATCH")),
    "APP/EAR/pom.xml": _pom("xxx-ear", "ear"),
    "APP/BATCH/pom.xml": _pom("xxx-batch"),
    "DEV/pom.xml": _pom("xxx", "war"),
}
FOLLOWUP = ("DEV was listed twice in the reactor (pom.xml and APP/pom.xml); the APP listing was removed so Maven can "
            "build — if APP is meant to be built on its own, add DEV back there and remove it from the root")


def _subjects(ws: Workspace) -> list[str]:
    return ws._git("log", "--format=%s").stdout.splitlines()


def _modules(ws: Workspace, rel: str) -> list[str]:
    from sbmig.restructure.pom_editor import PomEditor
    return PomEditor(ws.abs(rel)).modules()


@pytest.mark.parametrize("engine", ["inline", "graph"])
def test_intake_removes_the_nested_duplicate_before_maven(cfg, tmp_path, engine):
    cfg = cfg.model_copy(deep=True)
    cfg.loop.engine = engine
    ws = _ws(tmp_path, COMPANY)
    seen: list[list[str]] = []
    orch = make_orch(cfg, ws, build=FakeBuildRunner(ws, fn=files_build))
    # the effective-pom capture is the first Maven call: by then the reactor is de-duplicated
    orch._capture_effective = lambda report: seen.append(_modules(ws, "APP/pom.xml"))
    report = orch.run(stop_after=Phase.INTAKE)
    assert seen == [["./EAR", "./BATCH"]]
    assert "reactor-duplicate-removed" in report.flags and "aborted" not in report.flags
    assert _modules(ws, "pom.xml") == ["APP", "DEV"]                       # the root pom keeps its listing
    assert FOLLOWUP in report.manual_followups
    subject, _, body = ws._git("log", "-1", "--format=%B").stdout.strip().partition("\n\n")
    assert subject == "intake: reactor duplicate removed (DEV listed by pom.xml and APP/pom.xml)"
    assert body == "APP/pom.xml: <module>../DEV</module> removed — DEV stays listed by pom.xml"
    ev = [e for e in journal_events(ws) if e["event"] == "reactor_dedupe"]
    assert len(ev) == 1 and ev[0]["removed"] == ["APP/pom.xml: ../DEV"] and ev[0]["duplicates"] == ["DEV"]
    assert ev[0]["kept"] == ["pom.xml: DEV"] and len(ev[0]["notes"]) == 1


def test_nothing_to_dedupe_leaves_no_trace(cfg, tmp_path):
    ws = _ws(tmp_path, {**COMPANY, "APP/pom.xml": _pom("xxx-app", "pom", ("./EAR", "./BATCH"))})
    report = make_orch(cfg, ws, build=FakeBuildRunner(ws, fn=files_build)).run(stop_after=Phase.INTAKE)
    assert "reactor-duplicate-removed" not in report.flags
    assert not any(s.startswith("intake: reactor") for s in _subjects(ws))
    assert not [e for e in journal_events(ws) if e["event"] == "reactor_dedupe"]


def test_an_inactive_profile_duplicate_is_only_a_followup(cfg, tmp_path):
    prof = ("  <profiles>\n    <profile>\n      <id>full</id>\n      <modules>\n        <module>../DEV</module>\n"
            "      </modules>\n    </profile>\n  </profiles>\n")
    ws = _ws(tmp_path, {**COMPANY, "APP/pom.xml": _pom("xxx-app", "pom", ("./EAR", "./BATCH"), extra=prof)})
    report = make_orch(cfg, ws, build=FakeBuildRunner(ws, fn=files_build)).run(stop_after=Phase.INTAKE)
    assert "reactor-duplicate-removed" not in report.flags
    assert not any(s.startswith("intake: reactor") for s in _subjects(ws))              # no commit
    assert any(f.startswith("DEV is also listed by profile 'full' of APP/pom.xml (not active by default)")
               for f in report.manual_followups)
    ev = [e for e in journal_events(ws) if e["event"] == "reactor_dedupe"]
    assert ev and ev[0]["removed"] == []


def test_a_single_top_below_the_root_gets_an_aggregator_listing_it(cfg, tmp_path):
    files = {k: v for k, v in COMPANY.items() if k != "pom.xml"}           # the company tree: no root pom at all
    ws = _ws(tmp_path, files)
    report = make_orch(cfg, ws, build=FakeBuildRunner(ws, fn=files_build)).run(stop_after=Phase.INTAKE)
    assert "aggregator-generated" in report.flags and "reactor-duplicate-removed" not in report.flags
    assert _modules(ws, "pom.xml") == ["APP"]
    assert _modules(ws, "APP/pom.xml") == ["../DEV", "./EAR", "./BATCH"]   # the as-is aggregator is untouched
    assert "intake: generated root aggregator pom (APP)" in ws._git("log", "-1", "--format=%s").stdout


@pytest.mark.parametrize("engine", ["inline", "graph"])
def test_a_rootless_tree_that_is_not_msa_stops_at_analyze(cfg, tmp_path, engine):
    cfg = cfg.model_copy(deep=True)
    cfg.loop.engine = engine
    ws = _ws(tmp_path, {"a/pom.xml": _pom("a", "war"), "b/pom.xml": _pom("b", "war")})     # unrelated tops
    build = FakeBuildRunner(ws, fn=files_build)
    report = make_orch(cfg, ws, build=build, profile=make_profile(CaseType.SERVLET_TOMCAT)).run()
    assert "no-root-pom" in report.flags and "aborted" in report.flags and run_exit_code(report) == 3
    assert build.calls == [] and report.final_build is None
    assert any("no pom.xml at the project root and none was generated" in f and "a/pom.xml, b/pom.xml" in f
               for f in report.manual_followups)
    assert [e for e in journal_events(ws) if e["event"] == "no_root_pom"]


def test_a_rootless_msa_tree_goes_on(cfg, tmp_path):
    ws = _ws(tmp_path, {"a/pom.xml": _pom("a", "war"), "b/pom.xml": _pom("b", "war")})
    report = make_orch(cfg, ws, build=FakeBuildRunner(ws, fn=files_build),
                       profile=make_profile(CaseType.MSA)).run(stop_after=Phase.ANALYZE)
    assert "no-root-pom" not in report.flags and "aborted" not in report.flags
