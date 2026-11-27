"""취약점 조치에서 파일과 폴더를 옮기고 지우는 함수를 모은 모듈.
Windows에서 백신이나 편집기가 파일을 잠깐 열고 있어 거부되면 60초까지 다시 시도한다.

Renames, replaces and removals of the remediation package. On Windows a call refused with `winerror` 5 (access
denied) or 32 (sharing violation) because another program briefly holds a file open is made again: after 0.05 s,
the pause doubling up to 2 s, until the wait limit has passed since the first refusal; then the last exception is
raised, marked so that `waited_out(exc)` is True. The limit is `HOLD_WAIT_S`; inside a `one_wait()` block, once
one call has waited out its limit, later refusals of that block are waited for `SHORT_WAIT_S` at most (a holder
that lasts is not waited for once per file). The memory belongs to the block (a context variable, bound to the
thread that opened it), never to the process: outside a block every call has the full limit, whatever happened
before. `rmtree` also waits for `winerror` 145 (directory not empty). Any other exception is raised at once, so on
POSIX (no `OSError` carries `winerror`) the four functions behave exactly as the calls they stand for. Removals
make a read-only entry writable once on `winerror` 5 (Windows does not delete a read-only file). Standard library
only: `models.py` imports this module, so nothing from `sbmig.remediations` may be imported here. Functions are
looked up on `os` / `shutil` at call time (tests patch them there). Contract: `docs/INTERFACES.md` "v2.1z-ce".

Who holds the file (contract "v2.1z-cf"): `holders(paths)` names the programs that have one of the files open, through
the Windows Restart Manager (`rstrtmgr.dll` via `ctypes`, loaded only on Windows; off Windows it finds nothing). A
refused call looks nothing up while it retries (listing a directory would hold a handle under the directory being
renamed); when its time is up it runs one lookup in a daemon thread and waits `HOLDERS_WAIT_S` at most. None is
started inside a `one_wait()` block after a give-up, nor while an earlier lookup of the process has not returned
(`_pending` holds its start; the failed result says how long ago); a thread that cannot be started is a failed lookup.
The names go into the "still refused" line (base names only, at most eight); a failed lookup ends the WARNING with its
reason, never with "no program found". The helper thread never logs: the calling thread writes every line.
The lookup is read as `_lookup` at call time, so tests replace it; the Windows part takes its libraries as arguments
(`_rm_holders`), so a fake library drives it.
"""
from __future__ import annotations

import contextvars
import logging
import os
import shutil
import stat
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

log = logging.getLogger(__name__)

HOLD_WAIT_S = 60.0
FIRST_PAUSE_S = 0.05
MAX_PAUSE_S = 2.0
HOLD_WINERRORS = (5, 32)
RMTREE_WINERRORS = (5, 32, 145)
SHORT_WAIT_S = 2.0
HOLDERS_MAX_FILES = 500
HOLDERS_WAIT_S = 5.0
HOLDERS_SHOWN = 8
_DONE_S = 1e-3
_MARK = "sbmig_waited_out"

# one_wait 블록의 상태. 블록 밖에서는 None이고 블록을 연 스레드에서만 쓴다
_batch: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("sbmig_fsops_batch", default=None)

# 테스트가 실제로 기다리지 않도록 바꿔 넣을 수 있게 둔다
_sleep: Callable[[float], Any] = time.sleep
_now: Callable[[], float] = time.monotonic

_T = TypeVar("_T")


def _held(exc: BaseException, codes: tuple[int, ...]) -> bool:
    return isinstance(exc, OSError) and getattr(exc, "winerror", None) in codes


def waited_out(exc: BaseException) -> bool:
    """기다릴 수 있는 시간을 다 쓰고 던진 예외면 True를 반환한다.
    이런 예외를 받은 호출자는 다시 시도하지 말고 바로 던져야 한다.

    Contract rule 1 of "v2.1z-ce": a caller that repairs and tries again raises such an exception at once."""
    return bool(getattr(exc, _MARK, False))


def _own_batch() -> dict[str, Any] | None:
    b = _batch.get()
    return b if b is not None and b["thread"] == threading.get_ident() else None


def _wait_limit() -> float:
    b = _own_batch()
    return SHORT_WAIT_S if b is not None and b["gave_up"] else HOLD_WAIT_S


