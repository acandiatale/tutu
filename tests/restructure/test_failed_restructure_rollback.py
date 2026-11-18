"""A restructure that fails half way (after its own commits and file moves) puts the tree back to the commit it
started from and says so (review P2): the orchestrator's Handover sentence "the tree keeps the as-is layout" holds."""
from __future__ import annotations

from pathlib import Path

from sbmig.core.config import Config
from sbmig.restructure import PomEditError, Restructurer
from sbmig.restructure.restructurer import Restructurer as _R

from .helpers import make_ws
from .test_restructurer import boot2_files, boot2_profile


def _tree(ws) -> dict[str, bytes]:
    return {p.relative_to(ws.root).as_posix(): p.read_bytes() for p in ws.root.rglob("*")
            if p.is_file() and ".git" not in p.parts}


def test_a_failure_after_own_commits_resets_the_tree(tmp_path: Path, cfg: Config, monkeypatch) -> None:
    ws = make_ws(tmp_path, boot2_files(), "half")
    start, before = ws.head(), _tree(ws)

    def boom(self, *a, **k):
        raise PomEditError("simulated failure in _write_boot")

    monkeypatch.setattr(_R, "_write_boot", boom)
    res = Restructurer().apply(ws, boot2_profile(ws), cfg, mode="layout")
    assert not res.ok and "simulated failure" in (res.error or "")
    assert ws.head() == start and not ws.is_dirty()
    assert _tree(ws) == before
    assert res.changed_files == [] and res.moved_files == 0 and res.generated_files == []
    assert res.notes == [f"restructure undone: the tree was reset to {start[:12]}, the commit before this step, "
                         "because the step failed"]


def test_a_failure_without_changes_leaves_head_alone(tmp_path: Path, cfg: Config, monkeypatch) -> None:
    ws = make_ws(tmp_path, boot2_files(), "early")
    start = ws.head()

    def boom(self, c):
        raise PomEditError("failed before any change")

    monkeypatch.setattr(_R, "_layout", boom)
    res = Restructurer().apply(ws, boot2_profile(ws), cfg, mode="layout")
    assert not res.ok and ws.head() == start
    assert not any(n.startswith("restructure undone") for n in res.notes)
