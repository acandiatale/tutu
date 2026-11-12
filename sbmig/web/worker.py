"""빌드 파드(Job) 안에서 이관 요청 하나를 처음부터 끝까지 처리하는 Worker 모듈.
``python -m sbmig.web.worker``로 실행하고, 환경 변수 ``SBMIG_SOURCE_ZIP`` / ``SBMIG_OUT`` /
``SBMIG_RUN_ID``가 반드시 있어야 한다.
처리 순서: ``control/request.json`` 확인 -> 환경 변수 확인 -> ``worker-paths.yml`` 작성 -> 자식 프로세스로
``sbmig.cli run`` 실행 -> 실행 폴더의 비밀 값 마스킹 -> ``SBMIG-RESULT`` 출력 -> ``job-status.json`` 기록.
- ``control/`` 폴더는 웹 파드(Main)가 관리하므로 Worker는 읽기만 한다
- 비밀 값은 기록하는 시점에 마스킹한다. 자식은 항상 ``SBMIG_REDACT_OUTPUT=1``로 실행하고, Worker는 자식의
  출력을 줄마다 마스킹해서 파드 로그로 넘긴다
- Main이 ``control/state.json``에 남긴 ``env_pins``와 지금 환경을 비교한다.
  정책이 refuse면 실행을 거절하고(exit 3), warn이면 저널에 ``worker_env_differs``를 남기고 계속한다.
  기록에는 변수 이름만 남긴다
- ``SBMIG_JOB_SLOT``이 2 이상이면 슬롯 전용 Maven 로컬 저장소 ``<이름>-slot<k>``를
  ``SBMIG_BUILD__LOCAL_REPO``로 자식에게만 넘긴다. 만들 수 없으면 파드 안 임시 저장소를, 그것도 안 되면 공유
  저장소를 쓰고 저널 ``worker_slot``에 남긴다
- SIGTERM / SIGINT를 받으면 자식 프로세스 트리 전체를 종료하고 ``job-status.json``에 exit 3과 종료 사유를 기록한다
- 종료 코드: 3은 중단(거절, 종료 신호, sbmig exit 3, 자식 실행 실패), 2는 빌드 실패이면서
  ``SBMIG_FAIL_JOB_ON_BUILD_FAIL=1``일 때, 그 밖에는 0
- 이관이 BUILD SUCCESS면 자식이 취약점 조치까지 이어서 실행한다.
  마지막 마스킹은 ``remediation/``의 텍스트 파일도 대상으로 하고 ``IVAS_USER_ID``도 비밀 값으로 본다
- ``SBMIG_WORKER_ACTION=remediation-resume``이면 ``control/remediation-resume.json``을 확인한 뒤 자식으로
  ``remediation-resume``을 같은 규칙으로 실행하고 결과를 ``remediation-job-status.json``에 기록한다. 이관의
  결과 파일은 건드리지 않는다

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
the error text. Remediation resume (2026-11-11, wave 4): with `SBMIG_WORKER_ACTION=remediation-resume` the Worker runs a NEW
attempt that continues the chained remediation: it checks `control/remediation-resume.json` (written by Main; refused when
missing or for another action), the config digests / sbmig version / env pins of `request.json` (not the migration zip's
hash), runs `python -m sbmig.cli remediation-resume --run-dir <run dir>` (`chain.resume`) under the same rules (slot
repository, redacted pipe, SIGTERM, final redaction) and writes `remediation-job-status.json` — the migration's
`job-status.json`, reports and result line are never touched."""
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
RECORD_DIR_ENV = ("LLM_RECORD_DIR", "SBMIG_LLM__RECORD_DIR")    # LLM 원문 기록 폴더를 정하는 변수. 기록을 켜지 않으면 자식에게 넘기지 않는다
JOURNAL_FILE = "journal.jsonl"
RESULT_MARK = "SBMIG-RESULT"
REFUSE_NO_REQUEST = "control/request.json missing — the Worker runs only requests Main accepted"
REFUSE_NO_RESUME = (f"control/{contract.RESUME_STATE_FILE} missing or not for {contract.ACTION_REMEDIATION_RESUME} — the Worker "
                    "resumes a remediation only when Main recorded that attempt")
REFUSE_NO_CHAIN = "no chained remediation in this run directory (remediation/state.json missing)"
REMEDIATION_MARK = "SBMIG-REMEDIATION"       # 취약점 조치 재개를 실행한 자식이 마지막에 출력하는 결과 줄의 표시
# COLUMNS가 없을 때 자식 콘솔의 줄 폭
# 파이프로 출력하면 rich가 80칸에서 줄을 접고, 접힌 비밀 값은 줄 단위 마스킹이 놓치므로 넓게 잡는다
CHILD_COLUMNS = "200"
# ── 환경 확인 ──
ENV_DIFFERS_EVENT = "worker_env_differs"
# 환경 차이의 종류(contract.compare_env_pins의 키)와 메시지에 쓰는 이름. Main은 웹 파드, Worker는 Job 파드다
ENV_DIFF_LABELS = (("differs", "values differ"), ("main_only", "set in the web pod only"),
                   ("worker_only", "set in the Job pod only"))
