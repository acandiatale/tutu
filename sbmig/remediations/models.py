"""취약점 조치(remediation)의 공통 계약: 상태, 자료형, 오류, 해시 규칙, 실행 폴더 구조.
다른 파일(intake / ivas / maven / inventory / planner / apply / graph / service)은 서로를 직접 알지 않고 이 파일의
자료형과 ``interfaces.py`` 의 Protocol 에만 기댄다. 모든 자료형은 표준 dataclass 이고 ``to_dict`` / ``from_dict`` 로
JSON 파일에 오간다(파일 기반: 데이터베이스 없음). 비밀 값(IVAS 토큰·비밀번호, 저장소 자격증명)은 이 자료형 어디에도 넣지 않는다.

실행 폴더 ``<out>/_remediations/<id>/`` (``RunPaths``):
    original/source.zip        올린 ZIP 그대로(다시 쓰지 않는다)        original/manifest.json   원본 파일 목록(경로·크기·sha256)
    accepted/                  작업 트리(원본을 푼 것; 승인·적용된 변경만 들어온다)
    rounds/<n>/candidate/      n 번째 라운드의 격리된 후보 사본          rounds/<n>/{plan.json, candidate.patch, build.log, resolved.json}
    scans/<label>.json         검사 결과(baseline / final-r<n>)          delivery/result.zip + delivery.json
                               (+ delivery/result-r<k>-verified.zip: 뒤 라운드의 빌드 실패본이 result.zip 이 될 때 남기는 앞 라운드의 검증본)
    state.json                 작업 상태(원자적으로 교체)               approvals.jsonl          승인·거절 기록(덧붙이기만)
    journal.jsonl / remediation.log / report.md / report.json

Shared contract of the remediation feature: statuses, dataclasses, errors, hash rules and the run directory layout.
Everything is a plain dataclass with `to_dict` / `from_dict`; no secret value is ever stored in these types.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

SCHEMA = 1
REMEDIATIONS_DIR = "_remediations"       # under the web out dir; `_` keeps it out of the migration run list

# ---------------------------------------------------------------------------- 상태 / statuses
QUEUED = "QUEUED"                        # 접수됨, 작업 스레드 대기
RUNNING = "RUNNING"
AWAITING_APPROVAL = "AWAITING_APPROVAL"  # 승인 후보가 결정을 기다린다 → approvals 뒤 resume
AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"   # IVAS 업로드 결과가 불확실 → 사람이 확인한 뒤 resume(confirm=…)
STOPPED = "STOPPED"                      # 중지 요청을 workflow 경계에서 처리함 → resume 가능
INTERRUPTED = "INTERRUPTED"              # 서버가 실행 중에 내려갔다 → 자동 재개 없음, 명시적 resume 만
SUCCEEDED = "SUCCEEDED"                  # 성공 조건을 모두 만족 (evaluate 참고)
INCOMPLETE = "INCOMPLETE"                # 끝났지만 성공은 아님: 잔여·수동 검토·미완료 검사·거절된 제안 등
FAILED = "FAILED"                        # 기준 빌드 실패, 후보 빌드 실패, 검증 실패, 접수 거부 뒤 오류 등
STATUSES = (QUEUED, RUNNING, AWAITING_APPROVAL, AWAITING_CONFIRMATION, STOPPED, INTERRUPTED, SUCCEEDED, INCOMPLETE, FAILED)
TERMINAL = (SUCCEEDED, INCOMPLETE, FAILED)                       # 더 진행하지 않는다
RESUMABLE = (AWAITING_APPROVAL, AWAITING_CONFIRMATION, STOPPED, INTERRUPTED)
ACTIVE = (QUEUED, RUNNING)

# 그래프 노드 이름(순서) / graph nodes in order
NODES = ("baseline_build", "baseline_scan", "inventory", "plan", "apply", "build", "freeze_delivery", "rescan",
         "evaluate", "report")

# 이유 코드(state.reason, 보고서) — 문자열 그대로가 API 응답 계약이다
R_BASELINE_BUILD_FAILED = "baseline_build_failed"
R_CANDIDATE_BUILD_FAILED = "candidate_build_failed"
R_SCAN_INCOMPLETE = "scan_incomplete"
R_UPLOAD_UNCERTAIN = "upload_uncertain"
R_ZIP_HASH_MISMATCH = "zip_hash_mismatch"
R_TEXT_ONLY_VIOLATION = "text_only_violation"            # 버전 글자 밖의 변경이 후보에 있다
R_RESOLUTION_MISMATCH = "resolution_mismatch"            # 해석·패키징 결과가 계획과 다르다
R_BOUNDARY_REFUSED = "execution_boundary_refused"        # .mvn 설정 / 승인되지 않은 저장소 등
R_NOT_READY = "not_ready"                                # 필수 설정(IVAS 주소·자격증명, settings.xml, JDK 21) 없음
R_PENDING_APPROVAL = "pending_approval"
R_REJECTED_PROPOSALS = "rejected_proposals"
R_RESIDUAL_FINDINGS = "residual_findings"                # 최종 검사에 취약점이 남았다
R_MANUAL_REVIEW = "manual_review"                        # vendored / plugin / 매핑 불가 / 출처 불명
R_NO_CANDIDATE_VERSION = "no_candidate_version"
R_ROUNDS_EXHAUSTED = "rounds_exhausted"
R_STOP_REQUESTED = "stop_requested"
R_INTERNAL_ERROR = "internal_error"
R_POLICY_DENIED = "policy_denied"                        # 서버 정책 deny 에 걸린 항목
R_UNKNOWN_PROVENANCE = "unknown_provenance"            # 어느 parent / BOM 이 관리하는지 분명하지 않다
R_STALE_LOCATION = "stale_location"                    # 인벤토리 뒤에 파일이 바뀌어 버전 글자가 그 자리에 없다
R_INVENTORY_INCOMPLETE = "inventory_incomplete"

T = TypeVar("T")


def canonical_json(value: Any) -> str:
    """해시용 정규 JSON: 키 정렬, 공백 없음, 비 ASCII 그대로. 같은 값이면 늘 같은 글자열.

    Canonical JSON for hashing (sorted keys, no whitespace)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    """UTF-8 글자열의 sha256 (16진수 전체)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    """파일 내용의 sha256 (16진수 전체). 큰 ZIP 도 조각으로 읽는다."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _plain(v: Any) -> Any:
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return {f.name: _plain(getattr(v, f.name)) for f in dataclasses.fields(v)}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items()}
    if isinstance(v, Path):
        return v.as_posix()
    return v


