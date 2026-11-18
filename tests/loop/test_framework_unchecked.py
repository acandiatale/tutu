"""Review 2026-11-18 T1: framework artifacts configured, the as-is effective pom (parent) AND the dependency tree could not be
read, nothing found elsewhere → the run goes on as before, but says so: flag `framework-unchecked` and one Handover line
first in the list. A transitive lookup switched off / not made is not "could not be read". The exit code is unchanged."""
from __future__ import annotations

import pytest

from sbmig.core.models import FrameworkEvidence
from sbmig.loop.orchestrator import FRAMEWORK_UNCHECKED_FLAG, FRAMEWORK_UNCHECKED_LINE

from .test_orchestrator_framework import FW, FakeFw, bare_orch, empty_use, events, fw_cfg, fw_profile, install

LINE = ("framework check could not run (the parent pom and the dependency tree could not be read): if this project uses "
        "the in-house framework it must not be migrated in full — confirm before using the result")


def test_the_sentence_is_the_agreed_one():
    assert FRAMEWORK_UNCHECKED_LINE == LINE and FRAMEWORK_UNCHECKED_FLAG == "framework-unchecked"


def test_both_sources_unreadable_flags_and_says_so_first(cfg, ws, monkeypatch):
    install(monkeypatch, FakeFw(why="mvn dependency:tree failed (exit 1)"))
    orch = bare_orch(fw_cfg(cfg), ws)
    orch.report.manual_followups = ["other line"]
    prof = fw_profile(empty_use())
    orch._check_framework(orch.report, prof)
    assert "framework-unchecked" in orch.report.flags and "framework-project" not in orch.report.flags
    assert orch.report.manual_followups == [LINE, "other line"]
    assert not prof.reduced_scope
    assert events(ws, "framework_check")[0]["found"] is False


@pytest.mark.parametrize("why", ["off", "dry-run", "custom-runner"])
def test_a_transitive_lookup_not_made_is_not_flagged(cfg, ws, monkeypatch, why):
    install(monkeypatch, FakeFw())
    orch = bare_orch(fw_cfg(cfg, transitive="off" if why == "off" else "apply"), ws,
                     real_maven=why != "custom-runner", dry_run=why == "dry-run")
    orch._check_framework(orch.report, fw_profile(empty_use()))
    assert "framework-unchecked" not in orch.report.flags and LINE not in orch.report.manual_followups


def test_found_through_the_tree_is_not_flagged(cfg, ws, monkeypatch):
    install(monkeypatch, FakeFw(transitive=[FrameworkEvidence(source="transitive", artifact=FW, version="1", where="demo")]))
    orch = bare_orch(fw_cfg(cfg), ws)
    orch._check_framework(orch.report, fw_profile(empty_use()))
    assert "framework-unchecked" not in orch.report.flags and "framework-project" in orch.report.flags


def test_parent_readable_is_not_flagged(cfg, ws, monkeypatch):
    install(monkeypatch, FakeFw(why="mvn dependency:tree failed (exit 1)"))
    orch = bare_orch(fw_cfg(cfg), ws)
    use = empty_use()
    use.unchecked = []                                          # the as-is effective pom was read: nothing inherited
    orch._check_framework(orch.report, fw_profile(use))
    assert "framework-unchecked" not in orch.report.flags