# request.json의 실행별 값에 대응하는 환경 변수 이름. SBMIG_CONFIG는 파일 경로로 따로 비교한다
RUN_VALUE_FIELDS = (("SBMIG_STOP_AFTER", "stop_after"), ("SBMIG_LLM_PROVIDER", "llm_provider"), (contract.FLOW_ENV, "flow"))
# 값이 비어 있으면 기본값으로 보고 비교한다. SBMIG_FLOW나 flow 키가 없으면 full이다
RUN_VALUE_DEFAULTS = {contract.FLOW_ENV: contract.FLOW_FULL}
# 거절이나 경고 메시지 끝에 붙이는 운영자 안내.
# 네임스페이스와 디플로이먼트 이름은 설치마다 다르므로 넣지 않는다
ENV_PIN_HINT = ("after changing ConfigMap sbmig-env or the Job template, restart the web pod and re-create ConfigMap "
                f"sbmig-job-template; {contract.ENV_PIN_POLICY_ENV}=warn (or off) on the web pod relaxes this check")

# ── 슬롯마다 Maven 로컬 저장소 하나 ──
# 여러 파드가 PVC 하나(~/.m2)를 같이 쓰는데 Maven은 여러 프로세스가 한 로컬 저장소에 동시에 쓰는 것을
# 보장하지 않으므로 슬롯마다 저장소를 나눈다
LOCAL_REPO_ENV = "SBMIG_BUILD__LOCAL_REPO"    # 자식의 build.local_repo를 덮어쓰는 설정. load_config가 값을 yaml로 읽는다
SLOT_EVENT = "worker_slot"
# PVC에 슬롯 저장소를 만들 수 없을 때 쓰는 대체 경로
# 매번 비어 있어 느리지만 다른 파드와 공유하지 않으므로, 공유 저장소는 이것도 안 될 때만 쓴다
POD_LOCAL_REPO_NAME = "sbmig-m2-slot{slot}"
POD_LOCAL_KEY = "pod_local"                   # 파드 안 저장소를 쓸 때만 true로 붙는 worker_slot 저널 키
WRITE_PROBE = ".sbmig-write-probe-{pid}"      # 쓰기 가능 여부를 확인하는 임시 파일. 만들고 바로 지운다

# ── 종료 신호 ──
# 컨테이너의 PID 1은 처리기가 없으면 SIGTERM을 무시하므로, 처리기를 등록하지 않으면 종료 유예 시간 동안
# 자식과 Maven이 계속 파일을 쓴다
STOP_SIGNALS = ("SIGTERM", "SIGINT")
# 자식 트리에 신호를 보낸 뒤와 SIGKILL 뒤에 기다리는 시간
# 상태 파일까지 기록하려면 합이 쿠버네티스 종료 유예(30초)와 local-job 대기(10초)보다 짧아야 한다
TERM_GRACE_S = 5.0
KILL_GRACE_S = 3.0
TERMINATED_EXIT = 3                            # EXIT_ABORTED와 같은 값이므로 Main은 중단으로 처리한다
STOP_POLL_S = 0.1

RunChild = Callable[[list[str], Path, dict[str, str]], int]
# 설정 파일 목록과 worker-paths.yml을 받아 슬롯이 없을 때 쓸 로컬 저장소를 반환하는 함수 형식
RepoResolver = Callable[[list[str], Path], Path]
# 신호 처리기를 등록하고 원래대로 되돌리는 함수를 반환하는 함수 형식.
# 테스트는 실제 신호 없이 처리기를 직접 호출한다
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
    """종료 신호를 받아 자식 실행을 중단할 때 던지는 예외.
    ``except Exception``에 잡히지 않도록 ``BaseException``을 상속한다.

    Raised (from the signal handler) to abandon the child run on SIGTERM / SIGINT; a BaseException so that no
    `except Exception` swallows it."""

    def __init__(self, signum: int):
        self.signum = int(signum)
        super().__init__(signal_name(self.signum))


def signal_name(signum: int) -> str:
    """신호 번호에 해당하는 이름(``SIGTERM``)을 반환하고, 모르는 번호면 ``signal <n>``을 반환한다.
    / The signal's name, else `signal <n>`."""
    try:
        return signal.Signals(signum).name
    except ValueError:
        return f"signal {signum}"


class StopSignals:
    """종료 신호를 받았는지 기억하는 클래스.
    자식이 실행되는 동안(``arm`` ~ ``disarm``)에만 ``WorkerTerminated``를 한 번 던지고, 그 밖에서는 신호를 기억만 한다.
    상태 파일을 기록하는 도중에 끊기면 Main이 결과를 받지 못하기 때문이다.

    The stop-signal state: the handler remembers the first signal and raises `WorkerTerminated` only while armed (the child
    is running) and only once; outside that window (checks, the final redaction, the status file) it just records."""

    def __init__(self) -> None:
        self.signum: int | None = None
        self.armed = False

    def handle(self, signum: int, frame: Any = None) -> None:
        """받은 종료 신호를 기록하고, 자식 실행 중이면 ``WorkerTerminated``를 던진다. / The signal handler."""
        del frame
        if self.signum is None:
            self.signum = int(signum)
        if self.armed:
            self.armed = False
            raise WorkerTerminated(signum)

    def arm(self) -> None:
        """자식을 실행하기 직전에 호출한다. 이미 신호가 와 있으면 바로 ``WorkerTerminated``를 던진다.
        / Arm; raise at once if a signal came."""
        if self.signum is not None:
            raise WorkerTerminated(self.signum)
        self.armed = True

    def disarm(self) -> None:
        """자식이 끝난 뒤 호출해 신호가 와도 예외를 던지지 않게 한다. / Disarm after the child ended."""
        self.armed = False


