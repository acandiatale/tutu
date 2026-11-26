"""v2.1z-ck items 3 and 5: a reduced-scope (jdk-jakarta) run whose conversion is done carries
`conversion_complete: true` in the run status (single run, list rows, the `status` event), and the admin page words
its result `converted` (변환 완료, ok colour) instead of `incomplete`. Exit code, `success` and the scope stay as they
are; every other run has no such key and keeps its word."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import sbmig.web
from sbmig.core.config import PACKAGE_ROOT
from sbmig.web import admin, create_app
from sbmig.web.admin import ADMIN_PASSWORD_ENV, _result_word

TEST_PASSWORD = "test-pass"
SALT = "00112233445566778899aabbccddeeff"
ITER = 1000
ADMIN_PAGE = (Path(sbmig.web.__file__).parent / "static" / "admin.html").read_text(encoding="utf-8")


# the steps of a reduced plan as `report.json` stores them
REWRITE_STEP = {"id": "transform.rewrite", "kind": "openrewrite", "phase": "transform",
                "recipes": [{"id": "org.openrewrite.java.migrate.UpgradeToJava21", "source": "builtin"}]}
BUILD_STEP = {"id": "verify.build", "kind": "build", "phase": "verify", "recipes": [], "build_goal": "clean package"}
# an MSA plan: one rewrite step per service; only one of them recorded a result
TWO_REWRITES = {"scope": "jdk-jakarta", "steps": [REWRITE_STEP, {**REWRITE_STEP, "id": "transform.rewrite.svc-b"},
                                                  BUILD_STEP]}


def _report(**over) -> dict:
    # a framework run (reduced scope) whose conversion went through and whose one build failed
    rep = {
        "run_id": "x", "project_name": "example-app", "flow": "full", "started_at": "2026-11-26T00:00:00+00:00",
        "success": False, "blocked": [], "unresolved": [{"x": 1}], "error_entries": [],
        "flags": ["framework-project", "tests-not-compiled"],
        "plan": {"scope": "jdk-jakarta", "steps": [REWRITE_STEP, BUILD_STEP]},
        "profile": {"framework": {"scope": "jdk-jakarta", "name": "Example", "fix_loop": False,
                                  "patterns": ["example-fw-core"]}},
        "phases": [{"rewrite_results": [{"ok": True, "dry_run": False}]}, {"rewrite_results": []}],
        "final_build": {"ok": False, "goal": "clean package", "exit_code": 1, "errors": [{"x": 1}]},
    }
    rep.update(over)
    return rep


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv(ADMIN_PASSWORD_ENV, raising=False)
    monkeypatch.delenv("SBMIG_REDACT_OUTPUT", raising=False)
    for key in ("SBMIG_TOKEN", "SBMIG_OPEN"):
        monkeypatch.delenv(key, raising=False)
    hashed = hashlib.pbkdf2_hmac("sha256", TEST_PASSWORD.encode(), bytes.fromhex(SALT), ITER).hex()
    monkeypatch.setattr(admin, "ADMIN_PASSWORD_PBKDF2", {"iterations": ITER, "salt_hex": SALT, "hash_hex": hashed})


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    shutil.copy(PACKAGE_ROOT / "config" / "artifacts.local-e2e.yml", repo / "config" / "artifacts.local-e2e.yml")
    app = create_app(tmp_path / "out", repo)
    app.state.admin_auth.fail_delay_s = 0
    return TestClient(app)


def _run(client: TestClient, rid: str, report: object | None, *, exit_code: int = 2, flow: str = "full",
         raw: str | None = None) -> Path:
    rd = Path(client.app.state.manager.out_dir) / rid
    rd.mkdir(parents=True)
    if raw is not None:
        (rd / "report.json").write_text(raw, encoding="utf-8")
    elif report is not None:
        (rd / "report.json").write_text(json.dumps(report), encoding="utf-8")
    (rd / "web-status.json").write_text(json.dumps({"run_id": rid, "exit_code": exit_code, "flow": flow,
                                                    "upload": f"{rid}.zip", "stop_requested": False}),
                                        encoding="utf-8")
    return rd


def _status(client: TestClient, rid: str) -> dict:
    r = client.get(f"/api/runs/{rid}")
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# run status
# ---------------------------------------------------------------------------
def test_done_conversion_with_a_failed_build_is_marked_and_nothing_else_changes(client) -> None:
    _run(client, "fw-done", _report())
    st = _status(client, "fw-done")
    assert st["conversion_complete"] is True
    assert st["success"] is False and st["exit_code"] == 2
    assert st["framework"]["scope"] == "jdk-jakarta"
    # the row of the list is the same status dict
    r = client.get("/api/runs")
    assert r.status_code == 200
    row = next(x for x in r.json()["runs"] if x["run_id"] == "fw-done")
    assert row["conversion_complete"] is True and row["success"] is False and row["exit_code"] == 2


def test_status_event_carries_the_key(client) -> None:
    _run(client, "fw-sse", _report())
    with client.stream("GET", "/api/runs/fw-sse/events") as r:
        for line in r.iter_lines():
            if line.startswith("data: "):
                first = json.loads(line[6:])
                break
    assert first["conversion_complete"] is True


@pytest.mark.parametrize("report", [
    _report(flags=["framework-project", "rewrite-failed"]),
    _report(flags=["hollow-reactor"]),
    _report(flags=["aborted:user-stop"]),
    _report(plan={"scope": "full"}),
    _report(phases=[{"rewrite_results": [{"ok": False, "dry_run": False}]}]),
    _report(phases=[{"rewrite_results": [{"ok": True, "dry_run": True}]}]),
    _report(final_build=None),
    _report(final_build={}),
    _report(flow="remediation"),
    _report(plan=TWO_REWRITES),
    _report(plan={"scope": "jdk-jakarta"}),
], ids=["rewrite-failed", "hollow", "aborted", "full-scope", "rewrite-not-ok", "dry-run-only", "no-build",
        "empty-build", "report-flow-remediation", "one-of-two-rewrites", "no-steps"])
def test_other_runs_have_no_key(client, report) -> None:
    _run(client, "other", report)
    st = _status(client, "other")
    assert "conversion_complete" not in st
    assert st["exit_code"] == 2


@pytest.mark.parametrize("code, marked", [(2, True), (0, True), (None, True), (137, False), (1, False), (3, False)])
def test_only_exit_2_0_or_unknown_is_marked(client, code, marked) -> None:
    # a pod killed after report.json was written (137, e.g. during the chained remediation) is not shown as done
    _run(client, "ec", _report(), exit_code=code)
    st = _status(client, "ec")
    assert st["exit_code"] == code
    assert st.get("conversion_complete") is (True if marked else None)


def test_old_folder_without_any_recorded_code_is_marked(client) -> None:
    rd = Path(client.app.state.manager.out_dir) / "old"
    rd.mkdir(parents=True)
    (rd / "report.json").write_text(json.dumps(_report()), encoding="utf-8")
    st = _status(client, "old")
    assert st["exit_code"] is None and st["conversion_complete"] is True


def test_run_without_a_report_has_no_key(client) -> None:
    _run(client, "no-report", None)
    assert "conversion_complete" not in _status(client, "no-report")


def test_remediation_flow_run_has_no_key(client) -> None:
    # the run's recorded flow decides, even when the report itself would satisfy the rule
    rep = _report()
    rep.pop("flow")
    _run(client, "rem-only", rep, flow="remediation")
    st = _status(client, "rem-only")
    assert st["flow"] == "remediation" and "conversion_complete" not in st


@pytest.mark.parametrize("raw", ["[]", "null", "3", '"x"', "{broken"])
def test_report_that_is_not_an_object_still_answers(client, raw) -> None:
    _run(client, "odd", None, raw=raw)
    assert "conversion_complete" not in _status(client, "odd")
    assert client.get("/api/runs").status_code == 200


@pytest.mark.parametrize("over, marked", [
    ({"phases": "x"}, False), ({"plan": []}, False), ({"flags": 5}, False),
    ({"phases": [None, 3, {"rewrite_results": "x"}, {"rewrite_results": [None, "y"]}]}, False),
    ({"plan": {"scope": ["jdk-jakarta"]}}, False), ({"phases": {"a": 1}}, False),
    # a flags value that is not a list is an unreadable report: never read as "no stopping flag"
    ({"flags": None}, False), ({"flags": "rewrite-failed"}, False),
    ({"final_build": {"ok": "no"}}, False),
    ({"plan": {"scope": "jdk-jakarta", "steps": [REWRITE_STEP, {"id": "s"}]}}, False),
    ({"plan": {"scope": "jdk-jakarta", "steps": "x"}}, False),
], ids=["phases-str", "plan-list", "flags-int", "phases-odd", "scope-list", "phases-dict", "flags-null",
        "flags-str", "build-ok-str", "step-without-kind", "steps-str"])
def test_odd_shapes_still_answer(client, over, marked) -> None:
    _run(client, "odd", _report(**over))
    st = _status(client, "odd")
    assert st["exit_code"] == 2 and st["success"] is False
    assert st.get("conversion_complete") is (True if marked else None)
    assert client.get("/api/runs").status_code == 200


# ---------------------------------------------------------------------------
# admin result word
# ---------------------------------------------------------------------------
def test_result_word_for_exit_codes_and_reports() -> None:
    assert _result_word(2, _report()) == "converted"
    assert _result_word(2, _report(flags=["rewrite-failed"])) == "incomplete"
    assert _result_word(2, _report(plan={"scope": "full"})) == "incomplete"
    assert _result_word(2, None) == "incomplete"
    assert _result_word(2, _report(phases="x")) == "incomplete"
    assert _result_word(2, _report(flags=5)) == "incomplete"
    assert _result_word(0, _report(success=True)) == "ok"
    assert _result_word(0, _report()) == "ok"
    assert _result_word(3, _report()) == "aborted"
    assert _result_word(7, _report()) == "exit 7"
    assert _result_word(137, _report()) == "exit 137"
    assert _result_word(2, _report(plan=TWO_REWRITES)) == "incomplete"
    assert _result_word(2, _report(final_build={})) == "incomplete"
    assert _result_word(2, _report(plan={"scope": "jdk-jakarta"})) == "incomplete"
    # no exit code: the word comes from the report
    assert _result_word(None, _report()) == "converted"
    assert _result_word(None, _report(flags=["rewrite-failed"])) == "incomplete"
    assert _result_word(None, _report(flags=["aborted"])) == "aborted"
    assert _result_word(None, _report(success=True)) == "ok"
    assert _result_word(None, None) is None


def test_admin_row_of_such_a_run(client) -> None:
    rd = _run(client, "example-app-20261126-100000-abc123", _report())
    (rd / "control").mkdir()
    (rd / "job-status.json").write_text(json.dumps({"run_id": rd.name, "exit_code": 2}), encoding="utf-8")
    other = _run(client, "example-app-20261126-110000-def456", _report(plan=TWO_REWRITES))
    (other / "job-status.json").write_text(json.dumps({"run_id": other.name, "exit_code": 2}), encoding="utf-8")
    assert client.post("/admin/api/login", json={"password": TEST_PASSWORD}).status_code == 200
    r = client.get("/admin/api/usage")
    assert r.status_code == 200, r.text
    runs = {x["run_id"]: x for p in r.json()["projects"] for x in p["run_list"]}
    assert runs[rd.name]["result"] == "converted" and runs[rd.name]["scope"] == "jdk-jakarta"
    assert runs[other.name]["result"] == "incomplete"


def test_admin_page_maps_converted_to_the_ok_class() -> None:
    table = ADMIN_PAGE.split("const RESULT = {", 1)[1].split("};", 1)[0]
    assert "converted: ['ok', '변환 완료']" in table
    assert "incomplete: ['warn', '미완료']" in table
    # the chip escapes what it shows
    chip = ADMIN_PAGE.split("function resultChip(run) {", 1)[1].split("\n  }\n", 1)[0]
    assert "esc(hit ? hit[1] : raw)" in chip
