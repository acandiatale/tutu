"""v2.1z-bt: the remediation's result ZIP is received under the project name. `GET /api/remediations/{id}/files/
result.zip` attaches `<project>-remediation.zip` (`sbmig.web.contract.zip_download_name`): a chained or
remediation-flow run reads the name entered at upload from the migration run folder (`control/request.json`, else the
journal's `web_run` line), a run without one and a standalone remediation use the id. The status carries the same
`zip_name`; every other file keeps its name. The report never tells the reader to take `delivery/result.zip` (a path
inside the run folder): only the collapsed processing record names it. Fakes only (no IVAS, Nexus, Maven or network)."""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sbmig.core.config import PACKAGE_ROOT, load_config
from sbmig.remediations import i18n, report
from sbmig.remediations.api import create_router
from sbmig.remediations.models import FAILED, INCOMPLETE, SUCCEEDED, BuildOutcome
from sbmig.web import contract

from .fakes import FakeMaven, FakeScanner, make_run, scan_result
from .test_baseline_tolerated import BASE_ERR, javac_log, tolerated_components, tolerated_run
from .test_graph import one_fix_components, run
from .test_service import RUN_ID, FakeRunManager, chained, job_manager, manager, one_fix, submit_and_wait

RECORD = "\n## 처리 기록\n"


@pytest.fixture(scope="module")
def base_cfg():
    cfg = load_config(PACKAGE_ROOT / "config" / "artifacts.local-e2e.yml")
    return cfg.model_copy(update={"remediation": cfg.remediation.model_copy(update={"trusted_projects": True})})


@pytest.fixture(autouse=True)
def ivas_env(monkeypatch):
    monkeypatch.setenv("IVAS_BASE_URL", "https://ivas.invalid")
    monkeypatch.setenv("IVAS_USER_ID", "tester")
    monkeypatch.setenv("IVAS_PASSWORD", "pw-for-tests-only")


def client(mgr) -> TestClient:
    app = FastAPI()
    app.include_router(create_router(mgr, 10 * 1024 * 1024))
    return TestClient(app)


def request_json(run_dir, project):
    """The migration run's `control/request.json` as Main writes it in the Job modes."""
    contract.RequestRecord(run_id=run_dir.name, accepted_at="2026-11-23T10:10:10Z", upload="app.zip", configs=[],
                           zip_sha256="0" * 64, config_digests={}, sbmig_version="test", mode="k8s-job",
                           project=project).write(run_dir)


