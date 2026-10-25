# 09. 운영과 진단 (회사 환경)

이 문서는 회사(Windows, 폐쇄망, 사내 LLM 게이트웨이, 사내 Nexus) 환경에서 `sbmig`를 설치·실행·진단하는 운영 안내서입니다. `docs/RUNBOOK.md`, `docs/ERROR-HANDLING.md`, `docs/FLEET.md`와 2026-10-21 첫 회사 실행에서 실제로 겪은 문제(`docs/PLAN.md` round 9b / 9c의 "same-day follow-ups")를 한국어로 정리했습니다. round 10(2026-10-26)에서 추가된 인프라 사전 점검, 한 줄 결과(`SBMIG-RESULT`), 플릿 인프라 차단기·디스크 정리, Windows 스레드 덤프 방법도 들어 있습니다. 사전 준비 → 실행 체크리스트 → 보고서 읽는 법 → "멈춘 건가?" 진단 → 로그 파일 지도 → 오류 카탈로그(ID 유지) → 플릿 운영 → KB 위생 → FAQ → 지원 요청 시 보낼 것 순서입니다. 회사 PC에서 로그를 손으로 옮겨 적어야 한다는 전제로, 명령은 PowerShell 기준으로 쓰고 요청하는 로그는 최소한으로 줄였습니다.

## 이 문서를 읽고 나면
- 마이그레이션 PC를 준비하고(JDK 21, Maven + Nexus 미러, Python, git, `.env`) 첫 실행을 안전하게 할 수 있습니다.
- `report.md`를 위에서부터 읽고 종료 코드·플래그의 뜻을 판단할 수 있습니다.
- "멈춘 건지 일하는 중인지"를 heartbeat·watchdog·스레드 덤프로 구분하고, 개발자에게 보낼 세 줄을 뽑을 수 있습니다.
- 자주 보는 오류(X/E/L/S 카탈로그)마다 sbmig가 무엇을 하고 운영자가 무엇을 해야 하는지 압니다.
- 플릿(수백 개 프로젝트 순차 실행)과 KB를 관리할 수 있습니다. 플릿이 시작 전에 인프라를 점검하고(`preflight: …`), 인프라가 죽으면 기다렸다가 다시 점검하거나 멈추는(`fleet stopped: …`) 동작을 압니다.
- 실행이 끝날 때 나오는 한 줄(`SBMIG-RESULT …`)과 주의 코드(`attn=`)만 보고 무엇을 할지 정하고, 문제를 알릴 때 옮겨 적을 줄을 PowerShell로 뽑을 수 있습니다(3.4, 10절).

## 관련 코드
- `sbmig/loop/fixloop.py` — watchdog(`WATCHDOG_S`, `WATCHDOG_QUIET_S`, `WATCHDOG_DUMP_S`), 클러스터 결정 로그(`first error:`), 라운드 로그
- `sbmig/build/mvn.py` — Maven heartbeat(`HEARTBEAT_S`), 타임아웃 시 프로세스 트리 종료(`taskkill /T /F`), `mvn.cmd` 해석
- `sbmig/fix/company.py` — LLM heartbeat(`still in flight`), 하드 데드라인
- `sbmig/core/diag.py` — 스레드 덤프(Ctrl+Break, `dump-threads` 파일 트리거), `sbmig/web/app.py` — `/api/threads`, 접근 토큰
- `sbmig/report/result_line.py` — `SBMIG-RESULT` 한 줄과 주의 코드, `sbmig/fleet/preflight.py` — 플릿 사전 점검, `sbmig/fleet/cleanup.py` — `fleet.cleanup`과 `sbmig clean`
- `sbmig/core/workspace.py` — 비대화형 git(10분 타임아웃), UTF-8 디코딩, `changes.patch`
- `sbmig/report/markdown.py`, `sbmig/report/handover.py` — 보고서 절 제목과 순서
- `sbmig/fleet/runner.py`, `sbmig/fleet/cli.py`, `sbmig/kb/store.py`, `sbmig/kb/ledger.py`
- 문서: `docs/RUNBOOK.md`, `docs/ERROR-HANDLING.md`, `docs/FLEET.md`, `docs/ADVISORIES.md`

## 1. 사전 준비와 설치

### 1.1 마이그레이션 PC 요구 사항
| 항목 | 요구 | 이유 / 확인 방법 |
|---|---|---|
| Python | 3.10 이상(+ `uv` 또는 `pip`) | 에이전트 런타임. `python --version` |
| JDK | **21**, `build.java_home`에 경로 고정 | OpenRewrite와 모든 빌드가 21에서 돕니다. as-is 프로젝트를 옛 JDK로 빌드하는 일은 없습니다. `& "C:\Program Files\Java\jdk-21\bin\java.exe" -version`. 실행 첫머리의 사전 점검이 `mvn -v`의 Java 버전을 `build.require_java_major`(21)와 비교하고, 다르면 빌드 없이 중단합니다(종료 코드 3, 플래그 `jdk-not-21`, 9.20) |
| Maven | 3.9.x | `where.exe mvn`, `mvn -v`. Windows의 Maven은 `mvn.cmd`이며 sbmig가 PATH / `MAVEN_HOME` / `M2_HOME`에서 찾습니다. 못 찾으면 `build.mvn`에 `mvn.cmd` 절대 경로 |
| Maven settings | catch-all 미러(`mirrorOf external:*`)가 있는 settings.xml을 `build.settings_file`에 | `config/settings.nexus.example.xml`을 복사해 Nexus 그룹 URL 입력. pom에 선언된 저장소까지 Nexus로 우회합니다(RUNBOOK §0.1). |
| Nexus가 프록시해야 하는 것 | Maven Central의 `org.openrewrite.*`(플러그인 6.46.1, 레시피 BOM **3.37.0**과 그 BOM이 가리키는 레시피 아티팩트들 — 3.38.0은 Central에서 불일치), `org.springframework.boot:*:4.1.0`, jakarta 아티팩트, 프로젝트 의존성, 사내 `xxx-parent` / `xxx-boot-jar` / `xxx-comp-jar` 4.0.0 | 레시피 jar가 실제로 받아지는지는 `sbmig recipes check --jars --config config\artifacts.yml`로 확인합니다(Maven + 네트워크 필요). |
| LLM 게이트웨이 | 저장소 루트 `.env`: `LLM_BASE_URL`(전체 URL), `LLM_API_KEY`, `LLM_MODEL` | 제공자 `company`(순수 `requests.post`). `sbmig llm-probe`로 확인 |
| git | 아무 버전 | 시도마다 스냅샷·되돌리기·diff. 작업 사본 안의 일회용 로컬 저장소이며 `result.zip`에는 `.git`이 없습니다. |
| 디스크 | 프로젝트 크기 × (작업 사본 + `result.zip` + 로그) | 실행 디렉터리는 `out/<run id>/`. 플릿은 프로젝트마다 출력 드라이브의 남은 공간을 확인하고(`fleet.min_free_gb`, 5 GB) 끝난 프로젝트의 `target/`을 지웁니다(`fleet.cleanup`). 쌓인 작업 사본은 `sbmig clean`으로 정리합니다(7절) |

### 1.2 설치
```powershell
cd D:\migration-agent
uv venv
Set-ExecutionPolicy -Scope Process Bypass   # "Activate.ps1 … cannot be loaded because running scripts is disabled"이면: 이 창에서만 허용
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
- **`.env`**: 셸 프로필이 없어도 저장소 루트 `.env`(또는 `SBMIG_ENV_FILE`이 가리키는 파일)를 `load_config`가 가장 먼저 읽습니다. 셸에 이미 있는 변수는 덮어쓰지 않습니다. 파일은 UTF-8(BOM 유무 무관), UTF-16(메모장 "유니코드"나 PowerShell `>`로 저장한 파일), cp949 중 무엇으로 저장돼도 읽습니다. 실행 중에 `.env`를 고치면 다음 실행(웹 서버의 다음 run, 플릿의 다음 프로젝트)이 다시 읽습니다(round 10). 셸 변수는 여전히 이깁니다.
- **콘솔 코드 페이지(cp949)**: sbmig는 git·Maven 출력을 스스로 UTF-8로 디코딩하고 `changes.patch`는 git diff 원본 바이트로 씁니다(MS949 소스는 패치에서도 MS949). `UnicodeDecodeError: 'cp949' codec can't decode byte …`가 보이면 2026-10-21 이전 체크아웃입니다 → `git pull`. round 10부터는 콘솔 출력과 로그 핸들러가 cp949로 쓸 수 없는 문자를 `?`로 바꿔 쓰므로, 출력을 파일로 돌리거나(`> run.txt`) 작업 스케줄러로 돌려도 보고서를 쓴 뒤 `UnicodeEncodeError`로 죽지 않습니다. 콘솔 요약은 ✅ / ❌ 대신 `ok` / `FAIL`을 씁니다. 모든 Maven 호출의 `MAVEN_OPTS`에 `-Dstdout.encoding=UTF-8 -Dstderr.encoding=UTF-8`을 붙여 한글 경로가 깨지지 않게 합니다(`build.env`나 `MAVEN_OPTS`에 이미 그 속성이 있으면 그 값이 이김).
- **PowerShell 5.1의 `Get-Content` / `Select-String`**: 인코딩을 주지 않으면 UTF-8 로그의 `—`와 한글이 깨져 보입니다. 이 문서의 명령처럼 항상 `-Encoding UTF8`을 붙이십시오.
- **Ctrl+Break**: `sbmig run` / `sbmig fleet` 콘솔에서 Ctrl+Break를 누르면 실행은 계속되고 스레드 덤프만 씁니다(4.3). Maven / git 자식 프로세스는 숨은 콘솔(`CREATE_NO_WINDOW`)로 돌기 때문에 Ctrl+Break가 빌드를 끊지 않습니다. `sbmig serve` 창에서는 uvicorn이 Ctrl+Break를 받아 서버를 끄므로 쓰지 마십시오(대신 `dump-threads` 파일이나 `/api/threads`).
- **Windows 경로**: javac가 `/C:/…`, `file:///C:/…`, `file:/C:/…`, 퍼센트 인코딩(한글 폴더), 소문자 드라이브로 찍은 경로도 프로젝트 상대 경로로 바꾸므로 같은 에러는 PC와 폴더가 달라도 같은 시그니처가 됩니다(round 10).
- **타임아웃 종료**: Maven 타임아웃(`build.timeout_minutes`) 시 `taskkill /PID <pid> /T /F`로 프로세스 트리 전체를 죽입니다(그냥 terminate하면 `target/`과 소스 파일이 잠긴 채 남음).
- **git**: 작업 사본의 git은 비대화형입니다(프롬프트·훅·GPG 서명 끔, 명령당 10분 타임아웃). 그래도 커밋 단계에서 멈추면 백신/인덱서가 파일을 잡고 있는 것입니다. 명령마다 `-c core.longpaths=true -c core.autocrlf=false -c core.quotepath=false -c safe.directory=*`를 넘기고, 작업 사본의 `.git/info/attributes`에 `* -text -eol -filter -ident -working-tree-encoding`을 써서 프로젝트의 `.gitattributes`(`eol=crlf` 등)가 되돌리기 때 줄 끝을 바꾸지 않게 합니다(round 10).
- **PowerShell에서 `curl`**: `Invoke-WebRequest` 별칭일 수 있으니 `curl.exe`를 명시합니다.

