"""여러 프로세스가 KB를 동시에 수정하지 않도록 ``<kb>/.lock`` 잠금을 거는 모듈.
진입점은 :func:`kb_lock`이다.
웹 서버, 플릿, ``sbmig kb`` 명령, 여러 빌드 파드가 KB 하나를 같이 쓰므로 모든 KB 쓰기는 ``kb_lock`` 안에서 한다.
- 잠금은 ``O_CREAT | O_EXCL``로 만든 파일이고 {pid, host, at, token, cmd}를 기록한다.
  같은 프로세스 안에서는 다시 잡을 수 있다
- ``kb.lock_timeout_seconds``보다 오래됐거나, 같은 호스트에서 pid가 죽은 잠금은 경고를 남기고 해제한다
- 시간 안에 잠금을 못 잡으면 경고만 남기고 잠금 없이 진행한다. 잠금 때문에 실행을 실패시키지 않는다
- 잠금 구간 안에서는 LLM이나 Maven을 호출하지 않는다
- 잠금이 얼마나 오래됐는지는 KB 파일 시스템의 시계로 잰다.
  NFS에서는 서버가 mtime을 기록하므로 측정용 파일을 만들어 시계 차이를 측정한다
- 오래된 잠금은 ``.lock.breaker``를 잡은 프로세스 하나만 해제한다
- 오래 걸리는 작업은 :func:`keep_alive`로 mtime을 갱신해 잠금이 오래된 것으로 보이지 않게 한다

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
* release removes the file only when it still holds our token;
* v2.1z-ae (up to three build pods share one KB on NFS): a lock's age is measured on the KB file system's clock — the
  lock's mtime is stamped by the NFS server, so a waiter writes a probe file beside the lock
  (``.lock.probe.<pid>.<random>``, removed at once), reads its mtime and keeps the offset to its own clock (per KB
  directory, refreshed at most every 60 s, only when a waiter needs an age — an uncontended acquire costs nothing
  extra); a failing probe keeps the old comparison (this host's clock). A lock that turns stale right at the
  waiter's deadline (the lock of a pod killed inside its section, seen from its first moment) is waited for until
  it does, at most ``_EDGE`` seconds more, instead of going on without it.
* review fixes (2026-11-10):

  - breaking a stale lock is serialized by a second exclusive file ``.lock.breaker`` (pid / host / token, removed in
    ``finally``): only its holder re-reads the lock, judges it again, moves it aside, checks that the file moved is
    the one judged (same inode + mtime + token) and puts a wrongly moved lock back with ``os.link`` (exclusive —
    never a replace).
    A breaker file of another host expires after ``min(_BREAKER_STALE, max(timeout, 2))`` seconds on the file
    system's clock; one of this host expires exactly when its pid is dead (a live one is never expired by age); a
    waiter that cannot get the breaker keeps waiting within its deadline;
  - a stale lock that cannot be moved (EACCES / EPERM, a sharing violation) is waited for with the normal backoff
    and deadline — never a busy loop;
  - release removes the file only when it reads with our token (an empty / garbled file may be a new owner between
    its create and its payload write);
  - a holder keeps a long section alive with :func:`keep_alive` (``os.utime`` of the lock while it still holds our
    token, at most every ``_KEEPALIVE_EVERY`` s): ``kb reindex``, import, export and the case.json confirmation wait;
  - a non-positive timeout means the default (300 s) with one WARNING (0 made every lock instantly stale);
  - a clock probe that took longer than ``_PROBE_MAX`` (an NFS stall) is discarded, the previous offset is kept.

  What remains, honestly: (a) the breaker file itself is broken by rename-aside without a third level — two waiters
  that both judge a crashed breaker stale at the same moment can both proceed; each still re-reads, re-judges and
  inode-checks the lock, so a double holder then needs that AND a release + new create inside the microseconds
  between their rename and check; (b) the put-back ``os.link`` fails when a third writer created a lock in the
  microseconds between our rename and our link — then the moved lock's owner and the new one overlap once (a WARNING
  says so); (c) a holder frozen past the timeout (lock broken, retaken) that wakes and releases between its token
  read and its unlink removes the new owner's lock — :func:`keep_alive` makes this need a freeze, not a long section;
  (d) an EMPTY lock (no token) replaced in the window by another empty one that got the same inode number AND the
  same mtime (a file system with coarse timestamps) is taken for the judged one.
  On NFS all of this rests on exclusive create / rename / link being decided by the server (they are, NFSv3 / v4).

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
PROBE_PREFIX = LOCK_NAME + ".probe."   # ``.lock.probe.<pid>.<random>`` — never the lock, never a KB data name
DEFAULT_TIMEOUT = 300.0
_MAX_DELAY = 1.0
_PROBE_EVERY = 60.0                    # the file system clock offset is measured again at most this often (s)
_SKEW_NOTE = 2.0                       # an offset larger than this is said once (INFO)
_EDGE = 2.0                            # a lock turning stale within this many seconds of the deadline is waited for
BREAKER_NAME = LOCK_NAME + ".breaker"  # only the holder of this exclusive file may break a stale ``.lock``
_BREAKER_STALE = 30.0                  # a breaker file older than this (fs clock; capped by max(timeout, 2)) expired
_KEEPALIVE_EVERY = 10.0                # keep_alive touches the lock at most this often (s; timeout / 4 when shorter)
_PROBE_MAX = 1.0                       # a clock probe that took longer than this (s) is discarded (an NFS stall)
# 테스트에서 이 파드의 시계만 어긋나게 바꿀 수 있도록 이 이름으로 읽는다
_now = time.time


class _State:
    """KB 디렉토리 하나에 대한 프로세스 안의 잠금 상태를 담는 클래스.
    RLock, 중첩 깊이, 파일에 기록한 토큰을 가진다."""

    def __init__(self) -> None:
        self.rlock = threading.RLock()
        self.depth = 0
        self.token: Optional[str] = None
        # 파일 시스템 시계와 이 호스트 시계의 차이(초)이며, 기다리는 스레드만 RLock을 잡은 채 측정한다
        self.fs_offset: Optional[float] = None
        self.fs_offset_at = 0.0                           # time.monotonic() of the measurement
        self.skew_said: Optional[float] = None            # the offset the INFO line last reported
        self.timeout = DEFAULT_TIMEOUT                    # the timeout the current holder took the lock with
        self.alive_at = 0.0                               # time.monotonic() of the last keep_alive touch
        self.lost_said = False                            # keep_alive said "not ours any more" for this holding
        self.timeout_said = False                         # the non-positive timeout WARNING was given
        self.unmovable_said = False                       # the "stale but cannot be moved" WARNING was given


_STATES: dict[str, _State] = {}
_GUARD = threading.Lock()


def _state(root: Path) -> _State:
    # 같은 KB를 다른 경로 표기로 열어도 같은 상태를 쓰도록 realpath를 키로 쓴다
    key = os.path.realpath(str(root))
    with _GUARD:
        st = _STATES.get(key)
        if st is None:
            st = _STATES[key] = _State()
        return st


def lock_path(root: Path) -> Path:
    """잠금 파일 경로 ``<root>/.lock``을 반환한다."""
    return Path(root) / LOCK_NAME


def _read(path: Path) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def pid_alive(pid: int) -> bool:
    """이 호스트에서 ``pid`` 프로세스가 살아 있는지 확인한다.
    알 수 없으면 True를 반환하고, 그런 잠금은 경과 시간으로만 해제한다.

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
        # Windows의 os.kill(pid, 0)은 CTRL_C_EVENT를 보내므로 OpenProcess / GetExitCodeProcess로 확인한다
        # 접근이 거부되면 살아 있는 것으로 본다
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


