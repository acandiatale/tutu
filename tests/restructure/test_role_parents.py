"""Round 9b: role parents — root on `artifacts.parent` (aggregator), module/boot-jar on `boot_parent`,
module/comp-jar on `comp_parent`; old role parents (an earlier migration's 1.0.0) move to the configured version."""
from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.core.config import ArtifactSpec, Config
from sbmig.restructure import PomEditor, Restructurer

from .helpers import HEAD, CaseType, ServiceInfo, all_, dep, dep_keys, java, make_ws, profile, t, xml
from .test_restructurer import boot2_files, boot2_profile, msa_files, msa_profile, multi_files, multi_profile

PLATFORM = "com.company.platform"
MANDATORY = ["org.springframework.boot:spring-boot-starter-validation",
             "org.springframework.boot:spring-boot-starter-actuator"]
BOOT = "module/boot-jar"
COMP = "module/comp-jar"
STARTER = "org.springframework.boot:spring-boot-starter-parent"


def _spec(aid: str, version: str = "4.0.0") -> ArtifactSpec:
    return ArtifactSpec(group_id=PLATFORM, artifact_id=aid, version=version)


def _set_versions(cfg: Config, version: str) -> None:
    cfg.artifacts.parent = _spec("base-parent", version)
    cfg.artifacts.boot_parent = _spec("boot-jar", version)
    cfg.artifacts.comp_parent = _spec("comp-jar", version)


@pytest.fixture()
def rcfg(cfg: Config) -> Config:
    _set_versions(cfg, "4.0.0")
    assert cfg.artifacts.role_parents
    return cfg


def _parent(x) -> str:
    return f"{t(x, 'parent/groupId')}:{t(x, 'parent/artifactId')}:{t(x, 'parent/version')}"


def _tree(ws) -> dict[str, bytes]:
    return {p.relative_to(ws.root).as_posix(): p.read_bytes()
            for p in sorted(ws.root.rglob("*")) if p.is_file() and ".git" not in p.parts}


# ---------------------------------------------------------------------------------------------- (a) single module
def test_single_module_role_parents(tmp_path: Path, rcfg: Config) -> None:
    ws = make_ws(tmp_path, boot2_files(), "single")
    res = Restructurer().apply(ws, boot2_profile(ws), rcfg, mode="layout")
    assert res.ok, res.error
    r = xml(ws.root / "pom.xml")
    assert _parent(r) == f"{PLATFORM}:base-parent:4.0.0" and t(r, "parent/relativePath") == ""
    assert t(r, "packaging") == "pom" and [m.text for m in all_(r, "modules/module")] == [COMP, BOOT]
    assert dep_keys(r) == [] and all_(r, "dependencyManagement") == []          # aggregator: nothing to inherit
    assert all_(r, "properties") == []                                          # moved into the module poms
    assert t(r, "groupId") == "com.acme" and t(r, "version") == "1.0.0-SNAPSHOT"
    for mod, role, aid in ((COMP, "comp-jar", "shop-comp"), (BOOT, "boot-jar", "shop-boot")):
        x = xml(ws.root / mod / "pom.xml")
        assert _parent(x) == f"{PLATFORM}:{role}:4.0.0"
        assert t(x, "parent/relativePath") == ""                                  # external parent: no lookup
        assert (t(x, "groupId"), t(x, "artifactId"), t(x, "version")) == ("com.acme", aid, "1.0.0-SNAPSHOT")
        assert dep_keys(x)[-2:] == MANDATORY                                    # mandatory in both module poms
        assert t(x, "properties/maven.compiler.release") == "21"               # parent_manages_java_version false
        assert t(x, "repositories/repository/id") == "corp"                      # root repositories carried over
    c = xml(ws.root / COMP / "pom.xml")
    assert t(c, "properties/guava.version") == "33.4.8-jre"                    # root property → comp, bumped there
    b = xml(ws.root / BOOT / "pom.xml")
    assert dep_keys(b)[0] == "${project.groupId}:shop-comp"
    assert t(b, "dependencies/dependency/version") == "${project.version}"
    assert '<relativePath/>' in (ws.root / COMP / "pom.xml").read_text()
    assert res.parents_set == [
        f"module/comp-jar/pom.xml: - → {PLATFORM}:comp-jar:4.0.0",
        f"module/boot-jar/pom.xml: - → {PLATFORM}:boot-jar:4.0.0",
        f"pom.xml: org.springframework.boot:spring-boot-starter-parent:2.7.18 → {PLATFORM}:base-parent:4.0.0"]
    assert any(n.startswith("role parents: 3 parent(s) set") for n in res.notes)
    assert set(res.mandatory_added) == set(MANDATORY)


