"""설정 파일들을 읽어 Config 하나로 합치는 모듈. 설정 키의 구조도 여기서 정의한다.

Every command and sbmig.api.migrate start with load_config.
Merge order (later wins): default.yml -> --config yml (the company artifacts yml) -> SBMIG_* environment
variables -> overrides (explicit values).
`.env` only reaches os.environ. The LLM key is read from the environment (LLM_API_KEY) only, never written to
a yml or a log. Company values (in-house coordinates, prices, gateway) live in yml, never in code.
A new or changed key also changes config/default.yml and docs/INTERFACES.md.
"""
from __future__ import annotations

import fnmatch
import logging
import os
import re
from pathlib import Path
from collections.abc import Iterable
from typing import Any, Literal, Optional, get_args, get_origin

import yaml
from pydantic import BaseModel, Field, PrivateAttr, ValidationInfo, field_validator

log = logging.getLogger("sbmig.config")

from .models import GAV
from .scrub import refresh_secrets

PACKAGE_ROOT = Path(__file__).resolve().parents[2]          # 소스에서 실행할 때의 저장소 루트
DEFAULT_CONFIG_PATH = PACKAGE_ROOT / "config" / "default.yml"


class TargetConfig(BaseModel):
    """목표 버전을 담는 설정 클래스. 기본값은 Java 21, Spring Boot 4.1.0, Spring Framework 7이다."""
    java_version: str = "21"
    spring_boot_version: str = "4.1.0"
    spring_framework_version: str = "7.0.x"


class ArtifactSpec(BaseModel):
    """사내 아티팩트 하나의 좌표를 담는 설정 클래스.
    `aliases`는 같은 아티팩트로 보는 이전 g:a 키다. `property_name`은 버전을 두는 pom 속성 이름이다."""
    group_id: str
    artifact_id: str
    version: Optional[str] = None            # None이면 부모 pom이 버전을 관리한다
    scope: Optional[str] = None
    type: Optional[str] = None
    # 같은 아티팩트의 이전 g:a 키. as-is가 이 이름으로 쓰고 있으면 새 이름으로 바꾼다
    aliases: list[str] = Field(default_factory=list)
    # 관례상 버전을 pom 속성에 둘 때 그 속성 이름
    property_name: Optional[str] = None
    # 선택 아티팩트를 새로 넣게 하는 as-is 의존성(g:a 키 또는 glob). 프로젝트 pom의 <dependencies>에 있으면
    # 그 pom에 이 아티팩트를 선언한다. dependencyManagement에만 있는 것과 자신의 키, 별칭은 트리거가 아니다
    when_present: list[str] = Field(default_factory=list)
    # true(기본값)면 트리거가 된 원래 선언을 지운다. false면 원래 선언을 남기고 옆에 추가한다
    replaces: bool = True

    @field_validator("when_present", "aliases", mode="before")
    @classmethod
    def _key_list(cls, v: Any) -> Any:
        """값을 g:a 목록으로 정규화한다. 문자열 하나도 목록으로 바꾸고 공백을 지운다."""
        if v is None:
            return []
        if isinstance(v, str):
            v = [x for x in re.split(r"[,\s]+", v) if x]
        if isinstance(v, (list, tuple)):
            return [str(x).strip() for x in v if str(x).strip()]
        return v

    def gav(self) -> GAV:
        """이 설정을 GAV 모델로 반환한다."""
        return GAV(group_id=self.group_id, artifact_id=self.artifact_id, version=self.version,
                   scope=self.scope, type=self.type)

    @property
    def key(self) -> str:
        """버전을 뺀 `groupId:artifactId` 키를 반환한다."""
        return f"{self.group_id}:{self.artifact_id}"

    def triggered_by(self, key: str) -> bool:
        """의존성 ``key``가 있으면 이 선택 아티팩트를 넣어야 하는지 확인한다.
        ``when_present``에 맞고 아티팩트 자신의 키나 별칭이 아닐 때만 True다.


        Exact match or `fnmatch` glob on `group:artifact`."""
        if not self.when_present or not key or key == self.key or key in self.aliases:
            return False
        return any(key == pat or fnmatch.fnmatchcase(key, pat) for pat in self.when_present)


class ArtifactsConfig(BaseModel):
    """사내 아티팩트 설정을 담는 클래스.
    루트 부모(base-parent), 역할 부모(boot-jar / comp-jar), 필수 아티팩트 2개, 선택 아티팩트 8개로 이루어진다.
    실제 좌표는 회사 yml에만 둔다. `config/artifacts.example.yml`의 좌표는 플레이스홀더다."""
    # 루트 pom의 사내 부모(회사에서는 base-parent). 저장소에서 찾지 못하면 settings나 Nexus 문제로 보고 실행을 멈춘다
    parent: ArtifactSpec
    # 역할 부모. `{name}-boot` 모듈은 boot_parent를, 나머지 모듈은 comp_parent를 상속한다. 루트 pom은 집계 pom이 된다
    # 둘 다 None이면 모듈이 루트 pom을 상속한다. as-is 부모가 역할 부모와 g:a만 같으면 설정한 버전으로 바꾼다
    boot_parent: Optional[ArtifactSpec] = None
    comp_parent: Optional[ArtifactSpec] = None
    # 사내 부모 체인이 Spring Boot 버전을 관리하는지. auto(기본값)면 부모 체인의 pom을 읽어 정한다
    # 체인이 spring-boot-dependencies를 상속하거나 import하면 관리하는 것으로 본다
    # 관리하지 않으면 재구성이 Boot BOM을 직접 import한다. 버전은 체인의 속성(`${spring-boot.version}` 등)이다
    parent_manages_spring_boot: bool | Literal["auto"] = "auto"
    parent_manages_java_version: bool = True  # false면 재구성이 pom에 maven.compiler.release를 추가한다
    # 필수 사내 아티팩트(운영 설정은 정확히 2개). mandatory_target이 정한 pom에 선언한다
    mandatory: list[ArtifactSpec] = Field(default_factory=list)
    # 선택 사내 아티팩트(운영 설정은 8개). 이미 쓰고 있을 때만 optional_version_policy대로 버전을 맞춘다
    optional: list[ArtifactSpec] = Field(default_factory=list)
    # 프로젝트에 있으면 지울 의존성의 g:a 키(예: 이전 사내 부모가 쓰던 BOM)
    remove_when_present: list[str] = Field(default_factory=list)
    # 필수 의존성을 선언할 pom. root(기본값, 회사 규칙)는 모든 루트 pom이다. MSA는 서비스 루트마다, 맨 위 집계 pom 포함
    # modules는 역할 모듈 pom마다 선언한다
    mandatory_target: Literal["root", "modules"] = "root"
    # 선택 아티팩트 버전을 맞추는 방식. floor(기본값)는 설정한 버전보다 낮을 때만 올린다. 내리지는 않는다
    # pin은 항상 설정한 버전으로 맞춘다. 버전이 내려갈 수도 있다
    optional_version_policy: Literal["floor", "pin"] = "floor"

    @property
    def role_parents(self) -> bool:
        """역할 부모(boot_parent, comp_parent)가 둘 다 설정되었는지 확인한다."""
        return self.boot_parent is not None and self.comp_parent is not None

    def parent_specs(self) -> list["ArtifactSpec"]:
        """설정된 사내 부모 전체를 루트 부모, boot_parent, comp_parent 순으로 반환한다."""
        return [s for s in (self.parent, self.boot_parent, self.comp_parent) if s is not None]

    def parent_keys(self) -> list[str]:
        """사내 부모의 g:a 키를 별칭까지 포함해 반환한다.
        이 키의 부모를 찾지 못하면 settings나 Nexus 문제로 보고 실행을 중단한다."""
        out: list[str] = []
        for s in self.parent_specs():
            for k in (s.key, *s.aliases):
                if k not in out:
                    out.append(k)
        return out


class LegacyContextConfig(BaseModel):
    """Spring Framework / servlet 프로젝트를 Boot로 바꿀 때, 기동에 필요한 항목을 재구성이 미리 고칠지 정하는 설정 클래스.
    빌드와 LLM은 기동 실패를 볼 수 없어서 재구성이 미리 고친다. 끄면 인수인계 문장만 남는다.
    필터 클래스와 Boot 속성의 매핑은 `knowledge/config/web_xml_rules.yml`에 있다."""
    # @ImportResource에는 web.xml의 contextConfigLocation(루트 + DispatcherServlet)이 가리키는 XML만 넣는다
    # 그 XML이 <import>로 가져오는 파일은 Spring이 직접 읽는다. web.xml이 없거나 찾지 못하면 찾은 XML을 모두 넣는다
    entry_xml_only: bool = True
    exclude_test_xml: bool = True            # src/test/ 아래 XML은 Boot 컨텍스트에 넣지 않는다
    # as-is Spring은 같은 id의 빈 덮어쓰기와 순환 참조를 허용했지만 Boot 4는 둘 다 기본값이 금지다
    # true면 spring.main.allow-bean-definition-overriding / allow-circular-references를 true로 쓴다
    # 중복 빈 id는 인수인계에 기록한다
    allow_bean_overriding: bool = True
    allow_circular_references: bool = True
    # 건너뛴 web.xml 필터의 init-param을 web_xml_rules.yml의 properties 매핑대로 Boot 속성으로 옮긴다
    filter_properties: bool = True
    # <mvc:default-servlet-handler/>를 server.servlet.register-default-servlet=true로 옮긴다
    default_servlet: bool = True
    # XML의 CommonsMultipartResolver를 설정 복사본에서 빼고 spring.servlet.multipart.* 크기 제한으로 옮긴다
    multipart_resolver: bool = True
    # @EnableWebMvc나 WebMvcConfigurationSupport가 있으면 Boot 자동 설정이 꺼진다
    # 그때는 spring.mvc.view.*를 쓰지 않고 인수인계로 넘긴다
    detect_enable_web_mvc: bool = True
    # 설정 복사본의 /WEB-INF/... 위치를 classpath:asis-webinf/...로 고친다(jar 안에는 WEB-INF 경로가 없다)
    rewrite_webinf_locations: bool = True
    # classpath 위치의 ${...}를 *로 바꿔 프로필별 파일을 모두 찾는다
    profile_globs: bool = True
    # as-is에서 provided였던 의존성(jakarta.servlet-api 등)은 레시피가 compile로 바꿔도 provided로 되돌린다
    restore_asis_scopes: bool = True
    # spring_legacy 프로젝트에 웹 근거(web.xml, spring-webmvc, servlet-api, @Controller, JSP)가 있을 때만
    # webmvc starter를 넣는다. 근거가 없으면 spring-boot-starter를 넣는다. 배치나 CLI에서 Tomcat이 뜨지 않게 한다
    web_starter_by_evidence: bool = True
    # 소스가 MS949 / EUC-KR인 모듈의 pom에 project.build.sourceEncoding을 기록한다
    # JDK 18부터 javac 기본 인코딩이 UTF-8이라 그대로 두면 한글이 깨진다
    write_source_encoding: bool = True
    # pom의 문자 인코딩이 XML 선언과 다르면 자동 변환 전에 바로잡는다
    # Maven은 그런 pom도 빌드하지만 OpenRewrite는 읽지 못해 변환 전체가 실패한다
    correct_pom_encoding: bool = True
    # JNDI, DelegatingFilterProxy, *.do 매핑, <jsp-config>, log4j 1, Security XML처럼 자동으로 옮기지 못하는 항목을
    # 인수인계 문장과 XML 복사본 옆 `<!-- MIGRATION-TODO -->` 주석으로 남긴다
    handover_sentences: bool = True


