"""웹 파드(Main)와 Job 파드(Worker)가 공유 볼륨에서 주고받는 파일의 형식과 규칙을 정하는 모듈.

Main writes only ``<run dir>/control/``; the Worker writes the rest of the run folder. ``request.json``
holds the inputs pinned at intake (zip sha256, config digests, sbmig version) and is never changed.
``state.json`` is the control state, rewritten atomically on every change. Before a Job is created, the
``creating`` state and the Job name are recorded first, so that a restarted Main adopts the Job of that
name. The Worker writes ``job-status.json`` when it ends; its run id and attempt let Main take only the
current attempt's result. The Worker compares the zip, the configs and the code with the pins and does not
run on a difference (exit 3). A Job's owner is told by the labels ``sbmig-run-id`` and ``sbmig-owner``; Main
deletes only Jobs that carry its own labels. The Main that creates the Job records the hashes of the
environment variables the configs read (``env_pins``) and the policy; the Worker compares them with its own
environment. Secret-looking names get no hash either, and ``SECRET_NAMES`` (the IVAS user id) are secret
whatever their name. A remediation resume is a new attempt of the same run with another action: its control
state is ``control/remediation-resume.json``, its result ``remediation-job-status.json``, and the
migration's files are not touched.

The resume attempt uses the same `ControlState` (with `action` set) and the same intent-before-create
discipline.
Contract: docs/INTERFACES.md "v2.1z-ac"."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from sbmig.core.models import FLOW_FULL

CONTROL_DIR = "control"                     # <run dir>/control/ 폴더. Main만 기록한다
REQUEST_FILE = "request.json"               # 접수할 때 고정한 입력이라 바꾸지 않는다
STATE_FILE = "state.json"                   # Main의 제어 상태. 상태가 바뀔 때마다 원자적으로 다시 쓴다
JOB_STATUS_FILE = "job-status.json"         # Worker가 끝날 때 <run dir>에 기록하고 runs.py가 같은 이름으로 읽는다
WORKER_MODULE = "sbmig.web.worker"
WORKER_COMMAND = ["python", "-m", WORKER_MODULE]      # Job 템플릿(81)과 LocalJobClient가 실행하는 명령
RUN_LABEL = "sbmig-run-id"                  # 실행 id를 담는 Job, 파드 라벨
OWNER_LABEL = "sbmig-owner"                 # 요청마다 난수를 넣는 Job, 파드 라벨. Main은 이 값으로 자기 Job만 지운다
# Main이 Job마다 넣어 주는 실행별 환경 변수. 나머지는 템플릿의 리터럴, ConfigMap, Secret이 채운다.
# SBMIG_JOB_SLOT은 슬롯 번호(1부터), SBMIG_WORKER_ACTION은 할 일(비어 있으면 이관)이다.
# SBMIG_FLOW는 full이나 remediation, SBMIG_JOB_ATTEMPT는 이 Job이 맡은 시도 번호다
RUN_ENV = ("SBMIG_SOURCE_ZIP", "SBMIG_OUT", "SBMIG_RUN_ID", "SBMIG_CONFIG", "SBMIG_STOP_AFTER", "SBMIG_LLM_PROVIDER",
           "SBMIG_JOB_SLOT", "SBMIG_WORKER_ACTION", "SBMIG_FLOW", "SBMIG_JOB_ATTEMPT")
FLOW_ENV = "SBMIG_FLOW"                     # Main이 기록하고 Worker가 request.json의 flow와 비교한다
SLOT_ENV = "SBMIG_JOB_SLOT"                 # Main이 기록하고 Worker가 읽는다
# Main이 넘기는 시도 번호. Worker가 제어 상태를 읽기 전에 거절해도 결과 파일에 이 번호를 기록한다
ATTEMPT_ENV = "SBMIG_JOB_ATTEMPT"
# 취약점 조치 재개 시도
ACTION_ENV = "SBMIG_WORKER_ACTION"          # 비어 있으면 이관(sbmig run)을 한다. 값이 있으면 아래 action 중 하나다
ACTION_REMEDIATION_RESUME = "remediation-resume"
WORKER_ACTIONS = (ACTION_REMEDIATION_RESUME,)
RESUME_STATE_FILE = "remediation-resume.json"         # control/ 안의 재개 시도 제어 상태. Main만 기록한다
RESUME_STATUS_FILE = "remediation-job-status.json"    # 재개 Worker의 결과. 이관의 job-status.json과 따로 둔다
MAX_PARALLEL_ENV = "SBMIG_JOB_MAX_PARALLEL"  # 웹 파드가 읽는 동시 Job 최대 수. 기본 1, Job 모드에서만 쓴다
LLM_RECORDS_ENV = "SBMIG_WORKER_LLM_RECORDS"  # "1"이면 Worker가 llm.record_dir을 유지한다. 기본은 끈다
# 이 단어가 들어간 환경 변수는 비밀 값으로 보고 로그와 보고서에서 마스킹한다
SECRET_NAME_WORDS = ("TOKEN", "PASSWORD", "PASSWD", "SECRET", "API_KEY")
# 이 길이 이상인 값만 비밀 값 목록에 넣는다. 더 짧은 값은 독립된 토큰일 때만 가린다(redact._compile).
# 짧은 값을 문자열 그대로 바꾸면 로그의 다른 문자열까지 가려지기 때문이다
MIN_SECRET_LEN = 6
# 이름에 위 단어가 없어도 비밀로 다루는 환경 변수. 회사 규칙상 IVAS 사용자, 비밀번호, 토큰은 모두 비밀이다.
# 조치는 빌드 파드에서 실행돼서 Job 로그에 남을 수 있다. 아이디는 짧을 수 있어서 최소 길이를 따로 둔다
SECRET_NAMES = frozenset({"IVAS_USER_ID"})
MIN_NAMED_SECRET_LEN = 4                    # SECRET_NAMES 값을 마스킹하는 최소 길이
# 환경 변수 고정 검사
ENV_PIN_POLICY_ENV = "SBMIG_JOB_ENV_PIN"    # Worker 환경이 다를 때의 정책. refuse, warn, off 중 하나
ENV_PIN_REFUSE, ENV_PIN_WARN, ENV_PIN_OFF = "refuse", "warn", "off"
ENV_PIN_POLICIES = (ENV_PIN_REFUSE, ENV_PIN_WARN, ENV_PIN_OFF)
# 파드마다 달라도 되는 이름. LLM 원문 기록 폴더는 진단용이고 Worker는 SBMIG_WORKER_LLM_RECORDS로 따로 켠다.
# SBMIG_REMEDIATION__REQUIRE_APPROVAL은 없어진 설정이다. ConfigMap에 남아 있어도 Job을 거절하지 않도록 뺀다
ENV_PIN_SKIP = frozenset({"SBMIG_LLM__RECORD_DIR", "LLM_RECORD_DIR", "SBMIG_REMEDIATION__REQUIRE_APPROVAL"})
# Worker는 KB 위치를 SBMIG_KB_DIR로 받는다. 이 값은 웹 파드의 SBMIG_KB__DIR과 같아야 한다.
# 다르면 Worker가 이미지 안 yml의 경로에 KB를 기록한다. 그 KB는 파드와 함께 사라진다
ENV_PIN_ALIASES: dict[str, tuple[str, ...]] = {"SBMIG_KB__DIR": ("SBMIG_KB_DIR",)}
ENV_PIN_HASH_LEN = 16


class RunState(str, Enum):
    """Main이 관리하는 실행의 제어 상태를 나타내는 열거형.
    ``queued`` -> ``creating`` -> ``created`` -> ``running`` 순서로 바뀐다.
    끝나면 finished, stopped, lost, failed 중 하나가 된다.
    ``creating``은 Job 생성 요청을 보냈지만 응답을 아직 확인하지 못한 상태다."""
    QUEUED = "queued"
    CREATING = "creating"
    CREATED = "created"
    RUNNING = "running"
    FINISHED = "finished"
    STOPPED = "stopped"
    LOST = "lost"
    FAILED = "failed"


TERMINAL_STATES = frozenset({RunState.FINISHED, RunState.STOPPED, RunState.LOST, RunState.FAILED})


def state_file_for(action: str | None) -> str:
    """action에 맞는 Main 제어 상태 파일 이름을 반환한다.
    이관(None)은 ``state.json``, 조치 재개는 ``remediation-resume.json``이다."""
    return RESUME_STATE_FILE if action == ACTION_REMEDIATION_RESUME else STATE_FILE


def status_file_for(action: str | None) -> str:
    """action에 맞는 Worker 결과 파일 이름을 반환한다.
    이관은 ``job-status.json``, 조치 재개는 ``remediation-job-status.json``이다."""
    return RESUME_STATUS_FILE if action == ACTION_REMEDIATION_RESUME else JOB_STATUS_FILE


def utc_now() -> str:
    """현재 시각을 UTC ISO 8601 문자열(초 단위, Z)로 반환한다."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def control_dir(run_dir: Path) -> Path:
    """``<run dir>/control`` 경로를 반환한다. 폴더가 없으면 만든다."""
    d = Path(run_dir) / CONTROL_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """JSON을 파일에 원자적으로 기록한다. 같은 폴더의 임시 파일에 쓴 뒤 ``os.replace``로 바꾼다.
    그래서 NFS에서도 읽는 쪽이 반쯤 쓰인 JSON을 보지 않는다."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, default=str, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> dict[str, Any] | None:
    """JSON 객체를 파일에서 읽는다. 파일이 없거나 깨졌거나 객체가 아니면 None을 반환한다."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    """파일의 sha256을 hex 문자열로 반환한다. 큰 파일도 조금씩 읽어서 계산한다."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def config_digests(repo_root: Path, configs: list[str]) -> dict[str, str]:
    """``config/default.yml``과 선택한 설정 파일들의 sha256을 이름별로 반환한다. 없는 파일은 ``"missing"``이다.
    Main이 기록하면 Worker가 같은 경로에서 다시 계산해 비교한다."""
    d = Path(repo_root) / "config"
    out: dict[str, str] = {}
    for name in ["default.yml", *configs]:
        p = d / name
        out[name] = sha256_file(p) if p.is_file() else "missing"
    return out


