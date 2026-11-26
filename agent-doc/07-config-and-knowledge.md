# 설정과 지식 데이터

> 다루는 것: 설정이 어떻게 읽히고 겹쳐지는지, Config 모델의 섹션별 핵심 키, 회사 설정 파일 패턴, `knowledge/` 폴더의 표와 노트, 재배포 없이 회사가 더할 수 있는 파일, 저장소에 절대 넣으면 안 되는 것.
> 언제 읽나: 회사 값(부모 pom·필수/선택 아티팩트·Nexus·LLM)을 넣을 때, 동작을 키로 켜고 끌 때, 노트·표를 고치거나 더할 때.
> 함께 읽을 파일: [08-dev-conventions-and-tests.md](08-dev-conventions-and-tests.md), [09-deploy-and-ops.md](09-deploy-and-ops.md), [06-company-rules-restructure.md](06-company-rules-restructure.md), [05-llm-gateway-and-prompts.md](05-llm-gateway-and-prompts.md)

## 1. 설정이 읽히는 순서

모든 명령(`sbmig run`, `sbmig fleet`, `sbmig serve`의 실행, `sbmig kb …`)과 `sbmig.api.migrate`는 `sbmig/core/config.py`의
`load_config(*paths, overrides=None, repo_root=None)`로 시작한다. 순서:

```
0. load_dotenv_files()      .env 들을 os.environ 으로 읽는다 (설정 dict 에는 넣지 않는다)
1. config/default.yml       항상 먼저 (DEFAULT_CONFIG_PATH = <repo>/config/default.yml)
2. --config a.yml --config b.yml ...   주어진 순서대로 deep_merge (뒤의 것이 이김)
3. SBMIG_<SECTION>__<KEY>   환경 변수 덮어쓰기 (밑줄 두 개 = 한 단계 중첩, 값은 YAML 로 해석)
4. overrides (dict)         명시한 값: CLI 옵션(--java-home, --settings, --llm-provider ...), 플릿 매니페스트의 프로젝트별 overrides.
                            환경 값보다 이긴다(바꾼 값마다 INFO "config: <키> given explicitly replaces <변수> from the environment")
5. ${VAR} / ${VAR:-기본값}  문자열 안의 환경 변수 펼침 (없고 기본값도 없으면 그대로 남음)
6. 옛 키 경고 (_warn_removed_keys) → Config.model_validate (pydantic, 모르는 값은 여기서 실패)
```

- deep_merge 규칙: 양쪽이 dict이면 재귀로 합치고, 그 밖(목록·문자열·숫자)은 통째로 바뀐다. 그래서 회사 yml에
  `artifacts.optional` 목록을 쓰면 기본 목록에 더해지는 것이 아니라 교체된다. `openrewrite.recipes.jakarta` 같은 레시피 목록도 같다.
- 환경 변수 덮어쓰기 예: `SBMIG_LLM__MODEL=foo` → `llm.model = "foo"`, `SBMIG_LOOP__MAX_LLM_CALLS=0` → 숫자 0,
  `SBMIG_REVIEW__ENABLED=false` → 불리언. 키 이름은 소문자로 바뀐다.
- `${VAR}`: `config/default.yml`의 `llm.base_url: "${LLM_BASE_URL}"`, `llm.model: "${LLM_MODEL:-gpt-6-luna}"`,
  `llm.gateway: "${LLM_GATEWAY:-legacy}"` 처럼 LLM 값은 거의 다 환경 변수에서 온다. JDK 경로도 같은 방식이 편하다:
  회사 yml에 `build.java_home: "${JAVA21_HOME:-}"` (로컬 e2e 설정 `config/artifacts.local-e2e.yml`이 이렇게 한다).
  sbmig 코드는 `JAVA21_HOME`을 직접 읽지 않는다. yml이 참조하거나 `--java-home`으로 줄 때만 쓰인다.
- `repo_root`: 상대 경로 키(`kb.dir`, `plan.catalog_file`, `advisories.file`, `kb.redact_file` …)는 `Config.resolve_path`가
  레포 루트(기본은 `sbmig/` 옆 폴더) 기준으로 푼다. `--repo-root`로 바꿀 수 있다.

### `.env`를 찾는 곳

