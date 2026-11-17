"""개발 PC에서 쿠버네티스 Job 대신 로컬 서브프로세스로 Worker를 실행하는 클라이언트 모듈.
Job 모드의 처리 흐름은 그대로 유지된다.
``SBMIG_WEB_MODE=local-job``이거나, ``k8s-job``인데 클러스터 밖에서 실행 중이면 ``RunManager``가
``K8sClient`` 대신 이 클라이언트를 쓴다.
- ``create_job``은 매니페스트의 env 중 실행별 값(``contract.RUN_ENV``)만 넘겨, 파드와 같은 Worker
  ``python -m sbmig.web.worker``를 실행한다. 출력은 ``<run dir>/job-local.log``에 기록한다
- 템플릿의 나머지 env는 파드용 설정이므로 넘기지 않는다.
  로컬 Worker의 설정은 개발자의 셸과 ``.env``가 정한다
- ``job_state``는 프로세스 상태를 Running / Succeeded / Failed로 알려 주고, ``delete_job``은 프로세스를 종료한다
- Worker가 ``job-status.json``을 남기지 않고 끝났을 때만 대신 기록한다.
  조치 재개 Job은 ``remediation-job-status.json``에 기록한다

The developer's "pod": the Job-mode flow with a local subprocess in place of a Kubernetes Job — the same three calls as
`K8sClient`, running the same Worker the pod runs (`python -m sbmig.web.worker`, which verifies the pinned request, runs
`sbmig.cli run` and writes `job-status.json`); a fallback `job-status.json` only when the Worker left none. A remediation
resume Job (`SBMIG_WORKER_ACTION`, 2026-11-11) gets its fallback in `remediation-job-status.json` instead."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contract import ACTION_ENV, RUN_ENV, WORKER_MODULE, status_file_for
from .k8s import JobState, K8sError

log = logging.getLogger("sbmig.web.localjob")


def _kill_tree(proc: subprocess.Popen) -> None:
    """프로세스와 그 자식 프로세스(Maven / Java)를 모두 종료한다.
    POSIX에서는 프로세스 그룹에 TERM, 이어서 KILL을 보내고, Windows에서는 ``taskkill /T /F``를 쓴다."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=30, check=False)
        else:
            import signal
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError) as e:
        log.debug("local job pid %d: kill tree failed (%s) — falling back to terminate", proc.pid, e)
        proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()


