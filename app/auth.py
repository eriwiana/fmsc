from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

from litestar import Request, post
from litestar.di import Provide
from litestar.exceptions import ClientException, NotAuthorizedException
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Session, User, WsTicket
from app.schemas import SignupRequest, TicketResponse, TokenResponse

# Long enough to open a socket on a slow connection, short enough that one captured from
# a log or a Referer header is already dead.
TICKET_TTL = timedelta(seconds=30)

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
        # `from None`: the client is told the email is taken, not handed a chained
        # traceback about a unique constraint it cannot act on.
        raise ClientException("email already registered") from None
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


_authed = {"current_user": Provide(provide_current_user)}

# DELETE ... RETURNING is the check: the row is gone whether or not it turns out to be
# valid, so a ticket cannot be presented twice even by two sockets arriving together.
# `expires_at > clock_timestamp()` is evaluated by Postgres, not compared against this
# process's clock afterwards. The two disagree by enough to matter — it is the same class
# of bug as M1's now() and as the boundary flakes in this branch's test suite.
_CONSUME_TICKET_SQL = text(
    "DELETE FROM ws_tickets WHERE token = :token"
    " RETURNING user_id, expires_at > clock_timestamp() AS live"
)


@post("/ws/tickets", dependencies=_authed)
async def issue_ws_ticket(current_user: User, db_session: AsyncSession) -> TicketResponse:
    """Trade a session token for a credential that is safe to put in a URL."""
    ticket = WsTicket(
        token=secrets.token_urlsafe(32),
        user_id=current_user.id,
        expires_at=datetime.now(timezone.utc) + TICKET_TTL,
    )
    db_session.add(ticket)
    await db_session.commit()
    return TicketResponse(ticket=ticket.token, expires_at=ticket.expires_at)


async def user_for_ticket(db_session: AsyncSession, ticket: str) -> User:
    """Spend a ticket and return whose it was. Raises if it is unknown or expired."""
    row = (await db_session.execute(_CONSUME_TICKET_SQL, {"token": ticket})).first()
    await db_session.commit()
    # One message for every failure: unknown, already spent and expired are the same
    # answer to a caller, and telling them apart would say which tickets once existed.
    if row is None or not row.live:
        raise NotAuthorizedException("invalid or expired ticket")
    user = await db_session.get(User, row.user_id)
    if user is None:
        raise NotAuthorizedException("invalid or expired ticket")
    return user


async def purge_expired_tickets(db_session: AsyncSession) -> int:
    """Expired tickets are never read again and nothing else deletes them."""
    # clock_timestamp(), not now(): now() is transaction time, which is the bug M1 spent a
    # milestone on. Harmless in a transaction this short, wrong the moment it is not.
    result = await db_session.execute(
        text("DELETE FROM ws_tickets WHERE expires_at <= clock_timestamp()")
    )
    await db_session.commit()
    return result.rowcount
