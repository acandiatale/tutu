"""2026-11-05 (company run): the migration target parent declares a wagon build extension — the intake pre-checks the chain's
extensions before the first Maven call: stale markers cleared (journal), an unserved extension → WARNING, flag
`parent-extension-unresolvable`, one Handover line."""
from __future__ import annotations

from pathlib import Path

import sbmig.build.parent_extensions as pe
from sbmig.build.parent_extensions import ExtensionCheck
from sbmig.core.models import Phase
from sbmig.core.workspace import Workspace

from .fakes import FakeBuildRunner, journal_events
from .test_orchestrator import files_build, make_orch

NS = 'xmlns="http://maven.apache.org/POM/4.0.0"'
WAGON = "org.apache.maven.wagon:wagon-webdav:1.0-beta-2"


def _ws(tmp_path: Path) -> Workspace:
    src = tmp_path / "src" / "proj"
    src.mkdir(parents=True)
    (src / "pom.xml").write_text(f'<?xml version="1.0" encoding="UTF-8"?>\n<project {NS}>\n  <modelVersion>4.0.0</modelVersion>\n'
                                 "  <groupId>com.xxx</groupId>\n  <artifactId>xxx</artifactId>\n  <version>1.0</version>\n</project>\n",
                                 encoding="utf-8")
    w = Workspace.from_input(src, tmp_path / "out", run_id="t-run")
    w.git_init()
    return w


def test_an_unserved_parent_extension_is_flagged_and_handed_over(cfg, tmp_path, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(pe, "check_parent_build_extensions",
                        lambda c, facts=None, clear_markers=True: (seen.append("checked"),
                                                                   [ExtensionCheck(WAGON, "com.company:base-parent:4.0.0", False,
                                                                                   "not in the local repository and not readable from the repository")])[1])
    ws = _ws(tmp_path)
    orch = make_orch(cfg, ws, build=FakeBuildRunner(ws, fn=files_build))
    order: list[str] = []
    orch._capture_effective = lambda report: order.append("capture")
    report = orch.run(stop_after=Phase.INTAKE)
    assert seen == ["checked"] and order == ["capture"]
    assert "parent-extension-unresolvable" in report.flags and "aborted" not in report.flags
    assert any(f.startswith(f"the company parent com.company:base-parent:4.0.0 declares the build extension {WAGON}")
               for f in report.manual_followups)
    ev = [e for e in journal_events(ws) if e["event"] == "parent_build_extensions"]
    assert len(ev) == 1 and ev[0]["unresolvable"] == [WAGON]


def test_cleared_markers_are_journalled_without_a_flag(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(pe, "check_parent_build_extensions",
                        lambda c, facts=None, clear_markers=True: [ExtensionCheck(WAGON, "com.company:base-parent:4.0.0", True,
                                                                                  "repository", ["wagon-webdav-1.0-beta-2.pom.lastUpdated"])])
    ws = _ws(tmp_path)
    report = make_orch(cfg, ws, build=FakeBuildRunner(ws, fn=files_build)).run(stop_after=Phase.INTAKE)
    assert "parent-extension-unresolvable" not in report.flags
    ev = [e for e in journal_events(ws) if e["event"] == "parent_build_extensions"]
    assert ev[0]["markers_removed"] == {WAGON: ["wagon-webdav-1.0-beta-2.pom.lastUpdated"]} and ev[0]["unresolvable"] == []


def test_no_extensions_no_journal(cfg, tmp_path):
    ws = _ws(tmp_path)                                   # conftest: the chain facts are empty in loop tests
    make_orch(cfg, ws, build=FakeBuildRunner(ws, fn=files_build)).run(stop_after=Phase.INTAKE)
    assert not [e for e in journal_events(ws) if e["event"] == "parent_build_extensions"]
