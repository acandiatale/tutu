# sbmig 컨테이너 / 쿠버네티스 배포

이 폴더는 sbmig 을 이미지로 만들고 쿠버네티스에 올리는 파일들이다. 운영 방식은 PC 와 같다: **한 번에 프로젝트 하나**를 이관하고,
상태(실행 디렉터리 `out/`, 파일 KB `kb-v2/`, LLM 기록)는 이미지 밖에 둔다.

| 파일 | 역할 |
|---|---|
| `docker/Dockerfile` | **이미지 하나**: Python 3.11 + JDK 21 + Maven + git + 앱 코드. 웹 UI 에이전트와 요청별 Job 모두 이 이미지를 쓴다 |
| `docker/pip-deps.py` | 이미지가 설치할 의존성을 `pyproject.toml` 에서 읽는다(목록을 두 곳에 두지 않기 위해) |
| `docker/run-job.sh` | 요청별 Job 파드의 진입 스크립트: 소스 → KB → `sbmig run` → 결과 push → 종료 코드 |
| `k8s/10-storage.yaml` | PVC 둘: `/data`(웹 UI 에이전트의 업로드·실행·KB·LLM 기록), `~/.m2`(Maven 로컬 저장소) |
| `k8s/20-configmap.yaml` | 회사 설정 `company.yml` + Nexus `settings.xml` |
| `k8s/30-secret.example.yaml` | 비밀값 예시 — `sbmig-llm`(= PC 의 `.env` + 웹 토큰), `sbmig-git`(저장소 토큰). **복사해 채우고 커밋하지 않는다**(`30-secret.yaml` 은 git-ignore) |
| `k8s/40-deployment.yaml`, `k8s/50-service.yaml` | 웹 UI 에이전트(`sbmig serve`) Deployment(replica 1, Recreate) + Service — Job 모델만 쓰면 불필요 |
| `k8s/45-web-rbac.yaml` | 웹 파드가 Job 을 만들고 지켜보는 최소 권한(ServiceAccount + Role + RoleBinding). 디스패치 모드용 |
| `k8s/80-build-job.template.yaml`, `k8s/submit-job.sh` | **인프라 가이드(2026-11-05) 모델**: 요청별 Job 템플릿 + 템플릿을 손으로 돌려 보는 제출 스크립트(운영 방식이 아니라 검증 도구) |

**운영 방식은 하나다**: 에이전트가 웹 화면을 띄우고, 이관 대상 프로젝트의 담당자가 그 화면에 프로젝트 zip 을 올리면 에이전트가
이관을 돌린다. 담당자가 sbmig 명령을 직접 치는 일은 없다.
**목표 구성(§3)** 은 인프라 가이드대로 웹 파드가 업로드마다 빌드 Job 을 만드는 것이다(`sbmig serve` 의 Job 디스패치 모드, 만들 것).
**지금 바로 올릴 수 있는 구성(§2)** 은 웹 파드가 자기 프로세스 안에서 돌리는 것이다(코드 그대로). 화면·이미지·설정 파일은 둘이 같다.

## 1. 이미지 만들기

sbmig 체크아웃 루트에서(회사 레포에서는 `src/migration-agent/` 디렉터리에서), **amd64** 로:

```bash
docker build -f deploy/docker/Dockerfile -t <registry>/sbmig:<tag> .
# 사내 미러:  --build-arg PIP_INDEX_URL=https://nexus.company/repository/pypi/simple --build-arg PIP_TRUSTED_HOST=nexus.company
docker run --rm <registry>/sbmig:<tag> python -m sbmig.cli --help      # 확인
docker push <registry>/sbmig:<tag>
```

코드는 `/app` 에 있고 `PYTHONPATH=/app` 으로 돈다. `pip install` 로 site-packages 에 넣으면 `config/` 와 `knowledge/` 를 못 찾는다
(`config.py` 가 자기 위치에서 레포 루트를 잡는다). apt 미러가 필요하면 `RUN apt-get` 앞에 `/etc/apt/sources.list.d/` 교체 줄을 넣는다.
`JAVA_HOME` 은 `/opt/java21` 심볼릭 링크라 amd64 / arm64 어디서 빌드해도 맞는다.

