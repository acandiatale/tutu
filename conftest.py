"""Walls for every pytest run whose tests live under this repository (see ``scripts/bounded.py``).

* file-size cap on the pytest process and every child it starts (``BOUNDED_FILE_MB``, default 1024 MiB);
* free-space floor of the temp volume, checked before the run and every second during it (``BOUNDED_FLOOR_MB``,
  default 5120 MiB);
* cap on pytest's own basetemp tree (``BOUNDED_GROWTH_MB``, default 4096 MiB).

``0`` switches a limit off. A trip prints ``bounded: STOPPED: <reason>`` past pytest's capture and ends the process
with exit code 125. The report header's ``bounded:`` line is the liveness signal: a run without it has no wall.
Under the wrapper (``BOUNDED=1``) the wrapper watches the same folder, so no watch thread is started here.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "_repo_bounded", Path(__file__).resolve().parent / "scripts" / "bounded.py")
_bounded = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bounded)

_state: dict = {"stop": None, "header": "bounded: not configured"}


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


def pytest_report_header(config):
    return _state["header"]


def pytest_unconfigure(config):
    stop = _state.get("stop")
    if stop is not None:
        stop.set()
        _state["stop"] = None
