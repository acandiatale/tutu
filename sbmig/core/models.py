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
    # 2026-10-22 (fourth company project): when the attributed artifact is only the javax API jar
    # (`javax.persistence:javax.persistence-api:2.2` — the *type's* jar, not the library that still uses it), the
    # dependency tree says who drags it in: `via` = root → … → API jar coordinates, `via_direct` = the direct
    # dependency of the module on that path (the library to upgrade, e.g. `org.qlrm:qlrm:2.1.1`)
    via: list[str] = Field(default_factory=list)
    via_direct: Optional[GAV] = None


# ---------------------------------------------------------------------------
# Maven / project description
# ---------------------------------------------------------------------------
class JavaxApiJar(BaseModel):
    """A javax EE API jar (or a javax-era `jakarta.*-api` 2.x) still on a module's resolved classpath after the
    migration, with the dependency path that brings it (`mvn dependency:tree`). Rendered as the report section
    "javax API jars still on the classpath" (report.javax_api_inventory, 2026-10-22)."""
    artifact: GAV                                               # the API jar, e.g. javax.persistence:javax.persistence-api:2.2
    module: Optional[str] = None                                # module dir ("" / None = root)
    scope: Optional[str] = None                                 # compile | provided | runtime | test
    path: list[str] = Field(default_factory=list)               # root → … → API jar (coordinates as printed by the tree)
    via: Optional[GAV] = None                                   # direct dependency of the module on that path
    declared: bool = False                                      # the API jar itself is declared in the module pom


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
    repositories: list[str] = Field(default_factory=list)   # <repositories> / <pluginRepositories> URLs declared in
                                                           # this pom (profiles included), properties interpolated
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
    # round 9c (front module): as-is web resources — JSP files and directories holding static web files
    # (`src/main/webapp` minus WEB-INF, `src/main/resources/{static,public,templates}`); root-relative, sorted
    jsp_paths: list[str] = Field(default_factory=list)
    web_static_dirs: list[str] = Field(default_factory=list)
    web_static_count: int = 0
    xml_bean_configs: list[str] = Field(default_factory=list)    # files containing <beans xmlns="http://www.springframework.org/schema/beans"
    config_files: list[str] = Field(default_factory=list)        # application*.yml/properties, bootstrap*, logback*
    spring_boot_test_classes: list[str] = Field(default_factory=list)
    lombok: bool = False


# ---------------------------------------------------------------------------
# Externalized configuration (round 8): settings the code reads that are defined in no packaged resource
# ---------------------------------------------------------------------------
PropertyRefSource = Literal["value-annotation", "configuration-properties", "environment", "system-property",
                            "system-env", "xml-placeholder", "context-param", "jndi", "property-source", "profiles",
                            "config-placeholder"]   # round 9: `${…}` inside the value of a packaged definition file
                                                    # (`spring.datasource.url=${DB_URL}` → DB_URL is read at runtime)
PropertyStatus = Literal["defined", "defaulted", "external"]


class PropertyRef(BaseModel):
    """One place where the as-is code / XML reads an externalized setting."""
    key: str                                       # the key as written: "db.url", "java:comp/env/jdbc/x", "app.*" (prefix)
    source: PropertyRefSource
    file: str                                      # repo-relative path at analysis time
    line: Optional[int] = None
    default: Optional[str] = None                  # `${key:default}` / System.getProperty(k, default) → "default"
    scope: Literal["main", "test"] = "main"
    raw: str = ""                                  # matched text, sanitized, ≤ 200 chars


class PropertyFinding(BaseModel):
    """One key (or prefix) with its verdict. `boot_key` is what sbmig writes into application.properties when it
    differs from `key` (JNDI DataSource → spring.datasource.*, context-param → server.servlet.context-parameters.*)."""
    key: str
    status: PropertyStatus                         # defined in a packaged resource | code default only | nowhere
    refs: list[PropertyRef] = Field(default_factory=list)
    defined_in: list[str] = Field(default_factory=list)     # packaged files (profile files included) defining it
    boot_keys: list[str] = Field(default_factory=list)      # keys to write for this finding (default: [key])
    supplied_by: Optional[str] = None              # as-is supply route when detectable: "jndi" | "context-param" |
                                                   # "system-property" | "property-source:file" | None (unknown)
    note: str = ""                                 # rule note from knowledge/config/externalized.yml
    service: Optional[str] = None                  # MSA (round 9): the deployable whose code reads the key and whose
                                                   # packaged resources were checked; None = shared code outside every
                                                   # service (checked against every service's resources)