`load_dotenv_files`는 다음 후보를 읽는다: `$SBMIG_ENV_FILE`, 패키지 폴더에서 위로 git 루트(`.git`이 있는 폴더)까지의 모든 `.env`,
현재 디렉토리에서 위로 git 루트까지의 모든 `.env`. 먼 파일부터 읽어 가까운 파일이 이기고, 현재 디렉토리 쪽이 패키지 쪽을 이긴다.
셸에서 이미 설정된 변수는 절대 덮어쓰지 않는다. 파일이 수정되면 다음 실행(웹 서버의 다음 실행, 플릿의 다음 프로젝트)에서 다시 읽는다.
인코딩은 UTF-8(BOM 포함)·UTF-16(PowerShell 5.1 `>`)·cp949를 모두 받아들이고, `export KEY=…`, 따옴표, `# 주석`을 허용한다
(`KEY=   # 주석`은 빈 값). sbmig를 다른 레포의 하위 폴더(`<root>/src/migration-agent/`)에 두어도 루트의 `.env`를 찾는 이유가 이것이다.
`.env`의 키는 Config에 들어가지 않고 환경 변수로만 쓰인다. 템플릿은 `.env.example`이다(LLM 게이트웨이 변수, `LLM_RECORD_DIR`,
`JAVA21_HOME`, 웹 변수 `SBMIG_WEB_OUT` / `SBMIG_MAX_UPLOAD_MB`).

## 2. Config 모델의 섹션 (지금 코드의 이름 그대로)

최상위 필드는 `Config` 클래스(`sbmig/core/config.py`)에 있다: `target`, `artifacts`(필수, 기본값 없음), `layout`, `intake`,
`openrewrite`, `build`, `loop`, `llm`, `fix`, `script`, `review`, `errors`, `libraries`, `kb`, `plan`, `fleet`, `report`,
`advisories`, `externalized`, `framework`, `remediation`, 그리고 `repo_root`. `web` 섹션은 없다. 웹 UI는 `sbmig serve` 옵션과 환경 변수
(`SBMIG_WEB_TOKEN`, `SBMIG_MAX_UPLOAD_MB`, `SBMIG_WEB_OUT`)로 설정한다([09-deploy-and-ops.md](09-deploy-and-ops.md)).

