"""v2.1 adapters (`sbmig.loop.entries`): one place for every error-list / case-store call; a rename or a missing
keyword on the other side costs nothing, a failure is a warning."""
from __future__ import annotations

from types import SimpleNamespace

from sbmig.loop.entries import CaseScripts, RunErrors, script_path


class Strict:
    """An error list that takes `round` / `void` but not `scope`."""

    def __init__(self):
        self.calls = []

    def add_build(self, build, seen, *, round=None, void=False):
        self.calls.append((seen, round, void))
        return [1, 2]


def test_only_the_rejected_optional_keyword_is_dropped():
    el = Strict()
    assert RunErrors(el).add_build(object(), "fix r2", round=1, void=True, scope="svc-a") == 2
    assert el.calls == [("fix r2", 1, True)]


def test_an_inactive_list_answers_nothing_and_a_failing_one_warns(caplog):
    off = RunErrors(None)
    assert not off.active and off.add_build([], "build") == 0 and off.open_count() is None and off.write() == {}
    assert off.review_fixed("pom.xml", "x") is None and off.end_of_run() == {}

    class Broken:
        def verify_poms(self, build):
            raise OSError("disk")
    assert RunErrors(Broken()).verify_poms(object()) == 0
    assert any("errors.verify_poms failed: OSError: disk" in r.getMessage() for r in caplog.records)


def test_case_scripts_adapter():
    class Store:
        def best(self, case_id):
            return (SimpleNamespace(version=2, file="fix_v2.py"), "code")

        def describe(self, case_id):
            raise RuntimeError("x")
    cs = CaseScripts(Store())
    assert cs.active and cs.best("c")[1] == "code" and cs.describe("c") == "" and cs.find("c") is None
    assert not CaseScripts(None).active and CaseScripts(object()).best("c") is None
    assert script_path("abc", SimpleNamespace(version=3, file="fix_v3.py")) == "cases/abc/fix_v3.py"


# ------------------------------------------------------------------------------------------------ v2.1h (A22)
def test_a_typeerror_from_inside_the_callee_is_not_retried(caplog):
    """The callee takes the optional keyword and its BODY raises a TypeError that quotes it: one call (no second
    run of its side effects), the error reaches the guard (WARNING) and the call answers its "nothing" value."""
    class Inside:
        def __init__(self):
            self.calls = []

        def add_build(self, build, seen, *, round=None, void=False, scope=None):
            self.calls.append((seen, round, void, scope))                    # a side effect, like begin_try's
            raise TypeError("merge() got an unexpected keyword argument 'scope'")

        def finish_try(self, case_id, n, *, edits=None, **fields):
            self.calls.append(("finish", case_id, n, edits, fields))
            raise TypeError("note(): 'edits' must be a list")
    el = Inside()
    assert RunErrors(el).add_build(object(), "fix r2", round=1, void=True, scope="svc-a") == 0
    assert el.calls == [("fix r2", 1, True, "svc-a")]
    assert any("errors.add_build failed: TypeError: merge() got an unexpected keyword argument 'scope'"
               in r.getMessage() for r in caplog.records)
    el.calls.clear()
    RunErrors(el).finish_try("c1", 2, edits=["x"], result="improved")
    assert el.calls == [("finish", "c1", 2, ["x"], {"result": "improved"})]


def test_a_callee_without_the_keyword_is_called_once_without_it():
    class Old:
        def __init__(self):
            self.calls = []

        def finish_try(self, case_id, n, result=None, note=""):                # no edits / major_upgrades / verify
            self.calls.append((case_id, n, result, note))

    class OldStore:
        def __init__(self):
            self.calls = []

        def best(self, case_id):
            return None

        def credit(self, case_id, version, result, project="", run_id=""):    # no reason / round / n / files
            self.calls.append((case_id, version, result, project, run_id))
    el, st = Old(), OldStore()
    RunErrors(el).finish_try("c1", 1, result="improved", note="ok", edits=["e"], major_upgrades=[], verify="v")
    assert el.calls == [("c1", 1, "improved", "ok")]
    assert CaseScripts(st).credit("c1", 2, "no_change", project="p", run_id="r", reason="why", round=1, n=3,
                                  files=["A"])
    assert st.calls == [("c1", 2, "no_change", "p", "r")]


def test_an_uninspectable_callable_keeps_the_typeerror_retry():
    """No inspectable signature (a builtin-like callable): the old way — the keyword the TypeError names is
    dropped and the call made again."""
    class Opaque:
        __signature__ = "not a signature"                                    # inspect.signature() refuses it

        def __init__(self):
            self.calls = []

        def __call__(self, build, seen, **kw):
            if "scope" in kw:
                raise TypeError("add_build() got an unexpected keyword argument 'scope'")
            self.calls.append((seen, kw))
            return 3
    fn = Opaque()
    assert RunErrors(SimpleNamespace(add_build=fn)).add_build(object(), "build", round=2, scope="svc") == 3
    assert fn.calls == [("build", {"void": False, "round": 2})]
