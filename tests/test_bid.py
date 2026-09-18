"""The one critical check: the atomic bid money path (`place_bid_tx`).

Tests the real SQL the handler uses, against a real Postgres, with truly concurrent
sessions — the HTTP layer is thin glue and is smoke-tested manually (see README).

    docker run -d --name fmsc-pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=fmsc \
        -p 5432:5432 postgres:16-alpine
    alembic upgrade head
    DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/fmsc pytest
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.auctions import APP_TZ, place_bid_tx, resolve_deadline
from app.models import Auction, User

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
        await s.execute(text("TRUNCATE bids, auctions, sessions, users RESTART IDENTITY CASCADE"))
        await s.commit()
    yield maker
    await engine.dispose()


async def _seed(maker, starting="10.00", ends_delta=timedelta(hours=1)) -> tuple[int, int]:
    async with maker() as s:
        user = User(email="x@x.com", pw_hash="x")
        s.add(user)
        await s.flush()
        auction = Auction(
            title="t",
            seller_id=user.id,
            starting_bid=Decimal(starting),
            ends_at=datetime.now(timezone.utc) + ends_delta,
        )
        s.add(auction)
        await s.commit()
        return user.id, auction.id


def test_naive_deadline_is_jakarta():
    naive = datetime(2026, 6, 28, 17, 0, 0)
    resolved = resolve_deadline(naive)
    assert resolved.utcoffset() == timedelta(hours=7)  # WIB
    # an aware input is preserved, not reinterpreted
    aware = datetime(2026, 6, 28, 17, 0, 0, tzinfo=timezone.utc)
    assert resolve_deadline(aware) is aware
    assert APP_TZ.key == "Asia/Jakarta"


async def test_accept_and_reject(sm):
    uid, aid = await _seed(sm)
    async with sm() as s:
        assert await place_bid_tx(s, aid, uid, Decimal("10.00")) is True  # opening == starting ok
    async with sm() as s:
        assert await place_bid_tx(s, aid, uid, Decimal("9.99")) is False  # lower
    async with sm() as s:
        assert await place_bid_tx(s, aid, uid, Decimal("10.00")) is False  # equal must lose
    async with sm() as s:
        assert await place_bid_tx(s, aid, uid, Decimal("11.00")) is True  # strictly higher

    async with sm() as s:
        auction = await s.get(Auction, aid)
        assert auction.current_bid == Decimal("11.00")
        assert auction.bid_count == 2


async def test_bid_on_ended_auction_rejected(sm):
    uid, aid = await _seed(sm, ends_delta=timedelta(seconds=-1))  # already past deadline
    async with sm() as s:
        assert await place_bid_tx(s, aid, uid, Decimal("10.00")) is False


async def test_concurrent_bids_single_winner(sm):
    uid, aid = await _seed(sm)

    async def bid() -> bool:
        async with sm() as s:
            return await place_bid_tx(s, aid, uid, Decimal("20.00"))

    r1, r2 = await asyncio.gather(bid(), bid())
    # equal amounts → exactly one wins, the other can't strictly beat it
    assert sorted([r1, r2]) == [False, True]

    async with sm() as s:
        auction = await s.get(Auction, aid)
        bid_rows = (
            await s.execute(text("SELECT count(*) FROM bids WHERE auction_id = :id"), {"id": aid})
        ).scalar()
    assert auction.current_bid == Decimal("20.00")
    assert auction.bid_count == 1
    assert bid_rows == 1
