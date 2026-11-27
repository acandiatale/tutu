"""JSON 응답과 SSE 이벤트를 보낼 때 NaN과 무한대를 null로 바꾸는 모듈.
브라우저는 NaN이 들어 있는 JSON을 읽지 못한다.

Starlette's JSONResponse refuses NaN (the answer becomes a 500) and json.dumps writes a bare ``NaN``. The
run list (``GET /api/runs``) is serialized by FastAPI through pydantic and already sends null. Serialization
is tried as it is first and the values are replaced only when it fails, so an ordinary answer keeps its
bytes and cost.
"""
from __future__ import annotations

import json
import math
from typing import Any

from fastapi.responses import JSONResponse

# json이 NaN, 무한대를 거절할 때 내는 메시지의 앞부분. 이 메시지가 아닌 ValueError는 그대로 던진다
_NON_FINITE_ERROR = "Out of range float values are not JSON compliant"


def finite_json(value: Any) -> Any:
    """값 안의 NaN과 무한대를 None으로 바꿔서 반환한다.
    dict, list, tuple은 안쪽까지 모두 바꾼다."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: finite_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [finite_json(v) for v in value]
    return value


def _non_finite(e: ValueError) -> bool:
    return str(e).startswith(_NON_FINITE_ERROR)


def finite_dumps(value: Any, **kw: Any) -> str:
    """``json.dumps``처럼 JSON 문자열을 만든다. NaN과 무한대만 null로 바꾼다."""
    try:
        return json.dumps(value, **kw, allow_nan=False)
    except ValueError as e:
        if not _non_finite(e):
            raise
        return json.dumps(finite_json(value), **kw, allow_nan=False)


class FiniteJSONResponse(JSONResponse):
    """NaN과 무한대를 null로 바꿔 보내는 JSONResponse 클래스."""

    def render(self, content: Any) -> bytes:
        try:
            return super().render(content)
        except ValueError as e:
            if not _non_finite(e):
                raise
            return super().render(finite_json(content))
