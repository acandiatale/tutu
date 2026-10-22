from __future__ import annotations

import pytest

from sbmig.core.models import GAV, ErrorKind, Phase, PluginInfo, PomInfo
from sbmig.triage import HandlerRegistry, cluster_errors
from sbmig.triage.handlers import all_poms, nearest_pom, poms_for_cluster, version_for, version_lt

from .conftest import make_profile
from .fakes import err

G = GAV.parse


def test_registry_ids_match_planner_contract():
    reg = HandlerRegistry.default(None)
    ids = set(reg.ids())
    planner_ids = {"lombok-version-floor", "mapstruct-version-floor", "querydsl-jakarta-classifier",
                   "maven-compiler-plugin-floor", "surefire-plugin-floor", "javax-servlet-api-to-jakarta",
                   "jaxb-runtime-add", "remove-classic-loader-implementation", "mysql-connector-relocation"}
    assert planner_ids <= ids
    try:
        from sbmig.plan.pre_handlers import HANDLER_IDS
        assert set(HANDLER_IDS) <= ids
    except ImportError:  # pragma: no cover
        pass
    assert {"dependency-relocation", "missing-dependency-version", "test-compile-skip"} <= ids
    assert "lombok-version-floor" in reg and len(reg) == len(list(reg))
    assert reg.get("nope") is None


def test_version_lt():
    assert version_lt("1.18.20", "1.18.38") is True
    assert version_lt("1.18.38", "1.18.38") is False
    assert version_lt("${x}", "1") is None
    assert version_lt("3.8", "3.8.1") is True


def test_lombok_floor_dependency_property_and_annotation_processor(kit):
    kit.pom(deps=[G("org.projectlombok:lombok:${lombok.version}")], props={"lombok.version": "1.18.20"},
            extra_build="""
        <plugin>
          <groupId>org.apache.maven.plugins</groupId>
          <artifactId>maven-compiler-plugin</artifactId>
          <configuration><annotationProcessorPaths><path>
            <groupId>org.projectlombok</groupId>
            <artifactId>lombok</artifactId>
            <version>1.18.10</version>
          </path></annotationProcessorPaths></configuration>
        </plugin>""")
    kit.pom("child/pom.xml", deps=[G("org.projectlombok:lombok:1.16.0")])
    kit.pom("child/src/test/resources/pom.xml")              # test resource poms are ignored
    h = kit.handler("lombok-version-floor")
    changes = h.apply(kit.ws, None, make_profile(), None)
    assert sorted(c.path for c in changes) == ["child/pom.xml", "pom.xml"]
    assert kit.read().get_property("lombok.version") == "1.18.38"
    assert kit.dep("org.projectlombok:lombok", "child/pom.xml").version == "1.18.38"
    assert "<version>1.18.10</version>" not in kit.text()
    # idempotent
    assert h.apply(kit.ws, None, None, None) == []


def test_lombok_matches(fake_kit):
    h = fake_kit.handler("lombok-version-floor")
    c = cluster_errors([err(ErrorKind.PLUGIN_EXECUTION, "Fatal error compiling: java.lang.IllegalAccessError: class "
                            "lombok.javac.apt.LombokProcessor cannot access class com.sun.tools.javac.processing",
                            module="demo")])[0]
    assert h.matches(c, make_profile(libs=[("lombok", "1.18.20")]), None)
    # round 9: the javac-internals wording proves an old lombok is on the processor path whatever the poms declare
    assert h.matches(c, make_profile(libs=[("lombok", "1.18.38")]), None)
    assert h.matches(c, make_profile(libs=[]), None)
    assert h.matches(c, None, None)
    soft = cluster_errors([err(ErrorKind.COMPILE, "cannot find symbol: method builder() — lombok", module="demo")])[0]
    assert h.matches(soft, make_profile(libs=[("lombok", "1.18.20")]), None)
    assert not h.matches(soft, make_profile(libs=[("lombok", "1.18.38")]), None)     # version gate for soft hits
    other = cluster_errors([err(file="A.java", message="cannot find symbol")])[0]
    assert not h.matches(other, make_profile(libs=[("lombok", "1.0")]), None)


def test_mapstruct_floor_managed_and_unversioned(kit):
    kit.pom(deps=[G("org.mapstruct:mapstruct"), G("org.mapstruct:mapstruct-processor:1.4.2.Final")],
            managed=[G("org.mapstruct:mapstruct:1.5.0.Final")])
    ch = kit.handler("mapstruct-version-floor").apply(kit.ws)
    assert ch and kit.dep("org.mapstruct:mapstruct-processor").version == "1.6.3"
    assert kit.dep("org.mapstruct:mapstruct", managed=True).version == "1.6.3"


def test_querydsl_classifier(kit):
    kit.pom(deps=[G("com.querydsl:querydsl-jpa:4.4.0"), G("com.querydsl:querydsl-apt:4.4.0")])
    h = kit.handler("querydsl-jakarta-classifier")
    assert h.preemptive_phase == Phase.SPRING
    assert h.apply(kit.ws)
    d = kit.dep("com.querydsl:querydsl-jpa")
    assert d.version == "5.1.0" and d.classifier == "jakarta"
    assert h.apply(kit.ws) == []
    c = cluster_errors([err(file="A.java", symbol="class QUser")])[0]
    assert h.matches(c, make_profile(libs=[("querydsl", "4")]), None)
    assert not h.matches(c, make_profile(libs=[]), None)


def test_compiler_plugin_floor_and_release(kit, cfg):
    kit.pom(props={"maven.compiler.source": "1.8", "maven.compiler.target": "1.8"},
            plugins=[PluginInfo(artifact_id="maven-compiler-plugin", version="3.8.1",
                                configuration={"source": "1.8", "target": "1.8"})])
    h = kit.handler("maven-compiler-plugin-floor")
    assert h.apply(kit.ws, None, None, cfg)
    ed = kit.read()
    assert ed.get_property("maven.compiler.source") is None
    assert ed.get_property("maven.compiler.release") == "21"
    assert next(p for p in ed.plugins() if p.artifact_id == "maven-compiler-plugin").version == "3.14.0"
    text = kit.text()
    assert "<release>21</release>" in text and "<target>1.8</target>" not in text


