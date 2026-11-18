"""Real Maven: the command line `base_command` builds wins over a project's `.mvn/maven.config` (`-s` and `-gs`).

A pom-packaging project, `mvn -o -X -q validate` (no network, no plugin); the `Reading global settings from …` /
`Reading user settings from …` debug lines say which files Maven read. The last case is the control: without our `-gs`
Maven reads the project's file as its global settings (the hole this closes)."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from sbmig.build import mvn as mvnmod
from sbmig.core.config import load_config

pytestmark = pytest.mark.integration

POM = ('<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>'
       "<groupId>com.example</groupId><artifactId>settings-probe</artifactId><version>1.0</version>"
       "<packaging>pom</packaging></project>\n")
PROJECT_SETTINGS = ("<settings><mirrors><mirror><id>project-mirror</id><mirrorOf>*</mirrorOf>"
                    "<url>https://repo.example.invalid/maven-central</url></mirror></mirrors></settings>\n")
READ_RE = re.compile(r"Reading (global|user) settings from (.+?)\s*$", re.MULTILINE)


def _read(cfg, project: Path) -> dict[str, Path]:
    cmd = mvnmod.base_command(cfg) + ["-o", "-X", "-q", "validate"]
    cp = subprocess.run(cmd, cwd=str(project), env=mvnmod.mvn_env(cfg), capture_output=True, timeout=180,
                        stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace")
    out = cp.stdout + cp.stderr
    assert cp.returncode == 0, out[-2000:]
    found = {kind: Path(p).resolve() for kind, p in READ_RE.findall(out)}
    print(f"\n$ {' '.join(cmd[1:])}  (.mvn/maven.config: "
          f"{' '.join((project / '.mvn' / 'maven.config').read_text().split())})")
    for line in READ_RE.finditer(out):
        print("  " + line.group(0).strip())
    return found


def test_the_command_line_settings_beat_the_project_maven_config(tmp_path):
    if shutil.which("mvn") is None:
        pytest.skip("Maven not installed")
    mvnmod._MAVEN_HOMES.clear()
    ours = tmp_path / "ours.xml"
    ours.write_text("<settings/>\n", encoding="utf-8")
    cfg = load_config(overrides={"artifacts": {"parent": {"group_id": "g", "artifact_id": "a"}},
                                 "build": {"mvn": "mvn", "settings_file": str(ours), "local_repo": str(tmp_path / "m2")}})
    ok, detail = mvnmod.maven_available(cfg)                 # the preflight: remembers `Maven home:`
    assert ok, detail
    gs = mvnmod.maven_global_settings(cfg)
    assert gs is not None, "Maven home not found"
    project = tmp_path / "proj"
    (project / ".mvn").mkdir(parents=True)
    (project / "pom.xml").write_text(POM, encoding="utf-8")
    (project / ".mvn" / "settings.nexus.xml").write_text(PROJECT_SETTINGS, encoding="utf-8")
    (project / ".mvn" / "global.nexus.xml").write_text(PROJECT_SETTINGS, encoding="utf-8")
    config = project / ".mvn" / "maven.config"

    config.write_text("-s\n.mvn/settings.nexus.xml\n", encoding="utf-8")    # Maven 3.9: one argument per line
    got = _read(cfg, project)
    assert got == {"global": gs.resolve(), "user": ours.resolve()}

    config.write_text("-gs\n.mvn/global.nexus.xml\n", encoding="utf-8")
    got = _read(cfg, project)
    assert got == {"global": gs.resolve(), "user": ours.resolve()}

    # control: today's command line (no -gs) lets the project's file in as the global settings
    off = cfg.model_copy(deep=True)
    off.build.ignore_project_settings = False
    got = _read(off, project)
    assert got == {"global": (project / ".mvn" / "global.nexus.xml").resolve(), "user": ours.resolve()}
