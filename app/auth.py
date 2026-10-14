from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

from litestar import Request, post
from litestar.di import Provide
from litestar.exceptions import (
    ClientException,
    NotAuthorizedException,
    TooManyRequestsException,
)
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LoginAttempt, Session, User, WsTicket
from app.schemas import SignupRequest, TicketResponse, TokenResponse

# Long enough to open a socket on a slow connection, short enough that one captured from
# a log or a Referer header is already dead.
TICKET_TTL = timedelta(seconds=30)

# How long a login lasts. Chosen, not measured: long enough that bidders are not asked to
# sign in mid-auction, short enough that a token leaked today is useless next month.
# Logout and revoke-all are what handle a token known to have leaked; this is the backstop
# for the ones nobody notices.
SESSION_TTL = timedelta(days=7)

# Failed logins are limited on two independent buckets, never a combined (email, ip) key.
# A combined key defeats both halves at once: an attacker with many addresses gets a fresh
# per-account budget from each, and a scan across many accounts never spends a per-address
# budget. All three numbers are chosen, not measured.
#
# The account budget is the tight one because it is the one protecting a specific person.
# The address budget is looser because an office, a school or a mobile carrier can put many
# honest people behind one address.
LOGIN_WINDOW = timedelta(minutes=15)
MAX_FAILURES_PER_ACCOUNT = 5
MAX_FAILURES_PER_IP = 20

# Counts both buckets in one statement, and says when each would next let a request
# through. One subquery per bucket, each carrying its own equality: `count(*) FILTER` over
# the whole window reads the same numbers, but the only predicate is on failed_at, which
# neither index starts with — so it plans as a sequential scan and both indexes go unused.
# Measured at 50,000 attempts inside the window: 9.0 ms sequentially, 0.5 ms this way.
# The flood is exactly when the table is large, so the shape matters most under attack.
#
# The retry is still computed by Postgres against clock_timestamp(), never subtracted from
# this process's clock. It counts from the oldest attempt in the window because that is the
# one whose expiry drops the bucket back below its limit: an attempt is only ever recorded
# while neither bucket is full, so a bucket holds its limit and never more.
_LOGIN_FAILURES_SQL = text("""
    SELECT by_email.n AS by_email,
           by_ip.n AS by_ip,
           CEIL(EXTRACT(EPOCH FROM (
               by_email.oldest + CAST(:window AS interval) - clock_timestamp()))) AS email_retry,
           CEIL(EXTRACT(EPOCH FROM (
               by_ip.oldest + CAST(:window AS interval) - clock_timestamp()))) AS ip_retry
      FROM (SELECT count(*) AS n, MIN(failed_at) AS oldest
              FROM login_attempts
             WHERE email = :email
               AND failed_at > clock_timestamp() - CAST(:window AS interval)) AS by_email,
           (SELECT count(*) AS n, MIN(failed_at) AS oldest
              FROM login_attempts
             WHERE ip = :ip
               AND failed_at > clock_timestamp() - CAST(:window AS interval)) AS by_ip
    """)

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


# Hashed once at import, from a password nobody holds. `login` checks against this when
# the email is unknown, so a miss costs the same scrypt a hit does. Random rather than a
# constant so it is obvious nothing can ever match it.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(32))


def client_ip(request: Request) -> str:
    """The address a failed login is counted against.

    The *rightmost* X-Forwarded-For entry, not the leftmost: a client can send whatever it
    likes in that header, and a trusted proxy appends the peer it actually saw. With one
    proxy in front — Railway — the last entry is the real client and the first is whatever
    was claimed. Taking the leftmost would let an attacker have a new address per request,
    and let them spend someone else's budget.

    Falling back to the socket peer matters locally and in tests, where there is no proxy.
    """
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        # `or "unknown"`: a trailing comma leaves the rightmost entry empty, and an empty
        # string is a bucket of its own that every malformed header would share.
        return forwarded.rsplit(",", 1)[-1].strip() or "unknown"
    client = getattr(request, "client", None)
    return client.host if client else "unknown"


async def login_retry_after(db_session: AsyncSession, email: str, ip: str) -> int | None:
    """Seconds to wait before this login may be attempted again, or None if it may now.

    Read before the password is checked, so a locked-out caller costs one index scan
    rather than a scrypt hash.
    """
    row = (
        await db_session.execute(
            _LOGIN_FAILURES_SQL, {"email": email, "ip": ip, "window": LOGIN_WINDOW}
        )
    ).one()
    if row.by_email >= MAX_FAILURES_PER_ACCOUNT:
        return max(int(row.email_retry), 1)
    if row.by_ip >= MAX_FAILURES_PER_IP:
        return max(int(row.ip_retry), 1)
    return None


async def record_failed_login(db_session: AsyncSession, email: str, ip: str) -> None:
    """Both buckets are fed by one row; which bucket a row counts towards is decided by
    the query, not by what is written."""
    db_session.add(LoginAttempt(email=email, ip=ip, failed_at=datetime.now(timezone.utc)))
    await db_session.commit()


async def clear_failed_logins(db_session: AsyncSession, email: str) -> None:
    """A correct password empties the account's bucket.

    Without this, four typos followed by a successful login leave the account one typo
    away from a lockout for the rest of the window.

    The rows are the only record either bucket has, so deleting them by email also takes
    them out of the count for whichever addresses they came from: signing in refunds a
    little address budget as well. Not a way in — refunding it needs the password — but it
    does mean a shared address is softened by every honest user behind it who mistypes and
    then succeeds. Keeping the address count intact would need a cleared_at column and a
    filter on it in both buckets, which is more machinery than the leak is worth today.
    """
    await db_session.execute(delete(LoginAttempt).where(LoginAttempt.email == email))
    await db_session.commit()


