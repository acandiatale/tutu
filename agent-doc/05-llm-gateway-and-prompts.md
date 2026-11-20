# LLM 게이트웨이, 프롬프트, 답 형식

> 다루는 것: LLM provider(회사 게이트웨이 `company` 의 legacy / agent 모드, `anthropic`, `fake` 재생, 플러그인), `.env` 변수, 토큰 교환, 세션과 케이스 스레드, `sbmig llm-probe` 출력, 플러그인 접점, 프롬프트 파일과 사실 절, 답 프로토콜, 프롬프트 규칙, 마이그레이션 노트·권고·라이브러리 힌트, LLM 기록 파일, 사용량의 모델 이름(요청한 이름과 실제 모델), 더 센 모델, 거절 처리.
> 언제 읽나: 게이트웨이를 붙이거나 바꿀 때, LLM 이 이상하게 답한 이유를 볼 때, 프롬프트·노트를 고칠 때.
> 함께 읽을 파일: [03-run-debug-and-logs.md](03-run-debug-and-logs.md), [04-cases-scripts-kb.md](04-cases-scripts-kb.md), [07-config-and-knowledge.md](07-config-and-knowledge.md), [../docs/ADAPTERS.md](../docs/ADAPTERS.md)

## 1. 누가 LLM 을 부르나

| 호출 | 코드 | role (`LLMRequest.role`) |
|---|---|---|
| 수정 스크립트 요청(케이스마다 신규 / 보강) | `sbmig/fix/fixer.py` `LLMFixer.propose_script` | `fix`, 더 센 모델이면 `fix_escalation` |
| 구조 검토(재구성 뒤, 첫 빌드 전 한 번) | `sbmig/review/reviewer.py` | `structure_review` |
| 운영자 확인 안내(메이저 업그레이드가 `<verify>` 없이 적용됐을 때 한 번, `libraries.verify_guide_llm`) | `LLMFixer.verify_guide` | `verify_guide` |
| 계획 조언(`plan.advisor: llm` 일 때만, 기본 none) | `sbmig/plan/planner.py` | `plan_advisor` |

모든 호출은 `make_llm_client(cfg)`(`sbmig/fix/client.py`)가 만든 클라이언트 하나를 지난다. `llm.record_dir` 가 있으면 `RecordingClient` 로 감싸
호출마다 기록한다(§8).

## 2. provider (`llm.provider`)

| 값 | 무엇 |
|---|---|
| `company` (기본) | 회사 게이트웨이. `sbmig/fix/company.py`, 평범한 `requests.post(url, headers={Authorization: Bearer …}, json=…, timeout=…)` 한 번. yml 프로필·LangChain·세션 풀·TLS 설정 없음(일부러 단순하게) |
| `openai_compatible` | OpenAI 호환 `/chat/completions`(httpx) |
| `anthropic` | Anthropic Messages API. 검증용 설정 `config/llm-anthropic.yml`(키는 역시 `LLM_API_KEY` 환경 변수) |
| `fake` | 재생: `llm.base_url: "replay:<dir>"` → `<dir>/llm-*.json`([04](04-cases-scripts-kb.md) §10) |
| `plugin` 또는 그 밖의 이름 | 사내 어댑터(§6) |

### 2.1 `company` + `llm.gateway: legacy` (기본)

- URL = `LLM_BASE_URL`(전체 엔드포인트, 그대로), 헤더 `Authorization: Bearer <LLM_API_KEY>`(+ `llm.extra_headers`).
- 본문 `{"model": <LLM_MODEL>, "messages": [{role, content}, …], **llm.payload_extra}` — 기본 `payload_extra` 는 `need_origin: true`.
  max_tokens / temperature / stop 은 보내지 않는다(게이트웨이 몫). tools 없음 — 그래서 LLM 은 변경을 텍스트(`<edits>` / `<script>`)로 말한다.
- 답 본문 위치는 자동 탐지(`choices[0].message.content`, `content`, `response`, `data.content` …). 못 찾으면 `LLM_RESPONSE_PATH`(= `llm.response_path`, 예 `data.result.content`).
  사용량은 응답의 `usage` 객체에서.

