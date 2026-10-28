"""공유 데이터 모델(계약) — 패키지끼리 주고받는 pydantic 모델·Enum·Literal 이 전부 이 파일에 있다.
흐름 순서로: 분석(ProjectProfile, PomInfo, SourceStats) → 계획(MigrationPlan, PlanStep) → 빌드(BuildResult,
BuildError) → 수정 루프(ErrorCluster, FixAttempt, ScriptProposal, Edit) → v2.1 오류 목록(ErrorEntry, FixTry)과
KB 케이스(CaseInfo, CaseScript) → 보고서(RunReport, HandoverSummary, OperatorCheck) → 일괄 실행(FleetState).
바꿀 때는 docs/INTERFACES.md 에 적는다(플러그인·report.json·KB 파일이 이 모양에 의존). 경로 필드는 프로젝트
루트 기준 상대 경로다(`_abs` 로 끝나는 것과 로그·출력 경로 제외). ErrorCaseRecord / FixAttemptRecord /
CatalogCase 계열, ScriptMeta, FixLabel 은 v1·라운드 10·v2 초기
KB 의 모양으로, v2.1 코드는 채우지 않는다(옛 기록 읽기용).

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
    """UTC 현재 시각(timezone 포함). 모든 레코드 시각 필드의 기본값이다."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class CaseType(str, Enum):
    """분석이 정하는 프로젝트 종류(회사의 네 가지 경우 + unknown).
    계획(레시피·재구성 모드)과 보고서가 이 값으로 갈린다."""
    PURE_JAVA = "pure_java"            # case 1: in-house pure Java (no Spring, no servlet)
    SPRING_LEGACY = "spring_legacy"    # case 2a: Spring Framework 4/5 without Boot (xml / annotation config)
    SPRING_BOOT = "spring_boot"        # case 2b: Spring Boot 2.x / 3.x
    SERVLET_TOMCAT = "servlet_tomcat"  # case 3: raw servlet / JSP on Tomcat (war, web.xml)
    MSA = "msa"                        # case 4: several deployable services in one repo
    UNKNOWN = "unknown"


class Phase(str, Enum):
    """실행 단계 이름. v2.1 이 실제로 쓰는 것은 intake, analyze, plan, transform, restructure, verify, report
    이고, jdk21 / spring 은 v1 단계 이름이다(수정 루프 내부 기본값·옛 기록 호환으로만 남음)."""
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
    """계획 단계(PlanStep)의 종류. 오케스트레이터가 이 값으로 단계 실행 방법을 고른다. v2.1 계획은
    openrewrite / build / restructure / msa_pom / review 만 만든다(build_fix_loop 은 v1 의 것)."""
    OPENREWRITE = "openrewrite"
    RESTRUCTURE = "restructure"    # non-MSA layout transform
    MSA_POM = "msa_pom"            # MSA: pom-only parent/mandatory/optional edits
    BUILD_FIX_LOOP = "build_fix_loop"
    BUILD = "build"                # single build without fixing (verify)
    REVIEW = "review"              # v2: LLM structure review of poms / dependencies / modules before the first build


class ErrorKind(str, Enum):
    """빌드 로그 파서가 오류 한 건에 붙이는 종류. L2 서명의 첫
    조각이라 케이스 분류·차단 정책·보고서 묶음에 쓰인다."""
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
    """트리아지가 클러스터(케이스)마다 내리는 결정: skip(이번 라운드는 대기) / escalate(포기, 보고서로) /
    blocked(이 프로젝트 안에서는 못 고침) / script(저장된·보강·신규 스크립트 시도)."""
    SKIP = "skip"              # masked by another cluster this iteration
    ESCALATE = "escalate"      # give up on this cluster; goes to report
    BLOCKED = "blocked"        # cannot be fixed inside this project (external library / infra) → skip now, report
    SCRIPT = "script"          # v2: run a fix script for the cluster's case (stored version, augmented or new)


# 시도 한 번의 재빌드 점수. improved = 개선, partial = 부분, no_change = 변화 없음, regressed = 악화(→
# reverted 되돌림), inconclusive = 판정 불가, build_ok = 빌드 성공,
# apply_failed = 적용 실패. 개선이 아니면 다음 시도 전에 되돌린다.
AttemptOutcome = Literal["pending", "improved", "no_change", "regressed", "reverted", "build_ok", "apply_failed",
                         "blocked", "partial", "inconclusive"]
# round 10 (2026-10-26): `partial` = some of the cluster's targeted errors are gone, the rest remain, nothing new in the
# touched files (progress: not a failure for limits / KB failure counts, not a success for harvest / recipe credit);
# `inconclusive` = the scoring build could not show the cluster's errors (timed out, failed without parsed errors,
# stopped at an earlier stage that the attempt did not cause) — no KB credit either way, counts as tried


# 오류 목록(ErrorEntry) 한 건의 상태. open = 마지막 빌드에 아직 있음, pom-verify = 재구성의 pom 수정을 다음
# 빌드가 검증, resolved = 해결, blocked = 차단, unresolved =
# 끝까지 남음, note = 구조 검토 경고 같은 메모(고치지 않음).
EntryStatus = Literal["open", "pom-verify", "resolved", "blocked", "unresolved", "note"]
# open = still in the last build; pom-verify = a restructure pom correction the next build verifies; note = a review
# warning / informational entry (never fixed)
# 시도의 출처 = 사다리 단계. kb-script = 저장된 스크립트 재사용(LLM 호출 없음), llm-new = 신규, llm-augment =
# 보강, llm-edit = 스크립트 없는 직접 수정(LLM 이 편집 목록을 줌), review = 구조 검토의 수정.
TrySource = Literal["kb-script", "llm-new", "llm-augment", "llm-edit", "review"]
# kb-script = a stored version run without an LLM call; llm-new = first answer for the case; llm-augment = the LLM
# extended a failed script; llm-edit = the LLM said the fix cannot be a script and gave edits; review = the
# structure review's own fix
TryApplied = Literal["script", "edits", "none"]  # 실제로 적용된 것: 스크립트 / 편집 목록 / 없음


