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
종료 코드: 3 = 중단(거절 / sbmig exit 3 / 자식을 못 띄움), 2 = 빌드 실패이고 ``SBMIG_FAIL_JOB_ON_BUILD_FAIL=1``, 그 밖에는 0 — 빌드가
실패해도 보고서가 결과다(run-job.sh 와 같다).

The Job pod's whole life in shared-volume mode: verify the request Main pinned, run `sbmig.cli run` as a child (which
redacts its own log / journal lines as it writes them; its console output is redacted line by line on the way to the pod
log), redact the run dir, write `job-status.json` for this attempt. Exit 3 = stopped / refused, 2 = build failed and
`SBMIG_FAIL_JOB_ON_BUILD_FAIL=1`, else 0. Review 2026-11-10: when the Main that created the Job pinned its environment
(`env_pins` + policy in `control/state.json`), the Worker recomputes the pins and checks the per-run values against
`request.json` before it starts — refuse → exit 3, warn → journal `worker_env_differs` and go on; names only, never a
value or a hash."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

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
    # from here on a status file must exist whatever happens (Main waits for it)
    rc: int | None = None
    try:
        paths_yml = write_paths_yml(run_dir, env)
        cmd = build_command(env.get("SBMIG_PYTHON") or sys.executable, zip_path, resolve_configs(env, record, repo_root),
                            paths_yml, out, run_id, env.get("SBMIG_STOP_AFTER") or None,
                            env.get("SBMIG_LLM_PROVIDER") or None)
        _say(clean("sbmig run " + " ".join(cmd[4:])))
        rc = int(run_child(cmd, repo_root, child_environ(env)))
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
