from pydantic_settings import BaseSettings
from typing import List
import os
import secrets

env_file_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), '.env')


class Settings(BaseSettings):
    # ── Environment ──────────────────────────────────────────────────────────
    # Set APP_ENV=production when deploying. Defaults to "development".
    APP_ENV: str = "development"

    @property
    def is_production(self) -> bool:
        return self.APP_ENV.lower() == "production"

    # ── JWT Settings ─────────────────────────────────────────────────────────
    SECRET_KEY: str = ""
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # ── Validate SECRET_KEY ──────────────────────────────────────────────────
    def validate_secret_key(self) -> None:
        """Raise ValueError unless SECRET_KEY is set and strong. Runs in every environment.

        An earlier version fell back to a fresh random key on every use when this was
        unset, so tokens were signed with one key and checked with another.
        """
        problem = secret_key_problem(self.SECRET_KEY)
        if problem:
            raise ValueError(
                f"SECRET_KEY {problem}. Set it in backend/.env, for example with: "
                'python -c "import secrets; print(secrets.token_hex(32))"'
            )

    # MT5 connector: the only route to the broker. The terminal to use is chosen
    # on the connector with its own MT5_TERMINAL_PATH, not here.
    MT5_CONNECTOR_URL: str = ""
    MT5_API_TOKEN: str = ""
    # Connector traffic is limited to local and private networks unless this is true.
    # See core/connector_guard.py.
    ALLOW_REMOTE_CONNECTOR: bool = False

    # HuggingFace
    HF_REPO_ID: str = ""
    HUGGINGFACE_API_KEY: str = ""

    # AI Providers
    NVIDIA_API_KEY: str = ""
    GROQ_API_KEY: str = ""
    OPEN_ROUTER_API_KEY: str = ""
    GEMINI_API_KEY: str = ""
    GITHUB_API_KEY: str = ""
    CEREBRAS_API_KEY: str = ""
    MISTRAL_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""
    TOKENLB_API_KEY: str = ""
    ZENMUX_API_KEY: str = ""

    # CORS
    CORS_ORIGINS: str = "http://localhost:5173,http://localhost:3000"

    @property
    def cors_origins_list(self) -> List[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",")]

    # Database
    DATABASE_URL: str = "sqlite+aiosqlite:///./finance_engine.db"
    DATABASE_URL_SYNC: str | None = None  # auto-derived from DATABASE_URL if not set

    @property
    def database_url_sync(self) -> str:
        if self.DATABASE_URL_SYNC:
            return self.DATABASE_URL_SYNC
        return str(settings.DATABASE_URL).replace("+aiosqlite", "").replace("+asyncpg", "")

    # MT5 broker UTC offset (brokers often return timestamps in local time)
    # Examples: UTC+2 = 2, UTC+3 = 3, UTC = 0. Set to 0 if your broker returns UTC.
    # Hours the broker's server clock runs ahead of UTC. Only a fallback: the backend
    # reads the real offset from the connector while prices are live (core/broker_clock.py).
    MT5_BROKER_UTC_OFFSET: float = 0

    # Yahoo Finance (for market data)
    YAHOO_FINANCE_ENABLED: bool = True
    
    # SMTP / Daily Report Email
    SMTP_SERVER: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    REPORT_EMAIL: str = ""
    REPORT_EMAIL_PASSWORD: str = ""
    REPORT_RECIPIENT_EMAIL: str = ""
    SENDGRID_API_KEY: str = ""

    # Extra fields from .env (legacy/compat)
    PASSWORD: str = ""
    Bytez: str = ""
    Completions: str = ""

    # Default admin credentials (MUST be set via .env in production!)
    DEFAULT_ADMIN_PASSWORD: str = ""

    class Config:
        env_file = env_file_path
        case_sensitive = True
        extra = "ignore"


# Values published in docs or .env.example, which must never be used as real keys.
_KNOWN_PLACEHOLDER_KEYS = {
    "change-this-to-a-long-random-string-in-production",
    "your-secret-key", "secret", "changeme",
}
MIN_SECRET_KEY_LENGTH = 32

# Passwords that were once hardcoded in this repository.
KNOWN_WEAK_PASSWORDS = {"admin@2026", "admin2026", "usdt@2026", "password", "admin"}
MIN_ADMIN_PASSWORD_LENGTH = 12


def secret_key_problem(key: str) -> str | None:
    """Describe what is wrong with a signing key, or return None if it is usable."""
    if not key:
        return "is not set"
    if key.strip().lower() in _KNOWN_PLACEHOLDER_KEYS:
        return "is still the placeholder from the example file"
    if len(key) < MIN_SECRET_KEY_LENGTH:
        return f"is only {len(key)} characters, it needs at least {MIN_SECRET_KEY_LENGTH}"
    return None


def password_problem(password: str) -> str | None:
    """Describe what is wrong with a password, or return None if it is usable."""
    if not password:
        return "is not set"
    if password.strip().lower() in KNOWN_WEAK_PASSWORDS:
        return "is a password that was published in this repository"
    if len(password) < MIN_ADMIN_PASSWORD_LENGTH:
        return f"is only {len(password)} characters, it needs at least {MIN_ADMIN_PASSWORD_LENGTH}"
    return None


# The same rules apply to every account; the admin name is kept for existing callers.
admin_password_problem = password_problem


settings = Settings()