class BlockReason(str, Enum):
    """케이스를 차단(BLOCKED)한 이유. 보고서의 차단 섹션과 `loop.blocked_policy` 가 이 값으로 갈린다.
    차단은 "이 프로젝트 안에서는 못 고침" 이라는 뜻이지 실행
    실패가 아니다(회사 정책: block 으로 실행을 멈추지 않는다)."""
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
    """수정 루프가 일부러 손을 뗀 클러스터 하나(차단). 트리아지·수정 루프가 만들고, 보고서 'Blocked' 섹션과
    인수인계 섹션이 사람이 할 일(suggestion)·원인 라이브러리(artifact, via_direct)와 함께 보여 준다.

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
    # 지목된 것이 javax API jar 뿐일 때(타입의 jar 이지 그것을 쓰는 라이브러리가 아님) dependency:tree 로
    # 끌어온 경로(via)와 올려야 할 직접 의존성(via_direct)을
    # 남긴다 — 사람은 API jar 가 아니라 via_direct 를 올려야 한다.
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
# pom 없는 Tomcat 트리를 읽기만 하는 `sbmig inventory` 의 결과물. 회사 결정(2026-10-23)으로 그런 트리는
# 접수에서 거부되므로 pom 합성은 없다 — 소유 팀에 보낼 거부 안내를 쓰기 위한 설명서일 뿐이다.
# Exploded webapp inventory (`sbmig inventory`, 2026-10-23): a Tomcat directory without a pom — JSPs, Java sources,
# WEB-INF/lib jars, WEB-INF/classes — read only, written as inventory.md / inventory.json; the pom synthesis of the
# next round consumes it. Everything is a file-system fact or an explicitly labelled guess.
# ---------------------------------------------------------------------------
class JarInfo(BaseModel):
    """`sbmig inventory`: WEB-INF/lib 의 jar 하나(크기·sha1·파일
    이름으로 추정한 좌표). 추정은 추정이라고 표시한다."""
    path: str                                   # root-relative
    name: str                                   # file name
    size: int = 0
    sha1: str = ""                              # for a later Nexus / Central lookup
    version_guess: Optional[str] = None         # parsed from the file name (`commons-lang-2.6.jar` → 2.6)
    artifact_guess: Optional[str] = None        # "g:a" from knowledge/libraries/jar_names.yml (None = unknown)
    ignored: bool = False                       # *.jar.old / *.bak / *.jar_ … — backups Tomcat does not load


class WebappRoot(BaseModel):
    """`sbmig inventory`: WEB-INF 를 가진 웹 루트 하나 — web.xml
    요약, JSP·정적 파일 수, lib jar, 클래스 파일의 Java 버전."""
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
    """`sbmig inventory`: `package` 선언과 경로가 맞는 Java
    소스 루트 하나(인코딩·javax·프레임워크 import 집계)."""
    dir: str                                    # root-relative directory where `package a.b;` matches the path
    java_files: int = 0
    packages: list[str] = Field(default_factory=list)      # top-level packages (a.b)
    encoding: str = "utf-8"                     # majority detected encoding (cp949 / euc_kr / utf-8)
    javax_imports: dict[str, int] = Field(default_factory=dict)      # "javax.servlet" → files
    framework_imports: dict[str, int] = Field(default_factory=dict)  # spring / struts1 / struts2 / ibatis2 / mybatis / hibernate / egov → files
    jdk_internal_imports: int = 0               # sun.* / com.sun.* files
    inside_webapp: Optional[str] = None         # the WebappRoot.dir it sits under (e.g. "app1/WEB-INF/src"), else None


class ModuleProposal(BaseModel):
    """`sbmig inventory`: pom 을 만든다면 생길 Maven 모듈 추정
    하나. 지금은 inventory.md 에 보여 주기만 한다(pom 합성 없음).

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
    """`sbmig inventory <dir>` 의 전체 결과(inventory.md /
    inventory.json). 읽기 전용 — 트리를 바꾸지 않는다."""
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
# web.xml 해석 결과. 내장 Tomcat 도 회사 Boot WAR 도 web.xml 을 읽지 않으므로 재구성이 이것으로 등록
# 빈(WebXmlRegistrations)을 만든다. Servlet 3 이전 프로젝트는
# @WebServlet 이 없어서 이것이 없으면 서블릿·필터 매핑이 전부 사라진다.
# web.xml (2026-10-23): parsed by sbmig.analyze.webxml (DTD 2.2 / 2.3 and XSD 2.4 … 6.0, namespace-agnostic); the
# restructure renders WebXmlRegistrations (ServletRegistrationBean / FilterRegistrationBean /
# ServletListenerRegistrationBean / ErrorPageRegistrar) from it because neither embedded Tomcat nor the company's
# Boot WAR (web.xml is not packaged) reads the descriptor. Pre-Servlet-3 projects have no @WebServlet annotations,
# so @ServletComponentScan alone would lose every mapping.
# ---------------------------------------------------------------------------
class WebXmlServlet(BaseModel):
    """web.xml 의 `<servlet>` 하나와 그 매핑. analyze.webxml 이 만들고 재구성이
    ServletRegistrationBean 으로 옮긴다."""
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
    """web.xml 의 `<filter>` 하나. `order` 는 첫 `<filter-mapping>`
    의 위치 — 필터 체인 순서를 지키려고 남긴다."""
    name: str
    filter_class: str
    url_patterns: list[str] = Field(default_factory=list)
    servlet_names: list[str] = Field(default_factory=list)
    dispatchers: list[str] = Field(default_factory=list)   # REQUEST | FORWARD | INCLUDE | ERROR | ASYNC
    init_params: dict[str, str] = Field(default_factory=dict)
    async_supported: bool = False
    order: int = 0                                 # position of its first <filter-mapping> (web.xml chain order)


class WebXmlErrorPage(BaseModel):
    """web.xml 의 `<error-page>` 하나(재구성이 ErrorPageRegistrar 로 옮긴다)."""
    location: str
    error_code: Optional[int] = None
    exception_type: Optional[str] = None           # FQCN; both None = default error page


