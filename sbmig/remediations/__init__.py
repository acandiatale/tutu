"""취약점 조치(remediation) 기능 — 이관(migration)과는 별개의 LangGraph 흐름이다.
올린 Maven 프로젝트 ZIP 을 그대로 빌드하고(기준 빌드), IVAS(보안 취약점 점검)로 검사한 뒤, 취약한 라이브러리의
**버전 글자만** 고친 후보를 만들어 다시 빌드·재검사하고 결과 ZIP 과 보고서를 낸다. 애플리케이션 소스와 프로젝트 구조는
건드리지 않는다.

패키지 지도 (한 파일 = 한 책임):
    models.py      공통 자료형·상태·오류·해시 규칙 (계약; 다른 파일은 여기에만 기대어 서로 맞춘다)
    interfaces.py  갈아 끼우는 자리(Protocol): IVAS 클라이언트, Maven 게이트웨이, 버전 출처, 부품 묶음 Components
    intake.py      ZIP 검사·원본 보존·manifest·작업 트리, 서버 실행 경계 검사, 전달 ZIP 고정
    ivas.py        IVAS HTTP 클라이언트와 검사 절차(업로드 → 상태 → 요약 → 모든 페이지), 완전성 판정
    maven.py       서버가 관리하는 Maven 으로 package 빌드, 실제 해석 결과(dependency:tree)·패키징 결과 읽기
    inventory.py   IVAS 발견 항목을 Maven 해석 결과와 pom 선언 위치에 연결
    planner.py     버전 후보 선택(서버 정책·승인된 저장소 근거), 제안(proposal)과 승인 필요 여부, 해시
    apply.py       후보 사본에 버전 글자 변경 적용, "버전 글자만 바뀌었는가" / "해석 결과가 계획과 같은가" 검증
    graph.py       독립 LangGraph: baseline_build → baseline_scan → inventory → plan → apply → build →
                   freeze_delivery → rescan → evaluate → report (중단·승인 대기·반복 라운드)
    report.py      report.md / report.json
    service.py     작업 관리자: 작업 스레드, state.json, 승인·재개·중지·삭제, 재시작 뒤 INTERRUPTED
    api.py         FastAPI 라우터 /api/remediations…

Vulnerability remediation — a LangGraph flow separate from the migration graph. Only approved Maven version text
changes are applied; application sources and the project structure are never modified. Contract: `models.py` and
`interfaces.py`; `docs/INTERFACES.md` "remediations".
"""