### 2.2 `company` + `llm.gateway: agent` (사내 GPT agent 게이트웨이, `LLM_GATEWAY=agent`)

- 본문: `{"model": <LLM_ASSET_ID>, "messages": …, "stream": false, "async": false, "params": {"role": <모델 이름>}, "trace_id": <호출마다 새 uuid>,
  "max_tokens": …, "temperature": …, **llm.agent_extra}`. `params.role` 은 수정 호출이면 `LLM_MODEL`, 더 센 모델이면 `LLM_ESCALATION_MODEL`
  (예시 값 `build_error_fixer` / `escalation_fixer` — 게이트웨이가 실제 모델로 바꾼다). `payload_extra` 는 이 모드에서 보내지 않는다.
- URL 안의 `{asset_id}` 자리표시는 `LLM_ASSET_ID` 로 채운다(`agent_url`). 자리표시가 있는데 asset id 가 없으면 설정 오류.
- **토큰 교환**(`TokenManager`): `LLM_TOKEN_URL` 이 있으면 먼저 `POST <token_url>` 에 `Authorization: Bearer <앱 토큰>`(`LLM_APP_TOKEN`, 없으면
  `LLM_API_KEY`) → `{access_token, token_type, expires_in}`. 만료 `llm.token_refresh_margin_s`(300초) 전에 다시 받고, 401 이 오면 새 토큰으로 한 번 더.
  토큰 교환을 쓰고 `LLM_APP_TOKEN` 이 있으면 `LLM_API_KEY` 는 없어도 된다. 로그: `llm token refreshed: expires in …`.
  교환 실패(5xx, 429, 토큰 엔드포인트의 401 / 403, JSON 아닌 본문, `access_token` 없음, 전송 오류)는 `TokenExchangeError` 로 채팅 호출처럼 재시도하고,
  문구에 `status=<코드>` 또는 `cause=…` 가 붙어(`token exchange failed: HTTP 503 (status=503)`, `token exchange failed: transport (<Class>) (cause=…)`)
  수정 루프가 **LLM 장애**(쉼, `llm_unavailable`)로 센다 — 답으로 세지 않는다. 토큰 엔드포인트의 본문은 로그에 남기지 않는다
  (ERROR `llm token exchange HTTP <코드> (<n> character(s) of body, not logged: …)`).
- **세션**: sbmig 는 `session_id` 를 기본으로 보내지 않는다. 게이트웨이가 세션을 만들어 응답에 넣어 주면 `LLMResponse.session_id` 로 받고
  (`llm gateway session_id=… (returned by the gateway; sbmig sends none)` 로그), 대화를 **이어갈** 호출만 `metadata["session_id"]` 로 되돌려 보낸다.
  `thread_id` 는 보내지 않는다.

### 2.3 전송·재시도

- 재시도: 전송 오류와 재시도 상태(408, 409, 425, 429, 5xx 일부)만 `llm.max_retries`(3)번. `Retry-After` 우선(최대 120초), 없으면 1·2·4…초(조금씩 흩뜨림).
- 바쁨은 고장이 아니다: 429 와 `Retry-After` 가 붙은 502 / 503 / 504 는 `llm.busy_wait_seconds`(600)초까지 그 호출을 줄 세워 다시 보낸다(로그 `llm gateway busy` / `still busy`; 0 이면 위 재시도 규칙만). 빌드 파드 여럿이 한 게이트웨이를 같이 쓸 때를 위한 것.
- 토큰을 새로 받아 보냈는데 또 401 이면 1~5초 쉬고 `llm.auth_retries`(2)번 더 받아 보낸다. 그때 WARNING `LLM gateway rejected a token right after it was exchanged` 가 프로세스당 한 번 — 다른 파드가 같은 앱 토큰을 교환해 이 파드의 토큰이 무효가 됐을 수 있다는 뜻.
  호출 하나의 벽시계 상한은 대략 `llm.timeout_seconds`(180) + 30초.
