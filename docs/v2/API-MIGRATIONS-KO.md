# sbmig 외부 접수 API(`/api/migrations`) — remediation 연동 구현 지시서

> 2026-11-06. 용도: **사내 LLM(Sonnet 5.5 급)에게 이 문서와 저장소를 주고 구현을 맡기는 지시서**다. remediation 서비스가
> 사람 대신 이관을 접수하고 결과를 가져가도록, 지금의 웹 UI 가 쓰는 `/api/runs*` 옆에 remediation 계약으로 된 접수 창구를
> 하나 더 얹는다. 새 엔진·큐·DB 는 만들지 않는다. 구현이 끝났다는 기준은 §7 체크리스트다.
> 배경: `docs/INTERFACES.md` "k8s Job dispatch mode" 절, `deploy/README.md` §3, `agent-doc/09-deploy-and-ops.md`.

## 0. 한 문단 요약과 규칙

sbmig 웹 서버(`sbmig serve` / `uvicorn sbmig.web.app:app`)는 업로드한 zip 하나를 실행(run) 하나로 돌린다. 실행은 `RunManager`
가 만들고(`sbmig/web/runs.py`), FIFO 로 동시에 `SBMIG_JOB_MAX_PARALLEL` 건까지(기본 1; in-process 는 1) 돌며, 모드에 따라 이 프로세스 안(in-process), 서브프로세스(local-job),
쿠버네티스 Job(k8s-job)에서 돈다. 진행과 결과는 전부 **실행 폴더** `<out>/<run id>/` 의 파일(`journal.jsonl`, `sbmig.log`,
`report.md`, `report.json`, `result.zip`, `web-status.json` 또는 `job-status.json`)로 남고, 상태 조회는 그 파일을 읽는다.
이 작업은 그 위에 다음을 더한다: `POST /api/migrations`(접수, `Idempotency-Key`), `GET /api/migrations/{id}`(remediation
어휘로 번역한 상태), 결과 파일·이벤트·취소 경로. 실행 자체는 **한 줄도 바뀌지 않는다**.

구현하는 LLM 이 지킬 규칙:

1. **`RunManager` 를 그대로 쓴다.** 접수는 `RunManager.start_file`, 상태는 `status()`, 취소는 `stop()`, 이벤트는 `events()`.
   큐·스레드·Job 생성 코드를 새로 쓰지 않는다. 모드(in-process / local-job / k8s-job)를 새 코드가 구분하지 않는다.
2. **기존 `/api/runs*` 라우트와 화면(`sbmig/web/static/index.html`)의 동작을 바꾸지 않는다.** 두 라우트가 같이 쓸 헬퍼로
   코드를 빼는 것은 된다(기존 `tests/web/test_app.py` 가 그대로 통과해야 한다).
3. **DB 금지, 파일만.** 멱등 기록은 `<out>/_idempotency/` 아래 JSON 파일 하나씩. `_` 로 시작하는 폴더는 `RunManager.list_runs`
   가 실행으로 보지 않는다(이미 `_uploads` 가 그렇다).
4. **새 pip 의존성 금지**(사내망은 오프라인). 그래서 본문은 `application/zip` 원본 스트림이다(§2.1). multipart 가 꼭 필요하면
   `python-multipart` 를 `pyproject.toml` 과 `deploy/docker/pip-deps.py` 에 더하는 별도 승인이 필요하다.
5. **외부 접수는 서버 기본 설정으로만 돈다.** `configs` / `stop_after` / `llm_provider` 는 받지 않는다(400). 회사 고유값은
   코드에 넣지 않는다.
6. **한국어 주석 규약**(`docs/v2/CODE-COMMENTS-KO.md`): 새 모듈의 docstring 은 한국어 블록으로 시작하고, 공개 최상위 클래스·함수의
   docstring 은 한국어가 먼저, 영어 원문은 그 뒤. `tests/docs/test_korean_comments.py` 가 검사한다.
7. **테스트가 계약이다.** `tests/web/test_migrations_api.py` 를 §4 대로 쓰고, `python -m pytest -m "not integration" -o addopts=""`
   전체와 `tests/docs` 가 통과해야 끝이다. 커밋은 사람이 한다(지시가 따로 있으면 그대로).

