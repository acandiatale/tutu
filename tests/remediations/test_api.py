"""/api/remediations (sbmig/remediations/api.py) over Starlette's TestClient with fake components: status codes and
response shapes of API.md. No IVAS, Nexus, Maven or network; tmp_path only."""
from __future__ import annotations

import shutil

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sbmig.core.config import PACKAGE_ROOT, load_config
from sbmig.remediations import planner as planner_mod
from sbmig.remediations import service as service_mod
from sbmig.remediations.api import create_router
from sbmig.remediations.models import AWAITING_APPROVAL, D_APPROVED, SUCCEEDED

from .fakes import DEFAULT_FILES, FakeMaven, deterministic_zip
from .test_service import WAIT, Factory, Gate, manager, one_fix

STATUS_KEYS = {"id", "name", "status", "reason", "reasons", "message", "node", "next_node", "round", "counts", "created",
               "updated", "stop_requested", "pending_approvals", "delivery", "files", "links"}


@pytest.fixture(scope="module")
def base_cfg():
    cfg = load_config(PACKAGE_ROOT / "config" / "artifacts.local-e2e.yml")
    # the operator's "trusted projects" confirmation is a readiness condition (default false)
    return cfg.model_copy(update={"remediation": cfg.remediation.model_copy(update={"trusted_projects": True})})


@pytest.fixture(autouse=True)
def ivas_env(monkeypatch):
    monkeypatch.setenv("IVAS_BASE_URL", "https://ivas.invalid")
    monkeypatch.setenv("IVAS_USER_ID", "tester")
    monkeypatch.setenv("IVAS_PASSWORD", "pw-for-tests-only")


def make_client(mgr, limit=10 * 1024 * 1024) -> TestClient:
    app = FastAPI()
    app.include_router(create_router(mgr, limit))
    return TestClient(app)


ZIP = deterministic_zip({k: v.encode() for k, v in DEFAULT_FILES.items()})


def post_zip(client, body=ZIP, query="name=app.zip"):
    return client.post(f"/api/remediations?{query}", content=body, headers={"Content-Type": "application/zip"})


def started(client, mgr, wait=True):
    r = post_zip(client)
    assert r.status_code == 202, r.text
    rid = r.json()["id"]
    if wait:
        assert mgr.wait(rid, WAIT)
    return rid


# ---------------------------------------------------------------------------- upload
def test_post_returns_202_and_runs(tmp_path, base_cfg):
    mgr = manager(tmp_path, base_cfg)
    c = make_client(mgr)
    r = post_zip(c)
    assert r.status_code == 202
    body = r.json()
    assert set(body) == {"id", "status", "url"} and body["url"] == f"/api/remediations/{body['id']}"
    assert body["status"] in ("QUEUED", "RUNNING", SUCCEEDED)
    assert mgr.wait(body["id"], WAIT)
    st = c.get(body["url"])
    assert st.status_code == 200 and STATUS_KEYS <= set(st.json())
    assert st.json()["status"] == SUCCEEDED
    assert str(tmp_path) not in st.text


def test_post_rejects_bad_bodies(tmp_path, base_cfg):
    mgr = manager(tmp_path, base_cfg)
    c = make_client(mgr, limit=1024)
    r = post_zip(c, body=b"hello world")
    assert r.status_code == 400 and r.json()["code"] == "not_zip"
    r = post_zip(c, body=b"")
    assert r.status_code == 400
    r = post_zip(c, body=b"PK" + b"x" * 2048)                         # Content-Length pre-check
    assert r.status_code == 413 and r.json()["code"] == "too_large"

    def chunks():
        yield b"PK"
        for _ in range(4):
            yield b"x" * 512
    r = c.post("/api/remediations?name=a.zip", content=chunks())      # chunked: no Content-Length
    assert r.status_code == 413
    r = post_zip(c, body=b"PK garbage")                                # IntakeRejected
    assert r.status_code == 400 and r.json()["code"] == "not_zip" and r.json()["error"]
    assert not list((tmp_path / "out" / "_remediations" / ".incoming").glob("*"))
    assert mgr.list()["total"] == 0


def test_post_not_ready_is_503_with_names(tmp_path, base_cfg, monkeypatch):
    monkeypatch.delenv("IVAS_USER_ID")
    mgr = manager(tmp_path, base_cfg)
    c = make_client(mgr)
    r = post_zip(c)
    assert r.status_code == 503
    body = r.json()
    assert body["code"] == "not_ready" and "IVAS_USER_ID is not set" in body["missing"]
    assert "tester" not in r.text and "pw-for-tests-only" not in r.text
    r = c.get("/api/remediations/readiness")
    assert r.status_code == 200 and r.json() == {"ready": False, "missing": ["IVAS_USER_ID is not set"]}


