# Job 실행 상태 계약 — Main 파드(FastAPI)와 요청별 Job 파드 사이의 약속

> 2026-11-06, v2.1z-ac(2026-11-09: `control/` 상태 파일 · 입력 고정 · 재시작 복구 · 소유권 라벨 · 결과 검증 · Python Worker),
> v2.1z-ad(2026-11-10: Job 종료 확인 뒤에만 차례를 넘김 · 두 라벨이 다 맞아야 우리 Job · 환경 지문 · 쓰는 순간의 비밀값 정제) 반영. 용도: remediation 쪽이 보낸 통합 계획(Main 파드 하나 + 요청별 build Job, `/api/runs` 와 `/api/remediations` 가 같은
> 디스패처를 쓴다)의 **1단계 "실행 상태 계약 문서화"** 에 해당하는 sbmig 쪽 사실이다. 여기 적힌 것은 지금 코드가 하는 일이고
> (`sbmig/web/runs.py`, `sbmig/web/k8s.py`, `sbmig/web/localjob.py`, `deploy/docker/run-job.sh`, `deploy/k8s/81-web-job.template.yaml`),
> §11 은 통합하면서 양쪽이 **정해야 할 것**이다. 계약이 바뀌면 이 파일과 `docs/INTERFACES.md` "k8s Job dispatch mode" 절을 같이 고친다.
> 외부 접수 API 의 계약은 `docs/v2/API-MIGRATIONS-KO.md`, 환경 변수 전체는 `deploy/ENV.md`, 켜는 법은 `deploy/README.md` §3.

## 1. 책임 분담 — 누가 무엇을 쓰는가

Main 과 Worker 는 **공유 PVC 의 실행 폴더** `<out>/<run id>/`(운영: `/data/out/<run id>/`)로만 만난다. 프로세스 간 잠금·소켓·DB 는 없다.

| 쪽 | 쓰는 것 | 읽는 것 |
|---|---|---|
| **Main**(웹 파드, `RunManager`) | `source.zip`(업로드 원본), **`control/request.json`**(접수 때 고정한 입력: zip sha256 · 설정 digest · sbmig 버전 · attempt)과 **`control/state.json`**(제어 상태, 전이마다 원자적으로 다시 씀 — `control/` 은 Main 만 쓴다. Job 을 만드는 Main 이 `creating` 전이와 함께 환경 지문 `env_pins` · 정책 `env_pin_policy` 도 적는다, §3), `journal.jsonl` 의 첫 줄 `web_run` 과 `job_created` / `stop_request_sent` 줄, `stop.request`(멈춤 요청), 메모리의 `Run`(큐 상태·마지막 이벤트) | `journal.jsonl`, `sbmig.log`, `report.json` / `report.md`, `job-status.json`, 쿠버네티스 Job / Pod 상태 |
| **Worker**(Job 파드, `python -m sbmig.web.worker` → `sbmig run --run-id`; 시작 때 `control/request.json` 의 digest 와 `control/state.json` 의 환경 지문을 대조, 비밀값은 쓰는 순간 지우고 끝날 때 한 번 더 훑는다) | `journal.jsonl` 의 나머지 전부(`graph_node` / `graph_node_end` …), `sbmig.log`, `project/`(git 작업 복사본), `logs/`, `report.*`, `errors.*`, `result.zip`, `changes.patch`, **`job-status.json`**(마지막에) | `source.zip`, `control/request.json` · `control/state.json`(읽기만), `stop.request`(노드 사이·빌드 라운드 사이), 설정 yml, `settings.xml`, KB(`SBMIG_KB_DIR`, 운영 `/data/kb-v2`) |

Worker 는 Main 의 존재를 모른다. Main 이 죽어도 Job 은 끝까지 돌고 파일을 남긴다. Main 은 Worker 의 프로세스를 모른다. 파일과 Job 상태만 본다.

## 2. 식별자 연결

