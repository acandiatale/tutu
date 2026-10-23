# 08. 설정 레퍼런스

`sbmig`의 모든 동작은 YAML 설정(`config/default.yml` + 회사 설정 파일)과 환경 변수(`.env` 포함)로 결정됩니다. 이 문서는 `sbmig.core.config.Config`가 받아들이는 **모든 키**를 섹션별 표로 정리하고(키 목록은 `Config.model_json_schema()`와 `load_config('config/artifacts.example.yml')` 결과에서 생성해 `config/default.yml` 주석과 대조했습니다), 설정이 합쳐지는 우선순위, 예제 설정 파일 네 개의 용도, 회사에서 처음 설정할 때의 체크리스트와 자주 바꾸는 키 10개를 설명합니다. 회사 고유 좌표는 이 저장소에 없으므로 `xxx-parent` / `xxx-boot-jar` / `xxx-comp-jar`(4.0.0) 같은 자리표시자로 씁니다.

## 이 문서를 읽고 나면
- 설정 파일·`.env`·셸 환경 변수·CLI 플래그 중 무엇이 무엇을 이기는지 설명할 수 있습니다.
- 회사용 **config/artifacts.yml**(직접 만드는 파일)을 처음부터 채울 수 있습니다.
- 각 섹션(`artifacts`, `layout`, `openrewrite`, `build`, `loop`, `llm`, `fix`, `kb`, `plan`, `fleet`, `report`, `advisories`, `externalized`)의 키가 무슨 뜻이고 언제 바꾸는지 찾아볼 수 있습니다.
- `config/artifacts.local-e2e.yml`을 회사에서 쓰면 안 되는 이유를 압니다.

## 관련 코드
- `sbmig/core/config.py` — 스키마(`Config` 및 하위 모델), `load_config`, `load_dotenv_files`, `_apply_env_overrides`, `_expand_env`
- `config/default.yml` — 기본값(항상 가장 먼저 읽힘)
- `config/artifacts.example.yml` — 회사 설정 템플릿
- `config/artifacts.local-e2e.yml` — 로컬 e2e 전용(공개 대체 좌표 + 9c 이전 레이아웃)
- `config/llm-anthropic.yml`, `config/llm-plugin.example.yml`, `config/settings.nexus.example.xml`, `.env.example`
- `sbmig/cli.py` — CLI 플래그 → overrides 변환
- `sbmig/build/mvn.py` — `build.*` 키가 Maven 명령으로 바뀌는 곳

## 1. 설정이 합쳐지는 순서(우선순위)

### 1.1 로딩 순서
`load_config(*paths, overrides=…, repo_root=…)`(`sbmig/core/config.py`)는 다음 순서로 동작합니다. **뒤에 오는 것이 이깁니다.**

| 순서 | 단계 | 내용 |
|---|---|---|
| 0 | `.env` 읽기 | `load_dotenv_files()`가 `$SBMIG_ENV_FILE` → `<sbmig 저장소 루트>/.env` → `<현재 디렉터리>/.env` 순으로 `KEY=VALUE` 줄을 `os.environ`에 넣습니다. **이미 설정된 환경 변수는 덮어쓰지 않습니다**(셸 변수가 이김, 여러 `.env` 사이에서는 먼저 읽은 파일이 이김). `export KEY=…`, 따옴표, ` #` 주석을 허용합니다. |
| 1 | `config/default.yml` | 항상 먼저 읽힙니다(`DEFAULT_CONFIG_PATH`). |
| 2 | `--config` 파일들 | 지정한 순서대로 깊은 병합(`deep_merge`)합니다. 나중 파일이 이깁니다. |
| 3 | overrides | CLI 플래그(`--java-home`, `--settings`, `--llm-provider`, `--llm-plugin`, `--fix-plugin`, `--no-kb`), 웹의 `llm_provider` 파라미터, `sbmig.api.migrate(overrides=…)`. |
| 4 | `SBMIG_*` 환경 변수 | `SBMIG_LLM__MODEL=foo` → `llm.model = foo`(밑줄 두 개 = 한 단계 중첩, 값은 YAML로 해석). **CLI 플래그보다도 나중에 적용되므로 CLI보다 우선합니다.** |
| 5 | `${VAR}` 치환 | 병합이 끝난 모든 문자열 값에서 `${VAR}` / `${VAR:-기본값}`을 환경 변수로 치환합니다. 변수가 없고 기본값도 없으면 `${VAR}` 글자가 그대로 남습니다. |
| 6 | `--repo-root` | 지정하면 `repo_root`를 덮어씁니다. |
| 7 | 검증 | `Config.model_validate`. 정의되지 않은 키는 조용히 무시됩니다(pydantic 기본 동작). |

### 1.2 병합 규칙에서 주의할 점
- **딕셔너리는 키 단위로 합쳐지고, 리스트·스칼라는 통째로 교체됩니다.** 예를 들어 회사 파일에 `openrewrite.exclusions`를 한 항목만 쓰면 기본 4개 패턴이 모두 사라지고 그 한 항목만 남습니다. `artifacts.optional`도 리스트이므로 파일에 쓴 8개가 전부입니다.
- `artifacts` 섹션(최소한 `artifacts.parent`)에는 기본값이 없습니다. `--config` 없이 `sbmig run` / `sbmig analyze` / `sbmig plan` / `sbmig build` / `sbmig rewrite`를 실행하면 `config error … artifacts: Field required`와 힌트를 출력하고 종료 코드 2로 끝납니다. 예외: `sbmig llm-probe`는 artifacts를 임시값으로 채워 `.env`만으로 동작하고, `sbmig kb` / `sbmig recipes` / `sbmig advisories`도 artifacts가 없으면 자리표시자(`unset`)로 채워 동작합니다.
- 상대 경로 해석: `kb.dir`, `openrewrite.base_recipe_dir`, `plan.catalog_file`, `advisories.file`, `advisories.extra_files`, `externalized.rules_file`, `llm.record_dir`은 `repo_root`(기본: sbmig 저장소 루트) 기준으로 해석됩니다(`Config.resolve_path`). 반면 `build.settings_file`, `build.java_home`, `build.local_repo`, `build.mvn`은 `~`만 펼친 뒤 그대로 Maven에 넘어가며 Maven은 마이그레이션 대상 프로젝트 디렉터리에서 실행됩니다(코드 기준: `sbmig/build/mvn.py`). **이 네 개는 절대 경로로 쓰는 것을 권장합니다.**
- `SBMIG_`로 시작하는 다른 변수(`SBMIG_ENV_FILE`, `SBMIG_WEB_OUT`, `SBMIG_WEB_REPO_ROOT`, `SBMIG_MAX_UPLOAD_MB`)도 4단계에서 설정 키로 해석되지만(`env_file`, `web.out` 등) `Config`에 없는 키라 무시됩니다. 영향은 없습니다.

### 1.3 환경 변수로만 들어오는 값
| 값 | 들어오는 길 | 비고 |
|---|---|---|
| LLM API 키 | `llm.api_key_env`가 가리키는 변수(기본 `LLM_API_KEY`) | 키 자체는 어떤 YAML에도 쓰지 않습니다. 플러그인에는 메모리에서 `options["api_key"]`로만 전달됩니다. |
| 게이트웨이 URL | `LLM_BASE_URL` → `llm.base_url: "${LLM_BASE_URL}"` | 전체 엔드포인트 URL을 그대로 사용합니다(경로를 덧붙이지 않음). |
| 모델 | `LLM_MODEL`(기본 `gpt-6-luna`), `LLM_RECIPE_MODEL`, `LLM_ESCALATION_MODEL` | 비우면 `LLM_MODEL`과 같은 모델을 씁니다. |
| 응답 경로 | `LLM_RESPONSE_PATH` | 자동 감지가 실패할 때만. |
| LLM 기록 디렉터리 | `LLM_RECORD_DIR` | 비우면 기록하지 않습니다. |
| JDK(로컬 e2e) | `JAVA21_HOME` → `config/artifacts.local-e2e.yml`의 `build.java_home` | 회사 설정 템플릿은 `JAVA21_HOME`을 읽지 않습니다. 회사에서는 `build.java_home`에 직접 경로를 쓰거나 `--java-home`을 줍니다. |

환경 변수 전체 목록과 의미는 [07. API 레퍼런스](07-api-reference.md)의 환경 변수 절을 보십시오.

### 1.4 한눈에 보는 예
```text
config/default.yml            llm.model: "${LLM_MODEL:-gpt-6-luna}"
config/artifacts.yml          (llm 섹션 없음)
.env                          LLM_MODEL=gpt-6-luna
셸                             $env:LLM_MODEL = "other-model"     ← 셸이 .env를 이김
결과                           llm.model = other-model
CLI                            sbmig llm-probe --model probe-model  ← overrides(3단계)
SBMIG_LLM__MODEL=x             ← 4단계라 CLI보다도 우선
```

