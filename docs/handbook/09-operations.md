# 09. 운영과 진단 (회사 환경)

이 문서는 회사(Windows, 폐쇄망, 사내 LLM 게이트웨이, 사내 Nexus) 환경에서 `sbmig`를 설치·실행·진단하는 운영 안내서입니다. `docs/RUNBOOK.md`, `docs/ERROR-HANDLING.md`, `docs/FLEET.md`와 2026-10-21 첫 회사 실행에서 실제로 겪은 문제(`docs/PLAN.md` round 9b / 9c의 "same-day follow-ups")를 한국어로 정리했습니다. 사전 준비 → 실행 체크리스트 → 보고서 읽는 법 → "멈춘 건가?" 진단 → 로그 파일 지도 → 오류 카탈로그(ID 유지) → 플릿 운영 → KB 위생 → FAQ → 지원 요청 시 보낼 것 순서입니다. 회사 PC에서 로그를 손으로 옮겨 적어야 한다는 전제로, 명령은 PowerShell 기준으로 쓰고 요청하는 로그는 최소한으로 줄였습니다.

## 이 문서를 읽고 나면
- 마이그레이션 PC를 준비하고(JDK 21, Maven + Nexus 미러, Python, git, `.env`) 첫 실행을 안전하게 할 수 있습니다.
- `report.md`를 위에서부터 읽고 종료 코드·플래그의 뜻을 판단할 수 있습니다.
- "멈춘 건지 일하는 중인지"를 heartbeat·watchdog·스레드 덤프로 구분하고, 개발자에게 보낼 세 줄을 뽑을 수 있습니다.
- 자주 보는 오류(X/E/L/S 카탈로그)마다 sbmig가 무엇을 하고 운영자가 무엇을 해야 하는지 압니다.
- 플릿(수백 개 프로젝트 순차 실행)과 KB를 관리할 수 있습니다.

## 관련 코드
- `sbmig/loop/fixloop.py` — watchdog(`WATCHDOG_S`, `WATCHDOG_QUIET_S`, `WATCHDOG_DUMP_S`), 클러스터 결정 로그(`first error:`), 라운드 로그
- `sbmig/build/mvn.py` — Maven heartbeat(`HEARTBEAT_S`), 타임아웃 시 프로세스 트리 종료(`taskkill /T /F`), `mvn.cmd` 해석
- `sbmig/fix/company.py` — LLM heartbeat(`still in flight`), 하드 데드라인
- `sbmig/core/diag.py` — 스레드 덤프, `sbmig/web/app.py` — `/api/threads`
- `sbmig/core/workspace.py` — 비대화형 git(10분 타임아웃), UTF-8 디코딩, `changes.patch`
- `sbmig/report/markdown.py`, `sbmig/report/handover.py` — 보고서 절 제목과 순서
- `sbmig/fleet/runner.py`, `sbmig/fleet/cli.py`, `sbmig/kb/store.py`, `sbmig/kb/ledger.py`
- 문서: `docs/RUNBOOK.md`, `docs/ERROR-HANDLING.md`, `docs/FLEET.md`, `docs/ADVISORIES.md`

## 1. 사전 준비와 설치

### 1.1 마이그레이션 PC 요구 사항
| 항목 | 요구 | 이유 / 확인 방법 |
|---|---|---|
| Python | 3.10 이상(+ `uv` 또는 `pip`) | 에이전트 런타임. `python --version` |
| JDK | **21**, `build.java_home`에 경로 고정 | OpenRewrite와 모든 빌드가 21에서 돕니다. as-is 프로젝트를 옛 JDK로 빌드하는 일은 없습니다. `& "C:\Program Files\Java\jdk-21\bin\java.exe" -version` |
| Maven | 3.9.x | `where.exe mvn`, `mvn -v`. Windows의 Maven은 `mvn.cmd`이며 sbmig가 PATH / `MAVEN_HOME` / `M2_HOME`에서 찾습니다. 못 찾으면 `build.mvn`에 `mvn.cmd` 절대 경로 |
| Maven settings | catch-all 미러(`mirrorOf external:*`)가 있는 settings.xml을 `build.settings_file`에 | `config/settings.nexus.example.xml`을 복사해 Nexus 그룹 URL 입력. pom에 선언된 저장소까지 Nexus로 우회합니다(RUNBOOK §0.1). |
| Nexus가 프록시해야 하는 것 | Maven Central의 `org.openrewrite.*`(플러그인 6.46.1, 레시피 BOM **3.37.0**과 그 BOM이 가리키는 레시피 아티팩트들 — 3.38.0은 Central에서 불일치), `org.springframework.boot:*:4.1.0`, jakarta 아티팩트, 프로젝트 의존성, 사내 `xxx-parent` / `xxx-boot-jar` / `xxx-comp-jar` 4.0.0 | 레시피 jar가 실제로 받아지는지는 `sbmig recipes check --jars --config config\artifacts.yml`로 확인합니다(Maven + 네트워크 필요). |
| LLM 게이트웨이 | 저장소 루트 `.env`: `LLM_BASE_URL`(전체 URL), `LLM_API_KEY`, `LLM_MODEL` | 제공자 `company`(순수 `requests.post`). `sbmig llm-probe`로 확인 |
| git | 아무 버전 | 시도마다 스냅샷·되돌리기·diff. 작업 사본 안의 일회용 로컬 저장소이며 `result.zip`에는 `.git`이 없습니다. |
| 디스크 | 프로젝트 크기 × (작업 사본 + `result.zip` + 로그) | 실행 디렉터리는 `out/<run id>/` |

### 1.2 설치
```powershell
cd D:\migration-agent
uv venv
.venv\Scripts\Activate.ps1
uv pip install -e ".[dev]"          # uv가 없으면: py -3.12 -m venv .venv ; .venv\Scripts\pip install -e ".[dev]"
Copy-Item config\artifacts.example.yml config\artifacts.yml
Copy-Item config\settings.nexus.example.xml config\settings.nexus.xml
Copy-Item .env.example .env
notepad config\artifacts.yml        # 사내 좌표, build.java_home, build.settings_file(절대 경로)
notepad .env                        # LLM_BASE_URL / LLM_API_KEY / LLM_MODEL
```
설정 키 하나하나는 [08. 설정 레퍼런스](08-config-reference.md)의 4절 체크리스트를 따르십시오. 업데이트는 `git pull` 후 같은 명령으로 재설치합니다(편집 가능 설치라 보통 재설치가 필요 없지만 의존성이 바뀌면 필요).

### 1.3 Windows에서 알아 둘 것
- **`mvn.cmd`**: `Popen(["mvn"])`은 Windows에서 실패하므로 sbmig가 `shutil.which`로 `mvn.cmd`를 찾습니다. `where.exe mvn`이 찾으면 그대로 됩니다.
- **`.env`**: 셸 프로필이 없어도 저장소 루트 `.env`(또는 `SBMIG_ENV_FILE`이 가리키는 파일)를 `load_config`가 가장 먼저 읽습니다. 셸에 이미 있는 변수는 덮어쓰지 않습니다.
- **콘솔 코드 페이지(cp949)**: sbmig는 git·Maven 출력을 스스로 UTF-8로 디코딩하고 `changes.patch`는 git diff 원본 바이트로 씁니다(MS949 소스는 패치에서도 MS949). `UnicodeDecodeError: 'cp949' codec can't decode byte …`가 보이면 2026-10-21 이전 체크아웃입니다 → `git pull`.
- **타임아웃 종료**: Maven 타임아웃(`build.timeout_minutes`) 시 `taskkill /PID <pid> /T /F`로 프로세스 트리 전체를 죽입니다(그냥 terminate하면 `target/`과 소스 파일이 잠긴 채 남음).
- **git**: 작업 사본의 git은 비대화형입니다(프롬프트·훅·GPG 서명 끔, 명령당 10분 타임아웃). 그래도 커밋 단계에서 멈추면 백신/인덱서가 파일을 잡고 있는 것입니다.
- **PowerShell에서 `curl`**: `Invoke-WebRequest` 별칭일 수 있으니 `curl.exe`를 명시합니다.

## 2. 실행 체크리스트

