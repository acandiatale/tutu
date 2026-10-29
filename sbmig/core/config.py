"""설정 스키마와 로더 — 모든 명령과 sbmig.api.migrate 가 처음에 load_config 로 Config 를 만든다.
합치는 순서(뒤가 이김): default.yml → --config yml(회사 artifacts yml) → overrides → SBMIG_* 환경 변수.
`.env`(git 무시)는 os.environ 으로만 들어간다. LLM 키는 환경 변수(LLM_API_KEY)에서만 오고 yml·로그에 절대 쓰지
않는다. 회사 고유 값(사내 좌표, 가격, 게이트웨이)은 yml 에 두고 코드에 넣지 않는다(불변조건 2). 키를 더하거나
바꾸면 config/default.yml 과 docs/INTERFACES.md 도 함께 맞춘다.

Configuration schema + loader.

Layering (later wins):  config/default.yml  →  --config file(s)  →  environment overrides (SBMIG_* / ${VAR} expansion).
Company-specific coordinates live in a separate artifacts yml so the code base stays generic.


"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, Field, ValidationInfo, field_validator

log = logging.getLogger("sbmig.config")

from .models import GAV

PACKAGE_ROOT = Path(__file__).resolve().parents[2]          # repo root when running from source
DEFAULT_CONFIG_PATH = PACKAGE_ROOT / "config" / "default.yml"


class TargetConfig(BaseModel):
    """목표 버전(Java 21, Spring Boot 4.1.0, Spring Framework 7)."""
    java_version: str = "21"
    spring_boot_version: str = "4.1.0"
    spring_framework_version: str = "7.0.x"


class ArtifactSpec(BaseModel):
    """사내 아티팩트 하나의 좌표(artifacts yml). aliases = 같은
    것으로 볼 옛 g:a 키, property_name = 버전을 담는 관례 속성."""
    group_id: str
    artifact_id: str
    version: Optional[str] = None            # None → version managed by the parent
    scope: Optional[str] = None
    type: Optional[str] = None
    # optional artifacts: how to find the as-is usage (defaults to same group/artifact)
    aliases: list[str] = Field(default_factory=list)   # old "g:a" keys that should be treated as this artifact
    property_name: Optional[str] = None       # if the version is conventionally kept in a property

    def gav(self) -> GAV:
        """이 설정을 GAV 모델로."""
        return GAV(group_id=self.group_id, artifact_id=self.artifact_id, version=self.version,
                   scope=self.scope, type=self.type)

    @property
    def key(self) -> str:
        """`groupId:artifactId` 키(버전 없음)."""
        return f"{self.group_id}:{self.artifact_id}"


class ArtifactsConfig(BaseModel):
    """사내 아티팩트 설정(artifacts yml): 루트 부모 base-parent, 역할 부모 boot-jar / comp-jar, 필수 2개,
    선택 8개. 실제 좌표는 회사 yml 에만 있다(config/artifacts.example.yml 은 자리표시자) — 코드에 회사
    좌표를 지어 넣지 않는다."""
    parent: ArtifactSpec                      # the root pom's parent (company: `base-parent`)
    # round 9b — the company layout has role parents: `module/boot-jar/pom.xml` inherits the in-house `boot-jar`
    # parent and `module/comp-jar/pom.xml` the in-house `comp-jar` parent (all in-house artifacts share one line,
    # e.g. 4.0.0); the root pom (base-parent) is then an aggregator the modules do NOT inherit from. None = the
    # modules inherit the root pom (pre-9b behaviour). An as-is module whose parent has the same groupId:artifactId
    # as a role parent (an older migration on 1.0.0) is always moved to the configured version.
    # 역할 부모: `{name}-boot` 모듈은 boot_parent(boot-jar), 그 밖의 모듈은 comp_parent(comp-jar)를 상속한다.
    # 둘 다 None 이면 모듈이 루트 pom 을 상속한다(9b 이전 방식).
    boot_parent: Optional[ArtifactSpec] = None
    comp_parent: Optional[ArtifactSpec] = None
    parent_manages_spring_boot: bool = True   # if False we import spring-boot-dependencies BOM ourselves
    parent_manages_java_version: bool = True  # if False we set maven.compiler.release
    # 필수 사내 아티팩트(운영 설정은 정확히 2개): mandatory_target 에 따라 모든 루트 pom 에 선언한다.
    mandatory: list[ArtifactSpec] = Field(default_factory=list)   # exactly 2 in production config
    # 선택 사내 아티팩트(8개): 이미 쓰고 있을 때만 버전을 올린다(optional_version_policy).
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
        """역할 부모(boot_parent·comp_parent)가 둘 다 설정됐는지.

        True when the modules inherit their own in-house parents (boot_parent / comp_parent both set)."""
        return self.boot_parent is not None and self.comp_parent is not None

    def parent_specs(self) -> list["ArtifactSpec"]:
        """설정된 사내 부모 전부: 루트 부모, 그다음 boot_parent / comp_parent.

        Every configured in-house parent: root parent, then boot_parent / comp_parent when set."""
        return [s for s in (self.parent, self.boot_parent, self.comp_parent) if s is not None]

    def parent_keys(self) -> list[str]:
        """사내 부모를 뜻하는 g:a 키(별칭 포함). 이것을 해석하지 못하면
        실행이 중단된다(settings·Nexus 문제라 코드로 못 고침).

        `g:a` keys (incl. aliases) that mean "a configured in-house parent" — an unresolvable one aborts the run."""
        out: list[str] = []
        for s in self.parent_specs():
            for k in (s.key, *s.aliases):
                if k not in out:
                    out.append(k)
        return out


class LayoutConfig(BaseModel):
    """회사 레이아웃(라운드 9c): 루트 바로 아래 `{name}-boot`(Boot 설정만, boot-jar) / `{name}-back`(모든
    소스, comp-jar) / 웹 리소스가 있을 때만 `{name}-front`. Boot as-is 프로젝트는 디렉토리를 두고 pom 만
    바꾼다. 9c 이전 레이아웃은 이 키들로 되살린다(config/artifacts.local-e2e.yml).

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
    # 웹 루트 안의 <resource> 디렉토리 파일은 comp classpath 에 남기면서 front 의 static 에도 웹 경로 그대로
    # 복사한다(as-is WAR 가 그 경로로 서빙했으므로).
    front_copy_resource_dirs: bool = True
    # v2.1h (2026-10-28, user: "JSP 도 javax → jakarta 로 변경해 줘야 할 것 같아"): OpenRewrite never parses JSPs, so the
    # restructure renames the javax EE names of the analyzer's table (sbmig/analyze/jakarta_packages.yml) inside the
    # code regions of every JSP / JSPF / JSPX / tag file (page imports, useBean class / type, scriptlets; Java
    # literals and comments untouched) in every mode, after the files reached their final place.
    # False = the pre-10-28 behaviour (Handover follow-up only).
    # JSP 의 javax EE 이름을 jakarta 로 바꾼다(레시피가 JSP 를 못 보므로 재구성이 직접). 끄면 인수인계 항목만.
    jsp_jakarta_rename: bool = True
    # Boot as-is 프로젝트: in-place = 디렉토리 유지·pom 만
    # 수정(회사 규칙), layout = 다른 경우처럼 모듈로 나눔.
    boot_as_is: Literal["in-place", "layout"] = "in-place"   # Spring Boot as-is: keep directories (poms only) | split
    boot_module_pattern: str = "*-boot"                 # module dirs that are boot apps in Boot as-is / MSA (pom confirms)
    jsp_boot_packaging: Literal["war", "jar"] = "war"   # JSPs present → the boot module is a WAR for external Tomcat
    boot_artifact_suffix: str = "-boot"
    comp_artifact_suffix: str = "-back"                 # artifactId = <root artifactId> + suffix (front: "-front")
    split_policy: Literal["minimal", "web-layer"] = "minimal"
    # as-is 다중 모듈: merge = 소스를 comp(back) 모듈 하나로
    # 합침(기본, 회사 레이아웃), wrap = 기존 모듈을 감싸기만 함.
    multi_module_policy: Literal["wrap", "merge"] = "merge"
    # 2026-10-23: no pom.xml at the project root but sibling poms that depend on each other (e.g. APP/BATCH, APP/WEB,
    # APP/EAR built one by one and packaged by the EAR) → intake writes a root aggregator pom listing them, so they
    # build in one reactor and the merge policy applies (sbmig.analyze.aggregator)
    # 루트에 pom 이 없고 서로 의존하는 형제 pom 만 있으면
    # 접수가 루트 집계 pom 을 만들어 한 reactor 로 빌드한다.
    generate_aggregator: bool = True
    # merge: a path present in more than one module → first module in <modules> order wins, the other copies are
    # saved under <run dir>/merge-conflicts/ and listed in the Handover ("first-wins"); "fail" = restructure error
    merge_collision: Literal["first-wins", "fail"] = "first-wins"
    # round 9: after the in-house parent is set, drop explicit <version>s the new parent chain manages
    # (`mvn help:effective-pom` after the pom edits): gte = only when the managed version is ≥ the explicit one
    # (a deliberately newer pin stays), any = always, off = keep every explicit version. Mandatory / optional /
    # parent artifacts from `artifacts` and <scope>system</scope> dependencies are never touched.
    # 새 부모 체인이 관리하는 명시 <version> 을 지운다. gte = 관리 버전이 같거나 높을 때만(일부러 올린 고정은
    # 유지). 사내 아티팩트와 system scope 의존성은 건드리지 않는다.
    drop_managed_versions: Literal["gte", "any", "off"] = "gte"
    application_class_name: str = "Application"
    boot_resources_glob: list[str] = Field(default_factory=lambda: [
        "application*.yml", "application*.yaml", "application*.properties",
        "bootstrap*.yml", "bootstrap*.properties", "logback*.xml", "log4j2*.xml", "banner.txt",
    ])
    # 프로젝트 종류별로 boot 모듈에 넣을 Boot starter(spring_boot 는 as-is 의 starter 를 그대로 둔다).
    starter_by_case: dict[str, str] = Field(default_factory=lambda: {
        "pure_java": "spring-boot-starter",
        "servlet_tomcat": "spring-boot-starter-webmvc",
        "spring_legacy": "spring-boot-starter-webmvc",
        "spring_boot": "auto",          # keep whatever starters the as-is project already has
    })
    add_servlet_component_scan: bool = True
    # 2026-10-23: web.xml servlets / filters / listeners / error pages / session timeout → a generated
    # `WebXmlRegistrations` @Configuration in the boot module (`generate`), or follow-ups only (`followup`)
    # web.xml 의 서블릿·필터·리스너·오류 페이지 → generate = boot 모듈에 WebXmlRegistrations @Configuration
    # 생성, followup = 인수인계 후속 작업으로만 남김(Boot WAR 는 web.xml 을 읽지 않으므로 기본은 generate).
    web_xml_registrations: Literal["generate", "followup"] = "generate"
    import_xml_bean_configs: bool = True   # @ImportResource for detected <beans> xml files
    msa_parent_policy: Literal["per-service-root", "top-aggregator"] = "per-service-root"

    def module_name(self, which: Literal["boot", "comp", "front"], name: str) -> Optional[str]:
        """역할(boot / comp / front)의 모듈 디렉토리 이름(`{name}` 펼침). front 가 꺼져 있으면 None.

        The module directory name for a role (`{name}` expanded); None when the role has no module (front off)."""
        tpl = {"boot": self.boot_module, "comp": self.comp_module, "front": self.front_module}[which]
        return tpl.replace("{name}", name) if tpl else None

    def module_path(self, which: Literal["boot", "comp", "front"], name: str) -> Optional[str]:
        """역할 모듈의 루트 기준 posix 경로(module_dir + 이름), 예 `shop-boot` 또는 `module/boot-jar`.

        Root-relative posix path of a role module (`module_dir` + name), e.g. `shop-boot` or `module/boot-jar`."""
        n = self.module_name(which, name)
        if n is None:
            return None
        d = self.module_dir.strip("/")
        return f"{d}/{n}" if d else n


