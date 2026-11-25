"""취약점 조치 기능이 함께 쓰는 상태 값, 데이터 클래스, 예외, 해시 규칙, 실행 폴더 구조를 정의하는 모듈.
다른 파일들은 서로 직접 참조하지 않고 이 모듈과 ``interfaces.py``만 참조한다.
데이터는 ``to_dict`` / ``from_dict``로 JSON에 저장하고, 비밀 값은 넣지 않는다.
실행 폴더(``RunPaths``)의 구성은 아래와 같다.
- ``original/``: 업로드한 ZIP 원본과 파일 목록(manifest)
- ``accepted/``: 작업 트리. 빌드와 검증을 통과한 변경만 들어간다
- ``rounds/<n>/``: 라운드별 수정본과 계획, 빌드 로그
- ``scans/``, ``delivery/``: 검사 결과와 결과 ZIP
- ``state.json``, ``journal.jsonl``, ``remediation.log``, ``report.md``: 상태, 저널, 로그, 보고서

Run folder `<out>/_remediations/<id>/`. `rounds/<n>/` holds `candidate/`, plan.json, candidate.patch,
build.log, resolved.json. `delivery/result.zip` + delivery.json; when a later round delivers a failed
candidate, the earlier verified delivery is kept as `result-r<k>-verified.zip`.
`state.json` is replaced whole on every save.

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
REMEDIATIONS_DIR = "_remediations"       # 웹 출력 폴더 아래. `_`로 시작해서 이관 실행 목록에 나오지 않는다

# ---------------------------------------------------------------------------- 상태
QUEUED = "QUEUED"                        # 접수되어 작업 스레드를 기다리는 중
RUNNING = "RUNNING"
# IVAS 업로드 결과를 알 수 없어 사람이 확인한 뒤 resume(confirm=…)한다
AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
STOPPED = "STOPPED"                      # 노드 사이에서 중지 요청을 처리했으며 resume할 수 있다
INTERRUPTED = "INTERRUPTED"              # 실행 중에 서버가 내려갔다. 명시적인 resume으로만 재개한다
SUCCEEDED = "SUCCEEDED"                  # 성공 조건을 모두 만족했다(evaluate 참고)
INCOMPLETE = "INCOMPLETE"                # 끝났지만 성공은 아니다. 남은 취약점, 직접 확인, 미완료 검사 등
FAILED = "FAILED"                        # 조치 전 빌드 실패, 수정본 빌드 실패, 검증 실패 등
TERMINAL = (SUCCEEDED, INCOMPLETE, FAILED)                       # 더 이상 진행하지 않는 상태
RESUMABLE = (AWAITING_CONFIRMATION, STOPPED, INTERRUPTED)
ACTIVE = (QUEUED, RUNNING)
# 승인 단계가 있던 버전이 저장한 상태다. 읽기만 하며 재개하지 않고 끝난 작업으로 보여 준다
LEGACY_AWAITING_APPROVAL = "AWAITING_APPROVAL"
# 그런 작업의 재개를 거절할 때 쓰는 문장이다
LEGACY_APPROVAL_MESSAGE = ("the approval step of the remediation was removed: this remediation waited for an approval "
                           "and cannot be resumed; upload the project again and it runs to the end without approval")
# 화면에 그대로 보이는 문장이다. 없어진 기능이 다시 보이지 않도록 승인이라는 말을 쓰지 않는다
LEGACY_APPROVAL_MESSAGE_KO = "이 실행의 취약점 조치는 끝나지 않았습니다. 프로젝트를 다시 올리면 끝까지 진행합니다."

# 실행 순서대로 나열한 그래프 노드 이름
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
# 사내 아티팩트 자체이거나 그것을 거쳐 들어온 라이브러리다. 담당자가 고쳐야 하므로 조치하지 못한 항목으로 남긴다
R_INHOUSE_ARTIFACT = "inhouse_artifact"

T = TypeVar("T")


def canonical_json(value: Any) -> str:
    """해시 계산에 쓸 정규화된 JSON 문자열을 만든다.
    키를 정렬하고 공백을 넣지 않으므로 같은 값이면 항상 같은 문자열이 나온다."""
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
        # ``_omit_none`` 필드는 None이면 넣지 않는다. 이전 버전이 쓴 JSON과 같게 두기 위해서다
        omit = getattr(v, "_omit_none", ())
        # ``_omit_empty`` 필드는 빈 목록이나 빈 문자열이면 넣지 않는다
        empty = getattr(v, "_omit_empty", ())
        # ``_omit_false`` 필드는 False면 넣지 않는다
        false = getattr(v, "_omit_false", ())
        return {f.name: _plain(getattr(v, f.name)) for f in dataclasses.fields(v)
                if not (f.name in omit and getattr(v, f.name) is None)
                and not (f.name in empty and getattr(v, f.name) in ([], ""))
                and not (f.name in false and getattr(v, f.name) is False)}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items()}
    if isinstance(v, Path):
        return v.as_posix()
    return v


class Record:
    """데이터 클래스에 ``to_dict``와 ``from_dict``를 붙이는 믹스인 클래스.
    ``from_dict``는 모르는 키를 버리므로 새 버전이 저장한 파일도 읽을 수 있다.
    중첩된 데이터 클래스는 ``_nested``에 지정한다."""
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


# ---------------------------------------------------------------------------- 예외
class RemediationError(Exception):
    """취약점 조치 기능의 예외가 상속하는 기본 클래스. ``code``는 사유 코드나 예외별 코드다."""
    code = R_INTERNAL_ERROR

    def __init__(self, message: str, code: str | None = None, **details: Any):
        super().__init__(message)
        if code:
            self.code = code
        self.details = details


class IntakeRejected(RemediationError):
    """ZIP이 접수 규칙을 어겼을 때 던지는 예외 클래스(HTTP 400).

    Codes: not_zip / encrypted / path_traversal / absolute_path / symlink / special_file / duplicate_entry /
    normalized_collision / too_many_entries / too_large / compression_ratio / no_pom / empty."""
    code = "intake_rejected"


class NotReady(RemediationError):
    """필수 준비 조건이 없을 때 던지는 예외 클래스(HTTP 503).
    IVAS 주소와 계정, 서버 settings.xml, JDK 21, Maven이 필요하다."""
    code = R_NOT_READY


class IvasError(RemediationError):
    """IVAS 호출이 실패했을 때 던지는 예외 클래스. 이때 검사는 0건이 아니라 미완료로 처리한다."""
    code = "ivas_error"


class IvasUploadUncertain(IvasError):
    """업로드 요청을 보냈지만 결과를 알 수 없을 때 던지는 예외 클래스. 자동으로 다시 올리지 않는다.
    작업은 AWAITING_CONFIRMATION이 되고, 사람이 IVAS에서 확인한 뒤 resume한다."""
    code = R_UPLOAD_UNCERTAIN


class StateConflict(RemediationError):
    """현재 상태에서 할 수 없는 요청일 때 던지는 예외 클래스(HTTP 409).
    실행 중인 작업 삭제, 재개할 수 없는 작업의 resume 등이다. 화면용 문장은 ``details["message_ko"]``에 담는다."""
    code = "state_conflict"


# ---------------------------------------------------------------------------- 접수
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
    """원본 파일 하나의 ZIP 안 경로, 크기, sha256을 담는 데이터 클래스."""
    path: str
    size: int
    sha256: str


@dataclass
class IntakeResult(Record):
    """접수 결과를 담는 데이터 클래스.
    ``project_root``는 ``accepted/`` 안에서 루트 pom.xml이 있는 폴더다. 바로 아래에 있으면 "."이다."""
    original_zip: str                 # 실행 폴더 기준 상대 경로(original/source.zip)
    zip_sha256: str
    manifest: str                     # original/manifest.json
    accepted_dir: str                 # accepted
    project_root: str                 # accepted 기준 상대 경로
    tree_hash: str                    # accepted 트리의 해시(tree_hash 규칙)
    files: int = 0
    unpacked_bytes: int = 0
    notes: list[str] = field(default_factory=list)
    build_output_files: int = 0       # 업로드에 있었지만 manifest에서 뺀 빌드 산출물 파일 수

    def to_dict(self) -> dict[str, Any]:
        out = _plain(self)
        if not out.get("build_output_files"):
            out.pop("build_output_files", None)     # 0이면 이전 버전이 쓴 JSON과 같게 둔다
        return out


@dataclass
class BoundaryFinding(Record):
    """서버의 실행 제한을 우회할 수 있는 요소 하나를 담는 데이터 클래스.
    ``action``은 refuse(작업 거부) 또는 ignore(기록만 함)다.

    Codes: project_maven_config / maven_extensions / maven_wrapper / symlink / foreign_repository /
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
    # False면 빌드가 실패한 수정본이다. 재검사하지 않았으므로 성공 여부를 판단할 때 쓰지 않는다
    build_ok: bool = True
    scanned: bool = False             # sha256이 같은 ZIP을 IVAS가 끝까지 검사했는지 여부
    # True면 조치 전 빌드에 있던 오류로만 실패한 트리다. 재검사는 했지만 빌드로 확인한 변경은 아니다
    build_tolerated: bool = False