class LayoutConfig(BaseModel):
    """회사 표준 모듈 구성을 정하는 설정 클래스.
    루트 바로 아래에 `{name}-boot`(Boot 설정만, boot-jar)와 `{name}-back`(모든 소스, comp-jar)을 만든다.
    웹 리소스가 있을 때만 `{name}-front`(comp-jar)도 만든다. Boot as-is 프로젝트는 디렉토리를 그대로 두고 pom만 바꾼다.
    이전 구성은 `config/artifacts.local-e2e.yml`이 쓴다.

    Pre-9c layout = `module_dir: module`, `boot_module: boot-jar`, `comp_module: comp-jar`,
    `front_module: null`, `boot_as_is: layout`."""
    module_dir: str = ""                                # 모듈을 둘 하위 폴더. ""이면 루트 바로 아래(회사 규칙)
    boot_module: str = "{name}-boot"
    comp_module: str = "{name}-back"
    # 웹 리소스 모듈의 디렉토리 이름. None이면 front 모듈을 만들지 않고 웹 리소스를 Boot 모듈에 둔다
    front_module: Optional[str] = "{name}-front"
    project_name: Optional[str] = None                  # `{name}`에 쓸 이름. None이면 루트 pom의 artifactId
    front_static_dir: str = "src/main/resources/static"                          # HTML, css, js, 이미지
    front_jsp_dir: str = "src/main/resources/META-INF/resources/WEB-INF/jsp"     # JSP(Servlet 3 jar 리소스 규칙)
    # 웹 루트 안 리소스 폴더(media, templates, css 등)의 파일을 소스 모듈 classpath에 남기고
    # front 정적 폴더에도 원래 웹 경로 그대로 복사한다. false면 소스 모듈에만 두고 옮길 파일을 인수인계에 남긴다
    front_copy_resource_dirs: bool = True
    # 레시피는 JSP를 읽지 못한다. 그래서 파일이 최종 위치로 옮겨진 뒤 재구성이 JSP와 태그 파일의 javax EE 이름을 jakarta로 바꾼다
    # 코드 영역만 바꾸고 Java 문자열, 주석, 태그 라이브러리 URI는 그대로 둔다. false면 인수인계 항목만 남긴다
    jsp_jakarta_rename: bool = True
    # 프로젝트 안에 있지만 어떤 pom도 선언하지 않은 jar를 첫 빌드 전에 선언한다. 저장소에 좌표가 있으면 compile 의존성,
    # 없으면 <모듈>/lib에 복사하고 system scope로 선언한다. false면 수정 루프의 사전 시도만 jar를 추가한다
    wire_bundled_jars: bool = True
    # Boot as-is 프로젝트(MSA 포함)의 재구성 방식. in-place(회사 규칙)는 디렉토리를 그대로 두고 pom만 고친다
    # layout은 Boot가 아닌 프로젝트처럼 boot / back 모듈로 나눈다
    boot_as_is: Literal["in-place", "layout"] = "in-place"
    # 모듈이 하나인 Boot 앱의 처리 방식(boot_as_is가 in-place일 때). split은 Boot가 아닌 프로젝트처럼
    # boot / 소스 / 화면 모듈로 나누고, wrap은 통째로 `{name}-boot` 하나에 넣는다
    boot_single_module: Literal["split", "wrap"] = "split"
    # Boot 프로젝트에서 Boot 앱 모듈로 볼 디렉토리 이름 패턴. pom이 Boot 앱인지 다시 확인한다
    boot_module_pattern: str = "*-boot"
    # JSP가 있을 때 Boot 모듈의 패키징. 기본값은 war(실행 가능한 WAR)다. 실행 jar에서는 JSP를 서빙할 수 없다
    jsp_boot_packaging: Literal["war", "jar"] = "war"
    # 모듈 artifactId는 루트 artifactId에 이 접미사를 붙인다(front는 "-front")
    boot_artifact_suffix: str = "-boot"
    comp_artifact_suffix: str = "-back"
    # 소스를 나누는 방식. minimal(회사 규칙)은 모든 소스를 소스 모듈로 옮기고 Boot 모듈에는 Application 클래스와
    # Boot 리소스만 둔다. web-layer는 @Controller, @Configuration 등이 붙은 클래스를 Boot 모듈로 옮긴다
    split_policy: Literal["minimal", "web-layer"] = "minimal"
    # as-is 다중 모듈(MSA 아님)의 처리 방식. merge(기본값)는 모든 모듈의 소스를 소스 모듈 하나로 합치고,
    # wrap은 기존 모듈을 module/ 아래 그대로 두고 Boot 모듈만 추가한다
    multi_module_policy: Literal["wrap", "merge"] = "merge"
    # 루트에 pom이 없고 서로 의존하는 형제 pom만 있으면, 접수 단계가 루트 집계 pom을 만들어 한 reactor로 빌드한다
    # false면 그런 프로젝트는 루트 pom이 없어서 진행할 수 없다
    generate_aggregator: bool = True
    # merge에서 같은 경로의 파일이 여러 모듈에 있을 때의 처리. first-wins는 <modules> 순서상 먼저 나온 파일을 쓰고
    # 나머지는 `<run dir>/merge-conflicts/`에 보관해 인수인계에 남긴다. fail은 재구성 오류로 멈춘다
    merge_collision: Literal["first-wins", "fail"] = "first-wins"
    # 새 부모 체인이 관리하는 의존성의 명시 <version>을 지운다. gte는 관리 버전이 같거나 높을 때만, any는 항상 지우고
    # off는 지우지 않는다. 사내 부모, 필수 / 선택 아티팩트, system scope 의존성은 건드리지 않는다
    drop_managed_versions: Literal["gte", "any", "off"] = "gte"
    # 프로젝트 <properties>에서 사내 부모 체인이나 Boot BOM과 같은 이름의 버전 속성(spring-boot.version 등)을 지운다
    # 자식 속성이 부모 속성보다 우선해서, as-is 값이 남으면 새 버전을 덮어쓴다. `${이름}`으로 아직 참조하면 남긴다
    drop_bom_property_overrides: bool = True
    # 부모 체인 pom에는 없지만 회사가 지우려는 속성 이름(예외 처리용). 체인이 정의한 이름은 부모 pom에서 읽는다
    # 그래서 보통 비워 둔다. 아직 `${이름}`으로 참조하면 남긴다
    parent_property_names: list[str] = Field(default_factory=list)
    # 재구성이 BOM을 직접 import할 때 버전으로 쓸 부모 체인 속성 이름 후보. 체인이 정의한 첫 이름을 `${이름}`으로 쓴다
    # 하나도 없으면 target의 버전 숫자(Spring Cloud는 spring_cloud_bom.yml의 짝)를 그대로 쓴다
    bom_version_properties: dict[str, list[str]] = Field(default_factory=lambda: {
        "spring_boot": ["spring-boot.version", "spring.boot.version", "springboot.version", "spring-boot-version"],
        "spring_cloud": ["spring-cloud.version", "spring.cloud.version", "spring-cloud-version"],
    })
    application_class_name: str = "Application"          # Boot 모듈에 새로 만드는 시작 클래스 이름
    # Boot 모듈의 src/main/resources로 옮길 설정 파일 이름 패턴. resources 바로 아래와 config 폴더의 파일이 대상이다
    # 파일 이름으로만 비교한다. 폴더 경로는 넣을 수 없다
    boot_resources_glob: list[str] = Field(default_factory=lambda: [
        "application*.yml", "application*.yaml", "application*.properties",
        "bootstrap*.yml", "bootstrap*.properties", "logback*.xml", "log4j2*.xml", "banner.txt",
    ])
    # 프로젝트 종류별로 Boot 모듈에 넣을 Boot starter
    starter_by_case: dict[str, str] = Field(default_factory=lambda: {
        "pure_java": "spring-boot-starter",
        "servlet_tomcat": "spring-boot-starter-webmvc",
        "spring_legacy": "spring-boot-starter-webmvc",
        "spring_boot": "auto",          # as-is에 이미 있는 starter를 그대로 둔다
    })
    # 생성한 Application 클래스에 @ServletComponentScan을 붙인다. @WebServlet / @WebFilter / @WebListener 클래스가 등록된다
    add_servlet_component_scan: bool = True
    # web.xml의 서블릿, 필터, 리스너, 오류 페이지, 세션 타임아웃을 옮기는 방식. Boot는 web.xml을 읽지 않는다
    # generate는 boot 모듈에 WebXmlRegistrations @Configuration을 만든다. followup은 인수인계에만 남긴다
    web_xml_registrations: Literal["generate", "followup"] = "generate"
    # 찾은 Spring XML 빈 설정(<beans>)을 @ImportResource로 넣는다. false면 XML의 빈이 Boot에서 생성되지 않는다
    import_xml_bean_configs: bool = True
    # MSA에서 사내 루트 부모를 지정할 위치. per-service-root(회사 규칙)는 서비스 루트 pom마다 지정한다
    # top-aggregator는 맨 위 집계 pom에 한 번 지정하고 레시피도 한 번에 실행한다
    msa_parent_policy: Literal["per-service-root", "top-aggregator"] = "per-service-root"
    # legacy Spring 컨텍스트를 Boot로 바꿀 때 재구성이 미리 처리하는 항목들
    legacy_context: LegacyContextConfig = Field(default_factory=LegacyContextConfig)

    def module_name(self, which: Literal["boot", "comp", "front"], name: str) -> Optional[str]:
        """역할(boot / comp / front)의 모듈 디렉토리 이름을 반환한다. 이름의 `{name}`은 ``name``으로 바꾼다.
        front 모듈을 쓰지 않으면 None을 반환한다."""
        tpl = {"boot": self.boot_module, "comp": self.comp_module, "front": self.front_module}[which]
        return tpl.replace("{name}", name) if tpl else None

    def module_path(self, which: Literal["boot", "comp", "front"], name: str) -> Optional[str]:
        """역할 모듈의 루트 기준 posix 경로(module_dir + 이름)를 반환한다. 예: `shop-boot`, `module/boot-jar`."""
        n = self.module_name(which, name)
        if n is None:
            return None
        d = self.module_dir.strip("/")
        return f"{d}/{n}" if d else n


class RecipeSetConfig(BaseModel):
    """실행할 OpenRewrite 레시피 묶음을 정하는 설정 클래스. 계획 단계가 프로젝트 종류와 분석 결과로 묶음을 고른다.
    현업 결정으로 Java 21, javax -> jakarta, Boot 4처럼 꼭 필요한 묶음만 넣는다. 모두 재구성 전에 한 번에 실행한다.
    빈 묶음은 회사 설정이 다시 채울 수 있게 남겨 둔다. 채울 때는 catalog.yml의 `sets:`에도 넣어야 한다.

    Mirrors `openrewrite.recipes` of config/default.yml: a Config() without the yml plans the same recipes."""
    jdk21: list[str] = Field(default_factory=lambda: ["org.openrewrite.java.migrate.UpgradeToJava21"])
    jakarta: list[str] = Field(default_factory=lambda: [
        "org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta",
    ])
    spring_boot_4: list[str] = Field(default_factory=lambda: [
        "org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0",
        "org.openrewrite.java.spring.boot4.SpringBootProperties_4_1",
    ])
    # Spring Framework 프로젝트에 넣는 묶음. 재구성 뒤에는 Boot 4.1 BOM이 Spring 7 / Jackson 3를 가져온다
    # 그래서 그에 맞는 코드 변경을 레시피가 미리 한다. Boot 프로젝트는 상위 레시피 안에서 같은 단계가 실행된다
    spring_framework_7: list[str] = Field(default_factory=lambda: [
        "org.openrewrite.java.spring.framework.UpgradeSpringFramework_7_0",
    ])
    # Spring Security를 쓰는 Spring Framework 프로젝트에만 넣는다. 6_1 ~ 6_5와 7_0은 앞 단계를 포함하지 않아서 모두 나열한다
    # Boot 프로젝트는 UpgradeSpringBoot_4_0 안에서 같은 단계가 실행된다
    spring_security_7: list[str] = Field(default_factory=lambda: [
        *(f"org.openrewrite.java.spring.security6.UpgradeSpringSecurity_6_{v}" for v in range(6)),
        "org.openrewrite.java.spring.security7.UpgradeSpringSecurity_7_0",
    ])
    junit5: list[str] = Field(default_factory=list)      # 테스트는 범위 밖이라 꺼 둔 묶음
    # 아래 세 묶음은 그 라이브러리를 쓸 때만 넣는다. Boot 프로젝트는 상위 레시피가 같은 단계를 이미 실행한다
    # spring_batch_6은 spring-batch를, hibernate_7은 Hibernate ORM을 직접 쓰는 Spring Framework 프로젝트에 넣는다
    # servlet_jakarta_10은 모든 서블릿 프로젝트에 넣는다. 빈 목록이면 끈다
    spring_batch_6: list[str] = Field(default_factory=lambda: [
        "org.openrewrite.java.spring.batch.SpringBatch5To6Migration",
    ])
    hibernate_7: list[str] = Field(default_factory=lambda: [
        "org.openrewrite.hibernate.MigrateToHibernate71",
    ])
    servlet_jakarta_10: list[str] = Field(default_factory=lambda: [
        "org.openrewrite.java.migrate.jakarta.RemovalsServletJakarta10",
    ])
    # Hystrix를 쓰는 프로젝트에만 넣는다. Boot / Framework 묶음보다 먼저 실행해 Resilience4j로 바꾼다
    # 빈 목록이면 끄고 Hystrix는 as-is 버전 그대로 남는다
    hystrix_resilience4j: list[str] = Field(default_factory=lambda: [
        "com.company.migrate.HystrixToResilience4j",
    ])
    extra: dict[str, list[str]] = Field(default_factory=dict)


class OpenRewriteConfig(BaseModel):
    """OpenRewrite 실행(transform.rewrite) 설정을 담는 클래스.
    플러그인과 레시피 BOM 버전, 레시피 아티팩트, 시간 제한, JVM 메모리, 다운로드 재시도, 파싱에서 뺄 파일을 정한다."""
    plugin_version: str = "6.46.1"
    # 레시피 BOM은 3.37.0(rewrite-spring 6.37.0)으로 고정한다. 3.38.0은 Central에 없는 아티팩트를 가리킨다
    recipe_bom_version: str = "3.37.0"
    recipe_artifacts: list[str] = Field(default_factory=lambda: [
        "org.openrewrite.recipe:rewrite-migrate-java",
        "org.openrewrite.recipe:rewrite-spring",
        "org.openrewrite.recipe:rewrite-testing-frameworks",
        "org.openrewrite.recipe:rewrite-static-analysis",
    ])
    # 레시피 아티팩트 버전을 하나씩 직접 지정한다. 비워 두면 BOM이 정한 버전을 쓴다
    recipe_artifact_versions: dict[str, str] = Field(default_factory=dict)
    recipes: RecipeSetConfig = Field(default_factory=RecipeSetConfig)
    timeout_minutes: int = 40                           # 레시피 실행 한 번의 시간 상한(분)
    jvm_args: list[str] = Field(default_factory=lambda: ["-Xmx4g"])
    # OutOfMemoryError로 실패하면 이 JVM 옵션으로 한 번 더 실행한다
    oom_retry_jvm_args: list[str] = Field(default_factory=lambda: ["-Xmx8g"])
    # 레시피 실행이 다운로드 실패(프록시 429/5xx, 연결 끊김, 타임아웃)로 실패하거나 `x.y.x` 같은 버전을 남기면 다시 실행한다
    # 그 횟수와 대기 시간(초)이다. 0이면 다시 실행하지 않는다
    download_retry: int = 1
    download_retry_wait_s: int = 30
    # OpenRewrite가 파싱하지 않을 파일(`-Drewrite.exclusions`). JSP / HTML은 어느 레시피도 건드리지 않고 파서 경고만 남긴다
    # JSP 안의 javax 이름은 재구성이 바꾼다(layout.jsp_jakarta_rename)
    exclusions: list[str] = Field(default_factory=lambda: ["**/*.jsp", "**/*.jspx", "**/*.html", "**/*.htm"])
    # as-is가 쓰는 사내 부모 버전이 저장소에 없어서 레시피 실행이 실패했을 때의 처리
    # align은 부모를 설정한 버전으로 바꿔 커밋하고 레시피를 다시 실행한다. 수정 루프에서는 sbmig-parent 사전 시도가 한다
    # continue는 레시피 없이 진행하고, abort는 멈춘다. 설정한 버전도 저장소에 없으면 이 값과 상관없이 멈춘다
    unresolvable_asis_parent: Literal["align", "continue", "abort"] = "align"
    plugin_goal: str = "run"                            # run은 파일을 바꾸고 dryRun은 바꿀 내용만 확인한다
    # OpenRewrite는 settings의 미러를 Maven과 다르게 고른다. 그래서 OpenRewrite 호출에만 미러를 더한 복사본을 준다
    # 복사본을 만들 수 없거나 false면 원래 파일을 쓴다
    # `sbmig/rewrite/settings_translate.py` ("v2.1z-ci"); other Maven calls keep `build.settings_file`.
    translate_settings: bool = True


