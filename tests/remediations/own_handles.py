"""A guard that stands for Windows' open-file rule on POSIX (docs/INTERFACES.md "v2.1z-cf", rule 6). Test code only.

Windows refuses to replace, rename or delete a file while a process has it open without delete sharing (Python's
`open()` never shares delete), and to rename or remove a directory while a file inside it is open, or while it is
the working directory of a process. macOS and Linux allow all of it, so a handle that THIS process still holds on
such a path goes unnoticed by every POSIX test. This module makes it visible:

- `open_descriptors()` lists this process's open descriptors with their real paths (Linux: `/proc/self/fd`;
  macOS: `fcntl.F_GETPATH` over the descriptors `/dev/fd` lists). Sockets, pipes and other descriptors without a
  path are left out. Paths are resolved (`/var` vs `/private/var` on macOS), so comparisons are on real paths.
- `HandleGuard` (a context manager; `guard()` builds one) wraps `os.replace`, `os.rename`, `os.unlink` and
  `shutil.rmtree` (the four calls `sbmig.remediations.fsops` makes; it looks them up on `os` / `shutil` at call time,
  so patching those attributes reaches it, and no fsops internals are touched). Before each call it compares the
  open descriptors with the paths the call acts on: the source and the destination of a replace / rename (and
  everything under either when it is a directory), the path of an unlink, everything under the directory of an
  rmtree. The working directory of this process is checked the same way for directory operations. Every call is
  recorded (`calls`), every conflict too (`findings`, with the caller inside `sbmig` and the descriptor's path); with
  `strict=True` a conflict raises `HeldPathError` before the real call is made. The real call is made in any other
  case, so the code under test behaves as without the guard.

What it cannot see: descriptors of child processes (Maven, git, a fix script) and of other processes (the web pod, a
virus scanner, an indexer, an editor); a file that is only memory-mapped after its descriptor was closed (Python's
`mmap` keeps a duplicate descriptor, which IS seen); a holder that opens and closes the file between two calls (only
the moment of the call is looked at); a directory a child process has as its working directory. Calls made by
`shutil.rmtree` itself (its inner `os.unlink(name, dir_fd=…)`) and calls made with `dir_fd` are passed through
unchecked: the check is made once, at the entry of the outer call.

The disk watch of the test run (root `conftest.py`, `scripts/bounded.py` `tree_mb`) walks pytest's basetemp with
`os.scandir` in a thread of this process; the guard reads the descriptors under that walk's `WALK_LOCK`, so the
watch's walk is never reported, while any other walk running at the same time still is.

Safe under pytest-xdist: each worker is its own process and a guard only looks at its own descriptors, and a finding
needs a descriptor on one of the paths the call acts on, so descriptors of pytest, xdist or log files outside those
paths never count.
"""
from __future__ import annotations

import os
import shutil
import sys
import threading
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

_SELF = os.path.abspath(__file__)


def supported() -> bool:
    """True where this process's descriptors can be listed with their paths (Linux, macOS)."""
    if sys.platform.startswith("linux"):
        return os.path.isdir("/proc/self/fd")
    if sys.platform == "darwin":
        try:
            import fcntl
        except ImportError:
            return False
        return hasattr(fcntl, "F_GETPATH")
    return False


def _real(path: str) -> str:
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return os.path.abspath(path)


def _fd_path_linux(fd: int) -> str | None:
    try:
        target = os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        return None
    if not target.startswith("/"):
        return None                     # socket:[…], pipe:[…], anon_inode:…
    target = target.removesuffix(" (deleted)")
    return target


def _fd_path_macos(fd: int) -> str | None:
    import fcntl
    try:
        raw = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024))
    except (OSError, ValueError):
        return None                     # a socket, a pipe, a closed descriptor
    path = raw.split(b"\0", 1)[0]
    return os.fsdecode(path) if path.startswith(b"/") else None


def open_descriptors() -> dict[int, str]:
    """Open descriptors of this process that have a path: `{fd: real path}`. Raises `RuntimeError` off Linux / macOS."""
    if sys.platform.startswith("linux"):
        listing, reader = "/proc/self/fd", _fd_path_linux
    elif sys.platform == "darwin":
        listing, reader = "/dev/fd", _fd_path_macos
    else:
        raise RuntimeError(f"open descriptors cannot be listed on {sys.platform}")
    out: dict[int, str] = {}
    for name in os.listdir(listing):    # the listing's own descriptor is closed again before the loop runs
        try:
            fd = int(name)
        except ValueError:
            continue
        p = reader(fd)
        if p is not None:
            out[fd] = _real(p)
    return out


