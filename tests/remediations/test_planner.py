"""planner.py: target choice, change kinds, approval classes, merge, hashes, validate_decision."""
from __future__ import annotations

import types

import pytest

from sbmig.core.config import RemediationConfig, RemediationPolicyConfig
from sbmig.remediations import apply as A
from sbmig.remediations import planner as P
from sbmig.remediations.interfaces import VersionSource
from sbmig.remediations.models import (
    D_AUTO,
    D_PENDING,
    E_INSERT,
    K_BOM_VERSION,
    K_DIRECT_VERSION,
    K_MANAGED_OVERRIDE,
    K_OWN_MANAGEMENT_VERSION,
    K_PARENT_VERSION,
    K_PROPERTY_VERSION,
    O_BOM_MANAGED,
    O_DIRECT_LITERAL,
    O_DIRECT_PROPERTY,
    O_OWN_MANAGEMENT,
    O_PARENT_MANAGED,
    O_PLUGIN,
    O_TRANSITIVE,
    O_UNMAPPED,
    O_VENDORED,
    ApprovalMismatch,
    Decision,
    Inventory,
    InventoryItem,
    PomLocation,
)

POM = """<project>
  <parent>
    <groupId>example</groupId>
    <artifactId>parent</artifactId>
    <version>1.0.0</version>
  </parent>
  <properties>
    <lib.version>2.14.0</lib.version>
  </properties>
  <dependencies>
    <dependency>
      <groupId>example</groupId>
      <artifactId>lib</artifactId>
      <version>1.2.3</version>
    </dependency>
  </dependencies>
</project>
"""
# line numbers in POM
L_PARENT, L_PROP, L_DEP = 5, 8, 14


class FakeSource:
    def __init__(self, versions: dict[str, list[str]], host="nexus.invalid"):
        self.data = versions
        self.host = host
        self.asked: list[str] = []

    def versions(self, g, a):
        self.asked.append(f"{g}:{a}")
        return list(self.data.get(f"{g}:{a}", []))

    def evidence(self, g, a, v):
        return f"repository:{self.host}" if v in self.data.get(f"{g}:{a}", []) else ""


def _cfg(**kw):
    policy = kw.pop("policy", {})
    return types.SimpleNamespace(remediation=RemediationConfig(policy=RemediationPolicyConfig(**policy), **kw))


@pytest.fixture
def project(tmp_path):
    (tmp_path / "pom.xml").write_text(POM)
    return tmp_path


def _direct(version="1.2.3", hints=("1.2.5",), ga="example:lib", fid="CVE-1"):
    return InventoryItem(ga=ga, version=version, origin=O_DIRECT_LITERAL, finding_ids=[fid], patched_versions=list(hints),
                         location=PomLocation("pom.xml", L_DEP, version))


def _prop_item(ga="example:core", hints=("2.14.3",), fid="CVE-2"):
    return InventoryItem(ga=ga, version="2.14.0", origin=O_DIRECT_PROPERTY, finding_ids=[fid], patched_versions=list(hints),
                         location=PomLocation("pom.xml", L_PROP, "2.14.0", property="lib.version", element="property"))


def _plan(project, items, cfg=None, source=None, rejected=None, round=1, tree="t" * 64):
    return P.plan_round(project, Inventory(items=items), cfg or _cfg(), source or FakeSource({}), round=round,
                        source_tree_hash=tree, rejected=rejected)


# ------------------------------------------------------------------------------------------------- target choice


def test_fake_source_satisfies_protocol():
    assert isinstance(FakeSource({}), VersionSource)


def test_direct_literal_lowest_same_major_at_or_above_hint(project):
    src = FakeSource({"example:lib": ["1.2.3", "1.2.4", "1.2.5", "1.2.6", "1.3.0", "2.0.0"]})
    plan = _plan(project, [_direct()], source=src)
    (p,) = plan.proposals
    assert (p.kind, p.from_version, p.to_version, p.evidence) == (K_DIRECT_VERSION, "1.2.3", "1.2.5", "repository:nexus.invalid")
    assert p.risks == [] and p.decision == D_AUTO and not p.requires_approval
    assert p.id == "p-1-1" and p.edits[0].line == L_DEP and p.edits[0].old == "1.2.3" and p.edits[0].new == "1.2.5"
    assert "+      <version>1.2.5</version>" in p.diff and p.diff.startswith("--- a/pom.xml")
    assert p.proposal_hash == p.compute_hash() and p.policy_hash == plan.policy_hash == P.policy_hash(_cfg())
    assert p.source_tree_hash == "t" * 64
    assert (project / "pom.xml").read_text() == POM            # planning never writes


def test_highest_hint_wins_with_several_cves(project):
    src = FakeSource({"example:lib": ["1.2.4", "1.2.5", "1.2.7"]})
    plan = _plan(project, [_direct(hints=("1.2.4", "1.2.7", "1.2.5"))], source=src)
    assert plan.proposals[0].to_version == "1.2.7"


