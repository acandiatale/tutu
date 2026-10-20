from __future__ import annotations

from pathlib import Path

import pytest
from lxml import etree

from sbmig.core.interfaces import PomEditorLike
from sbmig.core.models import GAV, PluginInfo
from sbmig.restructure import PomEditError, PomEditor, property_ref

NS_POM = """<?xml version="1.0" encoding="UTF-8"?>
<!-- leading comment -->
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>

  <parent>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-parent</artifactId>
    <version>2.7.18</version>
  </parent>

  <groupId>com.acme</groupId>
  <artifactId>demo</artifactId>
  <version>1.0</version>

  <properties>
    <!-- versions -->
    <guava.version>30.0-jre</guava.version>
    <java.version>1.8</java.version>
  </properties>

  <dependencyManagement>
    <dependencies>
      <dependency>
        <groupId>com.google.guava</groupId>
        <artifactId>guava</artifactId>
        <version>${guava.version}</version>
      </dependency>
    </dependencies>
  </dependencyManagement>

  <dependencies>
    <!-- web -->
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-web</artifactId>
    </dependency>
    <dependency>
      <groupId>com.google.guava</groupId>
      <artifactId>guava</artifactId>
      <version>${guava.version}</version>
    </dependency>
    <dependency>
      <groupId>org.apache.commons</groupId>
      <artifactId>commons-lang3</artifactId>
      <version>${undefined.version}</version>
      <scope>compile</scope>
      <exclusions>
        <exclusion><groupId>x</groupId><artifactId>y</artifactId></exclusion>
      </exclusions>
    </dependency>
  </dependencies>

  <build>
    <pluginManagement>
      <plugins>
        <plugin>
          <artifactId>maven-surefire-plugin</artifactId>
          <version>2.22.2</version>
        </plugin>
      </plugins>
    </pluginManagement>
    <plugins>
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-compiler-plugin</artifactId>
        <version>3.8.1</version>
        <configuration>
          <source>${java.version}</source>
          <target>${java.version}</target>
        </configuration>
      </plugin>
    </plugins>
  </build>
</project>
<!-- trailing comment -->
"""

PLAIN_POM = """<project>
\t<modelVersion>4.0.0</modelVersion>
\t<groupId>g</groupId>
\t<artifactId>plain</artifactId>
\t<version>1</version>
</project>
"""


@pytest.fixture()
def ed() -> PomEditor:
    return PomEditor.from_string(NS_POM, "pom.xml")


def reparse(e: PomEditor) -> etree._Element:
    return etree.fromstring(e.to_string().encode("utf-8"))


def test_protocol_and_roundtrip(ed: PomEditor) -> None:
    assert isinstance(ed, PomEditorLike)
    assert ed.to_string() == NS_POM
    assert ed.indent_unit == "  "
    assert not ed.changed


def test_roundtrip_preserves_crlf_and_latin1(tmp_path: Path) -> None:
    text = PLAIN_POM.replace("\n", "\r\n")
    p = tmp_path / "pom.xml"
    p.write_bytes(text.encode())
    e = PomEditor(p)
    assert e.to_string() == text and e.indent_unit == "\t"
    latin = '<?xml version="1.0" encoding="ISO-8859-1"?>\n<project><name>caf\xe9</name></project>\n'
    p2 = tmp_path / "l.xml"
    p2.write_bytes(latin.encode("iso-8859-1"))
    e2 = PomEditor(p2)
    assert e2.encoding == "ISO-8859-1"
    e2.set_property("k", "v")
    e2.save()
    assert "caf\xe9" in p2.read_bytes().decode("iso-8859-1")
    bom = tmp_path / "bom.xml"
    bom.write_bytes(b"\xef\xbb\xbf" + PLAIN_POM.encode())
    e3 = PomEditor(bom)
    e3.save()
    assert bom.read_bytes().startswith(b"\xef\xbb\xbf")


def test_malformed_raises(tmp_path: Path) -> None:
    with pytest.raises(PomEditError):
        PomEditor.from_string("<project><a></project>")
    with pytest.raises(PomEditError):
        PomEditor.from_string("<notapom/>")
    with pytest.raises(PomEditError):
        PomEditor(tmp_path / "missing.xml")


