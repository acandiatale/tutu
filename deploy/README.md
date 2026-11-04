# sbmig 컨테이너 / 쿠버네티스 배포

이 폴더는 sbmig 을 이미지로 만들고 쿠버네티스에 올리는 파일들이다. 운영 방식은 PC 와 같다: **에이전트 하나가 프로젝트를
한 번에 하나씩** 이관하고, 상태(실행 디렉터리 `out/`, 파일 KB `kb-v2/`, LLM 기록)는 전부 볼륨에 둔다.

| 파일 | 역할 |
|---|---|
| `docker/Dockerfile` | **권장 — 이미지 하나**: Python 3.11 + JDK 21 + Maven + git + 앱 코드. 에이전트가 PC 에서처럼 mvn 을 직접 돌린다 |
| `docker/pip-deps.py` | 이미지가 설치할 의존성을 `pyproject.toml` 에서 읽는다(목록을 두 곳에 두지 않기 위해) |
| `docker/Dockerfile.builder`, `docker/mvn-k8s` | 선택 — 분리 레이아웃(§3)의 빌더 이미지와 `kubectl exec` 래퍼(원격 kill 전달, 종료 코드 보존) |
| `k8s/10-storage.yaml` | PVC 둘: `/data`(업로드·실행·KB·LLM 기록), `~/.m2`(Maven 로컬 저장소) |
| `k8s/20-configmap.yaml` | 회사 설정 `company.yml`(UI 가 실행마다 고르는 config) + Nexus `settings.xml` |
| `k8s/30-secret.example.yaml` | `.env` 에 해당하는 비밀값 — LLM 게이트웨이, 앱 토큰, 웹 토큰. **복사해 채우고 커밋하지 않는다**(`30-secret.yaml` 은 git-ignore) |
| `k8s/40-deployment.yaml`, `k8s/50-service.yaml` | 에이전트 Deployment(replica 1, Recreate) + Service |
| `k8s/60-job-run.example.yaml` | 웹 UI 없이 zip 하나를 `sbmig run` 으로 돌리는 Job 예시 |
| `k8s/80-build-job.template.yaml`, `k8s/submit-job.sh`, `docker/run-job.sh` | **인프라 가이드(2026-11-05) 모델**: 요청별 Job 템플릿(generateName, nodeSelector, emptyDir), 에이전트 쪽 제출 스크립트, Job 파드의 진입 스크립트(소스 → KB → sbmig run → push) — §6 |
| `docker/Dockerfile.agent` | 선택 — 분리 레이아웃의 에이전트 이미지(Python + git + kubectl, JDK 없음) |
| `k8s/70-split-sidecar.example.yaml`, `k8s/71-split-rbac.yaml`, `k8s/72-split-builder-pod.template.yaml` | 선택 — 분리 레이아웃: 사이드카 빌더, RBAC, 실행마다 생기는 빌더 파드 템플릿 |

## 1. 이미지 만들기

sbmig 체크아웃 루트에서(회사 레포에서는 `src/migration-agent/` 디렉터리에서):

```bash
docker build -f deploy/docker/Dockerfile -t <registry>/sbmig:<tag> .
# 사내 미러:  --build-arg PIP_INDEX_URL=https://nexus.company/repository/pypi/simple --build-arg PIP_TRUSTED_HOST=nexus.company
docker run --rm <registry>/sbmig:<tag> python -m sbmig.cli --help      # 확인
docker push <registry>/sbmig:<tag>
```

이미지 안에서 코드는 `/app` 에 있고 `PYTHONPATH=/app` 으로 돈다(`pip install` 로 site-packages 에 넣으면 `config/` 와
`knowledge/` 를 못 찾는다 — `config.py` 가 자기 위치에서 레포 루트를 잡는다). apt 미러가 필요하면 `RUN apt-get` 앞에
`/etc/apt/sources.list.d/` 교체 줄을 넣는다.

## 2. 올리기