def test_no_hint_is_no_candidate(project):
    src = FakeSource({"example:lib": ["1.2.4"]})
    plan = _plan(project, [_direct(hints=())], source=src)
    assert not plan.proposals
    assert plan.residuals[0].reason == "no_candidate_version" and "no patched version known" in plan.residuals[0].detail


def test_hint_not_in_repository_is_not_chosen(project):
    src = FakeSource({"example:lib": ["1.2.3", "1.2.4"]})
    plan = _plan(project, [_direct(hints=("1.2.5",))], source=src)
    assert not plan.proposals
    r = plan.residuals[0]
    assert r.reason == "no_candidate_version" and "1.2.5 is not in the repository" in r.detail
    assert "1.2.4 (< patched hint 1.2.5)" in r.detail


def test_deny_versions_and_prerelease_excluded(project):
    src = FakeSource({"example:lib": ["1.2.5", "1.2.6-RC1", "1.2.7"]})
    cfg = _cfg(policy={"deny_versions": {"example:lib": ["1.2.5"]}})
    assert _plan(project, [_direct()], cfg=cfg, source=src).proposals[0].to_version == "1.2.7"
    cfg = _cfg(policy={"deny_versions": {"example:lib": ["1.2.5"]}, "allow_prerelease": True})
    assert _plan(project, [_direct()], cfg=cfg, source=src).proposals[0].to_version == "1.2.6-RC1"


def test_nothing_left_lists_the_facts(project):
    src = FakeSource({"example:lib": ["1.2.2", "1.2.5", "1.2.6-RC1"]})
    cfg = _cfg(policy={"deny_versions": {"example:lib": ["1.2.5"]}})
    r = _plan(project, [_direct()], cfg=cfg, source=src).residuals[0]
    assert r.reason == "no_candidate_version"
    assert "1.2.5 (deny_versions)" in r.detail and "1.2.6-RC1 (prerelease)" in r.detail and "1.2.2" in r.detail


def test_major_fallback_and_allow_major_false(project):
    src = FakeSource({"example:lib": ["1.2.3", "2.0.0", "2.1.0", "3.0.0"]})
    (p,) = _plan(project, [_direct()], source=src).proposals
    assert p.to_version == "2.0.0" and p.risks == ["major"] and p.requires_approval and p.decision == D_PENDING
    plan = _plan(project, [_direct()], cfg=_cfg(policy={"allow_major": False}), source=src)
    assert not plan.proposals
    assert plan.residuals[0].reason == "no_candidate_version" and "allow_major is false" in plan.residuals[0].detail


def test_pin_wins_and_needs_to_be_newer(project):
    src = FakeSource({"example:lib": ["1.2.5"]})
    cfg = _cfg(policy={"pins": {"example:lib": "1.2.9"}})
    (p,) = _plan(project, [_direct(hints=())], cfg=cfg, source=src).proposals
    assert p.to_version == "1.2.9" and p.evidence == "policy:pins"
    cfg = _cfg(policy={"pins": {"example:lib": "1.2.3"}})
    r = _plan(project, [_direct()], cfg=cfg, source=src).residuals[0]
    assert r.reason == "no_candidate_version" and "not newer" in r.detail


def test_deny_by_ga_and_group_wildcard(project):
    src = FakeSource({"example:lib": ["1.2.5"]})
    for deny in (["example:lib"], ["example:*"]):
        plan = _plan(project, [_direct()], cfg=_cfg(policy={"deny": deny}), source=src)
        assert not plan.proposals and plan.residuals[0].reason == "policy_denied"
    assert _plan(project, [_direct()], cfg=_cfg(policy={"deny": ["other:*"]}), source=src).proposals


# ------------------------------------------------------------------------------------------------- origins / kinds


@pytest.mark.parametrize("origin", [O_PLUGIN, O_VENDORED, O_UNMAPPED])
def test_manual_origins(project, origin):
    item = InventoryItem(ga="example:lib", version="1.2.3", origin=origin, finding_ids=["CVE-1"], patched_versions=["1.2.5"])
    src = FakeSource({"example:lib": ["1.2.5"]})
    plan = _plan(project, [item], source=src)
    assert not plan.proposals and plan.residuals[0].reason == "manual_review"
    assert src.asked == []


def test_property_kind_has_note(project):
    src = FakeSource({"example:core": ["2.14.3"]})
    (p,) = _plan(project, [_prop_item()], source=src).proposals
    assert p.kind == K_PROPERTY_VERSION and "${lib.version} may govern other artifacts" in p.note
    assert "+    <lib.version>2.14.3</lib.version>" in p.diff


