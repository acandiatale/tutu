"""RemediationManager (sbmig/remediations/service.py): worker thread, state files, approvals, resume / stop / delete and the
restart rule — driven by fakes (no IVAS, Nexus, Maven or network; tmp_path only)."""
from __future__ import annotations

import json
import shutil
import sys
import threading
from pathlib import Path

import pytest

from sbmig.core.config import PACKAGE_ROOT, load_config
from sbmig.remediations import graph
from sbmig.remediations import intake as intake_mod
from sbmig.remediations import planner as planner_mod
from sbmig.remediations import service as service_mod
from sbmig.remediations.models import (
    AWAITING_APPROVAL,
    AWAITING_CONFIRMATION,
    D_APPROVED,
    D_PENDING,
    INTERRUPTED,
    QUEUED,
    REMEDIATIONS_DIR,
    RUNNING,
    STOPPED,
    SUCCEEDED,
    ApprovalMismatch,
    Decision,
    IntakeRejected,
    NotReady,
    RemediationState,
    RoundPlan,
    RunPaths,
    StateConflict,
    read_json,
    write_json_atomic,
)
from sbmig.remediations.service import (
    BadRemediationId,
    NotFinished,
    RemediationManager,
    UnknownRemediation,
    remediation_config,
)

from .fakes import (
    DEFAULT_FILES,
    UNCERTAIN,
    FakeMaven,
    FakeScanner,
    ScriptedPlanner,
    deterministic_zip,
    finding,
    make_components,
    proposal,
    scan_result,
)

WAIT = 15.0


@pytest.fixture(scope="module")
def base_cfg():
    cfg = load_config(PACKAGE_ROOT / "config" / "artifacts.local-e2e.yml")
    # the operator's "trusted projects" confirmation is a readiness condition (default false)
    return cfg.model_copy(update={"remediation": cfg.remediation.model_copy(update={"trusted_projects": True})})


@pytest.fixture(autouse=True)
def ivas_env(monkeypatch):
    monkeypatch.setenv("IVAS_BASE_URL", "https://ivas.invalid")
    monkeypatch.setenv("IVAS_USER_ID", "tester")
    monkeypatch.setenv("IVAS_PASSWORD", "pw-for-tests-only")


def cfg_with(base, remediation=None, build=None):
    upd = {}
    if remediation:
        upd["remediation"] = base.remediation.model_copy(update=remediation)
    if build:
        upd["build"] = base.build.model_copy(update=build)
    return base.model_copy(update=upd) if upd else base


class Factory:
    """components_factory: the same fake bundle for every run (so scanners keep their counters); records the cfgs."""

    def __init__(self, **kw):
        self.kw = kw
        self.cfgs: list = []
        self.calls = 0
        self.comps = None

    def __call__(self, cfg, paths):
        if paths is not None:
            self.calls += 1
            self.cfgs.append(cfg)
        if self.comps is None:
            kw = dict(self.kw)
            kw.setdefault("plan", ScriptedPlanner(policy_hash=planner_mod.policy_hash(cfg)))
            self.comps = make_components(intake=intake_mod, **kw)
        return self.comps


def manager(tmp_path: Path, cfg, factory=None) -> RemediationManager:
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True, exist_ok=True)
    return RemediationManager(tmp_path / "out", repo, components_factory=factory or Factory(),
                              config_loader=lambda names: cfg)


def upload(tmp_path: Path, files=None, raw: bytes | None = None) -> Path:
    p = tmp_path / f"upload-{len(list(tmp_path.glob('upload-*')))}.zip"
    p.write_bytes(raw if raw is not None else deterministic_zip({k: v.encode() for k, v in (files or DEFAULT_FILES).items()}))
    return p


def run_dirs(tmp_path: Path) -> list[Path]:
    root = tmp_path / "out" / REMEDIATIONS_DIR
    return [d for d in root.iterdir() if d.is_dir() and service_mod.ID_RE.match(d.name)] if root.is_dir() else []


def submit_and_wait(mgr, tmp_path, **kw):
    st = mgr.submit(upload(tmp_path, **kw), "app.zip", None)
    assert mgr.wait(st.id, WAIT)
    return mgr.status(st.id)


def one_fix(decision=D_PENDING, cfg=None, **kw):
    scan = kw.pop("scan", None) or FakeScanner({"baseline": [scan_result(finding())], "final-r1": [scan_result()]})
    plan = ScriptedPlanner([RoundPlan(round=1, source_tree_hash="", policy_hash="",
                                      proposals=[proposal(decision=decision, risks=["major"] if decision == D_PENDING else [])])],
                           policy_hash=planner_mod.policy_hash(cfg))
    return Factory(scan=scan, plan=plan, **kw)


class Gate:
    """FakeMaven.on_package that blocks the first `n` package calls until released."""

    def __init__(self, n=1):
        self.n = n
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, project):
        if self.n > 0:
            self.n -= 1
            self.entered.set()
            assert self.release.wait(WAIT)


# ---------------------------------------------------------------------------- submit / queue
def test_submit_runs_to_succeeded_with_fakes(tmp_path, base_cfg):
    f = one_fix(decision="auto", cfg=base_cfg)
    mgr = manager(tmp_path, base_cfg, f)
    st = submit_and_wait(mgr, tmp_path)
    assert st["status"] == SUCCEEDED, st
    assert st["delivery"]["sha256"] and st["pending_approvals"] == 0
    assert st["delivery"]["build_ok"] is True and st["delivery"]["scanned"] is True
    assert {"report.md", "report.json", "result.zip", "source.zip", "manifest.json", "journal.jsonl",
            "plan-r1.json", "candidate-r1.patch", "scan-baseline.json", "scan-final-r1.json"} <= set(st["files"])
    paths = mgr.paths(st["id"])
    state = read_json(paths.state)
    assert state["owner"] == mgr.owner
    events = [json.loads(x)["event"] for x in paths.journal.read_text().splitlines()]
    assert events[0] == "remediation_submitted" and "node_start" in events
    assert all("ts" in json.loads(x) for x in paths.journal.read_text().splitlines())
    assert mgr.result(st["id"])["state"]["status"] == SUCCEEDED
    assert service_mod.ID_RE.match(st["id"])