class Record:
    """dataclass 공통: ``to_dict`` (JSON 으로 쓸 수 있는 dict) / ``from_dict`` (모르는 키는 버린다 — 새 버전이 쓴 파일을
    옛 코드가 읽어도 죽지 않게). 중첩 자료형은 각 클래스가 ``_nested`` 로 알려 준다.

    Mixin for the dataclasses below: `to_dict` / tolerant `from_dict`."""
    _nested: dict[str, Any] = {}

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)

    @classmethod
    def from_dict(cls: type[T], data: dict[str, Any] | None) -> T:
        data = dict(data or {})
        names = {f.name for f in dataclasses.fields(cls)}     # type: ignore[arg-type]
        kw: dict[str, Any] = {}
        for k, v in data.items():
            if k not in names:
                continue
            sub = cls._nested.get(k)                           # type: ignore[attr-defined]
            if sub is not None and v is not None:
                if isinstance(v, list):
                    v = [sub.from_dict(x) if isinstance(x, dict) else x for x in v]
                elif isinstance(v, dict):
                    v = sub.from_dict(v)
            kw[k] = v
        return cls(**kw)                                       # type: ignore[call-arg]


# ---------------------------------------------------------------------------- 오류 / errors
class RemediationError(Exception):
    """이 기능의 오류 공통 부모. ``code`` 는 위 이유 코드 또는 각 오류의 고유 코드."""
    code = R_INTERNAL_ERROR

    def __init__(self, message: str, code: str | None = None, **details: Any):
        super().__init__(message)
        if code:
            self.code = code
        self.details = details


class IntakeRejected(RemediationError):
    """접수 거부(HTTP 400): ZIP 이 규칙을 어겼다. ``code`` 예: not_zip / encrypted / path_traversal / absolute_path /
    symlink / special_file / duplicate_entry / normalized_collision / too_many_entries / too_large /
    compression_ratio / no_pom / empty."""
    code = "intake_rejected"


