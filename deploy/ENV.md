# sbmig 쿠버네티스 환경 변수 목록 (인프라팀 전달용)

작성 2026-11-05, 고침 2026-11-10(회사 설정을 이미지에 굽는다; Job 모드의 환경 변수 대조·Job 종료 확인·출력 정제). 기준 매니페스트: `deploy/k8s/20-configmap.yaml`(ConfigMap `sbmig-config` =
`settings.xml`, ConfigMap `sbmig-env` = 환경 변수), `deploy/k8s/30-secret.example.yaml`(Secret `sbmig-llm`, `sbmig-git`),
`deploy/k8s/40-deployment.yaml`(웹 UI 에이전트), `deploy/k8s/81-web-job.template.yaml`(웹 파드가 업로드마다 만드는 Job 의 틀),
`deploy/k8s/80-build-job.template.yaml`(git 으로 받는 요청별 빌드 Job). 자세한 운영 설명은 `deploy/README.md`.

## 1. 원칙

설정은 세 층으로 읽힌다. 뒤가 앞을 이긴다.

1. `config/default.yml` — 이미지 안. 모든 키의 기본값.
2. `artifacts.yml` — 이미지 안 `/app/config/artifacts.yml`. 이관팀이 PC 에서 시험한 파일을 이미지에 굽는다(README §1). 회사 사실(부모·필수·선택
   아티팩트, 레이아웃, Nexus 주소).
3. **환경 변수** — 매니페스트의 `env`(컨테이너 경로, §6) + ConfigMap `sbmig-env` + Secret. 가장 뒤에 적용된다.

환경 변수 `SBMIG_<섹션>__<키>` 는 어떤 설정 키든 덮어쓴다. 밑줄 두 개(`__`)가 한 단계 아래를 뜻하고, 값은 YAML 로 읽는다
(`"20"` → 숫자, `"true"` → 참, `'["-Xmx4g"]'` → 목록). 예: `SBMIG_LOOP__PHASES__VERIFY__MAX_ITERATIONS=50` = `loop.phases.verify.max_iterations: 50`.
`artifacts.yml` 안에서도 `${VAR}` / `${VAR:-기본값}` 으로 환경 변수를 참조할 수 있다. **비밀은 어느 파일에도 적지 않는다**: 이미지에 굽는
`artifacts.yml` 은 이미지를 받는 누구나 읽고, ConfigMap 도 평문이다. 토큰·비밀번호는 Secret 으로만 오고 `settings.xml` 은 `${env.…}` 로 참조만 한다.

나누는 기준: **구조적인 회사 사실은 `artifacts.yml`, 시험 운영 중 자주 손대는 숫자·스위치는 env, 토큰·비밀번호는 Secret.**

- `sbmig-env` 를 바꾼 뒤에는 **웹 파드를 재시작한다**(`kubectl -n agent rollout restart deploy/sbmig`). 웹 파드는 시작할 때 한 번 읽고 Job 파드는
  만들어질 때 읽는다 — Job 모드에서는 Worker 가 자기 환경을 그 Job 을 만든 웹 파드의 것과 대조해, 다르면 몇 초 만에 다른 키 이름을 적고
  멈춘다(기본; §2 `SBMIG_JOB_ENV_PIN`, README §3 "환경 변수 대조"). 81 틀을 새로 받았으면 ConfigMap `sbmig-job-template` 도 다시 만든 뒤
  재시작한다(웹 파드는 시작할 때 틀을 읽어 둔다; §8).
- **값을 비워 두지 않는다.** `SBMIG_<섹션>__<키>: ""` 는 YAML null 로 읽혀 구운 yml 의 값을 "없음" 으로 덮는다(기본값으로 돌아가지 않고,
  키에 따라 실행이 시작부터 실패한다). 안 쓸 키는 값을 지우지 말고 줄째 주석으로 둔다.
- 키 이름이 틀리면(철자, 들여쓰기) 시작 로그에 `config: … is not a known key and is ignored` 로 뜬다. env 이름이 틀려도 같은 줄이
  `config: env override SBMIG_*__*: …` 로 뜬다. 이 줄이 없으면 이름은 모두 맞은 것이다.
