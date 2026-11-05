"""v2.1d dependency version policy: what the repository has for a library (sbmig.build.repo_versions).

No network: every lookup goes through an injected dict-backed fetch or a fake local repository under tmp_path."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest

from sbmig.build.repo_versions import (
    is_blocking_mirror,
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
    monkeypatch.delenv("MAVEN_HOME", raising=False)
    monkeypatch.delenv("M2_HOME", raising=False)
    monkeypatch.setattr("sbmig.build.repo_versions.global_settings_file", lambda *_a, **_k: None)   # v2.1z-e: no global settings


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
    # v2.1z-e: every URL of the repository set is named (the configured one first, then Central)
    assert rv.error.startswith(f"maven-metadata.xml not found at {REPO}org/nothing/here/maven-metadata.xml, ")
    assert rv.urls_tried[0] == f"{REPO} → not found" and rv.urls_tried[1].startswith(f"{CENTRAL} → not found")


def test_a_failing_fetch_never_raises_and_a_dead_repository_is_skipped(tmp_path):
    calls: list[str] = []

    def boom(url, credentials, timeout):
        calls.append(url)
        raise TimeoutError("timed out")

    # v2.1z-e: the lookup asks a repository SET; `offline` keeps Central out so the set is the configured URL alone
    look = RepositoryLookup(_cfg(tmp_path, repository_url=REPO, offline=True), fetch=boom)
    assert [u for u, _c, _o in look.repository_urls()] == [REPO]
    rv = look.candidates("a:b", "1.0")
    assert rv.error == f"remote repositories unreadable: {REPO} timeout" and rv.same_major == []
    assert rv.urls_tried[0] == f"{REPO} → timeout" and rv.urls_tried[-1].endswith("→ nothing")
    look.candidates("c:d", "1.0")
    assert len(calls) == 2
    rv = look.candidates("e:f", "1.0")                                # two timeouts in a row: not asked again
    assert len(calls) == 2 and rv.error == f"remote repositories unreadable: {REPO} skipped: did not answer earlier in this run"
    assert look.lookup_trace("e", "f")[0] == (REPO, "skipped: did not answer earlier in this run")
    assert hint_lines(rv)[0].startswith(f"e:f — the repository could not be read (remote repositories unreadable: {REPO} skipped")
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
    # an explicit configured URL wins (the operator's decision), the settings-derived set follows; one WARNING names both
    fetch = FakeFetch(REPO_FILES)
    look = RepositoryLookup(_cfg(tmp_path, repository_url="https://other.example/m2",
                                 settings_file=str(settings)), fetch=fetch)
    assert look.repository_url() == "https://other.example/m2/"
    assert [u for u, _c, _o in look.repository_urls()] == ["https://other.example/m2/", REPO]   # external:* covers Central
    assert any(n.startswith(f"Maven itself resolves from {REPO} (settings); sbmig's lookups use build.repository_url "
                            "https://other.example/m2/") for n in look.url_notes)
    assert look.settings_files == [f"{settings} (build.settings_file)"]
    # the same URL as the mirror borrows the mirror's credentials; a URL nowhere in the settings takes repository_server_id
    same = RepositoryLookup(_cfg(tmp_path, repository_url=REPO, settings_file=str(settings)), fetch=fetch)
    same.versions("opensymphony", "sitemesh")
    assert fetch.calls[-1][1] == ("svc", "pw") and same.url_notes == []
    by_id = RepositoryLookup(_cfg(tmp_path, repository_url="https://other.example/m2", settings_file=str(settings),
                                  repository_server_id="nexus"), fetch=fetch)
    assert by_id.repository_urls()[0][1] == ("svc", "pw") and "credentials from server nexus" in by_id.repository_urls()[0][2]
    # the settings mirror alone (with its credentials)
    look = RepositoryLookup(_cfg(tmp_path, settings_file=str(settings)), fetch=fetch)
    assert look.repository_url() == REPO and look.repository_urls()[0][1] == ("svc", "pw")
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
    assert lines[2] == ("opensymphony:sitemesh → verdict: raise to 2.6.0 (same major 2.x, jakarta build) — no major "
                        "change, no <verify> needed")
    assert len(lines) == 3
    lines = hint_lines(look.candidates("org.sitemesh:sitemesh", "2.4.2"))
    assert lines[:2] == ["org.sitemesh:sitemesh — current 2.4.2; no newer release on the 2.x line in the repository",
                         ("org.sitemesh:sitemesh — higher majors: 3.x: 3.3.0 (jakarta ✓, Java 8; first jakarta "
                          "release 3.2.0)")]
    assert lines[2].startswith("org.sitemesh:sitemesh → verdict: the 2.x line has NO jakarta build, so a major upgrade "
                               "to 3.3.0 is allowed here (rule 14: the newest release of that major; first jakarta "
                               "release 3.2.0)") and len(lines) == 3


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


# ---------------------------------------------------------------------------------------------------------------------
# the rule-14 verdict line (2026-10-27: "qlrm 같은 경우는 메이저로 올라가야 하지 않아?")
# ---------------------------------------------------------------------------------------------------------------------
def _rv(key: str, current: str, same=(), higher=(), first_jakarta=None, major=None):
    from sbmig.core.models import RepositoryVersions, VersionCandidate
    from sbmig.build.depchanges import major_of
    return RepositoryVersions(key=key, current=current, major=major if major is not None else major_of(current),
                              same_major=[VersionCandidate(version=v, jakarta=j) for v, j in same],
                              higher=[VersionCandidate(version=v, jakarta=j) for v, j in higher],
                              first_jakarta=first_jakarta, source="https://repo/")


def _entry(key: str, version: str, **kw):
    from sbmig.core.jakarta_libraries import JakartaLibrary
    return JakartaLibrary(key=key, jakarta_since=kw.pop("jakarta_since", version), version=version, **kw)


def test_verdict_same_major_jakarta_build():
    rv = _rv("opensymphony:sitemesh", "2.4.2", same=[("2.5.0", False), ("2.6.0", True)], higher=[("3.3.0", True)])
    assert hint_lines(rv)[-1] == ("opensymphony:sitemesh → verdict: raise to 2.6.0 (same major 2.x, jakarta build) — "
                                  "no major change, no <verify> needed")
    # the table pins a same-major release the repository did not pom-check
    rv2 = _rv("opensymphony:sitemesh", "2.4.2", same=[("2.5.0", None), ("2.6.0", None)])
    assert hint_lines(rv2, _entry("opensymphony:sitemesh", "2.6.0"))[-1].startswith(
        "opensymphony:sitemesh → verdict: raise to 2.6.0 (same major 2.x, jakarta build)")


def test_verdict_major_upgrade_allowed_when_the_line_has_no_jakarta_build():
    entry = _entry("org.qlrm:qlrm", "4.2.1", old_keys=["ch.simas.qlrm:qlrm"], jakarta_since="4.0.1")
    rv = _rv("ch.simas.qlrm:qlrm", "1.7.1", same=[], higher=[("2.1.1", None), ("3.0.4", None), ("4.2.1", None)])
    line = hint_lines(rv, entry)[-1]
    assert line.startswith("ch.simas.qlrm:qlrm → verdict: the 1.x line has NO jakarta build, so a major upgrade to "
                           "4.2.1 is allowed here (rule 14: the newest release of that major; jakarta since 4.0.1)")
    assert "write <verify>" in line and "listed for the operator" in line
    # no table entry: the repository's own jakarta ✓ on a higher major decides
    rv2 = _rv("g:a", "1.2", same=[("1.3", False)], higher=[("2.0", False), ("3.1", True)], first_jakarta="3.0")
    line = hint_lines(rv2)[-1]
    assert "a major upgrade to 3.1 is allowed here" in line and "first jakarta release 3.0" in line


def test_verdict_allow_major_nothing_known_and_no_facts():
    rv = _rv("org.sitemesh:sitemesh", "2.4.2", same=[], higher=[("3.3.0", True)])
    assert hint_lines(rv, _entry("org.sitemesh:sitemesh", "3.3.0", allow_major=True, source="company"))[-1] == (
        "org.sitemesh:sitemesh → verdict: a person approved the move to 3.3.0 (allow_major) — use it as given and "
        "write <verify>")
    rv = _rv("g:a", "1.2", same=[("1.3", False)], higher=[("2.0", False)])
    assert hint_lines(rv)[-1] == ("g:a → verdict: no jakarta build is known on any line — the API changed inside the "
                                  "same major: fix the code against the version the parent chain / Boot BOM manages "
                                  "and leave the version")
    from sbmig.core.models import RepositoryVersions
    assert not any("verdict" in ln for ln in hint_lines(RepositoryVersions(key="g:a", current=None, higher=[])))
    err = RepositoryVersions(key="g:a", current="1.2", major=1, error="timeout")
    assert not any("verdict" in ln for ln in hint_lines(err))


def test_verdict_unknown_jakarta_status_is_never_a_refusal():
    """v2.1v (B-H1 / E-H1): a jakarta-native library whose poms declare no `jakarta.*` dependency (Spring itself —
    `pom_is_jakarta` says unknown) used to get "answer <no_script>" in 22 API-removal prompts of one e2e run; the last
    branch now says the API changed inside the major and the managed version stays."""
    from sbmig.build.repo_versions import _verdict_line
    rv = _rv("org.springframework:spring-webmvc", "7.0.8", same=[("7.0.9", None), ("7.0.10", None)])
    line = _verdict_line(rv)
    assert line == ("org.springframework:spring-webmvc → verdict: no jakarta build is known on any line — the API "
                    "changed inside the same major: fix the code against the version the parent chain / Boot BOM "
                    "manages and leave the version")
    assert "<no_script>" not in line and "<analysis>" not in line
    # the other branches are unchanged: a same-major jakarta build still raises, nothing listed still says nothing
    assert "raise to 7.0.10" in _verdict_line(_rv("g:a", "7.0.8", same=[("7.0.10", True)]))
    assert _verdict_line(_rv("g:a", "7.0.8")) == ""


# ---------------------------------------------------------------------------------------------------------------------
# v2.1z-e: the repository set by Maven's rules
# ---------------------------------------------------------------------------------------------------------------------
POM_WITH_REPO = """<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>
<groupId>g</groupId><artifactId>app</artifactId><version>1</version>
<properties><vendor.repo>https://vendor.example/maven</vendor.repo></properties>
<repositories><repository><id>vendor</id><url>${vendor.repo}</url></repository>
<repository><id>broken</id><url>${nope}</url></repository></repositories></project>
"""


def test_a_catch_all_mirror_hides_the_pom_repositories(tmp_path):
    settings = tmp_path / "settings.xml"
    settings.write_text(SETTINGS % "nexus", encoding="utf-8")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pom.xml").write_text(POM_WITH_REPO, encoding="utf-8")
    look = RepositoryLookup(_cfg(tmp_path, settings_file=str(settings)), fetch=FakeFetch({}), project_root=proj)
    # external:* covers the vendor repository and Central: only the mirror is asked, in Maven's way
    assert [u for u, _c, _o in look.repository_urls()] == [REPO]
    assert "covering vendor" in look.repository_urls()[0][2] or "covering central" in look.repository_urls()[0][2]
    # without a mirror the pom repository is asked after the settings' own, then Central; the unresolved one is skipped
    plain = RepositoryLookup(_cfg(tmp_path), fetch=FakeFetch({}), project_root=proj)
    assert [u for u, _c, _o in plain.repository_urls()] == ["https://vendor.example/maven/", CENTRAL]
    assert plain.repository_urls()[0][2] == "pom pom.xml repository vendor"


def test_explicit_mirror_ids_and_exclusions(tmp_path):
    settings = tmp_path / "settings.xml"
    settings.write_text(SETTINGS % "nexus", encoding="utf-8")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pom.xml").write_text(POM_WITH_REPO.replace("<id>vendor</id>", "<id>local</id>")
                                  .replace("https://vendor.example/maven", "http://localhost:8081/m"), encoding="utf-8")
    look = RepositoryLookup(_cfg(tmp_path, settings_file=str(settings)), fetch=FakeFetch({}), project_root=proj)
    # `external:*,!local`: the localhost repository is neither external nor allowed by id → asked directly
    assert [u for u, _c, _o in look.repository_urls()] == ["http://localhost:8081/m/", REPO]
    from sbmig.build.repo_versions import mirror_covers
    assert mirror_covers("*", "x", "https://x") and mirror_covers("central", "central", CENTRAL)
    assert not mirror_covers("external:*", "local", "file:///tmp/repo") and mirror_covers("external:*", "v", "https://v")
    assert not mirror_covers("external:*,!vendor", "vendor", "https://v") and mirror_covers("external:http:*", "h", "http://h")
    assert not mirror_covers("external:http:*", "h", "https://h")


def test_the_global_settings_are_read_when_the_user_has_none(tmp_path, monkeypatch):
    # no user settings, no mirror anywhere: build.repository_url first, then Central
    look = RepositoryLookup(_cfg(tmp_path, repository_url=REPO), fetch=FakeFetch({}))
    assert [u for u, _c, _o in look.repository_urls()] == [REPO, CENTRAL]
    assert look.repository_urls()[0][2].startswith("build.repository_url (explicit")
    # a global $MAVEN_HOME/conf/settings.xml with a mirror is read when the user has none; a differing yml URL is warned about
    home = tmp_path / "maven-home"
    (home / "conf").mkdir(parents=True)
    (home / "conf" / "settings.xml").write_text(SETTINGS % "global-nexus", encoding="utf-8")
    monkeypatch.setenv("MAVEN_HOME", str(home))
    from sbmig.build import repo_versions as rv_mod
    monkeypatch.setattr(rv_mod, "global_settings_file", rv_mod.global_settings_file.__wrapped__
                        if hasattr(rv_mod.global_settings_file, "__wrapped__") else _real_global_settings_file)
    look = RepositoryLookup(_cfg(tmp_path, repository_url="https://stale.example/m2"), fetch=FakeFetch({}))
    assert [u for u, _c, _o in look.repository_urls()] == ["https://stale.example/m2/", REPO]
    assert look.settings_files == [f"{home / 'conf' / 'settings.xml'} (global settings)"]
    assert any(n.startswith(f"Maven itself resolves from {REPO} (settings); sbmig's lookups use build.repository_url "
                            "https://stale.example/m2/") for n in look.url_notes)
    assert [u for u, _c, _o in RepositoryLookup(_cfg(tmp_path), fetch=FakeFetch({})).repository_urls()] == [REPO]


def _real_global_settings_file(cfg=None):
    """The module's real `global_settings_file` (the autouse fixture replaces it with a no-op)."""
    return _GLOBAL_SETTINGS_REAL(cfg)


