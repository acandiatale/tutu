"""Job 파드의 일생 전체 — 공유 볼륨 모드의 Python Worker (v2.1z-ac, 2026-11-09; ``deploy/docker/run-job.sh`` 의 공유 볼륨 갈래를 대신한다).

``python -m sbmig.web.worker`` 로 돈다. 환경 변수 이름은 run-job.sh 와 같다(``SBMIG_SOURCE_ZIP`` / ``SBMIG_OUT`` / ``SBMIG_RUN_ID`` 필수).
순서: Main 이 접수 때 고정한 ``control/request.json`` 을 읽고(없으면 거절) zip·설정·코드 버전을 대조한다(다르면 거절, exit 3) →
환경 대조 → 저널에 ``worker_started`` → ``worker-paths.yml``(KB 위치, LLM 원문 기록 끄기 — 배포팀의 결정: 기본으로 끈다) → ``sbmig.cli run`` 자식
프로세스(출력은 파드 로그로) → 실행 폴더의 비밀 값 지우기 → ``SBMIG-RESULT`` 줄 출력 → ``worker_finished`` → ``job-status.json``(run id ·
attempt · 버전이 들어 있어 Main 이 **지금 시도**의 결과만 믿는다). Worker 는 ``control/`` 에 쓰지 않는다(Main 의 몫).
비밀 값은 **쓰는 순간** 지운다(리뷰 2026-11-10): 자식은 언제나 ``SBMIG_REDACT_OUTPUT=1`` 로 돌아 자기 로그·저널을 스스로 지우고,
Worker 는 자식의 출력을 한 줄씩 지워서 파드 로그로 넘긴다. 끝난 뒤 실행 폴더를 훑는 것은 그 뒤의 마지막 단계다(Maven 출력, 보고서).
환경 대조(리뷰 2026-11-10): Job 을 만든 Main 이 ``control/state.json`` 에 환경 지문(``env_pins``)과 정책을 적었으면 자기 환경으로 다시
계산해 대조하고 실행별 값(``SBMIG_CONFIG`` / ``SBMIG_STOP_AFTER`` / ``SBMIG_LLM_PROVIDER``)도 ``request.json`` 과 맞춰 본다 — refuse 면
거절(exit 3), warn 이면 저널에 ``worker_env_differs`` 를 적고 돈다. 글에는 이름만 적는다(값·지문은 적지 않는다).
자리(v2.1z-ae): Job 이 여러 개 동시에 돌면 Main 이 ``SBMIG_JOB_SLOT``(1 부터)을 준다. 2번 자리부터는 Maven 로컬 저장소를 따로
쓴다 — 이 실행이 원래 쓸 저장소 옆의 ``<이름>-slot<k>`` 폴더를 만들어 환경 대조가 끝난 뒤 **자식의 env 에만** ``SBMIG_BUILD__LOCAL_REPO`` 로
넘긴다. 1번 자리·변수 없음은 그대로이다. 그 폴더를 만들거나 쓸 수 없으면(또는 기준을 못 정하면) 파드 안의 임시 저장소
(``<tmp>/sbmig-m2-slot<k>`` — 매번 다시 받지만 나누지 않는다)로, 그것마저 안 될 때만 공유 저장소로 돌며 그렇게 적는다(파드 로그 한 줄 +
저널 ``worker_slot``; 파드 안 저장소면 ``pod_local: true``).
종료 신호(리뷰 2026-11-10): Worker 는 파드의 PID 1 이라 처리기가 없으면 SIGTERM 이 아무 일도 하지 않는다 — SIGTERM / SIGINT 를 받으면
자식의 트리 전체(자기 세션의 Maven 포함)에 신호를 넘기고, 잠깐(``TERM_GRACE_S``) 기다린 뒤 남은 것을 SIGKILL 하고, ``job-status.json`` 에
exit 3 과 "terminated by SIGTERM" 오류를 쓰고(저널 ``worker_finished`` 에 ``terminated``) 3 으로 끝난다.
종료 코드: 3 = 중단(거절 / 종료 신호 / sbmig exit 3 / 자식을 못 띄움), 2 = 빌드 실패이고 ``SBMIG_FAIL_JOB_ON_BUILD_FAIL=1``, 그 밖에는
0 — 빌드가 실패해도 보고서가 결과다(run-job.sh 와 같다).
취약점 조치(2026-11-11): 이관이 BUILD SUCCESS 로 끝나고 ``remediation.after_migration`` 이 켜져 있으면 자식 ``sbmig run`` 이 같은 프로세스에서
조치를 이어 돌린다(``<run dir>/remediation/``). 자식은 Worker 의 env 를 그대로 받으므로 IVAS 값(``IVAS_BASE_URL`` / ``IVAS_USER_ID`` /
``IVAS_PASSWORD``, Job 틀의 secretKeyRef)도 닿는다. 마지막 정제는 ``remediation/`` 의 텍스트 파일도 훑고(ZIP·프로젝트 트리·원본은 빼고),
``IVAS_USER_ID`` 는 이름에 비밀 낱말이 없어도 비밀로 지운다(``contract.SECRET_NAMES``). 조치 도중 종료 신호로 끊기면 이관의 결과 줄을
오류 글에 붙인다.

The Job pod's whole life in shared-volume mode: verify the request Main pinned, run `sbmig.cli run` as a child (which
redacts its own log / journal lines as it writes them; its console output is redacted line by line on the way to the pod
log), redact the run dir, write `job-status.json` for this attempt. Exit 3 = stopped / refused, 2 = build failed and
`SBMIG_FAIL_JOB_ON_BUILD_FAIL=1`, else 0. Review 2026-11-10: when the Main that created the Job pinned its environment
(`env_pins` + policy in `control/state.json`), the Worker recomputes the pins and checks the per-run values against
`request.json` before it starts — refuse → exit 3, warn → journal `worker_env_differs` and go on; names only, never a
value or a hash. Slots (v2.1z-ae): with several Jobs at once Main passes `SBMIG_JOB_SLOT` (1-based); slot k ≥ 2 gets its own
Maven local repository, the sibling `<base>-slot<k>` of the one the run would use otherwise, through `SBMIG_BUILD__LOCAL_REPO`
in the child's environment only (after the environment check); slot 1 / no slot = unchanged; when it cannot be created or
written (or the base is unknown) a pod-local repository under the temp directory (cold, never shared; `pod_local: true`),
and only when that fails too the shared repository, said so either way (journal `worker_slot`). Stop signals (review
2026-11-10): as the pod's PID 1 the Worker would ignore SIGTERM without a handler; on SIGTERM / SIGINT it forwards the
signal to the child's whole tree (Maven in its own session included), SIGKILLs what is left after `TERM_GRACE_S`, writes
exit 3 + a "terminated" error to `job-status.json` and exits 3. Remediation (2026-11-11): a migration that ended BUILD
SUCCESS with `remediation.after_migration` on continues into the remediation inside the same child (`<run dir>/remediation/`);
the child inherits the IVAS variables from the Worker's environment, the final redaction pass covers `remediation/`'s text files
(never its ZIPs, project trees or `original/`), and a stop signal during the remediation keeps the migration's result line in
the error text."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from sbmig.core import scrub
from sbmig.core.config import PACKAGE_ROOT
from sbmig.web import contract
from sbmig.web.redact import Redactor, redact_run_dir

REQUIRED_ENV = ("SBMIG_SOURCE_ZIP", "SBMIG_OUT", "SBMIG_RUN_ID")
PATHS_FILE = "worker-paths.yml"
RECORD_DIR_ENV = ("LLM_RECORD_DIR", "SBMIG_LLM__RECORD_DIR")    # LLM 원문 기록 폴더를 정하는 변수들 — 기록을 켜지 않았으면 자식에게 넘기지 않는다
JOURNAL_FILE = "journal.jsonl"
RESULT_MARK = "SBMIG-RESULT"
REFUSE_NO_REQUEST = "control/request.json missing — the Worker runs only requests Main accepted"
# 자식 콘솔의 줄 폭(COLUMNS 가 없을 때): 파이프는 터미널이 아니라 rich 가 80 칸에서 줄을 접는다 — 접힌 줄에 걸친 값은
# 줄 단위 정제가 놓친다. 넓게 잡아 접히는 일을 줄인다
CHILD_COLUMNS = "200"
# ── 환경 대조(리뷰 2026-11-10) ──
ENV_DIFFERS_EVENT = "worker_env_differs"
# 차이의 갈래(contract.compare_env_pins 의 키)와 글 속 이름 — Main 은 웹 파드, Worker 는 Job 파드다
ENV_DIFF_LABELS = (("differs", "values differ"), ("main_only", "set in the web pod only"),
                   ("worker_only", "set in the Job pod only"))
# Main 이 request.json 에 적은 실행별 값과 Worker 가 받은 env 의 이름(SBMIG_CONFIG 는 따로 본다 — 파일 경로 대조)
RUN_VALUE_FIELDS = (("SBMIG_STOP_AFTER", "stop_after"), ("SBMIG_LLM_PROVIDER", "llm_provider"))
# 거절·경고 글 끝의 고정 안내: 운영자가 할 일. 네임스페이스·디플로이먼트 이름은 넣지 않는다(설치마다 다르다)
ENV_PIN_HINT = ("after changing ConfigMap sbmig-env or the Job template, restart the web pod and re-create ConfigMap "
                f"sbmig-job-template; {contract.ENV_PIN_POLICY_ENV}=warn (or off) on the web pod relaxes this check")

# ── 자리마다 Maven 로컬 저장소 하나(v2.1z-ae, 2026-11-10) ──
# 세 파드가 PVC 하나(~/.m2)를 나눠 쓴다. Maven 기본 설정은 여러 프로세스가 한 로컬 저장소에 동시에 쓰는 것을 약속하지 않는다
LOCAL_REPO_ENV = "SBMIG_BUILD__LOCAL_REPO"    # 자식의 build.local_repo 를 정하는 설정 덮어쓰기(load_config 가 값을 yaml 로 읽는다)
SLOT_EVENT = "worker_slot"
# 자리 저장소를 PVC 에 못 만들 때의 물러섬: 파드 안(임시 폴더)의 저장소 — 매번 비어 있어 느리지만 다른 파드와 절대 나누지 않는다.
# 공유 저장소(여러 Maven 이 한 저장소에)는 그것마저 안 될 때만
POD_LOCAL_REPO_NAME = "sbmig-m2-slot{slot}"
POD_LOCAL_KEY = "pod_local"                   # worker_slot 저널 줄의 키(파드 안 저장소일 때만 true 로 붙는다)
WRITE_PROBE = ".sbmig-write-probe-{pid}"      # 쓰기 가능 확인용 파일 — 만들고 바로 지운다

# ── 종료 신호(리뷰 2026-11-10) ──
# 컨테이너의 PID 1 은 기본 신호 동작이 없다: 처리기가 없으면 SIGTERM 이 아무 일도 하지 않고, 자식 sbmig 와 Maven 이 종료 유예
# 시간 내내 자리 저장소와 실행 폴더에 계속 쓴다(그 사이 Main 은 같은 자리에 다음 실행을 띄울 수 있다)
STOP_SIGNALS = ("SIGTERM", "SIGINT")
# 자식 트리에 신호를 넘긴 뒤 스스로 끝나기를 기다리는 시간, 그 뒤 SIGKILL 후 사라지기를 기다리는 시간. 합이 쿠버네티스 기본 종료 유예
# (30초)와 local-job 의 기다림(10초, localjob._kill_tree) 안에 들어야 Worker 가 상태 파일까지 쓰고 나간다
TERM_GRACE_S = 5.0
KILL_GRACE_S = 3.0
TERMINATED_EXIT = 3                            # = EXIT_ABORTED — Main 이 아는 "중단"
STOP_POLL_S = 0.1

RunChild = Callable[[list[str], Path, dict[str, str]], int]
# (자식에게 넘길 설정 파일들, worker-paths.yml) → 이 실행이 자리 없이 쓸 로컬 저장소
RepoResolver = Callable[[list[str], Path], Path]
# 신호 처리기를 걸고, 원래대로 돌리는 함수를 돌려준다(테스트는 진짜 신호 없이 처리기를 직접 부른다)
InstallSignals = Callable[[Callable[[int, Any], None]], Callable[[], None]]


def _say(msg: str) -> None:
    print(f"[worker] {msg}", flush=True)


def _journal(run_dir: Path, event: str, data: dict) -> None:
    # a local helper on purpose: sbmig.web.runs imports this side of the contract (circular import otherwise);
    # flat like Workspace.journal / runs._append_journal so the web tail reads it the same way
    line = {"ts": contract.utc_now(), "event": event, **data}
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        with open(run_dir / JOURNAL_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
    except OSError as e:
        _say(f"WARN: cannot append to the journal: {e}")


class WorkerTerminated(BaseException):
    """종료 신호(SIGTERM / SIGINT)를 받아 자식 실행을 끊을 때 올리는 예외. ``BaseException`` 인 것은 ``except Exception`` 이
    삼키지 않게 하려는 것이다.

    Raised (from the signal handler) to abandon the child run on SIGTERM / SIGINT; a BaseException so that no
    `except Exception` swallows it."""

    def __init__(self, signum: int):
        self.signum = int(signum)
        super().__init__(signal_name(self.signum))


def signal_name(signum: int) -> str:
    """신호 번호의 이름(``SIGTERM``), 모르면 ``signal <n>``. / The signal's name, else `signal <n>`."""
    try:
        return signal.Signals(signum).name
    except ValueError:
        return f"signal {signum}"


