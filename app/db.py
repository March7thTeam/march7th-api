import sqlite3
from contextlib import closing
from pathlib import Path

from app.config import ensure_dirs, settings


SCHEMA = """
CREATE TABLE IF NOT EXISTS apis (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    base_url TEXT NOT NULL,
    auth_type TEXT DEFAULT 'none',
    secret_ref TEXT,
    description TEXT,
    tags TEXT,
    enabled INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS endpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    api_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    description TEXT,
    default_headers TEXT,
    default_query TEXT,
    default_body TEXT,
    enabled INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(api_id) REFERENCES apis(id)
);
CREATE TABLE IF NOT EXISTS request_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    api_id INTEGER,
    endpoint_id INTEGER,
    method TEXT NOT NULL,
    url_masked TEXT NOT NULL,
    status_code INTEGER,
    response_time_ms INTEGER,
    success INTEGER,
    error_summary TEXT,
    caller TEXT,
    request_preview TEXT,
    response_preview TEXT
);
CREATE TABLE IF NOT EXISTS secrets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    key TEXT NOT NULL UNIQUE,
    value_encrypted TEXT,
    status TEXT DEFAULT 'missing',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gateway_routes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    public_path TEXT NOT NULL UNIQUE,
    api_id INTEGER NOT NULL,
    endpoint_id INTEGER,
    require_token INTEGER DEFAULT 1,
    rate_limit_per_minute INTEGER DEFAULT 60,
    enabled INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(api_id) REFERENCES apis(id),
    FOREIGN KEY(endpoint_id) REFERENCES endpoints(id)
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'developer',
    status TEXT NOT NULL DEFAULT 'active',
    usage_reason TEXT,
    email_verified INTEGER DEFAULT 0,
    email_verification_token_hash TEXT,
    email_verification_sent_at TEXT,
    email_verified_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_login_at TEXT
);
CREATE TABLE IF NOT EXISTS verification_email_limits (
    bucket_hash TEXT PRIMARY KEY,
    window_started_at TEXT NOT NULL,
    last_sent_at TEXT NOT NULL,
    send_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS catalog_seed_state (
    seed_name TEXT PRIMARY KEY,
    seeded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    revoked_at TEXT,
    user_agent TEXT,
    ip_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON user_sessions(user_id, revoked_at, expires_at);
CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_password_reset_user ON password_reset_tokens(user_id);
CREATE TABLE IF NOT EXISTS password_reset_limits (
    bucket_hash TEXT PRIMARY KEY,
    window_started_at TEXT NOT NULL,
    last_sent_at TEXT NOT NULL,
    send_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS password_reset_attempts (
    bucket_hash TEXT PRIMARY KEY,
    window_started_at INTEGER NOT NULL,
    attempt_count INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_password_reset_attempts_window ON password_reset_attempts(window_started_at);
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    key_prefix TEXT NOT NULL,
    key_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    scopes TEXT,
    allowed_routes TEXT,
    model_policy TEXT NOT NULL DEFAULT 'all',
    rate_limit_per_minute INTEGER DEFAULT 60,
    daily_quota INTEGER DEFAULT 1000,
    monthly_quota INTEGER DEFAULT 30000,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    revoked_at TEXT,
    expires_at TEXT,
    last_used_at TEXT,
    FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS api_key_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    requested_scopes TEXT,
    requested_reason TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    admin_note TEXT,
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    reviewed_by INTEGER,
    FOREIGN KEY(user_id) REFERENCES users(id),
    FOREIGN KEY(reviewed_by) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS public_gateway_routes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    public_path TEXT NOT NULL UNIQUE,
    target_api_id INTEGER NOT NULL,
    target_endpoint_id INTEGER,
    target_method TEXT NOT NULL,
    target_path TEXT NOT NULL,
    required_scope TEXT,
    enabled INTEGER DEFAULT 1,
    public_docs INTEGER DEFAULT 1,
    allow_anonymous INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(target_api_id) REFERENCES apis(id),
    FOREIGN KEY(target_endpoint_id) REFERENCES endpoints(id)
);
CREATE TABLE IF NOT EXISTS api_usage_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    user_id INTEGER,
    api_key_id INTEGER,
    route_id INTEGER,
    method TEXT NOT NULL,
    public_path TEXT NOT NULL,
    target_url_masked TEXT,
    status_code INTEGER,
    response_time_ms INTEGER,
    success INTEGER,
    client_ip_hash TEXT,
    user_agent_preview TEXT,
    error_summary TEXT,
    request_preview TEXT,
    response_preview TEXT,
    request_id TEXT,
    protocol TEXT,
    model TEXT,
    streamed INTEGER DEFAULT 0,
    outcome TEXT,
    started_at TEXT,
    completed_at TEXT,
    first_byte_ms INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    total_tokens INTEGER,
    usage_reported INTEGER DEFAULT 0,
    FOREIGN KEY(user_id) REFERENCES users(id),
    FOREIGN KEY(api_key_id) REFERENCES api_keys(id),
    FOREIGN KEY(route_id) REFERENCES public_gateway_routes(id)
);
CREATE TABLE IF NOT EXISTS api_key_models (
    api_key_id INTEGER NOT NULL,
    model_id TEXT NOT NULL,
    PRIMARY KEY (api_key_id, model_id),
    FOREIGN KEY(api_key_id) REFERENCES api_keys(id) ON DELETE CASCADE
);
"""


