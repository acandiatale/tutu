"""RunManager flow regressions (2026-10-20 review): a stop after the final build does not mislabel a finished run,
runs are serialized on the shared KB (queued state), a crash before the body still finishes the run."""
from __future__ import annotations

import io
import sys
import threading
import types
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import sbmig.web.runs as runs_mod
from sbmig.web import RunManager


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("demo/pom.xml", "<project><modelVersion>4.0.0</modelVersion><groupId>g</groupId>"
                                    "<artifactId>demo</artifactId><version>1</version></project>")
    return buf.getvalue()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    import shutil
    from sbmig.core.config import PACKAGE_ROOT
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    shutil.copy(PACKAGE_ROOT / "config" / "artifacts.local-e2e.yml", repo / "config" / "artifacts.local-e2e.yml")
    (repo / "config" / "default.yml").write_text("x: 1\n")
    return repo


def _fake_graph(monkeypatch, script, closed):
    """`sbmig.graph.stream_graph` stand-in: yields `script` (node, update) pairs; closing early finalizes."""
    def stream_graph(orch, stop_after=None, dry_run=False):
        orch.report = SimpleNamespace(flags=[], manual_followups=[], finished_at=None, success=True, blocked=[],
                                      final_build=None)
        try:
            for node, update in script:
                if node == "phase" and update.get("phase") == "verify":
                    orch.report.final_build = SimpleNamespace(ok=True)
                yield node, update
        finally:
            closed.append(list(orch.report.flags))
            orch.report.finished_at = "now"
    monkeypatch.setitem(sys.modules, "sbmig.graph", types.SimpleNamespace(stream_graph=stream_graph))


def test_stop_after_the_final_build_finishes_normally(tmp_path, repo, monkeypatch):
    closed: list = []
    script = [("intake", {}), ("analyze", {}), ("plan", {"phases": ["jdk21", "verify"]}),
              ("phase", {"phase": "jdk21", "phases": ["verify"]}), ("phase", {"phase": "verify", "phases": []}),
              ("finalize", {"phase": None})]
    _fake_graph(monkeypatch, script, closed)
    mgr = RunManager(tmp_path / "out", repo)
    orig_push = RunManager._push

    def push(self, run, kind, data):          # the user clicked Stop while the verify phase (final build) ran
        if kind == "node" and data["node"] == "phase" and data["update"].get("phase") == "verify":
            run.cancel.set()
        orig_push(self, run, kind, data)
    monkeypatch.setattr(RunManager, "_push", push)
    rid = mgr.start(_zip_bytes(), "p.zip", ["artifacts.local-e2e.yml"])
    events = list(mgr.events(rid))
    assert [d["node"] for k, d in events if k == "node"] == ["intake", "analyze", "plan", "phase", "phase", "finalize"]
    run = mgr.runs[rid]
    assert closed == [[]] and "aborted" not in run.report.flags and run.exit_code == 0
    assert events[-2][1]["stop_requested"] is True and events[-2][1]["exit_code"] == 0


def test_stop_before_the_final_build_aborts_and_says_no_final_build(tmp_path, repo, monkeypatch):
    closed: list = []
    script = [("intake", {}), ("analyze", {}), ("plan", {"phases": ["jdk21", "verify"]}),
              ("phase", {"phase": "jdk21", "phases": ["verify"]}), ("phase", {"phase": "verify", "phases": []})]
    _fake_graph(monkeypatch, script, closed)
    mgr = RunManager(tmp_path / "out", repo)
    orig_push = RunManager._push

    def push(self, run, kind, data):
        orig_push(self, run, kind, data)
        if kind == "node" and data["node"] == "plan":
            run.cancel.set()
    monkeypatch.setattr(RunManager, "_push", push)
    rid = mgr.start(_zip_bytes(), "p.zip", ["artifacts.local-e2e.yml"])
    events = list(mgr.events(rid))
    assert [d["node"] for k, d in events if k == "node"] == ["intake", "analyze", "plan"]
    run = mgr.runs[rid]
    assert closed == [["aborted"]] and run.exit_code == 3
    assert any("final `mvn clean package` did not run" in f for f in run.report.manual_followups)


def test_runs_are_serialized_and_report_queued(tmp_path, repo, monkeypatch):
    release = threading.Event()
    started: list[str] = []

    def body(self, run):
        started.append(run.run_id)
        release.wait(10)
        return SimpleNamespace(success=True, blocked=[], flags=[])
    monkeypatch.setattr(RunManager, "_run_thread_body", body)
    mgr = RunManager(tmp_path / "out", repo)
    first = mgr.start(_zip_bytes(), "a.zip", ["artifacts.local-e2e.yml"])
    second = mgr.start(_zip_bytes(), "b.zip", ["artifacts.local-e2e.yml"])
    assert mgr.runs[first].active.wait(10)
    assert mgr.status(first)["state"] == "running" and mgr.status(second)["state"] == "queued"
    assert started == [first]
    release.set()
    assert mgr.runs[second].done.wait(10) and mgr.runs[first].done.wait(10)
    assert started == [first, second]
    assert mgr.status(second)["state"] == "finished" and mgr.status(second)["exit_code"] == 0


def test_crash_before_the_body_still_finishes_the_run(tmp_path, repo, monkeypatch):
    import sbmig.cli as cli

    def attach(path, thread_id=None):
        raise OSError("log directory not writable")
    monkeypatch.setattr(cli, "attach_file_log", attach)
    monkeypatch.setattr(RunManager, "_run_thread_body", lambda self, run: SimpleNamespace(success=True, blocked=[], flags=[]))
    mgr = RunManager(tmp_path / "out", repo)
    rid = mgr.start(_zip_bytes(), "p.zip", ["artifacts.local-e2e.yml"])
    events = list(mgr.events(rid))
    kinds = [k for k, _ in events]
    assert "error" in kinds and kinds[-1] == "end"
    st = mgr.status(rid)
    assert st["state"] == "finished" and st["exit_code"] == runs_mod.EXIT_ABORTED and "not writable" in st["error"]


def test_bad_stop_after_is_rejected(tmp_path, repo):
    mgr = RunManager(tmp_path / "out", repo)
    with pytest.raises(ValueError, match="stop_after must be one of"):
        mgr.start(_zip_bytes(), "p.zip", ["artifacts.local-e2e.yml"], stop_after="report")
    assert mgr.runs == {}
