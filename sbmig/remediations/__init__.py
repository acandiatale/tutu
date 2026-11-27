"""취약점 조치 기능을 모은 패키지. 프로젝트를 IVAS로 검사하고 취약한 라이브러리의 버전을 올린다.
버전을 올린 뒤 다시 빌드하고 재검사한다.

Modules: ``models.py`` / ``interfaces.py`` (shared data classes, swappable parts), ``intake.py`` /
``ivas.py`` / ``maven.py`` (intake, IVAS scan, server-side Maven build), ``inventory.py`` / ``planner.py`` /
``apply.py`` (findings to dependencies, version choice, candidate apply and checks), ``fixer.py`` (the build
fix loop after a failed candidate build), ``graph.py`` / ``report.py`` (node flow, report), ``service.py`` /
``api.py`` (job management, HTTP API). The project structure is never changed.

Vulnerability remediation, a LangGraph flow separate from the migration graph: baseline_build -> baseline_scan
-> inventory -> plan -> apply -> build -> freeze_delivery -> rescan -> evaluate -> report. Only planned Maven
version text changes are applied (no approval step); the one source change is the build fix loop on a failed
candidate (`fixer.py`, "v2.1z-bi"). `fsops.py` retries renames / removals Windows refuses while another
program holds a file. Contract: `models.py` and `interfaces.py`; `docs/INTERFACES.md` "remediations"."""