class PropertyInventory(BaseModel):
    """ProjectProfile.properties (analysis) and RunReport.properties (after restructure wrote the placeholders)."""
    findings: list[PropertyFinding] = Field(default_factory=list)
    packaged_files: list[str] = Field(default_factory=list)     # resource files scanned for definitions
    profiles: list[str] = Field(default_factory=list)           # application-<p>.* names + spring.profiles.active refs
    filtered_resources: list[str] = Field(default_factory=list) # Maven <filtering>true</filtering> resource dirs (pom props)
    filtered_binary_files: list[str] = Field(default_factory=list)  # binary files (webfonts, keystores, archives, …:
                                                   # analyze.properties.BINARY_RESOURCE_EXTENSIONS) a filtered resource
                                                   # would run through the filter (includes / excludes honoured; capped)
                                                   # → pre-emptive handler `resources-binary-filtering`
    placeholder_files: list[str] = Field(default_factory=list)  # files that received the sbmig placeholder block
    placeholders_written: list[str] = Field(default_factory=list)   # keys written with the sentinel
    notes: list[str] = Field(default_factory=list)
    services: list[str] = Field(default_factory=list)   # MSA (round 9): deployables scanned per service (findings carry
                                                        # `service`); empty = project-wide inventory

    @property
    def external(self) -> list[PropertyFinding]:
        return [f for f in self.findings if f.status == "external"]


class LibraryUsage(BaseModel):
    name: str                       # normalized: lombok, mapstruct, querydsl, spring-security, springdoc, swagger2,
                                    # mybatis, jpa, hibernate, feign, kafka, redis, jackson, junit4, junit5, mockito, ...
    version: Optional[str] = None
    evidence: list[str] = Field(default_factory=list)   # "pom:org.projectlombok:lombok", "import:javax.persistence"


class ServiceInfo(BaseModel):
    """One deployable unit inside an MSA repo — or (kind="library") a shared library root that must be migrated too."""
    name: str
    root_pom: str                   # relative path to the service's top pom
    case_type: CaseType
    kind: Literal["service", "library"] = "service"
    poms: list[str] = Field(default_factory=list)
    main_classes: list[str] = Field(default_factory=list)
    spring_boot_version: Optional[str] = None
    java_version: Optional[str] = None


class ProjectProfile(BaseModel):
    # NOTE round 9c: `boot_modules` (below) lists the module dirs that are boot apps by the company naming rule
    # (`layout.boot_module_pattern`, e.g. `*-boot`) confirmed by their pom (spring-boot-maven-plugin or a
    # @SpringBootApplication class); Boot as-is / MSA pom-only edits give those `boot_parent`, the rest `comp_parent`.
    root: str                                   # absolute path of project root at analysis time
    name: str
    case_type: CaseType
    confidence: float = 1.0
    reasons: list[str] = Field(default_factory=list)
    build_tool: Literal["maven", "gradle", "ant", "none"] = "maven"   # != maven → the run aborts at intake
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
    properties: PropertyInventory = Field(default_factory=PropertyInventory)   # externalized-config inventory (round 8)
    boot_modules: list[str] = Field(default_factory=list)      # round 9c, see the class note

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
    llm_usage: "LLMUsage" = Field(default_factory=lambda: LLMUsage())   # plan advisor (recipe model) usage


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
    # files the rewrite plugin could not parse (`There were problems parsing …` lines; 2026-10-22): recipes that
    # need types (Spring Data, Hibernate, method renames) silently skip them — the source handlers cover the rest
    parse_problems: list[str] = Field(default_factory=list)


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


