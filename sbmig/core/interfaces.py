"""Protocols that each workstream implements. The orchestrator (sbmig.loop) only talks to these.

Implementations (module → class):
    sbmig.analyze.analyzer     analyze_project(ws, cfg) -> ProjectProfile
    sbmig.plan.planner         build_plan(profile, cfg, kb=None) -> MigrationPlan
    sbmig.restructure.pom_editor.PomEditor          (PomEditorLike)
    sbmig.restructure.restructurer.Restructurer     (RestructurerLike)
    sbmig.rewrite.runner.OpenRewriteRunner          (RewriteRunnerLike)
    sbmig.build.runner.MavenRunner                  (BuildRunnerLike)
    sbmig.triage.triager.Triager                    (TriagerLike)
    sbmig.triage.handlers.HandlerRegistry           (HandlerLike items)
    sbmig.fix.client.*                              (LLMClientLike)
    sbmig.fix.fixer.LLMFixer                        (FixerLike)
    sbmig.kb.store.FileKnowledgeBase                (KnowledgeBaseLike)
    sbmig.loop.orchestrator.Orchestrator            drives everything
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Literal, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from .config import Config
from .models import (
    BuildError,
    BuildResult,
    ErrorCluster,
    FileChange,
    FixAttempt,
    FixProposal,
    GAV,
    HarvestResult,
    KBCase,
    KBMatch,
    KBRecipe,
    Phase,
    PhaseResult,
    PluginInfo,
    ProjectProfile,
    RecipeRef,
    RewriteResult,
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


@runtime_checkable
class LLMClientLike(Protocol):
    def complete(
        self,
        messages: list[ChatMessage],
        *,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        stop: Optional[list[str]] = None,
    ) -> LLMResponse: ...


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


@runtime_checkable
class RestructurerLike(Protocol):
    def apply(self, ws: Workspace, profile: ProjectProfile, cfg: Config, *,
              mode: Literal["layout", "msa_pom", "optional_bump"],
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
class HandlerLike(Protocol):
    """Deterministic fix. `matches` must be cheap and side-effect free."""
    id: str
    description: str

    def matches(self, cluster: ErrorCluster, profile: ProjectProfile, cfg: Config) -> bool: ...
    def apply(self, ws: Workspace, cluster: ErrorCluster, profile: ProjectProfile, cfg: Config) -> list[FileChange]: ...


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
    previous_attempts: list[FixAttempt] = Field(default_factory=list)
    other_errors_summary: str = ""                # one-line-per-cluster view of the rest of the build
    use_escalation_model: bool = False


@runtime_checkable
class FixerLike(Protocol):
    def propose(self, ws: Workspace, cluster: ErrorCluster, ctx: FixContext) -> FixProposal: ...
    def apply(self, ws: Workspace, changes: list[FileChange]) -> tuple[list[FileChange], list[str]]:
        """Apply changes; return (applied, errors). Must be atomic per file and never leave partial edits."""
        ...


# ---------------------------------------------------------------------------
# Knowledge base
# ---------------------------------------------------------------------------
@runtime_checkable
class KnowledgeBaseLike(Protocol):
    def match(self, cluster: ErrorCluster, ws: Workspace, profile: ProjectProfile) -> KBMatch: ...
    def active_recipe_files(self) -> list[Path]: ...
    def active_recipe_ids(self) -> list[str]: ...
    def record_case(self, case: KBCase) -> str: ...
    def credit_recipe(self, recipe_id: str, success: bool, project_fingerprint: str) -> None: ...
    def harvest(self, phase_result: PhaseResult, profile: ProjectProfile, ws: Workspace,
                llm: Optional[LLMClientLike] = None) -> HarvestResult: ...
    def list_recipes(self, status: Optional[str] = None) -> list[KBRecipe]: ...
    def promote(self, recipe_id: str) -> KBRecipe: ...
    def reject(self, recipe_id: str, reason: str = "") -> KBRecipe: ...
    def reindex(self) -> int: ...
