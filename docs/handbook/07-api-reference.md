# 07. API 레퍼런스 — CLI · REST · Python API · 플러그인 계약

이 문서는 `sbmig`를 바깥에서 부르는 네 가지 길을 빠짐없이 정리한 레퍼런스입니다. (1) 명령행 `sbmig`의 모든 명령과 옵션(`typer` 앱을 코드에서 직접 열거해 확인), (2) 웹 UI가 쓰는 REST/SSE API(`sbmig/web/app.py`, `sbmig/web/runs.py`), (3) 사내 에이전트 프레임워크가 직접 호출할 Python API `sbmig.api.migrate`, (4) 사내 LLM 게이트웨이·코딩 에이전트를 끼우는 플러그인 계약(tier 1 `llm.plugin`, tier 2 `fix.plugin`). 끝으로 환경 변수 목록과 `report.json` 스키마, 플래그 어휘, `journal.jsonl` 이벤트 어휘, ledger 레코드 필드를 정리합니다. 설정 키 하나하나의 의미는 [08. 설정 레퍼런스](08-config-reference.md), 운영 절차는 [09. 운영과 진단](09-operations.md)을 보십시오.

## 이 문서를 읽고 나면
- 모든 `sbmig` 명령의 인자·옵션·기본값·종료 코드와 남기는 파일을 찾아볼 수 있습니다.
- `curl.exe` / `Invoke-RestMethod`로 웹 서버에 마이그레이션을 올리고, SSE 이벤트를 받고, 중단하고, 결과 파일을 내려받을 수 있습니다.
- 사내 에이전트 코드에서 `migrate(...)`를 호출하고 `exit_code(report)`로 결과를 판정할 수 있습니다.
- tier 1 어댑터(`complete(LLMRequest) -> LLMResponse`)와 tier 2 fixer(`propose(ws, cluster, ctx) -> FixProposal`)를 계약대로 구현할 수 있습니다.
- `report.json`의 필드와 플래그·저널 이벤트의 뜻을 읽을 수 있습니다.

## 관련 코드
- `sbmig/cli.py` — 최상위 명령, `run_exit_code`, `attach_file_log`
- `sbmig/kb/cli.py`, `sbmig/plan/catalog_cli.py`, `sbmig/fleet/cli.py`, `sbmig/advisory/cli.py` — 하위 명령 그룹
- `sbmig/loop/orchestrator.py` — `parse_stop_after`, 플래그, 저널 이벤트 대부분
- `sbmig/web/app.py`, `sbmig/web/runs.py`, `sbmig/web/static/index.html` — REST/SSE
- `sbmig/api.py` — `migrate`, `exit_code`
- `sbmig/core/interfaces.py`, `sbmig/core/models.py`, `sbmig/fix/protocol.py`, `sbmig/llm/registry.py`, `sbmig/llm/adapter.py`
- `examples/adapters/company_gateway_adapter.py`, `examples/adapters/agent_fixer.py`, `config/llm-plugin.example.yml`
- `sbmig/core/config.py` — `.env` 로딩, `SBMIG_*` 덮어쓰기
- `sbmig/kb/ledger.py` — ledger 레코드
- round 10: `sbmig/report/result_line.py` — `SBMIG-RESULT` 줄과 주의 코드, `sbmig/fleet/preflight.py` — 플릿 사전 점검, `sbmig/fleet/cleanup.py` — `sbmig clean` / `fleet.cleanup`

## 1. CLI

### 1.1 공통 사항
- 설치 후 실행 파일은 가상환경의 `sbmig`입니다(Linux/macOS `.venv/bin/sbmig`, Windows `.venv\Scripts\sbmig.exe`). 가상환경을 활성화했다면 그냥 `sbmig`.
- 최상위 옵션(`sbmig` 바로 뒤, 명령 이름 **앞**에 씁니다):

| 옵션 | 타입 | 기본값 | 의미 |
|---|---|---|---|
| `--verbose`, `-v` | 플래그 | 꺼짐 | 콘솔 로그를 DEBUG로. 파일 로그는 원래 DEBUG입니다. |
| `--log-file` | 경로 | 없음 | 이 명령의 DEBUG 로그를 해당 파일에 **추가**로 기록. `sbmig run`은 이것과 무관하게 항상 실행 디렉터리의 `sbmig.log`에 기록합니다. |

예: `sbmig --log-file out/analyze.log analyze project.zip --config config/artifacts.example.yml`.

- `--config` / `-c`: 반복 가능, 지정 순서대로 병합(나중 파일 우선). 대부분의 명령은 회사 artifacts 섹션이 있는 파일(직접 만든 config/artifacts.yml)이 **필수**입니다. 없으면 `config error: N problem(s) in the merged configuration`, `artifacts: Field required`, 힌트를 출력하고 종료 코드 2. 예외: `llm-probe`, `kb`, `recipes`, `advisories`는 없어도 동작합니다.
- 설정 병합 우선순위는 [08. 설정 레퍼런스](08-config-reference.md) 1절. CLI 플래그는 overrides 단계이며 `SBMIG_*` 환경 변수가 CLI보다 나중에 적용된다는 점에 주의하십시오.
- 하위 그룹(`kb`, `recipes`, `fleet`, `advisories`)은 해당 패키지 import에 실패해도 `sbmig --help`가 깨지지 않도록 지연 로딩됩니다. 실패하면 그 명령을 부를 때 `… is not available: <오류>`로 종료 코드 1.
- 콘솔(stdout / stderr)과 로그 핸들러는 round 10부터 출력 인코딩으로 쓸 수 없는 문자를 바꿔 씁니다(`errors="replace"`). cp949 콘솔에서 출력을 파일로 돌려도 `UnicodeEncodeError`로 죽지 않습니다. 옮겨 적을 줄(`SBMIG-RESULT`, `LLM-PROBE`, `preflight:`)은 전부 ASCII입니다.
- Ctrl+Break(Windows) / `kill -USR1`(POSIX)은 스레드 덤프를 `out/threads-<ts>.txt`에 씁니다. 시작 디렉터리나 최근 실행 디렉터리에 `dump-threads` 파일을 만들어도 2초 안에 그 자리에 덤프가 생깁니다([09](09-operations.md) 4.3).

### 1.2 `parse_stop_after` — `--stop-after` 값
CLI(`--stop-after`), 웹(`stop_after` 쿼리), Python API(`stop_after=`)가 공유하는 함수(`sbmig/loop/orchestrator.py`)입니다. 대소문자를 구분하지 않고, 빈 값/None은 "끝까지 실행"입니다.

| 값 | 여기까지 실행하고 멈춤 |
|---|---|
| `intake` | 입력 복사/압축 해제, 빌드 도구 판정(Maven 아니면 여기서 중단) |
| `analyze` | 프로파일 분석(`profile.json`), 라이브러리 권고 탐지 |
| `plan` | 계획 수립(실행 디렉터리의 plan.json) |
| `jdk21` | JDK 21 레시피 + build-fix 루프 |
| `spring` | jakarta / Boot 4.1 / Framework 7 레시피 + 루프(`plan.parent_phase: spring`이면 부모/레이아웃 적용 포함) |
| `restructure` | 회사 레이아웃·부모·필수/옵션 의존성 + 루프 |
| `verify` | 최종 `mvn clean package`까지(= 사실상 전체) |

다른 값은 `stop_after must be one of: intake, analyze, plan, jdk21, spring, restructure, verify` 오류입니다. 멈춘 실행에도 보고서는 항상 쓰이며 플래그 `stopped-after-<phase>`가 붙습니다.

### 1.3 `sbmig run` — 한 프로젝트 마이그레이션
목적: 입력(디렉터리 또는 zip)을 `<out>/<run id>/project`로 복사해 전체 파이프라인을 돌리고 보고서를 씁니다. 입력 원본은 절대 수정하지 않습니다.

```text
sbmig run INPUT [--config FILE]... [--out DIR] [--run-id ID] [--stop-after PHASE] [--dry-run] [--no-kb]
          [--llm-provider NAME] [--llm-plugin SPEC] [--fix-plugin SPEC] [--java-home DIR] [--settings FILE]
          [--repo-root DIR] [-v]
```

| 인자/옵션 | 타입 | 기본값 | 의미 |
|---|---|---|---|
| `INPUT` | 경로(존재해야 함) | 필수 | as-is 프로젝트 디렉터리 또는 zip. zip 안에 최상위 폴더가 하나면 그 안으로 내려가 프로젝트 루트를 찾습니다. |
| `--config`, `-c` | 경로, 반복 | 없음 | 설정 파일(회사 artifacts 필수). |
| `--out`, `-o` | 경로 | `out` | 실행 디렉터리의 상위. 결과는 `<out>/<run id>/`. |
| `--run-id` | 문자열 | 자동 `run-YYYYMMDD-HHMMSS-xxxxxx` | 실행 id(디렉터리 이름). 이미 있으면 오류. |
| `--stop-after` | 문자열 | 없음 | 1.2의 값. |
| `--dry-run` | 플래그 | 꺼짐 | 분석·계획·결정만 계산해 저널에 남기고 아무것도 적용하지 않음(플래그 `dry-run`). |
| `--no-kb` | 플래그 | 꺼짐 | KB를 쓰지도 채우지도 않음(`kb.harvest`, `kb.use_active_recipes`, `kb.use_patch_replay`, `kb.use_hints`, `kb.generate_recipe_candidates`를 모두 false). round 10부터 `kb.catalog`, `kb.case_replace`, `kb.error_hints`, `kb.try_candidates_in_plan`, `kb.ledger`, `kb.symbol_index`, `kb.deterministic_candidates`도 끕니다(`sbmig.api`의 `NO_KB_OVERRIDES`, `migrate(kb=False)`와 같음). `kb.error_cases`는 켠 채라 실행 디렉터리의 `error_cases.*`는 그대로 생깁니다. |
| `--llm-provider` | 문자열 | 설정값 | `company`, `openai_compatible`, `anthropic`, `fake`, `plugin` 또는 진입점 이름 → `llm.provider`. |
| `--llm-plugin` | `pkg.module:attr` | 없음 | tier 1 어댑터. `--llm-provider plugin`을 함께 의미합니다 → `llm.plugin`. |
| `--fix-plugin` | `pkg.module:attr` | 없음 | tier 2 fixer → `fix.plugin`. |
| `--java-home` | 문자열 | 설정값 | JDK 21 경로 → `build.java_home`. |
| `--settings` | 문자열 | 설정값 | Maven settings.xml → `build.settings_file`. |
| `--repo-root` | 경로 | sbmig 저장소 | `kb/`, `knowledge/`의 기준 → `repo_root`. |
| `--verbose`, `-v` | 플래그 | 꺼짐 | 이 명령만 콘솔 DEBUG. |

종료 코드(`run_exit_code`, `sbmig/cli.py`):

| 코드 | 의미 |
|---|---|
| 0 | 최종 `mvn clean package` 성공이고 blocked 항목이 없음. |
| 2 | 끝까지 실행했지만 blocked 그리고/또는 unresolved 오류가 남음(또는 `restructure-failed`, stop-after/dry-run처럼 최종 빌드 성공이 없는 경우). `report.md`의 Blocked / Unresolved 절을 보십시오. |
| 3 | 중단: 플래그 `aborted` 또는 `aborted:…`(예: `aborted:parent-not-found`) 또는 `mvn-not-found` / `jdk-not-21`. Maven 없음, Maven이 Java 21이 아님(`build.require_java_major`, round 10), Maven 프로젝트 아님(`unsupported-build-tool:<gradle/ant/none>`), 사내 부모 해석 불가, 내부 오류, 웹에서 사용자 중단. |

라이브러리 권고(advisory)와 carried-over 고정은 종료 코드를 바꾸지 않습니다. 실행 끝에 콘솔 요약(결과는 `BUILD SUCCESS` / `NOT BUILDING`, 단계 표는 `ok` / `FAIL`), `log: <경로>   (share this file + report.md when asking for help)` 줄, 그리고 **마지막 줄로 한 줄 결과**를 출력합니다(round 10, 같은 줄이 `sbmig.log`에도 남음).

```text
SBMIG-RESULT run=<id> exit=<n> build=<ok|FAIL> unresolved=<n> blocked=<n> llm=<calls> attn=<codes|->
```

`attn`은 주의 코드(`infra:<kind>`, `crash`, `aborted[:<사유>]`, `blocked:<사유>`, `unresolved:<n>`, `restructure-failed`, `llm-budget-exhausted`, `time-budget-exhausted`, `javax-api-on-classpath`, `tests-skipped`, `test-compile-skipped`, `tests-not-compiled`, `incomplete`; 플릿은 `skipped:<도구|input>`도)를 중요한 순서로 쉼표로 이은 것입니다. 예외로 죽으면 traceback 뒤에 `SBMIG-RESULT run=<id> exit=1 build=FAIL … attn=crash`. 코드별 뜻은 [09](09-operations.md) 3.4.

