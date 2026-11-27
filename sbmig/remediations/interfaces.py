"""취약점 조치 그래프가 쓰는 구성 요소의 인터페이스(Protocol)와 그 묶음 ``Components``를 정의하는 모듈.

The graph (graph.py) never imports the real implementations: it calls only what it gets in `Components`.
Tests pass fakes that call no IVAS, Nexus or Maven.

Real implementations: `ivas.IvasHttpClient` / `ivas.run_scan`, `maven.ServerMaven`,
`planner.RepositoryVersionSource` and the functions of `intake` / `inventory` / `planner` / `apply`, wired by
`graph.default_components(cfg)`.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .models import (
    ApplyResult,
    BoundaryFinding,
    BuildOutcome,
    DeliveryInfo,
    Finding,
    FixResult,
    Inventory,
    PackagedLib,
    Proposal,
    ResolvedModel,
    RoundPlan,
    ScanResult,
    UploadReceipt,
)

Journal = Callable[[dict[str, Any]], None]      # journal.jsonl에 event 한 줄을 기록하는 함수
StopCheck = Callable[[], bool]                  # 중지 요청이 들어왔으면 True를 반환하는 함수


@dataclass
class Page:
    """IVAS 결과 한 페이지의 항목(``content``)과 전체 개수(``total``)를 담는 데이터 클래스."""
    content: list[dict[str, Any]]
    total: int


@runtime_checkable
class IvasClient(Protocol):
    """IVAS HTTP API 클라이언트의 인터페이스. 로그인과 토큰은 구현이 알아서 관리한다.
    실패하면 ``IvasError``를 던진다. 업로드 결과를 모를 때만 ``IvasUploadUncertain``을 던진다."""

    def upload(self, zip_path: Path, project_name: str) -> UploadReceipt:
        """ZIP을 올리고 접수 정보를 반환한다. 보낸 뒤 결과를 모르면 ``IvasUploadUncertain``을 던진다."""
        ...

    def status(self, project_id: str, analysis_id: str) -> str:
        """분석 상태 코드 ``anlStatusDivCd``를 그대로 반환한다.
        응답의 ID가 요청과 다르면 ``IvasError(code="id_mismatch")``를 던진다."""
        ...

    def summary(self, project_id: str, analysis_id: str) -> dict[str, int]:
        """검사 요약에서 범주별 total을 반환한다. 범주는 ``ivas.SUMMARY_CATEGORIES``의 8개다."""
        ...

    def page(self, project_id: str, analysis_id: str, kind: str, page: int, size: int,
             component_id: str | None = None) -> Page:
        """검사 결과 한 페이지를 반환한다. matched_files는 ``component_id``가 필요하다.

        `kind`: library / sw / file_main / file_potential / matched_files."""
        ...


class Scanner(Protocol):
    """IVAS 검사 절차 전체를 실행하는 인터페이스. 실제 구현은 ``ivas.run_scan``이다.
    ``receipt``를 주면 다시 올리지 않고 그 분석을 이어서 조회한다.
    ``on_receipt``는 업로드가 접수되는 즉시 호출된다. 호출한 쪽은 여기서 상태를 저장할 수 있다.
    ``IvasUploadUncertain``만 던진다. 다른 실패는 ``complete=False`` 결과로 반환한다."""

    def __call__(self, client: IvasClient, zip_path: Path, zip_sha256: str, label: str, project_name: str, *,
                 receipt: UploadReceipt | None = None, on_receipt: Callable[[UploadReceipt], None] | None = None,
                 stop: StopCheck | None = None, journal: Journal | None = None) -> ScanResult: ...


@runtime_checkable
class MavenGateway(Protocol):
    """서버의 Maven으로 빌드하는 인터페이스.
    JDK 21, 서버 settings.xml, 승인된 저장소만 쓰고 테스트는 실행하지 않는다. 프로젝트의 mvnw와 .mvn은 쓰지 않는다."""

    def readiness(self) -> list[str]:
        """빌드를 막는 문제 목록을 반환한다. 빈 목록이면 빌드할 수 있다. 비밀 값은 넣지 않는다."""
        ...

    def allowed_hosts(self) -> list[str]:
        """승인된 저장소 호스트 목록을 반환한다. 설정과 서버 settings.xml의 미러, 저장소에서 가져온다."""
        ...

    def package(self, project: Path, log_path: Path, stop: StopCheck | None = None,
                targets: list[str] | None = None) -> BuildOutcome:
        """``mvn clean package -Dmaven.test.skip=true``로 프로젝트를 빌드한다. 실패해도 예외를 던지지 않는다.
        ``targets``는 이관의 최종 빌드 대상 폴더다. 없으면 루트에서 한 번 빌드한다."""
        ...

    def resolve(self, project: Path, work: Path, targets: list[str] | None = None) -> ResolvedModel:
        """모듈마다 Maven이 실제로 고른 의존성과 플러그인을 반환한다. ``work``는 중간 파일을 둘 폴더다.
        ``targets``는 ``package``와 같다. 대상이 있으면 결과의 ``roots``에 기록한다."""
        ...

    def packaged(self, project: Path, targets: list[str] | None = None) -> list[PackagedLib]:
        """방금 빌드한 결과물(war, 실행 jar) 안의 jar 목록을 반환한다. ``targets``는 ``package``와 같다."""
        ...


@runtime_checkable
class VersionSource(Protocol):
    """새 버전의 근거가 되는 승인된 내부 저장소를 조회하는 인터페이스.
    실제 구현은 ``sbmig.build.repo_versions.RepositoryLookup``을 감싼다."""

    def versions(self, group_id: str, artifact_id: str) -> list[str]:
        """저장소에 있는 릴리스 버전 목록을 오름차순으로 반환한다. 알 수 없으면 빈 목록을 반환한다."""
        ...

    def evidence(self, group_id: str, artifact_id: str, version: str) -> str:
        """그 버전의 근거 문자열 "repository:<호스트>"를 반환한다. 근거가 없으면 빈 문자열을 반환한다.
        계정 정보와 전체 URL은 넣지 않는다."""
        ...


class IntakeFns(Protocol):
    """그래프가 쓰는 ``intake.py`` 함수 묶음의 인터페이스.
    ``tree``는 ``accepted/``나 수정본 폴더다. ``paths``는 원본 manifest의 경로 목록이다.
    빌드가 만든 ``target/`` 같은 파일은 해시, 복사, 결과 ZIP에 들어가지 않는다."""

    def manifest_paths(self, manifest: Path) -> list[str]: ...
    def tree_hash(self, tree: Path, paths: list[str]) -> str: ...
    def check_boundary(self, project: Path, cfg: Any, allowed_hosts: list[str]) -> list[BoundaryFinding]: ...
    def copy_tree(self, src: Path, dst: Path, paths: list[str]) -> None: ...
    def freeze_delivery(self, tree: Path, out_zip: Path, round: int, paths: list[str]) -> DeliveryInfo: ...
    # 수정본을 빌드할 때마다 호출해서 빌드가 바꾼 파일을 기록된 표대로 되돌린다.
    # (없던 경로, 바뀐 경로)를 반환한다. 되돌리지 못한 경로가 있으면 ``intake.SyncFailed``를 던진다
    def sync_tree_table(self, tree: Path, expected: Callable[[str], Path], table: dict[str, tuple],
                        paths: list[str]) -> tuple[list[str], list[str]]: ...


class InventoryFn(Protocol):
    """``inventory.build_inventory``의 인터페이스. IVAS가 찾은 취약점을 Maven 의존성과 pom 선언 위치에 연결한다.
    ``inhouse``는 사내 아티팩트 목록(``inhouse.inhouse_keys``)이다. 그래프는 항상 키워드 인자로 넘긴다."""

    def __call__(self, project: Path, findings: list[Finding], resolved: ResolvedModel, *,
                 inhouse: Mapping[str, str] | None = None) -> Inventory: ...


class PlanFn(Protocol):
    """``planner.plan_round``의 인터페이스. 인벤토리에서 변경 제안과 조치하지 못한 항목을 만든다.
    ``rejected``에 있는 덮어쓰기 항목은 같은 pom에 다시 제안하지 않는다.

    `rejected`: (ga, to_version, variant, pom) of override entries dropped in this run because they had no
    effect."""

    def __call__(self, project: Path, inventory: Inventory, cfg: Any, source: VersionSource, *, round: int,
                 source_tree_hash: str, rejected: set[tuple[str, str]] | None = None) -> RoundPlan: ...


class ApplyFns(Protocol):
    """``apply.py``의 함수 묶음을 나타내는 인터페이스."""

    def apply_proposals(self, project: Path, proposals: list[Proposal]) -> ApplyResult: ...
    def verify_text_only(self, original: Path, candidate: Path, proposals: list[Proposal], paths: list[str]) -> list[str]: ...
    def verify_resolution(self, proposals: list[Proposal], before: ResolvedModel, after: ResolvedModel,
                          packaged: list[PackagedLib]) -> list[str]: ...


class Fixer(Protocol):
    """수정본 빌드가 실패했을 때 빌드 오류 수정 루프로 소스를 고치는 인터페이스.
    실제 구현은 ``fixer.RemediationFixer``다.
    ``tree``는 읽기만 하고 고친 파일은 ``work_dir/fixed/``에 둔다. 덮어쓰기는 그래프가 한다.
    ``deadline``이 None이면 시간 한도가 없다. 예외를 던지지 않는다.

    The build fix loop on a failed candidate (v2.1z-bj). `tree` = the project root inside the candidate,
    only read (the loop runs in `work_dir/tree/`); paths are relative to it; `applied` = the round's applied
    `Proposal`s; `first_outcome` = the failed candidate build; `deadline` = a `time.monotonic()` instant or None.
    Fixed bytes go to `work_dir/fixed/<path>` (`FixResult.fixed_dir`, absolute; the graph makes it run-relative and
    overlays them). Never raises for an operational failure."""

    def run(self, cfg: Any, tree: Path, work_dir: Path, *, run_id: str, targets: list[str], applied: list[Proposal],
            manifest_paths: list[str], first_outcome: BuildOutcome, stop: Callable[[], bool], deadline: float | None,
            journal: Journal | None) -> FixResult: ...


@dataclass
class Components:
    """그래프가 쓰는 구성 요소를 모두 담는 데이터 클래스.
    테스트는 가짜 구현을 넣는다. 운영에서는 ``graph.default_components(cfg)``가 실제 구현을 넣는다.
    ``fixer``가 None이면 수정본 빌드가 실패해도 빌드 오류 수정 루프를 실행하지 않는다."""
    ivas: IvasClient
    scan: Scanner
    maven: MavenGateway
    versions: VersionSource
    intake: IntakeFns
    inventory: InventoryFn
    plan: PlanFn
    apply: ApplyFns
    fixer: Fixer | None = None