| 이름 | 값 | 어디에 |
|---|---|---|
| run id | `web-<yyyymmdd>-<hhmmss>-<6 hex>` (`RunManager.start_file`) | 실행 폴더 이름, 모든 저널 줄의 `run_id`, `SBMIG-RESULT` 줄 |
| Job 이름 | `sbmig-<run id>` 를 RFC 1123 로 (`job_name`: 소문자·숫자·`-`, 63자) | `metadata.name`; 저널 `job_created` 의 `job` |
| 파드 라벨 | `sbmig-run-id=<run id>` (Job 과 파드 템플릿 둘 다), 쿠버네티스가 붙이는 `job-name=<Job 이름>` | `kubectl get pods -l sbmig-run-id=…` |
| 소유 라벨 | `sbmig-owner=<요청마다 난수 16 hex>` (Job 과 파드 템플릿; `control/state.json` 의 `owner`) | Main 은 `sbmig-owner` 와 `sbmig-run-id` 가 **둘 다** 자기 값인 Job 만 이어받고 지운다. 라벨이 없는 Job(`{}`)은 남의 것이다 — 같은 이름이어도 이어받지도 지우지도 않고, 만들기(409)·재시작 복구에서 만나면 그 실행을 오류로 끝낸다. 라벨을 알려 주지 못하는 클라이언트(로컬 서브프로세스·테스트 가짜, `labels` 가 None)만 이름을 믿는다 |
| 외부 요청 id | 외부 접수 API 의 `Idempotency-Key` / `project_name` (`<run dir>/migration.json`, `<out>/_idempotency/<sha256(key)>.json`) — `API-MIGRATIONS-KO.md` §3 | Main 만 쓴다 |
| 실행 시도 id | `attempt`(지금은 늘 1) — `control/request.json` · `control/state.json` · `job-status.json` 에 들어 있어 Main 은 **같은 run id · 같은 attempt** 의 결과만 받는다. 재시도·재개는 여전히 **새 run id**(새 폴더, 새 Job); 외부 요청 id 와의 연결은 Main 쪽 기록 | §11-2 |

같은 실행 폴더로 Job 을 다시 만들면 안 된다. `job-status.json` 이 남아 있으면 Main 은 새 Job 을 보기도 전에 "끝남" 으로 읽고(`_read_job_status` 는 신선도를 보지 않는다), `stop.request` 가 남아 있으면 새 Job 이 첫 노드에서 멈춘다. 둘 다 Main 이 지우지 않는다(의도: 기록 보존).

## 3. 실행 폴더와 볼륨 레이아웃 (운영 경로)

```
/data/out/<run id>/          실행 폴더(위 표의 파일들). Main 과 Job 파드가 같은 PVC(sbmig-data, ReadWriteMany) 를 /data 에 마운트
/data/out/_uploads/          Main 의 임시 업로드(.incoming-*.part) — in-process 모드의 zip 보관도 여기
/data/out/_idempotency/      외부 접수 API 의 멱등 기록(만들 것)         ← `_` 로 시작하는 폴더는 실행이 아니다(list_runs 가 무시)
/data/kb-v2/                 파일 KB(SBMIG_KB_DIR). 모든 Job 이 같은 KB 에 쓴다 → 쓰기는 `.lock` 으로 차례(§8)
/home/sbmig/.m2/             Maven 로컬 저장소 PVC(sbmig-m2, 선택; 없으면 emptyDir). 자리마다 repository / repository-slot<k> (§8)
/app/config/artifacts.yml    이미지에 구운 회사 설정(Main 과 Job 파드가 같은 이미지 → 같은 파일; 2026-11-10)
/etc/sbmig/settings.xml      ConfigMap sbmig-config 의 파일. Job 파드도 같은 경로에 마운트
```

Job 템플릿이 받는 실행별 env(Main 이 첫 컨테이너의 env 에 덧붙인다, 같은 이름은 바꾼다): `SBMIG_SOURCE_ZIP`, `SBMIG_OUT`, `SBMIG_RUN_ID`,
`SBMIG_CONFIG`(공백으로 나눈 yml 경로들), 고급 설정일 때만 `SBMIG_STOP_AFTER`, `SBMIG_LLM_PROVIDER`. 나머지(이미지, 마운트, 자원,
`SBMIG_KB_DIR`, 컨테이너 경로 덮어쓰기 `SBMIG_BUILD__*` / `SBMIG_SCRIPT__PYTHON`, 변수마다 `configMapKeyRef` / `secretKeyRef` — `envFrom` 은 검증이 거절한다)는 템플릿 그대로다. 템플릿 자체는 `render_job` 이 바꾸지 않는다.
개발 PC 의 local-job 에서는 틀의 env 가운데 이 실행별 값(`contract.RUN_ENV`, 리터럴만)만 로컬 Worker 에 넘어가고, 나머지는 PC 의 환경이 정한다 — 컨테이너 경로·ConfigMap 참조는 파드의 것이다(2026-11-10 회귀 수정).