def test_second_submit_waits_in_line(tmp_path, base_cfg):
    gate = Gate()
    maven = FakeMaven(on_package=gate)
    mgr = manager(tmp_path, base_cfg, Factory(maven=maven))
    a = mgr.submit(upload(tmp_path), "a.zip", None)
    assert gate.entered.wait(WAIT)
    b = mgr.submit(upload(tmp_path), "b.zip", None)
    assert mgr.status(a.id)["status"] == RUNNING
    sb = mgr.status(b.id)
    assert sb["status"] == QUEUED and sb["queue_position"] == 1
    assert len(maven.package_calls) == 1
    gate.release.set()
    assert mgr.wait_idle(WAIT)
    assert mgr.status(a.id)["status"] == SUCCEEDED and mgr.status(b.id)["status"] == SUCCEEDED
    assert [p.parts[-3] for p in maven.package_calls] == [a.id, b.id]


def test_not_ready_refuses_and_leaves_no_directory(tmp_path, base_cfg, monkeypatch):
    monkeypatch.delenv("IVAS_PASSWORD")
    mgr = manager(tmp_path, base_cfg, Factory(maven=FakeMaven(readiness=["JDK 21 not found (/opt/secret/jdk)"])))
    with pytest.raises(NotReady) as ei:
        mgr.submit(upload(tmp_path), "a.zip", None)
    missing = ei.value.details["missing"]
    assert "IVAS_PASSWORD is not set" in missing
    assert any(m.startswith("JDK 21 not found") and "/opt/secret" not in m for m in missing)
    assert run_dirs(tmp_path) == []
    assert "IVAS_PASSWORD is not set" in mgr.readiness()


def test_rejected_zip_leaves_no_directory(tmp_path, base_cfg):
    mgr = manager(tmp_path, base_cfg)
    with pytest.raises(IntakeRejected):
        mgr.submit(upload(tmp_path, raw=b"PK not really a zip"), "a.zip", None)
    with pytest.raises(IntakeRejected):
        mgr.submit(upload(tmp_path, files={"README.txt": "no pom"}), "a.zip", None)
    assert run_dirs(tmp_path) == []


def test_unknown_config_name_is_a_bad_request(tmp_path, base_cfg):
    mgr = manager(tmp_path, base_cfg)
    with pytest.raises(ValueError):
        mgr.submit(upload(tmp_path), "a.zip", ["../secret.yml"])
    assert run_dirs(tmp_path) == []


# ---------------------------------------------------------------------------- restart rule
def _foreign_run(tmp_path, cfg, rid, status):
    """A run directory written by an earlier process (another owner token)."""
    run_dir = tmp_path / "out" / REMEDIATIONS_DIR / rid
    run_dir.mkdir(parents=True)
    res = intake_mod.accept_zip(upload(tmp_path), run_dir, intake_mod.limits_from_config(cfg))
    st = graph.new_state(rid, "old.zip", cfg, res, [], policy_hash=planner_mod.policy_hash(cfg))
    st.status, st.owner, st.reason = status, "an-earlier-process", "kept-reason"
    write_json_atomic(RunPaths(run_dir).state, st.to_dict())
    return rid


def test_restart_marks_running_and_queued_interrupted_and_runs_nothing(tmp_path, base_cfg):
    a = _foreign_run(tmp_path, base_cfg, "r20261111-100000-aaaaaa", RUNNING)
    b = _foreign_run(tmp_path, base_cfg, "r20261111-100001-bbbbbb", QUEUED)
    c = _foreign_run(tmp_path, base_cfg, "r20261111-100002-cccccc", AWAITING_APPROVAL)
    f = Factory()
    mgr = manager(tmp_path, base_cfg, f)
    assert sorted(mgr.interrupted) == [a, b]
    for rid in (a, b):
        st = mgr.status(rid)
        assert st["status"] == INTERRUPTED and st["reason"] == "kept-reason"
        assert "restarted" in st["message"] and "nothing is re-sent" in st["message"]
    assert mgr.status(c)["status"] == AWAITING_APPROVAL
    assert mgr.wait_idle(1.0)
    assert f.calls == 0                                          # nothing ran, nothing was uploaded
    # explicit resume only
    out = mgr.resume(a)
    assert out["status"] in (QUEUED, RUNNING, SUCCEEDED)
    assert mgr.wait(a, WAIT)
    assert mgr.status(a)["status"] == SUCCEEDED
    assert mgr.status(b)["status"] == INTERRUPTED
    assert f.calls == 1


# ---------------------------------------------------------------------------- approvals
def _decision(p: dict, **override) -> Decision:
    d = {"proposal_id": p["id"], "decision": D_APPROVED, "proposal_hash": p["proposal_hash"],
         "source_tree_hash": p["source_tree_hash"], "policy_hash": p["policy_hash"], "diff": p["diff"], "comment": "ok"}
    d.update(override)
    return Decision.from_dict(d)


def test_approvals_all_or_nothing_then_resume_applies(tmp_path, base_cfg):
    f = one_fix(cfg=base_cfg)
    mgr = manager(tmp_path, base_cfg, f)
    st = submit_and_wait(mgr, tmp_path)
    rid = st["id"]
    assert st["status"] == AWAITING_APPROVAL and st["pending_approvals"] == 1
    paths = mgr.paths(rid)
    props = mgr.proposals(rid)
    assert props["round"] == 1 and props["policy_hash"] == planner_mod.policy_hash(base_cfg)
    p = props["proposals"][0]
    assert p["decision"] == D_PENDING and p["proposal_hash"]
    before = paths.plan(1).read_bytes()

    with pytest.raises(StateConflict):
        mgr.resume(rid)                                         # proposals still pending
    bad = [({"proposal_hash": "0" * 64}, "proposal_hash"), ({"source_tree_hash": "x"}, "source_tree_hash"),
           ({"policy_hash": "y"}, "policy_hash"), ({"diff": "--- a\n+++ b\n"}, "diff")]
    for override, field_name in bad:
        with pytest.raises(ApprovalMismatch) as ei:
            mgr.decide(rid, [_decision(p, **override)], "alice")
        assert field_name in ei.value.details["fields"]
        assert paths.plan(1).read_bytes() == before and not paths.approvals.exists()
    with pytest.raises(ApprovalMismatch) as ei:               # duplicates in one request
        mgr.decide(rid, [_decision(p), _decision(p)], "alice")
    assert ei.value.details["fields"] == ["proposal_id"]
    with pytest.raises(ApprovalMismatch):                     # one good + one unknown: nothing written
        mgr.decide(rid, [_decision(p), _decision(p, proposal_id="p-1-99")], "alice")
    assert paths.plan(1).read_bytes() == before and not paths.approvals.exists()

    out = mgr.decide(rid, [_decision(p)], "alice")
    assert out == {"accepted": ["p-1-1"], "pending": 0}
    plan = RoundPlan.from_dict(read_json(paths.plan(1)))
    assert plan.proposals[0].decision == D_APPROVED
    lines = [json.loads(x) for x in paths.approvals.read_text().splitlines()]
    assert len(lines) == 1
    ln = lines[0]
    assert ln["by"] == "alice" and ln["proposal_id"] == "p-1-1" and ln["decision"] == D_APPROVED
    assert ln["proposal_hash"] == p["proposal_hash"] and ln["source_tree_hash"] == p["source_tree_hash"]
    assert ln["policy_hash"] == p["policy_hash"] and len(ln["diff_sha256"]) == 64 and ln["comment"] == "ok" and ln["ts"]
    assert mgr.status(rid)["status"] == AWAITING_APPROVAL      # decide does not resume
    with pytest.raises(ApprovalMismatch):                     # already decided → not pending any more
        mgr.decide(rid, [_decision(p)], "bob")

    mgr.resume(rid)
    assert mgr.wait(rid, WAIT)
    final = mgr.status(rid)
    assert final["status"] == SUCCEEDED, final
    assert "<version>1.1</version>" in (paths.accepted / "pom.xml").read_text()


