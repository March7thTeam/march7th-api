from contextlib import closing
import logging
import re
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from app.config import settings
from app.auth import require_developer
from app.db import connect
from app.email_verification import claim_verification_send, new_token_record, send_verification_email
from app.client_ip import client_address
from app.utils.timefmt import now_iso
from app.web.templating import templates
from app.ai_console import key_views
from app.ai_usage import read_usage_summary, usage_analytics
from app.credit_ledger import wallet_snapshot
from app.self_service_keys import issue_self_key, self_key_nonce
from app.routes.auth import diagnostic_request_id
from app.routes.compat import legacy_override
logger = logging.getLogger("luomoapi.mail")

router = APIRouter()


@router.get("/developer", response_class=HTMLResponse)
def developer_home(request: Request, user=Depends(require_developer)):
    with closing(connect()) as conn, conn:
        keys = conn.execute("SELECT id,name,key_prefix,status FROM api_keys WHERE user_id = ? ORDER BY created_at DESC", (user["id"],)).fetchall()
        today = now_iso()[:10]
        usage_today = conn.execute("SELECT COUNT(*) AS c FROM api_usage_logs WHERE user_id = ? AND created_at LIKE ?", (user["id"], f"{today}%")).fetchone()["c"]
        recent = conn.execute("SELECT * FROM api_usage_logs WHERE user_id = ? ORDER BY created_at DESC LIMIT 10", (user["id"],)).fetchall()
        routes = conn.execute("SELECT * FROM public_gateway_routes WHERE public_docs = 1 ORDER BY public_path").fetchall() if settings.api_catalog_enabled else []
        wallet = wallet_snapshot(conn, user["id"])
    usage = read_usage_summary(user["id"])
    return templates.TemplateResponse(request, "developer.html", {"user": user, "keys": keys, "usage_today": usage_today, "recent": recent, "routes": routes, "wallet": wallet, "usage": usage, "analytics": usage_analytics(user["id"], 14)})


@router.post("/developer/resend-verification")
def resend_verification(request: Request, user=Depends(require_developer)):
    if int(user["email_verified"] or 0):
        return RedirectResponse("/developer?verification=already", status_code=303)
    source_ip = client_address(request)
    if not claim_verification_send(user["email"], source_ip):
        return RedirectResponse("/developer?verification=queued", status_code=303)
    token, token_hash, sent_at = new_token_record()
    try:
        legacy_override("send_verification_email", send_verification_email)(user["email"], user["username"], token)
    except Exception as exc:
        logger.warning("verification_resend_failed request_id=%s error_type=%s", diagnostic_request_id(request), type(exc).__name__)
        return RedirectResponse("/developer?verification=failed", status_code=303)
    try:
        with closing(connect()) as conn, conn:
            conn.execute(
                """
                UPDATE users
                SET email_verification_token_hash=?, email_verification_sent_at=?, updated_at=?
                WHERE id=? AND email_verified=0
                """,
                (token_hash, sent_at, now_iso(), user["id"]),
            )
            conn.commit()
    except Exception as exc:
        logger.warning("verification_token_activation_failed request_id=%s error_type=%s", diagnostic_request_id(request), type(exc).__name__)
        return RedirectResponse("/developer?verification=processing", status_code=303)
    return RedirectResponse("/developer?verification=sent", status_code=303)


@router.get("/developer/keys", response_class=HTMLResponse)
def developer_keys(request: Request, user=Depends(require_developer), created: str | None = None):
    with closing(connect()) as conn, conn:
        requests = conn.execute("SELECT * FROM api_key_requests WHERE user_id = ? ORDER BY created_at DESC", (user["id"],)).fetchall()
    keys = key_views(user["id"])
    return templates.TemplateResponse(request, "developer_keys.html", {"user": user, "keys": keys, "requests": requests, "created": created == "1", "key_nonce": self_key_nonce(user["id"])})


@router.post("/developer/keys/request")
def developer_key_request(request: Request, name: str = Form(...), key_nonce: str = Form(""), requested_scopes: str = Form(""), requested_reason: str = Form(""),
                          credit_limit: str = Form(""), expires_on: str = Form(""), rate_limit_per_minute: str = Form("60"), models: str = Form(""), user=Depends(require_developer)):
    scopes = {s for s in re.split(r"[,\s]+", requested_scopes.strip()) if s}
    if scopes - {"models:read", "chat:completions", "responses:create", "images:generate"}:
        raise HTTPException(400, "自助密钥只支持 AI 协议权限；业务服务权限请联系管理员。")
    from app.key_settings import parse_owner_limits, selectable_models
    limits = parse_owner_limits(credit_limit=credit_limit, expires_on=expires_on, rpm=rate_limit_per_minute,
                                models=re.split(r'[,\s]+', models.strip()) if models.strip() else selectable_models())
    with closing(connect()) as conn, conn:
        result = issue_self_key(conn, user["id"], name, key_nonce, limits=limits if credit_limit.strip() or expires_on.strip() or rate_limit_per_minute != "60" or models.strip() else None)
    return templates.TemplateResponse(request, "developer_key_created.html", {"user": user, "result": result}, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.post("/developer/keys/{key_id}/revoke")
def developer_revoke_key(key_id: int, user=Depends(require_developer)):
    with closing(connect()) as conn, conn:
        conn.execute("UPDATE api_keys SET status='revoked', revoked_at=? WHERE id=? AND user_id=?", (now_iso(), key_id, user["id"]))
        conn.commit()
    return RedirectResponse("/developer/keys", status_code=303)


@router.get("/developer/logs", response_class=HTMLResponse)
def developer_logs(request: Request, user=Depends(require_developer)):
    from app.request_details import filtered_usage
    params = {name:request.query_params.get(name,'') for name in ('model','protocol','billing','since','until','key_id','request_id')}
    logs = filtered_usage(user, **params)
    return templates.TemplateResponse(request, "developer_logs.html", {"user":user,"logs":logs})


def _limits_page(request, user, key_id, *, error=None, saved=False, status=200):
    from app.key_settings import selectable_models
    key = next((item for item in key_views(user["id"]) if item["id"] == key_id), None)
    if not key:
        raise HTTPException(404, "密钥不存在。")
    return templates.TemplateResponse(request, "developer_key_limits.html", {"user": user, "key": key,
        "choices": selectable_models(), "error": error, "saved": saved}, status_code=status, headers={"Cache-Control": "no-store"})


@router.get("/developer/keys/{key_id}/limits", response_class=HTMLResponse)
def developer_key_limits(request: Request, key_id: int, saved: str = "", user=Depends(require_developer)):
    return _limits_page(request, user, key_id, saved=saved == "1")


@router.post("/developer/keys/{key_id}/limits")
async def developer_save_key_limits(request: Request, key_id: int, user=Depends(require_developer)):
    from app.key_settings import parse_owner_limits, save_owner_limits
    form = await request.form()
    try:
        limits = parse_owner_limits(credit_limit=str(form.get("credit_limit", "")), expires_on=str(form.get("expires_on", "")),
                                    rpm=str(form.get("rate_limit_per_minute", "60")), models=[str(v) for v in form.getlist("models")])
        with closing(connect()) as conn:
            save_owner_limits(conn, user["id"], key_id, limits)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise
        return _limits_page(request, user, key_id, error=exc.detail, status=exc.status_code)
    return RedirectResponse(f"/developer/keys/{key_id}/limits?saved=1", status_code=303)