class RecipeSetConfig(BaseModel):
    """실행할 OpenRewrite 레시피 묶음. v2 결정(2026-10-26): Java 21, javax → jakarta, Boot 프로젝트면 Boot
    4 레시피만 재구성 전에 한 번에 돈다. 빈 묶음은 회사 설정이 다시 켤 수 있게 남겼다(그때는 catalog.yml 의
    `sets:` 에도 올려야 함)."""
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
    spring_framework_7: list[str] = Field(default_factory=list)
    spring_security_7: list[str] = Field(default_factory=list)
    junit5: list[str] = Field(default_factory=list)
    extra: dict[str, list[str]] = Field(default_factory=dict)


class OpenRewriteConfig(BaseModel):
    """OpenRewrite 실행 설정(transform.rewrite): 플러그인·레시피 BOM 버전, 레시피 아티팩트, 시간 제한, JVM
    메모리, 다운로드 재시도, 파싱 제외 glob."""
    plugin_version: str = "6.46.1"
    # 레시피 BOM 은 3.37.0 고정(불변조건 5): 3.38.0 은 Maven
    # Central 에서 어긋나 있다. 3.37.0 → rewrite-spring 6.37.0.
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
    # 다운로드 실패(프록시 429/5xx, 연결 끊김, 시간 초과)로 레시피
    # 실행이 깨지면 기다렸다가 다시 돌린다(0 = 재시도 없음).
    download_retry: int = 1
    download_retry_wait_s: int = 30
    # round 9c: source globs the plugin must not parse (`-Drewrite.exclusions`). OpenRewrite's XML parser also accepts
    # .jsp/.jspx/.html/.htm; JSP scriptlets and `<` inside attribute strings only produce ANTLR "token recognition
    # error" noise (the file is skipped) and no recipe of ours touches them
    # JSP / HTML 은 파싱하지 않는다(어느 레시피도 건드리지 않고 ANTLR 경고만 쏟아짐). 그래서 JSP 안의 javax 는
    # v2 에서 바뀌지 않는다 — 인수인계 섹션이 안내한다.
    exclusions: list[str] = Field(default_factory=lambda: ["**/*.jsp", "**/*.jspx", "**/*.html", "**/*.htm"])
    # (현재 코드에서 읽지 않는 키)
    fail_on_dry_run_results: bool = False
    plugin_goal: str = "run"                            # run | dryRun


