import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SENSITIVE_WORDS = (
    "token",
    "password",
    "secret",
    "key",
    "cookie",
    "authorization",
    "credential",
    "api_key",
    "access_token",
    "refresh_token",
    "bearer",
)
ASSIGNMENT_RE = re.compile(
    r"(?i)\b(token|password|secret|key|cookie|authorization|credential|api_key|access_token|refresh_token|bearer)\b\s*[:=]\s*([^\s,;&]+)"
)


def is_sensitive_key(key: object) -> bool:
    return any(word in str(key).lower() for word in SENSITIVE_WORDS)


def redact_text(value: object) -> str:
    text = "" if value is None else str(value)
    text = ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=********", text)
    text = re.sub(r"(?i)(Bearer\s+)([A-Za-z0-9._~+/=-]+)", r"\1********", text)
    return text


def redact_obj(value):
    if isinstance(value, dict):
        return {key: "********" if is_sensitive_key(key) else redact_obj(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_obj(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_url(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return redact_text(url)
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        query.append((key, "********" if is_sensitive_key(key) else redact_text(value)))
    netloc = parts.netloc
    if parts.username or parts.password:
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        netloc = f"********@{host}"
    return urlunsplit((parts.scheme, netloc, parts.path, urlencode(query), parts.fragment))


def preview(value, limit: int = 4000) -> str:
    clean = redact_obj(value)
    if isinstance(clean, (dict, list)):
        text = json.dumps(clean, ensure_ascii=False, indent=2)
    else:
        text = redact_text(clean)
    return text[:limit]


def gateway_redact_text(value: object, limit: int = 1000) -> str:
    if value is None:
        return ""
    text = str(value)
    if any(word in text.lower() for word in SENSITIVE_WORDS):
        return "********"
    if len(text) > limit:
        return text[:limit] + "...[truncated]"
    return text


def redact_json_like(value: object, limit: int = 1000) -> str:
    try:
        if isinstance(value, (dict, list)):
            def scrub(obj: object) -> object:
                if isinstance(obj, dict):
                    out: dict[object, object] = {}
                    for key, child in obj.items():
                        if any(word in str(key).lower() for word in SENSITIVE_WORDS):
                            out[key] = "********"
                        else:
                            out[key] = scrub(child)
                    return out
                if isinstance(obj, list):
                    return [scrub(item) for item in obj[:20]]
                if isinstance(obj, str):
                    return gateway_redact_text(obj, limit=300)
                return obj

            text = json.dumps(scrub(value), ensure_ascii=False)
        else:
            text = str(value)
        return text if len(text) <= limit else text[:limit] + "...[truncated]"
    except Exception:
        return "[unserializable]"