```bash
kubectl apply -f deploy/k8s/10-storage.yaml
kubectl apply -f deploy/k8s/20-configmap.yaml      # <…> 자리를 회사 값으로 채운 뒤
cp deploy/k8s/30-secret.example.yaml deploy/k8s/30-secret.yaml && $EDITOR deploy/k8s/30-secret.yaml
kubectl apply -f deploy/k8s/30-secret.yaml
kubectl apply -f deploy/k8s/40-deployment.yaml -f deploy/k8s/50-service.yaml
kubectl rollout status deploy/sbmig
kubectl exec deploy/sbmig -- python -m sbmig.cli llm-probe             # token exchange: ok … HTTP 200
```

브라우저는 Service 를 통해 `http://<host>:8765/?token=<SBMIG_WEB_TOKEN>` 로 한 번 열면 7 일 쿠키가 남는다.
`--host 0.0.0.0` 이라 토큰이 없으면 서버가 뜨지 않는다(결과 zip 에 회사 소스가 들어가서 막아 둔 것).

설정의 역할 분담:

* **Secret `sbmig-llm`** = PC 의 `.env`: `LLM_GATEWAY=agent`, `LLM_BASE_URL`(경로의 `{asset_id}` 는 `LLM_ASSET_ID` 로 채워짐),
  `LLM_ASSET_ID`, `LLM_TOKEN_URL`, `LLM_APP_TOKEN`, `LLM_MODEL=build_error_fixer`, `LLM_ESCALATION_MODEL=escalation_fixer`,
  `LLM_RECORD_DIR=/data/llm-records`, `SBMIG_WEB_TOKEN`.
* **ConfigMap `sbmig-config`**: `company.yml` 은 `/app/config/company.yml` 로 들어가 UI 의 config 목록에 나온다. 여기에 컨테이너
  경로를 적는다 — `kb.dir: /data/kb-v2`(기본 `kb-v2` 는 이미지 안 경로라 재시작 때 사라진다), `build.settings_file:
  /etc/sbmig/settings.xml`, `build.repository_url`, `llm.record_dir`. `settings.xml` 은 `/etc/sbmig/settings.xml` 로.
* **리소스**: Maven / OpenRewrite 는 `build.jvm_args: ["-Xmx4g"]` 로 돈다 → 메모리 limit 은 그 위에 Python 몫을 더해 6~8 GiB.
  CPU 2~4. 업로드 상한은 `SBMIG_MAX_UPLOAD_MB`(기본 512, 매니페스트는 1024).
* **프로브**: 인증 없는 health 경로가 없다(토큰 가드가 403 을 낸다) → `tcpSocket` 프로브를 쓴다.

## 3. 빌드만 파드로 분리하기 — 이상적인 모양

팀이 분리로 간다면 기준은 하나다: **mvn 이 다른 컨테이너에서 돌아도 sbmig 는 모르게**. sbmig 은 `build.mvn` 에 적힌 명령을
모듈 디렉터리를 cwd 로 삼아 `[mvn, 인자…]` 로 부르고, 종료 코드와 출력만 본다. 그래서 분리는 **코드 변경 없이 설정 두 줄과 래퍼
하나**로 된다.

| 레이어 | 무엇 | 파일 |
|---|---|---|
| 에이전트 이미지 | Python + git + **kubectl + envsubst**, JDK / Maven 없음 | `docker/Dockerfile.agent` |
| 빌더 이미지 | JDK 21 + Maven + git, Python / 앱 코드 없음, `sleep infinity` | `docker/Dockerfile.builder` |
| 래퍼 | `build.mvn: /app/deploy/docker/mvn-k8s` — `kubectl exec` 로 같은 cwd 에서 mvn 실행. sbmig 의 타임아웃 / kill(SIGTERM) 을 받아 **원격 mvn 에도 전달**하고(pid 파일을 `/data/.sbmig-mvn/` 에), Maven 의 종료 코드를 그대로 돌려준다 | `docker/mvn-k8s` |
| 공유 볼륨 | 두 컨테이너가 `/data` 를 **같은 경로**에 마운트. `~/.m2` 는 빌더 쪽 PVC | `k8s/10-storage.yaml` |
| settings.xml | 빌더(mvn `-s`)와 에이전트(jakarta 빌드 탐색이 미러 주소를 읽는다) **둘 다**에 마운트 | `k8s/20-configmap.yaml` |
| 권한 | 자기 파드 / 빌더 파드에 `pods/exec`, 실행마다 파드를 만들면 `pods` create / delete | `k8s/71-split-rbac.yaml` |

