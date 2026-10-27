"""Company run 2026-10-27: a resources-filtering failure (`plugin_execution`, goal resources:resources) had no build
stage, so the compile errors its correct pom fix uncovered counted as "earlier errors that hide the target" and the fix
was reverted three times. A plugin execution failure now has a stage by its goal: process-resources comes BEFORE
compile, so errors the fix uncovers downstream are progress, not a regression."""
from __future__ import annotations

from sbmig.core.models import BuildError, DecisionKind, ErrorKind, Phase
from sbmig.errors.entries import error_stage, stage_of

from .fakes import FakeBuildRunner, FakeFixer, bad_build, compile_error, write_script
from .test_fixloop import make_loop

POM = "pom.xml"
JAVA = ["src/main/java/com/x/A.java", "src/main/java/com/x/B.java", "src/main/java/com/x/C.java"]


def filtering_error() -> BuildError:
    return BuildError(kind=ErrorKind.PLUGIN_EXECUTION, module="demo", goal="resources:resources",
                      message="filtering src/main/resources/static/img/.dm/.items.dmdb to target/classes/... failed "
                              "with MalformedInputException: Input length = 1",
                      signature="plugin_execution|resources:resources|filtering failed with malformedinputexception",
                      signature_generic="plugin_execution|resources:resources|filtering failed with malformedinputexception")


def test_plugin_execution_stage_by_goal():
    assert stage_of(ErrorKind.PLUGIN_EXECUTION, "resources:resources") == 2 < stage_of(ErrorKind.COMPILE)
    assert stage_of(ErrorKind.PLUGIN_EXECUTION, "war:war") == 5 > stage_of(ErrorKind.TEST_COMPILE)
    assert stage_of(ErrorKind.PLUGIN_EXECUTION, None, "plugin_execution|resources:testResources|x") == 2
    assert error_stage(filtering_error()) == 2 and error_stage(compile_error()) == 3


def test_resources_fix_that_uncovers_compile_errors_is_improved_not_reverted(cfg, ws, profile):
    ws.write(POM, "<project><build/></project>\n")
    for f in JAVA:
        ws.write(f, "class X { broken }\n")

    def build(w, call):
        if "nonFilteredFileExtension" not in w.read(POM):
            return bad_build(filtering_error())                       # the filter stops the build before compile
        return bad_build(*[compile_error(f) for f in JAVA])           # now javac runs and reports three errors

    fixed = "<project><build><plugins><plugin><artifactId>maven-resources-plugin</artifactId><configuration>" \
            "<nonFilteredFileExtensions><nonFilteredFileExtension>dmdb</nonFilteredFileExtension>" \
            "</nonFilteredFileExtensions></configuration></plugin></plugins></build></project>\n"
    fixer = FakeFixer(script_fn=lambda w, c, ctx: write_script({POM: fixed}, analysis="dmdb is binary: exclude it from filtering"))
    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build), fixer=fixer)
    pr = loop.run(Phase.VERIFY, "package", 2)
    att = next(a for a in pr.attempts if a.decision.kind == DecisionKind.SCRIPT)
    assert att.outcome == "improved", (att.notes, att.apply_errors, att.script_result)
    assert "nonFilteredFileExtension" in ws.read(POM)                # the fix stayed in the tree
    assert not any("regressed" in n for n in att.notes)
