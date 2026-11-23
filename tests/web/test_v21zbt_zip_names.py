"""v2.1z-bt: the migration's result ZIP is received under the project name (`<project>-migration.zip`).

Server: `GET /api/runs/{id}/files/result.zip` attaches with `contract.zip_download_name(run id, "migration", project)`,
the project being the one the run status reports (in memory, else `control/request.json`, `web-status.json`, the
journal's `web_run` line); every other file keeps its name; withholding and the 404s are unchanged. Every run status
(`GET /api/runs/{id}`, the run list rows, the event stream) carries `zip_name`, present whether or not the ZIP exists.

Page: the migration button 「변환된 프로젝트 받기」 shows the status's `zip_name` as its small file name and uses it as the
`download` attribute; the remediation button 「결과 ZIP 받기」 does the same with the remediation status's `zip_name`;
both fall back to `result.zip`; no text a reader sees names `result.zip` any more."""
from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import sbmig.web
from sbmig.core.config import PACKAGE_ROOT
from sbmig.web import create_app
from sbmig.web.contract import ControlState, RequestRecord, zip_download_name
from sbmig.web.runs import RunManager

from .uploads import runs_url

PAGE = (Path(sbmig.web.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
SCRIPT = PAGE.split("<script>", 1)[1].split("</script>", 1)[0]
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

JOB_ID = "Shop_Api-2-20261123-101010-abc123"
WEB_ID = "order-admin-20261123-101010-def456"
CLI_ID = "run-20261123-101010"


# ---------------------------------------------------------------------------------------------------------- fixtures
@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    shutil.copy(PACKAGE_ROOT / "config" / "artifacts.local-e2e.yml", repo / "config" / "artifacts.local-e2e.yml")
    (repo / "config" / "default.yml").write_text("x: 1\n")
    return repo


@pytest.fixture
def client(tmp_path: Path, repo: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "out", repo))


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("proj/pom.xml", "<project/>")
    return buf.getvalue()


def _run_dir(tmp_path: Path, rid: str, *, result_zip: bool = True) -> Path:
    rd = tmp_path / "out" / rid
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "report.md").write_text("# report\n", encoding="utf-8")
    (rd / "report.json").write_text(json.dumps({"success": True, "blocked": [], "unresolved": []}), encoding="utf-8")
    (rd / "sbmig.log").write_text("log line\n", encoding="utf-8")
    (rd / "changes.patch").write_text("diff --git a/x b/x\n", encoding="utf-8")
    (rd / "errors.jsonl").write_text('{"id": "e1"}\n', encoding="utf-8")
    if result_zip:
        (rd / "result.zip").write_bytes(_zip_bytes())
    return rd


def _request(rd: Path, rid: str, project: str | None) -> None:
    RequestRecord(run_id=rid, accepted_at="2026-11-23T10:10:10Z", upload="shop.zip", configs=[], zip_sha256="0" * 64,
                  config_digests={}, sbmig_version="test", mode="k8s-job", project=project).write(rd)


def _attached_as(client: TestClient, rid: str, name: str = "result.zip", query: str = "") -> str:
    r = client.get(f"/api/runs/{rid}/files/{name}{query}")
    assert r.status_code == 200, r.text
    return r.headers.get("content-disposition", "")


# ------------------------------------------------------------------------------------------- the files route (rule 2)
def test_job_mode_run_is_received_under_the_project_of_request_json(tmp_path, client):
    rd = _run_dir(tmp_path, JOB_ID)
    _request(rd, JOB_ID, "Shop_Api-2")
    ControlState(run_id=JOB_ID, state="finished", owner="o", job="sbmig-x", exit_code=0).write(rd)
    assert _attached_as(client, JOB_ID) == 'attachment; filename="Shop_Api-2-migration.zip"'
    # `?download=1` changes nothing for the ZIP
    assert _attached_as(client, JOB_ID, query="?download=1") == 'attachment; filename="Shop_Api-2-migration.zip"'
    r = client.get(f"/api/runs/{JOB_ID}/files/result.zip")
    # compared with the file on disk: `_zip_bytes()` stamps the entry with the current time, so a second build can differ
    assert r.content == (rd / "result.zip").read_bytes() and r.headers["content-type"] == "application/zip"


def test_in_process_run_reads_the_project_from_web_status_json(tmp_path, client):
    rd = _run_dir(tmp_path, WEB_ID)
    (rd / "web-status.json").write_text(json.dumps({"run_id": WEB_ID, "exit_code": 0, "project": "order-admin"}),
                                        encoding="utf-8")
    assert _attached_as(client, WEB_ID) == 'attachment; filename="order-admin-migration.zip"'


