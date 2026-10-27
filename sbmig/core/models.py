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
    TRANSFORM = "transform"      # v2 (flow-v2): every OpenRewrite recipe in one run, no build in between
    JDK21 = "jdk21"              # OpenRewrite UpgradeToJava21 + build-fix loop
    SPRING = "spring"            # OpenRewrite jakarta / Spring Boot 4 / Framework 7 + build-fix loop
    RESTRUCTURE = "restructure"  # parent / mandatory / optional / module split (or MSA pom-only) + build-fix loop
    VERIFY = "verify"            # final `mvn clean package`
    REPORT = "report"


class StepKind(str, Enum):
    OPENREWRITE = "openrewrite"
    RESTRUCTURE = "restructure"    # non-MSA layout transform
    MSA_POM = "msa_pom"            # MSA: pom-only parent/mandatory/optional edits
    BUILD_FIX_LOOP = "build_fix_loop"
    BUILD = "build"                # single build without fixing (verify)
    REVIEW = "review"              # v2: LLM structure review of poms / dependencies / modules before the first build


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
    SKIP = "skip"              # masked by another cluster this iteration
    ESCALATE = "escalate"      # give up on this cluster; goes to report
    BLOCKED = "blocked"        # cannot be fixed inside this project (external library / infra) → skip now, report
    SCRIPT = "script"          # v2: run a fix script for the cluster's case (stored version, augmented or new)


AttemptOutcome = Literal["pending", "improved", "no_change", "regressed", "reverted", "build_ok", "apply_failed",
                         "blocked", "partial", "inconclusive"]
# round 10 (2026-10-26): `partial` = some of the cluster's targeted errors are gone, the rest remain, nothing new in the
# touched files (progress: not a failure for limits / KB failure counts, not a success for harvest / recipe credit);
# `inconclusive` = the scoring build could not show the cluster's errors (timed out, failed without parsed errors,
# stopped at an earlier stage that the attempt did not cause) — no KB credit either way, counts as tried


EntryStatus = Literal["open", "pom-verify", "resolved", "blocked", "unresolved", "note"]
# open = still in the last build; pom-verify = a restructure pom correction the next build verifies; note = a review
# warning / informational entry (never fixed)
TrySource = Literal["kb-script", "llm-new", "llm-augment", "llm-edit", "review"]
# kb-script = a stored version run without an LLM call; llm-new = first answer for the case; llm-augment = the LLM
# extended a failed script; llm-edit = the LLM said the fix cannot be a script and gave edits; review = the
# structure review's own fix
TryApplied = Literal["script", "edits", "none"]


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
# ---------------------------------------------------------------------------
# Exploded webapp inventory (`sbmig inventory`, 2026-10-23): a Tomcat directory without a pom — JSPs, Java sources,
# WEB-INF/lib jars, WEB-INF/classes — read only, written as inventory.md / inventory.json; the pom synthesis of the
# next round consumes it. Everything is a file-system fact or an explicitly labelled guess.
# ---------------------------------------------------------------------------
class JarInfo(BaseModel):
    path: str                                   # root-relative
    name: str                                   # file name
    size: int = 0
    sha1: str = ""                              # for a later Nexus / Central lookup
    version_guess: Optional[str] = None         # parsed from the file name (`commons-lang-2.6.jar` → 2.6)
    artifact_guess: Optional[str] = None        # "g:a" from knowledge/libraries/jar_names.yml (None = unknown)
    ignored: bool = False                       # *.jar.old / *.bak / *.jar_ … — backups Tomcat does not load


class WebappRoot(BaseModel):
    dir: str                                    # root-relative directory holding WEB-INF ("" = the project root)
    web_xml: Optional[str] = None               # root-relative path when present
    servlet_spec: Optional[str] = None          # "2.3" (DTD) / "2.4" … "4.0" from the web.xml root element
    servlets: int = 0
    filters: int = 0
    listeners: int = 0
    taglibs: int = 0
    frameworks: list[str] = Field(default_factory=list)   # spring-mvc | struts1 | struts2 | egov | jsf | plain-servlet
    jndi_resources: list[str] = Field(default_factory=list)   # names from META-INF/context.xml + web.xml resource-ref
    jsp_count: int = 0
    jsp_sample: list[str] = Field(default_factory=list)
    static_count: int = 0                       # html / js / css / images outside WEB-INF
    spring_xml: list[str] = Field(default_factory=list)   # WEB-INF/**/*.xml with <beans
    lib_jars: list[JarInfo] = Field(default_factory=list)
    ignored_files: list[str] = Field(default_factory=list)   # backups / non-jar files under WEB-INF/lib
    classes_count: int = 0                      # .class files under WEB-INF/classes
    class_major_min: Optional[int] = None       # class-file major versions (50 = Java 6, 52 = Java 8 …)
    class_major_max: Optional[int] = None
    java_version_from_classes: Optional[str] = None   # "1.6" / "1.8" / "11" … from class_major_max
    classes_without_source: int = 0             # FQCNs under WEB-INF/classes with no .java anywhere
    classes_without_source_sample: list[str] = Field(default_factory=list)