남기는 파일(`<out>/<run id>/`): `project/`(git 브랜치 `sbmig/<run id>`, 단계·시도마다 커밋, 루트 `.gitignore`에 sbmig 줄 추가 — [05](05-directories-and-storage.md) 5.2), `sbmig.log`, `journal.jsonl`, `logs/NNN-<label>.log`(빌드·rewrite 로그), `profile.json`, plan.json, report.md, report.json, `changes.patch`, `result.zip`, `error_cases.jsonl`, `fix_attempts.jsonl`, `error_cases.csv`, `effective/`(effective-pom 캡처: `depmgmt.json`, `depmgmt-after-restructure.json`), `rewrite/`, `classpath/`, 필요 시 `merge-conflicts/`, `threads-<ts>.txt`. 각 파일의 용도는 [05. 디렉터리와 저장소](05-directories-and-storage.md).

```bash
# Linux / macOS
sbmig run ~/work/order-api.zip --config config/artifacts.yml --out out
sbmig run ~/work/order-api --config config/artifacts.yml --stop-after spring --java-home /usr/lib/jvm/java-21
```
```powershell
# Windows PowerShell
.venv\Scripts\sbmig.exe run D:\asis\order-api.zip --config config\artifacts.yml --out out
.venv\Scripts\sbmig.exe run D:\asis\order-api --config config\artifacts.yml --dry-run
echo $LASTEXITCODE
```

### 1.4 `sbmig serve` — 웹 UI 서버
목적: FastAPI 앱을 uvicorn으로 띄웁니다. 업로드한 zip을 서버 프로세스 안에서(실행당 워커 스레드 1개, LangGraph 엔진) 마이그레이션하고 SSE로 진행을 보여 줍니다.

```text
sbmig serve [--host ADDR] [--port N] [--out DIR] [--repo-root DIR] [--graceful-timeout N] [--max-upload-mb N] [--token T]
```

| 옵션 | 타입 | 기본값 | 의미 |
|---|---|---|---|
| `--host` | 문자열 | `127.0.0.1` | 바인드 주소. LAN에 공유하려면 `0.0.0.0` — **토큰이 있어야 함**(round 10). 토큰 없이 루프백이 아닌 주소면 `refusing to serve on <host> without a token …`을 찍고 종료 코드 2. |
| `--token` | 문자열 | `$SBMIG_WEB_TOKEN`(`.env`에서도 읽음) | 접근 토큰(round 10). 있으면 모든 요청에 필요: 브라우저는 처음 한 번 `http://<host>:<port>/?token=<토큰>`(HttpOnly·SameSite=Strict 쿠키에 토큰의 해시를 7일 저장하고 `/`로 다시 엶), API 클라이언트는 `Authorization: Bearer <토큰>`(또는 `?token=` 한 번). 없으면 루프백(127.0.0.1 / ::1) 클라이언트만 받고 LAN은 403. 화면은 401 / 403에서 안내를 보여 줍니다. |
| `--port` | 정수 | `8765` | 포트. |
| `--out`, `-o` | 경로 | `out/web` | 업로드(`<out>/_uploads/`)와 실행 디렉터리(`<out>/<run id>/`) 위치. |
| `--repo-root` | 경로 | sbmig 저장소 | 체크박스로 보여 줄 `config/*.yml`과 `kb/`의 기준. |
| `--graceful-timeout` | 정수 | `5` | 종료 시 열린 SSE 연결을 기다리는 초([09. 운영](09-operations.md) 11장). |
| `--max-upload-mb` | 정수 | `$SBMIG_MAX_UPLOAD_MB` 또는 `512` | 받아 주는 zip의 최대 크기(MB). `0` = 한도 없음. 큰 프로젝트 하나만 올릴 때 `--max-upload-mb 0`으로 다시 띄웁니다(코드 수정 불필요). |

서버 전체 로그는 `<out>/sbmig-web.log`, 실행별 로그는 `<out>/<run id>/sbmig.log`. 시작 시 같은 앱을 순수 uvicorn으로 띄우는 명령을 함께 출력합니다:

```bash
SBMIG_WEB_OUT=out/web SBMIG_WEB_REPO_ROOT=/path/to/migration-agent SBMIG_WEB_TOKEN=<긴 임의 값> \
  uvicorn sbmig.web.app:app --host 0.0.0.0 --port 8765 --timeout-graceful-shutdown 5
```
```powershell
$env:SBMIG_WEB_OUT = "out/web"; $env:SBMIG_WEB_REPO_ROOT = "D:\migration-agent"
.venv\Scripts\uvicorn.exe sbmig.web.app:app --host 127.0.0.1 --port 8765 --timeout-graceful-shutdown 5
# LAN에 공유: 토큰 필수 (.env에 SBMIG_WEB_TOKEN=… 를 두거나)
.venv\Scripts\sbmig.exe serve --host 0.0.0.0 --port 8765 --token <긴 임의 값>
```

순수 uvicorn으로 `--host 0.0.0.0`을 줘도 토큰이 없으면 이 PC가 아닌 접속은 403입니다(앱이 직접 막음).

`sbmig serve`는 `--config`를 받지 않습니다. 설정은 실행마다 웹 화면(또는 REST의 `configs`)에서 `config/` 안의 파일 이름으로 고릅니다. `build.java_home`, `build.settings_file` 같은 값은 그 yml 파일 안에 있어야 합니다(웹에는 `--java-home` 같은 덮어쓰기가 없음). 서버도 CLI와 같은 환경(`.env`, `JAVA21_HOME` 등)을 읽습니다 — `.env`는 서버가 뜰 때 한 번(`SBMIG_MAX_UPLOAD_MB`, `SBMIG_WEB_TOKEN`, 순수 uvicorn의 `SBMIG_WEB_*`) 그리고 실행마다(LLM 변수) 읽습니다. round 10부터 실행마다 다시 읽을 때 바뀐 `.env`가 실제로 반영됩니다(셸 변수는 여전히 우선, 5절). 종료 코드: Ctrl+C로 끝낼 때까지 실행.

### 1.5 `sbmig llm-probe` — LLM 연결 점검
목적: 설정된 제공자로 작은 요청 하나를 보내고, 실제로 보낸 요청(키는 마스킹), 프록시, 응답 헤더/본문, 추출된 답을 출력합니다. 새 게이트웨이에서 가장 먼저 실행합니다. `--config` 없이 `.env`만으로 동작합니다(artifacts를 임시값으로 채움).

```text
sbmig llm-probe [--config FILE]... [--prompt TEXT] [--llm-provider NAME] [--model NAME]
                [--payload-extra JSON] [--no-system] [--bare]
```

| 옵션 | 타입 | 기본값 | 의미 |
|---|---|---|---|
| `--config`, `-c` | 경로, 반복 | 없음 | 선택. LLM 섹션을 yml로 바꿀 때만. |
| `--prompt` | 문자열 | `Reply with the single word: pong` | 보낼 사용자 메시지. |
| `--llm-provider` | 문자열 | 설정값 | 제공자 덮어쓰기(`fake`면 게이트웨이 없이 동작 확인). |
| `--model` | 문자열 | `LLM_MODEL` | 이번 점검에서만 `llm.model` 덮어쓰기. |
| `--payload-extra` | JSON 객체 문자열 | 없음 | `llm.payload_extra`를 **교체**(병합 아님). `'{}'` = model + messages만 보냄, `'{"need_origin": false}'`. JSON 객체가 아니면 종료 코드 2. |
| `--no-system` | 플래그 | 꺼짐 | system 메시지 없이 user 메시지만. |
| `--bare` | 플래그 | 꺼짐 | sbmig 클라이언트를 거치지 않고 같은 페이로드를 순수 `requests.post(url, headers={Authorization}, json=payload, timeout=)`로 보냄. 게이트웨이 문제와 클라이언트 문제를 구분할 때. |

출력: `provider=… model=… url=… key_env=… payload_extra=… response_path=…`, `request payload: …`, `sent: POST <url>`, `sent headers: …`(Authorization은 앞 10자 + 길이만), `sent body: …`, `proxies in effect for this url: …  (NO_PROXY=…)`, `response headers: …`, `HTTP <code>`, 응답 JSON, `extracted reply: '…'`. 응답 텍스트를 못 찾으면 `PROBLEM: …`와 `auto-detect paths tried: …`를 출력하므로 `.env`의 `LLM_RESPONSE_PATH`를 JSON 위 점 경로로 설정합니다.

마지막 줄(round 10)은 옮겨 적기 쉬운 ASCII 한 줄입니다. 플릿 사전 점검의 `llm` 점검도 같은 호출(`probe_llm`)을 씁니다.

```text
LLM-PROBE ok provider=company model=<모델> status=200 latency=1.3s reply='pong'
LLM-PROBE FAIL <kind> provider=company model=<모델> status=<코드|-> <문제 요약>
```

`<kind>`는 `config`(URL / 키 / 모델 없음 등), `transport`(HTTP 응답 없음 — 문제에 `cause=<클래스>: <메시지>`), `http`(HTTP 오류 상태와 본문 앞부분), `reply`(JSON이 아니거나 답 텍스트를 못 찾음 → `LLM_RESPONSE_PATH`), `rejected`(콘텐츠 필터, 빈 답 + `finish_reason=length` = 프롬프트가 너무 큼 / 게이트웨이 max_tokens를 올리라는 힌트) 중 하나입니다.

종료 코드: 0 성공, 1 실패(`CONFIG ERROR`(URL/키/모델 없음), `TRANSPORT ERROR`, HTTP ≥ 300, JSON 아님, 텍스트 추출 실패), 2 설정 오류/잘못된 `--payload-extra`.

```bash
sbmig llm-probe --prompt "안녕"
sbmig llm-probe --prompt "안녕" --payload-extra '{}' --no-system --model gpt-6-luna
sbmig llm-probe --bare
```
```powershell
.venv\Scripts\sbmig.exe llm-probe --prompt "안녕"
.venv\Scripts\sbmig.exe llm-probe --payload-extra '{\"need_origin\": true}' --no-system
```
PowerShell에서 JSON 안의 큰따옴표는 `\"`로 이스케이프해야 네이티브 프로그램에 그대로 전달됩니다(PowerShell 버전에 따라 다름 — 안 되면 `'{}'`처럼 따옴표 없는 값으로 먼저 확인하십시오).

### 1.6 `sbmig analyze` — 분석만(변경 없음)
```text
sbmig analyze INPUT [--config FILE]... [--json]
```
| 인자/옵션 | 기본값 | 의미 |
|---|---|---|
| `INPUT` | 필수 | 디렉터리(복사하지 않고 그 자리에서 읽기만) 또는 zip(임시 디렉터리에 해제). |
| `--config`, `-c` | 없음 | 회사 artifacts 필수. |
| `--json` | 꺼짐 | 프로파일 전체(`ProjectProfile`)를 JSON으로 출력. |

출력: `<이름>: case <케이스> (confidence 0.xx)`, java/boot/spring 버전, pom 수, 빌드 도구, 판정 근거, 라이브러리, 서비스(MSA), 경고, 그리고 매칭된 권고 `advisory [guide|recipe] <id>@<버전> — <제목> → <대체>`. 종료 코드 0(설정 오류 2). 아무것도 바꾸지 않는 스모크 테스트로 씁니다.

### 1.7 `sbmig plan` — 분석 + 계획(변경 없음)
```text
sbmig plan INPUT [--config FILE]... [--json]
```
출력: 단계 목록(`id`, 종류, 빌드 goal, 대상, 레시피 id), `pre-emptive handlers: …`, 계획 노트. KB를 열 수 있으면 KB 레시피도 계획에 반영합니다. `--json`은 `MigrationPlan` 전체. 종료 코드 0.

### 1.8 `sbmig build` — Maven 실행 + 오류 파싱(디버깅)
```text
sbmig build PROJECT_DIR [--goal GOAL] [--config FILE]... [--out DIR]
```
| 인자/옵션 | 기본값 | 의미 |
|---|---|---|
| `PROJECT_DIR` | 필수(디렉터리) | 그 자리에서 Maven 실행(복사하지 않음). |
| `--goal`, `-g` | `compile` | Maven goal(예: `"clean package"`). |
| `--out` | 임시 디렉터리 | 로그 위치. |

출력: `BUILD SUCCESS|BUILD FAILURE (Ns) log: <경로>`와 오류 표(kind, 위치, 메시지, L1 시그니처). 종료 코드 0 성공 / 2 실패. 설정의 `build.*`(JDK, settings, extra_args)가 그대로 적용됩니다.

### 1.9 `sbmig parse-log` — 기존 Maven 로그 파싱
```text
sbmig parse-log LOGFILE [--root DIR] [--json]
```
`--root`(기본 `.`)는 경로를 상대화할 프로젝트 루트. 설정 파일이 필요 없습니다. 출력은 오류 표 또는 `no errors found`, `--json`은 `BuildError` 목록. KB 패턴(`kb/patterns`)을 만들 때 시그니처를 확인하는 용도입니다. 종료 코드 0.

