"""패키지끼리 주고받는 데이터 모델을 모아 둔 모듈. pydantic 모델, Enum, Literal이 모두 여기 있다.

Data flows analysis (ProjectProfile) -> plan (MigrationPlan) -> build (BuildResult) -> fix loop
(ErrorCluster, FixAttempt, ScriptProposal) -> error list (ErrorEntry, FixTry) and KB cases -> report
(RunReport) -> fleet (FleetState). Plugins, report.json and the KB files read these shapes: a change is
recorded in docs/INTERFACES.md. The ErrorCaseRecord / FixAttemptRecord / CatalogCase family, ScriptMeta and
FixLabel only read older KB records; current code never fills them.
Paths are relative to Workspace.root unless the field name ends with `_abs` or is a log / output path.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_serializer


def utcnow() -> datetime:
    """timezone 정보가 있는 UTC 현재 시각을 반환한다. 레코드 시각 필드의 기본값으로 쓴다."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Enum
# ---------------------------------------------------------------------------
class CaseType(str, Enum):
    """분석 단계가 정하는 프로젝트 종류. 회사의 네 가지 프로젝트 유형과 unknown이 있다.
    계획과 보고서 내용이 이 값에 따라 달라진다."""
    PURE_JAVA = "pure_java"            # Spring도 서블릿도 없는 순수 Java 프로젝트
    SPRING_LEGACY = "spring_legacy"    # Boot 없이 Spring Framework 4/5를 쓰는 프로젝트
    SPRING_BOOT = "spring_boot"        # Spring Boot 2.x / 3.x 프로젝트
    SERVLET_TOMCAT = "servlet_tomcat"  # Tomcat에 올리는 서블릿, JSP 프로젝트(war, web.xml)
    MSA = "msa"                        # 저장소 하나에 배포 단위 서비스가 여러 개인 프로젝트
    UNKNOWN = "unknown"


class Phase(str, Enum):
    """실행 단계 이름. 실제로 쓰는 단계는 intake, analyze, plan, transform, restructure, verify, report다.
    jdk21 / spring은 수정 루프 내부 기본값과 이전 기록을 읽을 때만 쓴다."""
    INTAKE = "intake"
    ANALYZE = "analyze"
    PLAN = "plan"
    TRANSFORM = "transform"      # 모든 OpenRewrite 레시피를 빌드 없이 한 번에 실행하는 단계
    JDK21 = "jdk21"              # UpgradeToJava21과 수정 루프 단계
    SPRING = "spring"            # jakarta, Boot 4, Framework 7 레시피와 수정 루프 단계
    RESTRUCTURE = "restructure"  # 부모, 필수, 선택 아티팩트와 모듈 분리. MSA는 pom만 고친다
    VERIFY = "verify"            # 마지막 `mvn clean package`
    REPORT = "report"


class StepKind(str, Enum):
    """계획 단계(PlanStep)의 종류. 오케스트레이터가 이 값을 보고 실행 방법을 고른다.
    지금 계획은 openrewrite / build / restructure / msa_pom / review만 만든다."""
    OPENREWRITE = "openrewrite"
    RESTRUCTURE = "restructure"    # MSA가 아닌 프로젝트의 레이아웃 변환
    MSA_POM = "msa_pom"            # MSA에서 pom만 고치는 부모, 필수, 선택 아티팩트 수정
    BUILD_FIX_LOOP = "build_fix_loop"
    BUILD = "build"                # 수정 없이 한 번만 하는 빌드(verify)
    REVIEW = "review"              # 첫 빌드 전에 pom, 의존성, 모듈을 LLM으로 검토하는 구조 검토


class ErrorKind(str, Enum):
    """빌드 로그 파서가 오류에 붙이는 종류.
    L2 서명의 첫 부분이다. 케이스 분류, 차단 정책, 보고서 묶음에 쓰인다."""
    COMPILE = "compile"
    TEST_COMPILE = "test_compile"
    DEPENDENCY_RESOLUTION = "dependency_resolution"
    PLUGIN_RESOLUTION = "plugin_resolution"
    PARENT_RESOLUTION = "parent_resolution"
    PLUGIN_EXECUTION = "plugin_execution"   # 컴파일러가 아닌 플러그인이 실행 중에 실패한 경우
    ENFORCER = "enforcer"
    POM_INVALID = "pom_invalid"
    REWRITE = "rewrite"                     # OpenRewrite 실행 실패
    TEST_FAILURE = "test_failure"           # 테스트를 실행할 때만 생긴다
    UNKNOWN = "unknown"


class DecisionKind(str, Enum):
    """트리아지가 케이스마다 정하는 처리 방법.
    - skip: 다른 케이스에 가려져 이번 라운드는 기다린다
    - escalate: 포기하고 보고서로 넘긴다
    - blocked: 이 프로젝트 안에서는 고칠 수 없다
    - script: 저장된 스크립트 재사용, 보강, 신규 중 하나를 실행한다"""
    SKIP = "skip"
    ESCALATE = "escalate"
    BLOCKED = "blocked"
    SCRIPT = "script"


# 시도 한 번의 재빌드 결과. improved가 아니면 다음 시도 전에 되돌리고, regressed는 되돌린 뒤 reverted가 된다
AttemptOutcome = Literal["pending", "improved", "no_change", "regressed", "reverted", "build_ok", "apply_failed",
                         "blocked", "partial", "inconclusive"]
# - partial: 대상 오류 일부만 없어지고 바꾼 파일에 새 오류는 없다. 한도와 KB 실패 수에는 실패로 세지 않고 성공으로도 세지 않는다
# - inconclusive: 평가 빌드가 타임아웃, 해석할 수 없는 실패, 시도와 무관한 앞 단계 중단으로 오류를 보여 주지 못했다.
#   KB에는 성공도 실패도 기록하지 않고 시도한 것으로만 센다


# 오류 목록 항목의 상태.
# open은 마지막 빌드에 남은 오류, pom-verify는 재구성이 고친 pom을 다음 빌드로 확인하는 상태, note는 고치지 않는 메모다
EntryStatus = Literal["open", "pom-verify", "resolved", "blocked", "unresolved", "note"]
# 시도의 출처.
# - kb-script: 저장된 스크립트를 LLM 없이 재사용
# - llm-new / llm-augment / llm-edit: LLM의 신규 스크립트, 실패한 스크립트 보강, 스크립트 대신 준 편집 목록
# - review: 구조 검토가 직접 한 수정
# - sbmig-*: LLM 없이 sbmig가 직접 고치는 사전 시도. 저장 스크립트는 남지 않는다
#   version은 빠진 의존성 버전, resources는 필터링 제외 확장자, jakarta는 jakarta 빌드로 교체,
#   bundled는 프로젝트에 들어 있는 jar 연결, parent는 저장소에 없는 사내 부모 버전을 설정 버전으로 맞춘다
TrySource = Literal["kb-script", "llm-new", "llm-augment", "llm-edit", "review", "sbmig-version", "sbmig-resources",
                    "sbmig-jakarta", "sbmig-bundled", "sbmig-parent"]
# sbmig-version은 as-is 빌드의 버전을 쓰고, 없으면 저장소의 최신 릴리스를 쓴다.
# sbmig-bundled는 저장소에 좌표가 있으면 그 좌표를, 없으면 <module>/lib의 system scope와 includeSystemScope를 쓴다
TryApplied = Literal["script", "edits", "none"]  # 실제로 적용된 것. 스크립트, 편집 목록, 없음 중 하나


class BlockReason(str, Enum):
    """케이스를 차단한 이유. 보고서의 차단 섹션과 `loop.blocked_policy`가 이 값을 본다.
    차단은 이 프로젝트 안에서 고칠 수 없다는 뜻이지 실행이 실패했다는 뜻이 아니다."""
    EXTERNAL_JAVAX_API = "external_javax_api"            # 의존성이 API에 아직 javax 타입을 쓴다
    EXTERNAL_API_INCOMPATIBLE = "external_api_incompatible"  # javax가 아닌 이유로 의존성 API가 바뀌거나 없어졌다
    ARTIFACT_NOT_FOUND = "artifact_not_found"            # 어느 저장소에도 없는 의존성이나 플러그인
    PARENT_NOT_FOUND = "parent_not_found"
    PLUGIN_INCOMPATIBLE = "plugin_incompatible"          # JDK 21에서 실패하고 알려진 최소 버전이 없는 플러그인
    UNSUPPORTED_SOURCE = "unsupported_source"            # 다루지 않는 kotlin, groovy, scala, 생성 코드
    ENCODING = "encoding"                                # 소스 인코딩을 알아내지 못했다
    BUDGET_EXHAUSTED = "budget_exhausted"
    LLM_UNAVAILABLE = "llm_unavailable"
    REPEATED_FAILURE = "repeated_failure"                # 허용된 시도를 다 쓰고 포기했다
    OSCILLATION = "oscillation"                          # 수정이 같은 오류 묶음 사이를 오간다
    # reactor에 같은 모듈이 두 번 들어간 pom 구조 문제. 접수에서 중복을 없애고, 그래도 남으면 LLM 없이 인수인계한다
    REACTOR_DUPLICATE = "reactor_duplicate"
    # 저장소가 응답하지 않아 의존성을 받지 못한 경우다(연결 타임아웃, 5xx, "Could not transfer artifact").
    # 프로젝트 문제가 아니다. LLM에 요청하지 않고, "찾을 수 없음"으로 보지 않고, 저장 스크립트 결과에도 넣지 않는다
    REPOSITORY_UNREACHABLE = "repository_unreachable"
    # Windows에서 다른 프로세스가 target/의 파일을 잠가 빌드가 실패한 경우다. 빌드 환경 문제라서 LLM에 요청하지 않는다.
    # 빌드 실행기가 이미 다시 시도했다. 바로 인수인계한다
    FILE_LOCK = "file_lock"
    # LLM이 수정 없이 프로젝트 밖의 문제라고 답한 경우다. 원인이 같은 다른 케이스는 LLM을 호출하지 않고 차단한다
    NEEDS_OWNER = "needs_owner"


class BlockedItem(BaseModel):
    """수정 루프가 더 이상 고치지 않기로 한 오류 묶음(클러스터) 하나를 담는 클래스.
    보고서의 차단 섹션과 인수인계 섹션이 사람이 할 일, 원인 라이브러리와 함께 보여 준다."""
    id: str
    reason: BlockReason
    phase: Phase
    cluster_id: str
    signatures: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    errors: list[BuildError] = Field(default_factory=list)      # 정리한 오류, 최대 10개 정도
    artifact: Optional[GAV] = None                              # 원인으로 지목한 라이브러리나 아티팩트
    owner_class: Optional[str] = None                           # 맞지 않는 API를 가진 라이브러리 클래스
    required_type: Optional[str] = None                         # 요구 타입. 예: javax...HttpServletRequest
    found_type: Optional[str] = None                            # 실제 타입. 예: jakarta...HttpServletRequest
    suggestion: str = ""                                        # 사람이 할 일
    attempts: int = 0                                           # 차단 전에 한 수정 시도 수
    first_seen_iteration: int = 0
    notes: list[str] = Field(default_factory=list)
    # 원인이 javax API jar뿐이면 dependency:tree에서 그 jar를 끌어온 경로(via)와 직접 의존성(via_direct)을 남긴다.
    # 사람이 버전을 올려야 하는 대상은 API jar가 아니라 via_direct다. via는 루트부터 API jar까지의 좌표 목록이다
    via: list[str] = Field(default_factory=list)
    via_direct: Optional[GAV] = None


# ---------------------------------------------------------------------------
# Maven, 프로젝트 정보
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# pom 없는 Tomcat 디렉토리를 읽기만 하는 `sbmig inventory`의 결과.
# 그런 디렉토리는 접수에서 거부한다. 거부 안내를 쓸 때만 쓴다
# ---------------------------------------------------------------------------
class JarInfo(BaseModel):
    """`sbmig inventory`가 찾은 WEB-INF/lib의 jar 하나를 담는 클래스.
    파일 이름으로 추정한 버전과 좌표는 추정값으로 따로 둔다."""
    path: str                                   # 루트 기준 경로
    name: str                                   # 파일 이름
    size: int = 0
    sha1: str = ""                              # 나중에 Nexus, Central을 조회할 때 쓰는 sha1
    version_guess: Optional[str] = None         # 파일 이름에서 읽은 버전. `commons-lang-2.6.jar`면 2.6
    artifact_guess: Optional[str] = None        # jar_names.yml에서 찾은 "g:a". 모르면 None
    ignored: bool = False                       # Tomcat이 읽지 않는 백업 파일(*.jar.old, *.bak 등)


class WebappRoot(BaseModel):
    """`sbmig inventory`가 찾은, WEB-INF가 있는 웹 루트 하나를 담는 클래스.
    web.xml 요약, JSP와 정적 파일 수, lib jar, 클래스 파일의 Java 버전을 담는다."""
    dir: str                                    # WEB-INF가 있는 디렉토리. 빈 문자열이면 프로젝트 루트
    web_xml: Optional[str] = None               # web.xml이 있으면 그 경로
    servlet_spec: Optional[str] = None          # web.xml 루트 요소에서 읽은 버전. "2.3"(DTD), "2.4" ~ "4.0"
    servlets: int = 0
    filters: int = 0
    listeners: int = 0
    taglibs: int = 0
    # spring-mvc, struts1, struts2, egov, jsf, plain-servlet 중 찾은 것
    frameworks: list[str] = Field(default_factory=list)
    jndi_resources: list[str] = Field(default_factory=list)   # context.xml과 web.xml resource-ref의 이름
    jsp_count: int = 0
    jsp_sample: list[str] = Field(default_factory=list)
    static_count: int = 0                       # WEB-INF 밖의 html, js, css, 이미지 수
    spring_xml: list[str] = Field(default_factory=list)   # <beans가 있는 WEB-INF 아래 xml
    lib_jars: list[JarInfo] = Field(default_factory=list)
    ignored_files: list[str] = Field(default_factory=list)   # WEB-INF/lib의 백업이나 jar가 아닌 파일
    classes_count: int = 0                      # WEB-INF/classes의 .class 파일 수
    class_major_min: Optional[int] = None       # 클래스 파일 major 버전. 50은 Java 6, 52는 Java 8
    class_major_max: Optional[int] = None
    java_version_from_classes: Optional[str] = None   # class_major_max로 정한 Java 버전("1.6", "1.8", "11" 등)
    classes_without_source: int = 0             # WEB-INF/classes에 있는데 .java가 어디에도 없는 클래스 수
    classes_without_source_sample: list[str] = Field(default_factory=list)


class SourceRoot(BaseModel):
    """`sbmig inventory`가 찾은, `package` 선언과 경로가 맞는 Java 소스 루트 하나를 담는 클래스.
    인코딩, javax와 프레임워크 import 수를 집계한다."""
    dir: str                                    # `package a.b;`가 경로와 맞는 디렉토리
    java_files: int = 0
    packages: list[str] = Field(default_factory=list)      # 최상위 패키지(a.b)
    encoding: str = "utf-8"                     # 가장 많이 나온 인코딩(cp949, euc_kr, utf-8)
    javax_imports: dict[str, int] = Field(default_factory=dict)      # javax 패키지별 파일 수
    # spring, struts1, struts2, ibatis2, mybatis, hibernate, egov별 파일 수
    framework_imports: dict[str, int] = Field(default_factory=dict)
    jdk_internal_imports: int = 0               # sun.*, com.sun.*을 import하는 파일 수
    inside_webapp: Optional[str] = None         # 이 루트가 들어 있는 WebappRoot.dir. 없으면 None


