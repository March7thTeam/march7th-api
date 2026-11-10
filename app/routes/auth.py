from contextlib import closing
from datetime import datetime, timedelta, timezone
import logging
import re
import secrets
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from app.auth import (SESSION_COOKIE, SESSION_MAX_AGE_SECONDS, authenticate_user, create_session, hash_password, revoke_session)
from app.db import connect
from app.email_verification import (claim_verification_send, hash_verification_token, is_verification_token_fresh, new_token_record, send_verification_email)
from app.client_ip import client_address, client_identity
from app.login_throttle import cancel_login_attempt, claim_password_reset_attempt, release_login_attempt, reserve_login_attempt
from app.utils.timefmt import now_iso
from app.web.templating import templates
from app.routes.compat import legacy_override
logger = logging.getLogger("luomoapi.mail")

router = APIRouter()


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse(request, "register.html", {"error": None, "message": None, "submitted": False})


@router.head("/register")
def register_head() -> None:
    return None


@router.post("/register", response_class=HTMLResponse)
def register_submit(request: Request, username: str = Form(...), email: str = Form(...),
                    password: str = Form(...), confirm_password: str = Form(...), usage_reason: str = Form("")):
    error = None
    username = username.strip()
    email = email.strip().lower()
    usage_reason = usage_reason.strip()
    if len(password) < 10:
        error = "密码至少需要 10 个字符。"
    elif password != confirm_password:
        error = "两次输入的密码不一致。"
    elif not username or not email:
        error = "请填写用户名和邮箱。"
    if error:
        return templates.TemplateResponse(request, "register.html", {"error": error, "message": None, "submitted": False}, status_code=400)
    message = "如注册信息可用，账户已创建，可直接登录并领取 AI API 密钥，无需人工审核。验证邮件用于确认邮箱，邮箱验证不影响使用；若未收到邮件，可登录后重新发送。"
    source_ip = client_address(request)
    if not claim_verification_send(email, source_ip):
        return templates.TemplateResponse(request, "register.html", {"error": None, "message": message, "submitted": True})
    now = now_iso()
    token, token_hash, sent_at = new_token_record()
    try:
        with closing(connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO users(username, email, password_hash, role, status, usage_reason,
                email_verified, email_verification_token_hash, email_verification_sent_at, created_at, updated_at)
                VALUES (?, ?, ?, 'developer', 'active', ?, 0, ?, ?, ?, ?)
                """,
                (username, email, hash_password(password), usage_reason, token_hash, sent_at, now, now),
            )
            conn.commit()
    except Exception:
        return templates.TemplateResponse(request, "register.html", {"error": None, "message": message, "submitted": True})
    try:
        legacy_override("send_verification_email", send_verification_email)(email, username, token)
    except Exception as exc:
        logger.warning("registration_verification_failed request_id=%s error_type=%s", diagnostic_request_id(request), type(exc).__name__)
    return templates.TemplateResponse(request, "register.html", {"error": None, "message": message, "submitted": True})


def diagnostic_request_id(request: Request) -> str:
    value = request.headers.get("x-request-id", "").strip()
    return value if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value) else secrets.token_hex(8)


@router.get("/verify-email", response_class=HTMLResponse)
def verify_email(request: Request, token: str = ""):
    token_hash = hash_verification_token(token.strip()) if token else ""
    with closing(connect()) as conn, conn:
        user = conn.execute(
            "SELECT * FROM users WHERE email_verification_token_hash = ?",
            (token_hash,),
        ).fetchone()
        if not user:
            return templates.TemplateResponse(request, "verify_email.html", {"success": False, "valid": False, "token": "", "message": "验证链接无效或已经使用过。"}, status_code=400)
        if not is_verification_token_fresh(user["email_verification_sent_at"]):
            return templates.TemplateResponse(request, "verify_email.html", {"success": False, "valid": False, "token": "", "message": "验证链接已过期。请登录控制台后重新发送验证邮件。"}, status_code=400)
    return templates.TemplateResponse(request, "verify_email.html", {"success": False, "valid": True, "token": token, "message": "点击下方按钮，确认这个邮箱地址属于你。"})


@router.post("/verify-email", response_class=HTMLResponse)
def verify_email_confirm(request: Request, token: str = Form(...)):
    token_hash = hash_verification_token(token.strip())
    with closing(connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        user = conn.execute(
            "SELECT id, email_verification_sent_at FROM users WHERE email_verification_token_hash=? AND email_verified=0",
            (token_hash,),
        ).fetchone()
        if not user or not is_verification_token_fresh(user["email_verification_sent_at"]):
            conn.rollback()
            return templates.TemplateResponse(request, "verify_email.html", {"success": False, "valid": False, "token": "", "message": "验证链接无效或已过期。请登录控制台后重新发送验证邮件。"}, status_code=400)
        now = now_iso()
        changed = conn.execute(
            "UPDATE users SET email_verified=1, email_verified_at=?, email_verification_token_hash=NULL, email_verification_sent_at=NULL, updated_at=? WHERE id=? AND email_verification_token_hash=? AND email_verified=0",
            (now, now, user["id"], token_hash),
        ).rowcount
        if changed != 1:
            conn.rollback()
            return templates.TemplateResponse(request, "verify_email.html", {"success": False, "valid": False, "token": "", "message": "验证链接无效或已经使用过。"}, status_code=400)
        conn.commit()
    return templates.TemplateResponse(request, "verify_email.html", {"success": True, "valid": False, "token": "", "message": "邮箱已验证，可返回登录页面。"})


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, reset: str = ""):
    return templates.TemplateResponse(request, "login.html", {"error": None, "message": "密码已重置，请重新登录。" if reset == "1" else None})


@router.head("/login")
def login_head() -> None:
    return None


@router.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    identity = client_identity(request)
    reservation = reserve_login_attempt(username, identity.address, identity.verified)
    if reservation is None:
        return templates.TemplateResponse(request, "login.html", {"error": "尝试次数过多，请 15 分钟后再试。", "username": username}, status_code=429, headers={"Retry-After": "900"})
    try:
        user = legacy_override("authenticate_user", authenticate_user)(username, password)
    except Exception:
        cancel_login_attempt(reservation)
        raise
    if not user:
        return templates.TemplateResponse(request, "login.html", {"error": "用户名或密码不正确，请重试。", "username": username}, status_code=401)
    release_login_attempt(reservation)
    with closing(connect()) as conn, conn:
        conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now_iso(), user["id"]))
        conn.commit()
    response = RedirectResponse("/admin" if user["role"] == "admin" else "/developer", status_code=303)
    response.set_cookie(SESSION_COOKIE, create_session(user["id"], user_agent=request.headers.get("user-agent", ""), source_ip=identity.address, expected_password_hash=user["password_hash"]), max_age=SESSION_MAX_AGE_SECONDS, httponly=True, secure=True, samesite="lax")
    return response


@router.post("/logout")
def logout(request: Request):
    revoke_session(request.cookies.get(SESSION_COOKIE))
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(SESSION_COOKIE, secure=True, httponly=True, samesite="lax")
    return response


# Responses deliberately do not reveal existence, status, throttling or SMTP configuration.
RESET_MESSAGE = "如果该邮箱对应可用账户，你将收到密码重置邮件，请检查收件箱。"


@router.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_page(request: Request):
    return templates.TemplateResponse(request, "forgot_password.html", {"message": None})


@router.post("/forgot-password", response_class=HTMLResponse)
def forgot_password_submit(request: Request, email: str = Form(...)):
    from app.auth import api_user_eligible
    from app.email_verification import claim_email_send, send_password_reset_email

    email = email.strip().lower()
    if claim_email_send(email, client_address(request), table="password_reset_limits"):
        with closing(connect()) as conn:
            user = conn.execute("SELECT id,username,email,status FROM users WHERE lower(email)=?", (email,)).fetchone()
        if user and api_user_eligible(user["status"]):
            token = secrets.token_urlsafe(32)
            now = datetime.now(timezone.utc)
            with closing(connect()) as conn, conn:
                conn.execute("INSERT INTO password_reset_tokens(token_hash,user_id,created_at,expires_at) VALUES (?,?,?,?)",
                             (hash_verification_token(token), user["id"], now.isoformat(), (now + timedelta(minutes=60)).isoformat()))
            try:
                send_password_reset_email(user["email"], user["username"], token)
            except Exception as exc:
                logger.warning("password_reset_mail_failed request_id=%s error_type=%s", diagnostic_request_id(request), type(exc).__name__)
    return templates.TemplateResponse(request, "forgot_password.html", {"message": RESET_MESSAGE})


def reset_response(request, token, *, valid=False, error=None, status=200, retry_after=None):
    headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return templates.TemplateResponse(request, "reset_password.html", {"valid": valid, "token": token if valid else "", "error": error},
        status_code=status, headers=headers)


def active_reset(conn, token):
    return conn.execute("""SELECT t.*,u.password_hash FROM password_reset_tokens t JOIN users u ON u.id=t.user_id
        WHERE t.token_hash=? AND t.used_at IS NULL AND julianday(t.expires_at)>julianday(?)
          AND u.status IN ('active','pending')""", (hash_verification_token(token), datetime.now(timezone.utc).isoformat())).fetchone() if token else None


@router.get("/reset-password", response_class=HTMLResponse)
def reset_password_page(request: Request, token: str = ""):
    with closing(connect()) as conn:
        valid = active_reset(conn, token) is not None
    return reset_response(request, token, valid=valid, error=None if valid else "重置链接无效或已过期，请重新申请。", status=200 if valid else 400)


@router.post("/reset-password", response_class=HTMLResponse)
def reset_password_submit(request: Request, token: str = "", new_password: str = Form(...),
                          confirm_password: str = Form(...)):
    from app.auth import revoke_user_sessions
    with closing(connect()) as conn:
        valid = active_reset(conn, token) is not None
    identity = client_identity(request)
    retry_after = claim_password_reset_attempt(
        hash_verification_token(token), identity.address, verified=identity.verified, valid_token=valid,
    )
    if retry_after:
        return reset_response(request, token, error="尝试次数过多，请稍后重试。", status=429, retry_after=retry_after)
    if not valid:
        return reset_response(request, token, error="重置链接无效或已过期，请重新申请。", status=400)
    if len(new_password) < 10 or new_password != confirm_password:
        return reset_response(request, token, valid=True, error="新密码至少需要 10 个字符，且两次输入必须一致。", status=400)
    # Cheap admission precedes PBKDF2, which stays outside the write transaction.
    encoded = hash_password(new_password)
    with closing(connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        record = active_reset(conn, token)
        if not record:
            return reset_response(request, token, error="重置链接无效或已过期，请重新申请。", status=400)
        now = now_iso()
        conn.execute("UPDATE users SET password_hash=?,updated_at=? WHERE id=?", (encoded, now, record["user_id"]))
        conn.execute("UPDATE password_reset_tokens SET used_at=? WHERE user_id=? AND used_at IS NULL", (now, record["user_id"]))
        revoke_user_sessions(conn, record["user_id"])
    response = RedirectResponse("/login?reset=1", status_code=303, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
    response.delete_cookie(SESSION_COOKIE, secure=True, httponly=True, samesite="lax")
    return response
