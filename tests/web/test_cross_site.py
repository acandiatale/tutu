"""W1 (review 2026-11-18): a page of another web site must not be able to start, stop or steer a run. The token guard
refuses a state-changing request (anything but GET / HEAD / OPTIONS) that carries an `Origin` whose host is not the
request's own host, or `Sec-Fetch-Site: cross-site`; a client that sends neither (curl, operators' scripts) and the page
itself (same origin) keep working, in open mode and in token mode. Admin and remediation routes sit behind the same guard."""
from __future__ import annotations

import io
import shutil
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sbmig.core.config import PACKAGE_ROOT
from sbmig.web import create_app
from sbmig.web.app import OPEN_ENV, TOKEN_ENV

TOKEN = "cross-site-test-token"
EVIL = "https://evil.example"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    shutil.copy(PACKAGE_ROOT / "config" / "artifacts.local-e2e.yml", repo / "config" / "artifacts.local-e2e.yml")
    return repo


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    monkeypatch.delenv(OPEN_ENV, raising=False)


def _not_a_zip() -> bytes:
    return b"not a zip at all"          # 400 from the route when the guard lets it through


STATE_CHANGING = [
    ("post", "/api/runs?name=p.zip"),
    ("post", "/api/runs/p-20261118-101010-abcdef/stop"),
    ("post", "/admin/api/login"),
    ("post", "/admin/api/logout"),
    ("post", "/api/remediations"),
    ("post", "/api/remediations/r1/resume"),
    ("post", "/api/remediations/r1/stop"),
    ("delete", "/api/remediations/r1"),
]


@pytest.mark.parametrize("method,path", STATE_CHANGING)
def test_foreign_origin_is_refused_in_open_mode(tmp_path, repo, method, path):
    app = create_app(tmp_path / "out", repo, open_network=True)
    c = TestClient(app, client=("10.1.2.3", 5000), base_url="http://sbmig.example:8765")
    r = getattr(c, method)(path, headers={"Origin": EVIL}, **({"content": _not_a_zip()} if method == "post" else {}))
    assert r.status_code == 403, (path, r.status_code, r.text)
    assert "cross-site" in r.json()["error"]


