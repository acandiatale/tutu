"""planner.py: a version property several artifacts use (review finding M2).

The property is changed only when the target version exists for every artifact that uses it (asked from the same
source that gave the evidence for the first); otherwise the item is a manual-review residual naming the property and
the artifacts sharing it. A property used by one artifact behaves as before."""
from __future__ import annotations

from sbmig.remediations.models import O_DIRECT_PROPERTY, O_TRANSITIVE, InventoryItem, PomLocation

from .test_planner import FakeSource, _cfg, _plan, _write

DB, CORE, ANN = "com.example.json:databind", "com.example.json:core", "com.example.json:annotations"


def _dep(a, v="${jackson.version}", indent="    "):
    return (f"{indent}<dependency>\n{indent}  <groupId>com.example.json</groupId>\n{indent}  <artifactId>{a}</artifactId>\n"
            f"{indent}  <version>{v}</version>\n{indent}</dependency>\n")


def _root(deps: str, mgmt: str = "", modules: str = "", extra: str = "") -> str:
    return ("<project>\n  <modelVersion>4.0.0</modelVersion>\n  <groupId>example</groupId>\n  <artifactId>app</artifactId>\n"
            "  <version>1.0.0</version>\n  <packaging>pom</packaging>\n"
            + (f"  <modules>\n{modules}  </modules>\n" if modules else "")
            + "  <properties>\n    <jackson.version>2.12.3</jackson.version>\n  </properties>\n"
            + (f"  <dependencyManagement>\n    <dependencies>\n{mgmt}    </dependencies>\n  </dependencyManagement>\n"
               if mgmt else "")
            + f"  <dependencies>\n{deps}  </dependencies>\n{extra}</project>\n")


def _module(name: str, deps: str, props: str = "") -> str:
    return ("<project>\n  <parent>\n    <groupId>example</groupId>\n    <artifactId>app</artifactId>\n"
            "    <version>1.0.0</version>\n  </parent>\n"
            f"  <artifactId>{name}</artifactId>\n"
            + (f"  <properties>\n{props}  </properties>\n" if props else "")
            + f"  <dependencies>\n{deps}  </dependencies>\n</project>\n")


def _item(proj, ga=DB, hints=("2.12.7.1",), fid="CVE-DB"):
    line = (proj / "pom.xml").read_text().splitlines().index("    <jackson.version>2.12.3</jackson.version>") + 1
    return InventoryItem(ga=ga, version="2.12.3", origin=O_DIRECT_PROPERTY, finding_ids=[fid], patched_versions=list(hints),
                         location=PomLocation("pom.xml", line, "2.12.3", property="jackson.version", element="property"))


def test_target_missing_for_another_user_of_the_property_is_a_residual(tmp_path):
    proj = _write(tmp_path, _root(_dep("databind") + _dep("core")))
    src = FakeSource({DB: ["2.12.3", "2.12.7.1"], CORE: ["2.12.3", "2.12.7"]})
    plan = _plan(proj, [_item(proj)], source=src)
    assert not plan.proposals
    (r,) = plan.residuals
    assert r.reason == "manual_review"
    assert r.detail == (f"version property ${{jackson.version}} (pom.xml) also sets the version of {CORE}; 2.12.7.1 is not "
                        f"in the repository for {CORE}: the property is not changed automatically")
    assert CORE in src.asked


def test_target_present_for_every_user_is_proposed(tmp_path):
    proj = _write(tmp_path, _root(_dep("databind") + _dep("core") + _dep("annotations")))
    src = FakeSource({DB: ["2.12.7.1"], CORE: ["2.12.7.1"], ANN: ["2.12.7.1"]})
    (p,) = _plan(proj, [_item(proj)], source=src).proposals
    assert (p.kind, p.to_version) == ("property_version", "2.12.7.1")
    assert "${jackson.version} may govern other artifacts" in p.note           # note unchanged


def test_property_used_by_one_artifact_behaves_as_before(tmp_path):
    proj = _write(tmp_path, _root(_dep("databind") + _dep("core", v="2.12.3")))
    src = FakeSource({DB: ["2.12.7.1"]})
    (p,) = _plan(proj, [_item(proj)], source=src).proposals
    assert p.to_version == "2.12.7.1" and src.asked == [DB]


def test_empty_listing_for_the_other_user_is_a_residual(tmp_path):
    proj = _write(tmp_path, _root(_dep("databind") + _dep("core")))
    plan = _plan(proj, [_item(proj)], source=FakeSource({DB: ["2.12.7.1"]}))
    assert not plan.proposals and plan.residuals[0].reason == "manual_review"
    assert f"2.12.7.1 is not in the repository for {CORE}" in plan.residuals[0].detail


