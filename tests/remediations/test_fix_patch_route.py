"""The `fix.patch` entry of the remediation files route (INTERFACES "v2.1z-bj" / "v2.1z-bk", Report, route, i18n): the
combined `fix.patch` that `report.write_fix_patch` writes next to `report.md` (every committed fixed round, in round
order) is served only when the saved state has a round committed with `fix.fixed` and a non-empty `fix.changed` AND
the file exists; otherwise 404 and absent from the status' file list. Per-round patches on disk never decide. The
reviews had reproduced a patch served for a round whose fix was not in the result, and with two fixed rounds a
report naming each round's patch while the route served only the last."""
from __future__ import annotations

import pytest

from sbmig.core.config import PACKAGE_ROOT, load_config
from sbmig.remediations import report, service
from sbmig.remediations.models import (
    INCOMPLETE,
    FixResult,
    RemediationState,
    RoundState,
    RunPaths,
    read_json,
    write_json_atomic,
)
from sbmig.remediations.service import REMEDIATIONS_DIR

from .test_api import make_client
from .test_service import _foreign_run, manager

APP = "app/src/main/java/com/example/App.java"


@pytest.fixture(scope="module")
def cfg():
    c = load_config(PACKAGE_ROOT / "config" / "artifacts.local-e2e.yml")
    return c.model_copy(update={"remediation": c.remediation.model_copy(update={"trusted_projects": True})})


@pytest.fixture(autouse=True)
def ivas_env(monkeypatch):
    for k, v in (("IVAS_BASE_URL", "https://ivas.invalid"), ("IVAS_USER_ID", "tester"), ("IVAS_PASSWORD", "pw")):
        monkeypatch.setenv(k, v)


def fixed(n, **kw):
    return FixResult(attempted=True, fixed=True, iterations=1, llm_calls=1, changed=[APP], patch=f"rounds/{n}/fix.patch", **kw)


def _run(tmp_path, cfg, rounds, patches=(1, 2, 3), combine=True):
    """A finished run whose state holds `rounds`, with a `fix.patch` file on disk in every round of `patches`, and
    the combined file written as the report node writes it (`combine`)."""
    rid = _foreign_run(tmp_path, cfg, "r20261120-110000-aaaaaa", INCOMPLETE)
    paths = RunPaths(tmp_path / "out" / REMEDIATIONS_DIR / rid)
    st = RemediationState.from_dict(read_json(paths.state))
    st.rounds = list(rounds)
    write_json_atomic(paths.state, st.to_dict())
    for n in patches:
        paths.round_dir(n).mkdir(parents=True, exist_ok=True)
        (paths.round_dir(n) / "fix.patch").write_text(f"patch of round {n}\n", encoding="utf-8")
    if combine:
        report.write_fix_patch(st, paths)
    return rid, paths, manager(tmp_path, cfg)


def _served(mgr, rid):
    c = make_client(mgr)
    r = c.get(f"/api/remediations/{rid}/files/fix.patch")
    listed = "fix.patch" in mgr.status(rid)["files"]
    return (r.status_code, r.text if r.status_code == 200 else None, listed)


def test_two_fixed_rounds_are_served_as_one_patch(tmp_path, cfg):
    rounds = [RoundState(round=1, committed=True, fix=fixed(1)),
              RoundState(round=2, committed=True, fix=fixed(2)),
              RoundState(round=3, committed=True)]                       # a later round without the loop
    rid, paths, mgr = _run(tmp_path, cfg, rounds)
    assert service._FIXED_FILES["fix.patch"](paths) == paths.root / "fix.patch"
    assert _served(mgr, rid) == (200, "# round 1\npatch of round 1\n# round 2\npatch of round 2\n", True)


def test_a_fixed_round_that_did_not_commit_is_not_served(tmp_path, cfg):
    rounds = [RoundState(round=1, committed=True, fix=fixed(1)),
              RoundState(round=2, committed=False, fix=fixed(2))]
    rid, _paths, mgr = _run(tmp_path, cfg, rounds)
    # the earlier committed fix is in the result; round 2's fix is not, so its patch is not in the file
    assert _served(mgr, rid) == (200, "# round 1\npatch of round 1\n", True)


