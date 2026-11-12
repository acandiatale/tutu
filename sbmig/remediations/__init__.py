"""취약점 조치 기능을 모은 패키지. 이관과는 별개인 LangGraph 흐름으로 실행된다.
업로드된 Maven 프로젝트 ZIP을 그대로 빌드하고 IVAS로 검사한 뒤, 취약한 라이브러리의 버전 문자열만 고친 수정본을 다시
빌드하고 재검사해서 결과 ZIP과 보고서를 만든다. 애플리케이션 소스와 프로젝트 구조는 건드리지 않는다.

패키지 구성:
models.py 공통 자료형, 상태, 오류, 해시 규칙. 다른 파일은 모두 이 정의를 따른다
interfaces.py 바꿔 넣을 수 있는 Protocol: IVAS 클라이언트, Maven 게이트웨이, 버전 출처, Components
intake.py ZIP 검사, 원본 보관, manifest, 작업 트리, 실행 환경 검사, 결과 ZIP 확정
ivas.py IVAS HTTP 클라이언트, 검사 절차, 검사 결과가 빠짐없는지 확인
maven.py package 빌드, dependency:tree와 패키징 결과 읽기
inventory.py IVAS가 찾은 취약점을 Maven 의존성과 pom 선언 위치에 연결
planner.py 버전 선택, 변경 제안과 승인 필요 여부, 해시
apply.py 수정본에 버전 변경 적용, 버전 문자열만 바뀌었는지와 의존성 결과 검증
graph.py baseline_build -> baseline_scan -> inventory -> plan -> apply ->
build -> freeze_delivery -> rescan -> evaluate -> report
    report.py      report.md / report.json
service.py 작업 관리자: 작업 스레드, state.json, 승인, 재개, 중지, 삭제, 재시작 뒤 INTERRUPTED 처리
api.py FastAPI 라우터 /api/remediations…

Vulnerability remediation — a LangGraph flow separate from the migration graph. Only approved Maven version text
changes are applied; application sources and the project structure are never modified. Contract: `models.py` and
`interfaces.py`; `docs/INTERFACES.md` "remediations".
"""
