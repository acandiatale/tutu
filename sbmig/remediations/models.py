"""취약점 조치의 여러 파일이 함께 쓰는 상태 값, 데이터 클래스, 오류, 해시 규칙, 실행 폴더 구조를 정의하는 모듈.
다른 파일들은 서로를 직접 참조하지 않고 이 파일의 데이터 클래스와 ``interfaces.py``의 Protocol만 쓴다.
데이터는 ``to_dict`` / ``from_dict``로 JSON 파일에 저장하고, 비밀 값은 어디에도 넣지 않는다.
실행 폴더 ``<out>/_remediations/<id>/``(``RunPaths``)의 구성은 아래와 같다.
- ``original/source.zip``: 업로드한 ZIP 원본, ``original/manifest.json``: 원본 파일 목록
- ``accepted/``: 작업 트리. 승인되어 적용된 변경만 들어간다
- ``rounds/<n>/``: 라운드별 수정본(``candidate/``)과 plan.json, candidate.patch, build.log, resolved.json
- ``scans/<label>.json``: 검사 결과, ``delivery/result.zip``과 delivery.json: 결과 ZIP. 뒤 라운드에서 빌드가
  실패한 수정본이 result.zip이 되면 앞 라운드에서 검증된 결과를 ``result-r<k>-verified.zip``으로 남긴다
- ``state.json``: 작업 상태(통째로 교체해서 저장), ``approvals.jsonl``: 승인과 거절 기록(뒤에 추가만 한다)
- ``journal.jsonl`` / ``remediation.log`` / ``report.md`` / ``report.json``: 저널, 로그, 보고서
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
QUEUED = "QUEUED"                        # 접수되어 작업 스레드를 기다리는 중
RUNNING = "RUNNING"
AWAITING_APPROVAL = "AWAITING_APPROVAL"  # 승인을 기다린다. approvals 뒤에 resume한다
AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"   # IVAS 업로드 결과를 알 수 없어 사람이 확인한 뒤 resume(confirm=…)한다
STOPPED = "STOPPED"                      # 노드 사이에서 중지 요청을 처리했으며 resume할 수 있다
INTERRUPTED = "INTERRUPTED"              # 실행 중에 서버가 내려갔다. 자동으로 재개하지 않고 명시적인 resume만 받는다
SUCCEEDED = "SUCCEEDED"                  # 성공 조건을 모두 만족했다(evaluate 참고)
INCOMPLETE = "INCOMPLETE"                # 끝났지만 성공은 아니다. 남은 취약점, 직접 확인, 미완료 검사 등
FAILED = "FAILED"                        # 조치 전 빌드 실패, 수정본 빌드 실패, 검증 실패 등
STATUSES = (QUEUED, RUNNING, AWAITING_APPROVAL, AWAITING_CONFIRMATION, STOPPED, INTERRUPTED, SUCCEEDED, INCOMPLETE, FAILED)
TERMINAL = (SUCCEEDED, INCOMPLETE, FAILED)                       # 더 이상 진행하지 않는 상태
RESUMABLE = (AWAITING_APPROVAL, AWAITING_CONFIRMATION, STOPPED, INTERRUPTED)
ACTIVE = (QUEUED, RUNNING)

# 실행 순서대로 나열한 그래프 노드 이름 / graph nodes in order
NODES = ("baseline_build", "baseline_scan", "inventory", "plan", "apply", "build", "freeze_delivery", "rescan",
         "evaluate", "report")

# state.reason과 보고서에 쓰는 사유 코드다. API 응답에 그대로 나가므로 문자열을 바꾸지 않는다
R_BASELINE_BUILD_FAILED = "baseline_build_failed"
R_CANDIDATE_BUILD_FAILED = "candidate_build_failed"
R_SCAN_INCOMPLETE = "scan_incomplete"
R_UPLOAD_UNCERTAIN = "upload_uncertain"
R_ZIP_HASH_MISMATCH = "zip_hash_mismatch"
R_TEXT_ONLY_VIOLATION = "text_only_violation"            # 수정본에 버전 문자열 말고 다른 변경이 있다
R_RESOLUTION_MISMATCH = "resolution_mismatch"            # 의존성 해석이나 패키징 결과가 계획과 다르다
R_BOUNDARY_REFUSED = "execution_boundary_refused"        # .mvn 설정, 승인되지 않은 저장소 등
R_NOT_READY = "not_ready"                                # 필수 설정이 없다. IVAS 주소와 계정, settings.xml, JDK 21
R_PENDING_APPROVAL = "pending_approval"
R_REJECTED_PROPOSALS = "rejected_proposals"
R_RESIDUAL_FINDINGS = "residual_findings"                # 최종 검사에 취약점이 남았다
R_MANUAL_REVIEW = "manual_review"                        # vendored, plugin, 매핑 불가, 출처 불명
R_NO_CANDIDATE_VERSION = "no_candidate_version"
R_ROUNDS_EXHAUSTED = "rounds_exhausted"
R_STOP_REQUESTED = "stop_requested"
R_INTERNAL_ERROR = "internal_error"
R_POLICY_DENIED = "policy_denied"                        # 서버 정책의 deny에 걸린 항목
R_UNKNOWN_PROVENANCE = "unknown_provenance"            # 어느 parent / BOM이 버전을 관리하는지 분명하지 않다
R_STALE_LOCATION = "stale_location"                    # 인벤토리 이후 파일이 바뀌어 그 위치에 버전 문자열이 없다
R_INVENTORY_INCOMPLETE = "inventory_incomplete"
# 사내 아티팩트(``remediation.inhouse_groups``) 자체이거나 그것을 거쳐 들어온 취약 라이브러리다.
# 그 담당자가 pom을 고쳐야 하므로
# 제안하지 않고 조치하지 못한 항목으로 남긴다. 보고서에는 ``remediation.inhouse_message``를 붙인다
R_INHOUSE_ARTIFACT = "inhouse_artifact"

T = TypeVar("T")


def canonical_json(value: Any) -> str:
    """해시 계산에 쓸 정규화된 JSON 문자열을 만든다.
    키를 정렬하고 공백 없이 비 ASCII 문자를 그대로 쓰므로 같은 값이면 항상 같은 문자열이 나온다.

    Canonical JSON for hashing (sorted keys, no whitespace)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    """UTF-8 문자열의 sha256을 16진수 문자열로 반환한다."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    """파일 내용의 sha256을 16진수 문자열로 반환한다. 큰 ZIP도 나눠서 읽는다."""
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
    """dataclass에 ``to_dict``와 ``from_dict``를 제공하는 공통 믹스인 클래스.
    ``from_dict``는 모르는 키를 버리므로 새 버전이 저장한 파일도 읽을 수 있다.
    중첩된 데이터 클래스는 각 클래스의 ``_nested``에 지정한다.

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
    """취약점 조치 기능에서 쓰는 오류의 공통 부모 클래스. ``code``는 사유 코드나 각 오류의 고유 코드다."""
    code = R_INTERNAL_ERROR

    def __init__(self, message: str, code: str | None = None, **details: Any):
        super().__init__(message)
        if code:
            self.code = code
        self.details = details


