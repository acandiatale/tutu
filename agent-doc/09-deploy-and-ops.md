# 배포와 운영

> 다루는 것: sbmig 를 실제로 돌리는 법 — 회사의 운영 방식(웹 UI 에 zip 업로드), PC·Linux 설치, 컨테이너 이미지와 쿠버네티스 두 모양, 일괄 실행(fleet), PC 사이의 KB 옮기기, 디스크 정리, 로그 파일, 문제를 보고할 때 보낼 줄.
> 언제 읽나: sbmig 를 새 PC / 서버 / 클러스터에 올릴 때, 운영 중 무엇을 보고 무엇을 보내야 할지 모를 때.
> 함께 읽을 파일: [../deploy/README.md](../deploy/README.md), [07-config-and-knowledge.md](07-config-and-knowledge.md), [03-run-debug-and-logs.md](03-run-debug-and-logs.md), [10-open-items-and-history.md](10-open-items-and-history.md)

## 1. 회사가 sbmig 를 쓰는 방식

**운영 모델은 웹 UI 다.** 에이전트(sbmig)는 웹 앱(`sbmig serve`)으로 떠 있고, 이관할 프로젝트의 **담당자가 자기 프로젝트를 zip 으로
UI 에 올리면** 에이전트가 이관을 돌린다. 담당자는 sbmig 를 직접 실행하지 않는다.

```
담당자 브라우저 ──zip 업로드──▶ sbmig serve (FastAPI + uvicorn, 워커 1)
                                 │  실행마다 작업 스레드 하나, LangGraph 그래프 (intake → … → finalize)
                                 │  진행: SSE 로 node / journal / log / report 이벤트
담당자 브라우저 ◀──결과 내려받기── report.md, result.zip, errors.md, sbmig.log …
```

- 화면: zip 을 끌어다 놓거나 파일 선택 → `마이그레이션 시작` → 단계 레일(접수 → 분석 → 계획 → 단계들 → 마무리)과 진행 기록 / 로그 탭 →
  끝나면 `보고서 받기(report.md)` / `변환된 프로젝트 받기(result.zip)` 버튼. **설정 파일은 서버가 정한다**: `sbmig serve --config <이름>`
  (여러 번) 또는 `SBMIG_WEB_CONFIGS=a.yml,b.yml`, 없으면 `config/artifacts.yml` → `artifacts.local-e2e.yml` 중 있는 것(`GET /api/configs` 의
  `default`). 담당자는 고르지 않고, 개발자는 "고급 설정"을 펼쳐 설정 파일 / LLM(fake) / 여기까지만 실행을 바꾼다. 로그 탭은 마지막 1,500 줄만
  그린다(전체는 `sbmig.log` 링크). 화면은 `sbmig/web/static/index.html` 한 파일(HTML + CSS + JS, 웹 폰트·CDN 없음 — 사내망).
- **한 번에 하나의 이관.** 그 사이 올라온 zip 은 `queued` 로 기다린다(파일 KB 를 공유하기 때문, 순차 이관이 운영 모델).
- **중지**는 그래프 노드 사이에서만 먹힌다(지금 노드가 끝난 뒤).
- **접근 제어**: 사내망 전용 페이지라 기본 배포는 `SBMIG_WEB_OPEN=1`(또는 `--open`)로 토큰 없이 모든 클라이언트를 받는다 — 시작 때
  `open to the network WITHOUT a token` 경고 한 줄. 토큰을 두려면 `SBMIG_WEB_TOKEN`(또는 `--token`): 결과 zip 에 회사 소스가 들어가므로
  토큰이 있으면 모든 요청에 요구하고(open 보다 우선), 브라우저는 `http://<host>:8765/?token=<토큰>` 로 한 번 열면 HttpOnly 쿠키가 남는다.
  둘 다 없으면 이 PC(루프백)만 받고, `--host 0.0.0.0` 이면 `sbmig serve` 가 뜨지 않는다.
- **업로드 상한**: `--max-upload-mb`, 기본 `$SBMIG_MAX_UPLOAD_MB` 또는 512 MB, 0 = 없음.
- `sbmig serve` 가 곧 uvicorn 이다(안에서 `uvicorn.run`). 실행 큐가 프로세스 안에 있으므로 **워커는 하나**여야 한다.
  종료 시 `--graceful-timeout`(기본 5초) 뒤 SSE 연결을 닫고, 실행 중인 Maven / OpenRewrite 프로세스를 정리한다. 중단된 실행은 `aborted`.
