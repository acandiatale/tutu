from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.analyze import PathView, module_order, parse_poms
from sbmig.analyze.pom_parser import (
    interpolate,
    owning_pom,
    parse_pom_text,
    pom_encoding,
    read_pom_text,
    xml_codec,
    xml_without_declaration,
)
from tests.analyze.helpers import NS, dep, plugin, pom, write


def _multi(root: Path) -> None:
    write(root, "pom.xml", pom(
        "parent", packaging="pom", modules=("core", "app"),
        props={"lombok.version": "1.18.20", "spring-boot.version": "2.7.18", "maven.compiler.release": "17",
               "lib.base": "1.2", "lib.version": "${lib.base}.3"},
        managed=(dep("org.springframework.boot", "spring-boot-dependencies", "${spring-boot.version}",
                     scope="import", type_="pom"),
                 dep("org.projectlombok", "lombok", "${lombok.version}"),
                 dep("com.acme", "core", "${project.version}")),
        plugin_mgmt=(plugin("maven-compiler-plugin", "3.11.0"),)))
    write(root, "core/pom.xml", pom(
        "core", group=None, version=None, parent=("com.acme", "parent", "1.0.0"),
        deps=(dep("org.projectlombok", "lombok"), dep("com.acme.lib", "thing", "${lib.version}"),
              dep("org.springframework", "spring-context", "5.3.31")),
        plugins=(plugin("maven-compiler-plugin"),), namespace=False))
    write(root, "app/pom.xml", pom(
        "app", group=None, version=None, parent=("com.acme", "parent", "1.0.0"),
        deps=(dep("com.acme", "core"), dep("org.springframework.boot", "spring-boot-starter-web")),
        plugins=(plugin("spring-boot-maven-plugin", g="org.springframework.boot",
                        config="<loaderImplementation>CLASSIC</loaderImplementation><excludes><exclude>x</exclude></excludes>"),)))
    # poms in ignored places
    write(root, "app/target/classes/META-INF/pom.xml", pom("ignored"))
    write(root, "app/src/test/resources/fixture/pom.xml", pom("ignored2"))
    write(root, "web/node_modules/x/pom.xml", pom("ignored3"))


def test_multi_module_inheritance_interpolation_and_dep_mgmt(tmp_path: Path) -> None:
    _multi(tmp_path)
    poms, warnings = parse_poms(PathView(tmp_path))
    assert warnings == []
    by = {p.path: p for p in poms}
    assert set(by) == {"pom.xml", "core/pom.xml", "app/pom.xml"}
    root, core, app = by["pom.xml"], by["core/pom.xml"], by["app/pom.xml"]
    assert root.is_root and root.is_aggregator and not root.parent_in_repo
    assert core.parent_in_repo and not core.is_root and core.group_id == "com.acme" and core.version == "1.0.0"
    deps = {d.key: d for d in core.dependencies}
    assert deps["org.projectlombok:lombok"].version == "1.18.20"          # managed by parent via property
    assert deps["com.acme.lib:thing"].version == "1.2.3"                  # nested ${} interpolation
    assert {d.key: d.version for d in app.dependencies}["com.acme:core"] == "1.0.0"   # ${project.version}
    assert root.dependency_management[0].scope == "import"
    assert root.spring_boot_version == "2.7.18" and app.spring_boot_version == "2.7.18"
    assert core.spring_version == "5.3.31"
    assert core.java_version == "17"
    compiler = next(p for p in core.plugins if p.artifact_id == "maven-compiler-plugin")
    assert compiler.version == "3.11.0"                                   # from parent pluginManagement
    assert not root.has_spring_boot_plugin and app.has_spring_boot_plugin
    bp = next(p for p in app.plugins if p.artifact_id == "spring-boot-maven-plugin")
    assert bp.configuration["loaderImplementation"] == "CLASSIC"
    assert bp.configuration["excludes"] == ["x"]
    assert module_order(poms) == ["pom.xml", "core/pom.xml", "app/pom.xml"]
    assert root.properties["lib.version"] == "${lib.base}.3"             # own raw properties are kept


def test_parent_matched_by_gav_when_relative_path_points_elsewhere(tmp_path: Path) -> None:
    write(tmp_path, "build/parent/pom.xml", pom("corp-parent", packaging="pom", props={"java.version": "11"}))
    write(tmp_path, "svc/pom.xml", pom("svc", parent=("com.acme", "corp-parent", "1.0.0")))
    write(tmp_path, "other/pom.xml", pom("other", parent=("com.acme", "corp-parent", "9.9.9"),
                                         relative_path="../build/parent"))
    write(tmp_path, "ext/pom.xml", pom("ext", parent=("org.springframework.boot", "spring-boot-starter-parent",
                                                      "3.3.5")))
    by = {p.path: p for p in parse_poms(PathView(tmp_path))[0]}
    assert by["svc/pom.xml"].parent_in_repo and by["svc/pom.xml"].java_version == "11"
    assert not by["other/pom.xml"].parent_in_repo                        # version mismatch → not the same parent
    assert by["ext/pom.xml"].is_root and by["ext/pom.xml"].spring_boot_version == "3.3.5"


