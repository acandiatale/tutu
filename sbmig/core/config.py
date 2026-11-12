"""설정 파일의 구조를 정의하고 읽어 들이는 모듈.
모든 명령과 sbmig.api.migrate가 처음에 load_config로 Config를 만든다.
- 합치는 순서(뒤가 우선): default.yml -> --config yml(회사 artifacts yml) -> overrides -> SBMIG_* 환경 변수
- `.env`는 os.environ으로만 들어간다. LLM 키는 환경 변수(LLM_API_KEY)에서만 읽고 yml이나 로그에 쓰지 않는다
- 사내 좌표, 가격, 게이트웨이 같은 회사 고유 값은 yml에 두고 코드에 넣지 않는다
- 키를 추가하거나 바꾸면 config/default.yml과 docs/INTERFACES.md도 함께 고친다

Configuration schema + loader.

Layering (later wins):  config/default.yml  →  --config file(s)  →  environment overrides (SBMIG_* / ${VAR} expansion).
Company-specific coordinates live in a separate artifacts yml so the code base stays generic.


"""
from __future__ import annotations

import fnmatch
import logging
import os
import re
from pathlib import Path
from typing import Any, Literal, Optional, get_args, get_origin

import yaml
from pydantic import BaseModel, Field, PrivateAttr, ValidationInfo, field_validator

log = logging.getLogger("sbmig.config")

from .models import GAV
from .scrub import refresh_secrets

PACKAGE_ROOT = Path(__file__).resolve().parents[2]          # repo root when running from source
DEFAULT_CONFIG_PATH = PACKAGE_ROOT / "config" / "default.yml"


class TargetConfig(BaseModel):
    """목표 버전(Java 21, Spring Boot 4.1.0, Spring Framework 7)."""
    java_version: str = "21"
    spring_boot_version: str = "4.1.0"
    spring_framework_version: str = "7.0.x"


class ArtifactSpec(BaseModel):
    """사내 아티팩트 하나의 좌표를 담는 설정 클래스.
    aliases는 같은 아티팩트로 보는 이전 g:a 키, property_name은 버전을 담는 관례 속성 이름이다."""
    group_id: str
    artifact_id: str
    version: Optional[str] = None            # None → version managed by the parent
    scope: Optional[str] = None
    type: Optional[str] = None
    # optional artifacts: how to find the as-is usage (defaults to same group/artifact)
    aliases: list[str] = Field(default_factory=list)   # old "g:a" keys that should be treated as this artifact
    property_name: Optional[str] = None       # if the version is conventionally kept in a property
    # 선택 사내 아티팩트를 새로 넣게 만드는 as-is 의존성(g:a 키 또는 glob)이다.
    # 프로젝트 pom의 <dependencies>에 있으면 재구성이 이 아티팩트를 선언한다
    # dependencyManagement에만 있는 것과 아티팩트 자신의 키, 별칭은 트리거가 아니다
    # v2.1n: as-is dependencies (g:a keys or globs such as `org.hibernate*:hibernate-core`) whose presence in a project
    # pom's <dependencies> introduces this optional artifact there; the artifact's own key and aliases never match
    when_present: list[str] = Field(default_factory=list)
    # true(기본값)면 아티팩트를 넣을 때 트리거가 된 선언을 지우고, false면 트리거를 남기고 옆에 추가한다
    # true (default): the matched trigger declarations are removed when the optional is added; false keeps them
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
        """버전을 뺀 `groupId:artifactId` 형식의 키."""
        return f"{self.group_id}:{self.artifact_id}"

    def triggered_by(self, key: str) -> bool:
        """``key``가 이 선택 아티팩트를 넣게 하는 트리거인지 확인한다.
        ``when_present``에 맞고 아티팩트 자신의 키나 별칭이 아닐 때만 True다.

        True when `key` matches one of `when_present` (exact or `fnmatch` glob on `group:artifact`) and is neither
        this artifact's own key nor one of its aliases."""
        if not self.when_present or not key or key == self.key or key in self.aliases:
            return False
        return any(key == pat or fnmatch.fnmatchcase(key, pat) for pat in self.when_present)


class ArtifactsConfig(BaseModel):
    """사내 아티팩트 설정을 담는 클래스.
    루트 부모 base-parent, 역할 부모 boot-jar / comp-jar, 필수 아티팩트 2개, 선택 아티팩트 8개로 이루어진다.
    실제 좌표는 회사 yml에만 있고 config/artifacts.example.yml은 플레이스홀더다.
    코드에 회사 좌표를 넣지 않는다."""
    parent: ArtifactSpec                      # the root pom's parent (company: `base-parent`)
    # round 9b — the company layout has role parents: `module/boot-jar/pom.xml` inherits the in-house `boot-jar`
    # parent and `module/comp-jar/pom.xml` the in-house `comp-jar` parent (all in-house artifacts share one line,
    # e.g. 4.0.0); the root pom (base-parent) is then an aggregator the modules do NOT inherit from. None = the
    # modules inherit the root pom (pre-9b behaviour). An as-is module whose parent has the same groupId:artifactId
    # as a role parent (an older migration on 1.0.0) is always moved to the configured version.
    # 역할 부모 설정이다. `{name}-boot` 모듈은 boot_parent를, 나머지 모듈은 comp_parent를 상속한다
    # 둘 다 None이면 모듈이 루트 pom을 상속한다
    boot_parent: Optional[ArtifactSpec] = None
    comp_parent: Optional[ArtifactSpec] = None
    # auto(기본값)면 사내 부모 체인의 pom을 읽어 정한다.
    # 체인이 spring-boot-dependencies를 상속하거나 import하면 true다
    # 아니면 false이고, 재구성이 그 BOM을 `${spring-boot.version}`으로 직접 import한다
    # v2.1t: auto (default) = decided from the parent chain's poms (it reaches spring-boot-dependencies → true, else
    # false: the restructure imports the BOM itself, through the chain's version property); true / false as before
    parent_manages_spring_boot: bool | Literal["auto"] = "auto"
    parent_manages_java_version: bool = True  # if False we set maven.compiler.release
    # 필수 사내 아티팩트(운영 설정은 정확히 2개). mandatory_target에 따라 모든 루트 pom에 선언한다
    mandatory: list[ArtifactSpec] = Field(default_factory=list)   # exactly 2 in production config
    # 선택 사내 아티팩트(8개). 이미 쓰고 있을 때만 버전을 올린다(optional_version_policy)
    optional: list[ArtifactSpec] = Field(default_factory=list)    # 8 in production config
    remove_when_present: list[str] = Field(default_factory=list)  # "g:a" keys to drop (e.g. old in-house parents' BOMs)
    # round 9c: where the mandatory dependencies are declared — `root`: every root pom (the project root; in MSA each
    # service root and the top aggregator), the company rule; `modules`: every role module pom (pre-9c with role parents)
    mandatory_target: Literal["root", "modules"] = "root"
    # floor: only raise an optional artifact to its configured version (never downgrade a newer as-is/recipe version)
    # pin  : always set exactly the configured version
    optional_version_policy: Literal["floor", "pin"] = "floor"

    @property
    def role_parents(self) -> bool:
        """역할 부모(boot_parent, comp_parent)가 둘 다 설정되었는지 확인한다.

        True when the modules inherit their own in-house parents (boot_parent / comp_parent both set)."""
        return self.boot_parent is not None and self.comp_parent is not None

    def parent_specs(self) -> list["ArtifactSpec"]:
        """설정된 사내 부모 전체를 루트 부모, boot_parent, comp_parent 순으로 반환한다.

        Every configured in-house parent: root parent, then boot_parent / comp_parent when set."""
        return [s for s in (self.parent, self.boot_parent, self.comp_parent) if s is not None]

    def parent_keys(self) -> list[str]:
        """사내 부모를 가리키는 g:a 키를 별칭까지 포함해 반환한다.
        이 부모를 해석하지 못하면 settings나 Nexus 문제이므로 실행을 중단한다.

        `g:a` keys (incl. aliases) that mean "a configured in-house parent" — an unresolvable one aborts the run."""
        out: list[str] = []
        for s in self.parent_specs():
            for k in (s.key, *s.aliases):
                if k not in out:
                    out.append(k)
        return out


class LegacyContextConfig(BaseModel):
    """Spring Framework / servlet 프로젝트를 Boot로 바꿀 때 재구성이 직접
    처리하는 기동 관련 항목을 켜고 끄는 설정 클래스.
    빌드도 LLM도 기동 실패는 볼 수 없으므로 재구성이 처리하며, 끄면 인수인계 문장만 남는다.
    클래스와 Boot 속성 매핑은 `knowledge/config/web_xml_rules.yml`에 있다.

    v2.1x: deterministic handling of the legacy Spring context at conversion time (prompt review 2026-10-31 §4). The
    build gate and the LLM never see startup failures, so the restructure handles the known ones itself; every switch
    off = Handover sentence only. Mappings (filter class → Boot property, context-param …) live in
    `knowledge/config/web_xml_rules.yml`, not here."""
    # @ImportResource에는 web.xml의 contextConfigLocation이 가리키는 진입 XML만 넣고, 찾지 못하면 전부
    # 넣는다. Only the entry XMLs named by
    # web.xml's contextConfigLocation (root + DispatcherServlet) are imported; XMLs reached by <import> are left to Spring;
    # no web.xml / no match → every XML as before
    entry_xml_only: bool = True
    exclude_test_xml: bool = True            # D2: XML under src/test/ is never imported into the Boot context
    # as-is Framework는 같은 id의 빈 덮어쓰기와 순환 참조를 허용했지만 Boot
    # 4는 둘 다 기본값이 false라서 그대로면 기동이 실패한다
    # 그래서 legacy 전환에서는 두 속성을 true로 쓰고 중복 id를 인수인계에 남긴다
    # D1: as-is Spring Framework allowed bean overriding and circular references; Boot 4 defaults both to false → the
    # restructure writes both `spring.main.*` properties as true for a converted legacy project and lists duplicate ids
    allow_bean_overriding: bool = True
    allow_circular_references: bool = True
    # 건너뛴 web.xml 필터의 init-param을 web_xml_rules.yml의 `properties:` 매핑대로 Boot 속성으로 옮긴다
    # spring.servlet.encoding.charset / force …). D4: init-params of skipped web.xml filters → Boot properties per the rules table
    filter_properties: bool = True
    # D8 / D9 / D10: <mvc:default-servlet-handler/> → server.servlet.register-default-servlet=true; XML CommonsMultipartResolver →
    # removed from the legacy-config COPY + spring.servlet.multipart.* limits; @EnableWebMvc / WebMvcConfigurationSupport detected →
    # no spring.mvc.view.* written, Handover instead
    default_servlet: bool = True
    multipart_resolver: bool = True
    detect_enable_web_mvc: bool = True
    # /WEB-INF/… 설정 파일 위치를 복사본에서 `classpath:asis-webinf/…`로
    # 고치고, `${profile}`이 든 위치는 `*` glob으로 바꾼다
    # D6 / D7: `/WEB-INF/…` locations rewritten to `classpath:asis-webinf/…` in the copies; `${…}` segments → `*` globs
    rewrite_webinf_locations: bool = True
    profile_globs: bool = True
    # OpenRewrite가 provided를 compile로 바꾼 jakarta.servlet-api 등을 as-is scope로 되돌린다
    # E-M6: a dependency whose as-is declaration had scope `provided` keeps it after the recipes (jakarta.servlet-api …)
    restore_asis_scopes: bool = True
    # spring_legacy의 webmvc starter는 웹 증거(web.xml, spring-webmvc, servlet-api, @Controller, JSP)가 있을 때만 쓴다
    # 증거가 없으면 CLI / batch 프로젝트에 Tomcat이 뜨지 않도록 `spring-boot-starter`를 쓴다
    # E-M9: the webmvc starter of `starter_by_case` only with web evidence; a CLI / batch spring_legacy gets `spring-boot-starter`
    web_starter_by_evidence: bool = True
    # UTF-8이 아닌 소스(MS949 / EUC-KR)를 가진 모듈의 pom에 재구성이 구조 검토 전에
    # project.build.sourceEncoding을 쓴다
    # D-M7: the restructure writes `project.build.sourceEncoding` for a module whose sources are not UTF-8
    write_source_encoding: bool = True
    # JNDI 빈, DelegatingFilterProxy(보안 외), OpenEntityManagerInViewFilter, *.do 매핑, <jsp-config>,
    # WebApplicationInitializer, log4j 1, Security XML은
    # 인수인계 문장과 XML 복사본 옆 `<!-- MIGRATION-TODO -->` 주석으로 남긴다
    handover_sentences: bool = True


