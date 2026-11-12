"""패키지끼리 주고받는 공유 데이터 모델을 모아 둔 모듈. pydantic 모델, Enum, Literal이 모두 이 파일에 있다.
데이터가 만들어지는 순서: 분석(ProjectProfile) -> 계획(MigrationPlan) -> 빌드(BuildResult) -> 수정
루프(ErrorCluster, FixAttempt, ScriptProposal) -> 오류 목록(ErrorEntry, FixTry)과 KB 케이스(CaseInfo,
CaseScript) -> 보고서(RunReport) -> 플릿(FleetState).
- 플러그인, report.json, KB 파일이 이 형태에 의존하므로 바꾸면 docs/INTERFACES.md에 기록한다
- 경로 필드는 프로젝트 루트 기준 상대 경로다. `_abs`로 끝나는 필드와 로그, 출력 경로만 예외다
- ErrorCaseRecord / FixAttemptRecord / CatalogCase 계열, ScriptMeta, FixLabel은 이전 KB 기록을 읽기 위해서만
  남아 있고 지금 코드는 채우지 않는다

Shared data models for sbmig.

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
    """timezone 정보가 있는 UTC 현재 시각을 반환한다. 레코드 시각 필드의 기본값으로 쓴다."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class CaseType(str, Enum):
    """분석 단계가 정하는 프로젝트 종류.
    회사의 네 가지 프로젝트 유형과 unknown이 있으며, 계획과 보고서가 이 값에 따라 달라진다."""
    PURE_JAVA = "pure_java"            # case 1: in-house pure Java (no Spring, no servlet)
    SPRING_LEGACY = "spring_legacy"    # case 2a: Spring Framework 4/5 without Boot (xml / annotation config)
    SPRING_BOOT = "spring_boot"        # case 2b: Spring Boot 2.x / 3.x
    SERVLET_TOMCAT = "servlet_tomcat"  # case 3: raw servlet / JSP on Tomcat (war, web.xml)
    MSA = "msa"                        # case 4: several deployable services in one repo
    UNKNOWN = "unknown"


class Phase(str, Enum):
    """실행 단계 이름. 실제로 쓰는 단계는 intake, analyze, plan, transform, restructure, verify, report다.
    jdk21 / spring은 수정 루프 내부 기본값과 이전 기록 호환을 위해서만 남아 있다."""
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
    """계획 단계(PlanStep)의 종류. 오케스트레이터가 이 값을 보고 실행 방법을 고른다.
    현재 계획은 openrewrite / build / restructure / msa_pom / review만 만든다."""
    OPENREWRITE = "openrewrite"
    RESTRUCTURE = "restructure"    # non-MSA layout transform
    MSA_POM = "msa_pom"            # MSA: pom-only parent/mandatory/optional edits
    BUILD_FIX_LOOP = "build_fix_loop"
    BUILD = "build"                # single build without fixing (verify)
    REVIEW = "review"              # v2: LLM structure review of poms / dependencies / modules before the first build


class ErrorKind(str, Enum):
    """빌드 로그 파서가 오류에 붙이는 종류.
    L2 서명의 첫 부분이므로 케이스 분류, 차단 정책, 보고서 묶음에 쓰인다."""
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
    """트리아지가 케이스마다 내리는 처리 방법.
    skip은 이번 라운드에서 기다리기, escalate는 포기하고 보고서로 넘기기, blocked는 이 프로젝트 안에서 고칠 수 없음이다.
    script는 저장된 스크립트 재사용, 보강, 신규 시도 중 하나를 실행한다."""
    SKIP = "skip"              # masked by another cluster this iteration
    ESCALATE = "escalate"      # give up on this cluster; goes to report
    BLOCKED = "blocked"        # cannot be fixed inside this project (external library / infra) → skip now, report
    SCRIPT = "script"          # v2: run a fix script for the cluster's case (stored version, augmented or new)


# 시도 한 번의 재빌드 결과. improved가 아니면 다음 시도 전에 되돌리고, regressed는 되돌린 뒤 reverted가 된다
AttemptOutcome = Literal["pending", "improved", "no_change", "regressed", "reverted", "build_ok", "apply_failed",
                         "blocked", "partial", "inconclusive"]
# round 10 (2026-10-26): `partial` = some of the cluster's targeted errors are gone, the rest remain, nothing new in the
# touched files (progress: not a failure for limits / KB failure counts, not a success for harvest / recipe credit);
# `inconclusive` = the scoring build could not show the cluster's errors (timed out, failed without parsed errors,
# stopped at an earlier stage that the attempt did not cause) — no KB credit either way, counts as tried


# 오류 목록 항목의 상태.
# pom-verify는 재구성이 고친 pom을 다음 빌드로 확인하는 상태이고, note는 고치지 않는 메모다
EntryStatus = Literal["open", "pom-verify", "resolved", "blocked", "unresolved", "note"]
# open = still in the last build; pom-verify = a restructure pom correction the next build verifies; note = a review
# warning / informational entry (never fixed)
# 시도의 출처. kb-script는 저장된 스크립트 재사용, llm-*는 LLM 시도, review는 구조 검토가 한 수정이다
# sbmig-*는 LLM 없이 sbmig가 직접 고치는 사전 시도다
TrySource = Literal["kb-script", "llm-new", "llm-augment", "llm-edit", "review", "sbmig-version", "sbmig-resources",
                    "sbmig-jakarta", "sbmig-bundled", "sbmig-parent"]
# kb-script = a stored version run without an LLM call; llm-new = first answer for the case; llm-augment = the LLM
# extended a failed script; llm-edit = the LLM said the fix cannot be a script and gave edits; review = the
# structure review's own fix; sbmig-version = v2.1l: sbmig wrote the missing dependency versions itself (the as-is
# build's version, else the repository's newest release) — no LLM call, no stored script; sbmig-resources = v2.1m:
# sbmig added the filtered binary files' extensions to nonFilteredFileExtensions — no LLM call, no stored script;
# sbmig-jakarta = v2.1z-e: sbmig swapped a javax-era dependency for the jakarta build it found in the repository — no LLM
# call, no stored script
# sbmig-bundled = v2.1z-i: sbmig wired a jar bundled in the tree (declared by no pom) that provides the missing package —
#   real coordinates when the repository has them, else system scope at <module>/lib + includeSystemScope; no LLM
# sbmig-parent = v2.1z-ag: sbmig set the configured version of a company parent whose as-is version the repository does
#   not have (사내 부모의 as-is 버전이 저장소에 없어 설정 버전으로 맞춘 시도) — no LLM call, no stored script
TryApplied = Literal["script", "edits", "none"]  # 실제로 적용된 것(스크립트, 편집 목록, 없음)


class BlockReason(str, Enum):
    """케이스를 차단한 이유. 보고서의 차단 섹션과 `loop.blocked_policy`가 이 값에 따라 달라진다.
    차단은 이 프로젝트 안에서 고칠 수 없다는 뜻이지 실행이 실패했다는 뜻이 아니다."""
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
    # v2.1k (2026-10-29): a module reached twice in the reactor ("Project … is duplicated in the reactor") — a pom structure
    # problem the intake de-duplicates; when it still reaches the build it is handed over, never given to the LLM
    REACTOR_DUPLICATE = "reactor_duplicate"
    # Windows에서 다른 프로세스가 target/의 파일을 잠가 빌드가 실패한 경우다.
    # 빌드 환경 문제이므로 LLM에 요청하지 않는다
    # v2.1r (2026-10-30): a file under target/ locked by another process (antivirus / IDE / a leftover JVM) — an
    # environment error the runner already retried; handed over at once, never given to the LLM
    FILE_LOCK = "file_lock"
    # v2.1y (2026-10-31, prompt review E-M1): the LLM answered with neither edits nor a script and named a blocker outside the
    # project (a library with no jakarta build, a framework the owners must replace, an artifact the repository lacks) — the
    # owning team decides; sibling cases with the same cause are blocked without another LLM call
    # LLM이 수정 없이 프로젝트 밖의 문제라고 답한 경우다.
    # 원인이 같은 다른 케이스는 LLM을 호출하지 않고 차단한다
    NEEDS_OWNER = "needs_owner"