def _item(path: Any) -> str:
    """The real path of the entry `path` names, without following `path` itself when it is a symlink."""
    s = os.fsdecode(os.fspath(path))
    s = os.path.abspath(s)
    head, tail = os.path.split(s.rstrip(os.sep) or s)
    return os.path.join(_real(head), tail) if tail else _real(s)


def _under(p: str, top: str) -> bool:
    return p == top or p.startswith(top.rstrip(os.sep) + os.sep)


def _caller() -> tuple[str, str]:
    """(innermost `sbmig` frame that is not fsops: `file:line function`, the full stack in short form)."""
    stack = [f for f in traceback.extract_stack() if os.path.abspath(f.filename) != _SELF]
    where = ""
    for f in reversed(stack):
        fn = f.filename.replace("\\", "/")
        if "/sbmig/" in fn and not fn.endswith("/sbmig/remediations/fsops.py"):
            where = f"sbmig/{fn.rsplit('/sbmig/', 1)[1]}:{f.lineno} {f.name}"
            break
    short = " <- ".join(f"{os.path.basename(f.filename)}:{f.lineno} {f.name}" for f in reversed(stack[-12:]))
    return where, short


@contextmanager
def _walk_lock() -> Iterator[None]:
    """Hold the test run's disk-watch lock while the open descriptors are read, so that the watch's own walk of the
    basetemp (`scripts/bounded.py` `tree_mb`, every >= 10 s in a thread of this process) has no `scandir` open at that
    moment. Without the walls (`_repo_bounded` not registered, or a module without `WALK_LOCK`) nothing is taken.

    Kept narrow on purpose: only around `open_descriptors()`, never while the guarded real call runs and never across
    a `yield` of test code. A guard that held the lock for long would stall the watch and so switch the growth wall
    off. A walk by any other code (ours, running at the same time) does not take this lock and is still seen."""
    mod = sys.modules.get("_repo_bounded")
    lock = getattr(mod, "WALK_LOCK", None) if mod is not None else None
    if lock is None:
        yield
        return
    with lock:
        yield


@dataclass
class Call:
    """One guarded call: the operation, its path arguments (real paths), the sbmig caller, whether fsops made it."""
    op: str
    paths: tuple[str, ...]
    caller: str
    via_fsops: bool


@dataclass
class Finding:
    """A descriptor of this process open on a path the call acts on (or the working directory inside it)."""
    op: str
    target: str            # the path argument the conflict is about (real path)
    held: str              # the descriptor's real path (or the working directory)
    fd: int                # -1 for the working directory
    caller: str
    stack: str

    def __str__(self) -> str:
        what = "the working directory" if self.fd < 0 else f"fd {self.fd}"
        return f"{self.op}({self.target}) while {what} is open on {self.held} — called from {self.caller or '?'}"


class HeldPathError(AssertionError):
    """Raised by a strict guard before a call Windows would refuse because this process holds the path."""

    def __init__(self, finding: Finding):
        self.finding = finding
        super().__init__(str(finding))


