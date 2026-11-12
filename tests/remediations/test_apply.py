"""apply.py: line-based version text replacement, validation, verify_text_only, verify_resolution."""
from __future__ import annotations

import codecs
import os

import pytest

from sbmig.remediations import apply as A
from sbmig.remediations.models import (
    E_INSERT,
    K_DIRECT_VERSION,
    K_MANAGED_OVERRIDE,
    K_PROPERTY_VERSION,
    ModuleResolution,
    PackagedLib,
    Proposal,
    ResolvedDep,
    ResolvedModel,
    TextEdit,
)

POM = """<?xml version="1.0" encoding="UTF-8"?>
<project>
  <modelVersion>4.0.0</modelVersion>
  <groupId>example</groupId>
  <artifactId>app</artifactId>
  <version>1.0.0</version>
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


def _prop(pid, edits, ga="example:lib", frm="1.2.3", to="1.2.4", kind=K_DIRECT_VERSION):
    return Proposal(id=pid, round=1, ga=ga, from_version=frm, to_version=to, kind=kind, edits=edits)


def _write(path, text, newline="\n", encoding="utf-8", bom=b""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bom + text.replace("\n", newline).encode(encoding))
    return path


# ------------------------------------------------------------------------------------------------- apply


def test_apply_replaces_only_the_span(tmp_path):
    _write(tmp_path / "pom.xml", POM)
    res = A.apply_proposals(tmp_path, [_prop("p-1-1", [TextEdit("pom.xml", 14, "1.2.3", "1.2.4")])])
    assert res.applied == ["p-1-1"] and res.skipped == {}
    assert res.changed_files == ["pom.xml"]
    assert (tmp_path / "pom.xml").read_text() == POM.replace("<version>1.2.3</version>", "<version>1.2.4</version>")
    assert res.candidate_tree_hash == ""
    assert "--- a/pom.xml" in res.patch and "+++ b/pom.xml" in res.patch
    assert "-      <version>1.2.3</version>\n" in res.patch and "+      <version>1.2.4</version>\n" in res.patch


def test_apply_keeps_crlf_and_missing_trailing_newline(tmp_path):
    text = POM.rstrip("\n")
    p = _write(tmp_path / "pom.xml", text, newline="\r\n")
    before = p.read_bytes()
    A.apply_proposals(tmp_path, [_prop("p", [TextEdit("pom.xml", 8, "2.14.0", "2.14.3")])])
    after = p.read_bytes()
    assert after == before.replace(b"2.14.0", b"2.14.3")
    assert after.count(b"\r\n") == before.count(b"\r\n") and not after.endswith(b"\n")


def test_apply_keeps_bom_and_declared_encoding(tmp_path):
    p1 = _write(tmp_path / "a" / "pom.xml", POM, bom=codecs.BOM_UTF8)
    latin = POM.replace('encoding="UTF-8"', 'encoding="ISO-8859-1"').replace("<modelVersion>",
                                                                          "<!-- caf\u00e9 -->\n  <modelVersion>")
    p2 = _write(tmp_path / "b" / "pom.xml", latin, encoding="latin-1")
    b1, b2 = p1.read_bytes(), p2.read_bytes()
    res = A.apply_proposals(tmp_path, [
        _prop("p1", [TextEdit("a/pom.xml", 14, "1.2.3", "1.2.4")]),
        _prop("p2", [TextEdit("b/pom.xml", 15, "1.2.3", "1.2.4")]),
    ])
    assert res.applied == ["p1", "p2"], res.skipped
    assert p1.read_bytes() == b1.replace(b"1.2.3", b"1.2.4") and p1.read_bytes().startswith(codecs.BOM_UTF8)
    assert p2.read_bytes() == b2.replace(b"1.2.3", b"1.2.4") and b"caf\xe9" in p2.read_bytes()


def test_apply_utf16_round_trip(tmp_path):
    text = POM.replace('encoding="UTF-8"', 'encoding="UTF-16"')
    p = _write(tmp_path / "pom.xml", text, encoding="utf-16-le", bom=codecs.BOM_UTF16_LE)
    A.apply_proposals(tmp_path, [_prop("p", [TextEdit("pom.xml", 14, "1.2.3", "1.2.4")])])
    assert p.read_bytes() == codecs.BOM_UTF16_LE + text.replace("1.2.3", "1.2.4").encode("utf-16-le")


def test_exactly_once_rule(tmp_path):
    _write(tmp_path / "pom.xml", POM.replace("<version>1.2.3</version>", "<version>1.2.3</version><!--1.2.3-->"))
    res = A.apply_proposals(tmp_path, [_prop("p", [TextEdit("pom.xml", 14, "1.2.3", "1.2.4")])])
    assert res.applied == [] and "occurs 2 times" in res.skipped["p"]
    res = A.apply_proposals(tmp_path, [_prop("q", [TextEdit("pom.xml", 13, "1.2.3", "1.2.4")])])
    assert "occurs 0 times" in res.skipped["q"]
    res = A.apply_proposals(tmp_path, [_prop("r", [TextEdit("pom.xml", 999, "1.2.3", "1.2.4")])])
    assert "does not exist" in res.skipped["r"]


@pytest.mark.parametrize("pom", ["../pom.xml", "/etc/pom.xml", "a/../pom.xml", "a\\pom.xml", "", "missing/pom.xml",
                                 "C:/pom.xml"])
def test_path_escapes_rejected(tmp_path, pom):
    proj = tmp_path / "proj"
    _write(proj / "pom.xml", POM)
    _write(tmp_path / "pom.xml", POM)
    res = A.apply_proposals(proj, [_prop("p", [TextEdit(pom, 14, "1.2.3", "1.2.4")])])
    assert res.applied == [] and "p" in res.skipped
    assert (tmp_path / "pom.xml").read_text() == POM


def test_symlink_escape_rejected(tmp_path):
    outside = _write(tmp_path / "outside" / "pom.xml", POM)
    proj = tmp_path / "proj"
    proj.mkdir()
    os.symlink(outside.parent, proj / "link")
    res = A.apply_proposals(proj, [_prop("p", [TextEdit("link/pom.xml", 14, "1.2.3", "1.2.4")])])
    assert "symbolic link" in res.skipped["p"]
    assert outside.read_text() == POM


@pytest.mark.parametrize("old,new", [("1.2.3", "<b>"), ("1.2.3", "1.2.4 "), ("1.2.3", '"1"'), ("1.2.3", "${x}"),
                                     ("1.2.3", "1&2"), ("", "1.2.4"), ("1.2.3", "1.2.3")])
def test_invalid_version_text_rejected(tmp_path, old, new):
    _write(tmp_path / "pom.xml", POM)
    res = A.apply_proposals(tmp_path, [_prop("p", [TextEdit("pom.xml", 14, old, new)])])
    assert res.applied == [] and "p" in res.skipped
    assert (tmp_path / "pom.xml").read_text() == POM


def test_atomic_skip_of_a_proposal_with_one_bad_edit(tmp_path):
    _write(tmp_path / "pom.xml", POM)
    bad = _prop("bad", [TextEdit("pom.xml", 8, "2.14.0", "2.14.3"), TextEdit("pom.xml", 14, "9.9.9", "9.9.10")])
    good = _prop("good", [TextEdit("pom.xml", 14, "1.2.3", "1.2.4")])
    res = A.apply_proposals(tmp_path, [bad, good])
    assert res.applied == ["good"] and "bad" in res.skipped
    text = (tmp_path / "pom.xml").read_text()
    assert "2.14.0" in text and "1.2.4" in text            # the bad proposal's valid edit was NOT applied


def test_conflicting_edits_on_one_line_skip_the_later_proposal(tmp_path):
    _write(tmp_path / "pom.xml", POM)
    a = _prop("a", [TextEdit("pom.xml", 14, "1.2.3", "1.2.4")])
    b = _prop("b", [TextEdit("pom.xml", 14, "1.2.3", "1.3.0")])
    same = _prop("c", [TextEdit("pom.xml", 14, "1.2.3", "1.2.4")])
    res = A.apply_proposals(tmp_path, [a, b, same])
    assert res.applied == ["a", "c"] and "conflicts" in res.skipped["b"]
    assert "<version>1.2.4</version>" in (tmp_path / "pom.xml").read_text()


def test_render_diff_is_pure(tmp_path):
    p = _write(tmp_path / "pom.xml", POM)
    diff = A.render_diff(tmp_path, [TextEdit("pom.xml", 8, "2.14.0", "2.14.3")])
    assert p.read_text() == POM
    assert diff.startswith("--- a/pom.xml\n+++ b/pom.xml\n")
    assert "-    <lib.version>2.14.0</lib.version>\n+    <lib.version>2.14.3</lib.version>\n" in diff
    with pytest.raises(A.EditError):
        A.render_diff(tmp_path, [TextEdit("pom.xml", 8, "nope", "2.14.3")])


def test_render_diff_marks_missing_trailing_newline(tmp_path):
    _write(tmp_path / "pom.xml", "<project><version>1.0</version></project>")
    diff = A.render_diff(tmp_path, [TextEdit("pom.xml", 1, "1.0", "1.1")])
    assert "\\ No newline at end of file\n" in diff


# ------------------------------------------------------------------------------------------------- verify_text_only


def _trees(tmp_path):
    orig, cand = tmp_path / "orig", tmp_path / "cand"
    for base in (orig, cand):
        _write(base / "pom.xml", POM)
        _write(base / "src" / "App.java", "class App {}\n")
    return orig, cand, ["pom.xml", "src/App.java"]


def test_verify_text_only_passes_for_the_planned_edit(tmp_path):
    orig, cand, paths = _trees(tmp_path)
    props = [_prop("p", [TextEdit("pom.xml", 14, "1.2.3", "1.2.4")])]
    A.apply_proposals(cand, props)
    assert A.verify_text_only(orig, cand, props, paths) == []


def test_verify_text_only_catches_an_extra_changed_line(tmp_path):
    orig, cand, paths = _trees(tmp_path)
    props = [_prop("p", [TextEdit("pom.xml", 14, "1.2.3", "1.2.4")])]
    A.apply_proposals(cand, props)
    p = cand / "pom.xml"
    p.write_text(p.read_text().replace("<version>1.0.0</version>", "<version>1.0.1</version>"))
    problems = A.verify_text_only(orig, cand, props, paths)
    assert any("more than the planned" in x for x in problems)


def test_verify_text_only_catches_a_changed_non_pom_file(tmp_path):
    orig, cand, paths = _trees(tmp_path)
    (cand / "src" / "App.java").write_text("class App { }\n")
    problems = A.verify_text_only(orig, cand, [], paths)
    assert problems == ["src/App.java: changed but no planned edit names it"]


def test_verify_text_only_catches_an_added_element(tmp_path):
    orig, cand, paths = _trees(tmp_path)
    props = [_prop("p", [TextEdit("pom.xml", 14, "1.2.3", "1.2.4")])]
    A.apply_proposals(cand, props)
    p = cand / "pom.xml"
    p.write_text(p.read_text().replace("</dependencies>", "  <dependency/>\n  </dependencies>"))
    assert A.verify_text_only(orig, cand, props, paths)


def test_verify_text_only_structure_check_flags_non_version_text():
    before = POM.encode()
    after = POM.replace("<artifactId>app</artifactId>", "<artifactId>apq</artifactId>").encode()
    assert any("<artifactId>" in x for x in A._structure_problems("pom.xml", before, after))
    after = POM.replace("</properties>", "<x/></properties>").encode()
    assert A._structure_problems("pom.xml", before, after) == ["pom.xml: XML element structure changed"]
    after = POM.replace("2.14.0", "2.14.3").encode()
    assert A._structure_problems("pom.xml", before, after) == []


def test_verify_text_only_catches_a_missing_file(tmp_path):
    orig, cand, paths = _trees(tmp_path)
    (cand / "src" / "App.java").unlink()
    assert A.verify_text_only(orig, cand, [], paths) == ["src/App.java: missing in the candidate"]
    assert "missing in the original" in A.verify_text_only(orig, cand, [], paths + ["extra.txt"])[0]


def test_verify_text_only_edit_outside_paths(tmp_path):
    orig, cand, _paths = _trees(tmp_path)
    props = [_prop("p", [TextEdit("pom.xml", 14, "1.2.3", "1.2.4")])]
    A.apply_proposals(cand, props)
    problems = A.verify_text_only(orig, cand, props, ["src/App.java"])
    assert problems == ["edited file pom.xml is not in the original file list"]


# ------------------------------------------------------------------------------------------------- verify_resolution


def _dep(ga, v, direct=True):
    g, a = ga.split(":")
    return ResolvedDep(group_id=g, artifact_id=a, version=v, direct=direct)


def _model(*deps, dir=".", complete=True):
    return ResolvedModel(modules=[ModuleResolution(dir=dir, group_id="example", artifact_id="app", version="1",
                                                   dependencies=list(deps))], complete=complete)


def test_verify_resolution_ok():
    p = _prop("p", [], to="1.2.4")
    before = _model(_dep("example:lib", "1.2.3"), _dep("example:t", "1.0", direct=False))
    after = _model(_dep("example:lib", "1.2.4"), _dep("example:t", "1.0", direct=False))
    assert A.verify_resolution([p], before, after, []) == []


def test_verify_resolution_still_old_and_not_planned():
    p = _prop("p", [], to="1.2.4")
    before = _model(_dep("example:lib", "1.2.3"))
    probs = A.verify_resolution([p], before, _model(_dep("example:lib", "1.2.3")), [])
    assert any("still resolves at 1.2.3" in x for x in probs)
    probs = A.verify_resolution([p], before, _model(_dep("example:lib", "1.2.5")), [])
    assert any("planned 1.2.4" in x for x in probs)


def test_verify_resolution_incomplete_after():
    p = _prop("p", [], to="1.2.4")
    after = _model(_dep("example:lib", "1.2.4"), complete=False)
    after.notes = ["module x unreadable"]
    probs = A.verify_resolution([p], _model(_dep("example:lib", "1.2.3")), after, [])
    assert any("incomplete" in x and not x.startswith("info:") for x in probs)


def test_verify_resolution_uncovered_direct_change_is_a_problem_transitive_is_info():
    p = _prop("p", [], to="1.2.4")
    before = _model(_dep("example:lib", "1.2.3"), _dep("example:other", "3.0"), _dep("example:t", "1.0", direct=False))
    after = _model(_dep("example:lib", "1.2.4"), _dep("example:other", "3.1"), _dep("example:t", "1.1", direct=False))
    probs = A.verify_resolution([p], before, after, [])
    blocking = [x for x in probs if not x.startswith("info:")]
    assert len(blocking) == 1 and "example:other" in blocking[0]
    assert any(x.startswith("info:") and "example:t" in x for x in probs)


def test_verify_resolution_property_companion_is_info():
    p = _prop("p", [], ga="example:core", frm="2.14.0", to="2.14.3", kind=K_PROPERTY_VERSION)
    before = _model(_dep("example:core", "2.14.0"), _dep("example:annotations", "2.14.0"))
    after = _model(_dep("example:core", "2.14.3"), _dep("example:annotations", "2.14.3"))
    probs = A.verify_resolution([p], before, after, [])
    assert probs and all(x.startswith("info:") for x in probs)


def test_verify_resolution_packaged_libs():
    p = _prop("p", [], to="1.2.4")
    before, after = _model(_dep("example:lib", "1.2.3")), _model(_dep("example:lib", "1.2.4"))
    ok = [PackagedLib("app/target/app.war", "WEB-INF/lib/lib-1.2.4.jar", "lib-1.2.4.jar", "example:lib", "1.2.4")]
    assert A.verify_resolution([p], before, after, ok) == []
    by_ga = [PackagedLib("app/target/app.war", "WEB-INF/lib/lib-1.2.3.jar", "lib-1.2.3.jar", "example:lib", "1.2.3")]
    assert any("still packages" in x for x in A.verify_resolution([p], before, after, by_ga))
    by_name = [PackagedLib("app/target/app.war", "WEB-INF/lib/lib-1.2.3.jar", "lib-1.2.3.jar")]
    assert any("still packages" in x for x in A.verify_resolution([p], before, after, by_name))
    other = [PackagedLib("app/target/app.war", "WEB-INF/lib/lib-1.2.3.jar", "lib-1.2.3.jar", "other:lib", "1.2.3")]
    assert A.verify_resolution([p], before, after, other) == []


# ------------------------------------------------------------------------------------------------- override inserts
DM = """<?xml version="1.0" encoding="UTF-8"?>
<project>
  <modelVersion>4.0.0</modelVersion>
  <properties>
    <lib.version>2.14.0</lib.version>
  </properties>
  <dependencyManagement>
    <dependencies>
      <dependency>
        <groupId>example</groupId>
        <artifactId>managed</artifactId>
        <version>3.0</version>
      </dependency>
    </dependencies>
  </dependencyManagement>
  <dependencies>
    <dependency>
      <groupId>example</groupId>
      <artifactId>lib</artifactId>
      <version>1.2.3</version>
    </dependency>
  </dependencies>
