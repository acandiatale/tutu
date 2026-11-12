# sbmig 컨테이너 / 쿠버네티스 배포

이 폴더는 sbmig 을 이미지로 만들고 쿠버네티스에 올리는 파일들이다. 이관 한 건이 도는 방식은 PC 와 같고(Job 모드에서는 동시에 `SBMIG_JOB_MAX_PARALLEL` 건까지 — §3 "동시에 여러 건"),
상태(실행 디렉터리 `out/`, 파일 KB `kb-v2/`, LLM 기록)는 이미지 밖에 둔다.

| 파일 | 역할 |
|---|---|
| `docker/Dockerfile` | **이미지 하나**: Python 3.11 + JDK 21 + Maven + git + 앱 코드. 웹 UI 에이전트와 요청별 Job 모두 이 이미지를 쓴다 |
| `docker/pip-deps.py` | 이미지가 설치할 의존성을 `pyproject.toml` 에서 읽는다(목록을 두 곳에 두지 않기 위해) |
| `docker/run-job.sh` | 요청별 Job 파드의 진입 스크립트: 소스 → KB → `sbmig run` → 결과 push → 종료 코드 |
| `k8s/10-storage.yaml` | PVC 둘: `/data`(웹 UI 에이전트의 업로드·실행·KB·LLM 기록), `~/.m2`(Maven 로컬 저장소) |
| `k8s/20-configmap.yaml` | Nexus `settings.xml`(`sbmig-config`) + 비밀 아닌 환경 변수(`sbmig-env`). 회사 설정 `artifacts.yml` 은 이미지에 굽는다(§1) |
| `k8s/30-secret.example.yaml` | 비밀값 예시 — `sbmig-llm`(= PC 의 `.env` + 웹 토큰), `sbmig-git`(저장소 토큰). **복사해 채우고 커밋하지 않는다**(`30-secret.yaml` 은 git-ignore) |
| `k8s/40-deployment.yaml`, `k8s/50-service.yaml` | 웹 UI 에이전트(`sbmig serve`) Deployment(replica 1, Recreate) + Service — Job 모델만 쓰면 불필요 |
| `k8s/45-web-rbac.yaml` | 웹 파드가 Job 을 만들고 지켜보는 최소 권한(ServiceAccount + Role + RoleBinding). kubeconfig Secret 이 없는 클러스터용 — 회사는 인프라팀의 kubeconfig 로 권한을 받으므로 쓰지 않는다 |
| `k8s/80-build-job.template.yaml`, `k8s/submit-job.sh` | **인프라 가이드(2026-11-05) 모델**: 요청별 Job 템플릿 + 템플릿을 손으로 돌려 보는 제출 스크립트(운영 방식이 아니라 검증 도구) |

**운영 방식은 하나다**: 에이전트가 웹 화면을 띄우고, 이관 대상 프로젝트의 담당자가 그 화면에 프로젝트 zip 을 올리면 에이전트가
이관을 돌린다. 담당자가 sbmig 명령을 직접 치는 일은 없다.
**회사 구성(§3)** 은 인프라 가이드대로 웹 파드가 업로드마다 빌드 Job 을 만드는 것이다(`sbmig serve` 의 Job 디스패치 모드). 매니페스트는
**이 모드가 켜진 채로** 나간다(`sbmig-env` 의 `SBMIG_WEB_MODE: "k8s-job"`, 2026-11-10) — §2 의 apply 전에 §3 "켜는 법" 2·3·4 를 한다.
그 줄을 주석 처리하면 웹 파드가 자기 프로세스 안에서 돌린다(§2 만으로 뜬다). 화면·이미지·설정 파일은 둘이 같다.

## 1. 이미지 만들기

sbmig 체크아웃 루트에서(회사 레포에서는 `src/migration-agent/` 디렉터리에서), **amd64** 로:

```bash
docker build -f deploy/docker/Dockerfile -t <registry>/sbmig:<tag> .
docker run --rm <registry>/sbmig:<tag> python -m sbmig.cli --help      # 확인
docker push <registry>/sbmig:<tag>
```

Windows PC(PowerShell, Docker Desktop)에서도 같은 세 줄이다(`-f deploy\docker\Dockerfile`).

**회사 레포 구조**(2026-11-09: 코드는 `src/migration-agent/`, `deploy/` 는 레포 루트)에서는 **루트에서** 두 인자를 주고 빌드한다. `.dockerignore` 는
컨텍스트 루트의 것만 읽히므로 견본 `deploy/docker/root.dockerignore.example` 을 루트에 `.dockerignore` 로 먼저 복사한다(안 하면 `.venv`·`out/` 이 이미지에
딸려 들어간다). 이미지 안 배치는 우리 레포에서 빌드한 것과 똑같다(`/app` = sbmig 체크아웃, `/app/deploy`).
```powershell
Copy-Item deploy\docker\root.dockerignore.example .dockerignore
docker build --build-arg APP_DIR=src/migration-agent --build-arg DEPLOY_DIR=deploy --build-arg REQUIRE_CONFIG=artifacts.yml -f deploy\docker\Dockerfile -t sbmig:<tag> .
```

**회사 설정 `artifacts.yml` 은 이미지에 굽는다**(2026-11-10 결정). PC 에서 시험한 `config/artifacts.yml` 이 `COPY` 로 `/app/config/artifacts.yml` 이 되고,
매니페스트는 그 파일을 이름으로 가리킨다(`SBMIG_WEB_CONFIGS=artifacts.yml`). ConfigMap 에 같은 내용을 다시 적지 않는다.

* `config/artifacts.yml` 은 git 이 무시하는 파일이라 새로 받은 체크아웃에는 없다. `--build-arg REQUIRE_CONFIG=artifacts.yml` 을 붙이면 없을 때 빌드가
  `ERROR: config/artifacts.yml is missing from the build context` 로 멈춘다. 안 붙이면 회사 설정 없는 이미지가 조용히 만들어진다.
* PC 경로는 고치지 않아도 된다. 아래 일곱 키는 매니페스트의 env 가 컨테이너 값으로 덮어쓴다(§2 "컨테이너 경로"): `kb.dir`, `build.settings_file`,
  `build.java_home`, `build.mvn`, `build.local_repo`, `script.python`, `llm.record_dir`. 그 밖에 PC 에만 맞는 값이 있으면 지우고 굽는다.
  확인: `Select-String -Path src\migration-agent\config\artifacts.yml -Pattern '[A-Za-z]:[\\/]'`
* 파일에 키·토큰·비밀번호를 적지 않는다(이미지를 받는 누구나 읽는다). 비밀은 Secret `sbmig-llm` 으로만 온다.
* 내용을 바꾸면 이미지를 다시 만들고 새 태그를 `40-deployment.yaml` 과 `81-web-job.template.yaml` 에 같이 넣는다. 숫자·스위치만 바꿀 때는
  이미지 대신 `sbmig-env` 의 `SBMIG_<섹션>__<키>` 를 쓴다(§2 env 목록).
* 이미지를 다시 만들지 않고 설정 파일을 바꾸고 싶으면 `20-configmap.yaml` 맨 아래 "ConfigMap 으로 줄 때" 의 주석을 푼다(구운 파일을 마운트가 가린다).

`src\migration-agent\pyproject.toml` 이 있어야 한다(의존성 목록). 없으면 우리 레포의 것을 그 자리에 복사한다. 빌드가 밖으로 못 나가는 망이면 Dockerfile 머리의 빌드 인자를
준다 — 전부 선택이고 비우면 공개 저장소를 쓴다:

| 인자 | 예 | 뜻 |
|---|---|---|
| `PYTHON_IMAGE` | `<harbor>/<proxy project>/library/python:3.11-slim-trixie` | 베이스 이미지를 사내 Harbor 의 Docker Hub 프록시에서 |
| `APT_MIRROR` | `https://nexus.company/repository` | `deb.debian.org` 대신 쓸 주소(뒤에 `/debian`, `/debian-security` 가 붙는다 — JDK 21·Maven·git 을 apt 로 받는다) |
| `PIP_INDEX_URL` / `PIP_TRUSTED_HOST` | `https://nexus.company/repository/pypi/simple` / `nexus.company` | PyPI 대신 사내 미러. `PIP_TRUSTED_HOST` 는 인증서를 검증하지 않을 호스트 — 여러 개면 빈칸으로 나눈다 |
| `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` | 회사 프록시 | 프록시로만 나가는 망 — 빌드 중에만 쓰인다 |

예: `docker build --build-arg APT_MIRROR=https://nexus.company/repository --build-arg PIP_INDEX_URL=… -f deploy\docker\Dockerfile -t sbmig:<tag> .`
**PC 의 pip 는 받는데 빌드의 `pip install` 만 `SSLError` 로 멈출 때**: 회사망이 HTTPS 를 중간에서 검사하고, 그 검사용 인증서가 PC 의 인증서
저장소에는 있지만 컨테이너 안에는 없는 것이다. 사내 PyPI 미러가 있으면 위 두 인자로 그쪽을 쓰고, 없으면 공개 호스트 둘을 믿게 한다(빌드 중의 pip 에만 적용):
`docker build --build-arg "PIP_TRUSTED_HOST=pypi.org files.pythonhosted.org" …`. PC 가 쓰는 주소는 `pip config list` 로 본다.

셸 스크립트(`run-job.sh`)는 `.gitattributes` 가 LF 로 고정하고 Dockerfile 이 한 번 더 LF 로 되돌리므로 Windows 체크아웃에서 빌드해도 된다.

**회사(2026-11-09)**: 인프라팀이 자기 레지스트리에 push 해 준다. PC 에서는 `deploy/docker/save-image.sh <tag>` 로 amd64 tar 를 만들어 넘기면 인프라팀이
`docker load -i` → `docker tag sbmig:<tag> <registry>/sbmig:<tag>` → `docker push` 한다(Dockerfile 을 넘겨 그쪽에서 빌드해도 같다). push 된 이미지의
**전체 이름**을 받아 `k8s/40-deployment.yaml` 과 `k8s/81-web-job.template.yaml` 의 `image:` 두 줄(`CHANGE_ME_REGISTRY/sbmig:CHANGE_ME_TAG` 자리)에 글자
그대로, 같은 값으로 넣는다(`imagePullPolicy: IfNotPresent`). 레지스트리가 pull 에 인증을 요구하면 인프라팀이 준 Secret 이름으로 양쪽의 `imagePullSecrets`
주석을 푼다. 태그는 빌드마다 새로(`latest` 금지 — 노드에 남은 옛 이미지가 그대로 돈다). 첫 파드가 `ImagePullBackOff` 면 대개 노드가 그 레지스트리의
인증서를 신뢰하지 않는 것이다(인프라팀 몫).

### 1.1 레지스트리 없이 노드에 직접 적재해야 할 때 — `docker save` tar (대안; 2026-11-06 에 레지스트리가 없다고 들었다가 11-09 에 push 로 정리됨)

레지스트리를 못 쓰는 클러스터라면 이미지를 tar 로 넘기고 인프라팀이 **파드가 뜰 수 있는 모든 노드**에 적재한다. 그때는 매니페스트
(`40-deployment.yaml`, `80-` / `81-*.template.yaml`)의 이미지를 레지스트리 없는 이름 `sbmig:<tag>` 로, `imagePullPolicy` 를 `Never` 로 바꾼다
(노드에 적재된 것만 쓴다 — 어디로도 pull 하러 가지 않는다). 지금 매니페스트는 레지스트리 형태다.

