"""v2.1z-h (company run 2026-11-03): the input held `boot/bin/` — a whole copy of the boot module (src, .project, pom.xml
with the same coordinates); the pom scan read it as a project pom, the structure review registered it as an "unlisted
module" and Maven refused the reactor ("is duplicated in the reactor"). `stray_pom_copies` drops such UNLISTED duplicates
from `parse_poms` (one warning each), `find_stray_pom_copies` gives the intake the list, `Workspace.ignored_dirs` prunes
their directories from every scan, and the review's invariant check restores an edit that lists one."""
from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.analyze.aggregator import plan_aggregator
from sbmig.analyze.analyzer import PathView
from sbmig.analyze.pom_parser import find_stray_pom_copies, parse_poms, stray_pom_copies
from sbmig.core.workspace import Workspace
from sbmig.review.inputs import invariant_violations, module_lines, raw_poms, reactor_duplicate_lines

from .helpers import pom

JAVA = "package com.example.boot;\npublic class App {}\n"
ROOT = pom("app", packaging="pom", modules=("boot",))
BOOT = pom("app-boot", parent=("com.acme", "app", "1.0.0"), packaging="jar")
COPY_FILES = {"boot/bin/pom.xml": BOOT, "boot/bin/.project": "<projectDescription/>",
              "boot/bin/src/main/java/com/example/boot/App.java": JAVA}


def _tree(root: Path, files: dict[str, str]) -> PathView:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return PathView(root)


def _paths(poms) -> list[str]:
    return sorted(p.path for p in poms)


# ---------------------------------------------------------------------------------------------------- the rule
def test_nested_copy_is_dropped_with_a_warning(tmp_path):
    ws = _tree(tmp_path, {"pom.xml": ROOT, "boot/pom.xml": BOOT, "boot/src/main/java/com/example/boot/App.java": JAVA,
                          **COPY_FILES})
    poms, warnings = parse_poms(ws)
    assert _paths(poms) == ["boot/pom.xml", "pom.xml"]
    assert warnings == [("pom ignored (stray copy): `boot/bin/pom.xml` has the same coordinates as `boot/pom.xml` "
                         "(`com.acme:app-boot`), lies inside the directory of `boot/pom.xml` and no pom lists it as a "
                         "module — not a module")]
    strays = find_stray_pom_copies(ws)
    assert [(s.path, s.duplicate_of, s.coordinates, s.nested, s.dir) for s in strays] == \
        [("boot/bin/pom.xml", "boot/pom.xml", "com.acme:app-boot", True, "boot/bin")]


def test_unlisted_duplicate_beside_a_listed_module_is_dropped_too(tmp_path):
    ws = _tree(tmp_path, {"pom.xml": ROOT, "boot/pom.xml": BOOT, "backup/boot/pom.xml": BOOT})
    poms, warnings = parse_poms(ws)
    assert _paths(poms) == ["boot/pom.xml", "pom.xml"]
    [s] = find_stray_pom_copies(ws)
    assert (s.path, s.duplicate_of, s.nested) == ("backup/boot/pom.xml", "boot/pom.xml", False)
    assert "lies beside the module `boot/pom.xml`" in warnings[0]


def test_two_unlisted_siblings_in_a_rootless_tree_are_left_alone(tmp_path):
    # no root pom, nothing lists either: the aggregator planning / Maven decide — not a "copy" by this rule
    ws = _tree(tmp_path, {"a/pom.xml": BOOT, "b/pom.xml": BOOT})
    poms, warnings = parse_poms(ws)
    assert _paths(poms) == ["a/pom.xml", "b/pom.xml"] and warnings == []
    assert find_stray_pom_copies(ws) == []


def test_a_listed_duplicate_is_never_dropped_here(tmp_path):
    # both listed: Maven's "duplicated in the reactor" → `reactor_duplicate` (handed over), not a stray copy
    ws = _tree(tmp_path, {"pom.xml": pom("app", packaging="pom", modules=("boot", "boot/bin")), "boot/pom.xml": BOOT,
                          **COPY_FILES})
    poms, warnings = parse_poms(ws)
    assert _paths(poms) == ["boot/bin/pom.xml", "boot/pom.xml", "pom.xml"] and warnings == []


def test_a_nested_pom_with_its_own_coordinates_stays(tmp_path):
    ws = _tree(tmp_path, {"pom.xml": ROOT, "boot/pom.xml": BOOT,
                          "boot/tools/pom.xml": pom("app-tools", parent=("com.acme", "app", "1.0.0"))})
    poms, warnings = parse_poms(ws)
    assert _paths(poms) == ["boot/pom.xml", "boot/tools/pom.xml", "pom.xml"] and warnings == []