class WebXmlModel(BaseModel):
    """web.xml 한 파일의 해석 결과(DTD 2.2/2.3, XSD 2.4~6.0).
    analyze.webxml 이 만들고 재구성의 web.xml 등록 단계가 읽는다."""
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
    """이관 뒤에도 모듈 classpath 에 남은 javax EE API jar 와 그것을 끌어온 경로(dependency:tree).
    build.deptree 가 만들고 보고서의 'javax API jars still
    on the classpath' 섹션과 차단 사유(via_direct)가 읽는다.

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
    """Maven 좌표(groupId:artifactId:version + scope 등). 분석·재구성·차단·보고서 어디서나 쓴다."""
    group_id: str
    artifact_id: str
    version: Optional[str] = None
    scope: Optional[str] = None
    type: Optional[str] = None
    classifier: Optional[str] = None
    optional: bool = False

    @property
    def key(self) -> str:
        """`groupId:artifactId` — 버전 없는 비교 키."""
        return f"{self.group_id}:{self.artifact_id}"

    @property
    def coordinates(self) -> str:
        """`g:a[:v]` — 버전이 있으면 붙인 표기."""
        return self.key + (f":{self.version}" if self.version else "")

    @classmethod
    def parse(cls, coords: str) -> "GAV":
        """`"g:a[:v]"` 문자열을 GAV 로(두 조각 미만이면 ValueError)."""
        parts = coords.split(":")
        if len(parts) < 2:
            raise ValueError(f"bad coordinates: {coords}")
        return cls(group_id=parts[0], artifact_id=parts[1], version=parts[2] if len(parts) > 2 and parts[2] else None)


class PluginInfo(BaseModel):
    """pom 의 `<plugin>` 하나(좌표 + configuration)."""
    group_id: str = "org.apache.maven.plugins"
    artifact_id: str
    version: Optional[str] = None
    configuration: dict[str, Any] = Field(default_factory=dict)

    @property
    def key(self) -> str:
        """`groupId:artifactId` 키(버전 없음)."""
        return f"{self.group_id}:{self.artifact_id}"


class PomInfo(BaseModel):
    """pom.xml 한 파일의 분석 결과(유효 groupId, 부모, 모듈, 의존성, 저장소, Java / Boot 버전). 분석이
    만들고 계획·재구성·구조 검토·보고서 인수인계가 읽는다."""
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
    """소스 트리 통계(분석의 source_scanner): main 클래스, 서블릿, javax import, web.xml, JSP·정적 리소스
    위치. 프로젝트 종류 판정과 재구성(front 모듈, web.xml 등록, 큰 미디어 파일 안내)의 근거가 된다."""
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
# as-is 코드가 외부 설정을 읽는 방식(@Value, @ConfigurationProperties,
# Environment, System.getProperty, XML ${…} 등).
PropertyRefSource = Literal["value-annotation", "configuration-properties", "environment", "system-property",
                            "system-env", "xml-placeholder", "context-param", "jndi", "property-source", "profiles",
                            "config-placeholder"]   # round 9: `${…}` inside the value of a packaged definition file
                                                    # (`spring.datasource.url=${DB_URL}` → DB_URL is read at runtime)
PropertyStatus = Literal["defined", "defaulted", "external"]


class PropertyRef(BaseModel):
    """as-is 코드·XML 이 외부 설정 키를 읽는 자리 하나(analyze.properties 가 찾는다).

    One place where the as-is code / XML reads an externalized setting."""
    key: str                                       # the key as written: "db.url", "java:comp/env/jdbc/x", "app.*" (prefix)
    source: PropertyRefSource
    file: str                                      # repo-relative path at analysis time
    line: Optional[int] = None
    default: Optional[str] = None                  # `${key:default}` / System.getProperty(k, default) → "default"
    scope: Literal["main", "test"] = "main"
    raw: str = ""                                  # matched text, sanitized, ≤ 200 chars


class PropertyFinding(BaseModel):
    """외부 설정 키(또는 접두어) 하나와 판정: defined(패키지된 리소스에 있음) / defaulted(코드 기본값만) /
    external(어디에도 없음 → 운영자가 값을 넣어야 함, 인수인계 '설정할 키').

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
    """외부 설정 목록 전체. 분석 때는 ProjectProfile.properties, 재구성이 placeholder 를 쓴 뒤에는
    RunReport.properties. 인수인계 섹션의 '설정할 키' 표와
    필터링 대상 바이너리 목록(구조 검토 입력)이 여기서 나온다.

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
        """status 가 external 인 항목만(어디에도 정의되지 않은 키)."""
        return [f for f in self.findings if f.status == "external"]


class LibraryUsage(BaseModel):
    """프로젝트가 쓰는 라이브러리 하나(정규화된 이름 + 근거). analyze.libraries 가 만들고
    권고(advisory)·프롬프트 힌트가 읽는다."""
    name: str                       # normalized: lombok, mapstruct, querydsl, spring-security, springdoc, swagger2,
                                    # mybatis, jpa, hibernate, feign, kafka, redis, jackson, junit4, junit5, mockito, ...
    version: Optional[str] = None
    evidence: list[str] = Field(default_factory=list)   # "pom:org.projectlombok:lombok", "import:javax.persistence"


class ServiceInfo(BaseModel):
    """MSA 저장소 안의 배포 단위(서비스) 하나, 또는 함께 이관해야 하는 공유 라이브러리 루트(kind=library).

    One deployable unit inside an MSA repo — or (kind="library") a shared library root that must be migrated too."""
    name: str
    root_pom: str                   # relative path to the service's top pom
    case_type: CaseType
    kind: Literal["service", "library"] = "service"
    poms: list[str] = Field(default_factory=list)
    main_classes: list[str] = Field(default_factory=list)
    spring_boot_version: Optional[str] = None
    java_version: Optional[str] = None


class ProjectProfile(BaseModel):
    """분석 단계의 결과 전체(profile.json): 프로젝트 종류, pom 들, 소스 통계, 라이브러리, 버전, MSA 서비스,
    외부 설정. 계획·재구성·구조 검토·수정 프롬프트·보고서가 모두 이것을 읽는다. build_tool 이 maven 이
    아니면 접수에서 중단된다."""
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
        """정규화된 라이브러리 이름(LibraryUsage.name)이 쓰이는지."""
        return any(lib.name == name for lib in self.libraries)

    @staticmethod
    def make_fingerprint(pom_paths: list[str], root_artifact_id: str) -> str:
        """pom 경로들과 루트 artifactId 로 만든 16자리 지문(계획이
        어느 프로필에서 나왔는지 맞춰 보는 용도)."""
        h = hashlib.sha256()
        h.update(root_artifact_id.encode())
        for p in sorted(pom_paths):
            h.update(b"|" + p.encode())
        return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------
class RecipeRef(BaseModel):
    """계획에 들어가는 OpenRewrite 레시피 하나. id 는
    knowledge/recipes/catalog.yml 에 있는 것만 쓴다(불변조건 5)."""
    id: str                                        # fully qualified recipe id
    source: Literal["builtin"] = "builtin"         # v2: OpenRewrite recipes only (no project / KB recipes)
    artifact: Optional[str] = None                 # "org.openrewrite.recipe:rewrite-spring" (version from config)


class PlanStep(BaseModel):
    """계획 단계 하나. 계획(plan.planner)이 만들고 오케스트레이터·LangGraph 엔진이 순서대로 실행한다.
    v2.1 의 id: transform.rewrite / transform.compile / restructure.apply·boot_pom·msa_pom·pom_only /
    restructure.review / verify.build (서비스별 단계면 접미사가 붙는다)."""
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
    """계획 단계의 결과(plan.json): 실행할 단계 목록. 결정적으로 만든다 — 작은 LLM 이 레시피를 고르던 옛
    사내 에이전트의 실패를 되풀이하지 않으려는 결정이다(`plan.advisor: llm` 은 기본으로 꺼져 있음)."""
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
    """빌드 로그에서 뽑은 오류 한 건(build 파서). L1 `signature`(구체) / L2 `signature_generic`(일반)
    서명이 붙고, L2 가 케이스를 정한다(case_id = sha1(L2)[:10])."""
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
        """같은 오류를 가리키는 키(파일:줄:L1 서명) — 빌드끼리 비교할 때 쓴다."""
        return f"{self.file or '-'}:{self.line or 0}:{self.signature}"


class BuildResult(BaseModel):
    """Maven 빌드 한 번의 결과(build.runner): 성공 여부, 오류 목록, 실패 모듈, 타임아웃, 로그 경로. 수정
    루프의 채점과 오류 목록 갱신이 읽는다."""
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
        """오류 개수."""
        return len(self.errors)

    def signature_multiset(self) -> dict[str, int]:
        """L1 서명별 오류 수 — 라운드끼리 비교(정체·진동 판정)할 때 쓴다."""
        out: dict[str, int] = {}
        for e in self.errors:
            out[e.signature] = out.get(e.signature, 0) + 1
        return out


class RewriteResult(BaseModel):
    """OpenRewrite 실행 한 번의 결과(rewrite.runner): 바뀐 파일, 레시피별 바뀐 파일, 파싱하지 못한 파일."""
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
    """트리아지가 묶은 오류 묶음. v2 부터 클러스터 = 케이스 하나(같은 L2 서명이면 파일이 달라도 한
    클러스터)이고, 수정 루프가 클러스터마다 사다리(재사용 → 보강 → 신규)를 돈다."""
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
        """클러스터 오류들의 L2 서명(중복 제거, 정렬)."""
        return sorted({e.signature_generic for e in self.errors if e.signature_generic})


class TriageDecision(BaseModel):
    """트리아지가 클러스터 하나에 내린 결정(DecisionKind)과 그 이유."""
    cluster_id: str
    kind: DecisionKind
    rationale: str = ""


class FileChange(BaseModel):
    """파일 편집 한 건(v1 의 search/replace 계약). v2.1 은 시도가
    바꾼 파일 목록을 FixAttempt.changes 에 남기는 데만 쓴다.

    One atomic edit. modify = search/replace (search must match exactly once), create/delete = whole file."""
    path: str
    op: Literal["modify", "create", "delete", "rename"]
    search: Optional[str] = None
    replace: Optional[str] = None
    content: Optional[str] = None
    new_path: Optional[str] = None


class LLMUsage(BaseModel):
    """LLM 사용량(호출 수, 입력 / 출력 토큰). 단계·시도마다 모아 더해 RunReport.llm_usage 가 된다."""
    model: str = ""
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, other: "LLMUsage") -> "LLMUsage":
        """두 사용량을 더한 새 객체(모델 이름은 먼저 있는 쪽을 쓴다)."""
        return LLMUsage(
            model=self.model or other.model,
            calls=self.calls + other.calls,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


class ModelCost(BaseModel):
    """한 실행에서 모델 하나의 사용량과 `llm.pricing` 으로 매긴 비용(가격이 없으면 cost=None).

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
    """한 실행의 LLM 비용(RunReport.llm_cost): 모델별 + 합계.
    가격 없는 모델은 합계에서 빠지고 unpriced_models 에 남는다.

    Priced LLM usage of a run (`RunReport.llm_cost`): per model + total. Models without a price are listed in
    `unpriced_models` and left out of `total`; `usage_missing` = calls were made but no token counts came back."""
    currency: str = "USD"
    total: float = 0.0
    by_model: list[ModelCost] = Field(default_factory=list)
    unpriced_models: list[str] = Field(default_factory=list)
    usage_missing: bool = False
    note: str = ""                                 # llm.pricing_note: where the prices come from / as of when