**환경 지문(v2.1z-ad).** 설정은 파일만이 아니다: yml 의 `${VAR}` 자리표시자와 덮어쓰기 `SBMIG_<섹션>__<키>` 는 파드의 환경 변수가 값을 정한다. 웹 파드는
ConfigMap `sbmig-env` 를 시작할 때 한 번, Job 파드는 만들어질 때 읽으므로 두 파드의 값이 갈릴 수 있다. 그래서:

- **누가 언제 적나**: Job 을 **만드는** Main 이 `creating` 전이와 함께 `control/state.json` 에 `env_pins`(이름 → 값의 sha256 앞 16자; 값 자체는 적지 않는다)와
  `env_pin_policy`(refuse | warn | off)를 적는다(`RunManager._create_job`). 접수 때가 아니다 — 대기 중에 웹 파드가 재시작되면 새 Main 의 환경이 기준이다.
  이미 있는 Job 을 이어받은 실행은 디스크의 지문을 그대로 쓴다. `request.json` 에 두지 않는 까닭: 그 파일은 접수 기록이라 바뀌지 않고, 기준은 Job 을
  렌더한 Main 이다. 정책이 off 이거나 지문 계산이 실패하면(경고만, Job 은 만든다) `env_pins` 는 null — Worker 가 대조하지 않는다.
- **대상 이름**: `config/default.yml` 과 이번 실행의 설정 파일이 읽는 `${VAR}` 이름 + 그 파드에 있는 `SBMIG_<섹션>__<키>` 덮어쓰기와 섹션을 통째로 바꾸는 `SBMIG_<섹션>`(`contract.env_pin_names`).
  비밀로 보이는 이름(TOKEN / PASSWORD / PASSWD / SECRET / API_KEY — `LLM_TOKEN_URL` 도 여기 걸린다), `SBMIG_LLM__RECORD_DIR` / `LLM_RECORD_DIR`,
  프록시 변수는 대조하지 않는다. 웹 파드의 `SBMIG_KB__DIR` 와 Job 파드의 `SBMIG_KB_DIR` 은 같은 키로 대조한다(`ENV_PIN_ALIASES`).
- **정책**: 웹 파드의 `SBMIG_JOB_ENV_PIN`(시작 때 한 번 읽는다). 기본은 k8s-job 이면 `refuse`, local-job 이면 `off`(로컬 Worker 는 Main 의 환경을 물려받는다).
- **Worker**: `request.json` 대조를 통과한 뒤 `worker_started` 전에 `state.json` 을 읽어(쓰지 않는다) 지문을 자기 환경으로 다시 계산해 대조하고, 실행별 값
  (`SBMIG_CONFIG` 가 가리키는 파일, `SBMIG_STOP_AFTER`, `SBMIG_LLM_PROVIDER`)도 `request.json` 과 맞춰 본다. 지문이 없으면(옛 Main, local-job) 대조하지 않는다.
  `refuse` → 거절(exit 3, 저널 `worker_refused` 에 다른 키 **이름**, `job-status.json` 의 `error`), `warn` → 저널 `worker_env_differs` 를 적고 Job 파드의 값으로 돈다.
  글에는 이름만 들어간다(값·지문은 없다).
- **운영 규칙**: `sbmig-env` 를 고친 뒤에는 **웹 파드를 재시작한다**(안 하면 다음 Job 이 몇 초 만에 거절된다). Job 틀을 고친 뒤에는 ConfigMap
  `sbmig-job-template` 을 다시 만들고 웹 파드를 재시작한다(Main 은 시작할 때 틀을 검증해 들고 있다). 덮어쓰기 값을 빈 문자열로 두지 않는다 —
  `SBMIG_X__Y=""` 는 YAML null 로 읽혀 yml 의 값을 없애고, 두 파드가 똑같이 비어 있으면 대조도 잡지 못한다.

## 4. 종료 코드와 결과 판정