### 2.1 처음 한 번
1. `sbmig llm-probe` — 설정 파일 없이 `.env`만으로 게이트웨이 호출. `HTTP 200`과 `extracted reply: '…'`가 나와야 합니다. 503이면 9.1.
2. `sbmig recipes check --config config\artifacts.yml`, `sbmig advisories check --config config\artifacts.yml` — 둘 다 문제 0건.
3. `sbmig analyze D:\asis\order-api.zip --config config\artifacts.yml` — 변경 없이 케이스 판정·경고·권고 확인.
4. `sbmig run D:\asis\order-api.zip --config config\artifacts.yml --dry-run` — 결정만 계산(플래그 `dry-run`, 종료 코드 2가 정상).
5. 실제 실행: `sbmig run D:\asis\order-api.zip --config config\artifacts.yml --out out`.

### 2.2 CLI와 웹 중 무엇을 쓰나
| | CLI `sbmig run` | 웹 `sbmig serve` |
|---|---|---|
| 설정 | `--config`, `--java-home`, `--settings` 등 플래그 | `config/` 안 yml을 체크(덮어쓰기 플래그 없음) |
| 진행 확인 | 콘솔 + `<run dir>/sbmig.log` | 화면의 그래프 노드 / 저널 / 로그 실시간(SSE) |
| 중단 | Ctrl+C(그 자리에서 중단, 보고서가 남지 않을 수 있음) | "중단" 버튼 — 현재 노드가 끝난 뒤 멈추고 보고서를 씀(verify 전이면 최종 빌드 없이 종료 코드 3) |
| 동시 실행 | 한 번에 하나만 돌리십시오(KB 공유) | 서버가 자동으로 줄 세움(`queued`) |
| 스레드 덤프 | watchdog 자동 덤프(5분) | `/api/threads` + 자동 덤프 |
| 다수 프로젝트 | `sbmig fleet run` | 하나씩 업로드 |

웹 서버 실행: `sbmig serve --port 8765`(LAN 공유 시 `--host 0.0.0.0`) → 브라우저 `http://127.0.0.1:8765`. REST 사용법은 [07. API 레퍼런스](07-api-reference.md).

### 2.3 단계 순서(무엇을 기다리는지 알기 위해)
intake → analyze → plan → jdk21(레시피 + 루프) → spring(레시피 + 루프) → restructure(부모/레이아웃 + 루프) → verify(최종 `mvn clean package`; 실패하면 마지막 수정 루프 후 재빌드) → 보고서·KB 수확. 회사 설정의 `plan.parent_phase: spring`이면 부모/레이아웃이 `spring.rewrite` 직후, spring 루프 **전**에 적용됩니다. 그래서 사내 부모를 못 받으면 spring 루프 전에 일찍 중단됩니다(정상 동작). 전체 흐름은 [01. 파이프라인](01-pipeline.md).

## 3. 보고서 읽는 법(`<run dir>/report.md`)

### 3.1 절 순서(`render_markdown`)
| 순서 | 절 제목 | 무엇을 보나 |
|---|---|---|
| 1 | `# Migration report — <프로젝트>` 표 | **Result**(`✅ BUILD SUCCESS` / `❌ NOT BUILDING`, 최종 `mvn clean package` 기준), Case, As-is(java/boot/spring/pom 수), Run id, 시작/종료(소요), LLM 호출·토큰, **Flags**, **Blocked** 개수 |
| 2 | `## Handover — what we replaced, what is yours` | 운영자·as-is 담당 팀이 **가장 먼저** 읽는 절. 아래 하위 절 |
| 2.1 | `### Versions replaced` | parent / java / spring boot 전후, 버전이 바뀐 의존성·BOM·플러그인·속성 표(이유: `parent swap`, `recipe`, `optional bump`, `mandatory`, `advisory:<id>`), **⚠ carried over** 행(새 부모가 관리하지 않아 as-is 버전으로 고정한 것 — 담당 팀 확인 필요), Added / Removed |
| 2.2 | `### Libraries needing your attention` | EOL/비관리 라이브러리(springfox, hystrix, sleuth …): 상태, 정책(`guide`/`recipe`), 한 일, 결과(`✅ replaced` / `⚠ still present`), 다음 할 일, 문서 링크. 빌드를 실패시키지 않습니다. |
| 2.3 | `**Merge conflicts**` | 멀티 모듈 merge에서 같은 경로가 여러 모듈에 있던 파일(첫 모듈 우선, 버린 사본은 `<run dir>/merge-conflicts/`) |
| 2.4 | `### Properties to set before running` | 코드가 읽지만 패키지에 없는 설정 키(JNDI, context-param, `-D`, catalina.properties): key, before migration, referenced at, written as, set via + 공급 방법 안내 + `__SET_ME__` 자리표시자 파일 |
| 2.5 | `### From here on, your team` | 번호 매긴 할 일 목록(교체 안 된 권고, 교체됐지만 확인할 것, carried-over 고정, blocked/unresolved 안내, 수동 후속 작업) |
| 3 | `## Blocked — needs human / library action` | 일부러 멈춘 클러스터: 사유, 아티팩트/소유 클래스, 필요 타입 vs 실제 타입, 파일, 제안, 시도 횟수. 나머지 마이그레이션은 계속 진행된 상태입니다. |
| 4 | `## Unresolved errors` | 예산 안에서 못 고친 오류(파일별, 마지막 javac/Maven 메시지) |
| 5 | `## Manual follow-ups` | as-is 담당자가 할 일(web.xml, JSP, XML 빈, 건너뛴 테스트, 저장소 미러 안내 등) |
| 6 | `## Flags` | 플래그와 설명 |
| 7 | `## What we changed` | 변경 요약 |
| 8 | `## Phases` | 단계별 반복·결과 |
| 9 | `## Fix attempts` | 시도별 결정·결과·커밋 |
| 10 | `## Escalated clusters` | 포기한 클러스터 |
| 11 | `## Knowledge base` | 이번 실행의 KB 케이스·후보·승격 |
| 12 | `## Notes`, `## Outputs` | 메모, 산출물 경로 |

### 3.2 종료 코드
| 코드 | 의미 | 운영자 행동 |
|---|---|---|
| 0 | 최종 `mvn clean package` 성공, blocked 없음 | Handover를 as-is 담당 팀에 전달. 런타임 검증은 담당 팀 몫(범위 밖) |
| 2 | 끝까지 실행했으나 blocked/unresolved 있음(또는 `restructure-failed`, dry-run, stop-after) | Blocked의 제안(라이브러리 버전, Nexus 프록시, `artifacts.optional` 추가 등)을 처리한 뒤 재실행 |
| 3 | 중단(Maven 없음, Maven 프로젝트 아님, 사내 부모 해석 불가, 내부 오류, 웹 중단) | Manual follow-ups 첫 줄의 사유를 해결 |

라이브러리 권고와 carried-over 고정은 종료 코드를 바꾸지 않습니다(회사 방침: 빌드를 막는 정책은 없음).