## 2. 섹션별 키 레퍼런스

표의 "기본값"은 `config/default.yml`을 읽은 뒤의 실제 값입니다. 파이썬 모델 기본값이 다르면 괄호로 적었습니다(파일 없이 `Config`를 직접 만드는 테스트 등에서만 의미가 있습니다).

### 2.1 `target` — 목표 버전
| 키 | 타입 | 기본값 | 의미 | 언제 바꾸나 | 관련 문서 |
|---|---|---|---|---|---|
| `target.java_version` | str | `"21"` | 목표 Java 버전. 부모가 Java 버전을 관리하지 않을 때(`artifacts.parent_manages_java_version: false`) `maven.compiler.release`로 씁니다. | 바꾸지 않습니다(이 도구의 범위가 JDK 21). | [01](01-pipeline.md) |
| `target.spring_boot_version` | str | `"4.1.0"` | 목표 Spring Boot. 부모가 Boot를 관리하지 않을 때 BOM/플러그인 버전으로 씁니다. | 회사 부모가 다른 4.1.x를 쓰는 경우에만. | [06](06-restructure-and-layouts.md) |
| `target.spring_framework_version` | str | `"7.0.x"` | 레시피 합성 시 참고하는 Framework 라인(코드 기준: `sbmig/rewrite/compose.py`). | 바꾸지 않습니다. | — |

### 2.2 `artifacts` — 사내 아티팩트 정책(회사 설정의 핵심)
`artifacts.parent`, `artifacts.boot_parent`, `artifacts.comp_parent`, `artifacts.mandatory[]`, `artifacts.optional[]`의 각 항목은 같은 `ArtifactSpec` 구조입니다.

**`ArtifactSpec` 필드**

| 필드 | 타입 | 기본값 | 의미 |
|---|---|---|---|
| `group_id` | str | (필수) | groupId |
| `artifact_id` | str | (필수) | artifactId |
| `version` | str 또는 null | null | null = 부모가 관리하는 버전을 따름(버전 태그를 쓰지 않음) |
| `scope` | str 또는 null | null | 의존성 scope. `mandatory`에서 `import`(+ `type: pom`)이면 BOM 방식으로 `<dependencyManagement>`에 선언합니다(`<dependencies>`에는 import scope를 쓸 수 없음). |
| `type` | str 또는 null | null | 의존성 type. jar 없이 pom으로만 배포되는 사내 아티팩트는 `pom`으로 지정 → `<type>pom</type>`로 선언되고, as-is가 type 없이 선언해 둔 경우에도 재구성이 `<type>`을 맞춥니다(없으면 존재하지 않는 jar를 찾다 실패). |
| `aliases` | list[str] | `[]` | 옛 `g:a` 키 — as-is 프로젝트가 이 이름으로 쓰고 있어도 같은 아티팩트로 취급(optional 판정, 부모 판정 모두) |
| `property_name` | str 또는 null | null | 버전을 관례적으로 `<properties>`의 이 이름에 두는 경우(예: `lib-db.version`) 그 속성을 갱신 |

**섹션 키**

| 키 | 타입 | 기본값 | 의미 | 언제 바꾸나 | 관련 문서 |
|---|---|---|---|---|---|
| `artifacts.parent` | ArtifactSpec | (기본값 없음, 필수) | 마이그레이션된 **루트 pom**의 부모. 회사: `base-parent`(자리표시자 `xxx-parent` 4.0.0). 이 부모(와 role parent)를 해석할 수 없으면 실행을 일찍 중단합니다(종료 코드 3, 플래그 `aborted:parent-not-found`). | 회사 좌표를 처음 넣을 때, 사내 버전 라인이 올라갈 때. | [06](06-restructure-and-layouts.md) |
| `artifacts.boot_parent` | ArtifactSpec 또는 null | null(예제: `boot-jar` 4.0.0) | `{name}-boot` 모듈 pom의 부모(role parent). `comp_parent`와 둘 다 설정하면 role-parent 모드: 루트는 aggregator, 모듈이 각자 role parent를 직접 상속(명시 groupId/version + `<relativePath/>`). 예전 마이그레이션의 같은 `g:a` 부모(예: 1.0.0)는 설정 버전으로 옮깁니다. | 회사 좌표(`xxx-boot-jar`). 비우면 모듈이 루트 pom을 상속합니다(9b 이전 동작). | [06](06-restructure-and-layouts.md) |
| `artifacts.comp_parent` | ArtifactSpec 또는 null | null(예제: `comp-jar` 4.0.0) | `{name}-back` / `{name}-front` 모듈 pom의 부모. | 회사 좌표(`xxx-comp-jar`). | [06](06-restructure-and-layouts.md) |
| `artifacts.parent_manages_spring_boot` | bool | `true` | true = 사내 부모가 Spring Boot 4.1을 관리 → sbmig는 Boot 버전을 고정하지 않고, as-is `spring-boot-dependencies` import와 boot 플러그인 버전을 제거합니다. false = sbmig가 `target.spring_boot_version`으로 BOM import / 플러그인 버전을 설정. | 회사 boot-jar 부모가 `spring-boot-maven-plugin` 버전까지 관리하지 않는 것이 확인되면 false. | [06](06-restructure-and-layouts.md) |
| `artifacts.parent_manages_java_version` | bool | `true` | false면 sbmig가 `maven.compiler.release`를 씁니다. | 사내 부모가 Java 21을 설정하지 않을 때. | — |
| `artifacts.mandatory` | list[ArtifactSpec] | `[]`(예제 2개) | 모든 마이그레이션 결과에 반드시 선언되는 의존성. 운영 설정은 정확히 2개입니다. | 회사 필수 의존성 좌표 입력 시. | [06](06-restructure-and-layouts.md) |
| `artifacts.optional` | list[ArtifactSpec] | `[]`(예제 8개) | as-is가 **이미 선언해 쓰는 경우에만** 버전을 올리는 사내 아티팩트(전이 의존성만 있으면 올리지 않음, ERROR-HANDLING S6). 의존성 API가 javax를 노출할 때(X1) 이 목록에 있으면 버전 bump가 해결책이 됩니다. | 사내 라이브러리 8종 좌표 입력 시, 새 Jakarta 빌드가 나온 라이브러리를 추가할 때. | [04](04-triage-and-loop.md) |
| `artifacts.remove_when_present` | list[str] | `[]` | 발견되면 pom에서 지울 `g:a` 키(예: 예전 사내 BOM `com.company.legacy:legacy-bom`). | 옛 사내 BOM/부모 잔재가 새 부모와 충돌할 때. | — |
| `artifacts.mandatory_target` | `root` / `modules` | `root` | 필수 의존성을 선언할 위치. `root` = 모든 루트 pom(프로젝트 루트, MSA는 각 서비스 루트 + 최상위 aggregator) — 회사 규칙. `modules` = 각 role 모듈 pom(9c 이전 방식). | 바꾸지 않습니다(회사 규칙). | [06](06-restructure-and-layouts.md) |
| `artifacts.optional_version_policy` | `floor` / `pin` | `floor` | `floor` = 설정 버전보다 낮을 때만 올림(더 새 버전은 유지), `pin` = 항상 정확히 설정 버전. | 사내 정책이 "정확히 이 버전"일 때 `pin`. | — |

