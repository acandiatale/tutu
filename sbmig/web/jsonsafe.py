"""실행 파일에서 읽은 값을 담는 JSON 응답과 SSE 이벤트에서 NaN과 무한대를 null로 바꿔 보내는 모듈.
- Starlette의 JSONResponse는 NaN을 거절해 500이 되고, json.dumps는 브라우저가 읽지 못하는 ``NaN``을 쓴다
- 실행 목록(``GET /api/runs``)은 FastAPI가 pydantic으로 직렬화해서 이미 null을 보낸다
- 먼저 그대로 직렬화하고 실패할 때만 값을 바꾸므로 보통 응답은 바이트도 비용도 그대로다"""
from __future__ import annotations

import json
import math
from typing import Any

from fastapi.responses import JSONResponse

# json이 유한하지 않은 float를 거절할 때의 메시지 앞부분. 순환 참조 같은 다른 ValueError는 그대로 던진다
_NON_FINITE_ERROR = "Out of range float values are not JSON compliant"


def finite_json(value: Any) -> Any:
    """dict, list, tuple 안의 유한하지 않은 float를 깊이와 상관없이 None으로 바꾼 값을 반환한다.
    다른 값과 키 순서는 그대로 둔다."""
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
    """``json.dumps``와 같은 문자열을 반환하되 유한하지 않은 float는 null로 쓴다.
    그런 값이 없으면 결과는 ``json.dumps``와 똑같다."""
    try:
        return json.dumps(value, **kw, allow_nan=False)
    except ValueError as e:
        if not _non_finite(e):
            raise
        return json.dumps(finite_json(value), **kw, allow_nan=False)


class FiniteJSONResponse(JSONResponse):
    """유한하지 않은 숫자를 null로 보내는 JSONResponse 클래스. 그런 값이 있을 때만 ``finite_json``으로 바꿔 직렬화한다."""

    def render(self, content: Any) -> bytes:
        try:
            return super().render(content)
        except ValueError as e:
            if not _non_finite(e):
                raise
            return super().render(finite_json(content))