| 어디 | 값 | 뜻 |
|---|---|---|
| sbmig (`sbmig run`) | 0 | 최종 `mvn clean package` 통과, 차단 없음 |
| | 2 | 끝났지만 차단·미해결 오류 있음(보고서가 결과) |
| | 3 | 중단(사전 점검·ADF·사내 부모 해석 불가·내부 오류·멈춤 요청) |
| 컨테이너(`run-job.sh`) | 0 | sbmig 0 **또는 2** (보고서가 나왔으면 Job 은 성공) — `SBMIG_FAIL_JOB_ON_BUILD_FAIL=1` 이면 2 를 그대로 |
| | 3 | sbmig 3 |
| | 4 | (git 모델) 결과 push 실패 |
| `job-status.json` | `{"exit_code": <sbmig 코드>, "finished_at": …, "run_id", "attempt", "sbmig_version", "error"?}` | **sbmig 의 코드 그대로.** Main 은 이 파일을 Job 상태보다 먼저 읽되 `run_id` · `attempt` 가 지금 시도와 같을 때만 받는다(다른 시도의 파일은 경고하고 무시). Worker 가 입력 검증에서 거부하면 `exit_code: 3` 과 `error` |

"실패를 성공으로 표시하지 않음" 은 **`job-status.json` 의 `exit_code` 와 `report.json` 의 `success` 로 판정**해야 지켜진다. 컨테이너 종료
코드 0 은 "보고서가 있다" 는 뜻일 뿐이다. `report.json` 에는 `success`, `blocked[]`, `unresolved[]`, `error_entries[]`, `llm_usage.calls`
가 있고 Main 의 `status()` 가 개수로 바꿔 준다.

## 5. Main 의 상태 도출 순서 (`RunManager.status`)

1. 메모리에 `Run` 이 있으면: `queued`(줄 서는 중) / `running`(Job 생성 뒤) / `finished` + `exit_code`. Job 실행이면 `job {name, phase, pod}`.
2. 없으면(재시작 뒤·메모리에서 밀림): `web-status.json`(in-process) 또는 **`job-status.json`** → `finished` + 그 안의 `exit_code`.
3. 그것도 없으면 저널의 `job_created` 줄이 **6시간 이내**면 `running`(Job 이 아직 돌고 있다고 본다, 화면이 다시 tail 한다), 더 오래됐으면 `unknown`.
4. `report.json` 만 있으면 `finished`, 폴더만 있으면 `unknown`, 폴더도 없으면 `missing`.

Job 감시 루프(`_run_job`, 2초 주기 `SBMIG_JOB_POLL_S`): 매 번 **`job-status.json` 먼저**, 그다음 Job/파드 상태. 상태 변화마다 화면에
`node` 이벤트 `{"node": "job", "update": {"state": created|creating|pending|running|succeeded|completed|failed|missing, "job", "pod", "message"}}`
(v2.1z-ad: 단계 표시를 바꾸지 않는 안내 `ending` · `api-unavailable` · `api-back` 이 더해졌다).
**'파드' 단계의 뜻(2026-11-10)**: "파드가 제 일을 했나" 이지 이관의 종료 코드가 아니다. Worker 는 sbmig 의 종료 코드 3(ADF 거부, intake 거절 …)을
컨테이너 종료 코드로 그대로 내므로 그런 실행의 Job 은 쿠버네티스에서 `Failed` 가 된다. 그 `Failed` 는 화면에 그대로 보내지 않는다:
이 실행·시도의 `job-status.json` 을 받으면 `completed`(`exit_code`, 본 적이 있으면 원래 `phase` 포함; 감시 한 번에 한 번만) → 화면은 '파드' 에 체크하고
멈춘 까닭(`중단: …`)은 엔진 단계 쪽에 남긴다. `failed` 는 둘뿐이다: Job 이 `Failed` 로 끝났는데 유예(`SBMIG_JOB_STATUS_GRACE_S`) 안에 결과 파일이
없다(파드가 죽었다: OOMKilled, 기한 초과, 축출) 또는 받은 결과 파일에 `error` 가 있다(Worker 가 요청을 거부했거나 sbmig 를 띄우지 못했다).
`status().job.phase` 는 쿠버네티스의 원래 값 그대로다. Job 이
Failed / Missing 인데 `job-status.json` 이 없으면 종료 코드 3 과 `error` 문구. **Job 종료 확인(v2.1z-ad)**: 이 시도의 `job-status.json` 을 받아도
실행은 Job 이 끝났거나(Succeeded / Failed) 사라진(Missing) 것을 API 로 확인한 뒤에야 끝난다. 파일을 쓴 뒤 120초(`SBMIG_JOB_EXIT_GRACE_S`) 넘게 파드가
남아 있으면 우리 라벨의 Job 을 지우고 사라질 때까지 기다린다(Worker 는 그 파일을 맨 마지막에 쓰므로 잃는 것이 없다). 쿠버네티스 API 가 답하지 않으면
**실행을 실패로 끝내지 않고 답할 때까지 기다린다**. 300초(`SBMIG_JOB_API_GRACE_S`)는 이제 알림 기준일 뿐이다(로그 ERROR, 저널 `job_api_unavailable`,
화면 안내, `status().job.note`; 이후 600초마다 로그; 다시 답하면 `job_api_back`). SSE 는 그대로다: Worker 가 쓰는 `journal.jsonl` / `sbmig.log` 를 tail 하고, `graph_node_end` 줄을 화면의 `node` 이벤트로 바꾼다.

