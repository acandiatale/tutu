"""Main(웹 파드)과 Worker(Job 파드)가 공유 볼륨 위에서 주고받는 파일의 계약 (v2.1z-ac, 2026-11-09 — 배포팀의 명세를 우리 코드로).

쓰는 사람을 나눈다. **Main** 은 ``<run dir>/control/`` 만 쓴다: 접수 때 입력을 고정한 ``request.json``(zip 의 sha256, 설정 파일의
digest, sbmig 버전 — 그 뒤로 바꾸지 않는다)과 제어 상태 ``state.json``(queued → creating → created → running → finished | stopped |
lost | failed; 전이마다 원자적으로 다시 쓴다; Job 을 만들기 **전에** creating 과 Job 이름을 적어 두므로 응답이 끊겨도 다시 뜬 Main 이
같은 이름의 Job 을 찾아 이어받는다). **Worker** 는 실행 폴더의 나머지(저널·로그·보고서·result.zip)와 끝날 때 ``job-status.json`` 을 쓴다 —
여기에 run id 와 attempt 가 들어 있어 Main 은 **지금 시도**의 결과만 믿는다. Worker 는 시작할 때 ``request.json`` 의 digest 와 자기가 받은
zip·설정·코드를 대조해 다르면 돌지 않는다(exit 3). Job 의 소유는 라벨 ``sbmig-run-id`` + ``sbmig-owner``(요청마다 난수)로 확인한다 —
Main 은 자기 라벨이 맞는 Job 만 지운다.

The file contract between the Main pod and the Worker pod on the shared volume: `control/request.json` (inputs pinned at
intake), `control/state.json` (Main's control state, written atomically, intent recorded before the Job is created),
`job-status.json` (the Worker's result for *this* attempt), the ownership labels, the Worker command, and the digest /
secret-name helpers both sides use. Only Main writes under `control/`; only the Worker writes the run's outputs."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

CONTROL_DIR = "control"                     # <run dir>/control/ — Main 만 쓴다
REQUEST_FILE = "request.json"               # 접수 때 고정한 입력(불변)
STATE_FILE = "state.json"                   # Main 의 제어 상태(전이마다 원자적으로 다시 쓴다)
JOB_STATUS_FILE = "job-status.json"         # Worker 가 끝날 때 <run dir> 에 쓴다 (runs.py 가 같은 이름을 읽는다)
WORKER_MODULE = "sbmig.web.worker"
WORKER_COMMAND = ["python", "-m", WORKER_MODULE]      # Job 틀(81-web-job.template.yaml)과 LocalJobClient 의 명령
RUN_LABEL = "sbmig-run-id"                  # Job / 파드 라벨: 실행 id
OWNER_LABEL = "sbmig-owner"                 # Job / 파드 라벨: 요청마다 난수 — Main 이 자기 Job 만 지우게
LLM_RECORDS_ENV = "SBMIG_WORKER_LLM_RECORDS"  # "1" 이면 Worker 가 설정의 llm.record_dir 을 그대로 둔다(기본: 끈다 — 소스가 공유 볼륨에 더 남지 않게)
SECRET_NAME_WORDS = ("TOKEN", "PASSWORD", "PASSWD", "SECRET", "API_KEY")   # 이 말이 든 env 는 비밀로 다룬다(값은 로그·보고서에서 지운다)
MIN_SECRET_LEN = 6                          # 이보다 짧은 값은 지우지 않는다(우연한 부분 일치로 로그를 망치지 않게)


class RunState(str, Enum):
    """Main 의 제어 상태. ``queued`` 접수됨(차례 대기) → ``creating`` Job 생성 요청 직전·직후(응답 불확실 구간) → ``created`` Job 이 있음 →
    ``running`` 파드가 돈다 → 끝: ``finished``(Worker 의 결과 받음) / ``stopped``(멈춤 뒤 삭제) / ``lost``(Job 이 사라졌거나 결과 검증 실패) /
    ``failed``(Main 쪽 오류 — 생성 실패 등).

    Main's control states; `creating` is the "request sent, answer uncertain" window."""
    QUEUED = "queued"
    CREATING = "creating"
    CREATED = "created"
    RUNNING = "running"
    FINISHED = "finished"
    STOPPED = "stopped"
    LOST = "lost"
    FAILED = "failed"


TERMINAL_STATES = frozenset({RunState.FINISHED, RunState.STOPPED, RunState.LOST, RunState.FAILED})