@contextmanager
def one_wait() -> Iterator[None]:
    """여러 호출을 묶어 끝까지 기다리는 일이 한 번만 생기게 한다.
    블록 안에서 한 호출이 기다리다 포기하면 그 뒤의 거부는 ``SHORT_WAIT_S``까지만 기다린다.
    블록을 연 스레드에만 적용되고 블록 밖에는 영향이 없다.

    The state lives in a context variable bound to the thread that opened the block; a nested block starts with the
    full limit, and the outer state comes back on exit (also when the block is left by an exception)."""
    token = _batch.set({"gave_up": False, "thread": threading.get_ident()})
    try:
        yield
    finally:
        _batch.reset(token)


def _name(path: Any) -> str:
    s = os.fspath(path)
    if isinstance(s, bytes):
        s = os.fsdecode(s)
    s = s.rstrip("/\\")
    return s.replace("\\", "/").rsplit("/", 1)[-1] or s


def _read_only_file(path: Any) -> bool:
    try:
        st = os.stat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and not st.st_mode & stat.S_IWRITE


# ---------------------------------------------------------------------------- 파일을 열고 있는 프로그램 찾기 (Windows)
_WINDOWS = sys.platform == "win32"
_ERROR_MORE_DATA = 234
_RM_LIST_TRIES = 4
_RM_FIRST_SIZE = 16
_CCH_RM_SESSION_KEY = 32
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_IMAGE_NAME_CHARS = 1024
_RM_TYPES: Any = None
_RM_TYPES_LOCK = threading.Lock()
# 찾기는 프로세스당 하나만 실행한다. 그래서 끝나지 않은 찾기의 시작 시각을 둔다
_PENDING_LOCK = threading.Lock()
_pending: dict[str, float] | None = None


def _base(s: str) -> str:
    s = s.rstrip("/\\")
    return s.replace("\\", "/").rsplit("/", 1)[-1] or s


def _files(paths: Any) -> list[str]:
    """Restart Manager에 등록할 일반 파일 목록을 만든다. 디렉토리는 그 아래 파일로 펼친다.

    `os.walk` order, missing paths left out, at most `HOLDERS_MAX_FILES` over all paths."""
    out: list[str] = []
    seen: set[str] = set()
    cap = HOLDERS_MAX_FILES

    def add(p: str) -> bool:
        try:
            if not stat.S_ISREG(os.lstat(p).st_mode):
                return False
        except OSError:
            return False
        key = os.path.normcase(p)
        if key not in seen:
            seen.add(key)
            out.append(p)
        return len(out) >= cap

    for raw in paths:
        p = os.path.abspath(os.fsdecode(os.fspath(raw)))
        if os.path.isdir(p) and not os.path.islink(p):
            for root, _dirs, names in os.walk(p):
                for n in names:
                    if add(os.path.join(root, n)):
                        return out
        elif add(p):
            return out
    return out


def _rm_types() -> Any:
    """Restart Manager 호출에 쓰는 ``ctypes`` 타입을 처음 쓸 때 만든다. import할 때는 아무것도 읽지 않는다.

    Windows sizes: DWORD / UINT / ULONG 32 bits, BOOL an int, FILETIME two DWORDs (alignment 4), so that
    `RM_UNIQUE_PROCESS` is 12 bytes and `RM_PROCESS_INFO` 668."""
    global _RM_TYPES
    with _RM_TYPES_LOCK:
        if _RM_TYPES is None:
            _RM_TYPES = _build_rm_types()
        return _RM_TYPES


def _build_rm_types() -> Any:
    import ctypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]

    class RM_UNIQUE_PROCESS(ctypes.Structure):
        _fields_ = [("dwProcessId", ctypes.c_uint32), ("ProcessStartTime", FILETIME)]

    class RM_PROCESS_INFO(ctypes.Structure):
        _fields_ = [("Process", RM_UNIQUE_PROCESS),
                    ("strAppName", ctypes.c_wchar * 256),            # CCH_RM_MAX_APP_NAME + 1
                    ("strServiceShortName", ctypes.c_wchar * 64),    # CCH_RM_MAX_SVC_NAME + 1
                    ("ApplicationType", ctypes.c_int),               # RM_APP_TYPE
                    ("AppStatus", ctypes.c_uint32),
                    ("TSSessionId", ctypes.c_uint32),
                    ("bRestartable", ctypes.c_int)]

    class _T:
        pass
    t = _T()
    t.ctypes = ctypes
    t.FILETIME, t.RM_UNIQUE_PROCESS, t.RM_PROCESS_INFO = FILETIME, RM_UNIQUE_PROCESS, RM_PROCESS_INFO
    t.DWORD = t.UINT = ctypes.c_uint32
    t.BOOL = ctypes.c_int
    t.WCHAR = ctypes.c_wchar
    t.HANDLE = ctypes.c_void_p
    return t


