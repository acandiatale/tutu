"""Review 2026-11-18 S2: the `graph_node_end` line of a node that raised a run abort carries the reason text only — the
Job-mode page showed "중단: _RunAborted: Maven (3.8.1+) refuses …". An internal error keeps its class name."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from sbmig.graph.engine import _staged
from sbmig.loop.orchestrator import _RunAborted


def _orch():
    lines: list[dict] = []
    return SimpleNamespace(ws=SimpleNamespace(journal=lines.append), abort_reason="parent not resolvable"), lines


def _raise(exc):
    def phase(state):
        raise exc
    return phase


def test_run_abort_note_has_no_class_name():
    orch, lines = _orch()
    node = _staged("verify", _raise(_RunAborted("Maven (3.8.1+) refuses http repositories")), orch)
    with pytest.raises(_RunAborted):
        node({})
    [end] = [x for x in lines if x["event"] == "graph_node_end"]
    assert end["aborted"] == "Maven (3.8.1+) refuses http repositories"


def test_run_abort_without_text_uses_the_abort_reason():
    orch, lines = _orch()
    node = _staged("intake", _raise(_RunAborted()), orch)
    with pytest.raises(_RunAborted):
        node({})
    assert [x for x in lines if x["event"] == "graph_node_end"][0]["aborted"] == "parent not resolvable"


def test_internal_error_keeps_its_class_name():
    orch, lines = _orch()
    node = _staged("verify", _raise(ValueError("bad")), orch)
    with pytest.raises(ValueError):
        node({})
    assert [x for x in lines if x["event"] == "graph_node_end"][0]["aborted"] == "ValueError: bad"
