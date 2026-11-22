"""v2.1z-bs rule 6, the web server's part.

- `GET /api/runs/{id}` does not answer 500 for a `report.json` that holds a non-finite number (`NaN`, `Infinity`,
  `-Infinity`: Python's json writes and reads them): the value is sent as `null`, as the run list already sends it.
  For finite values the body bytes are exactly those of Starlette's `JSONResponse` (what the route sent before).
  The admin page's usage answer, the answer of `POST /api/runs`, the code-change routes and the events of the run's
  event stream take the same rule. A payload without such a value is rendered once, exactly as before: the walk
  over the payload runs only when the plain rendering refuses a non-finite number.
- `RunManager.status` reads `report.json` once per call, with and without a request record."""
from __future__ import annotations

import hashlib
import io
import json
import math
import shutil
import zipfile
from pathlib import Path

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from sbmig.core.config import PACKAGE_ROOT
from sbmig.web import admin, codediff, create_app, jsonsafe
from sbmig.web.app import _sse
from sbmig.web.contract import ControlState, RequestRecord, RunState
from sbmig.web.jsonsafe import FiniteJSONResponse, finite_dumps, finite_json
from sbmig.web.runs import RunManager

from .uploads import runs_url

NON_FINITE = {"NaN": float("nan"), "Infinity": float("inf"), "-Infinity": float("-inf")}


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    shutil.copy(PACKAGE_ROOT / "config" / "artifacts.local-e2e.yml", repo / "config" / "artifacts.local-e2e.yml")
    return TestClient(create_app(tmp_path / "out", repo), raise_server_exceptions=False)


def _out(client: TestClient) -> Path:
    return Path(client.app.state.manager.out_dir)


def _finished(client: TestClient, rid: str, report: dict, *, web_flow: bool = True) -> Path:
    rd = _out(client) / rid
    rd.mkdir(parents=True)
    # json.dumps writes NaN / Infinity / -Infinity literally (allow_nan defaults to True), as the engine would
    (rd / "report.json").write_text(json.dumps(report), encoding="utf-8")
    web = {"run_id": rid, "exit_code": 0, "upload": f"{rid}.zip", "stop_requested": False}
    if web_flow:
        web["flow"] = "full"
    (rd / "web-status.json").write_text(json.dumps(web), encoding="utf-8")
    return rd


def _odd_report(value: float) -> dict:
    return {"success": value, "blocked": [], "unresolved": [], "flow": "full",
            "remediation": {"status": "SUCCEEDED", "reason": value, "missing": [value, 1.5]}}


# ---------------------------------------------------------------------------------------------- non-finite numbers
@pytest.mark.parametrize("name", list(NON_FINITE))
def test_single_run_status_sends_a_non_finite_report_number_as_null(client, name) -> None:
    rd = _finished(client, "odd", _odd_report(NON_FINITE[name]))
    assert name in (rd / "report.json").read_text(encoding="utf-8")      # the literal is really in the file
    r = client.get("/api/runs/odd")
    assert r.status_code == 200
    assert name not in r.text
    st = json.loads(r.text)                     # strict enough: the body is standard JSON
    assert st["success"] is None and st["state"] == "finished"
    assert st["remediation"]["status"] == "SUCCEEDED"
    # a key whose value was non-finite is null, not dropped (the dict is filtered with `is not None` before that)
    assert st["remediation"]["reason"] is None
    assert st["remediation"]["missing"] == [None, 1.5]


def test_run_list_sends_non_finite_report_numbers_as_null(client) -> None:
    for i, value in enumerate(NON_FINITE.values()):
        _finished(client, f"odd-{i}", _odd_report(value))
    _finished(client, "ok", {"success": True, "blocked": [], "unresolved": [], "flow": "full"})
    r = client.get("/api/runs")
    assert r.status_code == 200
    for literal in NON_FINITE:
        assert literal not in r.text
    rows = {row["run_id"]: row for row in r.json()["runs"]}
    assert set(rows) == {"odd-0", "odd-1", "odd-2", "ok"}
    assert all(rows[f"odd-{i}"]["success"] is None for i in range(3))
    assert rows["ok"]["success"] is True


