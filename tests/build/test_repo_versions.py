"""v2.1d dependency version policy: what the repository has for a library (sbmig.build.repo_versions).

No network: every lookup goes through an injected dict-backed fetch or a fake local repository under tmp_path."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest

from sbmig.build.repo_versions import (
    CENTRAL,
    RepositoryLookup,
    hint_lines,
    is_prerelease,
    mirror_from_settings,
    parse_version,
)
from sbmig.core.config import Config
from sbmig.core.jakarta_libraries import JakartaLibrary
from sbmig.core.models import RepositoryVersions, VersionCandidate

REPO = "https://nexus.example/repository/maven-public/"


@pytest.fixture(autouse=True)
def _no_user_settings(tmp_path, monkeypatch):
    """Never read the developer's ~/.m2/settings.xml (its mirror would win over Central)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))


def _cfg(tmp_path: Path, **build) -> Config:
    libraries = build.pop("libraries", {})
    build.setdefault("local_repo", str(tmp_path / "m2"))
    return Config.model_validate({"artifacts": {"parent": {"group_id": "g", "artifact_id": "a"}}, "build": build,
                                  "libraries": libraries})


# ---------------------------------------------------------------------------------------------------------------------
# versions
# ---------------------------------------------------------------------------------------------------------------------
def test_parse_version_ordering():
    ordered = ["1.0-alpha-1", "1.0-beta-2", "1.0-M1", "1.0-RC1", "1.0-SNAPSHOT", "1.0", "1.0-sp1", "1.0.1", "1.1",
               "1.10", "2.0.0-M1", "2.0.0", "2.5.0", "2.6.0-M1", "2.6.0", "3.3.0-RC1", "3.3.0", "3.3.1", "10.0"]
    shuffled = ordered[1::2] + ordered[::2]
    assert sorted(shuffled, key=parse_version) == ordered
    assert parse_version("1.0") == parse_version("1.0.0")
    assert parse_version("2.7.18.RELEASE") == parse_version("2.7.18")
    assert parse_version("5.6.15.Final") == parse_version("5.6.15")
    assert parse_version("junk") < parse_version("0.1") and parse_version("") < parse_version("0.1")
    assert parse_version("3.0-alpa-1") < parse_version("3.0")
    for weird in (None, "", "${x}", "..", "-", "1..2", "∞"):
        parse_version(weird)                                       # never raises


@pytest.mark.parametrize("v, pre", [("2.6.0", False), ("2.6.0-M1", True), ("2.6.0.M1", True), ("3.3.0-RC1", True),
                                    ("3.3.0.rc2", True), ("1.0-alpha-1", True), ("1.0-beta", True),
                                    ("1.0-SNAPSHOT", True), ("21-ea", True), ("1.0-preview", True), ("1.0.CR1", True),
                                    ("4.0.0-milestone-2", True), ("3.0-alpa-1", True), ("2.7.18.RELEASE", False),
                                    ("5.6.15.Final", False), ("33.0.0-jre", False), ("1.0-sp1", False),
                                    ("9.4.53.v20231009", False), ("RELEASE", True), ("20040616", False)])
def test_is_prerelease(v, pre):
    assert is_prerelease(v) is pre


# ---------------------------------------------------------------------------------------------------------------------
# settings.xml
# ---------------------------------------------------------------------------------------------------------------------
SETTINGS = """<?xml version="1.0" encoding="UTF-8"?>
<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0">
  <servers>
    <server><id>nexus</id><username>${env.NEXUS_USER}</username><password>${env.NEXUS_PASSWORD}</password></server>
    <server><id>enc</id><username>deploy</username><password>{COQLCE6DU6GtcS5P=}</password></server>
  </servers>
  <mirrors>
    <mirror><id>spring</id><mirrorOf>spring-milestones</mirrorOf><url>https://spring.example/m</url></mirror>
    <mirror><id>%s</id><mirrorOf>external:*,!local</mirrorOf><url>https://nexus.example/repository/maven-public</url></mirror>
  </mirrors>
</settings>
"""