class LayoutConfig(BaseModel):
    """회사 표준 모듈 구성을 정하는 설정 클래스. 루트 바로 아래에 `{name}-boot`(Boot 설정만, boot-jar),
    `{name}-back`(모든 소스, comp-jar), 웹 리소스가 있을 때만 `{name}-front`를 만든다.
    Boot as-is 프로젝트는 디렉토리를 그대로 두고 pom만 바꾼다.
    이전 구성은 이 키들로 다시 쓸 수 있다(config/artifacts.local-e2e.yml).

    Round 9c — the company layout (stated 2026-10-21): modules directly under the root named `{name}-boot`
    (Boot configuration only, parent `boot_parent`), `{name}-back` (every source incl. controllers, parent
    `comp_parent`) and — only when the as-is project has web resources — `{name}-front` (static files under
    `front_static_dir`, JSPs under `front_jsp_dir` per Servlet 3, parent `comp_parent`); `{name}` = the root pom's
    artifactId (`project_name` overrides). Spring Boot as-is projects (incl. MSA) keep their directories
    (`boot_as_is: in-place`): only the poms change — root → `artifacts.parent`, modules named like
    `boot_module_pattern` whose pom confirms a boot app → `boot_parent`, every other module → `comp_parent`; a
    single-module Boot app is wrapped into `{name}-boot` under an aggregator root. The pre-9c layout
    (`module/boot-jar` + `module/comp-jar`, Boot projects merged like the others) is `module_dir: module`,
    `boot_module: boot-jar`, `comp_module: comp-jar`, `front_module: null`, `boot_as_is: layout`
    (config/artifacts.local-e2e.yml keeps it for the older fixtures)."""
    module_dir: str = ""                                # "" = modules directly under the root
    boot_module: str = "{name}-boot"
    comp_module: str = "{name}-back"
    front_module: Optional[str] = "{name}-front"       # None = no front module (web resources stay in the boot module)
    project_name: Optional[str] = None                  # `{name}`; default = the root pom's artifactId
    front_static_dir: str = "src/main/resources/static"                          # HTML / css / js / images
    front_jsp_dir: str = "src/main/resources/META-INF/resources/WEB-INF/jsp"     # JSPs (Servlet 3 jar resources)
    # 2026-10-28 (user: "정적 리소스들도 front resources 에 디렉토리 그대로"): files under a <resource> / source directory
    # that lies INSIDE the web root (`STATIC/resources/**`: media, templates, downloads, css) stay classpath content
    # of the comp module AND are copied into the front module's `front_static_dir` at their web path, as the as-is WAR
    # served them. False = the pre-10-28 behaviour (kept in comp only, a follow-up names the static web files).
    # as-is WAR가 그 경로로 파일을 제공했으므로, 웹 루트 안 <resource> 디렉토리의 파일은 comp classpath에
    # 두면서 front의 static에도 같은 웹 경로로 복사한다
    front_copy_resource_dirs: bool = True
    # v2.1h (2026-10-28, user: "JSP 도 javax → jakarta 로 변경해 줘야 할 것 같아"): OpenRewrite never parses JSPs, so the
    # restructure renames the javax EE names of the analyzer's table (sbmig/analyze/jakarta_packages.yml) inside the
    # code regions of every JSP / JSPF / JSPX / tag file (page imports, useBean class / type, scriptlets; Java
    # literals and comments untouched) in every mode, after the files reached their final place.
    # False = the pre-10-28 behaviour (Handover follow-up only).
    # 레시피가 JSP를 읽지 않으므로 재구성이 JSP의 javax EE 이름을 jakarta로 바꾼다.
    # 끄면 인수인계 항목만 남는다
    jsp_jakarta_rename: bool = True
    # v2.1z-j: the restructure wires the jars no pom declares (compile dependency when the repository has the coordinates,
    # else <module>/lib + system scope) before the first build; False → the v2.1z-i pre-try only
    wire_bundled_jars: bool = True
    # Boot as-is 프로젝트 처리 방식이다.
    # in-place는 디렉토리를 유지하고 pom만 고치며(회사 규칙), layout은 다른 경우처럼 모듈로 나눈다
    boot_as_is: Literal["in-place", "layout"] = "in-place"   # Spring Boot as-is: keep directories (poms only) | split
    boot_module_pattern: str = "*-boot"                 # module dirs that are boot apps in Boot as-is / MSA (pom confirms)
    jsp_boot_packaging: Literal["war", "jar"] = "war"   # JSPs present → the boot module is an executable WAR (java -jar runs
                                                        # the embedded Tomcat; JSPs cannot be served from an executable jar)
    boot_artifact_suffix: str = "-boot"
    comp_artifact_suffix: str = "-back"                 # artifactId = <root artifactId> + suffix (front: "-front")
    split_policy: Literal["minimal", "web-layer"] = "minimal"
    # as-is 다중 모듈 처리 방식이다.
    # merge(기본값)는 소스를 comp(back) 모듈 하나로 합치고, wrap은 기존 모듈을 감싸기만 한다
    multi_module_policy: Literal["wrap", "merge"] = "merge"
    # 2026-10-23: no pom.xml at the project root but sibling poms that depend on each other (e.g. APP/BATCH, APP/WEB,
    # APP/EAR built one by one and packaged by the EAR) → intake writes a root aggregator pom listing them, so they
    # build in one reactor and the merge policy applies (sbmig.analyze.aggregator)
    # 루트에 pom이 없고 서로 의존하는 형제 pom만 있으면 접수 단계가 루트 집계 pom을 만들어 한 reactor로 빌드한다
    generate_aggregator: bool = True
    # merge: a path present in more than one module → first module in <modules> order wins, the other copies are
    # saved under <run dir>/merge-conflicts/ and listed in the Handover ("first-wins"); "fail" = restructure error
    merge_collision: Literal["first-wins", "fail"] = "first-wins"
    # round 9: after the in-house parent is set, drop explicit <version>s the new parent chain manages
    # (`mvn help:effective-pom` after the pom edits): gte = only when the managed version is ≥ the explicit one
    # (a deliberately newer pin stays), any = always, off = keep every explicit version. Mandatory / optional /
    # parent artifacts from `artifacts` and <scope>system</scope> dependencies are never touched.
    # 새 부모 체인이 관리하는 의존성의 명시 <version>을 지운다. gte는 관리 버전이 같거나 높을 때만 지운다
    # 사내 아티팩트와 system scope 의존성은 건드리지 않는다
    drop_managed_versions: Literal["gte", "any", "off"] = "gte"
    # 프로젝트 pom의 <properties> 중 사내 부모 체인이 정의한 버전 속성과 이름이 같은 것을 지운다.
    # 예: spring-boot.version
    # Maven 속성 상속에서 as-is 값이 부모 값을 덮지 않게 하기 위해서다. 아직 `${이름}`으로 참조하면 남긴다
    # v2.1o / v2.1p: remove project <properties> named like a version property the in-house parent chain defines
    # (read from its poms) unless still referenced; false keeps them (the review may flag them)
    drop_bom_property_overrides: bool = True
    # 부모 체인 pom에는 없지만 회사가 지우고 싶은 속성 이름이다.
    # 체인이 정의한 이름은 부모 pom에서 직접 읽으므로 기본값은 빈 목록이다
    # 아직 ${이름}으로 참조하면 남긴다
    # v2.1p: escape hatch — names to remove although no pom of the parent chain defines them (the chain's own names
    # are read from its poms now, sbmig.build.parent_chain); kept while still referenced
    parent_property_names: list[str] = Field(default_factory=list)
    # 재구성이 BOM을 import할 때 버전으로 쓸 부모 체인 속성 이름이다.
    # 앞에서부터 체인이 정의한 첫 이름을 `${이름}`으로 쓴다
    # v2.1t: the parent-chain property names a BOM import written by the restructure references (first one defined wins;
    # none → the literal target version / the spring_cloud_bom.yml pairing)
    bom_version_properties: dict[str, list[str]] = Field(default_factory=lambda: {
        "spring_boot": ["spring-boot.version", "spring.boot.version", "springboot.version", "spring-boot-version"],
        "spring_cloud": ["spring-cloud.version", "spring.cloud.version", "spring-cloud-version"],
    })
    application_class_name: str = "Application"
    boot_resources_glob: list[str] = Field(default_factory=lambda: [
        "application*.yml", "application*.yaml", "application*.properties",
        "bootstrap*.yml", "bootstrap*.properties", "logback*.xml", "log4j2*.xml", "banner.txt",
    ])
    # 프로젝트 종류별로 boot 모듈에 넣을 Boot starter다. spring_boot는 as-is starter를 그대로 둔다
    starter_by_case: dict[str, str] = Field(default_factory=lambda: {
        "pure_java": "spring-boot-starter",
        "servlet_tomcat": "spring-boot-starter-webmvc",
        "spring_legacy": "spring-boot-starter-webmvc",
        "spring_boot": "auto",          # keep whatever starters the as-is project already has
    })
    add_servlet_component_scan: bool = True
    # 2026-10-23: web.xml servlets / filters / listeners / error pages / session timeout → a generated
    # `WebXmlRegistrations` @Configuration in the boot module (`generate`), or follow-ups only (`followup`)
    # web.xml의 서블릿, 필터, 리스너, 오류 페이지 처리 방식이다.
    # generate는 boot 모듈에 WebXmlRegistrations @Configuration을 만들고, followup은 인수인계에만 남긴다
    # Boot WAR는 web.xml을 읽지 않으므로 기본값은 generate다
    web_xml_registrations: Literal["generate", "followup"] = "generate"
    import_xml_bean_configs: bool = True   # @ImportResource for detected <beans> xml files
    msa_parent_policy: Literal["per-service-root", "top-aggregator"] = "per-service-root"
    # 전환할 때 legacy Spring 컨텍스트를 처리하는 설정(LegacyContextConfig)
    legacy_context: LegacyContextConfig = Field(default_factory=LegacyContextConfig)

    def module_name(self, which: Literal["boot", "comp", "front"], name: str) -> Optional[str]:
        """역할(boot / comp / front)에 맞는 모듈 디렉토리 이름을 `{name}`을 치환해서 반환한다.
        front가 꺼져 있으면 None을 반환한다.

        The module directory name for a role (`{name}` expanded); None when the role has no module (front off)."""
        tpl = {"boot": self.boot_module, "comp": self.comp_module, "front": self.front_module}[which]
        return tpl.replace("{name}", name) if tpl else None

    def module_path(self, which: Literal["boot", "comp", "front"], name: str) -> Optional[str]:
        """역할 모듈의 루트 기준 posix 경로(module_dir + 이름)를 반환한다.
        예: `shop-boot`, `module/boot-jar`.

        Root-relative posix path of a role module (`module_dir` + name), e.g. `shop-boot` or `module/boot-jar`."""
        n = self.module_name(which, name)
        if n is None:
            return None
        d = self.module_dir.strip("/")
        return f"{d}/{n}" if d else n