```powershell
.venv\Scripts\sbmig.exe parse-log out\run-20261021-101500-ab12cd\logs\012-build-spring-it2.log --root out\run-20261021-101500-ab12cd\project
```

### 1.10 `sbmig rewrite` — 레시피 단독 실행
```text
sbmig rewrite PROJECT_DIR --recipe ID [--recipe ID]... [--dry-run] [--config FILE]... [--out DIR]
```
**`--dry-run`이 없으면 디렉터리를 그 자리에서 수정합니다.** 레시피 id는 카탈로그로 해석됩니다. 출력: `ok|FAILED: N file(s) changed  log: …`, 바뀐 파일(최대 50개), `error: …`. 종료 코드 0 / 2.

### 1.11 `sbmig version`
`sbmig <버전>` 출력. 종료 코드 0.

### 1.12 `sbmig kb` — 지식 베이스
그룹 옵션(하위 명령 **앞**): `--config`/`-c`(반복, artifacts 불필요), `--kb-dir`(경로, `kb.dir` 덮어쓰기). 예: `sbmig kb --kb-dir out/e2e/kb stats`.

| 명령 | 인자/옵션 | 목적 | 종료 코드 |
|---|---|---|---|
| `sbmig kb list` | `--status`/`-s` (`active`/`candidate`/`rejected`), `--cases`, `--json` | 레시피(기본) 또는 케이스 목록 | 0 |
| `sbmig kb show ID` | 케이스 id 또는 레시피 id | 케이스/레시피 상세(YAML 포함) | 0 / 1(없음) |
| `sbmig kb promote ID` | 레시피 id | 후보(또는 rejected)를 active로 | 0 / 1 |
| `sbmig kb reject ID` | `--reason`/`-r` | 레시피를 rejected로 | 0 / 1 |
| `sbmig kb reindex` | — | `index.json`, `symbols.json`을 케이스/레시피 파일에서, `error_hints.json`을 `errors.jsonl` / `fix_attempts.jsonl`에서 재생성 | 0 |
| `sbmig kb stats` | `--json` | 케이스 수, 상태별 레시피 수, 패턴 수, 색인 시그니처 수, `error_cases` / `fix_attempts` 줄 수, 카탈로그 케이스 수와 상태별 수(`catalog_cases`, `catalog_observed` / `candidate` / `active` / `rejected`) | 0 |
| `sbmig kb ledger` | `--project`, `--symbol`, `--label`, `--resolution`, `--signature`(모두 정규식), `--limit`/`-n`(기본 50, 0 = 전부), `--json`(한 줄에 한 레코드) | ledger 조회. `--resolution` 값: `handler`, `plan_recipe`, `kb_recipe`, `kb_patch`, `llm_patch`, `optional_bump`, `blocked`, `unresolved`, `advisory` | 0 |
| `sbmig kb symbols` | `--json`, `--symbol`(정규식) | 심볼 색인 | 0 |
| `sbmig kb learning` | `--md`, `--json`, `--top`(기본 15) | 배운 것 / 아직 LLM이 필요한 것 / 가장 많이 막는 것 | 0 |
| `sbmig kb export OUT.zip` | — | KB를 zip으로(`errors.jsonl`, `fix_attempts.jsonl`, `error_hints.json` 포함) | 0 |
| `sbmig kb import IN.zip` | `--replace` | zip 가져오기(기본 병합). 에러·시도 기록은 없는 `id`만 추가하고 힌트 색인을 다시 만듭니다(출력 `error_cases_added`, `fix_attempts_added`). 새 파일이 없는 옛 zip도 됩니다. `--replace`는 round 10부터 KB 옆 숨은 디렉터리에서 새 KB를 다 만든 뒤 이름 바꾸기로 교체하고 예전 KB를 `<kb.dir>.bak-<시각>`으로 남깁니다(중간에 죽어도 반쯤 지운 KB가 없음) | 0 |
| `sbmig kb match SIGNATURE` | `--project`/`-p` 디렉터리 | 디버그: 이 시그니처(L1/L2)에 KB가 무엇을 돌려줄지 | 0 |
| `sbmig kb validate RECIPE.yml` | — | 선언형 레시피가 KB 화이트리스트를 지키는지 | 0 / 1 |
| `sbmig kb errors list` | `--status`, `--method`, `--change-kind`, `--project`, `--symbol`, `--signature`(모두 정규식), `--run`(실행 id 일치), round 10: `--case`(카탈로그 케이스 id 또는 앞부분), `--resolver`(해결 수단 id 정규식: 핸들러 / 레시피 / 모델 / `case-replace:<케이스>:<쌍 해시>`), `--limit`/`-n`(기본 50, 0 = 전부, 마지막 N개), `--json`(한 줄에 한 레코드) | 에러 기록 목록: 상태, 방법, 변경 형태, 프로젝트, `파일:줄`, 메시지, 어떻게 해결 / 왜 못 했나 | 0 |
| `sbmig kb errors show ID` | 에러 id(유일한 앞부분도 됨) 또는 시도 id, `--json`(원본 기록), `--all-diffs`(실패한 시도의 diff까지) | 읽기 쉬운 화면: 에러, 그 케이스, 시도 이력과 실패 이유, 해결한 diff(여러 시도가 함께 해결했으면 `← part of the resolution` 표시와 그 시도들의 diff 전부, 실행 순서) | 0 / 1(없음) |
| `sbmig kb errors summary` | `--run`(반복), `--top`(기본 15), `--json` | 상태 / 방법 / 변경 형태별 수, 상태 × 방법 표, 자주 나온 실패 이유, 남은 L2 | 0 |
| `sbmig kb errors replace-candidates` | `--min-projects`(기본 1), `--limit`/`-n`(기본 50), `--json` | LLM이 고쳤지만 단순 텍스트 치환이었던 수정을 L2 + 치환 쌍으로 묶어 프로젝트 수 순으로 — 규칙(명령) 후보. round 10: 비밀값을 가린(`***`) 쌍은 나오지 않고, 점으로 이은 이름은 패키지 단위 쌍(`javax.xml.bind → jakarta.xml.bind`) | 0 |
| `sbmig kb cases list` | `--status`(observed / candidate / active / rejected), `--action`(hint / recipe / replace / handler / patch / block), `--kind`(에러 종류 / 분류), `--search`(id, 이름, 시그니처, 심볼, 소유 클래스) — 모두 정규식, `--limit`/`-n`(기본 0 = 전부), `--json` | 케이스 목록(프로젝트 수 순). `*`는 사람이 정한 값 | 0 |
| `sbmig kb cases show KEY` | id, id 앞부분, L2 / L1 시그니처 또는 심볼, `--json`(원본 케이스), `--no-diff` | 케이스 보고서: 이름, 키와 변형, 실제 적용되는 상태 / 동작(사람이 바꿨으면 자동값도), 근거 수, 타임라인(실패한 시도 → 수정), 성공 / 실패 방법, 힌트, 마지막 성공 수정의 diff(여러 시도로 해결했으면 그 시도들의 diff 전부), round 10: "history" 절(`<kb.dir>/audit.jsonl`의 사람 편집과 자동 전환) | 0 / 1 |
| `sbmig kb cases reject ID` | `--reason`/`-r` | 상태를 `rejected`로(다시 맞지 않음), 이유는 메모에. round 10: 그 L2의 KB 패치 재생, KB 레시피, 심볼 힌트도 멈춤 | 0 / 1 |
| `sbmig kb cases pin ID` | `--action`/`-a` `recipe:<id>[,…]` / `replace:<old>=<new>[,…]` / `handler:<id>` / `hint` / `block[:<사유>]` | 사람이 동작을 고정하고 상태를 `active`로. round 10부터 트리아지가 따름: `block` → LLM 없이 BLOCKED(`BlockReason` 값이 아닌 사유는 `external_api_incompatible` + 그 문장을 제안으로), `handler` → 그 핸들러를 먼저(등록되지 않은 id는 노트), `recipe` → KB 레시피 결정. 여럿이면 block → handler → recipe([04](04-triage-and-loop.md) 4.7). 편집은 `audit.jsonl`에 남음 | 0 / 1 |
| `sbmig kb cases unpin ID` | — | 고정한 동작과 상태를 지움(거부도 풀림) | 0 / 1 |
| `sbmig kb cases hint ID TEXT` / `note ID TEXT` | — | 사람 힌트(프롬프트에 먼저 들어감) / 메모 추가 | 0 / 1 |
| `sbmig kb errors export` | `--csv PATH`, `--attempts-csv PATH`, `list`와 같은 필터(`--case`, `--resolver` 포함) | 엑셀용 CSV(UTF-8 BOM). 필터를 주면 시도 CSV에는 고른 에러의 시도(이력과 해결에 들어간 시도 전부)만. 칸 앞의 `'`는 정말 수식이 될 칸에만(`=` / `@`로 시작, 또는 `+` / `-`로 시작하며 `( ) | ! = @` 포함) — `-source 8`은 그대로. 열 목록은 [05](05-directories-and-storage.md) 4.10b | 0 |

```bash
sbmig kb list --status candidate
sbmig kb ledger --resolution blocked --json
sbmig kb export kb-20261021.zip
sbmig kb errors summary                                   # 상태 × 방법, 자주 실패한 이유
sbmig kb errors list --status "unresolved|blocked" --method llm
sbmig kb errors replace-candidates --min-projects 2        # 규칙으로 바꿀 LLM 치환
sbmig kb errors export --csv errors.csv --attempts-csv attempts.csv
sbmig kb errors list --case 8bb4fd4a                          # 한 케이스의 기록만(round 10)
sbmig kb errors list --resolver "^case-replace" --status resolved   # 학습된 치환이 고친 것
sbmig kb cases list --status "candidate|active"                # 자동 적용되는 케이스
sbmig kb cases show compile|cannot_find_symbol|clientIp        # 실패 → 성공 과정과 diff
sbmig kb cases pin 8bb4fd4a4a --action "replace:clientIp=remoteClientIp"
```

### 1.13 `sbmig recipes` — 레시피 카탈로그(`knowledge/recipes/catalog.yml`)
모든 하위 명령이 `--config`/`-c`(artifacts 불필요)와 `--catalog FILE`(기본 `plan.catalog_file`)을 받습니다.

| 명령 | 옵션 | 목적 | 종료 코드 |
|---|---|---|---|
| `sbmig recipes list` | `--kb`(KB 레시피 포함), `--json` | 카탈로그 전체(출처, 단계, 아티팩트/파일, 게이트, advisor, 세트, 규칙) | 0 / 2(카탈로그 없음) |
| `sbmig recipes show RECIPE_ID` | `--json` | 레시피 하나와 그것을 참조하는 설정 세트·반응 규칙·합성 레시피, KB 메타 | 0 / 1 |
| `sbmig recipes check` | `--jars`(Maven + 네트워크로 실제 jar 확인), `--work-dir`(기본 `out/recipes-check`), `--json`, `--artifact g:a`(반복) | 설정 세트·반응 규칙·base 레시피의 모든 id가 카탈로그에 있는지(불변식 5). `--jars`는 `openrewrite.recipe_artifacts`를 BOM 버전으로 해석해 id를 jar에서 확인 | 0 / 1(문제) |
| `sbmig recipes sets` | `--json` | 케이스별로 규칙 기반 planner가 해석한 설정 레시피 세트 | 0 |

### 1.14 `sbmig fleet` — 여러 프로젝트 순차 실행
| 명령 | 인자/옵션 | 목적 |
|---|---|---|
| `sbmig fleet run MANIFEST` | `--out`/`-o`(기본 `out`), `--config`/`-c`(반복), `--order` (`manifest`/`simple-first`), `--max-projects N`, `--retry-failed/--no-retry-failed`, `--stop-on-abort/--no-stop-on-abort`, `--max-llm-calls-total N`(0 = 무제한), `--dry-run`(순서와 resume 판단만 출력), `--llm-provider` | manifest(디렉터리 / `.txt` / `.yml` / `.csv`)의 프로젝트를 하나씩 순서대로, 하나의 KB로. 같은 `--out`으로 다시 실행하면 이어서 진행 |
| `sbmig fleet resume OUT` | `--config`(기본: 지난 실행의 설정), `--max-projects`, `--retry-failed/--no-retry-failed`, `--stop-on-abort/--no-stop-on-abort` | `<OUT>/fleet/fleet-config.json`의 manifest·설정·overrides로 이어서 실행 |
| `sbmig fleet status [OUT]` | `--json`, `--attention`(round 10), OUT 기본 `out` | `<OUT>/fleet/state.json`의 진행 표. `--attention`이면 사람이 볼 프로젝트만 첫 주의 코드별로: `fleet <id>: N of M project(s) need attention`, `<코드> (<수>)`, `  <이름>  <상태>  <run id>  <코드들>  <입력>`(코드당 50개, 나머지는 `summary.csv`의 `attention` 열) |

