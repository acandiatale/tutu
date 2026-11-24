"""v2.1z-cb review: an error the build never reached is not "restored" by a rollback.

Reactor m0, m1. Build 1 reports E for m1 (m0 built, m1 failed). Every fix makes m0 fail with the same message, so m1
is skipped. After the first try is rolled back, the errors it had "removed" used to be added to the baseline of the
next try although that build could not show them; every later try then read "1 of 2 gone" (`partial`) on identical
builds, and a `partial` try is kept at the end of the loop: the change that broke m0 stayed in the tree. The same for
errors that have a file."""
from __future__ import annotations

import pytest

from sbmig.core.models import BuildError, ErrorKind, Phase

from .fakes import FakeFixer, FakeKB, bad_build, write_script
from .test_v21v_y_prompt_facts import make_loop

MSG = "Resolution of annotationProcessorPath dependencies failed: Cannot find version for annotation processor path"
SIG = "compile|build|resolution of annotationprocessorpath dependencies failed"
L2 = "compile|build|resolution of annotationprocessorpath"


def _module_error(m: str) -> BuildError:
    return BuildError(kind=ErrorKind.COMPILE, message=MSG, file=None, module=m, goal="compiler:compile",
                      signature=SIG, signature_generic=L2)


def _file_error(m: str) -> BuildError:
    return BuildError(kind=ErrorKind.COMPILE, message="cannot find symbol Widget", line=3, module=m,
                      file=f"{m}/src/main/java/com/example/A.java", signature="compile|cannot_find_symbol|Widget",
                      signature_generic="compile|cannot_find_symbol|Widget")


class _Counting(FakeFixer):
    """Every answer bumps `state.txt` (a file no error is in): the tree changes, the build fails one module earlier."""

    def __init__(self) -> None:
        super().__init__()
        self.n = 0

    def propose_script(self, ws, cluster, ctx):
        self.contexts.append(ctx)
        self.n += 1
        return write_script({"state.txt": f"{self.n}\n"}, analysis=f"try {self.n}")


def _state(w) -> int:
    return int(w.read("state.txt").strip()) if w.exists("state.txt") else 0


def _build(err: BuildError, built: list[str], failed: str):
    return bad_build(err).model_copy(update={"modules_built": list(built), "failed_module": failed})


@pytest.mark.parametrize("make", [_module_error, _file_error], ids=["module-level", "file"])
def test_a_target_the_build_never_reached_is_not_partial_progress(cfg, ws, make):
    ws.write("state.txt", "0\n")
    ws.commit("as-is")
    loop = make_loop(cfg, ws, _Counting(), kb=FakeKB(),
                     build=lambda w, c: _build(make("m1"), ["m0"], "m1") if _state(w) == 0
                     else _build(make("m0"), [], "m0"))
    pr = loop.run(Phase.VERIFY, "package", 4)
    assert "partial" not in [a.outcome for a in pr.attempts]
    assert "improved" not in [a.outcome for a in pr.attempts]
    assert _state(ws) == 0                      # nothing that broke m0 stays in the tree
