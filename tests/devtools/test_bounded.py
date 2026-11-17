"""Tests for scripts/bounded.py: the file / time / temp / floor walls of the wrapper and the DiskWatch logic.

Rules these tests follow (they are the point of the tool): every file a test writes is a few MiB at most, every
subprocess has a timeout <= 30 s and is killed in ``finally``, every writing loop has its own count AND time cap, all
data lives under ``tmp_path``, the floor and growth rules are tested with fakes (or an absurd floor), and
``RLIMIT_FSIZE`` is only lowered in subprocesses, never in this pytest process.
"""
from __future__ import annotations

import importlib.util
import os
import re
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "bounded.py"

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the wrapper's walls are POSIX process walls")


def _load():
    spec = importlib.util.spec_from_file_location("_bounded_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bounded = _load()
MIB = 1024 * 1024


def _env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("BOUNDED") and k not in ("PYTEST_CURRENT_TEST", "PYTEST_ADDOPTS")}
    env.update(extra)
    return env


def run_wrapper(tmp_path: Path, opts: list[str], command: list[str], *, timeout: float = 30,
                env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run the wrapper with ``--tmp <tmp_path>/base`` and the given options; killed in ``finally``."""
    base = tmp_path / "base"
    args = [sys.executable, str(SCRIPT), "--tmp", str(base), *opts, "--", *command]
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env=env if env is not None else _env())
    try:
        out, err = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(args, proc.returncode, out, err)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()


def py(code: str) -> list[str]:
    return [sys.executable, "-c", textwrap.dedent(code)]


def summary_line(stderr: str) -> str:
    lines = [ln for ln in stderr.splitlines() if ln.strip()]
    assert lines, "no stderr at all"
    return lines[-1]


SUMMARY_RE = re.compile(r"^bounded: exit=(-?\d+) seconds=\d+\.\d tmp=\d+ MiB file-cap=(\d+ MiB|off) "
                        r"floor=(\d+ MiB|off)$")


def wait_gone(pid: int, timeout_s: float = 8.0) -> bool:
    """True when ``pid`` no longer exists (a zombie being reaped by init may need a moment)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return True  # the pid was reused by another user's process
        time.sleep(0.1)
    return False


def read_pid(path: Path, timeout_s: float = 10.0) -> int:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            text = path.read_text().strip()
            if text:
                return int(text)
        time.sleep(0.05)
    raise AssertionError(f"{path} was never written")


def kill_quietly(pid: int | None) -> None:
    if pid is None:
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


# ------------------------------------------------------------------------------------------------- file cap

# The writer's own caps: at most 48 chunks of 64 KiB (3 MiB) and 10 s, whatever the wall does.
WRITE_3_MIB = """
import sys, time
deadline = time.monotonic() + 10
with open(sys.argv[1], "wb") as fh:
    for _ in range(48):
        if time.monotonic() > deadline:
            break
        fh.write(b"x" * 65536)
        fh.flush()
"""


def test_file_cap_python_writer_gets_efbig(tmp_path):
    target = tmp_path / "big.bin"
    r = run_wrapper(tmp_path, ["--file-mb", "1", "--seconds", "20", "--floor-mb", "0", "--tmp-mb", "0"],
                    [*py(WRITE_3_MIB), str(target)])
    assert r.returncode != 0
    assert "File too large" in r.stderr or "Errno 27" in r.stderr, r.stderr
    assert target.stat().st_size <= 1 * MIB
    assert SUMMARY_RE.match(summary_line(r.stderr)), r.stderr
    assert "file-cap=1 MiB" in summary_line(r.stderr)


def test_file_cap_non_python_writer_is_killed_by_sigxfsz(tmp_path):
    target = tmp_path / "big.bin"
    r = run_wrapper(tmp_path, ["--file-mb", "1", "--seconds", "20", "--floor-mb", "0", "--tmp-mb", "0"],
                    ["sh", "-c", f"head -c 3145728 /dev/zero > '{target}'"])
    assert r.returncode == 128 + signal.SIGXFSZ, r.stderr
    assert target.stat().st_size <= 1 * MIB


# ------------------------------------------------------------------------------------------------- time cap

def test_time_cap_kills_the_whole_process_group(tmp_path):
    pidfile = tmp_path / "grandchild.pid"
    code = f"""
    import subprocess, time
    p = subprocess.Popen(["sleep", "60"])
    open({str(pidfile)!r}, "w").write(str(p.pid))
    time.sleep(60)
    """
    grandchild = None
    t0 = time.monotonic()
    try:
        r = run_wrapper(tmp_path, ["--seconds", "1", "--floor-mb", "0", "--tmp-mb", "0"], py(code))
        elapsed = time.monotonic() - t0
        grandchild = read_pid(pidfile, timeout_s=1)
        assert r.returncode == bounded.EXIT_TIME, r.stderr
        assert elapsed < 10
        first = next(ln for ln in r.stderr.splitlines() if ln.startswith("bounded:"))
        assert first.startswith("bounded: STOPPED:") and "--seconds 1" in first
        assert wait_gone(grandchild), "the grandchild survived the process-group kill"
    finally:
        kill_quietly(grandchild)


# ------------------------------------------------------------------------------------------------- temp cap

def test_temp_total_cap_stops_the_run(tmp_path):
    # at most 14 files of 1 MiB (14 MiB) and 12 s, independent of the wall under test
    code = """
    import os, tempfile, time
    d = tempfile.gettempdir()
    deadline = time.monotonic() + 12
    for i in range(14):
        if time.monotonic() > deadline:
            break
        with open(os.path.join(d, f"f{i}.bin"), "wb") as fh:
            fh.write(b"x" * (1024 * 1024))
        time.sleep(0.3)
    """
    t0 = time.monotonic()
    r = run_wrapper(tmp_path, ["--tmp-mb", "4", "--seconds", "20", "--floor-mb", "0"], py(code))
    assert r.returncode == bounded.EXIT_DISK, r.stderr
    assert time.monotonic() - t0 < 12
    first = next(ln for ln in r.stderr.splitlines() if ln.startswith("bounded:"))
    assert first.startswith("bounded: STOPPED:") and "BOUNDED_GROWTH_MB" in first, r.stderr
    assert not list((tmp_path / "base").glob("bounded-*")), "the run folder was not removed"


# ------------------------------------------------------------------------------------------------- refusal

def test_refusal_below_the_floor_never_starts_the_command(tmp_path):
    marker = tmp_path / "ran.marker"
    r = run_wrapper(tmp_path, ["--floor-mb", "999999999"], ["touch", str(marker)])
    assert r.returncode == bounded.EXIT_REFUSED
    assert r.stderr.startswith("bounded: REFUSED:"), r.stderr
    assert not marker.exists()
    assert SUMMARY_RE.match(summary_line(r.stderr))


def test_refusal_through_the_environment(tmp_path):
    marker = tmp_path / "ran.marker"
    r = run_wrapper(tmp_path, [], ["touch", str(marker)], env=_env(BOUNDED_FLOOR_MB="999999999"))
    assert r.returncode == bounded.EXIT_REFUSED
    assert not marker.exists()


# ------------------------------------------------------------------------------------------------- exit codes

def test_exit_code_passes_through(tmp_path):
    r = run_wrapper(tmp_path, ["--seconds", "20", "--floor-mb", "0"], py("raise SystemExit(7)"))
    assert r.returncode == 7
    line = summary_line(r.stderr)
    assert SUMMARY_RE.match(line) and line.startswith("bounded: exit=7 ")


def test_death_by_signal_is_128_plus_n(tmp_path):
    r = run_wrapper(tmp_path, ["--seconds", "20", "--floor-mb", "0"],
                    py("import os, signal; os.kill(os.getpid(), signal.SIGTERM)"))
    assert r.returncode == 128 + signal.SIGTERM


def test_summary_line_states_the_limits(tmp_path):
    r = run_wrapper(tmp_path, ["--file-mb", "0", "--floor-mb", "0", "--seconds", "20"], ["true"])
    assert r.returncode == 0
    line = summary_line(r.stderr)
    assert SUMMARY_RE.match(line), line
    assert "file-cap=off" in line and "floor=off" in line
    assert len([ln for ln in r.stderr.splitlines() if ln.startswith("bounded: exit=")]) == 1


# ------------------------------------------------------------------------------------------------- temp folder

SHOW_TMP = """
import os, tempfile
print(tempfile.gettempdir())
print(os.environ["TMPDIR"], os.environ["TEMP"], os.environ["TMP"], os.environ["BOUNDED"])
open(os.path.join(tempfile.gettempdir(), "small.txt"), "w").write("hello")
"""


def test_tmpdir_points_inside_the_run_folder_and_is_removed(tmp_path):
    r = run_wrapper(tmp_path, ["--seconds", "20", "--floor-mb", "0"], py(SHOW_TMP))
    assert r.returncode == 0, r.stderr
    gettemp, envline = r.stdout.splitlines()[:2]
    run_dir = Path(gettemp)
    assert run_dir.parent.resolve() == (tmp_path / "base").resolve()
    assert re.fullmatch(r"bounded-\d{8}-\d{6}-\d+", run_dir.name)
    tmpdir, temp, tmp, flag = envline.split()
    assert Path(tmpdir) == Path(temp) == Path(tmp) == run_dir and flag == "1"
    assert not run_dir.exists()


def test_keep_leaves_the_run_folder(tmp_path):
    r = run_wrapper(tmp_path, ["--seconds", "20", "--floor-mb", "0", "--keep"], py(SHOW_TMP))
    assert r.returncode == 0, r.stderr
    run_dir = Path(r.stdout.splitlines()[0])
    assert (run_dir / "small.txt").read_text() == "hello"
    assert f"bounded: kept {run_dir}" in r.stderr


# ------------------------------------------------------------------------------------------------- signals

def test_sigterm_to_the_wrapper_kills_the_child(tmp_path):
    pidfile = tmp_path / "child.pid"
    code = f"""
    import os, time
    open({str(pidfile)!r}, "w").write(str(os.getpid()))
    time.sleep(60)
    """
    args = [sys.executable, str(SCRIPT), "--tmp", str(tmp_path / "base"), "--seconds", "25", "--floor-mb", "0",
            "--", *py(code)]
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=_env())
    child = None
    try:
        child = read_pid(pidfile)
        proc.send_signal(signal.SIGTERM)
        _, err = proc.communicate(timeout=20)
        assert proc.returncode == 128 + signal.SIGTERM, err
        assert "SIGTERM" in err
        assert wait_gone(child), "the child survived SIGTERM to the wrapper"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
        kill_quietly(child)