class ModelCost(BaseModel):
    """LLM usage of one model in a run, priced with `llm.pricing` (currency per 1M tokens) when a price is known."""
    model: str
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    price_key: str = ""                            # the llm.pricing entry that matched ("" = unpriced)
    input_per_mtok: Optional[float] = None
    output_per_mtok: Optional[float] = None
    cost: Optional[float] = None                   # None = no price configured for this model


class LLMCost(BaseModel):
    """Priced LLM usage of a run (`RunReport.llm_cost`): per model + total. Models without a price are listed in
    `unpriced_models` and left out of `total`; `usage_missing` = calls were made but no token counts came back."""
    currency: str = "USD"
    total: float = 0.0
    by_model: list[ModelCost] = Field(default_factory=list)
    unpriced_models: list[str] = Field(default_factory=list)
    usage_missing: bool = False
    note: str = ""                                 # llm.pricing_note: where the prices come from / as of when


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
    advisory_ids: list[str] = Field(default_factory=list)   # advisories whose hints were given to this attempt (round 6)
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
# How a fix changed the code — deterministic labels derived from the FileChange set + the error (sbmig.kb.labels).
# "symbol X → label Y (old → new)" is the unit of cross-project learning; mechanical labels compile to recipes.
FixLabel = Literal[
    "import-rename",          # only import lines changed: old FQCN → new FQCN            → ChangeType
    "type-rename",            # a type reference renamed in code (+imports)               → ChangeType
    "package-rename",         # whole package prefix moved                                → ChangePackage
    "annotation-replace",     # @Old → @New (possibly different attributes)               → ChangeType when 1:1
    "method-rename",          # same receiver/args, different method name                 → ChangeMethodName
    "method-signature-adapt", # call site adapted to a changed signature / builder / lambda DSL
    "override-signature",     # overriding method signature changed to match the new supertype
    "dependency-add",         # pom: <dependency> added                                   → AddDependency
    "dependency-replace",     # pom: g:a swapped                                          → ChangeDependency
    "dependency-remove",      # pom: <dependency> removed                                 → RemoveDependency
    "dependency-version",     # pom: version bumped                                       → UpgradeDependencyVersion
    "plugin-version",         # pom: plugin version bumped                                → UpgradePluginVersion
    "pom-property",           # pom: <properties> value changed/added/removed             → ChangePropertyValue
    "spring-property-rename", # application.properties/yml key renamed                    → ChangeSpringPropertyKey
    "config-value",           # a configuration value changed (not a key rename)
    "remove-code",            # dead/obsolete code or annotation removed
    "add-code",               # new method/field/class added
    "generic-rewrite",        # anything else (logic change)
]


class ResolutionRecord(BaseModel):
    """One line of kb/ledger.jsonl: in project P, error-case S was resolved (or blocked) by R. The cross-project
    memory a fleet run leaves behind, queryable with `sbmig kb ledger`."""
    id: str
    at: datetime = Field(default_factory=utcnow)
    project: str                                   # ws.root.name (no paths)
    project_fingerprint: str = ""
    run_id: str = ""
    case_type: CaseType = CaseType.UNKNOWN
    phase: Phase = Phase.SPRING
    signature: str = ""                            # L1
    signature_generic: str = ""                    # L2
    error_kind: ErrorKind = ErrorKind.COMPILE
    symbol: str = ""                               # javac symbol / artifact key / owner class — the "error-case symbol"
    resolution: Literal["handler", "plan_recipe", "kb_recipe", "kb_patch", "llm_patch", "optional_bump",
                        "blocked", "unresolved", "advisory"] = "llm_patch"   # advisory: library advisory finding
                                                   # (symbol = library, resolver_id = "<policy>:<recipes|->",
                                                   #  replacement = "lib → replacement", outcome = AdvisoryOutcome)
    resolver_id: str = ""                          # handler id / recipe id / kb case id / block reason
    fix_labels: list[str] = Field(default_factory=list)      # FixLabel values
    replacement: str = ""                          # "old → new" when the label implies one (FQCN, g:a, key …)
    outcome: str = ""                              # improved | build_ok | blocked | unresolved
    files: list[str] = Field(default_factory=list)           # basenames only
    case_id: Optional[str] = None                  # KBCase created/reused
    recipe_id: Optional[str] = None                # KB recipe used or generated
    model: str = ""
    notes: list[str] = Field(default_factory=list)


