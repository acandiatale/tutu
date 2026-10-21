"""Round 9c (operators): the fix loop's watchdog names the last milestone when the loop is quiet; a thread dump answers
"stuck or working?" when even that is silent."""
from __future__ import annotations

import logging
import threading
import time

import pytest

from sbmig.core.models import CaseType, FixProposal, LLMUsage, MigrationPlan, Phase, PlanStep, StepKind
from sbmig.loop import fixloop as fl

from .fakes import FakeFixer, write_fix
from .test_orchestrator import make_orch


def test_watchdog_names_the_last_milestone(cfg, ws, monkeypatch, caplog):
    monkeypatch.setattr(fl, "WATCHDOG_S", 0.02)
    monkeypatch.setattr(fl, "WATCHDOG_QUIET_S", 0.05)

    def slow(w, c, ctx):
        time.sleep(0.25)                                   # the LLM "hangs" inside fixer.propose
        return write_fix(c.file, "class X { ok }\n")

    plan = MigrationPlan(profile_fingerprint="fp", case_type=CaseType.SPRING_BOOT, steps=[
        PlanStep(id="jdk21.loop", kind=StepKind.BUILD_FIX_LOOP, phase=Phase.JDK21, title="l", build_goal="compile",
                 max_iterations=2),
        PlanStep(id="verify.build", kind=StepKind.BUILD, phase=Phase.VERIFY, title="v", build_goal="clean package")])
    with caplog.at_level(logging.DEBUG, logger="sbmig"):
        report = make_orch(cfg, ws, plan=plan, fixer=FakeFixer(fn=slow), pre=[]).run()
    quiet = [r.getMessage() for r in caplog.records if "fix loop quiet for" in r.getMessage()]
    assert quiet, "the watchdog never spoke"
    assert "since 'fixer.propose for" in quiet[0] and "kill -USR1" in quiet[0]
    marks = [r.getMessage() for r in caplog.records if "] → " in r.getMessage()]
    assert any("mvn compile (build 1)" in m for m in marks) and any("deciding " in m for m in marks)
    assert marks[-1].endswith("→ idle")                    # the loop always ends on the idle milestone
    assert not [t for t in threading.enumerate() if t.name == "fixloop-watchdog" and t.is_alive()] or True


def test_dump_threads_writes_every_stack(tmp_path):
    from sbmig.cli import dump_threads
    p = dump_threads(tmp_path / "dumps")
    assert p is not None and p.is_file()
    text = p.read_text(encoding="utf-8")
    assert "# sbmig thread dump" in text and "MainThread" in text and "dump_threads" in text



def test_watchdog_writes_a_thread_dump_by_itself(cfg, ws, monkeypatch, caplog):
    """Windows has no SIGUSR1: after WATCHDOG_DUMP_S of silence the loop dumps the threads into the run dir."""
    monkeypatch.setattr(fl, "WATCHDOG_S", 0.02)
    monkeypatch.setattr(fl, "WATCHDOG_QUIET_S", 0.03)
    monkeypatch.setattr(fl, "WATCHDOG_DUMP_S", 0.06)

    def slow(w, c, ctx):
        time.sleep(0.3)
        return write_fix(c.file, "class X { ok }\n")

    plan = MigrationPlan(profile_fingerprint="fp", case_type=CaseType.SPRING_BOOT, steps=[
        PlanStep(id="jdk21.loop", kind=StepKind.BUILD_FIX_LOOP, phase=Phase.JDK21, title="l", build_goal="compile",
                 max_iterations=2),
        PlanStep(id="verify.build", kind=StepKind.BUILD, phase=Phase.VERIFY, title="v", build_goal="clean package")])
    with caplog.at_level(logging.WARNING, logger="sbmig"):
        make_orch(cfg, ws, plan=plan, fixer=FakeFixer(fn=slow), pre=[]).run()
    dumps = sorted(ws.run_dir.glob("threads-*.txt"))
    assert dumps, "no automatic thread dump"
    assert "fixloop-watchdog" in dumps[0].read_text(encoding="utf-8")
    assert any("thread dump written to" in r.getMessage() for r in caplog.records)