def test_own_management_kinds(project):
    src = FakeSource({"example:lib": ["1.2.5"], "example:core": ["2.14.3"]})
    lit = _direct()
    lit.origin = O_OWN_MANAGEMENT
    assert _plan(project, [lit], source=src).proposals[0].kind == K_OWN_MANAGEMENT_VERSION
    prop = _prop_item()
    prop.origin = O_OWN_MANAGEMENT
    assert _plan(project, [prop], source=src).proposals[0].kind == K_PROPERTY_VERSION


def test_transitive_gets_an_override_proposal(project):
    item = InventoryItem(ga="example:t", version="1.0", origin=O_TRANSITIVE, patched_versions=["1.1"],
                         via=["example:lib:1.2.3"], finding_ids=["CVE-T"])
    (p,) = _plan(project, [item], source=FakeSource({"example:t": ["1.1"]})).proposals
    assert (p.kind, p.from_version, p.to_version, p.risks) == (K_MANAGED_OVERRIDE, "1.0", "1.1", ["managed_override"])
    assert p.requires_approval and p.decision == D_PENDING and p.evidence == "repository:nexus.invalid"
    assert "pins the transitive dependency brought by example:lib:1.2.3" in p.note
    assert "will NOT move this version any more" in p.note and "must remove the override" in p.note


def test_managed_without_pin_gets_an_override_proposal(project):
    known = InventoryItem(ga="example:m", version="1.0", origin=O_PARENT_MANAGED, patched_versions=["1.1"],
                          manager="example:parent:1.0.0", manager_location=PomLocation("pom.xml", L_PARENT, "1.0.0"))
    unknown = InventoryItem(ga="example:n", version="1.0", origin=O_BOM_MANAGED, patched_versions=["1.1"])
    plan = _plan(project, [known, unknown], source=FakeSource({"example:m": ["1.1"], "example:n": ["1.1"]}))
    assert [(p.ga, p.kind, p.decision) for p in plan.proposals] == [
        ("example:m", K_MANAGED_OVERRIDE, D_PENDING), ("example:n", K_MANAGED_OVERRIDE, D_PENDING)]
    assert "overrides the version managed by example:parent:1.0.0" in plan.proposals[0].note
    assert "a parent / BOM outside the project" in plan.proposals[1].note and not plan.residuals


def test_managed_with_pinned_manager_proposes_parent_version(project):
    item = InventoryItem(ga="example:m", version="1.0", origin=O_PARENT_MANAGED, finding_ids=["CVE-9"],
                         manager="example:parent:1.0.0",
                         manager_location=PomLocation("pom.xml", L_PARENT, "1.0.0", element="parent"))
    cfg = _cfg(policy={"pins": {"example:parent": "1.0.4"}})
    (p,) = _plan(project, [item], cfg=cfg).proposals
    assert (p.kind, p.ga, p.from_version, p.to_version) == (K_PARENT_VERSION, "example:parent", "1.0.0", "1.0.4")
    assert p.risks == ["parent"] and p.decision == D_PENDING and p.evidence == "policy:pins"
    assert "example:m:1.0" in p.note
    item.origin = O_BOM_MANAGED
    (p,) = _plan(project, [item], cfg=cfg).proposals
    assert p.kind == K_BOM_VERSION and p.risks == ["bom"]


# ------------------------------------------------------------------------------------------------- approval classes


def test_require_approval_true_makes_everything_pending(project):
    src = FakeSource({"example:lib": ["1.2.5"]})
    (p,) = _plan(project, [_direct()], cfg=_cfg(require_approval=True), source=src).proposals
    assert p.requires_approval and p.decision == D_PENDING
    (p,) = _plan(project, [_direct()], cfg=_cfg(require_approval=False), source=src).proposals
    assert not p.requires_approval and p.decision == D_AUTO


def test_always_approve_controls_major(project):
    src = FakeSource({"example:lib": ["2.0.0"]})
    (p,) = _plan(project, [_direct()], cfg=_cfg(always_approve=[]), source=src).proposals
    assert p.risks == ["major"] and p.decision == D_AUTO


# ------------------------------------------------------------------------------------------------- merge / rejected / stale


def test_merge_on_one_property_line(project):
    src = FakeSource({"example:core": ["2.14.3", "2.15.0"], "example:databind": ["2.14.3", "2.15.0"]})
    a = _prop_item(ga="example:core", hints=("2.14.3",), fid="CVE-A")
    b = _prop_item(ga="example:databind", hints=("2.15.0",), fid="CVE-B")
    plan = _plan(project, [b, a], source=src)
    (p,) = plan.proposals
    assert p.ga == "example:core" and p.to_version == "2.15.0" and p.finding_ids == ["CVE-A", "CVE-B"]
    assert "example:databind:2.14.0" in p.note and len(p.edits) == 1


def test_rejected_is_not_proposed_again(project):
    src = FakeSource({"example:lib": ["1.2.5"]})
    plan = _plan(project, [_direct()], source=src, rejected={("example:lib", "1.2.5")})
    assert not plan.proposals and plan.residuals[0].reason == "rejected_proposals"


