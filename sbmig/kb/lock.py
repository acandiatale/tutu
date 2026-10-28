"""프로세스 간 KB 잠금 ``<kb>/.lock``(round 10) — :func:`kb_lock`.
웹 서버·플릿·``sbmig kb`` 명령이 한 KB 를 같이 쓸 수 있으므로 모든 KB 쓰기(``errors.jsonl``, case.json,
``fix_v<N>.py``, cases.md, 에러별 JSON)는 ``kb_lock`` 안에서 한다. 방식: ``O_CREAT | O_EXCL`` 로 만든
파일(Windows·POSIX·공유 폴더 모두 동작)에 {pid, host, at, token, cmd} 를 적는다; 프로세스 안에서는 재진입
가능(KB 디렉토리마다 RLock, 가장 바깥 holder 만 파일을 만들고 지움). 오래됐거나(``kb.lock_timeout_seconds``)
같은 호스트에서 pid 가 죽은 잠금은 경고와 함께 깬다; 아니면 기다리다가 시간이 다 되면 경고만 남기고 잠금 없이
진행한다 — 잠금 때문에 실행을 실패시키지 않는다. 잠긴 구간은 짧게: 그 안에서 LLM 이나 Maven 을 부르지 않는다.

Cross-process KB lock ``<kb>/.lock`` (round 10, 2026-10-26).

The web server, a fleet and ``sbmig kb …`` commands may share one KB. Every KB write (append, index save, case /
recipe / catalog file, views, audit) runs inside :func:`kb_lock`:

* the lock is a file created with ``O_CREAT | O_EXCL`` holding ``{pid, host, at, token, cmd}`` — works on Windows
  and POSIX, local disks and shares;
* re-entrant within a process (one ``threading.RLock`` per KB directory; only the outermost holder creates /
  removes the file), so nested writes (``credit_recipe`` → ``promote``) never deadlock;
* a lock is stale — and broken, with a warning — when it is older than ``kb.lock_timeout_seconds`` or its pid is
  gone (same host); otherwise a writer waits with backoff up to the timeout, then logs a warning and goes on WITHOUT
  the lock (a run is never failed over it);
* release removes the file only when it still holds our token.

Keep the locked sections short: never call the LLM or Maven inside one.


"""
from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from sbmig.core.models import utcnow

log = logging.getLogger("sbmig.kb")

LOCK_NAME = ".lock"
DEFAULT_TIMEOUT = 300.0
_MAX_DELAY = 1.0


class _State:
    """KB 디렉토리 하나의 프로세스 안 상태: RLock, 중첩 깊이, 우리가 파일에 쓴 토큰."""

    def __init__(self) -> None:
        self.rlock = threading.RLock()
        self.depth = 0
        self.token: Optional[str] = None


_STATES: dict[str, _State] = {}
_GUARD = threading.Lock()


def _state(root: Path) -> _State:
    # 같은 KB 를 다른 경로 표기(상대 경로, 심볼릭 링크)로 불러도 같은 상태를 쓰도록 realpath 로 키를 만든다
    key = os.path.realpath(str(root))
    with _GUARD:
        st = _STATES.get(key)
        if st is None:
            st = _STATES[key] = _State()
        return st


def lock_path(root: Path) -> Path:
    """잠금 파일 경로 ``<root>/.lock``."""
    return Path(root) / LOCK_NAME


