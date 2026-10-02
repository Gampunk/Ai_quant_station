from slowapi import Limiter
from starlette.requests import Request

from .client_ip import client_ip


def user_identifier(request: Request) -> str:
    """
    Prefer authenticated user id from request.state.user (set by middleware).
    Fallback to IP address for unauthenticated requests.
    """
    user = getattr(request.state, "user", None)
    if user and user.get("id"):
        return f"user:{user['id']}"
    return f"ip:{client_ip(request)}"


limiter = Limiter(key_func=user_identifier)
