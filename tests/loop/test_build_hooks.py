"""v2.1z-s: `build.setup_command` / `build.teardown_command` — argv run once per run around the whole migration (the
Kubernetes split layout creates / deletes the builder pod with them). The environment carries SBMIG_RUN_ID /
SBMIG_RUN_DIR / SBMIG_PROJECT_DIR; a failing setup aborts the run, a failing teardown only warns, empty = nothing."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from sbmig.analyze import analyze_project
from sbmig.core.workspace import Workspace
from tests.loop.fakes import FakeBuildRunner, journal_events, ok_build
from tests.loop.test_orchestrator import make_orch

NS = 'xmlns="http://maven.apache.org/POM/4.0.0"'
POM = (f'<?xml version="1.0" encoding="UTF-8"?>\n<project {NS}>\n  <modelVersion>4.0.0</modelVersion>\n'
       "  <groupId>com.example</groupId>\n  <artifactId>hooked</artifactId>\n  <version>1.0</version>\n</project>\n")
TREE = {"pom.xml": POM, "src/main/java/com/example/App.java": "package com.example;\npublic class App {}\n"}


def _ws(tmp_path: Path) -> Workspace:
    src = tmp_path / "src" / "proj"
    for rel, body in TREE.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    w = Workspace.from_input(src, tmp_path / "out", run_id="hook-run")
    w.git_init()
    return w


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_hooks_run_once_around_the_run_with_the_run_facts_in_the_environment(cfg, tmp_path):
    marks = tmp_path / "marks"
    marks.mkdir()
    cfg.build.setup_command = _py(f"import os,json;open(r'{marks}/setup','w').write(json.dumps(dict(os.environ)))")
    cfg.build.teardown_command = _py(f"import os;open(r'{marks}/teardown','w').write(os.environ['SBMIG_RUN_ID'])")
    ws = _ws(tmp_path)
    orch = make_orch(cfg, ws, analyzer=analyze_project, build=FakeBuildRunner(ws, fn=lambda w, call: ok_build()))
    report = orch.run()
    assert orch.abort_reason is None and report.success
    env = json.loads((marks / "setup").read_text(encoding="utf-8"))
    assert env["SBMIG_RUN_ID"] == "hook-run" and Path(env["SBMIG_RUN_DIR"]).is_dir()
    assert Path(env["SBMIG_PROJECT_DIR"]).is_dir() and Path(env["SBMIG_PROJECT_DIR"]).name == "project"
    assert (marks / "teardown").read_text(encoding="utf-8") == "hook-run"
    hooks = [e for e in journal_events(ws) if e["event"] == "build_hook"]
    assert [(h["hook"], h["rc"]) for h in hooks] == [("setup", 0), ("teardown", 0)]


def test_a_failing_setup_aborts_the_run_and_teardown_still_runs(cfg, tmp_path):
    marks = tmp_path / "marks"
    marks.mkdir()
    cfg.build.setup_command = _py("import sys;print('no builder');sys.exit(3)")
    cfg.build.teardown_command = _py(f"open(r'{marks}/teardown','w').write('x')")
    ws = _ws(tmp_path)
    orch = make_orch(cfg, ws, analyzer=analyze_project, build=FakeBuildRunner(ws, fn=lambda w, call: ok_build()))
    report = orch.run()
    assert orch.abort_reason and "build.setup_command failed (rc=3)" in orch.abort_reason and "no builder" in orch.abort_reason
    assert not report.success
    assert (marks / "teardown").exists()                                   # the pod is deleted even after a failed setup
    hooks = [e for e in journal_events(ws) if e["event"] == "build_hook"]
    assert [(h["hook"], h["rc"]) for h in hooks] == [("setup", 3), ("teardown", 0)]


def test_a_failing_teardown_only_warns_and_empty_hooks_do_nothing(cfg, tmp_path):
    cfg.build.teardown_command = _py("import sys;sys.exit(1)")
    ws = _ws(tmp_path)
    orch = make_orch(cfg, ws, analyzer=analyze_project, build=FakeBuildRunner(ws, fn=lambda w, call: ok_build()))
    report = orch.run()
    assert orch.abort_reason is None and report.success
    assert [(h["hook"], h["rc"]) for h in journal_events(ws) if h["event"] == "build_hook"] == [("teardown", 1)]
    cfg.build.teardown_command = []
    ws2 = _ws(tmp_path / "second")
    orch2 = make_orch(cfg, ws2, analyzer=analyze_project, build=FakeBuildRunner(ws2, fn=lambda w, call: ok_build()))
    orch2.run()
    assert not [h for h in journal_events(ws2) if h["event"] == "build_hook"]