def _declare(rm: Any, k32: Any) -> None:
    """``_rm_holders``가 호출하는 함수마다 ``argtypes``와 ``restype``을 선언한다."""
    t = _rm_types()
    P = t.ctypes.POINTER
    rm.RmStartSession.argtypes = [P(t.DWORD), t.DWORD, P(t.WCHAR)]
    rm.RmStartSession.restype = t.DWORD
    rm.RmRegisterResources.argtypes = [t.DWORD, t.UINT, P(t.ctypes.c_wchar_p), t.UINT, P(t.RM_UNIQUE_PROCESS),
                                       t.UINT, P(t.ctypes.c_wchar_p)]
    rm.RmRegisterResources.restype = t.DWORD
    rm.RmGetList.argtypes = [t.DWORD, P(t.UINT), P(t.UINT), P(t.RM_PROCESS_INFO), P(t.DWORD)]
    rm.RmGetList.restype = t.DWORD
    rm.RmEndSession.argtypes = [t.DWORD]
    rm.RmEndSession.restype = t.DWORD
    k32.OpenProcess.argtypes = [t.DWORD, t.BOOL, t.DWORD]
    k32.OpenProcess.restype = t.HANDLE
    k32.QueryFullProcessImageNameW.argtypes = [t.HANDLE, t.DWORD, P(t.WCHAR), P(t.DWORD)]
    k32.QueryFullProcessImageNameW.restype = t.BOOL
    k32.CloseHandle.argtypes = [t.HANDLE]
    k32.CloseHandle.restype = t.BOOL


def _load_windows() -> tuple[Any, Any]:
    """Windows에서 ``rstrtmgr.dll``과 ``kernel32.dll``을 읽고 함수 시그니처를 선언한다."""
    import ctypes
    rm = ctypes.WinDLL("rstrtmgr", use_last_error=True)      # type: ignore[attr-defined]
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)     # type: ignore[attr-defined]
    _declare(rm, k32)
    return rm, k32


def _exe_name(k32: Any, pid: int) -> str:
    t = _rm_types()
    h = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = t.ctypes.create_unicode_buffer(_IMAGE_NAME_CHARS)
        size = t.DWORD(_IMAGE_NAME_CHARS)
        if not k32.QueryFullProcessImageNameW(h, 0, buf, t.ctypes.pointer(size)):
            return ""
        return _base(buf.value)
    finally:
        k32.CloseHandle(h)


def _entry(app: str, exe: str, pid: int, own: int) -> str:
    mark = ", this process" if pid == own else ""
    app, exe = app.strip(), exe.strip()
    if "\\" in app or app[1:2] == ":" and app[:1].isalpha() or app.startswith("/") or "/" in app and " " not in app:
        app = _base(app)
    if app and exe and app.lower() != exe.lower():
        return f"{app} [{exe}] (pid {pid}{mark})"
    return f"{app or exe or 'unnamed program'} (pid {pid}{mark})"


