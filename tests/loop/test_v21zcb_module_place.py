"""v2.1z-cb (docs/INTERFACES.md "v2.1z-cb"): the same error in another module is another error.

Maven stops at the first failing module, so a module-level error (maven-compiler-plugin's `Cannot find version for
annotation processor path`, file None, module set) shows in one module per build. The place of an error is its file,
else `@<module>`, else ''; the stall / oscillation check and the scoring compare (place, signature).

The error shape below is what `sbmig.build.log_parser.parse_maven_log` gives for the real Maven 3.9 line
(out/scratch/module-place/raw/mvn1.log): kind compile, file None, module 'm1', goal compiler:compile, the module name
nowhere in the signature."""
from __future__ import annotations

from sbmig.core.models import BlockReason, BuildError, ErrorKind, Phase
from sbmig.loop import fixloop
from sbmig.loop.fixloop import _pair

from .fakes import FakeFixer, FakeKB, bad_build, journal_events, ok_build, write_script
from .test_v21v_y_prompt_facts import make_loop

MSG = ("Resolution of annotationProcessorPath dependencies failed: Cannot find version for annotation processor path "
       "'org.mapstruct:mapstruct-processor.jar'. The version needs to be either provided directly in the plugin "
       "configuration or via dependency management.")
SIG = ("compile|build|resolution of annotationprocessorpath dependencies failed: cannot find version for annotation "
       "processor path 'org.mapstruct:mapstruct-processor.jar'. the versio")
L2 = "compile|build|resolution of annotationprocessorpath dependencies failed: cannot"
OTHER_SIG = "compile|cannot_find_symbol|Widget"           # one L1 signature for the file errors of every module
OTHER_L2 = "compile|cannot_find_symbol|Widget"
NOVER = "<project><!-- processor path without version --></project>\n"
FIXED = "<project><!-- processor path with version 1.6.3 --></project>\n"


def pom(m: str) -> str:
    return f"{m}/pom.xml"


def src(m: str) -> str:
    return f"{m}/src/main/java/com/example/{m}/Widget.java"


def mod_err(m: str) -> BuildError:
    return BuildError(kind=ErrorKind.COMPILE, message=MSG, file=None, module=m, goal="compiler:compile",
                      signature=SIG, signature_generic=L2)


def file_err(path: str, module: str, sig: str = OTHER_SIG, l2: str = OTHER_L2) -> BuildError:
    return BuildError(kind=ErrorKind.COMPILE, message="cannot find symbol Widget", file=path, module=module, line=3,
                      signature=sig, signature_generic=l2)


class PerError(FakeFixer):
    """Writes the fix for the first error of the case: a module error → that module's pom with a version; a file
    error → that file without `broken`. Records the (module, file) of every call."""

    def __init__(self, module_fix=None, file_fix=None):
        super().__init__()
        self.asked: list[tuple] = []
        self.module_fix = module_fix or (lambda w, e, n: {pom(e.module): FIXED})
        self.file_fix = file_fix or (lambda w, e, n: {e.file: f"class Widget {{ /* fixed {n} */ }}\n"})

    def propose_script(self, ws, cluster, ctx):
        self.contexts.append(ctx)
        e = cluster.errors[0]
        self.asked.append((e.module, e.file))
        n = len(self.asked)
        files = self.file_fix(ws, e, n) if e.file else self.module_fix(ws, e, n)
        return write_script(files)


def events(ws, name: str) -> list[dict]:
    return [e for e in journal_events(ws) if e.get("event") == name]


def start(ws, modules=("m1", "m2", "m3"), broken=("m1", "m2")) -> None:
    for m in modules:
        ws.write(pom(m), NOVER)
        ws.write(src(m), "class Widget { broken }\n" if m in broken else "class Widget { }\n")
    ws.commit("as-is")


def real_run_build(w, call):
    """The real run's order: m1's pom → m1's sources → m2's pom → m2's sources → m3's pom → success."""
    for m in ("m1", "m2", "m3"):
        if w.exists(pom(m)) and "without version" in w.read(pom(m)):
            return bad_build(mod_err(m))
        if w.exists(src(m)) and "broken" in w.read(src(m)):
            return bad_build(file_err(src(m), m))
    return ok_build()


# ------------------------------------------------------------------------------------------------ the place
def test_the_place_is_the_file_else_the_module_else_empty():
    assert fixloop._place(file_err("m1\\src\\A.java", "m1")) == "m1/src/A.java"
    assert fixloop._place(mod_err("m2")) == "@m2"
    assert fixloop._place(BuildError(kind=ErrorKind.UNKNOWN, message="x", signature="s")) == ""
    assert _pair(mod_err("m1")) == ("@m1", SIG) and _pair(mod_err("m1")) != _pair(mod_err("m2"))


# ------------------------------------------------------------------------------------------------ the real run
def test_the_real_runs_sequence_is_progress_not_oscillation(cfg, ws):
    start(ws)
    fixer = PerError()
    loop = make_loop(cfg, ws, fixer, build=real_run_build, kb=FakeKB())
    pr = loop.run(Phase.VERIFY, "package", 10)
    assert not events(ws, "oscillation")
    assert not any(b.reason == BlockReason.OSCILLATION for b in pr.blocked)
    assert not pr.blocked
    assert pr.build_ok and pr.iterations == 6
    assert fixer.asked == [("m1", None), ("m1", src("m1")), ("m2", None), ("m2", src("m2")), ("m3", None)]
    outcomes = [a.outcome for a in pr.attempts]
    # v2.1z-cd: at m2 / m3 the version that resolved the case in the module before runs first (no LLM call); it
    # fixes that module's pom only, so it changes nothing here (apply_failed, no build) and the augment follows
    assert outcomes == ["improved", "improved", "apply_failed", "improved", "apply_failed", "improved",
                        "apply_failed", "build_ok"]
    assert [a.source for a in pr.attempts if a.outcome == "apply_failed"] == ["kb-script"] * 3
    assert all(FIXED == ws.read(pom(m)) for m in ("m1", "m2", "m3"))