def test_in_process_run_cut_by_a_restart_reads_the_project_from_the_journal(tmp_path, client):
    rd = _run_dir(tmp_path, WEB_ID)
    (rd / "journal.jsonl").write_text(json.dumps({"event": "intake"}) + "\n"
                                      + json.dumps({"event": "web_run", "project": "order-admin"}) + "\n",
                                      encoding="utf-8")
    assert _attached_as(client, WEB_ID) == 'attachment; filename="order-admin-migration.zip"'


def test_a_run_without_a_project_uses_its_run_id(tmp_path, client):
    _run_dir(tmp_path, CLI_ID)                                  # a CLI run: no request.json, no web_run line
    assert _attached_as(client, CLI_ID) == f'attachment; filename="{CLI_ID}-migration.zip"'
    # an older web run (before projects were entered): its request.json carries none
    rd = _run_dir(tmp_path, "shop-20261101-101010-aaaaaa")
    _request(rd, "shop-20261101-101010-aaaaaa", None)
    ControlState(run_id=rd.name, state="finished", owner="o", job="sbmig-y", exit_code=0).write(rd)
    assert _attached_as(client, rd.name) == 'attachment; filename="shop-20261101-101010-aaaaaa-migration.zip"'


def test_a_project_that_is_no_safe_file_name_part_falls_back_to_the_run_id(tmp_path, client):
    rd = _run_dir(tmp_path, WEB_ID)
    (rd / "web-status.json").write_text(json.dumps({"run_id": WEB_ID, "project": "a b/../c"}), encoding="utf-8")
    assert _attached_as(client, WEB_ID) == f'attachment; filename="{WEB_ID}-migration.zip"'


def test_every_other_file_keeps_its_name(tmp_path, client):
    rd = _run_dir(tmp_path, JOB_ID)
    _request(rd, JOB_ID, "Shop_Api-2")
    ControlState(run_id=JOB_ID, state="finished", owner="o", job="sbmig-x", exit_code=0).write(rd)
    assert _attached_as(client, JOB_ID, "report.md", "?download=1") == 'attachment; filename="migration-report.md"'
    assert _attached_as(client, JOB_ID, "report.json", "?download=1") == 'attachment; filename="migration-report.json"'
    assert _attached_as(client, JOB_ID, "errors.jsonl") == 'attachment; filename="errors.jsonl"'
    assert _attached_as(client, JOB_ID, "changes.patch", "?download=1") == 'attachment; filename="changes.patch"'
    for inline in ("report.md", "sbmig.log", "changes.patch"):
        assert not _attached_as(client, JOB_ID, inline).startswith("attachment"), inline


def test_withholding_and_the_404s_are_unchanged(tmp_path, client):
    rd = _run_dir(tmp_path, JOB_ID)
    _request(rd, JOB_ID, "Shop_Api-2")
    ControlState(run_id=JOB_ID, state="running", owner="o", job="sbmig-x").write(rd)    # the final pass is still to come
    r = client.get(f"/api/runs/{JOB_ID}/files/result.zip")
    assert r.status_code == 409 and "once the run has finished" in r.json()["error"]
    assert client.get(f"/api/runs/{JOB_ID}/files/sbmig.log").status_code == 200          # a live file is served
    rd2 = _run_dir(tmp_path, WEB_ID, result_zip=False)
    (rd2 / "web-status.json").write_text(json.dumps({"run_id": WEB_ID, "project": "order-admin"}), encoding="utf-8")
    r = client.get(f"/api/runs/{WEB_ID}/files/result.zip")
    assert r.status_code == 404 and r.json() == {"error": "result.zip not found"}
    assert client.get(f"/api/runs/{WEB_ID}/files/order-admin-migration.zip").status_code == 404   # no second name on disk


def test_the_route_reads_the_project_without_building_the_whole_status(tmp_path, client, monkeypatch):
    rd = _run_dir(tmp_path, WEB_ID)
    (rd / "web-status.json").write_text(json.dumps({"run_id": WEB_ID, "project": "order-admin"}), encoding="utf-8")
    mgr = client.app.state.manager

    def no_status(rid):
        raise AssertionError("the files route must not build the full status")

    monkeypatch.setattr(mgr, "status", no_status)
    assert mgr.project_name(WEB_ID) == "order-admin"
    assert _attached_as(client, WEB_ID) == 'attachment; filename="order-admin-migration.zip"'