def test_compiler_plugin_added_when_absent_and_java_version(kit, cfg):
    kit.pom(props={"java.version": "1.8"})
    h = kit.handler("maven-compiler-plugin-floor")
    assert h.apply(kit.ws, None, None, cfg)
    ed = kit.read()
    assert ed.get_property("java.version") == "21"
    assert any(p.artifact_id == "maven-compiler-plugin" and p.version == "3.14.0" for p in ed.plugins())
    c = cluster_errors([err(ErrorKind.PLUGIN_EXECUTION, "Fatal error compiling: invalid target release: 21")])[0]
    assert h.matches(c, make_profile(), cfg)


def test_compiler_java_version_props_release(fake_kit, cfg):
    fake_kit.pom(props={"java.version": "11", "maven.compiler.source": "${java.version}"},
                 plugins=[PluginInfo(artifact_id="maven-compiler-plugin", version="3.14.0")])
    fake_kit.handler("maven-compiler-plugin-floor").apply(fake_kit.ws, None, None, cfg)
    assert fake_kit.read().get_property("maven.compiler.release") == "${java.version}"
    fake_kit.pom(props={"maven.compiler.release": "17"},
                 plugins=[PluginInfo(artifact_id="maven-compiler-plugin", version="3.14.0")])
    fake_kit.handler("maven-compiler-plugin-floor").apply(fake_kit.ws, None, None, cfg)
    assert fake_kit.read().get_property("maven.compiler.release") == "21"


def test_surefire_floor(kit):
    kit.pom(plugins=[PluginInfo(artifact_id="maven-surefire-plugin", version="2.22.2"),
                     PluginInfo(artifact_id="maven-failsafe-plugin")])
    h = kit.handler("surefire-plugin-floor")
    assert [c.path for c in h.apply(kit.ws)] == ["pom.xml"]
    plugs = {p.artifact_id: p.version for p in kit.read().plugins()}
    assert plugs["maven-surefire-plugin"] == "3.5.3"
    assert plugs["maven-failsafe-plugin"] is None                   # parent-managed: only pinned reactively
    c = cluster_errors([err(ErrorKind.PLUGIN_EXECUTION, "maven-failsafe-plugin:2.22 failed", module="demo")])[0]
    assert h.matches(c, make_profile(), None)
    h.apply(kit.ws, c, make_profile(), None)
    assert {p.artifact_id: p.version for p in kit.read().plugins()}["maven-failsafe-plugin"] == "3.5.3"


def test_servlet_api_to_jakarta(kit, cfg):
    kit.pom(deps=[G("javax.servlet:javax.servlet-api:3.1.0"), G("javax.servlet.jsp:jsp-api:2.1")])
    h = kit.handler("javax-servlet-api-to-jakarta")
    assert h.preemptive_phase == Phase.SPRING
    prof = make_profile(poms=[PomInfo(path="pom.xml", artifact_id="demo",
                                      dependencies=[G("javax.servlet:javax.servlet-api:3.1.0")])])
    assert h.apply(kit.ws, None, prof, cfg)
    d = kit.dep("jakarta.servlet:jakarta.servlet-api")
    assert d.version == "6.1.0" and d.scope == "provided"
    assert kit.dep("jakarta.servlet.jsp:jakarta.servlet.jsp-api").version == "4.0.0"
    assert kit.dep("javax.servlet:javax.servlet-api") is None
    c = cluster_errors([err(file="S.java", message="package jakarta.servlet.http does not exist")])[0]
    assert h.matches(c, prof, cfg)
    no = make_profile(poms=[PomInfo(path="pom.xml", artifact_id="demo")])
    assert not h.matches(c, no, cfg)


def test_mysql_relocation(kit, cfg):
    kit.pom(deps=[G("mysql:mysql-connector-java:8.0.28")])
    h = kit.handler("mysql-connector-relocation")
    prof = make_profile(poms=[PomInfo(path="pom.xml", artifact_id="demo",
                                      dependencies=[G("mysql:mysql-connector-java:8.0.28")])])
    c = cluster_errors([err(ErrorKind.DEPENDENCY_RESOLUTION, "could not find", artifact="mysql:mysql-connector-java:8.0.28")])[0]
    assert h.matches(c, prof, cfg)
    assert h.apply(kit.ws, c, prof, cfg)
    assert kit.dep("com.mysql:mysql-connector-j").version == "9.3.0"


def test_mysql_relocation_managed_versions(fake_kit, cfg):
    fake_kit.pom(deps=[G("mysql:mysql-connector-java")])
    h = fake_kit.handler("mysql-connector-relocation")
    h.apply(fake_kit.ws, None, make_profile(boot="3.2.0"), cfg)
    assert fake_kit.dep("com.mysql:mysql-connector-j").version is None
    fake_kit.pom(deps=[G("mysql:mysql-connector-java")])
    h.apply(fake_kit.ws, None, make_profile(boot="2.1.0"), cfg)
    assert fake_kit.dep("com.mysql:mysql-connector-j").version == "9.3.0"
    c = cluster_errors([err(ErrorKind.COMPILE, "mysql-connector-java old", file="A.java")])[0]
    assert h.matches(c, None, cfg)