@pytest.mark.parametrize("method,path", STATE_CHANGING)
def test_sec_fetch_site_cross_site_is_refused(tmp_path, repo, method, path):
    app = create_app(tmp_path / "out", repo, open_network=True)
    c = TestClient(app, client=("10.1.2.3", 5000), base_url="http://sbmig.example:8765")
    r = getattr(c, method)(path, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403, (path, r.status_code, r.text)


def test_upload_from_a_foreign_page_never_starts_a_run(tmp_path, repo):
    app = create_app(tmp_path / "out", repo, open_network=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("pom.xml", "<project/>")
    c = TestClient(app, client=("10.1.2.3", 5000), base_url="http://sbmig.example:8765")
    r = c.post("/api/runs?name=p.zip", content=buf.getvalue(),
               headers={"Origin": EVIL, "Content-Type": "text/plain", "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    assert c.get("/api/runs").json()["runs"] == []


def test_same_origin_and_headerless_clients_keep_working(tmp_path, repo):
    app = create_app(tmp_path / "out", repo, open_network=True)
    c = TestClient(app, client=("10.1.2.3", 5000), base_url="http://sbmig.example:8765")
    # the page itself: same origin (default port spelled or not), same-origin fetch metadata
    for origin in ("http://sbmig.example:8765", "HTTP://SBMIG.example:8765"):
        r = c.post("/api/runs?name=p.zip", content=_not_a_zip(),
                   headers={"Origin": origin, "Sec-Fetch-Site": "same-origin"})
        assert r.status_code == 400 and "zip" in r.json()["error"]
    # curl / scripts: no Origin, no Sec-Fetch-Site
    assert c.post("/api/runs?name=p.zip", content=_not_a_zip()).status_code == 400
    # reads are never refused, whatever the headers say
    assert c.get("/api/runs", headers={"Origin": EVIL, "Sec-Fetch-Site": "cross-site"}).status_code == 200
    # default port: Host without a port matches an Origin without one
    c80 = TestClient(app, client=("10.1.2.3", 5000), base_url="http://sbmig.example")
    assert c80.post("/api/runs?name=p.zip", content=_not_a_zip(),
                    headers={"Origin": "http://sbmig.example"}).status_code == 400
    # TLS ended at an ingress: the browser says https, the app sees http with the same host name
    assert c80.post("/api/runs?name=p.zip", content=_not_a_zip(),
                    headers={"Origin": "https://sbmig.example"}).status_code == 400
    # Host names a port: an Origin on another port of the same host name is refused
    assert c.post("/api/runs?name=p.zip", content=_not_a_zip(),
                  headers={"Origin": "http://sbmig.example:9999"}).status_code == 403
    # a look-alike host name is refused
    assert c80.post("/api/runs?name=p.zip", content=_not_a_zip(),
                    headers={"Origin": "http://sbmig.example.evil.example"}).status_code == 403
    # an opaque origin ("null": sandboxed frame, file://) is not ours
    assert c.post("/api/runs?name=p.zip", content=_not_a_zip(), headers={"Origin": "null"}).status_code == 403


def test_token_mode_refuses_a_foreign_origin_even_with_the_cookie(tmp_path, repo):
    app = create_app(tmp_path / "out", repo, token=TOKEN)
    c = TestClient(app, client=("10.1.2.3", 5000), base_url="http://sbmig.example:8765")
    assert c.get(f"/?token={TOKEN}", follow_redirects=False).status_code == 303       # the cookie is set
    assert c.post("/api/runs?name=p.zip", content=_not_a_zip(), headers={"Origin": EVIL}).status_code == 403
    assert c.post("/api/runs?name=p.zip", content=_not_a_zip(),
                  headers={"Origin": "http://sbmig.example:8765"}).status_code == 400
    assert c.post("/api/runs?name=p.zip", content=_not_a_zip(),
                  headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 400


def test_loopback_mode_refuses_a_foreign_origin(tmp_path, repo):
    """No token, loopback only: a browser on the operator's own PC visiting a hostile page is the case."""
    app = create_app(tmp_path / "out", repo)
    c = TestClient(app, client=("127.0.0.1", 5000), base_url="http://127.0.0.1:8765")
    assert c.post("/api/runs?name=p.zip", content=_not_a_zip(), headers={"Origin": EVIL}).status_code == 403
    assert c.post("/api/runs?name=p.zip", content=_not_a_zip(),
                  headers={"Origin": "http://127.0.0.1:8765"}).status_code == 400


def test_a_proxy_forwarded_host_counts_as_our_host(tmp_path, repo):
    """An ingress that rewrites Host keeps the browser's host in X-Forwarded-Host (a header a cross-site page cannot set
    without a CORS preflight the app never answers)."""
    app = create_app(tmp_path / "out", repo, open_network=True)
    c = TestClient(app, client=("10.1.2.3", 5000), base_url="http://sbmig-svc:8765")
    r = c.post("/api/runs?name=p.zip", content=_not_a_zip(),
               headers={"Origin": "https://sbmig.example", "X-Forwarded-Host": "sbmig.example",
                        "X-Forwarded-Proto": "https"})
    assert r.status_code == 400



def test_the_browsers_same_origin_statement_is_enough_behind_a_host_rewriting_proxy(tmp_path, repo):
    """A proxy that rewrites Host and leaves no X-Forwarded-Host must not lock the page out: `Sec-Fetch-Site: same-origin`
    is the browser's own statement (a page cannot set a `Sec-` header), so no host comparison is made."""
    app = create_app(tmp_path / "out", repo, open_network=True)
    c = TestClient(app, client=("10.1.2.3", 5000), base_url="http://sbmig-svc:8765")
    ok = c.post("/api/runs?name=p.zip", content=_not_a_zip(),
                headers={"Origin": "https://sbmig.example", "Sec-Fetch-Site": "same-origin"})
    assert ok.status_code == 400                                   # reached the route (the body is not a ZIP)
    # without that statement (a plain-http page sends none) the rewritten Host makes the page look foreign
    assert c.post("/api/runs?name=p.zip", content=_not_a_zip(),
                  headers={"Origin": "http://sbmig.example"}).status_code == 403


def test_allowed_origins_env_admits_named_hosts_and_star_switches_the_check_off(tmp_path, repo, monkeypatch):
    app = create_app(tmp_path / "out", repo, open_network=True)
    c = TestClient(app, client=("10.1.2.3", 5000), base_url="http://sbmig-svc:8765")

    def post(origin, **extra):
        return c.post("/api/runs?name=p.zip", content=_not_a_zip(), headers={"Origin": origin, **extra}).status_code

    monkeypatch.setenv("SBMIG_WEB_ALLOWED_ORIGINS", "sbmig.example, https://portal.example:8443")
    assert post("http://sbmig.example") == 400
    assert post("https://portal.example:8443", **{"Sec-Fetch-Site": "cross-site"}) == 400
    assert post("https://portal.example") == 403                    # another port of the named host is another origin
    assert post(EVIL) == 403
    monkeypatch.setenv("SBMIG_WEB_ALLOWED_ORIGINS", "*")
    assert post(EVIL, **{"Sec-Fetch-Site": "cross-site"}) == 400
    monkeypatch.delenv("SBMIG_WEB_ALLOWED_ORIGINS")
    assert post(EVIL) == 403
