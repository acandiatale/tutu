"""The chained remediation (sbmig/remediations/chain.py): run in the build pod after a migration's BUILD SUCCESS.
Fakes only — no IVAS, Nexus, Maven or network."""
from __future__ import annotations

import json
import os
import zipfile

import pytest

from sbmig.remediations import chain
from sbmig.remediations import graph as graph_mod
from sbmig.remediations.models import (
    AWAITING_APPROVAL,
    FAILED,
    STOPPED,
    SUCCEEDED,
    RemediationState,
    RoundPlan,
    ScanResult,
    StateConflict,
    UploadReceipt,
    read_json,
    write_json_atomic,
)

from .fakes import (
    POM,
    FakeMaven,
    FakeScanner,
    ScriptedPlanner,
    finding,
    make_cfg,
    make_components,
    proposal,
    scan_result,
)

ENV = {"IVAS_BASE_URL": "https://ivas.invalid", "IVAS_USER_ID": "tester", "IVAS_PASSWORD": "not-a-real-pw"}


@pytest.fixture()
def ivas_env(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)


def project(tmp_path, files=None):
    root = tmp_path / "run" / "project"
    files = files if files is not None else {"pom.xml": POM, "src/main/java/App.java": "class App {}\n"}
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path / "run", root


def one_fix_components(**kw):
    scan = FakeScanner({"baseline": [scan_result(finding())], "final-r1": [scan_result()]})
    plan = ScriptedPlanner([RoundPlan(round=1, source_tree_hash="", policy_hash="", proposals=[proposal()])])
    return make_components(scan=kw.pop("scan", scan), plan=kw.pop("plan", plan), **kw)


class Journal:
    def __init__(self):
        self.events = []

    def __call__(self, event):
        self.events.append(dict(event))

    def names(self):
        return [e["event"] for e in self.events]


def go(run_dir, root, cfg=None, comps=None, journal=None, stop=None, **kw):
    journal = journal if journal is not None else Journal()
    out = chain.run_after_migration(run_dir, root, cfg or make_cfg(after_migration=True), run_id="run-1",
                                    journal=journal, stop=stop or (lambda: False), components=comps, **kw)
    return out, journal


# ---------------------------------------------------------------------------- skip rules
def test_disabled_returns_none_silently(tmp_path, ivas_env):
    run_dir, root = project(tmp_path)
    out, j = go(run_dir, root, cfg=make_cfg(), comps=one_fix_components())
    assert out is None and j.events == [] and not (run_dir / "remediation").exists()


def test_not_ready_is_skipped_with_names(tmp_path, monkeypatch, caplog):
    for k in ENV:
        monkeypatch.delenv(k, raising=False)
    run_dir, root = project(tmp_path)
    out, j = go(run_dir, root, comps=make_components(maven=FakeMaven(readiness=["JDK 21 not found"])))
    assert out is None
    ev = [e for e in j.events if e["event"] == "remediation_skipped"]
    assert len(ev) == 1 and ev[0]["scope"] == "remediation" and ev[0]["reason"] == "not_ready"
    missing = " ".join(ev[0]["missing"])
    assert "IVAS_BASE_URL" in missing and "IVAS_PASSWORD" in missing and "JDK 21 not found" in missing
    assert not (run_dir / "remediation").exists()
    assert any(r.levelname == "WARNING" and "skipped" in r.getMessage() for r in caplog.records)


def test_readiness_does_not_require_trusted_projects(ivas_env):
    assert chain.readiness(make_cfg(trusted_projects=False), make_components()) == []


def test_readiness_names_a_component_that_cannot_be_built(ivas_env, monkeypatch):
    def boom(cfg, journal=None):
        raise RuntimeError("no settings")
    monkeypatch.setattr(graph_mod, "default_components", boom)
    assert chain.readiness(make_cfg(), None) == ["remediation components cannot be built (RuntimeError)"]