def test_dependency_relocation_table(kit, cfg):
    kit.pom(deps=[G("javax.persistence:javax.persistence-api:2.2"), G("org.springframework.boot:spring-boot-starter-web"),
                  G("org.hibernate:hibernate-core:5.6.15.Final")])
    h = kit.handler("dependency-relocation")
    prof = make_profile(poms=[PomInfo(path="pom.xml", artifact_id="demo", dependencies=[
        G("javax.persistence:javax.persistence-api:2.2"), G("org.springframework.boot:spring-boot-starter-web")])])
    pkg = cluster_errors([err(file="E.java", message="package jakarta.persistence does not exist")])[0]
    assert h.matches(pkg, prof, cfg)
    assert h.apply(kit.ws, pkg, prof, cfg)
    assert kit.dep("jakarta.persistence:jakarta.persistence-api").version == "3.2.0"
    assert kit.dep("org.springframework.boot:spring-boot-starter-web") is not None      # only the relevant one
    # pre-emptive: whole table
    h.apply(kit.ws, None, prof, cfg)
    assert kit.dep("org.springframework.boot:spring-boot-starter-webmvc").version is None
    assert kit.dep("org.hibernate.orm:hibernate-core").version == "7.1.0.Final"
    dep = cluster_errors([err(ErrorKind.DEPENDENCY_RESOLUTION, "x", artifact="org.springframework.boot:spring-boot-starter-aop:4.1.0")])[0]
    assert h.matches(dep, prof, cfg)
    cfg.target.spring_boot_version = "3.5.0"
    assert not h.matches(dep, prof, cfg)                                               # min_target_boot


def test_jaxb_runtime_add_flavours(kit):
    kit.pom()
    (kit.root / "src/main/java").mkdir(parents=True)
    (kit.root / "src/main/java/A.java").write_text("import javax.xml.bind.JAXBContext;\nclass A {}\n")
    h = kit.handler("jaxb-runtime-add")
    assert h.apply(kit.ws, None, make_profile(), None)
    assert kit.dep("javax.xml.bind:jaxb-api").version == "2.3.1"
    assert kit.dep("org.glassfish.jaxb:jaxb-runtime").scope == "runtime"
    assert h.apply(kit.ws, None, make_profile(), None) == []
    kit.pom()
    (kit.root / "src/main/java/A.java").write_text("import jakarta.xml.bind.JAXBContext;\nclass A {}\n")
    c = cluster_errors([err(file="src/main/java/A.java", message="package jakarta.xml.bind does not exist")])[0]
    assert h.matches(c, make_profile(), None)
    assert h.apply(kit.ws, c, make_profile(), None)
    assert kit.dep("jakarta.xml.bind:jakarta.xml.bind-api").version == "4.0.2"
    assert kit.dep("org.glassfish.jaxb:jaxb-runtime").version == "4.0.5"


def test_jaxb_targets_without_profile(fake_kit):
    fake_kit.pom(packaging="pom")
    fake_kit.pom("m/pom.xml")
    h = fake_kit.handler("jaxb-runtime-add")
    changed = [c.path for c in h.apply(fake_kit.ws, None, None, None)]
    assert changed == ["m/pom.xml"]


def test_remove_classic_loader(kit):
    kit.pom(extra_build="""
        <plugin>
          <groupId>org.springframework.boot</groupId>
          <artifactId>spring-boot-maven-plugin</artifactId>
          <configuration>
            <loaderImplementation>CLASSIC</loaderImplementation>
          </configuration>
        </plugin>""")
    h = kit.handler("remove-classic-loader-implementation")
    assert h.apply(kit.ws) and "loaderImplementation" not in kit.text()
    c = cluster_errors([err(ErrorKind.PLUGIN_EXECUTION, "Cannot find 'loaderImplementation' in class", module="d")])[0]
    assert h.matches(c, make_profile(), None)


def test_missing_dependency_version(kit, cfg):
    kit.pom(deps=[G("org.apache.commons:commons-lang3"), G("org.springframework.boot:spring-boot-starter")])
    msg = "'dependencies.dependency.version' for org.apache.commons:commons-lang3:jar is missing. @ line 4"
    c = cluster_errors([err(ErrorKind.POM_INVALID, msg, file="pom.xml"),
                        err(ErrorKind.POM_INVALID, "'dependencies.dependency.version' for "
                            "org.springframework.boot:spring-boot-starter:jar is missing.", file="pom.xml")])[0]
    h = kit.handler("missing-dependency-version")
    assert h.matches(c, make_profile(), cfg)
    assert h.apply(kit.ws, c, make_profile(), cfg)
    assert kit.dep("org.apache.commons:commons-lang3").version == "3.17.0"
    assert kit.dep("org.springframework.boot:spring-boot-starter").version == cfg.target.spring_boot_version
    unknown = cluster_errors([err(ErrorKind.POM_INVALID, "'dependencies.dependency.version' for a.b:zz:jar is missing.")])[0]
    assert not h.matches(unknown, make_profile(), cfg)
    assert h.apply(kit.ws, None) == []


def test_test_compile_skip_never_matches(fake_kit):
    h = fake_kit.handler("test-compile-skip")
    c = cluster_errors([err(ErrorKind.TEST_COMPILE, "x", file="T.java")])[0]
    assert not h.matches(c, make_profile(), None) and h.apply(fake_kit.ws) == []


def test_pom_location_helpers(fake_kit):
    fake_kit.pom()
    fake_kit.pom("svc/pom.xml", artifact="svc")
    ws = fake_kit.ws
    assert all_poms(ws) == ["pom.xml", "svc/pom.xml"]
    assert nearest_pom(ws, "svc/src/main/java/A.java") == "svc/pom.xml"
    prof = make_profile(poms=[PomInfo(path="pom.xml", artifact_id="root"), PomInfo(path="svc/pom.xml", artifact_id="svc-app")])
    c = cluster_errors([err(ErrorKind.ENFORCER, "x", module="svc-app")])[0]
    assert poms_for_cluster(ws, c, prof) == ["svc/pom.xml"]
    c = cluster_errors([err(ErrorKind.ENFORCER, "x", module="svc")])[0]
    assert poms_for_cluster(ws, c, None) == ["svc/pom.xml"]
    assert poms_for_cluster(ws, None, None) == ["pom.xml"]
    assert version_for("org.springframework.boot:x", None) is None


def test_registry_default_factory_is_real_editor():
    reg = HandlerRegistry.default(None)
    try:
        f = reg.pom_editor_factory
    except RuntimeError:  # pragma: no cover - workstream B missing
        pytest.skip("PomEditor not available")
    assert callable(f)
    reg.pom_editor_factory = None


