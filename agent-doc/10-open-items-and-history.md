# 열린 과제와 역사

> 다루는 것: sbmig 가 어디까지 왔는지(v1 → v2 → v2.1 증분을 주제별로), 회사 실행에서 나온 진단 사례(증상 → 원인 → 만든 것), 사용자가 결정할 열린 과제, `docs/PLAN.md` / `docs/INTERFACES.md` 읽는 법. 이 팩에서 날짜와 버전 태그가 허용되는 유일한 파일이다.
> 언제 읽나: "왜 이렇게 만들어졌나", "이 기능은 언제 왜 들어왔나", "다음에 무엇을 하기로 했나" 가 궁금할 때.
> 함께 읽을 파일: [08-dev-conventions-and-tests.md](08-dev-conventions-and-tests.md), [09-deploy-and-ops.md](09-deploy-and-ops.md), [../docs/PLAN.md](../docs/PLAN.md), [../docs/INTERFACES.md](../docs/INTERFACES.md)

## 1. 큰 줄기

| 시기 | 이름 | 무엇이었나 |
|---|---|---|
| 2026-10-18 … 10-26 | **v1** (git 태그 `company-v1`, `main`) | 결정적 핸들러 32개, KB 레시피, SEARCH/REPLACE 패치로 고치는 LLM, 라운드 1~10 (어댑터 경계, 레시피 카탈로그, 플릿, 라이브러리 권고 + 인수인계, LangGraph 엔진 + 웹 UI, 회사 레이아웃, 오류 케이스 카탈로그) |
| 2026-10-26 … 10-27 | **v2** (브랜치 `flow-v2`) | 현업 요청 "레시피가 무엇을 바꿀지 모르니 넣지 않는다": OpenRewrite 1회(Java 21 / jakarta / Boot 4 레시피만) → 회사 재구성 → LLM 구조 검토 → `mvn clean package` → 빌드 오류마다 **LLM 이 쓴 Python 수정 스크립트**, 오류 케이스(L2 서명)별로 KB 에 저장해 다음 프로젝트가 재사용. v1 코드 삭제 |
| 2026-10-27 … 11-04 | **v2.1d … v2.1z-u** | 회사 실행에서 나온 문제를 하나씩 막은 증분들(아래 2절) |

v1 이 실패한 이전 사내 에이전트의 두 원인(빌드를 돌리지 않고 "끝났다" 고 한 LLM, 작은 LLM 의 약한 레시피 선택)을 피하려고 만든 원칙 —
결정적 계획, 빌드로 판정하는 실행 — 은 v2 에서도 그대로다. 설계 결정의 원문은 `docs/v2/PLAN-v2.md`, v2.1 의 계획은 `docs/v2/PLAN-v2.1.md`.

## 2. v2.1 증분을 주제별로

각 행의 "읽을 곳" 은 `docs/INTERFACES.md` 의 절 제목(같은 이름의 Status 표가 `docs/PLAN.md` 에도 있다).

