"""Review 2026-11-18 R4, orchestrator side: `Orchestrator._finalize_code_diff` hands every run that is not a dry run
to `sbmig.report.codediff.write_code_diff`, which journals why it skips (`report.code_diff` false, no `.git`) and removes
a record an earlier attempt left in the same run folder. Finalize never raises because of it."""
from __future__ import annotations

import shutil

import pytest

from sbmig.report import codediff

from .fakes import journal_events
from .test_orchestrator import make_orch


def _stale(ws):
    d = ws.run_dir / codediff.DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    (d / "summary.json").write_text("{}", encoding="utf-8")
    return d


def test_code_diff_off_still_reaches_the_writer_and_the_stale_record_goes(cfg, ws):
    cfg.report.code_diff = False
    stale = _stale(ws)
    make_orch(cfg, ws)._finalize_code_diff()
    assert not stale.exists()
    assert any(e["event"] == "code_diff_skipped" and e["reason"] == "report.code_diff is false"
               for e in journal_events(ws))


def test_a_project_without_git_reaches_the_writer(cfg, ws):
    cfg.report.code_diff = True
    shutil.rmtree(ws.root / ".git")
    stale = _stale(ws)
    make_orch(cfg, ws)._finalize_code_diff()
    assert not stale.exists()
    assert any(e["event"] == "code_diff_skipped" and e["reason"] == "the project has no git repository"
               for e in journal_events(ws))


def test_a_dry_run_does_not_call_the_writer(cfg, ws, monkeypatch):
    monkeypatch.setattr(codediff, "write_code_diff", lambda *a, **k: pytest.fail("no code diff for a dry run"))
    orch = make_orch(cfg, ws)
    orch._dry_run = True
    orch._finalize_code_diff()


def test_a_failing_writer_never_raises_out_of_finalize(cfg, ws, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(codediff, "write_code_diff", boom)
    make_orch(cfg, ws)._finalize_code_diff()
