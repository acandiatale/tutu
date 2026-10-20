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
