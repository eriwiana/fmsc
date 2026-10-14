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
from types import SimpleNamespace

import pytest
from litestar.exceptions import NotAuthorizedException, TooManyRequestsException
from sqlalchemy import text

from app import auth
from app.auth import (
    LOGIN_WINDOW,
    MAX_FAILURES_PER_ACCOUNT,
    MAX_FAILURES_PER_IP,
    SESSION_TTL,
    client_ip,
    hash_password,
    login,
    login_retry_after,
    provide_token,
    prune_login_attempts,
    purge_expired_sessions,
    revoke_all_sessions,
    revoke_session,
    user_for_token,
)
from app.models import User
from app.schemas import SignupRequest

PASSWORD = "correct horse battery staple"


async def _user(maker, email: str) -> User:
    async with maker() as s:
        user = User(email=email, pw_hash="x")
        s.add(user)
        await s.commit()
        return user


async def _user_with_password(maker, email: str) -> User:
    """A user who can actually log in, for the rate-limit tests."""
    async with maker() as s:
        user = User(email=email, pw_hash=hash_password(PASSWORD))
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


async def test_an_expired_session_is_purged_and_a_live_one_is_kept(sm):
    """Beyond the criteria, and needed by them: once sessions expire, nothing else deletes
    the dead rows, so the table would grow by one row per login forever. Same sweeper slot
    in the closer's tick as the tickets.
    """
    user = await _user(sm, "sweep@x.com")
    await _session(sm, user.id, "dead", timedelta(seconds=-1))
    live = await _session(sm, user.id, "alive", SESSION_TTL)

    async with sm() as s:
        assert await purge_expired_sessions(s) == 1
    async with sm() as s:
        assert (await user_for_token(s, live)).email == "sweep@x.com"


async def test_logout_without_a_token_is_refused(sm):
    """A 204 would tell a client whose Authorization header never got built that it had
    signed out. An unknown or expired token still gets the 204 — that one is already
    true — so this is about the absent header, not about which tokens exist."""
    with pytest.raises(NotAuthorizedException):
        await provide_token(SimpleNamespace(headers={}))
    with pytest.raises(NotAuthorizedException):
        await provide_token(SimpleNamespace(headers={"Authorization": "Bearer "}))
    assert await provide_token(SimpleNamespace(headers={"Authorization": "Bearer abc"})) == "abc"


def _request(ip: str) -> SimpleNamespace:
    """Just enough Request for the handler: the header it reads and the peer it falls
    back to."""
    return SimpleNamespace(headers={"X-Forwarded-For": ip}, client=None)


async def _login(maker, email: str, password: str, ip: str = "10.0.0.1"):
    async with maker() as s:
        return await login.fn(
            data=SignupRequest(email=email, password=password),
            request=_request(ip),
            db_session=s,
        )


async def _fail_login(maker, email: str, ip: str = "10.0.0.1") -> None:
    with pytest.raises(NotAuthorizedException):
        await _login(maker, email, "wrong", ip)


async def test_the_account_bucket_is_not_keyed_by_address(sm):
    """Criterion 3. Five failures against one account from five different addresses must
    still lock that account.

    This is the bug the criterion names: with a combined (email, ip) key, every attacker
    with a botnet gets a fresh five guesses per address, so the per-account limit stops
    nobody who was worth stopping.
    """
    await _user_with_password(sm, "target@x.com")
    for n in range(MAX_FAILURES_PER_ACCOUNT):
        await _fail_login(sm, "target@x.com", ip=f"10.0.0.{n + 1}")

    # The correct password, from a sixth address: still refused, because the account is
    # what is locked.
    with pytest.raises(TooManyRequestsException):
        await _login(sm, "target@x.com", PASSWORD, ip="10.0.0.99")


async def test_the_address_bucket_is_not_keyed_by_account(sm):
    """Criterion 3, the other half. Twenty failures from one address, spread across twenty
    different accounts, must still lock that address — otherwise a scan for weak passwords
    across many accounts never trips a limit, because no single account sees enough.
    """
    for n in range(MAX_FAILURES_PER_IP):
        await _fail_login(sm, f"victim{n}@x.com", ip="203.0.113.7")

    await _user_with_password(sm, "innocent@x.com")
    with pytest.raises(TooManyRequestsException):
        await _login(sm, "innocent@x.com", PASSWORD, ip="203.0.113.7")


async def test_one_locked_account_does_not_lock_another(sm):
    """The buckets are per account, not one global counter. A limiter that locks everyone
    out when one account is attacked is a denial of service with extra steps."""
    await _user_with_password(sm, "attacked@x.com")
    await _user_with_password(sm, "bystander@x.com")
    for n in range(MAX_FAILURES_PER_ACCOUNT):
        await _fail_login(sm, "attacked@x.com", ip=f"198.51.100.{n + 1}")

    # Different account, and an address that has not spent its own budget.
    assert (await _login(sm, "bystander@x.com", PASSWORD, ip="198.51.100.200")).token


async def test_a_successful_login_clears_the_accounts_failures(sm):
    """Four typos then a correct password must not leave the account one typo away from a
    lockout for the next quarter of an hour."""
    await _user_with_password(sm, "typist@x.com")
    for _ in range(MAX_FAILURES_PER_ACCOUNT - 1):
        await _fail_login(sm, "typist@x.com")
    assert (await _login(sm, "typist@x.com", PASSWORD)).token

    for _ in range(MAX_FAILURES_PER_ACCOUNT - 1):
        await _fail_login(sm, "typist@x.com")
    assert (await _login(sm, "typist@x.com", PASSWORD)).token


