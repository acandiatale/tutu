# 배포와 운영

> 다루는 것: 실제 운영: 회사의 운영 방식(웹 UI에 zip 업로드), PC·Linux 설치, 컨테이너 이미지와 쿠버네티스 두 모양, 일괄 실행(fleet), PC 사이의 KB 옮기기, 디스크 정리, 로그 파일, 문제를 보고할 때 보낼 줄.
> 언제 읽나: sbmig를 새 PC / 서버 / 클러스터에 올릴 때, 운영 중 무엇을 보고 무엇을 보내야 할지 모를 때.
> 함께 읽을 파일: [../deploy/README.md](../deploy/README.md), [07](07-config-and-knowledge.md), [03](03-run-debug-and-logs.md), [10](10-open-items-and-history.md)

## 1. 회사가 sbmig를 쓰는 방식

운영 모델은 웹 UI다: 담당자가 자기 프로젝트를 zip으로 올리면 떠 있는 웹 앱(`sbmig serve`)이 이관을 돌린다.

흐름: 브라우저가 zip을 올림 → `sbmig serve`(FastAPI + uvicorn, 워커 1: 실행 큐가 프로세스 안; 실행마다 작업 스레드 하나와 LangGraph 그래프 intake → … → finalize) → SSE로
node / journal / log / report 이벤트 → 보고서, result.zip, errors.md, sbmig.log … 내려받기.

- 화면(머리 `panacea-remediating-oss`, 임시): 진행 방식 전체 진행(기본, 이관 후 조치) / 취약점 해결(조치만, [11](11-remediations.md)) → 프로젝트명(필수, 안내 「6자리 체인코드가 포함된 레포지토리 이름으로 작성해 주세요.」, `runs.PROJECT_RE`: 영문·숫자·`_`·`-` 30자; 어기면 400) + zip →
  `마이그레이션 시작`(또는 `취약점 조치 시작`) → 단계 레일과 진행 기록 / 로그 탭(마지막 1,500줄) → `보고서 받기(migration-report.md)` / `변환된 프로젝트 받기(result.zip)`. 미리보기는 따로 둘: 결과 카드 끝 「마이그레이션 보고서 미리보기」, 조치 칸 안 「조치 결과 보고서 미리보기」(`remediation/report.md` 그대로, [01](01-repo-map.md)).
  설정 파일은 서버가 정한다: `sbmig serve --config <이름>`(여러 번) 또는 `SBMIG_WEB_CONFIGS=a.yml,b.yml`, 없으면 `config/artifacts.yml` →
  `artifacts.local-e2e.yml` 중 있는 것(`GET /api/configs`의 `default`). 고급 설정(숨김, 그 값은 안 보냄)과 그 밖의 파일 목록은 화면에 없다(파일은 관리자 페이지).
  시작은 파일과 맞는 이름이 있어야 눌린다. 실행 폴더 `<프로젝트명>-YYYYmmdd-HHMMSS-<hex 6>`(`ls /data/out | grep <프로젝트명>`; 예전 이름도 그대로),
  실행 카드·「지난 실행」은 프로젝트명 먼저. 접수 거절(Ant, 풀기 한도를 넘은 ZIP 등)은 사유를 문장으로
  보임. 「지난 실행」은 최근 3일(`GET /api/runs?days=`; 대기·실행 중, 조치 대기, 열어 둔 실행 `keep`은 늘 보임; 「이전 실행 보기」로 전체,
  지우지 않음). 멈춘 실행(`user_stop`)·처리 불가는 주황, 실행 오류만 빨강. 화면은 `sbmig/web/static/index.html` 한 파일(폰트·CDN 없음).
- 웹 파드가 직접 돌리면 한 번에 하나, Job 모드는 동시에 `SBMIG_JOB_MAX_PARALLEL` 건(기본 1, 매니페스트 3). 넘는 zip은 `queued`로 기다리고 화면에 순번이 보인다.
- 중지는 그래프 노드 사이에서만 먹힌다. 계획한 최종 빌드가 모두 끝난 뒤 닿은 요청은 무시한다([02](02-flow-and-nodes.md) 11절).
- 접근 제어: 기본 배포는 `SBMIG_WEB_OPEN=1`(`--open`)로 토큰 없이 받는다(시작 때 경고).
  `SBMIG_WEB_TOKEN`(`--token`)이 있으면 모든 요청에 요구하고(open보다 우선), `http://<host>:8765/?token=<토큰>`로 한 번 열면 HttpOnly 쿠키가 남는다.
  둘 다 없으면 루프백만 받고 `--host 0.0.0.0`이면 뜨지 않는다. 다른 사이트의 요청은 거절: GET / HEAD / OPTIONS가 아닌 요청에
  `Sec-Fetch-Site: cross-site`가 있거나 `Origin` 호스트가 `Host` / `X-Forwarded-Host`와 다르면(`Origin: null` 포함) 토큰 검사보다 먼저 403
  `cross-site request refused: …`(모든 경로). 두 헤더 없는 curl과 이 화면은 통과.