def test_project_name_follows_the_status_order(tmp_path, repo):
    mgr = RunManager(tmp_path / "out", repo)
    rd = _run_dir(tmp_path, JOB_ID)
    (rd / "web-status.json").write_text(json.dumps({"run_id": JOB_ID, "project": "from-web-status"}), encoding="utf-8")
    (rd / "journal.jsonl").write_text(json.dumps({"event": "web_run", "project": "from-journal"}) + "\n", encoding="utf-8")
    assert mgr.project_name(JOB_ID) == "from-web-status" == mgr.status(JOB_ID)["project"]
    _request(rd, JOB_ID, "from-request")                       # request.json speaks first
    assert mgr.project_name(JOB_ID) == "from-request" == mgr.status(JOB_ID)["project"]
    assert mgr.project_name("nothing-here") is None


# ------------------------------------------------------------------------------------------------ statuses (rule 4)
def fake_body(self: RunManager, run) -> SimpleNamespace:
    (run.run_dir / "report.md").write_text("# report\n", encoding="utf-8")
    (run.run_dir / "report.json").write_text(json.dumps({"success": True, "blocked": [], "unresolved": []}),
                                             encoding="utf-8")
    (run.run_dir / "result.zip").write_bytes(_zip_bytes())
    return SimpleNamespace(success=True, blocked=[], flags=[])


def test_a_run_in_memory_carries_zip_name_everywhere(tmp_path, repo, monkeypatch):
    monkeypatch.setattr(RunManager, "_run_thread_body", fake_body)
    c = TestClient(create_app(tmp_path / "out", repo))
    r = c.post(runs_url("name=order.zip&configs=artifacts.local-e2e.yml", project="order-admin"),
               content=_zip_bytes(), headers={"Content-Type": "application/zip"})
    assert r.status_code == 202, r.text
    rid = r.json()["run_id"]
    assert r.json()["status"]["zip_name"] == "order-admin-migration.zip"          # the start answer, before any ZIP
    mgr = c.app.state.manager
    kind, first = next(iter(mgr.events(rid)))
    assert kind == "status" and first["zip_name"] == "order-admin-migration.zip"   # the event stream's status
    assert mgr.runs[rid].done.wait(10)
    st = c.get(f"/api/runs/{rid}").json()
    assert st["zip_name"] == "order-admin-migration.zip" and "result.zip" in st["files"]
    assert [row["zip_name"] for row in c.get("/api/runs").json()["runs"]] == ["order-admin-migration.zip"]
    assert _attached_as(c, rid) == 'attachment; filename="order-admin-migration.zip"'
    assert mgr.project_name(rid) == "order-admin"


def test_status_and_list_rows_carry_zip_name_with_or_without_the_zip(tmp_path, client):
    rd = _run_dir(tmp_path, JOB_ID, result_zip=False)
    _request(rd, JOB_ID, "Shop_Api-2")
    _run_dir(tmp_path, CLI_ID)
    (tmp_path / "out" / "empty-folder").mkdir()
    st = client.get(f"/api/runs/{JOB_ID}").json()
    assert st["zip_name"] == "Shop_Api-2-migration.zip" and "result.zip" not in st.get("files", [])
    rows = {row["run_id"]: row for row in client.get("/api/runs").json()["runs"]}
    assert rows[JOB_ID]["zip_name"] == "Shop_Api-2-migration.zip"
    assert rows[CLI_ID]["zip_name"] == f"{CLI_ID}-migration.zip"
    assert rows["empty-folder"]["zip_name"] == "empty-folder-migration.zip"
    # every row's name is the rule applied to the project the row reports
    for rid, row in rows.items():
        assert row["zip_name"] == zip_download_name(rid, "migration", row.get("project")), rid


def test_the_unreadable_row_carries_the_run_id_name(tmp_path, client, monkeypatch):
    mgr = client.app.state.manager
    _run_dir(tmp_path, CLI_ID)
    real = mgr.status

    def status(rid):
        if rid == CLI_ID:
            raise KeyError("x")
        return real(rid)

    monkeypatch.setattr(mgr, "status", status)
    rows = {row["run_id"]: row for row in client.get("/api/runs").json()["runs"]}
    assert rows[CLI_ID]["zip_name"] == f"{CLI_ID}-migration.zip"
    assert mgr._unreadable_row(CLI_ID)["zip_name"] == f"{CLI_ID}-migration.zip"


# --------------------------------------------------------------------------------------------------- page (rule 5)
def _between(start: str, end: str) -> str:
    i = SCRIPT.index(start)
    j = SCRIPT.index(end, i) + len(end)
    return SCRIPT[i:j]


