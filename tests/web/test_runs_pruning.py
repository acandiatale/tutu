"""Round 9: RunManager keeps at most `max_finished_runs` finished runs in memory; evicted runs are still answered
from their run directory (status / list_runs / events)."""
from __future__ import annotations

import threading
from types import SimpleNamespace

from sbmig.web import RunManager

from .test_runs_flow import _zip_bytes, repo  # noqa: F401  (fixture)


def test_cap_one_keeps_only_the_newest_finished_run(tmp_path, repo, monkeypatch):  # noqa: F811
    monkeypatch.setattr(RunManager, "_run_thread_body",
                        lambda self, run: SimpleNamespace(success=True, blocked=[], flags=[]))
    mgr = RunManager(tmp_path / "out", repo, max_finished_runs=1)
    ids = []
    for name in ("a.zip", "b.zip", "c.zip"):
        rid = mgr.start(_zip_bytes(), name, ["artifacts.local-e2e.yml"])
        run = mgr.runs[rid]
        run.thread.join(10)
        assert not run.thread.is_alive()
        ids.append(rid)
    assert list(mgr.runs) == [ids[-1]]
    rows = {r["run_id"]: r for r in mgr.list_runs()}
    assert set(ids) <= set(rows)
    for rid in ids:
        assert rows[rid]["state"] == "finished" and rows[rid]["exit_code"] == 0
    st = mgr.status(ids[0])                                          # evicted: answered from web-status.json
    assert st["state"] == "finished" and st["exit_code"] == 0 and "error" not in st
    events = list(mgr.events(ids[0]))                                # does not block on an evicted run
    assert events[0][0] == "status" and events[-1] == ("end", {"run_id": ids[0]})
    assert mgr.stop(ids[0]) is False


def test_running_and_queued_runs_are_never_evicted(tmp_path, repo, monkeypatch):  # noqa: F811
    release = threading.Event()

    def body(self, run):
        if run.zip_path.name.endswith("slow.zip"):
            release.wait(10)
        return SimpleNamespace(success=True, blocked=[], flags=[])
    monkeypatch.setattr(RunManager, "_run_thread_body", body)
    mgr = RunManager(tmp_path / "out", repo, max_finished_runs=0)
    slow = mgr.start(_zip_bytes(), "slow.zip", ["artifacts.local-e2e.yml"])
    queued = mgr.start(_zip_bytes(), "q.zip", ["artifacts.local-e2e.yml"])
    assert mgr.runs[slow].active.wait(10)
    mgr._prune_locked()                                               # nothing finished yet: nothing goes
    assert set(mgr.runs) == {slow, queued}
    assert mgr.status(queued)["state"] == "queued"
    t_slow, t_q = mgr.runs[slow].thread, mgr.runs[queued].thread
    release.set()
    t_slow.join(10)
    t_q.join(10)
    assert mgr.runs == {}                                             # cap 0: every finished run leaves memory
    assert mgr.status(slow)["state"] == "finished" and mgr.status(queued)["exit_code"] == 0


def test_crashed_run_keeps_its_error_after_eviction(tmp_path, repo, monkeypatch):  # noqa: F811
    def body(self, run):
        raise RuntimeError("engine bug")
    monkeypatch.setattr(RunManager, "_run_thread_body", body)
    mgr = RunManager(tmp_path / "out", repo, max_finished_runs=0)
    rid = mgr.start(_zip_bytes(), "p.zip", ["artifacts.local-e2e.yml"])
    run = mgr.runs.get(rid)                                          # may already be evicted (cap 0)
    if run is not None:
        run.thread.join(10)
    st = mgr.status(rid)
    assert rid not in mgr.runs and st["state"] == "finished" and st["exit_code"] == 3 and "engine bug" in st["error"]