class IntakeRejected(RemediationError):
    """ZIP이 접수 규칙을 어겼을 때 던지는 오류(HTTP 400).
    ``code`` 예: not_zip / encrypted / path_traversal / absolute_path /
    symlink / special_file / duplicate_entry / normalized_collision / too_many_entries / too_large /
    compression_ratio / no_pom / empty."""
    code = "intake_rejected"


class NotReady(RemediationError):
    """필수 준비 조건이 없을 때 던지는 오류(HTTP 503).
    IVAS 주소와 계정, 서버 settings.xml, JDK 21, Maven이 필요하다."""
    code = R_NOT_READY


class IvasError(RemediationError):
    """IVAS 호출이 실패했을 때 던지는 오류. 이때 검사 결과는 0건이 아니라 미완료로 처리한다."""
    code = "ivas_error"


class IvasUploadUncertain(IvasError):
    """업로드 요청은 보냈지만 결과를 알 수 없을 때 던지는 오류. 자동으로 다시 업로드하지 않는다.
    작업은 AWAITING_CONFIRMATION이 되고, 사람이 IVAS에서 확인한 뒤 resume한다."""
    code = R_UPLOAD_UNCERTAIN


class ApprovalMismatch(RemediationError):
    """승인 요청의 해시나 diff가 현재 수정본과 다를 때 던지는 오류(HTTP 409)."""
    code = "approval_mismatch"