def _function(name: str) -> str:
    m = re.search(r"\n  function " + name + r"\(.*?\n  }\n", SCRIPT, re.DOTALL)
    assert m, name
    return m.group(0)


def _node(js: str, tmp_path: Path) -> dict:
    f = tmp_path / "run.js"
    f.write_text(js, encoding="utf-8")
    p = subprocess.run([NODE, str(f)], capture_output=True, text=True, timeout=60, encoding="utf-8", check=False)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


MIG_BLOCK_START = "    // 변환된 프로젝트 ZIP은 서버가 상태에 담아 보낸 이름"
MIG_BLOCK_END = "set('dl-zip', 'result.zip', zipName);\n"
REM_BLOCK_START = "    const shownName = (name) =>"
REM_BLOCK_END = "      dl.appendChild(a);\n    };\n"


def test_page_buttons_take_the_name_from_the_status():
    assert '<span>변환된 프로젝트 받기 <span class="sub" id="dl-zip-name"></span></span>' in PAGE
    mig = _between(MIG_BLOCK_START, MIG_BLOCK_END)
    assert mig in _function("showResult")
    assert "const zipName = zipNameOf(st);" in mig and "setText('dl-zip-name', zipName);" in mig
    rem = _between(REM_BLOCK_START, REM_BLOCK_END)
    assert "name === 'result.zip' ? zipNameOf(st) : name" in rem
    assert "function renderRemediation(runId, st, force)" in SCRIPT
    assert SCRIPT.index("function renderRemediation(runId, st, force)") < SCRIPT.index(REM_BLOCK_START)
    # names go in as text, never as markup
    assert "innerHTML" not in mig and "innerHTML" not in rem
    assert "st.zip_name" in _function("zipNameOf")


@needs_node
def test_zip_name_of_falls_back_to_result_zip(tmp_path):
    cases = [{"zip_name": "shop-api-migration.zip"}, {"zip_name": " shop-api-remediation.zip "}, {}, {"zip_name": ""},
             {"zip_name": None}, {"zip_name": 3}, {"zip_name": {"x": 1}}, None]
    js = _function("zipNameOf") + f"\nprocess.stdout.write(JSON.stringify({json.dumps(cases)}.map(zipNameOf)));\n"
    assert _node(js, tmp_path) == ["shop-api-migration.zip", "shop-api-remediation.zip", "result.zip", "result.zip",
                                   "result.zip", "result.zip", "result.zip", "result.zip"]


MIG_HARNESS = r"""
const mk = (id) => ({ id, textContent: '', href: '', attrs: {},
  setAttribute(k, v) { this.attrs[k] = String(v); }, removeAttribute(k) { delete this.attrs[k]; if (k === 'href') this.href = ''; } });
const els = { 'dl-report': mk('dl-report'), 'dl-zip': mk('dl-zip'), 'dl-zip-name': mk('dl-zip-name') };
const $ = (id) => els[id];
function setText(id, s) { const el = $(id); if (el.textContent !== s) el.textContent = s; }
let current = 'shop-api-20261123-101010-abc123';
function fileUrl(name, download) { return `/api/runs/${encodeURIComponent(current)}/files/${name}` + (download ? '?download=1' : ''); }
const MIG_DL = { 'report.md': 'migration-report.md', 'report.json': 'migration-report.json' };
__ZIPNAMEOF__
function show(st) {
  const files = st.files || [];
__BLOCK__
  const pick = (id) => ({ href: els[id].href || null, download: els[id].attrs.download || null,
                          disabled: els[id].attrs['aria-disabled'] });
  return { sub: els['dl-zip-name'].textContent, zip: pick('dl-zip'), report: pick('dl-report') };
}
const out = [
  show({ files: ['report.md', 'result.zip'], zip_name: 'shop-api-migration.zip' }),
  show({ files: ['report.md'], zip_name: 'shop-api-migration.zip' }),
  show({ files: ['report.md', 'result.zip'] }),
];
process.stdout.write(JSON.stringify(out));
"""


@needs_node
def test_migration_button_shows_and_downloads_the_status_name(tmp_path):
    js = (MIG_HARNESS.replace("__ZIPNAMEOF__", _function("zipNameOf"))
          .replace("__BLOCK__", _between(MIG_BLOCK_START, MIG_BLOCK_END)))
    with_zip, without_zip, older_server = _node(js, tmp_path)
    url = "/api/runs/shop-api-20261123-101010-abc123/files/"
    assert with_zip == {"sub": "shop-api-migration.zip",
                        "zip": {"href": url + "result.zip?download=1", "download": "shop-api-migration.zip",
                                "disabled": "false"},
                        "report": {"href": url + "report.md?download=1", "download": "migration-report.md",
                                   "disabled": "false"}}
    # no ZIP yet: the name is shown, the button is disabled and carries no download name
    assert without_zip["sub"] == "shop-api-migration.zip"
    assert without_zip["zip"] == {"href": None, "download": None, "disabled": "true"}
    # a status without zip_name (an older server) falls back to result.zip
    assert older_server["sub"] == "result.zip" and older_server["zip"]["download"] == "result.zip"


