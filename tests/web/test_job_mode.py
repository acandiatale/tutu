"""2026-11-05 (k8s Job dispatch, the user's decision: everything on one shared PVC): in `SBMIG_WEB_MODE=k8s-job` the web pod
stores the upload as `<out>/<run id>/source.zip`, creates a Job from the template with the run's env, pushes `node` events
named `job` while the pod is created, reads the pod's journal (`graph_node_end` → `node` events), log and reports from the same
run dir, and ends on `job-status.json` / the Job's terminal state. A stop writes `stop.request`; after the grace period the Job
is deleted. No migration runs in this process."""
from __future__ import annotations

import base64
import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from sbmig.core.config import PACKAGE_ROOT
from sbmig.web import create_app
from sbmig.web.k8s import NAMESPACE_ENV, JobState, K8sClient, K8sError, job_name, render_job
from sbmig.web.runs import RunManager

from .test_app import _manager, _sse, _start


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    shutil.copy(PACKAGE_ROOT / "config" / "artifacts.local-e2e.yml", repo / "config" / "artifacts.local-e2e.yml")
    (repo / "config" / "default.yml").write_text("x: 1\n")
    return repo

TEMPLATE = Path(__file__).resolve().parents[2] / "deploy" / "k8s" / "81-web-job.template.yaml"


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


