"""프로그램이 멈춘 것처럼 보일 때 모든 스레드의 스택을 파일로 남기는 진단 모듈.

A dump is requested by `kill -USR1 <pid>`, Ctrl+Break in a Windows console, a `dump-threads` file in a run
dir or the start dir, `GET /api/threads`, or the watchdog. Entry points: install_thread_dump_signal,
dump_threads, watch_dump_trigger. A failing diagnosis never stops the run.

* Signal dumps go to `out/threads-<ts>.txt`.
* `dump-threads.txt` is what Explorer makes of the name with hidden extensions. Within TRIGGER_POLL_S the dump
  is written next to the trigger file and the trigger is removed.
* The fix-loop watchdog writes one by itself after WATCHDOG_DUMP_S of silence.
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
MAX_WATCHED_RUN_DIRS = 8            # 플릿은 프로젝트마다 실행 디렉토리를 등록한다. 그래서 최근 것만 감시한다

_WATCH_LOCK = threading.Lock()
_FIXED_DIRS: list[Path] = []                                            # 설치할 때의 cwd와 시그널의 out_dir
_RUN_DIRS: "collections.deque[Path]" = collections.deque(maxlen=MAX_WATCHED_RUN_DIRS)
_STUCK: dict[Path, int] = {}       # 지우지 못한 트리거 파일별 mtime. 같은 mtime으로는 다시 덤프하지 않는다
_WATCHER: Optional[threading.Thread] = None
_STOP = threading.Event()


def thread_dump_text() -> str:
    """모든 스레드의 현재 스택을 하나의 텍스트로 만들어 반환한다."""
    names = {t.ident: t.name for t in threading.enumerate()}
    parts = [f"# sbmig thread dump {time.strftime('%Y-%m-%d %H:%M:%S')} pid={os.getpid()} platform={sys.platform}"]
    for tid, frame in sys._current_frames().items():  # noqa: SLF001 - 다른 스레드 프레임을 얻는 공식 방법이다
        parts.append(f"\n--- thread {names.get(tid, '?')} ({tid}) ---")
        parts.append("".join(traceback.format_stack(frame)))
    return "\n".join(parts)


def dump_threads(out_dir: Optional[Path] = None) -> Optional[Path]:
    """스레드 덤프를 `<out_dir>/threads-<ts>.txt`에 기록하고, 그 경로를 로그에 남기고 반환한다.
    ``out_dir``가 없으면 `out/`에 쓴다. 파일에 쓸 수 없으면 stderr로 출력하고 None을 반환한다."""
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


# ---------------------------------------------------------------------------------------------- 트리거 파일
def watch_dump_trigger(*dirs: Path | str) -> None:
    """실행 디렉토리를 `dump-threads` 파일 감시 대상에 추가한다. 최근 MAX_WATCHED_RUN_DIRS개만 감시한다.
    이 함수는 감시 스레드를 시작하지 않는다. 스레드는 CLI와 serve가 따로 시작한다.

    The thread starts in `install_thread_dump_signal()` / `start_dump_trigger_watcher()`; library use and
    tests stay thread-free."""
    with _WATCH_LOCK:
        for d in dirs:
            if d:
                p = Path(d)
                if p in _RUN_DIRS:
                    _RUN_DIRS.remove(p)
                _RUN_DIRS.append(p)


def watched_dirs() -> list[Path]:
    """감시 중인 디렉토리 목록을 중복 없이 반환한다. 고정 디렉토리가 먼저 온다."""
    with _WATCH_LOCK:
        out: list[Path] = []
        for d in (*_FIXED_DIRS, *reversed(_RUN_DIRS)):
            if d not in out:
                out.append(d)
        return out


def check_dump_triggers(dirs: Optional[list[Path]] = None) -> list[Path]:
    """감시 디렉토리에 트리거 파일이 있으면 지우고, 그 디렉토리에 스레드 덤프를 기록한다.
    기록한 덤프 파일 목록을 반환한다.
    편집기에 열려 있어서 지울 수 없는 트리거 파일은 다시 저장되기 전까지 한 번만 덤프한다."""
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
    """``interval``마다 트리거 파일을 확인한다. 감시 스레드가 실행하는 함수다."""
    while not stop.wait(interval):
        try:
            check_dump_triggers()
        except Exception:                    # pragma: no cover - 진단 때문에 프로세스가 죽으면 안 된다
            log.debug("dump-threads trigger check failed", exc_info=True)


def start_dump_trigger_watcher(*dirs: Path | str, interval: float = TRIGGER_POLL_S) -> bool:
    """트리거 파일 감시 데몬 스레드를 시작한다. 이미 실행 중이면 새로 시작하지 않는다.
    감시 스레드가 실행 중이면 True를 반환한다.
    ``dirs``는 고정 감시 디렉토리에 더한다. 등록된 실행 디렉토리도 함께 감시한다."""
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
        except RuntimeError:                 # pragma: no cover - 인터프리터가 종료 중인 경우
            _WATCHER = None
            return False
    return True


def stop_dump_trigger_watcher(timeout: float = 5.0) -> None:
    """감시 스레드를 멈춘다. 테스트에서 쓴다. 다음 start 호출은 스레드를 새로 시작한다."""
    global _WATCHER
    _STOP.set()
    w, _WATCHER = _WATCHER, None
    if w is not None:
        w.join(timeout)


# ---------------------------------------------------------------------------------------------- 시그널
def _signal_name() -> Optional[str]:
    """덤프 요청에 쓸 시그널 이름을 반환한다.
    POSIX는 SIGUSR1, Windows는 SIGBREAK(Ctrl+Break)다. 둘 다 없으면 None을 반환한다."""
    import signal

    if hasattr(signal, "SIGUSR1"):
        return "SIGUSR1"
    if hasattr(signal, "SIGBREAK"):
        return "SIGBREAK"
    return None


def install_thread_dump_signal(out_dir: Optional[Path] = None) -> bool:
    """시그널과 트리거 파일로 스레드 덤프를 요청할 수 있게 한다. 예외를 던지지 않는다.
    시그널 핸들러는 메인 스레드에서만 등록할 수 있다. 다른 스레드에서 호출하면 False를 반환한다.
    그래도 트리거 파일 감시는 시작한다.
    트리거 파일은 현재 디렉토리, ``out_dir``, 모든 작업 공간의 실행 디렉토리에서 찾는다.

    Windows: the handler runs when the main thread next executes Python code, i.e. after the current Maven wait slot
    (≤ 60 s heartbeat); the file trigger answers within TRIGGER_POLL_S. Maven / git children have their own hidden
    console (`CREATE_NO_WINDOW`), so Ctrl+Break never interrupts a build."""
    import signal

    try:
        start_dump_trigger_watcher(Path.cwd(), *([out_dir] if out_dir else []))
    except OSError:                          # pragma: no cover - cwd가 지워진 경우
        pass
    name = _signal_name()
    if name is None or threading.current_thread() is not threading.main_thread():
        return False
    try:
        signal.signal(getattr(signal, name), lambda *_a: dump_threads(out_dir))
        return True
    except (ValueError, OSError):  # pragma: no cover
        return False
