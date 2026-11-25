"""Walls for every pytest run whose tests live under this repository (see ``scripts/bounded.py``).

* file-size cap on the pytest process and every child it starts (``BOUNDED_FILE_MB``, default 1024 MiB);
* free-space floor of the temp volume, checked before the run and every second during it (``BOUNDED_FLOOR_MB``,
  default 5120 MiB);
* cap on pytest's own basetemp tree (``BOUNDED_GROWTH_MB``, default 4096 MiB).

``0`` switches a limit off. A trip prints ``bounded: STOPPED: <reason>`` past pytest's capture and ends the process
with exit code 125. The report header's ``bounded:`` line is the liveness signal: a run without it has no wall.
Under the wrapper (``BOUNDED=1``) the wrapper watches the same folder, so no watch thread is started here.

Speed, macOS only: ``/usr/bin/git`` is the Xcode ``xcrun`` shim, about three times slower per call than the binary it
dispatches to, and the suite starts git some 26,000 times. The first process of a run (the xdist controller, or the
only process of a serial run) puts a private folder holding just a ``git`` symlink to ``xcrun -f git`` first on
``PATH``; xdist workers and every child inherit that ``PATH``. Only ``git`` is linked: the Xcode tool folder also
holds ``python3``, ``make``, ``gcc`` … which must not shadow the user's. Nothing is done when ``xcrun`` is missing or
fails, or when ``git`` already resolves to something other than the shim. The header's ``git:`` line says which git
the run uses.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "_repo_bounded", Path(__file__).resolve().parent / "scripts" / "bounded.py")
_bounded = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bounded)
# registered so that test code reaches the SAME module (its WALK_LOCK guards the disk watch's walk)
sys.modules["_repo_bounded"] = _bounded

_state: dict = {"stop": None, "header": "bounded: not configured", "git": "git: not found on PATH",
                "git_dir": None}

_XCRUN_SHIM = "/usr/bin/git"


def _use_real_git() -> None:
    """On macOS, put the real git binary behind the xcrun shim first on PATH (see the module docstring)."""
    found = shutil.which("git")
    if found is None:
        return
    _state["git"] = f"git: {found}"
    if sys.platform != "darwin" or os.path.realpath(found) != _XCRUN_SHIM:
        return
    try:
        cp = subprocess.run(["xcrun", "-f", "git"], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                            timeout=20)
    except (OSError, subprocess.SubprocessError):
        return
    real = cp.stdout.strip()
    if cp.returncode != 0 or not real or not os.access(real, os.X_OK) or os.path.realpath(real) == _XCRUN_SHIM:
        return
    folder = tempfile.mkdtemp(prefix="sbmig-real-git-")
    try:
        os.symlink(real, os.path.join(folder, "git"))
        probe = subprocess.run([os.path.join(folder, "git"), "--version"], stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=20)
        if probe.returncode != 0:
            raise OSError("the linked git does not start")
    except (OSError, subprocess.SubprocessError):
        shutil.rmtree(folder, ignore_errors=True)
        return
    os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")
    _state["git_dir"] = folder
    _state["git"] = f"git: {real} (the real binary; {_XCRUN_SHIM} is the slower xcrun shim)"


def pytest_configure(config):
    b = _bounded
    try:
        file_mb = b.env_mb(b.ENV_FILE_MB, 1024)
        floor_mb = b.env_mb(b.ENV_FLOOR_MB, 5120)
        growth_mb = b.env_mb(b.ENV_GROWTH_MB, 4096)
    except b.UsageError as exc:
        raise pytest.UsageError(f"bounded: {exc}") from None
    floor_path = Path(tempfile.gettempdir())
    if floor_mb > 0:
        free = b.free_mb(floor_path)
        if free < floor_mb:
            pytest.exit(f"bounded: REFUSED: free space on the volume of {floor_path} is {free} MiB, below the floor "
                        f"of {floor_mb} MiB (BOUNDED_FLOOR_MB); no test was run", returncode=b.EXIT_REFUSED)
    _use_real_git()
    try:
        cap = b.cap_file_size(file_mb)
    except RuntimeError as exc:
        raise pytest.UsageError(str(exc)) from None
    if file_mb <= 0:
        file_txt = "off"
    elif cap is None:
        file_txt = "file cap unavailable on this platform"
    else:
        file_txt = f"{cap // b.MIB} MiB"
    under_wrapper = os.environ.get("BOUNDED") == "1"
    if growth_mb <= 0:
        growth_txt = "off"
    elif under_wrapper:
        growth_txt = "the wrapper's (watched by the wrapper)"
    else:
        growth_txt = f"{growth_mb} MiB"
    floor_txt = f"{floor_mb} MiB" if floor_mb > 0 else "off"
    if under_wrapper and floor_mb > 0:
        floor_txt += " at start, then the wrapper's (watched by the wrapper)"
    _state["header"] = f"bounded: file cap {file_txt}, free floor {floor_txt}, own temp cap {growth_txt}"

    if under_wrapper or (floor_mb <= 0 and growth_mb <= 0):
        return

    def basetemp():
        # never forces the creation: None until pytest made the basetemp itself
        factory = getattr(config, "_tmp_path_factory", None)
        path = getattr(factory, "_basetemp", None)
        return path if path is not None and Path(path).is_dir() else None

    def on_trip(reason: str) -> None:
        capman = config.pluginmanager.getplugin("capturemanager")
        try:
            if capman is not None:
                # both captures: the global one and a capsys / capfd fixture's
                capman.suspend(in_=False)
        except Exception:  # noqa: BLE001 - the message must still go out
            pass
        lines = [f"bounded: STOPPED: {reason}"]
        current = os.environ.get("PYTEST_CURRENT_TEST")
        if current:
            lines.append(f"bounded: running test: {current}")
        text = "\n" + "\n".join(lines) + "\n"
        try:
            sys.stderr.write(text)
            sys.stderr.flush()
        except Exception:  # noqa: BLE001
            os.write(2, text.encode("utf-8", "replace"))
        os._exit(b.EXIT_DISK)

    watch = b.DiskWatch(floor_mb=floor_mb, growth_mb=growth_mb, tree_path=basetemp, floor_path=floor_path,
                        min_interval_s=10.0)
    _state["stop"] = b.watch_in_thread(watch, on_trip, tick_s=1.0)


_MAX_AUTO_WORKERS = 8


@pytest.hookimpl(optionalhook=True)        # an xdist hook: no error when pytest-xdist is not installed
def pytest_xdist_auto_num_workers(config):
    """``-n auto`` (the default in pyproject): one worker per CPU, at most 8.

    Measured on a 10-core Mac (8 performance + 2 efficiency cores): 8 workers ran the suite in ~106 s, 10 in
    ~115 s — the tests start git / python children, more workers than fast cores only add contention. A 4-core PC gets
    4 workers. ``PYTEST_XDIST_AUTO_NUM_WORKERS`` or an explicit ``-n N`` still decide; ``-n 0`` is the serial run."""
    env = os.environ.get("PYTEST_XDIST_AUTO_NUM_WORKERS", "").strip()
    if env:
        try:
            return max(0, int(env))
        except ValueError:
            pass                                     # xdist's own rule would reject it too; fall back to the cap
    return max(1, min(os.cpu_count() or 1, _MAX_AUTO_WORKERS))


def pytest_report_header(config):
    return [_state["header"], _state["git"]]


def pytest_unconfigure(config):
    stop = _state.get("stop")
    if stop is not None:
        stop.set()
        _state["stop"] = None
    folder = _state.get("git_dir")
    if folder is not None:
        # the process that made the link folder removes it, after its xdist workers have ended
        os.environ["PATH"] = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep) if p != folder)
        shutil.rmtree(folder, ignore_errors=True)
        _state["git_dir"] = None
