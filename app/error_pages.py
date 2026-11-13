"""Same-origin browser errors with the gateway's JSON responses preserved."""

from __future__ import annotations

import re
import secrets

from fastapi import Request
from fastapi.responses import JSONResponse

from app.security import apply_security_headers
from app.web.templating import templates


BROWSER_ERROR_CODES = {403, 404, 500, 502, 503, 504}


def is_api_path(path: str) -> bool:
    return path.startswith(("/v1/", "/api/")) or path.rstrip("/") in {
        "/v1", "/api", "/health", "/ready", "/developer/playground/run", "/developer/playground/models",
    }


def _accepts_html(request: Request) -> bool:
    accept = request.headers.get("accept", "").lower()
    if "application/json" in accept:
        return False
    for media_range in accept.split(","):
        media_type, *parameters = media_range.split(";")
        if media_type.strip() == "text/html" and not any(
            re.fullmatch(r"q\s*=\s*0(?:\.0{0,3})?", parameter.strip()) for parameter in parameters
        ):
            return True
    return False


def _api_exception(request: Request, status_code: int = 500) -> JSONResponse:
    request_id = request.headers.get("x-request-id", "").strip()
    if not request_id or len(request_id) > 128 or any(ord(char) < 0x20 or ord(char) == 0x7F for char in request_id):
        request_id = secrets.token_hex(16)
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": "Internal server error.", "type": "server_error", "code": "gateway_error"}},
        headers={"X-Request-ID": request_id},
    )


async def error_page_middleware(request: Request, call_next):
    try:
        response = await call_next(request)
    except Exception:
        response = _api_exception(request)
    if request.method not in {"GET", "HEAD"} or is_api_path(request.url.path) or response.status_code not in BROWSER_ERROR_CODES:
        return response

    if _accepts_html(request):
        response = templates.TemplateResponse(
            request,
            "browser_error.html",
            {"status_code": response.status_code},
            status_code=response.status_code,
            headers={"Cache-Control": "no-store"},
        )
        apply_security_headers(request, response)
    response.headers.add_vary_header("Accept")
    return response
