"""Override entries for a dependency resolved with a classifier or a non-jar type (Maven's management key is
g:a:type:classifier): apply.locate_override / render_override, the planner's one-proposal-per-variant rule, apply in
any subset, and verify_resolution against the variant's own dependencies."""
from __future__ import annotations

import types

import pytest

from sbmig.core.config import RemediationConfig, RemediationPolicyConfig
from sbmig.remediations import apply as A
from sbmig.remediations import planner as P
from sbmig.remediations.models import (
    D_APPROVED,
    D_PENDING,
    E_INSERT,
    K_MANAGED_OVERRIDE,
    K_OWN_MANAGEMENT_VERSION,
    O_TRANSITIVE,
    Inventory,
    InventoryItem,
    ModuleResolution,
    ModuleSource,
    PackagedLib,
    Proposal,
    ResolvedDep,
    ResolvedModel,
    TextEdit,
)

DM_POM = ("<project>\n  <modelVersion>4.0.0</modelVersion>\n  <groupId>example</groupId>\n  <artifactId>app</artifactId>\n"
          "  <version>1.0.0</version>\n  <dependencyManagement>\n    <dependencies>\n      <dependency>\n"
          "        <groupId>example</groupId>\n        <artifactId>bom</artifactId>\n        <version>1.0</version>\n"
          "        <type>pom</type>\n        <scope>import</scope>\n      </dependency>\n    </dependencies>\n"
          "  </dependencyManagement>\n</project>\n")
NO_DM_CRLF = "<project>\r\n\t<modelVersion>4.0.0</modelVersion>\r\n\t<dependencies>\r\n\t</dependencies>\r\n</project>\r\n"
PLAIN_POM = DM_POM.replace(DM_POM[DM_POM.index("  <dependencyManagement>"):DM_POM.index("</project>")], "")

# Captured from the function as it was before type / classifier existed (git HEAD 982834d, `locate_override(project,
# "example", "t", "1.1")`), not typed by hand: a plain jar entry must stay byte-identical.
HEAD_DM_EDIT = {"pom": "pom.xml", "line": 15, "old": "",
                "new": "      <dependency>\n        <groupId>example</groupId>\n        <artifactId>t</artifactId>\n"
                       "        <version>1.1</version>\n      </dependency>\n",
                "op": "insert", "anchor": "</dependencies>",
                "override": {"groupId": "example", "artifactId": "t", "version": "1.1"}}
HEAD_NO_DM_EDIT = {"pom": "pom.xml", "line": 3, "old": "",
                   "new": "\t<dependencyManagement>\r\n\t\t<dependencies>\r\n\t\t\t<dependency>\r\n"
                          "\t\t\t\t<groupId>example</groupId>\r\n\t\t\t\t<artifactId>t</artifactId>\r\n"
                          "\t\t\t\t<version>1.1</version>\r\n\t\t\t</dependency>\r\n\t\t</dependencies>\r\n"
                          "\t</dependencyManagement>\r\n",
                   "op": "insert", "anchor": "<dependencies>",
                   "override": {"groupId": "example", "artifactId": "t", "version": "1.1"}}
# proposal_hash of the plain transitive override plan_round made at HEAD 982834d for DM_POM / PLAIN_POM
HEAD_HASH_DM = "f4515416a691102898ee46a9dc1393979453e7c4f76ca944d837c332f2908c5d"
HEAD_HASH_PLAIN = "08ffba58cfea9b5e2e94bb744834c8b94965719b8a9c5121c2aa7704187c2576"


class Src:
    def __init__(self, versions=("1.1",)):
        self.v = list(versions)

    def versions(self, g, a):
        return list(self.v)

    def evidence(self, g, a, v):
        return "repository:nexus.invalid" if v in self.v else ""


def _cfg():
    return types.SimpleNamespace(remediation=RemediationConfig(policy=RemediationPolicyConfig()))


def _write(base, text, name="pom.xml"):
    f = base / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(text.encode())
    return base


def _item(variants=(), sources=None, modules=(), **kw):
    return InventoryItem(ga="example:t", version="1.0", origin=O_TRANSITIVE, patched_versions=["1.1"],
                         via=["example:lib:1.2.3"], finding_ids=["CVE-T"], modules=list(modules),
                         variants=list(variants), sources=list(sources or []), **kw)


def _plan(project, items, rejected=None):
    return P.plan_round(project, Inventory(items=items), _cfg(), Src(), round=1, source_tree_hash="t" * 64,
                        rejected=rejected)