## 2. ① 웹 UI 에이전트로 올리기

```bash
kubectl apply -f deploy/k8s/10-storage.yaml
kubectl apply -f deploy/k8s/20-configmap.yaml      # <…> 자리를 회사 값으로 채운 뒤
cp deploy/k8s/30-secret.example.yaml deploy/k8s/30-secret.yaml && $EDITOR deploy/k8s/30-secret.yaml
kubectl apply -f deploy/k8s/30-secret.yaml
kubectl apply -f deploy/k8s/40-deployment.yaml -f deploy/k8s/50-service.yaml
kubectl rollout status deploy/sbmig
kubectl exec deploy/sbmig -- python -m sbmig.cli llm-probe             # token exchange: ok … HTTP 200
```

브라우저는 Service 를 통해 `http://<host>:8765/` 로 연다. 사내망 전용 페이지라(2026-11-05 결정) Deployment 가 `SBMIG_WEB_OPEN=1`
로 토큰 없이 열고, 서버는 시작 때 "open to the network WITHOUT a token" 경고를 한 줄 남긴다. 토큰을 두고 싶으면 그 env 를 지우고
Secret 의 `SBMIG_WEB_TOKEN` 을 채운다 — 그때는 `http://<host>:8765/?token=<값>` 으로 한 번 열면 7 일 쿠키가 남고, `--host 0.0.0.0` 에
둘 다 없으면 서버가 뜨지 않는다(결과 zip 에 회사 소스가 들어가서 막아 둔 것).

* **환경 변수는 두 곳**(2026-11-05, 인프라팀의 ConfigMap 목록 요청): ConfigMap **`sbmig-env`** 에 비밀 아닌 것 전부(웹 파드 동작,
  LLM 게이트웨이 주소·에셋 id·role 이름, 설정 덮어쓰기 `SBMIG_<섹션>__<키>`), Secret **`sbmig-llm`** 에 비밀만(`LLM_APP_TOKEN` 또는
  `LLM_API_KEY`, 필요하면 `NEXUS_USER` / `NEXUS_PASSWORD`, 선택 `SBMIG_WEB_TOKEN`). 목록은 아래 "env 목록" 절.
* **ConfigMap `sbmig-config`**: `company.yml` 은 `/app/config/company.yml` 로 들어가고, Deployment 의 `SBMIG_WEB_CONFIGS=company.yml`
  (= `sbmig serve --config company.yml`) 이 **모든 업로드를 이 설정으로 돌린다** — 담당자는 화면에서 설정을 고르지 않는다(고급 설정을
  펼쳐야 목록이 보인다). 컨테이너 경로를 여기 적는다 — `kb.dir: /data/kb-v2`(기본 `kb-v2` 는 이미지 안이라 재시작 때 사라진다),
  `build.settings_file: /etc/sbmig/settings.xml`, `build.repository_url`, `llm.record_dir`.
* **화면**(2026-11-05 새 디자인, 회사 블루): zip 을 끌어다 놓거나 파일 선택 → `마이그레이션 시작` → 단계 레일(접수 → 분석 → 계획 →
  단계들 → 마무리)과 진행 기록 / 로그 탭 → 끝나면 `보고서 받기(report.md)` / `변환된 프로젝트 받기(result.zip)` 두 버튼. 로그는 화면에
  마지막 1,500 줄만 그린다(전체는 `sbmig.log` 링크) — 몇 만 줄이 쌓여 브라우저가 느려지던 문제의 답. 사내망이라 웹 폰트·CDN 없음.
* **리소스**: Maven / OpenRewrite 는 `openrewrite.jvm_args: ["-Xmx4g"]` 로 돈다 → 메모리 limit 6~8 GiB, CPU 2~4. 업로드 상한
  `SBMIG_MAX_UPLOAD_MB`(기본 512, 매니페스트는 1024). **프로브**: 인증 없는 health 경로가 없어 `tcpSocket`.
* `sbmig serve` 가 **곧 uvicorn** 이다(안에서 `uvicorn.run`). 워커는 하나여야 한다 — 실행 큐가 프로세스 안에 있어 `--workers 2` 나
  gunicorn 은 큐를 갈라 놓는다.

### env 목록 — ConfigMap `sbmig-env` / Secret `sbmig-llm` 에 올리는 것