def test_mirror_from_settings_catch_all_with_env_credentials(tmp_path, monkeypatch):
    f = tmp_path / "settings.xml"
    f.write_text(SETTINGS % "nexus", encoding="utf-8")
    monkeypatch.setenv("NEXUS_USER", "svc")
    monkeypatch.setenv("NEXUS_PASSWORD", "s3cret")
    assert mirror_from_settings(str(f)) == (REPO, ("svc", "s3cret"))
    monkeypatch.delenv("NEXUS_PASSWORD")
    assert mirror_from_settings(str(f)) == (REPO, None)             # an unset variable → no credentials


def test_mirror_from_settings_encrypted_password_and_first_mirror(tmp_path):
    f = tmp_path / "settings.xml"
    f.write_text(SETTINGS % "enc", encoding="utf-8")
    assert mirror_from_settings(str(f)) == (REPO, None)
    # no catch-all: the first mirror; no namespace
    g = tmp_path / "s2.xml"
    g.write_text("<settings><mirrors><mirror><id>a</id><mirrorOf>central</mirrorOf><url>http://a/</url></mirror>"
                 "</mirrors></settings>", encoding="utf-8")
    assert mirror_from_settings(str(g)) == ("http://a/", None)


def test_mirror_from_settings_none(tmp_path):
    f = tmp_path / "settings.xml"
    f.write_text("<settings><localRepository>/x</localRepository></settings>", encoding="utf-8")
    assert mirror_from_settings(str(f)) is None
    assert mirror_from_settings(str(tmp_path / "missing.xml")) is None
    (tmp_path / "bad.xml").write_text("<settings><mirrors>", encoding="utf-8")
    assert mirror_from_settings(str(tmp_path / "bad.xml")) is None
    assert mirror_from_settings("") is None


# ---------------------------------------------------------------------------------------------------------------------
# RepositoryLookup
# ---------------------------------------------------------------------------------------------------------------------
def _metadata(*versions: str) -> str:
    vs = "".join(f"<version>{v}</version>" for v in versions)
    return f"<metadata><groupId>x</groupId><versioning><versions>{vs}</versions></versioning></metadata>"


def _lib_pom(a: str, v: str, deps: str, props: str = "") -> str:
    return (f'<project xmlns="http://maven.apache.org/POM/4.0.0"><artifactId>{a}</artifactId><version>{v}</version>'
            f"<properties>{props}</properties><dependencies>{deps}</dependencies></project>")


JAVAX_SERVLET = ("<dependency><groupId>javax.servlet</groupId><artifactId>servlet-api</artifactId>"
                 "<version>2.5</version><scope>provided</scope></dependency>")
JAKARTA_SERVLET = ("<dependency><groupId>jakarta.servlet</groupId><artifactId>jakarta.servlet-api</artifactId>"
                   "<version>6.0.0</version><scope>provided</scope></dependency>")
OS = f"{REPO}opensymphony/sitemesh/"
ORG = f"{REPO}org/sitemesh/sitemesh/"
REPO_FILES = {
    f"{OS}maven-metadata.xml": _metadata("2.2.1", "2.4.2", "2.5.0", "2.6.0", "2.6.0-M1", "2.7.0-M1"),
    f"{OS}2.5.0/sitemesh-2.5.0.pom": _lib_pom("sitemesh", "2.5.0", JAVAX_SERVLET),
    f"{OS}2.6.0/sitemesh-2.6.0.pom": _lib_pom("sitemesh", "2.6.0", JAKARTA_SERVLET,
                                              "<maven.compiler.release>17</maven.compiler.release>"),
    f"{OS}2.7.0-M1/sitemesh-2.7.0-M1.pom": _lib_pom("sitemesh", "2.7.0-M1", JAKARTA_SERVLET),
    f"{ORG}maven-metadata.xml": _metadata("3.0.1", "3.1.0", "3.2.0", "3.2.1", "3.3.0"),
    f"{ORG}3.0.1/sitemesh-3.0.1.pom": _lib_pom("sitemesh", "3.0.1", JAVAX_SERVLET),
    f"{ORG}3.1.0/sitemesh-3.1.0.pom": _lib_pom("sitemesh", "3.1.0", JAVAX_SERVLET),
    f"{ORG}3.2.0/sitemesh-3.2.0.pom": _lib_pom("sitemesh", "3.2.0", JAKARTA_SERVLET),
    f"{ORG}3.2.1/sitemesh-3.2.1.pom": _lib_pom("sitemesh", "3.2.1", JAKARTA_SERVLET),
    f"{ORG}3.3.0/sitemesh-3.3.0.pom": _lib_pom("sitemesh", "3.3.0", JAKARTA_SERVLET,
                                               "<maven.compiler.target>1.8</maven.compiler.target>"),
}


