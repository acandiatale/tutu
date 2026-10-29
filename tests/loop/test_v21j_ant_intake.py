"""v2.1j orchestrator side (docs/INTERFACES.md "v2.1j"): an Ant project (build.xml, no pom.xml) gets a synthesized
pom.xml at intake — committed `intake: pom.xml synthesized from build.xml` after the as-is snapshot, journal
`pom_synthesized` + `build_tool` (`ant→maven`), flags `pom-synthesized` / `jar-coordinates-unknown`,
`report.pom_synthesis`, one Korean INFO line — and the run continues as a Maven project. `intake.ant_to_maven: false`,
`ok=False`, an exception or a missing analyzer module keep the old rejection (`unsupported-build-tool:ant`, exit 3).

The analyzer (`sbmig.analyze.ant.synthesize_pom`) is faked: a stub module is injected into `sys.modules` (the
orchestrator imports it with `importlib.import_module` on every call), so these tests do not depend on the real one."""
from __future__ import annotations

import logging
import sys
import types
from pathlib import Path

import pytest

from sbmig.cli import run_exit_code
from sbmig.core.models import JarCoordinate, Phase, PomSynthesis
from sbmig.core.workspace import Workspace
from sbmig.report.result_line import report_result_line

from .fakes import FakeBuildRunner, journal_events, make_profile, ok_build
from .test_orchestrator import make_orch

UNKNOWN = "WebContent/WEB-INF/lib/acme-util-1.2.jar"
KNOWN = "lib/commons-lang-2.6.jar"
POM = ("<project xmlns=\"http://maven.apache.org/POM/4.0.0\"><modelVersion>4.0.0</modelVersion>"
       "<groupId>com.company.migrated</groupId><artifactId>legacy-web</artifactId><version>1.0.0-SNAPSHOT</version>"
       "<packaging>war</packaging></project>\n")
INFO_LINE = "접수: Ant build.xml → pom.xml 합성 (jar 2개: pom.properties 1 · 이름표 0 · 조회 0 · 미상 1)"


def _ant_project(tmp_path: Path) -> Path:
    src = tmp_path / "src" / "legacy-web"
    files = {
        "build.xml": "<project name=\"legacy-web\" default=\"war\"><target name=\"war\"/></project>\n",
        "src/com/x/A.java": "package com.x;\nclass A {}\n",
        "WebContent/WEB-INF/web.xml": "<web-app/>\n",
    }
    for rel, text in files.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    for rel in (UNKNOWN, KNOWN):
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"PK\x03\x04 not a real jar")
    return src


def _ws(tmp_path: Path) -> Workspace:
    w = Workspace.from_input(_ant_project(tmp_path), tmp_path / "out", run_id="t-ant")
    w.git_init()
    return w


def synthesis(**kw) -> PomSynthesis:
    base: dict = {"ok": True, "pom_path": "pom.xml", "build_file": "build.xml", "group_id": "com.company.migrated",
                  "artifact_id": "legacy-web", "version": "1.0.0-SNAPSHOT", "packaging": "war", "source_dirs": ["src"],
                  "web_root": "WebContent", "lib_dirs": ["lib", "WebContent/WEB-INF/lib"],
                  "jars": [JarCoordinate(path=KNOWN, group_id="commons-lang", artifact_id="commons-lang",
                                         version="2.6", source="pom_properties", used=True),
                           JarCoordinate(path=UNKNOWN, source="unknown", scope="system", used=True)],
                  "unknown_jars": [UNKNOWN]}
    base.update(kw)
    return PomSynthesis(**base)


class FakeAnt:
    """Stands in for `sbmig.analyze.ant`: `synthesize_pom` writes pom.xml (only when ok) and returns `result`."""

    def __init__(self, result: PomSynthesis | Exception):
        self.result = result
        self.calls: list[tuple] = []

    def synthesize_pom(self, ws, cfg, *, lookup=None):
        self.calls.append((ws, cfg, lookup))
        if isinstance(self.result, Exception):
            raise self.result
        if self.result.ok:
            ws.write("pom.xml", POM)
        return self.result


