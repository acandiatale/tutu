# Job 실행 상태 계약 — Main 파드(FastAPI)와 요청별 Job 파드 사이의 약속

> 2026-11-06. 용도: remediation 쪽이 보낸 통합 계획(Main 파드 하나 + 요청별 build Job, `/api/runs` 와 `/api/remediations` 가 같은
> 디스패처를 쓴다)의 **1단계 "실행 상태 계약 문서화"** 에 해당하는 sbmig 쪽 사실이다. 여기 적힌 것은 지금 코드가 하는 일이고
> (`sbmig/web/runs.py`, `sbmig/web/k8s.py`, `sbmig/web/localjob.py`, `deploy/docker/run-job.sh`, `deploy/k8s/81-web-job.template.yaml`),
> §11 은 통합하면서 양쪽이 **정해야 할 것**이다. 계약이 바뀌면 이 파일과 `docs/INTERFACES.md` "k8s Job dispatch mode" 절을 같이 고친다.
> 외부 접수 API 의 계약은 `docs/v2/API-MIGRATIONS-KO.md`, 환경 변수 전체는 `deploy/ENV.md`, 켜는 법은 `deploy/README.md` §3.

## 1. 책임 분담 — 누가 무엇을 쓰는가

Main 과 Worker 는 **공유 PVC 의 실행 폴더** `<out>/<run id>/`(운영: `/data/out/<run id>/`)로만 만난다. 프로세스 간 잠금·소켓·DB 는 없다.

| 쪽 | 쓰는 것 | 읽는 것 |
|---|---|---|
| **Main**(웹 파드, `RunManager`) | `source.zip`(업로드 원본), `journal.jsonl` 의 첫 줄 `web_run` 과 `job_created` / `stop_request_sent` 줄, `stop.request`(멈춤 요청), 메모리의 `Run`(큐 상태·마지막 이벤트) | `journal.jsonl`, `sbmig.log`, `report.json` / `report.md`, `job-status.json`, 쿠버네티스 Job / Pod 상태 |
| **Worker**(Job 파드, `run-job.sh` → `sbmig run --run-id`) | `journal.jsonl` 의 나머지 전부(`graph_node` / `graph_node_end` …), `sbmig.log`, `project/`(git 작업 복사본), `logs/`, `report.*`, `errors.*`, `result.zip`, `changes.patch`, **`job-status.json`**(마지막에) | `source.zip`, `stop.request`(노드 사이·빌드 라운드 사이), 설정 yml, `settings.xml`, KB(`SBMIG_KB_DIR`, 운영 `/data/kb-v2`) |

Worker 는 Main 의 존재를 모른다. Main 이 죽어도 Job 은 끝까지 돌고 파일을 남긴다. Main 은 Worker 의 프로세스를 모른다. 파일과 Job 상태만 본다.

## 2. 식별자 연결

| 이름 | 값 | 어디에 |
|---|---|---|
| run id | `web-<yyyymmdd>-<hhmmss>-<6 hex>` (`RunManager.start_file`) | 실행 폴더 이름, 모든 저널 줄의 `run_id`, `SBMIG-RESULT` 줄 |
| Job 이름 | `sbmig-<run id>` 를 RFC 1123 로 (`job_name`: 소문자·숫자·`-`, 63자) | `metadata.name`; 저널 `job_created` 의 `job` |
| 파드 라벨 | `sbmig-run-id=<run id>` (Job 과 파드 템플릿 둘 다), 쿠버네티스가 붙이는 `job-name=<Job 이름>` | `kubectl get pods -l sbmig-run-id=…` |
| 외부 요청 id | 외부 접수 API 의 `Idempotency-Key` / `project_name` (`<run dir>/migration.json`, `<out>/_idempotency/<sha256(key)>.json`) — `API-MIGRATIONS-KO.md` §3 | Main 만 쓴다 |
| 실행 시도 id | **sbmig 에는 없다.** 한 run id = 한 폴더 = 한 Job. 재시도·재개는 **새 run id**(새 폴더, 새 Job)로 하고 외부 요청 id 와의 연결은 `migration.json` 같은 Main 쪽 기록이 맡는다 | §11-2 |

같은 실행 폴더로 Job 을 다시 만들면 안 된다. `job-status.json` 이 남아 있으면 Main 은 새 Job 을 보기도 전에 "끝남" 으로 읽고(`_read_job_status` 는 신선도를 보지 않는다), `stop.request` 가 남아 있으면 새 Job 이 첫 노드에서 멈춘다. 둘 다 Main 이 지우지 않는다(의도: 기록 보존).

## 3. 실행 폴더와 볼륨 레이아웃 (운영 경로)

