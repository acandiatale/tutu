from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.core.config import Config
from sbmig.core.interfaces import RestructurerLike
from sbmig.core.models import CaseType, PomInfo, ServiceInfo
from sbmig.restructure import PomEditor, Restructurer, sanitize_identifier, sanitize_package

from .helpers import HEAD, all_, dep, dep_keys, java, make_ws, profile, t, xml

MANDATORY = ["org.springframework.boot:spring-boot-starter-validation",
             "org.springframework.boot:spring-boot-starter-actuator"]


# ---------------------------------------------------------------------------------------------- fixtures
def boot2_files() -> dict[str, str]:
    pom = (HEAD +
           "    <!-- as-is parent -->\n"
           "    <parent>\n        <groupId>org.springframework.boot</groupId>\n"
           "        <artifactId>spring-boot-starter-parent</artifactId>\n        <version>2.7.18</version>\n"
           "        <relativePath/>\n    </parent>\n\n"
           "    <groupId>com.acme</groupId>\n    <artifactId>shop</artifactId>\n"
           "    <version>1.0.0-SNAPSHOT</version>\n    <name>Shop</name>\n\n"
           "    <properties>\n        <java.version>1.8</java.version>\n"
           "        <guava.version>30.0-jre</guava.version>\n    </properties>\n\n"
           "    <dependencies>\n"
           + dep("org.springframework.boot", "spring-boot-starter-web")
           + dep("com.google.guava", "guava", "${guava.version}")
           + dep("org.apache.commons", "commons-lang3", "3.12.0")
           + dep("org.projectlombok", "lombok", extra="            <optional>true</optional>\n")
           + dep("org.springframework.boot", "spring-boot-starter-actuator", "2.7.18")
           + dep("com.legacy", "legacy-bom-thing", "1.0")
           + dep("org.springframework.boot", "spring-boot-starter-test", scope="test")
           + "    </dependencies>\n\n"
           "    <repositories>\n        <repository>\n            <id>corp</id>\n"
           "            <url>https://repo.example/maven</url>\n        </repository>\n    </repositories>\n\n"
           "    <build>\n        <finalName>shop-app</finalName>\n        <plugins>\n"
           "            <plugin>\n                <groupId>org.springframework.boot</groupId>\n"
           "                <artifactId>spring-boot-maven-plugin</artifactId>\n            </plugin>\n"
           "            <plugin>\n                <groupId>org.apache.maven.plugins</groupId>\n"
           "                <artifactId>maven-compiler-plugin</artifactId>\n"
           "                <version>3.8.1</version>\n                <configuration>\n"
           "                    <source>${java.version}</source>\n                    <target>${java.version}</target>\n"
           "                    <annotationProcessorPaths>\n                        <path>\n"
           "                            <groupId>org.projectlombok</groupId>\n"
           "                            <artifactId>lombok</artifactId>\n"
           "                            <version>1.18.20</version>\n                        </path>\n"
           "                    </annotationProcessorPaths>\n"
           "                </configuration>\n            </plugin>\n"
           "        </plugins>\n    </build>\n</project>\n")
    return {
        "pom.xml": pom,
        "src/main/java/com/acme/shop/ShopApplication.java": java(
            "com.acme.shop", "ShopApplication", "@SpringBootApplication",
            imports="import org.springframework.boot.autoconfigure.SpringBootApplication;"),
        "src/main/java/com/acme/shop/web/HelloController.java": java("com.acme.shop.web", "HelloController",
                                                                     "@RestController"),
        "src/main/java/com/acme/shop/config/WebConfig.java": java("com.acme.shop.config", "WebConfig",
                                                                  "@Configuration"),
        "src/main/java/com/acme/shop/service/Greeter.java": java("com.acme.shop.service", "Greeter", "@Service"),
        "src/main/resources/application.yml": "server:\n  port: 8080\n",
        "src/main/resources/config/application-prod.properties": "x=1\n",
        "src/main/resources/logback-spring.xml": "<configuration/>\n",
        "src/main/resources/static/app.css": "body{}\n",
        "src/test/java/com/acme/shop/ShopApplicationTests.java": java("com.acme.shop", "ShopApplicationTests",
                                                                      "@SpringBootTest"),
        "src/test/java/com/acme/shop/service/GreeterTest.java": java("com.acme.shop.service", "GreeterTest"),
        "src/test/resources/application-test.yml": "a: b\n",
        "lombok.config": "lombok.addLombokGeneratedAnnotation = true\n",
        "README.md": "# shop\n",
    }


def boot2_profile(ws):
    return profile(ws, CaseType.SPRING_BOOT, base_package="com.acme.shop",
                   main_classes=["com.acme.shop.ShopApplication"],
                   spring_boot_test_classes=["com.acme.shop.ShopApplicationTests"],
                   controllers=["com.acme.shop.web.HelloController"])


@pytest.fixture()
def cfg_rm(cfg: Config) -> Config:
    cfg.artifacts.remove_when_present = ["com.legacy:legacy-bom-thing"]
    return cfg


# ---------------------------------------------------------------------------------------------- unit helpers
def test_sanitizers() -> None:
    assert sanitize_package("com.my-company.1app.class") == "com.my_company._1app.class_"
    assert sanitize_package("") is None and sanitize_package(None) is None
    assert sanitize_identifier("Application") == "Application"
    assert sanitize_identifier("") == "x" and sanitize_identifier("_") == "__"
    assert isinstance(Restructurer(), RestructurerLike)