- 웹 파드(40)와 80 의 Job 은 `envFrom: [configMapRef sbmig-env, secretRef sbmig-llm]` 순서로 읽는다. 같은 이름이 양쪽에 있으면 Secret 이 이기고,
  매니페스트의 `env` 에 직접 적힌 이름은 둘 다 이긴다.
- **웹 파드가 만드는 Job(81 틀)은 `envFrom` 을 쓰지 않는다**(Main 이 시작할 때 틀을 검증하고 거절한다). 변수마다 `configMapKeyRef` / `secretKeyRef` 한 줄이며,
  §3 의 LLM 값과 §5 의 키는 전부 틀에 들어 있다. `sbmig-env` 에 표에 없는 새 키를 더하면 81 틀에도 한 줄 더해야 Job 파드에 닿는다 — 설정 키
  (`SBMIG_<섹션>__<키>`, 설정 파일이 읽는 LLM 값)라면 그 줄 없이는 대조가 "웹 파드에만 있음" 으로 Job 을 멈춘다.

## 2. ConfigMap `sbmig-env` — 웹 파드 동작

| 변수 | 값 | 언제 바꾸나 |
|---|---|---|
| `SBMIG_WEB_CONFIGS` | `artifacts.yml` | 모든 업로드가 이 설정으로 돈다(= `sbmig serve --config`). 이미지 `/app/config/` 아래 파일 이름, 쉼표로 여러 개 |
| `SBMIG_WEB_OPEN` | `1` | 사내망 전용 페이지라 토큰 없이 연다. 토큰 보호를 켜려면 이 변수를 지우고 Secret 의 `SBMIG_WEB_TOKEN` 을 채운다 |
| `SBMIG_MAX_UPLOAD_MB` | `1024` | 업로드 zip 상한(MB). 기본 512, `0` = 없음 |
| `HTTP_PROXY` / `HTTPS_PROXY` | `http://<프록시 호스트>:<포트>` | 클러스터에서 프록시로 나가야 할 때만(웹 파드와 Job 파드가 같이 읽는다) |
| `NO_PROXY` | `<게이트웨이 호스트>,<nexus 호스트>,localhost,127.0.0.1` | 프록시를 켰을 때 사내 주소를 빼는 목록 |
| `SBMIG_WEB_MODE` | `k8s-job` (매니페스트 값) / `in-process` (변수가 없을 때) / `local-job` | `k8s-job` = 업로드마다 Job 하나(공유 PVC). 켜진 채로 나가므로 권한(회사: kubeconfig Secret, 없는 클러스터: 45)과 Job 틀 ConfigMap 이 웹 파드보다 먼저 있어야 한다(README §3 "켜는 법"); 줄을 주석 처리하면 웹 파드가 직접 돌린다. 개발 PC 에서는 `k8s-job` 이 자동으로 `local-job`(파드 자리에 로컬 서브프로세스)으로 내려간다 |
| `SBMIG_K8S_NAMESPACE` | `agent` (매니페스트 값) | Job 을 만드는 namespace. 줄이 없으면 kubeconfig 방식은 그 context 의 namespace, 그것도 없으면 `default`(PVC `sbmig-data` 를 못 찾는다); 서비스 계정 방식은 파드의 namespace |
| `SBMIG_JOB_TEMPLATE` | `/templates/81-web-job.template.yaml` (Deployment 의 env) | 채운 Job 틀을 ConfigMap `sbmig-job-template` 으로 `/templates` 에 마운트한다(README §3). 이미지 안의 `deploy/k8s/81-web-job.template.yaml` 은 자리표시자 그대로라 쓰지 않는다 |
| `SBMIG_WORKER_LLM_RECORDS` | (없음) / `"1"` (Job 틀의 env) | Worker 가 LLM 원문 기록을 허용할지. 기본은 끈다(원본 프롬프트 = 소스 코드가 공유 볼륨에 더 남지 않게). 진단 때만 틀의 두 줄을 같이 푼다: 이 스위치 + `SBMIG_LLM__RECORD_DIR: "/data/llm-records"`. 스위치 없이 폴더만 주면 Worker 가 그 변수를 넘기지 않는다 |
| `KUBECONFIG` | `/etc/kube/config` (Deployment 의 env, 매니페스트 값) | 회사 방식: 인프라팀이 준 kubeconfig Secret(`agent-kubeconfig`, 키 `config`)을 `/etc/kube` 에 마운트한다. 파일이 있으면 서비스 계정 토큰 대신 이것으로 쿠버네티스 API 를 부른다(token / 클라이언트 인증서 사용자; `exec` 는 거절). 개발 PC 에서 `k8s-job` 이 `local-job` 으로 내려가지 않게도 한다 |
| `SBMIG_JOB_POLL_S` | `2` | Job 상태 조회 간격(초) |
| `SBMIG_JOB_STOP_GRACE_S` | `2700` | 멈춤 요청(stop.request) 뒤 Job 을 지우기까지 기다리는 시간(초) — Maven 빌드 + OpenRewrite 한 번보다 길게 |
| `SBMIG_JOB_EXIT_GRACE_S` | `120` | Worker 가 결과(`job-status.json`)를 쓴 뒤에도 파드가 남아 있으면 이 초 뒤에 그 Job(우리 라벨인 것만)을 지우고 사라질 때까지 기다린다. 다음 업로드는 Job 이 끝난 것을 확인한 뒤에 시작한다 |
| `SBMIG_JOB_API_GRACE_S` | `300` | **알림만**(2026-11-10 부터): 쿠버네티스 API 가 이 초 동안 답하지 않으면 로그 ERROR 한 줄·저널 `job_api_unavailable`·화면 안내로 한 번 알리고 그 뒤 10분마다 로그에 다시 남긴다. 실행을 실패로 끝내지도, 차례를 넘기지도 않는다 — API 가 다시 답해(`job_api_back`) Job 이 끝난 것을 확인할 때까지 다음 업로드는 `queued` 로 기다린다 |
| `SBMIG_JOB_ENV_PIN` | `refuse` (k8s-job 기본) / `warn` / `off` (local-job 기본) | Job 파드의 환경 변수(설정 파일의 `${…}` 변수, `SBMIG_<섹션>__<키>`)가 그 Job 을 만든 웹 파드와 다를 때: `refuse` = 그 Job 은 돌지 않고 다른 키 이름을 적고 끝난다, `warn` = 저널 `worker_env_differs` 에 적고 Job 파드의 값으로 돈다, `off` = 대조하지 않는다. 웹 파드가 시작할 때 읽는다(바꾸면 재시작). 81 틀에는 줄이 필요 없다 — Worker 는 웹 파드가 `control/state.json` 에 적은 정책을 따른다. `warn` 은 개발 PC 가 `KUBECONFIG` 로 클러스터에 Job 을 만들 때처럼 두 환경이 원래 다른 경우에만 |
| `SBMIG_JOB_STATUS_GRACE_S` | `90` | Job 이 끝난 뒤 Worker 의 `job-status.json` 이 보일 때까지 기다리는 초. NFS 의 속성 캐시(기본 최대 60초)보다 길어야 끝난 결과를 "검증 안 됨" 으로 잘못 적지 않는다 |
| `SBMIG_LOCAL_JOB_ECHO` | `1` | 개발 PC 의 `local-job` 에서 서브프로세스 출력을 서버 콘솔에도 `[job <run id>]` 로 흘릴지. `0` = 파일(job-local.log)에만 |
| `SBMIG_LOCAL_JOB_COLUMNS` | `160` | `local-job` 서브프로세스의 콘솔 줄 폭(파이프라 기본 80칸으로 접힌다). 자식은 항상 UTF-8 로 쓴다 |

