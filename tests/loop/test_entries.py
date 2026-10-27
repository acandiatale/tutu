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
