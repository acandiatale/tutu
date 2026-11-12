# agent-doc — 사내 LLM 에게 먹이는 sbmig 문서 묶음

> 다루는 것: 이 폴더의 쓰임새, 파일 목록, 일 종류별로 어떤 파일을 함께 넣어야 하는지, LLM 이 지켜야 할 규칙.
> 언제 읽나: sbmig 를 처음 만지는 사람과 그 사람이 쓰는 LLM 이 맨 먼저 읽는다.
> 함께 읽을 파일: [00-what-sbmig-is.md](00-what-sbmig-is.md) 를 항상 같이 넣는다.

## 이 묶음이 무엇인가

sbmig 는 사내 Maven(Ant 포함) 프로젝트를 **JDK 21 + Spring Boot 4.1** 로 옮기는 에이전트다. 이 폴더는 그 코드를 **코드를 직접 못 보는
사내 LLM**(Claude Sonnet / Haiku 급, GPT 급 — 채팅창이든 에이전트 프레임워크든) 에게 설명하기 위해 쓴 한국어 문서 묶음이다. 담당자가
이 파일들을 LLM 에 붙여 넣고 "이 실행이 왜 이렇게 끝났나", "이 규칙을 바꾸려면 어디를 고치나" 를 같이 보게 하는 용도다.

사람이 읽는 긴 설명서는 따로 있다. 설계서 `docs/migration-agent/README.md`, 결정 기록
`docs/v2/PLAN-v2.md` 와 `docs/PLAN.md`, 계약 `docs/INTERFACES.md`. 이 묶음은 그것들을 **LLM 한 번에 들어가는 크기**(파일마다 350 줄 /
30 KB 아래)로 다시 쓴 것이고, 모든 경로·설정 키·명령 이름이 코드와 맞는지 `tests/docs/test_agent_doc.py` 가 검사한다.

## 파일 목록

| 파일 | 한 줄 요약 | 크기 감 |
|---|---|---|
| [00-what-sbmig-is.md](00-what-sbmig-is.md) | sbmig 가 무엇이고 무엇까지 하는지, 흐름 한 장, 불변식, 용어표 | 항상 넣는다 |
| [01-repo-map.md](01-repo-map.md) | 폴더·패키지 지도, 진입점, "이 일을 하려면 이 파일" 표 | 코드를 고칠 때 |
| [02-flow-and-nodes.md](02-flow-and-nodes.md) | 실행 단계마다 코드 위치, 읽는 것·결정·남기는 것, 실패하는 방식 | 흐름을 따라갈 때 |
| [03-run-debug-and-logs.md](03-run-debug-and-logs.md) | 실행 명령, 실행 디렉터리 지도, 로그 줄, 저널 이벤트, 종료 코드, 진단 절차와 요약 카드 | 실행 결과를 볼 때 |
| [04-cases-scripts-kb.md](04-cases-scripts-kb.md) | 케이스(오류 서명)와 사다리, 채점, 스크립트 실행기, KB 파일, `sbmig kb` 명령 | 수정 루프·KB 를 볼 때 |
| [05-llm-gateway-and-prompts.md](05-llm-gateway-and-prompts.md) | LLM 공급자와 게이트웨이, `.env` 변수, 프롬프트와 답 형식, 노트·권고 | LLM 호출을 볼 때 |
| [06-company-rules-restructure.md](06-company-rules-restructure.md) | 회사 레이아웃·버전·동봉 jar·접수 규칙이 코드에 어떻게 들어 있는지 | 회사 규칙을 바꿀 때 |
| [07-config-and-knowledge.md](07-config-and-knowledge.md) | 설정 파일의 층과 절, 회사 파일 만드는 법, `knowledge/` 데이터 | 설정을 바꿀 때 |
| [08-dev-conventions-and-tests.md](08-dev-conventions-and-tests.md) | 코드를 고칠 때의 규약(한국어 주석, 불변식, YAML 우선), 테스트, 체크리스트 | 코드를 고칠 때 |
| [09-deploy-and-ops.md](09-deploy-and-ops.md) | PC·리눅스·컨테이너·쿠버네티스 두 모양, 플릿, KB 이동, 문제 보고 | 설치·운영할 때 |
| [10-open-items-and-history.md](10-open-items-and-history.md) | 지금까지 온 길, 회사 실행에서 겪은 사례, 열린 결정 | 배경이 필요할 때 |
| [11-remediations.md](11-remediations.md) | 이관이 BUILD SUCCESS 로 끝난 빌드 파드에서 이어 도는 취약점 조치(별도 그래프, `/api/remediations`, IVAS, 버전 글자만 수정): 노드, 상태별 할 일, 승인, 진단, 설정 키, 고칠 자리 | 조치 작업을 볼 때 |
| [12-remediation-report.md](12-remediation-report.md) | 조치 보고서의 절과 절마다 할 일, 사내 라이브러리 안내(담당자 몫으로 빠지는 항목), 제안이 다스리는 모듈, 영어 문장을 한국어로 보여 주는 규칙 | 조치 보고서를 읽거나 바꿀 때 |

