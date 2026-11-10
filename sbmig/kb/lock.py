"""프로세스 간 KB 잠금 ``<kb>/.lock``(round 10) — :func:`kb_lock`.
웹 서버·플릿·``sbmig kb`` 명령이 한 KB 를 같이 쓸 수 있으므로 모든 KB 쓰기(``errors.jsonl``, case.json,
``fix_v<N>.py``, cases.md, 에러별 JSON)는 ``kb_lock`` 안에서 한다. 방식: ``O_CREAT | O_EXCL`` 로 만든
파일(Windows·POSIX·공유 폴더 모두 동작)에 {pid, host, at, token, cmd} 를 적는다; 프로세스 안에서는 재진입
가능(KB 디렉토리마다 RLock, 가장 바깥 holder 만 파일을 만들고 지움). 오래됐거나(``kb.lock_timeout_seconds``)
같은 호스트에서 pid 가 죽은 잠금은 경고와 함께 깬다; 아니면 기다리다가 시간이 다 되면 경고만 남기고 잠금 없이
진행한다 — 잠금 때문에 실행을 실패시키지 않는다. 잠긴 구간은 짧게: 그 안에서 LLM 이나 Maven 을 부르지 않는다.
v2.1z-ae(빌드 파드 셋이 NFS 의 KB 하나를 같이 씀): 잠금의 나이는 KB 파일 시스템의 시계로 잰다 — 잠금 파일의 mtime 은
NFS 서버가 찍으므로, 기다리는 쪽이 잠금 옆에 탐침 파일(``.lock.probe.<pid>.<난수>``)을 써서 서버 시계와 이 파드 시계의
차이를 재고(KB 마다 최대 60초에 한 번, 경합할 때만) 그 차이를 더한 '지금' 으로 나이를 계산한다. 탐침이 실패하면 예전
비교(이 파드의 시계)를 쓴다. 마감 순간에 막 낡아 가는 잠금은 잠금 없이 가지 않고 낡을 때까지 잠깐 더 기다린다.
검토 반영(2026-11-10): 낡은 잠금을 깨는 일은 두 번째 배타 파일 ``.lock.breaker`` 를 잡은 한 프로세스만 한다(그 안에서
다시 읽고 다시 판정하고, 옮긴 파일이 판정한 그 파일(inode)인지 확인하고, 잘못 옮긴 새 잠금은 ``os.link`` 로만 —
덮어쓰지 않고 — 되돌린다). 깨지 못한 낡은 잠금(이름 바꾸기 거부)은 바쁘게 돌지 않고 마감까지 기다린다. 오래 걸리는
구간(재색인·내보내기·case.json 확인 대기)은 :func:`keep_alive` 로 잠금의 mtime 을 새로 찍어 낡아 보이지 않게 한다.
``kb.lock_timeout_seconds`` 가 0 이하이면 기본값(300초)을 쓰고 WARNING 한 줄. 지연된 시계 탐침(1초 넘게 걸림)은 버린다.

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
# 시계는 이 이름으로 읽는다 — 테스트가 이 파드의 시계만 어긋나게 할 수 있게(파일 시스템의 mtime 은 그대로)
_now = time.time


class _State:
    """KB 디렉토리 하나의 프로세스 안 상태: RLock, 중첩 깊이, 우리가 파일에 쓴 토큰."""

    def __init__(self) -> None:
        self.rlock = threading.RLock()
        self.depth = 0
        self.token: Optional[str] = None
        # 파일 시스템 시계 - 이 호스트 시계(초). 기다리는 스레드만 RLock 을 쥔 채 재고 쓴다
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


def _probe_offset(root: Path) -> Optional[float]:
    """KB 파일 시스템의 시계 - 이 호스트의 시계(초): 잠금 옆에 탐침 파일을 O_EXCL 로 만들어 한 바이트 쓰고,
    그 mtime(NFS 면 서버가 찍음)을 쓰기 전후 이 호스트 시계의 가운데와 비교한 뒤 바로 지운다. 실패하면 0.0
    (= 예전처럼 이 호스트의 시계로 비교). 만들기·쓰기가 ``_PROBE_MAX`` 초보다 오래 걸렸으면(NFS 지연) None — 그
    값은 지연의 절반만큼 틀릴 수 있어 쓰지 않는다.

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
            # 탐침이 오래 걸리면(NFSv4 grace 기간 등) 서버가 mtime 을 찍은 순간을 모른다 — 버린다
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
    """KB 디렉토리마다 기억해 둔 파일 시스템 시계 차이(최대 ``_PROBE_EVERY`` 초마다 다시 잼). 차이가
    ``_SKEW_NOTE`` 초보다 크면 INFO 한 줄(같은 값이면 다시 말하지 않음). 기다리는 쪽만 부른다 — 잠금이 비어
    있으면 탐침도 없다.

    The cached file system clock offset of ``root`` (measured again at most every ``_PROBE_EVERY`` s); only a
    waiter asks for it."""
    st = _state(root)
    mono = time.monotonic()
    if st.fs_offset is not None and mono - st.fs_offset_at < _PROBE_EVERY:
        return st.fs_offset
    off = _probe_offset(root)
    if off is None:
        # 지연된 탐침은 기억하지 않는다: 예전 값(없으면 0)을 쓰고 다음에 다시 잰다
        return st.fs_offset if st.fs_offset is not None else 0.0
    st.fs_offset, st.fs_offset_at = off, mono
    if abs(off) > _SKEW_NOTE and (st.skew_said is None or abs(off - st.skew_said) > _SKEW_NOTE):
        st.skew_said = off
        log.info("kb lock %s: this host's clock and the KB file system's clock differ by %+.0f s — lock ages are "
                 "measured on the file system's clock", lock_path(root), off)
    return off