def _rm_holders(files: list[str], rm: Any, k32: Any) -> tuple[list[str], str | None]:
    """Restart Manager로 ``files``를 열고 있는 프로그램 이름을 찾는다.
    실패하면 빈 목록과 사유를 반환한다. 예외를 던지지 않고 로그도 남기지 않는다.

    Returns (names, None) or ([], reason). `rm` / `k32` are the two libraries (a fake in the tests)."""
    try:
        t = _rm_types()
        ct = t.ctypes
        handle = t.DWORD(0)
        key = ct.create_unicode_buffer(_CCH_RM_SESSION_KEY + 1)
        rc = rm.RmStartSession(ct.pointer(handle), 0, key)
        if rc != 0:
            return [], f"RmStartSession answered {rc}"
        try:
            names = (ct.c_wchar_p * len(files))(*files)
            rc = rm.RmRegisterResources(handle.value, len(files), names, 0, None, 0, None)
            if rc != 0:
                return [], f"RmRegisterResources answered {rc}"
            needed, count, reasons = t.UINT(0), t.UINT(0), t.DWORD(0)
            infos = (t.RM_PROCESS_INFO * _RM_FIRST_SIZE)()
            for _ in range(_RM_LIST_TRIES):
                count.value = len(infos)
                rc = rm.RmGetList(handle.value, ct.pointer(needed), ct.pointer(count), infos, ct.pointer(reasons))
                if rc == 0:
                    break
                if rc != _ERROR_MORE_DATA:
                    return [], f"RmGetList answered {rc}"
                # 두 호출 사이에 프로그램이 늘 수 있다. 조금 넉넉히 잡는다
                infos = (t.RM_PROCESS_INFO * (max(needed.value, len(infos)) + 4))()
            else:
                return [], f"RmGetList answered {_ERROR_MORE_DATA} {_RM_LIST_TRIES} times"
            own = os.getpid()
            out: list[str] = []
            for info in infos[:min(count.value, len(infos))]:
                pid = int(info.Process.dwProcessId)
                try:
                    exe = _exe_name(k32, pid)
                except Exception:  # noqa: BLE001
                    exe = ""
                s = _entry(info.strAppName, exe, pid, own)
                if s not in out:
                    out.append(s)
            return out, None
        finally:
            rm.RmEndSession(handle.value)
    # 프로그램 이름은 로그에만 쓴다. 찾다가 실패해도 예외로 던지지 않는다
    except Exception as e:  # noqa: BLE001
        return [], f"{type(e).__name__} {getattr(e, 'winerror', '') or ''}".strip()


def _restart_manager(files: list[str]) -> tuple[list[str], str | None]:
    """기본 ``_lookup``이다. Windows에서만 Restart Manager로 찾고, 다른 OS에서는 빈 결과를 반환한다."""
    if not _WINDOWS:
        return [], None
    try:
        rm, k32 = _load_windows()
    except Exception as e:  # noqa: BLE001
        return [], f"Restart Manager not loaded ({type(e).__name__})"
    return _rm_holders(files, rm, k32)


# 테스트가 바꿔 넣을 수 있도록 호출할 때마다 이 이름으로 찾는다
_lookup: Callable[[list[str]], tuple[list[str], str | None]] = _restart_manager


def _find(paths: Any) -> tuple[list[str], str | None, int]:
    """``paths``를 열고 있는 프로그램을 찾아 이름, 실패 사유, 살펴본 파일 수를 반환한다.
    예외를 던지지 않고 로그도 남기지 않는다.

    The reason is for a DEBUG line; None when the lookup worked."""
    try:
        files = _files(paths)
        if not files:
            return [], None, 0
        names, reason = _lookup(files)
        return list(names), reason, len(files)
    except Exception as e:  # noqa: BLE001
        return [], type(e).__name__, 0


def _claim() -> tuple[dict[str, float] | None, str | None]:
    """프로세스에 하나뿐인 찾기 차례를 잡는다. 앞선 찾기가 아직 끝나지 않았으면 None과 사유를 반환한다."""
    global _pending
    with _PENDING_LOCK:
        if _pending is not None:
            return None, f"a lookup started {int(_now() - _pending['started'])} s ago has not returned"
        _pending = {"started": _now()}
        return _pending, None


def _release(record: dict[str, float]) -> None:
    global _pending
    with _PENDING_LOCK:
        if _pending is record:
            _pending = None


