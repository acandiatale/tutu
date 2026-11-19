"""v2.1z-be: an upload needs a project name (the owner: "파일 올리고 프로젝트명 안적으면 거절당하게 … 프론트, 백엔드 둘
다 체크"; later: "영어랑 _, - 로만 허용 … 공백도 비허용!").

Server: `POST /api/runs?project=…` must match `^[A-Za-z0-9][A-Za-z0-9_-]{0,29}$` (not trimmed), checked before a
single body byte is read; two refusals (`project name is required` / the pattern sentence), each with `message_ko`.
The run id starts with the name (`<project>-YYYYmmdd-HHMMSS-<hex 6>`), and every name derived from it (Job name,
resume Job name, labels, the upload file) stays valid and unique. The name is kept with the run (request.json,
web-status.json, `Run`, recovery) and served by `GET /api/runs[/{id}]` and the SSE status; a run made before has no
`project` key and every reader falls back to the upload's file name. Page: the field, its rule, the disabled start
button, the messages. Admin: grouped by the entered name.

How "before a body byte" is proven: the app is called as a bare ASGI application with a `receive` callable that
counts its calls; a refusal must answer with `receive` never called (the request carries a real zip body, with a
Content-Length, and once more chunked without one). The control case (a valid name) shows the same harness does
count the reads."""
from __future__ import annotations

import io
import json
import re
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from fastapi.testclient import TestClient

import sbmig.web
from sbmig.core.config import PACKAGE_ROOT
from sbmig.web import RunManager, admin, contract, create_app
from sbmig.web.contract import ControlState, RequestRecord
from sbmig.web.k8s import job_name, render_job
from sbmig.web.runs import PROJECT_RE, RUN_ID_RE, check_project, resume_job_name, run_id_for

from .test_jobmode_hardening import ZIP, Cluster, _mgr, _wait
from .uploads import PROJECT, runs_url

