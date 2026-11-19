"""W4 (review 2026-11-18): `fleet.min_free_gb` is shipped and documented as the free-space floor before a run, but only the
fleet runner read it. The web path honours it now: an upload is refused with HTTP 507 and a JSON `error` (the page shows a
Korean sentence) when the free space of the output volume is under the floor; 0 = off."""
from __future__ import annotations

import io
import shutil
import zipfile
from collections import namedtuple
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sbmig.core.config import PACKAGE_ROOT
from sbmig.web import create_app
from sbmig.web.app import OPEN_ENV, TOKEN_ENV
from .uploads import runs_url

Usage = namedtuple("Usage", "total used free")
GB = 1024 ** 3


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    shutil.copy(PACKAGE_ROOT / "config" / "artifacts.local-e2e.yml", repo / "config" / "artifacts.local-e2e.yml")
    return repo


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    monkeypatch.delenv(OPEN_ENV, raising=False)
    monkeypatch.delenv("SBMIG_FLEET__MIN_FREE_GB", raising=False)


def _zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("pom.xml", "<project/>")
    return buf.getvalue()


def _free(monkeypatch, gb: float) -> None:
    import sbmig.fleet.preflight as pf
    monkeypatch.setattr(pf.shutil, "disk_usage", lambda p: Usage(100 * GB, 100 * GB - int(gb * GB), int(gb * GB)))


def test_an_upload_is_refused_with_507_when_the_output_volume_is_under_the_floor(tmp_path, repo, monkeypatch):
    _free(monkeypatch, 1.5)                          # default.yml: fleet.min_free_gb 5
    app = create_app(tmp_path / "out", repo, mode="in-process")
    with TestClient(app) as c:
        r = c.post(runs_url("name=p.zip"), content=_zip())
        assert r.status_code == 507, r.text
        err = r.json()["error"]
        assert "1.5 GB free" in err and "fleet.min_free_gb" in err and "5" in err
        assert c.get("/api/runs").json()["runs"] == []
    assert not list((tmp_path / "out").glob("_uploads/*.part")) and not list((tmp_path / "out").glob("_uploads/.incoming*"))


def test_enough_space_or_a_zero_floor_lets_the_upload_through(tmp_path, repo, monkeypatch):
    _free(monkeypatch, 1.5)
    monkeypatch.setenv("SBMIG_FLEET__MIN_FREE_GB", "0")
    mgr_app = create_app(tmp_path / "out", repo, mode="in-process")
    with TestClient(mgr_app) as c:
        r = c.post(runs_url("name=p.zip&stop_after=intake"), content=b"not a zip")
        assert r.status_code == 400                  # past the floor check: the zip check speaks
    monkeypatch.delenv("SBMIG_FLEET__MIN_FREE_GB")
    _free(monkeypatch, 50)
    app = create_app(tmp_path / "out2", repo, mode="in-process")
    with TestClient(app) as c:
        assert c.post(runs_url("name=p.zip"), content=b"not a zip").status_code == 400


def test_the_page_has_a_korean_sentence_for_507():
    page = (PACKAGE_ROOT / "sbmig" / "web" / "static" / "index.html").read_text(encoding="utf-8")
    assert "r.status === 507" in page and "저장 공간이 부족" in page
