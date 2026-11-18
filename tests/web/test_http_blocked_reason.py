"""S2 (review 2026-11-18): a run the fix loop aborted for a blocked HTTP repository (`aborted:http-blocked`) writes no
`run aborted: ` line on purpose — its reason is the Handover sentence that starts with the HTTP-blocker prefix. The run
status falls back to that sentence, so the result card shows the Korean sentence the report already has."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from sbmig.core.config import PACKAGE_ROOT
from sbmig.report.handover import HTTP_BLOCKED_FLAG, HTTP_BLOCKED_PREFIX
from sbmig.web.runs import RunManager

LINE = (f"{HTTP_BLOCKED_PREFIX}y `snap` (http://nexus.invalid/snap) via the blocked mirror `maven-default-http-blocker` — "
        "not a project or sbmig problem. Fix the Maven settings: (a) an https URL, or (b) in /data/.m2/settings.xml "
        "(build.settings_file) exempt `snap` from mirror `maven-default-http-blocker` (append `,!<repository id>` to its "
        "`<mirrorOf>`), or add a mirror for `snap` (same URL, `<blocked>false</blocked>`); blocker from /opt/maven/conf/settings.xml")


def _mgr(tmp_path: Path) -> RunManager:
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True, exist_ok=True)
    shutil.copy(PACKAGE_ROOT / "config" / "artifacts.local-e2e.yml", repo / "config" / "artifacts.local-e2e.yml")
    return RunManager(tmp_path / "out", repo, mode="in-process")


def _run(tmp_path: Path, followups: list[str], flags: list[str]) -> str:
    rid = "p-20261118-101010-abcdef"
    rd = tmp_path / "out" / rid
    rd.mkdir(parents=True)
    (rd / "report.json").write_text(json.dumps({"success": False, "flags": flags, "manual_followups": followups,
                                                "blocked": [], "unresolved": []}), encoding="utf-8")
    return rid


def test_the_http_blocked_abort_reason_reaches_the_status_in_korean(tmp_path):
    rid = _run(tmp_path, ["some earlier follow-up", LINE], ["infra:http-blocked", HTTP_BLOCKED_FLAG, "aborted"])
    st = _mgr(tmp_path).status(rid)
    assert st["abort_reason"] == LINE
    assert st["abort_reason_ko"].startswith("Maven(3.8.1 이상)이 HTTP 저장소 `snap` (http://nexus.invalid/snap) 접근을")


def test_a_run_aborted_line_still_wins_and_no_flag_means_no_fallback(tmp_path):
    rid = _run(tmp_path, [LINE, "run aborted: internal error: X"], [HTTP_BLOCKED_FLAG, "aborted"])
    assert _mgr(tmp_path).status(rid)["abort_reason"] == "internal error: X"
    (tmp_path / "out").rename(tmp_path / "old")
    rid = _run(tmp_path, [LINE], ["infra:http-blocked"])              # a warning line, the run was not aborted for it
    assert "abort_reason" not in _mgr(tmp_path).status(rid)