```bash
deploy/docker/save-image.sh <tag>       # docker build --platform linux/amd64 … + docker save → out/images/sbmig-<tag>.tar (+ .sha256) + 적재 명령 출력
```

인프라팀이 노드마다, 컨테이너 런타임에 맞는 한 줄:

| 런타임 | 적재 | 확인 |
|---|---|---|
| containerd (kubeadm 등 대부분) | `sudo ctr -n k8s.io images import sbmig-<tag>.tar` | `sudo crictl images \| grep sbmig` |
| k3s / RKE2 | `sudo k3s ctr images import sbmig-<tag>.tar` (또는 tar 를 `/var/lib/rancher/k3s/agent/images/` 에 두고 agent 재시작) | `sudo k3s crictl images` |
| Docker (cri-dockerd) | `docker load -i sbmig-<tag>.tar` | `docker images sbmig` |

규칙 넷.
- **`-n k8s.io`** 없이 `ctr images import` 하면 다른 containerd namespace 에 들어가 kubelet 이 보지 못한다.
- **태그는 매번 새로, `latest` 금지.** `Never` 는 노드에 있는 그 태그를 그대로 쓰므로 같은 태그로 다시 적재해도 도는 파드는 옛 이미지다.
  새 tar = 새 태그 = yaml 의 `image:` 한 줄을 바꿔 `kubectl apply`(Deployment 가 새 파드로 바뀐다).
- **노드 전부, 아니면 고정.** 웹 파드와 Job 파드가 뜰 수 있는 노드마다 적재한다. 노드 하나에만 적재했다면 Deployment 와 Job 틀 양쪽의
  `nodeSelector: {kubernetes.io/hostname: <node>}` 주석을 푼다(RWO PVC 일 때 필요한 고정과 같은 줄이다).
- 이미지가 그 노드에 없으면 파드는 `ErrImageNeverPull` 로 바로 멈춘다 — `kubectl describe pod <pod>` 의 Events 에 그대로 나온다(레지스트리에 가 보려다
  네트워크 에러로 헤매지 않는다).

빌드는 **`--platform linux/amd64`** 로(노드가 amd64; Apple silicon / arm64 PC 에서 만든 arm64 이미지는 노드에서 `exec format error`). buildx 의
provenance / SBOM attestation 이 붙은 인덱스는 `ctr images import` 가 거부할 수 있어 스크립트가 `--provenance=false --sbom=false` 로 끈다.
tar 는 1 GB 안팎(JDK + Maven). gzip 해서 보내면 `docker load` 는 그대로 읽고, `ctr` 은 풀어서 적재한다.

코드는 `/app` 에 있고 `PYTHONPATH=/app` 으로 돈다. `pip install` 로 site-packages 에 넣으면 `config/` 와 `knowledge/` 를 못 찾는다
(`config.py` 가 자기 위치에서 레포 루트를 잡는다). apt 미러가 필요하면 `RUN apt-get` 앞에 `/etc/apt/sources.list.d/` 교체 줄을 넣는다.
`JAVA_HOME` 은 `/opt/java21` 심볼릭 링크라 amd64 / arm64 어디서 빌드해도 맞는다.

## 2. ① 웹 UI 에이전트로 올리기

```bash
# 매니페스트마다 namespace: agent 가 들어 있다(회사 값, 2026-11-09) — apply 에 -n 이 필요 없고, 그 뒤의 조회·exec 는 -n agent
kubectl apply -f deploy/k8s/10-storage.yaml
kubectl apply -f deploy/k8s/20-configmap.yaml      # <…> 자리를 회사 값으로 채운 뒤
cp deploy/k8s/30-secret.example.yaml deploy/k8s/30-secret.yaml && $EDITOR deploy/k8s/30-secret.yaml
kubectl apply -f deploy/k8s/30-secret.yaml
# Job 모드가 켜져 있다(sbmig-env 의 SBMIG_WEB_MODE) — 권한과 Job 틀이 먼저다(§3 "켜는 법" 2·3·4). 없으면 웹 파드가 시작 검증에서 멈춘다
kubectl -n agent describe secret agent-kubeconfig   # 회사: 인프라팀의 kubeconfig Secret, Data 에 config 가 보여야 한다(없는 클러스터는 §3 "켜는 법" 2 의 b)
kubectl -n agent create configmap sbmig-job-template --from-file=deploy/k8s/81-web-job.template.yaml --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f deploy/k8s/40-deployment.yaml -f deploy/k8s/50-service.yaml
kubectl -n agent rollout status deploy/sbmig
kubectl -n agent exec deploy/sbmig -- python -m sbmig.cli llm-probe             # token exchange: ok … HTTP 200
```

브라우저는 Service 를 통해 `http://<host>:8765/` 로 연다. 사내망 전용 페이지라(2026-11-05 결정) Deployment 가 `SBMIG_WEB_OPEN=1`
로 토큰 없이 열고, 서버는 시작 때 "open to the network WITHOUT a token" 경고를 한 줄 남긴다. 토큰을 두고 싶으면 그 env 를 지우고
Secret 의 `SBMIG_WEB_TOKEN` 을 채운다 — 그때는 `http://<host>:8765/?token=<값>` 으로 한 번 열면 7 일 쿠키가 남고, `--host 0.0.0.0` 에
둘 다 없으면 서버가 뜨지 않는다(결과 zip 에 회사 소스가 들어가서 막아 둔 것).

* **환경 변수는 두 곳**(2026-11-05, 인프라팀의 ConfigMap 목록 요청): ConfigMap **`sbmig-env`** 에 비밀 아닌 것 전부(웹 파드 동작,
  LLM 게이트웨이 주소·에셋 id·role 이름, 설정 덮어쓰기 `SBMIG_<섹션>__<키>`), Secret **`sbmig-llm`** 에 비밀만(`LLM_APP_TOKEN` 또는
  `LLM_API_KEY`, 필요하면 `NEXUS_USER` / `NEXUS_PASSWORD`, 선택 `SBMIG_WEB_TOKEN`). 목록은 아래 "env 목록" 절.
* **회사 설정**: 이미지 안 `/app/config/artifacts.yml`(§1 에서 구운 파일)이고, `sbmig-env` 의 `SBMIG_WEB_CONFIGS=artifacts.yml`
  (= `sbmig serve --config artifacts.yml`) 이 **모든 업로드를 이 설정으로 돌린다** — 담당자는 화면에서 설정을 고르지 않는다(고급 설정을
  펼쳐야 목록이 보인다). 그 이름의 파일이 이미지에 없으면 웹 파드는 경고 한 줄만 남기고 회사 설정 없이 돈다 — 그래서 빌드에 `REQUIRE_CONFIG` 를 붙인다.
* **ConfigMap `sbmig-config`**: `settings.xml` 하나(`/etc/sbmig/settings.xml`, Nexus 미러). 이미지의 Maven 3.9 도 `http://` 저장소를
  막는 기본 미러 `maven-default-http-blocker` 를 갖고 있다 — Nexus 주소가 `http://` 면 차단 미러의 `<mirrorOf>` 에 `,!<저장소 ID>` 를 붙여 그 저장소를 예외로 빼거나(회사 settings.xml 이 쓰는 방식 — 쓰는 저장소 ID 를 모두 적는다),
  Nexus 미러에 `<blocked>false</blocked>` 를 둔다(아니면 실행이 `attn=infra:http-blocked` 로 멈춘다).
  미러의 `<url>` 은 허용 목록이 아니라 **대신 물어볼 주소**다: `maven-default-http-blocker` 를 실제 주소(릴리스 저장소 등)로 재정의한
  settings 에서는 `mirrorOf` 가 덮는 `http://` 저장소 요청이 전부 그 주소 하나로 간다 — 막히지는 않지만, `<repositories>` 에 적은
  다른 저장소(스냅샷 · public)는 한 번도 묻지 않아 거기에만 있는 아티팩트를 "못 찾는다". 그 저장소들의 ID 를 `,!<저장소 ID>` 로 빼야
  제 주소로 간다(쉼표 뒤 공백 없이, Maven 은 맞는 미러 중 첫 번째만 쓴다). 확인: `mvn help:effective-settings`.
* **컨테이너 경로**: 구운 파일은 PC 에서 시험한 것이라 PC 경로가 들어 있을 수 있다. 매니페스트(40 / 81 / 80)의 `env` 가 그 키들을 덮어쓴다 —
  환경 변수는 어떤 yml 보다 뒤에 적용된다. `sbmig-env` 가 아니라 매니페스트에 두는 이유: 80 모델은 KB 폴더와 기록 폴더를 `run-job.sh` 가 정한다.

  | env (매니페스트에 고정) | 값 | 어디에 |
  |---|---|---|
  | `SBMIG_KB__DIR` | `/data/kb-v2` | 40 (81 은 `SBMIG_KB_DIR`, 80 은 `run-job.sh`) |
  | `SBMIG_BUILD__SETTINGS_FILE` | `/etc/sbmig/settings.xml` | 40 · 81 · 80 |
  | `SBMIG_BUILD__JAVA_HOME` | `/opt/java21` | 40 · 81 · 80 |
  | `SBMIG_BUILD__MVN` | `mvn` | 40 · 81 · 80 |
  | `SBMIG_BUILD__LOCAL_REPO` | `null` (= `~/.m2` 볼륨) | 40 · 81 · 80 |
  | `SBMIG_SCRIPT__PYTHON` | `null` (= 이미지의 python) | 40 · 81 · 80 |
  | `SBMIG_LLM__RECORD_DIR` | `${LLM_RECORD_DIR:-}` (= 그 변수가 있을 때만 기록) | 40 |
* **화면**(2026-11-05 새 디자인, 회사 블루): zip 을 끌어다 놓거나 파일 선택 → `마이그레이션 시작` → 단계 레일(접수 → 분석 → 계획 →
  단계들 → 마무리)과 진행 기록 / 로그 탭 → 끝나면 `보고서 받기(report.md)` / `변환된 프로젝트 받기(result.zip)` 두 버튼. 로그는 화면에
  마지막 1,500 줄만 그린다(전체는 `sbmig.log` 링크) — 몇 만 줄이 쌓여 브라우저가 느려지던 문제의 답. 사내망이라 웹 폰트·CDN 없음.
* **리소스**: Maven / OpenRewrite 는 `openrewrite.jvm_args: ["-Xmx4g"]` 로 돈다 → 메모리 limit 6~8 GiB, CPU 2~4. 업로드 상한
  `SBMIG_MAX_UPLOAD_MB`(기본 512, 매니페스트는 1024). **프로브**: 인증 없는 health 경로가 없어 `tcpSocket`.
* `sbmig serve` 가 **곧 uvicorn** 이다(안에서 `uvicorn.run`). 워커는 하나여야 한다 — 실행 큐가 프로세스 안에 있어 `--workers 2` 나
  gunicorn 은 큐를 갈라 놓는다.

### env 목록 — ConfigMap `sbmig-env` / Secret `sbmig-llm` 에 올리는 것

인프라팀에 넘기는 자급자족 문서는 **`deploy/ENV.md`** (같은 내용 + 적용·확인 명령). 아래는 요약.