def test_stale_tree_rejects_the_decision(tmp_path, base_cfg):
    mgr = manager(tmp_path, base_cfg, one_fix(cfg=base_cfg))
    rid = submit_and_wait(mgr, tmp_path)["id"]
    p = mgr.proposals(rid)["proposals"][0]
    pom = mgr.paths(rid).accepted / "pom.xml"
    pom.write_text(pom.read_text() + "\n")
    with pytest.raises(ApprovalMismatch) as ei:
        mgr.decide(rid, [_decision(p)], "alice")
    assert "source_tree_hash" in ei.value.details["fields"]


def test_decide_outside_awaiting_approval_is_a_conflict(tmp_path, base_cfg):
    mgr = manager(tmp_path, base_cfg)
    rid = submit_and_wait(mgr, tmp_path)["id"]
    with pytest.raises(StateConflict):
        mgr.decide(rid, [Decision(proposal_id="p-1-1", decision=D_APPROVED)], "alice")


# ---------------------------------------------------------------------------- uncertain upload
@pytest.mark.parametrize("confirm,uploads", [("reupload", 2), ("receipt", 1)])
def test_uncertain_upload_needs_confirmation(tmp_path, base_cfg, confirm, uploads):
    scan = FakeScanner({"baseline": [UNCERTAIN, scan_result()]})
    f = Factory(scan=scan)
    mgr = manager(tmp_path, base_cfg, f)
    st = submit_and_wait(mgr, tmp_path)
    rid = st["id"]
    assert st["status"] == AWAITING_CONFIRMATION and st["pending_uploads"] == ["baseline"]
    assert scan.uploads == 1
    with pytest.raises(StateConflict):
        mgr.resume(rid)
    if confirm == "reupload":
        mgr.resume(rid, confirm="reupload", by="alice")
    else:
        with pytest.raises(ValueError):
            mgr.resume(rid, confirm="receipt")                # ids required
        mgr.resume(rid, confirm="receipt", project_id="prj-9", analysis_id="anl-9")
    assert mgr.wait(rid, WAIT)
    assert mgr.status(rid)["status"] == SUCCEEDED
    assert scan.uploads == uploads
    if confirm == "receipt":
        assert scan.calls[-1].receipt.analysis_id == "anl-9" and not scan.calls[-1].uploaded
    events = [json.loads(x) for x in mgr.paths(rid).journal.read_text().splitlines()]
    assert any(e["event"] == "resume" and e.get("confirm") == confirm for e in events)


def test_confirm_is_refused_outside_awaiting_confirmation(tmp_path, base_cfg):
    gate = Gate()
    mgr = manager(tmp_path, base_cfg, Factory(maven=FakeMaven(on_package=gate)))
    a = mgr.submit(upload(tmp_path), "a.zip", None)
    assert gate.entered.wait(WAIT)
    mgr.stop(a.id)
    gate.release.set()
    assert mgr.wait(a.id, WAIT)
    with pytest.raises(StateConflict):
        mgr.resume(a.id, confirm="reupload")


# ---------------------------------------------------------------------------- stop
def test_stop_takes_effect_at_the_boundary_then_resume(tmp_path, base_cfg):
    gate = Gate()
    mgr = manager(tmp_path, base_cfg, Factory(maven=FakeMaven(on_package=gate)))
    a = mgr.submit(upload(tmp_path), "a.zip", None)
    assert gate.entered.wait(WAIT)
    out = mgr.stop(a.id)
    assert out["stop_requested"] is True and "not cancelled" in out["note"]
    assert mgr.status(a.id)["stop_requested"] is True
    gate.release.set()
    assert mgr.wait(a.id, WAIT)
    st = mgr.status(a.id)
    assert st["status"] == STOPPED and st["next_node"] == "baseline_scan"
    with pytest.raises(StateConflict):
        mgr.stop(a.id)
    mgr.resume(a.id)
    assert mgr.wait(a.id, WAIT)
    st = mgr.status(a.id)
    assert st["status"] == SUCCEEDED and st["stop_requested"] is False


def test_stop_of_a_queued_run_stops_it_when_picked_up(tmp_path, base_cfg):
    gate = Gate()
    maven = FakeMaven(on_package=gate)
    mgr = manager(tmp_path, base_cfg, Factory(maven=maven))
    a = mgr.submit(upload(tmp_path), "a.zip", None)
    assert gate.entered.wait(WAIT)
    b = mgr.submit(upload(tmp_path), "b.zip", None)
    mgr.stop(b.id)
    gate.release.set()
    assert mgr.wait_idle(WAIT)
    assert mgr.status(a.id)["status"] == SUCCEEDED
    assert mgr.status(b.id)["status"] == STOPPED
    assert len(maven.package_calls) == 1


def test_shutdown_leaves_interrupted_not_stopped(tmp_path, base_cfg, monkeypatch):
    monkeypatch.setattr(service_mod, "SHUTDOWN_JOIN_S", 0.01)
    gate = Gate()
    mgr = manager(tmp_path, base_cfg, Factory(maven=FakeMaven(on_package=gate)))
    a = mgr.submit(upload(tmp_path), "a.zip", None)
    assert gate.entered.wait(WAIT)
    mgr.shutdown()
    gate.release.set()
    assert mgr.wait_idle(WAIT)
    st = mgr.status(a.id)
    assert st["status"] == INTERRUPTED and "shut down" in st["message"]