class BuildConfig(BaseModel):
    """Maven 빌드 설정(transform.compile, verify.build, 수정 루프의 재빌드): mvn 경로, settings.xml, 로컬
    저장소, JDK 21 홈, 시간 제한, 테스트 생략, 옛 소스 인코딩 후보, 버전 조회 저장소."""
    mvn: str = "mvn"
    settings_file: Optional[str] = None
    local_repo: Optional[str] = None
    java_home: Optional[str] = None                     # JDK 21 home; None → inherit
    offline: bool = False
    extra_args: list[str] = Field(default_factory=lambda: ["-B", "-ntp", "-Dstyle.color=never"])
    skip_tests: bool = True                             # -DskipTests (tests still compile)
    # 테스트는 범위 밖(사용자 2026-10-22 "테스트는 다 스킵"): 모든 빌드에 -Dmaven.test.skip=true 를 붙이고
    # 보고서에 tests-not-compiled 플래그와 후속 작업을 남긴다.
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
    # 옛 소스 인코딩 후보. JDK 18+ 는 기본이 UTF-8 이라 MS949 / EUC-KR 소스가 "unmappable character" 로 깨진다
    # — 구조 검토와 스크립트 헬퍼가 이 순서로 판별한다.
    encoding_candidates: list[str] = Field(default_factory=lambda: ["utf-8", "cp949", "euc-kr", "shift_jis", "latin-1"])
    # <repositories> / <pluginRepositories> declared in the project poms (old Nexus URLs, Central, Spring milestones)
    # are left as they are; route them through a `settings_file` mirror (mirrorOf external:*) — the plan notes say
    # whether the mirror covers them
    # package prefixes treated as *library* code in error signatures (project-owned FQCNs are masked as '*').
    # In-house library namespaces (parent/mandatory/optional group ids) are added automatically.
    # 오류 서명에서 '라이브러리 코드' 로 볼 패키지 접두어(프로젝트 소유 FQCN 은 '*' 로 가려 케이스가
    # 프로젝트를 넘어 재사용되게 한다).
    external_package_prefixes: list[str] = Field(default_factory=list)
    # round 10: the preflight aborts (flag `jdk-not-21`) when `mvn -v` reports another Java major (0 = no check)
    # 사전 점검: `mvn -v` 의 Java 메이저가 다르면 중단한다(플래그 jdk-not-21, 0 = 점검 안 함).
    require_java_major: int = 21
    # "… was cached in the local repository, resolution will not be reattempted …" (a Nexus 404 remembered for a day)
    # → the same build once more with -U before the error is triaged
    # Nexus 404 가 로컬 저장소에 하루 동안 기억된 경우, 트리아지 전에 같은 빌드를 -U 로 한 번 더 돌린다.
    retry_negative_cache: bool = True
    # v2.1d: where sbmig reads `maven-metadata.xml` / poms to tell the LLM which releases exist (same major first).
    # None → the catch-all <mirror> of `settings_file` (its <server> credentials, `${env.X}` resolved), else Maven
    # Central unless `offline`; a URL here wins
    # v2.1d: 어떤 릴리스가 있는지 LLM 에 알려 주려고 maven-metadata.xml 을 읽을 저장소. None 이면
    # settings_file 의 catch-all 미러, 그것도 없으면 Maven Central(offline 이 아닐 때).
    repository_url: Optional[str] = None
    repository_timeout_seconds: int = 10