- 업로드 상한: `--max-upload-mb`, 기본 `$SBMIG_MAX_UPLOAD_MB` 또는 512 MB(이미지 1024, 명령줄에 없어 ConfigMap 값이 먹음), 0 = 없음. 본문을
  받기 전 출력 볼륨 여유가 `fleet.min_free_gb`(5 GB)보다 적으면 HTTP 507(화면 「서버의 저장 공간이 부족해 …」, 0 = 안 봄). 실행 폴더는 저절로
  안 지워진다(§8). ZIP 풀기 한도 `intake.max_unpacked_mb`(4096), `intake.max_entry_mb`(1024), `intake.max_compression_ratio`(200, 1 MiB 이상
  풀리는 파일마다; 0 = 없음)를 목차로, 풀면서 다시 본다. 넘으면 아무것도 풀지 않고 `run aborted: ZIP refused: …`(exit 3, 저널 `intake_refused`,
  Maven·LLM 없음; 웹 파드가 직접 돌리면 HTTP 400).
- 관리자 페이지 `/admin`(링크 없음): 내장 비밀번호(`SBMIG_ADMIN_PASSWORD`로 교체)로 프로젝트명별(예전 실행은 파일 이름별) LLM 토큰 사용량(조치 수정 루프 몫 포함, 실행마다 「조치 N회 포함」; 일부를 모르면 `usage_partly_unknown`, 각 행에 「일부 알 수 없음」, 비용 = 가격 아는 행의 합(없으면 「알 수 없음」); 모델 이름: [05](05-llm-gateway-and-prompts.md) 9.1)·로그 ZIP·올린 원본. 열린 모드에서도 이것만 막힌다(실행별 결과 파일은 열린 API). `sbmig/web/admin.py`, `deploy/README.md` §2.
- `sbmig serve`가 곧 uvicorn이다. 종료 시 `--graceful-timeout`(5초) 뒤 SSE를 닫고 Maven /
  OpenRewrite를 정리한다(`aborted`). 플레인 uvicorn: `SBMIG_WEB_OUT=out/web uvicorn sbmig.web.app:app --port 8765 --timeout-graceful-shutdown 5`.

CLI 명령(`sbmig run`, `sbmig fleet`, `sbmig kb …`)은 개발·시험·진단용.

## 2. PC에 설치 (Windows / macOS / Linux)

| 항목 | 요구 |
|---|---|
| Python | 3.10 이상, `uv` 또는 `pip` |
| JDK | 21. `build.java_home`(회사 yml에서 `"${JAVA21_HOME:-}"`) 또는 `--java-home`. `mvn -v`가 `build.require_java_major`(21)와 다르면 사전 점검이 중단 |
| Maven | 3.9.x. Windows는 `mvn.cmd`를 PATH로 찾는다. 못 찾으면 `build.mvn`에 전체 경로 |
| Nexus | `config/settings.nexus.example.xml` 복사 → 그룹 URL → `build.settings_file`(catch-all 미러). 같은 Nexus를 `build.repository_url`에도 |
| LLM | `.env`(`.env.example` 복사): 게이트웨이 URL, 키 / 앱 토큰, 모델(역할) 이름 |
| git | 시도마다 스냅샷·되돌리기·diff에 쓴다 |

```bash
uv venv && source .venv/bin/activate && uv pip install -e ".[dev]"
cp config/artifacts.example.yml config/artifacts.yml  # 사내 좌표 채우기
cp .env.example .env  # LLM 변수 (git-ignore)
sbmig llm-probe  # 게이트웨이 확인: 마지막 줄 LLM-PROBE ok …
sbmig serve --host 0.0.0.0 --port 8765 --token <토큰>  # 운영: 웹 UI
sbmig run <project.zip> --config config/artifacts.yml  # 진단·개발: CLI 한 번
```

PowerShell: `.venv\Scripts\Activate.ps1`(막히면 `Set-ExecutionPolicy -Scope Process Bypass`), 로그는
`Get-Content out\<run id>\sbmig.log -Encoding UTF8 -Tail 30`(5.1은 `-Encoding UTF8` 없으면 한글이 깨짐). `git pull` 뒤 웹 서버를 다시 띄운다.

## 3. Linux 서버에 서비스로

체크아웃 + venv + `sbmig serve`를 systemd로 돌린다(저장소에 유닛 파일은 없다). 모양: `[Service]`에 `WorkingDirectory=<체크아웃>`,
`EnvironmentFile=<체크아웃>/.env`, `ExecStart=<체크아웃>/.venv/bin/python -m sbmig.cli serve --host 0.0.0.0 --port 8765 --out /var/sbmig/out`, `Restart=on-failure`.