설정의 원본은 세 층이다. `config/default.yml`(이미지 안, 모든 키의 기본값) → ConfigMap 의 `company.yml`(회사 사실: 부모·필수·선택
아티팩트, 레이아웃, 컨테이너 경로) → **환경 변수**(가장 뒤에 적용, 이긴다). `SBMIG_<섹션>__<키>` 는 어떤 설정 키든 덮어쓰고(밑줄 두 개가
한 단계 아래, 값은 YAML 로 읽음), `company.yml` 안에서는 `${VAR}` / `${VAR:-기본}` 으로 환경 변수를 참조할 수 있다. 그래서 **구조적인
사실은 `company.yml`, 시험 운영 중 자주 손대는 숫자와 스위치는 env** 로 나눈다. 바꾼 뒤에는 파드 재시작(`kubectl rollout restart deploy/sbmig`).
모르는 키 이름(철자·들여쓰기)은 시작 로그에 `config: … is not a known key` 로 뜬다.

| 변수 | 어디에 | 기본 / 예 | 언제 바꾸나 |
|---|---|---|---|
| `SBMIG_WEB_CONFIGS` | ConfigMap | `company.yml` | 업로드가 돌 설정 파일을 바꿀 때(쉼표로 여러 개) |
| `SBMIG_WEB_OPEN` | ConfigMap | `1` | 사내망 전용이라 토큰 없이 연다. 토큰을 쓰면 지우고 `SBMIG_WEB_TOKEN` |
| `SBMIG_MAX_UPLOAD_MB` | ConfigMap | `1024` (기본 512) | 큰 프로젝트 zip 이 거절될 때. 0 = 없음 |
| `NO_PROXY` | ConfigMap | 게이트웨이·Nexus 호스트 | 클러스터에 HTTP 프록시가 걸려 있을 때 |
| `LLM_GATEWAY` | ConfigMap | `agent` | 게이트웨이 모양이 바뀔 때(옛 모양 = 지움) |
| `LLM_BASE_URL` | ConfigMap | `https://…/agents/{asset_id}/chat/completions` | 게이트웨이 주소 변경 |
| `LLM_ASSET_ID` | ConfigMap | 에셋 id | 에셋 교체 |
| `LLM_TOKEN_URL` | ConfigMap | 토큰 발급 URL | 발급 경로 변경 |
| `LLM_MODEL` / `LLM_ESCALATION_MODEL` | ConfigMap | `build_error_fixer` / `escalation_fixer` | 게이트웨이의 role 이름이 바뀌거나 상위 모델을 끌 때(비움) |
| `LLM_RECORD_DIR` | ConfigMap | `/data/llm-records` | 기록을 끄거나(비움) 위치를 바꿀 때 |
| `LLM_APP_TOKEN` (또는 `LLM_API_KEY`) | **Secret** | 앱 토큰 | 토큰 교체 |
| `NEXUS_USER` / `NEXUS_PASSWORD` | **Secret** | — | Nexus 그룹이 로그인을 요구할 때(settings.xml 의 `${env.…}` 가 읽음) |
| `SBMIG_WEB_TOKEN` | **Secret**, 선택 | — | 토큰 보호를 켤 때만 |
| `GIT_TOKEN` (+ `GIT_USER`) | **Secret `sbmig-git`** | — | Job 모델만: 소스 clone / 결과·KB push |
| `SBMIG_SCRIPT__MAX_LLM_TRIES_PER_CASE` | ConfigMap(선택) | `20` | 케이스당 LLM 상한 — 회사 실행 결과를 보며 줄인다 |
| `SBMIG_SCRIPT__MAX_AUGMENTS_PER_CASE` | ConfigMap(선택) | `15` | 그중 보강 상한 |
| `SBMIG_SCRIPT__MAX_DECLINES_PER_CASE` | ConfigMap(선택) | `1` | 거절 몇 번에 소유자에게 넘기나 |
| `SBMIG_LOOP__STALL_LIMIT` | ConfigMap(선택) | `2` | 상위 모델로 올라가는 정체 횟수 |
| `SBMIG_LOOP__PHASES__VERIFY__MAX_ITERATIONS` | ConfigMap(선택) | `50` | 빌드-수정 라운드 상한 |
| `SBMIG_LOOP__MAX_TOTAL_MINUTES` | ConfigMap(선택) | `240` | 실행 하나의 시간 상한 |
| `SBMIG_LOOP__MAX_LLM_CALLS` | ConfigMap(선택) | `0` | 프로젝트당 LLM 총 호출 상한(0 없음, -1 저장 스크립트만) |
| `SBMIG_LLM__CASE_THREAD` | ConfigMap(선택) | `false` | 세션 이어 쓰기 — 첫 측정 실행 뒤 `true` |
| `SBMIG_LLM__TIMEOUT_SECONDS` / `MAX_OUTPUT_TOKENS` / `TEMPERATURE` | ConfigMap(선택) | `180` / `8000` / `0.0` | 게이트웨이 특성에 맞출 때 |
| `SBMIG_BUILD__TIMEOUT_MINUTES` | ConfigMap(선택) | `30` | 큰 프로젝트의 `mvn clean package` 가 잘릴 때 |
| `SBMIG_BUILD__REPOSITORY_URL` | ConfigMap(선택) | Nexus 그룹 URL | 환경별(개발/운영) Nexus 가 다를 때 — `company.yml` 값보다 우선 |
| `SBMIG_BUILD__REPOSITORY_TIMEOUT_SECONDS` | ConfigMap(선택) | `10` | Nexus 가 느릴 때 |
| `SBMIG_OPENREWRITE__TIMEOUT_MINUTES` / `JVM_ARGS` | ConfigMap(선택) | `40` / `'["-Xmx4g"]'` | 레시피 실행 시간·힙(limit 보다 2 GiB 이상 작게) |
| `SBMIG_INTAKE__ADF_POLICY` | ConfigMap(선택) | `stop` | ADF 프로젝트를 멈출지(stop) 경고만 할지(warn) |
| `SBMIG_LAYOUT__WIRE_BUNDLED_JARS` | ConfigMap(선택) | `true` | WEB-INF/lib 의 선언 없는 jar 연결을 끌 때 |
| `SBMIG_LIBRARIES__NEXUS_SEARCH` / `INCLUDE_PRERELEASE` | ConfigMap(선택) | `true` / `false` | jakarta 빌드 탐색 범위 |
| `SBMIG_REVIEW__FIX_CRITICAL` | ConfigMap(선택) | `true` | 구조 리뷰의 critical 자동 수정을 끌 때 |

