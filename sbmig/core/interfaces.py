"""플러그인 이음새(Protocol) — 오케스트레이터(sbmig.loop)는 구현이 아니라 이 Protocol 들과만 이야기한다.
아래 영어 목록이 Protocol → 구현 모듈 표다. 회사 LLM 게이트웨이는 LLMRequest / LLMResponse(1단계,
`llm.plugin`)로, 사내 코딩 에이전트는 FixerLike.propose_script(2단계, `fix.plugin`)로 꽂는다 — 새 HTTP
클라이언트를 만들지 않는다. 불변조건 4: LLMRequest / LLMResponse, FixerLike.propose_script, sbmig.api.migrate
의 모양은 바꾸지 않는다. v2.1 의 스크립트 저장소는 sbmig.errors.cases.CaseStore 다. 영어 목록의
sbmig.kb.scripts.ScriptStore 는 v2 초기 이름이다.

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
    """LLM 대화 메시지 하나(system / user / assistant). 회사 게이트웨이에는 messages 만 간다."""
    role: Literal["system", "user", "assistant"]
    content: str


class LLMResponse(BaseModel):
    """LLM 응답 하나(1단계 계약, 불변조건 4): 본문, 모델, 토큰 수,
    종료 이유. 어댑터가 만들고 수정기·구조 검토가 읽는다."""
    content: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    finish_reason: Optional[str] = None


# Who is asking — lets an in-house gateway / agent platform route or budget by purpose.
# 호출 목적 — 사내 게이트웨이가 목적별로 라우팅·예산을 나눌 수 있게 요청에 실어 보낸다.
LLMRole = Literal["fix", "fix_escalation", "plan_advisor", "structure_review", "verify_guide", "other"]
# fix scripts use fix / fix_escalation; the structure review asks with structure_review; the plan advisor plan_advisor


class LLMRequest(BaseModel):
    """LLM 호출 한 번에 대해 sbmig 가 아는 전부(1단계 어댑터 계약, 불변조건 4): 메시지, 모델, 토큰 한도,
    목적(role), 메타데이터(run_id·케이스 등 — 비밀은 절대 넣지 않음).

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
    """저수준 채팅 완성 클라이언트. 내장: company(회사 게이트웨이) / openai_compatible / anthropic /
    fake(재생). 구현은 `role` / `metadata`(와 앞으로 생길 키워드)를 받아야 하고 무시해도 된다.

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
    """1단계 어댑터: 사내 게이트웨이·에이전트 플랫폼용, 요청 하나(LLMRequest) → 응답 하나(LLMResponse).
    `llm.provider: plugin` + `llm.plugin` 또는 entry point 로
    꽂으면 sbmig 가 LLMClientLike 로 감싸고 호출 기록도 남긴다.

    Adapter tier for in-house LLM gateways / agent platforms: one request object in, one response out.
    Plug in via `llm.provider: plugin` + `llm.plugin: "pkg.module:attr"` (or an entry point in group
    `sbmig.llm_adapters`). sbmig wraps it in an LLMClientLike (`sbmig.llm.AdapterClient`) and still records calls."""
    def complete(self, request: LLMRequest) -> LLMResponse: ...


# ---------------------------------------------------------------------------
# POM editing (implemented by sbmig.restructure.pom_editor, used by triage handlers too)
# ---------------------------------------------------------------------------
@runtime_checkable
class PomEditorLike(Protocol):
    """pom.xml 하나의 형식·주석 보존 편집기(sbmig.restructure.pom_editor). 재구성과 스크립트 헬퍼 API 의 pom
    편집이 쓴다. 모든 변경 메서드는 문서가 바뀌었으면 True 를 돌려준다.

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
    """재구성 단계 한 번의 결과: 바뀐·생성된 파일, 필수·선택 아티팩트 처리, 병합 충돌, 외부 설정
    placeholder, 부모 교체, 회사 레이아웃의 실제 모듈 경로(boot / comp / front)와 boot 패키징, 보고서
    플래그. 오케스트레이터가 보고서·구조 검토에 넘긴다."""
    ok: bool
    changed_files: list[str] = Field(default_factory=list)
    moved_files: int = 0
    generated_files: list[str] = Field(default_factory=list)
    optional_bumped: list[str] = Field(default_factory=list)      # "g:a old→new"
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
    # v2.1l: 새 부모로 옮기며 버전을 잃은 의존성에 as-is 빌드의 버전을 적은 것(새 부모 체인이
    # 관리하면 다시 비움), as-is 빌드도 몰라 비어 남은 것. Version-less dependencies given the as-is build's
    # version (the new chain does not manage them): "g:a: <version> ← as-is <managed by g:a:v | declared>
    # (<pom>)"; the ones the as-is build did not resolve either (left empty, a follow-up each): "g:a (<pom>)"
    versions_pinned: list[str] = Field(default_factory=list)
    versions_unknown: list[str] = Field(default_factory=list)
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


