"""「지난 실행」 shows the last three days (the owner, 2026-11-19: "지난 실행은 3일치만 보여주도록"): `GET /api/runs?days=N`
cuts the list on the server so the page's 15-second refresh does not carry the whole history. Always listed whatever
their age: runs that are queued or running, a remediation that waits for a person or runs again in a new pod, and the run
the page has open (`keep`). `days` = a non-negative integer, 0 / omitted = everything; anything else → 400.
Also: a run a person stopped carries `user_stop: true` in its status, so the page can show it in the caution colour."""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sbmig.core.config import PACKAGE_ROOT
from sbmig.core.workspace import STOP_REQUEST_REASON
from sbmig.loop.orchestrator import STOP_LOOP_REASON
from sbmig.web import create_app
from sbmig.web.contract import RESUME_STATE_FILE, ControlState, RunState

DAY = 86400.0


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    shutil.copy(PACKAGE_ROOT / "config" / "artifacts.local-e2e.yml", repo / "config" / "artifacts.local-e2e.yml")
    return TestClient(create_app(tmp_path / "out", repo))


def _run(client: TestClient, rid: str, age_days: float, *, report: dict | None = None, exit_code: int | None = 0,
         stop_requested: bool = False) -> Path:
    rd = Path(client.app.state.manager.out_dir) / rid
    rd.mkdir(parents=True)
    rep = {"success": exit_code == 0, "blocked": [], "unresolved": [], **(report or {})}
    (rd / "report.json").write_text(json.dumps(rep), encoding="utf-8")
    (rd / "web-status.json").write_text(json.dumps({"run_id": rid, "exit_code": exit_code, "flow": "full",
                                                    "stop_requested": stop_requested}), encoding="utf-8")
    _age(rd, age_days)
    return rd


def _age(rd: Path, age_days: float) -> None:
    t = time.time() - age_days * DAY
    os.utime(rd, (t, t))


def _ids(d: dict) -> list[str]:
    return sorted(r["run_id"] for r in d["runs"])


def test_days_cuts_by_age_and_reports_what_is_hidden(client) -> None:
    for rid, age in (("d1", 1), ("d2", 2), ("d4", 4), ("d10", 10)):
        _run(client, rid, age)
    d = client.get("/api/runs?days=3").json()
    assert _ids(d) == ["d1", "d2"]
    assert d["days"] == 3 and d["hidden"] == 2
    # omitted and 0 = the whole list (older clients, the 「이전 실행 보기」 control)
    for q in ("", "?days=0"):
        d = client.get(f"/api/runs{q}").json()
        assert _ids(d) == ["d1", "d10", "d2", "d4"] and d["hidden"] == 0 and d["days"] == 0
    assert _ids(client.get("/api/runs?days=5").json()) == ["d1", "d2", "d4"]


def test_age_boundary(client) -> None:
    _run(client, "inside", 3 - 60 / DAY)          # one minute inside the window
    _run(client, "outside", 3 + 60 / DAY)         # one minute outside
    assert _ids(client.get("/api/runs?days=3").json()) == ["inside"]


def test_always_listed_whatever_the_age(client) -> None:
    mgr = client.app.state.manager
    # still running (a Job run recorded by Main, a pod in the cluster): its folder is 10 days old
    rd = Path(mgr.out_dir) / "old-running"
    rd.mkdir(parents=True)
    ControlState(run_id="old-running", state=RunState.RUNNING.value, job="sbmig-old").write(rd)
    _age(rd, 10)
    # a remediation waiting for a person
    _run(client, "old-awaiting", 9, report={"remediation": {"status": "AWAITING_APPROVAL"}})
    # a remediation running again in a new pod
    rd2 = _run(client, "old-resumed", 8, report={"remediation": {"status": "STOPPED"}})
    ControlState(run_id="old-resumed", state=RunState.RUNNING.value, job="sbmig-old-rem2",
                 action="remediation-resume").write(rd2, RESUME_STATE_FILE)
    _age(rd2, 8)
    # the run the page has open
    _run(client, "old-open", 20)
    # an old finished one stays out
    _run(client, "old-done", 7, report={"remediation": {"status": "SUCCEEDED"}})
    d = client.get("/api/runs?days=3&keep=old-open").json()
    assert _ids(d) == ["old-awaiting", "old-open", "old-resumed", "old-running"]
    assert d["hidden"] == 1
    states = {r["run_id"]: r["state"] for r in d["runs"]}
    assert states["old-running"] == "running"


def test_bad_input_is_refused(client) -> None:
    for q in ("days=-1", "days=abc", "days=1.5", "days=", "days=3&keep=..%2Fetc", "days=3&keep=bad$id"):
        r = client.get(f"/api/runs?{q}")
        assert r.status_code == 400, q
        assert "error" in r.json()
    assert client.get("/api/runs?days=00").status_code == 200


def test_user_stop_flag(client) -> None:
    # stopped from the page (web-status.json says so) and ended without a result: a user stop
    _run(client, "stopped", 0, exit_code=3, stop_requested=True,
         report={"success": False, "manual_followups": [f"run aborted: {STOP_REQUEST_REASON}"]})
    # stopped through stop.request outside the page: the engine's abort reason says so
    _run(client, "stopped-file", 0, exit_code=3,
         report={"success": False, "manual_followups": [f"run aborted: {STOP_REQUEST_REASON}"]})
    # the fix loop left early on a stop request: the same prefix
    _run(client, "stopped-loop", 0, exit_code=3,
         report={"success": False, "manual_followups": [f"run aborted: {STOP_LOOP_REASON}"]})
    # an engine abort (refused intake) is not a user stop
    _run(client, "refused", 0, exit_code=3,
         report={"success": False, "manual_followups": ["run aborted: Maven 변환 후 신청 부탁드립니다."]})
    # a failed build is not a user stop
    _run(client, "failed", 0, exit_code=2, report={"success": False})
    # a stop asked for too late (the run finished with a result) is not shown as stopped
    _run(client, "late-stop", 0, exit_code=2, stop_requested=True, report={"success": False})
    # a Job run withdrawn while waiting or deleted before its pod started: Main recorded `stopped`
    rd = Path(client.app.state.manager.out_dir) / "withdrawn"
    rd.mkdir(parents=True)
    ControlState(run_id="withdrawn", state=RunState.STOPPED.value, exit_code=3,
                 error="stopped before the job was created").write(rd)
    # a lost pod is not a user stop
    rd2 = Path(client.app.state.manager.out_dir) / "lost"
    rd2.mkdir(parents=True)
    ControlState(run_id="lost", state=RunState.LOST.value, exit_code=3, error="job disappeared").write(rd2)
    flags = {rid: client.get(f"/api/runs/{rid}").json().get("user_stop") for rid in
             ("stopped", "stopped-file", "stopped-loop", "refused", "failed", "late-stop", "withdrawn", "lost")}
    assert flags == {"stopped": True, "stopped-file": True, "stopped-loop": True, "refused": None, "failed": None, "late-stop": None,
                     "withdrawn": True, "lost": None}
