"""이관이 BUILD SUCCESS로 끝난 뒤 같은 빌드 파드에서 취약점 조치를 이어서 실행하는 모듈.
이관 그래프의 ``remediation`` 노드가 ``run_after_migration``을 호출하고, 이관 없이 조치만 하는 실행(``--flow
remediation``)은 ``run_only``를 호출한다.
- 조치 결과는 이관의 성공 여부, 종료 코드, 보고서 결과를 바꾸지 않으며 예외도 밖으로 던지지 않는다
- 실행 폴더는 ``<이관 실행 폴더>/remediation/``이고 작업 id는 이관 실행 id와 같다
- 이관이 끝난 프로젝트 트리를 ZIP으로 묶어 ``intake.accept_zip``에 넣는다.
  ``remediation.trusted_projects``는 확인하지 않는다
- Maven 로컬 저장소는 이관과 같은 것을 쓴다
- 빌드 파드는 사람을 기다리지 않는다. AWAITING_CONFIRMATION 상태를 저장하고 끝나며,
  재개할 때는 새 빌드 파드가 ``resume``을 호출한다
- 이전 버전이 AWAITING_APPROVAL로 저장한 조치는 재개하지 않고 ``StateConflict``를 던진다
- ``remediation.chain_budget_minutes``를 넘기면 STOPPED(사유 ``chain_budget``)로 멈추고 나중에 재개할 수 있다
- ``run_only``는 준비가 안 됐으면 상태를 만들지 않고 ``RemediationNotReady``를 던진다
- 상태를 진행시키는 동안 ``claim.json``으로 조치 폴더를 점유하며, 다른 프로세스가 점유 중이면 아무것도 바꾸지 않는다

Chained remediation: runs in the build pod after a migration that ended in BUILD SUCCESS. The migration graph's
`remediation` node calls `run_after_migration`; the remediation graph stays a separate feature. Its outcome never changes
the migration's verdict and no exception leaves this module's entry points. `run_only` is the entry point of the
`remediation` run flow (no migration): same files, state id and driver; the only exception it raises is
`RemediationNotReady` (no state was created). Whatever drives the state (the chained first run, `run_only`, `resume`,
the in-process service's worker) holds the run claim `claim.json` (`RunClaim`) for the time it drives; a second process
that finds a fresh claim refuses without touching anything.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import signal
import socket
import threading
import time
import traceback
import uuid
import zipfile
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import graph as graph_mod
from .interfaces import Components, Journal, StopCheck
from .models import (
    FAILED,
    R_INTERNAL_ERROR,
    RemediationState,
    RunPaths,
    StateConflict,
    read_json,
    write_json_atomic,
)

log = logging.getLogger("sbmig.remediations.chain")

CHAIN_DIR = "remediation"
# 시간 한도를 넘겨 STOPPED가 될 때의 사유 코드.
# 이관에 이어 실행하는 조치에만 쓰이므로 models가 아니라 여기에 둔다
R_CHAIN_BUDGET = "chain_budget"
_monotonic = time.monotonic               # 테스트에서 교체하는 시계
SCOPE = "remediation"                     # 이관 저널에서 취약점 조치가 남긴 줄을 구분하는 값
SOURCE_NAME = "source.zip"
ZIP_DATE = (1980, 1, 1, 0, 0, 0)          # ZIP 내용이 매번 같도록 시각을 고정한다
# 프로젝트 폴더 안의 sbmig 작업 파일과 VCS 폴더는 조치에 넘길 ZIP에 넣지 않는다
EXCLUDED_DIRS = (".git", ".sbmig")
_MAVEN_OUTPUT_MARKERS = ("maven-status", "maven-archiver")
# remediation.log 한 줄의 형식이다. 단독 업로드의 service.LOG_FORMAT과 같다
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
# 조치를 실행하는 컨텍스트 표시. LangGraph가 노드를 다른 스레드에서 실행해도 컨텍스트를 복사하므로 그 줄도 파일에 들어간다
_REM_LOG: contextvars.ContextVar[object | None] = contextvars.ContextVar("sbmig_remediation_log", default=None)


def chain_paths(run_dir: Path | str) -> RunPaths:
    """이관 실행 폴더 안에 있는 조치 실행 폴더 ``<run_dir>/remediation``의 경로들을 반환한다.

    The `RunPaths` of the chained remediation inside a migration run directory."""
    return RunPaths(Path(run_dir) / CHAIN_DIR)


def load_state(run_dir: Path | str) -> RemediationState | None:
    """이관에 이어 실행한 조치의 ``state.json``을 읽어 반환한다.
    파일이 없거나 읽을 수 없으면 None을 반환하고 예외를 던지지 않는다.

    The chained remediation's state, or None."""
    try:
        data = read_json(chain_paths(run_dir).state)
        return RemediationState.from_dict(data) if isinstance(data, dict) else None
    except Exception:
        log.debug("chained remediation state unreadable", exc_info=True)
        return None


# ---------------------------------------------------------------------------- 실행 폴더 점유 / run claim
# 상태를 진행시키는 프로세스는 조치 폴더 하나에 하나뿐이어야 한다.
# 웹 파드의 조회는 점유하지 않고 claim_info로 확인만 한다
CLAIM_NAME = "claim.json"
CLAIM_STALE_SECONDS = 600.0               # 이 시간 동안 갱신되지 않은 점유는 넘겨받을 수 있다
CLAIM_REFRESH_SECONDS = 30.0              # 점유한 프로세스가 claim.json의 mtime을 갱신하는 간격
CLAIM_HELD = "another process is already working on this run"
CLAIM_EVENT_TAKEN = "remediation_claim_taken_over"


def claim_path(root: Path | str) -> Path:
    """조치 폴더의 점유 파일 ``<root>/claim.json`` 경로를 반환한다.

    The claim file of one remediation directory."""
    return Path(root) / CLAIM_NAME


