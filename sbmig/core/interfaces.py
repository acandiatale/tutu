"""외부 구현을 끼워 넣는 연결 지점(Protocol)을 모아 둔 모듈.
오케스트레이터(sbmig.loop)는 구현 대신 이 Protocol만 사용한다.
회사 LLM 게이트웨이는 LLMRequest / LLMResponse(1단계, `llm.plugin`)로, 사내 코딩 에이전트는
FixerLike.propose_script(2단계, `fix.plugin`)로 연결한다. 새 HTTP 클라이언트는 만들지 않는다.
- LLMRequest / LLMResponse, FixerLike.propose_script, sbmig.api.migrate의 형태는 바꾸지 않는다
- 스크립트 저장소 구현은 sbmig.errors.cases.CaseStore다.
  영어 설명의 sbmig.kb.scripts.ScriptStore는 이전 이름이다

Protocols that each workstream implements. The orchestrator (sbmig.loop) only talks to these.

Implementations (module → class):
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
v2 (flow-v2, docs/v2/PLAN-v2.md):
    sbmig.script.ScriptRunner                       (ScriptRunnerLike)      fix scripts: static check + `python -I` run
    sbmig.kb.scripts.ScriptStore                    (ScriptStoreLike)       <kb>/scripts/<case_id>/v<N>.py + meta
    sbmig.fix.fixer.LLMFixer                        (ScriptFixerLike)       propose_script: new / augment
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
    """LLM 응답 하나를 담는 데이터 클래스(1단계 연결 규약). 본문, 모델, 토큰 수, 종료 이유를 담는다."""
    content: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    finish_reason: Optional[str] = None
    # v2.1z-n: the agent gateway creates a session when none is sent and returns its id — kept for the records and the
    # case-thread increment (sending it back continues that session); None for gateways without one
    session_id: Optional[str] = None


# Who is asking — lets an in-house gateway / agent platform route or budget by purpose.
# 사내 게이트웨이가 목적별로 라우팅과 예산을 나눌 수 있도록 호출 목적을 요청에 넣는다
LLMRole = Literal["fix", "fix_escalation", "plan_advisor", "structure_review", "verify_guide", "other"]
# fix scripts use fix / fix_escalation; the structure review asks with structure_review; the plan advisor plan_advisor


class LLMRequest(BaseModel):
    """LLM 호출 한 번의 요청을 담는 데이터 클래스(1단계 연결 규약).
    메시지, 모델, 토큰 한도, 목적(role), 메타데이터를 담는다.
    메타데이터에는 비밀 값을 넣지 않는다.

    Everything sbmig knows about one call, in one object (the adapter tier contract)."""
    messages: list[ChatMessage]
    model: Optional[str] = None                   # cfg.llm.model / recipe_model / escalation_model, or None = adapter default
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None           # None = adapter default (Claude 5 models reject sampling params)
    stop: Optional[list[str]] = None
    role: LLMRole = "other"
    metadata: dict = Field(default_factory=dict)  # run_id, project, phase, cluster_id, attempt … (never secrets)


@runtime_checkable
class LLMClientLike(Protocol):
    """채팅 응답을 받아 오는 저수준 LLM 클라이언트의 Protocol. 구현은 `role` / `metadata`와 나중에 추가될
    키워드 인자를 받을 수 있어야 하며, 받은 뒤 무시해도 된다.

    Low-level chat completion. Built-in clients: openai_compatible / anthropic / fake (sbmig.fix.client).
    Implementations must accept and may ignore `role` / `metadata` (and any future keyword)."""
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

    Adapter tier for in-house LLM gateways / agent platforms: one request object in, one response out.
    Plug in via `llm.provider: plugin` + `llm.plugin: "pkg.module:attr"` (or an entry point in group
    `sbmig.llm_adapters`). sbmig wraps it in an LLMClientLike (`sbmig.llm.AdapterClient`) and still records calls."""
    def complete(self, request: LLMRequest) -> LLMResponse: ...