### 3.3 플래그 빠른 해석
| 플래그 | 운영자가 이해할 것 |
|---|---|
| `tests-skipped` | 메인 코드는 패키징됨. 런타임 테스트 실패만 남아 `-DskipTests`로 빌드. 실패 테스트는 follow-up에. (`build.skip_tests: true`가 기본이라 보통은 안 나옴) |
| `test-compile-skipped` | 테스트 소스가 컴파일되지 않아 `-Dmaven.test.skip=true`로 빌드. 테스트 수정 필요 |
| `restructure-failed` | 회사 레이아웃 변환 단계가 실패. 빌드가 통과해도 성공 아님(종료 코드 2) |
| `source-encoding-set:cp949` 등 | 레거시 인코딩 감지, `project.build.sourceEncoding` 설정 |
| `enforcer-relaxed` / `enforcer-skipped` | enforcer 규칙 완화 / 건너뜀 — 규칙 검토 |
| `werror-removed` | `-Werror` 제거 |
| `managed-versions-carried-over` | Handover의 ⚠ 행 확인 |
| `rewrite-unresolved-versions` | 레시피가 Maven 메타데이터를 못 받아 `4.0.x` 같은 문자 그대로 버전을 남김 → 나중에 재실행하거나 직접 버전 지정 |
| `rewrite-parse-problems` | OpenRewrite가 일부 소스를 파싱하지 못해 레시피가 그 파일을 건너뜀(phase 노트에 파일 목록). 남은 것은 소스 핸들러와 수정 루프가 처리 → 9.16 |
| `jakarta-leftovers-renamed` | 레시피가 남긴 `javax.*`를 `jakarta-namespace-sweep`이 바꿈(E13). follow-up의 문자열 / 리소스 파일 확인 |
| `jsp-javax-renamed` | JSP / 태그 파일의 page import / 스크립틀릿 / useBean에 있던 `javax.*`를 `jakarta-namespace-sweep`이 바꿈(E13). JSP는 Tomcat이 실행 시점에 컴파일하므로 빌드로 확인되지 않음 — 노트에 나온 화면을 열어 확인 |
| `spring-data-crud-restored` | 페이징 리포지토리에 `ListCrudRepository<T, ID>`를 추가함(E14) |
| `javax-api-on-classpath` | 마이그레이션 후에도 javax API jar가 클래스패스에 남음(X1b). 보고서 절 "javax API jars still on the classpath"의 via 라이브러리에 Jakarta 빌드가 필요 |
| `jakarta-library-bumped` | jakarta 릴리스가 알려진 서드파티 라이브러리(`knowledge/libraries/jakarta_libraries.yml`)를 `jakarta-library-floor`가 올림 |
| `web-xml-registered` | web.xml의 서블릿 / 필터 / 리스너 / 오류 페이지를 boot 모듈의 `WebXmlRegistrations`로 생성함(W1). 인수인계의 "web.xml (<spec>) → WebXmlRegistrations: …" 줄과 jsp-file / welcome-file / mime-mapping 후속조치 확인. 생성 파일이 컴파일되지 않으면 그 클래스(또는 라이브러리)가 아직 `javax.servlet`인 것 |
| `llm-cooldown` / `llm-unavailable` | 게이트웨이 장애 → 9.1 |
| `llm-budget-exhausted` / `time-budget-exhausted` | 예산 소진(`loop.max_llm_calls` / `loop.max_total_minutes`) |
| `aborted`, `aborted:parent-not-found`, `mvn-not-found`, `unsupported-build-tool:<도구>` | 중단 사유(종료 코드 3) |
| `stopped-after-<단계>`, `dry-run` | 의도적 |

전체 목록은 [07. API 레퍼런스](07-api-reference.md) 6.2.

## 4. "멈춘 건가?" 진단 절차

### 4.1 정상적으로 조용한 구간
| 구간 | 얼마나 | 로그에 보이는 것 |
|---|---|---|
| OpenRewrite 실행 | 1–5분 이상(회사 Boot 프로젝트에서 258초 관찰, 대략 소스 양에 비례) | 60초마다 `mvn still running: Ns elapsed (timeout Ns) — N output line(s) so far, log: <파일>` (Windows에서는 `mvn.cmd still running: …`) |
| Maven 빌드 | 프로젝트에 따라 수십 초–수 분 | 같은 60초 heartbeat. `build.timeout_minutes`(30) 초과 시 종료 |
| LLM 호출 | 수십 초–수 분 | 시작 줄, 그 뒤 30초마다 `llm call still in flight: Ns elapsed (timeout Ns) — …`. `llm.timeout_seconds` + 30초가 지나면 포기하고 재시도 |
| effective-pom / classpath 캡처 | 수 분 이내(각 10분 / 15분 제한) | 60초 heartbeat |

### 4.2 watchdog 줄
fix 루프는 30초마다 깨어나서 마지막 milestone 이후 60초 이상 조용하면 다음 경고를 냅니다(`sbmig/loop/fixloop.py`).
```text
[spring] fix loop quiet for 120s since '<milestone>' (loop running 900s) — still working or stuck? a thread dump follows after 300s (also: GET /api/threads, kill -USR1 <pid>)
```
milestone은 지금 실행 중인 코드를 가리킵니다: `deciding <cluster>`, `advisory hints`, `merge-conflict hints`, `fixer.propose … (prompt packing + LLM call)`, `LLM answered: applying`, `git commit`, `mvn package (build 3)` 등. `fix prompt for <cluster>: packing …` / `… packed: N chars → calling the LLM` 줄이 프롬프트 구성을 감쌉니다. LLM·Maven은 자체 heartbeat가 있으므로, **heartbeat 없이 watchdog만 반복되면** 그 사이의 코드(힌트, 프롬프트 패킹, 패치 적용, git)에 걸린 것입니다.

### 4.3 스레드 덤프
| 방법 | 어디에 저장 | 비고 |
|---|---|---|
| 자동(watchdog) | 5분(`WATCHDOG_DUMP_S` = 300초) 동안 milestone이 없으면 실행 디렉터리에 `threads-<YYYYMMDD-HHMMSS>.txt` | CLI·웹 모두. milestone당 한 번 |
| 웹 `GET /api/threads` | 응답 본문 + `<웹 out>/threads-<ts>.txt` | Windows에서의 표준 방법: `curl.exe http://127.0.0.1:8765/api/threads` |
| POSIX `kill -USR1 <pid>` | 현재 디렉터리 기준 `out/threads-<ts>.txt` | Linux/macOS 전용(Windows에는 SIGUSR1 없음) |

덤프에서 실행 스레드는 `--- thread sbmig-run-<run id> (…) ---`(웹) 또는 `--- thread MainThread (…) ---`(CLI) 블록입니다. 맨 아래 몇 줄이 "지금 어디서 기다리는지"입니다.

### 4.4 판단 순서
1. `sbmig.log` 끝을 봅니다. heartbeat(`still running` / `still in flight`)가 계속 나오면 **일하는 중**입니다. 기다립니다.
2. heartbeat 없이 `fix loop quiet for …` 줄만 반복되면 milestone을 확인합니다.
3. 저널의 마지막 이벤트가 `llm_fix`(답 받음)인데 `attempt`가 없으면 패치 적용/`git commit`에서 멈춘 것입니다 → 백신/인덱서 의심.
4. 몇 분간 아무 줄도 없으면 스레드 덤프를 받습니다.
5. 개발자에게 보낼 것: **(a) watchdog 줄 한 줄, (b) 그 직전 클러스터의 `first error:` 줄, (c) 덤프의 `--- thread sbmig-run-…` 블록 맨 아래 15줄 정도**.

```powershell
$run = "out\run-20261021-101500-ab12cd"            # 웹이면 out\web\web-…
Get-Content "$run\sbmig.log" -Wait -Tail 30
Select-String -Path "$run\sbmig.log" -Pattern "fix loop quiet" | Select-Object -Last 1
Select-String -Path "$run\sbmig.log" -Pattern "first error:" | Select-Object -Last 1
curl.exe -s http://127.0.0.1:8765/api/threads > threads.txt
Select-String -Path threads.txt -Pattern "--- thread sbmig-run-" -Context 0,40
Get-ChildItem $run -Filter "threads-*.txt"
tasklist | findstr /i "java mvn python"
```

## 5. 로그 파일 지도
| 파일 | 내용 | 언제 보나 |
|---|---|---|
| `<run dir>/sbmig.log` | 실행 하나의 DEBUG 로그(결정, 빌드, LLM 호출 — 키 없음). 웹 실행은 해당 실행 스레드의 기록만 | 항상 첫 번째 |
| `<run dir>/journal.jsonl` | 기계 판독용 이벤트(한 줄 한 JSON). 이벤트 어휘는 [07](07-api-reference.md) 6.3 | 단계·결정 흐름을 시간순으로 볼 때 |
| `<run dir>/logs/NNN-build-<label>.log` | Maven 빌드 로그(번호는 실행 내 순서) | 빌드 실패의 원문 |
| `<run dir>/logs/NNN-rewrite-<label>.log` | OpenRewrite 실행 로그 | 레시피 실패, 다운로드 실패(X8), OOM(E8) |
| `<run dir>/logs/NNN-classpath-….log`, `NNN-effective-pom-….log` | 의존성 classpath / effective-pom 캡처 | X1 소유 아티팩트 추적, carried-over 판단 |
| `<run dir>/threads-<ts>.txt` | watchdog 자동 스레드 덤프 | 멈춤 진단 |
| `<llm.record_dir>/llm-NNNN.json` | LLM 요청/응답 기록(메시지, 응답, 모델, 사용량, 지연, role, metadata, error) | 게이트웨이·프롬프트 문제 |
| `<fleet out>/fleet/sbmig.log` | 플릿 전체 로그(+ 프로젝트별 `sbmig.log`) | 플릿 운영 |
| `<웹 out>/sbmig-web.log` | 웹 서버 전체 로그 | 서버 문제 |
| `<웹 out>/<run id>/web-status.json` | 웹 워커의 최종 상태(종료 코드, 오류, 중단 여부) | 서버 재시작 후 상태 확인 |
| `--log-file <경로>` | 어떤 명령이든 추가 DEBUG 로그 | analyze / plan 같은 단발 명령 진단 |

