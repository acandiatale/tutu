from __future__ import annotations

import io
import json
import os
import stat
from pathlib import Path

import pytest

import sbmig.build.mvn as mvn_mod
from sbmig.build import maven_available
from sbmig.core.models import RecipeRef
from sbmig.core.workspace import Workspace
from sbmig.rewrite import COMPOSITE_RECIPE_ID, OpenRewriteRunner, RecipeCompositionError, recipe_artifact_for

LOGS = Path(__file__).parent / "logs"
JDK21 = Path(os.environ.get("SBMIG_TEST_JAVA21_HOME",
                            "/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-21.0.1/Contents/Home"))
MJ = "org.openrewrite.recipe:rewrite-migrate-java"
SP = "org.openrewrite.recipe:rewrite-spring"
UP21 = "org.openrewrite.java.migrate.UpgradeToJava21"


class FakePopen:
    calls: list[dict] = []
    output: bytes = b"[INFO] BUILD SUCCESS\n"
    code = 0
    on_run = None

    def __init__(self, cmd, cwd=None, env=None, **kw):
        FakePopen.calls.append({"cmd": cmd, "cwd": cwd, "env": env})
        if FakePopen.on_run:
            FakePopen.on_run()
        self.stdout = io.BytesIO(FakePopen.output)
        self.returncode = None
        self.pid = -424242

    def wait(self, timeout=None):
        self.returncode = FakePopen.code
        return self.returncode


@pytest.fixture()
def fake(monkeypatch):
    FakePopen.calls, FakePopen.output, FakePopen.code, FakePopen.on_run = [], b"[INFO] BUILD SUCCESS\n", 0, None
    monkeypatch.setattr(mvn_mod, "_popen", FakePopen)
    return FakePopen


@pytest.fixture()
def rcfg(cfg):
    """No BOM lookups in unit tests: every artifact has an explicit version."""
    cfg.openrewrite.recipe_artifact_versions = {
        MJ: "3.42.0", SP: "6.37.0", "org.openrewrite.recipe:rewrite-testing-frameworks": "3.44.0",
        "org.openrewrite.recipe:rewrite-java-dependencies": "1.60.2",
        "org.openrewrite.recipe:rewrite-static-analysis": "2.41.0"}
    return cfg


@pytest.fixture()
def ws(tmp_path) -> Workspace:
    root = tmp_path / "proj"
    (root / "svc").mkdir(parents=True)
    (root / "pom.xml").write_text("<project/>\n")
    (root / "svc" / "pom.xml").write_text("<project/>\n")
    w = Workspace(root, tmp_path / "run", "t-run")
    w.git_init()
    return w


def events(ws):
    return [json.loads(x) for x in ws.journal_path.read_text().splitlines() if '"rewrite"' in x]


def log_text(name: str, ws: Workspace) -> bytes:
    return (LOGS / name).read_text().replace("/work/proj", str(ws.root)).encode()


# ------------------------------------------------------------------------------------------------ artifacts
def test_recipe_artifact_inference(ws, rcfg):
    assert recipe_artifact_for(UP21) == MJ
    assert recipe_artifact_for("org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0") == SP
    assert recipe_artifact_for("org.openrewrite.java.testing.junit5.JUnit4to5Migration") == \
        "org.openrewrite.recipe:rewrite-testing-frameworks"
    assert recipe_artifact_for("org.openrewrite.java.ChangeType") is None
    r = OpenRewriteRunner(ws, rcfg)
    _, text = r.compose([RecipeRef(id="com.company.migrate.SpringBoot41Version", source="project")])
    assert r.required_artifacts([RecipeRef(id="com.company.migrate.SpringBoot41Version", source="project")], text) \
        == [MJ, SP, "org.openrewrite.recipe:rewrite-java-dependencies"]
    assert r.required_artifacts([RecipeRef(id="org.openrewrite.maven.UpgradeParentVersion"),
                                 RecipeRef(id="x.y.Custom", artifact="com.acme:custom-recipes")], text) == \
        ["com.acme:custom-recipes"]