새 주제는 다음 번호(13, 14 …)로 더하고 이 표와 아래 "일 종류별" 표에 한 줄씩 적는다. 날짜·버전 꼬리표는 여전히 10 번에만 쓴다.

## 일 종류별로 넣을 파일

| 하려는 일 | 넣을 파일 |
|---|---|
| 끝난 실행이 왜 그렇게 끝났는지 진단 | 00, 03, 04 (+ 실행 디렉터리의 `report.md`, `errors.md`, `sbmig.log` 발췌) |
| LLM 이 이상한 답을 한 이유 | 00, 05, 04 (+ `llm.record_dir` 의 기록 파일 하나) |
| 회사 규칙(부모 pom, 모듈, 버전, jar) 을 바꾸기 | 00, 06, 07, 08, 01 |
| 설정만 바꾸기 | 00, 07 (+ `config/default.yml` 의 해당 절 — 주석이 한국어 설명이다) |
| 코드 고치기 전 온보딩 | 00, 01, 02, 08 |
| 설치·배포·쿠버네티스 | 00, 09 (+ `deploy/README.md`) |
| "왜 이렇게 만들었나" 배경 | 00, 10 (+ `docs/v2/PLAN-v2.md`) |
| 취약점 조치 작업 진단·규칙 변경 | 00, 11 (+ 실행 폴더의 `state.json`, `journal.jsonl`, 검사 파일 발췌), 배포는 + 09 |
| 조치 보고서 읽기, 사내 라이브러리 안내·보고서 문장 변경 | 00, 11, 12 (+ 실행 폴더의 `remediation/report.md`) |

Haiku 급 모델은 한 번에 세 파일 안팎이 안전하고, Sonnet / GPT 급은 묶음 전체를 넣어도 된다.

## LLM 이 지켜야 할 규칙 (붙여 넣을 때 같이 준다)

1. **이 문서는 코드의 요약이고 코드가 진실이다.** 문서와 코드가 다르면 코드를 따르고, 다른 점을 보고한다. 각 파일 끝의
   "확인하는 법" 에 있는 명령으로 주장을 검증할 수 있다.
2. **버전과 좌표를 추측하지 않는다.** 라이브러리 버전은 부모 체인 → 목표 Boot BOM → as-is → 저장소 순으로 사실에서만 온다.
   사내 아티팩트 좌표는 문서 어디에도 실물이 없다(`com.company.platform:base-parent` 같은 자리표시자만). 실물은 회사 PC 의
   `config/artifacts.yml` 에만 있다.
3. **회사 고유값은 Python 코드가 아니라 `config/` 와 `knowledge/` 의 YAML 에 넣는다.** 폴더 이름으로 판단하는 규칙을 코드에
   넣지 않는다(내용 기반으로 판단한다).
4. **비밀값은 환경 변수(`.env`)에만 있다.** 키·토큰을 yml, 코드, 로그, 문서에 쓰지 않는다. `.env` 파일 내용을 출력하지 않는다.
5. **코드를 고치면** `sbmig/` 모듈과 공개 정의에 한국어 docstring 을 먼저 두고(영어 원문은 남긴다), 해당 폴더 테스트와
   `tests/docs` 를 돌리고, `docs/PLAN.md` 에 한 줄을 남긴다. 자세한 절차는 [08-dev-conventions-and-tests.md](08-dev-conventions-and-tests.md).
6. **테스트·데모 실행의 산출물은 `out/` 아래에만** 쓴다. 저장소 안의 KB 와 fixtures 를 실행으로 더럽히지 않는다.
7. **빌드가 끝나지 않은 수정을 "끝났다" 고 하지 않는다.** sbmig 의 모든 시도는 재빌드가 채점하고, 사람도 같은 기준을 쓴다.

## 이 묶음을 손보는 법

파일을 고치거나 더하면 `.venv/bin/python -m pytest tests/docs/test_agent_doc.py -p no:cacheprovider` 를 돌린다. 백틱 안의 저장소 경로가
실제로 있는지, `절.키` 모양의 설정 키가 진짜 키인지, `sbmig <명령>` 이 등록된 명령인지, 상대 링크가 열리는지, 파일 크기가 한도
안인지, 이 README 가 모든 파일을 적고 있는지 검사한다. 실행 시점 경로는 `out/<run id>/…`, `kb-v2/cases/<case_id>/…` 처럼
자리표시자로 쓴다. 날짜와 버전 꼬리표(v2.1z-… 등)는 10 번 파일에만 쓴다. 나머지는 "지금의 코드" 를 설명한다.
