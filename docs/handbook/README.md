# 0. sbmig 핸드북 — 개요, 읽는 순서, 용어집

`sbmig`는 사내 Maven 프로젝트(약 500개)를 **JDK 21 + Spring Boot 4.1**로 옮기는 마이그레이션 에이전트입니다.
이 핸드북은 회사 담당자·운영자·개발자가 코드를 읽지 않고도 "무엇이, 왜, 어떤 순서로, 어디에 기록하며" 돌아가는지
이해할 수 있도록 한국어로 정리한 문서 모음입니다. 영문 기술 문서(`docs/ARCHITECTURE.md`, `docs/INTERFACES.md`,
`docs/ERROR-HANDLING.md`, `docs/RUNBOOK.md` 등)는 개발 레퍼런스로 계속 유지되며, 이 핸드북은 그 내용을 설명하는 역할을
합니다. 라운드별 변경 이력은 `docs/PLAN.md`에 있습니다.

## 이 문서를 읽고 나면
- 어떤 문서를 어떤 순서로 읽어야 하는지(역할별) 알 수 있습니다.
- 핸드북 전체에서 쓰는 용어(as-is / to-be, 케이스, 단계, 클러스터, 핸들러, 레시피, 어드바이저리, Handover, KB 등)의 뜻을 한곳에서 확인할 수 있습니다.
- 함께 제출되는 PPT·엑셀 파일이 무엇을 담고 있고 어떤 문서와 짝을 이루는지 알 수 있습니다.

## 관련 코드
- `CLAUDE.md` — 설계 원칙, 회사 제약, 불변식, 현재 상태(라운드별 요약)
- `README.md` — 설치와 기본 사용
- `docs/PLAN.md` — 라운드별 "Status" 표(무엇을 만들고 무엇을 검증했는지)

---

## 1. 왜 이렇게 만들었나 (한 문단)

이전 사내 에이전트는 두 가지 이유로 실패했습니다. (1) 수정 LLM이 **빌드를 돌리지 않고 "끝났다"**고 보고했고,
(2) 작은 LLM이 레시피를 고르게 하니 **선택이 약했습니다**. 그래서 sbmig는 반대로 설계했습니다.
계획은 **결정적**(코드가 프로젝트를 분석해 정해진 규칙으로 단계를 만들고), 모든 단계는 **빌드 게이트**(BUILD SUCCESS가
나야 다음 단계)로 닫히며, LLM은 결정적 핸들러·지식 베이스가 못 푸는 **잔여 에러에만** 투입됩니다. 실행 한 번이 끝날
때는 반드시 `mvn clean package`를 한 번 더 돌리고, 못 푼 에러를 리포트에 적습니다(불변식 1). 자세한 원칙은
[03-architecture.md](03-architecture.md) 1장을 보십시오.

## 2. 문서 목록

| 번호 | 문서 | 내용 | 주 독자 |
|---|---|---|---|
| 00 | [README.md](README.md) (이 문서) | 개요, 읽는 순서, 용어집, 제출물 안내 | 모두 |
| 01 | [01-pipeline.md](01-pipeline.md) | 파이프라인 상세: intake → analyze → plan → jdk21 → spring → restructure → verify → report. 단계별 입력·출력·파일·분기·실패 처리·저널 이벤트 | 운영자, 개발자 |
| 02 | [02-workflows.md](02-workflows.md) | 기능별 워크플로우: CLI/웹 실행, LLM 점검, 플릿, 지식 축적 루프, 레시피 점검, 어드바이저리, 외부화 설정, e2e, 결과 받기 | 운영자, 담당자 |
| 03 | [03-architecture.md](03-architecture.md) | 에이전트 아키텍처: 원칙과 불변식, 패키지 지도, 데이터 모델, 설정 계층, 실행 엔진(LangGraph), 웹 서버, 작업공간(git), 진단 장치, 플러그인 심, 타임아웃·예산 | 개발자 |
| 04 | [04-triage-and-loop.md](04-triage-and-loop.md) | 트리아지와 빌드-수정 루프를 쉽게: 로그 파싱 → 시그니처 → 클러스터 → 결정 사다리 → 적용 → 재빌드, 핸들러 카탈로그, LLM 프롬프트, 루프 제어, 로그 한 줄 해석표, 실제 시나리오 3개 | 모두 |
| 05 | [05-directories-and-storage.md](05-directories-and-storage.md) | 디렉터리별 저장 방식과 활용: `config/`, `knowledge/`, `kb/`, `fixtures/`, `out/<run id>/`, `tests/`, `scripts/`, `coding/` | 운영자, 개발자 |
| 06 | [06-restructure-and-layouts.md](06-restructure-and-layouts.md) | 재구성과 회사 레이아웃: `{name}-boot` / `-back` / `-front`, Boot as-is 유지, 역할 부모, 필수·선택 아티팩트, 멀티모듈 merge, JSP/WAR, 플레이스홀더 | 담당자, 개발자 |
| 07 | [07-api-reference.md](07-api-reference.md) | CLI · REST · Python API · 플러그인 계약 · 환경 변수 · report.json / journal / ledger 스키마 | 개발자 |
| 08 | [08-config-reference.md](08-config-reference.md) | 설정 레퍼런스: 모든 설정 키의 타입·기본값·의미·언제 바꾸나, 예시 파일 해설, 첫 설정 체크리스트 | 운영자 |
| 09 | [09-operations.md](09-operations.md) | 운영과 진단: 사전 준비, Nexus 미러, Windows 메모, 리포트 읽는 법, 한 줄 결과 `SBMIG-RESULT`와 주의 코드, "멈춘 건가?" 절차(Ctrl+Break, `dump-threads` 파일), 에러 카탈로그(한국어, 인프라 문제 포함), 플릿 운영(사전 점검, 인프라 차단기, 디스크 정리), KB 위생, 실제 실행 FAQ, 지원 요청 때 옮겨 적을 줄 | 운영자 |