class SourceRoot(BaseModel):
    dir: str                                    # root-relative directory where `package a.b;` matches the path
    java_files: int = 0
    packages: list[str] = Field(default_factory=list)      # top-level packages (a.b)
    encoding: str = "utf-8"                     # majority detected encoding (cp949 / euc_kr / utf-8)
    javax_imports: dict[str, int] = Field(default_factory=dict)      # "javax.servlet" → files
    framework_imports: dict[str, int] = Field(default_factory=dict)  # spring / struts1 / struts2 / ibatis2 / mybatis / hibernate / egov → files
    jdk_internal_imports: int = 0               # sun.* / com.sun.* files
    inside_webapp: Optional[str] = None         # the WebappRoot.dir it sits under (e.g. "app1/WEB-INF/src"), else None


class ModuleProposal(BaseModel):
    """One Maven module the pom synthesis would create (a guess the operator can change)."""
    name: str                                   # artifactId guess (webapp dir name, "app" for the root)
    webapp_dir: str
    source_dirs: list[str] = Field(default_factory=list)
    packaging: str = "war"
    java_version: str = "1.8"                   # from the class files, else the sources, else 1.8
    dependencies_known: int = 0                 # jars with an artifact guess
    dependencies_unknown: int = 0               # jars without one (→ lib/ + system scope / Nexus install)
    notes: list[str] = Field(default_factory=list)


class WebappInventory(BaseModel):
    root: str
    scanned_at: datetime = Field(default_factory=utcnow)
    build_files: list[str] = Field(default_factory=list)   # pom.xml / build.xml / .classpath / .project found anywhere
    webapp_roots: list[WebappRoot] = Field(default_factory=list)
    source_roots: list[SourceRoot] = Field(default_factory=list)
    duplicate_classes: int = 0                  # same FQCN in more than one source root
    duplicate_classes_sample: list[str] = Field(default_factory=list)
    proposals: list[ModuleProposal] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    outputs: dict[str, str] = Field(default_factory=dict)  # inventory_md, inventory_json


# ---------------------------------------------------------------------------
# web.xml (2026-10-23): parsed by sbmig.analyze.webxml (DTD 2.2 / 2.3 and XSD 2.4 … 6.0, namespace-agnostic); the
# restructure renders WebXmlRegistrations (ServletRegistrationBean / FilterRegistrationBean /
# ServletListenerRegistrationBean / ErrorPageRegistrar) from it because neither embedded Tomcat nor the company's
# Boot WAR (web.xml is not packaged) reads the descriptor. Pre-Servlet-3 projects have no @WebServlet annotations,
# so @ServletComponentScan alone would lose every mapping.
# ---------------------------------------------------------------------------
class WebXmlServlet(BaseModel):
    name: str
    servlet_class: Optional[str] = None            # FQCN; None for a jsp-file servlet
    jsp_file: Optional[str] = None
    url_patterns: list[str] = Field(default_factory=list)
    init_params: dict[str, str] = Field(default_factory=dict)
    load_on_startup: Optional[int] = None
    async_supported: bool = False
    multipart_location: Optional[str] = None       # <multipart-config> present → location / sizes
    multipart_max_file_size: Optional[int] = None
    multipart_max_request_size: Optional[int] = None
    multipart_file_size_threshold: Optional[int] = None


class WebXmlFilter(BaseModel):
    name: str
    filter_class: str
    url_patterns: list[str] = Field(default_factory=list)
    servlet_names: list[str] = Field(default_factory=list)
    dispatchers: list[str] = Field(default_factory=list)   # REQUEST | FORWARD | INCLUDE | ERROR | ASYNC
    init_params: dict[str, str] = Field(default_factory=dict)
    async_supported: bool = False
    order: int = 0                                 # position of its first <filter-mapping> (web.xml chain order)


class WebXmlErrorPage(BaseModel):
    location: str
    error_code: Optional[int] = None
    exception_type: Optional[str] = None           # FQCN; both None = default error page


