"""inventory.py: finding → resolved dependency → exact pom location (synthetic pom trees, hand-built models)."""
from __future__ import annotations

from pathlib import Path

from sbmig.remediations.inventory import ProjectPoms, build_inventory
from sbmig.remediations.models import (
    O_BOM_MANAGED,
    O_DIRECT_LITERAL,
    O_DIRECT_PROPERTY,
    O_OWN_MANAGEMENT,
    O_PARENT_MANAGED,
    O_PLUGIN,
    O_TRANSITIVE,
    O_UNMAPPED,
    O_VENDORED,
    Finding,
    ModuleResolution,
    ResolvedDep,
    ResolvedModel,
)

LIB = "example:lib"


def root_pom(*, parent: bool = False, props: str = "", mgmt: str = "", modules=("core",), deps: str = "") -> str:
    par = ("  <parent>\n    <groupId>example.ext</groupId>\n    <artifactId>base-parent</artifactId>\n"
           "    <version>4.0.0</version>\n    <relativePath/>\n  </parent>\n") if parent else ""
    mods = "".join(f"    <module>{m}</module>\n" for m in modules)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<project xmlns="http://maven.apache.org/POM/4.0.0">\n'
        "  <modelVersion>4.0.0</modelVersion>\n"
        f"{par}"
        "  <groupId>example</groupId>\n"
        "  <artifactId>root</artifactId>\n"
        "  <version>1.0.0</version>\n"
        "  <packaging>pom</packaging>\n"
        f"  <modules>\n{mods}  </modules>\n"
        f"  <properties>\n{props}  </properties>\n"
        f"  <dependencyManagement>\n    <dependencies>\n{mgmt}    </dependencies>\n  </dependencyManagement>\n"
        f"  <dependencies>\n{deps}  </dependencies>\n"
        "</project>\n")


def module_pom(name: str, deps: str = "", props: str = "") -> str:
    return (
        "<project>\n"
        "  <parent>\n    <groupId>example</groupId>\n    <artifactId>root</artifactId>\n    <version>1.0.0</version>\n"
        "  </parent>\n"
        f"  <artifactId>{name}</artifactId>\n"
        f"  <properties>\n{props}  </properties>\n"
        f"  <dependencies>\n{deps}  </dependencies>\n"
        "</project>\n")


def dep(g: str, a: str, v: str | None = None, extra: str = "") -> str:
    ver = f"      <version>{v}</version>\n" if v is not None else ""
    return f"    <dependency>\n      <groupId>{g}</groupId>\n      <artifactId>{a}</artifactId>\n{ver}{extra}    </dependency>\n"


def mdep(g: str, a: str, v: str, extra: str = "") -> str:
    return "  " + dep(g, a, v, extra).replace("\n    ", "\n      ").rstrip(" ")


def write(project: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = project / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return project


def line_of(project: Path, rel: str, needle: str, nth: int = 1) -> int:
    hits = [i for i, ln in enumerate((project / rel).read_text(encoding="utf-8").split("\n"), 1) if needle in ln]
    return hits[nth - 1]


def rdep(ga: str, v: str, *, direct: bool = True, path: list[str] | None = None) -> ResolvedDep:
    g, a = ga.split(":")
    return ResolvedDep(group_id=g, artifact_id=a, version=v, direct=direct, path=path or [])


def model(mods: dict[str, list[ResolvedDep]], *, complete: bool = True, plugins: dict | None = None) -> ResolvedModel:
    out = ResolvedModel(complete=complete)
    for d, deps in mods.items():
        name = "root" if d == "." else d
        out.modules.append(ModuleResolution(dir=d, group_id="example", artifact_id=name, version="1.0.0",
                                            dependencies=deps, plugins=(plugins or {}).get(d, [])))
    return out


def finding(fid: str, ident: str = LIB, version: str = "1.2.3", kind: str = "library", patched: str = "",
            locations: list[str] | None = None) -> Finding:
    return Finding(id=fid, kind=kind, identifier=ident, version=version, patched_version=patched,
                   locations=locations or [])


def only(inv):
    assert len(inv.items) == 1, [i.to_dict() for i in inv.items]
    return inv.items[0]


# ---------------------------------------------------------------------------- origins
def test_direct_literal_exact_line_and_text(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(), "core/pom.xml": module_pom("core", dep("example", "lib", "1.2.3"))})
    inv = build_inventory(p, [finding("CVE-1", patched="1.2.9"), finding("CVE-2", patched="1.3.0")],
                          model({".": [], "core": [rdep(LIB, "1.2.3")]}))
    it = only(inv)
    assert inv.complete
    assert it.origin == O_DIRECT_LITERAL and it.modules == ["core"]
    assert it.finding_ids == ["CVE-1", "CVE-2"] and it.patched_versions == ["1.2.9", "1.3.0"]
    loc = it.location
    assert (loc.pom, loc.text, loc.element, loc.property) == ("core/pom.xml", "1.2.3", "dependency", "")
    assert loc.line == line_of(p, "core/pom.xml", "<version>1.2.3</version>")
    assert "1.2.3" in (p / "core/pom.xml").read_text().split("\n")[loc.line - 1]


