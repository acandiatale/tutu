"""A project's `.mvn/` folder at intake. With `build.ignore_project_mvn` (default) the migration says once that the folder
was not used (flag `project-mvn-ignored`, one Handover line, journal `project_mvn`), in every flow that builds; with the
switch off a `.mvn/maven.config` pointing Maven at its own settings is noted as before (flag
`project-maven-settings-ignored`, journal `project_maven_settings`). The exit code does not change and the folder is
delivered byte for byte."""
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
    c.build.ignore_project_mvn = False               # the settings notes are the switch-off behaviour
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
    c.build.ignore_project_mvn = False
    report = _run(c, ws)
    assert not any(f.startswith("project-maven-settings") for f in report.flags)
    assert not any("maven.config" in f for f in report.manual_followups)
    ws2 = _ws(tmp_path / "b", {"pom.xml": POM, ".mvn/maven.config": "-T 4 -Drevision=1.0\n"})
    report2 = _run(_with_settings(cfg, tmp_path), ws2)
    assert not any(f.startswith("project-maven-settings") for f in report2.flags)
    assert not [e for e in journal_events(ws2) if e["event"] == "project_maven_settings"]


# ---------------------------------------------------------------------------- the whole folder (switch on)
MVN_FILES = {"pom.xml": POM, ".mvn/maven.config": "-s .mvn/settings.nexus.xml\n-Drevision=1.2.3\n-Pci\n",
             ".mvn/jvm.config": "-XX:MaxPermSize=256m\n", ".mvn/settings.nexus.xml": "<settings/>",
             ".mvn/wrapper/maven-wrapper.properties": "distributionUrl=https://repo.example.invalid/x.zip\n"}
MVN_LINE = ("the project's .mvn/ folder was not used for the builds of this run (maven.config: -s .mvn/settings.nexus.xml, "
            "-Drevision=1.2.3, -Pci; jvm.config: -XX:MaxPermSize=256m; Maven wrapper; other files: settings.nexus.xml); "
            "carried over to the command line: -Drevision=1.2.3 -Pci; the folder is delivered unchanged — check it "
            "against JDK 21 and the Maven version in use before building with it")


def _switch_on(cfg, tmp_path):
    c = _with_settings(cfg, tmp_path)
    c.build.ignore_project_mvn = True
    return c


def test_the_folder_is_noted_once_instead_of_the_settings_line(cfg, tmp_path):
    import zipfile
    ws = _ws(tmp_path, MVN_FILES)
    report = _run(_switch_on(cfg, tmp_path), ws)
    assert report.flags.count("project-mvn-ignored") == 1
    assert not any(f.startswith("project-maven-settings") for f in report.flags)
    assert report.manual_followups.count(MVN_LINE) == 1
    assert not any("points Maven at its own settings" in f for f in report.manual_followups)
    [ev] = [e for e in journal_events(ws) if e["event"] == "project_mvn"]
    assert not [e for e in journal_events(ws) if e["event"] == "project_maven_settings"]
    [folder] = ev["folders"]
    assert folder == {"dir": ".mvn", "maven_config": ["-s .mvn/settings.nexus.xml", "-Drevision=1.2.3", "-Pci"],
                      "jvm_config": ["-XX:MaxPermSize=256m"], "extensions": None, "wrapper": True,
                      "other_files": ["settings.nexus.xml"], "carried": ["-Drevision=1.2.3", "-Pci"]}
    assert ev["carried"] == ["-Drevision=1.2.3", "-Pci"] and ev["basedir"] == str(mvnmod.maven_basedir())
    assert report.success
    for rel, text in MVN_FILES.items():
        assert ws.read(rel) == text
    with zipfile.ZipFile(ws.run_dir / "result.zip") as zf:
        for rel, text in MVN_FILES.items():
            assert zf.read(f"{ws.root.name}/{rel}") == text.encode("utf-8")


def test_nothing_without_a_mvn_folder_or_with_an_empty_one(cfg, tmp_path):
    ws = _ws(tmp_path, {"pom.xml": POM})
    (ws.root / ".mvn").mkdir()
    report = _run(_switch_on(cfg, tmp_path), ws)
    assert "project-mvn-ignored" not in report.flags
    assert not [e for e in journal_events(ws) if e["event"] == "project_mvn"]
