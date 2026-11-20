"""The display project name a run reads from its own folder (`sbmig.web.contract.entered_project` /
`display_project`): the reports of a remediation-only run are titled with the project name entered at upload
(v2.1z-be), not with the working folder (`project`) or the uploaded file name. Display only: no identifier changes."""
from __future__ import annotations

import json

from sbmig.web import admin
from sbmig.web.contract import RUN_SLUG, RequestRecord, display_project, entered_project

WEB_ID = "shop-api-20261120-101010-abc123"


def request(run_dir, project="shop-api", run_id=WEB_ID):
    RequestRecord(run_id=run_id, accepted_at="2026-11-20T10:10:10Z", upload="shop.zip", configs=[], zip_sha256="0" * 64,
                  config_digests={}, sbmig_version="test", mode="k8s-job", flow="remediation", project=project
                  ).write(run_dir)


def journal(run_dir, *events):
    run_dir.mkdir(parents=True, exist_ok=True)
    with open(run_dir / "journal.jsonl", "a", encoding="utf-8") as fh:
        fh.writelines(json.dumps(ev) + "\n" for ev in events)


def test_job_mode_reads_request_json(tmp_path):
    rd = tmp_path / WEB_ID
    request(rd, project="Shop_Api-2")
    assert entered_project(rd) == "Shop_Api-2"
    assert display_project(rd, WEB_ID) == "Shop_Api-2"


def test_in_process_reads_the_web_run_journal_line(tmp_path):
    rd = tmp_path / WEB_ID
    journal(rd, {"event": "intake", "source": "x.zip"}, {"event": "web_run", "project": "shop-api", "flow": "remediation"})
    assert entered_project(rd) == "shop-api"
    assert display_project(rd, WEB_ID) == "shop-api"


def test_request_json_wins_over_the_journal(tmp_path):
    rd = tmp_path / WEB_ID
    request(rd, project="from-request")
    journal(rd, {"event": "web_run", "project": "from-journal"})
    assert entered_project(rd) == "from-request"


def test_web_run_without_a_name_falls_back_to_the_run_id_prefix(tmp_path):
    rd = tmp_path / WEB_ID
    request(rd, project=None)
    assert entered_project(rd) is None
    assert display_project(rd, WEB_ID) == "shop-api"
    rd2 = tmp_path / "in-process"
    journal(rd2, {"event": "web_run", "project": None})
    assert display_project(rd2, WEB_ID) == "shop-api"
    assert display_project(rd2, "odd-id") is None                    # not the web shape: today's value stays


def test_direct_cli_run_has_no_display_project(tmp_path):
    # a CLI run id `run-YYYYmmdd-HHMMSS-hex` has the web shape; without a web request it must not read as "run"
    rd = tmp_path / "run-20261120-101010-abc123"
    journal(rd, {"event": "intake", "source": "x.zip"})
    assert entered_project(rd) is None
    assert display_project(rd, rd.name) is None
    assert display_project(tmp_path / "missing", "run-20261120-101010-abc123") is None


def test_blank_or_multiline_names_are_ignored(tmp_path):
    rd = tmp_path / WEB_ID
    journal(rd, {"event": "web_run", "project": "  "})
    assert entered_project(rd) is None
    rd2 = tmp_path / "b"
    journal(rd2, {"event": "web_run", "project": "a\nb"})
    assert entered_project(rd2) is None


def test_corrupt_files_are_not_an_error(tmp_path):
    rd = tmp_path / WEB_ID
    (rd / "control").mkdir(parents=True)
    (rd / "control" / "request.json").write_text("{not json", encoding="utf-8")
    (rd / "journal.jsonl").write_text("garbage\n", encoding="utf-8")
    assert entered_project(rd) is None
    assert display_project(rd, WEB_ID) == "shop-api"                 # the unreadable request.json is still a web trace


def test_run_slug_is_the_admin_pages_pattern():
    assert RUN_SLUG.pattern == admin._RUN_SLUG.pattern
