"""웹 파드(Main)와 Job 파드(Worker)가 공유 볼륨에서 주고받는 파일의 형식과 규칙을 정하는 모듈.
- Main은 ``<run dir>/control/``에만 기록한다. ``request.json``은 접수할 때 고정한 입력(zip sha256, 설정 파일 해시, sbmig
  버전)이라 이후 바꾸지 않고, ``state.json``은 제어 상태라 상태가 바뀔 때마다 원자적으로 다시 쓴다
- Job을 만들기 전에 creating 상태와 Job 이름을 먼저 기록한다.
  응답이 끊겨도 다시 뜬 Main이 같은 이름의 Job을 찾아 이어받기 위해서다
- Worker는 실행 폴더의 나머지 파일과, 끝날 때 ``job-status.json``을 기록한다.
  이 파일에 run id와 attempt가 있어 Main은 현재 시도의 결과만 받는다
- Worker는 시작할 때 받은 zip, 설정, 코드를 ``request.json``의 해시와 비교하고, 다르면 실행하지 않는다(exit 3)
- Job의 소유자는 라벨 ``sbmig-run-id``와 ``sbmig-owner``로 확인하고, Main은 자기 라벨이 붙은 Job만 지운다
- Job을 만드는 Main은 설정이 읽는 환경 변수 값의 해시(``env_pins``)와 정책(``SBMIG_JOB_ENV_PIN``)을
  기록하고, Worker가 ``compare_env_pins``로 자기 환경과 비교한다. 비밀로 보이는 이름은 기록하지 않는다
- 취약점 조치 재개는 같은 실행에서 action만 다른 새 시도다. 제어 상태는 ``control/remediation-resume.json``,
  결과는 ``remediation-job-status.json``에 기록하고 이관의 파일은 건드리지 않는다

The file contract between the Main pod and the Worker pod on the shared volume: `control/request.json` (inputs pinned at
intake), `control/state.json` (Main's control state, written atomically, intent recorded before the Job is created),
`job-status.json` (the Worker's result for *this* attempt), the ownership labels, the Worker command, and the digest /
secret-name helpers both sides use. Only Main writes under `control/`; only the Worker writes the run's outputs.
Review 2026-11-10: the Main that creates the Job also records fingerprints of the environment values the configuration
reads (`env_pins`, no secret-looking names) and a policy in `state.json`; the Worker recomputes and compares them.
2026-11-11: `SECRET_NAMES` (the IVAS user id) is secret regardless of the name words — redacted, never pinned.
2026-11-11 (wave 4): a chained remediation is resumed in a NEW build pod as another attempt of the same run with a distinct
action (`SBMIG_WORKER_ACTION=remediation-resume`): Main keeps that attempt's control state in `control/remediation-resume.json`
(same `ControlState`, `action` set, same intent-before-create discipline) and the Worker writes `remediation-job-status.json` —
the migration's `control/state.json` / `job-status.json` are never touched by a resume."""
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

CONTROL_DIR = "control"                     # <run dir>/control/ 폴더이며 Main만 기록한다
REQUEST_FILE = "request.json"               # 접수할 때 고정한 입력이므로 바꾸지 않는다
STATE_FILE = "state.json"                   # Main의 제어 상태이며 상태가 바뀔 때마다 원자적으로 다시 쓴다
JOB_STATUS_FILE = "job-status.json"         # Worker가 끝날 때 <run dir>에 기록하고 runs.py가 같은 이름으로 읽는다
WORKER_MODULE = "sbmig.web.worker"
WORKER_COMMAND = ["python", "-m", WORKER_MODULE]      # Job 템플릿(81-web-job.template.yaml)과 LocalJobClient가 실행하는 명령
RUN_LABEL = "sbmig-run-id"                  # Job / 파드 라벨로 실행 id를 담는다
OWNER_LABEL = "sbmig-owner"                 # Job / 파드 라벨이며 요청마다 난수를 넣어 Main이 자기 Job만 지우게 한다
# Main이 Job마다 넣어 주는 실행별 환경 변수 이름이다.
# 나머지 환경 변수는 템플릿의 리터럴, ConfigMap, Secret이 채운다
# SBMIG_JOB_SLOT은 슬롯 번호(1부터), SBMIG_WORKER_ACTION은 할 일(비어 있으면 이관), SBMIG_FLOW는 실행
# 방식(full | remediation)이다. SBMIG_JOB_ATTEMPT는 이 Job이 맡은 시도 번호다
RUN_ENV = ("SBMIG_SOURCE_ZIP", "SBMIG_OUT", "SBMIG_RUN_ID", "SBMIG_CONFIG", "SBMIG_STOP_AFTER", "SBMIG_LLM_PROVIDER",
           "SBMIG_JOB_SLOT", "SBMIG_WORKER_ACTION", "SBMIG_FLOW", "SBMIG_JOB_ATTEMPT")
