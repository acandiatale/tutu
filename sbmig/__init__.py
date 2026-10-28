"""sbmig — 사내 JDK 21 + Spring Boot 4.1 이관 에이전트 (v2.1, 브랜치 ``flow-v2``). 인수인계용 패키지 지도.

흐름(자세한 설명: ``docs/v2/KB-AND-FLOW.md``): 접수(intake) → 분석(analyze) → 계획(plan) → 변환(OpenRewrite
1회) → 변환 후 컴파일 → 재구성(회사 레이아웃) → 구조 검토(LLM) → 검증 빌드(``mvn clean package``) → 수정
루프(케이스별 스크립트, 재빌드로 채점) → 최종 빌드 → 보고서 + 지식베이스(KB). 핵심 규칙: Maven 이 BUILD
SUCCESS 라고 해야 끝난다. 루프의 주인은 오케스트레이터이지 LLM 이 아니다.

패키지 지도 — 읽는 순서대로:
  cli.py / api.py   명령 진입점(``sbmig run …``, Typer)과 라이브러리 진입점(``sbmig.api.migrate``)
  core/             공유 계약: models.py(모든 레코드), config.py(설정 키·기본값), interfaces.py(플러그인 심)
  loop/             orchestrator.py(단계 실행 순서)와
  fixloop.py(수정 루프: 재사용 → 보강 → 신규, 채점, 되돌리기)
  graph/            위 루프를 LangGraph StateGraph 로 감싼
  것(``loop.engine: graph``; 웹 UI 가 쓰는 ``stream_graph``)
  analyze/          접수·프로필·권고 탐지(Maven 만, pom 없는 트리 거부, ``sbmig inventory``)
  plan/             v2 계획 단계 목록(planner.py), 레시피
  카탈로그(``knowledge/recipes/catalog.yml``), jar 확인
  rewrite/          OpenRewrite 1회 실행(레시피 yml 합성, BOM 3.37.0)
  restructure/      회사 레이아웃({name}-boot / -back / -front, 사내 부모 pom, 정적 리소스 복사, pom 편집기)
  review/           재구성 결과를 LLM 이 회사 규칙으로 검토(치명 항목만 스크립트로 수정)
  build/            Maven 실행, 빌드 로그 파서(L1 / L2 서명), 저장소 버전 조회·의존성 변경 표(v2.1d)
  triage/           오류를 케이스로 묶고 순서(근본 원인 먼저)·보류·차단 정책을 정한다
  fix/              LLM 에게 스크립트를 받는 쪽: 프롬프트
  문맥(context.py), 응답 프로토콜, 회사 게이트웨이(company.py)
  llm/              LLMRequest / LLMResponse 1단계 심(adapter.py),
  provider 등록(registry.py: company / fake / replay)
  script/           수정 스크립트 실행 런타임(정적 검사,
  ``python -I`` 자식 프로세스, 제한된 도우미 ``p``, 타임아웃)
  errors/           실행의 오류 목록(entries.py)과 케이스
  저장소(cases.py: ``cases/<id>/case.json`` + ``fix_v<N>.py``)
  kb/               KB 창구(store.py), ``sbmig kb …`` 명령(cli.py), 잠금, jsonl 안전 쓰기·복구, 비밀값 가리기
  advisory/         EOL 라이브러리 권고(안내만, 차단 없음)
  report/           보고서(markdown.py: 인수인계 섹션
  먼저), ``SBMIG-RESULT`` 한 줄, 한국어 문구(i18n.py), 비용
  fleet/            여러 프로젝트 순차 일괄 실행(``sbmig fleet``)
  web/              FastAPI + SSE 웹 UI(``sbmig serve``)
설정은 ``config/default.yml``(회사 값은 ``config/artifacts.*.yml``), 지식은 ``knowledge/``, KB 데이터는
``kb-v2/``. 코드의 한국어 주석 규약은 ``docs/v2/CODE-COMMENTS-KO.md``
(영어 원문은 그대로 두고 한국어를 앞에 더한다).

sbmig — in-house JDK 21 + Spring Boot 4.1 migration agent.

Pipeline (v2):  intake → analyze → plan → transform (one
OpenRewrite run, compile) → restructure → structure review
→ verify (mvn clean package) → script fix loop → final build → report + KB
Core rule: a phase is only "done" when `mvn` says BUILD SUCCESS. The orchestrator owns the loop, never the LLM.
"""

__version__ = "0.1.0"
