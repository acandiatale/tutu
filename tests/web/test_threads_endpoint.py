"""Round 9c: GET /api/threads — the Windows-friendly thread dump."""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from sbmig.web.app import create_app

REPO = Path(__file__).resolve().parents[2]


def test_threads_endpoint(tmp_path):
    app = create_app(tmp_path / "out", REPO)
    with TestClient(app) as client:
        r = client.get("/api/threads")
    assert r.status_code == 200 and "# sbmig thread dump" in r.text and "MainThread" in r.text
    assert list((tmp_path / "out").glob("threads-*.txt"))