FLOW_ENV = "SBMIG_FLOW"                     # Main이 기록하고 Worker가 request.json의 flow와 비교한다
SLOT_ENV = "SBMIG_JOB_SLOT"                 # Main이 기록하고 Worker가 읽는다
# Worker가 제어 상태를 읽기 전에 거절해도 상태 파일에 자기 시도 번호를 적도록 Main이 넘긴다
ATTEMPT_ENV = "SBMIG_JOB_ATTEMPT"
# 취약점 조치 재개 시도
ACTION_ENV = "SBMIG_WORKER_ACTION"          # 비어 있으면 이관(sbmig run)을 하고, 아니면 아래 action 중 하나를 한다
ACTION_REMEDIATION_RESUME = "remediation-resume"
WORKER_ACTIONS = (ACTION_REMEDIATION_RESUME,)
RESUME_STATE_FILE = "remediation-resume.json"         # control/ 안에 두는 재개 시도의 제어 상태이며 Main만 기록한다
RESUME_STATUS_FILE = "remediation-job-status.json"    # 재개 Worker의 결과이며 이관의 job-status.json과 따로 둔다
MAX_PARALLEL_ENV = "SBMIG_JOB_MAX_PARALLEL"  # 웹 파드가 읽는 동시 Job 최대 수(기본 1, Job 모드에서만 쓴다)
LLM_RECORDS_ENV = "SBMIG_WORKER_LLM_RECORDS"  # "1"이면 Worker가 llm.record_dir을 유지하고, 기본은 끈다
SECRET_NAME_WORDS = ("TOKEN", "PASSWORD", "PASSWD", "SECRET", "API_KEY")   # 이 단어가 들어간 환경 변수는 비밀 값으로 보고 로그와 보고서에서 마스킹한다
MIN_SECRET_LEN = 6                          # 우연한 부분 일치로 로그가 망가지지 않도록 이보다 짧은 값은 마스킹하지 않는다
# 이름에 위 단어가 없어도 비밀로 다루는 환경 변수다. 회사 규칙상 IVAS 사용자, 비밀번호, 토큰은 모두
# 비밀이고, 조치가 빌드 파드에서 실행되므로 Job 로그에 남을 수 있다
# 아이디는 짧을 수 있으므로 최소 길이를 따로 둔다
SECRET_NAMES = frozenset({"IVAS_USER_ID"})
MIN_NAMED_SECRET_LEN = 4                    # SECRET_NAMES 값을 마스킹하는 최소 길이
# 환경 변수 고정 검사
ENV_PIN_POLICY_ENV = "SBMIG_JOB_ENV_PIN"    # Worker 환경이 다를 때의 정책: refuse | warn | off
ENV_PIN_REFUSE, ENV_PIN_WARN, ENV_PIN_OFF = "refuse", "warn", "off"
ENV_PIN_POLICIES = (ENV_PIN_REFUSE, ENV_PIN_WARN, ENV_PIN_OFF)
# 파드마다 달라도 되는 이름이다.
# LLM 원문 기록 폴더는 진단용이고 Worker는 SBMIG_WORKER_LLM_RECORDS로 따로 켠다
# SBMIG_REMEDIATION__REQUIRE_APPROVAL은 없어진 설정이다. ConfigMap에 남아 있어도 Job을 거절하지 않게 비교에서 뺀다
ENV_PIN_SKIP = frozenset({"SBMIG_LLM__RECORD_DIR", "LLM_RECORD_DIR", "SBMIG_REMEDIATION__REQUIRE_APPROVAL"})
# Worker는 KB 위치를 SBMIG_KB_DIR로 받으므로 웹 파드의 SBMIG_KB__DIR과 같은 값이어야 한다.
# 다르면 Worker가 이미지 안 yml의 경로에 KB를 기록해 파드와 함께 사라진다
ENV_PIN_ALIASES: dict[str, tuple[str, ...]] = {"SBMIG_KB__DIR": ("SBMIG_KB_DIR",)}
ENV_PIN_HASH_LEN = 16


