"""사내 JDK 21 + Spring Boot 4.1 이관 에이전트 sbmig의 최상위 패키지. 아래에 인수인계용 패키지 지도를 둔다.

흐름: 접수(intake) -> 분석(analyze) -> 계획(plan) -> 변환(OpenRewrite 1회) -> 재구성(회사 레이아웃) -> 구조
검토(LLM) -> 검증 빌드(``mvn clean package``) -> 수정 루프(케이스별 스크립트, 재빌드로 결과 평가) -> 최종
빌드 -> 보고서와 지식베이스(KB). 자세한 설명은 ``docs/migration-agent/README.md``에 있다.
핵심 규칙: Maven이 BUILD SUCCESS를 내야 끝난다. 루프를 이끄는 것은 오케스트레이터이고 LLM이 아니다.

패키지 지도(읽는 순서):
- cli.py / api.py: 명령 진입점(``sbmig run …``, Typer)과 라이브러리 진입점(``sbmig.api.migrate``)
- core/: 공유 계약. models.py(모든 레코드), config.py(설정 키와 기본값), interfaces.py(플러그인 인터페이스)
- loop/: orchestrator.py(단계 실행 순서)와 fixloop.py(수정 루프: 재사용 -> 보강 -> 신규, 결과 평가, 되돌리기)
- graph/: 위 루프를 LangGraph StateGraph로 감싼 엔진(``loop.engine: graph``, 웹 UI가 쓰는 ``stream_graph``)
- analyze/: 접수, 프로필, 권고 탐지(Maven만 지원, pom 없는 트리 거부, ``sbmig inventory``)
- plan/: 계획 단계 목록(planner.py), 레시피 카탈로그(``knowledge/recipes/catalog.yml``), jar 확인
- rewrite/: OpenRewrite 1회 실행(레시피 yml 생성, BOM 3.37.0)
- restructure/: 회사 레이아웃({name}-boot / -back / -front, 사내 부모 pom, 정적 리소스 복사, pom 편집기)
- review/: 재구성 결과를 LLM이 회사 규칙으로 검토(치명 항목만 수정)
- build/: Maven 실행, 빌드 로그 파서(L1 / L2 서명), 저장소 버전 조회와 의존성 변경 표
- triage/: 오류를 케이스로 묶고 순서(근본 원인 먼저), 건너뛰기, 차단 정책을 정한다
- fix/: LLM에게 스크립트를 받는 쪽. 프롬프트 문맥(context.py), 응답 프로토콜, 회사 게이트웨이(company.py)
- llm/: LLMRequest / LLMResponse 1단계 인터페이스(adapter.py), provider 등록(registry.py: company / fake / replay)
- script/: 수정 스크립트 실행 런타임(정적 검사, ``python -I`` 자식 프로세스, 제한된 도우미 ``p``, 타임아웃)
- errors/: 실행의 오류 목록(entries.py)과 케이스 저장소(cases.py: ``cases/<id>/case.json``과 ``fix_v<N>.py``)
- kb/: KB 파사드(store.py), ``sbmig kb …`` 명령(cli.py), 잠금, jsonl 안전 쓰기와 복구, 비밀 값 마스킹
- advisory/: EOL 라이브러리 권고(안내만, 차단 없음)
- report/: 보고서(markdown.py: 인수인계 섹션 먼저), ``SBMIG-RESULT`` 한 줄, 한국어 문구(i18n.py), 비용
- fleet/: 여러 프로젝트를 차례로 처리하는 일괄 실행(``sbmig fleet``)
- web/: FastAPI와 SSE로 만든 웹 UI(``sbmig serve``)
- remediations/: 취약점 조치 흐름. 사내 검사 도구 IVAS로 취약한 라이브러리 버전을 찾아 버전 변경을 제안하고, 승인된 변경을 적용한 뒤 다시 빌드하고 재검사한다

설정은 ``config/default.yml``(회사 값은 ``config/artifacts.*.yml``), 지식은 ``knowledge/``, KB 데이터는
``kb-v2/``에 있다. 한국어 주석 규약은 ``docs/v2/CODE-COMMENTS-KO.md``에 있다.

sbmig — in-house JDK 21 + Spring Boot 4.1 migration agent.

Pipeline (v2):  intake → analyze → plan → transform (one
OpenRewrite run, compile) → restructure → structure review
→ verify (mvn clean package) → script fix loop → final build → report + KB
Core rule: a phase is only "done" when `mvn` says BUILD SUCCESS. The orchestrator owns the loop, never the LLM.
"""

__version__ = "0.1.0"