## 6. 멈춤

Main 이 `<run dir>/stop.request` 를 쓴다(저널 `stop_request_sent`). Worker 의 엔진은 **그래프 노드 사이**와 **수정 루프의 빌드 라운드 사이**에서
그 파일을 보고 멈춘다(최종 빌드 생략, exit 3, 저널 `stop_request`). 진행 중인 Maven 빌드·LLM 호출 하나는 끊지 않는다. 2700초
(`SBMIG_JOB_STOP_GRACE_S`, Maven 빌드 + OpenRewrite 한 번보다 길어야 한다) 안에 안 끝나면 Main 이 Job 을 지운다(`propagationPolicy Foreground`,
파드도 함께). 서버 종료는 Job 을 지우지 않는다. 멈춤이 큐에서 기다리는 실행에 오면 그 자리에서 줄에서 빼고 Job 을 만들지 않고 끝낸다(뒤의 실행들의 순번이 당겨진다). 지우기 전에 Job 의 라벨(`sbmig-run-id`, `sbmig-owner`)이
**둘 다** 자기 것인지 보고, 아니면 지우지 않는다(`job_delete_refused`); 지운 뒤에는 Job 이 실제로 사라질(`Missing`) 때까지 기다린 뒤에야 `stopped` 로 끝낸다(삭제 요청 수락 = 끝남이 아니다).
이 기다림에는 기한이 없다(v2.1z-ad — 예전에는 `SBMIG_JOB_API_GRACE_S` 까지): 삭제 요청이 실패하면 15초마다 다시 보내고, 끝나지 않는 동안 60초마다 경고한다.
멈춤을 요청한 실행의 Job 이 사라졌으면 `lost` 가 아니라 `stopped` 다.

## 7. 재시작과 장애 복구 — 되는 것과 안 되는 것