def test_stale_location(project):
    item = _direct()
    item.location = PomLocation("pom.xml", L_DEP, "1.2.2")
    plan = _plan(project, [item], source=FakeSource({"example:lib": ["1.2.5"]}))
    assert not plan.proposals and plan.residuals[0].reason == "stale_location"
    item.location = PomLocation("pom.xml", 999, "1.2.3")
    assert _plan(project, [item]).residuals[0].reason == "stale_location"


def test_incomplete_inventory_plans_nothing(project):
    inv = Inventory(items=[_direct()], complete=False, notes=["x"])
    plan = P.plan_round(project, inv, _cfg(), FakeSource({"example:lib": ["1.2.5"]}), round=1, source_tree_hash="t")
    assert not plan.proposals and plan.residuals[0].reason == "inventory_incomplete" and plan.notes


# ------------------------------------------------------------------------------------------------- hashes


def test_plan_is_deterministic_and_hash_sensitive(project):
    src = FakeSource({"example:lib": ["1.2.5", "1.2.6"], "example:core": ["2.14.3"]})
    items = [_direct(), _prop_item()]
    p1 = _plan(project, items, source=src)
    p2 = _plan(project, list(reversed(items)), source=src)
    assert p1.to_dict() == p2.to_dict()
    assert [p.id for p in p1.proposals] == ["p-1-1", "p-1-2"]
    a = p1.proposals[0]
    h = a.proposal_hash
    a.to_version = "1.2.6"
    assert a.compute_hash() != h
    a.to_version, a.note, a.decision = p2.proposals[0].to_version, "other", "approved"
    assert a.compute_hash() == h                        # decision / note are not hashed


def test_policy_hash_sensitivity():
    base = P.policy_hash(_cfg())
    assert base == P.policy_hash(_cfg())
    variants = [_cfg(require_approval=True), _cfg(always_approve=["major"]),
                _cfg(policy={"pins": {"example:lib": "1.2.5"}}), _cfg(policy={"deny": ["example:*"]}),
                _cfg(policy={"deny_versions": {"example:lib": ["1.0"]}}), _cfg(policy={"allow_prerelease": True}),
                _cfg(policy={"allow_major": False})]
    hashes = {P.policy_hash(c) for c in variants}
    assert base not in hashes and len(hashes) == len(variants)
    assert P.policy_hash(_cfg(always_approve=["bom", "major"])) == P.policy_hash(_cfg(always_approve=["major", "bom"]))


def test_policy_hash_covers_ignore_actions_sorted_and_upper_cased():
    base = P.policy_hash(_cfg(ignore_actions=["PC", "IG"]))
    assert base == P.policy_hash(_cfg(ignore_actions=[" ig", "pc"]))
    assert base != P.policy_hash(_cfg(ignore_actions=["PC"]))
    assert base != P.policy_hash(_cfg(ignore_actions=[]))
    assert P.policy_hash(_cfg()) == P.policy_hash(_cfg(ignore_actions=[]))   # default: nothing ignored
    assert P.policy_hash(_cfg(ignore_actions=["PC"])) != P.policy_hash(_cfg(ignore_actions=[]))


def test_policy_hash_ignores_ivas_summary_check():
    assert P.policy_hash(_cfg()) == P.policy_hash(_cfg(ivas_summary_check="nonzero"))


# ------------------------------------------------------------------------------------------------- validate_decision


@pytest.fixture
def pending(project):
    src = FakeSource({"example:lib": ["1.2.5"]})
    (p,) = _plan(project, [_direct()], cfg=_cfg(require_approval=True), source=src).proposals
    return p


def _decision(p, **kw):
    d = Decision(proposal_id=p.id, decision="approved", proposal_hash=p.proposal_hash,
                 source_tree_hash=p.source_tree_hash, policy_hash=p.policy_hash, diff=p.diff)
    for k, v in kw.items():
        setattr(d, k, v)
    return d


def _check(p, d, tree=None, policy=None):
    P.validate_decision(p, d, current_tree_hash=tree or p.source_tree_hash, current_policy_hash=policy or p.policy_hash)


def test_validate_decision_accepts_exact_match_and_crlf_diff(pending):
    _check(pending, _decision(pending))
    _check(pending, _decision(pending, decision="rejected"))
    _check(pending, _decision(pending, diff=pending.diff.replace("\n", "\r\n")))


@pytest.mark.parametrize("field,value,needle", [
    ("proposal_hash", "0" * 64, "proposal hash"),
    ("source_tree_hash", "0" * 64, "source tree hash"),
    ("policy_hash", "0" * 64, "policy hash"),
    ("diff", "something else", "diff"),
    ("decision", "maybe", "decision must be"),
    ("proposal_id", "p-9-9", "proposal id"),
])
def test_validate_decision_each_mismatch(pending, field, value, needle):
    with pytest.raises(ApprovalMismatch) as e:
        _check(pending, _decision(pending, **{field: value}))
    assert needle in str(e.value)