</project>
"""
ENTRY = ("      <dependency>\n        <groupId>example</groupId>\n        <artifactId>t</artifactId>\n"
         "        <version>1.1</version>\n      </dependency>\n")


def _override(pid, project, g="example", a="t", v="1.1"):
    return Proposal(id=pid, round=1, ga=f"{g}:{a}", from_version="1.0", to_version=v, kind=K_MANAGED_OVERRIDE,
                    edits=[A.locate_override(project, g, a, v)])


@pytest.mark.parametrize("newline,bom", [("\n", b""), ("\r\n", b""), ("\n", codecs.BOM_UTF8), ("\r\n", codecs.BOM_UTF8)])
def test_insert_keeps_line_endings_and_bom(tmp_path, newline, bom):
    p = _write(tmp_path / "pom.xml", DM, newline=newline, bom=bom)
    before = p.read_bytes()
    prop = _override("p-1-1", tmp_path)
    e = prop.edits[0]
    assert (e.line, e.anchor, e.op) == (14, "</dependencies>", E_INSERT)
    res = A.apply_proposals(tmp_path, [prop])
    assert res.applied == ["p-1-1"], res.skipped
    anchor = "    </dependencies>" + newline
    expected = before.replace(anchor.encode(), (ENTRY.replace("\n", newline) + anchor).encode(), 1)
    assert p.read_bytes() == expected and p.read_bytes().startswith(bom)


def test_render_override_shapes():
    assert A.render_override("example", "t", "1.1", "      ", "\n", False, "  ") == ENTRY
    sec = A.render_override("g", "a", "1", "\t", "\r\n", True, "\t")
    assert sec == ("\t<dependencyManagement>\r\n\t\t<dependencies>\r\n\t\t\t<dependency>\r\n\t\t\t\t<groupId>g</groupId>\r\n"
                   "\t\t\t\t<artifactId>a</artifactId>\r\n\t\t\t\t<version>1</version>\r\n\t\t\t</dependency>\r\n"
                   "\t\t</dependencies>\r\n\t</dependencyManagement>\r\n")
    for bad in (("g<", "a", "1"), ("g", "a b", "1"), ("g", "a", "${v}"), ("g", "a", '1"')):
        with pytest.raises(A.EditError):
            A.render_override(*bad, "  ", "\n", False, "  ")


@pytest.mark.parametrize("tamper", [
    lambda e: setattr(e, "new", e.new.replace("<version>1.1</version>", "<version>1.1</version><scope>test</scope>")),
    lambda e: setattr(e, "new", e.new + "      <!-- extra -->\n"),
    lambda e: setattr(e, "new", e.new.replace("example</groupId>", "other</groupId>")),
    lambda e: e.override.update(version="1.1 "),
    lambda e: e.override.update(extra="x"),
    lambda e: setattr(e, "old", "1.0"),
    lambda e: setattr(e, "pom", "sub/pom.xml"),
    lambda e: setattr(e, "op", "delete"),
])
def test_tampered_insert_is_rejected(tmp_path, tamper):
    p = _write(tmp_path / "pom.xml", DM)
    _write(tmp_path / "sub" / "pom.xml", DM)
    prop = _override("p-1-1", tmp_path)
    tamper(prop.edits[0])
    res = A.apply_proposals(tmp_path, [prop])
    assert res.applied == [] and "p-1-1" in res.skipped
    assert p.read_text() == DM and (tmp_path / "sub" / "pom.xml").read_text() == DM


def test_insert_with_moved_anchor_is_skipped(tmp_path):
    p = _write(tmp_path / "pom.xml", DM)
    prop = _override("p-1-1", tmp_path)
    p.write_text(DM.replace("<modelVersion>4.0.0</modelVersion>\n", "<modelVersion>4.0.0</modelVersion>\n  <!-- x -->\n"))
    changed = p.read_text()
    res = A.apply_proposals(tmp_path, [prop])
    assert res.applied == [] and "anchor line changed" in res.skipped["p-1-1"]
    assert p.read_text() == changed


def test_insert_needs_a_managed_override_proposal_and_no_existing_entry(tmp_path):
    _write(tmp_path / "pom.xml", DM)
    prop = _override("p-1-1", tmp_path)
    prop.kind = K_DIRECT_VERSION
    assert "managed_override proposal only" in A.apply_proposals(tmp_path, [prop]).skipped["p-1-1"]
    with pytest.raises(A.EditError, match="already has"):
        A.locate_override(tmp_path, "example", "managed", "3.1")
    forged = _override("p-1-2", tmp_path)
    forged.edits[0].override["artifactId"] = "managed"
    forged.edits[0].new = forged.edits[0].new.replace("<artifactId>t<", "<artifactId>managed<")
    assert "already has" in A.apply_proposals(tmp_path, [forged]).skipped["p-1-2"]


def test_combined_replace_and_inserts_in_one_pom(tmp_path):
    orig, cand = tmp_path / "orig", tmp_path / "cand"
    for base in (orig, cand):
        _write(base / "pom.xml", DM)
    rep = _prop("p-1-1", [TextEdit("pom.xml", 20, "1.2.3", "1.2.4")])
    o2, o3 = _override("p-1-2", cand), _override("p-1-3", cand, a="u", v="2.1")
    assert o2.edits[0].line == o3.edits[0].line == 14                    # both computed against the original file
    res = A.apply_proposals(cand, [o3, rep, o2])
    assert sorted(res.applied) == ["p-1-1", "p-1-2", "p-1-3"] and not res.skipped
    text = (cand / "pom.xml").read_text()
    assert "<version>1.2.4</version>" in text and text.index("<artifactId>t<") < text.index("<artifactId>u<")
    assert A.verify_text_only(orig, cand, [rep, o2, o3], ["pom.xml"]) == []
    # a proposal set missing one insert: the candidate has more than planned
    assert A.verify_text_only(orig, cand, [rep, o2], ["pom.xml"])


def test_two_section_inserts_become_one_section(tmp_path):
    orig, cand = tmp_path / "orig", tmp_path / "cand"
    for base in (orig, cand):
        _write(base / "pom.xml", POM, newline="\r\n")
    a, b = _override("p-1-1", cand), _override("p-1-2", cand, a="u", v="2.1")
    assert a.edits[0].anchor == "<dependencies>" and a.edits[0].new.startswith("  <dependencyManagement>\r\n")
    assert A.apply_proposals(cand, [a, b]).applied == ["p-1-1", "p-1-2"]
    text = (cand / "pom.xml").read_bytes().decode()
    assert text.count("<dependencyManagement>") == 1 and text.count("<dependency>") == 3
    assert A.verify_text_only(orig, cand, [a, b], ["pom.xml"]) == []
    # only one of them approved: its own section, exactly its diff
    cand2 = tmp_path / "cand2"
    _write(cand2 / "pom.xml", POM, newline="\r\n")
    A.apply_proposals(cand2, [b])
    assert A.verify_text_only(orig, cand2, [b], ["pom.xml"]) == []
    assert A.render_diff(orig, b.edits).replace("\r\n", "\n") == A._unified(
        "pom.xml", (orig / "pom.xml").read_text(), (cand2 / "pom.xml").read_text()).replace("\r\n", "\n")


def test_verify_text_only_catches_one_extra_inserted_element(tmp_path):
    orig, cand = tmp_path / "orig", tmp_path / "cand"
    for base in (orig, cand):
        _write(base / "pom.xml", DM)
    prop = _override("p-1-1", cand)
    A.apply_proposals(cand, [prop])
    assert A.verify_text_only(orig, cand, [prop], ["pom.xml"]) == []
    p = cand / "pom.xml"
    p.write_text(p.read_text().replace("<version>1.1</version>\n", "<version>1.1</version>\n        <scope>test</scope>\n"))
    problems = A.verify_text_only(orig, cand, [prop], ["pom.xml"])
    assert any("more than the planned" in x for x in problems)
    # the structure check alone also names it (bytes aside)
    ovs = A._overrides(prop.edits)
    assert any("not exactly groupId / artifactId / version" in x
               for x in A._structure_problems("pom.xml", (orig / "pom.xml").read_bytes(), p.read_bytes(), ovs))
    extra = (orig / "pom.xml").read_text().replace("    </dependencies>\n  </dependencyManagement>",
                                                   ENTRY + ENTRY.replace(">t<", ">x<") + "    </dependencies>\n"
                                                   "  </dependencyManagement>")
    assert A._structure_problems("pom.xml", (orig / "pom.xml").read_bytes(), extra.encode(), ovs) == [
        "pom.xml: XML element structure changed"]


def test_verify_resolution_for_an_override():
    p = Proposal(id="p-1-1", round=1, ga="example:t", from_version="1.0", to_version="1.1", kind=K_MANAGED_OVERRIDE)
    before = ResolvedModel(modules=[
        ModuleResolution(dir="a", group_id="e", artifact_id="a", version="1", dependencies=[_dep("example:t", "1.0", False)]),
        ModuleResolution(dir="b", group_id="e", artifact_id="b", version="1", dependencies=[_dep("example:x", "3.0")])])
    good = ResolvedModel(modules=[
        ModuleResolution(dir="a", group_id="e", artifact_id="a", version="1", dependencies=[_dep("example:t", "1.1", False)]),
        ModuleResolution(dir="b", group_id="e", artifact_id="b", version="1", dependencies=[_dep("example:x", "3.0")])])
    assert A.verify_resolution([p], before, good, []) == []
    still = ResolvedModel.from_dict(before.to_dict())
    assert any("still resolves at 1.0" in x for x in A.verify_resolution([p], before, still, []))
    moved = ResolvedModel.from_dict(good.to_dict())
    moved.modules[1].dependencies = [_dep("example:x", "3.1")]
    assert any("example:x" in x and not x.startswith("info:") for x in A.verify_resolution([p], before, moved, []))


# ------------------------------------------------------------------------------------------------- verify: module scope
LOG = "com.example.logging:log-api"


def _modules(**mods):
    return ResolvedModel(modules=[ModuleResolution(dir=d.replace("_", "-"), group_id="example", artifact_id=d,
                                                   version="1", dependencies=list(deps)) for d, deps in mods.items()])


def _company(after_domain="2.25.5"):
    before = _modules(app_domain=[_dep(LOG, "2.25.4")], app_service=[_dep(LOG, "2.25.4")],
                      app_store=[_dep(LOG, "2.25.4")])
    after = _modules(app_domain=[_dep(LOG, after_domain)], app_service=[_dep(LOG, "2.25.4")],
                     app_store=[_dep(LOG, "2.25.4")])
    return before, after


def _scoped(modules=("app-domain",), also=("app-service", "app-store")):
    p = _prop("p-1-1", [], ga=LOG, frm="2.25.4", to="2.25.5")
    p.modules, p.also_modules = list(modules), list(also)
    return p


def test_verify_scoped_proposal_leaves_other_origins_to_the_next_round():
    before, after = _company()
    lines = A.verify_resolution([_scoped()], before, after, [])
    assert [x for x in lines if not x.startswith("info:")] == []
    assert lines == [
        f"info: {LOG} in module app-service still resolves at 2.25.4 — not governed by the edited version text "
        "(p-1-1); left for the next round",
        f"info: {LOG} in module app-store still resolves at 2.25.4 — not governed by the edited version text "
        "(p-1-1); left for the next round"]
    # a non-governed module that followed the edit: nothing to say
    moved = _modules(app_domain=[_dep(LOG, "2.25.5")], app_service=[_dep(LOG, "2.25.5")], app_store=[_dep(LOG, "2.25.4")])
    assert len(A.verify_resolution([_scoped()], before, moved, [])) == 1


def test_verify_scoped_governed_module_not_moved_is_a_problem():
    before, after = _company(after_domain="2.25.4")
    probs = [x for x in A.verify_resolution([_scoped()], before, after, []) if not x.startswith("info:")]
    assert probs == [f"{LOG} still resolves at 2.25.4 (p-1-1)",
                     f"{LOG} in module app-domain resolves at 2.25.4, planned 2.25.5 (p-1-1)"]
    gone = _modules(app_service=[_dep(LOG, "2.25.4")], app_store=[_dep(LOG, "2.25.4")])
    probs = [x for x in A.verify_resolution([_scoped()], before, gone, []) if not x.startswith("info:")]
    assert probs == ["module app-domain is missing from the resolution after the change (p-1-1)"]


def test_verify_unscoped_proposal_keeps_the_legacy_rule():
    # the company run's shape: the edited module (here app-api) moved, three modules reached through another origin did not
    before = _modules(app_api=[_dep(LOG, "2.25.4")], app_domain=[_dep(LOG, "2.25.4")],
                      app_service=[_dep(LOG, "2.25.4")], app_store=[_dep(LOG, "2.25.4")])
    after = _modules(app_api=[_dep(LOG, "2.25.5")], app_domain=[_dep(LOG, "2.25.4")],
                     app_service=[_dep(LOG, "2.25.4")], app_store=[_dep(LOG, "2.25.4")])
    assert A.verify_resolution([_scoped(modules=(), also=())], before, after, []) == [
        f"{LOG} still resolves at 2.25.4 (p-1-1)",
        f"{LOG} in module app-domain resolves at 2.25.4, planned 2.25.5 (p-1-1)",
        f"{LOG} in module app-service resolves at 2.25.4, planned 2.25.5 (p-1-1)",
        f"{LOG} in module app-store resolves at 2.25.4, planned 2.25.5 (p-1-1)"]
    # the same run with the scope the inventory now records: no problem, three info lines
    scoped = _scoped(modules=("app-api",), also=("app-domain", "app-service", "app-store"))
    lines = A.verify_resolution([scoped], before, after, [])
    assert len(lines) == 3 and all(x.startswith("info:") for x in lines)


def test_verify_old_plan_json_without_scope_loads_and_uses_the_legacy_rule():
    data = _scoped().to_dict()
    data.pop("modules")
    data.pop("also_modules")
    p = Proposal.from_dict(data)
    assert p.modules == [] and p.also_modules == []
    before, after = _company()
    assert [x for x in A.verify_resolution([p], before, after, []) if not x.startswith("info:")] == [
        f"{LOG} still resolves at 2.25.4 (p-1-1)",
        f"{LOG} in module app-service resolves at 2.25.4, planned 2.25.5 (p-1-1)",
        f"{LOG} in module app-store resolves at 2.25.4, planned 2.25.5 (p-1-1)"]


def test_verify_scoped_packaged_jar_by_packaging_module():
    before, after = _company()
    p = _scoped()

    def lib(artifact):
        return [PackagedLib(artifact, "WEB-INF/lib/log-api-2.25.4.jar", "log-api-2.25.4.jar", LOG, "2.25.4")]
    blocking = [x for x in A.verify_resolution([p], before, after, lib("app-domain/target/app-domain.war"))
                if not x.startswith("info:")]
    assert blocking == [f"app-domain/target/app-domain.war still packages WEB-INF/lib/log-api-2.25.4.jar "
                        f"({LOG}:2.25.4, p-1-1)"]
    lines = A.verify_resolution([p], before, after, lib("app-service\\target\\app-service.war"))
    assert all(x.startswith("info:") for x in lines)
    assert (f"info: app-service\\target\\app-service.war still packages WEB-INF/lib/log-api-2.25.4.jar "
            f"({LOG}:2.25.4) — its module is not governed by p-1-1") in lines
    # the packaging module cannot be derived: a problem, as before
    assert any(not x.startswith("info:") and "still packages" in x
               for x in A.verify_resolution([p], before, after, lib("dist/app.war")))
    # root module: target/x.jar is module "."
    assert any(not x.startswith("info:") for x in
               A.verify_resolution([_scoped(modules=(".",), also=())], before, after, lib("target/app.jar")))
    assert A._packaging_module("target/app.jar") == "." and A._packaging_module("a/b/target/x.war") == "a/b"
    assert A._packaging_module("dist/app.war") is None