class ModuleProposal(BaseModel):
    """`sbmig inventory`가 pom을 만든다면 생길 Maven 모듈 하나를 추정한 클래스.
    지금은 inventory.md에 보여 주기만 한다."""
    name: str                                   # 추정 artifactId. 웹 디렉토리 이름, 루트면 "app"
    webapp_dir: str
    source_dirs: list[str] = Field(default_factory=list)
    packaging: str = "war"
    java_version: str = "1.8"                   # 클래스 파일, 소스 순으로 정하고 없으면 1.8
    dependencies_known: int = 0                 # 좌표를 추정한 jar 수
    dependencies_unknown: int = 0               # 좌표를 모르는 jar 수. lib/와 system scope나 Nexus 설치 대상
    notes: list[str] = Field(default_factory=list)


class WebappInventory(BaseModel):
    """`sbmig inventory <dir>`의 전체 결과(inventory.md / inventory.json)를 담는 클래스.
    디렉토리는 바꾸지 않는다."""
    root: str
    scanned_at: datetime = Field(default_factory=utcnow)
    build_files: list[str] = Field(default_factory=list)   # 어디서든 찾은 pom.xml, build.xml, .classpath, .project
    webapp_roots: list[WebappRoot] = Field(default_factory=list)
    source_roots: list[SourceRoot] = Field(default_factory=list)
    duplicate_classes: int = 0                  # 소스 루트 두 곳 이상에 있는 같은 FQCN 수
    duplicate_classes_sample: list[str] = Field(default_factory=list)
    proposals: list[ModuleProposal] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    outputs: dict[str, str] = Field(default_factory=dict)  # inventory_md, inventory_json 경로
    adf: Optional[AdfSummary] = None            # ADF 흔적이 있을 때의 판단과 규모


# ---------------------------------------------------------------------------
# web.xml 해석 결과. 내장 Tomcat과 회사 Boot WAR는 web.xml을 읽지 않는다
# 그래서 재구성이 이 값으로 등록 빈(WebXmlRegistrations)을 만든다
# Servlet 3 이전 프로젝트는 @WebServlet이 없다. 이 값이 없으면 서블릿과 필터 매핑이 모두 사라진다
# ---------------------------------------------------------------------------
class WebXmlServlet(BaseModel):
    """web.xml의 `<servlet>` 하나와 그 매핑을 담는 데이터 클래스.
    재구성이 ServletRegistrationBean으로 옮긴다."""
    name: str
    servlet_class: Optional[str] = None            # 서블릿 FQCN. jsp-file 서블릿이면 None
    jsp_file: Optional[str] = None
    url_patterns: list[str] = Field(default_factory=list)
    init_params: dict[str, str] = Field(default_factory=dict)
    load_on_startup: Optional[int] = None
    async_supported: bool = False
    multipart_location: Optional[str] = None       # <multipart-config>가 있을 때의 위치. 아래 셋은 크기 값
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
    dispatchers: list[str] = Field(default_factory=list)   # REQUEST, FORWARD, INCLUDE, ERROR, ASYNC
    init_params: dict[str, str] = Field(default_factory=dict)
    async_supported: bool = False
    order: int = 0                                 # 첫 <filter-mapping>의 위치. web.xml 체인 순서


class WebXmlErrorPage(BaseModel):
    """web.xml의 `<error-page>` 하나를 담는 데이터 클래스. 재구성이 ErrorPageRegistrar로 옮긴다."""
    location: str
    error_code: Optional[int] = None
    exception_type: Optional[str] = None           # 예외 FQCN. error_code도 None이면 기본 오류 페이지


class WebXmlModel(BaseModel):
    """web.xml 파일 하나의 해석 결과를 담는 데이터 클래스.
    analyze.webxml이 만들고 재구성의 web.xml 등록 단계가 읽는다."""
    path: str                                      # web.xml의 루트 기준 경로
    spec: Optional[str] = None                     # 서블릿 스펙 버전. "2.3", "3.1" 등
    display_name: Optional[str] = None
    servlets: list[WebXmlServlet] = Field(default_factory=list)
    filters: list[WebXmlFilter] = Field(default_factory=list)
    listeners: list[str] = Field(default_factory=list)          # 선언 순서대로의 리스너 FQCN
    session_timeout_minutes: Optional[int] = None
    welcome_files: list[str] = Field(default_factory=list)
    error_pages: list[WebXmlErrorPage] = Field(default_factory=list)
    mime_mappings: dict[str, str] = Field(default_factory=dict)  # 확장자별 MIME 타입
    context_params: dict[str, str] = Field(default_factory=dict) # 참고용. 실제 처리는 외부 설정 분석이 한다
    security_constraints: int = 0
    login_config: Optional[str] = None             # auth-method 값
    resource_refs: list[str] = Field(default_factory=list)
    taglibs: int = 0
    distributable: bool = False
    warnings: list[str] = Field(default_factory=list)   # 해석하지 못한 부분


class JavaxApiJar(BaseModel):
    """이관 뒤에도 모듈 classpath에 남은 javax EE API jar와 그 jar를 끌어온 의존성 경로를 담는 클래스.
    build.deptree가 만들고, 보고서의 javax API jar 섹션과 차단 사유(via_direct)가 읽는다.

    Also a javax-era `jakarta.*-api` 2.x. Report section "javax API jars still on the classpath"
    (report.javax_api_inventory); path from `mvn dependency:tree`."""
    artifact: GAV                                               # API jar. 예: javax.persistence-api:2.2
    module: Optional[str] = None                                # 모듈 디렉토리. 빈 문자열이나 None이면 루트
    scope: Optional[str] = None                                 # compile, provided, runtime, test
    path: list[str] = Field(default_factory=list)               # 루트부터 API jar까지 tree가 출력한 좌표 목록
    via: Optional[GAV] = None                                   # 그 경로에 있는 모듈의 직접 의존성
    declared: bool = False                                      # 모듈 pom이 API jar를 직접 선언했는지


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
    path: str                                  # 루트 기준 경로. 예: "pom.xml", "service-a/pom.xml"
    group_id: Optional[str] = None             # 실제 적용되는 groupId. 없으면 부모에서 물려받은 값
    artifact_id: str
    version: Optional[str] = None
    packaging: str = "jar"
    parent: Optional[GAV] = None
    parent_relative_path: Optional[str] = None
    parent_in_repo: bool = False               # 부모 pom이 이 저장소 안에 있는지
    modules: list[str] = Field(default_factory=list)
    properties: dict[str, str] = Field(default_factory=dict)
    # 의존성 목록. 저장소 안의 properties, dependencyManagement로 버전을 풀 수 있으면 푼다
    dependencies: list[GAV] = Field(default_factory=list)
    dependency_management: list[GAV] = Field(default_factory=list)
    # 이 pom(프로필 포함)에 선언된 <repositories>, <pluginRepositories> URL. properties를 치환한 값
    repositories: list[str] = Field(default_factory=list)
    plugins: list[PluginInfo] = Field(default_factory=list)
    java_version: Optional[str] = None         # maven.compiler.*, java.version, 컴파일러 플러그인에서 읽은 버전
    spring_boot_version: Optional[str] = None
    spring_version: Optional[str] = None
    has_spring_boot_plugin: bool = False
    is_root: bool = False                      # 저장소 안에 부모가 없는 pom. 상속 체인의 맨 위
    is_aggregator: bool = False                # modules가 있는 packaging pom


class SourceStats(BaseModel):
    """소스 파일 통계를 담는 데이터 클래스.
    main 클래스, 서블릿, javax import, web.xml, JSP와 정적 리소스 위치를 담는다.
    프로젝트 종류를 정하고 재구성(front 모듈, web.xml 등록, 큰 미디어 파일 안내)을 할 때 근거로 쓴다."""
    java_files: int = 0
    test_files: int = 0
    kotlin_files: int = 0
    packages: list[str] = Field(default_factory=list)
    base_package: Optional[str] = None
    main_classes: list[str] = Field(default_factory=list)        # @SpringBootApplication이 붙은 클래스 FQCN
    psvm_classes: list[str] = Field(default_factory=list)        # Boot가 아닌 main 메서드 클래스 FQCN
    servlet_classes: list[str] = Field(default_factory=list)     # HttpServlet 상속이나 @WebServlet 클래스
    filter_listener_classes: list[str] = Field(default_factory=list)
    controllers: list[str] = Field(default_factory=list)         # @Controller, @RestController 클래스
    javax_imports: dict[str, int] = Field(default_factory=dict)  # javax 패키지별 import 수
    jakarta_imports: dict[str, int] = Field(default_factory=dict)
    spring_annotations: dict[str, int] = Field(default_factory=dict)
    has_web_xml: bool = False
    web_xml_paths: list[str] = Field(default_factory=list)
    jsp_count: int = 0
    # as-is 웹 리소스. JSP 파일과 정적 파일 디렉토리(WEB-INF를 뺀 `src/main/webapp`,
    # `src/main/resources/{static,public,templates}`)를 루트 기준 경로로 정렬해 둔다
    jsp_paths: list[str] = Field(default_factory=list)
    web_static_dirs: list[str] = Field(default_factory=list)
    web_static_count: int = 0
    # <beans xmlns="http://www.springframework.org/schema/beans">가 있는 파일
    xml_bean_configs: list[str] = Field(default_factory=list)
    config_files: list[str] = Field(default_factory=list)        # application*.yml/properties, bootstrap*, logback*
    spring_boot_test_classes: list[str] = Field(default_factory=list)
    lombok: bool = False
    # 첫 web.xml의 선언 내용. 재구성이 WebXmlRegistrations로 바꾼다(`layout.web_xml_registrations`)
    web_xml_spec: Optional[str] = None                           # "2.3"(DTD) ~ "6.0". web.xml이 없거나 모르면 None
    web_xml_servlets: int = 0
    web_xml_filters: int = 0
    web_xml_listeners: int = 0
    jsp_javax_files: int = 0                                     # import나 스크립틀릿에 javax EE 타입이 있는 JSP, tag 수
    # pom들의 웹 디렉토리. `src/main/webapp`이 있으면 넣고 war 플러그인의 `<warSourceDirectory>`, `<webResources>`도 넣는다.
    # jsp_paths, web_static_dirs, web_static_count도 이 디렉토리를 포함한다.
    # 디렉토리 종류는 기록하지 않는다. 재구성이 pom을 다시 읽는다(`source_scanner.pom_web_dirs`)
    webapp_dirs: list[str] = Field(default_factory=list)
    # 웹 디렉토리나 classpath 리소스 아래 5MB(LARGE_MEDIA_BYTES) 이상 미디어, 압축 파일 목록과 전체 바이트 수.
    # 목록은 정렬해서 20개까지 두고 재구성 안내에 쓴다
    large_media_files: list[str] = Field(default_factory=list)
    large_media_bytes: int = 0


# ---------------------------------------------------------------------------
# 외부 설정: 코드가 읽는데 패키지 안 리소스 어디에도 정의되지 않은 설정
# ---------------------------------------------------------------------------
# as-is 코드가 외부 설정을 읽는 방식(@Value, @ConfigurationProperties, Environment, XML ${…} 등).
# config-placeholder는 패키지 안 설정 파일 값 속의 `${…}`이다
PropertyRefSource = Literal["value-annotation", "configuration-properties", "environment", "system-property",
                            "system-env", "xml-placeholder", "context-param", "jndi", "property-source", "profiles",
                            "config-placeholder"]   # `x=${DB_URL}`: DB_URL is read at runtime
PropertyStatus = Literal["defined", "defaulted", "external"]


class PropertyRef(BaseModel):
    """as-is 코드나 XML이 외부 설정 키를 읽는 위치 하나를 담는 데이터 클래스."""
    key: str                                       # 쓰인 그대로의 키. "db.url", "java:comp/env/jdbc/x", "app.*"(접두어)
    source: PropertyRefSource
    file: str                                      # 분석 시점의 루트 기준 경로
    line: Optional[int] = None
    default: Optional[str] = None                  # `${key:default}`나 System.getProperty(k, default)의 기본값
    scope: Literal["main", "test"] = "main"
    raw: str = ""                                  # 찾은 원문. 정리해서 200자까지만 둔다


class PropertyFinding(BaseModel):
    """외부 설정 키(또는 접두어) 하나와 그 값이 어디에 있는지를 담는 데이터 클래스.
    - defined: 패키지에 들어가는 리소스에 값이 있다
    - defaulted: 코드의 기본값만 있다
    - external: 어디에도 없어서 운영자가 값을 넣어야 한다

    `boot_keys` is what sbmig writes into application.properties when it differs from `key`
    (JNDI DataSource -> spring.datasource.*, context-param -> server.servlet.context-parameters.*)."""
    key: str
    status: PropertyStatus
    refs: list[PropertyRef] = Field(default_factory=list)
    defined_in: list[str] = Field(default_factory=list)     # 이 키를 정의한 패키지 안 파일. 프로필 파일 포함
    boot_keys: list[str] = Field(default_factory=list)      # 이 항목으로 기록할 키. 기본값은 [key]
    # as-is에서 값을 넣던 경로. "jndi", "context-param", "system-property", "property-source:file", 모르면 None
    supplied_by: Optional[str] = None
    note: str = ""                                 # knowledge/config/externalized.yml의 규칙 메모
    # MSA에서 이 키를 읽는 서비스. None이면 어느 서비스에도 속하지 않은 공유 코드이고 모든 서비스의 리소스와 비교한다
    service: Optional[str] = None


class PropertyInventory(BaseModel):
    """외부 설정 목록 전체를 담는 데이터 클래스.
    분석 때는 ProjectProfile.properties, 재구성 뒤에는 RunReport.properties에 들어간다.
    인수인계의 '설정할 키' 표와 필터링 대상 바이너리 목록을 여기서 만든다."""
    findings: list[PropertyFinding] = Field(default_factory=list)
    packaged_files: list[str] = Field(default_factory=list)     # 정의를 찾으려고 검사한 리소스 파일
    profiles: list[str] = Field(default_factory=list)           # application-<p>.* 이름, 활성 프로필 값
    filtered_resources: list[str] = Field(default_factory=list) # <filtering>true</filtering>인 리소스 디렉토리
    # 필터링 리소스 안에 있어 필터를 거치게 되는 바이너리 파일(웹폰트, 키스토어, 압축 파일 등).
    # 확장자는 BINARY_RESOURCE_EXTENSIONS이고 includes와 excludes를 따른다. 개수 제한이 있다
    filtered_binary_files: list[str] = Field(default_factory=list)
    placeholder_files: list[str] = Field(default_factory=list)  # sbmig 플레이스홀더 블록을 넣은 파일
    placeholders_written: list[str] = Field(default_factory=list)   # 표시 값으로 기록한 키
    notes: list[str] = Field(default_factory=list)
    # MSA에서 서비스별로 검사한 배포 단위. 비어 있으면 프로젝트 전체 목록이다
    services: list[str] = Field(default_factory=list)

    @property
    def external(self) -> list[PropertyFinding]:
        """어디에도 정의되지 않은 키(status가 external인 항목)만 반환한다."""
        return [f for f in self.findings if f.status == "external"]