class FakeFetch:
    def __init__(self, files: dict[str, str]):
        self.files = files
        self.calls: list[tuple[str, Optional[tuple[str, str]], float]] = []

    def __call__(self, url, credentials, timeout):
        self.calls.append((url, credentials, timeout))
        return self.files.get(url)


def test_same_major_candidates_with_pom_checks(tmp_path):
    fetch = FakeFetch(REPO_FILES)
    look = RepositoryLookup(_cfg(tmp_path, repository_url=REPO.rstrip("/"), repository_timeout_seconds=3), fetch=fetch)
    assert look.repository_url() == REPO
    assert look.versions("opensymphony", "sitemesh") == ["2.2.1", "2.4.2", "2.5.0", "2.6.0-M1", "2.6.0", "2.7.0-M1"]
    rv = look.candidates("opensymphony:sitemesh", "2.4.2")
    assert rv.error == "" and rv.source == f"{OS}maven-metadata.xml" and rv.major == 2
    assert [(c.version, c.jakarta, c.java) for c in rv.same_major] == [("2.5.0", False, None), ("2.6.0", True, 17)]
    assert rv.higher == [] and rv.first_jakarta == "2.6.0"
    assert all(t == 3.0 for _u, _c, t in fetch.calls)
    # cached: the same question asks the repository nothing new
    n = len(fetch.calls)
    look.candidates("opensymphony:sitemesh", "2.4.2")
    assert len(fetch.calls) == n


def test_prereleases_only_when_configured(tmp_path):
    look = RepositoryLookup(_cfg(tmp_path, repository_url=REPO, libraries={"include_prerelease": True}),
                            fetch=FakeFetch(REPO_FILES))
    rv = look.candidates("opensymphony:sitemesh", "2.4.2")
    assert [c.version for c in rv.same_major] == ["2.5.0", "2.6.0-M1", "2.6.0", "2.7.0-M1"]
    assert rv.same_major[-1].prerelease and rv.same_major[-1].jakarta is True


def test_higher_majors_and_first_jakarta(tmp_path):
    look = RepositoryLookup(_cfg(tmp_path, repository_url=REPO), fetch=FakeFetch(REPO_FILES))
    rv = look.candidates("org.sitemesh:sitemesh", "2.4.2")
    assert rv.same_major == []
    assert [(c.version, c.jakarta, c.java) for c in rv.higher] == [("3.3.0", True, 8)]
    assert rv.first_jakarta == "3.2.0"                              # walked down 3.x until the javax 3.1.0
    # max_pom_checks caps the poms read
    capped = RepositoryLookup(_cfg(tmp_path, repository_url=REPO, libraries={"max_pom_checks": 1}),
                              fetch=FakeFetch(REPO_FILES)).candidates("org.sitemesh:sitemesh", "2.4.2")
    assert capped.higher[0].jakarta is True and capped.first_jakarta == "3.3.0"
    # unknown current → the newest of every major
    rv = look.candidates("org.sitemesh:sitemesh", None)
    assert rv.same_major == [] and [c.version for c in rv.higher] == ["3.3.0"] and rv.major is None
    rv = look.candidates("org.sitemesh:sitemesh", "${sitemesh.version}")
    assert rv.current == "${sitemesh.version}" and [c.version for c in rv.higher] == ["3.3.0"]


