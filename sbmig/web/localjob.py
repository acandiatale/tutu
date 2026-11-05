"""개발 PC 의 "파드": Job 모드의 흐름을 그대로 두고 쿠버네티스 Job 자리에 로컬 서브프로세스를 쓴다 (2026-11-05, 사용자: "로컬 개발에서는
파드 생성이 들어가면 안 돌거니까 … 개발 모드일 때는 파드 생성 없이").

``SBMIG_WEB_MODE=local-job``(또는 ``k8s-job`` 인데 클러스터 밖) 이면 ``RunManager`` 가 ``K8sClient`` 대신 이 클라이언트를 쓴다. 같은
세 호출: ``create_job`` 은 렌더된 Job 매니페스트의 env(SBMIG_SOURCE_ZIP, SBMIG_OUT, SBMIG_RUN_ID, SBMIG_CONFIG, SBMIG_STOP_AFTER,
SBMIG_LLM_PROVIDER)에서 run-job.sh 가 돌릴 바로 그 명령 ``python -m sbmig.cli run <zip> --config … --out <out> --run-id <id>`` 을
만들어 띄우고(표준 출력은 ``<run dir>/job-local.log``), ``job_state`` 는 프로세스 상태를 Pending / Running / Succeeded / Failed 로,
``delete_job`` 은 프로세스를 끝낸다. 끝나면 run-job.sh 처럼 ``<run dir>/job-status.json`` 을 쓴다. 그래서 웹의 Job 모드 코드(실행 폴더,
저널 tail, '파드' 단계, stop.request)가 쿠버네티스 없이 노트북에서 전부 돈다.

The developer's "pod": the Job-mode flow with a local subprocess in place of a Kubernetes Job — the same three calls as
`K8sClient`, the command run-job.sh would run, `job-status.json` at the end. Everything else of the Job mode (run dir,
journal tailing, the '파드' step, stop.request) runs unchanged on a laptop."""
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

from .k8s import JobState, K8sError

log = logging.getLogger("sbmig.web.localjob")


class LocalJobClient:
    """로컬 서브프로세스로 Job 을 흉내 내는 클라이언트(``namespace`` 는 "local"). ``command_factory(env) -> list[str]`` 를 바꿔
    끼우면 테스트가 가짜 실행 파일을 쓸 수 있다.

    Jobs as local subprocesses; `command_factory(env)` lets tests substitute the command."""
    namespace = "local"

    ECHO_ENV = "SBMIG_LOCAL_JOB_ECHO"       # "0" turns the console echo of the child's output off (default on: development)

    def __init__(self, repo_root: Path | str | None = None, command_factory: Any = None, python: str | None = None,
                 echo: bool | None = None) -> None:
        self.repo_root = Path(repo_root) if repo_root else None
        self.python = python or sys.executable
        self.command_factory = command_factory or self._command
        # 사용자(2026-11-05): "서브프로세스로 돌면 콘솔 창에는 안 뜰 거잖아?" — 개발 모드라 자식의 출력을 파일(job-local.log)에 쓰면서
        # 서버 콘솔에도 `[job <run id>]` 접두어로 흘린다
        self.echo = (os.environ.get(self.ECHO_ENV, "1").strip().lower() not in ("0", "false", "no", "off")) if echo is None else echo
        self._procs: dict[str, tuple[subprocess.Popen, Path, dict[str, str]]] = {}

    # ---- the command run-job.sh would run
    def _command(self, env: dict[str, str]) -> list[str]:
        cmd = [self.python, "-m", "sbmig.cli", "run", env["SBMIG_SOURCE_ZIP"], "--out", env["SBMIG_OUT"],
               "--run-id", env["SBMIG_RUN_ID"]]
        for c in (env.get("SBMIG_CONFIG") or "").split():
            cmd += ["--config", c]
        if env.get("SBMIG_STOP_AFTER"):
            cmd += ["--stop-after", env["SBMIG_STOP_AFTER"]]
        if env.get("SBMIG_LLM_PROVIDER"):
            cmd += ["--llm-provider", env["SBMIG_LLM_PROVIDER"]]
        return cmd

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
        child_env = {**os.environ, **{k: v for k, v in env.items() if k.startswith("SBMIG_")}}
        try:
            proc = subprocess.Popen(cmd, cwd=str(self.repo_root) if self.repo_root else None, env=child_env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
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
                            sys.stdout.write(prefix + raw.decode("utf-8", errors="replace"))
                            sys.stdout.flush()
                        except (OSError, ValueError):      # a closed console never stops the job
                            self.echo = False
        except (OSError, ValueError) as e:
            log.debug("local job output pump for %s ended: %s", run_id, e)

    def job_state(self, name: str) -> JobState:
        """프로세스 상태 → JobState. 끝났으면 ``job-status.json`` 을 쓴다(run-job.sh 와 같은 신호). 모르는 이름 → Missing."""
        hit = self._procs.get(name)
        if hit is None:
            return JobState("Missing", message="no such local job")
        proc, run_dir, _env = hit
        rc = proc.poll()
        pod = f"local-{proc.pid}"
        if rc is None:
            return JobState("Running", pod=pod, message="")
        status = run_dir / "job-status.json"
        if not status.is_file():
            try:
                status.write_text(json.dumps({"exit_code": rc, "finished_at": datetime.now(timezone.utc).isoformat()}),
                                  encoding="utf-8")
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
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
        log.info("local job %s: terminated (pid %d)", name, proc.pid)