BUILD_DIGEST_LEN = 12
# 빌드 식별값에 넣는 파일. 패키지의 .py, 함께 배포되는 config/*.yml, knowledge/ 아래 전부를 넣는다
BUILD_DIGEST_PARTS = (("sbmig", "**/*.py"), ("config", "*.yml"), ("knowledge", "**/*"))
_build_digest_cache: dict[str, str] = {}


def build_digest(root: Path | None = None) -> str:
    """실행 중인 코드의 짧은 해시를 반환한다. 같은 체크아웃이나 이미지면 Main과 Worker가 같은 값을 얻는다.
    - 상대 경로 순으로 경로와 파일 내용을 넣는다
    - 점으로 시작하는 이름(.env 등), ``__pycache__``, ``out/``은 넣지 않는다
    - 읽을 수 없는 파일은 경로와 표시만 넣는다
    - 프로세스와 root마다 한 번만 계산한다

    `__version__` alone never changes, so without this a web pod and a build pod running different images
    could not be told apart."""
    base = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    key = str(base)
    if key in _build_digest_cache:
        return _build_digest_cache[key]
    files: set[Path] = set()
    for top, pattern in BUILD_DIGEST_PARTS:
        d = base / top
        if not d.is_dir():
            continue
        try:
            found = list(d.glob(pattern))
        except OSError:
            found = []
        for p in found:
            rel = p.relative_to(base)
            if any(part.startswith(".") or part in ("__pycache__", "out") for part in rel.parts):
                continue
            if p.is_file():
                files.add(rel)
    h = hashlib.sha256()
    for rel in sorted(files, key=lambda r: r.as_posix()):
        h.update(rel.as_posix().encode("utf-8") + b"\0")
        try:
            h.update(hashlib.sha256((base / rel).read_bytes()).digest())
        except OSError:
            h.update(b"<unreadable>")
    value = h.hexdigest()[:BUILD_DIGEST_LEN]
    _build_digest_cache[key] = value
    return value