| 주제 | 무엇을 바꿨나 (지금 동작) | 읽을 곳 |
|---|---|---|
| **버전 정책** | LLM 이 스스로 메이저를 올리거나 라이브러리를 바꾸지 않는다. as-is 부모가 관리하던 버전은 as-is 유효 pom 에서 옮겨 고정, 버전 없는 의존성은 LLM 없이 채움(`sbmig-version`), 모르면 `null`. 재구성 전 빌드 없음, 회사 부모 체인의 사실은 그 pom 들에서 읽음(Boot BOM 포함), 체인이 관리하지 않는 버전은 목표 Boot BOM 을 따름, as-is 버전 속성·고아 속성·체인을 가리는 관리 항목 제거 | v2.1d, v2.1l, v2.1o, v2.1p, v2.1s, v2.1t, v2.1z-k, v2.1z-l |
| **리소스·JSP·레거시 컨텍스트** | 필터링되는 바이너리 → `nonFilteredFileExtensions`(내용 기반), JSP 의 javax → jakarta 이름 바꾸기, 선택 사내 아티팩트의 트리거(`when_present`), 레거시 Spring 컨텍스트 스위치 | v2.1h, v2.1m, v2.1n, v2.1u … v2.1y |
| **동봉 jar** | pom 이 선언하지 않은 트리 안의 jar(WEB-INF/lib 의 벤더 jar)를 분석에서 찾아, 재구성 때 첫 빌드 전에 연결: 저장소에 좌표가 있으면 일반 의존성, 없으면 `<module>/lib/` + `system` 스코프 + Nexus 게시 요청. 목표 체인이 관리하는 g:a 는 대체됨(연결 안 함). 컴파일 오류 때의 LLM 없는 사전 시도(`sbmig-bundled`)는 안전망 | v2.1z-i, v2.1z-j |
| **javax / jakarta 라이브러리** | sbmig 가 jakarta 빌드를 직접 찾는다: 저장소 집합(명시 URL → settings 미러 → pom 저장소 → Central), 같은 좌표의 높은 라인(`-jakarta` 한정어, pom, jar 스캔), 표의 `jakarta_key`, Nexus 검색, 형제 이름 탐색 → LLM 없는 교체(`sbmig-jakarta`). 진단 명령 `sbmig repo-probe` | v2.1z-e |
| **접수 규칙** | Ant 프로젝트는 받지 않고 안내 문구 "Maven 변환 후 신청 부탁드립니다." 를 보인다(2026-11-12 회사 결정, v2.1z-aj; v2.1j 의 pom 합성은 `intake.ant_to_maven: true` 로 남음); 루트 pom 없는 트리는 집계 pom 생성, 리액터 중복 제거(v2.1k, 외톨이 집계 pom 은 2026-11-02); 소스가 모듈 밖에 있는 **속 빈 리액터**는 성공이 아님(v2.1z-c); **Oracle ADF** 는 증거 2종 이상이면 변환 불가로 멈춤(v2.1z-d); IDE 출력·백업 폴더의 **떠도는 pom 복사본**은 모듈이 아님(v2.1z-h) | v2.1j, v2.1k, v2.1z-c, v2.1z-d, v2.1z-h, v2.1z-aj |
| **수정 사다리** | 재사용 → 보강 → 신규, 케이스당 LLM 20회 / 보강 15회(넉넉히 잡고 줄여 갈 값), 거절 1회면 `needs_owner`, 직접 편집 4회, 비교용 스위치(`fix.mode`, `--no-kb`) 삭제, 리액터 artifactId 를 케이스 키에서 `<module>` 로 가림, 진동 상한 설정화 | v2.1i(삭제된 것), v2.1z |
| **LLM 게이트웨이** | legacy 모양 `{model, messages, need_origin}` → 사내 GPT **agent 게이트웨이**(`llm.gateway: agent`: asset id, `params.role`, 토큰 교환) → 게이트웨이가 돌려준 `session_id` 를 다시 보내 세션을 잇는 배관(`thread_id` 폐기) → **케이스 스레드**(`llm.case_thread`, 기본 꺼짐: 케이스의 보강은 같은 세션에 변화분만) → URL 안의 asset id → 세션은 모델을 못 바꾸므로 모델이 바뀌는 요청은 새 세션 | v2.1z-m, v2.1z-n, v2.1z-o, v2.1z-p, v2.1z-q, v2.1z-x |
| **배포** | 이미지 하나(`deploy/docker/Dockerfile`), 웹 UI Deployment, 인프라 가이드의 요청별 Job(`deploy/docker/run-job.sh`, `deploy/k8s/80-build-job.template.yaml`), 실행 전후 훅. 빌더 파드에 exec 하던 레이아웃은 Job 모델이 나오며 삭제, `deploy/` 주석 한국어 | v2.1z-r, v2.1z-s, v2.1z-t, v2.1z-u |
| **Job 디스패치 검토** (2026-11-10) | 배포팀 명세를 옮긴 Main / Worker 코드(v2.1z-ac)의 리뷰에서 나온 네 지적과 회귀 하나: ① 다음 실행은 앞 Job 이 **끝난 것을 API 로 확인한 뒤**(`job-status.json` 만으로 끝내지 않는다; API 장애는 실패가 아니라 줄을 붙잡고 `SBMIG_JOB_API_GRACE_S` 는 알림만, 새 `SBMIG_JOB_EXIT_GRACE_S`) ② 라벨 없는 Job 은 남의 것(`sbmig-owner` 와 `sbmig-run-id` 둘 다 맞아야) ③ Worker 가 자기 환경 변수를 그 Job 을 만든 Main 의 것과 대조(`control/state.json` 의 `env_pins` / `env_pin_policy`, `SBMIG_JOB_ENV_PIN` refuse / warn / off, 거절은 저널 `worker_refused`, 경고는 `worker_env_differs`) — 운영 규칙 "`sbmig-env` 를 바꾸면 웹 파드 재시작, 81 틀을 바꾸면 `sbmig-job-template` 을 다시 만들고 재시작" ④ 비밀 값은 끝난 뒤가 아니라 **쓰는 순간** 지운다(`sbmig/core/scrub.py`, `SBMIG_REDACT_OUTPUT`, 실행 중에는 `sbmig.log` / `journal.jsonl` 만 내려받기). 회귀: local-job 이 Job 틀의 컨테이너 경로를 PC 실행에 넘기던 것 → 실행별 값만 | v2.1z-ac, v2.1z-ad |
| **동시 Job** (2026-11-10) | 웹 파드가 Job 을 한 번에 하나가 아니라 `SBMIG_JOB_MAX_PARALLEL` 건까지 돌린다(코드 기본 1, 매니페스트 값 3; 웹 파드가 직접 도는 모드는 1). 올린 순서를 지키고, 도는 Job 마다 자리 번호(`control/state.json` 의 `slot`, Job env `SBMIG_JOB_SLOT`)를 주며 2 번 자리부터는 Maven 로컬 저장소를 따로 쓴다(`repository-slot<k>`, 저널 `worker_slot`). 기다리는 실행은 `status()` 의 `queue.position` 으로 순번을 보고 화면이 알린다. KB 는 폴더 하나 그대로: 쓰기는 모두 `<kb>/.lock` 안에서 하고, 여러 파드를 위해 새 케이스 폴더·스크립트 파일·LLM 기록 파일을 "없을 때만 만들기"로, 잠금의 나이를 KB 파일 시스템의 시계로 잰다. 회사 클러스터·NFS 에서는 아직 재지 않았다 | v2.1z-ae |
| **동시 Job 검토** (2026-11-10) | 세 파드 동시 실행을 다섯 갈래로 검토해 재현된 것만 고침: 끝 상태를 적은 뒤에 자리를 비움, 재시작 복구는 적힌 자리 번호를 그대로 지킴, API 의 이상한 응답은 실패가 아니라 "API 불가", 파드가 아직 끝나는 중인 `Failed` Job 은 끝난 것이 아님, 대기 중 멈춤은 바로 줄에서 빠짐; Worker 가 SIGTERM 에 Maven 까지 멈추고 자리 저장소를 못 만들면 파드 안 임시 저장소; KB 는 서버가 있다고 한 `case.json` 을 덮어쓰지 않음(`CaseUnreadable`), 낡은 잠금 깨기를 `.lock.breaker` 로 한 줄 세움, 긴 구간의 잠금 갱신(`keep_alive`); 게이트웨이의 바쁨(429)은 `llm.busy_wait_seconds` 까지 줄 서기, 갓 받은 토큰의 401 은 `llm.auth_retries` 번 다시 받기; 화면은 웹 파드 재시작 뒤 스스로 다시 붙음 | v2.1z-af |
| **실행 방식과 화면** (2026-11-12) | 실행 방식 두 가지: `full`(이관 → 성공하면 취약점 조치) / `remediation`(이관 없이 올린 프로젝트를 그대로 조치 — 이미 이관된 프로젝트를 다시 점검할 때). `sbmig run --flow`, `POST /api/runs?flow=`, 화면의 전체 진행 / 취약점 해결, Job env `SBMIG_FLOW`. 화면 머리 이름 `panacea-remediating-oss`(임시), 실행 폴더 이름은 올린 파일 이름의 앞머리로 시작, 접수 거절 사유를 문장으로 | v2.1z-aj |
| **KB 학습 기록** | 시도가 왜 실패했는지, 수정 전·후 코드, 시도 없이 해결된 오류의 해결자 | v2.1e, v2.1f |

