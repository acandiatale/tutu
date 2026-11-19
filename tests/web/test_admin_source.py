"""v2.1z-bb: the admin page hands out the uploaded original (the owner: "scp를 2번을 땡겨와야해서 너무 불편해서").
`GET /admin/api/runs/{id}/source` (+ `/source/check`), `has_source` / `source_bytes` in the usage rows, the page's
「원본 받기」. Hand-written run folders; the real admin password never appears here (the hash is monkeypatched)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from urllib.parse import unquote

import anyio
import pytest
from fastapi.testclient import TestClient

import sbmig.web
from sbmig.core.config import PACKAGE_ROOT
from sbmig.web import admin
from sbmig.web.admin import ADMIN_PASSWORD_ENV, ZIP_CHUNK
from sbmig.web.app import OPEN_ENV, TOKEN_ENV, create_app

TEST_PASSWORD = "test-pass"
SALT = "00112233445566778899aabbccddeeff"
ITER = 1000
JOB_RID = "my-proj-20261119-101010-aaaaaa"
INPROC_RID = "web-20261119-111111-bbbbbb"
REM_RID = "rem-only-20261119-121212-cccccc"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    monkeypatch.delenv(OPEN_ENV, raising=False)
    monkeypatch.delenv(ADMIN_PASSWORD_ENV, raising=False)
    digest = hashlib.pbkdf2_hmac("sha256", TEST_PASSWORD.encode(), bytes.fromhex(SALT), ITER).hex()
    monkeypatch.setattr(admin, "ADMIN_PASSWORD_PBKDF2", {"iterations": ITER, "salt_hex": SALT, "hash_hex": digest})


@pytest.fixture
def out(tmp_path: Path) -> Path:
    return tmp_path / "out"


@pytest.fixture
def client(tmp_path: Path, out: Path) -> TestClient:
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    shutil.copy(PACKAGE_ROOT / "config" / "artifacts.local-e2e.yml", repo / "config" / "artifacts.local-e2e.yml")
    app = create_app(out, repo)
    app.state.admin_auth.fail_delay_s = 0
    c = TestClient(app)
    assert c.post("/admin/api/login", json={"password": TEST_PASSWORD}).status_code == 200
    return c


def _w(p: Path, data: bytes | str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        p.write_text(data, encoding="utf-8")
    else:
        p.write_bytes(data)
    return p


def _job_run(out: Path, rid: str = JOB_RID, upload: str | None = "my proj.zip", body: bytes = b"PK-job-upload") -> Path:
    rd = out / rid
    rec = {"run_id": rid, "accepted_at": "2026-11-19T01:00:00+00:00", "configs": [], "zip_sha256": "0" * 64,
           "config_digests": {}, "sbmig_version": "x", "mode": "k8s-job", "flow": "full"}
    if upload is not None:
        rec.update({"upload": upload.replace(" ", "_"), "upload_name": upload})
    _w(rd / "control" / "request.json", json.dumps(rec))
    _w(rd / "source.zip", body)
    return rd


def _inproc_run(out: Path, rid: str = INPROC_RID, upload: str = "프로젝트 소스.zip", body: bytes = b"PK-inproc",
                flow: str = "full", stored: str | None = None) -> Path:
    rd = out / rid
    _w(rd / "web-status.json", json.dumps({"run_id": rid, "exit_code": 0, "flow": flow, "upload": upload,
                                           "success": True}))
    _w(rd / "report.json", json.dumps({"success": True, "flow": flow}))
    # RunManager.start_file: _uploads/<run id>-<name with [^A-Za-z0-9._-] → _>
    name = stored or f"{rid}-" + re.sub(r"[^A-Za-z0-9._-]+", "_", upload)
    _w(out / "_uploads" / name, body)
    return rd


# ---- the route

def test_no_session_gets_the_same_answer_as_the_other_admin_routes(tmp_path, out, client) -> None:
    _job_run(out)
    anon = TestClient(client.app)
    for path in (f"/admin/api/runs/{JOB_RID}/source", f"/admin/api/runs/{JOB_RID}/source/check",
                 f"/admin/api/runs/{JOB_RID}/logs.zip"):
        r = anon.get(path)
        assert r.status_code == 401 and r.json() == {"error": "login_required"}, path


def test_bad_unknown_and_missing(out, client) -> None:
    assert client.get("/admin/api/runs/bad$id/source").status_code == 400
    assert client.get("/admin/api/runs/bad$id/source").json() == {"error": "bad run id"}
    r = client.get("/admin/api/runs/nope-20261119-000000-ffffff/source")
    assert r.status_code == 404 and r.json() == {"error": "unknown run"}
    rd = _job_run(out)
    (rd / "source.zip").unlink()                              # cleaned up after the run
    for tail in ("source", "source/check"):
        r = client.get(f"/admin/api/runs/{JOB_RID}/{tail}")
        assert r.status_code == 404 and r.json() == {"error": "source not available"}


def test_job_mode_upload_and_ascii_name(out, client) -> None:
    body = os.urandom(5000)
    _job_run(out, body=body)
    r = client.get(f"/admin/api/runs/{JOB_RID}/source")
    assert r.status_code == 200 and r.content == body
    assert r.headers["content-type"] == "application/zip"
    assert r.headers["content-length"] == str(len(body))
    assert r.headers["content-disposition"] == f'attachment; filename="{JOB_RID}-my proj.zip"'
    check = client.get(f"/admin/api/runs/{JOB_RID}/source/check").json()
    assert check == {"ok": True, "bytes": len(body), "filename": f"{JOB_RID}-my proj.zip"}


def test_in_process_upload_and_korean_name(out, client) -> None:
    _inproc_run(out)
    _w(out / "_uploads" / f"{INPROC_RID}-decoy.zip", b"DECOY")   # the recorded name decides, not the folder listing
    r = client.get(f"/admin/api/runs/{INPROC_RID}/source")
    assert r.status_code == 200 and r.content == b"PK-inproc"
    cd = r.headers["content-disposition"]
    assert cd.startswith(f'attachment; filename="{INPROC_RID}-')
    star = cd.split("filename*=UTF-8''", 1)[1]
    assert unquote(star) == f"{INPROC_RID}-프로젝트 소스.zip"
    assert cd.split('filename="', 1)[1].split('"', 1)[0].isascii()


def test_unknown_name_falls_back_to_source_zip(out, client) -> None:
    _job_run(out, upload=None)
    r = client.get(f"/admin/api/runs/{JOB_RID}/source")
    assert r.headers["content-disposition"] == f'attachment; filename="{JOB_RID}-source.zip"'


def test_in_process_upload_found_by_prefix_when_the_name_differs(out, client) -> None:
    _inproc_run(out, stored=f"{INPROC_RID}-renamed_upload.zip")
    assert client.get(f"/admin/api/runs/{INPROC_RID}/source").content == b"PK-inproc"


def test_remediation_only_flow_uses_its_copy_only_for_that_flow(out, client) -> None:
    rd = _inproc_run(out, rid=REM_RID, upload="rem.zip", flow="remediation")
    (out / "_uploads" / f"{REM_RID}-rem.zip").unlink()        # the upload folder was cleaned; the flow kept a copy
    _w(rd / "remediation" / "original" / "source.zip", b"PK-rem-copy")
    assert client.get(f"/admin/api/runs/{REM_RID}/source").content == b"PK-rem-copy"
    # a full run's remediation/original holds the migrated tree, not the upload: never handed out as the original
    full = _inproc_run(out, upload="x.zip", flow="full")
    (out / "_uploads" / f"{INPROC_RID}-x.zip").unlink()
    _w(full / "remediation" / "original" / "source.zip", b"PK-migrated-tree")
    assert client.get(f"/admin/api/runs/{INPROC_RID}/source").status_code == 404


def test_job_source_wins_over_an_in_process_copy(out, client) -> None:
    rd = _job_run(out, body=b"PK-job")
    _w(out / "_uploads" / f"{JOB_RID}-my_proj.zip", b"PK-other")
    _w(rd / "remediation" / "original" / "source.zip", b"PK-third")
    assert client.get(f"/admin/api/runs/{JOB_RID}/source").content == b"PK-job"


def test_large_file_is_streamed_in_chunks(out, client, monkeypatch) -> None:
    body = os.urandom(ZIP_CHUNK * 5 + 123)
    _job_run(out, body=body)
    reads: list[int] = []
    real_open = anyio.open_file

    async def spy_open(*a, **k):
        fh = await real_open(*a, **k)
        real_read = fh.read

        async def read(n=-1):
            reads.append(n)
            return await real_read(n)
        fh.read = read
        return fh
    monkeypatch.setattr(admin.anyio, "open_file", spy_open)
    r = client.get(f"/admin/api/runs/{JOB_RID}/source")
    assert r.content == body
    # never one read of the whole file: every read asks for at most one chunk
    assert reads and all(0 < n <= ZIP_CHUNK for n in reads) and len(reads) >= 6


def test_nothing_outside_the_output_root(tmp_path, out, client) -> None:
    secret = _w(tmp_path / "secret.zip", b"SECRET")
    # a recorded upload name that tries to climb out
    _inproc_run(out, upload="../../secret.zip", stored=f"{INPROC_RID}-unused.zip")
    (out / "_uploads" / f"{INPROC_RID}-unused.zip").unlink()
    r = client.get(f"/admin/api/runs/{INPROC_RID}/source")
    assert r.status_code == 404 and secret.read_bytes() == b"SECRET"
    # a run id that tries to climb out
    for bad in ("..%2F..%2Fsecret", "..", "a/../../secret"):
        assert client.get(f"/admin/api/runs/{bad}/source").status_code in (400, 404), bad


def test_a_symbolic_link_is_never_followed(tmp_path, out, client) -> None:
    target = _w(tmp_path / "elsewhere.zip", b"ELSEWHERE")
    rd = _job_run(out)
    (rd / "source.zip").unlink()
    (rd / "source.zip").symlink_to(target)
    assert client.get(f"/admin/api/runs/{JOB_RID}/source").status_code == 404
    # nor a linked upload folder
    _inproc_run(out)
    shutil.move(str(out / "_uploads"), str(tmp_path / "real_uploads"))
    (out / "_uploads").symlink_to(tmp_path / "real_uploads", target_is_directory=True)
    assert client.get(f"/admin/api/runs/{INPROC_RID}/source").status_code == 404


def test_usage_rows_say_whether_the_source_is_there(out, client) -> None:
    _job_run(out, body=b"x" * 1234)
    gone = _job_run(out, rid="gone-20261119-131313-dddddd")
    (gone / "source.zip").unlink()
    _inproc_run(out)
    rows = {r["run_id"]: r for p in client.get("/admin/api/usage").json()["projects"] for r in p["run_list"]}
    assert (rows[JOB_RID]["has_source"], rows[JOB_RID]["source_bytes"]) == (True, 1234)
    assert (rows["gone-20261119-131313-dddddd"]["has_source"], rows["gone-20261119-131313-dddddd"]["source_bytes"]) \
        == (False, None)
    assert rows[INPROC_RID]["has_source"] is True and rows[INPROC_RID]["source_bytes"] == len(b"PK-inproc")


def test_one_info_line_per_download_without_client_data(out, client, caplog) -> None:
    _job_run(out, body=b"PK12345")
    with caplog.at_level("INFO", logger="sbmig.web.admin"):
        client.get(f"/admin/api/runs/{JOB_RID}/source")
    lines = [r.getMessage() for r in caplog.records if "source download" in r.getMessage()]
    assert lines == [f"admin source download: run {JOB_RID}, 7 bytes"]


# ---- the page

PAGE = (Path(sbmig.web.__file__).parent / "static" / "admin.html").read_text(encoding="utf-8")
PUBLIC = (Path(sbmig.web.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")


def test_admin_page_offers_the_original() -> None:
    assert "<th scope=\"col\">원본</th>" in PAGE
    assert "${API}/runs/${encodeURIComponent(id)}/source\" data-src=\"1\"" in PAGE and ">원본 받기</a>" in PAGE
    assert "esc(fmtSize(r.source_bytes))" in PAGE and "function fmtSize(v)" in PAGE
    # gone: the same place, disabled, with the reason
    assert 'aria-disabled="true" title="원본 파일이 서버에 없습니다"' in PAGE and "r.has_source === true" in PAGE
    # the pre-check, like the log ZIP: a message instead of a broken download
    assert "fetch(href + '/check'" in PAGE and "d.error === 'source not available'" in PAGE
    assert "colspan=\"${withCost ? 11 : 10}\"" in PAGE
    # the public page gets nothing
    assert "/source" not in PUBLIC and "원본 받기" not in PUBLIC
