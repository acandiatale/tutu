"""The drop-and-rebuild step survives a kill at any point (v2.1z-ap, finding R2 of the three-pods review).

Ported from the reviewer's `par3/remediation/test_drop_crash.py`: a pod killed while `_drop_ineffective` removed and
copied the candidate again left `next_node=build` on disk with the dropped proposal still in `applied` and its residual
only in memory; the resume pod built the half-copied tree and froze it as `result.zip`. Now the decision is saved with
`next_node=apply` before the candidate is touched, and a completeness marker guards build / freeze. Fakes only."""
from __future__ import annotations

import zipfile

import pytest

from sbmig.remediations import apply as apply_mod
from sbmig.remediations import graph
from sbmig.remediations import planner as P
from sbmig.remediations.models import (
    AWAITING_APPROVAL,
    D_APPROVED,
    FAILED,
    INTERRUPTED,
    BuildOutcome,
    Inventory,
    RemediationState,
    RoundPlan,
    read_json,
    write_json_atomic,
)

from .fakes import (
    FakeMaven,
    FakeScanner,
    FakeVersions,
    Recorder,
    ScriptedInventory,
    ScriptedPlanner,
    finding,
    make_cfg,
    make_components,
    make_run,
    proposal,
    scan_result,
)
from .test_module_overrides import DM_SECTION, THREE, _finding, _three, company_files, item

POMS = ["app-back/pom.xml", "app-boot/pom.xml", "app-front/pom.xml", "pom.xml"]
RESIDUAL = "did not change the resolved version"


class Killed(BaseException):
    """SIGKILL stand-in: not an Exception, so no node turns it into FAILED."""


class TreeMaven(FakeMaven):
    """Like real Maven, a tree without all its poms does not build."""

    def package(self, project, log_path, stop=None, targets=None):
        whole = all((project / p).is_file() for p in POMS)
        out = super().package(project, log_path, stop, targets)
        return out if whole else BuildOutcome(ok=False, exit_code=1, log=log_path.name, summary="[ERROR] missing pom")


def _components(models, on_package=None):
    return make_components(scan=FakeScanner({"baseline": [scan_result(_finding())], "final-r1": [scan_result(_finding())]}),
                           inventory=ScriptedInventory([Inventory(items=[item(modules=list(THREE))])]),
                           maven=TreeMaven(models=models, on_package=on_package),
                           versions=FakeVersions({"example:t": ["1.0", "1.1"]}), plan=P.plan_round, apply=apply_mod)


def _dropped_id(paths) -> str:
    pl = RoundPlan.from_dict(read_json(paths.plan(1)))
    return next(x.id for x in pl.proposals if x.edits[0].pom == "app-boot/pom.xml")


