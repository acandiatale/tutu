"""v2.1z-k (company run 2026-11-04): the boot pom kept as-is `java-version=1.8` and `org.springSecurity-version=4.2.7.RELEASE`
— spellings the name-based cleanups (chain-defined names, names a dropped `<version>` referenced) never see. The value-based
pass removes a property whose value looks like a version / Java level when no project pom references `${name}`; names in
`skip` (chain names, the Java-level properties `_fix_java` writes) and referenced or non-version properties stay."""
from __future__ import annotations

from pathlib import Path

from sbmig.restructure.pom_editor import PomEditor
from sbmig.restructure.restructurer import JAVA_PROPS, VERSION_LIKE_RE, orphan_version_properties

NS = 'xmlns="http://maven.apache.org/POM/4.0.0"'


def _pom(props: dict[str, str], deps: str = "") -> str:
    ps = "".join(f"<{k}>{v}</{k}>" for k, v in props.items())
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<project {NS}>\n  <modelVersion>4.0.0</modelVersion>\n'
            f"  <groupId>com.x</groupId><artifactId>a</artifactId><version>1</version>\n  <properties>{ps}</properties>\n"
            f"  <dependencies>{deps}</dependencies>\n</project>\n")


def test_unreferenced_version_like_properties_go_whatever_their_name(tmp_path: Path):
    boot = tmp_path / "boot" / "pom.xml"; back = tmp_path / "back" / "pom.xml"
    boot.parent.mkdir(); back.parent.mkdir()
    boot.write_text(_pom({"java-version": "1.8", "org.springSecurity-version": "4.2.7.RELEASE",
                          "project.build.sourceEncoding": "UTF-8", "maven.compiler.release": "21",
                          "spring-boot.version": "2.5.15", "my.lib.version": "1.2", "mobile.version": "1.1.5.RELEASE"}),
                    encoding="utf-8")
    back.write_text(_pom({}, "<dependency><groupId>com.x</groupId><artifactId>lib</artifactId>"
                             "<version>${my.lib.version}</version></dependency>"), encoding="utf-8")
    eds = {"boot/pom.xml": PomEditor(boot), "back/pom.xml": PomEditor(back)}
    removed = orphan_version_properties(eds, skip={"spring-boot.version", *JAVA_PROPS})
    assert sorted(removed) == [("boot/pom.xml", "java-version", "1.8"),
                               ("boot/pom.xml", "mobile.version", "1.1.5.RELEASE"),
                               ("boot/pom.xml", "org.springSecurity-version", "4.2.7.RELEASE")]
    eds["boot/pom.xml"].save()
    text = boot.read_text(encoding="utf-8")
    assert "java-version" not in text and "springSecurity" not in text and "mobile.version" not in text
    for kept in ("project.build.sourceEncoding", "maven.compiler.release", "spring-boot.version", "my.lib.version"):
        assert f"<{kept}>" in text, kept


def test_nothing_to_remove_leaves_the_pom_untouched(tmp_path: Path):
    pom = tmp_path / "pom.xml"
    pom.write_text(_pom({"project.build.sourceEncoding": "UTF-8", "x.version": "3.1"},
                        "<dependency><groupId>g</groupId><artifactId>x</artifactId><version>${x.version}</version></dependency>"),
                   encoding="utf-8")
    assert orphan_version_properties({"pom.xml": PomEditor(pom)}, skip=set()) == []


def test_version_like_values():
    yes = ["1.8", "17", "21", "4.2.7.RELEASE", "2.5.15", "3.0.0-M1", "1.1.5.RELEASE", "5.3.39", "2.0.0.Final", "1.0-SNAPSHOT"]
    no = ["UTF-8", "true", "${other}", "https://x", "a.b", "", "1.8 ", "release"]
    assert all(VERSION_LIKE_RE.match(v) for v in yes), [v for v in yes if not VERSION_LIKE_RE.match(v)]
    assert not any(VERSION_LIKE_RE.match(v) for v in no), [v for v in no if VERSION_LIKE_RE.match(v)]