- 실패 문구는 루프가 분류한다(형식을 바꾸지 말 것): 전송 장애 `cause=<예외 클래스>: <ASCII 메시지>`, HTTP 실패 `status=<코드>`, 빈 답 +
  `finish_reason=length` 는 요청 거절. 전송·설정 실패 연속 2번이면 실행에서 처음 한 번 `llm.cooldown_seconds`(60) 쉬고, 다시 그러면 `llm_unavailable`.
  서로 다른 프롬프트 둘이 같은 HTTP 400 본문을 받으면 설정 오류로 보고 그 실행의 LLM 호출을 멈춘다(`llm_config_error`).
- 로그 줄: `llm call provider=company role=… model=… [params.role=…] attempt n/m → POST <url> (prompt N chars, timeout Ts)`, 성공 시
  `llm call provider=company model=<요청한 이름> in=… out=… latency=… finish=…[ served=<실제 모델,…>]`(§9.1), 실패 시 `llm company transport error (attempt n/m): … cause=…`.

## 3. `.env` 변수 (이름만, `.env.example` 기준)

`LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, `LLM_ESCALATION_MODEL`, `LLM_RECIPE_MODEL`, `LLM_RESPONSE_PATH`, `LLM_RECORD_DIR`, `LLM_GATEWAY`,
`LLM_ASSET_ID`, `LLM_TOKEN_URL`, `LLM_APP_TOKEN`, `JAVA21_HOME`, `SBMIG_WEB_OUT`, `SBMIG_MAX_UPLOAD_MB`. 각 변수가 들어가는 설정 키:
`llm.base_url`, `llm.api_key_env`(키 변수의 **이름**), `llm.model`, `llm.escalation_model`, `llm.recipe_model`, `llm.response_path`,
`llm.record_dir`, `llm.gateway`, `llm.asset_id`, `llm.token_url`, `llm.app_token_env`. 키는 환경에서만 오고 yml·로그·기록에 쓰지 않는다.
모델 이름은 한 토큰이다(`KEY=name   # 주석` 의 주석은 버린다).

## 4. `sbmig llm-probe` — 새 게이트웨이에서 맨 먼저

출력 순서: ① `provider=… model=… url=… key_env=… payload_extra=… response_path=…` ② (agent / 토큰 교환이면) `gateway=… url=<채운 URL> asset_id=…
token_url=… app_token_env=… escalation_model=…` ③ `request payload: …` ④ `token exchange: …` ⑤ `sent: POST …`, `sent headers: …`(토큰 가림),
`sent body: …`, `proxies in effect for this url: … (NO_PROXY=…)`, `response headers: …`, `timeout: …` ⑥ `HTTP <status>` + 응답 JSON ⑦
`extracted reply: '…'` 또는 `CONFIG ERROR` / `TRANSPORT ERROR` / `TOKEN ERROR` / `PROBLEM: …` + `hint: …` ⑧ 마지막 줄 `LLM-PROBE ok …` /
`LLM-PROBE FAIL <kind> …`(ok 줄 끝에 응답이 알려 준 실제 모델 ` served=…`, 알 때만). 옵션: `--prompt`, `--model`, `--llm-provider`, `--payload-extra '<json>'`(payload_extra 를 **대체**, 예 `'{}'`),
`--no-system`, `--bare`(sbmig 클라이언트 없이 같은 payload 를 `requests.post`). 플릿 preflight 도 같은 호출 하나를 쓴다.

## 5. 케이스 스레드 (`llm.case_thread`, 기본 false)

- 켜면 한 케이스의 보강 요청들이 게이트웨이 세션 하나를 이어간다: 첫 요청은 전체 프롬프트(→ 세션 생성), 보강은 `LLMFixer.build_thread_turn` 이 만든
  **변화분만**(기준 스크립트의 결과, 지금 오류, 그 소스 발췌, pom, 버전 사실) user 메시지 하나로 보낸다. system 프롬프트·규칙·힌트·노트·기준 코드는 세션이 기억한다고 보고 뺀다.
  다른 케이스나 새로 작성(new)은 새 세션.