class WebXmlModel(BaseModel):
    path: str                                      # root-relative web.xml
    spec: Optional[str] = None                     # "2.3" / "3.1" …
    display_name: Optional[str] = None
    servlets: list[WebXmlServlet] = Field(default_factory=list)
    filters: list[WebXmlFilter] = Field(default_factory=list)
    listeners: list[str] = Field(default_factory=list)          # FQCNs in declaration order
    session_timeout_minutes: Optional[int] = None
    welcome_files: list[str] = Field(default_factory=list)
    error_pages: list[WebXmlErrorPage] = Field(default_factory=list)
    mime_mappings: dict[str, str] = Field(default_factory=dict)  # extension → type
    context_params: dict[str, str] = Field(default_factory=dict) # informational (properties handles them)
    security_constraints: int = 0
    login_config: Optional[str] = None             # auth-method
    resource_refs: list[str] = Field(default_factory=list)
    taglibs: int = 0
    distributable: bool = False
    warnings: list[str] = Field(default_factory=list)   # unparsable bits


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
    # 2026-10-23 (plain servlet / JSP projects with a pom): what the first web.xml declares, parsed once by
    # sbmig.analyze.webxml; the restructure turns it into WebXmlRegistrations (layout.web_xml_registrations)
    web_xml_spec: Optional[str] = None                           # "2.3" (DTD) … "6.0"; None = no web.xml / unknown
    web_xml_servlets: int = 0
    web_xml_filters: int = 0
    web_xml_listeners: int = 0
    jsp_javax_files: int = 0                                     # JSP / tag files whose page imports or scriptlets name javax EE types
    # 2026-10-23 (non-standard directories): root-relative web dirs of the poms — the default `src/main/webapp` when it
    # exists plus every maven-war-plugin `<warSourceDirectory>` (web root, e.g. `STATIC`) and `<webResources>` directory
    # (extra web content); kinds are not recorded here, the restructure re-reads the pom (`source_scanner.pom_web_dirs`).
    # jsp_paths / web_static_dirs / web_static_count include these roots.
    webapp_dirs: list[str] = Field(default_factory=list)
    # files ≥ 5 MB (source_scanner.LARGE_MEDIA_BYTES) with a media / archive extension below a web dir or a classpath
    # resource dir (root-relative, sorted, capped at 20) and the total bytes of all of them (restructure note)
    large_media_files: list[str] = Field(default_factory=list)
    large_media_bytes: int = 0


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
    source: Literal["builtin"] = "builtin"         # v2: OpenRewrite recipes only (no project / KB recipes)
    artifact: Optional[str] = None                 # "org.openrewrite.recipe:rewrite-spring" (version from config)


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
    case_id: Optional[str] = None                  # v2: the cluster is ONE case (L2 signature) across files —
                                                   # CatalogCase id = sha1(signature_generic)[:10]; `file` is then None

    @property
    def generic_signatures(self) -> list[str]:
        return sorted({e.signature_generic for e in self.errors if e.signature_generic})


class TriageDecision(BaseModel):
    cluster_id: str
    kind: DecisionKind
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


# ---------------------------------------------------------------------------
# v2 fix scripts (flow-v2, docs/v2/PLAN-v2.md §3.2): every build-error fix is a Python script `def fix(p)` written by
# the LLM, checked (AST) and run in a separate `python -I` process against a restricted helper API (sbmig.script),
# stored per case in <kb>/scripts/<case_id>/v<N>.py + v<N>.meta.json (sbmig.kb.scripts)
# ---------------------------------------------------------------------------
ScriptMode = Literal["new", "reuse", "augment"]
ScriptRunStatus = Literal["ok", "error", "timeout", "rejected"]
# ok = fix(p) returned; error = it raised (or a helper refused a path); timeout = killed after script.timeout_seconds;
# rejected = the static check refused the code before it ran (forbidden import / name / dunder access, no fix(p))


class ScriptRunResult(BaseModel):
    """What one run of a fix script did (sbmig.script.ScriptRunner.run). The exact change is the git diff the loop
    records; this is the script's own account."""
    status: ScriptRunStatus = "ok"
    changed_files: list[str] = Field(default_factory=list)    # relative posix paths written / created / deleted / moved
    log: list[str] = Field(default_factory=list)              # p.log(...) lines (capped by script.log_lines)
    error: str = ""                                           # exception / refusal / timeout text ("" when ok)
    problems: list[str] = Field(default_factory=list)         # static-check findings when rejected
    duration_s: float = 0.0


class ScriptMeta(BaseModel):
    """<kb>/scripts/<case_id>/v<N>.meta.json — one stored version of a case's fix script and its record."""
    case_id: str
    signature_generic: str = ""                    # the case's L2 signature
    version: int                                   # 1, 2, … per case
    parent: Optional[int] = None                   # the version it augments (lineage); None = written from scratch
    schema_version: int = 3
    sha1: str = ""                                 # of the code (an identical script is stored once)
    created_at: datetime = Field(default_factory=utcnow)
    run_id: str = ""
    project: str = ""
    model: str = ""
    source: Literal["llm", "human", "review"] = "llm"
    analysis: str = ""                             # the LLM's <analysis> (redacted)
    successes: int = 0                             # runs that resolved every targeted error of the case
    failures: int = 0                              # no_change / regressed / apply_failed / script error
    partials: int = 0                              # some targeted errors gone, the rest left
    success_projects: list[str] = Field(default_factory=list)
    last_outcome: str = ""                         # AttemptOutcome of the last run
    last_at: Optional[datetime] = None
    last_run_id: str = ""
    status: Literal["active", "retired", "quarantined"] = "active"   # retired = by a person; quarantined = the code
                                                   # matched a secret pattern (stored masked, never selected)
    notes: list[str] = Field(default_factory=list)


class ScriptBase(BaseModel):
    """The script an augment request extends (FixContext.script_base): the previous version and what it did here."""
    case_id: str
    version: int = 0                               # 0 = an unstored script of this run
    code: str
    outcome: str = ""                              # AttemptOutcome of its run in this project ("" = not run here)
    status: str = ""                               # ScriptRunStatus of that run
    error: str = ""
    log: list[str] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    remaining_errors: list[str] = Field(default_factory=list)    # "file:line message" of the case still present
    introduced_errors: list[str] = Field(default_factory=list)   # "file:line message" new in the files it touched
    history: list[str] = Field(default_factory=list)             # earlier versions: "v1: 2 ok / 1 failed — <failure>"