def install_stop_signals(handler: Callable[[int, Any], None]) -> Callable[[], None]:
    """``SIGTERM`` / ``SIGINT``에 ``handler``를 등록하고, 원래 처리기로 되돌리는 함수를 반환한다.
    주 스레드가 아니거나 플랫폼이 거부하면 등록하지 않으므로 Windows의 local-job에서도 문제없이 동작한다.

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


# ── 자식 프로세스 트리 종료 ──
# Maven은 별도 세션에서 실행되어 자식 그룹에만 신호를 보내면 살아남으므로, Worker의 자손 전체와 Worker에게
# 넘어온 고아 프로세스까지 찾아 함께 종료한다

def parse_proc_stat(raw: str) -> tuple[int, int, str]:
    """Linux ``/proc/<pid>/stat`` 한 줄을 읽어 ``(ppid, pgid, 상태)``를 반환한다.
    프로세스 이름에 괄호나 공백이 있을 수 있으므로 마지막 ``)`` 뒤부터 읽는다.
    형식이 맞지 않으면 ``ValueError`` / ``IndexError``를 던진다.

    Parse one `/proc/<pid>/stat` line into `(ppid, pgid, state)` (split after the last ')': comm may hold spaces / parens)."""
    rest = raw[raw.rindex(")") + 2:].split()
    return int(rest[1]), int(rest[2]), rest[0]


def process_table() -> dict[int, tuple[int, int, str]]:
    """현재 프로세스 목록을 ``{pid: (ppid, pgid, 상태)}`` 형태로 반환한다.
    Linux는 ``/proc``, 그 밖의 Unix는 ``ps``로 읽고, 읽지 못하면 빈 dict를 반환한다.

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
    ps_pid = ps.pid                     # ps 자신도 Worker의 자손으로 보이지만 종료할 대상이 아니므로 뺀다
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
    """``root``의 자손 pid를 모두 찾아 반환한다. ``root`` 자신은 포함하지 않는다.
    / Every descendant pid of `root` (not `root` itself)."""
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
    # 좀비(Z)는 이미 끝난 프로세스이고, pgid가 바뀌었으면 다른 프로세스가 번호를 재사용한 것이므로 건너뛴다
    hit = table.get(pid)
    return hit is not None and hit[1] == pgid and not hit[2].startswith("Z")


