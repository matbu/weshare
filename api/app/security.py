from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import settings
from .db import get_pool

hasher = PasswordHasher()
bearer = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def create_token(user_id: int) -> str:
    now = datetime.now(timezone.utc)
    payload = {"sub": str(user_id), "iat": now, "exp": now + timedelta(days=settings.jwt_ttl_days)}
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


async def optional_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> dict | None:
    if creds is None:
        return None
    try:
        payload = jwt.decode(creds.credentials, settings.jwt_secret, algorithms=["HS256"])
        user_id = int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")
    row = await get_pool().fetchrow(
        "SELECT id, email::text AS email, display_name, role::text AS role, created_at "
        "FROM users WHERE id = $1",
        user_id,
    )
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown user")
    return dict(row)


async def current_user(user: dict | None = Depends(optional_user)) -> dict:
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required",
                            headers={"WWW-Authenticate": "Bearer"})
    return user


async def moderator(user: dict = Depends(current_user)) -> dict:
    if user["role"] not in ("moderator", "admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "moderators only")
    return user