def test_single_module_role_parents_bom_in_both_modules(tmp_path: Path, rcfg: Config) -> None:
    rcfg.artifacts.parent_manages_spring_boot = False
    ws = make_ws(tmp_path, boot2_files(), "single-bom")
    res = Restructurer().apply(ws, boot2_profile(ws), rcfg, mode="layout")
    assert res.ok, res.error
    assert all_(xml(ws.root / "pom.xml"), "dependencyManagement") == []
    for mod in (COMP, BOOT):
        x = xml(ws.root / mod / "pom.xml")
        assert dep_keys(x, "dependencyManagement/dependencies/dependency") == [
            "org.springframework.boot:spring-boot-dependencies"]


# ---------------------------------------------------------------------------------------------- (b) the user's case
def _legacy_files(parent_group: str = PLATFORM, boot_parent: str = "boot-jar", comp_parent: str = "comp-jar",
                  version: str = "1.0.0") -> dict[str, str]:
    """root{boot-jar, catalog-service, order-service} migrated by an earlier tool: every module on an external
    in-house role parent 1.0.0, explicit module coordinates."""
    def ext_parent(aid: str) -> str:
        return (f"    <parent>\n        <groupId>{parent_group}</groupId>\n        <artifactId>{aid}</artifactId>\n"
                f"        <version>{version}</version>\n        <relativePath/>\n    </parent>\n")

    def coords(aid: str) -> str:
        return f"    <groupId>com.acme</groupId>\n    <artifactId>{aid}</artifactId>\n    <version>2.0.0</version>\n"

    root = (HEAD + ext_parent("base-parent") + coords("shop")
            + "    <packaging>pom</packaging>\n    <modules>\n        <module>boot-jar</module>\n"
            "        <module>catalog-service</module>\n        <module>order-service</module>\n    </modules>\n"
            "    <properties>\n        <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>\n"
            "    </properties>\n</project>\n")
    boot = (HEAD + ext_parent(boot_parent) + coords("shop-boot")
            + "    <dependencies>\n" + dep("com.acme", "catalog-service", "${project.version}")
            + dep("com.acme", "order-service", "${project.version}")
            + dep("org.springframework.boot", "spring-boot-starter-web")
            + "    </dependencies>\n    <build>\n        <plugins>\n            <plugin>\n"
            "                <groupId>org.springframework.boot</groupId>\n"
            "                <artifactId>spring-boot-maven-plugin</artifactId>\n"
            "            </plugin>\n        </plugins>\n    </build>\n</project>\n")
    catalog = (HEAD + ext_parent(comp_parent) + coords("catalog-service")
               + "    <dependencies>\n" + dep("com.google.guava", "guava", "31.0-jre") + "    </dependencies>\n"
               "</project>\n")
    order = (HEAD + ext_parent(comp_parent) + coords("order-service")
             + "    <properties>\n        <mapper.version>3.5.1</mapper.version>\n    </properties>\n"
             "    <dependencyManagement>\n        <dependencies>\n"
             + dep("org.mybatis", "mybatis", "${mapper.version}").replace("        <", "            <")
             + "        </dependencies>\n    </dependencyManagement>\n"
             "    <dependencies>\n" + dep("com.acme", "catalog-service", "${project.version}")
             + dep("org.mybatis", "mybatis") + "    </dependencies>\n</project>\n")
    return {
        "pom.xml": root,
        "boot-jar/pom.xml": boot,
        "boot-jar/src/main/java/com/acme/shop/ShopApplication.java": java(
            "com.acme.shop", "ShopApplication", "@SpringBootApplication"),
        "boot-jar/src/main/resources/application.yml": "server:\n  port: 8080\n",
        "catalog-service/pom.xml": catalog,
        "catalog-service/src/main/java/com/acme/shop/catalog/Catalog.java": java("com.acme.shop.catalog", "Catalog"),
        "order-service/pom.xml": order,
        "order-service/src/main/java/com/acme/shop/order/Order.java": java("com.acme.shop.order", "Order"),
    }


