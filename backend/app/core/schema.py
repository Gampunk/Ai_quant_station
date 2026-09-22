"""
Database schema management, run once at startup.

The models are the source of truth for a new database. Alembic migrations carry
existing databases forward. The migrations in alembic/versions are changes layered
on top of tables the models created, not a full history, so they cannot build a
database from nothing.

    empty database           create every table from the models, record it at head
    recorded version         apply any newer migrations
    tables, no version       made by older code: adopt at head only if it already
                             matches the models, otherwise refuse and say what is missing

Whatever path was taken, the database is then compared with the models, and a
missing table or column stops startup. Tables the models no longer define are
left alone.

Earlier versions ran `alembic upgrade head` from the wrong folder, so it never
ran, and the failure was printed and ignored on every start.
"""
import logging
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import NullPool

from .database import Base
from .. import models  # noqa: F401  registers every table on Base.metadata

log = logging.getLogger("schema")
# Plugin setup chatter on every start; the useful "Running upgrade" lines stay visible.
logging.getLogger("alembic.runtime.plugins").setLevel(logging.WARNING)
BACKEND_DIR = Path(__file__).resolve().parents[2]


class SchemaError(RuntimeError):
    """The database does not match what the code needs."""


def _alembic_config(url: str) -> Config:
    # No ini file: loading it would reconfigure logging and silence the app's loggers.
    cfg = Config()
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.attributes["database_url"] = url
    return cfg


def _engine(url: str):
    args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, poolclass=NullPool, connect_args=args)


def schema_problems(engine) -> list[str]:
    """Tables and columns the models need that the database lacks."""
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": False})
        diffs = compare_metadata(ctx, Base.metadata)
    problems = []
    for diff in diffs:
        if not isinstance(diff, tuple):
            continue  # modifications to existing columns, which are not fatal
        if diff[0] == "add_table":
            problems.append(f"table {diff[1].name} is missing")
        elif diff[0] == "add_column":
            problems.append(f"column {diff[2]}.{diff[3].name} is missing")
    return sorted(problems)


def _current_revision(engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def _legacy_fixes(engine) -> None:
    """One-off conversions for databases made by older code, before any version was recorded."""
    with engine.begin() as conn:
        if engine.dialect.name == "sqlite":
            cols = {row[1]: row for row in conn.execute(text("PRAGMA table_info(users)"))}
            is_active = cols.get("is_active")
            if is_active and str(is_active[2]).upper().startswith(("VARCHAR", "TEXT")):
                log.info("Converting users.is_active from text to boolean")
                conn.execute(text("UPDATE users SET is_active = CASE WHEN is_active IN ('true','1') THEN 1 ELSE 0 END"))
        elif engine.dialect.name == "postgresql":
            rows = conn.execute(text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND data_type = 'timestamp without time zone'"
            )).fetchall()
            for table, column in rows:
                log.info("Converting %s.%s to timestamp with time zone", table, column)
                conn.execute(text(f'ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE TIMESTAMP WITH TIME ZONE'))


def ensure_schema(url: str) -> str:
    """Bring the database at `url`, a synchronous SQLAlchemy URL, up to date.

    Returns what was done: created, upgraded, up to date, or adopted.
    Raises SchemaError if the database cannot be made to match the models.
    """
    engine = _engine(url)
    cfg = _alembic_config(url)
    head = ScriptDirectory.from_config(cfg).get_current_head()
    try:
        existing = set(inspect(engine).get_table_names())
        revision = _current_revision(engine) if "alembic_version" in existing else None

        if revision:
            if revision == head:
                action = "up to date"
            else:
                log.info("Upgrading database from %s to %s", revision, head)
                command.upgrade(cfg, "head")
                action = "upgraded"
        elif not existing & set(Base.metadata.tables):
            Base.metadata.create_all(engine)
            command.stamp(cfg, "head")
            action = "created"
        else:
            _legacy_fixes(engine)
            problems = schema_problems(engine)
            if problems:
                raise SchemaError(
                    "This database was made by older code, has no recorded migration version, "
                    "and does not match the models:\n  - " + "\n  - ".join(problems) + "\n"
                    "Back it up. Then find the last migration it already has, record it with "
                    "`alembic stamp <revision>` from backend/, and run `alembic upgrade head`."
                )
            command.stamp(cfg, "head")
            action = "adopted"

        problems = schema_problems(engine)
        if problems:
            raise SchemaError(
                "The database is missing what the code needs:\n  - " + "\n  - ".join(problems) + "\n"
                "If you changed a model, add a migration with "
                "`alembic revision --autogenerate -m \"...\"` from backend/."
            )
        log.info("Database schema %s, at revision %s", action, head)
        return action
    finally:
        engine.dispose()
