"""v2.1d dependency version policy: the pom read-back guard (sbmig.build.depchanges, docs/INTERFACES.md "v2.1d").

Company run 2026-10-27: the LLM replaced SiteMesh 2 (opensymphony:sitemesh 2.4.2) with org.sitemesh:sitemesh 3.2.1;
sbmig must notice such a move from the pom diff alone, mark what a person's table allows and tell the operator."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from sbmig.build.depchanges import (
    apply_table,
    changes_in_commit,
    dependency_changes,
    major_of,
    still_applied,
    synthesized_guide,
)
from sbmig.core.jakarta_libraries import JakartaLibrary
from sbmig.core.models import DependencyChange


def _pom(deps: str = "", *, props: str = "", parent: str = "", build: str = "", managed: str = "",
         version: str = "1.0.0") -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  {parent}
  <groupId>com.example</groupId>
  <artifactId>xxx-back</artifactId>
  <version>{version}</version>
  <properties>{props}</properties>
  <dependencyManagement><dependencies>{managed}</dependencies></dependencyManagement>
  <dependencies>{deps}</dependencies>
  <build>{build}</build>
</project>
"""


def _dep(g: str, a: str, v: str | None = None, *, classifier: str | None = None, scope: str | None = None) -> str:
    out = f"<dependency><groupId>{g}</groupId><artifactId>{a}</artifactId>"
    if v is not None:
        out += f"<version>{v}</version>"
    if classifier:
        out += f"<classifier>{classifier}</classifier>"
    if scope:
        out += f"<scope>{scope}</scope>"
    return out + "</dependency>"


def _one(changes: list[DependencyChange]) -> DependencyChange:
    assert len(changes) == 1, [c.text for c in changes]
    return changes[0]


# ---------------------------------------------------------------------------------------------------------------------
# major_of
# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("v, major", [("2.4.2", 2), ("3.3.0-RC1", 3), ("${sitemesh.version}", None), ("", None),
                                      (None, None), ("v2.1", 2), ("RELEASE", None), ("LATEST", None), ("10", 10),
                                      ("1.0.0.RELEASE", 1)])
def test_major_of(v, major):
    assert major_of(v) == major


# ---------------------------------------------------------------------------------------------------------------------
# dependency_changes
# ---------------------------------------------------------------------------------------------------------------------
def test_minor_bump():
    c = _one(dependency_changes(_pom(_dep("org.qlrm", "qlrm", "4.0.1")), _pom(_dep("org.qlrm", "qlrm", "4.2.1")),
                                "xxx-back/pom.xml"))
    assert (c.kind, c.key, c.before, c.after, c.section, c.path) == (
        "minor", "org.qlrm:qlrm", "4.0.1", "4.2.1", "dependencies", "xxx-back/pom.xml")
    assert not c.needs_operator


def test_major_bump():
    c = _one(dependency_changes(_pom(_dep("org.sitemesh", "sitemesh", "2.4.2")),
                                _pom(_dep("org.sitemesh", "sitemesh", "3.2.1")), "pom.xml"))
    assert c.kind == "major" and c.needs_operator and c.text == "org.sitemesh:sitemesh 2.4.2 → 3.2.1"


def test_property_resolved_version():
    """The dependency line is unchanged; only <sitemesh.version> moved — still a major change."""
    dep = _dep("org.sitemesh", "sitemesh", "${sitemesh.version}")
    c = _one(dependency_changes(_pom(dep, props="<sitemesh.version>2.4.2</sitemesh.version>"),
                                _pom(dep, props="<sitemesh.version>3.2.1</sitemesh.version>"), "pom.xml"))
    assert (c.kind, c.before, c.after) == ("major", "2.4.2", "3.2.1")


def test_project_version_and_unresolvable_property():
    dep = _dep("com.example", "xxx-common", "${project.version}")
    c = _one(dependency_changes(_pom(dep, version="1.0.0"), _pom(dep, version="2.0.0"), "pom.xml"))
    assert (c.kind, c.before, c.after) == ("major", "1.0.0", "2.0.0")
    # a property the pom does not define (inherited from the parent) stays raw and compares as text
    raw = _dep("a", "b", "${from.parent}")
    assert dependency_changes(_pom(raw), _pom(raw), "pom.xml") == []
    c = _one(dependency_changes(_pom(raw), _pom(_dep("a", "b", "${other.prop}")), "pom.xml"))
    assert (c.kind, c.before, c.after) == ("minor", "${from.parent}", "${other.prop}")