```powershell
Get-Content "$run\sbmig.log" -Tail 50
Select-String -Path "$run\sbmig.log" -Pattern "WARNING|ERROR" | Select-Object -Last 20
Get-Content "$run\journal.jsonl" | ForEach-Object { $_ | ConvertFrom-Json } | Select-Object ts, event | Select-Object -Last 20
Get-ChildItem "$run\logs" | Sort-Object LastWriteTime | Select-Object -Last 5 Name, Length, LastWriteTime
Select-String -Path "$run\logs\*build*.log" -Pattern "\[ERROR\]" | Select-Object -First 10
Select-String -Path "$run\sbmig.log" -Pattern "max fix rounds|fix round"
```

## 6. 자주 보는 오류 카탈로그
원칙: 고칠 수 없는 것에서 루프를 돌지 않고, 숨기지도 않습니다. 모든 클러스터는 `fixed`(핸들러/레시피/KB/LLM, 빌드로 확인), `blocked`(의도적으로 건너뜀 — 라이브러리·인프라·사람 조치 필요), `unresolved`(예산 안에서 실패) 중 하나로 끝납니다. blocked는 실행 내내 기억되어 이후 단계에서 다시 시도하지 않고, 마이그레이션은 계속됩니다. 원문은 `docs/ERROR-HANDLING.md`.

### 6.1 외부/인프라 차단(X)
| ID | 증상 | 의미 | sbmig가 하는 일 | 운영자가 할 일 |
|---|---|---|---|---|
| X1 의존성의 javax API | jakarta 전환 후 `incompatible types: jakarta.servlet.http.HttpServletRequest cannot be converted to javax.servlet.http.HttpServletRequest`, `… cannot be applied … required: javax.* found: jakarta.*` | 의존 라이브러리가 아직 javax 타입을 API로 노출 | 소유 클래스 → jar → GAV를 classpath로 추적하고, 그 jar가 javax API jar 자체이면 `dependency:tree` 경로로 끌어오는 라이브러리(`via`)를 찾음. 그 아티팩트가 `artifacts.optional`이면 버전 bump가 해결책, 아니면 LLM 0회로 즉시 blocked | Blocked 행의 "`API jar` ← `라이브러리`"를 봅니다. 표(`knowledge/libraries/jakarta_libraries.yml`)에 있는 라이브러리는 핸들러 `jakarta-library-floor`가 올림. 사내 아티팩트는 Jakarta 빌드 요청 + `artifacts.optional` 추가, 그 밖은 Jakarta 릴리스로 올리거나 교체 |
| X1b 마이그레이션 후 클래스패스에 남은 javax API jar | 빌드 성공 여부와 관계없음: 보고서 절 "javax API jars still on the classpath", 플래그 `javax-api-on-classpath` | javax 기준으로 컴파일된 라이브러리가 javax API jar를 끌어옴(jakarta 타입을 넘기면 컴파일 실패, 아니면 런타임 실패) | 최종 빌드 뒤 `mvn dependency:tree -Dincludes=<javax API 패턴>`(`report.javax_api_inventory`, journal `deptree` / `javax_api_inventory`) | 절의 "via" 라이브러리를 Jakarta 빌드로 올리거나 교체, pom에 직접 선언된 API jar는 제거 |
| X2 의존성 API 변경(javax 아님) | `cannot find symbol … location: class <라이브러리 클래스>`, `method … cannot be applied` | 라이브러리 메서드 이름/시그니처가 바뀜 | LLM 1회(이름 바뀐 메서드는 적응 가능, 성공하면 `ChangeMethodName` 후보) 후 blocked | 소유 아티팩트·심볼 확인, 라이브러리 담당에 문의 |
| X3 아티팩트 없음 | `Could not find artifact g:a:v in <repo>`, `The following artifacts could not be resolved` | Nexus에 없거나 좌표가 틀림 | 알려진 relocation 핸들러 먼저, 모르면 LLM 0회로 blocked(LLM은 버전을 지어내지 않음) | Nexus에 프록시하거나 좌표 수정 |
| X4 부모 해석 불가 | `Non-resolvable parent POM` | 사내 부모(`xxx-parent` 등)를 받을 수 없음 | 설정된 사내 부모면 일찍 중단(`aborted:parent-not-found`, 종료 코드 3) | settings.xml / Nexus / 좌표·버전 확인 |
| X5 JDK 21 비호환 플러그인 | `Unsupported class file major version 65`, `NoSuchMethodError … javac` | 오래된 Maven 플러그인 | 알려진 최소 버전이면 핸들러, 아니면 LLM 1회 후 blocked | 플러그인 버전 갱신 |
| X6 미지원 소스 | Kotlin/Groovy/Scala 오류, protobuf/jaxb2/openapi 생성 플러그인 실패 | 이 도구가 다루지 않는 소스 | 분석에서 경고, 오류는 blocked(`unsupported_source`) | 해당 모듈 수동 이관 |
| X7 권고 레시피가 지운 라이브러리 | `package com.netflix.hystrix does not exist`(`HystrixCommand` 하위 클래스) | 레시피가 바꾸지 못한 코드 | 권고의 "Library migration guidance"를 프롬프트에 넣어 LLM 1회 → 대체 라이브러리로 포팅. Handover에 LLM이 다시 쓴 파일 목록 | Handover에 적힌 파일 리뷰 |
| X8 레시피 실행 중 다운로드 실패 | rewrite 로그에 `status code: 429`, `Too Many Requests`, `Could not transfer artifact`, `Failed to download`, 502/503, reset/timeout/`UnknownHostException`; 또는 성공했는데 pom에 `4.0.x` / `2025.1.x` / `latest.release`가 남음 | 프록시 rate limit / 메타데이터 다운로드 실패 | `openrewrite.download_retry_wait_s` 대기 후 최대 `openrewrite.download_retry`회 재시도(오류에 `download-retry:` 접두어). 문자 그대로 버전은 단계 전으로 되돌려 1회 재실행, 그래도 남으면 플래그 `rewrite-unresolved-versions` + follow-up. 해석 실패만 있던 단계의 KB 레시피는 평가하지 않음(`kb_credit_skipped`) | 나중에 재실행하거나 버전을 직접 지정. 프록시 rate limit 문의 |
| X9 pom에 선언된 저장소에 접근 불가 | `Could not transfer artifact … from/to old-nexus (http://…)`, connect timeout, `UnknownHostException`, `Blocked mirror for repositories` | 옛 Nexus·Central·Spring milestone URL이 pom에 박혀 있음 | 분석이 선언 URL을 기록하고 catch-all 미러가 없으면 follow-up. 선택적으로 `build.pom_repositories: remove` → 핸들러 `strip-pom-repositories`가 블록 삭제 | `build.settings_file`에 `mirrorOf external:*` 미러 설정(권장). 그래도 "cached from a remote repository ID that is unavailable"이면 `build.extra_args`에 `-llr` 또는 새 `build.local_repo` |

