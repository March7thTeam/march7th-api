from contextlib import closing
from app.config import settings, secret_status
from app.db import connect, init_db
from app.auth import hash_password
from app.utils.timefmt import now_iso


DEFAULT_APIS = [
    {
        "name": "LuomoCore",
        "slug": "luomocore",
        "base_url": settings.luomocore_base_url,
        "auth_type": "bearer",
        "secret_ref": "LUOMOCORE_API_TOKEN",
        "description": "LuomoCore operations and memory center",
        "tags": "ops,brain,core",
        "endpoints": [("Health", "GET", "/health")],
    },
    {
        "name": "CodexProxy",
        "slug": "codexproxy",
        "base_url": settings.codexproxy_base_url,
        "auth_type": "bearer",
        "secret_ref": "CODEXPROXY_API_KEY",
        "description": "OpenAI-compatible gateway for Codex and other CLI model subscriptions",
        "tags": "ai,openai,codex,proxy",
        "endpoints": [
            ("Models", "GET", "/v1/models"),
            ("Chat completions", "POST", "/v1/chat/completions"),
            ("Responses", "POST", "/v1/responses"),
        ],
    },
]
DEFAULT_SECRETS = [
    ("LuomoCore API Token", "LUOMOCORE_API_TOKEN"),
    ("GitHub Token", "GITHUB_TOKEN"),
    ("Cloudflare API Token", "CLOUDFLARE_API_TOKEN"),
    ("CodexProxy API Key", "CODEXPROXY_API_KEY"),
]
DEFAULT_PUBLIC_ROUTES = [
    ("LuomoCore health", "luomocore/health", "luomocore", "GET", "/health", "luomocore:health"),
]


def seed() -> None:
    init_db()
    now = now_iso()
    with closing(connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        legacy_api = conn.execute("SELECT id FROM apis WHERE slug='cliproxy'").fetchone()
        current_api = conn.execute("SELECT id FROM apis WHERE slug='codexproxy'").fetchone()
        if legacy_api and not current_api:
            conn.execute(
                "UPDATE apis SET name='CodexProxy', slug='codexproxy', updated_at=? WHERE id=?",
                (now, legacy_api["id"]),
            )
        legacy_secret = conn.execute("SELECT id FROM secrets WHERE key='CLIPROXY_API_KEY'").fetchone()
        current_secret = conn.execute("SELECT id FROM secrets WHERE key='CODEXPROXY_API_KEY'").fetchone()
        if legacy_secret and not current_secret:
            conn.execute(
                "UPDATE secrets SET name='CodexProxy API Key', key='CODEXPROXY_API_KEY', updated_at=? WHERE id=?",
                (now, legacy_secret["id"]),
            )
        for item in DEFAULT_APIS:
            seed_name = "api:" + item["slug"]
            if conn.execute("SELECT 1 FROM catalog_seed_state WHERE seed_name=?", (seed_name,)).fetchone():
                continue
            existing = conn.execute("SELECT id FROM apis WHERE slug=?", (item["slug"],)).fetchone()
            if not existing:
                api_id = conn.execute("""INSERT INTO apis(name,slug,base_url,auth_type,secret_ref,description,tags,enabled,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,1,?,?)""", (item["name"],item["slug"],item["base_url"],item["auth_type"],item["secret_ref"],item["description"],item["tags"],now,now)).lastrowid
                conn.executemany("""INSERT INTO endpoints(api_id,name,method,path,description,default_headers,default_query,default_body,enabled,created_at,updated_at)
                    VALUES (?,?,?,?,'','{}','{}','',1,?,?)""", [(api_id,name,method,path,now,now) for name,method,path in item["endpoints"]])
            # Existing installations already own their catalog. Seed once and
            # never overwrite edits or recreate deliberately removed endpoints.
            conn.execute("INSERT INTO catalog_seed_state(seed_name,seeded_at) VALUES (?,?)", (seed_name,now))
        for name, key in DEFAULT_SECRETS:
            status = secret_status(key)
            existing = conn.execute("SELECT id FROM secrets WHERE key = ?", (key,)).fetchone()
            if existing:
                conn.execute("UPDATE secrets SET name = ?, status = ?, updated_at = ? WHERE key = ?", (name, status, now, key))
            else:
                conn.execute(
                    "INSERT INTO secrets(name, key, value_encrypted, status, created_at, updated_at) VALUES (?, ?, NULL, ?, ?, ?)",
                    (name, key, status, now, now),
                )
        admin = conn.execute("SELECT id FROM users WHERE username = ?", ("luomo",)).fetchone()
        if not admin and settings.admin_password_hash:
            conn.execute(
                """
                INSERT INTO users(username, email, password_hash, role, status, usage_reason, email_verified, email_verified_at, created_at, updated_at)
                VALUES (?, ?, ?, 'admin', 'active', 'Owner account', 1, ?, ?, ?)
                """,
                ("luomo", "luomo@luomo.moe", settings.admin_password_hash, now, now, now),
            )
        elif admin:
            conn.execute(
                "UPDATE users SET role='admin', status='active', email_verified=1, email_verified_at=COALESCE(email_verified_at, ?), updated_at=? WHERE username='luomo'",
                (now, now),
            )
        api_ids = {row["slug"]: row["id"] for row in conn.execute("SELECT id, slug FROM apis").fetchall()}
        for name, public_path, slug, method, target_path, scope in DEFAULT_PUBLIC_ROUTES:
            api_id = api_ids.get(slug)
            if not api_id:
                continue
            seed_name = "route:" + public_path
            if conn.execute("SELECT 1 FROM catalog_seed_state WHERE seed_name=?", (seed_name,)).fetchone():
                continue
            conn.execute("""INSERT OR IGNORE INTO public_gateway_routes(name,public_path,target_api_id,target_endpoint_id,target_method,
                target_path,required_scope,enabled,public_docs,allow_anonymous,created_at,updated_at)
                VALUES (?,?,?,NULL,?,?,?,0,1,0,?,?)""", (name,public_path,api_id,method,target_path,scope,now,now))
            conn.execute("INSERT INTO catalog_seed_state(seed_name,seeded_at) VALUES (?,?)", (seed_name,now))
        conn.commit()


if __name__ == "__main__":
    seed()
    print("default APIs seeded")