env 가 아닌 것: `company.yml` 과 `settings.xml`(ConfigMap `sbmig-config`, 파일), 이관팀이 고치는 회사 지식 **`<kb.dir>/jar_names.yml`**(jar
이름 → 좌표)과 **`<kb.dir>/libraries.yml`**(사람이 승인한 라이브러리 표) — KB 볼륨(`/data/kb-v2`)에 있어 이미지와 무관하게 바꾼다. 이미지
안의 `knowledge/`(노트, 자문, 레시피 카탈로그)는 이미지를 다시 만들어야 바뀐다.

## 3. 목표 구성 — 웹 파드(`sbmig serve`, Job 디스패치 모드) + 요청별 빌드 Job

인프라 가이드(2026-11-05)의 "Agent 가 요청별 빌드 Job 을 만든다" 를 sbmig 에 그대로 맞춘 그림이다. **Agent = 우리 웹 파드**,
**빌드 Job = sbmig 한 사이클**. 별도 프로젝트를 만들지 않는다. `sbmig serve` 에 실행 모드 하나(자기 안에서 | 쿠버네티스 Job)를 더해,
업로드된 zip 을 자기 프로세스에서 돌리는 대신 Job 을 만들어 돌린다. 화면, 이미지, 설정 파일, 결과 형식은 두 모드가 같다.