wheel만 설치(site-packages)해서는 동작하지 않는다. `sbmig/core/config.py`는 레포 루트를 자기 위치에서 잡고(`PACKAGE_ROOT` = `sbmig/`의 부모)
`config/`, `knowledge/`는 `sbmig/` 옆에 있어야 한다(wheel에 없음). 체크아웃 위에서 `pip install -e` 또는 `PYTHONPATH`로 돌린다(컨테이너도). `kb.dir`은 재배포에 사라지지 않을 절대 경로로.

## 4. 컨테이너 이미지

`deploy/docker/Dockerfile`: 이미지 하나, Python 3.11 + JDK 21 + Maven + git + curl + 앱 코드. 코드는 `/app`, `PYTHONPATH=/app`
(같은 이유로 site-packages 설치 안 함), 의존성은 `deploy/docker/pip-deps.py`가 `pyproject.toml`에서 읽는다(목록을 두 곳에 두지 않으려고).
`JAVA_HOME`은 `/opt/java21` 심볼릭 링크라 amd64 / arm64 어디서든 맞다. UID 1000, 작업 공간 `/workspace`.

```bash
docker build -f deploy/docker/Dockerfile -t <registry>/sbmig:<tag> .
docker run --rm <registry>/sbmig:<tag> python -m sbmig.cli --help
```

회사 클러스터의 값은 매니페스트에 있다(`deploy/README.md` §1, §3): namespace `agent`, 스토리지 클래스 `nfs-csi`(RWX), 이미지는 인프라팀
레지스트리. PC에서 `deploy/docker/save-image.sh <tag>`가 `linux/amd64`로 빌드해 `out/images/sbmig-<tag>.tar`로 저장하고, 인프라팀이
`docker load` → `docker tag` → `docker push` 한 전체 이름을 `40-deployment.yaml`과 `81-web-job.template.yaml`의 `image:`에 같은 값으로 넣는다
(`imagePullPolicy: Always`라 같은 태그도 반영된다; pull 인증은 `imagePullSecrets`). 바꾼 뒤 할 일: 이미지만 → 빌드·push, 웹 파드 재시작(빌드 파드는
뜰 때 받는다); 20 → apply, 재시작; 40 → apply; 81(이미지에 들어가니 빌드하는 체크아웃에서 채운다) → 이미지 빌드·push, 재시작. push와 재시작 사이에 올린 실행은 거절된다(§5 ②). 빌드 식별값 `sbmig version` = `0.1.0+<12자>`(`sbmig/**/*.py`,
`config/*.yml`, `knowledge/**`의 지문, 빌드 인자 없음); 빌드 파드 쪽은 저널 `worker_started`의 `sbmig_version`. 레지스트리를 못 쓰면 tar를 노드마다 적재하고(containerd는
`ctr -n k8s.io images import`, k3s는 `k3s ctr images import`, docker는 `docker load`) 이미지를 `sbmig:<tag>` + `imagePullPolicy: Never`로 바꾼다;
노드 하나에만 적재했으면 Deployment와 Job 틀의 `nodeSelector: {kubernetes.io/hostname: <node>}`를 푼다(이미지가 없는 노드에서는 `ErrImageNeverPull`).

회사 설정 `config/artifacts.yml`은 이미지에 굽는다: git 무시 파일이라 빌드에 `--build-arg REQUIRE_CONFIG=artifacts.yml`을 붙여 없으면 빌드를
멈춘다(`/app/config/artifacts.yml`, UI 설정 목록에 나옴). PC 경로가 든 키는 매니페스트 `env`가 덮어쓴다: `SBMIG_KB__DIR=/data/kb-v2`(웹 파드),
`SBMIG_BUILD__SETTINGS_FILE=/data/.m2/settings.xml`, `SBMIG_BUILD__JAVA_HOME=/opt/java21`, `SBMIG_BUILD__MVN=mvn`, `SBMIG_BUILD__LOCAL_REPO=/data/.m2/repository`,
`SBMIG_SCRIPT__PYTHON=null`, 웹 파드의 `SBMIG_LLM__RECORD_DIR=${LLM_RECORD_DIR:-}`(`tests/deploy/test_manifests.py`가 세 매니페스트에서 지킨다).
설정을 바꾸려면 이미지를 다시 만든다(숫자 손잡이는 `sbmig-env`). 이미지 밖: ConfigMap `deploy/k8s/20-configmap.yaml`(`sbmig-config` = settings.xml 원본,
`/data/.m2`로 한 번 복사; `sbmig-env` = 환경 변수), Secret 견본 `deploy/k8s/30-secret.example.yaml`(→ `deploy/k8s/30-secret.yaml`, git-ignore): `sbmig-llm`
(= PC의 `.env` + `SBMIG_WEB_TOKEN`), `sbmig-git`. 자원: OpenRewrite `openrewrite.jvm_args`(`-Xmx4g`) 때문에 메모리 6~8 GiB, CPU 2~4.

## 5. 쿠버네티스의 두 모양

### ① 웹 UI Deployment — 이관을 파드 안에서 (지금 동작)

