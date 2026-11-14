"""Owner-controlled limits on self-service AI keys.

Owners configure a separate credit cap, expiry, request rate and model subset.
Effective authorization always intersects administrator policy and owner limits.
Explicit unmetered administrator keys remain administrator-managed.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone

from fastapi import HTTPException

from app.credit_ledger import CreditError, credits_to_units
from app.credit_pricing import priced_models
from app.image_generation import image_models
from app.utils.timefmt import now_iso

SELF_RPM = 60


def _scope_set(raw) -> set[str]:
    return {part.strip() for part in str(raw or "").replace(",", " ").split() if part.strip()}


def owner_editable(key) -> bool:
    return key["status"] == "active" and "admin:unmetered" not in _scope_set(key["scopes"])


def owner_value(key, field, default=None):
    try:
        return key[field]
    except (KeyError, IndexError, TypeError):
        return default


def effective_model_ids(conn, key):
    from app.ai_usage import get_key_models
    import json
    admin = None if str(key['model_policy'] or 'all') == 'all' else set(get_key_models(conn, key['id']))
    raw = owner_value(key, 'owner_models_json')
    if raw is None:
        return admin
    try:
        chosen = json.loads(raw)
        if not isinstance(chosen, list) or any(not isinstance(m, str) for m in chosen):
            return set()
    except (ValueError, TypeError):
        return set()
    return set(chosen) if admin is None else admin & set(chosen)


def selectable_models() -> list[str]:
    return sorted(set(priced_models()) | set(image_models()))


def parse_owner_limits(*, credit_limit: str, expires_on: str, rpm: str, models: list[str]) -> dict:
    limit_units = None
    if credit_limit.strip():
        try:
            limit_units = credits_to_units(credit_limit)
        except CreditError as exc:
            raise HTTPException(400, f"积分上限：{exc.message}") from None
        if limit_units <= 0:
            raise HTTPException(400, "积分上限需大于 0；留空表示不设上限。")
    expiry = None
    if expires_on.strip():
        try:
            day = date.fromisoformat(expires_on.strip())
        except ValueError:
            raise HTTPException(400, "到期日期格式应为 YYYY-MM-DD。") from None
        moment = datetime.combine(day, time(23, 59, 59), tzinfo=timezone.utc)
        if moment <= datetime.now(timezone.utc):
            raise HTTPException(400, "到期日期必须晚于今天。")
        expiry = moment.isoformat()
    try:
        rate = int(rpm)
    except ValueError:
        raise HTTPException(400, "每分钟请求数必须是整数。") from None
    if not 1 <= rate <= SELF_RPM:
        raise HTTPException(400, f"每分钟请求数需在 1–{SELF_RPM} 之间。")
    chosen = sorted(set(models))
    allowed = set(selectable_models())
    if not chosen:
        raise HTTPException(400, "至少保留一个模型；不想再用这把密钥请直接撤销。")
    if set(chosen) - allowed:
        raise HTTPException(400, "只能从已计价模型中选择。")
    return {"credit_limit_units": limit_units, "expires_at": expiry, "rate_limit_per_minute": rate, "models": chosen}


def save_owner_limits(conn, user_id: int, key_id: int, limits: dict) -> None:
    conn.execute("BEGIN IMMEDIATE")
    key = conn.execute("SELECT * FROM api_keys WHERE id=? AND user_id=?", (key_id, user_id)).fetchone()
    if not key:
        raise HTTPException(404, "密钥不存在。")
    if not owner_editable(key):
        raise HTTPException(403, "这把密钥由管理员配置，请联系管理员修改。")
    from app.ai_usage import get_key_models
    import json
    admin_models = None if key['model_policy'] == 'all' else set(get_key_models(conn, key_id))
    if admin_models is not None and set(limits['models']) - admin_models:
        raise HTTPException(400, "只能选择管理员已授权的模型。")
    # Only the owner overlay changes. Empty owner settings never erase an admin constraint.
    conn.execute("""UPDATE api_keys SET owner_credit_limit_units=?, owner_expires_at=?, owner_rpm=?, owner_models_json=? WHERE id=?""",
                 (limits["credit_limit_units"], limits["expires_at"], limits["rate_limit_per_minute"], json.dumps(limits['models']), key_id))
    conn.execute("""INSERT INTO api_key_requests(user_id,name,requested_scopes,requested_reason,status,admin_note,created_at,reviewed_at)
        VALUES (?,?,?,'密钥所有者调整限额','approved','所有者自助修改：积分上限、到期、频率与模型范围',?,?)""",
                 (user_id, key["name"], key["scopes"], now_iso(), now_iso()))
    conn.commit()