class RunState(str, Enum):
    """Main이 관리하는 실행의 제어 상태를 나타내는 열거형.
    ``queued`` -> ``creating`` -> ``created`` -> ``running`` 순으로 진행하고 ``finished`` / ``stopped`` /
    ``lost`` / ``failed`` 중 하나로 끝난다. ``creating``은 Job 생성 요청의 응답을 아직 확인하지 못한 상태다.

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


def state_file_for(action: str | None) -> str:
    """action에 맞는 Main 제어 상태 파일 이름을 반환한다.
    이관(None)은 ``state.json``, 조치 재개는 ``remediation-resume.json``이다.

    The control-state file name of an action: the migration's `state.json`, a remediation resume's own file."""
    return RESUME_STATE_FILE if action == ACTION_REMEDIATION_RESUME else STATE_FILE


def status_file_for(action: str | None) -> str:
    """action에 맞는 Worker 결과 파일 이름을 반환한다.
    이관은 ``job-status.json``, 조치 재개는 ``remediation-job-status.json``이다.

    The Worker's result file name of an action (the resume never overwrites the migration's `job-status.json`)."""
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
    """JSON을 파일에 원자적으로 기록한다. 같은 폴더의 임시 파일에 쓴 뒤 ``os.replace``로 바꾸므로, NFS에서도
    읽는 쪽이 반쯤 쓰인 JSON을 보지 않는다.

    Write JSON through a temp file in the same directory and `os.replace` it."""
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
    """파일의 sha256을 스트리밍으로 계산해 hex 문자열로 반환한다."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def config_digests(repo_root: Path, configs: list[str]) -> dict[str, str]:
    """``config/default.yml``과 선택한 설정 파일들의 sha256을 이름별로 반환한다.
    없는 파일은 ``"missing"``으로 기록한다.
    Main이 접수할 때 기록하고, Worker가 같은 경로에서 다시 계산해 비교한다.

    sha256 of `config/default.yml` and each selected config, by file name (`"missing"` when absent)."""
    d = Path(repo_root) / "config"
    out: dict[str, str] = {}
    for name in ["default.yml", *configs]:
        p = d / name
        out[name] = sha256_file(p) if p.is_file() else "missing"
    return out


BUILD_DIGEST_LEN = 12
# 빌드 식별값에 넣는 파일이다. 패키지의 .py, 함께 배포되는 config/*.yml, knowledge/ 아래 전부를 넣는다
BUILD_DIGEST_PARTS = (("sbmig", "**/*.py"), ("config", "*.yml"), ("knowledge", "**/*"))
_build_digest_cache: dict[str, str] = {}


def build_digest(root: Path | None = None) -> str:
    """실행 중인 코드 트리의 짧은 지문을 반환한다. 같은 체크아웃이나 이미지면 Main과 Worker가 같은 값을 얻는다.
    상대 경로를 정렬해 경로와 파일 바이트를 넣고, 점으로 시작하는 이름(.env 등)과 ``__pycache__``, ``out/``은 넣지 않는다.
    읽을 수 없는 파일은 경로와 표시만 넣는다. 프로세스마다 한 번만 계산한다.

    A short digest of the running code tree (the package's `*.py`, the shipped `config/*.yml`, `knowledge/**`): sorted
    relative paths + file bytes; dot-names (`.env`), `__pycache__` and `out/` never; an unreadable file contributes its
    path and a marker. Computed once per process and root (review 2026-11-18, W2 / R1: `__version__` alone never changes,
    so a web pod and a build pod running different images could not be told apart)."""
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
    """이 코드의 sbmig 버전과 코드 트리 지문(``0.1.0+<12자>``)을 반환한다. Main과 Worker가 같은 이미지인지 확인할 때 쓴다.

    The package version plus the running tree's digest (`build_digest`), e.g. `0.1.0+3f2a9c1b0d4e`: the same checkout or
    image gives the same value in Main and Worker without a build argument; two different builds differ."""
    try:
        from sbmig import __version__
    except ImportError:  # pragma: no cover
        return "unknown"
    try:
        digest = build_digest()
    except Exception:  # noqa: BLE001 - 지문을 만들지 못해도 버전 비교 자체는 이어 간다
        digest = "unknown"
    return f"{__version__}+{digest}"