class NotReady(RemediationError):
    """필수 준비 조건이 없다(HTTP 503): IVAS 주소·자격증명, 서버 settings.xml, JDK 21, Maven."""
    code = R_NOT_READY


class IvasError(RemediationError):
    """IVAS 호출 실패(응답이 왔고 실패가 분명한 경우 포함). 검사는 미완료가 된다 — 0건이 아니다."""
    code = "ivas_error"


class IvasUploadUncertain(IvasError):
    """업로드 요청을 보냈지만 결과를 모른다(시간 초과·연결 끊김·읽을 수 없는 응답). **자동으로 다시 올리지 않는다** —
    작업은 AWAITING_CONFIRMATION 이 되고 사람이 IVAS 에서 확인한 뒤 resume 한다."""
    code = R_UPLOAD_UNCERTAIN


class ApprovalMismatch(RemediationError):
    """승인 요청의 해시(proposal / source tree / policy) 또는 diff 가 지금의 후보와 다르다(HTTP 409)."""
    code = "approval_mismatch"


class StateConflict(RemediationError):
    """지금 상태에서는 할 수 없는 요청(HTTP 409): 실행 중인 작업 삭제, 재개할 수 없는 상태의 resume 등."""
    code = "state_conflict"


# ---------------------------------------------------------------------------- 접수 / intake
@dataclass
class IntakeLimits(Record):
    """ZIP 검사 한도(``remediation.max_*`` 에서 온다). 바이트 단위."""
    max_zip_bytes: int = 1024 * 1024 * 1024
    max_entries: int = 200_000
    max_unpacked_bytes: int = 4096 * 1024 * 1024
    max_entry_bytes: int = 1024 * 1024 * 1024
    max_compression_ratio: int = 200


@dataclass
class ManifestEntry(Record):
    """원본 파일 하나: ZIP 안 경로(슬래시, 정규화), 크기, sha256."""
    path: str
    size: int
    sha256: str


@dataclass
class IntakeResult(Record):
    """접수 결과. ``project_root`` 는 accepted/ 안에서 루트 pom.xml 이 있는 폴더(ZIP 이 최상위 폴더 하나로 싸여 있으면 그 폴더,
    accepted 기준 상대 경로; 바로 있으면 ".")."""
    original_zip: str                 # 실행 폴더 기준 상대 경로 (original/source.zip)
    zip_sha256: str
    manifest: str                     # original/manifest.json
    accepted_dir: str                 # accepted
    project_root: str                 # accepted 기준 상대 경로
    tree_hash: str                    # accepted 트리의 해시 (tree_hash 규칙)
    files: int = 0
    unpacked_bytes: int = 0
    notes: list[str] = field(default_factory=list)


@dataclass
class BoundaryFinding(Record):
    """서버 실행 경계를 우회할 수 있는 요소 하나. ``action`` = refuse(작업 거부) / ignore(기록만).
    ``code``: project_maven_config / maven_extensions / maven_wrapper / symlink / foreign_repository /
    foreign_plugin_repository / settings_in_project / unparsable_pom."""
    code: str
    path: str
    action: str
    detail: str = ""


@dataclass
class DeliveryInfo(Record):
    """고정한 전달 ZIP. 이 ZIP 과 **같은 바이트**를 IVAS 에 다시 올린다(``sha256`` 로 확인)."""
    path: str                         # delivery/result.zip (실행 폴더 기준)
    sha256: str
    size: int
    files: int
    tree_hash: str                    # ZIP 에 담은 트리의 해시
    round: int = 0
    # False = 빌드가 실패한 수정본(remediation.failed_candidate: include). 이 ZIP 은 재검사하지 않았고 성공 판정에 쓰이지 않는다
    build_ok: bool = True
    scanned: bool = False             # 이 ZIP 과 같은 sha256 을 IVAS 가 검사했고 그 검사가 완전했는가


# ---------------------------------------------------------------------------- IVAS 검사 / scan
@dataclass
class UploadReceipt(Record):
    """업로드가 받아들여졌다는 IVAS 의 답: 프로젝트·분석 ID 와 올린 ZIP 의 sha256."""
    project_id: str
    analysis_id: str
    zip_sha256: str
    project_name: str = ""


