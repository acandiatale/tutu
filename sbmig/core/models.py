"""Shared data models for sbmig.

These are the contracts between workstreams. Change them only with a note in docs/INTERFACES.md.
All paths stored in models are **relative to the project root** (Workspace.root) unless the field name
ends with `_abs` or is a log/output path.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class CaseType(str, Enum):
    PURE_JAVA = "pure_java"            # case 1: in-house pure Java (no Spring, no servlet)
    SPRING_LEGACY = "spring_legacy"    # case 2a: Spring Framework 4/5 without Boot (xml / annotation config)
    SPRING_BOOT = "spring_boot"        # case 2b: Spring Boot 2.x / 3.x
    SERVLET_TOMCAT = "servlet_tomcat"  # case 3: raw servlet / JSP on Tomcat (war, web.xml)
    MSA = "msa"                        # case 4: several deployable services in one repo
    UNKNOWN = "unknown"


class Phase(str, Enum):
    INTAKE = "intake"
    ANALYZE = "analyze"
    PLAN = "plan"
    JDK21 = "jdk21"              # OpenRewrite UpgradeToJava21 + build-fix loop
    SPRING = "spring"            # OpenRewrite jakarta / Spring Boot 4 / Framework 7 + build-fix loop
    RESTRUCTURE = "restructure"  # parent / mandatory / optional / module split (or MSA pom-only) + build-fix loop
    VERIFY = "verify"            # final `mvn clean package`
    REPORT = "report"


class StepKind(str, Enum):
    OPENREWRITE = "openrewrite"
    HANDLER = "handler"            # deterministic pre-emptive fix (e.g. lombok version floor)
    RESTRUCTURE = "restructure"    # non-MSA layout transform
    MSA_POM = "msa_pom"            # MSA: pom-only parent/mandatory/optional edits
    BUILD_FIX_LOOP = "build_fix_loop"
    BUILD = "build"                # single build without fixing (verify)


class ErrorKind(str, Enum):
    COMPILE = "compile"
    TEST_COMPILE = "test_compile"
    DEPENDENCY_RESOLUTION = "dependency_resolution"
    PLUGIN_RESOLUTION = "plugin_resolution"
    PARENT_RESOLUTION = "parent_resolution"
    PLUGIN_EXECUTION = "plugin_execution"   # a plugin ran and failed (not compiler)
    ENFORCER = "enforcer"
    POM_INVALID = "pom_invalid"
    REWRITE = "rewrite"                     # OpenRewrite run failed
    TEST_FAILURE = "test_failure"           # only if tests are executed
    UNKNOWN = "unknown"


class DecisionKind(str, Enum):
    HANDLER = "handler"        # built-in / kb pattern deterministic fix
    KB_RECIPE = "kb_recipe"    # run promoted OpenRewrite recipe(s) from the knowledge base
    KB_PATCH = "kb_patch"      # replay a previously successful search/replace patch
    LLM_FIX = "llm_fix"
    SKIP = "skip"              # masked by another cluster this iteration
    ESCALATE = "escalate"      # give up on this cluster; goes to report
    BLOCKED = "blocked"        # cannot be fixed inside this project (external library / infra) → skip now, report


AttemptOutcome = Literal["pending", "improved", "no_change", "regressed", "reverted", "build_ok", "apply_failed",
                         "blocked"]


class BlockReason(str, Enum):
    EXTERNAL_JAVAX_API = "external_javax_api"            # a dependency still exposes javax.* types in its API
    EXTERNAL_API_INCOMPATIBLE = "external_api_incompatible"  # dependency API changed/removed (non-javax)
    ARTIFACT_NOT_FOUND = "artifact_not_found"            # dependency/plugin not in any repository
    PARENT_NOT_FOUND = "parent_not_found"
    PLUGIN_INCOMPATIBLE = "plugin_incompatible"          # a build plugin fails on JDK 21 and has no known floor
    UNSUPPORTED_SOURCE = "unsupported_source"            # kotlin/groovy/scala/generated code we do not handle
    ENCODING = "encoding"                                # source encoding could not be determined
    BUDGET_EXHAUSTED = "budget_exhausted"
    LLM_UNAVAILABLE = "llm_unavailable"
    REPEATED_FAILURE = "repeated_failure"                # escalated after the allowed attempts
    OSCILLATION = "oscillation"                          # fixes keep toggling between the same error sets


class BlockedItem(BaseModel):
    """One cluster the agent deliberately stopped working on. Rendered as the 'Blocked — needs human/library action'
    section of the report, with the most actionable context we could extract."""
    id: str
    reason: BlockReason
    phase: Phase
    cluster_id: str
    signatures: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    errors: list[BuildError] = Field(default_factory=list)      # sanitized, max ~10
    artifact: Optional[GAV] = None                              # offending library / artifact when attributed
    owner_class: Optional[str] = None                           # library class exposing the incompatible API
    required_type: Optional[str] = None                         # e.g. javax.servlet.http.HttpServletRequest
    found_type: Optional[str] = None                            # e.g. jakarta.servlet.http.HttpServletRequest
    suggestion: str = ""                                        # concrete human action
    attempts: int = 0                                           # fix attempts made before blocking
    first_seen_iteration: int = 0
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Maven / project description
# ---------------------------------------------------------------------------
class GAV(BaseModel):
    group_id: str
    artifact_id: str
    version: Optional[str] = None
    scope: Optional[str] = None
    type: Optional[str] = None
    classifier: Optional[str] = None
    optional: bool = False

    @property
    def key(self) -> str:
        return f"{self.group_id}:{self.artifact_id}"

    @property
    def coordinates(self) -> str:
        return self.key + (f":{self.version}" if self.version else "")

    @classmethod
    def parse(cls, coords: str) -> "GAV":
        parts = coords.split(":")
        if len(parts) < 2:
            raise ValueError(f"bad coordinates: {coords}")
        return cls(group_id=parts[0], artifact_id=parts[1], version=parts[2] if len(parts) > 2 and parts[2] else None)


class PluginInfo(BaseModel):
    group_id: str = "org.apache.maven.plugins"
    artifact_id: str
    version: Optional[str] = None
    configuration: dict[str, Any] = Field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.group_id}:{self.artifact_id}"


class PomInfo(BaseModel):
    path: str                                  # relative to project root, e.g. "pom.xml" or "service-a/pom.xml"
    group_id: Optional[str] = None             # effective (inherited from parent if absent)
    artifact_id: str
    version: Optional[str] = None
    packaging: str = "jar"
    parent: Optional[GAV] = None
    parent_relative_path: Optional[str] = None
    parent_in_repo: bool = False               # parent pom lives inside this repo
    modules: list[str] = Field(default_factory=list)
    properties: dict[str, str] = Field(default_factory=dict)
    dependencies: list[GAV] = Field(default_factory=list)          # versions resolved from properties/depMgmt in repo when possible
    dependency_management: list[GAV] = Field(default_factory=list)
    plugins: list[PluginInfo] = Field(default_factory=list)
    java_version: Optional[str] = None         # from maven.compiler.*/java.version/compiler plugin
    spring_boot_version: Optional[str] = None
    spring_version: Optional[str] = None
    has_spring_boot_plugin: bool = False
    is_root: bool = False                      # no parent within the repo (top of an inheritance chain)
    is_aggregator: bool = False                # packaging pom with modules


class SourceStats(BaseModel):
    java_files: int = 0
    test_files: int = 0
    kotlin_files: int = 0
    packages: list[str] = Field(default_factory=list)
    base_package: Optional[str] = None
    main_classes: list[str] = Field(default_factory=list)        # FQCN with @SpringBootApplication
    psvm_classes: list[str] = Field(default_factory=list)        # FQCN with public static void main (non-boot)
    servlet_classes: list[str] = Field(default_factory=list)     # extends HttpServlet / @WebServlet
    filter_listener_classes: list[str] = Field(default_factory=list)
    controllers: list[str] = Field(default_factory=list)         # @Controller/@RestController
    javax_imports: dict[str, int] = Field(default_factory=dict)  # "javax.servlet" -> count
    jakarta_imports: dict[str, int] = Field(default_factory=dict)
    spring_annotations: dict[str, int] = Field(default_factory=dict)
    has_web_xml: bool = False
    web_xml_paths: list[str] = Field(default_factory=list)
    jsp_count: int = 0
    xml_bean_configs: list[str] = Field(default_factory=list)    # files containing <beans xmlns="http://www.springframework.org/schema/beans"
    config_files: list[str] = Field(default_factory=list)        # application*.yml/properties, bootstrap*, logback*
    spring_boot_test_classes: list[str] = Field(default_factory=list)
    lombok: bool = False


class LibraryUsage(BaseModel):
    name: str                       # normalized: lombok, mapstruct, querydsl, spring-security, springdoc, swagger2,
                                    # mybatis, jpa, hibernate, feign, kafka, redis, jackson, junit4, junit5, mockito, ...
    version: Optional[str] = None
    evidence: list[str] = Field(default_factory=list)   # "pom:org.projectlombok:lombok", "import:javax.persistence"


class ServiceInfo(BaseModel):
    """One deployable unit inside an MSA repo."""
    name: str
    root_pom: str                   # relative path to the service's top pom
    case_type: CaseType
    poms: list[str] = Field(default_factory=list)
    main_classes: list[str] = Field(default_factory=list)
    spring_boot_version: Optional[str] = None
    java_version: Optional[str] = None


class ProjectProfile(BaseModel):
    root: str                                   # absolute path of project root at analysis time
    name: str
    case_type: CaseType
    confidence: float = 1.0
    reasons: list[str] = Field(default_factory=list)
    build_tool: Literal["maven", "gradle", "none"] = "maven"
    poms: list[PomInfo] = Field(default_factory=list)
    root_pom: Optional[PomInfo] = None
    source: SourceStats = Field(default_factory=SourceStats)
    libraries: list[LibraryUsage] = Field(default_factory=list)
    java_version: Optional[str] = None
    spring_boot_version: Optional[str] = None
    spring_version: Optional[str] = None
    packaging: Optional[str] = None
    is_multi_module: bool = False
    services: list[ServiceInfo] = Field(default_factory=list)      # MSA only
    optional_artifacts_in_use: list[GAV] = Field(default_factory=list)  # intersection with config.artifacts.optional
    mandatory_already_present: list[GAV] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    fingerprint: str = ""

    def has_library(self, name: str) -> bool:
        return any(lib.name == name for lib in self.libraries)

    @staticmethod
    def make_fingerprint(pom_paths: list[str], root_artifact_id: str) -> str:
        h = hashlib.sha256()
        h.update(root_artifact_id.encode())
        for p in sorted(pom_paths):
            h.update(b"|" + p.encode())
        return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------
class RecipeRef(BaseModel):
    id: str                                        # fully qualified recipe id
    source: Literal["builtin", "kb", "project"] = "builtin"
    artifact: Optional[str] = None                 # "org.openrewrite.recipe:rewrite-spring" (version from config)
    file: Optional[str] = None                     # declarative yml path for kb/project recipes (absolute)


class PlanStep(BaseModel):
    id: str                                        # "jdk21.rewrite", "spring.rewrite", "spring.loop", "restructure.apply", ...
    kind: StepKind
    phase: Phase
    title: str
    recipes: list[RecipeRef] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
    build_goal: Optional[str] = None               # compile | test-compile | package | verify
    max_iterations: int = 0
    target: Optional[str] = None                   # MSA service name / module path; None = whole project
    reason: str = ""
    optional: bool = False                         # failure does not abort the run


class MigrationPlan(BaseModel):
    profile_fingerprint: str
    case_type: CaseType
    steps: list[PlanStep]
    notes: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# Build / rewrite results
# ---------------------------------------------------------------------------
class BuildError(BaseModel):
    kind: ErrorKind
    message: str                                   # first line, cleaned
    file: Optional[str] = None                     # relative path
    line: Optional[int] = None
    column: Optional[int] = None
    symbol: Optional[str] = None                   # javac "symbol:" continuation
    location: Optional[str] = None                 # javac "location:" continuation
    module: Optional[str] = None                   # maven module artifactId/name
    artifact: Optional[GAV] = None                 # dependency/plugin/parent resolution
    goal: Optional[str] = None                     # "compiler:compile", "compiler:testCompile", ...
    raw: str = ""                                  # the error block as seen in the log
    signature: str = ""                            # L1 specific signature (see sbmig.build.signatures)
    signature_generic: str = ""                    # L2 generic signature

    @property
    def key(self) -> str:
        return f"{self.file or '-'}:{self.line or 0}:{self.signature}"


class BuildResult(BaseModel):
    ok: bool
    goal: str
    command: list[str]
    exit_code: int
    duration_s: float
    log_path: str                                  # absolute
    errors: list[BuildError] = Field(default_factory=list)
    modules_built: list[str] = Field(default_factory=list)
    failed_module: Optional[str] = None
    summary: str = ""                              # last ~40 relevant lines
    timed_out: bool = False

    @property
    def error_count(self) -> int:
        return len(self.errors)

    def signature_multiset(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.errors:
            out[e.signature] = out.get(e.signature, 0) + 1
        return out


class RewriteResult(BaseModel):
    ok: bool
    recipes: list[str]
    changed_files: list[str] = Field(default_factory=list)
    log_path: str = ""
    duration_s: float = 0.0
    error: Optional[str] = None
    command: list[str] = Field(default_factory=list)
    dry_run: bool = False
    recipes_applied: dict[str, list[str]] = Field(default_factory=dict)   # recipe id → files it changed (from 'by:' blocks)
    forked: bool = True                                                     # rewrite:run (forks compile) vs runNoFork


# ---------------------------------------------------------------------------
# Triage / fix
# ---------------------------------------------------------------------------
class ErrorCluster(BaseModel):
    id: str
    kind: ErrorKind
    file: Optional[str] = None
    module: Optional[str] = None
    errors: list[BuildError]
    signatures: list[str] = Field(default_factory=list)
    priority: int = 100                            # lower = handle first

    @property
    def generic_signatures(self) -> list[str]:
        return sorted({e.signature_generic for e in self.errors if e.signature_generic})


class TriageDecision(BaseModel):
    cluster_id: str
    kind: DecisionKind
    handler_id: Optional[str] = None
    recipe_ids: list[str] = Field(default_factory=list)
    kb_case_ids: list[str] = Field(default_factory=list)
    rationale: str = ""


class FileChange(BaseModel):
    """One atomic edit. modify = search/replace (search must match exactly once), create/delete = whole file."""
    path: str
    op: Literal["modify", "create", "delete", "rename"]
    search: Optional[str] = None
    replace: Optional[str] = None
    content: Optional[str] = None
    new_path: Optional[str] = None


class LLMUsage(BaseModel):
    model: str = ""
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, other: "LLMUsage") -> "LLMUsage":
        return LLMUsage(
            model=self.model or other.model,
            calls=self.calls + other.calls,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


class FixProposal(BaseModel):
    changes: list[FileChange]
    analysis: str = ""
    confidence: float = 0.5
    model: str = ""
    prompt_hash: str = ""
    llm_usage: LLMUsage = Field(default_factory=LLMUsage)
    requested_files: list[str] = Field(default_factory=list)   # filled when the LLM asked for more context
    raw_response: str = ""


class FixAttempt(BaseModel):
    id: str
    phase: Phase
    iteration: int
    cluster_id: str
    decision: TriageDecision
    changes: list[FileChange] = Field(default_factory=list)
    applied: bool = False
    apply_errors: list[str] = Field(default_factory=list)
    commit: Optional[str] = None
    model: str = ""
    prompt_hash: str = ""
    llm_usage: LLMUsage = Field(default_factory=LLMUsage)
    errors_before: int = 0
    errors_after: Optional[int] = None
    cluster_signatures: list[str] = Field(default_factory=list)
    cluster_errors: list[BuildError] = Field(default_factory=list)   # the cluster's errors (KB harvest: L2 sigs, sanitized errors)
    cluster_resolved: Optional[bool] = None        # the cluster's signatures disappeared after rebuild
    outcome: AttemptOutcome = "pending"
    analysis: str = ""
    notes: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)


class IterationRecord(BaseModel):
    phase: Phase
    iteration: int
    build_ok: bool
    error_count: int
    signatures: dict[str, int] = Field(default_factory=dict)
    cluster_count: int = 0
    decisions: list[TriageDecision] = Field(default_factory=list)
    attempt_ids: list[str] = Field(default_factory=list)
    stalled: bool = False
    stall_level: int = 0
    timestamp: datetime = Field(default_factory=utcnow)
    build_log: str = ""


class PhaseResult(BaseModel):
    phase: Phase
    step_ids: list[str] = Field(default_factory=list)
    ok: bool = False
    build_ok: bool = False
    iterations: int = 0
    rewrite_results: list[RewriteResult] = Field(default_factory=list)
    attempts: list[FixAttempt] = Field(default_factory=list)
    iteration_records: list[IterationRecord] = Field(default_factory=list)
    remaining_errors: list[BuildError] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: Optional[datetime] = None
    notes: list[str] = Field(default_factory=list)
    skipped: bool = False
    blocked: list[BlockedItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Knowledge base
# ---------------------------------------------------------------------------
class KBCase(BaseModel):
    id: str
    created_at: datetime = Field(default_factory=utcnow)
    project_fingerprint: str = ""
    case_type: CaseType = CaseType.UNKNOWN
    phase: Phase = Phase.SPRING
    signatures: list[str] = Field(default_factory=list)          # L1
    signatures_generic: list[str] = Field(default_factory=list)  # L2
    error_kind: ErrorKind = ErrorKind.COMPILE
    errors: list[BuildError] = Field(default_factory=list)       # sanitized (no abs paths)
    fix_kind: Literal["llm_patch", "handler", "recipe", "manual"] = "llm_patch"
    changes: list[FileChange] = Field(default_factory=list)
    summary: str = ""
    outcome: AttemptOutcome = "improved"
    reuse_count: int = 0
    reuse_projects: list[str] = Field(default_factory=list)
    recipe_candidate_id: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    model: str = ""


class KBRecipe(BaseModel):
    id: str                                        # recipe name inside the yml, e.g. com.company.kb.ReplaceFooWithBar
    status: Literal["candidate", "active", "rejected"] = "candidate"
    path: str                                      # absolute path of the declarative yml
    description: str = ""
    signatures: list[str] = Field(default_factory=list)          # L1/L2 signatures that trigger it
    source_case_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    promoted_at: Optional[datetime] = None
    success_count: int = 0
    fail_count: int = 0
    projects: list[str] = Field(default_factory=list)


class KBMatch(BaseModel):
    recipe_ids: list[str] = Field(default_factory=list)              # ACTIVE recipes whose signatures match
    recipe_files: list[str] = Field(default_factory=list)
    candidate_recipe_ids: list[str] = Field(default_factory=list)    # CANDIDATE recipes (tried after active, before LLM)
    candidate_recipe_files: list[str] = Field(default_factory=list)
    cases: list[KBCase] = Field(default_factory=list)            # similar cases → prompt hints
    replayable: list[KBCase] = Field(default_factory=list)       # same signature AND search text still present
    handler_ids: list[str] = Field(default_factory=list)
    hints: list[str] = Field(default_factory=list)               # short natural-language hints for the LLM prompt


class HarvestResult(BaseModel):
    cases_created: list[str] = Field(default_factory=list)
    candidates_created: list[str] = Field(default_factory=list)
    recipes_credited: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
class RunReport(BaseModel):
    run_id: str
    project_name: str
    case_type: CaseType
    started_at: datetime
    finished_at: Optional[datetime] = None
    success: bool = False                          # final verify build passed
    profile: Optional[ProjectProfile] = None
    plan: Optional[MigrationPlan] = None
    phases: list[PhaseResult] = Field(default_factory=list)
    final_build: Optional[BuildResult] = None
    llm_usage: LLMUsage = Field(default_factory=LLMUsage)
    harvest: Optional[HarvestResult] = None
    unresolved: list[BuildError] = Field(default_factory=list)
    blocked: list[BlockedItem] = Field(default_factory=list)       # deliberately skipped: needs library/infra/human action
    manual_followups: list[str] = Field(default_factory=list)    # things the as-is owner must do (runtime concerns)
    flags: list[str] = Field(default_factory=list)               # e.g. "test-compile-skipped"
    outputs: dict[str, str] = Field(default_factory=dict)        # project_dir, zip, diff, journal, report_md, report_json
    timings_s: dict[str, float] = Field(default_factory=dict)
