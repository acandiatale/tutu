"""S4 (review 2026-11-18): `sbmig clean` recognises the uploads of today's web run ids (`<slug>-YYYYMMDD-HHMMSS-<hex>`, the
old `web-…` ids too): an upload of a run that is running or waiting is never planned for deletion, and `--keep-failed`
keeps the uploads of failed runs."""
from __future__ import annotations

import json
from pathlib import Path

from sbmig.fleet.cleanup import plan_clean
from sbmig.web.runs import run_id_for


def _run(out: Path, rid: str, report: dict | None) -> None:
    rd = out / rid
    rd.mkdir(parents=True)
    if report is not None:
        (rd / "report.json").write_text(json.dumps(report), encoding="utf-8")


def test_uploads_of_running_and_failed_runs_are_kept(tmp_path):
    out = tmp_path / "out"
    up = out / "_uploads"
    up.mkdir(parents=True)
    running = run_id_for("my-app 2.zip", token="aaaaaa")          # my-app-2-YYYYMMDD-HHMMSS-aaaaaa
    failed = run_id_for("legacy_shop.zip", token="bbbbbb")
    ok = run_id_for("ok.zip", token="cccccc")
    old = "web-20261101-101010-dddddd"
    _run(out, running, None)                                        # no report: still running or waiting
    _run(out, failed, {"success": False, "flags": ["aborted"], "blocked": []})
    _run(out, ok, {"success": True, "flags": [], "blocked": []})
    _run(out, old, None)
    for rid, name in ((running, "my-app_2.zip"), (failed, "legacy_shop.zip"), (ok, "ok.zip"), (old, "x.zip")):
        (up / f"{rid}-{name}").write_bytes(b"PK")
    plan = [(i.kind, i.path.name, i.owner) for i in plan_clean(out, what=["uploads"])]
    assert ("upload", f"{ok}-ok.zip", ok) in plan and ("upload", f"{failed}-legacy_shop.zip", failed) in plan
    assert not any(running in name or old in name for _, name, _ in plan)
    kept = [i.path.name for i in plan_clean(out, what=["uploads"], keep_failed=True)]
    assert kept == [f"{ok}-ok.zip"]


def test_uploads_of_runs_named_after_a_project_are_recognised(tmp_path):
    """A web run id starts with the project name a person typed: upper-case letters and `_` are allowed there
    (`runs.PROJECT_RE`). The upload of such a run that is still going must never be planned for deletion."""
    out = tmp_path / "out"
    up = out / "_uploads"
    up.mkdir(parents=True)
    running = "Shop_API2-20261120-101500-a1b2c3"
    failed = "ORDER-admin_v2-20261120-101501-d4e5f6"
    done = "x-20261120-101502-0a0b0c"
    _run(out, running, None)
    _run(out, failed, {"success": False, "flags": ["aborted"], "blocked": []})
    _run(out, done, {"success": True, "flags": [], "blocked": []})
    for rid in (running, failed, done):
        (up / f"{rid}-upload.zip").write_bytes(b"PK")
    plan = [(i.kind, i.path.name, i.owner) for i in plan_clean(out, what=["uploads"])]
    assert not any(running in name for _, name, _ in plan)
    assert ("upload", f"{failed}-upload.zip", failed) in plan and ("upload", f"{done}-upload.zip", done) in plan
    assert [i.path.name for i in plan_clean(out, what=["uploads"], keep_failed=True)] == [f"{done}-upload.zip"]