## 2. 실행 체크리스트

### 2.1 처음 한 번
1. `sbmig llm-probe` — 설정 파일 없이 `.env`만으로 게이트웨이 호출. `HTTP 200`과 `extracted reply: '…'`, 마지막 줄 `LLM-PROBE ok provider=company model=<모델> status=200 latency=<초>s reply='pong'`이 나와야 합니다. 실패하면 마지막 줄 `LLM-PROBE FAIL <kind> …`(종료 코드 1)만 옮겨 적으면 됩니다. 503이면 9.1.
2. `sbmig recipes check --config config\artifacts.yml`, `sbmig advisories check --config config\artifacts.yml` — 둘 다 문제 0건.
3. `sbmig analyze D:\asis\order-api.zip --config config\artifacts.yml` — 변경 없이 케이스 판정·경고·권고 확인.
4. `sbmig run D:\asis\order-api.zip --config config\artifacts.yml --dry-run` — 결정만 계산(플래그 `dry-run`, 종료 코드 2가 정상).
5. 실제 실행: `sbmig run D:\asis\order-api.zip --config config\artifacts.yml --out out`. 마지막에 `SBMIG-RESULT …` 한 줄이 나옵니다(3.4).

### 2.2 CLI와 웹 중 무엇을 쓰나
| | CLI `sbmig run` | 웹 `sbmig serve` |
|---|---|---|
| 설정 | `--config`, `--java-home`, `--settings` 등 플래그 | `config/` 안 yml을 체크(덮어쓰기 플래그 없음) |
| 진행 확인 | 콘솔 + `<run dir>/sbmig.log` | 화면의 그래프 노드 / 저널 / 로그 실시간(SSE) |
| 중단 | Ctrl+C(그 자리에서 중단, 보고서가 남지 않을 수 있음) | "중단" 버튼 — 현재 노드가 끝난 뒤 멈추고 보고서를 씀(verify 전이면 최종 빌드 없이 종료 코드 3) |
| 동시 실행 | 한 번에 하나만 돌리십시오(KB 공유) | 서버가 자동으로 줄 세움(`queued`) |
| 스레드 덤프 | watchdog 자동 덤프(5분), Ctrl+Break, `dump-threads` 파일 | `/api/threads`, `dump-threads` 파일 + 자동 덤프(Ctrl+Break는 서버를 끔) |
| 결과 한 줄 | 콘솔 마지막 줄 `SBMIG-RESULT …` | 그 실행의 `sbmig.log`에 같은 줄 |
| 다수 프로젝트 | `sbmig fleet run` | 하나씩 업로드 |

웹 서버 실행: `sbmig serve --port 8765` → 브라우저 `http://127.0.0.1:8765`. 이 PC에서만 접속할 때는 토큰이 필요 없습니다. **다른 PC에서 접속하게 하려면 토큰이 필수입니다**(round 10): `.env`(또는 셸)에 `SBMIG_WEB_TOKEN=<길고 임의인 값>`을 두고 `sbmig serve --host 0.0.0.0 --port 8765`(또는 `--token <값>`)로 띄운 뒤, 다른 PC의 브라우저는 처음 한 번만 `http://<호스트>:8765/?token=<값>`으로 엽니다(7일짜리 HttpOnly 쿠키가 남고 주소창에서 토큰이 사라짐). API 클라이언트는 `Authorization: Bearer <값>` 헤더를 보냅니다. 토큰 없이 `--host 0.0.0.0`이면 서버가 뜨지 않고(`refusing to serve on 0.0.0.0 without a token …`, 종료 코드 2), 순수 `uvicorn --host 0.0.0.0`으로 띄워도 토큰이 없으면 이 PC가 아닌 접속은 403입니다. 토큰을 켜면 이 PC의 `curl.exe`도 헤더가 필요합니다. REST 사용법은 [07. API 레퍼런스](07-api-reference.md).

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
| 4.1 | `## Build errors and how each was handled` | 이번 실행에서 본 빌드 에러 전부를 하나씩: 상태별 / 해결 수단별 수, LLM이 단순 치환으로 고친 수, 표(최대 40행, 미해결부터): 상태, 위치, 메시지, 어떻게 해결했나 또는 왜 못 했나, 시도 수. 전체는 `error_cases.csv` / `error_cases.jsonl`, 시도별 diff는 `fix_attempts.jsonl`(2026-10-23) |
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
| 3 | 중단(Maven 없음, Maven이 Java 21이 아님(`jdk-not-21`), Maven 프로젝트 아님, 사내 부모 해석 불가, 내부 오류, 웹 중단) | Manual follow-ups 첫 줄(`run aborted: …`, `sbmig.log`에도 같은 ERROR 줄)의 사유를 해결 |

라이브러리 권고와 carried-over 고정은 종료 코드를 바꾸지 않습니다(회사 방침: 빌드를 막는 정책은 없음). 중단된 실행은 마지막 빌드가 통과했어도 성공으로 보지 않습니다(round 10: 계획된 최종 빌드가 결과 없이 남거나 실행이 중단되면 `report.success`가 false — 중단된 MSA 실행이 BUILD SUCCESS로 보이던 문제). 플릿(`sbmig fleet run`)이 인프라 문제로 멈춘 경우도 종료 코드 3입니다(7절).

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
| `aborted`, `aborted:parent-not-found`, `mvn-not-found`, `jdk-not-21`, `unsupported-build-tool:<도구>` | 중단 사유(종료 코드 3). `jdk-not-21`은 `build.java_home`(또는 `JAVA_HOME`)을 JDK 21로 고치고 재실행(9.20) |
| `llm-config-error` | 게이트웨이가 서로 다른 프롬프트 2개에 **같은** HTTP 400 본문을 돌려줌 → 모델 이름 / payload 설정 오류로 보고 이 실행의 LLM 호출을 멈춤(9.22) |
| `infra:llm`, `infra:maven`, `infra:jdk`, `infra:repository`, `infra:disk` | 실행이 인프라 이유로 실패했음(LLM 불가·설정 오류, Maven 없음, JDK가 21 아님, Nexus / 저장소 접속 실패, 디스크 가득). 플릿은 이 프로젝트를 `pending`으로 되돌리고 인프라 차단기를 셉니다(7절). 성공한 실행에는 붙지 않음 |
| `jdk-base64-shimmed`, `tools-jar-removed`, `boot4-types-relocated`, `security-path-matchers-rewritten`, `specification-unrestricted` | round 10 결정적 핸들러가 고침: `sun.misc.BASE64*` → 생성한 `sbmig.compat` 클래스, `tools.jar` 의존성 제거, Boot 4 타입 이동, `AntPathRequestMatcher` → `PathPatternRequestMatcher`(`// TODO sbmig:` 줄 확인), `Specification.where(null)` → `unrestricted()` ([04](04-triage-and-loop.md) 5.1) |
| `stopped-after-<단계>`, `dry-run` | 의도적 |

전체 목록은 [07. API 레퍼런스](07-api-reference.md) 6.2.

### 3.4 한 줄 결과 `SBMIG-RESULT`와 주의 코드(round 10)
`sbmig run`은 끝날 때(예외로 죽을 때도) 콘솔 마지막 줄과 `sbmig.log`에 다음 한 줄을 남깁니다. 플릿은 프로젝트마다 한 줄(콘솔과 `fleet/sbmig.log`), 웹은 그 실행의 `sbmig.log`에 남깁니다. 전부 ASCII라 cp949 콘솔에서도 그대로 옮겨 적을 수 있습니다. **문제를 알릴 때 가장 먼저 옮겨 적는 줄입니다.**