# ---------------------------------------------------------------------------
# POM editing (implemented by sbmig.restructure.pom_editor, used by triage handlers too)
# ---------------------------------------------------------------------------
@runtime_checkable
class PomEditorLike(Protocol):
    """형식과 주석을 그대로 두고 pom.xml을 고치는 편집기의 Protocol. 재구성과 스크립트 헬퍼 API가 사용한다.
    변경 메서드는 문서가 바뀌었으면 True를 반환한다.

    Formatting/comment-preserving editor for one pom.xml. All mutators return True if the document changed."""

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
# Restructure
# ---------------------------------------------------------------------------
class RestructureResult(BaseModel):
    """재구성 한 번의 결과를 담는 데이터 클래스.
    바뀐 파일, 사내 아티팩트 처리, 부모 교체, 실제 모듈 경로, 보고서 플래그 등을 담는다."""
    ok: bool
    changed_files: list[str] = Field(default_factory=list)
    moved_files: int = 0
    generated_files: list[str] = Field(default_factory=list)
    optional_bumped: list[str] = Field(default_factory=list)      # "g:a old→new"
    # 트리거 의존성(ArtifactSpec.when_present)이 있어서 새로 선언한 선택 사내 아티팩트와, replaces: true로 지운 트리거 선언의 목록
    # "g:a → <pom> (for <matched g:a>[, …])" / "<matched g:a> removed from <pom> → g:a"
    optional_added: list[str] = Field(default_factory=list)
    optional_replaced: list[str] = Field(default_factory=list)
    mandatory_added: list[str] = Field(default_factory=list)
    manual_followups: list[str] = Field(default_factory=list)      # e.g. "web.xml not migrated; servlets registered via @ServletComponentScan"
    notes: list[str] = Field(default_factory=list)
    error: Optional[str] = None
    # layout mode, multi-module `merge` policy (round 7b)
    merged_modules: list[str] = Field(default_factory=list)        # as-is module dirs folded into comp-jar
    kept_boot_module: Optional[str] = None                         # as-is boot module kept (final path, e.g. module/boot-jar)
    merge_conflicts: list[MergeConflict] = Field(default_factory=list)
    # externalized-config placeholders (round 8): written into boot-jar (layout) / each service (msa_pom)
    placeholder_files: list[str] = Field(default_factory=list)
    placeholders_written: list[str] = Field(default_factory=list)
    # round 9: explicit dependency versions dropped because the new parent chain manages the artifact
    # (`layout.drop_managed_versions`); "g:a: <explicit> → managed <version> (<pom>)"
    managed_versions_dropped: list[str] = Field(default_factory=list)
    # 관리 버전보다 높은 명시 버전 중 as-is 빌드에서는 관리 버전 이하였거나 없던 것.
    # 레시피가 올린 버전이므로 지운다
    # "g:a <explicit> (as-is <v | ->, managed <m>, <pom>)"
    recipe_versions_dropped: list[str] = Field(default_factory=list)
    # v2.1z-j: bundled jars (declared by no pom) the restructure wired / left alone — dicts for the journal and the Handover:
    # wired = {path, pom, coordinates, scope, placeholder, boot_poms}, skipped = {path, why} (superseded | container | unused |
    # declared | skip)
    jars_wired: list[dict[str, Any]] = Field(default_factory=list)
    jars_skipped: list[dict[str, Any]] = Field(default_factory=list)
    # 새 부모로 옮기면서 버전이 빠진 의존성 중 as-is 빌드의 버전을 넣은 것과, as-is 빌드도 버전을 몰라 비워 둔 것
    # Version-less dependencies given the as-is build's
    # version (the new chain does not manage them): "g:a: <version> ← as-is <managed by g:a:v | declared>
    # (<pom>)"; the ones the as-is build did not resolve either (left empty, a follow-up each): "g:a (<pom>)"
    versions_pinned: list[str] = Field(default_factory=list)
    versions_unknown: list[str] = Field(default_factory=list)
    # 부모의 Boot BOM 속성을 덮어쓰던 as-is 버전 속성 중 지운 것. "<pom>: name=value"
    bom_properties_removed: list[str] = Field(default_factory=list)
    # 재구성이 추가한 BOM import. "<pom>: g:a ${prop} (property of <defining pom>)" / "<pom>: g:a <v> (no
    # property in the parent chain)"
    # 아래 값은 artifacts.parent_manages_spring_boot의 실제 적용값이다. auto면 부모 체인의 pom을 보고 정한다
    bom_imports: list[str] = Field(default_factory=list)
    parent_manages_spring_boot_effective: bool | None = None
    # 지운 속성 이름과 그 속성을 정의한 사내 부모 체인 pom의 g:a:v. pom-verify 문구에 쓴다
    bom_property_sources: dict[str, str] = Field(default_factory=dict)
    # 관리 버전 정리로 지운 명시 버전에서만 참조하던 버전 속성. "<pom>: name=value"
    version_properties_removed: list[str] = Field(default_factory=list)
    # 필터링되는 리소스 아래 바이너리 파일이 깨지지 않도록 <nonFilteredFileExtensions>에 추가한 확장자.
    # pom마다 확장자와 플러그인을 남긴다
    non_filtered_extensions: list[str] = Field(default_factory=list)
    # round 9b: every parent the restructure set or replaced — "<pom>: <old g:a:v or '-'> → <new g:a:v>"
    parents_set: list[str] = Field(default_factory=list)
    # v2.1h: JSP / tag files whose javax EE names were renamed to jakarta (`layout.jsp_jakarta_rename`), and the number
    # of renamed references; "<rel path>: N" per file in `jsp_jakarta_renamed`
    jsp_jakarta_files: int = 0
    jsp_jakarta_refs: int = 0
    jsp_jakarta_renamed: list[str] = Field(default_factory=list)
    # round 9c: the company layout — module paths actually used (None = not created), the boot module's packaging
    # (`jar`, or `war` when JSPs made it a WAR for external Tomcat) and the files moved into the front module
    boot_module: Optional[str] = None
    comp_module: Optional[str] = None
    front_module: Optional[str] = None
    boot_packaging: Optional[str] = None
    front_files: int = 0
    # 2026-10-23: report flags the restructure raises (e.g. `web-xml-registered`); the orchestrator adds them to
    # RunReport.flags
    flags: list[str] = Field(default_factory=list)
    # v2.1x (2026-10-31, prompt review §4 — the legacy Spring context after the conversion; read by the fix loop's
    # layout facts, the structure review's "What the restructure did" and the Handover). All optional, [] / None when
    # the mode did not do it.
    # Boot 전환 결과. 수정 루프의 layout facts, 구조 검토, 인수인계에서 읽는다
    starter: Optional[str] = None                                  # the Boot starter written into the boot pom
    web_evidence: list[str] = Field(default_factory=list)          # why the project counts as a web app ("web.xml",
                                                                   # "spring-webmvc", "servlet-api", "@Controller ×N", "JSP ×N")
    xml_imports: list[str] = Field(default_factory=list)           # `classpath:legacy-config/…` entries of @ImportResource
    xml_skipped: list[str] = Field(default_factory=list)           # as-is XML files NOT imported and why ("src/test/… (test)",
                                                                   # "… (reached by <import> from …)", "… (not in contextConfigLocation)")
    legacy_properties_written: list[str] = Field(default_factory=list)   # "spring.main.allow-bean-definition-overriding=true (…why)"
    scope_restored: list[str] = Field(default_factory=list)        # "g:a: compile → provided (as-is scope, <pom>)"
    source_encodings_written: list[str] = Field(default_factory=list)   # "<pom>: project.build.sourceEncoding=MS949 (N files)"
    webinf_locations_rewritten: list[str] = Field(default_factory=list) # "<copy>: /WEB-INF/config/x.properties → classpath:asis-webinf/…"
    beans_removed: list[str] = Field(default_factory=list)         # "<copy>: <bean id> (CommonsMultipartResolver → spring.servlet.multipart.*)"
    duplicate_bean_ids: list[str] = Field(default_factory=list)    # "<id>: a.xml, b.xml"
    conversion_facts: dict[str, Any] = Field(default_factory=dict) # anything else worth a line (key → str | list[str]);
                                                                   # rendered as "- <key>: <value>" by the loop and the review