class LibraryUsage(BaseModel):
    """프로젝트가 쓰는 라이브러리 하나(정규화된 이름과 근거)를 담는 데이터 클래스.
    권고와 프롬프트 힌트가 읽는다."""
    # 정규화한 이름. lombok, mapstruct, querydsl, spring-security, springdoc, swagger2, mybatis, jpa,
    # hibernate, feign, kafka, redis, jackson, junit4, junit5, mockito 등
    name: str
    version: Optional[str] = None
    evidence: list[str] = Field(default_factory=list)   # 근거. "pom:org.projectlombok:lombok" 같은 값


class ServiceInfo(BaseModel):
    """MSA 저장소 안의 서비스 하나를 담는 클래스.
    kind가 library면 함께 이관해야 하는 공유 라이브러리 루트다."""
    name: str
    root_pom: str                   # 서비스 최상위 pom의 경로
    case_type: CaseType
    kind: Literal["service", "library"] = "service"
    poms: list[str] = Field(default_factory=list)
    main_classes: list[str] = Field(default_factory=list)
    spring_boot_version: Optional[str] = None
    java_version: Optional[str] = None


class AdfSummary(BaseModel):
    """Oracle ADF(JDeveloper) 프로젝트인지에 대한 판단과 규모를 담는 데이터 클래스.
    분석 단계가 ``sbmig.analyze.adf.detect_adf``로 모아 ``ProjectProfile.adf``와 inventory에 남긴다.
    - 근거 네 종류(``bc4j``, ``java``, ``model``, ``faces``) 중 둘 이상이면 ``is_adf``로 변환을 중단한다.
      하나면 ``traces``로 표시하고 계속한다
    - 종류별 근거 경로는 사람이 판단을 검토할 때 쓰는 자료다
    - ADF는 변환하지 않는다. 규모는 집계용 정보다

    `is_adf` rejects the run with "ADF 프로젝트라 변환이 불가능합니다"; `traces` is a soft flag."""
    is_adf: bool = False
    traces: bool = False
    families: list[str] = Field(default_factory=list)                 # 찾은 근거 종류. bc4j, java, model, faces 순서
    evidence: dict[str, list[str]] = Field(default_factory=dict)      # 종류별 근거 경로. 종류마다 개수 제한이 있다
    evidence_counts: dict[str, int] = Field(default_factory=dict)     # 종류별 근거 파일 수. 제한 없이 센다
    evidence_total: int = 0
    hints: list[str] = Field(default_factory=list)                    # .jpr, adf-config.xml 같은 참고 정보. 근거가 아니다
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
    tables: list[str] = Field(default_factory=list)            # 중복 없는 DBObjectName. 개수 제한이 있다
    tables_total: int = 0
    # 최상위 디렉토리별 java, java_adf, bc4j, pages, page_defs 수
    by_dir: dict[str, dict[str, int]] = Field(default_factory=dict)


class FrameworkEvidence(BaseModel):
    """사내 프레임워크 아티팩트를 프로젝트의 어디에서 찾았는지 담는 클래스.
    - direct: 프로젝트 pom의 `<dependencies>`에 선언돼 있다
    - parent: 프로젝트 pom에는 없고 프로젝트 밖 부모 pom에서 들어온다
    - bundled: 프로젝트 안에 있지만 어느 pom도 선언하지 않은 jar다
    - transitive: 다른 의존성이 끌어온다(`mvn dependency:tree`)"""
    source: Literal["direct", "parent", "bundled", "transitive"]
    artifact: str                                  # "g:a". 좌표를 모르는 bundled jar면 파일 이름
    version: Optional[str] = None
    # 찾은 위치. direct는 pom 경로, parent와 transitive는 모듈, bundled는 jar 경로
    where: str = ""
    parent: Optional[str] = None                   # parent일 때만 모듈 부모 pom의 g:a:v. 모르면 None
    via: list[str] = Field(default_factory=list)   # transitive일 때만 g:a:v 경로. 프로젝트 자신의 의존성이 처음


class FrameworkUse(BaseModel):
    """프로젝트가 사내 프레임워크를 쓰는지 검사한 결과와 그에 따른 이관 범위를 담는 클래스.
    scope가 jdk-jakarta면 Java 21과 jakarta 레시피, 검증 빌드, 이어지는 취약점 조치만 실행한다.

    `fix_loop` says whether that verify build gets the fix loop. `unchecked` names the sources that could not
    be looked at (`"parent: no as-is effective pom"`, `"transitive: dependency:tree failed"`)."""
    patterns: list[str] = Field(default_factory=list)              # 설정한 framework.artifacts 그대로
    evidence: list[FrameworkEvidence] = Field(default_factory=list)
    unchecked: list[str] = Field(default_factory=list)
    scope: Literal["full", "jdk-jakarta"] = "full"
    reason: str = ""                               # 이 범위를 고른 이유. 영어 한 문장, 계획 메모와 저널에 쓴다
    # 화면과 보고서에 보여 줄 프레임워크 이름(FrameworkConfig.display_name). 정하지 못하면 빈 문자열이다
    name: str = ""
    # 이 실행에서 빌드 오류 수정 루프를 실행하는지(framework.fix_loop). 줄인 범위가 아니면 뜻이 없다
    fix_loop: bool = False

    @property
    def found(self) -> bool:
        """프레임워크 아티팩트를 한 곳이라도 찾았으면 True를 반환한다."""
        return bool(self.evidence)

    @property
    def reduced(self) -> bool:
        """JDK와 jakarta만 올리는 범위로 실행하면 True를 반환한다."""
        return self.scope == "jdk-jakarta"

    def keys(self) -> list[str]:
        """찾은 프레임워크 아티팩트의 ``g:a`` 목록을 중복 없이 반환한다. 좌표를 모르는 jar는 빼고 반환한다."""
        out: list[str] = []
        for e in self.evidence:
            if e.artifact.count(":") == 1 and e.artifact not in out:
                out.append(e.artifact)
        return out


class ProjectProfile(BaseModel):
    """분석 단계의 결과 전체(profile.json)를 담는 데이터 클래스.
    계획, 재구성, 구조 검토, 수정 프롬프트, 보고서가 모두 읽는다.
    build_tool이 maven이 아니면 접수에서 중단한다.

    `boot_modules`: module dirs matching `layout.boot_module_pattern` (e.g. `*-boot`) and confirmed by their
    pom (spring-boot-maven-plugin or a @SpringBootApplication class); Boot as-is / MSA pom-only edits give
    those `boot_parent`, the rest `comp_parent`."""
    root: str                                   # 분석 시점 프로젝트 루트의 절대 경로
    name: str
    case_type: CaseType
    confidence: float = 1.0
    reasons: list[str] = Field(default_factory=list)
    build_tool: Literal["maven", "gradle", "ant", "none"] = "maven"   # maven이 아니면 접수에서 중단한다
    poms: list[PomInfo] = Field(default_factory=list)
    root_pom: Optional[PomInfo] = None
    source: SourceStats = Field(default_factory=SourceStats)
    libraries: list[LibraryUsage] = Field(default_factory=list)
    java_version: Optional[str] = None
    spring_boot_version: Optional[str] = None
    spring_version: Optional[str] = None
    packaging: Optional[str] = None
    is_multi_module: bool = False
    services: list[ServiceInfo] = Field(default_factory=list)      # MSA일 때만 채운다
    optional_artifacts_in_use: list[GAV] = Field(default_factory=list)  # 설정의 선택 아티팩트 중 이미 쓰는 것
    mandatory_already_present: list[GAV] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    fingerprint: str = ""
    properties: PropertyInventory = Field(default_factory=PropertyInventory)   # 외부 설정 목록
    boot_modules: list[str] = Field(default_factory=list)      # Boot 앱 모듈 디렉토리
    # Oracle ADF 판단 결과와 규모. 근거가 하나라도 있을 때만 채운다
    adf: Optional[AdfSummary] = None
    # 프로젝트(WEB-INF/lib, lib/ 등)에 있는데 어느 pom도 선언하지 않은 jar. as-is IDE 빌드는 썼지만 Maven은 모른다.
    # `scan_bundled_jars`가 채우고 수정 루프가 `sbmig-bundled`로 연결한다
    bundled_jars: list["JarCoordinate"] = Field(default_factory=list)
    # 사내 프레임워크 검사 결과. framework.artifacts가 비어 있으면 None이다. 계획은 `framework.scope`를 보고 정한다
    framework: Optional[FrameworkUse] = None

    @property
    def reduced_scope(self) -> bool:
        """JDK와 jakarta만 올리는 범위(프레임워크 프로젝트)로 실행하면 True를 반환한다."""
        return self.framework is not None and self.framework.reduced

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
# 계획
# ---------------------------------------------------------------------------
class RecipeRef(BaseModel):
    """계획에 들어가는 OpenRewrite 레시피 하나를 담는 데이터 클래스.
    id는 knowledge/recipes/catalog.yml에 있는 것만 쓴다."""
    id: str                                        # 패키지까지 붙인 레시피 id
    source: Literal["builtin"] = "builtin"         # OpenRewrite 레시피만 쓴다. 프로젝트나 KB 레시피는 없다
    artifact: Optional[str] = None                 # 레시피 아티팩트 g:a. 버전은 설정에서 정한다


class PlanStep(BaseModel):
    """계획 단계 하나를 담는 데이터 클래스.
    계획이 만들고 오케스트레이터와 LangGraph 엔진이 순서대로 실행한다.
    id는 transform.rewrite / restructure.* / restructure.review /
    verify.build 등이고, 서비스별 단계면 뒤에 접미사가 붙는다."""
    id: str                                        # 단계 id. "transform.rewrite", "restructure.apply" 등
    kind: StepKind
    phase: Phase
    title: str
    recipes: list[RecipeRef] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
    build_goal: Optional[str] = None               # compile, test-compile, package, verify
    max_iterations: int = 0
    target: Optional[str] = None                   # MSA 서비스 이름이나 모듈 경로. None이면 프로젝트 전체
    reason: str = ""
    optional: bool = False                         # True면 실패해도 실행을 중단하지 않는다


class MigrationPlan(BaseModel):
    """계획 단계의 결과(plan.json)를 담는 데이터 클래스.
    계획은 LLM이 아니라 규칙으로 정한다. `plan.advisor: llm`은 기본으로 꺼져 있다."""
    profile_fingerprint: str
    case_type: CaseType
    steps: list[PlanStep]
    notes: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    llm_usage: "LLMUsage" = Field(default_factory=lambda: LLMUsage())   # 계획 보조 LLM(레시피 모델) 사용량
    # 이관 범위. jdk-jakarta는 사내 프레임워크 프로젝트라 JDK와 jakarta만 올리는 계획이다
    scope: Literal["full", "jdk-jakarta"] = "full"


