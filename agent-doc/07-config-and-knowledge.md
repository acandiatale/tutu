# 설정과 지식 데이터

> 다루는 것: 설정이 어떻게 읽히고 겹쳐지는지, Config 모델의 섹션별 핵심 키, 회사 설정 파일 패턴, `knowledge/` 폴더의 표와 노트, 재배포 없이 회사가 더할 수 있는 파일, 저장소에 절대 넣으면 안 되는 것.
> 언제 읽나: 회사 값(부모 pom·필수/선택 아티팩트·Nexus·LLM)을 넣을 때, 동작을 키로 켜고 끌 때, 노트·표를 고치거나 더할 때.
> 함께 읽을 파일: [08-dev-conventions-and-tests.md](08-dev-conventions-and-tests.md), [09-deploy-and-ops.md](09-deploy-and-ops.md), [06-company-rules-restructure.md](06-company-rules-restructure.md), [05-llm-gateway-and-prompts.md](05-llm-gateway-and-prompts.md)

## 1. 설정이 읽히는 순서

모든 명령(`sbmig run`, `sbmig fleet`, `sbmig serve` 의 실행, `sbmig kb …`)과 `sbmig.api.migrate` 는 `sbmig/core/config.py` 의
`load_config(*paths, overrides=None, repo_root=None)` 로 시작한다. 이 함수가 하는 일을 순서대로 적으면 다음과 같다.

```
0. load_dotenv_files()      .env 들을 os.environ 으로 읽는다 (설정 dict 에는 넣지 않는다)
1. config/default.yml       항상 먼저 (DEFAULT_CONFIG_PATH = <repo>/config/default.yml)
2. --config a.yml --config b.yml ...   주어진 순서대로 deep_merge (뒤의 것이 이김)
3. overrides (dict)         CLI 옵션(--java-home, --settings, --llm-provider ...), 플릿 매니페스트의 프로젝트별 overrides
4. SBMIG_<SECTION>__<KEY>   환경 변수 덮어쓰기 (밑줄 두 개 = 한 단계 중첩, 값은 YAML 로 해석)
5. ${VAR} / ${VAR:-기본값}  문자열 안의 환경 변수 펼침 (없고 기본값도 없으면 그대로 남음)
6. 옛 키 경고 (_warn_removed_keys) → Config.model_validate (pydantic, 모르는 값은 여기서 실패)
```

- **deep_merge 규칙**: 양쪽이 dict 이면 재귀로 합치고, 그 밖(목록·문자열·숫자)은 통째로 바뀐다. 그래서 회사 yml 에
  `artifacts.optional` 목록을 쓰면 기본 목록에 더해지는 것이 아니라 **교체**된다. `openrewrite.recipes.jakarta` 같은 레시피 목록도 같다.
- **환경 변수 덮어쓰기 예**: `SBMIG_LLM__MODEL=foo` → `llm.model = "foo"`, `SBMIG_LOOP__MAX_LLM_CALLS=0` → 숫자 0,
  `SBMIG_REVIEW__ENABLED=false` → 불리언. 키 이름은 소문자로 바뀐다.
- **`${VAR}`**: `config/default.yml` 의 `llm.base_url: "${LLM_BASE_URL}"`, `llm.model: "${LLM_MODEL:-gpt-6-luna}"`,
  `llm.gateway: "${LLM_GATEWAY:-legacy}"` 처럼 LLM 값은 거의 다 환경 변수에서 온다. JDK 경로도 같은 방식이 편하다:
  회사 yml 에 `build.java_home: "${JAVA21_HOME:-}"` (로컬 e2e 설정 `config/artifacts.local-e2e.yml` 이 이렇게 한다).
  sbmig 코드는 `JAVA21_HOME` 을 직접 읽지 않는다 — yml 이 참조하거나 `--java-home` 으로 줄 때만 쓰인다.
- **`repo_root`**: 상대 경로 키(`kb.dir`, `plan.catalog_file`, `advisories.file`, `kb.redact_file` …)는 `Config.resolve_path` 가
  레포 루트(기본은 `sbmig/` 옆 폴더) 기준으로 푼다. `--repo-root` 로 바꿀 수 있다.

### `.env` 를 찾는 곳