환경 변수는 ConfigMap `sbmig-env`(비밀 아닌 것: `SBMIG_WEB_*` / `SBMIG_MAX_UPLOAD_MB`, LLM 게이트웨이 주소·에셋 id·role 이름, 설정 덮어쓰기
`SBMIG_<섹션>__<키>`, 예 `SBMIG_SCRIPT__MAX_LLM_TRIES_PER_CASE`)와 Secret `sbmig-llm`(토큰, `NEXUS_USER` / `NEXUS_PASSWORD`, 조치용 `IVAS_*` 세 값, 11)에
둔다. 웹 파드와 `80`의 Job은 `envFrom`으로 읽고, 웹 파드가 만드는 Job(`81` 틀)은 키마다 `configMapKeyRef` / `secretKeyRef` 한 줄이다. `sbmig-env`에
새 키를 더하면 `81`에도 한 줄(같은 테스트가 본다). 전체 표는 `deploy/ENV.md`, `deploy/README.md` "env 목록". 모르는 키는 시작 로그 `config: … is not a known key`.

`deploy/k8s/10-storage.yaml`(PVC 하나: `/data` = 업로드·실행·KB·LLM 기록·`.m2`), `20-configmap`, `30-secret`, `deploy/k8s/40-deployment.yaml`
(replica 1, Recreate: 실행 큐가 프로세스 안에 있고 두 번째 웹 파드는 감지되지 않으니 바꾸지 않는다; `tcpSocket` 프로브: 인증 없는 health 경로가 없음),
`deploy/k8s/50-service.yaml`.

```bash
kubectl apply -f deploy/k8s/10-storage.yaml -f deploy/k8s/20-configmap.yaml -f deploy/k8s/30-secret.yaml
kubectl apply -f deploy/k8s/40-deployment.yaml -f deploy/k8s/50-service.yaml
kubectl exec deploy/sbmig -- python -m sbmig.cli llm-probe
```

### ② Job 디스패치 모드 — 웹 파드가 "Agent", 업로드마다 빌드 Job

`sbmig serve --mode k8s-job`(또는 `sbmig-env`의 `SBMIG_WEB_MODE=k8s-job`: 켜진 채로 나가고, 주석
처리하면 ①): 업로드된 zip을 자기 프로세스에서 돌리는 대신 Job을 만든다.
전달·결과·KB는 전부 공유 PVC(`sbmig-data`, RWX, 두 파드가 `/data`에 마운트): 웹(Main)이 `/data/out/<run id>/source.zip`,
`control/request.json`(접수 때의 zip sha256·설정 digest·sbmig 버전), `control/state.json`(제어 상태; Job을 만드는 Main이 `creating`과 함께 설정
digest와 sbmig 버전을 다시 적는다)을 쓰고, 틀 `deploy/k8s/81-web-job.template.yaml`에 실행별 env(`SBMIG_SOURCE_ZIP`, `SBMIG_OUT`, `SBMIG_RUN_ID`,
`SBMIG_CONFIG`, `SBMIG_FLOW`, API의 `SBMIG_STOP_AFTER` / `SBMIG_LLM_PROVIDER`)와 라벨(`sbmig-run-id`, `sbmig-owner`)을 채워 Job을 만든다(먼저
`creating`을 적어 응답이 끊겨도 같은 이름의 Job을 이어받는다) → 파드의 `python -m sbmig.web.worker`가 zip은 request.json
과, 설정 digest·버전은 state.json의 값(없는 옛 파일이면 request.json)과 대조하고(다르면 exit 3, 저널 `worker_refused`) `sbmig run --out /data/out
--run-id <run id>`로 같은 실행 폴더에 저널·로그·보고서·`result.zip`을 쓰고, 비밀값을 지우고(`sbmig/web/redact.py`) `job-status.json
{exit_code, run_id, attempt}`를 쓴다 → 웹은 그 폴더를 tail해 SSE로 보낸다(`graph_node_end` → 화면 `node`, 레일 앞 '파드' 단계: 결과 파일이 있으면
종료 코드 3이어도 `completed`, 결과 없이 죽었거나 Worker가 거부했을 때만 `failed`) → Main은 지금 attempt의 상태 파일만 받는다(없으면 `verified:
false`). 멈춤 = 웹이 `<run dir>/stop.request`를 쓰고 엔진이 노드 사이에서 멈춘다(종료 3); `SBMIG_JOB_STOP_GRACE_S`
뒤에도 돌면 자기 라벨의 Job만 지우고 사라질 때까지 기다린다. 파드가 시작 전(Creating / Pending)이면 바로 지운다(못 뜨는 이유: [10](10-open-items-and-history.md)).
웹 파드 재시작 때 Job은 그대로 돌고, 다시 뜬 Main이 `control/state.json`으로 `queued`는 다시 줄에 세우고 `created` / `running`은 이어서 감시한다.
Job은 자리 번호(`SBMIG_JOB_SLOT`)를 받고 2번부터 Maven 저장소 `repository-slot<k>`를 따로 쓴다. KB 쓰기는 `<kb>/.lock`으로 차례. 쿠버네티스 API는
`sbmig/web/k8s.py`(requests, `kubernetes` 패키지 없음)로 Job 생성·상태·삭제와 권한 자기 점검(SelfSubjectAccessReview)만 부른다. 인증은 `KUBECONFIG`
의 kubeconfig(인프라팀 Secret `/etc/kube/config`; token / 클라이언트 인증서, `exec` 거절)가 있으면 그것, 없으면 서비스 계정(권한 `deploy/k8s/45-web-rbac.yaml`).
매니페스트는 kubeconfig 쪽이 켜진 채로 나간다(`sbmig-env`의 `SBMIG_K8S_NAMESPACE`가 Job의 namespace). Job 틀은 이미지 안의 사본
(`/app/deploy/k8s/81-web-job.template.yaml`; 대안은 ConfigMap 마운트 + `SBMIG_JOB_TEMPLATE`)이고, Main은 시작할 때 틀(`CHANGE_ME`
남은 값 없음(주석 제외), 컨테이너 하나, envFrom 없음, 비밀 env는 valueFrom(`SBMIG_<섹션>__<키>`는 비밀 아님), `/data` 마운트 하나)과 권한을
검증해 어긋나면 뜨지 않는다. 켜는 순서는 `deploy/README.md` §3 "켜는 법".
개발 PC: `k8s-job`인데 클러스터 밖이면 `local-job`(`sbmig/web/localjob.py`, 파드 자리에 로컬 서브프로세스 `sbmig run --run-id …`; `--mode local-job`
으로 직접 고를 수도 있다)으로 내려간다. 로컬 Worker에는 틀의 env 중 실행별 값(`RUN_ENV`)만 넘기고 나머지는 PC의 셸·`.env`가 정한다.