def test_a_healthy_run_answers_the_same_bytes_as_a_plain_json_response(client) -> None:
    report = {"success": True, "blocked": [{"x": 1}], "unresolved": [], "flow": "full",
              "llm_usage": {"calls": 3, "input_tokens": 10, "output_tokens": 2},
              "error_entries": [{"status": "fixed"}, {"status": "open"}],
              "remediation": {"status": "SUCCEEDED", "reason": "done 한글", "missing": [1.5, 2, "a"],
                              "baseline_tolerated": False, "reasons": ["r1"]},
              "profile": {"framework": {"scope": "jdk-jakarta", "name": "Example", "fix_loop": False}},
              "manual_followups": ["run aborted: something 한글"]}
    _finished(client, "healthy", report)
    r = client.get("/api/runs/healthy")
    assert r.status_code == 200
    st = client.app.state.manager.status("healthy")
    st.pop("parallel")
    got = r.json()
    got.pop("parallel")
    assert got == st
    # the route used to render the same dict with Starlette's JSONResponse: same bytes, byte for byte
    assert r.content == JSONResponse(r.json()).body
    for content in (r.json(), report, {"f": [0.1, -2.5, 1e300, 0.0, -0.0], "i": [0, -1, 2**63], "t": (1, 2.5)}):
        assert FiniteJSONResponse(content).body == JSONResponse(content).body


def test_finite_json_replaces_only_non_finite_floats() -> None:
    src = {"a": [float("nan"), {"b": float("inf"), "c": (float("-inf"), 2.0)}], "d": 1, "e": True, "f": None, "g": "NaN"}
    assert finite_json(src) == {"a": [None, {"b": None, "c": [None, 2.0]}], "d": 1, "e": True, "f": None, "g": "NaN"}
    assert list(finite_json({"z": 1, "a": 2})) == ["z", "a"]
    assert not any(isinstance(v, float) and not math.isfinite(v) for v in finite_json([float("nan")]))


def test_admin_usage_sends_a_non_finite_cost_as_null(client, monkeypatch) -> None:
    salt = "00112233445566778899aabbccddeeff"
    pw = "test-pass"
    monkeypatch.setattr(admin, "ADMIN_PASSWORD_PBKDF2", {"iterations": 1000, "salt_hex": salt, "hash_hex": hashlib.pbkdf2_hmac(
        "sha256", pw.encode(), bytes.fromhex(salt), 1000).hex()})
    monkeypatch.delenv(admin.ADMIN_PASSWORD_ENV, raising=False)
    client.app.state.admin_auth.fail_delay_s = 0
    _finished(client, "costly", {"success": True, "project_name": "demo",
                                 "llm_usage": {"calls": 1, "input_tokens": 1, "output_tokens": 1},
                                 "llm_cost": {"currency": "USD", "total": float("nan"),
                                              "by_model": [{"model": "m", "calls": 1, "cost": float("inf")}]}})
    assert client.post("/admin/api/login", json={"password": pw}).status_code == 200
    r = client.get("/admin/api/usage")
    assert r.status_code == 200
    assert "NaN" not in r.text and "Infinity" not in r.text
    json.loads(r.text)


# ------------------------------------------------------------------------------------------ report.json read once
def _count_report_reads(monkeypatch) -> list[Path]:
    seen: list[Path] = []
    real = Path.read_text

    def read_text(self, *a, **k):
        if self.name == "report.json":
            seen.append(self)
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", read_text)
    return seen


def test_status_reads_the_report_once_without_a_request_record(client, monkeypatch) -> None:
    # no request.json and no flow in web-status.json: the flow comes from report.json, then the report's fields
    _finished(client, "inproc", {"success": True, "blocked": [], "unresolved": [], "flow": "remediation"},
              web_flow=False)
    mgr = client.app.state.manager
    seen = _count_report_reads(monkeypatch)
    st = mgr.status("inproc")
    assert len(seen) == 1
    assert st["flow"] == "remediation" and st["success"] is True and st["state"] == "finished"


def test_status_reads_the_report_once_with_a_request_record(client, monkeypatch) -> None:
    rd = _out(client) / "jobrun"
    rd.mkdir(parents=True)
    RequestRecord(run_id="jobrun", accepted_at="2026-01-01T00:00:00+00:00", upload="u.zip", configs=["c.yml"],
                  zip_sha256="0" * 64, config_digests={}, sbmig_version="test", mode="k8s-job", flow="full",
                  upload_name="Upload Name.zip", project="proj").write(rd)
    ControlState(run_id="jobrun", state=RunState.FINISHED.value, exit_code=0, job="sbmig-jobrun").write(rd)
    (rd / "report.json").write_text(json.dumps({"success": False, "blocked": [1], "unresolved": [], "flow": "x"}),
                                    encoding="utf-8")
    mgr = client.app.state.manager
    seen = _count_report_reads(monkeypatch)
    st = mgr.status("jobrun")
    assert len(seen) == 1
    assert (st["flow"], st["success"], st["blocked"], st["state"]) == ("full", False, 1, "finished")