async def test_failures_older_than_the_window_do_not_count(sm):
    """The limit is a rate, not a lifetime total. Without the window a user who mistyped
    five times last month is locked out forever."""
    await _user_with_password(sm, "patient@x.com")
    for _ in range(MAX_FAILURES_PER_ACCOUNT):
        await _fail_login(sm, "patient@x.com")
    async with sm() as s:
        # Age every attempt past the window, by the database's clock.
        await s.execute(
            text(
                "UPDATE login_attempts SET failed_at ="
                " failed_at - CAST(:w AS interval) - interval '1 second'"
            ),
            {"w": LOGIN_WINDOW},
        )
        await s.commit()
    assert (await _login(sm, "patient@x.com", PASSWORD)).token


async def test_the_lockout_says_how_long_to_wait(sm):
    """A 429 with no Retry-After tells a client to guess, and a client that guesses
    retries immediately. The number counts from the oldest attempt still in the window,
    so it shrinks as the bucket drains rather than resetting on every rejected try."""
    await _user_with_password(sm, "waiting@x.com")
    for _ in range(MAX_FAILURES_PER_ACCOUNT):
        await _fail_login(sm, "waiting@x.com")

    with pytest.raises(TooManyRequestsException) as caught:
        await _login(sm, "waiting@x.com", PASSWORD)
    retry_after = int(caught.value.headers["Retry-After"])
    assert 0 < retry_after <= LOGIN_WINDOW.total_seconds()


async def test_a_locked_account_is_refused_before_the_password_is_checked(sm):
    """The lockout has to come first, or each rejected attempt still costs a scrypt hash —
    and an attacker who has already lost their budget can keep burning the CPU of the
    machine that locked them out."""
    await _user_with_password(sm, "costly@x.com")
    for _ in range(MAX_FAILURES_PER_ACCOUNT):
        await _fail_login(sm, "costly@x.com")

    async with sm() as s:
        before = await s.scalar(text("SELECT count(*) FROM login_attempts"))
    with pytest.raises(TooManyRequestsException):
        await _login(sm, "costly@x.com", "wrong")
    async with sm() as s:
        # A refusal that never reached the password check records nothing, so a locked-out
        # attacker cannot keep their own lockout alive by hammering it.
        assert await s.scalar(text("SELECT count(*) FROM login_attempts")) == before


async def test_attempts_outside_the_window_are_pruned(sm):
    """Nothing else deletes them and the window query never reads them again."""
    await _fail_login(sm, "old@x.com")
    async with sm() as s:
        await s.execute(
            text(
                "UPDATE login_attempts SET failed_at ="
                " failed_at - CAST(:w AS interval) - interval '1 second'"
            ),
            {"w": LOGIN_WINDOW},
        )
        await s.commit()
    await _fail_login(sm, "recent@x.com")

    async with sm() as s:
        assert await prune_login_attempts(s) == 1
    async with sm() as s:
        assert await s.scalar(text("SELECT count(*) FROM login_attempts")) == 1


async def test_a_forged_forwarded_header_cannot_buy_a_fresh_budget(sm):
    """X-Forwarded-For is read from the right, because only the rightmost entry was put
    there by the proxy in front of us. The rest is whatever the client typed.

    Reading from the left would give an attacker a new address per request — a limit that
    anybody can opt out of — and would also let them spend the budget of an address they
    do not have.
    """
    claimed, real = "1.2.3.4", "203.0.113.9"
    async with sm() as s:
        await login_retry_after(s, "x@x.com", real)  # no failures yet
    for n in range(MAX_FAILURES_PER_IP):
        with pytest.raises(NotAuthorizedException):
            async with sm() as s:
                await login.fn(
                    data=SignupRequest(email=f"spread{n}@x.com", password="wrong"),
                    # What a client sends, plus what the proxy appends.
                    request=SimpleNamespace(
                        headers={"X-Forwarded-For": f"{claimed}, {real}"}, client=None
                    ),
                    db_session=s,
                )

    async with sm() as s:
        # Counted against the proxy's entry, not the claimed one.
        assert await login_retry_after(s, "whoever@x.com", real) is not None
        assert await login_retry_after(s, "whoever@x.com", claimed) is None


async def test_an_empty_forwarded_entry_is_not_an_address(sm):
    """A trailing comma leaves the rightmost entry empty. An empty string would be a
    bucket of its own that every malformed header on the service shares, which is both a
    way to spend somebody else's budget and a way to dodge one's own."""
    assert client_ip(SimpleNamespace(headers={"X-Forwarded-For": "1.2.3.4, "}, client=None)) == (
        "unknown"
    )
    assert client_ip(SimpleNamespace(headers={}, client=SimpleNamespace(host="10.1.2.3"))) == (
        "10.1.2.3"
    )


async def test_a_login_for_an_unknown_address_still_pays_for_a_hash(sm, monkeypatch):
    """Skipping scrypt when the email is unknown makes the response time say whether an
    account exists — the same enumeration the failure counting exists to stop, except it
    needs one request per address instead of five. Identical bodies do not help if one of
    them arrives fifty milliseconds sooner.

    Counting the calls rather than timing them: a timing assertion on a shared runner is a
    flake, and the property under test is that the work happens at all.
    """
    hashed: list[str] = []
    real = auth.verify_password
    monkeypatch.setattr(
        auth, "verify_password", lambda pw, stored: hashed.append(stored) or real(pw, stored)
    )

    await _fail_login(sm, "nobody@x.com")
    assert len(hashed) == 1

    await _user_with_password(sm, "somebody@x.com")
    await _fail_login(sm, "somebody@x.com")
    assert len(hashed) == 2
    # Different stored hashes, or the dummy would be recognisable in its own right.
    assert hashed[0] != hashed[1]
