"""`build.mvn` is resolved with shutil.which (Windows: mvn.cmd), MAVEN_HOME/bin is searched, paths pass through."""
from __future__ import annotations

import os
from pathlib import Path

from sbmig.build import mvn as mvnmod
from sbmig.core.config import load_config


def _cfg(**build):
    return load_config(overrides={"artifacts": {"parent": {"group_id": "g", "artifact_id": "a"}}, "build": build})


def test_bare_name_is_resolved_through_path(tmp_path, monkeypatch):
    exe = tmp_path / "bin" / ("mvn.cmd" if os.name == "nt" else "mvn")
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\necho Apache Maven 3.9.9\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(exe.parent))
    cfg = _cfg(mvn="mvn")
    assert Path(mvnmod.mvn_executable(cfg)) == exe
    assert mvnmod.base_command(cfg)[0] == str(exe)


def test_maven_home_bin_is_searched_before_path(tmp_path, monkeypatch):
    exe = tmp_path / "maven" / "bin" / ("mvn.cmd" if os.name == "nt" else "mvn")
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    monkeypatch.setenv("MAVEN_HOME", str(tmp_path / "maven"))
    assert Path(mvnmod.mvn_executable(_cfg(mvn="mvn"))) == exe


def test_explicit_path_and_unresolvable_name_pass_through(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("MAVEN_HOME", raising=False)
    monkeypatch.delenv("M2_HOME", raising=False)
    assert mvnmod.mvn_executable(_cfg(mvn="C:/apache-maven/bin/mvn.cmd")).endswith("mvn.cmd")
    assert mvnmod.mvn_executable(_cfg(mvn="mvn-not-installed")) == "mvn-not-installed"
    ok, msg = mvnmod.maven_available(_cfg(mvn="mvn-not-installed"))
    assert not ok and "mvn-not-installed" in msg


def test_settings_catch_all_mirror_and_repository_notes(tmp_path, cfg):
    from sbmig.build.mvn import repository_notes, settings_catch_all_mirror
    from sbmig.core.models import PomInfo
    poms = [PomInfo(path="pom.xml", artifact_id="a", repositories=["http://old-nexus.corp/repo"]),
            PomInfo(path="svc/pom.xml", artifact_id="svc", repositories=["http://old-nexus.corp/repo", "https://repo.spring.io/milestone"])]
    none = cfg.model_copy(deep=True)
    none.build.settings_file = None
    assert settings_catch_all_mirror(none) is None
    notes = repository_notes(poms, none)
    assert len(notes) == 1 and notes[0].startswith("followup:") and "2 repository URL(s)" in notes[0] \
        and "old-nexus.corp" in notes[0] and "no build.settings_file" in notes[0]
    assert repository_notes([PomInfo(path="pom.xml", artifact_id="a")], none) == []
    plain = tmp_path / "plain.xml"
    plain.write_text("<settings><mirrors><mirror><id>x</id><mirrorOf>central</mirrorOf><url>u</url></mirror></mirrors>"
                     "<!-- <mirrorOf>*</mirrorOf> --></settings>", encoding="utf-8")
    partial = cfg.model_copy(deep=True)
    partial.build.settings_file = str(plain)
    assert settings_catch_all_mirror(partial) is False
    assert repository_notes(poms, partial)[0].startswith("followup:") and "no catch-all" in repository_notes(poms, partial)[0]
    mirror = tmp_path / "nexus.xml"
    mirror.write_text("<settings><mirrors><mirror><id>n</id><mirrorOf>external:*</mirrorOf><url>u</url></mirror></mirrors></settings>",
                      encoding="utf-8")
    good = cfg.model_copy(deep=True)
    good.build.settings_file = str(mirror)
    assert settings_catch_all_mirror(good) is True
    (n,) = repository_notes(poms, good)
    assert not n.startswith("followup:") and "routed through the settings mirror" in n
    strip = good.model_copy(deep=True)
    strip.build.pom_repositories = "remove"
    (n,) = repository_notes(poms, strip)
    assert "strips them" in n and "make sure" not in n
    strip_no_mirror = none.model_copy(deep=True)
    strip_no_mirror.build.pom_repositories = "remove"
    assert "make sure build.settings_file mirrors" in repository_notes(poms, strip_no_mirror)[0]


def test_relative_settings_local_repo_and_java_home_become_absolute(tmp_path, cfg, monkeypatch):
    """Maven runs inside the project directory, so a settings file given relative to the sbmig working directory
    (the RUNBOOK example `config/settings.nexus.xml`) must reach Maven as an absolute path."""
    from sbmig.build.mvn import base_command, mvn_env
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.nexus.xml").write_text("<settings/>", encoding="utf-8")
    c = cfg.model_copy(deep=True)
    c.build.settings_file = "config/settings.nexus.xml"
    c.build.local_repo = "m2"
    c.build.java_home = "jdk21"
    cmd = base_command(c)
    assert cmd[cmd.index("-s") + 1] == str((tmp_path / "config" / "settings.nexus.xml").resolve())
    assert f"-Dmaven.repo.local={(tmp_path / 'm2').resolve()}" in cmd
    assert mvn_env(c)["JAVA_HOME"] == str((tmp_path / "jdk21").resolve())
