"""Two explicit due queues recovered inside an IMMEDIATE transaction."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import logging
import sqlite3

from app import credit_ledger as ledger


MAX_DISPATCH_RECOVERY_ATTEMPTS = 5
WATCH_RECOVERY_BATCH_SIZE = 100

PENDING_QUERY = """SELECT usage_id, request_id FROM usage_dispatch_pending
    WHERE next_attempt_at<=? ORDER BY next_attempt_at, usage_id LIMIT ?"""
WATCH_QUERY = """SELECT usage_id FROM credit_hold_watch
    WHERE due_at<=? ORDER BY due_at, usage_id LIMIT ?"""
logger = logging.getLogger("luomoapi.credits")


def parse_timestamp(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def is_stale(value: str | None, cutoff: str) -> bool:
    parsed = parse_timestamp(value)
    boundary = parse_timestamp(cutoff)
    return parsed is not None and boundary is not None and parsed <= boundary


def initialize_dispatch_schema(conn: sqlite3.Connection) -> None:
    ledger._tx(conn)
    conn.execute("""CREATE TABLE IF NOT EXISTS usage_dispatch_pending (
        usage_id INTEGER PRIMARY KEY REFERENCES api_usage_logs(id),
        request_id TEXT,
        created_at TEXT NOT NULL,
        next_attempt_at TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT)""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_usage_dispatch_pending_next
        ON usage_dispatch_pending(next_attempt_at, usage_id)""")
    watch_exists = conn.execute("""SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='credit_hold_watch'""").fetchone()
    conn.execute("""CREATE TABLE IF NOT EXISTS credit_hold_watch (
        usage_id INTEGER PRIMARY KEY REFERENCES credit_holds(usage_id),
        due_at TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT)""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_credit_hold_watch_due
        ON credit_hold_watch(due_at, usage_id)""")
    if not watch_exists:
        # A finite, one-time migration. Absence from the unsent queue is not
        # proof of historical dispatch, so these holds can only enter review.
        for usage_id, created_at in conn.execute("""SELECT h.usage_id, h.created_at
            FROM credit_holds h WHERE h.status='held' AND NOT EXISTS (
                SELECT 1 FROM usage_dispatch_pending q WHERE q.usage_id=h.usage_id)"""):
            enqueue_hold_watch(conn, usage_id, created_at, legacy=True)


def enqueue_usage(conn: sqlite3.Connection, usage_id: int, request_id: str | None,
                  created_at: str, *, review_only: bool = False) -> None:
    # Direct ledger callers may lack explicit unsent log evidence. Make these
    # candidates visible immediately; only the hold's age can trigger review.
    ledger._tx(conn)
    created = parse_timestamp(created_at)
    if created is None:
        raise ValueError("Invalid usage creation timestamp")
    conn.execute("""INSERT INTO usage_dispatch_pending
        (usage_id, request_id, created_at, next_attempt_at) VALUES (?,?,?,?)
        ON CONFLICT(usage_id) DO NOTHING""",
        (usage_id, request_id, timestamp(created),
         timestamp(created if review_only else created + timedelta(minutes=ledger.STALE_HOLD_MINUTES))))


def enqueue_hold_watch(conn: sqlite3.Connection, usage_id: int, created_at: str,
                       *, legacy: bool = False) -> None:
    ledger._tx(conn)
    try:
        created = parse_timestamp(created_at)
        if created is None:
            raise ValueError("Invalid hold creation timestamp")
        due_at = timestamp(created + timedelta(minutes=ledger.STALE_HOLD_MINUTES))
        error = None
    except (ValueError, OverflowError):
        if not legacy:
            raise
        # Retain malformed historical evidence without guessing an expiry.
        due_at = timestamp(datetime.max.replace(tzinfo=timezone.utc))
        error = "Invalid historical hold creation timestamp; manual review required."
        logger.error("hold_watch_invalid_timestamp usage_id=%s created_at=%r", usage_id, created_at)
    conn.execute("""INSERT OR IGNORE INTO credit_hold_watch(usage_id, due_at, last_error)
        VALUES (?,?,?)""", (usage_id, due_at, error))


@contextmanager
def _savepoint(conn: sqlite3.Connection, name: str):
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
        conn.execute(f"RELEASE {name}")
    except Exception:
        conn.execute(f"ROLLBACK TO {name}")
        conn.execute(f"RELEASE {name}")
        raise


def _backoff(conn: sqlite3.Connection, usage_id: int, request_id: str | None,
             now: datetime, error: Exception, *, watch: bool = False,
             increment: bool = True) -> None:
    table, due_column, event = ("credit_hold_watch", "due_at", "stale_held") if watch else (
        "usage_dispatch_pending", "next_attempt_at", "stale_usage")
    try:
        with _savepoint(conn, "recovery_backoff"):
            conn.execute(f"""UPDATE {table} SET attempts=attempts+?,
                last_error=?, {due_column}=? WHERE usage_id=?""",
                (int(increment), str(error)[:2000],
                 timestamp(now + timedelta(seconds=ledger.RECOVERY_RETRY_SECONDS)), usage_id))
    except Exception:
        logger.exception("%s_backoff_failed usage_id=%s request_id=%s", event, usage_id, request_id)


def _require_reconciliation(conn: sqlite3.Connection, usage_id: int, reason: str,
                            *, preserve_existing: bool = False) -> None:
    conn.execute("DELETE FROM credit_hold_watch WHERE usage_id=?", (usage_id,))
    hold = conn.execute("SELECT status FROM credit_holds WHERE usage_id=?", (usage_id,)).fetchone()
    if hold:
        if hold["status"] not in {"held", "reconciliation_required"}:
            return
        if preserve_existing and hold["status"] == "reconciliation_required":
            return
        changed = conn.execute("""UPDATE credit_holds SET status='reconciliation_required',
            reason=?, updated_at=? WHERE usage_id=? AND status=?""",
            (reason, ledger.now_iso(), usage_id, hold["status"])).rowcount
        if changed != 1:
            raise ledger.CreditError("reservation_conflict", "转待核时预留状态已变化。", 409)
    # Preserve amounts, receipts, dispatch evidence and quota state verbatim.
    changed = conn.execute("""UPDATE api_usage_logs SET credit_status='reconciliation_required',
        credit_reason=? WHERE id=? AND (?='dispatch_conflict'
            OR (dispatch_state='reserved' AND completed_at IS NULL))""",
        (reason, usage_id, reason)).rowcount
    if changed != 1:
        raise ledger.CreditError("reservation_conflict", "转待核时日志状态已变化。", 409)


def _escalate_failed_dispatch(conn: sqlite3.Connection, usage_id: int,
                              request_id: str | None, now: datetime, cutoff: str) -> None:
    # Recheck persisted retry evidence in a savepoint independent of both the
    # failed release and its backoff. Escalation failure keeps the queue intact.
    with _savepoint(conn, "dispatch_escalation"):
        row = conn.execute("""SELECT q.attempts, q.last_error, a.id AS log_id,
            a.dispatch_state, a.completed_at FROM usage_dispatch_pending q
            LEFT JOIN api_usage_logs a ON a.id=q.usage_id WHERE q.usage_id=? AND q.request_id IS ?""",
            (usage_id, request_id)).fetchone()
        if not row or row["attempts"] < MAX_DISPATCH_RECOVERY_ATTEMPTS:
            return
        if row["log_id"] is not None and row["dispatch_state"] != "reserved":
            _recover_unsent(conn, usage_id, request_id, now, cutoff)
            return
        if row["log_id"] is None or row["dispatch_state"] != "reserved" or row["completed_at"] is not None:
            conn.execute("DELETE FROM usage_dispatch_pending WHERE usage_id=?", (usage_id,))
            return
        _require_reconciliation(conn, usage_id, "dispatch_recovery_failed")
        conn.execute("DELETE FROM usage_dispatch_pending WHERE usage_id=?", (usage_id,))
    logger.error("dispatch_recovery_escalated usage_id=%s request_id=%s last_error=%s",
                 usage_id, request_id, row["last_error"])


def _recover_unsent(conn: sqlite3.Connection, usage_id: int, request_id: str | None,
                    now: datetime, cutoff: str) -> int:
    row = conn.execute("SELECT * FROM api_usage_logs WHERE id=?", (usage_id,)).fetchone()
    hold = conn.execute("SELECT * FROM credit_holds WHERE usage_id=?", (usage_id,)).fetchone()
    if row and row["dispatch_state"] != "reserved" and hold and hold["status"] == "held":
        if not is_stale(hold["created_at"], cutoff):
            conn.execute("UPDATE usage_dispatch_pending SET next_attempt_at=? WHERE usage_id=?",
                         (timestamp(now + timedelta(seconds=ledger.RECOVERY_RETRY_SECONDS)), usage_id))
            return 0
        logger.error("unsent_queue_unsafe_log usage_id=%s request_id=%s dispatch_state=%s",
                     usage_id, request_id, row["dispatch_state"])
        _require_reconciliation(conn, usage_id, "dispatch_conflict", preserve_existing=True)
        conn.execute("DELETE FROM usage_dispatch_pending WHERE usage_id=?", (usage_id,))
        return 0
    if not row or row["dispatch_state"] != "reserved" or row["completed_at"] is not None:
        conn.execute("DELETE FROM usage_dispatch_pending WHERE usage_id=?", (usage_id,))
        return 0
    if hold and (hold["dispatch_state"] != "reserved" or hold["status"] not in {"held", "reconciliation_required"}):
        logger.error("unsent_queue_unsafe_hold usage_id=%s request_id=%s status=%s dispatch_state=%s",
                     usage_id, request_id, hold["status"], hold["dispatch_state"])
        _require_reconciliation(conn, usage_id, "dispatch_conflict", preserve_existing=True)
        conn.execute("DELETE FROM usage_dispatch_pending WHERE usage_id=?", (usage_id,))
        return 0
    if not is_stale(row["started_at"] if row["started_at"] is not None else row["created_at"], cutoff) or (
            hold and not is_stale(hold["created_at"], cutoff)):
        # A changed or unknown timestamp must not occupy the front of the queue.
        conn.execute("UPDATE usage_dispatch_pending SET next_attempt_at=? WHERE usage_id=?",
                     (timestamp(now + timedelta(seconds=ledger.RECOVERY_RETRY_SECONDS)), usage_id))
        return 0
    if hold and not ledger.release_reserved_credit(conn, usage_id, reason="stale_not_dispatched", stale_before=cutoff):
        raise ledger.CreditError("reservation_conflict", "未发送预留状态已变化。", 409)
    ledger._complete_stale_unsent(conn, usage_id, cutoff)
    return 1


def recover_dispatch_queue(conn: sqlite3.Connection, now: datetime) -> int:
    ledger._tx(conn)
    cutoff = timestamp(now - timedelta(minutes=ledger.STALE_HOLD_MINUTES))
    rows = conn.execute(PENDING_QUERY, (timestamp(now), ledger.RECOVERY_BATCH_SIZE)).fetchall()
    recovered = 0
    for usage_id, request_id in rows:
        try:
            with _savepoint(conn, "dispatch_record"):
                attempts = conn.execute("SELECT attempts FROM usage_dispatch_pending WHERE usage_id=?",
                                        (usage_id,)).fetchone()[0]
                if attempts >= MAX_DISPATCH_RECOVERY_ATTEMPTS:
                    changed = 0
                else:
                    changed = _recover_unsent(conn, usage_id, request_id, now, cutoff)
            recovered += changed
        except Exception as exc:
            logger.exception("stale_usage_record_failed usage_id=%s request_id=%s", usage_id, request_id)
            _backoff(conn, usage_id, request_id, now, exc)
        try:
            _escalate_failed_dispatch(conn, usage_id, request_id, now, cutoff)
        except Exception as exc:
            logger.exception("stale_usage_escalation_failed usage_id=%s request_id=%s", usage_id, request_id)
            # Once at the limit, only escalation is retried. Its own failure
            # also yields the due position while retaining the retry limit.
            _backoff(conn, usage_id, request_id, now, exc, increment=False)

    # Each phase has its own budget, for at most 100 + 100 candidate rows.
    watched = conn.execute(WATCH_QUERY, (timestamp(now), WATCH_RECOVERY_BATCH_SIZE)).fetchall()
    for (usage_id,) in watched:
        request_id = None
        try:
            with _savepoint(conn, "stale_held_record"):
                row = conn.execute("SELECT request_id FROM api_usage_logs WHERE id=?", (usage_id,)).fetchone()
                request_id = row[0] if row else None
                changed = conn.execute("""UPDATE credit_holds SET status='reconciliation_required',
                    reason='stale_hold', updated_at=? WHERE usage_id=? AND status='held'""",
                    (ledger.now_iso(), usage_id)).rowcount
                if changed:
                    conn.execute("""UPDATE api_usage_logs SET credit_status='reconciliation_required',
                        credit_reason='stale_hold' WHERE id=?""", (usage_id,))
                conn.execute("DELETE FROM credit_hold_watch WHERE usage_id=?", (usage_id,))
            recovered += changed
        except Exception as exc:
            logger.exception("stale_held_record_failed usage_id=%s request_id=%s", usage_id, request_id)
            _backoff(conn, usage_id, request_id, now, exc, watch=True)
    return recovered
