"""Shared Postgres fixtures. The suite talks to a real Postgres — the money path is
conditional SQL and MVCC behaviour, neither of which a sqlite stand-in reproduces.

    docker run -d --name fmsc-pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=fmsc \
        -p 5432:5432 postgres:16-alpine
    alembic upgrade head
    DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/fmsc pytest
"""

from __future__ import annotations

import asyncio
import os

# Before anything imports app: app.codes reads the pepper at import and refuses to start
# without one, which is the behaviour a deployment wants and an awkward thing for a test
# suite to be surprised by. A fixed value, because the tests assert on lookups.
os.environ.setdefault("FMSC_CODE_PEPPER", "test-pepper-not-a-secret-0123456789abcdef")

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.models import User

PG_URL = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/fmsc"
)
if PG_URL.startswith("postgresql://"):
    PG_URL = PG_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

# Every table, named. The short list this replaced leaned on CASCADE to reach the rest,
# which meant a new foreign key silently widened what the wipe locked — M11's two tables
# did exactly that, and the wider lock set deadlocked against the closer on CI. Spelled out,
# what this takes AccessExclusiveLock on is visible here and changes only when edited.
# CASCADE stays as a backstop for the next table somebody forgets.
_TABLES = (
    "settlements",
    "second_chance_offers",
    "outbox",
    "bids",
    "auctions",
    "code_audit",
    "code_attempts",
    "community_discord",
    "community_memberships",
    "community_codes",
    "communities",
    "ws_tickets",
    "sessions",
    "login_attempts",
    "users",
)
TRUNCATE_ALL = text(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE")

# The list above is only visible if it stays complete. A table nobody adds to it is reached
# by CASCADE instead, which is the widening this commit exists to stop, and silently. Asserted
# at import so the suite says so at collection rather than deadlocking on CI months later.
# The other direction needs no check: TRUNCATE on a table that is gone fails on its own.
_UNWIPED = set(User.metadata.tables) - set(_TABLES)
assert not _UNWIPED, f"tables missing from the wipe list, so only CASCADE reaches them: {_UNWIPED}"


async def wipe(session) -> None:
    """Empty every table, retrying once if the closer deadlocks against it.

    TRUNCATE needs AccessExclusiveLock on all of them, and tests that keep an application
    running — the e2e module holds one for its whole lifespan — have a closer ticking
    through auctions and the outbox the entire time. The two can take their locks in
    opposite orders, which Postgres resolves by shooting one of them, and the one holding a
    whole application hostage should not be the survivor's problem. Retrying is honest
    here: the wipe has no state to lose and the second attempt races nothing new.
    """
    for last_try in (False, True):
        try:
            await session.execute(TRUNCATE_ALL)
            await session.commit()
            return
        except DBAPIError:
            await session.rollback()
            if last_try:
                raise
            # Long enough for the closer's own transaction to finish its tick.
            await asyncio.sleep(0.2)


@pytest.fixture
async def sm():
    # NullPool: every checkout is a fresh connection on the current loop — no cross-loop reuse,
    # and concurrent sessions get distinct connections so they genuinely contend in Postgres.
    engine = create_async_engine(PG_URL, poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        await wipe(s)
    yield maker
    await engine.dispose()


class ChannelSpy:
    """Stand-in for ChannelsPlugin. Only `publish` is used by the closer."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    def publish(self, data: dict, channel: str) -> None:
        self.published.append((channel, data))


@pytest.fixture
def channels() -> ChannelSpy:
    return ChannelSpy()


@pytest.fixture
def other_channels() -> ChannelSpy:
    """A second subscriber, for asserting that two closers do not both announce."""
    return ChannelSpy()
