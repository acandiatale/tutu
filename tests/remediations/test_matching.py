"""matching.py and its use in the inventory: an IVAS (g:a, version) meets a Maven-resolved one, case-insensitively."""
from __future__ import annotations

from sbmig.remediations.inventory import build_inventory
from sbmig.remediations.matching import ga_key, same_ga, same_version, version_key
from sbmig.remediations.models import (
    O_DIRECT_LITERAL,
    O_PARENT_MANAGED,
    O_PLUGIN,
    O_TRANSITIVE,
    O_UNMAPPED,
    ResolvedDep,
)

from .test_inventory import LIB, dep, finding, model, module_pom, only, rdep, root_pom, write

V_MAVEN, V_IVAS = "2.2.0.RELEASE", "2.2.0.release"


# ---------------------------------------------------------------------------- the helper
def test_keys_casefold_and_strip():
    assert version_key(" 2.2.0.RELEASE ") == "2.2.0.release"
    assert version_key(None) == "" and version_key("  ") == ""
    assert ga_key(" Org.Example:Lib") == "org.example:lib" and ga_key(None) == ""


def test_same_version_ignores_case_only():
    assert same_version("2.2.0.release", "2.2.0.RELEASE")
    assert same_version("1.0-Beta", "1.0-BETA")
    assert same_version(None, "") and same_version(" 1.0", "1.0")
    assert not same_version("2.2.0", "2.2.0.RELEASE")              # nothing else is normalised
    assert not same_version("1.0", "1.0.0")


def test_same_ga_ignores_case_only():
    assert same_ga("Org.Example:Lib", "org.example:lib")
    assert not same_ga("org.example:lib", "org.example:lib-core")


# ---------------------------------------------------------------------------- inventory: one version, two spellings
def test_editable_item_matches_across_case_with_mavens_spelling(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(), "core/pom.xml": module_pom("core", dep("example", "lib", V_MAVEN))})
    it = only(build_inventory(p, [finding("CVE-1", version=V_IVAS)], model({"core": [rdep(LIB, V_MAVEN)]})))
    assert it.origin == O_DIRECT_LITERAL and it.version == V_MAVEN and it.reported_version == V_IVAS
    assert it.location.text == V_MAVEN and it.reported_ga == "" and it.finding_ids == ["CVE-1"]


def test_managed_item_matches_across_case(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(parent=True), "core/pom.xml": module_pom("core", dep("example", "lib"))})
    it = only(build_inventory(p, [finding("CVE-1", version=V_IVAS)], model({"core": [rdep(LIB, V_MAVEN)]})))
    assert it.origin == O_PARENT_MANAGED and it.version == V_MAVEN and it.reported_version == V_IVAS


def test_transitive_item_matches_across_case(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(), "core/pom.xml": module_pom("core", dep("example", "a", "1"))})
    resolved = model({"core": [rdep("example:a", "1"), rdep(LIB, V_MAVEN, direct=False, path=["example:a:1"])]})
    it = only(build_inventory(p, [finding("CVE-1", version=V_IVAS)], resolved))
    assert it.origin == O_TRANSITIVE and (it.version, it.reported_version) == (V_MAVEN, V_IVAS)
    assert it.via == ["example:a:1"]


def test_declared_text_in_another_case_is_still_the_version_maven_resolves(tmp_path):
    # Maven resolves "2.2.0.RELEASE", the pom says "2.2.0.release": one version for Maven, the text is the pom's
    p = write(tmp_path, {"pom.xml": root_pom(), "core/pom.xml": module_pom("core", dep("example", "lib", V_IVAS))})
    it = only(build_inventory(p, [finding("CVE-1", version=V_MAVEN)], model({"core": [rdep(LIB, V_MAVEN)]})))
    assert it.origin == O_DIRECT_LITERAL and it.location.text == V_IVAS and it.reported_version == ""


