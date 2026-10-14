"""M7 acceptance: a session that ends, and one that can be taken away.

Until now a session token was valid forever and nothing could revoke it, so a token
captured once — from a log, a backup, a shared laptop — was a permanent key to the
account. These are criteria 1 and 2:

1. An expired session authenticates nothing, and the expiry is enforced in the query
   rather than compared in Python afterwards.
2. Logout revokes one session and leaves the user's others working; revoke-all revokes
   every one.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from litestar.exceptions import NotAuthorizedException
from sqlalchemy import text

from app.auth import (
    SESSION_TTL,
    revoke_all_sessions,
    revoke_session,
    user_for_token,
)
from app.models import User


async def _user(maker, email: str) -> User:
    async with maker() as s:
        user = User(email=email, pw_hash="x")
        s.add(user)
        await s.commit()
        return user


async def _session(maker, user_id: int, token: str, ttl: timedelta) -> str:
    """Raw INSERT with the expiry computed by Postgres.

    `clock_timestamp() + interval`, not a Python datetime: the comparison under test is
    made by Postgres, so the deadline has to come off the same clock. A Python-computed
    expiry turns any skew between this process and the database into a failing test —
    which is exactly what nine tests did on this machine when its container clock drifted.
    """
    async with maker() as s:
        await s.execute(
            text(
                "INSERT INTO sessions (token, user_id, expires_at)"
                " VALUES (:token, :uid, clock_timestamp() + CAST(:ttl AS interval))"
            ),
            # The timedelta itself, not a string: asyncpg types the cast parameter as an
            # interval and encodes a timedelta into it directly.
            {"token": token, "uid": user_id, "ttl": ttl},
        )
        await s.commit()
        return token


async def test_a_live_session_authenticates(sm):
    """The control for everything below: without it, a broken query that refuses
    everything would pass every expiry test in this file."""
    user = await _user(sm, "live@x.com")
    token = await _session(sm, user.id, "live", SESSION_TTL)
    async with sm() as s:
        assert (await user_for_token(s, token)).email == "live@x.com"


async def test_an_expired_session_authenticates_nothing(sm):
    """Criterion 1. The row is still there — expiry is a property of the session, not of
    whether some sweeper has run yet."""
    user = await _user(sm, "stale@x.com")
    token = await _session(sm, user.id, "stale", timedelta(seconds=-1))
    async with sm() as s:
        with pytest.raises(NotAuthorizedException):
            await user_for_token(s, token)
    async with sm() as s:
        assert (
            await s.scalar(text("SELECT count(*) FROM sessions WHERE token = :t"), {"t": token})
            == 1
        )


async def test_a_session_expiring_mid_transaction_authenticates_nothing(sm):
    """Criterion 1, the part that says "in the query".

    Postgres `now()` is transaction_timestamp(). Authentication is the first query of the
    request, so with `now()` the comparison is made against a clock frozen at the moment
    the transaction opened — and a session that expires while the request is still being
    served authenticates anyway. Same bug as M1's bid deadline, one layer lower.
    """
    user = await _user(sm, "expiring@x.com")
    token = await _session(sm, user.id, "expiring", timedelta(milliseconds=300))
    async with sm() as s:
        await s.execute(text("SELECT 1"))  # opens the transaction, freezing now()
        await asyncio.sleep(0.6)  # the expiry passes on the wall clock
        with pytest.raises(NotAuthorizedException):
            await user_for_token(s, token)


async def test_logout_revokes_one_session_and_leaves_the_others(sm):
    """Criterion 2. Signing out of a laptop must not sign out the phone, or nobody will
    use it — and a revocation nobody uses protects nothing."""
    user = await _user(sm, "two@x.com")
    laptop = await _session(sm, user.id, "laptop", SESSION_TTL)
    phone = await _session(sm, user.id, "phone", SESSION_TTL)

    async with sm() as s:
        assert await revoke_session(s, laptop) == 1
    async with sm() as s:
        with pytest.raises(NotAuthorizedException):
            await user_for_token(s, laptop)
    async with sm() as s:
        assert (await user_for_token(s, phone)).email == "two@x.com"


async def test_revoking_an_unknown_token_changes_nothing(sm):
    """A logout with a stale or forged token is not an error; it is already true. It must
    also not be a way to probe which tokens exist."""
    user = await _user(sm, "ghost@x.com")
    live = await _session(sm, user.id, "ghost-live", SESSION_TTL)
    async with sm() as s:
        assert await revoke_session(s, "never-issued") == 0
    async with sm() as s:
        assert (await user_for_token(s, live)).email == "ghost@x.com"


async def test_revoke_all_revokes_every_session(sm):
    """Criterion 2. What a user reaches for after losing a device, and the only answer to
    a token that has already leaked."""
    user = await _user(sm, "all@x.com")
    other = await _user(sm, "bystander@x.com")
    tokens = [await _session(sm, user.id, f"all-{n}", SESSION_TTL) for n in range(3)]
    bystander = await _session(sm, other.id, "bystander", SESSION_TTL)

    async with sm() as s:
        assert await revoke_all_sessions(s, user.id) == 3
    for token in tokens:
        async with sm() as s:
            with pytest.raises(NotAuthorizedException):
                await user_for_token(s, token)
    # Scoped to one user: revoke-all is not a logout button for the whole service.
    async with sm() as s:
        assert (await user_for_token(s, bystander)).email == "bystander@x.com"