def _judge(path: Path, info: Optional[dict[str, Any]], timeout: float) -> tuple[bool, Optional[float]]:
    """(깨도 되나, 나이로 낡기까지 남은 초 또는 None). 같은 호스트에서 pid 가 죽은 잠금은 나이를 묻지 않고
    낡음(탐침도 안 함); 아니면 나이 = (이 호스트 시계 + 파일 시스템 시계 차이) - 잠금의 mtime 이 한계(내용이 비었거나
    깨진 잠금은 max(timeout, 5초), 아니면 timeout)를 넘을 때 낡음.

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
    """깨도 되는 잠금인가: 내용이 비었거나 깨진 잠금은 max(timeout, 5초)보다 오래됐을 때, 아니면 timeout 보다
    오래됐거나 같은 호스트에서 pid 가 죽었을 때. 나이는 KB 파일 시스템의 시계로 잰다(:func:`_judge`)."""
    return _judge(path, info, timeout)[0]


def _ident(path: Path) -> Optional[tuple[int, int, int]]:
    # (장치, inode, mtime_ns): 판정한 파일과 옮긴 파일이 같은지 본다 — 토큰은 빈 잠금에 없고, inode 번호는 지운 뒤
    # 곧 재사용될 수 있다(ext4); 새로 만든 파일·keep_alive 로 건드린 파일은 mtime 이 다르다
    try:
        s = os.stat(str(path))
    except OSError:
        return None
    return s.st_dev, s.st_ino, s.st_mtime_ns


def _put_back(aside: Path, path: Path) -> bool:
    """잘못 옮긴 파일을 제자리로 — 배타적으로만(``os.link``: 그 이름이 이미 있으면 실패, 덮어쓰지 않음). 하드 링크가
    없는 곳에서는 Windows 의 ``os.rename``(대상이 있으면 실패)만 쓴다. 되돌렸으면 True.

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
    """판정한 그 파일(``ident``, 토큰이 있으면 그 토큰)만 지운다: 옆 이름으로 옮기고, 옮긴 것이 그 파일인지 확인한 뒤
    지운다. 다른 파일을 옮겼으면 :func:`_put_back`. 결과: removed / gone(이미 없음) / changed(다른 파일이었음) /
    failed(옮기기 거부).

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
        # 판정한 뒤 풀리고 남이 새로 잡은 파일을 옮겼다 — 덮어쓰지 않고 되돌린다
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
    """깨는 권리(``.lock.breaker``)를 O_EXCL 로 잡는다. 이미 있으면: 같은 호스트 것은 pid 가 죽었을 때만, 다른 호스트
    것은 파일 시스템 시계로 ``min(_BREAKER_STALE, max(timeout, 2))`` 초보다 오래됐을 때만(죽은 breaker) 지우고 한 번
    더 해 본다. 우리 토큰 또는 None(다른 프로세스가 깨는 중 — 기다린다).

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
                # 같은 호스트: pid 로 정확히 안다 — 살아 있는 breaker 는 나이와 상관없이 기다린다
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
    """우리 토큰이 든 breaker 파일만 지운다."""
    bpath = Path(root) / BREAKER_NAME
    if (_read(bpath) or {}).get("token") != token:
        return
    try:
        os.unlink(str(bpath))
    except OSError:
        pass