# ---------------------------------------------------------------------------
# v2 수정 스크립트: LLM 이 쓴 `def fix(p)` 를 정적 검사(AST) 후 `python -I` 별도 프로세스에서 제한 API `p` 로
# 실행한다(sbmig.script). v2.1 의 저장 위치는 `<kb>/cases/<case_id>/fix_v<N>.py` + case.json(CaseScript)이다 —
# 아래 영어의 `<kb>/scripts/...` 경로와 sbmig.kb.scripts 는 v2 초기의 것.
# v2 fix scripts (flow-v2, docs/v2/PLAN-v2.md §3.2): every build-error fix is a Python script `def fix(p)` written by
# the LLM, checked (AST) and run in a separate `python -I` process against a restricted helper API (sbmig.script),
# stored per case in <kb>/scripts/<case_id>/v<N>.py + v<N>.meta.json (sbmig.kb.scripts)
# ---------------------------------------------------------------------------
# 사다리 단계: new = 신규(LLM), reuse = 저장된 스크립트
# 재사용(LLM 없음), augment = 보강(LLM 이 이전 버전을 고침).
ScriptMode = Literal["new", "reuse", "augment"]
# 스크립트 실행 결과: ok / error(예외·경로 거부) / timeout(script.timeout_seconds 뒤 강제 종료) /
# rejected(정적 검사 거부).
ScriptRunStatus = Literal["ok", "error", "timeout", "rejected"]
# ok = fix(p) returned; error = it raised (or a helper refused a path); timeout = killed after script.timeout_seconds;
# rejected = the static check refused the code before it ran (forbidden import / name / dunder access, no fix(p))


class ScriptRunResult(BaseModel):
    """수정 스크립트 한 번 실행의 결과(sbmig.script.ScriptRunner.run): 상태, 바꾼 파일, p.log 줄, 오류. 정확한
    변경 내용은 수정 루프가 남기는 git diff 이고, 이것은 스크립트 자신의 보고다.

    What one run of a fix script did (sbmig.script.ScriptRunner.run). The exact change is the git diff the loop
    records; this is the script's own account."""
    status: ScriptRunStatus = "ok"
    changed_files: list[str] = Field(default_factory=list)    # relative posix paths written / created / deleted / moved
    log: list[str] = Field(default_factory=list)              # p.log(...) lines (capped by script.log_lines)
    error: str = ""                                           # exception / refusal / timeout text ("" when ok)
    problems: list[str] = Field(default_factory=list)         # static-check findings when rejected
    duration_s: float = 0.0


