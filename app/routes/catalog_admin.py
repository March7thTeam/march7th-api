from pathlib import Path
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from app.catalog_validation import route_values, api_values, endpoint_values
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from app.auth import require_admin
from app.db import connect
from app.config import settings, secret_status
from app.utils.timefmt import now_iso
from app.utils.redact import preview, redact_url
from app.web.templating import templates
from app.api_client import TesterError, send_test_request
from app.models import ALLOWED_METHODS, AUTH_TYPES
from scripts.seed_default_apis import seed

router = APIRouter()


@router.get("/admin/routes", response_class=HTMLResponse)
def admin_routes(request: Request, user=Depends(require_admin)):
    with closing(connect()) as conn, conn:
        routes = conn.execute("""SELECT r.*, apis.name AS api_name,
            (SELECT COUNT(*) FROM api_usage_logs l WHERE l.route_id=r.id AND julianday(l.created_at)>=julianday(?)) AS calls_7d,
            (SELECT l.created_at FROM api_usage_logs l WHERE l.route_id=r.id ORDER BY julianday(l.created_at) DESC,l.id DESC LIMIT 1) AS last_called_at
            FROM public_gateway_routes r JOIN apis ON apis.id=r.target_api_id ORDER BY public_path""",
            ((datetime.now(timezone.utc)-timedelta(days=7)).isoformat(),)).fetchall()
        apis = conn.execute("SELECT * FROM apis ORDER BY name").fetchall()
    return templates.TemplateResponse(request, "admin_routes.html", {"user": user, "routes": routes, "apis": apis, "methods": ALLOWED_METHODS})


def route_form(name: str = Form(...), public_path: str = Form(...), target_api_id: int = Form(...),
               target_method: str = Form("GET"), target_path: str = Form(...), required_scope: str = Form(""),
               target_endpoint_id: int | None = Form(None), enabled: str | None = Form(None),
               public_docs: str | None = Form(None), allow_anonymous: str | None = Form(None)):
    return dict(name=name, public_path=public_path, target_api_id=target_api_id, target_method=target_method,
                target_path=target_path, required_scope=required_scope, target_endpoint_id=target_endpoint_id,
                enabled=int(bool(enabled)), public_docs=int(bool(public_docs)), allow_anonymous=int(bool(allow_anonymous)))


def write_route(conn, data, route_id=None):
    values = route_values(conn, data, route_id)
    now = now_iso()
    try:
        if route_id is None:
            conn.execute(f"INSERT INTO public_gateway_routes({','.join(values)},created_at,updated_at) VALUES ({','.join('?' for _ in values)},?,?)",
                         (*values.values(), now, now))
        else:
            changed = conn.execute(f"UPDATE public_gateway_routes SET {','.join(key+'=?' for key in values)},updated_at=? WHERE id=?",
                                   (*values.values(), now, route_id)).rowcount
            if not changed:
                raise HTTPException(404, "路由不存在。")
    except sqlite3.IntegrityError as exc:
        raise HTTPException(400, "路由路径冲突或目标无效。") from exc