## 3. 역할별 읽는 순서

| 역할 | 순서 | 목적 |
|---|---|---|
| 운영자(실행·모니터링) | 00 → 02 → 09 → 04(9장 로그 해석표) → 08 | 돌리고, 멈춤을 판단하고, 리포트를 읽고, 설정을 바꿀 수 있게 |
| 프로젝트 담당자(as-is 소유자) | 00 → 06 → 02(12장 결과 받기) → 09(3장 리포트 읽는 법) | 내 프로젝트가 어떤 모양으로 바뀌고 무엇을 내가 마저 해야 하는지 |
| 개발자(유지보수·확장) | 00 → 01 → 03 → 04 → 05 → 07 → 06 | 코드 구조와 계약을 이해하고 핸들러·레시피·어드바이저리를 추가할 수 있게 |
| 검토자(제출 심사) | 00 → PPT → 01 → 04 → 09 | 전체 그림과 안전장치, 검증 현황 |

## 4. 제출물(이 디렉터리)

| 파일 | 형식 | 내용 |
|---|---|---|
| `README.md`, `01-…` ~ `09-…` | Markdown | 본문. 코드와의 불일치는 `tests/docs/test_handbook.py`가 검사합니다(백틱으로 적은 저장소 경로·설정 키·CLI 명령·상호 링크가 실제로 존재하는지). |
| `sbmig-overview.pptx` | PowerPoint | 발표용 요약: 배경과 원칙, 파이프라인, 트리아지/루프, 회사 레이아웃, 아키텍처, 운영, 검증 현황 |
| [`../kb-map/index.html`](../kb-map/index.html) | HTML (Mermaid, 오프라인) | KB 데이터 지도: `kb/`에 쌓이는 파일별 키의 뜻, 파일끼리 잇는 연결 키(run_id, 에러 id, 시도 id, L1/L2, case_id, error_symbol, KB 케이스 id, 레시피 id), 관계도와 흐름도, 자주 찾는 질문 — 현업이 KB 데이터로 규칙을 만들 때 보는 지도. 원본 `docs/kb-map/kb-map.html`, 생성 `scripts/handbook/build_kb_map.py`(Mermaid는 같은 폴더의 `mermaid.min.js`, 인터넷 없이 열림) |
| `sbmig-reference.xlsx` | Excel | 표 중심 레퍼런스: 설정 키 전체, 핸들러 카탈로그, 에러 카탈로그, CLI 명령, REST API, 픽스처, 저널 이벤트, 리포트 플래그, 레시피 카탈로그, 어드바이저리, 지식 노트, 디렉터리 — 코드에서 직접 추출 |

## 5. 용어집

