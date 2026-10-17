"""Per-recipe attribution + fork/noFork goal selection (PM integration additions)."""
from pathlib import Path

from sbmig.rewrite.runner import OpenRewriteRunner

LOG = """[INFO] Running recipe(s)...
[WARNING] Changes have been made to pom.xml by:
[WARNING]     com.company.migrate.Run
[WARNING]         org.openrewrite.java.migrate.UpgradeToJava21
[WARNING]             org.openrewrite.maven.UpgradePluginVersion: {groupId=org.apache.maven.plugins, artifactId=maven-compiler-plugin, newVersion=3.x}
[WARNING] Changes have been made to src/main/java/com/example/orders/json/MoneyJsonComponent.java by:
[WARNING]     com.company.migrate.Run
[WARNING]         com.company.kb.JsonComponentToJacksonComponent
[WARNING]             org.openrewrite.java.ChangeType: {oldFullyQualifiedTypeName=org.springframework.boot.jackson.JsonComponent, newFullyQualifiedTypeName=org.springframework.boot.jackson.JacksonComponent}
[WARNING] Please review and commit the results.
[INFO] BUILD SUCCESS
"""


def test_parse_recipe_attribution():
    att = OpenRewriteRunner.parse_recipe_attribution(LOG)
    assert att["com.company.kb.JsonComponentToJacksonComponent"] == ["src/main/java/com/example/orders/json/MoneyJsonComponent.java"]
    assert att["org.openrewrite.java.migrate.UpgradeToJava21"] == ["pom.xml"]
    assert sorted(att["com.company.migrate.Run"]) == ["pom.xml", "src/main/java/com/example/orders/json/MoneyJsonComponent.java"]
    assert "Please" not in att and "{oldFullyQualifiedTypeName=org.springframework.boot.jackson.JsonComponent," not in att


def test_command_goal_selection(cfg, tmp_path: Path, make_ws):
    src = tmp_path / "p"; src.mkdir(); (src / "pom.xml").write_text("<project/>")
    ws = make_ws(src)
    r = OpenRewriteRunner(ws, cfg)
    cp = tmp_path / "rewrite.yml"; cp.write_text("")
    def goal(**kw):
        cmd = r.command(cp, ["g:a:1"], **kw)
        return [c for c in cmd if "rewrite-maven-plugin" in c][0].rsplit(":", 1)[-1]
    assert goal() == cfg.openrewrite.plugin_goal
    assert goal(fork=False) == "runNoFork"
    assert goal(fork=True) == "run"
    assert goal(dry_run=True) == "dryRun"
    assert goal(dry_run=True, fork=False) == "dryRunNoFork"