@runtime_checkable
class RestructurerLike(Protocol):
    """재구성 실행기의 Protocol. mode는 layout(모듈 분리) / msa_pom(서비스별 pom만) / optional_bump /
    boot_pom(Boot as-is 유지)이다."""
    def apply(self, ws: Workspace, profile: ProjectProfile, cfg: Config, *,
              mode: Literal["layout", "msa_pom", "optional_bump", "boot_pom"],   # boot_pom: round 9c Boot as-is in place
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
        module: Optional[str] = None,          # run only in this module directory (MSA service) — None = project root
        dry_run: bool = False,
        label: str = "",
        fork: Optional[bool] = None,          # None = config default; False = rewrite:runNoFork (broken projects)
    ) -> RewriteResult: ...


@runtime_checkable
class BuildRunnerLike(Protocol):
    """Maven 실행기의 Protocol. goal을 실행하고 오류를 파싱한 BuildResult를 반환한다.
    테스트는 항상 건너뛴다."""
    def run(
        self,
        goal: str,                              # compile | test-compile | package | verify | "clean package"
        *,
        module: Optional[str] = None,           # directory of the pom to build (MSA) — None = project root
        extra_args: Iterable[str] = (),
        label: str = "",
        skip_test_compile: bool = False,
        skip_tests: bool = False,
    ) -> BuildResult: ...


