"""개발 PC 의 "파드": Job 모드의 흐름을 그대로 두고 쿠버네티스 Job 자리에 로컬 서브프로세스를 쓴다 (2026-11-05, 사용자: "로컬 개발에서는
파드 생성이 들어가면 안 돌거니까 … 개발 모드일 때는 파드 생성 없이").

``SBMIG_WEB_MODE=local-job``(또는 ``k8s-job`` 인데 클러스터 밖) 이면 ``RunManager`` 가 ``K8sClient`` 대신 이 클라이언트를 쓴다. 같은
세 호출: ``create_job`` 은 렌더된 Job 매니페스트의 env(SBMIG_SOURCE_ZIP, SBMIG_OUT, SBMIG_RUN_ID, SBMIG_CONFIG, SBMIG_STOP_AFTER,
SBMIG_LLM_PROVIDER)를 그대로 넘겨 파드가 돌릴 바로 그 Worker ``python -m sbmig.web.worker`` 를 띄운다(v2.1z-ac — Worker 가
``control/request.json`` 의 digest 를 대조하고 ``sbmig.cli run`` 을 돌리고 비밀 값을 지우고 ``job-status.json`` 을 쓴다; 표준 출력은
``<run dir>/job-local.log``). ``job_state`` 는 프로세스 상태를 Running / Succeeded / Failed 로, ``delete_job`` 은 프로세스를 끝낸다.
Worker 가 ``job-status.json`` 을 남기지 않고 끝났을 때만(죽었거나 가짜 명령) 대신 쓴다. 그래서 웹의 Job 모드 코드(실행 폴더, 저널 tail,
'파드' 단계, stop.request)가 쿠버네티스 없이 노트북에서 전부 돈다.

The developer's "pod": the Job-mode flow with a local subprocess in place of a Kubernetes Job — the same three calls as
`K8sClient`, running the same Worker the pod runs (`python -m sbmig.web.worker`, which verifies the pinned request, runs
`sbmig.cli run` and writes `job-status.json`); a fallback `job-status.json` only when the Worker left none."""
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

from .contract import WORKER_MODULE
from .k8s import JobState, K8sError

log = logging.getLogger("sbmig.web.localjob")


def _kill_tree(proc: subprocess.Popen) -> None:
    """프로세스와 그 자손(Maven / Java)을 끝낸다: POSIX 는 세션(프로세스 그룹)에 TERM → KILL, Windows 는 ``taskkill /T /F``."""
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
    """로컬 서브프로세스로 Job 을 흉내 내는 클라이언트(``namespace`` 는 "local"). ``command_factory(env) -> list[str]`` 를 바꿔
    끼우면 테스트가 가짜 실행 파일을 쓸 수 있다.

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
        # 사용자(2026-11-05): "서브프로세스로 돌면 콘솔 창에는 안 뜰 거잖아?" — 개발 모드라 자식의 출력을 파일(job-local.log)에 쓰면서
        # 서버 콘솔에도 `[job <run id>]` 접두어로 흘린다
        self.echo = (os.environ.get(self.ECHO_ENV, "1").strip().lower() not in ("0", "false", "no", "off")) if echo is None else echo
        self._procs: dict[str, tuple[subprocess.Popen, Path, dict[str, str]]] = {}

    # ---- the command the Job pod runs
    def _command(self, env: dict[str, str]) -> list[str]:
        """파드와 같은 Worker 명령. 실행별 값(zip, out, run id, 설정 …)은 인자가 아니라 env 로 간다 — Worker 가 ``SBMIG_*`` 를 읽는다.

        The Worker command the pod runs; the run's values travel as `SBMIG_*` env, not as arguments."""
        return [self.python, "-m", WORKER_MODULE]

    @staticmethod
    def _env_of(manifest: dict) -> dict[str, str]:
        containers = manifest.get("spec", {}).get("template", {}).get("spec", {}).get("containers") or []
        items = containers[0].get("env") if containers else []
        return {e["name"]: str(e.get("value", "")) for e in (items or []) if isinstance(e, dict) and e.get("name")}

    def create_job(self, manifest: dict) -> str:
        """매니페스트의 env 로 명령을 만들어 서브프로세스를 띄운다. 이름 = 매니페스트의 이름."""
        name = str(manifest.get("metadata", {}).get("name") or f"local-{int(time.time())}")
        env = self._env_of(manifest)
        for key in ("SBMIG_SOURCE_ZIP", "SBMIG_OUT", "SBMIG_RUN_ID"):
            if not env.get(key):
                raise K8sError(f"local job: the manifest's env lacks {key}")
        run_dir = Path(env["SBMIG_OUT"]) / env["SBMIG_RUN_ID"]
        run_dir.mkdir(parents=True, exist_ok=True)
        cmd = list(self.command_factory(env))
        log_path = run_dir / "job-local.log"
        # 2026-11-05 (한국어 Windows): 자식의 stdout 은 파이프라 로케일(cp949)로 쓰이고 Rich 는 80칸으로 접는다 — 자식을 UTF-8 로 쓰게 하고
        # (job-local.log 도 UTF-8, `—` 로 죽는 "Logging error" 없음) 폭을 넉넉히 준다. 셸이 이미 정한 값은 그대로 둔다
        child_env = {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
                     "COLUMNS": os.environ.get(self.COLUMNS_ENV, "") or self.DEFAULT_COLUMNS,
                     **os.environ, **{k: v for k, v in env.items() if k.startswith("SBMIG_")}}
        # Worker 는 SBMIG_APP_DIR 의 config/ 로 digest 를 다시 계산한다 — Main 이 고정한 것과 같은 폴더여야 대조가 맞는다
        if self.repo_root is not None and "SBMIG_APP_DIR" not in env:
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
        """자식의 출력을 줄마다 job-local.log 에 쓰고(echo 면) 콘솔에도 ``[job <run id>]`` 로 흘린다. 자식이 끝나면 파일을 닫는다."""
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
        """프로세스 상태 → JobState. 끝났는데 Worker 가 ``job-status.json`` 을 남기지 않았으면 대신 쓴다(run id 포함, attempt 없음 = 대조
        생략). 모르는 이름 → Missing.

        The process state as a JobState; a fallback `job-status.json` only when the Worker left none."""
        hit = self._procs.get(name)
        if hit is None:
            return JobState("Missing", message="no such local job")
        proc, run_dir, env = hit
        rc = proc.poll()
        pod = f"local-{proc.pid}"
        if rc is None:
            return JobState("Running", pod=pod, message="")
        status = run_dir / "job-status.json"
        if not status.is_file():
            try:
                status.write_text(json.dumps({"exit_code": rc, "finished_at": datetime.now(timezone.utc).isoformat(),
                                              "run_id": env.get("SBMIG_RUN_ID") or None}), encoding="utf-8")
            except OSError as e:
                log.debug("local job %s: job-status.json not written: %s", name, e)
        return JobState("Succeeded" if rc in (0, 2) else "Failed", pod=pod, exit_code=rc,
                        message="" if rc in (0, 2) else f"sbmig exit {rc}")

    def delete_job(self, name: str) -> None:
        """프로세스를 끝낸다(terminate → kill). 모르는 이름이면 조용히."""
        hit = self._procs.get(name)
        if hit is None:
            return
        proc = hit[0]
        if proc.poll() is None:
            _kill_tree(proc)
        log.info("local job %s: terminated (pid %d)", name, proc.pid)