- 플레인 uvicorn 으로도 뜬다: `SBMIG_WEB_OUT=out/web uvicorn sbmig.web.app:app --port 8765` (이때 `--timeout-graceful-shutdown 5` 를 직접).

**CLI 명령(`sbmig run`, `sbmig fleet`, `sbmig kb …`)은 개발자·테스트·진단용이다.** 운영 경로는 위의 웹 UI 다.

## 2. PC 에 설치 (Windows / macOS / Linux)

| 항목 | 요구 |
|---|---|
| Python | 3.10 이상, `uv` 또는 `pip` |
| JDK | **21**. `build.java_home` 에 주거나(회사 yml 에서 `"${JAVA21_HOME:-}"` 처럼 참조) `--java-home`. `build.require_java_major`(21)와 `mvn -v` 가 다르면 사전 점검이 중단 |
| Maven | 3.9.x. Windows 는 `mvn.cmd` 를 PATH 로 찾는다 — 못 찾으면 `build.mvn` 에 전체 경로 |
| Nexus | `config/settings.nexus.example.xml` 을 복사해 그룹 URL 을 넣고 `build.settings_file` 로 가리킨다(catch-all 미러). 같은 Nexus 를 `build.repository_url` 에도 |
| LLM | `.env`(`.env.example` 복사): 게이트웨이 URL, 키 / 앱 토큰, 모델(역할) 이름 |
| git | 시도마다 스냅샷 / 되돌리기 / diff 에 쓴다 |

```bash
uv venv && source .venv/bin/activate && uv pip install -e ".[dev]"
cp config/artifacts.example.yml config/artifacts.yml     # 사내 좌표 채우기
cp .env.example .env                                     # LLM 변수 (git-ignore)
sbmig llm-probe                                          # 게이트웨이 확인: 마지막 줄 LLM-PROBE ok …
sbmig serve --host 0.0.0.0 --port 8765 --token <토큰>    # 운영: 웹 UI
sbmig run <project.zip> --config config/artifacts.yml    # 진단·개발: CLI 한 번
```

Windows PowerShell 에서는 `.venv\Scripts\Activate.ps1`(막히면 `Set-ExecutionPolicy -Scope Process Bypass`), 로그를 볼 때
`Get-Content out\<run id>\sbmig.log -Encoding UTF8 -Tail 30` (PowerShell 5.1 은 `-Encoding UTF8` 이 없으면 한글이 깨진다).
`git pull` 뒤에는 웹 서버를 다시 띄운다.

## 3. Linux 서버에 서비스로

체크아웃 + venv + `sbmig serve` 를 systemd 로 돌린다. 저장소에 유닛 파일은 없다 — 아래는 모양 예시다.

```
[Service]
WorkingDirectory=/opt/sbmig/migration-agent
EnvironmentFile=/opt/sbmig/migration-agent/.env
ExecStart=/opt/sbmig/migration-agent/.venv/bin/python -m sbmig.cli serve --host 0.0.0.0 --port 8765 --out /var/sbmig/out
Restart=on-failure
```

**wheel 만 설치(site-packages)해서는 동작하지 않는다.** `sbmig/core/config.py` 는 레포 루트를 자기 파일 위치에서 잡는다
(`PACKAGE_ROOT` = `sbmig/` 의 부모). `config/`, `knowledge/` 는 `sbmig/` 옆에 있어야 하고 wheel 에는 들어가지 않는다. 그래서 항상
체크아웃 위에서 `pip install -e` 또는 `PYTHONPATH` 로 돌린다(컨테이너도 같다). `kb.dir` 은 서버 재배포에 사라지지 않을 곳(절대 경로)으로.

## 4. 컨테이너 이미지

`deploy/docker/Dockerfile` — **이미지 하나**: Python 3.11 + JDK 21 + Maven + git + curl + 앱 코드. 코드는 `/app`, `PYTHONPATH=/app`
(같은 이유로 site-packages 설치 안 함), 의존성은 `deploy/docker/pip-deps.py` 가 `pyproject.toml` 에서 읽는다(목록을 두 곳에 두지 않으려고).
`JAVA_HOME` 은 `/opt/java21` 심볼릭 링크라 amd64 / arm64 어디서든 맞다. UID 1000, 작업 공간 `/workspace`.

