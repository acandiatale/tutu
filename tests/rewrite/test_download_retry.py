"""Catalog X8: an OpenRewrite run that fails on a download (proxy 429 / 5xx, reset, timeout) is retried after a wait."""
from __future__ import annotations

import json

import pytest

import sbmig.rewrite.runner as runner_mod
from sbmig.build.mvn import ProcessResult
from sbmig.core.models import RecipeRef
from sbmig.core.workspace import Workspace
from sbmig.rewrite import OpenRewriteRunner

UP21 = "org.openrewrite.java.migrate.UpgradeToJava21"
OK_LOG = "[WARNING] Changes have been made to src/main/java/A.java by:\n[WARNING]     " + UP21 + "\n[INFO] BUILD SUCCESS\n"
RATE_LOG = ("[ERROR] Failed to execute goal org.openrewrite.maven:rewrite-maven-plugin:6.46.1:run (default-cli) on "
            "project demo: Could not transfer artifact org.openrewrite.recipe:rewrite-spring:pom:6.37.1 from/to "
            "central (https://repo.maven.apache.org/maven2): status code: 429, reason phrase: Too Many Requests\n"
            "[INFO] BUILD FAILURE\n")
GW_LOG = "[ERROR] Failed to download https://repo/x/maven-metadata.xml: 503 Service Unavailable\n[INFO] BUILD FAILURE\n"
MISSING_LOG = ("[ERROR] Failed to execute goal on project demo: Could not resolve dependencies: Could not find "
               "artifact x:y:pom:1.0 in central (https://repo.maven.apache.org/maven2)\n[INFO] BUILD FAILURE\n")
OOM_LOG = "[ERROR] java.lang.OutOfMemoryError: Java heap space\n[INFO] BUILD FAILURE\n"
OTHER_LOG = "[ERROR] Failed to execute goal x:y:1:run: something else\n[INFO] BUILD FAILURE\n"
COMPILE_LOG = ("[ERROR] Failed to execute goal org.apache.maven.plugins:maven-compiler-plugin:3.14.0:compile "
               "(default-compile) on project demo: Compilation failure\n[INFO] BUILD FAILURE\n")


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


def _runner(ws, cfg, waits):
    r = OpenRewriteRunner(ws, cfg)
    r._sleep = waits.append
    return r


def _events(ws: Workspace, name: str) -> list[dict]:
    return [e for e in (json.loads(x) for x in ws.journal_path.read_text().splitlines()) if e["event"] == name]


def test_rate_limited_then_success(ws, cfg, script):
    cfg.openrewrite.download_retry_wait_s = 7
    script["runs"] = [(1, RATE_LOG), (0, OK_LOG)]
    waits: list = []
    res = _runner(ws, cfg, waits).run([RecipeRef(id=UP21)], label="spring")
    assert res.ok and res.error is None
    assert waits == [7] and len(script["calls"]) == 2
    assert script["calls"][1]["log"].endswith("rewrite-spring-dlretry.log")
    assert "src/main/java/A.java" in res.changed_files
    (ev,) = _events(ws, "rewrite_download_retry")
    assert ev["attempt"] == 1 and ev["wait_s"] == 7 and ev["reason"].lower() == "could not transfer artifact"
    assert ev["recipes"] == [UP21] and ev["first_log"].endswith("rewrite-spring.log")
    (done,) = _events(ws, "rewrite_download_retry_result")
    assert done["ok"] is True and done["error"] is None


def test_still_failing_is_prefixed(ws, cfg, script):
    script["runs"] = [(1, GW_LOG), (1, GW_LOG)]
    waits: list = []
    res = _runner(ws, cfg, waits).run([RecipeRef(id=UP21)])
    assert not res.ok and res.error.startswith("download-retry: ")
    assert waits == [30] and len(script["calls"]) == 2
    assert _events(ws, "rewrite_download_retry_result")[0]["ok"] is False


def test_retry_count_and_early_stop(ws, cfg, script):
    cfg.openrewrite.download_retry = 3
    script["runs"] = [(1, RATE_LOG), (1, RATE_LOG), (1, OTHER_LOG)]      # third run fails for another reason
    waits: list = []
    res = _runner(ws, cfg, waits).run([RecipeRef(id=UP21)])
    assert len(script["calls"]) == 3 and waits == [30, 30]
    assert [e["attempt"] for e in _events(ws, "rewrite_download_retry")] == [1, 2]
    assert script["calls"][2]["log"].endswith("-run-dlretry2.log")
    assert res.error.startswith("download-retry: ")


def test_retry_off(ws, cfg, script):
    cfg.openrewrite.download_retry = 0
    script["runs"] = [(1, RATE_LOG)]
    waits: list = []
    res = _runner(ws, cfg, waits).run([RecipeRef(id=UP21)])
    assert not res.ok and "download-retry" not in (res.error or "")
    assert len(script["calls"]) == 1 and waits == [] and not _events(ws, "rewrite_download_retry")


@pytest.mark.parametrize("text", [MISSING_LOG, OTHER_LOG])
def test_no_retry_for_missing_artifact_or_other_failures(ws, cfg, script, text):
    script["runs"] = [(1, text)]
    waits: list = []
    res = _runner(ws, cfg, waits).run([RecipeRef(id=UP21)])
    assert not res.ok and "download-retry" not in (res.error or "")
    assert len(script["calls"]) == 1 and waits == []


def test_oom_takes_precedence(ws, cfg, script):
    script["runs"] = [(1, OOM_LOG + RATE_LOG), (0, OK_LOG)]
    waits: list = []
    res = _runner(ws, cfg, waits).run([RecipeRef(id=UP21)])
    assert res.ok and waits == [] and len(script["calls"]) == 2
    assert script["calls"][1]["log"].endswith("-run-oom.log")
    assert not _events(ws, "rewrite_download_retry")


def test_download_retry_result_goes_through_nofork_fallback(ws, cfg, script):
    # forked run: compile fork fails on a 429 → download retry (still forked) fails in compile → runNoFork succeeds
    script["runs"] = [(1, COMPILE_LOG + RATE_LOG), (1, COMPILE_LOG), (0, OK_LOG)]
    waits: list = []
    res = _runner(ws, cfg, waits).run([RecipeRef(id=UP21)], label="spring")
    assert res.ok and not res.forked and waits == [30]
    assert script["calls"][1]["log"].endswith("rewrite-spring-dlretry.log")
    assert any(c.endswith(":runNoFork") for c in script["calls"][2]["cmd"])


def test_nofork_after_download_retry_keeps_prefix(ws, cfg, script):
    script["runs"] = [(1, COMPILE_LOG + RATE_LOG), (1, COMPILE_LOG + RATE_LOG), (1, OTHER_LOG)]
    res = _runner(ws, cfg, []).run([RecipeRef(id=UP21)])
    assert len(script["calls"]) == 3 and not res.ok and res.error.startswith("download-retry: ")
