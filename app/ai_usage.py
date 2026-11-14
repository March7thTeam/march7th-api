"""Usage accounting, model grants, and bounded upstream usage extraction.

The gateway deliberately records a request before it leaves the process.  The
reservation is committed in a short ``BEGIN IMMEDIATE`` transaction so that
rate and quota checks include concurrent in-flight requests.  Callers must
close their database connection before doing network I/O and later call
``complete_usage`` from a fresh connection.
"""

from __future__ import annotations

from functools import partial
from app.utils.timefmt import now_iso as _now_iso

import json
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
import logging
from app.auth import api_user_eligible
from app.admin_key_policy import ADMIN_EXEMPT_REASON, AdminKeyPolicyError, admin_unmetered
from app.credit_ledger import (CreditError, mark_credit_dispatched, recover_stale_usage,
                               release_reserved_credit, reserve_credit, settle_credit)
from app.dispatch_recovery import enqueue_usage
from app.key_budget import key_budget_exceeded
from app.credit_pricing import BillingQuote, PricingError, billing_details, billing_receipt_present, calculate_charge


UTC = timezone.utc
MAX_SUMMARY_DAYS = 366
GENERATION_PROTOCOLS = ("chat.completions", "responses", "images.generations")


@dataclass(frozen=True)
class QuotaRejection:
    code: str
    message: str
    status_code: int = 429


@dataclass(frozen=True)
class UsageReservation:
    id: int
    request_id: str
    started_at: str
    # ``None`` means policy=all; an empty tuple means an explicit empty
    # allowlist.  Captured under the same reservation lock for /v1/models.
    model_ids: tuple[str, ...] | None = None
    admin_unmetered: bool = False


def finish_unsent_usage(connect, reservation: UsageReservation, **completion) -> bool:
    """Try two independent database connections, honoring their busy timeout."""
    logger = logging.getLogger("luomoapi.usage")
    for attempt in range(2):
        try:
            with closing(connect()) as conn:
                if complete_usage(conn, reservation, dispatched=False, **completion):
                    return True
        except Exception:
            logger.exception("unsent_cleanup_attempt_failed usage_id=%s request_id=%s",
                             reservation.id, reservation.request_id)
        if attempt == 0:
            time.sleep(0.05)
    logger.error("unsent_cleanup_failed usage_id=%s request_id=%s",
                 reservation.id, reservation.request_id)
    return False


def mark_usage_dispatched(conn: sqlite3.Connection, reservation: UsageReservation) -> bool:
    """Commit the send boundary and the credit marker in one short transaction."""
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    updated = conn.execute("""UPDATE api_usage_logs SET dispatch_state='dispatched'
        WHERE id=? AND request_id=? AND completed_at IS NULL AND dispatch_state='reserved'""",
        (reservation.id, reservation.request_id))
    if updated.rowcount != 1:
        conn.rollback()
        raise RuntimeError("Usage reservation is no longer dispatchable")
    if conn.execute("SELECT 1 FROM credit_holds WHERE usage_id=?", (reservation.id,)).fetchone():
        mark_credit_dispatched(conn, reservation.id)
    conn.execute("DELETE FROM usage_dispatch_pending WHERE usage_id=?", (reservation.id,))
    conn.commit()
    return True


def _row_value(row: Any, name: str, default: Any = None) -> Any:
    """Read a value from sqlite rows and ordinary mappings safely."""

    if row is None:
        return default
    try:
        return row[name]
    except (KeyError, IndexError, TypeError):
        return default


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        return None


iso_now = partial(_now_iso, timespec="microseconds")

def get_key_models(conn: sqlite3.Connection, key_id: int) -> list[str]:
    """Return the exact model IDs assigned to an API key.

    An old database may be inspected before the migration has run.  Returning
    an empty list in that case keeps the console and key inspection pages
    readable while ``model_policy=all`` continues to preserve old keys.
    """

    try:
        rows = conn.execute(
            "SELECT model_id FROM api_key_models WHERE api_key_id=? ORDER BY model_id",
            (int(key_id),),
        ).fetchall()
    except (sqlite3.DatabaseError, TypeError, ValueError):
        return []
    return [str(row["model_id"] if isinstance(row, sqlite3.Row) else row[0]) for row in rows]


def model_allowed(conn: sqlite3.Connection, key_row: Any, model_id: str) -> bool:
    """Check one exact model grant, preserving the legacy ``all`` policy."""

    if not isinstance(model_id, str) or not model_id:
        return False
    from app.key_settings import effective_model_ids
    allowed = effective_model_ids(conn, key_row)
    return allowed is None or model_id in allowed


def _quota_limits(key_row: Any) -> tuple[int | None, int | None, int | None]:
    def value(name: str) -> int | None:
        raw = _row_value(key_row, name)
        if raw is None or raw == "":
            return None
        try:
            parsed = int(raw)
            # The console policy form defines zero as unlimited.  Negative
            # values are treated as absent here; policy validation rejects
            # them before a key is issued.
            return parsed if parsed > 0 else None
        except (TypeError, ValueError):
            return None

    # None means no configured limit.  Existing schema defaults are supplied
    # by SQLite and therefore arrive here as integers for normal keys.
    rates = [v for v in (value("rate_limit_per_minute"), value("owner_rpm")) if v is not None]
    return min(rates) if rates else None, value("daily_quota"), value("monthly_quota")


def _key_expired(key_row: Any, now: datetime) -> bool:
    for field in ('expires_at', 'owner_expires_at'):
        raw = _row_value(key_row, field)
        expiry = _parse_datetime(raw)
        if raw and (expiry is None or expiry <= now):
            return True
    return False