class PhaseLoopConfig(BaseModel):
    """단계별 수정 루프 설정(빌드 goal, 최대 라운드, 켜짐 여부). v2.1 은 verify 만 켜져 있다."""
    goal: str = "compile"
    max_iterations: int = 5
    enabled: bool = True


class LoopConfig(BaseModel):
    """수정 루프 설정: 라운드 수, LLM 호출·시간 한도, 정체·진동 판정, 라운드당 케이스 수, 악화 시 되돌림,
    차단 정책, 실행 엔진(graph / inline)."""
    # v2: 중간 빌드가 없다 — jdk21 / spring / restructure 루프는 꺼져 있고, 변환·재구성·검토 뒤 첫
    # 빌드(verify)의 수정 루프가 전부 고친다(최대 50 라운드).
    # v2: no intermediate builds — the jdk21 / spring / restructure loops are off; the first build is the final
    # `mvn clean package` after transform + restructure + review, and its fix loop (`verify`) does all the fixing
    phases: dict[str, PhaseLoopConfig] = Field(default_factory=lambda: {
        "jdk21": PhaseLoopConfig(goal="compile", max_iterations=3, enabled=False),
        "spring": PhaseLoopConfig(goal="test-compile", max_iterations=6, enabled=False),
        "restructure": PhaseLoopConfig(goal="package", max_iterations=6, enabled=False),
        "verify": PhaseLoopConfig(goal="package", max_iterations=50),   # v2.1: "라운드(빌드) 50회"
    })
    # 프로젝트당 LLM 호출 상한: 0 = 무제한, 양수 = 상한, -1 = 호출 금지(플릿 전체 예산을 다 쓴 경우).
    max_llm_calls: int = 0                   # 0 = no limit on LLM calls (v2.1); > 0 = a hard cap per project;
                                             # -1 = no LLM call at all (a fleet whose max_llm_calls_total is used up)
    max_total_minutes: int = 240
    # 같은 오류 집합이 N 번 되풀이되면 정체로 보고 전략을 바꾼다.
    stall_limit: int = 2                     # identical error multiset N times → escalate strategy
    # 한 라운드에 손대는 케이스 수 상한(근본 원인 케이스 먼저).
    max_clusters_per_iteration: int = 8
    revert_on_regression: bool = True
    regression_ratio: float = 1.5            # errors_after > errors_before * ratio → regression
    # 불변조건 1(빌드 게이트): 무슨 일이 있어도 마지막에 `mvn clean package` 를 한 번 더 돌린다.
    always_final_verify: bool = True         # `mvn clean package` at the end no matter what
    # graph = LangGraph StateGraph 가 단계를 몬다(회사 관례,
    # 기본), inline = 옛 루프. 보고서는 같다(테스트로 보장).
    engine: Literal["graph", "inline"] = "graph"   # graph: LangGraph StateGraph drives the phases (company
                                             # convention, sbmig.graph); inline: the original in-process loop
    oscillation_window: int = 4              # error-set history compared for A↔B toggling (≥ stall detection window)
    # 차단 사유별로 LLM 스크립트 시도를 몇 번 허용할지(0 = 바로 차단). 사내 부모를 못 찾는 것은 settings /
    # Nexus 문제라 코드로 못 고치므로 0 이다. 저장된 스크립트는 이 값과 상관없이 재사용한다.
    # how many LLM attempts a cluster of this BlockReason may get before it is blocked (0 = block immediately).
    # v2: no deterministic handler swaps a javax library, bumps a missing version or a JDK-21-incompatible plugin any
    # more, so those get LLM script attempts (a stored script is reused regardless); an unresolvable in-house parent
    # stays 0 (settings / Nexus — no code change fixes it)
    blocked_policy: dict[str, int] = Field(default_factory=lambda: {
        "external_javax_api": 2, "external_api_incompatible": 2, "artifact_not_found": 2, "parent_not_found": 0,
        "plugin_incompatible": 2, "unsupported_source": 0, "encoding": 0,
        "reactor_duplicate": 0,                      # v2.1k: a pom structure problem — handed over, never asked of the LLM
    })
    # pom 수정 뒤 이 종류의 오류가 나타나면 개수와 상관없이 되돌린다(LLM
    # 이 지어낸 의존성 하나가 모든 컴파일 오류를 가린 사례).
    structural_regression_kinds: list[str] = Field(default_factory=lambda: [
        "pom_invalid", "parent_resolution", "plugin_resolution", "dependency_resolution"])
                                             # appearing after a pom edit ⇒ revert regardless of count (round 10: a
                                             # made-up dependency an LLM added masked every compile error)


