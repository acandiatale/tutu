"""Protocols that each workstream implements. The orchestrator (sbmig.loop) only talks to these.

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
from typing import Iterable, Literal, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from .config import Config
from .models import (
    BuildError,
    CaseType,
    BuildResult,
    ErrorCluster,
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
    role: Literal["system", "user", "assistant"]
    content: str


class LLMResponse(BaseModel):
    content: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    finish_reason: Optional[str] = None


# Who is asking — lets an in-house gateway / agent platform route or budget by purpose.
LLMRole = Literal["fix", "fix_escalation", "plan_advisor", "structure_review", "other"]
# fix scripts use fix / fix_escalation; the structure review asks with structure_review; the plan advisor plan_advisor


class LLMRequest(BaseModel):
    """Everything sbmig knows about one call, in one object (the adapter tier contract)."""
    messages: list[ChatMessage]
    model: Optional[str] = None                   # cfg.llm.model / recipe_model / escalation_model, or None = adapter default
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None           # None = adapter default (Claude 5 models reject sampling params)
    stop: Optional[list[str]] = None
    role: LLMRole = "other"
    metadata: dict = Field(default_factory=dict)  # run_id, project, phase, cluster_id, attempt … (never secrets)


@runtime_checkable
class LLMClientLike(Protocol):
    """Low-level chat completion. Built-in clients: openai_compatible / anthropic / fake (sbmig.fix.client).
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
    """Adapter tier for in-house LLM gateways / agent platforms: one request object in, one response out.
    Plug in via `llm.provider: plugin` + `llm.plugin: "pkg.module:attr"` (or an entry point in group
    `sbmig.llm_adapters`). sbmig wraps it in an LLMClientLike (`sbmig.llm.AdapterClient`) and still records calls."""
    def complete(self, request: LLMRequest) -> LLMResponse: ...


# ---------------------------------------------------------------------------
# POM editing (implemented by sbmig.restructure.pom_editor, used by triage handlers too)
# ---------------------------------------------------------------------------
@runtime_checkable
class PomEditorLike(Protocol):
    """Formatting/comment-preserving editor for one pom.xml. All mutators return True if the document changed."""

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
    # round 9b: every parent the restructure set or replaced — "<pom>: <old g:a:v or '-'> → <new g:a:v>"
    parents_set: list[str] = Field(default_factory=list)
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
    def apply(self, ws: Workspace, profile: ProjectProfile, cfg: Config, *,
              mode: Literal["layout", "msa_pom", "optional_bump", "boot_pom"],   # boot_pom: round 9c Boot as-is in place
              params: Optional[dict] = None) -> RestructureResult: ...


# ---------------------------------------------------------------------------
# OpenRewrite / Maven
# ---------------------------------------------------------------------------
@runtime_checkable
class RewriteRunnerLike(Protocol):
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
    phase: Phase
    iteration: int
    stall_level: int = 0
    previous_attempts: list[FixAttempt] = Field(default_factory=list)   # attempts on same cluster signatures
    llm_calls_used: int = 0
    llm_calls_budget: int = 60


@runtime_checkable
class TriagerLike(Protocol):
    def cluster(self, errors: list[BuildError], profile: ProjectProfile) -> list[ErrorCluster]: ...
    def decide(self, cluster: ErrorCluster, kb_match: KBMatch, ctx: TriageContext, profile: ProjectProfile) -> TriageDecision: ...


class FixContext(BaseModel):
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


@runtime_checkable
class FixerLike(Protocol):
    """The fixer seam (invariant 4; tier 2 `fix.plugin`): the fixer answers a case with a fix script. `ctx.script_mode` = "new" (no
    stored script for the case) or "augment" (`ctx.script_base` = the version to extend; the answer must be the
    COMPLETE new script — it replaces the base and runs on a tree where the base was rolled back). The cluster is one
    case (L2) and may span many files; `<need_file>` rounds stay inside the fixer. Never raises: a failure is
    `ScriptProposal(code="", error=...)` (transport errors keep the v1 analysis prefixes the loop classifies)."""
    def propose_script(self, ws: Workspace, cluster: ErrorCluster, ctx: FixContext) -> ScriptProposal: ...


ScriptFixerLike = FixerLike          # the name step 1 introduced; one seam since step 4


# ---------------------------------------------------------------------------
# v2 fix scripts
# ---------------------------------------------------------------------------
@runtime_checkable
class ScriptRunnerLike(Protocol):
    """Runs one fix script against the project folder of `ws` (sbmig.script). `check` = the static check alone
    (empty list = allowed). `run` = check, then a separate `python -I` process with the helper API `p`
    (p.errors = `errors`), killed after `timeout_s` (default script.timeout_seconds). Changes stay uncommitted in the
    working tree — the caller commits / reverts. Never raises."""
    def check(self, code: str) -> list[str]: ...
    def run(self, ws: Workspace, code: str, *, errors: Iterable[BuildError] = (),
            timeout_s: Optional[int] = None) -> ScriptRunResult: ...


@runtime_checkable
class ScriptStoreLike(Protocol):
    """<kb>/scripts/<case_id>/v<N>.py + v<N>.meta.json (sbmig.kb.scripts). Writes hold the KB lock; analysis and
    logs are redacted; a script whose code matches a secret pattern is stored masked as `quarantined`."""
    def versions(self, case_id: str) -> list[ScriptMeta]: ...                  # oldest first
    def get(self, case_id: str, version: int) -> Optional[tuple[ScriptMeta, str]]: ...
    def best(self, case_id: str, *, limit: int = 1) -> list[tuple[ScriptMeta, str]]:
        """Reuse candidates, best first: active versions with at least one success (most success projects, then
        successes − failures, then the newest). Empty = nothing proven for this case."""
        ...
    def latest(self, case_id: str) -> Optional[tuple[ScriptMeta, str]]: ...   # newest active version (augment base)
    def add(self, case_id: str, signature_generic: str, code: str, *, parent: Optional[int] = None,
            analysis: str = "", model: str = "", run_id: str = "", project: str = "",
            source: Literal["llm", "human", "review"] = "llm") -> ScriptMeta:
        """Store the next version (an identical script — same sha1 — returns the existing meta)."""
        ...
    def credit(self, case_id: str, version: int, outcome: str, *, project: str = "", run_id: str = "") -> None:
        """After the scoring build: improved / build_ok = success (when every targeted error of the case is gone),
        partial = partial, no_change / regressed / apply_failed / blocked = failure, inconclusive / pending = nothing."""
        ...
    def case_ids(self) -> list[str]: ...


@runtime_checkable
class StructureReviewerLike(Protocol):
    """v2 (sbmig.review): after the restructure, before the first build — the LLM checks poms, dependencies and the
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
    """The file KB (sbmig.kb.store.FileKnowledgeBase): catalog hits for a cluster's case, the per-case script store,
    the review log and the catalog rebuild. `NullKnowledgeBase` (`--no-kb`) answers with nothing."""
    scripts: Optional[ScriptStoreLike]

    def match(self, cluster: ErrorCluster, ws: Workspace, profile: ProjectProfile) -> KBMatch: ...
    def record_review(self, review: StructureReview) -> None: ...             # one line in <kb>/reviews.jsonl
    def reindex(self) -> int: ...


ScriptKnowledgeLike = KnowledgeBaseLike   # the name step 1 introduced; one protocol since step 4