# ------------------------------------------------------------------------------------------------- the entry text
def test_plain_jar_entry_is_byte_identical_to_before(tmp_path):
    assert A.locate_override(_write(tmp_path / "a", DM_POM), "example", "t", "1.1").to_dict() == HEAD_DM_EDIT
    assert A.locate_override(_write(tmp_path / "b", NO_DM_CRLF), "example", "t", "1.1").to_dict() == HEAD_NO_DM_EDIT
    # explicit defaults are the same thing
    assert A.locate_override(tmp_path / "a", "example", "t", "1.1", "jar", "", "pom.xml").to_dict() == HEAD_DM_EDIT


def test_plain_override_proposal_keeps_its_hash(tmp_path):
    (p,) = _plan(_write(tmp_path / "a", DM_POM), [_item()]).proposals
    assert p.proposal_hash == HEAD_HASH_DM
    (q,) = _plan(_write(tmp_path / "b", PLAIN_POM), [_item()]).proposals
    assert q.proposal_hash == HEAD_HASH_PLAIN


@pytest.mark.parametrize("t,c,extra", [
    ("jar", "jakarta", ["<classifier>jakarta</classifier>"]),
    ("test-jar", "", ["<type>test-jar</type>"]),
    ("test-jar", "tests", ["<type>test-jar</type>", "<classifier>tests</classifier>"]),
])
def test_variant_entry_lines_in_maven_order(tmp_path, t, c, extra):
    e = A.locate_override(_write(tmp_path, DM_POM), "example", "t", "1.1", t, c)
    lines = [ln.strip() for ln in e.new.splitlines()]
    assert lines == ["<dependency>", "<groupId>example</groupId>", "<artifactId>t</artifactId>",
                     "<version>1.1</version>", *extra, "</dependency>"]
    assert e.override == {"groupId": "example", "artifactId": "t", "version": "1.1",
                          **({"type": t} if t != "jar" else {}), **({"classifier": c} if c else {})}
    variant = f"{t}/{c}"
    assert A.override_variant(Proposal(id="p", round=1, ga="example:t", from_version="1.0", to_version="1.1",
                                       kind=K_MANAGED_OVERRIDE, edits=[e])) == variant


def test_whole_section_with_a_classifier(tmp_path):
    e = A.locate_override(_write(tmp_path, NO_DM_CRLF), "example", "t", "1.1", classifier="jakarta")
    assert e.new == HEAD_NO_DM_EDIT["new"].replace("\t\t\t\t<version>1.1</version>\r\n",
                                                   "\t\t\t\t<version>1.1</version>\r\n"
                                                   "\t\t\t\t<classifier>jakarta</classifier>\r\n")


def test_entry_shape_refuses_jar_type_key_and_bad_text(tmp_path):
    e = A.locate_override(_write(tmp_path, DM_POM), "example", "t", "1.1", classifier="jakarta")
    bad = TextEdit.from_dict({**e.to_dict(), "override": {**e.override, "type": "jar"}})
    with pytest.raises(A.EditError):
        A.render_diff(tmp_path, [bad])
    bad = TextEdit.from_dict({**e.to_dict(), "override": {**e.override, "scope": "test"}})
    with pytest.raises(A.EditError):
        A.render_diff(tmp_path, [bad])
    with pytest.raises(A.EditError):
        A.locate_override(tmp_path, "example", "t", "1.1", classifier="${x}")


def test_existing_entry_for_another_variant_does_not_block(tmp_path):
    pom = DM_POM.replace("    </dependencies>\n  </dependencyManagement>",
                         "      <dependency>\n        <groupId>example</groupId>\n        <artifactId>t</artifactId>\n"
                         "        <version>1.0</version>\n      </dependency>\n    </dependencies>\n"
                         "  </dependencyManagement>")
    proj = _write(tmp_path, pom)
    with pytest.raises(A.EditError, match="already has a <dependencyManagement> entry for example:t;"):
        A.locate_override(proj, "example", "t", "1.1")
    e = A.locate_override(proj, "example", "t", "1.1", classifier="jakarta")
    assert e.override["classifier"] == "jakarta"
    with_cls = pom.replace("<version>1.0</version>\n      </dependency>\n    </dependencies>",
                           "<version>1.0</version>\n        <classifier>jakarta</classifier>\n      </dependency>\n"
                           "    </dependencies>")
    with pytest.raises(A.EditError, match="entry for example:t:jar:jakarta"):
        A.locate_override(_write(tmp_path / "c", with_cls), "example", "t", "1.1", classifier="jakarta")