## 1. 지금 있는 것 — 먼저 읽을 파일

| 파일 | 무엇 | 쓸 것 |
|---|---|---|
| `sbmig/web/app.py` | FastAPI 앱. `create_app(out_dir, repo_root, max_upload_mb, token, default_configs, open_network, mode, job_client)`. 라우트: `GET /api/configs`, `GET /api/runs`, `POST /api/runs`(zip 본문을 `<out>/_uploads/.incoming-*.part` 로 스트리밍, `PK` 서명과 상한을 조각마다 검사 → `mgr.start_file`), `GET /api/runs/{id}`, `GET /api/runs/{id}/events`(SSE), `POST /api/runs/{id}/stop`, `GET /api/runs/{id}/files/{name}`(`SERVED_FILES` 화이트리스트, `?download=1`). `_check_id`(`RUN_ID_RE`, 틀리면 400), `TokenGuard` 미들웨어 | 스트리밍·파일 제공 코드를 헬퍼로 빼서 재사용. 새 라우트 등록 지점 |
| `sbmig/web/runs.py` | `RunManager(out_dir, repo_root, max_finished_runs, default_configs, mode, job_client, job_template)`. `start_file(incoming, name, configs, stop_after, llm_provider) -> run_id`(ValueError = 잘못된 요청; 성공하면 `incoming` 은 실행 몫), `status(run_id) -> dict`(`exists`, `state` queued/running/finished/unknown/missing, `exit_code`, `error`, `stop_requested`, `node`, `job`, `success`, `blocked`, `unresolved`, `open`, `resolved`, `llm_calls`, `files`, `mode`), `stop(run_id) -> bool`, `events(run_id)`, `run_dir(run_id)`, `default_configs()`, `incoming_path()`, `list_runs()`, `effective_mode`. 상수 `RUN_ID_RE`, `SERVED_FILES`, `WEB_STATUS_FILE`, `JOB_STATUS_FILE`, `EXIT_ABORTED` | 그대로 호출. `start_file` 에 `extra` 인자 하나만 더한다(§3.2) |
| `sbmig/cli.py` | 종료 코드 `EXIT_SUCCESS 0`(빌드 통과, 차단 없음), `EXIT_INCOMPLETE 2`(끝났지만 차단·미해결 있음), `EXIT_ABORTED 3`(중단: 사전 점검·ADF·부모 해석 불가·내부 오류·사용자 멈춤) | 상태 번역표(§2.2) |
| `sbmig/web/k8s.py`, `sbmig/web/localjob.py` | Job 모드 클라이언트 | 읽기만. 건드리지 않는다 |
| `tests/web/test_app.py` | 가짜 실행(`fake_body` 가 `RunManager._run_thread_body` 를 대신해 보고서 파일을 쓴다), `client` 픽스처, `_zip_bytes`, `_start`, `_sse`, `_manager` | 새 테스트가 그대로 가져다 쓴다 |
| `tests/web/test_job_mode.py` | `FakeJobClient` / `FakePod` 로 k8s-job 모드를 흉내 | 모드 무관성 테스트 하나에 재사용 |
| `docs/INTERFACES.md`(v2 절 끝), `deploy/README.md` §3 표, `agent-doc/09-deploy-and-ops.md` 끝 | 이 API 가 "다음" 으로 적혀 있는 자리 | §5 대로 갱신 |

실행이 끝났다는 신호: in-process / local-job 은 작업 스레드가 `finished` 이벤트와 `web-status.json`(`exit_code`, `error`,
`stop_requested`, `last_node`)을 쓴다; k8s-job 은 파드의 `run-job.sh` 가 `job-status.json {exit_code, finished_at}` 을 쓴다.
`status()` 는 메모리에 없는 실행(서버 재시작 뒤)도 이 파일들과 `report.json` 으로 답한다 — 그래서 새 API 도 재시작에 안전하다.

## 2. 계약 (remediation ↔ sbmig)

§2.8 의 "확인 필요" 항목은 remediation 팀의 답에 따라 바뀔 수 있다. 바뀌어도 바뀌는 자리는 한 곳씩(상수·번역표)이어야 한다.