종료 코드: `fleet run`/`resume` = 0 모든 프로젝트 성공, 2 일부 incomplete/skipped/아직 미실행, 3 `--stop-on-abort`로 중단 프로젝트에서 정지 **또는 인프라 차단기가 멈춤**(round 10: `fleet stopped: infrastructure <kind> unavailable since <project> - fix it and run sbmig fleet again`, 상태 저장, 같은 명령으로 재개), 1 잘못된 설정·manifest 오류(`invalid configuration` / `fleet failed`). `resume`/`status`는 상태 파일이 없으면 1(`no fleet state in <OUT>`). 남기는 파일: `<OUT>/fleet/state.json`, `summary.md|csv|json`(round 10: csv는 UTF-8 BOM + `attention` / `input` 열, md에 "Needs attention" 절), `learning.md`, `fleet-config.json`, `sbmig.log`(INFO), 프로젝트별 `<OUT>/<fleet id>-<NNN>-<name>/`(재시도는 `-r2`…, round 10: 그 프로젝트의 DEBUG `sbmig.log` 포함).

round 10의 플릿 출력 줄(콘솔과 `fleet/sbmig.log`): 첫 프로젝트 전 `preflight: <disk|maven|java|settings|repository|llm> ok|FAIL|skip <내용>`(`fleet.preflight`), 프로젝트마다 `[i/N] <이름>: running (<run id>)` → `SBMIG-RESULT …` → `[i/N] <이름>: <상태> (exit …)`, 인프라 대기 `fleet: infrastructure <kind> unavailable since <project> - waiting N min, then re-checking (i/max)`, 재개 `fleet: infrastructure checks passed - resuming`. 설정 키는 [08](08-config-reference.md) 2.11, 운영 방법은 [09. 운영과 진단](09-operations.md) 7절.

### 1.15 `sbmig advisories` — 라이브러리 권고(`knowledge/libraries/advisories.yml`)
| 명령 | 옵션 | 목적 | 종료 코드 |
|---|---|---|---|
| `sbmig advisories list` | `--config`, `--json` | id, 라이브러리, 상태, 실효 정책(덮어쓰기 반영), 대체, 레시피 | 0 |
| `sbmig advisories show ADVISORY_ID` | `--config`, `--json` | 권고 하나와 실효 정책 | 0 / 1 |
| `sbmig advisories check` | `--config` | 권고 파일을 분석기 규칙과 레시피 카탈로그에 대조 | 0(`advisories OK: …`) / 1 |

### 1.16 `sbmig inventory` — pom 없는 웹앱 인벤토리(읽기 전용)
```text
sbmig inventory INPUT [--out DIR] [--config FILE]... [--json]
```
| 인자/옵션 | 기본값 | 의미 |
|---|---|---|
| `INPUT` | 필수 | pom 이 없는 Tomcat 웹앱 디렉터리(그 자리에서 읽기만) 또는 그 zip(임시 디렉터리에 해제 후 삭제). |
| `--out`, `-o` | `out/inventory` | `inventory.md` / `inventory.json` 을 쓸 디렉터리. |
| `--config`, `-c` | 없음 | 선택. `report.language`(`en` / `ko`)만 읽습니다. 없으면 artifacts 없이 동작합니다. |
| `--json` | 꺼짐 | 콘솔 요약 대신 `WebappInventory` 전체 JSON 을 출력. |

출력(12줄 이내): `inventory: <루트>`, 웹앱 루트별 JSP 수·Servlet 스펙·프레임워크, 소스 루트별 `.java` 수·인코딩, 클래스 파일의 Java 레벨, jar 좌표 확인 / 미확인 / 무시 수와 미확인 jar 앞 3개, 소스 없는 클래스 수, 중복 클래스 수, 모듈 제안, 빌드 파일, 경고 수, 산출물 경로. 아무것도 바꾸지 않으며 종료 코드는 항상 0(경로가 없으면 3). jar 좌표 표는 `knowledge/libraries/jar_names.yml`. 분석기는 `sbmig.analyze.scan_webapp_tree(root)`, 작성기는 `sbmig.report.inventory.write_inventory(inv, out_dir, cfg)` 입니다.

### 1.17 `sbmig clean` — 끝난 실행 정리(round 10)
```text
sbmig clean [--out DIR] [--fleet DIR] [--older-than DAYS] [--keep-failed] [--dry-run] [--records DIR]... [--config FILE]...
```
| 옵션 | 기본값 | 의미 |
|---|---|---|
| `--out`, `-o` | `out` | `sbmig run` / `sbmig serve`의 `--out`(예: `out/web`) |
| `--fleet` | 없음 | 플릿의 `--out` 디렉터리: 그 플릿의 실행 사본만 |
| `--older-than` | `0` | 이 일수보다 전에 끝난 것만(0 = 끝난 실행 전부) |
| `--keep-failed` | 꺼짐 | 초록이 아닌(종료 코드 0이 아닌) 실행은 남김 |
| `--dry-run` | 꺼짐 | 지울 것만 보여 줌(앞 20개 + `would free …`) |
| `--records` | `llm.record_dir` / `LLM_RECORD_DIR` | LLM 기록 디렉터리(반복 가능) |
| `--config`, `-c` | 없음 | `llm.record_dir`을 읽을 설정(artifacts 불필요) |

지우는 것: 끝난 실행의 작업 사본 `<run dir>/project`(작업 트리 + git 이력; `result.zip`, `report.md` / `.json`, `changes.patch`, `logs/`는 남김), 끝난 웹 실행의 업로드 zip(`<out>/_uploads/`), 하루 지난 `.part` 업로드, LLM 기록 `llm-NNNN.json`. 실행 중 / 대기 중인 실행은 건드리지 않습니다. 출력: `clean: <디렉터리> (<범위>)`, 종류별 `  project-copy  <개수>  <크기>`(`upload`, `partial-upload`, `llm-record`), 마지막 `would free …` 또는 `freed … (N item(s))`. 없는 디렉터리면 종료 코드 2, 그 밖은 0. 플릿은 프로젝트마다 `fleet.cleanup`으로 바로 정리할 수도 있습니다([08](08-config-reference.md) 2.11).

```powershell
.venv\Scripts\sbmig.exe clean --fleet out\fleet --older-than 7 --keep-failed --dry-run
.venv\Scripts\sbmig.exe clean --out out\web --older-than 3
```

## 2. REST API(웹 서버)

### 2.1 개요
- 기본 주소 `http://127.0.0.1:8765`. 오류 응답은 모두 `{"error": "<메시지>"}` JSON입니다.
- **접근(round 10)**: 토큰이 없으면 루프백 클라이언트만 받습니다(LAN 클라이언트는 403과 안내 — 순수 `uvicorn --host 0.0.0.0`이어도). 토큰(`SBMIG_WEB_TOKEN` / `sbmig serve --token`)이 있으면 모든 요청에 `Authorization: Bearer <토큰>` 또는 쿠키가 필요합니다(없거나 틀리면 401). `?token=<토큰>`을 한 번 붙이면 HttpOnly·SameSite=Strict 쿠키(토큰의 해시, 7일)를 받고, 브라우저 페이지는 토큰을 뺀 주소로 다시 열립니다. 거절 응답은 다른 오류와 같은 `{"error": "<안내>"}` JSON이고, `/` 페이지 요청이면 같은 안내를 담은 HTML입니다.
- 한 번에 한 마이그레이션만 실행합니다. 파일 기반 KB를 공유하므로 이후 업로드는 `queued` 상태로 기다렸다가 순서대로 시작합니다(`RunManager._run_lock`).
- run id 형식: `web-YYYYMMDD-HHMMSS-xxxxxx`. 경로의 run id는 `^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$`가 아니면 400 `bad run id`.

### 2.2 라우트 요약
| 메서드 | 경로 | 목적 | 성공 응답 |
|---|---|---|---|
| GET | `/`, `/index.html` | 단일 페이지 UI(`sbmig/web/static/index.html`, 캐시 금지) | 200 HTML |
| GET | `/static/*` | 정적 파일 | 200 |
| GET | `/api/configs` | 체크 가능한 설정 파일 | 200 `{"configs": [...], "repo_root": "...", "out_dir": "...", "max_upload_mb": 512}` |
| GET | `/api/runs` | 알려진 실행 목록(최신 순) | 200 `{"runs": [status, ...]}` |
| GET | `/api/threads` | 모든 스레드의 스택(진단) | 200 text/plain |
| POST | `/api/runs` | zip 업로드 + 실행 시작 | 202 `{"run_id": "...", "status": {...}}` |
| GET | `/api/runs/{run_id}` | 상태 | 200, 없으면 404 |
| GET | `/api/runs/{run_id}/events` | SSE 스트림 | 200 text/event-stream, 없으면 404 |
| POST | `/api/runs/{run_id}/stop` | 현재 그래프 노드 이후 중단 요청 | 200 `{"stopped": true|false}` |
| GET | `/api/runs/{run_id}/files/{name}` | 결과 파일 | 200 파일, 없으면 404 |

### 2.3 `GET /api/configs`
`<repo_root>/config/*.yml` 중 `default.yml`을 뺀 **파일 이름** 목록(정렬). 회사 설정을 웹에서 쓰려면 artifacts 파일을 `config/` 안에 두어야 합니다. `max_upload_mb`는 서버의 업로드 한도(MB, `0` = 없음)이고 화면은 이 값으로 올리기 전에 파일 크기를 확인합니다.

### 2.4 `POST /api/runs` — 업로드와 시작
**multipart가 아닙니다.** 요청 본문 = zip 파일의 원시 바이트, 나머지는 쿼리 파라미터입니다(웹 페이지도 `Content-Type: application/zip`으로 보냄).

| 쿼리 파라미터 | 기본값 | 의미 |
|---|---|---|
| `name` | `project.zip` | 업로드 파일 이름(안전한 문자로 바뀌어 `<out>/_uploads/<run id>-<name>`에 저장). |
| `configs` | `""` | 쉼표 구분 설정 파일 이름(예: `artifacts.yml,site.yml`). 각 이름은 `config/` 안에 실제로 있어야 하며 경로 구분자를 쓸 수 없습니다. 지정 순서대로 병합. |
| `stop_after` | 없음 | 1.2의 값. |
| `llm_provider` | 없음 | `llm.provider` 덮어쓰기(예: `fake`). |

검증과 오류:

| 상황 | 응답 |
|---|---|
| 본문 없음 | 400 `zip body required` |
| 업로드 한도 초과(Content-Length, 또는 받는 중의 실제 크기; 기본 512 MB, `--max-upload-mb` / `SBMIG_MAX_UPLOAD_MB`, `0` = 없음) | 413 `zip too large (max 512 MB)` |
| 본문이 `PK`로 시작하지 않음 | 400 `body is not a zip file` |
| 모르는 설정 파일, 잘못된 `stop_after`, 설정 검증 실패(artifacts 누락 등), 깨진 zip | 400 `{"error": "<원인>"}` |
| 정상 | 202 `{"run_id": "web-…", "status": {"state": "queued" 또는 "running", …}}` |

설정·Workspace·Orchestrator 생성까지는 요청 안에서 동기적으로 하므로 잘못된 요청은 400으로 바로 돌아오고, 실제 마이그레이션은 워커 스레드(`sbmig-run-<run id>`)에서 진행됩니다. 웹 실행은 KB를 항상 켭니다(`--no-kb` 없음).

### 2.5 실행 상태(`GET /api/runs/{run_id}`의 `state`)
| state | 의미 |
|---|---|
| `queued` | 이 서버 프로세스가 받았지만 앞 실행이 끝나기를 기다리는 중 |
| `running` | 마이그레이션 진행 중 |
| `finished` | 끝남(성공/실패/중단 모두). `exit_code`로 구분 |
| `unknown` | 디렉터리는 있으나 report.json도 `web-status.json`도 없음(예: 이전 서버 프로세스가 도중에 죽음) |
| `missing` | 디렉터리 없음(`exists: false`, HTTP 404) |

상태 객체 필드: `run_id`, `run_dir`, `exists`, `state`, `exit_code`(0/2/3 또는 null), `node`(마지막으로 끝난 그래프 노드), `stop_requested`(중단 요청 시 true), `error`(워커 예외), 그리고 report.json이 있으면 `success`, `blocked`(개수), `unresolved`(개수), `llm_calls`, 존재하는 결과 파일 목록 `files`. `GET /api/runs` 행에는 `started`(epoch 초)가 추가됩니다.

메모리 정리: `RunManager`는 끝난 실행을 최대 `max_finished_runs`개(기본 200, `DEFAULT_MAX_FINISHED_RUNS` — **설정 키가 아니라 `RunManager` 생성자 인자**입니다)만 메모리에 둡니다. 밀려난 실행과 이전 서버 프로세스의 실행은 실행 디렉터리의 report.json과 `web-status.json`(종료 코드·오류·중단 여부)으로 답합니다.

### 2.6 `GET /api/runs/{run_id}/events` — SSE
형식: 표준 Server-Sent Events(`event: <종류>` + `data: <JSON>`). 연결하면 **처음부터 다시 재생**합니다: 메모리의 node/error 이력 전부, 그리고 `journal.jsonl`과 `sbmig.log`를 파일 처음부터 읽습니다. 따라서 늦게 연결해도, 새로고침해도 전체 진행이 다시 보입니다.