# ---------------------------------------------------------------------------- IVAS 검사
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
    ``kind``는 library, file_component, file_component_potential, sw 중 하나다.
    sw는 지원하지 않으므로 하나라도 있으면 검사를 미완료로 본다."""
    id: str                           # CVE-…
    kind: str
    identifier: str
    version: str = ""
    patched_version: str = ""
    locations: list[str] = field(default_factory=list)
    action: str = ""                  # action.actionDivCd 그대로
    score: float | None = None        # rating 중 가장 높은 점수
    component_id: str = ""            # 파일 컴포넌트 ID
    # True면 파일 컴포넌트의 파일 위치(matchedfiles)를 읽지 못했다
    files_unread: bool = False

    @property
    def key(self) -> str:
        """중복 항목을 가려내는 키 (kind, id, identifier, version)를 반환한다."""
        return f"{self.kind}|{self.id}|{self.identifier}|{self.version}"


@dataclass
class ScanResult(Record):
    """검사 한 번의 결과를 담는 데이터 클래스.
    ``complete``가 False면 ``findings``가 비어 있어도 0건이 아니다. 성공 판단과 계획에 쓰지 않는다.

    `incomplete_reasons`: analysis_failed / analysis_timeout / page_missing / total_mismatch /
    duplicate_result / summary_mismatch / unsupported_sw / zip_hash_mismatch / id_mismatch / http_error /
    stop_requested."""
    label: str                        # baseline / final-r<n>
    zip_sha256: str
    complete: bool
    receipt: UploadReceipt | None = None
    status_code: str = ""             # 마지막 anlStatusDivCd
    findings: list[Finding] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)      # 요약의 범주별 total
    incomplete_reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # False면 파일 컴포넌트를 끝까지 읽지 못했다. report 모드에서만 생기고 ``complete``는 라이브러리만 보고 정한다
    file_complete: bool = True
    _nested = {"receipt": UploadReceipt, "findings": Finding}


# ---------------------------------------------------------------------------- Maven 의존성 해석
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
    # reactor 루트 폴더 목록. 보통은 비어 있고, 서비스별로 따로 빌드한 이관 프로젝트만 여러 개다
    roots: list[str] = field(default_factory=list)
    _nested = {"modules": ModuleResolution}


@dataclass
class PackagedLib(Record):
    """빌드 결과물 안에 실제로 들어간 jar 하나를 담는 데이터 클래스.
    ``ga``는 pom.properties로 알아낸 경우에만 채운다."""
    artifact: str                     # 빌드 결과물의 경로(예: app/target/app.war)
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


# ---------------------------------------------------------------------------- 인벤토리
# 취약한 라이브러리가 프로젝트의 어디에서 오는지 나타내는 코드
O_DIRECT_LITERAL = "direct_literal"           # pom의 <dependency><version>1.2</version>
O_DIRECT_PROPERTY = "direct_property"         # <version>${x.version}</version>이고 속성이 프로젝트 pom에 있다
O_OWN_MANAGEMENT = "own_management"           # 프로젝트 pom의 <dependencyManagement>가 버전을 정한다
O_PARENT_MANAGED = "parent_managed"           # 외부 부모 pom이 관리한다. 부모 버전을 바꾸거나 덮어써야 한다
O_BOM_MANAGED = "bom_managed"                 # import한 BOM이 관리한다
O_TRANSITIVE = "transitive"                   # 프로젝트에 선언 없이 다른 의존성이 끌고 온다
O_PLUGIN = "plugin"                           # 빌드 플러그인이나 그 의존성이라 자동으로 바꾸지 않는다
O_VENDORED = "vendored"                       # 트리에 들어 있는 파일(jar, js 등)이라 자동으로 바꾸지 않는다
O_UNMAPPED = "unmapped"                       # Maven 해석 결과와 연결하지 못해 자동으로 바꾸지 않는다
MANUAL_ORIGINS = (O_PLUGIN, O_VENDORED, O_UNMAPPED)


@dataclass
class PomLocation(Record):
    """버전 문자열이 기록된 위치를 담는 데이터 클래스.
    ``line``은 1부터 세고, ``text``는 버전 문자열 그대로다.
    버전을 속성으로 정하면 ``property``에 속성 이름을 담고, line과 text는 그 속성의 정의 위치를 가리킨다."""
    pom: str
    line: int
    text: str
    property: str = ""
    element: str = "dependency"       # dependency / management / property / parent / bom


@dataclass
class ModuleSource(Record):
    """모듈 하나에서 취약한 라이브러리가 어떤 경로로 들어왔는지 담는 데이터 클래스.
    보고서가 모듈마다 들어온 경로를 보여 줄 때 쓴다.

    How one module gets the library. `how` is the module's own origin
    code (`O_*`); `via` the g:a:v path from the module's own dependency down to the library's parent (empty for a
    direct dependency); `declared` where a project pom declares the dependency for this module; `manager` the parent /
    BOM that sets the version; `inhouse` the in-house artifacts on this module's path; `variant` is "" for a plain jar,
    else "<type>/<classifier>"."""
    module: str
    how: str = ""
    via: list[str] = field(default_factory=list)
    declared: PomLocation | None = None
    manager: str = ""
    inhouse: list[str] = field(default_factory=list)
    variant: str = ""
    _nested = {"declared": PomLocation}


@dataclass
class InventoryItem(Record):
    """취약한 라이브러리 하나(g:a와 해석된 버전)와 관련 취약점을 담는 데이터 클래스.
    ``origin``에 따라 자동으로 바꿀 수 있는지가 정해진다.

    `location` = the editable version text, `manager` = the parent / BOM that manages the version,
    `reason` = why the item is left for manual review."""
    ga: str
    version: str
    origin: str
    finding_ids: list[str] = field(default_factory=list)
    # IVAS가 알려 준 패치 버전. 힌트일 뿐 근거는 아니다
    # 줄끼리는 모두 만족해야 하고(AND), 한 줄 안의 여러 버전은 버전 라인별 대안이다(OR)
    patched_versions: list[str] = field(default_factory=list)
    modules: list[str] = field(default_factory=list)
    # ``location``이 버전을 정한다는 근거가 없는 모듈. 같은 버전이 다른 경로로 해석된 곳이다
    # 검증에서 실패로 치지 않고, 남아 있으면 다음 라운드에서 따로 계획한다
    also_modules: list[str] = field(default_factory=list)
    # 이 라이브러리를 끌고 온 사내 아티팩트의 g:a 목록. 자신이 사내 아티팩트면 자기 g:a를 담는다
    # ``inhouse_modules``는 그렇게 들어온 모듈 목록이다
    inhouse: list[str] = field(default_factory=list)
    inhouse_modules: list[str] = field(default_factory=list)
    via: list[str] = field(default_factory=list)                  # transitive일 때 끌고 온 경로(g:a:v)
    location: PomLocation | None = None
    manager: str = ""                                             # 버전을 관리하는 부모 / BOM의 g:a:v
    manager_location: PomLocation | None = None
    reason: str = ""
    paths: list[str] = field(default_factory=list)                # vendored 파일 경로
    # ``modules``의 모듈마다 이 라이브러리가 들어온 경로를 하나씩 담는다
    sources: list[ModuleSource] = field(default_factory=list)
    # IVAS 표기가 Maven 표기와 대소문자만 다를 때 IVAS 쪽 표기를 담는다. 같으면 빈 문자열이다
    reported_ga: str = ""
    reported_version: str = ""
    # 이 버전으로 해석된 의존성의 "<type>/<classifier>" 목록. 일반 jar만 있으면 비어 있다
    variants: list[str] = field(default_factory=list)
    _nested = {"location": PomLocation, "manager_location": PomLocation, "sources": ModuleSource}


@dataclass
class Inventory(Record):
    """취약점과 프로젝트 의존성을 연결한 결과 전체를 담는 데이터 클래스. ``complete``가 False면 계획하지 않는다."""
    items: list[InventoryItem] = field(default_factory=list)
    complete: bool = True
    notes: list[str] = field(default_factory=list)
    _nested = {"items": InventoryItem}


# ---------------------------------------------------------------------------- 계획
# 변경 종류
K_DIRECT_VERSION = "direct_version"               # 의존성의 <version> 값
K_PROPERTY_VERSION = "property_version"           # 프로젝트 pom의 속성 값
K_OWN_MANAGEMENT_VERSION = "own_management_version"   # 프로젝트 <dependencyManagement>의 버전 값
K_MANAGED_OVERRIDE = "managed_override"           # 간접, 부모, BOM이 관리하는 버전. pom에 덮어쓰는 항목을 넣는다
K_PARENT_VERSION = "parent_version"               # <parent><version>
K_BOM_VERSION = "bom_version"                     # import한 BOM의 <version>
# 제안의 위험 표시. 보고서가 주의할 변경으로 보여 준다
RISK_MAJOR, RISK_PARENT, RISK_BOM, RISK_MANAGED_OVERRIDE = "major", "parent", "bom", "managed_override"

# 제안의 결정 상태. 계획의 제안은 모두 적용한다
D_AUTO = "auto"
# 승인 단계가 있던 버전이 저장한 계획에서 적용한 제안의 결정 값이다. pending과 rejected는 적용하지 않는다
LEGACY_D_APPROVED = "approved"


E_REPLACE = "replace"     # 기존 버전 문자열을 바꾼다
E_INSERT = "insert"       # 버전을 덮어쓰는 항목을 추가한다. K_MANAGED_OVERRIDE 제안에서만 쓴다


@dataclass
class TextEdit(Record):
    """pom 텍스트 변경 하나를 담는 데이터 클래스.
    - replace(기본값): ``line``번째 줄의 ``old``를 ``new``로 바꾼다. ``old``는 그 줄에 한 번만 있어야 한다
    - insert: ``line``번째 줄 앞에 ``new`` 줄들을 넣는다. 적용할 때 그 줄이 ``anchor``와 같은지 확인한다

    replace: `old` and `new` are both version texts. insert: `old` is ""; the inserted text may only be one
    `<dependency>` built from `override` (groupId / artifactId / version, plus type / classifier when the
    dependency has them), or a `<dependencyManagement>` section holding it."""
    pom: str
    line: int
    old: str
    new: str
    op: str = E_REPLACE
    anchor: str = ""
    # insert일 때 {"groupId", "artifactId", "version"}이고, 필요하면 "type" / "classifier"도 담는다
    override: dict[str, str] = field(default_factory=dict)


@dataclass
class Proposal(Record):
    """변경 제안 하나를 담는 데이터 클래스.
    ``evidence``는 새 버전의 근거이고 계정 정보는 넣지 않는다.

    `diff` = this proposal alone; `source_tree_hash` = the accepted tree it was made from; `policy_hash` = the
    server policy's hash."""
    id: str                           # p-<round>-<n>
    round: int
    ga: str
    from_version: str
    to_version: str
    kind: str
    risks: list[str] = field(default_factory=list)
    decision: str = D_AUTO
    evidence: str = ""
    finding_ids: list[str] = field(default_factory=list)
    edits: list[TextEdit] = field(default_factory=list)
    diff: str = ""
    proposal_hash: str = ""
    source_tree_hash: str = ""
    policy_hash: str = ""
    note: str = ""
    # 이 제안이 버전을 정하는 모듈. 검증은 이 모듈에서 버전이 바뀌었는지 본다. 비어 있으면 모든 모듈을 본다
    # ``also_modules``는 다른 경로로 해석된 모듈이라 그대로 남아도 실패가 아니다. 둘 다 해시에 넣지 않는다
    modules: list[str] = field(default_factory=list)
    also_modules: list[str] = field(default_factory=list)
    # 모듈마다 라이브러리가 들어온 경로. 보고서에 쓰며 ``proposal_hash``에 넣지 않는다
    sources: list["ModuleSource"] = field(default_factory=list)
    _nested = {"edits": TextEdit, "sources": ModuleSource}

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
    # 보고서 표에 쓰는 값. 해석된 모듈, 끌고 온 가장 짧은 경로, 끌고 온 사내 아티팩트를 담는다
    # 사유가 inhouse_artifact가 아니어도 사내 아티팩트를 거친 모듈이 있으면 ``inhouse``를 채운다
    modules: list[str] = field(default_factory=list)
    via: list[str] = field(default_factory=list)
    inhouse: list[str] = field(default_factory=list)
    # 모듈마다 들어온 경로(``InventoryItem.sources``의 복사본)와 IVAS 쪽 버전 표기
    sources: list[ModuleSource] = field(default_factory=list)
    reported_version: str = ""
    _nested = {"sources": ModuleSource}