```
담당자 브라우저 ──zip 업로드──▶ 웹 파드 (sbmig serve, Job 디스패치 모드)
                                  │ 1 zip 보관 (/data/uploads/<run id>.zip), 일회용 내려받기 토큰 발급
                                  │ 2 Job 생성 (80-build-job.template; SBMIG_SOURCE_ZIP_URL, 결과 POST 주소)
                                  │ 3 pods/log 스트리밍 → 화면의 진행 표시 (노드 · 저널 · 로그)
                                  ▼
                            빌드 Job 파드 (같은 이미지, run-job.sh 한 사이클)
                                  │ 4 zip 내려받기 ← 웹 파드 Service
                                  │ 5 KB clone → sbmig run → 보고서 (emptyDir /workspace)
                                  │ 6 결과 POST → 웹 파드 (report.md, errors.md, result.zip, sbmig.log, journal.jsonl)
                                  │ 7 KB commit + push, 프로젝트 브랜치 sbmig/<run id> push (선택)
                                  ▼ 종료 (exit 0 / 2 / 3 / 4), ttl 뒤 정리
담당자 브라우저 ◀──결과 내려받기── 웹 파드 (/data/out/<run id>/)
```

### 두 파드가 하는 일

| | 웹 파드 (가이드의 Agent) | 빌드 Job 파드 |
|---|---|---|
| 프로그램 | `sbmig serve` (FastAPI + uvicorn, 워커 1, 큐 = 한 번에 하나) | `docker/run-job.sh` → `sbmig run` (LangGraph 그래프, LLM 호출, OpenRewrite, Maven, 수정 루프 전부) |
| 이미지 | `docker/Dockerfile` (같은 이미지, JDK 는 쓰지 않는다) | 같은 이미지 |
| 자원 | CPU 1, 메모리 1~2 GiB | CPU 2~4, 메모리 6~8 GiB (OpenRewrite `-Xmx4g`) |
| 볼륨 | PVC `/data` (업로드, 실행 기록, 결과) | emptyDir `/workspace` 20 Gi, `~/.m2` (PVC 권장) |
| 권한 | ServiceAccount `sbmig-web` (`k8s/45-web-rbac.yaml`) 또는 가이드의 `/etc/kube/config` | 없음 — 쿠버네티스 API 를 부르지 않는다 |
| 네트워크 | → 쿠버네티스 API | → 웹 파드 Service(zip, 결과), LLM 게이트웨이, Nexus, git(KB) |

빌드 파드가 **에이전트 본체**다. LangGraph 그래프와 LLM 호출이 거기서 돈다. 웹 파드는 받고, 만들고, 보여 준다.

### 이미 있는 것 / 만들 것

| 조각 | 상태 |
|---|---|
| 웹 UI: zip 업로드, 접근 토큰, 큐(한 번에 하나), 진행 화면(SSE), 결과 내려받기 | 있음 (`sbmig/web/`) |
| Job 파드 한 사이클: zip URL 입력, KB git clone / push, 결과 push 훅, 종료 코드 | 있음 (`docker/run-job.sh`, 로컬 모의 실행 검증) |
| Job 템플릿, 손으로 제출해 보는 스크립트 | 있음 (`k8s/80-build-job.template.yaml`, `k8s/submit-job.sh`) |
| 웹 파드 RBAC | 있음 (`k8s/45-web-rbac.yaml`, 매니페스트만) |
| `sbmig serve` 의 실행 모드 분기(자기 안에서 / 쿠버네티스 Job) | **만들 것** |
| Job 생성 · 감시 · 로그 · 삭제(kubernetes 클라이언트) | **만들 것** |
| 업로드 내려받기 엔드포인트(일회용 토큰) + 결과 수신 엔드포인트 | **만들 것** |
| Job 로그 → 화면 이벤트 다리(`sbmig run` 의 저널을 표준 출력에도 찍기) | **만들 것** |
| `run-job.sh`: 토큰 헤더로 zip 받기, 결과를 POST 로 보내기 | **만들 것** (소폭) |
| 가짜 kubernetes 클라이언트로 테스트 | **만들 것** |

규모 2~3 일. 중지 버튼 = Job 삭제. 끝난 Job 은 `ttlSecondsAfterFinished` 로 정리. 큐가 한 번에 하나만 돌리므로 Job 도 하나씩 돈다.

### 회의에서 정할 것

1. **Job 분리가 필수인가.** 필수가 아니면 §2 의 구성을 먼저 올려 LLM · Nexus 연결과 실제 프로젝트 몇 건을 검증하고, 디스패치 모드는 뒤에
   켠다. 화면 · 이미지가 같으니 실행 모드만 바꿔 끼운다.