설정의 원본은 세 층이다. `config/default.yml`(이미지 안, 모든 키의 기본값) → 이미지에 구운 `artifacts.yml`(회사 사실: 부모·필수·선택
아티팩트, 레이아웃, Nexus 주소) → **환경 변수**(가장 뒤에 적용, 이긴다). `SBMIG_<섹션>__<키>` 는 어떤 설정 키든 덮어쓰고(밑줄 두 개가
한 단계 아래, 값은 YAML 로 읽음), `artifacts.yml` 안에서는 `${VAR}` / `${VAR:-기본}` 으로 환경 변수를 참조할 수 있다. 그래서 **구조적인
사실은 `artifacts.yml`, 시험 운영 중 자주 손대는 숫자와 스위치는 env** 로 나눈다. 모르는 키 이름(철자·들여쓰기)은 시작 로그에
`config: … is not a known key` 로 뜬다.

`sbmig-env` 를 누가 읽나: 웹 파드(40)와 80 모델은 `envFrom` 으로 통째로, **Job 파드(81)는 틀에 적힌 키만**(틀에 `envFrom` 을 쓸 수 없다 —
키마다 `configMapKeyRef` 한 줄). 아래 표의 LLM 값과 `SBMIG_<섹션>__<키>` 는 전부 81 틀에 들어 있다. 표에 없는 새 키를 `sbmig-env` 에 더하면 81 틀에도
한 줄 더해야 Job 파드에 닿는다(`tests/deploy/test_manifests.py` 가 둘을 맞춰 본다). 바꾼 뒤에는 **웹 파드를 재시작한다**
(`kubectl -n agent rollout restart deploy/sbmig`). 웹 파드는 시작할 때 한 번 읽고 Job 파드는 만들어질 때 읽는데, Job 모드(§3)에서는 Worker 가 두 파드의
값을 대조해 다르면 그 Job 을 몇 초 만에 멈추기 때문이다(§3 "환경 변수 대조"). 값은 비워 두지 않는다: `SBMIG_<섹션>__<키>: ""` 는 YAML null 로 읽혀
구운 yml 의 값을 "없음" 으로 덮는다 — 안 쓸 키는 줄째 주석으로 둔다.

| 변수 | 어디에 | 기본 / 예 | 언제 바꾸나 |
|---|---|---|---|
| `SBMIG_WEB_CONFIGS` | ConfigMap | `artifacts.yml` | 업로드가 돌 설정 파일 — 이미지 `/app/config/` 아래 이름(쉼표로 여러 개) |
| `SBMIG_WEB_OPEN` | ConfigMap | `1` | 사내망 전용이라 토큰 없이 연다. 토큰을 쓰면 지우고 `SBMIG_WEB_TOKEN` |
| `SBMIG_MAX_UPLOAD_MB` | ConfigMap | `1024` (기본 512) | 큰 프로젝트 zip 이 거절될 때. 0 = 없음 |
| `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` | ConfigMap(선택) | 프록시 주소 / 게이트웨이·Nexus 호스트 | 클러스터에서 프록시로 나가야 할 때만 |
| `LLM_GATEWAY` | ConfigMap | `agent` | 게이트웨이 모양이 바뀔 때(옛 모양 = 지움) |
| `LLM_BASE_URL` | ConfigMap | `https://…/agents/{asset_id}/chat/completions` | 게이트웨이 주소 변경 |
| `LLM_ASSET_ID` | ConfigMap | 에셋 id | 에셋 교체 |
| `LLM_TOKEN_URL` | ConfigMap | 토큰 발급 URL | 발급 경로 변경 |
| `LLM_MODEL` / `LLM_ESCALATION_MODEL` | ConfigMap | `build_error_fixer` / `escalation_fixer` | 게이트웨이의 role 이름이 바뀌거나 상위 모델을 끌 때(비움) |
| `LLM_RECIPE_MODEL` | ConfigMap(선택) | (주석) | 계획 검토(`plan.advisor: llm`)를 켤 때의 값싼 모델. 비우면 `LLM_MODEL`. 81 틀에 키 줄이 있다 |
| `LLM_RECORD_DIR` | ConfigMap | (비움) | in-process 진단 때만 `/data/llm-records`. Job 모드는 81 틀의 두 줄(`SBMIG_WORKER_LLM_RECORDS` + `SBMIG_LLM__RECORD_DIR`)을 푼다 |
| `LLM_APP_TOKEN` (또는 `LLM_API_KEY`) | **Secret** | 앱 토큰 | 토큰 교체 |
| `NEXUS_USER` / `NEXUS_PASSWORD` | **Secret** | — | Nexus 그룹이 로그인을 요구할 때(settings.xml 의 `${env.…}` 가 읽음) |
| `SBMIG_WEB_TOKEN` | **Secret**, 선택 | — | 토큰 보호를 켤 때만 |
| `IVAS_BASE_URL` / `IVAS_USER_ID` / `IVAS_PASSWORD` | **Secret** `sbmig-llm` | — | 취약점 조치(§6, 빌드 파드에서 이관에 이어). 81 틀은 `secretKeyRef`(optional), 80 은 `envFrom`. `sbmig-env` 에는 넣지 않는다 |
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
| `SBMIG_BUILD__REPOSITORY_URL` | ConfigMap(선택) | Nexus 그룹 URL | 환경별(개발/운영) Nexus 가 다를 때 — 구운 `artifacts.yml` 값보다 우선 |
| `SBMIG_BUILD__REPOSITORY_TIMEOUT_SECONDS` | ConfigMap(선택) | `10` | Nexus 가 느릴 때 |
| `SBMIG_OPENREWRITE__TIMEOUT_MINUTES` / `JVM_ARGS` | ConfigMap(선택) | `40` / `'["-Xmx4g"]'` | 레시피 실행 시간·힙(limit 보다 2 GiB 이상 작게) |
| `SBMIG_INTAKE__ADF_POLICY` | ConfigMap(선택) | `stop` | ADF 프로젝트를 멈출지(stop) 경고만 할지(warn) |
| `SBMIG_LAYOUT__WIRE_BUNDLED_JARS` | ConfigMap(선택) | `true` | WEB-INF/lib 의 선언 없는 jar 연결을 끌 때 |
| `SBMIG_LIBRARIES__NEXUS_SEARCH` / `INCLUDE_PRERELEASE` | ConfigMap(선택) | `true` / `false` | jakarta 빌드 탐색 범위 |
| `SBMIG_REVIEW__FIX_CRITICAL` | ConfigMap(선택) | `true` | 구조 리뷰의 critical 자동 수정을 끌 때 |
| `SBMIG_FLEET__MIN_FREE_GB` | ConfigMap(선택) | `5` | 실행 전 디스크 여유 하한 |
| `SBMIG_REMEDIATION__AFTER_MIGRATION` | ConfigMap | `true` (켜서 내보냄; 코드 기본 false) | BUILD SUCCESS 뒤 빌드 파드에서 취약점 조치를 이어 돌릴지(§6). 끄면 `"false"` + 웹 파드 재시작 |
| `SBMIG_REMEDIATION__REQUIRE_APPROVAL` / `FAILED_CANDIDATE` / `IVAS_ANALYSIS_TIMEOUT_MINUTES` | ConfigMap(선택) | `false` / `include` / `30` | 조치의 승인 범위·실패한 수정본·IVAS 분석 대기(§6). 40 에만 literal 로 넣지 않는다(Job 이 멈춘다) |
| `SBMIG_REMEDIATION__CHAIN_BUDGET_MINUTES` / `IGNORE_ACTIONS` | ConfigMap(선택) | `90` / 비어 있음(예 `'["PC","IG"]'`) | 빌드 파드 안 조치의 시간 한도(0 = 없음)·계획에서 뺄 IVAS 조치 코드(기본 없음 = 모두 고칠 대상, §6) |
| `SBMIG_REMEDIATION__IVAS_SUMMARY_CHECK` | ConfigMap(선택) | `exact` | IVAS 요약 `components` 건수와 라이브러리 취약점 행 수를 같은지 대조 / `nonzero` = 0 인지만(§6) |

env 가 아닌 것: `artifacts.yml`(이미지에 구운 파일), `settings.xml`(ConfigMap `sbmig-config`, 파일), 이관팀이 고치는 회사 지식 **`<kb.dir>/jar_names.yml`**(jar
이름 → 좌표)과 **`<kb.dir>/libraries.yml`**(사람이 승인한 라이브러리 표) — KB 볼륨(`/data/kb-v2`)에 있어 이미지와 무관하게 바꾼다. 이미지
안의 `knowledge/`(노트, 자문, 레시피 카탈로그)는 이미지를 다시 만들어야 바뀐다.

## 3. Job 디스패치 모드 — 웹 파드(`sbmig serve --mode k8s-job`) + 업로드마다 빌드 Job (2026-11-05 결정·구현)

인프라 가이드의 "Agent 가 요청별 빌드 Job 을 만든다" 를 sbmig 에 맞춘 그림이다. **Agent = 우리 웹 파드**, **빌드 Job = sbmig 한 사이클**.
별도 프로젝트가 아니라 `sbmig serve` 의 실행 모드 하나(`SBMIG_WEB_MODE=k8s-job`)다. 화면·이미지·설정 파일·결과 형식은 ① 과 같다.
**전달·결과·KB 는 전부 공유 PVC** 로 간다(사용자 결정): 웹 파드와 Job 파드가 같은 `sbmig-data` 볼륨을 같은 경로 `/data` 에 마운트한다.
파드 안에 API 는 없고, 웹 파드 → 쿠버네티스 API 호출(Job 생성·상태·삭제)만 있다.

```
담당자 브라우저 ──zip 업로드──▶ 웹 파드 (sbmig serve --mode k8s-job)
                                  │ 1 /data/out/<run id>/source.zip 에 보관, 저널 첫 줄
                                  │ 2 Job 생성 (k8s/81-web-job.template.yaml + 실행별 env: SBMIG_SOURCE_ZIP, SBMIG_OUT, SBMIG_RUN_ID, SBMIG_CONFIG)
                                  │ 3 Job/파드 상태 감시 → 화면 레일의 '파드' 단계 (생성 중 → 준비됨 → 결과 파일을 받으면 체크)
                                  ▼
                            빌드 Job 파드 (같은 이미지, run-job.sh → sbmig run --out /data/out --run-id <run id>)
                                  │ 4 같은 실행 폴더에 journal.jsonl / sbmig.log / report.md / result.zip 을 쓴다
                                  │ 5 끝나면 job-status.json {exit_code}
                                  ▼
웹 파드: 실행 폴더의 저널·로그를 tail → SSE (node · journal · log) ──▶ 담당자 브라우저 ◀── 결과 내려받기 (/data/out/<run id>/)
```

멈춤 = 웹이 `<run dir>/stop.request` 를 쓴다 → 파드의 엔진이 노드 사이에서 멈춘다(최종 빌드 없음, 보고서는 aborted, 종료 3). 유예
`SBMIG_JOB_STOP_GRACE_S`(기본 2700 초) 안에 안 끝나면 Job 을 지운다. 웹 파드가 재시작되어도 Job 은 계속 돌고, 다음 서버는 폴더(`job-status.json`)로 답한다.

