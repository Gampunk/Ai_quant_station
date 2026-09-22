"""
Database schema management at startup: create, upgrade, adopt, or refuse.

Negative controls:
- In ensure_schema (app/core/schema.py), delete the final schema_problems check.
  test_recorded_but_drifted_database_is_refused must fail.
- Replace `command.upgrade(cfg, "head")` with `pass`.
  test_database_one_migration_behind_is_upgraded must fail.
"""
import logging
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from app.core.schema import SchemaError, _alembic_config, ensure_schema

BACKEND_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "schema.db"
    return path, f"sqlite:///{path}"


def _head():
    return ScriptDirectory.from_config(_alembic_config("sqlite://")).get_current_head()


def _revision(path):
    return sqlite3.connect(path).execute("select version_num from alembic_version").fetchone()[0]


def _columns(path, table):
    return {row[1] for row in sqlite3.connect(path).execute(f"pragma table_info({table})")}


def _sql(path, *statements):
    conn = sqlite3.connect(path)
    for statement in statements:
        conn.execute(statement)
    conn.commit()


def test_empty_database_is_created_and_recorded_at_head(db):
    path, url = db
    assert ensure_schema(url) == "created"
    assert _revision(path) == _head()
    assert "users" in {r[0] for r in sqlite3.connect(path).execute("select name from sqlite_master")}


def test_second_start_does_nothing(db):
    _, url = db
    ensure_schema(url)
    assert ensure_schema(url) == "up to date"


def test_database_one_migration_behind_is_upgraded(db):
    path, url = db
    ensure_schema(url)
    command.downgrade(_alembic_config(url), "-1")
    assert "market_regime" not in _columns(path, "autopilot_trades")

    assert ensure_schema(url) == "upgraded"
    assert "market_regime" in _columns(path, "autopilot_trades")
    assert _revision(path) == _head()


def test_older_database_matching_the_models_is_adopted(db):
    path, url = db
    ensure_schema(url)
    _sql(path, "drop table alembic_version")
    assert ensure_schema(url) == "adopted"
    assert _revision(path) == _head()


def test_older_database_missing_a_column_is_refused(db):
    path, url = db
    ensure_schema(url)
    _sql(path, "drop table alembic_version",
         "drop index ix_autopilot_trades_market_regime",
         "alter table autopilot_trades drop column market_regime")
    with pytest.raises(SchemaError, match="autopilot_trades.market_regime is missing"):
        ensure_schema(url)
    assert "alembic_version" not in {r[0] for r in sqlite3.connect(path).execute("select name from sqlite_master")}


def test_recorded_but_drifted_database_is_refused(db):
    path, url = db
    ensure_schema(url)
    _sql(path, "drop table position_audits")
    with pytest.raises(SchemaError, match="table position_audits is missing"):
        ensure_schema(url)


def test_tables_the_models_no_longer_define_are_ignored(db):
    """Older databases still hold tables for models deleted in step 7."""
    path, url = db
    ensure_schema(url)
    _sql(path, "create table memory_nodes (id integer primary key)")
    assert ensure_schema(url) == "up to date"


def test_running_migrations_does_not_silence_app_logging(db):
    _, url = db
    ensure_schema(url)
    for name in ("startup", "autopilot", "app.core.mt5_sync"):
        assert not logging.getLogger(name).disabled, f"logger {name} was disabled"


def test_standard_alembic_command_still_works(db):
    """Developers run `alembic` from backend/ with alembic.ini."""
    path, url = db
    ensure_schema(url)
    env = dict(os.environ, DATABASE_URL=f"sqlite+aiosqlite:///{path}")
    out = subprocess.run([sys.executable, "-m", "alembic", "current"], cwd=BACKEND_DIR,
                         env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert _head() in out.stdout + out.stderr
