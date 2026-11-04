"""v2.1z-i: wiring a bundled jar into a module pom (sbmig.restructure.system_jars)."""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from sbmig.core.models import JarCoordinate
from sbmig.core.workspace import Workspace
from sbmig.restructure.system_jars import (
    BUNDLED_TAIL,
    bundled_jar_followup,
    copy_jar_to_module_lib,
    ensure_include_system_scope,
    webinf_left_followup,
    wire_bundled_jar,
)

AS_IS = "src/main/webapp/WEB-INF/lib/acme-seed-1.0.jar"

BACK_POM = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
    <modelVersion>4.0.0</modelVersion>
    <groupId>com.example</groupId>
    <artifactId>shop-back</artifactId>
    <dependencies>
        <dependency>
            <groupId>org.example</groupId>
            <artifactId>util</artifactId>
        </dependency>
    </dependencies>
</project>
"""

BOOT_POM = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
    <modelVersion>4.0.0</modelVersion>
    <artifactId>shop-boot</artifactId>
    <build>
        <plugins>
            <plugin>
                <groupId>org.springframework.boot</groupId>
                <artifactId>spring-boot-maven-plugin</artifactId>
            </plugin>
        </plugins>
    </build>
</project>
"""


def _jar(path: Path, entry: str = "com/acme/seed/Seed.class", data: bytes = b"\xca\xfe\xba\xbe") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(entry, data)


def _ws(tmp_path: Path, *, jar_at: str = f"shop-back/{AS_IS}") -> Workspace:
    root = tmp_path / "proj"
    (root / "shop-back").mkdir(parents=True)
    (root / "shop-boot").mkdir(parents=True)
    (root / "shop-back/pom.xml").write_text(BACK_POM)
    (root / "shop-boot/pom.xml").write_text(BOOT_POM)
    _jar(root / jar_at)
    return Workspace(root, tmp_path / "run", "r1")


def _seed(**kw) -> JarCoordinate:
    return JarCoordinate(path=AS_IS, provides=["com.acme.seed"], **kw)


def test_wire_system_copies_declares_and_sets_include_system_scope(tmp_path):
    ws = _ws(tmp_path)
    w = wire_bundled_jar(ws, None, module_pom="shop-back/pom.xml", jar=_seed(), repository_has=False,
                         boot_poms=["shop-boot/pom.xml"], project_group="com.example")
    assert (w.group_id, w.artifact_id, w.version, w.scope) == ("com.example", "acme-seed", "1.0", "system")
    assert w.placeholder and w.copied_to == "shop-back/lib/acme-seed-1.0.jar"
    assert w.system_path == "${project.basedir}/lib/acme-seed-1.0.jar"
    assert w.boot_poms == ["shop-boot/pom.xml"]
    assert (ws.root / "shop-back/lib/acme-seed-1.0.jar").read_bytes() == (ws.root / f"shop-back/{AS_IS}").read_bytes()
    pom = (ws.root / "shop-back/pom.xml").read_text()
    assert ("        <!-- acme-seed-1.0.jar: Maven coordinates unknown (no pom.properties, not in the jar name table) — "
            "system scope keeps the build passing; install the jar into Nexus and replace this entry -->\n"
            "        <dependency>\n"
            "            <groupId>com.example</groupId>\n"
            "            <artifactId>acme-seed</artifactId>\n"
            "            <version>1.0</version>\n"
            "            <scope>system</scope>\n"
            "            <systemPath>${project.basedir}/lib/acme-seed-1.0.jar</systemPath>\n"
            "        </dependency>\n") in pom
    boot = (ws.root / "shop-boot/pom.xml").read_text()
    # system scope is not transitive: the boot pom declares the same g:a:v, pointing at the ONE copy in the back module
    assert ("        <dependency>\n"
            "            <groupId>com.example</groupId>\n"
            "            <artifactId>acme-seed</artifactId>\n"
            "            <version>1.0</version>\n"
            "            <scope>system</scope>\n"
            "            <systemPath>${project.basedir}/../shop-back/lib/acme-seed-1.0.jar</systemPath>\n"
            "        </dependency>\n") in boot
    assert "<!-- acme-seed-1.0.jar: Maven coordinates unknown" in boot
    assert not (ws.root / "shop-boot/lib").exists()
    assert ("<configuration>\n                    <includeSystemScope>true</includeSystemScope>\n"
            "                </configuration>") in boot