### 2.3 `layout` — 결과 디렉터리/모듈 구조
| 키 | 타입 | 기본값 | 의미 | 언제 바꾸나 | 관련 문서 |
|---|---|---|---|---|---|
| `layout.module_dir` | str | `""` | 모듈을 둘 하위 디렉터리. `""` = 루트 바로 아래(회사 규칙). 9c 이전 레이아웃은 `module`. | 바꾸지 않습니다. | [06](06-restructure-and-layouts.md) |
| `layout.boot_module` | str | `"{name}-boot"` | Boot 설정 전용 모듈 이름 템플릿(`{name}` = 루트 artifactId). | 바꾸지 않습니다. | [06](06-restructure-and-layouts.md) |
| `layout.comp_module` | str | `"{name}-back"` | 모든 소스(컨트롤러 포함)를 담는 모듈 이름. | 바꾸지 않습니다. | [06](06-restructure-and-layouts.md) |
| `layout.front_module` | str 또는 null | `"{name}-front"` | 웹 리소스(정적 파일/JSP)가 있을 때만 만드는 모듈. null = 만들지 않고 웹 리소스를 boot 모듈에 둠. | 바꾸지 않습니다. | [06](06-restructure-and-layouts.md) |
| `layout.project_name` | str 또는 null | null | `{name}` 대체값. null = 루트 pom artifactId. | 루트 artifactId가 모듈 이름으로 부적절한 프로젝트에 한해 프로젝트별로. | — |
| `layout.front_static_dir` | str | `src/main/resources/static` | front 모듈에서 HTML/css/js/이미지를 둘 위치(jar 리소스). | 바꾸지 않습니다. | [06](06-restructure-and-layouts.md) |
| `layout.front_jsp_dir` | str | `src/main/resources/META-INF/resources/WEB-INF/jsp` | front 모듈에서 JSP를 둘 위치(Servlet 3 jar 리소스). | 바꾸지 않습니다. | [06](06-restructure-and-layouts.md) |
| `layout.boot_as_is` | `in-place` / `layout` | `in-place` | Spring Boot as-is(MSA 포함): `in-place` = 디렉터리 유지, pom만 변경(루트 → `artifacts.parent`, `*-boot` 이고 pom이 Boot 앱인 모듈 → `boot_parent`, 나머지 → `comp_parent`, 단일 모듈 Boot 앱은 `{name}-boot`로 감쌈). `layout` = 다른 케이스처럼 분할. | 바꾸지 않습니다(회사 규칙). | [06](06-restructure-and-layouts.md) |
| `layout.boot_module_pattern` | str | `"*-boot"` | Boot as-is / MSA에서 boot 앱 모듈을 찾는 디렉터리 패턴(pom 검사로 확인). | 부서가 다른 접미사 관례를 쓸 때. | [06](06-restructure-and-layouts.md) |
| `layout.jsp_boot_packaging` | `war` / `jar` | `war` | JSP가 있으면 boot 모듈을 외부 Tomcat용 WAR로 만듦(front jar가 JSP를 담음). | JSP 프로젝트를 실행형 jar로 배포하기로 정해진 경우 `jar`. | [06](06-restructure-and-layouts.md) |
| `layout.boot_artifact_suffix` | str | `"-boot"` | boot 모듈 artifactId 접미사. | 바꾸지 않습니다. | — |
| `layout.comp_artifact_suffix` | str | `"-back"` | back 모듈 artifactId 접미사(front는 `-front`). | 바꾸지 않습니다. | — |
| `layout.split_policy` | `minimal` / `web-layer` | `minimal` | `minimal` = 모든 as-is 소스는 back으로, boot에는 Application 클래스(+ boot 리소스, `@SpringBootTest` 테스트)만. `web-layer` = `@SpringBootApplication`/`@Controller`/`@RestController`/`@Configuration`/`@ControllerAdvice` 클래스를 boot로 이동. | 회사 규칙은 컨트롤러도 back이므로 `minimal` 유지. | [06](06-restructure-and-layouts.md) |
| `layout.multi_module_policy` | `wrap` / `merge` | `merge` | as-is 멀티 모듈(비 MSA): `merge` = 모든 모듈 소스를 back으로 접음(boot 이름 모듈은 유지), `wrap` = 모듈을 그대로 두고 boot 추가. | 바꾸지 않습니다. | [06](06-restructure-and-layouts.md) |
| `layout.merge_collision` | `first-wins` / `fail` | `first-wins` | merge 중 같은 경로가 여러 모듈에 있을 때: `first-wins` = `<modules>` 순서 첫 모듈 사본 유지, 나머지는 `<run dir>/merge-conflicts/`에 저장 + Handover "Merge conflicts". `fail` = restructure 오류, 아무것도 옮기지 않음. | 충돌을 자동 해결하면 안 되는 프로젝트에서 `fail`. | [06](06-restructure-and-layouts.md) |
| `layout.drop_managed_versions` | `gte` / `any` / `off` | `gte` | 사내 부모 설정 후, 새 부모 체인이 관리하는 명시 `<version>`을 삭제: `gte` = 관리 버전이 같거나 더 새 것일 때만, `any` = 항상, `off` = 유지. artifacts 항목과 system scope는 건드리지 않음. | 명시 버전을 그대로 두라는 요청이 있을 때 `off`. | [06](06-restructure-and-layouts.md) |
| `layout.application_class_name` | str | `"Application"` | 생성하는 Boot 메인 클래스 이름. | 사내 명명 규칙이 있을 때. | — |
| `layout.boot_resources_glob` | list[str] | `application*.yml`, `application*.yaml`, `application*.properties`, `bootstrap*.yml`, `bootstrap*.properties`, `logback*.xml`, `log4j2*.xml`, `banner.txt` | boot 모듈로 옮기는 리소스 패턴(리스트이므로 지정 시 통째로 교체). | 다른 Boot 설정 파일 관례가 있을 때(전체 목록을 다시 적어야 함). | — |
| `layout.starter_by_case` | dict[str,str] | `pure_java: spring-boot-starter`, `servlet_tomcat: spring-boot-starter-webmvc`, `spring_legacy: spring-boot-starter-webmvc`, `spring_boot: auto` | 케이스별로 boot 모듈에 넣는 스타터(`auto` = as-is 스타터 유지). | 특정 케이스에 다른 스타터가 표준일 때. | [01](01-pipeline.md) |
| `layout.add_servlet_component_scan` | bool | `true` | servlet 프로젝트에 `@ServletComponentScan` 추가. | 서블릿 등록을 다른 방식으로 할 때. | [06](06-restructure-and-layouts.md) |
| `layout.web_xml_registrations` | `generate` / `followup` | `generate` | (2026-10-23) web.xml의 서블릿 / 필터 / 리스너 / 오류 페이지 → boot 모듈에 생성하는 `WebXmlRegistrations` @Configuration, `<session-timeout>` → `server.servlet.session.timeout`. skip 표 `knowledge/config/web_xml_rules.yml`. `followup` = 생성하지 않고 후속조치만. | web.xml 배선을 사람이 직접 옮기기로 한 경우. | [06](06-restructure-and-layouts.md) |
| `layout.import_xml_bean_configs` | bool | `true` | 감지된 `<beans>` XML 파일을 `@ImportResource`로 가져옴(boot 모듈 `legacy-config/`). | XML 설정을 수동 이관하기로 한 경우. | [06](06-restructure-and-layouts.md) |
| `layout.msa_parent_policy` | `per-service-root` / `top-aggregator` | `per-service-root` | MSA: 서비스 루트마다 rewrite/loop 체인 실행. `top-aggregator` = 루트 aggregator의 reactor가 모든 대상을 포함하면 루트에서 한 번에 실행. | 큰 MSA에서 실행 시간을 줄이고 싶을 때(검증 후). | [06](06-restructure-and-layouts.md) |