# ---------------------------------------------------------------------------------------------- boot2 single
def test_layout_boot2_single_module(tmp_path: Path, cfg_rm: Config) -> None:
    ws = make_ws(tmp_path, boot2_files(), "boot2")
    head0 = ws.head()
    res = Restructurer().apply(ws, boot2_profile(ws), cfg_rm, mode="layout")
    assert res.ok, res.error
    root = ws.root
    comp, boot = root / "module/comp-jar", root / "module/boot-jar"

    # --- moves
    assert not (root / "src").exists()
    assert (comp / "src/main/java/com/acme/shop/service/Greeter.java").exists()
    assert (comp / "src/main/java/com/acme/shop/web/HelloController.java").exists()   # minimal split policy
    assert (comp / "src/main/resources/static/app.css").exists()
    assert (comp / "src/test/java/com/acme/shop/service/GreeterTest.java").exists()
    assert (boot / "src/main/java/com/acme/shop/ShopApplication.java").exists()
    assert not (comp / "src/main/java/com/acme/shop/ShopApplication.java").exists()
    assert (boot / "src/test/java/com/acme/shop/ShopApplicationTests.java").exists()
    for r in ("application.yml", "logback-spring.xml", "config/application-prod.properties"):
        assert (boot / "src/main/resources" / r).exists(), r
        assert not (comp / "src/main/resources" / r).exists()
    assert (boot / "src/test/resources/application-test.yml").exists()
    assert (root / "lombok.config").exists() and (root / "README.md").exists()
    assert not list(boot.rglob("Application.java"))           # existing @SpringBootApplication reused
    assert res.moved_files >= 13

    # --- root pom exact structure
    r = xml(root / "pom.xml")
    assert t(r, "parent/groupId") == "org.springframework.boot"
    assert t(r, "parent/artifactId") == "spring-boot-starter-parent"
    assert t(r, "parent/version") == "4.1.0"
    assert t(r, "parent/relativePath") == ""
    assert t(r, "packaging") == "pom"
    assert [m.text for m in all_(r, "modules/module")] == ["module/comp-jar", "module/boot-jar"]
    assert dep_keys(r) == MANDATORY
    assert all(t(d, "version") is None for d in all_(r, "dependencies/dependency"))
    assert t(r, "properties/maven.compiler.release") == "21"          # parent_manages_java_version: false
    assert t(r, "properties/java.version") == "21"
    assert t(r, "properties/guava.version") == "33.4.8-jre"           # optional bump through the property
    assert r.find("{http://maven.apache.org/POM/4.0.0}build") is None
    assert t(r, "repositories/repository/id") == "corp"
    text = (root / "pom.xml").read_text()
    assert "<!-- as-is parent -->" in text and "spring-boot-maven-plugin" not in text
    assert res.mandatory_added == MANDATORY

    # --- comp pom
    c = xml(comp / "pom.xml")
    assert t(c, "parent/artifactId") == "shop" and t(c, "parent/relativePath") == "../../pom.xml"
    assert t(c, "parent/groupId") == "com.acme" and t(c, "parent/version") == "1.0.0-SNAPSHOT"
    assert t(c, "artifactId") == "shop-comp" and t(c, "packaging") == "jar"
    keys = dep_keys(c)
    assert keys == ["org.springframework.boot:spring-boot-starter-web", "com.google.guava:guava",
                    "org.apache.commons:commons-lang3", "org.projectlombok:lombok",
                    "org.springframework.boot:spring-boot-starter-test"]   # mandatory + remove_when_present dropped
    cdeps = {f"{t(d, 'groupId')}:{t(d, 'artifactId')}": d for d in all_(c, "dependencies/dependency")}
    assert t(cdeps["com.google.guava:guava"], "version") == "${guava.version}"
    assert t(cdeps["org.apache.commons:commons-lang3"], "version") == "3.17.0"
    assert t(cdeps["org.projectlombok:lombok"], "version") == "1.18.38"     # not managed in-project → pinned
    assert t(cdeps["org.projectlombok:lombok"], "optional") == "true"
    plugins = all_(c, "build/plugins/plugin")
    assert [t(p, "artifactId") for p in plugins] == ["maven-compiler-plugin"]
    assert t(plugins[0], "configuration/release") == "21"
    assert t(plugins[0], "configuration/source") is None
    assert t(plugins[0], "configuration/annotationProcessorPaths/path/version") == "1.18.38"

    # --- boot pom
    b = xml(boot / "pom.xml")
    assert t(b, "artifactId") == "shop-boot" and t(b, "parent/relativePath") == "../../pom.xml"
    assert dep_keys(b) == ["${project.groupId}:shop-comp", "org.springframework.boot:spring-boot-starter-test"]
    assert t(all_(b, "dependencies/dependency")[0], "version") == "${project.version}"
    bp = all_(b, "build/plugins/plugin")
    assert [t(p, "artifactId") for p in bp] == ["spring-boot-maven-plugin"]
    assert t(bp[0], "version") is None                              # parent manages boot
    assert t(bp[0], "executions/execution/goals/goal") == "repackage"
    assert t(b, "build/finalName") == "shop-app"

    # --- result bookkeeping
    assert "com.google.guava:guava 30.0-jre→33.4.8-jre" in res.optional_bumped
    assert "org.apache.commons:commons-lang3 3.12.0→3.17.0" in res.optional_bumped
    assert "org.projectlombok:lombok managed→1.18.38" in res.optional_bumped
    assert "org.projectlombok:lombok 1.18.20→1.18.38" in res.optional_bumped
    assert "module/comp-jar/pom.xml" in res.generated_files and "module/boot-jar/pom.xml" in res.generated_files
    assert "pom.xml" in res.changed_files
    assert any("@SpringBootTest" in f for f in res.manual_followups)
    assert ws.head() != head0 and not ws.is_dirty()

    # --- second run is a no-op
    res2 = Restructurer().apply(ws, boot2_profile(ws), cfg_rm, mode="layout")
    assert res2.ok and res2.changed_files == []
    assert res2.notes == ["already restructured", "as-is version carry-over skipped: no as-is effective pom",
                          "managed-version cleanup skipped: no effective pom"]


def test_layout_web_layer_split(tmp_path: Path, cfg: Config) -> None:
    ws = make_ws(tmp_path, boot2_files(), "weblayer")
    res = Restructurer(commit=False).apply(ws, boot2_profile(ws), cfg, mode="layout",
                                          params={"split_policy": "web-layer"})
    assert res.ok, res.error
    boot = ws.root / "module/boot-jar/src/main/java/com/acme/shop"
    assert (boot / "web/HelloController.java").exists()
    assert (boot / "config/WebConfig.java").exists()
    assert (ws.root / "module/comp-jar/src/main/java/com/acme/shop/service/Greeter.java").exists()


def test_layout_parent_not_managing_boot_adds_bom(tmp_path: Path, cfg: Config) -> None:
    cfg.artifacts.parent_manages_spring_boot = False
    cfg.artifacts.parent_manages_java_version = True
    files = boot2_files()
    files["pom.xml"] = files["pom.xml"].replace(
        "    <dependencies>\n",
        "    <dependencyManagement>\n        <dependencies>\n"
        + dep("org.example", "some-bom", "1.0").replace("        <", "            <").replace(
            "            </dependency>", "            </dependency>")
        + "        </dependencies>\n    </dependencyManagement>\n\n    <dependencies>\n", 1)
    ws = make_ws(tmp_path, files, "bom")
    res = Restructurer().apply(ws, boot2_profile(ws), cfg, mode="layout")
    assert res.ok, res.error
    r = xml(ws.root / "pom.xml")
    md = all_(r, "dependencyManagement/dependencies/dependency")
    assert [t(d, "artifactId") for d in md] == ["spring-boot-dependencies", "some-bom"]
    assert t(md[0], "version") == "4.1.0" and t(md[0], "type") == "pom" and t(md[0], "scope") == "import"
    for k in ("java.version", "maven.compiler.release", "maven.compiler.source"):
        assert t(r, f"properties/{k}") is None
    b = xml(ws.root / "module/boot-jar/pom.xml")
    assert t(all_(b, "build/plugins/plugin")[0], "version") == "4.1.0"
    c = xml(ws.root / "module/comp-jar/pom.xml")
    conf = all_(c, "build/plugins/plugin")[0]
    assert t(conf, "configuration/release") is None and t(conf, "configuration/source") is None


def test_parent_manages_boot_removes_asis_boot_bom(tmp_path: Path, cfg: Config) -> None:
    files = boot2_files()
    files["pom.xml"] = files["pom.xml"].replace(
        "    <dependencies>\n",
        "    <dependencyManagement>\n        <dependencies>\n"
        + dep("org.springframework.boot", "spring-boot-dependencies", "2.7.18",
              extra="            <type>pom</type>\n")
        + "        </dependencies>\n    </dependencyManagement>\n\n    <dependencies>\n", 1)
    ws = make_ws(tmp_path, files, "oldbom")
    res = Restructurer().apply(ws, boot2_profile(ws), cfg, mode="layout")
    assert res.ok
    r = xml(ws.root / "pom.xml")
    assert r.find("{http://maven.apache.org/POM/4.0.0}dependencyManagement") is None
    assert any("spring-boot-dependencies import removed" in n for n in res.notes)