**개발 PC 에서는 파드 없이 같은 흐름이 돈다.** `SBMIG_WEB_MODE=k8s-job` 인데 클러스터 밖이면(`KUBERNETES_SERVICE_HOST` 없음) 자동으로
`local-job` 으로 내려가 경고 한 줄을 찍고(`status.mode` 도 `local-job`), 파드 자리에 로컬 서브프로세스 `python -m sbmig.cli run <zip> --out … --run-id …`
를 띄운다(`sbmig/web/localjob.py`; 표준 출력은 `<run dir>/job-local.log`). 실행 폴더·저널 tail·'파드' 단계·`stop.request`·`job-status.json` 까지 운영과
같은 코드가 돌아서, 배포 없이 노트북에서 Job 모드를 시험한다. `sbmig serve --mode local-job` 으로 직접 고를 수도 있다. 로컬 Worker 의 설정은 PC 의
셸과 `.env` 가 정한다 — 틀의 env 는 실행별 값(`SBMIG_SOURCE_ZIP`, `SBMIG_OUT`, `SBMIG_RUN_ID`, `SBMIG_CONFIG` …)만 넘기고, 컨테이너 경로(`/data/kb-v2`,
`/opt/java21` …)와 ConfigMap 참조는 넘기지 않는다(2026-11-10 에 고친 회귀).

### 두 파드가 하는 일

| | 웹 파드 (Agent) | 빌드 Job 파드 |
|---|---|---|
| 프로그램 | `sbmig serve --mode k8s-job` (FastAPI + uvicorn, 워커 1, 대기 줄 = 동시에 `SBMIG_JOB_MAX_PARALLEL` 건; 시작 때 틀·권한 검증, `control/` 상태 파일, 재시작 복구) | `python -m sbmig.web.worker` → `sbmig run` (LangGraph, LLM, OpenRewrite, Maven, 수정 루프 전부; 앞뒤로 입력 digest 검증과 비밀값 정제) |
| 이미지 | `docker/Dockerfile` (같은 이미지, JDK 는 쓰지 않는다) | 같은 이미지 |
| 자원 | CPU 1, 메모리 1~2 GiB | CPU 2~4, 메모리 6~8 GiB (OpenRewrite `-Xmx4g`) |
| 볼륨 | PVC `sbmig-data` → `/data` (업로드, 실행 폴더, KB), **ReadWriteMany** | 같은 PVC 같은 경로 + `sbmig-m2`(또는 emptyDir) |
| 권한 | 인프라팀이 Secret 으로 주는 kubeconfig(`/etc/kube/config`, env `KUBECONFIG` — 회사 방식, 매니페스트에 켜져 있다; 있으면 이것을 먼저 쓴다) **또는** ServiceAccount `sbmig-web` (`k8s/45-web-rbac.yaml`: jobs create/get/list/watch/delete, pods get/list/watch). 어느 쪽이든 그 사용자에게 이 동사들이 있어야 한다 | 없음 |
| 네트워크 | → 쿠버네티스 API | → LLM 게이트웨이, Nexus |

회사 클러스터(2026-11-09): namespace **`agent`**, 스토리지 클래스 **`nfs-csi`**(NFS CSI 드라이버, ReadWriteMany 지원) — 매니페스트에 들어 있다. NFS 라서 볼 것:
uid 1000 으로 쓰기가 되는지(export 의 squash), 웹 화면의 로그 지연이 거슬리면 클래스 `mountOptions` 에 `actimeo=1`, `.m2` 가 느리면 그 PVC 만 노드 로컬로.
PVC 가 RWO 만 되는 클러스터라면 Job 템플릿과 Deployment 의 hostname `nodeSelector` 로 같은 노드에 고정해야 한다(두 파드가 동시에 마운트하기 때문).

### Main 이 지키는 것 (v2.1z-ac, 2026-11-09 — 배포팀 명세를 우리 코드로)

- **입력 고정**: 접수 때 `<run dir>/control/request.json` 에 zip 의 sha256, 설정 파일의 digest, sbmig 버전을 적는다. Worker(`python -m sbmig.web.worker`)는
  시작할 때 같은 값을 다시 계산해 다르면 돌지 않는다(exit 3, `job-status.json` 의 `error`, 저널 `worker_refused`). Main 과 Worker 가 같은 이미지
  여야 하는 이유가 여기 있다(설정 파일이 이미지에 있다. ConfigMap 으로 줄 때는 고친 뒤 웹 파드도 재시작해야 digest 가 맞는다).
- **생성 의도 먼저**: Job 을 만들기 전에 `control/state.json` 을 `creating` + Job 이름으로 쓴다. 생성 응답이 끊겨도(타임아웃, 409) 같은 이름의 Job 을 찾아
  이어받는다 — 같은 요청으로 Job 이 두 개 뜨지 않는다. 상태는 `queued → creating → created → running → finished | stopped | lost | failed`.
- **재시작 복구**: Main 이 다시 뜨면 끝나지 않은 `control/state.json` 을 찾아 `queued` 는 접수 순서로 다시 줄에 세우고, `created` / `running` 은 Job 을 이어서
  감시한다(멈춤·삭제도 된다). 개발 PC 의 local-job 은 복구하지 못하고 `lost` 로 적는다.
- **소유권**: Job 과 파드에 라벨 `sbmig-run-id` 와 `sbmig-owner`(요청마다 난수). Main 은 두 라벨이 다 자기 것인 Job 만 이어받거나 지우고, 지운 뒤에도 Job 이
  실제로 사라질 때까지 기다린 뒤 끝낸다(삭제 요청 수락 = 끝남이 아니다). 라벨이 없는 같은 이름의 Job 은 남의 것으로 본다(2026-11-10, 아래 표).
- **자리는 Job 이 끝난 것을 확인한 뒤에 넘긴다**(2026-11-10): 기다리는 업로드는 앞 Job 과 그 파드가 끝났거나 없어진 것을 쿠버네티스 API 로 확인한 뒤에
  그 자리에서 시작한다(자리 수 = `SBMIG_JOB_MAX_PARALLEL`, 아래 "동시에 여러 건"). 결과 파일을 받았어도 파드가 남아 있으면 기다리고, API 가 답하지 않으면 답할 때까지 기다린다(실행을 실패로 끝내지 않는다).
- **결과 검증**: `job-status.json` 에 `run_id` 와 `attempt` 가 들어 있고 Main 은 지금 시도의 파일만 받는다. 상태 파일 없이 파드만 끝났으면 `verified: false`
  로 표시하고 성공으로 적지 않는다.
- **비밀값 정제**: 비밀 env(이름에 TOKEN / PASSWORD / SECRET / API_KEY)의 값, 실행 중에 교환해 받은 게이트웨이 토큰, URL 의 `user:pass@` 를 `***` 로 지운다.
  2026-11-10 부터 **쓰는 순간**: `SBMIG_REDACT_OUTPUT=1`(40 · 80 의 env; Worker 는 자식 실행에 언제나 켠다)이면 로그 줄·저널 줄·예외 traceback 이 파일과
  파드 로그에 닿기 전에 지워지고, Worker 는 자식의 콘솔 출력도 한 줄씩 지워 파드 로그로 넘긴다. 끝날 때 실행 폴더(보고서, `logs/` 의 Maven 출력)를 한 번
  더 훑으므로, 실행 중에는 `sbmig.log` / `journal.jsonl` 만 내려받을 수 있다. 정제기를 걸 수 없으면 돌지 않는다(`deploy/ENV.md` §6).
  설정 덮어쓰기 `SBMIG_<섹션>__<키>` 는 비밀이 아니라 지우지 않는다(`llm.max_output_tokens` 의 숫자가 보고서에서 사라지지 않게).
  원본 LLM 기록(`llm.record_dir`)은 Worker 에서 기본으로 끈다 — 진단이 필요할 때만 Job 틀의 두 줄 `SBMIG_WORKER_LLM_RECORDS: "1"` +
  `SBMIG_LLM__RECORD_DIR: "/data/llm-records"` 을 푼다. 스위치 없이 폴더만 주면 Worker 가 그 변수를 자식 실행에 넘기지 않는다.

### 환경 변수 대조와 그 밖에 바뀐 것 (2026-11-10 리뷰)

**환경 변수 대조.** 설정은 파일만이 아니다. 설정 파일의 `${LLM_MODEL}` 같은 자리표시자와 `SBMIG_<섹션>__<키>` 덮어쓰기는 파드의 환경 변수가 값을
정하는데, 웹 파드는 `sbmig-env` 를 **시작할 때** 한 번 읽고 Job 파드는 **만들어질 때** 81 틀에 적힌 키만 읽는다. 그래서 웹 파드가 Job 을 만들 때 그
변수들의 값 지문(sha256 앞 16자 — 값 자체는 적지 않는다)을 `control/state.json` 의 `env_pins` 에, 정책을 `env_pin_policy` 에 적고, Worker 가 시작할 때
자기 환경으로 다시 계산해 대조한다. 실행별 값(`SBMIG_CONFIG`, `SBMIG_STOP_AFTER`, `SBMIG_LLM_PROVIDER`)이 접수 기록 `request.json` 과 같은지도 함께 본다.

- **대조하는 것**: 설정 파일(`default.yml` + 업로드가 쓰는 설정)의 `${…}` 변수(지금은 `LLM_GATEWAY`, `LLM_BASE_URL`, `LLM_ASSET_ID`, `LLM_MODEL`,
  `LLM_ESCALATION_MODEL`, `LLM_RECIPE_MODEL`, `LLM_RESPONSE_PATH`)와 파드에 있는 `SBMIG_<섹션>__<키>` 전부(섹션을 통째로 바꾸는 `SBMIG_<섹션>` 도). KB 폴더는 웹 파드의 `SBMIG_KB__DIR` 와 Job
  파드의 `SBMIG_KB_DIR` 를 같은 것으로 본다. **대조하지 않는 것**: 비밀로 보이는 이름(TOKEN / PASSWORD / SECRET / API_KEY 가 든 이름 — 이름 때문에
  `LLM_TOKEN_URL` 도), 프록시 변수, LLM 기록 폴더(`LLM_RECORD_DIR`, `SBMIG_LLM__RECORD_DIR`).
- **다르면**(기본 `SBMIG_JOB_ENV_PIN=refuse`, k8s-job): Job 이 몇 초 만에 돌지 않고 끝난다(exit 3). 이유에는 키 이름만 세 갈래(값이 다름 / 웹 파드에만 /
  Job 파드에만)로 적힌다 — 화면의 결과 줄, 실행 폴더 `job-status.json` 의 `error`, 저널의 `worker_refused` 줄, 파드 로그의 `[worker] REFUSED:` 줄.
  zip·설정 파일·버전 대조의 거절은 zip sha256·설정 파일 이름·sbmig 버전을 말하고, 이 거절은 환경 변수 이름을 늘어놓고 `SBMIG_JOB_ENV_PIN` 을 안내한다.
- **할 일**: `sbmig-env` 를 바꾸면 웹 파드를 재시작한다. 81 틀을 새로 받거나 고치면 ConfigMap `sbmig-job-template` 을 다시 만들고(아래 "켜는 법" 4)
  웹 파드를 재시작한다. "웹 파드에만" 에 나온 키는 대개 81 틀에 그 키의 줄이 없거나 `sbmig-job-template` 이 옛 틀이라는 뜻이다. 고친 뒤 다시 올린다.