def test_direct_literal_version_text_on_its_own_line(tmp_path):
    core = module_pom("core", "    <dependency>\n      <groupId>example</groupId>\n      <artifactId>lib</artifactId>\n"
                              "      <version>\n        1.2.3\n      </version>\n    </dependency>\n")
    p = write(tmp_path, {"pom.xml": root_pom(), "core/pom.xml": core})
    it = only(build_inventory(p, [finding("CVE-1")], model({"core": [rdep(LIB, "1.2.3")]})))
    assert it.origin == O_DIRECT_LITERAL
    assert it.location.line == line_of(p, "core/pom.xml", "        1.2.3") and it.location.text == "1.2.3"


def test_direct_property_defined_in_project_parent(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(props="    <lib.version>1.2.3</lib.version>\n"),
                         "core/pom.xml": module_pom("core", dep("example", "lib", "${lib.version}"))})
    it = only(build_inventory(p, [finding("CVE-1")], model({"core": [rdep(LIB, "1.2.3")]})))
    assert it.origin == O_DIRECT_PROPERTY
    loc = it.location
    assert (loc.pom, loc.text, loc.property, loc.element) == ("pom.xml", "1.2.3", "lib.version", "property")
    assert loc.line == line_of(p, "pom.xml", "<lib.version>")


def test_property_child_overrides_parent(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(props="    <lib.version>9.9</lib.version>\n"),
                         "core/pom.xml": module_pom("core", dep("example", "lib", "${lib.version}"),
                                                    props="    <lib.version>1.2.3</lib.version>\n")})
    it = only(build_inventory(p, [finding("CVE-1")], model({"core": [rdep(LIB, "1.2.3")]})))
    assert it.origin == O_DIRECT_PROPERTY and it.location.pom == "core/pom.xml"


def test_nested_property_indirection(tmp_path):
    props = "    <lib.version>${base.version}</lib.version>\n    <base.version>1.2.3</base.version>\n"
    p = write(tmp_path, {"pom.xml": root_pom(props=props),
                         "core/pom.xml": module_pom("core", dep("example", "lib", "${lib.version}"))})
    it = only(build_inventory(p, [finding("CVE-1")], model({"core": [rdep(LIB, "1.2.3")]})))
    assert it.origin == O_DIRECT_PROPERTY
    assert it.location.property == "base.version" and it.location.line == line_of(p, "pom.xml", "<base.version>1.2.3")


def test_composite_property_is_unmapped(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(props="    <base>1.2</base>\n"),
                         "core/pom.xml": module_pom("core", dep("example", "lib", "${base}.3"))})
    it = only(build_inventory(p, [finding("CVE-1")], model({"core": [rdep(LIB, "1.2.3")]})))
    assert it.origin == O_UNMAPPED and "expression" in it.reason and it.location is None


def test_property_from_external_parent_is_parent_managed(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(parent=True),
                         "core/pom.xml": module_pom("core", dep("example", "lib", "${lib.version}"))})
    it = only(build_inventory(p, [finding("CVE-1")], model({"core": [rdep(LIB, "1.2.3")]})))
    assert it.origin == O_PARENT_MANAGED and it.manager == "example.ext:base-parent:4.0.0"
    ml = it.manager_location
    assert (ml.pom, ml.text, ml.element) == ("pom.xml", "4.0.0", "parent")
    assert ml.line == line_of(p, "pom.xml", "<version>4.0.0</version>")
    assert "lib.version" in it.reason