## 3. ConfigMap `sbmig-env` — LLM 게이트웨이 (비밀이 아닌 것)

| 변수 | 값 | 뜻 |
|---|---|---|
| `LLM_GATEWAY` | `agent` | 사내 GPT 에이전트 게이트웨이 페이로드. 옛 모양의 게이트웨이면 변수를 지운다 |
| `LLM_BASE_URL` | `https://<게이트웨이 호스트>/api/serv/v1/agents/{asset_id}/chat/completions` | 호출 URL. `{asset_id}` 자리는 아래 값으로 채워진다 |
| `LLM_ASSET_ID` | `<에셋 id>` | 게이트웨이 에셋 |
| `LLM_TOKEN_URL` | `https://<게이트웨이 호스트>/<토큰 발급 경로>` | 앱 토큰 → 접근 토큰 교환 주소 |
| `LLM_MODEL` | `build_error_fixer` | `params.role` — 수정 1~2회차와 구조 리뷰. 게이트웨이가 모델에 매핑 |
| `LLM_ESCALATION_MODEL` | `escalation_fixer` | `params.role` — 케이스 3회차부터 / 정체 뒤. 비우면 상위 모델 없음 |
| `LLM_RECIPE_MODEL` | (주석) | 계획 검토(`plan.advisor: llm`)의 값싼 모델. 비우면 `LLM_MODEL`; 기본 흐름은 쓰지 않는다. 81 틀에 키 줄이 있다(두 파드의 값을 대조하므로) |
| `LLM_RECORD_DIR` | (비움) | 요청·응답 기록(비밀 없음, 프로젝트 소스가 들어간다). 웹 파드 안에서 돌리는 in-process 진단 때만 `/data/llm-records`. Job 틀에는 넣을 수 없다(위 `SBMIG_WORKER_LLM_RECORDS` 줄) |
| `LLM_RESPONSE_PATH` | (비움) | `sbmig llm-probe` 가 응답 본문을 못 찾을 때만 |