# ---------------------------------------------------------------------------------------------- add-known-dependency
def test_add_known_dependency_hamcrest_test_scope_managed(kit):
    """`cannot access org.hamcrest.Matcher` in a test → hamcrest test dep, version omitted under a Boot parent."""
    kit.pom(deps=[G("org.springframework.boot:spring-boot-starter-test")])
    txt = kit.text().replace("<modelVersion>4.0.0</modelVersion>",
                             "<modelVersion>4.0.0</modelVersion><parent><groupId>org.springframework.boot</groupId>"
                             "<artifactId>spring-boot-starter-parent</artifactId><version>4.1.0</version></parent>")
    (kit.root / "pom.xml").write_text(txt, encoding="utf-8")
    h = kit.handler("add-known-dependency")
    c = cluster_errors([err(kind=ErrorKind.TEST_COMPILE, file="src/test/java/A.java",
                            message="cannot access org.hamcrest.Matcher",
                            raw="[ERROR] /x/src/test/java/A.java:[38,5] cannot access org.hamcrest.Matcher\n"
                                "[ERROR]   class file for org.hamcrest.Matcher not found")])[0]
    assert h.matches(c, make_profile(), None)
    assert [ch.path for ch in h.apply(kit.ws, c, make_profile(), None)] == ["pom.xml"]
    d = kit.dep("org.hamcrest:hamcrest")
    assert d is not None and d.scope == "test" and d.version is None
    assert h.apply(kit.ws, c, make_profile(), None) == []          # idempotent


def test_add_known_dependency_jackson2_bridge_under_boot_bom(kit):
    """`package org.springframework.boot.jackson2 does not exist` → spring-boot-jackson2 (compile scope, managed)."""
    kit.pom(deps=[G("org.springframework.boot:spring-boot-starter-webmvc")],
            managed=[GAV(group_id="org.springframework.boot", artifact_id="spring-boot-dependencies",
                         version="4.1.0", type="pom", scope="import")])
    h = kit.handler("add-known-dependency")
    c = cluster_errors([err(file="src/main/java/Money.java",
                            message="package org.springframework.boot.jackson2 does not exist")])[0]
    assert h.matches(c, make_profile(), None)
    assert h.apply(kit.ws, c, make_profile(), None)
    d = kit.dep("org.springframework.boot:spring-boot-jackson2")
    assert d is not None and d.scope is None and d.version is None


def test_add_known_dependency_unmanaged_project_uses_table_version(kit, cfg):
    """No Boot parent/BOM: managed rows fall back to the table / versions.yml / target boot version."""
    kit.pom(deps=[G("org.apache.commons:commons-lang3:3.12.0")])
    h = kit.handler("add-known-dependency")
    c = cluster_errors([err(kind=ErrorKind.TEST_COMPILE, file="src/test/java/T.java",
                            message="package org.hamcrest does not exist")])[0]
    assert h.apply(kit.ws, c, make_profile(), cfg)
    assert kit.dep("org.hamcrest:hamcrest").version == "3.0"
    c2 = cluster_errors([err(file="src/main/java/Money.java",
                             message="package org.springframework.boot.jackson2 does not exist")])[0]
    assert h.apply(kit.ws, c2, make_profile(), cfg)
    assert kit.dep("org.springframework.boot:spring-boot-jackson2").version == cfg.target.spring_boot_version


def test_add_known_dependency_symbol_filter_and_no_match(kit):
    kit.pom()
    h = kit.handler("add-known-dependency")
    # javax.annotation.PostConstruct is NOT a jsr305 symbol → no match (the jakarta recipe handles it)
    c = cluster_errors([err(file="src/main/java/A.java", message="cannot find symbol", symbol="class PostConstruct",
                            raw="[ERROR] A.java:[3,24] cannot find symbol\n[ERROR]   symbol:   class PostConstruct\n"
                                "[ERROR]   location: package javax.annotation")])[0]
    assert not h.matches(c, make_profile(), None)
    c2 = cluster_errors([err(file="src/main/java/A.java", message="cannot find symbol", symbol="class Nullable",
                             raw="[ERROR] A.java:[3,24] cannot find symbol\n[ERROR]   symbol:   class Nullable\n"
                                 "[ERROR]   location: package javax.annotation")])[0]
    assert h.matches(c2, make_profile(), None)
    assert h.apply(kit.ws, c2, make_profile(), None)
    assert kit.dep("com.google.code.findbugs:jsr305").version == "3.0.2"
    c3 = cluster_errors([err(file="src/main/java/A.java", message="package com.acme.internal does not exist")])[0]
    assert not h.matches(c3, make_profile(), None)


_LEGACY_COMPILER_BLOCK = """
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-compiler-plugin</artifactId>
        <version>2.3.2</version>
        <configuration>
          <source>1.6</source>
          <target>1.6</target>
          <encoding>MS949</encoding>
          <fork>true</fork>
          <executable>/usr/java/jdk1.6.0_45/bin/javac</executable>
          <compilerVersion>1.6</compilerVersion>
          <compilerArguments>
            <bootclasspath>${java.home}/lib/rt.jar</bootclasspath>
            <verbose />
          </compilerArguments>
          <compilerArgument>-Xbootclasspath/p:lib/patch.jar</compilerArgument>
          <compilerArgs>
            <arg>-bootclasspath</arg>
            <arg>/usr/java/jdk1.6.0_45/jre/lib/rt.jar</arg>
            <arg>-Xlint:unchecked</arg>
            <arg>-extdirs</arg>
            <arg>lib/ext</arg>
          </compilerArgs>
        </configuration>
      </plugin>"""