```text
SBMIG-RESULT run=run-20261026-101500-ab12cd exit=2 build=FAIL unresolved=3 blocked=1 llm=7 attn=blocked:external_javax_api,unresolved:3
```

| 필드 | 뜻 |
|---|---|
| `run` | 실행 id(실행 디렉터리 이름) |
| `exit` | 종료 코드(3.2). 예외로 죽으면 `exit=1 … attn=crash` |
| `build` | 최종 `mvn clean package` 결과 `ok` / `FAIL`(중단된 실행은 `FAIL`) |
| `unresolved`, `blocked`, `llm` | 남은 에러 수, blocked 항목 수, LLM 호출 수 |
| `attn` | 사람이 봐야 하는 이유(주의 코드, 쉼표 구분, 중요한 것부터). `-` = 볼 것 없음 |

| 주의 코드 | 뜻 | 운영자가 할 일 |
|---|---|---|
| `infra:<kind>` (`llm` / `maven` / `jdk` / `repository` / `disk`) | 인프라 이유로 끝남(초록 실행에는 붙지 않음) | 해당 인프라 확인(6.6). 플릿은 자동으로 재시도·대기(7절) |
| `skipped:<도구>`, `skipped:input` | 플릿만: 이관하지 않음(`skipped:gradle` 등 Maven 아님, 입력 없음) | 대상에서 제외 / manifest 경로 확인 |
| `crash` | 실행이 예외로 끝남 | `sbmig.log`의 `run aborted by an unexpected error` 다음 traceback 마지막 줄을 개발자에게 |
| `aborted`, `aborted:<사유>` | 종료 코드 3(`aborted:parent-not-found`, 사용자 중단 …) | follow-up 첫 줄 `run aborted: …` |
| `blocked:<사유>` | 사유(`BlockReason`)마다 하나(`blocked:external_javax_api` …) | 보고서 Blocked 절의 제안 |
| `unresolved:<n>` | 최종 빌드 뒤 남은 에러 n개 | 보고서 Unresolved / "Build errors and how each was handled" |
| `restructure-failed`, `llm-budget-exhausted`, `time-budget-exhausted`, `javax-api-on-classpath`, `tests-skipped`, `test-compile-skipped`, `tests-not-compiled` | 같은 이름의 플래그(3.3) | 3.3 |
| `incomplete` | 종료 코드 2인데 위의 어느 것도 아님 | 보고서 확인 |

플릿에서는 같은 코드가 `summary.csv`의 `attention` 열에 들어가고, `sbmig fleet status <out> --attention`이 사람이 볼 프로젝트만 첫 코드별로 묶어 보여 줍니다(7절).

```powershell
Select-String -Path "$run\sbmig.log" -Pattern "SBMIG-RESULT" -Encoding UTF8 | Select-Object -Last 1
```

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
milestone은 지금 실행 중인 코드를 가리킵니다: `deciding <cluster>`, `advisory hints`, `merge-conflict hints`, `fixer.propose … (prompt packing + LLM call)`, `LLM answered: applying`, `git commit`, `mvn package (build 3)` 등. `fix prompt for <cluster>: packing …` / `… packed: N chars → calling the LLM` 줄이 프롬프트 구성을 감쌉니다. LLM·Maven은 자체 heartbeat가 있으므로, **heartbeat 없이 watchdog만 반복되면** 그 사이의 코드(힌트, 프롬프트 패킹, 패치 적용, git)에 걸린 것입니다. round 10부터 watchdog은 루프 빌드·rewrite·LLM HTTP 호출·살아 있는 Maven 자식 프로세스가 도는 동안에는 아예 말하지 않습니다(그 시간은 heartbeat가 맡음). 줄 끝의 `kill -USR1 <pid>`는 Linux/macOS용이고, Windows에서는 Ctrl+Break나 `dump-threads` 파일(4.3)을 씁니다.

### 4.3 스레드 덤프
| 방법 | 어디에 저장 | 비고 |
|---|---|---|
| 자동(watchdog) | 5분(`WATCHDOG_DUMP_S` = 300초) 동안 milestone이 없으면 실행 디렉터리에 `threads-<YYYYMMDD-HHMMSS>.txt` | CLI·웹 모두. milestone당 한 번 |
| 파일 트리거 `dump-threads`(round 10) | 그 파일이 있던 디렉터리의 `threads-<ts>.txt` | CLI·플릿·웹 모두, Windows의 표준 방법. sbmig를 시작한 디렉터리와 최근 실행 디렉터리(최대 8개)를 2초마다 보고, `dump-threads`(또는 탐색기가 만든 `dump-threads.txt`) 파일이 생기면 덤프를 쓰고 그 파일을 지웁니다. 로그 `thread dump requested by <경로>`. `New-Item "$run\dump-threads" -ItemType File` |
| Windows 콘솔 Ctrl+Break(round 10) | 현재 디렉터리 기준 `out\threads-<ts>.txt` | `sbmig run` / `sbmig fleet` 창에서. 실행은 계속됩니다. 메인 스레드가 Maven을 기다리는 중이면 다음 heartbeat 때(최대 60초 뒤) 써집니다. **`sbmig serve` 창에서는 서버가 꺼지므로 쓰지 마십시오** |
| 웹 `GET /api/threads` | 응답 본문 + `<웹 out>/threads-<ts>.txt` | `curl.exe http://127.0.0.1:8765/api/threads`(서버에 토큰이 있으면 `-H "Authorization: Bearer <토큰>"`) |
| POSIX `kill -USR1 <pid>` | 현재 디렉터리 기준 `out/threads-<ts>.txt` | Linux/macOS 전용(Windows에는 SIGUSR1 없음) |

덤프에서 실행 스레드는 `--- thread sbmig-run-<run id> (…) ---`(웹) 또는 `--- thread MainThread (…) ---`(CLI) 블록입니다. 맨 아래 몇 줄이 "지금 어디서 기다리는지"입니다.

### 4.4 판단 순서
1. `sbmig.log` 끝을 봅니다. heartbeat(`still running` / `still in flight`)가 계속 나오면 **일하는 중**입니다. 기다립니다.
2. heartbeat 없이 `fix loop quiet for …` 줄만 반복되면 milestone을 확인합니다.
3. 저널의 마지막 이벤트가 `llm_fix`(답 받음)인데 `attempt`가 없으면 패치 적용/`git commit`에서 멈춘 것입니다 → 백신/인덱서 의심.
4. 몇 분간 아무 줄도 없으면 스레드 덤프를 받습니다(`dump-threads` 파일, CLI 창이면 Ctrl+Break).
5. LLM 호출이 실패했다면 그 줄의 `cause=<예외 클래스>: <메시지>`(전송 실패)나 `finish_reason=length`(프롬프트가 너무 커서 게이트웨이가 답을 자름)를 봅니다(6.6).
6. 개발자에게 보낼 것(옮겨 적을 줄): **(a) watchdog 줄 한 줄, (b) 그 직전 클러스터의 `first error:` 줄, (c) 덤프의 `--- thread sbmig-run-…`(CLI는 `--- thread MainThread`) 블록 맨 아래 15줄 정도**, LLM 문제면 (d) `cause=` 또는 `finish_reason=length`가 든 줄 하나.

```powershell
$run = "out\run-20261021-101500-ab12cd"            # 웹이면 out\web\web-…, 플릿이면 out\fleet\fleet-…-NNN-<이름>
Get-Content "$run\sbmig.log" -Encoding UTF8 -Wait -Tail 30
Select-String -Path "$run\sbmig.log" -Pattern "fix loop quiet" -Encoding UTF8 | Select-Object -Last 1
Select-String -Path "$run\sbmig.log" -Pattern "first error:" -Encoding UTF8 | Select-Object -Last 1
Select-String -Path "$run\sbmig.log" -Pattern "cause=|finish_reason=length" -Encoding UTF8 | Select-Object -Last 1
New-Item "$run\dump-threads" -ItemType File          # 2초 안에 $run\threads-<시각>.txt가 생김
Get-ChildItem $run -Filter "threads-*.txt"
Select-String -Path "$run\threads-*.txt" -Pattern "--- thread (sbmig-run-|MainThread)" -Context 0,40 -Encoding UTF8
curl.exe -s http://127.0.0.1:8765/api/threads > threads.txt   # 웹 서버만(토큰이 있으면 -H "Authorization: Bearer <토큰>")
tasklist | findstr /i "java mvn python"
```