def test_set_property_is_byte_minimal_and_idempotent(ed: PomEditor) -> None:
    assert ed.set_property("guava.version", "33.4.8-jre") is True
    assert ed.to_string() == NS_POM.replace("<guava.version>30.0-jre<", "<guava.version>33.4.8-jre<")
    assert ed.set_property("guava.version", "33.4.8-jre") is False
    assert ed.get_property("guava.version") == "33.4.8-jre"
    assert ed.set_property("new.prop", "x") is True
    assert "    <java.version>1.8</java.version>\n    <new.prop>x</new.prop>\n  </properties>" in ed.to_string()
    assert ed.remove_property("new.prop") is True
    assert ed.remove_property("new.prop") is False
    assert ed.to_string() == NS_POM.replace("<guava.version>30.0-jre<", "<guava.version>33.4.8-jre<")


def test_set_parent_replace_and_create(ed: PomEditor) -> None:
    gav = GAV.parse("com.company:company-parent:4.1.0-1")
    assert ed.set_parent(gav, None) is True
    assert ed.set_parent(gav, None) is False
    s = ed.to_string()
    assert ("  <parent>\n    <groupId>com.company</groupId>\n    <artifactId>company-parent</artifactId>\n"
            "    <version>4.1.0-1</version>\n    <relativePath/>\n  </parent>") in s
    # everything outside the parent block untouched
    assert s.split("</parent>")[1] == NS_POM.split("</parent>")[1]
    assert ed.set_parent(gav, "../pom.xml") is True
    assert ed.parent_relative_path() == "../pom.xml"
    assert ed.set_parent_relative_path("") is True and ed.parent_relative_path() == ""
    assert ed.set_parent_relative_path(None) is True and ed.parent_relative_path() is None
    assert ed.remove_parent() is True and ed.get_parent() is None
    assert ed.remove_parent() is False

    plain = PomEditor.from_string(PLAIN_POM)
    assert plain.set_parent(gav) is True
    s = plain.to_string()
    assert s.index("<modelVersion>") < s.index("<parent>") < s.index("<groupId>g</groupId>")
    assert "\t<parent>\n\t\t<groupId>com.company</groupId>" in s
    assert plain.get_parent() == GAV(group_id="com.company", artifact_id="company-parent", version="4.1.0-1")


def test_packaging_and_modules_order(ed: PomEditor) -> None:
    assert ed.set_packaging("jar") is False            # default, absent → unchanged
    assert ed.set_packaging("pom") is True
    assert ed.set_packaging("pom") is False
    assert ed.packaging() == "pom"
    assert ed.set_modules(["module/comp-jar", "module/boot-jar"]) is True
    assert ed.set_modules(["module/comp-jar", "module/boot-jar"]) is False
    assert ed.add_module("module/boot-jar") is False
    assert ed.add_module("module/x") is True
    s = ed.to_string()
    assert s.index("<version>1.0</version>") < s.index("<packaging>") < s.index("<modules>") < s.index("<properties>")
    assert ed.modules() == ["module/comp-jar", "module/boot-jar", "module/x"]
    assert ed.set_modules([]) is True and ed.modules() == []
    plain = PomEditor.from_string(PLAIN_POM)
    assert plain.add_module("a") is True and plain.modules() == ["a"]


def test_dependency_queries(ed: PomEditor) -> None:
    deps = ed.dependencies()
    assert [d.key for d in deps] == ["org.springframework.boot:spring-boot-starter-web", "com.google.guava:guava",
                                     "org.apache.commons:commons-lang3"]
    assert deps[2].scope == "compile" and deps[1].version == "${guava.version}"
    assert ed.has_dependency("com.google.guava", "guava")
    assert not ed.has_dependency("x", "y")
    assert ed.has_managed_dependency("com.google.guava", "guava")
    assert [d.key for d in ed.managed_dependencies()] == ["com.google.guava:guava"]
    assert ed.resolve("${guava.version}") == "30.0-jre"
    assert ed.resolve("${project.version}") == "1.0"
    assert ed.resolve(None) is None
    assert property_ref("${a.b}") == "a.b" and property_ref("1.0") is None and property_ref(None) is None
    assert ed.effective_group_id() == "com.acme" and ed.effective_version() == "1.0"
    assert ed.artifact_id() == "demo" and ed.group_id() == "com.acme" and ed.version() == "1.0"


