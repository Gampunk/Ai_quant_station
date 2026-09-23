from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from starlette.middleware.base import BaseHTTPMiddleware
from slowapi.errors import RateLimitExceeded
from slowapi import _rate_limit_exceeded_handler
from slowapi.middleware import SlowAPIMiddleware
import os
import logging
import logging.config
import json
from pathlib import Path

from .core.rate_limit import limiter

from .core.config import settings
from .core.database import AsyncSessionLocal
from .core.security import get_password_hash
from .core.blacklist import cleanup_expired_tokens
from .api import auth, mt5, trade, ai, yahoo, execute, analytics, autopilot, historical_lab, backtest
from .core.mt5_sync import start_sync_scheduler
from .models.user import User


def _setup_logging():
    """Configure structured logging — JSON formatter for production, human-readable for dev."""
    if os.getenv("APP_ENV", "development") != "production":
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        return

    class JsonFormatter(logging.Formatter):
        def format(self, record):
            log_entry = {
                "timestamp": self.formatTime(record),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                "module": record.module,
                "funcName": record.funcName,
                "lineno": record.lineno,
            }
            if record.exc_info:
                log_entry["traceback"] = self.formatException(record.exc_info)
            return json.dumps(log_entry)

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


_setup_logging()

# Import all models to register them with SQLAlchemy Base before database creation
from . import models  # noqa: F401

app = FastAPI(
    title="The Finance Engine API",
    description="Professional Quantitative Trading Platform API",
    version="2.0.0"
)

# ── Rate Limiting Setup ──────────────────────────────────────────────────────

# Middleware that decodes JWT and sets request.state.user for rate limit key
from .core.security import decode_token


class UserIdentityMiddleware(BaseHTTPMiddleware):
    """Extract authenticated user from JWT and attach to request.state for rate limiting."""
    async def dispatch(self, request: Request, call_next):
        request.state.user = None
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            try:
                # Only picks a rate-limit bucket, so skip the revocation lookup.
                payload = await decode_token(auth[7:], check_revoked=False)
                if payload:
                    request.state.user = {
                        "id": payload.get("user_id"),
                        "username": payload.get("sub"),
                        "role": payload.get("role"),
                    }
            except Exception:
                pass
        response = await call_next(request)
        return response


app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


async def create_default_users():
    """Create the admin account on a fresh database, and nothing else.

    The password comes only from DEFAULT_ADMIN_PASSWORD. No other accounts are
    created: add people with `python create_admin.py` or the User Management page.
    Earlier versions created four more accounts with passwords written in the code.
    """
    from sqlalchemy import select
    from .core.config import admin_password_problem

    log = logging.getLogger("startup")
    async with AsyncSessionLocal() as session:
        if (await session.execute(select(User).where(User.username == "admin"))).scalar_one_or_none():
            return

        problem = admin_password_problem(settings.DEFAULT_ADMIN_PASSWORD)
        if problem:
            log.warning(
                "No admin account created: DEFAULT_ADMIN_PASSWORD %s. "
                "Set it in backend/.env and restart, or run `python create_admin.py`.", problem,
            )
            return

        session.add(User(
            username="admin", name="System Administrator", role="admin",
            hashed_password=get_password_hash(settings.DEFAULT_ADMIN_PASSWORD),
        ))
        await session.commit()
        log.info("Admin account created from DEFAULT_ADMIN_PASSWORD.")


@app.on_event("startup")
async def startup_event():
    # Refuse to start without a strong signing key, in every environment
    settings.validate_secret_key()

    # Refuse to start if the configured connector is not local or private
    from .core.connector_guard import check_connector_url
    check_connector_url(settings.MT5_CONNECTOR_URL)

    # Create, upgrade or adopt the database, and refuse to start if it does not
    # match the models. See core/schema.py.
    from .core.database import init_db
    await init_db()

    # Initial cleanup of stale revoked tokens
    removed = await cleanup_expired_tokens()
    if removed:
        print(f"Cleaned up {removed} expired revoked-token entries.")
    await create_default_users()

    # Auto-restart autopilot for users who had it enabled before reboot
    try:
        from .core.database import AsyncSessionLocal
        from .models.ai_memory import AutopilotSettings
        from sqlalchemy import select
        async with AsyncSessionLocal() as _db:
            result = await _db.execute(
                select(AutopilotSettings).where(AutopilotSettings.enabled == True)
            )
            for row in result.scalars().all():
                from .api.autopilot import _start_autopilot_internal
                await _start_autopilot_internal(row.user_id)
                print(f"  Autopilot auto-restarted for user #{row.user_id}")
    except Exception as e:
        print(f"  Autopilot auto-restart check: {e}")

    start_sync_scheduler()

    # Start strategy score aggregator (hourly cron)
    try:
        from .core.strategy_scorer import start_strategy_scorer, update_strategy_scores
        start_strategy_scorer()
        import asyncio
        asyncio.create_task(update_strategy_scores())
    except Exception as e:
        print(f"  Strategy scorer start: {e}")

    # Start daily report scheduler (23:50 UTC)
    try:
        from .core.email_reports import start_report_scheduler
        start_report_scheduler()
    except Exception as e:
        print(f"  Report scheduler start: {e}")

@app.on_event("shutdown")
async def shutdown_event():
    from .core.mt5_connector import shutdown_connector
    from .core.mt5_sync import shutdown_scheduler
    from .core.email_reports import shutdown_report_scheduler
    await shutdown_connector()
    shutdown_scheduler()
    shutdown_report_scheduler()

# Middleware chain: UserIdentity (innermost) → CORS → SlowAPI (outermost)
app.add_middleware(UserIdentityMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(SlowAPIMiddleware)

# Include API routers
app.include_router(auth.router, prefix="/api")
app.include_router(mt5.router, prefix="/api")
app.include_router(trade.router, prefix="/api")
app.include_router(ai.router, prefix="/api")
app.include_router(yahoo.router, prefix="/api")
app.include_router(execute.router, prefix="/api")
app.include_router(analytics.router, prefix="/api")
app.include_router(autopilot.router, prefix="/api")
app.include_router(historical_lab.router, prefix="/api")
app.include_router(backtest.router, prefix="/api")

# Mount static files (React build) - ONLY if frontend is built
# In development, frontend runs on separate dev server (Vite)
frontend_dist_path = Path(__file__).parent.parent.parent / "frontend" / "dist"

if frontend_dist_path.exists() and frontend_dist_path.is_dir():
    # Production mode: Serve built React frontend
    app.mount("/assets", StaticFiles(directory=frontend_dist_path / "assets"), name="assets")
    
    # Serve React app for all non-API routes (enables client-side routing)
    @app.get("/{full_path:path}")
    async def serve_react_app(full_path: str):
        # Don't interfere with API routes
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")
        
        # Serve index.html for client-side routing (React Router)
        return FileResponse(frontend_dist_path / "index.html")
else:
    # Development mode: Frontend runs on separate Vite dev server
    @app.get("/")
    async def root():
        return {
            "message": "The Finance Engine API is running",
            "docs": "/docs",
            "frontend_dev_server": "http://localhost:5173",
            "note": "To enable single-server deployment: Run 'npm run build' in frontend directory, then restart this server"
        }

@app.get("/health")
async def health():
    return {"status": "healthy"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8002"))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=(os.getenv("ENV", "production") == "development")
    )