# ------------------------------------------------------------------------------------------------ command
def test_command_env_and_config_file(ws, rcfg, fake):
    rcfg.build.java_home = "/opt/jdk21"
    rcfg.build.local_repo = "/tmp/m2repo"
    rcfg.openrewrite.jvm_args = ["-Xmx4g", "-XX:+UseG1GC"]
    os.environ["MAVEN_OPTS"] = "-Dfoo=1"
    try:
        res = OpenRewriteRunner(ws, rcfg).run([RecipeRef(id=UP21)], label="jdk21")
    finally:
        del os.environ["MAVEN_OPTS"]
    call = fake.calls[0]
    cfg_path = ws.run_dir / "rewrite" / "rewrite-1.yml"
    assert call["cmd"] == [mvn_mod.mvn_executable(rcfg), "-B", "-ntp", "-Dstyle.color=never", "-Dmaven.repo.local=/tmp/m2repo",
                           "org.openrewrite.maven:rewrite-maven-plugin:6.46.1:run",
                           f"-Drewrite.activeRecipes={COMPOSITE_RECIPE_ID}",
                           f"-Drewrite.configLocation={cfg_path.resolve()}",
                           f"-Drewrite.recipeArtifactCoordinates={MJ}:3.42.0",
                           "-Drewrite.exportDatatables=false", "-Drewrite.failOnInvalidActiveRecipes=true",
        "-Drewrite.exclusions=**/*.jsp,**/*.jspx,**/*.html,**/*.htm"]
    assert call["cwd"] == str(ws.root)
    # inherited MAVEN_OPTS first, the rewrite JVM args last (round 10: mvn_env may add -Dstdout/-Dstderr.encoding)
    opts = call["env"]["MAVEN_OPTS"]
    assert opts.startswith("-Dfoo=1 ") and opts.endswith(" -Xmx4g -XX:+UseG1GC")
    assert call["env"]["JAVA_HOME"] == "/opt/jdk21"
    assert res.ok and res.command == call["cmd"] and Path(res.log_path).name == "001-rewrite-jdk21.log"
    assert "- org.openrewrite.java.migrate.UpgradeToJava21" in cfg_path.read_text()
    ev = events(ws)[-1]
    assert ev["recipes"] == [UP21] and ev["ok"] is True and ev["changed_files_count"] == 0
    assert ev["config"] == str(cfg_path) and ev["artifacts"] == [f"{MJ}:3.42.0"]


def test_dry_run_module_and_numbering(ws, rcfg, fake):
    fake.output = log_text("real-dryrun-upgrade-java21.log", ws)
    r = OpenRewriteRunner(ws, rcfg)
    r.run([RecipeRef(id=UP21)])
    res = r.run([RecipeRef(id=UP21)], dry_run=True, module="svc")
    call = fake.calls[-1]
    assert call["cmd"][4] == "org.openrewrite.maven:rewrite-maven-plugin:6.46.1:dryRun"
    assert call["cwd"] == str(ws.root / "svc")
    assert res.dry_run and res.ok
    assert res.changed_files == ["svc/pom.xml", "svc/src/main/java/com/example/App.java"]
    assert (ws.run_dir / "rewrite" / "rewrite-2.yml").exists()


def test_changed_files_union_of_log_and_git(ws, rcfg, fake):
    fake.output = log_text("real-run-upgrade-java21.log", ws)
    fake.on_run = lambda: (ws.root / "extra.txt").write_text("generated\n")
    res = OpenRewriteRunner(ws, rcfg).run([RecipeRef(id=UP21)])
    assert res.ok
    assert res.changed_files == ["extra.txt", "pom.xml", "src/main/java/com/example/App.java"]
    assert events(ws)[-1]["changed_files_count"] == 3


def test_parse_changed_files_variants():
    text = ("[WARNING] Changes have been made to a/pom.xml by:\n[WARNING]     some.Recipe\n"
            "[WARNING] Generated new file src/main/java/New.java by:\n"
            "[WARNING] Deleted file src/main/java/Old.java by:\n"
            "[WARNING] File has been moved from src/A.java to src/B.java by:\n")
    assert OpenRewriteRunner.parse_changed_files(text) == ["a/pom.xml", "src/main/java/New.java",
                                                           "src/main/java/Old.java", "src/A.java", "src/B.java"]
    dry = ("[WARNING] These recipes would make changes to pom.xml:\n"
           "[WARNING] These recipes would move file from x/A.java to x/B.java:\n")
    assert OpenRewriteRunner.parse_changed_files(dry, dry_run=True) == ["pom.xml", "x/A.java", "x/B.java"]


# ------------------------------------------------------------------------------------------------ failures
def test_unknown_recipe_strict_failure(ws, rcfg, fake):
    fake.output, fake.code = log_text("real-run-unknown-recipe-strict.log", ws), 1
    res = OpenRewriteRunner(ws, rcfg).run([RecipeRef(id="org.openrewrite.java.migrate.DoesNotExist21")])
    assert not res.ok
    assert res.error == "unknown recipe(s): org.openrewrite.java.migrate.DoesNotExist21"