class BuildConfig(BaseModel):
    """Maven 빌드 설정을 담는 클래스.
    mvn 경로, settings.xml, 로컬 저장소, JDK 21 경로, 시간 제한, 테스트 생략, 소스 인코딩 후보, 버전 조회 저장소를 정한다."""
    mvn: str = "mvn"                                    # Maven 실행 파일. PATH에 없으면 전체 경로
    # 사내 Nexus를 가리키는 settings.xml. 지정하면 모든 Maven 호출에 -s로 넘긴다. None이면 Maven의 평소 settings를 쓴다
    settings_file: Optional[str] = None
    # settings_file을 넘길 때 Maven 설치본의 전역 settings도 -gs로 넘긴다. 프로젝트 .mvn/maven.config의 settings는 무시된다
    # false면 -s만 넘긴다. 그러면 프로젝트의 -gs 설정이 서버 설정과 합쳐진다
    ignore_project_settings: bool = True
    # 프로젝트의 .mvn 폴더(jvm.config, maven.config, extensions.xml)를 Maven이 읽지 않게 한다. MAVEN_BASEDIR에 빈 폴더를 준다
    # maven.config의 -D / -P만 명령줄로 옮긴다. 폴더 자체는 바꾸지 않고 결과물에 그대로 넣는다
    ignore_project_mvn: bool = True
    # 로컬 Maven 저장소(-Dmaven.repo.local). None이면 settings.xml의 값, 그것도 없으면 ~/.m2/repository
    local_repo: Optional[str] = None
    # JDK 21 설치 폴더. None이면 현재 환경의 JAVA_HOME을 쓴다. PC에 JDK가 여러 개 있으면 지정한다
    java_home: Optional[str] = None
    offline: bool = False                               # true면 Maven을 -o로 실행하고 Central도 조회하지 않는다
    extra_args: list[str] = Field(default_factory=lambda: ["-B", "-ntp", "-Dstyle.color=never"])
    # skip_test_compile이 꺼져 있을 때만 쓰인다. 테스트를 컴파일하되 실행하지 않는다(-DskipTests)
    skip_tests: bool = True
    # 테스트는 이관 범위 밖이다. 모든 빌드에 -Dmaven.test.skip=true를 붙여 테스트를 컴파일하지도 실행하지도 않는다
    # 보고서에는 tests-not-compiled 플래그와 담당 팀 후속 작업이 남는다
    skip_test_compile: bool = True
    # 아래 두 값은 skip_test_compile을 끈 경우에만 쓰인다. 테스트 컴파일 오류만 남으면 -Dmaven.test.skip=true로 다시 빌드한다
    # 테스트 실행 실패만 남으면 -DskipTests로 다시 빌드한다. 둘 다 마지막 수단이다
    allow_skip_test_compile_fallback: bool = True
    allow_skip_tests_fallback: bool = True              # 실패한 테스트는 tests-skipped 플래그와 후속 작업으로 남긴다
    timeout_minutes: int = 30                           # Maven 빌드 한 번의 시간 상한(분)
    # Maven을 실행할 때 더할 환경 변수(예: MAVEN_OPTS). 비밀 값은 넣지 않는다
    env: dict[str, str] = Field(default_factory=dict)
    # 소스 인코딩을 판별할 때 차례로 시도하는 후보. JDK 18부터 기본 인코딩이 UTF-8이라 MS949 / EUC-KR 소스가 깨진다
    # 재구성, 구조 검토, 스크립트 헬퍼가 이 순서로 판별한다. 다른 인코딩이 있으면 추가한다
    encoding_candidates: list[str] = Field(default_factory=lambda: ["utf-8", "cp949", "euc-kr", "shift_jis", "latin-1"])
    # 프로젝트 pom의 <repositories> / <pluginRepositories>는 그대로 둔다. 그 요청은 settings_file의 external:* 미러가 받는다
    # 오류 서명에서 라이브러리 코드로 볼 패키지 접두어. 프로젝트 고유 FQCN은 '*'로 가린다. 그래야 다른 프로젝트에서도
    # 같은 케이스가 된다. 사내 부모, 필수 / 선택 아티팩트의 groupId는 자동으로 들어간다
    external_package_prefixes: list[str] = Field(default_factory=list)
    # 사전 점검에서 `mvn -v`의 Java 메이저 버전이 이 값이 아니면 멈춘다(플래그 jdk-not-21). 0이면 점검하지 않는다
    require_java_major: int = 21
    # Nexus의 404가 로컬 저장소에 캐시되어(`was cached in the local repository`) 실패하면
    # 오류를 분류하기 전에 같은 빌드를 -U로 한 번 더 실행한다
    retry_negative_cache: bool = True
    # 실행 처음과 끝에 한 번씩 실행할 명령(argv 목록, 셸 아님). 별도 빌드 환경을 띄우고 내리는 범용 훅이다
    # setup이 실패하면 실행을 멈춘다. teardown이 실패하면 경고만 남긴다. 비워 두면 아무것도 하지 않는다
    # The hooks get SBMIG_RUN_ID / SBMIG_RUN_DIR / SBMIG_PROJECT_DIR; teardown runs after the report.
    setup_command: list[str] = Field(default_factory=list)
    teardown_command: list[str] = Field(default_factory=list)
    hook_timeout_s: int = 600                           # 위 두 명령 각각의 시간 상한(초)
    # 실제로 있는 버전을 sbmig가 직접 확인할 저장소 URL(maven-metadata.xml / pom). sbmig는 버전을 추측하지 않는다
    # 지정하면 가장 먼저 조회하고 settings에서 나온 저장소들을 이어서 조회한다. None이면 settings의 저장소만 쓴다
    # Settings set: mirrors by mirrorOf, active-profile repos, unmirrored pom repos, global settings, Central.
    repository_url: Optional[str] = None
    # repository_url이 settings 어디에도 없는 URL일 때 자격 증명을 가져올 <server> id
    # 같은 URL의 미러나 저장소가 settings에 있으면 그 자격 증명을 쓴다. 그때는 비워 둬도 된다
    repository_server_id: Optional[str] = None
    repository_timeout_seconds: int = 10                # 저장소 조회 HTTP 요청 하나의 시간 상한(초)
    # 저장소에 jar만 있고 pom이 없는 라이브러리 때문에 Maven이 멈추면, 로컬 저장소에 최소 pom을 만들고 다시 실행한다
    # jar는 받을 수 있고 pom은 어느 저장소에도 없는지 먼저 확인한다. 프로젝트 파일은 바꾸지 않는다
    pomless_stub: bool = True
    pomless_stub_max: int = 60                          # 한 실행에서 만드는 최소 pom의 상한
    # 필터링되는 리소스 아래에서 항상 바이너리로 취급할 회사 확장자(예: dmdb, 소문자에 점 없이)
    # 재구성이 스캔에서 찾은 확장자와 함께 <nonFilteredFileExtensions>에 넣는다. MalformedInputException을 막는다
    non_filtered_extensions: list[str] = Field(default_factory=list)
    # Windows에서 백신, IDE, 남은 java 프로세스가 target/ 파일을 잡아 빌드가 실패하면 기다렸다가 다시 빌드한다
    # 환경 문제를 LLM에 넘기지 않기 위해서다. 0이면 끈다. 패턴은 knowledge/config/build_env_errors.yml에 있다
    file_lock_retries: int = 3
    file_lock_retry_wait_s: float = 5.0

    @field_validator("non_filtered_extensions", mode="before")
    @classmethod
    def _non_filtered_list(cls, v: Any) -> Any:
        """문자열 하나(``dmdb`` / ``dmdb, xyz``)도 목록으로 바꾼다. 각 값은 소문자로 바꾸고 앞의 점을 뗀다."""
        if v is None:
            return []
        if isinstance(v, str):
            v = [x for x in re.split(r"[,\s]+", v) if x]
        if isinstance(v, (list, tuple)):
            return [str(x).strip().lower().lstrip(".") for x in v if str(x).strip().lstrip(".")]
        return v


class PhaseLoopConfig(BaseModel):
    """단계별 수정 루프 설정(빌드 goal, 최대 라운드, 켜고 끄기)을 담는 클래스. 지금은 verify 단계만 쓴다."""
    goal: str = "compile"
    max_iterations: int = 5
    enabled: bool = True


class LoopConfig(BaseModel):
    """수정 루프 설정을 담는 클래스.
    라운드 수, LLM 호출과 시간 한도, 정체와 진동 판단, 라운드당 케이스 수, 악화 시 되돌리기, 차단 정책, 실행 엔진(graph / inline)을 정한다."""
    # 중간 빌드가 없어서 jdk21 / spring / restructure 루프는 꺼져 있다. verify 루프가 모든 오류를 고친다
    # verify.max_iterations는 수정 루프 라운드(빌드 한 번) 수의 상한이다. 도달하면 마지막 빌드와 보고서로 넘어간다
    phases: dict[str, PhaseLoopConfig] = Field(default_factory=lambda: {
        "jdk21": PhaseLoopConfig(goal="compile", max_iterations=3, enabled=False),
        "spring": PhaseLoopConfig(goal="test-compile", max_iterations=6, enabled=False),
        "restructure": PhaseLoopConfig(goal="package", max_iterations=6, enabled=False),
        "verify": PhaseLoopConfig(goal="package", max_iterations=50),
    })
    # 프로젝트 하나의 LLM 호출 상한. 0은 무제한이다. 양수면 그 횟수에서 멈추고, -1이면 LLM을 호출하지 않는다
    # -1 is what a fleet sets once `max_llm_calls_total` is used up (stored scripts are still reused).
    max_llm_calls: int = 0
    # 실행 하나의 전체 시간 상한(분). 넘으면 남은 루프를 건너뛰지만 마지막 빌드는 실행한다
    max_total_minutes: int = 240
    # 같은 오류 묶음이 이 횟수만큼 그대로 반복되면 정체로 본다. 다음 시도부터 상위 모델(llm.escalation_model)을 쓴다
    # 오류는 위치(파일, 없으면 모듈)와 서명으로 비교한다
    stall_limit: int = 2
    # 한 라운드에서 처리하는 케이스 수 상한. 근본 원인으로 보이는 케이스부터 처리한다
    max_clusters_per_iteration: int = 8
    # 시도 뒤 오류 수가 시도 전 오류 수 × regression_ratio를 넘으면 악화로 보고 그 시도를 되돌린다
    revert_on_regression: bool = True
    regression_ratio: float = 1.5
    # 빌드 게이트라서 끄지 않는다. 어떤 경우에도 마지막에 `mvn clean package`를 한 번 더 실행한다
    always_final_verify: bool = True
    # 단계를 진행하는 방식. graph(회사 규칙)는 LangGraph로 진행한다. inline은 그래프 없이 차례로 실행한다
    # 이관 보고서는 같지만 inline은 취약점 조치로 이어가지 않는다. 그래프 쪽 문제를 가려낼 때만 inline을 쓴다
    engine: Literal["graph", "inline"] = "graph"
    # 오류 집합이 A와 B 사이를 오가는 진동을 볼 때 비교할 최근 빌드 수. 넓히면 진동을 더 빨리 알아챈다
    oscillation_window: int = 4
    # 진동이 이 횟수만큼 반복되면 관련 케이스를 차단한다. 더 오래 시도하려면 올린다
    oscillation_limit: int = 2
    # 다른 케이스가 고친 줄을 그대로 되돌려 같은 오류를 다시 낸 시도의 처리
    # - observe(기본값): 저널에 `trade_back`만 기록하고 시도 결과는 바꾸지 않는다. 두 시도가 모두 저장된 스크립트면
    #   둘 다 round_trip으로 표시한다. 그런 스크립트는 LLM 없이 재사용하지 않는다
    # - revert: 그 시도를 악화로 보고 되돌린다. oscillation_limit번 되풀이되면 되돌린 쪽 케이스를 차단한다
    # - off: 판단하지 않는다
    # revert counts per ordered pair of cases and keeps the side that fixed first, right or wrong.
    trade_back: Literal["observe", "revert", "off"] = "observe"
    # 차단 사유별로 허용할 LLM 스크립트 시도 수. 0이면 LLM을 호출하지 않고 바로 차단해 인수인계로 넘긴다
    # 저장된 스크립트는 이 값과 상관없이 재사용한다. 코드로 고칠 수 없는 환경 문제는 0이다
    blocked_policy: dict[str, int] = Field(default_factory=lambda: {
        "external_javax_api": 2, "external_api_incompatible": 2, "artifact_not_found": 2, "parent_not_found": 0,
        "plugin_incompatible": 2, "unsupported_source": 0, "encoding": 0,
        "reactor_duplicate": 0,                      # 같은 모듈이 reactor에 두 번 들어간 pom 구조 문제
        "file_lock": 0,                              # 다른 프로세스가 target/을 잡고 있는 환경 문제
        "needs_owner": 0,                            # LLM이 프로젝트 밖의 결정이 필요하다고 답한 경우
        "repository_unreachable": 0,                 # 저장소가 응답하지 않은 전송 실패
    })
    # pom을 고친 시도 뒤 이 종류의 오류가 새로 나오면 개수와 상관없이 되돌린다
    # LLM이 만든 의존성 하나가 모든 컴파일 오류를 가릴 수 있기 때문이다
    # Exception: every earlier target gone and all new structural errors of a later stage = uncovered
    # (hidden by the fixed pom problem), not caused by the attempt.
    structural_regression_kinds: list[str] = Field(default_factory=lambda: [
        "pom_invalid", "parent_resolution", "plugin_resolution", "dependency_resolution"])

    @field_validator("trade_back", mode="before")
    @classmethod
    def _trade_back_word(cls, v: Any) -> Any:
        """YAML이 불리언으로 읽은 off / on도 받는다. false는 off, true는 revert로 바꾼다. 문자열은 소문자로 바꾼다."""
        if isinstance(v, bool):
            return "revert" if v else "off"
        if isinstance(v, str):
            return v.strip().lower()
        return v