| 이벤트 | data | 언제 |
|---|---|---|
| `status` | 2.5의 상태 객체 | 첫 이벤트, 그리고 끝나기 직전 |
| `node` | `{"node": "intake"|"analyze"|"plan"|"phase"|"finalize", "update": {...}}` | LangGraph 노드 하나가 끝날 때. `update`는 그래프 상태 갱신(예: `phase`, `phases`, `aborted`, `stopped`, `summary`{case_type, phases[{phase, ok, build_ok, skipped, remaining_errors}], llm_calls, blocked, unresolved}). 20,000자를 넘으면 `{"_truncated": <길이>, "keys": [...]}`로 축약. LangGraph가 없어 inline 엔진으로 돌면 node 이벤트가 없습니다. |
| `error` | `{"message": "<예외 클래스>: <메시지>"}` | 워커가 예외로 죽었을 때(실행은 종료 코드 3으로 끝남) |
| `journal` | `journal.jsonl` 한 줄(JSON 객체, `ts`, `run_id`, `event`, …) | 저널이 늘어날 때 |
| `log` | `sbmig.log` 한 줄(문자열, DEBUG 포함) | 로그가 늘어날 때 |
| `report` | report.md 전체 텍스트(문자열) | 실행이 끝난 뒤 한 번 |
| `ping` | `{"t": <epoch 초>}` | 15초 동안 보낼 것이 없을 때 keep-alive |
| `end` | `{"run_id": "..."}` | 마지막 이벤트(이후 연결 종료) |

순서: 0.5초마다 새 줄을 모아, journal 이벤트(`ts`, UTC ISO 8601)와 log 줄(`%(asctime)s`, 로컬 시각)을 **타임스탬프로 정렬**해 보냅니다(round 9: 끝난 빌드의 FAILED가 다음 빌드 시작 뒤에 보이던 문제 해결). 타임스탬프가 없는 줄(예: traceback 연속 줄)은 파일 안 위치를 유지합니다. 페이지 코드는 `console` 이벤트도 받도록 되어 있지만 서버는 보내지 않습니다.

### 2.7 `POST /api/runs/{run_id}/stop` — 중단
- 취소 플래그를 세우고 `{"stopped": true}`를 돌려줍니다. 모르는 실행이거나 이미 끝났으면 `{"stopped": false}`.
- **그래프 노드 사이에서만 효과가 있습니다.** 진행 중인 Maven 빌드, OpenRewrite 실행, LLM 호출은 끊지 않습니다. 그 노드가 끝나면 멈춥니다.
- verify 단계 전에 멈추면 실행은 `aborted`("stopped by user (web UI) — the final `mvn clean package` did not run")로 마무리되고 종료 코드 3, **최종 `mvn clean package`를 실행하지 않습니다**(보고서에 그렇게 적힘).
- verify 단계(또는 finalize)가 이미 끝난 뒤의 중단은 아무것도 바꾸지 않습니다. 실행은 정상 종료합니다.
- inline 엔진(LangGraph 없음)에서는 중간 갱신이 없어 중단이 효과가 없습니다.
- `queued` 상태 실행에도 플래그는 세워지며, 차례가 와서 첫 노드가 끝나면 멈춥니다(코드 기준: `sbmig/web/runs.py`).

### 2.8 `GET /api/runs/{run_id}/files/{name}`
허용되는 `name`(그 외는 404 `unknown file`):

| name | Content-Type |
|---|---|
| report.md | `text/markdown; charset=utf-8` |
| report.json | `application/json` |
| `result.zip` | `application/zip`(항상 첨부로 다운로드) |
| `sbmig.log` | `text/plain; charset=utf-8` |
| `journal.jsonl` | `application/x-ndjson`(첨부) |
| `error_cases.csv` | `text/csv; charset=utf-8`(첨부, UTF-8 BOM — 엑셀로 바로 열림) |
| `error_cases.jsonl` / `fix_attempts.jsonl` | `application/x-ndjson; charset=utf-8`(첨부) |
| `changes.patch` | `text/plain; charset=utf-8` |
| plan.json | `application/json` |
| `profile.json` | `application/json` |

`?download`를 붙이면 첨부(attachment)로, 없으면 브라우저 안에서(inline) 엽니다. `.zip`, `.csv`, `.jsonl`은 항상 첨부입니다. 파일이 아직 없으면 404 `<name> not found`.

### 2.9 `GET /api/threads`
서버 프로세스의 모든 스레드 스택을 텍스트로 돌려주고 같은 내용을 `<out>/threads-<YYYYMMDD-HHMMSS>.txt`로 저장합니다(첫 줄 `# written to <경로>`). Windows에는 SIGUSR1이 없고 `sbmig serve` 창의 Ctrl+Break는 서버를 끄므로, "멈춘 것 같을 때" 이것이나 실행 디렉터리의 `dump-threads` 파일 트리거(round 10)를 씁니다. 실행 스레드는 `--- thread sbmig-run-<run id> (…) ---` 블록입니다. 진단 절차는 [09. 운영과 진단](09-operations.md).

### 2.10 예제
```bash
# 설정 목록
curl -s http://127.0.0.1:8765/api/configs
# 업로드 + 시작 (본문 = zip 바이트)
curl -s -X POST --data-binary @order-api.zip -H "Content-Type: application/zip" \
  "http://127.0.0.1:8765/api/runs?name=order-api.zip&configs=artifacts.yml&stop_after=spring"
# 상태 / SSE / 중단 / 파일
curl -s http://127.0.0.1:8765/api/runs/web-20261021-101500-ab12cd
curl -N http://127.0.0.1:8765/api/runs/web-20261021-101500-ab12cd/events
curl -s -X POST http://127.0.0.1:8765/api/runs/web-20261021-101500-ab12cd/stop
curl -s -o report.md http://127.0.0.1:8765/api/runs/web-20261021-101500-ab12cd/files/report.md
curl -s http://127.0.0.1:8765/api/threads
```
```powershell
# PowerShell: curl은 Invoke-WebRequest의 별칭일 수 있으므로 curl.exe를 명시합니다
curl.exe -s http://127.0.0.1:8765/api/configs
curl.exe -s -X POST --data-binary "@D:\asis\order-api.zip" -H "Content-Type: application/zip" "http://127.0.0.1:8765/api/runs?name=order-api.zip&configs=artifacts.yml"
curl.exe -N http://127.0.0.1:8765/api/runs/web-20261021-101500-ab12cd/events
curl.exe http://127.0.0.1:8765/api/threads

# 서버에 토큰이 있으면(round 10) 모든 요청에 헤더를 붙입니다
curl.exe -s -H "Authorization: Bearer $env:SBMIG_WEB_TOKEN" http://<호스트>:8765/api/runs

# Invoke-RestMethod
$r = Invoke-RestMethod -Method Post -InFile D:\asis\order-api.zip -ContentType "application/zip" `
     -Uri "http://127.0.0.1:8765/api/runs?name=order-api.zip&configs=artifacts.yml"
$h = @{ Authorization = "Bearer $env:SBMIG_WEB_TOKEN" }      # 토큰이 있을 때: -Headers $h
$r.run_id
(Invoke-RestMethod "http://127.0.0.1:8765/api/runs/$($r.run_id)").state
Invoke-RestMethod -Method Post "http://127.0.0.1:8765/api/runs/$($r.run_id)/stop"
Invoke-WebRequest "http://127.0.0.1:8765/api/runs/$($r.run_id)/files/report.md" -OutFile report.md
```

## 3. Python API — `sbmig.api`

### 3.1 시그니처
```python
def migrate(
    input: str | Path,                       # 프로젝트 디렉터리 또는 zip
    *,
    configs: Sequence[str | Path] = (),      # --config 파일들(순서대로 병합)
    out: str | Path = "out",                 # 실행 디렉터리 상위
    run_id: str | None = None,               # None = 자동
    overrides: dict | None = None,           # CLI 플래그와 같은 단계의 덮어쓰기 ({"llm": {"record_dir": "out/rec"}})
    llm: LLMClientLike | LLMAdapterLike | None = None,   # None = 설정(llm.provider, 플러그인 포함)으로 생성
    fixer: FixerLike | None = None,          # LLMFixer 대체(fix.plugin보다 우선)
    kb: KnowledgeBaseLike | None | bool = None,  # None/True = 파일 KB, False = KB 없음(--no-kb), 객체 = 그대로 사용
    stop_after: str | None = None,           # 1.2의 값
    dry_run: bool = False,
    repo_root: str | Path | None = None,
    components: Mapping[str, Any] | None = None,   # 고급: Orchestrator 협력 객체(build_runner, rewrite_runner, restructurer, …)
) -> RunReport

def exit_code(report: RunReport) -> int     # 0 / 2 / 3 — CLI와 동일한 run_exit_code
```

동작: `parse_stop_after` → `load_config(*configs, overrides=…, repo_root=…)` → `llm`이 주어지면 `wrap_llm`으로 감싸고(어댑터는 `AdapterClient`로 자동 변환) `llm.record_dir`이 설정돼 있으면 `RecordingClient`로 기록 → `Workspace.from_input(input, out, run_id)` → `Orchestrator(cfg, ws, llm=…, fixer=…, kb=…, **components).run(stop_after=…, dry_run=…)`. 반환값은 `RunReport`(report.json과 같은 모델, 6절). 잘못된 `stop_after`는 `ValueError`, 설정 검증 실패는 pydantic `ValidationError`(= `ValueError`)로 올라옵니다. `migrate`는 로그 파일을 붙이지 않으므로 필요하면 `sbmig.cli.attach_file_log(Path(...))`를 직접 부르십시오.

### 3.2 최소 예제
```python
from pathlib import Path
from sbmig.api import migrate, exit_code
from sbmig.cli import attach_file_log

attach_file_log(Path("out/api-run.log"))          # 선택: DEBUG 로그 파일
report = migrate("D:/asis/order-api.zip",
                 configs=["config/artifacts.yml"],
                 out="out",
                 stop_after=None, dry_run=False)
print(report.run_id, report.success, exit_code(report))
print(report.outputs.get("report_md"))
for b in report.blocked:
    print(b.reason.value, b.suggestion)
```

### 3.3 사내 에이전트 프레임워크에서 부르는 방법
- 회사 관례(LangGraph + uvicorn)의 에이전트 노드에서 `migrate(...)`를 **동기 함수로** 호출합니다. 한 번에 한 프로젝트씩(파일 KB 공유 — 동시 실행 금지).
- 게이트웨이를 사내 코드로 부르고 싶으면 `llm=MyGatewayAdapter(...)`(tier 1, `LLMAdapter` 하위 클래스)를 넘깁니다. sbmig가 프롬프트·패치 파싱·예산·기록을 그대로 맡습니다.
- 수정 자체를 사내 에이전트가 하게 하려면 `fixer=MyFixer(...)`(tier 2)를 넘깁니다.
- 결과 판정은 `exit_code(report)`(0 성공 / 2 미완료 / 3 중단). 사람에게 넘길 내용은 `report.outputs["report_md"]`의 Handover 절.

```python
from sbmig.api import migrate, exit_code
from mypkg.adapter import MyGatewayAdapter

report = migrate("path/to/project", configs=["config/artifacts.yml"], out="out",
                 llm=MyGatewayAdapter(base_url="https://gateway.internal/api"),
                 overrides={"loop": {"max_llm_calls": 30}})