# ---------------------------------------------------------------------------------------------- servlet war
def servlet_files() -> dict[str, str]:
    pom = (HEAD + "    <groupId>com.acme</groupId>\n    <artifactId>legacyweb</artifactId>\n"
           "    <version>2.0</version>\n    <packaging>war</packaging>\n\n"
           "    <properties>\n        <maven.compiler.source>1.8</maven.compiler.source>\n"
           "        <maven.compiler.target>1.8</maven.compiler.target>\n    </properties>\n\n"
           "    <dependencies>\n" + dep("javax.servlet", "javax.servlet-api", "4.0.1", "provided")
           + dep("org.springframework.boot", "spring-boot-starter-tomcat", "2.7.0", "provided")
           + dep("commons-io", "commons-io", "2.6") + "    </dependencies>\n\n"
           "    <build>\n        <sourceDirectory>java</sourceDirectory>\n        <plugins>\n"
           "            <plugin>\n                <artifactId>maven-war-plugin</artifactId>\n"
           "                <version>3.3.2</version>\n            </plugin>\n        </plugins>\n    </build>\n"
           "</project>\n")
    return {
        "pom.xml": pom,
        "java/com/acme/web/HelloServlet.java": java("com.acme.web", "HelloServlet", '@WebServlet("/hello")'),
        "src/main/webapp/WEB-INF/web.xml": "<web-app/>\n",
        "src/main/webapp/index.jsp": "<html/>\n",
        "src/main/resources/log4j2.xml": "<Configuration/>\n",
    }


def test_layout_servlet_war(tmp_path: Path, cfg: Config) -> None:
    ws = make_ws(tmp_path, servlet_files(), "servlet")
    prof = profile(ws, CaseType.SERVLET_TOMCAT, artifact_id="legacyweb", version="2.0", packaging="war",
                   base_package="com.acme.web", servlet_classes=["com.acme.web.HelloServlet"], has_web_xml=True,
                   web_xml_paths=["src/main/webapp/WEB-INF/web.xml"], jsp_count=1)
    res = Restructurer().apply(ws, prof, cfg, mode="layout")
    assert res.ok, res.error
    root = ws.root
    assert (root / "module/comp-jar/java/com/acme/web/HelloServlet.java").exists()     # custom source root moved
    assert (root / "module/comp-jar/src/main/webapp/WEB-INF/web.xml").exists()
    assert (root / "module/boot-jar/src/main/resources/log4j2.xml").exists()
    app = (root / "module/boot-jar/src/main/java/com/acme/web/Application.java").read_text()
    assert "package com.acme.web;" in app
    assert "@ServletComponentScan" in app and "import org.springframework.boot.web.server.servlet.context.ServletComponentScan;" in app
    assert "@ImportResource" not in app and "SpringApplication.run(Application.class, args);" in app
    assert "module/boot-jar/src/main/java/com/acme/web/Application.java" in res.generated_files

    c = xml(root / "module/comp-jar/pom.xml")
    assert t(c, "build/sourceDirectory") == "java"
    assert dep_keys(c) == ["javax.servlet:javax.servlet-api", "commons-io:commons-io"]
    assert c.find("{http://maven.apache.org/POM/4.0.0}build/{http://maven.apache.org/POM/4.0.0}plugins") is None
    cio = all_(c, "dependencies/dependency")[1]
    assert t(cio, "version") == "2.19.0"
    b = xml(root / "module/boot-jar/pom.xml")
    assert dep_keys(b) == ["${project.groupId}:legacyweb-comp", "org.springframework.boot:spring-boot-starter-webmvc",
                           "org.apache.tomcat.embed:tomcat-embed-jasper"]
    r = xml(root / "pom.xml")
    assert t(r, "packaging") == "pom" and t(r, "build/sourceDirectory") is None
    assert t(r, "properties/maven.compiler.release") == "21" and t(r, "properties/maven.compiler.source") is None
    fu = "\n".join(res.manual_followups)
    assert "web.xml at module/comp-jar/src/main/webapp/WEB-INF/web.xml is not read by embedded Tomcat" in fu
    assert "WAR packaging converted to executable jar" in fu
    assert "tomcat-embed-jasper" in fu


# ---------------------------------------------------------------------------------------------- spring legacy
def test_layout_spring_legacy_xml_configs(tmp_path: Path, cfg: Config) -> None:
    pom = (HEAD + "    <groupId>com.acme</groupId>\n    <artifactId>erp</artifactId>\n    <version>3.1</version>\n"
           "    <dependencies>\n" + dep("org.springframework", "spring-webmvc", "5.3.30") + "    </dependencies>\n"
           "</project>\n")
    files = {
        "pom.xml": pom,
        "src/main/java/com/acme/erp/Svc.java": java("com.acme.erp", "Svc"),
        "src/main/resources/spring/app-context.xml": "<beans/>\n",
        "src/main/webapp/WEB-INF/dispatcher-servlet.xml": "<beans/>\n",
        "src/main/webapp/WEB-INF/spring/app-context.xml": "<beans/>\n",
    }
    ws = make_ws(tmp_path, files, "legacy")
    prof = profile(ws, CaseType.SPRING_LEGACY, artifact_id="erp", version="3.1", base_package="com.acme.erp",
                   xml_bean_configs=["src/main/resources/spring/app-context.xml",
                                     "src/main/webapp/WEB-INF/dispatcher-servlet.xml",
                                     "src/main/webapp/WEB-INF/spring/app-context.xml", "src/missing.xml"])
    res = Restructurer().apply(ws, prof, cfg, mode="layout")
    assert res.ok, res.error
    boot = ws.root / "module/boot-jar"
    app = (boot / "src/main/java/com/acme/erp/Application.java").read_text()
    assert '"classpath:legacy-config/app-context.xml",' in app
    assert '"classpath:legacy-config/dispatcher-servlet.xml",' in app
    assert '"classpath:legacy-config/spring-app-context.xml"' in app
    assert "@ServletComponentScan" not in app
    for n in ("app-context.xml", "dispatcher-servlet.xml", "spring-app-context.xml"):
        assert (boot / "src/main/resources/legacy-config" / n).exists()
    assert (ws.root / "module/comp-jar/src/main/resources/spring/app-context.xml").exists()   # originals kept
    assert any("@ImportResource" in f for f in res.manual_followups)
    assert any("src/missing.xml" in n for n in res.notes)
    b = xml(boot / "pom.xml")
    assert "org.springframework.boot:spring-boot-starter-webmvc" in dep_keys(b)