def sbmig_version() -> str:
    """sbmig 버전과 코드 해시(``0.1.0+<12자>``)를 반환한다. Main과 Worker가 같은 이미지인지 확인할 때 쓴다.
    빌드 인자가 없어도 같은 이미지면 같은 값이다. 다른 빌드면 값이 다르다."""
    try:
        from sbmig import __version__
    except ImportError:  # pragma: no cover
        return "unknown"
    try:
        digest = build_digest()
    except Exception:  # noqa: BLE001 - 해시를 만들지 못해도 버전 비교는 계속한다
        digest = "unknown"
    return f"{__version__}+{digest}"


def new_owner_token() -> str:
    """요청 하나의 소유 토큰을 만든다. 라벨 값으로 쓸 수 있는 16자 hex 문자열이다."""
    return secrets.token_hex(8)


def is_secret_name(name: str) -> bool:
    """환경 변수 이름이 비밀 값으로 보이는지 확인한다.
    이름에 ``SECRET_NAME_WORDS``의 단어가 있으면 비밀로 본다. ``SECRET_NAMES``에 있는 이름도 비밀이다.
    설정 덮어쓰기 ``SBMIG_<섹션>__<키>``는 비밀로 보지 않는다. 이름에 TOKEN이 든 ``SBMIG_LLM__MAX_OUTPUT_TOKENS``도 그렇다."""
    up = str(name).upper()
    if up.startswith("SBMIG_") and "__" in up:
        return False
    return up in SECRET_NAMES or any(w in up for w in SECRET_NAME_WORDS)