def test_metadata_404_falls_back_to_the_local_repository(tmp_path):
    cfg = _cfg(tmp_path, repository_url=REPO)
    d = tmp_path / "m2" / "opensymphony" / "sitemesh"
    for v, deps in (("2.4.2", JAVAX_SERVLET), ("2.6.0", JAKARTA_SERVLET)):
        (d / v).mkdir(parents=True)
        (d / v / f"sitemesh-{v}.pom").write_text(_lib_pom("sitemesh", v, deps), encoding="utf-8")
    (d / "2.5.0").mkdir()                                             # a dir without a pom does not count
    (d / "maven-metadata-central.xml").write_text(_metadata("2.4.2", "2.5.1"), encoding="utf-8")
    look = RepositoryLookup(cfg, fetch=FakeFetch({}))
    rv = look.candidates("opensymphony:sitemesh", "2.4.2")
    assert rv.error == "" and rv.source == str(d)
    assert [(c.version, c.jakarta) for c in rv.same_major] == [("2.5.1", None), ("2.6.0", True)]
    assert look.pom_text("opensymphony", "sitemesh", "2.6.0") is not None
    # nothing anywhere → the reason
    rv = look.candidates("org.nothing:here", "1.0")
    assert rv.same_major == [] and rv.higher == []
    assert rv.error == f"maven-metadata.xml not found at {REPO}org/nothing/here/maven-metadata.xml"


def test_a_failing_fetch_never_raises_and_a_dead_repository_is_skipped(tmp_path):
    calls: list[str] = []

    def boom(url, credentials, timeout):
        calls.append(url)
        raise TimeoutError("timed out")

    look = RepositoryLookup(_cfg(tmp_path, repository_url=REPO), fetch=boom)
    rv = look.candidates("a:b", "1.0")
    assert rv.error == f"timeout reading {REPO}a/b/maven-metadata.xml" and rv.same_major == []
    look.candidates("c:d", "1.0")
    assert len(calls) == 2
    rv = look.candidates("e:f", "1.0")                                # two timeouts in a row: not asked again
    assert len(calls) == 2 and rv.error == f"{REPO} did not answer (timeout); skipped for the rest of the run"
    assert hint_lines(rv)[0].startswith(f"e:f — the repository could not be read ({REPO} did not answer (timeout)")
    assert RepositoryLookup(_cfg(tmp_path), fetch=FakeFetch({})).candidates("not-a-key", "1").error


def test_offline_without_mirror_has_no_url(tmp_path):
    look = RepositoryLookup(_cfg(tmp_path, offline=True), fetch=FakeFetch(REPO_FILES))
    assert look.repository_url() is None
    rv = look.candidates("opensymphony:sitemesh", "2.4.2")
    assert rv.error == "no repository URL (offline)"


def test_repository_url_precedence(tmp_path, monkeypatch):
    settings = tmp_path / "settings.xml"
    settings.write_text(SETTINGS % "nexus", encoding="utf-8")
    monkeypatch.setenv("NEXUS_USER", "svc")
    monkeypatch.setenv("NEXUS_PASSWORD", "pw")
    # configured URL wins (and borrows the mirror's credentials when it is the same URL)
    fetch = FakeFetch(REPO_FILES)
    look = RepositoryLookup(_cfg(tmp_path, repository_url="https://other.example/m2",
                                 settings_file=str(settings)), fetch=fetch)
    assert look.repository_url() == "https://other.example/m2/"
    same = RepositoryLookup(_cfg(tmp_path, repository_url=REPO, settings_file=str(settings)), fetch=fetch)
    same.versions("opensymphony", "sitemesh")
    assert fetch.calls[-1][1] == ("svc", "pw")
    # then the settings mirror (with its credentials)
    look = RepositoryLookup(_cfg(tmp_path, settings_file=str(settings)), fetch=fetch)
    assert look.repository_url() == REPO
    # then Central, unless offline
    assert RepositoryLookup(_cfg(tmp_path), fetch=fetch).repository_url() == CENTRAL
    assert RepositoryLookup(_cfg(tmp_path, offline=True), fetch=fetch).repository_url() is None
    # an offline build with a mirror still reads the mirror
    assert RepositoryLookup(_cfg(tmp_path, offline=True, settings_file=str(settings)),
                            fetch=fetch).repository_url() == REPO


