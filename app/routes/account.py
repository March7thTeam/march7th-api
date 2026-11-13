"""Authenticated profile, password changes and device revocation."""
from contextlib import closing
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import (SESSION_COOKIE, current_user, hash_password, verify_password,
                      revoke_user_sessions)
from app.db import connect
from app.utils.timefmt import now_iso
from app.web.templating import templates

router = APIRouter()


def account_response(request, user, *, error=None, message=None, status=200):
    with closing(connect()) as conn:
        sessions = [dict(row) for row in conn.execute("""SELECT id,user_agent,created_at,last_seen_at,expires_at
            FROM user_sessions WHERE user_id=? AND revoked_at IS NULL AND julianday(expires_at)>julianday(?)
            ORDER BY last_seen_at DESC,id DESC""", (user["id"], datetime.now(timezone.utc).isoformat()))]
    for session in sessions:
        session["user_agent"] = (session["user_agent"] or "未知设备")[:120]
        session["current"] = session["id"] == request.state.session_id
    return templates.TemplateResponse(request, "account.html", {"user": user, "sessions": sessions,
        "error": error, "message": message}, status_code=status, headers={"Cache-Control": "no-store"})


@router.get("/account", response_class=HTMLResponse)
def account_page(request: Request, user=Depends(current_user)):
    return account_response(request, user)


@router.post("/account/password", response_class=HTMLResponse)
def change_password(request: Request, current_password: str = Form(...), new_password: str = Form(...),
                    confirm_password: str = Form(...), user=Depends(current_user)):
    error = None
    if len(new_password) < 10:
        error = "新密码至少需要 10 个字符。"
    elif new_password != confirm_password:
        error = "两次输入的新密码不一致。"
    elif not verify_password(current_password, user["password_hash"]):
        error = "当前密码不正确。"
    elif new_password == current_password:
        error = "新密码不得与当前密码相同。"
    if error:
        return account_response(request, user, error=error, status=400)
    encoded = hash_password(new_password)
    with closing(connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        live = conn.execute("""SELECT 1 FROM user_sessions s JOIN users u ON u.id=s.user_id
            WHERE s.id=? AND s.user_id=? AND s.revoked_at IS NULL
              AND julianday(s.expires_at)>julianday(?) AND u.status IN ('active','pending')""",
            (request.state.session_id, user["id"], datetime.now(timezone.utc).isoformat())).fetchone()
        if not live:
            raise HTTPException(401, "会话已失效，请重新登录。")
        changed = conn.execute("UPDATE users SET password_hash=?,updated_at=? WHERE id=? AND password_hash=?",
                               (encoded, now_iso(), user["id"], user["password_hash"])).rowcount
        if changed != 1:
            raise HTTPException(409, "密码已变更，请重新登录后重试。")
        revoke_user_sessions(conn, user["id"], except_id=request.state.session_id)
        # An outstanding recovery email must not undo this intentional change.
        conn.execute("UPDATE password_reset_tokens SET used_at=? WHERE user_id=? AND used_at IS NULL", (now_iso(), user["id"]))
    return account_response(request, user, message="密码已修改，其他设备已退出登录。")


@router.post("/account/sessions/revoke-others")
def revoke_other_sessions(request: Request, user=Depends(current_user)):
    with closing(connect()) as conn, conn:
        revoke_user_sessions(conn, user["id"], except_id=request.state.session_id)
    return RedirectResponse("/account", status_code=303)


@router.post("/account/sessions/{session_id}/revoke")
def revoke_device(session_id: int, request: Request, user=Depends(current_user)):
    with closing(connect()) as conn, conn:
        changed = conn.execute("UPDATE user_sessions SET revoked_at=? WHERE id=? AND user_id=? AND revoked_at IS NULL",
                               (now_iso(), session_id, user["id"])).rowcount
        if not changed:
            raise HTTPException(404, "会话不存在。")
    response = RedirectResponse("/login" if session_id == request.state.session_id else "/account", status_code=303)
    if session_id == request.state.session_id:
        response.delete_cookie(SESSION_COOKIE, secure=True, httponly=True, samesite="lax")
    return response
