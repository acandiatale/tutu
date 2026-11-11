"""취약점 조치 HTTP 라우터 ``/api/remediations…`` — 기존 웹 앱(같은 프로세스, 같은 TokenGuard)에 붙는다.
흐름상 위치: ``sbmig/web/app.py`` 의 ``create_app`` 이 :func:`create_router` 를 포함한다 → :class:`~sbmig.remediations.service.RemediationManager`.
업로드는 ``/api/runs`` 와 같은 방식으로 메모리에 올리지 않고 임시 파일로 흘려 쓰며 조각마다 상한(``remediation.max_upload_mb``)과
ZIP 서명(PK)을 본다. 막히는 일(ZIP 검사·상태 파일 읽기)은 스레드풀에서 돈다 — 이벤트 루프를 막지 않는다. 오류는 늘
``{"error", "code", …}`` 모양이고 비밀 값·서버 절대 경로는 응답에 넣지 않는다. 계약: API.md (``docs/INTERFACES.md`` "remediations").

The `/api/remediations` router (contract: API.md). Error mapping: IntakeRejected → 400, NotReady → 503,
ApprovalMismatch / StateConflict / NotAvailable → 409, unknown id → 404, bad id → 400.
Job 모드(k8s-job / local-job)의 웹 파드는 조치를 돌리지 않는다: 단독 업로드는 본문을 받기 전에 409 ``not_available``,
이어 돈 조치(``<out>/<이관 실행 id>/remediation/``)는 같은 라우트로 읽고 승인한다.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import anyio
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from .models import ApprovalMismatch, Decision, IntakeRejected, NotReady, StateConflict
from .service import BadRemediationId, NotAvailable, RemediationManager, UnknownRemediation

log = logging.getLogger("sbmig.remediations.api")

# 응답에 실어도 되는 오류 세부 항목(그 밖의 details 는 내보내지 않는다 — 경로·내부 값이 섞이지 않게)
_SAFE_DETAILS = ("missing", "fields", "proposals", "status", "pending", "proposal_id", "mode")
_MEDIA = {".md": "text/markdown; charset=utf-8", ".json": "application/json", ".zip": "application/zip",
          ".jsonl": "application/x-ndjson", ".log": "text/plain; charset=utf-8", ".patch": "text/plain; charset=utf-8"}
_ATTACH = (".zip", ".jsonl", ".patch")


def error_response(exc: BaseException) -> JSONResponse:
    """오류 → HTTP 응답 ``{"error", "code", …}``. 알려진 세부 항목만 싣는다.

    Map an exception of this feature to its HTTP response (contract error shape)."""
    if isinstance(exc, (BadRemediationId,)):
        status = 400
    elif isinstance(exc, UnknownRemediation):
        status = 404
    elif isinstance(exc, IntakeRejected):
        status = 400
    elif isinstance(exc, NotReady):
        status = 503
    elif isinstance(exc, (ApprovalMismatch, StateConflict, NotAvailable)):
        status = 409
    elif isinstance(exc, ValueError):
        return JSONResponse({"error": str(exc), "code": "bad_request"}, status_code=400)
    else:
        log.error("remediation request failed: %s", type(exc).__name__)
        return JSONResponse({"error": "internal error", "code": "internal_error"}, status_code=500)
    body: dict[str, Any] = {"error": str(exc), "code": getattr(exc, "code", "error")}
    details = getattr(exc, "details", {}) or {}
    for k in _SAFE_DETAILS:
        if k in details:
            body[k] = details[k]
    return JSONResponse(body, status_code=status)


async def _call(fn: Any, *args: Any, **kw: Any) -> Any:
    return await run_in_threadpool(fn, *args, **kw)


async def _json_body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        raise ValueError("body is not valid JSON") from None
    if not isinstance(data, dict):
        raise ValueError("body must be a JSON object")
    return data


def create_router(manager: RemediationManager, max_upload_bytes: int | None = None) -> APIRouter:
    """``/api/remediations`` 라우터를 만든다. ``max_upload_bytes`` 가 None 이면 관리자의 기본 설정
    (``remediation.max_upload_mb``)에서, 0 = 상한 없음.

    Build the router; the upload limit defaults to the manager's `remediation.max_upload_mb` (0 = no limit)."""
    limit = manager.max_upload_bytes() if max_upload_bytes is None else int(max_upload_bytes)
    r = APIRouter(prefix="/api/remediations")

    def too_large() -> JSONResponse:
        return JSONResponse({"error": f"zip too large (max {limit // (1024 * 1024)} MiB)", "code": "too_large"},
                            status_code=413)

    @r.post("", status_code=202)
    async def submit(request: Request, name: str = "project.zip", configs: str | None = None) -> JSONResponse:
        if manager.job_mode:          # Job 모드: 본문(1 GiB 가까운 ZIP)을 받기 전에 거절한다 — 조치는 빌드 파드에서 돈다
            return error_response(NotAvailable(manager.readiness()[0], mode=manager.mode))
        length = request.headers.get("content-length")
        if limit and length and length.isdigit() and int(length) > limit:
            return too_large()
        # 본문은 메모리에 올리지 않고 임시 파일로 흘려 쓴다(1 GiB 가까운 ZIP) — 조각마다 상한과 ZIP 서명을 본다
        tmp = manager.incoming_path()
        size, head = 0, b""
        try:
            async with await anyio.open_file(tmp, "wb") as f:
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    if len(head) < 2:
                        head = (head + chunk)[:2]
                        if len(head) == 2 and head != b"PK":
                            return JSONResponse({"error": "body is not a zip file", "code": "not_zip"}, status_code=400)
                    size += len(chunk)
                    if limit and size > limit:
                        return too_large()
                    await f.write(chunk)
            if size == 0:
                return JSONResponse({"error": "zip body required", "code": "empty"}, status_code=400)
            if head != b"PK":
                return JSONResponse({"error": "body is not a zip file", "code": "not_zip"}, status_code=400)
            cfgs = None if configs is None else [c for c in configs.split(",") if c]
            try:
                st = await _call(manager.submit, tmp, name, cfgs)
            except Exception as e:  # noqa: BLE001 — mapped to the contract's error shape
                return error_response(e)
            return JSONResponse({"id": st.id, "status": st.status, "url": f"/api/remediations/{st.id}"},
                                status_code=202)
        finally:
            tmp.unlink(missing_ok=True)

    @r.get("")
    async def listing(page: int = 1, size: int = 20) -> JSONResponse:
        try:
            return JSONResponse(await _call(manager.list, page, size))
        except Exception as e:  # noqa: BLE001
            return error_response(e)

    # /{id} 보다 먼저 선언해야 "readiness" 가 ID 로 잡히지 않는다
    @r.get("/readiness")
    async def readiness() -> JSONResponse:
        return JSONResponse(await _call(manager.readiness_info))

    @r.get("/{rid}")
    async def status(rid: str) -> JSONResponse:
        try:
            return JSONResponse(await _call(manager.status, rid))
        except Exception as e:  # noqa: BLE001
            return error_response(e)

    @r.get("/{rid}/result")
    async def result(rid: str) -> JSONResponse:
        try:
            return JSONResponse(await _call(manager.result, rid))
        except Exception as e:  # noqa: BLE001
            return error_response(e)

    @r.get("/{rid}/proposals")
    async def proposals(rid: str, all: str | None = None) -> JSONResponse:
        every = (all or "").strip().lower() in ("1", "true", "yes", "on")
        try:
            return JSONResponse(await _call(manager.proposals, rid, every))
        except Exception as e:  # noqa: BLE001
            return error_response(e)

    @r.post("/{rid}/approvals")
    async def approvals(rid: str, request: Request) -> JSONResponse:
        try:
            body = await _json_body(request)
            by = str(body.get("by") or "").strip()
            items = body.get("decisions")
            if not isinstance(items, list) or not all(isinstance(x, dict) for x in items):
                raise ValueError("decisions must be a list of objects")
            decisions = [Decision.from_dict({**x, "by": by}) for x in items]
            return JSONResponse(await _call(manager.decide, rid, decisions, by))
        except TypeError:
            return JSONResponse({"error": "each decision needs proposal_id and decision", "code": "bad_request"},
                                status_code=400)
        except Exception as e:  # noqa: BLE001
            return error_response(e)

    @r.post("/{rid}/resume", status_code=202)
    async def resume(rid: str, request: Request) -> JSONResponse:
        try:
            body = await _json_body(request)
            kw = {k: (str(body[k]) if body.get(k) is not None else None)
                  for k in ("confirm", "label", "project_id", "analysis_id")}
            st = await _call(manager.resume, rid, kw["confirm"], kw["label"], kw["project_id"], kw["analysis_id"],
                             str(body.get("by") or ""))
            return JSONResponse(st, status_code=202)
        except Exception as e:  # noqa: BLE001
            return error_response(e)

    @r.post("/{rid}/stop", status_code=202)
    async def stop(rid: str) -> JSONResponse:
        try:
            return JSONResponse(await _call(manager.stop, rid), status_code=202)
        except Exception as e:  # noqa: BLE001
            return error_response(e)

    @r.get("/{rid}/files/{name}")
    async def files(rid: str, name: str) -> Response:
        try:
            path = await _call(manager.file_path, rid, name)
        except Exception as e:  # noqa: BLE001
            return error_response(e)
        if path is None:
            return JSONResponse({"error": f"{name} not found", "code": "not_found"}, status_code=404)
        ext = path.suffix.lower()
        attach = ext in _ATTACH
        return FileResponse(path, media_type=_MEDIA.get(ext, "application/octet-stream"),
                            headers={"Cache-Control": "no-store"}, filename=name if attach else None,
                            content_disposition_type="attachment" if attach else "inline")

    @r.delete("/{rid}", status_code=204)
    async def delete(rid: str) -> Response:
        try:
            await _call(manager.delete, rid)
        except Exception as e:  # noqa: BLE001
            return error_response(e)
        return Response(status_code=204)

    return r


__all__ = ["create_router", "error_response"]
