"""Catalog form validation, separate from runtime DNS/address pinning."""
import json
import re
from urllib.parse import urlsplit
from fastapi import HTTPException
from app.models import ALLOWED_METHODS, AUTH_TYPES
from app.upstream_security import validate_https_url


def target_path(value):
    value = value.strip()
    try:
        parts = urlsplit(value)
        valid = value.startswith("/") and not parts.scheme and not parts.netloc and "\\" not in value and not any(ord(c) < 32 for c in value)
    except ValueError:
        valid = False
    if not valid or value.startswith("//"):
        raise HTTPException(400, "上游路径必须以 / 开头，且不能含 scheme、host、反斜线或控制字符。")
    return value


def route_values(conn, data, route_id=None):
    data = dict(data)
    path = data["public_path"].strip().strip("/")
    if len(path) > 1024 or ".." in path or any(segment == "." for segment in path.split("/")) or not re.fullmatch(r"[a-z0-9._~-]+(?:/[a-z0-9._~-]+)*", path):
        raise HTTPException(400, "公开路径仅允许小写字母、数字及 . _ ~ - 分段，不能含 ..。")
    if conn.execute("SELECT 1 FROM public_gateway_routes WHERE public_path=? AND (? IS NULL OR id!=?)", (path, route_id, route_id)).fetchone():
        raise HTTPException(400, "公开路径已存在。")
    data["public_path"] = path
    data["target_path"] = target_path(data["target_path"])
    data["target_method"] = data["target_method"].upper()
    if data["target_method"] not in ALLOWED_METHODS:
        raise HTTPException(400, "HTTP 方法无效。")
    scope = data["required_scope"].strip()
    if scope and not re.fullmatch(r"[a-z][a-z0-9._-]*:[a-z][a-z0-9._-]*", scope):
        raise HTTPException(400, "scope 必须形如 name:action。")
    data["required_scope"] = scope
    data["name"] = data["name"].strip()
    if not data["name"]:
        raise HTTPException(400, "路由名称不能为空。")
    if not conn.execute("SELECT 1 FROM apis WHERE id=?", (data["target_api_id"],)).fetchone():
        raise HTTPException(400, "目标 API 不存在。")
    endpoint_id = data.get("target_endpoint_id")
    if endpoint_id is not None and not conn.execute("SELECT 1 FROM endpoints WHERE id=? AND api_id=?", (endpoint_id, data["target_api_id"])).fetchone():
        raise HTTPException(400, "目标端点不属于此 API。")
    return data


def api_values(data):
    data = dict(data)
    try:
        parts = validate_https_url(data["base_url"].strip())
        if parts.query or parts.fragment or "\\" in data["base_url"] or any(c.isspace() for c in data["base_url"]):
            raise ValueError("上游地址不能带 query、fragment、空白或反斜线。")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if data["auth_type"] not in AUTH_TYPES:
        raise HTTPException(400, "认证类型无效。")
    for field in ("name", "slug", "base_url", "description", "tags"):
        data[field] = data[field].strip()
    if not data["name"] or not re.fullmatch(r"[a-z0-9._~-]+", data["slug"]):
        raise HTTPException(400, "名称或标识无效。")
    data["secret_ref"] = data["secret_ref"].strip() or None
    return data


def endpoint_values(data):
    data = dict(data)
    data["path"] = target_path(data["path"])
    data["method"] = data["method"].upper()
    if data["method"] not in ALLOWED_METHODS or not data["name"].strip():
        raise HTTPException(400, "端点名称或 HTTP 方法无效。")
    for field in ("default_headers", "default_query"):
        data[field] = data[field].strip() or "{}"
        try:
            if not isinstance(json.loads(data[field]), dict):
                raise ValueError()
        except ValueError:
            raise HTTPException(400, "默认 headers 和 query 必须是 JSON 对象。") from None
    return data