2. **웹 파드 권한.** `k8s/45-web-rbac.yaml` 의 ServiceAccount(네임스페이스 안: jobs create / get / list / watch / delete, pods get / list / watch,
   pods/log get) 또는 가이드의 `/etc/kube/config`.
3. **네트워크.** 빌드 노드 → 웹 파드 Service(8765), LLM 게이트웨이(토큰 URL + 호출 URL), Nexus, git. 웹 파드 → 쿠버네티스 API.
4. **KB 영속성.** git 저장소(`SBMIG_KB_GIT_URL`: Job 시작에 clone, 끝에 push — 한 번에 하나라 충돌 없음) 또는 Job 이 마운트하는 PVC.
5. **`~/.m2`.** emptyDir 이면 Job 마다 Boot BOM · 레시피 · 플러그인을 다시 받는다(수백 MB, 몇 분) → PVC(순차 실행이라 RWO 로 충분) 또는
   미리 채운 이미지 레이어.
6. **결과 보관.** 결과 zip 에 회사 소스가 들어간다 → 웹 파드 `/data` 의 보존 기간, 소스 저장소에 브랜치(`sbmig/<run id>`)를 push 할지.

### 회의 발언용 요약

- 담당자는 웹 화면에 zip 을 올린다. 명령을 치지 않는다.
- 가이드의 Agent 자리는 우리 웹 파드, 빌드 Job 은 sbmig 한 사이클이다. 둘 다 같은 이미지.
- 웹 파드는 "어디서 돌리나" 만 바뀐다(자기 안 → Job). 화면, 설정, 결과 형식은 그대로.
- 빌드 파드가 에이전트 본체다. LLM 호출도 거기서 나가므로 빌드 노드에서 LLM 게이트웨이로 가는 길이 열려야 한다.
- 웹 파드에 필요한 권한은 네임스페이스 안 Job / Pod / 로그에 대한 동사 몇 개뿐이고 매니페스트는 준비돼 있다.
- KB 와 `~/.m2` 는 Job 사이에 살아야 한다. git 저장소인지 PVC 인지 정해 달라.
- 디스패치 모드 구현은 2~3 일. 그 전에도 §2 구성으로 먼저 올려 검증할 수 있다.

### 가이드 ↔ sbmig 대응

| 가이드의 말 | sbmig 에서 |
|---|---|
| "빌드 이미지: JDK, 빌드 도구, 분석 도구, git" | `docker/Dockerfile` 의 이미지 하나 |
| "소스 처리 · 빌드 · 분석 · 결과 저장 · 커밋 뒤 종료" | `docker/run-job.sh`: zip(또는 git) → KB clone → `sbmig run` → 결과 전달 → 종료 코드 |
| "결과 저장", "저장소 커밋" | 결과 POST(웹 파드) 또는 결과 저장소 push(`SBMIG_RESULT_GIT_URL`); 프로젝트 브랜치 `sbmig/<run id>` push |
| `/workspace` emptyDir | `sbmig run --out /workspace/out`; 파드가 죽기 전에 밖으로 보낸 것만 남는다 |
| Agent: `kubectl create -f /templates/build-job.yaml` | 웹 파드의 디스패치 모드(만들 것); 손으로는 `k8s/submit-job.sh` |
| `/etc/kube/config` 는 Agent 에만 | 그대로. 빌드 파드는 쿠버네티스 API 를 부르지 않는다 |

### 템플릿에서 바꿀 자리 (가이드 2절 그대로 + sbmig 용)

`namespace`, `nodeSelector` 라벨, `image`, `imagePullSecrets`; `activeDeadlineSeconds`(이관 한 번이 1~3 시간 — **4 시간** 권장),
`ttlSecondsAfterFinished`(로그 보존), emptyDir `sizeLimit`(작업 공간 20 Gi, `~/.m2` 10 Gi); 요청 환경 변수 `SBMIG_SOURCE_GIT_URL` /
`_REF`(또는 `SBMIG_SOURCE_ZIP_URL`), `SBMIG_KB_GIT_URL`, `SBMIG_RESULT_GIT_URL`; Secret `sbmig-llm`(LLM_*)과 `sbmig-git`(`GIT_TOKEN`, 선택
`GIT_USER`). 토큰은 환경에서만 읽고 URL 에 메모리 안에서만 붙이며 로그·push 파일에 남지 않는다.

