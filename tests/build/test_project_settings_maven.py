"""Real Maven: the command line `base_command` builds wins over a project's `.mvn/maven.config` (`-s` and `-gs`).

A pom-packaging project, `mvn -o -X -q validate` (no network, no plugin); the `Reading global settings from …` /
`Reading user settings from …` debug lines say which files Maven read. The last case is the control: without our `-gs`
Maven reads the project's file as its global settings (the hole this closes)."""
from __future__ import annotations

import os
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
    cmd = mvnmod.with_project_mvn(cfg, mvnmod.base_command(cfg) + ["-o", "-X", "-q", "validate"], project, top=project)
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
    off.build.ignore_project_mvn = False                    # Maven must read the project's .mvn/ for this control
    got = _read(off, project)
    assert got == {"global": (project / ".mvn" / "global.nexus.xml").resolve(), "user": ours.resolve()}



# ---------------------------------------------------------------------------- the whole .mvn/ folder
def _jdk21_cfg(tmp_path: Path, **build):
    opts = {"mvn": "mvn", "local_repo": str(tmp_path / "m2")}
    if os.environ.get("JAVA21_HOME"):
        opts["java_home"] = os.environ["JAVA21_HOME"]
    opts.update(build)
    return load_config(overrides={"artifacts": {"parent": {"group_id": "g", "artifact_id": "a"}}, "build": opts})


def _mvn(cfg, project: Path, goals: list[str]) -> tuple[list[str], subprocess.CompletedProcess]:
    cmd = mvnmod.with_project_mvn(cfg, mvnmod.base_command(cfg) + goals, project, top=project)
    cp = subprocess.run(cmd, cwd=str(project), env=mvnmod.mvn_env(cfg), capture_output=True, timeout=180,
                        stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace")
    print(f"\n$ MAVEN_BASEDIR={mvnmod.mvn_env(cfg).get('MAVEN_BASEDIR', '(unset)')} {' '.join(cmd[1:])}  -> exit "
          f"{cp.returncode}\n  " + "\n  ".join((cp.stdout + cp.stderr).strip().splitlines()[-3:]))
    return cmd, cp


def test_a_jdk8_jvm_config_and_a_38_style_maven_config_do_not_stop_the_build(tmp_path):
    if shutil.which("mvn") is None:
        pytest.skip("Maven not installed")
    project = tmp_path / "proj"
    (project / ".mvn").mkdir(parents=True)
    (project / "pom.xml").write_text(POM, encoding="utf-8")
    (project / ".mvn" / "jvm.config").write_text("-XX:MaxPermSize=256m\n", encoding="utf-8")
    (project / ".mvn" / "maven.config").write_text("-gs .mvn/x.xml\n-Drevision=1.2.3\n", encoding="utf-8")
    (project / ".mvn" / "x.xml").write_text(PROJECT_SETTINGS, encoding="utf-8")
    before = {p.name: p.read_bytes() for p in (project / ".mvn").iterdir()}

    _cmd, cp = _mvn(_jdk21_cfg(tmp_path, ignore_project_mvn=False), project, ["-o", "-q", "validate"])
    assert cp.returncode != 0
    assert "MaxPermSize" in cp.stdout + cp.stderr                # the JVM does not start

    cmd, cp = _mvn(_jdk21_cfg(tmp_path), project, ["-o", "-q", "validate"])
    assert cp.returncode == 0, (cp.stdout + cp.stderr)[-2000:]
    assert "-Drevision=1.2.3" in cmd and f"-Dmaven.multiModuleProjectDirectory={project.absolute()}" in cmd
    assert {p.name: p.read_bytes() for p in (project / ".mvn").iterdir()} == before


HELP = "org.apache.maven.plugins:maven-help-plugin:3.5.1"
PROBE_POM = ('<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>'
             "<groupId>com.example</groupId><artifactId>mmpd-probe</artifactId><version>1.0</version>"
             "<packaging>pom</packaging><properties><probe.dir>${maven.multiModuleProjectDirectory}</probe.dir>"
             "<probe.seen>${probe.fromconfig}</probe.seen></properties></project>\n")


def test_the_pom_sees_the_project_root_and_maven_reads_no_mvn_file(tmp_path):
    """`${maven.multiModuleProjectDirectory}` in a pom is the project root again, the carried `-D` reaches the pom, and
    neither `maven.config` (a 3.8-style `-gs <path>` line Maven 3.9 cannot parse), `extensions.xml` (an extension
    nobody can resolve offline) nor `jvm.config` (a JDK 8 option) is read by Maven. Needs the help plugin in the
    user's local repository (offline)."""
    if shutil.which("mvn") is None:
        pytest.skip("Maven not installed")
    repo = Path.home() / ".m2" / "repository"
    if not (repo / "org/apache/maven/plugins/maven-help-plugin/3.5.1").is_dir():
        pytest.skip("maven-help-plugin 3.5.1 not in ~/.m2/repository")
    project = tmp_path / "proj"
    (project / ".mvn").mkdir(parents=True)
    (project / "pom.xml").write_text(PROBE_POM, encoding="utf-8")
    (project / ".mvn" / "maven.config").write_text("-gs .mvn/x.xml\n-Dprobe.fromconfig=carried\n", encoding="utf-8")
    (project / ".mvn" / "jvm.config").write_text("-XX:MaxPermSize=256m\n", encoding="utf-8")
    (project / ".mvn" / "extensions.xml").write_text(
        "<extensions><extension><groupId>com.example.invalid</groupId><artifactId>no-such-ext</artifactId>"
        "<version>9.9.9</version></extension></extensions>\n", encoding="utf-8")
    out = tmp_path / "effective.xml"
    cfg = _jdk21_cfg(tmp_path, local_repo=str(repo))
    _cmd, cp = _mvn(cfg, project, ["-o", "-q", f"{HELP}:effective-pom", f"-Doutput={out}"])
    assert cp.returncode == 0, (cp.stdout + cp.stderr)[-2000:]
    text = out.read_text(encoding="utf-8")
    assert f"<probe.dir>{project.absolute()}</probe.dir>" in text
    assert "<probe.seen>carried</probe.seen>" in text