class BlockedItem(BaseModel):
    """수정 루프가 더 이상 고치지 않기로 한 오류 묶음(클러스터) 하나를 담는 데이터 클래스.
    보고서의 'Blocked' 섹션과 인수인계 섹션이 사람이 할 일(suggestion), 원인 라이브러리(artifact,
    via_direct)와 함께 보여 준다.

    One cluster the agent deliberately stopped working on. Rendered as the 'Blocked — needs human/library action'
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
    # 원인으로 지목된 것이 javax API jar뿐이면 dependency:tree에서 그 jar를 끌어온 경로(via)와 직접
    # 의존성(via_direct)을 남긴다
    # 사람이 버전을 올려야 하는 대상은 API jar가 아니라 via_direct이기 때문이다
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
# pom 없는 Tomcat 디렉토리를 읽기만 하는 `sbmig inventory`의 결과.
# 그런 디렉토리는 접수에서 거부하므로 거부 안내를 쓸 때만 쓴다
# Exploded webapp inventory (`sbmig inventory`, 2026-10-23): a Tomcat directory without a pom — JSPs, Java sources,
# WEB-INF/lib jars, WEB-INF/classes — read only, written as inventory.md / inventory.json; the pom synthesis of the
# next round consumes it. Everything is a file-system fact or an explicitly labelled guess.
# ---------------------------------------------------------------------------
class JarInfo(BaseModel):
    """`sbmig inventory`가 찾은 WEB-INF/lib의 jar 하나를 담는 데이터 클래스.
    크기, sha1, 파일 이름으로 추정한 좌표를 담고, 추정한 값은 추정이라고 표시한다."""
    path: str                                   # root-relative
    name: str                                   # file name
    size: int = 0
    sha1: str = ""                              # for a later Nexus / Central lookup
    version_guess: Optional[str] = None         # parsed from the file name (`commons-lang-2.6.jar` → 2.6)
    artifact_guess: Optional[str] = None        # "g:a" from knowledge/libraries/jar_names.yml (None = unknown)
    ignored: bool = False                       # *.jar.old / *.bak / *.jar_ … — backups Tomcat does not load


class WebappRoot(BaseModel):
    """`sbmig inventory`가 찾은 WEB-INF가 있는 웹 루트 하나를 담는 데이터 클래스.
    web.xml 요약, JSP와 정적 파일 수, lib jar, 클래스 파일의 Java 버전을 담는다."""
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
    """`sbmig inventory`가 찾은, `package` 선언과 경로가 맞는 Java 소스 루트 하나를 담는 데이터 클래스.
    인코딩, javax와 프레임워크 import 수를 집계한다."""
    dir: str                                    # root-relative directory where `package a.b;` matches the path
    java_files: int = 0
    packages: list[str] = Field(default_factory=list)      # top-level packages (a.b)
    encoding: str = "utf-8"                     # majority detected encoding (cp949 / euc_kr / utf-8)
    javax_imports: dict[str, int] = Field(default_factory=dict)      # "javax.servlet" → files
    framework_imports: dict[str, int] = Field(default_factory=dict)  # spring / struts1 / struts2 / ibatis2 / mybatis / hibernate / egov → files
    jdk_internal_imports: int = 0               # sun.* / com.sun.* files
    inside_webapp: Optional[str] = None         # the WebappRoot.dir it sits under (e.g. "app1/WEB-INF/src"), else None


class ModuleProposal(BaseModel):
    """`sbmig inventory`가 pom을 만든다면 생길 Maven 모듈을 추정한 데이터 클래스.
    지금은 inventory.md에 보여 주기만 한다.

    One Maven module the pom synthesis would create (a guess the operator can change)."""
    name: str                                   # artifactId guess (webapp dir name, "app" for the root)
    webapp_dir: str
    source_dirs: list[str] = Field(default_factory=list)
    packaging: str = "war"
    java_version: str = "1.8"                   # from the class files, else the sources, else 1.8
    dependencies_known: int = 0                 # jars with an artifact guess
    dependencies_unknown: int = 0               # jars without one (→ lib/ + system scope / Nexus install)
    notes: list[str] = Field(default_factory=list)


class WebappInventory(BaseModel):
    """`sbmig inventory <dir>`의 전체 결과(inventory.md / inventory.json)를 담는 데이터 클래스.
    디렉토리는 바꾸지 않는다."""
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
    adf: Optional[AdfSummary] = None            # v2.1z-d: the ADF verdict + sizing when the tree shows ADF evidence


# ---------------------------------------------------------------------------
# web.xml 해석 결과. 내장 Tomcat과 회사 Boot WAR는 web.xml을 읽지 않으므로 재구성이 이 값으로 등록
# 빈(WebXmlRegistrations)을 만든다
# Servlet 3 이전 프로젝트는 @WebServlet이 없으므로 이 값이 없으면 서블릿과 필터 매핑이 모두 사라진다
# web.xml (2026-10-23): parsed by sbmig.analyze.webxml (DTD 2.2 / 2.3 and XSD 2.4 … 6.0, namespace-agnostic); the
# restructure renders WebXmlRegistrations (ServletRegistrationBean / FilterRegistrationBean /
# ServletListenerRegistrationBean / ErrorPageRegistrar) from it because neither embedded Tomcat nor the company's
# Boot WAR (web.xml is not packaged) reads the descriptor. Pre-Servlet-3 projects have no @WebServlet annotations,
# so @ServletComponentScan alone would lose every mapping.
# ---------------------------------------------------------------------------
class WebXmlServlet(BaseModel):
    """web.xml의 `<servlet>` 하나와 그 매핑을 담는 데이터 클래스.
    재구성이 ServletRegistrationBean으로 옮긴다."""
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
    """web.xml의 `<filter>` 하나를 담는 데이터 클래스.
    `order`는 필터 체인 순서를 지키기 위한 첫 `<filter-mapping>`의 위치다."""
    name: str
    filter_class: str
    url_patterns: list[str] = Field(default_factory=list)
    servlet_names: list[str] = Field(default_factory=list)
    dispatchers: list[str] = Field(default_factory=list)   # REQUEST | FORWARD | INCLUDE | ERROR | ASYNC
    init_params: dict[str, str] = Field(default_factory=dict)
    async_supported: bool = False
    order: int = 0                                 # position of its first <filter-mapping> (web.xml chain order)


class WebXmlErrorPage(BaseModel):
    """web.xml의 `<error-page>` 하나를 담는 데이터 클래스. 재구성이 ErrorPageRegistrar로 옮긴다."""
    location: str
    error_code: Optional[int] = None
    exception_type: Optional[str] = None           # FQCN; both None = default error page


class WebXmlModel(BaseModel):
    """web.xml 파일 하나의 해석 결과를 담는 데이터 클래스.
    analyze.webxml이 만들고 재구성의 web.xml 등록 단계가 읽는다."""
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
    """이관 뒤에도 모듈 classpath에 남은 javax EE API jar와 그 jar를 끌어온 의존성 경로를 담는 데이터 클래스.
    build.deptree가 만들고, 보고서의 'javax API jars still on the classpath' 섹션과 차단 사유(via_direct)가 읽는다.

    A javax EE API jar (or a javax-era `jakarta.*-api` 2.x) still on a module's resolved classpath after the
    migration, with the dependency path that brings it (`mvn dependency:tree`). Rendered as the report section
    "javax API jars still on the classpath" (report.javax_api_inventory, 2026-10-22)."""
    artifact: GAV                                               # the API jar, e.g. javax.persistence:javax.persistence-api:2.2
    module: Optional[str] = None                                # module dir ("" / None = root)
    scope: Optional[str] = None                                 # compile | provided | runtime | test
    path: list[str] = Field(default_factory=list)               # root → … → API jar (coordinates as printed by the tree)
    via: Optional[GAV] = None                                   # direct dependency of the module on that path
    declared: bool = False                                      # the API jar itself is declared in the module pom


class GAV(BaseModel):
    """Maven 좌표(groupId:artifactId:version, scope 등)를 담는 데이터 클래스."""
    group_id: str
    artifact_id: str
    version: Optional[str] = None
    scope: Optional[str] = None
    type: Optional[str] = None
    classifier: Optional[str] = None
    optional: bool = False

    @property
    def key(self) -> str:
        """버전 없는 비교 키 `groupId:artifactId`를 반환한다."""
        return f"{self.group_id}:{self.artifact_id}"

    @property
    def coordinates(self) -> str:
        """버전이 있으면 버전까지 붙인 `g:a[:v]` 표기를 반환한다."""
        return self.key + (f":{self.version}" if self.version else "")

    @classmethod
    def parse(cls, coords: str) -> "GAV":
        """`"g:a[:v]"` 문자열을 GAV로 바꾼다. 콜론으로 나눈 부분이 둘보다 적으면 ValueError를 던진다.
        Maven의 긴 형식 `g:a:packaging[:classifier]:v`는 마지막 부분을 버전으로 본다."""
        parts = coords.split(":")
        if len(parts) < 2:
            raise ValueError(f"bad coordinates: {coords}")
        version = parts[-1] if len(parts) >= 4 else (parts[2] if len(parts) > 2 else "")
        return cls(group_id=parts[0], artifact_id=parts[1], version=version or None)


class PluginInfo(BaseModel):
    """pom의 `<plugin>` 하나(좌표와 configuration)를 담는 데이터 클래스."""
    group_id: str = "org.apache.maven.plugins"
    artifact_id: str
    version: Optional[str] = None
    configuration: dict[str, Any] = Field(default_factory=dict)

    @property
    def key(self) -> str:
        """버전 없는 `groupId:artifactId` 키를 반환한다."""
        return f"{self.group_id}:{self.artifact_id}"


class PomInfo(BaseModel):
    """pom.xml 파일 하나의 분석 결과를 담는 데이터 클래스.
    실제 적용되는 groupId, 부모, 모듈, 의존성, 저장소, Java / Boot 버전을 담는다."""
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
    """소스 트리 통계를 담는 데이터 클래스.
    main 클래스, 서블릿, javax import, web.xml, JSP와 정적 리소스 위치를 담는다.
    프로젝트 종류를 정하고 재구성(front 모듈, web.xml 등록, 큰 미디어 파일 안내)을 할 때 근거로 쓴다."""
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
# as-is 코드가 외부 설정을 읽는 방식(@Value, @ConfigurationProperties, Environment, XML ${…} 등)
PropertyRefSource = Literal["value-annotation", "configuration-properties", "environment", "system-property",
                            "system-env", "xml-placeholder", "context-param", "jndi", "property-source", "profiles",
                            "config-placeholder"]   # round 9: `${…}` inside the value of a packaged definition file
                                                    # (`spring.datasource.url=${DB_URL}` → DB_URL is read at runtime)
PropertyStatus = Literal["defined", "defaulted", "external"]


class PropertyRef(BaseModel):
    """as-is 코드나 XML이 외부 설정 키를 읽는 위치 하나를 담는 데이터 클래스.

    One place where the as-is code / XML reads an externalized setting."""
    key: str                                       # the key as written: "db.url", "java:comp/env/jdbc/x", "app.*" (prefix)
    source: PropertyRefSource
    file: str                                      # repo-relative path at analysis time
    line: Optional[int] = None
    default: Optional[str] = None                  # `${key:default}` / System.getProperty(k, default) → "default"
    scope: Literal["main", "test"] = "main"
    raw: str = ""                                  # matched text, sanitized, ≤ 200 chars


class PropertyFinding(BaseModel):
    """외부 설정 키(또는 접두어) 하나와 그 값이 어디에 있는지를 담는 데이터 클래스.
    - defined: 패키지에 들어가는 리소스에 값이 있다
    - defaulted: 코드의 기본값만 있다
    - external: 어디에도 없어서 운영자가 값을 넣어야 한다

    One key (or prefix) with its verdict. `boot_key` is what sbmig writes into application.properties when it
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
    """외부 설정 목록 전체를 담는 데이터 클래스.
    분석 때는 ProjectProfile.properties, 재구성 뒤에는 RunReport.properties에 들어간다.
    인수인계의 '설정할 키' 표와 필터링 대상 바이너리 목록을 여기서 만든다.

    ProjectProfile.properties (analysis) and RunReport.properties (after restructure wrote the placeholders)."""
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
        """어디에도 정의되지 않은 키(status가 external인 항목)만 반환한다."""
        return [f for f in self.findings if f.status == "external"]