def test_set_dependency_version_via_property(ed: PomEditor) -> None:
    assert ed.set_dependency_version("com.google.guava", "guava", "33.4.8-jre") is True
    assert ed.get_property("guava.version") == "33.4.8-jre"
    assert ed.dependencies()[1].version == "${guava.version}"          # reference untouched
    assert ed.set_dependency_version("com.google.guava", "guava", "33.4.8-jre") is False
    # undefined property → literal
    assert ed.set_dependency_version("org.apache.commons", "commons-lang3", "3.17.0") is True
    assert ed.dependencies()[2].version == "3.17.0"
    # add version where none existed
    assert ed.set_dependency_version("org.springframework.boot", "spring-boot-starter-web", "4.1.0") is True
    s = ed.to_string()
    assert ("<artifactId>spring-boot-starter-web</artifactId>\n      <version>4.1.0</version>\n    </dependency>"
            in s)
    # None → removes <version> and the local managed entry
    assert ed.set_dependency_version("com.google.guava", "guava", None) is True
    assert ed.dependencies()[1].version is None
    assert ed.managed_dependencies() == []
    assert ed.find("dependencyManagement") is None
    assert ed.set_dependency_version("com.google.guava", "guava", None) is False


def test_add_remove_dependency(ed: PomEditor) -> None:
    gav = GAV(group_id="org.projectlombok", artifact_id="lombok", version="1.18.38", scope="provided",
              optional=True, classifier="x", type="jar")
    assert ed.add_dependency(gav) is True
    assert ed.add_dependency(gav) is False
    added = ed.dependencies()[-1]
    assert added == gav
    s = ed.to_string()
    assert ("    <dependency>\n      <groupId>org.projectlombok</groupId>\n      <artifactId>lombok</artifactId>\n"
            "      <version>1.18.38</version>\n      <type>jar</type>\n      <classifier>x</classifier>\n"
            "      <scope>provided</scope>\n      <optional>true</optional>\n    </dependency>\n  </dependencies>") in s
    # if_absent=False upserts
    assert ed.add_dependency(GAV(group_id="org.projectlombok", artifact_id="lombok", version="1.18.40"),
                             if_absent=False) is True
    assert ed.dependencies()[-1].version == "1.18.40"
    assert ed.remove_dependency("org.projectlombok", "lombok") is True
    assert ed.remove_dependency("org.projectlombok", "lombok") is False
    assert ed.to_string() == NS_POM
    # no-namespace pom without <dependencies>
    plain = PomEditor.from_string(PLAIN_POM)
    assert plain.add_dependency(GAV.parse("a:b")) is True
    s = plain.to_string()
    assert "\t<dependencies>\n\t\t<dependency>\n\t\t\t<groupId>a</groupId>" in s
    assert "xmlns" not in s
    for d in ("a:b",):
        g, a = d.split(":")
        assert plain.remove_dependency(g, a) is True
    assert plain.find("dependencies") is None
    assert plain.remove_all_dependencies() is False


def test_dependencies_namespace_is_default(ed: PomEditor) -> None:
    ed.add_dependency(GAV.parse("a:b:1"))
    ed.add_plugin(PluginInfo(artifact_id="maven-jar-plugin"))
    s = ed.to_string()
    assert "ns0" not in s and s.count('xmlns="http://maven.apache.org/POM/4.0.0"') == 1
    root = reparse(ed)
    assert root.find("{http://maven.apache.org/POM/4.0.0}dependencies") is not None


def test_replace_dependency(ed: PomEditor) -> None:
    new = GAV(group_id="com.google.guava", artifact_id="guava-new", scope="runtime")
    assert ed.replace_dependency("com.google.guava", "guava", new) is True
    assert ed.replace_dependency("com.google.guava", "guava", new) is False
    d = ed.dependencies()[1]
    assert d.key == "com.google.guava:guava-new" and d.scope == "runtime" and d.version == "${guava.version}"
    assert ed.managed_dependencies()[0].key == "com.google.guava:guava-new"
    # exclusions kept on rename
    assert ed.replace_dependency("org.apache.commons", "commons-lang3",
                                 GAV(group_id="org.apache.commons", artifact_id="commons-lang4", version="4.0")) is True
    assert "<exclusion><groupId>x</groupId>" in ed.to_string()
    assert ed.dependencies()[2].version == "4.0"
    # target already present → old one dropped
    assert ed.replace_dependency("org.apache.commons", "commons-lang4", GAV.parse(
        "org.springframework.boot:spring-boot-starter-web")) is True
    assert [x.key for x in ed.dependencies()].count("org.springframework.boot:spring-boot-starter-web") == 1
    assert len(ed.dependencies()) == 2


