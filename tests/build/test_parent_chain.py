"""v2.1p: the in-house parent chain's facts read from its poms — configured parents → `<parent>` upwards → import BOMs
one level deep; child-wins properties, nearest-declaration-wins managed entries (declared before imported); the Boot
BOM added when the chain does not reach it; unreadable poms listed; cached; never raises. Offline: a tmp local
repository (the real function is imported before `tests/conftest.py` patches the module attribute)."""
from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.build import boot_bom
from sbmig.build.parent_chain import ParentChainFacts, clear_cache, load_parent_chain_facts
from sbmig.core.config import ArtifactSpec

NS = 'xmlns="http://maven.apache.org/POM/4.0.0"'
BOOT_V = "9.9.9"
BASE = "com.company:base-parent:4.0.0"
STARTER = f"org.springframework.boot:spring-boot-starter-parent:{BOOT_V}"
DEPS = f"org.springframework.boot:spring-boot-dependencies:{BOOT_V}"


def _dep(g: str, a: str, v: str, *, imp: bool = False) -> str:
    extra = "<type>pom</type><scope>import</scope>" if imp else ""
    return f"<dependency><groupId>{g}</groupId><artifactId>{a}</artifactId><version>{v}</version>{extra}</dependency>"


def _pom(coord: str, *, parent: str = "", props: dict[str, str] | None = None, deps: list[str] | None = None) -> str:
    g, a, v = coord.split(":")
    par = ""
    if parent:
        pg, pa, pv = parent.split(":")
        par = f"<parent><groupId>{pg}</groupId><artifactId>{pa}</artifactId><version>{pv}</version></parent>"
    p = "".join(f"<{k}>{val}</{k}>" for k, val in (props or {}).items())
    return (f"<project {NS}><modelVersion>4.0.0</modelVersion>{par}<groupId>{g}</groupId><artifactId>{a}</artifactId>"
            f"<version>{v}</version><packaging>pom</packaging><properties>{p}</properties>"
            f"<dependencyManagement><dependencies>{''.join(deps or [])}</dependencies></dependencyManagement>"
            "</project>\n")


def _write(repo: Path, coord: str, text: str) -> None:
    g, a, v = coord.split(":")
    d = repo.joinpath(*g.split("."), a, v)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{a}-{v}.pom").write_text(text, encoding="utf-8")


def _spec(coord: str) -> ArtifactSpec:
    g, a, v = coord.split(":")
    return ArtifactSpec(group_id=g, artifact_id=a, version=v)


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_cache()
    yield
    clear_cache()


@pytest.fixture()
def repo(tmp_path: Path, cfg):
    """base-parent (company) → spring-boot-starter-parent → spring-boot-dependencies (which imports x-bom); base-parent
    imports y-bom itself."""
    r = tmp_path / "m2"
    _write(r, BASE, _pom(BASE, parent=STARTER,
                         props={"java.version": "21", "spring-boot.version": "${boot.line}", "boot.line": BOOT_V,
                                "company.version": "4.0.0"},
                         deps=[_dep("org.example", "shared", "3.0.0"), _dep("com.company", "lib", "${company.version}"),
                               _dep("org.example.y", "y-bom", "1.0", imp=True)]))
    _write(r, STARTER, _pom(STARTER, parent=DEPS, props={"java.version": "17", "resource.delimiter": "@"}))
    _write(r, DEPS, _pom(DEPS, props={"lib.version": "3.5", "x.version": "7.0.8"},
                         deps=[_dep("org.example", "shared", "1.0.0"), _dep("org.example", "lib", "${lib.version}"),
                               _dep("org.example.x", "x-bom", "${x.version}", imp=True)]))
    _write(r, "org.example.x:x-bom:7.0.8", _pom("org.example.x:x-bom:7.0.8", props={"x-only.version": "1"},
                                                 deps=[_dep("org.example.x", "x-core", "${project.version}")]))
    _write(r, "org.example.y:y-bom:1.0", _pom("org.example.y:y-bom:1.0",
                                               deps=[_dep("org.example", "lib", "9.0"),
                                                     _dep("org.example.y", "y-core", "1.0")]))
    cfg.build.local_repo = str(r)
    cfg.target.spring_boot_version = BOOT_V
    cfg.artifacts.parent = _spec(BASE)
    cfg.artifacts.boot_parent = _spec(BASE)                     # the same pom twice → read once
    cfg.artifacts.comp_parent = _spec("com.company:comp-jar:4.0.0")   # not in the repository
    cfg.artifacts.parent_manages_spring_boot = True
    return r