@pytest.mark.parametrize("where", ["one_file", "copied", "applied", "rebuild", "plan_write"])
def test_a_kill_inside_the_drop_path_resumes_into_a_consistent_round(tmp_path, monkeypatch, where):
    c = make_cfg(max_rounds=1)
    state, paths = make_run(tmp_path, company_files(back_body=DM_SECTION))
    calls = {"pkg": 0, "copy": 0, "apply": 0, "plan": 0}

    def on_package(_p):
        calls["pkg"] += 1
        if where == "rebuild" and calls["pkg"] == 3:          # baseline, candidate, then the rebuilt candidate
            raise Killed()

    comps = _components([_three(), _three(boot="1.0", back="1.1", front="1.1")], on_package)
    real_copy, real_apply = comps.intake.copy_tree, comps.apply.apply_proposals

    def copy_tree(src, dst, files):
        calls["copy"] += 1
        if calls["copy"] == 3:                                # baseline tree, candidate, then the drop's copy
            if where == "one_file":
                real_copy(src, dst, sorted(files)[:1])
                raise Killed()
            real_copy(src, dst, files)
            if where == "copied":
                raise Killed()
            return None
        return real_copy(src, dst, files)

    def apply_proposals(project, props):
        calls["apply"] += 1
        out = real_apply(project, props)
        if where == "applied" and calls["apply"] == 2:        # the apply node, then the drop's apply
            raise Killed()
        return out

    real_write = graph.write_json_atomic

    def write(path, data):
        if path == paths.plan(1):
            calls["plan"] += 1
            if where == "plan_write" and calls["plan"] == 2:  # the plan node wrote it first
                raise Killed()
        return real_write(path, data)

    comps.intake.copy_tree = copy_tree
    comps.apply = type("A", (), {"apply_proposals": staticmethod(apply_proposals),
                                 **{k: getattr(apply_mod, k) for k in ("verify_text_only", "verify_resolution",
                                                                      "split_ineffective")}})
    monkeypatch.setattr(graph, "write_json_atomic", write)
    rec = Recorder()
    out = graph.run_remediation(state, paths, c, comps, save=rec.save, journal=rec.journal, stop=rec.stop)
    assert out.status == AWAITING_APPROVAL
    data = read_json(paths.plan(1))
    for x in data["proposals"]:
        x["decision"] = D_APPROVED
    write_json_atomic(paths.plan(1), data)
    dropped = _dropped_id(paths)
    with pytest.raises(Killed):
        graph.run_remediation(out, paths, c, comps, save=rec.save, journal=rec.journal, stop=rec.stop)
    monkeypatch.setattr(graph, "write_json_atomic", real_write)

    # what state.json holds after the kill: the decision is there, and the next node rebuilds the candidate
    disk = RemediationState.from_dict(rec.saves[-1])
    assert disk.next_node == "apply", disk.next_node
    assert dropped not in disk.rounds[0].applied
    assert [r for r in disk.residuals if RESIDUAL in r.detail]
    # the marker vouches only for a finished rebuild (killed during its Maven build), never for a half-made tree
    assert (paths.round_dir(1) / graph.CANDIDATE_READY).exists() == (where == "rebuild")

    # a NEW pod resumes (the web pod showed it INTERRUPTED)
    disk.status = INTERRUPTED
    after = _three(boot="1.0", back="1.1", front="1.1")
    comps2 = _components([after, after, after])
    rec2 = Recorder()
    out2 = graph.run_remediation(disk, paths, c, comps2, save=rec2.save, journal=rec2.journal, stop=rec2.stop)
    assert out2.status != FAILED, (out2.reasons, out2.message)
    assert out2.delivery is not None and out2.delivery.build_ok and out2.rounds[0].committed
    assert dropped not in out2.rounds[0].applied and len(out2.rounds[0].applied) == 2
    assert len([r for r in out2.residuals if RESIDUAL in r.detail]) == 1
    assert {x.id: x.decision for x in RoundPlan.from_dict(read_json(paths.plan(1))).proposals}[dropped] == "ineffective"
    with zipfile.ZipFile(paths.delivery_zip) as zf:
        assert sorted(zf.namelist()) == POMS                  # never a truncated result.zip
    assert "<artifactId>t</artifactId>" in (paths.accepted / "app-front/pom.xml").read_text()
    assert "<artifactId>t</artifactId>" not in (paths.accepted / "app-boot/pom.xml").read_text()


# ---------------------------------------------------------------------------- the completeness marker
def _one_fix(maven):
    scan = FakeScanner({"baseline": [scan_result(finding())], "final-r1": [scan_result()]})
    plan = ScriptedPlanner([RoundPlan(round=1, source_tree_hash="", policy_hash="", proposals=[proposal()])])
    return make_components(scan=scan, plan=plan, maven=maven)


def test_a_failed_candidate_without_its_marker_is_never_frozen(tmp_path):
    state, paths = make_run(tmp_path)
    marker = paths.round_dir(1) / graph.CANDIDATE_READY

    def lose_marker(project):
        if project == paths.candidate(1):
            assert marker.is_file()                           # written after copy + apply
            marker.unlink()                                   # as if the tree had been touched since

    comps = _one_fix(FakeMaven(outcomes=[True, False], on_package=lose_marker))
    rec = Recorder()
    out = graph.run_remediation(state, paths, make_cfg(), comps, save=rec.save, journal=rec.journal, stop=rec.stop)
    assert out.status == FAILED and out.reason == "candidate_build_failed"
    assert out.message.startswith(graph.CANDIDATE_NOT_VOUCHED.format(r=1))
    assert out.delivery is None and not paths.delivery_zip.exists()
    assert "candidate_not_vouched" in [e["event"] for e in rec.events]


def test_a_build_without_the_marker_makes_the_candidate_again(tmp_path):
    state, paths = make_run(tmp_path)
    calls = {"n": 0}

    def kill_in_build(_p):
        calls["n"] += 1
        if calls["n"] == 2:
            raise Killed()

    comps = _one_fix(FakeMaven(on_package=kill_in_build))
    rec = Recorder()
    with pytest.raises(Killed):
        graph.run_remediation(state, paths, make_cfg(), comps, save=rec.save, journal=rec.journal, stop=rec.stop)
    disk = RemediationState.from_dict(rec.saves[-1])
    assert disk.next_node == "build"
    # an older image left no marker, and the candidate lost a file
    (paths.round_dir(1) / graph.CANDIDATE_READY).unlink()
    (paths.candidate(1) / "src/main/java/App.java").unlink()
    disk.status = INTERRUPTED
    comps.maven = FakeMaven()
    rec2 = Recorder()
    out = graph.run_remediation(disk, paths, make_cfg(), comps, save=rec2.save, journal=rec2.journal, stop=rec2.stop)
    assert out.status == "SUCCEEDED", (out.reasons, out.message)
    assert "candidate_rebuilt" in [e["event"] for e in rec2.events]
    with zipfile.ZipFile(paths.delivery_zip) as zf:
        assert "src/main/java/App.java" in zf.namelist()
