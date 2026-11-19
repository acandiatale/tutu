"""The one helper the web tests use to address `POST /api/runs` (v2.1z-be: the route requires `project`).

`runs_url("name=p.zip&configs=…")` → `/api/runs?name=p.zip&configs=…&project=order-admin`. A test about the refusal
passes `project=None` (no parameter) or its own value explicitly."""
from __future__ import annotations

from urllib.parse import quote

PROJECT = "order-admin"


def runs_url(query: str = "", project: str | None = PROJECT) -> str:
    """The upload address with the project name the route requires (None = leave the parameter out)."""
    parts = [query] if query else []
    if project is not None:
        parts.append("project=" + quote(project, safe=""))
    return "/api/runs" + ("?" + "&".join(parts) if parts else "")
