"""취약점 조치 기능을 모은 패키지. 이관과 별개인 LangGraph 흐름으로 실행된다.
업로드된 프로젝트를 빌드하고 IVAS로 검사한 뒤, 취약한 라이브러리의 버전 문자열만 고쳐서 다시 빌드하고 재검사한다.
프로젝트 구조는 건드리지 않는다. 소스는 버전을 올린 뒤 빌드가 깨졌을 때 수정 루프만 고친다.
- ``models.py``, ``interfaces.py``: 공통 데이터 클래스와 바꿔 넣을 수 있는 구성 요소
- ``intake.py``, ``ivas.py``, ``maven.py``: 접수, IVAS 검사, 서버 Maven 빌드
- ``inventory.py``, ``planner.py``, ``apply.py``: 취약점과 의존성 연결, 버전 선택, 수정본 적용과 검증
- ``fixer.py``: 수정본 빌드가 실패했을 때의 빌드 오류 수정 루프
- ``graph.py``, ``report.py``: 노드 흐름과 보고서
- ``service.py``, ``api.py``: 작업 관리와 HTTP API

Vulnerability remediation, a LangGraph flow separate from the migration graph: baseline_build -> baseline_scan
-> inventory -> plan -> apply -> build -> freeze_delivery -> rescan -> evaluate -> report. Only planned Maven
version text changes are applied (no approval step); the one source change is the build fix loop on a failed
candidate (`fixer.py`, "v2.1z-bi"). `fsops.py` retries renames / removals Windows refuses while another
program holds a file. Contract: `models.py` and `interfaces.py`; `docs/INTERFACES.md` "remediations"."""