### 2.1 접수 — `POST /api/migrations`

```
POST /api/migrations?project_name=<이름>&name=<zip 파일 이름(선택)>
Idempotency-Key: <클라이언트가 만든 고유 키>
Content-Type: application/zip
Authorization: Bearer <SBMIG_WEB_TOKEN>        (서버에 토큰이 설정돼 있을 때만)

<zip 바이트 그대로>
```

| 항목 | 규칙 |
|---|---|
| `Idempotency-Key` 헤더 | **필수.** 앞뒤 공백 제거 후 `^[A-Za-z0-9._:-]{1,128}$`. 없거나 틀리면 400 `{"error": "Idempotency-Key header required"}` / `"bad Idempotency-Key"` |
| `project_name` 쿼리 | **필수.** `^[A-Za-z0-9._-]{1,64}$`(폴더 이름·라벨로 쓰일 수 있는 범위). 없거나 틀리면 400 |
| `name` 쿼리 | 선택. 원본 zip 파일 이름(표시용). 없으면 `<project_name>.zip`. `start_file` 의 `name` 으로 넘긴다(안전한 글자로 걸러진다) |
| 본문 | zip 원본. 기존 `/api/runs` 와 똑같이 `<out>/_uploads/.incoming-*.part` 로 스트리밍하며 `PK` 서명(400 `body is not a zip file`)과 상한(413, `SBMIG_MAX_UPLOAD_MB`)을 조각마다 검사하고, 동시에 **sha256** 을 누적한다(지문) |
| 받지 않는 것 | `configs`, `stop_after`, `llm_provider` 쿼리가 있으면 400 `{"error": "configs/stop_after/llm_provider are not accepted on this endpoint"}`. 설정은 항상 `mgr.default_configs()` |
| 지문(fingerprint) | `sha256(zip bytes)` + `project_name`. 같은 키로 다른 지문이 오면 422 `{"error": "Idempotency-Key reused with a different request"}` |
| 응답 202 (새 실행) | 아래 본문 |
| 응답 200 (재전송) | 같은 키·같은 지문: 새 실행을 만들지 않고 **처음 응답과 같은 본문**(상태 부분만 현재 값) + 헤더 `Idempotency-Replayed: true` |

202 본문(예):

```json
{
  "migration_id": "web-20261106-091500-3fa2c1",
  "run_id": "web-20261106-091500-3fa2c1",
  "project_name": "order-api",
  "name": "order-api.zip",
  "state": "queued",
  "accepted_at": "2026-11-06T00:15:00.123456+00:00",
  "links": {
    "self":   "/api/migrations/web-20261106-091500-3fa2c1",
    "events": "/api/migrations/web-20261106-091500-3fa2c1/events",
    "report": "/api/migrations/web-20261106-091500-3fa2c1/files/report.md",
    "result": "/api/migrations/web-20261106-091500-3fa2c1/files/result.zip",
    "cancel": "/api/migrations/web-20261106-091500-3fa2c1/cancel"
  }
}
```

`migration_id` 는 run id 와 같은 값이다(새 식별자 체계를 만들지 않는다). `links` 는 상대 경로다(앞단 프록시가 바뀌어도 맞게).

### 2.2 상태 — `GET /api/migrations/{id}`

`RunManager.status(id)` 를 remediation 어휘로 번역한다. id 는 `_check_id`(400), `exists` 가 false 면 404.