## 4. Secret — 여기에만 둔다 (ConfigMap·이미지·로그 금지)

| 변수 | Secret | 뜻 |
|---|---|---|
| `LLM_APP_TOKEN` | `sbmig-llm` | 토큰 교환에 쓰는 앱 토큰. 교환이 없는 게이트웨이면 대신 `LLM_API_KEY` 에 Bearer 값 |
| `NEXUS_USER` / `NEXUS_PASSWORD` | `sbmig-llm` | Nexus 그룹이 로그인을 요구할 때만. `settings.xml` 의 `<server>` 가 `${env.NEXUS_USER}` / `${env.NEXUS_PASSWORD}` 로 읽는다 |
| `SBMIG_WEB_TOKEN` | `sbmig-llm` (선택) | 웹 토큰 보호를 켤 때만. 켜면 브라우저는 `http://<host>:8765/?token=<값>` 으로 한 번 연다 |
| `GIT_TOKEN` (+ `GIT_USER`) | `sbmig-git` | 요청별 Job 모델만: 소스 clone, 결과·KB push |

## 5. ConfigMap `sbmig-env` — 자주 조정하는 설정 키 (선택, 기본값을 적어 둠)

20-configmap.yaml 에 주석으로 들어 있다. 필요한 줄의 주석을 풀면 구운 `artifacts.yml` 보다 우선한다. 웹 파드와 Job 파드(81 틀이 같은 키를 읽는다) 양쪽에 적용된다.