def test_two_ivas_spellings_of_one_version_are_one_group(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(), "core/pom.xml": module_pom("core", dep("example", "lib", V_MAVEN))})
    resolved = model({"core": [rdep(LIB, V_MAVEN)]})
    it = only(build_inventory(p, [finding("CVE-1", version=V_IVAS, patched="2.2.1"),
                                  finding("CVE-2", version="2.2.0.Release", patched="2.2.2")], resolved))
    assert it.finding_ids == ["CVE-1", "CVE-2"] and it.patched_versions == ["2.2.1", "2.2.2"]
    assert it.version == V_MAVEN and it.reported_version == "2.2.0.Release"        # first in sorted order
    # one IVAS spelling equals Maven's: nothing to report
    it = only(build_inventory(p, [finding("CVE-1", version=V_IVAS), finding("CVE-2", version=V_MAVEN)], resolved))
    assert it.reported_version == "" and it.finding_ids == ["CVE-1", "CVE-2"]


# ---------------------------------------------------------------------------- inventory: g:a case
def test_ga_differing_only_by_case_matches_when_no_exact_ga_resolves(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(), "core/pom.xml": module_pom("core", dep("example", "lib", "1.2.3"))})
    it = only(build_inventory(p, [finding("CVE-1", ident="Example:Lib")], model({"core": [rdep(LIB, "1.2.3")]})))
    assert it.ga == LIB and it.reported_ga == "Example:Lib" and it.origin == O_DIRECT_LITERAL


def test_exact_ga_wins_over_the_case_insensitive_one(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(modules=("core", "web")),
                         "core/pom.xml": module_pom("core", dep("example", "lib", "1.2.3")),
                         "web/pom.xml": module_pom("web", dep("Example", "Lib", "1.2.3"))})
    resolved = model({"core": [rdep(LIB, "1.2.3")], "web": [rdep("Example:Lib", "1.2.3")]})
    it = only(build_inventory(p, [finding("CVE-1", ident="Example:Lib")], resolved))
    assert it.ga == "Example:Lib" and it.modules == ["web"] and it.reported_ga == ""


# ---------------------------------------------------------------------------- inventory: real mismatches
def test_real_version_mismatch_keeps_the_unmapped_reason(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(), "core/pom.xml": module_pom("core", dep("example", "lib", "1.2.3"))})
    it = only(build_inventory(p, [finding("CVE-1", version="1.0.0")], model({"core": [rdep(LIB, "1.2.3")]})))
    assert it.origin == O_UNMAPPED and it.reason == "IVAS reports 1.0.0, Maven resolves 1.2.3"
    # the g:a in another case still finds what Maven resolves
    it = only(build_inventory(p, [finding("CVE-1", ident="Example:Lib", version="1.0.0")],
                              model({"core": [rdep(LIB, "1.2.3")]})))
    assert it.reason == "IVAS reports 1.0.0, Maven resolves 1.2.3" and it.ga == "Example:Lib"


def test_mismatch_lists_each_resolved_version_once_whatever_its_case(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(modules=("core", "web")), "core/pom.xml": module_pom("core"),
                         "web/pom.xml": module_pom("web")})
    resolved = model({"core": [rdep(LIB, "2.0.RELEASE", direct=False, path=["example:a:1"])],
                      "web": [rdep(LIB, "2.0.release", direct=False, path=["example:b:1"])]})
    it = only(build_inventory(p, [finding("CVE-1", version="1.0")], resolved))
    assert it.origin == O_UNMAPPED and it.reason == "IVAS reports 1.0, Maven resolves 2.0.RELEASE"


def test_plugin_ga_in_another_case(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(), "core/pom.xml": module_pom("core")})
    plugins = {"core": [ResolvedDep(group_id="example", artifact_id="plugdep", version="9.9", direct=False,
                                    path=["org.apache.maven.plugins:maven-x-plugin:1"])]}
    it = only(build_inventory(p, [finding("CVE-1", ident="Example:PlugDep", version="9.9")],
                              model({"core": []}, plugins=plugins)))
    assert it.origin == O_PLUGIN and it.ga == "example:plugdep" and it.reported_ga == "Example:PlugDep"
    assert [(s.module, s.how, s.via) for s in it.sources] == [
        ("core", O_PLUGIN, ["org.apache.maven.plugins:maven-x-plugin:1"])]