def test_managed_dependency_bom_import(ed: PomEditor) -> None:
    bom = GAV(group_id="org.springframework.boot", artifact_id="spring-boot-dependencies", version="4.1.0",
              type="pom", scope="import")
    assert ed.add_managed_dependency(bom, first=True) is True
    assert ed.add_managed_dependency(bom, first=True) is False
    md = ed.managed_dependencies()
    assert md[0] == bom and md[1].key == "com.google.guava:guava"
    assert ed.remove_managed_dependency("org.springframework.boot", "spring-boot-dependencies") is True
    assert ed.remove_managed_dependency("org.springframework.boot", "spring-boot-dependencies") is False
    assert ed.to_string() == NS_POM
    plain = PomEditor.from_string(PLAIN_POM)
    assert plain.add_managed_dependency(bom) is True
    root = etree.fromstring(plain.to_string().encode())
    d = root.find("dependencyManagement/dependencies/dependency")
    assert d.findtext("type") == "pom" and d.findtext("scope") == "import"
    assert plain.remove_managed_dependency("org.springframework.boot", "spring-boot-dependencies") is True
    assert plain.find("dependencyManagement") is None


def test_plugins(ed: PomEditor) -> None:
    plugin = PluginInfo(group_id="org.springframework.boot", artifact_id="spring-boot-maven-plugin",
                        configuration={"mainClass": "com.acme.App", "skip": False,
                                       "excludes": [{"exclude": {"groupId": "org.projectlombok",
                                                                 "artifactId": "lombok"}}],
                                       "jvmArguments": ["-Xmx1g", "-Dx=y"]})
    assert ed.add_plugin(plugin, executions=[{"goals": ["repackage"]}]) is True
    assert ed.add_plugin(plugin) is False
    info = next(p for p in ed.plugins() if p.artifact_id == "spring-boot-maven-plugin")
    assert info.configuration["mainClass"] == "com.acme.App"
    assert info.configuration["skip"] == "false"
    assert info.configuration["excludes"] == {"exclude": {"groupId": "org.projectlombok", "artifactId": "lombok"}}
    assert info.configuration["jvmArguments"] == ["-Xmx1g", "-Dx=y"]
    s = ed.to_string()
    assert "<executions>\n          <execution>\n            <goals>\n              <goal>repackage</goal>" in s
    assert "<jvmArgument>-Xmx1g</jvmArgument>" in s
    # upsert with new configuration
    assert ed.add_plugin(PluginInfo(group_id="org.springframework.boot", artifact_id="spring-boot-maven-plugin",
                                    version="4.1.0", configuration={"skip": True}), if_absent=False) is True
    assert ed.add_plugin(PluginInfo(group_id="org.springframework.boot", artifact_id="spring-boot-maven-plugin",
                                    version="4.1.0", configuration={"skip": True}), if_absent=False) is False
    info = next(p for p in ed.plugins() if p.artifact_id == "spring-boot-maven-plugin")
    assert info.version == "4.1.0" and info.configuration == {"skip": "true"}
    # set version: build + pluginManagement (default group id)
    assert ed.set_plugin_version("org.apache.maven.plugins", "maven-surefire-plugin", "3.5.2") is True
    assert ed.set_plugin_version("org.apache.maven.plugins", "maven-surefire-plugin", "3.5.2") is False
    assert ed.managed_plugins()[0].version == "3.5.2"
    assert ed.has_plugin("org.apache.maven.plugins", "maven-compiler-plugin")
    assert ed.remove_plugin("org.apache.maven.plugins", "maven-surefire-plugin") is True
    assert ed.find("build/pluginManagement") is None
    assert ed.remove_plugin("org.apache.maven.plugins", "maven-surefire-plugin") is False
    assert ed.remove_build_plugins_except(["org.apache.maven.plugins:maven-compiler-plugin"]) is True
    assert [p.artifact_id for p in ed.plugins()] == ["maven-compiler-plugin"]
    assert ed.remove_build_plugins_except([]) is True
    assert ed.find("build") is None