def test_the_rule_on_raw_poms_without_groups():
    from sbmig.analyze.pom_parser import parse_pom_text

    raws = [parse_pom_text(pom("app", group=None, packaging="pom", modules=("boot",)), "pom.xml"),
            parse_pom_text(pom("app-boot", group=None), "boot/pom.xml"),
            parse_pom_text(pom("app-boot", group=None), "boot/bin/pom.xml")]
    [s] = stray_pom_copies(raws)
    assert (s.path, s.coordinates) == ("boot/bin/pom.xml", ":app-boot")


# ---------------------------------------------------------------------------------------- consumers agree
def test_rootless_tree_plans_the_aggregator_without_the_copy(tmp_path):
    ws = _tree(tmp_path, {"boot/pom.xml": pom("app-boot", packaging="war"), **COPY_FILES})
    plan = plan_aggregator(ws)
    assert plan is not None and plan.modules == ["boot"] and plan.excluded == []


def test_review_module_lines_do_not_call_the_copy_an_unlisted_module(tmp_path):
    ws = _tree(tmp_path, {"pom.xml": ROOT, "boot/pom.xml": BOOT, **COPY_FILES})
    poms, _ = parse_poms(ws)
    lines = module_lines(poms, ws)
    assert not any(ln.startswith("Poms no aggregator lists") for ln in lines)
    assert not any("boot/bin" in ln for ln in lines)


def test_an_edit_listing_the_copy_is_a_reactor_duplicate_violation(tmp_path, cfg):
    ws = _tree(tmp_path, {"pom.xml": ROOT, "boot/pom.xml": BOOT, **COPY_FILES})
    before_poms, _ = parse_poms(ws)
    before = raw_poms(ws, before_poms)
    (tmp_path / "pom.xml").write_text(pom("app", packaging="pom", modules=("boot", "boot/bin")), encoding="utf-8")
    after_poms, _ = parse_poms(ws)                      # listed now → parsed → two reactor poms with one g:a
    out = reactor_duplicate_lines(before_poms, after_poms, ["pom.xml"])
    assert out == [("`pom.xml`: the reactor now holds 2 projects with the coordinates `com.acme:app-boot` "
                    "(`boot/bin/pom.xml`, `boot/pom.xml`) — Maven refuses that (\"is duplicated in the reactor\"); a copy "
                    "of a module (an IDE output / backup folder) is not a module: leave it unlisted")]
    # through the review's invariant check (what restores the edit)
    assert out[0] in invariant_violations(cfg, ["pom.xml"], before_poms, before, after_poms, raw_poms(ws, after_poms),
                                          None, [])
    # a pair that existed before the edit is not the review's doing
    assert reactor_duplicate_lines(after_poms, after_poms, ["pom.xml"]) == []


# ---------------------------------------------------------------------------------------- ignored_dirs
def test_workspace_scans_prune_the_ignored_directories(tmp_path):
    src = tmp_path / "src"
    _tree(src, {"pom.xml": ROOT, "boot/pom.xml": BOOT, "boot/src/main/java/com/example/boot/App.java": JAVA,
                **COPY_FILES})
    ws = Workspace.from_input(src, tmp_path / "out", run_id="t")
    assert "boot/bin/src/main/java/com/example/boot/App.java" in ws.java_files()
    ws.ignored_dirs.add("boot/bin")
    assert ws.is_ignored("boot/bin") and ws.is_ignored("boot/bin/pom.xml") and not ws.is_ignored("boot/binary")
    assert ws.java_files() == ["boot/src/main/java/com/example/boot/App.java"]
    assert ws.pom_files() == ["boot/pom.xml", "pom.xml"]
    assert ws.glob("**/App.java") == ["boot/src/main/java/com/example/boot/App.java"]      # the rglob branch too
    assert ws.exists("boot/bin/pom.xml")                                                 # the files are untouched
    view = PathView(src)
    view.ignored_dirs.add("boot/bin")
    assert view.java_files() == ["boot/src/main/java/com/example/boot/App.java"]


@pytest.mark.parametrize("rel", ["boot/bin", "boot/bin/"])
def test_find_pom_files_honours_ignored_dirs(tmp_path, rel):
    ws = _tree(tmp_path, {"pom.xml": ROOT, "boot/pom.xml": BOOT, "boot/bin/pom.xml": pom("other")})
    ws.ignored_dirs.add(rel.rstrip("/"))
    poms, _ = parse_poms(ws)
    assert _paths(poms) == ["boot/pom.xml", "pom.xml"]
