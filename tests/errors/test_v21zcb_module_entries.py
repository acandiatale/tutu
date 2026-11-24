"""v2.1z-cb (docs/INTERFACES.md "v2.1z-cb"): in the error list a file-less error matches only an entry of the same
module; an occurrence in another module is a new entry carrying its module. Entries / errors without a module match
as before (older data keeps working). Errors with a file are not changed."""
from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.core.models import BuildError, BuildResult, ErrorKind
from sbmig.errors import ErrorList

MSG = ("Resolution of annotationProcessorPath dependencies failed: Cannot find version for annotation processor path "
       "'org.mapstruct:mapstruct-processor.jar'. The version needs to be either provided directly in the plugin "
       "configuration or via dependency management.")
L2 = "compile|build|resolution of annotationprocessorpath dependencies failed: cannot"


def mod_err(module: str | None) -> BuildError:
    return BuildError(kind=ErrorKind.COMPILE, message=MSG, file=None, module=module, goal="compiler:compile",
                      signature=L2 + "|L1", signature_generic=L2)


def file_err(module: str) -> BuildError:
    return BuildError(kind=ErrorKind.COMPILE, message="cannot find symbol", file=f"{module}/src/main/java/A.java",
                      line=3, module=module, signature="compile|cannot_find_symbol|W|L1",
                      signature_generic="compile|cannot_find_symbol|W")


def build(*errors, failed_module=None, modules_built=()) -> BuildResult:
    return BuildResult(ok=not errors, goal="package", command=["mvn"], exit_code=1 if errors else 0, duration_s=1.0,
                       log_path="/x.log", errors=list(errors), failed_module=failed_module,
                       modules_built=list(modules_built))


@pytest.fixture()
def el(tmp_path: Path, cfg) -> ErrorList:
    return ErrorList(tmp_path / "run", "run-1", project="demo", cfg=cfg, roots=[tmp_path / "run" / "project"])


def test_the_same_message_in_another_module_is_a_new_entry(el: ErrorList):
    el.add_build(build(mod_err("m1"), failed_module="m1"), "build")
    el.add_build(build(file_err("m1"), failed_module="m1"), "fix r1")        # m1's pom fixed: its sources compile
    first = el.entries[0]
    assert first.module == "m1" and first.status == "resolved"
    new = el.add_build(build(mod_err("m2"), failed_module="m2", modules_built=["m1"]), "fix r2")
    assert [(e.module, e.file, e.status) for e in new] == [("m2", None, "open")]
    assert first.status == "resolved" and "reappeared" not in (first.note or "")
    assert [e.module for e in el.entries if e.file is None] == ["m1", "m2"]


def test_the_same_message_for_two_modules_in_one_build_is_two_entries(el: ErrorList):
    new = el.add_build(build(mod_err("m1"), mod_err("m2")), "build")
    assert [e.module for e in new] == ["m1", "m2"]
    assert el.add_build(build(mod_err("m1"), mod_err("m2")), "fix r1") == []


def test_the_same_module_twice_is_one_entry_that_reappears(el: ErrorList):
    el.add_build(build(mod_err("m1"), failed_module="m1"), "build")
    el.add_build(build(file_err("m1"), failed_module="m1"), "fix r1")
    assert el.entries[0].status == "resolved"
    assert el.add_build(build(mod_err("m1"), failed_module="m1"), "fix r2") == []
    e = el.entries[0]
    assert e.status == "open" and "reappeared in fix r2" in e.note
    assert len([x for x in el.entries if x.file is None]) == 1


def test_entries_and_errors_without_a_module_match_as_before(el: ErrorList):
    el.add_build(build(mod_err(None)), "build")
    assert el.add_build(build(mod_err(None)), "fix r1") == []               # no module on either side
    assert el.add_build(build(mod_err("m1")), "fix r2") == []               # an older entry without a module
    assert len(el.entries) == 1


def test_an_error_without_a_module_matches_an_entry_that_has_one(el: ErrorList):
    el.add_build(build(mod_err("m1")), "build")
    assert el.add_build(build(mod_err(None)), "fix r1") == []
    assert len(el.entries) == 1 and el.entries[0].module == "m1"