@pytest.mark.parametrize("why,fix,committed", [
    ("not committed", fixed(1), False),
    ("retry passed, nothing changed", FixResult(attempted=True, fixed=True, changed=[], retry_passed=True), True),
    ("not fixed", FixResult(attempted=True, fixed=False, reason="not fixed: 2 error(s) left"), False),
    ("confirming build failed", FixResult(attempted=True, fixed=False, reason="error: confirming build failed",
                                     discarded_changed=[APP]), False),
    ("not started", FixResult(attempted=False, reason="time budget used up"), False),
])
def test_no_round_in_the_result_means_404_and_not_listed(tmp_path, cfg, why, fix, committed):
    rid, paths, mgr = _run(tmp_path, cfg, [RoundState(round=1, committed=committed, fix=fix)])
    assert (paths.round_dir(1) / "fix.patch").is_file()                    # a file on disk does not decide
    assert _served(mgr, rid) == (404, None, False), why


def test_a_discarded_fix_is_not_served(tmp_path, cfg):
    rs = RoundState(round=1, committed=True,
                    fix_discarded=[FixResult(attempted=True, fixed=False, llm_calls=2, discarded_changed=[APP],
                                             reason="discarded: the candidate was remade")])
    rid, _paths, mgr = _run(tmp_path, cfg, [rs])
    assert _served(mgr, rid) == (404, None, False)


def test_without_a_state_nothing_is_served(tmp_path):
    paths = RunPaths(tmp_path / "run")
    paths.round_dir(1).mkdir(parents=True)
    (paths.round_dir(1) / "fix.patch").write_text("x\n", encoding="utf-8")
    assert not service._FIXED_FILES["fix.patch"](paths).is_file()
    paths.state.write_text("{not json", encoding="utf-8")
    assert not service._FIXED_FILES["fix.patch"](paths).is_file()


def test_the_combined_file_must_exist(tmp_path, cfg):
    """The state says a fix is in the result but no report wrote the combined file yet: nothing to serve, even with
    the per-round patch on disk."""
    rid, _paths, mgr = _run(tmp_path, cfg, [RoundState(round=1, committed=True, fix=fixed(1))], combine=False)
    assert _served(mgr, rid) == (404, None, False)


def test_a_missing_round_patch_is_named_in_the_combined_file(tmp_path, cfg, caplog):
    rounds = [RoundState(round=1, committed=True, fix=fixed(1)), RoundState(round=2, committed=True, fix=fixed(2))]
    with caplog.at_level("WARNING", logger="sbmig.remediations.report"):
        rid, _paths, mgr = _run(tmp_path, cfg, rounds, patches=(2,))
    assert _served(mgr, rid) == (200, "# round 1: patch file missing\n# round 2\npatch of round 2\n", True)
    assert any("round 1" in r.getMessage() and r.levelname == "WARNING" for r in caplog.records)


def test_a_combined_file_left_from_an_earlier_report_is_not_served(tmp_path, cfg):
    """A stale combined file while the saved state has no committed fixed round (written by hand here: the report
    itself removes it, see below) is neither served nor listed."""
    rid, paths, mgr = _run(tmp_path, cfg, [RoundState(round=1, committed=False, fix=fixed(1))], combine=False)
    (paths.root / "fix.patch").write_text("# round 1\nstale\n", encoding="utf-8")
    assert _served(mgr, rid) == (404, None, False)


def test_the_report_removes_the_combined_file_when_no_fix_is_in_the_result(tmp_path, cfg):
    rid, paths, mgr = _run(tmp_path, cfg, [RoundState(round=1, committed=True, fix=fixed(1))])
    assert (paths.root / "fix.patch").is_file()
    st = RemediationState.from_dict(read_json(paths.state))
    st.rounds = [RoundState(round=1, committed=False, fix=fixed(1))]
    write_json_atomic(paths.state, st.to_dict())
    assert report.write_fix_patch(st, paths) is None
    assert not (paths.root / "fix.patch").exists()
    assert not list(paths.root.glob("fix.patch.tmp"))
    assert _served(mgr, rid) == (404, None, False)