def test_post_unknown_config_is_400(tmp_path, base_cfg):
    c = make_client(manager(tmp_path, base_cfg))
    r = post_zip(c, query="name=a.zip&configs=nope.yml")
    assert r.status_code == 400 and r.json()["code"] == "bad_request"


# ---------------------------------------------------------------------------- reads
def test_readiness_route_is_not_taken_for_an_id(tmp_path, base_cfg):
    c = make_client(manager(tmp_path, base_cfg))
    r = c.get("/api/remediations/readiness")
    assert r.status_code == 200 and r.json() == {"ready": True, "missing": []}


def test_list_status_ids(tmp_path, base_cfg):
    mgr = manager(tmp_path, base_cfg)
    c = make_client(mgr)
    a, b = started(c, mgr), started(c, mgr)
    r = c.get("/api/remediations?page=1&size=1")
    body = r.json()
    assert r.status_code == 200 and set(body) == {"items", "page", "size", "total"}
    assert body["total"] == 2 and [x["id"] for x in body["items"]] == [b]
    assert c.get("/api/remediations?size=1000").json()["size"] == 100
    assert c.get("/api/remediations/r20000101-000000-abcdef").status_code == 404
    bad = c.get("/api/remediations/not-an-id")
    assert bad.status_code == 400 and bad.json()["code"] == "bad_id"
    assert c.get(f"/api/remediations/{a}").json()["links"]["self"] == f"/api/remediations/{a}"


def test_result_proposals_approvals_resume(tmp_path, base_cfg):
    mgr = manager(tmp_path, base_cfg, one_fix(cfg=base_cfg))
    c = make_client(mgr)
    rid = started(c, mgr)
    st = c.get(f"/api/remediations/{rid}").json()
    assert st["status"] == AWAITING_APPROVAL and st["pending_approvals"] == 1
    r = c.get(f"/api/remediations/{rid}/result")
    assert r.status_code == 409 and r.json()["code"] == "not_finished" and r.json()["status"] == AWAITING_APPROVAL

    props = c.get(f"/api/remediations/{rid}/proposals").json()
    assert set(props) >= {"status", "round", "source_tree_hash", "policy_hash", "proposals", "residuals"}
    assert props["policy_hash"] == planner_mod.policy_hash(base_cfg)
    p = props["proposals"][0]
    assert {"diff", "proposal_hash", "source_tree_hash", "policy_hash", "decision"} <= set(p)
    assert c.get(f"/api/remediations/{rid}/proposals?all=1").json()["proposals"][0]["id"] == p["id"]

    r = c.post(f"/api/remediations/{rid}/resume")
    assert r.status_code == 409 and r.json()["code"] == "state_conflict"
    dec = {"proposal_id": p["id"], "decision": D_APPROVED, "proposal_hash": p["proposal_hash"],
           "source_tree_hash": p["source_tree_hash"], "policy_hash": p["policy_hash"], "diff": p["diff"], "comment": "c"}
    r = c.post(f"/api/remediations/{rid}/approvals", json={"by": "alice", "decisions": [{**dec, "policy_hash": "x"}]})
    assert r.status_code == 409 and r.json()["code"] == "approval_mismatch" and "policy_hash" in r.json()["fields"]
    assert c.post(f"/api/remediations/{rid}/approvals", content=b"{not json").status_code == 400
    assert c.post(f"/api/remediations/{rid}/approvals", json={"decisions": "x"}).status_code == 400
    assert c.post(f"/api/remediations/{rid}/approvals", json={"decisions": [{"decision": "approved"}]}).status_code == 400
    r = c.post(f"/api/remediations/{rid}/approvals", json={"by": "alice", "decisions": [dec]})
    assert r.status_code == 200 and r.json() == {"accepted": [p["id"]], "pending": 0}

    r = c.post(f"/api/remediations/{rid}/resume", json={})
    assert r.status_code == 202 and STATUS_KEYS <= set(r.json())
    assert mgr.wait(rid, WAIT)
    r = c.get(f"/api/remediations/{rid}/result")
    assert r.status_code == 200 and r.json()["state"]["status"] == SUCCEEDED
    r = c.post(f"/api/remediations/{rid}/approvals", json={"by": "bob", "decisions": [dec]})
    assert r.status_code == 409 and r.json()["code"] == "state_conflict"
    assert c.post(f"/api/remediations/{rid}/resume").status_code == 409