class KBCase(BaseModel):
    id: str
    created_at: datetime = Field(default_factory=utcnow)
    project_fingerprint: str = ""
    project: str = ""                              # ws.root.name — for the ledger / fleet learning report
    symbol: str = ""                               # error-case symbol (see ResolutionRecord.symbol)
    symbol_owner: str = ""                         # FQCN of the type that owns `symbol` (javac prints imported types by
                                                   # their simple name; harvest resolves it from the changed files'
                                                   # imports / classpath) — ChangeMethodName needs it (round 5)
    fix_labels: list[str] = Field(default_factory=list)      # FixLabel values (deterministic, sbmig.kb.labels)
    replacement: str = ""                          # "old → new" when the labels imply one
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
    recipes_promoted: list[str] = Field(default_factory=list)      # auto-promoted during this harvest
    ledger_entries: int = 0                                        # ResolutionRecords appended
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Fleet (sbmig fleet: one agent, many projects, sequential)
# ---------------------------------------------------------------------------
FleetStatus = Literal["pending", "running", "success", "incomplete", "aborted", "skipped"]


class FleetProjectResult(BaseModel):
    name: str
    input: str                                     # path as given in the manifest
    status: FleetStatus = "pending"
    run_id: Optional[str] = None
    out_dir: Optional[str] = None
    case_type: CaseType = CaseType.UNKNOWN
    exit_code: Optional[int] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    duration_s: float = 0.0
    llm_calls: int = 0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_cost: Optional[float] = None               # RunReport.llm_cost.total (llm.pricing currency), None = unpriced
    blocked: int = 0
    blocked_reasons: list[str] = Field(default_factory=list)   # "external_javax_api:com.company:legacy-web"
    unresolved: int = 0
    flags: list[str] = Field(default_factory=list)
    kb_cases_created: list[str] = Field(default_factory=list)
    kb_candidates_created: list[str] = Field(default_factory=list)
    kb_recipes_promoted: list[str] = Field(default_factory=list)
    kb_recipes_used: list[str] = Field(default_factory=list)      # KB recipes/patches that fired (learning paid off)
    advisories: list[str] = Field(default_factory=list)           # "<advisory_id>:<outcome>" (library advisories)
    carried_over: int = 0                                         # dependencies pinned at as-is versions (handover)
    error: Optional[str] = None
    attempt: int = 1


class FleetState(BaseModel):
    """fleet/state.json — resumable; rewritten atomically after every project."""
    fleet_id: str
    manifest: str
    started_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    finished_at: Optional[datetime] = None
    projects: list[FleetProjectResult] = Field(default_factory=list)
    llm_calls_total: int = 0
    llm_cost_total: float = 0.0
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Library advisories (knowledge/libraries/advisories.yml → sbmig.advisory) and the handover summary
# ---------------------------------------------------------------------------
# Policy: there is deliberately NO "block" policy — a library that is EOL / no longer managed never fails the run.
#   guide  = build as far as we can, tell the owners what to do next (report "Handover" + manual follow-ups + ledger)
#   recipe = additionally run verified OpenRewrite recipe(s) in the spring phase; falls back to guide when the
#            recipe is unavailable (not in the catalog / artifact not configured / advisories.allow_recipes=false)
AdvisoryPolicy = Literal["guide", "recipe"]
AdvisoryStatus = Literal["eol", "unmanaged", "incompatible", "superseded"]
#   eol          = project discontinued (springfox, hystrix, sleuth, ribbon, zuul …)
#   unmanaged    = still alive but the target parent / Boot BOM no longer manages its version
#   incompatible = known not to work on JDK 21 / Boot 4 at runtime even though it compiles
#   superseded   = works, but a replacement is the company/upstream standard (old release train, httpclient 4 …)
AdvisoryOutcome = Literal["replaced", "still-present", "unknown"]
#   replaced      = after the run the library is no longer detected (or its version reached resolved_min_version)
#   still-present = the library is still declared/used; the owners get the follow-up
#   unknown       = verification impossible (dry-run, aborted before analyze, re-scan failed)