@dataclass
class RoundPlan(Record):
    """라운드 하나의 계획(제안과 남긴 항목)을 담는 데이터 클래스.
    ``source_tree_hash``와 ``policy_hash``는 모든 제안의 값과 같다."""
    round: int
    source_tree_hash: str
    policy_hash: str
    proposals: list[Proposal] = field(default_factory=list)
    residuals: list[Residual] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    _nested = {"proposals": Proposal, "residuals": Residual}

    def to_apply(self) -> list[Proposal]:
        """적용할 제안을 반환한다. 이전 버전이 저장한 계획에서 승인된 제안도 포함한다."""
        return [p for p in self.proposals if p.decision in (D_AUTO, LEGACY_D_APPROVED)]


@dataclass
class ApplyResult(Record):
    """수정본에 변경을 적용한 결과를 담는 데이터 클래스.
    ``patch``는 ``accepted``와 수정본 사이의 전체 diff다."""
    applied: list[str] = field(default_factory=list)          # 적용한 제안 ID
    skipped: dict[str, str] = field(default_factory=dict)     # 제안 ID별로 건너뛴 이유
    changed_files: list[str] = field(default_factory=list)
    patch: str = ""
    candidate_tree_hash: str = ""


@dataclass
class FixUsage(Record):
    """빌드 오류 수정 루프가 모델 하나에 호출한 LLM 사용량을 담는 데이터 클래스.
    단가를 모르면 ``cost``는 None이다.

    One model's share of the fix loop's LLM calls: `model` = the asked name (`asked_model_name`), `served` = the names
    the answers reported (the asked name left out), `cost` = the price under the run's `llm.pricing` (None when the
    model has no price, left out of the JSON then). Same rules as the migration's `llm_usage_by_model` / `llm_cost`.
    `tokens_unknown` (left out when False): the row's calls have no token counts (an exception escaped the loop and
    its journal did not account for them); such a row never has a cost."""
    model: str = ""
    served: list[str] = field(default_factory=list)
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float | None = None
    # 루프 밖으로 예외가 나와서 토큰 수를 모르는 호출이다. 이때 비용은 None이다
    tokens_unknown: bool = False
    _omit_none = ("cost",)
    _omit_empty = ("served",)
    _omit_false = ("tokens_unknown",)