def test_locate_override_for_a_module_pom_but_apply_takes_the_root_only(tmp_path):
    _write(tmp_path, PLAIN_POM)
    _write(tmp_path, PLAIN_POM.replace("<artifactId>app</artifactId>", "<artifactId>core</artifactId>"), "core/pom.xml")
    e = A.locate_override(tmp_path, "example", "t", "1.1", classifier="jakarta", pom="core/pom.xml")
    assert e.pom == "core/pom.xml" and e.anchor == "</project>" and "<classifier>jakarta</classifier>" in e.new
    p = Proposal(id="p-1-1", round=1, ga="example:t", from_version="1.0", to_version="1.1", kind=K_MANAGED_OVERRIDE,
                 decision=D_APPROVED, edits=[e])
    res = A.apply_proposals(tmp_path, [p])
    assert res.applied == [] and "project root pom.xml only" in res.skipped["p-1-1"]


# ------------------------------------------------------------------------------------------------- planning
MODULE = ("<project>\n  <parent>\n    <groupId>example</groupId>\n    <artifactId>app</artifactId>\n"
          "    <version>1.0.0</version>\n  </parent>\n  <artifactId>{name}</artifactId>\n{deps}</project>\n")


def _root(base):
    return base.replace("  <version>1.0.0</version>\n", "  <version>1.0.0</version>\n  <packaging>pom</packaging>\n"
                        "  <modules>\n    <module>api</module>\n    <module>web</module>\n  </modules>\n", 1)


def _mm(path, base=DM_POM, web_deps=""):
    """root (with `base`'s management section) + modules api / web inheriting it"""
    _write(path, _root(base))
    _write(path, MODULE.format(name="api", deps=""), "api/pom.xml")
    _write(path, MODULE.format(name="web", deps=web_deps), "web/pom.xml")
    return path


def _both(modules=("web",)):
    """resolves as the plain jar in `web` and with the jakarta classifier in `api`"""
    srcs = [ModuleSource(module="api", how=O_TRANSITIVE, variant="jar/jakarta"),
            *[ModuleSource(module=m, how=O_TRANSITIVE) for m in modules]]
    return _item(variants=["jar/jakarta"], sources=srcs, modules=["api", *modules])


def test_plain_and_classifier_are_two_independent_proposals(tmp_path):
    proj = _mm(tmp_path)
    plan = _plan(proj, [_both()])
    assert not plan.residuals
    plain, cls = plan.proposals
    assert [p.kind for p in plan.proposals] == [K_MANAGED_OVERRIDE] * 2
    assert all(p.requires_approval and p.decision == D_PENDING for p in plan.proposals)
    assert A.override_variant(plain) == "" and A.override_variant(cls) == "jar/jakarta"
    assert plain.modules == ["web"] and cls.modules == ["api"]
    # the plain jar entry is the one a plain-only item gets
    (alone,) = _plan(proj, [_item(modules=["web"])]).proposals
    assert plain.proposal_hash == alone.proposal_hash and plain.edits == alone.edits
    assert "the entry names classifier jakarta like the resolved dependency" in cls.note
    assert "classifier" not in plain.note
    assert plain.edits[0].line == cls.edits[0].line                 # both against the original text


def test_classifier_only_item_plans_no_plain_entry(tmp_path):
    srcs = [ModuleSource(module="api", how=O_TRANSITIVE, variant="jar/jakarta")]
    (p,) = _plan(_mm(tmp_path), [_item(variants=["jar/jakarta"], sources=srcs, modules=["api"])]).proposals
    assert A.override_variant(p) == "jar/jakarta" and p.edits[0].override["classifier"] == "jakarta"


def test_type_only_variant(tmp_path):
    srcs = [ModuleSource(module="api", how=O_TRANSITIVE, variant="test-jar/")]
    (p,) = _plan(_mm(tmp_path), [_item(variants=["test-jar/"], sources=srcs, modules=["api"])]).proposals
    assert p.edits[0].override == {"groupId": "example", "artifactId": "t", "version": "1.1", "type": "test-jar"}


def test_variant_mark_without_variants_stays_a_residual(tmp_path):
    item = _item(reason="type/classifier variant (jar/jakarta)")
    plan = _plan(_write(tmp_path, DM_POM), [item])
    (r,) = plan.residuals
    assert not plan.proposals and "non-jar type or with a classifier" in r.detail