@runtime_checkable
class RestructurerLike(Protocol):
    """재구성 실행기. mode: layout(모듈 나눔) / msa_pom(MSA 서비스별 pom 만) / optional_bump /
    boot_pom(Boot as-is 제자리)."""
    def apply(self, ws: Workspace, profile: ProjectProfile, cfg: Config, *,
              mode: Literal["layout", "msa_pom", "optional_bump", "boot_pom"],   # boot_pom: round 9c Boot as-is in place
              params: Optional[dict] = None) -> RestructureResult: ...


# ---------------------------------------------------------------------------
# OpenRewrite / Maven
# ---------------------------------------------------------------------------
@runtime_checkable
class RewriteRunnerLike(Protocol):
    """OpenRewrite 실행기(transform.rewrite): 레시피 목록을
    한 번에 돌리고 RewriteResult 를 돌려준다(빌드 없음)."""
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
    """Maven 실행기: goal(compile / package …)을 돌려 오류를 파싱한
    BuildResult 를 돌려준다. 테스트는 항상 건너뛴다(회사 결정)."""
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
    """트리아지가 결정을 내릴 때 보는 맥락: 단계, 라운드, 정체
    수준, 같은 서명에 대한 이전 시도, LLM 호출 사용량."""
    phase: Phase
    iteration: int
    stall_level: int = 0
    previous_attempts: list[FixAttempt] = Field(default_factory=list)   # attempts on same cluster signatures
    llm_calls_used: int = 0
    llm_calls_budget: int = 60


@runtime_checkable
class TriagerLike(Protocol):
    """트리아지: 빌드 오류를 클러스터(케이스)로 묶고(cluster),
    KB 조회 결과를 보고 클러스터마다 결정을 내린다(decide)."""
    def cluster(self, errors: list[BuildError], profile: ProjectProfile) -> list[ErrorCluster]: ...
    def decide(self, cluster: ErrorCluster, kb_match: KBMatch, ctx: TriageContext, profile: ProjectProfile) -> TriageDecision: ...


class FixContext(BaseModel):
    """수정기에 넘기는 맥락(수정 프롬프트의 재료): 프로필 요약, KB·권고·라이브러리 힌트, 이전 시도, 나머지
    오류 요약, 병합 충돌 참고 파일, 그리고 v2 스크립트 요청
    종류(new / augment)와 보강할 스크립트(script_base)."""
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
    # v2.1i: `edits` = ask for edits only (no script section, no helper API); plugins read it the same way
    fix_mode: Literal["script", "edits"] = "script"
    case_id: str = ""                             # the cluster's case (L2 signature id)
    signature_generic: str = ""
    script_base: Optional[ScriptBase] = None      # augment: the previous version and what it did in this project
    # v2.1i edits 모드: 이 실행에서 같은 케이스의 앞 시도(previous_attempts 의 id → 결과·실패 이유·트리에 남았는지·적용한
    # diff 또는 적용 못 한 edits). script 모드에서는 비어 있다. / edits mode: attempt id (of `previous_attempts`) → what
    # that try did — result and why it failed, whether it is still in the tree, the applied diff or the edits that did
    # not apply (capped); {} in script mode (augment carries `script_base` instead)
    previous_edits: dict[str, str] = Field(default_factory=dict)
    # v2.1d dependency version policy: facts for the prompt section "Dependency versions" — the people's table entry
    # (libraries.yml) and what the repository has for the libraries behind this case (same-major releases, which are
    # jakarta builds, the higher majors), one line each; [] when nothing is known
    library_hints: list[str] = Field(default_factory=list)