BUILTIN_LLM_PROVIDERS = ("company", "openai_compatible", "anthropic", "fake")


class ModelPrice(BaseModel):
    """모델 하나의 1M 토큰당 가격(`llm.pricing.<model>`)을 담는 클래스. 통화는 llm.pricing_currency로 정한다."""
    input_per_mtok: float
    output_per_mtok: float
    # 참고용 값이다. 게이트웨이 응답에서는 캐시 적중 여부를 알 수 없다
    cached_input_per_mtok: Optional[float] = None


class LLMConfig(BaseModel):
    """LLM 연결 설정을 담는 클래스. 키는 환경 변수(`api_key_env`, 기본값 LLM_API_KEY)에서만 읽는다.
    provider는 company(기본값), openai_compatible, anthropic, fake(재생), plugin 중 하나다. 등록된 어댑터 이름도 된다.

    Any other provider name is looked up in the `sbmig.llm_adapters` entry-point group."""
    provider: str = "company"
    # provider가 plugin일 때 연결할 회사 어댑터("패키지.모듈:이름"). 내장 provider에서는 읽지 않는다
    # attr = an LLMAdapterLike / LLMClientLike class or a factory `fn(options, cfg) -> adapter | client`.
    plugin: Optional[str] = None
    # 어댑터 팩토리에 그대로 넘기는 옵션. 키는 넣지 않는다(api_key_env의 키가 자동으로 더해진다)
    plugin_options: dict[str, Any] = Field(default_factory=dict)
    base_url: str = "${LLM_BASE_URL}"                    # 게이트웨이 주소. .env의 LLM_BASE_URL에서 읽는다
    api_key_env: str = "LLM_API_KEY"                     # API 키가 들어 있는 환경 변수의 이름(키 자체가 아님)
    model: str = "${LLM_MODEL}"                          # 수정 스크립트와 구조 검토에 쓰는 모델
    # 상위 모델. 진전이 없거나(loop.stall_limit) 한 케이스의 시도가 쌓이면 쓴다. 비우면 model을 계속 쓴다
    escalation_model: Optional[str] = None
    # 계획 검토(plan.advisor: llm)에 쓰는 저렴한 모델. 비우면 model을 쓴다
    recipe_model: Optional[str] = None

    @field_validator("model", "escalation_model", "recipe_model", mode="before")
    @classmethod
    def _clean_model_name(cls, v: Any, info: ValidationInfo) -> Any:
        """모델 이름 끝의 `# 주석`과 공백을 뗀다. 주석뿐인 값은 비운다.
        .env의 주석이 모델 이름에 섞이면 게이트웨이가 실행 중에 HTTP 400을 반환한다.

        A value that is only a comment (`LLM_RECIPE_MODEL= # x`, `set X=# ...` on Windows) becomes unset;
        `${...}` placeholders are left whole."""
        if not isinstance(v, str):
            return v
        cleaned = v.split("#", 1)[0].strip() if not v.lstrip().startswith("${") else v.strip()
        if cleaned != v.strip():
            log.warning("llm.%s=%r is not a plain model name: using %r", info.field_name, v, cleaned or None)
        if not cleaned:
            return None if info.field_name != "model" else ""
        return cleaned
    temperature: float = 0.0                 # 답변의 무작위성. 재현성을 위해 0을 권장한다
    max_output_tokens: int = 8000            # 응답 하나의 최대 토큰. 긴 스크립트가 잘리면 늘린다
    max_context_chars: int = 60000           # 요청 하나에 담는 맥락(소스, pom 발췌 등)의 문자 수 상한
    timeout_seconds: int = 180               # 요청 하나를 기다리는 시간(초)
    max_retries: int = 3                     # 전송 오류(연결 끊김, 5xx 등) 때 같은 요청을 다시 보내는 횟수
    # 게이트웨이가 바쁘다고 응답하면(429, 또는 Retry-After가 붙은 502 / 503 / 504) 기다렸다가 다시 보낸다
    # 기다린 시간의 합이 이 값(초)에 이를 때까지다. max_retries와 따로 센다. 0이면 바쁨도 max_retries로만 센다
    # Capped backoff + jitter; when used up the call falls through to the transport-failure rules (cooldown).
    busy_wait_seconds: int = 600
    # 토큰을 새로 받아 보냈는데도 401이면 잠깐 기다렸다가 다시 받아 보내는 추가 횟수
    # 다른 파드가 같은 앱 토큰을 교환해 기존 토큰이 무효가 됐을 수 있다. 0이면 401 뒤 한 번만 새로 받는다
    auth_retries: int = 2
    # 전송이나 설정 실패가 두 번 연속 나면 실행당 한 번 이 시간(초)만큼 기다렸다가 다시 시도한다
    # 또 연달아 실패하면 그 실행 동안 LLM을 쓸 수 없는 것으로 본다. 0이면 바로 사용 불가로 본다
    cooldown_seconds: int = 60
    # 케이스 하나에서 LLM이 파일을 더 보여 달라고 요청할 수 있는 횟수
    max_context_rounds: int = 2
    # 게이트웨이 요청에 더할 HTTP 헤더. yml에 기록되므로 키나 토큰은 넣지 않는다
    extra_headers: dict[str, str] = Field(default_factory=dict)
    prompt_version: str = "v1"               # 프롬프트 묶음 버전(sbmig/fix/prompts 파일 이름 끝)
    # 케이스 대화 모드. 세션이 앞선 대화를 기억하는 agent 게이트웨이에서만 의미가 있다
    # true면 케이스의 첫 요청이 전체 프롬프트로 세션을 열고, 같은 케이스의 보강은 달라진 내용만 보낸다
    # 열린 세션은 모델을 바꿀 수 없다. 그래서 상위 모델로 넘어가거나 돌아오는 요청은 새 세션을 연다
    # Delta = what the last script did, the errors now, new files; a fresh `new` / another case = new session.
    case_thread: bool = False
    # 이어 보낸 요청이 거절(4xx)되거나 세션 id가 오지 않으면 새 세션에서 전체 프롬프트로 다시 보낸다
    thread_fallback_full: bool = True
    # 요청과 응답을 기록할 폴더(키는 기록하지 않는다). 감사와 오프라인 재생에 쓴다. 비우면 기록하지 않는다
    record_dir: Optional[str] = None
    # company 공급자는 requests.post 한 번으로 호출한다(Bearer LLM_API_KEY, json = model + messages + payload_extra)
    # 이 단순한 형태를 유지하고 LangChain이나 httpx 옵션을 추가하지 않는다. sbmig/fix/company.py 참고
    # legacy 게이트웨이 요청에 그대로 추가하는 최상위 필드(회사 게이트웨이가 요구하는 값)
    payload_extra: dict[str, Any] = Field(default_factory=dict)
    # 회사 게이트웨이의 요청 형식. legacy는 model + messages + payload_extra를 보낸다
    # agent는 사내 GPT 에이전트 게이트웨이 형식이다. role 이름(build_error_fixer 등)을 게이트웨이가 실제 모델로 바꾼다
    # agent payload: {model: asset_id, messages, stream: false, async: false, params: {role: <model name>},
    # trace_id, max_tokens, temperature, **agent_extra}; session_id only to continue one; no payload_extra.
    gateway: str = "legacy"
    asset_id: Optional[str] = None                           # agent 게이트웨이의 최상위 model 값(LLM_ASSET_ID)
    # 토큰 교환 주소(LLM_TOKEN_URL). 지정하면 앱 토큰을 이 주소에 POST해서 받은 접근 토큰을 쓴다. 비우면 키를 그대로 쓴다
    token_url: Optional[str] = None
    # 앱 토큰이 들어 있는 환경 변수의 이름. 이 변수가 없으면 api_key_env의 값을 앱 토큰으로 쓴다
    app_token_env: str = "LLM_APP_TOKEN"
    # 접근 토큰이 만료되기 이 시간(초) 전에 새로 받는다
    token_refresh_margin_s: int = 300
    agent_extra: dict[str, Any] = Field(default_factory=dict)  # agent 게이트웨이 요청에 그대로 더하는 고정 필드
    # 응답 JSON에서 답변 텍스트가 있는 점 경로(예: data.result.content). None이면 자동으로 찾는다
    response_path: Optional[str] = None
    # 보고서 비용 표에 쓰는 모델별 1M 토큰당 가격. 회사 가격은 yml에만 둔다
    # 요청한 이름(agent에서는 role)으로 먼저 찾고, 없으면 게이트웨이가 알려 준 실제 모델 이름으로 찾는다
    # Key match: exact, then the longest key that is a prefix of / contained in the name (case-insensitive);
    # an unmatched model is listed as unpriced.
    pricing: dict[str, ModelPrice] = Field(default_factory=dict)
    pricing_currency: str = "USD"
    pricing_note: str = ""                   # 비용 표 아래에 표시하는 가격 출처 문장


class ScriptConfig(BaseModel):
    """수정 스크립트 실행 설정(sbmig.script)을 담는 클래스.
    LLM이 쓴 `def fix(p)`를 정적 검사한 뒤 별도의 `python -I` 프로세스에서 헬퍼 API `p`로 실행한다.
    `p`는 프로젝트 폴더만 다룬다. 실수를 막는 장치이고 악의적인 코드를 막는 샌드박스는 아니다.

    Static check: allowed imports only; no open / exec / eval / os / subprocess / socket / dunder access."""
    timeout_seconds: int = 60                # 스크립트 한 번의 시간 상한(초). 넘으면 프로세스 트리 전체를 종료한다
    # 스크립트를 실행할 Python. None이면 sbmig를 실행하는 Python을 쓴다
    # 다른 Python에는 pydantic과 lxml이 있어야 한다. 없으면 ModuleNotFoundError로 실패한다
    python: Optional[str] = None
    # 스크립트가 import할 수 있는 모듈을 기본 허용 목록에 더한다
    extra_allowed_imports: list[str] = Field(default_factory=list)
    max_code_chars: int = 30000              # 이보다 긴 스크립트는 실행하지 않고 거절한다
    log_lines: int = 200                     # 스크립트가 p.log로 남긴 줄 중 보관할 줄 수
    # 한 번에 이보다 많은 파일을 바꾸면 경고 메모를 남긴다. 바뀐 내용은 그대로 보관한다
    max_files_changed_warn: int = 40
    # 케이스마다 저장된 스크립트 재사용(LLM 없음) -> 보강 -> 신규 순서로 시도한다
    # 이전에 처리한 케이스면 KB의 가장 좋은 스크립트를 먼저 실행한다. LLM 호출을 줄이는 핵심 기능이라 끄지 않는다
    reuse: bool = True
    # 아래 케이스별 횟수는 한 실행에서 오류가 난 위치(파일, 없으면 모듈)별로 따로 센다
    # 위치를 모두 합친 LLM 호출은 케이스마다 max_llm_tries_per_case의 2배까지다
    reuse_versions: int = 1                  # LLM을 호출하기 전에 실행해 볼 저장 버전 수
    # 보강 횟수. 보강은 이 실행의 마지막 스크립트를 LLM이 고치고 늘리는 것이다. 다 쓰면 다음 요청은 신규다
    # 0이면 보강하지 않는다
    max_augments_per_case: int = 15
    # 케이스 하나의 LLM 호출(신규 + 보강) 상한. 도달하면 차단(repeated_failure)하고 인수인계로 넘긴다
    # 한도는 넉넉하게 잡았다. 회사 실행에서 실제로 필요한 횟수를 보고 줄여 간다
    max_llm_tries_per_case: int = 20
    # LLM이 편집도 스크립트도 없이 프로젝트 밖의 문제라고 답한 횟수가 이 값에 이르면 차단(needs_owner)한다
    # 같은 파일의 형제 케이스도 LLM을 호출하지 않고 함께 차단한다
    # A decline that contradicts known facts (a jakarta build exists, a bundled jar provides the package, a
    # `replacement: true` note matches) is not counted: re-asked with the fact, its edits / script applied.
    max_declines_per_case: int = 1
    # LLM이 스크립트 대신 직접 편집(<edits>)으로 고치는 케이스의 시도 횟수. 넘으면 차단(repeated_failure)한다
    # EOL 라이브러리를 프로젝트 코드로 대체하는 일처럼 재사용할 수 없는 수정이다
    max_edit_tries_per_case: int = 4
    # 스크립트가 성공하면 곧바로 한 번 더 실행해 본다. 또 바뀌면 non_idempotent로 표시하고 재사용하지 않는다
    idempotency_check: bool = True


class ErrorsConfig(BaseModel):
    """오류 목록 설정을 담는 클래스.
    실행 디렉토리에 errors.jsonl과 errors.md를 쓴다. 실행이 끝나면 KB의 errors.jsonl에도 추가한다.
    diff와 원래 오류의 길이 상한, 수정 전후 코드 조각과 스크립트 실행 이력의 크기를 정한다."""
    diff_chars: int = 6000                   # 시도 하나의 diff를 담는 문자 수 상한(0이면 상한 없음, 전체는 changes.patch)
    raw_chars: int = 600                     # 원래 오류 블록을 담는 문자 수 상한
    md_view: bool = True                     # 사람이 읽는 errors.md와 KB의 cases.md를 쓸지 여부
    # 오류마다 읽기 쉬운 JSON을 KB의 cases/<case_id>/errors/에 남긴다. 기계용 기록은 errors.jsonl이다
    json_files: bool = True
    snippet_lines: int = 0                   # 바뀐 곳마다 수정 전 / 수정 후 코드를 남길 줄 수. 0이면 전부
    script_runs: int = 30                    # 저장된 스크립트 버전마다 남기는 실행 이력 수(최신이 마지막)