REM_HARNESS = r"""
function el(tag, cls, text) {
  return { tag, className: cls || '', textContent: text != null ? String(text) : '', kids: [], attrs: {}, href: '',
           setAttribute(k, v) { this.attrs[k] = String(v); }, appendChild(c) { this.kids.push(c); } };
}
function remUrl(id, tail) { return `/api/remediations/${encodeURIComponent(id)}` + (tail || ''); }
const REM_DL = { 'report.md': 'remediation-report.md', 'report.json': 'remediation-report.json' };
__ZIPNAMEOF__
function render(runId, st) {
  const files = st.files, dl = el('div', 'dl');
__BLOCK__
  link('report.md', '조치 결과 보고서', 'quiet');
  link('result.zip', '결과 ZIP 받기', 'quiet', '빌드 실패본', 'bad');
  link('result-verified.zip', '검증된 이전 라운드 결과 받기', 'quiet');
  link('fix.patch', '자동 수정 내용 받기', 'quiet');
  return dl.kids.map((a) => ({ href: a.href, download: a.attrs.download, label: a.kids[0].textContent,
                               sub: a.kids[0].kids[0].textContent, badge: a.kids[1] ? a.kids[1].textContent : null }));
}
const files = ['report.md', 'result.zip', 'result-verified.zip', 'fix.patch'];
process.stdout.write(JSON.stringify([
  render('shop-api-20261123-101010-abc123', { files, zip_name: 'shop-api-remediation.zip' }),
  render('shop-api-20261123-101010-abc123', { files }),
]));
"""


@needs_node
def test_remediation_button_shows_and_downloads_the_remediation_status_name(tmp_path):
    js = (REM_HARNESS.replace("__ZIPNAMEOF__", _function("zipNameOf"))
          .replace("__BLOCK__", _between(REM_BLOCK_START, REM_BLOCK_END)))
    named, older = _node(js, tmp_path)
    base = "/api/remediations/shop-api-20261123-101010-abc123/files/"
    assert [(x["href"], x["download"], x["sub"]) for x in named] == [
        (base + "report.md?download=1", "remediation-report.md", "remediation-report.md"),
        (base + "result.zip", "shop-api-remediation.zip", "shop-api-remediation.zip"),
        (base + "result-verified.zip", "result-verified.zip", "result-verified.zip"),
        (base + "fix.patch", "fix.patch", "fix.patch"),
    ]
    assert named[1]["label"] == "결과 ZIP 받기 " and named[1]["badge"] == "빌드 실패본"
    # the remediation status of an older server has no zip_name: result.zip, the others unchanged
    assert [x["download"] for x in older] == ["remediation-report.md", "result.zip", "result-verified.zip", "fix.patch"]


def _script_code_only() -> str:
    # the script without comments: whole-line comments, and trailing ones that start after every quote is closed
    lines = []
    for line in SCRIPT.splitlines():
        if line.lstrip().startswith("//"):
            continue
        for m in re.finditer(r"\s//\s", line):
            before = line[:m.start()]
            if all(before.count(q) % 2 == 0 for q in "'\"`"):
                line = before
                break
        lines.append(line)
    return "\n".join(lines)


def test_no_text_a_reader_sees_names_result_zip():
    # HTML outside the script and the styles, comments removed
    html = re.sub(r"<script>.*?</script>|<style>.*?</style>|<!--.*?-->", "", PAGE, flags=re.DOTALL)
    assert "result.zip" not in html
    # in the script, `result.zip` is only the file key and the fallback: never inside a longer string
    code = _script_code_only()
    literals = re.findall(r"'[^'\n]*result\.zip[^'\n]*'|\"[^\"\n]*result\.zip[^\"\n]*\"|`[^`\n]*result\.zip[^`\n]*`", code)
    assert literals and set(literals) == {"'result.zip'"}, literals
    # the code-change view's sentence names the ZIP without the old file name, and no longer the patch nobody offers
    assert "const NOT_SHOWN = '변환된 프로젝트 ZIP에서 확인해 주세요.';" in SCRIPT
    assert "changes.patch" not in code