def test_wire_is_idempotent(tmp_path):
    ws = _ws(tmp_path)
    args = {"module_pom": "shop-back/pom.xml", "jar": _seed(), "repository_has": False,
            "boot_poms": ["shop-boot/pom.xml"], "project_group": "com.example"}
    wire_bundled_jar(ws, None, **args)
    before = (ws.root / "shop-back/pom.xml").read_text(), (ws.root / "shop-boot/pom.xml").read_text()
    w2 = wire_bundled_jar(ws, None, **args)
    after = (ws.root / "shop-back/pom.xml").read_text(), (ws.root / "shop-boot/pom.xml").read_text()
    assert before == after and after[0].count("<artifactId>acme-seed</artifactId>") == 1
    assert after[1].count("<artifactId>acme-seed</artifactId>") == 1
    assert w2.scope == "system" and not w2.boot_poms and any("already declared" in n for n in w2.notes)


def test_wire_keeps_real_coordinates_when_the_jar_has_them(tmp_path):
    ws = _ws(tmp_path)
    jar = _seed(group_id="com.acme", artifact_id="seed", version="2.3", source="pom_properties")
    w = wire_bundled_jar(ws, None, module_pom="shop-back/pom.xml", jar=jar, repository_has=False)
    assert (w.group_id, w.artifact_id, w.version, w.placeholder) == ("com.acme", "seed", "2.3", False)
    pom = (ws.root / "shop-back/pom.xml").read_text()
    assert "<groupId>com.acme</groupId>" in pom and "<version>2.3</version>" in pom
    assert "com.acme:seed:2.3 (pom_properties) is not in the repository" in pom
    assert bundled_jar_followup(w).startswith("bundled jar `acme-seed-1.0.jar` (pom_properties coordinates, not in "
                                              "the repository) wired into `shop-back/pom.xml` as a system dependency "
                                              "`com.acme:seed:2.3` — ")


def test_wire_compile_when_the_repository_has_it(tmp_path):
    ws = _ws(tmp_path)
    jar = _seed(group_id="com.acme", artifact_id="seed", version="2.3", source="pom_properties")
    w = wire_bundled_jar(ws, None, module_pom="shop-back/pom.xml", jar=jar, repository_has=True,
                         boot_poms=["shop-boot/pom.xml"])
    assert w.scope == "compile" and not w.copied_to and not w.boot_poms
    assert not (ws.root / "shop-back/lib").exists()
    pom = (ws.root / "shop-back/pom.xml").read_text()
    assert ("<dependency>\n            <groupId>com.acme</groupId>\n            <artifactId>seed</artifactId>\n"
            "            <version>2.3</version>\n        </dependency>") in pom
    assert "systemPath" not in pom and "<!--" not in pom
    assert "includeSystemScope" not in (ws.root / "shop-boot/pom.xml").read_text()
    assert bundled_jar_followup(w) == ("bundled jar `acme-seed-1.0.jar` wired into `shop-back/pom.xml` as a compile "
                                       "dependency `com.acme:seed:2.3` (the repository has it)")


def test_followup_text_system(tmp_path):
    ws = _ws(tmp_path)
    w = wire_bundled_jar(ws, None, module_pom="shop-back/pom.xml", jar=_seed(), repository_has=False,
                         project_group="com.example")
    assert bundled_jar_followup(w) == (
        "bundled jar `acme-seed-1.0.jar` (Maven coordinates unknown — no pom.properties, not in the jar name table) "
        "wired into `shop-back/pom.xml` as a system dependency `com.example:acme-seed:1.0` — " + BUNDLED_TAIL)


def test_name_clash_gets_an_artifact_id_prefixed_copy(tmp_path):
    ws = _ws(tmp_path)
    _jar(ws.root / "shop-back/lib/acme-seed-1.0.jar", entry="other/Other.class")
    w = wire_bundled_jar(ws, None, module_pom="shop-back/pom.xml", jar=_seed(), repository_has=False,
                         project_group="com.example")
    assert w.copied_to == "shop-back/lib/acme-seed-acme-seed-1.0.jar"
    assert w.system_path == "${project.basedir}/lib/acme-seed-acme-seed-1.0.jar"
    assert (ws.root / w.copied_to).is_file()


