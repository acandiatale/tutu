"""v2.1z-e jakarta build discovery (sbmig.build.jakarta_discovery): the verified jakarta builds of a javax-era library
in the repository — the same coordinates' higher line (`2.0.5-jakarta`, verified by its version qualifier or a jar scan
when the pom is silent), the library table's `jakarta_key`, Nexus search by stem, sibling-name probes (`grid8-jakarta`,
`com.example.grid9:grid9`); bounded, cached, never raising. No network: a dict-backed fetch."""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Optional

import pytest

from sbmig.build.jakarta_discovery import (
    JakartaBuild,
    JakartaDiscovery,
    digit_variants,
    discover_jakarta_builds,
    probe_report,
    qualifier_is_jakarta,
    scan_jar,
    sibling_names,
    stem,
)
from sbmig.build.repo_versions import RepositoryLookup
from sbmig.core.config import Config
from sbmig.core.jakarta_libraries import JakartaLibrary

NEXUS = "https://nexus.example/repository/maven-public/"
API = "https://nexus.example/service/rest/v1/search"


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("MAVEN_HOME", raising=False)
    monkeypatch.delenv("M2_HOME", raising=False)
    monkeypatch.setattr("sbmig.build.repo_versions.global_settings_file", lambda *_a, **_k: None)


def _cfg(tmp_path: Path, **libraries) -> Config:
    return Config.model_validate({"artifacts": {"parent": {"group_id": "g", "artifact_id": "a"}},
                                  "build": {"repository_url": NEXUS, "local_repo": str(tmp_path / "m2")},
                                  "libraries": libraries})


def _metadata(*versions: str) -> str:
    vs = "".join(f"<version>{v}</version>" for v in versions)
    return f"<metadata><versioning><versions>{vs}</versions></versioning></metadata>"


def _pom(g: str, a: str, v: str, deps: str = "") -> str:
    return (f'<project xmlns="http://maven.apache.org/POM/4.0.0"><groupId>{g}</groupId><artifactId>{a}</artifactId>'
            f"<version>{v}</version><dependencies>{deps}</dependencies></project>")


JAVAX_DEP = "<dependency><groupId>javax.servlet</groupId><artifactId>javax.servlet-api</artifactId></dependency>"
JAKARTA_DEP = "<dependency><groupId>jakarta.servlet</groupId><artifactId>jakarta.servlet-api</artifactId></dependency>"


