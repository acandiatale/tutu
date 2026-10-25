"""Operator diagnostics (round 9c): every thread's stack on demand — the answer to "is it stuck?" when neither the LLM
/ Maven heartbeats nor the fix-loop watchdog say anything.

* POSIX: `kill -USR1 <pid>`; Windows: Ctrl+Break in the console (`SIGBREAK`, round 10) → `out/threads-<ts>.txt`.
* Everywhere (Windows too, also under `sbmig serve`): create a file named `dump-threads` (or `dump-threads.txt`, what
  Explorer makes of it with hidden extensions) in the run directory or in the directory sbmig was started from →
  within TRIGGER_POLL_S the dump is written next to it and the trigger file is removed.
* The web endpoint `GET /api/threads`, and the fix-loop watchdog writes one by itself after WATCHDOG_DUMP_S of silence.
"""
from __future__ import annotations

import collections
import logging
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Optional

log = logging.getLogger("sbmig.diag")

TRIGGER_NAMES: tuple[str, ...] = ("dump-threads", "dump-threads.txt")
TRIGGER_POLL_S = 2.0
MAX_WATCHED_RUN_DIRS = 8            # a fleet registers one run dir per project; only the latest ones matter

_WATCH_LOCK = threading.Lock()
_FIXED_DIRS: list[Path] = []                                            # cwd at install time, the signal's out_dir
_RUN_DIRS: "collections.deque[Path]" = collections.deque(maxlen=MAX_WATCHED_RUN_DIRS)
_STUCK: dict[Path, int] = {}       # trigger files that could not be removed → their mtime (never re-fire on it)
_WATCHER: Optional[threading.Thread] = None
_STOP = threading.Event()


def thread_dump_text() -> str:
    names = {t.ident: t.name for t in threading.enumerate()}
    parts = [f"# sbmig thread dump {time.strftime('%Y-%m-%d %H:%M:%S')} pid={os.getpid()} platform={sys.platform}"]
    for tid, frame in sys._current_frames().items():  # noqa: SLF001 - the documented way to get other threads' frames
        parts.append(f"\n--- thread {names.get(tid, '?')} ({tid}) ---")
        parts.append("".join(traceback.format_stack(frame)))
    return "\n".join(parts)


def dump_threads(out_dir: Optional[Path] = None) -> Optional[Path]:
    """Write the dump to `<out_dir>/threads-<ts>.txt` (default `out/`) and log where; None when it cannot write
    (the text then goes to stderr)."""
    text = thread_dump_text()
    try:
        d = Path(out_dir) if out_dir else Path("out")
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"threads-{time.strftime('%Y%m%d-%H%M%S')}.txt"
        p.write_text(text, encoding="utf-8")
    except OSError:
        sys.stderr.write(text + "\n")
        return None
    log.warning("thread dump written to %s (%d threads)", p, text.count("--- thread "))
    return p


# ---------------------------------------------------------------------------------------------- file trigger
def watch_dump_trigger(*dirs: Path | str) -> None:
    """Also look for a `dump-threads` file in these run directories (latest MAX_WATCHED_RUN_DIRS kept). Only
    registers: the polling thread runs once `install_thread_dump_signal()` / `start_dump_trigger_watcher()` started it
    (the CLI and `sbmig serve` do; library use and tests stay thread-free)."""
    with _WATCH_LOCK:
        for d in dirs:
            if d:
                p = Path(d)
                if p in _RUN_DIRS:
                    _RUN_DIRS.remove(p)
                _RUN_DIRS.append(p)


def watched_dirs() -> list[Path]:
    with _WATCH_LOCK:
        out: list[Path] = []
        for d in (*_FIXED_DIRS, *reversed(_RUN_DIRS)):
            if d not in out:
                out.append(d)
        return out


def check_dump_triggers(dirs: Optional[list[Path]] = None) -> list[Path]:
    """One poll: for every trigger file found, remove it and write a thread dump into its directory. Returns the dump
    files written. A trigger that cannot be removed (open in an editor) fires once, not every poll."""
    written: list[Path] = []
    for d in (dirs if dirs is not None else watched_dirs()):
        for name in TRIGGER_NAMES:
            trigger = Path(d) / name
            try:
                st = trigger.stat()
            except OSError:
                _STUCK.pop(trigger, None)
                continue
            if _STUCK.get(trigger) == st.st_mtime_ns:
                continue
            try:
                trigger.unlink()
                _STUCK.pop(trigger, None)
            except OSError:
                _STUCK[trigger] = st.st_mtime_ns
            log.warning("thread dump requested by %s", trigger)
            p = dump_threads(trigger.parent)
            if p is not None:
                written.append(p)
    return written


def _watch_loop(interval: float, stop: threading.Event) -> None:
    while not stop.wait(interval):
        try:
            check_dump_triggers()
        except Exception:                    # pragma: no cover - diagnostics never take the process down
            log.debug("dump-threads trigger check failed", exc_info=True)


def start_dump_trigger_watcher(*dirs: Path | str, interval: float = TRIGGER_POLL_S) -> bool:
    """Start (once) the daemon thread that polls the watched directories (`dirs` + registered run dirs). True when
    the watcher runs after the call."""
    global _WATCHER
    with _WATCH_LOCK:
        for d in dirs:
            if d and Path(d) not in _FIXED_DIRS:
                _FIXED_DIRS.append(Path(d))
        if _WATCHER is not None and _WATCHER.is_alive():
            return True
        _STOP.clear()
        try:
            _WATCHER = threading.Thread(target=_watch_loop, args=(interval, _STOP), name="sbmig-dump-trigger",
                                        daemon=True)
            _WATCHER.start()
        except RuntimeError:                 # pragma: no cover - interpreter shutting down
            _WATCHER = None
            return False
    return True


def stop_dump_trigger_watcher(timeout: float = 5.0) -> None:
    """Stop the polling thread (tests; a later `start_dump_trigger_watcher` starts a new one)."""
    global _WATCHER
    _STOP.set()
    w, _WATCHER = _WATCHER, None
    if w is not None:
        w.join(timeout)


# ---------------------------------------------------------------------------------------------- signals
def _signal_name() -> Optional[str]:
    """SIGUSR1 on POSIX, SIGBREAK (Ctrl+Break) on Windows; None when neither exists."""
    import signal

    if hasattr(signal, "SIGUSR1"):
        return "SIGUSR1"
    if hasattr(signal, "SIGBREAK"):
        return "SIGBREAK"
    return None


def install_thread_dump_signal(out_dir: Optional[Path] = None) -> bool:
    """`kill -USR1 <pid>` (POSIX) / Ctrl+Break (Windows `SIGBREAK`) → dump_threads(out_dir); plus the `dump-threads`
    file trigger for the current directory, `out_dir` and every workspace run dir. The signal handler needs the main
    thread (returns False elsewhere — the file trigger still starts); never raises.

    Windows: the handler runs when the main thread next executes Python code, i.e. after the current Maven wait slot
    (≤ 60 s heartbeat); the file trigger answers within TRIGGER_POLL_S. Maven / git children have their own hidden
    console (`CREATE_NO_WINDOW`), so Ctrl+Break never interrupts a build."""
    import signal

    try:
        start_dump_trigger_watcher(Path.cwd(), *([out_dir] if out_dir else []))
    except OSError:                          # pragma: no cover - cwd deleted
        pass
    name = _signal_name()
    if name is None or threading.current_thread() is not threading.main_thread():
        return False
    try:
        signal.signal(getattr(signal, name), lambda *_a: dump_threads(out_dir))
        return True
    except (ValueError, OSError):  # pragma: no cover
        return False
