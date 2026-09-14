import os, sys, logging, tempfile
from pathlib import Path
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base

# ── Must set env BEFORE any backend imports ───────────────────────────────
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from dotenv import load_dotenv
load_dotenv(BACKEND_DIR / ".env")
logging.disable(logging.CRITICAL)
os.environ["APP_ENV"] = "test"

# Use a temp file for the test database (in-memory SQLite creates separate DB
# per connection, which breaks the blacklist module's sync engine approach)
_test_db_fd, _test_db_path = tempfile.mkstemp(suffix=".test.db")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_test_db_path}"

# ── Backend module imports ────────────────────────────────────────────────
from app.core.config import settings
from app.core.security import get_password_hash
from app.main import app
from app.core.blacklist import init_blacklist_table
from app.models.user import User
from app.models.ai_memory import (
    ChatMemory, UserPreferences, UserFeedback, GlobalInsights, ModelUsage,
    TradeRecord, CalculationHistory, IndicatorRequest, AutopilotTrade,
    UserPrompt, DefaultPromptStrategy, AutopilotSettings
)
from app.models.historical_lab import HistoricalBacktest
from app.models.market_data import MarketData

# ── Test database engine ─────────────────────────────────────────────────
from app.core.database import Base
from sqlalchemy.ext.asyncio import create_async_engine
test_engine = create_async_engine(
    f"sqlite+aiosqlite:///{_test_db_path}", echo=False
)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)


@pytest_asyncio.fixture(autouse=True)
async def setup_database():
    # Clear login rate limiter so tests don't get 429
    from app.api.auth import _login_attempts
    _login_attempts.clear()
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    init_blacklist_table()
    async with TestSessionLocal() as session:
        admin_pw = settings.DEFAULT_ADMIN_PASSWORD or "admin@2026"
        users = [
            User(username="admin", name="Admin", hashed_password=get_password_hash(admin_pw), role="admin"),
            User(username="keval_viradiya", name="Keval Viradiya", hashed_password=get_password_hash("Usdt@2026"), role="trader"),
            User(username="sagar_barot", name="Sagar Barot", hashed_password=get_password_hash("Usdt@2026"), role="trader"),
            User(username="meet_rao", name="Meet Rao", hashed_password=get_password_hash("Usdt@2026"), role="trader"),
            User(username="guest", name="Guest", hashed_password=get_password_hash("Usdt@2026"), role="viewer"),
        ]
        for u in users:
            session.add(u)
        await session.commit()
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_db():
    yield
    try:
        os.close(_test_db_fd)
        os.unlink(_test_db_path)
    except (PermissionError, FileNotFoundError):
        pass


from app.core.database import get_db

async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
    async with TestSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


app.dependency_overrides[get_db] = override_get_db


@pytest_asyncio.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    async with TestSessionLocal() as session:
        yield session


async def _login(client: AsyncClient, username: str, password: str) -> str:
    resp = await client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, f"Login failed for {username}: {resp.text[:200]}"
    return resp.json()["access_token"]


@pytest_asyncio.fixture
async def admin_token(client: AsyncClient) -> str:
    pw = settings.DEFAULT_ADMIN_PASSWORD or "admin@2026"
    return await _login(client, "admin", pw)


@pytest_asyncio.fixture
def auth_headers(admin_token: str) -> dict:
    return {"Authorization": f"Bearer {admin_token}"}


@pytest_asyncio.fixture
def trader_headers(trader_token: str) -> dict:
    return {"Authorization": f"Bearer {trader_token}"}


@pytest.fixture(scope="session")
def parquet_dir() -> Path:
    path = BACKEND_DIR.parent / "data_archive" / "parquet_storage"
    assert path.exists(), f"Parquet directory not found: {path}"
    return path


@pytest.fixture(scope="session")
def mt5_available() -> bool:
    import httpx
    try:
        url = settings.MT5_CONNECTOR_URL or "http://localhost:5001"
        resp = httpx.get(f"{url}/health", timeout=3)
        return resp.status_code == 200
    except Exception:
        return False


# ── Environment-dependent tests ──────────────────────────────────────────────
# Some integration tests call a real AI provider or read the local parquet
# archive. When those are unavailable the tests are SKIPPED with a visible
# reason instead of failing, so a missing API key never hides a real bug.
_PARQUET_DIR = BACKEND_DIR.parent / "data_archive" / "parquet_storage"


def _missing_ai_key() -> bool:
    return not settings.NVIDIA_API_KEY


def _missing_parquet() -> bool:
    return not (_PARQUET_DIR / "XAUUSD_2026.parquet").exists()


def pytest_collection_modifyitems(config, items):
    skip_ai = pytest.mark.skip(reason="requires NVIDIA_API_KEY in environment")
    skip_data = pytest.mark.skip(reason=f"requires local parquet archive at {_PARQUET_DIR}")
    for item in items:
        if "requires_ai_key" in item.keywords and _missing_ai_key():
            item.add_marker(skip_ai)
        if "requires_parquet" in item.keywords and _missing_parquet():
            item.add_marker(skip_data)