`load_dotenv_files` 는 다음 후보를 읽는다: `$SBMIG_ENV_FILE`, 패키지 폴더에서 위로 git 루트(`.git` 이 있는 폴더)까지의 모든 `.env`,
현재 디렉토리에서 위로 git 루트까지의 모든 `.env`. 먼 파일부터 읽어 **가까운 파일이 이기고**, 현재 디렉토리 쪽이 패키지 쪽을 이긴다.
**셸에서 이미 설정된 변수는 절대 덮어쓰지 않는다.** 파일이 수정되면 다음 실행(웹 서버의 다음 실행, 플릿의 다음 프로젝트)에서 다시 읽는다.
인코딩은 UTF-8(BOM 포함)·UTF-16(PowerShell 5.1 `>`)·cp949 를 모두 받아들이고, `export KEY=…`, 따옴표, `# 주석` 을 허용한다
(`KEY=   # 주석` 은 빈 값). sbmig 를 다른 레포의 하위 폴더(`<root>/src/migration-agent/`)에 두어도 루트의 `.env` 를 찾는 이유가 이것이다.
`.env` 의 키는 Config 에 들어가지 않고 환경 변수로만 쓰인다. 템플릿은 `.env.example` 이다(LLM 게이트웨이 변수, `LLM_RECORD_DIR`,
`JAVA21_HOME`, 웹 변수 `SBMIG_WEB_OUT` / `SBMIG_MAX_UPLOAD_MB`).

## 2. Config 모델의 섹션 (지금 코드의 이름 그대로)

최상위 필드는 `Config` 클래스(`sbmig/core/config.py`)에 있다: `target`, `artifacts`(필수 — 기본값 없음), `layout`, `intake`,
`openrewrite`, `build`, `loop`, `llm`, `fix`, `script`, `review`, `errors`, `libraries`, `kb`, `plan`, `fleet`, `report`,
`advisories`, `externalized`, 그리고 `repo_root`. **`web` 섹션은 없다** — 웹 UI 는 `sbmig serve` 옵션과 환경 변수
(`SBMIG_WEB_TOKEN`, `SBMIG_MAX_UPLOAD_MB`, `SBMIG_WEB_OUT`)로 설정한다([09-deploy-and-ops.md](09-deploy-and-ops.md)).

