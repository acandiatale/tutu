"""Operator diagnostics (round 9c): every thread's stack on demand — the answer to "is it stuck?" when neither the LLM
/ Maven heartbeats nor the fix-loop watchdog say anything. POSIX: `kill -USR1 <pid>`; everywhere (Windows too): the
web endpoint `GET /api/threads`, and the fix-loop watchdog writes one by itself after WATCHDOG_DUMP_S of silence."""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Optional

log = logging.getLogger("sbmig.diag")


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


def install_thread_dump_signal(out_dir: Optional[Path] = None) -> bool:
    """`kill -USR1 <pid>` → dump_threads. POSIX only (no SIGUSR1 on Windows: use /api/threads or the watchdog dump),
    main thread only; never raises."""
    import signal

    if not hasattr(signal, "SIGUSR1") or threading.current_thread() is not threading.main_thread():
        return False
    try:
        signal.signal(signal.SIGUSR1, lambda *_a: dump_threads(out_dir))
        return True
    except (ValueError, OSError):  # pragma: no cover
        return False