조각:

| 파일 | 하는 일 |
|---|---|
| `sbmig/web/runs.py` | `RunManager(mode=…)`: `_start_job`(실행 폴더 + source.zip + 저널 첫 줄) → `_run_job`(Job 생성·감시·멈춤·종료 코드), `status()`의 `mode` / `job`, `events()`의 `node` 변환 |
| `sbmig/web/k8s.py` | `K8sClient.in_cluster()`, `create_job` / `job_state` / `delete_job`, `render_job`, `job_name` |
| `sbmig/graph/engine.py` | 노드마다 `graph_node_end` 저널, `stop.request` 검사(`user_stop`) |
| `deploy/docker/run-job.sh` | 공유 볼륨 모드(`SBMIG_SOURCE_ZIP`, `SBMIG_OUT` / `SBMIG_RUN_ID`, `SBMIG_KB_DIR`); git 모드(`SBMIG_SOURCE_GIT_URL` / `SBMIG_SOURCE_ZIP_URL`, KB·결과 push) |
| `deploy/k8s/81-web-job.template.yaml` | 웹이 채우는 Job 틀. `80-build-job.template.yaml`은 git 모델(인프라팀 수동 제출용, 시험 도구 `deploy/k8s/submit-job.sh`) |
| `deploy/k8s/45-web-rbac.yaml` | 웹 파드의 최소 권한(jobs create / get / list / watch / delete, pods get / list / watch, pods/log get, events list: 없어도 됨) |

Job 모드가 지키는 넷(코드: `sbmig/web/runs.py`, `sbmig/web/worker.py`, `sbmig/web/contract.py`, `sbmig/web/redact.py`, `sbmig/core/scrub.py`)

1. 차례는 Job이 끝난 것을 확인한 뒤 넘어간다: `RunManager._run_job`이 돌아올 때 넘어가고, `_watch_job`은 `job-status.json`을 받아도
   `_hold_until_ended`로 Job이 `Succeeded` / `Failed` / `Missing`인 것을 API로 확인한 뒤 돌아온다. 결과 뒤 남은 파드는 `SBMIG_JOB_EXIT_GRACE_S`(120초)
   뒤 지우고 사라질 때까지 기다린다. 끝난 Job과 파드는 틀의 `ttlSecondsAfterFinished`(1시간) 뒤 쿠버네티스가 지운다. 웹 쪽 예외도 만든 Job을 지우고 확인한다.
   API 장애(`_poll_job`이 None)는 실패가 아니다: `SBMIG_JOB_API_GRACE_S`(300초) 뒤 알리고(로그 ERROR, 저널 `job_api_unavailable`, `status()`의 `job.note`)
   `API_REMIND_S`마다 다시, 돌아오면 `job_api_back`. 영구 장애는 큐를 멈춘다. 불확실한 생성 응답(409 / 전송 오류)은 같은 이름을 `CREATE_CONFIRM_POLLS`
   번 찾는다. 예외는 둘: 서버 종료(`_ServerClosing`, `keep_state`: 다음 Main이 이어받음)와 우리 것이 아닌 Job.