class RecipeSetConfig(BaseModel):
    """실행할 OpenRewrite 레시피 묶음을 정하는 설정 클래스.
    Java 21, javax -> jakarta, Boot 프로젝트면 Boot 4 레시피를 재구성 전에 한 번에 실행한다.
    빈 묶음은 회사 설정이 다시 채울 수 있게 남겨 두며, 채울 때는 catalog.yml의 `sets:`에도 넣어야 한다."""
    # mirrors `openrewrite.recipes` of config/default.yml (a Config() built without the yml plans the same recipes).
    # v2 (flow-v2, business decision 2026-10-26): only Java 21, javax → jakarta and — for Spring Boot projects — the
    # Boot 4 recipes run, all in ONE OpenRewrite run before the restructure ("레시피가 무엇을 바꿀지 모르니 넣지
    # 않는다"); Spring Framework 7 / Security / JUnit 5 / project / library / KB recipes are off, everything else is
    # fixed by LLM scripts. The empty sets stay so a company config can opt back in.
    jdk21: list[str] = Field(default_factory=lambda: ["org.openrewrite.java.migrate.UpgradeToJava21"])
    jakarta: list[str] = Field(default_factory=lambda: [
        "org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta",
    ])
    spring_boot_4: list[str] = Field(default_factory=lambda: [
        "org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0",
        "org.openrewrite.java.spring.boot4.SpringBootProperties_4_1",
    ])
    # spring_legacy 프로젝트도 Framework 7 레시피를 실행한다. Boot로 바뀌면 Boot 4.1 BOM이 Spring 7 /
    # Jackson 3를 가져오므로 그 코드 변경을 LLM에 맡기지 않기 위해서다
    # Boot 프로젝트는 상위 레시피 안에서 같은 레시피가 이미 실행된다
    spring_framework_7: list[str] = Field(default_factory=lambda: [
        "org.openrewrite.java.spring.framework.UpgradeSpringFramework_7_0",
    ])
    # Spring Security를 쓰는 spring_legacy 프로젝트에만 넣는 묶음이다.
    # 6_1 … 6_5와 7_0은 앞 단계를 포함하지 않으므로 모두 나열한다
    # Boot 프로젝트는 UpgradeSpringBoot_4_0 안에서 같은 단계가 실행된다
    spring_security_7: list[str] = Field(default_factory=lambda: [
        *(f"org.openrewrite.java.spring.security6.UpgradeSpringSecurity_6_{v}" for v in range(6)),
        "org.openrewrite.java.spring.security7.UpgradeSpringSecurity_7_0",
    ])
    junit5: list[str] = Field(default_factory=list)
    # spring_legacy가 spring-batch를 쓰면 Batch 5에서 6으로, 네이티브 Hibernate ORM을 쓰면 Hibernate 7.1
    # 묶음을, servlet_tomcat은 Servlet 6에서 제거된 API 레시피를 넣는다
    # Boot 프로젝트는 상위 레시피가 이미 처리한다. 빈 목록이면 끈다
    # v2.1w: planned only when the analyzer found the library (`spring-batch` / `hibernate`), like the Security chain;
    # `servlet_jakarta_10` for every servlet_tomcat project (spring_legacy gets it inside UpgradeSpringFramework_7_0)
    spring_batch_6: list[str] = Field(default_factory=lambda: [
        "org.openrewrite.java.spring.batch.SpringBatch5To6Migration",
    ])
    hibernate_7: list[str] = Field(default_factory=lambda: [
        "org.openrewrite.hibernate.MigrateToHibernate71",
    ])
    servlet_jakarta_10: list[str] = Field(default_factory=lambda: [
        "org.openrewrite.java.migrate.jakarta.RemovalsServletJakarta10",
    ])
    extra: dict[str, list[str]] = Field(default_factory=dict)


class OpenRewriteConfig(BaseModel):
    """OpenRewrite 실행 설정(transform.rewrite)을 담는 클래스. 플러그인과 레시피 BOM 버전, 레시피 아티팩트,
    시간 제한, JVM 메모리, 다운로드 재시도, 파싱에서 뺄 glob을 정한다."""
    plugin_version: str = "6.46.1"
    # 3.38.0은 Maven Central의 아티팩트가 서로 맞지 않으므로 레시피 BOM을 3.37.0(rewrite-spring 6.37.0)으로 고정한다
    recipe_bom_version: str = "3.37.0"   # 3.38.0 references unpublished artifacts (2026-10)
    recipe_artifacts: list[str] = Field(default_factory=lambda: [
        "org.openrewrite.recipe:rewrite-migrate-java",
        "org.openrewrite.recipe:rewrite-spring",
        "org.openrewrite.recipe:rewrite-testing-frameworks",
        "org.openrewrite.recipe:rewrite-static-analysis",
    ])
    recipe_artifact_versions: dict[str, str] = Field(default_factory=dict)   # override per artifact; else BOM-managed
    recipes: RecipeSetConfig = Field(default_factory=RecipeSetConfig)
    timeout_minutes: int = 40
    jvm_args: list[str] = Field(default_factory=lambda: ["-Xmx4g"])
    oom_retry_jvm_args: list[str] = Field(default_factory=lambda: ["-Xmx8g"])   # one retry after OutOfMemoryError
    # round 9: the recipe run failed (or left literal `x.y.x` versions) because a download failed (proxy 429/5xx,
    # reset, timeout) → wait and retry the run; 0 = no retry
    # 다운로드 실패(프록시 429/5xx, 연결 끊김, 타임아웃)로 레시피 실행이 깨지면 기다렸다가 다시 실행한다.
    # 0이면 재시도하지 않는다
    download_retry: int = 1
    download_retry_wait_s: int = 30
    # round 9c: source globs the plugin must not parse (`-Drewrite.exclusions`). OpenRewrite's XML parser also accepts
    # .jsp/.jspx/.html/.htm; JSP scriptlets and `<` inside attribute strings only produce ANTLR "token recognition
    # error" noise (the file is skipped) and no recipe of ours touches them
    # JSP / HTML은 어느 레시피도 건드리지 않고 ANTLR 경고만 많이 나오므로 파싱하지 않는다
    # 그래서 JSP 안의 javax는 레시피가 바꾸지 않는다
    exclusions: list[str] = Field(default_factory=lambda: ["**/*.jsp", "**/*.jspx", "**/*.html", "**/*.htm"])
    # 사내 부모 g:a의 as-is 버전이 저장소에 없어 레시피 실행이 실패했을 때의 처리다.
    # align은 설정한 버전으로 맞추고 다시 실행한다(수정 루프에서는 sbmig-parent 사전 시도)
    # continue는 레시피 없이 계속하고, abort는 중단한다. 설정한 버전 자체가 저장소에 없으면 항상 중단한다
    # v2.1z-ag: the as-is version of a company parent g:a is not in the repository — align (set the configured version,
    # rewrite once more; the fix loop's `sbmig-parent` pre-try), continue (no recipes, go on), abort (the old behaviour)
    unresolvable_asis_parent: Literal["align", "continue", "abort"] = "align"
    # 코드에서 읽지 않는 키
    fail_on_dry_run_results: bool = False
    plugin_goal: str = "run"                            # run | dryRun


class BuildConfig(BaseModel):
    """Maven 빌드 설정을 담는 클래스. mvn 경로, settings.xml, 로컬 저장소, JDK 21 홈, 시간 제한, 테스트
    생략, 이전 소스 인코딩 후보, 버전 조회 저장소를 정한다."""
    mvn: str = "mvn"
    settings_file: Optional[str] = None
    local_repo: Optional[str] = None
    java_home: Optional[str] = None                     # JDK 21 home; None → inherit
    offline: bool = False
    extra_args: list[str] = Field(default_factory=lambda: ["-B", "-ntp", "-Dstyle.color=never"])
    skip_tests: bool = True                             # -DskipTests (tests still compile)
    # 테스트는 범위 밖이므로 모든 빌드에 -Dmaven.test.skip=true를 붙이고, 보고서에 tests-not-compiled
    # 플래그와 후속 작업을 남긴다
    # -Dmaven.test.skip=true on EVERY build: test sources are neither compiled nor run, so a test-source error can
    # never block the migration (the company's decision, 2026-10-22: the fleet does not fix tests). The report flags
    # `tests-not-compiled` and lists the follow-up; the L7 / L7b fallbacks become moot.
    skip_test_compile: bool = True                      # v2 default (the company decision of 2026-10-22)
    allow_skip_test_compile_fallback: bool = True       # -Dmaven.test.skip=true as last resort (flagged in report)
    allow_skip_tests_fallback: bool = True              # -DskipTests when only runtime test failures remain
                                                        # (flag `tests-skipped`, failing tests listed as follow-ups)
    timeout_minutes: int = 30
    env: dict[str, str] = Field(default_factory=dict)
    # legacy source encodings (JDK 18+ defaults to UTF-8 → "unmappable character" on MS949/EUC-KR sources): the
    # detection candidates the structure review and the script helper use
    # 이전 소스 인코딩 후보다.
    # JDK 18 이상은 기본 인코딩이 UTF-8이라 MS949 / EUC-KR 소스가 "unmappable character"로 깨진다
    # 구조 검토와 스크립트 헬퍼가 이 순서로 인코딩을 판별한다
    encoding_candidates: list[str] = Field(default_factory=lambda: ["utf-8", "cp949", "euc-kr", "shift_jis", "latin-1"])
    # <repositories> / <pluginRepositories> declared in the project poms (old Nexus URLs, Central, Spring milestones)
    # are left as they are; route them through a `settings_file` mirror (mirrorOf external:*) — the plan notes say
    # whether the mirror covers them
    # package prefixes treated as *library* code in error signatures (project-owned FQCNs are masked as '*').
    # In-house library namespaces (parent/mandatory/optional group ids) are added automatically.
    # 오류 서명에서 라이브러리 코드로 볼 패키지 접두어다.
    # 프로젝트 고유 FQCN은 '*'로 가려서 케이스를 다른 프로젝트에서도 재사용할 수 있게 한다
    external_package_prefixes: list[str] = Field(default_factory=list)
    # round 10: the preflight aborts (flag `jdk-not-21`) when `mvn -v` reports another Java major (0 = no check)
    # 사전 점검으로 `mvn -v`의 Java 메이저 버전이 다르면 중단한다(플래그 jdk-not-21). 0이면 점검하지 않는다
    require_java_major: int = 21
    # "… was cached in the local repository, resolution will not be reattempted …" (a Nexus 404 remembered for a day)
    # → the same build once more with -U before the error is triaged
    # Nexus 404 응답이 로컬 저장소에 캐시되어 있으면 오류를 분류하기 전에 -U로 한 번 더 빌드한다
    retry_negative_cache: bool = True
    # v2.1z-s (2026-11-04, Kubernetes split layout): commands run ONCE per run around the whole migration — before the first
    # step (setup: e.g. create the builder pod and wait for it) and after the report (teardown: delete it). argv lists, no shell;
    # the environment carries SBMIG_RUN_ID / SBMIG_RUN_DIR / SBMIG_PROJECT_DIR. A failing setup aborts the run (nothing could
    # build); a failing teardown is a WARNING. Empty = nothing (the default; the single-image deployment needs none)
    setup_command: list[str] = Field(default_factory=list)
    teardown_command: list[str] = Field(default_factory=list)
    hook_timeout_s: int = 600
    # v2.1d: where sbmig reads `maven-metadata.xml` / poms to tell the LLM which releases exist (same major first).
    # None → the catch-all <mirror> of `settings_file` (its <server> credentials, `${env.X}` resolved), else Maven
    # Central unless `offline`; a URL here wins
    # LLM에 어떤 릴리스가 있는지 알려 주기 위해 maven-metadata.xml을 읽을 저장소다
    # None이면 settings_file의 catch-all 미러를, 그것도 없으면 Maven Central(offline이 아닐 때)을 쓴다
    repository_url: Optional[str] = None
    # 명시적인 repository_url은 운영자의 결정이므로 가장 먼저 조회하고, 그다음 settings가 가리키는 저장소들을 조회한다
    # settings의 주 URL과 다르면 WARNING을 한 번 남긴다.
    # 계정 정보는 같은 URL의 <server>를, 없으면 아래 id를 쓴다
    # v2.1z-e: an explicit repository_url is asked first (the operator's decision), then the settings-derived set; a
    # WARNING names both when they differ. Credentials: the <server> of the mirror / repository with the same URL, else
    # this server id (for a URL that appears nowhere in the settings).
    repository_server_id: Optional[str] = None
    repository_timeout_seconds: int = 10
    # 필터링되는 리소스 아래에서 항상 바이너리로 취급할 회사 확장자다(예: dmdb). 소문자로 점 없이 쓴다
    # 재구성이 스캔에서 찾은 확장자와 함께 <nonFilteredFileExtensions>에 넣어 MalformedInputException을 막는다
    # v2.1m: company extensions always written to `<nonFilteredFileExtensions>` of every pom that filters a resource,
    # next to the ones the scan finds (lower-case, no dot; `SBMIG_BUILD__NON_FILTERED_EXTENSIONS='[dmdb, xyz]'`)
    non_filtered_extensions: list[str] = Field(default_factory=list)
    # Windows에서 백신, IDE, 남은 java 프로세스가 target/ 파일을 잡아 빌드가 실패하면 기다렸다가 다시 빌드한다
    # 환경 문제를 LLM에 넘기지 않기 위해서다.
    # 패턴은 knowledge/config/build_env_errors.yml의 file_lock에 있다
    # v2.1r: a build failing on a Windows file lock under target/ is run again after a wait (0 retries = off)
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
    """단계별 수정 루프 설정(빌드 goal, 최대 라운드, 사용 여부)을 담는 클래스.
    현재는 verify 단계만 켜져 있다."""
    goal: str = "compile"
    max_iterations: int = 5
    enabled: bool = True


