"""Tests for the holder lookup of sbmig.remediations.fsops (INTERFACES "v2.1z-cf"): a refusal that lasts names the
programs that have the file open (Windows Restart Manager through ctypes).

The Restart Manager and kernel32 are replaced by fake library objects; the refusals are raised from the patched `os`
calls with `.winerror` set, and the clock of `fsops` is a fake, so nothing here sleeps for real."""
from __future__ import annotations

import ctypes
import logging
import os
import threading
import time
from ctypes import CFUNCTYPE, POINTER, c_int, c_uint32, c_void_p, c_wchar, c_wchar_p
from pathlib import Path
from typing import ClassVar

import pytest

from sbmig.remediations import fsops

LOGGER = "sbmig.remediations.fsops"
MORE_DATA = 234


# ---------------------------------------------------------------------------- helpers
class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.pauses: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        if len(self.pauses) > 10_000:
            raise AssertionError("the wait never ends")
        self.pauses.append(s)
        self.t += s


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


def refusing(real, times: int | None, winerror: int = 5):
    state = {"refused": 0}

    def fake(*a, **k):
        if times is None or state["refused"] < times:
            state["refused"] += 1
            raise win_error(winerror)
        return real(*a, **k)
    return fake


def messages(caplog, level=None) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER and (level is None or r.levelno == level)]


class FakeLibs:
    """Stands for rstrtmgr.dll and kernel32.dll. `procs` = [(pid, app name, full image path or None)]; `codes`
    overrides the answer of one call; `grow` = how many more processes appear after each ERROR_MORE_DATA."""

    def __init__(self, procs, codes=None, grow=0, more_data_forever=False):
        self.procs = list(procs)
        self.codes = dict(codes or {})
        self.grow = grow
        self.more_data_forever = more_data_forever
        self.calls: list[str] = []
        self.registered: list[str] = []
        self.ended: list[int] = []
        self.opened: list[int] = []
        self.closed: list[object] = []
        self.capacities: list[int] = []
        lib = self

        def RmStartSession(p_handle, flags, key):
            lib.calls.append("RmStartSession")
            assert flags == 0 and len(key) == 33
            if "RmStartSession" in lib.codes:
                return lib.codes["RmStartSession"]
            p_handle.contents.value = 7
            return 0

        def RmRegisterResources(h, n, names, n_apps, apps, n_svc, svcs):
            lib.calls.append("RmRegisterResources")
            assert h == 7 and n_apps == 0 and apps is None and n_svc == 0 and svcs is None
            lib.registered = [names[i] for i in range(n)]
            return lib.codes.get("RmRegisterResources", 0)

        def RmGetList(h, p_needed, p_count, infos, p_reasons):
            lib.calls.append("RmGetList")
            if "RmGetList" in lib.codes:
                return lib.codes["RmGetList"]
            cap = p_count.contents.value
            lib.capacities.append(cap)
            assert cap == len(infos)
            if lib.more_data_forever or cap < len(lib.procs):
                p_needed.contents.value = len(lib.procs)
                lib.procs += [(9000 + i, f"late{i}", None) for i in range(lib.grow)]
                return MORE_DATA
            for i, (pid, app, _img) in enumerate(lib.procs):
                infos[i].Process.dwProcessId = pid
                infos[i].strAppName = app
            p_count.contents.value = len(lib.procs)
            return 0

        def RmEndSession(h):
            lib.calls.append("RmEndSession")
            lib.ended.append(h)
            return 0

        def OpenProcess(access, inherit, pid):
            assert access == 0x1000 and not inherit
            lib.opened.append(pid)
            img = next((i for p, _a, i in lib.procs if p == pid), None)
            return None if img is None else 1000 + pid

        def QueryFullProcessImageNameW(h, flags, buf, p_size):
            assert flags == 0
            pid = h - 1000
            img = next(i for p, _a, i in lib.procs if p == pid)
            buf.value = img
            p_size.contents.value = len(img)
            return 1

        def CloseHandle(h):
            lib.closed.append(h)
            return 1

        for f in (RmStartSession, RmRegisterResources, RmGetList, RmEndSession):
            setattr(self, f.__name__, f)
        self.k32 = type("K32", (), {})()
        for f in (OpenProcess, QueryFullProcessImageNameW, CloseHandle):
            setattr(self.k32, f.__name__, f)


@pytest.fixture(autouse=True)
def lookups_end():
    """Each test leaves no lookup pending behind it (the record would fail the next test's lookup)."""
    yield
    deadline = time.monotonic() + 10
    while fsops._pending is not None and time.monotonic() < deadline:
        time.sleep(0.005)
    left = fsops._pending
    fsops._pending = None
    assert left is None, "a lookup was still pending 10 s after the test"