def web_run_line(run_dir, project):
    """The journal's `web_run` line the in-process web run writes before the run starts (no request.json there)."""
    with open(run_dir / "journal.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"event": "intake", "source": "app.zip"}) + "\n")
        fh.write(json.dumps({"event": "web_run", "project": project, "flow": "full"}) + "\n")


def disposition(c, rid, name, query=""):
    r = c.get(f"/api/remediations/{rid}/files/{name}{query}")
    assert r.status_code == 200, (name, r.status_code, r.text)
    return r.headers["content-disposition"]


# ---------------------------------------------------------------------------- the files route
def test_chained_run_with_the_project_in_request_json(tmp_path, base_cfg):
    paths = chained(tmp_path, base_cfg)
    assert paths.delivery_zip.is_file()
    request_json(paths.root.parent, "Shop_Api-2")
    c = client(job_manager(tmp_path, base_cfg))
    assert disposition(c, RUN_ID, "result.zip") == 'attachment; filename="Shop_Api-2-remediation.zip"'
    assert c.get(f"/api/remediations/{RUN_ID}/files/result.zip").content == paths.delivery_zip.read_bytes()
    assert c.get(f"/api/remediations/{RUN_ID}").json()["zip_name"] == "Shop_Api-2-remediation.zip"


def test_chained_in_process_run_with_the_project_only_in_the_journal(tmp_path, base_cfg):
    paths = chained(tmp_path, base_cfg)
    web_run_line(paths.root.parent, "billing-batch")
    assert not (paths.root.parent / "control" / "request.json").exists()
    mgr = manager(tmp_path, base_cfg)
    assert mgr.mode == "in-process" and mgr.is_chained(RUN_ID)
    c = client(mgr)
    assert disposition(c, RUN_ID, "result.zip") == 'attachment; filename="billing-batch-remediation.zip"'
    assert mgr.status(RUN_ID)["zip_name"] == "billing-batch-remediation.zip"


def test_chained_run_without_a_project_uses_the_migration_run_id(tmp_path, base_cfg):
    paths = chained(tmp_path, base_cfg)
    request_json(paths.root.parent, None)                        # a run uploaded before the name was asked for
    mgr = job_manager(tmp_path, base_cfg)
    c = client(mgr)
    assert disposition(c, RUN_ID, "result.zip") == f'attachment; filename="{RUN_ID}-remediation.zip"'
    assert mgr.zip_name(RUN_ID) == f"{RUN_ID}-remediation.zip"
    assert c.get(f"/api/remediations/{RUN_ID}").json()["zip_name"] == f"{RUN_ID}-remediation.zip"


def test_standalone_remediation_uses_its_own_id(tmp_path, base_cfg):
    mgr = manager(tmp_path, base_cfg, one_fix(cfg=base_cfg))
    st = submit_and_wait(mgr, tmp_path)
    rid = st["id"]
    assert st["status"] == SUCCEEDED and st["kind"] == "standalone"
    assert st["zip_name"] == f"{rid}-remediation.zip"
    c = client(mgr)
    assert disposition(c, rid, "result.zip") == f'attachment; filename="{rid}-remediation.zip"'
    assert disposition(c, rid, "report.md", "?download=1") == 'attachment; filename="remediation-report.md"'


def test_every_other_file_keeps_its_name(tmp_path, base_cfg):
    paths = chained(tmp_path, base_cfg)
    request_json(paths.root.parent, "shop-api")
    verified = paths.delivery_zip.with_name("result-r1-verified.zip")
    verified.write_bytes(paths.delivery_zip.read_bytes())       # an earlier round's verified delivery kept beside it
    paths.report_md.write_text("# 취약점 조치 결과\n", encoding="utf-8")   # a stop mid-scan wrote no report yet
    paths.report_json.write_text("{}", encoding="utf-8")
    c = client(job_manager(tmp_path, base_cfg))
    assert disposition(c, RUN_ID, "result.zip") == 'attachment; filename="shop-api-remediation.zip"'
    assert disposition(c, RUN_ID, "result-verified.zip") == 'attachment; filename="result-verified.zip"'
    assert disposition(c, RUN_ID, "source.zip") == 'attachment; filename="source.zip"'
    assert disposition(c, RUN_ID, "report.md", "?download=1") == 'attachment; filename="remediation-report.md"'
    assert disposition(c, RUN_ID, "report.json", "?download=1") == 'attachment; filename="remediation-report.json"'
    inline = c.get(f"/api/remediations/{RUN_ID}/files/report.md")              # inline as before
    assert inline.status_code == 200 and not inline.headers.get("content-disposition", "").startswith("attach")
    assert disposition(c, RUN_ID, "journal.jsonl") == 'attachment; filename="journal.jsonl"'


def test_withheld_and_missing_files_answer_as_before(tmp_path, base_cfg):
    paths = chained(tmp_path, base_cfg)
    request_json(paths.root.parent, "shop-api")
    rm = FakeRunManager(tmp_path / "out")
    c = client(job_manager(tmp_path, base_cfg, rm=rm))
    rm.withheld.add(RUN_ID)
    r = c.get(f"/api/remediations/{RUN_ID}/files/result.zip")
    assert r.status_code == 409 and r.json()["code"] == "withheld"
    rm.withheld.clear()
    paths.delivery_zip.unlink()
    r = c.get(f"/api/remediations/{RUN_ID}/files/result.zip")
    assert r.status_code == 404 and r.json()["code"] == "not_found"
    assert c.get("/api/remediations/web-20000101-000000-000000/files/result.zip").status_code == 404


def test_a_name_that_cannot_be_read_never_fails_the_download(tmp_path, base_cfg, monkeypatch):
    paths = chained(tmp_path, base_cfg)
    request_json(paths.root.parent, "shop-api")
    mgr = job_manager(tmp_path, base_cfg)
    c = client(mgr)

    def broken(_run_dir):
        raise OSError("disk went away")
    monkeypatch.setattr(contract, "entered_project", broken)
    assert disposition(c, RUN_ID, "result.zip") == f'attachment; filename="{RUN_ID}-remediation.zip"'
    assert mgr.status(RUN_ID)["zip_name"] == f"{RUN_ID}-remediation.zip"

    def worse(_rid):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(mgr, "zip_name", worse)
    assert disposition(c, RUN_ID, "result.zip") == 'attachment; filename="result.zip"'


# ---------------------------------------------------------------------------- the report
def _reader_part(md: str) -> str:
    """Everything a requester reads: the report without the collapsed processing record."""
    assert RECORD in md
    return md.split(RECORD, 1)[0]


def _steps(md: str) -> str:
    return md.split("## 해야 할 일", 1)[1].split("\n## ", 1)[0]


def test_step_texts():
    assert report._SUCCEEDED_STEP == "**결과 ZIP 받기** — 재검사를 마친 결과 ZIP을 받아 사용해 주세요."
    assert report._SUCCEEDED_STEP_ROUND0 == "**결과 ZIP 받기** — 검사를 마친 결과 ZIP을 받아 사용해 주세요."
    assert report._TOLERATED_BUILD_STEP == ("**바꾼 버전으로 한 번 빌드하기** — 빌드 오류를 고친 뒤 결과 ZIP의 pom.xml "
                                            "버전을 그대로 두고 한 번 빌드해 주세요. 빌드가 통과해야 바꾼 버전을 그대로 "
                                            "쓸 수 있습니다.")


def test_succeeded_report_after_a_change(tmp_path):
    state, paths = make_run(tmp_path)
    out, _ = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, True])))
    assert out.status == SUCCEEDED and out.delivery.round == 1
    md = paths.report_md.read_text(encoding="utf-8")
    assert report._SUCCEEDED_STEP in _steps(md)
    reader = _reader_part(md)
    assert "결과 파일: 결과 ZIP — 빌드 성공, 재검사 완료" in reader
    assert "delivery/result.zip" not in reader
    assert "`delivery/result.zip`" in md.split(RECORD, 1)[1]          # the operators' record keeps the path