BUILTIN_LLM_PROVIDERS = ("company", "openai_compatible", "anthropic", "fake")


class ModelPrice(BaseModel):
    """모델 하나의 1M 토큰당 가격(`llm.pricing.<model>`, 통화는 llm.pricing_currency).

    `llm.pricing.<model>`: list price per 1M tokens (currency = llm.pricing_currency)."""
    input_per_mtok: float
    output_per_mtok: float
    cached_input_per_mtok: Optional[float] = None   # informational: sbmig cannot see cache hits in the gateway reply


class LLMConfig(BaseModel):
    """LLM 연결 설정. provider = company(회사 게이트웨이, config/default.yml 의 기본) / openai_compatible /
    anthropic / fake(재생) / plugin / 등록된 어댑터 이름. 키는 환경 변수(`api_key_env`, 기본
    LLM_API_KEY)에서만 읽는다.

    `provider` is one of BUILTIN_LLM_PROVIDERS, `plugin` (→ `plugin` = "pkg.module:factory"), or the name of an
    adapter registered under the `sbmig.llm_adapters` entry-point group (in-house gateway / agent platform)."""
    # 클래스 기본값은 openai_compatible 이지만 config/default.yml 이 company 로 덮어쓴다.
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
        """모델 이름은 한 토큰이다: 끝의 `# 주석` 과 공백을 떼고, 주석뿐인 값은 비운다. 회사 .env 의 주석이
        모델 이름으로 넘어가 게이트웨이가 실행 중에 HTTP 400 을 낸 사례 때문.

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
    # round 9 (L5): the second consecutive transport/config failure pauses the loop once per run for this long and
    # retries; only a second streak marks the LLM unavailable for the rest of the run. 0 = no cool-down (old behaviour)
    # 전송·설정 실패가 두 번 연속이면 실행당 한 번 이만큼 쉬고
    # 재시도한다. 다시 연달아 실패하면 그 실행 동안 LLM 사용 불가로 본다.
    cooldown_seconds: int = 60
    # LLM 이 케이스 하나에서 파일을 더 달라고 할 수 있는 횟수.
    max_context_rounds: int = 2              # how many times the LLM may ask for more files per cluster
    extra_headers: dict[str, str] = Field(default_factory=dict)
    prompt_version: str = "v1"
    record_dir: Optional[str] = None         # record requests/responses for replay tests
    # company 공급자는 requests.post 한 번(Bearer LLM_API_KEY, json = model + messages + payload_extra)이다.
    # 게이트웨이 503 이후 사용자가 이 단순한 형태를 원했다 — LangChain·httpx 옵션을 더하지 않는다.
    # provider `company` (default): requests.post(LLM_BASE_URL, headers={Authorization: Bearer LLM_API_KEY},
    # json={"model", "messages", **payload_extra}, timeout=timeout_seconds) — see sbmig/fix/company.py
    payload_extra: dict[str, Any] = Field(default_factory=dict)      # extra top-level payload fields, sent verbatim
    response_path: Optional[str] = None      # dot path to the reply text, e.g. "data.result.content"; None → auto-detect
    # 2026-10-22: per-project API cost in the report. Keys are model ids as the gateway names them (exact match first,
    # then the longest key that is a prefix of / contained in the model id, case-insensitive); unmatched models are
    # listed as unpriced. Company specifics live in config/default.yml / the company config, never in code.
    # 모델별 가격(보고서의 비용 표). 회사 가격은 yml 에만 둔다.
    pricing: dict[str, ModelPrice] = Field(default_factory=dict)
    pricing_currency: str = "USD"
    pricing_note: str = ""                   # printed under the cost table (source + date of the prices)


class ScriptConfig(BaseModel):
    """v2 수정 스크립트 실행 설정(sbmig.script): `def fix(p)` 를 정적 검사 후 `python -I` 별도 프로세스에서
    헬퍼 API `p`(프로젝트 폴더만)로 돌린다. 적대적 작성자를 막는
    샌드박스가 아니라 실수로 밖을 건드리는 것을 막는 가드다(불변조건 7).

    v2 fix scripts (sbmig.script): the LLM answers with `def fix(p)`; it runs in a separate `python -I` process
    against the helper API `p` (project-folder files only), after a static check (allowed imports only; no open /
    exec / eval / os / subprocess / socket / dunder access). Not a sandbox against a hostile author — a guard
    against a script that reaches outside the project by mistake (docs/v2/PLAN-v2.md §3.2)."""
    timeout_seconds: int = 60                # per run; the process tree is killed after it (status timeout)
    # 스크립트 프로세스의 인터프리터. pydantic·lxml 이 설치돼
    # 있어야 한다(없으면 실행이 ModuleNotFoundError 를 보고).
    python: Optional[str] = None             # interpreter for the script process; None = the one running sbmig
    extra_allowed_imports: list[str] = Field(default_factory=list)   # added to the built-in whitelist (company call)
    max_code_chars: int = 30000              # a longer script is refused (status rejected)
    log_lines: int = 200                     # p.log lines kept per run
    max_files_changed_warn: int = 40         # a run that changes more files gets a warning note (the diff is kept)
    # 사다리 한도: 저장된 스크립트 재사용(reuse_versions 개, LLM 없음) → 보강(max_augments_per_case 번) →
    # 그래도 안 되면 BLOCKED(repeated_failure)로 인수인계.
    reuse: bool = True                       # same case seen before → run its best stored version first (no LLM)
    reuse_versions: int = 1                  # stored versions tried per case and run before the LLM is asked
    max_augments_per_case: int = 3           # LLM extensions per case and run; then the case is BLOCKED
                                             # (repeated_failure) and handed over


class ErrorsConfig(BaseModel):
    """v2.1 오류 목록 설정: 실행 디렉토리 errors.jsonl + errors.md, 끝에 KB errors.jsonl 에 덧붙임. diff·raw
    길이 상한과 v2.1e 학습 기록(변경 조각, 스크립트 실행 이력)의 크기.

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
    """v2.1d 의존성 버전 정책 설정: LLM 은 스스로 메이저를 올리거나 라이브러리를 바꾸지 않는다 — 같은 메이저의
    jakarta 릴리스가 먼저, 없을 때만 더 높은 메이저 + 운영자 확인
    가이드. 사람이 `<kb>/libraries.yml` 로 표를 늘린다(재배포 없음).

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


class ReviewConfig(BaseModel):
    """구조 검토 설정(restructure.review): 재구성 뒤 첫 빌드 전에 LLM 이 pom·의존성·모듈을 회사 규칙으로
    검토한다. critical 은 바로 수정(커밋, 되돌릴 수 있음), warning
    은 보고서 'Structure review' 와 오류 목록 메모로 남는다.

    v2 structure review (sbmig.review): after the restructure, before the first build, the LLM checks the poms,
    dependencies and module layout against the company rules. Critical findings → one fix script (committed, can be
    reverted); warnings → the report section "Structure review" and <kb>/reviews.jsonl."""
    enabled: bool = True
    fix_critical: bool = True                # run the review's script when it reports critical issues
    max_pom_chars: int = 60000               # all pom text in the prompt; longer poms are cut (head + tail)
    max_poms: int = 40
    # (v2.1 에서는 읽지 않는 키 — 검토는 오류 목록에 기록되고 KB reviews.jsonl 은 쓰지 않는다)
    record: bool = True                      # one line per run in <kb>/reviews.jsonl


class FixConfig(BaseModel):
    """어댑터 2단계(tier 2): 수정기 전체를 사내 코딩 에이전트로 바꾼다. plugin = "pkg.module:attr"(FixerLike
    클래스 또는 팩토리) — propose_script 계약을 지켜야 한다(불변조건 4).

    Second adapter tier: replace the whole fixer (our SEARCH/REPLACE protocol over a chat completion) with an
    in-house coding agent. `plugin` = "pkg.module:attr" where attr is a FixerLike class or a factory
    `fn(options: dict, cfg: Config, llm: LLMClientLike | None) -> FixerLike`."""
    plugin: Optional[str] = None
    plugin_options: dict[str, Any] = Field(default_factory=dict)
    # v2.1i (2026-10-28, user: "스크립트 만드는 부분을 껐다 켰다 하는 기능" — a fair token comparison for the business
    # team): `script` = the v2 ladder (the LLM answers with edits AND a reusable fix script, stored per case and reused on
    # later projects); `edits` = the LLM answers with edits only — no script rules, no helper API in the prompt, no
    # script storage / reuse / augment; every try is a fresh ask (same per-case LLM asks as script mode: script.max_augments_per_case).
    # 수정 방식: script = 스크립트 사다리(기본), edits = LLM 이 수정 내용만(스크립트 생성·저장·재사용 없음) — 토큰 비교용.
    mode: Literal["script", "edits"] = "script"
    # edits 모드에서 케이스당 LLM 요청 상한. 0 = 무제한(기본; 라운드 50·실행 시간·loop.max_llm_calls 만 남는다 — "전부 LLM 에게
    # 맡겼을 때" 의 토큰을 재는 용도, 2026-10-29), 양수 = 그 횟수 뒤 repeated_failure 로 차단(script 모드와 같게 하려면 3).
    # 무제한이면 loop.blocked_policy 의 양수 횟수도 적용하지 않는다(0 인 사유는 그대로 바로 Handover).
    # edits mode: LLM asks per case; 0 = no cap (only the rounds, the time budget and loop.max_llm_calls stop it;
    # the positive loop.blocked_policy counts are lifted too, the 0 ones still block at once), > 0 = BLOCKED after that many.
    edits_max_asks_per_case: int = 0


class KBConfig(BaseModel):
    """파일 KB 설정: 위치(kb.dir, 기본 kb-v2/), 비밀 마스킹 규칙, 프로세스 간 잠금 대기. 테스트·데모는 out/
    아래 KB 만 쓴다(불변조건 6)."""
    dir: str = "kb-v2"                       # relative to repo root unless absolute: errors.jsonl, cases.md, cases/<id>/
    # secret masking rules (built-ins + this yml: company additions, invariant 2), relative to the repo root
    redact_file: str = "knowledge/kb/redact.yml"
    lock_timeout_seconds: int = 300          # cross-process kb/.lock wait before a write gives up (logged, never fatal)


class PlanConfig(BaseModel):
    """계획 설정: 규칙 기반의 결정적 계획 + 선택적 LLM 검토(기본 꺼짐).
    catalog_file 이 레시피 id 의 유일한 출처다(불변조건 5)."""
    advisor: Literal["none", "llm"] = "none"   # optional LLM review of the rule-based plan
    catalog_file: str = "knowledge/recipes/catalog.yml"   # single source of truth for known recipes (advisor
                                             # whitelist, coverage, gates, descriptions); relative to repo root


class FleetConfig(BaseModel):
    """`sbmig fleet` 설정: 에이전트 하나가 프로젝트 N 개를 엄격히 순서대로, 사이사이 KB 를 채우며 돈다(약
    500개 운영 모델). 인프라 장애 대기, 디스크 여유, 끝난 프로젝트 정리 방식도 여기서 정한다.

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
    """보고서 설정: 형식(md / json), result.zip, diff, report.md 언어(ko 가능 — json·콘솔·로그는 영어
    유지), 최종 빌드 뒤 javax API jar 조사."""
    formats: list[str] = Field(default_factory=lambda: ["md", "json"])
    zip_result: bool = True
    include_diff: bool = True
    language: Literal["en", "ko"] = "en"   # report.md language (report.json, console and logs stay English)
    # after the final build: `mvn dependency:tree` per module → javax EE API jars still on the classpath and the
    # direct dependency that drags each in (the library still compiled against javax); also fills BlockedItem.via
    javax_api_inventory: bool = True