EditOp = Literal["replace", "write", "create", "delete", "move", "pom"]


class Edit(BaseModel):
    """One direct modification the LLM asked for (`<edits>` JSON, v2.1): applied by sbmig through the script helper
    API (project folder only, same guard as a fix script) — "LLM 이 직접 수정". `pom` edits carry the editor call in
    `pom` (`{"add_dependency": {"group_id": …, "artifact_id": …, "version": null}}`, `set_property`, …)."""
    op: EditOp
    path: str
    old: str = ""                                  # replace: exact text to replace (count = -1 → every occurrence)
    new: str = ""
    count: int = -1
    content: str = ""                              # write / create
    to: str = ""                                   # move
    pom: dict[str, Any] = Field(default_factory=dict)


class ScriptProposal(BaseModel):
    """The fixer's answer (FixerLike.propose_script). v2.1 (2026-10-27): 원인 `analysis` · 해결 방법 `solution` ·
    수정 `edits` (applied directly) · 스크립트 `code` (stored for reuse); `no_script_reason` when the LLM says the
    fix cannot be a reusable script (then `edits` is the fix)."""
    code: str = ""                                 # the <script> body (must define `def fix(p)`); "" = no script
    analysis: str = ""
    solution: str = ""
    edits: list[Edit] = Field(default_factory=list)
    no_script_reason: str = ""
    model: str = ""
    prompt_hash: str = ""
    llm_usage: LLMUsage = Field(default_factory=LLMUsage)
    requested_files: list[str] = Field(default_factory=list)
    raw_response: str = ""
    error: str = ""                                # why there is no script (unparsable answer, transport, refusal)


FixAnswer = ScriptProposal                      # the v2.1 name


# ---------------------------------------------------------------------------
# v2.1 error list (2026-10-27, docs/v2/PLAN-v2.1.md §3.3): ONE record per build error / pom correction of a run —
# <run dir>/errors.jsonl, appended to <kb>/errors.jsonl at the end; what it was, every try with the LLM's answer and
# the applied diff, how it ended. Replaces ErrorCaseRecord / FixAttemptRecord / the case catalog / reviews.jsonl.
# ---------------------------------------------------------------------------


class FixTry(BaseModel):
    n: int                                         # 1, 2, … within the entry's case and run
    round: int = 0                                 # fix round (0 = review)
    source: TrySource
    script_version: int = 0                        # stored version run / created (0 = none)
    analysis: str = ""                             # 원인
    solution: str = ""                             # 해결 방법
    no_script_reason: str = ""
    applied: TryApplied = "none"
    files: list[str] = Field(default_factory=list)
    diff: str = ""                                 # unified diff of what was applied (capped by errors.diff_chars)
    commit: str = ""
    result: AttemptOutcome = "pending"
    note: str = ""                                 # failure text, rollback, script error …
    llm_calls: int = 0
    seconds: float = 0.0


class ErrorEntry(BaseModel):
    id: str                                        # sha1(run_id | case_id | file | line | message)[:12]
    run_id: str = ""
    project: str = ""
    case_id: str = ""                              # sha1(l2)[:10] — the KB case (cases/<case_id>/)
    l2: str = ""                                   # signature_generic
    kind: ErrorKind = ErrorKind.UNKNOWN
    file: Optional[str] = None
    line: Optional[int] = None
    module: Optional[str] = None
    message: str = ""
    raw: str = ""                                  # ≤ 600 chars
    seen: str = ""                                 # compile | restructure | review | build | fix r3
    status: EntryStatus = "open"
    tries: list[FixTry] = Field(default_factory=list)
    resolved: str = ""                             # "r3 try 2" | "build" (gone without a try) | "review"
    script: str = ""                               # cases/<case_id>/fix_v2.py — the script that resolved it
    note: str = ""                                 # pom-verify: what the restructure did; note entries: the text
    at: datetime = Field(default_factory=utcnow)


CaseScriptStatus = Literal["unverified", "proven", "retired"]
# unverified = stored from an answer whose edits fixed this project (or run here once); proven = resolved the case in
# another project on reuse; retired = a person switched it off (`sbmig kb cases retire`)


class CaseScript(BaseModel):
    version: int
    file: str                                      # fix_v<N>.py (relative to the case folder)
    status: CaseScriptStatus = "unverified"
    source: TrySource = "llm-new"
    created_by: str = ""                           # run_id
    project: str = ""
    successes: int = 0
    failures: int = 0
    projects: list[str] = Field(default_factory=list)    # distinct projects it resolved the case in
    last_result: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class CaseInfo(BaseModel):
    """<kb>/cases/<case_id>/case.json — the case (L2) and its scripts."""
    case_id: str
    l2: str = ""
    name: str = ""                                 # e.g. "package jakarta.xml.bind does not exist"
    kind: ErrorKind = ErrorKind.UNKNOWN
    projects: list[str] = Field(default_factory=list)
    seen: int = 0                                  # entries over all runs
    resolved: int = 0
    scripts: list[CaseScript] = Field(default_factory=list)
    last_analysis: str = ""
    last_solution: str = ""
    updated_at: datetime = Field(default_factory=utcnow)