def _probe_offset(root: Path) -> Optional[float]:
    """KB 파일 시스템 시계에서 이 호스트 시계를 뺀 값(초)을 측정한다.
    잠금 옆에 측정용 파일을 만들어 mtime을 비교하고 바로 지운다.
    실패하면 0.0을, 측정이 ``_PROBE_MAX``초보다 오래 걸렸으면 값을 믿을 수 없으므로 None을 반환한다.

    File system clock minus this host's clock, from the mtime of a probe file written beside the lock (removed at
    once); 0.0 when the probe fails; None when the probe itself took longer than ``_PROBE_MAX`` (an NFS stall would
    bias the offset by up to half of it — discarded)."""
    probe = Path(root) / f"{PROBE_PREFIX}{os.getpid()}.{uuid.uuid4().hex[:8]}"
    made = False
    try:
        t0 = time.monotonic()
        before = _now()
        fd = os.open(str(probe), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        made = True
        try:
            os.write(fd, b"p")                            # a write: the server stamps the mtime with its clock
        finally:
            os.close(fd)
        after = _now()
        took = time.monotonic() - t0
        if took > _PROBE_MAX:
            # 측정이 오래 걸리면 서버가 mtime을 기록한 시점을 알 수 없으므로 값을 버린다
            log.debug("kb lock %s: clock probe took %.1f s — discarded", lock_path(root), took)
            return None
        return os.stat(str(probe)).st_mtime - (before + after) / 2.0
    except OSError as e:
        log.debug("kb lock %s: clock probe failed (%s) — lock ages on this host's clock", lock_path(root), e)
        return 0.0
    finally:
        if made:
            try:
                os.unlink(str(probe))
            except OSError:
                pass


def _fs_offset(root: Path) -> float:
    """KB 디렉토리별로 저장해 둔 시계 차이를 반환한다. 최대 ``_PROBE_EVERY``초마다 다시 측정한다.
    차이가 ``_SKEW_NOTE``초보다 크면 INFO 로그를 한 줄 남긴다. 잠금을 기다리는 쪽만 호출한다.

    The cached file system clock offset of ``root`` (measured again at most every ``_PROBE_EVERY`` s); only a
    waiter asks for it."""
    st = _state(root)
    mono = time.monotonic()
    if st.fs_offset is not None and mono - st.fs_offset_at < _PROBE_EVERY:
        return st.fs_offset
    off = _probe_offset(root)
    if off is None:
        # 오래 걸린 측정값은 저장하지 않고 이전 값을 쓴 뒤 다음에 다시 측정한다
        return st.fs_offset if st.fs_offset is not None else 0.0
    st.fs_offset, st.fs_offset_at = off, mono
    if abs(off) > _SKEW_NOTE and (st.skew_said is None or abs(off - st.skew_said) > _SKEW_NOTE):
        st.skew_said = off
        log.info("kb lock %s: this host's clock and the KB file system's clock differ by %+.0f s — lock ages are "
                 "measured on the file system's clock", lock_path(root), off)
    return off


def _judge(path: Path, info: Optional[dict[str, Any]], timeout: float) -> tuple[bool, Optional[float]]:
    """잠금을 해제해도 되는지 판단하고 (해제 가능 여부, 오래된 잠금이 되기까지 남은 초 또는 None)을 반환한다.
    같은 호스트에서 pid가 죽은 잠금은 바로 해제 대상이다.
    그 밖에는 파일 시스템 시계 기준 경과 시간을 timeout과 비교한다.

    (stale?, seconds until it turns stale by age or None). A same-host lock of a dead pid is stale without an age;
    otherwise the age is measured on the file system's clock against ``timeout`` (``max(timeout, 5)`` for an empty /
    garbled lock)."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False, None                                # gone already: the next O_EXCL wins
    if info is not None and info.get("host") == socket.gethostname() and not pid_alive(info.get("pid", 0)):
        return True, None
    # empty / garbled: a crash between create and write (or read right in between) — a longer limit
    limit = max(timeout, 5.0) if info is None else timeout
    age = _now() + _fs_offset(path.parent) - mtime
    return age > limit, limit - age


def _stale(path: Path, info: Optional[dict[str, Any]], timeout: float) -> bool:
    """해제해도 되는 잠금인지 확인한다.
    비었거나 깨진 잠금은 max(timeout, 5초), 그 밖에는 timeout을 기준으로 본다.
    같은 호스트에서 pid가 죽은 잠금도 해제 대상이다."""
    return _judge(path, info, timeout)[0]


def _ident(path: Path) -> Optional[tuple[int, int, int]]:
    # 빈 잠금에는 토큰이 없고 inode 번호는 재사용될 수 있으므로 (장치,
    # inode, mtime_ns)로 확인한 파일과 옮긴 파일이 같은지 본다
    try:
        s = os.stat(str(path))
    except OSError:
        return None
    return s.st_dev, s.st_ino, s.st_mtime_ns


def _put_back(aside: Path, path: Path) -> bool:
    """잘못 옮긴 파일을 덮어쓰지 않고 원래 위치로 되돌린다.
    ``os.link``를 쓰고, 하드 링크를 쓸 수 없으면 ``os.rename``을 쓴다.
    되돌렸으면 True를 반환한다.

    Put a wrongly moved file back, exclusively only (``os.link`` fails when the name exists — never a replace; on
    Windows without hard links ``os.rename``, which fails on an existing target). True when it is back."""
    try:
        os.link(str(aside), str(path))
    except FileExistsError:
        return False
    except (OSError, NotImplementedError):
        if os.name != "nt":
            return False
        try:
            os.rename(str(aside), str(path))              # Windows: rename never overwrites
            return True
        except OSError:
            return False
    try:
        os.unlink(str(aside))
    except OSError:
        pass
    return True


def _remove_judged(path: Path, ident: tuple[int, int, int], token: Optional[str] = None) -> str:
    """확인한 바로 그 잠금 파일만 지운다.
    다른 이름으로 옮겨 같은 파일인지 확인한 뒤 지우고, 다른 파일이면 :func:`_put_back`으로 되돌린다.
    결과로 removed / gone / changed / failed 중 하나를 반환한다.

    Remove exactly the judged file: move it aside, check the moved file is the judged one (same device, inode and
    mtime; the same token when the judged one had one), unlink it; another file moved by mistake is put back
    exclusively. → removed / gone / changed / failed."""
    aside = path.with_name(f"{path.name}.stale-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        os.replace(str(path), str(aside))
    except FileNotFoundError:
        return "gone"
    except OSError as e:                                  # EACCES / EPERM / a Windows sharing violation
        log.debug("kb lock %s: not moved aside (%s)", path, e)
        return "failed"
    if _ident(aside) != ident or (token is not None and (_read(aside) or {}).get("token") != token):
        # 확인한 뒤에 다른 프로세스가 새로 잡은 잠금을 옮긴 것이므로 덮어쓰지 않고 되돌린다
        if not _put_back(aside, path):
            log.warning("kb lock %s: a lock taken meanwhile was moved aside while breaking a stale one and could not "
                        "be put back (the name was taken again) — two writers may overlap once", path)
            try:
                os.unlink(str(aside))
            except OSError:
                pass
        return "changed"
    try:
        os.unlink(str(aside))
    except OSError:
        pass
    return "removed"


def _new_payload(token: str) -> str:
    return json.dumps({"pid": os.getpid(), "host": socket.gethostname(),
                       "at": utcnow().isoformat(timespec="seconds"), "token": token,
                       "cmd": " ".join(os.path.basename(a) if i == 0 else a
                                       for i, a in enumerate(sys.argv[:3]))[:120]})


def _take_breaker(root: Path, timeout: float) -> Optional[str]:
    """잠금을 해제할 권한(``.lock.breaker``)을 O_EXCL로 잡는다.
    잡으면 우리 토큰을, 다른 프로세스가 해제하는 중이면 None을 반환한다.
    기존 breaker 파일은 같은 호스트면 pid가 죽었을 때만, 다른 호스트면 일정 시간이 지났을 때만 지우고 한 번 더 시도한다.

    Take the right to break (``.lock.breaker``, O_EXCL). An existing one is removed only when it is dead: this host's
    by its pid (a live one is never expired by age), another host's when older than ``min(_BREAKER_STALE,
    max(timeout, 2))`` s on the file system's clock; then one more try. Our token, or None (somebody else is
    breaking: wait)."""
    bpath = Path(root) / BREAKER_NAME
    token = uuid.uuid4().hex
    for _ in range(2):
        try:
            fd = os.open(str(bpath), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except (FileExistsError, PermissionError):
            ident, binfo = _ident(bpath), _read(bpath)
            if ident is None:
                continue                                  # released just now: try again
            if binfo is not None and binfo.get("host") == socket.gethostname():
                # 같은 호스트면 pid로 정확히 알 수 있으므로 살아 있는 breaker는 경과 시간과 관계없이 기다린다
                stale = not pid_alive(binfo.get("pid", 0))
            else:
                stale, _ = _judge(bpath, binfo, min(_BREAKER_STALE, max(timeout, 2.0)))
            if not stale or _ident(bpath) != ident:
                return None
            if _remove_judged(bpath, ident, (binfo or {}).get("token")) == "removed":
                log.warning("kb lock %s: a breaker left by %s expired — removed", bpath, _owner_text(binfo))
            continue
        except OSError as e:
            log.debug("kb lock %s: breaker not taken (%s)", bpath, e)
            return None
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(_new_payload(token))
        except OSError as e:
            log.debug("kb lock %s: breaker not written (%s)", bpath, e)
            try:
                os.unlink(str(bpath))                     # ours: we just created it exclusively
            except OSError:
                pass
            return None
        return token
    return None


def _drop_breaker(root: Path, token: str) -> None:
    """breaker 파일에 이 프로세스의 토큰이 들어 있을 때만 그 파일을 지운다."""
    bpath = Path(root) / BREAKER_NAME
    if (_read(bpath) or {}).get("token") != token:
        return
    try:
        os.unlink(str(bpath))
    except OSError:
        pass


def _break(path: Path, info: Optional[dict[str, Any]], timeout: float = DEFAULT_TIMEOUT) -> bool:
    """오래된 잠금을 해제한다.
    ``.lock.breaker``를 잡은 프로세스만 해제하고, 확인한 잠금이 맞는지 다시 본 뒤 지운다.
    해제했거나 이미 없으면 True를 반환한다.
    False면 호출하는 쪽이 마감 시간을 확인하고 잠시 쉰 뒤 다시 확인한다.

    Break a stale lock — only as the holder of ``.lock.breaker``: re-read the lock, check it is the judged one (token,
    inode), judge it again, then move it aside and remove it (:func:`_remove_judged`). True when broken or already
    gone (create again now); False otherwise (another breaker at work, the lock changed, the rename refused) — the
    caller then checks its deadline and sleeps as for a live lock, never spins."""
    root = path.parent
    btok = _take_breaker(root, timeout)
    if btok is None:
        return False
    try:
        ident = _ident(path)
        if ident is None:
            return True                                   # released meanwhile: the next O_EXCL wins
        now_info = _read(path)
        if (now_info or {}).get("token") != (info or {}).get("token"):
            return False                                  # changed since the judgement: judge again next round
        stale, _ = _judge(path, now_info, timeout)
        if not stale or _ident(path) != ident:
            return False
        res = _remove_judged(path, ident, (now_info or {}).get("token"))
        if res == "removed":
            log.warning("kb lock %s was stale (%s) — broken", path, _owner_text(info))
            return True
        if res == "gone":
            return True
        if res == "failed":
            st = _state(root)
            if not st.unmovable_said:
                st.unmovable_said = True
                log.warning("kb lock %s is stale (%s) but cannot be moved aside — waiting as for a live lock",
                            path, _owner_text(info))
        return False
    finally:
        _drop_breaker(root, btok)


def _acquire(root: Path, timeout: float) -> Optional[str]:
    """잠금 파일을 O_EXCL로 만들 때까지 간격을 늘리며 기다린다. 오래된 잠금은 해제한다.
    잡으면 우리 토큰을, 타임아웃이나 디렉토리 문제가 있으면 None을 반환한다(잠금 없이 진행)."""
    path = lock_path(root)
    try:
        Path(root).mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("kb lock %s unavailable (%s) — going on without it", path, e)
        return None
    token = uuid.uuid4().hex
    payload = _new_payload(token)
    deadline = time.monotonic() + timeout
    delay, said, extended = 0.05, False, False
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except (FileExistsError, PermissionError):        # Windows: a lock being deleted reads as PermissionError
            # Windows에서는 지워지는 중인 잠금 파일이 PermissionError로 보이므로 잡혀 있는 것으로 보고 다시 시도한다
            info = _read(path)
            stale, until = _judge(path, info, timeout)
            # 해제하지 못했으면 바로 다시 시도하지 않고 아래에서 마감 시간 확인과 대기를 한다
            if stale and _break(path, info, timeout):
                continue
            left = deadline - time.monotonic()
            if left <= 0 and not extended and (until is None or until < _EDGE):
                # 죽은 파드의 잠금은 마감 시간과 거의 같은 때 오래된 잠금이 되므로, 그 경계에서 잠금 없이
                # 진행하지 않도록 최대 _EDGE초 더 기다린다
                # until이 None이면 잠금이 방금 풀린 것이므로 마감이 지나도 O_EXCL을 한 번 더 시도한다
                deadline = time.monotonic() + max(until or 0.0, 0.0) + 0.25
                extended = True
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
        except OSError as e:
            # 빈 잠금은 다른 프로세스가 주인을 알 수 없고 release도 지우지
            # 않으므로, 소유자를 기록하지 못한 잠금은 놓는다
            log.warning("kb lock %s: could not write the owner (%s) — the lock is dropped, going on without it",
                        path, e)
            try:
                os.unlink(str(path))                      # ours: we created it exclusively a moment ago
            except OSError:
                pass
            return None
        return token


def _release(root: Path, token: str) -> None:
    """잠금 파일이 우리 토큰으로 읽힐 때만 지운다.
    비었거나 깨진 파일은 방금 생성된 새 잠금일 수 있으므로 건드리지 않는다.
    잠깐 읽히지 않는 경우를 위해 몇 번 다시 읽는다.

    Remove the file only when it reads with our token; an empty / garbled file may be a new owner between its create
    and its payload write and is left alone (read again briefly first)."""
    path = lock_path(root)
    info = _read(path)
    for _ in range(3):
        if info is not None or not path.exists():
            break
        time.sleep(0.05)
        info = _read(path)
    if info is None or info.get("token") != token:
        if info is not None:
            log.debug("kb lock %s is not ours any more (%s); left alone", path, _owner_text(info))
        elif path.exists():
            log.debug("kb lock %s does not read at release — left alone (it is not known to be ours)", path)
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def keep_alive(root: Path | str, *, force: bool = False) -> bool:
    """잠금 파일에 아직 우리 토큰이 있으면 mtime을 갱신해 다른 프로세스가 해제하지 않게 한다.
    잠금을 잡은 쪽이 오래 걸리는 작업 중에 호출한다.
    자주 호출해도 되며 실제 갱신은 ``min(_KEEPALIVE_EVERY, timeout / 4)``초에 한 번이다.
    잠금이 없으면 False를 반환한다.

    Called by the holder inside a long section: touch the lock file (``os.utime``; the server's time on NFS) while it
    still holds our token, so nobody breaks it as stale. Cheap to call often — it touches at most every
    ``min(_KEEPALIVE_EVERY, timeout / 4)`` s (``force``: now). False when nothing was touched (not held, too soon,
    not ours any more)."""
    root = Path(root)
    st = _state(root)
    tok = st.token
    if st.depth <= 0 or tok is None:
        return False
    mono = time.monotonic()
    if not force and mono - st.alive_at < min(_KEEPALIVE_EVERY, max(st.timeout / 4.0, 0.05)):
        return False
    st.alive_at = mono
    path = lock_path(root)
    info = _read(path)
    if (info or {}).get("token") != tok:
        if not st.lost_said:
            st.lost_said = True
            log.warning("kb lock %s is no longer ours (now %s) — the section goes on, its writes may overlap",
                        path, _owner_text(info))
        return False
    try:
        os.utime(str(path), None)
    except OSError as e:
        log.debug("kb lock %s: not touched (%s)", path, e)
        return False
    return True


def _timeout_of(root: Path, timeout: Optional[float]) -> float:
    """잠금 대기 시간이자 오래된 잠금으로 보는 기준 시간(초)을 반환한다.
    None이나 0 이하면 기본값을 쓰고, 0 이하일 때는 KB 디렉토리마다 WARNING을 한 번 남긴다."""
    if timeout is None:
        return DEFAULT_TIMEOUT
    try:
        t = float(timeout)
    except (TypeError, ValueError):
        t = 0.0
    if t > 0:
        return t
    st = _state(root)
    if not st.timeout_said:
        st.timeout_said = True
        log.warning("kb lock %s: kb.lock_timeout_seconds is %s (not positive) — the default %.0f s is used",
                    lock_path(root), timeout, DEFAULT_TIMEOUT)
    return DEFAULT_TIMEOUT


@contextmanager
def kb_lock(root: Path | str, timeout: Optional[float] = None) -> Iterator[bool]:
    """KB에 쓰는 동안 ``<root>/.lock`` 잠금을 잡는다. 잡았으면 True, 못 잡고 진행하면 False를 yield한다.
    같은 프로세스 안에서는 다시 잡을 수 있다. ``timeout``이 0 이하면 기본값을 쓴다.

    Hold ``<root>/.lock`` for a KB write. Yields True when the lock is held (False: going on without it). A
    non-positive ``timeout`` means the default (one WARNING)."""
    root = Path(root)
    t = _timeout_of(root, timeout)
    st = _state(root)
    got = st.rlock.acquire(timeout=t)
    if not got:
        log.warning("kb lock %s: another thread of this process kept it for %.0f s — going on without it",
                    lock_path(root), t)
        yield False
        return
    try:
        if st.depth == 0:
            st.token = _acquire(root, t)
            st.timeout, st.alive_at, st.lost_said = t, time.monotonic(), False
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
    """이 스레드가 ``root``의 잠금을 잡고 있는지 확인한다. 테스트에서 쓴다.

    This thread holds the lock of ``root`` (tests / assertions)."""
    st = _state(Path(root))
    return st.depth > 0 and st.token is not None