| 변수 | 기본 | 뜻 |
|---|---|---|
| `SBMIG_SCRIPT__MAX_LLM_TRIES_PER_CASE` | `20` | 케이스당 LLM 요청 상한(넘으면 BLOCKED). 회사 실행 결과를 보며 줄인다 |
| `SBMIG_SCRIPT__MAX_AUGMENTS_PER_CASE` | `15` | 그중 보강(augment) 상한 |
| `SBMIG_SCRIPT__MAX_DECLINES_PER_CASE` | `1` | LLM 거절 몇 번에 소유자에게 넘기나 |
| `SBMIG_LOOP__STALL_LIMIT` | `2` | 같은 오류 묶음이 이 횟수 반복되면 상위 모델 |
| `SBMIG_LOOP__PHASES__VERIFY__MAX_ITERATIONS` | `50` | 빌드-수정 라운드 상한 |
| `SBMIG_LOOP__MAX_TOTAL_MINUTES` | `240` | 실행 하나의 시간 상한(분) |
| `SBMIG_LOOP__MAX_LLM_CALLS` | `0` | 프로젝트당 LLM 총 호출. `0` 없음, `-1` 저장 스크립트만 |
| `SBMIG_LLM__CASE_THREAD` | `false` | 케이스 스레드(게이트웨이 세션 이어 쓰기). 첫 측정 실행 뒤 `true` |
| `SBMIG_LLM__TIMEOUT_SECONDS` | `180` | 게이트웨이 응답 대기 |
| `SBMIG_LLM__MAX_OUTPUT_TOKENS` | `8000` | 응답 길이 상한 |
| `SBMIG_LLM__TEMPERATURE` | `0.0` | |
| `SBMIG_BUILD__TIMEOUT_MINUTES` | `30` | `mvn clean package` 한 번의 상한 |
| `SBMIG_BUILD__REPOSITORY_URL` | 구운 `artifacts.yml` 값 | 클러스터에서 쓸 Nexus 그룹 URL 이 PC 와 다를 때 |
| `SBMIG_BUILD__REPOSITORY_TIMEOUT_SECONDS` | `10` | sbmig 의 Nexus 조회(메타데이터 / pom) 대기 |
| `SBMIG_OPENREWRITE__TIMEOUT_MINUTES` | `40` | 레시피 실행 상한 |
| `SBMIG_OPENREWRITE__JVM_ARGS` | `'["-Xmx4g"]'` | 레시피 Maven 의 힙. 파드 memory limit 보다 2 GiB 이상 작게 |
| `SBMIG_INTAKE__ADF_POLICY` | `stop` | ADF(JDeveloper) 프로젝트: `stop` 변환 불가로 끝 / `warn` 경고만 |
| `SBMIG_LAYOUT__WIRE_BUNDLED_JARS` | `true` | WEB-INF/lib 의 선언 없는 jar 를 재구성 때 연결 |
| `SBMIG_LIBRARIES__NEXUS_SEARCH` | `true` | jakarta 빌드 탐색에 Nexus 검색 API 사용 |
| `SBMIG_LIBRARIES__INCLUDE_PRERELEASE` | `false` | 프리릴리스 버전도 후보로 |
| `SBMIG_REVIEW__FIX_CRITICAL` | `true` | 구조 리뷰의 critical 을 자동 수정 |
| `SBMIG_FLEET__MIN_FREE_GB` | `5` | 실행 전 디스크 여유 하한 |

## 6. 매니페스트에 고정된 컨테이너 경로 (채울 것 없음)

이미지에 구운 `artifacts.yml` 은 PC 에서 시험한 파일이라 PC 경로가 들어 있을 수 있다. 아래 줄이 매니페스트의 `env` 에 들어 있어 컨테이너 값으로 덮어쓴다.
`sbmig-env` 에는 넣지 않는다(80 의 Job 은 KB 폴더와 기록 폴더를 `run-job.sh` 가 정한다).

| 변수 | 값 | 들어 있는 매니페스트 |
|---|---|---|
| `SBMIG_KB__DIR` | `/data/kb-v2` | 40 (81 은 `SBMIG_KB_DIR`, 80 은 `run-job.sh`) |
| `SBMIG_BUILD__SETTINGS_FILE` | `/etc/sbmig/settings.xml` | 40 · 81 · 80 |
| `SBMIG_BUILD__JAVA_HOME` | `/opt/java21` | 40 · 81 · 80 |
| `SBMIG_BUILD__MVN` | `mvn` | 40 · 81 · 80 |
| `SBMIG_BUILD__LOCAL_REPO` | `null` (`~/.m2` 볼륨) | 40 · 81 · 80 |
| `SBMIG_SCRIPT__PYTHON` | `null` (이미지의 python) | 40 · 81 · 80 |
| `SBMIG_LLM__RECORD_DIR` | `${LLM_RECORD_DIR:-}` | 40 — `LLM_RECORD_DIR` 가 있을 때만 기록한다 |
| `SBMIG_REDACT_OUTPUT` | `1` | 40 · 80 (81 은 없음 — Worker 가 자식 `sbmig run` 에 언제나 켠다) |