class LibraryUsage(BaseModel):
    """프로젝트가 쓰는 라이브러리 하나(정규화된 이름과 근거)를 담는 데이터 클래스.
    권고와 프롬프트 힌트가 읽는다."""
    name: str                       # normalized: lombok, mapstruct, querydsl, spring-security, springdoc, swagger2,
                                    # mybatis, jpa, hibernate, feign, kafka, redis, jackson, junit4, junit5, mockito, ...
    version: Optional[str] = None
    evidence: list[str] = Field(default_factory=list)   # "pom:org.projectlombok:lombok", "import:javax.persistence"


class ServiceInfo(BaseModel):
    """MSA 저장소 안의 서비스 하나, 또는 함께 이관해야 하는 공유 라이브러리 루트(kind=library)를 담는 데이터 클래스.

    One deployable unit inside an MSA repo — or (kind="library") a shared library root that must be migrated too."""
    name: str
    root_pom: str                   # relative path to the service's top pom
    case_type: CaseType
    kind: Literal["service", "library"] = "service"
    poms: list[str] = Field(default_factory=list)
    main_classes: list[str] = Field(default_factory=list)
    spring_boot_version: Optional[str] = None
    java_version: Optional[str] = None


class AdfSummary(BaseModel):
    """Oracle ADF(JDeveloper) 프로젝트인지에 대한 판단과 규모를 담는 데이터 클래스.
    분석 단계가 ``sbmig.analyze.adf.detect_adf``로 모아 ``ProjectProfile.adf``와 inventory에 남긴다.
    - 근거 네 종류(``bc4j``, ``java``, ``model``, ``faces``) 중 둘 이상이면 ``is_adf``로 변환을 중단하고,
      하나면 ``traces``로 표시하고 계속한다
    - 종류별 근거 경로는 사람이 판단을 검토할 때 쓰는 자료다
    - ADF는 변환하지 않으므로 규모는 집계용 정보다

    The ADF verdict and sizing (v2.1z-d): four evidence families (`bc4j`, `java`, `model`, `faces`), `is_adf` = two or more
    present (rejected: "ADF 프로젝트라 변환이 불가능합니다"), `traces` = exactly one (soft flag, the run goes on); capped evidence
    paths per family for the audit; counts for the census. ADF projects are not converted (business decision)."""
    is_adf: bool = False
    traces: bool = False
    families: list[str] = Field(default_factory=list)                 # present families, A→D order: bc4j / java / model / faces
    evidence: dict[str, list[str]] = Field(default_factory=dict)      # family → evidence paths (capped per family)
    evidence_counts: dict[str, int] = Field(default_factory=dict)     # family → number of evidence files (uncapped)
    evidence_total: int = 0
    hints: list[str] = Field(default_factory=list)                    # .jpr naming ADF, adf-config.xml — information, never evidence
    entities: int = 0
    view_objects: int = 0
    app_modules: int = 0
    associations: int = 0
    view_links: int = 0
    page_defs: int = 0
    task_flows: int = 0
    flow_views: int = 0
    pages: int = 0
    java_total: int = 0
    java_adf: int = 0
    tables: list[str] = Field(default_factory=list)            # distinct DBObjectName (capped)
    tables_total: int = 0
    by_dir: dict[str, dict[str, int]] = Field(default_factory=dict)   # top-level dir → java / java_adf / bc4j / pages / page_defs


class ProjectProfile(BaseModel):
    """분석 단계의 결과 전체(profile.json)를 담는 데이터 클래스.
    계획, 재구성, 구조 검토, 수정 프롬프트, 보고서가 모두 읽는다.
    build_tool이 maven이 아니면 접수에서 중단한다."""
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
    # Oracle ADF 판단 결과와 규모.
    # 근거가 하나라도 있을 때만 채운다 / the ADF verdict + sizing, None without evidence
    adf: Optional[AdfSummary] = None
    # v2.1z-i: jars that sit in the tree (WEB-INF/lib, lib/, …) and that NO pom declares — the as-is IDE build saw them,
    # Maven does not; `sbmig.analyze.bundled_jars.scan_bundled_jars` fills it, the fix loop wires them (`sbmig-bundled`)
    bundled_jars: list["JarCoordinate"] = Field(default_factory=list)

    def has_library(self, name: str) -> bool:
        """정규화된 라이브러리 이름(LibraryUsage.name)을 프로젝트가 쓰는지 확인한다."""
        return any(lib.name == name for lib in self.libraries)

    @staticmethod
    def make_fingerprint(pom_paths: list[str], root_artifact_id: str) -> str:
        """pom 경로와 루트 artifactId로 16자리 해시를 만든다.
        계획이 어느 프로필에서 나왔는지 확인할 때 쓴다."""
        h = hashlib.sha256()
        h.update(root_artifact_id.encode())
        for p in sorted(pom_paths):
            h.update(b"|" + p.encode())
        return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------
class RecipeRef(BaseModel):
    """계획에 들어가는 OpenRewrite 레시피 하나를 담는 데이터 클래스.
    id는 knowledge/recipes/catalog.yml에 있는 것만 쓴다."""
    id: str                                        # fully qualified recipe id
    source: Literal["builtin"] = "builtin"         # v2: OpenRewrite recipes only (no project / KB recipes)
    artifact: Optional[str] = None                 # "org.openrewrite.recipe:rewrite-spring" (version from config)


class PlanStep(BaseModel):
    """계획 단계 하나를 담는 데이터 클래스.
    계획이 만들고 오케스트레이터와 LangGraph 엔진이 순서대로 실행한다.
    id는 transform.rewrite / restructure.* / restructure.review /
    verify.build 등이고, 서비스별 단계면 뒤에 접미사가 붙는다."""
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
    """계획 단계의 결과(plan.json)를 담는 데이터 클래스.
    계획은 LLM이 아니라 규칙으로 정하며, `plan.advisor: llm`은 기본으로 꺼져 있다."""
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
    """빌드 로그에서 뽑은 오류 한 건을 담는 데이터 클래스.
    L1 서명 `signature`와 L2 서명 `signature_generic`이 붙고, L2가 케이스를 정한다."""
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
        """같은 오류를 가리키는 키(파일:줄:L1 서명)를 반환한다. 빌드 결과끼리 비교할 때 쓴다."""
        return f"{self.file or '-'}:{self.line or 0}:{self.signature}"


class BuildResult(BaseModel):
    """Maven 빌드 한 번의 결과를 담는 데이터 클래스.
    성공 여부, 오류 목록, 실패한 모듈, 타임아웃, 로그 경로를 담는다."""
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
        """오류 개수를 반환한다."""
        return len(self.errors)

    def signature_multiset(self) -> dict[str, int]:
        """L1 서명별 오류 수를 반환한다.
        라운드끼리 비교해 진척이 없는지, 같은 상태를 오가는지 판단할 때 쓴다."""
        out: dict[str, int] = {}
        for e in self.errors:
            out[e.signature] = out.get(e.signature, 0) + 1
        return out


class RewriteResult(BaseModel):
    """OpenRewrite 실행 한 번의 결과를 담는 데이터 클래스.
    바뀐 파일, 레시피별로 바뀐 파일, 파싱하지 못한 파일을 담는다."""
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
    """트리아지가 묶은 오류 묶음(클러스터) 클래스.
    클러스터 하나가 케이스 하나이며, L2 서명이 같으면 파일이 달라도 같은 클러스터다."""
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
        """클러스터에 속한 오류들의 L2 서명을 중복 없이 정렬해서 반환한다."""
        return sorted({e.signature_generic for e in self.errors if e.signature_generic})


class TriageDecision(BaseModel):
    """트리아지가 클러스터 하나에 내린 처리 방법과 그 이유를 담는 데이터 클래스."""
    cluster_id: str
    kind: DecisionKind
    rationale: str = ""


class FileChange(BaseModel):
    """파일 편집 한 건을 담는 데이터 클래스. 지금은 시도가 바꾼 파일을 FixAttempt.changes에 남길 때만 쓴다.

    One atomic edit. modify = search/replace (search must match exactly once), create/delete = whole file."""
    path: str
    op: Literal["modify", "create", "delete", "rename"]
    search: Optional[str] = None
    replace: Optional[str] = None
    content: Optional[str] = None
    new_path: Optional[str] = None


