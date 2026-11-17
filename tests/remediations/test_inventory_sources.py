"""inventory.py: per-module sources, variants and the split by in-house provenance (synthetic poms, hand-built models)."""
from __future__ import annotations

from sbmig.remediations.inventory import VARIANT_MARK, build_inventory
from sbmig.remediations.models import (
    O_DIRECT_LITERAL,
    O_DIRECT_PROPERTY,
    O_OWN_MANAGEMENT,
    O_PARENT_MANAGED,
    O_TRANSITIVE,
    Inventory,
    InventoryItem,
    ModuleSource,
    PomLocation,
)

from .test_inventory import (
    INH,
    INH_KEYS,
    LIB,
    dep,
    finding,
    line_of,
    mdep,
    model,
    module_pom,
    only,
    rdep,
    root_pom,
    write,
)

PARENT = "example.ext:base-parent"           # the external parent of root_pom(parent=True)


def _variant(ga: str, v: str, t: str, c: str, **kw):
    d = rdep(ga, v, **kw)
    d.type, d.classifier = t, c
    return d


# ---------------------------------------------------------------------------- sources
def test_direct_literal_source_names_the_declaration_line(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(), "core/pom.xml": module_pom("core", dep("example", "lib", "1.2.3"))})
    it = only(build_inventory(p, [finding("CVE-1")], model({"core": [rdep(LIB, "1.2.3")]})))
    (s,) = it.sources
    assert (s.module, s.how, s.via, s.manager, s.inhouse, s.variant) == ("core", O_DIRECT_LITERAL, [], "", [], "")
    assert s.declared == it.location
    assert s.declared.line == line_of(p, "core/pom.xml", "<version>1.2.3</version>")


def test_property_source_declares_the_dependency_line_not_the_property(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(props="    <lib.version>1.2.3</lib.version>\n"),
                         "core/pom.xml": module_pom("core", dep("example", "lib", "${lib.version}"))})
    it = only(build_inventory(p, [finding("CVE-1")], model({"core": [rdep(LIB, "1.2.3")]})))
    (s,) = it.sources
    assert s.how == O_DIRECT_PROPERTY and it.location.pom == "pom.xml"
    d = s.declared
    assert (d.pom, d.text, d.element) == ("core/pom.xml", "${lib.version}", "dependency")
    assert d.line == line_of(p, "core/pom.xml", "${lib.version}")


def test_managed_version_source_points_at_the_declaration_without_version_text(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(mgmt=mdep("example", "lib", "1.2.3")),
                         "core/pom.xml": module_pom("core", dep("example", "lib"))})
    it = only(build_inventory(p, [finding("CVE-1")], model({"core": [rdep(LIB, "1.2.3")]})))
    (s,) = it.sources
    assert s.how == O_OWN_MANAGEMENT and it.location.element == "management"
    assert (s.declared.pom, s.declared.text, s.declared.element) == ("core/pom.xml", "", "dependency")
    assert s.declared.line == line_of(p, "core/pom.xml", "<dependency>")
    # managed by the external parent: the parent's g:a:v is the manager
    p2 = write(tmp_path / "par", {"pom.xml": root_pom(parent=True), "core/pom.xml": module_pom("core", dep("example", "lib"))})
    it = only(build_inventory(p2, [finding("CVE-1")], model({"core": [rdep(LIB, "1.2.3")]})))
    (s,) = it.sources
    assert (s.how, s.manager, s.declared.text) == (O_PARENT_MANAGED, f"{PARENT}:4.0.0", "")