### 6.2 환경/레거시 특성(E, 결정적 핸들러)
| ID | 증상 | 의미 | sbmig가 하는 일 | 운영자가 할 일 |
|---|---|---|---|---|
| E1 레거시 소스 인코딩 | `unmappable character (0x..) for encoding UTF-8` | JDK 18+는 기본 UTF-8, 소스는 MS949/EUC-KR | 핸들러 `source-encoding`: 인코딩 감지 후 `project.build.sourceEncoding` 설정(또는 `convert-utf8`). 파일은 항상 원래 인코딩으로 읽고 써서 한글 주석이 깨지지 않음. 플래그 `source-encoding-set:cp949` | 없음(UTF-8 통일 정책이면 `build.source_encoding_policy`) |
| E2 UTF-8 BOM | `illegal character: '\ufeff'` | 파일 앞 BOM | 핸들러 `strip-utf8-bom` | 없음 |
| E3 CP949 파일 이름 zip | 압축 해제 후 이름 깨짐 | UTF-8 플래그 없는 zip | intake에서 cp437 → cp949 재해석 | 없음 |
| E4 enforcer 규칙 | `requireJavaVersion [1.8,9)`, `dependencyConvergence` 실패 | JDK 21과 맞지 않는 규칙 | 핸들러 `enforcer-relax`(규칙 완화) → 안 되면 `-Denforcer.skip=true`(플래그) | 규칙 검토 |
| E5 `-Werror` | JDK 21 deprecation 경고가 오류로 | 컴파일러 설정 | 핸들러 `remove-werror`(플래그 `werror-removed`) | 없음 |
| E6 부모 교체로 관리 버전 상실 | restructure 후 `'dependencies.dependency.version' … is missing` | 옛 부모가 관리하던 버전을 새 부모가 모름 | 핸들러 `carry-over-managed-version`: intake 때 as-is effective pom에서 받은 버전으로 고정(⚠ Handover), 없으면 versions.yml, 없으면 LLM 1회 | Handover ⚠ 행을 담당 팀이 확인 |
| E7 `system` scope jar | `WEB-INF/lib`, `systemPath` jar 필요 | Nexus 밖 jar | 분석 경고, restructure가 `lib/`를 옮기고 `systemPath` 재작성, follow-up "Nexus에 설치" | jar를 Nexus에 등록 |
| E8 OpenRewrite OOM / 타임아웃 | rewrite 로그에 `OutOfMemoryError` | 레시피 JVM 메모리 부족 | `openrewrite.oom_retry_jvm_args`로 1회 재시도 후 빌드 루프로 진행 | `openrewrite.jvm_args` 증가, `openrewrite.exclusions` 추가 |
| E9 rewrite:run이 compile을 fork하지 못함 | 로그 `rewrite: forked run failed in compile → retrying with runNoFork` | 프로젝트가 아직 컴파일되지 않아 `rewrite:run`(compile 선행)이 실패 | 자동으로 `runNoFork` 재시도(해석된 classpath로 파싱) | 없음(정상 흐름) |
| E10 레거시 컴파일러 설정(Java 1.6/1.7 pom) | `option --boot-class-path cannot be used together with --release`, `not allowed with target 21`, `-Xbootclasspath/p:`, `Cannot run program …/jdk1.6/bin/javac` | 사라진 JDK를 가리키는 fork/executable, bootclasspath 등 | 핸들러 `compiler-legacy-config`(선제 + 반응): 해당 요소·인자 제거, `encoding` 등은 유지 | 없음 |
| E11 부모 교체 후 javax JSTL | `'dependencies.dependency.version' for javax.servlet:jstl:jar is missing` | 옛 부모 체인이 관리하던 javax JSTL(어차피 Jakarta JSP와 불일치) | 핸들러 `jstl-to-jakarta`: Jakarta JSTL(`org.glassfish.web:jakarta.servlet.jsp.jstl` 등)로 교체, 버전은 Boot BOM에 맡김 | 없음 |
| E12 필터링 리소스 아래 바이너리 | `maven-resources-plugin:3.3.x:resources … filtering …/static/fonts/fontawesome-webfont.eot … MalformedInputException: Input length = 1`(war 플러그인 `<webResources>`도) | resources-plugin ≥ 3.2(Boot 4 부모)가 필터링 대상 파일을 텍스트로 읽음 | 핸들러 `resources-binary-filtering`(선제 + 반응): 실패한 플러그인에 `<nonFilteredFileExtensions>`(웹폰트, keystore, 압축, 문서, 미디어 + 실패 확장자) 추가, 필터링 유지, 삭제 없음 | 없음 |
| E13 rewrite 뒤 남은 javax | `incompatible types: jakarta.persistence.Query cannot be converted to javax.persistence.Query`, `package javax.persistence does not exist`, `class file for javax.X not found`(프로젝트 소스) | rewrite 플러그인이 파싱하지 못한 파일(플래그 `rewrite-parse-problems`), `openrewrite.exclusions` 아래 디렉터리, 완전한 이름(FQN)으로 쓴 `javax.*`, 타입 정보 부족 | 핸들러 `jakarta-namespace-sweep`(선제 + 반응): 프로젝트 소스의 `javax.*` Jakarta EE 참조를 `jakarta.*`로(플래그 `jakarta-leftovers-renamed`). 의존성 jar가 javax를 쓰면 X1 | follow-up의 문자열 리터럴 / 리소스 파일(`javax.persistence.*` 속성 키, persistence.xml / web.xml 스키마) 확인 |
| E14 Spring Data 페이징 리포지토리 | `no suitable method found for findAll(no arguments)`, `cannot find symbol … method save(…)` | Spring Data 3에서 `PagingAndSortingRepository`가 `CrudRepository`를 상속하지 않음. 레시피 `MigratePagingAndSortingRepository`는 타입 정보가 있을 때만 고침 | 핸들러 `spring-data-crud-repository`(선제 + 반응): `ListCrudRepository<T, ID>` 추가(플래그 `spring-data-crud-restored`) | 없음 |

### 6.3 루프 안전장치(L)
| ID | 증상 | 의미 | sbmig가 하는 일 | 운영자가 할 일 |
|---|---|---|---|---|
| L1 정체 | 같은 오류 집합이 `loop.stall_limit`번 | 수정이 효과 없음 | escalation 모델 + 컨텍스트 확대, 그래도 안 되면 blocked(`repeated_failure`) | 필요하면 `LLM_ESCALATION_MODEL` 지정 |
| L2 진동 | 오류 집합이 A↔B↔A | 두 수정이 서로를 되돌림 | 정체로 취급 → blocked(`oscillation`) | 해당 파일 수동 확인 |
| L3 회귀 | 오류 수 × `loop.regression_ratio` 증가, 또는 pom 수정 후 `pom_invalid` / `parent_resolution` / `plugin_resolution` 등장 | 수정이 상황을 악화 | 그 시도를 git으로 되돌림 | 없음 |
| L4 LLM 프로토콜 쓰레기/빈 답 | 패치가 파싱되지 않음(반복) | 모델이 형식을 안 지킴 | `loop.max_llm_attempts_per_cluster` 후 blocked(`repeated_failure`), 실행 계속 | 모델 확인(`llm-NNNN.json`) |
| L5 LLM 불가 | 재시도 후에도 HTTP 오류 / 키 없음 / 파싱 불가 200(JSON 아님, 텍스트 없음, 잘못된 `LLM_RESPONSE_PATH`)이 연속 2회. 400/413/422나 content-filter 답은 그 요청의 잘못이라 세지 않음 | 게이트웨이 장애 | 첫 연속 2회 → 실행당 한 번 `llm.cooldown_seconds`(60초) 쉬고 재시도(저널 `llm_cooldown`, 플래그 `llm-cooldown`, 그 두 시도는 클러스터 시도 수에 미포함). 두 번째 연속 실패 → LLM 전용 클러스터 blocked(`llm_unavailable`), 핸들러·레시피는 계속 | 9.1 절차로 게이트웨이 확인 후 재실행 |
| L6 예산 소진 | `loop.max_llm_calls` / `loop.max_total_minutes` | 실행 예산 끝 | 남은 LLM 전용 클러스터 blocked(`budget_exhausted`), verify와 보고서는 항상 실행 | 예산 조정 후 재실행 |
| L7 테스트 컴파일만 실패 | TEST_COMPILE 클러스터만 남음 | 메인은 됨 | `-Dmaven.test.skip=true`, 플래그 `test-compile-skipped`, 오류는 follow-up | 테스트 수정 |
| L7b 런타임 테스트 실패만 | TEST_FAILURE만 남음 | 메인은 됨 | `-DskipTests`, 플래그 `tests-skipped`, 실패 테스트는 follow-up + unresolved, 최종 verify도 테스트 생략 | 테스트용 값 제공 후 `-DskipTests` 없이 재빌드 |
| L8 기존부터 깨진 프로젝트 | as-is가 자기 JDK에서도 컴파일 안 됨 | 원래 문제 | as-is는 빌드하지 않음 — JDK 21 이후 오류로 똑같이 보고 | as-is 담당에 알림 |
| L9 계획 후 내부 오류 | 단계 안 예외(컴포넌트 없음, 버그) | sbmig 문제 | `aborted`(종료 코드 3)이지만 **최종 `mvn clean package`는 시도**(dry-run, Maven 없음, 부모 해석 불가 같은 의도적 중단은 제외) — 보고서에 실제 미해결 오류 | `sbmig.log`의 traceback을 개발자에게 |
| L10 restructure 단계 실패/충돌 | 최종 빌드는 as-is 트리로 통과할 수 있어 성공처럼 보임 | 레이아웃 변환 실패 | 플래그 `restructure-failed`, 단계·모드·오류를 담은 follow-up, 저널 `restructure_failed`, `success=False` → 종료 코드 2(`optional_bump` 실패는 follow-up만) | follow-up의 오류 확인, 개발자에게 |
| L12 최종 verify 실패(수정 시도 없음) | `build-verify.log`에만 오류가 있고 앞 루프는 통과 | 회사 설정은 restructure 루프가 없어 최종 빌드가 첫 `package` 빌드 | `loop.phases.verify` 마지막 수정 루프(3라운드) 후 `clean package` 재실행; `repeated_failure` 클러스터는 풀려서 `loop.verify_extra_llm_attempts`(2)회 더 시도(저널 `blocked_released`, `verify_loop_start` / `verify_loop_end`) | 그래도 실패하면 보고서 Unresolved / Blocked 확인; 라운드·시도 수 증가는 `loop.phases.verify.max_iterations`, `loop.verify_extra_llm_attempts` |