def holders(paths: Any) -> list[str]:
    """``paths``를 열고 있는 프로그램 이름 목록을 반환한다.
    Windows가 아니거나 찾지 못하면 빈 목록을 반환한다. 예외를 던지지 않는다.

    One string per program: `<application name> [<executable>] (pid <n>)`, with `, this process` in the pid part
    for `os.getpid()`; base names only, never a path. A single `str` / `bytes` / `os.PathLike` is a list of one.
    A directory stands for the regular files under it (at most `HOLDERS_MAX_FILES` over all paths), a missing path
    is left out. Windows only (Restart Manager); elsewhere `[]`. Runs in the calling
    thread and counts as the process's one lookup. Never raises: a failure is `[]` and one DEBUG line."""
    names: list[str] = []
    reason: str | None = None
    shown = "?"
    try:
        if isinstance(paths, (str, bytes, os.PathLike)):
            paths = [paths]
        paths = list(paths)
        shown = ", ".join(_name(p) for p in paths)
        record, reason = _claim()
        if record is not None:
            try:
                names, reason, _n = _find(paths)
            finally:
                _release(record)
    except Exception as e:  # noqa: BLE001
        names, reason = [], type(e).__name__
    if reason:
        log.debug("remediation: the lookup of the programs holding %s failed: %s", shown, reason)
    return names


def _look(paths: tuple[Any, ...], wait: float) -> tuple[list[str], str | None, int] | None:
    """``_find``를 데몬 스레드에서 실행하고 ``wait``초까지 기다린다. 그때까지 끝나지 않으면 None을 반환한다.

    The helper thread clears the process's record when it ends; the starter clears it only when the thread never
    started."""
    record, reason = _claim()
    if record is None:
        return [], reason, 0
    box: dict[str, tuple[list[str], str | None, int]] = {}
    done = threading.Event()

    def run() -> None:
        try:
            box["r"] = _find(paths)
        except BaseException as e:  # noqa: BLE001
            box["r"] = ([], f"lookup failed ({type(e).__name__})", 0)
        finally:
            _release(record)
            done.set()

    th = threading.Thread(target=run, name="sbmig-fsops-holders", daemon=True)
    try:
        th.start()
    except Exception as e:  # noqa: BLE001
        return [], f"lookup not started ({type(e).__name__})", 0
    finally:
        if th.ident is None:
            _release(record)
    return box["r"] if done.wait(wait) else None


def _names_text(names: list[str]) -> str:
    shown = ", ".join(names[:HOLDERS_SHOWN])
    more = len(names) - HOLDERS_SHOWN
    return f"{shown} and {more} more" if more > 0 else shown


def _waiting(op: str, path: Any, call: Callable[[int], _T], final: Callable[[], bool] | None = None,
             codes: tuple[int, ...] = HOLD_WINERRORS, look: tuple[Any, ...] | None = None) -> _T:
    """``call``을 실행한다. 다른 프로그램이 파일을 열고 있어 거부되면 정해진 시간 동안 다시 시도한다.
    ``final``이 True를 반환하면 기다려도 풀리지 않는 거부다. 바로 던진다.

    ``call`` gets the try number (1 first). ``look``: the paths whose holders are looked up on give-up (default
    ``path``). On give-up the last exception is marked for `waited_out` and the block's ``gave_up`` is set."""
    try:
        return call(1)
    except OSError as e:
        if not _held(e, codes) or (final is not None and final()):
            raise
        last: OSError = e
    name = _name(path)
    start = _now()
    limit = _wait_limit()
    tries = 1
    pause = FIRST_PAUSE_S
    slept = 0.0
    log.info("remediation: %s of %s refused (WinError %s): another program has a file open there; waiting up to %g s",
             op, name, last.winerror, limit)  # type: ignore[attr-defined]
    while True:
        # 시계 값이 커서 짧게 쉰 시간이 시계에 반영되지 않아도 끝나도록 쉰 시간의 합도 함께 센다
        waited = max(_now() - start, slept)
        left = limit - waited
        if left <= _DONE_S:
            b = _own_batch()
            # 블록 안에서 이미 한 번 포기했으면 잡고 있는 프로그램도 그때 기록했다. 다시 찾지 않는다
            skip = b is not None and b["gave_up"]
            got = None if skip else _look((path,) if look is None else look, HOLDERS_WAIT_S)
            if got is not None and got[0]:
                log.warning("remediation: %s of %s still refused after %.1f s (%d tries, WinError %s): open in %s",
                            op, name, waited, tries, last.winerror, _names_text(got[0]))  # type: ignore[attr-defined]
            else:
                if skip:
                    tail = ""
                elif got is None:
                    tail = f" (the lookup of the holding program did not return in {HOLDERS_WAIT_S:g} s)"
                elif got[1]:
                    log.debug("remediation: %s of %s: the lookup of the holding program failed: %s", op, name, got[1])
                    tail = f" (the lookup of the holding program failed: {got[1]})"
                else:
                    tail = f" (no program found with the {got[2]} file(s) looked at open)"
                log.warning("remediation: %s of %s still refused after %.1f s (%d tries, WinError %s): a virus scanner, "
                            "an indexer or an editor that has the run folder open keeps a file open there%s",
                            op, name, waited, tries, last.winerror, tail)  # type: ignore[attr-defined]
            if b is not None:
                b["gave_up"] = True
            try:
                setattr(last, _MARK, True)
            except AttributeError:
                pass
            raise last
        _sleep(min(pause, left))
        slept += min(pause, left)
        pause = min(pause * 2, MAX_PAUSE_S)
        tries += 1
        try:
            result = call(tries)
        except OSError as e:
            if not _held(e, codes):
                raise
            last = e
            continue
        log.info("remediation: %s of %s went through after %.1f s (%d tries)", op, name, _now() - start, tries)
        return result


