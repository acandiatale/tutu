"""No proposal waits for approval (the approval step was removed): a run with a root-pom override, a module-pom override
and a major direct change goes through to its terminal state without ever saving AWAITING_APPROVAL, in the graph and in
the chained entry; a run an earlier version left inside `plan` with decided proposals and its old policy hash is planned
again after the upgrade and every proposal is applied (a person's rejection no longer exists). Real planner / apply,
fake Maven / IVAS."""
from __future__ import annotations

import pytest

from sbmig.remediations import apply as apply_mod
from sbmig.remediations import chain, graph
from sbmig.remediations import planner as P
from sbmig.remediations.models import (
    D_AUTO,
    INTERRUPTED,
    K_DIRECT_VERSION,
    LEGACY_AWAITING_APPROVAL,
    K_MANAGED_OVERRIDE,
    O_DIRECT_LITERAL,
    SUCCEEDED,
    Finding,
    Inventory,
    InventoryItem,
    ModuleResolution,
    ModuleSource,
    PomLocation,
    ResolvedDep,
    ResolvedModel,
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
    make_cfg,
    make_components,
    make_run,
    scan_result,
)
from .test_module_overrides import module_pom

ENV = {"IVAS_BASE_URL": "https://ivas.invalid", "IVAS_USER_ID": "tester", "IVAS_PASSWORD": "not-a-real-pw"}

ROOT = ("<project>\n  <modelVersion>4.0.0</modelVersion>\n  <groupId>example</groupId>\n  <artifactId>app</artifactId>\n"
        "  <version>1.0.0</version>\n  <packaging>pom</packaging>\n  <modules>\n    <module>core</module>\n"
        "    <module>other</module>\n  </modules>\n  <dependencies>\n    <dependency>\n"
        "      <groupId>example</groupId>\n      <artifactId>lib</artifactId>\n      <version>1.2.3</version>\n"
        "    </dependency>\n  </dependencies>\n</project>\n")
LIB_LINE = ROOT.splitlines().index("      <version>1.2.3</version>") + 1
FILES = {"pom.xml": ROOT,
         "core/pom.xml": module_pom("core", ("example", "app", "1.0.0")),            # inherits the root
         "other/pom.xml": module_pom("other")}                                       # inherits a company parent
VERSIONS = {"example:t": ["1.0", "1.1"], "example:lib": ["1.2.3", "2.0.0"]}


def _items():
    t = InventoryItem(ga="example:t", version="1.0", origin="transitive", patched_versions=["1.1"],
                      via=["example:x:2.0"], finding_ids=["CVE-T"], modules=["core", "other"],
                      sources=[ModuleSource(module=m, how="transitive", via=["example:x:2.0"]) for m in ("core", "other")])
    lib = InventoryItem(ga="example:lib", version="1.2.3", origin=O_DIRECT_LITERAL, patched_versions=["2.0.0"],
                        finding_ids=["CVE-L"], modules=["core"], location=PomLocation("pom.xml", LIB_LINE, "1.2.3"))
    return [t, lib]


def _findings(t="1.0", lib="1.2.3"):
    out = []
    if t == "1.0":
        out.append(Finding(id="CVE-T", kind="library", identifier="example:t", version="1.0", patched_version="1.1"))
    if lib == "1.2.3":
        out.append(Finding(id="CVE-L", kind="library", identifier="example:lib", version="1.2.3",
                           patched_version="2.0.0"))
    return out


def _model(t_core="1.0", t_other="1.0", lib="1.2.3"):
    def t(v):
        return ResolvedDep(group_id="example", artifact_id="t", version=v, path=["example:x:2.0"])
    return ResolvedModel(modules=[
        ModuleResolution(dir="core", group_id="example", artifact_id="core", version="1.0.0",
                         dependencies=[t(t_core), ResolvedDep(group_id="example", artifact_id="lib", version=lib,
                                                              direct=True)]),
        ModuleResolution(dir="other", group_id="example", artifact_id="other", version="1.0.0", dependencies=[t(t_other)])])


def _components(models, scans):
    return make_components(scan=FakeScanner(scans), inventory=ScriptedInventory([Inventory(items=_items())]),
                           maven=FakeMaven(models=models), versions=FakeVersions(VERSIONS),
                           plan=P.plan_round, apply=apply_mod)


def _assert_never_waited(rec_saves, events):
    assert LEGACY_AWAITING_APPROVAL not in [s["status"] for s in rec_saves]
    assert LEGACY_AWAITING_APPROVAL not in [e.get("status") for e in events]
    assert not [e for e in events if e.get("event") == "node_end" and e.get("next") is None
                and e.get("status") == LEGACY_AWAITING_APPROVAL]


def _check_plan(plan: RoundPlan) -> None:
    kinds = sorted((p.ga, p.kind, p.edits[0].pom) for p in plan.proposals)
    assert kinds == [("example:lib", K_DIRECT_VERSION, "pom.xml"), ("example:t", K_MANAGED_OVERRIDE, "other/pom.xml"),
                     ("example:t", K_MANAGED_OVERRIDE, "pom.xml")]
    for p in plan.proposals:
        assert p.decision == D_AUTO, p.id
    assert next(p for p in plan.proposals if p.ga == "example:lib").risks == ["major"]