| `status()` 의 `state` | `exit_code` / 그 밖 | API `state` | 뜻 |
|---|---|---|---|
| `queued` | – | `queued` | 접수됨, 자리를 기다림(`status()` 의 `queue.position` 이 대기 순번) |
| `running` | – | `running` | 돌고 있음(`node` 가 지금 단계, k8s 면 `job.phase` 도) |
| `finished` | 0 | `succeeded` | 최종 `mvn clean package` 통과, 차단 없음 |
| `finished` | 2 | `finished_with_issues` | 끝났지만 차단·미해결 오류 있음 — 보고서 필요(`blocked`, `unresolved`, `open`) |
| `finished` | 3 또는 `error` 있음 | `failed` | 중단(사전 점검·ADF·사내 부모 해석 불가·내부 오류). `error` 에 사유 |
| `finished` | `stop_requested` true | `cancelled` | 사용자가(또는 API 가) 멈춤. `failed` 보다 우선 |
| `finished` | `exit_code` 없음(`report.json` 만) | `success` 가 true 면 `succeeded`, 아니면 `finished_with_issues` | 아주 옛 실행 |
| `unknown` | – | `unknown` | 폴더는 있는데 결과 파일이 없음(서버가 실행 중에 죽음). 재접수 대상 |
| `missing` | – | 404 | 모르는 id |

번역표는 **`sbmig/web/migrations.py` 의 dict 한 곳**(`MIGRATION_STATES` + `translate_status`)에만 둔다. remediation 의 상태 이름이
다르면 그 dict 만 바꾼다.

본문(예, 끝난 실행):

```json
{
  "migration_id": "web-20261106-091500-3fa2c1",
  "project_name": "order-api",
  "state": "finished_with_issues",
  "exit_code": 2,
  "success": false,
  "blocked": 1, "unresolved": 0, "open": 1, "resolved": 7,
  "llm_calls": 9,
  "node": "finalize",
  "job": {"name": "sbmig-web-20261106-091500-3fa2c1", "phase": "Succeeded", "pod": "sbmig-web-…-x7k2q"},
  "error": null,
  "stop_requested": false,
  "mode": "k8s-job",
  "accepted_at": "2026-11-06T00:15:00.123456+00:00",
  "files": ["report.md", "report.json", "result.zip", "sbmig.log", "journal.jsonl", "errors.md", "errors.jsonl"],
  "links": {"self": "…", "events": "…", "report": "…", "result": "…", "cancel": "…"}
}
```

`status()` 가 주는 키는 그대로 지나가게 두고(`success`, `blocked`, `unresolved`, `open`, `resolved`, `llm_calls`, `node`, `job`,
`error`, `stop_requested`, `mode`, `files`), `state` 만 번역하고 `migration_id` / `project_name` / `accepted_at` / `links` 를 더한다.

### 2.3 결과 파일 — `GET /api/migrations/{id}/files/{name}`

`/api/runs/{id}/files/{name}` 과 **같은 함수**를 부른다(리다이렉트가 아니라 — 인증 헤더를 따라오지 않는 클라이언트가 있다). 허용
이름은 `SERVED_FILES` 의 키만(`report.md`, `report.json`, `result.zip`, `sbmig.log`, `journal.jsonl`, `changes.patch`, `plan.json`,
`profile.json`, `errors.md`, `errors.jsonl`); 그 밖은 404 `unknown file`, 아직 없는 파일도 404. `?download=1` 이면 첨부로.

### 2.4 진행 이벤트 — `GET /api/migrations/{id}/events`

`/api/runs/{id}/events` 와 같은 SSE 스트림(`node` / `journal` / `log` / `report` / `finished` 이벤트). remediation 이 폴링만 쓰면 안
써도 된다; 경로만 열어 둔다(같은 함수).

### 2.5 취소 — `POST /api/migrations/{id}/cancel`

`mgr.stop(id)`. 멈춤은 **그래프 노드 사이**(그리고 수정 루프의 빌드 라운드 사이)에서 먹는다: 진행 중인 Maven 빌드·LLM 호출은 끊지
않는다. k8s-job 이면 실행 폴더에 `stop.request` 가 쓰이고 유예(`SBMIG_JOB_STOP_GRACE_S`) 뒤 Job 이 지워진다.

| 경우 | 응답 |
|---|---|
| 돌고 있거나 대기 중 | 200 `{"migration_id": …, "state": "cancelling"}` |
| 이미 끝남(`stop()` 이 False 이고 `status().state` 가 finished) | 409 `{"error": "already finished", "state": <번역된 상태>}` |
| 모르는 id | 404 |

### 2.6 목록 — `GET /api/migrations?project_name=…&limit=50` (선택, 두 번째 단계)