class ReviewIssue(BaseModel):
    severity: Literal["critical", "warning"] = "warning"
    file: str = ""                                 # relative pom / module path ("" = project-wide)
    problem: str = ""
    fix_hint: str = ""


class StructureReview(BaseModel):
    """v2 structure review (sbmig.review): the LLM checks poms / dependencies / module layout after the restructure,
    before the first build. One line per run in <kb>/reviews.jsonl; RunReport.structure_review."""
    id: str = ""                                   # "<run_id>:review"
    schema_version: int = 3
    at: datetime = Field(default_factory=utcnow)
    run_id: str = ""
    project: str = ""
    case_type: CaseType = CaseType.UNKNOWN
    model: str = ""
    verdict: Literal["ok", "issues", "skipped", "failed"] = "skipped"   # skipped = review.enabled false / dry run /
                                                   # no LLM; failed = no usable answer (the build goes ahead anyway)
    issues: list[ReviewIssue] = Field(default_factory=list)
    analysis: str = ""
    script: str = ""                               # the script for the critical issues ("" = none)
    script_result: Optional[ScriptRunResult] = None
    answer: Optional["ScriptProposal"] = None      # v2.1: the LLM's 원인 / 해결 / edits / script (edits applied by the reviewer)
    commit: Optional[str] = None                   # commit of the script's changes (revertible)
    reverted: bool = False
    llm_usage: LLMUsage = Field(default_factory=LLMUsage)
    note: str = ""                                 # why skipped / failed, or what happened to the script

    @property
    def critical(self) -> list[ReviewIssue]:
        return [i for i in self.issues if i.severity == "critical"]


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
    # error cases (2026-10-23): what the KB needs to record failures as well as fixes
    base_commit: Optional[str] = None              # HEAD before the attempt: `git diff base_commit..commit` = the
                                                   # exact code it applied (handlers / recipes return paths only)
    introduced_errors: list[BuildError] = Field(default_factory=list)  # errors new in the touched files after the
                                                   # rebuild (set by the loop's scoring; why "regressed")
    # v2 fix scripts (decision SCRIPT): which version of which case's script ran, how it came about and what it did
    script_mode: Optional[ScriptMode] = None       # new (LLM, no stored version) | reuse (stored version, no LLM) |
                                                   # augment (LLM extended `script_parent`)
    script_case_id: str = ""
    script_version: int = 0                        # stored version that ran (0 = none / not stored)
    script_parent: Optional[int] = None
    script_code: str = ""
    script_result: Optional[ScriptRunResult] = None
    # v2.1: the try as the error list records it
    source: Optional[TrySource] = None             # kb-script | llm-new | llm-augment | llm-edit
    solution: str = ""                             # 해결 방법 (the LLM's <solution>)
    no_script_reason: str = ""
    edits_applied: int = 0                         # direct edits applied (0 = a script ran)


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


class KBMatch(BaseModel):
    """What the KB knows about a cluster's case (`FileKnowledgeBase.match`): the catalog hits and their hint lines."""
    hints: list[str] = Field(default_factory=list)               # short natural-language hints for the script prompt
    case_hits: list[Any] = Field(default_factory=list)           # CaseHit (case catalog, 2026-10-23)


# ---------------------------------------------------------------------------
# Error cases (2026-10-23, business requirement): every build error of a run is recorded on its own — what it was,
# what was tried, how it was resolved (with the code applied) or why it was not — and every fix attempt, failed
# ones included. kb/errors.jsonl + kb/fix_attempts.jsonl (append-only), kb/error_hints.json (hints index),
# <run dir>/error_cases.jsonl | fix_attempts.jsonl | error_cases.csv. See docs/INTERFACES.md "Error cases".
# ---------------------------------------------------------------------------
# who / what made the change: a deterministic handler, an OpenRewrite recipe (plan recipe ids or KB recipes), a
# replayed KB patch, the LLM, a plan step outside the fix loop (rewrite / restructure / pre-emptive handlers / bump)
FixMethod = Literal["handler", "recipe", "kb_patch", "llm", "plan_step", "none",
                    "script", "script_reuse", "script_augment"]
# v2: script = a new LLM script (no stored version), script_reuse = a stored version run again (no LLM call),
# script_augment = the LLM extended an earlier version; resolver_id = "script:<case_id>:v<N>"
# what the change looks like textually (sbmig.kb.changes.classify_diff), independent of who made it:
#   text_replace = every changed line is the old line with token substitutions (import swaps included) — a rule /
#   command could do it; code_edit = lines added / removed / restructured; file_* = whole-file operations;
#   mixed = text replaces + code edits in one change; none = nothing applied
ChangeKind = Literal["text_replace", "code_edit", "file_create", "file_delete", "file_rename", "mixed", "none"]
# resolved = gone in a later build that reached its stage, after an attempt that targeted it / touched its file;
# resolved_indirectly = gone, but no attempt targeted it (a pom fix, another file's fix, a plan step);
# blocked = still there, covered by a BlockedItem; unresolved = still there at the end of the run;
# not_visible = absent from the last build only because that build stopped before its stage (status unknown)
ErrorCaseStatus = Literal["resolved", "resolved_indirectly", "blocked", "unresolved", "not_visible"]