@pytest.fixture
def on_windows(monkeypatch):
    """Runs the default lookup as on Windows, with the fake libraries."""
    box: dict = {}

    def use(libs: FakeLibs) -> FakeLibs:
        box["libs"] = libs
        return libs
    monkeypatch.setattr(fsops, "_WINDOWS", True)
    monkeypatch.setattr(fsops, "_load_windows", lambda: (box["libs"], box["libs"].k32))
    return use


def held_file(tmp_path, name="result.zip"):
    p = tmp_path / name
    p.write_bytes(b"x")
    return p


# ---------------------------------------------------------------------------- holders(): names
def test_names_carry_app_exe_and_pid_and_never_a_path(tmp_path, on_windows):
    f = held_file(tmp_path)
    on_windows(FakeLibs([(4312, "Microsoft Word", r"C:\Program Files\Office\WINWORD.EXE"),
                         (77, "Explorer", None),
                         (88, "MsMpEng.exe", r"C:\ProgramData\Defender\MsMpEng.exe"),
                         (99, r"C:\tools\scan.exe", r"C:\tools\scan.exe")]))
    names = fsops.holders([f])
    assert names == ["Microsoft Word [WINWORD.EXE] (pid 4312)", "Explorer (pid 77)", "MsMpEng.exe (pid 88)",
                     "scan.exe (pid 99)"]
    assert not any("\\" in n or "/" in n for n in names)


def test_this_process_is_marked(tmp_path, on_windows):
    f = held_file(tmp_path)
    me = os.getpid()
    on_windows(FakeLibs([(me, "python.exe", r"C:\Python\python.exe"), (me + 1, "Python", r"C:\Python\python.exe")]))
    assert fsops.holders([f]) == [f"python.exe (pid {me}, this process)", f"Python [python.exe] (pid {me + 1})"]


def test_session_key_buffer_registration_and_end_session(tmp_path, on_windows):
    f = held_file(tmp_path)
    libs = on_windows(FakeLibs([(5, "Indexer", None)]))
    fsops.holders([f])
    assert libs.calls == ["RmStartSession", "RmRegisterResources", "RmGetList", "RmEndSession"]
    assert libs.registered == [str(f)] and libs.ended == [7]


def test_process_handles_are_closed(tmp_path, on_windows):
    f = held_file(tmp_path)
    libs = on_windows(FakeLibs([(5, "A", r"C:\a.exe"), (6, "B", None)]))
    fsops.holders([f])
    assert libs.opened == [5, 6] and libs.closed == [1005]


def test_more_data_grows_the_buffer(tmp_path, on_windows):
    f = held_file(tmp_path)
    procs = [(100 + i, f"app{i}", None) for i in range(20)]       # more than the first buffer of 16
    libs = on_windows(FakeLibs(procs, grow=3))                    # and 3 more appear between the two calls
    names = fsops.holders([f])
    assert len(names) == 23 and names[0] == "app0 (pid 100)" and names[-1] == "late2 (pid 9002)"
    assert libs.capacities[0] == 16 and libs.capacities[1] > 20 and len(libs.capacities) == 2