def test_managed_to_explicit_and_back():
    c = _one(dependency_changes(_pom(_dep("a", "b")), _pom(_dep("a", "b", "1.2")), "pom.xml"))
    assert (c.kind, c.before, c.after) == ("minor", None, "1.2")
    c = _one(dependency_changes(_pom(_dep("a", "b", "1.2")), _pom(_dep("a", "b")), "pom.xml"))
    assert (c.kind, c.before, c.after) == ("minor", "1.2", None)       # now managed by the parent


def test_replaced_library_is_one_change():
    before = _pom(_dep("opensymphony", "sitemesh", "2.4.2") + _dep("org.qlrm", "qlrm", "4.2.1"))
    after = _pom(_dep("org.sitemesh", "sitemesh", "3.2.1") + _dep("org.qlrm", "qlrm", "4.2.1"))
    c = _one(dependency_changes(before, after, "xxx-back/pom.xml"))
    assert (c.kind, c.key, c.replaced_by, c.before, c.after) == (
        "replaced", "opensymphony:sitemesh", "org.sitemesh:sitemesh", "2.4.2", "3.2.1")
    assert c.needs_operator and c.text == "opensymphony:sitemesh 2.4.2 → org.sitemesh:sitemesh 3.2.1"


def test_replaced_by_prefix_and_group_segment_and_unrelated():
    before = _pom(_dep("org.springframework.boot", "spring-boot-starter-web") + _dep("com.foo", "alpha", "1.0"))
    after = _pom(_dep("org.springframework.boot", "spring-boot-starter-webmvc") + _dep("org.zeta", "omega", "2.0"))
    ch = dependency_changes(before, after, "pom.xml")
    kinds = {(c.kind, c.key, c.replaced_by) for c in ch}
    assert ("replaced", "org.springframework.boot:spring-boot-starter-web",
            "org.springframework.boot:spring-boot-starter-webmvc") in kinds
    assert ("removed", "com.foo:alpha", None) in kinds and ("added", "org.zeta:omega", None) in kinds
    # the old artifactId is the new groupId's last segment
    c = _one(dependency_changes(_pom(_dep("hystrix", "hystrix", "1.5")),
                                _pom(_dep("io.github.hystrix", "core-lib", "2.0")), "pom.xml"))
    assert (c.kind, c.replaced_by) == ("replaced", "io.github.hystrix:core-lib")


def test_classifier_change_is_a_version_change_and_scope_only_is_ignored():
    c = _one(dependency_changes(_pom(_dep("org.ehcache", "ehcache", "3.8.1")),
                                _pom(_dep("org.ehcache", "ehcache", "3.12.0", classifier="jakarta")), "pom.xml"))
    assert (c.kind, c.key, c.before, c.after) == ("minor", "org.ehcache:ehcache", "3.8.1", "3.12.0")
    assert "jakarta" in c.note
    assert dependency_changes(_pom(_dep("a", "b", "1", scope="compile")), _pom(_dep("a", "b", "1", scope="provided")),
                              "pom.xml") == []


