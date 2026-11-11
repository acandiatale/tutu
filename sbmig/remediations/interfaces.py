"""취약점 조치의 갈아 끼우는 자리(Protocol)와 부품 묶음 ``Components``.
그래프(graph.py)는 진짜 구현을 직접 import 하지 않고 ``Components`` 로 받은 부품만 부른다 — 테스트는 IVAS / Nexus / Maven 에
나가지 않는 가짜 부품을 넣는다. 각 Protocol 의 진짜 구현: ``ivas.IvasHttpClient`` / ``ivas.run_scan``,
``maven.ServerMaven``, ``planner.RepositoryVersionSource``, ``intake`` / ``inventory`` / ``planner`` / ``apply`` 의 함수들.

Seams of the remediation feature. `graph.py` only talks to a `Components` bundle; tests inject fakes (no IVAS, Nexus
or Maven call). `default_components(cfg)` in `graph.py` wires the real implementations.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .models import (ApplyResult, BoundaryFinding, BuildOutcome, DeliveryInfo, Finding, Inventory, PackagedLib,
                     Proposal, ResolvedModel, RoundPlan, ScanResult, UploadReceipt)

Journal = Callable[[dict[str, Any]], None]      # journal.jsonl 에 한 줄 (event=… 포함)
StopCheck = Callable[[], bool]                  # True = 중지 요청이 들어왔다


@dataclass
class Page:
    """IVAS 결과 한 쪽: ``content`` 항목들과 전체 개수 ``total`` (totalElements)."""
    content: list[dict[str, Any]]
    total: int


@runtime_checkable
class IvasClient(Protocol):
    """IVAS HTTP 계약 한 겹. 로그인·토큰은 구현 안에서 처리한다(토큰은 로그·오류 문장에 넣지 않는다).
    실패는 ``IvasError``, 업로드 결과를 모를 때만 ``IvasUploadUncertain``."""

    def upload(self, zip_path: Path, project_name: str) -> UploadReceipt:
        """ZIP 을 올린다. 받아들여졌으면 영수증. 보낸 뒤 결과를 모르면 ``IvasUploadUncertain`` — 구현은 **재시도하지 않는다**."""
        ...

    def status(self, project_id: str, analysis_id: str) -> str:
        """분석 상태 코드 ``anlStatusDivCd`` 그대로. 응답의 ID 가 요청과 다르면 ``IvasError(code="id_mismatch")``."""
        ...

    def summary(self, project_id: str, analysis_id: str) -> dict[str, int]:
        """요약의 범주별 total: components / swComponents / fileComponents / codeSnippets / file / functionComponents /
        ospkgComponents / binaryComponents."""
        ...

    def page(self, project_id: str, analysis_id: str, kind: str, page: int, size: int,
             component_id: str | None = None) -> Page:
        """결과 한 쪽. ``kind``: library / sw / file_main / file_potential / matched_files (마지막은 ``component_id`` 필요)."""
        ...


class Scanner(Protocol):
    """검사 절차: 업로드 → 상태 조회 → 요약 → 모든 쪽 → 완전성 판정. 진짜 구현은 ``ivas.run_scan``.
    ``receipt`` 를 주면 **올리지 않고** 그 분석을 이어서 조회한다. ``on_receipt`` 는 업로드가 받아들여진 즉시 불린다(상태 저장용).
    ``IvasUploadUncertain`` 만 밖으로 나간다 — 다른 실패는 ``complete=False`` 인 결과로 돌아온다."""

    def __call__(self, client: IvasClient, zip_path: Path, zip_sha256: str, label: str, project_name: str, *,
                 receipt: UploadReceipt | None = None, on_receipt: Callable[[UploadReceipt], None] | None = None,
                 stop: StopCheck | None = None, journal: Journal | None = None) -> ScanResult: ...


@runtime_checkable
class MavenGateway(Protocol):
    """서버가 관리하는 Maven 실행(JDK 21, 서버 settings.xml, 승인된 저장소만, 테스트 없음). 프로젝트의 mvnw / .mvn 설정은 쓰지 않는다."""

    def readiness(self) -> list[str]:
        """빌드를 못 하게 하는 문제들(빈 목록 = 준비됨): JDK 21 없음, Maven 없음, settings.xml 없음 등. 비밀 값 없이."""
        ...

    def allowed_hosts(self) -> list[str]:
        """승인된 저장소 호스트(설정 + 서버 settings.xml 의 미러·저장소)."""
        ...

    def package(self, project: Path, log_path: Path, stop: StopCheck | None = None,
                targets: list[str] | None = None) -> BuildOutcome:
        """``mvn clean package -Dmaven.test.skip=true``. 실패해도 예외를 내지 않는다. ``targets`` = 이관의 최종 빌드 대상 폴더들
        (없으면 루트 한 번; 그래프는 비어 있지 않을 때만 넘긴다)."""
        ...

    def resolve(self, project: Path, work: Path, targets: list[str] | None = None) -> ResolvedModel:
        """모듈마다 실제 해석 결과(dependency:tree)와 플러그인. ``work`` 는 중간 파일을 둘 폴더. ``targets`` 는 ``package`` 와 같다
        (대상이 있으면 결과의 ``roots`` 에 기록)."""
        ...

    def packaged(self, project: Path, targets: list[str] | None = None) -> list[PackagedLib]:
        """방금 빌드한 산출물(war / 실행 jar) 안에 든 jar 목록. ``targets`` 는 ``package`` 와 같다."""
        ...


@runtime_checkable
class VersionSource(Protocol):
    """버전 후보의 근거가 되는 승인된 내부 저장소. 진짜 구현은 ``sbmig.build.repo_versions.RepositoryLookup`` 를 감싼다."""

    def versions(self, group_id: str, artifact_id: str) -> list[str]:
        """저장소가 가진 릴리스 버전들(오름차순). 모르면 빈 목록."""
        ...

    def evidence(self, group_id: str, artifact_id: str, version: str) -> str:
        """그 버전의 근거 문장 "repository:<호스트>" (자격증명·전체 URL 없이). 근거가 없으면 빈 글자열."""
        ...


class IntakeFns(Protocol):
    """``intake.py`` 의 함수 묶음(그래프가 쓰는 것만). ``tree`` 는 ZIP 뿌리 모양의 폴더(accepted 또는 후보 사본), ``paths`` 는
    원본 manifest 의 경로 목록 — 빌드가 만든 ``target/`` 같은 파일은 해시·복사·전달 ZIP 에 **들어가지 않는다**(원본에 있던
    파일만). 변경은 기존 pom 의 글자뿐이므로 전달 ZIP 의 파일 집합은 원본과 같다."""

    def manifest_paths(self, manifest: Path) -> list[str]: ...
    def tree_hash(self, tree: Path, paths: list[str]) -> str: ...
    def check_boundary(self, project: Path, cfg: Any, allowed_hosts: list[str]) -> list[BoundaryFinding]: ...
    def copy_tree(self, src: Path, dst: Path, paths: list[str]) -> None: ...
    def freeze_delivery(self, tree: Path, out_zip: Path, round: int, paths: list[str]) -> DeliveryInfo: ...


class InventoryFn(Protocol):
    """``inventory.build_inventory``: 발견 항목을 해석 결과와 pom 선언 자리에 연결한다."""

    def __call__(self, project: Path, findings: list[Finding], resolved: ResolvedModel) -> Inventory: ...


class PlanFn(Protocol):
    """``planner.plan_round``: 인벤토리에서 제안과 잔여 항목을 만든다. ``decided`` 는 이전 라운드들에서 거절된 제안의
    (ga, to_version) — 같은 제안을 다시 내지 않는다."""

    def __call__(self, project: Path, inventory: Inventory, cfg: Any, source: VersionSource, *, round: int,
                 source_tree_hash: str, rejected: set[tuple[str, str]] | None = None) -> RoundPlan: ...


class ApplyFns(Protocol):
    """``apply.py`` 의 함수 묶음."""

    def apply_proposals(self, project: Path, proposals: list[Proposal]) -> ApplyResult: ...
    def verify_text_only(self, original: Path, candidate: Path, proposals: list[Proposal], paths: list[str]) -> list[str]: ...
    def verify_resolution(self, proposals: list[Proposal], before: ResolvedModel, after: ResolvedModel,
                          packaged: list[PackagedLib]) -> list[str]: ...


@dataclass
class Components:
    """그래프가 쓰는 부품 전부. 테스트는 가짜를, 운영은 ``graph.default_components(cfg)`` 가 진짜를 넣는다."""
    ivas: IvasClient
    scan: Scanner
    maven: MavenGateway
    versions: VersionSource
    intake: IntakeFns
    inventory: InventoryFn
    plan: PlanFn
    apply: ApplyFns