class StopSignals:
    """종료 신호의 상태. 처리기(``handle``)는 첫 신호를 기억하고, 자식이 도는 동안(``arm`` ~ ``disarm``)에만 ``WorkerTerminated`` 를
    올린다 — 그 밖(입력 대조, 마지막 정제·상태 파일 쓰기)에서는 기억만 한다: 상태 파일을 쓰다 끊기면 Main 이 결과를 못 받는다.
    한 번 올린 뒤에는 다시 올리지 않는다(정리하는 동안 온 두 번째 SIGTERM 이 정리를 끊지 않게).

    The stop-signal state: the handler remembers the first signal and raises `WorkerTerminated` only while armed (the child
    is running) and only once; outside that window (checks, the final redaction, the status file) it just records."""

    def __init__(self) -> None:
        self.signum: int | None = None
        self.armed = False

    def handle(self, signum: int, frame: Any = None) -> None:
        """신호 처리기. / The signal handler."""
        del frame
        if self.signum is None:
            self.signum = int(signum)
        if self.armed:
            self.armed = False
            raise WorkerTerminated(signum)

    def arm(self) -> None:
        """자식을 띄우기 직전: 이미 신호가 왔으면 띄우지 않고 바로 ``WorkerTerminated``. / Arm; raise at once if a signal came."""
        if self.signum is not None:
            raise WorkerTerminated(self.signum)
        self.armed = True

    def disarm(self) -> None:
        """자식이 끝난 뒤. / Disarm after the child ended."""
        self.armed = False