@dataclass
class HandleGuard:
    """Wraps `os.replace` / `os.rename` / `os.unlink` / `shutil.rmtree` while the block runs (see the module text).

    `strict=True` raises `HeldPathError` at the first conflict, before the real call. `only_under` (a directory)
    limits the CHECK to calls whose paths are under it (calls elsewhere are still recorded); None checks every call.
    `check_cwd=False` leaves the working directory out."""
    strict: bool = False
    only_under: str | None = None
    check_cwd: bool = True
    calls: list[Call] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    _saved: dict[str, Any] = field(default_factory=dict)
    _local: threading.local = field(default_factory=threading.local)

    # ---- the check -------------------------------------------------------------------------------------------
    def conflicts(self, op: str, targets: list[tuple[str, bool]]) -> list[Finding]:
        """Findings for `targets` = [(real path, whole subtree?)] against the open descriptors now."""
        with _walk_lock():
            fds = open_descriptors()
        cwd = _real(os.getcwd()) if self.check_cwd else None
        where, stack = _caller()
        out: list[Finding] = []
        for target, subtree in targets:
            for fd, held in sorted(fds.items()):
                if held == target or (subtree and _under(held, target)):
                    out.append(Finding(op, target, held, fd, where, stack))
            if subtree and cwd is not None and _under(cwd, target):
                out.append(Finding(op, target, cwd, -1, where, stack))
        return out

    def _targets(self, op: str, args: tuple[Any, ...]) -> list[tuple[str, bool]]:
        out: list[tuple[str, bool]] = []
        for a in args:
            p = _item(a)
            is_dir = os.path.isdir(p) and not os.path.islink(p)
            out.append((p, op == "rmtree" or is_dir))
        return out

    def _wrap(self, op: str, real: Callable[..., Any], npaths: int) -> Callable[..., Any]:
        guard = self

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            # nested calls (shutil.rmtree's own unlinks) and dir_fd forms pass through: checked once at the entry
            if getattr(guard._local, "inside", False) or kwargs.get("dir_fd") is not None \
                    or kwargs.get("src_dir_fd") is not None or kwargs.get("dst_dir_fd") is not None:
                return real(*args, **kwargs)
            guard._local.inside = True
            try:
                paths = args[:npaths]
                targets = guard._targets(op, paths)
                where, _stack = _caller()
                via = any(f.filename.replace("\\", "/").endswith("/sbmig/remediations/fsops.py")
                          for f in traceback.extract_stack())
                guard.calls.append(Call(op, tuple(t for t, _ in targets), where, via))
                if guard.only_under is None or any(_under(t, guard.only_under) for t, _ in targets):
                    found = guard.conflicts(op, targets)
                    guard.findings.extend(found)
                    if found and guard.strict:
                        raise HeldPathError(found[0])
                return real(*args, **kwargs)
            finally:
                guard._local.inside = False
        wrapper.__name__ = getattr(real, "__name__", op)      # fsops._is_removal looks at the name of rmtree's func
        wrapper.__wrapped__ = real                            # type: ignore[attr-defined]
        return wrapper

    # ---- install / remove ------------------------------------------------------------------------------------
    def __enter__(self) -> HandleGuard:  # noqa: PYI034
        if not supported():
            raise RuntimeError(f"the own-handle guard does not run on {sys.platform}")
        self._saved = {"replace": os.replace, "rename": os.rename, "unlink": os.unlink, "rmtree": shutil.rmtree}
        os.replace = self._wrap("replace", self._saved["replace"], 2)           # type: ignore[assignment]
        os.rename = self._wrap("rename", self._saved["rename"], 2)              # type: ignore[assignment]
        os.unlink = self._wrap("unlink", self._saved["unlink"], 1)              # type: ignore[assignment]
        shutil.rmtree = self._wrap("rmtree", self._saved["rmtree"], 1)          # type: ignore[assignment]
        return self

    def __exit__(self, *exc: object) -> None:
        os.replace = self._saved["replace"]                                     # type: ignore[assignment]
        os.rename = self._saved["rename"]                                       # type: ignore[assignment]
        os.unlink = self._saved["unlink"]                                       # type: ignore[assignment]
        shutil.rmtree = self._saved["rmtree"]                                   # type: ignore[assignment]

    # ---- for the tests ---------------------------------------------------------------------------------------
    def calls_to(self, op: str, *, name: str | None = None, under: str | None = None) -> list[Call]:
        """Recorded calls of `op` whose LAST path ends with `name` and / or lies under `under`."""
        out = []
        for c in self.calls:
            if c.op != op:
                continue
            if name is not None and os.path.basename(c.paths[-1]) != name:
                continue
            if under is not None and not any(_under(p, _real(str(under))) for p in c.paths):
                continue
            out.append(c)
        return out

    def report(self) -> str:
        return "\n".join(str(f) for f in self.findings) or "(no descriptor held)"


@contextmanager
def guard(*, strict: bool = False, only_under: Any = None, check_cwd: bool = True) -> Iterator[HandleGuard]:
    """`with guard(only_under=tmp_path) as g: …; assert not g.findings, g.report()`."""
    g = HandleGuard(strict=strict, only_under=_real(str(only_under)) if only_under is not None else None,
                    check_cwd=check_cwd)
    with g:
        yield g


def held_on(path: Any) -> list[tuple[int, str]]:
    """Descriptors of this process open on `path` or under it when it is a directory: [(fd, real path)]."""
    p = _item(path)
    sub = os.path.isdir(p)
    return [(fd, h) for fd, h in sorted(open_descriptors().items()) if h == p or (sub and _under(h, p))]
