"""v2.1z-ao: the admin page's server side — password login + session, LLM usage per project read from the run folders,
log ZIPs (one run / all runs), `GET /admin`. Hand-written run folders under a temp output root; no real run, no network.
The real admin password never appears here: the built-in hash is monkeypatched with the hash of a test password."""
from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path

import anyio
import pytest
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from sbmig.core.config import PACKAGE_ROOT
from sbmig.web import admin, contract
from sbmig.web import app as app_module
from sbmig.web.admin import ADMIN_COOKIE, ADMIN_PASSWORD_ENV
from sbmig.web.app import OPEN_ENV, TOKEN_ENV, create_app

TEST_PASSWORD = "test-pass"
SALT = "00112233445566778899aabbccddeeff"
ITER = 1000
NOW = datetime.now(timezone.utc).isoformat()


def _hash(pw: str, salt: str = SALT, iterations: int = ITER) -> str:
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), iterations).hex()


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    monkeypatch.delenv(OPEN_ENV, raising=False)
    monkeypatch.delenv(ADMIN_PASSWORD_ENV, raising=False)
    monkeypatch.delenv("SBMIG_REDACT_OUTPUT", raising=False)
    monkeypatch.setattr(admin, "ADMIN_PASSWORD_PBKDF2",
                        {"iterations": ITER, "salt_hex": SALT, "hash_hex": _hash(TEST_PASSWORD)})
    # the "pod-local temp dir" of every ZIP is a folder under tmp_path (tempfile.gettempdir() follows tempfile.tempdir)
    (tmp_path / "systmp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "systmp"))
    yield
    assert not admin.ZIP_GATE.locked(), "a test left the ZIP gate held"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    shutil.copy(PACKAGE_ROOT / "config" / "artifacts.local-e2e.yml", repo / "config" / "artifacts.local-e2e.yml")
    return repo


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _app(tmp_path: Path, repo: Path, **kw):
    app = create_app(tmp_path / "out", repo, **kw)
    clock = Clock()
    app.state.admin_auth.clock = clock
    app.state.admin_auth.fail_delay_s = 0
    return app, clock


def _login(client: TestClient, pw: str = TEST_PASSWORD):
    return client.post("/admin/api/login", json={"password": pw})


# ---------------------------------------------------------------------------
# run folders
# ---------------------------------------------------------------------------
def _w(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _req(rd: Path, upload: str, accepted: str, flow: str = "full") -> None:
    _w(rd / "control" / "request.json", json.dumps({
        "run_id": rd.name, "accepted_at": accepted, "upload": upload.replace(" ", "_"), "upload_name": upload,
        "configs": [], "zip_sha256": "0" * 64, "config_digests": {}, "sbmig_version": "x", "mode": "k8s-job",
        "flow": flow}))


def _report(rd: Path, *, usage, by_model, cost=None, scope="full", success=True, flags=()) -> None:
    _w(rd / "report.json", json.dumps({
        "run_id": rd.name, "project_name": "ignored-name", "flow": "full", "started_at": "2026-01-01T00:00:00+00:00",
        "success": success, "blocked": [], "flags": list(flags), "plan": {"scope": scope},
        "llm_usage": dict(zip(("calls", "input_tokens", "output_tokens"), usage), model=""),
        "llm_usage_by_model": [dict(zip(("model", "calls", "input_tokens", "output_tokens"), m)) for m in by_model],
        "llm_cost": cost, "some_future_key": {"x": 1}}))


def _cost(total: float, per: dict[str, float]) -> dict:
    return {"currency": "USD", "total": total, "unpriced_models": [], "usage_missing": False,
            "by_model": [{"model": m, "cost": c} for m, c in per.items()]}


SHOP1, SHOP2 = "shop-20261101-100000-aaaa", "shop-20261102-100000-bbbb"
BILL1, BILL2 = "billing-20261103-100000-cccc", "billing-20261104-100000-dddd"
LEGACY = "legacy-20261105-100000-eeee"


def _shop_runs(out: Path) -> None:
    rd = out / SHOP1
    _req(rd, "shop.zip", "2026-11-01T10:00:00+00:00")
    _report(rd, usage=(3, 1000, 200), by_model=[("gpt-a", 2, 800, 150), ("gpt-b", 1, 200, 50)],
            cost=_cost(0.5, {"gpt-a": 0.4, "gpt-b": 0.1}))
    _w(rd / "job-status.json", json.dumps({"run_id": SHOP1, "exit_code": 0}))
    _w(rd / "sbmig.log", "line one\nline two\n")
    _w(rd / "journal.jsonl", '{"event": "intake"}\n')
    _w(rd / "remediation" / "remediation.log", "remediation log\n")
    for excluded in ("report.md", "result.zip", "source.zip", "logs/build-1.log", "llm-records/llm-001.json",
                     "remediation/journal.jsonl", "errors.md", "job-local.log"):
        _w(rd / excluded, "must not be in the zip\n")
    rd = out / SHOP2
    _req(rd, "shop.zip", "2026-11-02T10:00:00+00:00")
    _report(rd, usage=(1, 100, 10), by_model=[("gpt-a", 1, 100, 10)], cost=_cost(0.05, {"gpt-a": 0.05}),
            scope="jdk-jakarta", success=False)
    _w(rd / "job-status.json", json.dumps({"run_id": SHOP2, "exit_code": 2}))
    _w(rd / "journal.jsonl", '{"event": "intake"}\n')


def _all_runs(out: Path, outside: Path) -> None:
    _shop_runs(out)
    rd = out / BILL1                                   # unpriced, no log file
    _req(rd, "billing.zip", "2026-11-03T10:00:00+00:00")
    _report(rd, usage=(2, 5000, 500), by_model=[("gpt-a", 2, 5000, 500)], cost=None)
    _w(rd / "job-status.json", json.dumps({"run_id": BILL1, "exit_code": 0}))
    rd = out / BILL2                                   # running: request.json + the log's llm call lines only
    _req(rd, "billing.zip", "2026-11-04T10:00:00+00:00")
    _w(rd / "sbmig.log", "x INFO sbmig.fix.company: llm call provider=company model=gpt-b in=300 out=30 latency=1.0s "
                         "finish=stop\nnoise\nx INFO sbmig.fix.client: llm call provider=openai model=gpt-b in=200 out=20 "
                         "latency=1.0s finish=stop\n")
    _w(rd / "journal.jsonl", json.dumps({"ts": NOW, "event": "job_created", "job": "sbmig-x"}) + "\n")
    rd = out / LEGACY                                  # truncated report.json; the journal's fix events count
    _req(rd, "legacy.zip", "2026-11-05T10:00:00+00:00")
    _w(rd / "report.json", '{"llm_usage": {"calls": 4')
    _w(rd / "sbmig.log", "no llm lines here\n")
    _w(rd / "journal.jsonl", json.dumps({"event": "llm_script", "model": "gpt-a", "calls": 1,
                                         "tokens": {"input": 50, "output": 5}}) + "\n")
    _w(outside / "secret.log", "outside the root\n")
    (rd / "remediation").mkdir()
    os.symlink(outside / "secret.log", rd / "remediation" / "remediation.log")
    (out / "notes").mkdir()                            # not a run
    (out / "_uploads").mkdir()
    _w(out / "stray.txt", "a file")
    _req(outside / "evil-20261106-100000-ffff", "evil.zip", "2026-11-06T10:00:00+00:00")
    _w(outside / "evil-20261106-100000-ffff" / "sbmig.log", "evil\n")
    os.symlink(outside / "evil-20261106-100000-ffff", out / "evil-20261106-100000-ffff")


def _run(rid, accepted, flow, scope, status, result, calls, inp, outp, cost, by_model, known=True, partial=False,
         logs=True):
    return {"run_id": rid, "accepted_at": accepted, "flow": flow, "scope": scope, "status": status, "result": result,
            "calls": calls, "input_tokens": inp, "output_tokens": outp, "total_tokens": inp + outp, "cost": cost,
            "by_model": [{"model": m, "calls": c, "input_tokens": i, "output_tokens": o, "total_tokens": i + o,
                          "cost": mc} for m, c, i, o, mc in by_model],
            "usage_known": known, "partial": partial, "has_logs": logs}


SHOP_ROWS = [
    _run(SHOP2, "2026-11-02T10:00:00+00:00", "full", "jdk-jakarta", "finished", "incomplete", 1, 100, 10, 0.05,
         [("gpt-a", 1, 100, 10, 0.05)]),
    _run(SHOP1, "2026-11-01T10:00:00+00:00", "full", "full", "finished", "ok", 3, 1000, 200, 0.5,
         [("gpt-a", 2, 800, 150, 0.4), ("gpt-b", 1, 200, 50, 0.1)]),
]


def _usage(client: TestClient) -> dict:
    r = client.get("/admin/api/usage")
    assert r.status_code == 200, r.text
    data = r.json()
    datetime.fromisoformat(data.pop("generated_at"))
    return data


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------
DATA_ROUTES = ("/admin/api/usage", "/admin/api/logs.zip", f"/admin/api/runs/{SHOP1}/logs.zip")


def test_data_routes_need_the_login_and_logout_ends_it(tmp_path, repo):
    app, _ = _app(tmp_path, repo)
    _shop_runs(tmp_path / "out")
    c = TestClient(app)
    for route in DATA_ROUTES:
        r = c.get(route)
        assert r.status_code == 401 and r.json() == {"error": "login_required"}, route
    r = c.get("/admin/api/session")
    assert r.status_code == 200 and r.json() == {"authenticated": False}

    r = _login(c, "wrong-pass")
    assert r.status_code == 401 and r.json() == {"error": "bad_password"}
    assert "set-cookie" not in r.headers and ADMIN_COOKIE not in c.cookies
    assert c.get("/admin/api/usage").status_code == 401
    assert _login(c, "").status_code == 401
    assert c.post("/admin/api/login", content=b"not json").status_code == 401

    r = _login(c)
    assert r.status_code == 200 and r.json() == {"authenticated": True}
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{ADMIN_COOKIE}=")
    low = cookie.lower()
    assert "httponly" in low and "samesite=strict" in low and "path=/admin" in low and "max-age=43200" in low
    assert "secure" not in low                        # plain http on the intranet
    assert c.get("/admin/api/session").json() == {"authenticated": True}
    for route in DATA_ROUTES:
        assert c.get(route).status_code == 200, route

    old = c.cookies.get(ADMIN_COOKIE)
    r = c.post("/admin/api/logout")
    assert r.status_code == 200 and r.json() == {"authenticated": False}
    assert c.get("/admin/api/session").json() == {"authenticated": False}
    for route in DATA_ROUTES:
        assert c.get(route).status_code == 401, route
    # the old cookie value replayed after logout is dead too
    c2 = TestClient(app, cookies={ADMIN_COOKIE: old})
    assert c2.get("/admin/api/usage").status_code == 401


def test_cookie_is_secure_over_https(tmp_path, repo):
    app, _ = _app(tmp_path, repo)
    c = TestClient(app, base_url="https://testserver")
    assert "secure" in _login(c).headers["set-cookie"].lower()


def test_session_expires_after_twelve_hours_and_a_restart_logs_out(tmp_path, repo):
    app, clock = _app(tmp_path, repo)
    c = TestClient(app)
    assert _login(c).status_code == 200
    clock.t += 12 * 3600 - 5
    assert c.get("/admin/api/usage").status_code == 200
    clock.t += 10
    assert c.get("/admin/api/usage").status_code == 401
    assert _login(c).status_code == 200
    app2, _ = _app(tmp_path, repo)                   # another process: its own random sessions
    c2 = TestClient(app2, cookies={ADMIN_COOKIE: c.cookies.get(ADMIN_COOKIE)})
    assert c2.get("/admin/api/usage").status_code == 401


def test_five_failures_lock_one_address_for_the_window(tmp_path, repo):
    app, clock = _app(tmp_path, repo, open_network=True)
    a = TestClient(app, client=("10.0.0.1", 5000))
    b = TestClient(app, client=("10.0.0.2", 5000))
    for _ in range(5):
        assert _login(a, "nope-nope").status_code == 401
    r = _login(a)                                     # even the right password: locked
    assert r.status_code == 429 and r.json()["error"] == "too_many_attempts"
    assert int(r.headers["retry-after"]) == r.json()["retry_after_s"] == 600
    assert ADMIN_COOKIE not in a.cookies
    assert _login(b).status_code == 200               # another address is not affected
    clock.t += 300
    assert _login(a).status_code == 429
    assert _login(a).json()["retry_after_s"] == 300
    clock.t += 301
    assert _login(a).status_code == 200
    # a success clears the count
    for _ in range(4):
        assert _login(a, "nope-nope").status_code == 401
    assert _login(a).status_code == 200
    assert _login(a, "nope-nope").status_code == 401
    assert _login(a).status_code == 200


def test_failed_login_waits_without_blocking_and_never_logs_the_password(tmp_path, repo, caplog):
    import logging

    app, _ = _app(tmp_path, repo)
    app.state.admin_auth.fail_delay_s = 0.2
    c = TestClient(app)
    caplog.set_level(logging.DEBUG, logger="sbmig.web.admin")
    t0 = time.monotonic()
    assert _login(c, "guess-123456").status_code == 401
    assert time.monotonic() - t0 >= 0.2
    t0 = time.monotonic()
    assert _login(c).status_code == 200
    assert time.monotonic() - t0 < 0.2
    assert all("guess-123456" not in r.getMessage() and TEST_PASSWORD not in r.getMessage() for r in caplog.records)


def test_the_environment_password_replaces_the_built_in_one(tmp_path, repo, monkeypatch):
    monkeypatch.setenv(ADMIN_PASSWORD_ENV, "from-the-secret")
    app, _ = _app(tmp_path, repo)
    c = TestClient(app)
    assert _login(c, TEST_PASSWORD).status_code == 401
    assert _login(c, "from-the-secret").status_code == 200
    monkeypatch.setenv(ADMIN_PASSWORD_ENV, "")        # empty = the built-in hash again
    assert _login(TestClient(app), TEST_PASSWORD).status_code == 200


def test_built_in_hash_is_the_contract_value_and_this_file_does_not_hold_the_password(monkeypatch):
    monkeypatch.undo()
    spec = admin.ADMIN_PASSWORD_PBKDF2
    assert spec == {"iterations": 200_000, "salt_hex": "bfadb8a1a0a6a71e0571005ea6840ae3",
                    "hash_hex": "39baec1e83416b257be2de09e9ae13461ba0dfea532602f0f4a2445f6c763930"}
    words: set[str] = set()
    for node in ast.walk(ast.parse(Path(__file__).read_text(encoding="utf-8"))):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            words |= {w for w in node.value.split() if 4 <= len(w) <= 40}
            if 4 <= len(node.value) <= 40:
                words.add(node.value)
    salt, iters, want = bytes.fromhex(spec["salt_hex"]), spec["iterations"], bytes.fromhex(spec["hash_hex"])
    for w in words:
        assert hashlib.pbkdf2_hmac("sha256", w.encode(), salt, iters) != want


def test_the_admin_password_variable_is_a_secret_and_never_reaches_a_local_job(tmp_path, repo, monkeypatch):
    from sbmig.web.localjob import WEB_ONLY_ENV, LocalJobClient

    assert contract.is_secret_name(ADMIN_PASSWORD_ENV)
    assert ADMIN_PASSWORD_ENV in WEB_ONLY_ENV
    monkeypatch.setenv(ADMIN_PASSWORD_ENV, "pod-only-value")
    monkeypatch.setenv("SBMIG_SOME_OTHER", "kept")
    script = tmp_path / "env.py"
    script.write_text("import os\nprint('admin=' + os.environ.get('SBMIG_ADMIN_PASSWORD', '<none>') + "
                      "' other=' + os.environ.get('SBMIG_SOME_OTHER', '<none>'))\n", encoding="utf-8")
    jc = LocalJobClient(repo, command_factory=lambda env: [sys.executable, str(script)], echo=False)
    name = jc.create_job({"metadata": {"name": "sbmig-env"}, "spec": {"template": {"spec": {"containers": [{"name": "b", "env": [
        {"name": "SBMIG_SOURCE_ZIP", "value": "x.zip"}, {"name": "SBMIG_OUT", "value": str(tmp_path / "out")},
        {"name": "SBMIG_RUN_ID", "value": "r-env"}]}]}}}})
    deadline = time.time() + 10
    while not jc.job_state(name).finished and time.time() < deadline:
        time.sleep(0.02)
    time.sleep(0.1)
    text = (tmp_path / "out" / "r-env" / "job-local.log").read_text(encoding="utf-8")
    assert "admin=<none> other=kept" in text


def test_with_a_site_token_the_admin_routes_need_both(tmp_path, repo):
    app, _ = _app(tmp_path, repo, token="site-token-xyz")
    bare = TestClient(app)
    assert bare.post("/admin/api/login", json={"password": TEST_PASSWORD}).status_code == 401   # the site guard
    assert ADMIN_COOKIE not in bare.cookies
    site = TestClient(app, headers={"Authorization": "Bearer site-token-xyz"})
    r = site.get("/admin/api/session")             # the site guard let it through: 200, not 401
    assert r.status_code == 200 and r.json() == {"authenticated": False}
    assert site.get("/admin/api/usage").status_code == 401
    assert site.get("/admin/api/usage").json() == {"error": "login_required"}
    assert _login(site).status_code == 200
    assert site.get("/admin/api/usage").status_code == 200
    # the admin cookie alone (no site token) is refused by the site guard
    only_admin = TestClient(app, cookies={ADMIN_COOKIE: site.cookies.get(ADMIN_COOKIE)})
    r = only_admin.get("/admin/api/usage")
    assert r.status_code == 401 and "access token required" in r.json()["error"]
    # /admin?token=… sets the site cookie and reloads without the token, like the main page
    r = bare.get("/admin?token=site-token-xyz", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin"


# ---------------------------------------------------------------------------
# usage
# ---------------------------------------------------------------------------
def test_usage_of_priced_runs_only_has_cost_sums(tmp_path, repo):
    app, _ = _app(tmp_path, repo)
    _shop_runs(tmp_path / "out")
    c = TestClient(app)
    _login(c)
    assert _usage(c) == {
        "out_root": str((tmp_path / "out").resolve()),
        "totals": {"projects": 1, "runs": 2, "calls": 4, "input_tokens": 1100, "output_tokens": 210,
                   "total_tokens": 1310, "cost": 0.55, "currency": "USD", "unpriced_runs": 0},
        "by_model": [{"model": "gpt-a", "calls": 3, "input_tokens": 900, "output_tokens": 160, "total_tokens": 1060,
                      "cost": 0.45},
                     {"model": "gpt-b", "calls": 1, "input_tokens": 200, "output_tokens": 50, "total_tokens": 250,
                      "cost": 0.1}],
        "projects": [{"project": "shop", "runs": 2, "calls": 4, "input_tokens": 1100, "output_tokens": 210,
                      "total_tokens": 1310, "cost": 0.55, "unpriced_runs": 0,
                      "first_run_at": "2026-11-01T10:00:00+00:00",
                      "last_run_at": "2026-11-02T10:00:00+00:00", "run_list": SHOP_ROWS}],
    }


def test_usage_groups_by_upload_name_and_tolerates_odd_folders(tmp_path, repo):
    app, _ = _app(tmp_path, repo)
    outside = tmp_path / "outside"
    _all_runs(tmp_path / "out", outside)
    c = TestClient(app)
    _login(c)
    assert _usage(c) == {
        "out_root": str((tmp_path / "out").resolve()),
        "totals": {"projects": 3, "runs": 5, "calls": 9, "input_tokens": 6650, "output_tokens": 765,
                   "total_tokens": 7415, "cost": None, "currency": "USD", "unpriced_runs": 3},
        "by_model": [{"model": "gpt-a", "calls": 6, "input_tokens": 5950, "output_tokens": 665, "total_tokens": 6615,
                      "cost": None},
                     {"model": "gpt-b", "calls": 3, "input_tokens": 700, "output_tokens": 100, "total_tokens": 800,
                      "cost": None}],
        "projects": [
            {"project": "billing", "runs": 2, "calls": 4, "input_tokens": 5500, "output_tokens": 550,
             "total_tokens": 6050, "cost": None, "unpriced_runs": 2, "first_run_at": "2026-11-03T10:00:00+00:00",
             "last_run_at": "2026-11-04T10:00:00+00:00", "run_list": [
                 _run(BILL2, "2026-11-04T10:00:00+00:00", "full", None, "running", None, 2, 500, 50, None,
                      [("gpt-b", 2, 500, 50, None)]),
                 _run(BILL1, "2026-11-03T10:00:00+00:00", "full", "full", "finished", "ok", 2, 5000, 500, None,
                      [("gpt-a", 2, 5000, 500, None)], logs=False)]},
            {"project": "shop", "runs": 2, "calls": 4, "input_tokens": 1100, "output_tokens": 210,
             "total_tokens": 1310, "cost": 0.55, "unpriced_runs": 0, "first_run_at": "2026-11-01T10:00:00+00:00",
             "last_run_at": "2026-11-02T10:00:00+00:00", "run_list": SHOP_ROWS},
            {"project": "legacy", "runs": 1, "calls": 1, "input_tokens": 50, "output_tokens": 5,
             "total_tokens": 55, "cost": None, "unpriced_runs": 1, "first_run_at": "2026-11-05T10:00:00+00:00",
             "last_run_at": "2026-11-05T10:00:00+00:00", "run_list": [
                 _run(LEGACY, "2026-11-05T10:00:00+00:00", "full", None, "finished", None, 1, 50, 5, None,
                      [("gpt-a", 1, 50, 5, None)], partial=True)]},
        ],
    }


def test_usage_unknown_and_empty_cases(tmp_path, repo):
    app, _ = _app(tmp_path, repo)
    c = TestClient(app)
    _login(c)
    empty = _usage(c)                                  # the output root does not even exist yet
    assert empty["projects"] == [] and empty["by_model"] == []
    assert empty["totals"] == {"projects": 0, "runs": 0, "calls": 0, "input_tokens": 0, "output_tokens": 0,
                               "total_tokens": 0, "cost": None, "currency": None, "unpriced_runs": 0}
    out = tmp_path / "out"
    rd = out / "proj-20261110-090000-abcd"             # no usage on disk at all
    _req(rd, "My Proj.v2.zip", "2026-11-10T09:00:00Z")
    rd2 = out / "web-20261111-090000-abcd"             # no name anywhere but the report's / the id
    _report(rd2, usage=(0, 0, 0), by_model=[], cost=None)
    rd3 = out / "olderapp-20261112-090000-abcd"
    _w(rd3 / "control" / "request.json", "{broken")
    data = _usage(c)
    rows = {r["run_id"]: (p["project"], r) for p in data["projects"] for r in p["run_list"]}
    name, r = rows["proj-20261110-090000-abcd"]
    assert name == "My Proj.v2" and r["usage_known"] is False and r["cost"] is None and r["total_tokens"] == 0
    assert r["accepted_at"] == "2026-11-10T09:00:00+00:00" and r["status"] == "unknown" and r["has_logs"] is False
    name, r = rows["web-20261111-090000-abcd"]
    assert name == "ignored-name" and r["usage_known"] is True and r["cost"] == 0.0     # no call made: costs nothing
    name, r = rows["olderapp-20261112-090000-abcd"]
    assert name == "olderapp" and r["partial"] is True
    assert admin.project_of(None, None, "plainid") == "plainid"
    assert admin.project_of("C:\\work\\x.tar.gz", None, "r") == "x.tar"
    assert admin.project_of(".hidden", None, "r") == ".hidden"


# ---------------------------------------------------------------------------
# zips
# ---------------------------------------------------------------------------
def _zip(resp) -> dict[str, bytes]:
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    assert all(i.compress_type == zipfile.ZIP_DEFLATED for i in zf.infolist())
    return {n: zf.read(n) for n in zf.namelist()}


def _no_temp(tmp_path: Path) -> None:
    # the temp ZIP lived in the local temp dir and is gone; nothing was ever written under the output root
    assert list((tmp_path / "systmp").glob("sbmig-logs-*")) == []
    assert not (tmp_path / "out" / "_admin").exists()


def test_all_runs_zip(tmp_path, repo):
    app, _ = _app(tmp_path, repo)
    out = tmp_path / "out"
    _all_runs(out, tmp_path / "outside")
    c = TestClient(app)
    _login(c)
    r = c.get("/admin/api/logs.zip")
    disp = r.headers["content-disposition"]
    assert disp.startswith('attachment; filename="sbmig-logs-') and disp.endswith('.zip"')
    stamp = disp.split("sbmig-logs-")[1][:-5]
    datetime.strptime(stamp, "%Y%m%d-%H%M%S")
    files = _zip(r)
    expected = [f"{SHOP1}/sbmig.log", f"{SHOP1}/journal.jsonl", f"{SHOP1}/control/request.json",
                f"{SHOP1}/remediation/remediation.log",
                f"{SHOP2}/journal.jsonl", f"{SHOP2}/control/request.json",
                f"{BILL2}/sbmig.log", f"{BILL2}/journal.jsonl", f"{BILL2}/control/request.json",
                f"{LEGACY}/sbmig.log", f"{LEGACY}/journal.jsonl", f"{LEGACY}/control/request.json"]
    assert sorted(files) == sorted(expected)          # BILL1 has no log; the symlinks and "notes" are not runs
    for name, data in files.items():
        assert data == (out / name).read_bytes()
    assert b"outside the root" not in b"".join(files.values()) and b"evil" not in b"".join(files.values())
    _no_temp(tmp_path)


def test_one_run_zip_and_bad_ids(tmp_path, repo):
    app, _ = _app(tmp_path, repo)
    out = tmp_path / "out"
    _all_runs(out, tmp_path / "outside")
    c = TestClient(app)
    _login(c)
    r = c.get(f"/admin/api/runs/{SHOP1}/logs.zip")
    assert r.headers["content-disposition"] == f'attachment; filename="sbmig-logs-{SHOP1}.zip"'
    files = _zip(r)
    assert sorted(files) == sorted(f"{SHOP1}/{m}" for m in admin.LOG_MEMBERS)
    for name, data in files.items():
        assert data == (out / name).read_bytes()
    assert sorted(_zip(c.get(f"/admin/api/runs/{LEGACY}/logs.zip"))) == sorted(
        f"{LEGACY}/{m}" for m in ("sbmig.log", "journal.jsonl", "control/request.json"))
    # a run without any log file: a valid ZIP that says so
    assert _zip(c.get(f"/admin/api/runs/{BILL1}/logs.zip")) == {"README.txt": admin.EMPTY_README.encode()}
    # (a raw ".." segment is normalized away by the HTTP client itself, so it is sent encoded)
    for bad in ("%2e%2e", ".%2e", "..%2F..%2Fetc", "%2Fetc", "evil-20261106-100000-ffff", "notes-missing", "_uploads",
                ".hidden", "stray.txt"):
        assert c.get(f"/admin/api/runs/{bad}/logs.zip").status_code == 404, bad
    assert c.get("/admin/api/runs//etc/logs.zip").status_code == 404
    _no_temp(tmp_path)


def test_empty_root_zip_has_a_readme(tmp_path, repo):
    app, _ = _app(tmp_path, repo)
    (tmp_path / "out" / "notes").mkdir(parents=True)
    c = TestClient(app)
    _login(c)
    assert _zip(c.get("/admin/api/logs.zip")) == {"README.txt": admin.EMPTY_README.encode()}
    _no_temp(tmp_path)


def test_remediation_log_waits_for_the_final_redaction_pass(tmp_path, repo):
    out = tmp_path / "out"
    _shop_runs(out)
    rd = out / SHOP1
    dirs = [rd]
    with zipfile.ZipFile(admin.build_logs_zip(dirs, lambda rid: True)) as zf:
        assert f"{SHOP1}/remediation/remediation.log" not in zf.namelist()
        assert f"{SHOP1}/sbmig.log" in zf.namelist()
    with zipfile.ZipFile(admin.build_logs_zip(dirs, lambda rid: False)) as zf:
        assert f"{SHOP1}/remediation/remediation.log" in zf.namelist()


def test_temp_file_is_removed_when_the_client_goes_away(tmp_path):
    out = tmp_path / "out"
    _shop_runs(out)
    path = admin.build_logs_zip([out / SHOP1])
    assert path.is_file() and path.parent == Path(tempfile.gettempdir()) == tmp_path / "systmp"
    closed = []
    resp = admin.TempFileResponse(path, "x.zip", on_close=lambda: closed.append(1))

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body":
            raise OSError("client gone")

    async def go():
        with pytest.raises(ClientDisconnect):
            await resp({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)

    anyio.run(go)
    assert not path.exists()
    assert closed == [1]                               # the gate callback ran once, after the file was gone


def test_the_hour_old_sweep_is_gone_and_the_legacy_folder_is_cleaned_once_at_startup(tmp_path, repo):
    out = tmp_path / "out"
    legacy = out / "_admin"
    leftovers = [legacy / "sbmig-logs-abc_12x.zip", legacy / "sbmig-logs-q-9.zip"]
    for p in leftovers:
        _w(p, "PK")
    keep = [legacy / "notes.txt", legacy / "sbmig-logs-.zip", legacy / "sbmig-logs-a.zip.part", legacy / "other.zip"]
    for p in keep:
        _w(p, "keep")
    (legacy / "sbmig-logs-dir.zip").mkdir()                       # a directory with the pattern's name: untouched
    _w(tmp_path / "outside" / "x.txt", "outside")
    os.symlink(tmp_path / "outside" / "x.txt", legacy / "sbmig-logs-link.zip")   # a symlink: untouched, target too
    assert not hasattr(admin, "_sweep_tmp")
    _app(tmp_path, repo)                                          # create_admin_router runs the clean-up
    assert not any(p.exists() for p in leftovers)
    assert all(p.exists() for p in keep) and (legacy / "sbmig-logs-dir.zip").is_dir()
    assert (legacy / "sbmig-logs-link.zip").is_symlink() and (tmp_path / "outside" / "x.txt").exists()
    # a folder holding only leftovers goes away entirely; a later build never recreates it
    shutil.rmtree(legacy)
    _w(legacy / "sbmig-logs-zz.zip", "PK")
    app, _ = _app(tmp_path, repo)
    assert not legacy.exists()
    c = TestClient(app)
    _login(c)
    _zip(c.get("/admin/api/logs.zip"))
    _no_temp(tmp_path)


def test_a_log_streams_into_the_zip_in_chunks(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "ZIP_CHUNK", 512)                  # several chunks for a 40 KB file
    out = tmp_path / "out"
    rd = out / "big-20261101-100000-aaaa"
    _req(rd, "big.zip", "2026-11-01T10:00:00+00:00")
    line = b"x INFO sbmig.fix.company: llm call provider=company model=m in=1 out=2 latency=0.1s finish=stop\n"
    (rd / "sbmig.log").write_bytes(line * 400)                    # ~40 KB
    assert admin.usage_from_log(rd) == {"m": {"calls": 400, "input_tokens": 400, "output_tokens": 800}}
    with zipfile.ZipFile(admin.build_logs_zip([rd])) as zf:
        assert zf.read(f"{rd.name}/sbmig.log") == (rd / "sbmig.log").read_bytes()
        assert zf.testzip() is None


# ---------------------------------------------------------------------------
# ZIP limits (W2): one at a time, free space, total cap, per-file skip, bounded copy, preflight
# ---------------------------------------------------------------------------
DU = namedtuple("DU", "total used free")


def _small_run(out: Path, rid: str = SHOP1, log: bytes = b"line\n") -> Path:
    rd = out / rid
    _req(rd, "shop.zip", "2026-11-01T10:00:00+00:00")
    (rd / "sbmig.log").write_bytes(log)
    _w(rd / "journal.jsonl", '{"event": "intake"}\n')
    return rd


def _logged_in(tmp_path, repo) -> TestClient:
    app, _ = _app(tmp_path, repo)
    c = TestClient(app)
    _login(c)
    return c


def test_a_second_zip_while_one_is_building_answers_busy_at_once(tmp_path, repo, monkeypatch):
    out = tmp_path / "out"
    _small_run(out)
    c = _logged_in(tmp_path, repo)
    started, release = threading.Event(), threading.Event()
    real = admin.write_logs_zip

    def held(plan, tmp_dir=None):
        started.set()
        assert release.wait(10), "the test never released the build"
        return real(plan, tmp_dir)

    monkeypatch.setattr(admin, "write_logs_zip", held)
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("r", c.get("/admin/api/logs.zip")))
    t.start()
    try:
        assert started.wait(10)
        t0 = time.monotonic()
        for url in ("/admin/api/logs.zip", f"/admin/api/runs/{SHOP1}/logs.zip", "/admin/api/logs/check",
                    f"/admin/api/runs/{SHOP1}/logs/check"):
            r = c.get(url)
            assert r.status_code == 409 and r.json() == {"error": "busy"}, url
        assert time.monotonic() - t0 < 5                       # answered at once, not queued
    finally:
        release.set()
        t.join(10)
    assert sorted(_zip(result["r"])) == [f"{SHOP1}/control/request.json", f"{SHOP1}/journal.jsonl",
                                         f"{SHOP1}/sbmig.log"]
    assert not admin.ZIP_GATE.locked()                          # released once the temp file was deleted
    assert c.get("/admin/api/logs/check").status_code == 200
    _no_temp(tmp_path)


def test_no_space_in_the_local_temp_dir_answers_507_and_never_builds(tmp_path, repo, monkeypatch):
    out = tmp_path / "out"
    _small_run(out, log=b"x" * 3000)
    c = _logged_in(tmp_path, repo)
    monkeypatch.setattr(admin, "ZIP_FREE_FLOOR", 4096)
    asked = []

    def du(path):
        asked.append(path)
        return DU(10 ** 9, 0, free)

    monkeypatch.setattr(admin.shutil, "disk_usage", du)
    built = []
    monkeypatch.setattr(admin, "write_logs_zip", lambda *a, **k: built.append(1))
    free = 5000                                                 # 3 KB of logs + the 4 KB floor do not fit
    for url in ("/admin/api/logs.zip", "/admin/api/logs/check", f"/admin/api/runs/{SHOP1}/logs.zip"):
        r = c.get(url)
        assert r.status_code == 507, url
        body = r.json()
        assert body["error"] == "no_space" and body["free_bytes"] == 5000
        assert body["needed_bytes"] > 3000 + 4096 + 26 and set(body) == {"error", "needed_bytes", "free_bytes"}
    assert built == [] and asked and all(Path(p) == tmp_path / "systmp" for p in asked)   # the LOCAL temp dir
    free = 10 ** 8
    assert c.get("/admin/api/logs/check").json() == {"ok": True, "bytes": 3000 + 20 + len(
        (out / SHOP1 / "control" / "request.json").read_bytes()), "runs": 1, "skipped": 0}
    assert not admin.ZIP_GATE.locked()


def test_over_the_total_cap_answers_413(tmp_path, repo, monkeypatch):
    out = tmp_path / "out"
    _small_run(out, SHOP1, b"a" * 3000)
    _small_run(out, SHOP2, b"b" * 3000)
    c = _logged_in(tmp_path, repo)
    monkeypatch.setattr(admin, "ZIP_TOTAL_LIMIT", 4096)
    for url in ("/admin/api/logs.zip", "/admin/api/logs/check"):
        r = c.get(url)
        assert r.status_code == 413, url
        body = r.json()
        assert body["error"] == "too_large" and body["limit_bytes"] == 4096 and body["bytes"] > 6000
    # one run alone fits: the page tells the operator to download per run
    assert c.get(f"/admin/api/runs/{SHOP1}/logs/check").json()["ok"] is True
    assert f"{SHOP1}/sbmig.log" in _zip(c.get(f"/admin/api/runs/{SHOP1}/logs.zip"))
    _no_temp(tmp_path)


def test_a_file_over_the_per_file_limit_is_skipped_and_listed(tmp_path, repo, monkeypatch):
    out = tmp_path / "out"
    _small_run(out, SHOP1, b"r" * 3000)                          # the runaway log
    _small_run(out, SHOP2, b"ok\n")
    c = _logged_in(tmp_path, repo)
    monkeypatch.setattr(admin, "ZIP_FILE_LIMIT", 2048)
    assert c.get("/admin/api/logs/check").json()["skipped"] == 1
    files = _zip(c.get("/admin/api/logs.zip"))
    assert f"{SHOP1}/sbmig.log" not in files
    assert f"{SHOP1}/journal.jsonl" in files and f"{SHOP2}/sbmig.log" in files
    skipped = files["SKIPPED.txt"].decode()
    assert f"{SHOP1}/sbmig.log\t3000" in skipped and "2048" in skipped
    # only the runaway file: the ZIP holds SKIPPED.txt and no README
    rd = out / "solo-20261107-100000-abcd"
    _req(rd, "solo.zip", NOW)
    (rd / "sbmig.log").write_bytes(b"s" * 3000)
    assert "README.txt" not in _zip(c.get(f"/admin/api/runs/{rd.name}/logs.zip"))
    _no_temp(tmp_path)


def test_a_growing_log_is_copied_only_up_to_its_planned_size(tmp_path):
    out = tmp_path / "out"
    rd = _small_run(out, log=b"start\n" * 100)
    before = (rd / "sbmig.log").read_bytes()
    plan = admin.plan_logs_zip([rd])
    with open(rd / "sbmig.log", "ab") as fh:                     # the pod appends after the member started
        fh.write(b"later\n" * 600)
    path = admin.write_logs_zip(plan)
    try:
        with zipfile.ZipFile(path) as zf:
            assert zf.testzip() is None
            assert zf.getinfo(f"{rd.name}/sbmig.log").file_size == len(before)
            assert zf.read(f"{rd.name}/sbmig.log") == before
    finally:
        path.unlink()


def test_a_log_appended_during_the_build_keeps_the_archive_bounded_and_valid(tmp_path, monkeypatch):
    # writer thread: at most 64 KiB in total, stops in finally; the archive copies in 256-byte chunks meanwhile
    monkeypatch.setattr(admin, "ZIP_CHUNK", 256)
    out = tmp_path / "out"
    rd = _small_run(out, log=b"0" * 8192)
    stop, cap, written = threading.Event(), 64 * 1024, [0]

    def writer():
        with open(rd / "sbmig.log", "ab") as fh:
            while not stop.is_set() and written[0] < cap:
                fh.write(b"w" * 256)
                fh.flush()
                written[0] += 256
                time.sleep(0.0005)

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    try:
        plan = admin.plan_logs_zip([rd])
        planned = {arc: size for arc, _, size in plan.members}[f"{rd.name}/sbmig.log"]
        path = admin.write_logs_zip(plan)
    finally:
        stop.set()
        t.join(5)
    try:
        with zipfile.ZipFile(path) as zf:
            assert zf.testzip() is None
            assert zf.getinfo(f"{rd.name}/sbmig.log").file_size == planned
    finally:
        path.unlink()
    assert written[0] <= cap


def test_preflight_routes_need_the_login_and_answer_the_contract_shape(tmp_path, repo):
    out = tmp_path / "out"
    _all_runs(out, tmp_path / "outside")
    app, _ = _app(tmp_path, repo)
    c = TestClient(app)
    for url in ("/admin/api/logs/check", f"/admin/api/runs/{SHOP1}/logs/check"):
        assert c.get(url).status_code == 401
    _login(c)
    body = c.get("/admin/api/logs/check").json()
    assert body["ok"] is True and body["runs"] == 4 and body["skipped"] == 0       # SHOP1, SHOP2, BILL2, LEGACY
    total = sum((out / n).stat().st_size for n in _zip(c.get("/admin/api/logs.zip")))
    assert body["bytes"] == total
    one = c.get(f"/admin/api/runs/{SHOP1}/logs/check").json()
    assert one == {"ok": True, "bytes": sum((out / SHOP1 / m).stat().st_size for m in admin.LOG_MEMBERS),
                   "runs": 1, "skipped": 0}
    assert c.get(f"/admin/api/runs/{BILL1}/logs/check").json() == {"ok": True, "bytes": 0, "runs": 0, "skipped": 0}
    for bad in ("%2e%2e", "evil-20261106-100000-ffff", "_uploads"):
        assert c.get(f"/admin/api/runs/{bad}/logs/check").status_code == 404, bad
    _no_temp(tmp_path)


# ---------------------------------------------------------------------------
# remediation.log vs whoever still writes the remediation (W1)
# ---------------------------------------------------------------------------
@pytest.fixture
def job_managers(monkeypatch):
    from sbmig.web import contract as _contract
    monkeypatch.setenv("SBMIG_JOB_POLL_S", "0.05")
    monkeypatch.delenv(_contract.MAX_PARALLEL_ENV, raising=False)
    made: list = []
    yield made
    for mgr in made:
        mgr.shutdown("test teardown")
        for run in list(mgr.runs.values()) + list(mgr.resumes.values()):
            run.done.wait(10)


def _job_admin(tmp_path, repo, cluster, made, **kw):
    """A k8s-job RunManager over the fake cluster, the remediation service over it, and the admin router using both."""
    from fastapi import FastAPI

    from sbmig.remediations.service import RemediationManager

    from . import test_remediation_resume as rr
    (repo / "config" / "default.yml").write_text("x: 1\n")
    mgr = rr._mgr(tmp_path, repo, cluster, made, **kw)
    rem = RemediationManager(tmp_path / "out", repo, mode="k8s-job", run_manager=mgr)
    fa = FastAPI()
    auth = admin.AdminAuth(fail_delay_s=0)
    fa.include_router(admin.create_admin_router(mgr, auth, remediations=rem))
    c = TestClient(fa)
    _login(c)
    return mgr, c


REM_LOG = "remediation/remediation.log"


def _rem_log_in_zip(c: TestClient, rid: str) -> bool:
    return f"{rid}/{REM_LOG}" in _zip(c.get(f"/admin/api/runs/{rid}/logs.zip"))


def test_remediation_log_is_left_out_while_a_resume_pod_runs(tmp_path, repo, job_managers):
    from sbmig.web.runs import resume_job_name

    from . import test_remediation_resume as rr
    out = tmp_path / "out"
    rid = "web-20261111-010000-aaaaaa"
    rr.finished_run(out, rid)
    cluster = rr.ResumeCluster(out, hang=True)
    mgr, c = _job_admin(tmp_path, repo, cluster, job_managers)
    (out / rid / REM_LOG).write_text("IVAS login token=NOT-YET-REDACTED\n")
    assert _rem_log_in_zip(c, rid) is True                      # everything finished: shipped
    mgr.resume_remediation(rid)
    assert rr._wait(lambda: len(cluster.created) == 1) and mgr.remediation_active(rid)
    assert mgr.file_withheld(rid, "remediation.log") is False   # the migration-only rule would have shipped it
    files = _zip(c.get("/admin/api/logs.zip"))
    assert f"{rid}/{REM_LOG}" not in files and f"{rid}/sbmig.log" in files
    assert b"NOT-YET-REDACTED" not in b"".join(files.values())
    cluster.finish(resume_job_name(rid, 2))
    assert rr._wait(lambda: not mgr.remediation_active(rid))
    assert _rem_log_in_zip(c, rid) is True
    _no_temp(tmp_path)


def test_remediation_log_is_left_out_for_a_resume_recovered_after_a_restart(tmp_path, repo, job_managers):
    from sbmig.web.contract import OWNER_LABEL, RESUME_STATE_FILE, RUN_LABEL, ControlState
    from sbmig.web.runs import resume_job_name

    from . import test_remediation_resume as rr
    out = tmp_path / "out"
    rid = "web-20261111-010000-eeeeee"
    rr.finished_run(out, rid)
    (out / rid / REM_LOG).write_text("IVAS login token=NOT-YET-REDACTED\n")
    name = resume_job_name(rid, 2)
    ControlState(run_id=rid, state="running", attempt=2, owner="resume-owner", job=name, slot=2,
                 action=rr.ACTION).write(out / rid, RESUME_STATE_FILE)
    cluster = rr.ResumeCluster(out, phases=["Running"], hang=True)
    cluster.add(name, {RUN_LABEL: rid, OWNER_LABEL: "resume-owner"})
    cluster.manifests[name] = {"metadata": {"name": name, "labels": {RUN_LABEL: rid}},
                               "spec": {"template": {"spec": {"containers": [{"env": [
                                   {"name": contract.ACTION_ENV, "value": rr.ACTION}]}]}}}}
    mgr, c = _job_admin(tmp_path, repo, cluster, job_managers, max_parallel=3)   # the "restarted" web pod
    assert mgr.recovered == [f"{rid}#{rr.ACTION}"]
    try:
        assert _rem_log_in_zip(c, rid) is False
    finally:
        cluster.finish(name)
    assert rr._wait(lambda: not mgr.remediation_active(rid))
    assert _rem_log_in_zip(c, rid) is True


def test_remediation_log_is_left_out_while_the_migration_run_is_not_finished(tmp_path, repo, job_managers):
    from sbmig.web.contract import ControlState

    from . import test_remediation_resume as rr
    out = tmp_path / "out"
    rid = "web-20261111-010000-bbbbbb"
    rr.finished_run(out, rid)
    cluster = rr.ResumeCluster(out, hang=True)
    _, c = _job_admin(tmp_path, repo, cluster, job_managers)
    (out / rid / REM_LOG).write_text("chained remediation still writing\n")
    # the migration Job is still running (its final pass is still to come): written after the manager started, so
    # the manager reads it from disk like a run of another web-pod lifetime
    ControlState(run_id=rid, state="running", owner="mig-owner", job="sbmig-x", slot=1).write(out / rid)
    assert _rem_log_in_zip(c, rid) is False
    ControlState(run_id=rid, state="finished", owner="mig-owner", job="sbmig-x", exit_code=0, slot=1).write(out / rid)
    assert _rem_log_in_zip(c, rid) is True


def test_the_app_uses_the_remediation_services_own_rule_object(tmp_path, repo):
    out = tmp_path / "out"
    _shop_runs(out)
    app, _ = _app(tmp_path, repo)
    c = TestClient(app)
    _login(c)
    assert _rem_log_in_zip(c, SHOP1) is True
    asked = []
    app.state.remediations._withheld = lambda rid: asked.append(rid) or True
    assert _rem_log_in_zip(c, SHOP1) is False and asked == [SHOP1]
    app.state.remediations._withheld = lambda rid: 1 / 0          # an error withholds
    assert _rem_log_in_zip(c, SHOP1) is False
    assert admin.remediation_rule(None)(SHOP1) is True            # no rule reachable: withhold
    _no_temp(tmp_path)


# ---------------------------------------------------------------------------
# GET /admin
# ---------------------------------------------------------------------------
def test_admin_page_is_the_static_file_or_a_clear_404(tmp_path, repo, monkeypatch):
    app, _ = _app(tmp_path, repo)
    static = tmp_path / "static"
    static.mkdir()
    monkeypatch.setattr(app_module, "STATIC_DIR", static)
    c = TestClient(app)
    r = c.get("/admin")
    assert r.status_code == 404 and "admin.html is missing" in r.text
    (static / "admin.html").write_text("<!doctype html><title>admin</title>", encoding="utf-8")
    r = c.get("/admin")
    assert r.status_code == 200 and r.text.startswith("<!doctype html>")
    assert r.headers["content-type"].startswith("text/html") and r.headers["cache-control"] == "no-store"


def test_mixed_currencies_give_no_sum_and_no_currency(tmp_path):
    out = tmp_path / "out"
    _shop_runs(out)
    rep = json.loads((out / SHOP2 / "report.json").read_text())
    rep["llm_cost"]["currency"] = "KRW"
    (out / SHOP2 / "report.json").write_text(json.dumps(rep))
    data = admin.collect_usage(out)
    assert data["totals"]["currency"] is None and data["totals"]["cost"] is None
    assert data["totals"]["unpriced_runs"] == 0
    assert data["projects"][0]["cost"] is None and all(m["cost"] is None for m in data["by_model"])
    assert [r["cost"] for r in data["projects"][0]["run_list"]] == [0.05, 0.5]   # each run keeps its own price
