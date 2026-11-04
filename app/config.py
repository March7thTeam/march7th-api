import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


load_dotenv()

LEGACY_API_HOSTNAME = "api.luomo.moe"


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


@dataclass(frozen=True)
class Settings:
    app_name: str = env("APP_NAME", "march7th api")
    app_env: str = env("APP_ENV", "production")
    app_host: str = env("APP_HOST", "0.0.0.0")
    app_port: int = int(env("APP_PORT", "8790") or "8790")
    app_base_url: str = env("APP_BASE_URL", "https://api.march7th.cn").rstrip("/")
    database_path: Path = Path(env("DATABASE_PATH", "/app/data/luomoapi.db"))
    admin_username: str = env("ADMIN_USERNAME", "luomo")
    admin_password_hash: str = env("ADMIN_PASSWORD_HASH", "")
    session_secret: str = env("SESSION_SECRET", "")
    astrbot_base_url: str = env("ASTRBOT_BASE_URL", "https://atri-api.luomo.moe")
    luomocore_base_url: str = env("LUOMOCORE_BASE_URL", "https://ops.luomo.moe")
    codexproxy_base_url: str = env("CODEXPROXY_BASE_URL", "http://codex-proxy:8317")
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


def secret_status(key: str) -> str:
    return "configured" if os.getenv(key) else "missing"