def test_relative_path_to_explicit_file_and_versions(tmp_path: Path) -> None:
    write(tmp_path, "parent.xml", "")  # empty file: not a pom.xml, ignored
    write(tmp_path, "pom.xml", pom("root", packaging="pom", modules=("a/pom.xml",),
                                   props={"spring.version": "4.3.30.RELEASE"}))
    write(tmp_path, "a/pom.xml", pom("a", parent=("com.acme", "root", "1.0.0"), relative_path="../pom.xml",
                                     deps=(dep("org.springframework.boot", "spring-boot-starter", "2.1.0.RELEASE"),),
                                     plugins=(plugin("maven-compiler-plugin", "3.1",
                                                     config="<source>1.8</source><target>1.8</target>"),)))
    poms, _ = parse_poms(PathView(tmp_path))
    a = next(p for p in poms if p.path == "a/pom.xml")
    assert a.parent_in_repo and a.spring_version == "4.3.30.RELEASE"
    assert a.spring_boot_version == "2.1.0.RELEASE" and a.java_version == "1.8"
    assert module_order(poms) == ["pom.xml", "a/pom.xml"]


def test_unparsable_pom_is_skipped_with_warning(tmp_path: Path) -> None:
    write(tmp_path, "pom.xml", pom("ok"))
    write(tmp_path, "broken/pom.xml", "<project><artifactId>x</artifactId>")
    write(tmp_path, "noart/pom.xml", "<project><groupId>x</groupId></project>")
    write(tmp_path, "notproject/pom.xml", "<settings/>")
    poms, warnings = parse_poms(PathView(tmp_path))
    assert [p.path for p in poms] == ["pom.xml"]
    assert len(warnings) == 3 and all("pom skipped" in w for w in warnings)


def test_helpers() -> None:
    assert interpolate("${a}-${b}", {"a": "${b}", "b": "x"}) == "x-x"
    assert interpolate("${missing}", {}) == "${missing}"
    assert interpolate(None, {}) is None
    with pytest.raises(ValueError):
        parse_pom_text("<project/>", "pom.xml")
    raw = parse_pom_text(pom("x"), "pom.xml")
    assert raw.artifact_id == "x"


def test_owning_pom_and_module_order_with_unreferenced(tmp_path: Path) -> None:
    write(tmp_path, "pom.xml", pom("root", packaging="pom", modules=("a",)))
    write(tmp_path, "a/pom.xml", pom("a"))
    write(tmp_path, "loose/pom.xml", pom("loose"))
    poms, _ = parse_poms(PathView(tmp_path))
    assert module_order(poms) == ["pom.xml", "a/pom.xml", "loose/pom.xml"]
    assert owning_pom("a/src/main/java/X.java", poms).path == "a/pom.xml"
    assert owning_pom("src/main/java/X.java", poms).path == "pom.xml"


def test_repository_urls_declared_in_poms(tmp_path: Path) -> None:
    from sbmig.analyze.pom_parser import repository_urls
    text = pom("app", props={"nexus.url": "https://nexus.example.com"}).replace("</project>", """
  <repositories>
    <repository><id>old</id><url>http://old-nexus.corp/repo</url></repository>
    <!-- <repository><id>gone</id><url>http://commented-out/</url></repository> -->
    <repository><id>central-again</id><url> https://repo.maven.apache.org/maven2 </url></repository>
  </repositories>
  <distributionManagement>
    <repository><id>deploy</id><url>http://deploy-target/</url></repository>
  </distributionManagement>
  <profiles>
    <profile>
      <id>ci</id>
      <pluginRepositories>
        <pluginRepository><id>p</id><url>${nexus.url}/repository/plugins</url></pluginRepository>
      </pluginRepositories>
    </profile>
  </profiles>
</project>""")
    write(tmp_path, "pom.xml", text)
    assert repository_urls(text) == ["http://old-nexus.corp/repo", "https://repo.maven.apache.org/maven2",
                                     "${nexus.url}/repository/plugins"]
    poms, _ = parse_poms(PathView(tmp_path))
    assert poms[0].repositories == ["http://old-nexus.corp/repo", "https://repo.maven.apache.org/maven2",
                                    "https://nexus.example.com/repository/plugins"]      # interpolated
    write(tmp_path, "pom.xml", pom("plain"))
    assert parse_poms(PathView(tmp_path))[0][0].repositories == []