def test_compiler_legacy_config_removes_fork_executable_and_bootclasspath(kit, cfg):
    """Round 9: javac 21 rejects -bootclasspath / -Xbootclasspath / -extdirs with --release and the forked javac of a
    JDK 6 that is not installed; only those elements go, the rest of the block (encoding, -Xlint) stays."""
    kit.pom(extra_build=_LEGACY_COMPILER_BLOCK)
    h = kit.handler("compiler-legacy-config")
    assert h.apply(kit.ws, None, None, cfg)
    text = kit.text()
    for gone in ("<fork>", "<executable>", "<compilerVersion>", "<bootclasspath>", "<compilerArgument>",
                 "-bootclasspath", "rt.jar", "-extdirs", "lib/ext"):
        assert gone not in text, gone
    assert "<encoding>MS949</encoding>" in text and "<arg>-Xlint:unchecked</arg>" in text
    assert "<compilerArguments>" in text and "<verbose />" in text          # a non-legacy child keeps the container
    assert text.count("<compilerArgs>") == 1 and text.count("<arg>") == 1
    assert not h.apply(kit.ws, None, None, cfg)                             # idempotent


def test_compiler_legacy_config_drops_empty_containers_and_matches(fake_kit, cfg):
    fake_kit.pom(extra_build="""
      <plugin>
        <artifactId>maven-compiler-plugin</artifactId>
        <configuration>
          <compilerArguments>
            <bootclasspath>${java.home}/lib/rt.jar</bootclasspath>
          </compilerArguments>
          <compilerArgs>
            <arg>-Xbootclasspath/a:lib/x.jar</arg>
          </compilerArgs>
        </configuration>
      </plugin>""")
    h = fake_kit.handler("compiler-legacy-config")
    assert h.apply(fake_kit.ws, None, None, cfg)
    text = fake_kit.text()
    assert "<compilerArguments>" not in text and "<compilerArgs>" not in text and "<configuration>" in text
    for msg in ("error: option --boot-class-path cannot be used together with --release",
                "error: option -Xbootclasspath/p: cannot be used together with --release",
                "error: option --boot-class-path not allowed with target 21",
                "Fatal error compiling: Cannot run program \"/usr/java/jdk1.6.0_45/bin/javac\""):
        c = cluster_errors([err(ErrorKind.COMPILE, msg)])[0]
        assert h.matches(c, make_profile(), cfg), msg
    c = cluster_errors([err(ErrorKind.COMPILE, "cannot find symbol")])[0]
    assert not h.matches(c, make_profile(), cfg)


def test_compiler_legacy_config_leaves_clean_poms_alone(fake_kit, cfg):
    fake_kit.pom(plugins=[PluginInfo(artifact_id="maven-compiler-plugin", version="3.14.0",
                                     configuration={"release": "21", "encoding": "UTF-8"})])
    before = fake_kit.text()
    assert not fake_kit.handler("compiler-legacy-config").apply(fake_kit.ws, None, None, cfg)
    assert fake_kit.text() == before



_LOMBOK_ERR = ("java.lang.IllegalAccessError: class lombok.javac.apt.LombokProcessor (in unnamed module @0x1) cannot "
               "access class com.sun.tools.javac.processing.JavacProcessingEnvironment (in module jdk.compiler)")


def test_lombok_hard_error_pins_unversioned_processor_path(kit, cfg):
    """Round 9: the declared lombok is fine (1.18.30) but the processor path carries no version (plugin-managed by
    the old parent) → the javac-internals error selects the handler regardless of the profile and the path gets
    the floor version."""
    kit.pom(deps=[G("org.projectlombok:lombok:1.18.30")], extra_build="""
      <plugin>
        <artifactId>maven-compiler-plugin</artifactId>
        <configuration>
          <annotationProcessorPaths>
            <path>
              <groupId>org.projectlombok</groupId>
              <artifactId>lombok</artifactId>
            </path>
          </annotationProcessorPaths>
        </configuration>
      </plugin>""")
    h = kit.handler("lombok-version-floor")
    c = cluster_errors([err(ErrorKind.COMPILE, _LOMBOK_ERR)])[0]
    prof = make_profile(libs=[("lombok", "1.18.30")])
    assert h.matches(c, prof, cfg)
    assert h.apply(kit.ws, c, prof, cfg)
    import re as _re
    from sbmig.triage.handlers.pom_handlers import LOMBOK_VERSION
    text = kit.text()
    assert _re.search(r"<artifactId>lombok</artifactId>\s*<version>" + _re.escape(LOMBOK_VERSION), text)   # path pinned
    # the error persisting after the pin means the parent's list is merged in → the next round cuts the inheritance
    assert h.apply(kit.ws, c, prof, cfg)
    assert 'combine.self="override"' in kit.text()
    assert not h.apply(kit.ws, c, prof, cfg)                      # idempotent from here on


def test_lombok_inherited_from_parent_gets_an_explicit_pin(kit, cfg):
    """No project pom declares lombok (the parent's <dependencies> brings an old one): reactive on the javac error
    and pre-emptive when the imports show lombok → explicit provided 1.18.30 in the root pom."""
    kit.pom()
    h = kit.handler("lombok-version-floor")
    c = cluster_errors([err(ErrorKind.COMPILE, _LOMBOK_ERR)])[0]
    assert h.apply(kit.ws, c, make_profile(), cfg)
    from sbmig.triage.handlers.pom_handlers import LOMBOK_VERSION
    d = kit.dep("org.projectlombok:lombok")
    assert d is not None and d.version == LOMBOK_VERSION and d.scope == "provided"
    kit.pom()                                                     # fresh pom: pre-emptive path (no cluster)
    assert h.apply(kit.ws, None, make_profile(libs=[("lombok", None)]), cfg)
    assert kit.dep("org.projectlombok:lombok").version == LOMBOK_VERSION
    kit.pom()
    assert not h.apply(kit.ws, None, make_profile(), cfg)        # no lombok anywhere → nothing added



_EFFECTIVE_POM = """<?xml version="1.0"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <build>
    <plugins>
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-compiler-plugin</artifactId>
        <version>3.11.0</version>
        <configuration>
          <release>17</release>
          <annotationProcessorPaths>
            <path><groupId>org.projectlombok</groupId><artifactId>lombok</artifactId><version>1.18.12</version></path>
            <path><groupId>org.mapstruct</groupId><artifactId>mapstruct-processor</artifactId><version>1.5.5.Final</version></path>
          </annotationProcessorPaths>
        </configuration>
      </plugin>
    </plugins>
  </build>
</project>
"""