```

## 4. 플러그인 계약

두 개의 이음매(seam)가 있고 **둘 다 안정 계약(불변식 4)** 입니다: `LLMRequest` / `LLMResponse`, `FixerLike`, `sbmig.api.migrate(...)`. 상세 설명과 문제 해결 표는 `docs/ADAPTERS.md`, 작동 예제는 `examples/adapters/`.

| tier | 설정 | 구현할 것 | sbmig가 계속 하는 일 |
|---|---|---|---|
| 1. chat completion | `llm.provider: plugin` + `llm.plugin: "pkg.module:attr"`(또는 진입점 그룹 `sbmig.llm_adapters`의 이름을 `llm.provider`에) | `complete(request: LLMRequest) -> LLMResponse` | 프롬프트, SEARCH/REPLACE 프로토콜, 컨텍스트 패킹, 피드백 라운드, 예산, 기록, KB 수확 |
| 2. fixer 전체 | `fix.plugin: "pkg.module:attr"`(점 없는 이름 = 그룹 `sbmig.fixers`) | `propose(ws, cluster, ctx) -> FixProposal` [+ `apply`] | 클러스터링, triage, 차단 규칙, 결정적 핸들러, KB, 예산, git 커밋/되돌리기, 재빌드, 보고서 |

둘을 함께 쓸 수 있습니다. tier 2 팩토리는 tier 1 클라이언트를 `llm` 인자로 받습니다.

### 4.1 tier 1 데이터 계약(`sbmig/core/interfaces.py`)
| 모델 | 필드 | 타입 / 기본값 | 의미 |
|---|---|---|---|
| `ChatMessage` | `role` | `system` / `user` / `assistant` | |
| | `content` | str | |
| `LLMRequest` | `messages` | list[ChatMessage] | 대화 |
| | `model` | str 또는 None | `llm.model` / `llm.recipe_model` / `llm.escalation_model`, None = 어댑터 기본 |
| | `max_tokens` | int 또는 None | |
| | `temperature` | float 또는 None | None = 어댑터 기본 |
| | `stop` | list[str] 또는 None | |
| | `role` | `fix` / `fix_escalation` / `plan_advisor` / `recipe_candidate` / `other` | 누가 묻는지(라우팅·과금용). 모르는 값은 `other`로 바뀌고 원래 값은 `metadata["role_detail"]` |
| | `metadata` | dict | `phase`, `iteration`, `cluster_id`, round 10부터 `run_id`, `project` 등(비밀값 없음) — 게이트웨이 쪽에서 프로젝트·실행별로 호출을 묶을 수 있음 |
| `LLMResponse` | `content` | str | 답 텍스트 |
| | `model` | str | 실제 모델(비면 요청 모델로 채움) |
| | `input_tokens`, `output_tokens` | int, 0 | 사용량(예산·보고서) |
| | `finish_reason` | str 또는 None | |

| 역할(`role`) | 호출자 | sbmig가 요청하는 모델 |
|---|---|---|
| `fix` | LLMFixer(build-fix 루프) | `llm.model` |
| `fix_escalation` | 정체 후 LLMFixer | `llm.escalation_model`(없으면 `llm.model`) |
| `plan_advisor` | `plan.advisor: llm` | `llm.recipe_model`(없으면 `llm.model`) |
| `recipe_candidate` | KB 수확의 레시피 후보 생성 | `llm.recipe_model`(없으면 `llm.model`) |
| `other` | 그 외(예: llm-probe) | 전달된 값 |

프로토콜:
```python
class LLMAdapterLike(Protocol):                    # tier 1 (요청 객체형)
    def complete(self, request: LLMRequest) -> LLMResponse: ...

class LLMClientLike(Protocol):                     # 메시지형 클라이언트도 tier 1로 허용
    def complete(self, messages, *, model=None, max_tokens=None, temperature=None,
                 stop=None, role=None, metadata=None) -> LLMResponse: ...
```
메시지형 `complete`가 `role`/`metadata`를 받지 않으면 sbmig가 그 키워드를 빼고 부릅니다(`KwargFilterClient`). 선택적 기반 클래스 `sbmig.llm.LLMAdapter`(추상 `complete(request)`, `name` 속성, 아무것도 안 하는 `close()`).

### 4.2 tier 1 등록과 로딩(`sbmig/llm/registry.py`)
- 등록 문자열: `"pkg.module:attr"`(점 표기 `"pkg.module.attr"`도 허용). 형식이 틀리면 `PluginLoadError`(`must look like 'pkg.module:attr'`).
- `llm.provider` 해석 순서: (1) `company`/`openai_compatible`/`anthropic`/`fake` = 내장 클라이언트, (2) `plugin` = `llm.plugin` 로드(없으면 오류), (3) 그 외 이름 = 그룹 `sbmig.llm_adapters`의 같은 이름 진입점, 없으면 `llm.plugin`, 둘 다 없으면 `PluginLoadError`(설치된 진입점 이름과 등록 방법 두 가지를 안내).
- 로드한 객체가 클래스/팩토리면 시그니처가 받는 만큼 `(options, cfg)`로 호출합니다(`(options, cfg)`, `(options)`, `()` 모두 가능). 인스턴스면 그대로 씁니다.
- `options` = `llm.plugin_options`. `llm.api_key_env`가 가리키는 환경 변수가 있으면 그 값이 **메모리에서만** `options["api_key"]`로 추가됩니다(설정·기록·저널에 쓰지 않음). 키가 없어도 오류가 아닙니다.
- 어댑터/클라이언트 구분: `LLMAdapter` 하위 클래스 → `adapter_kind = "request"`(또는 `"client"`) 속성 → `complete`의 첫 매개변수 이름이 `request`이거나 `LLMRequest`로 주석 → 그 외는 메시지형 클라이언트.
- 진입점 등록(사내 패키지의 `pyproject.toml`):
```toml
[project.entry-points."sbmig.llm_adapters"]
mygateway = "mypkg.adapter:make_adapter"
```
설치(`pip install -e mypkg`) 후 `llm.provider: mygateway`.

sbmig가 보장하는 것: `llm.record_dir`이면 플러그인 호출도 `llm-NNNN.json`과 저널 `llm_call`로 기록, 예산(`loop.max_llm_calls`, `loop.max_llm_attempts_per_cluster`, `fleet.max_llm_calls_total`) 적용, 결과를 `LLMResponse`로 변환(dict의 `content`, `str`, `.content` 객체 허용, 그 외는 `LLMError`), 어댑터 예외는 `sbmig.fix.client.LLMError`로 바뀌고 두 번 연속이면 쿨다운 후 `llm_unavailable`. 플러그인이 할 일: 오류를 `content`로 돌려주지 말고 예외로 던질 것, 재시도·rate limit·인증·타임아웃은 직접 처리할 것, 키를 로그/파일에 남기지 말 것.

### 4.3 tier 2 fixer 계약
```python
class FixerLike(Protocol):
    def propose(self, ws: Workspace, cluster: ErrorCluster, ctx: FixContext) -> FixProposal: ...
    def apply(self, ws: Workspace, changes: list[FileChange]) -> tuple[list[FileChange], list[str]]: ...
        # (적용된 변경, 오류) — 파일 단위 원자성, 부분 수정 금지
```

| 모델 | 필드 | 의미 |
|---|---|---|
| `ErrorCluster` | `id`, `kind`(ErrorKind), `file`, `module`, `errors`(list[BuildError]), `signatures`, `priority` | 한 번에 고칠 오류 묶음(대개 파일 단위) |
| `FixContext` | `phase`, `iteration`, `stall_level`(0), `profile_summary`, `plan_notes`, `kb_hints`, `advisory_hints`, `previous_attempts`(list[FixAttempt]), `other_errors_summary`, `use_escalation_model`(False), `reference_notes`, `reference_files`(dict 경로→내용, 읽기 전용) | 수정에 필요한 맥락 |
| `FileChange` | `path`, `op`(`modify`/`create`/`delete`/`rename`), `search`, `replace`, `content`, `new_path` | `modify`는 search/replace(검색 문자열이 파일에서 정확히 한 곳에 맞아야 함) |
| `FixProposal` | `changes`(필수), `analysis`(""), `confidence`(0.5), `model`, `prompt_hash`, `llm_usage`(LLMUsage: model, calls, input_tokens, output_tokens), `requested_files`, `raw_response` | 제안 |

규칙:
- 팩토리는 `(options, cfg, llm)` 중 받는 만큼으로 호출됩니다(`options` = `fix.plugin_options`, `llm` = sbmig의 LLM 클라이언트 또는 None). 팩토리가 `notes_dir` 키워드를 선언하면 그것도 전달됩니다.
- `apply`가 없으면 sbmig가 `sbmig.fix.PatchApplier(ws).apply(changes)`로 위임하는 것을 붙여 줍니다(`ApplyDelegatingFixer`).
- fixer는 처음 LLM 수정이 필요한 클러스터에서 **지연 생성**되며 저널에 `{"event": "fixer", "plugin": spec}`(실패 시 `error` 포함)를 남깁니다.
- `propose` 안에서 파일을 직접 고치지 말고 변경만 돌려주십시오(sbmig가 적용·커밋·재빌드·되돌리기).
- 에이전트에 닿지 못하면 `FixProposal(changes=[], analysis="llm-error: ...")`를 돌려주거나 예외를 던집니다(전송 실패로 집계 → 반복되면 `llm_unavailable`). 일반 analysis와 빈 `changes`는 "안전한 수정 없음"입니다.
- 예산은 `propose()`당 `max(1, proposal.llm_usage.calls)`로 청구되므로 여러 번 호출하는 에이전트는 `llm_usage.calls`를 채우십시오.
- sbmig의 컨텍스트가 필요하면 `ContextPacker(cfg).pack(ws, cluster, ctx, ctx.profile_summary)` → `PackedContext`(`.sections`, `.files_included`, `.render()`).

### 4.4 내장 LLMFixer의 응답 프로토콜(`sbmig/fix/protocol.py`)
tier 1을 쓰면 모델은 아래 태그 형식으로 답해야 합니다(파서는 코드펜스·CRLF·소문자·닫는 태그 누락에 관대하고 예외를 던지지 않음).
```text
<analysis>왜 실패하고 무엇을 바꾸는지 1-5줄</analysis>
<confidence>0.0~1.0</confidence>
<patches>
=== MODIFY relative/path/File.java
<<<<<<< SEARCH
기존 텍스트(파일 안에서 유일한 온전한 줄들)
=======
바꿀 텍스트
>>>>>>> REPLACE
=== CREATE relative/path/New.java
전체 내용
=== DELETE relative/path/Old.java
=== RENAME old/path/Old.java -> new/path/New.java
</patches>
```
더 많은 맥락이 필요하면 `<need_files>a, b</need_files>` / `<need_grep>regex</need_grep>`(클러스터당 `llm.max_context_rounds`회, grep은 시간 제한 있음). 파싱 결과 `ParsedResponse`: `changes`, `analysis`, `need_files`, `need_grep`, `confidence`, `raw`, `warnings`.

### 4.5 내장 `company` 제공자의 요청 형태
`requests.post(LLM_BASE_URL, headers={"Authorization": "Bearer <LLM_API_KEY>", **llm.extra_headers}, json={"model": …, "messages": [{"role", "content"}…], **llm.payload_extra}, timeout=llm.timeout_seconds)`. 기본 `payload_extra`는 `{"need_origin": true}`. 생성 파라미터(max_tokens/temperature/stop)는 보내지 않습니다. 응답 텍스트는 OpenAI/Azure 형태(`choices[0].message.content`) 등을 자동 감지하고, content-filter 응답(`finish_reason=content_filter`, `prompt_filter_results`)은 "LLM 불가"가 아니라 그 요청의 거절로 처리합니다. 호출 중 30초마다 `llm call still in flight: …` 로그, 하드 데드라인은 `llm.timeout_seconds` + 30초(`sbmig/fix/company.py`).

### 4.6 계속 안정적으로 유지되는 것(불변식 4)
`LLMRequest` / `LLMResponse` / `ChatMessage` 필드, `LLMAdapterLike.complete(request)`, `FixerLike.propose/apply`, `FixContext`/`FixProposal`/`FileChange`, `sbmig.api.migrate(...)` 시그니처, 진입점 그룹 이름 `sbmig.llm_adapters` / `sbmig.fixers`. 필드는 추가될 수 있으나(예: round 9의 `reference_notes`) 제거·이름 변경은 하지 않는 것이 원칙입니다. 계약 변경 이력은 `docs/INTERFACES.md`.

## 5. 환경 변수
| 변수 | 읽는 곳 | 의미 | 비고 |
|---|---|---|---|
| `LLM_BASE_URL` | `config/default.yml` `llm.base_url` | 게이트웨이 전체 URL(그대로 사용) | `company` 제공자 필수. 없으면 `LLM_BASE_URL is not set …` |
| `LLM_API_KEY` | `llm.api_key_env`가 가리키는 이름(기본) | Bearer 토큰 | YAML에 쓰지 않음. 로그에는 마스킹 |
| `LLM_MODEL` | `llm.model`(기본 `gpt-6-luna`) | fix 모델 | |
| `LLM_RECIPE_MODEL` | `llm.recipe_model` | plan advisor / 레시피 후보 모델 | 비우면 `LLM_MODEL` |
| `LLM_ESCALATION_MODEL` | `llm.escalation_model` | 정체 시 모델 | 비우면 `LLM_MODEL` |
| `LLM_RESPONSE_PATH` | `llm.response_path` | 응답 텍스트 점 경로 | 자동 감지 실패 시만 |
| `LLM_RECORD_DIR` | `llm.record_dir` | 요청/응답 기록 디렉터리(`llm-NNNN.json`) | `.env.example`은 `out/llm-records` |
| `SBMIG_ENV_FILE` | `load_dotenv_files` | 추가로 읽을 `.env` 파일 경로(가장 먼저 읽음) | |
| `SBMIG_<섹션>__<키>` | `_apply_env_overrides` | 임의 설정 키 덮어쓰기(값은 YAML 해석). 예: `SBMIG_LOOP__MAX_LLM_CALLS=30` | CLI 플래그보다 우선 |
| `SBMIG_WEB_OUT` | `sbmig/web/app.py` | 순수 uvicorn 실행 시 실행 디렉터리(기본 `out/web`) | `sbmig serve`는 `--out` 사용 |
| `SBMIG_WEB_REPO_ROOT` | `sbmig/web/app.py` | 순수 uvicorn 실행 시 저장소 루트 | |
| `SBMIG_MAX_UPLOAD_MB` | `sbmig/web/app.py` | 웹 업로드 한도(MB, 기본 512, `0` = 없음). `.env`에 두어도 되며 서버가 뜰 때 읽음 | `sbmig serve --max-upload-mb`가 우선 |
| `SBMIG_WEB_TOKEN` | `sbmig/web/app.py`, `sbmig serve` | 웹 접근 토큰(round 10). 루프백이 아닌 `--host`에는 필수. `.env`에 두어도 됨(서버가 뜰 때 읽음) | `sbmig serve --token`이 우선. 길고 임의인 값 |
| `JAVA21_HOME` | `config/artifacts.local-e2e.yml`의 `build.java_home`, e2e 스크립트 | 로컬 e2e용 JDK 21 | 회사 설정에서는 `build.java_home`을 직접 지정 |
| `JAVA_HOME`, `PATH` | Maven 하위 프로세스 | `build.java_home`이 있으면 sbmig가 Maven 환경에서 덮어씀 | |
| `MAVEN_HOME`, `M2_HOME` | `mvn_executable` | `build.mvn`이 이름일 때 `<값>/bin`도 탐색 | |
| `NO_PROXY`, `HTTP(S)_PROXY` | `requests` | 게이트웨이 프록시 우회/사용 | `llm-probe`가 적용 프록시를 출력 |
| `COMPANY_LLM_KEY` | `config/llm-plugin.example.yml`의 예 | 플러그인 예제의 키 변수 이름 | 예제일 뿐 |

`.env`는 `$SBMIG_ENV_FILE` → `<저장소 루트>/.env` → `<현재 디렉터리>/.env` 순서로 읽히고, 이미 있는 변수는 덮어쓰지 않습니다(셸 우선). round 10부터 UTF-8(BOM 유무 무관), UTF-16(메모장 "유니코드" / PowerShell `>`로 저장), cp949 파일을 모두 읽고, 실행마다 파일의 수정 시각을 보고 **바뀐 `.env`를 다시 읽습니다**(웹 서버의 다음 run, 플릿의 다음 프로젝트). 이 로더가 전에 넣은 값은 파일을 따라가고, 셸에서 온 값은 여전히 이깁니다. `KEY=   # 주석`은 빈 값입니다.