def _read(path: Path) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def pid_alive(pid: int) -> bool:
    """이 호스트에서 ``pid`` 가 살아 있나? 모르면 True(그런 잠금은 나이로만 깬다).

    Is process ``pid`` running on this host? Unknown → True (only the age breaks such a lock)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        # Windows 에서 os.kill(pid, 0) 은 존재 확인이 아니라 CTRL_C_EVENT 를 보낸다 — 그래서 OpenProcess /
        # GetExitCodeProcess 로 묻는다(접근 거부 = 살아 있음)
        if os.name == "nt":
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)       # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(0x1000, False, pid)               # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return ctypes.get_last_error() == 5                          # ERROR_ACCESS_DENIED → it exists
            try:
                code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return code.value == 259                                 # STILL_ACTIVE
                return True
            finally:
                kernel32.CloseHandle(handle)
        os.kill(pid, 0)                                   # POSIX only: on Windows signal 0 would be CTRL_C_EVENT
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:  # noqa: BLE001 - unknown: assume alive
        return True
    return True


def _owner_text(info: Optional[dict[str, Any]]) -> str:
    if not info:
        return "an unknown process"
    return f"pid {info.get('pid', '?')} on {info.get('host', '?')} since {info.get('at', '?')}" + (
        f" ({info['cmd']})" if info.get("cmd") else "")


def _stale(path: Path, info: Optional[dict[str, Any]], timeout: float) -> bool:
    """깨도 되는 잠금인가: 내용이 비었거나 깨진 잠금은 max(timeout, 5초)보다 오래됐을 때, 아니면 timeout 보다
    오래됐거나 같은 호스트에서 pid 가 죽었을 때."""
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return False                                      # gone already: the next O_EXCL wins
    if info is None:                                      # empty / garbled: a crash between create and write
        return age > max(timeout, 5.0)
    if age > timeout:
        return True
    return info.get("host") == socket.gethostname() and not pid_alive(info.get("pid", 0))


def _break(path: Path, info: Optional[dict[str, Any]]) -> None:
    """낡은 잠금을 옆 이름으로 옮긴 뒤 지운다. 옮기는 사이 다른 프로세스가 새 잠금을 잡았으면(토큰이 다름)
    되돌려 놓는다 — 남의 새 잠금을 지우지 않으려고."""
    aside = path.with_name(f"{LOCK_NAME}.stale-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        os.replace(path, aside)
    except OSError:
        return                                            # somebody else broke / released it
    moved = _read(aside)
    if info is not None and moved is not None and moved.get("token") != info.get("token") and not path.exists():
        try:                                              # we moved a fresh lock taken meanwhile: put it back
            os.replace(aside, path)
            return
        except OSError:
            pass
    log.warning("kb lock %s was stale (%s) — broken", path, _owner_text(info))
    try:
        os.unlink(aside)
    except OSError:
        pass


def _acquire(root: Path, timeout: float) -> Optional[str]:
    """잠금 파일을 O_EXCL 로 만들 때까지 백오프하며 기다린다(낡은 잠금은 깬다). 우리 토큰을 돌려주고, 시간
    초과나 디렉토리 문제면 None(잠금 없이 진행)."""
    path = lock_path(root)
    try:
        Path(root).mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("kb lock %s unavailable (%s) — going on without it", path, e)
        return None
    token = uuid.uuid4().hex
    payload = json.dumps({"pid": os.getpid(), "host": socket.gethostname(),
                          "at": utcnow().isoformat(timespec="seconds"), "token": token,
                          "cmd": " ".join(os.path.basename(a) if i == 0 else a
                                          for i, a in enumerate(sys.argv[:3]))[:120]})
    deadline = time.monotonic() + timeout
    delay, said = 0.05, False
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except (FileExistsError, PermissionError):        # Windows: a lock being deleted reads as PermissionError
            # Windows: 지워지는 중인 잠금 파일은 PermissionError
            # 로 보인다 — 잡혀 있는 것으로 보고 다시 시도한다
            info = _read(path)
            if _stale(path, info, timeout):
                _break(path, info)
                continue
            left = deadline - time.monotonic()
            if left <= 0:
                log.warning("kb lock %s still held by %s after %.0f s — going on without it", path,
                            _owner_text(info), timeout)
                return None
            if not said and timeout - left >= 2.0:
                log.info("waiting for the kb lock %s (held by %s)", path, _owner_text(info))
                said = True
            time.sleep(min(delay, left))
            delay = min(delay * 2, _MAX_DELAY)
            continue
        except OSError as e:
            log.warning("kb lock %s unavailable (%s) — going on without it", path, e)
            return None
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
        except OSError as e:  # pragma: no cover - disk full right now
            log.warning("kb lock %s: could not write the owner (%s)", path, e)
        return token


def _release(root: Path, token: str) -> None:
    """파일이 아직 우리 토큰을 갖고 있을 때만 지운다(우리 잠금이 깨지고 남이 잡았으면 건드리지 않음)."""
    path = lock_path(root)
    info = _read(path)
    if info is not None and info.get("token") != token:
        log.debug("kb lock %s is not ours any more (%s); left alone", path, _owner_text(info))
        return
    try:
        os.unlink(path)
    except OSError:
        pass


@contextmanager
def kb_lock(root: Path | str, timeout: Optional[float] = None) -> Iterator[bool]:
    """KB 쓰기 동안 ``<root>/.lock`` 을 잡는다. 잡았으면 True, 못 잡고 그냥 진행하면 False 를 내준다. 같은
    프로세스 안에서는 재진입 가능하다(바깥 holder 만 파일을 만들고 지움).

    Hold ``<root>/.lock`` for a KB write. Yields True when the lock is held (False: going on without it)."""
    root = Path(root)
    t = DEFAULT_TIMEOUT if timeout is None else max(0.0, float(timeout))
    st = _state(root)
    got = st.rlock.acquire(timeout=t) if t > 0 else st.rlock.acquire(blocking=False)
    if not got:
        log.warning("kb lock %s: another thread of this process kept it for %.0f s — going on without it",
                    lock_path(root), t)
        yield False
        return
    try:
        if st.depth == 0:
            st.token = _acquire(root, t)
        st.depth += 1
        try:
            yield st.token is not None
        finally:
            st.depth -= 1
            if st.depth == 0 and st.token is not None:
                _release(root, st.token)
                st.token = None
    finally:
        st.rlock.release()


def holding(root: Path | str) -> bool:
    """이 스레드가 ``root`` 의 잠금을 잡고 있나(테스트·단언용).

    This thread holds the lock of ``root`` (tests / assertions)."""
    st = _state(Path(root))
    return st.depth > 0 and st.token is not None