def test_lombok_inherited_processor_paths_are_overridden(kit, cfg):
    """Round 9 (first company run): the project declares lombok 1.18.40, the parent's compiler configuration carries
    an annotationProcessorPaths with lombok 1.18.12 → Maven merges it in and the old processor wins. The handler
    writes the effective list, versions floored, with combine.self="override" into the project's compiler plugin."""
    import re as _re
    (kit.ws.run_dir / "effective").mkdir(parents=True, exist_ok=True)
    (kit.ws.run_dir / "effective" / "root.xml").write_text(_EFFECTIVE_POM, encoding="utf-8")
    kit.pom(deps=[G("org.projectlombok:lombok:1.18.40")], extra_build="""
      <plugin>
        <artifactId>maven-compiler-plugin</artifactId>
        <configuration>
          <release>21</release>
        </configuration>
      </plugin>""")
    h = kit.handler("lombok-version-floor")
    c = cluster_errors([err(ErrorKind.COMPILE, _LOMBOK_ERR)])[0]
    prof = make_profile(libs=[("lombok", "1.18.40"), ("mapstruct", "1.5.5.Final")])
    assert h.apply(kit.ws, c, prof, cfg)
    text = kit.text()
    assert '<annotationProcessorPaths combine.self="override">' in text and "<release>21</release>" in text
    paths = _re.findall(r"<artifactId>([^<]+)</artifactId>\s*<version>([^<]+)</version>\s*</path>", text)
    assert paths == [("lombok", "1.18.40"), ("mapstruct-processor", "1.6.3"), ("lombok-mapstruct-binding", "0.2.0")]
    assert text.count("<annotationProcessorPaths") == 1
    assert not h.apply(kit.ws, c, prof, cfg)                      # idempotent
    # no effective pom and no compiler plugin in the project: lombok (+ mapstruct) override added to the root pom
    # (the plugin block is added through the editor; the fake editor does not write the file → real mode only)
    if kit.mode != "real":
        return
    kit.pom(deps=[G("org.projectlombok:lombok:1.18.40")])
    for x in (kit.ws.run_dir / "effective").glob("*.xml"):
        x.unlink()
    assert h.apply(kit.ws, c, make_profile(libs=[("lombok", "1.18.40"), ("mapstruct", "1.6.3")]), cfg)
    text = kit.text()
    assert 'combine.self="override"' in text and "<artifactId>mapstruct-processor</artifactId>" in text
    assert _re.search(r"<artifactId>lombok</artifactId>\s*<version>1\.18\.40</version>", text)



def test_jstl_to_jakarta_relocation(kit, cfg):
    """Round 9c: javax JSTL (version managed by the old parent chain, so it goes missing after the parent swap) →
    the Jakarta JSTL the Boot 4 BOM manages; an explicit old version becomes the Jakarta version."""
    kit.pom(deps=[G("javax.servlet:jstl"), G("javax.servlet.jsp.jstl:jstl-api:1.2")])
    h = kit.handler("jstl-to-jakarta")
    c = cluster_errors([err(ErrorKind.POM_INVALID, "'dependencies.dependency.version' for javax.servlet:jstl:jar is missing",
                            artifact="javax.servlet:jstl")])[0]
    assert h.matches(c, make_profile(poms=[PomInfo(path="pom.xml", artifact_id="demo", is_root=True,
                                                   dependencies=[G("javax.servlet:jstl")])]), cfg)
    assert h.apply(kit.ws, None, None, cfg)                       # pre-emptive (spring phase)
    assert kit.dep("javax.servlet:jstl") is None and kit.dep("javax.servlet.jsp.jstl:jstl-api") is None
    impl = kit.dep("org.glassfish.web:jakarta.servlet.jsp.jstl")
    api = kit.dep("jakarta.servlet.jsp.jstl:jakarta.servlet.jsp.jstl-api")
    assert impl is not None and impl.version is None              # was unversioned → stays BOM-managed
    assert api is not None and api.version == "3.0.0"             # was explicit 1.2 → the Jakarta line
    assert not h.apply(kit.ws, None, None, cfg)



def test_nearest_pom_absolute_and_backslash_paths_terminate(fake_kit):
    """Round 9c: the handler-side nearest_pom spun forever on an absolute path (dirname('/') == '/')."""
    import time
    fake_kit.pom()
    ws = fake_kit.ws
    t0 = time.monotonic()
    assert nearest_pom(ws, "src/main/java/a/A.java") == "pom.xml"
    assert nearest_pom(ws, "src\\main\\java\\a\\A.java") == "pom.xml"
    assert nearest_pom(ws, str(ws.root / "src" / "main" / "java" / "A.java")) == "pom.xml"
    assert nearest_pom(ws, "/elsewhere/A.java") == "pom.xml"           # not under the root → the root pom
    assert nearest_pom(ws, "C:/elsewhere/A.java") in ("pom.xml", None)
    assert time.monotonic() - t0 < 2.0


# ---------------------------------------------------------------------------
# resources-binary-filtering (E12): binary files under a filtered resource
# ---------------------------------------------------------------------------
_FONT_MSG = ("filtering /w/src/main/resources/static/fonts/fontawesome-webfont.eot to "
             "/w/target/classes/static/fonts/fontawesome-webfont.eot failed with MalformedInputException: Input length = 1")
_NS = {"m": "http://maven.apache.org/POM/4.0.0"}
_WAR_WEBRESOURCES = """
      <plugin>
        <artifactId>maven-war-plugin</artifactId>
        <configuration>
          <webResources>
            <resource>
              <directory>src/main/webapp</directory>
              <filtering>true</filtering>
            </resource>
          </webResources>
        </configuration>
      </plugin>"""


