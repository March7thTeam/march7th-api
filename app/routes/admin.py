from contextlib import closing
from app.ai_usage import usage_analytics
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from app.auth import require_admin, api_user_eligible, revoke_user_sessions
from app.db import connect
from app.utils.timefmt import now_iso
from app.web.templating import templates
from app.ai_policies import validate_policy
from app.keyring import generate_api_key, hash_api_key

router = APIRouter()


@router.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request, user=Depends(require_admin)):
    today = now_iso()[:10]
    with closing(connect()) as conn, conn:
        stats = {
            "users": conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"],
            "pending_users": conn.execute("SELECT COUNT(*) AS c FROM users WHERE status='pending'").fetchone()["c"],
            "key_requests": conn.execute("SELECT COUNT(*) AS c FROM api_key_requests WHERE status='pending'").fetchone()["c"],
            "calls_today": conn.execute("SELECT COUNT(*) AS c FROM api_usage_logs WHERE created_at LIKE ?", (f"{today}%",)).fetchone()["c"],
            "reconciliation": conn.execute("SELECT COUNT(*) AS c FROM credit_holds WHERE status='reconciliation_required'").fetchone()["c"],
            "active_keys": conn.execute("SELECT COUNT(*) AS c FROM api_keys WHERE status='active'").fetchone()["c"],
        }
        recent = conn.execute("SELECT * FROM api_usage_logs ORDER BY created_at DESC LIMIT 10").fetchall()
    return templates.TemplateResponse(request, "admin.html", {"user": user, "stats": stats, "recent": recent, "analytics": usage_analytics(None, 14)})


@router.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request, user=Depends(require_admin)):
    with closing(connect()) as conn, conn:
        users = conn.execute("SELECT users.*, (SELECT COUNT(*) FROM api_keys WHERE api_keys.user_id=users.id) AS key_count FROM users ORDER BY created_at DESC").fetchall()
    return templates.TemplateResponse(request, "admin_users.html", {"user": user, "users": users})