```bash
docker build -f deploy/docker/Dockerfile -t <registry>/sbmig:<tag> .
docker run --rm <registry>/sbmig:<tag> python -m sbmig.cli --help
```

설정과 비밀값은 이미지 밖: ConfigMap `deploy/k8s/20-configmap.yaml`(회사 yml 이 `/app/config/company.yml` 로 들어가 UI 의 설정 목록에 나옴,
컨테이너 경로 — `kb.dir: /data/kb-v2`, `build.settings_file: /etc/sbmig/settings.xml`, `build.repository_url`, `llm.record_dir` — + settings.xml),
Secret 견본 `deploy/k8s/30-secret.example.yaml`(복사해 `deploy/k8s/30-secret.yaml` 로 채움, git-ignore): `sbmig-llm`(= PC 의 `.env` + `SBMIG_WEB_TOKEN`),
`sbmig-git`(저장소 토큰). 자원: OpenRewrite 가 `openrewrite.jvm_args`(`-Xmx4g`)로 돌므로 메모리 6~8 GiB, CPU 2~4.

## 5. 쿠버네티스의 두 모양

### ① 웹 UI Deployment — 이관을 파드 안에서 (지금 동작, 새 코드 없음)

환경 변수는 ConfigMap `sbmig-env`(비밀 아닌 것: 웹 파드 동작 `SBMIG_WEB_*` / `SBMIG_MAX_UPLOAD_MB`, LLM 게이트웨이 주소·에셋 id·role
이름, 설정 덮어쓰기 `SBMIG_<섹션>__<키>` — 예 `SBMIG_SCRIPT__MAX_LLM_TRIES_PER_CASE`, `SBMIG_LLM__CASE_THREAD`)와 Secret `sbmig-llm`(토큰,
`NEXUS_USER` / `NEXUS_PASSWORD`)에 나눠 둔다. 전체 표는 `deploy/ENV.md`(인프라팀 전달용) 와 `deploy/README.md` 의 "env 목록" 절. 모르는 키 이름은 시작 로그에
`config: … is not a known key` 로 뜬다.

`deploy/k8s/10-storage.yaml`(PVC 둘: `/data` = 업로드·실행·KB·LLM 기록, `~/.m2`), `20-configmap`, `30-secret`, `deploy/k8s/40-deployment.yaml`
(replica 1, Recreate — 실행 큐가 프로세스 안에 있으므로 둘 이상 띄우지 않는다, `tcpSocket` 프로브 — 인증 없는 health 경로가 없음),
`deploy/k8s/50-service.yaml`. PC 의 웹 UI 와 똑같이 담당자가 zip 을 올리고 결과를 받는다. KB 는 PVC 의 `/data/kb-v2` 에 남는다.

```bash
kubectl apply -f deploy/k8s/10-storage.yaml -f deploy/k8s/20-configmap.yaml -f deploy/k8s/30-secret.yaml
kubectl apply -f deploy/k8s/40-deployment.yaml -f deploy/k8s/50-service.yaml
kubectl exec deploy/sbmig -- python -m sbmig.cli llm-probe
```

### ② Job 디스패치 모드 — 웹 파드가 "Agent", 업로드마다 빌드 Job (있음, 2026-11-05)

