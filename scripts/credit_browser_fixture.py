"""Local-only, idempotent browser fixture; never uses upstream/mail credentials."""
import os
import sys
from pathlib import Path
from contextlib import closing
from datetime import datetime, timedelta, timezone

project = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project))
os.environ["DATABASE_PATH"] = str(project / "output" / "browser-fixture.sqlite")
os.environ["SESSION_SECRET"] = "local-browser-fixture-secret-only-over-32-characters"
os.environ["APP_BASE_URL"] = "http://127.0.0.1:8793"
os.environ["CODEXPROXY_API_KEY"] = ""
os.environ["CREDIT_PAYMENT_PROVIDER"] = ""
for field in ("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM"):
    os.environ[field] = ""

from app.db import connect, init_db
from app.auth import hash_password
from app.credit_ledger import add_credit, reserve_credit, mark_credit_dispatched, settle_credit
from app.credit_pricing import prepare_billable_request, calculate_charge, priced_models
from app.keyring import generate_api_key, hash_api_key
from app.utils.timefmt import now_iso
from app.self_service_keys import SELF_SCOPES


def seed_fixture():
    init_db()
    now = now_iso()
    with closing(connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        for name, role, status in (("qa_admin", "admin", "active"), ("qa_dev", "developer", "active"), ("qa_registered", "developer", "pending")):
            conn.execute("INSERT OR IGNORE INTO users(username,email,password_hash,role,status,email_verified,created_at,updated_at) VALUES (?,?,?,?,?,0,?,?)",
                         (name, name+"@example.test", hash_password("FixtureOnly-2026"), role, status, now, now))
        user_id = conn.execute("SELECT id FROM users WHERE username='qa_dev'").fetchone()[0]
        key_ids = []
        for index in range(3):
            name = f"QA key {index+1}"
            existing = conn.execute("SELECT id FROM api_keys WHERE user_id=? AND name=?", (user_id, name)).fetchone()
            if existing:
                key_ids.append(existing[0])
                continue
            raw, prefix = generate_api_key()
            key_id = conn.execute("""INSERT INTO api_keys(user_id,name,key_prefix,key_hash,status,scopes,model_policy,rate_limit_per_minute,daily_quota,monthly_quota,created_at)
                VALUES (?,?,?,?,'active',?,'allowlist',60,0,0,?)""", (user_id,name,prefix,hash_api_key(raw),SELF_SCOPES,now)).lastrowid
            conn.executemany("INSERT INTO api_key_models(api_key_id,model_id) VALUES (?,?)", [(key_id, model) for model in priced_models()])
            key_ids.append(key_id)
        add_credit(conn, user_id, 5_000_000_000, reference="browser-fixture-funding", actor_id=None, kind="fixture", note="Local fixture credits")
        models = ("gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra")
        for index in range(60):
            request_id = f"browser-fixture-{index}"
            if conn.execute("SELECT 1 FROM api_usage_logs WHERE request_id=?", (request_id,)).fetchone():
                continue
            stamp = (datetime.now(timezone.utc).replace(hour=12, minute=index % 60, second=0, microsecond=0) - timedelta(days=index % 14)).isoformat()
            model, inputs, outputs = models[index % 3], 100+index*7, 10+index
            success = int(index % 5 != 0)
            usage_id = conn.execute("""INSERT INTO api_usage_logs(created_at,started_at,completed_at,user_id,api_key_id,method,public_path,status_code,success,request_id,protocol,model,input_tokens,output_tokens,total_tokens,usage_reported,outcome,response_time_ms)
                VALUES (?,?,?,?,?,'POST','v1/chat/completions',?,?,?,?,?,?,?,?,1,?,?)""",
                (stamp,stamp,stamp,user_id,key_ids[index % 3],200 if success else 502,success,request_id,"chat.completions",model,inputs,outputs,inputs+outputs,"succeeded" if success else "failed",200+index*10)).lastrowid
            _, quote = prepare_billable_request({"model": model, "messages": [{"role":"user","content":"fixture"}], "max_completion_tokens":128}, protocol="chat.completions")
            reserve_credit(conn, usage_id, user_id, quote.amount_units, quote.snapshot)
            mark_credit_dispatched(conn, usage_id)
            charge = calculate_charge(quote.snapshot, {"input_tokens":inputs,"output_tokens":outputs,"cached_tokens":0,"cache_write_tokens":0,"model":model,"cached_tokens_reported":True,"cache_write_reported":True})
            settle_credit(conn, usage_id, charge, reason="Local browser fixture settlement")
        api_id = conn.execute("SELECT id FROM apis WHERE slug='qa-fixture'").fetchone()
        if api_id is None:
            api_id = conn.execute("INSERT INTO apis(name,slug,base_url,enabled,created_at,updated_at) VALUES ('QA fixture','qa-fixture','https://example.test',0,?,?)", (now,now)).lastrowid
        else:
            api_id = api_id[0]
        for suffix in ("health", "status"):
            conn.execute("""INSERT OR IGNORE INTO public_gateway_routes(name,public_path,target_api_id,target_method,target_path,required_scope,enabled,public_docs,allow_anonymous,created_at,updated_at)
                VALUES (?,?,?,'GET',?,?,0,1,0,?,?)""", ("QA "+suffix,"qa/"+suffix,api_id,"/"+suffix,"qa:"+suffix,now,now))


seed_fixture()
from app.main import app


# This localhost-only wrapper permits browser checks without weakening deployed cookies.
@app.middleware("http")
async def fixture_cookies(request, call_next):
    response = await call_next(request)
    if "set-cookie" in response.headers:
        response.headers["set-cookie"] = response.headers["set-cookie"].replace("; Secure", "")
    return response


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8793)
