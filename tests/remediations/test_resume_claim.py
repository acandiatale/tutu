"""One driver per run folder (v2.1z-ap, finding R1 of the three-pods review): the run claim `remediation/claim.json`.

Ported from the reviewer's `par3/remediation/test_two_resumes.py`: two `chain.resume` calls on one run folder used to
run the whole flow twice (two IVAS uploads, the last state.json wins). Also R3 (why the proposals came back) and M1
(the Maven repository of a resumed chained run). Fakes only; every wait is bounded, nothing writes in a loop."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from sbmig.remediations import chain
from sbmig.remediations import graph as graph_mod
from sbmig.remediations.models import (
    AWAITING_APPROVAL,
    D_APPROVED,
    FAILED,
    STOPPED,
    SUCCEEDED,
    RoundPlan,
    StateConflict,
    read_json,
    write_json_atomic,
)

from .fakes import (
    FakeMaven,
    FakeScanner,
    Recorder,
    ScriptedPlanner,
    finding,
    make_cfg,
    make_components,
    make_run,
    proposal,
    scan_result,
)
from .test_chain import Journal, go, ivas_env, one_fix_components, project  # noqa: F401 - fixture

WAIT = 15.0


def snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def stopped_run(tmp_path):
    run_dir, root = project(tmp_path)
    out, _ = go(run_dir, root, comps=one_fix_components(), stop=lambda: True)
    assert out.status == STOPPED
    return run_dir


def claim_file(run_dir) -> Path:
    return chain.claim_path(chain.chain_paths(run_dir).root)


class Hold:
    """FakeMaven.on_package that blocks the first package call until released (bounded)."""

    def __init__(self):
        self.inside = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def __call__(self, _project):
        self.calls += 1
        if self.calls == 1:
            self.inside.set()
            assert self.release.wait(WAIT), "the test never released the first resume"


# ---------------------------------------------------------------------------- two resumes on one run folder
def test_a_second_resume_refuses_and_touches_nothing_while_the_first_drives(tmp_path, ivas_env, monkeypatch):
    run_dir = stopped_run(tmp_path)
    as_read = chain.load_state(run_dir)              # what both pods read before either of them saved RUNNING
    reference = tmp_path / "reference"
    shutil.copytree(run_dir, reference)              # the same folder, driven by the first resume alone below
    hold = Hold()
    first = one_fix_components(maven=FakeMaven(on_package=hold))
    second = one_fix_components()
    result: dict = {}

    def drive():
        try:
            result["state"] = chain.resume(run_dir, make_cfg(), journal=Journal(), stop=lambda: False, components=first)
        except BaseException as e:  # noqa: BLE001 - reported by the assertion below
            result["error"] = repr(e)

    t = threading.Thread(target=drive, daemon=True)
    t.start()
    try:
        assert hold.inside.wait(WAIT)
        info = json.loads(claim_file(run_dir).read_text())
        assert info["pid"] == os.getpid() and info["purpose"] == "resume" and info["token"] and info["host"]
        before = snapshot(chain.chain_paths(run_dir).root)
        # the second pod read the state together with the first one (the race), so its own status check passes and
        # only the claim stands in its way; the first pod is blocked inside its build meanwhile
        with monkeypatch.context() as m:
            m.setattr(chain, "load_state", lambda _rd: as_read)
            with pytest.raises(StateConflict) as ei:
                chain.resume(run_dir, make_cfg(), journal=Journal(), stop=lambda: False, components=second)
        assert str(ei.value).startswith(chain.CLAIM_HELD + ": pid ")
        assert snapshot(chain.chain_paths(run_dir).root) == before        # byte-identical: nothing touched
        assert second.scan.uploads == 0 and second.maven.package_calls == []
        assert chain.claim_info(chain.chain_paths(run_dir).root)["token"] == info["token"]
    finally:
        hold.release.set()
        t.join(WAIT)
    assert not t.is_alive() and "error" not in result, result
    assert result["state"].status == SUCCEEDED and not claim_file(run_dir).exists()
    # the first resume ends exactly as it ends alone
    alone = chain.resume(reference, make_cfg(), journal=Journal(), stop=lambda: False, components=one_fix_components())
    assert (alone.status, alone.reason) == (result["state"].status, result["state"].reason)
    got, ref = snapshot(chain.chain_paths(run_dir).root), snapshot(chain.chain_paths(reference).root)
    assert sorted(got) == sorted(ref)
    assert got["delivery/result.zip"] == ref["delivery/result.zip"]
    assert got["rounds/1/plan.json"] == ref["rounds/1/plan.json"]


def test_the_in_process_service_catches_the_refusal_without_overwriting_the_state(tmp_path, ivas_env):
    run_dir = stopped_run(tmp_path)
    holder = chain.RunClaim(chain.chain_paths(run_dir).root, purpose="resume", attempt=3).acquire()
    try:
        state = chain.chain_paths(run_dir).state.read_bytes()
        with pytest.raises(StateConflict) as ei:
            chain.resume(run_dir, make_cfg(), journal=Journal(), stop=lambda: False, components=one_fix_components())
        assert "(attempt 3)" in str(ei.value)
        assert chain.chain_paths(run_dir).state.read_bytes() == state
    finally:
        holder.release()
    assert not claim_file(run_dir).exists()


# ---------------------------------------------------------------------------- stale claims
def _foreign_claim(run_dir, *, age: float, host: str = "another-pod", pid: int = 4242) -> Path:
    path = claim_file(run_dir)
    path.write_text(json.dumps({"pid": pid, "host": host, "attempt": 1, "started": "2026-11-17T00:00:00Z",
                                "purpose": "resume", "token": "old-token"}), encoding="utf-8")
    t = time.time() - age
    os.utime(path, (t, t))
    return path


def test_a_fresh_claim_of_another_pod_refuses(tmp_path, ivas_env):
    run_dir = stopped_run(tmp_path)
    path = _foreign_claim(run_dir, age=5)
    before = snapshot(chain.chain_paths(run_dir).root)
    with pytest.raises(StateConflict) as ei:
        chain.resume(run_dir, make_cfg(), journal=Journal(), stop=lambda: False, components=one_fix_components())
    assert "another-pod" in str(ei.value) and "attempt 1" in str(ei.value)
    assert snapshot(chain.chain_paths(run_dir).root) == before and path.exists()


def test_a_stale_claim_is_taken_over_and_journaled(tmp_path, ivas_env):
    run_dir = stopped_run(tmp_path)
    _foreign_claim(run_dir, age=chain.CLAIM_STALE_SECONDS + 60)
    j = Journal()
    out = chain.resume(run_dir, make_cfg(), journal=j, stop=lambda: False, components=one_fix_components())
    assert out.status == SUCCEEDED and not claim_file(run_dir).exists()
    taken = [e for e in j.events if e["event"] == chain.CLAIM_EVENT_TAKEN]
    assert len(taken) == 1 and taken[0]["holder"]["host"] == "another-pod" and "token" not in taken[0]["holder"]
    assert taken[0]["scope"] == "remediation"
    assert not list(chain.chain_paths(run_dir).root.glob("claim.json.stale.*"))
    assert not list(chain.chain_paths(run_dir).root.glob(".claim.probe.*"))


def test_a_claim_of_a_dead_pid_on_this_host_is_stale_at_once(tmp_path, ivas_env):
    import socket
    run_dir = stopped_run(tmp_path)
    child = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True,
                           timeout=60)
    _foreign_claim(run_dir, age=0, host=socket.gethostname(), pid=int(child.stdout.strip()))
    assert chain.claim_info(chain.chain_paths(run_dir).root) is None
    out = chain.resume(run_dir, make_cfg(), journal=Journal(), stop=lambda: False, components=one_fix_components())
    assert out.status == SUCCEEDED and not claim_file(run_dir).exists()


def test_keep_alive_refreshes_the_claim_and_release_removes_only_our_own(tmp_path, monkeypatch):
    monkeypatch.setattr(chain, "CLAIM_REFRESH_SECONDS", 0.05)
    root = tmp_path / "remediation"
    c = chain.RunClaim(root, purpose="test").acquire()
    try:
        old = time.time() - 3600
        os.utime(c.path, (old, old))
        deadline = time.monotonic() + WAIT
        while os.stat(c.path).st_mtime < old + 1800 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert os.stat(c.path).st_mtime > old + 1800, "the keep-alive never touched the claim"
        assert chain.claim_info(root)["purpose"] == "test"
        monkeypatch.setattr(chain, "CLAIM_STALE_SECONDS", -1)    # every claim is stale now: readers see none
        assert chain.claim_info(root) is None
    finally:
        c.release()
    assert not c.path.exists()
    # a claim that is not ours any more (taken over meanwhile) is left alone by release
    c2 = chain.RunClaim(root, purpose="test").acquire()
    c2.path.write_text(json.dumps({"token": "someone-else"}), encoding="utf-8")
    c2.release()
    assert c2.path.exists()


# ---------------------------------------------------------------------------- the claim is gone after every end state
class Seen:
    def __init__(self, run_dir):
        self.path = claim_file(run_dir)
        self.held = []

    def __call__(self, _project):
        self.held.append(self.path.exists())


@pytest.mark.parametrize("end", ["succeeded", "awaiting", "stopped", "failed", "crash"])
def test_the_claim_exists_while_driving_and_is_gone_after_every_end(tmp_path, ivas_env, end):
    run_dir, root = project(tmp_path)
    seen = Seen(run_dir)
    kw = {}
    if end == "awaiting":
        kw["plan"] = ScriptedPlanner([RoundPlan(round=1, source_tree_hash="", policy_hash="",
                                                proposals=[proposal(decision="pending", risks=["major"])])])
    maven = FakeMaven(on_package=seen, outcomes=[False] if end == "failed" else [])
    if end == "crash":
        def boom(_project):
            seen(_project)
            raise RuntimeError("a bug outside the nodes")
        maven = FakeMaven(on_package=boom)
    out, _ = go(run_dir, root, comps=one_fix_components(maven=maven, **kw),
                stop=(lambda: True) if end == "stopped" else None)
    expected = {"succeeded": SUCCEEDED, "awaiting": AWAITING_APPROVAL, "stopped": STOPPED, "failed": FAILED,
                "crash": FAILED}[end]
    assert out.status == expected
    assert not claim_file(run_dir).exists()
    if end != "stopped":
        assert seen.held and all(seen.held)


def test_run_only_takes_and_releases_the_claim(tmp_path, ivas_env):
    run_dir, root = project(tmp_path)
    seen = Seen(run_dir)
    out = chain.run_only(run_dir, root, make_cfg(), run_id="run-1", journal=Journal(), stop=lambda: False,
                         components=one_fix_components(maven=FakeMaven(on_package=seen)))
    assert out.status == SUCCEEDED and seen.held and all(seen.held) and not claim_file(run_dir).exists()


def test_a_first_run_finding_a_fresh_claim_creates_nothing(tmp_path, ivas_env):
    run_dir, root = project(tmp_path)
    claim_file(run_dir).parent.mkdir(parents=True)
    _foreign_claim(run_dir, age=1)
    out, _ = go(run_dir, root, comps=one_fix_components())
    assert out is None
    assert sorted(p.name for p in chain.chain_paths(run_dir).root.iterdir()) == ["claim.json"]
    out = chain.run_only(run_dir, root, make_cfg(), run_id="run-1", journal=Journal(), stop=lambda: False,
                         components=one_fix_components())
    assert out.status == FAILED and out.message.startswith(chain.CLAIM_HELD)
    assert sorted(p.name for p in chain.chain_paths(run_dir).root.iterdir()) == ["claim.json"]


# ---------------------------------------------------------------------------- R3: why the proposals came back
class PolicyPlanner:
    """A plan function whose current policy hash can change between two pods."""

    def __init__(self, inner: ScriptedPlanner):
        self.inner = inner

    def __call__(self, *a, **kw):
        return self.inner(*a, **kw)

    def policy_hash(self, _cfg):
        return self.inner.policy_hash


def _pending_plan():
    return RoundPlan(round=1, source_tree_hash="", policy_hash="", proposals=[proposal(decision="pending",
                                                                                         risks=["major"])])


def test_a_changed_policy_says_why_the_proposals_came_back(tmp_path):
    state, paths = make_run(tmp_path)
    inner = ScriptedPlanner([_pending_plan(), _pending_plan()])
    comps = make_components(plan=PolicyPlanner(inner), scan=FakeScanner({"baseline": [scan_result(finding())]}))
    rec = Recorder()
    out = graph_mod.run_remediation(state, paths, make_cfg(), comps, save=rec.save, journal=rec.journal, stop=rec.stop)
    assert out.status == AWAITING_APPROVAL and out.message == "1 proposal(s) of round 1 wait for approval"
    data = read_json(paths.plan(1))
    data["proposals"][0]["decision"] = D_APPROVED
    write_json_atomic(paths.plan(1), data)
    inner.policy_hash = "policy-hash-2"                  # the ConfigMap changed between the build pod and the resume pod
    out = graph_mod.run_remediation(out, paths, make_cfg(), comps, save=rec.save, journal=rec.journal, stop=rec.stop)
    assert out.status == AWAITING_APPROVAL and out.policy_hash == "policy-hash-2"
    assert out.message == f"{graph_mod.POLICY_CHANGED_NOTE} | 1 proposal(s) of round 1 wait for approval"
    assert (paths.round_dir(1) / "plan.stale.json").is_file()
    assert [e for e in rec.events if e["event"] == "approvals_dropped"]
    from sbmig.remediations import i18n
    assert i18n.ko_text(out.message).startswith("서버 설정이 바뀌어 앞서 승인한 내용은 적용하지 않고 계획을 다시 세웠습니다.")


def test_an_unchanged_policy_keeps_the_plain_message(tmp_path):
    state, paths = make_run(tmp_path)
    inner = ScriptedPlanner([_pending_plan()])
    comps = make_components(plan=PolicyPlanner(inner), scan=FakeScanner({"baseline": [scan_result(finding())]}))
    rec = Recorder()
    out = graph_mod.run_remediation(state, paths, make_cfg(), comps, save=rec.save, journal=rec.journal, stop=rec.stop)
    out.status = STOPPED                                  # a later resume of the same, undecided plan
    out = graph_mod.run_remediation(out, paths, make_cfg(), comps, save=rec.save, journal=rec.journal, stop=rec.stop)
    assert out.status == AWAITING_APPROVAL and graph_mod.POLICY_CHANGED_NOTE not in out.message