```
/data/out/<run id>/          실행 폴더(위 표의 파일들). Main 과 Job 파드가 같은 PVC(sbmig-data, ReadWriteMany) 를 /data 에 마운트
/data/out/_uploads/          Main 의 임시 업로드(.incoming-*.part) — in-process 모드의 zip 보관도 여기
/data/out/_idempotency/      외부 접수 API 의 멱등 기록(만들 것)         ← `_` 로 시작하는 폴더는 실행이 아니다(list_runs 가 무시)
/data/kb-v2/                 파일 KB(SBMIG_KB_DIR). 모든 Job 이 같은 KB 에 쓴다 → 한 번에 하나(§8)
/home/sbmig/.m2/             Maven 로컬 저장소 PVC(sbmig-m2, 선택; 없으면 emptyDir)
/app/config/artifacts.yml, /etc/sbmig/settings.xml   ConfigMap sbmig-config 의 파일. Job 파드도 같은 경로에 마운트
```

Job 템플릿이 받는 실행별 env(Main 이 첫 컨테이너의 env 에 덧붙인다, 같은 이름은 바꾼다): `SBMIG_SOURCE_ZIP`, `SBMIG_OUT`, `SBMIG_RUN_ID`,
`SBMIG_CONFIG`(공백으로 나눈 yml 경로들), 고급 설정일 때만 `SBMIG_STOP_AFTER`, `SBMIG_LLM_PROVIDER`. 나머지(이미지, 마운트, 자원,
`SBMIG_KB_DIR`, `envFrom` ConfigMap/Secret)는 템플릿 그대로다. 템플릿 자체는 `render_job` 이 바꾸지 않는다.

## 4. 종료 코드와 결과 판정

| 어디 | 값 | 뜻 |
|---|---|---|
| sbmig (`sbmig run`) | 0 | 최종 `mvn clean package` 통과, 차단 없음 |
| | 2 | 끝났지만 차단·미해결 오류 있음(보고서가 결과) |
| | 3 | 중단(사전 점검·ADF·사내 부모 해석 불가·내부 오류·멈춤 요청) |
| 컨테이너(`run-job.sh`) | 0 | sbmig 0 **또는 2** (보고서가 나왔으면 Job 은 성공) — `SBMIG_FAIL_JOB_ON_BUILD_FAIL=1` 이면 2 를 그대로 |
| | 3 | sbmig 3 |
| | 4 | (git 모델) 결과 push 실패 |
| `job-status.json` | `{"exit_code": <sbmig 코드>, "finished_at": …}` | **sbmig 의 코드 그대로.** Main 은 이 파일을 Job 상태보다 먼저 읽는다 |

"실패를 성공으로 표시하지 않음" 은 **`job-status.json` 의 `exit_code` 와 `report.json` 의 `success` 로 판정**해야 지켜진다. 컨테이너 종료
코드 0 은 "보고서가 있다" 는 뜻일 뿐이다. `report.json` 에는 `success`, `blocked[]`, `unresolved[]`, `error_entries[]`, `llm_usage.calls`
가 있고 Main 의 `status()` 가 개수로 바꿔 준다.

## 5. Main 의 상태 도출 순서 (`RunManager.status`)

1. 메모리에 `Run` 이 있으면: `queued`(줄 서는 중) / `running`(Job 생성 뒤) / `finished` + `exit_code`. Job 실행이면 `job {name, phase, pod}`.
2. 없으면(재시작 뒤·메모리에서 밀림): `web-status.json`(in-process) 또는 **`job-status.json`** → `finished` + 그 안의 `exit_code`.
3. 그것도 없으면 저널의 `job_created` 줄이 **6시간 이내**면 `running`(Job 이 아직 돌고 있다고 본다, 화면이 다시 tail 한다), 더 오래됐으면 `unknown`.
4. `report.json` 만 있으면 `finished`, 폴더만 있으면 `unknown`, 폴더도 없으면 `missing`.

Job 감시 루프(`_run_job`, 2초 주기 `SBMIG_JOB_POLL_S`): 매 번 **`job-status.json` 먼저**, 그다음 Job/파드 상태. 상태 변화마다 화면에
`node` 이벤트 `{"node": "job", "update": {"state": created|creating|pending|running|succeeded|failed|missing, "job", "pod", "message"}}`. Job 이
Failed / Missing 인데 `job-status.json` 이 없으면 종료 코드 3 과 `error` 문구. 쿠버네티스 API 가 잠깐 안 되면 300초(`SBMIG_JOB_API_GRACE_S`)까지
참는다. SSE 는 그대로다: Worker 가 쓰는 `journal.jsonl` / `sbmig.log` 를 tail 하고, `graph_node_end` 줄을 화면의 `node` 이벤트로 바꾼다.

