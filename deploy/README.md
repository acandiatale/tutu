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
| `k8s/80-build-job.template.yaml`, `k8s/submit-job.sh` | **인프라 가이드(2026-11-05) 모델**: 요청별 Job 템플릿 + Agent 쪽 제출 스크립트 |

두 운영 모양 중 하나를 고른다. **① 웹 UI 에이전트**(PC 와 같은 `sbmig serve`, 10~50) 또는 **② 요청별 Job**(인프라 가이드, 20·30·80).
설정과 비밀값(20, 30)은 둘이 공유한다.

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

브라우저는 Service 를 통해 `http://<host>:8765/?token=<SBMIG_WEB_TOKEN>` 로 한 번 열면 7 일 쿠키가 남는다. `--host 0.0.0.0` 이라
토큰이 없으면 서버가 뜨지 않는다(결과 zip 에 회사 소스가 들어가서 막아 둔 것).

* **Secret `sbmig-llm`** = PC 의 `.env`: `LLM_GATEWAY=agent`, `LLM_BASE_URL`(경로의 `{asset_id}` 는 `LLM_ASSET_ID` 로 채워짐),
  `LLM_ASSET_ID`, `LLM_TOKEN_URL`, `LLM_APP_TOKEN`, `LLM_MODEL=build_error_fixer`, `LLM_ESCALATION_MODEL=escalation_fixer`,
  `LLM_RECORD_DIR`, `SBMIG_WEB_TOKEN`.
* **ConfigMap `sbmig-config`**: `company.yml` 은 `/app/config/company.yml` 로 들어가 UI 의 config 목록에 나온다. 컨테이너 경로를 여기
  적는다 — `kb.dir: /data/kb-v2`(기본 `kb-v2` 는 이미지 안이라 재시작 때 사라진다), `build.settings_file: /etc/sbmig/settings.xml`,
  `build.repository_url`, `llm.record_dir`.
* **리소스**: Maven / OpenRewrite 는 `build.jvm_args: ["-Xmx4g"]` 로 돈다 → 메모리 limit 6~8 GiB, CPU 2~4. 업로드 상한
  `SBMIG_MAX_UPLOAD_MB`(기본 512, 매니페스트는 1024). **프로브**: 인증 없는 health 경로가 없어 `tcpSocket`.
* `sbmig serve` 가 **곧 uvicorn** 이다(안에서 `uvicorn.run`). 워커는 하나여야 한다 — 실행 큐가 프로세스 안에 있어 `--workers 2` 나
  gunicorn 은 큐를 갈라 놓는다.

## 3. ② 요청별 빌드 Job 모델 (인프라 가이드 2026-11-05)

가이드의 모델: **요청마다 Job 하나**, 그 파드가 "소스 처리 → 빌드 → 분석 → 결과 저장 → 저장소 커밋" 을 전부 하고 끝난다. 작업 공간은
`/workspace` emptyDir(파드와 함께 사라짐). Agent 는 `kubectl --kubeconfig=/etc/kube/config create -f` 로 Job 을 만들고 로그 / 결과를 본다.
sbmig 에 맞추면 이렇게 된다.

| 가이드의 말 | sbmig 에서 |
|---|---|
| "빌드 이미지: JDK, 빌드 도구, 분석 도구, git" | `docker/Dockerfile` 의 이미지. `build-runner` 자리에 이 이미지 |
| "소스 처리·빌드·분석·결과 저장·커밋 뒤 종료" | `docker/run-job.sh`: 소스 clone(또는 zip, 마운트) → KB clone → `sbmig run` → 결과 push → 종료 코드 |
| "저장소 커밋" | 실행 디렉터리의 `project/` 는 이미 git 저장소(브랜치 `sbmig/<run id>`) → 소스 저장소에 그 브랜치를 push |
| "결과 저장" | `report.md` / `errors.md` / `sbmig.log` / `journal.jsonl` / `logs/` 를 결과 저장소(`SBMIG_RESULT_GIT_URL`, 브랜치 = run id)에 push, 또는 `RESULT_PUSH_CMD` |
| `/workspace` emptyDir | sbmig 의 `--out /workspace/out`; 파드가 죽기 전에 push 되는 것만 남는다 |
| Agent: `kubectl create -f /templates/build-job.yaml` | `k8s/80-build-job.template.yaml` + `k8s/submit-job.sh`(권한 확인 → `--dry-run=server` → create → `logs -f` → `SBMIG-RESULT`) |
| `/etc/kube/config` 는 Agent 에만 | 그대로. 빌드 파드는 쿠버네티스 API 를 부르지 않는다(sbmig 도 안 부른다) |

### 꼭 결정해야 할 세 가지

1. **KB 영속성.** sbmig 의 핵심 자산은 파일 KB(케이스별 수정 스크립트와 성적)이고 다음 프로젝트가 재사용해야 한다. emptyDir 에 두면
   Job 마다 사라진다. 기본 해법: **KB 를 git 저장소로**(`SBMIG_KB_GIT_URL`) — Job 시작에 clone, 끝에 commit + push. 파일 KB 라 그대로
   된다. 조건은 Job 을 **한 번에 하나만** 돌리는 것(동시에 둘이 push 하면 충돌; 어차피 순차 이관이 운영 모델). 인프라가 Job 에 PVC 를
   허용하면 `sbmig-data` 류 PVC 에 `kb.dir` 을 두는 쪽이 더 단순하다.
2. **`~/.m2`.** emptyDir 이면 Job 마다 Boot 4 BOM, OpenRewrite 레시피, 플러그인을 Nexus 에서 다시 받는다(수백 MB, 몇 분). PVC(`sbmig-m2`,
   RWO 면 순차 실행에 충분) 로 바꾸거나, 이미지에 `~/.m2` 를 미리 채운 레이어(후속).
3. **Agent 의 역할.** 가이드의 Agent 는 "Job 을 만들고 보는 쪽" 이다. 지금 `sbmig serve` 는 이관을 자기 프로세스 안에서 돌리고 Job 을
   만들지 않는다. 당장은 **CLI 모델** — 운영자(또는 목록을 도는 스크립트)가 `submit-job.sh` 로 프로젝트마다 Job 을 만들고 결과는 저장소에서
   받는다. 웹 UI 의 "Job 디스패치 모드"(업로드 → Job 생성 → 로그 스트리밍 → 결과 회수)는 필요할 때 새 증분(2~3 일).

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

## 5. 팀원 초안(2026-11-04)에서 바꾼 것

* 웹 토큰 환경 변수는 `WEB_API_TOKEN` 이 아니라 **`SBMIG_WEB_TOKEN`** 이다.
* `kb.dir` 기본값은 이미지 안 `/app/kb-v2` 라 파드가 바뀌면 KB 가 사라진다 → `company.yml` 에서 `/data/kb-v2`(웹 UI) / Job 모델은 KB 저장소.
* 의존성 목록을 Dockerfile 에 다시 적지 않는다(`pip-deps.py` 가 `pyproject.toml` 을 읽는다). `python-multipart` 는 필요 없다 — 업로드는
  zip 본문을 그대로 스트리밍한다.
* `JAVA_HOME` 은 아키텍처별 경로 대신 `/opt/java21` 심볼릭 링크(팀원 아이디어, 인용부호만 고침).
* 에이전트 + 빌더 파드를 `kubectl exec` 로 잇는 레이아웃은 인프라 가이드의 Job 모델이 나오면서 뺐다(이력: `docs/INTERFACES.md` v2.1z-r/s).
  sbmig 에 남아 있는 `build.setup_command` / `teardown_command` 훅은 그 레이아웃용이었고 Job 모델에서는 비워 둔다.