@dataclass
class Finding(Record):
    """IVAS 발견 항목 하나(정규화). ``kind``: library(라이브러리 취약점) / file_component(파일 컴포넌트, main) /
    file_component_potential / sw(지원하지 않음 — 있으면 검사 미완료). ``identifier`` 는 IVAS 가 준 그대로
    (Maven 라이브러리는 "group:artifact"), ``locations`` 는 ZIP 안 경로."""
    id: str                           # CVE-…
    kind: str
    identifier: str
    version: str = ""
    patched_version: str = ""
    locations: list[str] = field(default_factory=list)
    action: str = ""                  # action.actionDivCd 그대로
    score: float | None = None        # rating 중 가장 높은 점수
    component_id: str = ""            # file component id

    @property
    def key(self) -> str:
        """중복 판정 키: (kind, id, identifier, version)."""
        return f"{self.kind}|{self.id}|{self.identifier}|{self.version}"


@dataclass
class ScanResult(Record):
    """검사 한 번의 결과. ``complete`` 가 False 면 ``findings`` 가 비어 있어도 **0건이 아니다** — 성공 판정과 계획에 쓰지 않는다.
    ``incomplete_reasons`` 코드: analysis_failed / analysis_timeout / page_missing / total_mismatch / duplicate_result /
    summary_mismatch / unsupported_sw / zip_hash_mismatch / id_mismatch / http_error / stop_requested."""
    label: str                        # baseline / final-r<n>
    zip_sha256: str
    complete: bool
    receipt: UploadReceipt | None = None
    status_code: str = ""             # 마지막 anlStatusDivCd
    findings: list[Finding] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)      # 요약의 범주별 total
    incomplete_reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    _nested = {"receipt": UploadReceipt, "findings": Finding}


# ---------------------------------------------------------------------------- Maven 해석 / resolution
@dataclass
class ResolvedDep(Record):
    """Maven 이 실제로 고른 의존성 하나(dependency:tree 의 한 줄). ``path`` 는 모듈에서 이 항목까지의 g:a:v 사슬(직접이면 빈 목록)."""
    group_id: str
    artifact_id: str
    version: str
    scope: str = "compile"
    type: str = "jar"
    classifier: str = ""
    direct: bool = False
    path: list[str] = field(default_factory=list)

    @property
    def ga(self) -> str:
        return f"{self.group_id}:{self.artifact_id}"


@dataclass
class ModuleResolution(Record):
    """모듈 하나의 해석 결과. ``dir`` 은 프로젝트 루트 기준(루트는 "."), ``plugins`` 는 빌드 플러그인과 그 의존성
    (g:a:v 만; 자동 변경 대상이 아니다)."""
    dir: str
    group_id: str
    artifact_id: str
    version: str
    packaging: str = "jar"
    dependencies: list[ResolvedDep] = field(default_factory=list)
    plugins: list[ResolvedDep] = field(default_factory=list)
    _nested = {"dependencies": ResolvedDep, "plugins": ResolvedDep}


@dataclass
class ResolvedModel(Record):
    """프로젝트 전체의 해석 결과. ``complete`` False = 일부 모듈을 읽지 못함(이유는 ``notes``) — 계획·검증에 쓸 수 없다."""
    modules: list[ModuleResolution] = field(default_factory=list)
    complete: bool = True
    notes: list[str] = field(default_factory=list)
    # reactor 의 뿌리 폴더들(프로젝트 루트 기준). 빈 목록 = 루트 pom 하나(".") — 이관이 서비스마다 빌드한 프로젝트만 여럿이다
    roots: list[str] = field(default_factory=list)
    _nested = {"modules": ModuleResolution}

    def versions_of(self, ga: str) -> set[str]:
        """이 g:a 가 해석된 모든 버전(모듈 전체)."""
        return {d.version for m in self.modules for d in m.dependencies if d.ga == ga}


@dataclass
class PackagedLib(Record):
    """빌드 산출물(war / 실행 jar) 안에 실제로 들어간 jar 하나. ``ga`` 는 알아낸 경우만(pom.properties), 아니면 빈 값."""
    artifact: str                     # 산출물 경로(프로젝트 루트 기준, 예: app/target/app.war)
    entry: str                        # 산출물 안 경로(WEB-INF/lib/foo-1.2.jar)
    file_name: str
    ga: str = ""
    version: str = ""