# ---------------------------------------------------------------------------
# 빌드, OpenRewrite 결과
# ---------------------------------------------------------------------------
class BuildError(BaseModel):
    """빌드 로그에서 뽑은 오류 한 건을 담는 데이터 클래스.
    L1 서명 `signature`와 L2 서명 `signature_generic`이 붙고, L2가 케이스를 정한다."""
    kind: ErrorKind
    message: str                                   # 정리한 첫 줄
    file: Optional[str] = None                     # 루트 기준 경로
    line: Optional[int] = None
    column: Optional[int] = None
    symbol: Optional[str] = None                   # javac "symbol:" 줄의 값
    location: Optional[str] = None                 # javac "location:" 줄의 값
    module: Optional[str] = None                   # Maven 모듈 artifactId나 이름
    artifact: Optional[GAV] = None                 # 받지 못한 의존성, 플러그인, 부모의 좌표
    goal: Optional[str] = None                     # "compiler:compile", "compiler:testCompile" 등
    raw: str = ""                                  # 로그에 나온 오류 블록 원문
    signature: str = ""                            # L1 서명(sbmig.build.signatures)
    signature_generic: str = ""                    # L2 서명

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
    log_path: str                                  # 절대 경로
    errors: list[BuildError] = Field(default_factory=list)
    modules_built: list[str] = Field(default_factory=list)
    failed_module: Optional[str] = None
    summary: str = ""                              # 관련 있는 마지막 40줄 정도
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
    recipes_applied: dict[str, list[str]] = Field(default_factory=dict)   # 레시피 id별로 바꾼 파일('by:' 블록)
    forked: bool = True                                                     # rewrite:run이면 True
    # rewrite 플러그인이 파싱하지 못한 파일(`There were problems parsing …` 줄).
    # 타입이 필요한 레시피는 이 파일을 말없이 건너뛴다
    parse_problems: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 트리아지, 수정
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
    priority: int = 100                            # 작을수록 먼저 처리한다
    # 이 클러스터의 케이스 id(sha1(signature_generic)[:10]). 파일이 달라도 L2 서명이 같으면 한 케이스이고
    # 이때 `file`은 None이다
    case_id: Optional[str] = None

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

    modify = search/replace (search must match exactly once), create / delete = whole file."""
    path: str
    op: Literal["modify", "create", "delete", "rename"]
    search: Optional[str] = None
    replace: Optional[str] = None
    content: Optional[str] = None
    new_path: Optional[str] = None


# 게이트웨이가 모델 이름 대신 돌려주는 값. 이런 값은 모델 이름으로 쓰지 않는다
PLACEHOLDER_MODEL_NAMES = frozenset({"", "?", "-", "unknown", "none", "null", "n/a", "default"})


def real_model_name(value: Any) -> str:
    """값이 실제 모델 이름이면 앞뒤 공백을 뗀 이름을 반환한다.
    문자열이 아니거나, 비었거나, `unknown` 같은 값이나 `${…}` 플레이스홀더면 빈 문자열을 반환한다."""
    if not isinstance(value, str):
        return ""
    v = value.strip()
    if v.lower() in PLACEHOLDER_MODEL_NAMES or "${" in v:
        return ""
    return v


def merge_model_names(*groups: Any) -> list[str]:
    """여러 모델 이름 목록을 처음 나온 순서대로 합친다. 대소문자만 다른 중복과 실제 이름이 아닌 값은 뺀다."""
    out: list[str] = []
    seen: set[str] = set()
    for g in groups:
        for n in (g or []):
            name = real_model_name(n)
            if name and name.lower() not in seen:
                seen.add(name.lower())
                out.append(name)
    return out


# 요청한 모델 이름을 하나도 알 수 없을 때 쓰는 이름. 게이트웨이가 정한 기본 모델로 호출한 경우다
DEFAULT_MODEL_LABEL = "gateway-default"


def asked_model_name(*candidates: Any) -> str:
    """후보 중 처음 나오는 실제 모델 이름을 반환한다. 하나도 없으면 DEFAULT_MODEL_LABEL을 반환한다.

    Candidates: the model a call asked with, then the configured defaults. A usage row is never named
    `unknown`, `?` or "" (docs/INTERFACES.md "v2.1z-bg")."""
    for c in candidates:
        name = real_model_name(c)
        if name:
            return name
    return DEFAULT_MODEL_LABEL


def served_beside(asked: str, *groups: Any) -> list[str]:
    """응답이 알려 준 모델 이름을 합치고 요청한 이름과 같은 것은 대소문자 구분 없이 뺀다."""
    a = (asked or "").strip().lower()
    return [n for n in merge_model_names(*groups) if n.lower() != a]


def _drop_empty_served(handler: Any, obj: Any) -> dict[str, Any]:
    # 응답 모델을 모르는 보고서의 JSON이 달라지지 않도록 served가 비면 키를 뺀다
    data = handler(obj)
    if isinstance(data, dict) and not data.get("served"):
        data.pop("served", None)
    return data


class LLMUsage(BaseModel):
    """LLM 사용량(호출 수, 입력 / 출력 토큰)을 담는 데이터 클래스.
    단계와 시도마다 더해서 RunReport.llm_usage를 만든다.
    model은 호출할 때 요청한 이름이고, served는 응답이 실제로 썼다고 알려 준 모델 이름이다.

    `model` on the agent gateway is the role name. `served` = a real top-level `model` and every
    `usage.by_model[].model` of the answers, merged as a set by `add`; left out of the JSON when empty."""
    model: str = ""
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    served: list[str] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def _ser(self, handler: Any) -> dict[str, Any]:
        return _drop_empty_served(handler, self)

    def add(self, other: "LLMUsage") -> "LLMUsage":
        """두 사용량을 더한 새 객체를 반환한다. 모델 이름은 먼저 값이 있는 쪽을 쓰고 served는 합친다."""
        return LLMUsage(
            model=self.model or other.model,
            calls=self.calls + other.calls,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            served=merge_model_names(self.served, other.served),
        )


class ModelCost(BaseModel):
    """한 실행에서 모델 하나의 사용량과 `llm.pricing`으로 계산한 비용을 담는 데이터 클래스.
    가격 정보가 없으면 cost는 None이다.

    Prices are currency per 1M tokens."""
    model: str
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    price_key: str = ""                            # 맞은 llm.pricing 항목. 빈 문자열이면 가격 없음
    input_per_mtok: Optional[float] = None
    output_per_mtok: Optional[float] = None
    cost: Optional[float] = None                   # 이 모델의 가격 설정이 없으면 None
    served: list[str] = Field(default_factory=list)   # 응답이 알려 준 모델 이름(LLMUsage.served)

    @model_serializer(mode="wrap")
    def _ser(self, handler: Any) -> dict[str, Any]:
        return _drop_empty_served(handler, self)


class LLMCost(BaseModel):
    """한 실행의 LLM 비용(모델별, 합계)을 담는 데이터 클래스.
    가격 정보가 없는 모델은 합계에서 빠지고 unpriced_models에 남는다.

    `RunReport.llm_cost`. `usage_missing` = calls were made but no token counts came back."""
    currency: str = "USD"
    total: float = 0.0
    by_model: list[ModelCost] = Field(default_factory=list)
    unpriced_models: list[str] = Field(default_factory=list)
    usage_missing: bool = False
    note: str = ""                                 # llm.pricing_note. 가격 출처와 기준 시점


# ---------------------------------------------------------------------------
# 수정 스크립트.
# LLM이 쓴 `def fix(p)`를 AST로 정적 검사한 뒤 별도 `python -I` 프로세스에서 제한된 API `p`로 실행한다.
# 저장 위치는 `<kb>/cases/<case_id>/fix_v<N>.py`와 case.json(CaseScript)이다. 실행은 sbmig.script가 맡는다
# ---------------------------------------------------------------------------
# new는 신규 작성(LLM), reuse는 저장된 스크립트 재사용(LLM 없음), augment는 이전 버전 보강(LLM)이다
ScriptMode = Literal["new", "reuse", "augment"]
# 스크립트 실행 결과. error는 예외나 경로 거부, timeout은 script.timeout_seconds가 지나 강제 종료,
# rejected는 실행 전에 정적 검사에서 거부된 경우다(금지된 import, 이름, dunder 접근, fix(p) 없음)
ScriptRunStatus = Literal["ok", "error", "timeout", "rejected"]


class ScriptRunResult(BaseModel):
    """수정 스크립트를 한 번 실행한 결과를 담는 데이터 클래스. 상태, 바꾼 파일, p.log 줄, 오류를 담는다.
    이것은 스크립트가 스스로 보고한 내용이다. 정확한 변경 내용은 수정 루프가 남기는 git diff에 있다.

    Produced by sbmig.script.ScriptRunner.run."""
    status: ScriptRunStatus = "ok"
    changed_files: list[str] = Field(default_factory=list)    # 쓰거나 만들거나 지우거나 옮긴 파일의 상대 경로
    log: list[str] = Field(default_factory=list)              # p.log(...) 줄. script.log_lines까지만 둔다
    error: str = ""                                           # 예외, 거부, 타임아웃 문구. ok면 빈 문자열
    problems: list[str] = Field(default_factory=list)         # rejected일 때 정적 검사가 찾은 문제
    duration_s: float = 0.0


class ScriptMeta(BaseModel):
    """이전 KB의 스크립트 메타 정보(`<kb>/scripts/<case_id>/v<N>.meta.json`)를 담는 데이터 클래스.
    지금은 case.json의 CaseScript가 같은 역할을 한다."""
    case_id: str
    signature_generic: str = ""                    # 케이스의 L2 서명
    version: int                                   # 케이스별 버전 번호. 1부터
    parent: Optional[int] = None                   # 보강한 원래 버전. 처음부터 쓴 스크립트면 None
    schema_version: int = 3
    sha1: str = ""                                 # 코드의 sha1. 같은 스크립트는 한 번만 저장한다
    created_at: datetime = Field(default_factory=utcnow)
    run_id: str = ""
    project: str = ""
    model: str = ""
    source: Literal["llm", "human", "review"] = "llm"
    analysis: str = ""                             # LLM의 <analysis>. 마스킹한 값
    successes: int = 0                             # 케이스의 대상 오류를 모두 없앤 실행 수
    failures: int = 0                              # no_change, regressed, apply_failed, 스크립트 오류 수
    partials: int = 0                              # 대상 오류가 일부만 없어진 실행 수
    success_projects: list[str] = Field(default_factory=list)
    last_outcome: str = ""                         # 마지막 실행의 AttemptOutcome
    last_at: Optional[datetime] = None
    last_run_id: str = ""
    # retired는 사람이 내린 스크립트, quarantined는 코드가 비밀 값 패턴에 걸린 스크립트. 마스킹해 저장하고 고르지 않는다
    status: Literal["active", "retired", "quarantined"] = "active"
    notes: list[str] = Field(default_factory=list)
    # ok로 끝난 직후 다시 실행했을 때 또 파일을 바꾼 스크립트(비멱등). 기록은 남기지만 재사용 대상에서는 뺀다
    non_idempotent: bool = False


class ScriptBase(BaseModel):
    """보강 요청에서 고칠 대상 스크립트를 담는 데이터 클래스.
    이전 버전 코드와 이 프로젝트에서의 결과(남은 오류, 새 오류, 버전 이력)를 담는다.

    Carried in FixContext.script_base."""
    case_id: str
    version: int = 0                               # 0이면 이번 실행에서 아직 저장하지 않은 스크립트
    code: str
    outcome: str = ""                              # 이 프로젝트에서 실행한 AttemptOutcome. 실행 전이면 빈 문자열
    status: str = ""                               # 그 실행의 ScriptRunStatus
    error: str = ""
    log: list[str] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    remaining_errors: list[str] = Field(default_factory=list)    # 아직 남은 케이스 오류("file:line message")
    introduced_errors: list[str] = Field(default_factory=list)   # 바꾼 파일에 새로 생긴 오류("file:line message")
    history: list[str] = Field(default_factory=list)             # 이전 버전 이력. 예: "v1: 2 ok / 1 failed"
    session_id: Optional[str] = None               # 기준 시도의 답을 받은 게이트웨이 세션. 보강이 이어 쓴다
    # 그 세션을 연 모델. 빈 문자열이면 게이트웨이 기본 모델이다.
    # 세션은 모델을 바꿀 수 없다. 모델이 다르면 새 세션을 연다
    session_model: str = ""


# LLM이 직접 요청한 수정의 종류. replace는 문자열 치환, pom은 pom 편집기 호출이다
EditOp = Literal["replace", "write", "create", "delete", "move", "pom"]


class EditHunk(BaseModel):
    """적용된 diff에서 뽑은 변경 조각 하나를 담는 데이터 클래스.
    수정 전(as-is) 줄과 수정 후(to-be) 줄을 담는다.
    오류별 JSON과 `sbmig kb errors show`에서 사람이 읽는다. 기본으로는 자르지 않는다(`errors.snippet_lines: 0`)."""
    lines: str = ""                                # 줄 표기. "~12", "+42–47", "-3"처럼 EditNote.lines와 같다
    before: str = ""                               # 지우거나 바꾼 as-is 줄. 추가나 새 파일이면 빈 문자열
    after: str = ""                                # 추가하거나 바꾼 뒤의 줄. 삭제면 빈 문자열
    truncated: bool = False                        # errors.snippet_lines 때문에 잘렸는지


class EditNote(BaseModel):
    """사람이 읽는 수정 내역 한 건. where와 change는 LLM이 쓰고 `lines`와 hunks는 sbmig가 diff에서 채운다.
    LLM이 아무것도 쓰지 않은 파일은 diff로 메모를 만들어 '수정' 칸이 비지 않게 한다.

    `where` / `change` come from every `<edits>` item's `desc` and from the `<changes>` list of a script."""
    path: str
    where: str = ""                                # LLM이 쓴 위치. "<dependencies> 블록", "42행 부근" 등
    change: str = ""                               # LLM이 쓴 변경 설명 한 줄
    # diff에서 읽은 줄 표기. "+42–47"은 추가, "~12"는 변경, "-3"은 삭제이고 여러 개면 ", "로 잇는다
    lines: str = ""
    source: Literal["llm", "diff"] = "llm"         # 메모를 쓴 쪽
    hunks: list[EditHunk] = Field(default_factory=list)   # diff 순서대로의 변경 조각


class Edit(BaseModel):
    """LLM이 요청한 직접 수정 한 건(`<edits>` JSON)을 담는 데이터 클래스.
    sbmig가 수정 스크립트와 같은 헬퍼 API와 안전 검사로 적용한다.
    pom 편집은 `pom` 필드에 편집기 호출을 담는다.

    Project folder only, same guard as a fix script. `pom` example:
    `{"add_dependency": {"group_id": ..., "artifact_id": ..., "version": null}}`, `set_property`, ..."""
    op: EditOp
    path: str
    desc: str = ""                                 # LLM이 쓴 한 줄 설명. EditNote.change가 된다
    old: str = ""                                  # replace에서 바꿀 원문. count가 -1이면 모두 바꾼다
    new: str = ""
    count: int = -1
    content: str = ""                              # write, create의 내용
    to: str = ""                                   # move의 대상 경로
    pom: dict[str, Any] = Field(default_factory=dict)


class ScriptProposal(BaseModel):
    """수정기와 구조 검토의 답.
    원인 analysis, 해결 solution, 바로 적용할 edits, 저장할 스크립트 code, 운영자 확인 verify를 담는다.
    code가 비고 no_script_reason이 있으면 edits가 수정 전부다.

    Returned by FixerLike.propose_script (tier-2 seam). `edits` are applied directly, `code` is stored for
    reuse."""
    code: str = ""                                 # <script> 본문. `def fix(p)`가 있어야 한다. 빈 문자열이면 스크립트 없음
    analysis: str = ""
    solution: str = ""
    edits: list[Edit] = Field(default_factory=list)
    changes: list[EditNote] = Field(default_factory=list)   # `<changes>`. 스크립트나 편집이 어디를 바꾸는지
    no_script_reason: str = ""
    # `<verify>` 운영자 확인 사항. 메이저 업그레이드나 라이브러리 교체 뒤 실행 시점에 확인할 것. 없으면 빈 문자열
    verify: str = ""
    model: str = ""
    prompt_hash: str = ""
    llm_usage: LLMUsage = Field(default_factory=LLMUsage)
    session_id: Optional[str] = None               # 답을 받은 게이트웨이 세션. 다음 요청이 이어 쓴다
    session_model: str = ""                        # 그 세션을 연 모델(보낸 `model`). 빈 문자열이면 기본 모델
    requested_files: list[str] = Field(default_factory=list)
    raw_response: str = ""
    error: str = ""                                # 스크립트가 없는 이유. 해석할 수 없는 답, 전송 실패, 거절 등
    # LLM의 <blocked> 답. 프로젝트 안에서 고칠 수 없는 이유다. 편집도 스크립트도 없다.
    # 루프는 이 답을 declined로 분류한다. script.max_declines_per_case를 넘으면 needs_owner로 차단한다
    blocked_reason: str = ""


FixAnswer = ScriptProposal                      # ScriptProposal의 다른 이름


# ---------------------------------------------------------------------------
# 의존성 버전 정책.
# 같은 메이저 안에서 jakarta와 JDK 21을 지원하는 버전을 고른다. 메이저 업그레이드와 라이브러리 교체는
# 차단하지 않고 OperatorCheck로 인수인계에 남긴다.
# LLM에는 정책과 저장소 버전(sbmig.build.repo_versions)을 주고, 수정 뒤 pom diff를 다시 읽는다(sbmig.build.depchanges)
# ---------------------------------------------------------------------------
# 변경 종류. 운영자 확인 대상은 major와 replaced다
# - major: 메이저 번호가 바뀌었다(2.4.2 -> 3.2.1)
# - minor: 그 밖의 버전 변경. classifier나 속성 변경도 들어간다
# - replaced: 같은 pom에서 라이브러리 하나를 빼고 대신할 것을 넣었다. artifactId가 같거나 한쪽이 다른 쪽을 포함한다
# - added / removed: 나머지 추가와 삭제
DependencyChangeKind = Literal["major", "minor", "added", "removed", "replaced"]
PomSection = Literal["dependencies", "dependencyManagement", "plugins", "pluginManagement", "parent"]


