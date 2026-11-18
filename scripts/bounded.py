#!/usr/bin/env python3
"""Run a command (or a pytest session) inside walls the operating system and a watchdog enforce.

Why: a test once reproduced "zip a log while it is being appended" with a writer thread that had no size limit and
no time limit; two runs wrote one 118 GB file into pytest's temp folder and filled the developer's disk. A rule in a
brief ("cap your loops") is a promise; this module is the wall, and every wall fails LOUDLY when it is hit.

Walls:
* file size: the soft ``RLIMIT_FSIZE`` of the process. A Python writer gets ``OSError: [Errno 27] File too large``,
  a non-Python writer is killed by ``SIGXFSZ``; children inherit the limit.
* wall clock (CLI only): the whole process group is stopped after ``--seconds``.
* own temp folder: the total size of the run's temp tree (``--tmp-mb`` / ``BOUNDED_GROWTH_MB``).
* free-space floor: the run stops when the volume holding the temp folder has less free space than
  ``--floor-mb`` / ``BOUNDED_FLOOR_MB``.

Library use: ``cap_file_size``, ``free_mb``, ``tree_mb``, ``DiskWatch`` and ``watch_in_thread`` (the repository's
root ``conftest.py`` loads this file by path). CLI use::

    python scripts/bounded.py [--file-mb N] [--seconds N] [--tmp-mb N] [--floor-mb N] [--tmp DIR] [--keep] \\
        -- <command> [args...]

``0`` switches a limit off. Exit codes: the command's own (``128 + n`` when it died of signal n), ``2`` usage error,
``124`` time cap, ``125`` disk wall (temp folder above the cap or free space below the floor), ``126`` refused to
start (free space already below the floor), ``127`` the command could not be started, ``128 + n`` the wrapper got
signal n (SIGINT / SIGTERM / SIGHUP). The command runs in its own session, so it has no controlling terminal.

Standard library only.
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

try:
    import resource
except ImportError:  # Windows and other platforms without POSIX resource limits
    resource = None  # type: ignore[assignment]

EXIT_USAGE = 2
EXIT_TIME = 124
EXIT_DISK = 125
EXIT_REFUSED = 126
EXIT_NOT_STARTED = 127

ENV_FILE_MB = "BOUNDED_FILE_MB"
ENV_FLOOR_MB = "BOUNDED_FLOOR_MB"
ENV_GROWTH_MB = "BOUNDED_GROWTH_MB"

MIB = 1024 * 1024
KILL_GRACE_S = 5.0


class UsageError(ValueError):
    """A limit given on the command line or in the environment is not a non-negative integer."""


def parse_mb(text: str, what: str) -> int:
    """Parse a non-negative integer limit; anything else is a usage error, never silently ignored."""
    try:
        value = int(str(text).strip())
    except ValueError:
        raise UsageError(f"{what} must be a non-negative integer, got {text!r}") from None
    if value < 0:
        raise UsageError(f"{what} must be a non-negative integer, got {text!r}")
    return value


def env_mb(name: str, default: int, environ: dict[str, str] | None = None) -> int:
    """The integer value of environment variable ``name`` (``default`` when unset or empty)."""
    env = os.environ if environ is None else environ
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    return parse_mb(raw, name)


def cap_file_size(mb: int) -> int | None:
    """Lower the soft ``RLIMIT_FSIZE`` of this process to ``mb`` MiB and return the soft limit in bytes now in force.

    A limit that is already lower is kept (never raised); the hard limit is kept. Returns ``None`` when ``mb <= 0``
    or the platform has no ``resource`` module. Raises ``RuntimeError`` when the limit read back is not in force:
    a wall that is silently absent is exactly the failure this module exists to close.
    """
    if mb <= 0 or resource is None:
        return None
    want = mb * MIB
    inf = resource.RLIM_INFINITY
    soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
    if soft != inf and soft <= want:
        target = soft
    else:
        target = want if hard == inf else min(want, hard)
    if target != soft:
        try:
            resource.setrlimit(resource.RLIMIT_FSIZE, (target, hard))
        except (ValueError, OSError) as exc:
            raise RuntimeError(f"bounded: could not set RLIMIT_FSIZE to {target} bytes: {exc}") from exc
    got = resource.getrlimit(resource.RLIMIT_FSIZE)[0]
    if got == inf or got > want:
        raise RuntimeError(f"bounded: RLIMIT_FSIZE did not take effect (asked {want} bytes, in force {got})")
    return got


def _existing(path: Path) -> Path:
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


# The real probe, bound when this file is loaded: a test that replaces ``shutil.disk_usage`` with a fake (to test a
# "disk full" answer of the product) must not make the watch of its own pytest run see a full disk and stop the run.
_disk_usage = shutil.disk_usage


def free_mb(path) -> int:
    """Free space in MiB of the volume holding ``path`` (its nearest existing ancestor)."""
    return int(_disk_usage(_existing(Path(path))).free // MIB)


def tree_mb(path, *, stop_above_mb: int | None = None) -> int:
    """Sum of ``st_size`` of the regular files under ``path`` in MiB, rounded up.

    Symlinks are not followed; files and folders that vanish during the walk are skipped. With ``stop_above_mb`` the
    walk may stop as soon as the sum is above it (the returned value is then above it, not the full total).
    """
    limit = None if stop_above_mb is None else stop_above_mb * MIB
    total = 0
    stack = [os.fspath(path)]
    while stack:
        top = stack.pop()
        try:
            it = os.scandir(top)
        except OSError:
            continue
        with it:
            for entry in it:
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISDIR(st.st_mode):
                    stack.append(entry.path)
                elif stat.S_ISREG(st.st_mode):
                    total += st.st_size
                    if limit is not None and total > limit:
                        return (total + MIB - 1) // MIB
    return (total + MIB - 1) // MIB


class DiskWatch:
    """Decide whether a run must stop because of disk space; pure logic, no thread inside.

    ``check()`` tests the free-space floor on every call (cheap) and walks the run's own tree at most every
    ``max(min_interval_s, 20 x duration of the previous walk)`` seconds, so the watch costs at most about 5 % of one
    core however big the tree is. A call where ``tree_path()`` is ``None`` does not count as a walk.
    """

    def __init__(self, *, floor_mb: int, growth_mb: int, tree_path: Callable[[], Path | None], floor_path: Path,
                 free: Callable[[Path], int] = free_mb, tree: Callable[..., int] = tree_mb,
                 clock: Callable[[], float] = time.monotonic, min_interval_s: float = 1.0) -> None:
        self.floor_mb = floor_mb
        self.growth_mb = growth_mb
        self.tree_path = tree_path
        self.floor_path = floor_path
        self._free = free
        self._tree = tree
        self._clock = clock
        self.min_interval_s = min_interval_s
        self._last_walk_end: float | None = None
        self._last_walk_s = 0.0
        self.walks = 0

    def check(self) -> str | None:
        """The reason the run must stop, else ``None``."""
        if self.floor_mb > 0:
            free = self._free(self.floor_path)
            if free < self.floor_mb:
                return (f"free space on the volume of {self.floor_path} is {free} MiB, below the floor of "
                        f"{self.floor_mb} MiB (BOUNDED_FLOOR_MB)")
        if self.growth_mb > 0:
            path = self.tree_path()
            if path is None:
                return None
            now = self._clock()
            if self._last_walk_end is not None:
                wait = max(self.min_interval_s, 20 * self._last_walk_s)
                if now - self._last_walk_end < wait:
                    return None
            start = self._clock()
            size = self._tree(path, stop_above_mb=self.growth_mb)
            end = self._clock()
            self._last_walk_s = max(0.0, end - start)
            self._last_walk_end = end
            self.walks += 1
            if size > self.growth_mb:
                return (f"the run's temp folder {path} holds {size} MiB, above the cap of {self.growth_mb} MiB "
                        f"(BOUNDED_GROWTH_MB)")
        return None


def watch_in_thread(watch: DiskWatch, on_trip: Callable[[str], None], *, tick_s: float = 1.0) -> threading.Event:
    """Call ``watch.check()`` every ``tick_s`` in a daemon thread; call ``on_trip(reason)`` once and stop.

    Setting the returned event stops the thread. An exception inside ``check()`` is reported on stderr (once per
    distinct message) and the watch keeps going.
    """
    stop = threading.Event()
    seen: set[str] = set()

    def run() -> None:
        while not stop.wait(tick_s):
            try:
                reason = watch.check()
            except Exception as exc:  # noqa: BLE001 - a broken check must be visible, not fatal to the thread
                msg = f"bounded: watch check failed: {exc!r}"
                if msg not in seen:
                    seen.add(msg)
                    try:
                        sys.stderr.write(msg + "\n")
                        sys.stderr.flush()
                    except Exception:  # noqa: BLE001
                        pass
                continue
            if reason is not None:
                stop.set()
                on_trip(reason)
                return

    threading.Thread(target=run, name="bounded-watch", daemon=True).start()
    return stop


# ---------------------------------------------------------------------------------------------------------- CLI


def _err(line: str) -> None:
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


def _mib_or_off(mb: int) -> str:
    return f"{mb} MiB" if mb > 0 else "off"


def _build_parser(environ: dict[str, str]) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bounded.py",
        usage="python scripts/bounded.py [--file-mb N] [--seconds N] [--tmp-mb N] [--floor-mb N] [--tmp DIR] "
              "[--keep] -- <command> [args...]",
        description="Run a command with a file-size cap, a wall-clock cap, a temp-folder cap and a free-space "
                    "floor. 0 switches a limit off.")
    p.add_argument("--file-mb", default=None, help=f"largest file the command may write (${ENV_FILE_MB}, 512)")
    p.add_argument("--seconds", default="1800", help="wall-clock cap for the whole process group (1800)")
    p.add_argument("--tmp-mb", default=None, help=f"cap on the run's own temp folder (${ENV_GROWTH_MB}, 2048)")
    p.add_argument("--floor-mb", default=None, help=f"free-space floor of the temp volume (${ENV_FLOOR_MB}, 5120)")
    p.add_argument("--tmp", default=None, help="base folder for the run's temp folder (system temp dir)")
    p.add_argument("--keep", action="store_true", help="keep the run's temp folder")
    return p


def _parse(argv: list[str], environ: dict[str, str]):
    if "--" in argv:
        i = argv.index("--")
        opts, command = argv[:i], argv[i + 1:]
    else:
        opts, command = argv, []
    parser = _build_parser(environ)
    ns = parser.parse_args(opts)  # exits 2 on an unknown option
    try:
        file_mb = parse_mb(ns.file_mb, "--file-mb") if ns.file_mb is not None else env_mb(ENV_FILE_MB, 512, environ)
        seconds = parse_mb(ns.seconds, "--seconds")
        tmp_mb = parse_mb(ns.tmp_mb, "--tmp-mb") if ns.tmp_mb is not None else env_mb(ENV_GROWTH_MB, 2048, environ)
        floor_mb = (parse_mb(ns.floor_mb, "--floor-mb") if ns.floor_mb is not None
                    else env_mb(ENV_FLOOR_MB, 5120, environ))
    except UsageError as exc:
        parser.print_usage(sys.stderr)
        _err(f"bounded: usage error: {exc}")
        raise SystemExit(EXIT_USAGE) from None
    if not command:
        parser.print_usage(sys.stderr)
        _err("bounded: usage error: no command given (put it after --)")
        raise SystemExit(EXIT_USAGE)
    tmp_base = Path(ns.tmp) if ns.tmp else Path(tempfile.gettempdir())
    return file_mb, seconds, tmp_mb, floor_mb, tmp_base, ns.keep, command


def _kill_group(proc: subprocess.Popen, sig: int) -> None:
    killpg = getattr(os, "killpg", None)
    try:
        if killpg is not None:
            killpg(proc.pid, sig)
        elif proc.poll() is None:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _stop_group(proc: subprocess.Popen) -> None:
    """SIGTERM the whole process group, SIGKILL it at the latest 5 s later."""
    _kill_group(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=KILL_GRACE_S)
    except subprocess.TimeoutExpired:
        pass
    _kill_group(proc, getattr(signal, "SIGKILL", signal.SIGTERM))
    try:
        proc.wait(timeout=KILL_GRACE_S)
    except subprocess.TimeoutExpired:
        pass


def _code_of(returncode: int | None) -> int:
    if returncode is None:
        return 1
    return 128 + (-returncode) if returncode < 0 else returncode


def main(argv: list[str] | None = None) -> int:
    """Run the CLI; returns the exit code (see the module docstring)."""
    environ = dict(os.environ)
    try:
        file_mb, seconds, tmp_mb, floor_mb, tmp_base, keep, command = _parse(
            list(sys.argv[1:] if argv is None else argv), environ)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_USAGE

    t0 = time.monotonic()
    state = {"tmp": 0}

    def summary(code: int) -> int:
        _err(f"bounded: exit={code} seconds={time.monotonic() - t0:.1f} tmp={state['tmp']} MiB "
             f"file-cap={_mib_or_off(file_mb)} floor={_mib_or_off(floor_mb)}")
        return code

    if file_mb > 0 and resource is None:
        _err("bounded: usage error: the file-size cap is not supported on this platform, pass --file-mb 0")
        return summary(EXIT_USAGE)

    tmp_base.mkdir(parents=True, exist_ok=True)
    if floor_mb > 0:
        free = free_mb(tmp_base)
        if free < floor_mb:
            _err(f"bounded: REFUSED: free space on the volume of {tmp_base} is {free} MiB, below the floor of "
                 f"{floor_mb} MiB (--floor-mb / BOUNDED_FLOOR_MB); the command was not started")
            return summary(EXIT_REFUSED)

    run_dir = tmp_base / f"bounded-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    run_dir.mkdir(parents=False, exist_ok=False)
    child_env = dict(environ)
    for key in ("TMPDIR", "TEMP", "TMP"):
        child_env[key] = str(run_dir)
    child_env["BOUNDED"] = "1"

    received: list[int] = []

    def on_signal(signum, _frame) -> None:
        received.append(signum)

    handled = [s for s in (getattr(signal, n, None) for n in ("SIGINT", "SIGTERM", "SIGHUP")) if s is not None]
    previous = {s: signal.signal(s, on_signal) for s in handled}

    def preexec() -> None:
        cap_file_size(file_mb)  # raises in the child when the cap is not in force; Popen then fails
        if seconds > 0:
            signal.alarm(seconds + 60)  # backstop that survives exec, even if the wrapper is SIGKILLed

    code = 1
    try:
        try:
            proc = subprocess.Popen(command, env=child_env, start_new_session=True,
                                    preexec_fn=preexec if os.name == "posix" else None)
        except FileNotFoundError as exc:
            _err(f"bounded: cannot start {command[0]!r}: {exc}")
            code = EXIT_NOT_STARTED
            return code
        except subprocess.SubprocessError as exc:
            _err(f"bounded: could not apply the limits in the child ({exc}); the command was not started")
            code = EXIT_USAGE
            return code

        tripped: list[str] = []
        trip_event = threading.Event()

        def on_trip(reason: str) -> None:
            tripped.append(reason)
            trip_event.set()

        stop_watch = None
        if floor_mb > 0 or tmp_mb > 0:
            watch = DiskWatch(floor_mb=floor_mb, growth_mb=tmp_mb, tree_path=lambda: run_dir, floor_path=tmp_base)
            stop_watch = watch_in_thread(watch, on_trip, tick_s=1.0)

        try:
            while True:
                try:
                    proc.wait(timeout=0.1)
                    code = _code_of(proc.returncode)
                    # a finished command leaves nothing behind in its group (no background writer)
                    _kill_group(proc, getattr(signal, "SIGKILL", signal.SIGTERM))
                    break
                except subprocess.TimeoutExpired:
                    pass
                if received:
                    sig = received[0]
                    _err(f"bounded: STOPPED: the wrapper received {signal.Signals(sig).name}")
                    _stop_group(proc)
                    code = 128 + sig
                    break
                if trip_event.is_set():
                    _err(f"bounded: STOPPED: {tripped[0]}")
                    _stop_group(proc)
                    code = EXIT_DISK
                    break
                if seconds > 0 and time.monotonic() - t0 > seconds:
                    _err(f"bounded: STOPPED: the wall clock passed --seconds {seconds}")
                    _stop_group(proc)
                    code = EXIT_TIME
                    break
        finally:
            if stop_watch is not None:
                stop_watch.set()
            if proc.poll() is None:
                _stop_group(proc)
        return code
    finally:
        for s, h in previous.items():
            signal.signal(s, h)
        state["tmp"] = tree_mb(run_dir)
        if keep:
            _err(f"bounded: kept {run_dir}")
        else:
            shutil.rmtree(run_dir, ignore_errors=True)
        summary(code)


if __name__ == "__main__":
    sys.exit(main())