## 5. 로그 파일 지도
| 파일 | 내용 | 언제 보나 |
|---|---|---|
| `<run dir>/sbmig.log` | 실행 하나의 DEBUG 로그(결정, 빌드, LLM 호출 — 키 없음). 웹 실행은 해당 실행 스레드의 기록만 | 항상 첫 번째 |
| `<run dir>/journal.jsonl` | 기계 판독용 이벤트(한 줄 한 JSON). 이벤트 어휘는 [07](07-api-reference.md) 6.3 | 단계·결정 흐름을 시간순으로 볼 때 |
| `<run dir>/logs/NNN-build-<label>.log` | Maven 빌드 로그(번호는 실행 내 순서) | 빌드 실패의 원문 |
| `<run dir>/logs/NNN-build-<label>-U.log` | 같은 빌드를 `-U`로 한 번 더 돌린 로그(Maven 네거티브 캐시, round 10, 6.6) | 첫 로그에 "was cached in the local repository"가 있었을 때 |
| `<run dir>/logs/NNN-rewrite-<label>.log` | OpenRewrite 실행 로그 | 레시피 실패, 다운로드 실패(X8), OOM(E8) |
| `<run dir>/logs/NNN-classpath-….log`, `NNN-effective-pom-….log` | 의존성 classpath / effective-pom 캡처 | X1 소유 아티팩트 추적, carried-over 판단 |
| `<run dir>/threads-<ts>.txt` | watchdog 자동 스레드 덤프, `dump-threads` 파일로 요청한 덤프 | 멈춤 진단 |
| `out\threads-<ts>.txt` | Ctrl+Break(Windows) / `kill -USR1`(POSIX)로 요청한 덤프(현재 디렉터리 기준) | 멈춤 진단 |
| `<llm.record_dir>/llm-NNNN.json` | LLM 요청/응답 기록(메시지, 응답, 모델, 사용량, 지연, role, metadata, error) | 게이트웨이·프롬프트 문제 |
| `<fleet out>/fleet/sbmig.log` | 플릿 전체 INFO 로그: 진행 줄 `[3/120] …`, `preflight: …`, 차단기 줄, 프로젝트마다 `SBMIG-RESULT …` | 플릿 운영 |
| `<fleet out>/<fleet id>-<NNN>-<이름>/sbmig.log` | 그 프로젝트 하나의 DEBUG 로그(round 10부터 프로젝트마다 따로 붙였다 뗌) | 플릿 안의 한 프로젝트 진단 |
| `<웹 out>/sbmig-web.log` | 웹 서버 전체 로그 | 서버 문제 |
| `<웹 out>/<run id>/web-status.json` | 웹 워커의 최종 상태(종료 코드, 오류, 중단 여부) | 서버 재시작 후 상태 확인 |
| `--log-file <경로>` | 어떤 명령이든 추가 DEBUG 로그 | analyze / plan 같은 단발 명령 진단 |

```powershell
Get-Content "$run\sbmig.log" -Encoding UTF8 -Tail 50
Select-String -Path "$run\sbmig.log" -Pattern "WARNING|ERROR" -Encoding UTF8 | Select-Object -Last 20
Select-String -Path "$run\sbmig.log" -Pattern "run aborted:|SBMIG-RESULT" -Encoding UTF8
Get-Content "$run\journal.jsonl" -Encoding UTF8 | ForEach-Object { $_ | ConvertFrom-Json } | Select-Object ts, event | Select-Object -Last 20
Get-ChildItem "$run\logs" | Sort-Object LastWriteTime | Select-Object -Last 5 Name, Length, LastWriteTime
Select-String -Path "$run\logs\*build*.log" -Pattern "\[ERROR\]" -Encoding UTF8 | Select-Object -First 10
Select-String -Path "$run\sbmig.log" -Pattern "max fix rounds|fix round" -Encoding UTF8
```

## 6. 자주 보는 오류 카탈로그
원칙: 고칠 수 없는 것에서 루프를 돌지 않고, 숨기지도 않습니다. 모든 클러스터는 `fixed`(핸들러/레시피/KB/LLM, 빌드로 확인), `blocked`(의도적으로 건너뜀 — 라이브러리·인프라·사람 조치 필요), `unresolved`(예산 안에서 실패) 중 하나로 끝납니다. 시도 하나는 다음 빌드가 `improved` / `partial`(일부만 사라짐, 진전) / `no_change` / `inconclusive`(그 빌드로는 판단 불가) / `regressed`로 채점합니다([04](04-triage-and-loop.md) 6.5). blocked는 실행 내내 기억되어 이후 단계에서 다시 시도하지 않고, 마이그레이션은 계속됩니다. 원문은 `docs/ERROR-HANDLING.md`.

### 6.1 외부/인프라 차단(X)
| ID | 증상 | 의미 | sbmig가 하는 일 | 운영자가 할 일 |
|---|---|---|---|---|
| X1 의존성의 javax API | jakarta 전환 후 `incompatible types: jakarta.servlet.http.HttpServletRequest cannot be converted to javax.servlet.http.HttpServletRequest`, `… cannot be applied … required: javax.* found: jakarta.*` | 의존 라이브러리가 아직 javax 타입을 API로 노출 | 소유 클래스 → jar → GAV를 classpath로 추적하고, 그 jar가 javax API jar 자체이면 `dependency:tree` 경로로 끌어오는 라이브러리(`via`)를 찾음. 그 아티팩트가 `artifacts.optional`이면 버전 bump가 해결책, 아니면 LLM 0회로 즉시 blocked | Blocked 행의 "`API jar` ← `라이브러리`"를 봅니다. 표(`knowledge/libraries/jakarta_libraries.yml`)에 있는 라이브러리는 핸들러 `jakarta-library-floor`가 올림. 사내 아티팩트는 Jakarta 빌드 요청 + `artifacts.optional` 추가, 그 밖은 Jakarta 릴리스로 올리거나 교체 |
| X1b 마이그레이션 후 클래스패스에 남은 javax API jar | 빌드 성공 여부와 관계없음: 보고서 절 "javax API jars still on the classpath", 플래그 `javax-api-on-classpath` | javax 기준으로 컴파일된 라이브러리가 javax API jar를 끌어옴(jakarta 타입을 넘기면 컴파일 실패, 아니면 런타임 실패) | 최종 빌드 뒤 `mvn dependency:tree -Dincludes=<javax API 패턴>`(`report.javax_api_inventory`, journal `deptree` / `javax_api_inventory`) | 절의 "via" 라이브러리를 Jakarta 빌드로 올리거나 교체, pom에 직접 선언된 API jar는 제거 |
| X2 의존성 API 변경(javax 아님) | `cannot find symbol … location: class <라이브러리 클래스>`, `method … cannot be applied` | 라이브러리 메서드 이름/시그니처가 바뀜 | LLM 1회(이름 바뀐 메서드는 적응 가능, 성공하면 `ChangeMethodName` 후보) 후 blocked | 소유 아티팩트·심볼 확인, 라이브러리 담당에 문의 |
| X3 아티팩트 없음 | `Could not find artifact g:a:v in <repo>`, `The following artifacts could not be resolved` | Nexus에 없거나 좌표가 틀림 | 알려진 relocation 핸들러 먼저, 모르면 LLM 0회로 blocked(LLM은 버전을 지어내지 않음) | Nexus에 프록시하거나 좌표 수정 |
| X4 부모 해석 불가 | `Non-resolvable parent POM` | 사내 부모(`xxx-parent` 등)를 받을 수 없음 | 설정된 사내 부모면 일찍 중단(`aborted:parent-not-found`, 종료 코드 3, round 10부터 `infra:repository`도 붙어 플릿은 그 프로젝트를 `pending`으로 되돌림) | settings.xml / Nexus / 좌표·버전 확인 |
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
| L3 회귀 | 고친 파일에 새 오류(같은 (파일, 시그니처)의 개수가 늘어남), 오류 수 × `loop.regression_ratio` 증가, 또는 pom 수정 후 `pom_invalid` / `parent_resolution` / `plugin_resolution` / `dependency_resolution` 등장(`loop.structural_regression_kinds`) | 수정이 상황을 악화. round 10: LLM이 지어낸 의존성이 해석 단계에서 빌드를 멈춰 컴파일 오류가 "사라진" 것처럼 보이던 문제도 여기서 잡힘 | 그 시도를 git으로 되돌림 | 없음 |
| L4 LLM 프로토콜 쓰레기/빈 답 | 패치가 파싱되지 않음(반복) | 모델이 형식을 안 지킴 | `loop.max_llm_attempts_per_cluster` 후 blocked(`repeated_failure`), 실행 계속. round 10부터 상한은 **같은 클러스터**(같은 클러스터 id, 또는 같은 파일 + 겹치는 시그니처)의 **채점된** 실패만 셉니다. 다른 파일의 같은 오류에 대한 시도는 힌트로만 들어가므로, 같은 `cannot find symbol`이 5개 파일에 있어도 5개 모두 LLM 차례를 받습니다 | 모델 확인(`llm-NNNN.json`) |
| L5 LLM 불가 | 재시도 후에도 전송 실패가 연속 2회. round 10부터 전송 실패는 `status=None|401|403|404|408|429|5xx`, `cause=…`, 알려진 전송 예외 클래스, 명시적 설정 문구(키 없음, `LLM_RESPONSE_PATH`로 답을 못 찾음 등)일 때만입니다. 400/413/422, content-filter, `finish_reason=length`는 그 요청의 잘못이라 세지 않음 | 게이트웨이 장애 | 첫 연속 2회 → 실행당 한 번 `llm.cooldown_seconds`(60초) 쉬고 재시도(저널 `llm_cooldown`, 플래그 `llm-cooldown`, 그 두 시도는 클러스터 시도 수에 미포함). 두 번째 연속 실패 → LLM 전용 클러스터 blocked(`llm_unavailable`), 핸들러·레시피는 계속. 같은 HTTP 400 본문이 서로 다른 프롬프트 2개에 오면 설정 오류(`llm-config-error`, 9.22) | 9.1 절차로 게이트웨이 확인 후 재실행 |
| L6 예산 소진 | `loop.max_llm_calls` / `loop.max_total_minutes` | 실행 예산 끝 | 남은 LLM 전용 클러스터 blocked(`budget_exhausted`), verify와 보고서는 항상 실행 | 예산 조정 후 재실행 |
| L7 테스트 컴파일만 실패 | TEST_COMPILE 클러스터만 남음 | 메인은 됨 | `-Dmaven.test.skip=true`, 플래그 `test-compile-skipped`, 오류는 follow-up | 테스트 수정 |
| L7b 런타임 테스트 실패만 | TEST_FAILURE만 남음 | 메인은 됨 | `-DskipTests`, 플래그 `tests-skipped`, 실패 테스트는 follow-up + unresolved, 최종 verify도 테스트 생략 | 테스트용 값 제공 후 `-DskipTests` 없이 재빌드 |
| L8 기존부터 깨진 프로젝트 | as-is가 자기 JDK에서도 컴파일 안 됨 | 원래 문제 | as-is는 빌드하지 않음 — JDK 21 이후 오류로 똑같이 보고 | as-is 담당에 알림 |
| L9 계획 후 내부 오류 | 단계 안 예외(컴포넌트 없음, 버그) | sbmig 문제 | `aborted`(종료 코드 3)이지만 **최종 `mvn clean package`는 시도**(dry-run, Maven 없음, JDK가 21 아님, 부모 해석 불가 같은 의도적 중단은 제외) — 보고서에 실제 미해결 오류. round 10: 결과가 없는 계획된 최종 빌드 스텝(MSA 서비스마다)이 모두 돌고, 끊긴 단계는 보고서에 `phase interrupted: …`(저널 `phase_end`의 `interrupted`)로 남으며, 중단된 실행은 성공으로 보지 않음. `sbmig.log`에 ERROR 한 줄 `run aborted: <사유>` | `run aborted:` 줄과 traceback 마지막 줄을 개발자에게 |
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
springfox, sleuth, hystrix, ribbon, zuul, netflix-feign, 옛 Spring Cloud train, spring-security-oauth2 레거시, log4j 1, commons-lang 2, httpclient 4, ibatis2, undertow(round 10, `guide`: `spring-boot-starter-undertow`는 tomcat starter로 재배치), `jdk-generified-apis`, `jdk-removed-apis`. round 10부터 `httpclient4` / `jackson1`은 Spring 프로젝트에서 `recipe`이고, Spring이 아닌 프로젝트에는 안내만 하는 `httpclient4-direct` / `jackson1-direct`가 붙습니다. 각각의 정책과 다음 할 일은 `docs/ERROR-HANDLING.md` §6과 `docs/ADVISORIES.md`. Handover의 "Libraries needing your attention" 표와 "From here on, your team" 목록에 나타납니다.

