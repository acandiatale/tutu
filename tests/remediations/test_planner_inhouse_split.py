"""The planner on the split inventory: an item with `inhouse` is in-house for every one of its modules (the inventory
splits a library by module), the project part of the same library is planned as if the in-house modules did not
exist, and every residual carries the item's per-module sources and the IVAS spelling."""
from __future__ import annotations

import types

import pytest

from sbmig.core.config import ArtifactsConfig, RemediationConfig, RemediationPolicyConfig
from sbmig.remediations import graph
from sbmig.remediations import planner as P
from sbmig.remediations.models import (
    K_MANAGED_OVERRIDE,
    K_PARENT_VERSION,
    O_DIRECT_LITERAL,
    O_PARENT_MANAGED,
    O_PLUGIN,
    O_TRANSITIVE,
    Inventory,
    InventoryItem,
    ModuleSource,
    PomLocation,
    RemediationState,
    Residual,
)

INH = "com.example.inhouse:inhouse-core"
PARENT = "com.example.inhouse:base-parent"
ROOT = ("<project>\n  <modelVersion>4.0.0</modelVersion>\n  <groupId>example</groupId>\n  <artifactId>app</artifactId>\n"
        "  <version>1.0.0</version>\n  <packaging>pom</packaging>\n  <modules>\n    <module>app-domain</module>\n"
        "    <module>app-web</module>\n  </modules>\n</project>\n")
MODULE = ("<project>\n  <parent>\n    <groupId>example</groupId>\n    <artifactId>app</artifactId>\n"
          "    <version>1.0.0</version>\n  </parent>\n  <artifactId>{name}</artifactId>\n</project>\n")
PARENT_ROOT = ROOT.replace("  <groupId>example</groupId>\n  <artifactId>app</artifactId>\n",
                           "  <parent>\n    <groupId>com.example.inhouse</groupId>\n    <artifactId>base-parent</artifactId>\n"
                           "    <version>4.0.0</version>\n  </parent>\n  <groupId>example</groupId>\n"
                           "  <artifactId>app</artifactId>\n", 1)


class Src:
    def versions(self, g, a):
        return {"example:t": ["1.1"], PARENT: ["4.0.1"]}.get(f"{g}:{a}", [])

    def evidence(self, g, a, v):
        return "repository:nexus.invalid" if v in self.versions(g, a) else ""


def _cfg(pins=None, deny=None):
    c = types.SimpleNamespace(remediation=RemediationConfig(
        policy=RemediationPolicyConfig(pins=dict(pins or {}), deny=list(deny or []))))
    c.artifacts = ArtifactsConfig(parent={"group_id": "com.example.inhouse", "artifact_id": "base-parent"},
                                  mandatory=[{"group_id": "com.example.inhouse", "artifact_id": "inhouse-core"}])
    return c


def _project(tmp_path, root=ROOT):
    (tmp_path / "pom.xml").write_text(root)
    for m in ("app-domain", "app-web"):
        (tmp_path / m).mkdir()
        (tmp_path / m / "pom.xml").write_text(MODULE.format(name=m))
    return tmp_path


def _src(module, how=O_TRANSITIVE, via=(), inhouse=(), manager=""):
    return ModuleSource(module=module, how=how, via=list(via), inhouse=list(inhouse), manager=manager)


def _parts():
    """example:t 1.0: app-domain gets it through the in-house artifact, app-web through a project dependency"""
    inh = InventoryItem(ga="example:t", version="1.0", origin=O_TRANSITIVE, patched_versions=["1.1"],
                        finding_ids=["CVE-T"], modules=["app-domain"], via=[f"{INH}:4.0.0"], inhouse=[INH],
                        inhouse_modules=["app-domain"], reported_version="1.0",
                        sources=[_src("app-domain", via=[f"{INH}:4.0.0"], inhouse=[INH])])
    own = InventoryItem(ga="example:t", version="1.0", origin=O_TRANSITIVE, patched_versions=["1.1"],
                        finding_ids=["CVE-T"], modules=["app-web"], via=["example:web-lib:2.0"],
                        sources=[_src("app-web", via=["example:web-lib:2.0"])])
    return inh, own


def _plan(project, items, cfg=None, rejected=None, complete=True):
    return P.plan_round(project, Inventory(items=items, complete=complete), cfg or _cfg(), Src(), round=1,
                        source_tree_hash="t" * 64, rejected=rejected)


def test_inhouse_part_is_a_residual_and_the_project_part_is_planned(tmp_path):
    inh, own = _parts()
    plan = _plan(_project(tmp_path), [inh, own])
    (p,) = plan.proposals
    assert p.kind == K_MANAGED_OVERRIDE and p.modules == ["app-web"] and p.finding_ids == ["CVE-T"]
    (r,) = plan.residuals
    assert r.reason == "inhouse_artifact" and r.modules == ["app-domain"] and r.inhouse == [INH]
    assert f"brought in by the in-house artifact(s) {INH}" in r.detail
    assert [s.module for s in r.sources] == ["app-domain"] and r.sources[0].inhouse == [INH]
    # the project part is planned exactly as if the in-house module did not exist
    (alone,) = _plan(tmp_path, [own]).proposals
    assert alone.diff == p.diff and alone.proposal_hash == p.proposal_hash