class DependencyChange(BaseModel):
    """pom diff에서 읽은 좌표 변경 한 건을 담는 데이터 클래스.
    시도와 구조 검토에 붙고, major / replaced 변경은 OperatorCheck가 된다.

    Built by sbmig.build.depchanges.dependency_changes."""
    path: str                                      # pom의 루트 기준 경로
    key: str                                       # 바뀌기 전 groupId:artifactId. added면 새 좌표
    before: Optional[str] = None                   # 바뀌기 전 버전. 없거나 부모가 관리하면 None
    after: Optional[str] = None                    # 바뀐 뒤 버전. replaced면 새 라이브러리의 버전
    kind: DependencyChangeKind
    replaced_by: Optional[str] = None              # replaced일 때 새 groupId:artifactId
    section: PomSection = "dependencies"
    # libraries.yml의 allow_major나 version 항목이 허용한 변경인지. 허용돼도 운영자 목록에는 표시와 함께 남는다
    approved: bool = False
    note: str = ""                                 # 그 항목의 메모나 저장소가 대신 내준 버전

    @property
    def needs_operator(self) -> bool:
        """운영자가 실행 시점에 확인해야 하는 변경(메이저 업그레이드나 라이브러리 교체)인지 확인한다."""
        return self.kind in ("major", "replaced")

    @property
    def text(self) -> str:
        """보고서에 쓸 한 줄 표기를 만든다. 라이브러리 교체면 새 좌표까지 쓴다.

        `org.sitemesh:sitemesh 2.4.2 → 3.2.1` / `opensymphony:sitemesh 2.4.2 → org.sitemesh:sitemesh 3.2.1`."""
        a = f"{self.replaced_by} {self.after or '-'}" if self.replaced_by else (self.after or "-")
        return f"{self.key} {self.before or '-'} → {a}"


class VersionCandidate(BaseModel):
    """저장소에 있는 릴리스 하나를 담는 데이터 클래스.
    jakarta 빌드인지, 정식 릴리스 전 버전인지, pom에 기록된 최소 Java 버전을 담는다."""
    version: str
    jakarta: Optional[bool] = None                 # pom이 jakarta.*에 의존하는지. 확인하지 않았으면 None
    prerelease: bool = False                       # milestone, RC, alpha, beta, SNAPSHOT
    java: Optional[int] = None                     # pom에 선언된 최소 Java(maven.compiler.release, target)


class RepositoryVersions(BaseModel):
    """설정된 저장소에 있는 라이브러리 하나의 버전 목록을 담는 데이터 클래스.
    수정 프롬프트의 "Dependency versions"에 넣는다. LLM이 같은 메이저 안에서 버전을 고르게 하려는 정보다.

    Repository = settings.xml mirror / build.repository_url / Maven Central
    (sbmig.build.repo_versions.RepositoryLookup)."""
    key: str                                       # groupId:artifactId
    current: Optional[str] = None                  # 프로젝트가 쓰는 버전. 모르면 None
    major: Optional[int] = None                    # `current`의 메이저
    # 같은 메이저에서 `current`보다 새 릴리스. 오래된 순서이고 프리릴리스는 설정했을 때만 넣는다
    same_major: list[VersionCandidate] = Field(default_factory=list)
    higher: list[VersionCandidate] = Field(default_factory=list)       # 더 높은 메이저마다 가장 새 릴리스
    first_jakarta: Optional[str] = None            # 확인한 릴리스 중 jakarta 빌드인 가장 낮은 버전. 메이저 무관
    source: str = ""                               # metadata URL이나 로컬 저장소 경로. 못 찾았으면 빈 문자열
    error: str = ""                                # 결과가 없는 이유. 타임아웃, 404, 오프라인, URL 없음 등
    # maven-metadata.xml을 조회한 저장소 URL과 응답. 로컬 저장소가 마지막이다.
    # 저널만 보고도 도달하지 못한 Nexus와 "새 릴리스 없음"을 구분할 수 있게 남긴다
    urls_tried: list[str] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=utcnow)


class OperatorCheck(BaseModel):
    """운영자 확인이 필요한 항목을 담는 데이터 클래스.
    최종 결과에 남은 메이저 업그레이드나 라이브러리 교체와 그 확인 가이드다.
    실행이 끝날 때 시도와 구조 검토에서 만든다. 되돌린 시도의 변경은 넣지 않는다.

    Shown as the first Handover block "운영자 확인 필요", report.json `operator_checks`,
    `SBMIG-RESULT ... major=<n>`."""
    kind: Literal["major_upgrade", "library_replaced"] = "major_upgrade"
    change: DependencyChange
    case_id: str = ""                              # 구조 검토가 한 변경이면 빈 문자열
    source: str = ""                               # 변경을 만든 곳. "fix r3 try 2"나 "review"
    commit: str = ""
    guide: str = ""                                # 운영자 확인 사항. LLM의 <verify>나 sbmig가 만든 문장
    guide_source: Literal["llm", "sbmig"] = "llm"
    files: list[str] = Field(default_factory=list) # 같은 시도가 바꾼 파일


# ---------------------------------------------------------------------------
# 오류 목록. 실행 중 생긴 빌드 오류나 pom 수정 한 건이 ErrorEntry 하나다
# 실행 디렉토리의 errors.jsonl에 기록한다. 실행이 끝나면 KB의 errors.jsonl에도 추가한다
# ---------------------------------------------------------------------------


class FixTry(BaseModel):
    """케이스에 대한 시도 한 건을 담는 데이터 클래스.
    ErrorList.begin_try가 만들고, 그 케이스에 속한 열린 오류들이 같은 객체를 공유한다.
    출처, 원인, 해결 방법, 수정 내역, diff, 커밋, 재빌드 결과를 담는다."""
    n: int                                         # 케이스와 실행 안에서의 시도 번호. 1부터
    round: int = 0                                 # 수정 라운드. 0이면 구조 검토
    source: TrySource
    script_version: int = 0                        # 실행했거나 만든 저장 버전. 없으면 0
    analysis: str = ""                             # 원인
    solution: str = ""                             # 해결 방법
    no_script_reason: str = ""
    applied: TryApplied = "none"
    files: list[str] = Field(default_factory=list)
    edits: list[EditNote] = Field(default_factory=list)   # 변경마다 위치, 내용(LLM)과 줄 표기(diff)
    # 이 시도가 pom에서 한 major, replaced 변경. diff에서 다시 읽는다
    major_upgrades: list[DependencyChange] = Field(default_factory=list)
    verify: str = ""                               # 운영자 확인 사항(LLM의 <verify> 또는 sbmig가 만든 가이드)
    verify_source: Literal["", "llm", "sbmig"] = ""   # 가이드를 쓴 쪽. 메이저 업그레이드가 없으면 빈 문자열
    diff: str = ""                                 # 적용한 unified diff. errors.diff_chars까지만 둔다
    commit: str = ""
    # 재빌드 결과. 수정 루프가 다음 라운드 빌드를 보고 채운다
    result: AttemptOutcome = "pending"
    note: str = ""                                 # 실패 문구, 되돌림, 스크립트 오류 등
    llm_calls: int = 0
    seconds: float = 0.0


class ResolvedBy(BaseModel):
    """자기 시도 없이 빌드에서 사라진 오류를 같은 라운드에 고쳤을 시도를 담는 클래스.
    그 오류의 파일이나 모듈 pom을 바꾼 시도다. `sbmig kb reindex`가 이전 기록에도 채운다.

    Set when `ErrorEntry.resolved == "build"`."""
    case_id: str = ""                              # 그 시도의 케이스
    round: int = 0
    n: int = 0
    commit: str = ""
    files: list[str] = Field(default_factory=list) # 그 시도가 바꾼 파일 중 맞은 것. 항목의 파일이나 위쪽 pom
    match: Literal["file", "pom", "reindex"] = "file"   # 맞춘 방법. reindex는 errors.jsonl에서 나중에 채운 것
    # 한 줄 요약
    # e.g. "7a52b1cea8 r1 try 1 — module/comp-jar/pom.xml +28–36 — jakarta.xml.bind-api 추가"
    text: str = ""


class ErrorEntry(BaseModel):
    """오류 목록의 레코드 하나를 담는 데이터 클래스. 빌드 오류, pom 수정, 구조 검토 메모 중 하나다.
    ErrorList가 만들고 수정 루프가 시도를 붙이며, 보고서, errors.md, KB가 읽는다."""
    id: str                                        # sha1(run_id | case_id | file | line | message)[:12]
    run_id: str = ""
    project: str = ""
    case_id: str = ""                              # KB 케이스 id. sha1(l2)[:10], cases/<case_id>/
    l2: str = ""                                   # L2 서명(signature_generic)
    kind: ErrorKind = ErrorKind.UNKNOWN
    file: Optional[str] = None
    line: Optional[int] = None
    module: Optional[str] = None
    message: str = ""
    raw: str = ""                                  # 원문. 600자까지
    # 오류를 처음 발견한 단계. compile, restructure, review, build, fix r3 등이고 fix r3은 3번째 수정 라운드다
    seen: str = ""
    status: EntryStatus = "open"
    tries: list[FixTry] = Field(default_factory=list)
    # 오류가 해결된 곳. "r3 try 2", "build", "review" 중 하나다.
    # "build"는 자기 시도 없이 사라진 경우다. 해결했을 것으로 보이는 시도를 resolved_by에 남긴다
    resolved: str = ""
    # resolved가 "build"일 때 같은 라운드에서 고쳤을 시도. 같은 파일, 모듈 pom, 루트 순으로 가까운 것이 먼저다
    resolved_by: list[ResolvedBy] = Field(default_factory=list)
    script: str = ""                               # 이 오류를 해결한 스크립트. 예: cases/<case_id>/fix_v2.py
    note: str = ""                                 # pom-verify면 재구성이 한 일, note 항목이면 메모 본문
    at: datetime = Field(default_factory=utcnow)
    # 서명 정보. L2를 만든 규칙 버전과, 규칙이 바뀌었을 때 L2를 다시 계산하는 데 필요한 값이다.
    # 이전 기록에는 비어 있으므로(sig 0) 그때는 raw의 symbol / location 줄에서 다시 읽는다
    sig: int = 0                                   # `l2`를 계산할 때의 SIGNATURE_VERSION
    l1: str = ""                                   # L1 서명(BuildError.signature)
    symbol: str = ""                               # javac "symbol:" 줄. `message`처럼 마스킹한다
    location: str = ""                             # javac "location:" 줄
    artifact: str = ""                             # 받지 못한 의존성이나 pom 오류의 g:a[:v]
    goal: str = ""                                 # 플러그인 goal. "compiler:compile", "war:war" 등
    was: list[str] = Field(default_factory=list)   # 재서명 전의 케이스 id. 오래된 것이 먼저


# 저장된 스크립트의 상태. 재사용할 때는 proven을 먼저 고른다
# - unverified: 이 프로젝트를 고친 답에서 저장했거나 여기서 한 번 실행한 스크립트
# - proven: 다른 프로젝트에서 재사용해 케이스를 해결한 스크립트
# - retired: 사람이 끈 스크립트(`sbmig kb cases retire`)
CaseScriptStatus = Literal["unverified", "proven", "retired"]


class ScriptRun(BaseModel):
    """저장된 스크립트 버전 하나가 한 프로젝트에서 실행된 기록을 담는 데이터 클래스.
    결과와 그 이유를 담아서 왜 보강했는지 사람과 이후 유사도 비교 단계가 알 수 있게 한다.

    `reason` is the try's note: errors left, new errors, the script's exception / timeout / static-check
    refusal."""
    run_id: str = ""
    project: str = ""
    round: int = 0                                 # 그 시도의 수정 라운드
    n: int = 0                                     # 케이스와 실행 안에서의 시도 번호
    result: str = ""                               # 평가한 AttemptOutcome
    reason: str = ""                               # 결과의 이유(시도 메모). 그냥 해결했으면 빈 문자열
    # 이 실행의 이관 범위(full / jdk-jakarta). 빈 값은 범위를 기록하기 전의 실행이다. full로 본다
    scope: str = ""
    errors_before: Optional[int] = None            # 실행 전 케이스 오류 수
    errors_left: Optional[int] = None              # 재빌드 뒤 남은 오류 수
    errors_new: Optional[int] = None               # 바꾼 파일에 새로 생긴 오류 수
    files: list[str] = Field(default_factory=list) # 이 실행이 바꾼 파일
    at: datetime = Field(default_factory=utcnow)


class CaseScript(BaseModel):
    """case.json 안에 저장된 스크립트 버전 하나(fix_v<N>.py)의 정보를 담는 데이터 클래스.
    상태, 성공 / 실패 수, 어느 버전에서 파생됐는지, 실행 이력을 담는다.
    CaseStore가 관리하고, CaseStore.best가 재사용할 버전을 고를 때 읽는다."""
    version: int
    file: str                                      # 케이스 폴더 기준 fix_v<N>.py
    status: CaseScriptStatus = "unverified"
    source: TrySource = "llm-new"
    created_by: str = ""                           # 만든 실행의 run_id
    project: str = ""
    successes: int = 0
    failures: int = 0
    projects: list[str] = Field(default_factory=list)    # 케이스를 해결한 프로젝트. 중복 없음
    last_result: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    # 버전 계보와 실행 이력
    parent: Optional[int] = None                   # 보강한 원래 버전. 새로 쓴 스크립트면 None
    # 이 버전을 만든 이유. 부모 버전이 실패한 내용이고 새 스크립트면 빈 문자열
    reason: str = ""
    last_failure: str = ""                         # 가장 최근 실패한 실행의 이유. 전체는 runs에 있다
    runs: list[ScriptRun] = Field(default_factory=list)   # 최신이 마지막. errors.script_runs까지만 둔다
    # ok로 끝난 직후 다시 실행했을 때 또 파일을 바꾼 스크립트(비멱등). 기록은 남기지만 재사용 대상에서는 뺀다
    non_idempotent: bool = False
    # 다른 케이스와 서로의 수정을 되돌린 시도에 쓰인 버전. LLM 없이 혼자 재사용하지 않는다.
    # 어느 범위의 실행이든 표시한다. 같은 범위에서 재사용이 아닌 성공이 기록되거나 `sbmig kb cases activate`하면 지운다
    round_trip: bool = False
    # 이 스크립트를 만든 실행의 이관 범위(full / jdk-jakarta). 빈 값은 범위 기록 전의 스크립트다. full로 본다.
    # 재사용할 버전을 고를 때 같은 범위의 것을 먼저 쓴다. 다른 범위에서 되돌려진 것은 이 버전의 실패로 세지 않는다
    scope: str = ""


class CaseInfo(BaseModel):
    """KB 케이스 하나(`<kb>/cases/<case_id>/case.json`)를 담는 데이터 클래스.
    L2 서명, 이름, 프로젝트, 저장된 스크립트, 마지막 원인과 해결 방법을 담는다.
    CaseStore가 기록하고 KB 조회, cases.md, 플릿 요약이 읽는다.
    사람이 설정하는 `pin`(block)은 이 모델에 없는 별도 키다."""
    case_id: str
    l2: str = ""
    name: str = ""                                 # 케이스 이름. 예: "package jakarta.xml.bind does not exist"
    kind: ErrorKind = ErrorKind.UNKNOWN
    projects: list[str] = Field(default_factory=list)
    seen: int = 0                                  # 모든 실행에서 나온 항목 수
    resolved: int = 0
    scripts: list[CaseScript] = Field(default_factory=list)
    last_analysis: str = ""
    last_solution: str = ""
    updated_at: datetime = Field(default_factory=utcnow)
    # `l2`를 만든 서명 규칙 버전(0은 버전 도입 전)과, 재서명으로 이 케이스에 합쳐진 이전 케이스 ID 목록.
    # `sbmig kb cases show <이전 id>`도 aliases로 이 케이스를 찾는다
    sig: int = 0
    aliases: list[str] = Field(default_factory=list)
    # case.json에는 모델 밖의 키도 있다. "pin"("block[:<reason>]", 시도하지 말라는 사람의 지시)과 "last_result"다.
    # 이 키는 CaseStore.extra가 따로 보관한다