### 6.6 인프라·환경 문제(round 10)
빌드 수정 루프가 고칠 수 없는 인프라 문제는 일찍 멈추고 한 줄로 알립니다. 실행에는 `infra:<kind>` 플래그가 붙고(실패한 실행만), 플릿은 그 프로젝트를 다시 돌릴 대상(`pending`)으로 되돌립니다(7절).

| 증상(로그 줄) | 의미 | sbmig가 하는 일 | 운영자가 할 일 |
|---|---|---|---|
| `preflight: Maven runs on Java 17.0.9 (runtime …), not Java 21: set build.java_home (or JAVA_HOME) to a JDK 21 home and rerun` → `run aborted: …` | `mvn -v`가 21이 아닌 Java를 보고(`build.require_java_major`) | 빌드 없이 중단, 종료 코드 3, 플래그 `jdk-not-21` + `infra:jdk` | `build.java_home`에 JDK 21 경로(9.20). `JAVA_HOME` 때문에 `mvn`이 아예 안 뜨면 `mvn-not-found`도 "install Maven"이 아니라 JAVA_HOME을 가리킴 |
| `maven: negative cache hit for g:a:v → retrying once with -U` | Maven이 "… was cached in the local repository, resolution will not be reattempted …"(예전 실패를 로컬 저장소가 기억) | 같은 빌드를 `-U`로 한 번 더(아티팩트당 실행에 한 번, 오프라인·이미 `-U`면 안 함). 로그 `NNN-build-<label>-U.log`, 저널 `build_negative_cache_retry`(`build.retry_negative_cache`) | 보통 없음. 그래도 실패하면 Nexus에 정말 없는 것(X3) |
| `LLM configuration error: the gateway answered HTTP 400 '…' to 2 different prompts — no more LLM calls in this run` | 게이트웨이가 다른 프롬프트에도 같은 400 → 프롬프트가 아니라 설정(모델 이름, payload 필드) 문제 | 이 실행의 LLM 호출 중단, 플래그 `llm-config-error`(실행이 실패하면 `infra:llm`), follow-up 한 줄(400 본문 요약) | `.env`의 `LLM_MODEL` / `LLM_*_MODEL`, `llm.payload_extra` 확인 → `sbmig llm-probe`(9.22) |
| `… status=None cause=ConnectTimeout: …` (LLM 경고 / 재시도 / 포기 줄) | 게이트웨이에 닿지 못함(`[WinError N]` / `[Errno N]`도 그대로 표시) | 재시도 → 쿨다운 → unavailable(L5) | `cause=` 뒤의 클래스와 메시지를 그대로 옮겨 적음, 프록시 / `NO_PROXY` 확인 |
| `… finish_reason=length …` (빈 답) | 게이트웨이의 `max_tokens`에 걸려 답이 잘림 — 프롬프트가 너무 큼 | 그 요청의 거절로 기록(재시도·L5 집계 없음). 텍스트가 조금이라도 있으면 경고와 함께 사용 | 게이트웨이 `max_tokens`를 올리거나 `llm.max_context_chars`를 줄임 |
| 사내 부모를 못 받음(`aborted:parent-not-found`), 또는 최종 빌드 실패가 전부 해석 오류이고 `Could not transfer`, `Connection refused/reset`, `timed out`, `Unknown host`, `PKIX`, 401/403/5xx, "was cached in the local repository" | Nexus / 저장소 접속 문제(단순 `Could not find artifact`는 해당 안 됨) | 플래그 `infra:repository`(플릿은 그 프로젝트를 `pending`으로) | Nexus 상태, `build.settings_file` 미러, 인증서 |
| 최종 빌드에 `No space left on device` / `not enough space on the disk` / `디스크 공간이 부족` | 디스크 가득 | 플래그 `infra:disk`. 플릿은 프로젝트 전에 `fleet.min_free_gb`도 확인 | `sbmig clean`(7절), 오래된 `out\` 정리 |

## 7. 플릿 운영(수백 개 프로젝트를 한 에이전트로 순차 실행)
- **manifest**: 디렉터리(그 안의 모든 `*.zip`과 `pom.xml`이 있는 하위 디렉터리, 이름순), `.txt`(한 줄에 경로 하나, `#` 주석), `.yml`(`projects: [{name, input, configs, overrides, tags}]`), `.csv`(헤더 `name,input,tags`). 경로는 manifest 파일 기준 상대 경로. 예: `fixtures/fleet/manifest.example.yml`. round 10부터 manifest는 UTF-8(BOM 유무 무관), UTF-16(PowerShell `dir -Name > projects.txt`), cp949 어느 것으로 저장돼도 읽습니다. 한글 등 ASCII가 아닌 이름이나 서로 겹치는 이름은 실행 디렉터리 이름에 ASCII 슬러그 + 입력 경로의 6자 해시를 씁니다(순수 ASCII 이름은 그대로). 상태는 **입력 경로**(Windows는 대소문자 무시)로 찾으므로 폴더 이름을 바꾸거나 추가해도 다른 프로젝트의 성공을 물려받지 않습니다.
- **엄격히 순차**: 병렬 실행은 없습니다. 프로젝트 k의 수정이 KB에 수확된 뒤 k+1이 시작하므로 뒤 프로젝트일수록 LLM 호출이 줄어듭니다.
- **실행/재개**:
```powershell
.venv\Scripts\sbmig.exe fleet run D:\fleet\projects.yml --config config\artifacts.yml --out out\fleet
.venv\Scripts\sbmig.exe fleet status out\fleet
.venv\Scripts\sbmig.exe fleet resume out\fleet                 # 같은 manifest·설정으로 이어서
.venv\Scripts\sbmig.exe fleet resume out\fleet --retry-failed  # incomplete / aborted도 다시
.venv\Scripts\sbmig.exe fleet run D:\fleet\projects.yml --config config\artifacts.yml --out out\fleet --dry-run
```
- **재개 규칙**: `success`는 다시 돌리지 않음, `incomplete`/`aborted`는 `--retry-failed`(또는 `fleet.retry_failed`)일 때만, 입력이 없던 `skipped`는 입력이 생기면 다음 재개 때 실행, Gradle/Ant 거부로 `skipped`된 것은 계속 skipped(`--retry-failed`로도 안 돎, `--stop-on-abort`도 멈추지 않음), 플릿 프로세스가 죽어 `running`으로 남은 것은 다시 실행, 인프라 이유로 `pending`(`error: infra:<kind>`)이 된 것은 그냥 다시 실행하면 돎. 재시도는 새 run id(`…-r2`).
- **사전 점검(preflight, round 10)**: `fleet.preflight: true`(기본)이면 첫 프로젝트 전에 한 번, 그리고 인프라 대기 뒤 다시 점검할 때마다 점검 하나에 한 줄씩 찍습니다. 형식은 `preflight: <점검> ok|FAIL|skip <내용>`입니다. **`FAIL` 줄 하나를 그대로 옮겨 적으면 됩니다.**