def _break(path: Path, info: Optional[dict[str, Any]], timeout: float = DEFAULT_TIMEOUT) -> bool:
    """낡은 잠금을 깬다 — ``.lock.breaker`` 를 잡은 한 프로세스만. 그 안에서 잠금을 다시 읽어 판정한 그 잠금(토큰,
    inode·mtime)인지 보고 다시 판정한 뒤에만 옆으로 옮겨 지운다(:func:`_remove_judged`). 깼거나 잠금이 이미 없으면 True
    (바로 다시 만들어 본다); 아니면 False — 다른 프로세스가 깨는 중, 잠금이 바뀜, 옮기기 거부: 호출자는 평소처럼
    마감을 확인하고 쉬었다가 다시 본다(바쁘게 돌지 않는다).

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
    """잠금 파일을 O_EXCL 로 만들 때까지 백오프하며 기다린다(낡은 잠금은 깬다). 우리 토큰을 돌려주고, 시간
    초과나 디렉토리 문제면 None(잠금 없이 진행)."""
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
            # Windows: 지워지는 중인 잠금 파일은 PermissionError
            # 로 보인다 — 잡혀 있는 것으로 보고 다시 시도한다
            info = _read(path)
            stale, until = _judge(path, info, timeout)
            # 깨지 못했으면(다른 프로세스가 깨는 중, 옮기기 거부) 바로 다시 돌지 않고 아래의 마감 확인·쉬기로 간다
            if stale and _break(path, info, timeout):
                continue
            left = deadline - time.monotonic()
            if left <= 0 and not extended and (until is None or until < _EDGE):
                # 잡은 채 죽은 파드의 잠금은 기다리기 시작한 바로 그때 생긴 것이라 마감과 거의 같은 순간에 낡는다 —
                # 그 경계에서 잠금 없이 가지 않도록 낡을 때까지만 한 번 더 기다린다(최대 _EDGE 초). until 이 None
                # 이면 잠금이 방금 풀렸다(stat 실패): 마감이라도 O_EXCL 을 한 번 더 해 본다
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
            # 주인을 못 적은 잠금은 놓는다: 비어 있는 잠금은 남이 우리 것인지 알 수 없고 release 도 지우지 않는다
            log.warning("kb lock %s: could not write the owner (%s) — the lock is dropped, going on without it",
                        path, e)
            try:
                os.unlink(str(path))                      # ours: we created it exclusively a moment ago
            except OSError:
                pass
            return None
        return token


def _release(root: Path, token: str) -> None:
    """파일이 우리 토큰으로 읽힐 때만 지운다(우리 잠금이 깨지고 남이 잡았으면, 또는 비었거나 깨져 읽히면 — 막
    만들고 아직 주인을 못 적은 새 잠금일 수 있다 — 건드리지 않음). 우리 잠금이 잠깐 안 읽히는 경우를 위해 몇 번
    다시 읽는다.

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
    """잠금을 쥔 쪽이 긴 구간(재색인·가져오기·내보내기·case.json 확인 대기) 동안 부른다: 파일이 아직 우리 토큰을
    갖고 있으면 mtime 을 새로 찍어(``os.utime``; NFS 에서는 서버 시각) 남이 낡은 잠금으로 깨지 않게 한다. 자주 불러도
    된다 — 실제로는 ``min(_KEEPALIVE_EVERY, timeout / 4)`` 초에 한 번만 건드린다(``force`` 면 바로). 잠금을 쥐고 있지
    않으면 아무것도 안 하고 False.

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
    """잠금 대기·낡음 한계(초). None 은 기본값; 0 이하(설정 실수 — 0 이면 모든 잠금이 바로 낡아 깨졌다)도 기본값에
    WARNING 한 줄(KB 디렉토리마다 한 번)."""
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
    """KB 쓰기 동안 ``<root>/.lock`` 을 잡는다. 잡았으면 True, 못 잡고 그냥 진행하면 False 를 내준다. 같은
    프로세스 안에서는 재진입 가능하다(바깥 holder 만 파일을 만들고 지움). ``timeout`` 이 0 이하이면 기본값.

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
    """이 스레드가 ``root`` 의 잠금을 잡고 있나(테스트·단언용).

    This thread holds the lock of ``root`` (tests / assertions)."""
    st = _state(Path(root))
    return st.depth > 0 and st.token is not None