파드 단위는 둘 중 하나다. **빌드마다 파드 하나는 권하지 않는다** — 이관 한 번에 mvn 이 수십 번 돌고 파드 기동이 매번 10~30 초,
JVM 워밍업과 `~/.m2` 캐시도 매번 잃는다.

**3-a. 사이드카(프로젝트 단위 분리를 가장 싸게)** — `k8s/70-split-sidecar.example.yaml`. 같은 파드에 `agent` + `builder`. 같은
노드라 RWX 저장소가 필요 없고, 빌더는 에이전트와 함께 살고 죽는다. 에이전트 환경 `SBMIG_BUILDER_CONTAINER=builder`, 회사 yml
`build.mvn: /app/deploy/docker/mvn-k8s`. 끝.

**3-b. 실행(프로젝트)마다 빌더 파드** — `k8s/72-split-builder-pod.template.yaml`. sbmig 에 실행 전후 훅이 있다(v2.1z-s):

```yaml
build:
  mvn: /app/deploy/docker/mvn-k8s
  setup_command: ["sh", "-c", "envsubst < /etc/sbmig/builder-pod.yaml | kubectl apply -f - && kubectl wait --for=condition=Ready pod/sbmig-builder-$SBMIG_RUN_ID --timeout=300s"]
  teardown_command: ["kubectl", "delete", "pod", "sbmig-builder-$SBMIG_RUN_ID", "--ignore-not-found", "--wait=false"]
```

`setup_command` 는 첫 단계 전에 한 번(실패하면 실행 중단 — 빌더 없이는 아무것도 빌드할 수 없다), `teardown_command` 는 보고서 뒤에
한 번(실패는 경고). 환경에 `SBMIG_RUN_ID` / `SBMIG_RUN_DIR` / `SBMIG_PROJECT_DIR` 가 들어간다. 에이전트 환경에 `SBMIG_BUILDER_PER_RUN=1`
을 두면 래퍼가 cwd(`/data/out/<run id>/…`)에서 run id 를 읽어 `sbmig-builder-<run id>` 파드를 찾는다. 템플릿은 ConfigMap 으로
`/etc/sbmig/builder-pod.yaml` 에 마운트한다. 빌더 파드가 다른 노드에 뜰 수 있으니 이 변형에서는 `/data` PVC 가 **ReadWriteMany**
여야 한다(아니면 nodeSelector 로 한 노드에 묶는다).

**Nexus 주소.** 두 곳에 들어간다. (1) `settings.xml` 의 `<mirror><url>` — mvn 이 모든 저장소를 그쪽으로 돌린다(`mirrorOf *`); (2)
`company.yml` 의 `build.repository_url` — sbmig 가 메타데이터 / pom 을 직접 읽어 릴리스 목록과 jakarta 빌드를 찾는 주소. 둘 다
`k8s/20-configmap.yaml` 의 `<https://nexus.company/…>` 자리다. 인증이 필요한 Nexus 면 `settings.xml` 의 `<servers>` 는 Secret 으로
빼서 마운트한다(ConfigMap 에 비밀값을 두지 않는다).

## 4. uvicorn 질문