`SBMIG_REDACT_OUTPUT` 은 경로가 아니라 스위치다(2026-11-10). `1` 이면 sbmig 가 로그 줄·저널 줄·예외 traceback·화면 이벤트에서 비밀 값
(이름에 TOKEN / PASSWORD / SECRET / API_KEY 가 든 환경 변수의 값, 실행 중에 교환해 받은 게이트웨이 토큰, URL 의 `user:pass@`)을 **쓰는 순간**
`***` 로 바꾼다 — 파드 로그와 화면의 로그 탭에도 값이 나가지 않는다. 실행이 끝날 때 실행 폴더(보고서, `logs/` 의 Maven 출력)를 한 번 더 훑으므로
실행 중에는 `sbmig.log` / `journal.jsonl` 만 내려받을 수 있다(나머지는 HTTP 409). 실패하면 닫힌다: 정제기를 걸 수 없으면 `sbmig` 은 exit 3, 웹 서버는
뜨지 않고, 한 줄을 지우다 실패하면 그 줄 대신 `line withheld` 가 든 고정 문구가 나간다. 끄지 않는다.

## 7. 환경 변수가 아닌 것

| 항목 | 어디에 | 비고 |
|---|---|---|
| `artifacts.yml` | 이미지 안 `/app/config/artifacts.yml` | 회사 아티팩트·레이아웃·Nexus 주소. 이관팀이 빌드 때 굽는다(`--build-arg REQUIRE_CONFIG=artifacts.yml`). 바꾸려면 이미지를 다시 만든다. 틀은 `config/artifacts.example.yml` |
| `settings.xml` | ConfigMap `sbmig-config` → `/etc/sbmig/settings.xml` | Maven 미러(`mirrorOf *` 또는 `external:*`) → Nexus 그룹. 로그인은 `${env.NEXUS_USER}` |
| `<kb.dir>/jar_names.yml` | KB 볼륨 `/data/kb-v2` | jar 파일 이름 → Nexus 좌표(이관팀이 채움). 이미지와 무관 |
| `<kb.dir>/libraries.yml` | KB 볼륨 `/data/kb-v2` | 사람이 승인한 라이브러리 표(javax → jakarta 좌표 등) |
| `knowledge/` (노트·자문·레시피 카탈로그) | 이미지 안 `/app/knowledge` | 바꾸려면 이미지를 다시 만든다 |

## 8. 적용과 확인

매니페스트마다 `namespace: agent` 가 들어 있어 `apply` 에는 `-n` 이 필요 없다.

```bash
kubectl apply -f deploy/k8s/20-configmap.yaml                 # sbmig-config(settings.xml) + sbmig-env (<…> 자리를 채운 뒤)
cp deploy/k8s/30-secret.example.yaml deploy/k8s/30-secret.yaml && $EDITOR deploy/k8s/30-secret.yaml
kubectl apply -f deploy/k8s/30-secret.yaml
kubectl -n agent create configmap sbmig-job-template --from-file=deploy/k8s/81-web-job.template.yaml --dry-run=client -o yaml | kubectl apply -f -   # Job 모드: 81 틀을 새로 받았을 때
kubectl -n agent rollout restart deploy/sbmig && kubectl -n agent rollout status deploy/sbmig   # sbmig-env / Job 틀을 바꾼 뒤에는 언제나
kubectl -n agent logs deploy/sbmig | grep -E "config: |sbmig web UI ready|open to the network"   # 틀린 키 이름 / 시작 상태
kubectl -n agent exec deploy/sbmig -- ls /app/config/artifacts.yml                               # 회사 설정이 이미지에 들어 있는지
kubectl -n agent exec deploy/sbmig -- python -m sbmig.cli llm-probe                              # 게이트웨이 토큰 교환 … HTTP 200
```