# ---------------------------------------------------------------------------
# Triage / handlers / fix
# ---------------------------------------------------------------------------
class TriageContext(BaseModel):
    """빌드 오류를 어떻게 처리할지 정할 때 쓰는 정보를 담는 데이터 클래스.
    단계, 라운드, 진척 없음 수준, 같은 서명의 이전 시도, LLM 호출 사용량을 담는다."""
    phase: Phase
    iteration: int
    stall_level: int = 0
    previous_attempts: list[FixAttempt] = Field(default_factory=list)   # attempts on same cluster signatures
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
    profile_summary: str = ""                     # short text: case type, boot/java versions, libs
    plan_notes: list[str] = Field(default_factory=list)
    kb_hints: list[str] = Field(default_factory=list)
    advisory_hints: list[str] = Field(default_factory=list)   # library advisories relevant to this cluster: what to
                                                  # migrate TO (replacement, followup text) — round 6, sbmig.advisory.hints_for
    previous_attempts: list[FixAttempt] = Field(default_factory=list)
    other_errors_summary: str = ""                # one-line-per-cluster view of the rest of the build
    use_escalation_model: bool = False
    # round 9: context that is not part of the build — merge-conflict hints (a same-path file existed in more than one
    # as-is module; first-wins kept one copy) and the dropped copies as read-only reference text
    reference_notes: list[str] = Field(default_factory=list)   # one line per hint ("<kept path> was merged first-wins …")
    reference_files: dict[str, str] = Field(default_factory=dict)   # label/path → content (read-only, never patched)
    # v2 fix scripts (ScriptFixerLike.propose_script): what is asked for and, for augment, the script to extend
    script_mode: Optional[Literal["new", "augment"]] = None
    case_id: str = ""                             # the cluster's case (L2 signature id)
    signature_generic: str = ""
    script_base: Optional[ScriptBase] = None      # augment: the previous version and what it did in this project
    # v2.1d dependency version policy: facts for the prompt section "Dependency versions" — the people's table entry
    # (libraries.yml) and what the repository has for the libraries behind this case (same-major releases, which are
    # jakarta builds, the higher majors), one line each; [] when nothing is known
    library_hints: list[str] = Field(default_factory=list)
    # 재구성 뒤의 프로젝트 정보를 한 줄씩 담는다.
    # Boot 전환 여부, 모듈과 부모, 생성 파일, starter, 사내 아티팩트 규칙, BOM import다
    # 값은 모두 RestructureResult와 cfg에서 가져오고, 재구성이 없었으면 빈 목록이다
    # v2.1v: facts about the project AFTER the restructure (converted to Boot by sbmig, module roles and parents, generated
    # files, boot starter, company artifact rules, BOM imports) — one line each, rendered as the section "Project after the
    # restructure" right after the header and never truncated; [] when no restructure ran (Boot as-is pom-only mode still
    # lists the parents / mandatory facts).
    layout_facts: list[str] = Field(default_factory=list)

    # edits 모드가 없어진 뒤에도 `ctx.fix_mode`를 읽는 2단계 플러그인이 깨지지 않게 남긴 읽기 전용 값. 직렬화나
    # 프롬프트에는 나오지 않는다 / v2.1z compatibility for tier-2 plugins written against v2.1i: read-only, never
    # serialized, never rendered.
    @property
    def fix_mode(self) -> str:
        """항상 "script"를 반환한다. 이전 플러그인과 호환하기 위한 읽기 전용 값이다.

        Always "script": the edits mode went in v2.1z (read-only, for plugins written against v2.1i)."""
        return "script"

    @property
    def previous_edits(self) -> dict[str, str]:
        """항상 빈 dict를 반환한다. 이전 플러그인과 호환하기 위한 읽기 전용 값이다.

        Always {}: the edits-mode history went in v2.1z (read-only, for plugins written against v2.1i)."""
        return {}