def test_transitive_source_carries_the_modules_own_path(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(modules=("core", "web")),
                         "core/pom.xml": module_pom("core", dep("example", "a", "1")),
                         "web/pom.xml": module_pom("web", dep("example", "b", "1"))})
    resolved = model({
        "core": [rdep(LIB, "1.2.3", direct=False, path=["example:a:1", "example:mid:2"])],
        "web": [rdep(LIB, "1.2.3", direct=False, path=["example:b:1"])]})
    it = only(build_inventory(p, [finding("CVE-1")], resolved))
    assert it.via == ["example:b:1"]                                    # the item keeps the shortest path
    assert [(s.module, s.how, s.via, s.declared) for s in it.sources] == [
        ("core", O_TRANSITIVE, ["example:a:1", "example:mid:2"], None), ("web", O_TRANSITIVE, ["example:b:1"], None)]


def test_also_modules_have_sources_in_module_order(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(modules=("a", "b", "c")),
                         "a/pom.xml": module_pom("a", dep("example", "x", "1")),
                         "b/pom.xml": module_pom("b", dep("example", "lib", "1.2.3")),
                         "c/pom.xml": module_pom("c", dep("example", "y", "1"))})
    resolved = model({"a": [rdep(LIB, "1.2.3", direct=False, path=["example:x:1"])], "b": [rdep(LIB, "1.2.3")],
                      "c": [rdep(LIB, "1.2.3", direct=False, path=["example:y:1"])]})
    it = only(build_inventory(p, [finding("CVE-1")], resolved))
    assert it.also_modules == ["a", "c"]
    assert [(s.module, s.how, s.via) for s in it.sources] == [
        ("a", O_TRANSITIVE, ["example:x:1"]), ("b", O_DIRECT_LITERAL, []), ("c", O_TRANSITIVE, ["example:y:1"])]


def test_inhouse_source_names_the_artifact(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(modules=("app",)),
                         "app/pom.xml": module_pom("app", dep("com.example.inhouse", "inhouse-core", "4.0.0"))})
    resolved = model({"app": [rdep(LIB, "1.2.3", direct=False, path=[f"{INH}:4.0.0"])]})
    it = only(build_inventory(p, [finding("CVE-1")], resolved, inhouse=INH_KEYS))
    (s,) = it.sources
    assert (s.how, s.via, s.inhouse) == (O_TRANSITIVE, [f"{INH}:4.0.0"], [INH])


def _variant_project(tmp_path):
    return write(tmp_path, {"pom.xml": root_pom(modules=("core", "web")),
                            "core/pom.xml": module_pom("core", dep("example", "a", "1")),
                            "web/pom.xml": module_pom("web", dep("example", "b", "1"))})


def _plain(mod: str):
    return rdep(LIB, "1.2.3", direct=False, path=[f"example:{mod}:1"])


def test_plain_jar_only_has_no_variant(tmp_path):
    p = _variant_project(tmp_path)
    it = only(build_inventory(p, [finding("CVE-1")], model({"core": [_plain("a")]})))
    assert it.variants == [] and it.sources[0].variant == "" and VARIANT_MARK not in it.reason


def test_classifier_only_lists_the_variants(tmp_path):
    p = _variant_project(tmp_path)
    resolved = model({"web": [_variant(LIB, "1.2.3", "jar", "jdk8", direct=False, path=["example:b:1"]),
                              _variant(LIB, "1.2.3", "test-jar", "tests", direct=False, path=["example:b:1"])]})
    it = only(build_inventory(p, [finding("CVE-1")], resolved))
    assert it.variants == ["jar/jdk8", "test-jar/tests"]
    assert it.sources[0].variant == "jar/jdk8, test-jar/tests"
    assert VARIANT_MARK in it.reason                                   # the mark stays until the planner switches


def test_plain_and_classifier_in_one_module_lists_the_plain_jar_first(tmp_path):
    p = _variant_project(tmp_path)
    resolved = model({"web": [_variant(LIB, "1.2.3", "jar", "jakarta", direct=False, path=["example:b:1"]),
                              _plain("b")]})
    it = only(build_inventory(p, [finding("CVE-1")], resolved))
    assert it.variants == ["jar/jakarta"]                              # the plain jar is never in item.variants
    assert it.sources[0].variant == "jar/, jar/jakarta"