# ---------------------------------------------------------------------------
# A1 (2026-10-28): poms whose XML declaration names a multi-byte / Java-only encoding
# ---------------------------------------------------------------------------
def _korean_pom(decl: str, name: str = "주문 서비스") -> str:
    return (f'<?xml version="1.0" encoding="{decl}"?>\n'
            "<!-- 주문 서비스 모듈: 한글 주석 -->\n"
            f'<project {NS}>\n  <modelVersion>4.0.0</modelVersion>\n'
            "  <groupId>com.acme</groupId><artifactId>order</artifactId><version>1.0.0</version>\n"
            f"  <name>{name}</name>\n"
            "  <properties><app.title>주문 관리</app.title></properties>\n"
            f"  <dependencies>{dep('org.apache.commons', 'commons-lang3', '3.12.0')}</dependencies>\n"
            "</project>\n")


@pytest.mark.parametrize("decl", ["EUC-KR", "MS949", "x-windows-949", "KSC5601", "CP949", "euc-kr"])
def test_cp949_pom_with_multibyte_declaration_is_parsed(tmp_path: Path, decl: str) -> None:
    (tmp_path / "pom.xml").write_bytes(_korean_pom(decl).encode("cp949"))
    poms, warnings = parse_poms(PathView(tmp_path))
    assert warnings == []
    assert [p.artifact_id for p in poms] == ["order"]
    assert poms[0].properties["app.title"] == "주문 관리"                      # decoded as cp949, not U+FFFD
    assert [d.coordinates for d in poms[0].dependencies] == ["org.apache.commons:commons-lang3:3.12.0"]
    assert "<name>주문 서비스</name>" in read_pom_text(PathView(tmp_path), "pom.xml")


def test_euc_kr_declaration_with_cp949_only_hangul(tmp_path: Path) -> None:
    # 똠 / 햏 are outside KS X 1001: strict EUC-KR fails, the cp949 superset decodes them
    (tmp_path / "pom.xml").write_bytes(_korean_pom("EUC-KR", name="똠햏 서비스").encode("cp949"))
    assert pom_encoding((tmp_path / "pom.xml").read_bytes()) == "cp949"
    poms, warnings = parse_poms(PathView(tmp_path))
    assert warnings == [] and poms[0].artifact_id == "order"
    assert "<name>똠햏 서비스</name>" in read_pom_text(PathView(tmp_path), "pom.xml")


def test_utf8_declaration_but_cp949_bytes_is_detected(tmp_path: Path) -> None:
    (tmp_path / "pom.xml").write_bytes(_korean_pom("UTF-8").encode("cp949"))
    poms, warnings = parse_poms(PathView(tmp_path))
    assert warnings == [] and poms[0].properties["app.title"] == "주문 관리"


def test_utf8_pom_with_bom_still_parses(tmp_path: Path) -> None:
    (tmp_path / "pom.xml").write_bytes(b"\xef\xbb\xbf" + _korean_pom("UTF-8").encode("utf-8"))
    assert pom_encoding((tmp_path / "pom.xml").read_bytes()) == "utf-8-sig"
    poms, warnings = parse_poms(PathView(tmp_path))
    assert warnings == [] and poms[0].artifact_id == "order"
    assert poms[0].properties["app.title"] == "주문 관리"


def test_parse_pom_text_accepts_bytes_and_declared_text() -> None:
    raw = parse_pom_text(_korean_pom("EUC-KR").encode("euc-kr"), "pom.xml")
    assert raw.artifact_id == "order" and raw.properties["app.title"] == "주문 관리"
    raw = parse_pom_text(_korean_pom("ISO-8859-1"), "pom.xml")          # decoded text: the declaration is ignored
    assert raw.properties["app.title"] == "주문 관리"


def test_xml_codec_and_declaration_helpers() -> None:
    assert [xml_codec(n) for n in ("EUC-KR", "MS949", "x-windows-949", "KSC5601", "UTF-8", "nope", None)] == \
        ["cp949", "cp949", "cp949", "cp949", "utf-8", None, None]
    assert xml_without_declaration('﻿ <?xml version="1.0" encoding="EUC-KR"?>\n<project/>') == "\n<project/>"
    assert xml_without_declaration("<project/>") == "<project/>"


def test_workspace_read_keeps_cp949_for_write_back(tmp_path: Path) -> None:
    from sbmig.core.workspace import Workspace

    root = tmp_path / "proj"
    root.mkdir()
    (root / "pom.xml").write_bytes(_korean_pom("EUC-KR").encode("cp949"))
    ws = Workspace(root, tmp_path / "run", "t")
    poms, warnings = parse_poms(ws)
    assert warnings == [] and poms[0].properties["app.title"] == "주문 관리"
    text = ws.read("pom.xml").replace("주문 관리", "주문 관리 v2")
    ws.write("pom.xml", text)
    data = (root / "pom.xml").read_bytes()
    assert data.decode("cp949") == text                       # still the declared (EUC-KR family) encoding