| 상황 | 지금 동작 |
|---|---|
| Main 재시작, Job 은 돌고 있음 | `control/state.json` 이 `created` / `running` 이면 다시 뜬 Main 이 그 Job 을 **이어받아 감시한다**(`RunManager.recover`): 멈춤·삭제·결과 검증이 그대로 된다. `creating`(생성 응답이 끊긴 구간)이면 같은 이름의 Job 이 있는지 보고 있으면 이어받고 없으면 만든다. 어느 쪽이든 그 Job 의 두 라벨이 이 요청의 것일 때만 이어받고(아니면 그 실행은 오류), Job 이 있는지는 API 가 답할 때까지 묻는다 |
| Main 재시작, 요청은 큐에 있었음(Job 미생성) | `control/state.json` 이 `queued` 면 접수 순서대로 **다시 줄에 선다**(`source.zip` 과 `request.json` 이 있으니 그대로 만든다). 개발 PC 의 local-job 은 프로세스가 서버와 함께 죽어 복구하지 못한다 → `lost` |
| Job 파드가 죽음(OOM·노드 교체) | `backoffLimit: 0` → 재시도 없음. Main 은 Failed + `job-status.json` 없음 → 종료 코드 3, `error` 에 사유 |
| Job 이 4시간을 넘김 | `activeDeadlineSeconds: 14400` → 쿠버네티스가 끊는다 → 위와 같음 |
| Job 이 끝남(성공이든 실패든) | 파드는 꺼진 채 목록에 남는다(삭제 명령은 없다). `ttlSecondsAfterFinished: 3600` → 1 시간 뒤 쿠버네티스가 Job 과 파드를 지운다. 결과는 볼륨에 남는다. 그 사이 Main 이 내려가 있었어도 `job-status.json` 이 결과다 |
| 쿠버네티스 API 장애 | 실행을 실패로 끝내지 않고 차례도 넘기지 않는다(v2.1z-ad): API 가 다시 답할 때까지 감시를 이어 가고 다음 업로드는 기다린다. 300초(`SBMIG_JOB_API_GRACE_S`) 뒤 한 번 크게 알린다(저널 `job_api_unavailable`), 다시 답하면 `job_api_back`. 끝내 답하지 않으면 Main 의 접속을 고칠 때까지 줄이 멈춘다(두 이관이 겹치는 것보다 낫다는 결정) |
| 결과 파일을 쓴 뒤에도 파드가 안 끝남 | 120초(`SBMIG_JOB_EXIT_GRACE_S`) 뒤 우리 Job 을 지우고 사라질 때까지 기다린 다음 차례를 넘긴다 |
| Job 파드의 환경이 웹 파드와 다름(`sbmig-env` 를 고치고 웹 파드를 재시작하지 않음, 낡은 Job 틀) | `refuse`(k8s-job 기본): Worker 가 몇 초 만에 거절, exit 3, `worker_refused` 에 다른 키 이름. `warn`: `worker_env_differs` 를 적고 Job 파드의 값으로 돈다(§3) |
| 같은 이름의 Job 이 있는데 라벨이 없거나 다름 | 이어받지도 지우지도 않는다. 만들기·재시작 복구에서 만나면 그 실행은 오류로 끝난다 |
| PVC 가 RWX 가 아님 | Job 이 다른 노드에 뜨면 마운트 못 해 Pending 에 멈춘다 → `nodeSelector` 로 웹 파드 노드에 고정해야 한다 |

"대기 요청도 복구" 가 필수라면 Main 시작 때 `web_run` 은 있고 `job_created` / `web-status.json` / `job-status.json` 은 없는 폴더를 다시 줄에 세우는
증분이 필요하다(`source.zip` 이 있으니 가능). 이건 sbmig 쪽에서 만들 수 있다 — §11-4.

## 8. 슬롯 (동시에 N 개, 2026-11-10 부터)

`RunManager` 는 올린 순서(FIFO, `_FifoTurns`)로, **동시에 `SBMIG_JOB_MAX_PARALLEL` 개까지**(기본 1, 매니페스트 값 3; `in-process` 모드는 언제나 1) Job 을
만든다. 도는 Job 마다 자리 번호(1 부터, 비어 있는 가장 작은 수)가 있다: `control/state.json` 의 `slot`(`creating` 전이 때 적는다; 없으면 1), Job env
`SBMIG_JOB_SLOT`. Worker 는 2 번 자리부터 Maven 로컬 저장소를 그 자리의 것(`<저장소>-slot<k>`)으로 바꿔 자식 실행에 넘긴다 — 같은 저장소에 Maven 둘이
쓰지 않게. 모든 Job 이 같은 파일 KB(`/data/kb-v2`)에 쓰는 것은 그대로이고, 쓰기는 `<kb>/.lock` 하나로 차례를 지킨다(잠금 안에서 다시 읽기 + 원자적 교체).
재시작 복구는 `creating` / `created` / `running` 을 적힌 자리 번호로 먼저 앉히고(`occupy`; 한도보다 많아도 그대로) `queued` 를 접수 순서로 줄 세운다.
`status()` 는 `parallel {slots, running, waiting}` 과 기다리는 실행의 `queue {position, ahead}` 를 준다(계약: `docs/INTERFACES.md` "v2.1z-ae").
자리는 그 Job 이 끝났거나 사라진 것을 API 로 확인한 뒤에야 넘어간다(v2.1z-ad) — 결과 파일을 받았을 때도, 멈춤으로 지웠을 때도, 웹 쪽 오류로 실패했을 때도
(그때는 우리 Job 을 먼저 지운다). 예외는 서버 종료(`state.json` 을 그대로 두어 다음 Main 이 이어서 지켜본다)와 남의 Job(건드리지 않는다)뿐이다.
remediation 이 KB 를 건드리지 않으면 같은 슬롯을 쓸 이유가 없다 — 같은 슬롯을 쓰면 remediation 한 건이 이관 한 건을(또는 반대로) 몇십 분
기다리게 한다(자리를 나눠 쓰더라도 한도는 둘을 합쳐 센다).

