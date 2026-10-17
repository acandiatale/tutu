"""Fixture corpus sanity: metadata schema, poms parse, and (integration) each builds as-is with its JDK."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from lxml import etree

from sbmig.core.models import CaseType

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "fixtures" / "projects"
NS = {"m": "http://maven.apache.org/POM/4.0.0"}

JDK_HOMES = {
    "8": os.environ.get("JAVA8_HOME",
                        "/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-1.8.0_362/Contents/Home"),
    "11": os.environ.get("JAVA11_HOME",
                         "/Users/acandiatale/Library/Java/JavaVirtualMachines/azul-11.0.18/Contents/Home"),
    "21": os.environ.get("JAVA21_HOME",
                         "/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-21.0.1/Contents/Home"),
}

# (fixture, sub-directory to build, JDK) — mirrors the README of each fixture
BUILD_MATRIX = [
    ("pure-java", ".", "8"),
    ("servlet-tomcat", ".", "8"),
    ("spring5-xml", ".", "8"),
    ("boot2", ".", "11"),
    ("boot3", ".", "21"),
    ("msa", "order-service", "11"),
    ("msa", "user-service", "21"),
]
NAMES = ["pure-java", "servlet-tomcat", "spring5-xml", "boot2", "boot3", "msa"]
REQUIRED_KEYS = {
    "case_type": str, "java_version": (str, type(None)), "spring_boot_version": (str, type(None)),
    "spring_version": (str, type(None)), "packaging": str, "libraries": list,
    "optional_artifacts_in_use": list, "main_classes": list,
}


def _expected(name: str) -> dict:
    return json.loads((FIXTURES / name / "expected.json").read_text(encoding="utf-8"))


def _poms(name: str) -> list[Path]:
    return sorted(p for p in (FIXTURES / name).rglob("pom.xml") if "target" not in p.parts)


def _declared_deps(pom: Path) -> set[str]:
    root = etree.parse(str(pom)).getroot()
    out = set()
    for dep in root.findall(".//m:dependencies/m:dependency", NS):
        g, a = dep.findtext("m:groupId", namespaces=NS), dep.findtext("m:artifactId", namespaces=NS)
        out.add(f"{g}:{a}")
    return out


def test_all_fixtures_present():
    assert sorted(p.name for p in FIXTURES.iterdir() if p.is_dir()) == sorted(NAMES)
    assert (FIXTURES / "README.md").exists()


@pytest.mark.parametrize("name", NAMES)
def test_expected_json_schema(name: str):
    exp = _expected(name)
    for key, typ in REQUIRED_KEYS.items():
        assert key in exp, f"{name}: missing {key}"
        assert isinstance(exp[key], typ), f"{name}: {key} has wrong type"
    assert exp["case_type"] in {c.value for c in CaseType} - {"unknown"}
    assert exp["packaging"] in {"jar", "war", "pom"}
    assert all(isinstance(x, str) and x for x in exp["libraries"])
    for ga in exp["optional_artifacts_in_use"]:
        assert ga.count(":") == 1, ga
    declared = set().union(*(_declared_deps(p) for p in _poms(name)))
    assert set(exp["optional_artifacts_in_use"]) <= declared, f"{name}: optional artifact not declared in a pom"
    for fqcn in exp["main_classes"]:
        rel = fqcn.replace(".", "/") + ".java"
        assert list((FIXTURES / name).rglob(rel)), f"{name}: main class {fqcn} not found"


@pytest.mark.parametrize("name", NAMES)
def test_poms_parse_and_readme(name: str):
    poms = _poms(name)
    assert poms
    for pom in poms:
        root = etree.parse(str(pom)).getroot()
        assert root.tag == "{http://maven.apache.org/POM/4.0.0}project"
        assert root.findtext("m:artifactId", namespaces=NS)
    readme = (FIXTURES / name / "README.md").read_text(encoding="utf-8")
    assert "mvn -q -DskipTests package" in readme and "JAVA_HOME=" in readme
    java_files = list((FIXTURES / name).rglob("*.java"))
    assert 0 < len(java_files) <= 12
    assert not list((FIXTURES / name).rglob("target")), f"{name}: stray target/ directory"


def test_optional_artifacts_cover_config(cfg):
    optional = {s.key for s in cfg.artifacts.optional}
    used: dict[str, set[str]] = {}
    for name in NAMES:
        for ga in _expected(name)["optional_artifacts_in_use"]:
            assert ga in optional, f"{name}: {ga} is not an optional artifact in artifacts.local-e2e.yml"
            used.setdefault(ga, set()).add(name)
    fixtures_with_optional = {n for names in used.values() for n in names}
    assert len(fixtures_with_optional) >= 3
    for ga in ("org.apache.commons:commons-lang3", "com.google.guava:guava", "org.projectlombok:lombok",
               "com.fasterxml.jackson.core:jackson-databind", "commons-io:commons-io"):
        assert ga in used, f"{ga} not exercised by any fixture"


def test_build_matrix_matches_fixtures():
    assert {n for n, _, _ in BUILD_MATRIX} == set(NAMES)


@pytest.mark.integration
@pytest.mark.parametrize("name,subdir,jdk", BUILD_MATRIX, ids=[f"{n}-{d}-jdk{j}" for n, d, j in BUILD_MATRIX])
def test_fixture_builds_as_is(name: str, subdir: str, jdk: str, tmp_path: Path):
    mvn = shutil.which("mvn")
    if not mvn:
        pytest.skip("mvn not on PATH")
    java_home = JDK_HOMES[jdk]
    if not Path(java_home, "bin", "java").exists():
        pytest.skip(f"JDK {jdk} not found at {java_home} (set JAVA{jdk}_HOME)")
    src = tmp_path / name
    shutil.copytree(FIXTURES / name, src, ignore=shutil.ignore_patterns("target"))
    env = {**os.environ, "JAVA_HOME": java_home}
    cp = subprocess.run([mvn, "-q", "-B", "-DskipTests", "package"], cwd=src / subdir, env=env,
                        capture_output=True, text=True, timeout=600)
    assert cp.returncode == 0, f"{name}/{subdir} failed on JDK {jdk}:\n{cp.stdout[-4000:]}\n{cp.stderr[-2000:]}"
    built = list((src / subdir / "target").glob("*.jar")) + list((src / subdir / "target").glob("*.war"))
    assert built, "no artifact produced"