def _filtering_pom(plugin_block: str = "", artifact: str = "demo", filtering: bool = True) -> str:
    res = """
    <resources>
      <resource>
        <directory>src/main/resources</directory>
        <filtering>true</filtering>
      </resource>
    </resources>""" if filtering else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.example</groupId>
  <artifactId>{artifact}</artifactId>
  <version>1.0.0</version>
  <build>{res}
    <plugins>
      <plugin>
        <artifactId>maven-compiler-plugin</artifactId>
      </plugin>{plugin_block}
    </plugins>
  </build>
</project>
"""


def _nfe(text: str, artifact: str = "maven-resources-plugin") -> list[str]:
    """nonFilteredFileExtension entries of the plugin-level configuration, through a real XML parse."""
    import xml.etree.ElementTree as ET
    out: list[str] = []
    for pl in ET.fromstring(text).iterfind(".//m:plugin", _NS):
        if pl.findtext("m:artifactId", namespaces=_NS) != artifact:
            continue
        out += [(e.text or "").strip() for e in
                pl.iterfind("m:configuration/m:nonFilteredFileExtensions/m:nonFilteredFileExtension", _NS)]
    return out


def _font_cluster(module: str = "demo", goal: str = "resources:resources", msg: str = _FONT_MSG, raw: str = ""):
    c = cluster_errors([err(ErrorKind.PLUGIN_EXECUTION, msg, module=module, raw=raw)])[0]
    c.errors[0].goal = goal
    return c


def test_resources_binary_filtering_extends_existing_configuration(fake_kit, cfg):
    (fake_kit.root / "pom.xml").write_text(_filtering_pom("""
      <plugin>
        <artifactId>maven-resources-plugin</artifactId>
        <configuration>
          <encoding>UTF-8</encoding>
          <nonFilteredFileExtensions>
            <nonFilteredFileExtension>pdf</nonFilteredFileExtension>
          </nonFilteredFileExtensions>
        </configuration>
        <executions>
          <execution>
            <id>copy</id>
            <configuration><outputDirectory>x</outputDirectory></configuration>
          </execution>
        </executions>
      </plugin>"""), encoding="utf-8")
    h = fake_kit.handler("resources-binary-filtering")
    c = _font_cluster()
    assert h.matches(c, make_profile(), cfg)
    assert [x.path for x in h.apply(fake_kit.ws, c, make_profile(), cfg)] == ["pom.xml"]
    text = fake_kit.text()
    exts = _nfe(text)
    assert exts.count("pdf") == 1 and {"eot", "ttf", "woff", "woff2", "jks"} <= set(exts)
    assert text.count("<nonFilteredFileExtensions>") == 1
    assert "<encoding>UTF-8</encoding>" in text and "<filtering>true</filtering>" in text   # filtering stays on
    assert "<outputDirectory>x</outputDirectory>" in text                                    # execution config kept
    assert h.apply(fake_kit.ws, c, make_profile(), cfg) == []                                 # idempotent


def test_resources_binary_filtering_creates_the_plugin_and_learns_the_extension(fake_kit, cfg):
    (fake_kit.root / "pom.xml").write_text(
        _filtering_pom("\n      <!-- <plugin><artifactId>maven-resources-plugin</artifactId></plugin> -->"),
        encoding="utf-8")
    h = fake_kit.handler("resources-binary-filtering")
    msg = _FONT_MSG.replace("fontawesome-webfont.eot", "glyphs.abc")
    assert [x.path for x in h.apply(fake_kit.ws, _font_cluster(msg=msg), make_profile(), cfg)] == ["pom.xml"]
    text = fake_kit.text()
    exts = _nfe(text)
    assert "abc" in exts and "woff2" in exts and "png" not in exts         # the plugin's own image list is not repeated
    assert "<!-- <plugin><artifactId>maven-resources-plugin</artifactId></plugin> -->" in text   # comment untouched
    assert text.index("<artifactId>maven-compiler-plugin</artifactId>") < text.index("<artifactId>maven-resources-plugin")
    assert "<groupId>org.apache.maven.plugins</groupId>" in text


def test_resources_binary_filtering_one_line_configuration_and_no_build(fake_kit, cfg):
    fake_kit.pom(plugins=[PluginInfo(artifact_id="maven-resources-plugin", version="3.3.1",
                                     configuration={"encoding": "UTF-8"})])   # the helper writes a one-line <configuration>
    h = fake_kit.handler("resources-binary-filtering")
    assert h.apply(fake_kit.ws, _font_cluster(), make_profile(), cfg)
    text = fake_kit.text()
    assert "<encoding>UTF-8</encoding>" in text and "eot" in _nfe(text)
    fake_kit.pom("lib/pom.xml", artifact="lib")                              # no <build> at all
    assert [x.path for x in h.apply(fake_kit.ws, _font_cluster(module="lib"), None, cfg)] == ["lib/pom.xml"]
    assert "ttf" in _nfe(fake_kit.text("lib/pom.xml"))


def test_resources_binary_filtering_root_declares_filtering_and_war_plugin(fake_kit, cfg):
    (fake_kit.root / "pom.xml").write_text(_filtering_pom(artifact="parent"), encoding="utf-8")
    for name, block in (("svc", ""), ("web", _WAR_WEBRESOURCES)):
        (fake_kit.root / name).mkdir()
        (fake_kit.root / name / "pom.xml").write_text(_filtering_pom(block, artifact=name, filtering=False),
                                                      encoding="utf-8")
    h = fake_kit.handler("resources-binary-filtering")
    # resources plugin failing in svc, filtering inherited from the root → the root gets the list, svc untouched
    assert [x.path for x in h.apply(fake_kit.ws, _font_cluster(module="svc"), None, cfg)] == ["pom.xml"]
    assert "eot" in _nfe(fake_kit.text()) and _nfe(fake_kit.text("svc/pom.xml")) == []
    # the war plugin filtering <webResources> of `web` → its own list on the war plugin in the module pom
    war = _font_cluster(module="web", goal="war:war",
                        msg=_FONT_MSG.replace("src/main/resources/static", "src/main/webapp"),
                        raw="Failed to execute goal org.apache.maven.plugins:maven-war-plugin:3.4.0:war (default-war) "
                            "on project web: " + _FONT_MSG)
    assert [x.path for x in h.apply(fake_kit.ws, war, None, cfg)] == ["web/pom.xml"]
    assert "woff" in _nfe(fake_kit.text("web/pom.xml"), "maven-war-plugin")
    assert _nfe(fake_kit.text("web/pom.xml")) == []
    assert "<filtering>true</filtering>" in fake_kit.text("web/pom.xml")


def test_resources_binary_filtering_preemptive_and_non_matches(fake_kit, cfg):
    (fake_kit.root / "pom.xml").write_text(_filtering_pom(filtering=False), encoding="utf-8")
    h = fake_kit.handler("resources-binary-filtering")
    assert h.apply(fake_kit.ws, None, make_profile(), cfg) == []            # nothing filtered → nothing to do
    (fake_kit.root / "pom.xml").write_text(_filtering_pom(_WAR_WEBRESOURCES), encoding="utf-8")
    assert [x.path for x in h.apply(fake_kit.ws, None, make_profile(), cfg)] == ["pom.xml"]
    text = fake_kit.text()
    assert "eot" in _nfe(text) and "eot" in _nfe(text, "maven-war-plugin")
    assert h.apply(fake_kit.ws, None, make_profile(), cfg) == []
    assert not h.matches(_font_cluster(msg="Cannot find 'loaderImplementation' in class"), make_profile(), cfg)
    assert not h.matches(cluster_errors([err(ErrorKind.COMPILE, _FONT_MSG)])[0], make_profile(), cfg)
    assert h.matches(_font_cluster(msg="Input length = 1"), make_profile(), cfg)


# ---------------------------------------------------------------------------
# strip-pom-repositories (opt-in, build.pom_repositories=remove)
# ---------------------------------------------------------------------------
_REPO_POM = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <artifactId>demo</artifactId>
  <!-- repositories: <repositories> kept in this comment -->
  <repositories>
    <repository>
      <id>old-nexus</id>
      <url>http://old-nexus.corp/repo</url>
    </repository>
  </repositories>
  <dependencies>
  </dependencies>
  <profiles>
    <profile>
      <id>ci</id>
      <pluginRepositories>
        <pluginRepository><id>p</id><url>http://old-nexus.corp/plugins</url></pluginRepository>
      </pluginRepositories>
      <properties><a>b</a></properties>
    </profile>
  </profiles>
</project>
"""


