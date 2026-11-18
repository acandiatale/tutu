"""Tests for the repository's root conftest.py: the walls every pytest run under this repository gets.

Each test builds a throwaway project in ``tmp_path`` (a copy of the root ``conftest.py`` and ``scripts/bounded.py``
plus a ``pytest.ini``) and runs ``python -m pytest`` there as a subprocess (timeout <= 30 s, killed in ``finally``),
with ``TMPDIR`` under ``tmp_path`` so the inner basetemp stays inside this test's own temp folder. The inner tests'
writing loops carry their own count and time caps.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(os.name != "posix", reason="RLIMIT_FSIZE is a POSIX limit")

INNER_TESTS = '''
import os, time
from pathlib import Path

MIB = 1024 * 1024


def test_write_3_mib(tmp_path):
    # count cap 48 x 64 KiB = 3 MiB and a 10 s time cap, whatever the wall does
    deadline = time.monotonic() + 10
    with open(tmp_path / "big.bin", "wb") as fh:
        for _ in range(48):
            if time.monotonic() > deadline:
                break
            fh.write(b"x" * 65536)
            fh.flush()
    assert (tmp_path / "big.bin").stat().st_size == 3 * MIB


def test_grow_tmp_path(tmp_path):
    # GUARD_FILES files of 1 MiB (<= 12), the first 3 at once, then GUARD_SLEEP between files; 15 s time cap
    n = min(int(os.environ.get("GUARD_FILES", "12")), 12)
    pause = float(os.environ.get("GUARD_SLEEP", "1.0"))
    deadline = time.monotonic() + 15
    for i in range(n):
        if time.monotonic() > deadline:
            break
        (tmp_path / f"f{i}.bin").write_bytes(b"x" * MIB)
        if i >= 2:
            time.sleep(pause)


def test_capfd_writer(tmp_path, capfd):
    # the same bounded loop while a capfd fixture holds fd 2: 8 files of 1 MiB at most, 15 s time cap
    deadline = time.monotonic() + 15
    for i in range(8):
        if time.monotonic() > deadline:
            break
        (tmp_path / f"c{i}.bin").write_bytes(b"x" * MIB)
        if i >= 2:
            time.sleep(1.0)


def test_marker(tmp_path):
    Path(os.environ["GUARD_MARKER"]).write_text("ran")


def test_which_git(tmp_path):
    import shutil
    Path(os.environ["GUARD_MARKER"]).write_text(shutil.which("git") or "")


def _hold(tmp_path, name):
    # under -n 2 each worker runs one of these: 3 MiB (count cap), its pid on record, then a hold of 20 s at most
    (tmp_path / "held.bin").write_bytes(b"x" * (3 * MIB))
    Path(os.environ["GUARD_MARKER"] + "." + name).write_text(str(os.getpid()))
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        time.sleep(0.1)


def test_hold_a(tmp_path):
    _hold(tmp_path, "a")


def test_hold_b(tmp_path):
    _hold(tmp_path, "b")
'''


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / "scripts").mkdir(parents=True)
    shutil.copy(REPO / "conftest.py", proj / "conftest.py")
    shutil.copy(REPO / "scripts" / "bounded.py", proj / "scripts" / "bounded.py")
    (proj / "pytest.ini").write_text("[pytest]\n")
    (proj / "test_inner.py").write_text(textwrap.dedent(INNER_TESTS))
    (tmp_path / "tmp").mkdir()
    return proj


def run_inner(project: Path, select: str, *, timeout: float = 30, args_extra: tuple[str, ...] = (),
              **env_extra: str):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("BOUNDED") and k not in ("PYTEST_CURRENT_TEST", "PYTEST_ADDOPTS")}
    env["TMPDIR"] = str(project.parent / "tmp")
    env["GUARD_MARKER"] = str(project.parent / "marker")
    env.setdefault("BOUNDED_FLOOR_MB", "0")          # never depend on the real disk's free space
    env.update(env_extra)
    args = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-o", "addopts=", *args_extra,
            "test_inner.py", "-k", select]
    proc = subprocess.Popen(args, cwd=project, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        out, err = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(args, proc.returncode, out, err)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()


def header(r) -> str:
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith("bounded:")]
    assert lines, f"no bounded: header line\n{r.stdout}\n{r.stderr}"
    return lines[0]


def test_header_line_is_printed_with_the_defaults(project):
    r = run_inner(project, "marker", BOUNDED_FLOOR_MB="")  # unset -> the default floor of 5120 MiB
    if r.returncode == 126:
        pytest.skip("this machine has less than 5 GiB free; the default floor refused the inner run")
    assert r.returncode == 0, r.stdout + r.stderr
    line = header(r)
    # the file cap may be lower than 1024 when the outer run already has a lower one (it is never raised)
    assert line.startswith("bounded: file cap ") and " MiB, " in line
    assert line.endswith(", free floor 5120 MiB, own temp cap 4096 MiB")


def test_file_cap_makes_a_big_write_fail_normally(project):
    r = run_inner(project, "write_3_mib", BOUNDED_FILE_MB="1")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "File too large" in r.stdout
    assert "1 failed" in r.stdout
    assert "file cap 1 MiB" in header(r)


def test_growth_cap_stops_the_process_with_125(project):
    t0 = time.monotonic()
    r = run_inner(project, "grow_tmp_path", BOUNDED_GROWTH_MB="2")
    assert r.returncode == 125, r.stdout + r.stderr
    assert time.monotonic() - t0 < 25
    both = r.stdout + r.stderr
    assert "bounded: STOPPED:" in both and "BOUNDED_GROWTH_MB" in both
    assert "bounded: running test: test_inner.py::test_grow_tmp_path" in both


def test_the_reason_gets_past_a_capfd_fixture(project):
    # a test holding capfd captures fd 2 on top of the global capture; the reason still reaches the terminal
    r = run_inner(project, "capfd_writer", BOUNDED_GROWTH_MB="2")
    assert r.returncode == 125, r.stdout + r.stderr
    both = r.stdout + r.stderr
    assert "bounded: STOPPED:" in both and "BOUNDED_GROWTH_MB" in both


def test_floor_refuses_the_run_with_126(project):
    r = run_inner(project, "marker", BOUNDED_FLOOR_MB="999999999")
    assert r.returncode == 126, r.stdout + r.stderr
    assert "bounded: REFUSED:" in r.stdout + r.stderr
    assert not (project.parent / "marker").exists()
    assert "passed" not in r.stdout


def test_under_the_wrapper_no_watch_thread(project):
    # 3 MiB at once, then 4 more with 0.5 s pauses: the session watch (if it ran) would trip at its first tick
    r = run_inner(project, "grow_tmp_path", BOUNDED="1", BOUNDED_GROWTH_MB="2", BOUNDED_FILE_MB="8",
                  GUARD_FILES="7", GUARD_SLEEP="0.5")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "1 passed" in r.stdout
    assert "bounded: STOPPED" not in r.stdout + r.stderr
    assert "watched by the wrapper" in header(r)


def test_all_limits_off(project):
    r = run_inner(project, "write_3_mib", BOUNDED_FILE_MB="0", BOUNDED_FLOOR_MB="0", BOUNDED_GROWTH_MB="0")
    assert r.returncode == 0, r.stdout + r.stderr
    assert header(r) == "bounded: file cap off, free floor off, own temp cap off"
    assert "1 passed" in r.stdout


def test_bad_environment_value_is_a_usage_error(project):
    r = run_inner(project, "marker", BOUNDED_FILE_MB="big")
    assert r.returncode == 4, r.stdout + r.stderr
    assert "BOUNDED_FILE_MB" in r.stdout + r.stderr
    assert not (project.parent / "marker").exists()


# ------------------------------------------------------------------------------------- the walls under pytest-xdist
def test_under_xdist_a_workers_file_cap_makes_the_write_fail(project):
    pytest.importorskip("xdist")
    r = run_inner(project, "write_3_mib", args_extra=("-n", "2"), BOUNDED_FILE_MB="1")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "File too large" in r.stdout and "1 failed" in r.stdout
    assert "file cap 1 MiB" in header(r)


def test_under_xdist_the_controllers_temp_cap_holds_for_the_sum_of_the_workers(project):
    """Two workers hold 3 MiB each: neither worker's own folder passes the cap of 4 MiB, the sum does. The controller
    watches the whole basetemp, stops the run with 125 and no worker is left behind."""
    pytest.importorskip("xdist")
    marker = project.parent / "marker"
    t0 = time.monotonic()
    r = run_inner(project, "hold", timeout=30, args_extra=("-n", "2"), BOUNDED_GROWTH_MB="4")
    assert r.returncode == 125, r.stdout + r.stderr
    assert time.monotonic() - t0 < 25                    # before the 20 s holds would have ended both tests
    both = r.stdout + r.stderr
    stopped = [ln for ln in both.splitlines() if ln.startswith("bounded: STOPPED:")]
    assert stopped and all("BOUNDED_GROWTH_MB" in ln for ln in stopped), both
    # the controller's watch tripped (its basetemp `pytest-N`), not a worker's (`pytest-N/popen-gwK`)
    folders = [Path(ln.split("temp folder ", 1)[1].split(" holds ", 1)[0]).name for ln in stopped]
    assert folders and all(f.startswith("pytest-") for f in folders), both
    pids = [int(Path(f"{marker}.{n}").read_text()) for n in ("a", "b")]
    deadline = time.monotonic() + 10
    while any(_alive(p) for p in pids) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not any(_alive(p) for p in pids), "a worker outlived the stopped controller"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - someone else's process took the number
        return False
    return True


def test_header_names_the_git_in_use(project):
    # PATH without the outer run's link folder: the inner run decides for itself
    path = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep) if "sbmig-real-git-" not in p)
    r = run_inner(project, "which_git", PATH=path)
    assert r.returncode == 0, r.stdout + r.stderr
    git_lines = [ln for ln in r.stdout.splitlines() if ln.startswith("git: ")]
    assert len(git_lines) == 1, r.stdout
    used = (project.parent / "marker").read_text()
    outer = shutil.which("git", path=path)
    if outer is None:
        assert git_lines[0] == "git: not found on PATH" and used == ""
    elif sys.platform == "darwin" and os.path.realpath(outer) == "/usr/bin/git" and shutil.which("xcrun", path=path):
        real = subprocess.run(["xcrun", "-f", "git"], capture_output=True, text=True).stdout.strip()
        assert git_lines[0].startswith(f"git: {real} (the real binary"), git_lines
        assert "sbmig-real-git-" in used and used.endswith(os.sep + "git")   # the link (its folder is gone now)
    else:
        assert git_lines[0] == f"git: {outer}" and used == outer
    assert not list((project.parent / "tmp").glob("sbmig-real-git-*"))   # the link folder is gone after the run