def test_leftover_background_process_is_killed_after_a_normal_exit(tmp_path):
    pidfile = tmp_path / "bg.pid"
    code = f"""
    import subprocess
    p = subprocess.Popen(["sleep", "60"])
    open({str(pidfile)!r}, "w").write(str(p.pid))
    """
    bg = None
    try:
        r = run_wrapper(tmp_path, ["--seconds", "20", "--floor-mb", "0"], py(code))
        bg = read_pid(pidfile, timeout_s=1)
        assert r.returncode == 0, r.stderr
        assert wait_gone(bg), "the background process outlived the command"
    finally:
        kill_quietly(bg)


# ------------------------------------------------------------------------------------------------- usage

@pytest.mark.parametrize("opts, cmd, env", [
    ([], [], {}),                                        # no command
    (["--seconds", "abc"], ["true"], {}),                # bad number
    (["--file-mb", "-1"], ["true"], {}),                 # negative number
    ([], ["true"], {"BOUNDED_FILE_MB": "lots"}),         # bad environment value
    ([], ["true"], {"BOUNDED_GROWTH_MB": "1.5"}),
    (["--no-such-option"], ["true"], {}),
])
def test_usage_errors_exit_2(tmp_path, opts, cmd, env):
    marker = tmp_path / "ran.marker"
    command = ["touch", str(marker)] if cmd else []
    r = run_wrapper(tmp_path, opts, command, env=_env(**env)) if command else \
        _run_raw([sys.executable, str(SCRIPT), "--tmp", str(tmp_path / "base"), *opts], env=_env(**env))
    assert r.returncode == bounded.EXIT_USAGE, r.stderr
    assert "usage" in r.stderr.lower()
    assert not marker.exists()


