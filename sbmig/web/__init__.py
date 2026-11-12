"""sbmig 웹 화면을 제공하는 패키지.
FastAPI 앱(``sbmig.web.app:app``)과 실행 관리자(``sbmig.web.runs``)로 구성된다.
``sbmig serve`` 또는 ``uvicorn sbmig.web.app:app``으로 띄우며, 업로드한 zip을 LangGraph(``stream_graph``)로
이관하고 진행 상황을 SSE로 브라우저(``static/index.html``)에 보낸다.
진입점은 :func:`create_app`과 :class:`RunManager`다.
동시에 실행하는 이관 수는 Job 모드(k8s-job / local-job)에서 ``SBMIG_JOB_MAX_PARALLEL``개(기본 1), 프로세스
안 실행에서 1개다. 나머지는 ``queued`` 상태로 업로드한 순서대로 기다린다.

sbmig web UI: FastAPI app (`sbmig.web.app:app`, served by uvicorn) + in-process run manager (`sbmig.web.runs`). Up to
`SBMIG_JOB_MAX_PARALLEL` migrations at a time in Job mode (default 1), one at a time in-process; the rest wait in upload order."""
from .app import create_app
from .runs import RunManager

__all__ = ["RunManager", "create_app"]