class ScriptMeta(BaseModel):
    """v2 초기 KB 의 스크립트 메타(`<kb>/scripts/<case_id>/v<N>.meta.json`). v2.1 은 쓰지 않는다 — 지금은
    case.json 의 CaseScript 가 같은 역할을 한다.

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


class ScriptBase(BaseModel):
    """보강 요청이 고칠 대상 스크립트(FixContext.script_base): 이전 버전 코드와 이 프로젝트에서 한 일(결과,
    남은 오류, 새로 생긴 오류, 버전 이력). 수정 루프가 만들고 보강 프롬프트가 읽는다.

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


# LLM 직접 수정(Edit)의 종류: replace(문자열 치환) / write / create / delete / move / pom(pom 편집기 호출).
EditOp = Literal["replace", "write", "create", "delete", "move", "pom"]


class EditHunk(BaseModel):
    """변경 조각 하나(v2.1e): 적용된 diff 에서 뽑은 수정 전(before, as-is) / 수정 후(after, to-be) 줄. 오류별
    JSON 과 `sbmig kb errors show` 에서 사람이 읽는다. 기본은 자르지 않는다(`errors.snippet_lines: 0`).

    v2.1e (2026-10-27, user: "as-is 코드와 to-be 코드도 들어갔으면"): one changed region of a file as a person reads
    it — the lines that were there (as-is) and the lines that are there now (to-be), taken from the applied diff.
    Whole by default (`errors.snippet_lines: 0`); people read these in the per-error JSON and `kb errors show`."""
    lines: str = ""                                # "~12" | "+42–47" | "-3" — the same spelling as EditNote.lines
    before: str = ""                               # as-is: the removed / replaced lines ("" for an insertion / new file)
    after: str = ""                                # to-be: the added / replacing lines ("" for a deletion)
    truncated: bool = False                        # errors.snippet_lines cut this hunk


class EditNote(BaseModel):
    """사람이 읽는 수정 내역 한 건(v2.1b): 어디(where)를 어떻게(change) — LLM 이 쓰고, `lines`·hunks 는 sbmig
    가 diff 에서 채운다. LLM 이 아무것도 적지 않은 파일은 diff 로 메모를 만들어 '수정' 칸이 비지 않게 한다.

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
    """LLM 이 요청한 직접 수정 한 건(`<edits>` JSON, v2.1). sbmig 가 수정 스크립트와 같은 헬퍼 API 로, 같은
    가드(프로젝트 폴더 안만) 아래에서 적용한다. pom 편집은 `pom` 에 편집기 호출을 담는다.

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
    """수정기(FixerLike.propose_script)와 구조 검토의 답: 원인 analysis · 해결 solution · 직접 수정 edits(바로
    적용) · 재사용할 스크립트 code(저장) · 운영자 확인 verify. code 가 비고 no_script_reason 이 있으면 edits
    가 수정 전부다.

    The fixer's answer (FixerLike.propose_script). v2.1 (2026-10-27): 원인 `analysis` · 해결 방법 `solution` ·
    수정 `edits` (applied directly) · 스크립트 `code` (stored for reuse); `no_script_reason` when the LLM says the
    fix cannot be a reusable script (then `edits` is the fix)."""
    code: str = ""                                 # the <script> body (must define `def fix(p)`); "" = no script
    analysis: str = ""
    solution: str = ""
    edits: list[Edit] = Field(default_factory=list)
    changes: list[EditNote] = Field(default_factory=list)   # `<changes>`: what the fix modifies where (script or edits)
    no_script_reason: str = ""
    verify: str = ""                               # v2.1d `<verify>`: 운영자 확인 사항 — what the operator must check at
                                                   # runtime after a major version upgrade / a library replaced ("" = none)
    model: str = ""
    prompt_hash: str = ""
    llm_usage: LLMUsage = Field(default_factory=LLMUsage)
    requested_files: list[str] = Field(default_factory=list)
    raw_response: str = ""
    error: str = ""                                # why there is no script (unparsable answer, transport, refusal)


FixAnswer = ScriptProposal                      # the v2.1 name


# ---------------------------------------------------------------------------
# v2.1d 의존성 버전 정책: 메이저는 올리지 않는다(같은 메이저에서 jakarta·JDK 21 지원 버전을 찾는다). 수정 뒤
# pom diff 를 다시 읽어 메이저 업그레이드·라이브러리 교체를
# OperatorCheck(인수인계 '운영자 확인 필요')로 남긴다 — 차단은 없다.
# v2.1d dependency version policy (2026-10-27, user: "메이저를 올리지 마라; 같은 메이저에서 jakarta / JDK 21 을 지원하는
# 버전을 찾아 올려라; 메이저를 올렸으면 무엇을 바꿨고 운영자가 무엇을 확인해야 하는지 가이드를 남겨라"). The LLM is told the
# policy and what the repository has (sbmig.build.repo_versions); after a fix, the pom diff is read back
# (sbmig.build.depchanges) and every major upgrade / library replacement becomes an OperatorCheck in the report's
# Handover ("운영자 확인 필요") — never a block: the build must pass.
# ---------------------------------------------------------------------------
# major = 메이저 번호가 바뀜, minor = 그 밖의 버전 변경, replaced = 같은 pom 에서 한 라이브러리를 빼고 대신할
# 것을 넣음, added / removed = 나머지. 운영자 확인 대상은 major 와 replaced 다.
DependencyChangeKind = Literal["major", "minor", "added", "removed", "replaced"]
# major = the major number of the version changed (2.4.2 → 3.2.1); minor = any other version change (incl. a
# classifier / property change); replaced = one library removed and another added in the same pom that stands in
# for it (same artifactId or one artifactId contains the other); added / removed = the rest
PomSection = Literal["dependencies", "dependencyManagement", "plugins", "pluginManagement", "parent"]


