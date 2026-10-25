"""Edge-case handlers (docs/ERROR-HANDLING.md §2: E1 E2 E4 E5 E6 E7)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sbmig.core.config import load_config
from sbmig.core.models import GAV, ErrorKind, PomInfo
from sbmig.triage import HandlerRegistry, cluster_errors
from sbmig.triage.handlers.edge_handlers import _range_admits, edge_handlers, java_charset

from .conftest import make_profile
from .fakes import err

G = GAV.parse
REPO_ROOT = Path(__file__).resolve().parents[2]
JAVA_KO = "package com.acme;\n// 한글 주석\npublic class {name} {{ String s = \"안녕\"; }}\n"


@pytest.fixture()
def ecfg():
    return load_config(REPO_ROOT / "config" / "artifacts.local-e2e.yml", repo_root=REPO_ROOT)


def _src(kit, rel: str, enc: str = "cp949", bom: bool = False, text: str | None = None) -> str:
    p = kit.root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    body = (text if text is not None else JAVA_KO.format(name=Path(rel).stem)).encode(enc)
    p.write_bytes((b"\xef\xbb\xbf" if bom else b"") + body)
    return rel


def _profile(kit, **kw):
    return make_profile(root=str(kit.root), **kw)


def test_order_and_registry():
    ids = [h.id for h in edge_handlers(None)]
    assert ids == ["source-encoding", "strip-utf8-bom", "carry-over-managed-version", "enforcer-relax",
                   "remove-werror", "remove-tools-jar", "systempath-relocate"]       # round 10: remove-tools-jar
    reg = HandlerRegistry.default(None).ids()
    assert reg[:7] == ids and reg.index("carry-over-managed-version") < reg.index("missing-dependency-version")


def test_helpers():
    assert java_charset("cp949") == "MS949" and java_charset("euc-kr") == "EUC-KR"
    assert java_charset("latin-1") == "ISO-8859-1" and java_charset("shift_jis") == "Shift_JIS"
    assert not _range_admits("[1.8,9)", "21") and _range_admits("[17,)", "21") and _range_admits("1.8", "21")
    assert not _range_admits("[1.8,21)", "21") and _range_admits("[11,21]", "21") and not _range_admits("${j}", "21")
    assert not _range_admits("(21,)", "21") and not _range_admits("22", "21")


# ---------------------------------------------------------------------------- E1 source-encoding
UNMAP = "unmappable character (0xC7) for encoding UTF-8"


def test_source_encoding_set_property(kit, ecfg):
    kit.pom(plugins=[], extra_build="""
        <plugin>
          <groupId>org.apache.maven.plugins</groupId>
          <artifactId>maven-compiler-plugin</artifactId>
          <configuration><encoding>UTF-8</encoding></configuration>
        </plugin>""")
    a = _src(kit, "src/main/java/com/acme/A.java")
    _src(kit, "src/main/java/com/acme/B.java")
    _src(kit, "src/main/java/com/acme/C.java", enc="utf-8", text="package com.acme; class C {}\n")
    before = (kit.root / a).read_bytes()
    h = kit.handler("source-encoding")
    c = cluster_errors([err(file=a, message=UNMAP, line=2)])[0]
    assert h.matches(c, _profile(kit), ecfg)
    ch = h.apply(kit.ws, c, _profile(kit), ecfg)
    assert [x.path for x in ch] == ["pom.xml"]
    ed = kit.read()
    assert ed.get_property("project.build.sourceEncoding") == "MS949"
    assert ed.get_property("project.reporting.outputEncoding") == "MS949"
    assert "<encoding>${project.build.sourceEncoding}</encoding>" in kit.text()
    assert (kit.root / a).read_bytes() == before                         # sources untouched
    assert h.flags == ["source-encoding-set:cp949"]
    assert h.apply(kit.ws, c, _profile(kit), ecfg) == [] and h.flags == []    # idempotent
    other = cluster_errors([err(file=a, message="cannot find symbol")])[0]
    assert not h.matches(other, _profile(kit), ecfg)


def test_source_encoding_convert_utf8(kit, ecfg):
    ecfg.build.source_encoding_policy = "convert-utf8"
    kit.pom()
    a = _src(kit, "src/main/java/com/acme/A.java", enc="euc-kr")
    b = _src(kit, "src/test/java/com/acme/BTest.java")
    c = cluster_errors([err(file=a, message=UNMAP)])[0]
    h = kit.handler("source-encoding")
    ch = sorted(x.path for x in h.apply(kit.ws, c, _profile(kit), ecfg))
    assert ch == sorted(["pom.xml", a, b])
    assert "한글 주석" in (kit.root / a).read_bytes().decode("utf-8")
    assert "안녕" in (kit.root / b).read_bytes().decode("utf-8")
    assert kit.read().get_property("project.build.sourceEncoding") == "UTF-8"
    assert h.flags == ["source-encoding-converted:cp949"]
    assert h.apply(kit.ws, c, _profile(kit), ecfg) == []


def test_source_encoding_mixed_converts_legacy(kit, ecfg):
    kit.pom()
    a = _src(kit, "src/main/java/com/acme/A.java")
    _src(kit, "src/main/java/com/acme/U.java", enc="utf-8")            # non-ascii UTF-8 file
    h = kit.handler("source-encoding")
    ch = {x.path for x in h.apply(kit.ws, cluster_errors([err(file=a, message=UNMAP)])[0], _profile(kit), ecfg)}
    assert a in ch and "pom.xml" in ch
    assert (kit.root / a).read_bytes().decode("utf-8").count("한글") == 1
    assert kit.read().get_property("project.build.sourceEncoding") == "UTF-8"
    assert h.notes and "mixed" in h.notes[0]


def test_source_encoding_preemptive_and_module_override(kit, ecfg):
    kit.pom(props={"project.build.sourceEncoding": "UTF-8"}, packaging="pom")
    kit.pom("svc/pom.xml", props={"project.build.sourceEncoding": "UTF-8"}, artifact="svc")
    _src(kit, "svc/src/main/java/com/acme/A.java")
    h = kit.handler("source-encoding")
    ch = sorted(x.path for x in h.apply(kit.ws, None, _profile(kit), ecfg))
    assert ch == ["pom.xml", "svc/pom.xml"]
    assert kit.read("svc/pom.xml").get_property("project.build.sourceEncoding") == "MS949"
    assert h.apply(kit.ws, None, _profile(kit), ecfg) == []


def test_source_encoding_all_utf8_no_change(kit, ecfg):
    kit.pom()
    a = _src(kit, "src/main/java/com/acme/A.java", enc="utf-8")
    h = kit.handler("source-encoding")
    assert h.apply(kit.ws, None, None, ecfg) == []
    assert h.apply(kit.ws, cluster_errors([err(file=a, message=UNMAP)])[0], None, ecfg) == []


# ---------------------------------------------------------------------------- E2 BOM
def test_strip_bom(kit):
    kit.pom()
    a = _src(kit, "src/main/java/com/acme/A.java", enc="utf-8", bom=True)
    b = _src(kit, "src/main/java/com/acme/B.java", enc="utf-8", bom=True)
    plain = _src(kit, "src/main/java/com/acme/P.java", enc="utf-8")
    h = kit.handler("strip-utf8-bom")
    c = cluster_errors([err(file=a, message="illegal character: '\\ufeff'", line=1),
                        err(file=a, message="class, interface, enum, or record expected", line=1)])[0]
    assert h.matches(c, None, None)
    ch = sorted(x.path for x in h.apply(kit.ws, c, None, None))
    assert ch == sorted([a, b]) and plain not in ch
    assert not (kit.root / a).read_bytes().startswith(b"\xef\xbb\xbf")
    assert (kit.root / a).read_bytes().decode("utf-8") == JAVA_KO.format(name="A")
    assert h.flags == ["utf8-bom-stripped:2"]
    assert h.apply(kit.ws, c, None, None) == []
    old = cluster_errors([err(file=a, message="illegal character: \\65279")])[0]
    assert h.matches(old, None, None)


# ---------------------------------------------------------------------------- E6 carry-over
MISSING = "'dependencies.dependency.version' for {}:jar is missing. @ line 30, column 17"


def _effective(kit, data: dict):
    d = kit.root.parent / "effective"            # Workspace(root, root.parent / "run") in the kit … and profile
    d.mkdir(parents=True, exist_ok=True)         # root.parent / effective (matches() has no ws)
    (d / "depmgmt.json").write_text(json.dumps(data), encoding="utf-8")
    run = kit.ws.run_dir / "effective"
    run.mkdir(parents=True, exist_ok=True)
    (run / "depmgmt.json").write_text(json.dumps(data), encoding="utf-8")


def test_carry_over_from_effective_pom(kit, ecfg):
    kit.pom(deps=[G("com.company:legacy-util"), G("org.projectlombok:lombok"), G("org.springframework.boot:spring-boot-starter-web")])
    _effective(kit, {"managed": {"com.company:legacy-util": "2.3.1", "org.projectlombok:lombok": "1.18.20",
                                 "org.springframework.boot:spring-boot-starter-web": "2.7.18"},
                     "direct": {}, "by_module": {"com.example:demo": {"managed": {"com.company:legacy-util": "2.4.0"}}}})
    h = kit.handler("carry-over-managed-version")
    c = cluster_errors([err(ErrorKind.POM_INVALID, MISSING.format("com.company:legacy-util"), file="pom.xml",
                            artifact="com.company:legacy-util", module="demo-comp"),
                        err(ErrorKind.POM_INVALID, MISSING.format("org.projectlombok:lombok"), file="pom.xml",
                            artifact="org.projectlombok:lombok", module="demo-comp")])
    prof = _profile(kit)
    assert all(h.matches(x, prof, ecfg) for x in c)
    ch = []
    for x in c:
        ch += h.apply(kit.ws, x, prof, ecfg)
    assert {x.path for x in ch} == {"pom.xml"}
    assert kit.dep("com.company:legacy-util").version == "2.4.0"           # module's own chain (suffix stripped)
    assert kit.dep("org.projectlombok:lombok").version == "1.18.38"        # as-is below the known-good floor
    assert h.notes and all("pinned to" in n and "as-is parent" in n for n in h.notes)
    boot = cluster_errors([err(ErrorKind.POM_INVALID, MISSING.format("org.springframework.boot:spring-boot-starter-web"),
                               artifact="org.springframework.boot:spring-boot-starter-web")])[0]
    assert not h.matches(boot, prof, ecfg)                                 # never pins the old Boot line
    assert h.apply(kit.ws, c[0], prof, ecfg) == []                         # idempotent


def test_carry_over_defers_without_effective_data(kit, ecfg):
    kit.pom(deps=[G("org.apache.commons:commons-lang3"), G("com.unknown:thing")])
    h = kit.handler("carry-over-managed-version")
    prof = _profile(kit)
    c = cluster_errors([err(ErrorKind.POM_INVALID, MISSING.format("org.apache.commons:commons-lang3"),
                            artifact="org.apache.commons:commons-lang3")])[0]
    assert not h.matches(c, prof, ecfg)            # generic missing-dependency-version handles versions.yml
    assert kit.registry.matching(c, prof, ecfg)[0].id == "missing-dependency-version"
    # apply directly still falls back to versions.yml
    assert h.apply(kit.ws, c, prof, ecfg)
    assert kit.dep("org.apache.commons:commons-lang3").version == "3.20.0"
    unk = cluster_errors([err(ErrorKind.POM_INVALID, MISSING.format("com.unknown:thing"),
                              artifact="com.unknown:thing")])[0]
    assert h.apply(kit.ws, unk, prof, ecfg) == []
    assert not kit.registry.matching(unk, prof, ecfg) or \
        kit.registry.matching(unk, prof, ecfg)[0].id != "carry-over-managed-version"


def test_carry_over_managed_section_and_child(kit, ecfg):
    kit.pom(managed=[G("com.company:bom-lib")], packaging="pom")
    kit.pom("svc/pom.xml", deps=[G("com.company:svc-lib")], artifact="svc")
    _effective(kit, {"managed": {"com.company:bom-lib": "1.0", "com.company:svc-lib": "3.0"}, "direct": {}})
    h = kit.handler("carry-over-managed-version")
    prof = _profile(kit)
    c1 = cluster_errors([err(ErrorKind.POM_INVALID, "'dependencyManagement.dependencies.dependency.version' for "
                             "com.company:bom-lib:jar is missing.", artifact="com.company:bom-lib")])[0]
    c2 = cluster_errors([err(ErrorKind.POM_INVALID, MISSING.format("com.company:svc-lib"), file="svc/pom.xml",
                             artifact="com.company:svc-lib", module="svc")])[0]
    assert h.matches(c1, prof, ecfg) and h.matches(c2, prof, ecfg)
    assert [x.path for x in h.apply(kit.ws, c1, prof, ecfg)] == ["pom.xml"]
    assert kit.dep("com.company:bom-lib", managed=True).version == "1.0"
    assert [x.path for x in h.apply(kit.ws, c2, prof, ecfg)] == ["svc/pom.xml"]
    assert kit.dep("com.company:svc-lib", "svc/pom.xml").version == "3.0"
    assert h.flags == ["managed-versions-carried-over"]


# ---------------------------------------------------------------------------- E4 enforcer
ENFORCER_BUILD = """
        <plugin>
          <groupId>org.apache.maven.plugins</groupId>
          <artifactId>maven-enforcer-plugin</artifactId>
          <version>3.5.0</version>
          <executions>
            <execution>
              <id>enforce-java</id>
              <goals><goal>enforce</goal></goals>
              <configuration>
                <rules>
                  <requireJavaVersion><version>[1.8,9)</version></requireJavaVersion>
                </rules>
              </configuration>
            </execution>
            <execution>
              <id>enforce-convergence</id>
              <goals><goal>enforce</goal></goals>
              <configuration>
                <rules><dependencyConvergence/></rules>
              </configuration>
            </execution>
          </executions>
        </plugin>"""
JAVA_RULE = ("RequireJavaVersion: Detected JDK version 21.0.1 (JAVA_HOME=/x) is not in the allowed range [1.8,9).")
CONV_RULE = "DependencyConvergence: Failed while enforcing releasability. Dependency convergence error for a:b:1"


def _enf(msg: str):
    return cluster_errors([err(ErrorKind.ENFORCER, msg, module="demo",
                               raw="[ERROR] Rule 0: org.apache.maven.enforcer.rules.version." + msg.split(":")[0]
                                   + " failed with message:")])[0]


def test_enforcer_relax_java_and_convergence(kit, ecfg):
    kit.pom(extra_build=ENFORCER_BUILD)
    h = kit.handler("enforcer-relax")
    c = _enf(JAVA_RULE)
    assert h.matches(c, None, ecfg)
    assert [x.path for x in h.apply(kit.ws, c, None, ecfg)] == ["pom.xml"]
    t = kit.text()
    assert "<requireJavaVersion><version>[21,)</version></requireJavaVersion>" in t
    assert h.flags == ["enforcer-relaxed"]
    assert h.apply(kit.ws, c, None, ecfg) == []                            # admits 21 now → no change
    conv = _enf(CONV_RULE)
    assert [x.path for x in h.apply(kit.ws, conv, None, ecfg)] == ["pom.xml"]
    t = kit.text()
    conv_exec = t[t.index("<id>enforce-convergence</id>"):]
    java_exec = t[t.index("<id>enforce-java</id>"):t.index("<id>enforce-convergence</id>")]
    assert "<skip>true</skip>" in conv_exec[:conv_exec.index("</execution>")]
    assert "<skip>" not in java_exec
    assert kit.read().get_property("enforcer.skip") is None
    assert h.apply(kit.ws, conv, None, ecfg) == []


def test_enforcer_rule_from_parent_falls_back_to_skip(kit, ecfg):
    kit.pom()
    h = kit.handler("enforcer-relax")
    c = _enf(JAVA_RULE)
    assert [x.path for x in h.apply(kit.ws, c, None, ecfg)] == ["pom.xml"]
    assert kit.read().get_property("enforcer.skip") == "true"
    assert h.flags == ["enforcer-skipped"]
    assert h.apply(kit.ws, c, None, ecfg) == []


def test_enforcer_policies(kit, ecfg):
    kit.pom(extra_build=ENFORCER_BUILD)
    h = kit.handler("enforcer-relax")
    ecfg.build.enforcer_policy = "keep"
    assert not h.matches(_enf(JAVA_RULE), None, ecfg)
    ecfg.build.enforcer_policy = "skip"
    assert h.matches(_enf(JAVA_RULE), None, ecfg)
    h.apply(kit.ws, _enf(JAVA_RULE), None, ecfg)
    assert kit.read().get_property("enforcer.skip") == "true" and "[1.8,9)" in kit.text()
    assert h.flags == ["enforcer-skipped"]
    compile_c = cluster_errors([err(file="A.java", message="cannot find symbol")])[0]
    assert not h.matches(compile_c, None, ecfg)


def test_enforcer_plugin_level_configuration(kit, ecfg):
    kit.pom(extra_build="""
        <plugin>
          <artifactId>maven-enforcer-plugin</artifactId>
          <configuration>
            <rules><bannedDependencies><excludes><exclude>x:y</exclude></excludes></bannedDependencies></rules>
          </configuration>
          <executions><execution><goals><goal>enforce</goal></goals></execution></executions>
        </plugin>""")
    h = kit.handler("enforcer-relax")
    h.apply(kit.ws, _enf("BannedDependencies: Found Banned Dependency: x:y:jar:1"), None, ecfg)
    t = kit.text()
    assert t.index("<skip>true</skip>") < t.index("<executions>")
    assert h.flags == ["enforcer-relaxed"]


# ---------------------------------------------------------------------------- E5 -Werror
WERROR_BUILD = """
        <plugin>
          <groupId>org.apache.maven.plugins</groupId>
          <artifactId>maven-compiler-plugin</artifactId>
          <configuration>
            <compilerArgs>
              <arg>-Xlint:all</arg>
              <arg>-Werror</arg>
            </compilerArgs>
            <compilerArgument>-Xlint:deprecation -Werror</compilerArgument>
            <failOnWarning>true</failOnWarning>
          </configuration>
        </plugin>"""


def _werr():
    return cluster_errors([err(ErrorKind.COMPILE, "Compilation failure", module="demo",
                               raw="[ERROR] Failed to execute goal org.apache.maven.plugins:maven-compiler-plugin:"
                                   "3.14.0:compile (default-compile) on project demo: Compilation failure\n"
                                   "[ERROR] src/main/java/com/acme/A.java: warnings found and -Werror specified")])[0]


def test_remove_werror(kit, ecfg):
    kit.pom(extra_build=WERROR_BUILD, props={"maven.compiler.failOnWarning": "true"})
    h = kit.handler("remove-werror")
    c = _werr()
    assert h.matches(c, None, ecfg)
    assert [x.path for x in h.apply(kit.ws, c, None, ecfg)] == ["pom.xml"]
    t = kit.text()
    assert "-Werror" not in t and "<arg>-Xlint:all</arg>" in t
    assert "<compilerArgument>-Xlint:deprecation</compilerArgument>" in t
    assert "<failOnWarning>false</failOnWarning>" in t
    assert kit.read().get_property("maven.compiler.failOnWarning") == "false"
    assert h.flags == ["werror-removed"]
    assert h.apply(kit.ws, c, None, ecfg) == []


def test_remove_werror_only_pure_werror_clusters(fake_kit, ecfg):
    h = fake_kit.handler("remove-werror")
    mixed = cluster_errors([err(ErrorKind.COMPILE, "warnings found and -Werror specified", file="A.java"),
                            err(ErrorKind.COMPILE, "cannot find symbol", file="A.java")])[0]
    assert not h.matches(mixed, None, ecfg)
    ecfg.build.werror_policy = "keep"
    assert not h.matches(_werr(), None, ecfg)


# ---------------------------------------------------------------------------- E7 systemPath
SYS_DEP = """<dependency>
      <groupId>com.vendor</groupId>
      <artifactId>vlib</artifactId>
      <version>1.0</version>
      <scope>system</scope>
      <systemPath>${project.basedir}/lib/vlib.jar</systemPath>
    </dependency>"""


def _sys_pom(kit, rel: str):
    kit.pom(rel, artifact="comp")
    p = kit.root / rel
    p.write_text(p.read_text().replace("</project>", f"  <dependencies>\n    {SYS_DEP}\n  </dependencies>\n</project>"))


def test_systempath_relocate_after_restructure(kit, ecfg):
    _sys_pom(kit, "module/comp-jar/pom.xml")
    jar = kit.root / "lib" / "vlib.jar"                       # still at the as-is root location
    jar.parent.mkdir(parents=True)
    jar.write_bytes(b"PK")
    prof = _profile(kit)
    c = cluster_errors([err(ErrorKind.DEPENDENCY_RESOLUTION, "Could not find artifact com.vendor:vlib:jar:1.0",
                            artifact="com.vendor:vlib:1.0", module="comp",
                            raw="Could not find artifact com.vendor:vlib:jar:1.0 at specified path /x/lib/vlib.jar")])[0]
    h = kit.handler("systempath-relocate")
    assert h.matches(c, prof, ecfg)
    assert [x.path for x in h.apply(kit.ws, c, prof, ecfg)] == ["module/comp-jar/pom.xml"]
    assert "<systemPath>${project.basedir}/../../lib/vlib.jar</systemPath>" in kit.text("module/comp-jar/pom.xml")
    assert h.flags == ["systempath-relocated"] and any("Nexus" in n for n in h.notes)
    assert h.apply(kit.ws, c, prof, ecfg) == []
    assert not h.matches(c, prof, ecfg)                       # fixed → no longer claims the cluster


def test_systempath_jar_moved_into_comp_jar(kit, ecfg):
    _sys_pom(kit, "pom.xml")                                  # pom still points at <root>/lib, jar moved
    jar = kit.root / "module/comp-jar/lib/vlib.jar"
    jar.parent.mkdir(parents=True)
    jar.write_bytes(b"PK")
    h = kit.handler("systempath-relocate")
    h.apply(kit.ws, None, None, ecfg)
    assert "${project.basedir}/module/comp-jar/lib/vlib.jar" in kit.text()


def test_systempath_unfixable_does_not_claim(kit, ecfg):
    _sys_pom(kit, "pom.xml")
    prof = _profile(kit)
    c = cluster_errors([err(ErrorKind.DEPENDENCY_RESOLUTION, "Could not find artifact com.vendor:vlib:jar:1.0",
                            artifact="com.vendor:vlib:1.0")])[0]
    h = kit.handler("systempath-relocate")
    assert not h.matches(c, prof, ecfg)
    assert h.apply(kit.ws, c, prof, ecfg) == [] and any("not found" in n for n in h.notes)
    pkg = cluster_errors([err(file="src/main/java/A.java", message="package org.hamcrest does not exist")])[0]
    assert not h.matches(pkg, prof, ecfg)
    other = cluster_errors([err(ErrorKind.DEPENDENCY_RESOLUTION, "Could not find artifact x:y:jar:1",
                                artifact="x:y:1")])[0]
    assert not h.matches(other, _profile(kit), ecfg)