# ---------------------------------------------------------------------------------------------- pure java
def test_layout_pure_java_generates_application(tmp_path: Path, cfg: Config) -> None:
    pom = ("<project>\n  <modelVersion>4.0.0</modelVersion>\n  <groupId>com.acme</groupId>\n"
           "  <artifactId>tool</artifactId>\n  <version>1</version>\n</project>\n")
    files = {"pom.xml": pom, "src/main/java/com/acme/tool/Main.java": java("com.acme.tool", "Main"),
             "src/main/java/com/acme/tool/Application.java": java("com.acme.tool", "Application")}
    ws = make_ws(tmp_path, files, "pure")
    prof = profile(ws, CaseType.PURE_JAVA, artifact_id="tool", version="1", base_package="com.acme.tool",
                   psvm_classes=["com.acme.tool.Main"])
    res = Restructurer().apply(ws, prof, cfg, mode="layout")
    assert res.ok, res.error
    gen = ws.root / "module/boot-jar/src/main/java/com/acme/tool/BootApplication.java"   # name clash avoided
    assert gen.exists()
    text = gen.read_text()
    assert "@SpringBootApplication" in text and "@ServletComponentScan" not in text
    b = PomEditor(ws.root / "module/boot-jar/pom.xml")
    assert [d.key for d in b.dependencies()] == ["${project.groupId}:tool-comp",
                                                 "org.springframework.boot:spring-boot-starter"]
    r = PomEditor(ws.root / "pom.xml")
    assert r.get_parent().artifact_id == "spring-boot-starter-parent" and r.packaging() == "pom"
    assert r.namespace is None
    assert any("com.acme.tool.Main" in f for f in res.manual_followups)


def test_layout_unknown_base_package_fallback(tmp_path: Path, cfg: Config) -> None:
    pom = HEAD + "    <groupId>g</groupId>\n    <artifactId>x</artifactId>\n    <version>1</version>\n</project>\n"
    ws = make_ws(tmp_path, {"pom.xml": pom}, "nopkg")
    prof = profile(ws, CaseType.SPRING_BOOT, artifact_id="x", group_id="g", version="1")
    res = Restructurer().apply(ws, prof, cfg, mode="layout", params={"starter": "auto"})
    assert res.ok, res.error
    assert (ws.root / "module/boot-jar/src/main/java/com/company/app/Application.java").exists()
    assert prof.warnings and "com.company.app" in prof.warnings[0]
    b = PomEditor(ws.root / "module/boot-jar/pom.xml")
    assert "org.springframework.boot:spring-boot-starter" in [d.key for d in b.dependencies()]


# ---------------------------------------------------------------------------------------------- multi-module
def multi_files() -> dict[str, str]:
    root = (HEAD + "    <groupId>com.acme</groupId>\n    <artifactId>suite</artifactId>\n    <version>5.0</version>\n"
            "    <packaging>pom</packaging>\n    <modules>\n        <module>core</module>\n"
            "        <module>web</module>\n    </modules>\n"
            "    <properties>\n        <lombok.version>1.18.20</lombok.version>\n    </properties>\n"
            "    <dependencies>\n" + dep("org.projectlombok", "lombok", "${lombok.version}", "provided")
            + "    </dependencies>\n"
            "    <build>\n        <plugins>\n            <plugin>\n"
            "                <artifactId>maven-surefire-plugin</artifactId>\n"
            "                <version>3.2.5</version>\n            </plugin>\n        </plugins>\n    </build>\n"
            "</project>\n")

    def child(aid: str, packaging: str, deps: str = "", plugins: str = "") -> str:
        return (HEAD + "    <parent>\n        <groupId>com.acme</groupId>\n        <artifactId>suite</artifactId>\n"
                "        <version>5.0</version>\n    </parent>\n"
                f"    <artifactId>{aid}</artifactId>\n    <packaging>{packaging}</packaging>\n"
                f"    <dependencies>\n{deps}    </dependencies>\n"
                + (f"    <build>\n        <plugins>\n{plugins}        </plugins>\n    </build>\n" if plugins else "")
                + "</project>\n")

    boot_plugin = ("            <plugin>\n                <groupId>org.springframework.boot</groupId>\n"
                   "                <artifactId>spring-boot-maven-plugin</artifactId>\n            </plugin>\n")
    return {
        "pom.xml": root,
        "core/pom.xml": child("suite-core", "jar", dep("com.google.guava", "guava", "31.0-jre")),
        "core/src/main/java/com/acme/suite/core/Repo.java": java("com.acme.suite.core", "Repo"),
        "web/pom.xml": child("suite-web", "war", dep("com.acme", "suite-core", "${project.version}"),
                             plugins=boot_plugin),
        "web/src/main/java/com/acme/suite/SuiteApplication.java": java("com.acme.suite", "SuiteApplication",
                                                                       "@SpringBootApplication"),
        "web/src/main/java/com/acme/suite/web/Ctl.java": java("com.acme.suite.web", "Ctl"),
        "web/src/main/resources/application.properties": "a=b\n",
    }


def multi_profile(ws):
    p = profile(ws, CaseType.SPRING_BOOT, artifact_id="suite", version="5.0", packaging="pom",
                base_package="com.acme.suite", main_classes=["com.acme.suite.SuiteApplication"])
    p.is_multi_module = True
    return p


def test_layout_multi_module_wrap(tmp_path: Path, cfg: Config) -> None:
    ws = make_ws(tmp_path, multi_files(), "multi")
    res = Restructurer().apply(ws, multi_profile(ws), cfg, mode="layout", params={"multi_module_policy": "wrap"})
    assert res.ok, res.error
    root = ws.root
    assert not (root / "core").exists() and (root / "module/core/pom.xml").exists()
    r = xml(root / "pom.xml")
    assert [m.text for m in all_(r, "modules/module")] == ["module/core", "module/web", "module/boot-jar"]
    assert dep_keys(r) == ["org.projectlombok:lombok"] + MANDATORY            # as-is root deps kept in wrap
    assert t(r, "properties/lombok.version") == "1.18.38"
    assert [t(p, "artifactId") for p in all_(r, "build/plugins/plugin")] == ["maven-surefire-plugin"]
    for m in ("core", "web"):
        c = xml(root / f"module/{m}/pom.xml")
        assert t(c, "parent/relativePath") == "../../pom.xml"
    # S7: the WAR module is wrapped unchanged (still a WAR, keeps its boot plugin and its classes)
    web = PomEditor(root / "module/web/pom.xml")
    assert web.packaging() == "war" and web.has_plugin("org.springframework.boot", "spring-boot-maven-plugin")
    core = PomEditor(root / "module/core/pom.xml")
    assert core.dependencies()[0].version == "33.4.8-jre"
    boot = root / "module/boot-jar"
    assert (root / "module/web/src/main/java/com/acme/suite/SuiteApplication.java").exists()
    assert (root / "module/web/src/main/resources/application.properties").exists()
    assert (root / "module/web/src/main/java/com/acme/suite/web/Ctl.java").exists()
    assert not (boot / "src/main/java/com/acme/suite/SuiteApplication.java").exists()
    assert (boot / "src/main/java/com/acme/suite/Application.java").exists()       # generated: no jar main class
    b = PomEditor(boot / "pom.xml")
    assert [d.key for d in b.dependencies()] == ["${project.groupId}:suite-core",          # jar modules only
                                                 "org.springframework.boot:spring-boot-starter"]   # auto: none as-is
    assert b.get_parent().artifact_id == "suite"
    assert any("WAR module module/web kept as-is" in f and "embedded deployment needs conversion" in f
               for f in res.manual_followups)
    assert any("SuiteApplication stays in WAR module module/web" in n for n in res.notes)
    assert "com.google.guava:guava 31.0-jre→33.4.8-jre" in res.optional_bumped


