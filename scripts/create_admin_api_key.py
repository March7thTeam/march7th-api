#!/usr/bin/env python3
import base64
import hashlib
import os
import secrets
import sys
from functools import partial
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.utils.timefmt import now_iso as _now_iso
from app.db import connect_database

now_iso = partial(_now_iso, timespec="microseconds")


DB_PATH = os.getenv("DATABASE_PATH", "data/luomoapi.db")



def hash_key(api_key: str) -> str:
    iterations = 260000
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        api_key.encode("utf-8"),
        salt,
        iterations,
    )
    return "pbkdf2_sha256${}${}${}".format(
        iterations,
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def connect():
    if not Path(DB_PATH).exists():
        raise SystemExit(f"Database not found: {DB_PATH}")

    return connect_database(DB_PATH, require_exists=True)


def main():
    conn = connect()

    try:
        user = conn.execute(
            "SELECT id, username, role, status FROM users WHERE username=?",
            ("luomo",),
        ).fetchone()

        if not user:
            raise SystemExit("Admin user luomo not found.")

        if user["status"] != "active":
            raise SystemExit("Admin user luomo is not active.")

        raw_key = "lapi_" + secrets.token_urlsafe(36)
        key_prefix = raw_key[:14] + "..."
        scopes = "luomocore:health"

        conn.execute(
            """
            INSERT INTO api_keys (
                user_id,
                name,
                key_prefix,
                key_hash,
                status,
                scopes,
                allowed_routes,
                rate_limit_per_minute,
                daily_quota,
                monthly_quota,
                created_at,
                approved_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user["id"],
                "Admin test key",
                key_prefix,
                hash_key(raw_key),
                "active",
                scopes,
                "luomocore/health",
                60,
                1000,
                30000,
                now_iso(),
                now_iso(),
            ),
        )

        conn.commit()

        print("API key created.")
        print("This key is shown only once. Copy it now:")
        print()
        print(raw_key)
        print()
        print("Key prefix:", key_prefix)
        print("Scopes:", scopes)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