### 6.4 구조 엣지 케이스(S)
| ID | 경우 | 의미 | sbmig가 하는 일 | 운영자가 할 일 |
|---|---|---|---|---|
| S1 MSA + 공유 라이브러리 모듈 | `common` 같은 비서비스 모듈 | 함께 이관해야 함 | 저장소 안 모든 루트 pom을 대상으로, 모든 루트 pom 부모 교체 | 없음 |
| S2 이미 마이그레이션됨 | Java 21 / Boot 4.1 / 재구성 완료 | 멱등 | 레시피 no-op, "already restructured", 다만 루트·모듈 부모는 설정 버전으로 옮김(`parents_set`) | 없음 |
| S3 pom만 있는 aggregator | 소스 없음 | — | 분석 경고, rewrite 단계 없음, pom만 변경 | 없음 |
| S4 Gradle / Ant / 빌드 없음 | `pom.xml`이 없음 | Maven만 지원(사용자 결정) | intake에서 거부: 플래그 `unsupported-build-tool:<gradle/ant/none>`, `aborted`, 종료 코드 3, 사유는 follow-up 첫 줄. pom이 어딘가 있으면 Maven 우선 | 대상에서 제외(수동) |
| S5 비표준 모듈 배치 | `<module>../x</module>` 등 루트 밖 모듈 | 작업 사본 밖 | `wrap`: 밖 모듈이면 이동 전 중단. `merge`: 밖 모듈은 건너뛰고 follow-up("별도 이관 / Nexus 설치") | 그 모듈을 따로 이관 |
| S6 optional 아티팩트가 전이로만 존재 | 직접 선언 없음 | 정책상 bump 안 함 | 선언된 사용만 셈 | 필요하면 직접 선언 |
| S7 as-is가 이미 WAR + JAR 멀티 모듈 | — | — | wrap 정책, WAR → jar follow-up | follow-up 처리 |
| S8 Java 1.5–1.7 소스 레벨 | JDK 21은 8 미만을 컴파일 못 함 | — | 분석 경고, `UpgradeToJava21`이 release 21 설정(JAXB/JAX-WS 등 추가), source/target 제거, `sun.misc` 등은 권고 `jdk-removed-apis` + LLM 힌트 | 포팅된 호출 리뷰 |
| S9 merge: 레지스트리 충돌 | `META-INF/services/*`, `spring.factories`, `*.imports` | 줄 목록 파일 | 모든 사본 줄의 합집합으로 유지, 원본은 `merge-conflicts/`에 저장 | 없음 |
| S10 merge: pom packaging 하위 모듈 | BOM / 중첩 aggregator | 옮길 것 없음 | 하위 `<modules>`를 깊이 우선 병합, properties·dependencyManagement를 루트로 접음 | 없음 |
| S11 merge: WAR 모듈 | — | — | `src/main/webapp`을 back 모듈로, web.xml/JSP follow-up이 새 위치를 가리킴 | follow-up 처리 |
| S12 merge: 유지한 boot 모듈 + 같은 이름 리소스 | — | — | boot 모듈은 자기 `application.properties` 유지, 나머지 사본은 back으로, "boot 사본이 런타임에 back 사본을 가림" follow-up | 설정값 통합 검토 |
| S13 merge: 사용자 정의 `<build><resources>` | — | — | 같은 상대 경로로 이동, 리소스 디렉터리 합집합 | 없음 |
| S14 새 부모가 관리하는 명시 버전 | JAXB 3.0.x, 옛 jackson/lombok 고정 등 | 불필요한 고정 | `layout.drop_managed_versions: gte`로 restructure 후 effective pom 기준 삭제, 노트 | 없음 |
| S15 role parent 위의 예전 사내 마이그레이션 | 모듈들이 옛 `comp-jar` / `boot-jar` 1.0.0을 직접 상속 | 옛 체인 잔존 | role parent 설정 시 boot 모듈 → `boot_parent`, 나머지 → `comp_parent`, 루트 → base-parent, 다른 버전의 role parent는 이동 | 없음 |
| S16 비 Boot 프로젝트의 웹 리소스 | `src/main/webapp` 정적 파일, JSP | 회사 레이아웃 | `{name}-front` 모듈(정적 → `static/`, JSP → `META-INF/resources/WEB-INF/jsp`), JSP면 `{name}-boot`가 외부 Tomcat용 WAR, XML 빈 설정 → boot `legacy-config/`, web.xml 등 → `asis-webinf/`(패키징 안 됨) | 뷰 prefix follow-up 확인 |
| S17 Spring Boot as-is | 디렉터리 유지가 회사 규칙 | — | `boot_pom` 모드: pom만 변경(`*-boot` 모듈 → boot-jar, 나머지 → comp-jar), 단일 모듈은 `{name}-boot`로 감쌈, 이름 규칙 위반은 경고 | 이름 경고 확인 |

### 6.5 라이브러리 권고(빌드는 막지 않음)
springfox, sleuth, hystrix, ribbon, zuul, netflix-feign, 옛 Spring Cloud train, spring-security-oauth2 레거시, log4j 1, commons-lang 2, httpclient 4, ibatis2, `jdk-generified-apis`, `jdk-removed-apis`. 각각의 정책과 다음 할 일은 `docs/ERROR-HANDLING.md` §6과 `docs/ADVISORIES.md`. Handover의 "Libraries needing your attention" 표와 "From here on, your team" 목록에 나타납니다.