| 섹션 | 무엇을 다스리나 | 운영자가 주로 바꾸는 키 |
|---|---|---|
| `target` | 목표 버전(정보용 문자열). Java 21 / Boot 4.1.0 / Framework 7.0.x | 보통 그대로: `target.java_version`, `target.spring_boot_version` |
| `artifacts` | 회사 부모 pom 과 사내 아티팩트. 루트 pom 의 부모, boot 모듈·그 외 모듈의 부모, 필수 2개, 선택 8개 | `artifacts.parent`, `artifacts.boot_parent`, `artifacts.comp_parent`, `artifacts.mandatory`, `artifacts.optional`, `artifacts.parent_manages_spring_boot` |
| `layout` | 재구성 모양: 모듈 이름(`{name}-boot` / `-back` / `-front`), Boot as-is 를 제자리로 둘지, JSP 패키징, 다중 모듈 정책, 버전 정리 규칙, 레거시 Spring 컨텍스트 스위치(`layout.legacy_context.*`) | `layout.boot_module`, `layout.comp_module`, `layout.front_module`, `layout.boot_as_is`, `layout.multi_module_policy`, `layout.wire_bundled_jars` |
| `intake` | 접수 규칙: Ant → pom 합성, 동봉 jar 스캔, jar 이름 표, ADF 정책 | `intake.ant_to_maven`, `intake.synth_group_id`, `intake.unknown_jar`, `intake.bundled_jars`, `intake.adf_policy` |
| `openrewrite` | OpenRewrite 1회 실행: 플러그인·레시피 BOM 버전, 레시피 세트, JVM 옵션, 제외 파일 | `openrewrite.recipes.jakarta`, `openrewrite.recipes.extra`, `openrewrite.jvm_args`, `openrewrite.timeout_minutes`, `openrewrite.recipe_bom_version` |
| `build` | Maven 호출: `mvn` 경로, settings.xml, JDK, 테스트 건너뛰기, 타임아웃, 저장소 조회 주소, 파일 잠금 재시도, 실행 전후 훅 | `build.mvn`, `build.settings_file`, `build.java_home`, `build.repository_url`, `build.local_repo`, `build.non_filtered_extensions` |
| `loop` | 수정 루프: 단계별 라운드 수(`loop.phases.verify`), LLM 총 호출 상한, 시간 상한, 차단 정책, 진동 감지 | `loop.phases.verify.max_iterations`, `loop.max_llm_calls`, `loop.max_total_minutes`, `loop.blocked_policy`, `loop.oscillation_limit` |
| `llm` | LLM 게이트웨이: provider, URL, 모델, 게이트웨이 모양(legacy / agent), 토큰 교환, 케이스 스레드, 기록 폴더, 가격표 | `llm.provider`, `llm.gateway`, `llm.model`, `llm.escalation_model`, `llm.record_dir`, `llm.case_thread` |
| `fix` | 어댑터 2단: 수정기 전체를 사내 코딩 에이전트로 바꾸는 플러그인 | `fix.plugin`, `fix.plugin_options` |
| `script` | 수정 스크립트 실행과 사다리 상한: 타임아웃, 스크립트용 파이썬, 재사용, 보강·LLM·거절·직접 수정 횟수 | `script.python`, `script.timeout_seconds`, `script.max_augments_per_case`, `script.max_llm_tries_per_case`, `script.max_declines_per_case`, `script.max_edit_tries_per_case` |
| `review` | 재구성 뒤 LLM 구조 검토: 켜기, critical 자동 수정, 프롬프트에 넣을 pom 크기, 불변조건 재검사 | `review.enabled`, `review.fix_critical`, `review.max_pom_chars`, `review.invariant_check` |
| `errors` | 오류 목록 기록 형식: 저장할 diff·원문 길이, 사람이 읽는 md / json 파일 | `errors.diff_chars`, `errors.md_view`, `errors.json_files` |
| `libraries` | 의존성 버전 정책과 jakarta 빌드 찾기: 저장소 조회, 회사 표 파일, Nexus 검색, jar 스캔 한도 | `libraries.lookup`, `libraries.company_file`, `libraries.discover_jakarta_builds`, `libraries.nexus_search` |
| `kb` | 파일 KB 위치·비밀 마스킹 규칙·잠금 대기 | `kb.dir`, `kb.redact_file`, `kb.lock_timeout_seconds` |
| `plan` | 계획: 레시피 카탈로그 파일, LLM 계획 조언자, 변환 직후 컴파일 여부 | `plan.catalog_file`, `plan.advisor`, `plan.compile_after_rewrite` |
| `fleet` | 일괄 실행: 순서, 재시도, 사전 점검, 인프라 차단기, 정리, 요약 형식 | `fleet.order`, `fleet.preflight`, `fleet.cleanup`, `fleet.min_free_gb`, `fleet.max_llm_calls_total` |
| `report` | 보고서: 형식, 결과 zip, diff 포함, 언어(보고서 md 만 en / ko) | `report.language`, `report.formats`, `report.zip_result` |
| `advisories` | EOL 라이브러리 권고(안내만, 차단 없음): 기본 파일, 회사 추가 파일, 항목 끄기 | `advisories.extra_files`, `advisories.policy_overrides`, `advisories.enabled` |
| `externalized` | 코드가 읽지만 어떤 리소스도 정의하지 않는 설정 키 → 자리표시자 블록과 인수인계 목록 | `externalized.enabled`, `externalized.sentinel`, `externalized.rules_file` |

몇 가지 주의할 기본값(지금 코드 기준):

- `build.skip_test_compile: true` — 모든 빌드가 `-Dmaven.test.skip=true` 로 돈다(테스트는 범위 밖).
- `loop.phases.verify` 의 기본 `max_iterations` 는 `config/default.yml` 에서 50 이다. 다른 단계(`jdk21`, `spring`, `restructure`)는 꺼져 있다.
- `loop.max_llm_calls: 0` 은 "상한 없음", `-1` 은 "LLM 호출 전혀 없음" 이다.
- `loop.blocked_policy` 는 차단 이유별 LLM 시도 수다(`reactor_duplicate`, `file_lock`, `needs_owner`, `parent_not_found` 는 0 = LLM 에 묻지 않음).
- `plan.compile_after_rewrite: false` — 재구성 전에는 빌드하지 않는다.
- `llm.case_thread: false` — 케이스 스레드는 기본 꺼짐.
- `openrewrite.recipe_bom_version` 은 3.37.0 으로 고정(아래 4절).

