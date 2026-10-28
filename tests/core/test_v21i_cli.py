"""v2.1i switches on the command line: `sbmig run --fix-mode script|edits` becomes the `fix.mode` override.

No Maven: `sbmig.loop.Orchestrator` is a stand-in that records the config it was built with."""
from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest
from typer.testing import CliRunner

from sbmig.cli import app, check_fix_mode, fix_modes

REPO = Path(__file__).resolve().parents[2]
E2E = REPO / "config" / "artifacts.local-e2e.yml"


class _Orch:
    seen: ClassVar[list] = []

    def __init__(self, cfg, ws, kb=None, **kw):
        self.cfg, self.ws, self.kb = cfg, ws, kb
        _Orch.seen.append(self)

    def run(self, stop_after=None, dry_run=False):
        from sbmig.core.models import CaseType, LLMUsage, RunReport, utcnow

        return RunReport(run_id=self.ws.run_id, project_name="p", case_type=CaseType.SPRING_BOOT, started_at=utcnow(),
                         success=True, llm_usage=LLMUsage(calls=0))


@pytest.fixture
def orch(monkeypatch):
    from sbmig import loop

    _Orch.seen = []
    monkeypatch.setattr(loop, "Orchestrator", _Orch)
    return _Orch


def _project(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    (p / "pom.xml").write_text("<project/>")
    return p


def _invoke(tmp_path: Path, *extra: str):
    from sbmig.cli import detach_file_log

    out = tmp_path / "out"
    try:
        return CliRunner().invoke(app, ["run", str(_project(tmp_path)), "--config", str(E2E), "--out", str(out),
                                        "--llm-provider", "fake", "--stop-after", "plan", *extra])
    finally:
        for f in out.glob("*/sbmig.log"):
            detach_file_log(f)


def test_fix_modes_come_from_the_config_model():
    assert fix_modes() == ("script", "edits")
    assert check_fix_mode(None) is None and check_fix_mode("") is None and check_fix_mode(" Edits ") == "edits"


def test_fix_mode_edits_reaches_the_config(tmp_path, orch):
    r = _invoke(tmp_path, "--fix-mode", "edits")
    assert r.exit_code == 0, r.output
    o = orch.seen[-1]
    assert o.cfg.fix.mode == "edits" and o.kb is None          # KB stays on unless --no-kb
    log = next((tmp_path / "out").glob("*/sbmig.log")).read_text(encoding="utf-8")
    assert "fix_mode=edits kb=on" in log


def test_default_is_the_config_value_and_no_kb_is_named(tmp_path, orch):
    from sbmig.loop import NullKnowledgeBase

    r = _invoke(tmp_path, "--no-kb")
    assert r.exit_code == 0, r.output
    o = orch.seen[-1]
    assert o.cfg.fix.mode == "script" and isinstance(o.kb, NullKnowledgeBase)
    log = next((tmp_path / "out").glob("*/sbmig.log")).read_text(encoding="utf-8")
    assert "fix_mode=script kb=off" in log


def test_invalid_fix_mode_is_a_usage_error(tmp_path, orch):
    r = _invoke(tmp_path, "--fix-mode", "patch")
    assert r.exit_code == 2
    assert "--fix-mode" in r.output and "script | edits" in r.output
    assert orch.seen == []                                     # nothing started
