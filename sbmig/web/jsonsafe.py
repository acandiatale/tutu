"""파일에서 읽은 값을 담는 JSON 응답과 SSE 이벤트에서 NaN과 무한대를 null로 바꿔 보낸다.

Python의 json은 NaN / Infinity / -Infinity를 쓰고 읽지만 Starlette의 JSONResponse는 그 값을 거절해 500이 되고,
json.dumps는 브라우저가 읽지 못하는 NaN 낱말을 쓴다. 실행 목록(`GET /api/runs`)은 FastAPI가 pydantic으로
직렬화해 이미 null을 보낸다. 먼저 그대로 직렬화하고 실패할 때만 값을 바꾸므로 보통 응답의 바이트와 비용은 그대로다.

A JSON response / SSE payload for data read from run files: a non-finite float (NaN, Infinity, -Infinity, which
Python's json writes and reads back) is sent as null, as the run list already sends it, instead of the 500 that
Starlette's `JSONResponse` (`allow_nan=False`) raises or the bare `NaN` that `json.dumps` writes. The payload is
rendered with `allow_nan=False` first; only when that raises for a non-finite float is it walked and converted.
So a payload without such a value costs no extra pass and its bytes are, by construction, the ones sent before."""
from __future__ import annotations

import json
import math
from typing import Any

from fastapi.responses import JSONResponse

# json이 유한하지 않은 float를 거절할 때의 메시지 앞부분이다. 순환 참조 같은 다른 ValueError는 그대로 던진다
_NON_FINITE_ERROR = "Out of range float values are not JSON compliant"


def finite_json(value: Any) -> Any:
    """유한하지 않은 float를 None으로 바꾼 값을 반환한다. 다른 값과 순서는 그대로 둔다.

    `value` with every non-finite float (in dicts, lists and tuples, at any depth) replaced by None; anything else,
    key order included, is kept as it is."""
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

    `json.dumps(value, **kw)` with `allow_nan=False`; a non-finite float makes it walk `value` once (`finite_json`)
    and dump again, so the text has null where the plain dump had `NaN` / `Infinity`. Without such a value the text
    is exactly the plain dump's (allow_nan changes nothing for finite floats)."""
    try:
        return json.dumps(value, **kw, allow_nan=False)
    except ValueError as e:
        if not _non_finite(e):
            raise
        return json.dumps(finite_json(value), **kw, allow_nan=False)


class FiniteJSONResponse(JSONResponse):
    """유한하지 않은 숫자를 null로 보내는 JSONResponse.

    `JSONResponse` that renders as usual and, only when the content holds a non-finite float, renders
    `finite_json(content)` instead."""

    def render(self, content: Any) -> bytes:
        try:
            return super().render(content)
        except ValueError as e:
            if not _non_finite(e):
                raise
            return super().render(finite_json(content))
