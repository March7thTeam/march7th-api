from contextlib import closing
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from app.config import settings
from app.db import connect
from app.ai_catalog import snapshot

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.head("/health")
def health_head() -> None:
    return None


@router.get("/ready")
def ready():
    try:
        with closing(connect()) as conn, conn:
            ready_tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('users','api_keys','public_gateway_routes','credit_wallets','credit_holds','credit_ledger','credit_codes','credit_payment_orders','credit_payment_reviews','self_key_claims','login_throttle','user_sessions','password_reset_tokens','password_reset_limits')"
                ).fetchall()
            }
            conn.execute("SELECT 1").fetchone()
        if len(ready_tables) != 14:
            raise RuntimeError("Required database tables are missing.")
    except Exception:
        return JSONResponse({"status": "not_ready", "checks": {"database": "failed"}}, status_code=503)
    if not settings.session_secret or len(settings.session_secret) < 32:
        return JSONResponse({"status": "not_ready", "checks": {"database": "ok", "session_secret": "missing_or_too_short"}}, status_code=503)
    return JSONResponse({"status": "ready", "checks": {"database": "ok", "session_secret": "ok"}})


@router.get("/api/public/status")
async def api_public_status() -> dict:
    return await snapshot()


@router.head("/")
def public_home_head() -> None:
    return None


@router.head("/docs")
def docs_head() -> None:
    return None