def test_more_data_forever_is_bounded(tmp_path, on_windows, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    f = held_file(tmp_path)
    libs = on_windows(FakeLibs([(1, "a", None)], more_data_forever=True))
    assert fsops.holders([f]) == []
    assert libs.calls.count("RmGetList") == fsops._RM_LIST_TRIES and libs.ended == [7]
    assert messages(caplog, logging.DEBUG) == [(f"remediation: the lookup of the programs holding result.zip failed: "
                                                f"RmGetList answered 234 {fsops._RM_LIST_TRIES} times")]


@pytest.mark.parametrize("call", ["RmStartSession", "RmRegisterResources", "RmGetList"])
def test_an_error_code_is_an_empty_answer_and_one_debug_line(tmp_path, on_windows, caplog, call):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    f = held_file(tmp_path)
    libs = on_windows(FakeLibs([(1, "a", None)], codes={call: 5}))
    assert fsops.holders([f]) == []
    debug = messages(caplog, logging.DEBUG)
    assert debug == [f"remediation: the lookup of the programs holding result.zip failed: {call} answered 5"]
    assert str(tmp_path) not in debug[0]
    assert libs.ended == ([] if call == "RmStartSession" else [7])      # a session that started is always ended


def test_open_process_failing_leaves_the_exe_out(tmp_path, on_windows):
    f = held_file(tmp_path)
    on_windows(FakeLibs([(5, "Defender", None)]))
    assert fsops.holders([f]) == ["Defender (pid 5)"]


def test_an_exception_inside_is_an_empty_answer(tmp_path, on_windows, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    f = held_file(tmp_path)
    libs = on_windows(FakeLibs([(1, "a", None)]))

    def broken(*a):
        raise OSError(f"bad {tmp_path}")
    libs.RmGetList = broken
    assert fsops.holders([f]) == []
    assert messages(caplog, logging.DEBUG) == [("remediation: the lookup of the programs holding result.zip failed: "
                                                "OSError")]
    assert libs.ended == [7]


def test_a_library_that_cannot_be_loaded(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    f = held_file(tmp_path)
    monkeypatch.setattr(fsops, "_WINDOWS", True)

    def fail():
        raise OSError("no rstrtmgr")
    monkeypatch.setattr(fsops, "_load_windows", fail)
    assert fsops.holders([f]) == []
    assert messages(caplog, logging.DEBUG)[0].endswith("Restart Manager not loaded (OSError)")


def test_off_windows_nothing_is_loaded(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    f = held_file(tmp_path)
    monkeypatch.setattr(fsops, "_WINDOWS", False)
    monkeypatch.setattr(fsops, "_load_windows", lambda: pytest.fail("loaded off Windows"))
    monkeypatch.setattr(fsops, "_rm_types", lambda: pytest.fail("ctypes types built off Windows"))
    assert fsops.holders([f]) == [] and messages(caplog) == []


def test_this_platform_has_the_default_lookup():
    assert fsops._lookup is fsops._restart_manager
    assert fsops._WINDOWS is (os.name == "nt")


# ---------------------------------------------------------------------------- files looked at
def test_directories_are_expanded_and_missing_paths_left_out(tmp_path, on_windows):
    (tmp_path / "d" / "sub").mkdir(parents=True)
    (tmp_path / "d" / "a.txt").write_text("1")
    (tmp_path / "d" / "sub" / "b.txt").write_text("2")
    single = held_file(tmp_path, "single.zip")
    libs = on_windows(FakeLibs([]))
    assert fsops.holders([tmp_path / "missing.zip", tmp_path / "d", single]) == []
    assert sorted(os.path.basename(p) for p in libs.registered) == ["a.txt", "b.txt", "single.zip"]
    assert libs.registered[-1] == str(single) and all(os.path.isabs(p) for p in libs.registered)


def test_only_missing_paths_register_nothing(tmp_path, on_windows):
    libs = on_windows(FakeLibs([(1, "a", None)]))
    assert fsops.holders([tmp_path / "gone", tmp_path / "also-gone"]) == [] and libs.calls == []


def test_the_file_cap_holds_over_all_paths(tmp_path, on_windows, monkeypatch):
    monkeypatch.setattr(fsops, "HOLDERS_MAX_FILES", 5)
    for d in ("a", "b"):
        (tmp_path / d).mkdir()
        for i in range(4):
            (tmp_path / d / f"{i}.txt").write_text("x")
    libs = on_windows(FakeLibs([]))
    fsops.holders([tmp_path / "a", tmp_path / "b"])
    assert len(libs.registered) == 5
    assert sum(1 for p in libs.registered if os.path.dirname(p) == str(tmp_path / "a")) == 4


def test_the_default_cap_is_500(tmp_path):
    assert fsops.HOLDERS_MAX_FILES == 500
    for i in range(510):
        (tmp_path / f"{i}.txt").write_text("")
    assert len(fsops._files([tmp_path])) == 500


# ---------------------------------------------------------------------------- the ctypes declarations
def test_every_called_function_is_declared():
    funcs = {}

    class Lib:
        def __getattr__(self, n):
            f = funcs.setdefault(n, type("F", (), {})())
            return f
    fsops._declare(Lib(), Lib())
    assert set(funcs) == {"RmStartSession", "RmRegisterResources", "RmGetList", "RmEndSession", "OpenProcess",
                          "QueryFullProcessImageNameW", "CloseHandle"}
    for n, f in funcs.items():
        assert f.argtypes is not None and f.restype is not None, n
    t = fsops._rm_types()
    assert funcs["RmGetList"].argtypes[3]._type_ is t.RM_PROCESS_INFO
    assert funcs["OpenProcess"].restype is ctypes.c_void_p


def test_structure_layout_follows_windows_sizes():
    t = fsops._rm_types()
    w = ctypes.sizeof(ctypes.c_wchar)                      # 2 on Windows, 4 on macOS / Linux
    assert ctypes.sizeof(t.RM_UNIQUE_PROCESS) == 12
    assert t.RM_PROCESS_INFO.strAppName.offset == 12
    assert t.RM_PROCESS_INFO.strServiceShortName.offset == 12 + 256 * w
    assert t.RM_PROCESS_INFO.ApplicationType.offset == 12 + 320 * w
    assert ctypes.sizeof(t.RM_PROCESS_INFO) == 12 + 320 * w + 16      # 668 on Windows
    assert [n for n, _ in t.RM_PROCESS_INFO._fields_] == ["Process", "strAppName", "strServiceShortName",
                                                          "ApplicationType", "AppStatus", "TSSessionId",
                                                          "bRestartable"]


# ---------------------------------------------------------------------------- log lines of a refused call
SENTENCE = "a virus scanner, an indexer or an editor that has the run folder open keeps a file open there"


def counting_lookup(answer=([], None), block: threading.Event | None = None, clock=None):
    """A `_lookup` stand-in that records each call (with the fake-clock time) and may block until `block` is set."""
    calls: list[tuple[float | None, list[str]]] = []

    def lookup(files):
        calls.append((clock.t if clock is not None else None, [os.path.basename(p) for p in files]))
        if block is not None:
            block.wait(30)
        return answer
    lookup.calls = calls  # type: ignore[attr-defined]
    return lookup


def wait_until_clear(seconds=5.0):
    deadline = time.monotonic() + seconds
    while fsops._pending is not None and time.monotonic() < deadline:
        time.sleep(0.002)
    assert fsops._pending is None


def test_warning_names_the_holders(tmp_path, clock, monkeypatch, caplog, on_windows):
    caplog.set_level(logging.INFO, logger=LOGGER)
    src, dst = held_file(tmp_path, ".result.next.zip"), held_file(tmp_path, "result.zip")
    libs = on_windows(FakeLibs([(4312, "Microsoft Word", r"C:\Office\WINWORD.EXE")]))
    monkeypatch.setattr(os, "replace", refusing(os.replace, None))
    with pytest.raises(PermissionError) as ei:
        fsops.replace(src, dst)
    assert fsops.waited_out(ei.value)
    warn = messages(caplog, logging.WARNING)
    assert len(warn) == 1
    assert warn[0].startswith("remediation: replace of .result.next.zip still refused after 60.0 s (")
    assert warn[0].endswith(", WinError 5): open in Microsoft Word [WINWORD.EXE] (pid 4312)")
    assert sorted(os.path.basename(p) for p in libs.registered) == [".result.next.zip", "result.zip"]
    assert not any(str(tmp_path) in m for m in messages(caplog))


def test_names_are_capped_at_eight(tmp_path, clock, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    f = held_file(tmp_path)
    monkeypatch.setattr(fsops, "_lookup", lambda files: ([f"p{i} (pid {i})" for i in range(11)], None))
    monkeypatch.setattr(os, "unlink", refusing(os.unlink, None, 32))
    with pytest.raises(PermissionError):
        fsops.unlink(f)
    w = messages(caplog, logging.WARNING)[0]
    assert w.endswith("WinError 32): open in " + ", ".join(f"p{i} (pid {i})" for i in range(8)) + " and 3 more")


def test_exactly_eight_names_have_no_more_part():
    names = [f"p{i}" for i in range(8)]
    assert fsops._names_text(names) == ", ".join(names)


class _CountingThread(threading.Thread):
    started: ClassVar[list[str]] = []

    def start(self):
        _CountingThread.started.append(self.name)
        super().start()


def test_a_call_that_goes_through_looks_nothing_up(tmp_path, clock, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    src = held_file(tmp_path, "a.partial")
    lookup = counting_lookup((["Word (pid 1)"], None))
    monkeypatch.setattr(fsops, "_lookup", lookup)
    _CountingThread.started = []
    monkeypatch.setattr(fsops.threading, "Thread", _CountingThread)
    monkeypatch.setattr(os, "replace", refusing(os.replace, 5))
    fsops.replace(src, tmp_path / "a.json")
    assert lookup.calls == [] and _CountingThread.started == []
    info = messages(caplog)
    assert len(info) == 2 and info[1] == "remediation: replace of a.partial went through after 1.6 s (6 tries)"


def test_nothing_is_looked_up_before_the_time_is_up(tmp_path, clock, monkeypatch, caplog):
    f = held_file(tmp_path)
    lookup = counting_lookup(clock=clock)
    monkeypatch.setattr(fsops, "_lookup", lookup)
    monkeypatch.setattr(os, "replace", refusing(os.replace, None))
    with pytest.raises(PermissionError):
        fsops.replace(f, tmp_path / "x")
    assert len(lookup.calls) == 1
    assert lookup.calls[0][0] == pytest.approx(fsops.HOLD_WAIT_S)       # only at the give-up


def test_nothing_found_names_the_number_of_files(tmp_path, clock, monkeypatch, caplog, on_windows):
    caplog.set_level(logging.INFO, logger=LOGGER)
    t = tmp_path / "tree"
    (t / "sub").mkdir(parents=True)
    for n in ("a", "b", "sub/c"):
        (t / n).write_text("x")
    on_windows(FakeLibs([]))
    monkeypatch.setattr(os, "unlink", refusing(os.unlink, None, 32))
    with pytest.raises(PermissionError):
        fsops.rmtree(t)
    w = messages(caplog, logging.WARNING)
    assert len(w) == 1
    assert w[0].startswith("remediation: rmtree of tree still refused after 60.0 s (")
    assert w[0].endswith(f"WinError 32): {SENTENCE} (no program found with the 3 file(s) looked at open)")


def test_a_failed_lookup_is_never_worded_no_program_found(tmp_path, clock, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    src, dst = held_file(tmp_path, "a.zip"), held_file(tmp_path, "b.zip")
    monkeypatch.setattr(fsops, "_lookup", lambda files: ([], "RmStartSession answered 353"))
    monkeypatch.setattr(os, "replace", refusing(os.replace, None))
    with pytest.raises(PermissionError):
        fsops.replace(src, dst)
    w = messages(caplog, logging.WARNING)
    assert w == [w[0]] and w[0].endswith(f"WinError 5): {SENTENCE} (the lookup of the holding program failed: "
                                         "RmStartSession answered 353)")
    assert "no program found" not in w[0]
    assert messages(caplog, logging.DEBUG) == [("remediation: replace of a.zip: the lookup of the holding program "
                                                "failed: RmStartSession answered 353")]


def test_a_lookup_that_never_returns_ends_the_lookups_until_it_does(tmp_path, clock, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    monkeypatch.setattr(fsops, "HOLDERS_WAIT_S", 0.3)
    release = threading.Event()
    lookup = counting_lookup((["Late (pid 1)"], None), block=release, clock=clock)
    monkeypatch.setattr(fsops, "_lookup", lookup)
    monkeypatch.setattr(os, "replace", refusing(os.replace, None))
    f = held_file(tmp_path)
    try:
        with pytest.raises(PermissionError):
            fsops.replace(f, tmp_path / "x")                       # gives up at fake t = 60, the lookup blocks
        assert messages(caplog, logging.WARNING)[-1].endswith(f"{SENTENCE} (the lookup of the holding program did "
                                                              "not return in 0.3 s)")
        t0 = time.monotonic()
        with pytest.raises(PermissionError):
            fsops.replace(f, tmp_path / "x")                       # gives up at fake t = 120
        assert time.monotonic() - t0 < 0.2                         # not another HOLDERS_WAIT_S
        assert messages(caplog, logging.WARNING)[-1].endswith(f"{SENTENCE} (the lookup of the holding program failed: "
                                                              "a lookup started 60 s ago has not returned)")
        assert len(lookup.calls) == 1
        assert messages(caplog, logging.DEBUG) == [("remediation: replace of result.zip: the lookup of the holding "
                                                    "program failed: a lookup started 60 s ago has not returned")]
    finally:
        release.set()
    wait_until_clear()
    with pytest.raises(PermissionError):
        fsops.replace(f, tmp_path / "x")                           # looked up again
    assert len(lookup.calls) == 2
    assert messages(caplog, logging.WARNING)[-1].endswith("): open in Late (pid 1)")


def test_holders_while_a_lookup_has_not_returned(tmp_path, clock, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    release = threading.Event()
    lookup = counting_lookup(block=release)
    monkeypatch.setattr(fsops, "_lookup", lookup)
    f = held_file(tmp_path)
    try:
        assert fsops._look((f,), 0.01) is None                    # pending
        clock.t += 7
        assert fsops.holders(f) == []
        assert messages(caplog, logging.DEBUG) == [("remediation: the lookup of the programs holding result.zip "
                                                    "failed: a lookup started 7 s ago has not returned")]
        assert len(lookup.calls) == 1
    finally:
        release.set()
    wait_until_clear()


def test_a_holders_call_counts_as_the_process_lookup(tmp_path, clock, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    release, entered = threading.Event(), threading.Event()

    def lookup(files):
        entered.set()
        release.wait(30)
        return ["A (pid 1)"], None
    monkeypatch.setattr(fsops, "_lookup", lookup)
    f = held_file(tmp_path)
    out = []
    th = threading.Thread(target=lambda: out.append(fsops.holders([f])))
    th.start()
    try:
        assert entered.wait(5)
        monkeypatch.setattr(os, "unlink", refusing(os.unlink, None, 32))
        with pytest.raises(PermissionError):
            fsops.unlink(f)
        assert messages(caplog, logging.WARNING)[-1].endswith("(the lookup of the holding program failed: a lookup "
                                                              "started 60 s ago has not returned)")
    finally:
        release.set()
        th.join(5)
    assert out == [["A (pid 1)"]] and fsops._pending is None


def test_no_lookup_inside_a_block_after_a_give_up(tmp_path, clock, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    lookup = counting_lookup((["Scanner (pid 9)"], None))
    monkeypatch.setattr(fsops, "_lookup", lookup)
    monkeypatch.setattr(os, "unlink", refusing(os.unlink, None, 32))
    files = [held_file(tmp_path, f"f{i}") for i in range(4)]
    with fsops.one_wait():
        for f in files:
            with pytest.raises(PermissionError):
                fsops.unlink(f)
    assert len(lookup.calls) == 1
    w = messages(caplog, logging.WARNING)
    assert len(w) == 4 and w[0].endswith("): open in Scanner (pid 9)")
    for line in w[1:]:
        assert line.endswith(f"WinError 32): {SENTENCE}")             # the v2.1z-ce sentence alone
    with pytest.raises(PermissionError):                               # outside the block: a lookup again
        fsops.unlink(files[0])
    assert len(lookup.calls) == 2


def _thread_whose_start_raises(exc, after_start=False):
    class T(threading.Thread):
        def start(self):
            if after_start:
                super().start()
            raise exc
    return T


def test_a_thread_that_cannot_start_is_a_failed_lookup(tmp_path, clock, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    f = held_file(tmp_path)
    monkeypatch.setattr(fsops.threading, "Thread", _thread_whose_start_raises(RuntimeError("can't start new thread")))
    monkeypatch.setattr(os, "replace", refusing(os.replace, None))
    with pytest.raises(PermissionError):
        fsops.replace(f, tmp_path / "y")
    assert messages(caplog, logging.WARNING)[0].endswith("(the lookup of the holding program failed: lookup not "
                                                         "started (RuntimeError))")
    assert fsops._pending is None


@pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit])
def test_an_interrupt_at_thread_start_propagates_and_clears_the_record(tmp_path, clock, monkeypatch, exc):
    monkeypatch.setattr(fsops.threading, "Thread", _thread_whose_start_raises(exc()))
    monkeypatch.setattr(os, "replace", refusing(os.replace, None))
    with pytest.raises(exc):
        fsops.replace(held_file(tmp_path), tmp_path / "y")
    assert fsops._pending is None


def test_an_interrupt_after_the_thread_started_leaves_the_record_to_the_thread(tmp_path, clock, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(fsops, "_lookup", counting_lookup(block=release))
    monkeypatch.setattr(fsops.threading, "Thread", _thread_whose_start_raises(KeyboardInterrupt(), after_start=True))
    try:
        with pytest.raises(KeyboardInterrupt):
            fsops._look((held_file(tmp_path),), 5)
        assert fsops._pending is not None                       # the running thread still owns it
    finally:
        release.set()
    wait_until_clear()


@pytest.mark.parametrize("exc", [RuntimeError("x"), SystemExit(1), KeyboardInterrupt()])
def test_the_record_is_cleared_whatever_happens_in_the_helper(tmp_path, exc, monkeypatch):
    def boom(paths):
        raise exc
    monkeypatch.setattr(fsops, "_find", boom)
    assert fsops._look((tmp_path,), 5) == ([], f"lookup failed ({type(exc).__name__})", 0)
    wait_until_clear()


def test_the_whole_give_up_is_bounded_on_the_real_clock(tmp_path, monkeypatch):
    """Real clock for the lookup wait (the waiting itself on the fake clock, so it costs nothing)."""
    c = FakeClock()
    monkeypatch.setattr(fsops, "_now", c.now)
    monkeypatch.setattr(fsops, "_sleep", c.sleep)
    monkeypatch.setattr(fsops, "HOLDERS_WAIT_S", 0.2)
    release = threading.Event()
    monkeypatch.setattr(fsops, "_lookup", counting_lookup(block=release))
    monkeypatch.setattr(os, "replace", refusing(os.replace, None))
    t0 = time.monotonic()
    try:
        with pytest.raises(PermissionError):
            fsops.replace(held_file(tmp_path), tmp_path / "x")
        assert time.monotonic() - t0 < 0.2 + 0.5
    finally:
        release.set()
    wait_until_clear()


def test_nothing_is_logged_from_the_helper_thread(tmp_path, clock, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    f = held_file(tmp_path)
    seen_threads = []

    def failing(files):
        seen_threads.append(threading.get_ident())
        logging.getLogger(LOGGER).debug("from the helper")          # what the module must never do; filtered below
        return [], "RmGetList answered 5"
    monkeypatch.setattr(fsops, "_lookup", failing)
    monkeypatch.setattr(os, "replace", refusing(os.replace, None))
    with pytest.raises(PermissionError):
        fsops.replace(f, tmp_path / "x")
    me = threading.get_ident()
    assert seen_threads and all(t != me for t in seen_threads)
    ours = [r for r in caplog.records if r.name == LOGGER and r.getMessage() != "from the helper"]
    assert ours and all(r.thread == me for r in ours)


def test_a_lookup_that_raises_never_reaches_the_caller(tmp_path, clock, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    f = held_file(tmp_path)

    def boom(files):
        raise RuntimeError("bad")
    monkeypatch.setattr(fsops, "_lookup", boom)
    monkeypatch.setattr(os, "replace", refusing(os.replace, None))
    with pytest.raises(PermissionError) as ei:
        fsops.replace(f, tmp_path / "x")
    assert ei.value.winerror == 5  # type: ignore[attr-defined]
    assert messages(caplog, logging.WARNING)[0].endswith("(the lookup of the holding program failed: RuntimeError)")


def test_the_failure_text_carries_no_names(tmp_path, clock, monkeypatch):
    f = held_file(tmp_path)
    monkeypatch.setattr(fsops, "_lookup", lambda files: (["Word (pid 1)"], None))
    monkeypatch.setattr(os, "replace", refusing(os.replace, None))
    with pytest.raises(PermissionError) as ei:
        fsops.replace(f, tmp_path / "x")
    assert "Word" not in str(ei.value)


def test_a_refused_directory_rename_holds_nothing_under_it_while_retried(tmp_path, monkeypatch):
    """With the own-handle guard (rule 6): nothing of ours is open under a directory while its rename is retried."""
    from .own_handles import guard, supported
    if not supported():
        pytest.skip("the own-handle guard does not run here")
    monkeypatch.setattr(fsops, "_sleep", lambda s: time.sleep(0.0005))   # retries spread over a few ms
    t = {"v": 0.0}

    def now():
        t["v"] += 0.001
        return t["v"]
    monkeypatch.setattr(fsops, "_now", now)
    hits = []
    for loop in range(20):
        src = tmp_path / f"accepted{loop}.partial"
        for d in range(20):
            (src / f"d{d}").mkdir(parents=True)
            for i in range(5):
                (src / f"d{d}" / f"{i}.java").write_text("x")
        with guard() as g:
            guarded = os.rename
            n = {"tries": 0}

            def rename(a, b, g=g, guarded=guarded, n=n):
                n["tries"] += 1
                if n["tries"] <= 30:      # the give-up comes after 36 tries
                    hits.extend(g.conflicts("rename", g._targets("rename", (a, b))))
                    raise win_error(5)
                return guarded(a, b)
            monkeypatch.setattr(os, "rename", rename)
            fsops.rename(src, tmp_path / f"accepted{loop}")
            assert not g.findings, g.report()
        assert (tmp_path / f"accepted{loop}").is_dir()
    assert not hits, "\n".join(str(h) for h in hits[:5])


@pytest.mark.parametrize("one", ["str", "bytes", "path"])
def test_a_single_path_is_a_list_of_one(tmp_path, on_windows, one):
    f = held_file(tmp_path)
    libs = on_windows(FakeLibs([(5, "Indexer", None)]))
    arg = {"str": str(f), "bytes": os.fsencode(str(f)), "path": Path(f)}[one]
    assert fsops.holders(arg) == ["Indexer (pid 5)"]
    assert libs.registered == [str(f)]


def test_holders_never_raises_on_a_bad_argument(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    assert fsops.holders(42) == [] and fsops.holders([object()]) == []
    assert len(messages(caplog, logging.DEBUG)) == 2


@pytest.mark.parametrize(("app", "shown"), [
    ("AC/DC Player", "AC/DC Player"),
    ("Microsoft Word", "Microsoft Word"),
    (r"C:\tools\scan.exe", "scan.exe"),
    (r"\\server\share\x.exe", "x.exe"),
    ("//fileserver/share/run/a.exe", "a.exe"),
    ("/opt/run/tool", "tool"),
    ("run/tool", "tool"),
])
def test_only_a_path_like_application_name_is_cut(app, shown):
    assert fsops._entry(app, "", 1, 0) == f"{shown} (pid 1)"


def test_the_types_are_built_once_across_threads(monkeypatch):
    monkeypatch.setattr(fsops, "_RM_TYPES", None)

    def slow_build():
        time.sleep(0.05)                  # wide enough for every thread to arrive while one builds
        return object()
    monkeypatch.setattr(fsops, "_build_rm_types", slow_build)
    got = []
    barrier = threading.Barrier(8)

    def build():
        barrier.wait()
        got.append(fsops._rm_types())
    ts = [threading.Thread(target=build) for _ in range(8)]
    for th in ts:
        th.start()
    for th in ts:
        th.join()
    assert len({id(x) for x in got}) == 1


# ---------------------------------------------------------------------------- real ctypes conversion
def ctypes_libs(procs, more_data_first=False, seen=None):
    """rstrtmgr / kernel32 stand-ins as REAL ctypes function pointers with the documented signatures, so every
    argument `_rm_holders` passes goes through the `argtypes` `_declare` set (also on POSIX)."""
    t = fsops._rm_types()
    seen = {} if seen is None else seen
    DWORD = UINT = c_uint32
    state = {"calls": 0}

    @CFUNCTYPE(DWORD, POINTER(DWORD), DWORD, POINTER(c_wchar))
    def RmStartSession(p_handle, flags, key):
        p_handle[0] = 7
        for i, ch in enumerate("0123456789abcdef0123456789abcdef"):
            key[i] = ch
        key[32] = "\0"
        seen["flags"] = flags
        return 0

    @CFUNCTYPE(DWORD, DWORD, UINT, POINTER(c_wchar_p), UINT, POINTER(t.RM_UNIQUE_PROCESS), UINT, POINTER(c_wchar_p))
    def RmRegisterResources(h, n, names, napps, apps, nsvc, svcs):
        seen["registered"] = [names[i] for i in range(n)]
        seen["nulls"] = (napps, bool(apps), nsvc, bool(svcs))
        seen["h"] = h
        return 0

    @CFUNCTYPE(DWORD, DWORD, POINTER(UINT), POINTER(UINT), POINTER(t.RM_PROCESS_INFO), POINTER(DWORD))
    def RmGetList(h, p_needed, p_count, infos, p_reasons):
        state["calls"] += 1
        cap = p_count[0]
        if (more_data_first and state["calls"] == 1) or cap < len(procs):
            p_needed[0] = len(procs)
            return MORE_DATA
        for i, (pid, app, _img) in enumerate(procs):
            infos[i].Process.dwProcessId = pid
            infos[i].strAppName = app
        p_count[0] = len(procs)
        p_reasons[0] = 0
        return 0

    @CFUNCTYPE(DWORD, DWORD)
    def RmEndSession(h):
        seen["ended"] = h
        return 0

    @CFUNCTYPE(c_void_p, DWORD, c_int, DWORD)
    def OpenProcess(access, inherit, pid):
        seen.setdefault("access", set()).add(access)
        img = next((i for p, _a, i in procs if p == pid), None)
        return None if img is None else 0x7FFF00000000 + pid      # a handle above 32 bits

    @CFUNCTYPE(c_int, c_void_p, DWORD, POINTER(c_wchar), POINTER(DWORD))
    def QueryFullProcessImageNameW(h, flags, buf, p_size):
        pid = h - 0x7FFF00000000
        img = next(i for p, _a, i in procs if p == pid)
        if len(img) + 1 > p_size[0]:
            return 0
        for i, ch in enumerate(img):
            buf[i] = ch
        buf[len(img)] = "\0"
        p_size[0] = len(img)
        return 1

    @CFUNCTYPE(c_int, c_void_p)
    def CloseHandle(h):
        seen.setdefault("closed", []).append(h)
        return 1

    class Lib:
        pass
    rm, k32, keep = Lib(), Lib(), []
    for target, funcs in ((rm, {"RmStartSession": RmStartSession, "RmRegisterResources": RmRegisterResources,
                                "RmGetList": RmGetList, "RmEndSession": RmEndSession}),
                          (k32, {"OpenProcess": OpenProcess, "QueryFullProcessImageNameW": QueryFullProcessImageNameW,
                                 "CloseHandle": CloseHandle})):
        for name, f in funcs.items():
            keep.append(f)
            # a fresh function pointer object on the same code, so `_declare` sets argtypes on it, not on the callback
            setattr(target, name, type(f).from_address(ctypes.addressof(f)))
    rm.keep = keep
    fsops._declare(rm, k32)
    return rm, k32, seen


def test_real_ctypes_round_trip(tmp_path):
    files = [str(held_file(tmp_path, "a.zip")), str(held_file(tmp_path, "b.zip"))]
    procs = [(4312, "Microsoft Word", r"C:\Office\WINWORD.EXE"), (77, "Explorer", None),
             (os.getpid(), "python.exe", r"C:\Python\python.exe")]
    rm, k32, seen = ctypes_libs(procs, more_data_first=True)
    names, reason = fsops._rm_holders(files, rm, k32)
    assert reason is None, reason
    assert names == ["Microsoft Word [WINWORD.EXE] (pid 4312)", "Explorer (pid 77)",
                     f"python.exe (pid {os.getpid()}, this process)"]
    assert seen["registered"] == files and seen["nulls"] == (0, False, 0, False) and seen["h"] == 7
    assert seen["flags"] == 0 and seen["ended"] == 7 and seen["access"] == {0x1000}
    assert seen["closed"] == [0x7FFF00000000 + 4312, 0x7FFF00000000 + os.getpid()]


def test_real_ctypes_many_processes(tmp_path):
    procs = [(100 + i, f"app{i}", None) for i in range(40)]
    rm, k32, _seen = ctypes_libs(procs)
    names, reason = fsops._rm_holders([str(held_file(tmp_path))], rm, k32)
    assert reason is None and len(names) == 40 and names[-1] == "app39 (pid 139)"