## 6. 멈춤

Main 이 `<run dir>/stop.request` 를 쓴다(저널 `stop_request_sent`). Worker 의 엔진은 **그래프 노드 사이**와 **수정 루프의 빌드 라운드 사이**에서
그 파일을 보고 멈춘다(최종 빌드 생략, exit 3, 저널 `stop_request`). 진행 중인 Maven 빌드·LLM 호출 하나는 끊지 않는다. 2700초
(`SBMIG_JOB_STOP_GRACE_S`, Maven 빌드 + OpenRewrite 한 번보다 길어야 한다) 안에 안 끝나면 Main 이 Job 을 지운다(`propagationPolicy Foreground`,
파드도 함께). 서버 종료는 Job 을 지우지 않는다. 멈춤이 큐에서 기다리는 실행에 오면 Job 을 만들지 않고 끝낸다.

## 7. 재시작과 장애 복구 — 되는 것과 안 되는 것

| 상황 | 지금 동작 |
|---|---|
| Main 재시작, Job 은 돌고 있음 | 저널 `job_created`(6시간 이내)로 `running` 으로 보이고 화면이 폴더를 tail 한다. 끝나면 `job-status.json` 으로 `finished`. **Job 을 다시 감시(stop / 삭제)하는 스레드는 없다** — 멈춤은 `stop.request` 를 손으로 쓰거나 `kubectl delete job` |
| Main 재시작, 요청은 큐에 있었음(Job 미생성) | **복구되지 않는다.** 폴더에 `web_run` 줄과 `source.zip` 만 있고 `status` 는 `unknown`. 다시 올려야 한다 |
| Job 파드가 죽음(OOM·노드 교체) | `backoffLimit: 0` → 재시도 없음. Main 은 Failed + `job-status.json` 없음 → 종료 코드 3, `error` 에 사유 |
| Job 이 4시간을 넘김 | `activeDeadlineSeconds: 14400` → 쿠버네티스가 끊는다 → 위와 같음 |
| 쿠버네티스 API 일시 장애 | 300초까지 재시도, 그 뒤 실행을 오류로 끝낸다(Job 자체는 돈다) |
| PVC 가 RWX 가 아님 | Job 이 다른 노드에 뜨면 마운트 못 해 Pending 에 멈춘다 → `nodeSelector` 로 웹 파드 노드에 고정해야 한다 |

"대기 요청도 복구" 가 필수라면 Main 시작 때 `web_run` 은 있고 `job_created` / `web-status.json` / `job-status.json` 은 없는 폴더를 다시 줄에 세우는
증분이 필요하다(`source.zip` 이 있으니 가능). 이건 sbmig 쪽에서 만들 수 있다 — §11-4.

## 8. 순차 슬롯

`RunManager` 는 **한 번에 하나**, 올린 순서(FIFO, `_FifoTurns`)로 Job 을 만든다. 이유는 하나다: **모든 Job 이 같은 파일 KB(`/data/kb-v2`)에 쓴다.**
remediation 이 KB 를 건드리지 않으면 같은 슬롯을 쓸 이유가 없다 — 같은 슬롯을 쓰면 remediation 한 건이 이관 한 건을(또는 반대로) 몇십 분
기다리게 한다. 병렬(3개)로 넓히는 일은 KB 잠금(케이스 폴더 단위)과 함께 가야 하고, 그건 sbmig 쪽 "다음" 항목이다.

## 9. 디스패처를 두 가지 Job 에 쓰는 법 (포크하지 않기)

| 조각 | 계약 |
|---|---|
| `RunManager(out_dir, repo_root, mode=, job_client=, job_template=)` | `mode` in-process / local-job / k8s-job (`SBMIG_WEB_MODE`); 클러스터 밖의 k8s-job 은 WARNING 과 함께 local-job 으로; 클러스터 안인데 서비스 계정이 없으면 시작 거부 |
| `render_job(template, run_id, env, namespace)` | 틀(yaml 경로 또는 dict)에 이름·라벨·namespace·첫 컨테이너 env 만 채운다. **다른 종류의 Job = 다른 틀 + 다른 env** 로 충분하다(명령은 틀의 `command`) |
| `K8sClient.create_job / job_state / delete_job` | requests 기반, 서비스 계정 토큰·CA 자동. `JobState(phase, pod, exit_code, message)` |
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
  선택 `SBMIG_WEB_TOKEN`) / 파일 ConfigMap(`sbmig-config`: `artifacts.yml`, `settings.xml`). 목록은 `deploy/ENV.md`.

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