def test_validate_decision_stale_proposal(pending):
    with pytest.raises(ApprovalMismatch, match="accepted tree changed"):
        _check(pending, _decision(pending), tree="1" * 64)
    with pytest.raises(ApprovalMismatch, match="policy changed"):
        _check(pending, _decision(pending), policy="1" * 64)


def test_validate_decision_not_pending_or_tampered(pending):
    pending.decision = D_AUTO
    with pytest.raises(ApprovalMismatch, match="not pending"):
        _check(pending, _decision(pending))
    pending.decision = D_PENDING
    pending.to_version = "9.9.9"                         # stored proposal no longer matches its own hash
    with pytest.raises(ApprovalMismatch, match="no longer matches its own hash"):
        _check(pending, _decision(pending))


# ------------------------------------------------------------------------------------------------- RepositoryVersionSource


class FakeLookup:
    def __init__(self, versions, answered=None, trace=None):
        self._v = versions
        self._answered = answered or {}
        self._trace = trace or []

    def versions(self, g, a):
        return list(self._v)

    def lookup_trace(self, g, a):
        return list(self._trace)


def test_repository_source_sorts_and_names_host_only():
    lk = FakeLookup(["1.10.0", "1.2.0", "1.9.0"],
                    answered={("example", "lib"): "https://user:secret@nexus.invalid:8443/repository/public/"})
    src = P.RepositoryVersionSource(None, lookup=lk, allowed_hosts=["Nexus.invalid"])
    assert src.versions("example", "lib") == ["1.2.0", "1.9.0", "1.10.0"]
    ev = src.evidence("example", "lib", "1.9.0")
    assert ev == "repository:nexus.invalid" and "secret" not in ev
    assert src.evidence("example", "lib", "7.0") == ""


def test_repository_source_local_and_unknown_are_no_evidence():
    local = FakeLookup(["1.0"], trace=[("/m2/example/lib", "ok (1 release(s), local repository)")])
    assert P.RepositoryVersionSource(None, lookup=local, allowed_hosts=["nexus.invalid"]).evidence(
        "example", "lib", "1.0") == ""
    assert P.RepositoryVersionSource(None, lookup=FakeLookup(["1.0"])).evidence("example", "lib", "1.0") == ""


def test_repository_source_unapproved_host_is_no_evidence():
    lk = FakeLookup(["1.0"], answered={("example", "lib"): "https://repo.other.invalid/maven2/"})
    assert P.RepositoryVersionSource(None, lookup=lk, allowed_hosts=["nexus.invalid"]).evidence("example", "lib", "1.0") == ""
    # a callable (ServerMaven.allowed_hosts) is read lazily, once
    calls = []
    src = P.RepositoryVersionSource(None, lookup=lk, allowed_hosts=lambda: calls.append(1) or ["repo.other.invalid"])
    assert src.evidence("example", "lib", "1.0") == "repository:repo.other.invalid"
    assert src.evidence("example", "lib", "1.0") == "repository:repo.other.invalid" and calls == [1]
    # nothing configured and no cfg: no approved host at all
    assert P.RepositoryVersionSource(None, lookup=lk).evidence("example", "lib", "1.0") == ""


def test_repository_source_hosts_from_config_and_server_settings(tmp_path):
    from sbmig.core.config import BuildConfig
    settings = tmp_path / "settings.xml"
    settings.write_text("<settings><mirrors><mirror><id>m</id><url>https://nexus.invalid/repo/</url>"
                        "<mirrorOf>*</mirrorOf></mirror></mirrors></settings>")
    lk = FakeLookup(["1.0"], answered={("example", "lib"): "https://nexus.invalid/repo/"})
    cfg = types.SimpleNamespace(build=BuildConfig(settings_file=str(settings)), remediation=RemediationConfig())
    assert P.RepositoryVersionSource(cfg, lookup=lk).evidence("example", "lib", "1.0") == "repository:nexus.invalid"
    cfg2 = types.SimpleNamespace(build=BuildConfig(settings_file=str(settings)),
                                 remediation=RemediationConfig(allowed_repository_hosts=["approved.invalid"]))
    assert P.RepositoryVersionSource(cfg2, lookup=lk).evidence("example", "lib", "1.0") == ""


# ------------------------------------------------------------------------------------------------- several hints in one
def test_several_versions_in_one_hint_prefer_the_current_line(project):
    item = _direct(version="1.2.3", hints=("1.2.7.1, 1.3.4.2",))
    src = FakeSource({"example:lib": ["1.2.5", "1.2.7.1", "1.2.8", "1.3.4.2", "1.3.5"]})
    assert _plan(project, [item], source=src).proposals[0].to_version == "1.2.7.1"
    # nothing at or above the hint on the current line: the other line's hint is the floor
    src = FakeSource({"example:lib": ["1.2.5", "1.3.0", "1.3.4.2", "1.3.5"]})
    assert _plan(project, [item], source=src).proposals[0].to_version == "1.3.4.2"
    # 1.2.8 is on the current line and ≥ 1.2.7.1 — chosen over 1.3.x even when 1.2.7.1 itself is missing
    src = FakeSource({"example:lib": ["1.2.8", "1.3.4.2"]})
    assert _plan(project, [item], source=src).proposals[0].to_version == "1.2.8"


