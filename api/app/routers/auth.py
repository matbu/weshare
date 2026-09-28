import asyncpg
from fastapi import APIRouter, Depends, HTTPException

from ..db import get_pool
from ..schemas import LoginIn, RegisterIn, TokenOut, UserOut
from ..security import create_token, current_user, hash_password, hasher, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])

USER_COLS = "id, email::text AS email, display_name, role::text AS role, created_at"


@router.post("/register", response_model=TokenOut, status_code=201)
async def register(body: RegisterIn):
    try:
        row = await get_pool().fetchrow(
            f"INSERT INTO users (email, password_hash, display_name) VALUES ($1, $2, $3) "
            f"RETURNING {USER_COLS}",
            body.email.lower(),
            hash_password(body.password),
            body.display_name,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(409, "email already registered")
    return TokenOut(access_token=create_token(row["id"]), user=UserOut(**dict(row)))


@router.post("/login", response_model=TokenOut)
async def login(body: LoginIn):
    pool = get_pool()
    row = await pool.fetchrow(f"SELECT {USER_COLS}, password_hash FROM users WHERE email = $1", body.email.lower())
    if row is None or not verify_password(row["password_hash"], body.password):
        raise HTTPException(401, "invalid email or password")
    if hasher.check_needs_rehash(row["password_hash"]):
        await pool.execute(
            "UPDATE users SET password_hash = $1 WHERE id = $2", hash_password(body.password), row["id"]
        )
    user = {k: row[k] for k in UserOut.model_fields}
    return TokenOut(access_token=create_token(row["id"]), user=UserOut(**user))


@router.get("/me", response_model=UserOut)
async def me(user: dict = Depends(current_user)):
    return UserOut(**user)