def test_validation_errors_with_build_success_still_fail(ws, rcfg, fake):
    fake.output = (b"[ERROR] Recipe validation error in com.company.migrate.Run for property newVersion: must not be"
                   b" null\n[INFO] BUILD SUCCESS\n")
    res = OpenRewriteRunner(ws, rcfg).run([RecipeRef(id=UP21)])
    assert not res.ok and res.error == "recipe validation: com.company.migrate.Run newVersion: must not be null"


def test_unresolvable_recipe_artifact_error(ws, rcfg, fake):
    fake.output, fake.code = log_text("real-run-bom-3.38.0-unresolvable.log", ws), 1
    res = OpenRewriteRunner(ws, rcfg).run([RecipeRef(id=UP21)])
    assert not res.ok
    assert res.error == ("dependency_resolution: Could not find artifact org.openrewrite.recipe:rewrite-migrate-java:"
                         "jar:3.43.0 in central (https://repo.maven.apache.org/maven2)")
    assert events(ws)[-1]["error"] == res.error


def test_generic_failure_text(ws, rcfg, fake):
    fake.output, fake.code = b"[INFO] BUILD FAILURE\n", 1
    assert OpenRewriteRunner(ws, rcfg).run([RecipeRef(id=UP21)]).error == "unknown: BUILD FAILURE"
    fake.output = b"nothing\n"
    assert OpenRewriteRunner(ws, rcfg).run([RecipeRef(id=UP21)]).error == "rewrite run failed (see log)"


def test_empty_recipe_list_is_noop(ws, rcfg, fake):
    res = OpenRewriteRunner(ws, rcfg).run([])
    assert res.ok and res.recipes == [] and fake.calls == []


def test_missing_project_recipe_raises(ws, rcfg, fake):
    with pytest.raises(RecipeCompositionError):
        OpenRewriteRunner(ws, rcfg).run([RecipeRef(id="com.company.migrate.Nope", source="project")])
    assert fake.calls == []


def test_artifact_resolution_failure_returns_not_ok(ws, cfg, fake, tmp_path):
    cfg.build.local_repo = str(tmp_path / "empty-repo")
    cfg.build.mvn = str(tmp_path / "no-mvn")
    res = OpenRewriteRunner(ws, cfg).run([RecipeRef(id=UP21)])
    assert not res.ok and "rewrite-recipe-bom" in res.error and fake.calls == []
    assert Path(res.log_path).read_text().startswith("[ERROR]")
    assert events(ws)[-1]["ok"] is False


def test_kb_active_recipes_are_composed(ws, rcfg, fake, tmp_path):
    kbfile = tmp_path / "kb-active.yml"
    kbfile.write_text("type: specs.openrewrite.org/v1beta/recipe\nname: com.company.kb.Fix1\nrecipeList:\n"
                      "  - org.openrewrite.java.ChangePackage:\n      oldPackageName: javax.foo\n"
                      "      newPackageName: jakarta.foo\n")

    class KB:
        def active_recipe_files(self):
            return [kbfile]

    rcfg.kb.use_active_recipes = True      # v1 KB recipes (off by default in v2, the composition still works)
    r = OpenRewriteRunner(ws, rcfg, kb=KB())
    res = r.run([RecipeRef(id="com.company.kb.Fix1", source="kb")])
    assert res.ok
    assert f"-Drewrite.recipeArtifactCoordinates={MJ}:3.42.0,{SP}:6.37.0" in fake.calls[0]["cmd"]
    rcfg.kb.use_active_recipes = False
    with pytest.raises(RecipeCompositionError):
        r.run([RecipeRef(id="com.company.kb.Fix1", source="kb")])


