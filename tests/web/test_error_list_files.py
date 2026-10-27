"""Web UI serves the run's error list (v2.1): `errors.jsonl` as an attachment, `errors.md` inline; the run status
counts open / resolved entries from report.json; the old error-case files are no longer served."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from sbmig.web import create_app
from sbmig.web.runs import SERVED_FILES, _event_time

FILES = {"errors.jsonl": ("application/x-ndjson", '{"id": "e1", "status": "resolved", "message": "오류"}\n'),
         "errors.md": ("text/markdown", "# Error list\n\n| # | case |\n")}


def _client(tmp_path: Path) -> TestClient:
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    return TestClient(create_app(tmp_path / "out", repo))


def test_error_list_files_are_served(tmp_path):
    client = _client(tmp_path)
    rd = tmp_path / "out" / "run-1"
    rd.mkdir(parents=True)
    (rd / "report.md").write_text("# report\n", encoding="utf-8")
    st = client.get("/api/runs/run-1").json()
    assert st["exists"] and not set(FILES) & set(st.get("files", []))            # listed only when present
    for name in FILES:
        assert client.get(f"/api/runs/run-1/files/{name}").status_code == 404
    for name, (_, body) in FILES.items():
        (rd / name).write_text(body, encoding="utf-8")
    st = client.get("/api/runs/run-1").json()
    assert set(FILES) <= set(st["files"]) and "report.md" in st["files"]
    for name, (ctype, body) in FILES.items():
        assert SERVED_FILES[name].startswith(ctype) and "charset=utf-8" in SERVED_FILES[name]
        r = client.get(f"/api/runs/run-1/files/{name}")
        assert r.status_code == 200, name
        assert r.headers["content-type"].startswith(ctype) and "charset=utf-8" in r.headers["content-type"]
        assert r.content.decode("utf-8") == body
    assert client.get("/api/runs/run-1/files/errors.jsonl").headers["content-disposition"].startswith("attachment")
    assert not client.get("/api/runs/run-1/files/errors.md").headers.get(
        "content-disposition", "").startswith("attachment")
    for gone in ("error_cases.csv", "error_cases.jsonl", "fix_attempts.jsonl"):
        assert gone not in SERVED_FILES
        (rd / gone).write_text("x", encoding="utf-8")
        assert client.get(f"/api/runs/run-1/files/{gone}").status_code == 404


def test_status_counts_open_and_resolved(tmp_path):
    client = _client(tmp_path)
    rd = tmp_path / "out" / "run-2"
    rd.mkdir(parents=True)
    (rd / "report.json").write_text(json.dumps({"success": False, "blocked": [], "unresolved": [],
                                                "error_entries": [{"status": "unresolved"}, {"status": "open"},
                                                                  {"status": "resolved"}, {"status": "pom-verify"}]}),
                                    encoding="utf-8")
    st = client.get("/api/runs/run-2").json()
    assert st["open"] == 2 and st["resolved"] == 1


def test_log_lines_with_stage_are_timed():
    """sbmig.log lines carry `[stage]` (v2.1) and keep the millisecond timestamp the stream is ordered by."""
    a = _event_time("log", "2026-10-27 10:12:03,120 INFO    [fix r1 7a52b1cea8] sbmig.loop: KB 탐색: 저장된 스크립트 없음")
    b = _event_time("log", "2026-10-27 10:12:03,450 INFO    [compile] sbmig.loop: x")
    assert a is not None and b is not None and round(b - a, 3) == 0.33
    assert _event_time("log", "2026-10-27 10:12:03 INFO    [-] sbmig.cli: no millis") is not None
    assert _event_time("log", "    continuation line") is None