async def prune_login_attempts(db_session: AsyncSession) -> int:
    """Attempts older than the window are never counted again."""
    result = await db_session.execute(
        text(
            "DELETE FROM login_attempts WHERE failed_at <= clock_timestamp() - CAST(:w AS interval)"
        ),
        {"w": LOGIN_WINDOW},
    )
    await db_session.commit()
    return result.rowcount


async def _issue_token(db_session: AsyncSession, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    db_session.add(
        Session(
            token=token,
            user_id=user_id,
            expires_at=datetime.now(timezone.utc) + SESSION_TTL,
        )
    )
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
async def login(data: SignupRequest, request: Request, db_session: AsyncSession) -> TokenResponse:
    ip = client_ip(request)
    retry_after = await login_retry_after(db_session, data.email, ip)
    if retry_after is not None:
        # Before the password check, so a caller who has spent their budget cannot keep
        # spending this machine's CPU on scrypt. Nothing is recorded for a request refused
        # here either, or hammering a lockout would extend it.
        raise TooManyRequestsException(
            detail="too many failed logins", headers={"Retry-After": str(retry_after)}
        )
    user = await db_session.scalar(select(User).where(User.email == data.email))
    # Hashed either way, against a throwaway hash when there is no such account. Skipping
    # the scrypt for a miss makes the response time say whether the address is registered,
    # which is the same enumeration the counting below is meant to stop (OWASP
    # WSTG-IDNT-04): identical bodies do not help if one of them arrives 50 ms sooner.
    correct = verify_password(data.password, user.pw_hash if user else _DUMMY_HASH)
    if user is None or not correct:
        # Counted whether or not the address exists: otherwise probing for valid accounts
        # is free, and the answer is the same either way.
        await record_failed_login(db_session, data.email, ip)
        raise NotAuthorizedException("invalid credentials")
    await clear_failed_logins(db_session, data.email)
    return TokenResponse(token=await _issue_token(db_session, user.id))


def bearer_token(request: Request) -> str:
    return request.headers.get("Authorization", "").removeprefix("Bearer ").strip()


async def provide_current_user(request: Request, db_session: AsyncSession) -> User:
    """HTTP auth dependency: resolve `Authorization: Bearer <token>` to a User."""
    return await user_for_token(db_session, bearer_token(request))


async def provide_token(request: Request) -> str:
    """For logout, which revokes the presented token rather than resolving it to a user.

    Presenting the token is the authorisation: it deletes its own row and nothing else,
    so an already-expired token can still clean up after itself.

    An empty header is refused rather than answered 204, because there is no token for
    that answer to be true of: a client whose header never got built would otherwise be
    told it had signed out. An unknown or expired token still gets the 204 — that one is
    already true, and telling the two apart would say which tokens exist.
    """
    token = bearer_token(request)
    if not token:
        raise NotAuthorizedException("missing token")
    return token


async def user_for_token(db_session: AsyncSession, token: str) -> User:
    if not token:
        raise NotAuthorizedException("missing token")
    user = await db_session.scalar(
        select(User)
        .join(Session, Session.user_id == User.id)
        .where(Session.token == token, Session.expires_at > func.clock_timestamp())
    )
    # One message for an unknown token and an expired one: the difference is not the
    # caller's business, and spelling it out says which tokens once existed.
    if user is None:
        raise NotAuthorizedException("invalid or expired token")
    return user


async def revoke_session(db_session: AsyncSession, token: str) -> int:
    """Log out one session. Returns how many rows went, which is 0 or 1.

    A logout with an unknown token is not an error — it is already true — and answering
    the same either way keeps it from being a way to probe which tokens exist.
    """
    result = await db_session.execute(delete(Session).where(Session.token == token))
    await db_session.commit()
    return result.rowcount


async def revoke_all_sessions(db_session: AsyncSession, user_id: int) -> int:
    """Log out everywhere. The answer to a token that has already leaked, and the reason
    expiry alone is not enough: seven days is a long time to wait for a stolen laptop."""
    result = await db_session.execute(delete(Session).where(Session.user_id == user_id))
    await db_session.commit()
    return result.rowcount


async def purge_expired_sessions(db_session: AsyncSession) -> int:
    """Expired sessions authenticate nothing and are never read again.

    clock_timestamp(), not now(), for the same reason as the tickets below.
    """
    result = await db_session.execute(
        text("DELETE FROM sessions WHERE expires_at <= clock_timestamp()")
    )
    await db_session.commit()
    return result.rowcount


_authed = {"current_user": Provide(provide_current_user)}


@post("/auth/logout", dependencies={"token": Provide(provide_token)}, status_code=204)
async def logout(token: str, db_session: AsyncSession) -> None:
    """Revoke the presented session and nothing else: signing out of a laptop leaves the
    phone signed in, or nobody uses it, and a revocation nobody uses protects nothing."""
    await revoke_session(db_session, token)


@post("/auth/logout-all", dependencies=_authed, status_code=204)
async def logout_all(current_user: User, db_session: AsyncSession) -> None:
    """Revoke every session this user has, including the one making the request."""
    await revoke_all_sessions(db_session, current_user.id)


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