## 3. 회사 실행에서 배운 것 — 진단의 실제 예

회사 PC 는 복사-붙여넣기가 안 된다. 진단은 사용자가 옮겨 적은 몇 줄(`SBMIG-RESULT`, Handover 첫 줄, 저널 이벤트, `[ERROR]` 줄, 실행 디렉토리의
git 기록)로 했다. 아래 사례는 모두 그런 줄에서 출발했다. 실제 벤더 좌표는 적지 않는다.

**(a) 사내 암호화 jar → 동봉 jar (2026-11-04)**
- 증상: `spring_legacy` WAR 이 `package com….crypto.seed does not exist` 로 실패, 수정 LLM 이 "좌표를 모른다" 며 거절 → BLOCKED `needs_owner` 8건.
- 원인: 사내 암호화·SSO jar 가 `src/main/webapp/WEB-INF/lib/` 에 있었고(pom.properties 없음) 어떤 pom 도 선언하지 않았다. IDE 빌드는 보았지만
  Maven 은 본 적이 없다. 재구성이 그 폴더를 back 모듈로 옮겼고, LLM 은 jar 가 있다는 사실을 몰랐다.
- 만든 것: 동봉 jar 스캔(`sbmig/analyze/bundled_jars.py`), 재구성 때 연결(`sbmig/restructure/bundled_jars.py`, `layout.wire_bundled_jars`),
  사전 시도(`sbmig/restructure/system_jars.py`), 프롬프트에 "프로젝트가 이 패키지를 제공하는 jar 를 동봉한다" 는 사실, 그 상황의 좌표 거절은
  받아들이지 않음. 픽스처 `webapp-bundled-jar`. 같은 날 사용자 지적: 찾은 jar 를 전부 연결하는 것은 안티패턴 → 대체됨(목표 체인이 관리하는 옛
  Spring jar 등)·컨테이너 API·참조 없는 jar 는 건드리지 않고 이름만 적는다.