def utc_now() -> str:
    """지금 시각, UTC ISO 8601 (초 단위, Z). / Now as ISO 8601 UTC."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def control_dir(run_dir: Path) -> Path:
    """``<run dir>/control`` (없으면 만든다). / The run's control directory."""
    d = Path(run_dir) / CONTROL_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """같은 폴더의 임시 파일에 쓰고 ``os.replace`` — 읽는 쪽이 반쪽짜리 JSON 을 보지 않게(NFS 에서도 rename 은 원자적이다).

    Write JSON through a temp file in the same directory and `os.replace` it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, default=str, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> dict[str, Any] | None:
    """JSON 객체를 읽는다; 없거나 깨졌거나 객체가 아니면 None. / A JSON object, or None."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    """파일의 sha256 hex (스트리밍). / Streaming sha256 of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def config_digests(repo_root: Path, configs: list[str]) -> dict[str, str]:
    """``config/default.yml`` 과 선택한 설정 파일들의 sha256 — 이름 → digest. 없는 파일은 ``"missing"``. Main 이 접수 때 적고 Worker 가
    같은 경로(같은 이미지·같은 ConfigMap 마운트)에서 다시 계산해 대조한다.

    sha256 of `config/default.yml` and each selected config, by file name (`"missing"` when absent)."""
    d = Path(repo_root) / "config"
    out: dict[str, str] = {}
    for name in ["default.yml", *configs]:
        p = d / name
        out[name] = sha256_file(p) if p.is_file() else "missing"
    return out


def sbmig_version() -> str:
    """이 코드의 sbmig 버전(Main 과 Worker 가 같은 이미지인지 보는 값). / This code's sbmig version."""
    try:
        from sbmig import __version__
    except ImportError:  # pragma: no cover
        return "unknown"
    return str(__version__)


def new_owner_token() -> str:
    """요청 하나의 소유 토큰(라벨 값으로 쓸 수 있는 16자 hex). / A per-request owner token (label-safe)."""
    return secrets.token_hex(8)


def secret_env_names(environ: dict[str, str] | None = None) -> list[str]:
    """이름에 ``SECRET_NAME_WORDS`` 가 든 환경 변수 이름들(정렬). / Env names that look like secrets."""
    env = os.environ if environ is None else environ
    return sorted(k for k in env if any(w in k.upper() for w in SECRET_NAME_WORDS))


def secret_values(environ: dict[str, str] | None = None, min_len: int = MIN_SECRET_LEN) -> list[str]:
    """비밀로 보이는 환경 변수의 값들(``min_len`` 이상, 긴 것부터 — 긴 값을 먼저 지워야 부분 문자열이 남지 않는다).

    The values of secret-looking env vars, longest first."""
    env = os.environ if environ is None else environ
    vals = {env[k] for k in secret_env_names(env) if len(env.get(k) or "") >= min_len}
    return sorted(vals, key=len, reverse=True)


@dataclass
class RequestRecord:
    """접수 때 고정한 입력(``control/request.json``). Worker 는 ``zip_sha256`` / ``config_digests`` / ``sbmig_version`` 을 자기 쪽에서 다시
    계산해 대조한다(``verify_request``). 한 번 쓰면 바꾸지 않는다.

    The inputs pinned at intake; the Worker recomputes the digests and refuses to run on a mismatch."""
    run_id: str
    accepted_at: str
    upload: str
    configs: list[str]
    zip_sha256: str
    config_digests: dict[str, str]
    sbmig_version: str
    mode: str
    stop_after: str | None = None
    llm_provider: str | None = None
    attempt: int = 1

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> RequestRecord:
        known = set(cls.__dataclass_fields__)                   # tolerate extra keys from a newer writer
        return cls(**{k: v for k, v in data.items() if k in known})

    def write(self, run_dir: Path) -> Path:
        p = control_dir(run_dir) / REQUEST_FILE
        atomic_write_json(p, self.to_json())
        return p

    @classmethod
    def read(cls, run_dir: Path) -> RequestRecord | None:
        data = read_json(Path(run_dir) / CONTROL_DIR / REQUEST_FILE)
        if not data:
            return None
        try:
            return cls.from_json(data)
        except TypeError:
            return None