def test_the_chain_merge_rules(repo: Path, cfg) -> None:
    f = load_parent_chain_facts(cfg)
    assert f.roots == [BASE, "com.company:comp-jar:4.0.0"]                      # de-duplicated, in config order
    # properties: the child wins; inherited from the whole chain; an imported BOM's are not inherited
    assert f.properties["java.version"] == "21" and f.property_sources["java.version"] == BASE
    assert f.properties["resource.delimiter"] == "@" and f.property_sources["resource.delimiter"] == STARTER
    assert f.properties["lib.version"] == "3.5" and f.property_sources["lib.version"] == DEPS
    assert f.properties["spring-boot.version"] == BOOT_V                         # ${…} resolved
    assert "x-only.version" not in f.properties
    # v2.1t: only what a COMPANY pom defines may be referenced — never the Boot poms' properties
    assert f.company_properties == {"java.version": "21", "spring-boot.version": BOOT_V, "boot.line": BOOT_V,
                                    "company.version": "4.0.0"}
    assert set(f.company_property_sources.values()) == {BASE}
    assert "lib.version" in f.properties and "lib.version" not in f.company_properties
    # managed: the nearest declaration wins; every declared entry wins over the imported BOMs
    assert f.managed["org.example:shared"] == "3.0.0"                            # base-parent over the Boot BOM
    assert f.managed["org.example:lib"] == "3.5"                                 # declared (Boot BOM) over y-bom's 9.0
    assert f.managed["com.company:lib"] == "4.0.0"
    assert f.managed["org.example.y:y-core"] == "1.0" and f.managed["org.example.x:x-core"] == "7.0.8"
    assert not f.boot_bom_added                                                  # the chain reaches the Boot BOM
    assert f.poms == 5                                                           # 3 chain poms + 2 imported BOMs
    assert f.unreadable == ["com.company:comp-jar:4.0.0"]
    assert any(s.startswith("com.company:comp-jar:4.0.0: not in the local repository") for s in f.sources)
    cap = f.as_capture()
    assert cap["by_module"] == {} and cap["parent"] == BASE and cap["managed"]["org.example:shared"] == "3.0.0"


def test_the_boot_bom_is_added_when_the_chain_misses_it(tmp_path: Path, cfg, monkeypatch) -> None:
    r = tmp_path / "m2"
    _write(r, BASE, _pom(BASE, props={"spring-boot.version": "4.1.0"}, deps=[_dep("org.example", "shared", "3.0.0")]))
    cfg.build.local_repo = str(r)
    cfg.artifacts.parent, cfg.artifacts.boot_parent, cfg.artifacts.comp_parent = _spec(BASE), None, None
    bb = boot_bom.BootBomFacts(version="4.1.0", managed={"org.example:shared": "1.0.0", "org.example:other": "2.0"},
                               properties={"hibernate.version": "7.4.1.Final", "spring-boot.version": "x"},
                               sources=["stub BOM"])
    monkeypatch.setattr(boot_bom, "load_boot_bom_facts", lambda c, version=None: bb)
    cfg.artifacts.parent_manages_spring_boot = True
    f = load_parent_chain_facts(cfg)
    assert f.boot_bom_added
    assert f.managed == {"org.example:shared": "3.0.0", "org.example:other": "2.0"}   # lowest priority
    assert f.properties["spring-boot.version"] == "4.1.0" and f.property_sources["spring-boot.version"] == BASE
    # v2.1t: the add-on is an import — its properties are not inherited, so they never enter `properties`
    assert "hibernate.version" not in f.properties and f.company_properties == {"spring-boot.version": "4.1.0"}
    assert any(s.startswith("Boot BOM add-on") for s in f.sources)
    clear_cache()
    cfg.artifacts.parent_manages_spring_boot = False                    # no promise → no add-on
    g = load_parent_chain_facts(cfg)
    assert not g.boot_bom_added and "org.example:other" not in g.managed


def test_a_chain_importing_the_boot_bom_reaches_it(tmp_path: Path, cfg, monkeypatch) -> None:
    """The company style: base-parent IMPORTS spring-boot-dependencies — its managed versions count, its properties
    are not inherited, no add-on."""
    r = tmp_path / "m2"
    _write(r, BASE, _pom(BASE, props={"spring-boot.version": BOOT_V},
                         deps=[_dep("org.springframework.boot", "spring-boot-dependencies", "${spring-boot.version}",
                                    imp=True)]))
    _write(r, DEPS, _pom(DEPS, props={"lib.version": "3.5"}, deps=[_dep("org.example", "lib", "${lib.version}")]))
    cfg.build.local_repo = str(r)
    cfg.artifacts.parent, cfg.artifacts.boot_parent, cfg.artifacts.comp_parent = _spec(BASE), None, None
    cfg.artifacts.parent_manages_spring_boot = True
    monkeypatch.setattr(boot_bom, "load_boot_bom_facts", lambda c, version=None: pytest.fail("no add-on expected"))
    f = load_parent_chain_facts(cfg)
    assert f.managed == {"org.example:lib": "3.5"} and not f.boot_bom_added
    assert set(f.properties) == {"spring-boot.version"}


def test_cache_and_never_raises(repo: Path, cfg, monkeypatch) -> None:
    first = load_parent_chain_facts(cfg)
    assert load_parent_chain_facts(cfg) is first
    clear_cache()
    assert load_parent_chain_facts(cfg) is not first
    assert load_parent_chain_facts(object()).empty                      # no artifacts at all
    cfg.artifacts.parent = _spec("com.company:missing:1.0")
    cfg.artifacts.boot_parent = cfg.artifacts.comp_parent = None
    clear_cache()
    f = load_parent_chain_facts(cfg)
    assert f.empty and f.unreadable == ["com.company:missing:1.0"]
    from sbmig.build import parent_chain
    monkeypatch.setattr(parent_chain, "_load", lambda *a: (_ for _ in ()).throw(RuntimeError("disk on fire")))
    clear_cache()
    assert load_parent_chain_facts(cfg).sources == ["failed: RuntimeError: disk on fire"]
    assert ParentChainFacts().empty