**(b) 남은 버전 속성 → 고아 속성 + 관리 항목 정리 (2026-11-04)**
- 증상: 재실행 뒤에도 boot pom 에 as-is `java-version=1.8`, Spring Security 4.2.x 를 가리키는 속성이 남고, Security 가 7.x 가 아니라 4.2.x 로 해석.
- 원인: 이름 규칙 기반 정리가 as-is 의 독특한 철자를 놓쳤다. 또 역할 부모 모듈이 받은 as-is `<dependencyManagement>` 항목이 가져온 Boot BOM 보다
  우선해(자기 항목이 이김) 옛 버전이 살아남았다.
- 만든 것: 값 기반 고아 속성 제거(참조 없는 버전 모양 값, 부모 체인 사실이 있을 때만), 체인·Boot BOM 이 같거나 새 라인으로 관리하는 자기 관리 항목
  제거, 구조 검토가 다른 라인의 Spring 계열 고정을 critical 로 판정. (`docs/INTERFACES.md` v2.1z-k, v2.1z-l)

**(c) Spring Mobile → 직접 편집 + 노트 (2026-11-04)**
- 증상: `org.springframework.mobile` 이 "jakarta 빌드 없음" 으로만 차단, 한 케이스가 스크립트 → 보강 → 되돌리기로 LLM 20회를 썼다.
- 원인: EOL 라이브러리이고 대체는 프로젝트 코드(작은 장치 판별기)를 쓰는 일이라 재사용할 스크립트가 아니다.
- 만든 것: 차단 줄이 EOL 권고와 대체품을 말함, 동봉 jar 로 들어온 라이브러리도 권고와 맞춤, `knowledge/notes/spring-mobile.md`(바이트코드 변환 금지,
  호환 대체 작성), 답은 `<no_script>` + `<edits>` 로 받고 `script.max_edit_tries_per_case`(4)로 제한.

**(d) spring-dao 같은 없어진 아티팩트 → 제거 노트 (2026-11-04)**
- 증상: 목표 라인에 존재하지 않는 옛 Spring 아티팩트에 버전을 박으려는 시도.
- 만든 것: `knowledge/notes/spring-removed-artifacts.md`(지우고 옮길 것), 관리되지 않는 그런 아티팩트에 버전을 고정하지 않음.