def test_plain_in_one_module_classifier_in_the_other(tmp_path):
    p = _variant_project(tmp_path)
    resolved = model({"core": [_plain("a")],
                      "web": [_variant(LIB, "1.2.3", "jar", "jakarta", direct=False, path=["example:b:1"])]})
    it = only(build_inventory(p, [finding("CVE-1")], resolved))
    assert it.variants == ["jar/jakarta"]
    assert [(s.module, s.variant) for s in it.sources] == [("core", ""), ("web", "jar/jakarta")]


def test_sources_round_trip_and_old_state_loads(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(mgmt=mdep("example", "lib", "1.2.3")),
                         "core/pom.xml": module_pom("core", dep("example", "lib"))})
    inv = build_inventory(p, [finding("CVE-1", version="1.2.3")], model({"core": [rdep(LIB, "1.2.3")]}))
    back = Inventory.from_dict(inv.to_dict())
    assert back.to_dict() == inv.to_dict()
    assert isinstance(back.items[0].sources[0], ModuleSource)
    assert isinstance(back.items[0].sources[0].declared, PomLocation)
    old = InventoryItem.from_dict({"ga": LIB, "version": "1.2.3", "origin": "transitive", "modules": ["core"]})
    assert (old.sources, old.reported_ga, old.reported_version, old.variants) == ([], "", "", [])


# ---------------------------------------------------------------------------- split by in-house provenance
def test_transitive_split_between_project_and_inhouse_paths(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(modules=("mod-a", "mod-b")),
                         "mod-a/pom.xml": module_pom("mod-a", dep("org.example", "third", "1")),
                         "mod-b/pom.xml": module_pom("mod-b", dep("com.example.inhouse", "inhouse-core", "4.0.0"))})
    resolved = model({"mod-a": [rdep(LIB, "1.2.3", direct=False, path=["org.example:third:1"])],
                      "mod-b": [rdep(LIB, "1.2.3", direct=False, path=[f"{INH}:4.0.0"])]})
    inv = build_inventory(p, [finding("CVE-1")], resolved, inhouse=INH_KEYS)
    own, inh = inv.items
    assert (own.origin, own.modules, own.inhouse, own.inhouse_modules) == (O_TRANSITIVE, ["mod-a"], [], [])
    assert (inh.origin, inh.modules, inh.inhouse, inh.inhouse_modules) == (O_TRANSITIVE, ["mod-b"], [INH], ["mod-b"])
    assert own.finding_ids == inh.finding_ids == ["CVE-1"]                # one finding, both parts
    assert own.via == ["org.example:third:1"] and inh.via == [f"{INH}:4.0.0"]


def test_parents_group_rule_splits_too(tmp_path):
    # every module inherits the in-house parent through the root; mod-a's path starts at a dependency the project
    # declares (the project's), mod-b's at one no project pom declares (inherited from the parent: in-house)
    p = write(tmp_path, {"pom.xml": root_pom(parent=True, modules=("mod-a", "mod-b")),
                         "mod-a/pom.xml": module_pom("mod-a", dep("org.example", "third", "1")),
                         "mod-b/pom.xml": module_pom("mod-b")})
    resolved = model({"mod-a": [rdep(LIB, "1.2.3", direct=False, path=["org.example:third:1"])],
                      "mod-b": [rdep(LIB, "1.2.3", direct=False, path=["org.example:inherited:1"])]})
    inv = build_inventory(p, [finding("CVE-1")], resolved, inhouse={PARENT: "parents"})
    assert [(i.modules, i.inhouse, i.inhouse_modules) for i in inv.items] == [
        (["mod-a"], [], []), (["mod-b"], [PARENT], ["mod-b"])]
    assert inv.items[1].sources[0].inhouse == [PARENT]


