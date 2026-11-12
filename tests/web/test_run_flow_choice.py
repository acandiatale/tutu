"""v2.1z-aj (company, 2026-11-12): a run is `full` (migration, then the chained remediation) or `remediation` (remediation
only, no migration). The flow is a per-run value like `stop_after`: `POST /api/runs?flow=…` → `RequestRecord.flow` (pinned
in control/request.json) → Job env `SBMIG_FLOW` (always set by Main) → the Worker checks it against request.json and adds
`--flow remediation` to `sbmig run`; in-process mode passes it to the engine. `status()` / the run list carry it, an aborted
run carries its abort reason, and the page has the two choices, the header name and the flow-aware result."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import sbmig.web
from sbmig.web import RunManager, contract, create_app, worker
from sbmig.web.contract import OWNER_LABEL, RUN_LABEL, RequestRecord
from sbmig.web.k8s import job_name
from sbmig.web.localjob import LocalJobClient

from .test_app import _manager, _sse, _start, _zip_bytes, fake_body, repo  # noqa: F401
from .test_jobmode_hardening import ZIP, Cluster, _mgr, _seed, _state, _wait
from .test_worker_env_pin import MAIN_KB, SHARED, _rewrite_record, fake_child, journal, pin, setup, worker_env  # noqa: F401

PAGE = (Path(sbmig.web.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
SCRIPT = PAGE.split("<script>", 1)[1].split("</script>", 1)[0]
CFG = "configs=artifacts.local-e2e.yml"


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setenv("SBMIG_JOB_POLL_S", "0.05")
    monkeypatch.delenv(contract.MAX_PARALLEL_ENV, raising=False)


@pytest.fixture
def client(tmp_path: Path, repo: Path, monkeypatch) -> TestClient:
    monkeypatch.setattr(RunManager, "_run_thread_body", fake_body)
    return TestClient(create_app(tmp_path / "out", repo))


@pytest.fixture
def managers():
    made: list[RunManager] = []
    yield made
    for mgr in made:
        mgr.shutdown("test teardown")
        for run in list(mgr.runs.values()):
            run.done.wait(10)


def _env(manifest: dict) -> dict[str, str]:
    return {e["name"]: e.get("value") for e in manifest["spec"]["template"]["spec"]["containers"][0]["env"]}


# ---------------------------------------------------------------- API + in-process
def test_flow_defaults_to_full_and_is_reported(client: TestClient):
    rid = _start(client, f"name=p.zip&{CFG}")
    _sse(client, rid)
    st = client.get(f"/api/runs/{rid}").json()
    assert st["flow"] == "full"
    assert _manager(client).runs[rid].flow == "full"
    (row,) = client.get("/api/runs").json()["runs"]
    assert row["flow"] == "full"
    lines = [json.loads(ln) for ln in (Path(st["run_dir"]) / "journal.jsonl").read_text(encoding="utf-8").splitlines()]
    assert next(e for e in lines if e["event"] == "web_run")["flow"] == "full"


def test_remediation_flow_is_accepted_reported_and_survives_eviction(client: TestClient):
    rid = _start(client, f"name=p.zip&{CFG}&flow=remediation")
    _sse(client, rid)
    assert client.get(f"/api/runs/{rid}").json()["flow"] == "remediation"
    assert [r["flow"] for r in client.get("/api/runs").json()["runs"]] == ["remediation"]
    # evicted from memory: the in-process run's web-status.json answers
    mgr = _manager(client)
    rd = mgr.run_dir(rid)
    assert json.loads((rd / "web-status.json").read_text(encoding="utf-8"))["flow"] == "remediation"
    mgr.runs.pop(rid)
    assert mgr.status(rid)["flow"] == "remediation"


@pytest.mark.parametrize("value", ["fast", "FULL", "remediation,full"])
def test_another_flow_value_is_refused_before_the_body(client: TestClient, value: str):
    r = client.post(f"/api/runs?name=p.zip&{CFG}&flow={value}", content=_zip_bytes(), headers={"Content-Type": "application/zip"})
    assert r.status_code == 400
    assert "unknown flow" in r.json()["error"] and "full, remediation" in r.json()["error"]
    assert client.get("/api/runs").json()["runs"] == []


def test_stop_after_with_the_remediation_flow_is_refused(client: TestClient):
    r = client.post(f"/api/runs?name=p.zip&{CFG}&flow=remediation&stop_after=plan", content=_zip_bytes(),
                    headers={"Content-Type": "application/zip"})
    assert r.status_code == 400 and "stop_after" in r.json()["error"]


def test_in_process_passes_the_flow_to_the_engine(tmp_path: Path, repo: Path, monkeypatch):
    """`stream_graph(orch, stop_after=…, flow="remediation")`; a `full` run passes no `flow` (the engine's default)."""
    import sbmig.graph as graph_mod

    seen: list[dict] = []

    def fake_stream(orch, stop_after=None, **kw):
        seen.append(kw)
        yield "intake", {}
        yield ("remediation", {}) if kw.get("flow") == "remediation" else ("finalize", {})

    monkeypatch.setattr(graph_mod, "stream_graph", fake_stream)
    mgr = RunManager(tmp_path / "out", repo)
    for flow in ("remediation", "full"):
        rid = mgr.start(_zip_bytes(), "p.zip", ["artifacts.local-e2e.yml"], flow=flow)
        assert mgr.runs[rid].done.wait(15)
    assert seen == [{"flow": "remediation"}, {}]


# ---------------------------------------------------------------- status: flow from disk, abort reason
def test_status_reads_the_flow_and_the_abort_reason_from_disk(tmp_path: Path, repo: Path):
    mgr = RunManager(tmp_path / "out", repo)
    # an old request.json (no flow key) reads as full
    rd = _seed(tmp_path / "out", "web-old", "finished")
    data = json.loads((rd / "control" / "request.json").read_text(encoding="utf-8"))
    assert "flow" in data
    data.pop("flow")
    (rd / "control" / "request.json").write_text(json.dumps(data), encoding="utf-8")
    assert RequestRecord.read(rd).flow == "full" and mgr.status("web-old")["flow"] == "full"
    # an in-process run refused at intake: report.json names the reason; the flow comes from the report
    rd2 = tmp_path / "out" / "web-ant"
    rd2.mkdir()
    (rd2 / "web-status.json").write_text(json.dumps({"exit_code": 3}), encoding="utf-8")
    (rd2 / "report.json").write_text(json.dumps({"flow": "remediation", "success": False, "flags": ["aborted"],
                                                 "manual_followups": ["run aborted: Maven 변환 후 신청 부탁드립니다.",
                                                                      "run aborted: later"],
                                                 "remediation": {"status": "skipped", "reason": "not_ready",
                                                                 "missing": ["IVAS_BASE_URL"], "dir": "remediation"}}),
                                     encoding="utf-8")
    st = mgr.status("web-ant")
    assert st["flow"] == "remediation" and st["exit_code"] == 3
    assert st["abort_reason"] == "Maven 변환 후 신청 부탁드립니다."
    assert st["remediation"] == {"status": "skipped", "reason": "not_ready", "missing": ["IVAS_BASE_URL"]}


def test_flow_null_in_request_json_reads_as_full(tmp_path: Path):
    rec = RequestRecord.from_json({"run_id": "r", "accepted_at": "t", "upload": "u", "configs": [], "zip_sha256": "x",
                                   "config_digests": {}, "sbmig_version": "v", "mode": "k8s-job", "flow": None})
    assert rec.flow == "full"


# ---------------------------------------------------------------- Job modes
def test_job_mode_pins_the_flow_and_sets_the_job_env(tmp_path: Path, repo: Path, managers):
    out = tmp_path / "out"
    cluster = Cluster(out)
    managers.append(mgr := _mgr(tmp_path, repo, cluster))
    a = mgr.start(ZIP, "a.zip", ["artifacts.local-e2e.yml"], flow="remediation")
    assert mgr.runs[a].done.wait(10)
    b = mgr.start(ZIP, "b.zip", ["artifacts.local-e2e.yml"])
    assert mgr.runs[b].done.wait(10)
    assert RequestRecord.read(out / a).flow == "remediation" and RequestRecord.read(out / b).flow == "full"
    envs = {m["metadata"]["labels"][RUN_LABEL]: _env(m) for m in cluster.created}
    assert envs[a][contract.FLOW_ENV] == "remediation"
    assert envs[b][contract.FLOW_ENV] == "full"                    # always set, also for full
    assert mgr.status(a)["flow"] == "remediation" and mgr.status(b)["flow"] == "full"
    # stop_after is still refused with the remediation flow in a Job mode
    with pytest.raises(ValueError):
        mgr.start(ZIP, "c.zip", ["artifacts.local-e2e.yml"], stop_after="plan", flow="remediation")


def test_recovery_keeps_the_flow(tmp_path: Path, repo: Path, managers):
    out = tmp_path / "out"
    rd = _seed(out, "web-q", "queued")
    rec = RequestRecord.read(rd)
    rec.flow = "remediation"
    rec.write(rd)
    cluster = Cluster(out)
    managers.append(mgr := _mgr(tmp_path, repo, cluster))
    assert mgr.recovered == ["web-q"]
    assert _wait(lambda: len(cluster.created) == 1)
    assert _env(cluster.created[0])[contract.FLOW_ENV] == "remediation"
    assert mgr.runs["web-q"].flow == "remediation" and mgr.status("web-q")["flow"] == "remediation"
    assert mgr.runs["web-q"].done.wait(10) and _state(out, "web-q").state == "finished"


def test_a_remediation_resume_job_carries_the_runs_flow(tmp_path: Path, repo: Path, managers):
    out = tmp_path / "out"
    rd = _seed(out, "web-r", "finished")
    rec = RequestRecord.read(rd)
    rec.flow = "remediation"
    rec.write(rd)
    cluster = Cluster(out, pod_attempt=False)
    cluster.add(job_name("web-r"), {RUN_LABEL: "web-r", OWNER_LABEL: "own3r"})
    managers.append(mgr := _mgr(tmp_path, repo, cluster))
    mgr.resume_remediation("web-r")
    assert _wait(lambda: len(cluster.created) == 1)
    env = _env(cluster.created[0])
    assert env[contract.FLOW_ENV] == "remediation" and env[contract.ACTION_ENV] == contract.ACTION_REMEDIATION_RESUME


def test_flow_is_a_run_value_not_a_pinned_config_override(tmp_path: Path):
    assert contract.FLOW_ENV in contract.RUN_ENV
    assert not contract.is_secret_name(contract.FLOW_ENV)
    assert contract.FLOW_ENV not in contract.env_pin_names(tmp_path, [], {contract.FLOW_ENV: "remediation"})


def test_local_job_passes_the_flow(tmp_path: Path):
    manifest = {"metadata": {"name": "j"}, "spec": {"template": {"spec": {"containers": [{"env": [
        {"name": "SBMIG_RUN_ID", "value": "r"}, {"name": contract.FLOW_ENV, "value": "remediation"},
        {"name": "SBMIG_KB_DIR", "value": "/data/kb-v2"}]}]}}}}
    env = LocalJobClient._env_of(manifest)
    assert env == {"SBMIG_RUN_ID": "r", contract.FLOW_ENV: "remediation"}


# ---------------------------------------------------------------- Worker
def test_worker_command_carries_the_remediation_flow_only():
    base = ("py", "z.zip", ["c.yml"], Path("p.yml"), "/out", "rid", None, None)
    assert "--flow" not in worker.build_command(*base)
    assert "--flow" not in worker.build_command(*base, flow="full")
    cmd = worker.build_command(*base, flow="remediation")
    assert cmd[-2:] == ["--flow", "remediation"]


def test_worker_runs_the_requested_flow(setup):
    _rewrite_record(setup, {"flow": "remediation"})
    pin(setup, {**SHARED, **MAIN_KB})
    calls: list = []
    assert worker.main(environ=worker_env(setup, SBMIG_FLOW="remediation"), run_child=fake_child(0, calls)) == 0
    (cmd, _cwd, _env_), = calls
    assert cmd[cmd.index("--flow") + 1] == "remediation"


def test_worker_full_flow_has_no_flow_argument_and_old_jobs_still_pass(setup):
    pin(setup, {**SHARED, **MAIN_KB})
    for extra in ({"SBMIG_FLOW": "full"}, {}, {"SBMIG_FLOW": ""}):      # new Main / an older Main without the variable
        calls: list = []
        assert worker.main(environ=worker_env(setup, **extra), run_child=fake_child(0, calls)) == 0, extra
        assert "--flow" not in calls[0][0]


@pytest.mark.parametrize("env_flow,record_flow", [("remediation", None), ("full", "remediation"), (None, "remediation")])
def test_worker_refuses_a_flow_that_differs_from_request_json(setup, env_flow, record_flow):
    if record_flow:
        _rewrite_record(setup, {"flow": record_flow})
    pin(setup, {**SHARED, **MAIN_KB})
    extra = {"SBMIG_FLOW": env_flow} if env_flow else {}
    calls: list = []
    assert worker.main(environ=worker_env(setup, **extra), run_child=fake_child(0, calls)) == 3
    assert not calls
    assert contract.JobStatus.read(setup["run_dir"]).error.startswith("SBMIG_FLOW")
    assert journal(setup["run_dir"])[-1]["event"] == "worker_refused"


# ---------------------------------------------------------------- the page
def test_page_header_is_the_name_only():
    assert "<title>panacea-remediating-oss</title>" in PAGE
    header = PAGE.split('<header class="top">', 1)[1].split("</header>", 1)[0]
    assert '<span class="name">panacea-remediating-oss</span>' in header
    assert 'class="what"' not in header and "JDK 21" not in header and ".top .what" not in PAGE


def test_page_offers_the_two_flows_with_full_as_default():
    pick = PAGE.split('<fieldset class="flowpick"', 1)[1].split("</fieldset>", 1)[0]
    assert re.search(r'<input type="radio" name="flow" id="flow-full" value="full" checked>', pick)
    assert re.search(r'<input type="radio" name="flow" id="flow-remediation" value="remediation">', pick)
    for text in ("전체 진행", "이관 후 취약점 조치까지 진행합니다", "취약점 해결", "이미 이관된 프로젝트의 취약점만 조치합니다"):
        assert text in pick, text
    assert "<select" not in pick                                       # two radio cards, not a dropdown
    assert ".flowopt:has(input:focus-visible)" in PAGE and ".flowpick { grid-template-columns: 1fr; }" in PAGE


def test_page_sends_the_flow_and_follows_it():
    assert "q.set('flow', flow)" in SCRIPT
    assert "'취약점 조치 시작'" in SCRIPT and "'마이그레이션 시작'" in SCRIPT
    assert "const REM_STEPS = ['intake', FLOW_REM, 'finalize'];" in SCRIPT
    assert "st.flow === FLOW_REM" in SCRIPT and "run.flow === FLOW_REM" in SCRIPT
    for label in ("'<span class=\"flowtag rem\">취약점 해결</span>'", "'<span class=\"flowtag\">전체 진행</span>'"):
        assert label in SCRIPT, label
    # the remediation flow's result: the migration buttons are hidden, the remediation block is the result
    assert "$('mainline').hidden = remOnly; $('maindl').hidden = remOnly;" in SCRIPT
    assert "$('remed').classList.toggle('solo', remOnly);" in SCRIPT


def test_page_shows_the_abort_reason_in_the_result_card():
    assert re.search(r'<div class="abort" id="abortbox" role="alert" hidden>', PAGE)
    assert "if (st.abort_reason_ko || st.abort_reason) return String(st.abort_reason_ko || st.abort_reason);" in SCRIPT
    assert "not ready): " not in SCRIPT                          # no string matching of engine sentences in the page
    assert "setText('aborttext', why);" in SCRIPT


def test_page_sentences_for_owners_do_not_say_pod():
    # '파드' stays only as the rail's stage label (and in code comments)
    code = re.sub(r"\s//.*", "", SCRIPT)                              # drop line comments
    visible = re.findall(r"'([^'\n]*파드[^'\n]*)'", code) + re.findall(r"`([^`\n]*파드[^`\n]*)`", code)
    assert visible == ["파드"], visible