**(e) `parent-chain-no-boot-bom` (2026-10-30)**
- 증상: 저널 `parent_chain_facts … boot_bom_added: true`, 이관된 pom 에 서로 다른 명시적 Boot 버전이 섞여 남음.
- 원인: 회사 base-parent 체인이 `spring-boot.version` 속성만 선언하고 Boot BOM 을 import 하지 않았다 — 아무것도 Boot 아티팩트를 관리하지 않았다.
- 만든 것: 경고 플래그 `parent-chain-no-boot-bom`(Handover 첫 후속 과제, `attn=`), 사용자가 base-parent 를 고칠 수 없어서 `auto` 모드가 역할 모듈
  pom 에 `spring-boot-dependencies:${spring-boot.version}` import 를 더함(v2.1s, v2.1t). 회사 쪽 근본 해결(base-parent 에 BOM import)은 열린 결정.

**(f) 떠도는 pom 복사본 (2026-11-03)** — `Project '…-boot:…' is duplicated in the reactor` 로 0회 시도 종료. 입력 zip 에 boot 모듈의 통째 복사
(`boot/bin/`)가 있었고, 구조 검토 LLM 이 그것을 "목록에 없는 모듈" 로 등록했다. 진단은 `git log -S "<module>…" -- pom.xml` 로 그 줄을 쓴 커밋
(`review: …`)을 찾아서 했다. → `stray_pom_copies`, 접수 단계 무시, 검토의 불변조건 재검사.

## 4. 열린 과제 (사용자가 결정)

| 과제 | 상태 |
|---|---|
| **케이스 스레드 측정** | 만들어졌고 기본 꺼짐. 세션의 이전 턴이 어떻게 과금되는지 문서가 없어 턴마다 `sent N chars → prompt_tokens=K` 를 로그로 남겨 측정 중. 켤지는 회사 실행의 수치를 보고 |
| **KB 케이스 키의 남은 뭉침** | 서명 규칙 버전과 재서명, 라이브러리 타입 멤버의 주인 타입 키, 정확 일치 보호는 있다(`04` 9절). 남은 것: 프로젝트 소유 타입의 멤버(`…|cannot_find_symbol|save`), 문법 오류(`';' expected`), `unreported exception <타입>` 은 이름·문구만으로 묶인다. 회사 KB 에서 실제로 잘못 재사용된 사례를 보고 다음 규칙 버전을 정한다 |
| **웹 UI 의 Job 디스패치 모드** | 있음(2026-11-05: 웹 파드가 업로드마다 Job, 공유 PVC; 지키는 규칙 v2.1z-ac 2026-11-09, v2.1z-ad 2026-11-10). 동시 Job 은 2026-11-10 부터(v2.1z-ae: `SBMIG_JOB_MAX_PARALLEL`, 매니페스트 값 3). 남은 것: 회사 클러스터·NFS 에서의 실측(RWX·이미지·권한, 세 파드 동시 실행, KB 잠금), 게이트웨이의 동시 호출 한도 확인, 외부 접수 API(`docs/v2/API-MIGRATIONS-KO.md`) |
| **Job 모드의 알려진 한계** (2026-11-10 검토 뒤) | (1) 끊긴 생성 요청이 확인 질의(`CREATE_CONFIRM_POLLS`) 뒤에 늦게 닿으면, Main 이 실패로 끝내고 차례를 넘긴 뒤에 Job 이 생길 수 있다 (2) 영구히 닿지 않는 쿠버네티스 API 는 Main 의 접근을 고칠 때까지 큐를 멈춘다(의도 — 두 이관이 겹치는 것보다 낫다) (3) 콘솔에서 접혀 두 줄에 걸친 비밀 값은 줄 단위 정제가 놓친다(Worker 는 자식에 넓은 `COLUMNS`) (4) 자식 안에서 생긴 토큰은 Worker 의 파이프 정제기가 모른다(자식의 자기 훅이 로그 줄은 지운다) (5) 6자 미만·짧은 숫자 비밀은 지우지 않는다 (6) `logs/` 의 Maven 출력은 끝날 때의 마지막 단계에서만 지운다 (7) uvicorn 접근 로그는 정제하지 않는다 (8) `deploy/docker/run-job.sh` 자신의 셸 출력은 sbmig 가 정제하지 않는다 (9) 환경 대조는 비밀로 보이는 이름(`LLM_TOKEN_URL` 포함 — 같은 이름 규칙으로 그 값은 출력에서 `***` 로 가려진다)과 프록시 변수를 비교하지 않는다 |
| **LLM 에게 as-is 보기** | 첫 커밋(as-is 스냅샷)의 파일을 `asis:<path>` 로 보여 주고 오류 파일의 as-is → 지금 diff 를 자동으로 주는 아이디어. 케이스 스레드 증분에 포함 예정 |
| **소스 채택(트리 프로필)** | 속 빈 리액터처럼 소스가 모듈 밖(`DEV/`, `STATIC/`)에 있는 트리를 모듈에 매핑하는 증분. 다음 증분으로 합의됨 |
| **MSA 의 비-Boot WAR 서비스** | 지금은 boot 부모만 주고 Boot 앱·레이아웃 분할은 안 함. "비-boot war 는 일단 그대로" — 실제 프로젝트를 먼저 본다 |
| **사다리 상한 낮추기** | LLM 20 / 보강 15 는 넉넉히 잡은 값. 회사 실행에서 케이스가 실제로 몇 번 필요한지 보고 줄인다 |
| **회사 환경에서 확인할 것** | `docs/RUNBOOK.md` §0(JDK 21, Maven 3.9, Nexus 가 프록시해야 할 것: OpenRewrite 플러그인·레시피 BOM 3.37.0, Boot 4.1.0, jakarta.*) — 첫 v2 실행은 같은 프로젝트로 `company-v1` 과 비교 |
| **base-parent 의 Boot BOM** | 회사(부모 소유자)가 base-parent 에 import 를 더하거나, 그 전까지 `artifacts.parent_manages_spring_boot: false` |