class ReviewIssue(BaseModel):
    """구조 검토가 찾은 문제 하나를 담는 데이터 클래스.
    critical은 바로 고치고, warning은 보고서와 오류 목록 메모에만 남긴다."""
    severity: Literal["critical", "warning"] = "warning"
    file: str = ""                                 # pom이나 모듈 경로. 빈 문자열이면 프로젝트 전체
    problem: str = ""
    fix_hint: str = ""
    # 검토 자료에 없는 버전, 속성, 좌표를 지적한 문제다. 적용하지 않고 보고서에만 남기며 인수인계에는 넣지 않는다.
    # 보고서에는 "(sbmig check) not in the material" 표시가 붙는다
    unsupported: bool = False


class StructureReview(BaseModel):
    """구조 검토 결과를 담는 데이터 클래스. 재구성 뒤 첫 빌드 전에 LLM이 pom, 의존성, 모듈을 회사 규칙으로
    검토한 결과, 문제 목록, critical 수정과 그 커밋을 담는다.
    보고서의 'Structure review'에 나오고, 오류 목록에는 review 레코드로 남는다.

    Built by sbmig.review; RunReport.structure_review."""
    id: str = ""                                   # "<run_id>:review"
    schema_version: int = 3
    at: datetime = Field(default_factory=utcnow)
    run_id: str = ""
    project: str = ""
    case_type: CaseType = CaseType.UNKNOWN
    model: str = ""
    # skipped는 review.enabled가 꺼졌거나 dry run이거나 LLM이 없는 경우,
    # failed는 쓸 수 있는 답이 없는 경우다. failed여도 빌드는 그대로 진행한다
    verdict: Literal["ok", "issues", "skipped", "failed"] = "skipped"
    issues: list[ReviewIssue] = Field(default_factory=list)
    analysis: str = ""
    script: str = ""                               # critical 문제를 고치는 스크립트. 없으면 빈 문자열
    script_result: Optional[ScriptRunResult] = None
    answer: Optional["ScriptProposal"] = None      # LLM의 원인, 해결, 편집, 스크립트. 편집은 검토가 적용한다
    major_upgrades: list[DependencyChange] = Field(default_factory=list)   # 그 커밋의 major, replaced 변경
    commit: Optional[str] = None                   # 스크립트 변경의 커밋. 되돌릴 수 있다
    reverted: bool = False
    llm_usage: LLMUsage = Field(default_factory=LLMUsage)
    note: str = ""                                 # skipped, failed의 이유나 스크립트가 어떻게 됐는지

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
    advisory_ids: list[str] = Field(default_factory=list)   # 이 시도에 힌트를 준 권고 id
    applied: bool = False
    apply_errors: list[str] = Field(default_factory=list)
    commit: Optional[str] = None
    model: str = ""
    prompt_hash: str = ""
    llm_usage: LLMUsage = Field(default_factory=LLMUsage)
    llm_session_id: Optional[str] = None           # 이 시도의 답을 받은 게이트웨이 세션. 다음 시도가 이어 쓸 수 있다
    llm_session_model: str = ""                    # 그 세션의 모델. 다음 시도는 모델이 같을 때만 세션을 이어 쓴다
    errors_before: int = 0
    errors_after: Optional[int] = None
    cluster_signatures: list[str] = Field(default_factory=list)
    cluster_errors: list[BuildError] = Field(default_factory=list)   # 클러스터의 오류. KB에 기록할 때 쓴다
    cluster_resolved: Optional[bool] = None        # 재빌드 뒤 클러스터의 서명이 모두 사라졌는지
    outcome: AttemptOutcome = "pending"
    analysis: str = ""
    notes: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    # 시도 전 HEAD. `git diff base_commit..commit`이 이 시도가 적용한 정확한 변경이다
    base_commit: Optional[str] = None
    # 재빌드 뒤 바꾼 파일에 새로 생긴 오류. 수정 루프의 평가가 채우고 regressed의 근거가 된다
    introduced_errors: list[BuildError] = Field(default_factory=list)
    # 실행한 스크립트 정보. new는 LLM 신규, reuse는 저장 버전 재사용, augment는 `script_parent`를 보강한 것이다
    script_mode: Optional[ScriptMode] = None
    script_case_id: str = ""
    script_version: int = 0                        # 실행한 저장 버전. 없거나 저장하지 않았으면 0
    script_parent: Optional[int] = None
    script_code: str = ""
    script_result: Optional[ScriptRunResult] = None
    # 오류 목록에 기록할 시도 정보
    source: Optional[TrySource] = None             # 시도의 출처(TrySource)
    solution: str = ""                             # 해결 방법(LLM의 <solution>)
    no_script_reason: str = ""
    edits_applied: int = 0                         # 적용한 직접 편집 수. 스크립트를 실행했으면 0
    # 이 시도가 pom에서 한 메이저 업그레이드, 라이브러리 교체와 그 운영자 가이드
    major_upgrades: list[DependencyChange] = Field(default_factory=list)
    verify: str = ""                               # 운영자 확인 사항. 답의 <verify>나 sbmig가 만든 문장
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
# KB
# ---------------------------------------------------------------------------
# 이전 KB의 학습용 수정 라벨. 지금 코드는 쓰지 않고 이전 기록을 읽을 때만 쓴다
# 각 줄 끝의 이름은 그 라벨에 맞는 OpenRewrite 레시피다
FixLabel = Literal[
    "import-rename",          # import 줄만 바뀌었다. ChangeType
    "type-rename",            # 코드의 타입 참조 이름이 바뀌었다. ChangeType
    "package-rename",         # 패키지 접두어 전체가 옮겨졌다. ChangePackage
    "annotation-replace",     # 다른 어노테이션으로 바뀌었다. 1:1이면 ChangeType
    "method-rename",          # 받는 쪽과 인자는 같고 메서드 이름만 바뀌었다. ChangeMethodName
    "method-signature-adapt", # 바뀐 시그니처, 빌더, 람다 DSL에 맞춰 호출부를 고쳤다
    "override-signature",     # 새 상위 타입에 맞춰 오버라이드 시그니처를 고쳤다
    "dependency-add",         # pom에 <dependency>를 추가했다. AddDependency
    "dependency-replace",     # pom의 g:a를 바꿨다. ChangeDependency
    "dependency-remove",      # pom에서 <dependency>를 지웠다. RemoveDependency
    "dependency-version",     # pom의 의존성 버전을 올렸다. UpgradeDependencyVersion
    "plugin-version",         # pom의 플러그인 버전을 올렸다. UpgradePluginVersion
    "pom-property",           # pom의 <properties> 값을 바꾸거나 넣거나 지웠다. ChangePropertyValue
    "spring-property-rename", # application.properties/yml 키 이름을 바꿨다. ChangeSpringPropertyKey
    "config-value",           # 키 이름이 아닌 설정 값을 바꿨다
    "remove-code",            # 쓰지 않는 코드나 어노테이션을 지웠다
    "add-code",               # 메서드, 필드, 클래스를 추가했다
    "generic-rewrite",        # 그 밖의 로직 변경
]


class KBMatch(BaseModel):
    """클러스터의 케이스에 대해 KB가 가진 정보(CaseHit 목록과 프롬프트 힌트 줄)를 담는 데이터 클래스.
    수정 루프가 라운드마다 받아서 재사용, 보강, 신규 중 하나를 고른다.

    Returned by `FileKnowledgeBase.match`."""
    hints: list[str] = Field(default_factory=list)               # 스크립트 프롬프트에 넣을 짧은 힌트
    case_hits: list[Any] = Field(default_factory=list)           # CaseHit 목록


# ---------------------------------------------------------------------------
# 이전 KB의 오류 사례 기록. 지금은 ErrorEntry가 대신한다. 아래 모델은 채우지 않는다
# 이전 KB와 report.json을 읽기 위해 남겨 둔 형식이다
# 파일은 kb/errors.jsonl, kb/fix_attempts.jsonl, kb/error_hints.json, <run dir>/error_cases.jsonl이다
# ---------------------------------------------------------------------------
# 변경을 만든 쪽. script는 LLM 신규, script_reuse는 저장 버전 재사용, script_augment는 이전 버전 보강이다
# handler, recipe, kb_patch, plan_step은 이전 흐름의 값이다. 스크립트의 resolver_id는 "script:<case_id>:v<N>"이다
FixMethod = Literal["handler", "recipe", "kb_patch", "llm", "plan_step", "none",
                    "script", "script_reuse", "script_augment"]
# 변경이 텍스트로 어떻게 보이는지(sbmig.kb.changes.classify_diff). 누가 만들었는지와는 상관없다
# - text_replace: 바뀐 줄이 모두 이전 줄에서 토큰만 바꾼 것이다. import 교체 포함
# - code_edit: 줄을 넣거나 지우거나 구조를 바꿨다. file_*는 파일 단위 작업이다
# - mixed: 한 변경에 text_replace와 code_edit가 섞였다. none은 적용한 것이 없다
ChangeKind = Literal["text_replace", "code_edit", "file_create", "file_delete", "file_rename", "mixed", "none"]
# 이전 오류 기록의 상태
# - resolved: 그 오류를 겨냥했거나 그 파일을 바꾼 시도 뒤, 그 단계까지 간 빌드에서 사라졌다
# - resolved_indirectly: 사라졌지만 겨냥한 시도가 없다(pom 수정, 다른 파일 수정, 계획 단계)
# - blocked: 남아 있고 BlockedItem에 들어 있다. unresolved는 실행이 끝날 때까지 남았다
# - not_visible: 마지막 빌드가 그 단계 전에 멈춰서 보이지 않았을 뿐이다. 상태를 모른다
ErrorCaseStatus = Literal["resolved", "resolved_indirectly", "blocked", "unresolved", "not_visible"]


class ReplacePair(BaseModel):
    """이전 기록을 읽기 위한 데이터 클래스. 텍스트 치환에서 old가 new로 count번 바뀐 것을 담는다."""
    old: str
    new: str
    count: int = 1
    files: list[str] = Field(default_factory=list)            # 루트 기준 경로


class ChangeClass(BaseModel):
    """이전 기록을 읽기 위한 데이터 클래스. 변경이 텍스트로 어떻게 보이는지의 분류를 담는다."""
    change_kind: ChangeKind = "none"
    replace_pairs: list[ReplacePair] = Field(default_factory=list)
    file_types: list[str] = Field(default_factory=list)       # java, pom, xml, properties, yaml, jsp, other
    files: list[str] = Field(default_factory=list)            # diff가 바꾼 파일의 루트 기준 경로
    lines_added: int = 0
    lines_removed: int = 0


# 이전 기록용 FixAttemptRecord / ErrorCaseRecord / StructureReview의 스키마 번호
KB_RECORD_SCHEMA = 3      # 3부터 script_* 필드와 script 계열 FixMethod가 있다. 1이나 없음, 2는 그 전 형식


class FixAttemptRecord(BaseModel):
    """이전 기록을 읽기 위한 데이터 클래스.
    kb/fix_attempts.jsonl 한 줄이 시도 한 건이며, 지금은 ErrorEntry.tries가 대신한다."""
    id: str                                        # "<run_id>:<attempt id>"
    schema_version: int = KB_RECORD_SCHEMA
    agent_version: str = ""                        # 기록한 sbmig 버전(sbmig.__version__)
    at: datetime = Field(default_factory=utcnow)
    project: str = ""
    project_fingerprint: str = ""
    run_id: str = ""
    case_type: CaseType = CaseType.UNKNOWN
    phase: Phase = Phase.SPRING
    iteration: int = 0
    attempt_id: str = ""                           # FixAttempt.id
    cluster_id: str = ""
    error_ids: list[str] = Field(default_factory=list)       # 이 시도가 겨냥한 ErrorCaseRecord id
    fix_method: FixMethod = "none"
    resolver_id: str = ""                          # handler id, 쉼표로 이은 레시피 id, KB 케이스 id, 모델 중 하나
    model: str = ""
    outcome: AttemptOutcome = "pending"
    applied: bool = False
    description: str = ""                          # 한 일. handler 설명, LLM 분석, 레시피, 케이스
    failure: str = ""                              # 실패 이유. improved, build_ok면 빈 문자열
    introduced_errors: list[str] = Field(default_factory=list)   # 바꾼 파일에 새로 생긴 오류("file:line message")
    remaining_errors: list[str] = Field(default_factory=list)    # 재빌드 뒤에도 남은 대상 오류
    diff: str = ""                                 # 적용한 unified diff. 적용하지 않았으면 제안의 diff
    diff_truncated: bool = False
    change: ChangeClass = Field(default_factory=ChangeClass)
    fix_labels: list[str] = Field(default_factory=list)          # FixLabel 값(sbmig.kb.labels)
    replacement: str = ""
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    advisory_ids: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    # 수정 스크립트 정보
    script_mode: Optional[ScriptMode] = None
    script_case_id: str = ""
    script_version: int = 0
    script_parent: Optional[int] = None
    script_status: str = ""                        # ScriptRunStatus 값
    script_error: str = ""
    script_log: list[str] = Field(default_factory=list)


class ErrorAttemptRef(BaseModel):
    """이전 기록을 읽기 위한 데이터 클래스.
    오류 이력에 있는 시도 요약 한 줄(ErrorCaseRecord.attempts)을 담는다."""
    attempt_id: str                                # FixAttemptRecord.id
    phase: Phase = Phase.SPRING
    iteration: int = 0
    fix_method: FixMethod = "none"
    resolver_id: str = ""
    outcome: AttemptOutcome = "pending"
    failure: str = ""
    change_kind: ChangeKind = "none"
    script_version: int = 0                        # 시도가 실행한 스크립트 버전. 스크립트가 없으면 0


