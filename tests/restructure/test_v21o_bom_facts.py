"""v2.1o (2) / v2.1p: without the post-restructure effective pom the managed-version cleanup and the v2.1l pin
settlement use the in-house parent chain's facts (`sbmig.build.parent_chain`, monkeypatched here — never the network,
never `~/.m2`; the Boot BOM loader only for poms importing the BOM when no parent manages Boot), and the as-is version
properties that override the chain's (`spring-security.version=5.7.11`, `spring-boot.version=2.5.15` …) are removed in
every mode."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sbmig.build import boot_bom, parent_chain
from sbmig.core.config import Config
from sbmig.core.models import PomInfo
from sbmig.restructure import PomEditor, Restructurer
from sbmig.review.inputs import restructure_lines

from .helpers import HEAD, CaseType, dep, java, make_ws, profile
from .test_company_layout import _boot_multi_files, _boot_multi_profile, _roles
from .test_restructurer import msa_files, msa_profile

CTX = "org.springframework:spring-context"
SEC = "org.springframework.security:spring-security-core"
TEXT = "org.apache.commons:commons-text"
CSV = "org.apache.commons:commons-csv"
HIBFOO = "org.hibernate:hibernate-foo"
BACK = "shop-back/pom.xml"
BOOT = "org.springframework.boot:spring-boot-dependencies:4.1.0"
BASE = "com.company.platform:base-parent:4.0.0"
FACTS = parent_chain.ParentChainFacts(
    roots=[BASE], managed={CTX: "7.0.8", SEC: "7.1.0", TEXT: "1.12.0"},
    properties={"spring-security.version": "7.1.0", "spring-batch.version": "6.0.4", "hibernate.version": "7.4.1.Final",
                "java.version": "17", "maven.compiler.release": "17", "spring-boot.version": "4.1.0"},
    property_sources={"spring-security.version": BOOT, "spring-batch.version": BOOT, "hibernate.version": BOOT,
                      "java.version": BASE, "maven.compiler.release": BASE, "spring-boot.version": BASE},
    sources=["test facts"], poms=3)
BOOT_FACTS = boot_bom.BootBomFacts(version="4.1.0", managed=dict(FACTS.managed), sources=["test Boot BOM"])
ASIS = {"managed": {TEXT: "1.10.0", CSV: "1.10.0"}, "direct": {}, "by_module": {}}
SOURCE_BOM = f"managed-version source: parent chain facts — {BASE} (3 managed artifacts)"


@pytest.fixture()
def ccfg(cfg: Config) -> Config:
    """The round-9c company layout with role parents (as `test_v21l_pin_versions.ccfg`)."""
    L = cfg.layout
    L.module_dir, L.boot_module = "", "{name}-boot"
    L.comp_module, L.front_module = "{name}-back", "{name}-front"
    L.boot_as_is = "in-place"
    L.comp_artifact_suffix = "-back"
    return _roles(cfg)


@pytest.fixture()
def facts(monkeypatch):
    calls: list[object] = []

    def _load(cfg):
        calls.append(cfg)
        return FACTS

    monkeypatch.setattr(parent_chain, "load_parent_chain_facts", _load)
    return calls


def _legacy_files() -> dict[str, str]:
    pom = (HEAD + "    <groupId>com.acme</groupId>\n    <artifactId>shop</artifactId>\n    <version>1.0.0</version>\n"
           "    <properties>\n        <spring.version>5.3.31</spring.version>\n"
           "        <spring-security.version>5.7.11</spring-security.version>\n"
           "        <spring-batch.version>4.3.10</spring-batch.version>\n"
           "        <hibernate.version>5.6.15.Final</hibernate.version>\n"
           "        <my.version>1.0</my.version>\n    </properties>\n"
           "    <dependencies>\n" + dep("org.springframework", "spring-context", "${spring.version}")
           + dep("org.springframework.security", "spring-security-core", "${spring-security.version}")
           + dep("org.hibernate", "hibernate-foo", "${hibernate.version}")        # not BOM-managed: stays
           + dep("org.apache.commons", "commons-text") + dep("org.apache.commons", "commons-csv")
           + "    </dependencies>\n</project>\n")
    return {"pom.xml": pom, "src/main/java/com/acme/shop/Main.java": java("com.acme.shop", "Main")}


def _layout(tmp_path: Path, cfg: Config, *, capture=None, name: str = "bom"):
    ws = make_ws(tmp_path, _legacy_files(), name)
    prof = profile(ws, CaseType.SPRING_LEGACY, artifact_id="shop", version="1.0.0", base_package="com.acme.shop")
    prof.poms = [PomInfo(path="pom.xml", artifact_id="shop", group_id="com.acme",
                         dependencies=PomEditor(ws.root / "pom.xml").dependencies())]
    params = {"asis_effective": lambda: ASIS, "effective_pom": lambda: capture}
    res = Restructurer().apply(ws, prof, cfg, mode="layout", params=params)
    assert res.ok, res.error
    return ws, res, prof, params


def _versions(ws, pom: str = BACK) -> dict[str, str | None]:
    return {d.key: d.version for d in PomEditor(ws.root / pom).dependencies()}


def _props(ws, pom: str = BACK) -> dict[str, str]:
    return PomEditor(ws.root / pom).properties()


def _events(ws, name: str) -> list[dict]:
    if not ws.journal_path.is_file():
        return []
    return [e for e in (json.loads(x) for x in ws.journal_path.read_text(encoding="utf-8").splitlines() if x.strip())
            if e.get("event") == name]


# ------------------------------------------------------------------------------------------ managed versions
def test_without_a_capture_the_bom_facts_decide(tmp_path: Path, ccfg: Config, facts) -> None:
    ws, res, _p, _pa = _layout(tmp_path, ccfg)
    v = _versions(ws)
    assert v[CTX] is None and v[SEC] is None                         # explicit Spring versions the BOM manages
    assert set(res.managed_versions_dropped) == {f"{CTX}: 5.3.31 → managed 7.0.8 ({BACK})",
                                                 f"{SEC}: 5.7.11 → managed 7.1.0 ({BACK})"}
    assert v[TEXT] is None                                           # pinned 1.10.0, the BOM manages it → emptied
    assert v[CSV] == "1.10.0"                                        # pinned, not BOM-managed → stays (no undo)
    assert res.versions_pinned == [f"{CSV}: 1.10.0 ← as-is managed by the as-is dependencyManagement ({BACK})"]
    assert any(n.startswith(SOURCE_BOM) and "unavailable" in n for n in res.notes)
    assert "managed-version cleanup skipped: no effective pom" not in res.notes
    assert not any("carry-over undone" in n for n in res.notes)
    assert len(facts) == 1                                           # read once per restructure


def test_without_a_capture_and_without_facts_the_pins_are_undone(tmp_path: Path, ccfg: Config) -> None:
    ws, res, _p, _pa = _layout(tmp_path, ccfg)                      # tests/conftest.py: no Boot BOM facts
    v = _versions(ws)
    assert v[CTX] == "${spring.version}" and v[TEXT] is None and v[CSV] is None     # nothing dropped, pins undone
    assert "managed-version cleanup skipped: no effective pom" in res.notes
    assert any(n.startswith("as-is version carry-over undone for 2 dependency(ies): no effective pom of the new "
                            "parent chain (and no Boot BOM facts)") for n in res.notes)
    assert res.bom_properties_removed == [] and _events(ws, "bom_property_overrides") == []


def test_a_real_capture_still_wins(tmp_path: Path, ccfg: Config, facts) -> None:
    capture = {"managed": {CTX: "7.0.8", CSV: "1.10.0"}, "properties": {}, "by_module": {}}
    ws, res, _p, _pa = _layout(tmp_path, ccfg, capture=capture)
    v = _versions(ws)
    assert v[CTX] is None and v[SEC] == "${spring-security.version}"     # the capture does not manage SEC
    assert v[CSV] is None and v[TEXT] == "1.10.0"                        # the capture's view of the pins
    assert "managed-version source: effective pom of the new parent chain" in res.notes
    assert not any(n.startswith(SOURCE_BOM) for n in res.notes)
    # the property step still reads the BOM facts: spring-security.version is referenced → kept
    assert "property spring-security.version kept: still referenced by shop-back/pom.xml" in res.notes


def test_no_parent_managing_boot_uses_the_facts_only_where_the_bom_is_imported(tmp_path: Path, ccfg: Config,
                                                                               monkeypatch) -> None:
    # a parent chain that carries no Boot (empty facts) + the restructure imports the BOM into the modules
    monkeypatch.setattr(boot_bom, "load_boot_bom_facts", lambda cfg, version=None: BOOT_FACTS)
    ccfg.artifacts.parent_manages_spring_boot = False
    ws, res, _p, _pa = _layout(tmp_path, ccfg)
    assert any(n.startswith("managed-version source: Boot BOM 4.1.0 (3 managed) for the 2 pom(s) importing it")
               for n in res.notes)
    assert _versions(ws)[CTX] is None
    # an imported BOM ignores the importer's properties: the override step does not run …
    assert res.bom_properties_removed == [] and _events(ws, "bom_property_overrides") == []
    assert _props(ws)["spring-batch.version"] == "4.3.10"                  # unreferenced, but not an override here
    # … while the properties only the dropped versions used go with them (whatever their names)
    assert sorted(res.version_properties_removed) == [f"{BACK}: spring-security.version=5.7.11",
                                                      f"{BACK}: spring.version=5.3.31"]


# ------------------------------------------------------------------------------------------ property overrides
def test_bom_named_properties_are_removed_unless_referenced(tmp_path: Path, ccfg: Config, facts) -> None:
    ws, res, _p, _pa = _layout(tmp_path, ccfg)
    props = _props(ws)
    assert "spring-security.version" not in props and "spring-batch.version" not in props
    assert props["hibernate.version"] == "5.6.15.Final"          # ${hibernate.version} still used by hibernate-foo
    assert "spring.version" not in props and props["my.version"] == "1.0"         # v2.1o: dead after the drop
    assert res.version_properties_removed == [f"{BACK}: spring.version=5.3.31"]
    assert res.bom_properties_removed == [f"{BACK}: spring-security.version=5.7.11",
                                          f"{BACK}: spring-batch.version=4.3.10"]
    assert "property hibernate.version kept: still referenced by shop-back/pom.xml" in res.notes
    assert any(n.startswith("2 as-is version propert(ies) removed: they override the in-house parent chain's "
                            f"properties of the same name ({BASE})") for n in res.notes)
    assert res.bom_property_sources == {"spring-security.version": BOOT, "spring-batch.version": BOOT}
    assert _props(ws)["maven.compiler.release"] == "21"          # the chain defines it too, but Java is ours
    ev = _events(ws, "bom_property_overrides")
    assert ev and ev[0]["removed"] == res.bom_properties_removed and ev[0]["source"] == "parent chain"
    chain_ev = _events(ws, "parent_chain_facts")
    assert len(chain_ev) == 1 and chain_ev[0]["roots"] == [BASE] and chain_ev[0]["managed"] == 3
    assert ev[0]["kept"] == [f"{BACK}: hibernate.version (referenced by {BACK})"]
    text = "\n".join(restructure_lines([], [res]))
    assert "as-is version properties removed (they overrode the parent's Spring Boot BOM properties)" in text
    # second pass (already restructured): nothing left to remove
    before = (ws.root / BACK).read_bytes()
    prof, params = _p, _pa
    res2 = Restructurer().apply(ws, prof, ccfg, mode="layout", params=params)
    assert res2.ok and res2.bom_properties_removed == [] and (ws.root / BACK).read_bytes() == before


def test_the_switch_turns_the_property_step_off(tmp_path: Path, ccfg: Config, facts) -> None:
    ccfg.layout.drop_bom_property_overrides = False
    ws, res, _p, _pa = _layout(tmp_path, ccfg)
    assert res.bom_properties_removed == [] and _props(ws)["spring-batch.version"] == "4.3.10"
    assert _events(ws, "bom_property_overrides") == []


def test_boot_pom_mode(tmp_path: Path, ccfg: Config, facts) -> None:
    files = _boot_multi_files()
    files["shop-core/pom.xml"] = files["shop-core/pom.xml"].replace(
        "    <dependencies>\n", "    <properties>\n        <spring-batch.version>4.3.10</spring-batch.version>\n"
                               "    </properties>\n    <dependencies>\n"
                               + dep("org.springframework", "spring-context", "5.3.31"))
    ws = make_ws(tmp_path, files, "boot-bom")
    res = Restructurer().apply(ws, _boot_multi_profile(ws), ccfg, mode="boot_pom",
                               params={"effective_pom": lambda: None})
    assert res.ok, res.error
    assert _versions(ws, "shop-core/pom.xml")[CTX] is None
    assert "spring-batch.version" not in _props(ws, "shop-core/pom.xml")
    assert res.bom_properties_removed == ["shop-core/pom.xml: spring-batch.version=4.3.10"]
    assert any(n.startswith(SOURCE_BOM) for n in res.notes)


def test_msa_pom_mode(tmp_path: Path, ccfg: Config, facts) -> None:
    files = msa_files()
    files["user-service/pom.xml"] = files["user-service/pom.xml"].replace(
        "    <properties>\n", "    <properties>\n        <hibernate.version>5.6.15.Final</hibernate.version>\n")
    files["order-service/pom.xml"] = files["order-service/pom.xml"].replace(
        "    </dependencies>", dep("org.springframework.security", "spring-security-core", "5.7.11")
        + "    </dependencies>")
    ws = make_ws(tmp_path, files, "msa-bom")
    res = Restructurer().apply(ws, msa_profile(ws), ccfg, mode="msa_pom", params={"effective_pom": lambda: None})
    assert res.ok, res.error
    assert res.bom_properties_removed == ["user-service/pom.xml: hibernate.version=5.6.15.Final"]
    assert _versions(ws, "order-service/pom.xml")[SEC] is None
    assert any(n.startswith(SOURCE_BOM) for n in res.notes)


def test_the_spring5_security_batch_shape(tmp_path: Path, ccfg: Config, facts) -> None:
    """Fixture shape: spring.version / spring-security.version / spring-batch.version — the first only referenced by
    versions the drop removes, the other two Boot BOM property names; a property a test-scoped dependency still
    references stays (its explicit version is not BOM-managed)."""
    pom = (HEAD + "    <groupId>com.acme</groupId>\n    <artifactId>shop</artifactId>\n    <version>1.0.0</version>\n"
           "    <properties>\n        <spring.version>5.3.31</spring.version>\n"
           "        <spring-security.version>5.7.11</spring-security.version>\n"
           "        <spring-batch.version>4.3.10</spring-batch.version>\n"
           "        <junit.version>4.13.2</junit.version>\n    </properties>\n"
           "    <dependencies>\n" + dep("org.springframework", "spring-context", "${spring.version}")
           + dep("org.springframework", "spring-webmvc", "${spring.version}")
           + dep("org.springframework.security", "spring-security-core", "${spring-security.version}")
           + dep("org.springframework.batch", "spring-batch-core", "${spring-batch.version}")
           + dep("junit", "junit", "${junit.version}", scope="test")
           + "    </dependencies>\n</project>\n")
    managed = {**FACTS.managed, "org.springframework:spring-webmvc": "7.0.8",
               "org.springframework.batch:spring-batch-core": "6.0.4"}
    ws = make_ws(tmp_path, {"pom.xml": pom, "src/main/java/com/acme/shop/Main.java": java("com.acme.shop", "Main")},
                 "s5")
    prof = profile(ws, CaseType.SPRING_LEGACY, artifact_id="shop", version="1.0.0", base_package="com.acme.shop")
    capture = {"managed": managed, "properties": {}, "by_module": {}}
    res = Restructurer().apply(ws, prof, ccfg, mode="layout", params={"effective_pom": lambda: capture})
    assert res.ok, res.error
    props = _props(ws)
    assert not {"spring.version", "spring-security.version", "spring-batch.version"} & set(props)
    assert props["junit.version"] == "4.13.2" and _versions(ws)["junit:junit"] == "${junit.version}"
    assert res.version_properties_removed == [f"{BACK}: spring.version=5.3.31"]
    assert sorted(res.bom_properties_removed) == [f"{BACK}: spring-batch.version=4.3.10",
                                                  f"{BACK}: spring-security.version=5.7.11"]
    assert any(n.startswith("1 version propert(ies) removed: no dependency references them after the "
                            "parent-managed versions were dropped: spring.version=5.3.31") for n in res.notes)
    text = "\n".join(restructure_lines([], [res]))
    assert "version properties removed (only the dropped explicit versions used them)" in text


# ------------------------------------------------------------------------------------------ in-house parent names
def _boot_with(props: str, extra_dep: str = "") -> dict[str, str]:
    files = _boot_multi_files()
    files["shop-core/pom.xml"] = files["shop-core/pom.xml"].replace(
        "    <dependencies>\n", f"    <properties>\n{props}    </properties>\n    <dependencies>\n" + extra_dep)
    return files


def test_a_name_only_the_in_house_parent_defines_is_removed(tmp_path: Path, ccfg: Config, facts) -> None:
    """`spring-boot.version` is no Boot BOM property: base-parent defines it — read from its pom, no config list."""
    assert ccfg.layout.parent_property_names == []                               # the default
    ws = make_ws(tmp_path, _boot_with("        <spring-boot.version>2.5.15</spring-boot.version>\n"), "pp-rm")
    res = Restructurer().apply(ws, _boot_multi_profile(ws), ccfg, mode="boot_pom")
    assert res.ok, res.error
    assert "spring-boot.version" not in _props(ws, "shop-core/pom.xml")
    assert res.bom_properties_removed == ["shop-core/pom.xml: spring-boot.version=2.5.15"]
    assert res.bom_property_sources == {"spring-boot.version": BASE}
    assert _events(ws, "bom_property_overrides")[0]["source"] == "parent chain"


def test_without_chain_facts_nothing_is_removed(tmp_path: Path, ccfg: Config) -> None:
    ws = make_ws(tmp_path, _boot_with("        <spring-boot.version>2.5.15</spring-boot.version>\n"), "pp-none")
    res = Restructurer().apply(ws, _boot_multi_profile(ws), ccfg, mode="boot_pom")   # conftest: no chain facts
    assert res.ok and res.bom_properties_removed == []
    assert _props(ws, "shop-core/pom.xml")["spring-boot.version"] == "2.5.15"


def test_a_referenced_spring_boot_version_is_kept(tmp_path: Path, ccfg: Config, facts) -> None:
    # org.example:boot-addon is not managed by anything → its ${spring-boot.version} version is never dropped
    files = _boot_with("        <spring-boot.version>2.5.15</spring-boot.version>\n",
                       dep("org.example", "boot-addon", "${spring-boot.version}"))
    ws = make_ws(tmp_path, files, "pp-keep")
    res = Restructurer().apply(ws, _boot_multi_profile(ws), ccfg, mode="boot_pom")
    assert res.ok, res.error
    assert _props(ws, "shop-core/pom.xml")["spring-boot.version"] == "2.5.15"
    assert res.bom_properties_removed == []
    assert "property spring-boot.version kept: still referenced by shop-core/pom.xml" in res.notes


def test_the_escape_hatch_needs_no_facts(tmp_path: Path, ccfg: Config) -> None:
    ccfg.layout.parent_property_names = ["spring.version"]
    ws = make_ws(tmp_path, _boot_with("        <spring.version>5.3.31</spring.version>\n"
                                      "        <other.version>1.0</other.version>\n"), "pp-cfg")
    res = Restructurer().apply(ws, _boot_multi_profile(ws), ccfg, mode="boot_pom")   # conftest: no chain facts
    assert res.ok, res.error
    props = _props(ws, "shop-core/pom.xml")
    assert "spring.version" not in props and props["other.version"] == "1.0"
    assert res.bom_properties_removed == ["shop-core/pom.xml: spring.version=5.3.31"]
    assert res.bom_property_sources == {}
    assert any(n.startswith("1 as-is propert(ies) removed: they override the in-house parent's properties")
               for n in res.notes)
    assert _events(ws, "bom_property_overrides")[0]["source"] == "parent_property_names"
    ccfg.layout.drop_bom_property_overrides = False                    # the switch covers these names too
    ws2 = make_ws(tmp_path / "off", _boot_with("        <spring.version>5.3.31</spring.version>\n"), "pp-off")
    res2 = Restructurer().apply(ws2, _boot_multi_profile(ws2), ccfg, mode="boot_pom")
    assert res2.bom_properties_removed == [] and _props(ws2, "shop-core/pom.xml")["spring.version"] == "5.3.31"


# ------------------------------------------------------------------------------------------ recipe-raised versions
def _recipe_raised(tmp_path: Path, cfg: Config, asis, name: str):
    """legacy-external-parent shape: the Framework 7 recipe set `spring.version` to 7.0.10 (`7.0.x` = the newest in the
    repository), the new parent chain manages spring-context 7.0.8."""
    pom = (HEAD + "    <groupId>com.acme</groupId>\n    <artifactId>shop</artifactId>\n    <version>1.0.0</version>\n"
           "    <properties>\n        <spring.version>7.0.10</spring.version>\n    </properties>\n"
           "    <dependencies>\n" + dep("org.springframework", "spring-context", "${spring.version}")
           + dep("org.example", "recipe-added", "2.0.0") + "    </dependencies>\n</project>\n")
    ws = make_ws(tmp_path, {"pom.xml": pom, "src/main/java/com/acme/shop/Main.java": java("com.acme.shop", "Main")},
                 name)
    prof = profile(ws, CaseType.SPRING_LEGACY, artifact_id="shop", version="1.0.0", base_package="com.acme.shop")
    capture = {"managed": {CTX: "7.0.8", "org.example:recipe-added": "1.5.0"}, "properties": {}, "by_module": {}}
    res = Restructurer().apply(ws, prof, cfg, mode="layout",
                               params={"asis_effective": asis, "effective_pom": lambda: capture})
    assert res.ok, res.error
    return ws, res


def test_a_recipe_raised_version_is_dropped(tmp_path: Path, ccfg: Config) -> None:
    asis = {"managed": {}, "direct": {CTX: "6.2.12"}, "by_module": {}}          # recipe-added: not in the as-is build
    ws, res = _recipe_raised(tmp_path, ccfg, lambda: asis, "rr")
    v = _versions(ws)
    assert v[CTX] is None and v["org.example:recipe-added"] is None
    assert res.recipe_versions_dropped == [f"{CTX} 7.0.10 (as-is 6.2.12, managed 7.0.8, {BACK})",
                                           f"org.example:recipe-added 2.0.0 (as-is -, managed 1.5.0, {BACK})"]
    assert res.managed_versions_dropped == []                                   # kept apart
    assert f"recipe-raised version dropped: {CTX} 7.0.10 (as-is 6.2.12, managed 7.0.8, {BACK})" in res.notes
    assert not any("newer than the parent-managed one kept" in n for n in res.notes)
    assert "spring.version" not in _props(ws) and res.version_properties_removed == [f"{BACK}: spring.version=7.0.10"]
    text = "\n".join(restructure_lines([], [res]))
    assert "recipe-raised versions dropped (the as-is build was at or below the managed version)" in text


def test_the_owners_own_newer_pin_is_kept(tmp_path: Path, ccfg: Config) -> None:
    asis = {"managed": {}, "direct": {CTX: "7.0.10", "org.example:recipe-added": "2.0.0"}, "by_module": {}}
    ws, res = _recipe_raised(tmp_path, ccfg, lambda: asis, "own")
    assert _versions(ws)[CTX] == "${spring.version}" and _props(ws)["spring.version"] == "7.0.10"
    assert res.recipe_versions_dropped == []
    assert any(n.startswith("explicit version(s) newer than the parent-managed one kept: ") and CTX in n
               for n in res.notes)


def test_without_an_as_is_capture_the_newer_version_is_kept(tmp_path: Path, ccfg: Config) -> None:
    ws, res = _recipe_raised(tmp_path, ccfg, lambda: None, "noasis")
    assert _versions(ws)[CTX] == "${spring.version}" and res.recipe_versions_dropped == []
    assert any("newer than the parent-managed one kept" in n for n in res.notes)