def test_own_management_literal_and_transitive(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(mgmt=mdep("example", "lib", "1.2.3")),
                         "core/pom.xml": module_pom("core", dep("example", "lib"))})
    it = only(build_inventory(p, [finding("CVE-1")], model({
        "core": [rdep(LIB, "1.2.3")]})))
    assert it.origin == O_OWN_MANAGEMENT
    assert (it.location.pom, it.location.element, it.location.text) == ("pom.xml", "management", "1.2.3")
    assert it.location.line == line_of(p, "pom.xml", "<version>1.2.3</version>")
    # a transitive dependency pinned by the project's own management is own_management too
    p2 = write(tmp_path / "t", {"pom.xml": root_pom(mgmt=mdep("example", "lib", "${lib.version}"),
                                                    props="    <lib.version>1.2.3</lib.version>\n"),
                                "core/pom.xml": module_pom("core", dep("example", "a", "1"))})
    it2 = only(build_inventory(p2, [finding("CVE-1")], model({
        "core": [rdep("example:a", "1"), rdep(LIB, "1.2.3", direct=False, path=["example:a:1"])]})))
    assert it2.origin == O_OWN_MANAGEMENT and it2.location.property == "lib.version"
    assert it2.location.element == "property"


def test_bom_managed_single_bom_no_parent(tmp_path):
    bom = mdep("example", "bom", "5.0", "        <type>pom</type>\n        <scope>import</scope>\n")
    p = write(tmp_path, {"pom.xml": root_pom(mgmt=bom), "core/pom.xml": module_pom("core", dep("example", "lib"))})
    it = only(build_inventory(p, [finding("CVE-1")], model({"core": [rdep(LIB, "1.2.3")]})))
    assert it.origin == O_BOM_MANAGED and it.manager == "example:bom:5.0" and it.reason == ""
    ml = it.manager_location
    assert (ml.pom, ml.text, ml.element, ml.line) == ("pom.xml", "5.0", "bom", line_of(p, "pom.xml", "<version>5.0<"))


def test_bom_version_through_property(tmp_path):
    bom = mdep("example", "bom", "${bom.version}", "        <type>pom</type>\n        <scope>import</scope>\n")
    p = write(tmp_path, {"pom.xml": root_pom(mgmt=bom, props="    <bom.version>5.0</bom.version>\n"),
                         "core/pom.xml": module_pom("core", dep("example", "lib"))})
    it = only(build_inventory(p, [finding("CVE-1")], model({"core": [rdep(LIB, "1.2.3")]})))
    assert it.manager == "example:bom:5.0"
    assert (it.manager_location.property, it.manager_location.line) == ("bom.version", line_of(p, "pom.xml", "<bom.version>"))


def test_parent_managed_external_parent_no_bom(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(parent=True), "core/pom.xml": module_pom("core", dep("example", "lib"))})
    it = only(build_inventory(p, [finding("CVE-1")], model({"core": [rdep(LIB, "1.2.3")]})))
    assert it.origin == O_PARENT_MANAGED and it.manager == "example.ext:base-parent:4.0.0" and it.reason == ""
    assert it.manager_location.line == line_of(p, "pom.xml", "<version>4.0.0</version>")


def test_ambiguous_provenance_parent_and_bom(tmp_path):
    bom = mdep("example", "bom", "5.0", "        <type>pom</type>\n        <scope>import</scope>\n")
    p = write(tmp_path, {"pom.xml": root_pom(parent=True, mgmt=bom),
                         "core/pom.xml": module_pom("core", dep("example", "lib"))})
    it = only(build_inventory(p, [finding("CVE-1")], model({"core": [rdep(LIB, "1.2.3")]})))
    assert it.origin == O_PARENT_MANAGED and it.manager == "" and it.manager_location is None
    assert it.reason.startswith("ambiguous provenance")
    assert "example.ext:base-parent:4.0.0" in it.reason and "example:bom:5.0" in it.reason


def test_ambiguous_two_boms_without_parent(tmp_path):
    imp = "        <type>pom</type>\n        <scope>import</scope>\n"
    p = write(tmp_path, {"pom.xml": root_pom(mgmt=mdep("example", "bom", "5.0", imp) + mdep("example", "bom2", "1", imp)),
                         "core/pom.xml": module_pom("core", dep("example", "lib"))})
    it = only(build_inventory(p, [finding("CVE-1")], model({"core": [rdep(LIB, "1.2.3")]})))
    assert it.origin == O_BOM_MANAGED and it.manager == "" and it.reason.startswith("ambiguous provenance")