`mgr.list_runs()` 를 번역해 최신순으로. `migration.json` 이 있는 실행만(화면에서 올린 실행은 섞이지 않게), `project_name` 으로
걸러서. 첫 구현에서 빼도 된다.

### 2.7 인증

새로 만드는 것 없음. `TokenGuard` 가 모든 경로에 그대로 적용된다: 서버에 `SBMIG_WEB_TOKEN` 이 있으면 `Authorization: Bearer <토큰>`
(또는 `?token=`), 없으면 루프백만, `SBMIG_WEB_OPEN=1` 이면 사내망 전체(2026-11-05 결정: 사내망 전용 페이지).

### 2.8 remediation 팀에 확인할 것 (답이 오면 상수·번역표만 바꾼다)

1. 필드 이름이 정확히 `project_name` / `name` 인지, 그 밖에 보내는 필드(요청자, 티켓 번호 …)가 있는지 → 있으면 `migration.json` 과
   저널 `web_run` 줄에 그대로 보존(`extra`).
2. zip 을 **본문 원본**으로 보낼 수 있는지, multipart 만 되는지(규칙 4).
3. 상태 어휘: `queued / running / succeeded / finished_with_issues / failed / cancelled / unknown` 을 그대로 쓸지.
4. 끝났을 때 콜백(webhook)이 필요한지, 폴링으로 충분한지. 콜백은 이 단계에서 만들지 않는다(§3.5).
5. 키 재사용 충돌의 코드(422 로 둠; 400 이나 409 를 원하면 상수 하나).
6. `Idempotency-Key` 보존 기간. 기본은 "지우지 않는다"(실행 폴더와 같이 남는다; 실행 폴더가 사라진 키는 §3.1 의 `expired`).

## 3. 구현 설계 (파일 단위)

```mermaid
sequenceDiagram
    participant R as remediation
    participant A as app.py (라우트)
    participant M as migrations.py
    participant RM as RunManager
    R->>A: POST /api/migrations (zip, Idempotency-Key, project_name)
    A->>A: _receive_zip: 스트리밍 + PK/상한 검사 + sha256
    A->>M: accept(key, fingerprint, incoming, ...)
    M->>M: lock; lookup(key)
    alt 기록 있음, 같은 지문
        M-->>A: replay (200)
    else 기록 있음, 다른 지문
        M-->>A: 422
    else 기록 없음
        M->>RM: start_file(incoming, name, default_configs, extra={project_name, ...})
        RM-->>M: run_id
        M->>M: record(key → run_id, 지문) 원자적 쓰기
        M-->>A: 202
    end
    R->>A: GET /api/migrations/{id}
    A->>RM: status(id)
    A->>M: translate_status → state
```

### 3.1 새 모듈 `sbmig/web/migrations.py`

| 이름 | 역할 |
|---|---|
| `IDEMPOTENCY_DIR = "_idempotency"`, `MIGRATION_FILE = "migration.json"` | 폴더·파일 이름 상수(한 곳) |
| `KEY_RE`, `PROJECT_NAME_RE` | §2.1 의 정규식 |
| `MIGRATION_STATES` | §2.2 번역표(dict). `translate_status(st: dict, accepted: dict \| None) -> dict` 가 이 표만 쓴다 |
| `class IdempotencyStore(out_dir)` | `lookup(key) -> dict \| None`, `record(key, data) -> None`, `lock`(`threading.Lock`). 파일 이름은 `sha256(key).hexdigest() + ".json"`(키에 어떤 글자가 와도 경로가 안전). 내용: `{"key", "run_id", "project_name", "fingerprint", "accepted_at", "name"}`. 쓰기는 임시 파일 + `os.replace`(원자적). 폴더는 첫 기록 때 만든다 |
| `accept(mgr, store, *, key, project_name, name, fingerprint, incoming) -> tuple[int, dict, dict]` | **lock 을 잡은 채로** `lookup` → 재전송이면 `(200, body, {"Idempotency-Replayed": "true"})`, 지문 다르면 `(422, …)`, 없으면 `mgr.start_file(incoming, name, mgr.default_configs(), extra=…)` → `record` → `(202, body, {})`. lock 을 `start_file` 까지 잡는 이유: 같은 키가 동시에 두 번 오면 하나만 실행을 만들어야 한다. `start_file` 은 이미 한 번에 하나만 올리는 모델이라 잠금 비용이 문제되지 않는다. 라우트는 이 함수를 `run_in_threadpool` 로 부른다(막히는 작업) |
| `register_migration_routes(app, mgr, max_upload)` | `/api/migrations*` 라우트 등록. `create_app` 이 기존 라우트 뒤에서 한 번 부른다 |