2. 소유는 라벨 둘 다: `_owned`: `sbmig-owner`(요청마다 난수)와 `sbmig-run-id`가 둘 다 맞아야 우리 Job이다. 라벨이 없는 Job은 남의 것:
   이어받지도 지우지도 않고 실행은 오류로 끝난다(운영자가 손으로 지운다). `JobState.labels is None`(`sbmig/web/localjob.py`, 테스트 가짜)일 때만 이름을 믿는다.
3. 환경 대조: zip·설정 파일·버전이 같아도 환경 변수가 다르면 다른 설정으로 돈다. Job을 만드는 Main이
   `creating` 전이와 함께 `control/state.json`에 `env_pins`(`env_pins()`: 설정 파일의 `${VAR}` 이름 + 그 환경의 `SBMIG_<섹션>__<키>`·섹션째 바꾸는 `SBMIG_<섹션>` + 별칭
   `SBMIG_KB__DIR` ↔ `SBMIG_KB_DIR` → 값 sha256 앞 16자; `is_secret_name` 이름(`LLM_TOKEN_URL` 포함), `ENV_PIN_SKIP`, 프록시 변수는 빠짐)와
   `env_pin_policy`를 적는다. 정책 = 웹 파드의 `SBMIG_JOB_ENV_PIN`(시작 때 한 번): `refuse`(k8s-job 기본) / `warn` / `off`(local-job 기본). Worker는
   `verify_request` 뒤 자기 환경으로 다시 계산해 `compare_env_pins`(`differs` / `main_only` / `worker_only`, 이름만)를 보고, 실행별 값(`SBMIG_CONFIG`의
   파일, `SBMIG_STOP_AFTER`, `SBMIG_LLM_PROVIDER`)을 `request.json`과 맞춘다. `refuse` → exit 3, 저널 `worker_refused`, `job-status.json`의 `error`에
   키 이름과 재시작 안내; `warn` → 저널 `worker_env_differs`, Worker의 값으로 돈다. `env_pins`가 없으면(옛 Main, `off`) 보지 않는다.
   구별: digest 거절은 zip sha256 또는 `config <name> differs between the web pod and the build pod` / `sbmig version differs: web pod <v>, build pod <v>`
   + `the web pod and the build pod run different images: …`(화면 「웹 파드와 빌드 파드가 서로 다른 이미지로 …」), 환경 거절은 변수 이름과 `SBMIG_JOB_ENV_PIN`을
   말한다. 재시작 전부터 줄에 있던 실행, 옛 이미지 실행의 조치 재개는 지금 두 파드가 같으면 돈다. 운영 규칙은 §4.
   `main_only`의 키는 대개 81 틀에 그 줄이 없다는 뜻이다.
4. 쓰는 순간 정제: `SBMIG_REDACT_OUTPUT=1`(40·80의 literal; Worker는 `child_environ`으로 자식에 늘 켠다)이면 진입점(CLI 콜백의
   `install_redaction_if_requested`, 웹의 `_ensure_console_logging`)이 `install_output_redaction`으로 `scrub.py`에 `Redactor`를 건다. 지나는 곳:
   `StageFormatter`, `Workspace.journal`, Main의 `_append_journal`과 SSE(`_push`), 잡히지 않은 예외(`install_scrubbed_excepthooks`; rich traceback 끔).
   지우는 값: `secret_values`(비밀 이름의 값 6자 이상, `IVAS_USER_ID`는 4자부터; 6자 미만은 낱말 하나일 때만)와 그 표기(`expand_values`: JSON·퍼센트
   인코딩·repr·줄마다), URL `user:pass@`, 실행 중 받은 토큰(`note_secret`). Fail closed: 실패한 줄은 `WITHHELD`(저널 `output_withheld`), 걸 수 없으면
   CLI exit 3·웹은 안 뜬다. Worker는 자식의 파이프도 `Redactor.line`으로 지운다. 마지막 `redact_run_dir`(Worker; `sbmig run`의 `final_redaction`; Main —
   정제가 걸린 in-process 실행과 결과 없이 끝난 Job)만 보고서·`logs/`에 닿는다(`.json`은 문자열 값 안만, `.jsonl`은 줄마다: 키·숫자·모양은 그대로).
   그래서 끝나기 전에는 `LIVE_FILES`(`sbmig.log`, `journal.jsonl`)만 내주고 나머지는 HTTP 409(`file_withheld`). 남은 한계는 [10](10-open-items-and-history.md).

다음: 클러스터 실측, 외부 접수 API(`docs/v2/API-MIGRATIONS-KO.md`). Main ↔ Job 파드의 파일·id·종료 코드·멈춤·복구 약속: `docs/v2/JOB-RUN-STATE-CONTRACT-KO.md`.

