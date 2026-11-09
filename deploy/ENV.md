# sbmig 쿠버네티스 환경 변수 목록 (인프라팀 전달용)

작성 2026-11-05. 기준 매니페스트: `deploy/k8s/20-configmap.yaml`(ConfigMap `sbmig-config` = 파일 두 개, ConfigMap `sbmig-env` = 환경 변수),
`deploy/k8s/30-secret.example.yaml`(Secret `sbmig-llm`, `sbmig-git`), `deploy/k8s/40-deployment.yaml`(웹 UI 에이전트),
`deploy/k8s/80-build-job.template.yaml`(요청별 빌드 Job). 자세한 운영 설명은 `deploy/README.md`.

## 1. 원칙

설정은 세 층으로 읽힌다. 뒤가 앞을 이긴다.

1. `config/default.yml` — 이미지 안. 모든 키의 기본값.
2. `artifacts.yml` — ConfigMap `sbmig-config` 의 파일. 회사 사실(부모·필수·선택 아티팩트, 레이아웃, 컨테이너 경로 `kb.dir` /
   `build.settings_file` / `llm.record_dir`).
3. **환경 변수** — ConfigMap `sbmig-env` + Secret. 가장 뒤에 적용된다.

환경 변수 `SBMIG_<섹션>__<키>` 는 어떤 설정 키든 덮어쓴다. 밑줄 두 개(`__`)가 한 단계 아래를 뜻하고, 값은 YAML 로 읽는다
(`"20"` → 숫자, `"true"` → 참, `'["-Xmx4g"]'` → 목록). 예: `SBMIG_LOOP__PHASES__VERIFY__MAX_ITERATIONS=50` = `loop.phases.verify.max_iterations: 50`.
`artifacts.yml` 안에서도 `${VAR}` / `${VAR:-기본값}` 으로 환경 변수를 참조할 수 있지만, **Job 모드의 회사 Worker 는 공유 설정 안의 변수 참조와 비밀값을
거부한다**(`CONFIG_INVALID`: "unresolved variables", "secret interpolation is not allowed", "credential values cannot be stored") — 회사 artifacts.yml 은 값을
그대로 적고, 비밀은 Secret 과 settings.xml 의 `${env.…}` 참조로만 둔다("Maven settings must reference credentials, not contain them").

나누는 기준: **구조적인 회사 사실은 `artifacts.yml`, 시험 운영 중 자주 손대는 숫자·스위치는 env, 토큰·비밀번호는 Secret.**

- 바꾼 뒤에는 파드를 재시작한다: `kubectl rollout restart deploy/sbmig`.
- 키 이름이 틀리면(철자, 들여쓰기) 시작 로그에 `config: … is not a known key and is ignored` 로 뜬다. env 이름이 틀려도 같은 줄이
  `config: env override SBMIG_*__*: …` 로 뜬다. 이 줄이 없으면 이름은 모두 맞은 것이다.
- Deployment 와 Job 은 둘 다 `envFrom: [configMapRef sbmig-env, secretRef sbmig-llm]` 순서로 읽는다. 같은 이름이 양쪽에 있으면 Secret 이 이긴다.

## 2. ConfigMap `sbmig-env` — 웹 파드 동작

| 변수 | 값 | 언제 바꾸나 |
|---|---|---|
| `SBMIG_WEB_CONFIGS` | `artifacts.yml` | 모든 업로드가 이 설정으로 돈다(= `sbmig serve --config`). 쉼표로 여러 개 |
| `SBMIG_WEB_OPEN` | `1` | 사내망 전용 페이지라 토큰 없이 연다. 토큰 보호를 켜려면 이 변수를 지우고 Secret 의 `SBMIG_WEB_TOKEN` 을 채운다 |
| `SBMIG_MAX_UPLOAD_MB` | `1024` | 업로드 zip 상한(MB). 기본 512, `0` = 없음 |
| `NO_PROXY` | `<게이트웨이 호스트>,<nexus 호스트>,localhost,127.0.0.1` | 클러스터에 HTTP 프록시가 걸려 있을 때만 |
| `SBMIG_WEB_MODE` | `in-process` (기본) / `k8s-job` / `local-job` | `k8s-job` = 업로드마다 Job 하나(공유 PVC), README §3 "켜는 법" 뒤에 켠다. 개발 PC 에서는 `k8s-job` 이 자동으로 `local-job`(파드 자리에 로컬 서브프로세스)으로 내려간다 |
| `SBMIG_K8S_NAMESPACE` | (비움 = 파드의 namespace, kubeconfig 면 그 context 의 namespace) | Job 을 다른 namespace 에 만들 때만 |
| `SBMIG_JOB_TEMPLATE` | `/templates/81-web-job.template.yaml` (Deployment 의 env) | 채운 Job 틀을 ConfigMap `sbmig-job-template` 으로 `/templates` 에 마운트한다(README §3). 이미지 안의 `deploy/k8s/81-web-job.template.yaml` 은 자리표시자 그대로라 쓰지 않는다 |
| `KUBECONFIG` | (없음) / `/etc/kube/config` (Deployment 의 env) | 인프라팀이 서비스 계정 대신 kubeconfig Secret 을 주는 방식일 때. 파일이 있으면 서비스 계정 토큰 대신 이것으로 쿠버네티스 API 를 부른다(token / 클라이언트 인증서 사용자; `exec` 는 거절). 개발 PC 에서 `k8s-job` 이 `local-job` 으로 내려가지 않게도 한다 |
| `SBMIG_JOB_POLL_S` | `2` | Job 상태 조회 간격(초) |
| `SBMIG_JOB_STOP_GRACE_S` | `600` | 멈춤 요청(stop.request) 뒤 Job 을 지우기까지 기다리는 시간(초) |
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
| `LLM_RECORD_DIR` | (비움) | 요청·응답 기록(비밀 없음). **회사 Worker 는 기록이 켜진 설정을 거부한다**("without raw recording") — Job 모드에서는 비워 두고, in-process 진단 때만 `/data/llm-records` |
| `LLM_RESPONSE_PATH` | (비움) | `sbmig llm-probe` 가 응답 본문을 못 찾을 때만 |