class ReplacePair(BaseModel):
    """One token-level substitution of a text-replace change: `old` became `new` `count` times (in `files`)."""
    old: str
    new: str
    count: int = 1
    files: list[str] = Field(default_factory=list)            # relative posix paths


class ChangeClass(BaseModel):
    """How a change looks textually — `sbmig.kb.changes.classify_diff(diff_text)`."""
    change_kind: ChangeKind = "none"
    replace_pairs: list[ReplacePair] = Field(default_factory=list)
    file_types: list[str] = Field(default_factory=list)       # java | pom | xml | properties | yaml | jsp | other
    files: list[str] = Field(default_factory=list)            # relative posix paths the diff touches
    lines_added: int = 0
    lines_removed: int = 0


KB_RECORD_SCHEMA = 3      # 1 (or absent) = before round 10 (2026-10-26); 2 = round 10; 3 = v2 fix scripts
                          # (flow-v2: script_* fields, fix methods script / script_reuse / script_augment)


class FixAttemptRecord(BaseModel):
    """One line of kb/fix_attempts.jsonl: one fix attempt of a run — success or failure — with the code it applied."""
    id: str                                        # "<run_id>:<attempt id>"
    schema_version: int = KB_RECORD_SCHEMA
    agent_version: str = ""                        # sbmig version that wrote it (sbmig.__version__)
    at: datetime = Field(default_factory=utcnow)
    project: str = ""
    project_fingerprint: str = ""
    run_id: str = ""
    case_type: CaseType = CaseType.UNKNOWN
    phase: Phase = Phase.SPRING
    iteration: int = 0
    attempt_id: str = ""                           # FixAttempt.id
    cluster_id: str = ""
    error_ids: list[str] = Field(default_factory=list)       # ErrorCaseRecord ids this attempt targeted
    fix_method: FixMethod = "none"
    resolver_id: str = ""                          # handler id | recipe ids (comma) | KB case ids | model
    model: str = ""
    outcome: AttemptOutcome = "pending"
    applied: bool = False
    description: str = ""                          # what was done: handler description / LLM analysis / recipes / case
    failure: str = ""                              # why it failed ("" on improved / build_ok)
    introduced_errors: list[str] = Field(default_factory=list)   # "file:line message" — appeared in touched files
    remaining_errors: list[str] = Field(default_factory=list)    # targeted errors still present after the rebuild
    diff: str = ""                                 # unified diff applied (git), or of the proposal when none applied
    diff_truncated: bool = False
    change: ChangeClass = Field(default_factory=ChangeClass)
    fix_labels: list[str] = Field(default_factory=list)          # FixLabel values (sbmig.kb.labels)
    replacement: str = ""
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    advisory_ids: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    # v2 fix scripts
    script_mode: Optional[ScriptMode] = None
    script_case_id: str = ""
    script_version: int = 0
    script_parent: Optional[int] = None
    script_status: str = ""                        # ScriptRunStatus
    script_error: str = ""
    script_log: list[str] = Field(default_factory=list)


class ErrorAttemptRef(BaseModel):
    """One attempt in an error's history (`ErrorCaseRecord.attempts`) — the short form of a FixAttemptRecord."""
    attempt_id: str                                # FixAttemptRecord.id
    phase: Phase = Phase.SPRING
    iteration: int = 0
    fix_method: FixMethod = "none"
    resolver_id: str = ""
    outcome: AttemptOutcome = "pending"
    failure: str = ""
    change_kind: ChangeKind = "none"
    script_version: int = 0                        # v2: the script version the attempt ran (0 = no script)


