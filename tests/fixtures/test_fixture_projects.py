"""Fixture corpus sanity: metadata schema (incl. the asserted `expect` block), poms parse, encodings, and
(integration) each fixture builds as-is with its documented JDK; fixtures/libs/install.sh works."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Literal

import pytest
import yaml
from lxml import etree
from pydantic import BaseModel, ConfigDict, Field, field_validator

from sbmig.core.config import load_config
from sbmig.core.models import BlockReason, CaseType

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "fixtures" / "projects"
LIBS = REPO / "fixtures" / "libs"
NS = {"m": "http://maven.apache.org/POM/4.0.0"}
JVM = "/Users/acandiatale/Library/Java/JavaVirtualMachines"

JDK_HOMES = {
    "8": os.environ.get("JAVA8_HOME", f"{JVM}/corretto-1.8.0_362/Contents/Home"),
    "11": os.environ.get("JAVA11_HOME", f"{JVM}/azul-11.0.18/Contents/Home"),
    "18": os.environ.get("JAVA18_HOME", f"{JVM}/corretto-18.0.2/Contents/Home"),
    "21": os.environ.get("JAVA21_HOME", f"{JVM}/corretto-21.0.1/Contents/Home"),
}

HAPPY = ["pure-java", "servlet-tomcat", "spring5-xml", "boot2", "boot3", "msa"]
SCENARIOS = ["boot2-javax-lib", "boot2-javax-lib-optional", "euckr-legacy", "enforcer-java8-werror",
             "missing-artifact", "msa-shared-lib", "boot2-cloud-legacy"]
NAMES = HAPPY + SCENARIOS
CP949_FIXTURES = {"euckr-legacy"}

# (fixture, sub-directory to build, JDK, needs) — mirrors the README of each fixture.
# needs: "libs" = legacy-web-lib installed (tail repo), "ghost" = ghost-lib tail repo, "ms949" = platform encoding
BUILD_MATRIX = [
    ("pure-java", ".", "8", ""),
    ("servlet-tomcat", ".", "8", ""),
    ("spring5-xml", ".", "8", ""),
    ("boot2", ".", "11", ""),
    ("boot3", ".", "21", ""),
    ("msa", "order-service", "11", ""),
    ("msa", "user-service", "21", ""),
    ("boot2-javax-lib", ".", "11", "libs"),
    ("boot2-javax-lib-optional", ".", "11", "libs"),
    ("euckr-legacy", ".", "8", "ms949"),
    ("enforcer-java8-werror", ".", "8", ""),
    ("missing-artifact", ".", "8", "ghost"),
    ("msa-shared-lib", ".", "18", ""),
    ("boot2-cloud-legacy", ".", "11", ""),
]
REQUIRED_KEYS = {
    "case_type": str, "java_version": (str, type(None)), "spring_boot_version": (str, type(None)),
    "spring_version": (str, type(None)), "packaging": str, "libraries": list,
    "optional_artifacts_in_use": list, "main_classes": list,
}
README_BUILD = re.compile(r"mvn -q -DskipTests( -D\S+)* package")


# ------------------------------------------------------------------------------------- expected.json schema
class BlockedExpect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: BlockReason
    artifact: str | None = Field(default=None, pattern=r"^[\w.\-]+:[\w.\-]+$")
    owner_class: str | None = None
    phase: str | None = None


class FileCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    encoding: str = "utf-8"
    contains: str | None = None
    not_contains: str | None = None

    @field_validator("encoding")
    @classmethod
    def _known(cls, v: str) -> str:
        "".encode(v)   # LookupError for unknown codecs
        return v


class PomParentCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    group_id: str | None = None
    artifact_id: str | None = None
    version: str | None = None


class PomDepsCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    artifacts: list[str]


class PostCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_decodes_as: FileCheck | None = None
    file_contains: FileCheck | None = None
    pom_parent: PomParentCheck | None = None
    pom_dependency_absent: PomDepsCheck | None = None
    pom_dependency_present: PomDepsCheck | None = None


class AdvisoryExpect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9.\-]*$")
    library: str | None = None
    status: str | None = None
    policy: Literal["guide", "recipe", "off"] | None = None
    recipe_source: Literal["plan", "advisory", "none"] | None = None
    outcome: Literal["replaced", "still-present", "unknown"] | None = None
    replacement: str | None = None


class HandoverExpect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    changes_min: int | None = Field(default=None, ge=0)
    carried_over_min: int | None = Field(default=None, ge=0)
    carried_over_max: int | None = Field(default=None, ge=0)
    parent_before: str | None = None
    parent_after: str | None = None
    java_before: str | None = None
    java_after: str | None = None
    spring_boot_before: str | None = None
    spring_boot_after: str | None = None
    from_here_contains: list[str] = Field(default_factory=list)
    removed_contains: list[str] = Field(default_factory=list)
    added_contains: list[str] = Field(default_factory=list)

    @field_validator("from_here_contains")
    @classmethod
    def _patterns(cls, v: list[str]) -> list[str]:
        for p in v:
            if p.startswith("regex:"):
                re.compile(p[len("regex:"):])
        return v

    @field_validator("removed_contains", "added_contains")
    @classmethod
    def _gavs(cls, v: list[str]) -> list[str]:
        for ga in v:
            assert re.fullmatch(r"[\w.\-]+:[\w.\-]+(:[\w.\-]+)?", ga), ga
        return v


class Expect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    success: bool
    exit_code: Literal[0, 2, 3]
    max_llm_calls: int | None = Field(default=None, ge=0)
    max_duration_s: float | None = Field(default=None, gt=0)
    blocked: list[BlockedExpect] | None = None
    flags: list[str] = Field(default_factory=list)
    forbidden_flags: list[str] = Field(default_factory=list)
    optional_bumped: list[str] = Field(default_factory=list)
    unresolved_max: int | None = Field(default=None, ge=0)
    journal_events: list[dict[str, str | int | bool | None]] = Field(default_factory=list)
    phases: dict[str, Literal["ok", "FAIL", "skipped"]] = Field(default_factory=dict)
    post_checks: list[PostCheck] = Field(default_factory=list)
    advisories: list[AdvisoryExpect] = Field(default_factory=list)
    advisories_exact: bool = False
    advisories_absent: list[str] = Field(default_factory=list)
    handover: HandoverExpect | None = None
    followups_contains: list[str] = Field(default_factory=list)

    @field_validator("flags", "forbidden_flags", "optional_bumped", "followups_contains")
    @classmethod
    def _patterns(cls, v: list[str]) -> list[str]:
        for p in v:
            if p.startswith("regex:"):
                re.compile(p[len("regex:"):])
        return v


class Expected(BaseModel):
    model_config = ConfigDict(extra="allow")       # services, libraries_modules, source_encoding … are informational
    case_type: str
    java_version: str | None
    spring_boot_version: str | None
    spring_version: str | None
    packaging: Literal["jar", "war", "pom"]
    libraries: list[str]
    optional_artifacts_in_use: list[str]
    main_classes: list[str]
    catalog: list[str] = Field(default_factory=list)
    requires_libs: list[str] = Field(default_factory=list)
    requires_absent: list[str] = Field(default_factory=list)
    expect: Expect


# ------------------------------------------------------------------------------------- helpers
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


def _effective_cfg(name: str):
    extra = FIXTURES / name / "sbmig.e2e.yml"
    paths = [REPO / "config" / "artifacts.local-e2e.yml"] + ([extra] if extra.exists() else [])
    return load_config(*paths, repo_root=REPO)


# ------------------------------------------------------------------------------------- unit checks
def test_all_fixtures_present():
    assert sorted(p.name for p in FIXTURES.iterdir() if p.is_dir()) == sorted(NAMES)
    assert (FIXTURES / "README.md").exists()
    readme = (FIXTURES / "README.md").read_text(encoding="utf-8")
    for name in NAMES:
        assert f"`{name}`" in readme, f"{name} missing from fixtures/projects/README.md"


@pytest.mark.parametrize("name", NAMES)
def test_expected_json_schema(name: str):
    exp = _expected(name)
    model = Expected.model_validate(exp)
    for key, typ in REQUIRED_KEYS.items():
        assert isinstance(exp[key], typ), f"{name}: {key} has wrong type"
    assert exp["case_type"] in {c.value for c in CaseType} - {"unknown"}
    assert all(isinstance(x, str) and x for x in exp["libraries"])
    for ga in exp["optional_artifacts_in_use"]:
        assert ga.count(":") == 1, ga
    declared = set().union(*(_declared_deps(p) for p in _poms(name)))
    assert set(exp["optional_artifacts_in_use"]) <= declared, f"{name}: optional artifact not declared in a pom"
    for fqcn in exp["main_classes"]:
        rel = fqcn.replace(".", "/") + ".java"
        assert list((FIXTURES / name).rglob(rel)), f"{name}: main class {fqcn} not found"
    e = model.expect
    assert e.success == (e.exit_code == 0), f"{name}: success and exit_code disagree"
    if e.blocked:
        assert not e.success, f"{name}: blocked items but success expected"
    for b in e.blocked or []:
        if b.artifact:
            assert b.artifact in declared, f"{name}: blocked artifact {b.artifact} is not declared in a pom"
    for gav in model.requires_libs + model.requires_absent:
        assert gav.count(":") == 2, gav


@pytest.mark.parametrize("name", NAMES)
def test_poms_parse_and_readme(name: str):
    poms = _poms(name)
    assert poms
    for pom in poms:
        root = etree.parse(str(pom)).getroot()
        assert root.tag == "{http://maven.apache.org/POM/4.0.0}project"
        assert root.findtext("m:artifactId", namespaces=NS)
    readme = (FIXTURES / name / "README.md").read_text(encoding="utf-8")
    assert README_BUILD.search(readme) and "JAVA_HOME=" in readme, f"{name}: README lacks the as-is build command"
    java_files = list((FIXTURES / name).rglob("*.java"))
    limit = 10 if name in SCENARIOS else 12       # scenario fixtures: ≤ 10 Java files
    assert 0 < len(java_files) <= limit, f"{name}: {len(java_files)} java files"
    assert not list((FIXTURES / name).rglob("target")), f"{name}: stray target/ directory"


@pytest.mark.parametrize("name", NAMES)
def test_post_check_paths_exist_in_fixture(name: str):
    """Every post_check names a file that exists in the as-is fixture (layout-independent `**` globs) — except
    `module/<comp-jar|boot-jar>/pom.xml`, which the restructure phase creates (single jar → comp-jar + boot-jar)."""
    for check in Expected.model_validate(_expected(name)).expect.post_checks:
        for kind, spec in check.model_dump(exclude_none=True).items():
            if re.fullmatch(r"module/(comp|boot)-jar/pom\.xml", spec["path"]):
                assert (FIXTURES / name / "pom.xml").exists()
                continue
            hits = list((FIXTURES / name).glob(spec["path"]))
            assert hits, f"{name}: {kind} path {spec['path']} matches nothing in the as-is fixture"
            if kind == "file_decodes_as" and spec.get("contains"):
                for h in hits:                   # the as-is file already satisfies the check
                    assert spec["contains"] in h.read_bytes().decode(spec["encoding"])


@pytest.mark.parametrize("name", sorted(CP949_FIXTURES))
def test_cp949_sources_are_not_utf8(name: str):
    files = list((FIXTURES / name).rglob("*.java"))
    assert files
    korean = re.compile(r"[가-힣]")
    for f in files:
        raw = f.read_bytes()
        with pytest.raises(UnicodeDecodeError):
            raw.decode("utf-8")
        text = raw.decode("cp949")
        assert korean.search(text), f"{f.name}: no Korean text"
    pom = (FIXTURES / name / "pom.xml").read_text(encoding="utf-8")
    assert "<project.build.sourceEncoding>" not in pom and "<encoding>" not in pom
    assert "안녕하세요" in (FIXTURES / name / "src/main/java/com/example/greeting/Greeting.java").read_bytes() \
        .decode("cp949")


@pytest.mark.parametrize("name", [n for n in NAMES if n not in CP949_FIXTURES])
def test_other_sources_are_utf8(name: str):
    for f in (FIXTURES / name).rglob("*.java"):
        f.read_bytes().decode("utf-8")


def test_base_config_has_exactly_8_optionals():
    cfg = load_config(REPO / "config" / "artifacts.local-e2e.yml", repo_root=REPO)
    assert len(cfg.artifacts.optional) == 8
    assert "com.company.legacy:legacy-web-lib" not in {s.key for s in cfg.artifacts.optional}


def test_fixture_config_override_keeps_8_optionals():
    overrides = sorted(p.parent.name for p in FIXTURES.glob("*/sbmig.e2e.yml"))
    assert overrides == ["boot2-javax-lib-optional"]
    raw = yaml.safe_load((FIXTURES / "boot2-javax-lib-optional" / "sbmig.e2e.yml").read_text(encoding="utf-8"))
    assert set(raw) == {"artifacts"} and set(raw["artifacts"]) == {"optional"}
    cfg = _effective_cfg("boot2-javax-lib-optional")
    keys = {s.key: s.version for s in cfg.artifacts.optional}
    assert len(cfg.artifacts.optional) == 8
    assert keys["com.company.legacy:legacy-web-lib"] == "2.0.0"
    assert "org.slf4j:slf4j-api" not in keys


def test_optional_artifacts_cover_config():
    used: dict[str, set[str]] = {}
    for name in NAMES:
        optional = {s.key for s in _effective_cfg(name).artifacts.optional}
        for ga in _expected(name)["optional_artifacts_in_use"]:
            assert ga in optional, f"{name}: {ga} is not an optional artifact in its effective e2e config"
            used.setdefault(ga, set()).add(name)
    fixtures_with_optional = {n for names in used.values() for n in names}
    assert len(fixtures_with_optional) >= 3
    for ga in ("org.apache.commons:commons-lang3", "com.google.guava:guava", "org.projectlombok:lombok",
               "com.fasterxml.jackson.core:jackson-databind", "commons-io:commons-io",
               "com.company.legacy:legacy-web-lib"):
        assert ga in used, f"{ga} not exercised by any fixture"


def test_javax_lib_variants_share_sources():
    a, b = FIXTURES / "boot2-javax-lib", FIXTURES / "boot2-javax-lib-optional"
    files_a = sorted(p.relative_to(a).as_posix() for p in a.rglob("*") if p.is_file())
    files_b = sorted(p.relative_to(b).as_posix() for p in b.rglob("*") if p.is_file())
    assert set(files_b) - set(files_a) == {"sbmig.e2e.yml"}
    for rel in files_a:
        if rel.endswith((".java", ".xml", ".yml")) and rel != "sbmig.e2e.yml":
            assert (a / rel).read_bytes() == (b / rel).read_bytes(), rel
    src = (a / "src/main/java/com/example/members/web/MemberController.java").read_text(encoding="utf-8")
    assert "new LegacyRequestHelper(http)" in src and "LegacyRequestHelper.clientIp(http)" in src
    assert "import javax.servlet.http.HttpServletRequest;" in src


def test_required_libs_exist_in_fixtures_libs():
    versions = {}
    for pom in LIBS.glob("legacy-web-lib/v*/pom.xml"):
        root = etree.parse(str(pom)).getroot()
        g, a, v = (root.findtext(f"m:{t}", namespaces=NS) for t in ("groupId", "artifactId", "version"))
        assert pom.parent.name == f"v{v}"
        versions[f"{g}:{a}:{v}"] = pom
    assert set(versions) == {"com.company.legacy:legacy-web-lib:1.0.0", "com.company.legacy:legacy-web-lib:2.0.0"}
    for name in NAMES:
        for gav in _expected(name).get("requires_libs", []):
            assert gav in versions, f"{name}: {gav} has no source under fixtures/libs"
    v1 = (LIBS / "legacy-web-lib/v1.0.0/src/main/java/com/company/legacy/web/LegacyRequestHelper.java").read_text()
    v2 = (LIBS / "legacy-web-lib/v2.0.0/src/main/java/com/company/legacy/web/LegacyRequestHelper.java").read_text()
    assert "import javax.servlet.http.HttpServletRequest;" in v1
    assert "import jakarta.servlet.http.HttpServletRequest;" in v2
    for src in (v1, v2):
        assert "public LegacyRequestHelper(HttpServletRequest request)" in src
        assert "public static String clientIp(HttpServletRequest req)" in src
    assert os.access(LIBS / "install.sh", os.X_OK) and os.access(LIBS / "ghost-lib/make-repo.sh", os.X_OK)
    assert "ghost-lib" not in (LIBS / "install.sh").read_text()   # must never be installed


def test_build_matrix_matches_fixtures():
    assert {n for n, _, _, _ in BUILD_MATRIX} == set(NAMES)


# ------------------------------------------------------------------------------------- integration
def _mvn() -> str:
    mvn = shutil.which("mvn") or ("/opt/homebrew/bin/mvn" if Path("/opt/homebrew/bin/mvn").exists() else None)
    if not mvn:
        pytest.skip("mvn not on PATH")
    return mvn


def _java_home(jdk: str) -> str:
    jh = JDK_HOMES[jdk]
    if not Path(jh, "bin", "java").exists():
        pytest.skip(f"JDK {jdk} not found at {jh} (set JAVA{jdk}_HOME)")
    return jh


@pytest.fixture(scope="session")
def libs_repo(tmp_path_factory) -> Path:
    """fixtures/libs installed into a throwaway local repository (used as a tail repo or full local repo)."""
    _mvn()
    _java_home("8")
    _java_home("21")
    repo = tmp_path_factory.mktemp("libs-repo")
    cp = subprocess.run(["bash", str(LIBS / "install.sh"), str(repo)], check=False, capture_output=True, text=True, timeout=900)
    assert cp.returncode == 0, cp.stdout[-3000:] + cp.stderr[-3000:]
    return repo


@pytest.fixture(scope="session")
def ghost_repo(tmp_path_factory) -> Path:
    _java_home("8")
    repo = tmp_path_factory.mktemp("ghost-repo")
    cp = subprocess.run(["bash", str(LIBS / "ghost-lib" / "make-repo.sh"), str(repo)], check=False, capture_output=True,
                        text=True, timeout=300)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    return repo


def _build(src: Path, jdk: str, extra_args: list[str] = (), extra_env: dict | None = None,
           goal: str = "package") -> subprocess.CompletedProcess:
    env = {**os.environ, "JAVA_HOME": _java_home(jdk), **(extra_env or {})}
    return subprocess.run([_mvn(), "-q", "-B", "-DskipTests", *extra_args, goal], check=False, cwd=src, env=env,
                          capture_output=True, text=True, timeout=900)


def _ghost_cleanup():
    """A failed resolution leaves *.lastUpdated markers in ~/.m2 — remove them (never a real jar)."""
    d = Path.home() / ".m2/repository/com/company/nonexistent/ghost-lib/1.2.3"
    if d.exists() and not list(d.glob("*.jar")):
        shutil.rmtree(d.parents[1], ignore_errors=True)


@pytest.mark.integration
@pytest.mark.parametrize("name,subdir,jdk,needs", BUILD_MATRIX,
                         ids=[f"{n}-{d}-jdk{j}".replace("-.-", "-") for n, d, j, _ in BUILD_MATRIX])
def test_fixture_builds_as_is(name: str, subdir: str, jdk: str, needs: str, tmp_path: Path, request):
    _mvn()
    _java_home(jdk)
    src = tmp_path / name
    shutil.copytree(FIXTURES / name, src, ignore=shutil.ignore_patterns("target"))
    args: list[str] = []
    env: dict = {}
    if needs == "libs":
        args.append(f"-Dmaven.repo.local.tail={request.getfixturevalue('libs_repo')}")
    elif needs == "ghost":
        args.append(f"-Dmaven.repo.local.tail={request.getfixturevalue('ghost_repo')}")
    elif needs == "ms949":
        env["JAVA_TOOL_OPTIONS"] = "-Dfile.encoding=MS949"
    t0 = time.monotonic()
    cp = _build(src / subdir, jdk, args, env)
    print(f"\nAS-IS {name}/{subdir} JDK {jdk}: {'PASS' if cp.returncode == 0 else 'FAIL'} "
          f"{time.monotonic() - t0:.1f}s")
    assert cp.returncode == 0, f"{name}/{subdir} failed on JDK {jdk}:\n{cp.stdout[-4000:]}\n{cp.stderr[-2000:]}"
    targets = [src / subdir / "target"] + [p for p in (src / subdir).glob("*/target")]
    built = [j for t in targets for j in list(t.glob("*.jar")) + list(t.glob("*.war"))]
    assert built, "no artifact produced"
    if needs == "ghost":
        assert not list((Path.home() / ".m2/repository/com/company/nonexistent").rglob("*.jar"))


@pytest.mark.integration
def test_install_libs_into_temp_repo_and_boot2_javax_lib_resolves(libs_repo: Path, tmp_path: Path):
    base = libs_repo / "com/company/legacy/legacy-web-lib"
    for v in ("1.0.0", "2.0.0"):
        assert (base / v / f"legacy-web-lib-{v}.jar").exists() and (base / v / f"legacy-web-lib-{v}.pom").exists()
    import zipfile
    with zipfile.ZipFile(base / "1.0.0/legacy-web-lib-1.0.0.jar") as z:
        major = int.from_bytes(z.read("com/company/legacy/web/LegacyRequestHelper.class")[6:8], "big")
    assert major == 52                                    # Java 8
    with zipfile.ZipFile(base / "2.0.0/legacy-web-lib-2.0.0.jar") as z:
        major = int.from_bytes(z.read("com/company/legacy/web/LegacyRequestHelper.class")[6:8], "big")
    assert major == 61                                    # Java 17
    # idempotent: a second install into the same repo succeeds
    cp = subprocess.run(["bash", str(LIBS / "install.sh"), str(libs_repo)], check=False, capture_output=True, text=True,
                        timeout=900)
    assert cp.returncode == 0, cp.stderr
    # boot2-javax-lib resolves legacy-web-lib from that repo *as its only local repository*
    src = tmp_path / "boot2-javax-lib"
    shutil.copytree(FIXTURES / "boot2-javax-lib", src)
    cp = _build(src, "11", [f"-Dmaven.repo.local={libs_repo}"])
    assert cp.returncode == 0, cp.stdout[-4000:] + cp.stderr[-2000:]
    assert list((src / "target").glob("member-api-*.jar"))


@pytest.mark.integration
def test_missing_artifact_unresolvable_without_ghost_repo(tmp_path: Path):
    src = tmp_path / "missing-artifact"
    shutil.copytree(FIXTURES / "missing-artifact", src)
    try:
        cp = _build(src, "8")
    finally:
        _ghost_cleanup()
    assert cp.returncode != 0
    out = cp.stdout + cp.stderr
    assert "com.company.nonexistent:ghost-lib:jar:1.2.3" in out
    assert "Could not find artifact" in out or "was not found in" in out


@pytest.mark.integration
def test_euckr_fails_with_utf8_platform_encoding(tmp_path: Path):
    src = tmp_path / "euckr-legacy"
    shutil.copytree(FIXTURES / "euckr-legacy", src)
    cp = _build(src, "8", extra_env={"JAVA_TOOL_OPTIONS": "-Dfile.encoding=UTF-8"})
    assert cp.returncode != 0 and "unmappable character" in cp.stdout + cp.stderr