class LoopConfig(BaseModel):
    """수정 루프 설정을 담는 클래스. 라운드 수, LLM 호출과 시간 한도, 정체와 진동 판단, 라운드당 케이스 수,
    악화 시 되돌리기, 차단 정책, 실행 엔진(graph / inline)을 정한다."""
    # 중간 빌드는 없다. jdk21 / spring / restructure 루프는 꺼져 있고, 변환, 재구성, 검토 뒤 첫
    # 빌드(verify)의 수정 루프가 모든 오류를 고친다
    # v2: no intermediate builds — the jdk21 / spring / restructure loops are off; the first build is the final
    # `mvn clean package` after transform + restructure + review, and its fix loop (`verify`) does all the fixing
    phases: dict[str, PhaseLoopConfig] = Field(default_factory=lambda: {
        "jdk21": PhaseLoopConfig(goal="compile", max_iterations=3, enabled=False),
        "spring": PhaseLoopConfig(goal="test-compile", max_iterations=6, enabled=False),
        "restructure": PhaseLoopConfig(goal="package", max_iterations=6, enabled=False),
        "verify": PhaseLoopConfig(goal="package", max_iterations=50),   # 라운드(빌드)는 최대 50회
    })
    # 프로젝트당 LLM 호출 상한이다. 0은 무제한, 양수는 상한, -1은 호출 금지(플릿 전체 예산을 다 쓴 경우)다
    max_llm_calls: int = 0                   # 0 = no limit on LLM calls (v2.1); > 0 = a hard cap per project;
                                             # -1 = no LLM call at all (a fleet whose max_llm_calls_total is used up)
    max_total_minutes: int = 240
    # 같은 오류 집합이 N번 반복되면 정체로 보고 전략을 바꾼다
    stall_limit: int = 2                     # identical error multiset N times → escalate strategy
    # 한 라운드에 처리하는 케이스 수 상한이다. 근본 원인 케이스부터 처리한다
    max_clusters_per_iteration: int = 8
    revert_on_regression: bool = True
    regression_ratio: float = 1.5            # errors_after > errors_before * ratio → regression
    # 빌드 게이트이므로 무슨 일이 있어도 마지막에 `mvn clean package`를 한 번 더 실행한다
    always_final_verify: bool = True         # `mvn clean package` at the end no matter what
    # graph(기본값)는 LangGraph StateGraph가 단계를 진행하고 inline은 이전 루프다.
    # 어느 쪽이든 보고서는 같다(테스트로 보장)
    engine: Literal["graph", "inline"] = "graph"   # graph: LangGraph StateGraph drives the phases (company
                                             # convention, sbmig.graph); inline: the original in-process loop
    oscillation_window: int = 4              # error-set history compared for A↔B toggling (≥ stall detection window)
    # 같은 오류 집합 사이를 오가는 진동이 몇 번 반복되면 관련 케이스를 차단할지 정한다
    # 창(window)은 비교 범위라서 넓히면 더 빨리 잡힌다. 느슨하게 하려면 이 값을 올린다
    # v2.1z-f: how many repeated A↔B toggles block the involved cases (was hard-coded 2); raise it to persist longer
    oscillation_limit: int = 2
    # 차단 사유별로 허용할 LLM 스크립트 시도 수다.
    # 0이면 바로 차단하고, 저장된 스크립트는 이 값과 관계없이 재사용한다
    # 사내 부모를 찾지 못하는 것은 settings / Nexus 문제라 코드로 고칠 수 없으므로 0이다
    # how many LLM attempts a cluster of this BlockReason may get before it is blocked (0 = block immediately).
    # v2: no deterministic handler swaps a javax library, bumps a missing version or a JDK-21-incompatible plugin any
    # more, so those get LLM script attempts (a stored script is reused regardless); an unresolvable in-house parent
    # stays 0 (settings / Nexus — no code change fixes it)
    blocked_policy: dict[str, int] = Field(default_factory=lambda: {
        "external_javax_api": 2, "external_api_incompatible": 2, "artifact_not_found": 2, "parent_not_found": 0,
        "plugin_incompatible": 2, "unsupported_source": 0, "encoding": 0,
        "reactor_duplicate": 0,                      # v2.1k: a pom structure problem — handed over, never asked of the LLM
        "file_lock": 0,                              # v2.1r: target/ locked by another process — environment, not code
        "needs_owner": 0,                            # v2.1y: the LLM declined (blocker outside the project) — handed over
    })
    # LLM이 만든 의존성 하나가 모든 컴파일 오류를 가릴 수 있으므로 pom 수정
    # 뒤 이 종류의 오류가 나오면 개수와 관계없이 되돌린다
    structural_regression_kinds: list[str] = Field(default_factory=lambda: [
        "pom_invalid", "parent_resolution", "plugin_resolution", "dependency_resolution"])
                                             # appearing after a pom edit ⇒ revert regardless of count (round 10: a
                                             # made-up dependency an LLM added masked every compile error)
                                             # except (2026-11-05) the LATER-stage errors a fixed pom problem had hidden —
                                             # every target gone + new structural kinds all of a later stage = uncovered


BUILTIN_LLM_PROVIDERS = ("company", "openai_compatible", "anthropic", "fake")


class ModelPrice(BaseModel):
    """모델 하나의 1M 토큰당 가격(`llm.pricing.<model>`)을 담는 클래스.
    통화는 llm.pricing_currency로 정한다.

    `llm.pricing.<model>`: list price per 1M tokens (currency = llm.pricing_currency)."""
    input_per_mtok: float
    output_per_mtok: float
    cached_input_per_mtok: Optional[float] = None   # informational: sbmig cannot see cache hits in the gateway reply