def _send(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _send_group(pgid: int, sig: int) -> None:
    # Worker 자신이나 시스템 전체에 신호를 보내지 않도록 Worker와 같은 그룹과 1 이하의 번호는 건너뛴다
    killpg = getattr(os, "killpg", None)
    if killpg is None or pgid <= 1 or pgid == os.getpgrp():
        return
    try:
        killpg(pgid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _reap(pids: set[int]) -> None:
    # Worker(PID 1)에게 넘어온 고아 프로세스는 Worker가 거둬야 좀비로 남지 않는다.
    # 우리 자식이 아닌 pid는 ChildProcessError가 난다
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
    """자식 ``proc``과 별도 세션에서 실행되는 Maven을 포함한 자손 프로세스 전체를 종료한다.
    자손 목록 확인 -> ``sig`` 전송 -> ``term_grace_s`` 대기 -> SIGKILL -> ``kill_grace_s`` 대기 순서로
    진행하고, Windows에서는 ``taskkill /T /F``를 쓴다.
    끝까지 남은 pid 집합을 반환하며(비어 있으면 모두 종료된 것이다) 예외를 던지지 않는다.

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
    known: dict[int, int] = {}                 # pid가 재사용됐는지 가려내기 위해 pid별로 처음 본 pgid를 기억한다

    def scan() -> dict[int, tuple[int, int, str]]:
        snap = table()
        for pid in descendants(me, snap) | ({proc.pid} if proc.pid in snap else set()):
            known.setdefault(pid, snap[pid][1])
        return snap

    def alive(snap: dict[int, tuple[int, int, str]]) -> set[int]:
        # 자식 자신은 Popen이 거두므로 프로세스 목록을 못 읽어도 자식의 종료 여부는 알 수 있다
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
        known[proc.pid] = os.getpgrp()        # 프로세스 목록을 못 읽어도 자식 자신에게는 신호를 보낸다
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
    """자식 프로세스를 실행하고 stdout / stderr를 한 줄씩 마스킹해 파드 로그로 내보낸 뒤 종료 코드를 반환한다.
    ``env``에 있는 비밀 값과 URL의 사용자 정보를 가리고, 마스킹에 실패한 줄은 정해진 문구로 대신 출력한다.

    Run the child, streaming its stdout / stderr to ours (the pod log) with every line redacted first (the secret
    values of `env`, their spellings, URL userinfo; a line that cannot be redacted is withheld); returns its exit code."""
    redactor = Redactor(lambda: contract.secret_values(env))
    # 파드 로그는 UTF-8로 읽는다.
    # 로케일 인코딩으로 두면 자식이 ASCII 밖의 문자를 출력하다 죽으므로 UTF-8로 고정한다
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
        # 종료 신호나 다른 실패가 나면 상태 파일을 기록하기 전에 Maven까지 트리째 종료한다.
        # 받은 신호를 그대로 보내 자식이 먼저 정리할 수 있게 한다
        # 파이프는 더 읽지 않고, 응답하지 않는 프로세스는 SIGKILL로 끝낸다
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
    """자식에게 넘길 설정 파일 목록을 만든다.
    ``SBMIG_CONFIG``가 있으면 그 값을 쓰고, 없으면 ``request.json``의 ``configs``를
    ``<repo root>/config/<name>`` 경로로 바꿔 쓴다. 경로 구분자가 없는 이름은 ``config/`` 아래 파일로 본다.

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
    """자식이 마지막 ``--config``로 읽을 ``<run dir>/worker-paths.yml``을 기록한다.
    ``SBMIG_KB_DIR``이 있으면 ``kb.dir``을 넣고, LLM 원문 기록을 켜지 않았으면 ``llm.record_dir: ""``를
    넣는다. 원문 기록에는 프로젝트 소스가 들어 있으므로 기본으로는 남기지 않는다.

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
    """자식 ``sbmig run``에 넘길 환경 변수를 만든다.
    - LLM 원문 기록을 켜지 않았으면(``SBMIG_WORKER_LLM_RECORDS`` ≠ 1) ``LLM_RECORD_DIR``과 ``SBMIG_LLM__RECORD_DIR``을
      뺀다. 환경 변수가 yml보다 우선하므로 ``worker-paths.yml``만으로는 끌 수 없다
    - 항상 ``SBMIG_REDACT_OUTPUT=1``을 넣고, ``COLUMNS``가 없으면 넓은 값을 넣는다

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
    """``SBMIG_JOB_SLOT``에서 슬롯 번호(1부터)를 읽어 반환한다. 값이 없거나 비어 있으면 None을 반환한다.
    양의 정수가 아니면 ``ValueError``를 던지고, 호출하는 쪽은 슬롯이 없는 것으로 처리한다.

    The 1-based slot number from `SBMIG_JOB_SLOT`; None when unset or blank; `ValueError` when it is not a positive integer."""
    raw = (environ.get(contract.SLOT_ENV) or "").strip()
    if not raw:
        return None
    n = int(raw)                     # ValueError for "abc", "2.0"
    if n < 1:
        raise ValueError(f"{contract.SLOT_ENV} must be a positive integer")
    return n


def slot_local_repo(base: Path, slot: int) -> Path:
    """슬롯 ``slot``이 쓸 로컬 저장소 경로를 반환한다.
    ``base`` 옆의 ``<base 이름>-slot<k>``이므로 같은 PVC 안에 남아 다음 실행이 의존성을 다시 받지 않는다.

    Slot `slot`'s local repository: the sibling `<base name>-slot<k>` of `base`."""
    return Path(base).parent / f"{Path(base).name}-slot{slot}"


def default_repo_resolver(configs: list[str], paths_yml: Path) -> Path:
    """슬롯이 없을 때 자식이 쓸 로컬 저장소를 자식과 같은 설정 파일, 같은 순서로 계산한다.
    ``build.local_repo`` -> settings.xml의 ``<localRepository>`` -> ``~/.m2/repository`` 순서로 찾는다.

    The repository the child would use without a slot, from the same config files in the same order (the Worker's paths
    file last): `build.local_repo` → settings.xml `<localRepository>` → `~/.m2/repository`. `load_config` reads its
    overrides from `os.environ` (the Worker's own environment in the pod)."""
    # sbmig.build를 불러오면 Maven 실행 코드 전체가 함께 로드되므로 슬롯 2번부터 필요할 때만 import한다
    from sbmig.build.mvn import local_repo_path
    from sbmig.core.config import load_config
    return local_repo_path(load_config(*configs, paths_yml))


def base_local_repo(environ: dict[str, str], configs: list[str], paths_yml: Path,
                    resolver: RepoResolver | None = None) -> Path:
    """슬롯 저장소의 기준이 되는 로컬 저장소를 반환한다.
    Worker의 ``SBMIG_BUILD__LOCAL_REPO``가 비어 있지 않으면 그 값을 쓰고, 아니면 ``resolver``로 계산한다.
    Job 템플릿의 문자열 ``"null"``은 지정하지 않은 것으로 본다.
    예외는 그대로 던지므로 호출하는 쪽이 처리한다.

    The repository the run would use without a slot: the Worker's own `SBMIG_BUILD__LOCAL_REPO` when it loads as a
    non-empty string (the template's literal "null" means unset), else `resolver`. Raises what the resolver raises."""
    raw = environ.get(LOCAL_REPO_ENV)
    if raw is not None:
        try:
            val = yaml.safe_load(raw)
        except yaml.YAMLError:
            val = None               # 자식의 load_config와 resolver도 이 값에서 똑같이 실패한다
        # ${VAR} 플레이스홀더는 자식의 load_config가 채우므로, 채워지지 않은 값을 경로로 쓰지 않고 resolver에 맡긴다
        if isinstance(val, str) and val.strip() and "${" not in val:
            return Path(val).expanduser()
    return Path((resolver or default_repo_resolver)(list(configs), Path(paths_yml)))


def yaml_env_value(text: str) -> str:
    """``SBMIG_<섹션>__<키>`` 환경 변수로 넘길 값을 만든다.
    자식의 ``load_config``가 ``yaml.safe_load``로 읽어도 같은 문자열이면 그대로, 아니면 JSON 따옴표로 감싼
    문자열을 반환한다. 둘 다 원래 문자열로 읽히지 않으면 ``ValueError``를 던진다.

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
    """PVC에 슬롯 저장소를 만들 수 없을 때 쓰는 파드 안 저장소 경로 ``<임시 폴더>/sbmig-m2-slot<k>``를 반환한다.
    파드가 끝나면 사라져 매번 다시 받아야 하지만 다른 파드와 공유하지 않는다.

    The pod-local fallback repository `<temp dir>/sbmig-m2-slot<k>`: cold on every run, never shared with another pod."""
    return Path(root if root is not None else tempfile.gettempdir()) / POD_LOCAL_REPO_NAME.format(slot=slot)


def prepare_repo_dir(path: Path) -> tuple[str | None, str | None]:
    """``path``를 Maven 로컬 저장소로 쓸 수 있게 준비한다.
    폴더를 만들고 파일을 하나 썼다 지워서 실제로 쓸 수 있는지 확인한다.
    NFS 루트처럼 폴더는 만들어지는데 쓰기가 안 되는 경우가 있기 때문이다.
    ``(환경 변수 값, None)`` 또는 ``(None, 실패 이유)``를 반환하고 예외를 던지지 않는다.

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
    """슬롯 번호에 맞는 Maven 로컬 저장소를 ``child_env``에 설정하고 저널 ``worker_slot``에 남길 데이터를 반환한다.
    ``SBMIG_JOB_SLOT``이 없으면 None을 반환하고, 1번 슬롯은 설정을 바꾸지 않는다. 2번부터는 기준 저장소 옆
    ``-slot<k>`` 폴더를 쓰고, 실패하면 파드 안 저장소(``pod_local: True``), 그것도 실패하면 공유
    저장소(``shared: True``)를 쓴다.
    예외를 던지지 않으며 ``environ``은 바꾸지 않는다.

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
        except Exception as e:  # noqa: BLE001 - 기준을 못 정해도 파드 안 저장소로 계속한다
            # 설정 검증 오류 메시지에는 값이 들어 있을 수 있으므로 예외 형식 이름만 남긴다
            why = f"the base Maven local repository could not be resolved ({type(e).__name__})"
        else:
            if not base.is_absolute():
                base = Path(cwd) / base      # 자식은 cwd에서 실행되므로 상대 경로도 cwd 기준으로 바꾼다
            path = slot_local_repo(base, slot)
            value, why = prepare_repo_dir(path)
            if value is not None:
                child_env[LOCAL_REPO_ENV] = value
                return {"slot": slot, "local_repo": str(path), "shared": False}
        # 공유 저장소를 쓰면 여러 파드의 Maven이 한 저장소에 쓰게 되므로 먼저 파드 안 저장소를 시도한다
        local = pod_local_repo(slot, local_root)
        value, why_local = prepare_repo_dir(local)
        if value is not None:
            child_env[LOCAL_REPO_ENV] = value
            return {"slot": slot, "local_repo": str(local), "shared": False, POD_LOCAL_KEY: True,
                    "note": f"pod-local repository: {why}"}
        return shared(f"{why}; pod-local fallback: {why_local}")
    except Exception as e:  # noqa: BLE001 - 어떤 예외가 나도 실행을 막지 않는다
        return shared(f"the slot repository could not be set up ({type(e).__name__})")


def slot_message(data: dict) -> str:
    """``apply_slot_repo`` 결과를 파드 로그에 남길 한 줄로 만든다. / One pod-log line for an `apply_slot_repo` result."""
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
                  stop_after: str | None, llm_provider: str | None, flow: str | None = None) -> list[str]:
    """자식 프로세스로 실행할 ``sbmig.cli run`` 명령을 만든다.
    형태는
    ``python -m sbmig.cli run <zip> --config … --config <worker-paths.yml> --out <out> --run-id <id> [--stop-after X] [--llm-provider Y] [--flow remediation]``이고,
    ``--flow``는 CLI 기본값인 full이 아닐 때만 붙인다.

    The child command line (run-job.sh's shape, the Worker's paths file as the last `--config`); `--flow` only when it
    is not the default `full`."""
    cmd = [python, "-m", "sbmig.cli", "run", zip_path]
    for c in configs:
        cmd += ["--config", c]
    cmd += ["--config", str(paths_yml), "--out", out, "--run-id", run_id]
    if stop_after:
        cmd += ["--stop-after", stop_after]
    if llm_provider:
        cmd += ["--llm-provider", llm_provider]
    if flow and flow != contract.FLOW_FULL:
        cmd += ["--flow", flow]
    return cmd


def build_resume_command(python: str, run_dir: Path, configs: list[str], paths_yml: Path) -> list[str]:
    """취약점 조치를 이어서 진행하는 자식 명령을 만든다: ``python -m sbmig.cli remediation-resume --run-dir <run dir> --config … --config <worker-paths.yml>``.

    The remediation resume child command (the Worker's paths file as the last `--config`)."""
    cmd = [python, "-m", "sbmig.cli", "remediation-resume", "--run-dir", str(run_dir)]
    for c in configs:
        cmd += ["--config", c]
    return cmd + ["--config", str(paths_yml)]


def last_result_line(run_dir: Path) -> str | None:
    """``<run dir>/sbmig.log``에서 마지막 ``SBMIG-RESULT`` 줄을 찾아 반환한다.
    운영자가 결과를 확인하는 줄이다. / The log's last SBMIG-RESULT line."""
    try:
        text = (Path(run_dir) / "sbmig.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    hits = [ln for ln in text.splitlines() if RESULT_MARK in ln]
    return hits[-1].strip() if hits else None


@dataclass
class EnvCheck:
    """환경 확인(``check_environment``) 한 번의 결과를 담는 데이터 클래스.
    ``policy``는 refuse 또는 warn이고, 차이는 변수 이름만 담는다. ``problems``가 비어 있으면 통과다.
    ``error``는 확인 자체가 실패했을 때의 예외 형식 이름이며, 메시지는 값을 담을 수 있으므로 넣지 않는다.

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
        """문제 문장들을 ``; ``로 잇고 정해진 안내 문구를 붙여 거절 사유나 경고 줄을 만든다. / The problems joined, plus the fixed hint."""
        return "; ".join(self.problems) + " — " + ENV_PIN_HINT

    def journal_data(self, run_id: str, clean: Callable[[str], str]) -> dict:
        """``worker_env_differs`` 저널 줄에 넣을 데이터를 만든다. 값은 빼고 이름 목록만 담는다. / The journal data of `worker_env_differs` (names only)."""
        data: dict = {"run_id": run_id, "policy": self.policy}
        for key in ("differs", "main_only", "worker_only", "run_values"):
            data[key] = [clean(n) for n in getattr(self, key)]
        if self.error:
            data["check_error"] = self.error
        return data

    def _failed(self, exc: BaseException) -> None:
        # 확인이 실패해도 Worker를 멈추지 않고 같은 정책 아래의 문제 하나로 센다
        self.error = self.error or type(exc).__name__
        self.problems.append(f"the environment check failed: {type(exc).__name__}")


def _real_path(path: str | Path, base: Path) -> str:
    p = Path(path)
    if not p.is_absolute():
        p = Path(base) / p          # 자식은 repo root에서 실행되므로 상대 경로는 repo root 기준으로 바꾼다
    return os.path.normcase(os.path.realpath(p))


def _run_value_problems(record: contract.RequestRecord, environ: dict[str, str],
                        repo_root: Path) -> list[tuple[str, str]]:
    # request.json의 digest는 요청한 설정 이름으로 계산했으므로, SBMIG_CONFIG가 다른 파일을 가리키면
    # 검증하지 않은 파일로 실행되는 것을 잡는다
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
        default = RUN_VALUE_DEFAULTS.get(name)
        if (environ.get(name) or default) != (getattr(record, attr, None) or default):     # 값이 비었거나 없으면 기본값으로 비교한다
            out.append((name, f"{name} differs from request.json's {attr}"))
    return out


def check_environment(record: contract.RequestRecord, state: contract.ControlState | None, environ: dict[str, str],
                      repo_root: Path) -> EnvCheck | None:
    """Job을 만든 Main의 환경(``state.env_pins``)과 이 Worker의 환경이 같은지 확인한다.
    ``env_pins`` 차이와 함께 실행별 값(``SBMIG_CONFIG`` 파일, ``SBMIG_STOP_AFTER``, ``SBMIG_LLM_PROVIDER``,
    ``SBMIG_FLOW``)이 ``request.json``과 같은지 본다.
    state나 ``env_pins``가 없거나 정책이 off면 None을 반환하고, 정책이 비어 있으면 refuse로 본다.
    예외를 던지지 않으며 결과에는 변수 이름만 담는다.

    Compare this Worker's environment with the pins the Main that created the Job wrote (`state.env_pins`). None = no
    check (no state, no pins, policy off); pins without a policy = refuse. Checks the pin diff and the per-run values
    against `request.json`; never raises (a failure inside becomes a problem under the same policy). Names only."""
    if state is None or state.env_pins is None:
        return None
    try:
        policy = contract.env_pin_policy(state.env_pin_policy, default=contract.ENV_PIN_REFUSE)
    except Exception:  # noqa: BLE001 - 읽을 수 없는 정책 값은 가장 엄격한 refuse로 본다
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
               repo_root: Path, state_name: str = contract.STATE_FILE) -> EnvCheck | None:
    # Worker는 control/을 읽기만 하고, 읽기에 실패하면 정책을 알 수 없으므로 refuse로 본다
    # 취약점 조치 재개는 그 Job을 만든 Main이 기록한 remediation-resume.json을 기준으로 확인한다
    try:
        state = (contract.ControlState.read(run_dir) if state_name == contract.STATE_FILE
                 else contract.ControlState.read(run_dir, state_name))
    except Exception as e:  # noqa: BLE001
        check = EnvCheck(policy=contract.ENV_PIN_REFUSE)
        check._failed(e)
        return check
    return check_environment(record, state, environ, repo_root)


def _refuse(run_dir: Path, run_id: str, reason: str, attempt: int | None, action: str | None = None) -> int:
    _say(f"REFUSED: {reason}")
    _journal(run_dir, "worker_refused", {"run_id": run_id, "reason": reason, **({"action": action} if action else {})})
    contract.JobStatus(exit_code=3, run_id=run_id, attempt=attempt, error=reason, sbmig_version=contract.sbmig_version(),
                       action=action).write(run_dir, contract.status_file_for(action))
    return 3


def terminated_error(signum: int) -> str:
    """종료 신호로 중단된 실행의 오류 메시지를 만든다.
    ``job-status.json``과 저널 ``worker_finished``의 ``error``에 쓴다.

    The error text of a run cut short by a stop signal."""
    return (f"the Worker was terminated by {signal_name(signum)} (pod deleted, Job deadline or stop) before sbmig finished "
            "— sbmig and its Maven processes were stopped; this run has no result")


def _finish_terminated(run_dir: Path, run_id: str, attempt: int, version: str, env: dict[str, str],
                       signum: int, action: str | None = None) -> int:
    # 실제 결과로 오해하지 않도록 exit 3과 오류 메시지를 남긴다.
    # Main은 상태 파일을 믿고 자기 마스킹을 건너뛰므로 마스킹을 먼저 한다
    error = terminated_error(signum)
    if action:
        # 중단된 취약점 조치 재개는 다음 재개 때 마지막으로 저장한 상태부터 다시 진행하며 이관 결과와는 상관없다
        error += (" — the remediation resume stopped where its state was last saved; the migration's results are untouched"
                  " (a running IVAS analysis is not cancelled)")
    else:
        # 이관이 끝난 뒤 이어서 실행한 취약점 조치 중에 끊길 수 있으므로, 이관 결과와 보고서가 남아 있다는
        # 것을 알 수 있게 SBMIG-RESULT 줄을 오류 메시지에 붙인다
        finished = last_result_line(run_dir)
        if finished:
            error += f" — the migration itself had finished before (its report is in the run dir): {finished}"
    _say(f"ERROR: {error}")
    redacted = redact_run_dir(run_dir, contract.secret_values(env))
    _journal(run_dir, "worker_finished", {"exit_code": TERMINATED_EXIT, "redacted_files": redacted, "error": error,
                                          "terminated": signal_name(signum), **({"action": action} if action else {})})
    contract.JobStatus(exit_code=TERMINATED_EXIT, run_id=run_id, attempt=attempt, error=error,
                       sbmig_version=version, action=action).write(run_dir, contract.status_file_for(action))
    return TERMINATED_EXIT


def main(argv: list[str] | None = None, *, environ: dict[str, str] | None = None, run_child: RunChild | None = None,
         repo_resolver: RepoResolver | None = None, install_signals: InstallSignals | None = None) -> int:
    """Worker의 진입점. 환경 변수로 받은 요청 하나를 확인하고 실행한 뒤 결과 상태 파일을 기록한다.
    종료 코드는 모듈 docstring에 정리되어 있다. ``repo_resolver``와 ``install_signals``는 테스트에서 바꿔
    넣는 값이며, None이면 ``default_repo_resolver``와 ``install_stop_signals``를 쓴다.

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

    clean = Redactor(lambda: contract.secret_values(env))      # Worker가 직접 출력하는 파드 로그, 저널, 상태 파일의 오류 메시지를 마스킹한다
    # 이 Job이 할 일. 비어 있으면 이관(sbmig run), remediation-resume이면 취약점 조치 재개다
    action = (env.get(contract.ACTION_ENV) or "").strip() or None
    if action is not None and action not in contract.WORKER_ACTIONS:
        # 알 수 없는 작업이면 이관의 job-status.json을 덮어쓰지 않도록 결과 파일을 기록하지 않는다.
        # Main은 컨테이너 종료 코드로 결과를 안다
        reason = f"unknown {contract.ACTION_ENV}={clean(action[:40])!r} — expected one of {', '.join(contract.WORKER_ACTIONS)}"
        _say(f"REFUSED: {reason}")
        _journal(run_dir, "worker_refused", {"run_id": run_id, "reason": reason})
        return 3
    record = contract.RequestRecord.read(run_dir)
    if record is None:
        return _refuse(run_dir, run_id, REFUSE_NO_REQUEST, None, action)
    if action == contract.ACTION_REMEDIATION_RESUME:
        try:
            resume_state = contract.ControlState.read(run_dir, contract.RESUME_STATE_FILE)
        except Exception:  # noqa: BLE001 — unreadable = not recorded by Main: refuse
            resume_state = None
        if resume_state is None or resume_state.action != action:
            return _refuse(run_dir, run_id, REFUSE_NO_RESUME, None, action)
        attempt = int(resume_state.attempt)
        # 재개는 이관 zip을 읽지 않으므로 sha256은 확인하지 않는다
        problems = contract.verify_request(record, repo_root, None, run_id)
        if not (run_dir / "remediation" / "state.json").is_file():
            problems.append(REFUSE_NO_CHAIN)
    else:
        attempt = int(record.attempt)
        problems = contract.verify_request(record, repo_root, Path(zip_path), run_id)
    if problems:
        return _refuse(run_dir, run_id, "; ".join(problems), attempt, action)
    # 파일이 같아도 환경 변수가 다르면 다른 설정으로 실행되므로 Job을 만든 Main의 env_pins와 비교한다
    check = _env_check(run_dir, record, env, repo_root, contract.state_file_for(action))
    if check is not None and check.problems:
        reason = clean(check.reason())
        if check.policy != contract.ENV_PIN_WARN:          # refuse와 알 수 없는 정책 값은 거절한다
            return _refuse(run_dir, run_id, reason, attempt, action)
        _say(f"WARNING: {reason}")
        _journal(run_dir, ENV_DIFFERS_EVENT, check.journal_data(run_id, clean))

    version = contract.sbmig_version()
    started = {"run_id": run_id, "attempt": attempt, "sbmig_version": version, "pid": os.getpid()}
    if action:
        started["action"] = action
    if check is not None:
        started["env_pin"] = check.policy
    _journal(run_dir, "worker_started", started)
    # PID 1은 처리기가 없으면 SIGTERM을 무시하므로 처리기를 등록한다.
    # 자식 실행 중에 신호가 오면 자식 트리를 종료하고 기록을 남긴다
    stops = StopSignals()
    restore = (install_signals or install_stop_signals)(stops.handle)
    try:
        return _run(env, record, run_dir, run_id, zip_path, out, repo_root, version, clean, run_child, repo_resolver, stops,
                    attempt=attempt, action=action)
    finally:
        restore()


def _run(env: dict[str, str], record: contract.RequestRecord, run_dir: Path, run_id: str, zip_path: str, out: str,
         repo_root: Path, version: str, clean: Redactor, run_child: RunChild, repo_resolver: RepoResolver | None,
         stops: StopSignals, *, attempt: int, action: str | None = None) -> int:
    # from here on a status file must exist whatever happens (Main waits for it)
    rc: int | None = None
    status_name = contract.status_file_for(action)
    tagged: dict = {"action": action} if action else {}
    try:
        paths_yml = write_paths_yml(run_dir, env)
        configs = resolve_configs(env, record, repo_root)
        python = env.get("SBMIG_PYTHON") or sys.executable
        if action == contract.ACTION_REMEDIATION_RESUME:
            cmd = build_resume_command(python, run_dir, configs, paths_yml)
        else:
            # 실행 방식은 request.json 값을 따르고 환경 변수 SBMIG_FLOW는 비교에만 쓴다.
            # 취약점 조치만 요청한 실행이 환경 변수 때문에 이관 전체로 실행되면 안 되기 때문이다
            cmd = build_command(python, zip_path, configs, paths_yml, out, run_id, env.get("SBMIG_STOP_AFTER") or None,
                                env.get("SBMIG_LLM_PROVIDER") or None, flow=record.flow or contract.FLOW_FULL)
        child_env = child_environ(env)
        # 2번 슬롯부터 별도의 Maven 로컬 저장소를 쓴다. 환경 확인이 끝난 뒤 자식의 환경 변수에만 넣는다
        slot = apply_slot_repo(child_env, env, configs, paths_yml, cwd=repo_root, resolver=repo_resolver)
        if slot is not None:
            _say(clean(slot_message(slot)))
            _journal(run_dir, SLOT_EVENT, {k: clean(v) if isinstance(v, str) else v for k, v in slot.items()})
        _say(clean(" ".join(["sbmig", *cmd[3:]])))
        stops.arm()                                   # 이미 신호가 와 있으면 자식을 실행하지 않는다
        try:
            rc = int(run_child(cmd, repo_root, child_env))
        finally:
            stops.disarm()                            # 이후의 신호는 기억만 해서 마스킹과 상태 파일 기록이 끊기지 않게 한다
    except WorkerTerminated as t:
        return _finish_terminated(run_dir, run_id, attempt, version, env, t.signum, action)
    except (Exception, KeyboardInterrupt) as e:  # noqa: BLE001 — whatever broke, Main must still get a status file
        error = clean(f"the Worker could not run sbmig: {type(e).__name__}: {e}")
        _say(f"ERROR: {error}")
        redacted = redact_run_dir(run_dir, contract.secret_values(env))
        _journal(run_dir, "worker_finished", {"exit_code": 3, "redacted_files": redacted, "error": error, **tagged})
        contract.JobStatus(exit_code=3, run_id=run_id, attempt=attempt, error=error, sbmig_version=version,
                           action=action).write(run_dir, status_name)
        return 3

    _say(f"sbmig exit {rc} — run dir {run_dir}")
    redacted = redact_run_dir(run_dir, contract.secret_values(env))
    # 재개 실행은 이관의 SBMIG-RESULT 줄을 다시 출력하지 않는다.
    # 자식이 출력한 SBMIG-REMEDIATION 줄이 파드 로그에 남아 있다
    line = None if action else last_result_line(run_dir)
    if line:
        print(line, flush=True)
    _journal(run_dir, "worker_finished", {"exit_code": rc, "redacted_files": redacted, **tagged})
    contract.JobStatus(exit_code=rc, run_id=run_id, attempt=attempt, sbmig_version=version,
                       action=action).write(run_dir, status_name)
    if rc == 3:
        return 3
    if rc == 2 and (env.get("SBMIG_FAIL_JOB_ON_BUILD_FAIL") or "").strip() == "1":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
