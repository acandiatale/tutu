"""Tests for sbmig.remediations.fsops (INTERFACES "v2.1z-ce"): renames, replaces and removals that Windows refuses
while another program holds a file open are waited for; everything else (all of POSIX) is raised at once.

Windows is simulated: the patched `os` call raises `PermissionError` with `.winerror` set on the instance. The clock
and the sleep of `fsops` are replaced by a fake, so nothing here sleeps for real."""
from __future__ import annotations

import ast
import itertools
import json
import logging
import os
import re
import shutil
import threading
import zipfile
from pathlib import Path

import pytest

from sbmig.remediations import chain, fixer, fsops, intake
from sbmig.remediations.models import IntakeLimits, RunPaths, write_json_atomic

LOGGER = "sbmig.remediations.fsops"
POM = b"<project><modelVersion>4.0.0</modelVersion><groupId>example</groupId><artifactId>lib</artifactId></project>"


# ---------------------------------------------------------------------------- helpers
class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.t = start
        self.pauses: list[float] = []
        self.on_sleep = None

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        if len(self.pauses) > 10_000:
            raise AssertionError("the wait never ends")     # a hang becomes a failure
        self.pauses.append(s)
        self.t += s
        if self.on_sleep is not None:
            self.on_sleep()


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(fsops, "_now", c.now)
    monkeypatch.setattr(fsops, "_sleep", c.sleep)
    return c


def win_error(n: int) -> PermissionError:
    e = PermissionError(13, "Access is denied")
    e.winerror = n  # type: ignore[attr-defined]
    return e


def refusing(real, times: int | None, winerror: int = 5, match=lambda *a: True):
    """A stand-in for an `os` function that refuses `times` calls matching `match` (None = forever)."""
    state = {"refused": 0, "calls": 0, "off": False}

    def fake(*a, **k):
        state["calls"] += 1
        if not state["off"] and match(*a) and (times is None or state["refused"] < times):
            state["refused"] += 1
            raise win_error(winerror)
        return real(*a, **k)
    fake.state = state  # type: ignore[attr-defined]
    return fake


def messages(caplog, level=None) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER and (level is None or r.levelno == level)]


def make_zip(path: Path, entries: list) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return path