- **열린 세션은 모델을 바꾸지 못한다**(게이트웨이 팀 확인). 사다리가 상위 모델(`llm.escalation_model`: 케이스의 세 번째 요청부터, 또는 정체 뒤)로 넘어가는 요청과
  기본 모델로 돌아오는 요청은 세션을 잇지 않고 전체 프롬프트로 새 세션을 연다(`case thread for … : the model changes (… → …)` INFO). 세션의 모델은
  `ScriptProposal.session_model` → `FixAttempt.llm_session_model` → `ScriptBase.session_model` 로 전해지고, 같은 모델일 때만 잇는다. 상위 모델이 설정에 없으면 같은 모델이라 세션을 잇는다.
- 이어 보낸 턴이 4xx 로 거절되면 `llm.thread_fallback_full`(true)에 따라 새 세션에 전체 프롬프트를 다시 보낸다(`thread continuation for … rejected (HTTP …)` 경고).
- 측정 로그: `thread turn (session <id>): sent N chars in K message(s) → prompt_tokens=T` — 세션 이력이 다시 과금되는지 보려는 줄. 문맥 / 피드백 라운드도 세션이 있으면 새 user 메시지 하나만 보낸다.
- 세션이 있는 게이트웨이(agent 모드)에서만 뜻이 있다. 재생 픽스처는 끈 채로(상태 없이) 돈다.

## 6. 플러그인 접점 (바꾸면 안 되는 계약, `sbmig/core/interfaces.py`)

| 단계 | 설정 | 계약 |
|---|---|---|
| 1단계 LLM 어댑터 | `llm.provider: plugin` + `llm.plugin: "pkg.module:attr"`(또는 entry point 그룹 `sbmig.llm_adapters` 의 이름), `llm.plugin_options` | `LLMAdapterLike.complete(request: LLMRequest) -> LLMResponse`. `LLMRequest`: `messages`(`ChatMessage` role/content), `model`, `max_tokens`, `temperature`, `stop`, `role`, `metadata`(비밀 없음). `LLMResponse`: `content`, `model`(요청한 이름), `served_models`(응답이 알려 준 실제 모델), `input_tokens`, `output_tokens`, `finish_reason`, `session_id`. 저수준 `LLMClientLike.complete(messages, *, model, max_tokens, temperature, stop, role, metadata)` 도 받는다 |
| 2단계 수정기 | `fix.plugin: "pkg.module:attr"`(또는 그룹 `sbmig.fixers`), `fix.plugin_options`, CLI `--fix-plugin` | `FixerLike.propose_script(ws, cluster, ctx: FixContext) -> ScriptProposal`. 예외 대신 `ScriptProposal(error=…)`. 적용·커밋·채점·되돌리기는 언제나 루프 몫 |

로딩은 `sbmig/llm/registry.py`. 자세한 것은 [../docs/ADAPTERS.md](../docs/ADAPTERS.md).

## 7. 프롬프트

| 파일 | 무엇을 묻나 |
|---|---|
| `sbmig/fix/prompts/script_system_v1.md` | 수정 system 프롬프트: 케이스 하나에 원인 / 해결 / 편집 / 재사용 스크립트로 답하라, 규칙 14개, `<edits>` 문법, 변경 설명 형식, 도우미 API `p` 표, 출력 형식, `<need_files>` / `<need_grep>`, `<blocked>` |
| `sbmig/fix/prompts/script_user_v1.md.j2` | user 프롬프트 틀: 신규(편집 + 스크립트 둘 다) / 보강(기준 스크립트를 대체할 **완전한** 새 스크립트, 기준 변경이 트리에 남았는지 되돌렸는지) + 사실 절들 + 설명 언어 |
| `sbmig/fix/prompts/verify_system_v1.md` | 메이저 업그레이드 뒤 운영자 확인 줄만 `<verify>` 안에 쓰라 |
| `sbmig/review/prompts/review_v1.md` | 구조 검토: 부모·모듈·패키징·사내 아티팩트·의존성 버전·인코딩·필터링 바이너리를 회사 규칙에 비추어 보고 `<review>` JSON(verdict ok / issues, severity critical / warning), critical 은 직접 `<edits>` 로 고쳐라(Java 컴파일 문제는 보고하지 말 것) |

`llm.prompt_version`(v1)이 파일 이름의 `_v1` 이다. 검토 입력은 `out/<run id>/logs/review-input.md` 로 남는다(`review.save_input`). `review.ask_script`
가 false(기본)면 검토는 편집만 받는다; critical 수정은 `review: fix critical structure issues (n)` 으로 커밋되고 `review.invariant_check` 가 재구성 불변조건을 다시 본다.