def replace(src: Any, dst: Any) -> None:
    """``os.replace``를 실행한다. 다른 프로그램이 파일을 열고 있어 거부되면 기다렸다가 다시 시도한다.
    ``dst``가 읽기 전용 파일이면 기다리지 않고 바로 던진다.

    Waits on `winerror` 5 / 32. A read-only `dst` is not waited for: Windows refuses to replace that for good, and
    the caller that expects it (`intake._write_back`) makes it writable and tries again."""
    _waiting("replace", src, lambda _n: os.replace(src, dst), lambda: _read_only_file(dst), look=(src, dst))


def rename(src: Any, dst: Any) -> None:
    """``os.rename``을 실행한다. 다른 프로그램이 파일을 열고 있어 거부되면 기다렸다가 다시 시도한다."""
    _waiting("rename", src, lambda _n: os.rename(src, dst), look=(src, dst))


def unlink(path: Any) -> None:
    """``os.unlink``를 실행한다.
    읽기 전용 파일이면 쓰기 권한을 준 뒤 한 번 더 지운다. 다른 프로그램이 열고 있으면 기다렸다가 다시 시도한다.

    On `winerror` 5 the chmod and second unlink come first; the wait applies only when that fails too."""
    def call(_n: int) -> None:
        try:
            os.unlink(path)
        except OSError as e:
            if getattr(e, "winerror", None) != 5:
                raise
            os.chmod(path, stat.S_IWRITE)
            os.unlink(path)
    _waiting("unlink", path, call)


def _is_removal(func: Any) -> bool:
    return func in (os.unlink, os.remove, os.rmdir) or getattr(func, "__name__", "") in ("unlink", "remove", "rmdir")


def _writable_again(func: Any, p: Any, exc: BaseException) -> None:
    # 읽기 전용이라 거부된 삭제만 쓰기 권한을 주고 한 번 더 지운다. 다시 실패하면 그 예외를 그대로 던진다
    if not (_is_removal(func) and getattr(exc, "winerror", None) == 5):
        raise exc
    os.chmod(p, stat.S_IWRITE)
    func(p)


def _rmtree_once(path: Any) -> None:
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_writable_again)
    else:
        shutil.rmtree(path, onerror=lambda func, p, info: _writable_again(func, p, info[1]))


def rmtree(path: Any) -> None:
    """``shutil.rmtree``를 실행한다.
    읽기 전용 항목은 쓰기 권한을 준 뒤 지운다. 열려 있는 파일이 있으면 기다렸다가 남은 것을 다시 지운다.

    A read-only entry refused with `winerror` 5 is made writable and removed once more; a removal refused with
    `winerror` 5 / 32 / 145 as a whole is waited for and made again on what is left. A path that no
    longer exists on a later try is success; on the first try the call raises as `shutil.rmtree` does."""
    def call(n: int) -> None:
        if n > 1 and not os.path.lexists(path):
            return
        try:
            _rmtree_once(path)
        except FileNotFoundError:
            if n > 1 and not os.path.lexists(path):
                return
            raise
    _waiting("rmtree", path, call, codes=RMTREE_WINERRORS)