| 섹션 | 무엇을 다스리나 | 운영자가 주로 바꾸는 키 |
|---|---|---|
| `target` | 목표 버전(정보용 문자열). Java 21 / Boot 4.1.0 / Framework 7.0.x | 보통 그대로: `target.java_version`, `target.spring_boot_version` |
| `artifacts` | 회사 부모 pom과 사내 아티팩트. 루트 pom의 부모, boot 모듈·그 외 모듈의 부모, 필수 2개, 선택 8개 | `artifacts.parent`, `artifacts.boot_parent`, `artifacts.comp_parent`, `artifacts.mandatory`, `artifacts.optional`, `artifacts.parent_manages_spring_boot` |
| `layout` | 재구성 모양: 모듈 이름(`{name}-boot` / `-back` / `-front`), Boot as-is를 제자리로 둘지, 모듈 하나인 Boot 앱을 나눌지, JSP 패키징, 다중 모듈 정책, 버전 정리 규칙, 레거시 Spring 컨텍스트 스위치(`layout.legacy_context.*`) | `layout.boot_module`, `layout.comp_module`, `layout.front_module`, `layout.boot_as_is`, `layout.boot_single_module`, `layout.multi_module_policy`, `layout.wire_bundled_jars` |
| `intake` | 접수 규칙: 올린 ZIP의 풀기 한도, Ant 거절과 안내 문구(스위치를 켜면 pom 합성), 동봉 jar 스캔, jar 이름 표, ADF 정책 | `intake.max_unpacked_mb`(4096), `intake.max_entry_mb`(1024), `intake.max_compression_ratio`(200; 셋 다 0 = 없음), `intake.ant_to_maven`(기본 false = 거절), `intake.ant_message`, `intake.synth_group_id`, `intake.unknown_jar`, `intake.bundled_jars`, `intake.adf_policy` |
| `framework` | 사내 프레임워크 프로젝트 판정과 줄인 실행(범위 `jdk-jakarta`). 비우면 검사하지 않는다([13](13-framework-projects.md)) | `framework.artifacts`(회사 좌표, 회사 yml에만), `framework.name`(화면·보고서의 이름), `framework.fix_loop`(기본 false = 빌드 1회, true = 수정 루프), `framework.scope`, `framework.transitive`, `framework.recipe_sets` |
| `openrewrite` | OpenRewrite 1회 실행: 플러그인·레시피 BOM 버전, 레시피 세트, JVM 옵션, 제외 파일 | `openrewrite.recipes.jakarta`, `openrewrite.recipes.extra`, `openrewrite.jvm_args`, `openrewrite.timeout_minutes`, `openrewrite.recipe_bom_version` |
| `build` | Maven 호출: `mvn` 경로, settings.xml, JDK, 테스트 건너뛰기, 타임아웃, 저장소 조회 주소, 파일 잠금 재시도, 실행 전후 훅 | `build.mvn`, `build.settings_file`, `build.ignore_project_settings`(기본 true: `-s`와 함께 설치본 전역 settings를 `-gs`로 넘겨 프로젝트 `.mvn/maven.config`의 settings를 무시), `build.ignore_project_mvn`(기본 true: `MAVEN_BASEDIR`에 빈 폴더를 줘 Maven이 프로젝트 `.mvn/`의 jvm.config·maven.config·extensions.xml을 읽지 않고, maven.config의 `-D`·`-P`와 `-Dmaven.multiModuleProjectDirectory=<루트>`를 명령줄로 넘김. 폴더는 그대로 결과물에), `build.java_home`, `build.repository_url`, `build.local_repo`, `build.non_filtered_extensions`, `build.pomless_stub` / `build.pomless_stub_max`(아래) |
| `loop` | 수정 루프: 단계별 라운드 수(`loop.phases.verify`), LLM 총 호출 상한, 시간 상한, 차단 정책, 진동 감지 | `loop.phases.verify.max_iterations`, `loop.max_llm_calls`, `loop.max_total_minutes`, `loop.blocked_policy`, `loop.oscillation_limit` |
| `llm` | LLM 게이트웨이: provider, URL, 모델, 게이트웨이 모양(legacy / agent), 토큰 교환, 케이스 스레드, 기록 폴더, 가격표(`llm.pricing`: 요청한 이름(role) 먼저, 다음 실제 모델 이름, [05](05-llm-gateway-and-prompts.md) 9.1) | `llm.provider`, `llm.gateway`, `llm.model`, `llm.escalation_model`, `llm.record_dir`, `llm.case_thread` |
| `fix` | 어댑터 2단: 수정기 전체를 사내 코딩 에이전트로 바꾸는 플러그인 | `fix.plugin`, `fix.plugin_options` |
| `script` | 수정 스크립트 실행과 사다리 상한: 타임아웃, 스크립트용 파이썬, 재사용, 보강·LLM·거절·직접 수정 횟수 | `script.python`, `script.timeout_seconds`, `script.max_augments_per_case`, `script.max_llm_tries_per_case`, `script.max_declines_per_case`, `script.max_edit_tries_per_case` |
| `review` | 재구성 뒤 LLM 구조 검토: 켜기, critical 자동 수정, 프롬프트에 넣을 pom 크기, 불변조건 재검사 | `review.enabled`, `review.fix_critical`, `review.max_pom_chars`, `review.invariant_check` |
| `errors` | 오류 목록 기록 형식: 저장할 diff·원문 길이, 사람이 읽는 md / json 파일 | `errors.diff_chars`, `errors.md_view`, `errors.json_files` |
| `libraries` | 의존성 버전 정책과 jakarta 빌드 찾기: 저장소 조회, 회사 표 파일, Nexus 검색, jar 스캔 한도 | `libraries.lookup`, `libraries.company_file`, `libraries.discover_jakarta_builds`, `libraries.nexus_search` |
| `kb` | 파일 KB 위치·비밀 마스킹 규칙·잠금 대기 | `kb.dir`, `kb.redact_file`, `kb.lock_timeout_seconds` |
| `plan` | 계획: 레시피 카탈로그 파일, LLM 계획 조언자, 변환 직후 컴파일 여부 | `plan.catalog_file`, `plan.advisor`, `plan.compile_after_rewrite` |
| `fleet` | 일괄 실행: 순서, 재시도, 사전 점검, 인프라 차단기, 정리, 요약 형식. `fleet.min_free_gb`는 웹 업로드의 여유 공간 하한이기도 하다(모자라면 HTTP 507, 0 = 안 봄) | `fleet.order`, `fleet.preflight`, `fleet.cleanup`, `fleet.min_free_gb`, `fleet.max_llm_calls_total` |
| `report` | 보고서: 형식, 결과 zip, diff 포함, 언어(report.md만: `ko` = 요청자용 한국어 안내서 `sbmig/report/guide_ko.py`, `en` = 진단용 배치 `sbmig/report/markdown.py`) | `report.language`, `report.formats`, `report.zip_result` |
| `advisories` | EOL 라이브러리 권고(안내만, 차단 없음): 기본 파일, 회사 추가 파일, 항목 끄기 | `advisories.extra_files`, `advisories.policy_overrides`, `advisories.enabled` |
| `remediation` | 취약점 조치(IVAS): 이관 뒤에 이어 돌지, 실패한 빌드 뒤에도 이어 돌지, 정책, 사내 그룹([11](11-remediations.md)) | `remediation.after_migration`, `remediation.after_failed_build`(기본 `framework` = 사내 프레임워크 실행은 빌드가 실패해도 조치, `never`), `remediation.fix_loop`(실패한 후보의 빌드 오류 수정, 기본 true)·`fix_loop_max_iterations`·`fix_loop_max_minutes`, `remediation.inhouse_groups`, `remediation.file_components`(`report` / `count`) |
| `externalized` | 코드가 읽지만 어떤 리소스도 정의하지 않는 설정 키 → 자리표시자 블록과 인수인계 목록 | `externalized.enabled`, `externalized.sentinel`, `externalized.rules_file` |