def test_split_hint_shapes(project):
    from sbmig.remediations.inventory import split_patched_versions as split
    assert split("2.12.7.1, 2.13.4.2") == ["2.12.7.1", "2.13.4.2"]
    assert split(" 1.0;1.1  1.2,,") == ["1.0", "1.1", "1.2"]
    assert split("[2.0,3.0)") == ["[2.0,3.0)"]
    assert split("") == [] and split("1.0, 1.0") == ["1.0"]
    # separate findings (separate list items) must ALL be fixed; "1.3.0" alone on another line does not fix "1.2.9"
    item = _direct(version="1.2.3", hints=("1.2.9", "1.2.5"))
    src = FakeSource({"example:lib": ["1.2.5", "1.2.9", "1.3.0"]})
    assert _plan(project, [item], source=src).proposals[0].to_version == "1.2.9"


def test_range_hint_is_no_candidate_naming_the_raw_hint(project):
    item = _direct(version="1.2.3", hints=("[1.2.5,2.0)",))
    plan = _plan(project, [item], source=FakeSource({"example:lib": ["1.2.5"]}))
    assert not plan.proposals
    r = plan.residuals[0]
    assert r.reason == "no_candidate_version" and "[1.2.5,2.0)" in r.detail


# ------------------------------------------------------------------------------------------------- managed overrides
DM_POM = """<project>
  <modelVersion>4.0.0</modelVersion>
  <groupId>example</groupId>
  <artifactId>app</artifactId>
  <version>1.0.0</version>
  <dependencyManagement>
    <dependencies>
      <dependency>
        <groupId>example</groupId>
        <artifactId>bom</artifactId>
        <version>1.0</version>
        <type>pom</type>
        <scope>import</scope>
      </dependency>
    </dependencies>
  </dependencyManagement>
  <dependencies>
    <dependency>
      <groupId>example</groupId>
      <artifactId>lib</artifactId>
    </dependency>
  </dependencies>
</project>
"""


def _trans(ga="example:t", version="1.0", hints=("1.1",), modules=(), via=("example:lib:1.2.3",), fid="CVE-T", reason=""):
    return InventoryItem(ga=ga, version=version, origin=O_TRANSITIVE, patched_versions=list(hints), via=list(via),
                         finding_ids=[fid], modules=list(modules), reason=reason)


def _write(tmp_path, text, name="pom.xml"):
    f = tmp_path / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    return tmp_path


def test_override_into_existing_management_section(tmp_path):
    proj = _write(tmp_path, DM_POM)
    (p,) = _plan(proj, [_trans()], source=FakeSource({"example:t": ["1.1"]})).proposals
    (e,) = p.edits
    assert (e.op, e.pom, e.line, e.old, e.anchor) == (E_INSERT, "pom.xml", 15, "", "</dependencies>")
    assert e.override == {"groupId": "example", "artifactId": "t", "version": "1.1"}
    assert e.new == ("      <dependency>\n        <groupId>example</groupId>\n        <artifactId>t</artifactId>\n"
                     "        <version>1.1</version>\n      </dependency>\n")
    assert p.diff == A.render_diff(proj, [e]) and "+        <version>1.1</version>" in p.diff
    assert p.proposal_hash == p.compute_hash()
    assert (proj / "pom.xml").read_text() == DM_POM                    # planning never writes


def test_override_section_before_dependencies_build_or_project_end(tmp_path):
    src = FakeSource({"example:t": ["1.1"]})
    (p,) = _plan(_write(tmp_path, POM), [_trans()], source=src).proposals       # POM: <dependencies> at line 10
    e = p.edits[0]
    assert (e.line, e.anchor) == (10, "<dependencies>") and e.new.startswith("  <dependencyManagement>\n    <dependencies>\n")
    build = "<project>\n  <modelVersion>4.0.0</modelVersion>\n  <build>\n  </build>\n</project>\n"
    e = _plan(_write(tmp_path, build), [_trans()], source=src).proposals[0].edits[0]
    assert (e.line, e.anchor) == (3, "<build>")
    bare = "<project>\n\t<modelVersion>4.0.0</modelVersion>\n</project>\n"
    e = _plan(_write(tmp_path, bare), [_trans()], source=src).proposals[0].edits[0]
    assert (e.line, e.anchor) == (3, "</project>") and e.new.startswith("\t<dependencyManagement>\n\t\t<dependencies>\n")