class LibraryAdvisory(BaseModel):
    """One entry of knowledge/libraries/advisories.yml (company additions: advisories.extra_files, merged by id)."""
    id: str                                        # "springfox"
    library: Optional[str] = None                  # LibraryUsage.name the entry keys on (sbmig.analyze.libraries rule)
    artifacts: list[str] = Field(default_factory=list)   # extra "group:artifact-glob" pom patterns (direct or managed)
    status: AdvisoryStatus = "eol"
    title: str                                     # "Springfox (Swagger 2) is discontinued"
    summary: str = ""                              # one paragraph for the report
    replacement: Optional[str] = None              # "org.springdoc:springdoc-openapi-starter-webmvc-ui" or free text
    replacement_version: Optional[str] = None      # "3.x (Boot 4)"
    policy: AdvisoryPolicy = "guide"
    recipes: list[str] = Field(default_factory=list)     # OpenRewrite recipe ids (must exist in the recipe catalog)
    followup: str = ""                             # the "from here on, your team …" sentence (runtime work)
    docs: list[str] = Field(default_factory=list)
    applies_to: list[CaseType] = Field(default_factory=list)   # empty = every case type
    applies_when_version_below: Optional[str] = None   # only when the detected version is below this (unparsable
                                                   # versions such as release-train names count as "below")
    resolved_min_version: Optional[str] = None     # outcome "replaced" when the library is still present but at
                                                   # least this version (release-train bumps)
    notes: Optional[str] = None                    # knowledge/notes/*.md pointer


class AdvisoryFinding(BaseModel):
    """A LibraryAdvisory that matched this project. Created after analyze, enriched by the planner (recipes) and
    verified at finalize (outcome). Serialized in report.json (`advisories`) and the ledger."""
    advisory_id: str
    library: str                                   # library name (or advisory id when artifact-matched only)
    status: AdvisoryStatus
    title: str
    summary: str = ""
    policy: AdvisoryPolicy                         # effective policy after config overrides / recipe availability
    policy_reason: str = ""                        # "catalog" | "config override" | "recipe unavailable → guide" …
    detected_version: Optional[str] = None
    evidence: list[str] = Field(default_factory=list)     # LibraryUsage evidence strings
    artifacts: list[str] = Field(default_factory=list)    # "g:a" keys found in the as-is poms
    recipes: list[str] = Field(default_factory=list)      # recipe ids that will run for it
    recipe_source: Literal["plan", "advisory", "none"] = "none"   # plan = already inside the planned composites,
                                                   # advisory = added to spring.rewrite because of this finding
    outcome: AdvisoryOutcome = "unknown"
    replacement: Optional[str] = None
    replacement_version: Optional[str] = None
    followup: str = ""
    docs: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    llm_fixed_files: list[str] = Field(default_factory=list)   # files the LLM rewrote for this library (round 6:
                                                   # successful llm_fix attempts whose advisory_ids include this id)
    llm_fix_notes: list[str] = Field(default_factory=list)     # one line per such attempt (its analysis), for the Handover

    @property
    def replaced(self) -> bool:
        return self.outcome == "replaced"


class MergeConflict(BaseModel):
    """`layout.multi_module_policy: merge`: one path that existed in more than one as-is module (or the root `src/`).
    The copy of the first module in the root `<modules>` order is kept in comp-jar; the others are dropped from the
    tree but saved under the run directory and listed in the Handover ("Merge conflicts")."""
    path: str                                      # comp-jar-relative, e.g. "src/main/resources/messages.properties"
    kept_from: str                                 # module label that won ("<root>" for the root src/)
    dropped_from: list[str] = Field(default_factory=list)   # module labels whose copy was dropped
    identical: bool = False                        # every copy had the same bytes (nothing lost)
    saved_copies: list[str] = Field(default_factory=list)   # run-dir-relative paths of the dropped copies
                                                   # ("merge-conflicts/<module>/src/...", empty when identical)
    union: bool = False                            # round 9: line-list registry (META-INF/services/*, spring.factories,
                                                   # META-INF/spring/*.imports) → the kept file is the union of every
                                                   # copy's lines (nothing dropped; saved_copies keep the originals)