@dataclass
class BuildOutcome(Record):
    """package 빌드 한 번. ``ok`` 는 BUILD SUCCESS 일 때만 True."""
    ok: bool
    exit_code: int | None = None
    log: str = ""                     # 로그 파일 (실행 폴더 기준 상대 경로)
    summary: str = ""                 # 실패 요약 몇 줄(비밀 값 없음)
    seconds: float = 0.0
    timed_out: bool = False


# ---------------------------------------------------------------------------- 인벤토리 / inventory
# 발견 항목이 프로젝트의 어디에서 오는가 / where a finding's library comes from
O_DIRECT_LITERAL = "direct_literal"           # pom 의 <dependency><version>1.2</version>
O_DIRECT_PROPERTY = "direct_property"         # <version>${x.version}</version>, 속성은 프로젝트 pom 에 있다
O_OWN_MANAGEMENT = "own_management"           # 프로젝트 pom 의 <dependencyManagement> 가 버전을 정한다(글자 또는 프로젝트 속성)
O_PARENT_MANAGED = "parent_managed"           # 외부 부모 pom 이 관리 — 바꾸려면 부모 버전 변경 또는 override
O_BOM_MANAGED = "bom_managed"                 # 가져온 BOM(<scope>import</scope>)이 관리
O_TRANSITIVE = "transitive"                   # 다른 의존성이 끌고 온다(프로젝트에 선언 없음)
O_PLUGIN = "plugin"                           # 빌드 플러그인 또는 그 의존성 — 자동 변경 안 함
O_VENDORED = "vendored"                       # 트리에 든 파일(jar / js …) — 자동 변경 안 함
O_UNMAPPED = "unmapped"                       # Maven 해석 결과와 연결하지 못함 — 자동 변경 안 함
AUTO_ORIGINS = (O_DIRECT_LITERAL, O_DIRECT_PROPERTY, O_OWN_MANAGEMENT, O_PARENT_MANAGED, O_BOM_MANAGED, O_TRANSITIVE)
MANUAL_ORIGINS = (O_PLUGIN, O_VENDORED, O_UNMAPPED)


@dataclass
class PomLocation(Record):
    """버전 글자가 적힌 자리. ``pom`` 은 프로젝트 루트 기준 경로, ``line`` 은 1 부터, ``text`` 는 그 버전 글자 그대로
    (예: "2.14.0"), ``property`` 는 속성으로 정해질 때 그 이름(이때 line / text 는 속성 정의 자리)."""
    pom: str
    line: int
    text: str
    property: str = ""
    element: str = "dependency"       # dependency / management / property / parent / bom


@dataclass
class InventoryItem(Record):
    """취약한 라이브러리 하나(g:a + 해석된 버전)와 그것을 가리키는 발견 항목들. ``origin`` 이 자동 변경 가능 여부를 정한다.
    ``location`` 은 고칠 수 있는 버전 글자의 자리(없으면 None), ``manager`` 는 parent_managed / bom_managed 일 때 그 부모·BOM 의
    g:a:v 와 선언 자리, ``reason`` 은 수동 검토로 남기는 까닭."""
    ga: str
    version: str
    origin: str
    finding_ids: list[str] = field(default_factory=list)
    # IVAS 가 알려 준 패치 버전(힌트일 뿐, 근거가 아니다): 발견 항목마다 정규화한 한 줄 — 줄들은 모두 만족해야 하고(AND),
    # 한 줄 안에 버전이 여럿이면 버전 라인별 대안이다(OR)
    patched_versions: list[str] = field(default_factory=list)
    modules: list[str] = field(default_factory=list)
    via: list[str] = field(default_factory=list)                  # transitive 일 때 끌고 오는 사슬(g:a:v)
    location: PomLocation | None = None
    manager: str = ""                                             # 관리하는 부모 / BOM 의 g:a:v
    manager_location: PomLocation | None = None
    reason: str = ""
    paths: list[str] = field(default_factory=list)                # vendored 파일 경로
    _nested = {"location": PomLocation, "manager_location": PomLocation}


