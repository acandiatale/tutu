"""Review 2026-11-18 P2 (orchestrator side): a restructure step that raises half way, or returns ok=False after its own
commits, leaves the tree at the commit from before the step — so "the tree keeps the as-is layout / parents" is true."""
from __future__ import annotations

import pytest

from sbmig.core.interfaces import RestructureResult

from .fakes import FakeBuildRunner, ok_build
from .test_orchestrator import make_orch


class HalfwayRestructurer:
    """Moves a file and commits it (the restructurer's own commit), writes one more file, then fails."""

    def __init__(self, crash: bool):
        self.crash = crash

    def apply(self, ws, profile, cfg, *, mode, params=None):
        ws.write("module/boot-jar/pom.xml", "<project/>\n")
        ws.commit("restructure: wrap (own commit)")
        ws.write("half-moved.txt", "x\n")
        if self.crash:
            raise RuntimeError("PomEditError in _write_boot")
        return RestructureResult(ok=False, error="PomEditError in _write_boot", changed_files=["pom.xml"])


@pytest.mark.parametrize("crash", [True, False])
def test_failed_restructure_leaves_the_pre_step_tree(cfg, ws, crash):
    report = make_orch(cfg, ws, restructurer=HalfwayRestructurer(crash),
                       build=FakeBuildRunner(ws, fn=lambda w, c: ok_build())).run()
    assert "restructure-failed" in report.flags and not report.success
    assert not ws.exists("module/boot-jar/pom.xml") and not ws.exists("half-moved.txt")
    log = ws._git("log", "--format=%s").stdout
    assert "restructure: wrap (own commit)" not in log
    assert any("keeps the as-is layout / parents" in m for m in report.manual_followups)