def test_managed_by_facts_resolve_ambiguity(tmp_path):
    bom = mdep("example", "bom", "5.0", "        <type>pom</type>\n        <scope>import</scope>\n")
    p = write(tmp_path, {"pom.xml": root_pom(parent=True, mgmt=bom),
                         "core/pom.xml": module_pom("core", dep("example", "lib"))})
    it = only(build_inventory(p, [finding("CVE-1")], model({"core": [rdep(LIB, "1.2.3")]}),
                              managed_by=lambda ga: "example:bom:5.0" if ga == LIB else None))
    assert it.origin == O_BOM_MANAGED and it.manager == "example:bom:5.0" and it.manager_location.text == "5.0"


def test_transitive_with_shortest_via(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(modules=("core", "web")),
                         "core/pom.xml": module_pom("core", dep("example", "a", "1")),
                         "web/pom.xml": module_pom("web", dep("example", "b", "1"))})
    resolved = model({
        "core": [rdep(LIB, "1.2.3", direct=False, path=["example:a:1", "example:mid:2"])],
        "web": [rdep(LIB, "1.2.3", direct=False, path=["example:b:1"])]})
    it = only(build_inventory(p, [finding("CVE-1")], resolved))
    assert it.origin == O_TRANSITIVE and it.modules == ["core", "web"] and it.via == ["example:b:1"]
    assert it.location is None


def test_declared_literal_differs_from_resolved_is_unmapped(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(), "core/pom.xml": module_pom("core", dep("example", "lib", "1.1"))})
    it = only(build_inventory(p, [finding("CVE-1")], model({"core": [rdep(LIB, "1.2.3")]})))
    assert it.origin == O_UNMAPPED and it.reason == "core/pom.xml declares example:lib 1.1, Maven resolves 1.2.3"


def test_inherited_dependency_declared_in_project_parent(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(deps=dep("example", "lib", "1.2.3")), "core/pom.xml": module_pom("core")})
    it = only(build_inventory(p, [finding("CVE-1")], model({".": [rdep(LIB, "1.2.3")], "core": [rdep(LIB, "1.2.3")]})))
    assert it.origin == O_DIRECT_LITERAL and it.location.pom == "pom.xml" and it.modules == [".", "core"]


# ---------------------------------------------------------------------------- grouping
def test_two_modules_pinning_their_own_literals_give_two_items(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(modules=("core", "web")),
                         "core/pom.xml": module_pom("core", dep("example", "lib", "1.2.3")),
                         "web/pom.xml": module_pom("web", dep("example", "lib", "1.2.3"))})
    inv = build_inventory(p, [finding("CVE-1")], model({"core": [rdep(LIB, "1.2.3")], "web": [rdep(LIB, "1.2.3")]}))
    assert [(i.origin, i.location.pom, i.modules) for i in inv.items] == [
        (O_DIRECT_LITERAL, "core/pom.xml", ["core"]), (O_DIRECT_LITERAL, "web/pom.xml", ["web"])]
    assert all(i.finding_ids == ["CVE-1"] for i in inv.items)


def test_shared_property_location_is_one_item(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(modules=("core", "web"), props="    <lib.version>1.2.3</lib.version>\n"),
                         "core/pom.xml": module_pom("core", dep("example", "lib", "${lib.version}")),
                         "web/pom.xml": module_pom("web", dep("example", "lib", "${lib.version}"))})
    it = only(build_inventory(p, [finding("CVE-1")], model({"core": [rdep(LIB, "1.2.3")], "web": [rdep(LIB, "1.2.3")]})))
    assert it.origin == O_DIRECT_PROPERTY and it.modules == ["core", "web"]


def test_transitive_through_sibling_folds_into_editable_item(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(modules=("core", "web")),
                         "core/pom.xml": module_pom("core", dep("example", "lib", "1.2.3")),
                         "web/pom.xml": module_pom("web", dep("example", "core", "1.0.0"))})
    resolved = model({"core": [rdep(LIB, "1.2.3")],
                      "web": [rdep("example:core", "1.0.0"), rdep(LIB, "1.2.3", direct=False, path=["example:core:1.0.0"])]})
    it = only(build_inventory(p, [finding("CVE-1")], resolved))
    assert it.origin == O_DIRECT_LITERAL and it.location.pom == "core/pom.xml" and it.modules == ["core", "web"]
    assert "also resolved in web (transitive via example:core:1.0.0)" in it.reason


