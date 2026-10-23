"""Web UI serves the error-case files of a run (2026-10-23): listed in the run status when present, downloaded as
attachments with the right content type."""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from sbmig.web import create_app
from sbmig.web.runs import SERVED_FILES

FILES = {"error_cases.csv": ("text/csv", "﻿id,status\ne1,resolved\n"),
         "error_cases.jsonl": ("application/x-ndjson", '{"id": "e1", "message": "오류"}\n'),
         "fix_attempts.jsonl": ("application/x-ndjson", '{"id": "run-1:spring-1-1", "diff": "--- a"}\n')}


def _client(tmp_path: Path) -> TestClient:
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    return TestClient(create_app(tmp_path / "out", repo))


def test_error_case_files_are_served_as_attachments(tmp_path):
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
        assert r.headers["content-disposition"].startswith("attachment") and name in r.headers["content-disposition"]
        assert r.content.decode("utf-8") == body
    # report.md still renders inline (no attachment disposition)
    r = client.get("/api/runs/run-1/files/report.md")
    assert r.status_code == 200 and not r.headers.get("content-disposition", "").startswith("attachment")
