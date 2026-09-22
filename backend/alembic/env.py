"""
Alembic environment configuration.
Bridges the project's async SQLAlchemy setup to Alembic's migration engine.
"""
import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# --- Project setup ---
# Add the backend/ directory to sys.path so that `app` can be imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load alembic.ini config
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Import the project's SQLAlchemy Base metadata
from app.core.database import Base  # noqa: E402
from app.models import user, ai_memory, historical_lab, market_data  # noqa: F401, E402

target_metadata = Base.metadata


def get_database_url() -> str:
    """The database URL: from the caller when core/schema.py runs migrations, else settings."""
    if config.attributes.get("database_url"):
        return config.attributes["database_url"]
    from app.core.config import settings
    url = str(settings.DATABASE_URL)
    # Alembic needs a synchronous driver — strip the +async suffix
    return url.replace("+aiosqlite", "").replace("+asyncpg", "")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (used by stamping + script generation)."""
    url = get_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    url = get_database_url()
    is_sqlite = url.startswith("sqlite")

    # Build a sync engine for Alembic from the async URL
    # No ini section when core/schema.py calls in without a config file.
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = url

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args={"check_same_thread": False} if is_sqlite else {},
    )

    if is_sqlite:
        @event.listens_for(connectable, "connect")
        def _enable_foreign_keys(dbapi_conn, _connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


from sqlalchemy import event  # noqa: E402

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
