"""inventory.py: declarations the planner must not edit (review finding M5).

A declaration that sits only inside a profile that is not active by default, and a library declared twice in one
`<dependencies>` block, become manual-review residuals that say why (the edit would not move the resolved version)."""
from __future__ import annotations

from sbmig.remediations import planner as P
from sbmig.remediations.inventory import build_inventory
from sbmig.remediations.models import O_DIRECT_LITERAL, O_DIRECT_PROPERTY, O_OWN_MANAGEMENT, O_UNMAPPED

from .test_inventory import LIB, dep, finding, line_of, model, module_pom, only, rdep, root_pom, write
from .test_planner import FakeSource, _cfg


def _profile(pid: str, deps: str, *, active: str | None = None, mgmt: bool = False) -> str:
    act = f"      <activation>\n        <activeByDefault>{active}</activeByDefault>\n      </activation>\n" if active else ""
    body = deps.replace("\n    ", "\n        ").replace("    <dependency>", "        <dependency>", 1)
    if mgmt:
        return (f"    <profile>\n      <id>{pid}</id>\n{act}      <dependencyManagement>\n        <dependencies>\n{body}"
                "        </dependencies>\n      </dependencyManagement>\n    </profile>\n")
    return f"    <profile>\n      <id>{pid}</id>\n{act}      <dependencies>\n{body}      </dependencies>\n    </profile>\n"


def _with_profiles(pom: str, profiles: str) -> str:
    return pom.replace("</project>\n", f"  <profiles>\n{profiles}  </profiles>\n</project>\n")


def _inv(p, version="1.2.3"):
    return build_inventory(p, [finding("CVE-1", patched="1.2.9")], model({".": [], "core": [rdep(LIB, version)]}))


def test_declaration_only_in_an_inactive_profile_is_a_residual(tmp_path):
    core = _with_profiles(module_pom("core"), _profile("old", dep("example", "lib", "1.2.3")))
    p = write(tmp_path, {"pom.xml": root_pom(), "core/pom.xml": core})
    it = only(_inv(p))
    assert it.origin == O_UNMAPPED and it.location is None
    assert it.reason == ("example:lib is declared in core/pom.xml only inside profile(s) old, which are not active by "
                         "default; not changed automatically")
    plan = P.plan_round(p, _inv(p), _cfg(), FakeSource({LIB: ["1.2.9"]}), round=1, source_tree_hash="t")
    assert not plan.proposals and plan.residuals[0].reason == "manual_review" and plan.residuals[0].detail == it.reason


def test_declaration_in_a_profile_active_by_default_is_edited(tmp_path):
    core = _with_profiles(module_pom("core"), _profile("main", dep("example", "lib", "1.2.3"), active="true"))
    p = write(tmp_path, {"pom.xml": root_pom(), "core/pom.xml": core})
    it = only(_inv(p))
    assert it.origin == O_DIRECT_LITERAL and it.location.line == line_of(p, "core/pom.xml", "<version>1.2.3</version>")


def test_active_by_default_false_or_other_activation_is_not_active(tmp_path):
    for active in ("false", None):
        core = _with_profiles(module_pom("core"), _profile("x", dep("example", "lib", "1.2.3"), active=active))
        p = write(tmp_path, {"pom.xml": root_pom(), "core/pom.xml": core})
        assert only(_inv(p)).origin == O_UNMAPPED


def test_top_level_declaration_beside_an_inactive_profile_is_edited_as_before(tmp_path):
    core = _with_profiles(module_pom("core", dep("example", "lib", "1.2.3")),
                          _profile("old", dep("example", "lib", "1.0.0")))
    p = write(tmp_path, {"pom.xml": root_pom(), "core/pom.xml": core})
    it = only(_inv(p))
    assert it.origin == O_DIRECT_LITERAL
    assert it.location.line == line_of(p, "core/pom.xml", "<version>1.2.3</version>")


def test_library_declared_twice_in_one_block_is_a_residual(tmp_path):
    core = module_pom("core", dep("example", "lib", "1.0.0") + dep("example", "lib", "1.2.3"))
    p = write(tmp_path, {"pom.xml": root_pom(), "core/pom.xml": core})
    it = only(_inv(p))
    assert it.origin == O_UNMAPPED
    assert it.reason == ("example:lib is declared 2 times in one <dependencies> block of core/pom.xml; not changed "
                         "automatically")


def test_same_library_with_another_classifier_is_not_a_duplicate(tmp_path):
    core = module_pom("core", dep("example", "lib", "1.2.3")
                      + dep("example", "lib", "1.2.3", extra="      <classifier>tests</classifier>\n"))
    p = write(tmp_path, {"pom.xml": root_pom(), "core/pom.xml": core})
    assert only(_inv(p)).origin == O_DIRECT_LITERAL


def test_duplicate_in_own_management_is_a_residual(tmp_path):
    from .test_inventory import mdep
    root = root_pom(mgmt=mdep("example", "lib", "1.0.0") + mdep("example", "lib", "1.2.3"))
    p = write(tmp_path, {"pom.xml": root, "core/pom.xml": module_pom("core", dep("example", "lib"))})
    it = only(_inv(p))
    assert it.origin == O_UNMAPPED and "declared 2 times in one <dependencies> block of pom.xml" in it.reason


def test_management_only_in_an_inactive_profile_is_a_residual(tmp_path):
    root = _with_profiles(root_pom(), _profile("old", dep("example", "lib", "1.2.3"), mgmt=True))
    p = write(tmp_path, {"pom.xml": root, "core/pom.xml": module_pom("core", dep("example", "lib"))})
    it = only(_inv(p))
    assert it.origin == O_UNMAPPED and "only inside profile(s) old" in it.reason


def test_property_version_in_an_inactive_profile_is_a_residual(tmp_path):
    core = _with_profiles(module_pom("core", props="    <lib.version>1.2.3</lib.version>\n"),
                          _profile("old", dep("example", "lib", "${lib.version}")))
    p = write(tmp_path, {"pom.xml": root_pom(), "core/pom.xml": core})
    assert only(_inv(p)).origin == O_UNMAPPED


def test_plain_declarations_are_unchanged(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(props="    <lib.version>1.2.3</lib.version>\n"),
                         "core/pom.xml": module_pom("core", dep("example", "lib", "${lib.version}"))})
    assert only(_inv(p)).origin == O_DIRECT_PROPERTY
    from .test_inventory import mdep
    p2 = write(tmp_path / "b", {"pom.xml": root_pom(mgmt=mdep("example", "lib", "1.2.3")),
                                "core/pom.xml": module_pom("core", dep("example", "lib"))})
    assert only(_inv(p2)).origin == O_OWN_MANAGEMENT