| 용어 | 뜻 |
|---|---|
| as-is / to-be | 마이그레이션 전 프로젝트 / 후 프로젝트. sbmig는 as-is를 옛 JDK로 빌드하지 않고 처음부터 JDK 21로 빌드합니다. |
| 케이스(case) | 분석기가 판정하는 프로젝트 유형: `pure_java`, `spring_legacy`(Spring Framework 4/5), `spring_boot`(Boot 2/3), `servlet_tomcat`, `msa`(여러 배포 단위), `unknown`. 케이스가 레시피 세트와 재구성 모드를 정합니다. |
| 단계(phase) / 스텝(step) | 단계는 `intake`, `analyze`, `plan`, `jdk21`, `spring`, `restructure`, `verify`. 스텝은 계획 안의 단위(`jdk21.rewrite`, `spring.loop` …). MSA는 서비스마다 `@<service>` 접미사가 붙습니다. |
| 빌드 게이트 | 한 단계는 BUILD SUCCESS일 때만 끝납니다. 실패하면 빌드-수정 루프가 돌고, 그래도 안 되면 다음 단계로 넘어가되 리포트에 남깁니다. 실행 끝에는 항상 `mvn clean package`를 한 번 더 돌립니다. |
| OpenRewrite / 레시피(recipe) | 소스·pom을 규칙대로 고쳐 주는 도구와 그 규칙. `knowledge/recipes/catalog.yml`이 레시피 id의 단일 출처이고, 라운드마다 `sbmig recipes check`로 드리프트를 막습니다. |
| 빌드-수정 루프(fix loop) | 빌드 → 로그 파싱 → 에러 클러스터 → 결정 → 적용 → 재빌드를 반복하는 루프. 단계마다 반복 횟수와 LLM 예산이 정해져 있습니다. |
| 에러 종류(ErrorKind) | 파서가 분류하는 빌드 에러 유형: `compile`, `test_compile`, `dependency_resolution`, `plugin_resolution`, `parent_resolution`, `plugin_execution`, `enforcer`, `pom_invalid`, `rewrite`, `test_failure`, `unknown`. |
| 시그니처 / 클러스터 | 시그니처는 에러를 "같은 문제"로 묶기 위한 정규화 문자열(L1 구체, L2 일반). 클러스터는 한 번의 결정 단위로 묶인 에러 묶음(파일 기준). |
| 결정 사다리 | 클러스터마다 HANDLER > KB_RECIPE > KB_PATCH > blocked 판정 > LLM_FIX 순서로 해법을 고릅니다. LLM은 마지막입니다. |
| 핸들러(handler) | 결정적(LLM 없는) 수정기. 예: `lombok-version-floor`, `jstl-to-jakarta`, `resources-binary-filtering`, `jakarta-namespace-sweep`. 선제(pre-emptive)로 첫 빌드 전에 돌기도, 에러 문구를 보고 반응(reactive)하기도 합니다. |
| blocked | 프로젝트 안에서 풀 수 없는 문제(외부 라이브러리가 javax API를 쓰는 경우, Nexus에 없는 아티팩트 등). LLM을 쓰지 않고 바로 리포트로 보냅니다. |
| stall level | 같은 클러스터가 반복해서 안 풀릴 때 올라가는 단계. 단계가 오르면 더 넓은 문맥·상위 모델로 재시도하고, 한계에 닿으면 escalate합니다. |
| KB(knowledge base) / ledger | 파일 기반 지식 베이스(`kb/`). `ledger.jsonl`에 모든 시도(에러 시그니처 → 수정 라벨 → 결과)가 기록되고, 성공한 패치는 다음 프로젝트에서 LLM 없이 재생(KB_PATCH)되거나 레시피 후보로 승격됩니다. DB는 쓰지 않습니다(불변식 3). round 10부터 쓰기는 프로세스 간 잠금(`.lock`)으로 직렬화되고, 저장 전에 비밀값을 가리며(`knowledge/kb/redact.yml`), 형식이 바뀌어 회사 KB는 한 번 지우고 다시 쌓습니다([05](05-directories-and-storage.md) 4.11). |
| 수정 라벨(fix label) | 한 수정이 "무슨 종류의 수정이었나"를 결정적으로 붙인 이름(`method-rename`, `generic-rewrite` …). 레시피 승격의 단위입니다. |
| 어드바이저리(advisory) | EOL·비관리 라이브러리(springfox, hystrix, sleuth …)에 대한 안내. 정책은 `guide`(안내만) 또는 `recipe`(레시피로 전환)뿐이며 `block`은 없습니다. 리포트의 Handover에 모입니다. |
| Handover | 리포트 맨 앞의 인수인계 섹션: 못 푼 에러, blocked, 어드바이저리, 실행 전에 채워야 할 속성(`__SET_ME__`), merge 충돌, LLM이 고친 파일. as-is 담당자가 가장 먼저 읽는 곳입니다. |
| 외부화 설정 / `__SET_ME__` | 코드가 읽지만 패키지 안 어디에도 정의되지 않은 설정 키. 재구성이 boot 모듈 `application.properties`에 `__SET_ME__` 플레이스홀더로 적어 두고 Handover에 목록을 냅니다. |
| 역할 부모(role parents) | 사내 부모 pom 세 가지: 루트용 `xxx-parent`(`artifacts.parent`), boot 모듈용 `xxx-boot-jar`(`artifacts.boot_parent`), 그 외 모듈용 `xxx-comp-jar`(`artifacts.comp_parent`). 모두 4.0.0 한 버전 라인입니다. 실제 좌표는 회사에서 `config/artifacts.yml`에 적습니다. |
| 필수 / 선택 아티팩트 | 모든 루트 pom에 넣는 사내 의존성 2개(`artifacts.mandatory`)와 이미 쓰고 있을 때만 버전을 올리는 8개(`artifacts.optional`). |
| 회사 레이아웃 | Boot가 아닌 프로젝트는 `{name}-boot` / `{name}-back` / `{name}-front`로 분리, Boot as-is 프로젝트는 디렉터리를 그대로 두고 pom만 바꿉니다. 상세는 [06](06-restructure-and-layouts.md). |
| 플릿(fleet) | 한 에이전트가 여러 프로젝트를 **엄격히 순차**로 처리하는 모드. 앞 프로젝트에서 배운 것이 KB를 통해 뒤 프로젝트에 쓰입니다. |
| 픽스처(fixture) / e2e | `fixtures/projects/`의 소형 as-is 프로젝트와 기대 결과(`expected.json`). `scripts/e2e.sh`가 실제 Maven으로 끝까지 돌려 회귀를 잡습니다. |
| 저널(journal) | `out/<run id>/journal.jsonl`. 실행 중 벌어진 일을 이벤트 단위로 기록한 기계 판독용 로그. 웹 UI도 이 스트림을 보여 줍니다. |
| 워치독(watchdog) | 빌드-수정 루프가 일정 시간 조용하면 `fix loop quiet for Ns since '<milestone>'`를 찍고, 더 길어지면 스레드 덤프를 자동 저장하는 장치. "멈춘 건가?"의 1차 판단 근거입니다. 덤프는 사람이 요청할 수도 있습니다: 실행 디렉터리에 `dump-threads` 파일을 만들거나, CLI 콘솔에서 Ctrl+Break(Windows), 웹이면 `/api/threads`. |
| exit code | 0 = 최종 빌드 성공이고 blocked 없음, 2 = blocked 또는 미해결 에러와 함께 종료(리포트 확인), 3 = 중단(Maven 없음, Maven이 Java 21이 아님, Maven 프로젝트 아님, 사내 부모 해석 불가 등). |
| `SBMIG-RESULT` / 주의 코드(attention) | 실행(플릿은 프로젝트마다)이 끝날 때 남기는 ASCII 한 줄: `run=… exit=… build=ok|FAIL unresolved=… blocked=… llm=… attn=…`. `attn`은 사람이 봐야 하는 이유(`infra:llm`, `blocked:external_javax_api`, `unresolved:3` …). 문제를 알릴 때 가장 먼저 옮겨 적는 줄입니다([09](09-operations.md) 3.4). |
| 채점 결과(outcome) | 시도 하나를 다음 빌드가 매긴 결과: `improved`(겨냥한 에러가 모두 사라짐), `partial`(일부만), `no_change`, `inconclusive`(그 빌드로는 판단 불가), `regressed` / `reverted`, `build_ok`. "안 보임"은 "해결"이 아닙니다([04](04-triage-and-loop.md) 6.5). |
| 사전 점검(preflight) / 인프라 차단기 | 플릿이 첫 프로젝트 전에 디스크·Maven·Java·settings·저장소·LLM을 한 줄씩 점검하고(`preflight: … ok|FAIL`), 프로젝트가 연달아 인프라 이유로 끝나면 기다렸다 다시 점검하거나 `fleet stopped: …`로 멈추는 장치. 실패한 프로젝트는 `pending`으로 남아 같은 명령으로 이어 갑니다([09](09-operations.md) 7절). |

## 6. 문서 규약

- 본문은 한국어, 코드 식별자·파일 경로·설정 키·CLI 플래그·로그 문구는 코드 그대로 백틱 영문으로 적습니다.
- 실행 산출물 경로는 `out/<run id>/…`처럼 플레이스홀더로 적습니다. 회사 좌표는 `xxx-parent` 같은 자리표시자만 씁니다(실제 값은 회사 설정 파일에만).
- 숫자(기본값, 타임아웃, 횟수)는 `config/default.yml`과 코드에서 읽은 값입니다. 바뀌면 문서도 같이 고칩니다.
- 각 문서는 "요약 → 이 문서를 읽고 나면 → 관련 코드 → 본문" 순서로 시작합니다.
