"""The per-upload choice how a framework project is handled (`framework_mode`: `reduced` | `convert`) travels like
`flow`: `POST /api/runs?framework_mode=…` (checked before the body) → `Run` / `RequestRecord.framework_mode` (pinned in
control/request.json, "" = not chosen, old records load) → Job env `SBMIG_FRAMEWORK_MODE` (set by Main when chosen,
compared by the Worker with request.json) → `sbmig run --framework-mode` / the engine keyword → `status()` (`framework_mode` only when chosen or in report.json). A converted
run's status carries `conversion` (report.json, before it the journal event `framework_converted`). `GET /api/configs`
says whether the server can convert (`framework.convert_available`) and its default (`framework.mode`). The page has the
choice on the upload card and the conversion sentence on the run / result cards. Contract: ROUND2.md section W."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import sbmig.web
from sbmig.core.config import PACKAGE_ROOT
from sbmig.web import RunManager, contract, create_app, worker
from sbmig.web.contract import RUN_LABEL, RequestRecord
from sbmig.web.localjob import LocalJobClient
from sbmig.web.runs import FrameworkModeError, check_framework_mode

from .test_app import _sse, _zip_bytes, fake_body
from .test_jobmode_hardening import ZIP, Cluster, _mgr, _seed, _wait
from .test_worker_env_pin import MAIN_KB, SHARED, _rewrite_record, fake_child, journal, pin, setup, worker_env  # noqa: F401
from .uploads import runs_url

PAGE = (Path(sbmig.web.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
SCRIPT = PAGE.split("<script>", 1)[1].split("</script>", 1)[0]
E2E = "artifacts.local-e2e.yml"
FW_YML = 'framework:\n  artifacts: ["example-fw-core"]\n'
CONFIGS = [E2E, "site.yml"]
CFG_Q = "configs=" + ",".join(CONFIGS)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setenv("SBMIG_JOB_POLL_S", "0.05")
    monkeypatch.delenv(contract.MAX_PARALLEL_ENV, raising=False)
    for key in ("SBMIG_FRAMEWORK__ARTIFACTS", "SBMIG_FRAMEWORK__NAME", "SBMIG_FRAMEWORK__MODE", "SBMIG_FRAMEWORK",
                "SBMIG_CONVERT__ENABLED", "SBMIG_CONVERT", contract.FRAMEWORK_MODE_ENV):
        monkeypatch.delenv(key, raising=False)


def _repo(tmp_path: Path, site: str = FW_YML) -> Path:
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    shutil.copy(PACKAGE_ROOT / "config" / E2E, repo / "config" / E2E)
    (repo / "config" / "site.yml").write_text(site, encoding="utf-8")
    return repo


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _repo(tmp_path)


@pytest.fixture
def client(tmp_path: Path, repo: Path, monkeypatch) -> TestClient:
    monkeypatch.setattr(RunManager, "_run_thread_body", fake_body)
    return TestClient(create_app(tmp_path / "out", repo, default_configs=CONFIGS))


@pytest.fixture
def managers():
    made: list[RunManager] = []
    yield made
    for mgr in made:
        mgr.shutdown("test teardown")
        for run in list(mgr.runs.values()):
            run.done.wait(10)


def _post(client: TestClient, query: str):
    return client.post(runs_url(query), content=_zip_bytes(), headers={"Content-Type": "application/zip"})


def _env(manifest: dict) -> dict[str, str]:
    return {e["name"]: e.get("value") for e in manifest["spec"]["template"]["spec"]["containers"][0]["env"]}


def _web_run(mgr: RunManager, rid: str) -> dict:
    lines = (mgr.run_dir(rid) / "journal.jsonl").read_text(encoding="utf-8").splitlines()
    return next(e for e in map(json.loads, lines) if e["event"] == "web_run")


# ---------------------------------------------------------------- validation
def test_check_framework_mode_values():
    assert check_framework_mode(None) == "" and check_framework_mode("") == ""
    assert check_framework_mode("reduced") == "reduced" and check_framework_mode("convert") == "convert"
    # the remediation flow ignores a valid value
    assert check_framework_mode("convert", "remediation") == ""
    for bad in ("Convert", "full", "reduced,convert"):
        with pytest.raises(FrameworkModeError) as e:
            check_framework_mode(bad)
        assert "unknown framework_mode" in str(e.value) and "reduced, convert" in str(e.value)
        assert e.value.message_ko and "처리 방법" in e.value.message_ko
    with pytest.raises(FrameworkModeError):                # a bad value stays bad in every flow
        check_framework_mode("x", "remediation")


@pytest.mark.parametrize("value", ["fast", "CONVERT", "reduced,convert"])
def test_an_unknown_value_is_refused_before_the_body(client: TestClient, value: str):
    r = _post(client, f"name=p.zip&{CFG_Q}&framework_mode={value}")
    assert r.status_code == 400
    body = r.json()
    assert "unknown framework_mode" in body["error"] and body["field"] == "framework_mode"
    assert re.search(r"[가-힣]", body["message_ko"])
    assert client.get("/api/runs").json()["runs"] == []


def test_convert_is_refused_when_the_server_cannot_convert(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(RunManager, "_run_thread_body", fake_body)
    repo = _repo(tmp_path, FW_YML + "convert:\n  enabled: false\n")
    client = TestClient(create_app(tmp_path / "out", repo, default_configs=CONFIGS))
    assert client.get("/api/configs").json()["framework"]["convert_available"] is False
    r = _post(client, f"name=p.zip&{CFG_Q}&framework_mode=convert")
    assert r.status_code == 400 and r.json()["field"] == "framework_mode"
    assert "서버" in r.json()["message_ko"] and "전환" in r.json()["message_ko"]
    assert client.get("/api/runs").json()["runs"] == []
    # without any framework configured the conversion is not available either
    plain = TestClient(create_app(tmp_path / "out2", _repo(tmp_path / "p", "x: 1\n"), default_configs=CONFIGS))
    assert _post(plain, f"name=p.zip&{CFG_Q}&framework_mode=convert").status_code == 400
    # reduced, and the remediation flow, are still accepted there
    assert _post(client, f"name=p.zip&{CFG_Q}&framework_mode=reduced").status_code == 202
    assert _post(client, f"name=p.zip&{CFG_Q}&flow=remediation&framework_mode=convert").status_code == 202


def test_start_file_refuses_convert_too(tmp_path: Path):
    """The route checks before the body; `start_file` checks again for every other caller."""
    repo = _repo(tmp_path, FW_YML + "convert:\n  enabled: false\n")
    mgr = RunManager(tmp_path / "out", repo)
    with pytest.raises(FrameworkModeError):
        mgr.start(_zip_bytes(), "p.zip", CONFIGS, framework_mode="convert")
    assert not any((tmp_path / "out").glob("p-*"))


# ---------------------------------------------------------------- in-process
def test_the_choice_is_stored_journalled_and_read_back(client: TestClient):
    mgr = client.app.state.manager
    r = _post(client, f"name=p.zip&{CFG_Q}&framework_mode=convert")
    assert r.status_code == 202, r.text
    rid = r.json()["run_id"]
    _sse(client, rid)
    assert mgr.runs[rid].framework_mode == "convert"
    assert _web_run(mgr, rid)["framework_mode"] == "convert"
    assert client.get(f"/api/runs/{rid}").json()["framework_mode"] == "convert"
    # evicted from memory: web-status.json answers
    assert json.loads((mgr.run_dir(rid) / "web-status.json").read_text(encoding="utf-8"))["framework_mode"] == "convert"
    mgr.runs.pop(rid)
    assert mgr.status(rid)["framework_mode"] == "convert"


def test_absent_means_not_chosen_and_remediation_stores_nothing(client: TestClient):
    mgr = client.app.state.manager
    a = _post(client, f"name=p.zip&{CFG_Q}").json()["run_id"]
    b = _post(client, f"name=p.zip&{CFG_Q}&flow=remediation&framework_mode=convert").json()["run_id"]
    for rid in (a, b):
        _sse(client, rid)
        assert mgr.runs[rid].framework_mode == ""
        assert "framework_mode" not in client.get(f"/api/runs/{rid}").json()
        assert "framework_mode" not in json.loads((mgr.run_dir(rid) / "web-status.json").read_text(encoding="utf-8"))
    assert _web_run(mgr, a)["framework_mode"] == ""


def test_in_process_passes_the_mode_to_the_engine_only_when_chosen(tmp_path: Path, repo: Path, monkeypatch):
    import sbmig.graph as graph_mod

    seen: list[dict] = []

    def fake_stream(orch, stop_after=None, **kw):
        seen.append(kw)
        yield "intake", {}
        yield "finalize", {}

    monkeypatch.setattr(graph_mod, "stream_graph", fake_stream)
    mgr = RunManager(tmp_path / "out", repo)
    for mode in ("convert", None, "reduced"):
        rid = mgr.start(_zip_bytes(), "p.zip", CONFIGS, framework_mode=mode)
        assert mgr.runs[rid].done.wait(15)
    rid = mgr.start(_zip_bytes(), "p.zip", CONFIGS, flow="remediation", framework_mode="convert")
    assert mgr.runs[rid].done.wait(15)
    assert seen == [{"framework_mode": "convert"}, {}, {"framework_mode": "reduced"}, {"flow": "remediation"}]


# ---------------------------------------------------------------- Job modes
def test_job_mode_pins_the_mode_and_sets_the_job_env(tmp_path: Path, repo: Path, managers):
    out = tmp_path / "out"
    cluster = Cluster(out)
    managers.append(mgr := _mgr(tmp_path, repo, cluster))
    a = mgr.start(ZIP, "a.zip", CONFIGS, framework_mode="convert")
    b = mgr.start(ZIP, "b.zip", CONFIGS)
    c = mgr.start(ZIP, "c.zip", CONFIGS, flow="remediation", framework_mode="reduced")
    for rid in (a, b, c):
        assert mgr.runs[rid].done.wait(10)
    assert [RequestRecord.read(out / r).framework_mode for r in (a, b, c)] == ["convert", "", ""]
    envs = {m["metadata"]["labels"][RUN_LABEL]: _env(m) for m in cluster.created}
    assert envs[a][contract.FRAMEWORK_MODE_ENV] == "convert"
    assert contract.FRAMEWORK_MODE_ENV not in envs[b] and contract.FRAMEWORK_MODE_ENV not in envs[c]
    assert [mgr.status(r).get("framework_mode") for r in (a, b, c)] == ["convert", None, None]
    journal_a = [json.loads(ln) for ln in (out / a / "journal.jsonl").read_text(encoding="utf-8").splitlines()]
    assert next(e for e in journal_a if e["event"] == "web_run")["framework_mode"] == "convert"


def test_recovery_keeps_the_mode(tmp_path: Path, repo: Path, managers):
    out = tmp_path / "out"
    rd = _seed(out, "web-q", "queued")
    rec = RequestRecord.read(rd)
    rec.framework_mode = "convert"
    rec.write(rd)
    cluster = Cluster(out)
    managers.append(mgr := _mgr(tmp_path, repo, cluster))
    assert mgr.recovered == ["web-q"]
    assert _wait(lambda: len(cluster.created) == 1)
    assert _env(cluster.created[0])[contract.FRAMEWORK_MODE_ENV] == "convert"
    assert mgr.runs["web-q"].framework_mode == "convert"


def test_a_remediation_resume_job_carries_the_runs_mode(tmp_path: Path, repo: Path, managers):
    """The resume Worker compares the run values with request.json too, so its Job carries the recorded mode."""
    from sbmig.web.contract import OWNER_LABEL
    from sbmig.web.k8s import job_name

    out = tmp_path / "out"
    rd = _seed(out, "web-r", "finished")
    rec = RequestRecord.read(rd)
    rec.framework_mode = "reduced"
    rec.write(rd)
    cluster = Cluster(out, pod_attempt=False)
    cluster.add(job_name("web-r"), {RUN_LABEL: "web-r", OWNER_LABEL: "own3r"})
    managers.append(mgr := _mgr(tmp_path, repo, cluster))
    mgr.resume_remediation("web-r")
    assert _wait(lambda: len(cluster.created) == 1)
    assert _env(cluster.created[0])[contract.FRAMEWORK_MODE_ENV] == "reduced"


def test_the_mode_is_a_run_value_not_a_pinned_config_override(tmp_path: Path):
    assert contract.FRAMEWORK_MODE_ENV == "SBMIG_FRAMEWORK_MODE" and contract.FRAMEWORK_MODE_ENV in contract.RUN_ENV
    assert not contract.is_secret_name(contract.FRAMEWORK_MODE_ENV)
    assert contract.FRAMEWORK_MODE_ENV not in contract.env_pin_names(tmp_path, [], {contract.FRAMEWORK_MODE_ENV: "convert"})


def test_local_job_passes_the_mode(tmp_path: Path):
    manifest = {"metadata": {"name": "j"}, "spec": {"template": {"spec": {"containers": [{"env": [
        {"name": "SBMIG_RUN_ID", "value": "r"}, {"name": contract.FRAMEWORK_MODE_ENV, "value": "convert"},
        {"name": "SBMIG_KB_DIR", "value": "/data/kb-v2"}]}]}}}}
    assert LocalJobClient._env_of(manifest) == {"SBMIG_RUN_ID": "r", contract.FRAMEWORK_MODE_ENV: "convert"}


def test_old_request_json_reads_as_not_chosen(tmp_path: Path):
    base = {"run_id": "r", "accepted_at": "t", "upload": "u", "configs": [], "zip_sha256": "x", "config_digests": {},
            "sbmig_version": "v", "mode": "k8s-job"}
    assert RequestRecord.from_json(base).framework_mode == ""
    assert RequestRecord.from_json({**base, "framework_mode": None}).framework_mode == ""
    assert RequestRecord.from_json({**base, "framework_mode": "convert"}).framework_mode == "convert"


# ---------------------------------------------------------------- Worker
def test_worker_command_carries_the_mode_only_when_chosen():
    base = ("py", "z.zip", ["c.yml"], Path("p.yml"), "/out", "rid", None, None)
    assert "--framework-mode" not in worker.build_command(*base)
    assert "--framework-mode" not in worker.build_command(*base, framework_mode="")
    assert worker.build_command(*base, framework_mode="convert")[-2:] == ["--framework-mode", "convert"]
    assert worker.build_command(*base, flow="full", framework_mode="reduced")[-2:] == ["--framework-mode", "reduced"]
    cmd = worker.build_command(*base, flow="remediation", framework_mode="convert")
    assert "--framework-mode" not in cmd and cmd[-2:] == ["--flow", "remediation"]


def test_worker_runs_the_requested_mode(setup):
    _rewrite_record(setup, {"framework_mode": "convert"})
    pin(setup, {**SHARED, **MAIN_KB})
    calls: list = []
    assert worker.main(environ=worker_env(setup, SBMIG_FRAMEWORK_MODE="convert"), run_child=fake_child(0, calls)) == 0
    (cmd, _cwd, _env_), = calls
    assert cmd[cmd.index("--framework-mode") + 1] == "convert"


def test_worker_without_a_mode_has_no_argument_and_old_jobs_still_pass(setup):
    pin(setup, {**SHARED, **MAIN_KB})
    for extra in ({}, {"SBMIG_FRAMEWORK_MODE": ""}):
        calls: list = []
        assert worker.main(environ=worker_env(setup, **extra), run_child=fake_child(0, calls)) == 0, extra
        assert "--framework-mode" not in calls[0][0]


@pytest.mark.parametrize("env_mode,record_mode", [("convert", None), ("reduced", "convert"), (None, "convert")])
def test_worker_refuses_a_mode_that_differs_from_request_json(setup, env_mode, record_mode):
    if record_mode:
        _rewrite_record(setup, {"framework_mode": record_mode})
    pin(setup, {**SHARED, **MAIN_KB})
    extra = {"SBMIG_FRAMEWORK_MODE": env_mode} if env_mode else {}
    calls: list = []
    assert worker.main(environ=worker_env(setup, **extra), run_child=fake_child(0, calls)) == 3
    assert not calls
    assert contract.JobStatus.read(setup["run_dir"]).error.startswith("SBMIG_FRAMEWORK_MODE")
    assert journal(setup["run_dir"])[-1]["event"] == "worker_refused"


# ---------------------------------------------------------------- /api/configs and the status of a converted run
def test_configs_say_whether_the_server_can_convert(tmp_path: Path):
    def fw(site: str) -> dict:
        root = tmp_path / str(len(list(tmp_path.iterdir())))
        return TestClient(create_app(root / "out", _repo(root, site), default_configs=CONFIGS)).get(
            "/api/configs").json()["framework"]

    on = fw(FW_YML)
    assert on["convert_available"] is True and on["mode"] == "reduced" and on["configured"] is True
    assert fw(FW_YML + "  mode: convert\n")["mode"] == "convert"
    assert fw(FW_YML + "convert:\n  enabled: false\n")["convert_available"] is False
    # no framework name: no conversion (a glob names no artifact, nothing configured at all)
    assert fw('framework:\n  artifacts: ["example-fw-*"]\n')["convert_available"] is False
    assert fw("x: 1\n") == {"configured": False, "name": "", "fix_loop": False, "remediation_always": False,
                            "convert_available": False, "mode": "reduced"}


CONVERSION = {"converted": True, "framework": "Example", "project": "myShop", "package": "com.example.myShop",
              "modules": ["myShop-boot", "myShop-domain", "myShop-service", "myShop-store"], "commit": "abc",
              "counts": {"services": 3, "mappers": 2, "java_files": 40}, "datasource": "configured:oracle",
              "queue": [{"component": "x", "reason": "y", "source_path": "a"}], "queue_total": 7,
              "unexposed": [], "skipped": [], "errors": []}
SHOWN = {"framework": "Example", "project": "myShop", "package": "com.example.myShop",
         "modules": ["myShop-boot", "myShop-domain", "myShop-service", "myShop-store"],
         "counts": {"services": 3, "mappers": 2, "java_files": 40}, "queue_total": 7, "datasource": "configured:oracle"}


def _run_dir(tmp_path: Path, rid: str) -> Path:
    rd = tmp_path / "out" / rid
    rd.mkdir(parents=True)
    return rd


def test_status_of_a_converted_run_from_the_report(tmp_path: Path, repo: Path):
    mgr = RunManager(tmp_path / "out", repo)
    rd = _run_dir(tmp_path, "conv")
    (rd / "report.json").write_text(json.dumps({"success": True, "framework_mode": "convert", "conversion": CONVERSION}),
                                    encoding="utf-8")
    st = mgr.status("conv")
    assert st["conversion"] == SHOWN and st["framework_mode"] == "convert"
    assert "framework" not in st                       # the converted run has no reduced scope
    # nothing to convert: no conversion object; the report's mode still reads back
    (rd / "report.json").write_text(json.dumps({"success": True, "framework_mode": "convert",
                                                "conversion": {**CONVERSION, "converted": False}}), encoding="utf-8")
    st = mgr.status("conv")
    assert "conversion" not in st and st["framework_mode"] == "convert"


def test_status_of_a_converted_run_from_the_journal_before_the_report(tmp_path: Path, repo: Path):
    mgr = RunManager(tmp_path / "out", repo)
    rd = _run_dir(tmp_path, "conv-live")
    ev = {"event": "framework_converted", "project": "myShop", "package": "com.example.myShop",
          "modules": ["myShop-boot", 7], "counts": {"services": 3, "bad": "x"}, "queue": 4, "errors": [],
          "datasource": "not_found"}
    with open(rd / "journal.jsonl", "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"event": "intake"}) + "\n" + json.dumps(ev) + "\n")
    assert mgr.status("conv-live")["conversion"] == {
        "framework": "", "project": "myShop", "package": "com.example.myShop", "modules": ["myShop-boot"],
        "counts": {"services": 3}, "queue_total": 4, "datasource": "not_found"}
    # the report, once written, wins over the journal
    (rd / "report.json").write_text(json.dumps({"success": False, "conversion": CONVERSION}), encoding="utf-8")
    assert mgr.status("conv-live")["conversion"] == SHOWN
    # no conversion anywhere: no key
    plain = _run_dir(tmp_path, "plain")
    (plain / "journal.jsonl").write_text(json.dumps({"event": "plan", "steps": []}) + "\n", encoding="utf-8")
    assert "conversion" not in mgr.status("plain")


def test_old_runs_read_as_before(tmp_path: Path, repo: Path):
    """A run written before the choice existed: no `framework_mode` anywhere, no `conversion`."""
    mgr = RunManager(tmp_path / "out", repo)
    old = _seed(tmp_path / "out", "web-old", "finished")
    data = json.loads((old / "control" / "request.json").read_text(encoding="utf-8"))
    data.pop("framework_mode", None)
    (old / "control" / "request.json").write_text(json.dumps(data), encoding="utf-8")
    (old / "report.json").write_text(json.dumps({"success": True}), encoding="utf-8")
    in_proc = _run_dir(tmp_path, "inproc-old")
    (in_proc / "web-status.json").write_text(json.dumps({"exit_code": 0, "flow": "full"}), encoding="utf-8")
    for rid in ("web-old", "inproc-old"):
        st = mgr.status(rid)
        assert "framework_mode" not in st and "conversion" not in st, rid


# ---------------------------------------------------------------- the page
def test_page_offers_the_choice_like_the_flow_choice():
    intake = PAGE.split('class="card intake"', 1)[1].split("</section>", 1)[0]
    pick = intake.split('<fieldset class="flowpick" id="fwpick" hidden>', 1)[1].split("</fieldset>", 1)[0]
    assert '<legend id="fwpick-title">사내 프레임워크 프로젝트 처리 방법</legend>' in pick
    assert '<input type="radio" name="framework_mode" id="fwmode-reduced" value="reduced" checked>' in pick
    assert '<input type="radio" name="framework_mode" id="fwmode-convert" value="convert">' in pick
    for text in ("JDK 21, Jakarta만 적용", "프레임워크는 그대로 둡니다. 빌드 오류는 고치지 않습니다.", "사내 규정으로 전환",
                 "Spring Boot 프로젝트로 다시 만들고 사내 부모 pom을 적용한 뒤 빌드 오류를 고칩니다.",
                 "프레임워크 프로젝트가 아니면 이 선택은 쓰이지 않습니다."):
        assert text in pick, text
    assert pick.count('class="flowopt') == 2 and "<select" not in pick
    # below the flow choice, below the notice
    assert intake.index('id="fwupload"') < intake.index('id="flowpick"') < intake.index('id="fwpick"')


def test_page_choice_rules():
    # shown only when the server names a framework and can convert, hidden for the remediation flow
    assert "fwCfg.configured === true && fwCfg.convert_available === true" in SCRIPT
    assert "$('fwpick').hidden = !fwChoiceOn() || chosenFlow() === FLOW_REM;" in SCRIPT
    assert "setDropLimit(); describeConfigs(); syncFwMode();" in SCRIPT
    # preselected = the server default, also after a successful start
    assert "function fwDefault() { return fwCfg && fwCfg.mode === FW_CONVERT ? FW_CONVERT : FW_REDUCED; }" in SCRIPT
    assert SCRIPT.count("setFwChoice(fwDefault());") == 2
    # the title and the reduced sub-line follow the configuration
    assert "`${name} 프레임워크 프로젝트 처리 방법`" in SCRIPT
    assert "'프레임워크는 그대로 둡니다. 빌드 오류도 고칩니다.'" in SCRIPT
    # sent only when shown, locked while uploading
    assert "if (fwMode) q.set('framework_mode', fwMode);" in SCRIPT
    assert "if (!fwChoiceOn() || chosenFlow() === FLOW_REM) return '';" in SCRIPT
    assert "$('fwpick').disabled = true; $('fwpick').classList.add('locked');" in SCRIPT
    assert "$('fwpick').disabled = false; $('fwpick').classList.remove('locked');" in SCRIPT
    # the server's refusal of the choice is a toast, not a message under the project name
    refusal = "if (r.status === 400 && d.message_ko && d.field === 'framework_mode') { toast(String(d.message_ko), true); return; }"
    assert refusal in SCRIPT
    assert SCRIPT.index(refusal) < SCRIPT.index("if (r.status === 400 && d.message_ko) { showProjectMsg(")


def test_page_notices_of_a_conversion():
    assert "`${name} 프레임워크 프로젝트를 사내 규정의 Spring Boot 프로젝트로 전환합니다.`" in SCRIPT
    assert "'사내 프레임워크 프로젝트를 사내 규정의 Spring Boot 프로젝트로 전환합니다.'" in SCRIPT
    # the upload notice follows the choice
    upload = SCRIPT.split("function setUploadFramework(f) {", 1)[1].split("\n  }\n", 1)[0]
    assert "if (chosenFwMode() === FW_CONVERT) {" in upload and "setText('fwupload-head', convHead(name));" in upload
    # the run and result cards: from the status and from the journal event
    assert "if (st.conversion && typeof st.conversion === 'object') setConversion(st.conversion);" in SCRIPT
    assert "if (name === 'framework_converted') setConversion(ev);" in SCRIPT
    render = SCRIPT.split("function renderFramework() {", 1)[1].split("\n  }\n", 1)[0]
    assert "$(id).hidden = !fwReduced && !fwConv;" in render
    assert "fwConv ? convHead(fwConv.name)" in render and "fwConv ? convBody(fwConv)" in render
    # a converted run runs 재구성 / 구조 검토: no reduced scope, so placeSkipped draws nothing
    conv = SCRIPT.split("function setConversion(src) {", 1)[1].split("\n  }\n", 1)[0]
    assert "if (fwConv) { fwInfo = null; fwReduced = false; }" in conv and "placeSkipped();" in conv
    assert "if (fw && fwConv) return;" in SCRIPT
    assert "if (!fwReduced || railFlow === FLOW_REM) return;" in SCRIPT
    # a new run starts without it
    assert "resetRail(runFlow); setFramework(null); setConversion(null);" in SCRIPT
    # values from a run go in as text
    for fn in ("setConversion", "convBody", "convHead", "syncFwMode"):
        body = SCRIPT.split(f"function {fn}(", 1)[1].split("\n  }\n", 1)[0]
        assert "innerHTML" not in body and "insertAdjacentHTML" not in body, fn


# ---------------------------------------------------------------- review follow-up
def test_configs_report_the_effective_default(tmp_path: Path):
    """`framework.mode: convert` on a server that cannot convert reads as `reduced` (the engine applies the same
    rule to a run where nothing was chosen); `convert` only when the conversion is available."""
    def fw(site: str) -> dict:
        root = tmp_path / str(len(list(tmp_path.iterdir())))
        return TestClient(create_app(root / "out", _repo(root, site), default_configs=CONFIGS)).get(
            "/api/configs").json()["framework"]

    off = fw(FW_YML + "  mode: convert\nconvert:\n  enabled: false\n")
    assert off["mode"] == "reduced" and off["convert_available"] is False
    unnamed = fw('framework:\n  artifacts: ["example-fw-*"]\n  mode: convert\n')
    assert unnamed["mode"] == "reduced" and unnamed["convert_available"] is False
    assert fw(FW_YML + "  mode: convert\n")["mode"] == "convert"
    assert fw(FW_YML)["mode"] == "reduced"


def test_an_explicit_convert_is_still_refused_where_the_default_says_convert(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(RunManager, "_run_thread_body", fake_body)
    repo = _repo(tmp_path, FW_YML + "  mode: convert\nconvert:\n  enabled: false\n")
    client = TestClient(create_app(tmp_path / "out", repo, default_configs=CONFIGS))
    assert _post(client, f"name=p.zip&{CFG_Q}&framework_mode=convert").status_code == 400
    assert _post(client, f"name=p.zip&{CFG_Q}").status_code == 202        # nothing chosen: the effective default


def test_page_preselects_from_the_reported_default():
    assert "function fwDefault() { return fwCfg && fwCfg.mode === FW_CONVERT ? FW_CONVERT : FW_REDUCED; }" in SCRIPT


@pytest.mark.parametrize("changes,named", [({"framework_mode": "bogus"}, "framework_mode"),
                                           ({"flow": "bogus"}, "flow")])
def test_worker_refuses_an_unknown_recorded_value_before_anything_runs(setup, changes, named):
    """Main copies request.json's value into the Job env, so the env comparison cannot catch a value no run knows
    (a hand edit, another image): the Worker refuses the record itself, whatever the env pin policy."""
    _rewrite_record(setup, changes)
    pin(setup, {**SHARED, **MAIN_KB}, contract.ENV_PIN_OFF)
    env_name = contract.FRAMEWORK_MODE_ENV if named == "framework_mode" else contract.FLOW_ENV
    calls: list = []
    assert worker.main(environ=worker_env(setup, **{env_name: "bogus"}), run_child=fake_child(0, calls)) == 3
    assert not calls
    error = contract.JobStatus.read(setup["run_dir"]).error
    assert f"request.json's {named}" in error and "'bogus'" in error
    events = journal(setup["run_dir"])
    assert events[-1]["event"] == "worker_refused" and not any(e["event"] == "worker_started" for e in events)


def test_record_value_problems_and_the_command_guard():
    rec = RequestRecord(run_id="r", accepted_at="t", upload="u", configs=[], zip_sha256="x", config_digests={},
                        sbmig_version="v", mode="k8s-job")
    assert worker.record_value_problems(rec) == []
    for flow, mode in (("full", "convert"), ("full", "reduced"), ("remediation", "")):
        rec.flow, rec.framework_mode = flow, mode
        assert worker.record_value_problems(rec) == [], (flow, mode)
    rec.flow, rec.framework_mode = "fast", "bogus"
    assert [p.split(" ", 1)[0] for p in worker.record_value_problems(rec)] == ["request.json's", "request.json's"]
    # the command never carries a value no CLI knows
    base = ("py", "z.zip", ["c.yml"], Path("p.yml"), "/out", "rid", None, None)
    cmd = worker.build_command(*base, flow="fast", framework_mode="bogus")
    assert "--flow" not in cmd and "--framework-mode" not in cmd