def test_the_fix_of_one_module_is_improved_when_the_next_module_shows_the_same_message(cfg, ws):
    start(ws, modules=("m1", "m2"), broken=())
    fixer = PerError()
    loop = make_loop(cfg, ws, fixer, build=real_run_build, kb=FakeKB())
    pr = loop.run(Phase.VERIFY, "package", 10)
    first = pr.attempts[0]
    assert first.outcome == "improved", first.notes
    assert first.cluster_resolved is True
    assert ws.read(pom("m1")) == FIXED                              # kept, not rolled back
    assert not events(ws, "stall") and not events(ws, "oscillation")
    # v2.1z-cd: m1's v1 runs first at m2 and changes nothing there (apply_failed, no build)
    assert pr.build_ok and [a.outcome for a in pr.attempts] == ["improved", "apply_failed", "build_ok"]


# ------------------------------------------------------------------------------------------------ real toggles
def test_the_same_modules_error_coming_back_is_still_an_oscillation(cfg, ws):
    """m1's pom token A → the module error of m1; token B → a file error; the module fix writes B, the file fix
    writes A: A, B, A, B on one place."""
    ws.write(pom("m1"), "<project>A without version</project>\n")
    ws.write(src("m1"), "class Widget { }\n")
    ws.commit("as-is")

    def build(w, call):
        return bad_build(mod_err("m1") if "A without" in w.read(pom("m1")) else file_err(src("m1"), "m1"))

    fixer = PerError(module_fix=lambda w, e, n: {pom("m1"): f"<project>B {n}</project>\n"},
                     file_fix=lambda w, e, n: {pom("m1"): f"<project>A without version {n}</project>\n"})
    loop = make_loop(cfg, ws, fixer, build=build, kb=FakeKB())
    pr = loop.run(Phase.VERIFY, "package", 10)
    assert len(events(ws, "oscillation")) == 2
    assert [b.reason for b in pr.blocked] and all(b.reason == BlockReason.OSCILLATION for b in pr.blocked)
    assert pr.iterations == 4


def test_a_file_error_toggling_in_one_file_is_still_an_oscillation(cfg, ws):
    a = src("m1")
    ws.write(a, "class Widget { S }\n")
    ws.commit("as-is")

    def build(w, call):
        s = "S" in w.read(a)
        return bad_build(file_err(a, "m1", sig="sig-S" if s else "sig-T", l2="l2-S" if s else "l2-T"))

    fixer = PerError(file_fix=lambda w, e, n: {a: f"class Widget {{ {'T' if 'S' in w.read(a) else 'S'} {n} }}\n"})
    loop = make_loop(cfg, ws, fixer, build=build, kb=FakeKB())
    pr = loop.run(Phase.VERIFY, "package", 10)
    assert len(events(ws, "oscillation")) == 2
    assert [b.reason for b in pr.blocked] and all(b.reason == BlockReason.OSCILLATION for b in pr.blocked)


def test_identical_places_and_signatures_two_builds_in_a_row_are_a_stall(cfg, ws):
    start(ws, modules=("m1",), broken=())
    fixer = PerError(module_fix=lambda w, e, n: {"notes.txt": f"nothing {n}\n"})
    loop = make_loop(cfg, ws, fixer, build=real_run_build, kb=FakeKB())
    pr = loop.run(Phase.VERIFY, "package", 4)
    stalls = events(ws, "stall")
    assert stalls and stalls[0]["iteration"] == 2
    assert pr.iteration_records[1].stalled


def test_the_same_signature_in_two_files_in_alternating_builds_is_no_oscillation(cfg, ws):
    """S in F1, then T in F3, then S in F2, then T in F4: the same two signatures alternate, in four files."""
    f1, f2, f3, f4 = (f"m1/src/main/java/com/example/F{i}.java" for i in range(1, 5))
    order = [(f1, "sig-S"), (f3, "sig-T"), (f2, "sig-S"), (f4, "sig-T")]
    for f, _s in order:
        ws.write(f, "class F { broken }\n")
    ws.commit("as-is")

    def build(w, call):
        for f, s in order:
            if "broken" in w.read(f):
                return bad_build(file_err(f, "m1", sig=s, l2="l2-" + s))
        return ok_build()

    fixer = PerError()
    loop = make_loop(cfg, ws, fixer, build=build, kb=FakeKB())
    pr = loop.run(Phase.VERIFY, "package", 10)
    assert not events(ws, "oscillation") and not events(ws, "stall")
    assert not pr.blocked and pr.build_ok
    # v2.1z-cd: in F2 / F4 the version that fixed the case in F1 / F3 runs first and changes nothing there
    assert [a.outcome for a in pr.attempts] == ["improved", "improved", "apply_failed", "improved", "apply_failed",
                                                "build_ok"]
    # the report keeps the per-signature view
    assert [r.signatures for r in pr.iteration_records][:4] == [{"sig-S": 1}, {"sig-T": 1}, {"sig-S": 1}, {"sig-T": 1}]