# ---------------------------------------------------------------------------- delete / files / list
def test_delete_rules(tmp_path, base_cfg):
    gate = Gate()
    mgr = manager(tmp_path, base_cfg, Factory(maven=FakeMaven(on_package=gate)))
    a = mgr.submit(upload(tmp_path), "a.zip", None)
    assert gate.entered.wait(WAIT)
    with pytest.raises(StateConflict):
        mgr.delete(a.id)
    gate.release.set()
    assert mgr.wait(a.id, WAIT)
    root = mgr.paths(a.id).root
    assert mgr.paths(a.id).original_zip.is_file()
    mgr.delete(a.id)
    assert not root.exists()
    with pytest.raises(UnknownRemediation):
        mgr.status(a.id)
    with pytest.raises(UnknownRemediation):
        mgr.delete(a.id)
    assert mgr.list()["total"] == 0


def test_file_whitelist_never_leaves_the_run_dir(tmp_path, base_cfg):
    mgr = manager(tmp_path, base_cfg, one_fix(decision="auto", cfg=base_cfg))
    rid = submit_and_wait(mgr, tmp_path)["id"]
    root = mgr.paths(rid).root.resolve()
    for name in ("report.md", "report.json", "result.zip", "source.zip", "manifest.json", "journal.jsonl",
                 "remediation.log", "delivery.json", "plan-r1.json", "candidate-r1.patch", "build-r1.log",
                 "baseline-build.log", "scan-baseline.json", "scan-final-r1.json"):
        p = mgr.file_path(rid, name)
        assert p is not None and p.is_file() and root in p.parents, name
    for name in ("../state.json", "state.json", "/etc/passwd", "..", "plan-r1.log", "plan-r2.json",
                 "scan-../../state.json", "scan-other.json", "approvals.jsonl", "accepted/pom.xml"):
        assert mgr.file_path(rid, name) is None, name
    with pytest.raises(BadRemediationId):
        mgr.file_path("../" + rid, "report.md")
    with pytest.raises(UnknownRemediation):
        mgr.file_path("r20000101-000000-abcdef", "report.md")
    log_text = mgr.file_path(rid, "remediation.log").read_text()
    assert "pw-for-tests-only" not in log_text and str(root) not in log_text


def test_failed_candidate_is_the_result_zip_and_the_verified_one_is_downloadable(tmp_path, base_cfg):
    scan = FakeScanner({"baseline": [scan_result(finding(), finding("example:lib", "1.1"))],
                        "final-r1": [scan_result(finding("example:lib", "1.1"))]})
    plan = ScriptedPlanner([RoundPlan(round=1, source_tree_hash="", policy_hash="", proposals=[proposal()]),
                            RoundPlan(round=2, source_tree_hash="", policy_hash="",
                                      proposals=[proposal(1, round=2, old="1.1", new="1.2")])],
                           policy_hash=planner_mod.policy_hash(base_cfg))
    f = Factory(scan=scan, plan=plan, maven=FakeMaven(outcomes=[True, True, False]))
    mgr = manager(tmp_path, base_cfg, f)
    st = submit_and_wait(mgr, tmp_path)
    assert st["status"] == "FAILED" and st["reason"] == "candidate_build_failed", st
    assert st["delivery"]["build_ok"] is False and st["delivery"]["scanned"] is False and st["delivery"]["round"] == 2
    assert {"result.zip", "result-verified.zip", "source.zip"} <= set(st["files"])
    paths = mgr.paths(st["id"])
    verified = mgr.file_path(st["id"], "result-verified.zip")
    assert verified is not None and verified.name == "result-r1-verified.zip"
    assert verified.read_bytes() != paths.delivery_zip.read_bytes()


def test_result_verified_is_absent_without_a_failed_candidate(tmp_path, base_cfg):
    mgr = manager(tmp_path, base_cfg, one_fix(decision="auto", cfg=base_cfg))
    st = submit_and_wait(mgr, tmp_path)
    assert "result-verified.zip" not in st["files"] and mgr.file_path(st["id"], "result-verified.zip") is None


def test_list_pages_newest_first(tmp_path, base_cfg):
    mgr = manager(tmp_path, base_cfg)
    ids = [submit_and_wait(mgr, tmp_path)["id"] for _ in range(3)]
    p1 = mgr.list(page=1, size=2)
    assert p1["total"] == 3 and p1["page"] == 1 and p1["size"] == 2
    assert [x["id"] for x in p1["items"]] == [ids[2], ids[1]]
    p2 = mgr.list(page=2, size=2)
    assert [x["id"] for x in p2["items"]] == [ids[0]]
    assert mgr.list(size=500)["size"] == 100
    assert p1["items"][0]["url"] == f"/api/remediations/{ids[2]}" and p1["items"][0]["status"] == SUCCEEDED


def test_result_before_the_end_is_not_finished(tmp_path, base_cfg):
    mgr = manager(tmp_path, base_cfg, one_fix(cfg=base_cfg))
    rid = submit_and_wait(mgr, tmp_path)["id"]
    with pytest.raises(NotFinished) as ei:
        mgr.result(rid)
    assert ei.value.code == "not_finished" and ei.value.details["status"] == AWAITING_APPROVAL


# ---------------------------------------------------------------------------- maven local repository
def test_local_repo_gets_the_remediation_suffix(tmp_path, base_cfg):
    cfg = cfg_with(base_cfg, build={"local_repo": "/m2/repository/"})
    f = Factory()
    mgr = manager(tmp_path, cfg, f)
    submit_and_wait(mgr, tmp_path)
    assert f.cfgs[0].build.local_repo == "/m2/repository-remediation"
    assert cfg.build.local_repo == "/m2/repository/"            # the loaded config is not mutated
    plain = cfg_with(base_cfg, build={"local_repo": None})
    assert remediation_config(plain) is plain
    once = remediation_config(cfg)
    assert remediation_config(once).build.local_repo == "/m2/repository-remediation"


def test_bad_ids_are_refused_before_the_file_system(tmp_path, base_cfg):
    mgr = manager(tmp_path, base_cfg)
    for bad in ("..", "../x", "a/b", "a\\b", "-x", ".hidden", "_remediations", "a b", "x" * 90, "a..b", "", None):
        with pytest.raises(BadRemediationId):
            mgr.status(bad)
    # the migration run id format (chained remediations) is a valid id: unknown, not bad
    for ok in ("r2026", "R20261111-000000-abcdef", "web-20261111-101010-abc123", "run.1_x"):
        with pytest.raises(UnknownRemediation):
            mgr.status(ok)


def test_untrusted_projects_setting_blocks_submit(tmp_path, base_cfg):
    """Without the operator's `remediation.trusted_projects` confirmation nothing is accepted (Maven runs project plugins)."""
    cfg = cfg_with(base_cfg, remediation={"trusted_projects": False})
    fac = Factory()
    mgr = RemediationManager(tmp_path / "out", PACKAGE_ROOT, components_factory=fac, config_loader=lambda names: cfg)
    try:
        missing = mgr.readiness(cfg)
        assert any("trusted_projects" in m for m in missing)
        z = tmp_path / "p.zip"
        import zipfile
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("pom.xml", "<project/>")
        with pytest.raises(NotReady):
            mgr.submit(z, "p.zip", None)
        assert not any((tmp_path / "out" / "_remediations").glob("r*"))
    finally:
        mgr.shutdown()