class LibrariesConfig(BaseModel):
    """의존성 버전 정책 설정을 담는 클래스. LLM은 스스로 메이저 버전을 올리거나 라이브러리를 바꾸지 않는다.
    같은 메이저의 jakarta 릴리스를 먼저 쓴다. 없을 때만 더 높은 메이저를 쓰고 운영자에게 확인을 안내한다.
    기본 표(knowledge/libraries/jakarta_libraries.yml)는 `<kb>/libraries.yml`에 항목을 더해 늘린다. 다시 배포하지 않아도 된다."""
    # 저장소에 어떤 릴리스가 있는지 조회해 프롬프트에 넣는다. false면 표와 정책만 넣는다
    lookup: bool = True
    max_pom_checks: int = 6                  # jakarta 빌드인지 확인하려고 라이브러리마다 받아 보는 후보 pom 수
    include_prerelease: bool = False         # 마일스톤, RC, 알파 버전도 후보로 보여 줄지 여부
    max_artifacts_per_case: int = 3          # 케이스 하나에서 조회할 라이브러리 수
    # 사람이 추가하거나 승인한 라이브러리 표. kb.dir 기준 파일 이름이다. 같은 항목이면 이 표가 우선한다
    company_file: str = "libraries.yml"
    # 메이저를 올린 응답에 확인 안내(<verify>)가 없으면 안내만 받으려고 LLM을 한 번 더 호출한다
    # false거나 LLM이 없으면 sbmig가 diff로 안내를 만든다
    verify_guide_llm: bool = True
    # javax를 쓰는 라이브러리의 jakarta 빌드를 sbmig가 저장소에서 직접 찾는다. 확인되면 LLM보다 먼저 선언을 바꾼다
    # (사전 시도 sbmig-jakarta). 버전 한정어, classifier, pom의 jakarta 선언으로 확인한다. 그래도 모르면 jar의 클래스 참조를 센다
    # false면 릴리스 목록만 보여 준다. 확인, 형제 이름 탐색, 사전 시도, 거절 응답 반려는 하지 않는다
    # Search order: the same coordinates' newer releases, Nexus 3 search by name / group stem, sibling names.
    discover_jakarta_builds: bool = True
    # 저장소 URL이 `…/repository/<이름>/` 형태면 Nexus 3 검색 API(`service/rest/v1/search`)를 쓴다
    nexus_search: bool = True
    # 라이브러리마다 이름 규칙으로 조회해 보는 후보 수(-jakarta를 붙인 이름, 끝 숫자를 올린 이름 등)
    max_jakarta_probes: int = 12
    max_jar_scans: int = 2                   # pom으로 판단할 수 없을 때 라이브러리마다 받아 볼 jar 수
    max_jar_scan_mb: int = 20                # 이보다 큰 jar는 받지 않는다(그 후보는 미확인으로 남는다)


class ReviewConfig(BaseModel):
    """구조 검토 설정(restructure.review)을 담는 클래스.
    재구성 뒤 첫 빌드 전에 LLM이 pom, 의존성, 모듈을 회사 규칙에 맞는지 검토한다.
    critical은 응답의 편집으로 바로 고치고 커밋한다. 그래서 되돌릴 수 있다. warning은 보고서와 오류 목록에 남긴다."""
    enabled: bool = True                     # false면 구조 검토를 건너뛰고 재구성 바로 뒤에 빌드한다
    fix_critical: bool = True                # critical 지적을 바로 고칠지 여부. false면 보고만 한다
    # 프롬프트에 넣는 pom 문자 수 합계 상한. 넘는 pom은 앞뒤만 남기고 자른다
    max_pom_chars: int = 60000
    max_poms: int = 40                       # 프롬프트에 넣는 pom 개수 상한
    # 읽지 않는 키다. 검토 결과는 오류 목록에 기록하고 KB의 reviews.jsonl에는 쓰지 않는다
    record: bool = True
    # 검토는 기본으로 편집(edits)만 요구한다. 그래서 시스템 프롬프트에 스크립트 절과 헬퍼 API 표가 없다
    # true면 <script>도 요구한다
    ask_script: bool = False
    # 검토 편집을 적용한 뒤 재구성의 불변 규칙을 다시 검사한다(체인이 정의한 속성, 관리 아티팩트의 버전, 보호 키, 낯선 <parent>)
    # 어긴 편집은 되돌리고, 이유를 붙여 한 번 다시 요청한다
    invariant_check: bool = True
    # 검토에 보낸 user 프롬프트를 <run dir>/logs/review-input.md로 남긴다(문제 확인용)
    save_input: bool = True


class FixConfig(BaseModel):
    """수정기 전체를 사내 코딩 에이전트로 바꿀 때 쓰는 설정 클래스(어댑터 2단계).
    plugin은 "pkg.module:attr"(FixerLike 클래스나 팩토리)이다. propose_script 계약을 지켜야 한다.

    Factory: `fn(options: dict, cfg: Config, llm: LLMClientLike | None) -> FixerLike`. The replaced
    fixer still runs inside the script ladder and the KB."""
    plugin: Optional[str] = None
    plugin_options: dict[str, Any] = Field(default_factory=dict)   # 팩토리에 그대로 넘기는 옵션(비밀 값은 넣지 않는다)
    # 수정은 항상 스크립트로 한다. 그래서 수정 모드 스위치(`mode`, `edits_max_asks_per_case`)는 없다


class KBConfig(BaseModel):
    """파일 KB 설정을 담는 클래스.
    위치(kb.dir, 기본값 kb-v2/), 비밀 값 마스킹 규칙, 프로세스 간 잠금 대기 시간을 정한다.
    테스트와 데모는 out/ 아래의 KB만 쓴다."""
    # KB 폴더. 상대 경로는 저장소 루트 기준이다. 운영에서는 체크아웃 밖의 절대 경로를 지정한다
    dir: str = "kb-v2"
    # KB에 쓰기 전에 비밀 값을 마스킹하는 추가 규칙(회사 패턴). 기본 규칙에 더해진다
    redact_file: str = "knowledge/kb/redact.yml"
    # 여러 프로세스가 같은 KB를 쓸 때 kb/.lock을 기다리는 시간(초). 넘으면 그 기록만 포기하고 로그에 남긴다
    lock_timeout_seconds: int = 300


class PlanConfig(BaseModel):
    """계획 설정을 담는 클래스.
    계획은 규칙으로 세워서 늘 같은 결과가 나온다. LLM 검토는 선택이고 기본으로 꺼져 있다.
    레시피 id는 catalog_file 한 곳에서만 정의한다."""
    # none(기본값)은 검토하지 않는다. llm이면 llm.recipe_model이 변환 단계에 레시피를 더할 수 있다. 더할 수 있는 레시피는 catalog.yml에 있는 것뿐이다
    advisor: Literal["none", "llm"] = "none"
    # 레시피 목록 파일(레시피 id의 유일한 출처). 없거나 읽을 수 없으면 경고를 남기고 내장 최소 목록을 쓴다
    catalog_file: str = "knowledge/recipes/catalog.yml"
    # OpenRewrite 뒤, 재구성 전의 `mvn clean compile`은 기본으로 실행하지 않는다. 반쯤 옮겨진 프로젝트의 오류만 쌓인다
    # true면 한 번 컴파일해서 오류 목록을 미리 채운다. 고치지는 않는다
    compile_after_rewrite: bool = False


class FleetConfig(BaseModel):
    """`sbmig fleet` 설정을 담는 클래스.
    에이전트 하나가 프로젝트 N개를 반드시 순서대로 처리한다. 그 사이에 KB가 채워진다.
    인프라 장애 대기, 디스크 여유 공간, 끝난 프로젝트 정리 방식도 여기서 정한다."""
    # 처리 순서. manifest는 목록 순서대로, simple-first는 순수 Java, 서블릿, Spring Framework, Boot, MSA 순으로 처리한다
    order: Literal["manifest", "simple-first"] = "manifest"
    stop_on_abort: bool = False              # true면 프로젝트 하나가 중단(exit 3)되는 즉시 전체를 멈춘다
    retry_failed: bool = False               # 지난번에 미완료나 중단으로 끝난 프로젝트도 다시 처리할지 여부
    max_projects: int = 0                    # 처리할 프로젝트 수의 상한. 0이면 전부
    max_llm_calls_total: int = 0             # 일괄 실행 전체의 LLM 호출 상한. 0이면 제한 없음
    reindex_every: int = 10                  # 프로젝트 N개마다 `kb reindex`를 실행한다. 0이면 하지 않는다
    # 첫 프로젝트 전에 Java / Maven / settings / LLM / 디스크를 한 번 점검한다
    preflight: bool = True
    infra_failure_limit: int = 3             # 인프라 문제로 연달아 이만큼 실패하면 기다렸다가 다시 점검한다
    # 다시 점검하기 전에 기다리는 시간(분). 0이면 바로 멈춘다
    infra_retry_minutes: int = 15
    # 다시 점검하는 최대 횟수. 넘으면 상태를 저장하고 멈춘다. `fleet resume`으로 이어 간다
    infra_max_waits: int = 4
    # 프로젝트가 끝난 뒤 디스크를 비우는 방식. build-output은 실행 복사본의 target/만 지운다
    # workspace는 프로젝트 복사본까지 지운다(result.zip, 보고서, patch는 남는다)
    cleanup: Literal["none", "build-output", "workspace"] = "build-output"
    # 프로젝트를 시작하기 전 출력 드라이브에 필요한 여유 공간(GB). 0이면 점검하지 않는다
    min_free_gb: float = 5.0
    summary_formats: list[str] = Field(default_factory=lambda: ["md", "csv", "json"])


class ReportConfig(BaseModel):
    """보고서 설정을 담는 클래스.
    형식(md / json), result.zip, diff, report.md 언어, 최종 빌드 뒤 javax API jar 조사를 정한다.
    report.md는 ko를 고를 수 있다. json, 콘솔, 로그는 영어 그대로다."""
    formats: list[str] = Field(default_factory=lambda: ["md", "json"])
    zip_result: bool = True                  # 바뀐 프로젝트를 result.zip으로 묶어 남긴다
    include_diff: bool = True                # as-is 대비 전체 변경을 changes.patch로 남긴다
    # 웹 화면 「코드 변경」에 쓰는 <run dir>/codediff/를 남긴다. 최종 파일마다 원래 위치의 as-is 파일과 비교한다
    code_diff: bool = True
    code_diff_context: int = Field(default=10, ge=0, le=50)   # 바뀐 줄 위아래로 함께 남기는 줄 수
    # report.md 언어. ko는 요청 담당자용 안내서(회사 설정), en은 진단용 구성이다
    language: Literal["en", "ko"] = "en"
    # 최종 빌드 뒤 모듈마다 `mvn dependency:tree`로 classpath에 남은 javax API jar를 찾아 보고서에 기록한다
    # 그 jar를 가져오는 직접 의존성도 함께 기록한다. 끄면 그 섹션이 빠지고 시간이 줄어든다
    # Also fills BlockedItem.via.
    javax_api_inventory: bool = True


class AdvisoryConfig(BaseModel):
    """라이브러리 권고 설정(EOL, 관리되지 않는 라이브러리)을 담는 클래스.
    실행을 막지 않고 수정 프롬프트의 힌트와 인수인계 섹션에만 쓴다."""
    enabled: bool = True                     # false면 권고를 찾지 않는다
    file: str = "knowledge/libraries/advisories.yml"      # 기본 권고 목록(절대 경로가 아니면 저장소 루트 기준)
    # 회사가 추가하거나 덮어쓰는 목록. 형식은 같다. id로 합치고 뒤 파일이 우선한다
    extra_files: list[str] = Field(default_factory=list)
    # 권고를 id별로 끌 수 있다(off). guide면 그대로 쓴다
    policy_overrides: dict[str, Literal["guide", "off"]] = Field(default_factory=dict)
    # 부모를 바꾼 뒤 관리되지 않아 as-is 버전을 그대로 쓰는 의존성을 인수인계에 기록한다
    report_carried_over: bool = True
    max_from_here: int = 40                               # 인수인계 "이제부터 할 일" 목록의 최대 항목 수


class IntakeConfig(BaseModel):
    """접수 설정을 담는 클래스.
    Ant 프로젝트(pom.xml 없이 ``build.xml``만 있음)는 ``ant_message``를 남기고 종료한다(exit 3).
    - ``ant_to_maven: true``면 ``sbmig.analyze.ant.synthesize_pom``으로 pom.xml을 만들어 접수한다.
      코드는 남겨 두었다. 이 스위치만 켜면 된다
    - jar 좌표는 jar 안 pom.properties -> 이름 표(jar_names.yml + <kb>/jar_names.yml) -> sha1 저장소 조회 순으로
      찾는다. 못 찾으면 ``unknown_jar`` 정책을 따른다
    - pom.xml도 build.xml도 없는 프로젝트는 거부한다
    """
    ant_to_maven: bool = False                              # true면 Ant 프로젝트에 pom.xml을 만들어 진행한다
    # 올린 ZIP을 풀 때의 한도(0이면 한도 없음). 전체 크기, 파일 하나, 풀린 크기 / ZIP 크기의 배수다
    # 넘으면 아무것도 쓰지 않고 접수에서 끝낸다(exit 3)
    max_unpacked_mb: int = Field(default=4096, ge=0)
    max_entry_mb: int = Field(default=1024, ge=0)
    max_compression_ratio: int = Field(default=200, ge=0)
    ant_message: str = "Maven 변환 후 신청 부탁드립니다."    # Ant 프로젝트를 거부할 때 신청자에게 보여 주는 문구
    # Ant 프로젝트에 만드는 pom.xml의 groupId(플레이스홀더). 회사가 정한 값이 있으면 회사 yml에서 덮어쓴다
    synth_group_id: str = "com.company.migrated"
    synth_version: str = "1.0.0-SNAPSHOT"                   # build.xml에 버전이 없을 때 만드는 pom의 버전
    # 좌표를 끝내 찾지 못한 jar의 처리. system은 원래 경로로 system scope 선언을 한다(빌드는 통과한다). skip은 선언하지 않는다
    unknown_jar: Literal["system", "skip"] = "system"
    # 프로젝트 안에 있지만 어떤 pom도 선언하지 않은 jar(WEB-INF/lib 등)를 분석 단계에서 찾아 기록한다
    bundled_jars: bool = True
    # 아직 좌표를 모르는 jar를 sha1로 저장소에서 조회한다. 네트워크가 막힌 곳에서만 끈다
    jar_lookup: bool = True
    jar_names_file: str = "knowledge/libraries/jar_names.yml"   # 기본 jar 이름과 좌표 대응표
    # 회사가 추가하는 같은 형식의 표. kb.dir 기준이다. 기본 표의 같은 항목을 덮어쓴다
    company_jar_names_file: str = "jar_names.yml"
    # 배포 전송용 <build><extensions>(wagon-*)를 첫 Maven 호출 전에 지운다
    # Maven은 어떤 goal보다 먼저 확장을 받는다. 그래서 받지 못하는 확장 하나가 OpenRewrite까지 막는다
    strip_deploy_extensions: bool = True
    build_extensions_file: str = "knowledge/libraries/build_extensions.yml"   # 배포 전송용 확장 목록
    # build.xml의 source / target이 이보다 낮으면 올린다. JDK 21은 1.8 미만을 컴파일하지 못한다
    min_java_source: str = "1.8"
    # pom.properties나 패키지를 찾으려고 열어 보는 jar 크기의 상한(바이트). 이보다 큰 jar는 열지 않는다
    max_jar_bytes_scan: int = 64 * 1024 * 1024
    # Oracle ADF(JDeveloper) 프로젝트는 변환하지 않는다. 네 가지 근거 중 둘 이상이면 ADF 프로젝트로 본다
    # stop(기본값)은 분석 직후 "ADF 프로젝트라 변환이 불가능합니다"로 끝낸다(exit 3, Maven과 LLM 호출 없음)
    # continue는 보고만 하고 계속 진행한다(권장하지 않음)
    adf_policy: Literal["stop", "continue"] = "stop"