몇 가지 주의할 기본값(지금 코드 기준):

- `build.skip_test_compile: true`: 모든 빌드가 `-Dmaven.test.skip=true`로 돈다(테스트는 범위 밖).
- `loop.phases.verify`의 기본 `max_iterations`는 `config/default.yml`에서 50이다. 다른 단계(`jdk21`, `spring`, `restructure`)는 꺼져 있다.
- `loop.max_llm_calls: 0`은 "상한 없음", `-1`은 "LLM 호출 전혀 없음" 이다.
- `loop.blocked_policy`는 차단 이유별 LLM 시도 수다(`reactor_duplicate`, `file_lock`, `needs_owner`, `parent_not_found`, `repository_unreachable`은 0 = LLM에 묻지 않음).
- `plan.compile_after_rewrite: false`: 재구성 전에는 빌드하지 않는다.
- `llm.case_thread: false`: 케이스 스레드는 기본 꺼짐.
- `openrewrite.recipe_bom_version`은 3.37.0으로 고정(아래 4절).

`load_config`는 지워진 옛 키(`fix.mode` 등)가 회사 yml에 남아 있으면 경고한다(조용히 무시하면 설정의 뜻이 바뀐 줄 모른다).

### pom 없는 라이브러리 — `build.pomless_stub`(기본 true), `build.pomless_stub_max`(실행당 60)

두 경우다(사내 라이브러리). (1) jar만 있고 pom이 없다: Maven은 pom을 모든 저장소에 묻고, 모두 "없음"이면 경고(`The POM for … is
missing`)만 하지만, 한 곳이라도 전송 오류(닫힌 망 Central 프록시의 502, 연결 거부)면 `Failed to read artifact descriptor for
G:A:jar:V`(원인 `Could not transfer artifact G:A:pom:V from/to …`)로 멈춘다. Maven 3.9는 전송 오류를 캐시하지 않는다.
최소 pom은 `packaging` jar(pom이 없을 때 Maven의 가정). (2) classifier 붙은 jar만 있고 pom도 기본 jar도 없다: Maven은 경고만 하지만
OpenRewrite가 `Unable to download POM: G:A:V. Tried repositories:`로 멈춘다. 최소 pom은 `packaging` pom(기본 jar 없이 OpenRewrite가
받는 것). 로컬 저장소에 `<그룹 경로>/<a>/<v>/<a>-<v>.pom`이 있으면 pom을 묻지 않는다(저장소 순서, 남은 `.pom.lastUpdated` 무관).

- 찾기(`sbmig/build/pomless.py`): (1) descriptor 줄이 정확히 `G:A:jar:V`이고 같은 `G:A:pom:V`의 전송 문구가 그 줄이나 다음 세 줄에 있을 때
  (Maven 3.8 한 줄 형식, "during a previous attempt" 형식도). 부모 pom·가져온 BOM(`Non-resolvable parent / import POM`), 플러그인·빌드 확장,
  단순 `Could not find artifact`는 아니다. (2) 그 OpenRewrite 문장의 좌표. 그중 하나가 최소 pom을 얻은 라운드에만 같은 로그의
  경고 `The POM for G:A:jar:C:V is missing`의 좌표도 본다(OpenRewrite는 첫 실패 모듈 것만 적는다).