def secret_env_names(environ: dict[str, str] | None = None) -> list[str]:
    """비밀로 보이는 환경 변수 이름을 정렬해서 반환한다."""
    env = os.environ if environ is None else environ
    return sorted(k for k in env if is_secret_name(k))


def secret_values(environ: dict[str, str] | None = None, min_len: int = MIN_SECRET_LEN) -> list[str]:
    """비밀로 보이는 환경 변수의 값 목록을 반환한다.
    ``min_len`` 이상인 값만 담는다. 긴 값부터 정렬해서 일부만 가려진 채 남는 값이 없게 한다.
    ``SECRET_NAMES``의 값은 ``MIN_NAMED_SECRET_LEN`` 길이부터 넣는다. 짧은 계정 아이디도 가린다."""
    env = os.environ if environ is None else environ
    vals = set()
    for k in secret_env_names(env):
        floor = min(min_len, MIN_NAMED_SECRET_LEN) if k.upper() in SECRET_NAMES else min_len
        if len(env.get(k) or "") >= floor:
            vals.add(env[k])
    return sorted(vals, key=len, reverse=True)


def env_pin_policy(value: str | None, default: str = ENV_PIN_REFUSE) -> str:
    """``SBMIG_JOB_ENV_PIN`` 값을 refuse, warn, off 중 하나로 바꾼다.
    공백과 대소문자는 무시한다. 비었거나 모르는 값이면 ``default``를 반환한다."""
    # 깨진 state.json에 숫자나 목록이 들어 있어도 예외 없이 default로 처리한다
    v = value.strip().lower() if isinstance(value, str) else ""
    return v if v in ENV_PIN_POLICIES else default