def test_unmapped_module_result_is_never_folded(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(modules=("core", "web")),
                         "core/pom.xml": module_pom("core", dep("example", "lib", "1.2.3")),
                         "web/pom.xml": module_pom("web", dep("example", "lib", "${base}-x"))})
    inv = build_inventory(p, [finding("CVE-1")], model({"core": [rdep(LIB, "1.2.3")], "web": [rdep(LIB, "1.2.3")]}))
    assert [(i.origin, i.modules) for i in inv.items] == [(O_DIRECT_LITERAL, ["core"]), (O_UNMAPPED, ["web"])]


def test_non_editable_majority_wins_and_others_in_reason(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(parent=True, modules=("a", "b", "c")),
                         "a/pom.xml": module_pom("a", dep("example", "lib")),
                         "b/pom.xml": module_pom("b", dep("example", "lib")),
                         "c/pom.xml": module_pom("c", dep("example", "x", "1"))})
    resolved = model({"a": [rdep(LIB, "1.2.3")], "b": [rdep(LIB, "1.2.3")],
                      "c": [rdep(LIB, "1.2.3", direct=False, path=["example:x:1"])]})
    it = only(build_inventory(p, [finding("CVE-1")], resolved))
    assert it.origin == O_PARENT_MANAGED and it.modules == ["a", "b", "c"]
    assert "also resolved in c (transitive via example:x:1)" in it.reason


# ---------------------------------------------------------------------------- not resolved
def test_version_mismatch_is_unmapped_with_facts(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(), "core/pom.xml": module_pom("core", dep("example", "lib", "1.2.3"))})
    it = only(build_inventory(p, [finding("CVE-1", version="1.0.0")], model({"core": [rdep(LIB, "1.2.3")]})))
    assert it.origin == O_UNMAPPED and it.reason == "IVAS reports 1.0.0, Maven resolves 1.2.3"
    assert it.version == "1.0.0"


def test_not_resolved_at_all(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(), "core/pom.xml": module_pom("core")})
    it = only(build_inventory(p, [finding("CVE-1")], model({"core": []})))
    assert it.origin == O_UNMAPPED
    assert it.reason == "IVAS reports example:lib 1.2.3, Maven does not resolve example:lib in any module"


def test_plugin_origin(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(), "core/pom.xml": module_pom("core")})
    plugins = {"core": [ResolvedDep(group_id="example", artifact_id="plugdep", version="9.9", direct=False,
                                    path=["org.apache.maven.plugins:maven-x-plugin:1"])]}
    it = only(build_inventory(p, [finding("CVE-1", ident="example:plugdep", version="9.9")],
                              model({"core": []}, plugins=plugins)))
    assert it.origin == O_PLUGIN and it.modules == ["core"] and "declared 9.9" in it.reason


def test_vendored_library_location_under_wrapper_folder(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(), "core/pom.xml": module_pom("core"),
                         "core/src/main/webapp/WEB-INF/lib/lib-1.2.3.jar": "jar"})
    f = finding("CVE-1", locations=["myapp/core/src/main/webapp/WEB-INF/lib/lib-1.2.3.jar", "myapp/core/pom.xml",
                                    "../escape.jar"])
    it = only(build_inventory(p, [f], model({"core": []})))
    assert it.origin == O_VENDORED and it.paths == ["myapp/core/src/main/webapp/WEB-INF/lib/lib-1.2.3.jar"]


def test_file_components_are_vendored(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(), "core/pom.xml": module_pom("core")})
    fs = [finding("CVE-9", ident="jquery", version="1.7.1", kind="file_component", locations=["static/js/jquery.js"]),
          finding("CVE-8", ident="jquery", version="1.7.1", kind="file_component_potential",
                  locations=["static/js/jq.min.js"])]
    it = only(build_inventory(p, fs, model({"core": []})))
    assert it.origin == O_VENDORED and it.finding_ids == ["CVE-8", "CVE-9"]
    assert it.paths == ["static/js/jq.min.js", "static/js/jquery.js"]