- 쓰기 전 증명(`RepositoryLookup.file_answers`: 저장소 집합·미러·자격증명 + 프로젝트 pom의 `<repositories>`): pom은 GET, jar는 HEAD(거부되면
  1바이트 범위 GET, 내려받지 않음). 200/206 있음, 404/410 없음, 401/403 거부(둘 다 못 받는 것으로 침), 그 밖 실패. (1)은 jar가
  한 저장소 이상(또는 로컬)에 있고 pom은 어디에도 없을 때, (2)는 로컬 폴더에 `<a>-<v>-<classifier>.jar`(링크 아닌 비지 않은 파일,
  sources·javadoc 제외)가 있고 pom도 `<a>-<v>.jar`도 어디에도 없을 때만 쓴다. 아니면 저널 `pomless_check {ga, version, reason}`:
  `pom-available`(진짜 전송 문제), `jar-unavailable`((1) 저장소 장애나 라이브러리 없음, (2) 로컬 폴더에 그 jar 없음), `no-repository`(저장소나
  로컬 경로 없음), `bad-coordinate`, `cap`, `off`, (2)만 `snapshot`(SNAPSHOT 버전), `pom-present`(pom이 있는데도 실패),
  `main-jar-available`.
- 로컬 저장소(`pomless.maven_local_repo`): `build.local_repo` → `build.extra_args`의 `-Dmaven.repo.local` → Maven이 쓰는 settings의
  `<localRepository>` → 전역 settings → `~/.m2/repository`. 클러스터는 실패를 만난 파드(자리)의 저장소에 쓴다.
- 파일: 첫 줄 주석 `Written by sbmig: the repository has a jar but no pom for …`(`pomless.STUB_MARKER`). 임시 파일 + 하드 링크라 있는 파일을
  바꾸지 않는다(다른 파드가 쓴 것도 된 것). 좌표·`modelVersion`·`packaging`뿐, 의존성 없음.
- 그 뒤 같은 명령을 한 번 더(로그 `-pomless<k>`). 라이브러리는 실행당 한 번, 명령은 상한까지 여러 번. `MavenRunner.run` 재시도 순서: HTTP
  차단(없음) → Windows 파일 잠금 → 최소 pom → `-U`(최소 pom을 쓴 뒤엔 건너뜀).
- 자리: `MavenRunner.run`, `sbmig/rewrite/runner.py`, `sbmig/build/`의 `effective.py`·`deptree.py`·`classpath.py`, `sbmig/plan/jarcheck.py`,
  `sbmig/remediations/maven.py`(`package`, `resolve`). `sbmig/rewrite/versions.py`(레시피 BOM) 제외. 프로젝트 파일 변경·LLM
  없음. 프레임워크 프로젝트에도, 차단 분류(`repository_unreachable`) 전에 돈다.
- 남는 것: 저널 `pomless_stub {ga, version, path, jar_from, created}`(`created` false = 이미 있던 파일, (2)는 `form: "classified"`, `jar` 더),
  플래그 `pomless-library`, 최소 pom마다 INFO 한 줄, 라이브러리를 적은 인수인계 한 줄(`pomless.handover_line`).
- 나중 실행: 마무리에서 아는 의존성(필터 없는 의존성 트리, 없으면 effective-pom 캡처의 직접 의존성과 선언, classifier jar 포함, 300개까지)의
  로컬 pom이 표식으로 시작하면 같은 플래그와 줄, 저널 `pomless_stub_used {ga, version, path}`. 트리가 없으면 간접 의존성은 놓칠 수 있다.
  취약점 해결만 하는 실행은 저널과 로그에만 남긴다(10).
- 파일을 찾고 지우는 법: `deploy/README.md` §3, 진단·한국어 문구: `docs/RUNBOOK.md`.

### 변환 전 pom 인코딩 맞추기 — `layout.legacy_context.correct_pom_encoding`(기본 true)

잰 사실(rewrite-maven-plugin 6.46.1, Maven 3.9.10): UTF-8 선언이나 선언 없음 위의 MS949 바이트는 OpenRewrite가 파싱하지 못해 레시피 전체가
0개 파일로 실패한다. `EUC-KR` 선언 + EUC-KR 바이트는 파싱되고 바뀐 줄만 다르게 쓰인다. `MS949`·`x-windows-949`·`CP949` 선언은 파싱되지만
EUC-KR로 못 쓰는 음절이 있으면 한글 위에 대체 문자를 쓴다. Maven은 어느 경우든 빌드한다. 그래서 두 실행 방식 모두, 매 `transform.rewrite`
처음(소스 인코딩 단계 앞)에 `src/` 밖의 프로젝트 pom을 맞춘다(`sbmig/restructure/pom_charset.py`, `Orchestrator._correct_pom_encodings`):