def test_default_configuration_runs_to_the_end_without_waiting(tmp_path):
    cfg = make_cfg(max_rounds=1)
    state, paths = make_run(tmp_path, FILES, cfg=cfg, policy_hash=P.policy_hash(cfg))
    comps = _components([_model(), _model(t_core="1.1", t_other="1.1", lib="2.0.0")],
                        {"baseline": [scan_result(*_findings())], "final-r1": [scan_result()]})
    rec = Recorder()
    out = graph.run_remediation(state, paths, cfg, comps, save=rec.save, journal=rec.journal, stop=rec.stop)
    assert out.status == SUCCEEDED, (out.reason, out.message)
    _assert_never_waited(rec.saves, rec.events)
    plan = RoundPlan.from_dict(read_json(paths.plan(1)))
    _check_plan(plan)
    assert plan.to_apply() == plan.proposals
    assert sorted(out.rounds[0].applied) == sorted(p.id for p in plan.proposals) and out.rounds[0].committed
    root = (paths.accepted / "pom.xml").read_text()
    assert "<version>2.0.0</version>" in root and "<artifactId>t</artifactId>" in root
    assert "<artifactId>t</artifactId>" in (paths.accepted / "other/pom.xml").read_text()
    assert rec.saves[-1]["status"] == SUCCEEDED


@pytest.fixture()
def ivas_env(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)


def test_chained_entry_never_returns_a_waiting_status(tmp_path, ivas_env):
    root = tmp_path / "run" / "project"
    for rel, text in FILES.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    comps = _components([_model(), _model(t_core="1.1", t_other="1.1", lib="2.0.0")],
                        {"baseline": [scan_result(*_findings())], "final-r1": [scan_result()]})
    events: list[dict] = []
    out = chain.run_after_migration(tmp_path / "run", root, make_cfg(after_migration=True, max_rounds=1),
                                    run_id="run-1", journal=events.append, stop=lambda: False, components=comps)
    assert out is not None and out.status == SUCCEEDED, (out.reason, out.message)
    assert LEGACY_AWAITING_APPROVAL not in [e.get("status") for e in events]
    paths = chain.chain_paths(tmp_path / "run")
    assert read_json(paths.state)["status"] == SUCCEEDED
    plan = RoundPlan.from_dict(read_json(paths.plan(1)))
    _check_plan(plan)
    assert sorted(out.rounds[0].applied) == sorted(p.id for p in plan.proposals)


def test_a_run_left_inside_plan_by_an_earlier_version_is_planned_again_and_applied(tmp_path, monkeypatch):
    cfg = make_cfg(max_rounds=1)
    state, paths = make_run(tmp_path, FILES, cfg=cfg, policy_hash="an-earlier-policy-hash")
    comps = _components([_model(), _model(t_core="1.1", t_other="1.1", lib="2.0.0")],
                        {"baseline": [scan_result(*_findings())], "final-r1": [scan_result()]})
    rec = Recorder()
    real = graph._NODE_FNS["plan"]

    def plan_then_die(h):                       # the pod went down right after plan.json was written
        real(h)
        rec.stop_flag = True
        return "apply"

    monkeypatch.setitem(graph._NODE_FNS, "plan", plan_then_die)
    out = graph.run_remediation(state, paths, cfg, comps, save=rec.save, journal=rec.journal, stop=rec.stop)
    monkeypatch.undo()
    # plan.json and state.json as the version with the approval step left them
    data = read_json(paths.plan(1))
    for p in data["proposals"]:
        p.update(policy_hash="an-earlier-policy-hash", requires_approval=True,
                 decision="rejected" if p["ga"] == "example:lib" else "approved")
    data["policy_hash"] = "an-earlier-policy-hash"
    write_json_atomic(paths.plan(1), data)
    out.status, out.next_node, out.policy_hash = INTERRUPTED, "plan", "an-earlier-policy-hash"

    rec2 = Recorder()
    out = graph.run_remediation(out, paths, cfg, comps, save=rec2.save, journal=rec2.journal, stop=rec2.stop)
    names = [e.get("event") for e in rec2.events]
    assert "policy_changed" in names and "plan_stale" in names and "approvals_dropped" not in names
    assert out.status == SUCCEEDED, (out.status, out.reason, out.message)
    _assert_never_waited(rec2.saves, rec2.events)
    assert out.policy_hash == P.policy_hash(cfg)
    plan = RoundPlan.from_dict(read_json(paths.plan(1)))
    assert plan.policy_hash == P.policy_hash(cfg)
    _check_plan(plan)
    assert sorted(out.rounds[0].applied) == sorted(p.id for p in plan.proposals) and out.rounds[0].committed
    assert "<version>2.0.0</version>" in (paths.accepted / "pom.xml").read_text()          # the old rejection is gone
    stale = read_json(paths.round_dir(1) / "plan.stale.json")
    assert sorted(p["decision"] for p in stale["proposals"]) == ["approved", "approved", "rejected"]