@pytest.mark.parametrize("text", [None, "{bad", "[]", "null"])
def test_status_without_a_readable_report_object_reads_at_most_once(client, monkeypatch, text) -> None:
    rd = _out(client) / "plain"
    rd.mkdir(parents=True)
    if text is not None:
        (rd / "report.json").write_text(text, encoding="utf-8")
    (rd / "web-status.json").write_text(json.dumps({"run_id": "plain", "exit_code": 0}), encoding="utf-8")
    mgr = client.app.state.manager
    seen = _count_report_reads(monkeypatch)
    st = mgr.status("plain")
    assert len(seen) == (0 if text is None else 1)
    assert st["flow"] == "full" and "success" not in st and st["state"] == "finished"


# ------------------------------------------------------------------------------- the walk runs only when needed
def _no_walk(monkeypatch) -> list[int]:
    calls: list[int] = []
    real = jsonsafe.finite_json

    def counted(value):
        calls.append(1)
        return real(value)

    monkeypatch.setattr(jsonsafe, "finite_json", counted)
    return calls


def test_a_finite_payload_is_rendered_without_the_walk(client, monkeypatch) -> None:
    _finished(client, "healthy", {"success": True, "blocked": [], "unresolved": [], "flow": "full",
                                  "remediation": {"status": "SUCCEEDED", "missing": [1.5, 2]}})
    calls = _no_walk(monkeypatch)
    payload = {"f": [0.1, -2.5, 1e300], "s": "NaN", "n": None, "k": {"x": (1, 2.5)}}
    assert FiniteJSONResponse(payload).body == JSONResponse(payload).body
    assert finite_dumps(payload, ensure_ascii=False, default=str) == json.dumps(payload, ensure_ascii=False, default=str)
    assert client.get("/api/runs/healthy").status_code == 200
    assert calls == []
    # a non-finite value: the walk runs (it recurses, so it may count more than once), then the converted rendering
    assert FiniteJSONResponse({"x": float("nan")}).body == b'{"x":null}'
    assert calls


def test_another_value_error_is_not_turned_into_a_walk(monkeypatch) -> None:
    calls = _no_walk(monkeypatch)
    loop: dict = {}
    loop["self"] = loop
    with pytest.raises(ValueError, match="Circular"):
        FiniteJSONResponse(loop)
    with pytest.raises(ValueError, match="Circular"):
        finite_dumps(loop)
    assert calls == []


# ------------------------------------------------------------------------------------------------- event stream
def _strict(text: str):
    def refuse(word: str):
        raise ValueError(f"non-standard JSON constant {word}")
    return json.loads(text, parse_constant=refuse)


def _stream(client: TestClient, rid: str) -> list[tuple[str, str]]:
    events, event = [], None
    with client.stream("GET", f"/api/runs/{rid}/events") as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                events.append((event, line[6:]))
                if event == "end":
                    break
    return events


@pytest.mark.parametrize("name", list(NON_FINITE))
def test_the_status_event_sends_a_non_finite_report_number_as_null(client, name) -> None:
    _finished(client, "odd", _odd_report(NON_FINITE[name]))
    events = _stream(client, "odd")
    statuses = [data for event, data in events if event == "status"]
    assert statuses
    for data in statuses:
        assert name not in data
        st = _strict(data)                      # what a browser's JSON.parse accepts
        assert st["success"] is None and st["remediation"]["missing"] == [None, 1.5]
    for _event, data in events:
        _strict(data)


def test_a_journal_line_with_a_non_finite_number_is_sent_as_null(client) -> None:
    rd = _finished(client, "odd", {"success": True, "blocked": [], "unresolved": [], "flow": "full"})
    (rd / "journal.jsonl").write_text(json.dumps({"event": "llm_cost", "cost": float("nan")}) + "\n", encoding="utf-8")
    journal = [data for event, data in _stream(client, "odd") if event == "journal"]
    assert journal and _strict(journal[0]) == {"event": "llm_cost", "cost": None}


def test_a_healthy_runs_events_are_the_bytes_sent_before(client) -> None:
    rd = _finished(client, "healthy", {"success": True, "blocked": [], "unresolved": [], "flow": "full",
                                       "remediation": {"status": "SUCCEEDED", "reason": "끝 한글", "missing": [1.5]}})
    (rd / "journal.jsonl").write_text(json.dumps({"event": "build", "ok": True, "t": 0.25, "msg": "빌드"}) + "\n",
                                      encoding="utf-8")
    (rd / "report.md").write_text("# 보고서\n", encoding="utf-8")
    events = _stream(client, "healthy")
    assert {e for e, _ in events} >= {"status", "journal", "report", "end"}
    for event, data in events:
        value = json.loads(data)
        before = f"event: {event}\ndata: {json.dumps(value, ensure_ascii=False, default=str)}\n\n".encode()
        assert _sse(event, value) == before
        assert data.encode() == before.split(b"data: ", 1)[1].rstrip(b"\n")


