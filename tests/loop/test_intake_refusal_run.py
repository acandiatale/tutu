"""Review 2026-11-18 W3 follow-up: a workspace refused at intake (`Workspace.intake_refusal`: an uploaded ZIP over the
unpacking limits, nothing unpacked) is ended by `Orchestrator.run()` itself — both engines, every flow — with exactly
that reason (`run aborted: <reason>`, flag `aborted`, exit 3), before preflight, before any Maven / LLM call and before
intake edits anything. `sbmig.api.migrate` and every other caller of `run()` get the same record as the CLI."""
from __future__ import annotations

import pytest

from sbmig.cli import run_exit_code
from sbmig.core.workspace import Workspace

from .fakes import FakeBuildRunner, FakeFixer, journal_events
from .test_orchestrator import make_orch

REASON = "the ZIP unpacks to more than 1024 MB (intake.max_unpacked_mb): refused, nothing was unpacked"


def _refused(tmp_path) -> Workspace:
    src = tmp_path / "empty"
    src.mkdir()
    ws = Workspace.from_input(src, tmp_path / "out", run_id="t-refused")
    ws.intake_refusal = REASON
    return ws


def _never(*a, **k):
    pytest.fail("nothing may run for a workspace refused at intake")


def _orch(cfg, ws, engine):
    cfg = cfg.model_copy(deep=True)
    cfg.loop.engine = engine
    return make_orch(cfg, ws, build=FakeBuildRunner(ws, fn=_never), fixer=FakeFixer(fn=_never),
                     analyzer=_never, planner=_never)


@pytest.mark.parametrize("engine", ["graph", "inline"])
@pytest.mark.parametrize("flow", ["full", "remediation"])
def test_run_ends_a_workspace_refused_at_intake(cfg, tmp_path, monkeypatch, engine, flow):
    from sbmig.remediations import chain
    monkeypatch.setattr(chain, "run_only", _never)
    ws = _refused(tmp_path)
    orch = _orch(cfg, ws, engine)
    monkeypatch.setattr(orch, "_preflight", _never, raising=False)
    report = orch.run(**({"flow": flow} if flow != "full" else {}))
    assert "aborted" in report.flags and run_exit_code(report) == 3
    assert report.manual_followups[0] == f"run aborted: {REASON}"
    assert [m for m in report.manual_followups if m.startswith("run aborted:")] == [f"run aborted: {REASON}"]
    assert orch.abort_reason == REASON
    assert [e["reason"] for e in journal_events(ws) if e["event"] == "abort"] == [REASON]
    assert not (ws.root / ".git").exists() and list(ws.root.iterdir()) == []      # intake edited nothing
    assert (ws.run_dir / "report.md").exists()


@pytest.mark.parametrize("flow", ["full", "remediation"])
def test_stream_graph_called_directly_ends_it_too(cfg, tmp_path, monkeypatch, flow):
    from sbmig.graph import stream_graph
    from sbmig.remediations import chain
    monkeypatch.setattr(chain, "run_only", _never)
    ws = _refused(tmp_path)
    orch = _orch(cfg, ws, "graph")
    list(stream_graph(orch, flow=flow))
    assert orch.report is not None and run_exit_code(orch.report) == 3
    assert orch.report.manual_followups[0] == f"run aborted: {REASON}"


def test_the_clis_own_early_refusal_records_it_once(cfg, tmp_path):
    from sbmig.cli import refuse_intake
    ws = _refused(tmp_path)
    report = refuse_intake(_orch(cfg, ws, "graph"), REASON)
    assert [m for m in report.manual_followups if m.startswith("run aborted:")] == [f"run aborted: {REASON}"]