### 7.1 수정 프롬프트의 사실 절 (`sbmig/fix/context.py` `ContextPacker.pack_script`, 대략 이 순서)

`Case`(머리말: as-is 프로필, 목표, 케이스) → `Project after the restructure`(재구성 사실, 자르지 않음: boot / back / front 모듈과 부모·패키징, 생성한
파일, 사내 아티팩트 규칙 — `sbmig/loop/layout_facts.py`) → `Errors of this case (N)` → 보강이면 `Script to extend (…)` + `What … did in this project` →
`POM (<path>)` 발췌(parent / properties / dependencyManagement / dependencies / build) → `Related project types named in the errors` → `Reference files`
(병합에서 빠진 사본, 읽기 전용) → `Dependency versions (what the repository has; policy: same major first, see rule 14)` → `Library migration guidance
(what to migrate TO)`(권고) → `Module merge notes` → `Knowledge-base hints (similar cases)` → `Previous attempts on this case (they did NOT resolve it — do not
repeat them)` → `Other error cases in this build` → `Plan notes`(transform / restructure / warning / msa / followup 으로 시작하는 줄만) → `Migration note: <제목>`
(최대 4개) → `Files with errors (N)` → `Source (exact text around the error lines; …)`. 예산은 `llm.max_context_chars`(60000); 넘치면 선택 절부터 줄이고
"Context was shortened" 를 붙인다. LLM 이 `<need_files>` / `<need_grep>` 로 더 달라면 `llm.max_context_rounds`(2)번까지 내준다(요청 파일은 줄 수와 글자 수로 자르고 `llm.max_context_chars` 를 지킨다;
머리 `// <path> lines 1-N (whole file, C characters: cut to fit the context budget)`, 자국 `// … N characters omitted (use <need_grep> to locate code there) …`).

사내 프레임워크 프로젝트(범위 `jdk-jakarta`)는 기본으로 LLM을 부르지 않는다(`framework.fix_loop: false`). `true`로 켰을 때는 `Project after the restructure` 대신 `Scope of this run`(`FixContext.scope_facts`)이 들어간다:
이번 실행이 Java 21과 jakarta만 한다는 것, 프레임워크 아티팩트마다 버전을 바꾸거나 지우지 말라는 줄, 해도 되는 것과 안 되는 것(Spring Boot·
회사 부모 추가, `<parent>` 변경, 모듈 이동 금지). 시스템 프롬프트도 같은 파일 안의 줄인 범위용 소개와 규칙을 쓰고, 버전 사실은
`parent chain: manages g:a at v`(프로젝트 자기 부모)로 온다. `full` 범위의 프롬프트는 바뀌지 않는다.

취약점 조치가 실패한 후보에 돌리는 수정 루프([11](11-remediations.md) 2절)는 같은 프롬프트에 `plan_notes` 한 문단을 더한다: 올린 버전
(`g:a old -> new`), 새 버전에 맞게 소스를 고칠 것, `pom.xml`을 고치지 말고 파일을 만들거나 지우지 말 것(`sbmig/remediations/fixer.py`). 이 호출의
모델별 사용량은 `FixResult.usage`(가격 포함)에 남고, 결과 줄 `llm=`, `GET /api/runs/{id}`, 관리자 페이지가 `fixer.fix_usage`로 더한다(이관 `report.json`에는 없다).

"Dependency versions" 절의 사실 줄 모양: `new parent chain: manages g:a at v`, `target Boot BOM: manages g:a at v`, `as-is build: g:a v`,
`parent chain property: name=value`, `managed artifacts under <group> …`, jakarta 판정 `verdict: …` / `approved`, 그리고 동봉 jar 사실
"the project bundles `<jar>` which provides package `<p>` …". 저널 `library_hints` 의 `lines` 가 같은 줄이다.

### 7.2 답 프로토콜 (`sbmig/fix/script_protocol.py` `parse_script_response`)