def _read_claim(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _fs_now(root: Path) -> float:
    """조치 폴더가 있는 파일 시스템의 현재 시각을 잰다. 실패하면 이 호스트의 시각을 반환한다.
    NFS에서는 서버가 mtime을 기록하므로 파드 사이의 시계 차이가 점유 판단에 끼어들지 않는다.

    Now on the claim's file system clock (a probe file's mtime, removed at once); this host's clock on failure."""
    probe = Path(root) / f".claim.probe.{os.getpid()}.{uuid.uuid4().hex[:8]}"
    made = False
    try:
        fd = os.open(str(probe), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        made = True
        try:
            os.write(fd, b"p")
        finally:
            os.close(fd)
        return os.stat(str(probe)).st_mtime
    except OSError:
        return time.time()
    finally:
        if made:
            try:
                os.unlink(str(probe))
            except OSError:
                pass


def _claim_stale(root: Path, info: dict[str, Any], mtime: float) -> bool:
    """점유가 오래되어 넘겨받아도 되는지 판단한다.
    같은 호스트에서 pid가 죽었거나, 파일 시스템 시계로 ``CLAIM_STALE_SECONDS``초 넘게 갱신되지 않았으면 오래된 것이다."""
    from sbmig.kb.lock import pid_alive
    if info.get("host") == socket.gethostname() and info.get("pid") is not None and not pid_alive(info.get("pid")):
        return True
    return _fs_now(root) - mtime > float(CLAIM_STALE_SECONDS)


def _holder_text(info: dict[str, Any] | None) -> str:
    if not info:
        return "an unknown process"
    text = f"pid {info.get('pid', '?')} on host {info.get('host', '?')} since {info.get('started', '?')}"
    if info.get("attempt") not in (None, ""):
        text += f" (attempt {info['attempt']})"
    return text


def claim_info(root: Path | str) -> dict[str, Any] | None:
    """지금 조치 폴더를 점유한 프로세스 정보를 반환한다. 점유가 없거나 오래되어 넘겨받을 수 있으면 None이다.
    읽기만 하며 점유 파일을 만들거나 지우지 않는다.

    The live holder of a remediation directory (pid, host, attempt, started, purpose), or None when there is no claim
    or it is stale. Read-only: the web pod's readers use this; they never take the claim."""
    root = Path(root)
    path = claim_path(root)
    try:
        st = os.stat(path)
    except OSError:
        return None
    info = _read_claim(path) or {}
    if _claim_stale(root, info, st.st_mtime):
        return None
    return info or {"pid": "?", "host": "?"}


def claim_refusal(info: dict[str, Any] | None) -> str:
    """점유 때문에 거절할 때 쓰는 영어 문장을 만든다.

    The refusal sentence that names the holder."""
    return f"{CLAIM_HELD}: {_holder_text(info)}"


class RunClaim:
    """조치 폴더 하나를 상태를 진행시키는 프로세스 하나만 쓰도록 점유하는 클래스.
    ``acquire``가 ``claim.json``을 배타적으로 만들고, ``with`` 블록이 끝나면 어떤 경로로 끝나든 지운다.

    One driver per remediation directory: `claim.json` created with ``O_CREAT | O_EXCL`` holding pid, host, attempt,
    start time, purpose and a token. A fresh claim of anyone else → `StateConflict` (nothing touched). A stale claim (the
    holder's pid gone on this host, or not refreshed for `CLAIM_STALE_SECONDS` on the file system's clock — a pod that
    was OOM-killed never releases it) is moved aside, checked to be the one judged, removed and journaled
    (`remediation_claim_taken_over`, the old holder named). While held, a daemon thread refreshes the mtime every
    `CLAIM_REFRESH_SECONDS` (so a long Maven build or IVAS wait never makes it look stale). Leaving the `with` block
    stops the thread, restores the SIGTERM handler and removes the file when it still holds our token. In the main
    thread a default SIGTERM handler is replaced by one that raises `SystemExit` for the time of the block, so a
    SIGTERM still runs the release."""

    def __init__(self, root: Path | str, *, purpose: str, attempt: Any = None, journal: Journal | None = None):
        self.root = Path(root)
        self.path = claim_path(self.root)
        self.purpose = purpose
        self.attempt = attempt
        self.token = uuid.uuid4().hex
        self.held = False
        self._jr = journal
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._restore: Callable[[], None] | None = None
        self._lost_said = False

    def acquire(self) -> RunClaim:
        """점유 파일을 만든다. 다른 프로세스가 살아 있는 점유를 가졌으면 아무것도 바꾸지 않고 ``StateConflict``를 던진다.

        Take the claim; `StateConflict` naming the holder when a fresh claim of another process exists."""
        self.root.mkdir(parents=True, exist_ok=True)
        info: dict[str, Any] | None = None
        for _ in range(5):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                try:
                    st = os.stat(self.path)
                except FileNotFoundError:
                    continue
                info = _read_claim(self.path)
                if not _claim_stale(self.root, info or {}, st.st_mtime):
                    raise StateConflict(claim_refusal(info), holder=dict(info or {})) from None
                self._take_over(st, info)
                continue
            payload = {"pid": os.getpid(), "host": socket.gethostname(), "attempt": self.attempt,
                       "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "purpose": self.purpose,
                       "token": self.token}
            try:
                os.write(fd, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                try:
                    os.fsync(fd)
                except OSError:
                    pass
            finally:
                os.close(fd)
            self.held = True
            self._thread = threading.Thread(target=self._keep_alive, name="sbmig-remediation-claim", daemon=True)
            self._thread.start()
            return self
        raise StateConflict(claim_refusal(info or _read_claim(self.path)), holder=dict(info or {}))

    def _take_over(self, judged: os.stat_result, info: dict[str, Any] | None) -> None:
        """오래된 점유 파일을 옆으로 옮기고, 옮긴 파일이 판단한 그 파일인지 확인한 뒤 지운다.
        다른 프로세스가 그 사이에 새로 만든 점유였으면 제자리에 되돌린다."""
        aside = self.path.with_name(f"{CLAIM_NAME}.stale.{uuid.uuid4().hex[:8]}")
        try:
            os.rename(self.path, aside)
        except FileNotFoundError:
            return                           # another taker moved it first
        try:
            st = os.stat(aside)
            if (st.st_ino, st.st_mtime_ns) != (judged.st_ino, judged.st_mtime_ns):
                try:
                    os.link(aside, self.path)      # exclusive: never replaces a claim made in between
                except OSError:
                    log.warning("remediation claim %s: a claim moved aside by mistake could not be put back", self.path)
                return
            log.warning("remediation claim %s: taking over a stale claim of %s", self.path, _holder_text(info))
            if self._jr is not None:
                try:
                    self._jr({"event": CLAIM_EVENT_TAKEN, "holder": {k: v for k, v in (info or {}).items() if k != "token"},
                              "purpose": self.purpose})
                except Exception:  # noqa: BLE001 - 저널 기록 실패로 점유를 막지 않는다
                    log.debug("claim journal line not written", exc_info=True)
        finally:
            try:
                os.unlink(aside)
            except OSError:
                pass

    def refresh(self) -> bool:
        """점유 파일이 아직 이 프로세스의 것이면 mtime을 지금으로 갱신한다.

        Touch the claim while it still holds our token; False when it is not ours any more."""
        info = _read_claim(self.path)
        if not info or info.get("token") != self.token:
            if not self._lost_said:
                self._lost_said = True
                log.warning("remediation claim %s is no longer held by this process", self.path)
            return False
        try:
            os.utime(self.path, None)
            return True
        except OSError:
            log.debug("remediation claim not refreshed", exc_info=True)
            return False

    def _keep_alive(self) -> None:
        while not self._stop.wait(float(CLAIM_REFRESH_SECONDS)):
            self.refresh()

    def release(self) -> None:
        """갱신 스레드를 멈추고, 점유 파일이 아직 이 프로세스의 것이면 지운다.

        Stop the keep-alive and remove the claim when it still holds our token."""
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        if not self.held:
            return
        self.held = False
        info = _read_claim(self.path)
        if info and info.get("token") == self.token:
            try:
                os.unlink(self.path)
            except OSError:
                log.warning("remediation claim %s could not be removed", self.path, exc_info=True)

    def __enter__(self) -> RunClaim:
        self._restore = _sigterm_to_exit()
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            if self._restore is not None:
                self._restore()
        finally:
            self.release()


class _LogFormatter(logging.Formatter):
    """파일에 쓰기 직전에 비밀 값, URL 속 계정 정보, 실행 폴더 절대 경로를 지우는 포매터 클래스."""

    def __init__(self, root: Path):
        super().__init__(LOG_FORMAT)
        self.root = root

    def format(self, record: logging.LogRecord) -> str:
        return graph_mod.scrub_message(super().format(record), self.root, limit=200_000)


class _LogFilter(logging.Filter):
    """조치를 실행하는 스레드와 그 컨텍스트를 물려받은 스레드의 줄만 통과시키는 필터 클래스."""

    def __init__(self, thread_id: int, token: object):
        super().__init__()
        self.thread_id = thread_id
        self.token = token

    def filter(self, record: logging.LogRecord) -> bool:
        return record.thread == self.thread_id or _REM_LOG.get() is self.token


@contextmanager
def remediation_log(paths: RunPaths) -> Iterator[logging.Handler | None]:
    """조치를 실행하는 동안 ``remediation/remediation.log``에 sbmig 로그를 DEBUG까지 덧붙인다.
    같은 파일의 핸들러가 이미 있으면 새로 달지 않고 None을 넘기며, 끝나면 단 핸들러를 떼고 닫는다.

    Append the sbmig log (DEBUG) of this remediation to `remediation/remediation.log` while the block runs: the
    chained run, the remediation-only run and a resume get the file the standalone run has. Lines pass the same
    scrubbing as the service's handler (`graph.scrub_message`: secret env values, URL userinfo, the run dir's absolute
    path, then the process-wide output scrubber); only this thread and threads running in its context write to it.
    A handler for the same file already on the `sbmig` logger (the in-process service's) is reused, never doubled; the
    handler added here is removed in `finally`. A file that cannot be opened yields None (the run goes on)."""
    target = os.path.abspath(paths.log)
    root = logging.getLogger("sbmig")
    if any(isinstance(h, logging.FileHandler) and os.path.abspath(h.baseFilename) == target for h in root.handlers):
        yield None
        return
    try:
        paths.root.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(paths.log, encoding="utf-8")
    except OSError:
        log.debug("remediation.log not attached", exc_info=True)
        yield None
        return
    token = object()
    ctx = _REM_LOG.set(token)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(_LogFormatter(paths.root))
    fh.addFilter(_LogFilter(threading.get_ident(), token))
    if root.level == logging.NOTSET or root.level > logging.DEBUG:
        for h in root.handlers:     # 파일에는 DEBUG까지, 콘솔에는 원래 수준대로 남긴다
            if h.level == logging.NOTSET:
                h.setLevel(logging.INFO if root.level in (logging.NOTSET, logging.INFO) else root.level)
        root.setLevel(logging.DEBUG)
    root.addHandler(fh)
    try:
        yield fh
    finally:
        root.removeHandler(fh)
        fh.close()
        try:
            _REM_LOG.reset(ctx)
        except ValueError:
            _REM_LOG.set(None)


def _sigterm_to_exit() -> Callable[[], None]:
    """메인 스레드의 SIGTERM 처리가 기본값이면 ``SystemExit``를 던지는 처리로 잠시 바꾸고, 되돌리는 함수를 반환한다.
    점유 해제가 ``finally``에서 실행되게 하기 위해서다."""
    try:
        if threading.current_thread() is not threading.main_thread():
            return lambda: None
        old = signal.getsignal(signal.SIGTERM)
        if old not in (signal.SIG_DFL, None):
            return lambda: None

        def handler(signum: int, _frame: Any) -> None:
            raise SystemExit(128 + signum)
        signal.signal(signal.SIGTERM, handler)
    except (ValueError, OSError, AttributeError):
        return lambda: None

    def restore() -> None:
        try:
            signal.signal(signal.SIGTERM, old)
        except (ValueError, OSError):
            pass
    return restore


def _try_claim(paths: RunPaths, jr: Journal, purpose: str, attempt: Any = None) -> RunClaim | None:
    """점유를 시도하고, 다른 프로세스가 가졌으면 WARNING 한 줄을 남기고 None을 반환한다."""
    try:
        return RunClaim(paths.root, purpose=purpose, attempt=attempt, journal=jr).acquire()
    except StateConflict as e:
        log.warning("vulnerability remediation %s: not started here: %s", paths.root.parent.name, e)
        return None


def _clear_root(root: Path) -> None:
    """상태 파일 없이 남은 조치 폴더의 내용을 지운다. 이 프로세스의 점유 파일은 남긴다."""
    import shutil
    root.mkdir(parents=True, exist_ok=True)
    for child in root.iterdir():
        # 점유 파일과 지금 열어 둔 remediation.log는 남긴다. 로그에는 앞선 시도의 줄도 이어서 남는다
        if child.name in (CLAIM_NAME, "remediation.log"):
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def _resume_attempt(run_dir: Path) -> Any:
    """Main이 기록한 재개 시도 번호(``control/remediation-resume.json``의 attempt)를 반환한다. 없으면 None이다."""
    try:
        data = read_json(Path(run_dir) / "control" / "remediation-resume.json")
        return data.get("attempt") if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 - 시도 번호는 점유 파일의 참고 정보일 뿐이다
        return None


def _problem(text: Any) -> str:
    return graph_mod.scrub_message(str(text), limit=500)


def readiness(cfg: Any, components: Components | None) -> list[str]:
    """취약점 조치를 시작할 수 없게 하는 문제 목록을 반환한다. 빈 목록이면 시작할 수 있다.
    빠진 IVAS 환경 변수 이름과 Maven / JDK / settings.xml 문제만 담고 값은 넣지 않는다.
    ``components``가 None이면 운영 구현으로 확인한다.

    Problems that prevent a chained remediation (IVAS env names + Maven readiness; never values; no trusted_projects)."""
    from . import ivas as ivas_mod
    problems = [_problem(p) for p in ivas_mod.readiness(ivas_mod.ivas_settings(cfg))]
    try:
        comps = components if components is not None else graph_mod.default_components(cfg)
        problems += [_problem(p) for p in comps.maven.readiness()]
    except Exception as e:  # noqa: BLE001 - 구성 요소를 만들지 못한 것 자체를 준비 문제로 본다
        problems.append(f"remediation components cannot be built ({type(e).__name__})")
    return problems


def _is_build_output(dirpath: Path, rel_parts: tuple[str, ...], filenames: Iterable[str]) -> bool:
    """``dirpath/target`` 폴더가 Maven 빌드 산출물인지 판단한다.
    src 아래면 산출물이 아니고, 옆에 pom.xml이 있거나 안에 Maven이 남기는 폴더가 있으면 산출물이다.
    패키지 이름이 target인 소스를 빠뜨리지 않도록 ``Workspace.walk``, result.zip과 같은 규칙을 쓴다."""
    if "src" in rel_parts:
        return False
    return "pom.xml" in filenames or any((dirpath / "target" / m).is_dir() for m in _MAVEN_OUTPUT_MARKERS)


def source_zip(project_dir: Path | str, out_zip: Path | str, *, exclude_dirs: Iterable[str] = (),
               journal: Journal | None = None) -> None:
    """이관이 끝난 프로젝트 트리를 ZIP으로 묶는다. 일반 파일만 이름 순으로 넣고 시각과 권한을 고정해서 매번
    같은 바이트가 나오게 하며, 루트 pom.xml은 ZIP 최상위에 둔다.
    ``.git/``, ``.sbmig/``, pom.xml 옆의 ``target/``, ``exclude_dirs``는 뺀다.
    심볼릭 링크는 접수에서 거부되므로 넣지 않고 저널에 ``remediation_symlink_skipped``를 남긴다.

    A deterministic ZIP of the migrated project (regular files only; `.git`, `.sbmig`, Maven `target/` output and
    `exclude_dirs` left out; symlinks skipped and journaled); the root pom.xml sits at the ZIP root."""
    root = Path(project_dir)
    out = Path(out_zip)
    ignored = {str(d).replace("\\", "/").strip("/") for d in exclude_dirs if str(d).strip("/")}
    files: list[str] = []
    skipped: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        d = Path(dirpath)
        rel_dir = d.relative_to(root).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir
        parts = tuple(rel_dir.split("/")) if rel_dir else ()
        keep: list[str] = []
        for name in sorted(dirnames):
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if (d / name).is_symlink():
                skipped.append(rel)
            elif name in EXCLUDED_DIRS or rel in ignored or any(rel.startswith(i + "/") for i in ignored) or name == "target" and _is_build_output(d, parts, filenames):
                continue
            else:
                keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames):
            rel = f"{rel_dir}/{name}" if rel_dir else name
            p = d / name
            if p.is_symlink():
                skipped.append(rel)
            elif p.is_file():
                files.append(rel)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.partial")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in sorted(files):
            info = zipfile.ZipInfo(rel, date_time=ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with open(root / rel, "rb") as src, zf.open(info, "w") as dst:
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    dst.write(chunk)
    os.replace(tmp, out)
    if skipped:
        log.warning("remediation source ZIP: %d symlink(s) left out (%s)", len(skipped), ", ".join(skipped[:5]))
        if journal is not None:
            try:
                journal({"event": "remediation_symlink_skipped", "paths": sorted(skipped)[:50], "count": len(skipped)})
            except Exception:
                log.debug("symlink journal line not written", exc_info=True)


def _scoped(journal: Journal | None) -> Journal:
    """이관 저널에 기록하는 줄마다 ``"scope": "remediation"``을 붙이는 저널 함수를 반환한다.
    저널 기록 실패는 무시한다."""
    def write(event: dict[str, Any]) -> None:
        if journal is None:
            return
        try:
            journal({**event, "scope": SCOPE})
        except Exception:
            log.debug("remediation journal line not written: %s", event.get("event"), exc_info=True)
    return write


def _safe_stop(stop: StopCheck | None) -> StopCheck:
    def check() -> bool:
        try:
            return bool(stop()) if stop is not None else False
        except Exception:  # noqa: BLE001 - 읽을 수 없는 중지 표시는 중지 요청으로 보지 않는다
            return False
    return check


class _Budget:
    """이번 호출의 시간 한도(분 단위, 0 이하면 한도 없음)를 관리하는 클래스.
    ``wrap(stop)``은 실제 중지 요청이 있거나 한도를 넘으면 참을 반환하는 중지 확인 함수를 만든다.
    한도를 처음 넘을 때 저널 ``remediation_budget``을 한 번 기록한다.

    The wall-time budget of one chained invocation; `wrap(stop)` also turns true once it is used up."""

    def __init__(self, cfg: Any, jr: Journal):
        rem = getattr(cfg, "remediation", None)
        try:
            minutes = float(getattr(rem, "chain_budget_minutes", 0) or 0)
        except (TypeError, ValueError):
            minutes = 0.0
        self.minutes = minutes
        self.started = _monotonic()
        self.exhausted = False
        self._jr = jr

    def over(self) -> bool:
        if self.minutes <= 0:
            return False
        if self.exhausted:
            return True
        if _monotonic() - self.started >= self.minutes * 60.0:
            self.exhausted = True
            self._jr({"event": "remediation_budget", "minutes": self.minutes})
            log.warning("chained remediation: time budget of %g minute(s) used up — stopping at the next boundary",
                        self.minutes)
            return True
        return False

    def time_left(self) -> float | None:
        """남은 시간(초)을 반환한다. 한도가 없으면 None이다."""
        if self.minutes <= 0:
            return None
        return self.minutes * 60.0 - (_monotonic() - self.started)

    def wrap(self, stop: StopCheck) -> StopCheck:
        # 그래프의 빌드 오류 수정 루프가 남은 시간을 알 수 있도록 중지 확인 함수에 time_left를 붙인다
        def check() -> bool:
            return bool(stop()) or self.over()
        check.time_left = self.time_left        # type: ignore[attr-defined]
        return check


def _budget_outcome(paths: RunPaths, state: RemediationState, budget: _Budget, real_stop: StopCheck) -> RemediationState:
    """시간 한도 때문에 STOPPED가 된 상태의 사유를 ``chain_budget``으로 바꿔 저장한다.
    실제 중지 요청이 있었으면 사유를 그대로 둔다.
    화면에서 시간 한도로 멈췄으며 재개하면 이어서 진행한다고 안내하기 위해서다."""
    from .models import R_STOP_REQUESTED, STOPPED
    if state.status != STOPPED or state.reason != R_STOP_REQUESTED or real_stop() or not budget.over():
        return state
    detail = state.message or ""
    state.reason, state.reasons = R_CHAIN_BUDGET, [R_CHAIN_BUDGET]
    state.stop_requested = False
    state.message = (f"the chained remediation used its time budget of {budget.minutes:g} minute(s) in this pod; "
                     "resume continues from the stopped node"
                     + (" (the remote IVAS analysis keeps running and is polled again without a new upload)"
                        if state.pending_receipt else "")
                     + (f" | {detail}" if detail else ""))
    _try_save(paths, state)
    return state


def _saver(paths: RunPaths) -> Callable[[RemediationState], None]:
    owner = f"chain-{os.getpid()}"

    def save(st: RemediationState) -> None:
        st.owner = owner
        write_json_atomic(paths.state, st.to_dict())
    return save


def _failed_state(run_id: str, reason: str, message: str, policy_hash: str = "",
                  base: RemediationState | None = None) -> RemediationState:
    now = time.time()
    st = base or RemediationState(id=run_id, name=SOURCE_NAME, created=now, policy_hash=policy_hash, next_node="")
    st.status, st.reason, st.reasons, st.message, st.updated = FAILED, reason, [reason], message, now
    return st


def _try_save(paths: RunPaths, st: RemediationState) -> None:
    try:
        _saver(paths)(st)
    except Exception:
        log.warning("chained remediation state not saved", exc_info=True)


def _error(paths: RunPaths, jr: Journal, e: BaseException, run_id: str,
           state: RemediationState | None) -> RemediationState:
    """잡은 예외를 저널 ``remediation_error``와 로그(비밀 값을 마스킹한 traceback)에 남기고 FAILED 상태로 저장한다."""
    msg = graph_mod.scrub_message(f"{type(e).__name__}: {e}", paths.root, 500)
    jr({"event": "remediation_error", "type": type(e).__name__, "message": msg})
    log.error("chained remediation failed: %s", graph_mod.scrub_message(traceback.format_exc(), paths.root, 20000))
    st = _failed_state(run_id, R_INTERNAL_ERROR, msg, base=state)
    _try_save(paths, st)
    return st


def clean_targets(targets: Iterable[str] | None) -> list[str]:
    """이관의 최종 빌드 대상 폴더 목록을 조치 상태에 넣을 형태로 정리한다.
    프로젝트 기준 상대 경로로 순서를 유지하고, 중복, 빈 값, 프로젝트 밖 경로는 뺀다.
    루트 하나뿐이면 빈 목록을 반환하며, 이때 조치는 루트에서 한 번 빌드한다.

    The migration's final-build target directories as stored in the state; [] when that is the root alone."""
    from .inventory import normalize_roots
    roots = normalize_roots([str(t) for t in (targets or []) if t is not None])
    return [] if roots == ["."] else roots


def _baseline_outcome(paths: RunPaths, state: RemediationState, jr: Journal, excluded: Iterable[str]) -> RemediationState:
    """같은 트리로 이관의 최종 빌드는 통과했는데 조치 전 빌드가 실패했으면 그 사실을 메시지 앞에 분명히 쓴다.
    같은 Maven 설정이라는 것, 비교할 두 로그, 조치가 빌드한 트리에서 빠진 것(.git / .sbmig / target 산출물 /
    접수에서 제외한 폴더)을 담고 저널 ``remediation_baseline_differs``를 남긴다. 이관 결과는 바꾸지 않는다."""
    from .models import R_BASELINE_BUILD_FAILED
    # 이관의 최종 빌드가 실패한 트리(baseline_tolerated)에는 이 안내가 맞지 않는다
    if (state.status != FAILED or state.reason != R_BASELINE_BUILD_FAILED or state.baseline_tolerated
            or "migration's final build passed" in (state.message or "")):
        return state
    left_out = ".git/, .sbmig/, target/ build output" + (f", ignored folders {', '.join(excluded)}" if excluded else "")
    where = (f" (build targets: {', '.join(state.build_targets)})" if state.build_targets else " (at the project root)")
    detail = state.message or ""
    state.message = ("the migration's final build passed on this same tree, but the remediation's baseline build failed"
                     f"{where} with the same Maven command base, settings.xml and local repository (only IVAS_* / LLM_* "
                     "variables are removed from its environment) — compare remediation/baseline/build.log with the "
                     "migration's final build log (logs/*-build-verify*.log); the remediation builds a copy "
                     f"(remediation/baseline/tree) without {left_out}"
                     + (f" | {detail}" if detail else ""))
    jr({"event": "remediation_baseline_differs", "remediation": state.id, "targets": list(state.build_targets or [])})
    log.warning("vulnerability remediation %s: the baseline build failed although the migration's final build passed on the "
                "same tree — see remediation/baseline/build.log", state.id)
    _try_save(paths, state)
    return state


_END_MESSAGE_CAP = 600


def _ended(jr: Journal, st: RemediationState, paths: RunPaths | None = None) -> None:
    jr({"event": "remediation_end", "remediation": st.id, "status": st.status, "reason": st.reason})
    text = " ".join(str(st.message or "").split())
    if text:
        # 회사 PC에서는 복사와 붙여넣기가 안 되므로 sbmig.log 한 줄만 보고도 실패 이유를 알 수 있게 한다
        msg = graph_mod.scrub_message(text, paths.root if paths is not None else None, _END_MESSAGE_CAP)
        msg = " ".join(msg.split())
        log.info("chained remediation %s: %s %s — %s", st.id, st.status, st.reason, msg)
    else:
        log.info("chained remediation %s: %s %s", st.id, st.status, st.reason)


def run_after_migration(run_dir: Path | str, project_dir: Path | str, cfg: Any, *, run_id: str, journal: Journal | None,
                        stop: StopCheck | None, components: Components | None = None,
                        exclude_dirs: Iterable[str] = (), build_targets: Iterable[str] = (),
                        baseline_may_fail: bool = False) -> RemediationState | None:
    """빌드에 성공한 이관 뒤에 취약점 조치를 한 번 실행하고 마지막 상태를 반환한다. 예외를 던지지 않는다.
    ``baseline_may_fail``이 True면 이관의 최종 빌드가 실패한 트리이며, 조치 전 빌드가 실패해도 조치를 이어 간다.
    - None을 반환하면 건너뛴 것이다. 기능이 꺼져 있으면 조용히 건너뛰고, 준비가 안 됐으면 저널
      ``remediation_skipped``와 WARNING 한 줄을 남긴다. 조치 상태가 이미 있으면 새로 시작하지 않고 저널
      ``remediation_exists``를 남긴 뒤 그 상태를 반환한다
    - 접수에서 거부되면 FAILED(사유는 거부 코드), 다른 예외면 FAILED internal_error로 저장한다
    - ``build_targets``는 이관의 최종 빌드 대상이며 조치도 같은 대상을 같은 방식으로 빌드한다

    One remediation after a successful migration. None = skipped (disabled / not ready); an existing chained state is
    returned untouched; otherwise the final state. Never raises."""
    paths = chain_paths(run_dir)
    jr = _scoped(journal)
    state: RemediationState | None = None
    try:
        budget = _Budget(cfg, jr)            # 시간 한도는 이 호출이 시작된 시점부터 잰다
        rem = getattr(cfg, "remediation", None)
        if rem is None or not bool(getattr(rem, "after_migration", False)):
            return None
        existing = load_state(run_dir)
        if existing is not None:
            jr({"event": "remediation_exists", "remediation": existing.id, "status": existing.status})
            log.info("chained remediation %s already exists (%s) — not started again", existing.id, existing.status)
            return existing
        comps = components
        if comps is None:
            try:
                comps = graph_mod.default_components(cfg, journal=jr)
            except Exception:
                log.debug("remediation components not built", exc_info=True)
        missing = readiness(cfg, comps)
        if missing:
            jr({"event": "remediation_skipped", "reason": "not_ready", "missing": missing})
            log.warning("vulnerability remediation skipped (not ready): %s", "; ".join(missing))
            return None
        real_stop = _safe_stop(stop)
        claim = _try_claim(paths, jr, "chained")
        if claim is None:
            # 다른 프로세스가 이 조치 폴더를 쓰고 있으면 아무것도 바꾸지 않는다
            return load_state(run_dir)
        with claim, remediation_log(paths):
            try:
                existing = load_state(run_dir)
                if existing is not None:
                    jr({"event": "remediation_exists", "remediation": existing.id, "status": existing.status})
                    return existing
                return _start(paths, Path(project_dir), cfg, comps, run_id=run_id, jr=jr, stop=budget.wrap(real_stop),
                              exclude_dirs=exclude_dirs, budget=budget, real_stop=real_stop, build_targets=build_targets,
                              baseline_may_fail=baseline_may_fail)
            except Exception as e:  # noqa: BLE001 — the migration's verdict must never depend on the remediation
                return _error(paths, jr, e, run_id, state)
    except Exception as e:  # noqa: BLE001 — the migration's verdict must never depend on the remediation
        return _error(paths, jr, e, run_id, state)


def _start(paths: RunPaths, project_dir: Path, cfg: Any, comps: Components, *, run_id: str, jr: Journal,
           stop: StopCheck, exclude_dirs: Iterable[str], budget: _Budget | None = None,
           real_stop: StopCheck | None = None, build_targets: Iterable[str] = (),
           baseline_may_fail: bool = False) -> RemediationState:
    from . import intake as intake_mod
    from . import planner as planner_mod
    from .models import IntakeRejected
    # 상태 파일 없이 남은 폴더는 접수 중에 중단된 이전 시도다.
    # IVAS에 보낸 기록은 상태가 생긴 뒤에만 있으므로 지우고 다시 시작한다. 점유 파일은 남긴다
    _clear_root(paths.root)
    policy = planner_mod.policy_hash(cfg)
    jr({"event": "remediation_start", "remediation": run_id, **({"baseline_may_fail": True} if baseline_may_fail else {})})
    if baseline_may_fail:
        log.info("vulnerability remediation %s: start (after the migration's failed build; a failing baseline build "
                 "is tolerated)", run_id)
    else:
        log.info("vulnerability remediation %s: start (after the migration's BUILD SUCCESS)", run_id)
    targets = clean_targets(build_targets)
    excluded = sorted({str(d).replace("\\", "/").strip("/") for d in exclude_dirs if str(d).strip("/")})
    if targets:
        jr({"event": "remediation_build_targets", "targets": targets})
        log.info("vulnerability remediation %s: building the migration's targets %s", run_id, ", ".join(targets))
    source_zip(project_dir, paths.original_zip, exclude_dirs=excluded, journal=jr)
    try:
        result = intake_mod.accept_zip(paths.original_zip, paths.root, intake_mod.limits_from_config(cfg),
                                       **({"build_targets": targets} if targets else {}))
    except IntakeRejected as e:
        st = _failed_state(run_id, e.code, graph_mod.scrub_message(str(e), paths.root, 500), policy)
        _try_save(paths, st)
        jr({"event": "remediation_intake_rejected", "remediation": run_id, "code": e.code})
        log.warning("vulnerability remediation %s: intake rejected the migrated tree (%s)", run_id, e.code)
        _ended(jr, st, paths)
        return st
    state = graph_mod.new_state(run_id, SOURCE_NAME, cfg, result, [], policy_hash=policy)
    state.build_targets = targets
    state.baseline_tolerated = bool(baseline_may_fail)
    save = _saver(paths)
    save(state)
    return _drive(paths, state, cfg, comps, jr=jr, stop=stop, budget=budget, real_stop=real_stop, excluded=excluded)


class RemediationNotReady(Exception):
    """조치만 하는 실행(``run_only``)을 시작할 수 없을 때 던지는 예외.
    빠진 IVAS 환경 변수 이름과 Maven 준비 문제를 담고 값은 넣지 않으며, 이때 상태는 만들어지지 않는다.

    The remediation-only run cannot start (`missing` = problem sentences, names only); no state was created."""

    def __init__(self, missing: Iterable[str]):
        self.missing = [str(m) for m in missing]
        super().__init__("; ".join(self.missing) or "not ready")


def zip_targets(zip_path: Path | str, targets: Iterable[str]) -> list[str]:
    """프로젝트 루트 기준의 빌드 대상 폴더를 ZIP 최상위 기준 경로로 바꾼다.
    ZIP이 프로젝트를 폴더로 감쌌으면 모든 대상에 맞는 가장 짧은 앞부분을 붙인다.
    맞추지 못하거나 ZIP을 읽을 수 없으면 그대로 반환하며, 그 경우 ``accept_zip``이 ``no_pom``으로 거부한다.

    Re-root project-relative build targets onto the ZIP root (a wrapping folder is prefixed); unchanged when nothing fits."""
    tl = [str(t).strip("/") for t in targets if str(t).strip("/") not in ("", ".")]
    if not tl:
        return []
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = {n.replace("\\", "/") for n in zf.namelist()}
    except (OSError, zipfile.BadZipFile, ValueError, EOFError):
        return tl
    if all(f"{t}/pom.xml" in names for t in tl):
        return tl
    tail = f"/{tl[0]}/pom.xml"
    prefixes = sorted({n[:-len(tail) + 1] for n in names if n.endswith(tail)}, key=lambda p: (len(p), p))
    for p in prefixes:
        if all(f"{p}{t}/pom.xml" in names for t in tl):
            return [f"{p}{t}" for t in tl]
    return tl


def run_only(run_dir: Path | str, source: Path | str, cfg: Any, *, run_id: str, journal: Journal | None,
             stop: StopCheck | None, components: Components | None = None,
             build_targets: Iterable[str] = ()) -> RemediationState:
    """이관 없이 취약점 조치만 한 번 실행한다(실행 방식 ``remediation``). ``source``가 ZIP 파일이면 그
    바이트를 그대로, 디렉토리면 ``source_zip``으로 묶어서 ``accept_zip``에 넣는다.
    - 준비가 안 됐으면 저널 ``remediation_skipped``를 남기고 상태를 만들지 않은 채 ``RemediationNotReady``를
      던진다. 던지는 예외는 이것뿐이다
    - 조치 상태가 이미 있으면 그대로 반환하고, 그 밖에는 이관에 이어 실행하는 조치와 같은 규칙으로 실행한다
    - ZIP이 폴더로 감싸여 있으면 ``build_targets``를 ``zip_targets``로 바꾼다

    One remediation without a migration. Raises only `RemediationNotReady` (nothing created); an existing state is
    returned untouched; otherwise the final state (intake rejection → FAILED, other errors → FAILED internal_error)."""
    paths = chain_paths(run_dir)
    jr = _scoped(journal)
    try:
        budget = _Budget(cfg, jr)
        existing = load_state(run_dir)
        if existing is not None:
            jr({"event": "remediation_exists", "remediation": existing.id, "status": existing.status})
            log.info("vulnerability remediation %s already exists (%s) — not started again", existing.id, existing.status)
            return existing
        comps = components
        if comps is None:
            try:
                comps = graph_mod.default_components(cfg, journal=jr)
            except Exception:
                log.debug("remediation components not built", exc_info=True)
        missing = readiness(cfg, comps)
        if missing:
            jr({"event": "remediation_skipped", "reason": "not_ready", "missing": missing})
            raise RemediationNotReady(missing)
        real_stop = _safe_stop(stop)
        claim = _try_claim(paths, jr, "remediation-only")
        if claim is None:
            # 다른 프로세스가 이 조치 폴더를 쓰고 있으면 아무것도 바꾸지 않고, 저장하지 않은 FAILED 상태를 반환한다
            return load_state(run_dir) or _failed_state(run_id, StateConflict.code,
                                                        claim_refusal(_read_claim(claim_path(paths.root))))
        with claim, remediation_log(paths):
            try:
                existing = load_state(run_dir)
                if existing is not None:
                    jr({"event": "remediation_exists", "remediation": existing.id, "status": existing.status})
                    return existing
                return _start_only(paths, Path(source), cfg, comps, run_id=run_id, jr=jr, stop=budget.wrap(real_stop),
                                   budget=budget, real_stop=real_stop, build_targets=build_targets)
            except Exception as e:  # noqa: BLE001 — a saved FAILED state, never a traceback out of here
                return _error(paths, jr, e, run_id, load_state(run_dir))
    except RemediationNotReady:
        raise
    except Exception as e:  # noqa: BLE001 — a saved FAILED state, never a traceback out of here
        return _error(paths, jr, e, run_id, load_state(run_dir))


def _start_only(paths: RunPaths, source: Path, cfg: Any, comps: Components, *, run_id: str, jr: Journal,
                stop: StopCheck, budget: _Budget, real_stop: StopCheck,
                build_targets: Iterable[str]) -> RemediationState:
    from . import intake as intake_mod
    from . import planner as planner_mod
    from .models import IntakeRejected
    # 상태 파일 없이 남은 폴더는 접수 중에 중단된 이전 시도다.
    # IVAS에 보낸 기록은 상태가 생긴 뒤에만 있으므로 지워도 된다. 점유 파일은 남긴다
    _clear_root(paths.root)
    policy = planner_mod.policy_hash(cfg)
    jr({"event": "remediation_start", "remediation": run_id, "flow": "remediation"})
    log.info("vulnerability remediation %s: start (remediation only — the project as uploaded, no migration)", run_id)
    targets = clean_targets(build_targets)
    if source.is_dir():
        name = SOURCE_NAME
        source_zip(source, paths.original_zip, journal=jr)
        zip_in = paths.original_zip
    else:
        name = source.name or SOURCE_NAME
        targets = zip_targets(source, targets)
        zip_in = source
    if targets:
        jr({"event": "remediation_build_targets", "targets": targets})
        log.info("vulnerability remediation %s: building the targets %s (no root pom)", run_id, ", ".join(targets))
    try:
        result = intake_mod.accept_zip(zip_in, paths.root, intake_mod.limits_from_config(cfg),
                                       **({"build_targets": targets} if targets else {}))
    except IntakeRejected as e:
        st = _failed_state(run_id, e.code, graph_mod.scrub_message(str(e), paths.root, 500), policy)
        st.name = name
        _try_save(paths, st)
        jr({"event": "remediation_intake_rejected", "remediation": run_id, "code": e.code})
        log.warning("vulnerability remediation %s: intake rejected the uploaded project (%s)", run_id, e.code)
        _ended(jr, st, paths)
        return st
    state = graph_mod.new_state(run_id, name, cfg, result, [], policy_hash=policy)
    state.build_targets = targets
    _saver(paths)(state)
    return _drive(paths, state, cfg, comps, jr=jr, stop=stop, budget=budget, real_stop=real_stop, chained=False)


def _run_flow(run_dir: Path) -> str:
    """실행 폴더의 report.json에 기록된 실행 방식을 반환한다. 없거나 읽을 수 없으면 "full"을 반환한다.
    재개할 때 조치 전 빌드 실패 메시지를 고르는 데 쓴다."""
    try:
        data = read_json(Path(run_dir) / "report.json")
        return str(data.get("flow") or "full") if isinstance(data, dict) else "full"
    except Exception:  # noqa: BLE001 — an unreadable report means "the chained case", as before
        return "full"


def _drive(paths: RunPaths, state: RemediationState, cfg: Any, comps: Components, *, jr: Journal,
           stop: StopCheck, budget: _Budget | None = None, real_stop: StopCheck | None = None,
           excluded: Iterable[str] = (), chained: bool = True) -> RemediationState:
    save = _saver(paths)
    try:
        state = graph_mod.run_remediation(state, paths, cfg, comps, save=save, journal=jr, stop=stop)
    except Exception as e:  # noqa: BLE001 — run_remediation turns node errors into FAILED; this guards the rest
        return _error(paths, jr, e, state.id, state)
    if chained:                              # 이관 없이 실행한 조치에는 이관의 최종 빌드가 통과했다는 정보가 없다
        state = _baseline_outcome(paths, state, jr, excluded)
    if budget is not None:
        state = _budget_outcome(paths, state, budget, real_stop or (lambda: False))
    _ended(jr, state, paths)
    return state


def resume(run_dir: Path | str, cfg: Any, *, journal: Journal | None, stop: StopCheck | None,
           components: Components | None = None) -> RemediationState:
    """이관에 이어 실행한 취약점 조치를 재개한다.
    새 빌드 파드의 Worker가 remediation-resume 작업으로 호출한다.
    재개할 수 없는 상태면 ``StateConflict``를 던지고, 아니면 ``stop_requested``를 지우고 ``next_node``부터
    그래프를 실행한다. 시간 한도는 이 호출부터 다시 잰다.

    Continue a RESUMABLE chained remediation (`StateConflict` otherwise; for a state an earlier version saved in
    `AWAITING_APPROVAL` the conflict says that the approval step was removed, `graph.approval_removed`). The run claim
    (`RunClaim`) is taken first:
    a fresh claim of another process → `StateConflict` ("another process is already working on this run: …") with
    nothing touched; the state is read again under the claim."""
    paths = chain_paths(run_dir)
    jr = _scoped(journal)
    state = load_state(run_dir)
    if state is None:
        raise StateConflict("no chained remediation in this run directory")
    if (removed := graph_mod.approval_removed(state)) is not None:
        log.error("chained remediation %s: resume refused: %s", state.id, removed)
        raise removed
    if not graph_mod.can_resume(state):
        raise StateConflict(f"chained remediation {state.id} cannot be resumed from {state.status}")
    try:
        claim = RunClaim(paths.root, purpose="resume", attempt=_resume_attempt(Path(run_dir)), journal=jr).acquire()
    except StateConflict as e:
        log.error("chained remediation %s: resume refused: %s", state.id, e)
        raise
    with claim, remediation_log(paths):
        # 점유하기 전에 다른 프로세스가 상태를 바꿨을 수 있으므로 다시 읽는다
        state = load_state(run_dir)
        if state is None:
            raise StateConflict("no chained remediation in this run directory")
        if not graph_mod.can_resume(state):
            raise StateConflict(f"chained remediation {state.id} cannot be resumed from {state.status}")
        try:
            budget = _Budget(cfg, jr)            # 재개한 이 파드에서 시간 한도를 처음부터 다시 잰다
            comps = components if components is not None else graph_mod.default_components(cfg, journal=jr)
            state.stop_requested = False
            jr({"event": "remediation_resume", "remediation": state.id, "next_node": state.next_node})
            real_stop = _safe_stop(stop)
            return _drive(paths, state, cfg, comps, jr=jr, stop=budget.wrap(real_stop), budget=budget,
                          real_stop=real_stop, chained=_run_flow(Path(run_dir)) != "remediation")
        except Exception as e:  # noqa: BLE001
            return _error(paths, jr, e, state.id, state)


def summary(state: RemediationState | None, run_dir: Path | str, *, skipped: dict[str, Any] | None = None) -> dict[str, Any]:
    """이관 보고서(``RunReport.remediation``)에 넣을 짧은 JSON 요약을 만든다.
    status(건너뛰었으면 "skipped"), reason, reasons, counts, 짧은 message, 결과 파일 경로, 결과 ZIP의
    build_ok / scanned를 담고, 조치 전 빌드를 실행했으면 ``baseline_build_ok``도 담는다.
    조치 전 빌드 실패를 받아들인 실행이면 ``baseline_tolerated: true``와 ``delivery.build_tolerated: true``를 담는다.

    A small JSON-able digest for the migration report."""
    if state is None:
        out: dict[str, Any] = {"status": "skipped", "dir": CHAIN_DIR}
        out.update({k: v for k, v in (skipped or {}).items() if k in ("reason", "missing")})
        return out
    paths = chain_paths(run_dir)
    files: dict[str, str] = {}
    for key, p in (("report_md", paths.report_md), ("report_json", paths.report_json), ("state", paths.state),
                   ("delivery_zip", paths.delivery_zip)):
        try:
            if p.is_file():
                files[key] = f"{CHAIN_DIR}/{p.relative_to(paths.root).as_posix()}"
        except OSError:
            continue
    out = {"status": state.status, "reason": state.reason, "reasons": list(state.reasons or [])[:10],
           "counts": dict(state.counts or {}), "message": graph_mod.scrub_message(state.message or "", paths.root, 300),
           "dir": CHAIN_DIR, "files": files}
    if state.delivery is not None:
        out["delivery"] = {"build_ok": bool(state.delivery.build_ok), "scanned": bool(state.delivery.scanned),
                           "sha256": state.delivery.sha256}
        if state.delivery.build_tolerated:
            out["delivery"]["build_tolerated"] = True
    if state.baseline_tolerated:
        # 조치 전 빌드 실패를 받아들인 실행이다. 이관 보고서와 화면이 빌드로 확인하지 못했다고 알린다
        out["baseline_tolerated"] = True
    if state.baseline_build is not None:
        # 실행 방식이 remediation일 때 결과 줄의 build= 값.
        # 업로드한 그대로의 빌드가 실행되고 통과했는지를 나타낸다
        out["baseline_build_ok"] = bool(state.baseline_build.ok)
    return out