```text
preflight: disk       ok   212.4 GB free on D:\sbmig\out (min 5)
preflight: maven      ok   Apache Maven 3.9.9 (…)
preflight: java       FAIL 17 but build.require_java_major is 21 (Java version: 17.0.9 …); set build.java_home / JAVA_HOME to a JDK 21
preflight: settings   ok   D:\sbmig\config\settings.nexus.xml (mirror external:* -> http://<nexus>/repository/public/)
preflight: repository ok   http://<nexus>/repository/public/ HTTP 200
preflight: llm        ok   <모델> HTTP 200 1.3s
```

  `disk`는 `fleet.min_free_gb`, `maven` / `java`는 `mvn -v`와 `build.require_java_major`, `settings`는 `build.settings_file`이 있고 읽히는지와 catch-all 미러, `repository`는 그 미러 URL에 Maven처럼 GET 한 번(settings의 `<proxy>`는 쓰고 셸의 `HTTP_PROXY`는 무시, 500 미만이면 살아 있음, Python이 사내 CA를 몰라 TLS 검증만 실패해도 살아 있음), `llm`은 `sbmig llm-probe`와 같은 호출 한 번(`fake` 제공자나 `loop.max_llm_calls: 0`이면 `skip`)입니다.
- **인프라 차단기(round 10)**: 프로젝트가 `infra:<kind>`로 끝나거나 인프라 예외(디스크 가득, LLM 요청 오류, mvn 사라짐)로 죽으면 실패로 세지 않고 `pending`(`error: infra:<kind>`)으로 되돌립니다. 그런 프로젝트가 연속 `fleet.infra_failure_limit`(3)개면 다음 줄을 찍고 `fleet.infra_retry_minutes`(15)분 기다린 뒤 사전 점검을 다시 하고, 통과하면 미뤘던 프로젝트부터(프로세스당 각 최대 2번) 이어 갑니다.

```text
fleet: infrastructure llm unavailable since order-api - waiting 15 min, then re-checking (1/4)
fleet: infrastructure checks passed - resuming
fleet stopped: infrastructure llm unavailable since order-api - fix it and run sbmig fleet again
```

  `fleet.infra_max_waits`(4)번 기다려도 안 되거나, `infra_retry_minutes: 0`이거나, 다시 점검한 결과가 설정 오류(Maven 없음, JDK가 21 아님, settings 파일 없음, LLM 키 / 모델 거부)이면 더 기다리지 않고 `fleet stopped: …` 한 줄로 멈춥니다(상태 저장, 종료 코드 3, 콘솔 마지막 줄에 한 번 더). 첫 사전 점검이 설정 오류면 첫 프로젝트 전에 바로 멈춥니다. 원인을 고친 뒤 **같은 명령을 다시 실행하면** `pending` 프로젝트부터 이어서 돕니다.
- **디스크(round 10)**: 프로젝트마다 시작 전에 출력 드라이브의 남은 공간을 봅니다(`fleet.min_free_gb`, 기본 5, 0 = 확인 안 함). 모자라면 `disk` 인프라 문제로 위 차단기를 탑니다. 프로젝트가 끝나면(보고서와 `result.zip`을 쓴 뒤) `fleet.cleanup`이 정리합니다: `build-output`(기본) = 작업 사본 안에서 `pom.xml` 옆의 `target/`만 삭제(`target`이라는 Java 패키지는 건드리지 않음), `workspace` = `result.zip`이 있을 때 `project/` 작업 사본(git 이력 포함)까지 삭제(`result.zip`, `report.*`, `changes.patch`, `journal.jsonl`, `sbmig.log`, `logs/`는 남김), `none` = 정리 안 함.
- **쌓인 것 지우기 `sbmig clean`**: 끝난 실행의 작업 사본(`<run dir>/project`), 끝난 웹 실행의 업로드 zip, 하루 지난 `.part` 업로드, LLM 기록 `llm-NNNN.json`을 지웁니다. 실행 중 / 대기 중인 실행은 건드리지 않습니다. 종류별 개수와 크기를 찍고 `would free …`(`--dry-run`) 또는 `freed …`로 끝납니다.

```powershell
.venv\Scripts\sbmig.exe clean --fleet out\fleet --older-than 7 --keep-failed --dry-run   # 무엇이 지워질지만
.venv\Scripts\sbmig.exe clean --fleet out\fleet --older-than 7 --keep-failed             # 실제로 지움
.venv\Scripts\sbmig.exe clean --out out\web --older-than 3                               # 웹 실행 + 업로드
```

- **요약 파일**(프로젝트마다 다시 씀): `<out>/fleet/state.json`, `summary.md|csv|json`(advisories, carried_over 열 포함, "Libraries flagged across the fleet"), `learning.md`(LLM이 2회 이상 고쳤는데 레시피가 없는 심볼 = 레시피/핸들러를 만들 후보, 가장 많은 프로젝트를 막는 아티팩트 = 라이브러리 담당에게 보낼 표, 권고, 효과 본 KB 레시피), `fleet-config.json`, `sbmig.log`(INFO). round 10: `summary.csv`는 UTF-8 BOM(엑셀에서 한글 경로가 바로 보임)이고 `attention`(3.4의 주의 코드)과 `input` 열이 있으며, `summary.md`에 "Needs attention" 절(첫 코드별 묶음)이 생겼습니다. 파일은 하나씩 따로 쓰므로 엑셀이 `summary.csv`를 잡고 있어도 나머지는 갱신되고 로그에 한 줄만 남습니다.
- **사람이 볼 프로젝트만**: `sbmig fleet status out\fleet --attention` → `fleet <id>: N of M project(s) need attention` 다음에 첫 주의 코드별로 `<코드> (<수>)`와 `<이름>  <상태>  <run id>  <코드들>  <입력 경로>` 줄(코드당 50개까지, 나머지는 `summary.csv`의 `attention` 열).
- **프로젝트별 로그(round 10)**: 프로젝트마다 `<out>/<fleet id>-<NNN>-<이름>/sbmig.log`(DEBUG)를 그 프로젝트 동안만 붙입니다. `fleet/sbmig.log`는 INFO(진행 줄, `preflight:`, 차단기 줄, `SBMIG-RESULT`)입니다.
- **종료 코드**: 0 전부 성공, 2 일부 미완료/skipped/미실행, 3 `--stop-on-abort`로 정지 또는 인프라 차단기가 멈춤(`fleet stopped: …`), 1 설정·manifest 오류.
- **KB 공유**: 프로젝트별 `configs`/`overrides`는 플릿 설정 위에 병합되지만 `kb.dir`은 항상 플릿 공통입니다. round 10부터 모든 KB 쓰기가 프로세스 간 잠금 `<kb.dir>/.lock` 안에서 일어나므로 웹 서버·플릿·`sbmig kb …` 명령이 같은 `kb/`를 써도 파일이 깨지지 않습니다(잠금은 `kb.lock_timeout_seconds`(300초)가 지나거나 그 pid가 없으면 낡은 것으로 보고 풀며, 그때까지 기다린 뒤에는 경고를 남기고 진행). 다만 학습 순서가 섞이지 않게 플릿은 하나만 돌리십시오.
- **조절**: `fleet.order`(`simple-first`로 쉬운 것부터), `fleet.max_llm_calls_total`, `fleet.max_projects`, `fleet.stop_on_abort`, `fleet.reindex_every`, round 10: `fleet.preflight`, `fleet.infra_failure_limit`, `fleet.infra_retry_minutes`, `fleet.infra_max_waits`, `fleet.cleanup`, `fleet.min_free_gb`([08](08-config-reference.md) 2.11).

```powershell
Select-String -Path out\fleet\fleet\sbmig.log -Pattern "preflight: .* FAIL|fleet stopped:|fleet: infrastructure" -Encoding UTF8 | Select-Object -Last 3
Select-String -Path out\fleet\fleet\sbmig.log -Pattern "SBMIG-RESULT" -Encoding UTF8 | Select-Object -Last 5
.venv\Scripts\sbmig.exe fleet status out\fleet --attention
```

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
| `<kb.dir>/errors.jsonl`, `fix_attempts.jsonl`, `error_hints.json` | 빌드 에러 하나 / 수정 시도 하나마다 한 줄(실패 포함), 그 힌트 색인 |
| `<kb.dir>/catalog/`, `<kb.dir>/views/` | 케이스 카탈로그(에러 종류 하나 = 케이스 하나)와 사람용 목록 |
| `<kb.dir>/audit.jsonl` | round 10: 사람이 `sbmig kb cases …`로 고친 것(OS 사용자, 명령, 전 → 후)과 케이스 상태 / 동작의 자동 전환("fallen back: …") |
| `<kb.dir>/quarantine.jsonl` | round 10: 읽을 수 없던 `errors.jsonl` 줄의 기록(경고와 함께). 그 L2의 케이스는 `rebuild`가 지우지 않음 |
| `<kb.dir>/*.jsonl.corrupt` | round 10: 추가 전용 파일 끝의 잘린 줄(프로세스가 쓰다 죽음)을 다음 쓰기 전에 옮겨 둔 것(경고). 온전한 줄만 KB로 셈 |
| `<kb.dir>/.lock` | round 10: 프로세스 간 잠금(pid / host / 시각 / 토큰). 쓰는 동안만 있음 |
| `kb.bak-<시각>/` (KB 옆) | `sbmig kb import --replace`가 바꾸기 전 KB를 남긴 것 |