def new_owner_token() -> str:
    """요청 하나의 소유 토큰을 만든다. 라벨 값으로 쓸 수 있는 16자 hex 문자열이다."""
    return secrets.token_hex(8)


def is_secret_name(name: str) -> bool:
    """환경 변수 이름이 비밀 값으로 보이는지 확인한다.
    이름에 ``SECRET_NAME_WORDS``의 단어가 들어 있거나 ``SECRET_NAMES``에 있으면 비밀로 본다.
    설정 덮어쓰기 ``SBMIG_<섹션>__<키>``는 비밀을 담지 않으므로 제외한다.
    예를 들어 ``SBMIG_LLM__MAX_OUTPUT_TOKENS``는 마스킹하지 않는다.

    Whether an env name looks like a secret (contains one of `SECRET_NAME_WORDS`, or is one of `SECRET_NAMES`). A config override
    `SBMIG_<SECTION>__<KEY>` never is: no config key holds a credential, so `SBMIG_LLM__MAX_OUTPUT_TOKENS` is neither redacted nor
    refused as a literal."""
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
    ``min_len`` 이상인 값만 담고, 부분 문자열이 남지 않도록 긴 값부터 정렬한다.
    ``SECRET_NAMES``의 값은 짧은 계정 아이디도 가리도록 ``MIN_NAMED_SECRET_LEN`` 길이부터 포함한다.

    The values of secret-looking env vars, longest first; a `SECRET_NAMES` value from `MIN_NAMED_SECRET_LEN` characters on."""
    env = os.environ if environ is None else environ
    vals = set()
    for k in secret_env_names(env):
        floor = min(min_len, MIN_NAMED_SECRET_LEN) if k.upper() in SECRET_NAMES else min_len
        if len(env.get(k) or "") >= floor:
            vals.add(env[k])
    return sorted(vals, key=len, reverse=True)


def env_pin_policy(value: str | None, default: str = ENV_PIN_REFUSE) -> str:
    """``SBMIG_JOB_ENV_PIN`` 값을 정책 이름으로 바꾼다.
    공백과 대소문자는 무시하고, 비었거나 모르는 값이면 ``default``를 반환한다.

    Normalize a `SBMIG_JOB_ENV_PIN` value to `refuse` / `warn` / `off`; empty or unknown → `default`."""
    v = value.strip().lower() if isinstance(value, str) else ""      # 깨진 state.json에 숫자나 목록이 들어 있어도 예외 없이 default로 처리한다
    return v if v in ENV_PIN_POLICIES else default


def config_env_names(repo_root: Path, configs: list[str]) -> set[str]:
    """설정 파일들의 ``${VAR}`` 플레이스홀더가 읽는 환경 변수 이름을 모아 반환한다.
    yml로 읽은 값만 보므로 주석은 세지 않는다.
    yml로 읽히지 않는 파일은 텍스트 전체에서 찾고, 없는 파일은 건너뛴다.

    The env names the `${VAR}` placeholders of `config/default.yml` and the selected configs read (values only, not
    comments; a file that is not valid YAML is scanned as text; a missing file is skipped)."""
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
    둘 다 없으면 None을 반환한다. 빈 값으로 설정된 변수는 ``""``를 반환하므로 None과 구분된다.

    The value pinned for one name: the variable itself, else its alias (`ENV_PIN_ALIASES`); None when unset (a variable
    set to the empty string is `""`, not None)."""
    env = os.environ if environ is None else environ
    for key in (name, *ENV_PIN_ALIASES.get(name, ())):
        if key in env:
            return env[key]
    return None