# 프레임워크 아티팩트 이름 끝에 붙는 역할 단어. 화면에 보일 이름을 만들 때 잘라 낸다
_FRAMEWORK_ROLE_WORDS = frozenset({"core", "framework", "fw", "starter", "parent", "bom", "boot", "common", "base", "api"})


class FrameworkConfig(BaseModel):
    """사내 프레임워크를 쓰는 프로젝트를 찾아 이관 범위를 줄이는 설정 클래스.
    ``artifacts``의 아티팩트가 의존성에 있으면 JDK와 jakarta만 올리고 빌드한다. 재구성과 구조 검토는 하지 않는다.
    부모 pom과 프레임워크 버전은 바꾸지 않는다. 목록이 비어 있으면 검사하지 않는다."""
    # 프레임워크 아티팩트 목록. "artifactId"나 "groupId:artifactId"로 쓰고 *를 쓸 수 있다
    # 회사 좌표이므로 값은 회사 파일(config/artifacts.yml)에만 둔다
    artifacts: list[str] = Field(default_factory=list)
    # jdk-jakarta는 JDK와 jakarta만 올리고 빌드한 뒤 취약점 조치로 넘어간다. full은 표시만 하고 전체 이관을 한다
    scope: Literal["jdk-jakarta", "full"] = "jdk-jakarta"
    # 다른 라이브러리가 끌고 온 경우의 처리. apply는 직접 선언과 같게 본다. note는 알림만 하고 전체 이관을 한다
    # off는 검사하지 않는다(Maven 실행 한 번을 아낀다)
    transitive: Literal["apply", "note", "off"] = "apply"
    # jdk-jakarta 범위에서 실행할 레시피 묶음 이름(openrewrite.recipes의 이름)
    # Servlet 6에서 없어진 API까지 정리하려면 servlet_jakarta_10을 더한다
    recipe_sets: list[str] = Field(default_factory=lambda: ["jdk21", "jakarta"])
    # 화면과 보고서에 보여 줄 프레임워크 이름. 비우면 아티팩트 이름에서 만든다. 회사 이름이므로 회사 파일에 둔다
    name: str = ""
    # false면 변환 뒤 빌드를 한 번만 하고 남은 오류를 알린다. 사전 시도, 저장된 스크립트, LLM은 실행하지 않는다
    # true면 다른 프로젝트처럼 빌드 오류를 고친다(부모 pom과 프레임워크 버전은 건드리지 않는다)
    fix_loop: bool = False

    @field_validator("recipe_sets")
    @classmethod
    def _known_recipe_sets(cls, v: list[str]) -> list[str]:
        """``recipe_sets``의 이름이 ``openrewrite.recipes``의 세트 이름인지 확인한다."""
        allowed = [n for n, f in RecipeSetConfig.model_fields.items() if n != "extra"]
        out: list[str] = []
        for name in v or []:
            n = str(name or "").strip()
            if n not in allowed:
                raise ValueError(f"framework.recipe_sets: unknown recipe set '{name}' (allowed: {', '.join(allowed)})")
            if n not in out:
                out.append(n)
        return out

    def patterns(self) -> list[str]:
        """빈 값을 뺀 패턴 목록을 반환한다."""
        return [p.strip() for p in self.artifacts if isinstance(p, str) and p.strip()]

    def display_name(self, found: Iterable[str] = ()) -> str:
        """화면과 보고서에 쓸 프레임워크 이름을 반환한다. ``name``이 있으면 그 값을 쓴다.
        없으면 아티팩트 이름에서 만든다. 찾은 아티팩트(``found``)를 먼저 보고, 설정한 패턴을 그다음에 본다.
        만들 수 없으면 빈 문자열을 반환한다.
        - glob이 든 패턴은 아티팩트 하나를 가리키지 않아서 건너뛴다
        - 끝의 역할 단어(`core`, `starter` 등)를 떼고 첫 글자를 대문자로 바꾼다. 예: `example-fw-core`는 `Example`

        `found` items are `g:a[:v]` or a bare artifactId. On `""` callers say "the in-house framework"."""
        if self.name.strip():
            return self.name.strip()
        for raw in [*found, *self.patterns()]:
            parts = str(raw or "").strip().split(":")
            art = parts[1] if len(parts) >= 2 else parts[0]
            if not art or any(ch in art for ch in "*?[]"):
                continue
            tokens = [t for t in re.split(r"[-_.]", art) if t]
            while len(tokens) > 1 and tokens[-1].lower() in _FRAMEWORK_ROLE_WORDS:
                tokens.pop()
            if tokens:
                s = "-".join(tokens)
                return s[:1].upper() + s[1:]
        return ""

    @property
    def enabled(self) -> bool:
        """프레임워크 검사가 켜져 있으면 True를 반환한다. 패턴이 하나라도 있으면 켜져 있다."""
        return bool(self.patterns())

    def matches(self, group_id: Optional[str], artifact_id: Optional[str]) -> bool:
        """g:a가 프레임워크 아티팩트 패턴에 맞는지 확인한다.
        `:`가 없는 패턴은 artifactId와 비교하고, 있는 패턴은 `groupId:artifactId`와 비교한다. 대소문자를 구분한다."""
        a = (artifact_id or "").strip()
        if not a:
            return False
        key = f"{(group_id or '').strip()}:{a}"
        for p in self.patterns():
            if fnmatch.fnmatchcase(key, p) if ":" in p else fnmatch.fnmatchcase(a, p):
                return True
        return False

    def matches_key(self, key: Optional[str]) -> bool:
        """``g:a`` 또는 ``g:a:...`` 문자열이 프레임워크 아티팩트 패턴에 맞는지 확인한다."""
        parts = (key or "").split(":")
        return len(parts) >= 2 and self.matches(parts[0], parts[1])

    def matches_jar(self, file_name: Optional[str]) -> bool:
        """좌표를 모르는 jar의 파일 이름이 패턴의 artifactId에 맞는지 확인한다. 이름은 ``<artifactId>[-<버전>].jar`` 꼴이다.
        artifactId 부분이 `*`뿐인 패턴은 파일 이름에 맞지 않는 것으로 본다."""
        stem = (file_name or "").replace("\\", "/").rsplit("/", 1)[-1]
        if stem.lower().endswith(".jar"):
            stem = stem[:-4]
        if not stem:
            return False
        for p in self.patterns():
            a = p.split(":", 1)[1] if ":" in p else p
            if not a.strip("*"):
                continue
            body = fnmatch.translate(a)
            body = body[:-2] if body.endswith(r"\Z") else body
            if re.match(body + r"(-\d[\w.\-]*)?\Z", stem):
                return True
        return False

    def maven_includes(self) -> list[str]:
        """``mvn dependency:tree -Dincludes``에 넣을 패턴 목록을 반환한다. artifactId만 쓴 패턴은 ``*:<artifactId>``가 된다."""
        return [p if ":" in p else f"*:{p}" for p in self.patterns()]


class ExternalizedConfig(BaseModel):
    """코드는 읽지만 패키지된 리소스에는 없는 설정 키를 다루는 설정 클래스.
    컨테이너가 JNDI, context-param, -D로 주던 값을 찾아 Boot 모듈 설정 파일에 플레이스홀더를 넣는다.
    인수인계에는 어떤 값을 어떻게 넣을지 안내한다."""
    enabled: bool = True                              # false면 찾지도 쓰지도 않는다
    # 플레이스홀더 값. grep으로 찾기 쉽다. 환경 변수, -D, 외부 파일이 Boot 우선순위에 따라 덮어쓸 수 있다
    sentinel: str = "__SET_ME__"
    # 플레이스홀더 블록을 Boot 모듈의 설정 파일 끝에 추가한다. MSA는 서비스마다 추가한다
    write_placeholders: bool = True
    # 추가할 파일 이름(Boot 모듈이나 MSA 서비스의 src/main/resources 기준)
    placeholder_file: str = "application.properties"
    # JNDI / context-param / 시스템 속성과 Boot 키의 대응 규칙과 안내
    rules_file: str = "knowledge/config/externalized.yml"
    include_test_refs: bool = True                    # 테스트에서만 읽는 키에도 플레이스홀더를 넣는다
    max_refs_per_key: int = 3                         # 인수인계에서 키마다 보여 줄 참조 위치 수


class RemediationPolicyConfig(BaseModel):
    """취약점 조치의 서버 정책을 담는 클래스. 새 버전을 고르는 근거 중 하나다.
    이 값들의 해시가 계획에 묶인다. 정책이 바뀌면 멈춰 있던 조치는 이어 갈 때 계획을 다시 세운다."""
    pins: dict[str, str] = Field(default_factory=dict)        # "g:a"별로 서버가 정한 이 버전으로만 올린다
    deny: list[str] = Field(default_factory=list)             # "g:a" 또는 "g:*"는 자동으로 바꾸지 않고 직접 확인으로 남긴다
    deny_versions: dict[str, list[str]] = Field(default_factory=dict)   # "g:a"별로 후보에서 뺄 버전
    allow_prerelease: bool = False                            # 사전 릴리스(alpha, beta, RC, SNAPSHOT)를 후보로 쓸지 여부
    allow_major: bool = True                                  # false면 메이저 변경은 제안하지 않는다


