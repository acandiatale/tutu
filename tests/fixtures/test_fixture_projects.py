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
             "missing-artifact", "msa-shared-lib", "boot2-cloud-legacy", "boot2-method-rename", "boot2-hystrix-command",
             "multi-module-merge", "legacy-external-config", "java6-legacy", "multi-module-conflict", "legacy-ibatis",
             "legacy-role-parents", "company-boot-single", "company-spring5-web", "company-servlet-jsp",
             "boot2-rewrite-excluded", "boot2-qlrm-javax", "servlet-webxml-legacy", "servlet-nonstandard-dirs",
             "boot2-fix-failures", "ear-sibling-poms", "servlet-war-overlay"]
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
    ("multi-module-merge", ".", "11", ""),
    ("legacy-role-parents", ".", "11", ""),
    ("boot2-javax-lib", ".", "11", "libs"),
    ("boot2-javax-lib-optional", ".", "11", "libs"),
    ("euckr-legacy", ".", "8", "ms949"),
    ("enforcer-java8-werror", ".", "8", ""),
    ("missing-artifact", ".", "8", "ghost"),
    ("msa-shared-lib", ".", "18", ""),
    ("boot2-cloud-legacy", ".", "11", ""),
    ("boot2-method-rename", ".", "11", "libs"),
    ("boot2-fix-failures", ".", "11", "libs"),
    ("ear-sibling-poms", "APP/BATCH", "11", ""),           # no root pom: each module builds on its own
    ("servlet-war-overlay", ".", "11", "libs"),
    ("boot2-hystrix-command", ".", "11", ""),
    ("legacy-external-config", ".", "8", ""),
    ("java6-legacy", ".", "8", ""),
    ("multi-module-conflict", ".", "11", ""),
    ("legacy-ibatis", ".", "8", ""),
    ("company-boot-single", ".", "11", ""),
    ("company-spring5-web", ".", "11", ""),
    ("company-servlet-jsp", ".", "8", ""),
    ("boot2-rewrite-excluded", ".", "11", ""),
    ("boot2-qlrm-javax", ".", "11", ""),
    ("servlet-webxml-legacy", ".", "8", ""),
    ("servlet-nonstandard-dirs", ".", "8", ""),
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


class PomPackagingCheck(BaseModel):
    """`pom_packaging`: the pom's `<packaging>` (absent = jar)."""
    model_config = ConfigDict(extra="forbid")
    path: str
    packaging: Literal["jar", "war", "pom"]


class PomModulesCheck(BaseModel):
    """`pom_module_list`: the exact top-level `<modules>` list (in order unless `any_order`)."""
    model_config = ConfigDict(extra="forbid")
    path: str
    modules: list[str]
    any_order: bool = False


class PathCheck(BaseModel):
    """`file_exists` / `file_absent`: a path (glob allowed) in the *migrated* tree."""
    model_config = ConfigDict(extra="forbid")
    path: str


class FileSameCheck(BaseModel):
    """`file_same_as` (2026-10-23): every file matching `path` in the migrated tree is byte-equal to the as-is
    fixture file `source` (binaries survive the restructure and resource filtering)."""
    model_config = ConfigDict(extra="forbid")
    path: str
    source: str


class PostCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_decodes_as: FileCheck | None = None
    file_contains: FileCheck | None = None
    file_exists: PathCheck | None = None
    file_absent: PathCheck | None = None
    pom_parent: PomParentCheck | None = None
    pom_dependency_absent: PomDepsCheck | None = None
    pom_dependency_present: PomDepsCheck | None = None
    pom_dependency_unversioned: PomDepsCheck | None = None    # declared without <version> (parent-managed)
    pom_dependency_versioned: PomDepsCheck | None = None      # declared with a <version>
    pom_packaging: PomPackagingCheck | None = None
    pom_module_list: PomModulesCheck | None = None
    file_same_as: FileSameCheck | None = None


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


class KBRecipeExpect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipe: str = Field(pattern=r"^[A-Za-z][\w.]*$")
    options: dict[str, str | int | bool] = Field(default_factory=dict)
    name: str | None = None


class KBExpect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dir: str = Field(pattern=r"^out/[\w.\-/]+$")
    candidates_min: int | None = Field(default=None, ge=0)
    harvest_candidates_min: int | None = Field(default=None, ge=0)
    recipes: list[KBRecipeExpect] = Field(default_factory=list)