def test_users_in_modules_management_and_plugins_are_counted(tmp_path):
    plugin = ("  <build>\n    <plugins>\n      <plugin>\n        <groupId>com.example.json</groupId>\n"
              "        <artifactId>json-maven-plugin</artifactId>\n        <version>${jackson.version}</version>\n"
              "      </plugin>\n    </plugins>\n  </build>\n")
    proj = _write(tmp_path, _root(_dep("databind"), mgmt=_dep("annotations", indent="      "),
                                  modules="    <module>svc</module>\n", extra=plugin))
    _write(tmp_path, _module("svc", _dep("core")), "svc/pom.xml")
    full = {DB: ["2.12.7.1"], CORE: ["2.12.7.1"], ANN: ["2.12.7.1"], "com.example.json:json-maven-plugin": ["2.12.7.1"]}
    assert _plan(proj, [_item(proj)], source=FakeSource(full)).proposals
    for missing in (CORE, ANN, "com.example.json:json-maven-plugin"):
        src = FakeSource({k: v for k, v in full.items() if k != missing})
        plan = _plan(proj, [_item(proj)], source=src)
        assert not plan.proposals and missing in plan.residuals[0].detail, missing


def test_a_module_redefining_the_property_does_not_share_it(tmp_path):
    proj = _write(tmp_path, _root(_dep("databind"), modules="    <module>svc</module>\n"))
    _write(tmp_path, _module("svc", _dep("core"), props="    <jackson.version>2.13.0</jackson.version>\n"), "svc/pom.xml")
    src = FakeSource({DB: ["2.12.7.1"]})
    assert _plan(proj, [_item(proj)], source=src).proposals and CORE not in src.asked


def test_version_spelled_in_another_case_counts_as_present(tmp_path):
    proj = _write(tmp_path, _root(_dep("databind") + _dep("core")))
    src = FakeSource({DB: ["2.12.7.RELEASE"], CORE: ["2.12.7.release"]})
    (p,) = _plan(proj, [_item(proj, hints=("2.12.7.RELEASE",))], source=src).proposals
    assert p.to_version == "2.12.7.RELEASE"


def test_policy_pin_needs_the_version_for_every_user(tmp_path):
    proj = _write(tmp_path, _root(_dep("databind") + _dep("core")))
    cfg = _cfg(policy={"pins": {DB: "2.12.7.1"}})
    plan = _plan(proj, [_item(proj, hints=())], cfg=cfg, source=FakeSource({CORE: ["2.12.7"]}))
    assert not plan.proposals and plan.residuals[0].reason == "manual_review"
    assert _plan(proj, [_item(proj, hints=())], cfg=cfg, source=FakeSource({CORE: ["2.12.7.1"]})).proposals
    cfg2 = _cfg(policy={"pins": {DB: "2.12.7.1", CORE: "2.12.7.1"}})
    assert _plan(proj, [_item(proj, hints=())], cfg=cfg2, source=FakeSource({})).proposals


def test_both_users_vulnerable_merge_into_one_edit_when_both_have_the_target(tmp_path):
    proj = _write(tmp_path, _root(_dep("databind") + _dep("core")))
    a, b = _item(proj, ga=CORE, hints=("2.12.6",), fid="CVE-C"), _item(proj, ga=DB, hints=("2.12.7.1",))
    src = FakeSource({DB: ["2.12.6", "2.12.7.1"], CORE: ["2.12.6", "2.12.7.1"]})
    (p,) = _plan(proj, [a, b], source=src).proposals
    assert p.to_version == "2.12.7.1" and sorted(p.finding_ids) == ["CVE-C", "CVE-DB"]


def test_existing_management_entry_with_a_shared_property(tmp_path):
    mgmt = _dep("databind", indent="      ") + _dep("core", indent="      ")
    proj = _write(tmp_path, _root("", mgmt=mgmt))
    t = InventoryItem(ga=DB, version="2.12.3", origin=O_TRANSITIVE, patched_versions=["2.12.7.1"],
                      via=["example:x:1.0"], finding_ids=["CVE-DB"])
    plan = _plan(proj, [t], source=FakeSource({DB: ["2.12.7.1"], CORE: ["2.12.7"]}))
    assert not plan.proposals and "also sets the version of " + CORE in plan.residuals[0].detail
    assert _plan(proj, [t], source=FakeSource({DB: ["2.12.7.1"], CORE: ["2.12.7.1"]})).proposals