def test_plugins_management_and_parent_sections():
    p = "<parent><groupId>com.co</groupId><artifactId>base-parent</artifactId><version>{}</version></parent>"
    before = _pom(parent=p.format("1.0.0"), managed=_dep("a", "managed", "1.0"),
                  build="<pluginManagement><plugins><plugin><artifactId>maven-war-plugin</artifactId>"
                        "<version>2.6</version></plugin></plugins></pluginManagement>"
                        "<plugins><plugin><groupId>org.codehaus.mojo</groupId><artifactId>build-helper-maven-plugin"
                        "</artifactId><version>1.12</version></plugin></plugins>")
    after = _pom(parent=p.format("4.0.0"), managed=_dep("a", "managed", "1.1"),
                 build="<pluginManagement><plugins><plugin><artifactId>maven-war-plugin</artifactId>"
                       "<version>3.4.0</version></plugin></plugins></pluginManagement>"
                       "<plugins><plugin><groupId>org.codehaus.mojo</groupId><artifactId>build-helper-maven-plugin"
                       "</artifactId><version>3.6.0</version></plugin></plugins>")
    by = {c.section: c for c in dependency_changes(before, after, "pom.xml")}
    assert set(by) == {"dependencyManagement", "plugins", "pluginManagement", "parent"}
    assert (by["dependencyManagement"].kind, by["dependencyManagement"].key) == ("minor", "a:managed")
    assert (by["plugins"].kind, by["plugins"].key) == ("major", "org.codehaus.mojo:build-helper-maven-plugin")
    assert (by["pluginManagement"].key, by["pluginManagement"].kind) == (
        "org.apache.maven.plugins:maven-war-plugin", "major")              # default plugin groupId
    assert (by["parent"].kind, by["parent"].before, by["parent"].after) == ("major", "1.0.0", "4.0.0")
    # a parent whose coordinates changed is a replacement
    c = _one(dependency_changes(_pom(parent=p.format("1.0.0")),
                                _pom(parent="<parent><groupId>com.co</groupId><artifactId>boot-jar</artifactId>"
                                            "<version>4.0.0</version></parent>"), "pom.xml"))
    assert (c.kind, c.section, c.key, c.replaced_by) == ("replaced", "parent", "com.co:base-parent", "com.co:boot-jar")


def test_unparsable_or_empty_pom_gives_nothing():
    good = _pom(_dep("a", "b", "1"))
    assert dependency_changes("<project><dependencies>", good, "pom.xml") == []
    assert dependency_changes(good, "not xml at all", "pom.xml") == []
    assert dependency_changes("", good, "pom.xml") == []
    assert dependency_changes(good, "<notaproject/>", "pom.xml") == []
    assert dependency_changes(good, good, "pom.xml") == []


# ---------------------------------------------------------------------------------------------------------------------
# apply_table
# ---------------------------------------------------------------------------------------------------------------------
def _lib(key: str, version: str, **kw) -> JakartaLibrary:
    return JakartaLibrary(key=key, jakarta_since=kw.pop("jakarta_since", version), version=version, **kw)


def _replaced(after: str = "3.2.1") -> DependencyChange:
    return DependencyChange(path="pom.xml", key="opensymphony:sitemesh", before="2.4.2", after=after,
                            kind="replaced", replaced_by="org.sitemesh:sitemesh")


def test_apply_table_allow_major_on_the_new_library():
    ch = _replaced()
    apply_table([ch], [_lib("org.sitemesh:sitemesh", "3.3.0", allow_major=True, source="company",
                            note="Approved by the web team. Check sitemesh3.xml.")])
    assert ch.approved and ch.note == "approved by the company table: Approved by the web team."
    # allow_major covers the entry's major line only (a jump to 4.x is not what the person allowed)
    ch4 = _replaced("4.0.0")
    apply_table([ch4], [_lib("org.sitemesh:sitemesh", "3.3.0", allow_major=True, source="company")])
    assert not ch4.approved


def test_apply_table_version_equals_after():
    major = DependencyChange(path="pom.xml", key="org.qlrm:qlrm", before="1.7.1", after="4.2.1", kind="major")
    relocated = DependencyChange(path="pom.xml", key="ch.simas.qlrm:qlrm", before="1.7.1", after="4.2.1",
                                 kind="replaced", replaced_by="org.qlrm:qlrm")
    table = [_lib("org.qlrm:qlrm", "4.2.1", old_keys=["ch.simas.qlrm:qlrm"], jakarta_since="4.0.1",
                  note="QLRM moved to jakarta in 4.0.1.")]
    apply_table([major, relocated], table)
    # a cross-major pin is a FACT of the shipped table, not an approval ("approved" = a person's allow_major or a
    # same-major pin); the change stays an operator check, the note says the table knows the release
    assert not major.approved and not relocated.approved
    assert major.note == "the shipped table knows this jakarta release (no same-major jakarta build listed)"
    assert relocated.note == major.note
    other = DependencyChange(path="pom.xml", key="org.qlrm:qlrm", before="1.7.1", after="4.0.1", kind="major")
    apply_table([other], table)
    assert not other.approved and other.note == ""
    same = DependencyChange(path="pom.xml", key="org.qlrm:qlrm", before="4.0.1", after="4.2.1", kind="minor")
    apply_table([same], table)
    assert same.approved and same.note == "approved by the shipped table: QLRM moved to jakarta in 4.0.1."
    allowed = [_lib("org.qlrm:qlrm", "4.2.1", old_keys=["ch.simas.qlrm:qlrm"], jakarta_since="4.0.1",
                    allow_major=True, source="company")]
    major2 = DependencyChange(path="pom.xml", key="org.qlrm:qlrm", before="1.7.1", after="4.2.1", kind="major")
    apply_table([major2], allowed)
    assert major2.approved and major2.note.startswith("approved by the company table")