@dataclass
class FixResult(Record):
    """수정본 빌드가 실패해서 실행한 빌드 오류 수정 루프의 결과를 담는 데이터 클래스.
    ``changed``는 고쳤을 때만 채운다. ``reason``은 영어 문장이고 고쳤으면 빈 문자열이다.

    Result of the build fix loop on a failed candidate (`sbmig/remediations/fixer.py`). `changed`: sorted paths
    relative to the project root (the loop's repository root), empty unless fixed; `patch`: run-relative path of
    `rounds/<r>/fix.patch` or ""; `reason` (English, "" when fixed): `not fixed: <n> error(s) left`,
    `llm unavailable`, `stop requested`, `time budget used up`, `rejected: <guard reason>`,
    `error: <Type>: <msg>`, `error: confirming build failed`, `discarded: the candidate was remade`,
    `discarded: the stored fix is not usable`, `interrupted: the run ended during the loop` (with ` (usage unknown)`
    when the killed loop's usage could not be read); `models`: the
    model names the calls asked with. `usage`: one `FixUsage` per asked model
    (their calls / tokens add up to `llm_calls` / `input_tokens` / `output_tokens`, their names are `models`);
    `currency` = `llm.pricing_currency` when a row has a cost. Both are left out of the JSON when empty,
    and an older FixResult reads back with `usage == []` (`fixer.fix_usage_rows` then makes one row of the
    totals).
    `fixed_dir` (run-relative `rounds/<r>/fixloop/fixed`, the fixed bytes the graph overlays), `tree_hash`
    (the candidate's `intake.tree_hash` after the overlay), `retry_passed` (fixed with no file changed: the loop's own
    first build passed), `discarded_changed` (the files of a fix that a failed confirming build or a remade candidate
    threw away). All four are left out of the JSON when empty / False. `loop_id` = the token the graph wrote
    into `fixloop/loop.json` before the loop started (tells a recorded loop's folder from a killed one; left out when
    empty). `usage_unknown` (left out when False): the loop's LLM usage could not be read (a killed loop whose journal
    is unreadable, an exception out of the fixer with an unreadable journal), so its zero calls / tokens are not exact;
    `fixer.fix_usage` reports it as `calls_unknown`. `ended_round` (left out when False; older JSON reads False): this
    discarded result ended its round as not fixed (a stored fix the loop had just returned that could not be
    applied), so the node's next entry does not run the loop again; set only on that path by
    `graph._discard_fix`.
    Contract: `docs/INTERFACES.md` "v2.1z-bj"."""
    attempted: bool = False
    fixed: bool = False
    iterations: int = 0
    attempts: int = 0
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    models: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    patch: str = ""
    reason: str = ""
    remaining: int = 0
    # 모델별 사용량. 호출이 없으면 비어 있고 JSON에 넣지 않는다
    usage: list[FixUsage] = field(default_factory=list)
    currency: str = ""
    # 고친 파일을 둔 폴더(``rounds/<r>/fixloop/fixed``). 고친 파일이 없으면 빈 문자열이다
    fixed_dir: str = ""
    # 고친 파일을 덮어쓴 뒤의 수정본 트리 해시. 재개할 때 같은 수정본에 덮어썼는지 확인한다
    tree_hash: str = ""
    # 소스를 바꾸지 않았는데 루프의 첫 빌드가 통과했다. 처음 빌드 실패가 일시적이었다는 뜻이다
    retry_passed: bool = False
    # 확인 빌드 실패나 수정본 재생성으로 버린 고친 파일 목록. 결과에는 들어가지 않았다
    discarded_changed: list[str] = field(default_factory=list)
    # 이 결과를 낸 루프의 표식(``fixloop/loop.json``). 기록된 루프와 중간에 끝난 루프의 폴더를 구별한다
    loop_id: str = ""
    # 루프의 LLM 사용량을 읽지 못했다. 이때 호출 수와 토큰 수의 0은 정확한 값이 아니다
    usage_unknown: bool = False
    # 이 결과를 버리면서 라운드를 고치지 못함으로 끝냈다. 다음 진입에서 루프를 다시 실행하지 않는다
    ended_round: bool = False
    _nested = {"usage": FixUsage}
    _omit_empty = ("usage", "currency", "fixed_dir", "tree_hash", "discarded_changed", "loop_id")
    _omit_false = ("retry_passed", "usage_unknown", "ended_round")