class LLMUsage(BaseModel):
    """LLM 사용량(호출 수, 입력 / 출력 토큰)을 담는 데이터 클래스.
    단계와 시도마다 더해서 RunReport.llm_usage를 만든다."""
    model: str = ""
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, other: "LLMUsage") -> "LLMUsage":
        """두 사용량을 더한 새 객체를 반환한다. 모델 이름은 먼저 값이 있는 쪽을 쓴다."""
        return LLMUsage(
            model=self.model or other.model,
            calls=self.calls + other.calls,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


class ModelCost(BaseModel):
    """한 실행에서 모델 하나의 사용량과 `llm.pricing`으로 계산한 비용을 담는 데이터 클래스.
    가격 정보가 없으면 cost는 None이다.

    LLM usage of one model in a run, priced with `llm.pricing` (currency per 1M tokens) when a price is known."""
    model: str
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    price_key: str = ""                            # the llm.pricing entry that matched ("" = unpriced)
    input_per_mtok: Optional[float] = None
    output_per_mtok: Optional[float] = None
    cost: Optional[float] = None                   # None = no price configured for this model


class LLMCost(BaseModel):
    """한 실행의 LLM 비용(모델별, 합계)을 담는 데이터 클래스.
    가격 정보가 없는 모델은 합계에서 빠지고 unpriced_models에 남는다.

    Priced LLM usage of a run (`RunReport.llm_cost`): per model + total. Models without a price are listed in
    `unpriced_models` and left out of `total`; `usage_missing` = calls were made but no token counts came back."""
    currency: str = "USD"
    total: float = 0.0
    by_model: list[ModelCost] = Field(default_factory=list)
    unpriced_models: list[str] = Field(default_factory=list)
    usage_missing: bool = False
    note: str = ""                                 # llm.pricing_note: where the prices come from / as of when


# ---------------------------------------------------------------------------
# 수정 스크립트.
# LLM이 쓴 `def fix(p)`를 AST로 정적 검사한 뒤 별도 `python -I` 프로세스에서 제한된 API `p`로 실행한다
# 저장 위치는 `<kb>/cases/<case_id>/fix_v<N>.py`와 case.json(CaseScript)이고, 아래 영어 설명의
# `<kb>/scripts/...`는 이전 경로다
# v2 fix scripts (flow-v2, docs/v2/PLAN-v2.md §3.2): every build-error fix is a Python script `def fix(p)` written by
# the LLM, checked (AST) and run in a separate `python -I` process against a restricted helper API (sbmig.script),
# stored per case in <kb>/scripts/<case_id>/v<N>.py + v<N>.meta.json (sbmig.kb.scripts)
# ---------------------------------------------------------------------------
# new는 신규 작성(LLM), reuse는 저장된 스크립트 재사용(LLM 없음), augment는 이전 버전 보강(LLM)이다
ScriptMode = Literal["new", "reuse", "augment"]
# 스크립트 실행 결과. error는 예외나 경로 거부, timeout은 script.timeout_seconds가 지나 강제 종료,
# rejected는 정적 검사에서 거부된 경우다
ScriptRunStatus = Literal["ok", "error", "timeout", "rejected"]
# ok = fix(p) returned; error = it raised (or a helper refused a path); timeout = killed after script.timeout_seconds;
# rejected = the static check refused the code before it ran (forbidden import / name / dunder access, no fix(p))


class ScriptRunResult(BaseModel):
    """수정 스크립트를 한 번 실행한 결과를 담는 데이터 클래스. 상태, 바꾼 파일, p.log 줄, 오류를 담는다.
    이것은 스크립트 스스로의 보고이고, 정확한 변경 내용은 수정 루프가 남기는 git diff다.

    What one run of a fix script did (sbmig.script.ScriptRunner.run). The exact change is the git diff the loop
    records; this is the script's own account."""
    status: ScriptRunStatus = "ok"
    changed_files: list[str] = Field(default_factory=list)    # relative posix paths written / created / deleted / moved
    log: list[str] = Field(default_factory=list)              # p.log(...) lines (capped by script.log_lines)
    error: str = ""                                           # exception / refusal / timeout text ("" when ok)
    problems: list[str] = Field(default_factory=list)         # static-check findings when rejected
    duration_s: float = 0.0


class ScriptMeta(BaseModel):
    """이전 KB의 스크립트 메타 정보(`<kb>/scripts/<case_id>/v<N>.meta.json`)를 담는 데이터 클래스.
    지금은 case.json의 CaseScript가 같은 역할을 한다.

    <kb>/scripts/<case_id>/v<N>.meta.json — one stored version of a case's fix script and its record."""
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
    # v2.1y (2026-10-31, prompt review E-H3): the loop ran the script a second time right after `ok` and it changed the
    # tree again (`new JobBuilder(.., jobRepository, jobRepository)`) — kept for the record, never selected for reuse
    # ok로 끝난 직후 다시 실행했을 때 또 파일을 바꾼 스크립트(비멱등).
    # 기록은 남기지만 재사용 대상에서는 뺀다
    non_idempotent: bool = False


class ScriptBase(BaseModel):
    """보강 요청에서 고칠 대상 스크립트를 담는 데이터 클래스.
    이전 버전 코드와 이 프로젝트에서의 결과(남은 오류, 새 오류, 버전 이력)를 담는다.

    The script an augment request extends (FixContext.script_base): the previous version and what it did here."""
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
    session_id: Optional[str] = None               # v2.1z-p: the gateway session the base attempt's answer came from (continued by the augment)
    session_model: str = ""                        # v2.1z-x: the model that session was opened with ("" = the gateway default) — a session
                                                   # cannot change its model, so a different model opens a new session instead


# LLM이 직접 요청한 수정의 종류. replace는 문자열 치환, pom은 pom 편집기 호출이다
EditOp = Literal["replace", "write", "create", "delete", "move", "pom"]


class EditHunk(BaseModel):
    """적용된 diff에서 뽑은 변경 조각 하나를 담는 데이터 클래스.
    수정 전(as-is) 줄과 수정 후(to-be) 줄을 담는다.
    오류별 JSON과 `sbmig kb errors show`에서 사람이 읽으며, 기본으로는 자르지 않는다(`errors.snippet_lines: 0`).

    v2.1e (2026-10-27, user: "as-is 코드와 to-be 코드도 들어갔으면"): one changed region of a file as a person reads
    it — the lines that were there (as-is) and the lines that are there now (to-be), taken from the applied diff.
    Whole by default (`errors.snippet_lines: 0`); people read these in the per-error JSON and `kb errors show`."""
    lines: str = ""                                # "~12" | "+42–47" | "-3" — the same spelling as EditNote.lines
    before: str = ""                               # as-is: the removed / replaced lines ("" for an insertion / new file)
    after: str = ""                                # to-be: the added / replacing lines ("" for a deletion)
    truncated: bool = False                        # errors.snippet_lines cut this hunk


class EditNote(BaseModel):
    """사람이 읽는 수정 내역 한 건. where와 change는 LLM이 쓰고 `lines`와 hunks는 sbmig가 diff에서 채운다.
    LLM이 아무것도 쓰지 않은 파일은 diff로 메모를 만들어 '수정' 칸이 비지 않게 한다.

    One modification as a person reads it (v2.1b, 2026-10-27: "edit 란에 어디의 어떤 줄을 어떻게 수정하는지"):
    the LLM writes `where` / `change` (every `<edits>` item's `desc`, and the `<changes>` list for a script);
    sbmig fills `lines` from the applied diff (`+42–47`, `~12`, `-3`) and, when the LLM wrote nothing for a file the
    diff touched, generates the note from the diff itself so the key is never empty."""
    path: str
    where: str = ""                                # the LLM's location hint ("<dependencies> 블록", "import 줄", "42행 부근")
    change: str = ""                               # "jakarta.xml.bind-api 와 jaxb-runtime 의존성을 추가합니다"
    lines: str = ""                                # from the diff: "+42–47" added, "~12" changed, "-3" removed, several joined by ", "
    source: Literal["llm", "diff"] = "llm"         # who wrote the note
    hunks: list[EditHunk] = Field(default_factory=list)   # v2.1e: as-is / to-be per changed region, in diff order


class Edit(BaseModel):
    """LLM이 요청한 직접 수정 한 건(`<edits>` JSON)을 담는 데이터 클래스.
    sbmig가 수정 스크립트와 같은 헬퍼 API와 안전 검사로 적용한다.
    pom 편집은 `pom` 필드에 편집기 호출을 담는다.

    One direct modification the LLM asked for (`<edits>` JSON, v2.1): applied by sbmig through the script helper
    API (project folder only, same guard as a fix script) — "LLM 이 직접 수정". `pom` edits carry the editor call in
    `pom` (`{"add_dependency": {"group_id": …, "artifact_id": …, "version": null}}`, `set_property`, …)."""
    op: EditOp
    path: str
    desc: str = ""                                 # the LLM's one-line description of this edit (→ EditNote.change)
    old: str = ""                                  # replace: exact text to replace (count = -1 → every occurrence)
    new: str = ""
    count: int = -1
    content: str = ""                              # write / create
    to: str = ""                                   # move
    pom: dict[str, Any] = Field(default_factory=dict)


class ScriptProposal(BaseModel):
    """수정기와 구조 검토의 답.
    원인 analysis, 해결 solution, 바로 적용할 edits, 저장할 스크립트 code, 운영자 확인 verify를 담는다.
    code가 비고 no_script_reason이 있으면 edits가 수정 전부다.

    The fixer's answer (FixerLike.propose_script).
    v2.1 (2026-10-27): 원인 `analysis` · 해결 방법 `solution` ·
    수정 `edits` (applied directly) · 스크립트 `code` (stored for reuse); `no_script_reason` when the LLM says the
    fix cannot be a reusable script (then `edits` is the fix)."""
    code: str = ""                                 # the <script> body (must define `def fix(p)`); "" = no script
    analysis: str = ""
    solution: str = ""
    edits: list[Edit] = Field(default_factory=list)
    changes: list[EditNote] = Field(default_factory=list)   # `<changes>`: what the fix modifies where (script or edits)
    no_script_reason: str = ""
    verify: str = ""                               # `<verify>` 운영자 확인 사항, what the operator must check at
                                                   # runtime after a major version upgrade / a library replaced ("" = none)
    model: str = ""
    prompt_hash: str = ""
    llm_usage: LLMUsage = Field(default_factory=LLMUsage)
    session_id: Optional[str] = None               # v2.1z-o: the gateway session the answer came from (continuation)
    session_model: str = ""                        # v2.1z-x: the model that session was opened with (the `model` sent; "" = default)
    requested_files: list[str] = Field(default_factory=list)
    raw_response: str = ""
    error: str = ""                                # why there is no script (unparsable answer, transport, refusal)
    # v2.1y (2026-10-31, prompt review B-M5 / E-M1): the LLM's `<blocked>why</blocked>` — the case cannot be fixed inside the
    # project (a library without a jakarta build, a framework the owners must replace, an artifact the repository lacks); no edits,
    # no script. The loop classifies such an answer as `declined` → BLOCKED needs_owner (script.max_declines_per_case).
    # LLM의 <blocked> 답. 프로젝트 안에서 고칠 수 없는 이유이며, 루프가 이 답을 declined로 분류한다
    blocked_reason: str = ""


FixAnswer = ScriptProposal                      # the v2.1 name


# ---------------------------------------------------------------------------
# 의존성 버전 정책.
# 메이저는 올리지 않고, 수정 뒤 메이저 업그레이드와 라이브러리 교체를 OperatorCheck로 남긴다.
# 차단은 하지 않는다
# v2.1d dependency version policy (2026-10-27, user: "메이저를 올리지 마라; 같은 메이저에서 jakarta / JDK 21 을 지원하는
# 버전을 찾아 올려라; 메이저를 올렸으면 무엇을 바꿨고 운영자가 무엇을 확인해야 하는지 가이드를 남겨라").
# The LLM is told the
# policy and what the repository has (sbmig.build.repo_versions); after a fix, the pom diff is read back
# (sbmig.build.depchanges) and every major upgrade / library replacement becomes an OperatorCheck in the report's
# Handover ("운영자 확인 필요") — never a block: the build must pass.
# ---------------------------------------------------------------------------
# 변경 종류. 운영자 확인 대상은 major와 replaced다
DependencyChangeKind = Literal["major", "minor", "added", "removed", "replaced"]
# major = the major number of the version changed (2.4.2 → 3.2.1); minor = any other version change (incl. a
# classifier / property change); replaced = one library removed and another added in the same pom that stands in
# for it (same artifactId or one artifactId contains the other); added / removed = the rest
PomSection = Literal["dependencies", "dependencyManagement", "plugins", "pluginManagement", "parent"]


class DependencyChange(BaseModel):
    """pom diff에서 읽은 좌표 변경 한 건을 담는 데이터 클래스.
    시도와 구조 검토에 붙고, major / replaced 변경은 OperatorCheck가 된다.

    One coordinate change read from a pom diff (sbmig.build.depchanges.dependency_changes)."""
    path: str                                      # the pom, project-relative posix
    key: str                                       # groupId:artifactId before the change (added: the new one)
    before: Optional[str] = None                   # version before (None = absent / managed by the parent)
    after: Optional[str] = None                    # version after (replaced: the NEW library's version)
    kind: DependencyChangeKind
    replaced_by: Optional[str] = None              # replaced: the new groupId:artifactId
    section: PomSection = "dependencies"
    approved: bool = False                         # a person's table entry (libraries.yml: allow_major / version)
                                                   # allows this move — still listed for the operator, marked approved
    note: str = ""                                 # the table entry's note / what the repository offered instead

    @property
    def needs_operator(self) -> bool:
        """운영자가 실행 시점에 확인해야 하는 변경(메이저 업그레이드나 라이브러리 교체)인지 확인한다.

        A change the operator must verify at runtime (major upgrade or a library replaced)."""
        return self.kind in ("major", "replaced")

    @property
    def text(self) -> str:
        """보고서에 쓸 한 줄 표기를 만든다. 라이브러리 교체면 새 좌표까지 쓴다.

        `org.sitemesh:sitemesh 2.4.2 → 3.2.1` / `opensymphony:sitemesh 2.4.2 → org.sitemesh:sitemesh 3.2.1`."""
        a = f"{self.replaced_by} {self.after or '-'}" if self.replaced_by else (self.after or "-")
        return f"{self.key} {self.before or '-'} → {a}"


class VersionCandidate(BaseModel):
    """저장소에 있는 릴리스 하나를 담는 데이터 클래스.
    jakarta 빌드 여부, 정식 릴리스 전 버전인지 여부, pom에 기록된 최소 Java 버전을 담는다.

    One release the repository has (sbmig.build.repo_versions)."""
    version: str
    jakarta: Optional[bool] = None                 # its pom depends on jakarta.* (None = pom not checked / unknown)
    prerelease: bool = False                       # milestone / RC / alpha / beta / SNAPSHOT
    java: Optional[int] = None                     # minimum Java the pom states (maven.compiler.release / target)


class RepositoryVersions(BaseModel):
    """설정된 저장소에 있는 라이브러리 하나의 버전 목록을 담는 데이터 클래스.
    수정 프롬프트의 "Dependency versions"에 정보로 넣어 LLM이 같은 메이저 안에서 버전을 고르게 한다.

    What the configured repository (settings.xml mirror / build.repository_url / Maven Central) has for one
    library — the facts the fix prompt lists under "Dependency versions" (sbmig.build.repo_versions.RepositoryLookup)."""
    key: str                                       # groupId:artifactId
    current: Optional[str] = None                  # the version the project uses (None = unknown)
    major: Optional[int] = None                    # major of `current`
    same_major: list[VersionCandidate] = Field(default_factory=list)   # releases of the current major newer than
                                                   # `current`, oldest → newest (prereleases only when configured)
    higher: list[VersionCandidate] = Field(default_factory=list)       # the newest release of every higher major
    first_jakarta: Optional[str] = None            # lowest checked release whose pom is a jakarta build (any major)
    source: str = ""                               # metadata URL / local repository path / "" (nothing found)
    error: str = ""                                # why the lookup gave nothing (timeout, 404, offline, no url)
    # v2.1z-e: every repository URL asked for the maven-metadata.xml and what it answered ("<url> → ok (5 release(s))",
    # "<url> → HTTP 401 (credentials?)", "<url> → timeout …"), the local repository last — a run is diagnosable from the
    # journal / the prompt facts alone, and "no newer release" is never mistaken for a fact about an unreachable Nexus
    urls_tried: list[str] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=utcnow)


