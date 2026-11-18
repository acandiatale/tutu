"""The server's Maven settings always win: `base_command` passes `-gs <maven home>/conf/settings.xml` next to `-s`, so a
`-gs` in the project's `.mvn/maven.config` cannot replace Maven's global settings; the project's own settings options
are read (never applied) for the run's notes."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from sbmig.build import mvn as mvnmod
from sbmig.core.config import load_config


@pytest.fixture(autouse=True)
def _fresh_cache():
    mvnmod._MAVEN_HOMES.clear()
    mvnmod._MAVEN_HOME_LOGGED.clear()
    yield
    mvnmod._MAVEN_HOMES.clear()
    mvnmod._MAVEN_HOME_LOGGED.clear()


def _cfg(**build):
    return load_config(overrides={"artifacts": {"parent": {"group_id": "g", "artifact_id": "a"}}, "build": build})


def _maven_home(root: Path) -> Path:
    """A minimal Maven installation: bin/mvn, bin/m2.conf, conf/settings.xml."""
    (root / "bin").mkdir(parents=True)
    (root / "conf").mkdir()
    exe = root / "bin" / ("mvn.cmd" if os.name == "nt" else "mvn")
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    (root / "bin" / "m2.conf").write_text("main is org.apache.maven.cli.MavenCli from plexus.core\n")
    (root / "conf" / "settings.xml").write_text("<settings/>")
    return exe


def _settings(tmp_path: Path) -> Path:
    p = tmp_path / "server-settings.xml"
    p.write_text("<settings/>", encoding="utf-8")
    return p


def test_defaults():
    c = _cfg()
    assert c.build.ignore_project_settings is True
    assert c.remediation.project_maven_config == "ignore"


def test_gs_follows_s_when_the_maven_home_is_found(tmp_path):
    exe = _maven_home(tmp_path / "maven")
    s = _settings(tmp_path)
    cmd = mvnmod.base_command(_cfg(mvn=str(exe), settings_file=str(s)))
    i = cmd.index("-s")
    assert cmd[i:i + 4] == ["-s", str(s.absolute()), "-gs", str(tmp_path / "maven" / "conf" / "settings.xml")]
    assert cmd.count("-gs") == 1


def test_symlinked_executable_and_libexec_layout(tmp_path):
    # Homebrew: bin/mvn -> ../Cellar/maven/X/bin/mvn (a wrapper), the installation is X/libexec
    cellar = tmp_path / "Cellar" / "maven" / "3.9.10"
    (cellar / "bin").mkdir(parents=True)
    wrapper = cellar / "bin" / "mvn"
    wrapper.write_text("#!/bin/sh\n")
    wrapper.chmod(0o755)
    _maven_home(cellar / "libexec")
    (tmp_path / "bin").mkdir()
    link = tmp_path / "bin" / "mvn"
    link.symlink_to(wrapper)
    home = mvnmod.maven_home(_cfg(mvn=str(link)))
    assert home == (cellar / "libexec").resolve()


def test_no_gs_when_the_home_cannot_be_found(tmp_path):
    lone = tmp_path / "tools" / "mvn"
    lone.parent.mkdir()
    lone.write_text("#!/bin/sh\n")
    s = _settings(tmp_path)
    cmd = mvnmod.base_command(_cfg(mvn=str(lone), settings_file=str(s)))
    assert "-s" in cmd and "-gs" not in cmd
    # a bare name nobody can resolve: no guessed path either
    assert "-gs" not in mvnmod.base_command(_cfg(mvn="mvn-not-installed-anywhere", settings_file=str(s)))


def test_no_settings_file_and_switch_off_keep_the_old_command(tmp_path):
    exe = _maven_home(tmp_path / "maven")
    plain = mvnmod.base_command(_cfg(mvn=str(exe)))
    assert "-s" not in plain and "-gs" not in plain
    s = _settings(tmp_path)
    off = mvnmod.base_command(_cfg(mvn=str(exe), settings_file=str(s), ignore_project_settings=False))
    assert off == [str(exe), "-B", "-ntp", "-Dstyle.color=never", "-s", str(s.absolute())]


def test_maven_home_from_the_preflight_version_output(tmp_path, monkeypatch):
    home = tmp_path / "elsewhere" / "apache-maven"
    (home / "conf").mkdir(parents=True)
    (home / "conf" / "settings.xml").write_text("<settings/>")
    exe = tmp_path / "bin" / "mvn"                     # nothing beside the executable: only `mvn -v` knows the home
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\n")
    out = ("Apache Maven 3.9.9 (abc)\n"
           f"Maven home: {home}\n"
           "Java version: 21.0.2, vendor: X, runtime: /jdk\n")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, out, ""))
    c = _cfg(mvn=str(exe), settings_file=str(_settings(tmp_path)))
    assert mvnmod.maven_available(c)[0]
    cmd = mvnmod.base_command(c)
    assert cmd[cmd.index("-gs") + 1] == str(home / "conf" / "settings.xml")


def test_the_home_is_looked_up_once_per_process(tmp_path, monkeypatch):
    exe = _maven_home(tmp_path / "maven")
    calls = []
    real = mvnmod._home_beside

    def counting(p):
        calls.append(p)
        return real(p)

    monkeypatch.setattr(mvnmod, "_home_beside", counting)
    c = _cfg(mvn=str(exe), settings_file=str(_settings(tmp_path)))
    for _ in range(3):
        assert "-gs" in mvnmod.base_command(c)
    assert len(calls) == 1


# ---------------------------------------------------------------------------- the project's .mvn/maven.config
def _project(tmp_path: Path, config: str | None) -> Path:
    root = tmp_path / "proj"
    (root / ".mvn").mkdir(parents=True)
    (root / "pom.xml").write_text("<project/>")
    if config is not None:
        (root / ".mvn" / "maven.config").write_text(config, encoding="utf-8")
    return root


@pytest.mark.parametrize("text, settings, global_settings, offline", [
    ("-s .mvn/settings.nexus.xml\n", ["-s .mvn/settings.nexus.xml"], [], []),
    ("--settings .mvn/a.xml", ["--settings .mvn/a.xml"], [], []),
    ("--settings=.mvn/a.xml -B", ["--settings=.mvn/a.xml"], [], []),
    ("-s=.mvn/a.xml", ["-s=.mvn/a.xml"], [], []),
    ("-s.mvn/a.xml", ["-s.mvn/a.xml"], [], []),
    ("--sett .mvn/a.xml", ["--sett .mvn/a.xml"], [], []),
    ("-gs .mvn/global.nexus.xml", [], ["-gs .mvn/global.nexus.xml"], []),
    ("--global-settings=.mvn/g.xml", [], ["--global-settings=.mvn/g.xml"], []),
    ("--global-settings .mvn/g.xml", [], ["--global-settings .mvn/g.xml"], []),
    ("-gs.mvn/g.xml", [], ["-gs.mvn/g.xml"], []),
    ("-T 4 -o\n-s x.xml\n-Drevision=1.0", ["-s x.xml"], [], ["-o"]),
    ("--offline -gs g.xml", [], ["-gs g.xml"], ["--offline"]),
    ("-B -ntp -Drevision=1.0 -T 1C", [], [], []),
    # Maven 3.9 reads one argument per line and skips lines starting with '#'
    ("-gs\n.mvn/global.nexus.xml\n", [], ["-gs .mvn/global.nexus.xml"], []),
    ("# -s .mvn/old.xml\n-B\n", [], [], []),
])
def test_settings_options_of_the_project(tmp_path, text, settings, global_settings, offline):
    found = mvnmod.project_maven_settings(_project(tmp_path, text))
    assert found is not None
    assert (found.settings, found.global_settings, found.offline) == (settings, global_settings, offline)
    assert found.names_settings == bool(settings or global_settings)


def test_no_maven_config(tmp_path):
    assert mvnmod.project_maven_settings(_project(tmp_path, None)) is None


IGNORED = ("the project's .mvn/maven.config points Maven at its own settings (-s .mvn/settings.nexus.xml): ignored here "
           "— every build of this run used the server's settings")
OFFLINE = (". The same file also turns on offline mode (-o), which the command line cannot switch off: Maven downloads "
           "nothing in this run and builds only from what the local repository already holds")


def test_note_for_settings_ignored(tmp_path):
    exe = _maven_home(tmp_path / "maven")
    c = _cfg(mvn=str(exe), settings_file=str(_settings(tmp_path)))
    found = mvnmod.project_maven_settings(_project(tmp_path, "-s .mvn/settings.nexus.xml"))
    assert mvnmod.project_settings_note(c, found) == ("project-maven-settings-ignored", IGNORED)


def test_note_names_offline_in_a_second_sentence(tmp_path):
    exe = _maven_home(tmp_path / "maven")
    c = _cfg(mvn=str(exe), settings_file=str(_settings(tmp_path)))
    found = mvnmod.project_maven_settings(_project(tmp_path, "-o -s .mvn/settings.nexus.xml"))
    assert mvnmod.project_settings_note(c, found) == ("project-maven-settings-ignored", IGNORED + OFFLINE)
    c.build.offline = True                               # the server runs offline anyway: nothing differs
    assert mvnmod.project_settings_note(c, found) == ("project-maven-settings-ignored", IGNORED)


def test_note_for_global_settings(tmp_path):
    exe = _maven_home(tmp_path / "maven")
    s = str(_settings(tmp_path))
    found = mvnmod.project_maven_settings(_project(tmp_path, "-gs .mvn/global.nexus.xml"))
    flag, line = mvnmod.project_settings_note(_cfg(mvn=str(exe), settings_file=s), found)
    assert flag == "project-maven-settings-ignored" and "(-gs .mvn/global.nexus.xml): ignored here" in line
    # the switch off: Maven merges the project's global settings, and the note says so
    flag, line = mvnmod.project_settings_note(_cfg(mvn=str(exe), settings_file=s, ignore_project_settings=False), found)
    assert (flag, line) == ("project-maven-settings-used",
                            "the project's .mvn/maven.config points Maven at its own settings (-gs .mvn/global.nexus.xml): "
                            "Maven read them in this run as its global settings, merged with the server's "
                            "(build.ignore_project_settings is false)")
    lone = tmp_path / "lone" / "mvn"
    lone.parent.mkdir()
    lone.write_text("")
    _flag, line = mvnmod.project_settings_note(_cfg(mvn=str(lone), settings_file=s), found)
    assert line.endswith("merged with the server's (Maven home not found, so no -gs was passed)")


def test_no_note_without_server_settings_or_settings_options(tmp_path):
    exe = _maven_home(tmp_path / "maven")
    found = mvnmod.project_maven_settings(_project(tmp_path, "-s .mvn/settings.nexus.xml"))
    assert mvnmod.project_settings_note(_cfg(mvn=str(exe)), found) is None
    other = mvnmod.project_maven_settings(_project(tmp_path / "b", "-o -T 4"))
    assert mvnmod.project_settings_note(_cfg(mvn=str(exe), settings_file=str(_settings(tmp_path))), other) is None