class RemediationConfig(BaseModel):
    """취약점 조치 설정을 담는 클래스. 비밀이 아닌 값만 둔다.
    IVAS 주소, 아이디, 비밀번호는 환경 변수로만 받는다(``IVAS_BASE_URL`` / ``IVAS_USER_ID`` / ``IVAS_PASSWORD``,
    Secret). 우선순위는 다른 설정과 같다."""
    # Maven 빌드는 업로드된 프로젝트의 플러그인 코드를 서버에서 실행한다. 보안 격리가 아니다
    # 운영자가 신뢰할 수 있는 프로젝트만 올라온다고 확인했을 때만 true로 둔다. false면 접수하지 않는다(not_ready)
    trusted_projects: bool = False
    # 이관이 BUILD SUCCESS로 끝나면 같은 빌드 파드에서 이어서 취약점 조치를 실행한다
    # 결과는 <실행 폴더>/remediation/에 남는다. 이관의 성공 여부와 종료 코드는 바뀌지 않는다
    # IVAS 접속 값이 없으면 건너뛴다. 이때는 trusted_projects를 확인하지 않는다
    after_migration: bool = False
    # 이관의 마지막 빌드가 실패했을 때도 조치를 이어서 실행할 대상. framework는 사내 프레임워크 프로젝트만 조치한다
    # never는 빌드가 통과한 실행만 조치한다. 확인 빌드 없이 바꾼 버전은 INCOMPLETE로 끝난다
    after_failed_build: Literal["framework", "never"] = "framework"
    # 이어서 실행하는 조치가 빌드 파드에서 쓸 수 있는 시간(분). 넘으면 다음 단계 경계에서 STOPPED로 멈춘다
    # 재개하면 새 파드가 이어서 한다. 파드 수명 한도에 걸려 끝난 이관까지 중단된 것처럼 보이지 않게 한다. 0이면 한도 없음
    chain_budget_minutes: int = 90
    # 조치 대상에서 뺄 IVAS 조치 구분(action.actionDivCd) 값. 그 항목은 보고서에는 남지만 계획과 성공 판단에서 빠진다
    # 값: NE 조치 필요, NP 조치계획 미등록, PL 조치예정, OT 조치일 경과, PC 패치 완료, IG 조치 예외
    # 기본값은 빈 목록이다. 예외 처리한 항목을 빼려면 [PC, IG]처럼 쓴다
    ignore_actions: list[str] = Field(default_factory=list)
    # 사내 아티팩트로 볼 그룹. mandatory / optional / parents(parent, boot_parent, comp_parent)는 artifacts yml의 목록이다
    # framework는 framework.artifacts다. 그 아티팩트 자체이거나 그것을 거쳐 들어온 취약 라이브러리는 프로젝트에서 고치지 않는다
    # inhouse_artifact로 남긴다. 빈 목록이면 쓰지 않는다. 정책 해시에 들어간다
    inhouse_groups: list[str] = Field(default_factory=lambda: ["mandatory", "optional", "parents", "framework"])
    inhouse_message: str = "해당 아티팩트는 예외처리가 가능합니다. 담당자에게 연락 부탁드립니다."
    # 보고서의 위험도 표시 기준. IVAS 점수가 이 값 이상이면 빨강 / 주황 / 노랑 표시를 붙인다
    # 기본값은 CVSS 심각(9.0), 높음(7.0), 보통(4.0) 구간이다. 표시만 바꾸므로 정책 해시에 들어가지 않는다
    severity_red: float = 9.0
    severity_orange: float = 7.0
    severity_yellow: float = 4.0
    max_upload_mb: int = 1024                # 업로드 ZIP 파일 크기 한도(MiB)
    max_entries: int = 200_000               # ZIP 안의 항목 수 한도
    max_unpacked_mb: int = 4096              # 압축을 풀었을 때 전체 크기 한도(MiB)
    max_entry_mb: int = 1024                 # 항목 하나를 풀었을 때 크기 한도(MiB)
    max_compression_ratio: int = 200         # zip 폭탄을 막기 위한 항목별 압축률 한도
    # 회사 레이아웃의 모듈은 루트 pom이 아니라 회사 부모 pom을 상속한다. 그래서 루트에 넣은 버전 지정이 적용되지 않는다
    # true면 그런 모듈의 pom에 버전 지정을 넣는 제안을 만든다. false면 직접 확인 항목으로 남긴다
    module_overrides: bool = True
    max_rounds: int = 3                      # 계획 -> 적용 -> 빌드 -> 재검사 반복 횟수 한도
    # 수정본 빌드가 실패했을 때의 처리. include(기본값)는 실패한 수정본도 결과 ZIP으로 낸다(상태 FAILED, 재검사 없음)
    # discard는 결과 ZIP에 넣지 않고 candidate.patch로만 남긴다. 앞 라운드에서 검증된 결과 ZIP과 원본 ZIP은 남는다
    failed_candidate: Literal["include", "discard"] = "include"
    # 버전을 올린 수정본의 빌드가 실패하면 빌드 오류 수정 루프(LLM)로 소스를 고쳐 본다
    # pom.xml은 고치지 않는다. 파일을 만들거나 지우지도 않는다. 조치 전 빌드부터 실패한 프로젝트에서는 실행하지 않는다
    fix_loop: bool = True
    # 수정 루프가 한 라운드에서 빌드와 수정을 반복하는 횟수 한도
    fix_loop_max_iterations: int = Field(default=10, ge=1, le=50)
    # 수정 루프가 한 라운드에서 쓸 수 있는 시간 한도(분). 실행의 남은 시간이 더 짧으면 그 시간까지만 쓴다
    fix_loop_max_minutes: int = Field(default=30, ge=1, le=240)
    # IVAS가 프로젝트 안의 파일에서 찾은 취약점(파일 컴포넌트)의 처리. 어느 쪽이든 고치지는 않는다
    # report는 보고서에만 싣고 결과 판단에 넣지 않는다. count는 직접 확인 항목으로 남겨 판단에 넣는다
    file_components: Literal["report", "count"] = "report"
    # 프로젝트에 서버 설정을 우회할 수 있는 파일이 있을 때의 처리. refuse는 거부하고, ignore는 기록만 하고 진행한다
    project_maven_config: Literal["refuse", "ignore"] = "ignore"     # .mvn/maven.config, jvm.config, extensions.xml
    maven_wrapper: Literal["refuse", "ignore"] = "ignore"            # mvnw / .mvn/wrapper. 서버 Maven만 쓰므로 실행되지 않는다
    # 승인된 저장소 호스트 목록. 비어 있으면 서버 settings.xml의 미러와 저장소 호스트만 승인된 것으로 본다
    allowed_repository_hosts: list[str] = Field(default_factory=list)
    # pom이 승인되지 않은 호스트의 <repositories>를 선언했을 때의 처리 방식
    foreign_repository: Literal["refuse", "ignore"] = "refuse"
    build_timeout_minutes: int = 60          # package 빌드 한 번의 시간 한도(분)
    # IVAS 호출 설정
    ivas_timeout_seconds: int = 60           # 요청 하나의 시간 한도. 업로드는 ivas_upload_timeout_seconds를 쓴다
    ivas_upload_timeout_seconds: int = 1800
    ivas_poll_seconds: int = 15              # 분석 상태를 조회하는 간격
    ivas_analysis_timeout_minutes: int = 30   # 이 시간 안에 끝나지 않으면 0건이 아니라 검사 미완료로 본다
    ivas_page_size: int = 100
    ivas_project_prefix: str = "sbmig"       # IVAS projectNm = <prefix>-<작업 ID>-<baseline|final-rN>
    # 요약 대조 방식. exact는 요약의 components 건수와 라이브러리 취약점 상세 행 수가 같아야 검사 완료로 본다
    # nonzero는 0인지만 본다. 제안 내용을 바꾸지 않는 값이라 정책 해시에 들어가지 않는다
    ivas_summary_check: Literal["exact", "nonzero"] = "exact"
    policy: RemediationPolicyConfig = Field(default_factory=RemediationPolicyConfig)