@router.post("/admin/routes")
def admin_add_route(data=Depends(route_form), user=Depends(require_admin)):
    with closing(connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        write_route(conn, data)
    return RedirectResponse("/admin/routes", status_code=303)


@router.get("/admin/routes/{route_id}/edit", response_class=HTMLResponse)
def route_edit(route_id: int, request: Request, user=Depends(require_admin)):
    with closing(connect()) as conn:
        route = conn.execute("SELECT * FROM public_gateway_routes WHERE id=?", (route_id,)).fetchone()
        if not route:
            raise HTTPException(404, "路由不存在。")
        apis = conn.execute("SELECT * FROM apis ORDER BY name").fetchall()
        endpoints = conn.execute("SELECT id,api_id,name FROM endpoints ORDER BY api_id,id").fetchall()
    return templates.TemplateResponse(request, "route_edit.html", {"user": user, "route": route, "apis": apis, "endpoints": endpoints, "methods": ALLOWED_METHODS})


@router.post("/admin/routes/{route_id}/edit")
def route_save(route_id: int, data=Depends(route_form), user=Depends(require_admin)):
    with closing(connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        write_route(conn, data, route_id)
    return RedirectResponse("/admin/routes", status_code=303)


@router.post("/admin/routes/{route_id}/copy")
def route_copy(route_id: int, user=Depends(require_admin)):
    with closing(connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        source = conn.execute("SELECT * FROM public_gateway_routes WHERE id=?", (route_id,)).fetchone()
        if not source:
            raise HTTPException(404, "路由不存在。")
        values = {k: source[k] for k in ("name", "public_path", "target_api_id", "target_endpoint_id", "target_method", "target_path", "required_scope", "enabled", "public_docs", "allow_anonymous")}
        path = values["public_path"]
        index = 1
        while True:
            suffix = "-copy" + (f"-{index}" if index > 1 else "")
            candidate = path[:1024-len(suffix)].rstrip("/") + suffix
            if not conn.execute("SELECT 1 FROM public_gateway_routes WHERE public_path=?", (candidate,)).fetchone():
                break
            index += 1
        values.update(name=values["name"] + "（副本）", public_path=candidate, enabled=0)
        write_route(conn, values)
    return RedirectResponse("/admin/routes", status_code=303)


@router.post("/admin/routes/{route_id}/delete")
def route_delete(route_id: int, user=Depends(require_admin)):
    with closing(connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM api_usage_logs WHERE route_id=? LIMIT 1", (route_id,)).fetchone():
            raise HTTPException(409, "此路由已有调用记录，不能删除，请改为停用。")
        if not conn.execute("DELETE FROM public_gateway_routes WHERE id=?", (route_id,)).rowcount:
            raise HTTPException(404, "路由不存在。")
    return RedirectResponse("/admin/routes", status_code=303)


@router.post("/admin/routes/{route_id}/toggle")
def admin_toggle_route(route_id: int, enabled: int = Form(0), user=Depends(require_admin)):
    with closing(connect()) as conn, conn:
        conn.execute("UPDATE public_gateway_routes SET enabled=?, updated_at=? WHERE id=?", (1 if enabled else 0, now_iso(), route_id))
        conn.commit()
    return RedirectResponse("/admin/routes", status_code=303)


@router.get("/apis", response_class=HTMLResponse)
def apis_page(request: Request, user=Depends(require_admin)):
    with closing(connect()) as conn, conn:
        apis = conn.execute("SELECT * FROM apis ORDER BY name").fetchall()
    return templates.TemplateResponse(request, "apis.html", {"user": user, "apis": apis, "auth_types": AUTH_TYPES})


def api_form(name: str = Form(...), slug: str = Form(...), base_url: str = Form(...), auth_type: str = Form("none"),
             secret_ref: str = Form(""), description: str = Form(""), tags: str = Form(""), enabled: str | None = Form(None)):
    return api_values(dict(name=name, slug=slug, base_url=base_url, auth_type=auth_type, secret_ref=secret_ref,
                           description=description, tags=tags, enabled=int(bool(enabled))))


def write_api(conn, data, api_id=None):
    if conn.execute("SELECT 1 FROM apis WHERE slug=? AND (? IS NULL OR id!=?)", (data["slug"], api_id, api_id)).fetchone():
        raise HTTPException(400, "API 标识已存在。")
    now = now_iso()
    if api_id is None:
        conn.execute(f"INSERT INTO apis({','.join(data)},created_at,updated_at) VALUES ({','.join('?' for _ in data)},?,?)", (*data.values(), now, now))
    elif not conn.execute(f"UPDATE apis SET {','.join(k+'=?' for k in data)},updated_at=? WHERE id=?", (*data.values(), now, api_id)).rowcount:
        raise HTTPException(404, "API 不存在。")


@router.post("/apis")
def add_api(data=Depends(api_form), user=Depends(require_admin)):
    with closing(connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        write_api(conn, data)
    return RedirectResponse("/apis", status_code=303)


@router.post("/apis/{api_id}/edit")
def edit_api(api_id: int, data=Depends(api_form), user=Depends(require_admin)):
    with closing(connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        write_api(conn, data, api_id)
    return RedirectResponse(f"/apis/{api_id}", status_code=303)


@router.post("/apis/{api_id}/toggle")
def toggle_api(api_id: int, enabled: int = Form(0), user=Depends(require_admin)):
    with closing(connect()) as conn, conn:
        if not conn.execute("UPDATE apis SET enabled=?,updated_at=? WHERE id=?", (int(bool(enabled)), now_iso(), api_id)).rowcount:
            raise HTTPException(404, "API 不存在。")
    return RedirectResponse(f"/apis/{api_id}", status_code=303)


@router.get("/apis/{api_id}", response_class=HTMLResponse)
def api_detail(api_id: int, request: Request, user=Depends(require_admin)):
    with closing(connect()) as conn, conn:
        api = conn.execute("SELECT * FROM apis WHERE id = ?", (api_id,)).fetchone()
        if not api:
            raise HTTPException(status_code=404)
        endpoints = conn.execute("SELECT * FROM endpoints WHERE api_id = ? ORDER BY method, path", (api_id,)).fetchall()
    return templates.TemplateResponse(request, "api_detail.html", {"user": user, "api": api, "endpoints": endpoints, "methods": ALLOWED_METHODS, "auth_types": AUTH_TYPES})


def endpoint_form(name: str = Form(...), method: str = Form("GET"), path: str = Form(...), description: str = Form(""),
                  default_headers: str = Form("{}"), default_query: str = Form("{}"), default_body: str = Form(""), enabled: str | None = Form(None)):
    return endpoint_values(dict(name=name, method=method, path=path, description=description, default_headers=default_headers,
                                default_query=default_query, default_body=default_body, enabled=int(bool(enabled))))


def write_endpoint(conn, api_id, data, endpoint_id=None):
    if not conn.execute("SELECT 1 FROM apis WHERE id=?", (api_id,)).fetchone():
        raise HTTPException(404, "API 不存在。")
    now = now_iso()
    if endpoint_id is None:
        conn.execute(f"INSERT INTO endpoints(api_id,{','.join(data)},created_at,updated_at) VALUES (?,{','.join('?' for _ in data)},?,?)", (api_id,*data.values(),now,now))
    elif not conn.execute(f"UPDATE endpoints SET {','.join(k+'=?' for k in data)},updated_at=? WHERE id=? AND api_id=?", (*data.values(),now,endpoint_id,api_id)).rowcount:
        raise HTTPException(404, "端点不存在。")


@router.post("/apis/{api_id}/endpoints")
def add_endpoint(api_id: int, data=Depends(endpoint_form), user=Depends(require_admin)):
    with closing(connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        write_endpoint(conn, api_id, data)
    return RedirectResponse(f"/apis/{api_id}", status_code=303)


@router.post("/apis/{api_id}/endpoints/{endpoint_id}/edit")
def edit_endpoint(api_id: int, endpoint_id: int, data=Depends(endpoint_form), user=Depends(require_admin)):
    with closing(connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        write_endpoint(conn, api_id, data, endpoint_id)
    return RedirectResponse(f"/apis/{api_id}", status_code=303)


@router.post("/apis/{api_id}/endpoints/{endpoint_id}/delete")
def delete_endpoint(api_id: int, endpoint_id: int, user=Depends(require_admin)):
    with closing(connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM endpoints WHERE id=? AND api_id=?", (endpoint_id,api_id)).fetchone():
            raise HTTPException(404, "端点不存在。")
        # Routes already store fixed method/path snapshots. Preserve them when
        # removing the optional editor link to an endpoint.
        conn.execute("UPDATE public_gateway_routes SET target_endpoint_id=NULL WHERE target_endpoint_id=?", (endpoint_id,))
        conn.execute("UPDATE gateway_routes SET endpoint_id=NULL WHERE endpoint_id=?", (endpoint_id,))
        conn.execute("DELETE FROM endpoints WHERE id=? AND api_id=?", (endpoint_id,api_id))
    return RedirectResponse(f"/apis/{api_id}", status_code=303)


@router.get("/tester", response_class=HTMLResponse)
def tester_page(request: Request, api_id: int | None = None, endpoint_id: int | None = None, user=Depends(require_admin)):
    return _tester_response(request, user, api_id=api_id, endpoint_id=endpoint_id)


@router.post("/tester", response_class=HTMLResponse)
def tester_submit(request: Request, api_id: int = Form(...), method: str = Form("GET"), path: str = Form(...),
                  headers_json: str = Form("{}"), query_json: str = Form("{}"), body_json: str = Form(""),
                  use_saved_auth: str | None = Form(None), user=Depends(require_admin)):
    with closing(connect()) as conn, conn:
        api = conn.execute("SELECT * FROM apis WHERE id = ?", (api_id,)).fetchone()
    result = error = None
    try:
        result = send_test_request(api, method, path, headers_json, query_json, body_json, bool(use_saved_auth))
        _record_log(api_id, None, method.upper(), result, user["username"])
    except TesterError as exc:
        error = str(exc)
        _record_manual_error(api_id, method.upper(), path, error, user["username"])
    return _tester_response(request, user, api_id=api_id, form={"method": method.upper(), "path": path, "headers_json": headers_json, "query_json": query_json, "body_json": body_json, "use_saved_auth": bool(use_saved_auth)}, result=result, error=error)


def _tester_response(request: Request, user, api_id: int | None = None, endpoint_id: int | None = None, form: dict | None = None, result=None, error: str | None = None):
    with closing(connect()) as conn, conn:
        apis = conn.execute("SELECT * FROM apis WHERE enabled = 1 ORDER BY name").fetchall()
        endpoints = conn.execute("SELECT * FROM endpoints WHERE enabled = 1 ORDER BY api_id, method, path").fetchall()
        selected_endpoint = conn.execute("SELECT * FROM endpoints WHERE id = ?", (endpoint_id,)).fetchone() if endpoint_id else None
    form = form or {"method": selected_endpoint["method"] if selected_endpoint else "GET", "path": selected_endpoint["path"] if selected_endpoint else "", "headers_json": selected_endpoint["default_headers"] if selected_endpoint else "{}", "query_json": selected_endpoint["default_query"] if selected_endpoint else "{}", "body_json": selected_endpoint["default_body"] if selected_endpoint else "", "use_saved_auth": True}
    return templates.TemplateResponse(request, "tester.html", {"user": user, "apis": apis, "endpoints": endpoints, "selected_api_id": api_id, "methods": ALLOWED_METHODS, "form": form, "result": result, "error": error})


def _record_log(api_id: int | None, endpoint_id: int | None, method: str, result, caller: str) -> None:
    with closing(connect()) as conn, conn:
        conn.execute(
            """
            INSERT INTO request_logs(created_at, api_id, endpoint_id, method, url_masked, status_code, response_time_ms,
            success, error_summary, caller, request_preview, response_preview)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (now_iso(), api_id, endpoint_id, method, result.url_masked, result.status_code, result.response_time_ms, 1 if result.success else 0, result.error_summary, caller, result.request_preview, result.response_preview),
        )
        conn.commit()


def _record_manual_error(api_id: int | None, method: str, path: str, error: str, caller: str) -> None:
    with closing(connect()) as conn, conn:
        conn.execute(
            """
            INSERT INTO request_logs(created_at, api_id, endpoint_id, method, url_masked, status_code, response_time_ms,
            success, error_summary, caller, request_preview, response_preview)
            VALUES (?, ?, NULL, ?, ?, NULL, NULL, 0, ?, ?, ?, '')
            """,
            (now_iso(), api_id, method, redact_url(path), preview(error, 500), caller, preview({"path": path, "error": error})),
        )
        conn.commit()


@router.get("/secrets", response_class=HTMLResponse)
def secrets_page(request: Request, user=Depends(require_admin)):
    seed()
    with closing(connect()) as conn, conn:
        rows = conn.execute("SELECT * FROM secrets ORDER BY name").fetchall()
    return templates.TemplateResponse(request, "secrets.html", {"user": user, "secrets": rows})


@router.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request, user=Depends(require_admin)):
    with closing(connect()) as conn, conn:
        logs = conn.execute("SELECT request_logs.*, apis.name AS api_name FROM request_logs LEFT JOIN apis ON apis.id = request_logs.api_id ORDER BY request_logs.created_at DESC LIMIT 100").fetchall()
    return templates.TemplateResponse(request, "logs.html", {"user": user, "logs": logs})


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user=Depends(require_admin)):
    status_panel = {
        "Database": "exists" if Path(settings.database_path).exists() else "missing",
        "App port": settings.app_port,
        "Session secret": "configured" if settings.session_secret else "missing",
        "Admin": "configured",
        "Environment": settings.app_env,
        "AstrBot credential": secret_status("ASTRBOT_API_KEY"),
        "LuomoCore credential": secret_status("LUOMOCORE_API_TOKEN"),
        "SMTP host": settings.smtp_host or "missing",
        "SMTP username": "configured" if settings.smtp_username else "missing",
        "SMTP password": secret_status("SMTP_PASSWORD"),
        "Email verification TTL hours": settings.email_verification_ttl_hours,
    }
    return templates.TemplateResponse(request, "settings.html", {"user": user, "status": status_panel})