class StateConflict(RemediationError):
    """현재 상태에서 할 수 없는 요청일 때 던지는 오류(HTTP 409).
    실행 중인 작업 삭제, 재개할 수 없는 상태에서의 resume 등이다."""
    code = "state_conflict"


# ---------------------------------------------------------------------------- 접수 / intake
@dataclass
class IntakeLimits(Record):
    """ZIP 검사 한도(``remediation.max_*``)를 바이트 단위로 담는 데이터 클래스."""
    max_zip_bytes: int = 1024 * 1024 * 1024
    max_entries: int = 200_000
    max_unpacked_bytes: int = 4096 * 1024 * 1024
    max_entry_bytes: int = 1024 * 1024 * 1024
    max_compression_ratio: int = 200


@dataclass
class ManifestEntry(Record):
    """원본 파일 하나의 ZIP 안 경로(정규화된 값), 크기, sha256을 담는 데이터 클래스."""
    path: str
    size: int
    sha256: str


@dataclass
class IntakeResult(Record):
    """접수 결과를 담는 데이터 클래스. ``project_root``는 accepted/ 안에서 루트 pom.xml이 있는 폴더의 상대
    경로이고, accepted/ 바로 아래에 있으면 "."이다."""
    original_zip: str                 # 실행 폴더 기준 상대 경로(original/source.zip)
    zip_sha256: str
    manifest: str                     # original/manifest.json
    accepted_dir: str                 # accepted
    project_root: str                 # accepted 기준 상대 경로
    tree_hash: str                    # accepted 트리의 해시(tree_hash 규칙)
    files: int = 0
    unpacked_bytes: int = 0
    notes: list[str] = field(default_factory=list)


@dataclass
class BoundaryFinding(Record):
    """서버의 실행 제한을 우회할 수 있는 요소 하나를 담는 데이터 클래스.
    ``action``은 refuse(작업 거부) 또는 ignore(기록만 함)다.
    ``code``: project_maven_config / maven_extensions / maven_wrapper / symlink / foreign_repository /
    foreign_plugin_repository / settings_in_project / unparsable_pom."""
    code: str
    path: str
    action: str
    detail: str = ""


@dataclass
class DeliveryInfo(Record):
    """확정한 결과 ZIP의 정보를 담는 데이터 클래스.
    재검사에는 이 ZIP과 같은 바이트를 업로드하고 ``sha256``으로 확인한다."""
    path: str                         # delivery/result.zip(실행 폴더 기준)
    sha256: str
    size: int
    files: int
    tree_hash: str                    # ZIP에 담은 트리의 해시
    round: int = 0
    # False면 빌드가 실패한 수정본이다(remediation.failed_candidate: include).
    # 재검사하지 않았으므로 성공 여부를 판단할 때 쓰지 않는다
    build_ok: bool = True
    scanned: bool = False             # sha256이 같은 ZIP을 IVAS가 끝까지 검사했는지 여부


# ---------------------------------------------------------------------------- IVAS 검사 / scan
@dataclass
class UploadReceipt(Record):
    """IVAS가 업로드를 접수한 정보(프로젝트 ID, 분석 ID, 업로드한 ZIP의 sha256)를 담는 데이터 클래스."""
    project_id: str
    analysis_id: str
    zip_sha256: str
    project_name: str = ""


@dataclass
class Finding(Record):
    """IVAS 검사 결과 항목 하나를 정규화해서 담는 데이터 클래스.
    ``identifier``는 IVAS 값 그대로이고 ``locations``는 ZIP 안 경로다.
    ``kind``는 library / file_component / file_component_potential / sw 중 하나다.
    sw는 지원하지 않으므로 하나라도 있으면 검사를 미완료로 본다."""
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
        """중복 항목을 가려내는 키 (kind, id, identifier, version)를 반환한다."""
        return f"{self.kind}|{self.id}|{self.identifier}|{self.version}"


