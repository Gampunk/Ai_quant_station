"""
JWT token revocation blacklist backed by the database.

Persisted table: backend/app/models/ai_memory.py → RevokedToken

Usage:
    from app.core.blacklist import blacklist_token, is_token_blacklisted, cleanup_expired_tokens
    await blacklist_token(token_string, expires_at=datetime(...))
    if await is_token_blacklisted(token_string): ...
    cleaned = await cleanup_expired_tokens()
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import create_engine, text
from ..core.config import settings

logger = logging.getLogger(__name__)

_SYNC_URL = str(settings.DATABASE_URL).replace("+aiosqlite", "").replace("+asyncpg", "")

_sync_engine = create_engine(
    _SYNC_URL,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False} if _SYNC_URL.startswith("sqlite") else {},
)


async def blacklist_token(token: str, expires_at: Optional[datetime] = None) -> None:
    """Persist a revoked token to the database."""
    if expires_at is None:
        expires_at = datetime.now(timezone.utc) + timedelta(
            days=settings.REFRESH_TOKEN_EXPIRE_DAYS or 7
        )

    def _insert(jti: str, exp: datetime):
        with _sync_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO revoked_tokens (token_jti, expires_at, created_at)"
                    " VALUES (:jti, :exp, :created)"
                    " ON CONFLICT (token_jti) DO NOTHING"
                ),
                {
                    "jti": jti,
                    "exp": exp.isoformat(),
                    "created": datetime.now(timezone.utc).isoformat(),
                },
            )

    await asyncio.get_running_loop().run_in_executor(None, _insert, token, expires_at)


async def is_token_blacklisted(token: str) -> bool:
    """Check if a token is revoked."""

    def _lookup(jti: str) -> bool:
        with _sync_engine.connect() as conn:
            row = conn.execute(
                text("SELECT expires_at FROM revoked_tokens WHERE token_jti = :jti"),
                {"jti": jti},
            ).fetchone()
            if row is None:
                return False
            raw = row[0]
            if isinstance(raw, str):
                expires_at = datetime.fromisoformat(raw.replace(' ', 'T'))
                # Handle cases where isoformat might be naive
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
            else:
                expires_at = raw
                # Ensure expires_at is timezone-aware if it's a datetime object
                if hasattr(expires_at, 'tzinfo') and expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                
            return expires_at > datetime.now(timezone.utc)

    return await asyncio.get_running_loop().run_in_executor(None, _lookup, token)


async def cleanup_expired_tokens() -> int:
    """Delete expired revoked-token rows. Returns number removed."""

    def _cleanup() -> int:
        with _sync_engine.begin() as conn:
            result = conn.execute(
                text("DELETE FROM revoked_tokens WHERE expires_at < :now"),
                {"now": datetime.now(timezone.utc).isoformat()},
            )
            return result.rowcount  # type: ignore[attr-defined]

    return await asyncio.get_running_loop().run_in_executor(None, _cleanup)


def init_blacklist_table() -> None:
    """Create revoked_tokens table at startup (Alembic is the source of truth)."""
    from ..models.ai_memory import Base  # noqa

    try:
        Base.metadata.create_all(
            bind=_sync_engine,
            tables=[Base.metadata.tables["revoked_tokens"]],
        )
    except Exception:
        # Without this table logout cannot revoke anything.
        logger.exception("Could not create the revoked_tokens table")