def test_layout_multi_module_merge(tmp_path: Path, cfg: Config) -> None:
    ws = make_ws(tmp_path, multi_files(), "merge")
    res = Restructurer().apply(ws, multi_profile(ws), cfg, mode="layout",
                               params={"multi_module_policy": "merge"})
    assert res.ok, res.error
    root = ws.root
    assert not (root / "core").exists() and not (root / "web").exists()
    comp = root / "module/comp-jar"
    assert (comp / "src/main/java/com/acme/suite/core/Repo.java").exists()
    assert (comp / "src/main/java/com/acme/suite/web/Ctl.java").exists()
    assert (root / "module/boot-jar/src/main/java/com/acme/suite/SuiteApplication.java").exists()
    c = PomEditor(comp / "pom.xml")
    assert [d.key for d in c.dependencies()] == ["org.projectlombok:lombok", "com.google.guava:guava"]
    r = PomEditor(root / "pom.xml")
    assert r.modules() == ["module/comp-jar", "module/boot-jar"]
    assert [d.key for d in r.dependencies()] == MANDATORY
    assert any("WAR packaging converted" in f for f in res.manual_followups)


def test_layout_multi_module_merge_collision(tmp_path: Path, cfg: Config) -> None:
    files = multi_files()
    files["core/src/main/java/com/acme/suite/web/Ctl.java"] = java("com.acme.suite.web", "Ctl")
    ws = make_ws(tmp_path, files, "collide")
    res = Restructurer().apply(ws, multi_profile(ws), cfg, mode="layout", params={"merge_collision": "fail"})
    assert not res.ok and "collision" in (res.error or "")
    assert "src/main/java/com/acme/suite/web/Ctl.java exists in both core and web" in res.error
    assert (ws.root / "core/src/main/java/com/acme/suite/web/Ctl.java").exists()      # nothing moved
    assert (ws.root / "web/src/main/java/com/acme/suite/web/Ctl.java").exists()
    assert not (ws.root / "module").exists() and res.moved_files == 0 and res.merge_conflicts == []


def kept_boot_files(boot_dir: str = "boot-jar") -> dict[str, str]:
    """core + web jar modules and an as-is boot-jar module (own Application, application.yml, test)."""
    files = multi_files()
    files["pom.xml"] = files["pom.xml"].replace(
        "        <module>web</module>\n", f"        <module>web</module>\n        <module>{boot_dir}</module>\n")
    files["web/pom.xml"] = files["web/pom.xml"].replace("<packaging>war</packaging>", "<packaging>jar</packaging>")
    del files["web/src/main/java/com/acme/suite/SuiteApplication.java"]
    del files["web/src/main/resources/application.properties"]
    rel = "../" * (boot_dir.count("/") + 1) + "pom.xml"
    files[f"{boot_dir}/pom.xml"] = (
        HEAD + "    <parent>\n        <groupId>com.acme</groupId>\n        <artifactId>suite</artifactId>\n"
        f"        <version>5.0</version>\n        <relativePath>{rel}</relativePath>\n    </parent>\n"
        "    <artifactId>suite-app</artifactId>\n    <packaging>jar</packaging>\n"
        "    <properties>\n        <maven.compiler.source>11</maven.compiler.source>\n"
        "        <maven.compiler.target>11</maven.compiler.target>\n    </properties>\n"
        "    <dependencies>\n" + dep("com.acme", "suite-core", "${project.version}")
        + dep("${project.groupId}", "suite-web", "${project.version}")
        + dep("org.springframework.boot", "spring-boot-starter-web")
        + "    </dependencies>\n"
        "    <build>\n        <plugins>\n            <plugin>\n"
        "                <groupId>org.springframework.boot</groupId>\n"
        "                <artifactId>spring-boot-maven-plugin</artifactId>\n"
        "                <configuration>\n                    <layers><enabled>true</enabled></layers>\n"
        "                </configuration>\n"
        "            </plugin>\n        </plugins>\n    </build>\n</project>\n")
    files[f"{boot_dir}/src/main/java/com/acme/suite/SuiteApplication.java"] = java(
        "com.acme.suite", "SuiteApplication", "@SpringBootApplication")
    files[f"{boot_dir}/src/main/resources/application.yml"] = "server:\n  port: 8081\n"
    files[f"{boot_dir}/src/test/java/com/acme/suite/SuiteApplicationTests.java"] = java(
        "com.acme.suite", "SuiteApplicationTests", "@SpringBootTest")
    return files


@pytest.mark.parametrize("boot_dir", ["boot-jar", "apps/boot-jar"])
def test_layout_merge_keeps_asis_boot_module(tmp_path: Path, cfg: Config, boot_dir: str) -> None:
    ws = make_ws(tmp_path, kept_boot_files(boot_dir), "kept-" + boot_dir.replace("/", "-"))
    prof = multi_profile(ws)
    prof.source.spring_boot_test_classes = ["com.acme.suite.SuiteApplicationTests"]
    res = Restructurer().apply(ws, prof, cfg, mode="layout")                    # merge is the default policy
    assert res.ok, res.error
    root = ws.root
    for gone in ("core", "web", boot_dir):
        assert not (root / gone).exists()
    boot = root / "module/boot-jar"
    comp = root / "module/comp-jar"
    assert res.kept_boot_module == "module/boot-jar" and res.merged_modules == ["core", "web"]
    assert res.merge_conflicts == []
    # boot-jar: as-is sources untouched, nothing generated, nothing moved in from comp-jar
    assert (boot / "src/main/java/com/acme/suite/SuiteApplication.java").read_text() == java(
        "com.acme.suite", "SuiteApplication", "@SpringBootApplication")
    assert (boot / "src/main/resources/application.yml").read_text() == "server:\n  port: 8081\n"
    assert (boot / "src/test/java/com/acme/suite/SuiteApplicationTests.java").exists()
    assert not (boot / "src/main/java/com/acme/suite/Application.java").exists()
    assert not any(f.endswith("Application.java") for f in res.generated_files)
    b = xml(boot / "pom.xml")
    assert t(b, "artifactId") == "suite-app" and t(b, "parent/relativePath") == "../../pom.xml"
    assert dep_keys(b) == ["${project.groupId}:suite-comp", "org.springframework.boot:spring-boot-starter-web"]
    assert t(b, "dependencies/dependency/version") == "${project.version}"
    plugins = all_(b, "build/plugins/plugin")
    assert [t(p, "artifactId") for p in plugins] == ["spring-boot-maven-plugin"]
    assert t(plugins[0], "configuration/layers/enabled") == "true"                 # as-is plugin kept
    assert t(b, "properties/maven.compiler.source") is None                        # java level fixed
    # comp-jar: core + web sources, no dependency on the merged modules nor on boot-jar
    assert (comp / "src/main/java/com/acme/suite/core/Repo.java").exists()
    assert (comp / "src/main/java/com/acme/suite/web/Ctl.java").exists()
    keys = [d.key for d in PomEditor(comp / "pom.xml").dependencies()]
    assert keys == ["org.projectlombok:lombok", "com.google.guava:guava"]
    r = PomEditor(root / "pom.xml")
    assert r.modules() == ["module/comp-jar", "module/boot-jar"]
    assert any("as-is boot module" in n and "kept as module/boot-jar" in n for n in res.notes)
    assert not any("has no @SpringBootApplication" in n for n in res.notes)
    # second run: already restructured
    res2 = Restructurer().apply(ws, prof, cfg, mode="layout")
    assert res2.ok and "already restructured" in res2.notes and res2.changed_files == []