def test_non_maven_identifier_is_unmapped(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(), "core/pom.xml": module_pom("core")})
    it = only(build_inventory(p, [finding("CVE-1", ident="lodash")], model({"core": []})))
    assert it.origin == O_UNMAPPED and "not group:artifact" in it.reason


# ---------------------------------------------------------------------------- completeness / order
def test_incomplete_resolution_propagates(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(), "core/pom.xml": module_pom("core", dep("example", "lib", "1.2.3"))})
    resolved = model({"core": [rdep(LIB, "1.2.3")]}, complete=False)
    resolved.notes.append("module web (example:web): no dependency tree")
    inv = build_inventory(p, [finding("CVE-1")], resolved)
    assert not inv.complete and "module web (example:web): no dependency tree" in inv.notes


def test_unparsable_pom_makes_inventory_incomplete(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(modules=("core", "web")),
                         "core/pom.xml": module_pom("core", dep("example", "lib", "1.2.3")),
                         "web/pom.xml": "<project><unclosed>"})
    inv = build_inventory(p, [finding("CVE-1")], model({"core": [rdep(LIB, "1.2.3")]}))
    assert not inv.complete and any("web/pom.xml" in n for n in inv.notes)
    assert inv.items[0].origin == O_DIRECT_LITERAL


def test_deterministic_ordering(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(modules=("core", "web")),
                         "core/pom.xml": module_pom("core", dep("example", "lib", "1.2.3") + dep("example", "abc", "2")),
                         "web/pom.xml": module_pom("web", dep("example", "lib", "1.2.3"))})
    resolved = model({"core": [rdep(LIB, "1.2.3"), rdep("example:abc", "2")], "web": [rdep(LIB, "1.2.3")]})
    fs = [finding("CVE-3", ident="zzz:file", kind="file_component", version="1"), finding("CVE-1"),
          finding("CVE-2", ident="example:abc", version="2")]
    a = build_inventory(p, fs, resolved)
    b = build_inventory(p, list(reversed(fs)), resolved)
    assert [i.to_dict() for i in a.items] == [i.to_dict() for i in b.items]
    assert [(i.ga, i.location.pom if i.location else "") for i in a.items] == [
        ("example:abc", "core/pom.xml"), (LIB, "core/pom.xml"), (LIB, "web/pom.xml"), ("zzz:file", "")]


def test_project_poms_follow_relative_path_parent_outside_reactor(tmp_path):
    parent = root_pom(modules=()).replace("<artifactId>root</artifactId>", "<artifactId>root</artifactId>", 1)
    agg = ("<project>\n  <groupId>example</groupId>\n  <artifactId>agg</artifactId>\n  <version>1.0.0</version>\n"
           "  <packaging>pom</packaging>\n  <modules>\n    <module>core</module>\n  </modules>\n</project>\n")
    core = module_pom("core", dep("example", "lib", "${lib.version}")).replace(
        "  </parent>", "    <relativePath>../parent/pom.xml</relativePath>\n  </parent>")
    p = write(tmp_path, {"pom.xml": agg, "parent/pom.xml": parent.replace(
        "  <properties>\n", "  <properties>\n    <lib.version>1.2.3</lib.version>\n"), "core/pom.xml": core})
    pp = ProjectPoms(p)
    assert pp.reactor == [".", "core"] and pp.poms["core"].parent is pp.poms["parent"]
    it = only(build_inventory(p, [finding("CVE-1")], model({"core": [rdep(LIB, "1.2.3")]})))
    assert it.origin == O_DIRECT_PROPERTY and it.location.pom == "parent/pom.xml"


def test_patched_versions_normalized_per_finding(tmp_path):
    p = write(tmp_path, {"pom.xml": root_pom(), "core/pom.xml": module_pom("core", dep("example", "lib", "1.2.3"))})
    fs = [finding("CVE-1", patched="1.2.7.1,1.3.4.2"), finding("CVE-2", patched=" 1.2.9 ; "),
          finding("CVE-3", patched="1.2.7.1, 1.3.4.2"), finding("CVE-4", patched="[1.2.5,2.0)"), finding("CVE-5")]
    it = only(build_inventory(p, fs, model({".": [], "core": [rdep(LIB, "1.2.3")]})))
    # one entry per distinct finding hint (findings AND together); the versions inside one entry are alternatives
    assert it.patched_versions == ["1.2.7.1, 1.3.4.2", "1.2.9", "[1.2.5,2.0)"]