### 실행 전 확인 (가이드 5절 + sbmig)

config 계정 권한 `jobs` create/get/delete, `pods` get/list, `pods/log` get. 빌드 파드에서 열려야 하는 곳: Nexus(Maven + `build.repository_url`),
소스 저장소(clone + push), KB·결과 저장소(push), LLM 게이트웨이(토큰 URL + 호출 URL). `--dry-run=server` 는 nodeSelector 라벨 불일치를 못
잡는다 — Pending 이면 라벨을 본다.

### 종료 코드

`run-job.sh` 는 보고서가 나왔으면 0(sbmig exit 0 또는 2 — 빌드 실패도 "결과" 다), 결과를 밖으로 못 보냈으면 4, sbmig 가 중단(exit 3:
preflight / ADF / 내부 오류)이면 3. `SBMIG_FAIL_JOB_ON_BUILD_FAIL=1` 이면 빌드 실패도 Job 실패(2)로 본다.

### 로컬 모의 실행 (검증 2026-11-04)

`run-job.sh` 는 소스·KB·결과 저장소를 로컬 bare 저장소(file://)로 세워 끝까지 돌려 확인했다: 소스 저장소에 `sbmig/<run id>` 브랜치, KB 저장소에
커밋(이 실행이 저장한 케이스 스크립트 포함), 결과 저장소에 `<run id>` 브랜치, 종료 코드 0.

## 4. Nexus 주소

두 곳에 들어간다. (1) `settings.xml` 의 `<mirror><url>` — mvn 이 모든 저장소를 그쪽으로 돌린다(`mirrorOf *`). (2) `company.yml` 의
`build.repository_url` — sbmig 가 메타데이터 / pom 을 직접 읽어 릴리스 목록과 jakarta 빌드를 찾는 주소. 둘 다 `k8s/20-configmap.yaml` 의
`<https://nexus.company/…>` 자리다. Nexus 에 인증이 필요하면 `settings.xml` 의 `<servers>` 는 Secret 으로 빼서 마운트한다.
미러는 꼭 둔다: Maven 3.8 이상은 http:// 저장소를 기본 차단 미러(`maven-default-http-blocker`)로 막는데, Job 파드는 `~/.m2` 캐시가
없어서 미러 없이 http Nexus 만 믿으면 사내 아티팩트마다 `Blocked mirror for repositories` 로 실패한다. `mirrorOf *` 미러가
그 차단보다 먼저 적용되므로 Nexus 가 http 여도 미러를 두면 된다(이관 PC 에서 2026-11-05 에 실제로 겪은 일: `config/settings.nexus.example.xml` 주석).

## 5. 팀원 초안(2026-11-04)에서 바꾼 것

* 웹 토큰 환경 변수는 `WEB_API_TOKEN` 이 아니라 **`SBMIG_WEB_TOKEN`** 이다 — 그리고 사내망 전용이라 기본 배포는 토큰 없이
  `SBMIG_WEB_OPEN=1` 로 연다(2026-11-05).
* `kb.dir` 기본값은 이미지 안 `/app/kb-v2` 라 파드가 바뀌면 KB 가 사라진다 → `company.yml` 에서 `/data/kb-v2`(웹 UI) / Job 모델은 KB 저장소.
* 의존성 목록을 Dockerfile 에 다시 적지 않는다(`pip-deps.py` 가 `pyproject.toml` 을 읽는다). `python-multipart` 는 필요 없다 — 업로드는
  zip 본문을 그대로 스트리밍한다.
* `JAVA_HOME` 은 아키텍처별 경로 대신 `/opt/java21` 심볼릭 링크(팀원 아이디어, 인용부호만 고침).
* 에이전트 + 빌더 파드를 `kubectl exec` 로 잇는 레이아웃은 인프라 가이드의 Job 모델이 나오면서 뺐다(이력: `docs/INTERFACES.md` v2.1z-r/s).
  sbmig 에 남아 있는 `build.setup_command` / `teardown_command` 훅은 그 레이아웃용이었고 Job 모델에서는 비워 둔다.