## 7. 플릿 운영(수백 개 프로젝트를 한 에이전트로 순차 실행)
- **manifest**: 디렉터리(그 안의 모든 `*.zip`과 `pom.xml`이 있는 하위 디렉터리, 이름순), `.txt`(한 줄에 경로 하나, `#` 주석), `.yml`(`projects: [{name, input, configs, overrides, tags}]`), `.csv`(헤더 `name,input,tags`). 경로는 manifest 파일 기준 상대 경로. 예: `fixtures/fleet/manifest.example.yml`.
- **엄격히 순차**: 병렬 실행은 없습니다. 프로젝트 k의 수정이 KB에 수확된 뒤 k+1이 시작하므로 뒤 프로젝트일수록 LLM 호출이 줄어듭니다.
- **실행/재개**:
```powershell
.venv\Scripts\sbmig.exe fleet run D:\fleet\projects.yml --config config\artifacts.yml --out out\fleet
.venv\Scripts\sbmig.exe fleet status out\fleet
.venv\Scripts\sbmig.exe fleet resume out\fleet                 # 같은 manifest·설정으로 이어서
.venv\Scripts\sbmig.exe fleet resume out\fleet --retry-failed  # incomplete / aborted도 다시
.venv\Scripts\sbmig.exe fleet run D:\fleet\projects.yml --config config\artifacts.yml --out out\fleet --dry-run
```
- **재개 규칙**: `success`는 다시 돌리지 않음, `incomplete`/`aborted`는 `--retry-failed`(또는 `fleet.retry_failed`)일 때만, 입력이 없던 `skipped`는 입력이 생기면 다음 재개 때 실행, Gradle/Ant 거부로 `skipped`된 것은 계속 skipped(`--retry-failed`로도 안 돎, `--stop-on-abort`도 멈추지 않음), 플릿 프로세스가 죽어 `running`으로 남은 것은 다시 실행. 재시도는 새 run id(`…-r2`).
- **요약 파일**(프로젝트마다 다시 씀): `<out>/fleet/state.json`, `summary.md|csv|json`(advisories, carried_over 열 포함, "Libraries flagged across the fleet"), `learning.md`(LLM이 2회 이상 고쳤는데 레시피가 없는 심볼 = 레시피/핸들러를 만들 후보, 가장 많은 프로젝트를 막는 아티팩트 = 라이브러리 담당에게 보낼 표, 권고, 효과 본 KB 레시피), `fleet-config.json`, `sbmig.log`.
- **종료 코드**: 0 전부 성공, 2 일부 미완료/skipped/미실행, 3 `--stop-on-abort`로 정지, 1 설정·manifest 오류.
- **KB 공유**: 프로젝트별 `configs`/`overrides`는 플릿 설정 위에 병합되지만 `kb.dir`은 항상 플릿 공통입니다. 웹 서버와 플릿을 동시에 같은 `kb/`로 돌리지 마십시오(파일 KB는 프로세스 간 잠금이 없음).
- **조절**: `fleet.order`(`simple-first`로 쉬운 것부터), `fleet.max_llm_calls_total`, `fleet.max_projects`, `fleet.stop_on_abort`, `fleet.reindex_every`.

## 8. KB 위생
KB는 파일 기반(데이터베이스 없음)이며 `kb.dir`(기본 저장소의 `kb/`) 아래에 쌓입니다.

| 경로 | 쌓이는 것 |
|---|---|
| `kb/cases` | 성공한 LLM 수정마다 `YYYY-MM/<id>.json`(KBCase) |
| `kb/recipes/candidates` | 기계적 수정에서 만든 레시피 후보(`<name>.yml` + `<name>.meta.json`) |
| `kb/recipes/active` | 승격된 레시피 — **모든 실행**의 OpenRewrite에 포함되고 시그니처가 맞으면 LLM보다 먼저 시도 |
| `kb/patterns` | 손으로 쓴 지름길(시그니처 정규식 → 레시피 / 핸들러 id / 힌트) |
| `<kb.dir>/index.json` | 시그니처 색인 |
| `<kb.dir>/ledger.jsonl` | 추가 전용 해결 기록(프로젝트 × 심볼 × 해결 방식 × 라벨) |
| `<kb.dir>/symbols.json` | 오류 심볼별 수정 이력 |

운영 습관:
1. **후보 검토**: `sbmig kb list --status candidate` → `sbmig kb show <id>`로 YAML 확인 → `sbmig kb promote <id>` 또는 `sbmig kb reject <id> --reason "…"`. 자동 승격은 서로 다른 `kb.auto_promote_after_projects`(기본 2)개 프로젝트에서 실패 없이 성공했을 때. 수동 검토만 원하면 0.
2. **문제 레시피 의심 시**: 해당 레시피를 `reject`하거나, 시험 실행만 `--no-kb`로 돌려 비교합니다.
3. **손으로 고친 뒤**: `sbmig kb reindex`(index.json + symbols.json 재생성). 플릿은 `fleet.reindex_every`마다 자동.
4. **정리(prune)**: 자동 삭제 명령은 없습니다. ledger는 추가 전용이 원칙입니다. 커지면 `sbmig kb export`로 보관한 뒤 정리 여부를 개발자와 정합니다(코드 기준: `sbmig/kb/store.py`).
5. **백업/이동**: `sbmig kb export kb-YYYYMMDD.zip` → 다른 PC에서 `sbmig kb import kb-YYYYMMDD.zip`(기본 병합, `--replace`는 교체). git에 함께 커밋해도 됩니다. 플릿 시작 전과 매일 끝에 export를 권장합니다.
6. **오염 방지**: 테스트·데모 실행은 `out/` 아래 KB를 씁니다(`config/artifacts.local-e2e.yml`의 `kb.dir: out/e2e/kb`). 회사 실험 실행도 `--no-kb` 또는 별도 `--config`로 `kb.dir`을 바꾸십시오.
7. **배운 것 보기**: `sbmig kb learning --md`, `sbmig kb ledger --resolution blocked`, `sbmig kb symbols`.

## 9. FAQ — 2026-10-21 회사 실행에서 실제로 나온 것

### 9.1 게이트웨이가 503을 돌려준다(Postman에서는 됨)
원인은 페이로드 플래그 철자였습니다. 올바른 철자는 **`need_origin`**(예전 설정의 `need_orgin` 오타가 503의 원인, 수정됨). 확인 순서:
```powershell
.venv\Scripts\sbmig.exe llm-probe --prompt "안녕"                          # sent body에 "need_origin": true 확인
.venv\Scripts\sbmig.exe llm-probe --payload-extra '{}' --no-system --model <Postman의 모델>   # Postman과 같은 최소 요청
.venv\Scripts\sbmig.exe llm-probe --bare                                   # sbmig 클라이언트 우회
```
`sent body`, `proxies in effect`, `response headers`를 Postman 요청과 비교합니다. 프록시가 끼면 `.env`에 `NO_PROXY=<게이트웨이 호스트>`.

### 9.2 `rewrite: forked run failed in compile → retrying with runNoFork`
정상입니다(E9). 아직 컴파일되지 않는 프로젝트에서 `rewrite:run`이 compile을 먼저 돌리다 실패하면, 의존성 classpath로 파싱하는 `runNoFork`로 자동 재시도합니다.

### 9.3 `fix round 4/3`? / `max fix rounds (3) used up`
fix 라운드를 다 쓴 뒤의 마지막 빌드 줄입니다. 마지막 라운드의 변경도 빌드·평가한 뒤 "max fix rounds (N) used up; the phase ends here and the next phase / the final build carry the rest (reported as unresolved)"로 단계를 끝냅니다. 예전 표기 `fix round 4/3`도 같은 뜻이었습니다(문구만 정리됨). 라운드가 자주 모자라면 `loop.phases.spring.max_iterations` 등을 늘립니다.

### 9.4 lombok `IllegalAccessError`
JDK 21에서 옛 lombok(부모가 상속한 annotation processor 경로 포함)이 javac 내부에 접근하다 실패하는 것입니다. 핸들러 `lombok-version-floor`가 javac 내부 오류 문구만으로도 선택되어, 버전 없는 `annotationProcessorPaths` 항목을 고정하고, lombok 선언이 없으면 provided로 추가하고, 부모가 상속한 옛 processor 경로는 `<annotationProcessorPaths combine.self="override">`로 끊습니다. LLM 호출이 들면 안 되는 오류입니다 — 반복되면 개발자에게.

