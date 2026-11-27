"""sbmig 웹 화면을 제공하는 패키지. 업로드한 zip을 이관하고 진행 상황을 SSE로 보여 준다.

The FastAPI app (``sbmig.web.app:app``) and the run manager (``sbmig.web.runs``), started by ``sbmig serve``
or ``uvicorn sbmig.web.app:app``. Migrations at a time: ``SBMIG_JOB_MAX_PARALLEL`` in Job mode (default 1),
one in-process. The others wait as ``queued`` in upload order."""
from .app import create_app
from .runs import RunManager

__all__ = ["RunManager", "create_app"]