def _run_raw(args, env):
    return subprocess.run(args, check=False, capture_output=True, text=True, timeout=30, env=env)


def test_missing_resource_module_requires_file_mb_0(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(bounded, "resource", None)
    assert bounded.cap_file_size(4) is None
    code = bounded.main(["--tmp", str(tmp_path / "base"), "--floor-mb", "0", "--", "true"])
    assert code == bounded.EXIT_USAGE
    assert "not supported on this platform, pass --file-mb 0" in capsys.readouterr().err
    assert not (tmp_path / "base").exists() or not list((tmp_path / "base").glob("bounded-*"))


# ------------------------------------------------------------------------------------------------- cap_file_size

CAP_PROBE = f"""
import importlib.util, json, resource
spec = importlib.util.spec_from_file_location("b", {str(SCRIPT)!r})
b = importlib.util.module_from_spec(spec); spec.loader.exec_module(b)
out = {{}}
hard0 = resource.getrlimit(resource.RLIMIT_FSIZE)[1]
out["off"] = b.cap_file_size(0)
out["two"] = b.cap_file_size(2)
out["hard_kept"] = resource.getrlimit(resource.RLIMIT_FSIZE)[1] == hard0
resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, hard0))
out["higher_asked"] = b.cap_file_size(4)
out["soft_after"] = resource.getrlimit(resource.RLIMIT_FSIZE)[0]
print(json.dumps(out))
"""


def test_cap_file_size_in_a_subprocess():
    import json
    r = subprocess.run([sys.executable, "-c", CAP_PROBE], check=False, capture_output=True, text=True, timeout=30, env=_env())
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["off"] is None
    assert out["two"] == 2 * MIB
    assert out["hard_kept"] is True
    assert out["higher_asked"] == 1 * MIB          # an existing lower limit is never raised
    assert out["soft_after"] == 1 * MIB


# ------------------------------------------------------------------------------------------------- tree / free

def test_tree_mb_counts_regular_files_only(tmp_path):
    root = tmp_path / "t"
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "one.bin").write_bytes(b"x" * MIB)
    (root / "a" / "b" / "two.bin").write_bytes(b"x" * (2 * MIB))
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"x" * (3 * MIB))
    (root / "link.bin").symlink_to(outside)
    (root / "dirlink").symlink_to(tmp_path, target_is_directory=True)
    assert bounded.tree_mb(root) == 3
    assert bounded.tree_mb(root, stop_above_mb=1) > 1
    assert bounded.tree_mb(tmp_path / "missing") == 0
    (root / "tiny").write_bytes(b"x")
    assert bounded.tree_mb(root) == 4               # rounded up: one byte above 3 MiB is above 3


