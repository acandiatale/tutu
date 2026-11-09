"""Job 파드의 일생 전체 — 공유 볼륨 모드의 Python Worker (v2.1z-ac, 2026-11-09; ``deploy/docker/run-job.sh`` 의 공유 볼륨 갈래를 대신한다).

``python -m sbmig.web.worker`` 로 돈다. 환경 변수 이름은 run-job.sh 와 같다(``SBMIG_SOURCE_ZIP`` / ``SBMIG_OUT`` / ``SBMIG_RUN_ID`` 필수).
순서: Main 이 접수 때 고정한 ``control/request.json`` 을 읽고(없으면 거절) zip·설정·코드 버전을 대조한다(다르면 거절, exit 3) →
저널에 ``worker_started`` → ``worker-paths.yml``(KB 위치, LLM 원문 기록 끄기 — 배포팀의 결정: 기본으로 끈다) → ``sbmig.cli run`` 자식
프로세스(출력은 파드 로그로) → 실행 폴더의 비밀 값 지우기 → ``SBMIG-RESULT`` 줄 출력 → ``worker_finished`` → ``job-status.json``(run id ·
attempt · 버전이 들어 있어 Main 이 **지금 시도**의 결과만 믿는다). Worker 는 ``control/`` 에 쓰지 않는다(Main 의 몫).
종료 코드: 3 = 중단(거절 / sbmig exit 3 / 자식을 못 띄움), 2 = 빌드 실패이고 ``SBMIG_FAIL_JOB_ON_BUILD_FAIL=1``, 그 밖에는 0 — 빌드가
실패해도 보고서가 결과다(run-job.sh 와 같다).

The Job pod's whole life in shared-volume mode: verify the request Main pinned, run `sbmig.cli run` as a child, redact
the run dir, write `job-status.json` for this attempt. Exit 3 = stopped / refused, 2 = build failed and
`SBMIG_FAIL_JOB_ON_BUILD_FAIL=1`, else 0."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from sbmig.core.config import PACKAGE_ROOT
from sbmig.web import contract
from sbmig.web.redact import redact_run_dir

REQUIRED_ENV = ("SBMIG_SOURCE_ZIP", "SBMIG_OUT", "SBMIG_RUN_ID")
PATHS_FILE = "worker-paths.yml"
JOURNAL_FILE = "journal.jsonl"
RESULT_MARK = "SBMIG-RESULT"
REFUSE_NO_REQUEST = "control/request.json missing — the Worker runs only requests Main accepted"

RunChild = Callable[[list[str], Path, dict[str, str]], int]


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


def default_run_child(cmd: list[str], cwd: Path, env: dict[str, str]) -> int:
    """자식 프로세스를 띄우고 그 stdout / stderr 를 우리 stdout(파드 로그)으로 그대로 흘린 뒤 종료 코드를 돌려준다.

    Run the child, streaming its stdout / stderr to ours (the pod log); returns its exit code."""
    # 파드 로그는 UTF-8 로 읽힌다 — 자식이 로케일 인코딩으로 쓰다 `—` 같은 글자에서 죽지 않게(localjob 의 2026-11-05 교훈)
    child_env = {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", **env}
    proc = subprocess.Popen(cmd, cwd=str(cwd), env=child_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert proc.stdout is not None
    out = getattr(sys.stdout, "buffer", None)
    try:
        for chunk in iter(lambda: proc.stdout.readline(), b""):
            if out is not None:
                out.write(chunk)
                out.flush()
            else:
                sys.stdout.write(chunk.decode("utf-8", errors="replace"))
                sys.stdout.flush()
    except BaseException:
        proc.kill()               # Ctrl-C / SIGTERM on the pod: don't leave Maven behind before the status file is written
        proc.wait()
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


def _refuse(run_dir: Path, run_id: str, reason: str, record: contract.RequestRecord | None) -> int:
    _say(f"REFUSED: {reason}")
    _journal(run_dir, "worker_refused", {"run_id": run_id, "reason": reason})
    contract.JobStatus(exit_code=3, run_id=run_id, attempt=record.attempt if record else None, error=reason,
                       sbmig_version=contract.sbmig_version()).write(run_dir)
    return 3


def main(argv: list[str] | None = None, *, environ: dict[str, str] | None = None, run_child: RunChild | None = None) -> int:
    """Worker 진입점: 환경 변수로 받은 요청 하나를 검증하고 돌리고 결과 상태 파일을 쓴다. 종료 코드는 모듈 설명 참고.

    The Worker entry point (`python -m sbmig.web.worker`); see the module docstring for the exit codes."""
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

    record = contract.RequestRecord.read(run_dir)
    if record is None:
        return _refuse(run_dir, run_id, REFUSE_NO_REQUEST, None)
    problems = contract.verify_request(record, repo_root, Path(zip_path), run_id)
    if problems:
        return _refuse(run_dir, run_id, "; ".join(problems), record)

    version = contract.sbmig_version()
    _journal(run_dir, "worker_started", {"run_id": run_id, "attempt": record.attempt, "sbmig_version": version,
                                         "pid": os.getpid()})
    # from here on a status file must exist whatever happens (Main waits for it)
    rc: int | None = None
    try:
        paths_yml = write_paths_yml(run_dir, env)
        cmd = build_command(env.get("SBMIG_PYTHON") or sys.executable, zip_path, resolve_configs(env, record, repo_root),
                            paths_yml, out, run_id, env.get("SBMIG_STOP_AFTER") or None,
                            env.get("SBMIG_LLM_PROVIDER") or None)
        _say("sbmig run " + " ".join(cmd[4:]))
        rc = int(run_child(cmd, repo_root, env))
    except (Exception, KeyboardInterrupt) as e:  # noqa: BLE001 — whatever broke, Main must still get a status file
        error = f"the Worker could not run sbmig: {type(e).__name__}: {e}"
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