@runtime_checkable
class FixerLike(Protocol):
    """수정 스크립트를 만들어 주는 수정기의 Protocol(2단계 `fix.plugin`).
    케이스 하나를 받아 수정 스크립트로 답한다.
    - new: 저장된 스크립트가 없을 때 새로 만든다
    - augment: script_base를 고친 완전한 새 스크립트를 만든다.
      이전 스크립트의 변경을 되돌린 트리에서 실행된다
    실패하면 예외를 던지지 않고 ScriptProposal(error=…)을 반환한다.

    The fixer seam (invariant 4; tier 2 `fix.plugin`): the fixer answers a case with a fix script. `ctx.script_mode` = "new" (no
    stored script for the case) or "augment" (`ctx.script_base` = the version to extend; the answer must be the
    COMPLETE new script — it replaces the base and runs on a tree where the base was rolled back). The cluster is one
    case (L2) and may span many files; `<need_file>` rounds stay inside the fixer. Never raises: a failure is
    `ScriptProposal(code="", error=...)` (transport errors keep the v1 analysis prefixes the loop classifies)."""
    def propose_script(self, ws: Workspace, cluster: ErrorCluster, ctx: FixContext) -> ScriptProposal: ...


ScriptFixerLike = FixerLike          # the name step 1 introduced; one seam since step 4


@runtime_checkable
class VerifyGuideLike(Protocol):
    """수정기가 선택적으로 제공하는 운영자 확인 가이드 기능의 Protocol. 메이저 업그레이드나 라이브러리 교체
    답에 운영자 확인 사항이 없으면 루프가 한 번 더 요청한다(`libraries.verify_guide_llm`).
    구현이 없거나 실패하면 sbmig가 diff로 가이드를 만든다. 예외를 던지지 않는다.

    v2.1d, optional on a fixer: after a fix that raised a dependency's major version or replaced a library, the loop
    asks ONCE for the 운영자 확인 사항 the answer did not carry (`libraries.verify_guide_llm`). `changes` = the
    DependencyChange items read from the applied diff, `diff` = that diff (capped). Returns the guide text (plain
    lines, the report language) or "" on any failure; never raises. A fixer without it gets sbmig's synthesized
    guide (`sbmig.build.depchanges.synthesized_guide`)."""
    def verify_guide(self, ws: Workspace, cluster: ErrorCluster, ctx: FixContext, changes: list[DependencyChange],
                     diff: str) -> str: ...


# ---------------------------------------------------------------------------
# v2 fix scripts
# ---------------------------------------------------------------------------
@runtime_checkable
class ScriptRunnerLike(Protocol):
    """수정 스크립트 실행기의 Protocol. check는 정적 검사만 하고, run은 검사 후 별도 `python -I`
    프로세스에서 헬퍼 API `p`로 스크립트를 실행한다.
    변경은 커밋하지 않은 채 두고, 커밋할지 되돌릴지는 호출하는 쪽이 정한다. 예외를 던지지 않는다.

    Runs one fix script against the project folder of `ws` (sbmig.script). `check` = the static check alone
    (empty list = allowed). `run` = check, then a separate `python -I` process with the helper API `p`
    (p.errors = `errors`), killed after `timeout_s` (default script.timeout_seconds). Changes stay uncommitted in the
    working tree — the caller commits / reverts. Never raises."""
    def check(self, code: str) -> list[str]: ...
    def run(self, ws: Workspace, code: str, *, errors: Iterable[BuildError] = (),
            timeout_s: Optional[int] = None) -> ScriptRunResult: ...