class PropertiesExpect(BaseModel):
    """Round 8 externalized-config inventory (`scripts/e2e.py evaluate_properties`)."""
    model_config = ConfigDict(extra="forbid")
    external_min: int | None = Field(default=None, ge=0)
    external: list[str] = Field(default_factory=list)
    absent: list[str] = Field(default_factory=list)
    external_in: dict[str, list[str]] = Field(default_factory=dict)   # MSA: service name → its external keys
    written: list[str] = Field(default_factory=list)
    placeholder_file_contains: list[str] = Field(default_factory=list)


class ErrorCaseRecordExpect(BaseModel):
    """One wanted `ErrorCaseRecord` (scripts/e2e.py `evaluate_error_cases`): listed fields must all match one record."""
    model_config = ConfigDict(extra="forbid")
    file: str | None = None
    status: str | None = None
    fix_method: str | None = None
    targeted: bool | None = None
    change_kind: str | None = None
    replace_rule_candidate: bool | None = None
    replace_pairs: list[dict[str, str]] = Field(default_factory=list)
    attempts_min: int | None = Field(default=None, ge=0)
    attempt_outcomes: list[str] = Field(default_factory=list)
    failure_reason: str | None = None
    block_reason: str | None = None

    @field_validator("file", "status", "fix_method", "change_kind", "failure_reason", "block_reason")
    @classmethod
    def _pattern(cls, v: str | None) -> str | None:
        if v and v.startswith("regex:"):
            re.compile(v[len("regex:"):])
        return v


