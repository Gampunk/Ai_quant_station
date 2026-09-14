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

    # ── Validate SECRET_KEY is set ───────────────────────────────────────────
    def validate_secret_key(self) -> None:
        """Raise ValueError if SECRET_KEY is not set in production."""
        if self.is_production and not self.SECRET_KEY:
            raise ValueError(
                "SECRET_KEY must be explicitly set in .env when APP_ENV=production. "
                "Randomly generated keys are lost on restart and invalidate all active sessions."
            )

    # MT5 Settings
    MT5_SERVER_PORT: int = 5001
    MT5_API_TOKEN: str = ""
    MT5_TERMINAL_PATH: str | None = None
    
    # MT5 Connector (External Windows Server)
    MT5_CONNECTOR_URL: str = ""
    MT5_USE_EXTERNAL_CONNECTOR: bool = False
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
    MT5_BROKER_UTC_OFFSET: int = 0

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

    @property
    def effective_secret_key(self) -> str:
        """Return SECRET_KEY — validated by validate_secret_key() at startup."""
        if not self.SECRET_KEY:
            # Development fallback — will be caught by validate_secret_key() in prod
            import secrets as _secrets
            return _secrets.token_hex(32)
        return self.SECRET_KEY

    class Config:
        env_file = env_file_path
        case_sensitive = True
        extra = "ignore"


settings = Settings()