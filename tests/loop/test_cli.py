from __future__ import annotations

import pytest
from typer.testing import CliRunner

from sbmig.cli import app
from sbmig.core.models import CaseType, RunReport, utcnow

runner = CliRunner()


def test_help_and_version():
    res = runner.invoke(app, ["--help"])
    assert res.exit_code == 0
    for cmd in ("run", "analyze", "plan", "build", "parse-log", "rewrite", "kb", "version"):
        assert cmd in res.output
    res = runner.invoke(app, ["version"])
    assert res.exit_code == 0 and "sbmig" in res.output


def test_parse_log(tmp_path):
    pytest.importorskip("sbmig.build")
    from sbmig import build as b
    if not hasattr(b, "parse_maven_log"):
        pytest.skip("parse_maven_log not available yet")
    log = tmp_path / "build.log"
    log.write_text(
        "[INFO] BUILD FAILURE\n"
        "[ERROR] COMPILATION ERROR : \n"
        f"[ERROR] {tmp_path}/src/main/java/com/x/Foo.java:[3,8] cannot find symbol\n"
        "  symbol:   class Bar\n  location: class com.x.Foo\n"
        "[ERROR] Failed to execute goal org.apache.maven.plugins:maven-compiler-plugin:3.13.0:compile "
        "(default-compile) on project demo: Compilation failure\n")
    res = runner.invoke(app, ["parse-log", str(log), "--root", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert "error(s)" in res.output
    res = runner.invoke(app, ["parse-log", str(log), "--root", str(tmp_path), "--json"])
    assert res.exit_code == 0 and '"kind"' in res.output and "Foo.java" in res.output
    empty = tmp_path / "ok.log"
    empty.write_text("[INFO] BUILD SUCCESS\n")
    assert "no errors" in runner.invoke(app, ["parse-log", str(empty)]).output


class FakeOrch:
    seen: dict = {}

    def __init__(self, cfg, ws, **kw):
        FakeOrch.seen = {"cfg": cfg, "ws": ws, "kw": kw}

    def run(self, stop_after=None, dry_run=False):
        FakeOrch.seen.update(stop_after=stop_after, dry_run=dry_run)
        return RunReport(run_id="r", project_name="p", case_type=CaseType.UNKNOWN, started_at=utcnow(),
                         success=dry_run)


def test_run_dry_run_with_patched_orchestrator(tmp_path, monkeypatch, repo_root):
    import sbmig.loop
    monkeypatch.setattr(sbmig.loop, "Orchestrator", FakeOrch)
    src = tmp_path / "proj"
    src.mkdir()
    (src / "pom.xml").write_text("<project/>")
    cfg_file = repo_root / "config" / "artifacts.local-e2e.yml"
    res = runner.invoke(app, ["run", str(src), "--config", str(cfg_file), "--out", str(tmp_path / "out"),
                              "--run-id", "cli-1", "--dry-run", "--no-kb", "--llm-provider", "fake",
                              "--java-home", "/jdk21", "--settings", "/s.xml", "--stop-after", "spring", "-v"])
    assert res.exit_code == 0, res.output
    s = FakeOrch.seen
    assert s["dry_run"] is True and s["stop_after"].value == "spring"
    cfg = s["cfg"]
    assert cfg.llm.provider == "fake" and cfg.build.java_home == "/jdk21" and cfg.build.settings_file == "/s.xml"
    assert cfg.kb.harvest is False and s["kw"]["kb"] is not None
    assert (tmp_path / "out" / "cli-1" / "project" / "pom.xml").exists()
    # failure exit code + bad phase
    res = runner.invoke(app, ["run", str(src), "--config", str(cfg_file), "--out", str(tmp_path / "out"),
                              "--run-id", "cli-2"])
    assert res.exit_code == 2
    res = runner.invoke(app, ["run", str(src), "--config", str(cfg_file), "--out", str(tmp_path / "out"),
                              "--run-id", "cli-3", "--stop-after", "bogus"])
    assert res.exit_code == 1


def test_analyze_and_plan_commands(tmp_path, repo_root):
    pytest.importorskip("sbmig.analyze")
    src = tmp_path / "proj"
    (src / "src/main/java/com/x").mkdir(parents=True)
    (src / "pom.xml").write_text(
        '<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>'
        "<groupId>com.x</groupId><artifactId>demo</artifactId><version>1</version></project>")
    (src / "src/main/java/com/x/App.java").write_text("package com.x; public class App {}\n")
    cfg_file = str(repo_root / "config" / "artifacts.local-e2e.yml")
    res = runner.invoke(app, ["analyze", str(src), "--config", cfg_file])
    assert res.exit_code == 0, res.output
    assert "demo" in res.output
    res = runner.invoke(app, ["analyze", str(src), "--config", cfg_file, "--json"])
    assert res.exit_code == 0 and '"case_type"' in res.output
    try:
        from sbmig.plan import build_plan  # noqa: F401
    except ImportError:  # pragma: no cover
        pytest.skip("planner not available")
    res = runner.invoke(app, ["plan", str(src), "--config", cfg_file])
    assert res.exit_code == 0, res.output
    assert "verify.build" in res.output
    res = runner.invoke(app, ["plan", str(src), "--config", cfg_file, "--json"])
    assert res.exit_code == 0 and '"steps"' in res.output
