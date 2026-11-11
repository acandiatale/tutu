"""Main(웹 파드)과 Worker(Job 파드)가 공유 볼륨 위에서 주고받는 파일의 계약 (v2.1z-ac, 2026-11-09 — 배포팀의 명세를 우리 코드로).

쓰는 사람을 나눈다. **Main** 은 ``<run dir>/control/`` 만 쓴다: 접수 때 입력을 고정한 ``request.json``(zip 의 sha256, 설정 파일의
digest, sbmig 버전 — 그 뒤로 바꾸지 않는다)과 제어 상태 ``state.json``(queued → creating → created → running → finished | stopped |
lost | failed; 전이마다 원자적으로 다시 쓴다; Job 을 만들기 **전에** creating 과 Job 이름을 적어 두므로 응답이 끊겨도 다시 뜬 Main 이
같은 이름의 Job 을 찾아 이어받는다). **Worker** 는 실행 폴더의 나머지(저널·로그·보고서·result.zip)와 끝날 때 ``job-status.json`` 을 쓴다 —
여기에 run id 와 attempt 가 들어 있어 Main 은 **지금 시도**의 결과만 믿는다. Worker 는 시작할 때 ``request.json`` 의 digest 와 자기가 받은
zip·설정·코드를 대조해 다르면 돌지 않는다(exit 3). Job 의 소유는 라벨 ``sbmig-run-id`` + ``sbmig-owner``(요청마다 난수)로 확인한다 —
Main 은 자기 라벨이 맞는 Job 만 지운다.

리뷰 2026-11-10 — **환경 변수 고정**: zip·설정 파일·버전이 같아도 두 파드의 환경 변수가 다르면 다른 설정으로 돈다(설정 파일의
``${VAR}`` 자리표시자와 덮어쓰기 ``SBMIG_<섹션>__<키>`` 는 환경 변수가 값을 정한다). Job 을 만드는 Main 이 그 이름들의 값 지문
(``env_pins`` — 비밀로 보이는 이름은 빼고, sha256 앞 16자)과 정책(``SBMIG_JOB_ENV_PIN``: refuse | warn | off)을 ``state.json`` 에
creating 전이와 함께 적고, Worker 가 자기 환경으로 다시 계산해 대조한다(``compare_env_pins``).
2026-11-11: 비밀로 다루는 이름에 낱말 규칙 밖의 ``SECRET_NAMES``(IVAS 계정 아이디)가 더해졌다 — 지우는 대상이 되고 지문에서는 빠진다.
2026-11-11 (wave 4) — **조치 재개 시도**: 빌드 파드에서 승인·확인을 기다리며 끝난 이어 돈 조치를 새 빌드 파드에서 잇는 것은 같은 실행의
**다른 행동(action)을 가진 새 시도**다. Main 은 그 시도의 제어 상태를 ``control/remediation-resume.json`` 에(같은 ``ControlState``, ``action``
이 채워짐, creating 을 먼저 적는 규칙도 같다), Worker 는 결과를 ``remediation-job-status.json`` 에 쓴다 — 이관의 ``control/state.json`` ·
``job-status.json`` 은 재개가 절대 건드리지 않는다(이관은 "끝남, 빌드 성공" 그대로). Job env ``SBMIG_WORKER_ACTION`` 이 Worker 에게 행동을 알린다.
``IVAS_BASE_URL`` 은 비밀이 아니지만 ``SBMIG_`` 덮어쓰기도 설정 파일의 ``${VAR}`` 도 아니라 지문 대상이 아니다.

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
# Main 이 Job 마다 끼워 넣는 실행별 값의 이름(RunManager._job_env). 그 밖의 env 는 파드의 것이다 — 틀의 리터럴·ConfigMap·Secret 이 채운다
# SBMIG_JOB_SLOT: 동시에 도는 Job 가운데 이 Job 의 자리 번호(1 부터) — Worker 가 자리마다 다른 Maven 로컬 저장소를 쓰게 한다
# SBMIG_WORKER_ACTION: 이 Job 이 할 일(없음 = 이관, "remediation-resume" = 이어 돈 조치 재개) — 2026-11-11 wave 4
RUN_ENV = ("SBMIG_SOURCE_ZIP", "SBMIG_OUT", "SBMIG_RUN_ID", "SBMIG_CONFIG", "SBMIG_STOP_AFTER", "SBMIG_LLM_PROVIDER",
           "SBMIG_JOB_SLOT", "SBMIG_WORKER_ACTION")
SLOT_ENV = "SBMIG_JOB_SLOT"                 # 위 RUN_ENV 의 한 이름: Main 이 적고(_job_env) Worker 가 읽는다
# ── 이어 돈 조치의 재개 시도(2026-11-11 wave 4) ──
ACTION_ENV = "SBMIG_WORKER_ACTION"          # 위 RUN_ENV 의 한 이름: 비었으면 이관(sbmig run), 아니면 아래 행동 중 하나
ACTION_REMEDIATION_RESUME = "remediation-resume"
WORKER_ACTIONS = (ACTION_REMEDIATION_RESUME,)
RESUME_STATE_FILE = "remediation-resume.json"         # control/ 안: 재개 시도의 Main 제어 상태(Main 만 쓴다)
RESUME_STATUS_FILE = "remediation-job-status.json"    # 실행 폴더: 재개 Worker 의 결과(이관의 job-status.json 과 따로)
MAX_PARALLEL_ENV = "SBMIG_JOB_MAX_PARALLEL"  # 웹 파드만 읽는다: 동시에 도는 Job 의 최대 수(기본 1; k8s-job / local-job 에서만)
LLM_RECORDS_ENV = "SBMIG_WORKER_LLM_RECORDS"  # "1" 이면 Worker 가 설정의 llm.record_dir 을 그대로 둔다(기본: 끈다 — 소스가 공유 볼륨에 더 남지 않게)
SECRET_NAME_WORDS = ("TOKEN", "PASSWORD", "PASSWD", "SECRET", "API_KEY")   # 이 말이 든 env 는 비밀로 다룬다(값은 로그·보고서에서 지운다)
MIN_SECRET_LEN = 6                          # 이보다 짧은 값은 지우지 않는다(우연한 부분 일치로 로그를 망치지 않게)
# 이름에 위 낱말이 없어도 비밀로 다루는 env(2026-11-11): IVAS 계정 아이디 — 회사 규칙은 IVAS 의 사용자·비밀번호·토큰을 모두 비밀로 본다.
# 취약점 조치가 빌드 파드에서 돌게 되어 이 값이 Job 파드의 로그·보고서에 닿을 수 있다. 아이디는 짧을 수 있어 하한을 따로 둔다
SECRET_NAMES = frozenset({"IVAS_USER_ID"})
MIN_NAMED_SECRET_LEN = 4                    # SECRET_NAMES 의 값은 이 길이부터 지운다(remediations.graph.scrub_message 와 같은 하한)
# ── 환경 변수 고정(리뷰 2026-11-10) ──
ENV_PIN_POLICY_ENV = "SBMIG_JOB_ENV_PIN"    # Main 의 env: Worker 의 환경이 다를 때 refuse(돌지 않는다) | warn(저널에 적고 돈다) | off(안 본다)
ENV_PIN_REFUSE, ENV_PIN_WARN, ENV_PIN_OFF = "refuse", "warn", "off"
ENV_PIN_POLICIES = (ENV_PIN_REFUSE, ENV_PIN_WARN, ENV_PIN_OFF)
# 파드마다 달라도 되는 이름: LLM 원문 기록 폴더(웹 파드 안에서 도는 실행의 진단용 — Worker 는 SBMIG_WORKER_LLM_RECORDS 로 따로 켠다)
ENV_PIN_SKIP = frozenset({"SBMIG_LLM__RECORD_DIR", "LLM_RECORD_DIR"})
# 같은 설정을 정하는 다른 이름: Worker 는 KB 위치를 SBMIG_KB_DIR 로 받는다(worker-paths.yml 의 kb.dir) — 웹 파드의 덮어쓰기
# SBMIG_KB__DIR 과 같은 값이어야 한다(틀에 그 줄이 없으면 Worker 는 구운 yml 의 PC 경로에 KB 를 쓰고 파드와 함께 잃는다)
ENV_PIN_ALIASES: dict[str, tuple[str, ...]] = {"SBMIG_KB__DIR": ("SBMIG_KB_DIR",)}
ENV_PIN_HASH_LEN = 16


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


def state_file_for(action: str | None) -> str:
    """행동별 Main 제어 상태 파일 이름(``control/`` 안): 이관(None) = ``state.json``, 조치 재개 = ``remediation-resume.json``.

    The control-state file name of an action: the migration's `state.json`, a remediation resume's own file."""
    return RESUME_STATE_FILE if action == ACTION_REMEDIATION_RESUME else STATE_FILE


def status_file_for(action: str | None) -> str:
    """행동별 Worker 결과 파일 이름(실행 폴더): 이관 = ``job-status.json``, 조치 재개 = ``remediation-job-status.json``.

    The Worker's result file name of an action (the resume never overwrites the migration's `job-status.json`)."""
    return RESUME_STATUS_FILE if action == ACTION_REMEDIATION_RESUME else JOB_STATUS_FILE


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
    같은 경로(같은 이미지 — 회사 yml 을 ConfigMap 으로 줄 때는 같은 마운트)에서 다시 계산해 대조한다.

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


def is_secret_name(name: str) -> bool:
    """환경 변수 이름이 비밀로 보이는가 — 이름에 ``SECRET_NAME_WORDS`` 가 들어 있으면 그렇다. 설정 덮어쓰기 ``SBMIG_<섹션>__<키>``(밑줄 두 개)는
    예외다: 설정 키는 비밀을 담지 않는다(키·토큰은 환경 변수 이름으로만 가리킨다 — ``llm.api_key_env``). 그래서 ``SBMIG_LLM__MAX_OUTPUT_TOKENS``
    의 값이 보고서에서 지워지거나 Worker 틀에서 literal 이라고 거절되지 않는다. ``SECRET_NAMES``(IVAS 계정 아이디)는 낱말이 없어도 비밀이다.

    Whether an env name looks like a secret (contains one of `SECRET_NAME_WORDS`, or is one of `SECRET_NAMES`). A config override
    `SBMIG_<SECTION>__<KEY>` never is: no config key holds a credential, so `SBMIG_LLM__MAX_OUTPUT_TOKENS` is neither redacted nor
    refused as a literal."""
    up = str(name).upper()
    if up.startswith("SBMIG_") and "__" in up:
        return False
    return up in SECRET_NAMES or any(w in up for w in SECRET_NAME_WORDS)


def secret_env_names(environ: dict[str, str] | None = None) -> list[str]:
    """비밀로 보이는 환경 변수 이름들(``is_secret_name``, 정렬). / Env names that look like secrets."""
    env = os.environ if environ is None else environ
    return sorted(k for k in env if is_secret_name(k))


def secret_values(environ: dict[str, str] | None = None, min_len: int = MIN_SECRET_LEN) -> list[str]:
    """비밀로 보이는 환경 변수의 값들(``min_len`` 이상, 긴 것부터 — 긴 값을 먼저 지워야 부분 문자열이 남지 않는다).
    ``SECRET_NAMES`` 의 값은 ``MIN_NAMED_SECRET_LEN`` 부터(``min_len`` 이 더 작으면 그것) — 짧은 계정 아이디도 지운다.

    The values of secret-looking env vars, longest first; a `SECRET_NAMES` value from `MIN_NAMED_SECRET_LEN` characters on."""
    env = os.environ if environ is None else environ
    vals = set()
    for k in secret_env_names(env):
        floor = min(min_len, MIN_NAMED_SECRET_LEN) if k.upper() in SECRET_NAMES else min_len
        if len(env.get(k) or "") >= floor:
            vals.add(env[k])
    return sorted(vals, key=len, reverse=True)


def env_pin_policy(value: str | None, default: str = ENV_PIN_REFUSE) -> str:
    """``SBMIG_JOB_ENV_PIN`` 의 값을 정책 이름으로(앞뒤 공백·대소문자 무시). 비었거나 모르는 값이면 ``default``.

    Normalize a `SBMIG_JOB_ENV_PIN` value to `refuse` / `warn` / `off`; empty or unknown → `default`."""
    v = value.strip().lower() if isinstance(value, str) else ""      # 깨진 state.json 의 값(숫자·목록)도 예외 없이 default
    return v if v in ENV_PIN_POLICIES else default


def config_env_names(repo_root: Path, configs: list[str]) -> set[str]:
    """``config/default.yml`` 과 선택한 설정 파일들이 ``${VAR}`` 자리표시자로 읽는 환경 변수 이름들(주석 속의 것은 세지 않는다 —
    yml 을 읽은 값만 본다; yml 로 읽히지 않는 파일은 글 전체에서 찾는다). 없는 파일은 건너뛴다.

    The env names the `${VAR}` placeholders of `config/default.yml` and the selected configs read (values only, not
    comments; a file that is not valid YAML is scanned as text; a missing file is skipped)."""
    import yaml          # 늦은 import: 이 모듈은 설정 스택 없이도 읽힌다(k8s 클라이언트, 테스트)

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
    """고정할 이름 하나의 값: 그 이름의 환경 변수, 없으면 ``ENV_PIN_ALIASES`` 의 다른 이름(설정이 실제로 적용되는 순서와 같다).
    어느 것도 없으면 None(빈 값으로 **설정된** 변수는 ``""`` — 없는 것과 다르다).

    The value pinned for one name: the variable itself, else its alias (`ENV_PIN_ALIASES`); None when unset (a variable
    set to the empty string is `""`, not None)."""
    env = os.environ if environ is None else environ
    for key in (name, *ENV_PIN_ALIASES.get(name, ())):
        if key in env:
            return env[key]
    return None


def env_pin_names(repo_root: Path, configs: list[str], environ: dict[str, str] | None = None) -> list[str]:
    """고정할 환경 변수 이름들(정렬): 설정 파일의 자리표시자(``config_env_names``) + 이 환경의 설정 덮어쓰기
    ``SBMIG_<섹션>__<키>``(와 섹션 하나를 통째로 바꾸는 ``SBMIG_<섹션>`` — ``load_config`` 가 같은 규칙으로 읽는다)와 그 값 안의
    자리표시자 + 다른 이름으로만 설정된 ``ENV_PIN_ALIASES`` 의 이름. 비밀로 보이는 이름(``is_secret_name`` — 그 값의 지문을 공유
    볼륨에 남기지 않는다)과 ``ENV_PIN_SKIP`` 은 뺀다. 프록시 변수는 대상이 아니다.

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
        # 밑줄 두 개 = 설정 키 하나. 밑줄 두 개가 없으면 최상위 섹션 이름일 때만 설정이다(SBMIG_WEB_MODE · SBMIG_OUT 류는 아니다)
        if "__" in key or key[len("SBMIG_"):].lower() in sections:
            names.add(key)
            names |= placeholder_names(val)
    names |= {name for name, aliases in ENV_PIN_ALIASES.items() if any(a in env for a in aliases)}
    return sorted(n for n in names if n not in ENV_PIN_SKIP and not is_secret_name(n))


