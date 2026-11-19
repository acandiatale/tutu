"""W3 (review 2026-11-18), in-process web: an upload whose ZIP is over `intake.max_*` is refused with HTTP 400 and the reason
sentence (nothing unpacked, no run). In the Job modes the build pod's `sbmig run` ends the run refused (exit 3, report)."""
from __future__ import annotations

import io
import os
import shutil
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from sbmig.core.config import PACKAGE_ROOT
from sbmig.web import create_app
from sbmig.web.app import OPEN_ENV, TOKEN_ENV
from .uploads import runs_url

MIB = 1024 * 1024


def test_an_in_process_upload_over_the_unpacked_limit_is_refused(tmp_path: Path, monkeypatch):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    monkeypatch.delenv(OPEN_ENV, raising=False)
    monkeypatch.setenv("SBMIG_INTAKE__MAX_UNPACKED_MB", "1")
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    shutil.copy(PACKAGE_ROOT / "config" / "artifacts.local-e2e.yml", repo / "config" / "artifacts.local-e2e.yml")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("p/pom.xml", "<project/>")
        z.writestr("p/a.bin", os.urandom(2 * MIB))
    app = create_app(tmp_path / "out", repo, mode="in-process")
    with TestClient(app) as c:
        r = c.post(runs_url("name=p.zip"), content=buf.getvalue())
        assert r.status_code == 400
        assert r.json()["error"] == ("ZIP refused: it unpacks to 2 MiB, over the limit of 1 MiB (intake.max_unpacked_mb) "
                                     "— nothing was unpacked")
        assert c.get("/api/runs").json()["runs"] == []
    assert [p.name for p in (tmp_path / "out").iterdir() if not p.name.startswith("_")] == []