class Config(BaseModel):
    """전체 설정을 담는 클래스. load_config가 default.yml, --config yml, SBMIG_* 환경 변수, overrides를 차례로 합친 결과다.
    회사 고유 값은 yml에 있다."""
    target: TargetConfig = Field(default_factory=TargetConfig)
    artifacts: ArtifactsConfig
    layout: LayoutConfig = Field(default_factory=LayoutConfig)
    intake: IntakeConfig = Field(default_factory=IntakeConfig)
    framework: FrameworkConfig = Field(default_factory=FrameworkConfig)   # 사내 프레임워크 프로젝트 판별
    openrewrite: OpenRewriteConfig = Field(default_factory=OpenRewriteConfig)
    build: BuildConfig = Field(default_factory=BuildConfig)
    loop: LoopConfig = Field(default_factory=LoopConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    fix: FixConfig = Field(default_factory=FixConfig)
    script: ScriptConfig = Field(default_factory=ScriptConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    errors: ErrorsConfig = Field(default_factory=ErrorsConfig)
    libraries: LibrariesConfig = Field(default_factory=LibrariesConfig)
    kb: KBConfig = Field(default_factory=KBConfig)
    plan: PlanConfig = Field(default_factory=PlanConfig)
    fleet: FleetConfig = Field(default_factory=FleetConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)
    advisories: AdvisoryConfig = Field(default_factory=AdvisoryConfig)
    externalized: ExternalizedConfig = Field(default_factory=ExternalizedConfig)
    remediation: RemediationConfig = Field(default_factory=RemediationConfig)   # 취약점 조치 설정
    repo_root: str = str(PACKAGE_ROOT)
    # load_config가 남긴 `config: …` 경고 문장. 실행 로그는 설정을 읽은 뒤에 연결된다. 그래서 접수 단계가 다시 기록한다
    _config_warnings: list[str] = PrivateAttr(default_factory=list)

    # ---- 경로와 조회 헬퍼 -----------------------------------------------
    def resolve_path(self, p: str | Path) -> Path:
        """상대 경로를 repo_root 기준 절대 경로로 바꾼다. 절대 경로는 그대로 반환한다."""
        p = Path(p)
        return p if p.is_absolute() else Path(self.repo_root) / p

    @property
    def kb_dir(self) -> Path:
        """KB 디렉토리 경로(repo_root 기준 kb.dir)를 반환한다."""
        return self.resolve_path(self.kb.dir)

    @property
    def recipe_catalog_file(self) -> Path:
        """레시피 카탈로그 파일의 경로를 반환한다(설정 ``plan.catalog_file``)."""
        return self.resolve_path(self.plan.catalog_file)

    @property
    def advisories_file(self) -> Path:
        """기본 권고 파일의 경로를 반환한다(설정 ``advisories.file``)."""
        return self.resolve_path(self.advisories.file)

    @property
    def advisory_files(self) -> list[Path]:
        """기본 권고 파일과 회사가 추가한 파일 목록을 반환한다. 내용은 id 기준으로 합치며 뒤의 것이 우선한다."""
        return [self.advisories_file, *(self.resolve_path(p) for p in self.advisories.extra_files)]

    def optional_keys(self) -> dict[str, ArtifactSpec]:
        """선택 사내 아티팩트를 g:a와 별칭 키로 찾을 수 있는 표를 반환한다."""
        out: dict[str, ArtifactSpec] = {}
        for spec in self.artifacts.optional:
            out[spec.key] = spec
            for alias in spec.aliases:
                out[alias] = spec
        return out


# ---------------------------------------------------------------------------
# 설정 읽기
# ---------------------------------------------------------------------------
_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


def _expand_env(value: Any) -> Any:
    """`${VAR}` / `${VAR:-기본값}`을 환경 변수 값으로 바꾼다. 변수도 기본값도 없으면 그대로 둔다."""
    if isinstance(value, str):
        def repl(m: re.Match[str]) -> str:
            return os.environ.get(m.group(1), m.group(2) if m.group(2) is not None else m.group(0))
        return _ENV_RE.sub(repl, value)
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


def placeholder_names(value: Any) -> set[str]:
    """yml 값 안의 ``${VAR}`` / ``${VAR:-기본값}``이 가리키는 환경 변수 이름들을 반환한다.
    `_expand_env`가 바꾸는 곳만 본다. dict 키는 보지 않는다.
    웹 Main이 설정에 쓰인 환경 변수를 알아낼 때 쓴다(``sbmig.web.contract.env_pin_names``)."""
    if isinstance(value, str):
        return {m.group(1) for m in _ENV_RE.finditer(value)}
    if isinstance(value, list):
        return set().union(*(placeholder_names(v) for v in value)) if value else set()
    if isinstance(value, dict):
        return set().union(*(placeholder_names(v) for v in value.values())) if value else set()
    return set()


def deep_merge(base: dict, override: dict) -> dict:
    """두 dict를 합친다. 뒤의 값이 우선한다. 양쪽이 모두 dict인 값은 안쪽까지 합치고, 나머지는 통째로 바꾼다."""
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _apply_env_overrides(data: dict) -> dict:
    """`SBMIG_A__B=값` 환경 변수를 data["a"]["b"]에 넣는다. 밑줄 두 개가 한 단계 아래를 뜻한다. 값은 YAML로 해석한다."""
    for key, val in os.environ.items():
        if not key.startswith("SBMIG_"):
            continue
        path = [p.lower() for p in key[len("SBMIG_"):].split("__") if p]
        if not path:
            continue
        cur = data
        for p in path[:-1]:
            cur = cur.setdefault(p, {})
            if not isinstance(cur, dict):
                break
        else:
            cur[path[-1]] = yaml.safe_load(val)
    return data


def _env_override_paths() -> dict[tuple[str, ...], str]:
    # SBMIG_<섹션>__<키> 환경 변수의 설정 경로와 변수 이름
    out: dict[tuple[str, ...], str] = {}
    for key in os.environ:
        if key.startswith("SBMIG_") and "__" in key:
            path = tuple(p.lower() for p in key[len("SBMIG_"):].split("__") if p)
            if path:
                out[path] = key
    return out


def _leaf_paths(d: dict, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    out: list[tuple[str, ...]] = []
    for k, v in d.items():
        path = (*prefix, str(k).lower())
        if isinstance(v, dict) and v:
            out.extend(_leaf_paths(v, path))
        else:
            out.append(path)
    return out


def merge_explicit(data: dict, explicit: dict, label: str = "config: ") -> dict:
    """명시한 값(명령줄 옵션, 일괄 실행 매니페스트의 프로젝트별 값)을 환경 변수가 적용된 설정 위에 합친다.
    명시한 값이 `SBMIG_<SECTION>__<KEY>` 환경 변수 값을 덮어쓰면 그 키마다 INFO 한 줄을 남긴다."""
    env_paths = _env_override_paths()
    if env_paths:
        for leaf in _leaf_paths(explicit):
            for path, name in sorted(env_paths.items()):
                n = min(len(path), len(leaf))
                if path[:n] == leaf[:n]:
                    log.info("%s%s given explicitly replaces %s from the environment", label, ".".join(leaf), name)
    return deep_merge(data, explicit)


_DOTENV_LOADED: dict[str, float] = {}     # 읽은 파일의 실제 경로와 그때의 mtime
_DOTENV_SET: dict[str, str] = {}          # 이 로더가 os.environ에 넣은 키. 파일이 바뀌면 함께 바꾼다


def _read_env_text(f: Path) -> str:
    """편집기가 어떤 인코딩으로 저장했든 `.env`를 읽는다.
    UTF-16(PowerShell 5.1의 `>`)과 UTF-8(BOM 포함)을 먼저 시도한다. 둘 다 아니면 cp949로 읽는다."""
    data = f.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp949", errors="replace")


def dotenv_chain(start: Path) -> list[Path]:
    """``start``부터 상위 폴더로 올라가며 ``.env`` 후보를 모은다. 가까운 것이 앞에 온다.
    ``.git``이 있는 폴더까지 올라간다(없으면 파일 시스템 루트). 그래서 패키지가 하위 폴더에 있어도
    저장소 루트의 ``.env``를 찾는다."""
    out: list[Path] = []
    d = Path(start)
    while True:
        out.append(d / ".env")
        if (d / ".git").exists() or d.parent == d:
            return out
        d = d.parent


def load_dotenv_files(*extra: str | Path) -> list[Path]:
    """`.env` 파일들을 읽어 os.environ에 넣는다.
    `$SBMIG_ENV_FILE`, 그리고 패키지 폴더와 현재 디렉토리에서 git 루트까지 올라가며 찾은 `.env`를 읽는다.
    - 먼 파일부터 읽는다. 그래서 가까운 파일이 우선하고, 현재 디렉토리 쪽이 패키지 쪽보다 우선한다
    - 셸에서 설정한 변수가 항상 우선한다. 파일이 바뀌면 다시 읽는다
    - `export KEY=...`, 따옴표, `#` 주석을 받는다
    - 키는 Config에 넣지 않고 환경 변수로만 쓴다
    """
    found: list[Path] = []
    candidates = [os.environ.get("SBMIG_ENV_FILE"), *reversed(dotenv_chain(PACKAGE_ROOT)),
                  *reversed(dotenv_chain(Path.cwd())), *extra]
    for c in candidates:
        if not c:
            continue
        f = Path(c).expanduser()
        try:
            key = str(f.resolve())
            if not f.is_file():
                continue
            mtime = f.stat().st_mtime
            if _DOTENV_LOADED.get(key) == mtime:          # 전에 읽었고 그 뒤로 바뀌지 않았다
                found.append(f)
                continue
            text = _read_env_text(f)
        except (OSError, UnicodeError):
            continue
        found.append(f)
        _DOTENV_LOADED[key] = mtime                       # 웹 서버나 일괄 실행에서 파일을 고치면 다시 읽는다
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            elif v.startswith("#"):
                # `KEY=   # 주석`은 빈 값이다. 주석이 모델 이름으로 넘어가면 게이트웨이가 HTTP 400을 반환한다
                v = ""
            else:
                v = v.split(" #", 1)[0].rstrip()
            # 셸에서 직접 설정한 변수는 덮어쓰지 않는다. 이 로더가 넣은 값만 파일이 바뀌면 함께 바뀐다
            if k and (k not in os.environ or (k in _DOTENV_SET and os.environ[k] == _DOTENV_SET[k])):
                os.environ[k] = v
                _DOTENV_SET[k] = v
    refresh_secrets()          # 방금 읽은 .env에 비밀 값이 있을 수 있어 마스킹 대상을 갱신한다
    return found


class _YamlLoader(yaml.SafeLoader):
    """중복 키를 기록하는 YAML SafeLoader 클래스.
    PyYAML은 같은 매핑에 키가 두 번 나오면 마지막 값만 남긴다. 회사 yml에 `build:` 블록이 두 개면 앞 블록이 사라진다.

    Records (key, first line, second line)."""

    def __init__(self, stream: Any) -> None:
        super().__init__(stream)
        self.duplicate_keys: list[tuple[str, int, int]] = []

    def construct_mapping(self, node: Any, deep: bool = False) -> Any:
        # YAML 병합 키(`<<: *anchor`)를 먼저 펼친다. merge 태그를 만들 생성자가 없어서
        # 펼치지 않으면 섹션끼리 블록을 공유하는 yml을 읽지 못한다
        self.flatten_mapping(node)
        seen: dict[Any, int] = {}
        for key_node, _value_node in node.value:
            if getattr(key_node, "tag", "") == "tag:yaml.org,2002:merge":
                continue
            key = self.construct_object(key_node, deep=deep)
            try:
                hash(key)
            except TypeError:
                continue
            if key in seen:
                self.duplicate_keys.append((str(key), seen[key], key_node.start_mark.line + 1))
            else:
                seen[key] = key_node.start_mark.line + 1
        return super().construct_mapping(node, deep=deep)


def _read_yaml(path: Path, sink: Optional[list[str]] = None) -> dict:
    """yml 파일 하나를 읽는다.
    중복 키가 있으면 파일명, 키, 두 줄 번호를 담은 WARNING을 남긴다.
    최상위가 매핑이 아니면 파일을 무시하고 WARNING을 남긴다.
    경고 문장은 ``sink``에도 모은다. 실행 로그에 다시 기록할 때 쓴다."""
    with open(path, "r", encoding="utf-8") as fh:
        loader = _YamlLoader(fh)
        try:
            loaded = loader.get_single_data() or {}
        finally:
            loader.dispose()
    lines: list[str] = []
    for key, first, second in loader.duplicate_keys:
        lines.append(f"config: {path}: key '{key}' appears twice (lines {first} and {second}) — YAML keeps the LAST block only, "
                     "the values of the first one are lost; merge the two blocks into one")
    if not isinstance(loaded, dict):
        lines.append(f"config: {path}: the top level is a {type(loaded).__name__}, not a mapping of sections — the file is ignored")
    for line in lines:
        log.warning("%s", line)
    if sink is not None:
        sink.extend(lines)
    return loaded if isinstance(loaded, dict) else {}


# _warn_removed_keys가 설명하는 키. _warn_unknown_keys는 이 키를 다시 알리지 않는다
_REMOVED_KEYS = frozenset({"fix.mode", "fix.edits_max_asks_per_case", "build.pom_repositories",
                           "remediation.require_approval", "remediation.always_approve",
                           "openrewrite.fail_on_dry_run_results"})


def _section_model(annotation: Any) -> Optional[type[BaseModel]]:
    """필드 타입이 설정 섹션(BaseModel, Optional 포함)이면 그 모델을 반환한다. 아니면 None이다.
    dict / list는 섹션으로 보지 않는다."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    origin = get_origin(annotation)
    if origin is not None and origin not in (dict, list, tuple, set, frozenset):
        for arg in get_args(annotation):
            if isinstance(arg, type) and issubclass(arg, BaseModel):
                return arg
    return None


def _mapping_value_model(annotation: Any) -> Optional[type[BaseModel]]:
    """``dict[str, Model]`` 필드의 값 모델을 반환한다.
    항목 이름은 자유롭게 쓸 수 있고 항목 안의 키만 검사한다."""
    if get_origin(annotation) is dict:
        args = get_args(annotation)
        if len(args) == 2 and isinstance(args[1], type) and issubclass(args[1], BaseModel):
            return args[1]
    return None


def _env_override_dict() -> dict:
    """``SBMIG_<섹션>__<키>`` 환경 변수만 모아 중첩 dict로 만든다. 알 수 없는 키를 검사할 때 쓴다.
    밑줄 두 개가 없는 ``SBMIG_WEB_CONFIGS`` 같은 이름은 설정 키가 아니라서 뺀다.
    경로가 서로 겹치는 조합은 무시한다. 값은 모두 None이다."""
    out: dict = {}
    for key in os.environ:
        if not key.startswith("SBMIG_") or "__" not in key:
            continue
        path = [p.lower() for p in key[len("SBMIG_"):].split("__") if p]
        cur = out
        for p in path[:-1]:
            cur = cur.setdefault(p, {})
            if not isinstance(cur, dict):
                break
        else:
            if path:
                cur[path[-1]] = None
    return out


def _list_item_model(annotation: Any) -> Optional[type[BaseModel]]:
    """``list[Model]`` 필드의 항목 모델을 반환한다. 그런 필드가 아니면 None을 반환한다."""
    if get_origin(annotation) is list:
        args = get_args(annotation)
        if len(args) == 1 and isinstance(args[0], type) and issubclass(args[0], BaseModel):
            return args[0]
    return None


def _warn_unknown_keys(data: dict, model: Optional[type[BaseModel]] = None, prefix: str = "",
                       label: str = "") -> list[str]:
    """합친 설정에서 모델에 없는 키를 WARNING으로 알린다.
    pydantic은 모르는 키를 조용히 버린다. 그래서 들여쓰기 실수를 놓치기 쉽다.
    섹션 필드와 ``dict[str, 모델]`` / ``list[모델]`` 필드의 항목까지 따라 내려간다. 기록한 문장 목록을 반환한다(테스트용).

    A key a known section declares gets a "did you mean <section>.<key>" hint; `_REMOVED_KEYS` are skipped."""
    out: list[str] = []
    model = model or Config
    if not isinstance(data, dict):
        return out
    fields = model.model_fields
    for key, value in data.items():
        dotted = f"{prefix}{key}"
        if key not in fields:
            if dotted in _REMOVED_KEYS:
                continue
            homes = [name for name, f in Config.model_fields.items()
                     if (m := _section_model(f.annotation)) is not None and key in m.model_fields]
            hint = f" — did you mean {' / '.join(f'{h}.{key}' for h in homes)}?" if homes else ""
            out.append(f"config: {label}{dotted} is not a known key and is ignored (check the indentation / spelling{hint})")
            continue
        sub = _section_model(fields[key].annotation)
        if sub is not None and isinstance(value, dict):
            out.extend(_warn_unknown_keys(value, sub, f"{dotted}.", label))
            continue
        entry = _mapping_value_model(fields[key].annotation)
        if entry is not None and isinstance(value, dict):
            for name, inner in value.items():
                if isinstance(inner, dict):
                    out.extend(_warn_unknown_keys(inner, entry, f"{dotted}.{name}.", label))
            continue
        item = _list_item_model(fields[key].annotation)
        if item is not None and isinstance(value, list):
            for i, inner in enumerate(value):
                if isinstance(inner, dict):
                    out.extend(_warn_unknown_keys(inner, item, f"{dotted}[{i}].", label))
    if model is Config:
        for line in out:
            log.warning("%s", line)
    return out


def _warn_removed_keys(data: dict) -> list[str]:
    """회사 yml이나 환경 변수에 남은 이전 키를 WARNING으로 알린다. 기록한 문장 목록을 반환한다(테스트용).
    조용히 넘어가면 설정의 뜻이 바뀐 것을 알 수 없다. 대상은 아래와 같다.
    - 없앤 키: `fix.mode`, `fix.edits_max_asks_per_case`, `build.pom_repositories`,
      `remediation.require_approval`, `remediation.always_approve`, `openrewrite.fail_on_dry_run_results`
    - `script.max_llm_tries_per_case` 없이 쓴 `script.max_augments_per_case`(이제 보강 횟수만 센다)
    """
    out: list[str] = []
    fix = data.get("fix") if isinstance(data.get("fix"), dict) else {}
    for key in ("mode", "edits_max_asks_per_case"):
        if key in fix:
            out.append(f"config: fix.{key}={fix[key]!r} is ignored since v2.1z (the edits mode is gone; the fix is always the "
                       "script ladder) — remove it from the yml")
    # `build.pom_repositories`는 더 이상 쓰지 않는다. 폐쇄망에서는 settings.xml의 catch-all 미러(mirrorOf external:*)를 쓴다
    build = data.get("build") if isinstance(data.get("build"), dict) else {}
    if "pom_repositories" in build:
        out.append(f"config: build.pom_repositories={build['pom_repositories']!r} is ignored since v2 (the strip handler went with "
                   "the v1 handlers; pom-declared repositories are covered by the settings.xml mirror `mirrorOf external:*`, see "
                   "docs/RUNBOOK.md §0.1) — remove it from the yml")
    # 취약점 조치의 승인 단계는 없어졌다. yml이나 환경 변수에 남은 값은 오류 없이 무시하고 한 번 알린다
    env_rem = _env_override_dict().get("remediation")
    env_rem = env_rem if isinstance(env_rem, dict) else {}
    yml_rem = data.get("remediation") if isinstance(data.get("remediation"), dict) else {}
    for key in ("require_approval", "always_approve"):
        # 환경 변수 값은 합친 설정에도 들어 있으므로 환경 변수 쪽으로 한 번만 알린다
        if key in env_rem:
            where, shown = "env override SBMIG_*__*: ", ""
        elif key in yml_rem:
            where, shown = "", f"={yml_rem[key]!r}"
        else:
            continue
        out.append(f"config: {where}remediation.{key}{shown} is ignored: the approval step of the vulnerability "
                   "remediation was removed (every proposal is applied without waiting) — remove it")
    # `openrewrite.fail_on_dry_run_results`는 어떤 코드도 읽지 않아서 없앴다. 남아 있어도 무시하고 알린다
    env_rw = _env_override_dict().get("openrewrite")
    env_rw = env_rw if isinstance(env_rw, dict) else {}
    yml_rw = data.get("openrewrite") if isinstance(data.get("openrewrite"), dict) else {}
    if "fail_on_dry_run_results" in env_rw:
        where, shown = "env override SBMIG_*__*: ", ""
    elif "fail_on_dry_run_results" in yml_rw:
        where, shown = "", f"={yml_rw['fail_on_dry_run_results']!r}"
    else:
        where = shown = None
    if where is not None:
        out.append(f"config: {where}openrewrite.fail_on_dry_run_results{shown} is ignored: no code read it, so the key "
                   "was removed — remove it")
    script = data.get("script") if isinstance(data.get("script"), dict) else {}
    if "max_augments_per_case" in script and "max_llm_tries_per_case" not in script:
        out.append(f"config: script.max_augments_per_case={script['max_augments_per_case']!r} counts augment steps only since "
                   "v2.1z; the LLM asks per case are capped by script.max_llm_tries_per_case (not set → default "
                   f"{ScriptConfig.model_fields['max_llm_tries_per_case'].default})")
    for line in out:
        log.warning("%s", line)
    return out


def load_config(*paths: str | Path, overrides: Optional[dict] = None, repo_root: Optional[Path] = None) -> Config:
    """설정을 읽어 합친다.
    순서는 default.yml -> 주어진 yml들 -> SBMIG_* 환경 변수 -> overrides(명시한 값) -> `${VAR}` 치환이다.
    모든 명령과 sbmig.api.migrate가 이 함수로 시작한다. .env는 그 전에 환경 변수로 읽힌다.
    명시한 값이 환경 변수 값을 덮어쓰면 INFO 한 줄을 남긴다(`merge_explicit`)."""
    load_dotenv_files()
    data: dict = {}
    warnings: list[str] = []
    files = [DEFAULT_CONFIG_PATH, *[Path(p) for p in paths]]
    for f in files:
        if f and Path(f).exists():
            data = deep_merge(data, _read_yaml(Path(f), warnings))
    # 모르는 키는 yml 키로만 검사한다. _apply_env_overrides는 설정 키가 아닌 SBMIG_* 변수(SBMIG_WEB_CONFIGS 등)도 넣는다
    # 그런 값은 pydantic이 일부러 버린다. 오타가 아니다
    warnings += _warn_unknown_keys(data)
    # SBMIG_<섹션>__<키> 환경 변수(쿠버네티스 ConfigMap)의 이름 오타도 같은 방식으로 알린다
    warnings += _warn_unknown_keys(_env_override_dict(), label="env override SBMIG_*__*: ")
    data = _apply_env_overrides(data)
    # 명령줄 옵션처럼 명시한 값은 SBMIG_<섹션>__<키> 환경 변수보다 우선한다. 그래서 환경 변수를 적용한 뒤에 합친다
    if overrides:
        data = merge_explicit(data, overrides, "config: ")
    data = _expand_env(data)
    if repo_root:
        data["repo_root"] = str(repo_root)
    warnings += _warn_removed_keys(data)
    cfg = Config.model_validate(data)
    cfg._config_warnings = warnings
    return cfg