## 9. 디스패처를 두 가지 Job 에 쓰는 법 (포크하지 않기)

| 조각 | 계약 |
|---|---|
| `RunManager(out_dir, repo_root, mode=, job_client=, job_template=)` | `mode` in-process / local-job / k8s-job (`SBMIG_WEB_MODE`); 클러스터 밖의 k8s-job 은 WARNING 과 함께 local-job 으로; 클러스터 안인데 서비스 계정이 없으면 시작 거부 |
| `render_job(template, run_id, env, namespace)` | 틀(yaml 경로 또는 dict)에 이름·라벨·namespace·첫 컨테이너 env 만 채운다. **다른 종류의 Job = 다른 틀 + 다른 env** 로 충분하다(명령은 틀의 `command`) |
| `K8sClient.create_job / job_state / delete_job` | requests 기반, 서비스 계정 토큰·CA 자동. `JobState(phase, pod, exit_code, message, labels)` — `labels` 는 API 가 답한 라벨 그대로(라벨 없는 Job 은 `{}`), None 은 라벨을 모르는 클라이언트뿐 |
| `LocalJobClient(repo_root, command_factory=…)` | 개발 모드의 서브프로세스 "파드". `command_factory(env) -> argv` 로 **다른 종류의 Worker 명령**을 끼울 수 있다. `job-status.json` 을 run-job.sh 와 같은 모양으로 쓴다 |
| `run-job.sh`(공유 볼륨 모드) | `SBMIG_SOURCE_ZIP` + `SBMIG_OUT` + `SBMIG_RUN_ID` → `sbmig run … --run-id`, 끝에 `job-status.json`. remediation Worker 는 **자기 진입점**을 두되 같은 파일 규약(§4)을 지키면 Main 은 구분할 필요가 없다 |
| RBAC(`45-web-rbac.yaml`) | jobs: create get list watch delete / pods: get list watch / pods/log: get. `pods/exec` 없음 |

권장 모양: Job 종류마다 템플릿 한 장(`81-web-job.template.yaml`, `82-remediation-job.template.yaml`)과 Worker 진입점 하나, 디스패처는 하나.
`RunManager` 에 "종류" 를 넘기는 인자(템플릿·명령 팩토리·종료 코드 표)를 더하는 쪽이 디스패처를 복사하는 쪽보다 유지가 쉽다.

## 10. 인증·네트워크·보안

- **TokenGuard 는 앱 전체에 걸린다.** `SBMIG_WEB_TOKEN` 이 있으면 모든 경로가 Bearer 토큰, 없고 `SBMIG_WEB_OPEN=1` 이면 모든 경로가 열림(사내망 전용 결정),
  둘 다 없으면 루프백만. remediation 라우터를 **같은 FastAPI 앱에 붙이면 같은 규칙**을 받는다. 다른 인증(헤더·JWT)이 필요하면 그 라우터 전용 의존성으로
  얹되, TokenGuard 와 겹치지 않게 결정해야 한다(§11-5).
- TLS 는 Ingress / Service 앞단의 일이다. 파드 안은 평문 HTTP(uvicorn 8765).
- Job 파드가 나가야 하는 곳: LLM 게이트웨이(토큰 URL + 호출 URL), Nexus, (remediation 이 쓰는 외부 서비스가 있으면 그것도). NetworkPolicy 가 있으면 Job 파드 라벨
  `app=sbmig, kind=run` 에 egress 를 열어야 한다.
- ConfigMap(`sbmig-env`: 비밀 아닌 값 + `SBMIG_<SECTION>__<KEY>` 손잡이) / Secret(`sbmig-llm`: `GIT_TOKEN`, `LLM_APP_TOKEN`/`LLM_API_KEY`, `NEXUS_*`,
  선택 `SBMIG_WEB_TOKEN`) / 파일 ConfigMap(`sbmig-config`: `settings.xml`) / 이미지(`/app/config/artifacts.yml`, 비밀 없음). 목록은 `deploy/ENV.md`.