@pytest.fixture()
def fake_ant(monkeypatch):
    def install(result: PomSynthesis | Exception = None) -> FakeAnt:  # type: ignore[assignment]
        fake = FakeAnt(synthesis() if result is None else result)
        mod = types.ModuleType("sbmig.analyze.ant")
        mod.synthesize_pom = fake.synthesize_pom  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "sbmig.analyze.ant", mod)
        return fake
    return install


def _boom_analyzer(ws, cfg):
    raise AssertionError("analyzer must not run for a rejected project")


def _orch(cfg, ws, **kw):
    return make_orch(cfg, ws, build=FakeBuildRunner(ws, fn=lambda w, c: ok_build(c.get("goal") or "compile")), **kw)


def _subjects(ws: Workspace) -> list[str]:
    return ws._git("log", "--reverse", "--format=%s").stdout.splitlines()


@pytest.mark.parametrize("engine", ["inline", "graph"])
def test_ant_project_gets_a_synthesized_pom_at_intake(cfg, tmp_path, fake_ant, caplog, engine):
    cfg = cfg.model_copy(deep=True)
    cfg.loop.engine = engine
    fake = fake_ant()
    ws = _ws(tmp_path)
    with caplog.at_level(logging.INFO, logger="sbmig"):
        report = _orch(cfg, ws, analyzer=_boom_analyzer).run(stop_after=Phase.INTAKE)
    assert len(fake.calls) == 1 and fake.calls[0][0] is ws and fake.calls[0][1] is cfg
    assert "aborted" not in report.flags and run_exit_code(report) != 3
    assert "pom-synthesized" in report.flags and "jar-coordinates-unknown" in report.flags
    assert not any(f.startswith("unsupported-build-tool") for f in report.flags)
    assert report.pom_synthesis is not None and report.pom_synthesis.unknown_jars == [UNKNOWN]
    # the as-is snapshot first, then the pom commit — changes.patch shows pom.xml as an addition
    subjects = _subjects(ws)
    assert subjects[0] == "sbmig: as-is snapshot"
    assert "intake: pom.xml synthesized from build.xml" in subjects
    sha = ws._git("log", "-1", "--format=%H", "--grep", "pom.xml synthesized").stdout.strip()
    assert ws._git("show", "--name-status", "--format=", sha).stdout.split() == ["A", "pom.xml"]
    assert "+<project" in ws.diff_bytes(ws.first_commit()).decode() and "new file mode" in \
        ws.diff_bytes(ws.first_commit()).decode()
    ev = [e for e in journal_events(ws) if e.get("event") == "pom_synthesized"]
    assert len(ev) == 1
    assert {k: ev[0][k] for k in ("pom", "build_file", "packaging", "jars", "unknown", "dropped")} == {
        "pom": "pom.xml", "build_file": "build.xml", "packaging": "war", "jars": 2, "unknown": 1, "dropped": 0}
    tools = [e for e in journal_events(ws) if e.get("event") == "build_tool"]
    assert [(e["tool"], e["ok"]) for e in tools] == [("ant→maven", True)]
    assert [r.getMessage() for r in caplog.records if r.getMessage().startswith("접수: Ant")] == [INFO_LINE]
    assert "stopped-after-intake" in report.flags


def test_the_run_continues_as_a_maven_project(cfg, tmp_path, fake_ant):
    """The analyze fallback (`profile.build_tool`) passes because the synthesized pom exists; the report shows it."""
    from sbmig.analyze import detect_build_tool

    fake_ant()
    ws = _ws(tmp_path)
    seen: list[str] = []

    def analyzer(w, c):
        det = detect_build_tool(w)
        seen.append(det.tool)
        return make_profile(build_tool=det.tool)

    report = _orch(cfg, ws, analyzer=analyzer).run()
    assert seen == ["maven"] and report.success and run_exit_code(report) == 0
    assert report.final_build is not None and report.final_build.ok
    line = report_result_line(report, run_exit_code(report))
    assert "attn=" in line and "jar-coordinates-unknown" in line.split("attn=")[1].split()[0]
    md = (ws.run_dir / "report.md").read_text(encoding="utf-8")
    assert "### Ant project: pom.xml synthesized" in md
    assert (f"mvn install:install-file -Dfile={UNKNOWN} -DgroupId=com.company.migrated -DartifactId=acme-util "
            "-Dversion=1.2 -Dpackaging=jar") in md
    assert "pom.xml synthesized from build.xml (2 jars: 1 pom.properties, 0 table, 0 lookup, 1 unknown)" in md
    patch = Path(report.outputs["diff"]).read_text(encoding="utf-8")
    assert "--- /dev/null\n+++ b/pom.xml" in patch


