"""A project whose `.mvn/maven.config` points Maven at its own settings: the migration notes it once at intake (flag
`project-maven-settings-ignored`, one Handover line, journal `project_maven_settings`), the exit code does not change
and `.mvn/maven.config` is delivered as the owners wrote it."""
from __future__ import annotations

from pathlib import Path

from sbmig.build import mvn as mvnmod
from sbmig.core.workspace import Workspace

from .fakes import FakeBuildRunner, journal_events, ok_build
from .test_orchestrator import make_orch

NS = 'xmlns="http://maven.apache.org/POM/4.0.0"'
POM = (f'<?xml version="1.0" encoding="UTF-8"?>\n<project {NS}>\n  <modelVersion>4.0.0</modelVersion>\n'
       "  <groupId>com.example</groupId>\n  <artifactId>app</artifactId>\n  <version>1.0</version>\n</project>\n")
CONFIG = "-s .mvn/settings.nexus.xml\n-o\n"
LINE = ("the project's .mvn/maven.config points Maven at its own settings (-s .mvn/settings.nexus.xml): ignored here — "
        "every build of this run used the server's settings. The same file also turns on offline mode (-o), which the "
        "command line cannot switch off: Maven downloads nothing in this run and builds only from what the local "
        "repository already holds")


def _ws(tmp_path: Path, files: dict[str, str]) -> Workspace:
    src = tmp_path / "src" / "proj"
    for rel, text in files.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    w = Workspace.from_input(src, tmp_path / "out", run_id="t-run")
    w.git_init()
    return w


def _with_settings(cfg, tmp_path: Path):
    s = tmp_path / "server-settings.xml"
    s.write_text("<settings/>", encoding="utf-8")
    c = cfg.model_copy(deep=True)
    c.build.settings_file = str(s)
    return c


def _run(cfg, ws):
    orch = make_orch(cfg, ws, build=FakeBuildRunner(ws, fn=lambda w, call: ok_build()))
    return orch.run()


def test_noted_once_and_the_file_is_kept(cfg, tmp_path):
    mvnmod._MAVEN_HOMES.clear()
    ws = _ws(tmp_path, {"pom.xml": POM, ".mvn/maven.config": CONFIG, ".mvn/settings.nexus.xml": "<settings/>"})
    report = _run(_with_settings(cfg, tmp_path), ws)
    assert report.flags.count("project-maven-settings-ignored") == 1
    assert report.manual_followups.count(LINE) == 1
    [ev] = [e for e in journal_events(ws) if e["event"] == "project_maven_settings"]
    assert ev["file"] == ".mvn/maven.config" and ev["settings"] == ["-s .mvn/settings.nexus.xml"]
    assert ev["offline"] == ["-o"] and ev["ignored"] is True
    assert report.success
    assert ws.read(".mvn/maven.config") == CONFIG


def test_nothing_without_server_settings_or_without_settings_options(cfg, tmp_path):
    ws = _ws(tmp_path / "a", {"pom.xml": POM, ".mvn/maven.config": CONFIG})
    c = cfg.model_copy(deep=True)
    c.build.settings_file = None
    report = _run(c, ws)
    assert not any(f.startswith("project-maven-settings") for f in report.flags)
    assert not any("maven.config" in f for f in report.manual_followups)
    ws2 = _ws(tmp_path / "b", {"pom.xml": POM, ".mvn/maven.config": "-T 4 -Drevision=1.0\n"})
    report2 = _run(_with_settings(cfg, tmp_path), ws2)
    assert not any(f.startswith("project-maven-settings") for f in report2.flags)
    assert not [e for e in journal_events(ws2) if e["event"] == "project_maven_settings"]