class FakePod:
    """Writes what the Job pod would write into the shared run dir, after the Job turned Running."""

    def __init__(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code

    def run(self, run_dir: Path) -> None:
        with open(run_dir / "journal.jsonl", "a", encoding="utf-8") as jf:
            for node, phase, extra in (("intake", "intake", {}), ("analyze", "analyze", {}),
                                       ("plan", "plan", {"phases": ["verify"]}), ("phase", "verify", {"phases": []}),
                                       ("finalize", None, {})):
                jf.write(json.dumps({"ts": _ts(), "event": "graph_node", "node": node, "phase": phase}) + "\n")
                jf.write(json.dumps({"ts": _ts(), "event": "graph_node_end", "node": node, "phase": phase, "aborted": None,
                                     "stopped": False, **extra}) + "\n")
        (run_dir / "sbmig.log").write_text("2026-11-05 22:00:00,000 INFO [verify] build ok\nSBMIG-RESULT ok\n", encoding="utf-8")
        (run_dir / "report.md").write_text("# report\n\nBUILD SUCCESS\n", encoding="utf-8")
        (run_dir / "report.json").write_text(json.dumps({"success": True, "blocked": [], "unresolved": [],
                                                         "llm_usage": {"calls": 2}}), encoding="utf-8")
        (run_dir / "job-status.json").write_text(json.dumps({"exit_code": self.exit_code, "finished_at": _ts()}), encoding="utf-8")


class FakeJobClient:
    """Scripted Job states; the pod "runs" when the state reaches Running."""
    namespace = "sbmig-ns"

    def __init__(self, phases: list[str] | None = None, pod: FakePod | None = None, out_dir: Path | None = None):
        self.phases = list(phases or ["Creating", "Pending", "Running"])
        self.pod = pod
        self.out_dir = out_dir
        self.created: list[dict] = []
        self.deleted: list[str] = []
        self.states_served = 0

    def create_job(self, manifest: dict) -> str:
        self.created.append(manifest)
        return manifest["metadata"]["name"]

    def job_state(self, name: str) -> JobState:
        self.states_served += 1
        phase = self.phases.pop(0) if len(self.phases) > 1 else self.phases[0]
        if phase == "Running" and self.pod is not None and self.out_dir is not None:
            run_id = next(e["value"] for e in self.created[-1]["spec"]["template"]["spec"]["containers"][0]["env"]
                          if e["name"] == "SBMIG_RUN_ID")
            self.pod.run(self.out_dir / run_id)
            self.pod = None                                  # once
        return JobState(phase, pod=f"{name}-x1" if phase != "Creating" else None,
                        exit_code=0 if phase == "Succeeded" else None,
                        message="ContainerCreating" if phase == "Pending" else "")

    def delete_job(self, name: str) -> None:
        self.deleted.append(name)
        self.phases = ["Missing"]


@pytest.fixture
def job_client(tmp_path: Path) -> FakeJobClient:
    return FakeJobClient(pod=FakePod(), out_dir=tmp_path / "out")


@pytest.fixture
def client(tmp_path: Path, repo: Path, job_client: FakeJobClient, monkeypatch) -> TestClient:
    monkeypatch.setenv("SBMIG_JOB_POLL_S", "0.05")
    monkeypatch.setenv("SBMIG_JOB_TEMPLATE", str(TEMPLATE))
    return TestClient(create_app(tmp_path / "out", repo, mode="k8s-job", job_client=job_client))


def test_upload_creates_a_job_and_the_ui_sees_pod_then_nodes_then_files(client: TestClient, job_client: FakeJobClient, tmp_path: Path):
    rid = _start(client, "name=my%20proj.zip&configs=artifacts.local-e2e.yml&stop_after=verify")
    events = _sse(client, rid)
    kinds = [k for k, _ in events]
    assert kinds[0] == "status" and kinds[-1] == "end" and "report" in kinds
    final_status = [d for k, d in events if k == "status"][-1]
    assert final_status["state"] == "finished" and final_status["exit_code"] == 0
    # the Job: one manifest from the template with the run's env; the zip in the run dir, not under _uploads
    assert len(job_client.created) == 1
    m = job_client.created[0]
    assert m["metadata"]["name"] == job_name(rid) and m["metadata"]["namespace"] == "sbmig-ns"
    env = {e["name"]: e.get("value") for e in m["spec"]["template"]["spec"]["containers"][0]["env"]}   # valueFrom entries → None
    run_dir = tmp_path / "out" / rid
    assert env["SBMIG_SOURCE_ZIP"] == str(run_dir / "source.zip") and env["SBMIG_OUT"] == str(tmp_path / "out")
    assert env["SBMIG_RUN_ID"] == rid and env["SBMIG_CONFIG"].endswith("config/artifacts.local-e2e.yml")
    assert env["SBMIG_STOP_AFTER"] == "verify" and env["SBMIG_KB_DIR"] == "/data/kb-v2"      # template value kept
    assert (run_dir / "source.zip").is_file() and not list((tmp_path / "out" / "_uploads").glob("*.zip"))
    # the pod states, then the pod's nodes (from graph_node_end), in order
    nodes = [d for k, d in events if k == "node"]
    job_states = [d["update"]["state"] for d in nodes if d["node"] == "job"]
    assert job_states[:3] == ["created", "creating", "pending"] and "running" in job_states
    assert [d["node"] for d in nodes if d["node"] != "job"] == ["intake", "analyze", "plan", "phase", "finalize"]
    plan = next(d for d in nodes if d["node"] == "plan")
    assert plan["update"]["phases"] == ["verify"]
    run = _manager(client).runs[rid]
    assert ("finished", {"exit_code": 0, "success": True}) in run.history
    st = client.get(f"/api/runs/{rid}").json()
    assert st["state"] == "finished" and st["mode"] == "k8s-job" and st["job"]["name"] == job_name(rid)
    assert st["exit_code"] == 0 and "report.md" in st["files"] and st["llm_calls"] == 2
    assert client.get(f"/api/runs/{rid}/files/report.md").text.startswith("# report")
    # the web wrote only its own journal lines; the migration ran "in the pod"
    journal = [json.loads(ln) for ln in (run_dir / "journal.jsonl").read_text(encoding="utf-8").splitlines()]
    assert journal[0]["event"] == "web_run" and journal[0]["mode"] == "k8s-job" and journal[1]["event"] == "job_created"
    assert any(e["event"] == "graph_node_end" for e in journal)


def test_stop_writes_the_request_file_then_deletes_after_the_grace_period(tmp_path: Path, repo: Path, monkeypatch):
    monkeypatch.setenv("SBMIG_JOB_POLL_S", "0.05")
    monkeypatch.setenv("SBMIG_JOB_STOP_GRACE_S", "0.2")
    jc = FakeJobClient(phases=["Pending", "Running"], pod=None)        # the pod never finishes on its own
    mgr = RunManager(tmp_path / "out", repo, mode="k8s-job", job_client=jc, job_template=TEMPLATE)
    rid = mgr.start(b"PK\x05\x06" + b"\0" * 18, "p.zip", ["artifacts.local-e2e.yml"])
    deadline = time.time() + 5
    while mgr.status(rid)["state"] != "running" and time.time() < deadline:
        time.sleep(0.02)
    assert mgr.stop(rid)
    run = mgr.runs[rid]
    assert run.done.wait(5)
    assert (tmp_path / "out" / rid / "stop.request").is_file() and jc.deleted == [job_name(rid)]
    assert run.exit_code == 3 and "deleted after the grace period" in (run.error or "")
    st = mgr.status(rid)
    assert st["state"] == "finished" and st["stop_requested"] is True


def test_shutdown_and_stop_never_create_a_queued_job(tmp_path: Path, repo: Path, monkeypatch):
    """Review 2026-11-05: the shutdown cascaded through the FIFO and created every queued Job; a stop on a queued run created the
    Job only to delete it. Neither may reach create_job."""
    monkeypatch.setenv("SBMIG_JOB_POLL_S", "0.05")
    jc = FakeJobClient(phases=["Running"], pod=None)
    mgr = RunManager(tmp_path / "out", repo, mode="k8s-job", job_client=jc, job_template=TEMPLATE)
    first = mgr.start(b"PK\x05\x06" + b"\0" * 18, "a.zip", ["artifacts.local-e2e.yml"])
    queued = [mgr.start(b"PK\x05\x06" + b"\0" * 18, f"q{i}.zip", ["artifacts.local-e2e.yml"]) for i in range(3)]
    deadline = time.time() + 5
    while mgr.status(first)["state"] != "running" and time.time() < deadline:
        time.sleep(0.02)
    assert mgr.stop(queued[0])                         # a stop while queued
    mgr.shutdown()
    for rid in [first, *queued]:
        assert mgr.runs[rid].done.wait(5)
    assert len(jc.created) == 1                         # only the running one ever had a Job
    assert "stopped before the job was created" in (mgr.runs[queued[0]].error or "")
    assert all("shut down before the job was created" in (mgr.runs[r].error or "") for r in queued[1:])


def test_a_transient_api_error_does_not_end_the_run(tmp_path: Path, repo: Path, monkeypatch):
    monkeypatch.setenv("SBMIG_JOB_POLL_S", "0.05")
    monkeypatch.setenv("SBMIG_JOB_API_GRACE_S", "5")
    jc = FakeJobClient(pod=FakePod(), out_dir=tmp_path / "out")
    calls = {"n": 0}
    real = jc.job_state

    def flaky(name):
        calls["n"] += 1
        if calls["n"] in (2, 3):
            raise K8sError("GET jobs: HTTP 503: apiserver busy")
        return real(name)
    jc.job_state = flaky
    mgr = RunManager(tmp_path / "out", repo, mode="k8s-job", job_client=jc, job_template=TEMPLATE)
    rid = mgr.start(b"PK\x05\x06" + b"\0" * 18, "p.zip", ["artifacts.local-e2e.yml"])
    assert mgr.runs[rid].done.wait(10) and mgr.runs[rid].exit_code == 0 and mgr.runs[rid].error is None
    # a persistent outage ends the run after the grace
    monkeypatch.setenv("SBMIG_JOB_API_GRACE_S", "0.1")
    jc2 = FakeJobClient(phases=["Running"], pod=None)
    jc2.job_state = lambda name: (_ for _ in ()).throw(K8sError("GET jobs: HTTP 503"))
    mgr2 = RunManager(tmp_path / "out2", repo, mode="k8s-job", job_client=jc2, job_template=TEMPLATE)
    rid2 = mgr2.start(b"PK\x05\x06" + b"\0" * 18, "p.zip", ["artifacts.local-e2e.yml"])
    assert mgr2.runs[rid2].done.wait(10) and mgr2.runs[rid2].exit_code == 3 and "HTTP 503" in (mgr2.runs[rid2].error or "")


def test_a_job_of_an_earlier_server_process_is_still_running(tmp_path: Path, repo: Path):
    mgr = RunManager(tmp_path / "out", repo, mode="k8s-job", job_client=FakeJobClient(), job_template=TEMPLATE)
    rd = tmp_path / "out" / "web-old"
    rd.mkdir(parents=True)
    now = datetime.now(timezone.utc).isoformat()
    (rd / "journal.jsonl").write_text(json.dumps({"ts": now, "event": "web_run", "mode": "k8s-job"}) + "\n"
                                      + json.dumps({"ts": now, "event": "job_created", "job": "sbmig-web-old"}) + "\n"
                                      + json.dumps({"ts": now, "event": "graph_node_end", "node": "analyze"}) + "\n", encoding="utf-8")
    st = mgr.status("web-old")
    assert st["state"] == "running" and st["job"]["name"] == "sbmig-web-old" and st["node"] == "analyze"
    # finished on the volume → finished; too old without any status → unknown
    (rd / "job-status.json").write_text(json.dumps({"exit_code": 2, "finished_at": now}), encoding="utf-8")
    assert mgr.status("web-old")["state"] == "finished" and mgr.status("web-old")["exit_code"] == 2
    (rd / "job-status.json").unlink()
    old = "2020-01-01T00:00:00+00:00"
    (rd / "journal.jsonl").write_text(json.dumps({"ts": old, "event": "job_created", "job": "sbmig-web-old"}) + "\n", encoding="utf-8")
    assert mgr.status("web-old")["state"] == "unknown"


def test_inside_a_cluster_without_a_service_account_refuses_to_start(tmp_path: Path, repo: Path, monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    import sbmig.web.runs as runs_mod
    monkeypatch.setattr(runs_mod, "in_cluster_available", lambda: False)
    with pytest.raises(ValueError, match="service account"):
        RunManager(tmp_path / "out", repo, mode="k8s-job", job_template=TEMPLATE)


def test_server_shutdown_leaves_the_job_running(tmp_path: Path, repo: Path, monkeypatch):
    monkeypatch.setenv("SBMIG_JOB_POLL_S", "0.05")
    jc = FakeJobClient(phases=["Running"], pod=None)
    mgr = RunManager(tmp_path / "out", repo, mode="k8s-job", job_client=jc, job_template=TEMPLATE)
    rid = mgr.start(b"PK\x05\x06" + b"\0" * 18, "p.zip", ["artifacts.local-e2e.yml"])
    deadline = time.time() + 5
    while mgr.status(rid)["state"] != "running" and time.time() < deadline:
        time.sleep(0.02)
    info = mgr.shutdown()
    assert info["stopped"] == [] and mgr.runs[rid].done.wait(5)
    assert jc.deleted == [] and not (tmp_path / "out" / rid / "stop.request").exists()
    assert "continues in the cluster" in (mgr.runs[rid].error or "")


def test_outside_a_cluster_the_run_fails_readably(tmp_path: Path, repo: Path, monkeypatch):
    """A pod that says it is in a cluster (KUBERNETES_SERVICE_HOST) but has no usable service account: the run fails readably
    (outside a cluster the mode degrades to local-job instead — tests/web/test_local_job_mode.py)."""
    import sbmig.web.runs as runs_mod
    monkeypatch.setattr(runs_mod, "in_cluster_available", lambda: True)              # no degrade …
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)                        # … but in_cluster() cannot build a client
    mgr = RunManager(tmp_path / "out", repo, mode="k8s-job", job_template=TEMPLATE)
    rid = mgr.start(b"PK\x05\x06" + b"\0" * 18, "p.zip", ["artifacts.local-e2e.yml"])
    assert mgr.runs[rid].done.wait(5)
    assert mgr.runs[rid].exit_code == 3 and "not inside a Kubernetes cluster" in (mgr.runs[rid].error or "")
    assert any(k == "error" for k, _ in mgr.runs[rid].history)


def test_bad_mode_is_rejected(tmp_path: Path, repo: Path):
    with pytest.raises(ValueError):
        RunManager(tmp_path / "out", repo, mode="docker")


# ---- the client and the template helpers
def test_job_name_and_render_job():
    assert job_name("web-20261105-213045-abc123") == "sbmig-web-20261105-213045-abc123"
    assert job_name("Run_ID.With Caps") == "sbmig-run-id-with-caps" and len(job_name("x" * 80)) <= 63
    job = render_job(TEMPLATE, "web-1", {"SBMIG_RUN_ID": "web-1", "SBMIG_KB_DIR": "/other/kb"}, namespace="ns1")
    assert job["metadata"]["name"] == "sbmig-web-1" and job["metadata"]["namespace"] == "ns1"
    assert "generateName" not in job["metadata"] and job["metadata"]["labels"]["sbmig-run-id"] == "web-1"
    env = {e["name"]: e.get("value") for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}   # valueFrom entries → None
    assert env["SBMIG_KB_DIR"] == "/other/kb" and env["SBMIG_RUN_ID"] == "web-1" and env["SBMIG_FAIL_JOB_ON_BUILD_FAIL"] == "0"
    assert job["spec"]["template"]["metadata"]["labels"]["sbmig-run-id"] == "web-1"
    with pytest.raises(K8sError):
        render_job({"kind": "Deployment"}, "x", {})


class _Resp:
    def __init__(self, status_code: int, body: dict | None = None):
        self.status_code = status_code
        self._body = body or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class _Session:
    def __init__(self, routes):
        self.routes = routes
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        key = (method, url.split("?")[0])
        hit = self.routes.get(key)
        if callable(hit):
            return hit(kw)
        return hit if hit is not None else _Resp(404, {"message": "not found"})


def test_k8s_client_three_calls(tmp_path: Path):
    base = "https://10.0.0.1:443"
    jobs = f"{base}/apis/batch/v1/namespaces/ns/jobs"
    pods = f"{base}/api/v1/namespaces/ns/pods"
    state = {"job": {"status": {}}, "pods": {"items": []}}
    routes = {("POST", jobs): _Resp(201, {"metadata": {"name": "sbmig-r1"}}),
              ("GET", f"{jobs}/sbmig-r1"): lambda kw: _Resp(200, state["job"]),
              ("GET", pods): lambda kw: _Resp(200, state["pods"]),
              ("DELETE", f"{jobs}/sbmig-r1"): _Resp(200, {})}
    s = _Session(routes)
    c = K8sClient(base, "tok", "ns", verify="/ca.crt", session=s)
    assert c.create_job({"metadata": {"name": "sbmig-r1"}}) == "sbmig-r1"
    assert s.calls[0][2]["headers"]["Authorization"] == "Bearer tok" and s.calls[0][2]["verify"] == "/ca.crt"
    assert c.job_state("sbmig-r1").phase == "Creating"
    state["pods"] = {"items": [{"metadata": {"name": "sbmig-r1-abc", "creationTimestamp": "t1"},
                                "status": {"phase": "Pending", "containerStatuses": [{"state": {"waiting": {"reason": "ImagePullBackOff", "message": "x"}}}]}}]}
    st = c.job_state("sbmig-r1")
    assert (st.phase, st.pod, st.message) == ("Pending", "sbmig-r1-abc", "ImagePullBackOff x")
    state["pods"]["items"][0]["status"] = {"phase": "Running", "containerStatuses": [{"state": {"running": {}}}]}
    assert c.job_state("sbmig-r1").phase == "Running"
    state["pods"]["items"][0]["status"] = {"phase": "Succeeded", "containerStatuses": [{"state": {"terminated": {"exitCode": 2, "reason": "Error"}}}]}
    state["job"] = {"status": {"succeeded": 1}}
    st = c.job_state("sbmig-r1")
    assert st.phase == "Succeeded" and st.exit_code == 2 and st.finished
    state["job"] = {"status": {"failed": 1, "conditions": [{"type": "Failed", "reason": "DeadlineExceeded", "message": "too long"}]}}
    st = c.job_state("sbmig-r1")
    assert st.phase == "Failed" and st.message == "DeadlineExceeded too long"
    c.delete_job("sbmig-r1")
    assert s.calls[-1][0] == "DELETE" and s.calls[-1][2]["json"]["propagationPolicy"] == "Foreground"
    assert c.job_state("sbmig-nope").phase == "Missing"
    s.routes[("GET", f"{jobs}/sbmig-r1")] = _Resp(403, {"message": "forbidden"})
    with pytest.raises(K8sError, match="HTTP 403"):
        c.job_state("sbmig-r1")
    with pytest.raises(K8sError, match="not inside a Kubernetes cluster"):
        K8sClient.in_cluster()


# ---- 2026-11-09: the infra team's agent model — a kubeconfig Secret at /etc/kube/config instead of a service account

def _kubeconfig(tmp_path: Path, user: dict, cluster_extra: dict | None = None, ns: str | None = "agent") -> Path:
    ca = base64.b64encode(b"-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----\n").decode("ascii")
    cluster = {"server": "https://k8s.example:6443", "certificate-authority-data": ca}
    cluster.update(cluster_extra or {})
    if "certificate-authority" in cluster or cluster.get("insecure-skip-tls-verify"):
        cluster.pop("certificate-authority-data", None)
    ctx: dict = {"cluster": "c1", "user": "u1"}
    if ns:
        ctx["namespace"] = ns
    doc = {"apiVersion": "v1", "kind": "Config", "current-context": "x1",
           "clusters": [{"name": "c1", "cluster": cluster}], "users": [{"name": "u1", "user": user}],
           "contexts": [{"name": "x1", "context": ctx}]}
    p = tmp_path / "kube" / "config"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return p


def test_a_kubeconfig_with_a_token_user_is_preferred_over_the_service_account(tmp_path: Path, monkeypatch):
    monkeypatch.delenv(NAMESPACE_ENV, raising=False)
    monkeypatch.setenv("KUBECONFIG", str(_kubeconfig(tmp_path, {"token": "t0k"})))
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")           # a pod, but the explicit kubeconfig wins
    s = _Session({})
    c = K8sClient.connect(session=s)
    assert c.base_url == "https://k8s.example:6443" and c.namespace == "agent"       # the context's namespace
    assert c._headers["Authorization"] == "Bearer t0k" and c.cert is None
    assert isinstance(c.verify, str) and Path(c.verify).read_text().startswith("-----BEGIN CERTIFICATE-----")
    assert c.source.startswith("kubeconfig ") and "context x1" in c.source
    assert c.job_state("sbmig-x").phase == "Missing"
    assert s.calls[0][2]["verify"] == c.verify and "cert" not in s.calls[0][2]
    monkeypatch.setenv(NAMESPACE_ENV, "other")                          # the env still wins over the context
    assert K8sClient.connect(session=_Session({})).namespace == "other"


def test_a_kubeconfig_with_a_client_certificate_user(tmp_path: Path, monkeypatch):
    monkeypatch.delenv(NAMESPACE_ENV, raising=False)
    crt, key = base64.b64encode(b"CRT").decode(), base64.b64encode(b"KEY").decode()
    path = _kubeconfig(tmp_path, {"client-certificate-data": crt, "client-key-data": key}, ns=None)
    s = _Session({})
    c = K8sClient.from_kubeconfig(path, session=s)
    assert c.namespace == "default" and "Authorization" not in c._headers
    assert c.cert is not None and Path(c.cert[0]).read_text() == "CRT" and Path(c.cert[1]).read_text() == "KEY"
    assert (Path(c.cert[1]).stat().st_mode & 0o777) == 0o600
    c.job_state("sbmig-x")
    assert s.calls[0][2]["cert"] == c.cert


def test_a_kubeconfig_with_relative_ca_path_insecure_or_exec(tmp_path: Path):
    (tmp_path / "kube").mkdir(parents=True, exist_ok=True)
    (tmp_path / "kube" / "ca.pem").write_text("CA", encoding="utf-8")
    c = K8sClient.from_kubeconfig(_kubeconfig(tmp_path, {"token": "t"}, {"certificate-authority": "ca.pem"}), session=_Session({}))
    assert c.verify == str(tmp_path / "kube" / "ca.pem")                # relative to the kubeconfig's folder, like kubectl
    c = K8sClient.from_kubeconfig(_kubeconfig(tmp_path, {"token": "t"}, {"insecure-skip-tls-verify": True}), session=_Session({}))
    assert c.verify is False
    with pytest.raises(K8sError, match="exec / auth-provider"):
        K8sClient.from_kubeconfig(_kubeconfig(tmp_path, {"exec": {"command": "aws"}}))
    with pytest.raises(K8sError, match="no token, client certificate or password"):
        K8sClient.from_kubeconfig(_kubeconfig(tmp_path, {}))
    with pytest.raises(K8sError, match="kubeconfig not found"):
        K8sClient.from_kubeconfig(tmp_path / "nope")


def test_a_kubeconfig_outside_a_cluster_keeps_the_job_mode(tmp_path: Path, repo: Path, monkeypatch):
    """Development without a kubeconfig degrades to local-job (tests/web/test_local_job_mode.py); an explicit KUBECONFIG means a real
    cluster — the Mac Studio's colima from the laptop, or the infra team's agent pod."""
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.setenv("KUBECONFIG", str(_kubeconfig(tmp_path, {"token": "t"})))
    mgr = RunManager(tmp_path / "out", repo, mode="k8s-job", job_template=TEMPLATE)
    assert mgr.effective_mode == "k8s-job"


def test_a_missing_job_template_fails_the_run_readably(tmp_path: Path, repo: Path, monkeypatch):
    """The image's copy of the template keeps its CHANGE_ME placeholders; the pod mounts the filled one as a ConfigMap. A missing
    file must not create a Job from nothing."""
    monkeypatch.setenv("SBMIG_JOB_POLL_S", "0.05")
    jc = FakeJobClient()
    mgr = RunManager(tmp_path / "out", repo, mode="k8s-job", job_client=jc, job_template=tmp_path / "templates" / "nope.yaml")
    rid = mgr.start(b"PK\x05\x06" + b"\0" * 18, "p.zip", ["artifacts.local-e2e.yml"])
    assert mgr.runs[rid].done.wait(5)
    assert mgr.runs[rid].exit_code == 3 and "job template not found" in (mgr.runs[rid].error or "")
    assert "sbmig-job-template" in (mgr.runs[rid].error or "") and jc.created == []


def test_the_worker_template_follows_the_company_worker_contract():
    """2026-11-09: the company's Main (sbmig.web.worker_template) validates the business template at startup — one non-restarting
    container, no envFrom, credentials only through valueFrom, no LLM_RECORD_DIR / SBMIG_WEB_TOKEN, exactly one writable whole-PVC mount
    that contains the output dir. The shipped template must keep passing those rules."""
    job = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    spec, pspec = job["spec"], job["spec"]["template"]["spec"]
    assert job["apiVersion"] == "batch/v1" and job["metadata"]["namespace"]
    assert spec.get("backoffLimit", 0) == 0 and spec.get("parallelism", 1) == 1 and spec.get("completions", 1) == 1
    assert pspec["restartPolicy"] == "Never" and pspec["automountServiceAccountToken"] is False
    assert len(pspec["containers"]) == 1 and not pspec.get("initContainers")
    c = pspec["containers"][0]
    assert c["name"] and c["image"] and "envFrom" not in c
    names = [e["name"] for e in c["env"]]
    assert not ({"LLM_RECORD_DIR", "SBMIG_WEB_TOKEN", "WEB_API_TOKEN"} & set(names))
    for e in c["env"]:
        if any(w in e["name"] for w in ("TOKEN", "PASSWORD", "SECRET", "API_KEY")):
            assert "value" not in e and "valueFrom" in e, e["name"]
    for key in ("LLM_APP_TOKEN", "LLM_API_KEY", "NEXUS_USER", "NEXUS_PASSWORD"):
        ref = next(e for e in c["env"] if e["name"] == key)["valueFrom"]["secretKeyRef"]
        assert ref["name"] and ref["key"] == key
    volumes = {v["name"]: v for v in pspec["volumes"]}
    out_dir = Path("/data/out")
    shared = [m for m in c["volumeMounts"] if out_dir.is_relative_to(Path(m["mountPath"]))]
    assert len(shared) == 1 and "persistentVolumeClaim" in volumes[shared[0]["name"]]
    assert not shared[0].get("readOnly") and not shared[0].get("subPath")
    assert not [m for m in c["volumeMounts"] if Path(m["mountPath"]).is_relative_to(out_dir)]

