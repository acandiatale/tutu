"""v2.1o: the Spring Boot BOM's facts without Maven — `spring-boot-dependencies:<v>` from the local repository, else
`RepositoryLookup.pom_text`; `${…}` resolved; imported BOMs one level deep; cached per process; never raises.
Offline: a tmp local repository and a stub lookup (the real function is imported before `tests/conftest.py` patches
the module attribute)."""
from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from sbmig.build import boot_bom
from sbmig.build.boot_bom import BootBomFacts, clear_cache, load_boot_bom_facts

NS = 'xmlns="http://maven.apache.org/POM/4.0.0"'
VERSION = "9.9.9"


def _dep(g: str, a: str, v: str, *, imp: bool = False) -> str:
    extra = "<type>pom</type><scope>import</scope>" if imp else ""
    return f"<dependency><groupId>{g}</groupId><artifactId>{a}</artifactId><version>{v}</version>{extra}</dependency>"


def _pom(g: str, a: str, v: str, props: dict[str, str], deps: list[str], parent: str = "") -> str:
    p = "".join(f"<{k}>{val}</{k}>" for k, val in props.items())
    return (f"<project {NS}><modelVersion>4.0.0</modelVersion>{parent}<groupId>{g}</groupId>"
            f"<artifactId>{a}</artifactId><version>{v}</version><packaging>pom</packaging>"
            f"<properties>{p}</properties><dependencyManagement><dependencies>{''.join(deps)}"
            "</dependencies></dependencyManagement></project>\n")


def boot_pom(imports: int = 1) -> str:
    deps = [_dep("org.springframework.boot", "spring-boot-starter-web", "${project.version}"),
            _dep("org.example", "lib", "${lib.version}"),
            _dep("org.example", "shared", "1.0.0"),                       # wins over the imported BOM's 2.0.0
            _dep("org.example", "broken", "${nowhere.version}"),          # unresolvable → skipped
            _dep("org.example.x", "x-bom", "${x.version}", imp=True)]
    deps += [_dep("org.many", f"bom-{i}", "1.0", imp=True) for i in range(1, imports)]
    return _pom("org.springframework.boot", "spring-boot-dependencies", VERSION,
                {"lib.version": "${lib.major}.5", "lib.major": "3", "x.version": "7.0.8",
                 "spring-security.version": "7.1.0"}, deps)


X_BOM = _pom("org.example.x", "x-bom", "7.0.8", {"x-extra.version": "1.2"},
             [_dep("org.example.x", "x-core", "${project.version}"),
              _dep("org.example.x", "x-extra", "${x-extra.version}"),
              _dep("org.example.x", "x-lib", "${lib.version}"),           # the Boot BOM's property
              _dep("org.example", "shared", "2.0.0"),
              _dep("org.example.y", "y-bom", "1.0", imp=True)])          # nested import: not followed


def _write(repo: Path, g: str, a: str, v: str, text: str) -> None:
    d = repo.joinpath(*g.split("."), a, v)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{a}-{v}.pom").write_text(text, encoding="utf-8")


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_cache()
    yield
    clear_cache()


@pytest.fixture()
def local(tmp_path: Path, cfg):
    repo = tmp_path / "m2"
    repo.mkdir()
    cfg.build.local_repo = str(repo)
    cfg.target.spring_boot_version = VERSION
    return repo


class _StubLookup:
    """`RepositoryLookup` stand-in: answers `pom_text` from a dict and counts the calls."""
    texts: ClassVar[dict[tuple[str, str, str], str]] = {}
    calls: ClassVar[list[tuple[str, str, str]]] = []

    def __init__(self, cfg=None, *, fetch=None):
        pass

    def pom_text(self, g, a, v):
        _StubLookup.calls.append((g, a, v))
        return _StubLookup.texts.get((g, a, v))


@pytest.fixture()
def stub(monkeypatch):
    _StubLookup.texts, _StubLookup.calls = {}, []
    monkeypatch.setattr("sbmig.build.repo_versions.RepositoryLookup", _StubLookup)
    return _StubLookup