# ---------------------------------------------------------------------------- web modes / chained remediations
RUN_ID = "web-20261111-101010-abc123"


class FakeRunManager:
    """What RemediationManager uses of RunManager: out_dir, run_dir, status, file_withheld, stop, runs."""

    def __init__(self, out_dir: Path, state: str = "finished"):
        self.out_dir = out_dir
        self.states: dict[str, str] = {}
        self.default_state = state
        self.withheld: set[str] = set()
        self.stopped: list[str] = []
        self.runs: dict = {}
        self.resumed = []
        self.active_resumes: set[str] = set()
        self.resume_stops: list[str] = []

    def run_dir(self, run_id):
        return self.out_dir / run_id

    def status(self, run_id):
        return {"run_id": run_id, "state": self.states.get(run_id, self.default_state)}

    def file_withheld(self, run_id, name):
        return run_id in self.withheld and name not in ("sbmig.log", "journal.jsonl")

    def stop(self, run_id):
        self.stopped.append(run_id)
        return True

    # the remediation resume in a new build pod (RunManager.resume_remediation & co, wave 4)
    resumed: list
    refuse: Exception | None = None

    def resume_remediation(self, run_id):
        if self.refuse is not None:
            raise self.refuse
        self.resumed.append(run_id)
        self.active_resumes.add(run_id)
        return self.remediation_job(run_id)

    def remediation_job(self, run_id):
        if run_id not in self.active_resumes and run_id not in self.resumed:
            return None
        active = run_id in self.active_resumes
        out = {"state": "queued" if active else "finished", "active": active, "attempt": 1 + len(self.resumed),
               "job": f"sbmig-{run_id}-rem2"}
        if active:
            out["queue"] = {"position": 1, "ahead": 0}
        return out

    def remediation_active(self, run_id):
        return run_id in self.active_resumes

    def stop_remediation(self, run_id):
        if run_id not in self.active_resumes:
            return None
        self.active_resumes.discard(run_id)
        self.resume_stops.append(run_id)
        return "withdrawn"


def chained(tmp_path: Path, cfg, run_id: str = RUN_ID, status: str = AWAITING_APPROVAL, configs=None,
            owner: str = "a-build-pod") -> RunPaths:
    """A chained remediation as the build pod leaves it: built by a throw-away in-process manager under build/ up to
    AWAITING_APPROVAL (one pending proposal), then moved to out/<run id>/remediation/ with the run id as state id."""
    builder = RemediationManager(tmp_path / "build", tmp_path / "repo", components_factory=one_fix(cfg=cfg),
                                 config_loader=lambda names: cfg)
    sid = builder.submit(upload(tmp_path), "app.zip", None).id
    assert builder.wait(sid, WAIT)
    builder.shutdown()
    run_dir = tmp_path / "out" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    target = run_dir / service_mod.CHAIN_DIR
    shutil.move(str(tmp_path / "build" / REMEDIATIONS_DIR / sid), str(target))
    paths = RunPaths(target)
    data = read_json(paths.state)
    assert data["status"] == AWAITING_APPROVAL
    data.update({"id": run_id, "status": status, "owner": owner})
    write_json_atomic(paths.state, data)
    if configs is not None:
        write_json_atomic(run_dir / "control" / "request.json", {"run_id": run_id, "configs": configs})
    return paths


