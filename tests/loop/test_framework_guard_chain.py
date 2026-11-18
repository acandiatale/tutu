"""Review 2026-11-18 L2: the framework guard of a reduced-scope run resolves a framework declaration's version through
the project's own parent chain — a module declaring `${fw.version}` whose root defines it is protected when an attempt
changes the property in the root (or overrides it in the module); a property a framework-managed import uses is too."""
from __future__ import annotations

from sbmig.core.models import Phase
from sbmig.loop.fixloop import FRAMEWORK_GUARD_PREFIX

from .fakes import FakeFixer, answer
from .test_fixloop_framework import FW, fw_cfg, reduce
from .test_v21l_versions import NS, _ws, make_loop, pom_build

MOD_FOO = "app/src/main/java/com/x/Foo.java"


def root_pom(prop: str = "1.2.0", extra_props: str = "", dm: str = "") -> str:
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<project {NS}>\n  <modelVersion>4.0.0</modelVersion>\n'
            "  <groupId>com.x</groupId>\n  <artifactId>demo-root</artifactId>\n  <version>1.0.0</version>\n"
            "  <packaging>pom</packaging>\n"
            f"  <properties>\n    <fw.version>{prop}</fw.version>\n{extra_props}  </properties>\n"
            f"{dm}  <modules>\n    <module>app</module>\n  </modules>\n</project>\n")


def app_pom(version: str = "${fw.version}", props: str = "") -> str:
    g, a = FW.split(":")
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<project {NS}>\n  <modelVersion>4.0.0</modelVersion>\n'
            "  <parent>\n    <groupId>com.x</groupId>\n    <artifactId>demo-root</artifactId>\n"
            "    <version>1.0.0</version>\n  </parent>\n  <artifactId>app</artifactId>\n"
            f"{props}  <dependencies>\n    <dependency>\n      <groupId>{g}</groupId>\n      <artifactId>{a}</artifactId>\n"
            f"      <version>{version}</version>\n    </dependency>\n  </dependencies>\n</project>\n")


def _run(cfg, tmp_path, files, first_edits):
    ws = _ws(tmp_path, {**files, MOD_FOO: "class Foo { broken }\n"})
    fix = {"op": "write", "path": MOD_FOO, "content": "class Foo { ok }\n"}
    fixer = FakeFixer(scripts=[answer(edits=[*first_edits, fix]), answer(edits=[fix])])
    loop = reduce(make_loop(fw_cfg(cfg), ws, fixer, pom_build(broken_file=MOD_FOO)))
    pr = loop.run(Phase.VERIFY, "package", 4)
    return ws, pr


def test_root_property_change_of_a_module_framework_version_is_not_kept(cfg, tmp_path):
    ws, pr = _run(cfg, tmp_path, {"pom.xml": root_pom(), "app/pom.xml": app_pom()},
                  [{"op": "replace", "path": "pom.xml", "old": "<fw.version>1.2.0</fw.version>",
                    "new": "<fw.version>2.0.0</fw.version>"}])
    first = pr.attempts[0]
    assert first.outcome == "apply_failed" and not first.applied
    [why] = [e for e in first.apply_errors if e.startswith(FRAMEWORK_GUARD_PREFIX)]
    assert f"changed the version of the framework artifact {FW} in app/pom.xml from 1.2.0 to 2.0.0" in why
    assert "<fw.version>1.2.0</fw.version>" in ws.read("pom.xml")
    assert pr.ok and pr.attempts[1].applied


def test_module_override_of_the_root_property_is_not_kept(cfg, tmp_path):
    ws, pr = _run(cfg, tmp_path, {"pom.xml": root_pom(), "app/pom.xml": app_pom()},
                  [{"op": "replace", "path": "app/pom.xml", "old": "  <artifactId>app</artifactId>\n",
                    "new": "  <artifactId>app</artifactId>\n  <properties>\n    <fw.version>2.0.0</fw.version>\n"
                           "  </properties>\n"}])
    first = pr.attempts[0]
    assert first.outcome == "apply_failed"
    assert any("from 1.2.0 to 2.0.0" in e for e in first.apply_errors)


def test_nested_root_property_is_resolved(cfg, tmp_path):
    root = root_pom(prop="${base.version}", extra_props="    <base.version>1.2.0</base.version>\n")
    ws, pr = _run(cfg, tmp_path, {"pom.xml": root, "app/pom.xml": app_pom()},
                  [{"op": "replace", "path": "pom.xml", "old": "<base.version>1.2.0</base.version>",
                    "new": "<base.version>3.0.0</base.version>"}])
    assert pr.attempts[0].outcome == "apply_failed"
    assert any("from 1.2.0 to 3.0.0" in e for e in pr.attempts[0].apply_errors)


def test_property_of_a_framework_import_is_protected(cfg, tmp_path):
    g = FW.split(":")[0]
    dm = ("  <dependencyManagement>\n    <dependencies>\n      <dependency>\n"
          f"        <groupId>{g}</groupId>\n        <artifactId>example-fw-bom</artifactId>\n"
          "        <version>${fw.version}</version>\n        <type>pom</type>\n        <scope>import</scope>\n"
          "      </dependency>\n    </dependencies>\n  </dependencyManagement>\n")
    c = fw_cfg(cfg)
    c.framework.artifacts = [f"{g}:example-fw-*"]
    ws = _ws(tmp_path, {"pom.xml": root_pom(dm=dm), "app/pom.xml": app_pom(version=None or "${fw.version}"),
                        MOD_FOO: "class Foo { broken }\n"})
    fix = {"op": "write", "path": MOD_FOO, "content": "class Foo { ok }\n"}
    bump = {"op": "replace", "path": "pom.xml", "old": "<fw.version>1.2.0</fw.version>",
            "new": "<fw.version>2.0.0</fw.version>"}
    fixer = FakeFixer(scripts=[answer(edits=[bump, fix]), answer(edits=[fix])])
    loop = reduce(make_loop(c, ws, fixer, pom_build(broken_file=MOD_FOO)))
    pr = loop.run(Phase.VERIFY, "package", 4)
    assert pr.attempts[0].outcome == "apply_failed"
    assert any(f"{g}:example-fw-bom" in e for e in pr.attempts[0].apply_errors)


def test_an_unrelated_root_property_change_is_kept(cfg, tmp_path):
    root = root_pom(extra_props="    <other.version>1</other.version>\n")
    ws, pr = _run(cfg, tmp_path, {"pom.xml": root, "app/pom.xml": app_pom()},
                  [{"op": "replace", "path": "pom.xml", "old": "<other.version>1</other.version>",
                    "new": "<other.version>2</other.version>"}])
    assert pr.ok and pr.attempts[0].applied
    assert "<other.version>2</other.version>" in ws.read("pom.xml")