def test_existing_root_entry_for_the_plain_jar_and_an_insert_for_the_classifier(tmp_path):
    pom = DM_POM.replace("    </dependencies>\n  </dependencyManagement>",
                         "      <dependency>\n        <groupId>example</groupId>\n        <artifactId>t</artifactId>\n"
                         "        <version>1.0</version>\n      </dependency>\n    </dependencies>\n"
                         "  </dependencyManagement>")
    proj = _mm(tmp_path, pom)
    plan = _plan(proj, [_both()])
    kinds = sorted((p.kind, A.override_variant(p)) for p in plan.proposals)
    assert kinds == [(K_MANAGED_OVERRIDE, "jar/jakarta"), (K_OWN_MANAGEMENT_VERSION, "")]
    own = next(p for p in plan.proposals if p.kind == K_OWN_MANAGEMENT_VERSION)
    lines = (proj / "pom.xml").read_text().splitlines()
    assert own.edits[0].op == "replace" and lines[own.edits[0].line - 1].strip() == "<version>1.0</version>"
    assert "<artifactId>t</artifactId>" in lines[own.edits[0].line - 2] and own.modules == ["web"]


def test_declared_plain_version_blocks_only_the_plain_entry(tmp_path):
    dep = ("  <dependencies>\n    <dependency>\n      <groupId>example</groupId>\n      <artifactId>t</artifactId>\n"
           "      <version>1.0</version>\n    </dependency>\n  </dependencies>\n")
    plan = _plan(_mm(tmp_path, web_deps=dep), [_both()])
    (p,) = plan.proposals
    assert A.override_variant(p) == "jar/jakarta"
    (r,) = plan.residuals
    assert r.reason == "manual_review" and "is declared with a version in web/pom.xml (1.0)" in r.detail
    assert [s.module for s in r.sources] == ["web"]


def test_rejected_variant_does_not_reject_the_plain_entry(tmp_path):
    proj = _mm(tmp_path)
    plan = _plan(proj, [_both()], rejected={("example:t", "1.1", "jar/jakarta")})
    (p,) = plan.proposals
    assert A.override_variant(p) == ""
    (r,) = plan.residuals
    assert r.reason == "rejected_proposals"
    plan = _plan(proj, [_both()], rejected={("EXAMPLE:t", "1.1")})                   # the plain jar, any case
    (p,) = plan.proposals
    assert A.override_variant(p) == "jar/jakarta"


# ------------------------------------------------------------------------------------------------- applying
def _approved(plan):
    for p in plan.proposals:
        p.decision = D_APPROVED
    return plan.proposals


@pytest.mark.parametrize("base", [DM_POM, PLAIN_POM])
def test_apply_any_subset_and_reapply(tmp_path, base):
    base_in = base
    orig = _mm(tmp_path / "orig", base_in)
    base = (orig / "pom.xml").read_text()
    plain, cls = _approved(_plan(orig, [_both()]))
    outs = {}
    for name, chosen in (("both", [cls, plain]), ("plain", [plain]), ("cls", [cls])):
        cand = _mm(tmp_path / name, base_in)
        res = A.apply_proposals(cand, chosen)
        assert sorted(res.applied) == sorted(p.id for p in chosen) and not res.skipped
        assert A.verify_text_only(orig, cand, chosen, ["pom.xml"]) == []
        outs[name] = (cand / "pom.xml").read_text()
        # each proposal's own diff is exactly what applying it alone does
        if len(chosen) == 1:
            assert A.render_diff(orig, chosen[0].edits) == A._unified("pom.xml", base, outs[name])
    both = outs["both"]
    assert both.count("<dependencyManagement>") == 1 and both.count("<artifactId>t</artifactId>") == 2
    assert both.index("<version>1.1</version>\n") < both.index("<classifier>jakarta</classifier>")
    assert "<classifier>" not in outs["plain"] and outs["cls"].count("<classifier>jakarta</classifier>") == 1
    # idempotent: the same approvals on a fresh copy give the same bytes ...
    again = _mm(tmp_path / "again", base_in)
    A.apply_proposals(again, [plain, cls])
    assert (again / "pom.xml").read_text() == both
    # ... and on the already-changed tree nothing is inserted twice
    res = A.apply_proposals(tmp_path / "both", [plain, cls])
    assert res.applied == [] and set(res.skipped) == {plain.id, cls.id}
    assert (tmp_path / "both" / "pom.xml").read_text() == both