def test_free_mb_of_a_missing_path_uses_its_ancestor(tmp_path):
    assert bounded.free_mb(tmp_path / "not" / "there") == bounded.free_mb(tmp_path)
    assert bounded.free_mb(tmp_path) >= 0


# ------------------------------------------------------------------------------------------------- DiskWatch

class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeTree:
    """Returns ``size`` and advances the clock by ``walk_s`` per walk, recording each call."""

    def __init__(self, clock: FakeClock, size: int = 0, walk_s: float = 0.0) -> None:
        self.clock, self.size, self.walk_s, self.calls = clock, size, walk_s, []

    def __call__(self, path, *, stop_above_mb=None):
        self.calls.append((path, stop_above_mb))
        self.clock.now += self.walk_s
        return self.size


def make_watch(*, floor_mb=0, growth_mb=0, free=lambda p: 10_000, tree=None, clock=None, path=Path("/x"),
               min_interval_s=1.0):
    clock = clock or FakeClock()
    tree = tree or FakeTree(clock)
    holder = {"path": path}
    w = bounded.DiskWatch(floor_mb=floor_mb, growth_mb=growth_mb, tree_path=lambda: holder["path"],
                          floor_path=Path("/vol"), free=free, tree=tree, clock=clock,
                          min_interval_s=min_interval_s)
    return w, clock, tree, holder