| 태그 | 뜻 |
|---|---|
| `<analysis>` | 원인(KB 에 남음, 보고서 「직접 수정한 파일」의 "수정 이유") |
| `<solution>` | 해결 방법 한 문단 |
| `<verify>` | 운영자 확인 줄 — 메이저 버전 변경·라이브러리 교체 뒤에만, 그때는 필수. 이것만 있는 답은 답이 아님 |
| `<edits>` | 이 프로젝트에 바로 적용할 JSON 목록. op: `replace`(path, old, new, count) / `write` / `create`(path, content) / `delete` / `move`(path, to) / `pom`(편집기 호출), 항목마다 `desc` 한 문장 |
| `<changes>` | 스크립트가 바꾸는 것의 설명 목록 `{path, where, change}`(스크립트면 필수, 없으면 `[sbmig] the answer did not describe its changes`) |
| `<script>` | `def fix(p)` 재사용 스크립트(KB 에 저장) |
| `<no_script>` | 스크립트로 만들 수 없는 이유 — 그때는 `<edits>` 가 수정(출처 `llm-edit`) |
| `<need_files>` / `<need_grep>` | 문맥 요청 |
| `<blocked>` | 프로젝트 안에서 못 고치는 이유 한 문장(400자) → 거절 |

`<analysis>`, `<solution>`, `<changes>`·`desc` 는 `describe_language`(`sbmig/script/notes.py`)가 정한 언어로 쓰라고 프롬프트가 요구한다:
`report.language: ko` 면 한국어. 구조 검토 프롬프트의 problem / hint / analysis 도 같다. 전에 KB 에 저장된 설명은 그 언어 그대로다.

파서는 관대하고 예외를 내지 않는다(펜스, CRLF, 닫는 태그 빠짐, JSON 의 꼬리 쉼표·주석·작은따옴표 수리). 산문·편집 블록을 먼저 떼어 내므로 JSP 편집 속
`<script>` 를 스크립트로 오인하지 않는다. 속성이 붙은 여는 태그(`<script type="python">`)도 알아보고, 블록이 여럿이면 마지막 완전한 것을 쓴다(WARNING
`several <script> blocks (N); took the last complete one`). 쓸 수 없는 스크립트·못 읽는 편집이면 피드백 한 번. 진짜 정적 검사는 `sbmig/script/check.py`.
**토큰 한도에서 잘린 답**(`finish_reason` length / max_tokens)에서 `<script>` / `<edits>` 가 닫히지 않았으면 쓰지도 저장하지도 않고, 피드백 라운드에서
짧은 답을 청한다. 또 잘리면 그 시도는 답 문제로 실패한다(`the answer was cut off at the output token limit (finish_reason=<r>) before <script> closed`).
피드백 라운드가 실패해도(503 / 429 등) 첫 답의 쓸 수 있는 편집은 쓴다(메모 `[sbmig] feedback round failed (llm-error: …); the first answer's valid edits are used`).

### 7.3 프롬프트가 주는 규칙 (system 프롬프트 "Rules" 1–14 요지)

1 스크립트 하나가 케이스의 모든 파일을 `p.errors` 로 고친다 · 2 멱등 · 3 오류가 요구하는 것만 바꾸고 동작 보존, `<parent>` 변경·Java / Boot 내리기 금지 ·
4 정확한 치환 우선, 정규식은 고정 · 5 `p` 로만 작업 · 6 방어적으로(없으면 로그) · 7 모든 변경 `p.log` · 8 목표 스택(`jakarta.*`, JDK 의 `javax.sql` 등은 유지,
Jackson 3 규칙) · 9 런타임 걱정은 `MIGRATION-TODO` 주석 · 10 "Library migration guidance" 가 있으면 제거된 라이브러리를 다시 넣지 말고 이름 붙은 대체로 이식 ·
11 안 본 코드는 추측하지 말고 `<need_files>` · 12 보강은 **완전한** 새 스크립트, EOL 라이브러리 대체처럼 프로젝트 고유면 `<no_script>` + `<edits>` ·
13 앞선 시도와 다르게 · 14 **버전**: 메이저를 바꾸지 않고 라이브러리를 스스로 바꾸지 않는다; 사실 순서는 새 부모 체인 → 목표 Boot BOM(→ 버전 없음),
`as-is build`, `parent chain property` 는 `${name}` 으로; 사실이 없으면 `"version": null`(sbmig 가 채움), 버전을 몰라 거절·포기하지 말 것; javax 라이브러리는 같은
메이저의 jakarta 릴리스, 없을 때만 상위 메이저 + `<verify>`. 끝으로 "좌표·버전을 지어내지 말 것", "알 수 없는 버전은 차단 사유가 아니다".

