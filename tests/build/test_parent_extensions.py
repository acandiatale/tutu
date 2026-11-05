"""2026-11-05 (company run): the migration target parent base-parent 4.0.0 declares org.apache.maven.wagon:wagon-webdav:1.0-beta-2
in <build><extensions>. Inherited, so no child pom can drop it; Maven must download it before any goal. The company PC held a
stale `wagon-webdav-1.0-beta-2.pom.lastUpdated` from the HTTP-blocker days, so Maven kept failing although Nexus served the pom
again. The intake pre-check reads the chain's extensions, clears stale markers of a repository-served extension and reports
one nobody serves."""
from __future__ import annotations

from pathlib import Path

from sbmig.build import repo_versions
from sbmig.build.parent_chain import ParentChainFacts
from sbmig.build.parent_extensions import ExtensionCheck, check_parent_build_extensions

WAGON = "org.apache.maven.wagon:wagon-webdav:1.0-beta-2"
POM = '<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion><groupId>org.apache.maven.wagon</groupId>' \
      "<artifactId>wagon-webdav</artifactId><version>1.0-beta-2</version></project>"
PROVIDERS = "org.apache.maven.wagon:wagon-providers:1.0-beta-2"
POM_WITH_PARENT = ('<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>'
                   "<parent><groupId>org.apache.maven.wagon</groupId><artifactId>wagon-providers</artifactId><version>1.0-beta-2</version></parent>"
                   "<artifactId>wagon-webdav</artifactId></project>")
PROVIDERS_POM = ('<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion><groupId>org.apache.maven.wagon</groupId>'
                 "<artifactId>wagon-providers</artifactId><version>1.0-beta-2</version><packaging>pom</packaging></project>")


def _facts() -> ParentChainFacts:
    return ParentChainFacts(roots=["com.company:base-parent:4.0.0"], build_extensions=[WAGON],
                            build_extension_sources={WAGON: "com.company:base-parent:4.0.0"})


def _folder(repo: Path) -> Path:
    d = repo / "org" / "apache" / "maven" / "wagon" / "wagon-webdav" / "1.0-beta-2"
    d.mkdir(parents=True, exist_ok=True)
    return d


class _Serving:
    def __init__(self, cfg=None, **kw):
        self.cfg = cfg

    def pom_text(self, g, a, v):
        return POM if (g, a, v) == ("org.apache.maven.wagon", "wagon-webdav", "1.0-beta-2") else None


def test_stale_markers_of_a_repository_served_extension_are_removed(tmp_path: Path, cfg, monkeypatch):
    repo = tmp_path / "m2"
    cfg.build.local_repo = str(repo)
    d = _folder(repo)
    (d / "wagon-webdav-1.0-beta-2.pom.lastUpdated").write_text("x", encoding="utf-8")
    (d / "wagon-webdav-1.0-beta-2.jar.lastUpdated").write_text("x", encoding="utf-8")
    (d / "resolver-status.properties").write_text("x", encoding="utf-8")
    monkeypatch.setattr(repo_versions, "RepositoryLookup", _Serving)
    checks = check_parent_build_extensions(cfg, _facts())
    assert len(checks) == 1 and checks[0].readable and checks[0].where == "repository"
    assert checks[0].markers_removed == ["wagon-webdav-1.0-beta-2.jar.lastUpdated", "wagon-webdav-1.0-beta-2.pom.lastUpdated"]
    assert sorted(p.name for p in d.iterdir()) == ["resolver-status.properties"]           # only *.lastUpdated goes
    assert "stale markers removed" in checks[0].line() and checks[0].declared_by == "com.company:base-parent:4.0.0"
    # clear_markers off (build.retry_negative_cache: false) → reported, nothing deleted
    (d / "wagon-webdav-1.0-beta-2.pom.lastUpdated").write_text("x", encoding="utf-8")
    checks = check_parent_build_extensions(cfg, _facts(), clear_markers=False)
    assert checks[0].readable and checks[0].markers_removed == [] and (d / "wagon-webdav-1.0-beta-2.pom.lastUpdated").is_file()