`load_config` 는 지워진 옛 키(`fix.mode` 등)가 회사 yml 에 남아 있으면 경고 로그를 남긴다. 조용히 무시되면 회사 설정의 뜻이 바뀐 줄 모르기 때문이다.

## 3. 회사 설정 파일 패턴

| 파일 | 저장소에 있나 | 역할 |
|---|---|---|
| `config/default.yml` | 있음 | 모든 기본값. 회사 값을 여기 쓰지 않는다 |
| `config/artifacts.example.yml` | 있음 | 회사 yml 의 견본. 좌표는 **전부 자리표시자**(`com.company.platform:base-parent:4.0.0`, `company-core`, `lib-auth` …) |
| `config/artifacts.yml` | 없음(git-ignore) | 회사 PC 에서 `cp config/artifacts.example.yml config/artifacts.yml` 로 만들고 실제 좌표를 채운다 |
| `config/company.yml` | 없음 | 컨테이너에서 ConfigMap 으로 들어가는 회사 yml(`deploy/k8s/20-configmap.yaml`), 내용은 위와 같은 종류 + 컨테이너 경로 |
| `config/artifacts.local-e2e.yml` | 있음 | 로컬 e2e 용 **공개 대역**: 부모 = `spring-boot-starter-parent` 4.1.0, 필수·선택 = 공개 라이브러리, 예전 레이아웃(`module/boot-jar` + `module/comp-jar`), `llm.provider: fake`, `kb.dir: out/e2e/kb` |
| `config/llm-anthropic.yml` | 있음 | Anthropic API 로 검증할 때 겹쳐 쓰는 yml(`llm.provider: anthropic`, 키는 여전히 `LLM_API_KEY` 환경 변수) |
| `config/llm-plugin.example.yml` | 있음 | 사내 LLM 어댑터(`llm.plugin: "pkg.module:attr"`)와 수정기 플러그인(`fix.plugin`)을 꽂는 견본 |
| `config/settings.nexus.example.xml` | 있음 | Nexus 미러 settings.xml 견본(`<mirrorOf>external:*</mirrorOf>`). 복사해 `config/settings.nexus.xml` 등으로 두고 `build.settings_file` 로 가리킨다 |

**반드시 바꿔야 하는 자리표시자**: `artifacts.parent` / `artifacts.boot_parent` / `artifacts.comp_parent` 의 g:a:v, `artifacts.mandatory`
2개, `artifacts.optional` 8개(별칭 `aliases`, 트리거 `when_present` 가 있으면 함께), `intake.synth_group_id`(Ant 합성 pom 의 groupId),
settings.xml 의 Nexus 그룹 URL 과 `build.repository_url`(같은 Nexus — 하나는 Maven 용, 하나는 sbmig 가 직접 메타데이터를 읽는 용).
실행은 `sbmig run <프로젝트> --config config/artifacts.yml` 이고, 여러 yml 을 겹칠 때는 `--config` 를 여러 번 준다(뒤가 이김).

## 4. `knowledge/` — 배포되는 지식 데이터

`knowledge/` 는 코드와 함께 배포되는 YAML / Markdown 이다. 회사 규칙과 라이브러리 사실은 Python 이 아니라 여기에 둔다(불변조건 2).

### 레시피 카탈로그 `knowledge/recipes/catalog.yml`

- sbmig 가 아는 **모든 OpenRewrite 레시피 id 의 단일 출처**. 읽는 코드는 `sbmig/plan/catalog.py`(`RecipeCatalog`).
- 각 항목: `id`, `origin`(openrewrite 만), `artifact`(레시피 jar), `phase`, `description`, `docs`, `covers`(묶음 레시피가 이미 포함하는 것),
  `gate`(프로필 조건), `advisor`(LLM 조언자가 더할 수 있나), `sets`(이 id 를 참조하는 `openrewrite.recipes` 세트 이름), `verified`, `notes`.
- **카탈로그는 무엇을 돌릴지 정하지 않는다.** 실제 실행 세트는 `openrewrite.recipes` 의 `jdk21`, `jakarta`, `spring_boot_4`,
  `spring_framework_7`, `spring_security_7`, `junit5`(빈 목록), `spring_batch_6`, `hibernate_7`, `servlet_jakarta_10`, `extra` 이고,
  계획기가 케이스 종류와 감지된 라이브러리로 세트를 고른다(`sbmig recipes sets` 가 보여 준다).