@dataclass
class ScanResult(Record):
    """검사 한 번의 결과를 담는 데이터 클래스.
    ``complete``가 False면 ``findings``가 비어 있어도 0건이 아니므로 성공 여부 판단과 계획에 쓰지 않는다.
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
    """Maven이 실제로 고른 의존성 하나를 담는 데이터 클래스.
    ``path``는 모듈에서 이 의존성까지 거치는 g:a:v 경로이고, 직접 의존성이면 빈 목록이다."""
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
    """모듈 하나의 의존성 해석 결과를 담는 데이터 클래스.
    ``dir``은 프로젝트 루트 기준 경로이고, ``plugins``는 자동으로 바꾸지 않는 빌드 플러그인과 그 의존성이다."""
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
    """프로젝트 전체의 의존성 해석 결과를 담는 데이터 클래스.
    ``complete``가 False면 일부 모듈을 읽지 못한 것이므로 계획과 검증에 쓸 수 없다."""
    modules: list[ModuleResolution] = field(default_factory=list)
    complete: bool = True
    notes: list[str] = field(default_factory=list)
    # reactor 루트 폴더 목록이다.
    # 보통은 빈 목록(루트 pom 하나)이고, 이관이 서비스마다 따로 빌드한 프로젝트만 여러 개다
    roots: list[str] = field(default_factory=list)
    _nested = {"modules": ModuleResolution}

    def versions_of(self, ga: str) -> set[str]:
        """이 g:a가 모든 모듈에서 해석된 버전들을 집합으로 반환한다."""
        return {d.version for m in self.modules for d in m.dependencies if d.ga == ga}


@dataclass
class PackagedLib(Record):
    """빌드 결과물 안에 실제로 들어간 jar 하나를 담는 데이터 클래스.
    ``ga``는 pom.properties로 알아낸 경우에만 채운다."""
    artifact: str                     # 빌드 결과물의 프로젝트 루트 기준 경로(예: app/target/app.war)
    entry: str                        # 빌드 결과물 안의 경로(예: WEB-INF/lib/foo-1.2.jar)
    file_name: str
    ga: str = ""
    version: str = ""


@dataclass
class BuildOutcome(Record):
    """package 빌드 한 번의 결과를 담는 데이터 클래스. ``ok``는 BUILD SUCCESS일 때만 True다."""
    ok: bool
    exit_code: int | None = None
    log: str = ""                     # 로그 파일(실행 폴더 기준 상대 경로)
    summary: str = ""                 # 비밀 값을 뺀 실패 요약 몇 줄
    seconds: float = 0.0
    timed_out: bool = False


# ---------------------------------------------------------------------------- 인벤토리 / inventory
# 발견 항목의 라이브러리가 프로젝트의 어디에서 오는지 / where a finding's library comes from
O_DIRECT_LITERAL = "direct_literal"           # pom의 <dependency><version>1.2</version>
O_DIRECT_PROPERTY = "direct_property"         # <version>${x.version}</version>이고 속성은 프로젝트 pom에 있다
O_OWN_MANAGEMENT = "own_management"           # 프로젝트 pom의 <dependencyManagement>가 버전을 정한다
O_PARENT_MANAGED = "parent_managed"           # 외부 부모 pom이 관리하므로 부모 버전을 바꾸거나 override해야 한다
O_BOM_MANAGED = "bom_managed"                 # import한 BOM이 관리한다
O_TRANSITIVE = "transitive"                   # 프로젝트에 선언 없이 다른 의존성이 끌고 온다
O_PLUGIN = "plugin"                           # 빌드 플러그인이나 그 의존성이라 자동으로 바꾸지 않는다
O_VENDORED = "vendored"                       # 트리에 들어 있는 파일(jar, js 등)이라 자동으로 바꾸지 않는다
O_UNMAPPED = "unmapped"                       # Maven 해석 결과와 연결하지 못해 자동으로 바꾸지 않는다
AUTO_ORIGINS = (O_DIRECT_LITERAL, O_DIRECT_PROPERTY, O_OWN_MANAGEMENT, O_PARENT_MANAGED, O_BOM_MANAGED, O_TRANSITIVE)
MANUAL_ORIGINS = (O_PLUGIN, O_VENDORED, O_UNMAPPED)


@dataclass
class PomLocation(Record):
    """버전 문자열이 기록된 위치를 담는 데이터 클래스.
    ``pom``은 프로젝트 루트 기준 경로, ``line``은 1부터 세는 줄 번호, ``text``는 버전 문자열 그대로다.
    버전을 속성으로 정하면 ``property``에 속성 이름을 담고, line / text는 그 속성이 정의된 위치를 가리킨다."""
    pom: str
    line: int
    text: str
    property: str = ""
    element: str = "dependency"       # dependency / management / property / parent / bom


@dataclass
class InventoryItem(Record):
    """취약한 라이브러리 하나(g:a와 해석된 버전)와 그것을 가리키는 발견 항목들을 담는 데이터 클래스.
    ``origin``에 따라 자동으로 바꿀 수 있는지가 정해진다.
    ``location``은 고칠 수 있는 버전 문자열의 위치, ``manager``는 버전을 관리하는 부모 / BOM, ``reason``은
    직접 확인 항목으로 남기는 이유다."""
    ga: str
    version: str
    origin: str
    finding_ids: list[str] = field(default_factory=list)
    # IVAS가 알려 준 패치 버전으로, 참고할 힌트일 뿐 근거는 아니다.
    # 줄은 모두 만족해야 하고(AND), 한 줄 안의 여러 버전은 버전 라인별 대안이다(OR)
    patched_versions: list[str] = field(default_factory=list)
    modules: list[str] = field(default_factory=list)
    # ``modules`` 중 ``location``이 버전을 정한다는 근거가 없는 모듈이다.
    # 같은 g:a:version이 다른 경로로 해석된 곳이므로
    # 검증에서 실패로 치지 않고, 남아 있으면 다음 라운드에서 따로 계획한다
    also_modules: list[str] = field(default_factory=list)
    # 이 라이브러리를 끌고 온 사내 아티팩트의 g:a 목록이다. 항목 자체가 사내 아티팩트면 자기 g:a를 담는다.
    # ``inhouse_modules``는 그렇게 들어온 모듈 목록이다
    inhouse: list[str] = field(default_factory=list)
    inhouse_modules: list[str] = field(default_factory=list)
    via: list[str] = field(default_factory=list)                  # transitive일 때 끌고 오는 경로(g:a:v)
    location: PomLocation | None = None
    manager: str = ""                                             # 버전을 관리하는 부모 / BOM의 g:a:v
    manager_location: PomLocation | None = None
    reason: str = ""
    paths: list[str] = field(default_factory=list)                # vendored 파일 경로
    _nested = {"location": PomLocation, "manager_location": PomLocation}


@dataclass
class Inventory(Record):
    """발견 항목과 프로젝트 사이의 연결 전체를 담는 데이터 클래스. ``complete``가 False면 계획하지 않는다."""
    items: list[InventoryItem] = field(default_factory=list)
    complete: bool = True
    notes: list[str] = field(default_factory=list)
    _nested = {"items": InventoryItem}


# ---------------------------------------------------------------------------- 계획 / plan
# 변경 종류 / change kinds
K_DIRECT_VERSION = "direct_version"               # 의존성의 <version> 값
K_PROPERTY_VERSION = "property_version"           # 프로젝트 pom의 속성 값
K_OWN_MANAGEMENT_VERSION = "own_management_version"   # 프로젝트 <dependencyManagement>의 버전 값
K_MANAGED_OVERRIDE = "managed_override"           # 간접 / 부모 / BOM 관리 버전. 루트 pom에 override를 넣고 늘 승인받는다
K_PARENT_VERSION = "parent_version"               # <parent><version>
K_BOM_VERSION = "bom_version"                     # import한 BOM의 <version>
# 승인이 필요한 위험 표시 / risk flags = `remediation.always_approve` 값
RISK_MAJOR, RISK_PARENT, RISK_BOM, RISK_MANAGED_OVERRIDE = "major", "parent", "bom", "managed_override"

# 제안의 결정 상태 / decision states
D_AUTO = "auto"            # 승인 없이 적용되는 제안
D_PENDING = "pending"      # 승인 대기
D_APPROVED = "approved"
D_REJECTED = "rejected"


E_REPLACE = "replace"     # 기존 버전 문자열을 바꾼다
E_INSERT = "insert"       # 관리 버전 override 항목을 추가한다. K_MANAGED_OVERRIDE 제안에서만 쓴다


@dataclass
class TextEdit(Record):
    """pom 텍스트 변경 하나를 담는 데이터 클래스.
    - replace(기본값): ``line``번째 줄에서 ``old``를 ``new``로 바꾼다.
      ``old``는 그 줄에 정확히 한 번 있어야 하고, 둘 다 버전 문자열이어야 한다
    - insert: ``line``번째 줄 앞에 ``new`` 줄들을 넣는다.
      ``old``는 빈 문자열이고, 적용할 때 그 줄이 ``anchor``와 같은지 확인한다
    insert로 넣을 수 있는 내용은 ``override``의 groupId / artifactId / version으로 만든 ``<dependency>``
    하나, 또는 그것을 담은 ``<dependencyManagement>`` 절뿐이다."""
    pom: str
    line: int
    old: str
    new: str
    op: str = E_REPLACE
    anchor: str = ""
    override: dict[str, str] = field(default_factory=dict)    # insert일 때 {"groupId", "artifactId", "version"}


@dataclass
class Proposal(Record):
    """변경 제안 하나를 담는 데이터 클래스이며, 승인은 이 단위로 한다.
    ``diff``는 이 제안만 적용한 diff이고, ``evidence``는 새 버전의 근거다(계정 정보는 넣지 않는다).
    세 해시로 승인을 현재 수정본에 묶는다: ``proposal_hash``, ``source_tree_hash``(제안을 만든 accepted
    트리), ``policy_hash``(서버 정책)."""
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
    # 이 제안이 버전을 정하는 모듈들이다.
    # 검증은 이 모듈에서 from이 to로 바뀌었는지 보고, 비어 있으면 from 버전이던 모든 모듈을 본다
    # ``also_modules``는 다른 경로로 해석된 모듈이라 그대로 남아도 실패가 아니다.
    # 둘 다 ``proposal_hash``에 넣지 않는다
    modules: list[str] = field(default_factory=list)
    also_modules: list[str] = field(default_factory=list)
    _nested = {"edits": TextEdit}

    def compute_hash(self) -> str:
        """``proposal_hash``를 계산한다. decision / note / modules / also_modules는 해시에 넣지 않는다."""
        return sha256_text(canonical_json({
            "ga": self.ga, "from": self.from_version, "to": self.to_version, "kind": self.kind,
            "risks": sorted(self.risks), "edits": [e.to_dict() for e in self.edits], "diff": self.diff}))


@dataclass
class Residual(Record):
    """자동으로 바꾸지 않고 남긴 항목을 담는 데이터 클래스.
    ``reason``은 사유 코드, ``detail``은 사람이 읽는 설명 한 줄이다."""
    ga: str
    version: str
    origin: str
    reason: str
    detail: str = ""
    finding_ids: list[str] = field(default_factory=list)
    # 보고서 표에 쓰는 값이다.
    # 이 버전이 해석된 모듈, 끌고 온 가장 짧은 경로, 끌고 온 사내 아티팩트의 g:a를 담는다
    # ``reason``이 inhouse_artifact가 아니어도 일부 모듈이 사내 아티팩트를 거쳐 들어왔으면 ``inhouse``를 채운다
    modules: list[str] = field(default_factory=list)
    via: list[str] = field(default_factory=list)
    inhouse: list[str] = field(default_factory=list)


@dataclass
class RoundPlan(Record):
    """라운드 하나의 계획(제안과 남긴 항목)을 담는 데이터 클래스.
    ``source_tree_hash`` / ``policy_hash``에는 모든 제안과 같은 값이 들어간다."""
    round: int
    source_tree_hash: str
    policy_hash: str
    proposals: list[Proposal] = field(default_factory=list)
    residuals: list[Residual] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    _nested = {"proposals": Proposal, "residuals": Residual}

    def pending(self) -> list[Proposal]:
        """결정을 기다리는 제안 목록을 반환한다."""
        return [p for p in self.proposals if p.decision == D_PENDING]

    def to_apply(self) -> list[Proposal]:
        """적용할 제안, 곧 자동 적용 대상과 승인된 제안을 반환한다."""
        return [p for p in self.proposals if p.decision in (D_AUTO, D_APPROVED)]


@dataclass
class ApplyResult(Record):
    """수정본에 변경을 적용한 결과를 담는 데이터 클래스.
    ``patch``는 accepted와 candidate 사이의 전체 diff다."""
    applied: list[str] = field(default_factory=list)          # proposal ids
    skipped: dict[str, str] = field(default_factory=dict)     # proposal id별로 건너뛴 이유
    changed_files: list[str] = field(default_factory=list)
    patch: str = ""
    candidate_tree_hash: str = ""


@dataclass
class Decision(Record):
    """승인 요청 한 건을 담는 데이터 클래스. 세 해시와 diff가 저장된 제안과 모두 같아야 받아들인다."""
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
    """라운드 하나의 진행 기록을 담는 데이터 클래스."""
    round: int
    plan: str = ""                    # rounds/<n>/plan.json
    applied: list[str] = field(default_factory=list)
    build: BuildOutcome | None = None
    verify_problems: list[str] = field(default_factory=list)
    scan_label: str = ""
    committed: bool = False           # 수정본이 accepted로 확정되었는지 여부
    _nested = {"build": BuildOutcome}


@dataclass
class RemediationState(Record):
    """작업 하나의 상태 전체(``state.json``)를 담는 데이터 클래스.
    - ``next_node``: 재개할 때 처음부터 다시 실행할 노드. 검사 노드는 ``pending_upload`` /
      ``pending_receipt``가 있으면 다시 업로드하지 않고 이어서 결과를 조회한다
    - ``owner``: 이 작업을 실행하는 프로세스의 토큰.
      서버가 재시작되면 RUNNING / QUEUED로 남아 있던 작업은 INTERRUPTED가 된다"""
    id: str
    name: str = ""                    # 업로드한 파일 이름
    status: str = QUEUED
    reason: str = ""
    message: str = ""
    created: float = 0.0
    updated: float = 0.0
    node: str = ""                    # 현재 또는 마지막으로 실행한 노드
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
    # 업로드를 보냈지만 결과를 모르는 검사(label별 zip sha256)이므로 사람이 확인해야 한다
    pending_upload: dict[str, str] = field(default_factory=dict)
    # 업로드가 접수되어 분석을 기다리는 검사다. 재개하면 다시 업로드하지 않고 이어서 조회한다
    pending_receipt: dict[str, dict] = field(default_factory=dict)
    rounds: list[RoundState] = field(default_factory=list)
    delivery: DeliveryInfo | None = None
    residuals: list[Residual] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)          # 성공이 아닌 모든 이유를 evaluate가 채운다
    counts: dict[str, int] = field(default_factory=dict)      # baseline / final / fixed / residual …
    # 이관의 최종 빌드 대상 폴더다. 빈 목록이면 루트에서 한 번 빌드한다
    # 루트 aggregator가 없는 MSA는 이관처럼 서비스마다 빌드한다(``maven.ServerMaven``의 ``targets``)
    build_targets: list[str] = field(default_factory=list)
    schema: int = SCHEMA
    _nested = {"intake": IntakeResult, "boundary": BoundaryFinding, "baseline_build": BuildOutcome,
               "rounds": RoundState, "delivery": DeliveryInfo, "residuals": Residual}


class RunPaths:
    """실행 폴더 안의 파일 경로를 정하는 클래스. 모든 파일 경로는 이 클래스로 얻는다.

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
        """실행 폴더 기준 상대 경로를 '/' 구분자로 반환한다."""
        return Path(p).resolve().relative_to(self.root.resolve()).as_posix()

    def project(self, base: Path, project_root: str) -> Path:
        """``base``(accepted 또는 수정본) 안의 프로젝트 루트 폴더 경로를 반환한다."""
        return base if project_root in ("", ".") else base / project_root


def write_json_atomic(path: Path, data: Any) -> None:
    """JSON 파일을 임시 파일에 쓴 뒤 os.replace로 바꿔 넣는다.
    state.json이 반쯤 쓰인 채로 남지 않게 하기 위해서다."""
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
    """JSON 파일을 읽는다. 파일이 없거나 깨졌으면 None을 반환한다."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