@dataclass
class Inventory(Record):
    """발견 항목 → 프로젝트 연결의 전체. ``complete`` False 면 계획하지 않는다."""
    items: list[InventoryItem] = field(default_factory=list)
    complete: bool = True
    notes: list[str] = field(default_factory=list)
    _nested = {"items": InventoryItem}


# ---------------------------------------------------------------------------- 계획 / plan
# 변경의 종류 / change kinds
K_DIRECT_VERSION = "direct_version"               # 의존성의 <version> 글자
K_PROPERTY_VERSION = "property_version"           # 프로젝트 pom 의 속성 값
K_OWN_MANAGEMENT_VERSION = "own_management_version"   # 프로젝트 <dependencyManagement> 의 버전 글자
K_MANAGED_OVERRIDE = "managed_override"           # 간접 의존성·부모/BOM 관리 버전: 루트 pom 의 <dependencyManagement> 에 override 항목을 넣음(늘 승인)
K_PARENT_VERSION = "parent_version"               # <parent><version>
K_BOM_VERSION = "bom_version"                     # import 한 BOM 의 <version>
# 위험 표시(승인 부류) / risk flags = `remediation.always_approve` 의 값들
RISK_MAJOR, RISK_PARENT, RISK_BOM, RISK_MANAGED_OVERRIDE = "major", "parent", "bom", "managed_override"

# 제안의 결정 상태 / decision states
D_AUTO = "auto"            # 승인 없이 적용되는 제안
D_PENDING = "pending"      # 승인 대기
D_APPROVED = "approved"
D_REJECTED = "rejected"


E_REPLACE = "replace"     # 기존 버전 글자 바꾸기
E_INSERT = "insert"       # 관리 버전 override 항목 넣기 (K_MANAGED_OVERRIDE 제안에서만)


@dataclass
class TextEdit(Record):
    """글자 변경 하나. ``op`` 가 replace(기본)면 ``pom`` 의 ``line`` 번째 줄에서 ``old`` 를 ``new`` 로 바꾼다(그 줄에 ``old`` 가
    정확히 한 번 있어야 하고, ``old`` / ``new`` 는 버전 글자뿐이다 — 태그나 다른 글자를 포함하지 않는다).

    ``op`` 가 insert 면 (사용자 결정 2026-11-11: 간접 의존성·부모/BOM 관리 버전은 "관리 버전 override 항목 추가" 로 고치되 늘
    승인을 받는다) ``line`` 번째 줄 **앞에** ``new`` (온전한 줄들, 줄 끝 포함)를 끼워 넣는다. ``old`` 는 빈 글자열이고 ``anchor`` 는
    그 ``line`` 번째 줄의 지금 글자(앞뒤 공백 뺀 것) — 적용할 때 그 줄이 그대로인지 확인한다. 넣는 글자는 ``override`` 의
    g / a / version 으로만 만들어진 정해진 모양이어야 한다: 루트 pom 의 ``<dependencyManagement><dependencies>`` 안의
    ``<dependency>`` 항목 하나(groupId / artifactId / version 만), 또는 그 절이 없을 때 그 항목 하나를 담은
    ``<dependencyManagement>`` 절 전체. 다른 어떤 XML 도 넣지 않는다."""
    pom: str
    line: int
    old: str
    new: str
    op: str = E_REPLACE
    anchor: str = ""
    override: dict[str, str] = field(default_factory=dict)    # insert 일 때 {"groupId", "artifactId", "version"}


@dataclass
class Proposal(Record):
    """변경 제안 하나 = 승인의 단위. ``diff`` 는 이 제안만 적용했을 때의 통합 diff(정확한 글자), ``evidence`` 는 새 버전의 근거
    ("policy:pins" 또는 "repository:<호스트>" — URL 의 자격증명은 넣지 않는다).
    해시 세 개가 승인을 지금의 후보에 묶는다: ``proposal_hash`` (ga·버전·종류·edits·diff), ``source_tree_hash`` (제안을 만든
    accepted 트리), ``policy_hash`` (서버 정책)."""
    id: str                           # p-<round>-<n>
    round: int
    ga: str
    from_version: str
    to_version: str
    kind: str
    risks: list[str] = field(default_factory=list)
    requires_approval: bool = False
    decision: str = D_AUTO
    evidence: str = ""
    finding_ids: list[str] = field(default_factory=list)
    edits: list[TextEdit] = field(default_factory=list)
    diff: str = ""
    proposal_hash: str = ""
    source_tree_hash: str = ""
    policy_hash: str = ""
    note: str = ""
    _nested = {"edits": TextEdit}

    def compute_hash(self) -> str:
        """``proposal_hash`` 의 정의: 아래 값들의 정규 JSON 의 sha256. decision / note 는 들어가지 않는다."""
        return sha256_text(canonical_json({
            "ga": self.ga, "from": self.from_version, "to": self.to_version, "kind": self.kind,
            "risks": sorted(self.risks), "edits": [e.to_dict() for e in self.edits], "diff": self.diff}))