def test_layout_merge_kept_boot_module_without_application_and_plugin(tmp_path: Path, cfg: Config) -> None:
    files = kept_boot_files()
    del files["boot-jar/src/main/java/com/acme/suite/SuiteApplication.java"]
    files["boot-jar/pom.xml"] = files["boot-jar/pom.xml"].replace(
        files["boot-jar/pom.xml"][files["boot-jar/pom.xml"].index("    <build>"):], "</project>\n")
    files["other/boot-jar/pom.xml"] = files["web/pom.xml"].replace("suite-web", "suite-boot2").replace(
        "<version>5.0</version>\n    </parent>",
        "<version>5.0</version>\n        <relativePath>../../pom.xml</relativePath>\n    </parent>")
    files["other/boot-jar/src/main/java/com/acme/suite/x/X.java"] = java("com.acme.suite.x", "X")
    files["pom.xml"] = files["pom.xml"].replace("        <module>boot-jar</module>\n",
                                                "        <module>boot-jar</module>\n        <module>other/boot-jar</module>\n")
    ws = make_ws(tmp_path, files, "kept-noapp")
    p = multi_profile(ws)
    p.source.main_classes = []
    res = Restructurer().apply(ws, p, cfg, mode="layout")
    assert res.ok, res.error
    assert res.kept_boot_module == "module/boot-jar" and res.merged_modules == ["core", "web", "other/boot-jar"]
    assert any("has no @SpringBootApplication class" in n for n in res.notes)
    assert any("other/boot-jar is also named boot-jar" in n for n in res.notes)
    assert (ws.root / "module/comp-jar/src/main/java/com/acme/suite/x/X.java").exists()
    assert not (ws.root / "module/boot-jar/src/main/java").exists()                 # nothing generated
    b = PomEditor(ws.root / "module/boot-jar/pom.xml")
    plugin = b.find_plugins("org.springframework.boot", "spring-boot-maven-plugin", where="build")
    assert plugin and "repackage" in "".join(plugin[0].itertext())
    assert [d.key for d in b.dependencies()][0] == "${project.groupId}:suite-comp"


def _conflict_files() -> dict[str, str]:
    files = multi_files()
    files["core/src/main/resources/messages.properties"] = "greeting=core\n"
    files["web/src/main/resources/messages.properties"] = "greeting=web\n"
    files["core/src/main/resources/META-INF/shared.txt"] = "same\n"
    files["web/src/main/resources/META-INF/shared.txt"] = "same\n"
    return files


def test_layout_merge_collisions_first_wins(tmp_path: Path, cfg: Config) -> None:
    ws = make_ws(tmp_path, _conflict_files(), "firstwins")
    res = Restructurer().apply(ws, multi_profile(ws), cfg, mode="layout")
    assert res.ok, res.error
    comp = ws.root / "module/comp-jar"
    assert (comp / "src/main/resources/messages.properties").read_text() == "greeting=core\n"
    assert (comp / "src/main/resources/META-INF/shared.txt").read_text() == "same\n"
    saved = ws.run_dir / "merge-conflicts/web/src/main/resources/messages.properties"
    assert saved.read_text() == "greeting=web\n"
    assert not (ws.run_dir / "merge-conflicts/web/src/main/resources/META-INF").exists()
    by_path = {mc.path: mc for mc in res.merge_conflicts}
    assert set(by_path) == {"src/main/resources/messages.properties", "src/main/resources/META-INF/shared.txt"}
    diff = by_path["src/main/resources/messages.properties"]
    assert diff.kept_from == "core" and diff.dropped_from == ["web"] and not diff.identical
    assert diff.saved_copies == ["merge-conflicts/web/src/main/resources/messages.properties"]
    same = by_path["src/main/resources/META-INF/shared.txt"]
    assert same.identical and same.saved_copies == [] and same.kept_from == "core" and same.dropped_from == ["web"]
    assert "1 identical duplicate file(s) dropped: src/main/resources/META-INF/shared.txt" in res.notes
    fu = [f for f in res.manual_followups if f.startswith("merge conflicts:")]
    assert len(fu) == 1 and fu[0].startswith("merge conflicts: 1 file(s) existed in more than one module")
    assert f"{ws.run_dir}/merge-conflicts/" in fu[0] and fu[0].endswith("src/main/resources/messages.properties")
    assert not (ws.root / "core").exists() and not (ws.root / "web").exists()
    assert res.kept_boot_module is None and res.merged_modules == ["core", "web"]


def test_layout_merge_collision_with_root_src(tmp_path: Path, cfg: Config) -> None:
    files = _conflict_files()
    files["src/main/resources/messages.properties"] = "greeting=root\n"
    ws = make_ws(tmp_path, files, "rootwins")
    res = Restructurer().apply(ws, multi_profile(ws), cfg, mode="layout")
    assert res.ok, res.error
    assert (ws.root / "module/comp-jar/src/main/resources/messages.properties").read_text() == "greeting=root\n"
    mc = next(m for m in res.merge_conflicts if m.path == "src/main/resources/messages.properties")
    assert mc.kept_from == "<root>" and mc.dropped_from == ["core", "web"]
    assert (ws.run_dir / "merge-conflicts/core/src/main/resources/messages.properties").read_text() == "greeting=core\n"
    assert (ws.run_dir / "merge-conflicts/web/src/main/resources/messages.properties").read_text() == "greeting=web\n"


def test_layout_merge_not_already_restructured_without_comp_jar(tmp_path: Path, cfg: Config) -> None:
    files = {}
    for rel, text in kept_boot_files().items():
        if rel == "pom.xml":
            text = text.replace("<module>core</module>", "<module>module/core</module>").replace(
                "<module>web</module>", "<module>module/web</module>").replace(
                "<module>boot-jar</module>", "<module>module/boot-jar</module>")
        elif not rel.startswith("src/"):
            text = text.replace("<version>5.0</version>\n    </parent>",
                                "<version>5.0</version>\n        <relativePath>../../pom.xml</relativePath>\n"
                                "    </parent>").replace("<relativePath>../pom.xml</relativePath>",
                                                         "<relativePath>../../pom.xml</relativePath>")
            rel = "module/" + rel
        files[rel] = text
    ws = make_ws(tmp_path, files, "undermodule")
    wrap_like = Restructurer().apply(ws, multi_profile(ws), cfg, mode="layout")
    assert wrap_like.ok, wrap_like.error
    assert "already restructured" not in wrap_like.notes
    assert wrap_like.kept_boot_module == "module/boot-jar"
    assert wrap_like.merged_modules == ["module/core", "module/web"]
    root = ws.root
    assert not (root / "module/core").exists() and not (root / "module/web").exists()
    assert (root / "module/comp-jar/src/main/java/com/acme/suite/core/Repo.java").exists()
    assert (root / "module/boot-jar/src/main/java/com/acme/suite/SuiteApplication.java").exists()
    assert PomEditor(root / "pom.xml").modules() == ["module/comp-jar", "module/boot-jar"]
    assert [d.key for d in PomEditor(root / "module/boot-jar/pom.xml").dependencies()][0] == \
        "${project.groupId}:suite-comp"