def env_pin_names(repo_root: Path, configs: list[str], environ: dict[str, str] | None = None) -> list[str]:
    """고정할 환경 변수 이름 목록을 만든다. 설정 파일의 플레이스홀더, 이 환경의 ``SBMIG_<섹션>__<키>``와
    ``SBMIG_<섹션>`` 덮어쓰기, 그 값 안의 플레이스홀더, 별칭으로만 설정된 이름을 모은다.
    공유 볼륨에 비밀 값의 해시를 남기지 않도록 비밀로 보이는 이름과 ``ENV_PIN_SKIP``의 이름은 뺀다.

    The env names to pin: the configs' placeholders, this environment's `SBMIG_<SECTION>__<KEY>` overrides (and a
    whole-section `SBMIG_<SECTION>`, which `load_config` reads by the same rule) and the placeholders inside their
    values, and an alias-only name; never a secret-looking name nor `ENV_PIN_SKIP`."""
    from sbmig.core.config import Config, placeholder_names
    env = os.environ if environ is None else environ
    names = config_env_names(repo_root, configs)
    sections = frozenset(Config.model_fields)
    for key, val in env.items():
        if not key.startswith("SBMIG_"):
            continue
        # 밑줄 두 개가 있으면 설정 키 하나를 덮어쓰는 것이고, 없으면 최상위 섹션 이름일 때만 설정으로 본다
        if "__" in key or key[len("SBMIG_"):].lower() in sections:
            names.add(key)
            names |= placeholder_names(val)
    names |= {name for name, aliases in ENV_PIN_ALIASES.items() if any(a in env for a in aliases)}
    return sorted(n for n in names if n not in ENV_PIN_SKIP and not is_secret_name(n))


def env_pins(repo_root: Path, configs: list[str], environ: dict[str, str] | None = None) -> dict[str, str]:
    """이 환경의 고정 해시를 만든다. ``env_pin_names`` 중 설정된 이름마다 값의 sha256 앞
    ``ENV_PIN_HASH_LEN``자를 담고, 값 자체는 기록하지 않는다.
    설정되지 않은 이름은 빠지며, 한쪽에만 있는 이름은 ``compare_env_pins``가 알려 준다.

    This environment's pins: each set name of `env_pin_names` → the first `ENV_PIN_HASH_LEN` hex of its value's sha256."""
    out: dict[str, str] = {}
    for name in env_pin_names(repo_root, configs, environ):
        val = env_pin_value(name, environ)
        if val is not None:
            out[name] = hashlib.sha256(val.encode("utf-8", errors="replace")).hexdigest()[:ENV_PIN_HASH_LEN]
    return out


def compare_env_pins(pinned: dict[str, str], actual: dict[str, str]) -> dict[str, list[str]]:
    """Main의 해시와 Worker의 해시를 비교해 차이 나는 이름만 반환한다.
    ``differs``는 값이 다른 이름, ``main_only`` / ``worker_only``는 한쪽에만 설정된 이름이다.

    The differences between Main's pins and the Worker's, names only: `differs`, `main_only`, `worker_only`."""
    pinned, actual = dict(pinned or {}), dict(actual or {})
    return {"differs": sorted(k for k in pinned.keys() & actual.keys() if pinned[k] != actual[k]),
            "main_only": sorted(pinned.keys() - actual.keys()),
            "worker_only": sorted(actual.keys() - pinned.keys())}


