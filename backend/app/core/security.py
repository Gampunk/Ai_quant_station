from datetime import datetime, timedelta, timezone
from typing import Optional
from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from .config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer()


import bcrypt

def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        password_bytes = plain_password.encode('utf-8')
        # Warn if password exceeds bcrypt's 72-byte limit
        if len(password_bytes) > 72:
            password_bytes = password_bytes[:72]
        hashed_bytes = hashed_password.encode('utf-8')
        return bcrypt.checkpw(password_bytes, hashed_bytes)
    except Exception:
        try:
            return pwd_context.verify(plain_password, hashed_password)
        except Exception:
            return False


def get_password_hash(password: str) -> str:
    password_bytes = password.encode("utf-8")[:72]
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password_bytes, salt).decode("utf-8")


TRADING_ROLES = ("admin", "trader")


def _signing_key() -> str:
    """The JWT signing key. Refuses to work with a missing or weak key rather than guessing."""
    settings.validate_secret_key()
    return settings.SECRET_KEY


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire, "type": "access"})
    encoded_jwt = jwt.encode(to_encode, _signing_key(), algorithm=settings.ALGORITHM)
    return encoded_jwt


def create_refresh_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode.update({"exp": expire, "type": "refresh"})
    encoded_jwt = jwt.encode(to_encode, _signing_key(), algorithm=settings.ALGORITHM)
    return encoded_jwt


async def decode_token(token: str, check_revoked: bool = True) -> dict | None:
    """Return the token's payload, or None if it is invalid, expired, or revoked.

    The signature is checked first, so a forged or malformed token never costs a
    database lookup. Pass check_revoked=False only where revocation cannot matter,
    such as choosing a rate-limit bucket.
    """
    try:
        payload = jwt.decode(token, _signing_key(), algorithms=[settings.ALGORITHM])
    except JWTError:
        return None
    if check_revoked:
        from .blacklist import is_token_blacklisted
        if await is_token_blacklisted(token):
            return None
    return payload


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """The logged-in user, read fresh from the database on every request.

    Role and active status come from the database, not the token, so demoting or
    deactivating someone takes effect on their very next request rather than when
    their token happens to expire.
    """
    payload = await decode_token(credentials.credentials)
    if payload is None:
        raise _unauthorized("Invalid authentication token")
    if payload.get("type") != "access":
        raise _unauthorized("Invalid token type")
    user_id = payload.get("user_id")
    if user_id is None or payload.get("sub") is None:
        raise _unauthorized("Invalid token payload")

    from .database import AsyncSessionLocal
    from ..models.user import User
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
    if user is None or user.username != payload.get("sub"):
        raise _unauthorized("Account no longer exists")
    if not user.is_active:
        raise _unauthorized("Account is disabled")

    return {"username": user.username, "id": user.id, "role": user.role}


async def get_current_user_optional(credentials: Optional[HTTPAuthorizationCredentials] = Depends(HTTPBearer(auto_error=False))):
    if credentials is None:
        return None
    try:
        return await get_current_user(credentials)
    except HTTPException:
        return None

def require_role(*roles: str):
    """Dependency that allows only the given roles. Returns the current user."""
    async def checker(current_user: dict = Depends(get_current_user)) -> dict:
        if current_user.get("role") not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This action needs one of these roles: {', '.join(roles)}",
            )
        return current_user
    return checker


# Placing trades, running the autopilot, and anything that executes AI-written code.
require_trader = require_role(*TRADING_ROLES)