@router.post("/admin/users/{user_id}/status")
def admin_user_status(user_id: int, status_value: str = Form(...), user=Depends(require_admin)):
    if status_value not in {"pending", "active", "suspended", "disabled"}:
        raise HTTPException(status_code=400, detail="账户状态无效。")
    if user_id == user["id"]:
        raise HTTPException(status_code=400, detail="不能修改自己的账户状态，以免失去管理权限。")
    with closing(connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE users SET status=?, updated_at=? WHERE id=?", (status_value, now_iso(), user_id))
        if status_value in {"suspended", "disabled"}:
            revoke_user_sessions(conn, user_id)
        conn.commit()
    return RedirectResponse("/admin/users", status_code=303)


@router.get("/admin/key-requests", response_class=HTMLResponse)
def admin_key_requests(request: Request, user=Depends(require_admin)):
    with closing(connect()) as conn, conn:
        rows = conn.execute(
            "SELECT api_key_requests.*, users.username FROM api_key_requests JOIN users ON users.id=api_key_requests.user_id ORDER BY api_key_requests.created_at DESC"
        ).fetchall()
    return templates.TemplateResponse(request, "admin_key_requests.html", {"user": user, "requests": rows, "new_key": None, "prefix": None})


@router.post("/admin/key-requests/{request_id}/approve")
def admin_approve_key(request: Request, request_id: int, scopes: str = Form("models:read,chat:completions"), rate_limit_per_minute: int = Form(60),
                      daily_quota: int = Form(1000), monthly_quota: int = Form(30000), expires_at: str = Form(""),
                      models: str = Form(""),
                      user=Depends(require_admin)):
    scopes, model_ids, expiry = validate_policy(scopes, models, rate_limit_per_minute, daily_quota, monthly_quota, expires_at)
    allowed_routes = ""
    raw_key, prefix = generate_api_key()
    encoded_key = hash_api_key(raw_key)
    now = now_iso()
    with closing(connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        req = conn.execute("SELECT * FROM api_key_requests WHERE id=?", (request_id,)).fetchone()
        if not req:
            raise HTTPException(status_code=404)
        if req["status"] != "pending":
            raise HTTPException(409, "该申请已处理，不能重复签发。")
        owner = conn.execute("SELECT status,email_verified FROM users WHERE id=?", (req["user_id"],)).fetchone()
        if not owner or not api_user_eligible(owner["status"]):
            raise HTTPException(400, "账户已停用，无法签发密钥。")
        cursor = conn.execute(
            """
            INSERT INTO api_keys(user_id, name, key_prefix, key_hash, status, scopes, allowed_routes,
            rate_limit_per_minute, daily_quota, monthly_quota, created_at, approved_at, expires_at, model_policy)
            VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, 'allowlist')
            """,
            (req["user_id"], req["name"], prefix, encoded_key, scopes, allowed_routes, rate_limit_per_minute, daily_quota, monthly_quota, now, now, expiry),
        )
        conn.executemany("INSERT INTO api_key_models(api_key_id,model_id) VALUES (?,?)", [(cursor.lastrowid, model) for model in model_ids])
        conn.execute("UPDATE api_key_requests SET status='approved', reviewed_at=?, reviewed_by=? WHERE id=?", (now, user["id"], request_id))
        conn.commit()
    return templates.TemplateResponse(request, "admin_key_approved.html", {"user": user, "new_key": raw_key, "prefix": prefix})


@router.post("/admin/key-requests/{request_id}/reject")
def admin_reject_key(request_id: int, admin_note: str = Form(""), user=Depends(require_admin)):
    with closing(connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        req = conn.execute("SELECT status FROM api_key_requests WHERE id=?", (request_id,)).fetchone()
        if not req:
            raise HTTPException(404, "申请不存在")
        if req["status"] != "pending":
            raise HTTPException(409, "该申请已经处理")
        conn.execute("UPDATE api_key_requests SET status='rejected', admin_note=?, reviewed_at=?, reviewed_by=? WHERE id=? AND status='pending'", (admin_note, now_iso(), user["id"], request_id))
        conn.commit()
    return RedirectResponse("/admin/key-requests", status_code=303)


@router.get("/admin/logs", response_class=HTMLResponse)
def admin_usage_logs(request: Request, user=Depends(require_admin)):
    with closing(connect()) as conn, conn:
        logs = conn.execute("SELECT api_usage_logs.*, users.username FROM api_usage_logs LEFT JOIN users ON users.id=api_usage_logs.user_id ORDER BY api_usage_logs.created_at DESC LIMIT 200").fetchall()
    return templates.TemplateResponse(request, "admin_logs.html", {"user": user, "logs": logs})


@router.get("/admin/api-keys", response_class=HTMLResponse)
def admin_api_keys(request: Request, user=Depends(require_admin)):
    with closing(connect()) as conn, conn:
        keys = conn.execute(
            """
            SELECT
                k.id,k.name,k.key_prefix,k.status,k.scopes,k.allowed_routes,k.rate_limit_per_minute,k.daily_quota,k.monthly_quota,k.expires_at,k.last_used_at,
                u.username,
                u.email
            FROM api_keys k
            LEFT JOIN users u ON u.id = k.user_id
            ORDER BY k.created_at DESC
            """
        ).fetchall()
    return templates.TemplateResponse(request, "admin_api_keys.html", {"user": user,
            "keys": keys,
        },
    )


@router.post("/admin/api-keys/{key_id}/status")
def admin_api_key_status(
    key_id: int,
    status_value: str = Form(...),
    user=Depends(require_admin),
):
    if status_value not in {"active", "revoked"}:
        raise HTTPException(400, "密钥状态无效。")

    with closing(connect()) as conn, conn:
        if status_value == "revoked":
            conn.execute(
                "UPDATE api_keys SET status='revoked', revoked_at=? WHERE id=?",
                (now_iso(), key_id),
            )
        else:
            conn.execute(
                "UPDATE api_keys SET status='active', revoked_at=NULL WHERE id=?",
                (key_id,),
            )
        conn.commit()

    return RedirectResponse("/admin/api-keys", status_code=303)