def _legacy_profile(ws):
    p = profile(ws, CaseType.SPRING_BOOT, artifact_id="shop", version="2.0.0", packaging="pom",
                base_package="com.acme.shop", main_classes=["com.acme.shop.ShopApplication"])
    p.is_multi_module = True
    return p


def test_legacy_role_parents_merged_and_moved(tmp_path: Path, rcfg: Config) -> None:
    ws = make_ws(tmp_path, _legacy_files(), "legacy")
    res = Restructurer().apply(ws, _legacy_profile(ws), rcfg, mode="layout")
    assert res.ok, res.error
    root = ws.root
    for gone in ("boot-jar", "catalog-service", "order-service"):
        assert not (root / gone).exists()
    assert res.kept_boot_module == BOOT and res.merged_modules == ["catalog-service", "order-service"]
    r = xml(root / "pom.xml")
    assert _parent(r) == f"{PLATFORM}:base-parent:4.0.0"
    assert [m.text for m in all_(r, "modules/module")] == [COMP, BOOT] and dep_keys(r) == []
    assert all_(r, "dependencyManagement") == []
    b = xml(root / BOOT / "pom.xml")
    assert _parent(b) == f"{PLATFORM}:boot-jar:4.0.0" and t(b, "parent/relativePath") == ""
    assert (t(b, "groupId"), t(b, "artifactId"), t(b, "version")) == ("com.acme", "shop-boot", "2.0.0")
    assert dep_keys(b) == ["${project.groupId}:shop-comp", "org.springframework.boot:spring-boot-starter-web",
                           *MANDATORY]
    assert t(b, "dependencies/dependency/version") == "${project.version}"
    assert t(b, "properties/project.build.sourceEncoding") is None             # never inherited the root pom
    assert (root / BOOT / "src/main/java/com/acme/shop/ShopApplication.java").exists()
    c = xml(root / COMP / "pom.xml")
    assert _parent(c) == f"{PLATFORM}:comp-jar:4.0.0" and t(c, "parent/relativePath") == ""
    assert (t(c, "groupId"), t(c, "artifactId"), t(c, "version")) == ("com.acme", "shop-comp", "2.0.0")
    assert dep_keys(c) == ["com.google.guava:guava", "org.mybatis:mybatis", *MANDATORY]
    # the merged modules' management / the root's properties live in the comp pom now
    assert dep_keys(c, "dependencyManagement/dependencies/dependency") == ["org.mybatis:mybatis"]
    assert t(c, "properties/mapper.version") == "3.5.1"
    assert t(c, "properties/project.build.sourceEncoding") == "UTF-8"
    assert (root / COMP / "src/main/java/com/acme/shop/catalog/Catalog.java").exists()
    assert (root / COMP / "src/main/java/com/acme/shop/order/Order.java").exists()
    assert sorted(res.parents_set) == sorted([
        f"module/comp-jar/pom.xml: - → {PLATFORM}:comp-jar:4.0.0",
        f"module/boot-jar/pom.xml: {PLATFORM}:boot-jar:1.0.0 → {PLATFORM}:boot-jar:4.0.0",
        f"pom.xml: {PLATFORM}:base-parent:1.0.0 → {PLATFORM}:base-parent:4.0.0"])
    assert not any("parent left unchanged" in n for n in res.notes)


def test_legacy_boot_module_on_comp_parent_is_replaced(tmp_path: Path, rcfg: Config) -> None:
    ws = make_ws(tmp_path, _legacy_files(boot_parent="comp-jar"), "legacy-comp")
    res = Restructurer().apply(ws, _legacy_profile(ws), rcfg, mode="layout")
    assert res.ok, res.error
    assert _parent(xml(ws.root / BOOT / "pom.xml")) == f"{PLATFORM}:boot-jar:4.0.0"
    assert f"module/boot-jar/pom.xml: {PLATFORM}:comp-jar:1.0.0 → {PLATFORM}:boot-jar:4.0.0" in res.parents_set