def test_a_locally_present_extension_needs_nothing_and_an_unserved_one_is_reported(tmp_path: Path, cfg, monkeypatch):
    repo = tmp_path / "m2"
    cfg.build.local_repo = str(repo)
    d = _folder(repo)
    (d / "wagon-webdav-1.0-beta-2.pom").write_text(POM, encoding="utf-8")
    (d / "wagon-webdav-1.0-beta-2.pom.lastUpdated").write_text("x", encoding="utf-8")    # harmless beside the real pom
    checks = check_parent_build_extensions(cfg, _facts())                                  # conftest: offline lookup
    assert checks[0].readable and checks[0].where.endswith("wagon-webdav-1.0-beta-2.pom") and checks[0].markers_removed == []
    # nobody serves it: not readable, markers untouched, the reason kept
    for f in d.iterdir():
        f.unlink()
    (d / "wagon-webdav-1.0-beta-2.pom.lastUpdated").write_text("x", encoding="utf-8")
    checks = check_parent_build_extensions(cfg, _facts())
    assert not checks[0].readable and "not readable from the repository" in checks[0].where
    assert (d / "wagon-webdav-1.0-beta-2.pom.lastUpdated").is_file() and "NOT readable" in checks[0].line()


def test_the_extension_poms_parent_chain_is_checked_too(tmp_path: Path, cfg, monkeypatch):
    """Maven resolves the extension's <parent> chain as well — a served wagon-webdav pom whose parent wagon-providers pom nobody
    serves still fails with 'Failed to read artifact descriptor'; the pre-check names that parent."""
    repo = tmp_path / "m2"
    cfg.build.local_repo = str(repo)
    served = {("org.apache.maven.wagon", "wagon-webdav", "1.0-beta-2"): POM_WITH_PARENT}

    class _Repo:
        def __init__(self, cfg=None, **kw):
            pass

        def pom_text(self, g, a, v):
            return served.get((g, a, v))
    monkeypatch.setattr(repo_versions, "RepositoryLookup", _Repo)
    check = check_parent_build_extensions(cfg, _facts())[0]
    assert not check.readable and check.where.startswith(f"its parent pom {PROVIDERS} is not readable")
    assert check.ancestors == [] and "NOT readable" in check.line()
    # the parent served too (with a stale marker in the local repo) → readable, the chain recorded, the marker gone
    served[("org.apache.maven.wagon", "wagon-providers", "1.0-beta-2")] = PROVIDERS_POM
    d = repo / "org" / "apache" / "maven" / "wagon" / "wagon-providers" / "1.0-beta-2"
    d.mkdir(parents=True)
    (d / "wagon-providers-1.0-beta-2.pom.lastUpdated").write_text("x", encoding="utf-8")
    check = check_parent_build_extensions(cfg, _facts())[0]
    assert check.readable and check.ancestors == [PROVIDERS] and check.markers_removed == ["wagon-providers-1.0-beta-2.pom.lastUpdated"]
    assert f"parent poms: {PROVIDERS}" in check.line()


def test_no_extensions_or_unresolvable_coordinates(tmp_path: Path, cfg):
    cfg.build.local_repo = str(tmp_path / "m2")
    assert check_parent_build_extensions(cfg, ParentChainFacts()) == []
    facts = ParentChainFacts(build_extensions=["${wagon.group}:wagon-webdav:1.0-beta-2"],
                             build_extension_sources={"${wagon.group}:wagon-webdav:1.0-beta-2": "g:p:1"})
    checks = check_parent_build_extensions(cfg, facts)
    assert checks == [ExtensionCheck("${wagon.group}:wagon-webdav:1.0-beta-2", "g:p:1", False,
                                     "coordinates not resolvable from the chain's properties")]