class Loader:
    """config_loader that records the names it was asked for."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.calls: list[list[str]] = []

    def __call__(self, names):
        self.calls.append(list(names))
        return self.cfg


def job_manager(tmp_path: Path, cfg, rm=None, factory=None, mode="k8s-job", loader=None) -> RemediationManager:
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True, exist_ok=True)
    return RemediationManager(tmp_path / "out", repo, components_factory=factory or Factory(),
                              config_loader=loader or (lambda names: cfg), mode=mode,
                              run_manager=rm if rm is not None else FakeRunManager(tmp_path / "out"))


@pytest.mark.parametrize("mode", ["k8s-job", "local-job"])
def test_job_mode_never_runs_anything_here(tmp_path, base_cfg, mode):
    f = Factory()
    mgr = job_manager(tmp_path, base_cfg, factory=f, mode=mode)
    assert mgr.job_mode
    with pytest.raises(service_mod.NotAvailable) as ei:
        mgr.submit(upload(tmp_path), "a.zip", None)
    assert ei.value.code == "not_available" and "build pod" in str(ei.value) and "remediation.after_migration" in str(ei.value)
    assert run_dirs(tmp_path) == []
    info = mgr.readiness_info()
    assert info["ready"] is False and info["runs_in"] == "build_pod" and info["mode"] == mode
    assert "build pod" in info["missing"][0]
    assert f.comps is None                       # Maven was never probed, no component was built
    assert mgr._worker is None
    with pytest.raises(service_mod.NotAvailable):
        mgr._enqueue_locked("r20261111-000000-abcdef")
    assert mgr._worker is None


def test_in_process_readiness_says_where_it_runs(tmp_path, base_cfg):
    info = manager(tmp_path, base_cfg).readiness_info()
    assert info == {"ready": True, "missing": [], "mode": "in-process", "runs_in": "web"}


def test_job_mode_chained_reads_and_approvals(tmp_path, base_cfg):
    paths = chained(tmp_path, base_cfg, configs=["company.yml"])
    loader = Loader(base_cfg)
    mgr = job_manager(tmp_path, base_cfg, loader=loader)
    (mgr.repo_root / "config" / "company.yml").write_text("x: 1\n")
    assert mgr.is_chained(RUN_ID) and mgr.paths(RUN_ID).root == paths.root
    st = mgr.status(RUN_ID)
    assert st["kind"] == "chained" and st["run_id"] == RUN_ID and st["id"] == RUN_ID
    assert st["status"] == AWAITING_APPROVAL and st["pending_approvals"] == 1 and "stale" not in st
    assert st["resume"]["available"] is False and "제안이 1건 남아" in st["resume"]["note"]
    assert st["links"]["self"] == f"/api/remediations/{RUN_ID}"
    assert {"source.zip", "plan-r1.json", "journal.jsonl"} <= set(st["files"])
    rows = mgr.list()["items"]
    assert [(r["id"], r["kind"], r.get("run_id")) for r in rows] == [(RUN_ID, "chained", RUN_ID)]
    assert mgr.file_path(RUN_ID, "plan-r1.json").is_file()
    assert mgr.file_path(RUN_ID, "../state.json") is None
    p = mgr.proposals(RUN_ID)["proposals"][0]
    with pytest.raises(NotFinished):
        mgr.result(RUN_ID)
    out = mgr.decide(RUN_ID, [_decision(p)], "alice")
    assert out == {"accepted": [p["id"]], "pending": 0}
    # a chained approval is checked against the policy hash the BUILD POD recorded in state.json — the web pod does not
    # recompute it from its own environment (a differing env value would refuse a valid approval)
    assert loader.calls == []
    assert RoundPlan.from_dict(read_json(paths.plan(1))).proposals[0].decision == D_APPROVED
    assert json.loads(paths.approvals.read_text().splitlines()[0])["by"] == "alice"
    assert mgr.status(RUN_ID)["resume"] == {"available": True, "note": ""}       # decided: a new build pod can resume it
    with pytest.raises(StateConflict):
        mgr.stop(RUN_ID)                                         # not running
    with pytest.raises(StateConflict) as ei:
        mgr.delete(RUN_ID)
    assert "owns its directory" in str(ei.value) and paths.state.is_file()
    assert mgr._worker is None


def test_chained_configs_fall_back_to_the_server_default(tmp_path, base_cfg):
    chained(tmp_path, base_cfg, configs=["gone.yml"])
    mgr = job_manager(tmp_path, base_cfg)
    (mgr.repo_root / "config" / "artifacts.yml").write_text("x: 1\n")
    assert mgr.chained_configs(RUN_ID) == ["artifacts.yml"]
    # in-process runs record their configs in the journal's web_run line
    rd = tmp_path / "out" / RUN_ID
    (rd / "control" / "request.json").unlink()
    (mgr.repo_root / "config" / "mine.yml").write_text("x: 1\n")
    (rd / "journal.jsonl").write_text(json.dumps({"event": "web_run", "configs": ["mine.yml"]}) + "\n")
    assert mgr.chained_configs(RUN_ID) == ["mine.yml"]


def test_chained_list_merges_newest_first(tmp_path, base_cfg):
    chained(tmp_path, base_cfg)
    mgr = manager(tmp_path, base_cfg)
    sid = submit_and_wait(mgr, tmp_path)["id"]
    rows = mgr.list()["items"]
    assert [(r["id"], r["kind"]) for r in rows] == [(sid, "standalone"), (RUN_ID, "chained")]
    assert mgr.list()["total"] == 2 and "run_id" not in rows[0]
    assert mgr.status(sid)["kind"] == "standalone"


def test_stale_chained_state_is_shown_interrupted_without_rewriting(tmp_path, base_cfg):
    paths = chained(tmp_path, base_cfg, status=RUNNING)
    before = paths.state.read_bytes()
    rm = FakeRunManager(tmp_path / "out", state="finished")
    mgr = job_manager(tmp_path, base_cfg, rm=rm)
    st = mgr.status(RUN_ID)
    assert st["status"] == INTERRUPTED and st["stale"] is True and st["recorded_status"] == RUNNING
    assert "build pod" in st["message"]
    row = mgr.list()["items"][0]
    assert row["status"] == INTERRUPTED and row["stale"] is True
    with pytest.raises(StateConflict):
        mgr.stop(RUN_ID)
    assert paths.state.read_bytes() == before and not (tmp_path / "out" / RUN_ID / "stop.request").exists()


def test_running_chained_stop_writes_the_stop_request(tmp_path, base_cfg):
    paths = chained(tmp_path, base_cfg, status=RUNNING)
    before = paths.state.read_bytes()
    rm = FakeRunManager(tmp_path / "out", state="running")
    mgr = job_manager(tmp_path, base_cfg, rm=rm)
    assert mgr.status(RUN_ID)["status"] == RUNNING and mgr.status(RUN_ID)["stop_requested"] is False
    out = mgr.stop(RUN_ID)
    assert out["stop_requested"] is True and "not cancelled" in out["note"]
    assert (tmp_path / "out" / RUN_ID / "stop.request").is_file()
    assert rm.stopped == []                                     # a Job run is not RunManager.stop()ed (it deletes the Job)
    assert paths.state.read_bytes() == before                   # the build pod's state file is left alone
    assert mgr.status(RUN_ID)["stop_requested"] is True
    with pytest.raises(StateConflict):
        mgr.delete(RUN_ID)


def test_startup_never_rewrites_chained_states(tmp_path, base_cfg):
    paths = chained(tmp_path, base_cfg, status=RUNNING, owner="another-pod")
    before = paths.state.read_bytes()
    for mode in ("in-process", "k8s-job"):
        mgr = job_manager(tmp_path, base_cfg, rm=FakeRunManager(tmp_path / "out", state="running"), mode=mode)
        assert mgr.interrupted == [] and paths.state.read_bytes() == before
        assert mgr.status(RUN_ID)["status"] == RUNNING


def test_chained_files_are_withheld_until_the_final_pass(tmp_path, base_cfg):
    chained(tmp_path, base_cfg)
    rm = FakeRunManager(tmp_path / "out")
    rm.withheld.add(RUN_ID)
    mgr = job_manager(tmp_path, base_cfg, rm=rm)
    st = mgr.status(RUN_ID)
    assert st["files"] == ["journal.jsonl"] and st["withheld"] is True
    assert mgr.file_path(RUN_ID, "journal.jsonl").is_file()
    for name in ("plan-r1.json", "source.zip", "manifest.json"):
        with pytest.raises(service_mod.Withheld) as ei:
            mgr.file_path(RUN_ID, name)
        assert ei.value.code == "withheld"
    assert mgr.file_path(RUN_ID, "nope.txt") is None            # unknown names stay 404, not 409
    p = mgr.proposals(RUN_ID)["proposals"][0]
    with pytest.raises(service_mod.Withheld):
        mgr.decide(RUN_ID, [_decision(p)], "alice")
    rm.withheld.clear()
    assert mgr.file_path(RUN_ID, "plan-r1.json").is_file()


class FakeChain:
    """Stands in for sbmig.remediations.chain: resume() finishes the state as SUCCEEDED."""

    def __init__(self):
        self.calls: list[dict] = []

    def resume(self, run_dir, cfg, *, journal, stop, components=None):
        self.calls.append({"run_dir": Path(run_dir), "stop": stop(), "components": components})
        paths = RunPaths(Path(run_dir) / "remediation")
        data = read_json(paths.state)
        data["status"] = SUCCEEDED
        write_json_atomic(paths.state, data)
        journal({"event": "fake_chain_resumed"})
        return RemediationState.from_dict(data)


def test_in_process_chained_resume_goes_through_chain(tmp_path, base_cfg, monkeypatch):
    paths = chained(tmp_path, base_cfg, status=STOPPED)
    fake = FakeChain()
    monkeypatch.setitem(sys.modules, "sbmig.remediations.chain", fake)
    rm = FakeRunManager(tmp_path / "out", state="finished")
    f = Factory()
    mgr = job_manager(tmp_path, base_cfg, rm=rm, factory=f, mode="in-process")
    assert mgr.status(RUN_ID)["resume"]["available"] is True
    out = mgr.resume(RUN_ID)
    assert out["kind"] == "chained"
    assert mgr.wait(RUN_ID, WAIT)
    assert len(fake.calls) == 1 and fake.calls[0]["run_dir"] == paths.root.parent and fake.calls[0]["stop"] is False
    assert fake.calls[0]["components"] is f.comps
    assert mgr.status(RUN_ID)["status"] == SUCCEEDED
    events = [json.loads(x)["event"] for x in paths.journal.read_text().splitlines()]
    assert "resume" in events and "fake_chain_resumed" in events


def test_in_process_chained_resume_rules(tmp_path, base_cfg, monkeypatch):
    paths = chained(tmp_path, base_cfg, status=STOPPED)
    rm = FakeRunManager(tmp_path / "out", state="running")
    mgr = job_manager(tmp_path, base_cfg, rm=rm, mode="in-process")
    with pytest.raises(StateConflict):
        mgr.resume(RUN_ID)                                      # the migration run is still going
    rm.default_state = "finished"
    before = paths.state.read_bytes()
    monkeypatch.setitem(sys.modules, "sbmig.remediations.chain", None)   # chain.py not there (yet)
    with pytest.raises(service_mod.NotAvailable):
        mgr.resume(RUN_ID)
    assert paths.state.read_bytes() == before and mgr._worker is None
    # a stale RUNNING one is written INTERRUPTED only when a person resumes it
    data = read_json(paths.state)
    data["status"] = RUNNING
    write_json_atomic(paths.state, data)
    fake = FakeChain()
    monkeypatch.setitem(sys.modules, "sbmig.remediations.chain", fake)
    mgr.resume(RUN_ID)
    assert mgr.wait(RUN_ID, WAIT)
    assert len(fake.calls) == 1 and mgr.status(RUN_ID)["status"] == SUCCEEDED


# ---------------------------------------------------------------------------- wave 4: resume in a NEW build pod (Job modes)
def test_job_mode_resume_dispatches_a_build_pod_and_reports_the_resume_job(tmp_path, base_cfg):
    paths = chained(tmp_path, base_cfg)
    rm = FakeRunManager(tmp_path / "out")
    mgr = job_manager(tmp_path, base_cfg, rm=rm)
    p = mgr.proposals(RUN_ID)["proposals"][0]
    with pytest.raises(StateConflict) as ei:
        mgr.resume(RUN_ID)                                       # a proposal still waits for a decision
    assert ei.value.details["pending"] == 1 and rm.resumed == []
    mgr.decide(RUN_ID, [_decision(p)], "")                       # no approver name needed
    out = mgr.resume(RUN_ID)
    assert rm.resumed == [RUN_ID] and mgr._worker is None        # nothing runs in the web pod
    assert out["resume_job"]["state"] == "queued" and out["resume_job"]["queue"] == {"position": 1, "ahead": 0}
    assert out["resume"] == {"available": False, "note": service_mod.RESUME_ACTIVE_NOTE}
    assert out["status"] == AWAITING_APPROVAL and out["withheld"] is True       # the build pod owns the files now
    with pytest.raises(StateConflict):
        mgr.resume(RUN_ID)                                       # one resume at a time
    stopped = mgr.stop(RUN_ID)
    assert stopped["resume"] == "withdrawn" and rm.resume_stops == [RUN_ID]
    events = [json.loads(x) for x in paths.journal.read_text().splitlines()]
    assert any(e["event"] == "resume" and e.get("runs_in") == "build_pod" for e in events)


def test_job_mode_refused_dispatch_restores_the_state_file(tmp_path, base_cfg):
    paths = chained(tmp_path, base_cfg, status=RUNNING)          # stale: its pod is gone, the migration finished
    rm = FakeRunManager(tmp_path / "out", state="finished")
    rm.refuse = StateConflict("the web server is shutting down")
    mgr = job_manager(tmp_path, base_cfg, rm=rm)
    before = paths.state.read_bytes()
    with pytest.raises(StateConflict):
        mgr.resume(RUN_ID)
    assert paths.state.read_bytes() == before                    # restored: the next resume starts from the same file
    events = [json.loads(x)["event"] for x in paths.journal.read_text().splitlines()]
    assert events[-1] == "resume_refused"
    rm.refuse = None
    mgr.resume(RUN_ID)                                           # the explicit resume writes INTERRUPTED, then dispatches
    assert read_json(paths.state)["status"] == INTERRUPTED and rm.resumed == [RUN_ID]
    st = mgr.status(RUN_ID)
    assert "stale" not in st and st["resume_job"]["active"] is True


def test_job_mode_resume_refusals(tmp_path, base_cfg):
    chained(tmp_path, base_cfg)
    rm = FakeRunManager(tmp_path / "out")
    mgr = job_manager(tmp_path, base_cfg, rm=rm)
    p = mgr.proposals(RUN_ID)["proposals"][0]
    rm.active_resumes.add(RUN_ID)                                # a resume pod (of another request) owns the state
    with pytest.raises(StateConflict):
        mgr.decide(RUN_ID, [_decision(p)], "")
    with pytest.raises(StateConflict):
        mgr.resume(RUN_ID)
    assert mgr.status(RUN_ID)["resume"]["note"] == service_mod.RESUME_ACTIVE_NOTE
    rm.active_resumes.clear()

    class NoDispatch(FakeRunManager):
        resume_remediation = None                                # an older RunManager without the feature
    mgr2 = job_manager(tmp_path, base_cfg, rm=NoDispatch(tmp_path / "out"))
    assert mgr2.status(RUN_ID)["resume"]["available"] is False
    with pytest.raises(service_mod.NotAvailable):
        mgr2.resume(RUN_ID)


def test_job_mode_receipt_is_written_before_the_job_and_the_new_pod_never_reuploads(tmp_path, monkeypatch):
    """End to end without a cluster: RemediationManager (receipt) → RunManager (k8s-job, fake cluster) → the Worker's action
    in the "pod" → chain.resume with fake components: the resumed baseline scan polls the receipt — zero re-uploads."""
    import os

    from sbmig.remediations import chain
    from sbmig.web import contract, worker
    from sbmig.web.contract import RUN_LABEL, ControlState, JobStatus, RequestRecord
    from sbmig.web.k8s import job_name
    from sbmig.web.localjob import LocalJobClient
    from sbmig.web.runs import RunManager, resume_job_name
    from tests.web.test_jobmode_hardening import TEMPLATE, Cluster

    from .fakes import POM, make_cfg

    monkeypatch.setenv("SBMIG_JOB_POLL_S", "0.05")
    cfg_name = "artifacts.local-e2e.yml"
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    shutil.copy(PACKAGE_ROOT / "config" / cfg_name, repo / "config" / cfg_name)
    (repo / "config" / "default.yml").write_text("x: 1\n")
    out = tmp_path / "out"
    rd = out / RUN_ID
    project = rd / "project"
    for rel, text in {"pom.xml": POM, "src/main/java/App.java": "class App {}\n"}.items():
        (project / rel).parent.mkdir(parents=True, exist_ok=True)
        (project / rel).write_text(text, encoding="utf-8")

    def comps(scan):
        plan = ScriptedPlanner([RoundPlan(round=1, source_tree_hash="", policy_hash="", proposals=[proposal()])])
        return make_components(scan=scan, plan=plan)
    # the first build pod: the baseline upload's result is unknown → AWAITING_CONFIRMATION, saved, pod ends
    first = chain.run_after_migration(rd, project, make_cfg(after_migration=True), run_id=RUN_ID, journal=lambda e: None,
                                      stop=lambda: False, components=comps(FakeScanner({"baseline": [UNCERTAIN]})))
    assert first.status == AWAITING_CONFIRMATION
    sha = first.pending_upload["baseline"]
    # the migration's own Job-mode records: finished, BUILD SUCCESS
    (rd / "source.zip").write_bytes(b"PK\x05\x06" + b"\0" * 18)
    RequestRecord(run_id=RUN_ID, accepted_at="2026-11-11T01:00:00Z", upload="p.zip", configs=[cfg_name],
                  zip_sha256=contract.sha256_file(rd / "source.zip"), config_digests=contract.config_digests(repo, [cfg_name]),
                  sbmig_version=contract.sbmig_version(), mode="k8s-job").write(rd)
    ControlState(run_id=RUN_ID, state="finished", owner="mig", job=job_name(RUN_ID), exit_code=0).write(rd)
    JobStatus(exit_code=0, run_id=RUN_ID, attempt=1).write(rd)
    (rd / "report.md").write_text("# report\n")
    (rd / "report.json").write_text(json.dumps({"success": True}))
    (rd / "result.zip").write_bytes(b"PK\x05\x06" + b"\1" * 18)
    keep = ("report.md", "report.json", "result.zip", "job-status.json", "control/state.json")
    before = {n: (rd / n).read_bytes() for n in keep}

    scan2 = FakeScanner({"baseline": [scan_result(finding())], "final-r1": [scan_result()]})
    pod_runs: list = []

    def child(cmd, cwd, env):                                   # what `sbmig remediation-resume` does in the new pod
        pod_runs.append(cmd)
        chain.resume(Path(cmd[cmd.index("--run-dir") + 1]), make_cfg(), journal=lambda e: None, stop=lambda: False,
                     components=comps(scan2))
        return 0

    class PodCluster(Cluster):
        def __init__(self):
            super().__init__(out, phases=["Pending", "Running", "Running", "Succeeded"], pod_attempt=False)
            self.manifests: dict = {}
            self.seen_state: dict | None = None

        def create_job(self, manifest):
            # intent before create: the confirmation is on disk before the Job exists
            self.seen_state = read_json(rd / service_mod.CHAIN_DIR / "state.json")
            name = super().create_job(manifest)
            self.manifests[name] = manifest
            return name

        def job_state(self, name):
            st = super().job_state(name)
            j = self.jobs.get(name)
            if st.phase == "Running" and j is not None and not j.get("ran"):
                j["ran"] = True
                env = {**os.environ, **LocalJobClient._env_of(self.manifests[name]), "SBMIG_APP_DIR": str(repo),
                       "SBMIG_PYTHON": "py"}
                assert worker.main(environ=env, run_child=child, install_signals=lambda h: (lambda: None)) == 0
            return st

    cluster = PodCluster()
    rm = RunManager(out, repo, mode="k8s-job", job_client=cluster, job_template=TEMPLATE)
    mgr = RemediationManager(out, repo, components_factory=Factory(), config_loader=lambda names: make_cfg(),
                             mode="k8s-job", run_manager=rm)
    try:
        with pytest.raises(StateConflict):
            mgr.resume(RUN_ID)                                   # AWAITING_CONFIRMATION needs a confirmation
        st = mgr.resume(RUN_ID, confirm="receipt", project_id="prj-9", analysis_id="anl-9")
        assert st["resume_job"]["attempt"] == 2
        assert rm.resumes[RUN_ID].done.wait(WAIT)
        assert cluster.seen_state["pending_upload"] == {}
        assert cluster.seen_state["pending_receipt"]["baseline"]["analysis_id"] == "anl-9"
        assert cluster.seen_state["pending_receipt"]["baseline"]["zip_sha256"] == sha
        [m] = cluster.created
        assert m["metadata"]["name"] == resume_job_name(RUN_ID, 2) and m["metadata"]["labels"][RUN_LABEL] == RUN_ID
        assert len(pod_runs) == 1 and pod_runs[0][3] == "remediation-resume"
        # the uncertain baseline is never uploaded again: it is polled by the confirmed receipt (the final scan of the new
        # delivery ZIP is the one upload of this pod)
        assert scan2.calls[0].label == "baseline" and not scan2.calls[0].uploaded
        assert scan2.calls[0].receipt.analysis_id == "anl-9"
        assert [c.label for c in scan2.calls if c.uploaded] == ["final-r1"] and scan2.uploads == 1
        assert read_json(rd / service_mod.CHAIN_DIR / "state.json")["status"] == SUCCEEDED
        assert {n: (rd / n).read_bytes() for n in keep} == before          # the migration's results are untouched
        assert rm.status(RUN_ID)["state"] == "finished" and rm.status(RUN_ID)["success"] is True
        rst = mgr.status(RUN_ID)
        assert rst["status"] == SUCCEEDED and rst["resume_job"]["state"] == "finished" and "withheld" not in rst
        assert JobStatus.read(rd, contract.RESUME_STATUS_FILE).attempt == 2
    finally:
        mgr.shutdown()
        rm.shutdown("test teardown")