- 바이트가 엄격한 EUC-KR이면 선언만 `EUC-KR`로(선언이 없으면 파일의 줄바꿈으로 더함), 아니면(MS949에만 있는 음절) UTF-8 선언과 함께 UTF-8로
  다시 저장. 쓰기 전에 결과가 엄격히 파싱되고 선언을 뺀 글(한글 포함)이 같은지 본다.
- 건드리지 않음: BOM이 있는 파일, 엄격히 파싱되고 한국어 계열 선언에 EUC-KR 바이트만 있는 pom, 올바른 XML이 아닌 것, latin-1이나 shift_jis로만
  설명되는 바이트, 이미 맞는 pom.
- 남는 것: 커밋 `transform: pom encodings corrected`, 저널 `pom_encodings {step, entries: [{pom, declared, actual, how}]}`(`how` =
  `declaration` | `utf-8`), pom마다 `seen` `transform` 오류 목록 항목, 플래그 `pom-encoding-corrected`, 인수인계 한 줄(「자동 변환 전에 pom 파일의
  글자 인코딩을 바로잡았습니다. …」). 뒤의 pom 쓰기(`PomEditor.save`, scope 되돌리기, 수정 루프의 pom 도우미, `Workspace.write`)는 그 인코딩을
  지킨다. 픽스처 `fixtures/projects/fw-pom-ms949`.

## 3. 회사 설정 파일 패턴

| 파일 | 저장소에 있나 | 역할 |
|---|---|---|
| `config/default.yml` | 있음 | 모든 기본값. 회사 값을 여기 쓰지 않는다 |
| `config/artifacts.example.yml` | 있음 | 회사 yml의 견본. 좌표는 전부 자리표시자(`com.company.platform:base-parent:4.0.0`, `company-core`, `lib-auth` …) |
| `config/artifacts.yml` | 없음(git-ignore) | 회사 PC에서 `cp config/artifacts.example.yml config/artifacts.yml`로 만들고 실제 좌표를 채운다. 컨테이너에서는 같은 파일을 이미지에 굽는다(`docker build --build-arg REQUIRE_CONFIG=artifacts.yml` → `/app/config/artifacts.yml`; 파일이 없으면 빌드가 멈춘다). PC 경로가 들어 있는 키(`kb.dir`, `build.settings_file`, `build.java_home`, `build.mvn`, `build.local_repo`, `script.python`, `llm.record_dir`)는 매니페스트의 `SBMIG_<섹션>__<키>` 환경 변수가 컨테이너 값으로 덮어쓴다. 비밀은 적지 않는다. 회사는 이 한 파일만 고치며 쓴다 |
| `config/artifacts.local-e2e.yml` | 있음 | 로컬 e2e용 공개 대역: 부모 = `spring-boot-starter-parent` 4.1.0, 필수·선택 = 공개 라이브러리, 예전 레이아웃(`module/boot-jar` + `module/comp-jar`), `llm.provider: fake`, `kb.dir: out/e2e/kb` |
| `config/llm-anthropic.yml` | 있음 | Anthropic API로 검증할 때 겹쳐 쓰는 yml(`llm.provider: anthropic`, 키는 여전히 `LLM_API_KEY` 환경 변수) |
| `config/llm-plugin.example.yml` | 있음 | 사내 LLM 어댑터(`llm.plugin: "pkg.module:attr"`)와 수정기 플러그인(`fix.plugin`)을 꽂는 견본 |
| `config/settings.nexus.example.xml` | 있음 | Nexus 미러 settings.xml 견본(`<mirrorOf>external:*</mirrorOf>`). 복사해 `config/settings.nexus.xml` 등으로 두고 `build.settings_file`로 가리킨다 |

반드시 바꿔야 하는 자리표시자: `artifacts.parent` / `artifacts.boot_parent` / `artifacts.comp_parent`의 g:a:v, `artifacts.mandatory`
2개, `artifacts.optional` 8개(별칭 `aliases`, 트리거 `when_present`가 있으면 함께), `intake.synth_group_id`(Ant 합성 pom의 groupId, `intake.ant_to_maven`을 켤 때만),
settings.xml의 Nexus 그룹 URL과 `build.repository_url`(같은 Nexus: 하나는 Maven 용, 하나는 sbmig가 직접 메타데이터를 읽는 용).
사내 프레임워크 프로젝트를 가려낼 때만 `framework.artifacts`(예 `["com.example.fw:example-fw-core"]`)를 채운다. 비워 두면 검사하지 않는다.
아티팩트 이름에서 만든 프레임워크 이름(`example-fw-core` → `Example`)이 틀릴 때만 `framework.name`을 함께 적는다.
실행은 `sbmig run <프로젝트> --config config/artifacts.yml`이고, 여러 yml을 겹칠 때는 `--config`를 여러 번 준다(뒤가 이김).

