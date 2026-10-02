from fastapi import APIRouter, Body, Depends, HTTPException, status, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime, timezone
from collections import defaultdict
import logging
import time

from ..core.database import get_db
from ..core.security import (
    verify_password, get_password_hash,
    create_access_token, create_refresh_token,
    decode_token, get_current_user,
    set_password, token_claims, issued_before_password_change,
)
from ..core.blacklist import blacklist_token
from ..core.client_ip import client_ip
from ..core.config import password_problem
from ..models.user import User
from ..models.schemas import UserLogin, Token, UserResponse, PasswordChange
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Authentication"])

# ── Login throttling ─────────────────────────────────────────────────────────
# In memory, per worker. The app runs as a single worker (see deploy notes).
_login_attempts: dict[str, list[float]] = defaultdict(list)   # address -> attempt times
_LOGIN_RATE_LIMIT = 10          # attempts per address per window
_LOGIN_RATE_WINDOW = 60         # seconds

_failed_logins: dict[str, list[float]] = defaultdict(list)    # username -> failure times
_MAX_FAILURES = 5               # failures per username before a lockout
_FAILURE_WINDOW = 15 * 60       # seconds

# Checked against when the username does not exist, so an unknown username takes
# as long to reject as a wrong password and response time reveals nothing.
_DUMMY_HASH = get_password_hash("dummy-password-for-timing-only")

VALID_ROLES = ("admin", "trader", "viewer")


def reset_login_limits() -> None:
    """Clear all throttling state. Used by tests."""
    _login_attempts.clear()
    _failed_logins.clear()


def _within(times: list[float], window: int, now: float) -> list[float]:
    return [t for t in times if now - t < window]


def _check_login_rate(ip: str) -> bool:
    now = time.time()
    _login_attempts[ip] = _within(_login_attempts[ip], _LOGIN_RATE_WINDOW, now)
    if len(_login_attempts[ip]) >= _LOGIN_RATE_LIMIT:
        return False
    _login_attempts[ip].append(now)
    return True


def _username_locked(username: str) -> bool:
    now = time.time()
    _failed_logins[username] = _within(_failed_logins[username], _FAILURE_WINDOW, now)
    return len(_failed_logins[username]) >= _MAX_FAILURES


def _validate_new_password(password: str) -> None:
    problem = password_problem(password)
    if problem:
        raise HTTPException(status_code=400, detail=f"That password {problem}.")


def _validate_role(role: str) -> None:
    if role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"Role must be one of: {', '.join(VALID_ROLES)}")


@router.post("/login", response_model=Token)
async def login(request: Request, user_data: UserLogin, db: AsyncSession = Depends(get_db)):
    if not _check_login_rate(client_ip(request)):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many login attempts. Try again in {_LOGIN_RATE_WINDOW} seconds."
        )
    username = user_data.username
    if _username_locked(username):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed attempts for this account. Try again in 15 minutes."
        )

    try:
        user = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
    except Exception:
        raise HTTPException(status_code=500, detail="Internal authentication error")

    # Always run one password check, so unknown usernames and wrong passwords take the same time.
    is_valid = verify_password(user_data.password, user.hashed_password if user else _DUMMY_HASH)
    if not user or not is_valid:
        _failed_logins[username].append(time.time())
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password"
        )

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is disabled")

    _failed_logins.pop(username, None)
    try:
        user.last_login = datetime.now(timezone.utc)
        await db.commit()
    except Exception:
        logger.warning("Could not record the last login of %s", username, exc_info=True)

    claims = token_claims(user)
    return Token(access_token=create_access_token(data=claims), refresh_token=create_refresh_token(data=claims))


@router.post("/refresh", response_model=Token)
async def refresh_token(refresh_data: dict, db: AsyncSession = Depends(get_db)):
    refresh_token = refresh_data.get("refresh_token")
    if not refresh_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Refresh token is required"
        )

    payload = await decode_token(refresh_token)
    if not payload or payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token"
        )

    username = payload.get("sub")
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()

    if not user or user.id != payload.get("user_id"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found"
        )
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account is disabled")
    if issued_before_password_change(payload, user):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="The password was changed. Log in again")

    # Revoke the old refresh token so it can't be reused. If that fails, refuse:
    # handing out a new pair would leave the old token working too.
    try:
        exp_dt = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
        await blacklist_token(refresh_token, expires_at=exp_dt)
    except Exception:
        logger.exception("Could not revoke a used refresh token for %s", username)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Could not complete the refresh. Try again.")

    access_token = create_access_token(data=token_claims(user))
    new_refresh_token = create_refresh_token(data=token_claims(user))

    return Token(access_token=access_token, refresh_token=new_refresh_token)