- **완화**: `sbmig-env` 에 `SBMIG_JOB_ENV_PIN: "warn"`(웹 파드 재시작) — Job 은 자기 값으로 돌고 저널에 `worker_env_differs` 한 줄(키 이름)과 파드 로그에
  경고를 남긴다. `off` 는 대조하지 않는다. 운영 클러스터에서는 `refuse` 로 둔다. `warn` 이 맞는 것은 두 환경이 원래 다를 때뿐이다 — 개발 PC 의
  `sbmig serve --mode k8s-job` 이 `KUBECONFIG` 로 클러스터에 Job 을 만들 때(웹 쪽은 PC 의 `.env`, Job 쪽은 클러스터의 ConfigMap). `local-job` 은 기본이 `off`.
- 지문은 Job 을 **만드는** 웹 파드가 적는다: 대기 중에 웹 파드가 재시작되면 새 웹 파드의 환경이 기준이다. 확인할 때는 값이 아니라 필드 둘만 본다 —
  `kubectl -n agent exec deploy/sbmig -- cat /data/out/<run id>/control/state.json` 의 `env_pin_policy` 와 `env_pins` 의 키 이름.

**그 밖에 운영자 눈에 보이는 것**

| 바뀐 것 | 보이는 것 | 할 일 |
|---|---|---|
| 자리는 그 Job 이 **끝난 것을 확인한 뒤** 다음 업로드로 넘어간다 | 결과가 나온 뒤에도 파드가 남으면 화면 레일에 "파드가 끝나기를 기다립니다", 1분마다 로그 경고. `SBMIG_JOB_EXIT_GRACE_S`(120 초) 뒤 웹 파드가 그 Job 을 지우고(저널 `job_deleted`) 사라질 때까지 기다린다 | 대개 없음. 오래 가면 `kubectl -n agent describe job sbmig-<run id>` |
| 쿠버네티스 API 장애는 실행을 **실패로 끝내지 않고** 자리를 붙잡는다 | `SBMIG_JOB_API_GRACE_S`(300 초, 이제 알림만) 넘게 답이 없으면 로그 ERROR 한 줄, 저널 `job_api_unavailable`, 화면 안내(그 뒤 10분마다 로그). 자리가 다 찼으면 다음 업로드는 `queued`. 다시 답하면 저널 `job_api_back`, 줄이 저절로 풀린다 | 웹 파드의 API 접근(kubeconfig Secret 의 토큰·인증서, 서비스 계정, 네트워크)을 고친다. 자격 증명을 바꿨으면 웹 파드를 재시작해 다시 읽게 한다 — 고치기 전의 재시작은 소용없다(시작 검증이 API 를 불러 웹 파드가 뜨지 않는다) |
| 우리 라벨(`sbmig-owner` + `sbmig-run-id`)이 없는 같은 이름의 Job 은 **이어받지도 지우지도 않는다** | Job 을 만들 때나 웹 파드 재시작 뒤에 만나면 그 실행이 오류로 끝난다(이유에 그 Job 이 이 요청의 것이 아니라는 말). 멈춤 때는 지우지 않고 저널에 `job_delete_refused` | `kubectl -n agent get job sbmig-<run id> --show-labels` 로 누가 만든 Job 인지 보고, 지워도 되면 `kubectl -n agent delete job sbmig-<run id>` 뒤 다시 올린다 |
| 결과 파일은 **실행이 끝난 뒤에** | 실행 중 파일 목록에는 `sbmig.log` / `journal.jsonl` 만, 그 밖의 파일(`report.md` …)을 주소로 직접 받으면 HTTP 409 | 끝날 때까지 기다린다 |
| 로그·저널은 **쓰는 순간** 정제 | 파드 로그·화면 로그 탭·`sbmig.log`·저널에서 비밀 값이 `***`. 한 줄을 지우다 실패하면 그 줄 대신 `line withheld` 가 든 고정 문구 | 그 문구가 보이면 알린다: `Select-String -Path <받은 sbmig.log> -Pattern "line withheld" -Encoding UTF8` |

### 동시에 여러 건 (2026-11-10)

`sbmig-env` 의 **`SBMIG_JOB_MAX_PARALLEL`**(매니페스트 값 `"3"`)이 동시에 도는 빌드 파드 수다. 웹 파드가 시작할 때 한 번 읽는다 — 바꾸면
`kubectl -n agent rollout restart deploy/sbmig`. 줄이 없거나 `"1"` 이면 예전처럼 한 번에 하나다. 웹 파드 안에서 직접 돌리는 `in-process` 모드는 값과 상관없이
하나다. 81 틀에는 줄이 필요 없다(Job 파드는 자기 자리 번호만 받는다).

| 무엇 | 어떻게 |
|---|---|
| 차례 | 올린 순서. 자리가 다 찼으면 `queued` 로 기다리고, 화면 제목에 "대기 중 — 앞에 N건", 업로드 칸에는 지금 올리면 몇 번째로 기다리는지가 보인다. 자리가 나면 저절로 시작한다(창을 닫아도 차례는 그대로) |
| 자리 번호 | 도는 Job 마다 1 · 2 · 3 중 하나: `control/state.json` 의 `slot`, Job env `SBMIG_JOB_SLOT`, 저널 `job_created` / `worker_slot`. 자리는 그 Job 과 파드가 **끝난 것을 확인한 뒤**에 다음 이관으로 넘어간다 |
| Maven 로컬 저장소 | 자리마다 따로 쓴다. 1 번은 지금까지의 `/home/sbmig/.m2/repository`, 2 · 3 번은 같은 PVC(`sbmig-m2`) 안의 `repository-slot2` / `repository-slot3`(Worker 가 만든다). 한 저장소에 Maven 여럿이 동시에 쓰는 일을 없앤다. 2 · 3 번 자리의 첫 이관은 의존성을 처음부터 받아 느리다(자리마다 한 번). 그 폴더를 만들거나 쓸 수 없으면 공용 저장소가 아니라 **파드 안의 임시 저장소**(`/tmp/sbmig-m2-slot<k>`)를 쓴다: 느리지만 다른 파드와 겹치지 않는다. 저널 `worker_slot` 의 `pod_local: true` 와 파드 로그의 WARNING 이 그 표시이고, PVC `sbmig-m2` 폴더의 소유자 / `fsGroup` 을 고치면 없어진다 |
| KB (`/data/kb-v2`) | 세 파드가 같은 폴더를 읽고 쓴다. 쓰기는 `<kb>/.lock` 파일 하나로 차례를 지킨다: 잠금 안에서 다시 읽고, 임시 파일에 쓴 뒤 이름을 바꿔 넣는다. 한 번의 쓰기는 짧아서(수십 밀리초) 서로 기다리는 시간은 보이지 않는다. 새 케이스 폴더와 스크립트 파일(`fix_v<N>.py`)은 "없을 때만 만들기"로 만들어서 두 파드가 같은 케이스를 동시에 처음 만나도 서로 덮어쓰지 않는다(둘 다 LLM 을 부르고 v1 · v2 로 나란히 남는다). 파드가 잠금을 쥔 채 죽으면 다른 파드는 `kb.lock_timeout_seconds`(300 초)까지 한 번 기다린 뒤 이어 간다(실행이 잠금 때문에 실패하지는 않는다). 낡은 잠금을 깨는 일은 `.lock.breaker` 파일로 한 번에 한 파드만 한다. **KB 를 통째로 바꾸는 `sbmig kb import --replace` 는 도는 Job 이 없을 때만** 한다(폴더를 바꿔 끼우면서 잠금 파일도 같이 옮겨진다) |
| 웹 파드 재시작 | 돌던 Job 은 그대로 돈다. 다시 뜬 웹 파드가 적어 둔 자리 번호 그대로 이어받고, 기다리던 업로드는 접수 순서로 다시 줄을 선다 |
| 멈춤 | 도는 이관은 지금처럼 노드 사이에서 멈춘다. 기다리는 이관에 누른 멈춤은 **바로** 먹힌다: 줄에서 빠지고 `stopped` 로 끝나며(Job 을 만들지 않는다) 뒤에서 기다리던 이관들의 순번이 한 칸씩 당겨진다 |

**인프라팀에 확인할 것.** 파드 크기는 회사 값으로 맞춰 두었다: 웹 파드 1개와 빌드 파드 3개 모두 CPU 2 / 메모리 8Gi, requests 와 limits 가
같다(40 · 81 의 `resources`). 넷이 같이 돌려면 CPU 8 / 메모리 32Gi 의 여유가
있어야 한다. Job 모드의 웹 파드는 빌드를 하지 않으므로(화면, Job 만들기, 로그 읽기) vCPU 를 아껴야 하면 웹 파드를
줄이는 것이 가장 싸다(예: CPU 500m / 메모리 1Gi). 빌드 파드의 메모리는 Maven 힙(`openrewrite.jvm_args` `-Xmx4g`)보다 2 GiB 이상 커야 한다.
CPU 2 로 이관 한 건이 얼마나 걸리는지는 재지 않았다 — 보고서 머리 표의 소요 시간으로 PC 실행과 비교한다. 모자라면 그 파드는 `Pending` 으로 서 있고 화면의 '파드' 단계가 넘어가지 않는다(`kubectl -n agent describe pod <파드 이름>` 의 Events).
namespace 에 ResourceQuota 가 있으면 파드가 만들어지지 않을 수 있다(`kubectl -n agent describe job sbmig-<run id>` 의 Events). `sbmig-m2`(50Gi)에는
저장소가 셋 생기므로 용량을 본다: `kubectl -n agent exec deploy/sbmig -- du -sh /home/sbmig/.m2`. KB 가 있는 NFS 는 마운트 옵션에
`nocto` 가 없어야 한다(기본값이면 된다: 잠금을 잡은 뒤 연 파일이 앞 파드가 쓴 내용을 보여 준다는 보장). 하드 링크가 안 되는 저장소면 스크립트 파일은 "없을 때만 만들기"(배타적 생성)로 쓴다.

**LLM 게이트웨이.** 세 파드가 같은 앱 토큰으로 동시에 호출한다. 게이트웨이 담당에게 두 가지를 확인한다: 앱 토큰 하나의 동시 호출 한도, 그리고 토큰을
다시 교환하면 앞서 받은 토큰이 무효가 되는지. 코드는 둘 다 버틴다: "지금 바쁘다"는 답(429, `Retry-After` 가 붙은 502 / 503 / 504)에는 그 호출을
`llm.busy_wait_seconds`(600 초)까지 줄 서서 다시 보내고(`sbmig.log` 에 `llm gateway busy` / `still busy` 줄), 갓 받은 토큰이 401 로 거절되면 잠깐 쉬고
`llm.auth_retries`(2)번 더 받아 본다(그때 WARNING `LLM gateway rejected a token right after it was exchanged` 가 한 번 찍힌다 — 이 줄이 보이면 알려 달라).
`still busy` 줄이 자주 보이면 값을 `"2"` 로 낮춘다.

**웹 파드를 재시작하면.** 열려 있던 화면은 "서버가 다시 시작되는 중입니다" 로 바뀌고 2 · 4 · 8 … 30 초 간격으로 스스로 다시 붙는다(새로 고칠 필요 없다).
빌드 파드는 종료 신호(SIGTERM: 파드 삭제, Job 시간 초과)를 받으면 sbmig 와 Maven 을 몇 초 안에 멈추고 `job-status.json` 에 그 사실을 적고 끝난다.

