"""A whole fake remediation (plan → apply → build → verify → rescan → evaluate) over the REAL planner and apply
modules, where IVAS spells the version in lower case before and after (`2.2.0.release`) and Maven resolves
`2.2.0.RELEASE`: the library is planned, verified and evaluated as one library."""
from __future__ import annotations

from sbmig.remediations import apply as apply_mod
from sbmig.remediations import graph
from sbmig.remediations import planner as planner_mod
from sbmig.remediations.models import (
    AWAITING_APPROVAL,
    D_APPROVED,
    INCOMPLETE,
    K_MANAGED_OVERRIDE,
    O_DIRECT_LITERAL,
    O_TRANSITIVE,
    SUCCEEDED,
    Finding,
    Inventory,
    InventoryItem,
    ModuleResolution,
    ModuleSource,
    PomLocation,
    ResolvedDep,
    ResolvedModel,
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

POM = """<project>
  <modelVersion>4.0.0</modelVersion>
  <groupId>example</groupId>
  <artifactId>app</artifactId>
  <version>0.1</version>
  <dependencies>
    <dependency>
      <groupId>example</groupId>
      <artifactId>lib</artifactId>
      <version>2.2.0.RELEASE</version>
    </dependency>
  </dependencies>
</project>
"""
L_LIB = 10


def _finding(cve="CVE-1", ident="example:lib", version="2.2.0.release", patched="2.2.1.release"):
    return Finding(id=cve, kind="library", identifier=ident, version=version, patched_version=patched)


def _model(*deps):
    return ResolvedModel(modules=[ModuleResolution(dir=".", group_id="example", artifact_id="app", version="0.1",
                                                   dependencies=list(deps))])


def _lib(v, direct=True, c="", path=()):
    return ResolvedDep(group_id="example", artifact_id="lib", version=v, direct=direct, classifier=c, path=list(path))


def _direct_item():
    return InventoryItem(ga="example:lib", version="2.2.0.RELEASE", reported_version="2.2.0.release",
                         origin=O_DIRECT_LITERAL, finding_ids=["CVE-1"], patched_versions=["2.2.1.release"],
                         modules=["."], location=PomLocation("pom.xml", L_LIB, "2.2.0.RELEASE"),
                         sources=[ModuleSource(module=".", how=O_DIRECT_LITERAL)])


def _run(state, paths, comps, cfg):
    rec = Recorder()
    return graph.run_remediation(state, paths, cfg, comps, save=rec.save, journal=rec.journal, stop=rec.stop)


def _components(scan, inventory, models):
    return make_components(scan=scan, inventory=ScriptedInventory(inventory), maven=FakeMaven(models=models),
                           versions=FakeVersions({"example:lib": ["2.2.0.RELEASE", "2.2.1.RELEASE"]}),
                           plan=planner_mod.plan_round, apply=apply_mod)


def test_lower_case_ivas_version_is_planned_verified_and_counted_fixed(tmp_path):
    cfg = make_cfg()
    state, paths = make_run(tmp_path, {"pom.xml": POM})
    comps = _components(FakeScanner({"baseline": [scan_result(_finding())], "final-r1": [scan_result()]}),
                        [Inventory(items=[_direct_item()])],
                        [_model(_lib("2.2.0.RELEASE")), _model(_lib("2.2.1.RELEASE"))])
    out = _run(state, paths, comps, cfg)
    plan = read_json(paths.plan(1))
    (p,) = plan["proposals"]
    assert (p["from_version"], p["to_version"]) == ("2.2.0.RELEASE", "2.2.1.RELEASE")
    assert "<version>2.2.1.RELEASE</version>" in (paths.accepted / "pom.xml").read_text()
    assert out.status == SUCCEEDED, (out.reason, out.message)
    assert out.counts["baseline_findings"] == 1 and out.counts["final_findings"] == 0 and out.counts["fixed"] == 1


def test_a_finding_still_reported_in_another_case_is_neither_fixed_nor_new(tmp_path):
    cfg = make_cfg(max_rounds=1)
    state, paths = make_run(tmp_path, {"pom.xml": POM})
    other = _finding("CVE-2", "example:other", "1.0", "")
    still = _finding("CVE-2", "EXAMPLE:Other", "1.0", "")              # the same finding, IVAS spelled it otherwise
    comps = _components(FakeScanner({"baseline": [scan_result(_finding(), other)], "final-r1": [scan_result(still)]}),
                        [Inventory(items=[_direct_item()])],
                        [_model(_lib("2.2.0.RELEASE")), _model(_lib("2.2.1.RELEASE"))])
    out = _run(state, paths, comps, cfg)
    assert out.status == INCOMPLETE and out.reason == "residual_findings"
    assert out.counts["fixed"] == 1                    # CVE-1 only: CVE-2 is still there, whatever its case
    assert out.counts["final_findings"] == 1


DM_POM = """<project>
  <modelVersion>4.0.0</modelVersion>
  <groupId>example</groupId>
  <artifactId>app</artifactId>
  <version>0.1</version>
  <dependencies>
    <dependency>
      <groupId>example</groupId>
      <artifactId>web-lib</artifactId>
      <version>3.0</version>
    </dependency>
  </dependencies>
</project>
"""


def test_classifier_override_flow_with_lower_case_ivas(tmp_path):
    cfg = make_cfg(always_approve=["managed_override"])         # approval switched on: the flow stops once
    state, paths = make_run(tmp_path, {"pom.xml": DM_POM})
    item = InventoryItem(ga="example:lib", version="2.2.0.RELEASE", reported_version="2.2.0.release",
                         origin=O_TRANSITIVE, finding_ids=["CVE-1"], patched_versions=["2.2.1.release"], modules=["."],
                         via=["example:web-lib:3.0"], variants=["jar/jakarta"],
                         sources=[ModuleSource(module=".", how=O_TRANSITIVE, via=["example:web-lib:3.0"],
                                               variant="jar/jakarta")])
    web = ResolvedDep(group_id="example", artifact_id="web-lib", version="3.0", direct=True)
    before = _model(web, _lib("2.2.0.RELEASE", direct=False, c="jakarta", path=["example:web-lib:3.0"]))
    after = _model(web, _lib("2.2.1.RELEASE", direct=False, c="jakarta", path=["example:web-lib:3.0"]))
    comps = _components(FakeScanner({"baseline": [scan_result(_finding())], "final-r1": [scan_result()]}),
                        [Inventory(items=[item])], [before, after])
    out = _run(state, paths, comps, cfg)
    assert out.status == AWAITING_APPROVAL
    data = read_json(paths.plan(1))
    (p,) = data["proposals"]
    assert p["kind"] == K_MANAGED_OVERRIDE and p["edits"][0]["override"]["classifier"] == "jakarta"
    p["decision"] = D_APPROVED
    write_json_atomic(paths.plan(1), data)
    out = _run(out, paths, comps, cfg)
    pom = (paths.accepted / "pom.xml").read_text()
    assert "<classifier>jakarta</classifier>" in pom and "<version>2.2.1.RELEASE</version>" in pom
    assert out.status == SUCCEEDED, (out.reason, out.message)
    assert out.counts["fixed"] == 1
