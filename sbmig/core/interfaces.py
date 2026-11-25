"""외부 구현을 끼워 넣는 연결 지점(Protocol)을 모아 둔 모듈.
오케스트레이터(sbmig.loop)는 구현 대신 이 Protocol만 사용한다.
회사 LLM 게이트웨이는 LLMRequest / LLMResponse(1단계, `llm.plugin`)로, 사내 코딩 에이전트는
FixerLike.propose_script(2단계, `fix.plugin`)로 연결한다. 새 HTTP 클라이언트는 만들지 않는다.
- LLMRequest / LLMResponse, FixerLike.propose_script, sbmig.api.migrate의 형태는 바꾸지 않는다
- 스크립트 저장소 구현은 sbmig.errors.cases.CaseStore다

Implementations (module -> class):
    sbmig.analyze.analyzer     analyze_project(ws, cfg) -> ProjectProfile
    sbmig.plan.planner         build_plan(profile, cfg, kb=None) -> MigrationPlan
    sbmig.restructure.pom_editor.PomEditor          (PomEditorLike)
    sbmig.restructure.restructurer.Restructurer     (RestructurerLike)
    sbmig.rewrite.runner.OpenRewriteRunner          (RewriteRunnerLike)
    sbmig.build.runner.MavenRunner                  (BuildRunnerLike)
    sbmig.triage.triager.Triager                    (TriagerLike)
    sbmig.fix.client.*                              (LLMClientLike)
    sbmig.fix.fixer.LLMFixer                        (FixerLike = ScriptFixerLike)
    sbmig.kb.store.FileKnowledgeBase                (KnowledgeBaseLike = ScriptKnowledgeLike)
    sbmig.loop.orchestrator.Orchestrator            drives everything
    sbmig.script.ScriptRunner                       (ScriptRunnerLike)      fix scripts: static check + `python -I` run
    sbmig.errors.cases.CaseStore                    (ScriptStoreLike)       <kb>/cases/<case_id>/
    sbmig.review.StructureReviewer                  (StructureReviewerLike) LLM structure review before the first build
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Literal, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from .config import Config
from .models import (
    BuildError,
    DependencyChange,
    CaseType,
    BuildResult,
    ErrorCluster,
    ErrorEntry,
    CaseScript,
    CaseInfo,
    FixAttempt,
    GAV,
    KBMatch,
    MergeConflict,
    Phase,
    PluginInfo,
    ProjectProfile,
    RecipeRef,
    RewriteResult,
    ScriptBase,
    ScriptMeta,
    ScriptProposal,
    ScriptRunResult,
    StructureReview,
    TriageDecision,
)
from .workspace import Workspace


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------
class ChatMessage(BaseModel):
    """LLM 대화 메시지 하나(system / user / assistant)를 담는 데이터 클래스.
    회사 게이트웨이에는 messages만 전달한다."""
    role: Literal["system", "user", "assistant"]
    content: str


class LLMResponse(BaseModel):
    """LLM 응답 하나를 담는 데이터 클래스(1단계 연결 규약). 본문, 모델, 토큰 수, 종료 이유를 담는다.
    - model: 요청할 때 쓴 이름(설정한 모델, 에이전트 게이트웨이에서는 role). 기본 클라이언트와 어댑터 래퍼가 이렇게 채운다
    - served_models: 응답이 알려 준 실제 모델 이름. `unknown` 같은 값과 요청한 이름은 뺀다

    `served_models` sources: a top-level `model` and every `usage.by_model[].model` of the answer."""
    content: str
    model: str
    served_models: list[str] = Field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    finish_reason: Optional[str] = None
    # 세션 id를 보내지 않으면 에이전트 게이트웨이가 세션을 만들고 그 id를 돌려준다. 다시 보내면 그 세션을 이어 쓴다.
    # 세션이 없는 게이트웨이면 None이다
    session_id: Optional[str] = None


# 사내 게이트웨이가 목적별로 라우팅과 예산을 나눌 수 있도록 호출 목적을 요청에 넣는다.
# 수정 스크립트는 fix / fix_escalation, 구조 검토는 structure_review, 계획 보조는 plan_advisor를 쓴다
LLMRole = Literal["fix", "fix_escalation", "plan_advisor", "structure_review", "verify_guide", "other"]


class LLMRequest(BaseModel):
    """LLM 호출 한 번의 요청을 담는 데이터 클래스(1단계 연결 규약).
    메시지, 모델, 토큰 한도, 목적(role), 메타데이터를 담는다.
    메타데이터에는 비밀 값을 넣지 않는다."""
    messages: list[ChatMessage]
    # cfg.llm.model, recipe_model, escalation_model 중 하나. None이면 어댑터 기본값
    model: Optional[str] = None
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None           # None이면 어댑터 기본값. Claude 5 모델은 샘플링 값을 거부한다
    stop: Optional[list[str]] = None
    role: LLMRole = "other"
    metadata: dict = Field(default_factory=dict)  # run_id, project, phase, cluster_id, attempt 등. 비밀 값 금지


@runtime_checkable
class LLMClientLike(Protocol):
    """채팅 응답을 받아 오는 저수준 LLM 클라이언트의 Protocol. 구현은 `role` / `metadata`와 나중에 추가될
    키워드 인자를 받을 수 있어야 하며, 받은 뒤 무시해도 된다.

    Built-in clients: openai_compatible / anthropic / fake (sbmig.fix.client)."""
    def complete(
        self,
        messages: list[ChatMessage],
        *,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        stop: Optional[list[str]] = None,
        role: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> LLMResponse: ...


@runtime_checkable
class LLMAdapterLike(Protocol):
    """사내 게이트웨이나 에이전트 플랫폼을 연결하는 1단계 어댑터의 Protocol. LLMRequest 하나를 받아
    LLMResponse 하나를 반환한다.
    `llm.provider: plugin`과 `llm.plugin`이나 entry point로 연결하면 sbmig가 LLMClientLike로 감싸고 호출 기록도 남긴다.

    `llm.plugin: "pkg.module:attr"`, or an entry point in group `sbmig.llm_adapters`; wrapper
    `sbmig.llm.AdapterClient`."""
    def complete(self, request: LLMRequest) -> LLMResponse: ...


# ---------------------------------------------------------------------------
# pom 편집. 구현은 sbmig.restructure.pom_editor
# ---------------------------------------------------------------------------
@runtime_checkable
class PomEditorLike(Protocol):
    """형식과 주석을 그대로 두고 pom.xml을 고치는 편집기의 Protocol. 재구성과 스크립트 헬퍼 API가 사용한다.
    변경 메서드는 문서가 바뀌었으면 True를 반환한다."""

    path: Path

    def set_parent(self, gav: GAV, relative_path: Optional[str] = None) -> bool: ...
    def remove_parent(self) -> bool: ...
    def set_packaging(self, packaging: str) -> bool: ...
    def set_modules(self, modules: list[str]) -> bool: ...
    def add_module(self, module: str) -> bool: ...
    def get_property(self, key: str) -> Optional[str]: ...
    def set_property(self, key: str, value: str) -> bool: ...
    def remove_property(self, key: str) -> bool: ...
    def has_dependency(self, group_id: str, artifact_id: str) -> bool: ...
    def add_dependency(self, gav: GAV, if_absent: bool = True) -> bool: ...
    def remove_dependency(self, group_id: str, artifact_id: str) -> bool: ...
    def set_dependency_version(self, group_id: str, artifact_id: str, version: Optional[str]) -> bool: ...
    def replace_dependency(self, old_group: str, old_artifact: str, new: GAV) -> bool: ...
    def add_managed_dependency(self, gav: GAV, if_absent: bool = True) -> bool: ...
    def remove_managed_dependency(self, group_id: str, artifact_id: str) -> bool: ...
    def add_plugin(self, plugin: PluginInfo, if_absent: bool = True) -> bool: ...
    def set_plugin_version(self, group_id: str, artifact_id: str, version: str) -> bool: ...
    def remove_plugin(self, group_id: str, artifact_id: str) -> bool: ...
    def dependencies(self) -> list[GAV]: ...
    def save(self) -> None: ...
    def to_string(self) -> str: ...


# ---------------------------------------------------------------------------
# 재구성
# ---------------------------------------------------------------------------
class RestructureResult(BaseModel):
    """재구성 한 번의 결과를 담는 데이터 클래스.
    바뀐 파일, 사내 아티팩트 처리, 부모 교체, 실제 모듈 경로, 보고서 플래그 등을 담는다."""
    ok: bool
    changed_files: list[str] = Field(default_factory=list)
    moved_files: int = 0
    generated_files: list[str] = Field(default_factory=list)
    optional_bumped: list[str] = Field(default_factory=list)      # "g:a old→new"
    # 트리거 의존성(ArtifactSpec.when_present)이 있어서 새로 선언한 선택 사내 아티팩트와, replaces: true로 지운 트리거 선언
    # Formats: "g:a → <pom> (for <matched g:a>[, …])" / "<matched g:a> removed from <pom> → g:a".
    optional_added: list[str] = Field(default_factory=list)
    optional_replaced: list[str] = Field(default_factory=list)
    mandatory_added: list[str] = Field(default_factory=list)
    manual_followups: list[str] = Field(default_factory=list)      # 사람이 이후 할 일 문장
    notes: list[str] = Field(default_factory=list)
    error: Optional[str] = None
    # layout 모드에서 multi-module `merge` 정책을 쓸 때의 결과
    merged_modules: list[str] = Field(default_factory=list)        # comp-jar로 합친 as-is 모듈 디렉토리
    kept_boot_module: Optional[str] = None                         # 남긴 as-is boot 모듈의 최종 경로
    merge_conflicts: list[MergeConflict] = Field(default_factory=list)
    # 외부 설정 플레이스홀더. layout은 boot-jar에, msa_pom은 서비스마다 기록한다
    placeholder_files: list[str] = Field(default_factory=list)
    placeholders_written: list[str] = Field(default_factory=list)
    # 새 부모 체인이 관리해서 지운 명시 버전(`layout.drop_managed_versions`). "g:a: <explicit> → managed <version> (<pom>)"
    managed_versions_dropped: list[str] = Field(default_factory=list)
    # 관리 버전보다 높은 명시 버전 중 as-is 빌드에서는 관리 버전 이하였거나 없던 것.
    # 레시피가 올린 버전이므로 지운다. "g:a <explicit> (as-is <v | ->, managed <m>, <pom>)"
    recipe_versions_dropped: list[str] = Field(default_factory=list)
    # 어느 pom도 선언하지 않은 트리 안의 jar 중 재구성이 연결한 것과 그대로 둔 것. 저널과 인수인계에 쓴다
    # wired = {path, pom, coordinates, scope, placeholder, boot_poms};
    # skipped = {path, why}, why in superseded | container | unused | declared | skip.
    jars_wired: list[dict[str, Any]] = Field(default_factory=list)
    jars_skipped: list[dict[str, Any]] = Field(default_factory=list)
    # 새 부모로 옮기면서 버전이 빠진 의존성 중 as-is 빌드의 버전을 넣은 것과, as-is 빌드도 버전을 몰라 비워 둔 것
    # Formats: "g:a: <version> ← as-is <managed by g:a:v | declared> (<pom>)" / "g:a (<pom>)" (one follow-up
    # each).
    versions_pinned: list[str] = Field(default_factory=list)
    versions_unknown: list[str] = Field(default_factory=list)
    # 부모의 Boot BOM 속성을 덮어쓰던 as-is 버전 속성 중 지운 것. "<pom>: name=value"
    bom_properties_removed: list[str] = Field(default_factory=list)
    # 재구성이 추가한 BOM import와, artifacts.parent_manages_spring_boot의 실제 적용값.
    # auto면 부모 체인의 pom을 보고 정한다
    # bom_imports: "<pom>: g:a ${prop} (property of <defining pom>)" / "<pom>: g:a <v> (no property in the
    # parent chain)".
    bom_imports: list[str] = Field(default_factory=list)
    parent_manages_spring_boot_effective: bool | None = None
    # 지운 속성 이름과 그 속성을 정의한 사내 부모 체인 pom의 g:a:v. pom-verify 문구에 쓴다
    bom_property_sources: dict[str, str] = Field(default_factory=dict)
    # 관리 버전 정리로 지운 명시 버전에서만 참조하던 버전 속성. "<pom>: name=value"
    version_properties_removed: list[str] = Field(default_factory=list)
    # 필터링되는 리소스 아래 바이너리 파일이 깨지지 않도록 <nonFilteredFileExtensions>에 추가한 확장자.
    # pom마다 확장자와 플러그인을 남긴다
    non_filtered_extensions: list[str] = Field(default_factory=list)
    # 재구성이 넣거나 바꾼 모든 부모. "<pom>: <old g:a:v or '-'> → <new g:a:v>"
    parents_set: list[str] = Field(default_factory=list)
    # javax EE 이름을 jakarta로 바꾼 JSP, tag 파일 수와 바꾼 참조 수(`layout.jsp_jakarta_rename`).
    # jsp_jakarta_renamed에는 파일마다 "<rel path>: N"을 둔다
    jsp_jakarta_files: int = 0
    jsp_jakarta_refs: int = 0
    jsp_jakarta_renamed: list[str] = Field(default_factory=list)
    # 회사 레이아웃에서 실제로 쓴 모듈 경로(만들지 않았으면 None), boot 모듈의 packaging, front 모듈로 옮긴 파일 수.
    # boot_packaging은 jar이고, JSP가 있어 WAR로 만들었으면 war다
    boot_module: Optional[str] = None
    comp_module: Optional[str] = None
    front_module: Optional[str] = None
    boot_packaging: Optional[str] = None
    front_files: int = 0
    # 재구성이 올린 보고서 플래그(예: `web-xml-registered`). 오케스트레이터가 RunReport.flags에 더한다
    flags: list[str] = Field(default_factory=list)
    # Boot 전환 결과. 수정 루프의 layout facts, 구조 검토, 인수인계에서 읽는다.
    # 모두 선택 값이고 그 모드가 하지 않은 일이면 빈 목록이나 None이다
    starter: Optional[str] = None                                  # boot pom에 넣은 Boot starter
    # 웹 앱으로 본 근거. "web.xml", "spring-webmvc", "servlet-api", "@Controller ×N", "JSP ×N" 등
    web_evidence: list[str] = Field(default_factory=list)
    xml_imports: list[str] = Field(default_factory=list)           # @ImportResource의 `classpath:…` 항목
    # import하지 않은 as-is XML과 그 이유
    # e.g. "src/test/… (test)", "… (reached by <import> from …)", "… (not in contextConfigLocation)".
    xml_skipped: list[str] = Field(default_factory=list)
    # 기록한 레거시 호환 속성. 예: "spring.main.allow-bean-definition-overriding=true (…why)"
    legacy_properties_written: list[str] = Field(default_factory=list)
    scope_restored: list[str] = Field(default_factory=list)        # "g:a: compile → provided (as-is scope, <pom>)"
    # 기록한 소스 인코딩. "<pom>: project.build.sourceEncoding=MS949 (N files)"
    source_encodings_written: list[str] = Field(default_factory=list)
    # 바꾼 /WEB-INF 경로. "<copy>: /WEB-INF/config/x.properties → classpath:asis-webinf/…"
    webinf_locations_rewritten: list[str] = Field(default_factory=list)
    # 지운 빈. "<copy>: <bean id> (CommonsMultipartResolver → spring.servlet.multipart.*)"
    beans_removed: list[str] = Field(default_factory=list)
    duplicate_bean_ids: list[str] = Field(default_factory=list)    # "<id>: a.xml, b.xml"
    # 그 밖에 한 줄로 남길 정보(key별 str 또는 list[str]). 루프와 구조 검토가 "- <key>: <value>"로 보여 준다
    conversion_facts: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class RestructurerLike(Protocol):
    """재구성 실행기의 Protocol. mode는 layout(모듈 분리) / msa_pom(서비스별 pom만) / optional_bump /
    boot_pom(Boot as-is 유지)이다."""
    def apply(self, ws: Workspace, profile: ProjectProfile, cfg: Config, *,
              mode: Literal["layout", "msa_pom", "optional_bump", "boot_pom"],   # boot_pom은 제자리에서 고친다
              params: Optional[dict] = None) -> RestructureResult: ...


# ---------------------------------------------------------------------------
# OpenRewrite / Maven
# ---------------------------------------------------------------------------
@runtime_checkable
class RewriteRunnerLike(Protocol):
    """OpenRewrite 실행기의 Protocol. 레시피 목록을 한 번에 실행하고 RewriteResult를 반환한다.
    빌드는 하지 않는다."""
    def run(
        self,
        recipes: list[RecipeRef],
        *,
        extra_recipe_files: Iterable[Path] = (),
        module: Optional[str] = None,          # 이 모듈 디렉토리(MSA 서비스)에서만 실행한다. None이면 프로젝트 루트
        dry_run: bool = False,
        label: str = "",
        fork: Optional[bool] = None,          # None이면 설정 기본값, False면 rewrite:runNoFork(빌드가 깨진 프로젝트)
    ) -> RewriteResult: ...


@runtime_checkable
class BuildRunnerLike(Protocol):
    """Maven 실행기의 Protocol. goal을 실행하고 오류를 파싱한 BuildResult를 반환한다.
    테스트는 항상 건너뛴다."""
    def run(
        self,
        goal: str,                              # compile, test-compile, package, verify, "clean package"
        *,
        module: Optional[str] = None,           # 빌드할 pom의 디렉토리(MSA). None이면 프로젝트 루트
        extra_args: Iterable[str] = (),
        label: str = "",
        skip_test_compile: bool = False,
        skip_tests: bool = False,
    ) -> BuildResult: ...


# ---------------------------------------------------------------------------
# 트리아지, 수정
# ---------------------------------------------------------------------------
class TriageContext(BaseModel):
    """빌드 오류를 어떻게 처리할지 정할 때 쓰는 정보를 담는 데이터 클래스.
    단계, 라운드, 진척 없음 수준, 같은 서명의 이전 시도, 다른 위치에서 해결한 시도, LLM 호출 사용량을 담는다."""
    phase: Phase
    iteration: int
    stall_level: int = 0
    previous_attempts: list[FixAttempt] = Field(default_factory=list)   # 같은 클러스터 서명에 대한 시도
    # 이 케이스가 다른 위치에서 저장 버전을 실행해 해결한 시도. 수정 루프가 채우고 이때 previous_attempts는
    # 여기서 한 시도만 담는다. 비어 있으면 다시 실행할 것이 없다
    resolved_elsewhere: list[FixAttempt] = Field(default_factory=list)
    # 이 실행에서 모든 위치를 합한 케이스의 LLM 요청 수. None이면 모르는 것이고 위치를 합한 한도를 두지 않는다
    # docs/INTERFACES.md "v2.1z-cd" rule 4.
    llm_asks_total: Optional[int] = None
    llm_calls_used: int = 0
    llm_calls_budget: int = 60


@runtime_checkable
class TriagerLike(Protocol):
    """빌드 오류를 케이스로 묶고(cluster) KB 조회 결과를 보고 케이스마다 처리 방법을 정하는(decide) Protocol."""
    def cluster(self, errors: list[BuildError], profile: ProjectProfile) -> list[ErrorCluster]: ...
    def decide(self, cluster: ErrorCluster, kb_match: KBMatch, ctx: TriageContext, profile: ProjectProfile) -> TriageDecision: ...


class FixContext(BaseModel):
    """수정 프롬프트에 들어가는 정보를 담는 데이터 클래스.
    스크립트 요청 종류(new / augment)와 보강할 스크립트(script_base)도 담는다."""
    phase: Phase
    iteration: int
    stall_level: int = 0
    profile_summary: str = ""                     # 프로젝트 종류, Boot / Java 버전, 라이브러리 요약
    plan_notes: list[str] = Field(default_factory=list)
    kb_hints: list[str] = Field(default_factory=list)
    # 이 클러스터와 관련된 라이브러리 권고. 무엇으로 옮길지(대체품, 후속 작업)를 담는다(sbmig.advisory.hints_for)
    advisory_hints: list[str] = Field(default_factory=list)
    previous_attempts: list[FixAttempt] = Field(default_factory=list)
    other_errors_summary: str = ""                # 빌드의 나머지 오류를 클러스터마다 한 줄로 보여 준 것
    use_escalation_model: bool = False
    # 빌드와 별개인 참고 정보. 같은 경로 파일이 여러 as-is 모듈에 있어 하나만 남긴 병합 충돌 안내와,
    # 빠진 복사본의 읽기 전용 본문이다
    reference_notes: list[str] = Field(default_factory=list)   # 안내마다 한 줄
    reference_files: dict[str, str] = Field(default_factory=dict)   # 라벨이나 경로별 본문. 읽기 전용이고 고치지 않는다
    # 요청하는 스크립트 종류. augment면 script_base가 보강할 스크립트다
    script_mode: Optional[Literal["new", "augment"]] = None
    case_id: str = ""                             # 클러스터의 케이스 id(L2 서명)
    signature_generic: str = ""
    script_base: Optional[ScriptBase] = None      # augment일 때 이전 버전과 이 프로젝트에서의 결과
    # 프롬프트 "Dependency versions" 절에 넣을 정보. libraries.yml 항목과, 이 케이스 라이브러리의 저장소 버전
    # (같은 메이저 릴리스, jakarta 빌드 여부, 더 높은 메이저)을 한 줄씩 둔다. 모르면 빈 목록이다
    library_hints: list[str] = Field(default_factory=list)
    # 재구성 뒤의 프로젝트 정보를 한 줄씩 담는다.
    # Boot 전환 여부, 모듈과 부모, 생성 파일, starter, 사내 아티팩트 규칙, BOM import다.
    # 값은 모두 RestructureResult와 cfg에서 가져오고, 재구성이 없었으면 빈 목록이다. Boot as-is pom 모드도 부모와
    # 필수 아티팩트 정보는 넣는다. 프롬프트 머리 바로 뒤 "Project after the restructure" 절에 자르지 않고 넣는다
    layout_facts: list[str] = Field(default_factory=list)
    # 이관 범위와 그 범위에서 LLM이 지켜야 할 사실. full이면 scope_facts는 빈 목록이다.
    # jdk-jakarta면 시스템 프롬프트가 줄인 범위용 소개를 쓰고, scope_facts가 "Project after the restructure" 대신
    # "Scope of this run" 절이 된다
    scope: Literal["full", "jdk-jakarta"] = "full"
    scope_facts: list[str] = Field(default_factory=list)
    # 이 케이스의 오류를 남긴 다른 케이스의 시도를 설명하는 절의 본문. 그런 시도가 없으면 빈 문자열이다.
    # `earlier_fix_text`가 길이를 제한해 만들고 패커는 자르지 않고 그대로 넣는다
    # Section title: "Earlier fix these errors come from (it is in the tree)".
    earlier_fix: str = ""
    # 이 케이스의 이전 시도 중 오류를 없앴던(improved, build_ok) 시도. 패커는 이 시도를 "did NOT resolve it" 절에서
    # 빼고 "These errors were resolved before and are back" 절에 따로 보여 준다.
    # 2단계 수정기를 위해 previous_attempts에도 그대로 남는다
    resolved_before: list[FixAttempt] = Field(default_factory=list)

    # edits 모드가 없어진 뒤에도 `ctx.fix_mode`를 읽는 2단계 플러그인이 깨지지 않게 남긴 읽기 전용 값.
    # 직렬화나 프롬프트에는 나오지 않는다
    @property
    def fix_mode(self) -> str:
        """항상 "script"를 반환한다. 이전 플러그인과 호환하기 위한 읽기 전용 값이다."""
        return "script"

    @property
    def previous_edits(self) -> dict[str, str]:
        """항상 빈 dict를 반환한다. 이전 플러그인과 호환하기 위한 읽기 전용 값이다."""
        return {}


@runtime_checkable
class FixerLike(Protocol):
    """수정 스크립트를 만들어 주는 수정기의 Protocol(2단계 `fix.plugin`).
    케이스 하나(L2, 여러 파일에 걸칠 수 있다)를 받아 수정 스크립트로 답한다. `ctx.script_mode`에 따라 다르다.
    - new: 저장된 스크립트가 없을 때 새로 만든다
    - augment: `ctx.script_base`를 고친 완전한 새 스크립트를 만든다. 답은 기준 스크립트를 대신하고,
      기준 스크립트의 변경을 되돌린 트리에서 실행된다
    - `<need_file>`로 파일을 더 받는 왕복은 수정기 안에서 끝낸다
    실패하면 예외를 던지지 않고 ScriptProposal(code="", error=…)을 반환한다.

    Invariant 4 seam. Transport errors keep the analysis prefixes the loop classifies."""
    def propose_script(self, ws: Workspace, cluster: ErrorCluster, ctx: FixContext) -> ScriptProposal: ...


ScriptFixerLike = FixerLike          # FixerLike의 다른 이름


@runtime_checkable
class VerifyGuideLike(Protocol):
    """수정기가 선택적으로 제공하는 운영자 확인 가이드 기능의 Protocol. 메이저 업그레이드나 라이브러리 교체
    답에 운영자 확인 사항이 없으면 루프가 한 번 더 요청한다(`libraries.verify_guide_llm`).
    구현이 없거나 실패하면 sbmig가 diff로 가이드를 만든다. 예외를 던지지 않는다.
    - changes: 적용한 diff에서 읽은 DependencyChange 목록, diff: 그 diff(길이 제한 있음)
    - 보고서 언어로 쓴 가이드 문장을 반환하고, 실패하면 빈 문자열을 반환한다

    Fallback guide: `sbmig.build.depchanges.synthesized_guide`."""
    def verify_guide(self, ws: Workspace, cluster: ErrorCluster, ctx: FixContext, changes: list[DependencyChange],
                     diff: str) -> str: ...


# ---------------------------------------------------------------------------
# 수정 스크립트
# ---------------------------------------------------------------------------
@runtime_checkable
class ScriptRunnerLike(Protocol):
    """수정 스크립트 실행기의 Protocol. check는 정적 검사만 하고, run은 검사 후 별도 `python -I`
    프로세스에서 헬퍼 API `p`로 스크립트를 실행한다.
    변경은 커밋하지 않은 채 두고, 커밋할지 되돌릴지는 호출하는 쪽이 정한다. 예외를 던지지 않는다.
    - check: 문제 목록을 반환하고, 비어 있으면 허용이다
    - run: `p.errors`에 ``errors``를 넣고, ``timeout_s``(기본 script.timeout_seconds)가 지나면 강제 종료한다"""
    def check(self, code: str) -> list[str]: ...
    def run(self, ws: Workspace, code: str, *, errors: Iterable[BuildError] = (),
            timeout_s: Optional[int] = None) -> ScriptRunResult: ...


@runtime_checkable
class ScriptStoreLike(Protocol):
    """케이스별 수정 스크립트 저장소의 Protocol. 구현은 sbmig.errors.cases.CaseStore다.
    - best: 검증된(proven) 버전 중 최신을, 없으면 미검증(unverified) 버전 중 최신을 고른다.
      폐기된(retired) 버전은 고르지 않는다
    - credit: 재빌드 결과를 그 버전에 기록한다
    - find는 케이스가 없으면 None을, best는 고를 버전이 없으면 None을 반환한다

    Files: <kb>/cases/<case_id>/case.json + fix_v<N>.py. CaseStore also has `code(case_id, version)`,
    `retire` / `activate`, `pin_of` / `set_pin`, `describe(case_id)` (the "KB 탐색" log phrase),
    `record_run(entries)`, `stats()`."""
    def find(self, case_id: str) -> Optional[CaseInfo]: ...
    def best(self, case_id: str) -> Optional[tuple[CaseScript, str]]: ...
    def add_script(self, case_id: str, l2: str, code: str, *, source: str, run_id: str, project: str,
                   analysis: str = "", solution: str = "") -> CaseScript: ...
    def credit(self, case_id: str, version: int, result: str, project: str = "", run_id: str = "") -> None: ...


@runtime_checkable
class StructureReviewerLike(Protocol):
    """구조 검토의 Protocol. 재구성 뒤 첫 빌드 전에 LLM이 pom, 의존성, 모듈을 회사 규칙에 맞는지 검토한다.
    critical 항목은 `<edits>`로 직접 고치고 오케스트레이터가 `review: …`로 커밋한다. 예외를 던지지 않는다.
    실패하면 verdict가 failed나 skipped다.

    Company rules = artifacts.* / layout.*. With `review.ask_script: true` a critical fix may come as a
    script, run through the ScriptRunnerLike (uncommitted)."""
    def review(self, ws: Workspace, profile: ProjectProfile, cfg: Config, *, run_id: str = "",
               restructure_notes: Iterable[str] = (), restructure_results: Iterable["RestructureResult"] = ()
               ) -> StructureReview: ...


# ---------------------------------------------------------------------------
# KB
# ---------------------------------------------------------------------------
@runtime_checkable
class KnowledgeBaseLike(Protocol):
    """파일 KB의 Protocol. 구현은 CaseStore를 감싼 파사드인 sbmig.kb.FileKnowledgeBase다.
    케이스 저장소(cases), 조회(match), 실행 결과 기록(record_run), cases.md 재생성(reindex)을 제공한다.
    `sbmig.api.migrate(kb=False)`이거나 sbmig.kb를 불러올 수 없으면 아무것도 반환하지 않는 NullKnowledgeBase를 쓴다.

    `match` returns a CaseHit for a stored script or a pinned block; NullKnowledgeBase lives in sbmig.loop."""
    cases: Any

    def match(self, cluster: ErrorCluster, ws: Workspace, profile: ProjectProfile) -> KBMatch: ...
    def record_run(self, entries: Iterable[ErrorEntry]) -> None: ...
    def reindex(self) -> int: ...


ScriptKnowledgeLike = KnowledgeBaseLike   # KnowledgeBaseLike의 다른 이름
