"""Catalog E8: an OpenRewrite run that dies with OutOfMemoryError is retried once with oom_retry_jvm_args."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import sbmig.rewrite.runner as runner_mod
from sbmig.build.mvn import ProcessResult
from sbmig.core.models import RecipeRef
from sbmig.core.workspace import Workspace
from sbmig.rewrite import OpenRewriteRunner

UP21 = "org.openrewrite.java.migrate.UpgradeToJava21"
OOM_LOG = ("[INFO] Running recipe(s)...\n[ERROR] Failed to execute goal org.openrewrite.maven:rewrite-maven-plugin:"
           "6.46.1:run (default-cli) on project demo: Execution default-cli of goal ... failed: "
           "An API incompatibility was encountered ... java.lang.OutOfMemoryError: Java heap space\n"
           "[INFO] BUILD FAILURE\n")
GC_LOG = "[ERROR] java.lang.OutOfMemoryError: GC overhead limit exceeded\n[INFO] BUILD FAILURE\n"
OK_LOG = "[WARNING] Changes have been made to src/main/java/A.java by:\n[WARNING]     " + UP21 + "\n[INFO] BUILD SUCCESS\n"
FAIL_LOG = "[ERROR] Failed to execute goal x:y:1:run: something else\n[INFO] BUILD FAILURE\n"


@pytest.fixture()
def ws(tmp_path) -> Workspace:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "pom.xml").write_text("<project/>\n")
    w = Workspace(root, tmp_path / "run", "t-run")
    w.git_init()
    return w


@pytest.fixture()
def script(monkeypatch):
    """Scripted run_logged: each call pops (exit_code, text) and records cmd + MAVEN_OPTS + log label."""
    state: dict = {"runs": [], "calls": []}

    def fake_run(cmd, *, cwd, env, log_path, timeout_s):
        code, text = state["runs"].pop(0)
        log_path.write_text(text, encoding="utf-8")
        state["calls"].append({"cmd": list(cmd), "opts": env.get("MAVEN_OPTS", ""), "log": log_path.name})
        return ProcessResult(exit_code=code, text=text, duration_s=0.1)
    monkeypatch.setattr(runner_mod, "run_logged", fake_run)
    monkeypatch.setattr(runner_mod, "resolve_recipe_artifact_versions",
                        lambda cfg, env, artifacts, cache_path, work_dir: {a: "1.0" for a in artifacts})
    return state


def _journal(ws: Workspace) -> list[dict]:
    return [json.loads(x) for x in ws.journal_path.read_text().splitlines()]


def test_oom_then_success(ws, cfg, script):
    cfg.openrewrite.jvm_args = ["-Xmx4g"]
    cfg.openrewrite.oom_retry_jvm_args = ["-Xmx8g", "-XX:+UseG1GC"]
    script["runs"] = [(1, OOM_LOG), (0, OK_LOG)]
    res = OpenRewriteRunner(ws, cfg).run([RecipeRef(id=UP21)], label="jdk21")
    assert res.ok and res.error is None
    assert len(script["calls"]) == 2
    assert "-Xmx4g" in script["calls"][0]["opts"] and "-Xmx8g" not in script["calls"][0]["opts"]
    assert "-Xmx8g -XX:+UseG1GC" in script["calls"][1]["opts"] and "-Xmx4g" not in script["calls"][1]["opts"]
    assert script["calls"][1]["log"].endswith("rewrite-jdk21-oom.log")
    assert "src/main/java/A.java" in res.changed_files
    evs = [e["event"] for e in _journal(ws)]
    assert "rewrite_oom_retry" in evs and "rewrite_oom_retry_result" in evs


def test_oom_twice_reports_oom_retry(ws, cfg, script):
    script["runs"] = [(1, GC_LOG), (1, GC_LOG)]
    res = OpenRewriteRunner(ws, cfg).run([RecipeRef(id=UP21)])
    assert not res.ok and res.error.startswith("oom-retry (still out of memory)")
    assert len(script["calls"]) == 2 and script["calls"][1]["log"].endswith("-run-oom.log")


def test_no_retry_without_oom(ws, cfg, script):
    script["runs"] = [(1, FAIL_LOG)]
    res = OpenRewriteRunner(ws, cfg).run([RecipeRef(id=UP21)])
    assert not res.ok and "oom-retry" not in (res.error or "")
    assert len(script["calls"]) == 1
    assert not any(e["event"] == "rewrite_oom_retry" for e in _journal(ws))


def test_oom_retry_then_nofork_keeps_big_heap(ws, cfg, script):
    compile_fail = ("[ERROR] Failed to execute goal org.apache.maven.plugins:maven-compiler-plugin:3.14.0:compile "
                    "(default-compile) on project demo: Compilation failure\n[INFO] BUILD FAILURE\n")
    script["runs"] = [(1, OOM_LOG), (1, compile_fail), (0, OK_LOG)]
    res = OpenRewriteRunner(ws, cfg).run([RecipeRef(id=UP21)], label="spring")
    assert len(script["calls"]) == 3
    assert res.ok and not res.forked
    assert all("-Xmx8g" in c["opts"] for c in script["calls"][1:])
    assert any(c.endswith(":runNoFork") for c in script["calls"][2]["cmd"])
