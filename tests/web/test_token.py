"""Round 10: the web UI hands out company source (result.zip) and runs Maven on uploaded poms — a token is required
off loopback (`SBMIG_WEB_TOKEN` / `--token`), loopback stays tokenless, `sbmig serve` refuses 0.0.0.0 without one."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from sbmig.core.config import PACKAGE_ROOT
from sbmig.web import create_app
from sbmig.web.app import TOKEN_COOKIE, TOKEN_ENV, is_loopback_host

TOKEN = "s3cret-token-for-tests"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    shutil.copy(PACKAGE_ROOT / "config" / "artifacts.local-e2e.yml", repo / "config" / "artifacts.local-e2e.yml")
    return repo


@pytest.fixture(autouse=True)
def _no_env_token(monkeypatch):
    monkeypatch.delenv(TOKEN_ENV, raising=False)


def test_loopback_hosts():
    assert is_loopback_host("127.0.0.1") and is_loopback_host("localhost") and is_loopback_host("::1")
    assert is_loopback_host("[::1]") and is_loopback_host("127.0.1.1")
    assert not is_loopback_host("0.0.0.0") and not is_loopback_host("::") and not is_loopback_host("10.1.2.3")
    assert not is_loopback_host("migration-pc")


def test_without_a_token_only_loopback_clients_are_served(tmp_path, repo):
    app = create_app(tmp_path / "out", repo)
    assert TestClient(app, client=("127.0.0.1", 5000)).get("/api/configs").status_code == 200
    assert TestClient(app).get("/api/runs").status_code == 200                 # in-process client: local
    lan = TestClient(app, client=("10.20.30.40", 5000))
    r = lan.get("/api/runs")
    assert r.status_code == 403 and TOKEN_ENV in r.json()["error"]
    page = lan.get("/")
    assert page.status_code == 403 and "text/html" in page.headers["content-type"] and TOKEN_ENV in page.text


def test_token_required_bearer_cookie_and_query(tmp_path, repo):
    app = create_app(tmp_path / "out", repo, token=TOKEN)
    anon = TestClient(app, client=("10.20.30.40", 5000))
    for path in ("/api/runs", "/api/configs", "/api/threads", "/api/runs/x/files/result.zip", "/static/index.html"):
        r = anon.get(path)
        assert r.status_code == 401, path
    assert anon.get("/").status_code == 401 and "?token=" in anon.get("/").text
    assert anon.get("/api/runs", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert anon.get("/api/runs?token=wrong").status_code == 401
    # API clients: the bearer header
    assert anon.get("/api/runs", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    # browsers: /?token= once → 303 to / with an HttpOnly, SameSite=Strict cookie; afterwards the cookie is enough
    browser = TestClient(app, client=("10.20.30.40", 5001), follow_redirects=False)
    r = browser.get(f"/?token={TOKEN}")
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{TOKEN_COOKIE}=") and "HttpOnly" in cookie and "samesite=strict" in cookie.lower()
    assert TOKEN not in cookie                                                  # the cookie never carries the token
    browser.cookies.set(TOKEN_COOKIE, cookie.split(";")[0].split("=", 1)[1])
    assert browser.get("/").status_code == 200 and browser.get("/api/runs").status_code == 200
    # an API call with ?token= works too and leaves the cookie behind
    api = TestClient(app, client=("10.20.30.40", 5002))
    r = api.get(f"/api/configs?token={TOKEN}")
    assert r.status_code == 200 and TOKEN_COOKIE in r.headers.get("set-cookie", "")
    # loopback needs the token as well once one is configured (the server cannot tell a proxy from a browser)
    assert TestClient(app, client=("127.0.0.1", 5003)).get("/api/runs").status_code == 401


def test_env_token_is_used(tmp_path, repo, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    app = create_app(tmp_path / "out", repo)
    assert TestClient(app).get("/api/runs").status_code == 401
    assert TestClient(app).get("/api/runs", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_serve_refuses_a_lan_address_without_a_token(monkeypatch, tmp_path):
    import uvicorn

    from sbmig.cli import app

    started = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: started.append(kw))
    monkeypatch.setenv("SBMIG_ENV_FILE", str(tmp_path / "none.env"))
    r = CliRunner().invoke(app, ["serve", "--host", "0.0.0.0", "--out", str(tmp_path / "web")])
    assert r.exit_code == 2 and started == []
    out = r.output + (r.stderr if r.stderr_bytes is not None else "")
    assert "refusing to serve on 0.0.0.0 without a token" in out and TOKEN_ENV in out
    r = CliRunner().invoke(app, ["serve", "--host", "0.0.0.0", "--out", str(tmp_path / "web"), "--token", TOKEN])
    assert r.exit_code == 0, r.output
    assert started and started[-1]["host"] == "0.0.0.0" and TOKEN not in r.output     # the token is never printed
    assert "access token required" in r.output
    r = CliRunner().invoke(app, ["serve", "--out", str(tmp_path / "web")])          # 127.0.0.1: no token needed
    assert r.exit_code == 0, r.output
    assert "access token required" not in r.output