class LocalJobClient:
    """로컬 서브프로세스로 쿠버네티스 Job을 대신하는 클라이언트 클래스. ``namespace``는 "local"이다.
    테스트는 ``command_factory(env) -> list[str]``를 바꿔 가짜 실행 파일을 쓸 수 있다.

    Jobs as local subprocesses; `command_factory(env)` lets tests substitute the command."""
    namespace = "local"

    ECHO_ENV = "SBMIG_LOCAL_JOB_ECHO"       # "0" turns the console echo of the child's output off (default on: development)
    COLUMNS_ENV = "SBMIG_LOCAL_JOB_COLUMNS" # the width Rich wraps the child's console lines at (a pipe is not a terminal: 80 by default)
    DEFAULT_COLUMNS = "160"

    def __init__(self, repo_root: Path | str | None = None, command_factory: Any = None, python: str | None = None,
                 echo: bool | None = None) -> None:
        self.repo_root = Path(repo_root) if repo_root else None
        self.python = python or sys.executable
        self.command_factory = command_factory or self._command
        # 서브프로세스 출력은 콘솔에 보이지 않으므로 job-local.log에 기록하면서 서버 콘솔에도
        # `[job <run id>]`를 앞에 붙여 출력한다
        self.echo = (os.environ.get(self.ECHO_ENV, "1").strip().lower() not in ("0", "false", "no", "off")) if echo is None else echo
        self._procs: dict[str, tuple[subprocess.Popen, Path, dict[str, str]]] = {}

    # ---- the command the Job pod runs
    def _command(self, env: dict[str, str]) -> list[str]:
        """파드와 같은 Worker 실행 명령을 만든다.
        실행별 값은 인자가 아니라 환경 변수로 넘기고 Worker가 ``SBMIG_*``를 읽는다.

        The Worker command the pod runs; the run's values travel as `SBMIG_*` env, not as arguments."""
        return [self.python, "-m", WORKER_MODULE]

    @staticmethod
    def _env_of(manifest: dict) -> dict[str, str]:
        """매니페스트에서 로컬 Worker로 넘길 환경 변수를 고른다.
        ``contract.RUN_ENV``에 있는 이름이면서 값을 직접 쓴 항목만 고른다.
        템플릿은 파드 기준이라 컨테이너 경로나 ``valueFrom``(ConfigMap / Secret) 항목은 개발 PC에 맞지
        않는다. 특히 ``valueFrom``은 로컬에서 ``""``가 되는데, 빈 ``SBMIG_<A>__<B>``는 설정 값을
        ``None``으로 덮어써 ``load_config``를 실패하게 만든다.

        The env the local Worker gets from the manifest: the per-run names with a literal value only. The template describes
        the POD — its container paths and its ConfigMap / Secret references are not the developer machine's."""
        containers = manifest.get("spec", {}).get("template", {}).get("spec", {}).get("containers") or []
        items = containers[0].get("env") if containers else []
        return {e["name"]: str(e["value"]) for e in (items or [])
                if isinstance(e, dict) and e.get("name") in RUN_ENV and "valueFrom" not in e and e.get("value") is not None}

    def create_job(self, manifest: dict) -> str:
        """매니페스트의 환경 변수로 명령을 만들어 서브프로세스를 실행하고, 매니페스트에 있는 Job 이름을 반환한다."""
        name = str(manifest.get("metadata", {}).get("name") or f"local-{int(time.time())}")
        env = self._env_of(manifest)
        for key in ("SBMIG_SOURCE_ZIP", "SBMIG_OUT", "SBMIG_RUN_ID"):
            if not env.get(key):
                raise K8sError(f"local job: the manifest's env lacks {key}")
        run_dir = Path(env["SBMIG_OUT"]) / env["SBMIG_RUN_ID"]
        run_dir.mkdir(parents=True, exist_ok=True)
        cmd = list(self.command_factory(env))
        log_path = run_dir / "job-local.log"
        # 자식의 stdout은 파이프라서 로케일(cp949)로 출력되고 Rich는 80칸에서 줄을 바꾸므로, 자식이 UTF-8로
        # 출력하게 하고 폭을 넉넉히 준다
        # 셸에서 이미 정한 값은 그대로 둔다
        child_env = {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
                     "COLUMNS": os.environ.get(self.COLUMNS_ENV, "") or self.DEFAULT_COLUMNS,
                     **os.environ, **env}
        # Worker는 SBMIG_APP_DIR 아래 config/로 해시를 다시 계산하므로 Main이 고정한 폴더와 같아야 한다.
        # 템플릿 값은 컨테이너 경로이므로 이 Main의 저장소 경로를 넣는다
        if self.repo_root is not None:
            child_env["SBMIG_APP_DIR"] = str(self.repo_root)
        # its own process group / session: delete_job must take the Maven / Java grandchildren with it (review 2026-11-05)
        group: dict[str, Any] = ({"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)} if os.name == "nt"
                                 else {"start_new_session": True})
        try:
            proc = subprocess.Popen(cmd, cwd=str(self.repo_root) if self.repo_root else None, env=child_env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **group)
        except OSError as e:
            raise K8sError(f"local job: cannot start {cmd[0]}: {e}") from e
        threading.Thread(target=self._pump, args=(proc, log_path, env["SBMIG_RUN_ID"]), name=f"sbmig-localjob-{name}",
                         daemon=True).start()
        self._procs[name] = (proc, run_dir, env)
        log.info("local job %s: pid %d — %s%s", name, proc.pid, " ".join(cmd),
                 f" (output → {log_path.name}" + (" and this console)" if self.echo else ")"))
        return name

    def _pump(self, proc: subprocess.Popen, log_path: Path, run_id: str) -> None:
        """자식 프로세스의 출력을 줄마다 job-local.log에 기록하고, echo가 켜져 있으면 콘솔에도
        ``[job <run id>]``를 붙여 출력한다. 자식이 끝나면 파일을 닫는다."""
        prefix = f"[job {run_id}] "
        try:
            with open(log_path, "ab") as fh, proc.stdout:  # type: ignore[union-attr]
                for raw in iter(proc.stdout.readline, b""):
                    fh.write(raw)
                    fh.flush()
                    if self.echo:
                        try:
                            text = prefix + raw.decode("utf-8", errors="replace")
                            enc = getattr(sys.stdout, "encoding", None) or "utf-8"
                            # a cp949 console cannot show every character the child wrote (— ✓ …): replace, never raise
                            sys.stdout.write(text.encode(enc, errors="replace").decode(enc, errors="replace"))
                            sys.stdout.flush()
                        except (OSError, ValueError, LookupError):      # a closed console never stops the job
                            self.echo = False
        except (OSError, ValueError) as e:
            log.debug("local job output pump for %s ended: %s", run_id, e)

    def job_state(self, name: str) -> JobState:
        """프로세스 상태를 JobState로 반환한다. 모르는 이름이면 Missing을 반환한다.
        프로세스가 끝났는데 Worker가 ``job-status.json``을 남기지 않았으면, run id만 넣고 attempt 없이 대신 기록한다.

        The process state as a JobState; a fallback `job-status.json` only when the Worker left none."""
        hit = self._procs.get(name)
        if hit is None:
            return JobState("Missing", message="no such local job")
        proc, run_dir, env = hit
        rc = proc.poll()
        pod = f"local-{proc.pid}"
        if rc is None:
            return JobState("Running", pod=pod, message="")
        # 조치 재개 Job은 결과 파일이 따로 있으므로 이미 있는 이관의 job-status.json은 건드리지 않는다
        status = run_dir / status_file_for(env.get(ACTION_ENV) or None)
        if not status.is_file():
            try:
                status.write_text(json.dumps({"exit_code": rc, "finished_at": datetime.now(timezone.utc).isoformat(),
                                              "run_id": env.get("SBMIG_RUN_ID") or None,
                                              **({"action": env[ACTION_ENV]} if env.get(ACTION_ENV) else {})}),
                                  encoding="utf-8")
            except OSError as e:
                log.debug("local job %s: job-status.json not written: %s", name, e)
        return JobState("Succeeded" if rc in (0, 2) else "Failed", pod=pod, exit_code=rc,
                        message="" if rc in (0, 2) else f"sbmig exit {rc}")

    def delete_job(self, name: str) -> None:
        """프로세스를 종료한다(terminate 후 kill). 모르는 이름이면 무시한다."""
        hit = self._procs.get(name)
        if hit is None:
            return
        proc = hit[0]
        if proc.poll() is None:
            _kill_tree(proc)
        log.info("local job %s: terminated (pid %d)", name, proc.pid)