재전송 응답의 `state`: 기록의 `run_id` 로 `mgr.status()` 를 다시 읽어 번역한다. 실행 폴더가 사라져 `exists` 가 false 면 `state:
"expired"` 로 답한다(새 실행을 만들지 않는다 — 키를 바꿔 다시 접수하라는 뜻).

### 3.2 `RunManager` 의 최소 변경 (`sbmig/web/runs.py`)

- `start_file(..., extra: dict[str, Any] | None = None)` 과 `_start_job(..., extra)`: `extra` 가 있으면 (1) 저널 `web_run` 줄에
  병합해 쓰고(이미 있는 줄 — 키만 늘어난다), (2) `<run dir>/migration.json` 에 `{"migration_id": run_id, **extra, "accepted_at": …}`
  을 쓴다. in-process 경로는 `Workspace.from_input` 뒤, Job 경로는 `_start_job` 의 저널 줄 옆. 둘 다 OSError 는 DEBUG 로그로
  삼키고 실행을 거절하지 않는다(저널 줄과 같은 태도).
- `status()`: `migration.json` 이 있으면 `info["migration"] = <그 내용>`. 그 밖은 바꾸지 않는다.
- `extra` 를 안 주는 기존 호출(`/api/runs`, 테스트)은 동작이 같다.

### 3.3 `sbmig/web/app.py`

- `/api/runs` 의 본문 스트리밍을 `async def _receive_zip(request, mgr, max_upload) -> tuple[Path, str] | JSONResponse` 로 뺀다
  (반환: 임시 파일 경로 + sha256 hex, 또는 400/413 응답). 두 라우트가 같이 쓴다. 거절·실패 시 임시 파일은 호출한 라우트의 `finally`
  가 지운다(지금과 같다).
- `files` 핸들러 본문을 `def _serve_file(mgr, run_id, name, download)` 로 빼고 `/api/runs/{id}/files/{name}` 과
  `/api/migrations/{id}/files/{name}` 이 같이 쓴다. `events` 도 같은 식.
- `create_app` 끝에서 `store = IdempotencyStore(mgr.out_dir)`; `app.state.idempotency = store`;
  `register_migration_routes(app, mgr, max_upload)`.
- 시작 로그(`sbmig web UI ready …`)에 `external intake /api/migrations` 한 토막을 더한다(운영자가 켜진 걸 본다).

### 3.4 모드별 동작 — 새 코드가 모르는 채로 맞아야 한다

| 모드 | 접수 뒤 | 상태 | 재시작 뒤 |
|---|---|---|---|
| in-process | 이 프로세스의 스레드 | 메모리 → `web-status.json` | 폴더로 답한다(`finished`/`unknown`) |
| local-job(개발) | 서브프로세스 "파드" | `job-status.json` | 같음 |
| k8s-job | 공유 PVC `/data/out/<id>/source.zip` → Job | `job` 필드 + `job-status.json` | `journal` 의 `job_created` 로 `running`(6 시간 뒤 `unknown`) |

`<out>` 이 k8s 에서는 공유 PVC 라서 `_idempotency/` 도 거기 있다 — 웹 파드가 다시 떠도 키는 남는다.

### 3.5 이 단계에서 하지 않는 것

콜백(webhook) 전송, 동시 실행 폭(큐 폭 3), multipart, 새 인증 방식, 키 만료 청소, 실행 폴더 삭제 API. 콜백이 필요해지면
`RunManager._worker` 의 `finally`(`_write_web_status` 옆)와 `_run_job` 의 끝에서 `migration.json` 의 `callback_url` 로 POST 하는
증분 하나로 들어간다 — 그래서 `extra` 를 `migration.json` 에 통째로 남겨 둔다.