# ---------------------------------------------------------------------------- 작업 상태
@dataclass
class RoundState(Record):
    """라운드 하나의 진행 기록을 담는 데이터 클래스."""
    round: int
    plan: str = ""                    # rounds/<n>/plan.json
    applied: list[str] = field(default_factory=list)
    build: BuildOutcome | None = None
    verify_problems: list[str] = field(default_factory=list)
    scan_label: str = ""
    committed: bool = False           # 수정본을 accepted로 확정했는지 여부
    # 조치 전 빌드가 실패한 실행에서 수정본 빌드와 비교한 결과(maven.compare_with_baseline)
    build_compare: dict[str, Any] = field(default_factory=dict)
    # True면 수정본 빌드가 조치 전 빌드에 있던 오류로만 실패해서 라운드를 이어 갔다
    build_tolerated: bool = False
    # 빌드 오류 수정 루프의 결과. 실행하지 않았으면 None이고 JSON에 넣지 않는다
    fix: FixResult | None = None
    # 수정 루프가 고친 라운드의 처음 실패한 빌드. 이때 ``build``는 고친 뒤 다시 한 빌드다
    fix_first_build: BuildOutcome | None = None
    # 수정본을 다시 만들면서 버린 수정 루프 결과. LLM 사용량은 계속 센다
    fix_discarded: list[FixResult] = field(default_factory=list)
    # 라운드의 빌드가 지우거나 바꿔서 원래 바이트로 되돌린 manifest 경로(정렬)
    build_changed: list[str] = field(default_factory=list)
    _nested = {"build": BuildOutcome, "fix": FixResult, "fix_first_build": BuildOutcome, "fix_discarded": FixResult}
    _omit_none = ("fix", "fix_first_build")
    _omit_empty = ("fix_discarded", "build_changed")