## 4. Secret — 여기에만 둔다 (ConfigMap·이미지·로그 금지)

| 변수 | Secret | 뜻 |
|---|---|---|
| `LLM_APP_TOKEN` | `sbmig-llm` | 토큰 교환에 쓰는 앱 토큰. 교환이 없는 게이트웨이면 대신 `LLM_API_KEY` 에 Bearer 값 |
| `NEXUS_USER` / `NEXUS_PASSWORD` | `sbmig-llm` | Nexus 그룹이 로그인을 요구할 때만. `settings.xml` 의 `<server>` 가 `${env.NEXUS_USER}` / `${env.NEXUS_PASSWORD}` 로 읽는다 |
| `SBMIG_WEB_TOKEN` | `sbmig-llm` (선택) | 웹 토큰 보호를 켤 때만. 켜면 브라우저는 `http://<host>:8765/?token=<값>` 으로 한 번 연다 |
| `GIT_TOKEN` (+ `GIT_USER`) | `sbmig-git` | 요청별 Job 모델만: 소스 clone, 결과·KB push |

## 5. ConfigMap `sbmig-env` — 자주 조정하는 설정 키 (선택, 기본값을 적어 둠)

20-configmap.yaml 에 주석으로 들어 있다. 필요한 줄의 주석을 풀면 `artifacts.yml` 보다 우선한다.

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
| `SBMIG_BUILD__REPOSITORY_URL` | `artifacts.yml` 값 | 환경별(개발/운영) Nexus 그룹 URL 이 다를 때 |
| `SBMIG_BUILD__REPOSITORY_TIMEOUT_SECONDS` | `10` | sbmig 의 Nexus 조회(메타데이터 / pom) 대기 |
| `SBMIG_OPENREWRITE__TIMEOUT_MINUTES` | `40` | 레시피 실행 상한 |
| `SBMIG_OPENREWRITE__JVM_ARGS` | `'["-Xmx4g"]'` | 레시피 Maven 의 힙. 파드 memory limit 보다 2 GiB 이상 작게 |
| `SBMIG_INTAKE__ADF_POLICY` | `stop` | ADF(JDeveloper) 프로젝트: `stop` 변환 불가로 끝 / `warn` 경고만 |
| `SBMIG_LAYOUT__WIRE_BUNDLED_JARS` | `true` | WEB-INF/lib 의 선언 없는 jar 를 재구성 때 연결 |
| `SBMIG_LIBRARIES__NEXUS_SEARCH` | `true` | jakarta 빌드 탐색에 Nexus 검색 API 사용 |
| `SBMIG_LIBRARIES__INCLUDE_PRERELEASE` | `false` | 프리릴리스 버전도 후보로 |
| `SBMIG_REVIEW__FIX_CRITICAL` | `true` | 구조 리뷰의 critical 을 자동 수정 |
| `SBMIG_FLEET__MIN_FREE_GB` | `5` | 실행 전 디스크 여유 하한 |

## 6. 환경 변수가 아닌 것

| 항목 | 어디에 | 비고 |
|---|---|---|
| `artifacts.yml` | ConfigMap `sbmig-config` → `/app/config/artifacts.yml` | 회사 아티팩트·레이아웃·컨테이너 경로. 틀은 `config/artifacts.example.yml` |
| `settings.xml` | ConfigMap `sbmig-config` → `/etc/sbmig/settings.xml` | Maven 미러(`mirrorOf *` 또는 `external:*`) → Nexus 그룹. 로그인은 `${env.NEXUS_USER}` |
| `<kb.dir>/jar_names.yml` | KB 볼륨 `/data/kb-v2` | jar 파일 이름 → Nexus 좌표(이관팀이 채움). 이미지와 무관 |
| `<kb.dir>/libraries.yml` | KB 볼륨 `/data/kb-v2` | 사람이 승인한 라이브러리 표(javax → jakarta 좌표 등) |
| `knowledge/` (노트·자문·레시피 카탈로그) | 이미지 안 `/app/knowledge` | 바꾸려면 이미지를 다시 만든다 |

## 7. 적용과 확인

```bash
kubectl apply -f deploy/k8s/20-configmap.yaml                 # sbmig-config + sbmig-env (<…> 자리를 채운 뒤)
cp deploy/k8s/30-secret.example.yaml deploy/k8s/30-secret.yaml && $EDITOR deploy/k8s/30-secret.yaml
kubectl apply -f deploy/k8s/30-secret.yaml
kubectl rollout restart deploy/sbmig && kubectl rollout status deploy/sbmig
kubectl logs deploy/sbmig | grep -E "config: |sbmig web UI ready|open to the network"   # 틀린 키 이름 / 시작 상태
kubectl exec deploy/sbmig -- python -m sbmig.cli llm-probe                              # 게이트웨이 토큰 교환 … HTTP 200
```