class AdvisoryConfig(BaseModel):
    """라이브러리 권고 설정(EOL·관리 안 되는 라이브러리). 실행을
    막지 않는다 — 수정 프롬프트 힌트와 인수인계 섹션뿐이다.

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
    """접수 설정(v2.1j, 2026-10-29 현업: "Ant 프로젝트도 Maven 으로 바꾼 뒤 이관"). ``build.xml`` 만 있고 pom.xml 이 없는
    프로젝트는 접수에서 pom.xml 을 합성해 받는다(``sbmig.analyze.ant.synthesize_pom``): build.xml 의 javac / war 설정과
    폴더 구조로 소스·웹 루트·리소스·Java 수준·인코딩·packaging 을 정하고, lib 의 jar 는 ① jar 안의 pom.properties
    ② 이름 표(jar_names.yml + <kb>/jar_names.yml) ③ sha1 저장소 조회 순으로 Maven 좌표를 찾는다. 못 찾은 jar 는
    ``unknown_jar`` 정책대로(system = systemPath 의존성으로 선언해 빌드는 통과, 인수인계에 install-file 명령 안내).
    pom.xml 도 build.xml 도 없는 트리는 여전히 거부한다(2026-10-23 결정).

    Intake (v2.1j): an Ant project (build.xml, no pom.xml) gets a synthesized pom.xml at intake — layout from build.xml
    and the directories, jar coordinates from the jar's pom.properties, the name table, then a sha1 repository lookup;
    unknown jars follow `unknown_jar`. A tree with neither pom.xml nor build.xml is still rejected."""
    ant_to_maven: bool = True                               # false = reject Ant projects as before (exit 3)
    synth_group_id: str = "com.company.migrated"           # placeholder groupId of a synthesized pom (company config overrides)
    synth_version: str = "1.0.0-SNAPSHOT"                   # version of a synthesized pom when build.xml names none
    unknown_jar: Literal["system", "skip"] = "system"       # system = <systemPath> dependency (build passes) | skip = not declared
    jar_lookup: bool = True                                 # sha1 lookup (build.repository_url / settings mirror / Central) for jars still unknown
    jar_names_file: str = "knowledge/libraries/jar_names.yml"   # shipped stem → coordinates table
    company_jar_names_file: str = "jar_names.yml"           # company rows under kb.dir (same format), merged over the shipped table
    min_java_source: str = "1.8"                            # build.xml source / target below this is raised (JDK 21 compiles 8+ only)
    max_jar_bytes_scan: int = 64 * 1024 * 1024              # a jar bigger than this is not opened for pom.properties / packages


class ExternalizedConfig(BaseModel):
    """외부 설정 키 처리: 코드는 읽지만 패키지된 리소스에는 없는 설정(예전에는 컨테이너가 줌: JNDI,
    context-param, -D). 목록을 만들고 __SET_ME__ placeholder
    를 써 넣어, 인수인계가 어떤 값을 어떻게 넣을지 알려 준다.

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