def test_strip_pom_repositories_is_opt_in(fake_kit, cfg):
    (fake_kit.root / "pom.xml").write_text(_REPO_POM, encoding="utf-8")
    h = fake_kit.handler("strip-pom-repositories")
    c = cluster_errors([err(ErrorKind.DEPENDENCY_RESOLUTION,
                            "Could not transfer artifact com.acme:x:pom:1.0 from/to old-nexus (http://old-nexus.corp/repo): "
                            "Connect timed out", artifact="com.acme:x:1.0")])[0]
    assert cfg.build.pom_repositories == "keep"
    assert not h.matches(c, make_profile(), cfg) and h.apply(fake_kit.ws, c, make_profile(), cfg) == []
    assert "<repositories>" in fake_kit.text()
    on = cfg.model_copy(deep=True)
    on.build.pom_repositories = "remove"
    assert h.matches(c, make_profile(), on)
    assert [x.path for x in h.apply(fake_kit.ws, None, make_profile(), on)] == ["pom.xml"]     # pre-emptive form
    text = fake_kit.text()
    import xml.etree.ElementTree as ET
    ET.fromstring(text)
    assert "<repositories>\n    <repository>" not in text and "<pluginRepositories>" not in text
    assert "<!-- repositories: <repositories> kept in this comment -->" in text
    assert "<properties><a>b</a></properties>" in text and "<dependencies>" in text
    assert h.apply(fake_kit.ws, c, make_profile(), on) == []


def test_add_known_dependency_junit4_leftovers_and_platform_rows(kit):
    """2026-10-22 company run: `package org.junit.experimental.categories does not exist` (JUnit4to5Migration dropped
    junit:junit, a @Category test survived) → junit:junit test-scope, version omitted under a Boot parent; JUnit 5
    packages keep their own rows (longest prefix wins)."""
    kit.pom(deps=[G("org.springframework.boot:spring-boot-starter-test")])
    txt = kit.text().replace("<modelVersion>4.0.0</modelVersion>",
                             "<modelVersion>4.0.0</modelVersion><parent><groupId>org.springframework.boot</groupId>"
                             "<artifactId>spring-boot-starter-parent</artifactId><version>4.1.0</version></parent>")
    (kit.root / "pom.xml").write_text(txt, encoding="utf-8")
    h = kit.handler("add-known-dependency")
    c = cluster_errors([err(kind=ErrorKind.TEST_COMPILE, file="src/test/java/com/acme/SlowTests.java",
                            message="package org.junit.experimental.categories does not exist",
                            raw="[ERROR] /x/src/test/java/com/acme/SlowTests.java:[3,41] package "
                                "org.junit.experimental.categories does not exist")])[0]
    assert h.matches(c, make_profile(), None)
    assert [ch.path for ch in h.apply(kit.ws, c, make_profile(), None)] == ["pom.xml"]
    d = kit.dep("junit:junit")
    assert d is not None and d.scope == "test" and d.version is None
    assert kit.dep("org.junit.jupiter:junit-jupiter") is None
    c2 = cluster_errors([err(kind=ErrorKind.TEST_COMPILE, file="src/test/java/com/acme/AllTests.java",
                             message="package org.junit.platform.suite.api does not exist")])[0]
    assert h.apply(kit.ws, c2, make_profile(), None)
    assert kit.dep("org.junit.platform:junit-platform-suite").scope == "test"
    c3 = cluster_errors([err(kind=ErrorKind.TEST_COMPILE, file="src/test/java/com/acme/T.java",
                             message="package org.junit.jupiter.params does not exist")])[0]
    assert h.apply(kit.ws, c3, make_profile(), None)
    assert kit.dep("org.junit.jupiter:junit-jupiter") is not None          # not junit:junit again