def test_the_global_settings_behind_the_configured_mvn_path_are_read(tmp_path, monkeypatch):
    """2026-11-05 (company PC): Maven is not on PATH — `build.mvn` holds the full path of `bin/mvn.cmd`; the Nexus mirror lives
    in that installation's conf/settings.xml, so the lookup must find it through build.mvn, not only through PATH / MAVEN_HOME."""
    home = tmp_path / "tools" / "apache-maven-3.9.9"
    (home / "bin").mkdir(parents=True)
    (home / "conf").mkdir()
    exe = home / "bin" / "mvn.cmd"
    exe.write_text("@echo off\n", encoding="utf-8")
    (home / "conf" / "settings.xml").write_text(SETTINGS % "company-nexus", encoding="utf-8")
    monkeypatch.delenv("MAVEN_HOME", raising=False)
    monkeypatch.delenv("M2_HOME", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    from sbmig.build import repo_versions as rv_mod
    monkeypatch.setattr(rv_mod, "global_settings_file", _real_global_settings_file)
    look = RepositoryLookup(_cfg(tmp_path, mvn=str(exe)), fetch=FakeFetch({}))
    assert [u for u, _c, _o in look.repository_urls()] == [REPO]
    assert look.settings_files == [f"{home / 'conf' / 'settings.xml'} (global settings)"]
    assert look.settings_missing == [f"{Path.home() / '.m2' / 'settings.xml'} (~/.m2/settings.xml): missing"]
    assert look.repository_urls()[0][2].startswith("settings mirror 'company-nexus'")
    # a bare `mvn` that PATH does not know: no global settings, Central only — as before
    look2 = RepositoryLookup(_cfg(tmp_path, mvn="mvn"), fetch=FakeFetch({}))
    assert [u for u, _c, _o in look2.repository_urls()] == [CENTRAL]


import sbmig.build.repo_versions as _rv_module  # noqa: E402
_GLOBAL_SETTINGS_REAL = _rv_module.global_settings_file



def test_an_unreadable_settings_file_is_named_and_warned(tmp_path, caplog):
    """A settings.xml that does not parse (a stray byte, an unclosed tag) must not drop the Nexus silently: a WARNING names the
    file and the lookup lists it as unreadable."""
    import logging
    bad = tmp_path / "settings.xml"
    bad.write_text("<settings><mirrors><mirror><id>nexus</id><url>https://nexus.example/</url>", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="sbmig.build.repo_versions"):
        look = RepositoryLookup(_cfg(tmp_path, settings_file=str(bad)), fetch=FakeFetch({}))
        urls = [u for u, _c, _o in look.repository_urls()]
    assert urls == [CENTRAL] and look.settings_files == []
    assert look.settings_missing == [f"{bad} (build.settings_file): unreadable (see the WARNING above)"]
    assert any(f"settings {bad} could not be read" in r.getMessage() and "build.repository_url" in r.getMessage()
               for r in caplog.records)




GLOBAL_WITH_BLOCKER = """<?xml version="1.0" encoding="UTF-8"?>
<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0">
  <mirrors>
    <mirror>
      <id>maven-default-http-blocker</id>
      <mirrorOf>external:http:*</mirrorOf>
      <name>Pseudo repository to mirror external repositories initially using HTTP.</name>
      <url>http://0.0.0.0/</url>
      <blocked>true</blocked>
    </mirror>
  </mirrors>
</settings>
"""
USER_WITH_HTTP_NEXUS = """<?xml version="1.0" encoding="UTF-8"?>
<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0">
  <profiles><profile><id>company</id><repositories>
    <repository><id>xxx-internal-releases</id><url>http://nexus.company.local/repository/releases</url></repository>
  </repositories></profile></profiles>
  <activeProfiles><activeProfile>company</activeProfile></activeProfiles>
</settings>
"""


def test_maven_default_http_blocker_is_not_asked_and_is_explained(tmp_path, monkeypatch, caplog):
    """The company PC (2026-11-05): the user settings' active-profile repository is the Nexus over plain http; Maven 3.9's global
    conf/settings.xml ships `maven-default-http-blocker` (mirrorOf external:http:*, http://0.0.0.0/) which covers it — Maven refuses
    the repository, and so does sbmig: it is not asked (the probe had shown `http://0.0.0.0/ → unreachable (WinError 10049)`), a
    note says why, Central stays; an explicit build.repository_url is asked first regardless."""
    home = tmp_path / "maven-home"
    (home / "conf").mkdir(parents=True)
    (home / "conf" / "settings.xml").write_text(GLOBAL_WITH_BLOCKER, encoding="utf-8")
    user = tmp_path / "user-settings.xml"
    user.write_text(USER_WITH_HTTP_NEXUS, encoding="utf-8")
    monkeypatch.setenv("MAVEN_HOME", str(home))
    from sbmig.build import repo_versions as rv_mod
    monkeypatch.setattr(rv_mod, "global_settings_file", _real_global_settings_file)
    look = RepositoryLookup(_cfg(tmp_path, settings_file=str(user)), fetch=FakeFetch({}))
    urls = [u for u, _c, _o in look.repository_urls()]
    assert urls == [CENTRAL] and not any("0.0.0.0" in u for u in urls)
    assert any(n.startswith("xxx-internal-releases (http://nexus.company.local/repository/releases/) is blocked by Maven's HTTP "
                            "blocker (mirror 'maven-default-http-blocker', mirrorOf external:http:*, global settings)")
               and "build.repository_url" in n for n in look.url_notes)
    # the operator's explicit URL is first and never mirrored
    look2 = RepositoryLookup(_cfg(tmp_path, settings_file=str(user),
                                  repository_url="http://nexus.company.local/repository/maven-public"), fetch=FakeFetch({}))
    assert [u for u, _c, _o in look2.repository_urls()] == ["http://nexus.company.local/repository/maven-public/", CENTRAL]
    # a user settings mirror that overrides the blocker (same mirrorOf, the real URL, not blocked) wins: user mirrors come first
    user.write_text(USER_WITH_HTTP_NEXUS.replace("<activeProfiles>",
                    "<mirrors><mirror><id>maven-default-http-blocker</id><mirrorOf>external:http:*</mirrorOf>"
                    "<url>http://nexus.company.local/repository/maven-public</url><blocked>false</blocked></mirror></mirrors>"
                    "<activeProfiles>"), encoding="utf-8")
    look3 = RepositoryLookup(_cfg(tmp_path, settings_file=str(user)), fetch=FakeFetch({}))
    assert [u for u, _c, _o in look3.repository_urls()][0] == "http://nexus.company.local/repository/maven-public/"
    # a failure while building the set is said on screen (note:) and logged as WARNING — never a silent Central-only list
    def boom(_cfg=None):
        raise RuntimeError("settings exploded")
    monkeypatch.setattr(rv_mod, "global_settings_file", boom)
    import logging as _logging
    with caplog.at_level(_logging.WARNING, logger=rv_mod.log.name):
        look_fail = RepositoryLookup(_cfg(tmp_path, settings_file=str(user), repository_url="http://nexus.company.local/x"),
                                     fetch=FakeFetch({}))
        look_fail.repository_urls()
    assert any(n.startswith("repository set incomplete: building it failed midway (RuntimeError: settings exploded)")
               for n in look_fail.url_notes)
    assert any("repository set incomplete" in r.getMessage() and r.levelno == _logging.WARNING for r in caplog.records)
    monkeypatch.setattr(rv_mod, "global_settings_file", _real_global_settings_file)
    # build.repository_server_id beats the URL heuristic — two settings repositories on ONE URL with different logins (review
    # 2026-11-05: the first URL match won while the reason named the configured server)
    two = USER_WITH_HTTP_NEXUS.replace("<profiles>",
        "<servers><server><id>xxx-internal-releases</id><username>ua</username><password>pa</password></server>"
        "<server><id>xxx-snapshots</id><username>ub</username><password>pb</password></server></servers><profiles>").replace(
        "</repositories>",
        "<repository><id>xxx-snapshots</id><url>https://nexus.company.local/repository/group</url></repository></repositories>").replace(
        "<url>http://nexus.company.local/repository/releases</url>", "<url>https://nexus.company.local/repository/group</url>")
    user.write_text(two, encoding="utf-8")
    cfg_sid = _cfg(tmp_path, settings_file=str(user), repository_url="https://nexus.company.local/repository/group")
    cfg_sid.build.repository_server_id = "xxx-snapshots"
    first = RepositoryLookup(cfg_sid, fetch=FakeFetch({})).repository_urls()[0]
    assert first[1] == ("ub", "pb") and first[2].endswith("; credentials from server xxx-snapshots")
    cfg_url = _cfg(tmp_path, settings_file=str(user), repository_url="https://nexus.company.local/repository/group")
    first = RepositoryLookup(cfg_url, fetch=FakeFetch({})).repository_urls()[0]
    assert first[1] == ("ua", "pa") and first[2].endswith("; credentials from the settings entry with the same URL")
    user.write_text(USER_WITH_HTTP_NEXUS, encoding="utf-8")
    # the blocked repository's <server> login still reaches the explicit URL that points at the same Nexus
    user.write_text(USER_WITH_HTTP_NEXUS.replace("<profiles>",
                    "<servers><server><id>xxx-internal-releases</id><username>nx</username><password>pw</password></server>"
                    "</servers><profiles>"), encoding="utf-8")
    look4 = RepositoryLookup(_cfg(tmp_path, settings_file=str(user),
                                  repository_url="http://nexus.company.local/repository/releases"), fetch=FakeFetch({}))
    first = look4.repository_urls()[0]
    assert first[0] == "http://nexus.company.local/repository/releases/" and first[1] == ("nx", "pw")
    assert first[2].startswith("build.repository_url (explicit: first)")
    assert is_blocking_mirror("http://0.0.0.0/") and is_blocking_mirror("https://ok/", True)
    assert not is_blocking_mirror("http://nexus.company.local/") and not is_blocking_mirror("")