class OperatorCheck(BaseModel):
    """운영자 확인이 필요한 항목을 담는 데이터 클래스.
    최종 트리에 남은 메이저 업그레이드나 라이브러리 교체와 그 확인 가이드다.
    실행이 끝날 때 시도와 구조 검토에서 만들며, 되돌린 시도의 변경은 넣지 않는다.

    v2.1d: a modification the operator must verify at runtime — a major version upgrade or a library replaced by
    another one. Report Handover "운영자 확인 필요" (first block), report.json `operator_checks`, `SBMIG-RESULT …
    major=<n>`. Built at the end from the error list's tries (+ the structure review) for changes still in the
    final tree (a rolled-back try is not listed)."""
    kind: Literal["major_upgrade", "library_replaced"] = "major_upgrade"
    change: DependencyChange
    case_id: str = ""                              # "" for the structure review
    source: str = ""                               # "fix r3 try 2" | "review"
    commit: str = ""
    guide: str = ""                                # 운영자 확인 사항: the LLM's <verify> text, or sbmig's synthesized lines
    guide_source: Literal["llm", "sbmig"] = "llm"
    files: list[str] = Field(default_factory=list) # files the same try changed (what the port touched)


# ---------------------------------------------------------------------------
# 오류 목록. 실행 중 생긴 빌드 오류나 pom 수정 한 건이 ErrorEntry 하나다
# 실행 디렉토리의 errors.jsonl에 기록하고, 실행이 끝나면 KB의 errors.jsonl에 추가한다
# v2.1 error list (2026-10-27, docs/v2/PLAN-v2.1.md §3.3): ONE record per build error / pom correction of a run —
# <run dir>/errors.jsonl, appended to <kb>/errors.jsonl at the end; what it was, every try with the LLM's answer and
# the applied diff, how it ended. Replaces ErrorCaseRecord / FixAttemptRecord / the case catalog / reviews.jsonl.
# ---------------------------------------------------------------------------


class FixTry(BaseModel):
    """케이스에 대한 시도 한 건을 담는 데이터 클래스.
    ErrorList.begin_try가 만들고, 그 케이스에 속한 열린 오류들이 같은 객체를 공유한다.
    출처, 원인, 해결 방법, 수정 내역, diff, 커밋, 재빌드 결과를 담는다."""
    n: int                                         # 1, 2, … within the entry's case and run
    round: int = 0                                 # fix round (0 = review)
    source: TrySource
    script_version: int = 0                        # stored version run / created (0 = none)
    analysis: str = ""                             # 원인
    solution: str = ""                             # 해결 방법
    no_script_reason: str = ""
    applied: TryApplied = "none"
    files: list[str] = Field(default_factory=list)
    edits: list[EditNote] = Field(default_factory=list)   # 수정: per change, where / what (LLM) + lines (diff)
    major_upgrades: list[DependencyChange] = Field(default_factory=list)   # v2.1d: major / replaced changes this try
                                                   # made in poms (read back from its diff)
    verify: str = ""                               # 운영자 확인 사항(LLM의 <verify> 또는 sbmig가 만든 가이드)
    verify_source: Literal["", "llm", "sbmig"] = ""   # who wrote the guide ("" = no major upgrade)
    diff: str = ""                                 # unified diff of what was applied (capped by errors.diff_chars)
    commit: str = ""
    # 재빌드 결과. 수정 루프가 다음 라운드 빌드를 보고 채운다
    result: AttemptOutcome = "pending"
    note: str = ""                                 # failure text, rollback, script error …
    llm_calls: int = 0
    seconds: float = 0.0


