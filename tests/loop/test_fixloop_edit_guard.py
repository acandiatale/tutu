"""v2.1z-bi part B: the caller's `edit_guard` of `BuildFixLoop` (docs/INTERFACES.md "v2.1z-bi", "Generic hook in the
loop").

`edit_guard(ws, pre_head, touched_paths)` is asked after any change was applied and before it is committed: LLM
scripts, LLM edits and sbmig's own pre-tries alike (the framework guard skips the pre-tries; this hook does not).
`touched_paths` lists the repo-relative paths added, modified or deleted since `pre_head`. A non-empty answer rejects
the attempt like a framework-guard rejection (nothing kept, the reason in `apply_errors`, shown to the next ask); a
guard that raises is a rejection `edit guard failed: <Type>: <msg>`; `edit_guard=None` changes nothing."""
from __future__ import annotations

from sbmig.core.models import Phase
from sbmig.loop import BuildFixLoop, LoopBudget
from sbmig.loop.fixloop import SBMIG_VERSION_SOURCE
from sbmig.triage import Triager

from .fakes import FakeBuildRunner, FakeFixer, FakeKB, FakeScriptRunner, answer, journal_events, write_script
from .test_v21l_versions import (
    ASIS,
    CORE,
    FOO,
    IO,
    TEXT,
    YAML,
    Lookup,
    PomApplier,
    _captures,
    _pom,
    _profile,
    _versions,
    _ws,
    pom_build,
    subjects,
    use_lookup,
)

NEW = "src/main/java/com/x/New.java"
OLD = "src/main/java/com/x/Old.java"


def guarded_loop(cfg, ws, fixer, build, guard=..., **kw) -> BuildFixLoop:
    """The loop of the v2.1l tests with a script runner; `guard` left out = the constructor's default."""
    extra = {} if guard is ... else {"edit_guard": guard}
    return BuildFixLoop(cfg, ws, FakeBuildRunner(ws, fn=build), Triager(cfg), fixer, FakeKB(), _profile(),
                        budget=LoopBudget.from_config(cfg), script_runner=FakeScriptRunner(),
                        edit_applier=PomApplier(), **extra, **kw)