### 2.4 `openrewrite` — 레시피 실행
| 키 | 타입 | 기본값 | 의미 | 언제 바꾸나 | 관련 문서 |
|---|---|---|---|---|---|
| `openrewrite.plugin_version` | str | `"6.46.1"` | `rewrite-maven-plugin` 버전. | Nexus에 다른 버전만 있을 때(검증 필요). | [02](02-workflows.md) |
| `openrewrite.recipe_bom_version` | str | `"3.37.0"` | 레시피 BOM. **3.38.0은 Central에서 불일치(404)라 고정**(불변식 5). | 바꾸지 않습니다. | `docs/RECIPES.md` |
| `openrewrite.recipe_artifacts` | list[str] | `rewrite-migrate-java`, `rewrite-spring`, `rewrite-testing-frameworks`, `rewrite-static-analysis`(모두 `org.openrewrite.recipe`) | 플러그인 classpath에 넣는 레시피 아티팩트. | 회사 레시피 아티팩트를 추가할 때(전체 목록을 다시 적음). | `docs/RECIPES.md` |
| `openrewrite.recipe_artifact_versions` | dict[str,str] | `{}` | 아티팩트별 버전 강제(없으면 BOM 관리 버전). | BOM 버전이 Nexus에 없을 때 개별 지정. | `docs/RECIPES.md` |
| `openrewrite.recipes.jdk21` | list[str] | `UpgradeToJava21` | jdk21 단계 레시피. | 바꾸지 않습니다. | `docs/RECIPES.md` |
| `openrewrite.recipes.jakarta` | list[str] | `JavaxMigrationToJakarta` | javax → jakarta. | 바꾸지 않습니다. | `docs/RECIPES.md` |
| `openrewrite.recipes.spring_boot_4` | list[str] | `UpgradeSpringBoot_4_0`, `SpringBootProperties_4_1`, `com.company.migrate.SpringBoot41Version`, `com.company.migrate.Boot4JacksonTwins` | Boot 4.1 체인(4.1 umbrella가 아직 없어 4.0 + 4.1 속성 + 자체 버전 레시피). | 바꾸지 않습니다. | `docs/RECIPES.md` |
| `openrewrite.recipes.spring_framework_7` | list[str] | `UpgradeSpringFramework_7_0` | Spring Framework 7(레거시 Spring). | 바꾸지 않습니다. | `docs/RECIPES.md` |
| `openrewrite.recipes.spring_security_7` | list[str] | `UpgradeSpringSecurity_7_0` | Security 7. | 바꾸지 않습니다. | `docs/RECIPES.md` |
| `openrewrite.recipes.junit5` | list[str] | `JUnit4to5Migration` | JUnit 4 → 5. | 바꾸지 않습니다. | `docs/RECIPES.md` |
| `openrewrite.recipes.extra` | dict[str, list[str]] | `{}` | 케이스 이름(`pure_java` 등) 또는 `all` → 추가 레시피. 모든 id는 `knowledge/recipes/catalog.yml`에 있어야 합니다(`sbmig recipes check`). | 회사 레시피를 특정 케이스에 추가할 때. | `docs/RECIPES.md` |
| `openrewrite.base_recipe_dir` | str | `knowledge/recipes/base` | 프로젝트 소유 선언형 레시피 디렉터리. | 바꾸지 않습니다. | — |
| `openrewrite.timeout_minutes` | int | `40` | 레시피 실행 1회 제한 시간. | 소스가 매우 많은 프로젝트(회사 Boot 프로젝트에서 rewrite 258초 관찰, 대략 소스량에 비례). | [09](09-operations.md) |
| `openrewrite.jvm_args` | list[str] | `["-Xmx4g"]` | 플러그인 JVM 옵션. | 대형 프로젝트에서 느리거나 OOM이 날 때. | [09](09-operations.md) |
| `openrewrite.oom_retry_jvm_args` | list[str] | `["-Xmx8g"]` | `OutOfMemoryError` 후 1회 재시도에 쓰는 JVM 옵션(E8). | 호스트 메모리에 맞춰. | [09](09-operations.md) |
| `openrewrite.download_retry` | int | `1` | 다운로드 실패(429/5xx/reset/timeout)로 실패하거나 `4.0.x` 같은 문자 그대로 버전을 남겼을 때 재시도 횟수(X8). 0 = 재시도 없음. | 프록시가 자주 429를 줄 때 늘림(오케스트레이터는 단계 재실행을 최대 1회만 함). | [09](09-operations.md) |
| `openrewrite.download_retry_wait_s` | int | `30` | 재시도 전 대기 초. | rate limit이 길 때. | [09](09-operations.md) |
| `openrewrite.exclusions` | list[str] | `**/*.jsp`, `**/*.jspx`, `**/*.html`, `**/*.htm` | 플러그인이 파싱하지 않을 글롭(`-Drewrite.exclusions`). JSP/HTML의 `token recognition error at: '<'` 잡음 제거. | 회사 레시피가 JSP를 고쳐야 할 때만 비움. 대형 생성 소스 디렉터리를 빼서 속도를 높일 때 추가(리스트 전체를 다시 적음). | [09](09-operations.md) |
| `openrewrite.fail_on_dry_run_results` | bool | `false` | 현재 코드에서 읽지 않는 키입니다(코드 기준: `sbmig/core/config.py`). | 바꿀 필요 없습니다. | — |
| `openrewrite.plugin_goal` | str | `"run"` | `run` 또는 `dryRun`. 컴파일되지 않는 프로젝트에서는 자동으로 `runNoFork`로 재시도합니다(E9). | 바꾸지 않습니다. | [09](09-operations.md) |

### 2.5 `build` — Maven 실행
| 키 | 타입 | 기본값 | 의미 | 언제 바꾸나 | 관련 문서 |
|---|---|---|---|---|---|
| `build.mvn` | str | `"mvn"` | Maven 실행 파일. 경로 구분자가 없으면 PATH(+ `MAVEN_HOME`/`M2_HOME`의 `bin`)에서 `shutil.which`로 찾고, Windows에서는 `mvn.cmd`/`mvn.bat`을 먼저 찾습니다. | `where mvn`이 못 찾는 Windows: `C:/apache-maven-3.9.x/bin/mvn.cmd`. | [09](09-operations.md) |
| `build.settings_file` | str 또는 null | null | 모든 Maven 호출(빌드, OpenRewrite, effective-pom, BOM 조회)에 `-s`로 넘기는 settings.xml. `mirrorOf external:*` catch-all 미러가 있으면 pom에 선언된 저장소도 Nexus로 우회됩니다. | 회사: 반드시 설정(절대 경로). `config/settings.nexus.example.xml` 복사본. | [09](09-operations.md) |
| `build.local_repo` | str 또는 null | null | `-Dmaven.repo.local=…`. | 마이그레이션 전용 로컬 저장소를 쓸 때, `_remote.repositories` 문제로 새 저장소가 필요할 때. | [09](09-operations.md) |
| `build.java_home` | str 또는 null | null | JDK 21 경로. 설정하면 Maven 환경의 `JAVA_HOME`과 `PATH` 앞부분을 바꿉니다. | 회사: 반드시 고정 권장. | [09](09-operations.md) |
| `build.offline` | bool | `false` | `-o`(오프라인). | 로컬 저장소에 모든 것이 이미 있을 때만. | — |
| `build.extra_args` | list[str] | `["-B", "-ntp", "-Dstyle.color=never"]` | 모든 Maven 호출에 붙는 인자. | `-llr`가 필요한 프로젝트(원격 저장소 id 캐시 문제) 등. 리스트 전체를 다시 적습니다. | [09](09-operations.md) |
| `build.skip_tests` | bool | `true` | `-DskipTests`(테스트는 컴파일하지만 실행하지 않음). | 테스트까지 돌리려면 false. | [09](09-operations.md) |
| `build.skip_test_compile` | bool | `false` (회사 예제 `true`) | 모든 빌드에 `-Dmaven.test.skip=true`: 테스트 소스를 컴파일도 실행도 하지 않으므로 테스트 소스 오류가 마이그레이션을 막지 못함. 플래그 `tests-not-compiled`, Handover follow-up "소유 팀이 테스트를 컴파일·수정". | 회사 결정(2026-10-22): 테스트는 전부 건너뛴다 → `true`. | [09](09-operations.md) |
| `build.allow_skip_test_compile_fallback` | bool | `true` | 테스트 컴파일 오류만 남으면 `-Dmaven.test.skip=true`로 패키징(플래그 `test-compile-skipped`, L7). | 테스트 컴파일 실패를 실패로 보고 싶을 때 false. | [04](04-triage-and-loop.md) |
| `build.allow_skip_tests_fallback` | bool | `true` | 런타임 테스트 실패만 남으면 `-DskipTests`로 패키징(플래그 `tests-skipped`, L7b). `skip_tests: false`일 때만 의미가 있습니다. | 테스트 실패를 실패로 보고 싶을 때 false. | [04](04-triage-and-loop.md) |
| `build.timeout_minutes` | int | `30` | Maven 1회 제한 시간. 초과 시 프로세스 트리를 종료(Windows는 `taskkill /T /F`). | 대형 빌드. | [09](09-operations.md) |
| `build.env` | dict[str,str] | `{}` | Maven 프로세스에 추가할 환경 변수(예: `MAVEN_OPTS`). | 메모리 옵션, 프록시 변수. | — |
| `build.source_encoding_policy` | `set-property` / `convert-utf8` | `set-property` | MS949/EUC-KR 레거시 소스: `project.build.sourceEncoding` 설정 또는 UTF-8로 변환(E1). 파일은 항상 원래 인코딩으로 읽고 씁니다. | 소스를 UTF-8로 통일하기로 한 경우 `convert-utf8`. | [09](09-operations.md) |
| `build.encoding_candidates` | list[str] | `utf-8`, `cp949`, `euc-kr`, `shift_jis`, `latin-1` | 인코딩 감지 시도 순서. | 다른 레거시 인코딩이 있을 때. | — |
| `build.enforcer_policy` | `relax-rule` / `skip` / `keep` | `relax-rule` | `requireJavaVersion` 등 enforcer 실패 처리(E4). | 규칙을 건드리면 안 될 때 `keep`. | [09](09-operations.md) |
| `build.werror_policy` | `remove` / `keep` | `remove` | `-Werror` 제거(E5, 플래그 `werror-removed`). | 경고 0 정책을 유지할 때 `keep`. | [09](09-operations.md) |
| `build.pom_repositories` | `keep` / `remove` | `keep` | pom 안의 `<repositories>`/`<pluginRepositories>`: `keep` = 그대로(settings 미러로 우회), `remove` = 첫 빌드 전에 핸들러 `strip-pom-repositories`가 삭제(미러 필수, X9). | 결과 pom에서 죽은 URL을 없애고 싶을 때 `remove`. | [09](09-operations.md) |
| `build.external_package_prefixes` | list[str] | `[]` | 오류 시그니처에서 "라이브러리 코드"로 취급할 패키지 접두어(사내 groupId는 자동 추가). | 사내 라이브러리 패키지가 groupId와 다를 때. | [04](04-triage-and-loop.md) |