def test_apply_table_same_major_note_and_no_cross_library_approval():
    """The shipped org.sitemesh:sitemesh entry (3.3.0) describes SiteMesh 3 users; it does not approve a move from
    SiteMesh 2 — but the table's same-major SiteMesh 2 release is offered in the note."""
    ch = _replaced("3.3.0")
    apply_table([ch], [_lib("opensymphony:sitemesh", "2.6.0"), _lib("org.sitemesh:sitemesh", "3.3.0",
                                                                     jakarta_since="3.2.0")])
    assert not ch.approved
    assert ch.note == "the table offers a same-major jakarta release: 2.6.0"
    minor = DependencyChange(path="pom.xml", key="opensymphony:sitemesh", before="2.4.2", after="2.5.0", kind="minor")
    apply_table([minor], [_lib("opensymphony:sitemesh", "2.6.0")])
    assert not minor.approved and minor.note == ""                    # only major / replaced get the offer
    apply_table([minor], None)                                        # never raises


# ---------------------------------------------------------------------------------------------------------------------
# still_applied
# ---------------------------------------------------------------------------------------------------------------------
class _FakeWs:
    def __init__(self, root: Path):
        self.root = root

    def abs(self, rel):
        return self.root / rel

    def read(self, rel):
        return (self.root / rel).read_text(encoding="utf-8")


def test_still_applied(tmp_path: Path):
    ws = _FakeWs(tmp_path)
    (tmp_path / "m").mkdir()
    (tmp_path / "m/pom.xml").write_text(_pom(_dep("org.sitemesh", "sitemesh", "${sm}") + _dep("a", "managed"),
                                             props="<sm>3.2.1</sm>"), encoding="utf-8")
    assert still_applied(ws, DependencyChange(path="m/pom.xml", key="opensymphony:sitemesh", before="2.4.2",
                                              after="3.2.1", kind="replaced", replaced_by="org.sitemesh:sitemesh"))
    assert not still_applied(ws, DependencyChange(path="m/pom.xml", key="opensymphony:sitemesh", before="2.4.2",
                                                  after="3.3.0", kind="replaced", replaced_by="org.sitemesh:sitemesh"))
    assert still_applied(ws, DependencyChange(path="m/pom.xml", key="org.sitemesh:sitemesh", before="2.4.2",
                                              after="3.2.1", kind="major"))
    assert still_applied(ws, DependencyChange(path="m/pom.xml", key="a:managed", before="1.0", after=None,
                                              kind="minor"))
    assert still_applied(ws, DependencyChange(path="m/pom.xml", key="opensymphony:sitemesh", before="2.4.2",
                                              kind="removed"))
    assert not still_applied(ws, DependencyChange(path="m/pom.xml", key="org.sitemesh:sitemesh", before="2.4.2",
                                                  kind="removed"))
    assert still_applied(ws, DependencyChange(path="m/pom.xml", key="a:managed", after="9", kind="added"))
    # rolled back: the tree has the old library again
    (tmp_path / "m/pom.xml").write_text(_pom(_dep("opensymphony", "sitemesh", "2.4.2")), encoding="utf-8")
    assert not still_applied(ws, DependencyChange(path="m/pom.xml", key="opensymphony:sitemesh", before="2.4.2",
                                                  after="3.2.1", kind="replaced",
                                                  replaced_by="org.sitemesh:sitemesh"))
    # unreadable / unparsable
    assert not still_applied(ws, DependencyChange(path="missing/pom.xml", key="a:b", kind="added"))
    (tmp_path / "m/pom.xml").write_text("<project>", encoding="utf-8")
    assert not still_applied(ws, DependencyChange(path="m/pom.xml", key="a:b", kind="removed"))