def config_env_names(repo_root: Path, configs: list[str]) -> set[str]:
    """설정 파일의 ``${VAR}`` 플레이스홀더가 읽는 환경 변수 이름을 모아 반환한다.
    ``config/default.yml``과 선택한 설정을 본다. yml로 읽은 값만 보므로 주석은 세지 않는다.
    yml로 읽히지 않는 파일은 텍스트 전체에서 찾는다. 없는 파일은 건너뛴다."""
    import yaml          # 설정 모듈 없이도 이 모듈을 쓸 수 있도록 함수 안에서 import한다

    from sbmig.core.config import placeholder_names
    names: set[str] = set()
    for name in ["default.yml", *configs]:
        try:
            text = (Path(repo_root) / "config" / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            data: Any = yaml.safe_load(text)
        except yaml.YAMLError:
            data = text
        names |= placeholder_names(data)
    return names


def env_pin_value(name: str, environ: dict[str, str] | None = None) -> str | None:
    """고정할 환경 변수 하나의 값을 반환한다. 그 변수가 없으면 ``ENV_PIN_ALIASES``의 다른 이름을 찾는다.
    둘 다 없으면 None을 반환한다. 빈 값으로 설정된 변수는 ``""``를 반환한다. None과는 다르다."""
    env = os.environ if environ is None else environ
    for key in (name, *ENV_PIN_ALIASES.get(name, ())):
        if key in env:
            return env[key]
    return None


def env_pin_names(repo_root: Path, configs: list[str], environ: dict[str, str] | None = None) -> list[str]:
    """고정할 환경 변수 이름 목록을 만든다.
    - 설정 파일의 플레이스홀더를 모은다
    - 이 환경의 ``SBMIG_<섹션>__<키>``와 ``SBMIG_<섹션>`` 덮어쓰기를 모은다
    - 덮어쓰기 값 안의 플레이스홀더와 별칭으로만 설정된 이름도 넣는다
    - 비밀로 보이는 이름과 ``ENV_PIN_SKIP``은 뺀다. 공유 볼륨에 비밀 값의 해시를 남기지 않는다

    A whole-section `SBMIG_<SECTION>` is read by `load_config` by the same rule, hence pinned too."""
    from sbmig.core.config import Config, placeholder_names
    env = os.environ if environ is None else environ
    names = config_env_names(repo_root, configs)
    sections = frozenset(Config.model_fields)
    for key, val in env.items():
        if not key.startswith("SBMIG_"):
            continue
        # 밑줄 두 개가 있으면 설정 키 하나를 덮어쓴다. 없으면 최상위 섹션 이름일 때만 설정으로 본다
        if "__" in key or key[len("SBMIG_"):].lower() in sections:
            names.add(key)
            names |= placeholder_names(val)
    names |= {name for name, aliases in ENV_PIN_ALIASES.items() if any(a in env for a in aliases)}
    return sorted(n for n in names if n not in ENV_PIN_SKIP and not is_secret_name(n))


def env_pins(repo_root: Path, configs: list[str], environ: dict[str, str] | None = None) -> dict[str, str]:
    """이 환경의 고정 해시를 만든다. ``env_pin_names`` 중 설정된 이름마다 값의 sha256 앞 ``ENV_PIN_HASH_LEN``자를 담는다.
    값 자체는 기록하지 않는다. 설정되지 않은 이름은 빠진다.
    한쪽에만 있는 이름은 ``compare_env_pins``가 알려 준다."""
    out: dict[str, str] = {}
    for name in env_pin_names(repo_root, configs, environ):
        val = env_pin_value(name, environ)
        if val is not None:
            out[name] = hashlib.sha256(val.encode("utf-8", errors="replace")).hexdigest()[:ENV_PIN_HASH_LEN]
    return out


def compare_env_pins(pinned: dict[str, str], actual: dict[str, str]) -> dict[str, list[str]]:
    """Main의 해시와 Worker의 해시를 비교해 차이 나는 이름만 반환한다.
    ``differs``는 값이 다른 이름이다. ``main_only``와 ``worker_only``는 한쪽에만 설정된 이름이다."""
    pinned, actual = dict(pinned or {}), dict(actual or {})
    return {"differs": sorted(k for k in pinned.keys() & actual.keys() if pinned[k] != actual[k]),
            "main_only": sorted(pinned.keys() - actual.keys()),
            "worker_only": sorted(actual.keys() - pinned.keys())}


@dataclass
class RequestRecord:
    """접수할 때 고정한 입력(``control/request.json``)을 담는 클래스. 한 번 기록하면 바꾸지 않는다.
    Worker가 ``zip_sha256``, ``config_digests``, ``sbmig_version``을 다시 계산해 비교한다. 다르면 실행하지 않는다."""
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
    # 실행 방식(sbmig.core.models.RUN_FLOWS). 이 키가 없는 이전 request.json은 full로 읽는다
    flow: str = FLOW_FULL
    # 사용자가 올린 파일 이름 그대로다. ``upload``는 파일 시스템에 맞게 다듬은 이름이다. 이전 기록에는 이 값이 없다
    upload_name: str | None = None
    # 업로드할 때 입력한 프로젝트명. 실행 id 앞부분과 같다. 화면, 관리 페이지, 보고서 제목에 보여 주기만 한다.
    # Worker는 이 값을 검사하지 않는다. 이전 기록에는 이 값이 없다
    project: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> RequestRecord:
        known = set(cls.__dataclass_fields__)                   # 새 버전이 기록한 모르는 키는 무시한다
        rec = cls(**{k: v for k, v in data.items() if k in known})
        if not rec.flow:                                        # flow가 null이나 빈 문자열이면 이전 기록처럼 full로 처리한다
            rec.flow = FLOW_FULL
        return rec

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


# 결과 ZIP을 내려받을 때의 이름 끝. 이관 결과는 migration, 취약점 조치 결과는 remediation이다
ZIP_KINDS = ("migration", "remediation")
# 내려받는 이름 앞부분에 쓸 수 있는 값. 업로드 때 검사한 프로젝트명과 실행 id가 모두 여기에 맞는다
_ZIP_BASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")


def zip_download_name(run_id: str, kind: str, project: str | None = None) -> str:
    """결과 ZIP을 사람이 내려받을 때 쓰는 이름 ``<프로젝트명>-<kind>.zip``을 반환한다. 파일을 읽지 않는다.
    프로젝트명을 쓸 수 없으면 실행 id를, 둘 다 쓸 수 없으면 ``result.zip``을 쓴다.
    ``kind``가 ``ZIP_KINDS``에 없으면 ValueError를 던진다. 실행 폴더 안의 파일 이름은 그대로다.

    A chained remediation's id is the migration run id, a standalone one its own id."""
    if kind not in ZIP_KINDS:
        raise ValueError(f"unknown result kind: {kind!r}")
    for base in (project, run_id):
        if isinstance(base, str) and _ZIP_BASE_RE.match(base):
            return f"{base}-{kind}.zip"
    return "result.zip"


# 웹 실행 id의 모양(<프로젝트명>-YYYYMMDD-HHMMSS-<hex>). sbmig.web.admin._RUN_SLUG와 같은 식이다
RUN_SLUG = re.compile(r"^(.+)-\d{8}-\d{6}-[0-9a-f]+$")
_JOURNAL_HEAD_LINES = 50                    # web_run 줄을 찾을 때 읽는 journal 앞부분 줄 수


def _web_run_event(run_dir: Path) -> dict[str, Any] | None:
    # 웹 화면이 journal 앞부분에 기록한 web_run 줄을 찾는다. 없거나 읽을 수 없으면 None을 반환한다
    try:
        with open(Path(run_dir) / "journal.jsonl", encoding="utf-8", errors="replace") as fh:
            for _ in range(_JOURNAL_HEAD_LINES):
                line = fh.readline()
                if not line:
                    break
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if isinstance(ev, dict) and ev.get("event") == "web_run":
                    return ev
    except OSError:
        pass
    return None


def _clean_name(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() and "\n" not in value else None


def entered_project(run_dir: Path) -> str | None:
    """업로드할 때 입력한 프로젝트명을 실행 폴더에서 찾는다. 화면에 보여 주는 데만 쓴다.
    ``request.json``(Job 모드)을 먼저 보고, 없으면 journal의 ``web_run`` 줄(in-process)을 본다.
    둘 다 없으면 None을 반환한다."""
    rec = RequestRecord.read(run_dir)
    name = _clean_name(rec.project) if rec is not None else None
    if name:
        return name
    ev = _web_run_event(run_dir)
    return _clean_name(ev.get("project")) if ev is not None else None


def display_project(run_dir: Path, run_id: str) -> str | None:
    """웹에서 올린 실행의 보고서 제목에 쓸 프로젝트명을 반환한다. 입력한 이름이 없으면 실행 id 앞부분을 쓴다.
    CLI로 직접 실행해서 웹 요청의 흔적(``request.json``이나 ``web_run`` 줄)이 없으면 None을 반환한다.

    Otherwise a CLI run's `run-...` id would read as a project called `run`."""
    name = entered_project(run_dir)
    if name:
        return name
    if not (Path(run_dir) / CONTROL_DIR / REQUEST_FILE).is_file() and _web_run_event(run_dir) is None:
        return None
    m = RUN_SLUG.match(str(run_id or ""))
    return m.group(1) if m else None


@dataclass
class ControlState:
    """Main의 제어 상태(``control/state.json``)를 담는 클래스. 상태가 바뀔 때마다 파일 전체를 원자적으로 다시 쓴다.
    Worker는 읽기만 한다. ``owner``는 ``sbmig-owner`` 라벨 값이다.
    ``env_pins``, ``env_pin_policy``, ``slot``은 Job을 만드는 Main이 creating으로 바꿀 때 기록한다."""
    run_id: str
    state: str = RunState.QUEUED.value
    attempt: int = 1
    job: str | None = None
    owner: str = field(default_factory=new_owner_token)
    accepted_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    exit_code: int | None = None
    error: str | None = None
    env_pins: dict[str, str] | None = None      # Job을 만드는 Main의 환경 해시. None이면 비교하지 않는다
    env_pin_policy: str | None = None           # refuse, warn, off 중 하나. Worker가 이 값을 따른다
    slot: int | None = None                     # 이 Job의 슬롯 번호. None이면 1번 슬롯으로 본다
    action: str | None = None                   # None이면 이관 시도, "remediation-resume"이면 조치 재개 시도
    # Job을 만드는 Main이 creating과 함께 기록하는 설정 파일 해시와 sbmig 버전. Worker는 이 값과 비교한다.
    # 이 값이 없는 이전 state.json이면 request.json의 값과 비교한다
    config_digests: dict[str, str] | None = None
    sbmig_version: str | None = None

    @property
    def terminal(self) -> bool:
        try:
            return RunState(self.state) in TERMINAL_STATES
        except ValueError:
            return False

    def transition(self, state: RunState | str, **changes: Any) -> ControlState:
        """상태를 바꾸고 updated_at을 갱신한 뒤 자신을 반환한다. 파일 저장은 ``write``가 한다."""
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

    def write(self, run_dir: Path, name: str = STATE_FILE) -> Path:
        p = control_dir(run_dir) / name
        atomic_write_json(p, self.to_json())
        return p

    @classmethod
    def read(cls, run_dir: Path, name: str = STATE_FILE) -> ControlState | None:
        data = read_json(Path(run_dir) / CONTROL_DIR / name)
        if not data or not data.get("run_id"):
            return None
        try:
            return cls.from_json(data)
        except TypeError:
            return None


@dataclass
class JobStatus:
    """Worker가 끝날 때 기록하는 ``<run dir>/job-status.json``을 담는 클래스. 종료 코드, 시각, 실행과 시도를 담는다.
    Main은 ``matches``가 참인 파일만 받는다. 이전 시도의 결과를 현재 결과로 착각하지 않기 위해서다.
    run-job.sh가 쓰는 필드 두 개짜리 파일도 읽을 수 있다."""
    exit_code: int
    finished_at: str = field(default_factory=utc_now)
    run_id: str | None = None
    attempt: int | None = None
    error: str | None = None
    sbmig_version: str | None = None
    action: str | None = None                   # 조치 재개 시도의 결과면 그 action 이름, 이관이면 비어 있다

    def matches(self, run_id: str, attempt: int) -> bool:
        return (self.run_id is None or self.run_id == run_id) and (self.attempt is None or int(self.attempt) == int(attempt))

    def to_json(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}

    def write(self, run_dir: Path, name: str = JOB_STATUS_FILE) -> Path:
        p = Path(run_dir) / name
        atomic_write_json(p, self.to_json())
        return p

    @classmethod
    def read(cls, run_dir: Path, name: str = JOB_STATUS_FILE) -> JobStatus | None:
        data = read_json(Path(run_dir) / name)
        if not data or "exit_code" not in data:
            return None
        try:
            return cls(exit_code=int(data["exit_code"]), finished_at=str(data.get("finished_at") or ""),
                       run_id=data.get("run_id"), attempt=data.get("attempt"), error=data.get("error"),
                       sbmig_version=data.get("sbmig_version"), action=data.get("action"))
        except (TypeError, ValueError):
            return None


IMAGE_DIFFERS_HINT = ("the web pod and the build pod run different images: restart the web pod after pushing an image "
                      "(and upload the project again)")
IMAGE_DIFFERS_KO = ("웹 파드와 빌드 파드가 서로 다른 이미지로 실행되고 있어 진행하지 않았습니다. "
                    "운영 담당자에게 웹 파드 재시작을 요청한 뒤 프로젝트를 다시 올려 주세요.")


def verify_request(record: RequestRecord, repo_root: Path, zip_path: Path | None, run_id: str | None = None,
                   pinned: ControlState | None = None) -> list[str]:
    """Worker가 받은 입력이 Main이 고정한 값과 같은지 확인한다. 다른 점을 문장 목록으로 반환한다. 빈 목록이면 통과다.
    - zip sha256과 run id는 ``request.json``과 비교한다. ``zip_path``가 None이면 zip 비교는 건너뛴다
    - 설정 파일 해시와 sbmig 버전은 Job을 만든 Main이 ``pinned``에 기록한 값과 비교한다
    - ``pinned``에 그 값이 없는 이전 state.json이면 ``request.json``의 값과 비교한다
    - 해시나 버전이 다르면 마지막에 ``IMAGE_DIFFERS_HINT``를 한 번 붙인다

    Comparing with the creating Main's pins accepts a run that waited across a web-pod restart, or a resume of
    a run an older image made, when Main and Worker agree now."""
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
    want_digests = pinned.config_digests if pinned is not None and pinned.config_digests is not None \
        else record.config_digests
    want_version = pinned.sbmig_version if pinned is not None and pinned.sbmig_version else record.sbmig_version
    image = False
    mine = config_digests(repo_root, list(record.configs))
    for name, want in (want_digests or {}).items():
        got = mine.get(name, "missing")
        if got != want:
            problems.append(f"config {name} differs between the web pod and the build pod")
            image = True
    if want_version and want_version != sbmig_version():
        problems.append(f"sbmig version differs: web pod {want_version}, build pod {sbmig_version()}")
        image = True
    if image:
        problems.append(IMAGE_DIFFERS_HINT)
    return problems