# ------------------------------------------------------------------------------------------- code-change routes
def _codediff_run(client: TestClient, rid: str, number: float) -> None:
    rd = _out(client) / rid
    d = rd / "codediff"
    d.mkdir(parents=True)
    entry = {"id": 0, "path": "a/A.java", "old_path": None, "status": "added", "changed": True, "binary": False,
             "added": 1, "removed": 0, "encoding": "utf-8", "patch": None, "omitted": "cap", "score": number}
    summary = {"files": 1, "ratio": number}
    head = {"version": 1, "run_id": rid, "generated_at": "2026-11-18T00:00:00Z", "base": "0" * 40, "context": 10,
            "zip_root": "project", "summary": summary, "moves": [], "moves_total": 0}
    (d / "index.json").write_text(json.dumps({**head, "files": [entry], "files_total": 1}), encoding="utf-8")
    (d / "summary.json").write_text(json.dumps(head), encoding="utf-8")
    (d / "patch.bin").write_bytes(b"")
    with zipfile.ZipFile(rd / "result.zip", "w") as zf:
        zf.writestr("project/a/A.java", "x\n")


@pytest.mark.parametrize("name", list(NON_FINITE))
def test_code_change_routes_send_a_non_finite_index_number_as_null(client, name) -> None:
    codediff.clear_caches()
    _codediff_run(client, "cd", NON_FINITE[name])
    try:
        r = client.get("/api/runs/cd/codediff")
        assert r.status_code == 200 and name not in r.text
        idx = _strict(r.text)
        assert idx["summary"]["ratio"] is None and idx["files"][0]["score"] is None
        r = client.get("/api/runs/cd/codediff/files/0")
        assert r.status_code == 200 and name not in r.text
        _strict(r.text)
        r = client.get("/api/runs/cd/codediff/files/0/lines")
        assert r.status_code == 200
        assert _strict(r.text)["lines"] == ["x"]
    finally:
        codediff.clear_caches()


def test_code_change_index_with_finite_numbers_is_the_plain_rendering(client) -> None:
    codediff.clear_caches()
    _codediff_run(client, "cd", 0.5)
    try:
        r = client.get("/api/runs/cd/codediff")
        assert r.status_code == 200 and r.content == JSONResponse(r.json()).body
    finally:
        codediff.clear_caches()


# ------------------------------------------------------------------------------------------------ POST /api/runs
def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("proj/pom.xml", "<project/>")
    return buf.getvalue()


def _post_client(tmp_path: Path, monkeypatch, success) -> TestClient:
    """An in-process server whose run writes its report at once, and whose upload answer is built only after the run
    finished: the one way the answer of POST /api/runs carries report.json (a run that ended before `status()`)."""
    def body(self, run):
        (run.run_dir / "report.json").write_text(json.dumps({"success": success, "blocked": [], "unresolved": []}),
                                                 encoding="utf-8")
        from types import SimpleNamespace
        return SimpleNamespace(success=True, blocked=[], flags=[])

    real_start = RunManager.start_file

    def start_file(self, *a, **k):
        rid = real_start(self, *a, **k)
        assert self.runs[rid].done.wait(10)
        return rid

    monkeypatch.setattr(RunManager, "_run_thread_body", body)
    monkeypatch.setattr(RunManager, "start_file", start_file)
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    shutil.copy(PACKAGE_ROOT / "config" / "artifacts.local-e2e.yml", repo / "config" / "artifacts.local-e2e.yml")
    return TestClient(create_app(tmp_path / "out", repo))


def _post(client: TestClient):
    r = client.post(runs_url("name=p.zip&configs=artifacts.local-e2e.yml"), content=_zip_bytes(),
                    headers={"Content-Type": "application/zip"})
    assert r.status_code == 202, r.text
    return r


def test_upload_answer_sends_a_non_finite_report_number_as_null(tmp_path, monkeypatch) -> None:
    r = _post(_post_client(tmp_path, monkeypatch, float("nan")))
    assert "NaN" not in r.text
    st = _strict(r.text)["status"]
    assert st["state"] == "finished" and st["success"] is None


def test_upload_answer_of_a_healthy_run_is_the_plain_rendering(tmp_path, monkeypatch) -> None:
    r = _post(_post_client(tmp_path, monkeypatch, True))
    body = r.json()
    assert set(body) == {"run_id", "status"} and body["status"]["success"] is True
    assert r.content == JSONResponse(body).body