@dataclass
class RequestRecord:
    """접수할 때 고정한 입력(``control/request.json``)을 담는 데이터 클래스. 한 번 기록하면 바꾸지 않는다.
    Worker가 ``zip_sha256``, ``config_digests``, ``sbmig_version``을 다시 계산해
    비교한다(``verify_request``).

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
    # 실행 방식(sbmig.core.models.RUN_FLOWS)이며 이 키가 없는 이전 request.json은 full로 읽는다
    flow: str = FLOW_FULL
    # 사용자가 올린 파일 이름 그대로다.
    # ``upload``는 파일 시스템에 맞게 다듬은 이름이고, 이전 기록에는 이 값이 없다
    upload_name: str | None = None
    # 업로드할 때 입력한 프로젝트명이며 실행 id 앞부분과 같다. 화면, 관리 페이지, 실행의 보고서 제목이
    # 보여 주기만 하고(entered_project) Worker의 검사는 비교하지 않는다. 이전 기록에는 이 값이 없다
    project: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> RequestRecord:
        known = set(cls.__dataclass_fields__)                   # tolerate extra keys from a newer writer
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


# 결과 ZIP을 내려받을 때의 이름 끝이다. 이관 결과는 migration, 취약점 조치 결과는 remediation이다
ZIP_KINDS = ("migration", "remediation")
# 내려받는 이름 앞부분에 쓸 수 있는 값이다. 업로드 때 검사한 프로젝트명과 실행 id가 모두 여기에 맞는다
_ZIP_BASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")


def zip_download_name(run_id: str, kind: str, project: str | None = None) -> str:
    """결과 ZIP을 사람이 내려받을 때 쓰는 이름 ``<프로젝트명>-<kind>.zip``을 반환한다. 파일을 읽지 않는다.
    프로젝트명을 쓸 수 없으면 실행 id를 쓰고, 둘 다 쓸 수 없으면 ``result.zip``을 반환한다.

    The name a person receives a result ZIP under (v2.1z-bt): `<project>-migration.zip` for the migration's
    `result.zip`, `<project>-remediation.zip` for the remediation's `delivery/result.zip`. `project` is the name entered
    at upload (the caller reads it; this function does no I/O); when it is missing or not a safe file-name part, the
    run id (a chained remediation's id is the migration run id, a standalone one its own id) stands in; when neither
    fits, `result.zip`. Files in the run folder keep their names. `kind` must be one of `ZIP_KINDS` (ValueError)."""
    if kind not in ZIP_KINDS:
        raise ValueError(f"unknown result kind: {kind!r}")
    for base in (project, run_id):
        if isinstance(base, str) and _ZIP_BASE_RE.match(base):
            return f"{base}-{kind}.zip"
    return "result.zip"


# 웹 실행 id의 모양(<프로젝트명>-YYYYMMDD-HHMMSS-<hex>)이다. sbmig.web.admin._RUN_SLUG와 같은 식이다
RUN_SLUG = re.compile(r"^(.+)-\d{8}-\d{6}-[0-9a-f]+$")
_JOURNAL_HEAD_LINES = 50                    # web_run 줄을 찾을 때 읽는 journal 앞부분 줄 수


def _web_run_event(run_dir: Path) -> dict[str, Any] | None:
    # 웹 화면이 journal 앞부분에 기록한 web_run 줄을 찾는다. 없거나 읽을 수 없으면 None이다
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
    """업로드할 때 입력한 프로젝트명을 실행 폴더에서 찾는다. ``request.json``(Job 모드), journal의 ``web_run`` 줄
    (in-process) 순이며 없으면 None을 반환한다. 보여 주는 데만 쓴다.

    The project name entered at upload (v2.1z-be), read from the run folder for DISPLAY only: `control/request.json`'s
    `project` (Job modes), else the `project` of the journal's `web_run` line (in-process: Main writes no request.json,
    but journals that line before the run starts). None when neither carries one (a direct CLI run, an older run)."""
    rec = RequestRecord.read(run_dir)
    name = _clean_name(rec.project) if rec is not None else None
    if name:
        return name
    ev = _web_run_event(run_dir)
    return _clean_name(ev.get("project")) if ev is not None else None


def display_project(run_dir: Path, run_id: str) -> str | None:
    """웹에서 올린 실행의 보고서 제목에 쓸 프로젝트명을 정한다. 입력한 프로젝트명, 실행 id 앞부분 순이다.
    웹 요청의 흔적(``request.json``이나 ``web_run`` 줄)이 없는 실행(CLI로 직접 실행)이면 None을 반환한다.

    The display project name of a run uploaded through the web: the entered name (`entered_project`), else the project
    part of the run id when the id has the web shape (`RUN_SLUG`). None for a run without a web request (a direct CLI
    run keeps its own name: its `run-…` id would otherwise read as a project called `run`)."""
    name = entered_project(run_dir)
    if name:
        return name
    if not (Path(run_dir) / CONTROL_DIR / REQUEST_FILE).is_file() and _web_run_event(run_dir) is None:
        return None
    m = RUN_SLUG.match(str(run_id or ""))
    return m.group(1) if m else None


@dataclass
class ControlState:
    """Main의 제어 상태(``control/state.json``)를 담는 데이터 클래스.
    상태가 바뀔 때마다 ``write``로 파일 전체를 원자적으로 다시 쓰고, Worker는 읽기만 한다.
    ``job``은 ``job_name(run_id)``로 정해지는 Job 이름이고, ``owner``는 ``sbmig-owner`` 라벨 값이다.
    ``env_pins``, ``env_pin_policy``, ``slot``은 Job을 만드는 Main이 creating 상태로 바꿀 때 기록하므로,
    대기 중에 웹 파드가 재시작하면 새 Main의 환경이 기준이 된다.

    Main's control state for one run (atomic rewrite on every transition). `env_pins` / `env_pin_policy` are written by
    the Main that creates the Job, with the `creating` transition; the Worker only reads this file."""
    run_id: str
    state: str = RunState.QUEUED.value
    attempt: int = 1
    job: str | None = None
    owner: str = field(default_factory=new_owner_token)
    accepted_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    exit_code: int | None = None
    error: str | None = None
    env_pins: dict[str, str] | None = None      # Job을 만드는 Main의 환경 해시이며 None이면 비교하지 않는다
    env_pin_policy: str | None = None           # refuse | warn | off 중 하나이며 Worker가 이 값을 따른다
    slot: int | None = None                     # 이 Job의 슬롯 번호이며 None이면 1번 슬롯으로 본다
    action: str | None = None                   # None은 이관 시도이고 "remediation-resume"은 조치 재개 시도다
    # Job을 만드는 Main이 creating과 함께 기록하는 설정 파일 해시와 sbmig 버전이다. Worker는 이 값과 비교하고,
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
    """Worker가 끝날 때 기록하는 ``<run dir>/job-status.json``을 담는 데이터 클래스.
    종료 코드, 시각, 어느 실행과 시도의 결과인지를 담는다.
    Main은 이전 시도의 결과를 현재 결과로 착각하지 않도록 ``matches``가 참인 파일만 결과로 받는다.
    run-job.sh가 쓰는 필드 두 개짜리 파일도 읽을 수 있다.

    The Worker's result for one attempt; Main accepts it only when `matches(run_id, attempt)`."""
    exit_code: int
    finished_at: str = field(default_factory=utc_now)
    run_id: str | None = None
    attempt: int | None = None
    error: str | None = None
    sbmig_version: str | None = None
    action: str | None = None                   # 조치 재개 시도의 결과면 그 action 이름이 들어가고 이관이면 비어 있다

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
    """Worker가 받은 입력이 Main이 고정한 값과 같은지 확인한다.
    zip sha256과 run id는 ``request.json``과, 설정 파일 해시와 sbmig 버전은 Job을 만든 Main이 ``pinned``에 기록한 값과
    비교한다. ``pinned``에 그 값이 없으면(이전 버전의 state.json) ``request.json``의 값과 비교한다.
    다른 점을 문장 목록으로 반환하고, 빈 목록이면 통과다. ``zip_path``가 None이면 zip 비교는 건너뛴다.

    The Worker's check of the pinned inputs; returns the mismatches as sentences (empty = ok). The zip's sha256 stays the
    intake pin (it identifies the input); the config digests and the version are the ones the Main that CREATED this Job
    recorded with `creating` (`pinned`), so a run that waited across a web-pod restart, or a remediation resume of a run
    an older image made, is accepted when Main and Worker agree now. A state file without them (written by the version
    before) falls back to `request.json`'s pins. A digest / version mismatch ends with `IMAGE_DIFFERS_HINT` once."""
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