def test_user_settings_mirror_is_used_when_no_settings_file(tmp_path):
    m2 = Path.home() / ".m2"
    m2.mkdir()
    (m2 / "settings.xml").write_text(SETTINGS % "x", encoding="utf-8")
    assert RepositoryLookup(_cfg(tmp_path), fetch=FakeFetch({})).repository_url() == REPO


def test_missing_libraries_config_does_not_crash(tmp_path):
    class Build:
        repository_url = REPO
        local_repo = str(tmp_path / "m2")
        settings_file = None
        offline = False

    class Cfg:
        build = Build()

    rv = RepositoryLookup(Cfg(), fetch=FakeFetch(REPO_FILES)).candidates("opensymphony:sitemesh", "2.4.2")
    assert [c.version for c in rv.same_major] == ["2.5.0", "2.6.0"]


# ---------------------------------------------------------------------------------------------------------------------
# hint_lines
# ---------------------------------------------------------------------------------------------------------------------
def test_hint_lines_text(tmp_path):
    look = RepositoryLookup(_cfg(tmp_path, repository_url=REPO), fetch=FakeFetch(REPO_FILES))
    entry = JakartaLibrary(key="opensymphony:sitemesh", jakarta_since="2.6.0", version="2.6.0", requires_java=17,
                           note="SiteMesh 2.6.0 is the jakarta build. More text.", source="company")
    lines = hint_lines(look.candidates("opensymphony:sitemesh", "2.4.2"), entry)
    assert lines[0].startswith("opensymphony:sitemesh: approved — raise to 2.6.0 (jakarta since 2.6.0, requires Java 17")
    assert "source: company table" in lines[0] and "More text" not in lines[0]
    assert lines[1] == ("opensymphony:sitemesh — current 2.4.2; same major (2.x) in the repository, newer than "
                        "current: 2.5.0 (javax), 2.6.0 (jakarta ✓, Java 17)")
    assert len(lines) == 2
    lines = hint_lines(look.candidates("org.sitemesh:sitemesh", "2.4.2"))
    assert lines == ["org.sitemesh:sitemesh — current 2.4.2; no newer release on the 2.x line in the repository",
                     ("org.sitemesh:sitemesh — higher majors: 3.x: 3.3.0 (jakarta ✓, Java 8; first jakarta release "
                      "3.2.0)")]


def test_hint_lines_error_cap_and_unknown_current():
    err = RepositoryVersions(key="a:b", current="1.0", major=1, error="timeout after 10s reading https://x/")
    assert hint_lines(err) == [("a:b — the repository could not be read (timeout after 10s reading https://x/); "
                                "stay on the same major and ask if unsure")]
    many = RepositoryVersions(key="a:b", current="1.0", major=1, source="https://x/maven-metadata.xml",
                              same_major=[VersionCandidate(version=f"1.{i}") for i in range(1, 10)])
    line = hint_lines(many)[0]
    assert "… (+3), 1.4, 1.5, 1.6, 1.7, 1.8, 1.9" in line
    local = RepositoryVersions(key="a:b", current=None, source="/home/u/.m2/repository/a/b",
                               higher=[VersionCandidate(version="1.9"), VersionCandidate(version="2.1", jakarta=True)])
    assert hint_lines(local) == [("a:b — current version unknown; newest release per major in the repository "
                                  "(local repository only): 1.x: 1.9; 2.x: 2.1 (jakarta ✓)")]
    assert hint_lines(None) == []                                     # never raises