**되돌리기.** `SBMIG_JOB_MAX_PARALLEL: "1"` 로 고쳐 apply 하고 웹 파드를 재시작한다. 돌던 Job 은 끝까지 돌고(한도보다 많아도 그대로 둔다), 기다리던
업로드는 도는 수가 한도 아래로 내려온 뒤에 시작한다.

### 켜는 법

1. `k8s/10-storage.yaml`: 회사 값이 들어 있다(`storageClassName: nfs-csi`, 두 PVC 모두 `ReadWriteMany`, namespace `agent`). 다른 클러스터면 이 세 값만 바꾼다.
2. 권한, 둘 중 하나. (a) **회사**: 인프라팀이 준 kubeconfig Secret(`agent-kubeconfig`, 키 이름 `config`) — `k8s/40-deployment.yaml` 의 세 자리
   (`KUBECONFIG` env, `/etc/kube` 마운트, `kubeconfig` 볼륨)가 **켜진 채로 나간다**(2026-11-10). `kubectl -n agent describe secret agent-kubeconfig` 의
   Data 에 `config` 가 보이면 된다; Secret 이름이 다르면 볼륨의 `secretName`, 키 이름이 다르면 env 의 파일 이름을 맞춘다. namespace 는 `sbmig-env` 의
   `SBMIG_K8S_NAMESPACE: "agent"` 가 정한다(없으면 kubeconfig context 의 namespace, 그것도 없으면 `default`).
   (b) kubeconfig 가 없는 클러스터(연습용 kind 등): 그 세 자리를 주석 처리하고 `k8s/45-web-rbac.yaml` 을 먼저 apply 한 뒤 `serviceAccountName:
   sbmig-web` 줄의 주석을 푼다. 코드는 `KUBECONFIG` 파일이 있으면 그것으로, 없으면 서비스 계정으로 API 를 부른다
   (`sbmig/web/k8s.py` `K8sClient.connect`; kubeconfig 의 token / 클라이언트 인증서 사용자만 — `exec` 방식은 거절한다).
3. `k8s/40-deployment.yaml` 과 `k8s/81-web-job.template.yaml` 의 `image:` 에 인프라팀이 push 한 이미지의 전체 이름을 **같은 값**으로 넣는다(§1,
   `imagePullPolicy: IfNotPresent`; pull 인증이 필요하면 `imagePullSecrets`). 회사 설정이 이미지 안에 있으므로 두 이미지가 다르면 Worker 가 digest 불일치로 거절한다.
4. 채운 Job 틀을 ConfigMap 으로 올린다 — 웹 파드는 `/templates/81-web-job.template.yaml`(`SBMIG_JOB_TEMPLATE`, Deployment 의 env)을 읽는다.
   Main 은 **시작할 때** 틀을 검증하고 어긋나면 뜨지 않는다(`MIGRATION_TEMPLATE_INVALID: …`): 컨테이너 하나(backoffLimit 0, restartPolicy Never),
   **envFrom 금지**(변수마다 `valueFrom`), 비밀 env 는 `secretKeyRef` 만(설정 덮어쓰기 `SBMIG_<섹션>__<키>` 는 literal 가능), `LLM_RECORD_DIR` / `SBMIG_WEB_TOKEN` 없음, `/data` 는 PVC 전체를 쓰기 가능으로
   정확히 하나. 권한도 그때 확인한다(SelfSubjectAccessReview: jobs create/get/list/watch/delete, pods get/list/watch — 모자라면 어떤 동사인지 적고 멈춘다).
   이미지 안의 틀 사본은 `CHANGE_ME` 그대로라 쓰지 않는다. 틀을 바꿀 때마다(새 버전의 81 틀을 받았을 때 포함) 같은 명령으로 다시 만들고 **웹 파드를
   재시작한다** — Main 은 시작할 때 틀을 읽어 두므로 재시작 전에는 옛 틀로 Job 을 만든다(옛 틀에 없는 키는 환경 변수 대조에 걸린다).
   ```bash
   kubectl -n agent create configmap sbmig-job-template --from-file=deploy/k8s/81-web-job.template.yaml --dry-run=client -o yaml | kubectl apply -f -
   ```
   Secret 은 immutable 로 두는 걸 권한다(`30-secret.example.yaml`). 토큰을 바꿀 때: 새 이름의 Secret(`sbmig-llm-2`) → 틀의 `secretKeyRef.name` 과
   `40-deployment.yaml` 의 `secretRef` → ConfigMap 틀 다시 apply → 파드 재시작.
5. LLM 게이트웨이 주소가 사내 DNS 로 안 풀리면(인프라팀 임시 명세의 `hostAliases`) Deployment 와 Job 틀 **양쪽**의 `hostAliases` 주석을 같은 값으로 푼다 —
   LLM 을 부르는 쪽은 Job 파드다.
6. **KB 를 먼저 올린다.** `/data/kb-v2` 는 비워 두고 시작하면 안 된다 — 케이스 스크립트뿐 아니라 회사 지식 파일 `jar_names.yml`(번들 jar 이름 → Nexus
   좌표)과 `libraries.yml`(승인한 라이브러리 표)이 거기 산다. 빈 KB 로 돌리면 어제 되던 프로젝트가 vendor jar(IBSheet 류)를 못 묶어 `package … does not
   exist` 로 끝날 수 있다(2026-11-09 회사 PC 에서 확인). 회사 PC 의 KB 폴더를 통째로(비밀값 없음):
   ```powershell
   kubectl -n agent cp .\kb-v2 <웹 파드 이름>:/data/
   kubectl -n agent exec <웹 파드 이름> -- ls /data/kb-v2
   ```
   목적지는 `/data/` 로 적는다 — `/data/kb-v2` 로 적으면 그 폴더가 이미 있을 때 `/data/kb-v2/kb-v2` 로 한 겹 더 들어가 KB 가 빈 것처럼 돈다(kind 에서
   확인, 2026-11-10). 그래서 PC 쪽 폴더 이름이 `kb-v2` 여야 하고, 그 폴더가 보이는 곳에서 상대 경로로 친다(`C:\…` 의 `:` 을 kubectl 이 파드 이름
   구분자로 읽을 수 있다). 둘째 줄에 `cases` 와 `jar_names.yml` 이 바로 보이면 된 것이다. 같은 명령을 다시 치면 같은 자리에 덮어쓴다.
7. ConfigMap `sbmig-env` 의 `SBMIG_WEB_MODE: "k8s-job"` 과 `SBMIG_K8S_NAMESPACE: "agent"` 는 **켜진 채로 나간다**(2026-11-10). 그 전의 파일로 올린
   클러스터는 두 줄을 넣고 `kubectl apply -f deploy/k8s/20-configmap.yaml`, Deployment 를 고쳤으면 `kubectl apply -f deploy/k8s/40-deployment.yaml`
   (`unchanged` 로 나오면 `kubectl -n agent rollout restart deploy/sbmig`).
8. 확인: 시작 로그 `sbmig web UI ready (… mode k8s-job — job template …)`, 업로드 뒤 `kubectl -n agent get jobs -l app=sbmig,kind=run`, 화면 레일의 '파드' 단계,
   실행 폴더에 파드가 쓴 `sbmig.log` / `job-status.json`.

**빌드 파드는 언제 사라지나.** 동시에 도는 빌드 파드는 `SBMIG_JOB_MAX_PARALLEL` 개까지다(넘는 업로드는 줄을 선다). 이관이 끝나면 파드의 프로세스가 끝날 뿐 아무도 삭제 명령을 내리지
않는다: 파드는 `Completed`(거절·중단처럼 종료 코드 3 이면 `Error`)로 **꺼진 채** 목록에 남고 CPU·메모리는 그때 반납된다. `Error` 여도 결과 파일을 남겼으면
화면의 '파드' 단계는 체크다(파드는 제 일을 했다. '파드' 가 X 인 것은 파드가 결과 없이 죽었거나 Worker 가 요청을 거부한 때뿐이다). 그 기록(Job 과 파드, 파드 로그)은
81 틀의 `ttlSecondsAfterFinished`(3600 = 1 시간) 뒤 쿠버네티스가 지운다 — 하루 수십 건이 돌아도 목록에는 지난 한 시간에 끝난 것만 보인다. 결과와 `sbmig.log` 는
볼륨(`/data/out/<run id>/`)에 남는다. 웹 파드가 Job 을 지우는 것은 예외 셋뿐이고 자기 라벨 둘이 붙은 Job 만 지운다: 중단을 눌렀는데 `SBMIG_JOB_STOP_GRACE_S`
(2700초) 뒤에도 돌 때, 결과 파일을 쓰고도 파드가 `SBMIG_JOB_EXIT_GRACE_S`(120초) 넘게 남을 때, 웹 쪽 오류로 실행이 끝났는데 Job 이 남았을 때. 4 시간을 넘긴
Job 은 쿠버네티스가 끊는다(`activeDeadlineSeconds`). 틀의 값을 바꾸면 ConfigMap `sbmig-job-template` 을 다시 만들고 웹 파드를 재시작한다(새 Job 부터 적용).

### 있는 것 / 다음 것

| 조각 | 상태 |
|---|---|
| `sbmig serve --mode k8s-job`: 업로드 → 실행 폴더 + Job 생성 → 상태 감시 → 화면 → 멈춤(stop.request / 삭제) | 있음 (`sbmig/web/runs.py`, `sbmig/web/k8s.py`) |
| 쿠버네티스 API 클라이언트(requests, 서비스 계정 토큰·CA; `kubernetes` 패키지 없음) | 있음 (`sbmig/web/k8s.py`) |
| Job 템플릿(공유 볼륨) | 있음 (`k8s/81-web-job.template.yaml`) |
| `run-job.sh` 공유 볼륨 모드(`SBMIG_SOURCE_ZIP`, `SBMIG_OUT`/`SBMIG_RUN_ID`, `SBMIG_KB_DIR`, `job-status.json`) | 있음 (호환용 — 틀의 command 는 아래 Worker) |
| Python Worker `sbmig.web.worker`(입력 digest 검증, 비밀 정제, attempt 가 든 job-status) + Main 의 `control/` 상태 파일·재시작 복구·소유권 라벨·결과 검증·시작 때 틀/권한 검증 | 있음 (v2.1z-ac, 2026-11-09) |
| 리뷰(2026-11-10): Job 이 끝난 것을 확인한 뒤의 차례, 두 라벨이 다 맞아야 하는 소유, 환경 변수 대조(`SBMIG_JOB_ENV_PIN`), 쓰는 순간의 출력 정제(`SBMIG_REDACT_OUTPUT`) | 있음 (`sbmig/web/runs.py`, `sbmig/web/worker.py`, `sbmig/web/contract.py`, `sbmig/web/redact.py`, `sbmig/core/scrub.py`) |
| 엔진: 노드마다 `graph_node_end` 저널(화면의 node 이벤트), `stop.request` 로 노드 사이 멈춤 | 있음 (`sbmig/graph/engine.py`) |
| 가짜 클라이언트 테스트 | 있음 (`tests/web/test_job_mode.py`, `tests/deploy/test_run_job_sh.py`) |
| 클러스터 값 | namespace `agent`, `nfs-csi` ReadWriteMany, 이미지는 인프라팀 레지스트리에 push — 매니페스트에 반영(2026-11-09). 첫 apply 와 권한 실측은 **다음** |
| 동시 Job N 개(`SBMIG_JOB_MAX_PARALLEL`, 매니페스트 값 3): 자리 번호, 자리별 Maven 저장소, 대기 순번 화면, KB 동시 쓰기 보강 | 있음 (2026-11-10; `sbmig/web/runs.py`, `sbmig/web/worker.py`, `sbmig/kb/lock.py`, `sbmig/errors/cases.py`) — 회사 클러스터·NFS 에서의 실측은 **다음** |
| 외부 접수 API(remediation 과 같은 계약: zip + `project_name` / `name` + `Idempotency-Key`) | **다음** — 구현 지시서 `docs/v2/API-MIGRATIONS-KO.md`(사내 LLM 에 인계) |
| Main 파드 ↔ Job 파드의 실행 상태 계약(책임·id·폴더·종료 코드·멈춤·복구·슬롯; remediation 통합 1단계) | 있음 — `docs/v2/JOB-RUN-STATE-CONTRACT-KO.md` |