def env_pins(repo_root: Path, configs: list[str], environ: dict[str, str] | None = None) -> dict[str, str]:
    """이 환경의 고정 지문: ``env_pin_names`` 중 **설정된** 이름 → 값의 sha256 앞 ``ENV_PIN_HASH_LEN`` 자(값 자체는 적지 않는다).
    설정되지 않은 이름은 빠진다 — 한쪽에만 있는 이름은 ``compare_env_pins`` 가 따로 알려 준다.

    This environment's pins: each set name of `env_pin_names` → the first `ENV_PIN_HASH_LEN` hex of its value's sha256."""
    out: dict[str, str] = {}
    for name in env_pin_names(repo_root, configs, environ):
        val = env_pin_value(name, environ)
        if val is not None:
            out[name] = hashlib.sha256(val.encode("utf-8", errors="replace")).hexdigest()[:ENV_PIN_HASH_LEN]
    return out


def compare_env_pins(pinned: dict[str, str], actual: dict[str, str]) -> dict[str, list[str]]:
    """Main 이 적은 지문과 Worker 가 계산한 지문의 차이 — **이름만**(정렬): ``differs``(양쪽에 있고 값이 다름), ``main_only``
    (Main 에만 설정됨), ``worker_only``(Worker 에만 설정됨). 차이가 없는 항목은 빈 목록.

    The differences between Main's pins and the Worker's, names only: `differs`, `main_only`, `worker_only`."""
    pinned, actual = dict(pinned or {}), dict(actual or {})
    return {"differs": sorted(k for k in pinned.keys() & actual.keys() if pinned[k] != actual[k]),
            "main_only": sorted(pinned.keys() - actual.keys()),
            "worker_only": sorted(actual.keys() - pinned.keys())}


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
    전이할 때마다 ``write`` 로 통째로 다시 쓴다(원자적). ``env_pins`` / ``env_pin_policy`` 는 Job 을 **만드는** Main 이 creating 전이와
    함께 적는다(접수한 Main 이 아니다 — 대기 중에 웹 파드가 재시작되면 새 Main 의 환경이 기준이다). Worker 는 이 파일을 읽기만 한다.
    ``slot`` 도 같은 전이 때 적는다: 동시에 도는 Job 가운데 이 Job 의 자리 번호 — 재시작한 Main 이 같은 자리를 다시 잡는다.

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
    env_pins: dict[str, str] | None = None      # Job 을 만드는 Main 의 환경 지문(creating 전이 때 적는다; None = 대조 안 함)
    env_pin_policy: str | None = None           # refuse | warn | off — Main 의 SBMIG_JOB_ENV_PIN (Worker 는 이 값을 따른다)
    slot: int | None = None                     # 이 Job 이 차지한 자리 번호(creating 전이 때 적는다; None = 자리 개념 이전에 만든 Job = 1번 자리)
    action: str | None = None                   # None = 이관 시도, "remediation-resume" = 이어 돈 조치 재개 시도(파일도 따로)

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
    """Worker 가 끝날 때 쓰는 ``<run dir>/job-status.json``: 종료 코드, 시각, 그리고 **어느 실행·시도의** 결과인지. Main 은 ``matches`` 가
    참인 파일만 결과로 받는다(옛 시도의 파일을 지금 성공으로 읽지 않게). run-job.sh 가 쓰는 두 필드짜리 파일도 읽힌다(id 없음 = 대조 생략).

    The Worker's result for one attempt; Main accepts it only when `matches(run_id, attempt)`."""
    exit_code: int
    finished_at: str = field(default_factory=utc_now)
    run_id: str | None = None
    attempt: int | None = None
    error: str | None = None
    sbmig_version: str | None = None
    action: str | None = None                   # 조치 재개 시도의 결과면 그 행동 이름(이관이면 없음)

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