def connect() -> sqlite3.Connection:
    ensure_dirs()
    return connect_database(settings.database_path)


def connect_database(path: str | Path, *, require_exists: bool = False) -> sqlite3.Connection:
    if require_exists and not Path(path).exists():
        raise RuntimeError(f"Database not found: {path}")
    # A short busy timeout lets concurrent reservation transactions queue
    # briefly.  The gateway still closes this connection before any HTTP I/O.
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    ensure_dirs()
    with closing(connect()) as conn, conn:
        conn.executescript(SCHEMA)
        conn.execute("BEGIN IMMEDIATE")
        _ensure_columns(conn)
        # Local import avoids coupling the accounting module to connection setup.
        from app.credit_ledger import initialize_credit_schema, recover_stale_usage
        initialize_credit_schema(conn)
        # Numbered steps come after the idempotent baseline above; see app/migrations.py.
        from app.migrations import apply_migrations
        apply_migrations(conn)
        conn.commit()
        recover_stale_usage(conn, force=True)


def _ensure_columns(conn: sqlite3.Connection) -> None:
    def columns(table: str) -> set[str]:
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}

    additions = {
        "users": {
            "email_verified": "ALTER TABLE users ADD COLUMN email_verified INTEGER DEFAULT 0",
            "email_verification_token_hash": "ALTER TABLE users ADD COLUMN email_verification_token_hash TEXT",
            "email_verification_sent_at": "ALTER TABLE users ADD COLUMN email_verification_sent_at TEXT",
            "email_verified_at": "ALTER TABLE users ADD COLUMN email_verified_at TEXT",
        },
        "api_keys": {
            "model_policy": "ALTER TABLE api_keys ADD COLUMN model_policy TEXT NOT NULL DEFAULT 'all'",
        },
        "api_usage_logs": {
            "dispatch_state": "ALTER TABLE api_usage_logs ADD COLUMN dispatch_state TEXT",
            "request_id": "ALTER TABLE api_usage_logs ADD COLUMN request_id TEXT",
            "protocol": "ALTER TABLE api_usage_logs ADD COLUMN protocol TEXT",
            "model": "ALTER TABLE api_usage_logs ADD COLUMN model TEXT",
            "streamed": "ALTER TABLE api_usage_logs ADD COLUMN streamed INTEGER DEFAULT 0",
            "outcome": "ALTER TABLE api_usage_logs ADD COLUMN outcome TEXT",
            "started_at": "ALTER TABLE api_usage_logs ADD COLUMN started_at TEXT",
            "completed_at": "ALTER TABLE api_usage_logs ADD COLUMN completed_at TEXT",
            "first_byte_ms": "ALTER TABLE api_usage_logs ADD COLUMN first_byte_ms INTEGER",
            "input_tokens": "ALTER TABLE api_usage_logs ADD COLUMN input_tokens INTEGER",
            "output_tokens": "ALTER TABLE api_usage_logs ADD COLUMN output_tokens INTEGER",
            "total_tokens": "ALTER TABLE api_usage_logs ADD COLUMN total_tokens INTEGER",
            "usage_reported": "ALTER TABLE api_usage_logs ADD COLUMN usage_reported INTEGER DEFAULT 0",
        },
    }
    for table, table_additions in additions.items():
        existing = columns(table)
        for column, statement in table_additions.items():
            if column not in existing:
                conn.execute(statement)

    # These objects are additive and can be created on an existing production
    # database after ALTER TABLE has completed.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS api_key_models (
            api_key_id INTEGER NOT NULL,
            model_id TEXT NOT NULL,
            PRIMARY KEY (api_key_id, model_id),
            FOREIGN KEY(api_key_id) REFERENCES api_keys(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_prefix_status ON api_keys(key_prefix, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_api_usage_key_started ON api_usage_logs(api_key_id, started_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_api_usage_user_started ON api_usage_logs(user_id, started_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_api_usage_route_created ON api_usage_logs(route_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_api_usage_user_created ON api_usage_logs(user_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_api_usage_request_id ON api_usage_logs(request_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_api_usage_effective_at ON api_usage_logs(COALESCE(started_at, created_at))")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_api_usage_user_effective_at ON api_usage_logs(user_id, COALESCE(started_at, created_at))")
