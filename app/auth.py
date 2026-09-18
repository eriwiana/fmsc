from __future__ import annotations

import hashlib
import hmac
import os
import secrets

from litestar import Request, post
from litestar.exceptions import ClientException, NotAuthorizedException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Session, User
from app.schemas import SignupRequest, TokenResponse

# scrypt params: stdlib, no dependency. n=2**14 is a sane interactive cost.
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"{salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    salt_hex, hash_hex = stored.split("$", 1)
    dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), **_SCRYPT)
    return hmac.compare_digest(dk.hex(), hash_hex)


async def _issue_token(db_session: AsyncSession, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    db_session.add(Session(token=token, user_id=user_id))
    await db_session.commit()
    return token


@post("/auth/signup")
async def signup(data: SignupRequest, db_session: AsyncSession) -> TokenResponse:
    if not data.email or not data.password:
        raise ClientException("email and password required")
    user = User(email=data.email, pw_hash=hash_password(data.password))
    db_session.add(user)
    try:
        await db_session.flush()
    except IntegrityError:
        await db_session.rollback()
        raise ClientException("email already registered")
    return TokenResponse(token=await _issue_token(db_session, user.id))


@post("/auth/login")
async def login(data: SignupRequest, db_session: AsyncSession) -> TokenResponse:
    user = await db_session.scalar(select(User).where(User.email == data.email))
    if user is None or not verify_password(data.password, user.pw_hash):
        raise NotAuthorizedException("invalid credentials")
    return TokenResponse(token=await _issue_token(db_session, user.id))


async def provide_current_user(request: Request, db_session: AsyncSession) -> User:
    """HTTP auth dependency: resolve `Authorization: Bearer <token>` to a User."""
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    return await user_for_token(db_session, token)


async def user_for_token(db_session: AsyncSession, token: str) -> User:
    if not token:
        raise NotAuthorizedException("missing token")
    user = await db_session.scalar(
        select(User).join(Session, Session.user_id == User.id).where(Session.token == token)
    )
    if user is None:
        raise NotAuthorizedException("invalid token")
    return user