def test_ga_clash_with_another_jar_gets_a_sha1_tail(tmp_path):
    ws = _ws(tmp_path)
    pom = ws.root / "shop-back/pom.xml"
    pom.write_text(pom.read_text().replace(
        "    </dependencies>",
        "        <dependency>\n            <groupId>com.example</groupId>\n            <artifactId>acme-seed</artifactId>\n"
        "            <version>1.0</version>\n            <scope>system</scope>\n"
        "            <systemPath>${project.basedir}/lib/other.jar</systemPath>\n        </dependency>\n    </dependencies>"))
    w = wire_bundled_jar(ws, None, module_pom="shop-back/pom.xml", jar=_seed(sha1="abcdef0123456789"),
                         repository_has=False, project_group="com.example")
    assert w.artifact_id == "acme-seed-abcdef0"
    assert "<artifactId>acme-seed-abcdef0</artifactId>" in pom.read_text()


def test_jar_at_its_as_is_path_and_root_module(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "pom.xml").write_text(BACK_POM)
    _jar(root / AS_IS)
    ws = Workspace(root, tmp_path / "run", "r1")
    w = wire_bundled_jar(ws, None, module_pom="pom.xml", jar=_seed(), repository_has=False)
    assert w.copied_to == "lib/acme-seed-1.0.jar" and (root / "lib/acme-seed-1.0.jar").is_file()
    assert w.group_id == "com.example"          # the pom's own groupId when the caller gives none


def test_unparsable_pom_raises_value_error(tmp_path):
    ws = _ws(tmp_path)
    (ws.root / "shop-back/pom.xml").write_text("<project><broken></project>")
    with pytest.raises(ValueError):
        wire_bundled_jar(ws, None, module_pom="shop-back/pom.xml", jar=_seed(), repository_has=False)


def test_missing_jar_raises_value_error(tmp_path):
    ws = _ws(tmp_path)
    with pytest.raises(ValueError):
        wire_bundled_jar(ws, None, module_pom="shop-back/pom.xml",
                         jar=JarCoordinate(path="WEB-INF/lib/nowhere.jar"), repository_has=False)


def test_ensure_include_system_scope(tmp_path):
    ws = _ws(tmp_path)
    assert ensure_include_system_scope(ws, "shop-boot/pom.xml") is True
    assert ensure_include_system_scope(ws, "shop-boot/pom.xml") is False
    assert ensure_include_system_scope(ws, "shop-back/pom.xml") is False      # no Boot plugin
    pom = ws.root / "shop-boot/pom.xml"
    pom.write_text(BOOT_POM.replace("</artifactId>\n            </plugin>",
                                    "</artifactId>\n                <configuration>\n"
                                    "                    <mainClass>a.B</mainClass>\n"
                                    "                </configuration>\n            </plugin>"))
    assert ensure_include_system_scope(ws, "shop-boot/pom.xml") is True
    text = pom.read_text()
    assert text.count("<configuration>") == 1 and "<mainClass>a.B</mainClass>" in text
    assert "<includeSystemScope>true</includeSystemScope>" in text


def test_copy_helper(tmp_path):
    ws = _ws(tmp_path)
    src = f"shop-back/{AS_IS}"
    assert copy_jar_to_module_lib(ws, src, "shop-back", "seed") == ("shop-back/lib/acme-seed-1.0.jar",
                                                                    "acme-seed-1.0.jar", True)
    # same content already there → no second copy
    assert copy_jar_to_module_lib(ws, src, "shop-back", "seed") == ("shop-back/lib/acme-seed-1.0.jar",
                                                                    "acme-seed-1.0.jar", False)
    # already in place
    assert copy_jar_to_module_lib(ws, "shop-back/lib/acme-seed-1.0.jar", "shop-back", "seed")[2] is False


def test_webinf_left_followup():
    assert webinf_left_followup(["w/META-INF/x.txt"], "shop-back/src/main/webapp") == (
        "1 file(s) under WEB-INF/lib, WEB-INF/classes or META-INF stay in shop-back/src/main/webapp (not packaged): "
        "declare the jars as Maven dependencies / move the classes into src/main/java")
    left = [f"w/WEB-INF/lib/j{i}.jar" for i in range(7)] + ["w/WEB-INF/classes/a/A.class"]
    line = webinf_left_followup(left, "w")
    assert line.startswith("8 file(s) under WEB-INF/lib, WEB-INF/classes or META-INF stay in w (not packaged): "
                           "jar(s) `j0.jar`, `j1.jar`, `j2.jar`, `j3.jar`, `j4.jar` … — sbmig wires a bundled jar")
    assert "(`sbmig-bundled`)" in line and line.endswith("; move the classes into src/main/java")
    assert "\n" not in line
    assert not webinf_left_followup(left[:2], "w").endswith("src/main/java")