def test_succeeded_report_without_a_change(tmp_path):
    state, paths = make_run(tmp_path)
    out, _ = run(state, paths, one_fix_components(scan=FakeScanner({"baseline": [scan_result()]})))
    assert out.status == SUCCEEDED and out.delivery.round == 0
    md = paths.report_md.read_text(encoding="utf-8")
    assert report._SUCCEEDED_STEP_ROUND0 in _steps(md) and report._SUCCEEDED_STEP not in md
    assert "delivery/result.zip" not in _reader_part(md)


def test_tolerated_build_report(tmp_path):
    state, paths = tolerated_run(tmp_path)
    out, _ = run(state, paths, tolerated_components(javac_log(BASE_ERR)))
    assert out.status == INCOMPLETE and out.delivery.build_tolerated and out.delivery.round == 1
    md = paths.report_md.read_text(encoding="utf-8")
    assert report._TOLERATED_BUILD_STEP in _steps(md)
    reader = _reader_part(md)
    assert "결과 파일: 결과 ZIP — 빌드로 확인하지 못함(조치 전과 같은 빌드 오류), 재검사 완료" in reader
    assert "delivery/result.zip" not in reader


def test_failed_delivery_warning(tmp_path):
    state, paths = make_run(tmp_path)
    bad = BuildOutcome(ok=False, exit_code=1, summary="[ERROR] COMPILATION ERROR")
    out, _ = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, bad])))
    assert out.status == FAILED and out.reason == "candidate_build_failed" and out.delivery is not None
    md = paths.report_md.read_text(encoding="utf-8")
    reader = _reader_part(md)
    assert "> **주의:** 결과 ZIP에는 라운드 1에서 빌드에 실패한 수정본(" in reader
    assert "결과 파일: 결과 ZIP — ⚠️ 빌드에 실패한 수정본, 재검사하지 않음" in reader
    assert "delivery/result.zip" not in reader