### 회의에서 정할 것 (남은 것)

1. ~~PVC 접근 모드~~ 정해짐(2026-11-09): `nfs-csi`, ReadWriteMany. **이미지**: 인프라팀 레지스트리의 전체 이름과 pull 인증(Secret) 여부를 받는다.
2. **네트워크.** 빌드 노드 → LLM 게이트웨이(토큰 URL + 호출 URL), Nexus. 웹 파드 → 쿠버네티스 API.
3. **Job 자원 상한.** `81-web-job.template.yaml` 의 requests/limits, `activeDeadlineSeconds`(4 시간).

### 가이드 ↔ sbmig 대응

| 가이드의 말 | sbmig 에서 |
|---|---|
| "빌드 이미지: JDK, 빌드 도구, 분석 도구, git" | `docker/Dockerfile` 의 이미지 하나 |
| "소스 처리 · 빌드 · 분석 · 결과 저장 · 커밋 뒤 종료" | `docker/run-job.sh`: zip(또는 git) → KB clone → `sbmig run` → 결과 전달 → 종료 코드 |
| "결과 저장", "저장소 커밋" | 결과 POST(웹 파드) 또는 결과 저장소 push(`SBMIG_RESULT_GIT_URL`); 프로젝트 브랜치 `sbmig/<run id>` push |
| `/workspace` emptyDir | `sbmig run --out /workspace/out`; 파드가 죽기 전에 밖으로 보낸 것만 남는다 |
| Agent: `kubectl create -f /templates/build-job.yaml` | 웹 파드의 디스패치 모드(있음) — 같은 자리: ConfigMap `sbmig-job-template` 을 `/templates` 에 마운트, `SBMIG_JOB_TEMPLATE`; 손으로는 `k8s/submit-job.sh` |
| `/etc/kube/config` 는 Agent 에만 | 그대로 — 웹 파드가 `KUBECONFIG` 로 그 파일을 읽어 API 를 부른다(서비스 계정 대신, 2026-11-09). 빌드 파드는 쿠버네티스 API 를 부르지 않는다 |

### 템플릿에서 바꿀 자리 (가이드 2절 그대로 + sbmig 용)

`namespace`(회사 값 `agent` 가 들어 있다), `nodeSelector` 라벨, `image`(인프라팀 레지스트리의 전체 이름 — §1; 레지스트리 없이 노드에 적재한 이미지면
`sbmig:<tag>` + `imagePullPolicy: Never` — §1.1; `imagePullSecrets` 는 pull 에 인증이 필요할 때만); `activeDeadlineSeconds`(이관 한 번이 1~3 시간 — **4 시간** 권장),
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

두 곳에 들어간다. (1) `settings.xml` 의 `<mirror><url>` — mvn 이 모든 저장소를 그쪽으로 돌린다(`mirrorOf *`). (2) `artifacts.yml` 의
`build.repository_url` — sbmig 가 메타데이터 / pom 을 직접 읽어 릴리스 목록과 jakarta 빌드를 찾는 주소. (1) 은 `k8s/20-configmap.yaml` 의
`<https://nexus.company/…>` 자리, (2) 는 이미지에 구운 `artifacts.yml` 의 값이다(클러스터에서 다른 주소를 써야 하면 `sbmig-env` 의
`SBMIG_BUILD__REPOSITORY_URL`). Nexus 에 인증이 필요하면 `settings.xml` 의 `<servers>` 는 Secret 으로 빼서 마운트한다.
미러는 꼭 둔다: Maven 3.8 이상은 http:// 저장소를 기본 차단 미러(`maven-default-http-blocker`)로 막는데, Job 파드는 `~/.m2` 캐시가
없어서 미러 없이 http Nexus 만 믿으면 사내 아티팩트마다 `Blocked mirror for repositories` 로 실패한다. `mirrorOf *` 미러가
그 차단보다 먼저 적용되므로 Nexus 가 http 여도 미러를 두면 된다(이관 PC 에서 2026-11-05 에 실제로 겪은 일: `config/settings.nexus.example.xml` 주석).

## 5. 팀원 초안(2026-11-04)에서 바꾼 것

* 웹 토큰 환경 변수는 `WEB_API_TOKEN` 이 아니라 **`SBMIG_WEB_TOKEN`** 이다 — 그리고 사내망 전용이라 기본 배포는 토큰 없이
  `SBMIG_WEB_OPEN=1` 로 연다(2026-11-05).
* `kb.dir` 기본값은 이미지 안 `/app/kb-v2` 라 파드가 바뀌면 KB 가 사라진다 → 매니페스트의 env 로 `/data/kb-v2`(웹 UI: `SBMIG_KB__DIR`, Job 파드: `SBMIG_KB_DIR`) / 80 모델은 KB 저장소.
* 의존성 목록을 Dockerfile 에 다시 적지 않는다(`pip-deps.py` 가 `pyproject.toml` 을 읽는다). `python-multipart` 는 필요 없다 — 업로드는
  zip 본문을 그대로 스트리밍한다.
* `JAVA_HOME` 은 아키텍처별 경로 대신 `/opt/java21` 심볼릭 링크(팀원 아이디어, 인용부호만 고침).
* 에이전트 + 빌더 파드를 `kubectl exec` 로 잇는 레이아웃은 인프라 가이드의 Job 모델이 나오면서 뺐다(이력: `docs/INTERFACES.md` v2.1z-r/s).
  sbmig 에 남아 있는 `build.setup_command` / `teardown_command` 훅은 그 레이아웃용이었고 Job 모델에서는 비워 둔다.

## 6. 취약점 조치 — 빌드 파드에서 이관에 이어서 (2026-11-11)

이관된 Maven 프로젝트를 그대로 빌드하고 IVAS 로 검사한 뒤, 프로젝트 pom 에 적힌 취약 라이브러리의 **버전 글자만** 고쳐 다시 빌드·재검사하고
결과 ZIP 과 보고서를 낸다(소스·구조는 건드리지 않는다). 예외는 하나 — 간접 의존성·부모/BOM 이 관리하는 버전은 루트 pom 의
`<dependencyManagement>` 에 `<dependency>` 항목 하나(groupId / artifactId / version)를 넣는 제안으로 고치고, 이 제안은 설정과 무관하게 늘 사람 승인을
기다린다. 계약은 `docs/INTERFACES.md` "remediations (취약점 조치)", LLM 용 설명은 `agent-doc/11-remediations.md`.

* **어디서 도나(사용자 결정 2026-11-11)**: 웹 파드가 아니라 **빌드 파드**다. 이관이 **BUILD SUCCESS** 로 끝나고 `remediation.after_migration` 이
  켜져 있으면 같은 `sbmig run` 프로세스가 이관 그래프의 `remediation` 노드(finalize 뒤)에서 별도의 조치 그래프를 부른다
  (sbmig/remediations/chain.py). 결과는 그 실행 폴더의 **`<run dir>/remediation/`**(`/data/out/<run id>/remediation/` — 원본 ZIP, `state.json`,
  `report.md` / `report.json`, `delivery/result.zip`, 라운드별 계획·패치·빌드 로그, 검사 결과)이고 조치의 id 는 **이관 run id** 와 같다.
  빌드 파드의 JDK 21(`/opt/java21`), Maven, ConfigMap `sbmig-config` 의 `settings.xml`(필수 — 승인된 저장소·미러가 여기서 나온다)을 쓴다. Maven 로컬
  저장소는 **그 파드의 자리 저장소**다(1번 자리 `~/.m2/repository`, k번 자리 `repository-slot<k>` — §3 "동시에 여러 건"; `-remediation` 접미사는
  in-process 모드의 단독 업로드에만 붙는다). 테스트는 늘 건너뛴다(`-Dmaven.test.skip=true`), TLS 검증은 끄지 않는다.
* **이관 판정은 그대로**: 조치의 성패는 이관의 성공 판정·종료 코드·보고서 판정을 바꾸지 않는다. 운영자가 옮겨 적는 줄 끝에
  `SBMIG-RESULT … remediation=<상태|skipped>` 가 붙는다. IVAS 세 값(또는 Maven 준비)이 없으면 조치를 건너뛰고 저널에 `remediation_skipped`
  (빠진 것의 **이름**만)를 남긴다. 이관이 BUILD SUCCESS 가 아니면 조치는 돌지 않는다. `remediation.trusted_projects` 는 이 경로에서 묻지 않는다
  (이관이 이미 그 프로젝트를 빌드했다) — 단독 업로드만 막는 스위치다.
* **사람이 정할 것이 생기면**: 빌드 파드는 사람을 기다리지 못한다. `AWAITING_APPROVAL`(승인 대기) / `AWAITING_CONFIRMATION`(업로드 결과 확인)이면
  상태를 저장하고 파드가 끝난다. 승인·제안·결과 파일은 웹 파드에서 본다. 화면의 승인은 체크박스 목록과 버튼 하나 **`선택한 항목 적용`** 이다:
  체크한 제안 = 승인, 체크하지 않은 제안 = 거절, 확인 창("N건 적용, M건은 적용하지 않음") 뒤 **재개가 자동으로** 일어난다(API 로는
  `POST …/approvals` 에 `"resume": true` — 답에 `resumed` 또는 `resume_error`, 결정은 어느 쪽이든 기록된다; 승인자 이름 `by` 는 비워도 된다).
  거절한 제안은 그 작업에서 다시 제안하지 않는다.
* **재개 = 새 빌드 파드**: Job 모드에서 이관에 이어 돈 조치의 `resume` 은 같은 Job 틀로 **새 Job** 을 같은 실행 폴더에 띄운다(Worker 동작
  `remediation-resume` → `chain.resume`). 업로드와 **같은 FIFO 줄**에서 `SBMIG_JOB_MAX_PARALLEL` 자리 하나를 기다린다(§3 "동시에 여러 건").
  필요한 것은 모두 공유 볼륨의 `state.json` 에 있다 — 검사 중에 멈췄으면 IVAS `projectId` / `analysisId`(`pending_receipt`)가 있어 **다시 올리지
  않고** 같은 분석을 이어서 조회한다. 업로드 결과 확인 대기면 웹 파드가 요청의 `confirm` / `label` / `project_id` / `analysis_id` 를 Job 을 만들기
  **전에** `state.json` 에 쓴다.