### 9.5 rewrite 로그의 `token recognition error at: '<'`
OpenRewrite의 XML 파서가 JSP/HTML을 읽다 낸 경고(파일은 건너뜀)였습니다. 9c부터 `openrewrite.exclusions`(`**/*.jsp`, `**/*.jspx`, `**/*.html`, `**/*.htm`)로 파싱에서 제외합니다. 아직 보이면 예전 체크아웃이거나 회사 설정이 `exclusions` 리스트를 덮어쓴 것입니다(리스트는 통째로 교체됨).

### 9.6 `'dependencies.dependency.version' for javax.servlet:jstl:jar is missing`
E11. 핸들러 `jstl-to-jakarta`가 Jakarta JSTL로 바꿉니다(버전은 Boot BOM). 그대로 실패하면 개발자에게.

### 9.7 `DefaultMutableTreeNode` / "inference variable T has incompatible equality constraints"
JDK 9에서 `javax.swing.tree`가 제네릭화되어(`children()` → `Enumeration<TreeNode>`) javac 21이 거부하는 것입니다. 권고 `jdk-generified-apis`와 fix-loop 노트가 LLM에 포팅 방법을 줍니다. Handover에 나오면 포팅된 트리 코드를 리뷰합니다.

### 9.8 Boot rewrite가 258초 걸렸다
정상입니다. 레시피 실행 시간은 대략 소스 양에 비례합니다. 그동안 60초마다 `still running` heartbeat가 나옵니다. 조절 손잡이: `openrewrite.jvm_args`(메모리), `openrewrite.timeout_minutes`(기본 40분), `openrewrite.exclusions`(생성 소스·대용량 리소스 제외 — 리스트 전체를 다시 적음).

### 9.9 `MalformedInputException: Input length = 1`(fontawesome 웹폰트)
E12. 핸들러 `resources-binary-filtering`이 `nonFilteredFileExtensions`를 추가합니다. 이 핸들러가 생기기 전에는 LLM이 시도하다 실패했습니다 — 지금도 LLM으로 가면 예전 체크아웃입니다.

### 9.10 `UnicodeDecodeError: 'cp949'` 다음 `could not write changes.patch: data must be str, not NoneType`
2026-10-21 이전 체크아웃입니다(git을 콘솔 코드 페이지로 디코딩). `git pull` 후 재설치하십시오. 지금은 git을 바이너리 모드로 돌리고 `changes.patch`를 원본 바이트로 씁니다.

### 9.11 pom에 적힌 저장소에 접속할 수 없다
X9. `config/settings.nexus.example.xml`을 복사해 그룹 URL을 넣고 `build.settings_file`에 절대 경로로 지정합니다(`mirrorOf external:*`가 pom 선언 저장소까지 Nexus로 돌림). 결과 pom에서 URL 자체를 없애려면 추가로 `build.pom_repositories: remove`.

### 9.12 "spring it2"에서 로그 없이 멈췄다(`fixer.propose`에서 정지)
원인은 `nearest_pom`이 Windows 절대 경로에서 무한 루프를 돈 것이었고 커밋 `8c41670`에서 고쳤습니다. 이후 회사 2차 실행이 `spring it2`를 통과했습니다. 같은 증상이 다시 보이면 4절 절차로 세 줄을 보내 주십시오.

### 9.13 boot 모듈 pom에 `spring-boot-maven-plugin`이 있다
의도된 것입니다(실행형 jar/war로 repackage). 빌드 결과물 안에 `BOOT-INF/`가 있는지로 확인합니다.
```powershell
tar -tf shop-boot\target\shop-boot-1.0.0.jar | Select-String "BOOT-INF/" | Select-Object -First 5
```
(WAR면 `WEB-INF/lib-provided/` 등을 봅니다. 파일 이름은 프로젝트에 맞게 바꾸십시오.)

### 9.14 LLM 답을 받은 직후(`llm_fix`) 멈춘다
4.4의 3번. 패치 적용 / `git commit` 단계입니다. git은 비대화형(10분 타임아웃)이므로 그래도 멈추면 백신·인덱서가 작업 사본 파일을 잡고 있는 것입니다. `out/` 디렉터리를 실시간 검사 예외로 등록하는 것을 검토하십시오.

### 9.15 `config error … artifacts: Field required`
`--config config\artifacts.yml`을 빠뜨렸습니다(종료 코드 2). 웹에서는 artifacts 파일을 체크하지 않은 경우입니다.

### 9.16 `There were problems parsing …` / 플래그 `rewrite-parse-problems`
OpenRewrite가 그 파일들을 파싱하지 못해 **어떤 레시피도 그 파일을 고치지 않았습니다**(phase 노트에 파일 목록, 요약 줄만 있으면 `(unknown files — see the rewrite log)`). 남은 javax 참조는 E13 핸들러, 페이징 리포지토리는 E14 핸들러, 나머지는 수정 루프가 빌드 오류를 보고 처리합니다. 빌드가 통과했으면 할 일은 없습니다. 같은 파일이 계속 나오면 rewrite 로그에서 원인(문법 오류, 생성 소스)을 확인하십시오.

### 9.17 `jakarta.persistence.Query cannot be converted to javax.persistence.Query` / `no suitable method found for findAll(no arguments)`
E13 / E14. 핸들러 `jakarta-namespace-sweep` / `spring-data-crud-repository`가 LLM 없이 고칩니다(플래그 `jakarta-leftovers-renamed` / `spring-data-crud-restored`). 이 오류로 LLM 호출이 들면 예전 체크아웃입니다. javax 쪽이 의존성 jar의 클래스이면 X1(blocked)입니다.

## 10. 지원 요청 시 보낼 것
회사 PC는 폐쇄망이고 로그를 손으로 옮겨 적어야 하므로 **최소한만** 보냅니다.

| 우선순위 | 무엇 | 어떻게 얻나 |
|---|---|---|
| 1 | run id와 종료 코드 | `report.md` 첫 표의 Run id, `echo $LASTEXITCODE` 또는 웹 상태 |
| 2 | report.md 첫 표의 Result / Flags / Blocked 줄, 그리고 Blocked 첫 행 또는 Unresolved 첫 항목 | report.md |
| 3 | 멈춤이면 세 줄: watchdog 줄(`fix loop quiet for Ns since '<milestone>'`), `first error:` 줄, 스레드 덤프의 `--- thread sbmig-run-…` 블록 맨 아래 15줄 | 4.4의 PowerShell 명령 |
| 4 | 게이트웨이 문제면 `sbmig llm-probe`의 `HTTP <code>` 줄과 응답 본문 첫 줄 | 9.1 |
| 5 | (가능할 때만) `sbmig.log` 마지막 30줄, 해당 `llm-NNNN.json` | 5절 |

API 키는 어떤 로그에도 남지 않지만(`llm-probe` 출력도 마스킹) 옮겨 적을 때 `.env` 내용은 포함하지 마십시오.

## 11. 서버 종료가 멈출 때 (2026-10-21 추가)

`sbmig serve`를 Ctrl+C로 끄면 uvicorn은 열린 연결이 닫히길 기다립니다. 브라우저 탭이 실행 중인 run의 이벤트 스트림(SSE)을 붙들고 있으면
예전에는 `Waiting for connections to close`에서 영원히 멈췄고, Windows에서는 `mvn.cmd`가 띄운 `java.exe`가 서버가 죽은 뒤에도 살아
남아 `target/`을 잠근 채 CPU를 썼습니다. 지금은 종료 시 (1) 열린 스트림을 `end`(reason `server shutdown`)로 끝내고, (2) 실행 중인 run에
정지를 요청하고, (3) 아직 도는 Maven/OpenRewrite 프로세스 트리를 `taskkill /T /F`로 죽입니다. uvicorn이 연결을 기다리는 시간은
`sbmig serve --graceful-timeout 5`(기본 5초)로 제한됩니다. `uvicorn sbmig.web.app:app …`으로 직접 띄울 때는
`--timeout-graceful-shutdown 5`를 꼭 붙이십시오. 종료로 끊긴 run은 리포트에 `aborted`로 남고 최종 빌드는 없습니다.
그래도 남는 프로세스가 있으면 PowerShell에서 `tasklist | findstr java`로 확인하고 `taskkill /PID <pid> /T /F`로 정리합니다.