`sbmig serve --mode k8s-job`(또는 ConfigMap 의 `SBMIG_WEB_MODE=k8s-job`): 업로드된 zip 을 자기 프로세스에서 돌리는 대신 Job 을 만든다.
**전달·결과·KB 는 전부 공유 PVC**(`sbmig-data`, ReadWriteMany, 두 파드가 `/data` 에 마운트): 웹이 `/data/out/<run id>/source.zip` 을 두고
`deploy/k8s/81-web-job.template.yaml` 에 실행별 env(`SBMIG_SOURCE_ZIP`, `SBMIG_OUT`, `SBMIG_RUN_ID`, `SBMIG_CONFIG`, 고급 설정의
`SBMIG_STOP_AFTER` / `SBMIG_LLM_PROVIDER`)를 채워 Job 을 만든다 → 파드의 `run-job.sh` 가 `sbmig run --out /data/out --run-id <run id>` 를 돌려 같은
실행 폴더에 저널·로그·보고서·`result.zip` 을 쓴다 → 웹은 그 폴더를 tail 해 SSE 로 보낸다(`graph_node_end` 저널 줄 → 화면 `node` 이벤트, 레일 앞에
'파드' 단계: 생성 중 → 준비됨) → 끝나면 파드가 `job-status.json {exit_code}` 를 쓴다. 멈춤 = 웹이 `<run dir>/stop.request` 를 쓰고 엔진이 노드
사이에서 멈춘다(최종 빌드 없음, 종료 3); `SBMIG_JOB_STOP_GRACE_S`(600) 뒤에도 돌면 Job 삭제. 웹 파드 재시작 때 Job 은 그대로 돈다.
큐는 그대로라 Job 도 한 번에 하나(병렬 3 개는 다음 단계). 쿠버네티스 API 는 `sbmig/web/k8s.py`(requests + 서비스 계정 토큰·CA, `kubernetes`
패키지 없음)로 Job 생성·상태·삭제만 부른다 — 권한은 `deploy/k8s/45-web-rbac.yaml`. 켜는 순서는 `deploy/README.md` §3 "켜는 법".
개발 PC: `k8s-job` 인데 클러스터 밖이면 자동으로 `local-job`(`sbmig/web/localjob.py`, 파드 자리에 로컬 서브프로세스 `sbmig run --run-id …`)으로
내려가 같은 흐름이 파드 없이 돈다(`--mode local-job` 으로 직접 고를 수도 있다).

조각:

| 파일 | 하는 일 |
|---|---|
| `sbmig/web/runs.py` | `RunManager(mode=…)`: `_start_job`(실행 폴더 + source.zip + 저널 첫 줄) → `_run_job`(Job 생성·감시·멈춤·종료 코드), `status()` 의 `mode` / `job`, `events()` 의 `graph_node_end` → `node` 변환, `job-status.json` 으로 끝난 실행 인식 |
| `sbmig/web/k8s.py` | `K8sClient.in_cluster()`, `create_job` / `job_state` / `delete_job`, `render_job`, `job_name` |
| `sbmig/graph/engine.py` | 노드마다 `graph_node_end` 저널, `stop.request` 검사(`user_stop`) |
| `deploy/docker/run-job.sh` | 공유 볼륨 모드(`SBMIG_SOURCE_ZIP`, `SBMIG_OUT` / `SBMIG_RUN_ID`, `SBMIG_KB_DIR`, `job-status.json`); git 모드(`SBMIG_SOURCE_GIT_URL` / `SBMIG_SOURCE_ZIP_URL`, KB·결과 push)는 그대로 |
| `deploy/k8s/81-web-job.template.yaml` | 웹이 채우는 Job 틀(공유 PVC) — `80-build-job.template.yaml` 은 git 모델(인프라팀 수동 제출용) |
| `deploy/k8s/45-web-rbac.yaml` | 웹 파드의 최소 권한(jobs create / get / list / watch / delete, pods get / list / watch, pods/log get) |
| `deploy/k8s/submit-job.sh` | git 모델 템플릿을 **손으로 시험해 보는 도구**. 운영 모델이 아니다 |

다음: 클러스터 실측(RWX·이미지·권한), 동시 Job 3 개, 외부 접수 API(remediation 과 같은 계약 — 구현 지시서 `docs/v2/API-MIGRATIONS-KO.md`).

`run-job.sh` 종료 코드: 보고서가 나왔으면 0(sbmig 0 또는 2 — 빌드 실패도 "결과"), sbmig 중단(exit 3: 사전 점검 / ADF / 내부 오류)이면 3,
결과를 밖으로 못 보냈으면 4. `SBMIG_FAIL_JOB_ON_BUILD_FAIL=1` 이면 빌드 실패를 2 로.

**인프라 회의에서 정할 것**: Job 분리가 필수인지(아니면 ① 을 먼저 올린다), 웹 파드 권한(RBAC 파일 또는 `/etc/kube/config`), 네트워크(빌드 노드 →
웹 파드 · LLM 게이트웨이 · Nexus · git), KB 영속성(git 저장소 또는 Job 이 마운트하는 PVC), `~/.m2`(PVC 또는 미리 채운 레이어), 결과 보관
기간과 소스 저장소 브랜치 push 여부.