def test_watch_floor_trip_names_both_numbers():
    w, *_ = make_watch(floor_mb=5000, free=lambda p: 4999)
    reason = w.check()
    assert reason and "4999" in reason and "5000" in reason and "BOUNDED_FLOOR_MB" in reason


def test_watch_floor_is_checked_on_every_call():
    free = {"v": 9000}
    w, *_ = make_watch(floor_mb=5000, growth_mb=100, free=lambda p: free["v"])
    assert w.check() is None
    free["v"] = 10                                   # no time passed: the floor still trips at once
    assert w.check() is not None


def test_watch_growth_trip_names_size_path_cap():
    clock = FakeClock()
    tree = FakeTree(clock, size=101)
    w, *_ = make_watch(growth_mb=100, tree=tree, clock=clock, path=Path("/run/tmp"))
    reason = w.check()
    assert reason and "101" in reason and "/run/tmp" in reason and "100" in reason
    assert "BOUNDED_GROWTH_MB" in reason
    assert tree.calls == [(Path("/run/tmp"), 100)]


def test_watch_growth_at_the_cap_is_not_a_trip():
    clock = FakeClock()
    w, *_ = make_watch(growth_mb=100, tree=FakeTree(clock, size=100), clock=clock)
    assert w.check() is None


def test_watch_rate_limit_min_interval():
    clock = FakeClock()
    tree = FakeTree(clock, size=1, walk_s=0.0)
    w, *_ = make_watch(growth_mb=100, tree=tree, clock=clock, min_interval_s=10.0)
    assert w.check() is None and len(tree.calls) == 1
    clock.now += 9.9
    assert w.check() is None and len(tree.calls) == 1
    clock.now += 0.2
    assert w.check() is None and len(tree.calls) == 2


def test_watch_rate_limit_twenty_times_the_walk():
    clock = FakeClock()
    tree = FakeTree(clock, size=1, walk_s=2.0)       # a walk takes 2 s -> next walk no sooner than 40 s later
    w, *_ = make_watch(growth_mb=100, tree=tree, clock=clock, min_interval_s=1.0)
    w.check()
    assert len(tree.calls) == 1
    for _ in range(39):
        clock.now += 1.0
        w.check()
    assert len(tree.calls) == 1
    clock.now += 1.0
    w.check()
    assert len(tree.calls) == 2


def test_watch_none_tree_path_is_not_a_walk():
    clock = FakeClock()
    tree = FakeTree(clock, size=500)
    w, clock, tree, holder = make_watch(growth_mb=100, tree=tree, clock=clock, path=None, min_interval_s=10.0)
    for _ in range(5):
        assert w.check() is None
        clock.now += 1.0
    assert tree.calls == []
    holder["path"] = Path("/now/there")              # the first call with a path walks at once
    assert w.check() is not None
    assert len(tree.calls) == 1


def test_watch_limits_off():
    clock = FakeClock()
    tree = FakeTree(clock, size=10**9)
    w, *_ = make_watch(floor_mb=0, growth_mb=0, free=lambda p: 0, tree=tree, clock=clock)
    for _ in range(3):
        assert w.check() is None
    assert tree.calls == []


def test_watch_in_thread_trips_once_and_stops():
    calls = []

    class Watch:
        n = 0

        def check(self):
            self.n += 1
            return "boom" if self.n >= 2 else None

    import threading
    tripped = threading.Event()
    watch = Watch()
    stop = bounded.watch_in_thread(watch, lambda r: (calls.append(r), tripped.set()), tick_s=0.01)
    assert tripped.wait(5)
    time.sleep(0.1)
    assert calls == ["boom"]
    assert stop.is_set()
    assert watch.n == 2


def test_watch_in_thread_stop_event_stops_it():
    class Watch:
        n = 0

        def check(self):
            self.n += 1

    watch = Watch()
    stop = bounded.watch_in_thread(watch, lambda r: None, tick_s=0.01)
    time.sleep(0.1)
    stop.set()
    time.sleep(0.05)
    n = watch.n
    time.sleep(0.1)
    assert watch.n == n and n >= 1