@runtime_checkable
class ScriptStoreLike(Protocol):
    """케이스별 수정 스크립트 저장소의 Protocol. 구현은 sbmig.errors.cases.CaseStore다.
    - best: 검증된(proven) 버전 중 최신을, 없으면 미검증(unverified) 버전 중 최신을 고른다.
      폐기된(retired) 버전은 고르지 않는다
    - credit: 재빌드 결과를 해당 버전에 기록한다

    v2.1: the per-case script store is `sbmig.errors.cases.CaseStore` (<kb>/cases/<case_id>/case.json + fix_v<N>.py):
    `find(case_id) -> CaseInfo | None`, `best(case_id) -> (CaseScript, code) | None` (proven newest, else unverified
    newest; retired never), `code(case_id, version)`, `add_script(case_id, l2, code, *, source, run_id, project,
    analysis, solution) -> CaseScript`, `credit(case_id, version, result, project, run_id)`, `retire` / `activate`,
    `pin_of` / `set_pin`, `describe(case_id)` (the "KB 탐색" log phrase), `record_run(entries)`, `stats()`."""
    def find(self, case_id: str) -> Optional[CaseInfo]: ...
    def best(self, case_id: str) -> Optional[tuple[CaseScript, str]]: ...
    def add_script(self, case_id: str, l2: str, code: str, *, source: str, run_id: str, project: str,
                   analysis: str = "", solution: str = "") -> CaseScript: ...
    def credit(self, case_id: str, version: int, result: str, project: str = "", run_id: str = "") -> None: ...


@runtime_checkable
class StructureReviewerLike(Protocol):
    """구조 검토의 Protocol. 재구성 뒤 첫 빌드 전에 LLM이 pom, 의존성, 모듈을 회사 규칙에 맞는지 검토한다.
    critical 항목은 `<edits>`로 직접 고치고 오케스트레이터가 `review: …`로 커밋한다. 예외를 던지지 않는다.

    v2 (sbmig.review): after the restructure, before the first build — the LLM checks poms, dependencies and the
    module layout against the company rules (artifacts.* / layout.*). Critical issues with a script → the script
    runs through the ScriptRunnerLike (uncommitted; the orchestrator commits it as `review: …`); the review is
    recorded in <kb>/reviews.jsonl when `kb` is given. Never raises (verdict failed / skipped instead)."""
    def review(self, ws: Workspace, profile: ProjectProfile, cfg: Config, *, run_id: str = "",
               restructure_notes: Iterable[str] = (), restructure_results: Iterable["RestructureResult"] = ()
               ) -> StructureReview: ...


# ---------------------------------------------------------------------------
# Knowledge base
# ---------------------------------------------------------------------------
@runtime_checkable
class KnowledgeBaseLike(Protocol):
    """파일 KB의 Protocol. 구현은 CaseStore를 감싼 파사드인 sbmig.kb.FileKnowledgeBase다.
    케이스 저장소(cases), 조회(match), 실행 결과 기록(record_run), cases.md 재생성(reindex)을 제공한다.
    `sbmig.api.migrate(kb=False)`이거나 sbmig.kb를 불러올 수 없으면 아무것도 반환하지 않는 NullKnowledgeBase를 쓴다.

    The v2.1 file KB (sbmig.kb.FileKnowledgeBase, a facade over sbmig.errors.cases.CaseStore): the case store
    (`cases`: find / best / code / add_script / credit / describe / pin), catalog hits for a cluster's case
    (`match`: a CaseHit for a stored script or a pinned block), the end-of-run append (`record_run`) and the
    `cases.md` rebuild (`reindex`). `NullKnowledgeBase` (sbmig.loop: `sbmig.api.migrate(kb=False)`, or sbmig.kb
    unavailable; the `--no-kb` switch went in v2.1z) answers with nothing."""
    cases: Any

    def match(self, cluster: ErrorCluster, ws: Workspace, profile: ProjectProfile) -> KBMatch: ...
    def record_run(self, entries: Iterable[ErrorEntry]) -> None: ...
    def reindex(self) -> int: ...


ScriptKnowledgeLike = KnowledgeBaseLike   # the step-1 name; one protocol since step 4
