import base64
import hashlib
import hmac
import secrets
import re
from datetime import datetime, timedelta, timezone
from app.utils.timefmt import now_iso
from contextlib import closing
from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, Request, status

from app.config import settings
from app.db import connect


SESSION_COOKIE = "luomoapi_session"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 12
PBKDF2_ITERATIONS = 260_000


def api_user_eligible(user_status: str) -> bool:
    """Registration permits API access; suspension still revokes it immediately."""
    return user_status in {"active", "pending"}


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256:{PBKDF2_ITERATIONS}:{salt}:{base64.b64encode(digest).decode('ascii')}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        separator = ":" if ":" in encoded else "$"
        algorithm, iterations_text, salt, expected = encoded.split(separator, 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), int(iterations_text))
        actual = base64.b64encode(digest).decode("ascii")
        return secrets.compare_digest(actual, expected)
    except Exception:
        return False


def _sign(payload: str) -> str:
    return hmac.new(settings.session_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(user_id_or_username: int | str, *, user_agent: str = "", source_ip: str | None = None,
                   expected_password_hash: str | None = None) -> str:
    """Issue an opaque 12-hour session; only its SHA-256 digest is persisted.

    Accept an existing numeric user ID or username, including for isolated tests.
    Login supplies the authenticated password hash to guard concurrent resets.
    """
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    with closing(connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        field = "id" if isinstance(user_id_or_username, int) else "username"
        user = conn.execute(f"SELECT * FROM users WHERE {field}=?", (user_id_or_username,)).fetchone()
        if not user:
            raise ValueError("Session owner does not exist.")
        if expected_password_hash is not None and (not api_user_eligible(user["status"]) or user["password_hash"] != expected_password_hash):
            raise HTTPException(401, "账户凭据已变更，请重新登录。")
        conn.execute("""INSERT INTO user_sessions(token_hash,user_id,created_at,expires_at,last_seen_at,user_agent,ip_hash)
                        VALUES (?,?,?,?,?,?,?)""",
                     (hash_session_token(token), user["id"], now.isoformat(),
                      (now + timedelta(seconds=SESSION_MAX_AGE_SECONDS)).isoformat(), now.isoformat(),
                      user_agent[:512], hash_session_token(source_ip) if source_ip else None))
    return token


def session_user(token: str | None, request: Request | None = None):
    # Reject legacy signed cookies before any DB lookup.
    if not token or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        return None
    now = datetime.now(timezone.utc)
    with closing(connect()) as conn, conn:
        row = conn.execute("""SELECT u.*, s.id AS session_id, s.last_seen_at AS session_last_seen
            FROM user_sessions s JOIN users u ON u.id=s.user_id
            WHERE s.token_hash=? AND s.revoked_at IS NULL AND julianday(s.expires_at)>julianday(?)
              AND u.status IN ('active','pending')""", (hash_session_token(token), now.isoformat())).fetchone()
        if row is None:
            return None
        cutoff = (now - timedelta(minutes=5)).isoformat()
        if row["session_last_seen"] <= cutoff:
            # The predicate makes concurrent readers share one update per 5 minutes.
            conn.execute("UPDATE user_sessions SET last_seen_at=? WHERE id=? AND last_seen_at<=? AND revoked_at IS NULL",
                         (now.isoformat(), row["session_id"], cutoff))
        if request is not None:
            request.state.session_id = row["session_id"]
        return row


def verify_session(token: str | None) -> str | None:
    user = session_user(token)
    return user["username"] if user else None


def revoke_session(token: str | None) -> None:
    if token:
        with closing(connect()) as conn, conn:
            conn.execute("UPDATE user_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                         (now_iso(), hash_session_token(token)))


def revoke_user_sessions(conn, user_id: int, *, except_id: int | None = None) -> None:
    conn.execute("UPDATE user_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL AND (? IS NULL OR id!=?)",
                 (now_iso(), user_id, except_id, except_id))


def get_user_by_username(username: str):
    with closing(connect()) as conn:
        return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def authenticate_user(username: str, password: str):
    user = get_user_by_username(username)
    if not user or user["status"] in {"disabled", "suspended"}:
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    return user


def current_user(
    request: Request,
    session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) :
    user = session_user(session_token, request)
    if user:
        return user
    if "text/html" in request.headers.get("accept", ""):
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)


def optional_user(request: Request, session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None):
    return session_user(session_token, request)


def require_admin(user=Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def require_developer(user=Depends(current_user)):
    if user["role"] not in {"admin", "developer"}:
        raise HTTPException(status_code=403, detail="Developer access required")
    return user