### 2.6 `loop` — build-fix 루프와 예산
| 키 | 타입 | 기본값 | 의미 | 언제 바꾸나 | 관련 문서 |
|---|---|---|---|---|---|
| `loop.phases.jdk21.goal` / `.max_iterations` / `.enabled` | str / int / bool | `compile` / `3` / `true` | jdk21 단계의 빌드 goal, 수정 라운드 수, 사용 여부. | 거의 바꾸지 않습니다. | [04](04-triage-and-loop.md) |
| `loop.phases.spring.goal` / `.max_iterations` / `.enabled` | str / int / bool | `test-compile` / `6` / `true` | spring 단계. | LLM 예산이 넉넉하고 오류가 많은 프로젝트에서 라운드 증가. | [04](04-triage-and-loop.md) |
| `loop.phases.restructure.goal` / `.max_iterations` / `.enabled` | str / int / bool | `package` / `6` / `true` | restructure 단계. | 위와 같음. | [04](04-triage-and-loop.md) |
| `loop.phases.verify.goal` / `.max_iterations` / `.enabled` | str / int / bool | `package` / `3` / `true` | 최종 `clean package`가 **실패했을 때만** 도는 마지막 수정 루프. 끝나면 `clean package`를 다시 실행. 회사 예제는 4라운드. | `0` / `false`로 끄거나, 최종 빌드에서만 나는 오류가 많으면 라운드 증가. | [04](04-triage-and-loop.md) |
| `loop.max_llm_calls` | int | `60` | 실행당 LLM 호출 상한(하네스트의 레시피 후보 생성 포함). 소진 시 플래그 `llm-budget-exhausted`, 남은 LLM 전용 클러스터는 blocked(L6). | 예산 정책. | [04](04-triage-and-loop.md) |
| `loop.max_total_minutes` | int | `180` | 실행당 시간 상한(플래그 `time-budget-exhausted`, verify와 보고서는 항상 실행). | 대형 프로젝트. | [04](04-triage-and-loop.md) |
| `loop.stall_limit` | int | `2` | 같은 오류 집합이 N번 반복되면 escalation 모델 + 컨텍스트 확대(L1). | 거의 바꾸지 않습니다. | [04](04-triage-and-loop.md) |
| `loop.max_clusters_per_iteration` | int | `8` | 한 라운드에서 다루는 최대 클러스터 수. | 오류가 매우 많을 때 조정. | [04](04-triage-and-loop.md) |
| `loop.revert_on_regression` | bool | `true` | 회귀한 시도를 git으로 되돌림(L3). | 바꾸지 않습니다. | [04](04-triage-and-loop.md) |
| `loop.regression_ratio` | float | `1.5` | 수정 후 오류 수 > 이전 × 비율이면 회귀. | 바꾸지 않습니다. | [04](04-triage-and-loop.md) |
| `loop.always_final_verify` | bool | `true` | 무슨 일이 있어도 마지막에 `mvn clean package`(불변식 1). | **바꾸지 않습니다.** | [01](01-pipeline.md) |
| `loop.engine` | `graph` / `inline` | `graph` | `graph` = LangGraph `StateGraph`로 단계 구동(회사 관례, 웹 UI의 node 이벤트), `inline` = 원래 루프. 보고서는 동일. | LangGraph 문제를 의심할 때 `inline`으로 비교. | [03](03-architecture.md) |
| `loop.oscillation_window` | int | `4` | A↔B 진동 감지에 쓰는 오류 집합 이력 길이(L2). | 바꾸지 않습니다. | [04](04-triage-and-loop.md) |
| `loop.max_llm_attempts_per_cluster` | int | `2` | 클러스터당 LLM 시도 후 blocked(`repeated_failure`). | 예산 정책. | [04](04-triage-and-loop.md) |
| `loop.verify_extra_llm_attempts` | int | `2` | 최종 검증 루프에서 `repeated_failure` / `oscillation`으로 blocked된 클러스터를 풀고 더 주는 LLM 시도 수(escalation 모델). `0`이면 풀지 않음. 정책·예산 blocked는 대상 아님. | 마지막에 더 끈질기게 시도하고 싶을 때 증가. | [04](04-triage-and-loop.md) |
| `loop.blocked_policy` | dict[str,int] | `external_javax_api: 0`, `external_api_incompatible: 1`, `artifact_not_found: 0`, `parent_not_found: 0`, `plugin_incompatible: 1`, `unsupported_source: 0`, `encoding: 0` | 차단 사유별로 blocked 전 허용 LLM 시도 수(0 = LLM에 묻지 않음). | 거의 바꾸지 않습니다(LLM이 버전을 지어내지 않게 하는 안전장치). | [04](04-triage-and-loop.md) |
| `loop.structural_regression_kinds` | list[str] | `pom_invalid`, `parent_resolution`, `plugin_resolution` | pom 수정 후 나타나면 개수와 무관하게 되돌리는 오류 종류. | 바꾸지 않습니다. | [04](04-triage-and-loop.md) |

### 2.7 `llm` — LLM 제공자
| 키 | 타입 | 기본값 | 의미 | 언제 바꾸나 | 관련 문서 |
|---|---|---|---|---|---|
| `llm.provider` | str | `company`(모델 기본 `openai_compatible`) | `company`(사내 게이트웨이, `requests.post`) / `openai_compatible` / `anthropic` / `fake` / `plugin` / 진입점 이름. | 검증 실행(`anthropic`), 오프라인 테스트(`fake`), 사내 어댑터(`plugin`). | [07](07-api-reference.md) |
| `llm.plugin` | str 또는 null | null | `pkg.module:attr` — tier 1 어댑터. | 사내 프레임워크 어댑터를 쓸 때. | [07](07-api-reference.md) |
| `llm.plugin_options` | dict | `{}` | 플러그인 팩토리에 그대로 전달(비밀값 금지). | 어댑터 설정. | [07](07-api-reference.md) |
| `llm.base_url` | str | `"${LLM_BASE_URL}"` | 게이트웨이 전체 URL(그대로 사용). `fake` + `replay:<dir>`은 기록 재생. | `.env`로만 설정. | [09](09-operations.md) |
| `llm.api_key_env` | str | `LLM_API_KEY` | API 키가 들어 있는 **환경 변수 이름**. | 플러그인이 다른 변수를 쓸 때. | [07](07-api-reference.md) |
| `llm.model` | str | `"${LLM_MODEL:-gpt-6-luna}"` | 수정(fix) 모델. | `.env`의 `LLM_MODEL`로. | [09](09-operations.md) |
| `llm.escalation_model` | str 또는 null | `"${LLM_ESCALATION_MODEL:-}"`(비면 `llm.model`) | 정체된 클러스터용 강한 모델. | `.env`로. | [04](04-triage-and-loop.md) |
| `llm.recipe_model` | str 또는 null | `"${LLM_RECIPE_MODEL:-}"`(비면 `llm.model`) | plan advisor·KB 레시피 후보용 저렴한 모델. | `.env`로. | [04](04-triage-and-loop.md) |
| `llm.temperature` | float | `0.0` | 샘플링 온도(`company` 제공자는 생성 파라미터를 보내지 않음). | 바꾸지 않습니다. | — |
| `llm.max_output_tokens` | int | `8000` | 응답 토큰 상한(company 제외). | 긴 패치가 잘릴 때. | — |
| `llm.max_context_chars` | int | `60000` | 요청당 컨텍스트 문자 예산. | 게이트웨이 입력 한도에 맞춰. | [04](04-triage-and-loop.md) |
| `llm.timeout_seconds` | int | `180` | 호출 1회 타임아웃. 하드 데드라인은 이 값 + 30초. | 게이트웨이가 느릴 때. | [09](09-operations.md) |
| `llm.max_retries` | int | `3` | 내장 클라이언트의 HTTP 재시도. | 게이트웨이 정책. | — |
| `llm.cooldown_seconds` | int | `60` | 두 번 연속 전송 실패 시 실행당 1회 쉬고 재시도(L5). 0 = 즉시 unavailable. | 게이트웨이 장애가 짧게 반복될 때. | [09](09-operations.md) |
| `llm.max_context_rounds` | int | `2` | LLM이 파일/grep을 더 요청할 수 있는 횟수(클러스터당). | 거의 바꾸지 않습니다. | — |
| `llm.extra_headers` | dict[str,str] | `{}` | 요청에 붙일 추가 헤더(비밀값 금지). | 게이트웨이가 팀 헤더 등을 요구할 때. | — |
| `llm.prompt_version` | str | `"v1"` | 프롬프트 버전 표식(기록/해시용). | 바꾸지 않습니다. | — |
| `llm.record_dir` | str 또는 null | `"${LLM_RECORD_DIR:-}"` | 모든 요청/응답을 `llm-NNNN.json`으로 저장(비밀값 없음). | 지원 요청·재생 테스트용으로 켬. | [09](09-operations.md) |
| `llm.pricing.<model>.input_per_mtok` / `.output_per_mtok` / `.cached_input_per_mtok` | float | `gpt-5.6-terra: 2.00 / 12.00 / 0.20`, `gpt-6-luna: 0.10 / 0.50 / 0.01` | 모델별 단가(1M 토큰당). 키는 게이트웨이가 보는 모델 id(정확히 일치 → 접두어/부분 일치). 보고서 "LLM 사용량과 비용"과 플릿 `summary.md`의 `llm_cost` 열이 이 표로 계산됩니다. 단가 없는 모델은 "단가 없음"으로 표시되고 합계에서 빠집니다. | 게이트웨이 청구 단가가 다르면 여기에 적음. 기본값은 OpenAI 공시 단가(2026-10-22). | [02](02-workflows.md) |
| `llm.pricing_currency` | str | `USD` | 단가 통화 표기. | 원화 청구면 `KRW`와 단가를 함께 바꿈. | — |
| `llm.pricing_note` | str | OpenAI 공시 단가 출처 문구 | 비용 표 아래에 그대로 인쇄되는 출처·기준일. | 단가를 바꾸면 같이 바꿈. | — |
| `llm.payload_extra` | dict | `{need_origin: true}` | 페이로드 최상위에 그대로 넣는 필드. **철자는 `need_origin`**(예전 `need_orgin` 오타가 503 원인). | 게이트웨이 요구 필드가 바뀔 때. `sbmig llm-probe --payload-extra`로 먼저 확인. | [09](09-operations.md) |
| `llm.response_path` | str 또는 null | `"${LLM_RESPONSE_PATH:-}"` | 응답 텍스트의 점 경로(예: `data.result.content`). 비면 자동 감지. | 자동 감지가 실패할 때만. | [09](09-operations.md) |