### 5.1 round 10에 추가된 설정 키(요약)
뜻과 기본값의 원본은 [08. 설정 레퍼런스](08-config-reference.md)입니다.

| 키 | 기본값 | 한 줄 |
|---|---|---|
| `build.require_java_major` | `21` | `mvn -v`의 Java 주 버전이 다르면 첫 빌드 전에 중단(`jdk-not-21`, 종료 코드 3). `0` = 확인 안 함 |
| `build.retry_negative_cache` | `true` | "was cached in the local repository"로 실패한 빌드를 `-U`로 한 번 더 |
| `loop.structural_regression_kinds` | `pom_invalid`, `parent_resolution`, `plugin_resolution`, `dependency_resolution` | pom 수정 뒤 새로 나타나면 개수와 상관없이 되돌리는 에러 종류(round 10에 `dependency_resolution` 추가) |
| `kb.redact_file` | `knowledge/kb/redact.yml` | 내장 비밀값 마스킹 규칙에 더할 회사 패턴 |
| `kb.lock_timeout_seconds` | `300` | 프로세스 간 KB 잠금 `<kb.dir>/.lock`의 대기 / 낡음 기준 |
| `kb.error_diff_chars` | `12000` | 저장하는 시도 diff 상한 — 분류한 **뒤** 자름(0 = 자르지 않음) |
| `fleet.preflight` | `true` | 첫 프로젝트 전(그리고 인프라 대기 뒤) 디스크 / Maven / Java / settings / 저장소 / LLM 점검 |
| `fleet.infra_failure_limit` | `3` | 인프라 이유로 끝난 프로젝트가 연속 몇 개면 기다렸다 다시 점검할지 |
| `fleet.infra_retry_minutes` | `15` | 기다리는 분. `0` = 바로 멈춤 |
| `fleet.infra_max_waits` | `4` | 최대 대기 횟수, 넘으면 `fleet stopped: …` |
| `fleet.cleanup` | `build-output` | 프로젝트 뒤 정리: `none` / `build-output` / `workspace` |
| `fleet.min_free_gb` | `5` | 프로젝트마다 확인할 출력 드라이브의 최소 여유(GB), `0` = 확인 안 함 |

## 6. report.json 스키마(`RunReport`, `sbmig/core/models.py`)

### 6.1 최상위 필드
| 필드 | 타입 | 의미 |
|---|---|---|
| `run_id` | str | 실행 id |
| `project_name` | str | 프로젝트 이름 |
| `case_type` | str | `pure_java` / `spring_legacy` / `spring_boot` / `servlet_tomcat` / `msa` / `unknown` |
| `started_at`, `finished_at` | datetime | 시작/종료(UTC) |
| `success` | bool | 최종 verify 빌드가 성공했고 `restructure-failed`가 아님. round 10: 실행이 중단됐거나 계획된 최종 빌드 스텝(MSA는 서비스마다) 중 결과가 없는 것이 있으면 false |
| `profile` | ProjectProfile | 분석 결과(`profile.json`과 같음): `root`, `name`, `case_type`, `confidence`, `reasons`, `build_tool`, `poms`, `root_pom`, `source`, `libraries`, `java_version`, `spring_boot_version`, `spring_version`, `packaging`, `is_multi_module`, `services`, `optional_artifacts_in_use`, `mandatory_already_present`, `warnings`, `fingerprint`, `properties`, `boot_modules` |
| `plan` | MigrationPlan | `profile_fingerprint`, `case_type`, `steps`[PlanStep: `id`, `kind`, `phase`, `title`, `recipes`, `params`, `build_goal`, `max_iterations`, `target`, `reason`, `optional`], `notes`, `created_at`, `llm_usage` |
| `phases` | list[PhaseResult] | 단계별: `phase`, `step_ids`, `ok`, `build_ok`, `iterations`, `rewrite_results`, `attempts`(FixAttempt), `iteration_records`, `remaining_errors`, `started_at`, `finished_at`, `notes`, `skipped`, `blocked` |
| `final_build` | BuildResult | `ok`, `goal`, `command`, `exit_code`, `duration_s`, `log_path`, `errors`, `modules_built`, `failed_module`, `summary`, `timed_out` |
| `llm_usage` | LLMUsage | `model`, `calls`, `input_tokens`, `output_tokens`(plan advisor 포함) |
| `harvest` | HarvestResult | `cases_created`, `candidates_created`, `recipes_credited`, `recipes_promoted`, `ledger_entries`, `notes` |
| `unresolved` | list[BuildError] | 남은 오류: `kind`, `message`, `file`, `line`, `column`, `symbol`, `location`, `module`, `artifact`, `goal`, `raw`, `signature`(L1), `signature_generic`(L2) |
| `blocked` | list[BlockedItem] | 의도적으로 멈춘 클러스터: `id`, `reason`, `phase`, `cluster_id`, `signatures`, `files`, `errors`, `artifact`, `owner_class`, `required_type`, `found_type`, `suggestion`, `attempts`, `first_seen_iteration`, `notes`, `via`, `via_direct` |
| `javax_api_jars` | list[JavaxApiJar] | 마이그레이션 후 클래스패스에 남은 javax API jar(X1b): `artifact`, `module`, `scope`, `path`, `via`, `declared` |
| `manual_followups` | list[str] | as-is 담당자가 할 일 |
| `advisories` | list[AdvisoryFinding] | `advisory_id`, `library`, `status`, `title`, `summary`, `policy`, `policy_reason`, `detected_version`, `evidence`, `artifacts`, `recipes`, `recipe_source`(`plan`/`advisory`/`none`), `outcome`(`replaced`/`still-present`/`unknown`), `replacement`, `replacement_version`, `followup`, `docs`, `notes`, `llm_fixed_files`, `llm_fix_notes` |
| `handover` | HandoverSummary | `parent_before/after`, `java_before/after`, `spring_boot_before/after`, `changes`(VersionChange: `kind`, `key`, `before`, `after`, `before_source`, `after_source`, `reason`, `carried_over`, `pom`), `added`, `removed`, `carried_over`, `from_here`, `notes`, `merge_conflicts`, `properties_to_set`, `properties_guide` |
| `merge_conflicts` | list[MergeConflict] | `path`, `kept_from`, `dropped_from`, `identical`, `saved_copies`, `union` |
| `properties` | PropertyInventory | `findings`(PropertyFinding: `key`, `status` `defined`/`defaulted`/`external`, `refs`, `defined_in`, `boot_keys`, `supplied_by`, `note`, `service`), `packaged_files`, `profiles`, `filtered_resources`, `filtered_binary_files`, `placeholder_files`, `placeholders_written`, `notes`, `services` |
| `flags` | list[str] | 6.2 |
| `outputs` | dict[str,str] | `run_dir`, `project_dir`, `journal`, `logs`, `profile`, `plan`, `diff`, `zip`, `report_md`, `report_json`, `effective_depmgmt` 등 경로 |
| `timings_s` | dict[str,float] | 단계별 소요 시간 |

`BlockReason` 값: `external_javax_api`, `external_api_incompatible`, `artifact_not_found`, `parent_not_found`, `plugin_incompatible`, `unsupported_source`, `encoding`, `budget_exhausted`, `llm_unavailable`, `repeated_failure`, `oscillation`. `ErrorKind` 값: `compile`, `test_compile`, `dependency_resolution`, `plugin_resolution`, `parent_resolution`, `plugin_execution`, `enforcer`, `pom_invalid`, `rewrite`, `test_failure`, `unknown`. FixAttempt `outcome` 값: `pending`, `improved`, `partial`(round 10: 겨냥한 에러 일부만 사라짐, 진전), `no_change`, `inconclusive`(round 10: 그 빌드로 판단 불가 — 타임아웃, 파싱된 에러 없는 실패, 앞 단계에서 멈춤), `regressed`, `reverted`, `build_ok`, `apply_failed`, `blocked`. 결정 종류(`DecisionKind`): `handler`, `kb_recipe`, `kb_patch`, `llm_fix`, `skip`, `escalate`, `blocked`.