def test_the_local_repository_bom_and_its_import(local: Path, cfg, stub) -> None:
    _write(local, "org.springframework.boot", "spring-boot-dependencies", VERSION, boot_pom())
    _write(local, "org.example.x", "x-bom", "7.0.8", X_BOM)
    f = load_boot_bom_facts(cfg)
    assert not f.empty and f.version == VERSION
    assert f.managed["org.springframework.boot:spring-boot-starter-web"] == VERSION        # ${project.version}
    assert f.managed["org.example:lib"] == "3.5"                                            # nested ${…}
    assert f.managed["org.example:shared"] == "1.0.0"                     # the Boot BOM's own entry wins
    assert "org.example:broken" not in f.managed
    assert f.managed["org.example.x:x-core"] == "7.0.8" and f.managed["org.example.x:x-extra"] == "1.2"
    assert f.managed["org.example.x:x-lib"] == "3.5"                      # resolved from the parent BOM
    assert "org.example.x:x-bom" not in f.managed and "org.example.y:y-bom" not in f.managed
    assert f.properties["lib.version"] == "3.5" and f.properties["spring-security.version"] == "7.1.0"
    assert "x-extra.version" not in f.properties                         # an imported BOM's properties never count
    assert stub.calls == []                                               # everything came from the local repository
    assert f.sources[0].startswith(f"org.springframework.boot:spring-boot-dependencies:{VERSION}: {local}")
    assert "1 unresolved" in f.sources[0] and f.sources[1].startswith("org.example.x:x-bom:7.0.8: ")


def test_the_repository_answers_what_the_local_repository_lacks(local: Path, cfg, stub) -> None:
    stub.texts = {("org.springframework.boot", "spring-boot-dependencies", VERSION): boot_pom(),
                  ("org.example.x", "x-bom", "7.0.8"): X_BOM}
    f = load_boot_bom_facts(cfg)
    assert f.managed["org.example.x:x-core"] == "7.0.8"
    assert stub.calls == [("org.springframework.boot", "spring-boot-dependencies", VERSION),
                          ("org.example.x", "x-bom", "7.0.8")]
    assert f.sources[0].endswith("repository (3 managed, 4 properties, 1 imported BOMs, 1 unresolved)")


def test_as_capture_has_the_load_effective_shape(local: Path, cfg, stub) -> None:
    _write(local, "org.springframework.boot", "spring-boot-dependencies", VERSION, boot_pom())
    cap = load_boot_bom_facts(cfg).as_capture()
    assert set(cap) == {"managed", "direct", "plugins", "properties", "parent", "modules", "by_module"}
    assert cap["by_module"] == {} and cap["managed"]["org.example:lib"] == "3.5"
    assert cap["properties"]["x.version"] == "7.0.8"
    assert "not in the local repository and not readable" in load_boot_bom_facts(cfg).sources[-1]   # x-bom


def test_nothing_readable_is_empty_and_not_cached(local: Path, cfg, stub) -> None:
    f = load_boot_bom_facts(cfg)
    assert f.empty and f.version == VERSION and f.managed == {} and f.as_capture()["managed"] == {}
    want = (f"org.springframework.boot:spring-boot-dependencies:{VERSION}: not in the local repository and not "
            "readable from the repository")
    assert f.sources == [want]
    _write(local, "org.springframework.boot", "spring-boot-dependencies", VERSION, boot_pom())
    assert not load_boot_bom_facts(cfg).empty                             # a failure is retried next time


def test_a_successful_load_is_cached_per_version(local: Path, cfg, stub) -> None:
    _write(local, "org.springframework.boot", "spring-boot-dependencies", VERSION, boot_pom())
    first = load_boot_bom_facts(cfg)
    assert load_boot_bom_facts(cfg) is first and load_boot_bom_facts(cfg, VERSION) is first
    assert load_boot_bom_facts(cfg, "1.0.0").empty                        # another version: its own entry
    clear_cache()
    assert load_boot_bom_facts(cfg) is not first


def test_the_number_of_poms_is_bounded(local: Path, cfg, stub) -> None:
    _write(local, "org.springframework.boot", "spring-boot-dependencies", VERSION, boot_pom(imports=80))
    f = load_boot_bom_facts(cfg)
    assert not f.empty
    assert len(stub.calls) <= boot_bom.MAX_POMS                           # 1 local + the rest asked, then skipped
    assert any(f"skipped (more than {boot_bom.MAX_POMS} poms)" in s for s in f.sources)


def test_never_raises(local: Path, cfg, stub, monkeypatch) -> None:
    assert load_boot_bom_facts(object()).empty                            # no target / build at all
    _write(local, "org.springframework.boot", "spring-boot-dependencies", VERSION, "not xml at all")
    assert load_boot_bom_facts(cfg).empty
    monkeypatch.setattr(boot_bom, "_load", lambda *a: (_ for _ in ()).throw(RuntimeError("disk on fire")))
    clear_cache()
    f = load_boot_bom_facts(cfg)
    assert f.empty and f.sources == ["failed: RuntimeError: disk on fire"]
    assert BootBomFacts().empty