### 2.8 `fix` — tier 2 fixer 플러그인
| 키 | 타입 | 기본값 | 의미 | 언제 바꾸나 | 관련 문서 |
|---|---|---|---|---|---|
| `fix.plugin` | str 또는 null | null | `pkg.module:attr`(점 없는 이름은 진입점 그룹 `sbmig.fixers`) — sbmig의 LLMFixer를 사내 코딩 에이전트로 교체. | 사내 에이전트 프레임워크를 연결할 때. | [07](07-api-reference.md) |
| `fix.plugin_options` | dict | `{}` | 팩토리에 전달할 옵션. | 위와 같음. | [07](07-api-reference.md) |

### 2.9 `kb` — 파일 기반 지식 베이스
| 키 | 타입 | 기본값 | 의미 | 언제 바꾸나 | 관련 문서 |
|---|---|---|---|---|---|
| `kb.dir` | str | `kb` | KB 디렉터리(repo root 기준). | 플릿별로 KB를 분리하거나 공유 위치로 옮길 때. 테스트/데모는 `out/` 아래(불변식 6). | [05](05-directories-and-storage.md) |
| `kb.ledger` | bool | `true` | `<kb.dir>/ledger.jsonl`에 해결 기록 추가. | 끄지 않습니다. | [05](05-directories-and-storage.md) |
| `kb.symbol_index` | bool | `true` | `<kb.dir>/symbols.json` 유지 + 심볼 힌트. | 끄지 않습니다. | [05](05-directories-and-storage.md) |
| `kb.deterministic_candidates` | bool | `true` | 라벨에서 LLM 없이 레시피 후보 생성. | 끄지 않습니다. | [04](04-triage-and-loop.md) |
| `kb.use_active_recipes` | bool | `true` | 승격된 KB 레시피를 매 실행에 포함. | 문제 레시피를 의심할 때 일시적으로 false(또는 `--no-kb`). | [05](05-directories-and-storage.md) |
| `kb.use_patch_replay` | bool | `true` | 이전 성공 패치 재적용(LLM 없이). | 위와 같음. | [04](04-triage-and-loop.md) |
| `kb.use_hints` | bool | `true` | KB 힌트를 프롬프트에 포함. | 위와 같음. | — |
| `kb.harvest` | bool | `true` | 실행 결과를 KB에 수확. | 시험 실행에서 KB를 오염시키지 않으려면 false(또는 `--no-kb`). | [05](05-directories-and-storage.md) |
| `kb.generate_recipe_candidates` | bool | `true` | 수확 시 LLM(recipe 모델)으로 레시피 후보 생성. | LLM 예산 절약 시 false. | [05](05-directories-and-storage.md) |
| `kb.auto_promote_after_projects` | int | `2` | 서로 다른 N개 프로젝트에서 실패 없이 성공하면 후보 → active. 0 = 자동 승격 안 함. | 수동 검토만 허용할 때 0. | [05](05-directories-and-storage.md) |
| `kb.try_candidates_in_plan` | bool | `true` | 후보 레시피를 plan의 OpenRewrite 단계에서 함께 실행하고 단계 빌드 결과로 평가. | 후보가 빌드를 자주 깨뜨릴 때 false. | [05](05-directories-and-storage.md) |
| `kb.max_hints` | int | `5` | 프롬프트에 넣는 KB 힌트 수. | 거의 바꾸지 않습니다. | — |

### 2.10 `plan` — 계획
| 키 | 타입 | 기본값 | 의미 | 언제 바꾸나 | 관련 문서 |
|---|---|---|---|---|---|
| `plan.advisor` | `none` / `llm` | `none` | `llm` = recipe 모델이 규칙 기반 계획을 검토해 카탈로그 안의 레시피만 **추가**. | 검증 후에만. | [01](01-pipeline.md) |
| `plan.catalog_file` | str | `knowledge/recipes/catalog.yml` | 레시피 id의 단일 출처(불변식 5). | 바꾸지 않습니다. | `docs/RECIPES.md` |
| `plan.intermediate_builds` | bool | `true` | jdk21 단계 후 spring 레시피 전에 중간 빌드. | 바꾸지 않습니다. | [01](01-pipeline.md) |
| `plan.optional_bump_phase` | `spring` / `restructure` | `spring` | optional 사내 아티팩트 bump 시점(spring rewrite 직후 = 첫 빌드 전에 Jakarta 빌드가 classpath에). | 바꾸지 않습니다. | [01](01-pipeline.md) |
| `plan.parent_phase` | `restructure` / `spring` | `restructure`(**회사 예제: `spring`**) | 사내 부모/레이아웃 적용 시점. `spring` = spring rewrite 직후, spring build-fix 루프 전 → 루프가 회사 부모 체인 기준으로 고치고 부모 해석 실패가 빨리 드러남. | 회사 실행은 `spring`(사용자 결정, 2026-10-21). | [01](01-pipeline.md) |

### 2.11 `fleet` — 여러 프로젝트 순차 실행
| 키 | 타입 | 기본값 | 의미 | 언제 바꾸나 | 관련 문서 |
|---|---|---|---|---|---|
| `fleet.order` | `manifest` / `simple-first` | `manifest` | 실행 순서. `simple-first` = pure_java → servlet → spring_legacy → boot → msa. | KB를 쉬운 프로젝트로 먼저 채우고 싶을 때. | [09](09-operations.md) |
| `fleet.stop_on_abort` | bool | `false` | 한 프로젝트가 중단(종료 코드 3)되면 플릿 정지(Gradle/Ant 거부는 제외). | 환경 문제를 빨리 잡고 싶을 때. | [09](09-operations.md) |
| `fleet.retry_failed` | bool | `false` | resume 시 incomplete/aborted 재실행. | 원인 수정 후. | [09](09-operations.md) |
| `fleet.max_projects` | int | `0` | 이번 호출에서 실행할 최대 프로젝트 수(0 = 전부). | 하루 처리량 제한. | [09](09-operations.md) |
| `fleet.max_llm_calls_total` | int | `0` | 플릿 전체 LLM 예산(0 = 무제한). 각 프로젝트는 `min(loop.max_llm_calls, 남은 값)`. | 게이트웨이 사용량 제한. | [09](09-operations.md) |
| `fleet.reindex_every` | int | `10` | N개 프로젝트마다 `kb reindex`(0 = 안 함). | 거의 바꾸지 않습니다. | [05](05-directories-and-storage.md) |
| `fleet.summary_formats` | list[str] | `md`, `csv`, `json` | 요약 파일 형식(learning.md는 항상). | 필요 없는 형식 제외. | [09](09-operations.md) |