**round 10 결정(2026-10-26): KB 형식이 바뀌었으니 회사 KB는 지우고 다시 쌓습니다.** 에러 시그니처(L2 = 케이스 키)가 더 잘게 나뉘고 기록에 `schema_version` 등이 붙어, 예전 KB의 케이스·힌트는 새 실행과 맞지 않습니다. 아직 운영 단계가 아니므로 변환하지 않고 지운 뒤 새로 쌓습니다. 필요하면 지우기 전에 `sbmig kb export`로 보관만 해 둡니다. 사람이 쓴 `kb\patterns\`와 `.gitkeep`은 남깁니다. 회사 설정의 `kb.dir`이 저장소 `kb\`가 아니면 아래 경로를 그 디렉터리로 바꾸십시오.

```powershell
.venv\Scripts\sbmig.exe kb --config config\artifacts.yml export D:\backup\kb-before-round10.zip   # 선택: 보관만
Remove-Item -Recurse -Force kb\cases\*, kb\recipes\active\*, kb\recipes\candidates\*, kb\recipes\rejected\*, kb\catalog, kb\views -ErrorAction SilentlyContinue
Remove-Item -Force kb\*.jsonl, kb\*.json, kb\*.corrupt -ErrorAction SilentlyContinue
git status --short kb                                                                         # .gitkeep 파일이 지워졌으면 git checkout -- kb
```

운영 습관:
1. **후보 검토**: `sbmig kb list --status candidate` → `sbmig kb show <id>`로 YAML 확인 → `sbmig kb promote <id>` 또는 `sbmig kb reject <id> --reason "…"`. 자동 승격은 서로 다른 `kb.auto_promote_after_projects`(기본 2)개 프로젝트에서 실패 없이 성공했을 때. 수동 검토만 원하면 0.
2. **문제 레시피 의심 시**: 해당 레시피를 `reject`하거나, 시험 실행만 `--no-kb`로 돌려 비교합니다(round 10부터 `--no-kb`는 케이스 카탈로그, 학습된 치환, 에러 힌트, 계획의 후보 레시피, ledger, 심볼 색인, 결정적 후보까지 끕니다. 실행 디렉터리의 `error_cases.*`는 그대로 씀). 케이스가 의심스러우면 `sbmig kb cases reject <id> --reason "…"` — round 10부터 그 L2의 KB 패치 재생, KB 레시피, 심볼 힌트도 멈춥니다. 사람이 고정한 동작(`sbmig kb cases pin <id> --action handler:<id>|block[:<사유>]|recipe:<id>`)은 이제 트리아지가 그대로 따릅니다([04](04-triage-and-loop.md) 4.7).
3. **손으로 고친 뒤**: `sbmig kb reindex`(index.json + symbols.json 재생성). 플릿은 `fleet.reindex_every`마다 자동.
4. **정리(prune)**: 자동 삭제 명령은 없습니다. ledger는 추가 전용이 원칙입니다. 커지면 `sbmig kb export`로 보관한 뒤 정리 여부를 개발자와 정합니다(코드 기준: `sbmig/kb/store.py`).
5. **백업/이동**: `sbmig kb export kb-YYYYMMDD.zip` → 다른 PC에서 `sbmig kb import kb-YYYYMMDD.zip`(기본 병합, `--replace`는 교체: 숨은 옆 디렉터리에서 새 KB를 다 만든 뒤 이름 바꾸기로 교체하고 예전 KB는 `kb.bak-<시각>`으로 남김). round 10부터 저장소 `.gitignore`가 KB 데이터 파일(`<kb.dir>/errors.jsonl`, `<kb.dir>/ledger.jsonl`, `<kb.dir>/catalog/` 등 — 저장소 `kb/` 기준, `.gitkeep`만 추적)을 무시하므로 git 커밋이 아니라 export zip으로 백업합니다. 플릿 시작 전과 매일 끝에 export를 권장합니다.
6. **오염 방지**: 테스트·데모 실행은 `out/` 아래 KB를 씁니다(`config/artifacts.local-e2e.yml`의 `kb.dir: out/e2e/kb`). 회사 실험 실행도 `--no-kb` 또는 별도 `--config`로 `kb.dir`을 바꾸십시오.
7. **배운 것 보기**: `sbmig kb learning --md`, `sbmig kb ledger --resolution blocked`, `sbmig kb symbols`, `sbmig kb errors list --case <id 앞부분>`, `sbmig kb errors list --resolver "case-replace"`(한 케이스 / 한 해결 수단의 기록만, round 10).
8. **비밀값(round 10)**: KB에 들어가는 모든 것(diff, 원문, 메시지)은 값만 가립니다(`password=***`, `<password>`, JDBC `user/pass@`, `Authorization: Bearer`, PEM 키 …, `${…}` / `__SET_ME__`는 그대로). 회사 고유 패턴(사내 토큰 형식, OTP 키 이름 등)은 `knowledge/kb/redact.yml`(`kb.redact_file`)의 `key_words` / `key_patterns` / `patterns` / `allow_keys`에 **이름과 패턴만** 추가합니다. 가린 값이 걸린 치환 쌍은 버리고, 그런 해결은 규칙 후보가 되지 않으며(노트 `redacted: …`), 그런 KB 패치 케이스는 재생하지 않고 힌트로만 씁니다(태그 `no-replay:redacted`).

## 9. FAQ — 회사 실행(2026-10-21~)과 round 10 점검 줄에서 나온 것

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

### 9.18 `llm company HTTP 400: … "#…" is an unsupported model` — 잘 되다가 중간에 한 번
`.env`의 역할 모델 줄을 값 없이 주석만 남겨 두면(`LLM_RECIPE_MODEL=   # 비우면 LLM_MODEL`, `LLM_ESCALATION_MODEL= #model`) 2026-10-23 이전 파서는 **주석 텍스트를 값으로** 읽었습니다. 그 모델은 실행 중간에 처음 쓰이므로(레시피 모델: 첫 KB 수확 / 플랜 조언, 상향 모델: 정체 `loop.stall_limit` 이후 또는 verify 루프) 그때 게이트웨이가 400을 돌려주고, 400은 요청의 잘못이라 L5에 세지 않아 루프는 계속됩니다. 지금은 주석만 있는 값은 빈 값이고, 설정을 읽을 때 모든 모델 이름에서 `# …` 꼬리와 공백을 떼어 냅니다(`llm.recipe_model=' # …' is not a plain model name: using None` 경고). 확인: `sbmig.log`에서 400 바로 앞의 `llm request role=… model=…` 줄, 또는 `.env`의 `LLM_*_MODEL` 줄이 `KEY=이름   # 주석` 꼴인지.

### 9.19 `could not collect dependencies for project …-back … failed to read artifact descriptor …WEB:war … From/to central`
APP/BATCH, APP/WEB, APP/EAR처럼 **루트 pom 없이 형제 pom만 있고 EAR이 WAR를 묶는** 프로젝트에서 2026-10-23 이전에 나던 오류입니다. `APP/EAR`을 프로젝트로 넣으면 EAR pom이 프로젝트 전체로 취급되어, EAR의 `<type>war</type>` 의존성(WEB 모듈)이 back(jar) 모듈로 복사되고 Maven이 그 war를 central에서 찾습니다. 화면은 WEB 모듈에 있으므로 front도 생기지 않습니다. 지금은 이렇게 동작합니다.
- `APP/EAR`만 넣으면 intake에서 `ear-without-modules`로 중단하고 "모든 모듈이 들어 있는 상위 디렉터리로 다시 실행"을 안내합니다.
- 루트(APP·DB·STATIC이 보이는 폴더)를 넣으면 intake가 루트 집합 pom을 만들고(`aggregator-generated`) 세 모듈을 한 번에 빌드한 뒤 back / front / boot로 병합합니다. EAR 모듈은 없애고(`ear-module-dropped`, `contextRoot`는 후속 조치), pom이 참조하지 않는 `STATIC/` 같은 폴더는 그대로 둡니다(`web-content-left`).
- 다른 이유로 jar 모듈에 `<type>war</type>` 의존성(war overlay)이 있으면 boot WAR로 옮깁니다(`war-overlay-moved`). overlay war는 사내 Nexus(`build.settings_file`)에서 받을 수 있어야 합니다.

### 9.20 `run aborted: Maven runs on Java 17.0.9 (…), not Java 21` / `preflight: java FAIL 17 but build.require_java_major is 21`
Maven이 JDK 21이 아닌 Java로 돌고 있습니다(대개 `JAVA_HOME`이 JDK 17 / 8). 이 상태에서는 모든 빌드가 수정 루프로 고칠 수 없는 이유로 실패하므로, round 10부터 첫 빌드 전에 중단합니다(종료 코드 3, 플래그 `jdk-not-21`, `infra:jdk`). 괄호 안의 `runtime …` / `build.java_home=…` / `JAVA_HOME=…`가 Maven이 실제로 쓴 Java입니다. `config\artifacts.yml`의 `build.java_home`을 JDK 21 경로로 지정하고 다시 실행합니다. 확인:
```powershell
& "C:\Program Files\Java\jdk-21\bin\java.exe" -version
$env:JAVA_HOME = "C:\Program Files\Java\jdk-21"; mvn -v | Select-String "Java version"
```
검사를 끄려면 `build.require_java_major: 0`(권장하지 않음).