class Config(BaseModel):
    """전체 설정(load_config 가 default.yml → --config yml → overrides → SBMIG_* 환경 변수 순으로 합친
    결과). 회사 고유 값(사내 좌표 등)은 코드가 아니라 yml 에 있다(불변조건 2)."""
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
    repo_root: str = str(PACKAGE_ROOT)

    # ---- helpers -----------------------------------------------------
    def resolve_path(self, p: str | Path) -> Path:
        """상대 경로를 repo_root 기준 절대 경로로 바꾼다(절대 경로는 그대로)."""
        p = Path(p)
        return p if p.is_absolute() else Path(self.repo_root) / p

    @property
    def kb_dir(self) -> Path:
        """KB 디렉토리(kb.dir 을 repo_root 기준으로)."""
        return self.resolve_path(self.kb.dir)

    @property
    def recipe_catalog_file(self) -> Path:
        """레시피 카탈로그 파일(plan.catalog_file)."""
        return self.resolve_path(self.plan.catalog_file)

    @property
    def advisories_file(self) -> Path:
        """기본 권고 파일(advisories.file)."""
        return self.resolve_path(self.advisories.file)

    @property
    def advisory_files(self) -> list[Path]:
        """기본 권고 파일 + 회사 추가 파일(뒤의 것이 이김, id 기준 병합).

        Base advisories file followed by the company extra files (later wins, merged by id)."""
        return [self.advisories_file, *(self.resolve_path(p) for p in self.advisories.extra_files)]

    def optional_keys(self) -> dict[str, ArtifactSpec]:
        """선택 사내 아티팩트를 g:a 와 별칭 키로 찾는 표."""
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
    """`${VAR}` / `${VAR:-기본값}` 을 환경 변수로 펼친다(변수가 없고 기본값도 없으면 그대로 둔다)."""
    if isinstance(value, str):
        def repl(m: re.Match[str]) -> str:
            return os.environ.get(m.group(1), m.group(2) if m.group(2) is not None else m.group(0))
        return _ENV_RE.sub(repl, value)
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