def _jar(*refs: bytes) -> bytes:
    """A jar with one class file whose bytes carry the given package references."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("com/example/grid/GridExporter.class", b"\xca\xfe\xba\xbe" + b"".join(refs))
        zf.writestr("META-INF/MANIFEST.MF", b"Manifest-Version: 1.0\n")
    return buf.getvalue()


class Fetch:
    def __init__(self, files: dict[str, str], jars: Optional[dict[str, bytes]] = None):
        self.files, self.jars = files, jars or {}
        self.urls: list[str] = []
        self.jar_urls: list[str] = []

    def text(self, url, credentials, timeout):
        self.urls.append(url)
        return self.files.get(url)

    def data(self, url, credentials, timeout, max_bytes):
        self.jar_urls.append(url)
        return self.jars.get(url)


def _lookup(tmp_path: Path, files: dict[str, str], jars: Optional[dict[str, bytes]] = None, **libraries):
    fetch = Fetch(files, jars)
    look = RepositoryLookup(_cfg(tmp_path, **libraries), fetch=fetch.text, fetch_bytes=fetch.data)
    return look, fetch


def _vendor_files() -> dict[str, str]:
    """The company shape: grid8 1.0.6 / 1.1.3 / 1.1.15 (javax, poms declare javax.servlet) and 2.0.5-jakarta (silent pom)."""
    base = f"{NEXUS}com/example/grid8/grid8/"
    files = {base + "maven-metadata.xml": _metadata("1.0.6", "1.1.3", "1.1.15", "2.0.5-jakarta")}
    for v in ("1.0.6", "1.1.3", "1.1.15"):
        files[f"{base}{v}/grid8-{v}.pom"] = _pom("com.example.grid8", "grid8", v, JAVAX_DEP)
    files[f"{base}2.0.5-jakarta/grid8-2.0.5-jakarta.pom"] = _pom("com.example.grid8", "grid8", "2.0.5-jakarta")
    return files


# ---------------------------------------------------------------------------------------------------------------------
# names and verification helpers
# ---------------------------------------------------------------------------------------------------------------------
def test_stems_digit_variants_and_sibling_names():
    assert stem("grid8") == "grid" and stem("vendor-ui9") == "vendor-ui" and stem("plain") == "plain" and stem("") == ""
    assert digit_variants("grid8") == ["grid9", "grid10"] and digit_variants("plain") == []
    names = sibling_names("com.example.grid8", "grid8", limit=12)
    assert names[0] == ("com.example.grid8", "grid8-jakarta")
    assert ("com.example.grid9", "grid9") in names and ("com.example.grid8", "grid9") in names
    assert ("com.example.grid9", "grid8") not in names                      # the group alone means nothing
    assert len(names) <= 12 and len(set(names)) == len(names)
    assert sibling_names("org.plain", "lib", limit=3) == [("org.plain", "lib-jakarta"), ("org.plain", "lib_jakarta"),
                                                           ("org.plain", "lib.jakarta")]


def test_version_qualifier_and_jar_scan():
    assert qualifier_is_jakarta("2.0.5-jakarta") and qualifier_is_jakarta("3.1.JAKARTA") and not qualifier_is_jakarta("2.0.5")
    assert scan_jar(_jar(b"jakarta/servlet/http/HttpServletRequest")) is True
    assert scan_jar(_jar(b"javax/servlet/http/HttpServletRequest")) is False
    assert scan_jar(_jar(b"javax/annotation/processing/Generated")) is None          # the JDK's, not EE
    assert scan_jar(_jar(b"com/example/Other")) is None and scan_jar(b"not a zip") is None
    assert scan_jar(_jar(b"jakarta/persistence/Entity", b"javax/annotation/processing/X")) is True


# ---------------------------------------------------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------------------------------------------------
def test_the_vendor_line_higher_major_with_a_jakarta_qualifier_is_verified_and_preferred(tmp_path):
    look, fetch = _lookup(tmp_path, _vendor_files())
    d = discover_jakarta_builds(look, "com.example.grid8", "grid8", "1.0.6", cfg=look.cfg)
    same = [b for b in d.builds if b.source == "same"]
    assert [b.version for b in same] == ["1.1.15", "1.1.3", "2.0.5-jakarta"]       # same major newest first, then higher
    best = d.best()
    assert best is not None and best.coordinates == "com.example.grid8:grid8:2.0.5-jakarta" and best.verified
    assert best.how == "version qualifier" and best.major_change and best.source == "same"
    assert not [b for b in same if b.version.startswith("1.1") and b.verified]       # javax poms stay unverified
    assert d.hint_lines()[0].startswith("repository also has com.example.grid8:grid8 2.0.5-jakarta (jakarta ✓ — version qualifier; "
                                        "found by same coordinates, a higher major)")
    assert "probed" in d.searched_text() and "grid8-jakarta" in d.searched_text()
    # cached per (g, a, current) on the lookup: the second call makes no new request
    n = len(fetch.urls)
    assert discover_jakarta_builds(look, "com.example.grid8", "grid8", "1.0.6", cfg=look.cfg) is d and len(fetch.urls) == n


def test_a_silent_pom_without_a_qualifier_is_verified_by_the_jar_scan_or_stays_unverified(tmp_path):
    base = f"{NEXUS}com/example/grid8/grid8/"
    files = {base + "maven-metadata.xml": _metadata("1.0.6", "2.0.0"),
             base + "1.0.6/grid8-1.0.6.pom": _pom("com.example.grid8", "grid8", "1.0.6", JAVAX_DEP),
             base + "2.0.0/grid8-2.0.0.pom": _pom("com.example.grid8", "grid8", "2.0.0")}
    jars = {base + "2.0.0/grid8-2.0.0.jar": _jar(b"jakarta/servlet/http/HttpServletResponse")}
    look, fetch = _lookup(tmp_path, files, jars)
    d = discover_jakarta_builds(look, "com.example.grid8", "grid8", "1.0.6", cfg=look.cfg)
    b = next(x for x in d.builds if x.version == "2.0.0")
    assert b.verified and b.how == "jar scan ✓" and fetch.jar_urls == [base + "2.0.0/grid8-2.0.0.jar"]
    # no jar → unverified, never the best
    look2, _f = _lookup(tmp_path, files)
    d2 = discover_jakarta_builds(look2, "com.example.grid8", "grid8", "1.0.6", cfg=look2.cfg)
    b2 = next(x for x in d2.builds if x.version == "2.0.0")
    assert not b2.verified and b2.how == "pom silent, jar inconclusive" and d2.best() is None
    assert any("not verified" in ln for ln in d2.hint_lines())
    # the scan budget: max_jar_scans 0 → not scanned
    look3, f3 = _lookup(tmp_path, files, jars, max_jar_scans=0)
    d3 = discover_jakarta_builds(look3, "com.example.grid8", "grid8", "1.0.6", cfg=look3.cfg)
    assert next(x for x in d3.builds if x.version == "2.0.0").how == "pom silent, jar not scanned" and f3.jar_urls == []


def test_sibling_probes_find_the_digit_variant_and_the_jakarta_artifact_id(tmp_path):
    base8 = f"{NEXUS}com/example/grid8/grid8/"
    base9 = f"{NEXUS}com/example/grid9/grid9/"
    files = {base8 + "maven-metadata.xml": _metadata("1.0.6"),
             base8 + "1.0.6/grid8-1.0.6.pom": _pom("com.example.grid8", "grid8", "1.0.6", JAVAX_DEP),
             base9 + "maven-metadata.xml": _metadata("1.0.0"),
             base9 + "1.0.0/grid9-1.0.0.pom": _pom("com.example.grid9", "grid9", "1.0.0", JAKARTA_DEP)}
    look, fetch = _lookup(tmp_path, files)
    d = discover_jakarta_builds(look, "com.example.grid8", "grid8", "1.0.6", cfg=look.cfg)
    best = d.best()
    assert best is not None and best.coordinates == "com.example.grid9:grid9:1.0.0" and best.source == "name-probe"
    assert best.how == "pom declares jakarta.*" and not best.major_change
    assert "com.example.grid9:grid9" in d.probed and len(d.probed) <= 12
    assert any(u.endswith("com/example/grid8/grid8-jakarta/maven-metadata.xml") for u in fetch.urls)
    # the `-jakarta` artifactId on the same group
    files_j = {base8 + "maven-metadata.xml": _metadata("1.0.6"),
               base8 + "1.0.6/grid8-1.0.6.pom": _pom("com.example.grid8", "grid8", "1.0.6", JAVAX_DEP),
               f"{NEXUS}com/example/grid8/grid8-jakarta/maven-metadata.xml": _metadata("1.2.0"),
               f"{NEXUS}com/example/grid8/grid8-jakarta/1.2.0/grid8-jakarta-1.2.0.pom":
                   _pom("com.example.grid8", "grid8-jakarta", "1.2.0")}
    look2, _f = _lookup(tmp_path, files_j)
    d2 = discover_jakarta_builds(look2, "com.example.grid8", "grid8", "1.0.6", cfg=look2.cfg)
    b = d2.best()
    assert b is not None and b.coordinates == "com.example.grid8:grid8-jakarta:1.2.0"
    assert b.how == "artifactId names jakarta"        # v2.1z-g: the vendor's own `-jakarta` artifactId verifies it
    # the probe bound
    look3, f3 = _lookup(tmp_path, files_j, max_jakarta_probes=1)
    d3 = discover_jakarta_builds(look3, "com.example.grid8", "grid8", "1.0.6", cfg=look3.cfg)
    assert d3.probed == ["grid8-jakarta"] and d3.best() is not None


def test_nexus_search_by_stem_with_pages(tmp_path):
    base8 = f"{NEXUS}com/example/grid8/grid8/"
    base_v = f"{NEXUS}com/vendor/vendor-grid-jakarta/"
    page1 = json.dumps({"items": [{"group": "com.vendor", "name": "vendor-grid-jakarta", "version": "3.0.0"},
                                  {"group": "org.other", "name": "gridlines", "version": "1"}],
                        "continuationToken": "t2"})
    page2 = json.dumps({"items": [{"group": "com.example.grid8", "name": "grid8", "version": "1.0.6"}],
                        "continuationToken": None})
    files = {base8 + "maven-metadata.xml": _metadata("1.0.6"),
             base8 + "1.0.6/grid8-1.0.6.pom": _pom("com.example.grid8", "grid8", "1.0.6", JAVAX_DEP),
             f"{API}?repository=maven-public&name=%2Agrid%2A": page1,
             f"{API}?repository=maven-public&name=%2Agrid%2A&continuationToken=t2": page2,
             base_v + "maven-metadata.xml": _metadata("3.0.0"),
             base_v + "3.0.0/vendor-grid-jakarta-3.0.0.pom": _pom("com.vendor", "vendor-grid-jakarta", "3.0.0", JAKARTA_DEP)}
    look, fetch = _lookup(tmp_path, files, max_jakarta_probes=0)
    d = discover_jakarta_builds(look, "com.example.grid8", "grid8", "1.0.6", cfg=look.cfg)
    found = [b for b in d.builds if b.source == "nexus-search"]
    assert [b.coordinates for b in found] == ["com.vendor:vendor-grid-jakarta:3.0.0", "org.other:gridlines:?"][:1] or \
        found[0].coordinates == "com.vendor:vendor-grid-jakarta:3.0.0"
    assert found[0].verified and d.best() is found[0]
    assert any("Nexus search name=*grid*" in x for x in d.searched)
    assert sum(1 for u in fetch.urls if u.startswith(API)) == 2                     # both pages, once each
    # nexus_search off → no API call
    look2, f2 = _lookup(tmp_path, files, nexus_search=False, max_jakarta_probes=0)
    discover_jakarta_builds(look2, "com.example.grid8", "grid8", "1.0.6", cfg=look2.cfg)
    assert not any(u.startswith(API) for u in f2.urls)


def test_the_table_jakarta_key_wins_and_a_classifier_entry_counts(tmp_path):
    base8 = f"{NEXUS}com/example/grid8/grid8/"
    base9 = f"{NEXUS}com/example/grid9/grid9/"
    files = {base8 + "maven-metadata.xml": _metadata("1.0.6"),
             base8 + "1.0.6/grid8-1.0.6.pom": _pom("com.example.grid8", "grid8", "1.0.6", JAVAX_DEP),
             base9 + "maven-metadata.xml": _metadata("1.0.0", "1.1.0-SNAPSHOT"),
             base9 + "1.0.0/grid9-1.0.0.pom": _pom("com.example.grid9", "grid9", "1.0.0")}
    entry = JakartaLibrary(key="com.example.grid8:grid8", jakarta_key="com.example.grid9:grid9", jakarta_since="1.0.0",
                           version="1.0.0", source="company")
    look, _f = _lookup(tmp_path, files, max_jakarta_probes=0, nexus_search=False)
    d = discover_jakarta_builds(look, "com.example.grid8", "grid8", "1.0.6", table_entry=entry, cfg=look.cfg)
    b = d.best()
    assert b is not None and b.source == "table" and b.coordinates == "com.example.grid9:grid9:1.0.0"
    assert b.verified and b.how == "library table (a person's entry)"
    assert "library table: com.example.grid9:grid9 1.0.0" in d.searched
    # the repository cannot be read: the table's own version, still applied
    look2, _f2 = _lookup(tmp_path, {}, max_jakarta_probes=0, nexus_search=False)
    d2 = discover_jakarta_builds(look2, "com.example.grid8", "grid8", "1.0.6", table_entry=entry, cfg=look2.cfg)
    assert d2.best() is not None and d2.best().version == "1.0.0" and "repository unreadable" in d2.searched[0]
    # a classifier jar of the same coordinates (Ehcache-style)
    ehc = JakartaLibrary(key="org.ehcache:ehcache", jakarta_since="3.10.0", version="3.10.8", classifier="jakarta")
    look3, _f3 = _lookup(tmp_path, {}, max_jakarta_probes=0, nexus_search=False)
    d3 = discover_jakarta_builds(look3, "org.ehcache", "ehcache", "3.9.0", table_entry=ehc, cfg=look3.cfg)
    assert d3.best() is not None and d3.best().classifier == "jakarta" and d3.best().source == "table"


def test_nothing_found_records_what_was_searched_and_never_raises(tmp_path):
    base8 = f"{NEXUS}com/example/grid8/grid8/"
    files = {base8 + "maven-metadata.xml": _metadata("1.0.6", "1.1.15"),
             base8 + "1.1.15/grid8-1.1.15.pom": _pom("com.example.grid8", "grid8", "1.1.15", JAVAX_DEP)}
    look, _f = _lookup(tmp_path, files)
    d = discover_jakarta_builds(look, "com.example.grid8", "grid8", "1.0.6", cfg=look.cfg)
    assert d.best() is None and d.verified == []
    txt = d.searched_text()
    assert "same coordinates: 1 release(s) newer than 1.0.6 checked, none jakarta" in txt
    assert "probed" in txt and "none found" in txt and "Nexus search" in txt
    assert d.hint_lines() == [f"no jakarta build of com.example.grid8:grid8 was found in the repository ({txt})"]
    # a lookup object that lacks everything: an empty discovery with an error, no exception
    class Broken:
        include_prerelease = False

        def candidates(self, key, current):
            raise RuntimeError("boom")

    d2 = discover_jakarta_builds(Broken(), "g", "a", "1", cfg=None)
    assert d2.builds == [] and d2.best() is None and "boom" in d2.error
    assert discover_jakarta_builds(None, "g", "a", "1").error == "no repository lookup"


def test_discovery_reports_unreadable_remote_repositories(tmp_path):
    def boom(url, credentials, timeout):
        raise TimeoutError("timed out")

    look = RepositoryLookup(_cfg(tmp_path), fetch=boom)
    d = discover_jakarta_builds(look, "com.example.grid8", "grid8", "1.0.6", cfg=look.cfg)
    assert d.best() is None
    assert any(x.startswith("remote repositories unreadable: ") and "timeout" in x for x in d.searched)
    assert "sibling search skipped" in d.searched_text()


def test_best_prefers_table_then_same_major_then_lowest_higher_major_then_siblings():
    d = JakartaDiscovery(key="g:a", current="1.0")
    d.builds = [JakartaBuild("g", "a", "3.0.0", source="same", verified=True, major_change=True),
                JakartaBuild("g", "a", "2.1.0", source="same", verified=True, major_change=True),
                JakartaBuild("g", "a", "2.0.0", source="same", verified=True, major_change=True),
                JakartaBuild("g", "a-jakarta", "1.0.0", source="name-probe", verified=True),
                JakartaBuild("g", "a", "9.9.9", source="same", verified=False, major_change=True)]
    assert d.best().version == "2.1.0"                                    # lowest higher major, newest of it
    d.builds.append(JakartaBuild("g", "a", "1.5.0", source="same", verified=True))
    assert d.best().version == "1.5.0"                                    # the same major beats a major change
    d.builds.append(JakartaBuild("g2", "a2", "7.0.0", source="table", verified=True))
    assert d.best().key == "g2:a2"                                        # the people's table first


def test_probe_report_lists_urls_outcomes_versions_and_verdicts(tmp_path):
    look, _f = _lookup(tmp_path, _vendor_files())
    lines = probe_report(look, "com.example.grid8", "grid8", "1.0.6", cfg=look.cfg)
    text = "\n".join(lines)
    assert lines[0] == "REPO-PROBE com.example.grid8:grid8 (current 1.0.6)"
    assert f"1. {NEXUS}  [build.repository_url (explicit: first); credentials: no]" in text
    assert f"{NEXUS} → ok (4 release(s))" in text
    assert "versions: 1.0.6, 1.1.3, 1.1.15, 2.0.5-jakarta" in text
    assert "higher majors: 2.0.5-jakarta (jakarta ✓ — version qualifier)" in text
    assert lines[-1].startswith("REPO-PROBE ok com.example.grid8:grid8 versions=4 jakarta=com.example.grid8:grid8:2.0.5-jakarta")
    none = probe_report(RepositoryLookup(_cfg(tmp_path), fetch=lambda u, c, t: None), "g", "a", None, cfg=None)
    assert none[-1].startswith("REPO-PROBE none g:a versions=0 jakarta=-")
    # 2026-11-05 (company PC: "repo-probe shows no Nexus"): the probe names the user settings path it looked for and says
    # that the poms' <repositories> are not part of a probe without --project
    text = "\n".join(none)
    assert "settings looked for but not read: " in text and ".m2" in text and "(~/.m2/settings.xml): missing" in text
    assert "pom <repositories>: not included (no --project given; a run reads the project's poms)" in text
    with_project = probe_report(RepositoryLookup(_cfg(tmp_path), project_root=tmp_path, fetch=lambda u, c, t: None),
                                "g", "a", None, cfg=None)
    assert not any(ln.startswith("pom <repositories>: not included") for ln in with_project)


def test_the_switch_off_is_the_v21d_behaviour(tmp_path):
    """`libraries.discover_jakarta_builds: false`: nothing is verified or probed — not even the same coordinates' higher
    line — so the loop has nothing to apply and a decline stands; the record says why."""
    look, fetch = _lookup(tmp_path, _vendor_files(), discover_jakarta_builds=False)
    d = discover_jakarta_builds(look, "com.example.grid8", "grid8", "1.0.6", cfg=look.cfg)
    assert d.builds == [] and d.best() is None and d.hint_lines() == []
    assert d.searched == ["discovery off (libraries.discover_jakarta_builds: false)"] and d.probed == []
    assert fetch.urls == [] and fetch.jar_urls == []                        # no request at all


def test_artifact_id_naming_jakarta_verifies_the_build_by_itself():
    """v2.1z-g (company Nexus: `dext5-jakarta:21.1` beside `dext5:1.0.1`): the vendor's own `-jakarta` artifactId is the
    verification — no pom declaration, no jar scan needed (the pom is silent and the jar may be large)."""
    from types import SimpleNamespace

    from sbmig.build.jakarta_discovery import _Verifier, artifact_names_jakarta

    assert artifact_names_jakarta("dext5-jakarta") and artifact_names_jakarta("grid_jakarta") and artifact_names_jakarta("x.jakarta")
    assert not artifact_names_jakarta("dext5") and not artifact_names_jakarta("jakartax") and not artifact_names_jakarta("")

    class NoLookup:                      # must never be consulted for a name-verified artifact
        def pom_text(self, *a, **k):
            raise AssertionError("pom_text called")

    cfg = SimpleNamespace(libraries=SimpleNamespace(max_jar_scans=2, max_jar_scan_mb=20))
    ok, how = _Verifier(NoLookup(), cfg).verify("com.example.up", "dext5-jakarta", "21.1", None)
    assert ok and how == "artifactId names jakarta"