class ErrorCaseRecord(BaseModel):
    """One line of kb/errors.jsonl: ONE build error of a run (N errors in a build → N records): what it was, what
    was tried, how it was resolved — or why not, and which errors were the problem."""
    id: str                                        # sha1(run_id | identity | occurrence)[:16]
    schema_version: int = KB_RECORD_SCHEMA
    agent_version: str = ""
    at: datetime = Field(default_factory=utcnow)
    project: str = ""
    project_fingerprint: str = ""
    run_id: str = ""
    case_type: CaseType = CaseType.UNKNOWN
    # the error (sanitized: relative paths, secrets redacted, raw ≤ 2000 chars)
    error_kind: ErrorKind = ErrorKind.UNKNOWN
    message: str = ""
    file: Optional[str] = None
    line: Optional[int] = None
    symbol: Optional[str] = None                   # javac "symbol:"
    location: Optional[str] = None
    module: Optional[str] = None
    artifact: str = ""                             # g:a[:v] of resolution errors
    signature: str = ""                            # L1
    signature_generic: str = ""                    # L2
    error_symbol: str = ""                         # cross-project key (sbmig.kb.labels.symbol_for_errors)
    raw: str = ""
    # lifecycle in the run (build labels "<phase>-<n>", "verify-final", "verify-final-2")
    first_seen: str = ""
    first_phase: Phase = Phase.SPRING
    last_seen: str = ""
    builds_present: int = 1
    # outcome
    status: ErrorCaseStatus = "unresolved"
    resolved_at: str = ""                          # build label in which it was gone
    resolved_phase: Optional[Phase] = None
    fix_method: FixMethod = "none"                 # of the resolving attempt / step
    resolver_id: str = ""
    resolved_by: Optional[str] = None              # FixAttemptRecord id, or "step:<step id>" for plan_step
    # every attempt whose change is part of the resolution, in run order (2026-10-25; `resolved_by` among them): applied,
    # kept in the tree (not reverted) and aimed at this error or changing its file since it (re)appeared — an
    # earlier `no_change` attempt that added an import stays in the tree and belongs to the fix
    resolution_attempts: list[str] = Field(default_factory=list)
    targeted: bool = False                         # the resolving attempt's cluster contained this error
    description: str = ""                          # how it was resolved, or what was tried (status-dependent)
    change_kind: ChangeKind = "none"               # of the resolving change (all `resolution_attempts` combined)
    replace_pairs: list[ReplacePair] = Field(default_factory=list)
    fix_labels: list[str] = Field(default_factory=list)
    diff_excerpt: str = ""                         # the resolution diff restricted to this error's file: one
    #   section per resolution attempt (`# attempt <id> …` headers when more than one), each ≤ kb.error_excerpt_chars
    # failure side
    failure_reason: str = ""                       # why not resolved ("" when resolved)
    problem_errors: list[str] = Field(default_factory=list)  # "file:line message" that made the last attempt fail
    block_reason: str = ""                         # BlockReason value
    suggestion: str = ""
    attempts: list[ErrorAttemptRef] = Field(default_factory=list)   # every attempt that targeted it, in order
    attempt_count: int = 0
    llm_attempts: int = 0
    notes: list[str] = Field(default_factory=list)
    case_id: str = ""                              # CatalogCase id = sha1(signature_generic)[:10] (2026-10-23)
    # round 10 context (from the ProjectProfile / the tracker): what the project was and what came right before
    asis_java: str = ""
    asis_spring_boot: str = ""
    asis_spring: str = ""
    packaging: str = ""
    introduced_by: str = ""                        # "step:<plan step id>" or an attempt id that preceded its first build
    # v2: the case script that resolved it (from the resolving attempt)
    script_case_id: str = ""
    script_version: int = 0


# ---------------------------------------------------------------------------
# Case catalog (2026-10-23): ONE reusable case per error signature (L2 = the case key), variants by the owner class /
# L1 — kb/catalog/<kind>/<class>/<symbol>--<id>.yml. Built from the error-case events (kb/errors.jsonl,
# fix_attempts.jsonl), the KB recipes and patch cases; the `human` block is never overwritten by the KB, every other
# field is recomputed. A hit at triage applies the case's action (see docs/INTERFACES.md "Case catalog").
# ---------------------------------------------------------------------------
CaseStatus = Literal["observed", "candidate", "active", "rejected"]
CaseActionKind = Literal["hint", "block", "script"]
# script = run the case's best stored fix script (CaseAction.script_version); block = never try (BlockReason +
# suggestion); hint = nothing proven yet (the case's hints go into the script prompt)


class CaseVariant(BaseModel):
    owner: str = ""                                # FQCN owning the symbol (javac location / symbol owner); "" unknown
    signature: str = ""                            # L1
    seen: int = 0
    projects: int = 0


class CaseFix(BaseModel):
    """One way this case was fixed (grouped by method + resolver + replace pairs), the latest attempt as example."""
    method: FixMethod = "none"
    resolver_id: str = ""
    change_kind: ChangeKind = "none"
    replace_pairs: list[ReplacePair] = Field(default_factory=list)
    description: str = ""
    attempt_id: str = ""                           # FixAttemptRecord id of the latest such fix (diff there)
    attempt_ids: list[str] = Field(default_factory=list)     # its resolution attempts when more than one
    error_id: str = ""
    project: str = ""
    at: Optional[datetime] = None
    count: int = 0
    projects: int = 0


class CaseFailure(BaseModel):
    method: FixMethod = "none"
    outcome: AttemptOutcome = "no_change"
    failure: str = ""
    attempt_id: str = ""
    project: str = ""
    count: int = 0
    projects: int = 0


class CaseEvent(BaseModel):
    """Timeline entry: this case seen in one run (one ErrorCaseRecord)."""
    at: Optional[datetime] = None
    project: str = ""
    run_id: str = ""
    error_id: str = ""
    file: str = ""
    status: ErrorCaseStatus = "unresolved"
    fix_method: FixMethod = "none"
    attempts: list[str] = Field(default_factory=list)   # "method:outcome" in order (failures before the fix)
    resolved_by: Optional[str] = None


class CaseAction(BaseModel):
    kind: CaseActionKind = "hint"
    block_reason: str = ""                                    # block: BlockReason value + suggestion
    suggestion: str = ""
    script_version: int = 0                                   # script: <kb>/scripts/<case id>/v<N>.py