### 2.12 `report` — 보고서 출력
| 키 | 타입 | 기본값 | 의미 | 언제 바꾸나 | 관련 문서 |
|---|---|---|---|---|---|
| `report.formats` | list[str] | `md`, `json` | 마크다운/JSON 보고서 작성 여부. | 바꾸지 않습니다(웹 UI와 플릿이 JSON을 읽음). | [09](09-operations.md) |
| `report.zip_result` | bool | `true` | 결과 프로젝트를 `result.zip`으로(`.git`, `target` 제외, dry-run 제외). | 디스크 절약 시 false. | [05](05-directories-and-storage.md) |
| `report.include_diff` | bool | `true` | `changes.patch`(첫 커밋 대비 git diff 원본 바이트) 작성. | 바꾸지 않습니다. | [05](05-directories-and-storage.md) |
| `report.language` | str | `en` | `report.md`의 언어(`en` \| `ko`). 제목·표 머리글·안내 문장만 바뀌고 플래그 이름, 레시피/핸들러 id, 경로, Maven/javac 메시지, 루프가 남긴 영어 노트는 그대로입니다. `report.json`·콘솔·로그는 항상 영어. | 회사 운영자용으로 `ko`(`config/artifacts.example.yml`에 설정됨). | [02](02-workflows.md) |
| `report.javax_api_inventory` | bool | `true` | 최종 빌드 뒤 모듈마다 `mvn dependency:tree -Dincludes=<javax API 패턴>`을 돌려 클래스패스에 남은 javax API jar와 그것을 끌어오는 직접 의존성을 보고서 절 / 플래그 `javax-api-on-classpath` / Blocked 행의 `via`로 보여 줌(spring 단계가 돈 실행, dry-run 제외). | 그대로 둡니다. | [04](04-triage-and-loop.md) |

### 2.13 `advisories` — 라이브러리 권고(EOL/비관리 라이브러리)
| 키 | 타입 | 기본값 | 의미 | 언제 바꾸나 | 관련 문서 |
|---|---|---|---|---|---|
| `advisories.enabled` | bool | `true` | false면 권고 탐지·Handover 라이브러리 표 없음(버전 표는 유지). | 바꾸지 않습니다. | `docs/ADVISORIES.md` |
| `advisories.file` | str | `knowledge/libraries/advisories.yml` | 기본 권고 카탈로그. | 바꾸지 않습니다. | `docs/ADVISORIES.md` |
| `advisories.extra_files` | list[str] | `[]` | 회사 추가/덮어쓰기 파일(같은 스키마, id로 병합, 나중 파일 우선). | 사내 EOL 라이브러리를 등록할 때. | `docs/ADVISORIES.md` |
| `advisories.allow_recipes` | bool | `true` | false면 모든 권고가 `guide`(spring rewrite에 권고 레시피 추가 안 함). | 권고 레시피를 일괄 중지할 때. | `docs/ADVISORIES.md` |
| `advisories.policy_overrides` | dict[str, guide/recipe/off] | `{}` | id별 정책 덮어쓰기(예: `{springfox: guide, hystrix: off}`). **`block` 정책은 없습니다.** | 특정 권고 레시피를 끄거나 켤 때(`recipe`로 바꾸기 전 RUNBOOK §3.7 절차). | `docs/ADVISORIES.md` |
| `advisories.report_carried_over` | bool | `true` | ⚠ carried-over(새 부모가 관리하지 않아 as-is 버전으로 고정한) 의존성 목록 표시. | 바꾸지 않습니다. | [09](09-operations.md) |
| `advisories.ledger` | bool | `true` | 권고 결과를 ledger에 `resolution=advisory`로 기록. | 바꾸지 않습니다. | [05](05-directories-and-storage.md) |
| `advisories.max_from_here` | int | `40` | "From here on, your team" 목록 상한. | 목록이 너무 길 때. | [09](09-operations.md) |

### 2.14 `externalized` — 외부 설정값 자리표시자
| 키 | 타입 | 기본값 | 의미 | 언제 바꾸나 | 관련 문서 |
|---|---|---|---|---|---|
| `externalized.enabled` | bool | `true` | 코드가 읽지만 패키지 리소스에 없는 키(JNDI, context-param, `-D`, catalina.properties 등) 목록화. | 바꾸지 않습니다. | [06](06-restructure-and-layouts.md) |
| `externalized.sentinel` | str | `"__SET_ME__"` | 자리표시자 값(grep 가능). | 사내 표준 표식이 있을 때. | [06](06-restructure-and-layouts.md) |
| `externalized.write_placeholders` | bool | `true` | boot 모듈(MSA는 서비스별) `application.properties`에 표식 블록 추가. | 자리표시자를 원치 않을 때 false(Handover 표는 유지). | [06](06-restructure-and-layouts.md) |
| `externalized.placeholder_file` | str | `application.properties` | 자리표시자를 쓰는 파일(`src/main/resources` 기준). | 바꾸지 않습니다. | — |
| `externalized.rules_file` | str | `knowledge/config/externalized.yml` | JNDI / context-param / system-property → Boot 키 규칙과 안내문. | 회사 규칙 추가 시. | — |
| `externalized.include_test_refs` | bool | `true` | 테스트만 읽는 키도 자리표시자 생성. | 바꾸지 않습니다. | — |
| `externalized.max_refs_per_key` | int | `3` | Handover에 키당 보여 줄 참조 위치 수. | 보고서 길이 조절. | — |

### 2.15 최상위 키
| 키 | 타입 | 기본값 | 의미 | 언제 바꾸나 |
|---|---|---|---|---|
| `repo_root` | str | sbmig 저장소 루트(`PACKAGE_ROOT`) | 상대 경로 기준(`kb/`, `knowledge/` 등). | 저장소 밖에서 KB/knowledge를 쓸 때 `--repo-root`로. |

## 3. 설정 파일별 용도

### 3.1 `config/default.yml`
모든 실행이 가장 먼저 읽는 기본값입니다. **회사 값을 여기에 쓰지 마십시오.** 업데이트(`git pull`) 때 덮어써질 수 있고 저장소 공용 파일입니다. 회사 값은 별도 파일(아래 **config/artifacts.yml**)에 둡니다. 맨 위 주석대로 어떤 값이든 `SBMIG_섹션__키` 환경 변수로 덮어쓸 수 있습니다.

### 3.2 `config/artifacts.example.yml` — 회사 설정 템플릿(줄 단위 해설)
복사해서 **config/artifacts.yml**을 만듭니다(웹 UI는 `config/` 아래 `*.yml` 이름을 목록으로 보여 주므로 파일은 반드시 `config/` 안에 둡니다).

| 줄 | 내용 | 회사에서 할 일 |
|---|---|---|
| 1–11 | 주석: parent / boot_parent / comp_parent / mandatory / optional의 뜻. 7–11행의 "모듈 pom에 필수 의존성 2개"는 9b 시점 설명이고, 현재는 40행 `mandatory_target: root`가 우선합니다. | 읽기만. |
| 13–16 | `artifacts.parent` = `com.company.platform:base-parent:4.0.0` 자리표시자 | 실제 `xxx-parent` 좌표, 버전 4.0.0으로 교체. |
| 17–20 | `artifacts.boot_parent` = `boot-jar` 4.0.0 | 실제 `xxx-boot-jar` 좌표. |
| 21–24 | `artifacts.comp_parent` = `comp-jar` 4.0.0 | 실제 `xxx-comp-jar` 좌표. |
| 25 | `parent_manages_spring_boot: true` | 사내 boot-jar 부모가 Boot 4.1과 boot 플러그인 버전을 관리하는지 확인. 아니면 false. |
| 26 | `parent_manages_java_version: true` | 사내 부모가 Java 21을 설정하는지 확인. |
| 27–29 | `mandatory` 2개(`company-core`, `company-logging`, `version: null` = 부모 관리) | 실제 필수 의존성 2개 좌표. |
| 30–38 | `optional` 8개(`lib-auth`… `lib-web`, 3.0.0). `lib-auth`에 `aliases`, `lib-db`에 `property_name` 예시 | 실제 사내 라이브러리 8개와 버전(사내 라인 4.0.0이면 그 버전). 옛 좌표로 쓰이던 것은 `aliases`에. |
| 39 | `remove_when_present: []` | 옛 사내 BOM이 있으면 `g:a`로 추가. |
| 40–41 | `mandatory_target: root` | 그대로(회사 규칙). |
| 42 | `optional_version_policy: floor` | 그대로(정책상 정확한 버전 강제면 `pin`). |
| 44–48 | `plan.parent_phase: spring` | 그대로(회사 결정). |
| 50–52 | `build.settings_file: null` | `config/settings.nexus.example.xml`을 복사해 그룹 URL을 넣고 그 **절대 경로**를 지정. |
| 53 | `build.pom_repositories: keep` | 보통 그대로. pom의 죽은 URL을 지우려면 `remove`(미러 필수). |
| 54 | `build.java_home: null` | JDK 21 경로(예: `C:/Program Files/Java/jdk-21`). |
| 56 | llm 섹션 없음 | LLM은 `.env`로만 설정. |