- **비밀값 정제는 쓰는 순간에(v2.1z-ad).** `SBMIG_REDACT_OUTPUT=1`(40·80 에 리터럴, 81 은 Worker 가 자식에 강제)이면 로그 줄·저널 줄·화면 이벤트·잡히지 않은
  예외의 traceback 에서 비밀 env 의 값(실행 중에 받은 게이트웨이 토큰 포함)을 쓰기 전에 `***` 로 바꾼다(`sbmig/core/scrub.py`). 지우다 실패한 줄은 원문 대신
  고정 문구로 나간다. Worker 는 자식의 콘솔 출력도 한 줄씩 지워 파드 로그로 넘기고, 끝날 때 실행 폴더를 한 번 더 훑는다. 실행 중에는 `sbmig.log` /
  `journal.jsonl` 만 내려받을 수 있고 나머지 파일(보고서·오류 목록·result.zip …)은 마지막 정제가 끝날 때까지 HTTP 409 다. 환경 지문(§3)도 비밀로 보이는 이름은
  다루지 않는다 — 비밀 값의 지문을 공유 볼륨에 남기지 않는다.

## 11. 통합하면서 정할 것 (sbmig 쪽 의견 포함)

1. **API 하나인가 둘인가.** 외부 접수는 `/api/migrations`(`API-MIGRATIONS-KO.md`) 로 설계돼 있고, 계획서는 `/api/remediations` 를 말한다. 이름과 필드(`project_name`, `name`,
   `Idempotency-Key`), 상태 어휘를 한 번에 정한다. 상태 번역표는 한 곳(dict)에 두는 것이 조건.
2. **재시도·재개 = 새 run id.** 승인 뒤 재개를 "새 Job" 으로 한다는 계획과 맞는다. 단 **새 실행 폴더**여야 한다(§2). 외부 요청 id ↔ 시도 run id 들의 연결은 Main 의 기록(`migration.json`
   의 `attempts[]` 같은 것)으로.
3. **종료 코드 10(승인 대기).** sbmig 에는 없다. remediation Worker 가 `job-status.json` 에 `exit_code: 10` 을 쓰고 컨테이너는 0 으로 끝나면, Main 의 번역표에
   `10 → awaiting_approval` 한 줄로 끝난다. 컨테이너 코드로 전달하면 Job 이 Failed 로 보여 `backoffLimit`·화면과 충돌한다.
4. **대기 요청 복구.** 지금은 안 된다(§7). 필수면 sbmig 쪽에서 시작 시 재큐 증분을 만든다 — 그 전에 "Main 이 죽는 동안 올라온 요청" 은 어차피 없으니 범위를 확인.
5. **인증 모델.** 사내망 전용(토큰 없음)으로 가면 remediation 쪽 호출도 토큰 없이 들어온다. 그래도 될지, 아니면 토큰을 켜고 remediation 서비스에도 토큰을 줄지.
6. **슬롯 공유 여부.** §8. remediation 이 KB 를 안 쓰면 **별도 슬롯(또는 슬롯 없음)** 을 권한다.
7. **이미지.** 하나의 이미지에 두 코드가 들어가면 `deploy/docker/pip-deps.py` 에 의존성을 합치고(오프라인), 코드는 `/app` 아래 `sbmig/` 옆에 둔다. `config/` / `knowledge/` 는
   `sbmig/` 옆에 있어야 하므로 pip install 하지 않는다(Dockerfile 주석).
8. **IVAS 가 무엇인지.** 계획서의 Worker 구성 요소에 있는데 sbmig 에는 없다. Job 파드의 네트워크·자원 요구에 들어가야 하면 템플릿에 반영.

## 12. 테스트 자산

| 파일 | 무엇을 흉내 내나 |
|---|---|
| `tests/web/test_job_mode.py` | `FakeJobClient`(상태 시나리오) + `FakePod`(실행 폴더에 저널·보고서·`job-status.json` 을 쓰는 가짜 파드). 새 Job 종류의 Main 쪽 테스트는 이 둘을 그대로 쓴다 |
| `tests/web/test_local_job_mode.py` | 가짜 `sbmig run` 스크립트로 local-job 모드(실제 명령줄, stop.request, 폴백 경고) |
| `tests/deploy/test_run_job_sh.py` | bash 로 run-job.sh 를 실제로 실행(가짜 sbmig) — 종료 코드 표(§4) 의 증거 |
| `tests/web/test_app.py` | 업로드·SSE·파일 다운로드(in-process 가짜 실행) |