def test_no_unknown_jar_no_attention_flag(cfg, tmp_path, fake_ant):
    fake_ant(synthesis(jars=[JarCoordinate(path=KNOWN, group_id="commons-lang", artifact_id="commons-lang",
                                           version="2.6", source="name_table")], unknown_jars=[]))
    ws = _ws(tmp_path)
    report = _orch(cfg, ws).run(stop_after=Phase.INTAKE)
    assert "pom-synthesized" in report.flags and "jar-coordinates-unknown" not in report.flags


@pytest.mark.parametrize("engine", ["inline", "graph"])
def test_ant_to_maven_off_keeps_the_rejection(cfg, tmp_path, fake_ant, engine):
    cfg = cfg.model_copy(deep=True)
    cfg.loop.engine = engine
    cfg.intake.ant_to_maven = False
    fake = fake_ant()
    ws = _ws(tmp_path)
    report = _orch(cfg, ws, analyzer=_boom_analyzer).run()
    assert fake.calls == []
    assert "unsupported-build-tool:ant" in report.flags and "aborted" in report.flags
    assert run_exit_code(report) == 3 and report.pom_synthesis is None
    assert report.manual_followups[0].startswith("run aborted: Ant build.xml detected and no pom.xml")
    assert "only Maven projects are supported" in report.manual_followups[0]
    assert not ws.exists("pom.xml") and "pom-synthesized" not in report.flags


@pytest.mark.parametrize("engine", ["inline", "graph"])
def test_failed_synthesis_aborts_with_its_error(cfg, tmp_path, fake_ant, engine):
    cfg = cfg.model_copy(deep=True)
    cfg.loop.engine = engine
    fake = fake_ant(PomSynthesis(ok=False, error="no source root found next to build.xml"))
    ws = _ws(tmp_path)
    report = _orch(cfg, ws, analyzer=_boom_analyzer).run()
    assert len(fake.calls) == 1
    assert "unsupported-build-tool:ant" in report.flags and "aborted" in report.flags
    assert run_exit_code(report) == 3 and report.pom_synthesis is None
    assert "pom-synthesized" not in report.flags and "jar-coordinates-unknown" not in report.flags
    assert "no source root found next to build.xml" in report.manual_followups[0]
    assert "intake: pom.xml synthesized from build.xml" not in _subjects(ws)
    ev = [e for e in journal_events(ws) if e.get("event") == "build_tool"]
    assert ev and ev[0]["tool"] == "ant" and ev[0]["ok"] is False
    assert "no source root found" in ev[0]["reason"]
    assert not any(e.get("event") == "pom_synthesized" for e in journal_events(ws))


def test_synthesis_exception_is_a_rejection(cfg, tmp_path, fake_ant):
    fake_ant(RuntimeError("boom"))
    ws = _ws(tmp_path)
    report = _orch(cfg, ws, analyzer=_boom_analyzer).run()
    assert "unsupported-build-tool:ant" in report.flags and run_exit_code(report) == 3
    assert "pom.xml could not be synthesized from build.xml: RuntimeError: boom" in report.manual_followups[0]
    assert (ws.run_dir / "report.md").exists()


def test_missing_analyzer_module_is_a_rejection(cfg, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "sbmig.analyze.ant", None)       # import raises ModuleNotFoundError
    ws = _ws(tmp_path)
    report = _orch(cfg, ws, analyzer=_boom_analyzer).run()
    assert "unsupported-build-tool:ant" in report.flags and run_exit_code(report) == 3
    assert "pom synthesis (sbmig.analyze.ant) is not available" in report.manual_followups[0]