def test_existing_root_entry_uses_the_replace_route(tmp_path):
    pom = DM_POM.replace("      <dependency>\n        <groupId>example</groupId>\n        <artifactId>bom</artifactId>",
                         "      <dependency>\n        <groupId>example</groupId>\n        <artifactId>t</artifactId>"
                         "\n        <version>1.0</version>\n      </dependency>\n"
                         "      <dependency>\n        <groupId>example</groupId>\n        <artifactId>bom</artifactId>")
    proj = _write(tmp_path, pom)
    (p,) = _plan(proj, [_trans()], source=FakeSource({"example:t": ["1.1"]})).proposals
    assert p.kind == K_OWN_MANAGEMENT_VERSION and p.edits[0].op == "replace" and p.edits[0].line == 11
    assert p.decision == D_AUTO and "existing <dependencyManagement> entry" in p.note
    # an entry with a version that is not the resolved plain text: never a second entry
    proj = _write(tmp_path, pom.replace("<version>1.0</version>\n      </dependency>", "<version>${ext}</version>\n      </dependency>", 1))
    plan = _plan(proj, [_trans()], source=FakeSource({"example:t": ["1.1"]}))
    assert not plan.proposals and plan.residuals[0].reason == "manual_review"
    assert "a second entry is never added" in plan.residuals[0].detail


def test_override_always_needs_approval(tmp_path):
    proj = _write(tmp_path, DM_POM)
    cfg = _cfg(always_approve=[], require_approval=False)
    (p,) = _plan(proj, [_trans()], cfg=cfg, source=FakeSource({"example:t": ["1.1"]})).proposals
    assert p.requires_approval and p.decision == D_PENDING and p.risks == ["managed_override"]
    assert "never applied automatically" in p.note


def test_override_major_flag(tmp_path):
    proj = _write(tmp_path, DM_POM)
    (p,) = _plan(proj, [_trans(hints=("2.0",))], source=FakeSource({"example:t": ["2.0"]})).proposals
    assert p.risks == ["major", "managed_override"] and p.to_version == "2.0"


def test_override_without_hint_or_candidate_is_residual(tmp_path):
    proj = _write(tmp_path, DM_POM)
    plan = _plan(proj, [_trans(hints=())], source=FakeSource({"example:t": ["1.1"]}))
    assert not plan.proposals and plan.residuals[0].reason == "no_candidate_version"
    plan = _plan(proj, [_trans(hints=("1.2",))], source=FakeSource({"example:t": ["1.1"]}))
    assert not plan.proposals and plan.residuals[0].reason == "no_candidate_version"


def test_single_line_pom_is_residual(tmp_path):
    proj = _write(tmp_path, "<project><modelVersion>4.0.0</modelVersion></project>\n")
    plan = _plan(proj, [_trans()], source=FakeSource({"example:t": ["1.1"]}))
    assert not plan.proposals and plan.residuals[0].reason == "manual_review"
    assert "cannot place an override entry safely" in plan.residuals[0].detail
    one = DM_POM.replace("      </dependency>\n    </dependencies>", "      </dependency></dependencies>")
    plan = _plan(_write(tmp_path, one), [_trans()], source=FakeSource({"example:t": ["1.1"]}))
    assert "cannot place an override entry safely" in plan.residuals[0].detail


ROOT = ("<project>\n  <modelVersion>4.0.0</modelVersion>\n  <groupId>example</groupId>\n  <artifactId>root</artifactId>\n"
        "  <version>1.0.0</version>\n  <packaging>pom</packaging>\n  <modules>\n    <module>core</module>\n"
        "    <module>other</module>\n  </modules>\n</project>\n")


def _mod(name, parent="root", deps=""):
    return (f"<project>\n  <parent>\n    <groupId>example</groupId>\n    <artifactId>{parent}</artifactId>\n"
            f"    <version>1.0.0</version>\n  </parent>\n  <artifactId>{name}</artifactId>\n"
            f"  <dependencies>\n{deps}  </dependencies>\n</project>\n")


def test_module_not_inheriting_from_the_root_is_residual(tmp_path):
    _write(tmp_path, ROOT)
    _write(tmp_path, _mod("core"), "core/pom.xml")
    _write(tmp_path, _mod("other", parent="external-parent"), "other/pom.xml")
    src = FakeSource({"example:t": ["1.1"]})
    (p,) = _plan(tmp_path, [_trans(modules=["core"])], source=src).proposals
    assert p.kind == K_MANAGED_OVERRIDE and p.edits[0].pom == "pom.xml" and p.edits[0].anchor == "</project>"
    plan = _plan(tmp_path, [_trans(modules=["core", "other"])], source=src)
    assert not plan.proposals and "other do not inherit from the root pom.xml" in plan.residuals[0].detail