@dataclass
class RemediationState(Record):
    """작업 하나의 상태 전체(``state.json``)를 담는 데이터 클래스.
    - ``next_node``: 재개할 때 처음부터 다시 실행할 노드
    - ``pending_upload``, ``pending_receipt``가 있으면 검사 노드는 다시 올리지 않고 결과를 이어서 조회한다
    - ``owner``: 실행 중인 프로세스의 토큰. 서버가 재시작되면 RUNNING, QUEUED 작업은 INTERRUPTED가 된다"""
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
    intake: IntakeResult | None = None
    boundary: list[BoundaryFinding] = field(default_factory=list)
    baseline_build: BuildOutcome | None = None
    baseline_scan: str = ""           # scans/baseline.json
    final_scan: str = ""              # scans/final-r<n>.json
    # 업로드를 보냈지만 결과를 모르는 검사(label별 zip sha256). 사람이 확인해야 한다
    pending_upload: dict[str, str] = field(default_factory=dict)
    # 업로드가 접수되어 분석을 기다리는 검사. 재개하면 다시 올리지 않고 이어서 조회한다
    pending_receipt: dict[str, dict] = field(default_factory=dict)
    rounds: list[RoundState] = field(default_factory=list)
    delivery: DeliveryInfo | None = None
    residuals: list[Residual] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)          # 성공이 아닌 모든 이유를 evaluate가 채운다
    counts: dict[str, int] = field(default_factory=dict)      # baseline / final / fixed / residual …
    # 이관의 최종 빌드 대상 폴더. 비어 있으면 루트에서 한 번 빌드한다
    # 루트 aggregator가 없는 MSA는 서비스마다 빌드한다(``maven.ServerMaven``의 ``targets``)
    build_targets: list[str] = field(default_factory=list)
    # True면 조치 전 빌드가 실패해도 검사와 조치를 이어 간다. 사내 프레임워크 프로젝트의 실행이다
    baseline_tolerated: bool = False
    # 빌드 산출물을 뺐을 때 조치 전 검사에 올린 original/scan-source.zip의 sha256. 처음 만들 때 기록한다
    # 비어 있으면 JSON에 넣지 않으므로 이전 버전이 쓴 state도 그대로 읽는다
    scan_source_sha256: str = ""
    schema: int = SCHEMA
    _nested = {"intake": IntakeResult, "boundary": BoundaryFinding, "baseline_build": BuildOutcome,
               "rounds": RoundState, "delivery": DeliveryInfo, "residuals": Residual}
    _omit_empty = ("scan_source_sha256",)


class RunPaths:
    """실행 폴더 안의 파일 경로를 정하는 클래스. 모든 파일 경로는 이 클래스로 얻는다."""

    def __init__(self, root: Path | str):
        self.root = Path(root)

    @property
    def original_zip(self) -> Path: return self.root / "original" / "source.zip"
    @property
    def manifest(self) -> Path: return self.root / "original" / "manifest.json"
    # 빌드 산출물을 뺐을 때만 만드는 검사용 ZIP. accepted/를 manifest 경로대로 묶는다
    @property
    def scan_source(self) -> Path: return self.root / "original" / "scan-source.zip"
    @property
    def accepted(self) -> Path: return self.root / "accepted"
    @property
    def state(self) -> Path: return self.root / "state.json"
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
    """JSON을 임시 파일에 쓰고 원래 파일과 바꿔치기한다. 반쯤 쓰인 파일이 남지 않는다."""
    import os
    import tempfile

    from . import fsops
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        fsops.replace(tmp, path)
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