class DependencyChange(BaseModel):
    """pom diff 에서 읽은 좌표 변경 한 건(build.depchanges). 시도(FixTry.major_upgrades)·구조 검토에 붙고,
    major / replaced 는 OperatorCheck 가 되어 운영자 확인 필요 목록에 오른다.

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
        """운영자가 실행 시 확인해야 하는 변경인지(메이저 업그레이드 또는 라이브러리 교체).

        A change the operator must verify at runtime (major upgrade or a library replaced)."""
        return self.kind in ("major", "replaced")

    @property
    def text(self) -> str:
        """보고서용 한 줄 표기(`g:a 2.4.2 → 3.2.1`, 교체면 새 좌표까지).

        `org.sitemesh:sitemesh 2.4.2 → 3.2.1` / `opensymphony:sitemesh 2.4.2 → org.sitemesh:sitemesh 3.2.1`."""
        a = f"{self.replaced_by} {self.after or '-'}" if self.replaced_by else (self.after or "-")
        return f"{self.key} {self.before or '-'} → {a}"


class VersionCandidate(BaseModel):
    """저장소에 있는 릴리스 하나(jakarta 빌드 여부, 프리릴리스 여부, pom 이 밝힌 최소 Java).

    One release the repository has (sbmig.build.repo_versions)."""
    version: str
    jakarta: Optional[bool] = None                 # its pom depends on jakarta.* (None = pom not checked / unknown)
    prerelease: bool = False                       # milestone / RC / alpha / beta / SNAPSHOT
    java: Optional[int] = None                     # minimum Java the pom states (maven.compiler.release / target)


class RepositoryVersions(BaseModel):
    """설정된 저장소(settings.xml 미러 / build.repository_url / Maven Central)가 라이브러리 하나에 대해 가진
    버전들. 수정 프롬프트의 "Dependency versions" 사실
    목록이다(build.repo_versions) — LLM 이 같은 메이저 안에서 고르게 한다.

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
    checked_at: datetime = Field(default_factory=utcnow)


class OperatorCheck(BaseModel):
    """v2.1d 운영자 확인 필요 항목: 최종 트리에 남은 메이저 업그레이드 / 라이브러리 교체와 그 확인 가이드.
    실행 끝에 오류 목록의 시도(+ 구조 검토)에서 만들어 인수인계 첫 블록·report.json·`SBMIG-RESULT … major=<n>`
    에 나온다. 되돌린 시도의 변경은 넣지 않는다.

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
# v2.1 오류 목록: 실행의 빌드 오류 / pom 수정 한 건 = 레코드 하나(ErrorEntry). 실행 디렉토리 errors.jsonl 에
# 쓰고 끝에 KB errors.jsonl 에 덧붙인다 — 무엇이었는지, 시도마다 LLM 답과 적용 diff, 어떻게 끝났는지.
# v2.1 error list (2026-10-27, docs/v2/PLAN-v2.1.md §3.3): ONE record per build error / pom correction of a run —
# <run dir>/errors.jsonl, appended to <kb>/errors.jsonl at the end; what it was, every try with the LLM's answer and
# the applied diff, how it ended. Replaces ErrorCaseRecord / FixAttemptRecord / the case catalog / reviews.jsonl.
# ---------------------------------------------------------------------------


class FixTry(BaseModel):
    """케이스 시도 한 건. ErrorList.begin_try 가 만들어 그 케이스의 열린 오류들이 같은 객체를 나눠 갖는다:
    출처(사다리 단계), 원인·해결·수정 내역, diff, 커밋, 재빌드 점수. errors.md·보고서·KB 케이스
    기록(ScriptRun)이 읽는다."""
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
    verify: str = ""                               # v2.1d 운영자 확인 사항 for them (LLM <verify> or sbmig synthesized)
    verify_source: Literal["", "llm", "sbmig"] = ""   # who wrote the guide ("" = no major upgrade)
    diff: str = ""                                 # unified diff of what was applied (capped by errors.diff_chars)
    commit: str = ""
    # 재빌드 점수(AttemptOutcome). 수정 루프가 다음 라운드 빌드를 보고 채운다.
    result: AttemptOutcome = "pending"
    note: str = ""                                 # failure text, rollback, script error …
    llm_calls: int = 0
    seconds: float = 0.0


class ResolvedBy(BaseModel):
    """v2.1f: 자기 시도 없이 빌드에서 사라진 오류(resolved == "build")에 대해 같은 라운드에 그 파일이나 위쪽
    pom 을 바꾼 적용된 시도 — 아마도 고친 시도다. 오류 목록이 만들고, `sbmig kb reindex` 가 옛 기록을
    채운다(match=reindex).

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
    """v2.1 오류 목록의 레코드 하나 = 빌드 오류 / pom 수정 / 구조 검토 메모 한 건. 오류
    목록(sbmig.errors.entries.ErrorList)이 만들고 수정 루프가 시도(tries)를 붙이며,
    보고서·errors.md·KB(CaseStore.record_run)가 읽는다."""
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
    # 처음 본 곳: compile(변환 후 컴파일) / restructure /
    # review / build(검증 빌드) / fix r3(수정 루프 3라운드)
    seen: str = ""                                 # compile | restructure | review | build | fix r3
    status: EntryStatus = "open"
    tries: list[FixTry] = Field(default_factory=list)
    # 해결한 곳: "r3 try 2"(그 시도) / "build"(자기 시도 없이 사라짐 → resolved_by) / "review"(구조 검토)
    resolved: str = ""                             # "r3 try 2" | "build" (gone without a try) | "review"
    resolved_by: list[ResolvedBy] = Field(default_factory=list)   # v2.1f: "build" → the same-round try that probably
                                                   # fixed it (nearest first: same file, then its module pom, then root)
    script: str = ""                               # cases/<case_id>/fix_v2.py — the script that resolved it
    note: str = ""                                 # pom-verify: what the restructure did; note entries: the text
    at: datetime = Field(default_factory=utcnow)


# 저장된 스크립트 상태: unverified = 이 프로젝트에서만 통했거나 한 번 돌았음, proven = 다른 프로젝트에서
# 재사용으로 케이스를 해결함, retired = 사람이 끔(`sbmig
# kb cases retire`). 재사용 선택은 proven 을 먼저 고른다.
CaseScriptStatus = Literal["unverified", "proven", "retired"]
# unverified = stored from an answer whose edits fixed this project (or run here once); proven = resolved the case in
# another project on reuse; retired = a person switched it off (`sbmig kb cases retire`)


