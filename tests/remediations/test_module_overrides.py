"""Override entries in a module's own pom (module_overrides): the company layout, where the modules inherit company
parent poms and not the project root, gets one proposal per (module pom, management key), applied like every proposal;
apply / verify per pom; the switched-off plan is the plan made before this increment."""
from __future__ import annotations

import types

import pytest

from sbmig.core.config import RemediationConfig, RemediationPolicyConfig
from sbmig.remediations import apply as A
from sbmig.remediations import apply as apply_mod
from sbmig.remediations import graph
from sbmig.remediations import planner as P
from sbmig.remediations.models import (
    D_AUTO,
    FAILED,
    INCOMPLETE,
    K_MANAGED_OVERRIDE,
    K_OWN_MANAGEMENT_VERSION,
    O_TRANSITIVE,
    Finding,
    Inventory,
    InventoryItem,
    ModuleResolution,
    ModuleSource,
    PackagedLib,
    ResolvedDep,
    ResolvedModel,
    RoundPlan,
    canonical_json,
    read_json,
    sha256_text,
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

MODULES = ("app-boot", "app-back", "app-front")
ROOT = ("<project>\n  <modelVersion>4.0.0</modelVersion>\n  <groupId>example</groupId>\n  <artifactId>app</artifactId>\n"
        "  <version>1.0.0</version>\n  <packaging>pom</packaging>\n  <modules>\n"
        + "".join(f"    <module>{m}</module>\n" for m in MODULES) + "  </modules>\n</project>\n")
DM_SECTION = ("  <dependencyManagement>\n    <dependencies>\n      <dependency>\n        <groupId>example</groupId>\n"
              "        <artifactId>bom</artifactId>\n        <version>1.0</version>\n        <type>pom</type>\n"
              "        <scope>import</scope>\n      </dependency>\n    </dependencies>\n  </dependencyManagement>\n")
ENTRY_T = ("      <dependency>\n        <groupId>example</groupId>\n        <artifactId>t</artifactId>\n"
           "        <version>1.0</version>\n      </dependency>\n")


def module_pom(name, parent=("com.example.inhouse", "comp-jar", "4.0.0"), body="", head=""):
    g, a, v = parent
    rel = "    <relativePath/>\n" if parent[1] != "app" else ""
    return (f"{head}<project>\n  <modelVersion>4.0.0</modelVersion>\n  <parent>\n    <groupId>{g}</groupId>\n"
            f"    <artifactId>{a}</artifactId>\n    <version>{v}</version>\n{rel}  </parent>\n"
            f"  <groupId>example</groupId>\n  <artifactId>{name}</artifactId>\n  <version>1.0.0</version>\n"
            f"{body}</project>\n")


def company_files(back_body="", boot_body="", boot_head="", front_parent=None):
    """root aggregator + app-boot (boot-jar) / app-back / app-front (comp-jar), none inheriting the root"""
    return {"pom.xml": ROOT,
            "app-boot/pom.xml": module_pom("app-boot", ("com.example.inhouse", "boot-jar", "4.0.0"), boot_body, boot_head),
            "app-back/pom.xml": module_pom("app-back", body=back_body),
            "app-front/pom.xml": module_pom("app-front", front_parent or ("com.example.inhouse", "comp-jar", "4.0.0"))}


def write(base, files):
    for name, text in files.items():
        f = base / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    return base


def src(m, variant=""):
    return ModuleSource(module=m, how=O_TRANSITIVE, via=[f"example:{m}-lib:2.0"], variant=variant)


def item(modules=("app-boot", "app-back"), variants=(), sources=None, version="1.0", fid="CVE-T"):
    return InventoryItem(ga="example:t", version=version, origin=O_TRANSITIVE, patched_versions=["1.1"],
                         via=["example:lib:2.0"], finding_ids=[fid], modules=list(modules), variants=list(variants),
                         sources=list(sources if sources is not None else [src(m) for m in modules]))


class Src:
    def versions(self, g, a):
        return ["1.0", "1.1"]

    def evidence(self, g, a, v):
        return "repository:nexus.invalid"


def cfg(**kw):
    return types.SimpleNamespace(remediation=RemediationConfig(policy=RemediationPolicyConfig(), **kw))


def plan(project, items, c=None, rejected=None):
    return P.plan_round(project, Inventory(items=items), c or cfg(), Src(), round=1, source_tree_hash="t" * 64,
                        rejected=rejected)


def by_pom(pl):
    return {p.edits[0].pom: p for p in pl.proposals}


# ------------------------------------------------------------------------------------------------- planning
def test_company_layout_one_proposal_per_module_pom(tmp_path):
    proj = write(tmp_path, company_files(back_body=DM_SECTION))
    pl = plan(proj, [item()])
    assert not pl.residuals
    props = by_pom(pl)
    assert sorted(props) == ["app-back/pom.xml", "app-boot/pom.xml"]
    for mod, p in (("app-back", props["app-back/pom.xml"]), ("app-boot", props["app-boot/pom.xml"])):
        assert p.kind == K_MANAGED_OVERRIDE and p.decision == D_AUTO
        assert p.risks == ["managed_override"] and p.modules == [mod] and p.also_modules == []
        assert [s.module for s in p.sources] == [mod]
        assert f"adds a <dependencyManagement> entry example:t:1.1 to {mod}/pom.xml" in p.note
        assert p.diff.startswith(f"--- a/{mod}/pom.xml")
    # app-back has a section: the entry goes inside it; app-boot has none: the section is created
    back, boot = props["app-back/pom.xml"].edits[0], props["app-boot/pom.xml"].edits[0]
    assert back.anchor == "</dependencies>" and not back.new.lstrip().startswith("<dependencyManagement>")
    assert boot.anchor == "</project>" and boot.new.lstrip().startswith("<dependencyManagement>")


def test_module_entry_is_applied_like_every_proposal(tmp_path):
    proj = write(tmp_path, company_files())
    pl = plan(proj, [item(modules=["app-back"])])
    (p,) = pl.proposals
    assert p.decision == D_AUTO and pl.to_apply() == [p]
    assert "always needs approval" not in p.note


def test_existing_entry_in_the_module_pom_uses_its_version_text(tmp_path):
    body = DM_SECTION.replace("    </dependencies>", ENTRY_T + "    </dependencies>")
    proj = write(tmp_path, company_files(back_body=body))
    pl = plan(proj, [item()])
    props = by_pom(pl)
    own = props["app-back/pom.xml"]
    assert own.kind == K_OWN_MANAGEMENT_VERSION and own.edits[0].op == "replace" and own.modules == ["app-back"]
    assert "uses the existing <dependencyManagement> entry of app-back/pom.xml" in own.note
    assert props["app-boot/pom.xml"].kind == K_MANAGED_OVERRIDE


def test_declared_version_in_the_module_is_a_residual_for_that_module_only(tmp_path):
    dep = ("  <dependencies>\n    <dependency>\n      <groupId>example</groupId>\n      <artifactId>t</artifactId>\n"
           "      <version>1.0</version>\n    </dependency>\n  </dependencies>\n")
    proj = write(tmp_path, company_files(back_body=dep))
    pl = plan(proj, [item()])
    assert list(by_pom(pl)) == ["app-boot/pom.xml"]
    (r,) = pl.residuals
    assert r.modules == ["app-back"] and "is declared with a version in app-back/pom.xml (1.0)" in r.detail
    assert [s.module for s in r.sources] == ["app-back"]


def test_mixed_project_root_entry_and_one_module_entry(tmp_path):
    files = company_files(front_parent=("example", "app", "1.0.0"))
    proj = write(tmp_path, files)
    pl = plan(proj, [item(modules=["app-back", "app-front"])])
    props = by_pom(pl)
    assert sorted(props) == ["app-back/pom.xml", "pom.xml"]
    assert props["pom.xml"].modules == ["app-front"] and props["app-back/pom.xml"].modules == ["app-back"]
    assert "to the root pom.xml" in props["pom.xml"].note


def test_descendant_inheriting_an_outside_module_is_governed_by_its_pom(tmp_path):
    files = company_files()
    files["app-back/pom.xml"] = files["app-back/pom.xml"].replace("<version>1.0.0</version>\n</project>",
                                                                  "<version>1.0.0</version>\n  <packaging>pom</packaging>\n"
                                                                  "  <modules>\n    <module>sub</module>\n  </modules>\n"
                                                                  "</project>")
    files["app-back/sub/pom.xml"] = module_pom("sub", ("example", "app-back", "1.0.0")).replace(
        "<version>1.0.0</version>\n  </parent>", "<version>1.0.0</version>\n    <relativePath>..</relativePath>\n  </parent>")
    proj = write(tmp_path, files)
    pl = plan(proj, [item(modules=["app-back", "app-back/sub"])])
    (p,) = pl.proposals
    assert p.edits[0].pom == "app-back/pom.xml" and p.modules == ["app-back", "app-back/sub"]


def test_classifier_variant_in_a_module_pom(tmp_path):
    proj = write(tmp_path, company_files())
    srcs = [src("app-back", "jar/jakarta"), src("app-boot")]
    pl = plan(proj, [item(variants=["jar/jakarta"], sources=srcs)])
    got = sorted((p.edits[0].pom, A.override_variant(p), tuple(p.modules)) for p in pl.proposals)
    assert got == [("app-back/pom.xml", "jar/jakarta", ("app-back",)), ("app-boot/pom.xml", "", ("app-boot",))]
    cls = next(p for p in pl.proposals if A.override_variant(p))
    assert "<classifier>jakarta</classifier>" in cls.edits[0].new


def test_a_pom_with_a_dtd_cannot_take_an_entry_the_other_module_still_does(tmp_path):
    proj = write(tmp_path, company_files(boot_head='<?xml version="1.0"?>\n<!DOCTYPE project>\n'))
    pl = plan(proj, [item()])
    assert list(by_pom(pl)) == ["app-back/pom.xml"]
    (r,) = pl.residuals
    assert r.reason == "manual_review" and r.modules == ["app-boot"]
    assert r.detail == "cannot place an override entry safely: app-boot/pom.xml declares a DTD / entities; not edited"


def test_two_items_of_one_key_in_one_module_pom_are_one_proposal(tmp_path):
    proj = write(tmp_path, company_files())
    pl = plan(proj, [item(modules=["app-back"]), item(modules=["app-back"], version="1.0.5", fid="CVE-2")])
    (p,) = pl.proposals
    assert p.edits[0].pom == "app-back/pom.xml" and p.finding_ids == ["CVE-T", "CVE-2"]


# ------------------------------------------------------------------------------------------------- switched off
# Captured from plan_round at HEAD e4ba9fd (before module poms were a target) for this project and item, as
# sha256(canonical_json(plan)) with the policy hashes left out (the policy hash takes `module_overrides: false`).
HEAD_OFF_PLAN = "0f84df2c227e38416ad97e28e042b0d804c19a255d32150c9f54c630a3ac867e"
# the same digest and the proposal hashes for a project whose modules all inherit the root (plain jar + classifier
# item of test_override_variants), default configuration. Re-captured when the default stopped waiting for approval:
# the earlier digest (8dbb4b82…) differed only in `requires_approval` / `decision`, the approval sentence in the
# notes and the plan note about waiting; the proposal hashes below did not move. Re-captured again when the approval
# step was removed: the field `requires_approval` left the proposals; with `requires_approval: false` put back the
# plan hashes to the previous digest 09b2109b… exactly.
HEAD_ROOT_PLAN = "14bd4ef566a81e53ff6533c30dd8628c01d804594ce85597482e3a030b26e993"
HEAD_ROOT_HASHES = ["1286564a1e2c739beec26853d2c913096cb4988936017c319088cdfdf228ded2",
                    "1109bb227d3f9bd2d59adc0f7b5e16eaf19db58ae25eb2aced1495f3cf476e9d"]


def _plan_digest(pl: RoundPlan) -> str:
    data = pl.to_dict()
    data.pop("policy_hash", None)
    for p in data["proposals"]:
        p.pop("policy_hash", None)
        p.pop("sources", None)
    return sha256_text(canonical_json(data))


def test_switched_off_gives_todays_residual_and_plan(tmp_path):
    proj = write(tmp_path, company_files(back_body=DM_SECTION))
    pl = plan(proj, [item()], c=cfg(module_overrides=False))
    assert not pl.proposals
    (r,) = pl.residuals
    assert r.detail == ("module(s) app-back, app-boot do not inherit from the root pom.xml: an override entry there "
                        "would not reach them")
    assert _plan_digest(pl) == HEAD_OFF_PLAN


def test_root_plan_is_unchanged_by_default(tmp_path):
    from . import test_override_variants as TV
    pl = P.plan_round(TV._mm(tmp_path), Inventory(items=[TV._both()]), TV._cfg(), TV.Src(), round=1,
                      source_tree_hash="t" * 64)
    assert _plan_digest(pl) == HEAD_ROOT_PLAN
    assert [p.proposal_hash for p in pl.proposals] == HEAD_ROOT_HASHES
    assert [[s.module for s in p.sources] for p in pl.proposals] == [p.modules for p in pl.proposals]


def test_policy_hash_only_changes_when_switched_off():
    base = P.policy_hash(cfg())
    assert base == P.policy_hash(cfg(module_overrides=True))
    assert base != P.policy_hash(cfg(module_overrides=False))
    legacy = cfg()
    del legacy.remediation.__dict__["module_overrides"]          # a config object from before the field
    assert P.policy_hash(legacy) == base


# ------------------------------------------------------------------------------------------------- apply / verify
def test_apply_one_entry_then_both_and_reapply(tmp_path):
    orig = write(tmp_path / "orig", company_files(back_body=DM_SECTION))
    props = by_pom(plan(orig, [item()]))
    back = props["app-back/pom.xml"]
    cand = write(tmp_path / "cand", company_files(back_body=DM_SECTION))
    res = A.apply_proposals(cand, [back])
    assert res.applied == [back.id] and res.changed_files == ["app-back/pom.xml"]
    paths = sorted(company_files())
    assert A.verify_text_only(orig, cand, [back], paths) == []
    assert (cand / "app-boot/pom.xml").read_text() == (orig / "app-boot/pom.xml").read_text()
    # both on a fresh copy: each pom gets its entry; idempotent bytes; never inserted twice
    both = list(props.values())
    one = write(tmp_path / "one", company_files(back_body=DM_SECTION))
    two = write(tmp_path / "two", company_files(back_body=DM_SECTION))
    A.apply_proposals(one, both)
    A.apply_proposals(two, both)
    for rel in paths:
        assert (one / rel).read_text() == (two / rel).read_text()
    assert A.verify_text_only(orig, one, both, paths) == []
    again = A.apply_proposals(one, both)
    assert again.applied == [] and set(again.skipped) == {p.id for p in both}


def _model(**versions):
    return ResolvedModel(modules=[ModuleResolution(dir=m, group_id="example", artifact_id=m, version="1.0.0",
                                                   dependencies=[ResolvedDep(group_id="example", artifact_id="t",
                                                                             version=v, path=["example:lib:2.0"])])
                                  for m, v in versions.items()])


def test_verify_resolution_per_governed_module(tmp_path):
    proj = write(tmp_path, company_files())
    props = list(plan(proj, [item(modules=list(MODULES))]).proposals)
    assert len(props) == 3
    before = _model(**{m: "1.0" for m in MODULES})
    # (a) every module entry moved its module
    assert A.verify_resolution(props, before, _model(**{m: "1.1" for m in MODULES}), []) == []
    # (b) app-front's parent declares the dependency with a version: its entry did not change anything
    after = _model(**{"app-boot": "1.1", "app-back": "1.1", "app-front": "1.0"})
    lines = A.verify_resolution(props, before, after, [])
    front = next(p for p in props if p.modules == ["app-front"])
    assert [x for x in lines if not x.startswith("info:")] == [
        f"example:t still resolves at 1.0 ({front.id})",
        f"example:t in module app-front resolves at 1.0, planned 1.1 ({front.id})"]
    # the two effective proposals each see app-front (not theirs) left at 1.0: information only
    assert sorted(x.split("(")[-1] for x in lines if x.startswith("info:")) == sorted(
        f"{p.id}); left for the next round" for p in props if p is not front)
    stale = PackagedLib(artifact="app-front/target/app-front.war", entry="WEB-INF/lib/t-1.0.jar", file_name="t-1.0.jar",
                        ga="example:t", version="1.0")
    extra = A.verify_resolution(props, before, after, [stale])
    assert any(x.startswith("app-front/target/app-front.war still packages") and front.id in x for x in extra)
    assert any(x.startswith("info:") and "not governed by" in x for x in extra)


# ------------------------------------------------------------------------------------------------- the whole flow
def _finding(version="1.0"):
    return Finding(id="CVE-T", kind="library", identifier="example:t", version=version, patched_version="1.1")


def _components(models, scans):
    return make_components(scan=FakeScanner(scans), inventory=ScriptedInventory([Inventory(items=[item()])]),
                           maven=FakeMaven(models=models), versions=FakeVersions({"example:t": ["1.0", "1.1"]}),
                           plan=P.plan_round, apply=apply_mod)


def _run(state, paths, comps, c):
    rec = Recorder()
    return graph.run_remediation(state, paths, c, comps, save=rec.save, journal=rec.journal, stop=rec.stop)


# ------------------------------------------------------------------------------------------------- ineffective entries
THREE = ("app-boot", "app-back", "app-front")


def _flow(tmp_path, models, *, scans=None, items=None, files=None, max_rounds=1, maven=None):
    """One run (every proposal applied); returns (state, paths, comps, rec)"""
    c = make_cfg(max_rounds=max_rounds)
    state, paths = make_run(tmp_path, files or company_files(back_body=DM_SECTION))
    inv = [Inventory(items=x) for x in (items or [[item(modules=list(THREE))]])]
    comps = make_components(scan=FakeScanner(scans or {"baseline": [scan_result(_finding())]}),
                            inventory=ScriptedInventory(inv), maven=maven or FakeMaven(models=models),
                            versions=FakeVersions({"example:t": ["1.0", "1.1"], "example:d": ["1.0", "1.1"]}),
                            plan=P.plan_round, apply=apply_mod)
    rec = Recorder()
    out = graph.run_remediation(state, paths, c, comps, save=rec.save, journal=rec.journal, stop=rec.stop)
    return out, paths, comps, rec


def _three(boot="1.0", back="1.0", front="1.0"):
    return _model(**{"app-boot": boot, "app-back": back, "app-front": front})


def test_one_ineffective_entry_is_dropped_and_the_rest_commits(tmp_path):
    orig = dict(company_files(back_body=DM_SECTION))
    models = [_three(), _three(boot="1.0", back="1.1", front="1.1"), _three(boot="1.0", back="1.1", front="1.1")]
    out, paths, comps, rec = _flow(tmp_path, models, scans={"baseline": [scan_result(_finding())],
                                                            "final-r1": [scan_result(_finding())]})
    assert len(comps.maven.package_calls) == 3                          # baseline, candidate, candidate without it
    assert out.rounds[0].committed and out.status == INCOMPLETE, (out.reason, out.message)
    assert "resolution_mismatch" not in out.reasons and "manual_review" in out.reasons
    pl = RoundPlan.from_dict(read_json(paths.plan(1)))
    dropped = [x for x in pl.proposals if x.decision == graph.D_INEFFECTIVE]
    assert [x.edits[0].pom for x in dropped] == ["app-boot/pom.xml"]
    assert sorted(out.rounds[0].applied) == sorted(x.id for x in pl.proposals if x.decision == D_AUTO)
    assert out.counts["proposals_applied"] == 2
    # the entry that had created the section is gone: app-boot's pom is the original byte for byte
    assert (paths.accepted / "app-boot/pom.xml").read_text() == orig["app-boot/pom.xml"]
    # the committed tree equals the two remaining proposals applied alone to the original
    fresh = write(tmp_path / "fresh", orig)
    A.apply_proposals(fresh, [x for x in pl.proposals if x.decision == D_AUTO])
    for rel in orig:
        assert (paths.accepted / rel).read_text() == (fresh / rel).read_text(), rel
    (r,) = [x for x in out.residuals if "did not change the resolved version" in x.detail]
    assert r.reason == "manual_review" and r.modules == ["app-boot"] and [s.module for s in r.sources] == ["app-boot"]
    assert r.finding_ids == ["CVE-T"] and r.detail == (
        "the override entry in app-boot/pom.xml did not change the resolved version of example:t (1.0 stays): a "
        "dependency declared with a version, here or in a parent, wins over <dependencyManagement>; the entry was "
        "removed again")
    ev = [e for e in rec.events if e.get("event") == "override_ineffective"]
    assert [(e["pom"], e["ga"], e["modules"]) for e in ev] == [("app-boot/pom.xml", "example:t", ["app-boot"])]


def test_every_entry_ineffective_commits_nothing_and_does_not_fail(tmp_path):
    orig = dict(company_files(back_body=DM_SECTION))
    models = [_three(), _three()]
    out, paths, comps, _rec = _flow(tmp_path, models)
    assert len(comps.maven.package_calls) == 2                          # no rebuild: nothing left
    assert out.status == INCOMPLETE and out.status != FAILED, (out.reason, out.message)
    assert not out.rounds[0].committed and out.rounds[0].applied == []
    assert out.delivery is not None and out.delivery.sha256 == out.intake.zip_sha256      # the original bytes
    for rel, text in orig.items():
        assert (paths.accepted / rel).read_text() == text
    (r,) = [x for x in out.residuals if "did not change the resolved version" in x.detail]
    assert r.modules == sorted(THREE) and r.detail == (
        "the override entries in app-back/pom.xml, app-boot/pom.xml, app-front/pom.xml did not change the resolved "
        "version of example:t (1.0 stays): a dependency declared with a version, here or in a parent, wins over "
        "<dependencyManagement>; the entries were removed again")


def test_a_version_text_mismatch_in_the_same_round_keeps_todays_failure(tmp_path):
    dep = ("  <dependencies>\n    <dependency>\n      <groupId>example</groupId>\n      <artifactId>d</artifactId>\n"
           "      <version>1.0</version>\n    </dependency>\n  </dependencies>\n")
    files = company_files(back_body=DM_SECTION + dep)
    line = files["app-back/pom.xml"].splitlines().index("      <version>1.0</version>") + 1
    from sbmig.remediations.models import O_DIRECT_LITERAL, PomLocation
    direct = InventoryItem(ga="example:d", version="1.0", origin=O_DIRECT_LITERAL, patched_versions=["1.1"],
                           finding_ids=["CVE-D"], modules=["app-back"],
                           location=PomLocation("app-back/pom.xml", line, "1.0"))

    def model(t_boot, d):
        m = _three(boot=t_boot, back="1.1", front="1.1")
        back = next(x for x in m.modules if x.dir == "app-back")
        back.dependencies.append(ResolvedDep(group_id="example", artifact_id="d", version=d, direct=True))
        return m
    out, _paths, comps, _rec = _flow(tmp_path, [model("1.0", "1.0"), model("1.0", "1.0")], files=files,
                                     items=[[item(modules=list(THREE)), direct]],
                                     scans={"baseline": [scan_result(_finding(), Finding(
                                         id="CVE-D", kind="library", identifier="example:d", version="1.0",
                                         patched_version="1.1"))]})
    assert out.status == FAILED and out.reason == "resolution_mismatch"
    assert len(comps.maven.package_calls) == 2 and not out.rounds[0].committed


def test_a_second_verification_problem_fails(tmp_path):
    models = [_three(), _three(boot="1.0", back="1.1", front="1.1"), _three(boot="1.0", back="1.0", front="1.1")]
    out, _paths, comps, _rec = _flow(tmp_path, models)
    assert len(comps.maven.package_calls) == 3
    assert out.status == FAILED and out.reason == "resolution_mismatch" and not out.rounds[0].committed
    assert "after removing the ineffective override entr(ies)" in out.message


def test_no_time_for_the_rebuild_fails_and_says_so(tmp_path):
    rec_box = {}

    class StopAfterVerify(FakeMaven):
        def packaged(self, project, targets=None):
            if rec_box.get("armed"):
                rec_box["rec"].stop_flag = True
            return super().packaged(project, targets)

    maven = StopAfterVerify(models=[_three(), _three(boot="1.0", back="1.1", front="1.1")])
    c = make_cfg(max_rounds=1)
    state, paths = make_run(tmp_path, company_files(back_body=DM_SECTION))
    comps = make_components(scan=FakeScanner({"baseline": [scan_result(_finding())]}),
                            inventory=ScriptedInventory([Inventory(items=[item(modules=list(THREE))])]), maven=maven,
                            versions=FakeVersions({"example:t": ["1.0", "1.1"]}), plan=P.plan_round, apply=apply_mod)
    rec = Recorder()
    rec_box["rec"] = rec
    rec_box["armed"] = True                      # the stop arrives with the first packaged-jar listing (the candidate)
    out = graph.run_remediation(state, paths, c, comps, save=rec.save, journal=rec.journal, stop=rec.stop)
    assert out.status == FAILED and out.reason == "resolution_mismatch"
    assert "were not removed and rebuilt: a stop was requested or the time budget is used up" in out.message
    assert len(maven.package_calls) == 2


def test_the_dropped_entry_is_not_proposed_again_in_the_run(tmp_path):
    models = [_three(), _three(boot="1.0", back="1.1", front="1.1"), _three(boot="1.0", back="1.1", front="1.1"),
              _three(boot="1.0", back="1.1", front="1.1")]                                   # round 2 before
    out, paths, _comps, _rec = _flow(
        tmp_path, models, max_rounds=2, items=[[item(modules=list(THREE))], [item(modules=["app-boot"])]],
        scans={"baseline": [scan_result(_finding())], "final-r1": [scan_result(_finding())]})
    assert out.round == 2, (out.status, out.reason, out.message)
    pl2 = RoundPlan.from_dict(read_json(paths.plan(2)))
    assert pl2.proposals == []
    (r,) = pl2.residuals
    assert r.detail == ("the override entry for example:t → 1.1 in app-boot/pom.xml had no effect in an earlier "
                        "round of this run; not proposed again") and r.modules == ["app-boot"]


def test_split_ineffective_only_takes_inserted_entries():
    props = list(_props_for_split())
    entry, text = props
    lines = [f"example:t still resolves at 1.0 ({entry.id})",
             f"example:t in module app-boot resolves at 1.0, planned 1.1 ({entry.id})",
             f"app-boot/target/app.war still packages WEB-INF/lib/t-1.0.jar (example:t:1.0, {entry.id})",
             f"info: something ({text.id})"]
    assert A.split_ineffective(props, lines) == ([entry], [])
    # a version-text proposal that did not move: not droppable
    bad = [f"example:d still resolves at 1.0 ({text.id})"]
    assert A.split_ineffective(props, lines + bad) == ([entry], bad)
    # an entry that moved the version elsewhere than planned had an effect: not droppable
    moved = [f"example:t in module app-boot resolves at 1.2, planned 1.1 ({entry.id})"]
    assert A.split_ineffective(props, moved) == ([], moved)
    # a line without a proposal id
    other = ["resolution after the change is incomplete: x"]
    assert A.split_ineffective(props, lines[:1] + other) == ([entry], other)


def _props_for_split():
    from sbmig.remediations.models import E_INSERT, Proposal, TextEdit
    yield Proposal(id="p-1-1", round=1, ga="example:t", from_version="1.0", to_version="1.1", kind=K_MANAGED_OVERRIDE,
                   modules=["app-boot"], edits=[TextEdit(pom="app-boot/pom.xml", line=1, old="", new="", op=E_INSERT,
                                                         anchor="x", override={"groupId": "example",
                                                                               "artifactId": "t", "version": "1.1"})])
    yield Proposal(id="p-1-2", round=1, ga="example:d", from_version="1.0", to_version="1.1", kind="direct_version",
                   modules=["app-back"], edits=[TextEdit(pom="app-back/pom.xml", line=3, old="1.0", new="1.1")])


def test_info_line_for_an_inserted_entry():
    props = [p for p in _props_for_split()][:1]
    before = _model(**{"app-boot": "1.0", "app-front": "1.0"})
    after = _model(**{"app-boot": "1.1", "app-front": "1.0"})
    assert A.verify_resolution(props, before, after, []) == [
        ("info: example:t in module app-front still resolves at 1.0 — outside the modules the inserted entry in "
         "app-boot/pom.xml governs (p-1-1); left for the next round")]


@pytest.mark.parametrize("module_overrides", [True, False])
def test_proposals_api_fields(tmp_path, module_overrides):
    pl = plan(write(tmp_path, company_files()), [item()], c=cfg(module_overrides=module_overrides))
    for p in pl.proposals:
        d = p.to_dict()
        assert {"edits", "modules", "also_modules", "sources"} <= set(d)
        assert d["edits"][0]["pom"].endswith("pom.xml") and set(d["edits"][0]["override"]) >= {"groupId", "version"}
        assert [s["module"] for s in d["sources"]] == d["modules"]
