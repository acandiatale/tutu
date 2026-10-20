"""sbmig web UI: FastAPI app (`sbmig.web.app:app`, served by uvicorn) + in-process run manager (`sbmig.web.runs`)."""
from .app import create_app
from .runs import RunManager

__all__ = ["RunManager", "create_app"]