class ScriptRun(BaseModel):
    """v2.1e: 저장된 스크립트 버전 하나가 한 프로젝트에서 돈 기록 — 점수와 그 이유(남은 오류, 새 오류,
    예외·타임아웃·정적 검사 거부). 왜 보강됐는지를 사람과 이후 유사도 단계가 본다.

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
    """case.json 안의 저장된 스크립트 버전 하나(fix_v<N>.py): 상태, 성공 / 실패 수, 해결한 프로젝트,
    계보(parent·reason), 실행 이력(runs). CaseStore 가
    관리하고 재사용할 버전 선택(CaseStore.best)이 읽는다."""
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


class CaseInfo(BaseModel):
    """KB 케이스 하나(`<kb>/cases/<case_id>/case.json`): L2 서명, 이름, 본 프로젝트, seen / resolved, 저장된
    스크립트들, 마지막 원인·해결. CaseStore 가 쓰고 KB 조회(match)·cases.md·플릿 요약이 읽는다. 사람의
    `pin`(block) 은 모델 밖 키다.

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
    # case.json may carry extra keys the store keeps beside the model: "pin" ("block[:<reason>]", a person's
    # instruction never to try the case) and "last_result" (sbmig.errors.cases.CaseStore.extra)


class ReviewIssue(BaseModel):
    """구조 검토가 찾은 문제 하나: critical(곧바로 수정) 또는 warning(보고서와 오류 목록 메모에만)."""
    severity: Literal["critical", "warning"] = "warning"
    file: str = ""                                 # relative pom / module path ("" = project-wide)
    problem: str = ""
    fix_hint: str = ""


class StructureReview(BaseModel):
    """구조 검토(sbmig.review) 결과: 재구성 뒤 첫 빌드 전에 LLM 이 pom·의존성·모듈을 회사 규칙에 비춰 본 판정,
    문제 목록, critical 수정과 그 커밋. RunReport.structure_review → 보고서 'Structure review'; 오류 목록에는
    review 레코드로 남는다(KB 의 reviews.jsonl 은 v2.1 에서 더 이상 쓰지 않는다).

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
        """severity 가 critical 인 문제만."""
        return [i for i in self.issues if i.severity == "critical"]


class FixAttempt(BaseModel):
    """수정 루프의 시도 한 건(내부 기록): 결정, 바꾼 파일, 커밋, 전후 오류 수, 점수(outcome), 스크립트
    정보(mode·버전·결과), v2.1 출처·해결·직접 수정 수, v2.1d 메이저 업그레이드. 수정 루프가 만들고 채점하며
    오류 목록의 FixTry 로 옮겨 적는다."""
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
    # v2.1d: major upgrades / library replacements this attempt made in poms, and the operator guide for them
    major_upgrades: list[DependencyChange] = Field(default_factory=list)
    verify: str = ""                               # 운영자 확인 사항 (the answer's <verify>, or synthesized)
    verify_source: Literal["", "llm", "sbmig"] = ""


class IterationRecord(BaseModel):
    """수정 루프 한 라운드의 요약: 빌드 성공 여부, 오류 수, 서명별 수, 클러스터·결정, 정체 수준.
    PhaseResult.iteration_records 를 거쳐 저널·보고서로 간다."""
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
    """단계 하나(transform / restructure / verify …)의 결과: 성공 여부, 반복 수, 레시피 결과, 시도, 남은
    오류, 차단 항목. 오케스트레이터가 단계마다 하나씩 RunReport.phases 에 쌓는다."""
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
# v1 KB 학습용 수정 라벨. sbmig.kb.labels 는 v2 에서 지워졌고
# v2.1 코드는 이 Literal 을 쓰지 않는다(옛 기록 읽기용).
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
    """KB 가 클러스터의 케이스에 대해 아는 것(FileKnowledgeBase.match): CaseHit 목록 + 프롬프트 힌트 줄. 수정
    루프가 라운드마다 받아 재사용 / 보강 / 신규를 고른다.

    What the KB knows about a cluster's case (`FileKnowledgeBase.match`): the catalog hits and their hint lines."""
    hints: list[str] = Field(default_factory=list)               # short natural-language hints for the script prompt
    case_hits: list[Any] = Field(default_factory=list)           # CaseHit (case catalog, 2026-10-23)


# ---------------------------------------------------------------------------
# v1·라운드 10 의 오류 사례 기록(옛 kb/errors.jsonl 모양, fix_attempts.jsonl, 케이스 카탈로그). v2.1 은
# ErrorEntry 가 대신하며 아래 모델들은 채워지지 않는다(RunReport.error_cases
# 는 빈 목록) — 옛 KB·report.json 을 읽기 위해 남긴 계약.
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
    """(v1 기록) 텍스트 치환 변경의 토큰 치환 하나: old → new 가 count 번.

    One token-level substitution of a text-replace change: `old` became `new` `count` times (in `files`)."""
    old: str
    new: str
    count: int = 1
    files: list[str] = Field(default_factory=list)            # relative posix paths


class ChangeClass(BaseModel):
    """(v1 기록) 변경이 글자로 어떻게 보이는지의 분류(옛 sbmig.kb.changes.classify_diff, v2 에서 삭제됨).

    How a change looks textually — `sbmig.kb.changes.classify_diff(diff_text)`."""
    change_kind: ChangeKind = "none"
    replace_pairs: list[ReplacePair] = Field(default_factory=list)
    file_types: list[str] = Field(default_factory=list)       # java | pom | xml | properties | yaml | jsp | other
    files: list[str] = Field(default_factory=list)            # relative posix paths the diff touches
    lines_added: int = 0
    lines_removed: int = 0


# (v1·v2 초기 기록) FixAttemptRecord / ErrorCaseRecord / StructureReview 의 스키마 번호.
KB_RECORD_SCHEMA = 3      # 1 (or absent) = before round 10 (2026-10-26); 2 = round 10; 3 = v2 fix scripts
                          # (flow-v2: script_* fields, fix methods script / script_reuse / script_augment)


