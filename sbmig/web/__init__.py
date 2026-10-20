"""sbmig serve: one-page web UI + SSE over `sbmig run` subprocesses (sbmig.web.server)."""
from .server import RunManager, RunSpec, default_command, make_server, serve

__all__ = ["RunManager", "RunSpec", "default_command", "make_server", "serve"]
