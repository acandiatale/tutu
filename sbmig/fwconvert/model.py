"""프레임워크 전환 모듈들이 주고받는 레코드를 모아 둔 파일.

Contract: docs/FW-CONVERT.md section 8. Plain dataclasses without I/O. A field gets a default when it is added so
that no module breaks. Paths are POSIX strings relative to the root they name (source root or output root)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from sbmig.fwconvert.naming import kebab, package_path

# ---- 프로젝트 이름과 경로 ---------------------------------------------------


@dataclass(frozen=True)
class ModuleNames:
    """생성할 네 모듈의 디렉터리 이름. 아티팩트 이름과 같다."""
    boot: str
    domain: str
    service: str
    store: str


@dataclass(frozen=True)
class ProjectIdentity:
    """생성할 프로젝트의 이름, 도메인, 기본 패키지."""
    project_name: str
    domain: str
    package_base: str
    modules: ModuleNames
    source: str = "file"            # file: 올린 파일 이름에서 만든 이름. override: 사용자가 준 이름


@dataclass(frozen=True)
class Layout:
    """생성할 파일의 패키지와 경로를 알려 준다. 경로는 출력 루트 기준이다."""
    identity: ProjectIdentity

    @property
    def pkg(self) -> str:
        """기본 패키지."""
        return self.identity.package_base

    @property
    def spec_pkg(self) -> str:
        """``{X}Service`` 인터페이스의 패키지."""
        return f"{self.pkg}.domain.spec"

    @property
    def store_pkg(self) -> str:
        """``{X}Store`` 인터페이스의 패키지."""
        return f"{self.pkg}.domain.store"

    @property
    def logic_pkg(self) -> str:
        """``{X}Logic`` 클래스의 패키지."""
        return f"{self.pkg}.domain.logic"

    @property
    def spring_logic_pkg(self) -> str:
        """``{X}SpringLogic`` 클래스의 패키지."""
        return f"{self.pkg}.logic"

    @property
    def rest_pkg(self) -> str:
        """컨트롤러의 패키지."""
        return f"{self.pkg}.rest"

    @property
    def mybatis_store_pkg(self) -> str:
        """``{X}MybatisStore`` 클래스의 패키지."""
        return f"{self.pkg}.store"

    def domain_pkg(self, part: str) -> str:
        """domain 모듈의 하위 패키지(entity, proxy, util 등)를 반환한다."""
        return f"{self.pkg}.domain.{part}"

    def module_of(self, package: str) -> str:
        """패키지가 속한 모듈의 디렉터리 이름을 반환한다."""
        m = self.identity.modules
        if package.startswith(f"{self.pkg}.domain"):
            return m.domain
        if package.startswith(self.mybatis_store_pkg):
            return m.store
        if package.startswith(self.spring_logic_pkg) or package.startswith(self.rest_pkg):
            return m.service
        return m.boot

    def java_path(self, package: str, class_name: str) -> str:
        """클래스 파일의 경로를 반환한다."""
        return f"{self.module_of(package)}/src/main/java/{package_path(package)}/{class_name}.java"

    @property
    def mapper_dir(self) -> str:
        """MyBatis mapper를 두는 폴더."""
        return f"{self.identity.modules.boot}/src/main/resources/mybatis/mapper"

    def mapper_path(self, resource: str) -> str:
        """리소스의 mapper 파일 경로를 반환한다."""
        return f"{self.mapper_dir}/{kebab(resource)}-mapper.xml"

    def mapper_namespace(self, x_name: str) -> str:
        """리소스의 mapper namespace를 반환한다."""
        return f"{self.mybatis_store_pkg}.{x_name}Store"

    @property
    def static_dir(self) -> str:
        """JSP와 정적 파일을 복사해 두는 폴더."""
        return f"{self.identity.modules.boot}/src/main/webapp/WEB-INF/static/{self.identity.project_name}"


# ---- mapper ------------------------------------------------------------------


@dataclass
class Statement:
    """mapper의 SQL 문 하나."""
    id: str                         # 마지막 조각만 남긴 id
    original_id: str = ""           # 원본에 쓰여 있던 id
    verb: str = "select"            # 본문의 첫 SQL 키워드로 정한 종류: select, insert, update, delete
    tag: str = ""                   # XML 태그 이름. 복사한 mapper에서는 verb와 다를 수 있다
    params: list[str] = field(default_factory=list)


@dataclass
class MapperFile:
    """출력에 있는 mapper 파일 하나."""
    resource: str                   # 원본에서 온 리소스 이름(파일 이름 등)
    x_name: str
    namespace: str
    path: str                       # 출력 루트 기준
    source: str = ""                # 원본 루트 기준
    origin: str = "converted"       # converted: SQL 파일을 변환함. copied: 있던 mapper를 복사함
    statements: list[Statement] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)   # sql-key의 앞부분으로 쓰일 수 있는 이름들
    error: str = ""


class MapperIndex:
    """``sql-key``로 mapper와 SQL 문을 찾는다."""

    def __init__(self, mappers: Optional[list[MapperFile]] = None) -> None:
        self.mappers: list[MapperFile] = list(mappers or [])

    def by_x(self, x_name: str) -> Optional[MapperFile]:
        """클래스 이름 앞부분이 같은 mapper를 반환한다. 없으면 None이다."""
        for m in self.mappers:
            if m.x_name == x_name:
                return m
        return None

    def candidates(self, sql_key: str) -> list[tuple[MapperFile, Statement]]:
        """키에 맞는 SQL 문을 모두 반환한다. ``find``가 None을 반환했을 때 둘 이상이 맞았는지 알아보는 데 쓴다.
        찾는 순서는 ``find``와 같고, 처음으로 하나라도 맞은 단계의 결과만 반환한다."""
        key = str(sql_key or "").strip()
        if not key:
            return []
        prefix, _, ident = key.rpartition(".")
        hits = [(m, s) for m in self.mappers for s in m.statements if "." in key and s.original_id == key]
        if not hits and prefix:
            low = prefix.lower()
            hits = [(m, s) for m in self.mappers if low in {k.lower() for k in m.keys}
                    for s in m.statements if s.id == ident]
        if not hits:
            hits = [(m, s) for m in self.mappers for s in m.statements if s.id == ident]
        return hits

    def find(self, sql_key: str) -> Optional[tuple[MapperFile, Statement]]:
        """``a.b`` 꼴의 키로 SQL 문을 찾는다. 찾지 못했거나 둘 이상이 맞으면 None이다.
        원본 id가 키와 같은 문을 먼저 찾는다.
        없으면 앞부분 ``a``가 mapper의 ``keys``에 있고 id가 ``b``인 문, 그래도 없으면 id가 ``b``인 문을 전체에서 찾는다.

        The exact original id goes first: a file that holds `a.getList` and `b.getList` keeps the first as `getList`
        and renames the second, and both prefixes are keys of that one file."""
        key = str(sql_key or "").strip()
        if not key:
            return None
        prefix, _, ident = key.rpartition(".")
        hits: list[tuple[MapperFile, Statement]] = [(m, s) for m in self.mappers for s in m.statements
                                                    if "." in key and s.original_id == key]
        if not hits and prefix:
            low = prefix.lower()
            hits = [(m, s) for m in self.mappers if low in {k.lower() for k in m.keys}
                    for s in m.statements if s.id == ident]
        if not hits:
            hits = [(m, s) for m in self.mappers for s in m.statements if s.id == ident]
        return hits[0] if len(hits) == 1 else None


# ---- 서비스 XML ----------------------------------------------------------------


@dataclass
class Activity:
    """서비스 XML의 activity 하나."""
    name: str
    class_name: str = ""
    kind: str = "other"             # search, update, upsert, delete, sortsearch, pagesearch, router, user, other
    properties: dict[str, str] = field(default_factory=dict)
    transitions: dict[str, str] = field(default_factory=dict)


@dataclass
class DbVerdict:
    """activity가 DB를 쓰는지 판정한 결과."""
    db_backed: bool = False
    confidence: str = "ambiguous"   # certain, strong, not_db, external_call, ambiguous
    pass_hit: str = "3"             # 판정한 단계 이름. docs/FW-CONVERT.md 2.3의 표
    statement: str = ""             # 찾은 SQL 문의 키(``ns.id``). 없으면 빈 문자열
    evidence: str = ""              # 사람이 읽을 근거 한 줄


@dataclass
class ServiceDef:
    """서비스 XML 파일 하나에서 읽은 서비스."""
    name: str                       # 쓰여 있는 이름
    resource: str = ""              # 이름에서 서비스 접미사를 뗀 것
    x_name: str = ""
    source: str = ""
    dialect: str = "activity"       # activity: 변환하는 형식. bean: 변환하지 않는 형식
    activities: list[Activity] = field(default_factory=list)
    has_router: bool = False
    transitions: dict[str, str] = field(default_factory=dict)     # router의 전이 이름과 activity 이름
    verdicts: dict[str, DbVerdict] = field(default_factory=dict)  # activity 이름별 판정


# ---- 리소스(생성 단위) ---------------------------------------------------------


@dataclass
class Endpoint:
    """mapper에서 만든 메서드가 노출될 REST 주소."""
    verb: str                       # GET, POST, DELETE
    path: str = ""                  # 컨트롤러 주소 뒤에 붙는 부분. 빈 문자열, /summary, /{id} 등


@dataclass
class Method:
    """리소스의 메서드 하나. 생성하는 모든 클래스가 이 레코드로 메서드를 만든다."""
    name: str
    origin: str = "service"         # service: 서비스 XML에서 옴. mapper: mapper의 SQL 문에서 옴
    kind: str = "other"             # activity 종류. mapper에서 온 메서드는 statement
    returns: str = "list"           # list, int, map
    db_backed: bool = False
    read_only: bool = False         # 조회면 True. 트랜잭션을 읽기 전용으로 만든다
    activity: str = ""              # 서비스에서 온 메서드의 activity 이름
    pass_hit: str = ""              # DB 판정 단계. DB를 쓰지 않는 메서드의 주석에 쓴다
    evidence: str = ""
    namespace: str = ""             # 호출할 SQL 문이 있는 mapper의 namespace. 없으면 빈 문자열
    statement_id: str = ""          # 호출할 SQL 문의 id
    session_call: str = ""          # selectList, selectOne, insert, update, delete. SQL 문이 없으면 빈 문자열
    endpoint: Optional[Endpoint] = None   # mapper 기반 컨트롤러에서 노출할 주소. 노출하지 않으면 None
    # 프론트엔드가 호출하지 않아서 노출하지 않은 주소. 보고서에 남겨서 404가 났을 때 찾을 수 있게 한다
    unexposed: Optional[Endpoint] = None


@dataclass
class Resource:
    """같은 이름 ``{X}``로 묶인 서비스와 mapper. 클래스 한 벌을 생성하는 단위다."""
    resource: str                   # 서비스 이름에서 접미사를 뗀 것, 또는 mapper의 리소스 이름
    x_name: str
    methods: list[Method] = field(default_factory=list)
    service: Optional[ServiceDef] = None
    mapper: Optional[MapperFile] = None     # 같은 이름의 mapper 가운데 첫째
    mappers: list[MapperFile] = field(default_factory=list)       # 같은 이름의 mapper 전부. 메서드는 모두에서 만든다
    has_router: bool = False
    transitions: dict[str, str] = field(default_factory=dict)     # 전이 이름과 메서드 이름. 메서드가 없으면 빈 문자열
    stub: bool = False              # router는 있는데 호출할 SQL 문이 하나도 없으면 True
    # mapper 기반 컨트롤러의 주소에서 ``/api/`` 뒤에 오는 이름. 리소스끼리 겹치지 않는다. 비어 있으면 리소스 이름의 kebab이다
    rest_base: str = ""


# ---- 프론트엔드 ----------------------------------------------------------------


@dataclass
class RestCall:
    """프론트엔드 코드가 호출하는 REST 주소 하나."""
    verb: str
    path: str
    source: str = ""


@dataclass
class FrontendApiMap:
    """프론트엔드에서 읽은 화면 주소와 REST 호출."""
    rest_endpoints: list[RestCall] = field(default_factory=list)
    page_routes: list[str] = field(default_factory=list)
    mvc_url_prefix: str = ""
    # 화면 주소별로 원본에 쓰여 있던 주소들. 컨텍스트 경로는 뺀다. ``/``로 시작하면 전체 경로다
    # 아니면 쓰여 있던 상대 경로 그대로다(``이름.mvc``, ``../폴더/이름.mvc``). 생성기가 화면 주소 앞부분을 기준으로 푼다
    page_urls: dict[str, list[str]] = field(default_factory=dict)


# ---- 결과 ----------------------------------------------------------------------


@dataclass
class QueueItem:
    """사람이 확인해야 할 항목 하나."""
    component: str
    reason: str
    source_path: str = ""
    last_error: str = ""
    excerpt: str = ""               # 원본 소스의 앞부분. 2,000자까지


@dataclass
class GeneratedFile:
    """생성한 텍스트 파일 하나. 경로는 출력 루트 기준이다."""
    path: str
    text: str


# ---- 전환 실행(파이프라인) -------------------------------------------------------


@dataclass
class ConvertOptions:
    """전환 한 번에 필요한 값. 설정과 실행 정보에서 호출자가 채운다."""
    source_name: str                # 프로젝트 이름을 만들 이름(입력한 프로젝트 이름이나 올린 파일 이름)
    domain: str = ""                # 도메인 코드. 비어 있으면 이름의 접두사와 규칙의 기본값으로 정한다
    override: str = ""              # 프로젝트 이름을 그대로 지정할 때만 쓴다
    java_version: str = "21"        # 목표 Java 버전
    boot_version: str = ""          # 목표 Spring Boot 버전. 생성하는 루트 pom의 부모 버전이다
    group_id: str = ""              # 생성하는 pom의 groupId. 비어 있으면 패키지에서 만든다
    version: str = "1.0.0"          # 생성하는 pom의 version
    # pom에 버전을 써야 하는 의존성과 그 버전("g:a": "버전"). 버전은 호출자가 채운다. 빈 값은 허용하지 않는다
    versions: dict[str, str] = field(default_factory=dict)
    # 벤더 이름과 JDBC 드라이버 좌표("g:a" 또는 "g:a:v")
    jdbc_drivers: dict[str, str] = field(default_factory=dict)


@dataclass
class DatasourceInfo:
    """원본 설정에서 읽은 DB 연결 정보. 비밀번호 값은 담지 않는다."""
    configured: bool = False
    vendor: str = ""                # oracle, mysql, mariadb, postgresql, sqlserver, tibero, h2
    url: str = ""
    driver_class: str = ""
    username: str = ""
    has_password: bool = False      # 원본에 비밀번호가 있었는지. 값은 어디에도 옮기지 않는다
    # 찾지 못한 이유: not_found, jndi, unknown_vendor, unresolved(주소에 풀리지 않은 ${…}가 남음). 찾았으면 빈 문자열
    reason: str = ""
    source: str = ""                # 읽은 파일. 원본 루트 기준


@dataclass
class ConvertResult:
    """전환 한 번의 결과. 생성한 파일은 출력 폴더에 있고 여기에는 요약만 둔다."""
    converted: bool = False         # 전환할 파일(서비스 XML, SQL 파일)이 하나도 없으면 False다. 출력 폴더는 비어 있다
    identity: Optional[ProjectIdentity] = None
    counts: dict[str, int] = field(default_factory=dict)
    datasource: Optional[DatasourceInfo] = None
    queue: list[QueueItem] = field(default_factory=list)
    unexposed: list[str] = field(default_factory=list)      # 노출하지 않은 REST 주소
    skipped: list[str] = field(default_factory=list)        # 읽지 않은 파일과 이유
    errors: list[str] = field(default_factory=list)