def _usage_count(conn: sqlite3.Connection, key_id: int, since: datetime) -> int:
    # COALESCE keeps old rows (which predate started_at) in quota accounting.
    row = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM api_usage_logs
        WHERE api_key_id=? AND COALESCE(started_at, created_at)>=?
          AND COALESCE(protocol, '') <> 'models'
          AND public_path NOT IN ('v1/models', '/v1/models')
          AND COALESCE(dispatch_state, '') <> 'not_dispatched'
          AND NOT (COALESCE(protocol, '')='public'
                   AND COALESCE(outcome, '') IN ('route_changed', 'route_disabled', 'not_found'))
        """,
        (key_id, since.isoformat()),
    ).fetchone()
    return int(row["c"] if isinstance(row, sqlite3.Row) else row[0])


def quota_rejection(conn: sqlite3.Connection, key_row: Any, now: datetime | None = None) -> QuotaRejection | None:
    """Check expiry, minute, daily, and monthly request limits.

    This function intentionally does not begin a transaction.  Use
    ``reserve_usage`` for the atomic check-and-insert needed by request paths.
    """

    now = (now or datetime.now(UTC)).astimezone(UTC)
    if _key_expired(key_row, now):
        return QuotaRejection("key_expired", "API key has expired.", 403)
    key_id = _row_value(key_row, "id")
    if key_id is None:
        return QuotaRejection("quota_error", "API key quota could not be checked.", 503)
    try:
        key_id = int(key_id)
    except (TypeError, ValueError):
        return QuotaRejection("quota_error", "API key quota could not be checked.", 503)

    per_minute, daily, monthly = _quota_limits(key_row)
    if per_minute is not None and _usage_count(conn, key_id, now - timedelta(seconds=60)) >= per_minute:
        return QuotaRejection("rate_limited", "API key rate limit exceeded.")

    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if daily is not None and _usage_count(conn, key_id, day_start) >= daily:
        return QuotaRejection("quota_exceeded", "API key daily quota exceeded.")

    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if monthly is not None and _usage_count(conn, key_id, month_start) >= monthly:
        return QuotaRejection("monthly_quota_exceeded", "API key monthly quota exceeded.")
    return None


def public_route_rejection(
    conn: sqlite3.Connection, route_id: int, expected_route: Any = None,
) -> QuotaRejection | None:
    """Recheck the exact target and authorization used to construct a request."""
    route = conn.execute("""SELECT r.*, a.base_url, a.auth_type, a.secret_ref,
        a.enabled AS upstream_enabled, a.updated_at AS upstream_updated_at
        FROM public_gateway_routes r JOIN apis a ON a.id=r.target_api_id WHERE r.id=?""",
        (route_id,)).fetchone()
    if not route:
        return QuotaRejection("not_found", "Public route not found.", 404)
    if not route["enabled"] or not route["upstream_enabled"]:
        return QuotaRejection("route_disabled", "This route is documented but not currently enabled.", 404)
    fields = ("target_api_id", "base_url", "target_method", "target_path", "required_scope",
              "allow_anonymous", "enabled", "upstream_enabled", "auth_type", "secret_ref",
              "public_path", "updated_at", "upstream_updated_at")
    if expected_route is not None and any(route[name] != _row_value(expected_route, name) for name in fields):
        return QuotaRejection("route_changed", "This route changed during admission; retry the request.", 404)
    return None


def reserve_usage(
    conn: sqlite3.Connection,
    key_row: Any,
    *,
    request_id: str,
    protocol: str,
    model: str | None,
    streamed: bool,
    method: str,
    public_path: str,
    route_id: int | None = None,
    route_snapshot: Any = None,
    target_url_masked: str | None = None,
    client_ip_hash: str | None = None,
    user_agent_preview: str | None = None,
    enforce_quota: bool = True,
    private_scope: str | None = None,
    require_private_grant: bool = False,
    model_id: str | None = None,
    billing_quote: BillingQuote | None = None,
    expected_admin_unmetered: bool | None = None,
    session_id: int | None = None,
    expected_user_id: int | None = None,
) -> tuple[UsageReservation | None, QuotaRejection | None]:
    """Atomically check quotas and insert an in-flight usage reservation."""

    key_id = _row_value(key_row, "id")
    if key_id is None:
        return None, QuotaRejection("quota_error", "API key quota could not be checked.", 503)
    started_at = iso_now()
    try:
        recover_stale_usage(conn)
        # BEGIN IMMEDIATE serializes the check and reservation.  No caller may
        # perform HTTP work while this transaction or connection is retained.
        conn.execute("BEGIN IMMEDIATE")
        if protocol == "public" and route_id is not None:
            rejection = public_route_rejection(conn, route_id, route_snapshot)
            if rejection:
                conn.rollback()
                return None, rejection
        # Re-read mutable admission state after acquiring the write lock.  A
        # concurrent revoke, suspension, expiry, scope, or model-policy edit
        # therefore takes effect before this request leaves the process.
        current = conn.execute(
            """
            SELECT k.*, u.role AS user_role, u.status AS user_status
            FROM api_keys k JOIN users u ON u.id=k.user_id
            WHERE k.id=?
            """,
            (int(key_id),),
        ).fetchone()
        if not current or current["status"] != "active" or not api_user_eligible(current["user_status"]):
            conn.rollback()
            return None, QuotaRejection("permission_error", "User or API key is not active.", 403)
        if expected_user_id is not None:
            live_session = conn.execute("""SELECT 1 FROM user_sessions WHERE id=? AND user_id=?
                AND revoked_at IS NULL AND julianday(expires_at)>julianday(?)""",
                (session_id, expected_user_id, started_at)).fetchone()
            if current["user_id"] != expected_user_id or not live_session:
                conn.rollback()
                return None, QuotaRejection("permission_error", "Session or key ownership changed; sign in again.", 403)
        # The models endpoint deliberately skips request-count quota checks,
        # but expiry remains an admission rule and must be checked again under
        # this transaction after the mutable row has been re-read.
        if _key_expired(current, datetime.now(UTC)):
            conn.rollback()
            return None, QuotaRejection("key_expired", "API key has expired.", 403)
        try:
            exempt = admin_unmetered(current, owner_role=current["user_role"], owner_status=current["user_status"])
        except AdminKeyPolicyError as exc:
            conn.rollback()
            return None, QuotaRejection("permission_error", str(exc), 403)
        # Old callers are ordinary, never implicitly exempt. Refuse both
        # directions of a policy change after request shaping/authentication.
        if exempt != (expected_admin_unmetered is True):
            conn.rollback()
            return None, QuotaRejection("authorization_changed", "API key authorization changed; retry the request.", 403)
        if require_private_grant:
            scopes = {part.strip() for part in str(current["scopes"] or "").replace(",", " ").split() if part.strip()}
            routes = {part.strip() for part in str(current["allowed_routes"] or "").replace(",", " ").split() if part.strip()}
            legacy_ok = private_scope != "images:generate" and "codexproxy:use" in scopes and "codexproxy/*" in routes
            new_ok = private_scope is not None and private_scope in scopes
            if not (legacy_ok or new_ok):
                conn.rollback()
                return None, QuotaRejection("permission_error", "API key is not authorized for this endpoint.", 403)
        if model_id is not None and not model_allowed(conn, current, model_id):
            conn.rollback()
            return None, QuotaRejection("model_not_allowed", "API key is not authorized for this model.", 403)
        rejection = quota_rejection(conn, current) if enforce_quota else None
        if rejection:
            conn.rollback()
            return None, rejection
        if protocol in GENERATION_PROTOCOLS and billing_quote is None and not exempt:
            conn.rollback()
            return None, QuotaRejection("billing_quote_missing", "A credit reservation is required for generation.", 503)
        # The owner's optional per-key cap is checked under the same write lock
        # as the reservation, against this key's settled and still-frozen spend.
        if billing_quote is not None and not exempt and key_budget_exceeded(conn, current, billing_quote.amount_units):
            conn.rollback()
            return None, QuotaRejection("key_credit_limit_exceeded",
                "This API key has reached its credit limit. Raise the limit or use another key.", 402)
        current_model_ids: tuple[str, ...] | None = None
        if protocol == 'models':
            from app.key_settings import effective_model_ids
            allowed = effective_model_ids(conn, current)
            current_model_ids = tuple(sorted(allowed)) if allowed is not None else None
        cursor = conn.execute(
            """
            INSERT INTO api_usage_logs(
                created_at, user_id, api_key_id, route_id, method, public_path,
                target_url_masked, status_code, response_time_ms, success,
                client_ip_hash, user_agent_preview, error_summary,
                request_preview, response_preview, request_id, protocol, model,
                streamed, outcome, started_at, completed_at, first_byte_ms,
                input_tokens, output_tokens, total_tokens, usage_reported,
                credit_status, credit_reserved_units, credit_charged_units, credit_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, NULL, NULL, NULL,
                      ?, ?, ?, ?, 'in_flight', ?, NULL, NULL, NULL, NULL, NULL, 0,
                      ?, 0, 0, ?)
            """,
            (
                started_at,
                current["user_id"],
                int(key_id),
                route_id,
                method.upper(),
                public_path,
                target_url_masked,
                client_ip_hash,
                user_agent_preview,
                request_id,
                protocol,
                model,
                1 if streamed else 0,
                started_at,
                "admin_exempt" if exempt else "legacy_unbilled",
                ADMIN_EXEMPT_REASON if exempt else None,
            ),
        )
        conn.execute("UPDATE api_usage_logs SET dispatch_state='reserved' WHERE id=?", (cursor.lastrowid,))
        if billing_quote is not None and not exempt:
            reserve_credit(conn, int(cursor.lastrowid), int(current["user_id"]), billing_quote.amount_units, billing_quote.snapshot)
        enqueue_usage(conn, int(cursor.lastrowid), request_id, started_at)
        conn.commit()
        return UsageReservation(int(cursor.lastrowid), request_id, started_at, current_model_ids, exempt), None
    except CreditError as exc:
        conn.rollback()
        return None, QuotaRejection(exc.code, exc.message, exc.status)
    except sqlite3.OperationalError as exc:
        try:
            conn.rollback()
        except sqlite3.DatabaseError:
            pass
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            return None, QuotaRejection("quota_busy", "API key quota is temporarily busy.", 503)
        return None, QuotaRejection("quota_error", "API key quota could not be recorded.", 503)
    except sqlite3.DatabaseError:
        try:
            conn.rollback()
        except sqlite3.DatabaseError:
            pass
        return None, QuotaRejection("quota_error", "API key quota could not be recorded.", 503)


def _normalise_outcome(outcome: str | None, status_code: int | None, success: bool | None) -> str:
    if outcome:
        return str(outcome)[:64]
    if success is True or (success is None and status_code is not None and 200 <= status_code < 400):
        return "succeeded"
    return "failed"


def complete_usage(
    conn: sqlite3.Connection,
    reservation: UsageReservation | int,
    *,
    status_code: int | None,
    response_time_ms: int | None,
    first_byte_ms: int | None,
    outcome: str | None = None,
    success: bool | None = None,
    error_summary: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    total_tokens: int | None = None,
    usage_reported: bool = False,
    response_preview: str | None = None,
    completed_at: str | None = None,
    billing_usage: dict | None = None,
    billing_release: bool = False,
    dispatched: bool | None = None,
) -> bool:
    """Finalize a reservation from a fresh connection after network I/O."""

    row_id = reservation.id if isinstance(reservation, UsageReservation) else int(reservation)
    final_outcome = _normalise_outcome(outcome, status_code, success)
    if success is None:
        success = final_outcome in {"succeeded", "completed"} or (
            status_code is not None and 200 <= status_code < 400 and final_outcome not in {"streamerror", "client_disconnect", "upstream_error"}
        )
    if not usage_reported:
        input_tokens = output_tokens = total_tokens = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        hold = conn.execute("SELECT * FROM credit_holds WHERE usage_id=?", (row_id,)).fetchone()
        if dispatched is False:
            row = conn.execute("SELECT * FROM api_usage_logs WHERE id=?", (row_id,)).fetchone()
            if not row or (isinstance(reservation, UsageReservation) and row["request_id"] != reservation.request_id):
                conn.rollback()
                return False
            if row["dispatch_state"] == "not_dispatched" and row["completed_at"] is not None:
                if hold and hold["status"] != "released":
                    conn.rollback()
                    return False
                conn.execute("DELETE FROM usage_dispatch_pending WHERE usage_id=?", (row_id,))
                conn.commit()
                return True
            if hold and not release_reserved_credit(conn, row_id, reason="Request failed before dispatch."):
                conn.rollback()
                return False
            updated = conn.execute("""UPDATE api_usage_logs SET dispatch_state='not_dispatched'
                WHERE id=? AND dispatch_state='reserved' AND completed_at IS NULL""", (row_id,))
            if updated.rowcount != 1:
                conn.rollback()
                return False
        accepted_billing = False
        if hold and dispatched is not False:
            actual = None
            reason = "Upstream usage was unavailable; frozen funds require reconciliation."
            if billing_usage is not None:
                try:
                    actual = calculate_charge(hold["pricing_snapshot"], billing_usage)
                    reason = "Settled from authoritative upstream token usage."
                except PricingError as exc:
                    reason = exc.message
            elif billing_release:
                actual = 0
                reason = "Request was not dispatched or was explicitly rejected before generation."
            result = settle_credit(conn, row_id, actual, reason=reason, usage_json=json.dumps(billing_usage, separators=(",", ":"), sort_keys=True) if billing_usage else None)
            stored_receipt = conn.execute("SELECT usage_json FROM credit_holds WHERE usage_id=?", (row_id,)).fetchone()[0]
            try:
                stored_details = json.loads(stored_receipt) if stored_receipt is not None else None
            except (ValueError, TypeError):
                stored_details = None
            accepted_billing = (hold["status"] not in {"settled", "released"} and actual is not None
                                and billing_usage is not None and result["actual_units"] == actual
                                and stored_details == billing_usage)
            if accepted_billing:
                input_tokens, output_tokens = billing_usage["input_tokens"], billing_usage["output_tokens"]
                total_tokens, usage_reported = input_tokens + output_tokens, True
        conn.execute(
            """
            UPDATE api_usage_logs
            SET status_code=?, response_time_ms=?, success=?, error_summary=?,
                response_preview=?, outcome=?, completed_at=?, first_byte_ms=?,
                input_tokens=?, output_tokens=?, total_tokens=?, usage_reported=?
            WHERE id=? AND completed_at IS NULL
            """,
            (
                status_code,
                response_time_ms,
                1 if success else 0,
                error_summary,
                response_preview,
                final_outcome,
                completed_at or iso_now(),
                first_byte_ms,
                input_tokens,
                output_tokens,
                total_tokens,
                1 if usage_reported else 0,
                row_id,
            ),
        )
        if accepted_billing:
            # A late receipt completes billing statistics without rewriting the
            # original transport outcome, completion time, or response metadata.
            conn.execute("UPDATE api_usage_logs SET input_tokens=?,output_tokens=?,total_tokens=?,usage_reported=1 WHERE id=?",
                         (input_tokens, output_tokens, total_tokens, row_id))
        # Exemption affects money, not usage evidence. Keep the parsed receipt
        # even though no credit hold (and therefore no settlement) exists.
        receipt = billing_usage
        if receipt is None and usage_reported:
            receipt = {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": total_tokens}
        if receipt is not None:
            conn.execute("""UPDATE api_usage_logs SET credit_usage_json=?
                            WHERE id=? AND credit_status='admin_exempt' AND credit_usage_json IS NULL""",
                         (json.dumps(receipt, separators=(",", ":"), sort_keys=True), row_id))
        conn.execute("DELETE FROM usage_dispatch_pending WHERE usage_id=?", (row_id,))
        conn.commit()
        return conn.execute("SELECT 1 FROM api_usage_logs WHERE id=?", (row_id,)).fetchone() is not None
    except (sqlite3.DatabaseError, CreditError):
        try:
            conn.rollback()
        except sqlite3.DatabaseError:
            pass
        logging.getLogger("luomoapi.credits").exception("usage_settlement_failed usage_id=%s", row_id)
        return False


def insert_usage(
    conn: sqlite3.Connection,
    *,
    user_id: int | None,
    api_key_id: int | None,
    route_id: int | None,
    method: str,
    public_path: str,
    target_url_masked: str | None,
    status_code: int | None,
    response_time_ms: int | None,
    success: bool,
    client_ip_hash: str | None,
    user_agent_preview: str | None,
    error_summary: str | None,
    request_id: str | None = None,
    protocol: str | None = None,
    model: str | None = None,
    streamed: bool = False,
    outcome: str | None = None,
    started_at: str | None = None,
    completed_at: str | None = None,
    first_byte_ms: int | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    total_tokens: int | None = None,
    usage_reported: bool = False,
    request_preview: str | None = None,
    response_preview: str | None = None,
    dispatched: bool | None = None,
) -> int | None:
    """Insert a completed non-reserved usage row for legacy callers."""

    started_at = started_at or iso_now()
    completed_at = completed_at or started_at
    if not usage_reported:
        input_tokens = output_tokens = total_tokens = None
    try:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        if route_id is not None and not conn.execute("SELECT 1 FROM public_gateway_routes WHERE id=?", (route_id,)).fetchone():
            route_id = None
        cur = conn.execute(
            """
            INSERT INTO api_usage_logs(
                created_at, user_id, api_key_id, route_id, method, public_path,
                target_url_masked, status_code, response_time_ms, success,
                client_ip_hash, user_agent_preview, error_summary,
                request_preview, response_preview, request_id, protocol, model,
                streamed, outcome, started_at, completed_at, first_byte_ms,
                input_tokens, output_tokens, total_tokens, usage_reported, dispatch_state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                started_at,
                user_id,
                api_key_id,
                route_id,
                method.upper(),
                public_path,
                target_url_masked,
                status_code,
                response_time_ms,
                1 if success else 0,
                client_ip_hash,
                user_agent_preview,
                error_summary,
                request_preview,
                response_preview,
                request_id,
                protocol,
                model,
                1 if streamed else 0,
                _normalise_outcome(outcome, status_code, success),
                started_at,
                completed_at,
                first_byte_ms,
                input_tokens,
                output_tokens,
                total_tokens,
                1 if usage_reported else 0,
                None if dispatched is None else ("dispatched" if dispatched else "not_dispatched"),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    except sqlite3.DatabaseError:
        try:
            conn.rollback()
        except sqlite3.DatabaseError:
            pass
        logging.getLogger("luomoapi.usage").exception("usage_insert_failed request_id=%s public_path=%s", request_id, public_path)
        return None


def _int_usage(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _usage_from_mapping(value: Any) -> tuple[int | None, int | None, int | None, bool]:
    if not isinstance(value, dict):
        return None, None, None, False
    # OpenAI Chat uses prompt/completion tokens; Responses uses input/output.
    input_tokens = _int_usage(value.get("input_tokens", value.get("prompt_tokens")))
    output_tokens = _int_usage(value.get("output_tokens", value.get("completion_tokens")))
    total_tokens = _int_usage(value.get("total_tokens"))
    reported = any(item is not None for item in (input_tokens, output_tokens, total_tokens))
    return input_tokens, output_tokens, total_tokens, reported


def extract_usage(payload: Any, *, protocol: str, completed_only: bool = False) -> tuple[int | None, int | None, int | None, bool]:
    """Extract usage from documented OpenAI response locations only.

    Chat Completions reports ``usage`` at the response/chunk top level.
    Responses reports it at the top level for JSON responses and under
    ``response.usage`` for completion events.  Deliberately avoiding a
    recursive walk prevents a tool result or arbitrary content object with a
    field named ``usage`` from being counted as billing metadata.
    """

    if not isinstance(payload, dict):
        return None, None, None, False
    protocol = (protocol or "").lower()
    completed_types = {"response.completed", "response.done", "response.complete", "completed", "done", "response.incomplete", "response.failed"}
    event_type = str(payload.get("type", "")).lower()
    if completed_only and event_type not in completed_types:
        return None, None, None, False

    candidate = _usage_from_mapping(payload.get("usage"))
    if candidate[3]:
        return candidate
    if protocol.startswith("response"):
        response = payload.get("response")
        if isinstance(response, dict):
            candidate = _usage_from_mapping(response.get("usage"))
            if candidate[3]:
                return candidate
    return None, None, None, False


def extract_json_usage(body: bytes, *, protocol: str) -> tuple[int | None, int | None, int | None, bool]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, None, None, False
    return extract_usage(payload, protocol=protocol, completed_only=False)


MAX_SSE_EVENT_BYTES = 1024 * 1024


def _sse_separator(value: bytes) -> tuple[int, int] | None:
    """Return the earliest supported SSE event separator and its byte size."""

    candidates = [(value.find(b"\n\n"), 2), (value.find(b"\r\n\r\n"), 4)]
    candidates = [(index, size) for index, size in candidates if index >= 0]
    return min(candidates, key=lambda item: item[0]) if candidates else None


class SSEUsageParser:
    """Incrementally inspect SSE events without changing their raw bytes."""

    def __init__(self, *, protocol: str, require_billing_details: bool = True):
        self.protocol = protocol
        self.require_billing_details = require_billing_details
        self._buffer = b""
        self._usage: tuple[int | None, int | None, int | None, bool] = (None, None, None, False)
        self.terminal = False
        self.stream_complete = False
        self.error = False
        self.saw_event = False
        self.usage_unknown = False
        self._discarding_oversize_event = False
        self.billing_usage: dict | None = None
        self.billing_receipt_seen = False
        self.response_model: str | None = None
        self._receipt_locked = False

    def _accept_receipt(self, candidate, details) -> None:
        if details is None and self.require_billing_details:
            self.usage_unknown = True
            return
        receipt_changed = self.billing_usage != details
        if not self.require_billing_details:
            receipt_changed = self._usage != candidate or (
                self.billing_usage is not None and details is not None and self.billing_usage != details)
        if self._receipt_locked and receipt_changed:
            self.usage_unknown = True
            return
        self._usage = candidate
        if details is not None:
            self.billing_usage = details
        self._receipt_locked = True

    @property
    def usage(self) -> tuple[int | None, int | None, int | None, bool]:
        # A discarded event may have contained the authoritative usage
        # object.  Do not expose an earlier partial value as if it were final.
        return (None, None, None, False) if self.usage_unknown else self._usage

    def _consume_event(self, event: bytes) -> None:
        data_lines: list[bytes] = []
        event_type = ""
        for line in event.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n"):
            if line.startswith(b"data:"):
                data_lines.append(line[5:].lstrip(b" "))
            elif line.startswith(b"event:"):
                try:
                    event_type = line[6:].strip().decode("utf-8", "ignore").lower()
                except Exception:
                    event_type = ""
        if not data_lines:
            return
        raw = b"\n".join(data_lines).strip()
        if not raw:
            return
        if raw == b"[DONE]":
            self.terminal = True
            self.stream_complete = True
            self.saw_event = True
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        self.saw_event = True
        if billing_receipt_present(payload, protocol=self.protocol):
            self.billing_receipt_seen = True
        lowered_type = event_type or (str(payload.get("type", "")).lower() if isinstance(payload, dict) else "")
        if isinstance(payload, dict):
            obj = payload.get("response") if isinstance(payload.get("response"), dict) else payload
            if isinstance(obj.get("model"), str):
                self.response_model = obj["model"]
        if self.protocol.lower().startswith("response"):
            if lowered_type in {"response.completed", "response.done", "response.complete", "completed", "done"}:
                self.terminal = True
                self.stream_complete = True
            elif lowered_type in {"response.failed", "response.incomplete", "response.error", "error", "failed", "incomplete"}:
                self.terminal = True
                self.stream_complete = True
                self.error = True
        elif isinstance(payload, dict):
            if lowered_type in {"error", "response.error"} or isinstance(payload.get("error"), dict):
                self.terminal = True
                self.stream_complete = True
                self.error = True
            choices = payload.get("choices")
            if isinstance(choices, list) and any(isinstance(item, dict) and item.get("finish_reason") is not None for item in choices):
                self.terminal = True
        if self.protocol.lower().startswith("response"):
            # Some servers expose the event type only as an SSE field while
            # others put it in data.type; make the latter explicit for the
            # completed-only traversal.
            if isinstance(payload, dict) and event_type and "type" not in payload:
                payload = {"type": event_type, **payload}
            candidate = extract_usage(payload, protocol=self.protocol, completed_only=True)
        else:
            candidate = extract_usage(payload, protocol=self.protocol, completed_only=False)
        choices = payload.get("choices") if isinstance(payload, dict) else None
        final_chat_receipt = isinstance(choices, list) and (not choices or all(
            isinstance(item, dict) and item.get("finish_reason") is not None for item in choices))
        final_chat_receipt = final_chat_receipt or (choices is None and self.terminal)
        if candidate[3]:
            details = billing_details(payload, protocol=self.protocol)
            if details is not None and details.get("model") is None:
                details["model"] = self.response_model
            if self.protocol.lower().startswith("response") or final_chat_receipt or self._receipt_locked:
                self._accept_receipt(candidate, details)
        elif (self.stream_complete or (not self.protocol.lower().startswith("response") and final_chat_receipt)) and billing_receipt_present(payload, protocol=self.protocol):
            # An incomplete final receipt must not expose an earlier statistic.
            self.usage_unknown = True

    def feed(self, chunk: bytes) -> None:
        if not isinstance(chunk, (bytes, bytearray)):
            return
        self._buffer += bytes(chunk)
        # SSE event separators are blank lines.  Retain an incomplete event
        # so arbitrary upstream chunk boundaries never lose usage metadata.
        while True:
            separator = _sse_separator(self._buffer)
            if separator is None:
                if len(self._buffer) > MAX_SSE_EVENT_BYTES:
                    # Keep only enough tail bytes to recognize a separator
                    # split across chunks.  Raw relay remains untouched.
                    self.usage_unknown = True
                    self._discarding_oversize_event = True
                    self._buffer = self._buffer[-3:]
                break
            index, separator_len = separator
            event = self._buffer[:index]
            self._buffer = self._buffer[index + separator_len :]
            if self._discarding_oversize_event or len(event) > MAX_SSE_EVENT_BYTES:
                self.usage_unknown = True
                self._discarding_oversize_event = False
                continue
            if not event:
                continue
            self._consume_event(event)

    def finish(self) -> None:
        if self._buffer.strip() and not self._discarding_oversize_event and len(self._buffer) <= MAX_SSE_EVENT_BYTES:
            self._consume_event(self._buffer)
        elif self._buffer.strip() or self._discarding_oversize_event:
            self.usage_unknown = True
        self._buffer = b""
        self._discarding_oversize_event = False


def _summary_cutoff(days: int) -> datetime:
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 7
    days = min(MAX_SUMMARY_DAYS, max(1, days))
    return datetime.now(UTC) - timedelta(days=days)


@dataclass(frozen=True)
class UsageWindow:
    start: datetime
    end: datetime


def usage_window(days: int) -> UsageWindow:
    """Compute one half-open UTC calendar window shared by charts and details."""
    if isinstance(days, bool) or not isinstance(days, int) or days not in {7, 14, 30}:
        raise ValueError("days must be 7, 14 or 30")
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return UsageWindow(today - timedelta(days=days - 1), today + timedelta(days=1))


def _usage_window_filter(window: UsageWindow, timestamp: str) -> tuple[str, list[str]]:
    # SQLite datetime() normalizes Z, offsets, space separators and naive UTC.
    # First search the existing expression index over a date envelope. The
    # extra day on each side covers every supported ISO timezone offset.
    where = f"""{timestamp}>=? AND {timestamp}<?
        AND datetime({timestamp})>=? AND datetime({timestamp})<?"""
    return where, [
        str((window.start - timedelta(days=1)).date()),
        str((window.end + timedelta(days=1)).date()),
        window.start.strftime("%Y-%m-%d %H:%M:%S"),
        window.end.strftime("%Y-%m-%d %H:%M:%S"),
    ]


def read_usage_summary(user_id: int | None, days: int = 7, *, window: UsageWindow | None = None) -> dict[str, Any]:
    """Return dashboard-safe usage aggregates and at most 100 recent rows.

    ``window`` shares the chart's UTC boundaries and normalization. Without
    it the legacy rolling ``days`` cutoff and timestamp behavior are preserved.
    """

    from app.db import connect  # local import avoids importing app config here

    # Models/list and the older public gateway have separate accounting; this
    # dashboard includes text and image generation while keeping the same summary keys.
    timestamp = "COALESCE(started_at, created_at)"
    if window is None:
        time_filter, args = f"{timestamp}>=?", [_summary_cutoff(days).isoformat()]
        day_sql = f"substr({timestamp}, 1, 10)"
        order_sql = timestamp
    else:
        time_filter, args = _usage_window_filter(window, timestamp)
        day_sql = f"date(datetime({timestamp}))"
        order_sql = f"datetime({timestamp})"
    where = "protocol IN (" + ",".join("?" for _ in GENERATION_PROTOCOLS) + ") AND " + time_filter
    args = [*GENERATION_PROTOCOLS, *args]
    if user_id is not None:
        where += " AND user_id=?"
        args.append(int(user_id))

    summary: dict[str, Any] = {
        "requests": 0,
        "completed": 0,
        "succeeded": 0,
        "failed": 0,
        "in_flight": 0,
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "usage_reported_requests": 0,
        "avg_latency_ms": None,
    }
    by_model: list[dict[str, Any]] = []
    daily: list[dict[str, Any]] = []
    recent: list[dict[str, Any]] = []
    with connect() as conn:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS requests,
                   SUM(CASE WHEN completed_at IS NOT NULL OR (outcome IS NOT NULL AND outcome!='in_flight') THEN 1 ELSE 0 END) AS completed,
                   SUM(CASE WHEN outcome IN ('succeeded','completed') OR success=1 THEN 1 ELSE 0 END) AS succeeded,
                   SUM(CASE WHEN (completed_at IS NOT NULL OR (outcome IS NOT NULL AND outcome!='in_flight')) AND COALESCE(success,0)=0 THEN 1 ELSE 0 END) AS failed,
                   SUM(CASE WHEN outcome='in_flight' AND completed_at IS NULL THEN 1 ELSE 0 END) AS in_flight,
                   SUM(CASE WHEN usage_reported=1 THEN input_tokens ELSE NULL END) AS input_tokens,
                   SUM(CASE WHEN usage_reported=1 THEN output_tokens ELSE NULL END) AS output_tokens,
                   SUM(CASE WHEN usage_reported=1 THEN total_tokens ELSE NULL END) AS total_tokens,
                   SUM(CASE WHEN usage_reported=1 THEN 1 ELSE 0 END) AS usage_reported_requests,
                   AVG(CASE WHEN completed_at IS NOT NULL THEN response_time_ms END) AS avg_latency_ms
            FROM api_usage_logs WHERE {where}
            """,
            args,
        ).fetchone()
        if row:
            for key in summary:
                value = row[key]
                if key in {"completed", "succeeded", "failed", "in_flight", "usage_reported_requests"}:
                    summary[key] = int(value or 0)
                elif key in {"input_tokens", "output_tokens", "total_tokens"}:
                    summary[key] = int(value) if value is not None else None
                elif key == "avg_latency_ms":
                    summary[key] = round(float(value), 2) if value is not None else None
                elif key == "requests":
                    summary[key] = int(value or 0)

        try:
            rows = conn.execute(
                f"""
                SELECT COALESCE(model, '') AS model, COUNT(*) AS requests,
                       SUM(CASE WHEN outcome IN ('succeeded','completed') OR success=1 THEN 1 ELSE 0 END) AS succeeded,
                       SUM(CASE WHEN (completed_at IS NOT NULL OR (outcome IS NOT NULL AND outcome!='in_flight')) AND COALESCE(success,0)=0 THEN 1 ELSE 0 END) AS failed,
                       SUM(CASE WHEN usage_reported=1 THEN total_tokens ELSE NULL END) AS total_tokens
                FROM api_usage_logs WHERE {where}
                GROUP BY COALESCE(model, '') ORDER BY requests DESC, model
                """,
                args,
            ).fetchall()
            by_model = [
                {"model": row["model"] or None, "requests": int(row["requests"] or 0), "succeeded": int(row["succeeded"] or 0), "failed": int(row["failed"] or 0), "total_tokens": int(row["total_tokens"]) if row["total_tokens"] is not None else None}
                for row in rows
            ]
            rows = conn.execute(
                f"""
                SELECT {day_sql} AS day,
                       COUNT(*) AS requests,
                       SUM(CASE WHEN outcome IN ('succeeded','completed') OR success=1 THEN 1 ELSE 0 END) AS succeeded,
                       SUM(CASE WHEN (completed_at IS NOT NULL OR (outcome IS NOT NULL AND outcome!='in_flight')) AND COALESCE(success,0)=0 THEN 1 ELSE 0 END) AS failed,
                       SUM(CASE WHEN usage_reported=1 THEN total_tokens ELSE NULL END) AS total_tokens
                FROM api_usage_logs WHERE {where}
                GROUP BY day ORDER BY day
                """,
                args,
            ).fetchall()
            daily = [
                {"day": row["day"], "requests": int(row["requests"] or 0), "succeeded": int(row["succeeded"] or 0), "failed": int(row["failed"] or 0), "total_tokens": int(row["total_tokens"]) if row["total_tokens"] is not None else None}
                for row in rows
            ]
            rows = conn.execute(
                f"""
                SELECT id, created_at, request_id, protocol, model, streamed,
                       outcome, status_code, response_time_ms, first_byte_ms,
                       input_tokens, output_tokens, total_tokens, usage_reported,
                       credit_status, credit_charged_units
                FROM api_usage_logs WHERE {where}
                ORDER BY {order_sql} DESC, id DESC LIMIT 100
                """,
                args,
            ).fetchall()
            recent = [dict(row) for row in rows]
            from app.credit_ledger import units_to_credits
            for item in recent:
                item["charged_credits"] = units_to_credits(item["credit_charged_units"] or 0)
        except sqlite3.DatabaseError:
            # An old read-only database can still serve the zero summary while
            # it is awaiting startup migration.
            pass
    return {"summary": summary, "by_model": by_model, "daily": daily, "recent": recent}


__all__ = [
    "QuotaRejection",
    "UsageReservation",
    "SSEUsageParser",
    "complete_usage",
    "extract_json_usage",
    "extract_usage",
    "get_key_models",
    "insert_usage",
    "model_allowed",
    "quota_rejection",
    "read_usage_summary",
    "reserve_usage",
    "usage_analytics",
]


def usage_analytics(user_id: int | None, days: int = 14, *, window: UsageWindow | None = None,
                    protocols: Iterable[str] | None = None) -> dict:
    """UTC calendar-day aggregates; settled hold units are the charge authority.

    Sum integer units in Python so administrator totals cannot overflow SQLite's
    signed 64-bit SUM or acquire floating-point rounding. ``protocols=None``
    preserves the all-protocol API default; pages opt into generation only.
    """
    from contextlib import closing
    from app.db import connect
    from app.credit_ledger import units_to_credits

    if isinstance(days, bool) or not isinstance(days, int) or days not in {7, 14, 30}:
        raise ValueError("days must be 7, 14 or 30")
    window = window or usage_window(days)
    start = window.start.date()
    today = (window.end - timedelta(days=1)).date()
    fields = ("requests", "succeeded", "failed", "in_flight", "input_tokens", "output_tokens", "actual_units")

    def empty(**labels):
        return {**labels, **dict.fromkeys(fields, 0)}

    def add(target, values):
        for name in fields:
            target[name] += values[name]

    def display(row):
        return {**{k: v for k, v in row.items() if k != "actual_units"}, "credits": units_to_credits(row["actual_units"])}

    daily = {str(start + timedelta(days=i)): empty(day=str(start + timedelta(days=i))) for i in range(days)}
    models, keys = {}, {}
    totals = empty()
    where, params = _usage_window_filter(window, "COALESCE(a.started_at,a.created_at)")
    if protocols is not None:
        protocols = tuple(protocols)
        where += " AND a.protocol IN (" + ",".join("?" for _ in protocols) + ")"
        params.extend(protocols)
    if user_id is not None:
        where += " AND a.user_id=?"
        params.append(user_id)
    with closing(connect()) as conn:
        # One read snapshot includes counters and charges from the same settlement.
        rows = conn.execute(f"""SELECT date(datetime(COALESCE(a.started_at,a.created_at))) AS day,
            a.model,a.api_key_id,k.name AS key_name,k.key_prefix,a.success,a.outcome,a.completed_at,
            a.input_tokens,a.output_tokens,
            CASE WHEN h.status='settled' THEN h.actual_units ELSE 0 END AS actual_units
            FROM api_usage_logs a LEFT JOIN api_keys k ON k.id=a.api_key_id AND k.user_id=a.user_id
            LEFT JOIN credit_holds h ON h.usage_id=a.id AND h.user_id=a.user_id WHERE {where}""", params)
        for row in rows:
            success = row["success"] == 1 or row["outcome"] in {"succeeded", "completed"}
            in_flight = row["success"] is None and row["outcome"] == "in_flight" and not row["completed_at"]
            values = dict(requests=1, succeeded=int(success), failed=int(not success and not in_flight),
                          in_flight=int(in_flight), input_tokens=int(row["input_tokens"] or 0),
                          output_tokens=int(row["output_tokens"] or 0), actual_units=int(row["actual_units"] or 0))
            model = row["model"] or "未记录模型"
            key_id = row["api_key_id"]
            model_row = models.setdefault(model, empty(model=model, name=model))
            key_row = keys.setdefault(key_id, empty(key_id=key_id, name=row["key_name"] or "未关联密钥", key_prefix=row["key_prefix"]))
            for target in (daily[row["day"]], totals, model_row, key_row):
                add(target, values)

    def top(mapping, kind):
        ordered = sorted(mapping.values(), key=lambda r: (-r["requests"], str(r.get(kind))))
        result = ordered[:10]
        if len(ordered) > 10:
            other = empty(name="其他", **{kind: None}, other=True)
            for row in ordered[10:]:
                add(other, row)
            result.append(other)
        return [display(row) for row in result]

    return {"days": days, "timezone": "UTC", "start_date": str(start), "end_date": str(today),
            "totals": display(totals), "daily": [display(row) for row in daily.values()],
            "by_model": top(models, "model"), "by_key": top(keys, "key_id")}
