"""v2.1z-br, the web server's part of the safe sweep.

- Visible effect 1: a `report.json` that is valid JSON but not an object (`[]`, `null`, a number, a string) is treated
  exactly like one that cannot be parsed: `GET /api/runs` answers 200 with every row, the single-run status answers too.
- Visible effect 6: the status JSON no longer carries `run_dir` (no page reads it).
- Without a visible effect: `RunManager.status` reads `control/request.json` and `web-status.json` at most once per
  call; the framework journal scans (`_fw_scans`) and the admin page's login failures drop entries that are expired or
  can be computed again, with the same answers."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sbmig.core.config import PACKAGE_ROOT
from sbmig.web import admin, create_app
from sbmig.web import runs as runs_mod
from sbmig.web.contract import ControlState, RequestRecord, RunState

NON_OBJECTS = ["[]", "null", "3", '"x"']


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    shutil.copy(PACKAGE_ROOT / "config" / "artifacts.local-e2e.yml", repo / "config" / "artifacts.local-e2e.yml")
    return TestClient(create_app(tmp_path / "out", repo))


def _out(client: TestClient) -> Path:
    return Path(client.app.state.manager.out_dir)


def _finished(client: TestClient, rid: str, report_text: str | None) -> Path:
    rd = _out(client) / rid
    rd.mkdir(parents=True)
    if report_text is not None:
        (rd / "report.json").write_text(report_text, encoding="utf-8")
    (rd / "web-status.json").write_text(json.dumps({"run_id": rid, "exit_code": 0, "flow": "full", "upload": f"{rid}.zip",
                                                    "stop_requested": False}), encoding="utf-8")
    return rd


def _healthy(client: TestClient, rid: str) -> Path:
    return _finished(client, rid, json.dumps({"success": True, "blocked": [], "unresolved": [], "flow": "full"}))


def _comparable(row: dict) -> dict:
    # zip_name is built from the run id when the run has no project (v2.1z-bt)
    return {k: v for k, v in row.items() if k not in ("run_id", "started", "upload", "zip_name")}


@pytest.mark.parametrize("text", NON_OBJECTS)
def test_runs_list_answers_with_every_row_when_a_report_is_not_an_object(client, text) -> None:
    _healthy(client, "ok-one")
    _healthy(client, "ok-two")
    _finished(client, "odd", text)
    _finished(client, "broken", "{not json")
    r = client.get("/api/runs")
    assert r.status_code == 200
    rows = {row["run_id"]: row for row in r.json()["runs"]}
    assert set(rows) == {"ok-one", "ok-two", "odd", "broken"}
    assert rows["ok-one"]["success"] is True and rows["ok-two"]["success"] is True
    # exactly like a report that cannot be parsed: listed from the other files, without the report's fields
    assert _comparable(rows["odd"]) == _comparable(rows["broken"])
    assert rows["odd"]["state"] == "finished" and rows["odd"]["upload"] == "odd.zip"
    for key in ("success", "blocked", "unresolved", "llm_calls", "open", "resolved"):
        assert key not in rows["odd"]


@pytest.mark.parametrize("text", NON_OBJECTS)
def test_single_run_status_answers_when_the_report_is_not_an_object(client, text) -> None:
    _finished(client, "odd", text)
    _finished(client, "broken", "{not json")
    r = client.get("/api/runs/odd")
    assert r.status_code == 200
    st = r.json()
    assert st["state"] == "finished" and "success" not in st
    assert _comparable(st) == _comparable(client.get("/api/runs/broken").json())


def test_status_has_no_run_dir(client) -> None:
    _healthy(client, "ok-one")
    st = client.get("/api/runs/ok-one").json()
    assert "run_dir" not in st and "out_dir" not in st
    (row,) = client.get("/api/runs").json()["runs"]
    assert "run_dir" not in row and "out_dir" not in row
    # the server keeps finding the folder through the run manager
    assert client.app.state.manager.run_dir("ok-one") == _out(client) / "ok-one"


def _count_reads(monkeypatch) -> dict[str, int]:
    counts = {"request": 0, "web": 0}
    real_req, real_web = RequestRecord.read.__func__, runs_mod._read_web_status

    def req(cls, rd):
        counts["request"] += 1
        return real_req(cls, rd)

    def web(rd):
        counts["web"] += 1
        return real_web(rd)

    monkeypatch.setattr(RequestRecord, "read", classmethod(req))
    monkeypatch.setattr(runs_mod, "_read_web_status", web)
    return counts


def test_status_reads_web_status_once(client, monkeypatch) -> None:
    _healthy(client, "inproc")            # an in-process run of an earlier server: no request.json
    before = client.app.state.manager.status("inproc")
    counts = _count_reads(monkeypatch)
    after = client.app.state.manager.status("inproc")
    assert counts == {"request": 1, "web": 1}
    assert after == before and after["upload"] == "inproc.zip" and after["flow"] == "full"


def test_status_reads_request_json_once(client, monkeypatch) -> None:
    rd = _out(client) / "jobrun"
    rd.mkdir(parents=True)
    RequestRecord(run_id="jobrun", accepted_at="2026-01-01T00:00:00+00:00", upload="u.zip", configs=["c.yml"],
                  zip_sha256="0" * 64, config_digests={}, sbmig_version="test", mode="k8s-job", flow="remediation",
                  upload_name="Upload Name.zip", project="proj").write(rd)
    ControlState(run_id="jobrun", state=RunState.FINISHED.value, exit_code=0, job="sbmig-jobrun").write(rd)
    counts = _count_reads(monkeypatch)
    st = client.app.state.manager.status("jobrun")
    assert counts["request"] == 1
    assert counts["web"] <= 1
    assert (st["flow"], st["upload"], st["project"], st["state"]) == ("remediation", "Upload Name.zip", "proj", "finished")


def _framework_run(client: TestClient, rid: str) -> Path:
    rd = _out(client) / rid
    rd.mkdir(parents=True)
    lines = [{"event": "framework_project", "scope": "jdk-jakarta", "name": "Example", "fix_loop": False},
             {"event": "plan", "scope": "jdk-jakarta"}]
    (rd / "journal.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    return rd


def test_framework_scan_is_dropped_once_the_report_exists_and_computed_again_the_same(client) -> None:
    mgr = client.app.state.manager
    rd = _framework_run(client, "fw")
    first = mgr.status("fw")["framework"]
    assert "fw" in mgr._fw_scans
    report = {"success": False, "blocked": [], "unresolved": [],
              "profile": {"framework": {"scope": "jdk-jakarta", "name": "Example", "fix_loop": False}}}
    (rd / "report.json").write_text(json.dumps(report), encoding="utf-8")
    assert mgr.status("fw")["framework"] == first
    assert "fw" not in mgr._fw_scans
    # the report goes away again (or turns unreadable): the journal is read from the start, same answer
    (rd / "report.json").write_text("[]", encoding="utf-8")
    assert mgr.status("fw")["framework"] == first


def test_framework_scan_of_a_vanished_run_is_dropped_by_the_list(client) -> None:
    mgr = client.app.state.manager
    rd = _framework_run(client, "gone")
    _framework_run(client, "kept")
    mgr.list_runs()
    assert {"gone", "kept"} <= set(mgr._fw_scans)
    shutil.rmtree(rd)
    mgr.list_runs()
    assert "gone" not in mgr._fw_scans and "kept" in mgr._fw_scans


def test_admin_login_failures_drop_expired_addresses_with_the_same_answers() -> None:
    now = [1000.0]
    auth = admin.AdminAuth(clock=lambda: now[0], fail_delay_s=0)
    for _ in range(admin.FAIL_LIMIT):
        auth.failed("10.0.0.1")
    assert auth.blocked_for("10.0.0.1") > 0
    auth.failed("10.0.0.2")
    assert set(auth._failures) == {"10.0.0.1", "10.0.0.2"}       # both still inside the window
    now[0] += admin.FAIL_WINDOW_S + 1
    assert auth.failed("10.0.0.3") == 1
    assert set(auth._failures) == {"10.0.0.3"}
    assert auth.blocked_for("10.0.0.1") == 0.0 and auth.blocked_for("10.0.0.2") == 0.0
    # an address with one failure still inside the window is kept
    now[0] += 1
    auth.failed("10.0.0.4")
    assert set(auth._failures) == {"10.0.0.3", "10.0.0.4"}


# ---------------------------------------------------------------- follow-up: wrong field types, journal lines, the net
WRONG_TYPES = [3, "text", {"a": 1}, True, 1.5]


def _report_row(client: TestClient, rid: str, report: dict) -> dict:
    _finished(client, rid, json.dumps(report))
    r = client.get("/api/runs")
    assert r.status_code == 200
    rows = {row["run_id"]: row for row in r.json()["runs"]}
    single = client.get(f"/api/runs/{rid}")
    assert single.status_code == 200
    return rows[rid]


@pytest.mark.parametrize("key", ["blocked", "unresolved"])
@pytest.mark.parametrize("value", WRONG_TYPES)
def test_blocked_or_unresolved_of_another_type_counts_as_absent(client, key, value) -> None:
    _healthy(client, "ok")
    row = _report_row(client, "odd", {"success": False, key: value})
    assert row[key] == 0 and row["success"] is False


@pytest.mark.parametrize("value", WRONG_TYPES)
def test_error_entries_of_another_type_counts_as_absent(client, value) -> None:
    row = _report_row(client, "odd", {"success": True, "error_entries": value})
    assert (row["open"], row["resolved"]) == (0, 0)


@pytest.mark.parametrize("key", ["manual_followups", "flags"])
@pytest.mark.parametrize("value", WRONG_TYPES)
def test_abort_reason_fields_of_another_type_count_as_absent(client, key, value) -> None:
    row = _report_row(client, "odd", {"success": False, key: value})
    assert "abort_reason" not in row


@pytest.mark.parametrize("value", WRONG_TYPES + [[1, 2]])
def test_remediation_framework_and_llm_usage_of_another_type_do_not_raise(client, value) -> None:
    row = _report_row(client, "odd", {"success": True, "remediation": value, "profile": value, "llm_usage": value})
    assert "remediation" not in row and "framework" not in row and row["llm_calls"] is None


@pytest.mark.parametrize("line", NON_OBJECTS)
def test_journal_line_that_is_not_an_object_is_skipped(client, line) -> None:
    _healthy(client, "ok")
    rd = _out(client) / "jobrun"
    rd.mkdir(parents=True)
    created = {"ts": "2999-01-01T00:00:00+00:00", "event": "job_created", "job": "sbmig-jobrun"}
    text = line + "\n" + json.dumps(created) + "\n" + line + "\n" + json.dumps({"event": "graph_node_end", "node": "plan"}) + "\n"
    (rd / "journal.jsonl").write_text(text, encoding="utf-8")
    r = client.get("/api/runs")
    assert r.status_code == 200
    rows = {row["run_id"]: row for row in r.json()["runs"]}
    assert set(rows) == {"ok", "jobrun"}
    assert rows["jobrun"]["state"] == "running" and rows["jobrun"]["job"]["name"] == "sbmig-jobrun"
    assert rows["jobrun"]["node"] == "plan"
    assert client.get("/api/runs/jobrun").status_code == 200


def test_one_row_that_cannot_be_built_is_listed_as_unreadable(client, monkeypatch, caplog) -> None:
    mgr = client.app.state.manager
    _healthy(client, "ok-one")
    _healthy(client, "ok-two")
    _finished(client, "bad", json.dumps({"success": True}))
    real = mgr.status

    def status(rid):
        if rid == "bad":
            raise KeyError("secret-from-a-project-file")
        return real(rid)

    monkeypatch.setattr(mgr, "status", status)
    with caplog.at_level("WARNING", logger="sbmig"):
        r = client.get("/api/runs")
    assert r.status_code == 200
    assert "secret-from-a-project-file" not in r.text
    rows = {row["run_id"]: row for row in r.json()["runs"]}
    assert set(rows) == {"ok-one", "ok-two", "bad"}
    assert rows["ok-one"]["success"] is True and rows["ok-two"]["success"] is True
    started = rows["bad"].pop("started")
    assert isinstance(started, float) and started > 0
    assert rows["bad"] == {"run_id": "bad", "exists": True, "mode": mgr.effective_mode, "flow": "full", "upload": None,
                           "zip_name": "bad-migration.zip", "state": "finished", "exit_code": None}
    warnings = [rec for rec in caplog.records if rec.levelname == "WARNING" and "bad" in rec.getMessage()]
    assert len(warnings) == 1
    assert "KeyError" in warnings[0].getMessage() and "secret-from-a-project-file" not in warnings[0].getMessage()
    assert warnings[0].exc_info is None


def test_the_minimal_row_matches_a_run_whose_files_cannot_be_read(client) -> None:
    mgr = client.app.state.manager
    rd = _out(client) / "unreadable"
    rd.mkdir(parents=True)
    (rd / "report.json").write_text("{bad", encoding="utf-8")
    st = mgr.status("unreadable")
    for k in ("parallel", "files"):
        st.pop(k, None)
    assert mgr._unreadable_row("unreadable") == st
    (rd / "report.json").unlink()
    st = mgr.status("unreadable")
    st.pop("parallel", None)
    assert mgr._unreadable_row("unreadable") == st == {**st, "state": "unknown"}


def test_single_run_status_has_no_net(client, monkeypatch) -> None:
    import sbmig.web.runs as rm

    _healthy(client, "ok")

    def boom(*a, **k):
        raise KeyError("x")

    monkeypatch.setattr(rm, "_abort_reason", boom)
    with pytest.raises(KeyError):
        client.get("/api/runs/ok")


def test_flags_as_a_string_is_not_searched_as_text(client) -> None:
    from sbmig.report.handover import HTTP_BLOCKED_FLAG, HTTP_BLOCKED_PREFIX

    followups = [HTTP_BLOCKED_PREFIX + " at http://repo.example.invalid/ (blocked mirror)"]
    # the same followups with a real flag list do give the abort reason: the string case is the only difference
    assert _report_row(client, "listed", {"success": False, "flags": [HTTP_BLOCKED_FLAG],
                                          "manual_followups": followups})["abort_reason"].startswith(HTTP_BLOCKED_PREFIX)
    row = _report_row(client, "text", {"success": False, "flags": f"aborted {HTTP_BLOCKED_FLAG} other",
                                       "manual_followups": followups})
    assert "abort_reason" not in row and "abort_reason_ko" not in row


def test_a_run_folder_the_server_cannot_enter_is_listed_as_unknown(client, caplog) -> None:
    import os

    _healthy(client, "ok")
    locked = _out(client) / "locked"
    locked.mkdir()
    (locked / "report.json").write_text(json.dumps({"success": True}), encoding="utf-8")
    os.chmod(locked, 0)
    try:
        try:
            (locked / "report.json").is_file()
            os.listdir(locked)
            can_enter = True
        except PermissionError:
            can_enter = False
        if can_enter:
            pytest.skip("this process can enter a mode-000 folder (root?)")
        with caplog.at_level("WARNING", logger="sbmig"):
            r = client.get("/api/runs")
        assert r.status_code == 200
        rows = {row["run_id"]: row for row in r.json()["runs"]}
        assert set(rows) == {"ok", "locked"}
        assert rows["ok"]["state"] == "finished" and rows["ok"]["success"] is True
        started = rows["locked"].pop("started")
        assert isinstance(started, (int, float))
        mgr = client.app.state.manager
        assert rows["locked"] == {"run_id": "locked", "exists": True, "mode": mgr.effective_mode, "flow": "full",
                                  "upload": None, "zip_name": "locked-migration.zip", "state": "unknown",
                                  "exit_code": None}
        warnings = [rec for rec in caplog.records if rec.levelname == "WARNING" and "locked" in rec.getMessage()]
        assert len(warnings) == 1 and "PermissionError" in warnings[0].getMessage()
    finally:
        os.chmod(locked, 0o755)