- 회사 yml 이 빈 세트를 채우거나 `openrewrite.recipes.extra` 에 id 를 더하면, 그 id 를 카탈로그 항목의 `sets:` 에도 적어야 한다.
  어긋나면 `sbmig recipes check` 가 exit 1 이다(`--jars` 를 주면 Maven 으로 레시피 jar 를 받아 id 가 정말 있는지도 본다 — 네트워크 필요).
- 레시피 BOM 은 `openrewrite.recipe_bom_version` = 3.37.0 으로 고정이다(3.38.0 은 Maven Central 에서 불완전). 플러그인은
  `openrewrite.plugin_version`.
- 지키는 테스트: `tests/plan/test_catalog.py`(옛 조언자 사전의 스냅샷 `OLD_ADVISOR_CATALOG`, 카탈로그와 모든 설정 세트의 일치,
  배포되는 세 설정 모두에서 `check` 가 빈 결과). `scripts/e2e.sh` 도 시작할 때 `recipes check --jars` 와 `advisories check` 를 돌린다.

### 라이브러리 표 `knowledge/libraries/`

| 파일 | 내용 | 읽는 코드 |
|---|---|---|
| `knowledge/libraries/advisories.yml` | EOL / 관리 끊긴 라이브러리 권고(springfox, hystrix …): 상태, 대체품, 노트. 안내와 인수인계만, 차단 없음 | `sbmig/analyze/libraries.py`, `sbmig/advisory/` (`advisories.file`, 회사 추가는 `advisories.extra_files`) |
| `knowledge/libraries/jakarta_libraries.yml` | javax 시대 서드파티 라이브러리의 jakarta 릴리스(어느 버전부터, 권장 버전, 필요한 Java) | `sbmig/core/jakarta_libraries.py` |
| `knowledge/libraries/libraries.company.example.yml` | 위와 같은 스키마의 회사 표 견본 → `<kb>/libraries.yml` 로 둔다 | 같음(`libraries.company_file`, 회사 항목이 이김) |
| `knowledge/libraries/jar_names.yml` | jar 파일 이름 줄기 → Maven 좌표 표(Ant 접수, 동봉 jar) | `sbmig/analyze/jar_coords.py` (`intake.jar_names_file`) |

### 그 밖의 규칙 표 `knowledge/config/`, `knowledge/kb/`

| 파일 | 내용 | 읽는 코드 |
|---|---|---|
| `knowledge/config/build_env_errors.yml` | 환경 오류 패턴(Windows 파일 잠금 등) — LLM 에 묻지 않고 재빌드 | `sbmig/build/env_errors.py` |
| `knowledge/config/externalized.yml` | JNDI / context-param / 시스템 속성 → Boot 키 규칙 | `sbmig/analyze/properties.py` (`externalized.rules_file`) |
| `knowledge/config/web_xml_rules.yml` | web.xml 의 서블릿·필터·리스너를 Boot 등록으로 바꿀지 건너뛸지 | `sbmig/restructure/webxml_registrations.py` |
| `knowledge/config/legacy_context_rules.yml` | 레거시 Spring 컨텍스트(ContextLoaderListener 등) 해석 규칙 | `sbmig/restructure/legacy_context.py` |
| `knowledge/config/spring_cloud_bom.yml` | Boot 버전 ↔ Spring Cloud 트레인 짝 | `sbmig/build/cloud_bom.py` |
| `knowledge/kb/redact.yml` | KB 에 적기 전에 가리는 비밀 패턴(회사 패턴을 여기 더함) | `sbmig/kb/redact.py` (`kb.redact_file`) |

### 마이그레이션 노트 `knowledge/notes/`

- `*.md` 노트(jdk21, jakarta, boot3-to-4, spring-security-7, spring-mobile, spring-removed-artifacts …)와 색인 `knowledge/notes/index.yml`.
- 노트가 프롬프트에 들어가는 길: 수정 루프의 `ContextPacker`(`sbmig/fix/context.py`)가 색인의 `keywords`(정규식)를 오류 문장·심볼·위치·원문·
  아티팩트에 맞춰 본다. 오류 본문에 맞으면 키워드당 3점, import / pom 에만 맞으면 1점. 오류 본문에 맞은 노트가 하나라도 있으면 그것들만,
  최대 4개(`DEFAULT_MAX_NOTES`)가 프롬프트에 실린다. `cases` 는 케이스 종류 제한, `phases` 는 참고용, `always` 는 모두 false 다.