class LLMConfig(BaseModel):
    """LLM 연결 설정을 담는 클래스. 키는 환경 변수(`api_key_env`, 기본값 LLM_API_KEY)에서만 읽는다.
    provider는 company(기본값), openai_compatible, anthropic, fake(재생), plugin, 등록된 어댑터 이름 중 하나다.

    `provider` is one of BUILTIN_LLM_PROVIDERS, `plugin` (→ `plugin` = "pkg.module:factory"), or the name of an
    adapter registered under the `sbmig.llm_adapters` entry-point group (in-house gateway / agent platform)."""
    # 클래스 기본값은 config/default.yml과 같은 company다
    provider: str = "company"                            # the class default now matches config/default.yml (2026-10-28)
    plugin: Optional[str] = None             # "pkg.module:attr" — attr is an LLMAdapterLike/LLMClientLike class or a
                                             # factory `fn(options: dict, cfg: Config) -> adapter | client`
    plugin_options: dict[str, Any] = Field(default_factory=dict)   # passed verbatim to the plugin factory
    base_url: str = "${LLM_BASE_URL}"
    api_key_env: str = "LLM_API_KEY"
    model: str = "${LLM_MODEL}"
    escalation_model: Optional[str] = None   # stronger model used after stall
    recipe_model: Optional[str] = None       # cheaper model for recipe selection advice + KB recipe candidates (None → model)

    @field_validator("model", "escalation_model", "recipe_model", mode="before")
    @classmethod
    def _clean_model_name(cls, v: Any, info: ValidationInfo) -> Any:
        """모델 이름에서 끝의 `# 주석`과 공백을 떼고, 주석뿐인 값은 비운다.
        .env의 주석이 모델 이름에 섞여 들어가면 게이트웨이가 실행 중에 HTTP 400을 반환하기 때문이다.

        A model name is one token: an inline `# comment` tail and surrounding whitespace are dropped, and a
        value that is only a comment (`LLM_RECIPE_MODEL= # cheaper model`, `set X=# ...` on Windows) is unset.
        Without this the gateway answers `HTTP 400 … "# cheaper model" is an unsupported model` mid-run."""
        if not isinstance(v, str):
            return v
        cleaned = v.split("#", 1)[0].strip() if not v.lstrip().startswith("${") else v.strip()
        if cleaned != v.strip():
            log.warning("llm.%s=%r is not a plain model name: using %r", info.field_name, v, cleaned or None)
        if not cleaned:
            return None if info.field_name != "model" else ""
        return cleaned
    temperature: float = 0.0
    max_output_tokens: int = 8000
    max_context_chars: int = 60000           # budget for packed context per request
    timeout_seconds: int = 180
    max_retries: int = 3
    # 429와 Retry-After가 붙은 502 / 503 / 504는 고장이 아니라 바쁜
    # 상태이므로 대기 시간의 합이 이 초에 이를 때까지 재시도한다
    # 0이면 max_retries만큼만 재시도한다
    # 429 (and 502 / 503 / 504 with Retry-After) = the gateway is busy, not broken: keep retrying that call (capped
    # backoff + jitter) for up to this many seconds of waiting, independent of max_retries. 0 = max_retries only.
    busy_wait_seconds: int = 600
    # 토큰을 교환한 직후에도 401이면 잠깐 기다렸다가 다시 교환해서 보내는 추가 횟수다.
    # 다른 파드가 같은 앱 토큰을 교환해 기존 토큰이 무효가 됐을 수 있기 때문이다
    # 0이면 401 뒤에 한 번만 다시 교환한다
    # extra refresh-and-retry rounds after a 401 that follows a fresh exchange (another client exchanging the same app
    # token may have invalidated it). 0 = one refresh after a 401, as before.
    auth_retries: int = 2
    # round 9 (L5): the second consecutive transport/config failure pauses the loop once per run for this long and
    # retries; only a second streak marks the LLM unavailable for the rest of the run. 0 = no cool-down (old behaviour)
    # 전송이나 설정 실패가 두 번 연속 나면 실행당 한 번 이 시간만큼 기다렸다가 재시도한다
    # 다시 연속으로 실패하면 그 실행 동안 LLM을 쓸 수 없는 것으로 본다
    cooldown_seconds: int = 60
    # 케이스 하나에서 LLM이 파일을 더 요청할 수 있는 횟수
    max_context_rounds: int = 2              # how many times the LLM may ask for more files per cluster
    extra_headers: dict[str, str] = Field(default_factory=dict)
    prompt_version: str = "v1"
    # v2.1z-p (2026-11-04): the case thread — one gateway session per case and run (agent gateway: a session remembers the
    # system prompt and the earlier turns, the user checked it): the first ask of a case opens it (full prompt, no session id),
    # every augment of the same case continues it with the DELTA only (what the last script did, the errors now, new files),
    # a fresh `new` after the augments / another case = a new session. Off = stateless asks as before.
    case_thread: bool = False
    thread_fallback_full: bool = True        # a continued turn rejected (4xx) / no session id back → resend the full prompt in a new session
    record_dir: Optional[str] = None         # record requests/responses for replay tests
    # company 공급자는 requests.post 한 번으로 호출한다(Bearer LLM_API_KEY, json = model + messages + payload_extra)
    # 이 단순한 형태를 유지하고 LangChain이나 httpx 옵션을 추가하지 않는다
    # provider `company` (default): requests.post(LLM_BASE_URL, headers={Authorization: Bearer LLM_API_KEY},
    # json={"model", "messages", **payload_extra}, timeout=timeout_seconds) — see sbmig/fix/company.py
    payload_extra: dict[str, Any] = Field(default_factory=dict)      # extra top-level payload fields, sent verbatim
    # v2.1z-m (2026-11-04): the in-house GPT agent gateway. `gateway: agent` sends {model: asset_id, messages, stream: false,
    # async: false, params: {role: <the chosen model name — LLM_MODEL / LLM_ESCALATION_MODEL, e.g. build_error_fixer /
    # escalation_fixer>}, session_id, trace_id, max_tokens, temperature, **agent_extra}; the Bearer token comes from a token
    # exchange when token_url is set (POST token_url with the app token → access_token, refreshed before it expires)
    gateway: str = "legacy"                                  # legacy | agent (LLM_GATEWAY)
    asset_id: Optional[str] = None                           # LLM_ASSET_ID: the agent gateway's top-level `model`
    token_url: Optional[str] = None                          # LLM_TOKEN_URL: the token exchange endpoint (empty = static key)
    app_token_env: str = "LLM_APP_TOKEN"                     # the app token's variable; unset → LLM_API_KEY is the app token
    token_refresh_margin_s: int = 300                        # refresh when less than this is left before expiry (5 min)
    agent_extra: dict[str, Any] = Field(default_factory=dict)  # static extra fields of the agent payload, sent verbatim
    response_path: Optional[str] = None      # dot path to the reply text, e.g. "data.result.content"; None → auto-detect
    # 2026-10-22: per-project API cost in the report. Keys are model ids as the gateway names them (exact match first,
    # then the longest key that is a prefix of / contained in the model id, case-insensitive); unmatched models are
    # listed as unpriced. Company specifics live in config/default.yml / the company config, never in code.
    # 모델별 가격(보고서 비용 표)이다. 회사 가격은 yml에만 둔다
    pricing: dict[str, ModelPrice] = Field(default_factory=dict)
    pricing_currency: str = "USD"
    pricing_note: str = ""                   # printed under the cost table (source + date of the prices)


class ScriptConfig(BaseModel):
    """수정 스크립트 실행 설정(sbmig.script)을 담는 클래스.
    `def fix(p)`를 정적 검사한 뒤 별도의 `python -I` 프로세스에서 헬퍼 API `p`로 실행한다.
    `p`는 프로젝트 폴더만 다룬다.
    악의적인 작성자를 막는 샌드박스가 아니라 실수로 프로젝트 밖을 건드리지 않게 하는 장치다.

    v2 fix scripts (sbmig.script): the LLM answers with `def fix(p)`; it runs in a separate `python -I` process
    against the helper API `p` (project-folder files only), after a static check (allowed imports only; no open /
    exec / eval / os / subprocess / socket / dunder access). Not a sandbox against a hostile author — a guard
    against a script that reaches outside the project by mistake (docs/v2/PLAN-v2.md §3.2)."""
    timeout_seconds: int = 60                # per run; the process tree is killed after it (status timeout)
    # 스크립트 프로세스의 인터프리터다.
    # pydantic과 lxml이 설치되어 있어야 하며, 없으면 ModuleNotFoundError로 실패한다
    python: Optional[str] = None             # interpreter for the script process; None = the one running sbmig
    extra_allowed_imports: list[str] = Field(default_factory=list)   # added to the built-in whitelist (company call)
    max_code_chars: int = 30000              # a longer script is refused (status rejected)
    log_lines: int = 200                     # p.log lines kept per run
    max_files_changed_warn: int = 40         # a run that changes more files gets a warning note (the diff is kept)
    # 수정 순서: 저장된 스크립트 재사용(reuse_versions개, LLM 없음) -> 보강(max_augments_per_case번) -> 신규 작성
    # 케이스당 LLM 요청(신규 + 보강)이 max_llm_tries_per_case에 이르면 BLOCKED(repeated_failure)로 인수인계한다
    reuse: bool = True                       # same case seen before → run its best stored version first (no LLM)
    reuse_versions: int = 1                  # stored versions tried per case and run before the LLM is asked
    # v2.1z (2026-11-02): generous defaults, to be lowered once the company runs show what a case really needs. augments =
    # LLM extensions of this run's last code-bearing try (augment steps only; used up → the next ask is a fresh `new`);
    # llm tries = every LLM ask for the case in this run (new + augment) — at the cap the case is BLOCKED (repeated_failure)
    # and handed over. Both are plain caps (0 augments = never augment; 0 tries = never ask the LLM for the case). Before
    # v2.1z one counter (3) counted new + augment together.
    max_augments_per_case: int = 15          # augment steps per case and run
    max_llm_tries_per_case: int = 20         # LLM asks per case and run (new + augment); then BLOCKED repeated_failure
    # LLM이 편집도 스크립트도 없이 프로젝트 밖의 문제라고 답한 케이스는 이 횟수를 넘으면 BLOCKED needs_owner가 된다
    # 같은 파일의 형제 케이스는 LLM을 호출하지 않고 함께 차단한다
    # v2.1y: declined answers (no edits, no script, a blocker outside the project) allowed per case before BLOCKED needs_owner;
    # sibling cases in the same files are blocked without another call
    max_declines_per_case: int = 1
    # v2.1z-n (company run 2026-11-04: a Spring Mobile replacement burned 20 asks as script → augment → rollback): a case the
    # LLM fixes by DIRECT EDITS (<no_script> + <edits>: project code, nothing to reuse) gets this many edit attempts before
    # BLOCKED repeated_failure — fewer than the script ladder, each attempt is a fresh ask with the previous ones summarized
    max_edit_tries_per_case: int = 4
    # ok 직후 같은 스크립트를 한 번 더 실행해서 또 바뀌면 non_idempotent로 표시한다.
    # 저장은 하지만 재사용하지 않는다
    # v2.1y: run the script once more right after `ok`; a second change marks the stored version non_idempotent (never reused)
    idempotency_check: bool = True


class ErrorsConfig(BaseModel):
    """오류 목록 설정을 담는 클래스.
    실행 디렉토리에 errors.jsonl과 errors.md를 쓰고, 끝나면 KB의 errors.jsonl에 추가한다.
    diff와 원문 길이 상한, 학습 기록(변경 조각, 스크립트 실행 이력)의 크기를 정한다.

    v2.1 error list (docs/v2/PLAN-v2.1.md §3.3): <run dir>/errors.jsonl + errors.md, appended to <kb>/errors.jsonl."""
    diff_chars: int = 6000                   # cap of a try's stored diff (0 = no cap); the full diff is in changes.patch
    raw_chars: int = 600                     # cap of an entry's raw error block
    md_view: bool = True                     # write <run dir>/errors.md and <kb>/cases.md
    json_files: bool = True                  # one readable <kb>/cases/<case_id>/errors/<run_id>-<id>.json per error
                                             # (people browse these; errors.jsonl stays the machine copy)
    # v2.1e: as-is / to-be code per modification (EditNote.hunks) and the stored scripts' run history
    snippet_lines: int = 0                   # lines kept per side of a hunk; 0 = the whole hunk (user: "사람이 보는
                                             # 거면 40줄 고정 안 해도 되지 않을까")
    script_runs: int = 30                    # ScriptRun records kept per stored script version (newest last)


class LibrariesConfig(BaseModel):
    """의존성 버전 정책 설정을 담는 클래스. LLM은 스스로 메이저 버전을 올리거나 라이브러리를 바꾸지 않는다.
    같은 메이저의 jakarta 릴리스를 먼저 찾고, 없을 때만 더 높은 메이저와 운영자 확인 안내를 준다.
    표는 사람이 `<kb>/libraries.yml`로 늘린다.

    v2.1d dependency version policy (2026-10-27): the LLM never raises a dependency's major version or replaces a
    library on its own — same-major jakarta release first (the prompt lists what the repository has); a higher major
    only when the same major has no jakarta build, and then with a 운영자 확인 guide. People extend the shipped
    table (knowledge/libraries/jakarta_libraries.yml) in `<kb>/libraries.yml` without a redeploy."""
    lookup: bool = True                      # ask the repository (build.repository_url / settings mirror / Central)
                                             # which releases exist; off → the prompt carries the table + policy only
    max_pom_checks: int = 6                  # candidate poms fetched per library to tell jakarta builds apart
    include_prerelease: bool = False         # milestones / RCs / alphas listed as candidates
    max_artifacts_per_case: int = 3          # libraries looked up per case (the errors' artifacts / owner classes)
    company_file: str = "libraries.yml"      # the people's table, relative to kb.dir (same schema; its entry wins)
    verify_guide_llm: bool = True            # a major upgrade whose answer has no <verify> → ONE more LLM call for
                                             # the guide; off (or no LLM) → sbmig synthesizes it from the diff
    # 아직 javax를 쓰는 라이브러리의 jakarta 빌드를 sbmig가 저장소에서 직접 찾아, 확인되면 LLM보다 먼저 바꿔
    # 넣는다(사전 시도 sbmig-jakarta)
    # 버전 한정어나 classifier `jakarta`, pom의 jakarta.* 선언으로 확인하고, 없으면 jar를 내려받아 class 참조를 센다
    # v2.1z-e jakarta build discovery: sbmig itself looks for a jakarta build of a javax-era library (same coordinates'
    # newer releases → Nexus 3 search by name / group stem → sibling-name probes) and the `sbmig-jakarta` pre-try swaps the
    # declaration before any LLM call; verification = a `jakarta` version qualifier / classifier, the pom's jakarta.*
    # declarations, else a bounded jar scan of the class files' references
    discover_jakarta_builds: bool = True     # the kill switch: off → v2.1d behaviour (the same coordinates' releases are still
                                             # listed in the hints; no verification, no siblings, no `sbmig-jakarta` pre-try,
                                             # no decline rejection)
    nexus_search: bool = True                # Nexus 3 `service/rest/v1/search` when the repository URL is `…/repository/<name>/`
    max_jakarta_probes: int = 12             # sibling names probed per library (`-jakarta` variants, raised trailing digits)
    max_jar_scans: int = 2                   # candidate jars downloaded per library when the pom says nothing
    max_jar_scan_mb: int = 20                # a bigger jar is not downloaded (the candidate stays "unverified")