`run-job.sh` 종료 코드: 보고서가 나왔으면 0(sbmig 0 / 2), sbmig 중단(exit 3)이면 3, 결과를 못 내보냈으면 4. `SBMIG_FAIL_JOB_ON_BUILD_FAIL=1`이면 빌드 실패를 2로.

인프라와 정할 것: 네트워크(빌드 노드 → 웹 파드·LLM 게이트웨이·Nexus·git), 결과 보관 기간, 소스 저장소 브랜치 push 여부.

`build.setup_command` / `build.teardown_command`는 실행 전후 한 번씩 도는 일반 훅이다(두 모양 모두 비움).

## 6. 일괄 실행 (fleet) — 개발·대량 시험용

`sbmig fleet run <manifest> --config config/artifacts.yml --out out/fleet`가 프로젝트를 하나씩 차례로, KB 하나로 돌린다.
매니페스트: 디렉토리(`*.zip`과 pom이 있는 하위 폴더), `.txt`(줄마다 경로), `.yml`(`projects: [{name, input, configs, overrides, tags}]`),
`.csv`(`name,input,tags`). 예: `fixtures/fleet/manifest.example.yml`. 프로젝트별 `overrides`는 겹쳐지지만 `kb.dir`은 언제나 플릿 공통이다.

| 명령 | 하는 일 |
|---|---|
| `sbmig fleet run` | 실행(기본은 이어서, 성공한 것은 건너뜀). `--dry-run`은 순서와 재개 결정만 |
| `sbmig fleet resume <out>` | 같은 매니페스트·설정(`fleet-config.json`)으로 멈춘 곳부터. `--retry-failed`면 incomplete / aborted도 |
| `sbmig fleet status <out>` | 진행 표(실행 중에도). `--attention`은 사람이 볼 프로젝트만 주의 코드별로 |

남는 것(`<out>/fleet/`): `state.json`, `summary.md` / `summary.csv`(UTF-8 BOM, `attention` 열) / `summary.json`, `learning.md`(KB가 배운 것),
`fleet-config.json`, `sbmig.log`(프로젝트마다 `SBMIG-RESULT`). 프로젝트마다 실행 디렉토리 `out/<fleet-id>-<NNN>-<name>/`. 코드는 `sbmig/fleet/`(`runner.py`, `manifest.py`, `preflight.py`, `cleanup.py`, `summary.py`).

인프라 보호: 첫 프로젝트 전 사전 점검(`fleet.preflight`: disk / maven / java / settings / repository / llm, `preflight: <check> ok|FAIL|skip`);
인프라 이유로 끝난 프로젝트는 `pending`으로 되돌리고, `fleet.infra_failure_limit`(3)번 연속이면 `fleet.infra_retry_minutes`(15) 기다렸다 다시 점검,
`fleet.infra_max_waits`(4)번 뒤 멈춘다(다시 실행하면 이어감). 프로젝트 전 여유 공간 `fleet.min_free_gb`(웹 업로드의 하한이기도 하다), 끝난 뒤 정리
`fleet.cleanup`(`build-output` = `target/` 삭제, `workspace` = 프로젝트 복사본도, `none`). 명령줄·매니페스트의 값은 `SBMIG_<섹션>__<키>` 환경 값보다
이긴다(바꿀 때마다 INFO `… given explicitly replaces <변수> from the environment`).
종료 코드: 0 전부 성공, 2 일부 미완, 3 중단(`--stop-on-abort` 또는 인프라).

## 7. KB 운영 (PC 사이 옮기기)

KB는 `kb.dir` 아래 파일뿐이다: `errors.jsonl`, `cases.md`, `cases/<case_id>/case.json` + `fix_v<N>.py`, `.lock`(프로세스 간 잠금, `kb.lock_timeout_seconds`).

| 명령 | 하는 일 |
|---|---|
| `sbmig kb export <OUT.zip>` | KB를 zip 하나로 |
| `sbmig kb import <zip>` | 가져오기(기본은 합치기, `--replace`는 통째 교체하고 예전 KB를 `.bak-*`로). 스크립트는 들어온 파일 이름과 상관없이 케이스 폴더의 `fix_v<N>.py`로 쓴다 |
| `sbmig kb validate` | 파일 점검(문제 있으면 exit 1) |
| `sbmig kb stats` | 항목·케이스·상태별 스크립트 버전 수 |
| `sbmig kb reindex` | `errors.jsonl` 로부터 케이스 수치·실행 기록을 다시 셈(`git pull` 뒤 한 번 권장) |
| `sbmig kb cases list` / `sbmig kb cases show <case_id>` | 케이스와 저장 스크립트(계보·실행 기록). `retire` / `activate` / `pin` / `unpin`은 사람의 결정 |

모든 `sbmig kb` 명령은 `--kb-dir`로 `kb.dir`을 덮어쓴다.

## 8. 디스크 정리 `sbmig clean`