def install_stop_signals(handler: Callable[[int, Any], None]) -> Callable[[], None]:
    """``SIGTERM`` / ``SIGINT`` 에 ``handler`` 를 걸고 원래 처리기로 돌리는 함수를 돌려준다. 주 스레드가 아니거나 플랫폼이 거절하면
    걸지 않는다(Windows 의 local-job 에서도 import·실행이 깨지지 않는다).

    Install `handler` for SIGTERM / SIGINT; returns a function restoring the previous handlers. Skipped off the main thread
    or where the platform refuses (never breaks a Windows / macOS dev run)."""
    previous: dict[int, Any] = {}
    if threading.current_thread() is not threading.main_thread():
        return lambda: None
    for name in STOP_SIGNALS:
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            previous[sig] = signal.signal(sig, handler)
        except (ValueError, OSError, RuntimeError):
            continue

    def restore() -> None:
        for sig, prev in previous.items():
            try:
                signal.signal(sig, prev if prev is not None else signal.SIG_DFL)
            except (ValueError, OSError, RuntimeError, TypeError):
                pass
    return restore


# ── 자식 프로세스 트리 끝내기 ──
# sbmig 자식은 SIGTERM 에 처리기가 없어 atexit(kill_live_processes) 없이 바로 죽고, Maven 은 자기 세션에서 돈다(mvn.run_logged 의
# start_new_session) — 자식의 그룹에만 신호를 보내면 Maven 은 남는다. 그래서 신호를 보내기 전에 Worker 의 자손 전체를 적어 두고,
# 고아가 되어 Worker(파드의 PID 1)에게 넘어온 것도 다시 훑어 함께 끝낸다

def parse_proc_stat(raw: str) -> tuple[int, int, str]:
    """Linux ``/proc/<pid>/stat`` 한 줄 → ``(ppid, pgid, 상태)``. 형식은 ``pid (comm) state ppid pgrp …`` 이고 comm 에 괄호·공백이
    있을 수 있어 마지막 ``)`` 뒤를 자른다. 형식이 틀리면 ``ValueError`` / ``IndexError``.

    Parse one `/proc/<pid>/stat` line into `(ppid, pgid, state)` (split after the last ')': comm may hold spaces / parens)."""
    rest = raw[raw.rindex(")") + 2:].split()
    return int(rest[1]), int(rest[2]), rest[0]


def process_table() -> dict[int, tuple[int, int, str]]:
    """``{pid: (ppid, pgid, 상태)}`` — Linux 는 ``/proc``, 그 밖의 Unix 는 ``ps``. 읽지 못하면 빈 dict(Windows 포함).

    The process table `{pid: (ppid, pgid, state)}` from `/proc` (Linux) or `ps` (other Unix); empty when unreadable."""
    table: dict[int, tuple[int, int, str]] = {}
    proc = Path("/proc")
    if sys.platform.startswith("linux") and proc.is_dir():
        for d in proc.iterdir():
            if not d.name.isdigit():
                continue
            try:
                table[int(d.name)] = parse_proc_stat((d / "stat").read_text(encoding="utf-8", errors="replace"))
            except (OSError, ValueError, IndexError):
                continue
        return table
    if os.name == "nt":
        return table
    try:
        ps = subprocess.Popen(["ps", "-A", "-o", "pid=,ppid=,pgid=,stat="], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True)
    except OSError:
        return table
    try:
        out, _ = ps.communicate(timeout=10)
    except subprocess.SubprocessError:
        ps.kill()
        ps.communicate()
        return table
    ps_pid = ps.pid                     # ps 자신은 Worker 의 자손으로 표에 보인다 — 끝낼 대상이 아니다
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[0])
            if pid != ps_pid:
                table[pid] = (int(parts[1]), int(parts[2]), parts[3])
        except ValueError:
            continue
    return table


def descendants(root: int, table: dict[int, tuple[int, int, str]]) -> set[int]:
    """``root`` 의 자손 pid 전부(``root`` 제외). / Every descendant pid of `root` (not `root` itself)."""
    children: dict[int, list[int]] = {}
    for pid, (ppid, _pgid, _state) in table.items():
        children.setdefault(ppid, []).append(pid)
    out: set[int] = set()
    todo = [root]
    while todo:
        for kid in children.get(todo.pop(), []):
            if kid not in out and kid != root:
                out.add(kid)
                todo.append(kid)
    return out


def _alive(pid: int, pgid: int, table: dict[int, tuple[int, int, str]]) -> bool:
    # 좀비(Z)는 이미 끝난 것. pgid 가 달라졌으면 그 번호를 다른 프로세스가 다시 쓰는 것 — 건드리지 않는다
    hit = table.get(pid)
    return hit is not None and hit[1] == pgid and not hit[2].startswith("Z")