class CaseEvidence(BaseModel):
    seen: int = 0                                  # error records
    projects: int = 0
    runs: int = 0
    first_at: Optional[datetime] = None
    last_at: Optional[datetime] = None
    by_status: dict[str, int] = Field(default_factory=dict)
    resolved_by: dict[str, int] = Field(default_factory=dict)  # fix_method → resolved records
    fixes: list[CaseFix] = Field(default_factory=list)         # top 5 by projects, count
    failures: list[CaseFailure] = Field(default_factory=list)  # top 5
    timeline: list[CaseEvent] = Field(default_factory=list)    # last 20, oldest first


class CaseHuman(BaseModel):
    """Edited by people (YAML or `sbmig kb cases …`); never overwritten by harvest / reindex; wins over the auto part."""
    name: Optional[str] = None
    status: Optional[CaseStatus] = None            # e.g. rejected, or active by decision
    action: Optional[CaseAction] = None            # a pinned action
    hints: list[str] = Field(default_factory=list)
    notes: str = ""


class CatalogCase(BaseModel):
    id: str                                        # sha1(signature_generic)[:10]
    name: str = ""                                 # auto, e.g. "LegacyRequestHelper.clientIp — cannot find symbol"
    error_kind: ErrorKind = ErrorKind.UNKNOWN
    error_class: str = ""                          # 2nd L2 segment (cannot_find_symbol, package_does_not_exist, …)
    symbol: str = ""                               # 3rd L2 segment / error symbol
    signature_generic: str = ""
    variants: list[CaseVariant] = Field(default_factory=list)
    status: CaseStatus = "observed"                # auto
    action: CaseAction = Field(default_factory=CaseAction)   # auto
    evidence: CaseEvidence = Field(default_factory=CaseEvidence)
    hints: list[str] = Field(default_factory=list)            # auto (best fix, top failure)
    human: CaseHuman = Field(default_factory=CaseHuman)
    updated_at: datetime = Field(default_factory=utcnow)

    def effective_status(self) -> CaseStatus:
        return self.human.status or self.status

    def effective_action(self) -> CaseAction:
        return self.human.action or self.action

    def effective_name(self) -> str:
        return self.human.name or self.name


class CaseHit(BaseModel):
    """A catalog match for some errors of a cluster (effective status / action already applied)."""
    case_id: str
    name: str = ""
    status: CaseStatus = "observed"
    action: CaseAction = Field(default_factory=CaseAction)
    errors: list[BuildError] = Field(default_factory=list)
    variant_owner: str = ""
    hints: list[str] = Field(default_factory=list)
    pinned: bool = False                           # round 10: `action` is the human block's pinned action (triage obeys it)


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
    advisories: list[str] = Field(default_factory=list)           # "<advisory_id>:<outcome>" (library advisories)
    error_counts: dict[str, int] = Field(default_factory=dict)    # v2.1: ErrorEntry status → count (open / resolved / …)
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
AdvisoryPolicy = Literal["guide"]              # v2: advisories are LLM hints + Handover only
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
    policy: AdvisoryPolicy = "guide"               # always guide in v2 (kept for report.json readers)
    detected_version: Optional[str] = None
    evidence: list[str] = Field(default_factory=list)     # LibraryUsage evidence strings
    artifacts: list[str] = Field(default_factory=list)    # "g:a" keys found in the as-is poms
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
    unresolved: list[BuildError] = Field(default_factory=list)
    blocked: list[BlockedItem] = Field(default_factory=list)       # deliberately skipped: needs library/infra/human action
    manual_followups: list[str] = Field(default_factory=list)    # things the as-is owner must do (runtime concerns)
    advisories: list[AdvisoryFinding] = Field(default_factory=list)   # library advisories (EOL / unmanaged libs)
    handover: Optional[HandoverSummary] = None                   # versions replaced + "from here on, your team"
    merge_conflicts: list[MergeConflict] = Field(default_factory=list)   # from the layout restructure (merge policy)
    properties: Optional[PropertyInventory] = None               # profile inventory + placeholders written (round 8)
    flags: list[str] = Field(default_factory=list)               # e.g. "test-compile-skipped"
    javax_api_jars: list[JavaxApiJar] = Field(default_factory=list)   # report.javax_api_inventory (2026-10-22)
    # every build error of the run, one record each (2026-10-23; full detail incl. diffs in <run dir>/error_cases.jsonl
    # and fix_attempts.jsonl — the report copy has `diff_excerpt` emptied to keep report.json small)
    error_cases: list[ErrorCaseRecord] = Field(default_factory=list)
    error_case_counts: dict[str, int] = Field(default_factory=dict)   # status → count
    structure_review: Optional[StructureReview] = None           # v2: LLM review of poms / modules before the first build
    error_entries: list[ErrorEntry] = Field(default_factory=list)    # v2.1: the run's error list (diffs capped)
    outputs: dict[str, str] = Field(default_factory=dict)        # project_dir, zip, diff, journal, report_md, report_json
    timings_s: dict[str, float] = Field(default_factory=dict)


MigrationPlan.model_rebuild()