class ErrorCaseRecord(BaseModel):
    """이전 기록을 읽기 위한 데이터 클래스. kb/errors.jsonl 한 줄이 빌드 오류 한 건이다.
    지금은 ErrorEntry를 쓰므로 RunReport.error_cases는 비어 있다."""
    id: str                                        # sha1(run_id | identity | occurrence)[:16]
    schema_version: int = KB_RECORD_SCHEMA
    agent_version: str = ""
    at: datetime = Field(default_factory=utcnow)
    project: str = ""
    project_fingerprint: str = ""
    run_id: str = ""
    case_type: CaseType = CaseType.UNKNOWN
    # 오류 내용. 경로는 상대 경로로 바꾸고 비밀 값은 마스킹한다. raw는 2000자까지 둔다
    error_kind: ErrorKind = ErrorKind.UNKNOWN
    message: str = ""
    file: Optional[str] = None
    line: Optional[int] = None
    symbol: Optional[str] = None                   # javac "symbol:" 줄
    location: Optional[str] = None
    module: Optional[str] = None
    artifact: str = ""                             # 받지 못한 의존성의 g:a[:v]
    signature: str = ""                            # L1 서명
    signature_generic: str = ""                    # L2 서명
    error_symbol: str = ""                         # 프로젝트 간 비교 키(sbmig.kb.labels.symbol_for_errors)
    raw: str = ""
    # 실행 안에서의 경과. 빌드 라벨은 "<phase>-<n>", "verify-final", "verify-final-2" 형식이다
    first_seen: str = ""
    first_phase: Phase = Phase.SPRING
    last_seen: str = ""
    builds_present: int = 1
    # 결과
    status: ErrorCaseStatus = "unresolved"
    resolved_at: str = ""                          # 오류가 사라진 빌드 라벨
    resolved_phase: Optional[Phase] = None
    fix_method: FixMethod = "none"                 # 해결한 시도나 단계의 방법
    resolver_id: str = ""
    resolved_by: Optional[str] = None              # FixAttemptRecord id. plan_step이면 "step:<step id>"
    # 해결에 들어간 모든 시도를 실행 순서대로 둔다. resolved_by도 이 안에 있다.
    # 적용돼서 프로젝트에 남았고, 이 오류를 겨냥했거나 그 파일을 바꾼 시도다. import를 넣은 앞의 no_change 시도도 들어간다
    resolution_attempts: list[str] = Field(default_factory=list)
    targeted: bool = False                         # 해결한 시도의 클러스터에 이 오류가 있었는지
    description: str = ""                          # 해결한 방법이나 시도한 내용. status에 따라 다르다
    change_kind: ChangeKind = "none"               # 해결한 변경 전체(resolution_attempts를 합친 것)의 종류
    replace_pairs: list[ReplacePair] = Field(default_factory=list)
    fix_labels: list[str] = Field(default_factory=list)
    # 해결 diff 중 이 오류 파일 부분만 뽑은 것. 시도마다 한 구역이고 여러 개면 `# attempt <id>` 머리줄을 단다.
    # 구역마다 kb.error_excerpt_chars까지 둔다
    diff_excerpt: str = ""
    # 실패 쪽 정보
    failure_reason: str = ""                       # 해결하지 못한 이유. 해결했으면 빈 문자열
    problem_errors: list[str] = Field(default_factory=list)  # 마지막 시도를 실패하게 한 오류("file:line message")
    block_reason: str = ""                         # BlockReason 값
    suggestion: str = ""
    attempts: list[ErrorAttemptRef] = Field(default_factory=list)   # 이 오류를 겨냥한 시도. 순서대로
    attempt_count: int = 0
    llm_attempts: int = 0
    notes: list[str] = Field(default_factory=list)
    case_id: str = ""                              # 케이스 id. sha1(signature_generic)[:10]
    # 프로젝트 정보와 바로 앞에 있었던 일(ProjectProfile, 추적기에서 가져온다)
    asis_java: str = ""
    asis_spring_boot: str = ""
    asis_spring: str = ""
    packaging: str = ""
    introduced_by: str = ""                        # 처음 나온 빌드 직전의 "step:<plan step id>"나 시도 id
    # 이 오류를 해결한 케이스 스크립트(해결한 시도에서 가져온다)
    script_case_id: str = ""
    script_version: int = 0


# ---------------------------------------------------------------------------
# 이전 케이스 카탈로그(kb/catalog/…yml). 지금은 `cases/<case_id>/case.json`을 쓴다
# 아직 쓰는 것은 KB 조회 결과인 CaseAction, CaseHit와 그 Literal(CaseStatus, CaseActionKind)뿐이다
# 파일은 kb/catalog/<kind>/<class>/<symbol>--<id>.yml이고 L2 하나가 케이스 하나다.
# `human` 블록은 덮어쓰지 않고 나머지 필드는 다시 계산한다
# ---------------------------------------------------------------------------
CaseStatus = Literal["observed", "candidate", "active", "rejected"]
# script는 케이스의 가장 좋은 저장 스크립트를 실행한다. block은 시도하지 않는다. hint는 힌트만 프롬프트에 넣는다
CaseActionKind = Literal["hint", "block", "script"]


class CaseVariant(BaseModel):
    """이전 카탈로그를 읽기 위한 데이터 클래스. 케이스의 변형 하나(소유 클래스 / L1 서명)를 담는다."""
    owner: str = ""                                # 심볼을 가진 클래스 FQCN. 모르면 빈 문자열
    signature: str = ""                            # L1 서명
    seen: int = 0
    projects: int = 0


class CaseFix(BaseModel):
    """이전 카탈로그를 읽기 위한 데이터 클래스. 케이스를 고친 방법 하나를 담는다.

    Grouped by method + resolver + replace pairs; the latest attempt is the example."""
    method: FixMethod = "none"
    resolver_id: str = ""
    change_kind: ChangeKind = "none"
    replace_pairs: list[ReplacePair] = Field(default_factory=list)
    description: str = ""
    attempt_id: str = ""                           # 같은 방법의 가장 최근 FixAttemptRecord id. diff는 거기 있다
    attempt_ids: list[str] = Field(default_factory=list)     # 해결 시도가 여러 개면 그 목록
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
    """이전 카탈로그를 읽기 위한 데이터 클래스. 한 실행에서 본 케이스의 타임라인 항목(ErrorCaseRecord 하나)을 담는다."""
    at: Optional[datetime] = None
    project: str = ""
    run_id: str = ""
    error_id: str = ""
    file: str = ""
    status: ErrorCaseStatus = "unresolved"
    fix_method: FixMethod = "none"
    attempts: list[str] = Field(default_factory=list)   # "method:outcome" 목록. 실패한 것이 해결보다 먼저
    resolved_by: Optional[str] = None


class CaseAction(BaseModel):
    """KB 조회가 케이스에 대해 정한 행동을 담는 데이터 클래스.
    FileKnowledgeBase.match가 만들고 트리아지가 따른다.
    - script: 저장된 스크립트 버전을 실행한다
    - block: 사람이 차단으로 고정했다. 시도하지 않는다
    - hint: 힌트만 준다"""
    kind: CaseActionKind = "hint"
    block_reason: str = ""                                    # block일 때 BlockReason 값. suggestion과 함께 쓴다
    suggestion: str = ""
    script_version: int = 0                                   # script일 때 실행할 저장 버전


class CaseEvidence(BaseModel):
    """이전 카탈로그를 읽기 위한 데이터 클래스. 케이스의 근거 집계를 담는다."""
    seen: int = 0                                  # 오류 기록 수
    projects: int = 0
    runs: int = 0
    first_at: Optional[datetime] = None
    last_at: Optional[datetime] = None
    by_status: dict[str, int] = Field(default_factory=dict)
    resolved_by: dict[str, int] = Field(default_factory=dict)  # fix_method별 해결 기록 수
    fixes: list[CaseFix] = Field(default_factory=list)         # 프로젝트 수, 횟수 순 상위 5개
    failures: list[CaseFailure] = Field(default_factory=list)  # 상위 5개
    timeline: list[CaseEvent] = Field(default_factory=list)    # 최근 20개. 오래된 것이 먼저


class CaseHuman(BaseModel):
    """이전 카탈로그를 읽기 위한 데이터 클래스. 사람이 편집하는 부분이라서 자동 재계산이 덮어쓰지 않는다.
    값이 있으면 자동으로 정한 값보다 우선한다."""
    name: Optional[str] = None
    status: Optional[CaseStatus] = None            # 사람이 정한 상태. 예: rejected, active
    action: Optional[CaseAction] = None            # 사람이 고정한 행동
    hints: list[str] = Field(default_factory=list)
    notes: str = ""