class ReviewConfig(BaseModel):
    """구조 검토 설정(restructure.review)을 담는 클래스.
    재구성 뒤 첫 빌드 전에 LLM이 pom, 의존성, 모듈을 회사 규칙에 맞는지 검토한다.
    critical은 바로 고치고 커밋하므로 되돌릴 수 있다.
    warning은 보고서의 'Structure review'와 오류 목록 메모로 남긴다.

    v2 structure review (sbmig.review): after the restructure, before the first build, the LLM checks the poms,
    dependencies and module layout against the company rules. Critical findings → one fix script (committed, can be
    reverted); warnings → the report section "Structure review" and <kb>/reviews.jsonl."""
    enabled: bool = True
    fix_critical: bool = True                # run the review's script when it reports critical issues
    max_pom_chars: int = 60000               # all pom text in the prompt; longer poms are cut (head + tail)
    max_poms: int = 40
    # 읽지 않는 키다. 검토 결과는 오류 목록에 기록하고 KB의 reviews.jsonl에는 쓰지 않는다
    record: bool = True                      # one line per run in <kb>/reviews.jsonl
    # 검토는 기본으로 edits만 요구하므로 시스템 프롬프트에서 스크립트 절과 헬퍼 API 표가 빠진다.
    # true면 <script>도 요구한다
    # v2.1y: edits-only review by default (the script section and helper table leave the prompt); true = ask for <script> too
    ask_script: bool = False
    # 검토 편집을 적용한 뒤 재구성 불변식(체인이 정의한 속성 추가, 관리 아티팩트의 버전, 보호 키, 낯선 <parent>)을 다시 검사한다
    # 위반한 편집은 원래대로 되돌리고, 이유를 알려 주며 한 번 다시 요청한다
    # v2.1y: re-check the restructure's invariants after the edits
    invariant_check: bool = True
    # 회사에서 특정 줄을 확인할 수 있도록 검토 입력(user 프롬프트)을 <run dir>/logs/review-input.md로 남긴다
    save_input: bool = True


class FixConfig(BaseModel):
    """어댑터 2단계 설정을 담는 클래스로, 수정기 전체를 사내 코딩 에이전트로 바꿀 때 쓴다.
    plugin은 "pkg.module:attr"(FixerLike 클래스 또는 팩토리)이고 propose_script 계약을 지켜야 한다.

    Second adapter tier: replace the whole fixer (our SEARCH/REPLACE protocol over a chat completion) with an
    in-house coding agent. `plugin` = "pkg.module:attr" where attr is a FixerLike class or a factory
    `fn(options: dict, cfg: Config, llm: LLMClientLike | None) -> FixerLike`."""
    plugin: Optional[str] = None
    plugin_options: dict[str, Any] = Field(default_factory=dict)
    # 수정은 항상 스크립트로 하므로 수정 모드 스위치(`mode: script | edits`, `edits_max_asks_per_case`)는 없다.
    # The v2.1i `mode` / `edits_max_asks_per_case` switches (the token comparison) were
    # removed in v2.1z: the fix is always the script ladder.


class KBConfig(BaseModel):
    """파일 KB 설정을 담는 클래스.
    위치(kb.dir, 기본값 kb-v2/), 비밀 값 마스킹 규칙, 프로세스 간 잠금 대기 시간을 정한다.
    테스트와 데모는 out/ 아래의 KB만 쓴다."""
    dir: str = "kb-v2"                       # relative to repo root unless absolute: errors.jsonl, cases.md, cases/<id>/
    # secret masking rules (built-ins + this yml: company additions, invariant 2), relative to the repo root
    redact_file: str = "knowledge/kb/redact.yml"
    lock_timeout_seconds: int = 300          # cross-process kb/.lock wait before a write gives up (logged, never fatal)


class PlanConfig(BaseModel):
    """계획 설정을 담는 클래스.
    규칙에 따라 항상 같은 결과를 내는 계획과 선택적인 LLM 검토(기본값 꺼짐)를 정한다.
    레시피 id는 catalog_file 한 곳에서만 정의한다."""
    advisor: Literal["none", "llm"] = "none"   # optional LLM review of the rule-based plan
    catalog_file: str = "knowledge/recipes/catalog.yml"   # single source of truth for known recipes (advisor
                                             # whitelist, coverage, gates, descriptions); relative to repo root
    # 반쯤 옮겨진 트리의 오류만 쌓이므로 OpenRewrite 뒤 재구성 전의 `mvn clean compile`은 기본으로 실행하지 않는다
    # true면 한 번 컴파일해 오류 목록을 미리 채우지만 고치지는 않는다
    compile_after_rewrite: bool = False     # v2.1o: no build before the restructure; true = the v2.1 compile step


class FleetConfig(BaseModel):
    """`sbmig fleet` 설정을 담는 클래스.
    에이전트 하나가 프로젝트 N개를 반드시 순서대로 처리하며 그 사이에 KB를 채운다.
    인프라 장애 대기, 디스크 여유 공간, 끝난 프로젝트 정리 방식도 여기서 정한다.

    `sbmig fleet`: one agent, N projects, strictly sequential, KB fed between projects."""
    order: Literal["manifest", "simple-first"] = "manifest"   # simple-first: pure_java → servlet → spring_legacy → boot → msa
    stop_on_abort: bool = False              # stop the whole fleet when one project aborts (exit 3)
    retry_failed: bool = False               # re-run projects whose last status was incomplete/aborted
    max_projects: int = 0                    # 0 = all
    max_llm_calls_total: int = 0             # 0 = unlimited; fleet-wide LLM budget across projects
    reindex_every: int = 10                  # kb reindex after every N projects (0 = never)
    # round 10: infrastructure guard for the 500-project run
    preflight: bool = True                   # check Java / Maven / settings / LLM / disk once before the first project
    infra_failure_limit: int = 3             # this many projects in a row ended for an infrastructure reason → wait
    infra_retry_minutes: int = 15            # wait between infrastructure re-checks (0 = stop the fleet right away)
    infra_max_waits: int = 4                 # re-checks before the fleet stops (state saved; `fleet resume` goes on)
    cleanup: Literal["none", "build-output", "workspace"] = "build-output"   # after a project: build-output = delete
                                             # target/ dirs of its run copy; workspace = also the project copy (result.zip,
                                             # report and patch stay)
    min_free_gb: float = 5.0                 # free space needed on the output drive before a project starts (0 = no check)
    summary_formats: list[str] = Field(default_factory=lambda: ["md", "csv", "json"])


class ReportConfig(BaseModel):
    """보고서 설정을 담는 클래스.
    형식(md / json), result.zip, diff, report.md 언어, 최종 빌드 뒤 javax API jar 조사를 정한다.
    report.md는 ko를 고를 수 있지만 json, 콘솔, 로그는 영어로 유지한다."""
    formats: list[str] = Field(default_factory=lambda: ["md", "json"])
    zip_result: bool = True
    include_diff: bool = True
    language: Literal["en", "ko"] = "en"   # report.md language (report.json, console and logs stay English)
    # after the final build: `mvn dependency:tree` per module → javax EE API jars still on the classpath and the
    # direct dependency that drags each in (the library still compiled against javax); also fills BlockedItem.via
    javax_api_inventory: bool = True


class AdvisoryConfig(BaseModel):
    """라이브러리 권고 설정(EOL, 관리되지 않는 라이브러리)을 담는 클래스.
    실행을 막지 않고 수정 프롬프트의 힌트와 인수인계 섹션에만 쓴다.

    Library advisories (knowledge/libraries/advisories.yml → sbmig.advisory): EOL / no-longer-managed libraries
    such as springfox, hystrix, sleuth. Never blocks a run — the build goes as far as it can and the report's
    Handover section says what the owning team must do next."""
    enabled: bool = True
    file: str = "knowledge/libraries/advisories.yml"      # relative to repo root unless absolute
    extra_files: list[str] = Field(default_factory=list)  # company additions / overrides (same schema, merged by id,
                                                          # later files win)
    policy_overrides: dict[str, Literal["guide", "off"]] = Field(default_factory=dict)
                                                          # advisory id → "off" disables the entry ("guide" = as is)
    report_carried_over: bool = True                      # list as-is-version pins (unmanaged after the parent swap)
    max_from_here: int = 40                               # cap of the handover's "from here on" list


class IntakeConfig(BaseModel):
    """접수 설정을 담는 클래스.
    Ant 프로젝트(``build.xml``만 있고 pom.xml이 없음)는 ``ant_message``를 남기고 종료한다(exit 3).
    - ``ant_to_maven: true``면 ``sbmig.analyze.ant.synthesize_pom``으로 pom.xml을 만들어 접수한다.
      코드는 남겨 두었으므로 이 스위치만 켜면 된다
    - jar 좌표는 jar 안 pom.properties -> 이름 표(jar_names.yml + <kb>/jar_names.yml) -> sha1 저장소 조회
      순으로 찾고, 못 찾으면 ``unknown_jar`` 정책을 따른다
    - pom.xml도 build.xml도 없는 트리는 거부한다

    Intake: an Ant project (build.xml, no pom.xml) is refused at intake with `ant_message` (exit 3; company decision
    2026-11-12). With `ant_to_maven: true` it gets a synthesized pom.xml instead (v2.1j; the code is kept) — layout from
    build.xml and the directories, jar coordinates from the jar's pom.properties, the name table, then a sha1 repository
    lookup; unknown jars follow `unknown_jar`. A tree with neither pom.xml nor build.xml is always rejected."""
    ant_to_maven: bool = False                              # true = synthesize a pom.xml for an Ant project and go on (v2.1j)
    ant_message: str = "Maven 변환 후 신청 부탁드립니다."    # what an Ant project's owner is told when it is refused
    synth_group_id: str = "com.company.migrated"           # placeholder groupId of a synthesized pom (company config overrides)
    synth_version: str = "1.0.0-SNAPSHOT"                   # version of a synthesized pom when build.xml names none
    unknown_jar: Literal["system", "skip"] = "system"       # system = <systemPath> dependency (build passes) | skip = not declared
    bundled_jars: bool = True                      # v2.1z-i: scan undeclared jars in the tree (intake.bundled_jars)
    jar_lookup: bool = True                                 # sha1 lookup (build.repository_url / settings mirror / Central) for jars still unknown
    jar_names_file: str = "knowledge/libraries/jar_names.yml"   # shipped stem → coordinates table
    company_jar_names_file: str = "jar_names.yml"           # company rows under kb.dir (same format), merged over the shipped table
    # 2026-11-05: <build><extensions> that are deploy transports (wagon-*) are removed before the first Maven call — an unresolvable
    # one stops the model build, OpenRewrite included; the list is knowledge, not code
    strip_deploy_extensions: bool = True
    build_extensions_file: str = "knowledge/libraries/build_extensions.yml"
    min_java_source: str = "1.8"                            # build.xml source / target below this is raised (JDK 21 compiles 8+ only)
    max_jar_bytes_scan: int = 64 * 1024 * 1024              # a jar bigger than this is not opened for pom.properties / packages
    # Oracle ADF(JDeveloper) 프로젝트는 변환하지 않는다.
    # 근거 계열이 둘 이상이면 "ADF 프로젝트라 변환이 불가능합니다"로 끝낸다
    # stop은 분석 직후 멈춘다(exit 3, 플래그 adf-project, 보고서 "ADF 규모" 섹션, 인수인계 첫 줄, LLM과 Maven 호출 없음)
    # continue는 보고만 하고 계속 진행한다(권장하지 않음) / stop (default) = end the run at analyze with the message and the sizing;
    # continue = report only, the run goes on (not recommended)
    adf_policy: Literal["stop", "continue"] = "stop"