@dataclass
class Residual(Record):
    """자동으로 바꾸지 않고 남긴 항목(잔여·수동 검토). ``reason`` 은 이유 코드(R_MANUAL_REVIEW / R_NO_CANDIDATE_VERSION /
    policy_denied / unknown_provenance …), ``detail`` 은 사람이 읽을 한 줄."""
    ga: str
    version: str
    origin: str
    reason: str
    detail: str = ""
    finding_ids: list[str] = field(default_factory=list)


@dataclass
class RoundPlan(Record):
    """라운드 하나의 계획: 제안들 + 남긴 항목들. ``source_tree_hash`` / ``policy_hash`` 는 모든 제안에 같은 값으로 들어간다."""
    round: int
    source_tree_hash: str
    policy_hash: str
    proposals: list[Proposal] = field(default_factory=list)
    residuals: list[Residual] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    _nested = {"proposals": Proposal, "residuals": Residual}

    def pending(self) -> list[Proposal]:
        """아직 결정을 기다리는 제안."""
        return [p for p in self.proposals if p.decision == D_PENDING]

    def to_apply(self) -> list[Proposal]:
        """적용할 제안: 자동 + 승인됨."""
        return [p for p in self.proposals if p.decision in (D_AUTO, D_APPROVED)]


@dataclass
class ApplyResult(Record):
    """후보 사본에 적용한 결과. ``patch`` 는 accepted → candidate 전체의 통합 diff."""
    applied: list[str] = field(default_factory=list)          # proposal ids
    skipped: dict[str, str] = field(default_factory=dict)     # proposal id → 이유
    changed_files: list[str] = field(default_factory=list)
    patch: str = ""
    candidate_tree_hash: str = ""


@dataclass
class Decision(Record):
    """승인 요청의 한 줄. 세 해시와 diff 가 저장된 제안과 **모두** 같아야 받아들인다."""
    proposal_id: str
    decision: str                     # approved / rejected
    proposal_hash: str = ""
    source_tree_hash: str = ""
    policy_hash: str = ""
    diff: str = ""
    by: str = ""
    comment: str = ""


# ---------------------------------------------------------------------------- 작업 상태 / state
@dataclass
class RoundState(Record):
    """라운드 하나의 진행 기록."""
    round: int
    plan: str = ""                    # rounds/<n>/plan.json
    applied: list[str] = field(default_factory=list)
    build: BuildOutcome | None = None
    verify_problems: list[str] = field(default_factory=list)
    scan_label: str = ""
    committed: bool = False           # 후보가 accepted 로 확정되었는가
    _nested = {"build": BuildOutcome}