## 4. `knowledge/` — 배포되는 지식 데이터

`knowledge/`는 코드와 함께 배포되는 YAML / Markdown이다. 회사 규칙과 라이브러리 사실은 Python이 아니라 여기에 둔다(불변조건 2).

### 레시피 카탈로그 `knowledge/recipes/catalog.yml`

- sbmig가 아는 모든 OpenRewrite 레시피 id의 단일 출처. 읽는 코드는 `sbmig/plan/catalog.py`(`RecipeCatalog`).
- 각 항목: `id`, `origin`(openrewrite만), `artifact`(레시피 jar), `phase`, `description`, `docs`, `covers`(묶음 레시피가 이미 포함하는 것),
  `gate`(프로필 조건), `advisor`(LLM 조언자가 더할 수 있나), `sets`(이 id를 참조하는 `openrewrite.recipes` 세트 이름), `verified`, `notes`.
- 카탈로그는 무엇을 돌릴지 정하지 않는다. 실제 실행 세트는 `openrewrite.recipes`의 `jdk21`, `jakarta`, `spring_boot_4`,
  `spring_framework_7`, `spring_security_7`, `junit5`(빈 목록), `spring_batch_6`, `hibernate_7`, `servlet_jakarta_10`, `hystrix_resilience4j`, `extra`이고,
  계획기가 케이스 종류와 감지된 라이브러리로 세트를 고른다(`sbmig recipes sets`가 보여 준다).
- 회사 yml이 빈 세트를 채우거나 `openrewrite.recipes.extra`에 id를 더하면, 그 id를 카탈로그 항목의 `sets:`에도 적어야 한다.
  어긋나면 `sbmig recipes check`가 exit 1이다(`--jars`를 주면 Maven으로 레시피 jar를 받아 id가 정말 있는지도 본다; 네트워크 필요).
- 레시피 BOM은 `openrewrite.recipe_bom_version` = 3.37.0으로 고정이다(3.38.0은 Maven Central에서 불완전). 플러그인은
  `openrewrite.plugin_version`.
- 지키는 테스트: `tests/plan/test_catalog.py`(옛 조언자 사전의 스냅샷 `OLD_ADVISOR_CATALOG`, 카탈로그와 모든 설정 세트의 일치,
  배포되는 세 설정 모두에서 `check`가 빈 결과). `scripts/e2e.sh`도 시작할 때 `recipes check --jars`와 `advisories check`를 돌린다.

### 라이브러리 표 `knowledge/libraries/`

| 파일 | 내용 | 읽는 코드 |
|---|---|---|
| `knowledge/libraries/advisories.yml` | EOL / 관리 끊긴 라이브러리 권고(springfox, hystrix …): 상태, 대체품, 노트, 한국어 문구(`title_ko`, `summary_ko`, `followup_ko`, `replacement_ko`; 비면 영어). 안내와 인수인계만, 차단 없음 | `sbmig/analyze/libraries.py`, `sbmig/advisory/` (`advisories.file`, 회사 추가는 `advisories.extra_files`) |
| `knowledge/libraries/jakarta_libraries.yml` | javax 시대 서드파티 라이브러리의 jakarta 릴리스(어느 버전부터, 권장 버전, 필요한 Java) | `sbmig/core/jakarta_libraries.py` |
| `knowledge/libraries/libraries.company.example.yml` | 위와 같은 스키마의 회사 표 견본 → `<kb>/libraries.yml`로 둔다 | 같음(`libraries.company_file`, 회사 항목이 이김) |
| `knowledge/libraries/jar_names.yml` | jar 파일 이름 줄기 → Maven 좌표 표(Ant 접수, 동봉 jar) | `sbmig/analyze/jar_coords.py` (`intake.jar_names_file`) |

### 그 밖의 규칙 표 `knowledge/config/`, `knowledge/kb/`