class FixAttemptRecord(BaseModel):
    """(v1·v2 초기 기록) 옛 kb/fix_attempts.jsonl 한 줄 = 시도
    한 건. v2.1 은 쓰지 않는다(ErrorEntry.tries 가 대신).

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
    """(v1 기록) 오류 이력의 시도 요약 한 줄(ErrorCaseRecord.attempts).

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
    """(v1·라운드 10 기록) 옛 kb/errors.jsonl 한 줄 = 빌드 오류 한 건. v2.1 은 ErrorEntry 를 쓰고
    RunReport.error_cases 는 비어 있다.

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
# 라운드 10 의 케이스 카탈로그(kb/catalog/…yml). v2.1 은 `cases/<case_id>/case.json`(CaseInfo)으로 바뀌었고,
# 여기서 아직 쓰이는 것은 KB 조회 결과인 CaseAction·CaseHit 와 그 Literal(CaseStatus, CaseActionKind)뿐이다.
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
    """(라운드 10 카탈로그) 케이스의 변형 하나(소유 클래스 / L1 서명)."""
    owner: str = ""                                # FQCN owning the symbol (javac location / symbol owner); "" unknown
    signature: str = ""                            # L1
    seen: int = 0
    projects: int = 0


class CaseFix(BaseModel):
    """(라운드 10 카탈로그) 케이스를 고친 방법 하나(방법 + 해결자 + 치환 쌍으로 묶음).

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
    """(라운드 10 카탈로그) 케이스에서 실패한 방법 하나."""
    method: FixMethod = "none"
    outcome: AttemptOutcome = "no_change"
    failure: str = ""
    attempt_id: str = ""
    project: str = ""
    count: int = 0
    projects: int = 0


class CaseEvent(BaseModel):
    """(라운드 10 카탈로그) 타임라인 항목: 한 실행에서 본 케이스.

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
    """KB 조회가 케이스에 정한 행동: script(저장된 스크립트 버전 실행) / block(사람이 고정한 차단 —
    시도하지 않음) / hint(힌트만). FileKnowledgeBase.match 가 만들고 트리아지가 따른다."""
    kind: CaseActionKind = "hint"
    block_reason: str = ""                                    # block: BlockReason value + suggestion
    suggestion: str = ""
    script_version: int = 0                                   # script: <kb>/scripts/<case id>/v<N>.py


class CaseEvidence(BaseModel):
    """(라운드 10 카탈로그) 케이스의 증거 집계."""
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
    """(라운드 10 카탈로그) 사람이 편집하는 블록 — 자동 재계산이 덮어쓰지 않는다.

    Edited by people (YAML or `sbmig kb cases …`); never overwritten by harvest / reindex; wins over the auto part."""
    name: Optional[str] = None
    status: Optional[CaseStatus] = None            # e.g. rejected, or active by decision
    action: Optional[CaseAction] = None            # a pinned action
    hints: list[str] = Field(default_factory=list)
    notes: str = ""


class CatalogCase(BaseModel):
    """(라운드 10 카탈로그) 케이스 하나. v2.1 은 CaseInfo 를 쓴다."""
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
        """사람 블록의 상태가 있으면 그것, 없으면 자동 상태."""
        return self.human.status or self.status

    def effective_action(self) -> CaseAction:
        """사람 블록의 행동이 있으면 그것, 없으면 자동 행동."""
        return self.human.action or self.action

    def effective_name(self) -> str:
        """사람이 붙인 이름이 있으면 그것, 없으면 자동 이름."""
        return self.human.name or self.name


class CaseHit(BaseModel):
    """KB 조회의 케이스 적중 하나(FileKnowledgeBase.match): 케이스, 상태(active = 전에 성공 / candidate =
    미검증), 행동(script 버전 또는 사람의 block), 해당 오류,
    힌트. 트리아지·수정 루프가 사다리 첫 칸(재사용)에 쓴다.

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
    """`sbmig fleet` 의 프로젝트 하나의 결과(상태, run_id, 종료 코드, LLM 사용량·비용, 차단·미해결 수, 오류
    상태별 수). fleet/state.json 과 일괄 실행 요약에 쓰인다."""
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
    """fleet/state.json — 일괄 실행의 진행 상태. 프로젝트 하나가
    끝날 때마다 원자적으로 다시 써서, 중단 뒤 이어서 돌릴 수 있다.

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
# 정책: block 정책은 일부러 없다(사용자: 500개 프로젝트가 모두 완벽할 수는 없다). v2 의 권고는 guide 뿐 — 수정
# 프롬프트의 힌트 + 보고서 인수인계 섹션. 아래 영어의 recipe 정책 설명은 v1 의 것이다.
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
    """knowledge/libraries/advisories.yml 의 권고 항목 하나(EOL·관리 안 되는 라이브러리). 회사 추가분은
    advisories.extra_files 로 id 기준 병합된다.

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
    """이 프로젝트에 걸린 권고 하나. 분석 뒤 만들어지고 마무리에서 outcome(replaced / still-present)을
    확인한다. 수정 프롬프트 힌트, 인수인계 섹션, report.json `advisories` 로 간다.

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
        """실행 뒤 라이브러리가 더는 보이지 않는지(outcome == replaced)."""
        return self.outcome == "replaced"


class MergeConflict(BaseModel):
    """`layout.multi_module_policy: merge` 에서 여러 as-is 모듈(또는 루트 src/)에 같은 경로가 있던 파일 하나.
    루트 `<modules>` 순서의 첫 모듈 것이 남고, 나머지는 실행
    디렉토리에 저장되어 인수인계 'Merge conflicts' 에 나온다.

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
    """인수인계 '바뀐 버전' 표의 한 줄: as-is 와 최종 pom 의 버전(g:a 기준, 프로젝트 전체). carried_over = 새
    부모 / BOM 이 관리하지 않아 as-is 버전으로 고정한 것 — 소유 팀이 확인하거나 바꿔야 한다.

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


class HandoverSummary(BaseModel):
    """인수인계 요약("여기까지 바꿨고, 이제부터는 소유 팀 몫"): 부모·Java·Boot 전후, 버전 변경, 추가·제거,
    고정 유지, 팀이 할 일(from_here), 병합 충돌, 설정할 키.
    마무리에서 report.handover.build_handover 가 만든다.

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
class RunReport(BaseModel):
    """실행 한 번의 최종 보고서(report.json, report.md 의 원본). 오케스트레이터가 단계마다 채우고
    마무리에서 인수인계·운영자 확인·오류 목록을 붙인다. `success` = 최종 검증 빌드 성공(불변조건 1: 최종
    빌드 없이 끝나는 실행은 없다)."""
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
    operator_checks: list[OperatorCheck] = Field(default_factory=list)   # v2.1d: 운영자 확인 필요 (major upgrades /
                                                   # library replacements still in the final tree, with their guide)
    outputs: dict[str, str] = Field(default_factory=dict)        # project_dir, zip, diff, journal, report_md, report_json
    timings_s: dict[str, float] = Field(default_factory=dict)


# MigrationPlan 이 아래쪽에 정의된 LLMUsage 를 문자열로 참조하므로, 모든 모델이 정의된 뒤 한 번 다시 빌드한다.
MigrationPlan.model_rebuild()