def test_layout_wrap_already_restructured_rule_unchanged(tmp_path: Path, cfg: Config) -> None:
    root = (HEAD + "    <groupId>com.acme</groupId>\n    <artifactId>x</artifactId>\n    <version>1</version>\n"
            "    <packaging>pom</packaging>\n    <modules>\n        <module>module/core</module>\n"
            "        <module>module/boot-jar</module>\n    </modules>\n</project>\n")
    ws = make_ws(tmp_path, {"pom.xml": root}, "wrapdone")
    res = Restructurer().apply(ws, profile(ws, CaseType.PURE_JAVA, artifact_id="x", version="1"), cfg,
                               mode="layout", params={"multi_module_policy": "wrap"})
    assert res.ok and "already restructured" in res.notes


def test_layout_missing_root_pom(tmp_path: Path, cfg: Config) -> None:
    ws = make_ws(tmp_path, {"README": "x"}, "nopom")
    res = Restructurer().apply(ws, profile(ws, CaseType.PURE_JAVA), cfg, mode="layout")
    assert not res.ok and "root pom not found" in res.error


def test_unknown_mode(tmp_path: Path, cfg: Config) -> None:
    ws = make_ws(tmp_path, {"README": "x"}, "mode")
    res = Restructurer().apply(ws, profile(ws, CaseType.PURE_JAVA), cfg, mode="bogus")  # type: ignore[arg-type]
    assert not res.ok and "unknown restructure mode" in res.error


# ---------------------------------------------------------------------------------------------- msa
def msa_files() -> dict[str, str]:
    def svc(aid: str, extra_deps: str = "") -> str:
        return (HEAD + "    <parent>\n        <groupId>org.springframework.boot</groupId>\n"
                "        <artifactId>spring-boot-starter-parent</artifactId>\n        <version>3.2.0</version>\n"
                "        <relativePath/>\n    </parent>\n"
                f"    <groupId>com.acme</groupId>\n    <artifactId>{aid}</artifactId>\n    <version>1.0</version>\n"
                "    <properties>\n        <java.version>17</java.version>\n    </properties>\n"
                "    <dependencies>\n" + dep("org.springframework.boot", "spring-boot-starter-web") + extra_deps
                + "    </dependencies>\n    <build>\n        <plugins>\n            <plugin>\n"
                "                <groupId>org.springframework.boot</groupId>\n"
                "                <artifactId>spring-boot-maven-plugin</artifactId>\n"
                "                <version>3.2.0</version>\n            </plugin>\n        </plugins>\n    </build>\n"
                "</project>\n")
    top = (HEAD + "    <groupId>com.acme</groupId>\n    <artifactId>platform</artifactId>\n    <version>1.0</version>\n"
           "    <packaging>pom</packaging>\n    <modules>\n        <module>order-service</module>\n"
           "        <module>user-service</module>\n    </modules>\n</project>\n")
    return {
        "pom.xml": top,
        "order-service/pom.xml": svc("order-service", dep("com.google.guava", "guava", "30.0-jre")),
        "order-service/src/main/java/com/acme/order/OrderApp.java": java("com.acme.order", "OrderApp",
                                                                         "@SpringBootApplication"),
        "user-service/pom.xml": svc("user-service"),
    }


def msa_profile(ws):
    p = profile(ws, CaseType.MSA, artifact_id="platform", version="1.0", packaging="pom")
    p.services = [ServiceInfo(name="order-service", root_pom="order-service/pom.xml", case_type=CaseType.SPRING_BOOT),
                  ServiceInfo(name="user-service", root_pom="user-service/pom.xml", case_type=CaseType.SPRING_BOOT)]
    return p


def test_msa_pom_only(tmp_path: Path, cfg: Config) -> None:
    ws = make_ws(tmp_path, msa_files(), "msa")
    before = sorted(p.relative_to(ws.root).as_posix() for p in ws.root.rglob("*") if ".git" not in p.parts)
    res = Restructurer().apply(ws, msa_profile(ws), cfg, mode="msa_pom")
    assert res.ok, res.error
    after = sorted(p.relative_to(ws.root).as_posix() for p in ws.root.rglob("*") if ".git" not in p.parts)
    assert before == after                                                    # no directory moved
    for s in ("order-service", "user-service"):
        x = xml(ws.root / s / "pom.xml")
        assert t(x, "parent/artifactId") == "spring-boot-starter-parent" and t(x, "parent/version") == "4.1.0"
        assert t(x, "parent/relativePath") == ""
        keys = dep_keys(x)
        assert keys[0] == "org.springframework.boot:spring-boot-starter-web" and keys[-2:] == MANDATORY
        assert t(x, "properties/maven.compiler.release") == "21" and t(x, "properties/java.version") == "21"
        assert t(all_(x, "build/plugins/plugin")[0], "version") is None       # boot plugin version → parent
    o = PomEditor(ws.root / "order-service/pom.xml")
    assert o.dependencies()[1].version == "33.4.8-jre"
    assert sorted(res.changed_files) == ["order-service/pom.xml", "user-service/pom.xml"]
    assert PomEditor(ws.root / "pom.xml").get_parent() is None
    assert set(res.mandatory_added) == set(MANDATORY)


def test_msa_top_aggregator_and_climb(tmp_path: Path, cfg: Config) -> None:
    files = msa_files()
    # user-service inherits from the in-repo top pom → climbing must land on pom.xml
    files["user-service/pom.xml"] = files["user-service/pom.xml"].replace(
        "<groupId>org.springframework.boot</groupId>\n        <artifactId>spring-boot-starter-parent</artifactId>\n"
        "        <version>3.2.0</version>\n        <relativePath/>",
        "<groupId>com.acme</groupId>\n        <artifactId>platform</artifactId>\n        <version>1.0</version>")
    ws = make_ws(tmp_path, files, "msa2")
    res = Restructurer().apply(ws, msa_profile(ws), cfg, mode="msa_pom")
    assert res.ok, res.error
    assert PomEditor(ws.root / "pom.xml").get_parent().artifact_id == "spring-boot-starter-parent"
    assert PomEditor(ws.root / "user-service/pom.xml").get_parent().artifact_id == "platform"
    assert any("inherits from in-repo pom.xml" in n for n in res.notes)

    cfg.layout.msa_parent_policy = "top-aggregator"
    ws2 = make_ws(tmp_path, msa_files(), "msa3")
    res2 = Restructurer().apply(ws2, msa_profile(ws2), cfg, mode="msa_pom")
    assert res2.ok
    assert PomEditor(ws2.root / "pom.xml").get_parent().artifact_id == "spring-boot-starter-parent"
    assert PomEditor(ws2.root / "order-service/pom.xml").get_parent().version == "3.2.0"


