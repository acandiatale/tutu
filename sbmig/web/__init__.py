"""sbmig 웹 화면을 제공하는 패키지.
FastAPI 앱(``sbmig.web.app:app``)과 실행 관리자(``sbmig.web.runs``)로 구성된다.
``sbmig serve`` 또는 ``uvicorn sbmig.web.app:app``으로 띄우며, 업로드한 zip을 이관하고 진행 상황을 SSE로 보여 준다.
- 동시에 실행하는 이관 수는 Job 모드에서 ``SBMIG_JOB_MAX_PARALLEL``개(기본 1), in-process에서 1개다
- 나머지는 ``queued`` 상태로 업로드한 순서대로 기다린다"""
from .app import create_app
from .runs import RunManager

__all__ = ["RunManager", "create_app"]