def test_project_part_residual_carries_no_inhouse(tmp_path):
    inh, own = _parts()
    plan = _plan(_project(tmp_path), [inh, own], rejected={("example:t", "1.1")})
    rej = next(r for r in plan.residuals if r.reason == "rejected_proposals")
    assert rej.modules == ["app-web"] and rej.inhouse == [] and rej.via == ["example:web-lib:2.0"]
    assert [s.module for s in rej.sources] == ["app-web"]
    assert [r.reason for r in plan.residuals if r.inhouse] == ["inhouse_artifact"]


def test_inhouse_artifact_itself(tmp_path):
    item = InventoryItem(ga=INH, version="4.0.0", origin=O_DIRECT_LITERAL, finding_ids=["CVE-I"],
                         patched_versions=["4.0.1"], location=PomLocation("pom.xml", 5, "1.0.0"), modules=["app-web"],
                         inhouse=[INH], inhouse_modules=["app-web"], sources=[_src("app-web", how=O_DIRECT_LITERAL)])
    (r,) = _plan(_project(tmp_path), [item]).residuals
    assert r.reason == "inhouse_artifact" and f"{INH} is an in-house artifact" in r.detail
    assert r.sources == item.sources


def test_parent_managed_inhouse_item_without_pin_is_inhouse(tmp_path):
    item = InventoryItem(ga="example:t", version="1.0", origin=O_PARENT_MANAGED, patched_versions=["1.1"],
                         finding_ids=["CVE-T"], modules=["app-domain", "app-web"], manager=f"{PARENT}:4.0.0",
                         manager_location=PomLocation("pom.xml", 6, "4.0.0", element="parent"),
                         inhouse=[PARENT], inhouse_modules=["app-domain", "app-web"],
                         sources=[_src(m, how=O_PARENT_MANAGED, manager=f"{PARENT}:4.0.0", inhouse=[PARENT])
                                  for m in ("app-domain", "app-web")])
    proj = _project(tmp_path, PARENT_ROOT)
    plan = _plan(proj, [item])
    (r,) = plan.residuals
    assert not plan.proposals and r.reason == "inhouse_artifact" and r.inhouse == [PARENT]
    assert len(r.sources) == 2
    # the policy.pins exception: the server pins the company parent, so the parent-version proposal is made
    plan = _plan(proj, [item], cfg=_cfg(pins={PARENT: "4.0.1"}))
    (p,) = plan.proposals
    assert p.kind == K_PARENT_VERSION and p.ga == PARENT and p.to_version == "4.0.1" and not plan.residuals


def test_parent_pin_residual_is_not_inhouse(tmp_path):
    item = InventoryItem(ga="example:t", version="1.0", origin=O_PARENT_MANAGED, patched_versions=["1.1"],
                         finding_ids=["CVE-T"], modules=["app-web"], manager=f"{PARENT}:4.0.0",
                         manager_location=None, inhouse=[PARENT], inhouse_modules=["app-web"],
                         sources=[_src("app-web", how=O_PARENT_MANAGED, inhouse=[PARENT])])
    (r,) = _plan(_project(tmp_path, PARENT_ROOT), [item], cfg=_cfg(pins={PARENT: "4.0.1"})).residuals
    assert r.reason == "unknown_provenance" and r.inhouse == [] and r.sources == item.sources


@pytest.mark.parametrize("kind", ["manual", "denied", "no_candidate", "incomplete"])
def test_every_residual_kind_carries_sources_and_reported_version(tmp_path, kind):
    srcs = [_src("app-web", via=["example:web-lib:2.0"])]
    item = InventoryItem(ga="example:t", version="2.2.0.RELEASE", origin=O_TRANSITIVE, patched_versions=["9.9"],
                         finding_ids=["CVE-T"], modules=["app-web"], sources=srcs, reported_version="2.2.0.release")
    cfg = _cfg()
    if kind == "manual":
        item.origin = O_PLUGIN
    elif kind == "denied":
        cfg = _cfg(deny=["example:t"])
    plan = _plan(_project(tmp_path), [item], cfg=cfg, complete=kind != "incomplete")
    (r,) = plan.residuals
    assert r.sources == srcs and r.reported_version == "2.2.0.release" and r.version == "2.2.0.RELEASE"


def test_a_finding_in_both_parts_is_counted_once(tmp_path):
    """`fill_counts` counts findings from the scans; two residual rows (one per section) for one finding stay two
    rows in `residual` but one finding in `baseline_findings` / `final_findings` / `fixed`."""
    st = RemediationState(id="r", name="n", status="RUNNING")
    inh, own = _parts()
    plan = _plan(_project(tmp_path), [inh, own], rejected={("example:t", "1.1")})
    graph._merge_residuals(st, plan.residuals)
    assert sorted(r.reason for r in st.residuals) == ["inhouse_artifact", "rejected_proposals"]
    assert {fid for r in st.residuals for fid in r.finding_ids} == {"CVE-T"}


def test_merge_residuals_is_case_insensitive_and_unions_sources():
    st = RemediationState(id="r", name="n", status="RUNNING")
    a = Residual(ga="example:t", version="2.2.0.RELEASE", origin="transitive", reason="manual_review",
                 modules=["m1"], sources=[_src("m1")])
    b = Residual(ga="example:t", version="2.2.0.release", origin="transitive", reason="manual_review",
                 modules=["m2"], sources=[_src("m2")], reported_version="2.2.0.release")
    graph._merge_residuals(st, [a])
    graph._merge_residuals(st, [b])
    (r,) = st.residuals
    assert r.modules == ["m1", "m2"] and [s.module for s in r.sources] == ["m1", "m2"]
    assert r.version == "2.2.0.RELEASE" and r.reported_version == "2.2.0.release"
