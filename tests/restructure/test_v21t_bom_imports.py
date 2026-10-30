"""v2.1t: `artifacts.parent_manages_spring_boot: auto` is decided from the parent chain's poms; when the chain carries no
Spring Boot BOM the restructure imports it with the chain's `${spring-boot.version}` (else the literal target), Spring
Cloud likewise (`${spring-cloud.version}`, else the `spring_cloud_bom.yml` pairing), and in-house artifact versions
reference the chain's property (`${lib-jpa.version}`) instead of a literal. Offline: the chain loader is monkeypatched."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sbmig.build import parent_chain
from sbmig.core.config import ArtifactSpec, Config
from sbmig.core.models import LibraryUsage
from sbmig.restructure import PomEditor, Restructurer

from .helpers import HEAD, CaseType, dep, java, make_ws, profile
from .test_company_layout import _roles

BASE = "com.company.platform:base-parent:4.0.0"
BOOTJ = "com.company.platform:boot-jar:4.0.0"
COMPJ = "com.company.platform:comp-jar:4.0.0"
BOOT_KEY = "org.springframework.boot:spring-boot-dependencies"
CLOUD_KEY = "org.springframework.cloud:spring-cloud-dependencies"
BACK, BOOTP = "shop-back/pom.xml", "shop-boot/pom.xml"
JPA = "com.company.lib:lib-jpa-starter"


BOOT_DEPS = "org.springframework.boot:spring-boot-dependencies:4.1.0"


def _chain(props: dict[str, str] | None = None, *, reaches: bool = False, managed: dict[str, str] | None = None,
           roots: tuple[str, ...] = (BASE, BOOTJ, COMPJ), boot_props: dict[str, str] | None = None
           ) -> parent_chain.ParentChainFacts:
    """`props` are defined by base-parent (a company pom); `boot_props` by the inherited Boot BOM (not company)."""
    props = {"spring-boot.version": "4.1.0", "spring-cloud.version": "2025.1.4", "lib-jpa.version": "4.0.1"} \
        if props is None else props
    boot_props = dict(boot_props or {})
    every = {**boot_props, **props}
    return parent_chain.ParentChainFacts(
        roots=list(roots), managed=dict(managed or {"com.company.platform:company-core": "4.0.0"}),
        properties=every, property_sources={**{k: BOOT_DEPS for k in boot_props}, **{k: BASE for k in props}},
        company_properties=dict(props), company_property_sources={k: BASE for k in props},
        root_properties={r: sorted(every) for r in roots}, reaches_boot_bom=reaches, boot_bom_added=not reaches,
        sources=["test chain"], poms=3)


@pytest.fixture()
def ccfg(cfg: Config) -> Config:
    """The round-9c company layout with role parents, `parent_manages_spring_boot: auto` (the v2.1t default)."""
    L = cfg.layout
    L.module_dir, L.boot_module = "", "{name}-boot"
    L.comp_module, L.front_module = "{name}-back", "{name}-front"
    L.boot_as_is = "in-place"
    L.comp_artifact_suffix = "-back"
    cfg = _roles(cfg)
    cfg.artifacts.parent_manages_spring_boot = "auto"
    return cfg


def _files(extra_deps: str = "") -> dict[str, str]:
    pom = (HEAD + "    <groupId>com.acme</groupId>\n    <artifactId>shop</artifactId>\n    <version>1.0.0</version>\n"
           "    <dependencies>\n" + dep("org.springframework", "spring-context", "6.1.14") + extra_deps
           + "    </dependencies>\n</project>\n")
    return {"pom.xml": pom, "src/main/java/com/acme/shop/Main.java": java("com.acme.shop", "Main")}


def _run(tmp_path: Path, cfg: Config, monkeypatch, chain, *, libraries=(), extra_deps: str = "", name: str = "t21"):
    monkeypatch.setattr(parent_chain, "load_parent_chain_facts", lambda c: chain)
    ws = make_ws(tmp_path, _files(extra_deps), name)
    prof = profile(ws, CaseType.SPRING_LEGACY, artifact_id="shop", version="1.0.0", base_package="com.acme.shop")
    prof.libraries = [LibraryUsage(name=n) for n in libraries]
    res = Restructurer().apply(ws, prof, cfg, mode="layout")
    assert res.ok, res.error
    return ws, res, prof


def _imports(ws, pom: str) -> dict[str, str | None]:
    ed = PomEditor(ws.root / pom)
    return {g.key: g.version for g in ed.managed_dependencies() if (g.scope or "") == "import"}


def _events(ws, name: str) -> list[dict]:
    return [e for e in (json.loads(x) for x in ws.journal_path.read_text(encoding="utf-8").splitlines() if x.strip())
            if e.get("event") == name]


# ------------------------------------------------------------------------------------------------ Spring Boot
def test_auto_without_a_chain_bom_imports_it_through_the_chain_property(tmp_path: Path, ccfg: Config,
                                                                         monkeypatch) -> None:
    ws, res, prof = _run(tmp_path, ccfg, monkeypatch, _chain())
    assert res.parent_manages_spring_boot_effective is False
    assert any(n.startswith("artifacts.parent_manages_spring_boot: auto → false") for n in res.notes)
    for pom in (BACK, BOOTP):
        assert _imports(ws, pom)[BOOT_KEY] == "${spring-boot.version}"
        assert PomEditor(ws.root / pom).managed_dependencies()[0].key == BOOT_KEY      # first entry
        assert f"{pom}: {BOOT_KEY} ${{spring-boot.version}} (property of {BASE})" in res.bom_imports
    plugin = PomEditor(ws.root / BOOTP).find_plugins("org.springframework.boot", "spring-boot-maven-plugin", "build")
    assert plugin and PomEditor.text_of(plugin[0], "version") == "${spring-boot.version}"
    ev = _events(ws, "bom_imports")
    assert {e["pom"] for e in ev} == {BACK, BOOTP} and all(e["property"] == "spring-boot.version" for e in ev)
    assert "parent-chain-no-boot-bom" not in res.flags                  # auto: no warning, it imports itself
    # idempotent second run (already restructured)
    before = {p: (ws.root / p).read_bytes() for p in (BACK, BOOTP)}
    res2 = Restructurer().apply(ws, prof, ccfg, mode="layout")
    assert res2.ok and res2.bom_imports == [] and {p: (ws.root / p).read_bytes() for p in before} == before


def test_no_chain_property_imports_the_literal_target(tmp_path: Path, ccfg: Config, monkeypatch) -> None:
    ws, res, _prof = _run(tmp_path, ccfg, monkeypatch, _chain({"other.version": "1"}))
    assert _imports(ws, BACK)[BOOT_KEY] == "4.1.0"
    assert f"{BACK}: {BOOT_KEY} 4.1.0 (no property in the parent chain)" in res.bom_imports


def test_a_property_only_another_root_defines_is_not_used(tmp_path: Path, ccfg: Config, monkeypatch) -> None:
    chain = _chain()
    chain.root_properties = {BASE: ["spring-boot.version"], BOOTJ: [], COMPJ: []}    # comp-jar's chain lacks it
    ws, _res, _prof = _run(tmp_path, ccfg, monkeypatch, chain, name="t21root")
    assert _imports(ws, BACK)[BOOT_KEY] == "4.1.0"                     # ${…} would not resolve on comp-jar


def test_explicit_true_keeps_the_warning_and_writes_no_import(tmp_path: Path, ccfg: Config, monkeypatch) -> None:
    ccfg.artifacts.parent_manages_spring_boot = True
    ws, res, _prof = _run(tmp_path, ccfg, monkeypatch, _chain(), name="t21true")
    assert res.parent_manages_spring_boot_effective is True and res.bom_imports == []
    assert BOOT_KEY not in _imports(ws, BACK) and "parent-chain-no-boot-bom" in res.flags


def test_explicit_false_imports_even_when_the_chain_reaches_boot(tmp_path: Path, ccfg: Config, monkeypatch) -> None:
    ccfg.artifacts.parent_manages_spring_boot = False
    ws, res, _prof = _run(tmp_path, ccfg, monkeypatch, _chain(reaches=True), name="t21false")
    assert res.parent_manages_spring_boot_effective is False
    assert _imports(ws, BACK)[BOOT_KEY] == "${spring-boot.version}"


def test_auto_with_a_chain_reaching_boot_imports_nothing(tmp_path: Path, ccfg: Config, monkeypatch) -> None:
    ws, res, _prof = _run(tmp_path, ccfg, monkeypatch, _chain(reaches=True), name="t21reach")
    assert res.parent_manages_spring_boot_effective is True and res.bom_imports == []
    assert BOOT_KEY not in _imports(ws, BACK)


# ------------------------------------------------------------------------------------------------ Spring Cloud
def test_spring_cloud_follows_the_chain_property(tmp_path: Path, ccfg: Config, monkeypatch) -> None:
    ws, res, _prof = _run(tmp_path, ccfg, monkeypatch, _chain(reaches=True), libraries=["spring-cloud"],
                          name="t21cloud")
    assert _imports(ws, BACK)[CLOUD_KEY] == "${spring-cloud.version}"
    assert f"{BACK}: {CLOUD_KEY} ${{spring-cloud.version}} (property of {BASE})" in res.bom_imports


def test_spring_cloud_without_a_property_takes_the_pairing(tmp_path: Path, ccfg: Config, monkeypatch) -> None:
    ws, res, _prof = _run(tmp_path, ccfg, monkeypatch, _chain({}, reaches=True), libraries=["spring-cloud"],
                          name="t21pair")
    assert _imports(ws, BACK)[CLOUD_KEY] == "2025.1.4"                 # knowledge/config/spring_cloud_bom.yml, Boot 4.1
    assert f"{BACK}: {CLOUD_KEY} 2025.1.4 (no property in the parent chain)" in res.bom_imports


def test_spring_cloud_the_chain_manages_is_left_alone(tmp_path: Path, ccfg: Config, monkeypatch) -> None:
    chain = _chain(reaches=True, managed={"org.springframework.cloud:spring-cloud-commons": "5.0.0"})
    ws, res, _prof = _run(tmp_path, ccfg, monkeypatch, chain, libraries=["spring-cloud"], name="t21cmgd")
    assert CLOUD_KEY not in _imports(ws, BACK) and not any(CLOUD_KEY in x for x in res.bom_imports)
    ws2, _res2, _p2 = _run(tmp_path / "nolib", ccfg, monkeypatch, _chain(reaches=True), name="t21nolib")
    assert CLOUD_KEY not in _imports(ws2, BACK)                         # no Spring Cloud in the project


# ------------------------------------------------------------------------------------------------ in-house
def _jpa_spec(**kw) -> ArtifactSpec:
    return ArtifactSpec(group_id="com.company.lib", artifact_id="lib-jpa-starter", version="4.0.0", **kw)


def _version(ws, key: str, pom: str = BACK) -> str | None:
    return {g.key: g.version for g in PomEditor(ws.root / pom).dependencies()}.get(key)


def test_an_optional_with_a_property_name_the_chain_defines(tmp_path: Path, ccfg: Config, monkeypatch) -> None:
    ccfg.artifacts.optional = [_jpa_spec(property_name="company-jpa.version")]
    chain = _chain({"company-jpa.version": "4.0.2", "lib-jpa.version": "4.0.1"}, reaches=True)
    ws, res, _prof = _run(tmp_path, ccfg, monkeypatch, chain, extra_deps=dep("com.company.lib", "lib-jpa-starter",
                                                                            "3.0.0"), name="t21pn")
    assert _version(ws, JPA) == "${company-jpa.version}"               # property_name first
    assert f"{JPA} 3.0.0→${{company-jpa.version}}" in res.optional_bumped
    assert f"optional {JPA}: version ${{company-jpa.version}} (property of {BASE})" in res.notes


def test_an_optional_matched_by_its_artifact_id_stem(tmp_path: Path, ccfg: Config, monkeypatch) -> None:
    ccfg.artifacts.optional = [_jpa_spec()]
    ws, _res, prof = _run(tmp_path, ccfg, monkeypatch, _chain(reaches=True),
                          extra_deps=dep("com.company.lib", "lib-jpa-starter", "3.0.0"), name="t21stem")
    assert _version(ws, JPA) == "${lib-jpa.version}"                   # lib-jpa-starter → lib-jpa.version
    before = (ws.root / BACK).read_bytes()
    res2 = Restructurer().apply(ws, prof, ccfg, mode="layout")
    assert res2.ok and res2.optional_bumped == [] and (ws.root / BACK).read_bytes() == before


def test_no_chain_property_keeps_the_literal(tmp_path: Path, ccfg: Config, monkeypatch) -> None:
    ccfg.artifacts.optional = [_jpa_spec()]
    ws, res, _prof = _run(tmp_path, ccfg, monkeypatch, _chain({"unrelated.version": "1"}, reaches=True),
                          extra_deps=dep("com.company.lib", "lib-jpa-starter", "3.0.0"), name="t21lit")
    assert _version(ws, JPA) == "4.0.0" and f"{JPA} 3.0.0→4.0.0" in res.optional_bumped


def test_a_mandatory_artifact_follows_the_chain_property(tmp_path: Path, ccfg: Config, monkeypatch) -> None:
    spec = ccfg.artifacts.mandatory[0]
    chain = _chain({f"{spec.artifact_id}.version": "4.1.0"}, reaches=True)
    ws, res, _prof = _run(tmp_path, ccfg, monkeypatch, chain, name="t21mand")
    assert _version(ws, spec.key, "pom.xml") == f"${{{spec.artifact_id}.version}}"
    assert any(n.startswith(f"mandatory {spec.key}: version ${{{spec.artifact_id}.version}}") for n in res.notes)


def test_the_boot_boms_own_properties_are_never_referenced(tmp_path: Path, ccfg: Config, monkeypatch) -> None:
    """e2e boot2 (2026-10-30): with a chain inheriting the Boot BOM, its lombok.version / commons-lang3.version are not
    company properties — the optionals keep their literal versions (as before v2.1t)."""
    ccfg.artifacts.optional = [ArtifactSpec(group_id="org.projectlombok", artifact_id="lombok", version="1.18.38"),
                               ArtifactSpec(group_id="org.apache.commons", artifact_id="commons-lang3",
                                            version="3.17.0")]
    chain = _chain({}, reaches=True, boot_props={"lombok.version": "1.18.42", "commons-lang3.version": "3.19.0",
                                                 "spring-boot.version": "4.1.0"})
    extra = dep("org.projectlombok", "lombok", "1.18.30") + dep("org.apache.commons", "commons-lang3", "3.12.0")
    ws, res, _prof = _run(tmp_path, ccfg, monkeypatch, chain, extra_deps=extra, name="t21boot")
    assert _version(ws, "org.projectlombok:lombok") == "1.18.38"
    assert _version(ws, "org.apache.commons:commons-lang3") == "3.17.0"
    assert not any("${" in x for x in res.optional_bumped)


def test_a_company_root_property_names_the_reuse_version(tmp_path: Path, ccfg: Config, monkeypatch) -> None:
    ccfg.artifacts.optional = [ArtifactSpec(group_id="com.company.lib", artifact_id="xxx-reuse", version="4.0.0")]
    chain = _chain({"xxx-reuse.version": "4.0.3"}, reaches=True, boot_props={"lombok.version": "1.18.42"})
    ws, res, _prof = _run(tmp_path, ccfg, monkeypatch, chain,
                          extra_deps=dep("com.company.lib", "xxx-reuse", "3.0.0"), name="t21reuse")
    assert _version(ws, "com.company.lib:xxx-reuse") == "${xxx-reuse.version}"
    assert "com.company.lib:xxx-reuse 3.0.0→${xxx-reuse.version}" in res.optional_bumped


def test_the_boot_import_property_must_be_a_company_one(tmp_path: Path, ccfg: Config, monkeypatch) -> None:
    """With the local stand-ins only the Boot poms define spring-boot.version-style names → the literal path."""
    ccfg.artifacts.parent_manages_spring_boot = False
    chain = _chain({}, reaches=True, boot_props={"spring-boot.version": "4.1.0"})
    ws, res, _prof = _run(tmp_path, ccfg, monkeypatch, chain, name="t21lit2")
    assert _imports(ws, BACK)[BOOT_KEY] == "4.1.0"
    assert f"{BACK}: {BOOT_KEY} 4.1.0 (no property in the parent chain)" in res.bom_imports