class ResolvedBy(BaseModel):
    """자기 시도 없이 빌드에서 사라진 오류에 대해, 같은 라운드에 그 파일이나 상위 pom을 바꾼 시도를 담는
    데이터 클래스. 그 시도가 실제로 오류를 고쳤을 가능성이 높다.
    오류 목록이 만들고, `sbmig kb reindex`가 이전 기록에도 채운다.

    v2.1f (2026-10-28, user: "뭘 어떻게 수정했다 이런 건 안 적혀 있는데"): an error that disappeared from a build WITHOUT a try
    of its own (`ErrorEntry.resolved == "build"`) names the applied try of the round that touched its file or its
    module's pom — the probable fix, so a case with no script is not a mystery in the KB."""
    case_id: str = ""                              # the try's case
    round: int = 0
    n: int = 0
    commit: str = ""
    files: list[str] = Field(default_factory=list) # the try's changed files that match (the entry's file / a pom above it)
    match: Literal["file", "pom", "reindex"] = "file"   # how it was matched (reindex = backfilled from errors.jsonl)
    text: str = ""                                 # one line: "7a52b1cea8 r1 try 1 — module/comp-jar/pom.xml +28–36 — jakarta.xml.bind-api 추가"


class ErrorEntry(BaseModel):
    """오류 목록의 레코드 하나를 담는 데이터 클래스. 빌드 오류, pom 수정, 구조 검토 메모 중 하나다.
    ErrorList가 만들고 수정 루프가 시도를 붙이며, 보고서, errors.md, KB가 읽는다."""
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
    # 오류를 처음 발견한 단계. fix r3은 수정 루프의 3번째 라운드다
    seen: str = ""                                 # compile | restructure | review | build | fix r3
    status: EntryStatus = "open"
    tries: list[FixTry] = Field(default_factory=list)
    # 오류가 해결된 곳.
    # "build"는 자기 시도 없이 사라진 경우이며, 해결했을 것으로 보이는 시도를 resolved_by에 남긴다
    resolved: str = ""                             # "r3 try 2" | "build" (gone without a try) | "review"
    resolved_by: list[ResolvedBy] = Field(default_factory=list)   # v2.1f: "build" → the same-round try that probably
                                                   # fixed it (nearest first: same file, then its module pom, then root)
    script: str = ""                               # cases/<case_id>/fix_v2.py — the script that resolved it
    note: str = ""                                 # pom-verify: what the restructure did; note entries: the text
    at: datetime = Field(default_factory=utcnow)
    # 서명 정보. L2를 만든 규칙 버전과, 규칙이 바뀌었을 때 L2를 다시 계산하는 데 필요한 값이다
    # 이전 기록에는 비어 있으므로(sig 0) 그때는 raw의 symbol / location 줄에서 다시 읽는다
    # signature facts (v2.1z-ai): the rule version that produced `l2`, and what a later rule version needs to
    # compute it again. Entries stored before this have none (sig 0 = rules 1): the re-sign reads `raw` instead.
    sig: int = 0                                   # sbmig.build.signatures.SIGNATURE_VERSION when `l2` was computed
    l1: str = ""                                   # BuildError.signature (the specific signature)
    symbol: str = ""                               # javac "symbol:" line (masked like `message`)
    location: str = ""                             # javac "location:" line
    artifact: str = ""                             # g:a[:v] of a resolution / pom error
    goal: str = ""                                 # plugin goal ("compiler:compile", "war:war")
    was: list[str] = Field(default_factory=list)   # earlier case ids of this entry (re-signed), oldest first


# 저장된 스크립트의 상태. proven은 다른 프로젝트에서 재사용해 해결한 것, retired는 사람이 끈 것이다.
# 재사용할 때는 proven을 먼저 고른다
CaseScriptStatus = Literal["unverified", "proven", "retired"]
# unverified = stored from an answer whose edits fixed this project (or run here once); proven = resolved the case in
# another project on reuse; retired = a person switched it off (`sbmig kb cases retire`)


class ScriptRun(BaseModel):
    """저장된 스크립트 버전 하나가 한 프로젝트에서 실행된 기록을 담는 데이터 클래스.
    결과와 그 이유를 담는다.
    왜 보강했는지를 사람과 이후의 유사도 비교 단계가 확인할 수 있게 한다.

    v2.1e (user: "왜 실패했는지 이유도 json 에 실렸으면"): one run of a stored script version in one project — the
    scored result and the reason in words (the try's note: errors left, new errors, the script's exception / timeout
    / static-check refusal), so people and a later similarity step see why a version was augmented."""
    run_id: str = ""
    project: str = ""
    round: int = 0                                 # fix round of the try
    n: int = 0                                     # try number within the case and run
    result: str = ""                               # AttemptOutcome as scored (improved / build_ok / partial / no_change /
                                                   # regressed / reverted / apply_failed / inconclusive / blocked)
    reason: str = ""                               # why (the try's note) — "" when it simply resolved the case
    errors_before: Optional[int] = None            # the case's errors before the run
    errors_left: Optional[int] = None              # still there after the rebuild
    errors_new: Optional[int] = None               # new errors in the touched files (regressions)
    files: list[str] = Field(default_factory=list) # files the run changed
    at: datetime = Field(default_factory=utcnow)


class CaseScript(BaseModel):
    """case.json 안에 저장된 스크립트 버전 하나(fix_v<N>.py)의 정보를 담는 데이터 클래스.
    상태, 성공 / 실패 수, 어느 버전에서 파생됐는지, 실행 이력을 담는다.
    CaseStore가 관리하고, CaseStore.best가 재사용할 버전을 고를 때 읽는다."""
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
    # v2.1e lineage and history (docs/INTERFACES.md "v2.1e")
    parent: Optional[int] = None                   # augment: the version this one extends (None = written new)
    reason: str = ""                               # why this version was made: the parent's failure in words
                                                   # ("v1: no_change — 3 error(s) remained: …"), "" for a new script
    last_failure: str = ""                         # the most recent failed run's reason (quick view; runs[] has all)
    runs: list[ScriptRun] = Field(default_factory=list)   # newest last, capped by errors.script_runs
    # v2.1y (2026-10-31, prompt review E-H3): the loop ran the script a second time right after `ok` and it changed the tree
    # again (`new JobBuilder(.., jobRepository, jobRepository)`) — kept for the record, never selected for reuse
    # ok로 끝난 직후 다시 실행했을 때 또 파일을 바꾼 스크립트(비멱등).
    # 기록은 남기지만 재사용 대상에서는 뺀다
    non_idempotent: bool = False


class CaseInfo(BaseModel):
    """KB 케이스 하나(`<kb>/cases/<case_id>/case.json`)를 담는 데이터 클래스.
    L2 서명, 이름, 프로젝트, 저장된 스크립트, 마지막 원인과 해결 방법을 담는다.
    CaseStore가 기록하고 KB 조회, cases.md, 플릿 요약이 읽는다.
    사람이 설정하는 `pin`(block)은 이 모델에 없는 별도 키다.

    <kb>/cases/<case_id>/case.json — the case (L2) and its scripts."""
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
    # `l2`를 만든 서명 규칙 버전(0은 버전 도입 전)과, 재서명으로 이 케이스에 합쳐진 이전 케이스 ID 목록
    # the signature rule version of `l2` (0 = stored before versions existed) and the earlier case ids whose
    # content a re-sign moved here (`sbmig kb cases show <old id>` still finds the case through them)
    sig: int = 0
    aliases: list[str] = Field(default_factory=list)
    # case.json may carry extra keys the store keeps beside the model: "pin" ("block[:<reason>]", a person's
    # instruction never to try the case) and "last_result" (sbmig.errors.cases.CaseStore.extra)


class ReviewIssue(BaseModel):
    """구조 검토가 찾은 문제 하나를 담는 데이터 클래스.
    critical은 바로 고치고, warning은 보고서와 오류 목록 메모에만 남긴다."""
    severity: Literal["critical", "warning"] = "warning"
    file: str = ""                                 # relative pom / module path ("" = project-wide)
    problem: str = ""
    fix_hint: str = ""
    # v2.1y (2026-10-31, prompt review E-M5): the issue names a version / property / coordinate that is nowhere in the review
    # material (a stale replay, a hallucination) — kept in the report's "Structure review" with the mark "(sbmig check) not in the
    # material", never applied, never in the Handover. 검토 자료에 없는 버전이나 속성을 지적한 문제다.
    # 보고서에만 남기고 인수인계에는 넣지 않는다
    unsupported: bool = False