def test_kept_boot_module_inheriting_root_gets_root_content(tmp_path: Path, rcfg: Config) -> None:
    from .test_restructurer import kept_boot_files
    files = kept_boot_files()
    files["pom.xml"] = files["pom.xml"].replace("    </properties>", "        <boot.flag>on</boot.flag>\n    </properties>")
    files["boot-jar/pom.xml"] = files["boot-jar/pom.xml"].replace(
        "<layers><enabled>true</enabled></layers>", "<layers><enabled>true</enabled></layers><x>${boot.flag}</x>")
    ws = make_ws(tmp_path, files, "kept-root")
    res = Restructurer().apply(ws, multi_profile(ws), rcfg, mode="layout")
    assert res.ok, res.error
    b = xml(ws.root / BOOT / "pom.xml")
    assert _parent(b) == f"{PLATFORM}:boot-jar:4.0.0" and t(b, "parent/relativePath") == ""
    assert (t(b, "groupId"), t(b, "version")) == ("com.acme", "5.0")           # explicit, from the root
    assert t(b, "properties/lombok.version") is None                           # not referenced by the boot pom
    assert t(b, "properties/boot.flag") == "on"                                 # referenced root property copied
    assert dep_keys(b)[-2:] == MANDATORY
    assert f"module/boot-jar/pom.xml: com.acme:suite:5.0 → {PLATFORM}:boot-jar:4.0.0" in res.parents_set


def test_stand_in_parents_sharing_one_key(tmp_path: Path, cfg: Config) -> None:
    """The local e2e fixture: all three parents are spring-boot-starter-parent 4.1.0 (one key)."""
    starter = ArtifactSpec(group_id="org.springframework.boot", artifact_id="spring-boot-starter-parent",
                           version="4.1.0")
    cfg.artifacts.boot_parent = starter
    cfg.artifacts.comp_parent = starter
    ws = make_ws(tmp_path, _legacy_files("org.springframework.boot", "spring-boot-starter-parent",
                                         "spring-boot-starter-parent", "2.7.18"), "stand-in")
    files = ws.root / "pom.xml"
    files.write_text(files.read_text().replace("<artifactId>base-parent</artifactId>",
                                               "<artifactId>spring-boot-starter-parent</artifactId>"))
    res = Restructurer().apply(ws, _legacy_profile(ws), cfg, mode="layout")
    assert res.ok, res.error
    for pom in ("pom.xml", f"{BOOT}/pom.xml", f"{COMP}/pom.xml"):
        x = xml(ws.root / pom)
        assert _parent(x) == "org.springframework.boot:spring-boot-starter-parent:4.1.0"
        assert t(x, "parent/relativePath") == ""
    assert dep_keys(xml(ws.root / "pom.xml")) == []
    assert len(res.parents_set) == 3
    res2 = Restructurer().apply(ws, _legacy_profile(ws), cfg, mode="layout")
    assert res2.ok and "already restructured" in res2.notes and res2.changed_files == []


# ---------------------------------------------------------------------------------------------- (c) idempotency
def test_second_run_is_a_no_op_and_version_move_moves_all_three(tmp_path: Path, rcfg: Config) -> None:
    ws = make_ws(tmp_path, _legacy_files(), "idem")
    assert Restructurer().apply(ws, _legacy_profile(ws), rcfg, mode="layout").ok
    before = _tree(ws)
    res2 = Restructurer().apply(ws, _legacy_profile(ws), rcfg, mode="layout")
    assert res2.ok and "already restructured" in res2.notes
    assert res2.changed_files == [] and res2.parents_set == [] and _tree(ws) == before
    _set_versions(rcfg, "4.1.0")
    res3 = Restructurer().apply(ws, _legacy_profile(ws), rcfg, mode="layout")
    assert res3.ok, res3.error
    assert sorted(res3.parents_set) == sorted([
        f"pom.xml: {PLATFORM}:base-parent:4.0.0 → {PLATFORM}:base-parent:4.1.0",
        f"module/boot-jar/pom.xml: {PLATFORM}:boot-jar:4.0.0 → {PLATFORM}:boot-jar:4.1.0",
        f"module/comp-jar/pom.xml: {PLATFORM}:comp-jar:4.0.0 → {PLATFORM}:comp-jar:4.1.0"])
    assert _parent(xml(ws.root / "pom.xml")) == f"{PLATFORM}:base-parent:4.1.0"
    assert _parent(xml(ws.root / BOOT / "pom.xml")) == f"{PLATFORM}:boot-jar:4.1.0"
    assert _parent(xml(ws.root / COMP / "pom.xml")) == f"{PLATFORM}:comp-jar:4.1.0"
    res4 = Restructurer().apply(ws, _legacy_profile(ws), rcfg, mode="layout")
    assert res4.ok and res4.changed_files == [] and res4.parents_set == []