# ---------------------------------------------------------------------------- the translated messages
# The status's message_ko (the page's #remed-msg) and the report's 「상세:」 line show these right above the button that
# saves <project>-remediation.zip: the Korean says 결과 ZIP, never the run-folder path. Other paths stay.
MESSAGES = [
    ("delivery/result.zip contains the round 2 change set whose candidate build FAILED (proposals: p-2-1; build log "
     "rounds/2/build.log); it was not rescanned; the original is preserved as original/source.zip; the verified delivery "
     "of an earlier round is kept as delivery/result-r1-verified.zip",
     "결과 ZIP에는 수정본 빌드에 실패한 라운드 2의 변경이 들어 있습니다(제안 p-2-1, 빌드 로그 rounds/2/build.log). 이 ZIP은 "
     "재검사하지 않았습니다. 원본은 original/source.zip에 그대로 보관합니다. 이전 라운드에서 검증한 결과 ZIP은 "
     "delivery/result-r1-verified.zip에 있습니다."),
    ("delivery/result.zip contains the round 2 change set whose candidate build FAILED (proposals: p-2-1; build log "
     "rounds/2/build.log); it was not rescanned; the original is preserved as original/source.zip; the scanned delivery "
     "of an earlier round (its build was not confirmed) is kept as delivery/result-r1-verified.zip",
     "결과 ZIP에는 수정본 빌드에 실패한 라운드 2의 변경이 들어 있습니다(제안 p-2-1, 빌드 로그 rounds/2/build.log). 이 ZIP은 "
     "재검사하지 않았습니다. 원본은 original/source.zip에 그대로 보관합니다. 이전 라운드에서 재검사한 결과 ZIP(빌드로는 "
     "확인하지 못함)은 delivery/result-r1-verified.zip에 있습니다."),
    ("round 2 candidate discarded; delivery/result.zip is the verified state of round 1, without the failed change",
     "라운드 2의 수정본은 버렸습니다. 결과 ZIP은 라운드 1에서 검증한 상태이며, 실패한 변경은 들어 있지 않습니다."),
    ("the result ZIP of round 2 could not be written (OSError: disk full); delivery/result.zip is still the result ZIP "
     "of round 1",
     "라운드 2의 결과 ZIP을 쓰지 못했습니다(OSError). 결과 ZIP은 라운드 1의 결과 그대로입니다. 원문: `disk full`"),
    ("the result ZIP of round 2 could not be written (OSError: disk full); delivery/result.zip is still the result ZIP "
     "of round 0",
     "라운드 2의 결과 ZIP을 쓰지 못했습니다(OSError). 결과 ZIP은 바꾼 것이 없는 이전 결과 그대로입니다. 원문: `disk full`"),
    ("delivery/result.zip is now the result ZIP of round 2, but delivery/delivery.json could not be written (OSError: "
     "disk full); the record is completed the next time this run is opened",
     "결과 ZIP은 이제 라운드 2의 결과입니다. 다만 기록 파일(delivery/delivery.json)을 쓰지 못했습니다(OSError). 이 실행을 "
     "다음에 열 때 기록을 마저 맞춥니다. 원문: `disk full`"),
    ("delivery/result.zip is now the result ZIP of round 0, but state.json could not be written (OSError: disk full); "
     "the record is completed the next time this run is opened",
     "결과 ZIP은 이제 바꾼 것이 없는 결과입니다. 다만 기록 파일(state.json)을 쓰지 못했습니다(OSError). 이 실행을 다음에 열 때 "
     "기록을 마저 맞춥니다. 원문: `disk full`"),
]


@pytest.mark.parametrize("english,korean", MESSAGES)
def test_translated_messages_say_result_zip_not_the_run_folder_path(english, korean):
    out = i18n.ko_text(english)
    assert out == korean
    assert "delivery/result.zip" not in out
