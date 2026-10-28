"""sbmig 웹 UI: FastAPI 앱(``sbmig.web.app:app``) + 프로세스 안 실행 관리자(``sbmig.web.runs``).
흐름상 위치: ``sbmig serve`` 또는 ``uvicorn sbmig.web.app:app`` → 업로드한 zip 을 이 프로세스 안에서
LangGraph(``stream_graph``)로 이관하고, 진행을 SSE 로 브라우저(``static/index.html``)에 흘린다. 회사
규칙(2026-10-20): 에이전트는 uvicorn 아래 LangGraph 로 돈다. 핵심 진입점: :func:`create_app`,
:class:`RunManager`. 이관은 한 번에 하나(파일 KB 를 함께 쓰므로), 그 사이에 올린 것은 ``queued`` 로 기다린다.

sbmig web UI: FastAPI app (`sbmig.web.app:app`, served by uvicorn) + in-process run manager (`sbmig.web.runs`)."""
from .app import create_app
from .runs import RunManager

__all__ = ["RunManager", "create_app"]