### 9.21 `fleet: infrastructure <kind> unavailable since <project> - waiting 15 min …` / `fleet stopped: infrastructure …`
플릿의 인프라 차단기입니다(7절). 프로젝트 3개가 연달아 인프라 이유(`infra:<kind>`)로 끝나 플릿이 기다리는 중이거나 멈춘 것입니다. 실패한 프로젝트는 `pending`으로 남아 있어 잃은 것은 없습니다. `<kind>`(`llm` / `maven` / `jdk` / `repository` / `disk`)를 고친 뒤 같은 `sbmig fleet run …`(또는 `sbmig fleet resume out\fleet`)을 다시 실행합니다. 원인을 모르면 `Select-String -Path out\fleet\fleet\sbmig.log -Pattern "preflight: .* FAIL" -Encoding UTF8 | Select-Object -Last 1`의 결과 한 줄을 옮겨 적어 주십시오.

### 9.22 `LLM configuration error: the gateway answered HTTP 400 '…' to 2 different prompts — no more LLM calls in this run`
게이트웨이가 서로 다른 프롬프트 두 개에 **같은** 400 본문을 돌려줬습니다. 프롬프트 탓이 아니라 설정 탓(지원하지 않는 모델 이름, payload 필드)이므로, 같은 400으로 예산을 다 쓰지 않도록 이 실행의 LLM 호출을 멈춥니다(플래그 `llm-config-error`, follow-up 한 줄). 핸들러·레시피는 계속 돌고, LLM이 필요한 클러스터는 `llm_unavailable`로 blocked됩니다. 본문이 "too long / context length / max tokens" 같은 크기 문제이면 설정 오류로 보지 않습니다. 확인: `.env`의 `LLM_MODEL` / `LLM_RECIPE_MODEL` / `LLM_ESCALATION_MODEL`(9.18), 그다음 `sbmig llm-probe --model <모델>`의 마지막 `LLM-PROBE …` 줄.

### 9.23 `maven: negative cache hit for <g:a:v> → retrying once with -U`
정상입니다. 예전에 Nexus가 잠깐 죽었을 때의 실패를 로컬 저장소가 기억해("… was cached in the local repository, resolution will not be reattempted until the update interval …") Nexus가 살아난 뒤에도 같은 실패가 나던 경우입니다. 같은 빌드를 `-U`로 한 번 더 돌리고(실행 안에서 아티팩트당 한 번), 그 로그는 `logs\NNN-build-<label>-U.log`입니다. 끄려면 `build.retry_negative_cache: false`. `-U` 뒤에도 실패하면 정말 없는 아티팩트(X3)입니다.

### 9.24 LLM 줄의 `cause=…`, `finish_reason=length`
- `status=None cause=ConnectTimeout: …`, `cause=ProxyError: …`, `cause=SSLError: …`: HTTP 응답 자체가 없었습니다(전송 실패, 재시도 → 쿨다운 → L5). `cause=` 뒤 클래스와 120자 이내 메시지(`[WinError 10061]` 등 포함)를 그대로 옮겨 적어 주십시오.
- `finish_reason=length`(빈 답): 게이트웨이의 출력 토큰 한도에 걸린 것, 즉 프롬프트가 너무 큰 것입니다. 재시도하지 않고 그 요청의 거절로 기록합니다(L5에 세지 않음). 게이트웨이 `max_tokens`를 올리거나 `llm.max_context_chars`를 줄입니다. 답이 잘렸지만 텍스트가 있으면 경고와 함께 그대로 씁니다.
- `sbmig llm-probe`는 마지막에 `LLM-PROBE ok …` / `LLM-PROBE FAIL <kind> provider=… model=… status=… <문제>` 한 줄을 찍습니다(종료 코드 0 / 1). 이 줄 하나면 충분합니다.

### 9.25 `.venv\Scripts\Activate.ps1 cannot be loaded because running scripts is disabled on this system`
PowerShell 실행 정책입니다. 그 창에서만 허용하고 다시 활성화합니다: `Set-ExecutionPolicy -Scope Process Bypass` → `.venv\Scripts\Activate.ps1`. 활성화 없이 `.venv\Scripts\sbmig.exe …`로 바로 실행해도 됩니다.

### 9.26 PowerShell에서 로그의 `—`와 한글이 깨져 보인다
로그 파일은 UTF-8인데 Windows PowerShell 5.1의 `Get-Content` / `Select-String`이 기본 인코딩으로 읽은 것입니다. `-Encoding UTF8`을 붙입니다(`Get-Content "$run\sbmig.log" -Encoding UTF8 -Tail 30`). 콘솔에 바로 찍히는 sbmig 출력은 round 10부터 cp949 콘솔에서도 죽지 않고, 쓸 수 없는 문자만 `?`로 나옵니다.

### 9.27 같은 오류가 여러 파일에 있는데 첫 라운드에 몇 개가 `blocked: repeated_failure`
round 10 이전 동작입니다. 파일 5개에 같은 `cannot find symbol`이 있으면 앞 파일의 시도가 뒤 파일의 시도 수로 잡혀 3개만 LLM을 부르고 2개는 첫 라운드에 blocked됐습니다. 지금은 같은 클러스터의 채점된 실패만 세고, 다른 파일의 시도는 "this run: the same error (…) in … was fixed by …" 힌트로만 프롬프트에 들어갑니다(6.3 L4). 이 증상이 보이면 예전 체크아웃입니다.

## 10. 지원 요청 시 보낼 것
회사 PC는 폐쇄망이고 로그를 손으로 옮겨 적어야 하므로 **최소한만** 보냅니다.

| 우선순위 | 무엇 | 어떻게 얻나 |
|---|---|---|
| 1 | `SBMIG-RESULT …` 한 줄(run id, 종료 코드, 빌드 결과, 남은 수, 주의 코드가 다 들어 있음) | 콘솔 마지막 줄, 또는 아래 `Select-String` |
| 2 | 플릿이면 `preflight: … FAIL …` 줄 또는 `fleet stopped: …` 줄 | `fleet\sbmig.log`(7절 명령) |
| 3 | 중단(exit 3)이면 `run aborted: …` 줄, 아니면 report.md의 Blocked 첫 행 또는 Unresolved 첫 항목 | `sbmig.log` / report.md |
| 4 | 멈춤이면 세 줄: watchdog 줄(`fix loop quiet for Ns since '<milestone>'`), `first error:` 줄, 스레드 덤프의 `--- thread sbmig-run-…`(CLI는 `MainThread`) 블록 맨 아래 15줄 | 4.4의 PowerShell 명령 |
| 5 | LLM 문제면 `LLM-PROBE …` 줄, `cause=` / `finish_reason=length`가 든 줄, `LLM configuration error` 줄 | `sbmig llm-probe`, 아래 `Select-String` |
| 6 | 개발자가 특정 줄을 더 물으면 그 `Select-String` 명령의 결과 | 개발자가 명령을 함께 보냅니다 |

로그 파일 통째나 `llm-NNNN.json`을 옮겨 적을 필요는 없습니다. 위 줄들로 시작하고, 더 필요한 줄은 개발자가 PowerShell 명령으로 지정합니다.

```powershell
$run = "out\run-20261026-101500-ab12cd"
Select-String -Path "$run\sbmig.log" -Pattern "SBMIG-RESULT|run aborted:" -Encoding UTF8 | Select-Object -Last 2
Select-String -Path "$run\sbmig.log" -Pattern "cause=|finish_reason=length|LLM configuration error" -Encoding UTF8 | Select-Object -Last 2
.venv\Scripts\sbmig.exe llm-probe | Select-Object -Last 1
```

API 키는 어떤 로그에도 남지 않지만(`llm-probe` 출력도 마스킹) 옮겨 적을 때 `.env` 내용은 포함하지 마십시오.

## 11. 서버 종료가 멈출 때 (2026-10-21 추가)

`sbmig serve`를 Ctrl+C로 끄면 uvicorn은 열린 연결이 닫히길 기다립니다. 브라우저 탭이 실행 중인 run의 이벤트 스트림(SSE)을 붙들고 있으면
예전에는 `Waiting for connections to close`에서 영원히 멈췄고, Windows에서는 `mvn.cmd`가 띄운 `java.exe`가 서버가 죽은 뒤에도 살아
남아 `target/`을 잠근 채 CPU를 썼습니다. 지금은 종료 시 (1) 열린 스트림을 `end`(reason `server shutdown`)로 끝내고, (2) 실행 중인 run에
정지를 요청하고, (3) 아직 도는 Maven/OpenRewrite 프로세스 트리를 `taskkill /T /F`로 죽입니다. uvicorn이 연결을 기다리는 시간은
`sbmig serve --graceful-timeout 5`(기본 5초)로 제한됩니다. `uvicorn sbmig.web.app:app …`으로 직접 띄울 때는
`--timeout-graceful-shutdown 5`를 꼭 붙이십시오. 종료로 끊긴 run은 리포트에 `aborted`로 남고 최종 빌드는 없습니다.
그래도 남는 프로세스가 있으면 PowerShell에서 `tasklist | findstr java`로 확인하고 `taskkill /PID <pid> /T /F`로 정리합니다.
`sbmig serve` 창의 Ctrl+Break도 uvicorn이 받아 서버를 끕니다. 서버가 도는 중에 스레드 덤프가 필요하면 `dump-threads` 파일(4.3)이나 `/api/threads`를 쓰십시오.
