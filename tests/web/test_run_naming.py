"""2026-11-12 (the user, after seeing the page): the run id starts with a slug of the uploaded file name, the page shows the
original file name (Unicode kept) for every run, the footer with server paths is gone, and the abort reason's Korean comes
from the server (`abort_reason_ko`, through `sbmig.report.i18n.tr_line`), not from string matching in the page."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import sbmig.web
from sbmig.web import RunManager, contract, create_app
from sbmig.web.contract import RequestRecord
from sbmig.web.k8s import job_name
from sbmig.web.runs import RUN_ID_RE, RUN_ID_SLUG_MAX, resume_job_name, run_id_for, run_id_slug

from .test_app import _sse, _zip_bytes, fake_body, repo  # noqa: F401
from .test_jobmode_hardening import ZIP, Cluster, _mgr, _seed, _wait

PAGE = (Path(sbmig.web.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
SCRIPT = PAGE.split("<script>", 1)[1].split("</script>", 1)[0]
NOW = datetime(2026, 11, 12, 9, 30, 5)
DNS1123 = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setenv("SBMIG_JOB_POLL_S", "0.05")
    monkeypatch.delenv(contract.MAX_PARALLEL_ENV, raising=False)


@pytest.fixture
def managers():
    made: list[RunManager] = []
    yield made
    for mgr in made:
        mgr.shutdown("test teardown")
        for run in list(mgr.runs.values()):
            run.done.wait(10)


# ---------------------------------------------------------------- the slug and the run id
@pytest.mark.parametrize("name,slug", [
    ("repo@a1b2c3d.zip", "repo-a1b2c3d"),
    ("My Project v1.2_final.ZIP", "my-project-v1-2-final"),
    ("ORDER-API.zip", "order-api"),
    ("주문관리시스템.zip", "web"),                          # Korean only → today's prefix
    ("주문 order 관리.zip", "order"),
    (".zip", "web"),
    ("@@@ ... ___.zip", "web"),                           # punctuation only
    ("", "web"),
    (None, "web"),
    ("../../etc/passwd.zip", "passwd"),                    # no traversal: the basename only
    ("..\\..\\win\\evil..zip", "evil"),
    ("a/b/../c d.zip", "c-d"),
    ("x" * 200 + ".zip", "x" * RUN_ID_SLUG_MAX),
    ("abcdefghijklmnopqrstuvw-xyz.zip", "abcdefghijklmnopqrstuvw"),   # the cut lands on '-': stripped again
    ("project.tar.gz", "project-tar-gz"),
])
def test_slug(name, slug):
    assert run_id_slug(name) == slug
    assert len(slug) <= RUN_ID_SLUG_MAX and not slug.startswith("-") and not slug.endswith("-")


def test_run_id_shape_and_randomness():
    assert run_id_for("repo@a1b2c3d.zip", now=NOW, token="abc123") == "repo-a1b2c3d-20261112-093005-abc123"
    assert run_id_for("한글.zip", now=NOW, token="abc123") == "web-20261112-093005-abc123"
    ids = {run_id_for("same.zip", now=NOW) for _ in range(50)}
    assert len(ids) == 50                                  # same file, same second: the random part differs


def test_the_longest_run_id_fits_every_derived_name():
    rid = run_id_for("z" * 300 + ".zip", now=NOW, token="f" * 6)
    assert len(rid) == RUN_ID_SLUG_MAX + 23 == 47 and RUN_ID_RE.match(rid)
    job = job_name(rid)
    assert job == f"sbmig-{rid}" and len(job) == 53 and DNS1123.match(job)       # never cut: the random part survives
    for attempt in (2, 99, 9999):
        rj = resume_job_name(rid, attempt)
        assert rj == f"{job}-rem{attempt}" and len(rj) <= 63 and DNS1123.match(rj)
    assert len(rid) <= 63 and DNS1123.match(rid)          # label value sbmig-run-id
    assert re.fullmatch(r"[a-z0-9-]+", rid)                # URL segment / Windows file name without escaping


def test_an_existing_directory_is_never_reused(tmp_path: Path, repo: Path, monkeypatch):
    import sbmig.web.runs as runs_mod
    mgr = RunManager(tmp_path / "out", repo)
    (tmp_path / "out" / "p-20261112-093005-aaaaaa").mkdir(parents=True)
    tokens = iter(["aaaaaa", "aaaaaa", "bbbbbb"])
    monkeypatch.setattr(runs_mod, "run_id_for", lambda name, now=None, token=None: f"p-20261112-093005-{next(tokens)}")
    assert mgr._claim_run_id("p.zip") == "p-20261112-093005-bbbbbb"
    assert (tmp_path / "out" / "p-20261112-093005-bbbbbb").is_dir()


# ---------------------------------------------------------------- the upload name, in-process and Job modes
def test_in_process_run_id_and_upload_name(tmp_path: Path, repo: Path, monkeypatch):
    monkeypatch.setattr(RunManager, "_run_thread_body", fake_body)
    client = TestClient(create_app(tmp_path / "out", repo))
    name = "주문 관리 <v2>.zip"
    r = client.post("/api/runs", params={"name": name, "configs": "artifacts.local-e2e.yml"}, content=_zip_bytes(),
                    headers={"Content-Type": "application/zip"})
    assert r.status_code == 202, r.text
    rid = r.json()["run_id"]
    assert rid.startswith("v2-") and r.json()["status"]["upload"] == name
    _sse(client, rid)
    assert client.get(f"/api/runs/{rid}").json()["upload"] == name
    assert [row["upload"] for row in client.get("/api/runs").json()["runs"]] == [name]
    mgr = client.app.state.manager
    assert json.loads((mgr.run_dir(rid) / "web-status.json").read_text(encoding="utf-8"))["upload"] == name
    mgr.runs.pop(rid)                                       # evicted: web-status.json answers
    assert mgr.status(rid)["upload"] == name


def test_job_mode_pins_the_original_name(tmp_path: Path, repo: Path, managers):
    out = tmp_path / "out"
    cluster = Cluster(out)
    managers.append(mgr := _mgr(tmp_path, repo, cluster))
    rid = mgr.start(ZIP, "repo@a1b2c3d.zip", ["artifacts.local-e2e.yml"])
    assert rid.startswith("repo-a1b2c3d-") and mgr.runs[rid].done.wait(10)
    rec = RequestRecord.read(out / rid)
    assert rec.upload_name == "repo@a1b2c3d.zip" and rec.upload == "repo_a1b2c3d.zip"
    assert cluster.created[0]["metadata"]["name"] == job_name(rid)
    mgr.runs.pop(rid)
    assert mgr.status(rid)["upload"] == "repo@a1b2c3d.zip"


def test_old_web_runs_still_list_open_and_recover(tmp_path: Path, repo: Path, managers):
    out = tmp_path / "out"
    _seed(out, "web-20261109-010000-abcdef", "queued")       # an old request.json: no upload_name, the safe name only
    cluster = Cluster(out)
    managers.append(mgr := _mgr(tmp_path, repo, cluster))
    assert mgr.recovered == ["web-20261109-010000-abcdef"]
    assert _wait(lambda: len(cluster.created) == 1)
    st = mgr.status("web-20261109-010000-abcdef")
    assert st["upload"] == "p.zip"
    assert [r["run_id"] for r in mgr.list_runs()] == ["web-20261109-010000-abcdef"]


def test_status_without_any_recorded_name(tmp_path: Path, repo: Path):
    rd = tmp_path / "out" / "web-20261101-000000-aaaaaa"
    rd.mkdir(parents=True)
    (rd / "web-status.json").write_text(json.dumps({"exit_code": 0}), encoding="utf-8")
    assert RunManager(tmp_path / "out", repo).status(rd.name)["upload"] is None


# ---------------------------------------------------------------- abort reason in Korean from the server
def _aborted(tmp_path: Path, reason: str) -> Path:
    rd = tmp_path / "out" / "web-20261112-000000-aaaaaa"
    rd.mkdir(parents=True)
    (rd / "web-status.json").write_text(json.dumps({"exit_code": 3}), encoding="utf-8")
    (rd / "report.json").write_text(json.dumps({"success": False, "flags": ["aborted"],
                                                "manual_followups": [f"run aborted: {reason}"]}), encoding="utf-8")
    return rd


def test_abort_reason_ko_from_the_follow_up_line(tmp_path: Path, repo: Path, monkeypatch):
    import sbmig.report.i18n as i18n
    reason = "vulnerability remediation not started (not ready): IVAS_BASE_URL"

    def tr(lang, line):
        m = re.match(r"run aborted: vulnerability remediation not started \(not ready\): (.+)$", line)
        return f"실행 중단: 취약점 조치를 시작하지 못했습니다(준비 안 됨): {m.group(1)}" if m else line
    monkeypatch.setattr(i18n, "tr_line", tr)
    rd = _aborted(tmp_path, reason)
    st = RunManager(tmp_path / "out", repo).status(rd.name)
    assert st["abort_reason"] == reason
    assert st["abort_reason_ko"] == "취약점 조치를 시작하지 못했습니다(준비 안 됨): IVAS_BASE_URL"


def test_abort_reason_ko_from_the_bare_reason(tmp_path: Path, repo: Path, monkeypatch):
    import sbmig.report.i18n as i18n
    monkeypatch.setattr(i18n, "tr_line", lambda lang, line: "Gradle 프로젝트입니다" if line == "Gradle project" else line)
    rd = _aborted(tmp_path, "Gradle project")
    assert RunManager(tmp_path / "out", repo).status(rd.name)["abort_reason_ko"] == "Gradle 프로젝트입니다"


def test_no_abort_reason_ko_when_nothing_translates(tmp_path: Path, repo: Path):
    rd = _aborted(tmp_path, "Maven 변환 후 신청 부탁드립니다.")
    st = RunManager(tmp_path / "out", repo).status(rd.name)
    assert st["abort_reason"] == "Maven 변환 후 신청 부탁드립니다." and "abort_reason_ko" not in st


# ---------------------------------------------------------------- the page
def test_page_has_no_footer():
    assert "<footer" not in PAGE and "footer {" not in PAGE and "$('foot')" not in SCRIPT and 'id="foot"' not in PAGE
    assert "$('cfgnote').textContent = s;" in SCRIPT          # the config name on the upload card stays


def test_page_shows_the_upload_name():
    assert '<p class="fname" id="runname" hidden></p>' in PAGE
    assert "const fname = run.upload ? String(run.upload) : run.run_id;" in SCRIPT
    assert '<span class="fname" title="${esc(fname)}">${esc(fname)}</span>' in SCRIPT     # escaped, full name in title
    assert "text-overflow: ellipsis" in PAGE
    assert "n.textContent = name; n.title = name;" in SCRIPT and "if (st.upload) setRunName(st.upload);" in SCRIPT


@pytest.mark.parametrize("name", ["주문관리시스템_최종본(2026년 11월 수정 반영) 배포용.zip", "2026-11-12.zip", "123.zip"])
def test_a_slug_without_a_letter_falls_back_to_web(name):
    # the digits left of a Korean name ("2026-11") tell nobody whose project it is and read like the date after them
    assert run_id_slug(name) == "web"


def test_digits_stay_when_the_name_has_letters():
    assert run_id_slug("order-api-2.zip") == "order-api-2"
    assert run_id_slug("v2 주문 시스템.zip") == "v2"
