"""v2.1x (2026-10-31, D6 / D7, review E-H4 "a placeholder overwrites the real value"): property files under WEB-INF named
by a location are definitions (the restructure points the legacy-config copies at their boot stash copy), and a
`${…}` segment of a `classpath:` location becomes a `*` glob — the packaged profile files it may name are definitions."""
from __future__ import annotations

from pathlib import Path

from sbmig.analyze import load_rules, scan_properties
from sbmig.analyze.analyzer import PathView
from sbmig.analyze.pom_parser import parse_poms
from sbmig.analyze.properties import profile_glob
from sbmig.analyze.source_scanner import scan_sources
from tests.analyze.helpers import pom, write

REPO = Path(__file__).resolve().parents[2]
RULES = REPO / "knowledge" / "config" / "externalized.yml"
BEANS = ('<beans xmlns="http://www.springframework.org/schema/beans" '
         'xmlns:context="http://www.springframework.org/schema/context">\n{body}\n</beans>\n')


def inv(root: Path, cfg):
    if not (root / "pom.xml").exists():
        write(root, "pom.xml", pom("app"))
    ws = PathView(root)
    poms, _ = parse_poms(ws)
    return scan_properties(ws, poms, scan_sources(ws), load_rules(RULES), cfg)


def java_reads(root: Path, *keys: str, head: str = "") -> None:
    fields = "\n".join(f'    @Value("${{{k}}}") String f{i};' for i, k in enumerate(keys))
    write(root, "src/main/java/com/acme/Svc.java",
          f"package com.acme;\n\nimport org.springframework.beans.factory.annotation.Value;\n{head}\n"
          f"public class Svc {{\n{fields}\n}}\n")


def test_webinf_property_file_named_by_xml_is_a_definition(tmp_path: Path, cfg) -> None:
    write(tmp_path, "src/main/webapp/WEB-INF/spring/root.xml",
          BEANS.format(body='<context:property-placeholder location="/WEB-INF/config/globals.properties"/>'))
    write(tmp_path, "src/main/webapp/WEB-INF/config/globals.properties", "Globals.DbType=oracle\n")
    java_reads(tmp_path, "Globals.DbType", "other.key")
    f = {x.key: x for x in inv(tmp_path, cfg).findings}
    assert f["Globals.DbType"].status == "defined"
    assert f["Globals.DbType"].defined_in == ["src/main/webapp/WEB-INF/config/globals.properties"]
    assert f["other.key"].status == "external"


def test_webinf_property_source_in_java_is_a_definition(tmp_path: Path, cfg) -> None:
    java_reads(tmp_path, "app.mode", head='@org.springframework.context.annotation.PropertySource("/WEB-INF/app.properties")')
    write(tmp_path, "src/main/webapp/WEB-INF/app.properties", "app.mode=live\n")
    f = {x.key: x for x in inv(tmp_path, cfg).findings}
    assert f["app.mode"].status == "defined"


def test_profile_segment_in_classpath_location_is_a_glob(tmp_path: Path, cfg) -> None:
    write(tmp_path, "src/main/resources/spring/root.xml",
          BEANS.format(body='<context:property-placeholder location="classpath:config/${spring.profiles.active}/app.properties"/>'))
    write(tmp_path, "src/main/resources/config/dev/app.properties", "app.mode=dev\n")
    write(tmp_path, "src/main/resources/config/prod/app.properties", "app.mode=prod\n")
    java_reads(tmp_path, "app.mode")
    i = inv(tmp_path, cfg)
    f = {x.key: x for x in i.findings}
    assert f["app.mode"].status == "defined"                         # packaged: never __SET_ME__
    assert sorted(f["app.mode"].defined_in) == ["src/main/resources/config/dev/app.properties",
                                                "src/main/resources/config/prod/app.properties"]
    assert not any("property-source:file" in n and "app.properties" in n for n in i.notes)


def test_profile_glob() -> None:
    assert profile_glob("config/${env}/app-${region:kr}.properties") == "config/*/app-*.properties"
    assert profile_glob("a.properties") == "a.properties"