class ExternalizedConfig(BaseModel):
    """외부 설정 키 처리 방식을 담는 클래스.
    코드는 읽지만 패키지된 리소스에는 없는 설정(JNDI, context-param, -D 등 컨테이너가 주던 값)을 다룬다.
    목록을 만들고 __SET_ME__ 플레이스홀더를 넣어 두어, 인수인계에서 어떤 값을 어떻게 넣을지 알려 준다.

    Round 8: settings the code reads but no packaged resource defines (they came from the container before:
    JNDI, context-param, -D, catalina.properties). sbmig lists them, writes sentinel placeholders so the packaged
    configuration is complete, and the Handover tells the owners which values to set and how."""
    enabled: bool = True
    sentinel: str = "__SET_ME__"
    write_placeholders: bool = True                   # append the block to the boot-jar application.properties
    placeholder_file: str = "application.properties"  # relative to src/main/resources of boot-jar / each MSA service
    rules_file: str = "knowledge/config/externalized.yml"   # JNDI / context-param / system-property → Boot key rules + guide
    include_test_refs: bool = True                    # keys read only by tests still get placeholders (tests must load)
    max_refs_per_key: int = 3                         # references shown per key in the Handover


class RemediationPolicyConfig(BaseModel):
    """취약점 조치의 서버 정책을 담는 클래스로, 새 버전을 고르는 근거 중 하나다.
    이 값들의 해시가 승인 대기 제안에 묶이므로 정책이 바뀌면 이전 제안은 승인할 수 없다."""
    pins: dict[str, str] = Field(default_factory=dict)        # "g:a"별로 서버가 정한 이 버전으로만 올린다
    deny: list[str] = Field(default_factory=list)             # "g:a" 또는 "g:*"는 자동으로 바꾸지 않고 직접 확인으로 남긴다
    deny_versions: dict[str, list[str]] = Field(default_factory=dict)   # "g:a"별로 후보에서 뺄 버전
    allow_prerelease: bool = False                            # 시험판(alpha / beta / RC / SNAPSHOT)을 후보로 쓸지 여부
    allow_major: bool = True                                  # false면 메이저 변경은 제안하지 않는다


class RemediationConfig(BaseModel):
    """취약점 조치 설정을 담는 클래스로, 비밀이 아닌 값만 둔다.
    IVAS 주소, 아이디, 비밀번호는 환경 변수(``IVAS_BASE_URL`` / ``IVAS_USER_ID`` / ``IVAS_PASSWORD``,
    Secret)로만 받으며 우선순위는 다른 설정과 같다."""
    # Maven 빌드는 업로드된 프로젝트의 플러그인 코드를 서버에서 실행하며 보안 샌드박스가 아니다
    # 운영자가 신뢰할 수 있는 프로젝트만 올라온다고 확인했을 때만 true로 둔다.
    # false면 접수하지 않는다(not_ready)
    trusted_projects: bool = False
    # 조치는 웹 파드가 아니라 빌드 파드에서 실행한다.
    # 이관이 BUILD SUCCESS로 끝나면 이관 그래프의 `remediation` 노드가 조치 그래프를 호출한다
    # 결과는 <실행 폴더>/remediation/에 남고 이관의 성공 여부와 종료 코드는 바뀌지 않는다.
    # IVAS 값이 없으면 건너뛰며, trusted_projects는 확인하지 않는다
    after_migration: bool = False
    # 이관 뒤 이어서 실행하는 조치가 빌드 파드에서 쓸 수 있는 시간(분)이다.
    # 넘으면 다음 노드로 넘어갈 때 STOPPED로 멈추고, 재개하면 새 파드가 이어서 실행한다
    # 파드 수명 한도에 걸려 이미 끝난 이관까지 중단된 것처럼 보이지 않게 하기 위해서다. 0이면 한도가 없다
    chain_budget_minutes: int = 90
    # 더 이상 고칠 대상이 아닌 IVAS 조치 구분(action.actionDivCd) 값이다.
    # 이 항목은 보고서에는 남지만 계획과 성공 판단에서 뺀다
    # NE 조치 필요 / NP 조치계획 미등록 / PL 조치예정 / OT 조치일 경과 / PC 패치 완료 / IG 조치 예외.
    # 기본값은 빈 목록이고, 예외 항목을 빼려면 [PC, IG]처럼 쓴다
    ignore_actions: list[str] = Field(default_factory=list)
    # 사내 아티팩트로 볼 artifacts yml의 그룹 목록이다.
    # mandatory / optional / parents(parent, boot_parent, comp_parent)
    # 이 아티팩트 자체이거나 이를 거쳐 들어온 취약 라이브러리는 프로젝트에서 고치지 않고 inhouse_artifact로
    # 남긴다. 빈 목록이면 쓰지 않으며, 이 값은 정책 해시에 들어간다
    inhouse_groups: list[str] = Field(default_factory=lambda: ["mandatory", "optional", "parents"])
    inhouse_message: str = "해당 아티팩트는 예외처리가 가능합니다. 담당자에게 연락 부탁드립니다."
    # 보고서의 위험도 표시 기준이다. IVAS 점수가 이 값 이상이면 취약점 옆에 빨강 / 주황 / 노랑 표시를 붙인다
    # 기본값은 CVSS 심각(9.0), 높음(7.0), 보통(4.0) 구간이다. 표시만 바꾸므로 정책 해시에 들어가지 않는다
    severity_red: float = 9.0
    severity_orange: float = 7.0
    severity_yellow: float = 4.0
    max_upload_mb: int = 1024                # 업로드 ZIP 파일 크기 한도(MiB)
    max_entries: int = 200_000               # ZIP 안의 항목 수 한도
    max_unpacked_mb: int = 4096              # 압축을 풀었을 때 전체 크기 한도(MiB)
    max_entry_mb: int = 1024                 # 항목 하나를 풀었을 때 크기 한도(MiB)
    max_compression_ratio: int = 200         # zip 폭탄을 막기 위한 항목별 압축률 한도
    # false(기본값)면 always_approve 대상만 승인을 기다리고 나머지는 자동 적용한다.
    # true면 모든 제안이 승인을 기다린다
    require_approval: bool = False
    # 항상 승인이 필요한 대상이다. major(메이저 변경), parent(부모 pom 버전), bom(import한 BOM 버전),
    # managed_override(루트 pom에 override 추가)
    # XML 추가는 자동 적용하지 않으므로 managed_override는 이 목록에서 빼도 항상 승인을 기다린다
    always_approve: list[str] = Field(default_factory=lambda: ["major", "parent", "bom", "managed_override"])
    max_rounds: int = 3                      # 계획 -> 적용 -> 빌드 -> 재검사 반복 횟수 한도
    # 수정본 빌드가 실패했을 때의 처리다. include(기본값)는 실패한 수정본을 결과 ZIP으로 내고(상태 FAILED,
    # delivery.build_ok=false, 재검사 안 함) accepted/와 원본 ZIP은 그대로 둔다
    # discard는 실패한 수정본을 결과 ZIP에 넣지 않고 candidate.patch로만 남기며, 앞 라운드에서 검증된 결과 ZIP은 남는다
    failed_candidate: Literal["include", "discard"] = "include"
    # 프로젝트에서 서버 설정을 우회할 수 있는 요소를 발견했을 때 refuse(거부)하거나 ignore(무시하고 기록)한다
    project_maven_config: Literal["refuse", "ignore"] = "refuse"     # .mvn/maven.config, jvm.config, extensions.xml
    maven_wrapper: Literal["refuse", "ignore"] = "ignore"            # mvnw / .mvn/wrapper. 서버 Maven만 쓰므로 실행되지 않는다
    # 승인된 저장소 호스트 목록이다.
    # 비어 있으면 서버 settings.xml의 미러와 저장소 호스트만 승인된 것으로 본다
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
    # 요약 대조 방식이다.
    # exact는 요약의 components 건수와 라이브러리 취약점 상세 행 수가 같아야 하고, nonzero는 0인지만 본다
    # 제안 내용은 바꾸지 않으므로 정책 해시에 들어가지 않는다
    ivas_summary_check: Literal["exact", "nonzero"] = "exact"
    policy: RemediationPolicyConfig = Field(default_factory=RemediationPolicyConfig)


class Config(BaseModel):
    """전체 설정을 담는 클래스. load_config가 default.yml, --config yml, overrides, SBMIG_* 환경 변수 순으로
    합친 결과이고, 회사 고유 값은 yml에 있다."""
    target: TargetConfig = Field(default_factory=TargetConfig)
    artifacts: ArtifactsConfig
    layout: LayoutConfig = Field(default_factory=LayoutConfig)
    intake: IntakeConfig = Field(default_factory=IntakeConfig)       # v2.1j: Ant → pom synthesis at intake
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
    remediation: RemediationConfig = Field(default_factory=RemediationConfig)   # 취약점 조치 설정(/api/remediations)
    repo_root: str = str(PACKAGE_ROOT)
    # load_config가 출력한 `config: …` 경고 문장이다.
    # 실행 로그는 설정을 읽은 뒤에 연결되므로 접수 단계가 다시 기록한다
    _config_warnings: list[str] = PrivateAttr(default_factory=list)

    # ---- helpers -----------------------------------------------------
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
        """기본 권고 파일과 회사가 추가한 파일 목록을 반환한다.
        내용은 id 기준으로 합치며 뒤의 것이 우선한다.

        Base advisories file followed by the company extra files (later wins, merged by id)."""
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
# Loading
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
    """yml에서 읽은 값 안의 ``${VAR}`` / ``${VAR:-기본값}`` 플레이스홀더가 가리키는 환경 변수 이름들을
    반환한다. dict 키는 보지 않는다.
    웹 Main이 설정이 읽는 환경 변수를 알아낼 때 쓴다(``sbmig.web.contract.env_pin_names``).

    The env names the `${VAR}` / `${VAR:-default}` placeholders inside a loaded yml structure refer to (the places
    `_expand_env` expands; dict keys are not looked at)."""
    if isinstance(value, str):
        return {m.group(1) for m in _ENV_RE.finditer(value)}
    if isinstance(value, list):
        return set().union(*(placeholder_names(v) for v in value)) if value else set()
    if isinstance(value, dict):
        return set().union(*(placeholder_names(v) for v in value.values())) if value else set()
    return set()