끝난 실행의 프로젝트 복사본, 웹 UI 업로드 zip, 남은 `.part` 업로드, LLM 기록(`llm-*.json`)을 지운다. 실행·대기 중인 실행과 그 업로드는 건드리지 않는다
(업로드 이름의 실행 id `<프로젝트명>-YYYYMMDD-HHMMSS-<hex>`로 짝짓는다; `--keep-failed`면 실패한 실행의 업로드도 남김). 손으로만 돈다. 실행 폴더, `source.zip`, `result.zip`은 지우지 않는다.
옵션: `--out`(웹이면 `out/web`), `--fleet`, `--older-than <일>`, `--keep-failed`, `--dry-run`(먼저 이것으로), `--records`.

## 9. 로그 파일

- `out/<run id>/sbmig.log`: 실행 하나의 DEBUG 로그(웹도 실행마다), 끝에 `SBMIG-RESULT`. `journal.jsonl`: 단계·결정 이벤트. `logs/`: Maven 로그(`NNN-build-*.log`), 구조 검토 입력 등.
- `<fleet out>/fleet/sbmig.log`: 플릿 INFO 로그(사전 점검, `SBMIG-RESULT`, 인프라 대기). `--log-file <path>`: 어떤 명령이든 DEBUG 로그를 덧붙임.
- `llm.record_dir`(`LLM_RECORD_DIR`): LLM 요청·응답 `llm-NNNN.json`(비밀값 없음).

## 10. 문제를 보고할 때 보내는 것

회사 PC는 복사·붙여넣기가 안 되는 Windows다. 파일 통째가 아닌 특정 줄을 PowerShell 명령과 함께 요청한다.

1. 결과 한 줄(가장 먼저):
   `SBMIG-RESULT run=<id> exit=<n> build=<ok|FAIL> unresolved=<n> blocked=<n> llm=<calls> attn=<codes|->`
   (`Select-String -Path out\<run id>\sbmig.log -Pattern "SBMIG-RESULT" -Encoding UTF8`)
2. 사전 점검 실패: `preflight: <check> FAIL …` 줄.
3. 원인 줄: `cause=` 줄(게이트웨이·사전 점검 실패), 「확인해 주세요」 첫 항목, 「해결하지 못한 오류」의 파일·줄(케이스 id는 `errors.md`).
4. 케이스 id로 KB에서: `sbmig kb cases show <case_id>`, `sbmig kb errors show <id>`.
5. 빌드 오류 원문: `logs\NNN-build-*.log`의 `[ERROR]` 줄 몇 개.
6. 어느 단계가 어떤 줄을 썼나: `git -C out\<run id>\project log --oneline` (첫 커밋은 as-is 스냅샷), `git log -S "<문자열>" -- pom.xml`.

주의 코드(`attn=`) 예: `infra:<kind>`, `crash`, `aborted[:<reason>]`, `blocked:<reason>`, `unresolved:<n>`, `restructure-failed`,
`hollow-reactor`, `parent-chain-no-boot-bom`, `tests-not-compiled`. 정의는 `sbmig/report/result_line.py`.

| 보이는 것 | 볼 곳과 할 일 |
|---|---|
| 「웹 파드와 빌드 파드가 서로 다른 이미지로 …」 | 저널 `worker_refused`. push 뒤 웹 파드 재시작이 빠졌다 → 재시작, 다시 올림(§4) |
| 웹 파드 로그 `MIGRATION_TEMPLATE_INVALID: … CHANGE_ME` | 이미지의 81이 비었다 → 채워 빌드·push, 재시작(§4) |
| `run aborted: ZIP refused: …`(exit 3) | 저널 `intake_refused`, 걸린 `intake.*` 키가 문장에 있다(§1) |
| 업로드가 HTTP 507 | 출력 볼륨 여유 < `fleet.min_free_gb` → 정리(§8) 또는 볼륨 확장 |
| HTTP 403 `cross-site request refused: …` | 다른 사이트의 페이지가 보낸 요청(§1) |

## 확인하는 법

```bash
.venv/bin/python -m sbmig.cli serve --help  # --host / --token / --max-upload-mb / --graceful-timeout
sed -n 1,40p sbmig/web/app.py  # 라우트, 토큰 규칙
grep -n "def _hold_until_ended\|def _owned\|def _poll_job\|def file_withheld\|GRACE_ENV\|API_GRACE" sbmig/web/runs.py
grep -n "ENV_PIN\|def env_pins\|def compare_env_pins\|RUN_ENV =" sbmig/web/contract.py
grep -n "REDACT_OUTPUT_ENV\|WITHHELD" sbmig/core/scrub.py; grep -n "def install_output_redaction\|def expand_values" sbmig/web/redact.py
.venv/bin/python -m sbmig.cli clean --help
grep -n "PACKAGE_ROOT\s*=" sbmig/core/config.py  # 레포 루트를 자기 위치에서 잡는 줄
grep -n "CROSS_SITE_MESSAGE\|def sbmig_version\|IMAGE_DIFFERS" sbmig/web/app.py sbmig/web/contract.py
```
