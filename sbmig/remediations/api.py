"""취약점 조치 HTTP API(``/api/remediations…``)를 제공하는 모듈.
웹 앱의 ``create_app``이 :func:`create_router`를 붙이므로 같은 프로세스와 TokenGuard를 쓴다.
- 업로드는 임시 파일로 받으면서 크기 상한(``remediation.max_upload_mb``)과 ZIP 서명을 확인한다
- ZIP 검사와 상태 파일 읽기는 이벤트 루프를 막지 않도록 스레드풀에서 실행한다
- 오류 응답은 ``{"error", "code", …}`` 형태이고 비밀 값과 서버 절대 경로는 넣지 않는다
- Job 모드에서는 단독 업로드를 본문을 받기 전에 409 ``not_available``로 거절한다

The `/api/remediations` router (contract: API.md). In Job mode a remediation chained after a migration is read
through the same routes; a resume runs in a new build pod. Error mapping: IntakeRejected → 400, NotReady →
503, StateConflict / NotAvailable → 409, unknown id → 404, bad id → 400. A 409 carries `message_ko` when the
service gave one (the refused resume of a state saved in `AWAITING_APPROVAL`). `GET …/files/{name}?download=1`
attaches any file; report.md / report.json are then named remediation-report.md / remediation-report.json.
`result.zip` is attached as `<project>-remediation.zip` (`RemediationManager.zip_name`, v2.1z-bt); every other
file keeps its name."""
from __future__ import annotations

import json
import logging
from typing import Any

import anyio
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from .models import IntakeRejected, NotReady, StateConflict
from .service import BadRemediationId, NotAvailable, RemediationManager, UnknownRemediation

log = logging.getLogger("sbmig.remediations.api")

# 경로나 내부 값이 응답에 섞이지 않도록 이 목록에 있는 세부 항목만 내보낸다
_SAFE_DETAILS = ("missing", "status", "mode", "message_ko")
_MEDIA = {".md": "text/markdown; charset=utf-8", ".json": "application/json", ".zip": "application/zip",
          ".jsonl": "application/x-ndjson", ".log": "text/plain; charset=utf-8", ".patch": "text/plain; charset=utf-8"}
_ATTACH = (".zip", ".jsonl", ".patch")
# 내려받을 때 쓰는 이름. 작업 폴더 안의 파일 이름과 주소는 그대로 둔다
DOWNLOAD_NAMES = {"report.md": "remediation-report.md", "report.json": "remediation-report.json"}


def error_response(exc: BaseException) -> JSONResponse:
    """예외를 ``{"error", "code", …}`` 형태의 HTTP 응답으로 바꾼다. 세부 항목은 공개해도 되는 것만 넣는다."""
    if isinstance(exc, (BadRemediationId,)):
        status = 400
    elif isinstance(exc, UnknownRemediation):
        status = 404
    elif isinstance(exc, IntakeRejected):
        status = 400
    elif isinstance(exc, NotReady):
        status = 503
    elif isinstance(exc, (StateConflict, NotAvailable)):
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
    """``/api/remediations`` 라우터를 만든다.
    ``max_upload_bytes``가 None이면 ``remediation.max_upload_mb``를 쓴다. 0이면 크기 제한이 없다."""
    limit = manager.max_upload_bytes() if max_upload_bytes is None else int(max_upload_bytes)
    r = APIRouter(prefix="/api/remediations")

    def too_large() -> JSONResponse:
        return JSONResponse({"error": f"zip too large (max {limit // (1024 * 1024)} MiB)", "code": "too_large"},
                            status_code=413)

    @r.post("", status_code=202)
    async def submit(request: Request, name: str = "project.zip", configs: str | None = None) -> JSONResponse:
        if manager.job_mode:          # Job 모드는 빌드 파드에서 조치하므로 큰 본문을 받기 전에 거절한다
            return error_response(NotAvailable(manager.readiness()[0], mode=manager.mode))
        length = request.headers.get("content-length")
        if limit and length and length.isdigit() and int(length) > limit:
            return too_large()
        # 본문이 1 GiB에 가까울 수 있으므로 임시 파일로 받으면서 크기 상한과 ZIP 서명을 확인한다
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
            except Exception as e:  # noqa: BLE001 - 정해진 오류 응답 형태로 바꾼다
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

    # /{rid}보다 먼저 선언해야 readiness가 ID로 해석되지 않는다
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
    async def files(rid: str, name: str, download: str | None = None) -> Response:
        try:
            path = await _call(manager.file_path, rid, name)
        except Exception as e:  # noqa: BLE001
            return error_response(e)
        if path is None:
            return JSONResponse({"error": f"{name} not found", "code": "not_found"}, status_code=404)
        ext = path.suffix.lower()
        # ?download=1이면 어떤 파일이든 첨부 파일로 보낸다
        attach = ext in _ATTACH or download is not None
        filename = DOWNLOAD_NAMES.get(name, name) if attach else None
        if attach and name == "result.zip":
            # 결과 ZIP은 프로젝트명으로 받는다. 이름을 정하지 못하면 result.zip 그대로 보낸다
            try:
                filename = await _call(manager.zip_name, rid)
            except Exception:  # 표시용 이름이므로 정하지 못해도 내려받기는 실패시키지 않는다
                log.debug("remediation %s: download name of result.zip not computed", rid, exc_info=True)
        return FileResponse(path, media_type=_MEDIA.get(ext, "application/octet-stream"),
                            headers={"Cache-Control": "no-store"},
                            filename=filename,
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