### 6.2 플래그 어휘
| 플래그 | 설정하는 곳 | 의미 | 종료 코드 영향 |
|---|---|---|---|
| `aborted` | orchestrator / 웹 중단 | 실행이 일찍 멈춤(사유는 follow-up 첫 항목) | 3 |
| `aborted:parent-not-found` | fix loop | 설정된 사내 부모 POM을 해석할 수 없음 | 3 |
| `mvn-not-found` | orchestrator | 이 PC에서 Maven을 실행할 수 없음(round 10: 원인이 `JAVA_HOME`이면 "install Maven"이 아니라 JAVA_HOME을 가리킴) | 3 |
| `jdk-not-21` | orchestrator(preflight) | `mvn -v`의 Java가 `build.require_java_major`가 아님 → 첫 빌드 전 중단(follow-up `run aborted: Maven runs on Java … not Java 21: …`) | 3 |
| `infra:llm` / `infra:maven` / `infra:jdk` / `infra:repository` / `infra:disk` | orchestrator(finalize) | round 10: 실행이 인프라 이유로 실패 — LLM 불가·설정 오류이고 실행 실패 / `mvn-not-found` / `jdk-not-21`(또는 JAVA_HOME 때문에 mvn 실패) / 사내 부모를 못 받거나 최종 빌드 실패가 전부 전송·저장소 문구의 해석 에러 / 디스크 가득. 플릿 차단기가 셈 | 실행 결과대로(대개 2 또는 3) |
| `llm-config-error` | fix loop | round 10: 같은 HTTP 400 본문이 서로 다른 프롬프트 2개에 옴 → 이 실행의 LLM 호출 중단 | 빌드 결과에 따름 |
| `unsupported-build-tool:<gradle/ant/none>` | intake | Maven 프로젝트가 아님(함께 `aborted`) | 3 |
| `dry-run` | orchestrator | 결정만 계산, 적용 없음 | 2 |
| `stopped-after-<phase>` | orchestrator | `--stop-after`로 의도적 정지 | 2(최종 빌드 없음) |
| `restructure-failed` | orchestrator | restructure 단계 실패/충돌(as-is 트리로 빌드가 통과해도 성공으로 보지 않음) | 2 |
| `rewrite-unresolved-versions` | orchestrator | 레시피가 `4.0.x`/`latest.release` 같은 문자 그대로 버전을 남김(메타데이터 다운로드 실패, X8) | 빌드 결과에 따름 |
| `rewrite-parse-problems` | orchestrator | rewrite 플러그인이 `There were problems parsing …`을 출력(`RewriteResult.parse_problems`, E8b) — 그 파일은 레시피가 건너뜀 | 빌드 결과에 따름 |
| `time-budget-exhausted` | loop / orchestrator | `loop.max_total_minutes` 도달 | 빌드 결과에 따름 |
| `llm-budget-exhausted` | orchestrator | `loop.max_llm_calls` 모두 사용 | 빌드 결과에 따름 |
| `llm-unavailable` | fix loop | LLM이 (쿨다운 후에도) 연속 실패 → LLM 전용 클러스터는 blocked(`llm_unavailable`) | 빌드 결과에 따름 |
| `llm-cooldown` | fix loop | 첫 연속 실패 후 `llm.cooldown_seconds` 동안 쉼(L5) | 없음 |
| `test-compile-skipped` | fix loop | 테스트 컴파일만 실패 → `-Dmaven.test.skip=true`로 패키징 | 없음(follow-up) |
| `tests-skipped` | fix loop | 런타임 테스트 실패만 → `-DskipTests`로 패키징 | 없음(follow-up) |
| `source-encoding-set:<enc>` | 핸들러 `source-encoding` | `project.build.sourceEncoding`을 감지된 인코딩(예: `cp949`)으로 | 없음 |
| `source-encoding-converted:<enc>` | 핸들러 `source-encoding` | 소스를 UTF-8로 변환(`convert-utf8`) | 없음 |
| `utf8-bom-stripped:<N>` | 핸들러 `strip-utf8-bom` | N개 파일에서 BOM 제거 | 없음 |
| `managed-versions-carried-over` | 핸들러 `carry-over-managed-version` | 새 부모가 관리하지 않는 의존성을 as-is 버전으로 고정(⚠) | 없음 |
| `enforcer-relaxed` | 핸들러 `enforcer-relax` | JDK 21에서 실패하는 enforcer 규칙 완화 | 없음 |
| `enforcer-skipped` | 핸들러 `enforcer-relax` | `-Denforcer.skip=true` | 없음 |
| `werror-removed` | 핸들러 `remove-werror` | 컴파일러 `-Werror` 제거 | 없음 |
| `systempath-relocated` | 핸들러 `systempath-relocate` | system scope jar 경로 재작성 | 없음 |
| `jakarta-leftovers-renamed` | 핸들러 `jakarta-namespace-sweep` | 소스에 남은 `javax.*` Jakarta EE 참조를 `jakarta.*`로(E13) | 없음(follow-up) |
| `spring-data-crud-restored` | 핸들러 `spring-data-crud-repository` | 페이징 리포지토리에 `ListCrudRepository<T, ID>` 추가(E14) | 없음 |
| `jakarta-library-bumped` | 핸들러 `jakarta-library-floor` | jakarta 릴리스가 알려진 서드파티 라이브러리를 그 버전으로(`knowledge/libraries/jakarta_libraries.yml`) | 없음 |
| `web-xml-registered` | 재구성기(`RestructureResult.flags` → orchestrator) | web.xml 선언을 boot 모듈의 `WebXmlRegistrations` @Configuration으로 생성(W1) | 없음 |
| `jdk-base64-shimmed` | 핸들러 `jdk-base64-shim` | round 10: `sun.misc.BASE64*` / xerces `Base64` → 생성한 `sbmig.compat` 대체 클래스 | 없음 |
| `tools-jar-removed` | 핸들러 `remove-tools-jar` | round 10: `com.sun:tools` / `jdk.tools:jdk.tools` 의존성 삭제 | 없음 |
| `boot4-types-relocated` | 핸들러 `boot4-type-relocation-sweep` | round 10: Boot 4 우산 레시피가 옮기지 않은 타입을 새 패키지로 | 없음 |
| `security-path-matchers-rewritten` | 핸들러 `security-path-pattern-matcher` | round 10: `AntPathRequestMatcher` / `MvcRequestMatcher` → `PathPatternRequestMatcher`(`// TODO sbmig:` 줄 확인) | 없음 |
| `specification-unrestricted` | 핸들러 `spring-data-specification-unrestricted` | round 10: `Specification.where(null)` → `Specification.unrestricted()` | 없음 |
| `javax-api-on-classpath` | orchestrator(`report.javax_api_inventory`) | 마이그레이션 후 javax API jar가 클래스패스에 남음(X1b, 보고서 절 + follow-up) | 없음(follow-up) |
| `blocked-finalize-failed: <오류>` | orchestrator | blocked 항목 마무리 중 내부 오류 | — |
| `report-failed: <오류>` | orchestrator | 보고서 작성 실패 | — |

report.md의 "Flags" 절은 이 중 상당수의 설명을 함께 보여 줍니다(`FLAG_HELP`, `sbmig/report/markdown.py`).

### 6.3 `journal.jsonl` 이벤트 어휘
각 줄은 `{"ts": <UTC ISO>, "run_id": …, "event": <이름>, …}`입니다(`Workspace.journal`).

| 이벤트 | 주요 필드 | 의미 |
|---|---|---|
| `intake` | `source`, `project_dir` | 입력 복사/해제 |
| `build_tool` | `tool`, `ok`, `reason` | Maven 여부 판정 |
| `analyze` | `case_type`, `poms`, … | 분석 완료 |
| `advisories` / `advisories_planned` / `advisories_verified` | 발견·계획·최종 검증 결과(또는 `error`) | 라이브러리 권고 |
| `effective_pom` / `effective_depmgmt` / `effective_depmgmt_after` | `ok`, `path`, `skipped` | effective-pom 캡처(as-is / restructure 후) |
| `classpath` | `module`, `ok`, `entries` | 의존성 classpath 캡처(X1 원인 추적) |
| `deptree` | `module`, `ok`, `includes`, `log`(, `roots`) | `mvn dependency:tree` 1회(`sbmig/build/deptree.py`, X1 `via` / X1b) |
| `javax_api_inventory` | `jars`, `via` | 최종 빌드 뒤 클래스패스에 남은 javax API jar와 그것을 끌어오는 직접 의존성 |
| `plan` | `steps`, `pre_handlers` | 계획 |
| `plan_advice` | `model` 또는 `error` | LLM plan advisor |
| `preflight` | 점검 항목 | Maven·부모 사전 점검 |
| `graph_node` | `node`, `phase` | LangGraph 노드 진입 |
| `phase_start` / `phase_end` | `phase`, `steps`, `ok`, `build_ok`(round 10: 예외로 끊긴 단계는 `interrupted`) | 단계 경계 |
| `phase_skipped` | `phase`, `reason` | stop_after 등으로 건너뜀 |
| `step_start` / `step_end` | `step`, `kind`, `ok` | 계획 단계 경계 |
| `handlers_preemptive` / `handler_failed` | `why`, `requested`, `changed` / `handler`, `error` | 선제 핸들러 |
| `rewrite` | `step`, `ok`, `recipes`, … | OpenRewrite 실행 |
| `rewrite_download_retry` / `rewrite_download_retry_result` | `recipes`, `ok`, `log` | X8 다운로드 재시도 |
| `rewrite_oom_retry` / `rewrite_oom_retry_result` | `recipes`, `ok`, `log` | E8 OOM 재시도 |
| `rewrite_unresolved_versions` | `step`, `poms` | 문자 그대로 버전 잔존 |
| `rewrite_parse_problems` | `step`, `files` | rewrite 플러그인이 파싱하지 못한 파일(E8b). `rewrite` 이벤트에는 개수 `parse_problems` |
| `loop_start` / `loop_end` | `phase`, `goal`, `max_iterations` / `ok`, `iterations` | build-fix 루프 경계 |
| `build` | `phase`, `iteration`, `goal`, `ok`, `exit_code` | Maven 빌드 |
| `build_negative_cache_retry` | `goal`, `module`, `artifacts`, `first_log` | round 10: 네거티브 캐시 → 같은 빌드를 `-U`로 한 번 더 |
| `triage` | `phase`, `iteration`, 클러스터 | 클러스터링 |
| `decision` | `phase`, `iteration`, 결정 | 클러스터별 결정(handler/recipe/KB/LLM/blocked…) |
| `recipe_batch` | `recipes` | 반응형 레시피 묶음 실행 |
| `fixer` | `plugin`, (`error`) | tier 2 fixer 생성 |
| `llm_call` | `record`, `role`, `model` | LLM 호출 1회(기록 켰을 때) |
| `llm_fix` | `cluster_id`, `model`, `rounds` | LLM이 답하고 패치가 파싱됨 |
| `merge_conflict_hints` | `phase`, `iteration` | 병합 충돌 참고 사본을 프롬프트에 포함 |
| `attempt` / `attempt_scored` | 시도 / 결과 점수 | 수정 시도와 재빌드 후 평가 |
| `revert` | `to` 또는 `commit`, `mode` | 회귀 시도 되돌리기 |
| `stall` / `oscillation` / `reround` | `phase`, `iteration` | 정체·진동 감지, 재라운드 |
| `blocked` / `blocked_resolved` / `only_blocked` | `id`, `reason` | 차단 등록 / 나중 단계에서 해소 / blocked만 남음 |
| `verify_loop_start` / `verify_loop_end` / `blocked_released` | `step`, `goal`, `errors`, `releasable` / `build_ok`, `attempts`, `iterations`, `released` / `id`, `reason`, `blocked_in`, `iteration`, `extra_llm_attempts` | 최종 `clean package` 실패 → 마지막 수정 루프 시작 / 끝 / 그 루프의 빌드 N에서 풀린 blocked 항목(앞 단계 `blocked_in`에서 막힌 것) |
| `llm_cooldown` / `llm_unavailable` | `phase`, `iteration` | L5 |
| `llm_config_error` | `phase`, `iteration`, `body`, `prompts` | round 10: 같은 HTTP 400 → 설정 오류, LLM 호출 중단 |
| `budget_exhausted` | `phase`, `kind` | 시간 예산 소진 |
| `test_compile_fallback` / `tests_fallback` | `test_errors` / `failing` | L7 / L7b |
| `abort` / `run_aborted` | `reason` | 중단 |
| `stop_after` | `phase` | 의도적 정지 |
| `restructure` / `restructure_failed` | `step`, `mode`, `ok` / `error` | 레이아웃·pom 변환 |
| `restructure_moves` | `step`, `moved`, `blocked_rekeyed` | round 10: 재구성 단계의 파일 이동(git 이름 변경)을 blocked 레지스트리에 반영 |
| `placeholders` | `step`, `files`, `keys` | `__SET_ME__` 자리표시자 작성 |
| `web_xml_registrations` | `path`, `spec`, `servlets`, `filters`, `listeners`, `error_pages`, `skipped`, `jsp_file_servlets`, `class` | web.xml → 생성 클래스 `WebXmlRegistrations`(W1) |
| `verify` | `step`, `target`, `ok` | 최종 빌드 |
| `kb_credit` / `kb_credit_skipped` | `phase`, `recipes`, `success` / `reason` | KB 레시피 평가(해석 실패만 있으면 건너뜀, round 10: 시도가 `partial` / `inconclusive`이면 `attempt`와 함께 건너뜀) |
| `harvest` | `phase`, `cases`, … | KB 수확 |
| `handover` | `changes`, `carried_over`, … | Handover 요약 |
| `report` | `success`, `flags`, `unresolved`, `blocked`, `llm_calls`, `outputs` | 마지막 이벤트 |
| `warning` | `message` | 내부 경고 |

### 6.4 ledger 레코드(`<kb.dir>/ledger.jsonl`, `ResolutionRecord`)
한 줄 = 한 해결(또는 차단/미해결/권고) 기록. 파일 이름은 basename만 저장하고 절대 경로·비밀값은 없습니다.

| 필드 | 의미 |
|---|---|
| `id`, `at` | 레코드 id, 기록 시각 |
| `project`, `project_fingerprint`, `run_id`, `case_type`, `phase` | 어느 프로젝트·실행·단계 |
| `signature`, `signature_generic`, `error_kind` | L1 / L2 시그니처, 오류 종류 |
| `symbol` | 오류 심볼(javac `symbol:`, `package X`, 아티팩트 `g:a`, 소유 클래스; 권고는 라이브러리) |
| `resolution` | `handler` / `plan_recipe` / `kb_recipe` / `kb_patch` / `llm_patch` / `optional_bump` / `blocked` / `unresolved` / `advisory` |
| `resolver_id` | 핸들러 id / 레시피 id / KB case id / 차단 사유(권고는 `<policy>:<recipes|->`) |
| `fix_labels` | `import-rename`, `type-rename`, `package-rename`, `annotation-replace`, `method-rename`, `method-signature-adapt`, `override-signature`, `dependency-add`, `dependency-replace`, `dependency-remove`, `dependency-version`, `plugin-version`, `pom-property`, `spring-property-rename`, `config-value`, `remove-code`, `add-code`, `generic-rewrite` |
| `replacement` | "old → new"(라벨이 의미할 때) |
| `outcome` | `improved` / `build_ok` / `blocked` / `unresolved`(권고는 `replaced` 등) |
| `files` | basename 목록 |
| `case_id`, `recipe_id`, `model`, `notes` | 관련 KB 케이스·레시피, 모델, 메모 |

조회는 `sbmig kb ledger`(1.12), 플릿 요약은 `learning.md`. 예제 레코드는 `docs/FLEET.md`.