def deep_merge(base: dict, override: dict) -> dict:
    """dict를 깊게 병합한다. 뒤의 값이 우선하며, 양쪽이 모두 dict면 재귀로 합치고 나머지는 통째로 바꾼다."""
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _apply_env_overrides(data: dict) -> dict:
    """`SBMIG_A__B=값` 환경 변수를 data["a"]["b"]에 넣는다. 밑줄 두 개가 중첩을 뜻하고 값은 YAML로 해석한다.

    SBMIG_LLM__MODEL=foo  →  data["llm"]["model"] = "foo"  (double underscore = nesting)."""
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


_DOTENV_LOADED: dict[str, float] = {}     # resolved path → mtime it was read at
_DOTENV_SET: dict[str, str] = {}          # keys this loader put into os.environ (a later edit of the file may update them)


def _read_env_text(f: Path) -> str:
    """편집기의 저장 방식과 관계없이 `.env`를 읽는다.
    UTF-8(BOM 포함), UTF-16(PowerShell 5.1의 `>`)을 먼저 시도하고 그 밖에는 cp949로 읽는다.

    `.env` text whatever the editor saved: UTF-8 (BOM or not), UTF-16 (PowerShell 5.1 `>`), else cp949."""
    data = f.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp949", errors="replace")


def dotenv_chain(start: Path) -> list[Path]:
    """``start``부터 상위 폴더로 올라가며 ``.env`` 후보를 모은다. 가까운 것이 앞에 온다.
    ``.git``이 있는 폴더까지 포함하고 거기서 멈추므로, 패키지가 하위 폴더에 있어도 저장소 루트의 ``.env``를 찾는다.

    `.env` candidates from `start` upwards, nearest first, up to and including the folder that holds `.git`
    (else the filesystem root): the repo root's `.env` is found when the package lives in a sub-folder."""
    out: list[Path] = []
    d = Path(start)
    while True:
        out.append(d / ".env")
        if (d / ".git").exists() or d.parent == d:
            return out
        d = d.parent


def load_dotenv_files(*extra: str | Path) -> list[Path]:
    """`$SBMIG_ENV_FILE`, 패키지 폴더, 현재 디렉토리에서 git 루트까지의 `.env`를 읽어 os.environ에 넣는다.
    - 먼 파일부터 읽으므로 가까운 파일이 우선하고, 현재 디렉토리의 `.env`가 패키지 쪽보다 우선한다
    - 셸에서 설정한 변수가 항상 우선하며, 파일이 바뀌면 다시 읽는다
    - 키는 Config에 넣지 않고 환경 변수로만 쓴다

    Read `KEY=VALUE` lines from `$SBMIG_ENV_FILE`, every `.env` from the git root down to the package root and from
    the git root down to the cwd into `os.environ`. A later file overrides what this loader set earlier, so the
    nearest file wins a key and `<cwd>/.env` still beats the package's; shell variables win everything;
    `export KEY=...`, quotes and `#` comments are accepted. The files are git-ignored and are never read into the
    config itself: keys still reach sbmig only through environment variables."""
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
            if _DOTENV_LOADED.get(key) == mtime:          # read before and unchanged since
                found.append(f)
                continue
            text = _read_env_text(f)
        except (OSError, UnicodeError):
            continue
        found.append(f)
        _DOTENV_LOADED[key] = mtime                       # an edited file is read again (web server, fleet)
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
                v = ""           # `KEY=   # comment` is an empty value (2026-10-23: a company .env sent the comment
                                 # text of LLM_RECIPE_MODEL / LLM_ESCALATION_MODEL as the model name → HTTP 400)
            else:
                v = v.split(" #", 1)[0].rstrip()
            # 셸에서 직접 설정한 변수는 덮어쓰지 않는다. 이 로더가 넣은 값만 파일이 바뀌면 함께 바뀐다
            # shell variables win; a value this loader set earlier follows the edited file
            if k and (k not in os.environ or (k in _DOTENV_SET and os.environ[k] == _DOTENV_SET[k])):
                os.environ[k] = v
                _DOTENV_SET[k] = v
    refresh_secrets()          # 방금 읽은 .env에 비밀 값이 있을 수 있어 마스킹 대상을 갱신한다
    return found


class _YamlLoader(yaml.SafeLoader):
    """중복 키를 기록하는 YAML SafeLoader 클래스.
    PyYAML은 같은 매핑에 키가 두 번 나오면 마지막 값만 남기고 앞의 값을 조용히 버리기 때문이다.

    A SafeLoader that records duplicate mapping keys as (key, first line, second line): PyYAML silently keeps the LAST
    value, so a company yml with two `build:` blocks loses the first block's values."""

    def __init__(self, stream: Any) -> None:
        super().__init__(stream)
        self.duplicate_keys: list[tuple[str, int, int]] = []

    def construct_mapping(self, node: Any, deep: bool = False) -> Any:
        # `<<: *anchor` (YAML merge keys): SafeConstructor folds them in flatten_mapping — do that first, or the merge tag
        # reaches construct_object with no constructor and a yml that shares a block between sections stops loading
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
    """yml 파일 하나를 읽는다. 중복 키가 있으면 파일명, 키, 두 줄 번호와 함께 WARNING을 남기고, 최상위가
    매핑이 아니면 파일을 무시한다는 WARNING을 남긴다.
    경고 문장은 실행 로그에 다시 기록할 수 있도록 ``sink``에도 모은다.

    Read one yml; a duplicate key is a WARNING naming the file, the key and both lines (the last block wins); a non-mapping
    top level is a WARNING (the file is ignored). Sentences also go to `sink`."""
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


# the keys _warn_removed_keys explains — _warn_unknown_keys does not report them a second time
_REMOVED_KEYS = frozenset({"fix.mode", "fix.edits_max_asks_per_case", "build.pom_repositories"})


def _section_model(annotation: Any) -> Optional[type[BaseModel]]:
    """필드 타입이 설정 섹션(BaseModel, Optional 포함)이면 그 모델을, 아니면 None을 반환한다.
    dict / list는 섹션으로 보지 않는다.

    The BaseModel behind a field annotation (`Model` or `Optional[Model]`), else None; dict / list fields are not sections."""
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
    항목 이름은 자유롭게 쓸 수 있고 항목 안의 키만 검사한다.

    The value model of a `dict[str, Model]` field: entry names are free, the keys inside each entry are checked."""
    if get_origin(annotation) is dict:
        args = get_args(annotation)
        if len(args) == 2 and isinstance(args[1], type) and issubclass(args[1], BaseModel):
            return args[1]
    return None


def _env_override_dict() -> dict:
    """알 수 없는 키를 검사하기 위해 ``SBMIG_<섹션>__<키>`` 환경 변수만 모아 중첩 dict로 만든다.
    밑줄 두 개가 없는 ``SBMIG_WEB_CONFIGS`` 같은 이름은 설정 키가 아니므로 뺀다.
    중첩 경로가 서로 겹치는 조합은 무시한다.

    The `SBMIG_<SECTION>__<KEY>` variables as a nested dict (placeholder values) for the unknown-key check; names
    without a double underscore (SBMIG_WEB_CONFIGS …) are not config keys and are left out."""
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
    pydantic은 모르는 키를 조용히 버리므로 들여쓰기 실수를 놓치기 쉽기 때문이다.
    섹션 필드만 따라 내려가고 dict / list 필드는 보지 않는다. 기록한 문장 목록을 반환한다(테스트용).

    Log every key the Config models do not declare (pydantic drops unknown keys silently): a mis-indented key is the
    typical cause. Descends into section fields only (never into dict / list fields); a name that belongs to exactly
    known sections gets a "did you mean" hint. Returns the sentences logged (for tests)."""
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
    """회사 yml에 남은 이전 키를 경고로 알린다. 제거된 `fix.mode` / `fix.edits_max_asks_per_case`와,
    `script.max_llm_tries_per_case` 없이 쓴 `script.max_augments_per_case`가 대상이다.
    조용히 넘어가면 설정의 뜻이 바뀐 것을 알 수 없기 때문이다. 기록한 문장 목록을 반환한다(테스트용).

    v2.1z: log the keys a company yml may still carry — the removed `fix.mode` / `fix.edits_max_asks_per_case` (ignored) and
    `script.max_augments_per_case` without `script.max_llm_tries_per_case` (augments only now; the per-case LLM total is the new
    key, default 20). Returns the sentences logged (for tests)."""
    out: list[str] = []
    fix = data.get("fix") if isinstance(data.get("fix"), dict) else {}
    for key in ("mode", "edits_max_asks_per_case"):
        if key in fix:
            out.append(f"config: fix.{key}={fix[key]!r} is ignored since v2.1z (the edits mode is gone; the fix is always the "
                       "script ladder) — remove it from the yml")
    # `build.pom_repositories`는 더 이상 쓰지 않으며, 폐쇄망에서는
    # settings.xml의 catch-all 미러(mirrorOf external:*)를 쓴다
    # 회사 yml에 남아 있으면 조용히 무시되므로 경고한다
    build = data.get("build") if isinstance(data.get("build"), dict) else {}
    if "pom_repositories" in build:
        out.append(f"config: build.pom_repositories={build['pom_repositories']!r} is ignored since v2 (the strip handler went with "
                   "the v1 handlers; pom-declared repositories are covered by the settings.xml mirror `mirrorOf external:*`, see "
                   "docs/RUNBOOK.md §0.1) — remove it from the yml")
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
    순서는 default.yml -> 주어진 yml들 -> overrides -> SBMIG_* 환경 변수 -> `${VAR}` 치환이다.
    모든 명령과 sbmig.api.migrate가 이 함수로 시작하며, .env는 그 전에 환경 변수로 읽힌다.

    Load default.yml, then each given yml (artifacts yml, site yml ...), then dict overrides, then env
    (`.env` files are read into the environment first, see `load_dotenv_files`)."""
    load_dotenv_files()
    data: dict = {}
    warnings: list[str] = []
    files = [DEFAULT_CONFIG_PATH, *[Path(p) for p in paths]]
    for f in files:
        if f and Path(f).exists():
            data = deep_merge(data, _read_yaml(Path(f), warnings))
    # the yml keys only: SBMIG_* env variables that are not config keys (SBMIG_WEB_CONFIGS, SBMIG_WEB_TOKEN, SBMIG_MAX_UPLOAD_MB)
    # also land in `data` through _apply_env_overrides and are dropped by pydantic on purpose — they are not a typo
    warnings += _warn_unknown_keys(data)
    # the SBMIG_<SECTION>__<KEY> overrides (k8s ConfigMap env, 2026-11-05): a mistyped name is reported the same way
    warnings += _warn_unknown_keys(_env_override_dict(), label="env override SBMIG_*__*: ")
    if overrides:
        data = deep_merge(data, overrides)
    data = _apply_env_overrides(data)
    data = _expand_env(data)
    if repo_root:
        data["repo_root"] = str(repo_root)
    warnings += _warn_removed_keys(data)
    cfg = Config.model_validate(data)
    cfg._config_warnings = warnings
    return cfg
