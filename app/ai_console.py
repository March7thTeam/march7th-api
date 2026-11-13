from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.ai_catalog import snapshot
from app.auth import optional_user, require_developer, require_admin
from app.ai_usage import read_usage_summary, get_key_models, usage_analytics, usage_window
from app.db import connect
from app.ai_policies import validate_policy

router = APIRouter()
from app.web.templating import templates


@router.get("/models", response_class=HTMLResponse)
async def models_page(request: Request, user=Depends(optional_user)):
    return templates.TemplateResponse(request, "ai_models.html", {"user": user, "catalog": await snapshot()})


@router.get("/status", response_class=HTMLResponse)
async def status_page(request: Request, user=Depends(optional_user)):
    return templates.TemplateResponse(request, "ai_status.html", {"user": user, "status": await snapshot()})


@router.get("/api/public/ai-catalog")
async def public_catalog():
    return JSONResponse(await snapshot(), headers={"Cache-Control": "public, max-age=15"})


def key_views(user_id=None):
    with connect() as conn:
        rows = conn.execute("SELECT id,user_id,name,key_prefix,status,scopes,allowed_routes,model_policy,rate_limit_per_minute,daily_quota,monthly_quota,expires_at,last_used_at FROM api_keys" + (" WHERE user_id=?" if user_id is not None else "") + " ORDER BY created_at DESC", (user_id,) if user_id is not None else ()).fetchall()
        return [{**dict(row), "models": get_key_models(conn, row["id"])} for row in rows]


def _page_days(value: str | None) -> int:
    """Range picker for the usage pages; anything unexpected falls back to two weeks."""
    return {"7": 7, "14": 14, "30": 30}.get((value or "").strip(), 14)


@router.get("/developer/usage", response_class=HTMLResponse)
def usage_page(request: Request, user=Depends(require_developer), days: str | None = None):
    span = _page_days(days)
    window = usage_window(span)
    return templates.TemplateResponse(request, "ai_usage.html", {"user": user, "usage": read_usage_summary(user["id"], window=window), "all_users": False, "analytics": usage_analytics(user["id"], span, window=window, protocols=("chat.completions", "responses", "images.generations"))})


@router.get("/admin/ai-usage", response_class=HTMLResponse)
def admin_usage(request: Request, user=Depends(require_admin), days: str | None = None):
    span = _page_days(days)
    window = usage_window(span)
    return templates.TemplateResponse(request, "ai_usage.html", {"user": user, "usage": read_usage_summary(None, window=window), "all_users": True, "analytics": usage_analytics(None, span, window=window, protocols=("chat.completions", "responses", "images.generations"))})


@router.get("/api/developer/usage")
def usage_api(user=Depends(require_developer)):
    return JSONResponse(read_usage_summary(user["id"]), headers={"Cache-Control": "no-store"})


@router.get("/developer/playground", response_class=HTMLResponse)
async def playground(request: Request, user=Depends(require_developer)):
    from app.image_input import image_input_enabled
    return templates.TemplateResponse(request, "ai_playground.html", {"user": user, "catalog": await snapshot(), "keys": key_views(user["id"]), "image_input": image_input_enabled()})


@router.get("/developer/images", response_class=HTMLResponse)
async def image_playground(request: Request, user=Depends(require_developer)):
    from app.image_generation import image_generation_available
    return templates.TemplateResponse(request, "ai_images.html", {"user": user, "keys": key_views(user["id"]),
        "image_generation_enabled": image_generation_available()})


@router.post("/developer/images/download")
async def image_download(request: Request, user=Depends(require_developer)):
    """Return only the logged-in caller's submitted PNG as an HTTP attachment.

    No provider request, image cache, file lookup or billing operation occurs.
    The existing session middleware enforces same-origin form submission.
    """
    import base64
    from urllib.parse import parse_qsl
    from app.cliproxy_gateway import read_bounded_body
    from app.image_generation import IMAGE_SIZES, valid_image_response

    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
        raise HTTPException(415, "请使用本站图片下载表单。")
    try:
        body = await read_bounded_body(request, max_bytes=20 * 1024 * 1024)
        fields = parse_qsl(body.decode("ascii"), keep_blank_values=True, strict_parsing=True,
                           encoding="utf-8", errors="strict", max_num_fields=2)
        if len(fields) != 2 or {name for name, _ in fields} != {"b64_json", "size"}:
            raise ValueError("Unexpected download fields")
        values = dict(fields)
        if values["size"] not in IMAGE_SIZES or not valid_image_response(
                {"data": [{"b64_json": values["b64_json"]}]}, expected_size=values["size"]):
            raise ValueError("Invalid PNG")
        image = base64.b64decode(values["b64_json"], validate=True)
    except (ValueError, UnicodeError) as exc:
        if str(exc) == "request_too_large":
            raise HTTPException(413, "图片下载表单超过20MiB上限。") from None
        raise HTTPException(400, "图片下载内容无效，请重新生成后下载。") from None
    return Response(image, media_type="image/png", headers={
        "Content-Disposition": 'attachment; filename="generated.png"',
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/admin/ai-keys/{key_id}", response_class=HTMLResponse)
def edit_policy(request: Request, key_id: int, user=Depends(require_admin), saved: str = ""):
    key = next((row for row in key_views() if row["id"] == key_id), None)
    if not key:
        raise HTTPException(404, "密钥不存在")
    return templates.TemplateResponse(request, "ai_key_policy.html", {"user": user, "key": key, "saved": saved == "1"})


@router.post("/admin/ai-keys/{key_id}")
def save_policy(key_id: int, scopes: str = Form(...), models: str = Form(""), model_policy: str = Form("allowlist"), rate_limit_per_minute: int = Form(...), daily_quota: int = Form(...), monthly_quota: int = Form(...), expires_at: str = Form(""), user=Depends(require_admin)):
    scopes, model_ids, expiry = validate_policy(scopes, models, rate_limit_per_minute, daily_quota, monthly_quota, expires_at, policy=model_policy, legacy=True)
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        key = conn.execute("SELECT * FROM api_keys WHERE id=?", (key_id,)).fetchone()
        if not key:
            raise HTTPException(404, "密钥不存在")
        routes = key["allowed_routes"] or ""
        conn.execute("UPDATE api_keys SET scopes=?,allowed_routes=?,model_policy=?,rate_limit_per_minute=?,daily_quota=?,monthly_quota=?,expires_at=? WHERE id=?", (scopes,routes,model_policy,rate_limit_per_minute,daily_quota,monthly_quota,expiry,key_id))
        conn.execute("DELETE FROM api_key_models WHERE api_key_id=?", (key_id,))
        conn.executemany("INSERT INTO api_key_models(api_key_id,model_id) VALUES (?,?)", [(key_id,model) for model in model_ids])
        conn.commit()
    return RedirectResponse(f"/admin/ai-keys/{key_id}?saved=1", status_code=303)


def analytics_response(user_id, days):
    if days not in {7, 14, 30}:
        raise HTTPException(400, "days 仅支持 7、14、30。")
    return JSONResponse(usage_analytics(user_id, days), headers={"Cache-Control": "no-store"})


@router.get("/api/developer/usage/analytics")
def developer_analytics(days: int = 14, user=Depends(require_developer)):
    return analytics_response(user["id"], days)


@router.get("/api/admin/usage/analytics")
def admin_analytics(days: int = 14, user=Depends(require_admin)):
    return analytics_response(None, days)