def _send(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _send_group(pgid: int, sig: int) -> None:
    # 자기 그룹(Worker 와 같은 그룹)과 1 이하는 보내지 않는다 — Worker 자신이나 시스템 전체를 치지 않게
    killpg = getattr(os, "killpg", None)
    if killpg is None or pgid <= 1 or pgid == os.getpgrp():
        return
    try:
        killpg(pgid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _reap(pids: set[int]) -> None:
    # 고아가 Worker(파드의 PID 1)에게 넘어오면 Worker 가 거둬야 좀비로 남지 않는다. 우리 자식이 아닌 pid 는 ChildProcessError
    waitpid = getattr(os, "waitpid", None)
    nohang = getattr(os, "WNOHANG", None)
    if waitpid is None or nohang is None:
        return
    for pid in pids:
        try:
            waitpid(pid, nohang)
        except (ChildProcessError, OSError):
            pass


def stop_process_tree(proc: subprocess.Popen, sig: int = signal.SIGTERM, *, term_grace_s: float = TERM_GRACE_S,
                      kill_grace_s: float = KILL_GRACE_S,
                      table: Callable[[], dict[int, tuple[int, int, str]]] = process_table) -> set[int]:
    """자식 ``proc`` 과 그 자손 전체(자기 세션에서 도는 Maven 포함)를 끝낸다: 자손을 적어 두고 → 각 프로세스와 (Worker 의 것이 아닌)
    프로세스 그룹에 ``sig`` → ``term_grace_s`` 동안 기다림(그 사이 Worker 에게 넘어온 고아도 다시 훑는다) → 남은 것에 SIGKILL →
    ``kill_grace_s`` 기다림. Windows 는 ``taskkill /T /F``. 끝내 남은 pid 를 돌려준다(빈 집합 = 다 끝남). 예외를 내지 않는다.

    Stop the child and its whole tree (Maven in its own session included): snapshot the descendants, send `sig` to each
    and to each process group that is not the Worker's, wait `term_grace_s` (re-scanning for orphans reparented to the
    Worker), SIGKILL what is left, wait `kill_grace_s`. Windows: `taskkill /T /F`. Returns the pids still alive."""
    if os.name == "nt" or not hasattr(os, "killpg"):
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError):
            proc.kill()
        try:
            proc.wait(timeout=term_grace_s + kill_grace_s)
        except subprocess.TimeoutExpired:
            return {proc.pid}
        return set()

    me = os.getpid()
    known: dict[int, int] = {}                 # pid → 처음 본 pgid(번호 재사용을 가려낸다)

    def scan() -> dict[int, tuple[int, int, str]]:
        snap = table()
        for pid in descendants(me, snap) | ({proc.pid} if proc.pid in snap else set()):
            known.setdefault(pid, snap[pid][1])
        return snap

    def alive(snap: dict[int, tuple[int, int, str]]) -> set[int]:
        # 자식 자신은 Popen 이 거둔다(poll) — 표를 못 읽는 곳에서도 자식의 생사는 안다
        proc.poll()
        _reap({p for p in known if p != proc.pid})
        out = {p for p, g in known.items() if p != proc.pid and _alive(p, g, snap)}
        if proc.returncode is None:
            out.add(proc.pid)
        return out

    def blast(s: int, pids: set[int]) -> None:
        for pid in pids:
            _send(pid, s)
        for pgid in {known[p] for p in pids}:
            _send_group(pgid, s)

    snap = scan()
    if proc.poll() is None and proc.pid not in known:
        known[proc.pid] = os.getpgrp()        # 표를 못 읽었어도 자식 자신에게는 보낸다
    blast(sig, set(known))
    left: set[int] = set(known)
    for grace, final in ((term_grace_s, False), (kill_grace_s, True)):
        deadline = time.monotonic() + grace
        while True:
            snap = scan()
            left = alive(snap)
            if not left or time.monotonic() >= deadline:
                break
            time.sleep(STOP_POLL_S)
        if not left or final:
            break
        blast(signal.SIGKILL, left)
    try:
        proc.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass
    return left


def default_run_child(cmd: list[str], cwd: Path, env: dict[str, str]) -> int:
    """자식 프로세스를 띄우고 그 stdout / stderr 를 **한 줄씩 비밀 값을 지워서** 우리 stdout(파드 로그)으로 흘린 뒤 종료 코드를
    돌려준다. 지울 값은 ``env`` 의 비밀 변수 값(``contract.secret_values``)과 그 표기들, URL 사용자 정보. 지우다 실패한 줄은
    원문 대신 고정 문구가 나간다.

    Run the child, streaming its stdout / stderr to ours (the pod log) with every line redacted first (the secret
    values of `env`, their spellings, URL userinfo; a line that cannot be redacted is withheld); returns its exit code."""
    redactor = Redactor(lambda: contract.secret_values(env))
    # 파드 로그는 UTF-8 로 읽힌다 — 자식이 로케일 인코딩으로 쓰다 `—` 같은 글자에서 죽지 않게(localjob 의 2026-11-05 교훈)
    child_env = {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", **env}
    proc = subprocess.Popen(cmd, cwd=str(cwd), env=child_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert proc.stdout is not None
    out = getattr(sys.stdout, "buffer", None)
    try:
        for chunk in iter(lambda: proc.stdout.readline(), b""):
            chunk = redactor.line(chunk)
            if out is not None:
                out.write(chunk)
                out.flush()
            else:
                sys.stdout.write(chunk.decode("utf-8", errors="replace"))
                sys.stdout.flush()
    except BaseException as e:
        # SIGTERM / Ctrl-C(WorkerTerminated) 나 그 밖의 실패: 상태 파일을 쓰기 전에 Maven 까지 트리째 끝낸다. 받은 신호를 그대로
        # 넘겨 자식이 스스로 정리할 기회를 먼저 준다. 파이프는 더 읽지 않는다 — 끝나는 프로세스들이 막혀도 SIGKILL 이 끝낸다
        sig = e.signum if isinstance(e, WorkerTerminated) else signal.SIGTERM
        left = stop_process_tree(proc, sig)
        if left:
            _say(f"WARNING: processes still alive after SIGKILL: {sorted(left)}")
        try:
            proc.stdout.close()
        except OSError:
            pass
        raise
    return proc.wait()


def resolve_configs(environ: dict[str, str], record: contract.RequestRecord, repo_root: Path) -> list[str]:
    """쓸 설정 파일들: ``SBMIG_CONFIG``(공백으로 구분한 경로 또는 이름)가 있으면 그것, 없으면 ``request.json`` 의 ``configs`` 를
    ``<repo root>/config/<name>`` 으로. 경로 구분자가 없는 이름은 ``config/`` 아래로 본다(Main 이 이름으로 고정하므로).

    The config files to pass: `SBMIG_CONFIG` (paths or bare names) else the request's configs under `<repo root>/config/`."""
    raw = (environ.get("SBMIG_CONFIG") or "").split() or list(record.configs or [])
    out: list[str] = []
    for c in raw:
        if "/" in c or "\\" in c:
            out.append(c)
        else:
            out.append(str(repo_root / "config" / c))
    return out


def write_paths_yml(run_dir: Path, environ: dict[str, str]) -> Path:
    """``<run dir>/worker-paths.yml``: ``SBMIG_KB_DIR`` 이 있으면 ``kb.dir``, LLM 원문 기록을 켜지 않았으면 ``llm.record_dir: ""`` —
    원문 기록에는 프로젝트 소스가 들어 있어 공유 볼륨에 더 남기지 않는 것이 배포팀의 기본값이다. 마지막 ``--config`` 로 넘긴다.

    Write the Worker's override config (KB dir; raw LLM records off unless `SBMIG_WORKER_LLM_RECORDS=1`)."""
    lines: list[str] = []
    kb = (environ.get("SBMIG_KB_DIR") or "").strip()
    if kb:
        lines += ["kb:", f"  dir: {json.dumps(kb)}"]        # JSON string = valid YAML scalar, safe for Windows paths / colons
    if (environ.get(contract.LLM_RECORDS_ENV) or "").strip() != "1":
        lines += ["llm:", '  record_dir: ""']
    p = Path(run_dir) / PATHS_FILE
    p.write_text("\n".join(lines) + "\n" if lines else "{}\n", encoding="utf-8")
    return p


def child_environ(environ: dict[str, str]) -> dict[str, str]:
    """자식 ``sbmig run`` 의 환경. LLM 원문 기록을 켜지 않았으면(``SBMIG_WORKER_LLM_RECORDS`` ≠ 1) 기록 폴더를 정하는 변수
    (``LLM_RECORD_DIR``, 설정 덮어쓰기 ``SBMIG_LLM__RECORD_DIR``)를 뺀다 — 환경 변수는 yml 보다 우선하므로 ``worker-paths.yml`` 의
    ``llm.record_dir: ""`` 만으로는 덮어쓰기를 못 끈다. 켰으면 그대로 넘긴다(회사 yml 이 이미지에 구워져 있으면 그 덮어쓰기가 기록 폴더를 정한다).
    그리고 언제나 ``SBMIG_REDACT_OUTPUT=1``(자식이 로그·저널을 쓰는 순간 지운다 — Worker 아래에서는 끌 수 없다)과, 없으면
    넓은 ``COLUMNS`` 를 넣는다.

    The child's environment: without `SBMIG_WORKER_LLM_RECORDS=1` the variables that choose the raw-record directory are dropped
    (an env override beats the paths yml). Always adds `SBMIG_REDACT_OUTPUT=1` (the child redacts its log / journal lines as
    it writes them; not optional under the Worker) and a wide `COLUMNS` when none is set."""
    if (environ.get(contract.LLM_RECORDS_ENV) or "").strip() == "1":
        out = dict(environ)
    else:
        out = {k: v for k, v in environ.items() if k.upper() not in RECORD_DIR_ENV}
    out[scrub.REDACT_OUTPUT_ENV] = "1"
    if not (out.get("COLUMNS") or "").strip():
        out["COLUMNS"] = CHILD_COLUMNS
    return out


def slot_number(environ: dict[str, str]) -> int | None:
    """``SBMIG_JOB_SLOT`` 의 자리 번호(1 부터). 없거나 비었으면 None(자리 개념 없음 — 예전 Main, ``sbmig run`` 직접 실행). 양의 정수가
    아니면 ``ValueError`` — 부르는 쪽이 "자리 없음"으로 다루고 그렇게 적는다.

    The 1-based slot number from `SBMIG_JOB_SLOT`; None when unset or blank; `ValueError` when it is not a positive integer."""
    raw = (environ.get(contract.SLOT_ENV) or "").strip()
    if not raw:
        return None
    n = int(raw)                     # ValueError for "abc", "2.0"
    if n < 1:
        raise ValueError(f"{contract.SLOT_ENV} must be a positive integer")
    return n


def slot_local_repo(base: Path, slot: int) -> Path:
    """자리 ``slot`` 의 로컬 저장소: ``base`` 옆의 ``<base 이름>-slot<k>``(예: ``/home/sbmig/.m2/repository-slot2``) — 같은 PVC
    안에 남아 다음 실행이 다시 받지 않는다.

    Slot `slot`'s local repository: the sibling `<base name>-slot<k>` of `base`."""
    return Path(base).parent / f"{Path(base).name}-slot{slot}"


def default_repo_resolver(configs: list[str], paths_yml: Path) -> Path:
    """자리가 없을 때 자식이 쓸 로컬 저장소를 자식과 같은 설정 파일들(같은 순서, ``worker-paths.yml`` 이 마지막)로 계산한다:
    ``build.local_repo`` → settings.xml 의 ``<localRepository>`` → ``~/.m2/repository``(``sbmig.build.mvn.local_repo_path``).
    ``load_config`` 는 덮어쓰기를 ``os.environ`` 에서 읽는다 — 파드에서는 Worker 의 env 와 같다(테스트는 다른 resolver 를 넣는다).

    The repository the child would use without a slot, from the same config files in the same order (the Worker's paths
    file last): `build.local_repo` → settings.xml `<localRepository>` → `~/.m2/repository`. `load_config` reads its
    overrides from `os.environ` (the Worker's own environment in the pod)."""
    # 늦게 import: sbmig.build 는 Maven 실행기 전체를 끌고 온다 — 자리 2 부터만 필요하다
    from sbmig.build.mvn import local_repo_path
    from sbmig.core.config import load_config
    return local_repo_path(load_config(*configs, paths_yml))


def base_local_repo(environ: dict[str, str], configs: list[str], paths_yml: Path,
                    resolver: RepoResolver | None = None) -> Path:
    """이 실행이 자리 없이 쓸 로컬 저장소(자리 저장소의 기준): Worker 자신의 ``SBMIG_BUILD__LOCAL_REPO`` 가 yaml 로 읽어 비지 않은
    문자열이면 그것(Job 틀의 리터럴 ``"null"`` 은 "정하지 않음"), 아니면 ``resolver``(기본 ``default_repo_resolver``). 예외는 그대로
    올린다 — 부르는 쪽이 공유 저장소로 물러선다.

    The repository the run would use without a slot: the Worker's own `SBMIG_BUILD__LOCAL_REPO` when it loads as a
    non-empty string (the template's literal "null" means unset), else `resolver`. Raises what the resolver raises."""
    raw = environ.get(LOCAL_REPO_ENV)
    if raw is not None:
        try:
            val = yaml.safe_load(raw)
        except yaml.YAMLError:
            val = None               # 자식의 load_config 도 이 값에서 깨진다 — resolver 가 같은 이유로 실패해 공유 저장소로 간다
        # ${VAR} 자리표시자는 자식의 load_config 가 펼친다 — 펼치지 않은 글자를 경로로 쓰지 않고 resolver(같은 load_config)에 맡긴다
        if isinstance(val, str) and val.strip() and "${" not in val:
            return Path(val).expanduser()
    return Path((resolver or default_repo_resolver)(list(configs), Path(paths_yml)))


def yaml_env_value(text: str) -> str:
    """``SBMIG_<섹션>__<키>`` 로 넘길 값: 자식의 ``load_config`` 는 값을 ``yaml.safe_load`` 로 읽으므로, 그대로 읽어 같은 문자열이면
    그대로, 아니면(``: `` · `` #`` · 예약 기호로 시작 …) JSON 따옴표 문자열(YAML 이 같은 문자열로 읽는다). 어느 쪽도 되돌아오지
    않으면 ``ValueError``.

    The value to pass as an `SBMIG_<SECTION>__<KEY>` override so that the child's `yaml.safe_load` returns `text`: as
    is when it round-trips, else JSON-quoted; `ValueError` when neither form does."""
    for candidate in (text, json.dumps(text, ensure_ascii=False)):
        try:
            if yaml.safe_load(candidate) == text:
                return candidate
        except yaml.YAMLError:
            continue
    raise ValueError("the path does not survive the YAML reading of an env override")


def pod_local_repo(slot: int, root: Path | str | None = None) -> Path:
    """PVC 에 자리 저장소를 못 만들 때 쓰는 파드 안의 저장소: ``<임시 폴더>/sbmig-m2-slot<k>``(``root`` 가 없으면
    ``tempfile.gettempdir()``). 파드가 끝나면 사라진다 — 매번 의존성을 다시 받지만 다른 파드와 나누지 않는다.

    The pod-local fallback repository `<temp dir>/sbmig-m2-slot<k>`: cold on every run, never shared with another pod."""
    return Path(root if root is not None else tempfile.gettempdir()) / POD_LOCAL_REPO_NAME.format(slot=slot)


def prepare_repo_dir(path: Path) -> tuple[str | None, str | None]:
    """``path`` 를 Maven 로컬 저장소로 쓸 수 있게 한다: 자식에게 넘길 값(``yaml_env_value``)을 만들고, 폴더를 만들고, 파일 하나를
    만들었다 지워 **실제로 쓸 수 있는지** 본다(NFS 의 루트는 만들기는 되는데 쓰기가 안 되는 일이 있다). ``(값, None)`` 또는
    ``(None, 이유)``. 예외를 내지 않는다.

    Make `path` usable as a local repository: the env value, the directory, and a real write probe (create + remove a
    file). Returns `(value, None)` or `(None, reason)`; never raises."""
    try:
        value = yaml_env_value(str(path))
    except ValueError as e:
        return None, f"{path}: {e}"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return None, f"cannot create {path} ({e.strerror or type(e).__name__})"
    probe = path / WRITE_PROBE.format(pid=os.getpid())
    try:
        with open(probe, "x", encoding="utf-8") as fh:
            fh.write("probe\n")
        probe.unlink()
    except OSError as e:
        return None, f"cannot write to {path} ({e.strerror or type(e).__name__})"
    return value, None


def apply_slot_repo(child_env: dict[str, str], environ: dict[str, str], configs: list[str], paths_yml: Path, *,
                    cwd: Path, resolver: RepoResolver | None = None, local_root: Path | str | None = None) -> dict | None:
    """자리 규칙을 자식의 env(``child_env``, 그 자리에서 고친다)에 적용하고 저널 ``worker_slot`` 의 데이터를 돌려준다.
    ``SBMIG_JOB_SLOT`` 이 없으면 None(아무것도 하지 않고 적지도 않는다). 1번 자리 → 그대로 ``{slot: 1, local_repo: None, shared:
    False}``. k ≥ 2 → (1) 기준 저장소(``base_local_repo``; 상대 경로면 자식의 작업 폴더 ``cwd`` 기준) 옆의 ``-slot<k>`` 폴더를 만들고
    쓰기를 확인해 ``SBMIG_BUILD__LOCAL_REPO`` 를 넣는다 → ``{slot: k, local_repo: <경로>, shared: False}``. (2) 기준을 못 정했거나 그
    폴더를 만들거나 쓸 수 없으면 파드 안의 저장소(``pod_local_repo``; ``local_root`` 는 테스트용) → ``{…, local_repo: <경로>, shared:
    False, pod_local: True, note: <이유>}`` — 느리지만 다른 파드와 나누지 않는다. (3) 그것마저 안 되면, 또는 번호가 틀렸으면 env 는
    그대로(공유 저장소) → ``{…, local_repo: None, shared: True, note: <이유>}``. 예외를 내지 않는다 — 이것 때문에 실행이 실패하는 일은
    없다. Worker 의 env(``environ``)는 건드리지 않는다(환경 대조는 파드 자신의 env 를 본다).

    Apply the slot rule to the child's environment (`child_env`, changed in place) and return the `worker_slot` journal
    data; None when `SBMIG_JOB_SLOT` is unset. Slot 1 = unchanged; slot k ≥ 2 = (1) the sibling `<base>-slot<k>`, created
    and write-probed, passed as `SBMIG_BUILD__LOCAL_REPO`; (2) when the base cannot be resolved or the sibling cannot be
    created / written, a pod-local repository under the temp directory (`pod_local: true` + note; cold, never shared);
    (3) only when that fails too (or the slot number is bad) the shared repository with a note. Never raises; `environ`
    is never changed."""
    raw = (environ.get(contract.SLOT_ENV) or "").strip()
    if not raw:
        return None
    try:
        slot = slot_number(environ)
    except ValueError:
        return {"slot": None, "local_repo": None, "shared": True,
                "note": f"{contract.SLOT_ENV}={raw[:40]!r} is not a positive integer — treated as no slot"}
    if slot == 1:
        return {"slot": 1, "local_repo": None, "shared": False}

    def shared(note: str) -> dict:
        return {"slot": slot, "local_repo": None, "shared": True, "note": note}

    try:
        why: str | None = None
        try:
            base = base_local_repo(environ, configs, paths_yml, resolver)
        except Exception as e:  # noqa: BLE001 — 기준을 못 정해도 실행은 간다(아래의 파드 안 저장소로)
            # 형식 이름만: 설정 검증 오류의 글은 값을 담을 수 있다
            why = f"the base Maven local repository could not be resolved ({type(e).__name__})"
        else:
            if not base.is_absolute():
                base = Path(cwd) / base      # 자식은 cwd 에서 돌고 거기서 상대 경로를 푼다(mvn.base_command 의 absolute())
            path = slot_local_repo(base, slot)
            value, why = prepare_repo_dir(path)
            if value is not None:
                child_env[LOCAL_REPO_ENV] = value
                return {"slot": slot, "local_repo": str(path), "shared": False}
        # 공유 저장소로 물러서면 세 파드의 Maven 이 한 저장소에 쓴다 — 그 전에 파드 안의 저장소(느리지만 나누지 않는다)
        local = pod_local_repo(slot, local_root)
        value, why_local = prepare_repo_dir(local)
        if value is not None:
            child_env[LOCAL_REPO_ENV] = value
            return {"slot": slot, "local_repo": str(local), "shared": False, POD_LOCAL_KEY: True,
                    "note": f"pod-local repository: {why}"}
        return shared(f"{why}; pod-local fallback: {why_local}")
    except Exception as e:  # noqa: BLE001 — 어떤 예외도 실행을 막지 않는다
        return shared(f"the slot repository could not be set up ({type(e).__name__})")


def slot_message(data: dict) -> str:
    """``apply_slot_repo`` 결과를 파드 로그 한 줄로. / One pod-log line for an `apply_slot_repo` result."""
    if data.get("shared"):
        where = f"job slot {data['slot']}: " if data.get("slot") else ""
        return f"WARNING: {where}{data.get('note')}; the run uses the shared Maven local repository"
    if data.get(POD_LOCAL_KEY):
        uid = f" (uid {os.getuid()})" if hasattr(os, "getuid") else ""
        return (f"WARNING: job slot {data['slot']}: {data.get('note')}; the run uses the pod-local Maven local repository "
                f"{data['local_repo']} and downloads all its dependencies again (nothing is kept after the pod, nothing is "
                f"shared with another pod) — to keep one repository per slot between runs, let the pod's user{uid} create "
                f"and write the '<repository>-slot{data['slot']}' folder beside the Maven local repository on the Maven "
                f"volume (PVC sbmig-m2: ownership / fsGroup of its folder), or set build.local_repo to a writable path there")
    if data.get("local_repo"):
        return f"job slot {data['slot']}: own Maven local repository {data['local_repo']}"
    return f"job slot {data.get('slot')}: the Maven local repository is unchanged"


def build_command(python: str, zip_path: str, configs: list[str], paths_yml: Path, out: str, run_id: str,
                  stop_after: str | None, llm_provider: str | None) -> list[str]:
    """자식 명령: ``python -m sbmig.cli run <zip> --config … --config <worker-paths.yml> --out <out> --run-id <id>
    [--stop-after X] [--llm-provider Y]``.

    The child command line (run-job.sh's shape, the Worker's paths file as the last `--config`)."""
    cmd = [python, "-m", "sbmig.cli", "run", zip_path]
    for c in configs:
        cmd += ["--config", c]
    cmd += ["--config", str(paths_yml), "--out", out, "--run-id", run_id]
    if stop_after:
        cmd += ["--stop-after", stop_after]
    if llm_provider:
        cmd += ["--llm-provider", llm_provider]
    return cmd


def last_result_line(run_dir: Path) -> str | None:
    """``<run dir>/sbmig.log`` 의 마지막 ``SBMIG-RESULT`` 줄(운영자가 옮겨 적는 줄). / The log's last SBMIG-RESULT line."""
    try:
        text = (Path(run_dir) / "sbmig.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    hits = [ln for ln in text.splitlines() if RESULT_MARK in ln]
    return hits[-1].strip() if hits else None


@dataclass
class EnvCheck:
    """환경 대조 한 번의 결과(``check_environment``). ``policy`` 는 refuse | warn. 차이는 **이름만** 담는다: ``differs`` /
    ``main_only`` / ``worker_only``(``contract.compare_env_pins``), ``run_values``(``request.json`` 과 다른 실행별 변수 이름),
    ``problems``(사람이 읽을 문장 — 비면 통과), ``error``(대조 자체가 실패했을 때 그 예외의 형식 이름 — 메시지는 값을 담을 수 있어 뺀다).

    One environment check: the policy, the differing names per category, the per-run variables that differ from
    `request.json`, the problem sentences (empty = ok) and the exception type when the check itself failed. Names only."""
    policy: str
    differs: list[str] = field(default_factory=list)
    main_only: list[str] = field(default_factory=list)
    worker_only: list[str] = field(default_factory=list)
    run_values: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    error: str | None = None

    def reason(self) -> str:
        """문장들을 ``; `` 로 잇고 고정 안내를 붙인 글(거절 사유 / 경고 줄). / The problems joined, plus the fixed hint."""
        return "; ".join(self.problems) + " — " + ENV_PIN_HINT

    def journal_data(self, run_id: str, clean: Callable[[str], str]) -> dict:
        """``worker_env_differs`` 저널 줄의 데이터 — 이름 목록만. / The journal data of `worker_env_differs` (names only)."""
        data: dict = {"run_id": run_id, "policy": self.policy}
        for key in ("differs", "main_only", "worker_only", "run_values"):
            data[key] = [clean(n) for n in getattr(self, key)]
        if self.error:
            data["check_error"] = self.error
        return data

    def _failed(self, exc: BaseException) -> None:
        # 대조가 깨져도 Worker 는 죽지 않는다: 같은 정책 아래의 문제 하나로 센다(refuse 는 닫힌 채로, warn 은 계속)
        self.error = self.error or type(exc).__name__
        self.problems.append(f"the environment check failed: {type(exc).__name__}")


def _real_path(path: str | Path, base: Path) -> str:
    p = Path(path)
    if not p.is_absolute():
        p = Path(base) / p          # 자식은 repo root 에서 돈다(cwd) — 상대 경로는 거기서 풀린다
    return os.path.normcase(os.path.realpath(p))


def _run_value_problems(record: contract.RequestRecord, environ: dict[str, str],
                        repo_root: Path) -> list[tuple[str, str]]:
    # request.json 의 digest 는 요청의 설정 이름으로 계산했다 — SBMIG_CONFIG 가 다른 파일을 가리키면 검증 안 된 파일로 돈다
    out: list[tuple[str, str]] = []
    names = [str(c) for c in (record.configs or [])]
    want = [_real_path(Path(repo_root) / "config" / c, repo_root) for c in names]
    got = [_real_path(c, repo_root) for c in resolve_configs(environ, record, repo_root)]
    if got != want:
        given = " ".join((environ.get("SBMIG_CONFIG") or "").split())
        sentence = (f"SBMIG_CONFIG names other config files ({given}) than request.json ({', '.join(names) or 'none'})"
                    " — the pinned digests cover the request's files only")
        out.append(("SBMIG_CONFIG", sentence))
    for name, attr in RUN_VALUE_FIELDS:
        if (environ.get(name) or None) != (getattr(record, attr, None) or None):     # 비었거나 없으면 None
            out.append((name, f"{name} differs from request.json's {attr}"))
    return out


def check_environment(record: contract.RequestRecord, state: contract.ControlState | None, environ: dict[str, str],
                      repo_root: Path) -> EnvCheck | None:
    """Job 을 만든 Main 의 환경(``state.env_pins`` — 리뷰 2026-11-10)과 이 Worker 의 환경을 대조한다. state 가 없거나(옛 Main,
    local-job) 지문이 없거나 정책이 off 면 None — 대조하지 않는다. 지문은 있는데 정책이 비었으면 refuse 로 본다. 대조하면 두 가지를
    본다: 지문의 차이(``contract.env_pins`` 를 이 환경으로 다시 계산해 ``compare_env_pins``)와, 실행별 값(``SBMIG_CONFIG`` 가 가리키는
    파일 · ``SBMIG_STOP_AFTER`` · ``SBMIG_LLM_PROVIDER``)이 ``request.json`` 과 같은지. 예외를 내지 않는다: 대조 중의 예외는 문제
    하나(``the environment check failed: <형식>``)가 된다. 결과에는 이름만 있다(값·지문 없음).

    Compare this Worker's environment with the pins the Main that created the Job wrote (`state.env_pins`). None = no
    check (no state, no pins, policy off); pins without a policy = refuse. Checks the pin diff and the per-run values
    against `request.json`; never raises (a failure inside becomes a problem under the same policy). Names only."""
    if state is None or state.env_pins is None:
        return None
    try:
        policy = contract.env_pin_policy(state.env_pin_policy, default=contract.ENV_PIN_REFUSE)
    except Exception:  # noqa: BLE001 — 읽을 수 없는 정책 값은 가장 엄한 쪽으로
        policy = contract.ENV_PIN_REFUSE
    if policy == contract.ENV_PIN_OFF:
        return None
    check = EnvCheck(policy=policy)
    try:
        mine = contract.env_pins(repo_root, [str(c) for c in (record.configs or [])], environ)
        diff = contract.compare_env_pins(state.env_pins, mine)
        for key, _ in ENV_DIFF_LABELS:
            setattr(check, key, [str(n) for n in diff.get(key) or []])
        parts = [f"{label}: {', '.join(getattr(check, key))}" for key, label in ENV_DIFF_LABELS if getattr(check, key)]
        if parts:
            check.problems.append("environment differs from the web pod's: " + "; ".join(parts))
    except Exception as e:  # noqa: BLE001 — the check must never crash the Worker
        check._failed(e)
    try:
        for name, sentence in _run_value_problems(record, environ, repo_root):
            check.run_values.append(name)
            check.problems.append(sentence)
    except Exception as e:  # noqa: BLE001
        check._failed(e)
    return check


def _env_check(run_dir: Path, record: contract.RequestRecord, environ: dict[str, str],
               repo_root: Path) -> EnvCheck | None:
    # Worker 는 control/ 을 읽기만 한다. 읽다가 깨지면(있을 수 없는 일이지만) 정책을 모르니 닫힌 쪽(refuse)으로
    try:
        state = contract.ControlState.read(run_dir)
    except Exception as e:  # noqa: BLE001
        check = EnvCheck(policy=contract.ENV_PIN_REFUSE)
        check._failed(e)
        return check
    return check_environment(record, state, environ, repo_root)


def _refuse(run_dir: Path, run_id: str, reason: str, record: contract.RequestRecord | None) -> int:
    _say(f"REFUSED: {reason}")
    _journal(run_dir, "worker_refused", {"run_id": run_id, "reason": reason})
    contract.JobStatus(exit_code=3, run_id=run_id, attempt=record.attempt if record else None, error=reason,
                       sbmig_version=contract.sbmig_version()).write(run_dir)
    return 3


def terminated_error(signum: int) -> str:
    """종료 신호로 끊긴 실행의 오류 글(``job-status.json`` 의 ``error``, 저널 ``worker_finished`` 의 ``error``).

    The error text of a run cut short by a stop signal."""
    return (f"the Worker was terminated by {signal_name(signum)} (pod deleted, Job deadline or stop) before sbmig finished "
            "— sbmig and its Maven processes were stopped; this run has no result")


def _finish_terminated(run_dir: Path, run_id: str, record: contract.RequestRecord, version: str, env: dict[str, str],
                       signum: int) -> int:
    # 진짜 결과처럼 보이면 안 된다: exit 3(Main 이 아는 "중단") + 오류 글. 정제를 먼저 — Main 은 받은 상태 파일을 믿고 자기 정제를 건너뛴다
    error = terminated_error(signum)
    # 2026-11-11: 이관 뒤에 취약점 조치가 이어 돌면 이관은 이미 끝났는데 조치 중에(Job 기한 등) 끊길 수 있다 — 운영자가 이관의 결과
    # 줄(SBMIG-RESULT)과 보고서는 남아 있다는 것을 알게 그 줄을 오류 글에 붙인다(종료 코드는 그대로 3: 자식이 끝을 맺지 못했다)
    finished = last_result_line(run_dir)
    if finished:
        error += f" — the migration itself had finished before (its report is in the run dir): {finished}"
    _say(f"ERROR: {error}")
    redacted = redact_run_dir(run_dir, contract.secret_values(env))
    _journal(run_dir, "worker_finished", {"exit_code": TERMINATED_EXIT, "redacted_files": redacted, "error": error,
                                          "terminated": signal_name(signum)})
    contract.JobStatus(exit_code=TERMINATED_EXIT, run_id=run_id, attempt=record.attempt, error=error,
                       sbmig_version=version).write(run_dir)
    return TERMINATED_EXIT


def main(argv: list[str] | None = None, *, environ: dict[str, str] | None = None, run_child: RunChild | None = None,
         repo_resolver: RepoResolver | None = None, install_signals: InstallSignals | None = None) -> int:
    """Worker 진입점: 환경 변수로 받은 요청 하나를 검증하고 돌리고 결과 상태 파일을 쓴다. 종료 코드는 모듈 설명 참고.
    ``repo_resolver`` 는 자리 저장소의 기준을 정하는 함수(테스트용; None = ``default_repo_resolver``). ``install_signals`` 는
    SIGTERM / SIGINT 처리기를 거는 함수(테스트용; None = ``install_stop_signals``): 자식이 도는 동안 신호가 오면 자식 트리를 끝내고
    ``job-status.json`` 에 exit 3 + "terminated" 오류를 쓰고 3 으로 끝난다.

    The Worker entry point (`python -m sbmig.web.worker`); see the module docstring for the exit codes. `repo_resolver`
    finds the base of a slot's repository (tests; None = `default_repo_resolver`); `install_signals` installs the
    SIGTERM / SIGINT handler (tests; None = `install_stop_signals`): a signal while the child runs stops the child's tree,
    writes exit 3 + a "terminated" error to `job-status.json` and exits 3."""
    del argv                                           # no options: the Job template passes everything through env
    env = dict(os.environ if environ is None else environ)
    run_child = run_child or default_run_child
    missing = [k for k in REQUIRED_ENV if not (env.get(k) or "").strip()]
    if missing:
        print(f"[worker] ERROR: required environment variables missing: {', '.join(missing)}", file=sys.stderr)
        return 3
    zip_path, out, run_id = env["SBMIG_SOURCE_ZIP"], env["SBMIG_OUT"], env["SBMIG_RUN_ID"]
    run_dir = Path(out) / run_id
    repo_root = Path(env.get("SBMIG_APP_DIR") or PACKAGE_ROOT)

    clean = Redactor(lambda: contract.secret_values(env))      # Worker 자신이 쓰는 글(파드 로그 줄, 저널·상태 파일의 오류 글)용
    record = contract.RequestRecord.read(run_dir)
    if record is None:
        return _refuse(run_dir, run_id, REFUSE_NO_REQUEST, None)
    problems = contract.verify_request(record, repo_root, Path(zip_path), run_id)
    if problems:
        return _refuse(run_dir, run_id, "; ".join(problems), record)
    # 리뷰 2026-11-10: 파일이 같아도 env 가 다르면 다른 설정으로 돈다 — Job 을 만든 Main 의 환경 지문과 대조(이름만 적는다)
    check = _env_check(run_dir, record, env, repo_root)
    if check is not None and check.problems:
        reason = clean(check.reason())
        if check.policy != contract.ENV_PIN_WARN:          # refuse (그리고 알 수 없는 값) — 닫힌 쪽
            return _refuse(run_dir, run_id, reason, record)
        _say(f"WARNING: {reason}")
        _journal(run_dir, ENV_DIFFERS_EVENT, check.journal_data(run_id, clean))

    version = contract.sbmig_version()
    started = {"run_id": run_id, "attempt": record.attempt, "sbmig_version": version, "pid": os.getpid()}
    if check is not None:
        started["env_pin"] = check.policy
    _journal(run_dir, "worker_started", started)
    # 리뷰 2026-11-10: 파드의 PID 1 은 처리기가 없으면 SIGTERM 을 무시한다 — 자식이 도는 동안의 신호는 자식 트리를 끝내고 기록을 남긴다
    stops = StopSignals()
    restore = (install_signals or install_stop_signals)(stops.handle)
    try:
        return _run(env, record, run_dir, run_id, zip_path, out, repo_root, version, clean, run_child, repo_resolver, stops)
    finally:
        restore()


def _run(env: dict[str, str], record: contract.RequestRecord, run_dir: Path, run_id: str, zip_path: str, out: str,
         repo_root: Path, version: str, clean: Redactor, run_child: RunChild, repo_resolver: RepoResolver | None,
         stops: StopSignals) -> int:
    # from here on a status file must exist whatever happens (Main waits for it)
    rc: int | None = None
    try:
        paths_yml = write_paths_yml(run_dir, env)
        configs = resolve_configs(env, record, repo_root)
        cmd = build_command(env.get("SBMIG_PYTHON") or sys.executable, zip_path, configs,
                            paths_yml, out, run_id, env.get("SBMIG_STOP_AFTER") or None,
                            env.get("SBMIG_LLM_PROVIDER") or None)
        child_env = child_environ(env)
        # v2.1z-ae: 2번 자리부터 자기 Maven 로컬 저장소 — 환경 대조가 끝난 뒤, 자식의 env 에만(대조는 파드 자신의 env 를 본다)
        slot = apply_slot_repo(child_env, env, configs, paths_yml, cwd=repo_root, resolver=repo_resolver)
        if slot is not None:
            _say(clean(slot_message(slot)))
            _journal(run_dir, SLOT_EVENT, {k: clean(v) if isinstance(v, str) else v for k, v in slot.items()})
        _say(clean("sbmig run " + " ".join(cmd[4:])))
        stops.arm()                                   # 이미 신호가 왔으면 자식을 띄우지 않는다
        try:
            rc = int(run_child(cmd, repo_root, child_env))
        finally:
            stops.disarm()                            # 자식이 끝난 뒤의 신호는 기억만 — 정제·상태 파일 쓰기를 끊지 않는다
    except WorkerTerminated as t:
        return _finish_terminated(run_dir, run_id, record, version, env, t.signum)
    except (Exception, KeyboardInterrupt) as e:  # noqa: BLE001 — whatever broke, Main must still get a status file
        error = clean(f"the Worker could not run sbmig: {type(e).__name__}: {e}")
        _say(f"ERROR: {error}")
        redacted = redact_run_dir(run_dir, contract.secret_values(env))
        _journal(run_dir, "worker_finished", {"exit_code": 3, "redacted_files": redacted, "error": error})
        contract.JobStatus(exit_code=3, run_id=run_id, attempt=record.attempt, error=error, sbmig_version=version).write(run_dir)
        return 3

    _say(f"sbmig exit {rc} — run dir {run_dir}")
    redacted = redact_run_dir(run_dir, contract.secret_values(env))
    line = last_result_line(run_dir)
    if line:
        print(line, flush=True)
    _journal(run_dir, "worker_finished", {"exit_code": rc, "redacted_files": redacted})
    contract.JobStatus(exit_code=rc, run_id=run_id, attempt=record.attempt, sbmig_version=version).write(run_dir)
    if rc == 3:
        return 3
    if rc == 2 and (env.get("SBMIG_FAIL_JOB_ON_BUILD_FAIL") or "").strip() == "1":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
