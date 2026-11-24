"""조치 폴더의 이름 바꾸기와 삭제를 맡는 모듈. 표준 라이브러리만 쓴다.
Windows에서 다른 프로그램(백신, 색인, 편집기)이 안의 파일을 잠깐 열고 있어 거부되면 정해진 시간 동안 기다렸다가 다시 시도한다.
그 밖의 오류는 바로 던지므로 Linux에서는 ``os``, ``shutil``의 호출과 똑같이 동작한다.

Renames, replaces and removals of the remediation package. On Windows a call refused with `winerror` 5 (access
denied) or 32 (sharing violation) because another program briefly holds a file open is made again: after 0.05 s,
the pause doubling up to 2 s, until the wait limit has passed since the first refusal; then the last exception is
raised, marked so that `waited_out(exc)` is True. The limit is `HOLD_WAIT_S`; inside a `one_wait()` block, once
one call has waited out its limit, later refusals of that block are waited for `SHORT_WAIT_S` at most (a holder
that lasts is not waited for once per file). The memory belongs to the block (a context variable, bound to the
thread that opened it), never to the process: outside a block every call has the full limit, whatever happened
before. `rmtree` also waits for `winerror` 145 (directory not empty). Any other exception is raised at once, so on
POSIX (no `OSError` carries `winerror`) the four functions behave exactly as the calls they stand for. Removals make a read-only entry writable once on `winerror` 5
(Windows does not delete a read-only file). Standard library only: `models.py` imports this module, so nothing from
`sbmig.remediations` may be imported here. Functions are looked up on `os` / `shutil` at call time (tests patch
them there). Contract: `docs/INTERFACES.md` "v2.1z-ce".
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
_DONE_S = 1e-3
_MARK = "sbmig_waited_out"

# one_wait 블록의 상태다. 블록 밖에서는 None이고, 블록을 연 스레드에서만 쓴다
_batch: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("sbmig_fsops_batch", default=None)

# 테스트가 실제로 기다리지 않도록 바꿔 넣을 수 있게 둔다
_sleep: Callable[[float], Any] = time.sleep
_now: Callable[[], float] = time.monotonic

_T = TypeVar("_T")


def _held(exc: BaseException, codes: tuple[int, ...]) -> bool:
    return isinstance(exc, OSError) and getattr(exc, "winerror", None) in codes


def waited_out(exc: BaseException) -> bool:
    """기다릴 수 있는 시간을 다 쓰고 던진 예외면 True를 반환한다.

    True for the exception a call raised after its wait limit had passed (contract rule 1): a caller that repairs
    and tries again raises such an exception at once."""
    return bool(getattr(exc, _MARK, False))


def _own_batch() -> dict[str, Any] | None:
    b = _batch.get()
    return b if b is not None and b["thread"] == threading.get_ident() else None


def _wait_limit() -> float:
    b = _own_batch()
    return SHORT_WAIT_S if b is not None and b["gave_up"] else HOLD_WAIT_S


@contextmanager
def one_wait() -> Iterator[None]:
    """블록 안에서 한 호출이 기다리다 포기하면 그 뒤의 거부는 ``SHORT_WAIT_S``만 기다린다. 블록 밖에는 영향이 없다.

    A batch of calls that waits in full only once: inside the block, after one call has waited out its limit, every
    later refusal is waited for `SHORT_WAIT_S` at most. The state lives in a context variable bound to the thread
    that opened the block; a nested block starts with the full limit, and the outer state comes back on exit (also
    when the block is left by an exception)."""
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


def _waiting(op: str, path: Any, call: Callable[[int], _T], final: Callable[[], bool] | None = None,
             codes: tuple[int, ...] = HOLD_WINERRORS) -> _T:
    """``call``을 실행하고, 다른 프로그램이 파일을 열고 있어 거부되면 정해진 시간 동안 다시 시도한다.
    ``final``이 True를 반환하면 기다려도 풀리지 않는 거부이므로 바로 던진다."""
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
        # 시계 값이 커서 짧은 쉼이 시계에 더해지지 않아도 끝나도록 쉰 시간의 합도 함께 센다
        waited = max(_now() - start, slept)
        left = limit - waited
        if left <= _DONE_S:
            log.warning("remediation: %s of %s still refused after %.1f s (%d tries, WinError %s): a virus scanner, an "
                        "indexer or an editor that has the run folder open keeps a file open there",
                        op, name, waited, tries, last.winerror)  # type: ignore[attr-defined]
            b = _own_batch()
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

    `os.replace(src, dst)`, waited for while Windows refuses it with `winerror` 5 / 32. Not waited for when `dst`
    is a read-only file: Windows refuses to replace that for good, and the caller that expects it
    (`intake._write_back`) makes it writable and tries again."""
    _waiting("replace", src, lambda _n: os.replace(src, dst), lambda: _read_only_file(dst))


def rename(src: Any, dst: Any) -> None:
    """``os.rename``을 실행한다. 다른 프로그램이 파일을 열고 있어 거부되면 기다렸다가 다시 시도한다.

    `os.rename(src, dst)`, waited for while Windows refuses it with `winerror` 5 / 32."""
    _waiting("rename", src, lambda _n: os.rename(src, dst))


def unlink(path: Any) -> None:
    """``os.unlink``를 실행한다. 읽기 전용이라 거부되면 쓸 수 있게 바꾼 뒤 한 번 더 지우고, 열려 있으면 기다린다.

    `os.unlink(path)`; on `winerror` 5 the file is made writable and removed once more before the wait applies."""
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
    # 읽기 전용 항목만 쓸 수 있게 바꾸고 그 항목을 한 번 더 지운다. 다시 실패하면 그 예외를 그대로 던진다
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
    """``shutil.rmtree``를 실행한다. 읽기 전용 항목은 쓸 수 있게 바꿔 지우고, 열려 있는 파일이 있으면 기다렸다가 남은 것을 다시 지운다.

    `shutil.rmtree(path)`; a read-only entry refused with `winerror` 5 is made writable and removed once more, and a
    removal refused with `winerror` 5 / 32 / 145 as a whole is waited for and made again on what is left. A path that no
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