class VersionChange(BaseModel):
    """One row of the handover's 'versions replaced' table: as-is vs final pom(s), whole project, keyed by g:a."""
    kind: Literal["parent", "dependency", "bom", "plugin", "property"] = "dependency"
    key: str                                       # "g:a" (parent / dependency / bom / plugin) or property name
    before: Optional[str] = None                   # as-is version: declared, else the as-is effective (managed) one
    after: Optional[str] = None                    # final version: declared, else None (managed by the new parent/BOM)
    before_source: Literal["declared", "managed", "absent"] = "declared"
    after_source: Literal["declared", "managed", "removed"] = "declared"
    reason: str = ""                               # "parent swap" | "recipe" | "optional bump" | "mandatory" |
                                                   # "carried over (no longer managed)" | "advisory:<id>" | ""
    carried_over: bool = False                     # pinned at the as-is effective version because the new parent /
                                                   # Boot BOM does not manage it — the owners must confirm or replace
    pom: str = "pom.xml"                           # final pom that declares it (relative)


class HandoverSummary(BaseModel):
    """'Here is what we replaced, from here on it is yours' — compressed for the owning team. Built at finalize
    from profile.poms (as-is), the as-is effective dependencyManagement and the final poms."""
    parent_before: Optional[str] = None            # "g:a:v"
    parent_after: Optional[str] = None
    java_before: Optional[str] = None
    java_after: Optional[str] = None
    spring_boot_before: Optional[str] = None
    spring_boot_after: Optional[str] = None
    changes: list[VersionChange] = Field(default_factory=list)   # version changed (incl. parent/bom), sorted by key
    added: list[str] = Field(default_factory=list)               # "g:a[:v]" new in the final poms (mandatory, replacements)
    removed: list[str] = Field(default_factory=list)             # "g:a[:v]" gone from the final poms
    carried_over: list[VersionChange] = Field(default_factory=list)   # subset of changes with carried_over=True
    from_here: list[str] = Field(default_factory=list)           # ordered "your team" list: advisories not replaced,
                                                                 # replaced-but-verify, carried-over pins, blocked /
                                                                 # unresolved pointers, manual follow-ups (deduped)
    notes: list[str] = Field(default_factory=list)               # e.g. "effective dependencyManagement unavailable"
    merge_conflicts: list[str] = Field(default_factory=list)     # rendered lines, one per RunReport.merge_conflicts
                                                                 # entry whose copies differed (identical ones are
                                                                 # summarized in one line)
    properties_to_set: list[str] = Field(default_factory=list)   # rendered rows: one per external key (round 8)
    properties_guide: list[str] = Field(default_factory=list)    # how to supply values (knowledge/config/externalized.yml)


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
    llm_usage_by_model: list[LLMUsage] = Field(default_factory=list)   # fix / escalation / recipe models apart
    llm_cost: Optional[LLMCost] = None                                  # priced with llm.pricing (2026-10-22)
    harvest: Optional[HarvestResult] = None
    unresolved: list[BuildError] = Field(default_factory=list)
    blocked: list[BlockedItem] = Field(default_factory=list)       # deliberately skipped: needs library/infra/human action
    manual_followups: list[str] = Field(default_factory=list)    # things the as-is owner must do (runtime concerns)
    advisories: list[AdvisoryFinding] = Field(default_factory=list)   # library advisories (EOL / unmanaged libs)
    handover: Optional[HandoverSummary] = None                   # versions replaced + "from here on, your team"
    merge_conflicts: list[MergeConflict] = Field(default_factory=list)   # from the layout restructure (merge policy)
    properties: Optional[PropertyInventory] = None               # profile inventory + placeholders written (round 8)
    flags: list[str] = Field(default_factory=list)               # e.g. "test-compile-skipped"
    javax_api_jars: list[JavaxApiJar] = Field(default_factory=list)   # report.javax_api_inventory (2026-10-22)
    outputs: dict[str, str] = Field(default_factory=dict)        # project_dir, zip, diff, journal, report_md, report_json
    timings_s: dict[str, float] = Field(default_factory=dict)


MigrationPlan.model_rebuild()