# ---------------------------------------------------------------------------- happy path
def test_happy_path_succeeds_and_writes_under_the_chain_dir(tmp_path, ivas_env):
    run_dir, root = project(tmp_path)
    (root / "target").mkdir()
    (root / "target" / "app.jar").write_bytes(b"jar")
    comps = one_fix_components()
    out, j = go(run_dir, root, comps=comps)
    assert out is not None and out.status == SUCCEEDED, (out.reason, out.message)
    assert out.id == "run-1"
    paths = chain.chain_paths(run_dir)
    assert paths.root == run_dir / "remediation"
    assert paths.original_zip.is_file() and paths.accepted.is_dir() and paths.delivery_zip.is_file()
    assert read_json(paths.state)["status"] == SUCCEEDED
    assert chain.load_state(run_dir).status == SUCCEEDED
    # the migrated project itself is untouched (the remediation works on its own copy)
    assert "<version>1.0</version>" in (root / "pom.xml").read_text(encoding="utf-8")
    names = j.names()
    assert names[0] == "remediation_start" and names[-1] == "remediation_end"
    assert all(e["scope"] == "remediation" for e in j.events)
    assert "node_start" in names
    # the ZIP the intake got: root pom at the ZIP root, no build output
    with zipfile.ZipFile(paths.original_zip) as zf:
        assert sorted(zf.namelist()) == ["pom.xml", "src/main/java/App.java"]
    s = chain.summary(out, run_dir)
    assert s["status"] == SUCCEEDED and s["files"]["state"] == "remediation/state.json"
    assert s["files"]["delivery_zip"] == "remediation/delivery/result.zip"
    json.dumps(s)


def test_uses_the_pods_local_repository_unchanged(tmp_path, ivas_env, monkeypatch):
    seen = {}

    def fake_default(cfg, journal=None):
        seen["repo"] = cfg.build.local_repo
        return one_fix_components()
    monkeypatch.setattr(graph_mod, "default_components", fake_default)
    cfg = make_cfg(after_migration=True)
    cfg.build = type("B", (), {"local_repo": "/m2/repo-slot2"})()
    run_dir, root = project(tmp_path)
    out, _ = go(run_dir, root, cfg=cfg)
    assert out.status == SUCCEEDED and seen["repo"] == "/m2/repo-slot2"


def test_awaiting_approval_is_saved_and_returned(tmp_path, ivas_env):
    plan = ScriptedPlanner([RoundPlan(round=1, source_tree_hash="", policy_hash="",
                                      proposals=[proposal(decision="pending", risks=["major"])])])
    run_dir, root = project(tmp_path)
    out, _ = go(run_dir, root, comps=one_fix_components(plan=plan))
    assert out.status == AWAITING_APPROVAL and chain.load_state(run_dir).status == AWAITING_APPROVAL


# ---------------------------------------------------------------------------- source ZIP rules
def test_source_zip_rules(tmp_path):
    root = tmp_path / "p"
    files = {
        "pom.xml": "<project/>", "mod/pom.xml": "<project/>", "mod/src/main/java/A.java": "a",
        "mod/target/classes/A.class": "x", "target/out.jar": "x",
        "src/main/java/com/x/target/Keep.java": "keep",      # a source package named target
        "docs/target/notes.txt": "keep",                       # no pom beside it: not build output
        ".git/HEAD": "ref", ".sbmig/work.json": "{}", "bin/pom.xml": "<project/>",
    }
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    os.symlink(root / "pom.xml", root / "link.xml")
    os.symlink(root / "mod", root / "linkdir")
    j = Journal()
    a, b = tmp_path / "a.zip", tmp_path / "b.zip"
    chain.source_zip(root, a, exclude_dirs=["bin"], journal=j)
    chain.source_zip(root, b, exclude_dirs=["bin"])
    assert a.read_bytes() == b.read_bytes()                    # deterministic
    with zipfile.ZipFile(a) as zf:
        names = zf.namelist()
        assert names == sorted(names)
        assert all(i.date_time == (1980, 1, 1, 0, 0, 0) for i in zf.infolist())
    assert names == ["docs/target/notes.txt", "mod/pom.xml", "mod/src/main/java/A.java", "pom.xml",
                     "src/main/java/com/x/target/Keep.java"]
    ev = [e for e in j.events if e["event"] == "remediation_symlink_skipped"]
    assert ev and ev[0]["paths"] == ["link.xml", "linkdir"]