def test_reject_one_apply_the_other_then_replan(tmp_path):
    orig = _mm(tmp_path / "orig")
    _plain, cls = _approved(_plan(orig, [_both()]))
    cand = _mm(tmp_path / "cand")
    assert A.apply_proposals(cand, [cls]).applied == [cls.id]
    # next plan on the changed tree: the classifier entry exists now, the plain jar still gets its insert
    plan = _plan(cand, [_both()])
    keys = sorted((p.kind, A.override_variant(p)) for p in plan.proposals)
    assert (K_MANAGED_OVERRIDE, "") in keys


# ------------------------------------------------------------------------------------------------- verifying
def _dep(ga, v, t="jar", c=""):
    g, a = ga.split(":")
    return ResolvedDep(group_id=g, artifact_id=a, version=v, type=t, classifier=c, path=["example:lib:1.2.3"])


def _model(**mods):
    return ResolvedModel(modules=[ModuleResolution(dir=m, group_id="example", artifact_id=m, version="1",
                                                   dependencies=list(deps)) for m, deps in mods.items()])


def _override(variant, modules, pid="p-1-1"):
    t, _, c = variant.partition("/") if variant else ("jar", "", "")
    ov = {"groupId": "example", "artifactId": "t", "version": "1.1",
          **({"type": t} if t and t != "jar" else {}), **({"classifier": c} if c else {})}
    return Proposal(id=pid, round=1, ga="example:t", from_version="1.0", to_version="1.1", kind=K_MANAGED_OVERRIDE,
                    modules=list(modules),
                    edits=[TextEdit(pom="pom.xml", line=1, old="", new="", op=E_INSERT, anchor="x", override=ov)])


def test_verify_resolution_looks_at_the_variant_dependency():
    before = _model(api=[_dep("example:t", "1.0", c="jakarta")], web=[_dep("example:t", "1.0")])
    moved = _model(api=[_dep("example:t", "1.1", c="jakarta")], web=[_dep("example:t", "1.0")])
    # the classifier entry moved the classifier dependency; the plain jar (not approved) stays: no problem
    assert A.verify_resolution([_override("jar/jakarta", ["api"])], before, moved, []) == []
    # a plain entry does not govern the classifier dependency: checking it against `api` finds nothing to move
    stayed = _model(api=[_dep("example:t", "1.0", c="jakarta")], web=[_dep("example:t", "1.1")])
    assert A.verify_resolution([_override("", ["web"])], before, stayed, []) == []
    # the classifier proposal whose dependency did not move is a problem
    lines = A.verify_resolution([_override("jar/jakarta", ["api"])], before, stayed, [])
    assert "example:t still resolves at 1.0 (p-1-1)" in lines
    assert any("in module api resolves at 1.0, planned 1.1" in ln for ln in lines)


def test_verify_resolution_versions_compare_case_insensitively():
    before = _model(web=[_dep("example:t", "1.0.RELEASE")])
    after = _model(web=[_dep("example:t", "1.1.RELEASE")])
    p = _override("", ["web"])
    p.from_version, p.to_version = "1.0.release", "1.1.Release"
    assert A.verify_resolution([p], before, after, []) == []


def test_verify_resolution_packaged_classifier_jar():
    before = _model(api=[_dep("example:t", "1.0", c="jakarta")], web=[_dep("example:t", "1.0")])
    after = _model(api=[_dep("example:t", "1.1", c="jakarta")], web=[_dep("example:t", "1.0")])
    stale_cls = PackagedLib(artifact="api/target/api.war", entry="WEB-INF/lib/t-1.0-jakarta.jar",
                            file_name="t-1.0-jakarta.jar", ga="example:t", version="1.0")
    plain_jar = PackagedLib(artifact="web/target/web.war", entry="WEB-INF/lib/t-1.0.jar", file_name="t-1.0.jar",
                            ga="example:t", version="1.0")
    cls = _override("jar/jakarta", ["api"])
    assert any("still packages WEB-INF/lib/t-1.0-jakarta.jar" in ln and not ln.startswith("info:")
               for ln in A.verify_resolution([cls], before, after, [stale_cls]))
    assert A.verify_resolution([cls], before, after, [plain_jar]) == []
    # the plain entry ignores the (rejected) classifier jar but still sees a stale plain jar
    plain = _override("", ["web"])
    after2 = _model(api=[_dep("example:t", "1.0", c="jakarta")], web=[_dep("example:t", "1.1")])
    assert A.verify_resolution([plain], before, after2, [stale_cls]) == []
    assert any("t-1.0.jar" in ln for ln in A.verify_resolution([plain], before, after2, [plain_jar]))