## 4. 테스트 — `tests/web/test_migrations_api.py`

`tests/web/test_app.py` 의 `client` 픽스처(가짜 실행), `_zip_bytes`, `_manager`, `_sse` 를 import 해서 쓴다. 번호가 곧 인수 기준이다.

1. **검증.** `Idempotency-Key` 없음 → 400; 틀린 키(공백·120 자 초과·`/`) → 400; `project_name` 없음·틀림 → 400; 본문이 zip 아님 → 400;
   `configs=` / `stop_after=` / `llm_provider=` 가 붙음 → 400; 상한 초과(`SBMIG_MAX_UPLOAD_MB=1` 로 앱을 만들고 큰 본문) → 413.
2. **접수.** 올바른 요청 → 202, `migration_id == run_id`, `RUN_ID_RE` 통과, `state in ("queued", "running")`, `links` 다섯 키.
   실행 폴더에 `migration.json`(`project_name`, `accepted_at`), `journal.jsonl` 의 `web_run` 줄에 `project_name`.
3. **재전송.** 같은 키·같은 zip·같은 `project_name` → 200, 같은 `migration_id`, 헤더 `Idempotency-Replayed: true`,
   `len(_manager(client).runs)` 가 늘지 않음, `<out>/_idempotency/` 에 파일 하나.
4. **충돌.** 같은 키 + 다른 zip → 422; 같은 키 + 다른 `project_name` → 422; 실행 수 그대로.
5. **동시성.** 같은 키로 스레드 두 개가 동시에 POST → 하나는 202, 하나는 200, 실행은 하나(`threading.Barrier` 로 맞춰 쏜다).
6. **상태 번역.** 가짜 실행이 끝난 뒤 `GET` → `succeeded`, `exit_code 0`, `files` 에 `report.md`. 종료 코드 2·3·멈춤은
   `web-status.json` / `report.json` 을 실행 폴더에 직접 써 둔 "이전 프로세스의 실행"으로 만든다(`RunManager` 메모리에 없는 폴더):
   2 → `finished_with_issues`, 3 → `failed`, `stop_requested` → `cancelled`, 폴더만 → `unknown`, 없는 id → 404, `..` → 400.
7. **파일.** `/api/migrations/{id}/files/report.md` → 200, 본문이 `/api/runs/{id}/files/report.md` 와 같음; `?download=1` 이면
   `Content-Disposition: attachment`; 모르는 이름 → 404; 아직 없는 `result.zip` → 404.
8. **이벤트.** `/api/migrations/{id}/events` 가 `finished` 이벤트로 끝난다(`_sse` 는 `/api/runs/…` 경로를 박아 두었으니 경로를 인자로 받는 변형을 이 파일에 둔다).
9. **취소.** 돌고 있는 실행에 `cancel` → 200 `cancelling` 이고 `status()["stop_requested"]`; 끝난 실행 → 409; 모르는 id → 404.
   (가짜 실행이 너무 빨리 끝나면 `fake_body` 를 `threading.Event` 로 잠깐 세우는 변형을 이 파일 안에 둔다.)
10. **재시작.** 같은 `out` 폴더로 `create_app` 을 다시 만든 뒤 같은 키로 POST → 200 재전송, `GET` 이 폴더로 답한다.
11. **모드 무관.** `tests/web/test_job_mode.py` 의 `FakeJobClient` + `FakePod` 로 `mode="k8s-job"` 앱을 만들고 접수 → `status` 에
    `job` 필드, 끝나면 `succeeded`. 실행 폴더의 `migration.json` 이 Job 경로에서도 쓰였는지.
12. **기존 것 그대로.** `tests/web/test_app.py`, `test_token.py`, `test_job_mode.py`, `test_local_job_mode.py` 전부 통과.

실행:

```bash
.venv/bin/python -m pytest tests/web tests/docs -o addopts="" -q
.venv/bin/python -m pytest -m "not integration" -o addopts="" -q       # 전체(약 10 분)
```

## 5. 문서 갱신 (코드와 같은 커밋에)