class ErrorCasesExpect(BaseModel):
    """2026-10-23: one record per build error of the run, failures included (`report.error_cases`)."""
    model_config = ConfigDict(extra="forbid")
    min: int | None = Field(default=None, ge=0)
    status: dict[Literal["resolved", "resolved_indirectly", "blocked", "unresolved", "not_visible"], int] = \
        Field(default_factory=dict)
    status_min: dict[Literal["resolved", "resolved_indirectly", "blocked", "unresolved", "not_visible"], int] = \
        Field(default_factory=dict)
    records: list[ErrorCaseRecordExpect] = Field(default_factory=list)
    run_files: list[Literal["error_cases.jsonl", "fix_attempts.jsonl", "error_cases.csv"]] = Field(default_factory=list)
    kb_errors_min: int | None = Field(default=None, ge=0)
    kb_attempts_min: int | None = Field(default=None, ge=0)


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
    journal_events: list[dict[str, str | int | bool | None | list[str]]] = Field(default_factory=list)   # list = equal
    phases: dict[str, Literal["ok", "FAIL", "skipped"]] = Field(default_factory=dict)
    post_checks: list[PostCheck] = Field(default_factory=list)
    advisories: list[AdvisoryExpect] = Field(default_factory=list)
    advisories_exact: bool = False
    advisories_absent: list[str] = Field(default_factory=list)
    handover: HandoverExpect | None = None
    followups_contains: list[str] = Field(default_factory=list)
    kb: KBExpect | None = None
    properties: PropertiesExpect | None = None
    error_cases: ErrorCasesExpect | None = None

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
    `module/<comp-jar|boot-jar>/pom.xml`, which the restructure phase creates (single jar → comp-jar + boot-jar), and
    the `application.properties` that receives the externalized-config placeholders (round 8: boot-jar; round 9: an
    MSA service's own `<service>/src/main/resources/application.properties`). Company-layout fixtures (round 9c,
    COMPANY_OVERRIDES): a path inside a role module the restructure creates (`<name>-boot|back|front/…`) must name an
    as-is file by its basename — except the module poms and the generated `*Application.java`."""
    aid = COMPANY_NAMES.get(name, "shop")
    roles = re.compile(rf"(?:{re.escape(aid)})-(?:boot|back|front)/(.+)") if name in COMPANY_OVERRIDES else None
    for check in Expected.model_validate(_expected(name)).expect.post_checks:
        for kind, spec in check.model_dump(exclude_none=True).items():
            if kind in ("file_exists", "file_absent"):      # describe the migrated tree, not the as-is one
                continue
            if kind == "file_same_as":
                assert (FIXTURES / name / spec["source"]).is_file(), f"{name}: file_same_as source {spec['source']}"
            m = roles.fullmatch(spec["path"]) if roles and not (FIXTURES / name / spec["path"]).exists() else None
            if m:
                base = m.group(1).rsplit("/", 1)[-1]
                # generated by the restructure: module poms, the Application, WebXmlRegistrations (2026-10-23) and the
                # boot application.properties that receives web.xml's session-timeout
                if base not in ("pom.xml", "*Application.java", "WebXmlRegistrations.java", "application.properties"):
                    assert list((FIXTURES / name).rglob(base)), f"{name}: {kind} {spec['path']}: no as-is {base}"
                continue
            if spec["path"] == "pom.xml" and "aggregator-generated" in Expected.model_validate(
                    _expected(name)).expect.flags:
                continue                  # 2026-10-23: the root pom is generated at intake (sibling poms, no root)
            if re.fullmatch(r"module/(comp|boot)-jar/pom\.xml", spec["path"]):
                assert (FIXTURES / name / "pom.xml").exists()
                continue
            if re.fullmatch(r"(module/boot-jar|[\w.\-]+)/src/main/resources/application\.properties", spec["path"]) and \
                    (_expected(name).get("expect") or {}).get("properties"):
                continue                  # placeholder target: boot-jar, or an MSA service's own file (round 9)
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


OVERRIDES = {   # fixture → (top-level keys of its sbmig.e2e.yml, legacy-web-lib version in the optional list)
    "boot2-javax-lib-optional": ({"artifacts"}, "2.0.0"),
    "boot2-method-rename": ({"artifacts", "llm", "kb"}, "3.0.0"),
    "boot2-fix-failures": ({"artifacts", "llm", "kb"}, "3.0.0"),
}


LLM_ONLY_OVERRIDES = {"boot2-hystrix-command": {"llm", "kb"}, "java6-legacy": {"llm", "kb"},
                      "multi-module-conflict": {"llm", "kb"}}   # no artifacts override: the base 8 optionals apply
# 2026-10-22: openrewrite.exclusions only — directories the recipes must not see (E13 / E14 source handlers)
REWRITE_ONLY_OVERRIDES = {"boot2-rewrite-excluded": {"**/vendor/**", "**/repository/**"}}
# round 9c: company-layout fixtures — the override sets the company layout keys back (config/artifacts.local-e2e.yml
# pins the pre-9c layout for the older fixtures) and adds the role parents (artifacts.boot_parent / comp_parent)
COMPANY_OVERRIDES = {name: {"layout": {"module_dir", "boot_module", "comp_module", "front_module", "boot_as_is"},
                            "artifacts": {"boot_parent", "comp_parent"}}
                     for name in ("legacy-role-parents", "company-boot-single", "company-spring5-web",
                                  "company-servlet-jsp", "servlet-webxml-legacy", "servlet-nonstandard-dirs",
                                  "ear-sibling-poms", "servlet-war-overlay")}

COMPANY_NAMES = {"servlet-webxml-legacy": "legacy", "servlet-nonstandard-dirs": "ndirs", "ear-sibling-poms": "portal",
                 "servlet-war-overlay": "board"}   # root artifactId (= `{name}`) when it is not `shop`


def test_fixture_config_override_keeps_8_optionals():
    from sbmig.core.config import LayoutConfig

    overrides = sorted(p.parent.name for p in FIXTURES.glob("*/sbmig.e2e.yml"))
    assert overrides == sorted([*OVERRIDES, *LLM_ONLY_OVERRIDES, *COMPANY_OVERRIDES,
                                     *REWRITE_ONLY_OVERRIDES])
    company = LayoutConfig()                                       # the company layout = the code defaults
    for name, keys in COMPANY_OVERRIDES.items():
        raw = yaml.safe_load((FIXTURES / name / "sbmig.e2e.yml").read_text(encoding="utf-8"))
        assert {k: set(v) for k, v in raw.items()} == keys, name
        cfg = _effective_cfg(name)
        for key in keys["layout"]:
            assert getattr(cfg.layout, key) == getattr(company, key), (name, key)
        assert cfg.layout.module_path("boot", "shop") == "shop-boot", name
        assert cfg.artifacts.mandatory_target == "root", name
        assert cfg.artifacts.role_parents and len(cfg.artifacts.optional) == 8, name
        assert cfg.artifacts.boot_parent.key == cfg.artifacts.parent.key          # local stand-in: the same parent
        assert cfg.artifacts.comp_parent.key == cfg.artifacts.parent.key
        named = FIXTURES / name / "pom.xml"
        if not named.exists():                     # ear-sibling-poms: no root pom, `{name}` comes from the EAR
            named = FIXTURES / name / "APP" / "EAR" / "pom.xml"
        assert etree.parse(str(named)).getroot().findtext("m:artifactId", namespaces=NS) == \
            COMPANY_NAMES.get(name, "shop")
    base = load_config(REPO / "config" / "artifacts.local-e2e.yml", repo_root=REPO).layout
    assert (base.module_dir, base.boot_module, base.front_module, base.boot_as_is) == ("module", "boot-jar", None, "layout")
    for name, top in LLM_ONLY_OVERRIDES.items():
        raw = yaml.safe_load((FIXTURES / name / "sbmig.e2e.yml").read_text(encoding="utf-8"))
        assert set(raw) == top, name
        assert len(_effective_cfg(name).artifacts.optional) == 8, name
    from sbmig.core.config import OpenRewriteConfig

    for name, extra in REWRITE_ONLY_OVERRIDES.items():
        raw = yaml.safe_load((FIXTURES / name / "sbmig.e2e.yml").read_text(encoding="utf-8"))
        assert raw == {"openrewrite": {"exclusions": raw["openrewrite"]["exclusions"]}}, name
        cfg = _effective_cfg(name)
        assert len(cfg.artifacts.optional) == 8, name
        excl = cfg.openrewrite.exclusions
        assert set(OpenRewriteConfig().exclusions) <= set(excl), name   # lists replace: the defaults are repeated
        assert extra <= set(excl), name
    for name, (top, legacy_version) in OVERRIDES.items():
        raw = yaml.safe_load((FIXTURES / name / "sbmig.e2e.yml").read_text(encoding="utf-8"))
        assert set(raw) == top and set(raw["artifacts"]) == {"optional"}, name
        cfg = _effective_cfg(name)
        keys = {s.key: s.version for s in cfg.artifacts.optional}
        assert len(cfg.artifacts.optional) == 8, name
        assert keys["com.company.legacy:legacy-web-lib"] == legacy_version, name
        assert "org.slf4j:slf4j-api" not in keys, name
    first, *rest = (yaml.safe_load((FIXTURES / n / "sbmig.e2e.yml").read_text(encoding="utf-8"))["artifacts"]["optional"]
                    for n in OVERRIDES)
    for other in rest:                                                               # same list, other version
        assert [dict(x, version=None) for x in first] == [dict(x, version=None) for x in other]


def test_method_rename_override_replays_llm_into_own_kb():
    """boot2-method-rename: scripted LLM (replay dir in the fixture) and its own KB dir under out/ — the same dir
    its `expect.kb` reads (scripts/e2e.py clears it before the run)."""
    name = "boot2-method-rename"
    cfg = _effective_cfg(name)
    assert cfg.llm.provider == "fake"
    assert cfg.llm.base_url == f"replay:fixtures/projects/{name}/replay"
    replay = cfg.resolve_path(cfg.llm.base_url[len("replay:"):])
    assert replay.is_dir() and replay == FIXTURES / name / "replay"
    kb = Expected.model_validate(_expected(name)).expect.kb
    assert kb is not None and cfg.kb.dir == kb.dir and kb.dir.startswith("out/") and kb.dir != "out/e2e"
    assert cfg.kb_dir.resolve() != (REPO / "kb").resolve()
    assert any(r.recipe == "org.openrewrite.java.ChangeMethodName" for r in kb.recipes)


def test_method_rename_replay_patches_match_the_call_sites():
    """Each replayed response is a valid fix-protocol answer whose SEARCH text is a call-site line of the fixture
    (the OpenRewrite spring phase only rewrites the javax imports, never these lines); together they rename every
    `LegacyRequestHelper.clientIp(` call, one file per response (one LLM call per file cluster)."""
    from sbmig.fix.client import load_replay_responses
    from sbmig.fix.protocol import parse_fix_response

    name = "boot2-method-rename"
    responses = load_replay_responses(FIXTURES / name / "replay")
    assert len(responses) == _expected(name)["expect"]["max_llm_calls"] == 2
    touched = []
    for text in responses:
        parsed = parse_fix_response(text)
        assert parsed.changes and not parsed.warnings and parsed.confidence is not None
        assert len({ch.path for ch in parsed.changes}) == 1
        for ch in parsed.changes:
            src = (FIXTURES / name / ch.path).read_text(encoding="utf-8")
            assert ch.op == "modify" and src.count(ch.search) == 1, ch.path
            assert "LegacyRequestHelper.clientIp(" in ch.search and "javax" not in ch.search
            assert ch.replace == ch.search.replace("LegacyRequestHelper.clientIp(", "LegacyRequestHelper.remoteClientIp(")
            touched.append(ch.path)
    callers = sorted(p.relative_to(FIXTURES / name).as_posix() for p in (FIXTURES / name).rglob("*.java")
                     if "LegacyRequestHelper.clientIp(" in p.read_text(encoding="utf-8"))
    assert sorted(touched) == callers and len(callers) == 2


def test_hystrix_command_override_replays_llm_into_own_kb():
    """boot2-hystrix-command (round 6): scripted LLM + own KB dir under out/ (cleared by scripts/e2e.py)."""
    name = "boot2-hystrix-command"
    cfg = _effective_cfg(name)
    assert cfg.llm.provider == "fake" and cfg.llm.base_url == f"replay:fixtures/projects/{name}/replay"
    assert cfg.resolve_path(cfg.llm.base_url[len("replay:"):]) == FIXTURES / name / "replay"
    kb = Expected.model_validate(_expected(name)).expect.kb
    assert kb is not None and cfg.kb.dir == kb.dir == "out/e2e/kb-hystrix-command"
    assert cfg.kb_dir.resolve() != (REPO / "kb").resolve()


def test_hystrix_command_replay_ports_the_subclass():
    """Response 1 ports the programmatic HystrixCommand subclass to Resilience4j (SEARCH blocks copied from the
    as-is file, which the spring phase does not touch) and keeps `public Integer execute()` for the caller;
    response 2 (the caller's cluster) changes nothing. One LLM call per file cluster (2) + the harvest's
    recipe-candidate call (the repeated replay answer is rejected) → max_llm_calls == 3."""
    from sbmig.fix.client import load_replay_responses
    from sbmig.fix.protocol import parse_fix_response

    name = "boot2-hystrix-command"
    rel = "src/main/java/com/example/stock/service/InventoryCommand.java"
    src = (FIXTURES / name / rel).read_text(encoding="utf-8")
    assert "extends HystrixCommand<Integer>" in src and "import com.netflix.hystrix.HystrixCommand;" in src
    caller = (FIXTURES / name / "src/main/java/com/example/stock/service/StockService.java").read_text(encoding="utf-8")
    assert "new InventoryCommand(gateway, sku).execute()" in caller
    responses = load_replay_responses(FIXTURES / name / "replay")
    assert len(responses) == 2 and _expected(name)["expect"]["max_llm_calls"] == 3   # 2 fix calls + the harvest's recipe-candidate call
    first, second = (parse_fix_response(t) for t in responses)
    assert first.changes and not first.warnings and {c.path for c in first.changes} == {rel}
    out = src
    for ch in first.changes:
        assert ch.op == "modify" and out.count(ch.search) == 1, ch.search
        out = out.replace(ch.search, ch.replace)
    assert "com.netflix.hystrix" not in out and "extends HystrixCommand" not in out
    assert "import io.github.resilience4j.circuitbreaker.CircuitBreaker;" in out
    assert 'CircuitBreaker.ofDefaults("inventory")' in out and "public Integer execute()" in out
    assert "MIGRATION-TODO" in out
    assert second.changes == [] and not second.wants_context


def test_multi_module_conflict_replay_patches_the_kept_copy():
    """multi-module-conflict (round 9): response 1 is one SEARCH/REPLACE on the kept comp-jar Shared.java — the `core`
    copy, moved verbatim by the first-wins merge — that adds the farewell() only the dropped `web` copy had;
    response 2 answers the KB harvest with NONE. Own KB dir, replay dir in the fixture."""
    from sbmig.fix.client import load_replay_responses
    from sbmig.fix.protocol import parse_fix_response

    name = "multi-module-conflict"
    cfg = _effective_cfg(name)
    assert cfg.llm.provider == "fake" and cfg.llm.base_url == f"replay:fixtures/projects/{name}/replay"
    kb = Expected.model_validate(_expected(name)).expect.kb
    assert kb is not None and cfg.kb.dir == kb.dir == "out/e2e/kb-multi-module-conflict"
    rel = "src/main/java/com/example/shop/shared/Shared.java"
    kept = (FIXTURES / name / "core" / rel).read_text(encoding="utf-8")
    dropped = (FIXTURES / name / "web" / rel).read_text(encoding="utf-8")
    assert "farewell(" not in kept and "public static String farewell(String name)" in dropped
    ctrl = (FIXTURES / name / "web/src/main/java/com/example/shop/web/ShopController.java").read_text(encoding="utf-8")
    assert "Shared.farewell(name)" in ctrl
    core_svc = (FIXTURES / name / "core/src/main/resources/META-INF/services/com.example.shop.spi.Plugin").read_text()
    web_svc = (FIXTURES / name / "web/src/main/resources/META-INF/services/com.example.shop.spi.Plugin").read_text()
    assert core_svc.split() == ["com.example.shop.core.CorePlugin"] and web_svc.split() == ["com.example.shop.web.WebPlugin"]
    responses = load_replay_responses(FIXTURES / name / "replay")
    assert len(responses) == _expected(name)["expect"]["max_llm_calls"] == 2
    first = parse_fix_response(responses[0])
    assert first.changes and not first.warnings and {c.path for c in first.changes} == {f"module/comp-jar/{rel}"}
    out = kept
    for ch in first.changes:
        assert ch.op == "modify" and out.count(ch.search) == 1, ch.search
        out = out.replace(ch.search, ch.replace)
    assert "public static String farewell(String name)" in out and "public static String greet(String name)" in out
    assert responses[1].strip() == "NONE"


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


@pytest.mark.parametrize("variant,extra", [
    ("boot2-javax-lib-optional", {"sbmig.e2e.yml"}),
    ("boot2-method-rename", {"sbmig.e2e.yml", "replay/llm-001.json", "replay/llm-002.json"}),
    ("boot2-fix-failures", {"sbmig.e2e.yml", *(f"replay/llm-00{i}.json" for i in range(1, 6))}),
])
def test_javax_lib_variants_share_sources(variant: str, extra: set[str]):
    a, b = FIXTURES / "boot2-javax-lib", FIXTURES / variant
    files_a = sorted(p.relative_to(a).as_posix() for p in a.rglob("*") if p.is_file())
    files_b = sorted(p.relative_to(b).as_posix() for p in b.rglob("*") if p.is_file())
    assert set(files_b) - set(files_a) == extra
    assert set(files_a) <= set(files_b)
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
    assert set(versions) == {f"com.company.legacy:legacy-web-lib:{v}" for v in ("1.0.0", "2.0.0", "3.0.0")}
    for pom in LIBS.glob("legacy-base-web/v*/pom.xml"):        # 2026-10-23: the WAR that servlet-war-overlay overlays
        root = etree.parse(str(pom)).getroot()
        g, a, v = (root.findtext(f"m:{t}", namespaces=NS) for t in ("groupId", "artifactId", "version"))
        assert pom.parent.name == f"v{v}" and root.findtext("m:packaging", namespaces=NS) == "war"
        versions[f"{g}:{a}:{v}"] = pom
    assert "install_lib \"$LIBS/legacy-base-web/v1.0.0\"" in (LIBS / "install.sh").read_text(encoding="utf-8")
    for name in NAMES:
        for gav in _expected(name).get("requires_libs", []):
            assert gav in versions, f"{name}: {gav} has no source under fixtures/libs"
    v1 = (LIBS / "legacy-web-lib/v1.0.0/src/main/java/com/company/legacy/web/LegacyRequestHelper.java").read_text()
    v2 = (LIBS / "legacy-web-lib/v2.0.0/src/main/java/com/company/legacy/web/LegacyRequestHelper.java").read_text()
    v3 = (LIBS / "legacy-web-lib/v3.0.0/src/main/java/com/company/legacy/web/LegacyRequestHelper.java").read_text()
    assert "import javax.servlet.http.HttpServletRequest;" in v1
    assert "import jakarta.servlet.http.HttpServletRequest;" in v2
    assert "import jakarta.servlet.http.HttpServletRequest;" in v3
    for src in (v1, v2, v3):
        assert "public LegacyRequestHelper(HttpServletRequest request)" in src
        assert "public String requestKey()" in src and "public boolean isAjax()" in src
    for src in (v1, v2):
        assert "public static String clientIp(HttpServletRequest req)" in src
    # 3.0.0: clientIp renamed to remoteClientIp (static + instance), no alias left behind
    assert "public static String remoteClientIp(HttpServletRequest req)" in v3 and "public String remoteClientIp()" in v3
    assert not re.search(r"\bString clientIp\(", v3)
    masking = [(LIBS / f"legacy-web-lib/v{v}/src/main/java/com/company/legacy/web/MaskingUtils.java").read_bytes()
               for v in ("2.0.0", "3.0.0")]
    assert masking[0] == masking[1]
    assert "legacy-web-lib/v3.0.0" in (LIBS / "install.sh").read_text()
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
    for v in ("1.0.0", "2.0.0", "3.0.0"):
        assert (base / v / f"legacy-web-lib-{v}.jar").exists() and (base / v / f"legacy-web-lib-{v}.pom").exists()
    import zipfile
    with zipfile.ZipFile(base / "1.0.0/legacy-web-lib-1.0.0.jar") as z:
        major = int.from_bytes(z.read("com/company/legacy/web/LegacyRequestHelper.class")[6:8], "big")
    assert major == 52                                    # Java 8
    for v in ("2.0.0", "3.0.0"):
        with zipfile.ZipFile(base / f"{v}/legacy-web-lib-{v}.jar") as z:
            cls = z.read("com/company/legacy/web/LegacyRequestHelper.class")
        assert int.from_bytes(cls[6:8], "big") == 61      # Java 17
        assert (b"remoteClientIp" in cls) == (v == "3.0.0")
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


def test_legacy_external_config_inventory_matches_expected():
    """Round 8: the analyzer's externalized-config verdicts for the fixture equal its expected.json `externalized`."""
    from sbmig.analyze import analyze_project
    from sbmig.analyze.analyzer import PathView

    exp = _expected("legacy-external-config")["externalized"]
    prof = analyze_project(PathView(FIXTURES / "legacy-external-config"), _effective_cfg("legacy-external-config"))
    by = {f.key: f.status for f in prof.properties.findings}
    assert sorted(k for k, s in by.items() if s == "external") == sorted(exp["external"])
    assert sorted(k for k, s in by.items() if s == "defined") == sorted(exp["defined"])


def test_fix_failures_replay_is_deliberately_imperfect():
    """boot2-fix-failures (2026-10-23): llm-001 / llm-002 (an attempt and its feedback round) have SEARCH blocks that
    are not in ClientIpFilter.java (apply_failed), llm-003 / llm-005 edit MemberController.java but keep the removed
    `clientIp(` call (no_change), llm-004 is the real rename — so the run records a failed attempt before a fix, and a
    cluster that stays broken."""
    from sbmig.fix.client import load_replay_responses
    from sbmig.fix.protocol import parse_fix_response

    name = "boot2-fix-failures"
    cfg = _effective_cfg(name)
    assert cfg.llm.provider == "fake" and cfg.llm.base_url == f"replay:fixtures/projects/{name}/replay"
    exp = Expected.model_validate(_expected(name)).expect
    assert exp.kb is not None and cfg.kb.dir == exp.kb.dir == "out/e2e/kb-fix-failures"
    assert exp.error_cases is not None and exp.error_cases.kb_errors_min
    responses = [parse_fix_response(t) for t in load_replay_responses(FIXTURES / name / "replay")]
    assert len(responses) == 5 and all(r.changes and not r.warnings for r in responses)
    src = {ch.path: (FIXTURES / name / ch.path).read_text(encoding="utf-8") for r in responses for ch in r.changes}
    bad1, bad2, keep1, real, keep2 = (r.changes[0] for r in responses)
    for ch in (bad1, bad2):                                                         # apply_failed after feedback
        assert ch.path.endswith("ClientIpFilter.java") and src[ch.path].count(ch.search) == 0
    assert real.path == bad1.path and src[real.path].count(real.search) == 1
    assert real.replace == real.search.replace(".clientIp(", ".remoteClientIp(")                     # text replace
    for ch in (keep1, keep2):
        assert ch.path.endswith("MemberController.java") and src[ch.path].count(ch.search) == 1
        assert "LegacyRequestHelper.clientIp(http)" in ch.replace                                  # still broken