class StructureReview(BaseModel):
    """구조 검토 결과를 담는 데이터 클래스. 재구성 뒤 첫 빌드 전에 LLM이 pom, 의존성, 모듈을 회사 규칙으로
    검토한 결과, 문제 목록, critical 수정과 그 커밋을 담는다.
    보고서의 'Structure review'에 나오고, 오류 목록에는 review 레코드로 남는다.

    v2 structure review (sbmig.review): the LLM checks poms / dependencies / module layout after the restructure,
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
    major_upgrades: list[DependencyChange] = Field(default_factory=list)   # v2.1d: major / replaced changes of its commit
    commit: Optional[str] = None                   # commit of the script's changes (revertible)
    reverted: bool = False
    llm_usage: LLMUsage = Field(default_factory=LLMUsage)
    note: str = ""                                 # why skipped / failed, or what happened to the script

    @property
    def critical(self) -> list[ReviewIssue]:
        """severity가 critical인 문제만 반환한다."""
        return [i for i in self.issues if i.severity == "critical"]


class FixAttempt(BaseModel):
    """수정 루프의 시도 한 건을 담는 내부 기록용 데이터 클래스.
    처리 방법, 바꾼 파일, 커밋, 전후 오류 수, 결과, 스크립트 정보를 담는다.
    수정 루프가 만들고 결과를 평가한 뒤 오류 목록의 FixTry로 옮긴다."""
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
    llm_session_id: Optional[str] = None           # v2.1z-o: the gateway session of this attempt's answer (the next rung may continue it)
    llm_session_model: str = ""                    # v2.1z-x: the model that session runs on — the next rung continues it only with the same model
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
    source: Optional[TrySource] = None             # kb-script | llm-* | review | sbmig-version | sbmig-resources
    solution: str = ""                             # 해결 방법 (the LLM's <solution>)
    no_script_reason: str = ""
    edits_applied: int = 0                         # direct edits applied (0 = a script ran)
    # v2.1d: major upgrades / library replacements this attempt made in poms, and the operator guide for them
    major_upgrades: list[DependencyChange] = Field(default_factory=list)
    verify: str = ""                               # 운영자 확인 사항 (the answer's <verify>, or synthesized)
    verify_source: Literal["", "llm", "sbmig"] = ""


class IterationRecord(BaseModel):
    """수정 루프 한 라운드의 요약을 담는 데이터 클래스.
    빌드 성공 여부, 오류 수, 서명별 오류 수, 클러스터와 처리 방법, 진척 없음 수준을 담는다."""
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
    """단계 하나의 결과를 담는 데이터 클래스.
    성공 여부, 반복 횟수, 레시피 결과, 시도, 남은 오류, 차단 항목을 담는다.
    오케스트레이터가 단계마다 RunReport.phases에 추가한다."""
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
# 이전 KB의 학습용 수정 라벨. 지금 코드는 쓰지 않고 이전 기록을 읽을 때만 쓴다
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
    """클러스터의 케이스에 대해 KB가 가진 정보(CaseHit 목록과 프롬프트 힌트 줄)를 담는 데이터 클래스.
    수정 루프가 라운드마다 받아서 재사용, 보강, 신규 중 하나를 고른다.

    What the KB knows about a cluster's case (`FileKnowledgeBase.match`): the catalog hits and their hint lines."""
    hints: list[str] = Field(default_factory=list)               # short natural-language hints for the script prompt
    case_hits: list[Any] = Field(default_factory=list)           # CaseHit (case catalog, 2026-10-23)


# ---------------------------------------------------------------------------
# 이전 KB의 오류 사례 기록. 지금은 ErrorEntry가 대신하므로 아래 모델은 채우지 않는다
# 이전 KB와 report.json을 읽기 위해 남겨 둔 형식이다
# Error cases (2026-10-23, business requirement): every build error of a run is recorded on its own — what it was,
# what was tried, how it was resolved (with the code applied) or why it was not — and every fix attempt, failed
# ones included. kb/errors.jsonl + kb/fix_attempts.jsonl (append-only), kb/error_hints.json (hints index),
# <run dir>/error_cases.jsonl | fix_attempts.jsonl | error_cases.csv (record models kept for reading older files).
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
    """이전 기록을 읽기 위한 데이터 클래스. 텍스트 치환에서 old가 new로 count번 바뀐 것을 담는다.

    One token-level substitution of a text-replace change: `old` became `new` `count` times (in `files`)."""
    old: str
    new: str
    count: int = 1
    files: list[str] = Field(default_factory=list)            # relative posix paths


class ChangeClass(BaseModel):
    """이전 기록을 읽기 위한 데이터 클래스. 변경이 텍스트로 어떻게 보이는지의 분류를 담는다.

    How a change looks textually — `sbmig.kb.changes.classify_diff(diff_text)`."""
    change_kind: ChangeKind = "none"
    replace_pairs: list[ReplacePair] = Field(default_factory=list)
    file_types: list[str] = Field(default_factory=list)       # java | pom | xml | properties | yaml | jsp | other
    files: list[str] = Field(default_factory=list)            # relative posix paths the diff touches
    lines_added: int = 0
    lines_removed: int = 0


# 이전 기록용 FixAttemptRecord / ErrorCaseRecord / StructureReview의 스키마 번호
KB_RECORD_SCHEMA = 3      # 1 (or absent) = before round 10 (2026-10-26); 2 = round 10; 3 = v2 fix scripts
                          # (flow-v2: script_* fields, fix methods script / script_reuse / script_augment)


class FixAttemptRecord(BaseModel):
    """이전 기록을 읽기 위한 데이터 클래스.
    kb/fix_attempts.jsonl 한 줄이 시도 한 건이며, 지금은 ErrorEntry.tries가 대신한다.

    One line of kb/fix_attempts.jsonl: one fix attempt of a run — success or failure — with the code it applied."""
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
    """이전 기록을 읽기 위한 데이터 클래스.
    오류 이력에 있는 시도 요약 한 줄(ErrorCaseRecord.attempts)을 담는다.

    One attempt in an error's history (`ErrorCaseRecord.attempts`) — the short form of a FixAttemptRecord."""
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
    """이전 기록을 읽기 위한 데이터 클래스. kb/errors.jsonl 한 줄이 빌드 오류 한 건이며, 지금은 ErrorEntry를
    쓰므로 RunReport.error_cases는 비어 있다.

    One line of kb/errors.jsonl: ONE build error of a run (N errors in a build → N records): what it was, what
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
# 이전 케이스 카탈로그(kb/catalog/…yml). 지금은 `cases/<case_id>/case.json`을 쓴다
# 아직 쓰는 것은 KB 조회 결과인 CaseAction, CaseHit와 그 Literal(CaseStatus, CaseActionKind)뿐이다
# Case catalog (2026-10-23): ONE reusable case per error signature (L2 = the case key), variants by the owner class /
# L1 — kb/catalog/<kind>/<class>/<symbol>--<id>.yml. Built from the error-case events (kb/errors.jsonl,
# fix_attempts.jsonl), the KB recipes and patch cases; the `human` block is never overwritten by the KB, every other
# field is recomputed. A hit at triage applies the case's action.
# ---------------------------------------------------------------------------
CaseStatus = Literal["observed", "candidate", "active", "rejected"]
CaseActionKind = Literal["hint", "block", "script"]
# script = run the case's best stored fix script (CaseAction.script_version); block = never try (BlockReason +
# suggestion); hint = nothing proven yet (the case's hints go into the script prompt)


class CaseVariant(BaseModel):
    """이전 카탈로그를 읽기 위한 데이터 클래스. 케이스의 변형 하나(소유 클래스 / L1 서명)를 담는다."""
    owner: str = ""                                # FQCN owning the symbol (javac location / symbol owner); "" unknown
    signature: str = ""                            # L1
    seen: int = 0
    projects: int = 0


class CaseFix(BaseModel):
    """이전 카탈로그를 읽기 위한 데이터 클래스. 케이스를 고친 방법 하나를 담는다.

    One way this case was fixed (grouped by method + resolver + replace pairs), the latest attempt as example."""
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
    """이전 카탈로그를 읽기 위한 데이터 클래스. 케이스에서 실패한 방법 하나를 담는다."""
    method: FixMethod = "none"
    outcome: AttemptOutcome = "no_change"
    failure: str = ""
    attempt_id: str = ""
    project: str = ""
    count: int = 0
    projects: int = 0


class CaseEvent(BaseModel):
    """이전 카탈로그를 읽기 위한 데이터 클래스. 한 실행에서 본 케이스의 타임라인 항목을 담는다.

    Timeline entry: this case seen in one run (one ErrorCaseRecord)."""
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
    """KB 조회가 케이스에 대해 정한 행동을 담는 데이터 클래스.
    FileKnowledgeBase.match가 만들고 트리아지가 따른다.
    - script: 저장된 스크립트 버전을 실행한다
    - block: 사람이 차단으로 고정했으므로 시도하지 않는다
    - hint: 힌트만 준다"""
    kind: CaseActionKind = "hint"
    block_reason: str = ""                                    # block: BlockReason value + suggestion
    suggestion: str = ""
    script_version: int = 0                                   # script: <kb>/scripts/<case id>/v<N>.py


class CaseEvidence(BaseModel):
    """이전 카탈로그를 읽기 위한 데이터 클래스. 케이스의 근거 집계를 담는다."""
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
    """이전 카탈로그를 읽기 위한 데이터 클래스. 사람이 편집하는 부분이므로 자동 재계산이 덮어쓰지 않는다.

    Edited by people (YAML or `sbmig kb cases …`); never overwritten by harvest / reindex; wins over the auto part."""
    name: Optional[str] = None
    status: Optional[CaseStatus] = None            # e.g. rejected, or active by decision
    action: Optional[CaseAction] = None            # a pinned action
    hints: list[str] = Field(default_factory=list)
    notes: str = ""


class CatalogCase(BaseModel):
    """이전 카탈로그의 케이스 하나를 읽기 위한 데이터 클래스. 지금은 CaseInfo를 쓴다."""
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
        """사람이 정한 상태가 있으면 그 값을, 없으면 자동으로 정한 상태를 반환한다."""
        return self.human.status or self.status

    def effective_action(self) -> CaseAction:
        """사람이 정한 행동이 있으면 그 값을, 없으면 자동으로 정한 행동을 반환한다."""
        return self.human.action or self.action

    def effective_name(self) -> str:
        """사람이 붙인 이름이 있으면 그 값을, 없으면 자동으로 만든 이름을 반환한다."""
        return self.human.name or self.name


class CaseHit(BaseModel):
    """KB 조회에서 맞는 케이스 하나를 담는 데이터 클래스.
    케이스, 상태(active는 이전에 성공, candidate는 미검증), 행동, 해당 오류, 힌트를 담는다.
    트리아지와 수정 루프가 재사용을 시도할 때 쓴다.

    A catalog match for some errors of a cluster (effective status / action already applied)."""
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
    """`sbmig fleet`에서 프로젝트 하나의 결과를 담는 데이터 클래스. fleet/state.json과 플릿 요약에 쓴다."""
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
    llm_calls: int = 0                              # v2.1h: every attempt of the project added up (A9)
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
    """fleet/state.json에 저장하는 플릿 진행 상태 클래스.
    프로젝트가 끝날 때마다 파일 전체를 원자적으로 다시 쓰므로 중단된 뒤에도 이어서 실행할 수 있다.

    fleet/state.json — resumable; rewritten atomically after every project."""
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
# 500개 프로젝트가 모두 완벽할 수는 없으므로 block 정책은 일부러 두지 않는다
# 권고는 guide뿐이며 수정 프롬프트 힌트와 보고서 인수인계 섹션으로 전달한다.
# 아래 영어 설명의 recipe 정책은 이전 내용이다
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
    """knowledge/libraries/advisories.yml의 권고 항목 하나를 담는 데이터 클래스.
    회사가 추가한 항목은 advisories.extra_files에서 읽어 id 기준으로 합친다.

    One entry of knowledge/libraries/advisories.yml (company additions: advisories.extra_files, merged by id)."""
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
    """이 프로젝트에 해당하는 권고 하나를 담는 데이터 클래스.
    분석 뒤에 만들고 마무리 단계에서 outcome(replaced / still-present)을 확인한다.
    수정 프롬프트 힌트, 인수인계 섹션, report.json의 `advisories`에 쓴다.

    A LibraryAdvisory that matched this project. Created after analyze, enriched by the planner (recipes) and
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
        """실행 뒤 그 라이브러리가 더 이상 보이지 않는지(outcome == replaced) 확인한다."""
        return self.outcome == "replaced"


