# sbmig 컨테이너 / 쿠버네티스 배포

이 폴더는 sbmig 을 이미지로 만들고 쿠버네티스에 올리는 파일들이다. 운영 방식은 PC 와 같다: **에이전트 하나가 프로젝트를
한 번에 하나씩** 이관하고, 상태(실행 디렉터리 `out/`, 파일 KB `kb-v2/`, LLM 기록)는 전부 볼륨에 둔다.

| 파일 | 역할 |
|---|---|
| `docker/Dockerfile` | **권장 — 이미지 하나**: Python 3.11 + JDK 21 + Maven + git + 앱 코드. 에이전트가 PC 에서처럼 mvn 을 직접 돌린다 |
| `docker/pip-deps.py` | 이미지가 설치할 의존성을 `pyproject.toml` 에서 읽는다(목록을 두 곳에 두지 않기 위해) |
| `docker/Dockerfile.builder`, `docker/mvn-k8s` | 선택 — 분리 레이아웃(§3)의 빌더 이미지와 `kubectl exec` 래퍼 |
| `k8s/10-storage.yaml` | PVC 둘: `/data`(업로드·실행·KB·LLM 기록), `~/.m2`(Maven 로컬 저장소) |
| `k8s/20-configmap.yaml` | 회사 설정 `company.yml`(UI 가 실행마다 고르는 config) + Nexus `settings.xml` |
| `k8s/30-secret.example.yaml` | `.env` 에 해당하는 비밀값 — LLM 게이트웨이, 앱 토큰, 웹 토큰. **복사해 채우고 커밋하지 않는다**(`30-secret.yaml` 은 git-ignore) |
| `k8s/40-deployment.yaml`, `k8s/50-service.yaml` | 에이전트 Deployment(replica 1, Recreate) + Service |
| `k8s/60-job-run.example.yaml` | 웹 UI 없이 zip 하나를 `sbmig run` 으로 돌리는 Job 예시 |
| `k8s/70-split-sidecar.example.yaml`, `k8s/71-split-rbac.yaml` | 선택 — 분리 레이아웃(사이드카 빌더) 과 그 RBAC |

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

## 3. "공통 이미지 + 에이전트 파드 + 요청마다 생기는 빌드 파드" 안에 대하여

가능하다. 단 비용이 있고, sbmig 의 동작 방식과 맞춰 보면 **이미지 하나가 맞는 기본값**이다.

* sbmig 은 이관 한 번에 mvn 을 **수십 번** 돈다(OpenRewrite, effective-pom 캡처, 첫 빌드, 수정 시도마다 재빌드). mvn 이 다른
  파드에 있으면 (1) 두 파드가 같은 경로에 같은 볼륨을 마운트해야 하고 — 노드가 다르면 ReadWriteMany(NFS / CephFS) 가 필요하다,
  (2) 호출마다 `kubectl exec` 왕복, (3) sbmig 의 타임아웃 / 프로세스 트리 kill 이 로컬 `kubectl` 에만 닿아 원격 mvn 이 남을 수 있다,
  (4) `~/.m2` 가 빌더 쪽에 있어야 매 실행 Nexus 전체 다운로드를 피한다, (5) 파드 생성 / 삭제 / 실패 정리 코드가 에이전트에 들어가야
  한다(지금 sbmig 에는 없다).
* 분리의 진짜 이유는 하나다: **임의의 프로젝트 빌드(= 임의의 Maven 플러그인 실행)를 LLM 앱 토큰을 가진 컨테이너에서 돌리지 않는다**는
  보안 분리. 그게 요구 사항이면 §3-a 사이드카가 비용이 가장 작다.

**3-a. 사이드카(권장되는 분리 방식)** — `k8s/70-split-sidecar.example.yaml`. 같은 파드에 `agent`(Python) 와 `builder`(JDK + Maven)
두 컨테이너, 둘이 `/data` 를 함께 마운트. 에이전트는 `build.mvn: /app/deploy/docker/mvn-k8s` 로 mvn 을 부르고, 래퍼가
`kubectl exec -c builder -- bash -c "cd <같은 경로> && mvn …"` 로 넘긴다. 같은 노드라 RWX 저장소가 필요 없고, 빌더는 에이전트와
함께 살고 죽으니 요청마다 만들고 지울 것이 없다. 필요한 것: `71-split-rbac.yaml`(자기 파드에 `pods/exec`), `kubectl` 이 든 에이전트
이미지(단일 이미지에 `kubernetes-client` 를 더하거나 팀원의 base 이미지), 그리고 sbmig 코드 변경 없음.

**3-b. 요청마다 빌드 파드(팀원 초안)** — 위 비용 전부 + 파드 라이프사이클 코드. 지금 sbmig 은 파드를 만들지 않으므로, 이 방식은
운영자가(또는 별도 컨트롤러가) 빌더 파드를 띄우고 `SBMIG_BUILDER_POD` 를 에이전트에 알려 주는 형태로만 가능하다. 500 개를 순차로
도는 용도에서 얻는 것이 없어 권하지 않는다.

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