def test_declared_version_in_a_module_is_residual(tmp_path):
    _write(tmp_path, ROOT)
    dep = ("    <dependency>\n      <groupId>example</groupId>\n      <artifactId>m</artifactId>\n"
           "      <version>${ext.version}</version>\n    </dependency>\n")
    _write(tmp_path, _mod("core", deps=dep), "core/pom.xml")
    _write(tmp_path, _mod("other"), "other/pom.xml")
    item = InventoryItem(ga="example:m", version="1.0", origin=O_PARENT_MANAGED, patched_versions=["1.1"], modules=["core"])
    plan = _plan(tmp_path, [item], source=FakeSource({"example:m": ["1.1"]}))
    assert not plan.proposals and "a declared version wins" in plan.residuals[0].detail


def test_classifier_variant_is_residual(tmp_path):
    proj = _write(tmp_path, DM_POM)
    plan = _plan(proj, [_trans(reason="type/classifier variant (test-jar/tests)")], source=FakeSource({"example:t": ["1.1"]}))
    assert not plan.proposals and "non-jar type or with a classifier" in plan.residuals[0].detail


def test_two_overrides_in_one_round(tmp_path):
    src = FakeSource({"example:t": ["1.1"], "example:u": ["2.1"]})
    for text in (DM_POM, POM):
        proj = _write(tmp_path, text)
        plan = _plan(proj, [_trans(), _trans(ga="example:u", version="2.0", hints=("2.1",), fid="CVE-U")], source=src)
        a, b = plan.proposals
        assert [a.id, b.id] == ["p-1-1", "p-1-2"] and a.edits[0].line == b.edits[0].line      # both against the original
        assert a.diff == A.render_diff(proj, a.edits) and b.diff == A.render_diff(proj, b.edits)
        for p in (a, b):
            p.decision = "approved"
        res = A.apply_proposals(proj, [b, a])                            # order given does not matter: by proposal id
        assert res.applied == ["p-1-2", "p-1-1"] and not res.skipped
        out = (proj / "pom.xml").read_text()
        assert out.count("<dependencyManagement>") == 1
        assert out.index("<artifactId>t</artifactId>") < out.index("<artifactId>u</artifactId>")


def test_same_artifact_at_two_versions_is_one_override(tmp_path):
    proj = _write(tmp_path, DM_POM)
    src = FakeSource({"example:t": ["1.1", "1.2"]})
    plan = _plan(proj, [_trans(), _trans(version="1.0.5", hints=("1.2",), fid="CVE-T2")], source=src)
    (p,) = plan.proposals
    assert p.to_version == "1.2" and p.edits[0].override["version"] == "1.2" and p.finding_ids == ["CVE-T", "CVE-T2"]


# ------------------------------------------------------------------------------------------------- module scope
SCOPE_NOTE = ("also resolved in app-service, app-store through another origin; this edit may not govern them — "
              "whatever stays at 1.2.3 is planned in the next round")


def test_direct_proposal_carries_governed_and_also_modules(project):
    item = _direct()
    item.modules, item.also_modules = ["app-domain", "app-service", "app-store"], ["app-service", "app-store"]
    src = FakeSource({"example:lib": ["1.2.5"]})
    (p,) = _plan(project, [item], source=src).proposals
    assert p.modules == ["app-domain"] and p.also_modules == ["app-service", "app-store"]
    assert SCOPE_NOTE in p.note
    # the scope is not part of the proposal hash: the same proposal without it hashes the same
    plain = _direct()
    (q,) = _plan(project, [plain], source=src).proposals
    assert q.modules == [] and q.also_modules == [] and SCOPE_NOTE not in q.note
    assert p.compute_hash() == q.compute_hash() and p.proposal_hash == q.proposal_hash


def test_scoped_direct_without_also_has_no_note(project):
    item = _direct()
    item.modules = ["app-domain"]
    (p,) = _plan(project, [item], source=FakeSource({"example:lib": ["1.2.5"]})).proposals
    assert p.modules == ["app-domain"] and p.also_modules == [] and "through another origin" not in p.note


def test_override_proposal_governs_every_item_module(tmp_path):
    _write(tmp_path, ROOT)
    _write(tmp_path, _mod("core"), "core/pom.xml")
    _write(tmp_path, _mod("other"), "other/pom.xml")
    (p,) = _plan(tmp_path, [_trans(modules=["core", "other"])], source=FakeSource({"example:t": ["1.1"]})).proposals
    assert p.kind == K_MANAGED_OVERRIDE and p.modules == ["core", "other"] and p.also_modules == []


def test_merge_unions_scopes_and_governed_wins_over_also(project):
    a = _prop_item()
    a.modules, a.also_modules = ["m1", "m2"], ["m2"]
    b = _prop_item(ga="example:annotations", fid="CVE-3")
    b.modules, b.also_modules = ["m2", "m3"], ["m3"]
    src = FakeSource({"example:core": ["2.14.3"], "example:annotations": ["2.14.3"]})
    (p,) = _plan(project, [a, b], source=src).proposals
    assert p.modules == ["m1", "m2"] and p.also_modules == ["m3"]