def test_stop_and_delete(tmp_path, base_cfg):
    gate = Gate()
    mgr = manager(tmp_path, base_cfg, Factory(maven=FakeMaven(on_package=gate)))
    c = make_client(mgr)
    rid = started(c, mgr, wait=False)
    assert gate.entered.wait(WAIT)
    r = c.delete(f"/api/remediations/{rid}")
    assert r.status_code == 409 and r.json()["code"] == "state_conflict"
    r = c.post(f"/api/remediations/{rid}/stop")
    assert r.status_code == 202 and r.json()["stop_requested"] is True and "not cancelled" in r.json()["note"]
    gate.release.set()
    assert mgr.wait(rid, WAIT)
    assert c.get(f"/api/remediations/{rid}").json()["status"] == "STOPPED"
    assert c.post(f"/api/remediations/{rid}/stop").status_code == 409
    r = c.delete(f"/api/remediations/{rid}")
    assert r.status_code == 204 and not (tmp_path / "out" / "_remediations" / rid).exists()
    assert c.delete(f"/api/remediations/{rid}").status_code == 404


def test_files_whitelist(tmp_path, base_cfg):
    mgr = manager(tmp_path, base_cfg, one_fix(decision="auto", cfg=base_cfg))
    c = make_client(mgr)
    rid = started(c, mgr)
    r = c.get(f"/api/remediations/{rid}/files/report.md")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    r = c.get(f"/api/remediations/{rid}/files/result.zip")
    assert r.status_code == 200 and r.content[:2] == b"PK" and "attachment" in r.headers["content-disposition"]
    assert c.get(f"/api/remediations/{rid}/files/plan-r1.json").status_code == 200
    for name in ("state.json", "%2E%2E", "approvals.jsonl", "plan-r9.json", "..%2Fstate.json"):
        assert c.get(f"/api/remediations/{rid}/files/{name}").status_code == 404, name
    assert c.get("/api/remediations/r20000101-000000-abcdef/files/report.md").status_code == 404
    assert c.get("/api/remediations/bad/files/report.md").status_code == 400


# ---------------------------------------------------------------------------- the real web app
def test_web_app_includes_the_router_and_keeps_api_runs(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    shutil.copy(PACKAGE_ROOT / "config" / "artifacts.local-e2e.yml", repo / "config" / "artifacts.local-e2e.yml")
    monkeypatch.delenv("KUBECONFIG", raising=False)
    monkeypatch.setenv("SBMIG_REMEDIATION__TRUSTED_PROJECTS", "true")      # the operator's confirmation, as the manifest sets it
    factory = Factory()
    monkeypatch.setattr(service_mod, "default_components_factory", factory)
    from sbmig.web import create_app
    app = create_app(tmp_path / "out", repo)
    with TestClient(app) as c:
        r = c.get("/api/remediations/readiness")
        assert r.status_code == 200 and r.json() == {"ready": True, "missing": []}
        assert c.get("/api/remediations").json()["total"] == 0
        assert c.get("/api/runs").status_code == 200
        assert c.get("/api/configs").status_code == 200
        rid = started(c, app.state.remediations)
        assert c.get(f"/api/remediations/{rid}").json()["status"] == SUCCEEDED
        # the migration run list does not see the remediation directory
        assert all(not x.get("run_id", "").startswith("_") for x in c.get("/api/runs").json()["runs"])


def test_web_app_token_guards_the_routes(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    monkeypatch.setattr(service_mod, "default_components_factory", Factory())
    from sbmig.web import create_app
    c = TestClient(create_app(tmp_path / "out", repo, token="t0ken-for-tests"))
    assert c.get("/api/remediations/readiness").status_code == 401
    ok = c.get("/api/remediations/readiness", headers={"Authorization": "Bearer t0ken-for-tests"})
    assert ok.status_code == 200


def test_restart_between_apps_interrupts(tmp_path, base_cfg):
    gate = Gate()
    f = Factory(maven=FakeMaven(on_package=gate))
    mgr = manager(tmp_path, base_cfg, f)
    c = make_client(mgr)
    rid = started(c, mgr, wait=False)
    assert gate.entered.wait(WAIT)
    mgr2 = manager(tmp_path, base_cfg, Factory())       # "the server restarted" (another owner token)
    c2 = make_client(mgr2)
    st = c2.get(f"/api/remediations/{rid}").json()
    assert st["status"] == "INTERRUPTED" and "restarted" in st["message"]
    gate.release.set()
    assert mgr.wait(rid, WAIT)
    assert mgr2.wait_idle(1.0)                          # mgr2 never ran anything
