"""Versioned, forward-only schema changes.

Everything that existed before version 1 is the baseline: the idempotent
``CREATE TABLE IF NOT EXISTS`` / ``ADD COLUMN`` steps in ``app.db``,
``credit_ledger``, ``dispatch_recovery``, ``self_service_keys``,
``login_throttle`` and ``credit_payments``. Those keep running on every start
and are not rewritten here.

New schema changes are appended to ``MIGRATIONS`` as one numbered step each and
never edited afterwards. A step runs once, inside the caller's transaction, and
is recorded in ``schema_migrations`` in that same transaction, so a failed
upgrade leaves neither the change nor its record behind.
"""
from __future__ import annotations

import sqlite3

from app.utils.timefmt import now_iso


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_xinfo({table})")}


def _key_credit_limit(conn: sqlite3.Connection) -> None:
    """Optional lifetime spending cap per API key, in integer credit units; NULL means no cap."""
    if "credit_limit_units" not in _columns(conn, "api_keys"):
        conn.execute("""ALTER TABLE api_keys ADD COLUMN credit_limit_units INTEGER
            CHECK(credit_limit_units IS NULL OR (typeof(credit_limit_units)='integer' AND credit_limit_units>0))""")


def _owner_limits(conn):
    for field, kind in [('owner_credit_limit_units', "INTEGER CHECK(owner_credit_limit_units IS NULL OR (typeof(owner_credit_limit_units)='integer' AND owner_credit_limit_units>0))"),
                        ('owner_rpm', "INTEGER CHECK(owner_rpm IS NULL OR (typeof(owner_rpm)='integer' AND owner_rpm>0))"),
                        ('owner_expires_at', 'TEXT'), ('owner_models_json', 'TEXT')]:
        if field not in _columns(conn, 'api_keys'):
            conn.execute(f'ALTER TABLE api_keys ADD COLUMN {field} {kind}')


def _price_versions(conn):
    from app.price_store import install_schema
    install_schema(conn)


MIGRATIONS = (
    (1, "api_keys_credit_limit", _key_credit_limit),
    (2, "owner_limit_overlay", _owner_limits),
    (3, "immutable_price_versions", _price_versions),
)


def apply_migrations(conn: sqlite3.Connection) -> list[int]:
    """Apply pending steps in order within the caller's transaction; returns the versions applied."""
    if not conn.in_transaction:
        raise RuntimeError("Schema migrations must run inside the caller's transaction.")
    conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)""")
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    done = []
    for version, name, step in MIGRATIONS:
        if version in applied:
            continue
        step(conn)
        conn.execute("INSERT INTO schema_migrations(version,name,applied_at) VALUES (?,?,?)", (version, name, now_iso()))
        done.append(version)
    return done


def schema_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    except sqlite3.DatabaseError:
        return 0
    return int(row[0] or 0)