class CatalogCase(BaseModel):
    """이전 카탈로그의 케이스 하나를 읽기 위한 데이터 클래스. 지금은 CaseInfo를 쓴다."""
    id: str                                        # sha1(signature_generic)[:10]
    name: str = ""                                 # 자동으로 만든 이름
    error_kind: ErrorKind = ErrorKind.UNKNOWN
    error_class: str = ""                          # L2의 두 번째 부분. cannot_find_symbol 등
    symbol: str = ""                               # L2의 세 번째 부분, 오류 심볼
    signature_generic: str = ""
    variants: list[CaseVariant] = Field(default_factory=list)
    status: CaseStatus = "observed"                # 자동으로 정한 상태
    action: CaseAction = Field(default_factory=CaseAction)   # 자동으로 정한 행동
    evidence: CaseEvidence = Field(default_factory=CaseEvidence)
    hints: list[str] = Field(default_factory=list)            # 자동 힌트. 가장 좋은 수정, 가장 많은 실패
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
    케이스, 상태(active는 이전에 성공, candidate는 미검증), 행동, 맞은 오류, 힌트를 담는다.
    트리아지와 수정 루프가 재사용을 시도할 때 쓴다.

    Status / action are the effective ones (human block already applied)."""
    case_id: str
    name: str = ""
    status: CaseStatus = "observed"
    action: CaseAction = Field(default_factory=CaseAction)
    errors: list[BuildError] = Field(default_factory=list)
    variant_owner: str = ""
    hints: list[str] = Field(default_factory=list)
    pinned: bool = False                           # `action`이 사람이 고정한 행동인지. 트리아지가 그대로 따른다


# ---------------------------------------------------------------------------
# 플릿: `sbmig fleet`이 여러 프로젝트를 차례로 실행한다
# ---------------------------------------------------------------------------
FleetStatus = Literal["pending", "running", "success", "incomplete", "aborted", "skipped"]


class FleetProjectResult(BaseModel):
    """`sbmig fleet`에서 프로젝트 하나의 결과를 담는 데이터 클래스. fleet/state.json과 플릿 요약에 쓴다."""
    name: str
    input: str                                     # 매니페스트에 있는 경로 그대로
    status: FleetStatus = "pending"
    run_id: Optional[str] = None
    out_dir: Optional[str] = None
    case_type: CaseType = CaseType.UNKNOWN
    exit_code: Optional[int] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    duration_s: float = 0.0
    llm_calls: int = 0                              # 프로젝트의 모든 시도를 합한 호출 수
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_cost: Optional[float] = None               # RunReport.llm_cost.total. 가격이 없으면 None
    blocked: int = 0
    blocked_reasons: list[str] = Field(default_factory=list)   # "<사유>:<g:a>" 형식
    unresolved: int = 0
    flags: list[str] = Field(default_factory=list)
    advisories: list[str] = Field(default_factory=list)           # 라이브러리 권고. "<advisory_id>:<outcome>"
    error_counts: dict[str, int] = Field(default_factory=dict)    # ErrorEntry 상태별 개수
    carried_over: int = 0                                         # as-is 버전으로 고정한 의존성 수
    error: Optional[str] = None
    attempt: int = 1


class FleetState(BaseModel):
    """fleet/state.json에 저장하는 플릿 진행 상태 클래스.
    프로젝트가 끝날 때마다 파일 전체를 원자적으로 다시 쓰므로 중단된 뒤에도 이어서 실행할 수 있다."""
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
# 라이브러리 권고(knowledge/libraries/advisories.yml, sbmig.advisory)와 인수인계 요약
# ---------------------------------------------------------------------------
# 500개 프로젝트가 모두 완벽할 수는 없다. 그래서 block 정책은 일부러 두지 않는다.
# 권고는 guide뿐이다. 수정 프롬프트 힌트와 보고서 인수인계 섹션으로 전달한다
AdvisoryPolicy = Literal["guide"]              # 권고는 LLM 힌트와 인수인계로만 쓴다
# 권고 상태
# - eol: 개발이 끝난 라이브러리(springfox, hystrix, sleuth, ribbon, zuul 등)
# - unmanaged: 살아 있지만 대상 부모나 Boot BOM이 더는 버전을 관리하지 않는다
# - incompatible: 컴파일은 되지만 JDK 21, Boot 4에서 실행 시점에 동작하지 않는다
# - superseded: 동작은 하지만 회사나 상위 프로젝트의 표준 대체품이 있다(오래된 릴리스 트레인, httpclient 4 등)
AdvisoryStatus = Literal["eol", "unmanaged", "incompatible", "superseded"]
# 실행 뒤 확인한 결과
# - replaced: 라이브러리가 더 보이지 않거나 버전이 resolved_min_version에 이르렀다
# - still-present: 아직 선언돼 있거나 쓰이고 있어서 소유 팀에 후속 작업을 넘긴다
# - unknown: 확인할 수 없다(dry run, 분석 전 중단, 재검사 실패)
AdvisoryOutcome = Literal["replaced", "still-present", "unknown"]


class LibraryAdvisory(BaseModel):
    """knowledge/libraries/advisories.yml의 권고 항목 하나를 담는 데이터 클래스.
    회사가 추가한 항목은 advisories.extra_files에서 읽어 id 기준으로 합친다."""
    id: str                                        # 권고 id. 예: "springfox"
    library: Optional[str] = None                  # 이 항목을 찾는 LibraryUsage.name(sbmig.analyze.libraries 규칙)
    artifacts: list[str] = Field(default_factory=list)   # 추가로 볼 pom 패턴 "group:artifact-glob". 직접, 관리 모두
    status: AdvisoryStatus = "eol"
    title: str                                     # 제목. 예: "Springfox (Swagger 2) is discontinued"
    summary: str = ""                              # 보고서에 넣을 한 문단
    replacement: Optional[str] = None              # 대체 좌표나 자유 문장
    replacement_version: Optional[str] = None      # 대체 버전. 예: "3.x (Boot 4)"
    followup: str = ""                             # 소유 팀이 이후 할 일(실행 시점 작업) 문장
    docs: list[str] = Field(default_factory=list)
    applies_to: list[CaseType] = Field(default_factory=list)   # 적용할 프로젝트 종류. 비어 있으면 전부
    # 찾은 버전이 이보다 낮을 때만 적용한다. 릴리스 트레인 이름처럼 읽을 수 없는 버전은 낮은 것으로 본다
    applies_when_version_below: Optional[str] = None
    # 라이브러리가 남아 있어도 이 버전 이상이면 outcome을 replaced로 본다(릴리스 트레인 올림)
    resolved_min_version: Optional[str] = None
    notes: Optional[str] = None                    # knowledge/notes/*.md 경로
    # 한국어 보고서에 쓰는 문장. 비어 있으면 영어 문장을 그대로 보여 준다
    title_ko: str = ""
    summary_ko: str = ""
    followup_ko: str = ""
    replacement_ko: str = ""                       # `replacement`가 좌표가 아닌 자유 문장일 때만 쓴다


class AdvisoryFinding(BaseModel):
    """이 프로젝트에 해당하는 권고 하나를 담는 데이터 클래스.
    분석 뒤에 만들고 마무리 단계에서 outcome(replaced / still-present)을 확인한다.
    수정 프롬프트 힌트, 인수인계 섹션, report.json의 `advisories`에 쓴다.

    The planner may enrich it (recipes); also serialized in the ledger."""
    advisory_id: str
    library: str                                   # 라이브러리 이름. 아티팩트로만 찾았으면 권고 id
    status: AdvisoryStatus
    title: str
    summary: str = ""
    policy: AdvisoryPolicy = "guide"               # 항상 guide다. report.json을 읽는 쪽을 위해 남긴다
    detected_version: Optional[str] = None
    evidence: list[str] = Field(default_factory=list)     # LibraryUsage의 근거 문자열
    artifacts: list[str] = Field(default_factory=list)    # as-is pom에서 찾은 "g:a"
    outcome: AdvisoryOutcome = "unknown"
    replacement: Optional[str] = None
    replacement_version: Optional[str] = None
    followup: str = ""
    docs: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    # LLM이 이 라이브러리 때문에 고친 파일. advisory_ids에 이 id가 있는 성공한 시도에서 모은다
    llm_fixed_files: list[str] = Field(default_factory=list)
    llm_fix_notes: list[str] = Field(default_factory=list)     # 그런 시도마다 분석 한 줄. 인수인계에 쓴다
    # 한국어 보고서에 쓰는 문장(LibraryAdvisory의 같은 이름 필드에서 옮긴다). 비어 있으면 영어 문장을 쓴다
    title_ko: str = ""
    summary_ko: str = ""
    followup_ko: str = ""
    replacement_ko: str = ""

    @property
    def replaced(self) -> bool:
        """실행 뒤 그 라이브러리가 더 이상 보이지 않는지(outcome == replaced) 확인한다."""
        return self.outcome == "replaced"


class MergeConflict(BaseModel):
    """`layout.multi_module_policy: merge`에서 여러 as-is 모듈에 같은 경로로 있던 파일 하나를 담는 데이터 클래스.
    루트 `<modules>` 순서에서 첫 모듈의 파일을 comp-jar에 남기고, 나머지는 실행
    디렉토리에 저장해 인수인계의 'Merge conflicts'에 보여 준다.

    The root `src/` counts as a module ("<root>")."""
    path: str                                      # comp-jar 기준 경로. 예: "src/main/resources/a.properties"
    kept_from: str                                 # 남긴 모듈. 루트 src/면 "<root>"
    dropped_from: list[str] = Field(default_factory=list)   # 복사본을 뺀 모듈
    identical: bool = False                        # 모든 복사본의 바이트가 같았는지. 같으면 잃은 것이 없다
    # 뺀 복사본의 실행 디렉토리 기준 경로("merge-conflicts/<module>/src/..."). identical이면 비어 있다
    saved_copies: list[str] = Field(default_factory=list)
    # 줄 목록 파일(META-INF/services/*, spring.factories, META-INF/spring/*.imports)이면 모든 복사본의 줄을 합친다.
    # 빠지는 것은 없고 saved_copies에 원본이 남는다
    union: bool = False


class VersionChange(BaseModel):
    """인수인계 '바뀐 버전' 표의 한 줄을 담는 데이터 클래스.
    as-is pom과 최종 pom의 버전을 g:a 기준으로 비교한다.
    carried_over는 새 부모나 BOM이 관리하지 않아서 as-is 버전으로 고정한 것이다. 소유 팀이 확인해야 한다."""
    kind: Literal["parent", "dependency", "bom", "plugin", "property"] = "dependency"
    key: str                                       # "g:a". property면 속성 이름
    before: Optional[str] = None                   # as-is 버전. 선언값, 없으면 as-is effective pom이 관리한 값
    after: Optional[str] = None                    # 최종 선언 버전. 새 부모나 BOM이 관리하면 None
    before_source: Literal["declared", "managed", "absent"] = "declared"
    after_source: Literal["declared", "managed", "removed"] = "declared"
    # 바뀐 이유. "parent swap", "recipe", "optional bump", "mandatory", "carried over (no longer managed)",
    # "advisory:<id>", 빈 문자열 중 하나
    reason: str = ""
    carried_over: bool = False                     # as-is effective 버전으로 고정했는지. 소유 팀이 확인하거나 바꿔야 한다
    pom: str = "pom.xml"                           # 이 항목을 선언한 최종 pom의 상대 경로
    # 대상 Boot BOM에서 가져와 고정한 버전이 as-is 버전과 다른 경우. 소유 팀이 알아야 하는 항목이다
    boot_bom_pin: bool = False


class HandoverSummary(BaseModel):
    """인수인계 요약을 담는 데이터 클래스. sbmig가 어디까지 바꿨고 이후 무엇이 소유 팀의 일인지를 담는다.
    부모, Java, Boot 전후 버전, 버전 변경, 추가와 제거, 팀이 할 일, 병합 충돌, 설정할 키가 들어가며
    report.handover.build_handover가 만든다.

    Inputs: profile.poms (as-is), the as-is effective dependencyManagement and the final poms."""
    parent_before: Optional[str] = None            # "g:a:v"
    parent_after: Optional[str] = None
    java_before: Optional[str] = None
    java_after: Optional[str] = None
    spring_boot_before: Optional[str] = None
    spring_boot_after: Optional[str] = None
    changes: list[VersionChange] = Field(default_factory=list)   # 버전이 바뀐 항목. 부모, BOM 포함, key 순
    added: list[str] = Field(default_factory=list)               # 최종 pom에 새로 생긴 "g:a[:v]"(필수, 대체품)
    removed: list[str] = Field(default_factory=list)             # 최종 pom에서 사라진 "g:a[:v]"
    carried_over: list[VersionChange] = Field(default_factory=list)   # changes 중 carried_over인 것
    # 소유 팀이 할 일을 순서대로, 중복 없이 둔다. 바꾸지 못한 권고, 바꿨지만 확인할 것, as-is 버전 고정,
    # 차단과 미해결 안내, 수동 후속 작업 순이다
    from_here: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)               # 참고 문장. effective pom을 못 읽은 경우 등
    # RunReport.merge_conflicts 중 복사본이 달랐던 항목마다 한 줄. 같았던 것은 한 줄로 요약한다
    merge_conflicts: list[str] = Field(default_factory=list)
    properties_to_set: list[str] = Field(default_factory=list)   # 외부 설정 키마다 한 줄
    properties_guide: list[str] = Field(default_factory=list)    # 값을 넣는 방법(knowledge/config/externalized.yml)


# ---------------------------------------------------------------------------
# 보고서
# ---------------------------------------------------------------------------
class CodeChange(BaseModel):
    """자동 변환 뒤에 sbmig가 고친 파일 하나를 담는 데이터 클래스. 구조 검토와 수정 단계가 남긴 최종 변경이다.
    라운드별 시도가 아니라 변환 직후 상태와 최종 상태를 비교한 결과다. 한 파일을 여러 번 고쳐도 한 건이다.
    보고서의 "코드 수정 내역"이 읽는다.

    Built at finalize by `sbmig.report.changes.collect_code_changes`."""
    path: str                                      # 최종 루트 기준 경로
    status: Literal["modified", "added", "deleted"] = "modified"
    added: int = 0                                 # 최종 diff에서 추가된 줄 수. removed는 지운 줄 수
    removed: int = 0
    # 어디를 어떻게 바꿨는지. 남은 시도에서 LLM이 이 파일에 쓴 EditNote를 한 문장씩 처음 나온 순서로 둔다
    notes: list[str] = Field(default_factory=list)
    # 왜 바꿨는지. 이 파일을 바꾼 채로 남은 시도의 케이스마다 원인(FixTry.analysis) 하나씩
    reasons: list[str] = Field(default_factory=list)
    # 이 파일의 변환 직후부터 최종까지 unified diff. 크기 제한 초과, 바이너리, 읽기 실패면 빈 문자열
    diff: str = ""
    truncated: bool = False                        # `diff`가 줄 수 제한에서 잘렸는지


class JarCoordinate(BaseModel):
    """Ant 프로젝트 lib의 jar 하나와 찾아낸 Maven 좌표를 담는 데이터 클래스.
    접수 단계의 pom 생성이 만들고 보고서의 Handover가 읽는다.
    ``source``는 좌표를 어디서 찾았는지, ``used``는 소스가 이 jar의 패키지를 import하는지 여부다(None은 확인하지 않음)."""
    path: str                                      # jar의 루트 기준 경로
    sha1: str = ""
    group_id: str = ""
    artifact_id: str = ""
    version: str = ""
    source: Literal["pom_properties", "name_table", "sha1_lookup", "manifest", "unknown"] = "unknown"
    scope: str = "compile"                         # 컨테이너가 주는 servlet, jsp, el API면 provided
    used: Optional[bool] = None                    # 소스가 이 jar의 패키지를 import하는지. 확인하지 않았으면 None
    packages: list[str] = Field(default_factory=list)   # jar 안의 최상위 패키지. 개수 제한이 있는 표시용 목록
    note: str = ""
    # 클래스가 있는 모든 패키지. 제한이 없고, `bundled_jars.providers`가 패키지를 가진 jar를 찾을 때 쓴다
    provides: list[str] = Field(default_factory=list)

    @property
    def key(self) -> str:
        """``g:a``를 반환한다. 좌표가 없으면 빈 문자열을 반환한다."""
        return f"{self.group_id}:{self.artifact_id}" if self.group_id and self.artifact_id else ""


class PomSynthesis(BaseModel):
    """접수 단계에서 Ant 프로젝트용으로 생성한 pom.xml의 기록을 담는 데이터 클래스.
    좌표, build.xml에서 읽은 레이아웃, jar 좌표 표, 좌표를 모르는 jar, 옮기지 못한 타깃을 담는다.
    오케스트레이터가 ``RunReport.pom_synthesis``에 넣고 보고서가 읽는다."""
    ok: bool
    pom_path: str = "pom.xml"
    build_file: str = "build.xml"
    group_id: str = ""
    artifact_id: str = ""
    version: str = ""
    packaging: str = "jar"                         # WEB-INF가 있는 웹 루트나 <war> 태스크가 있으면 war
    source_dirs: list[str] = Field(default_factory=list)
    web_root: Optional[str] = None                 # WEB-INF가 있는 디렉토리(WebContent, web, WebRoot 등)
    resource_dirs: list[str] = Field(default_factory=list)
    lib_dirs: list[str] = Field(default_factory=list)
    java_source: str = "1.8"
    encoding: str = "UTF-8"
    jars: list[JarCoordinate] = Field(default_factory=list)
    unknown_jars: list[str] = Field(default_factory=list)      # 좌표를 모르는데 쓰이는(또는 확인 안 한) jar. system scope
    dropped_jars: list[str] = Field(default_factory=list)      # 좌표를 모르고 쓰이지도 않아 선언하지 않은 jar
    build_xml_notes: list[str] = Field(default_factory=list)   # 옮기지 못한 타깃, 태스크(코드 생성, 복사 등)
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
# remediation은 이관 없이 업로드한 프로젝트의 취약점 조치만 한다.
# CLI `--flow`, 웹 `POST /api/runs?flow=`, 화면의 "전체 진행" / "취약점 해결"이 이 값이다
FLOW_FULL = "full"
FLOW_REMEDIATION = "remediation"
RUN_FLOWS = (FLOW_FULL, FLOW_REMEDIATION)


class RunReport(BaseModel):
    """실행 한 번의 최종 보고서(report.json, report.md의 원본)를 담는 데이터 클래스.
    오케스트레이터가 단계마다 채우고, 마무리 단계에서 인수인계, 운영자 확인, 오류 목록을 붙인다.
    `success`는 최종 검증 빌드가 성공했다는 뜻이다. 최종 빌드 없이 끝나는 실행은 없다."""
    pom_synthesis: Optional["PomSynthesis"] = None   # 접수에서 Ant 프로젝트의 pom.xml을 만들었을 때만 채운다
    # 이 실행의 방식(RUN_FLOWS). full은 이관, remediation은 이관 없이 취약점 조치만 한다
    flow: str = "full"
    run_id: str
    project_name: str
    case_type: CaseType
    started_at: datetime
    finished_at: Optional[datetime] = None
    success: bool = False                          # 최종 검증 빌드가 성공했는지
    profile: Optional[ProjectProfile] = None
    plan: Optional[MigrationPlan] = None
    phases: list[PhaseResult] = Field(default_factory=list)
    final_build: Optional[BuildResult] = None
    llm_usage: LLMUsage = Field(default_factory=LLMUsage)
    llm_usage_by_model: list[LLMUsage] = Field(default_factory=list)   # 수정, 에스컬레이션, 레시피 모델별 사용량
    llm_cost: Optional[LLMCost] = None                                  # llm.pricing으로 계산한 비용
    unresolved: list[BuildError] = Field(default_factory=list)
    blocked: list[BlockedItem] = Field(default_factory=list)       # 라이브러리, 인프라, 사람의 조치가 필요해 건너뛴 것
    manual_followups: list[str] = Field(default_factory=list)    # as-is 소유 팀이 할 일(실행 시점 문제)
    advisories: list[AdvisoryFinding] = Field(default_factory=list)   # 라이브러리 권고(EOL, 관리되지 않는 라이브러리)
    handover: Optional[HandoverSummary] = None                   # 바뀐 버전과 소유 팀이 이후 할 일
    merge_conflicts: list[MergeConflict] = Field(default_factory=list)   # merge 정책 재구성에서 생긴 충돌
    properties: Optional[PropertyInventory] = None               # 외부 설정 목록과 기록한 플레이스홀더
    flags: list[str] = Field(default_factory=list)               # 예: "test-compile-skipped"
    javax_api_jars: list[JavaxApiJar] = Field(default_factory=list)   # report.javax_api_inventory 결과
    # 이전 형식의 빌드 오류 기록. 지금은 비어 있다
    error_cases: list[ErrorCaseRecord] = Field(default_factory=list)
    error_case_counts: dict[str, int] = Field(default_factory=dict)   # 상태별 개수
    structure_review: Optional[StructureReview] = None           # 첫 빌드 전 구조 검토 결과
    error_entries: list[ErrorEntry] = Field(default_factory=list)    # 이 실행의 오류 목록. diff는 잘라서 둔다
    # 자동 변환 뒤에 고친 파일들의 최종 변경. Java 파일, pom, 나머지 순서이고 diff는 앞의 일부 파일에만 담는다
    code_changes: list[CodeChange] = Field(default_factory=list)
    code_changes_total: int = 0                    # 변환 뒤 바뀐 파일 전체 수. 목록보다 많을 수 있다
    code_changes_base: str = ""                    # diff의 기준 커밋. 모으지 않았으면 빈 문자열
    # 운영자 확인 필요 항목. 최종 결과에 남은 메이저 업그레이드와 라이브러리 교체, 그 가이드
    operator_checks: list[OperatorCheck] = Field(default_factory=list)
    # 빌드 성공 뒤 이어서 실행한 취약점 조치의 요약(``sbmig.remediations.chain.summary``).
    # 이관의 성공 여부나 종료 코드에는 쓰지 않는다. 보고서의 "취약점 조치" 절과 SBMIG-RESULT의 remediation=만 이 값을 읽는다
    remediation: Optional[dict[str, Any]] = None
    outputs: dict[str, str] = Field(default_factory=dict)        # project_dir, zip, diff, journal 등의 경로
    timings_s: dict[str, float] = Field(default_factory=dict)


# MigrationPlan은 아래에서 정의하는 LLMUsage를 문자열로 참조한다. 그래서 모든 모델을 정의한 뒤에 다시 빌드한다
MigrationPlan.model_rebuild()