### 3.3 `config/artifacts.local-e2e.yml` — 회사에서는 사용 금지
- "사내 부모"를 `spring-boot-starter-parent` 4.1.0으로, 필수 2개를 공개 스타터(validation, actuator)로, optional 8개를 공개 라이브러리(commons-lang3, guava, lombok …)로 **흉내 낸** 파일입니다.
- 9c 이전 레이아웃을 고정합니다(`module_dir: module`, `boot_module: boot-jar`, `comp_module: comp-jar`, `front_module: null`, `boot_as_is: layout`, `comp_artifact_suffix: "-comp"`). 회사 레이아웃과 다릅니다.
- `llm.provider: fake`(LLM 호출 없음), `kb.dir: out/e2e/kb`(저장소 `kb/`를 오염시키지 않음), `build.java_home: "${JAVA21_HOME:-}"`.
- 회사에서 이 파일로 실행하면 사내 부모 대신 공개 부모가 들어간 결과가 나오고 LLM이 동작하지 않습니다. 로컬 개발 PC의 e2e(`scripts/e2e.sh`)에서만 씁니다.

### 3.4 `config/llm-anthropic.yml` — Anthropic 검증 실행 전용
`llm.provider: anthropic`, `llm.model: claude-sonnet-5`, `llm.recipe_model: claude-haiku-4-5`, `llm.record_dir: out/llm-records`, `plan.advisor: llm`, `kb.generate_recipe_candidates: true`. 키는 `LLM_API_KEY` 환경 변수로만 넣습니다. 사내 게이트웨이 운영과 무관하며 회사 실행에 섞지 않습니다.

### 3.5 `config/llm-plugin.example.yml` — tier 1/2 플러그인 예제
`llm.provider: plugin`, `llm.plugin: examples.adapters.company_gateway_adapter:make_adapter`, `llm.api_key_env: COMPANY_LLM_KEY`, `llm.plugin_options`(base_url, 역할별 모델 표 등), `fix.plugin: null`. 사내 에이전트 프레임워크를 붙일 때의 출발점입니다. 상세는 [07. API 레퍼런스](07-api-reference.md)의 플러그인 계약 절.

### 3.6 `config/settings.nexus.example.xml` — Maven 미러
- `<mirrorOf>external:*</mirrorOf>` 미러 하나(id `company-nexus`, URL `https://nexus.example.com/repository/maven-public/` — **반드시 교체**)로 Central과 pom에 선언된 모든 원격 저장소를 Nexus 그룹으로 돌립니다. `file://`·localhost 저장소는 건드리지 않고 Maven 3.8+의 http 차단("Blocked mirror for repositories")도 우회합니다.
- 인증이 필요하면 주석 처리된 `<servers>` 블록을 `${env.NEXUS_USER}` 같은 환경 변수 참조로 씁니다(비밀번호를 파일에 쓰지 않음).
- 복사본 경로를 `build.settings_file`에 지정하면 sbmig가 모든 Maven 호출에 `-s`를 붙입니다. 이때 `~/.m2/settings.xml`은 읽히지 않습니다.
- Nexus 그룹이 프록시해야 하는 것: `org.openrewrite.*`(플러그인 6.46.1, 레시피 BOM 3.37.0과 그 아티팩트), `org.springframework.boot:*:4.1.0`, jakarta 아티팩트, 그리고 프로젝트 의존성.

### 3.7 `.env.example`
```text
LLM_BASE_URL=http://llm-gateway.example.internal/v1/chat     # 전체 엔드포인트 URL
LLM_API_KEY=put-the-bearer-token-here
LLM_MODEL=gpt-6-luna                          # fix 모델
# LLM_ESCALATION_MODEL=                        # 비우면 LLM_MODEL
# LLM_RECIPE_MODEL=                            # 비우면 LLM_MODEL
# LLM_RESPONSE_PATH=data.result.content       # llm-probe가 응답을 못 찾을 때만
LLM_RECORD_DIR=out/llm-records
JAVA21_HOME=C:/Program Files/Java/jdk-21
# SBMIG_WEB_OUT=out/web                     # 순수 uvicorn일 때만(`sbmig serve`는 --out)
# SBMIG_MAX_UPLOAD_MB=0                     # 웹 업로드 한도(MB), 0 = 없음(기본 512); 서버가 뜰 때 읽음
```
저장소 루트에 `.env`로 복사합니다(git-ignored). LLM 변수는 실행마다, `SBMIG_WEB_*`와 `SBMIG_MAX_UPLOAD_MB`는 서버가 뜰 때 읽으므로 바꾸면 서버를 다시 띄웁니다. `JAVA21_HOME`은 회사 설정에서 자동으로 쓰이지 않습니다(위 1.3) — 회사에서는 `build.java_home`을 직접 지정합니다.

## 4. 회사에서 처음 설정할 때 체크리스트
1. `config/artifacts.example.yml`을 **config/artifacts.yml**로 복사합니다.
2. `artifacts.parent` = `xxx-parent`(4.0.0), `artifacts.boot_parent` = `xxx-boot-jar`(4.0.0), `artifacts.comp_parent` = `xxx-comp-jar`(4.0.0) 실제 좌표를 넣습니다(이 저장소는 실제 좌표를 모릅니다 — 추측해 넣지 마십시오).
3. `artifacts.mandatory`에 필수 의존성 **2개**, `artifacts.optional`에 사내 라이브러리 **8개**(사내 라인 버전)를 넣습니다. 옛 좌표로 쓰이던 라이브러리는 `aliases`를 채웁니다.
4. `artifacts.parent_manages_spring_boot` / `artifacts.parent_manages_java_version`이 사내 부모의 실제 동작과 맞는지 확인합니다.
5. `plan.parent_phase: spring`이 들어 있는지 확인합니다(템플릿에 있음).
6. `build.java_home`에 JDK 21 절대 경로를 넣습니다.
7. `config/settings.nexus.example.xml`을 복사해 Nexus 그룹 URL을 넣고, 그 절대 경로를 `build.settings_file`에 넣습니다.
8. Windows에서 `where mvn`이 Maven을 못 찾으면 `build.mvn`에 `mvn.cmd` 절대 경로를 넣습니다.
9. `.env.example`을 저장소 루트 `.env`로 복사해 `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`(필요하면 `LLM_RECIPE_MODEL`, `LLM_ESCALATION_MODEL`, `LLM_RECORD_DIR`)을 채웁니다.
10. 확인: `sbmig llm-probe`(설정 없이 `.env`만), `sbmig recipes check --config config/artifacts.yml`, `sbmig advisories check --config config/artifacts.yml`, 그리고 대표 프로젝트로 `sbmig analyze <프로젝트> --config config/artifacts.yml`.

```powershell
Copy-Item config\artifacts.example.yml config\artifacts.yml
Copy-Item config\settings.nexus.example.xml config\settings.nexus.xml
Copy-Item .env.example .env
notepad config\artifacts.yml
.venv\Scripts\sbmig.exe llm-probe
.venv\Scripts\sbmig.exe recipes check --config config\artifacts.yml
```

## 5. 자주 바꾸는 키 Top 10
| 순위 | 키 | 왜 자주 바꾸나 |
|---|---|---|
| 1 | `build.java_home` | 호스트마다 JDK 21 경로가 다릅니다. 첫 실행 전 필수. |
| 2 | `build.settings_file` | 폐쇄망 Nexus 미러. pom 선언 저장소 문제(X9)의 표준 해법. |
| 3 | `artifacts.optional` | 사내 라이브러리의 Jakarta 빌드가 새로 나오거나 X1 차단을 풀 때 추가/버전 갱신. |
| 4 | `loop.max_llm_calls` | 프로젝트 규모와 게이트웨이 예산에 맞춘 조정. |
| 5 | `loop.phases.spring.max_iterations` | 오류가 많은 프로젝트에서 라운드가 모자랄 때(`max fix rounds (6) used up`). |
| 6 | `openrewrite.timeout_minutes` / `openrewrite.jvm_args` | 대형 프로젝트의 rewrite 시간·메모리. |
| 7 | `openrewrite.exclusions` | 생성 소스·대용량 리소스를 파싱에서 제외해 rewrite 시간 단축(리스트 전체를 다시 적음). |
| 8 | `build.extra_args` | `-llr` 등 프로젝트별 Maven 옵션(리스트 전체를 다시 적음). |
| 9 | `advisories.policy_overrides` | 특정 권고 레시피를 끄거나(guide/off) 검증 후 켬(recipe). |
| 10 | `build.skip_tests` / `build.skip_test_compile` | 테스트 정책. 회사 결정(2026-10-22)은 테스트 전부 건너뛰기: `skip_test_compile: true`(컴파일도 안 함). 기본값은 실행만 안 함. |

프로젝트 한 개에만 적용하려면 별도 yml(예: config/site-overrides.yml 같은 이름으로 직접 생성)을 두 번째 `--config`로 주거나, 플릿 manifest의 `overrides`에 씁니다(`kb.dir`은 플릿 전체 공통이라 프로젝트별로 바꿀 수 없습니다).
