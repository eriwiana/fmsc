"""Shared Postgres fixtures. The suite talks to a real Postgres — the money path is
conditional SQL and MVCC behaviour, neither of which a sqlite stand-in reproduces.

    docker run -d --name fmsc-pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=fmsc \
        -p 5432:5432 postgres:16-alpine
    alembic upgrade head
    DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/fmsc pytest
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

PG_URL = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/fmsc"
)
if PG_URL.startswith("postgresql://"):
    PG_URL = PG_URL.replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture
async def sm():
    # NullPool: every checkout is a fresh connection on the current loop — no cross-loop reuse,
    # and concurrent sessions get distinct connections so they genuinely contend in Postgres.
    engine = create_async_engine(PG_URL, poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        await s.execute(
            text(
                "TRUNCATE outbox, bids, auctions, ws_tickets, sessions, users RESTART IDENTITY CASCADE"
            )
        )
        await s.commit()
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