def test_already_restructured_module_on_foreign_parent_is_noted(tmp_path: Path, cfg: Config, rcfg: Config) -> None:
    # restructured earlier WITHOUT role parents (modules inherit the root): a later role run leaves them, with a note
    rcfg.artifacts.boot_parent = rcfg.artifacts.comp_parent = None
    rcfg.artifacts.parent = _spec("base-parent")
    ws = make_ws(tmp_path, boot2_files(), "foreign")
    assert Restructurer().apply(ws, boot2_profile(ws), rcfg, mode="layout").ok
    _set_versions(rcfg, "4.0.0")
    res = Restructurer().apply(ws, boot2_profile(ws), rcfg, mode="layout")
    assert res.ok and "already restructured" in res.notes and res.parents_set == []
    assert any(n.startswith("module/boot-jar/pom.xml inherits com.acme:shop (not the role parent") for n in res.notes)
    assert _parent(xml(ws.root / BOOT / "pom.xml")) == "com.acme:shop:1.0.0-SNAPSHOT"


def test_wrap_moves_old_role_parents_of_wrapped_modules(tmp_path: Path, rcfg: Config) -> None:
    files = multi_files()
    files["core/pom.xml"] = files["core/pom.xml"].replace(
        "<groupId>com.acme</groupId>\n        <artifactId>suite</artifactId>\n        <version>5.0</version>\n",
        f"<groupId>{PLATFORM}</groupId>\n        <artifactId>comp-jar</artifactId>\n        <version>1.0.0</version>\n"
        "        <relativePath/>\n")
    ws = make_ws(tmp_path, files, "wrap-role")
    res = Restructurer().apply(ws, multi_profile(ws), rcfg, mode="layout", params={"multi_module_policy": "wrap"})
    assert res.ok, res.error
    core = xml(ws.root / "module/core/pom.xml")
    assert _parent(core) == f"{PLATFORM}:comp-jar:4.0.0" and t(core, "parent/relativePath") == ""
    assert (t(core, "groupId"), t(core, "version")) == (PLATFORM, "1.0.0")     # effective coordinates kept
    assert f"module/core/pom.xml: {PLATFORM}:comp-jar:1.0.0 → {PLATFORM}:comp-jar:4.0.0" in res.parents_set
    web = xml(ws.root / "module/web/pom.xml")
    assert t(web, "parent/artifactId") == "suite"                               # still inherits the root (wrap)
    b = xml(ws.root / BOOT / "pom.xml")
    assert _parent(b) == f"{PLATFORM}:boot-jar:4.0.0" and dep_keys(b)[-2:] == MANDATORY
    assert dep_keys(xml(ws.root / "pom.xml"))[-2:] == MANDATORY                 # wrapped modules inherit the root


# ---------------------------------------------------------------------------------------------- (d) MSA
def _msa_role_files() -> dict[str, str]:
    files = msa_files()
    files["shared-lib/pom.xml"] = (
        HEAD + f"    <parent>\n        <groupId>{PLATFORM}</groupId>\n        <artifactId>comp-jar</artifactId>\n"
        "        <version>1.0.0</version>\n        <relativePath/>\n    </parent>\n"
        "    <groupId>com.acme</groupId>\n    <artifactId>shared-lib</artifactId>\n    <version>1.0</version>\n"
        "</project>\n")
    return files


def _msa_role_profile(ws):
    p = msa_profile(ws)
    p.services.append(ServiceInfo(name="shared-lib", root_pom="shared-lib/pom.xml", case_type=CaseType.PURE_JAVA,
                                  kind="library"))
    return p