| 파일 | 내용 | 읽는 코드 |
|---|---|---|
| `knowledge/config/build_env_errors.yml` | 환경 오류 패턴(Windows 파일 잠금 등); LLM에 묻지 않고 재빌드 | `sbmig/build/env_errors.py` |
| `knowledge/config/externalized.yml` | JNDI / context-param / 시스템 속성 → Boot 키 규칙 | `sbmig/analyze/properties.py` (`externalized.rules_file`) |
| `knowledge/config/web_xml_rules.yml` | web.xml의 서블릿·필터·리스너를 Boot 등록으로 바꿀지 건너뛸지 | `sbmig/restructure/webxml_registrations.py` |
| `knowledge/config/legacy_context_rules.yml` | 레거시 Spring 컨텍스트(ContextLoaderListener 등) 해석 규칙 | `sbmig/restructure/legacy_context.py` |
| `knowledge/config/spring_cloud_bom.yml` | Boot 버전 ↔ Spring Cloud 트레인 짝 | `sbmig/build/cloud_bom.py` |
| `knowledge/kb/redact.yml` | KB에 적기 전에 가리는 비밀 패턴(회사 패턴을 여기 더함) | `sbmig/kb/redact.py` (`kb.redact_file`) |

### 마이그레이션 노트 `knowledge/notes/`

- `*.md` 노트(jdk21, jakarta, boot3-to-4, spring-security-7, spring-mobile, spring-removed-artifacts …)와 색인 `knowledge/notes/index.yml`.
- 노트가 프롬프트에 들어가는 길: 수정 루프의 `ContextPacker`(`sbmig/fix/context.py`)가 색인의 `keywords`(정규식)를 오류 문장·심볼·위치·원문·
  아티팩트에 맞춰 본다. 오류 본문에 맞으면 키워드당 3점, import / pom 에만 맞으면 1점. 오류 본문에 맞은 노트가 하나라도 있으면 그것들만,
  최대 4개(`DEFAULT_MAX_NOTES`)가 프롬프트에 실린다. `cases`는 케이스 종류 제한, `phases`는 참고용, `always`는 모두 false 다.
- 색인과 각 노트의 앞머리(front-matter: `title`, `keywords`, `cases`, `phases`, `always`)는 같아야 하고, 노트 하나는 160줄 이하여야 한다.
  `tests/fix/test_fix_notes.py`가 둘 다 검사한다. 노트를 더하면 색인에도 항목을 더한다.

## 5. 재배포 없이 회사가 더할 수 있는 것 (`kb.dir` 아래)

`kb.dir`(기본 `kb-v2`, 컨테이너에서는 `/data/kb-v2` 등)은 실행이 쓰는 KB 이면서, 사람이 표를 놓는 곳이기도 하다. 다음 파일은 코드 배포 없이
그 자리에 두면 다음 실행부터 읽힌다.

| 파일 | 키 | 형식 |
|---|---|---|
| `kb-v2/jar_names.yml` | `intake.company_jar_names_file` | `knowledge/libraries/jar_names.yml`과 같음, 배포 표 위에 합쳐짐 |
| `kb-v2/libraries.yml` | `libraries.company_file` | `knowledge/libraries/libraries.company.example.yml`과 같음, 회사 항목이 이김 |

권고는 `advisories.extra_files`에 회사 파일 경로를 적으면(같은 스키마, id로 합쳐짐) 더해지고, `advisories.policy_overrides`로 항목을 끈다
(`{hystrix: off}`). 비밀 마스킹 패턴은 `kb.redact_file`이 가리키는 파일에 더한다.

## 6. 저장소와 yml에 절대 넣지 않는 것

- 키·토큰: LLM 키는 환경 변수(`LLM_API_KEY`, `LLM_APP_TOKEN`)로만. yml에는 변수 이름만(`llm.api_key_env`, `llm.app_token_env`).
  `.env`와 `deploy/k8s/30-secret.yaml`은 git-ignore이고, 기록 파일(`llm.record_dir`)에도 키는 남지 않는다.
- 실제 사내 좌표: 저장소의 yml·노트·테스트·프롬프트에는 자리표시자만(`com.company.*`, `com.example.*`). 실제 값은 회사 PC의
  `config/artifacts.yml` 에만 있다. 공개 대역은 `config/artifacts.local-e2e.yml`.
- 버전 숫자를 코드·프롬프트에 박기: 버전은 부모 체인·Boot BOM·as-is 빌드·저장소에서 사실로 읽는다. 표에 적는 것은 사람이 확인한 값뿐이다.
- 회사 KB 데이터(`kb-v2/`)는 git-ignore이다. 테스트·데모 실행은 `out/` 아래 KB만 쓴다(`out/e2e/kb-<fixture>`).

문서가 코드에서 떨어지지 않게 지키는 테스트는 이 팩용 `tests/docs/test_agent_doc.py`, 레시피 쪽은 `tests/plan/test_catalog.py`이다.

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