`sbmig serve` 가 **곧 uvicorn** 이다(안에서 `uvicorn.run` 을 부른다). `uvicorn sbmig.web.app:app` 으로 띄우는 것과 같은 앱이고,
그쪽은 `SBMIG_WEB_OUT` / `SBMIG_WEB_REPO_ROOT` / `SBMIG_WEB_TOKEN` 환경 변수와 `--timeout-graceful-shutdown` 을 직접 줘야
한다. 워커는 **하나**여야 한다 — 실행 큐와 "한 번에 한 이관" 상태가 프로세스 안에 있어서 `--workers 2` 나 gunicorn 다중 워커는
큐를 갈라 놓는다. 그래서 Dockerfile 의 CMD 는 `sbmig serve` 다.

## 5. 팀원 초안에서 바꾼 것

* 토큰 환경 변수 이름은 `WEB_API_TOKEN` 이 아니라 **`SBMIG_WEB_TOKEN`** 이다.
* `kb.dir` 기본값(`kb-v2`)은 이미지 안 `/app/kb-v2` 라 파드가 바뀌면 KB 가 사라진다 → `company.yml` 에서 `/data/kb-v2` 로.
  같은 이유로 `LLM_RECORD_DIR=/data/llm-records`, `~/.m2` 는 PVC.
* 의존성 목록을 Dockerfile 에 다시 적지 않는다(`pip-deps.py` 가 `pyproject.toml` 을 읽는다). `python-multipart` 는 필요 없다 —
  업로드는 zip 본문을 그대로 스트리밍하고 multipart 를 쓰지 않는다.
* `JAVA_HOME` 은 Debian 의 고정 경로 `/usr/lib/jvm/java-21-openjdk-amd64` 로(심볼릭 링크 계산 불필요). `JAVA21_HOME` 도 같이.
* `company.yml` 과 `settings.xml` 을 ConfigMap 으로, 비밀값은 Secret 으로 분리했고 프로브 / 리소스 / Recreate / fsGroup 을 더했다.

## 6. 인프라 가이드(2026-11-05) — "요청별 빌드 Job" 모델과 sbmig 맞춤

인프라팀 가이드는 §3 과 다른 모델이다: **요청마다 Job 하나**, 그 파드가 "소스 처리 → 빌드 → 분석 → 결과 저장 → 저장소 커밋" 을
전부 하고 끝난다. 작업 공간은 `/workspace` emptyDir(파드와 함께 사라짐). sbmig 에 맞추면 이렇게 된다.

| 가이드의 말 | sbmig 에서 |
|---|---|
| "빌드 이미지: JDK, 빌드 도구, 분석 도구, git" | **단일 이미지** `docker/Dockerfile`(Python + JDK 21 + Maven + git). `build-runner` 자리에 이 이미지 |
| "소스 처리·빌드·분석·결과 저장·커밋 뒤 종료" | `docker/run-job.sh`: 소스 clone(또는 zip) → KB clone → `sbmig run` → 결과 push → 종료 코드 |
| "저장소 커밋" | 실행 디렉터리의 `project/` 는 이미 git 저장소(브랜치 `sbmig/<run id>`) → 소스 저장소에 그 브랜치를 push |
| "결과 저장" | `report.md` / `errors.md` / `sbmig.log` / `journal.jsonl` 을 결과 저장소(`SBMIG_RESULT_GIT_URL`, 브랜치 = run id)에 push, 또는 `RESULT_PUSH_CMD` |
| `/workspace` emptyDir | sbmig 의 `--out /workspace/out`; 파드가 죽기 전에 push 되는 것만 남는다 |
| Agent: `kubectl create -f /templates/build-job.yaml` | `k8s/80-build-job.template.yaml` + `k8s/submit-job.sh`(can-i → dry-run=server → create → logs -f → 결과) |
| `/etc/kube/config` 는 Agent 에만 | 그대로. 빌드 파드는 API 를 부르지 않는다(sbmig 도 안 부른다) |

**꼭 짚어야 할 세 가지.**