@dataclass
class RemediationState(Record):
    """``state.json`` — 작업 하나의 전부. ``next_node`` 는 재개할 때 들어갈 노드(그 노드를 처음부터 다시 한다; 단
    검사 노드는 ``pending_upload`` / ``pending_receipt`` 가 있으면 다시 올리지 않고 이어서 조회한다).
    ``owner`` 는 지금 이 작업을 돌리는 프로세스의 토큰 — 서버가 새로 뜨면 토큰이 달라지고, RUNNING / QUEUED 로 남은 작업은
    INTERRUPTED 가 된다."""
    id: str
    name: str = ""                    # 올린 파일 이름
    status: str = QUEUED
    reason: str = ""
    message: str = ""
    created: float = 0.0
    updated: float = 0.0
    node: str = ""                    # 지금(또는 마지막으로) 돈 노드
    next_node: str = NODES[0]
    round: int = 0
    stop_requested: bool = False
    owner: str = ""
    configs: list[str] = field(default_factory=list)
    policy_hash: str = ""
    require_approval: bool = False
    intake: IntakeResult | None = None
    boundary: list[BoundaryFinding] = field(default_factory=list)
    baseline_build: BuildOutcome | None = None
    baseline_scan: str = ""           # scans/baseline.json
    final_scan: str = ""              # scans/final-r<n>.json
    # 업로드를 보낸 뒤 결과를 모르는 검사(label → zip sha256): 사람이 확인해야 한다
    pending_upload: dict[str, str] = field(default_factory=dict)
    # 업로드가 받아들여졌고 분석을 기다리는 검사(label → UploadReceipt dict): 재개하면 다시 올리지 않고 이어서 조회한다
    pending_receipt: dict[str, dict] = field(default_factory=dict)
    rounds: list[RoundState] = field(default_factory=list)
    delivery: DeliveryInfo | None = None
    residuals: list[Residual] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)          # 성공이 아닌 모든 이유(evaluate 가 채운다)
    counts: dict[str, int] = field(default_factory=dict)      # baseline / final / fixed / residual …
    # 이관의 최종 빌드가 돈 대상 폴더(프로젝트 루트 기준). 빈 목록 = 루트에서 한 번(이관과 같다). 루트 aggregator 가 없는 MSA 는
    # 이관이 서비스마다 빌드했으므로 조치도 같은 폴더에서 같은 방식으로 빌드한다(``maven.ServerMaven`` 의 ``targets``)
    build_targets: list[str] = field(default_factory=list)
    schema: int = SCHEMA
    _nested = {"intake": IntakeResult, "boundary": BoundaryFinding, "baseline_build": BuildOutcome,
               "rounds": RoundState, "delivery": DeliveryInfo, "residuals": Residual}


class RunPaths:
    """실행 폴더 안의 경로 규칙(모듈 docstring 의 표). 모든 파일이 이 클래스를 거쳐 같은 자리를 본다.

    Path rules inside one remediation's directory."""

    def __init__(self, root: Path | str):
        self.root = Path(root)

    @property
    def original_zip(self) -> Path: return self.root / "original" / "source.zip"
    @property
    def manifest(self) -> Path: return self.root / "original" / "manifest.json"
    @property
    def accepted(self) -> Path: return self.root / "accepted"
    @property
    def state(self) -> Path: return self.root / "state.json"
    @property
    def approvals(self) -> Path: return self.root / "approvals.jsonl"
    @property
    def journal(self) -> Path: return self.root / "journal.jsonl"
    @property
    def log(self) -> Path: return self.root / "remediation.log"
    @property
    def report_md(self) -> Path: return self.root / "report.md"
    @property
    def report_json(self) -> Path: return self.root / "report.json"
    @property
    def delivery_zip(self) -> Path: return self.root / "delivery" / "result.zip"
    @property
    def delivery_json(self) -> Path: return self.root / "delivery" / "delivery.json"
    @property
    def baseline_log(self) -> Path: return self.root / "baseline" / "build.log"

    def round_dir(self, n: int) -> Path: return self.root / "rounds" / str(n)
    def candidate(self, n: int) -> Path: return self.round_dir(n) / "candidate"
    def plan(self, n: int) -> Path: return self.round_dir(n) / "plan.json"
    def patch(self, n: int) -> Path: return self.round_dir(n) / "candidate.patch"
    def build_log(self, n: int) -> Path: return self.round_dir(n) / "build.log"
    def resolved(self, n: int) -> Path: return self.round_dir(n) / "resolved.json"
    def scan(self, label: str) -> Path: return self.root / "scans" / f"{label}.json"

    def rel(self, p: Path) -> str:
        """실행 폴더 기준 상대 경로(슬래시)."""
        return Path(p).resolve().relative_to(self.root.resolve()).as_posix()

    def project(self, base: Path, project_root: str) -> Path:
        """``base`` (accepted 또는 후보 사본) 안의 프로젝트 루트 폴더."""
        return base if project_root in ("", ".") else base / project_root


def write_json_atomic(path: Path, data: Any) -> None:
    """JSON 파일을 원자적으로 쓴다(임시 파일 → os.replace). state.json 이 반쯤 쓰인 채 남지 않게."""
    import os
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path) -> Any:
    """JSON 파일을 읽는다. 없거나 깨졌으면 None."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