`build.setup_command` / `build.teardown_command` 는 실행 전후 한 번씩 도는 일반 훅이다(두 모양 모두 비워 둔다).

## 6. 일괄 실행 (fleet) — 개발·대량 시험용

`sbmig fleet run <manifest> --config config/artifacts.yml --out out/fleet` 가 목록의 프로젝트를 **하나씩 차례로**, KB 하나를 공유하며 돌린다.
매니페스트: 디렉토리(그 안의 `*.zip` 과 pom 이 있는 하위 폴더), `.txt`(한 줄에 경로 하나), `.yml`(`projects: [{name, input, configs, overrides, tags}]`),
`.csv`(`name,input,tags`). 예: `fixtures/fleet/manifest.example.yml`. 프로젝트별 `overrides` 는 겹쳐지지만 `kb.dir` 은 언제나 플릿 공통이다.

| 명령 | 하는 일 |
|---|---|
| `sbmig fleet run` | 실행(기본은 이어서 — 성공한 것은 다시 돌리지 않음). `--dry-run` 은 순서와 재개 결정만 |
| `sbmig fleet resume <out>` | 같은 매니페스트·설정(`fleet-config.json`)으로 멈춘 곳부터. `--retry-failed` 면 incomplete / aborted 도 |
| `sbmig fleet status <out>` | 진행 표(실행 중에도 다른 창에서). `--attention` 은 사람이 봐야 할 프로젝트만 주의 코드별로 |

남는 것(`<out>/fleet/` 아래): `state.json`(프로젝트별 결과), `summary.md` / `summary.csv`(UTF-8 BOM, `attention` 열) /
`summary.json`, `learning.md`(이 플릿 동안 KB 가 배운 것), `fleet-config.json`, `sbmig.log`(프로젝트마다 `SBMIG-RESULT` 한 줄). 각 프로젝트는
보통 실행 디렉토리(`out/<fleet-id>-<NNN>-<name>/`)를 갖는다. 코드는 `sbmig/fleet/`(`runner.py`, `manifest.py`, `preflight.py`, `cleanup.py`, `summary.py`).

인프라 보호: 첫 프로젝트 전에 사전 점검(`fleet.preflight`: disk / maven / java / settings / repository / llm, 한 줄씩 `preflight: <check> ok|FAIL|skip`);
인프라 이유로 끝난 프로젝트는 실패로 세지 않고 `pending` 으로 되돌리며, `fleet.infra_failure_limit`(3)번 연속이면 `fleet.infra_retry_minutes`(15)
기다렸다 다시 점검, `fleet.infra_max_waits`(4)번 뒤 멈춘다(상태 저장, 다시 실행하면 이어감). 프로젝트 전 여유 공간 `fleet.min_free_gb`,
끝난 뒤 정리 `fleet.cleanup`(`build-output` = 실행 복사본의 `target/` 삭제, `workspace` = 프로젝트 복사본도, `none`).
종료 코드: 0 전부 성공, 2 일부 미완, 3 중단(`--stop-on-abort` 또는 인프라).

## 7. KB 운영 (PC 사이 옮기기)

KB 는 `kb.dir` 아래 파일뿐이다: `errors.jsonl`, `cases.md`, `cases/<case_id>/case.json` + `fix_v<N>.py`, `.lock`(웹·플릿·`sbmig kb` 가 같은
KB 를 쓸 때의 프로세스 간 잠금, `kb.lock_timeout_seconds`).

| 명령 | 하는 일 |
|---|---|
| `sbmig kb export <OUT.zip>` | KB 를 zip 하나로 |
| `sbmig kb import <zip>` | 가져오기(기본은 합치기, `--replace` 는 통째 교체하고 예전 KB 를 `.bak-*` 로) |
| `sbmig kb validate` | 파일 점검(문제 있으면 exit 1) |
| `sbmig kb stats` | 항목·케이스·상태별 스크립트 버전 수 |
| `sbmig kb reindex` | `errors.jsonl` 로부터 케이스 수치·실행 기록을 다시 셈(`git pull` 뒤 한 번 권장) |
| `sbmig kb cases list` / `sbmig kb cases show <case_id>` | 케이스와 저장 스크립트(계보·실행 기록). `retire` / `activate` / `pin` / `unpin` 은 사람의 결정 |