def test_timeout(ws, rcfg, tmp_path):
    p = tmp_path / "slow"
    p.write_text("#!/bin/sh\necho '[INFO] Running recipe(s)...'\nsleep 30\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    rcfg.build.mvn = str(p)
    res = OpenRewriteRunner(ws, rcfg, timeout_s=1).run([RecipeRef(id=UP21)])
    assert not res.ok and res.error.startswith("rewrite timed out")


def test_missing_executable(ws, rcfg):
    rcfg.build.mvn = "/nonexistent/mvn"
    res = OpenRewriteRunner(ws, rcfg).run([RecipeRef(id=UP21)])
    assert not res.ok and "cannot start" in res.error


def test_workspace_without_git(tmp_path, rcfg, fake):
    root = tmp_path / "nogit"
    root.mkdir()
    w = Workspace(root, tmp_path / "run2", "nogit")
    assert OpenRewriteRunner(w, rcfg).run([RecipeRef(id=UP21)]).ok


# ------------------------------------------------------------------------------------------------ integration
JAVA8_POM = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>
<groupId>com.example</groupId><artifactId>java8-app</artifactId><version>1.0.0</version>
<properties><maven.compiler.source>1.8</maven.compiler.source><maven.compiler.target>1.8</maven.compiler.target>
<project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
</project>
"""
APP = """package com.example;

import java.util.ArrayList;
import java.util.List;

public class App {
    public static void main(String[] args) {
        List<String> l = new ArrayList<String>();
        l.add("a");
        System.out.println(l.get(0));
    }
}
"""


@pytest.fixture()
def java8_ws(tmp_path, cfg):
    if not JDK21.exists():
        pytest.skip("JDK 21 not available")
    ok, msg = maven_available(cfg)
    if not ok:
        pytest.skip(f"mvn not available: {msg}")
    cfg.build.java_home = str(JDK21)
    src = tmp_path / "src-java8"
    (src / "src/main/java/com/example").mkdir(parents=True)
    (src / "pom.xml").write_text(JAVA8_POM)
    (src / "src/main/java/com/example/App.java").write_text(APP)
    w = Workspace.from_input(src, tmp_path / "out", run_id="it-rewrite")
    w.git_init()
    return w


@pytest.mark.integration
def test_real_upgrade_to_java21(java8_ws, cfg):
    # rewrite-recipe-bom 3.38.0 (the configured default) manages rewrite-migrate-java 3.43.0 / rewrite-spring 6.38.0,
    # which are NOT published on Maven Central (checked 2026-10-18) → this run uses 3.37.0 unless overridden.
    cfg.openrewrite.recipe_bom_version = os.environ.get("SBMIG_TEST_RECIPE_BOM", "3.37.0")
    res = OpenRewriteRunner(java8_ws, cfg).run([RecipeRef(id=UP21, artifact=MJ)], label="jdk21")
    assert res.ok, res.error
    assert "pom.xml" in res.changed_files
    assert "src/main/java/com/example/App.java" in res.changed_files
    assert "<maven.compiler.source>21</maven.compiler.source>" in java8_ws.read("pom.xml")
    assert "l.getFirst()" in java8_ws.read("src/main/java/com/example/App.java")


@pytest.mark.integration
def test_real_bom_3_38_0_run_reports_resolution_problem(java8_ws, cfg):
    cfg.openrewrite.recipe_bom_version = "3.38.0"
    res = OpenRewriteRunner(java8_ws, cfg).run([RecipeRef(id=UP21)])
    if not res.ok:                     # upstream gap; passes either way once Central has the artifacts
        assert "rewrite-migrate-java:jar:3.43.0" in res.error
    else:
        assert "pom.xml" in res.changed_files


@pytest.mark.integration
def test_real_unknown_recipe_fails(java8_ws, cfg):
    cfg.openrewrite.recipe_bom_version = os.environ.get("SBMIG_TEST_RECIPE_BOM", "3.37.0")
    res = OpenRewriteRunner(java8_ws, cfg).run([RecipeRef(id="org.openrewrite.java.migrate.NoSuchRecipe")])
    assert not res.ok
    assert res.error == "unknown recipe(s): org.openrewrite.java.migrate.NoSuchRecipe"
    assert res.changed_files == []


def test_command_passes_rewrite_exclusions(ws, rcfg, fake):
    """Round 9c: the plugin must not parse JSP / HTML (OpenRewrite's XML parser accepts them and only logs ANTLR
    `token recognition error` noise); the globs come from openrewrite.exclusions and an empty list drops the flag."""
    from sbmig.rewrite.runner import OpenRewriteRunner
    r = OpenRewriteRunner(ws, rcfg)
    cmd = r.command(ws.run_dir / "rewrite.yml", [])
    flag = next(a for a in cmd if a.startswith("-Drewrite.exclusions="))
    assert flag == "-Drewrite.exclusions=**/*.jsp,**/*.jspx,**/*.html,**/*.htm"
    rcfg.openrewrite.exclusions = []
    assert not any(a.startswith("-Drewrite.exclusions=") for a in r.command(ws.run_dir / "rewrite.yml", []))