def test_plugin_on_plain_pom_and_refs() -> None:
    plain = PomEditor.from_string(PLAIN_POM)
    assert plain.add_plugin(PluginInfo(artifact_id="maven-compiler-plugin", version="3.13.0",
                                       configuration={"release": 21, "annotationProcessorPaths": [
                                           {"path": {"groupId": "org.projectlombok", "artifactId": "lombok",
                                                     "version": "${lombok.version}"}}]})) is True
    root = etree.fromstring(plain.to_string().encode())
    assert root.findtext("build/plugins/plugin/configuration/release") == "21"
    refs = plain.artifact_refs()
    assert [(r.section, r.key, r.version) for r in refs] == [
        ("annotationProcessorPath", "org.projectlombok:lombok", "${lombok.version}")]
    assert plain.rename_ref(refs[0].element, "org.projectlombok", "lombok2") is True
    assert plain.set_ref_version(refs[0].element, "1.0") is True
    assert plain.artifact_refs()[0].key == "org.projectlombok:lombok2"


def test_misc_helpers(ed: PomEditor, tmp_path: Path) -> None:
    assert ed.properties() == {"guava.version": "30.0-jre", "java.version": "1.8"}
    assert ed.has_property("java.version")
    assert ed.set_coordinate("version", "2.0") is True and ed.version() == "2.0"
    assert ed.remove_element("build/plugins") is True
    assert ed.find("build/plugins") is None and ed.find("build/pluginManagement") is not None
    assert ed.remove_element("nope/nothing") is False
    clone = ed.clone_to(tmp_path / "x" / "pom.xml")
    assert clone.to_string() == ed.to_string() and clone.path == tmp_path / "x" / "pom.xml"
    ed.path = tmp_path / "orig.xml"
    ed.save()
    assert not ed.changed and (tmp_path / "orig.xml").read_text() == ed.to_string()
    el = ed.ensure_path("build/extensions")
    assert PomEditor.local_name(el) == "extensions"
    assert PomEditor.text_of(None) is None


def test_load_from_file_and_save(tmp_path: Path) -> None:
    p = tmp_path / "pom.xml"
    p.write_text(NS_POM, encoding="utf-8")
    e = PomEditor(p)
    e.set_packaging("pom")
    assert e.changed
    e.save()
    assert "<packaging>pom</packaging>" in p.read_text()
    assert PomEditor(p).packaging() == "pom"
    assert p.read_text().endswith("</project>\n<!-- trailing comment -->\n")
    assert p.read_text().startswith('<?xml version="1.0" encoding="UTF-8"?>\n<!-- leading comment -->\n')


def test_copy_into_other_namespace() -> None:
    src = PomEditor.from_string(PLAIN_POM.replace("</project>", "\t<dependencies>\n\t\t<!-- c -->\n\t\t<dependency>"
                                                  "<groupId>a</groupId><artifactId>b</artifactId></dependency>\n"
                                                  "\t</dependencies>\n</project>"))
    dst = PomEditor.from_string(NS_POM)
    cont = dst.ensure_path("dependencies")
    for el in src.dependency_elements():
        dst.copy_into(cont, el)
    assert dst.dependencies()[-1].key == "a:b"
    assert "ns0" not in dst.to_string()
    reparse(dst)


def test_remove_version_keeps_formatting_and_properties(tmp_path: Path) -> None:
    text = ("<project>\n  <properties>\n    <x.version>1</x.version>\n  </properties>\n  <dependencies>\n"
            "    <dependency>\n      <groupId>g</groupId>\n      <artifactId>a</artifactId>\n"
            "      <version>${x.version}</version>\n      <scope>test</scope>\n    </dependency>\n"
            "  </dependencies>\n</project>\n")
    ed = PomEditor.from_string(text, tmp_path / "pom.xml")
    el = ed.dependency_elements(False)[0]
    assert ed.remove_version(el) is True
    assert ed.remove_version(el) is False                                    # idempotent
    out = ed.to_string()
    assert "<version>" not in out and "<x.version>1</x.version>" in out
    assert "      <artifactId>a</artifactId>\n      <scope>test</scope>\n    </dependency>" in out
