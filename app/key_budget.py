"""Optional lifetime credit cap per API key.

A key's committed amount is everything its requests have cost or may still
cost: settled charges, plus the full frozen amount of holds that are still in
flight, plus the larger of frozen/known cost for holds awaiting review.
Released holds count as zero. The cap is compared against that total plus the
new request's reservation, inside the same ``BEGIN IMMEDIATE`` transaction that
creates the reservation, so concurrent requests cannot jointly exceed it.

The wallet remains the only place money lives; a cap never grants credit.
"""
from __future__ import annotations

import sqlite3
from typing import Any


def key_credit_limit(key_row: Any) -> int | None:
    limits = []
    for field in ('credit_limit_units', 'owner_credit_limit_units'):
        try:
            value = key_row[field]
        except (KeyError, IndexError, TypeError):
            continue
        if type(value) is int and value > 0:
            limits.append(value)
    return min(limits) if limits else None


def key_committed_units(conn: sqlite3.Connection, key_id: int) -> int:
    """Summed in Python so a large history cannot overflow SQLite's 64-bit SUM."""
    rows = conn.execute("""SELECT h.status, h.amount_units, h.actual_units
        FROM api_usage_logs a JOIN credit_holds h ON h.usage_id=a.id
        WHERE a.api_key_id=? AND h.status!='released'""", (int(key_id),))
    total = 0
    for status, amount, actual in rows:
        if status == "settled":
            total += int(actual or 0)
        elif status == "held":
            total += int(amount or 0)
        else:  # reconciliation_required: the known cost may exceed the freeze
            total += max(int(amount or 0), int(actual or 0))
    return total


def key_budget(conn: sqlite3.Connection, key_row: Any) -> dict | None:
    """``None`` when the key has no cap; otherwise limit, committed and remaining units."""
    limit = key_credit_limit(key_row)
    if limit is None:
        return None
    committed = key_committed_units(conn, int(key_row["id"]))
    return {"limit_units": limit, "committed_units": committed, "remaining_units": max(0, limit - committed)}


def key_budget_exceeded(conn: sqlite3.Connection, key_row: Any, reservation_units: int) -> bool:
    budget = key_budget(conn, key_row)
    return budget is not None and budget["committed_units"] + int(reservation_units) > budget["limit_units"]


def key_charge_allowed(conn: sqlite3.Connection, hold, actual_units: int) -> bool:
    """Replace this hold's commitment with the charge under the settlement lock.

    A cap tightened during I/O takes effect here too. Zero releases always help;
    reliable over-cap receipts remain pending, without silently charging the cap.
    """
    if actual_units == 0:
        return True
    key = conn.execute('SELECT k.* FROM api_keys k JOIN api_usage_logs a ON a.api_key_id=k.id WHERE a.id=?', (hold['usage_id'],)).fetchone()
    if key is None:
        return True  # Historical usage without an API key has no per-key cap.
    limit = key_credit_limit(key)
    if limit is None:
        return True
    previous = (int(hold['actual_units'] or 0) if hold['status']=='settled' else
                max(int(hold['amount_units'] or 0), int(hold['actual_units'] or 0))
                if hold['status']=='reconciliation_required' else int(hold['amount_units'] or 0))
    return key_committed_units(conn,key['id']) - previous + actual_units <= limit