1. **KB 영속성.** sbmig 의 핵심 자산은 파일 KB(`kb-v2/`: 케이스별 수정 스크립트와 성적) 이고 다음 프로젝트가 그걸 재사용해야 한다.
   emptyDir 에 두면 Job 마다 사라진다. 해법: **KB 를 git 저장소로**(`SBMIG_KB_GIT_URL`) — Job 시작에 clone, 끝에 commit + push.
   파일 KB 라 그대로 된다. 조건은 **Job 을 한 번에 하나만** 돌리는 것(동시에 두 개가 push 하면 충돌; 어차피 순차 이관이 운영 모델).
   인프라가 PVC 를 허용하면 `kb` 볼륨을 PVC 로 바꾸는 쪽이 더 단순하다 — 내일 물어볼 것.
2. **`~/.m2`.** emptyDir 이면 Job 마다 Boot 4 BOM, OpenRewrite 레시피, 플러그인을 Nexus 에서 다시 받는다(수백 MB, 수 분). 해법 둘:
   `m2` 볼륨을 PVC 로(ReadWriteOnce 면 순차 실행에 충분), 또는 이미지 빌드 때 `~/.m2` 를 미리 채운 레이어(후속).
3. **Agent 의 역할.** 가이드의 Agent 는 "kubectl 로 Job 을 만들고 로그와 결과를 보는 쪽" 이다. 지금 `sbmig serve` 는 이관을 **자기
   프로세스 안에서** 돌리고 Job 을 만들지 않는다. 그래서 내일 결정할 것: (A) 당장은 **CLI 모델** — 운영자(또는 간단한 스크립트)가
   `submit-job.sh` 로 프로젝트마다 Job 을 만들고 결과는 저장소에서 받는다(500개 순차 = 목록을 돌리는 스크립트 한 장); (B) `sbmig serve`
   에 "Job 디스패치 모드"(업로드 → Job 생성 → 로그 스트리밍 → 결과 회수)를 새 증분으로 만든다(2~3일). A 로 시작해 B 는 필요할 때.

**Job 템플릿에서 바꿀 자리(가이드 2절 그대로).** `namespace`, `nodeSelector` 라벨, `image`, `imagePullSecrets`; 그리고 sbmig 용으로
`activeDeadlineSeconds`(이관 한 번이 1~3 시간 — 4 시간 권장), `ttlSecondsAfterFinished`(로그 보존), emptyDir `sizeLimit`(프로젝트 +
빌드 산출물; `~/.m2` 는 별도 10 Gi), 환경 변수 `SBMIG_SOURCE_GIT_URL` / `_REF`(또는 `SBMIG_SOURCE_ZIP_URL`), `SBMIG_KB_GIT_URL`,
`SBMIG_RESULT_GIT_URL`, Secret `sbmig-llm`(LLM_*), `sbmig-git`(`GIT_TOKEN`, 선택 `GIT_USER`). 토큰은 환경에서만 읽고 URL 에 메모리
안에서만 붙이며 로그·push 파일에 남지 않는다.

**실행 전 확인(가이드 5절 + sbmig).** config 계정 권한: `jobs` create/get/delete, `pods` get/list, `pods/log` get. 빌드 파드에서
Nexus(Maven + `build.repository_url`), 소스 저장소(clone + push), KB·결과 저장소(push), LLM 게이트웨이(토큰 URL + 호출 URL) 가 열려
있어야 한다. `kubectl … create --dry-run=server` 가 nodeSelector 라벨 불일치는 잡지 못한다 — Pending 이면 라벨을 본다.

**종료 코드.** `run-job.sh` 는 보고서가 나왔으면 0(sbmig exit 0 또는 2 — 빌드 실패도 "결과" 다), 결과를 밖으로 못 보냈으면 4,
sbmig 가 중단(exit 3: preflight / ADF / 내부 오류)이면 3. `SBMIG_FAIL_JOB_ON_BUILD_FAIL=1` 이면 빌드 실패도 Job 실패로 본다.