def test_library_that_is_an_inhouse_artifact_keeps_its_modules_together(tmp_path):
    # mod-a declares the in-house artifact (editable text), mod-b gets it through a third-party dependency
    p = write(tmp_path, {"pom.xml": root_pom(modules=("mod-a", "mod-b")),
                         "mod-a/pom.xml": module_pom("mod-a", dep("com.example.inhouse", "inhouse-core", "4.0.0")),
                         "mod-b/pom.xml": module_pom("mod-b", dep("org.example", "third", "1"))})
    resolved = model({"mod-a": [rdep(INH, "4.0.0")],
                      "mod-b": [rdep(INH, "4.0.0", direct=False, path=["org.example:third:1"])]})
    it = only(build_inventory(p, [finding("CVE-1", ident=INH, version="4.0.0")], resolved, inhouse=INH_KEYS))
    assert it.origin == O_DIRECT_LITERAL and it.modules == ["mod-a", "mod-b"] and it.also_modules == ["mod-b"]
    assert it.inhouse == [INH] and it.inhouse_modules == ["mod-a", "mod-b"]


def test_editable_location_over_both_kinds_of_module_stays_the_projects(tmp_path):
    # the root's <dependencyManagement> pins the library for both modules: one version text, the project's own
    p = write(tmp_path, {"pom.xml": root_pom(modules=("mod-a", "mod-b"), mgmt=mdep("example", "lib", "1.2.3")),
                         "mod-a/pom.xml": module_pom("mod-a", dep("org.example", "third", "1")),
                         "mod-b/pom.xml": module_pom("mod-b", dep("com.example.inhouse", "inhouse-core", "4.0.0"))})
    resolved = model({"mod-a": [rdep(LIB, "1.2.3", direct=False, path=["org.example:third:1"])],
                      "mod-b": [rdep(LIB, "1.2.3", direct=False, path=[f"{INH}:4.0.0"])]})
    it = only(build_inventory(p, [finding("CVE-1")], resolved, inhouse=INH_KEYS))
    assert it.origin == O_OWN_MANAGEMENT and it.modules == ["mod-a", "mod-b"] and it.also_modules == []
    assert it.inhouse == [] and it.inhouse_modules == []                # the item is the project's to fix
    assert [(s.module, s.inhouse) for s in it.sources] == [("mod-a", []), ("mod-b", [INH])]
    # the same text governing only in-house modules is still the project's
    resolved = model({"mod-b": [rdep(LIB, "1.2.3", direct=False, path=[f"{INH}:4.0.0"])]})
    it = only(build_inventory(p, [finding("CVE-1")], resolved, inhouse=INH_KEYS))
    assert it.origin == O_OWN_MANAGEMENT and it.inhouse == [] and it.sources[0].inhouse == [INH]


def test_every_inhouse_item_is_inhouse_for_all_its_modules(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(modules=("a", "b", "c")),
                         "a/pom.xml": module_pom("a", dep("example", "lib", "1.2.3")),
                         "b/pom.xml": module_pom("b", dep("com.example.inhouse", "inhouse-core", "4.0.0")),
                         "c/pom.xml": module_pom("c", dep("example", "y", "1"))})
    resolved = model({"a": [rdep(LIB, "1.2.3")], "b": [rdep(LIB, "1.2.3", direct=False, path=[f"{INH}:4.0.0"])],
                      "c": [rdep(LIB, "1.2.3", direct=False, path=["example:y:1"])]})
    inv = build_inventory(p, [finding("CVE-1")], resolved, inhouse=INH_KEYS)
    for it in inv.items:
        assert not it.inhouse or it.inhouse_modules == it.modules
        assert all(bool(s.inhouse) == bool(it.inhouse) for s in it.sources)
    assert [(i.origin, i.modules, i.also_modules) for i in inv.items] == [
        (O_DIRECT_LITERAL, ["a", "c"], ["c"]), (O_TRANSITIVE, ["b"], [])]
