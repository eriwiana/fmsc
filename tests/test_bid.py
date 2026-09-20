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


async def _seed(maker, starting="10.00", ends_delta=timedelta(hours=1)) -> tuple[int, int, int]:
    """One seller, one bidder, one auction. Seller and bidder are distinct on purpose:
    an auction whose seller is also its only bidder cannot exercise the real guards."""
    async with maker() as s:
        seller = User(email="seller@x.com", pw_hash="x")
        bidder = User(email="bidder@x.com", pw_hash="x")
        s.add_all([seller, bidder])
        await s.flush()
        auction = Auction(
            title="t",
            seller_id=seller.id,
            starting_bid=Decimal(starting),
            ends_at=datetime.now(timezone.utc) + ends_delta,
        )
        s.add(auction)
        await s.commit()
        return seller.id, bidder.id, auction.id


async def _add_bidder(maker, email: str) -> int:
    async with maker() as s:
        user = User(email=email, pw_hash="x")
        s.add(user)
        await s.commit()
        return user.id


async def _bid_rows(maker, auction_id: int) -> int:
    async with maker() as s:
        return (
            await s.execute(
                text("SELECT count(*) FROM bids WHERE auction_id = :id"), {"id": auction_id}
            )
        ).scalar()


def test_naive_deadline_is_jakarta():
    naive = datetime(2026, 6, 28, 17, 0, 0)
    resolved = resolve_deadline(naive)
    assert resolved.utcoffset() == timedelta(hours=7)  # WIB
    # an aware input is preserved, not reinterpreted
    aware = datetime(2026, 6, 28, 17, 0, 0, tzinfo=timezone.utc)
    assert resolve_deadline(aware) is aware
    assert APP_TZ.key == "Asia/Jakarta"


async def test_opening_bid_may_equal_starting_bid(sm):
    _, bidder, aid = await _seed(sm)
    async with sm() as s:
        assert await place_bid_tx(s, aid, bidder, Decimal("10.00")) is True


async def test_bid_below_starting_bid_rejected(sm):
    _, bidder, aid = await _seed(sm)
    async with sm() as s:
        assert await place_bid_tx(s, aid, bidder, Decimal("9.99")) is False


async def test_bid_equal_to_current_bid_rejected(sm):
    _, bidder, aid = await _seed(sm)
    async with sm() as s:
        await place_bid_tx(s, aid, bidder, Decimal("10.00"))
    async with sm() as s:
        assert await place_bid_tx(s, aid, bidder, Decimal("10.00")) is False


async def test_higher_bid_replaces_the_leader(sm):
    _, bidder, aid = await _seed(sm)
    other = await _add_bidder(sm, "other@x.com")
    async with sm() as s:
        await place_bid_tx(s, aid, bidder, Decimal("10.00"))
    async with sm() as s:
        assert await place_bid_tx(s, aid, other, Decimal("11.00")) is True
    async with sm() as s:
        auction = await s.get(Auction, aid)
    assert auction.current_bid == Decimal("11.00")
    assert auction.current_winner_id == other
    assert auction.bid_count == 2


async def test_rejected_bid_writes_no_history_row(sm):
    _, bidder, aid = await _seed(sm)
    async with sm() as s:
        assert await place_bid_tx(s, aid, bidder, Decimal("9.99")) is False
    assert await _bid_rows(sm, aid) == 0


async def test_bid_on_closed_auction_rejected(sm):
    """The status guard alone: the deadline is still an hour away."""
    _, bidder, aid = await _seed(sm)
    async with sm() as s:
        await s.execute(text("UPDATE auctions SET status = 'closed' WHERE id = :id"), {"id": aid})
        await s.commit()
    async with sm() as s:
        assert await place_bid_tx(s, aid, bidder, Decimal("10.00")) is False


async def test_bid_on_ended_auction_rejected(sm):
    _, bidder, aid = await _seed(sm, ends_delta=timedelta(seconds=-1))  # already past deadline
    async with sm() as s:
        assert await place_bid_tx(s, aid, bidder, Decimal("10.00")) is False


async def test_late_bid_rejected_despite_stale_transaction_clock(sm):
    """Postgres `now()` is transaction_timestamp(), not wall time.

    On the real HTTP path the auth dependency queries the session table first, so the
    transaction is already open — and its clock already frozen — before the bid statement
    runs. A bid that arrives after the deadline must still lose.
    """
    _, bidder, aid = await _seed(sm, ends_delta=timedelta(milliseconds=300))
    async with sm() as s:
        await s.execute(text("SELECT 1"))  # opens the transaction, freezing now()
        await asyncio.sleep(0.6)  # the deadline passes on the wall clock
        assert await place_bid_tx(s, aid, bidder, Decimal("10.00")) is False


async def test_bid_on_missing_auction_rejected(sm):
    _, bidder, aid = await _seed(sm)
    async with sm() as s:
        assert await place_bid_tx(s, aid + 1000, bidder, Decimal("10.00")) is False


async def test_concurrent_bids_single_winner(sm):
    _, bidder, aid = await _seed(sm)
    other = await _add_bidder(sm, "other@x.com")

    async def bid(uid: int) -> bool:
        async with sm() as s:
            return await place_bid_tx(s, aid, uid, Decimal("20.00"))

    r1, r2 = await asyncio.gather(bid(bidder), bid(other))
    # equal amounts → exactly one wins, the other can't strictly beat it
    assert sorted([r1, r2]) == [False, True]

    async with sm() as s:
        auction = await s.get(Auction, aid)
    assert auction.current_bid == Decimal("20.00")
    assert auction.bid_count == 1
    assert await _bid_rows(sm, aid) == 1