- 색인과 각 노트의 앞머리(front-matter: `title`, `keywords`, `cases`, `phases`, `always`)는 **같아야 하고**, 노트 하나는 **160줄 이하**여야 한다.
  `tests/fix/test_fix_notes.py` 가 둘 다 검사한다. 노트를 더하면 색인에도 항목을 더한다.

## 5. 재배포 없이 회사가 더할 수 있는 것 (`kb.dir` 아래)

`kb.dir`(기본 `kb-v2`, 컨테이너에서는 `/data/kb-v2` 등)은 실행이 쓰는 KB 이면서, 사람이 표를 놓는 곳이기도 하다. 다음 파일은 코드 배포 없이
그 자리에 두면 다음 실행부터 읽힌다.

| 파일 | 키 | 형식 |
|---|---|---|
| `kb-v2/jar_names.yml` | `intake.company_jar_names_file` | `knowledge/libraries/jar_names.yml` 과 같음, 배포 표 위에 합쳐짐 |
| `kb-v2/libraries.yml` | `libraries.company_file` | `knowledge/libraries/libraries.company.example.yml` 과 같음, 회사 항목이 이김 |

권고는 `advisories.extra_files` 에 회사 파일 경로를 적으면(같은 스키마, id 로 합쳐짐) 더해지고, `advisories.policy_overrides` 로 항목을 끈다
(`{hystrix: off}`). 비밀 마스킹 패턴은 `kb.redact_file` 이 가리키는 파일에 더한다.

## 6. 저장소와 yml 에 절대 넣지 않는 것

- **키·토큰**: LLM 키는 환경 변수(`LLM_API_KEY`, `LLM_APP_TOKEN`)로만. yml 에는 변수 **이름**만(`llm.api_key_env`, `llm.app_token_env`).
  `.env` 와 `deploy/k8s/30-secret.yaml` 은 git-ignore 이고, 기록 파일(`llm.record_dir`)에도 키는 남지 않는다.
- **실제 사내 좌표**: 저장소의 yml·노트·테스트·프롬프트에는 자리표시자만(`com.company.*`, `com.example.*`). 실제 값은 회사 PC 의
  `config/artifacts.yml` 에만 있다. 공개 대역은 `config/artifacts.local-e2e.yml`.
- **버전 숫자를 코드·프롬프트에 박기**: 버전은 부모 체인·Boot BOM·as-is 빌드·저장소에서 사실로 읽는다. 표에 적는 것은 사람이 확인한 값뿐이다.
- 회사 KB 데이터(`kb-v2/`)는 git-ignore 이다. 테스트·데모 실행은 `out/` 아래 KB 만 쓴다(`out/e2e/kb-<fixture>`).

문서가 코드에서 떨어지지 않게 지키는 테스트는 `tests/docs/test_handbook.py`(한국어 핸드북의 경로·설정 키·CLI·링크)와 이 팩용
`tests/docs/test_agent_doc.py`, 레시피 쪽은 `tests/plan/test_catalog.py` 이다.

## 확인하는 법

```bash
grep -n "^def load_config" -A25 sbmig/core/config.py            # 읽는 순서
grep -n "^class Config" -A25 sbmig/core/config.py                # 최상위 섹션 이름
grep -n "def _apply_env_overrides\|def load_dotenv_files\|def dotenv_chain" sbmig/core/config.py
grep -v '^\s*#' config/default.yml | grep -v '^\s*$'             # 기본값만 (주석 빼고)
.venv/bin/python -c "from sbmig.core.config import load_config; c=load_config('config/artifacts.local-e2e.yml'); print(c.kb.dir, c.llm.provider)"
SBMIG_LLM__MODEL=x .venv/bin/python -c "from sbmig.core.config import load_config; print(load_config('config/artifacts.local-e2e.yml').llm.model)"
.venv/bin/python -m sbmig.cli recipes check --config config/artifacts.local-e2e.yml
.venv/bin/python -m sbmig.cli recipes sets --config config/artifacts.local-e2e.yml
.venv/bin/python -m sbmig.cli advisories check --config config/artifacts.local-e2e.yml
.venv/bin/python -m pytest tests/plan/test_catalog.py tests/fix/test_fix_notes.py -p no:cacheprovider -q
grep -rn "company_jar_names_file\|company_file" sbmig/analyze/jar_coords.py sbmig/core/jakarta_libraries.py
```