# ---------------------------------------------------------------------------------------------------------------------
# synthesized_guide
# ---------------------------------------------------------------------------------------------------------------------
def test_synthesized_guide_ko_and_en():
    major = DependencyChange(path="xxx-back/pom.xml", key="org.sitemesh:sitemesh", before="2.4.2", after="3.2.1",
                             kind="major")
    approved = _replaced("3.3.0")
    approved.approved, approved.note = True, "approved by the company table: ok"
    minor = DependencyChange(path="pom.xml", key="a:b", before="1.0", after="1.1", kind="minor")
    ko = synthesized_guide([major, minor, approved, major], "ko")
    blocks = ko.split("\n\n")
    assert len(blocks) == 2                                         # minor ignored, the duplicate once
    first = blocks[0].splitlines()
    assert first[0].startswith("org.sitemesh:sitemesh 2.4.2 → 3.2.1 (xxx-back/pom.xml): 메이저 버전이 바뀌었습니다")
    assert "2.x → 3.x" in first[0]
    assert len(first) >= 4 and all(x.startswith("- ") for x in first[1:])
    assert "MIGRATION-TODO" in blocks[0] and "filter" in blocks[0] and "properties" in blocks[0]
    assert "승인" in blocks[1] and "approved by the company table: ok" in blocks[1]
    assert all(len(line) <= 160 for line in ko.splitlines())
    en = synthesized_guide([major, approved], "en")
    assert "the major version changed" in en and "replaced by another library" in en
    assert "a person approved this change" in en and "메이저" not in en
    assert all(len(line) <= 160 for line in en.splitlines())
    assert synthesized_guide([minor]) == "" and synthesized_guide([]) == ""
    long = DependencyChange(path="a/" * 80 + "pom.xml", key="g:" + "x" * 50, before="1", after="2", kind="major")
    assert all(len(line) <= 160 for line in synthesized_guide([long]).splitlines())


# ---------------------------------------------------------------------------------------------------------------------
# changes_in_commit
# ---------------------------------------------------------------------------------------------------------------------
def test_changes_in_commit_with_a_real_workspace(tmp_path: Path):
    if shutil.which("git") is None:  # pragma: no cover
        pytest.skip("git not installed")
    from sbmig.core.workspace import Workspace

    root = tmp_path / "project"
    (root / "xxx-back").mkdir(parents=True)
    (root / "pom.xml").write_text(_pom(_dep("a", "b", "1.0")), encoding="utf-8")
    (root / "xxx-back/pom.xml").write_text(_pom(_dep("opensymphony", "sitemesh", "2.4.2")), encoding="utf-8")
    (root / "xxx-back/README.txt").write_text("x", encoding="utf-8")
    ws = Workspace(root, tmp_path / "run", "t")
    pre = ws.git_init()
    (root / "xxx-back/pom.xml").write_text(_pom(_dep("org.sitemesh", "sitemesh", "3.2.1")), encoding="utf-8")
    (root / "xxx-back/README.txt").write_text("y", encoding="utf-8")
    (root / "xxx-front").mkdir()
    (root / "xxx-front/pom.xml").write_text(_pom(_dep("c", "d", "1")), encoding="utf-8")   # a new pom: no changes
    sha = ws.commit("fix: sitemesh")
    c = _one(changes_in_commit(ws, pre, sha))
    assert (c.path, c.kind, c.key, c.replaced_by) == ("xxx-back/pom.xml", "replaced", "opensymphony:sitemesh",
                                                      "org.sitemesh:sitemesh")
    assert changes_in_commit(ws, sha, sha) == []
    assert changes_in_commit(ws, "deadbeef", sha) == []               # git cannot tell → []


def test_changes_in_commit_with_a_fake_workspace():
    files = {("p", "m/pom.xml"): _pom(_dep("a", "b", "1.0")), ("s", "m/pom.xml"): _pom(_dep("a", "b", "2.0"))}

    class Ws:
        def changed_between(self, a, b):
            return ["m/pom.xml", "m/src/A.java", "notpom.xml"]

        def file_at(self, sha, rel):
            return files.get((sha, rel))

    c = _one(changes_in_commit(Ws(), "p", "s"))
    assert (c.kind, c.path, c.before, c.after) == ("major", "m/pom.xml", "1.0", "2.0")

    class Broken:
        def changed_between(self, a, b):
            raise RuntimeError("git failed")

    assert changes_in_commit(Broken(), "p", "s") == []