class MergeConflict(BaseModel):
    """`layout.multi_module_policy: merge`에서 여러 as-is 모듈에 같은 경로로 있던 파일 하나를 담는 데이터 클래스.
    루트 `<modules>` 순서에서 첫 모듈의 파일을 남기고, 나머지는 실행
    디렉토리에 저장해 인수인계의 'Merge conflicts'에 보여 준다.

    `layout.multi_module_policy: merge`: one path that existed in more than one as-is module (or the root `src/`).
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
    """인수인계 '바뀐 버전' 표의 한 줄을 담는 데이터 클래스.
    as-is pom과 최종 pom의 버전을 g:a 기준으로 비교한다.
    carried_over는 새 부모나 BOM이 관리하지 않아 as-is 버전으로 고정한 것이므로 소유 팀이 확인해야 한다.

    One row of the handover's 'versions replaced' table: as-is vs final pom(s), whole project, keyed by g:a."""
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
    # 대상 Boot BOM에서 가져와 고정한 버전이 as-is 버전과 다른 경우. 소유 팀이 알아야 하는 항목이다
    boot_bom_pin: bool = False                     # pinned from the target Boot BOM and different from the as-is one


class HandoverSummary(BaseModel):
    """인수인계 요약을 담는 데이터 클래스. sbmig가 어디까지 바꿨고 이후 무엇이 소유 팀의 일인지를 담는다.
    부모, Java, Boot 전후 버전, 버전 변경, 추가와 제거, 팀이 할 일, 병합 충돌, 설정할 키가 들어가며
    report.handover.build_handover가 만든다.

    'Here is what we replaced, from here on it is yours' — compressed for the owning team. Built at finalize
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
class JarCoordinate(BaseModel):
    """Ant 프로젝트 lib의 jar 하나와 찾아낸 Maven 좌표를 담는 데이터 클래스.
    접수 단계의 pom 생성이 만들고 보고서의 Handover가 읽는다.
    ``source``는 좌표를 어디서 찾았는지, ``used``는 소스가 이 jar의 패키지를 import하는지 여부다(None은 확인하지 않음).

    One jar of an Ant project's lib directory and the Maven coordinates found for it (v2.1j)."""
    path: str                                      # repo-relative jar path (e.g. WebContent/WEB-INF/lib/commons-lang-2.6.jar)
    sha1: str = ""
    group_id: str = ""
    artifact_id: str = ""
    version: str = ""
    source: Literal["pom_properties", "name_table", "sha1_lookup", "manifest", "unknown"] = "unknown"
    scope: str = "compile"                         # provided for servlet / jsp / el APIs the container supplies
    used: Optional[bool] = None                    # a source file imports a package of this jar (None = not checked)
    packages: list[str] = Field(default_factory=list)   # top-level packages inside the jar (handover, the `used` check)
    note: str = ""
    # v2.1z-i: EVERY package holding a class (uncapped; `packages` above is the capped display list) — the
    # bundled-jar provider check (`sbmig.analyze.bundled_jars.providers`) answers "which jar holds package p" from it
    provides: list[str] = Field(default_factory=list)

    @property
    def key(self) -> str:
        """``g:a``를 반환한다. 좌표가 없으면 빈 문자열을 반환한다."""
        return f"{self.group_id}:{self.artifact_id}" if self.group_id and self.artifact_id else ""


class PomSynthesis(BaseModel):
    """접수 단계에서 Ant 프로젝트용으로 생성한 pom.xml의 기록을 담는 데이터 클래스.
    좌표, build.xml에서 읽은 레이아웃, jar 좌표 표, 좌표를 모르는 jar, 옮기지 못한 타깃을 담는다.
    오케스트레이터가 ``RunReport.pom_synthesis``에 넣고 보고서가 읽는다.

    What the intake synthesized for an Ant project (v2.1j): the pom's coordinates and packaging, the layout read from
    build.xml, the jar table, the unknown jars that sources use, the jars left out as unused, the build.xml targets
    that could not be translated."""
    ok: bool
    pom_path: str = "pom.xml"
    build_file: str = "build.xml"
    group_id: str = ""
    artifact_id: str = ""
    version: str = ""
    packaging: str = "jar"                         # war when a web root with WEB-INF (or a <war> task) exists
    source_dirs: list[str] = Field(default_factory=list)
    web_root: Optional[str] = None                 # directory holding WEB-INF (WebContent, web, WebRoot, …)
    resource_dirs: list[str] = Field(default_factory=list)
    lib_dirs: list[str] = Field(default_factory=list)
    java_source: str = "1.8"
    encoding: str = "UTF-8"
    jars: list[JarCoordinate] = Field(default_factory=list)
    unknown_jars: list[str] = Field(default_factory=list)      # source unknown AND used (or not checked) → system / skip
    dropped_jars: list[str] = Field(default_factory=list)      # unknown and unused → not declared
    build_xml_notes: list[str] = Field(default_factory=list)   # targets / tasks not translated (code generation, copies, …)
    warnings: list[str] = Field(default_factory=list)
    error: Optional[str] = None

    @property
    def counts(self) -> dict[str, int]:
        """좌표를 찾은 출처별 jar 수를 반환한다. 보고서의 한 줄 요약에 쓴다."""
        out: dict[str, int] = {}
        for j in self.jars:
            out[j.source] = out.get(j.source, 0) + 1
        return out


# 실행 방식. full은 이관 전체이며, 성공하면 설정에 따라 취약점 조치가 이어진다.
# remediation은 이관 없이 업로드한 프로젝트의 취약점 조치만 한다
# CLI `--flow`, 웹 `POST /api/runs?flow=`, 화면의 "전체 진행" / "취약점 해결"과 대응한다
FLOW_FULL = "full"
FLOW_REMEDIATION = "remediation"
RUN_FLOWS = (FLOW_FULL, FLOW_REMEDIATION)


class RunReport(BaseModel):
    """실행 한 번의 최종 보고서(report.json, report.md의 원본)를 담는 데이터 클래스.
    오케스트레이터가 단계마다 채우고, 마무리 단계에서 인수인계, 운영자 확인, 오류 목록을 붙인다.
    `success`는 최종 검증 빌드가 성공했다는 뜻이다. 최종 빌드 없이 끝나는 실행은 없다."""
    pom_synthesis: Optional["PomSynthesis"] = None   # v2.1j: set when the intake synthesized a pom.xml for an Ant project
    # 이 실행의 방식.
    # full은 이관(뒤에 취약점 조치가 이어질 수 있음), remediation은 이관 없이 취약점 조치만 한다
    flow: str = "full"                             # RUN_FLOWS: "full" | "remediation"
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
    operator_checks: list[OperatorCheck] = Field(default_factory=list)   # 운영자 확인 필요 (major upgrades /
                                                   # library replacements still in the final tree, with their guide)
    # 빌드 성공 뒤 이어서 실행한 취약점 조치의 요약(``sbmig.remediations.chain.summary``)
    # 이관의 성공 여부나 종료 코드에는 쓰지 않으며, 보고서의 "취약점 조치" 절과 SBMIG-RESULT의
    # remediation=만 이 값을 읽는다
    remediation: Optional[dict[str, Any]] = None
    outputs: dict[str, str] = Field(default_factory=dict)        # project_dir, zip, diff, journal, report_md, report_json
    timings_s: dict[str, float] = Field(default_factory=dict)


# MigrationPlan이 아래에서 정의하는 LLMUsage를 문자열로 참조하므로 모든 모델을 정의한 뒤에 다시 빌드한다
MigrationPlan.model_rebuild()