## 8. 노트, 권고, 라이브러리 힌트

- **마이그레이션 노트** `knowledge/notes/*.md` + `knowledge/notes/index.yml`(파일, 제목, `keywords` 정규식, `cases`, `phases`, `always`). 점수 = 오류 본문(메시지·심볼·
  위치·raw)에 맞은 키워드 ×3 + import / pom 에만 맞은 키워드 ×1. 케이스 종류로 거르고, 상위 4개(`DEFAULT_MAX_NOTES`)가 `Migration note: <제목>` 절로 간다.
  노트의 `fix-loop:` 절은 규칙 10 처럼 읽힌다(예 `knowledge/notes/spring-mobile.md`: 바이트코드 변환이 아니라 호환 대체 코드를 쓰라).
- **권고** `knowledge/libraries/advisories.yml`(+ `advisories.extra_files`): EOL·관리 안 되는 라이브러리(hystrix, springfox …). 정책은 안내뿐(`guide`) —
  빌드를 막지 않는다. 오류가 그 라이브러리의 패키지·아티팩트·대체 아티팩트를 언급하면 `sbmig/advisory/hints.py` 가 "Library migration guidance" 줄을 더하고,
  보고서에 남는다(ko: 「확인해 주세요」 맨 앞에 권고마다 한 항목, `title_ko` / `summary_ko` / `followup_ko` / `replacement_ko` 문구;
  en: Handover 의 "Libraries needing your attention").
- **라이브러리 힌트**(`_library_hints`, `libraries.lookup`): 케이스 뒤의 라이브러리를 표(`knowledge/libraries/jakarta_libraries.yml` + `kb-v2/libraries.yml`)와
  저장소(maven-metadata.xml, pom, jar 스캔)로 조회해 "Dependency versions" 절을 만든다. 저장소 목록과 조회 결과는 `sbmig repo-probe` 로 본다.

## 9. LLM 기록 파일과 응답 경로

`llm.record_dir`(= `LLM_RECORD_DIR`) 아래 호출마다 `llm-NNNN.json`: `messages`, `response`, `session_id`, `model`, `usage`, `finish_reason`, `latency`,
`prompt_hash`, `error`, `role`, `metadata`. 키·헤더는 없다. 실패한 호출도 `error` 와 함께 남는다. 저널 `llm_call` 이 기록 경로를, `llm_script` 가 모드·호출 수·토큰을 남긴다.
지우기는 `sbmig clean --records <dir>`. `LLM_RESPONSE_PATH` 는 자동 탐지가 실패할 때만 쓴다(`sbmig llm-probe` 가 응답 JSON 을 그대로 보여 준다).

### 9.1 사용량의 모델 이름 (요청한 이름과 실제 모델)

- 사용량 행(`LLMUsage.model`, 보고서 `llm_usage_by_model`, 비용 표 `ModelCost.model`)의 이름은 **호출할 때 요청한 이름**이다. agent 게이트웨이는
  `params.role`(예 `build_error_fixer` / `escalation_fixer`, `CompanyClient.asked_name`; 맨 위 `model` 은 asset id), legacy 는 본문의 `model`.
  `LLMResponse.model` 도 같은 뜻이다(내장 클라이언트와 어댑터 감싸개가 `named_response` 로 맞춘다).
- 게이트웨이가 알려 준 실제 모델(자리 표시 값이 아닌 맨 위 `model`, `usage.by_model[].model`)은 `served` 로 옆에 둔다(`LLMResponse.served_models`,
  `LLMUsage.served`, `ModelCost.served`; 비면 JSON 에 쓰지 않음). 저널 `llm_script` / `llm_verify_guide` 의 `served`, `llm call` 로그 줄 끝 ` served=a,b`,
  보고서 모델 칸 `` `build_error_fixer` (실제 모델) `` 가 같은 값이다. 행 하나의 토큰은 실제 모델별로 나누지 않는다.
