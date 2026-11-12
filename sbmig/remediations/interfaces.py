"""취약점 조치에서 바꿔 넣을 수 있는 구성 요소의 Protocol과, 그것을 모은 ``Components``를 정의하는 모듈.
그래프(graph.py)는 실제 구현을 직접 import하지 않고 ``Components``로 받은 구성 요소만 호출한다.
테스트는 IVAS / Nexus / Maven을 호출하지 않는 가짜 구현을 넣는다.
실제 구현은 ``ivas.IvasHttpClient`` / ``ivas.run_scan``, ``maven.ServerMaven``,
``planner.RepositoryVersionSource``와 ``intake`` / ``inventory`` / ``planner`` / ``apply``의 함수들이다.

Seams of the remediation feature. `graph.py` only talks to a `Components` bundle; tests inject fakes (no IVAS, Nexus
or Maven call). `default_components(cfg)` in `graph.py` wires the real implementations.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .models import (ApplyResult, BoundaryFinding, BuildOutcome, DeliveryInfo, Finding, Inventory, PackagedLib,
                     Proposal, ResolvedModel, RoundPlan, ScanResult, UploadReceipt)

Journal = Callable[[dict[str, Any]], None]      # journal.jsonl에 event가 포함된 한 줄을 기록하는 함수
StopCheck = Callable[[], bool]                  # 중지 요청이 들어왔으면 True를 반환하는 함수


@dataclass
class Page:
    """IVAS 결과 한 페이지를 담는 데이터 클래스.
    ``content`` 항목들과 전체 개수 ``total``(totalElements)을 담는다."""
    content: list[dict[str, Any]]
    total: int


@runtime_checkable
class IvasClient(Protocol):
    """IVAS HTTP API를 호출하는 클라이언트의 인터페이스.
    로그인과 토큰은 구현 안에서 처리하며 토큰은 로그와 오류 메시지에 넣지 않는다.
    실패하면 ``IvasError``를 던지고, 업로드 결과를 모를 때만 ``IvasUploadUncertain``을 던진다."""

    def upload(self, zip_path: Path, project_name: str) -> UploadReceipt:
        """ZIP을 올리고 접수되면 접수 정보를 반환한다.
        보낸 뒤 결과를 모르면 ``IvasUploadUncertain``을 던지며 재시도하지 않는다."""
        ...

    def status(self, project_id: str, analysis_id: str) -> str:
        """분석 상태 코드 ``anlStatusDivCd``를 그대로 반환한다.
        응답의 ID가 요청과 다르면 ``IvasError(code="id_mismatch")``를 던진다."""
        ...

    def summary(self, project_id: str, analysis_id: str) -> dict[str, int]:
        """검사 요약에서 범주별 total을 반환한다. 범주는 components / swComponents / fileComponents /
        codeSnippets / file / functionComponents / ospkgComponents / binaryComponents다."""
        ...

    def page(self, project_id: str, analysis_id: str, kind: str, page: int, size: int,
             component_id: str | None = None) -> Page:
        """검사 결과 한 페이지를 반환한다. ``kind``는 library / sw / file_main / file_potential /
        matched_files이고, matched_files는 ``component_id``가 필요하다."""
        ...


class Scanner(Protocol):
    """IVAS 검사 절차 전체(업로드 -> 상태 조회 -> 요약 -> 모든 페이지 -> 결과가 빠짐없는지 확인)를 실행하는
    인터페이스. 실제 구현은 ``ivas.run_scan``이다.
    ``receipt``를 주면 다시 올리지 않고 그 분석을 이어서 조회한다.
    ``on_receipt``는 상태를 저장할 수 있도록 업로드가 접수되는 즉시 호출된다.
    밖으로 던지는 예외는 ``IvasUploadUncertain``뿐이고, 다른 실패는 ``complete=False`` 결과로 반환한다."""

    def __call__(self, client: IvasClient, zip_path: Path, zip_sha256: str, label: str, project_name: str, *,
                 receipt: UploadReceipt | None = None, on_receipt: Callable[[UploadReceipt], None] | None = None,
                 stop: StopCheck | None = None, journal: Journal | None = None) -> ScanResult: ...


@runtime_checkable
class MavenGateway(Protocol):
    """서버가 관리하는 Maven으로 빌드하는 인터페이스.
    JDK 21, 서버 settings.xml, 승인된 저장소만 쓰고 테스트는 실행하지 않는다. 프로젝트의 mvnw / .mvn 설정은 쓰지 않는다."""

    def readiness(self) -> list[str]:
        """빌드를 막는 문제 목록을 반환한다. 빈 목록이면 빌드할 수 있다.
        JDK 21, Maven, settings.xml 누락 같은 문제를 비밀 값 없이 담는다."""
        ...

    def allowed_hosts(self) -> list[str]:
        """승인된 저장소 호스트 목록을 반환한다. 설정과 서버 settings.xml의 미러, 저장소에서 가져온다."""
        ...

    def package(self, project: Path, log_path: Path, stop: StopCheck | None = None,
                targets: list[str] | None = None) -> BuildOutcome:
        """``mvn clean package -Dmaven.test.skip=true``로 프로젝트를 빌드한다.
        실패해도 예외를 던지지 않는다.
        ``targets``는 이관의 최종 빌드 대상 폴더이고, 없으면 루트에서 한 번 빌드한다."""
        ...

    def resolve(self, project: Path, work: Path, targets: list[str] | None = None) -> ResolvedModel:
        """모듈마다 Maven이 실제로 정한 의존성(dependency:tree)과 플러그인을 반환한다.
        ``work``는 중간 파일을 둘 폴더다.
        ``targets``는 ``package``와 같으며, 대상이 있으면 결과의 ``roots``에 기록한다."""
        ...

    def packaged(self, project: Path, targets: list[str] | None = None) -> list[PackagedLib]:
        """방금 빌드한 산출물(war / 실행 jar) 안에 들어 있는 jar 목록을 반환한다.
        ``targets``는 ``package``와 같다."""
        ...


@runtime_checkable
class VersionSource(Protocol):
    """버전 후보의 근거가 되는 승인된 내부 저장소를 조회하는 인터페이스.
    실제 구현은 ``sbmig.build.repo_versions.RepositoryLookup``을 감싼다."""

    def versions(self, group_id: str, artifact_id: str) -> list[str]:
        """저장소에 있는 릴리스 버전 목록을 오름차순으로 반환한다. 알 수 없으면 빈 목록을 반환한다."""
        ...

    def evidence(self, group_id: str, artifact_id: str, version: str) -> str:
        """그 버전의 근거 문자열 "repository:<호스트>"를 반환한다.
        자격 증명과 전체 URL은 넣지 않으며, 근거가 없으면 빈 문자열을 반환한다."""
        ...


class IntakeFns(Protocol):
    """그래프가 쓰는 ``intake.py`` 함수 묶음의 인터페이스.
    ``tree``는 ZIP 최상위 형태의 폴더(accepted 또는 수정본 트리), ``paths``는 원본 manifest의 경로 목록이다.
    빌드가 만든 ``target/`` 같은 파일은 해시, 복사, 결과 ZIP에 들어가지 않는다.
    기존 pom 내용만 바꾸므로 결과 ZIP의 파일 목록은 원본과 같다."""

    def manifest_paths(self, manifest: Path) -> list[str]: ...
    def tree_hash(self, tree: Path, paths: list[str]) -> str: ...
    def check_boundary(self, project: Path, cfg: Any, allowed_hosts: list[str]) -> list[BoundaryFinding]: ...
    def copy_tree(self, src: Path, dst: Path, paths: list[str]) -> None: ...
    def freeze_delivery(self, tree: Path, out_zip: Path, round: int, paths: list[str]) -> DeliveryInfo: ...


class InventoryFn(Protocol):
    """``inventory.build_inventory``의 인터페이스.
    IVAS가 찾은 취약점을 Maven 의존성과 pom 선언 위치에 연결한다.
    ``inhouse``는 사내 아티팩트 목록(``inhouse.inhouse_keys``)이고 그래프가 항상 키워드 인자로 넘긴다.
    없으면 ``{}``이다."""

    def __call__(self, project: Path, findings: list[Finding], resolved: ResolvedModel, *,
                 inhouse: Mapping[str, str] | None = None) -> Inventory: ...


class PlanFn(Protocol):
    """``planner.plan_round``의 인터페이스. 인벤토리에서 변경 제안과 조치하지 못한 항목을 만든다.
    ``decided``는 이전 라운드에서 거절된 제안의 (ga, to_version)이며, 같은 제안을 다시 내지 않는 데 쓴다."""

    def __call__(self, project: Path, inventory: Inventory, cfg: Any, source: VersionSource, *, round: int,
                 source_tree_hash: str, rejected: set[tuple[str, str]] | None = None) -> RoundPlan: ...


class ApplyFns(Protocol):
    """``apply.py``의 함수 묶음을 나타내는 인터페이스."""

    def apply_proposals(self, project: Path, proposals: list[Proposal]) -> ApplyResult: ...
    def verify_text_only(self, original: Path, candidate: Path, proposals: list[Proposal], paths: list[str]) -> list[str]: ...
    def verify_resolution(self, proposals: list[Proposal], before: ResolvedModel, after: ResolvedModel,
                          packaged: list[PackagedLib]) -> list[str]: ...


@dataclass
class Components:
    """그래프가 쓰는 구성 요소를 모두 담는 데이터 클래스.
    테스트는 가짜 구현을, 운영에서는 ``graph.default_components(cfg)``가 실제 구현을 넣는다."""
    ivas: IvasClient
    scan: Scanner
    maven: MavenGateway
    versions: VersionSource
    intake: IntakeFns
    inventory: InventoryFn
    plan: PlanFn
    apply: ApplyFns