class Recorder:
    """An edit guard that records its calls and answers from `answers` (in order; then None)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[tuple[str, list[str]]] = []

    def __call__(self, ws, pre, touched):
        self.calls.append((pre, list(touched)))
        return self.answers.pop(0) if self.answers else None


def no_pom(ws, pre, touched):
    bad = [p for p in touched if p.rsplit("/", 1)[-1] == "pom.xml"]
    return f"pom.xml must not be edited ({', '.join(bad)})" if bad else None


# ------------------------------------------------------------------------------------------------ LLM attempts
def test_a_rejected_llm_script_is_not_kept_and_the_reason_reaches_the_next_try(cfg, tmp_path):
    ws = _ws(tmp_path, {"pom.xml": _pom([]), FOO: "class Foo { broken }\n"})
    first = write_script({FOO: "class Foo { ok }\n", "pom.xml": _pom([(IO, "2.11.0")])})
    second = write_script({FOO: "class Foo { ok }\n"})
    fixer = FakeFixer(scripts=[first, second])
    loop = guarded_loop(cfg, ws, fixer, pom_build(broken_file=FOO), guard=no_pom)
    pr = loop.run(Phase.VERIFY, "package", 4)
    assert pr.ok
    a1, a2 = pr.attempts
    assert a1.outcome == "apply_failed" and not a1.applied and a1.commit is None
    assert "pom.xml must not be edited (pom.xml)" in a1.apply_errors
    # the next ask sees the rejected attempt with the guard's sentence
    prev = fixer.contexts[1].previous_attempts
    assert any("pom.xml must not be edited (pom.xml)" in a.apply_errors for a in prev)
    assert a2.applied and a2.outcome == "build_ok"
    assert "commons-io" not in ws.read("pom.xml")
    [ev] = [e for e in journal_events(ws) if e["event"] == "edit_guard"]
    assert ev["attempt"] == a1.id and ev["reason"] == "pom.xml must not be edited (pom.xml)"
    assert ev["paths"] == sorted([FOO, "pom.xml"])


def test_rejected_llm_edits_are_not_kept(cfg, tmp_path):
    ws = _ws(tmp_path, {"pom.xml": _pom([]), FOO: "class Foo { broken }\n"})
    a1 = answer(edits=[{"op": "write", "path": "pom.xml", "content": _pom([(IO, "2.11.0")])},
                       {"op": "write", "path": FOO, "content": "class Foo { ok }\n"}])
    a2 = answer(edits=[{"op": "write", "path": FOO, "content": "class Foo { ok }\n"}])
    fixer = FakeFixer(scripts=[a1, a2])
    loop = guarded_loop(cfg, ws, fixer, pom_build(broken_file=FOO), guard=no_pom)
    pr = loop.run(Phase.VERIFY, "package", 4)
    assert pr.ok
    first, second = pr.attempts
    assert first.outcome == "apply_failed" and "pom.xml must not be edited (pom.xml)" in first.apply_errors
    assert any("pom.xml must not be edited (pom.xml)" in a.apply_errors
               for a in fixer.contexts[1].previous_attempts)
    assert second.applied and "commons-io" not in ws.read("pom.xml")


# ------------------------------------------------------------------------------------------------ pre-tries
def test_a_pre_try_is_checked_too(cfg, tmp_path, monkeypatch):
    use_lookup(monkeypatch, Lookup({YAML: ["2.2", "2.3"]}))
    ws = _ws(tmp_path, {"pom.xml": _pom([(TEXT, None), (YAML, None), (CORE, None)])})
    _captures(ws, ASIS)
    head = ws.head()
    guard = Recorder("no pom edits in this run")
    loop = guarded_loop(cfg, ws, FakeFixer(), pom_build(managed=frozenset({CORE})), guard=guard)
    pr = loop.run(Phase.VERIFY, "package", 1)
    pre = pr.attempts[0]
    assert pre.source == SBMIG_VERSION_SOURCE
    assert pre.outcome == "apply_failed" and not pre.applied
    assert "no pom edits in this run" in pre.apply_errors
    assert guard.calls[0] == (head, ["pom.xml"])
    assert _versions(ws)[TEXT] is None and _versions(ws)[YAML] is None       # nothing of it kept
    assert not any(s.startswith("fix(sbmig-version)") for s in subjects(ws))
    assert any(e["event"] == "edit_guard" and e["source"] == SBMIG_VERSION_SOURCE for e in journal_events(ws))


# ------------------------------------------------------------------------------------------------ touched paths
def test_new_and_deleted_files_are_among_the_touched_paths(cfg, tmp_path):
    ws = _ws(tmp_path, {"pom.xml": _pom([]), FOO: "class Foo { broken }\n", OLD: "class Old {}\n"})
    head = ws.head()
    s = write_script({FOO: "class Foo { ok }\n", NEW: "class New {}\n"})
    s.code += f"    p.delete({OLD!r})\n"
    guard = Recorder()
    loop = guarded_loop(cfg, ws, FakeFixer(scripts=[s]), pom_build(broken_file=FOO), guard=guard)
    pr = loop.run(Phase.VERIFY, "package", 3)
    assert pr.ok and pr.attempts[0].applied
    assert guard.calls == [(head, sorted([FOO, NEW, OLD]))]
    assert ws.exists(NEW) and not ws.exists(OLD)


def test_a_guard_that_raises_rejects_the_attempt(cfg, tmp_path):
    ws = _ws(tmp_path, {"pom.xml": _pom([]), FOO: "class Foo { broken }\n"})

    def boom(ws_, pre, touched):
        raise ValueError("boom")

    loop = guarded_loop(cfg, ws, FakeFixer(scripts=[write_script({FOO: "class Foo { ok }\n"})]),
                        pom_build(broken_file=FOO), guard=boom)
    pr = loop.run(Phase.VERIFY, "package", 1)
    att = pr.attempts[0]
    assert att.outcome == "apply_failed" and "edit guard failed: ValueError: boom" in att.apply_errors
    assert "broken" in ws.read(FOO) and not ws.is_dirty()


# ------------------------------------------------------------------------------------------------ no guard
def _scenario(cfg, tmp_path, name, **kw):
    ws = _ws(tmp_path / name, {"pom.xml": _pom([]), FOO: "class Foo { broken }\n"})
    s = write_script({FOO: "class Foo { ok }\n", "pom.xml": _pom([(IO, "2.11.0")])})
    pr = guarded_loop(cfg, ws, FakeFixer(scripts=[s]), pom_build(broken_file=FOO), **kw).run(Phase.VERIFY,
                                                                                             "package", 3)
    return ws, pr


def test_without_a_guard_nothing_changes(cfg, tmp_path):
    ws0, pr0 = _scenario(cfg, tmp_path, "default")
    ws1, pr1 = _scenario(cfg, tmp_path, "none", guard=None)
    ws2, pr2 = _scenario(cfg, tmp_path, "accepting", guard=Recorder())
    for ws, pr in ((ws0, pr0), (ws1, pr1), (ws2, pr2)):
        assert pr.ok and [(a.source, a.outcome, a.applied) for a in pr.attempts] == [("llm-new", "build_ok", True)]
        assert "commons-io" in ws.read("pom.xml")
        assert not any(e["event"] == "edit_guard" for e in journal_events(ws))
    assert subjects(ws0) == subjects(ws1) == subjects(ws2)
    assert ws0.read("pom.xml") == ws1.read("pom.xml") == ws2.read("pom.xml")
    assert ws0.read(FOO) == ws1.read(FOO) == ws2.read(FOO)
    assert BuildFixLoop.__init__.__kwdefaults__["edit_guard"] is None