- `docs/INTERFACES.md` v2 절 끝: "external intake API (`/api/migrations`)" 불릿 하나 — 계약 요약(§2.1 의 헤더·쿼리·응답 코드,
  §2.2 의 번역표, 멱등 파일 위치), 이 문서 링크.
- `deploy/README.md` §3 "있는 것 / 다음 것" 표의 외부 접수 API 줄을 **있음**(`sbmig/web/migrations.py`)으로.
- `agent-doc/09-deploy-and-ops.md` 끝 "다음:" 줄에서 외부 접수 API 를 빼고 표에 `sbmig/web/migrations.py` 한 줄(350 줄 / 30 KB
  한도 — `tests/docs/test_agent_doc.py`).
- `docs/PLAN.md` 맨 끝 Status 표에 한 줄.
- `.env.example` 에는 더할 것이 없다(새 환경 변수 없음). 상한·토큰·모드는 기존 변수 그대로다.

## 6. remediation 쪽에 줄 호출 예시

```bash
# 접수
curl -sS -X POST "http://sbmig.internal/api/migrations?project_name=order-api&name=order-api.zip" \
  -H "Idempotency-Key: rem-ticket-4821-attempt-1" \
  -H "Authorization: Bearer $SBMIG_WEB_TOKEN" \
  -H "Content-Type: application/zip" --data-binary @order-api.zip
# 상태(폴링, 30 초 간격이면 충분하다 — 한 실행이 보통 20 분 ~ 2 시간)
curl -sS "http://sbmig.internal/api/migrations/web-20261106-091500-3fa2c1" -H "Authorization: Bearer $SBMIG_WEB_TOKEN"
# 결과
curl -sS -o report.md  "http://sbmig.internal/api/migrations/<id>/files/report.md"  -H "Authorization: Bearer $SBMIG_WEB_TOKEN"
curl -sS -o result.zip "http://sbmig.internal/api/migrations/<id>/files/result.zip?download=1" -H "Authorization: Bearer $SBMIG_WEB_TOKEN"
```

```powershell
# Windows 에서 접수
Invoke-RestMethod -Method Post -Uri "http://sbmig.internal/api/migrations?project_name=order-api" `
  -Headers @{ "Idempotency-Key" = "rem-ticket-4821-attempt-1"; "Authorization" = "Bearer $env:SBMIG_WEB_TOKEN" } `
  -ContentType "application/zip" -InFile .\order-api.zip
```

`state` 가 `succeeded` / `finished_with_issues` / `failed` / `cancelled` 가 되면 끝이다. `finished_with_issues` 와 `failed` 는
`report.md` 의 맨 앞 **Handover(인수인계)** 절이 담당팀이 할 일을 적어 둔다; `failed` 의 `error` 는 sbmig 쪽 사유(설정·Nexus·ADF).

## 7. 완료 체크리스트

- [ ] `sbmig/web/migrations.py` 가 있고 모듈·공개 정의에 한국어 docstring 이 먼저 온다.
- [ ] `/api/migrations` 다섯 경로(접수·상태·파일·이벤트·취소)가 §2 대로 답한다. 목록(§2.6)은 선택.
- [ ] 멱등 기록이 `<out>/_idempotency/<sha256(key)>.json` 이고, 같은 키의 재전송은 실행을 만들지 않는다(§4-3, 4, 5, 10).
- [ ] 상태 번역이 `MIGRATION_STATES` 한 곳에 있고 §2.2 표와 같다(§4-6).
- [ ] `/api/runs*` 와 화면 동작이 그대로다(§4-12). `RunManager` 변경은 `extra` + `migration.json` + `status()["migration"]` 뿐이다.
- [ ] 새 pip 의존성이 없다. `configs` / `stop_after` / `llm_provider` 를 외부 접수가 받지 않는다.
- [ ] `tests/web/test_migrations_api.py` 12 항목 통과, 전체 `-m "not integration"` 통과, `tests/docs` 통과.
- [ ] §5 의 문서 네 곳이 갱신됐다.
- [ ] §2.8 의 확인 사항 중 답을 받은 것은 상수·번역표에 반영했고, 못 받은 것은 이 문서에 "미확인" 으로 남겼다.