def deep_merge(base: dict, override: dict) -> dict:
    """dict 를 깊게 합친다: 뒤의 것이 이기고, 양쪽이 dict 면 재귀, 나머지는 통째로 바꾼다."""
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _apply_env_overrides(data: dict) -> dict:
    """`SBMIG_A__B=값` → data["a"]["b"](밑줄 두 개 = 중첩). 값은 YAML 로 해석한다(true, 숫자, 목록).

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
    """편집기가 어떻게 저장했든 `.env` 를 읽는다: UTF-8(BOM 포함), UTF-16(PowerShell 5.1 의 `>`), 그 밖은
    cp949 — 회사 Windows PC 에서 생긴 파일들 때문.

    `.env` text whatever the editor saved: UTF-8 (BOM or not), UTF-16 (PowerShell 5.1 `>`), else cp949."""
    data = f.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp949", errors="replace")


def dotenv_chain(start: Path) -> list[Path]:
    """``start`` 부터 위로 올라가며 ``.env`` 후보를 모은다: ``start/.env``, ``start.parent/.env`` … ``.git`` 이 있는
    폴더(git 루트)까지 포함하고 거기서 멈춘다(없으면 파일시스템 루트까지). 가까운 것이 앞에 온다 — 패키지가
    ``<repo>/src/migration-agent/`` 처럼 하위에 있어도 레포 루트의 ``.env`` 를 찾는다(2026-10-28).

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
    """`$SBMIG_ENV_FILE`, 패키지 폴더에서 git 루트까지의 `.env`, 현재 디렉토리에서 git 루트까지의 `.env` 의
    KEY=VALUE 를 os.environ 으로 읽는다(:func:`dotenv_chain`). 이 로더가 넣은 값은 뒤에 읽는 파일이 덮어쓰므로
    먼 파일(git 루트)부터 읽어 **가까운 파일이 이기고**, 예전처럼 현재 디렉토리의 `.env` 가 패키지 쪽을 이긴다.
    셸 변수는 언제나 이기고, 파일이 바뀌면 다시 읽는다(웹 서버·플릿). 키는 Config 에 들어가지 않고 환경 변수로만 쓰인다.

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
            # 셸에서 직접 설정한 변수는 덮어쓰지 않는다. 이 로더가 전에 넣은 값만 파일 수정을 따라간다.
            # shell variables win; a value this loader set earlier follows the edited file
            if k and (k not in os.environ or (k in _DOTENV_SET and os.environ[k] == _DOTENV_SET[k])):
                os.environ[k] = v
                _DOTENV_SET[k] = v
    return found


def load_config(*paths: str | Path, overrides: Optional[dict] = None, repo_root: Optional[Path] = None) -> Config:
    """설정을 읽어 합친다: default.yml → 주어진 yml 들 → overrides → SBMIG_* 환경 변수 → `${VAR}` 펼침. 모든
    명령과 sbmig.api.migrate 가 이것으로 시작한다(.env 는 먼저 환경 변수로 읽힌다).

    Load default.yml, then each given yml (artifacts yml, site yml ...), then dict overrides, then env
    (`.env` files are read into the environment first, see `load_dotenv_files`)."""
    load_dotenv_files()
    data: dict = {}
    files = [DEFAULT_CONFIG_PATH, *[Path(p) for p in paths]]
    for f in files:
        if f and Path(f).exists():
            with open(f, "r", encoding="utf-8") as fh:
                loaded = yaml.safe_load(fh) or {}
            data = deep_merge(data, loaded)
    if overrides:
        data = deep_merge(data, overrides)
    data = _apply_env_overrides(data)
    data = _expand_env(data)
    if repo_root:
        data["repo_root"] = str(repo_root)
    return Config.model_validate(data)