PAGE = (Path(sbmig.web.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
SCRIPT = PAGE.split("<script>", 1)[1].split("</script>", 1)[0]
ADMIN_PAGE = (Path(sbmig.web.__file__).parent / "static" / "admin.html").read_text(encoding="utf-8")
DNS1123 = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
LABEL_VALUE = re.compile(r"^(([A-Za-z0-9][-A-Za-z0-9_.]*)?[A-Za-z0-9])?$")
NOW = datetime(2026, 11, 20, 9, 30, 5)

REQUIRED = {"error": "project name is required", "message_ko": "프로젝트명을 입력해 주세요."}
PATTERN = {"error": "project name: letters, digits, _ and - only, at most 30 characters, starting with a letter or a digit",
           "message_ko": "프로젝트명은 영문, 숫자, _, - 만 쓸 수 있습니다(공백 없이 30자까지, 첫 글자는 영문이나 숫자)."}

REFUSED = [                                    # (value, answer); None = the parameter is left out
    (None, REQUIRED),
    ("", REQUIRED),
    ("a" * 31, PATTERN),
    ("-order", PATTERN),
    ("_order", PATTERN),
    ("order admin", PATTERN),
    (" order", PATTERN),
    ("order ", PATTERN),
    ("주문관리", PATTERN),
    ("주문 관리 시스템", PATTERN),
    ("order.admin", PATTERN),
    ("order/admin", PATTERN),
    ("..", PATTERN),
    ("order\tadmin", PATTERN),
    ("order\n", PATTERN),
    ("order\r\nX-Evil: 1", PATTERN),
    (" ", PATTERN),
    ("ｏｒｄｅｒ", PATTERN),                      # full-width letters are not English letters
]
ACCEPTED = ["a", "7", "order-admin", "shop_api2", "Shop_API2", "a" * 30, "A_" * 15, "order-", "x_"]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    shutil.copy(PACKAGE_ROOT / "config" / "artifacts.local-e2e.yml", repo / "config" / "artifacts.local-e2e.yml")
    (repo / "config" / "default.yml").write_text("x: 1\n")
    return repo


def fake_body(self: RunManager, run) -> SimpleNamespace:
    (run.run_dir / "report.md").write_text("# report\n", encoding="utf-8")
    (run.run_dir / "report.json").write_text(json.dumps({"success": True, "blocked": [], "unresolved": []}),
                                             encoding="utf-8")
    return SimpleNamespace(success=True, blocked=[], flags=[])


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("proj/pom.xml", "<project/>")
    return buf.getvalue()


def _wait_done(client: TestClient, rid: str) -> dict:
    mgr = client.app.state.manager
    assert mgr.runs[rid].done.wait(10)
    return client.get(f"/api/runs/{rid}").json()


# ---------------------------------------------------------------- the rule, in one place
@pytest.mark.parametrize("value,answer", REFUSED)
def test_check_project_refuses(value, answer):
    with pytest.raises(ValueError) as e:
        check_project(value)
    assert str(e.value) == answer["error"] and e.value.message_ko == answer["message_ko"]


@pytest.mark.parametrize("value", ACCEPTED)
def test_check_project_accepts_and_never_changes_the_value(value):
    assert check_project(value) == value and PROJECT_RE.fullmatch(value)


def test_the_pattern_is_the_contract_one():
    assert PROJECT_RE.pattern == r"^[A-Za-z0-9][A-Za-z0-9_-]{0,29}$"
    # `$` would accept a trailing newline with re.match; the check must not
    assert not PROJECT_RE.fullmatch("order\n")


# ---------------------------------------------------------------- refused before a body byte is read
def _asgi_post(app, query: str, body: bytes, chunked: bool) -> tuple[int, dict, int]:
    """POST /api/runs as a bare ASGI call; returns (status, json body, how often the app asked for body bytes)."""
    reads = {"n": 0}
    pieces = [body[i:i + 64] for i in range(0, len(body), 64)] or [b""]
    sent: list[dict] = []
    headers = [(b"host", b"testserver"), (b"content-type", b"application/zip")]
    headers.append((b"transfer-encoding", b"chunked") if chunked else (b"content-length", str(len(body)).encode()))
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "http_version": "1.1",
             "method": "POST", "scheme": "http", "path": "/api/runs", "raw_path": b"/api/runs",
             "query_string": query.encode("ascii"), "root_path": "", "headers": headers,
             "client": ("127.0.0.1", 50000), "server": ("testserver", 80)}

    async def receive():
        reads["n"] += 1
        i = reads["n"] - 1
        if i < len(pieces):
            return {"type": "http.request", "body": pieces[i], "more_body": i < len(pieces) - 1}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    anyio.run(app, scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    raw = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, json.loads(raw or b"{}"), reads["n"]


def _query(value: str | None) -> str:
    return runs_url("name=p.zip&configs=artifacts.local-e2e.yml", project=value).split("?", 1)[1]


@pytest.mark.parametrize("chunked", [False, True], ids=["content-length", "chunked"])
@pytest.mark.parametrize("value,answer", REFUSED)
def test_a_refusal_reads_no_body_byte(tmp_path, repo, monkeypatch, value, answer, chunked):
    monkeypatch.setattr(RunManager, "_run_thread_body", fake_body)
    app = create_app(tmp_path / "out", repo)
    status, data, reads = _asgi_post(app, _query(value), _zip_bytes(), chunked)
    assert (status, data) == (400, answer)
    assert reads == 0                                         # receive() was never called
    out = tmp_path / "out"
    assert not [p for p in out.rglob("*") if p.is_file()] if out.exists() else True   # no .part, no run folder


@pytest.mark.parametrize("chunked", [False, True], ids=["content-length", "chunked"])
def test_the_harness_counts_reads_when_the_name_is_valid(tmp_path, repo, monkeypatch, chunked):
    monkeypatch.setattr(RunManager, "_run_thread_body", fake_body)
    app = create_app(tmp_path / "out", repo)
    status, data, reads = _asgi_post(app, _query("Shop_API2"), _zip_bytes(), chunked)
    assert status == 202, data
    assert reads >= 1 and data["run_id"].startswith("Shop_API2-")
    assert app.state.manager.runs[data["run_id"]].done.wait(10)


def test_the_flow_check_still_comes_first(tmp_path, repo):
    app = create_app(tmp_path / "out", repo)
    status, data, reads = _asgi_post(app, "name=p.zip&flow=nope", _zip_bytes(), False)
    assert status == 400 and "unknown flow" in data["error"] and reads == 0


def test_the_refusal_through_the_test_client_too(tmp_path, repo, monkeypatch):
    monkeypatch.setattr(RunManager, "_run_thread_body", fake_body)
    c = TestClient(create_app(tmp_path / "out", repo))
    r = c.post(runs_url("name=p.zip", project=None), content=_zip_bytes(), headers={"Content-Type": "application/zip"})
    assert r.status_code == 400 and r.json() == REQUIRED
    r = c.post("/api/runs", params={"name": "p.zip", "project": "주문 관리 시스템"}, content=_zip_bytes())
    assert r.status_code == 400 and r.json() == PATTERN
    assert c.get("/api/runs").json()["runs"] == []


# ---------------------------------------------------------------- the run id starts with the name
def test_the_run_id_of_an_accepted_upload(tmp_path, repo, monkeypatch):
    monkeypatch.setattr(RunManager, "_run_thread_body", fake_body)
    c = TestClient(create_app(tmp_path / "out", repo))
    r = c.post(runs_url("name=%EC%A3%BC%EB%AC%B8.zip&configs=artifacts.local-e2e.yml", project="Shop_API2"),
               content=_zip_bytes(), headers={"Content-Type": "application/zip"})
    assert r.status_code == 202, r.text
    rid = r.json()["run_id"]
    assert re.fullmatch(r"Shop_API2-\d{8}-\d{6}-[0-9a-f]{6}", rid) and RUN_ID_RE.match(rid)
    assert (tmp_path / "out" / rid).is_dir()                  # the folder a person types in a shell
    _wait_done(c, rid)
    uploads = list((tmp_path / "out" / "_uploads").glob("*.zip"))
    assert [p.name for p in uploads] == [f"{rid}-_.zip"]      # the upload file keeps `<run id>-<cleaned name>`


def test_run_id_for_takes_the_project_unchanged():
    assert run_id_for("whatever.zip", now=NOW, token="abc123", project="Shop_API2") == "Shop_API2-20261120-093005-abc123"
    assert run_id_for("repo@a1b2c3d.zip", now=NOW, token="abc123") == "repo-a1b2c3d-20261120-093005-abc123"   # no name: as before


@pytest.mark.parametrize("project", ["A_" * 15, "Z" * 30, "a" * 30, "x-_" * 10])
def test_the_longest_names_give_valid_unique_job_names(project):
    a = run_id_for(None, now=NOW, token="abcde0", project=project)
    b = run_id_for(None, now=NOW, token="abcde1", project=project)     # same project, same second, last hex differs
    assert len(a) == 53 and RUN_ID_RE.match(a)
    for rid in (a, b):
        job = job_name(rid)
        assert len(job) <= 63 and DNS1123.match(job) and job.endswith(rid[-23:].lower())
        assert LABEL_VALUE.match(rid) and len(rid) <= 63               # the sbmig-run-id label takes the id itself
        for attempt in (1, 2, 99, 9999):
            rj = resume_job_name(rid, attempt)
            assert len(rj) <= 63 and DNS1123.match(rj) and rj.endswith(f"{rid[-23:].lower()}-rem{attempt}"), rj
    assert job_name(a) != job_name(b)
    for attempt in (1, 99, 9999):
        assert resume_job_name(a, attempt) != resume_job_name(b, attempt)


def test_case_and_underscore_never_merge_two_runs():
    # Shop_API2 and shop-api2 fold to the same Job prefix; the random part keeps the Jobs apart
    a = run_id_for(None, now=NOW, token="aaaaaa", project="Shop_API2")
    b = run_id_for(None, now=NOW, token="bbbbbb", project="shop-api2")
    assert job_name(a) != job_name(b) and job_name(a).startswith("sbmig-shop-api2-")


def test_render_job_labels_take_the_new_run_id():
    rid = run_id_for(None, now=NOW, token="abc123", project="A_" * 15)
    tpl = Path(__file__).resolve().parents[2] / "deploy" / "k8s" / "81-web-job.template.yaml"
    job = render_job(tpl, rid, {"SBMIG_RUN_ID": rid}, owner="own3r")
    assert job["metadata"]["labels"][contract.RUN_LABEL] == rid
    assert job["spec"]["template"]["metadata"]["labels"][contract.RUN_LABEL] == rid
    assert DNS1123.match(job["metadata"]["name"]) and len(job["metadata"]["name"]) <= 63


@pytest.mark.parametrize("rid", ["run-20261101-000000-abcdef", "web-20261109-010000-abcdef",
                                 "repo-a1b2c3d-20261112-093005-abc123", "z" * 24 + "-20261112-093005-ffffff"])
def test_older_run_ids_keep_their_job_names(rid):
    assert job_name(rid) == f"sbmig-{rid}"
    assert resume_job_name(rid, 3) == f"sbmig-{rid}-rem3"


# ---------------------------------------------------------------- kept with the run and served
def test_in_process_stores_and_serves_the_name(tmp_path, repo, monkeypatch):
    monkeypatch.setattr(RunManager, "_run_thread_body", fake_body)
    c = TestClient(create_app(tmp_path / "out", repo))
    r = c.post(runs_url("name=order.zip&configs=artifacts.local-e2e.yml", project="order-admin"),
               content=_zip_bytes(), headers={"Content-Type": "application/zip"})
    assert r.status_code == 202, r.text
    rid = r.json()["run_id"]
    assert r.json()["status"]["project"] == "order-admin" and r.json()["status"]["upload"] == "order.zip"
    mgr = c.app.state.manager
    first_event = next(iter(mgr.events(rid)))
    assert first_event[0] == "status" and first_event[1]["project"] == "order-admin"       # the SSE status
    st = _wait_done(c, rid)
    assert st["project"] == "order-admin"
    assert [(row["project"], row["upload"]) for row in c.get("/api/runs").json()["runs"]] == [("order-admin", "order.zip")]
    web = json.loads((tmp_path / "out" / rid / "web-status.json").read_text(encoding="utf-8"))
    assert web["project"] == "order-admin" and web["upload"] == "order.zip"
    journal = [json.loads(ln) for ln in (tmp_path / "out" / rid / "journal.jsonl").read_text(encoding="utf-8").splitlines()]
    assert next(e for e in journal if e.get("event") == "web_run")["project"] == "order-admin"
    # a restart of the web pod: a new manager reads web-status.json
    assert RunManager(tmp_path / "out", repo).status(rid)["project"] == "order-admin"


def test_an_in_process_run_cut_by_a_restart_keeps_the_name_from_its_journal(tmp_path, repo):
    rd = tmp_path / "out" / "order-admin-20261120-093005-abc123"
    rd.mkdir(parents=True)
    (rd / "journal.jsonl").write_text(json.dumps({"event": "web_run", "project": "order-admin", "upload": "o.zip"}) + "\n",
                                      encoding="utf-8")                  # no web-status.json: the process died mid-run
    assert RunManager(tmp_path / "out", repo).status(rd.name)["project"] == "order-admin"


def test_job_mode_pins_the_name_and_a_restart_keeps_it(tmp_path, repo):
    out = tmp_path / "out"
    cluster = Cluster(out)
    mgr = _mgr(tmp_path, repo, cluster)
    try:
        rid = mgr.start(ZIP, "o.zip", ["artifacts.local-e2e.yml"], project="Shop_API2")
        assert rid.startswith("Shop_API2-") and mgr.runs[rid].done.wait(10)
        assert RequestRecord.read(out / rid).project == "Shop_API2"
        assert mgr.status(rid)["project"] == "Shop_API2"
        m = cluster.created[0]
        assert m["metadata"]["name"] == job_name(rid) and DNS1123.match(m["metadata"]["name"])
        env = m["spec"]["template"]["spec"]["containers"][0]["env"]
        assert not any("Shop_API2" in str(e.get("name")) for e in env)              # never an env NAME
        carrying = {e["name"]: e["value"] for e in env if "Shop_API2" in str(e.get("value"))}
        assert carrying.pop("SBMIG_RUN_ID") == rid                           # only inside the run id ...
        assert all(v.startswith(str(out / rid)) for v in carrying.values()), carrying   # ... and the run folder's path
        assert "project" not in json.dumps(m["metadata"]["labels"])
    finally:
        mgr.shutdown("test")
    again = RunManager(out, repo)
    assert again.status(rid)["project"] == "Shop_API2" and again.status(rid)["upload"] == "o.zip"


def test_recovery_of_a_queued_job_run_keeps_the_name(tmp_path, repo):
    out = tmp_path / "out"
    rid = "order-admin-20261120-010000-abcdef"
    rd = out / rid
    rd.mkdir(parents=True)
    (rd / "source.zip").write_bytes(ZIP)
    RequestRecord(run_id=rid, accepted_at="2026-11-20T01:00:00Z", upload="o.zip", configs=["artifacts.local-e2e.yml"],
                  zip_sha256=contract.sha256_file(rd / "source.zip"), config_digests={},
                  sbmig_version=contract.sbmig_version(), mode="k8s-job", upload_name="o.zip",
                  project="order-admin").write(rd)
    ControlState(run_id=rid, state="queued", owner="own3r", accepted_at="2026-11-20T01:00:00Z").write(rd)
    cluster = Cluster(out)
    mgr = _mgr(tmp_path, repo, cluster)
    try:
        assert mgr.recovered == [rid] and mgr.runs[rid].project == "order-admin"
        assert mgr.status(rid)["project"] == "order-admin"
        assert _wait(lambda: len(cluster.created) == 1)
        assert cluster.created[0]["metadata"]["name"] == job_name(rid)
    finally:
        mgr.shutdown("test")
        mgr.runs[rid].done.wait(10)


def test_an_older_run_has_no_project_key(tmp_path, repo):
    out = tmp_path / "out"
    old_job = out / "web-20261109-010000-abcdef"
    old_job.mkdir(parents=True)
    RequestRecord(run_id=old_job.name, accepted_at="2026-11-09T01:00:00Z", upload="p.zip", configs=[], zip_sha256="0",
                  config_digests={}, sbmig_version="x", mode="k8s-job", upload_name="주문 관리.zip").write(old_job)
    data = json.loads((old_job / "control" / "request.json").read_text(encoding="utf-8"))
    data.pop("project", None)                                  # what an older Main wrote
    (old_job / "control" / "request.json").write_text(json.dumps(data), encoding="utf-8")
    old_inproc = out / "run-20261101-000000-aaaaaa"
    old_inproc.mkdir(parents=True)
    (old_inproc / "web-status.json").write_text(json.dumps({"exit_code": 0, "upload": "legacy.zip"}), encoding="utf-8")
    mgr = RunManager(out, repo)
    for rd, upload in ((old_job, "주문 관리.zip"), (old_inproc, "legacy.zip")):
        st = mgr.status(rd.name)
        assert "project" not in st and st["upload"] == upload
    rows = mgr.list_recent(0)["runs"]
    assert all("project" not in r for r in rows)


def test_the_request_record_reads_both_shapes():
    base = {"run_id": "r", "accepted_at": "t", "upload": "p.zip", "configs": [], "zip_sha256": "0", "config_digests": {},
            "sbmig_version": "x", "mode": "k8s-job"}
    assert RequestRecord.from_json(base).project is None
    assert RequestRecord.from_json({**base, "project": "order-admin"}).project == "order-admin"


# ---------------------------------------------------------------- admin
def _admin_run(out: Path, rid: str, upload: str | None, project: str | None) -> None:
    rd = out / rid
    (rd / "control").mkdir(parents=True)
    req = {"run_id": rid, "accepted_at": "2026-11-20T01:00:00+00:00", "upload": "x.zip", "configs": [],
           "zip_sha256": "0", "config_digests": {}, "sbmig_version": "x", "mode": "k8s-job", "upload_name": upload}
    if project is not None:
        req["project"] = project
    (rd / "control" / "request.json").write_text(json.dumps(req), encoding="utf-8")
    (rd / "report.json").write_text(json.dumps({"success": True, "project_name": "pom-name"}), encoding="utf-8")


def test_admin_groups_by_the_entered_name(tmp_path):
    out = tmp_path / "out"
    _admin_run(out, "order-admin-20261120-010000-aaaaaa", "order_v1.zip", "order-admin")
    _admin_run(out, "order-admin-20261120-020000-bbbbbb", "order_final.zip", "order-admin")
    _admin_run(out, "web-20261109-010000-cccccc", "legacy shop.zip", None)        # older: grouped by file name
    data = admin.collect_usage(out)
    groups = {p["project"]: sorted(r["run_id"] for r in p["run_list"]) for p in data["projects"]}
    assert groups == {"order-admin": ["order-admin-20261120-010000-aaaaaa", "order-admin-20261120-020000-bbbbbb"],
                      "legacy shop": ["web-20261109-010000-cccccc"]}


def test_project_of_order():
    assert admin.project_of("o.zip", "pom", "x-20261120-010000-abcdef", entered="order-admin") == "order-admin"
    assert admin.project_of("o.zip", "pom", "x-20261120-010000-abcdef") == "o"
    assert admin.project_of(None, "pom", "x-20261120-010000-abcdef") == "pom"
    assert admin.project_of(None, None, "Shop_API2-20261120-010000-abcdef") == "Shop_API2"   # the id's first part
    assert admin.project_of(None, None, "run-20261101-000000-abcdef") == "run"


def test_admin_filter_matches_the_project_field():
    assert "p.project" in ADMIN_PAGE                           # the name filter reads the grouped project name


# ---------------------------------------------------------------- the page
def _input_tag() -> str:
    m = re.search(r'<input[^>]*id="project"[^>]*>', PAGE)
    assert m, "no project input"
    return m.group(0)


def test_page_field_label_and_attributes():
    tag = _input_tag()
    assert re.search(r'<label[^>]*for="project"[^>]*>[^<]*프로젝트명', PAGE)
    for attr in ('required', 'maxlength="30"', 'autocomplete="off"', 'autocapitalize="off"', 'spellcheck="false"',
                 'pattern="[A-Za-z0-9][A-Za-z0-9_\\-]{0,29}"', 'aria-describedby="project-hint"'):
        assert attr in tag, attr
    assert "value=" not in tag                                 # empty at first
    assert 'id="project-hint"' in PAGE and "영문, 숫자, _, - 만" in PAGE and "공백 없이 30자까지" in PAGE
    # before the file is chosen
    assert PAGE.index('id="project"') < PAGE.index('id="drop"')


def test_page_rule_is_the_server_one():
    assert "const PROJECT_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,29}$/;" in SCRIPT
    assert PROJECT_RE.pattern == "^[A-Za-z0-9][A-Za-z0-9_-]{0,29}$"


def test_page_messages_and_disabled_rule():
    assert "'프로젝트명을 입력해 주세요.'" in SCRIPT
    assert "'프로젝트명은 영문, 숫자, _, - 만 쓸 수 있습니다(공백 없이 30자까지, 첫 글자는 영문이나 숫자).'" in SCRIPT
    assert "function canStart()" in SCRIPT and "projectOk()" in SCRIPT
    assert "setAttribute('aria-invalid', 'true')" in SCRIPT
    assert "q.set('project', projectValue())" in SCRIPT
    assert "d.message_ko" in SCRIPT                             # the server's refusal beside the field
    # never filled from the file name
    setfile = SCRIPT.split("function setFile(f) {", 1)[1].split("\n  }\n", 1)[0]
    assert "$('project')" not in setfile and "project" not in setfile.replace("canStart", "")


def test_page_shows_the_project_as_the_run_name():
    assert "run.project ?" in SCRIPT and "setRunName(st.project, st.upload)" in SCRIPT
    assert 'class="upname"' in SCRIPT                            # the file name beside it, quieter
    assert "const fname = run.upload ? String(run.upload) : run.run_id;" in SCRIPT      # older runs as before


def test_runs_url_helper():
    assert runs_url("name=p.zip") == f"/api/runs?name=p.zip&project={PROJECT}"
    assert runs_url("name=p.zip", project=None) == "/api/runs?name=p.zip"