@runtime_checkable
class FixerLike(Protocol):
    """수정기 이음새(불변조건 4, 2단계 `fix.plugin`): 케이스 하나에 수정 스크립트로 답한다. new = 저장본 없음,
    augment = script_base 를 고친 '완전한' 새 스크립트(기존 것을
    되돌린 트리에서 돈다). 실패는 예외 대신 ScriptProposal(error=…).

    The fixer seam (invariant 4; tier 2 `fix.plugin`): the fixer answers a case with a fix script. `ctx.script_mode` = "new" (no
    stored script for the case) or "augment" (`ctx.script_base` = the version to extend; the answer must be the
    COMPLETE new script — it replaces the base and runs on a tree where the base was rolled back). The cluster is one
    case (L2) and may span many files; `<need_file>` rounds stay inside the fixer. Never raises: a failure is
    `ScriptProposal(code="", error=...)` (transport errors keep the v1 analysis prefixes the loop classifies)."""
    def propose_script(self, ws: Workspace, cluster: ErrorCluster, ctx: FixContext) -> ScriptProposal: ...


ScriptFixerLike = FixerLike          # the name step 1 introduced; one seam since step 4


@runtime_checkable
class VerifyGuideLike(Protocol):
    """v2.1d, 수정기의 선택 기능: 메이저 업그레이드·라이브러리 교체를 한 수정의 답에 운영자 확인 사항이 없으면
    루프가 한 번 더 묻는다(`libraries.verify_guide_llm`). 없거나
    실패하면 sbmig 가 diff 로 가이드를 만든다. 예외를 내지 않는다.

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
    """수정 스크립트 실행기(sbmig.script): check = 정적 검사만, run = 검사 후 `python -I` 별도 프로세스에서
    헬퍼 API `p` 로 실행(timeout 뒤 프로세스 트리 강제 종료). 변경은 커밋하지 않은 채 두고 호출자가
    커밋·되돌림을 정한다. 예외를 내지 않는다.

    Runs one fix script against the project folder of `ws` (sbmig.script). `check` = the static check alone
    (empty list = allowed). `run` = check, then a separate `python -I` process with the helper API `p`
    (p.errors = `errors`), killed after `timeout_s` (default script.timeout_seconds). Changes stay uncommitted in the
    working tree — the caller commits / reverts. Never raises."""
    def check(self, code: str) -> list[str]: ...
    def run(self, ws: Workspace, code: str, *, errors: Iterable[BuildError] = (),
            timeout_s: Optional[int] = None) -> ScriptRunResult: ...


@runtime_checkable
class ScriptStoreLike(Protocol):
    """케이스별 스크립트 저장소 계약. v2.1 구현은
    sbmig.errors.cases.CaseStore(`<kb>/cases/<case_id>/case.json` + fix_v<N>.py). best = proven 중 최신,
    없으면 unverified 중 최신(retired 는 절대 안 고름); credit = 재빌드 점수를 그 버전에 기록.

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
    """구조 검토 이음새(sbmig.review): 재구성 뒤 첫 빌드 전에 LLM 이 pom·의존성·모듈을 회사 규칙으로 검토한다.
    v2.1 은 critical 을 `<edits>` 로 직접 고치고(스크립트만 온 답은 ScriptRunnerLike 로), 오케스트레이터가
    `review: …` 로 커밋한다. 예외를 내지 않는다.

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
    """v2.1 파일 KB 이음새(sbmig.kb.FileKnowledgeBase = CaseStore 위의 창구): 케이스 저장소(cases), 클러스터
    조회(match), 실행 끝 기록(record_run), cases.md 재생성(reindex). `--no-kb` 이면 아무것도 답하지 않는
    NullKnowledgeBase 를 쓴다.

    The v2.1 file KB (sbmig.kb.FileKnowledgeBase, a facade over sbmig.errors.cases.CaseStore): the case store
    (`cases`: find / best / code / add_script / credit / describe / pin), catalog hits for a cluster's case
    (`match`: a CaseHit for a stored script or a pinned block), the end-of-run append (`record_run`) and the
    `cases.md` rebuild (`reindex`). `NullKnowledgeBase` (`--no-kb`, sbmig.loop) answers with nothing."""
    cases: Any

    def match(self, cluster: ErrorCluster, ws: Workspace, profile: ProjectProfile) -> KBMatch: ...
    def record_run(self, entries: Iterable[ErrorEntry]) -> None: ...
    def reindex(self) -> int: ...


ScriptKnowledgeLike = KnowledgeBaseLike   # the step-1 name; one protocol since step 4