- `unknown`, `?`, `-`, 빈 값 같은 자리 표시는 이름으로 쓰지 않는다(`PLACEHOLDER_MODEL_NAMES`). 요청한 이름을 하나도 모르면 `gateway-default`.
- 가격(`llm.pricing`, `sbmig/report/cost.py` `price_for_usage`): 요청한 이름으로 먼저, 없으면 실제 모델 이름을 차례로 찾는다. role 이름 키가 있으면 그것이 이긴다.
- 관리자 페이지: 요청한 이름 옆에 실제 모델을 흐리게, 실행마다 「모델」 칸. 모델 이름을 기록하기 전의 예전 실행에서 이름이 `unknown` / `?` / 빈 행은
  한 줄 「이름 없음」(이전 실행)으로 묶는다. 그 실행의 `sbmig.log` 에서 이름 없는 응답 줄마다 앞에 `params.role=` 요청 줄이 있고 호출 수와 토큰이 보고서와
  꼭 같을 때만 role 이름으로 나눈다(`sbmig/web/admin.py`). 운영 화면은 [09](09-deploy-and-ops.md) 1절.

## 10. 더 센 모델과 거절

- **더 센 모델**: 루프가 `use_escalation_model` 을 켜는 때 = 정체 단계 ≥ `loop.stall_limit`(2), 또는 케이스의 세 번째 LLM 요청부터. 그때 `llm.escalation_model`
  (비면 `llm.model`)을 쓰고, agent 모드에서는 그 값이 `params.role` 이 된다. 시도 메모 `escalation model requested (…)`.
- **거절**(`<blocked>`, 또는 편집도 스크립트도 없는 답): `script.max_declines_per_case`(1)번 뒤 차단(`needs_owner`, 또는 트리아지가 정한 구체 사유).
  거절이 sbmig 의 사실과 모순이면 받지 않는다 — 저장소에서 **확인된** jakarta 빌드가 있거나, 트리 안 동봉 jar 가 그 패키지를 제공할 때. 그때는 거절로 세지 않고
  보통 실패로 기록해 그 사실과 함께 다시 묻는다(저널 `decline_rejected`, 로그 `LLM 의 거절을 받지 않음: …`).
- LLM 불가(`llm_unavailable`)나 예산 소진(`budget_exhausted`)으로 막힌 케이스에도 LLM 없는 사전 시도는 돈다([04](04-cases-scripts-kb.md) §5).

## 확인하는 법

```
grep -n "    def payload\|\"stream\"\|session_id\|trace_id\|thread_id" sbmig/fix/company.py | head -20
grep -n "class TokenManager" -A10 sbmig/fix/company.py; grep -n "def agent_url" -A10 sbmig/fix/client.py
grep -n "provider ==\|BUILTIN_LLM_PROVIDERS" sbmig/fix/client.py sbmig/core/config.py
grep -n "case_thread\|build_thread_turn\|thread turn" sbmig/fix/fixer.py
grep -n "^class \|    def complete\|    def propose_script" sbmig/core/interfaces.py
grep -n "_Section(\"" sbmig/fix/context.py; grep -n "DEFAULT_MAX_NOTES\|PROMPT_PLAN_NOTE_PREFIXES =" sbmig/fix/context.py
grep -n "^[0-9]*\. \|^1[0-4]\. " sbmig/fix/prompts/script_system_v1.md   # 규칙 번호
grep -n "<[a-z_]*>" sbmig/fix/script_protocol.py | head -40
grep -n "rec = {" -A14 sbmig/fix/client.py; grep -n "escalate = " sbmig/loop/fixloop.py
grep -n "def named_response\|def served_models_of\|def served_log_suffix" sbmig/fix/client.py; grep -n "def price_for_usage" sbmig/report/cost.py
grep -n "def _decline_contradiction" -A20 sbmig/loop/fixloop.py
.venv/bin/sbmig llm-probe --help
.venv/bin/python -m pytest tests/docs/test_agent_doc.py -p no:cacheprovider -q
```
