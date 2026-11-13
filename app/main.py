import asyncio
import mimetypes
from collections import OrderedDict
from contextlib import asynccontextmanager, closing
from urllib.parse import parse_qs

import httpx
from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.staticfiles import StaticFiles
from app.auth import authenticate_user  # Compatibility patch seam.
from app.email_verification import send_verification_email  # Compatibility patch seam.
from app.config import settings
from app.db import connect, init_db
from app.http_clients import create_http_client
from app.security import security_headers
from app.error_redirect import error_redirect_middleware
from app.self_service_keys import init_self_key_schema
from app.login_throttle import init_login_throttle_schema
from app.credit_payments import initialize_payment_schema, payment_context
from app.client_ip import forwarded_https
from app.web.templating import STATIC_DIR, fingerprint, templates
from scripts.seed_default_apis import seed
from app.routes import register_routers

mimetypes.add_type("font/woff2", ".woff2")

class CachedStaticFiles(StaticFiles):
    """Templates reference assets with a content hash (?v=…), so a matching response is immutable."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            requested = parse_qs(scope.get("query_string", b"").decode("latin-1")).get("v", [])
            try:
                current = fingerprint(path.replace("\\", "/"))
            except ValueError:
                current = None
            immutable = current is not None and requested == [current]
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable" if immutable else "public, max-age=3600"
        return response


async def restrict_codexproxy_hostname(request: Request, call_next):
    host = request.headers.get("host", "").split(":", 1)[0].lower()
    if host == "cli.luomo.moe" and request.url.path != "/health" and not request.url.path.startswith("/v1/"):
        from app.utils.errors import json_error
        from app.public_gateway_secure import request_id_for
        return json_error(404, "not_found", "Not Found", request_id=request_id_for(request), openai=True)
    return await call_next(request)


async def redirect_api_http_to_https(request: Request, call_next):
    host = request.headers.get("host", "").split(":", 1)[0].lower()
    forwarded_proto = request.headers.get("x-forwarded-proto", "").lower()
    cf_visitor = request.headers.get("cf-visitor", "").lower()
    is_http = forwarded_proto == "http" or '"scheme":"http"' in cf_visitor
    if host in settings.https_api_hosts and is_http:
        target = request.url.replace(scheme="https")
        return RedirectResponse(str(target), status_code=308)
    return await call_next(request)


async def trust_forwarded_https(request: Request, call_next):
    if request.scope.get("scheme") == "http" and forwarded_https(request):
        request.scope["scheme"] = "https"
    return await call_next(request)


def _browser_form_submission(request: Request) -> bool:
    """A console form posted by a browser navigation; API clients and fetch() keep JSON errors."""
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return False
    if request.url.path.startswith(("/v1/", "/api/")) or request.url.path in {"/v1", "/api"}:
        return False
    return "text/html" in request.headers.get("accept", "").lower()


def _form_error_page(request: Request, status_code: int, message: str, headers=None):
    response = templates.TemplateResponse(request, "error.html", {"status_code": status_code, "message": message},
                                          status_code=status_code, headers=headers)
    response.headers["Cache-Control"] = "no-store"
    return response


async def form_http_error(request: Request, exc: StarletteHTTPException):
    if exc.status_code >= 400 and _browser_form_submission(request):
        detail = exc.detail if isinstance(exc.detail, str) and exc.detail else "请求没有完成，请返回上一页检查后重试。"
        return _form_error_page(request, exc.status_code, detail, getattr(exc, "headers", None))
    return await http_exception_handler(request, exc)


async def form_validation_error(request: Request, exc: RequestValidationError):
    if _browser_form_submission(request):
        return _form_error_page(request, 422, "表单内容不完整或格式不正确，请返回上一页检查必填项后重试。")
    return await request_validation_exception_handler(request, exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.session_secret or len(settings.session_secret) < 32:
        raise RuntimeError("SESSION_SECRET must contain at least 32 characters.")
    init_db()
    seed()
    with closing(connect()) as conn, conn:
        init_self_key_schema(conn)
        init_login_throttle_schema(conn)
        initialize_payment_schema(conn)
        conn.commit()
    app.state.credit_payment_context = payment_context
    app.state.public_upstream_clients = OrderedDict()
    app.state.public_upstream_clients_active = {}
    app.state.public_upstream_clients_lock = asyncio.Lock()
    app.state.codex_http_client = create_http_client(httpx.Timeout(connect=15.0, read=120.0, write=60.0, pool=15.0))

    try:
        yield
    finally:
        for client in list(getattr(app.state, "public_upstream_clients", {}).values()):
            await client.aclose()
        client = getattr(app.state, "codex_http_client", None)
        if client:
            await client.aclose()


def create_app() -> FastAPI:
    application = FastAPI(title=settings.app_name, docs_url=None, redoc_url=None, lifespan=lifespan)
    register_routers(application)
    application.add_exception_handler(StarletteHTTPException, form_http_error)
    application.add_exception_handler(RequestValidationError, form_validation_error)
    application.mount("/static", CachedStaticFiles(directory=str(STATIC_DIR)), name="static")
    application.middleware("http")(security_headers)
    application.middleware("http")(error_redirect_middleware)
    application.middleware("http")(restrict_codexproxy_hostname)
    application.middleware("http")(redirect_api_http_to_https)
    application.middleware("http")(trust_forwarded_https)
    return application


app = create_app()