## 5. `docs/PLAN.md` 와 `docs/INTERFACES.md` 읽는 법

- **`docs/PLAN.md`** 는 라운드마다 "Status — <날짜>, <라운드>" 표 하나다. **최신이 맨 아래.** 각 행은 증분 / 산출물 / 어디(파일) / 상태이고,
  표 아래에 열린 과제가 붙는다. v2 이후는 "Status — 2026-10-27, v2" 부터.
- **`docs/INTERFACES.md`** 는 계약(모델 필드, 설정 키, CLI, 보고서, 저널 이벤트)이다. 앞쪽(공개 진입점, 변경 기록, Error cases, Case catalog,
  Round 10)은 **역사**이고, **"v2 — flow-v2" 절과 그 뒤의 v2.1x 절들이 지금 유효한 계약**이다. 같은 주제가 여러 절에 나오면 뒤의 절이 이긴다
  (예: v2.1i 의 `fix.mode` 는 v2.1z 에서 삭제).
- 둘 다 길다(수천 줄). 통째로 읽지 말고 절 제목으로 찾는다: `grep -n "^## " docs/INTERFACES.md`.
- `CLAUDE.md` 는 결정과 현황의 요약(사용자가 말한 회사 제약, 불변조건, 알려진 후속 과제)이고, 문서와 코드가 다르면 **코드가 사실**이다.

## 확인하는 법

```bash
git tag | grep company-v1
grep -n "^## Status" docs/PLAN.md | tail -15
grep -n "^## v2" docs/INTERFACES.md
grep -n "PARENT_NO_BOOT_BOM_FLAG\s*=" sbmig/restructure/restructurer.py
grep -n "def stray_pom_copies" sbmig/analyze/pom_parser.py
grep -n "case_thread\|thread_fallback_full" sbmig/core/config.py
grep -n "CREATE_CONFIRM_POLLS\|API_REMIND_S\|HOLD_WARN_S" sbmig/web/runs.py; grep -n "ENV_PIN_POLICY_ENV" sbmig/web/contract.py
grep -n "as-is view for the LLM" docs/INTERFACES.md
grep -n "Open items" -A12 docs/PLAN.md | tail -14
ls knowledge/notes/spring-mobile.md knowledge/notes/spring-removed-artifacts.md fixtures/projects/webapp-bundled-jar
```
