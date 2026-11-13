from datetime import datetime, timedelta, timezone
from contextlib import closing
from typing import Any
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool

from app.ai_catalog import snapshot
from app.auth import optional_user
from app.config import settings
from app.db import connect
from app.web.templating import STATIC_DIR, templates


router = APIRouter()
EMPTY_STATS = {"enabled_routes": 0, "disabled_routes": 0, "documented_routes": 0}
PUBLIC_PAGE_PATHS = ("/", "/docs", "/models", "/pricing", "/status")


def get_public_route_stats() -> dict[str, int]:
    try:
        with closing(connect()) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS documented, COALESCE(SUM(enabled = 1), 0) AS enabled FROM public_gateway_routes WHERE public_docs = 1"
            ).fetchone()
    except Exception:
        return dict(EMPTY_STATS)
    documented, enabled = int(row["documented"] or 0), int(row["enabled"] or 0)
    return {"enabled_routes": enabled, "disabled_routes": documented - enabled, "documented_routes": documented}


def get_public_routes() -> list[dict[str, Any]]:
    try:
        with closing(connect()) as conn:
            rows = conn.execute(
                """
                SELECT id, name, public_path, target_method, required_scope, enabled, public_docs, allow_anonymous
                FROM public_gateway_routes
                WHERE public_docs = 1
                ORDER BY enabled DESC, name ASC
                """
            ).fetchall()
    except Exception:
        return []
    return [dict(row) for row in rows]


@router.get("/api/public/stats")
def public_stats_api():
    return JSONResponse(get_public_route_stats())


@router.get("/api/public/routes")
def public_routes_api():
    return JSONResponse(
        {
            "routes": get_public_routes(),
            "stats": get_public_route_stats(),
        }
    )


@router.get("/security", response_class=PlainTextResponse)
def security_policy():
    return security_text()


@router.api_route("/robots.txt", methods=["GET", "HEAD"], response_class=PlainTextResponse)
def robots_txt():
    rules = ["User-agent: *", "Disallow: /"]
    rules.extend(f"Allow: {path}$" for path in PUBLIC_PAGE_PATHS)
    rules.extend([
        "Allow: /static/",
        "Allow: /favicon.ico$",
        "Allow: /sitemap.xml$",
        "Allow: /security$",
        "Allow: /.well-known/security.txt$",
        f"Sitemap: {settings.app_base_url}/sitemap.xml",
        "",
    ])
    return "\n".join(rules)


@router.api_route("/sitemap.xml", methods=["GET", "HEAD"])
def sitemap_xml():
    urls = "\n".join(
        f"  <url><loc>{escape(settings.app_base_url + path)}</loc></url>" for path in PUBLIC_PAGE_PATHS
    )
    return Response(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}\n</urlset>\n",
        media_type="application/xml",
    )


@router.api_route("/favicon.ico", methods=["GET", "HEAD"], response_class=FileResponse)
def favicon():
    return FileResponse(
        STATIC_DIR / "favicon.ico",
        media_type="image/vnd.microsoft.icon",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/.well-known/security.txt", response_class=PlainTextResponse)
def well_known_security_txt():
    return security_text()


def security_text() -> str:
    return "\n".join(
        [
            "Contact: mailto:security@luomo.moe",
            "Contact: mailto:luomo0712@qq.com",
            "Preferred-Languages: zh, en",
            # RFC 9116 requires Expires; roll it forward so the file never goes stale.
            f"Expires: {(datetime.now(timezone.utc) + timedelta(days=180)).strftime('%Y-%m-%dT00:00:00Z')}",
            f"Canonical: {settings.app_base_url}/.well-known/security.txt",
            f"Policy: {settings.app_base_url}/security",
            "",
            f"Scope: {settings.app_authority} public portal, developer console, API key gateway, and {settings.app_name} account flows.",
            f"Out of scope: denial-of-service testing, social engineering, credential stuffing, destructive testing, and attempts against SSH, firewall, Cloudflare records outside {settings.app_authority}, or unrelated Luomo services.",
            "Please do not include secrets, API keys, passwords, cookies, or private user data in reports.",
            "",
        ]
    )


@router.get("/")
async def public_home(request: Request, user=Depends(optional_user)):
    # Route lookups are synchronous SQLite reads; keep them off the event loop.
    routes, stats = await run_in_threadpool(lambda: (get_public_routes(), get_public_route_stats()))
    return templates.TemplateResponse(request, "index.html", {"user": user, "catalog": await snapshot(), "routes": routes, **stats})


@router.get("/docs")
async def public_docs(request: Request, user=Depends(optional_user)):
    routes, stats = await run_in_threadpool(lambda: (get_public_routes(), get_public_route_stats()))
    return templates.TemplateResponse(request, "docs.html", {"user": user, "catalog": await snapshot(), "routes": routes, **stats})
