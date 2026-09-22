from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from .config import settings

_is_sqlite = str(settings.DATABASE_URL).startswith("sqlite")
_engine_kwargs = dict(echo=False, future=True)
if not _is_sqlite:
    _engine_kwargs.update(pool_size=20, max_overflow=10, pool_pre_ping=True, pool_recycle=3600)
engine = create_async_engine(settings.DATABASE_URL, **_engine_kwargs)

# PostgreSQL enforces foreign keys natively. For SQLite, we handle it below.
if _is_sqlite:
    from sqlalchemy import event

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_sqlite_fks(dbapi_connection, connection_record):
        if hasattr(dbapi_connection, "execute"):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False
)

Base = declarative_base()


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db() -> str:
    """Bring the database schema up to date. See core/schema.py. Returns what was done."""
    import asyncio
    from .schema import ensure_schema
    return await asyncio.get_running_loop().run_in_executor(None, ensure_schema, settings.database_url_sync)
