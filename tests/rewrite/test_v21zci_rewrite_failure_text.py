"""v2.1z-ci rules 5 and 6: a failed recipe run names the pom it could not download, and the download retry starts only
for a transfer problem (not for OpenRewrite's warnings, not for a pom that every repository answered 404 for).

The log fixtures are excerpts of real rewrite-maven-plugin 6.46.1 runs (hosts and paths neutral)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import sbmig.rewrite.runner as runner_mod
from sbmig.build.mvn import ProcessResult
from sbmig.core.models import RecipeRef, RewriteResult
from sbmig.core.workspace import Workspace
from sbmig.rewrite import OpenRewriteRunner
from sbmig.rewrite.failure import clean_reason
from sbmig.rewrite.runner import pom_download_problems, pom_failure_text

LOGS = Path(__file__).parent / "logs"
FATAL = (LOGS / "v21zci-unable-to-download-pom.log").read_text(encoding="utf-8")
PASSING = (LOGS / "v21zci-passing-run-download-warnings.log").read_text(encoding="utf-8")
UP21 = "org.openrewrite.java.migrate.UpgradeToJava21"
OTHER_FAILURE = "[ERROR] Failed to execute goal x:y:1:run: something else\n[INFO] BUILD FAILURE\n"


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
        state["calls"].append(list(cmd))
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


# ------------------------------------------------------------------ parsing
def test_both_forms_of_the_plugin_output_are_read_and_merged():
    poms, unreachable = pom_download_problems(FATAL)
    assert poms == [("com.example.rs:rs-lib-cls:1.0", [("http://public.example.invalid:18804/", "HTTP 404")])]
    assert unreachable == [("http://nexus.example.invalid:18803/", "Connection refused")]


def test_a_passing_run_has_no_pom_problem():
    assert pom_download_problems(PASSING) == ([], [])
    assert pom_failure_text(PASSING) == ""


def _many(n: int) -> str:
    """n poms that two repositories with real-length paths answered 404 for, behind two unreachable repositories
    (the reviewer's shape: in the first text order three such poms gave a 727-character text and the 300-character
    cut lost "not reachable" and two poms)."""
    a = "https://deploy:s3cret@nexus.example.invalid/repository/maven-public-group/"
    b = "https://nexus.example.invalid/repository/maven-central-proxy/"
    blocks = [f"[ERROR] org.openrewrite.maven.MavenDownloadingException: Unable to download POM: "
              f"com.example.platform:platform-lib-{i}:2.{i}.0. Tried repositories:\n"
              f"[ERROR] {a}: HTTP 404\n[ERROR] {b}: HTTP 404\n"
              f"[ERROR]   org.openrewrite.maven.internal.MavenPomDownloader.download(MavenPomDownloader.java:750)\n"
              for i in range(n)]
    return ("[WARNING] Failed to access maven repository http://u:p@nexus.example.invalid/repository/releases/ due "
            "to: Connection refused\n"
            "[WARNING] Failed to access maven repository http://old.example.invalid/m2/ due to: Connection refused\n"
            + "".join(blocks))


def test_several_tried_repositories_userinfo_stripped_and_at_most_three_poms():
    out = pom_failure_text(_many(5))
    assert "s3cret" not in out and "deploy" not in out and "u:p@" not in out
    assert out == ("not reachable: nexus.example.invalid/repository/releases/ (refused), "
                   "old.example.invalid/m2/ (refused); "
                   "Unable to download POM: com.example.platform:platform-lib-0:2.0.0, "
                   "com.example.platform:platform-lib-1:2.1.0, com.example.platform:platform-lib-2:2.2.0 and 2 more; "
                   "tried nexus.example.invalid/repository/maven-public-group/: HTTP 404, "
                   "nexus.example.invalid/repository/maven-central-proxy/: HTTP 404")


def test_the_300_character_line_keeps_the_unreachable_repositories_and_the_three_poms(ws, cfg, script):
    text = _many(3) + "[ERROR] Failed to execute goal org.openrewrite.maven:rewrite-maven-plugin:6.46.1:run " \
        "(default-cli) on project app: Failed to parse or resolve the Maven POM file or one of its dependencies; We " \
        "can not reliably continue without this information.\n[INFO] BUILD FAILURE\n"
    script["runs"] = [(1, text)]
    res = _runner(ws, cfg, []).run([RecipeRef(id=UP21)])
    line = clean_reason(res.error, roots=[str(ws.root), str(ws.run_dir)])
    assert len(res.error) > 300 and len(line) <= 300
    for must in ("nexus.example.invalid/repository/releases/ (refused)", "old.example.invalid/m2/ (refused)", "com.example.platform:platform-lib-0:2.0.0",
                 "com.example.platform:platform-lib-1:2.1.0", "com.example.platform:platform-lib-2:2.2.0"):
        assert must in line, must


def test_the_blocked_mirror_address_is_not_called_unreachable():
    text = ("[WARNING] Failed to access maven repository http://0.0.0.0/ due to: Repository http://0.0.0.0/ has invalid "
            "IP address.\n" + FATAL.replace("[WARNING] Failed to access maven repository http://nexus.example.invalid"
                                            ":18803/ due to: Connection refused\n", ""))
    assert pom_download_problems(text)[1] == []
    assert "not reachable" not in pom_failure_text(text)


# ------------------------------------------------------------------ rule 5 through the runner
def test_the_error_text_names_the_pom_and_the_unreachable_repository(ws, cfg, script):
    script["runs"] = [(1, FATAL)]
    waits: list = []
    res = _runner(ws, cfg, waits).run([RecipeRef(id=UP21)])
    assert not res.ok and len(script["calls"]) == 1 and waits == []          # rule 6: no retry for this
    assert res.error == ("not reachable: nexus.example.invalid:18803/ (refused); Unable to download POM: "
                         "com.example.rs:rs-lib-cls:1.0; tried public.example.invalid:18804/: HTTP 404")
    assert not _events(ws, "rewrite_download_retry")
    # the orchestrator's `transform.rewrite: the automatic conversion failed (…): <reason>` line gets all of it
    line = clean_reason(res.error, roots=[str(ws.root), str(ws.run_dir)])
    assert line == res.error and len(line) <= 300


def test_without_a_pom_failure_the_error_text_is_unchanged(ws, cfg, script):
    script["runs"] = [(1, OTHER_FAILURE)]
    res = _runner(ws, cfg, []).run([RecipeRef(id=UP21)])
    assert "Unable to download POM" not in (res.error or "") and "something else" in res.error


# ------------------------------------------------------------------ rule 6
def test_openrewrite_warnings_on_a_failed_run_do_not_start_the_retry(ws, cfg, script):
    script["runs"] = [(1, PASSING.replace("BUILD SUCCESS", "BUILD FAILURE") + OTHER_FAILURE)]
    waits: list = []
    res = _runner(ws, cfg, waits).run([RecipeRef(id=UP21)])
    assert not res.ok and len(script["calls"]) == 1 and waits == []


def test_only_refused_connections_and_404_do_not_start_the_retry(ws, cfg, script):
    script["runs"] = [(1, FATAL)]                    # 404 from the only tried repository, the other one refused
    res = _runner(ws, cfg, []).run([RecipeRef(id=UP21)])
    assert len(script["calls"]) == 1 and "download-retry" not in res.error


@pytest.mark.parametrize("reason", ["Connection reset", "Connect timed out", "Read timed out",
                                    "nexus.example.invalid", "java.net.UnknownHostException: nexus.example.invalid"])
def test_an_unreachable_repository_that_timed_out_reset_or_did_not_resolve_starts_the_retry(ws, cfg, script, reason):
    # the pom may be on the repository OpenRewrite could not reach: waiting can help
    script["runs"] = [(1, FATAL.replace("due to: Connection refused", f"due to: {reason}")), (1, FATAL)]
    waits: list = []
    res = _runner(ws, cfg, waits).run([RecipeRef(id=UP21)])
    assert len(script["calls"]) == 2 and waits == [30]
    assert _events(ws, "rewrite_download_retry")[0]["reason"].startswith("Unable to download POM (not reachable: ")
    assert res.error.startswith("download-retry: ")


@pytest.mark.parametrize("answer", ["HTTP 503", "HTTP 429", "HTTP 502", "Connect timed out",
                                    "repo.maven.apache.org", "HTTP 200"])
def test_a_tried_repository_with_a_transfer_problem_starts_the_retry(ws, cfg, script, answer):
    text = FATAL.replace("http://public.example.invalid:18804/: HTTP 404", f"http://public.example.invalid:18804/: {answer}")
    cfg.openrewrite.download_retry_wait_s = 3
    script["runs"] = [(1, text), (1, FATAL)]
    waits: list = []
    res = _runner(ws, cfg, waits).run([RecipeRef(id=UP21)])
    assert len(script["calls"]) == 2 and waits == [3]
    (ev,) = _events(ws, "rewrite_download_retry")
    assert ev["reason"].startswith("Unable to download POM (")
    # the second run answered 404 only: no further waiting, the final error is prefixed and still names the pom
    assert res.error.startswith("download-retry: not reachable: ") and "rs-lib-cls:1.0" in res.error


def test_could_not_transfer_artifact_still_starts_the_retry(ws, cfg, script):
    text = ("[ERROR] Failed to execute goal on project app: Could not transfer artifact com.example:x:pom:1 from/to "
            "nexus (https://nexus.example.invalid/): Connection reset\n[INFO] BUILD FAILURE\n")
    script["runs"] = [(1, text), (0, PASSING)]
    waits: list = []
    res = _runner(ws, cfg, waits).run([RecipeRef(id=UP21)])
    assert res.ok and waits == [30] and len(script["calls"]) == 2


def test_the_appended_pom_part_never_starts_the_retry_by_its_own_words(ws, cfg, tmp_path):
    log = tmp_path / "x.log"
    log.write_text(OTHER_FAILURE, encoding="utf-8")
    err = ("not reachable: nexus.example.invalid/ (timed out); Unable to download POM: g:a:1; tried "
           "repo.example.invalid/: HTTP 503 Service Unavailable | recipe validation: x")
    res = RewriteResult(ok=False, recipes=[UP21], log_path=str(log), error=err)
    assert OpenRewriteRunner(ws, cfg)._download_failure(res) is None


def test_a_maven_warning_never_starts_the_retry(ws, cfg, script):
    # a metadata transfer warning on a run that failed for another reason: waiting does not help that reason
    warn = ("[WARNING] Could not transfer metadata com.example:x/maven-metadata.xml from/to nexus "
            "(https://nexus.example.invalid/): Transfer failed for https://nexus.example.invalid/x: 502 Bad Gateway\n")
    script["runs"] = [(1, warn + OTHER_FAILURE)]
    waits: list = []
    _runner(ws, cfg, waits).run([RecipeRef(id=UP21)])
    assert len(script["calls"]) == 1 and waits == []
    # the same words on an [ERROR] line still do
    script["runs"] = [(1, warn.replace("[WARNING]", "[ERROR]") + OTHER_FAILURE), (0, PASSING)]
    script["calls"].clear()
    assert _runner(ws, cfg, waits).run([RecipeRef(id=UP21)]).ok and len(script["calls"]) == 2