* **웹 파드가 하는 일**: 상태·제안·승인 기록·파일 내려받기만(`/api/remediations/<이관 run id>/…`). 웹 파드는 조치를 돌리지 않으므로 Job 모드에서
  IVAS 값을 쓰지 않는다(Secret 을 envFrom 으로 통째로 받기는 한다). 단독 업로드 `POST /api/remediations` 는 **in-process 모드에서만** 받고
  Job 모드(`k8s-job` / `local-job`)에서는 409 `not_available` 이다. in-process 모드의 단독 조치는 예전 그대로다: 웹 파드 안의 작업 스레드, 한 번에 한 건,
  결과는 `/data/out/_remediations/<id>/`, `remediation.trusted_projects: true` 가 있어야 접수, 웹 파드가 다시 뜨면 `INTERRUPTED` + 명시적 재개.
* **스위치를 어디에 두었나** — `SBMIG_REMEDIATION__AFTER_MIGRATION: "true"` 는 ConfigMap **`sbmig-env` 의 그룹 3** 에 켜져 있다(코드 기본값은 false).
  설정 덮어쓰기라 Worker 의 환경 대조(§3) 대상이고, 웹 파드와 Job 파드의 값이 다르거나 한쪽에만 있으면 그 Job 은 `refuse` 로 멈춘다. `sbmig-env`
  한 곳에 두면 웹 파드(40, `envFrom`)·웹이 만드는 Job(81, 그 키의 `configMapKeyRef` 한 줄)·80 모델(`envFrom`)이 같은 값을 받는다. 40 / 81 의
  literal env 로 두면 두 파일을 늘 같이 고쳐야 하고 하나를 빠뜨리면 모든 업로드가 멈춘다 — 그래서 literal 로 두지 않았다. 끄려면 `"false"` 로 바꾸고
  웹 파드를 재시작한다. 같은 이유로 조치 설정(`SBMIG_REMEDIATION__REQUIRE_APPROVAL`, `…__FAILED_CANDIDATE`, `…__IVAS_ANALYSIS_TIMEOUT_MINUTES`,
  `…__CHAIN_BUDGET_MINUTES`, `…__IGNORE_ACTIONS`, `…__IVAS_SUMMARY_CHECK`)도
  `sbmig-env` 그룹 3 에 주석으로 있고 81 에 키 줄이 있다. **40 에만 literal 로 넣으면 Job 이 멈춘다**(예전 안내 "40 의 env 에 한 줄" 은 이제 틀리다).
* **Secret 에 넣을 것**: `sbmig-llm` 에 `IVAS_BASE_URL`(https 주소, 끝의 `/` 없이, 주소 안에 계정·쿼리 금지), `IVAS_USER_ID`, `IVAS_PASSWORD`
  (`k8s/30-secret.example.yaml`). 81 은 세 키를 `secretKeyRef`(`optional: true` — IVAS 가 없는 클러스터에서도 이관은 돈다)로, 80 은 `envFrom` 으로 받는다.
  비밀은 환경 대조의 지문에 들어가지 않는다(`IVAS_BASE_URL` 도 덮어쓰기·설정 자리표시자가 아니라 대상이 아니다). 로그·보고서에서는 `IVAS_PASSWORD`,
  **`IVAS_USER_ID`**(이름에 비밀 낱말이 없어도 — 4자 이상), 로그인 토큰이 `***` 로 지워진다. Worker 의 마지막 정제는 `remediation/` 의 텍스트 파일도
  훑고 ZIP(`original/source.zip`, `delivery/*.zip`)·프로젝트 트리(`accepted/`, `rounds/<n>/candidate/`)·`original/` 은 건드리지 않는다.
  IVAS 로그인은 파드마다 따로 해도 된다(여러 로그인이 동시에 유효함을 사용자가 세 개로 확인했다) — 빌드 파드 셋이 함께 돌아도 된다.
* **시간**: Job 의 `activeDeadlineSeconds` 는 14400(4시간) 그대로다. 조치가 더하는 것: 기준 빌드 1회 + IVAS 업로드·분석(대기 한도
  `remediation.ivas_analysis_timeout_minutes` 30분) + 라운드마다 후보 빌드 + 재검사(업로드·분석 다시). 보통(빌드 수 분, 분석 수십 분, 1라운드)은
  20~75분이다. 최악은 설정 상한의 합 — 빌드 한 번 `remediation.build_timeout_minutes` 60분, 업로드 `ivas_upload_timeout_seconds` 30분, 분석 30분,
  `max_rounds` 3 → 60 + 60 + 3 × 120 = 약 8시간 — 이고, 이관 자체의 상한(`loop.max_total_minutes` 240분)이 이미 4시간이라 최악끼리는 어떤 숫자로도
  다 담기지 않는다. 기한에 걸리면 Worker 가 종료 신호를 받아 exit 3 으로 끝나고(이관 보고서는 남는다; `job-status.json` 의 오류 글에 이관의
  `SBMIG-RESULT` 줄이 붙는다), 웹은 그 실행을 "중단" 으로 보인다. 큰 프로젝트가 기한에 자주 걸리면 81 의 숫자를 늘리고 ConfigMap
  `sbmig-job-template` 을 다시 만든다. **조치의 시간 한도** `remediation.chain_budget_minutes`(기본 90분, 0 = 없음): 이 파드에서 조치가 그만큼
  돌면 다음 경계에서 `STOPPED`, 이유 **`chain_budget`** 으로 멈추고(저널 `remediation_budget`) 파드는 정상으로 끝난다 — 재개하면 새 빌드 파드가
  이어 간다(한도는 파드마다 다시 잰다). 진행 중이던 IVAS 분석은 IVAS 에서 계속 돌고 재개가 같은 분석을 이어서 조회한다. 바꾸려면
  `sbmig-env` 의 `SBMIG_REMEDIATION__CHAIN_BUDGET_MINUTES` 주석을 풀고 웹 파드를 재시작한다.
* **IVAS 조치 구분 코드**(`action.actionDivCd`): NE 조치 필요 / NP 조치계획 미등록 / PL 조치예정 / OT 조치일 경과 / PC 패치 완료 / IG 조치 예외.
  `remediation.ignore_actions` 의 기본은 **비어 있음** — 모든 조치 구분을 고칠 대상으로 본다(기존 사내 구현도 PC·IG 를 거르지 않는다, 2026-11-11).
  보안팀이 예외 처리한 항목을 빼려면 `SBMIG_REMEDIATION__IGNORE_ACTIONS: '["PC","IG"]'` 처럼 적는다: 그 코드의 항목은 검사 파일과 보고서의 "조치
  대상에서 제외한 항목" 표에 남지만 계획하지 않고 남은 취약점으로 세지 않는다. 빈 코드·모르는 코드는 늘 조치 대상이다. 바꾸면 정책 해시가 바뀌어 열린
  제안은 다시 만들어진다.
* **IVAS 요약 대조**(`remediation.ivas_summary_check`, 기본 `exact`): 기존 사내 구현처럼 요약의 `components.total` 이 모든 쪽에서 받은 라이브러리
  취약점 상세 행 수(중복 제거 전)와 같아야 검사 완료다. 다르면 `summary_mismatch`(검사 미완료, 두 수는 검사 메모에). `fileComponents` · `swComponents`
  는 상세 행과의 대응이 확인되지 않아 0 인지 아닌지만 본다. 실제 IVAS 의 건수 뜻이 달라 멀쩡한 검사가 미완료로 끝나면
  `SBMIG_REMEDIATION__IVAS_SUMMARY_CHECK: "nonzero"` 주석을 풀고 웹 파드를 재시작한다(정책 해시와 무관).
* **80 모델(요청별 Job, emptyDir)**: 같은 스위치·Secret 이 `envFrom` 으로 닿아 조치도 돈다. 그러나 작업 공간이 파드와 함께 사라지므로
  `run-job.sh` 는 결과 저장소에 `remediation/` 의 글로 된 요약(`report.md`, `report.json`, `state.json`, `journal.jsonl`, `delivery.json`)만 보낸다.
  `result.zip` 은 git 에 넣지 않는다 — 필요하면 `RESULT_PUSH_CMD` 로 실행 폴더째 내보낸다. 이 모델에는 승인 화면도 없다.
* **후보 빌드가 실패하면**(`remediation.failed_candidate`, 기본 `include`): 실패한 수정본도 `result.zip` 으로 나온다 — 상태 `FAILED`
  `candidate_build_failed`, `delivery.build_ok` 가 `false`, IVAS 재검사 없음, `report.md` 첫 줄에 "빌드가 실패한 수정본" 경고. 앞 라운드에서 검증을
  마친 ZIP 이 있으면 `result-verified.zip`. `discard` 면 내지 않는다. "계획한 것만 바뀌었나"·해석 검증에 걸린 후보는 어느 값이든 내지 않는다.
* **업로드 결과를 모를 때**(`AWAITING_CONFIRMATION`): 자동 재업로드는 없다. IVAS 화면에서 그 분석이 있는지 보고 재개한다(`{"confirm": "reupload"}` /
  `{"confirm": "receipt", …}`) — Job 모드면 위의 새 빌드 파드로.
* **채울 자리**: `k8s/30-secret.example.yaml` 의 `IVAS_BASE_URL`(`https://CHANGE_ME_IVAS_HOST[:port]/<base path>`), `IVAS_USER_ID`(`<IVAS 계정>`),
  `IVAS_PASSWORD`(`<IVAS 비밀번호>`). 그 밖에 조치만을 위한 자리는 없다(`settings.xml` 의 Nexus 주소는 §4 그대로).

### 이 변경을 받은 뒤 운영자 점검표

1. **이미지**: 새 태그로 빌드·push 하고(§1) 40 과 81 의 `image:` 를 그 태그로 바꾼다(두 파드가 같은 이미지여야 Worker 의 버전 대조가 맞는다).
2. **Secret**: `30-secret.yaml` 에 IVAS 세 키를 넣고 **새 이름**(예: `sbmig-llm-2` — Secret 이 `immutable` 이라 키를 더할 수 없다)으로 만든다.
   40 의 `secretRef`, 81 의 `secretKeyRef` 일곱 줄(`LLM_APP_TOKEN` … `IVAS_PASSWORD`)의 `name`, (80 을 쓰면) 80 의 `secretRef` 를 새 이름으로 바꾼다.
3. **ConfigMap `sbmig-env`**: `kubectl apply -f deploy/k8s/20-configmap.yaml`(그룹 3 의 `SBMIG_REMEDIATION__AFTER_MIGRATION: "true"`).
4. **ConfigMap `sbmig-job-template`** 를 다시 만든다:
   `kubectl -n agent delete configmap sbmig-job-template` → `kubectl -n agent create configmap sbmig-job-template --from-file=deploy/k8s/81-web-job.template.yaml`.
5. **웹 파드**: `kubectl apply -f deploy/k8s/40-deployment.yaml` 뒤 `kubectl -n agent rollout restart deploy/sbmig` — 웹 파드는 시작할 때
   `sbmig-env` 와 Job 틀을 읽는다. 재시작하지 않으면 다음 Job 이 `SBMIG_REMEDIATION__AFTER_MIGRATION` 을 "set in the Job pod only" 로 적고 멈춘다.
6. **확인**: 작은 프로젝트 하나를 올려 Job 파드 로그의 `SBMIG-RESULT … remediation=…` 줄을 본다. `remediation=skipped` 면 저널의
   `remediation_skipped` 줄이 빠진 값의 이름을 알려 준다(대개 Secret 이름을 81 에서 바꾸지 않은 경우).