def test_msa_role_parents(tmp_path: Path, rcfg: Config) -> None:
    ws = make_ws(tmp_path, _msa_role_files(), "msa-role")
    res = Restructurer().apply(ws, _msa_role_profile(ws), rcfg, mode="msa_pom")
    assert res.ok, res.error
    for s in ("order-service", "user-service"):
        x = xml(ws.root / s / "pom.xml")
        assert _parent(x) == f"{PLATFORM}:boot-jar:4.0.0" and t(x, "parent/relativePath") == ""
        assert dep_keys(x)[-2:] == MANDATORY
    lib = xml(ws.root / "shared-lib/pom.xml")
    assert _parent(lib) == f"{PLATFORM}:comp-jar:4.0.0"
    assert not any(k in MANDATORY for k in dep_keys(lib))
    assert PomEditor(ws.root / "pom.xml").get_parent() is None                  # aggregator untouched (policy)
    assert sorted(res.parents_set) == sorted([
        f"shared-lib/pom.xml: {PLATFORM}:comp-jar:1.0.0 → {PLATFORM}:comp-jar:4.0.0",
        f"order-service/pom.xml: {STARTER}:3.2.0 → {PLATFORM}:boot-jar:4.0.0",
        f"user-service/pom.xml: {STARTER}:3.2.0 → {PLATFORM}:boot-jar:4.0.0"])


def test_msa_top_aggregator_role_parents(tmp_path: Path, rcfg: Config) -> None:
    rcfg.layout.msa_parent_policy = "top-aggregator"
    files = _msa_role_files()
    files["user-service/pom.xml"] = files["user-service/pom.xml"].replace(
        "<groupId>org.springframework.boot</groupId>\n        <artifactId>spring-boot-starter-parent</artifactId>\n"
        "        <version>3.2.0</version>",
        f"<groupId>{PLATFORM}</groupId>\n        <artifactId>boot-jar</artifactId>\n        <version>1.0.0</version>")
    ws = make_ws(tmp_path, files, "msa-top")
    res = Restructurer().apply(ws, _msa_role_profile(ws), rcfg, mode="msa_pom")
    assert res.ok, res.error
    assert _parent(xml(ws.root / "pom.xml")) == f"{PLATFORM}:base-parent:4.0.0"
    assert _parent(xml(ws.root / "user-service/pom.xml")) == f"{PLATFORM}:boot-jar:4.0.0"     # old role parent
    assert _parent(xml(ws.root / "shared-lib/pom.xml")) == f"{PLATFORM}:comp-jar:4.0.0"
    assert t(xml(ws.root / "order-service/pom.xml"), "parent/version") == "3.2.0"           # foreign parent left
    assert f"pom.xml: - → {PLATFORM}:base-parent:4.0.0" in res.parents_set


# ---------------------------------------------------------------------------------------------- (e) no role parents
COMP_POM_PRE_9B = """<?xml version="1.0" encoding="UTF-8"?>
<!-- Generated by sbmig (restructure): library module holding the as-is sources and dependencies. -->
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd">
    <modelVersion>4.0.0</modelVersion>

    <parent>
        <groupId>com.acme</groupId>
        <artifactId>shop</artifactId>
        <version>1.0.0-SNAPSHOT</version>
        <relativePath>../../pom.xml</relativePath>
    </parent>

    <artifactId>shop-comp</artifactId>
    <packaging>jar</packaging>
</project>
"""


def test_without_role_parents_modules_inherit_the_root(tmp_path: Path, cfg: Config) -> None:
    assert not cfg.artifacts.role_parents
    from sbmig.core.models import GAV
    from sbmig.restructure.restructurer import render
    assert render("comp-pom.xml.j2", parent=GAV(group_id="com.acme", artifact_id="shop", version="1.0.0-SNAPSHOT"),
                  relative_path="../../pom.xml", group_id=None, version=None, artifact_id="shop-comp",
                  name=None) == COMP_POM_PRE_9B
    ws = make_ws(tmp_path, _legacy_files(), "plain")
    res = Restructurer().apply(ws, _legacy_profile(ws), cfg, mode="layout")
    assert res.ok, res.error
    b = xml(ws.root / BOOT / "pom.xml")
    assert _parent(b) == f"{PLATFORM}:boot-jar:1.0.0"                           # today's rule: left unchanged
    assert any("does not inherit the root pom" in n and "parent left unchanged" in n for n in res.notes)
    c = xml(ws.root / COMP / "pom.xml")
    assert t(c, "parent/artifactId") == "shop" and t(c, "parent/relativePath") == "../../pom.xml"
    assert t(c, "groupId") is None and t(c, "version") is None
    assert dep_keys(xml(ws.root / "pom.xml")) == MANDATORY
    assert res.parents_set == [f"pom.xml: {PLATFORM}:base-parent:1.0.0 → {STARTER}:4.1.0"]
    assert not any(n.startswith("role parents:") for n in res.notes)