@dataclass
class ControlState:
    """Main 의 제어 상태(``control/state.json``). ``job`` 은 결정적 이름(``job_name(run_id)``), ``owner`` 는 라벨 ``sbmig-owner`` 의 값.
    전이할 때마다 ``write`` 로 통째로 다시 쓴다(원자적).

    Main's control state for one run (atomic rewrite on every transition)."""
    run_id: str
    state: str = RunState.QUEUED.value
    attempt: int = 1
    job: str | None = None
    owner: str = field(default_factory=new_owner_token)
    accepted_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    exit_code: int | None = None
    error: str | None = None

    @property
    def terminal(self) -> bool:
        try:
            return RunState(self.state) in TERMINAL_STATES
        except ValueError:
            return False

    def transition(self, state: RunState | str, **changes: Any) -> ControlState:
        """상태를 바꾸고(updated_at 갱신) 자신을 돌려준다; 파일에는 ``write`` 로 쓴다."""
        self.state = state.value if isinstance(state, RunState) else str(state)
        for k, v in changes.items():
            setattr(self, k, v)
        self.updated_at = utc_now()
        return self

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ControlState:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})

    def write(self, run_dir: Path) -> Path:
        p = control_dir(run_dir) / STATE_FILE
        atomic_write_json(p, self.to_json())
        return p

    @classmethod
    def read(cls, run_dir: Path) -> ControlState | None:
        data = read_json(Path(run_dir) / CONTROL_DIR / STATE_FILE)
        if not data or not data.get("run_id"):
            return None
        try:
            return cls.from_json(data)
        except TypeError:
            return None


@dataclass
class JobStatus:
    """Worker 가 끝날 때 쓰는 ``<run dir>/job-status.json``: 종료 코드, 시각, 그리고 **어느 실행·시도의** 결과인지. Main 은 ``matches`` 가
    참인 파일만 결과로 받는다(옛 시도의 파일을 지금 성공으로 읽지 않게). run-job.sh 가 쓰는 두 필드짜리 파일도 읽힌다(id 없음 = 대조 생략).

    The Worker's result for one attempt; Main accepts it only when `matches(run_id, attempt)`."""
    exit_code: int
    finished_at: str = field(default_factory=utc_now)
    run_id: str | None = None
    attempt: int | None = None
    error: str | None = None
    sbmig_version: str | None = None

    def matches(self, run_id: str, attempt: int) -> bool:
        return (self.run_id is None or self.run_id == run_id) and (self.attempt is None or int(self.attempt) == int(attempt))

    def to_json(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}

    def write(self, run_dir: Path) -> Path:
        p = Path(run_dir) / JOB_STATUS_FILE
        atomic_write_json(p, self.to_json())
        return p

    @classmethod
    def read(cls, run_dir: Path) -> JobStatus | None:
        data = read_json(Path(run_dir) / JOB_STATUS_FILE)
        if not data or "exit_code" not in data:
            return None
        try:
            return cls(exit_code=int(data["exit_code"]), finished_at=str(data.get("finished_at") or ""),
                       run_id=data.get("run_id"), attempt=data.get("attempt"), error=data.get("error"),
                       sbmig_version=data.get("sbmig_version"))
        except (TypeError, ValueError):
            return None


def verify_request(record: RequestRecord, repo_root: Path, zip_path: Path | None, run_id: str | None = None) -> list[str]:
    """Worker 쪽 대조: 받은 zip 의 sha256, 설정 파일들의 digest, sbmig 버전, run id 가 ``request.json`` 과 같은지. 다른 점을 사람이 읽을
    문장으로 돌려준다(빈 목록 = 통과). zip 이 None 이면 zip 은 건너뛴다.

    The Worker's check of the pinned inputs; returns the mismatches as sentences (empty = ok)."""
    problems: list[str] = []
    if run_id and record.run_id != run_id:
        problems.append(f"run id differs: request.json says {record.run_id}, the Worker was started for {run_id}")
    if zip_path is not None:
        if not Path(zip_path).is_file():
            problems.append(f"source zip missing: {zip_path}")
        else:
            got = sha256_file(Path(zip_path))
            if got != record.zip_sha256:
                problems.append(f"source zip changed since intake (sha256 {got[:12]}… ≠ pinned {record.zip_sha256[:12]}…)")
    mine = config_digests(repo_root, list(record.configs))
    for name, want in (record.config_digests or {}).items():
        got = mine.get(name, "missing")
        if got != want:
            problems.append(f"config {name} differs from the pinned copy (Main and Worker must mount the same ConfigMap)")
    if record.sbmig_version and record.sbmig_version != sbmig_version():
        problems.append(f"sbmig version differs: Main {record.sbmig_version}, Worker {sbmig_version()} (same image tag for both pods)")
    return problems