모든 `sbmig kb` 명령은 `--kb-dir` 로 `kb.dir` 을 덮어쓸 수 있다.

## 8. 디스크 정리 `sbmig clean`

끝난 실행의 프로젝트 복사본, 웹 UI 업로드 zip, 남은 `.part` 업로드, LLM 기록(`llm-*.json`)을 지운다. 실행 중·대기 중인 실행은 건드리지 않는다.
옵션: `--out`(웹이면 `out/web`), `--fleet`, `--older-than <일>`, `--keep-failed`, `--dry-run`(먼저 이것으로), `--records`.

## 9. 로그 파일

| 파일 | 내용 |
|---|---|
| `out/<run id>/sbmig.log` | 실행 하나의 DEBUG 로그(웹 실행도 실행마다 따로) — 마지막에 `SBMIG-RESULT` 줄 |
| `out/<run id>/journal.jsonl` | 단계·결정 이벤트(`restructure`, `bundled_jars`, `library_lookup` …) |
| `out/<run id>/logs/` | Maven 빌드 로그(`NNN-build-*.log`), 구조 검토 입력 등 |
| `<fleet out>/fleet/sbmig.log` | 플릿의 INFO 로그(사전 점검, 프로젝트별 `SBMIG-RESULT`, 인프라 대기) |
| `--log-file <path>` | 어떤 명령이든 DEBUG 로그를 그 파일에도 덧붙임 |
| `llm.record_dir` (`LLM_RECORD_DIR`) | LLM 요청·응답 `llm-NNNN.json`(비밀값 없음) |

## 10. 문제를 보고할 때 보내는 것

회사 PC 는 **복사-붙여넣기가 안 되는 Windows** 다. 로그 파일 통째를 요청하지 말고 **특정 줄**을 PowerShell 명령과 함께 요청한다.

1. **결과 한 줄** — 가장 먼저:
   `SBMIG-RESULT run=<id> exit=<n> build=<ok|FAIL> unresolved=<n> blocked=<n> llm=<calls> attn=<codes|->`
   (`Select-String -Path out\<run id>\sbmig.log -Pattern "SBMIG-RESULT" -Encoding UTF8`)
2. **사전 점검 실패**: `preflight: <check> FAIL …` 줄.
3. **원인 줄**: `cause=` 가 들어간 줄(LLM 게이트웨이·사전 점검 실패의 원인), 보고서의 Handover 첫 줄들, 미해결 오류 표의 케이스 id.
4. **케이스 id** 로 KB 에서: `sbmig kb cases show <case_id>`, `sbmig kb errors show <id>`.
5. 빌드 오류 원문: `logs\NNN-build-*.log` 의 `[ERROR]` 줄 몇 개.
6. 어느 단계가 어떤 줄을 썼나: `git -C out\<run id>\project log --oneline` (첫 커밋은 as-is 스냅샷), `git log -S "<문자열>" -- pom.xml`.

주의 코드(`attn=`) 예: `infra:<kind>`, `crash`, `aborted[:<reason>]`, `blocked:<reason>`, `unresolved:<n>`, `restructure-failed`,
`hollow-reactor`, `parent-chain-no-boot-bom`, `tests-not-compiled`. 정의는 `sbmig/report/result_line.py`.

## 확인하는 법

```bash
.venv/bin/python -m sbmig.cli serve --help              # --host / --token / --max-upload-mb / --graceful-timeout
sed -n 1,40p sbmig/web/app.py                           # 라우트, 토큰 규칙
grep -n "queued" sbmig/web/runs.py
.venv/bin/python -m sbmig.cli fleet --help && .venv/bin/python -m sbmig.cli kb --help && .venv/bin/python -m sbmig.cli clean --help
grep -n "PACKAGE_ROOT\s*=" sbmig/core/config.py         # 레포 루트를 자기 위치에서 잡는 줄
grep -o 'SBMIG_[A-Z_]*' deploy/docker/run-job.sh | sort -u
ls deploy/docker deploy/k8s && cat deploy/README.md
grep -n "def write_summaries" -A25 sbmig/fleet/summary.py
grep -n "SBMIG-RESULT" sbmig/report/result_line.py | head
```
