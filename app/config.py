import os
import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


load_dotenv()

LEGACY_API_HOSTNAME = "api.luomo.moe"


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def public_url(raw: str, name: str, *, optional: bool = False) -> str:
    if not raw and optional:
        return ""
    parsed = urlsplit(raw)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or any(c.isspace() for c in raw)):
        raise ValueError(f"{name} must be an absolute http(s) URL without credentials, query or fragment.")
    try:
        parsed.port
    except ValueError:
        raise ValueError(f"{name} has an invalid port.") from None
    return raw.rstrip("/")


def footer_links() -> tuple:
    data = json.loads(env("FOOTER_LINKS", "[]"))
    if not isinstance(data, list) or len(data) > 12:
        raise ValueError("FOOTER_LINKS must be a JSON array of at most 12 links.")
    result = []
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("label"), str) or not 1 <= len(item["label"]) <= 80:
            raise ValueError("Each footer link needs a label and URL.")
        result.append({"label": item["label"], "url": public_url(item.get("url", ""), "FOOTER_LINKS")})
    return tuple(result)


def upstream_base_url() -> str:
    return public_url(env("UPSTREAM_BASE_URL", env("CODEXPROXY_BASE_URL", "http://localhost:8317")), "UPSTREAM_BASE_URL")


def upstream_api_key() -> str:
    # Presence of the canonical variable takes precedence, including an empty value.
    return env("UPSTREAM_API_KEY", env("CODEXPROXY_API_KEY", ""))


@dataclass(frozen=True)
class Settings:
    app_name: str = env("APP_NAME", "march7th api")
    app_env: str = env("APP_ENV", "production")
    app_host: str = env("APP_HOST", "0.0.0.0")
    app_port: int = int(env("APP_PORT", "8790") or "8790")
    app_base_url: str = public_url(env("APP_BASE_URL", "http://localhost:8790"), "APP_BASE_URL")
    database_path: Path = Path(env("DATABASE_PATH", "/app/data/luomoapi.db"))
    admin_username: str = env("ADMIN_USERNAME", "admin")
    admin_password_hash: str = env("ADMIN_PASSWORD_HASH", "")
    session_secret: str = env("SESSION_SECRET", "")
    astrbot_base_url: str = env("ASTRBOT_BASE_URL", "")
    luomocore_base_url: str = env("LUOMOCORE_BASE_URL", "")
    codexproxy_base_url: str = upstream_base_url()
    operator_name: str = env("OPERATOR_NAME", "Self-hosted operator")
    operator_url: str = public_url(env("OPERATOR_URL", ""), "OPERATOR_URL", optional=True)
    public_description: str = env("PUBLIC_DESCRIPTION", "自托管模型换乘枢纽：统一入口、密钥权限与透明积分账本。")
    footer_links: tuple = footer_links()
    api_catalog_enabled: bool = env("API_CATALOG_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
    admin_email: str = env("ADMIN_EMAIL", "admin@example.com")
    smtp_host: str = env("SMTP_HOST", "")
    smtp_port: int = int(env("SMTP_PORT", "465") or "465")
    smtp_username: str = env("SMTP_USERNAME", "")
    smtp_password: str = env("SMTP_PASSWORD", "")
    smtp_from: str = env("SMTP_FROM", env("SMTP_USERNAME", ""))
    smtp_use_tls: bool = env("SMTP_USE_TLS", "true").lower() in {"1", "true", "yes", "on"}
    email_verification_ttl_hours: int = int(env("EMAIL_VERIFICATION_TTL_HOURS", "24") or "24")

    @property
    def app_authority(self) -> str:
        return urlsplit(self.app_base_url).netloc

    @property
    def https_api_hosts(self) -> set[str]:
        base_url = urlsplit(self.app_base_url)
        hosts = {LEGACY_API_HOSTNAME}
        if base_url.scheme == "https" and base_url.hostname:
            hosts.add(base_url.hostname)
        return hosts


settings = Settings()


def ensure_dirs() -> None:
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)


def integration_secret(key: str) -> str:
    return upstream_api_key() if key in {'UPSTREAM_API_KEY', 'CODEXPROXY_API_KEY'} else env(key)


def secret_status(key: str) -> str:
    return "configured" if integration_secret(key) else "missing"