class LogoutRequest(BaseModel):
    refresh_token: str | None = None


@router.post("/logout")
async def logout(
    body: LogoutRequest | None = Body(default=None),
    credentials: HTTPAuthorizationCredentials | None = Depends(HTTPBearer(auto_error=False)),
):
    """Revoke the access token and the refresh token, so neither works again.

    Needs no valid login: the access token may already have expired, and holding
    a refresh token is enough to be allowed to throw it away. Always answers 200.
    An earlier version revoked only the access token, so the seven-day refresh
    token could keep minting new logins after logout.
    """
    revoked = 0
    candidates = [
        (credentials.credentials if credentials else None, "access"),
        (body.refresh_token if body else None, "refresh"),
    ]
    for token, expected_type in candidates:
        if not token:
            continue
        payload = await decode_token(token, check_revoked=False)
        if not payload or payload.get("type") != expected_type:
            continue
        await blacklist_token(token, expires_at=datetime.fromtimestamp(payload["exp"], tz=timezone.utc))
        revoked += 1
    return {"message": "Logged out", "revoked": revoked}


@router.get("/me")
async def get_me(current_user: dict = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.username == current_user["username"]))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    return UserResponse.model_validate(user).model_dump(mode="json")


@router.put("/password")
async def change_password(
    password_data: PasswordChange,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(User).where(User.username == current_user["username"]))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    if not verify_password(password_data.current_password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect"
        )

    _validate_new_password(password_data.new_password)
    set_password(user, password_data.new_password)
    await db.commit()

    # Every earlier session, this one included, has ended. A fresh pair keeps
    # the person making the change logged in.
    claims = token_claims(user)
    return {
        "message": "Password changed successfully",
        "access_token": create_access_token(data=claims),
        "refresh_token": create_refresh_token(data=claims),
    }


# User Management Schemas
class UserCreate(BaseModel):
    username: str
    name: str
    password: str
    role: str = "trader"


class UserUpdate(BaseModel):
    name: str | None = None
    password: str | None = None
    role: str | None = None
    is_active: bool | None = None


def require_admin(current_user: dict = Depends(get_current_user)):
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user


@router.get("/users")
async def list_users(
    skip: int = 0,
    limit: int = 100,
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db)
):
    """List all users - Admin only"""
    limit = min(limit, 500)
    result = await db.execute(select(User).order_by(User.created_at.desc()).offset(skip).limit(limit))
    users = result.scalars().all()
    return [UserResponse.model_validate(u).model_dump(mode="json") for u in users]


@router.post("/users")
async def create_user(
    user_data: UserCreate,
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db)
):
    """Create new user - Admin only"""
    result = await db.execute(select(User).where(User.username == user_data.username))
    existing = result.scalar_one_or_none()
    
    if existing:
        raise HTTPException(status_code=400, detail="Username already exists")
    _validate_role(user_data.role)
    _validate_new_password(user_data.password)

    new_user = User(
        username=user_data.username,
        name=user_data.name,
        hashed_password=get_password_hash(user_data.password),
        role=user_data.role
    )
    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)
    return UserResponse.model_validate(new_user).model_dump(mode="json")


@router.put("/users/{user_id}")
async def update_user(
    user_id: int,
    user_data: UserUpdate,
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db)
):
    """Update user - Admin only"""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    if user.username == "admin" and (
        (user_data.role is not None and user_data.role != "admin") or user_data.is_active is False
    ):
        raise HTTPException(status_code=400, detail="The admin account cannot be demoted or disabled")

    if user_data.name is not None:
        user.name = user_data.name
    if user_data.role is not None:
        _validate_role(user_data.role)
        user.role = user_data.role
    if user_data.password is not None:
        _validate_new_password(user_data.password)
        set_password(user, user_data.password)
    # Used to be accepted and silently ignored, so "deactivate" did nothing.
    if user_data.is_active is not None:
        user.is_active = user_data.is_active
    
    await db.commit()
    await db.refresh(user)
    return UserResponse.model_validate(user).model_dump(mode="json")


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: int,
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db)
):
    """Delete user - Admin only"""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    if user.username == "admin":
        raise HTTPException(status_code=400, detail="Cannot delete admin user")
    
    await db.delete(user)
    await db.commit()
    return {"message": f"User {user.username} deleted successfully"}