# ---------------------------------------------------------------------------- 1. refused, then allowed
def test_replace_refused_twice_then_allowed(tmp_path, clock, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    src, dst = tmp_path / "deep" / "a.partial", tmp_path / "deep" / "a.json"
    src.parent.mkdir()
    src.write_text("new")
    fake = refusing(os.replace, 2)
    monkeypatch.setattr(os, "replace", fake)
    fsops.replace(src, dst)
    assert dst.read_text() == "new" and not src.exists()
    assert fake.state["calls"] == 3
    assert clock.pauses == [0.05, 0.1]
    info = messages(caplog, logging.INFO)
    assert info[0] == ("remediation: replace of a.partial refused (WinError 5): another program has a file open "
                       "there; waiting up to 60 s")
    assert re.fullmatch(r"remediation: replace of a\.partial went through after 0\.[12] s \(3 tries\)", info[1])
    assert len(info) == 2
    assert not any(str(tmp_path) in m for m in messages(caplog))
    assert messages(caplog, logging.WARNING) == []


def test_rename_and_unlink_wait_too(tmp_path, clock, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    a = tmp_path / "claim.json"
    a.write_text("x")
    monkeypatch.setattr(os, "rename", refusing(os.rename, 1, 32))
    fsops.rename(a, tmp_path / "aside")
    assert (tmp_path / "aside").exists() and not a.exists()
    monkeypatch.setattr(os, "unlink", refusing(os.unlink, 1, 32))
    fsops.unlink(tmp_path / "aside")
    assert not (tmp_path / "aside").exists()
    assert len(clock.pauses) == 2
    assert any(m.startswith("remediation: rename of claim.json refused (WinError 32)") for m in messages(caplog))
    assert any(m.startswith("remediation: unlink of aside went through") for m in messages(caplog))


# ---------------------------------------------------------------------------- 2. refused for too long
def test_refused_longer_than_the_wait_raises_the_last_exception(tmp_path, clock, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    src = tmp_path / "held"
    src.write_text("x")
    raised: list[BaseException] = []
    real = os.replace

    def fake(*a, **k):
        e = win_error(5)
        raised.append(e)
        raise e
    monkeypatch.setattr(os, "replace", fake)
    with pytest.raises(PermissionError) as ei:
        fsops.replace(src, tmp_path / "dst")
    assert ei.value is raised[-1] and type(ei.value) is PermissionError
    assert ei.value.winerror == 5  # type: ignore[attr-defined]
    p = clock.pauses
    assert p[:6] == [0.05, 0.1, 0.2, 0.4, 0.8, 1.6]
    assert all(x <= fsops.MAX_PAUSE_S for x in p)
    assert all(b == min(a * 2, 2.0) for a, b in itertools.pairwise(p[:-1]))       # the last one is cut to the time left
    assert sum(p) == pytest.approx(fsops.HOLD_WAIT_S) and sum(p) <= fsops.HOLD_WAIT_S + 1e-9
    warn = messages(caplog, logging.WARNING)
    assert warn == [(f"remediation: replace of held still refused after 60.0 s ({len(raised)} tries, WinError 5): a "
                     "virus scanner, an indexer or an editor that has the run folder open keeps a file open there "
                     "(no program found with the 1 file(s) looked at open)")]      # v2.1z-cf rule 4, POSIX finds none
    assert len(raised) == len(p) + 1
    monkeypatch.setattr(os, "replace", real)
    assert src.exists()


# ---------------------------------------------------------------------------- 3. everything else at once
@pytest.mark.parametrize("exc", [PermissionError(13, "Permission denied"), FileNotFoundError(2, "No such file")])
def test_posix_errors_are_raised_at_once(tmp_path, clock, monkeypatch, caplog, exc):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    calls = []

    def fake(*a, **k):
        calls.append(a)
        raise exc
    for name in ("replace", "rename"):
        monkeypatch.setattr(os, name, fake)
    monkeypatch.setattr(os, "unlink", fake)
    with pytest.raises(type(exc)) as ei:
        fsops.replace(tmp_path / "a", tmp_path / "b")
    assert ei.value is exc
    with pytest.raises(type(exc)):
        fsops.rename(tmp_path / "a", tmp_path / "b")
    with pytest.raises(type(exc)):
        fsops.unlink(tmp_path / "a")
    assert len(calls) == 3 and clock.pauses == [] and messages(caplog) == []


def test_a_missing_tree_raises_like_shutil(tmp_path, clock):
    with pytest.raises(FileNotFoundError):
        fsops.rmtree(tmp_path / "nothing")
    assert clock.pauses == []


def test_winerror_32_is_waited_for_and_another_winerror_is_not(tmp_path, clock, monkeypatch):
    src = tmp_path / "a"
    src.write_text("x")
    monkeypatch.setattr(os, "replace", refusing(os.replace, 3, 32))
    fsops.replace(src, tmp_path / "b")
    assert (tmp_path / "b").exists() and len(clock.pauses) == 3
    clock.pauses.clear()
    fake = refusing(os.replace, None, 3)
    monkeypatch.setattr(os, "replace", fake)
    with pytest.raises(PermissionError) as ei:
        fsops.replace(tmp_path / "b", tmp_path / "c")
    assert ei.value.winerror == 3  # type: ignore[attr-defined]
    assert fake.state["calls"] == 1 and clock.pauses == []


# ---------------------------------------------------------------------------- 4. rmtree
def make_tree(root: Path) -> Path:
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "b" / "held.txt").write_text("1")
    (root / "a" / "ro.txt").write_text("2")
    (root / "top.txt").write_text("3")
    return root


def test_rmtree_removes_a_real_tree(tmp_path, clock):
    t = make_tree(tmp_path / "tree")
    fsops.rmtree(t)
    assert not t.exists() and clock.pauses == []


def test_rmtree_makes_a_read_only_entry_writable_without_waiting(tmp_path, clock, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    t = make_tree(tmp_path / "tree")
    chmods = []
    real_chmod = os.chmod

    def chmod(p, mode, *a, **k):
        chmods.append((os.path.basename(os.fspath(p)), mode))
        return real_chmod(p, mode, *a, **k)
    monkeypatch.setattr(os, "chmod", chmod)
    fake = refusing(os.unlink, 1, 5, match=lambda p, *a: os.path.basename(os.fspath(p)) == "ro.txt")
    monkeypatch.setattr(os, "unlink", fake)
    fsops.rmtree(t)
    assert not t.exists()
    assert ("ro.txt", fsops.stat.S_IWRITE) in chmods
    assert clock.pauses == [] and messages(caplog) == []


def test_rmtree_waits_for_a_held_entry(tmp_path, clock, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    t = make_tree(tmp_path / "tree")
    fake = refusing(os.unlink, 3, 32, match=lambda p, *a: os.path.basename(os.fspath(p)) == "held.txt")
    monkeypatch.setattr(os, "unlink", fake)
    fsops.rmtree(t)
    assert not t.exists()
    assert clock.pauses == [0.05, 0.1, 0.2]
    assert re.fullmatch(r"remediation: rmtree of tree went through after 0\.[34] s \(4 tries\)",
                        messages(caplog, logging.INFO)[-1])


def test_rmtree_held_directory_is_waited_for(tmp_path, clock, monkeypatch):
    t = make_tree(tmp_path / "tree")
    monkeypatch.setattr(os, "chmod", lambda *a, **k: None)     # S_IWRITE alone would lock a POSIX folder
    monkeypatch.setattr(os, "rmdir", refusing(os.rmdir, 2, 5, match=lambda p, *a: os.path.basename(os.fspath(p)) == "b"))
    fsops.rmtree(t)
    assert not t.exists() and len(clock.pauses) == 1     # the first refusal was taken for a read-only entry


def test_rmtree_of_a_tree_that_vanished_between_tries_is_success(tmp_path, clock, monkeypatch):
    t = make_tree(tmp_path / "tree")
    real_rmtree = shutil.rmtree
    fake = refusing(os.unlink, None, 32, match=lambda p, *a: os.path.basename(os.fspath(p)) == "held.txt")
    monkeypatch.setattr(os, "unlink", fake)

    def vanish():
        fake.state["off"] = True     # another remover takes the tree away while fsops waits
        real_rmtree(t)
    clock.on_sleep = vanish
    fsops.rmtree(t)
    assert not t.exists() and len(clock.pauses) == 1


def test_rmtree_refused_forever_raises(tmp_path, clock, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    t = make_tree(tmp_path / "tree")
    monkeypatch.setattr(os, "unlink", refusing(os.unlink, None, 32,
                                               match=lambda p, *a: os.path.basename(os.fspath(p)) == "held.txt"))
    with pytest.raises(PermissionError) as ei:
        fsops.rmtree(t)
    assert ei.value.winerror == 32  # type: ignore[attr-defined]
    assert sum(clock.pauses) == pytest.approx(fsops.HOLD_WAIT_S)
    assert len(messages(caplog, logging.WARNING)) == 1
    assert (t / "a" / "b" / "held.txt").exists()


def test_clear_root_goes_through_fsops(tmp_path, clock, monkeypatch):
    root = tmp_path / "remediation"
    make_tree(root / "rounds")
    (root / "state.json.tmp").write_text("{}")
    (root / chain.CLAIM_NAME).write_text("{}")
    monkeypatch.setattr(os, "unlink", refusing(os.unlink, 1, 32,
                                               match=lambda p, *a: os.path.basename(os.fspath(p)) == "state.json.tmp"))
    chain._clear_root(root)
    assert sorted(p.name for p in root.iterdir()) == [chain.CLAIM_NAME]
    assert clock.pauses == [0.05]


# ---------------------------------------------------------------------------- 5. accept_zip end to end
def is_partial(src, *a, **k) -> bool:
    return os.path.basename(os.fspath(src)) == ".accepted.partial"


def test_accept_zip_waits_for_the_partial_rename(tmp_path, clock, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    z = make_zip(tmp_path / "up.zip", [("pom.xml", POM), ("src/main/java/A.java", b"class A {}")])
    run = tmp_path / "run"
    monkeypatch.setattr(os, "replace", refusing(os.replace, 2, 5, match=is_partial))
    res = intake.accept_zip(z, run, IntakeLimits())
    rp = RunPaths(run)
    assert res.files == 2
    assert (rp.accepted / "pom.xml").read_bytes() == POM
    assert (rp.accepted / "src/main/java/A.java").read_bytes() == b"class A {}"
    assert not (run / ".accepted.partial").exists()
    assert clock.pauses == [0.05, 0.1]
    assert messages(caplog, logging.INFO)[0].startswith("remediation: replace of .accepted.partial refused (WinError 5)")


def test_accept_zip_refused_forever_fails_and_cleans_up(tmp_path, clock, monkeypatch):
    z = make_zip(tmp_path / "up.zip", [("pom.xml", POM), ("src/main/java/A.java", b"class A {}")])
    run = tmp_path / "run"
    monkeypatch.setattr(os, "replace", refusing(os.replace, None, 5, match=is_partial))
    with pytest.raises(PermissionError) as ei:
        intake.accept_zip(z, run, IntakeLimits())
    assert ei.value.winerror == 5  # type: ignore[attr-defined]
    assert not (run / ".accepted.partial").exists()
    assert not (run / "accepted").exists()
    assert not (run / "original" / "manifest.json").exists()
    assert sum(clock.pauses) == pytest.approx(fsops.HOLD_WAIT_S)


# ---------------------------------------------------------------------------- 6. write_json_atomic
def test_write_json_atomic_refused_once(tmp_path, clock, monkeypatch):
    target = tmp_path / "state.json"
    target.write_text(json.dumps({"old": 1}))
    monkeypatch.setattr(os, "replace", refusing(os.replace, 1, 32))
    write_json_atomic(target, {"new": 2})
    assert json.loads(target.read_text()) == {"new": 2}
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]
    assert clock.pauses == [0.05]


# ---------------------------------------------------------------------------- a read-only destination is final
def _refuse_read_only_destination(monkeypatch):
    """Windows' rule for `os.replace`: a read-only destination file is refused with access denied, every time."""
    real = os.replace
    calls = []

    def fake(src, dst):
        calls.append(os.fspath(dst))
        if os.path.isfile(dst) and not os.stat(dst).st_mode & 0o200:
            raise win_error(5)
        return real(src, dst)
    monkeypatch.setattr(os, "replace", fake)
    return calls


def test_replace_onto_a_read_only_file_is_not_waited_for(tmp_path, clock, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    src, dst = tmp_path / "a.tmp", tmp_path / "a.txt"
    src.write_text("new")
    dst.write_text("old")
    os.chmod(dst, 0o444)
    calls = _refuse_read_only_destination(monkeypatch)
    with pytest.raises(PermissionError) as ei:
        fsops.replace(src, dst)
    assert ei.value.winerror == 5
    assert clock.pauses == [] and len(calls) == 1 and messages(caplog) == []
    assert dst.read_text() == "old"


def test_write_back_over_a_read_only_file_makes_it_writable_at_once(tmp_path, clock, monkeypatch):
    tree, src = tmp_path / "tree", tmp_path / "expected.txt"
    (tree / "m").mkdir(parents=True)
    (tree / "m" / "a.txt").write_text("changed by a build")
    os.chmod(tree / "m" / "a.txt", 0o444)
    src.write_text("expected")
    calls = _refuse_read_only_destination(monkeypatch)
    intake._write_back(tree, "m/a.txt", src, 0o644)
    assert (tree / "m" / "a.txt").read_text() == "expected"
    assert clock.pauses == [] and len(calls) == 2
    assert sorted(x.name for x in (tree / "m").iterdir()) == ["a.txt"]


# ---------------------------------------------------------------------------- the guard (contract rule 7)
PACKAGE = Path(__file__).resolve().parents[2] / "sbmig" / "remediations"
BARE_ATTRS = {("os", "replace"), ("os", "rename"), ("os", "renames"), ("shutil", "rmtree"), ("shutil", "move")}
BARE_IMPORTS = {"os": {"replace", "rename", "renames"}, "shutil": {"rmtree", "move"}}
# (file, enclosing function, form) of the sites the contract leaves as they are
ALLOWED = {
    ("chain.py", "RunClaim._take_over", "os.rename"),                  # a takeover must not wait
    ("intake.py", "_make_writable_and_remove", "shutil.rmtree"),       # ignore_errors=True
    ("service.py", "RemediationManager._remove_dir", "shutil.rmtree"),  # its own make-writable handler
}


def bare_forms(path: Path) -> list[tuple[str, str, str, int]]:
    """(file, enclosing function, form, line) of every bare rename / replace / removal form in `path`."""
    hits: list[tuple[str, str, str, int]] = []
    stack: list[str] = []

    class Visitor(ast.NodeVisitor):
        def _scope(self, node):
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()
        visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scope

        def visit_Attribute(self, node):
            if isinstance(node.value, ast.Name) and (node.value.id, node.attr) in BARE_ATTRS:
                hits.append((path.name, ".".join(stack), f"{node.value.id}.{node.attr}", node.lineno))
            self.generic_visit(node)

        def visit_ImportFrom(self, node):
            for a in node.names:
                if a.name in BARE_IMPORTS.get(node.module or "", ()):
                    hits.append((path.name, ".".join(stack), f"from {node.module} import {a.name}", node.lineno))

        def visit_Call(self, node):
            if (isinstance(node.func, ast.Attribute) and node.func.attr in ("replace", "rename")
                    and len(node.args) == 1 and not node.keywords):
                hits.append((path.name, ".".join(stack), f".{node.func.attr}(x)", node.lineno))
            self.generic_visit(node)

    Visitor().visit(ast.parse(path.read_text(encoding="utf-8")))
    return hits


def test_guard_sees_every_bare_form(tmp_path):
    f = tmp_path / "sample.py"
    f.write_text("import os, shutil\nfrom os import replace\nfrom shutil import move\n"
                 "def g(p, q):\n    x = os.replace\n    shutil.rmtree(p)\n    os.renames(p, q)\n"
                 "    p.rename(q)\n    p.replace(q)\n    'a'.replace('a', 'b')\n    p.with_suffix('.x')\n")
    forms = sorted(h[2] for h in bare_forms(f))
    assert forms == sorted(["from os import replace", "from shutil import move", "os.replace", "shutil.rmtree",
                            "os.renames", ".rename(x)", ".replace(x)"])


def test_no_bare_rename_or_rmtree_in_the_package():
    bad: list[str] = []
    used: set[tuple[str, str, str]] = set()
    for f in sorted(PACKAGE.glob("*.py")):
        if f.name == "fsops.py":
            continue
        for name, func, form, line in bare_forms(f):
            if (name, func, form) in ALLOWED:
                used.add((name, func, form))
            else:
                bad.append(f"{name}:{line} in {func or '<module>'}: {form}")
    assert not bad, ("bare rename / replace / removal in sbmig/remediations; use sbmig.remediations.fsops "
                     "(replace / rename / unlink / rmtree) instead:\n" + "\n".join(bad))
    assert used == ALLOWED, f"allow-list entries that match nothing any more: {sorted(ALLOWED - used)}"


# ---------------------------------------------------------------------------- what the review's mutants showed
def test_waited_out_marks_only_the_exception_raised_after_the_deadline(tmp_path, clock, monkeypatch):
    monkeypatch.setattr(os, "replace", refusing(os.replace, None, 5))
    with pytest.raises(PermissionError) as ei:
        fsops.replace(tmp_path / "a", tmp_path / "b")
    assert fsops.waited_out(ei.value)
    assert not fsops.waited_out(win_error(5)) and not fsops.waited_out(FileNotFoundError())


def test_a_later_rmtree_try_that_finds_the_path_missing_inside_raises(tmp_path, clock, monkeypatch):
    t = make_tree(tmp_path / "tree")
    calls = {"n": 0}

    def fake(path, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise win_error(32)
        raise FileNotFoundError(2, "No such file or directory", "inner")
    monkeypatch.setattr(shutil, "rmtree", fake)
    with pytest.raises(FileNotFoundError):
        fsops.rmtree(t)
    assert t.exists() and calls["n"] == 2


def test_a_vanished_tree_is_success_without_another_rmtree_call(tmp_path, clock, monkeypatch):
    t = make_tree(tmp_path / "tree")
    real = shutil.rmtree
    calls = {"n": 0}

    def fake(path, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise win_error(32)
        raise AssertionError("shutil.rmtree called again on a path that is gone")
    monkeypatch.setattr(shutil, "rmtree", fake)
    clock.on_sleep = lambda: real(t)
    fsops.rmtree(t)
    assert not t.exists() and calls["n"] == 1


def test_rmtree_waits_for_145_and_the_other_three_do_not(tmp_path, clock, monkeypatch):
    t = make_tree(tmp_path / "tree")
    monkeypatch.setattr(os, "rmdir", refusing(os.rmdir, 2, 145, match=lambda p, *a: os.path.basename(os.fspath(p)) == "b"))
    fsops.rmtree(t)
    assert not t.exists() and clock.pauses == [0.05, 0.1]
    clock.pauses.clear()
    (tmp_path / "f").write_text("x")
    for name, call in (("replace", lambda: fsops.replace(tmp_path / "f", tmp_path / "g")),
                       ("rename", lambda: fsops.rename(tmp_path / "f", tmp_path / "g")),
                       ("unlink", lambda: fsops.unlink(tmp_path / "f"))):
        fake = refusing(getattr(os, name), None, 145)
        monkeypatch.setattr(os, name, fake)
        with pytest.raises(PermissionError):
            call()
        assert fake.state["calls"] == 1, name
    assert clock.pauses == []


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root removes anything")
def test_posix_rmtree_of_a_locked_folder_raises_what_shutil_raises_and_changes_no_mode(tmp_path, clock):
    def build(root: Path) -> Path:
        (root / "locked").mkdir(parents=True)
        (root / "locked" / "f.txt").write_text("x")
        os.chmod(root / "locked", 0o500)
        return root

    def modes(root: Path) -> dict[str, int]:
        return {p.name: os.lstat(p).st_mode for p in [root, *root.rglob("*")]}
    a, b = build(tmp_path / "a"), build(tmp_path / "b")
    try:
        with pytest.raises(OSError) as want:
            shutil.rmtree(a)
        before = modes(b)
        with pytest.raises(OSError) as got:
            fsops.rmtree(b)
        assert type(got.value) is type(want.value) and got.value.errno == want.value.errno
        assert os.path.basename(got.value.filename) == os.path.basename(want.value.filename)
        assert modes(b) == before and clock.pauses == []
    finally:
        for root in (a, b):
            if (root / "locked").exists():
                os.chmod(root / "locked", 0o700)


def held_destination(monkeypatch, winerror: int = 32):
    """`os.replace` refused for every destination inside a folder named `tree` (a holder that never lets go)."""
    real = os.replace
    calls = []

    def fake(src, dst):
        if "tree" in Path(dst).parts:
            calls.append(os.fspath(dst))
            raise win_error(winerror)
        return real(src, dst)
    monkeypatch.setattr(os, "replace", fake)
    return calls


def test_write_back_on_a_held_destination_waits_once(tmp_path, clock, monkeypatch):
    tree, src = tmp_path / "tree", tmp_path / "expected.txt"
    (tree / "m").mkdir(parents=True)
    (tree / "m" / "a.txt").write_text("changed by a build")
    src.write_text("expected")
    held_destination(monkeypatch, 5)
    with pytest.raises(PermissionError) as ei:
        intake._write_back(tree, "m/a.txt", src, 0o644)
    assert fsops.waited_out(ei.value)
    assert sum(clock.pauses) == pytest.approx(fsops.HOLD_WAIT_S)
    assert sorted(x.name for x in (tree / "m").iterdir()) == ["a.txt"]


def refused_for(real, clock: FakeClock, seconds: float, winerror: int = 32, match=lambda *a: True):
    """A stand-in that refuses calls matching `match` until `seconds` have passed on the fake clock from now."""
    until = clock.t + seconds
    state = {"calls": 0}

    def fake(*a, **k):
        state["calls"] += 1
        if match(*a) and clock.t < until:
            raise win_error(winerror)
        return real(*a, **k)
    fake.state = state  # type: ignore[attr-defined]
    return fake


def give_up_once(tmp_path: Path, monkeypatch, name: str = "gave-up") -> None:
    real = os.replace
    monkeypatch.setattr(os, "replace", refusing(real, None, 32))
    with pytest.raises(PermissionError) as ei:
        fsops.replace(tmp_path / name, tmp_path / "x")
    assert fsops.waited_out(ei.value)
    monkeypatch.setattr(os, "replace", real)


def waited_for(clock: FakeClock, call) -> float:
    before = len(clock.pauses)
    with pytest.raises(PermissionError):
        call()
    return sum(clock.pauses[before:])


# ---------------------------------------------------------------------------- the memory belongs to a block
def test_outside_a_block_a_give_up_takes_nothing_from_the_next_call(tmp_path, clock, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    monkeypatch.setattr(os, "replace", refusing(os.replace, None, 32))
    for name in ("one", "one", "two"):                 # the same path again, then another one
        assert waited_for(clock, lambda n=name: fsops.replace(tmp_path / n, tmp_path / "x")) == pytest.approx(
            fsops.HOLD_WAIT_S)
        assert messages(caplog, logging.INFO)[-1].endswith(f"of {name} refused (WinError 32): another program has a "
                                                           "file open there; waiting up to 60 s")


def test_a_failed_intake_leaves_the_full_wait_to_the_next_run(tmp_path, clock, monkeypatch):
    z = make_zip(tmp_path / "up.zip", [("pom.xml", POM), ("src/main/java/A.java", b"class A {}")])
    real = os.replace
    monkeypatch.setattr(os, "replace", refusing(real, None, 5, match=is_partial))
    with pytest.raises(PermissionError):
        intake.accept_zip(z, tmp_path / "run1", IntakeLimits())
    monkeypatch.setattr(os, "replace", refused_for(real, clock, 5.0, 5, match=is_partial))
    before = len(clock.pauses)
    res = intake.accept_zip(z, tmp_path / "run2", IntakeLimits())
    assert res.files == 2 and (RunPaths(tmp_path / "run2").accepted / "pom.xml").read_bytes() == POM
    assert 5.0 <= sum(clock.pauses[before:]) < 8.0


def test_a_logged_removal_failure_leaves_the_full_wait_to_the_delivery_swap(tmp_path, clock, monkeypatch, caplog):
    t = make_tree(tmp_path / "tree")
    real_unlink = os.unlink
    monkeypatch.setattr(os, "unlink", refusing(real_unlink, None, 32,
                                               match=lambda p, *a: os.path.basename(os.fspath(p)) == "held.txt"))
    fixer._rmtree_quiet(t, "work copy")                # gives up, only logged
    assert t.exists() and sum(clock.pauses) == pytest.approx(fsops.HOLD_WAIT_S)
    monkeypatch.setattr(os, "unlink", real_unlink)
    nz, dz = tmp_path / ".result.next.zip", tmp_path / "result.zip"
    nz.write_bytes(b"zip")
    monkeypatch.setattr(os, "replace", refused_for(os.replace, clock, 5.0))
    before = len(clock.pauses)
    fsops.replace(nz, dz)
    assert dz.read_bytes() == b"zip" and 5.0 <= sum(clock.pauses[before:]) < 8.0


def test_inside_a_block_one_full_wait_then_short_ones(tmp_path, clock, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    monkeypatch.setattr(os, "replace", refusing(os.replace, None, 32))
    call = lambda n: lambda: fsops.replace(tmp_path / n, tmp_path / "x")
    with fsops.one_wait():
        assert waited_for(clock, call("one")) == pytest.approx(fsops.HOLD_WAIT_S)
        assert messages(caplog, logging.INFO)[-1].endswith("waiting up to 60 s")
        for n in ("two", "three"):
            assert waited_for(clock, call(n)) == pytest.approx(fsops.SHORT_WAIT_S)
            assert messages(caplog, logging.INFO)[-1].endswith(f"of {n} refused (WinError 32): another program has a "
                                                               "file open there; waiting up to 2 s")
    assert waited_for(clock, call("after")) == pytest.approx(fsops.HOLD_WAIT_S)
    assert messages(caplog, logging.INFO)[-1].endswith("waiting up to 60 s")


def test_a_block_left_by_an_exception_restores_the_full_limit(tmp_path, clock, monkeypatch):
    monkeypatch.setattr(os, "replace", refusing(os.replace, None, 32))
    with pytest.raises(PermissionError), fsops.one_wait():
        fsops.replace(tmp_path / "one", tmp_path / "x")
    assert waited_for(clock, lambda: fsops.replace(tmp_path / "two", tmp_path / "x")) == pytest.approx(
        fsops.HOLD_WAIT_S)


def test_a_nested_block_has_its_own_state(tmp_path, clock, monkeypatch):
    monkeypatch.setattr(os, "replace", refusing(os.replace, None, 32))
    call = lambda: fsops.replace(tmp_path / "a", tmp_path / "x")
    with fsops.one_wait():
        with fsops.one_wait():                         # a fresh inner block: the outer has not given up
            assert waited_for(clock, call) == pytest.approx(fsops.HOLD_WAIT_S)
            assert waited_for(clock, call) == pytest.approx(fsops.SHORT_WAIT_S)
        assert waited_for(clock, call) == pytest.approx(fsops.HOLD_WAIT_S)     # the inner give-up stayed inside
        assert waited_for(clock, call) == pytest.approx(fsops.SHORT_WAIT_S)
        with fsops.one_wait():                         # nested in a block that has given up: full limit
            assert waited_for(clock, call) == pytest.approx(fsops.HOLD_WAIT_S)
        assert waited_for(clock, call) == pytest.approx(fsops.SHORT_WAIT_S)     # the outer state came back


def test_another_thread_started_inside_a_block_has_the_full_limit(tmp_path, clock, monkeypatch):
    monkeypatch.setattr(os, "replace", refusing(os.replace, None, 32))
    got: list[float] = []
    with fsops.one_wait():
        waited_for(clock, lambda: fsops.replace(tmp_path / "a", tmp_path / "x"))
        th = threading.Thread(target=lambda: got.append(waited_for(clock, lambda: fsops.replace(tmp_path / "b",
                                                                                                tmp_path / "x"))))
        th.start()
        th.join()
    assert got == [pytest.approx(fsops.HOLD_WAIT_S)]


def test_sync_tree_table_with_five_held_paths_waits_once_in_full(tmp_path, clock, monkeypatch):
    tree, exp = tmp_path / "tree", tmp_path / "expected"
    rels = [f"m/f{i}.txt" for i in range(5)]
    for rel in rels:
        for root, body in ((tree, "changed"), (exp, "expected")):
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(body)
    held_destination(monkeypatch)
    with pytest.raises(intake.SyncFailed):
        intake.sync_tree_table(tree, lambda rel: exp / rel, {}, rels)
    assert sum(clock.pauses) == pytest.approx(fsops.HOLD_WAIT_S + 4 * fsops.SHORT_WAIT_S)
    assert sorted(x.name for x in (tree / "m").iterdir()) == [f"f{i}.txt" for i in range(5)]
    # right after it raised, a refused call has the full limit again
    assert waited_for(clock, lambda: fsops.replace(tmp_path / "tree" / "m" / "f0.txt", tmp_path / "tree" / "y")) \
        == pytest.approx(fsops.HOLD_WAIT_S)


def test_end_of_a_fix_loop_waits_once_in_full_for_a_lasting_holder(tmp_path, clock, monkeypatch, caplog):
    """`fixer._remove_leftovers`: three removals whose failure is only logged. A holder that lasts costs one full
    wait and two short ones, not three full ones, and the call after it has the full limit again."""
    patch_file, fixed_root, work = tmp_path / "fix.patch", tmp_path / "fixed", tmp_path / "tree"
    patch_file.write_text("p")
    for d in (fixed_root, work):
        d.mkdir()
        (d / "f").write_text("x")
    real_unlink = os.unlink
    held = {"on": True}

    def unlink(p, *a, **k):
        if held["on"]:
            raise win_error(32)
        return real_unlink(p, *a, **k)
    monkeypatch.setattr(os, "unlink", unlink)
    fixer._remove_leftovers(work, [patch_file, fixed_root])
    assert sum(clock.pauses) == pytest.approx(60 + 2 * 2)
    assert patch_file.exists() and fixed_root.exists() and work.exists()
    clock.pauses.clear()
    with pytest.raises(PermissionError):
        fsops.unlink(patch_file)
    assert sum(clock.pauses) == pytest.approx(60)
    held["on"] = False
    fixer._remove_leftovers(work, [])
    assert not work.exists() and fixed_root.exists()


def test_sync_tree_table_that_returns_leaves_the_full_limit(tmp_path, clock, monkeypatch):
    tree, exp = tmp_path / "tree", tmp_path / "expected"
    for root, body in ((tree, "changed"), (exp, "expected")):
        (root / "m").mkdir(parents=True)
        (root / "m" / "a.txt").write_text(body)
    assert intake.sync_tree_table(tree, lambda rel: exp / rel, {}, ["m/a.txt"]) == ([], ["m/a.txt"])
    give_up_once(tmp_path, monkeypatch)
    held_destination(monkeypatch)
    assert waited_for(clock, lambda: fsops.replace(tmp_path / "src", tmp_path / "tree" / "m" / "a.txt")) \
        == pytest.approx(fsops.HOLD_WAIT_S)


def test_take_over_does_not_wait(tmp_path, clock, monkeypatch):
    claim = chain.RunClaim.__new__(chain.RunClaim)
    claim.path = tmp_path / chain.CLAIM_NAME
    claim.path.write_text("{}")
    fake = refusing(os.rename, None, 5)
    monkeypatch.setattr(os, "rename", fake)
    with pytest.raises(PermissionError):
        claim._take_over(os.stat(claim.path), None)
    assert fake.state["calls"] == 1 and clock.pauses == [] and claim.path.exists()


# 4068.077… and 16382.473… are starts at which a loop ended by `waited >= limit` never ends (the last pause, cut
# to the time left, no longer moves the clock); 1e17 is one at which even a 0.05 s pause does not move it.
@pytest.mark.parametrize("start", [1e9, 4068.0771770492506, 16382.4730785581, 1e17])
def test_a_clock_at_a_large_value_still_ends_the_wait(tmp_path, monkeypatch, start):
    c = FakeClock(start)
    monkeypatch.setattr(fsops, "_now", c.now)
    monkeypatch.setattr(fsops, "_sleep", c.sleep)
    monkeypatch.setattr(os, "replace", refusing(os.replace, None, 5))
    with pytest.raises(PermissionError) as ei:
        fsops.replace(tmp_path / "a", tmp_path / "b")
    assert fsops.waited_out(ei.value) and len(c.pauses) < 100
    assert sum(c.pauses) == pytest.approx(fsops.HOLD_WAIT_S, abs=1e-2)