# ---------------------------------------------------------------------------- failures never raise
def test_intake_rejection_is_a_failed_state(tmp_path, ivas_env):
    run_dir, root = project(tmp_path, {"README.txt": "no pom here"})
    out, j = go(run_dir, root, comps=one_fix_components())
    assert out.status == FAILED and out.reason == "no_pom"
    assert chain.load_state(run_dir).reason == "no_pom"
    assert "remediation_intake_rejected" in j.names() and j.names()[-1] == "remediation_end"


def test_exception_inside_the_graph_never_escapes(tmp_path, ivas_env, monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("graph exploded with https://user:pw@nexus.invalid/x")
    monkeypatch.setattr(graph_mod, "run_remediation", boom)
    run_dir, root = project(tmp_path)
    out, j = go(run_dir, root, comps=one_fix_components())
    assert out.status == FAILED and out.reason == "internal_error"
    err = [e for e in j.events if e["event"] == "remediation_error"]
    assert err and err[0]["type"] == "RuntimeError" and "user:pw" not in err[0]["message"]
    assert chain.load_state(run_dir).status == FAILED
    assert any("chained remediation failed" in r.getMessage() for r in caplog.records)


def test_exception_before_the_state_exists_never_escapes(tmp_path, ivas_env, monkeypatch):
    monkeypatch.setattr(chain, "source_zip", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    run_dir, root = project(tmp_path)
    out, j = go(run_dir, root, comps=one_fix_components())
    assert out.status == FAILED and "remediation_error" in j.names()


def test_existing_state_is_returned_untouched(tmp_path, ivas_env):
    run_dir, root = project(tmp_path)
    paths = chain.chain_paths(run_dir)
    st = RemediationState(id="run-1", status=STOPPED, next_node="baseline_scan")
    write_json_atomic(paths.state, st.to_dict())
    before = paths.state.read_bytes()
    comps = one_fix_components()
    out, j = go(run_dir, root, comps=comps)
    assert out.status == STOPPED and paths.state.read_bytes() == before
    assert j.names() == ["remediation_exists"] and comps.maven.package_calls == []


def test_leftover_dir_without_state_is_restarted(tmp_path, ivas_env):
    run_dir, root = project(tmp_path)
    paths = chain.chain_paths(run_dir)
    (paths.accepted / "half").mkdir(parents=True)
    out, _ = go(run_dir, root, comps=one_fix_components())
    assert out.status == SUCCEEDED


# ---------------------------------------------------------------------------- resume
def test_resume_rules(tmp_path, ivas_env):
    run_dir, root = project(tmp_path)
    with pytest.raises(StateConflict):
        chain.resume(run_dir, make_cfg(), journal=Journal(), stop=lambda: False, components=one_fix_components())
    stop = {"on": True}
    out, _ = go(run_dir, root, comps=one_fix_components(), stop=lambda: stop["on"])
    assert out.status == STOPPED
    st = chain.load_state(run_dir)
    st.stop_requested = True
    write_json_atomic(chain.chain_paths(run_dir).state, st.to_dict())
    j = Journal()
    out = chain.resume(run_dir, make_cfg(), journal=j, stop=lambda: False, components=one_fix_components())
    assert out.status == SUCCEEDED and not out.stop_requested
    assert j.names()[0] == "remediation_resume" and all(e["scope"] == "remediation" for e in j.events)
    with pytest.raises(StateConflict):                     # terminal now
        chain.resume(run_dir, make_cfg(), journal=Journal(), stop=lambda: False, components=one_fix_components())


def test_summary_of_a_skip():
    s = chain.summary(None, "/x", skipped={"event": "remediation_skipped", "reason": "not_ready", "missing": ["A"]})
    assert s == {"status": "skipped", "dir": "remediation", "reason": "not_ready", "missing": ["A"]}


# ---------------------------------------------------------------------------- time budget
class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


@pytest.fixture()
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(chain, "_monotonic", c)
    return c


def test_budget_stops_at_the_next_boundary_as_chain_budget_and_resume_continues(tmp_path, ivas_env, clock):
    comps = one_fix_components(maven=FakeMaven(on_package=lambda _p: setattr(clock, "t", 31 * 60.0)))
    run_dir, root = project(tmp_path)
    out, j = go(run_dir, root, cfg=make_cfg(after_migration=True, chain_budget_minutes=30), comps=comps)
    assert out.status == STOPPED and out.reason == chain.R_CHAIN_BUDGET and out.reasons == [chain.R_CHAIN_BUDGET]
    assert out.next_node == "baseline_scan" and not out.stop_requested
    assert "time budget of 30 minute(s)" in out.message
    saved = chain.load_state(run_dir)
    assert saved.reason == chain.R_CHAIN_BUDGET and saved.status == STOPPED
    assert j.names().count("remediation_budget") == 1
    assert next(e for e in j.events if e["event"] == "remediation_end")["reason"] == chain.R_CHAIN_BUDGET
    # a new pod resumes: the budget is measured again from this call
    clock.t = 1000 * 60.0
    out = chain.resume(run_dir, make_cfg(chain_budget_minutes=30), journal=Journal(), stop=lambda: False,
                       components=one_fix_components())
    assert out.status == SUCCEEDED, (out.reason, out.message)


def test_budget_during_a_scan_keeps_the_receipt_and_resume_polls_without_upload(tmp_path, ivas_env, clock):
    def in_flight(*, zip_path, zip_sha256, label, receipt, on_receipt):
        r = UploadReceipt(project_id="prj-1", analysis_id="anl-1", zip_sha256=zip_sha256, project_name="n")
        on_receipt(r)
        clock.t = 91 * 60.0                      # the budget runs out while sbmig polls IVAS
        return ScanResult(label=label, zip_sha256=zip_sha256, complete=False, receipt=r,
                          incomplete_reasons=["stop_requested"])
    from .fakes import FakeScanner
    scan = FakeScanner({"baseline": [in_flight]})
    run_dir, root = project(tmp_path)
    out, _ = go(run_dir, root, comps=one_fix_components(scan=scan))      # default budget: 90 minutes
    assert out.status == STOPPED and out.reason == chain.R_CHAIN_BUDGET
    assert out.pending_receipt["baseline"]["analysis_id"] == "anl-1" and "polled again" in out.message
    scan2 = FakeScanner({"baseline": [scan_result(finding())], "final-r1": [scan_result()]})
    out = chain.resume(run_dir, make_cfg(), journal=Journal(), stop=lambda: False,
                       components=one_fix_components(scan=scan2))
    assert out.status == SUCCEEDED
    assert scan2.calls[0].label == "baseline" and not scan2.calls[0].uploaded
    assert scan2.calls[0].receipt.analysis_id == "anl-1"


def test_a_real_stop_stays_stop_requested_even_when_the_budget_ran_out(tmp_path, ivas_env, clock):
    flag = {"on": False}

    def late(_p):
        clock.t = 999 * 60.0
        flag["on"] = True
    run_dir, root = project(tmp_path)
    out, _ = go(run_dir, root, comps=one_fix_components(maven=FakeMaven(on_package=late)), stop=lambda: flag["on"])
    assert out.status == STOPPED and out.reason == "stop_requested"


def test_zero_budget_means_no_limit(tmp_path, ivas_env, clock):
    comps = one_fix_components(maven=FakeMaven(on_package=lambda _p: setattr(clock, "t", 10 ** 9)))
    run_dir, root = project(tmp_path)
    out, j = go(run_dir, root, cfg=make_cfg(after_migration=True, chain_budget_minutes=0), comps=comps)
    assert out.status == SUCCEEDED and "remediation_budget" not in j.names()