def test_msa_fallback_targets_and_bom(tmp_path: Path, cfg: Config) -> None:
    cfg.artifacts.parent_manages_spring_boot = False
    ws = make_ws(tmp_path, msa_files(), "msa4")
    p = profile(ws, CaseType.MSA, artifact_id="platform", version="1.0")
    p.poms = [PomInfo(path="order-service/pom.xml", artifact_id="order-service", is_root=True),
              PomInfo(path="nope/pom.xml", artifact_id="nope", is_root=True)]
    res = Restructurer().apply(ws, p, cfg, mode="msa_pom")
    assert res.ok, res.error
    o = PomEditor(ws.root / "order-service/pom.xml")
    assert o.managed_dependencies()[0].key == "org.springframework.boot:spring-boot-dependencies"
    assert o.plugins()[0].version == "4.1.0"
    assert PomEditor(ws.root / "user-service/pom.xml").get_parent().version == "3.2.0"
    assert any("nope/pom.xml not found" in n for n in res.notes)
    ws2 = make_ws(tmp_path, {"README": "x"}, "msa5")
    p2 = profile(ws2, CaseType.MSA)
    p2.root_pom = None
    assert not Restructurer().apply(ws2, p2, cfg, mode="msa_pom").ok


def test_optional_alias_and_property_name(tmp_path: Path, cfg: Config) -> None:
    from sbmig.core.config import ArtifactSpec
    cfg.artifacts.optional = [
        ArtifactSpec(group_id="com.company.lib", artifact_id="lib-auth", version="3.0.0",
                     aliases=["com.company.legacy:auth"]),
        ArtifactSpec(group_id="com.company.lib", artifact_id="lib-db", version="3.0.0",
                     property_name="lib-db.version"),
        ArtifactSpec(group_id="com.company.lib", artifact_id="lib-web", version="3.0.0"),
    ]
    pom = (HEAD + "    <groupId>com.acme</groupId>\n    <artifactId>a</artifactId>\n    <version>1</version>\n"
           "    <properties>\n        <lib-db.version>1.0</lib-db.version>\n    </properties>\n"
           "    <dependencies>\n" + dep("com.company.legacy", "auth", "1.2")
           + dep("com.company.lib", "lib-db", "1.0") + "    </dependencies>\n</project>\n")
    ws = make_ws(tmp_path, {"pom.xml": pom, "src/main/java/a/A.java": java("a", "A")}, "alias")
    res = Restructurer().apply(ws, profile(ws, CaseType.PURE_JAVA, artifact_id="a", version="1",
                                           base_package="a"), cfg, mode="layout")
    assert res.ok, res.error
    c = PomEditor(ws.root / "module/comp-jar/pom.xml")
    assert [(d.key, d.version) for d in c.dependencies()] == [("com.company.lib:lib-auth", "3.0.0"),
                                                              ("com.company.lib:lib-db", "${lib-db.version}")]
    assert PomEditor(ws.root / "pom.xml").get_property("lib-db.version") == "3.0.0"
    assert "com.company.lib:lib-auth 1.2→3.0.0" in res.optional_bumped
    assert "com.company.lib:lib-db 1.0→3.0.0" in res.optional_bumped
    assert not any("lib-web" in b for b in res.optional_bumped)           # absent optional never added
    assert all("lib-web" not in d.key for d in c.dependencies())
    assert any("renamed to com.company.lib:lib-auth" in n for n in res.notes)


def test_boot_jar_inherits_asis_test_and_runtime_deps_when_tests_move(tmp_path: Path, cfg_rm: Config) -> None:
    """Moved @SpringBootTest classes need the as-is test classpath (modular *-test starters, security-test, h2)."""
    files = boot2_files()
    files["pom.xml"] = files["pom.xml"].replace(
        dep("org.springframework.boot", "spring-boot-starter-test", scope="test"),
        dep("org.springframework.boot", "spring-boot-starter-test", scope="test")
        + dep("org.springframework.boot", "spring-boot-starter-webmvc-test", scope="test")
        + dep("org.springframework.boot", "spring-boot-starter-security-test", scope="test")
        + dep("com.h2database", "h2", scope="runtime"))
    ws = make_ws(tmp_path, files, "boot2-testdeps")
    res = Restructurer().apply(ws, boot2_profile(ws), cfg_rm, mode="layout")
    assert res.ok, res.error
    b = xml(ws.root / "module/boot-jar/pom.xml")
    keys = dep_keys(b)
    assert "org.springframework.boot:spring-boot-starter-webmvc-test" in keys
    assert "org.springframework.boot:spring-boot-starter-security-test" in keys
    assert "com.h2database:h2" in keys
    assert keys.count("org.springframework.boot:spring-boot-starter-test") == 1
    bdeps = {f"{t(d, 'groupId')}:{t(d, 'artifactId')}": d for d in all_(b, "dependencies/dependency")}
    assert t(bdeps["org.springframework.boot:spring-boot-starter-security-test"], "scope") == "test"
    assert t(bdeps["com.h2database:h2"], "scope") == "runtime"
    assert "org.springframework.boot:spring-boot-starter-web" not in keys       # compile deps come via comp-jar


def test_optional_bump_prefers_conventional_override_property(tmp_path: Path, cfg_rm: Config) -> None:
    """lombok has no version (managed by the parent) but the project defines <lombok.version> → bump the property
    (policy pin: the configured version is set exactly, even if lower)."""
    cfg_rm.artifacts.optional_version_policy = "pin"
    files = boot2_files()
    files["pom.xml"] = files["pom.xml"].replace(
        "        <guava.version>30.0-jre</guava.version>\n",
        "        <guava.version>30.0-jre</guava.version>\n        <lombok.version>1.18.48</lombok.version>\n")
    ws = make_ws(tmp_path, files, "boot2-lombokprop")
    res = Restructurer().apply(ws, boot2_profile(ws), cfg_rm, mode="layout")
    assert res.ok, res.error
    r = xml(ws.root / "pom.xml")
    assert t(r, "properties/lombok.version") == "1.18.38"
    c = xml(ws.root / "module/comp-jar/pom.xml")
    cdeps = {f"{t(d, 'groupId')}:{t(d, 'artifactId')}": d for d in all_(c, "dependencies/dependency")}
    assert t(cdeps["org.projectlombok:lombok"], "version") is None
    assert any(e.startswith("org.projectlombok:lombok 1.18.48→1.18.38") for e in res.optional_bumped)


def test_optional_bump_floor_policy_never_downgrades(tmp_path: Path, cfg_rm: Config) -> None:
    """Default policy `floor`: a newer as-is/recipe version (lombok 1.18.48 > configured 1.18.38) is kept, while an
    older one (guava 30.0-jre < 33.4.8-jre) is still raised."""
    assert cfg_rm.artifacts.optional_version_policy == "floor"
    files = boot2_files()
    files["pom.xml"] = files["pom.xml"].replace(
        "        <guava.version>30.0-jre</guava.version>\n",
        "        <guava.version>30.0-jre</guava.version>\n        <lombok.version>1.18.48</lombok.version>\n")
    ws = make_ws(tmp_path, files, "boot2-floor")
    res = Restructurer().apply(ws, boot2_profile(ws), cfg_rm, mode="layout")
    assert res.ok, res.error
    r = xml(ws.root / "pom.xml")
    assert t(r, "properties/lombok.version") == "1.18.48"
    assert t(r, "properties/guava.version") == "33.4.8-jre"
    # the property (1.18.48) is kept; only the older annotationProcessorPaths entry (1.18.20) is raised
    assert not any(e.startswith("org.projectlombok:lombok 1.18.48") for e in res.optional_bumped)
    assert any(e.startswith("com.google.guava:guava 30.0-jre→33.4.8-jre") for e in res.optional_bumped)
    assert any("optional_version_policy=floor" in n for n in res.notes)
