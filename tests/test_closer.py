"""The closer: the other half of an absolute auction.

A bid that wins is only a win if the auction actually shuts at its deadline and says so
exactly once. These run one pass at a time via `close_due` rather than driving the loop.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import text

from app.auctions import APP_TZ, MAX_EXTENSION, close_due, place_bid_tx, run_closer
from app.models import Auction, User

# Its own seeder rather than the bid suite's: these want an auction already past its
# deadline and no bidder, which is the opposite of what the bid tests need.


async def _seed(maker, ends_delta: timedelta = timedelta(seconds=-1)) -> int:
    async with maker() as s:
        seller = User(email="seller@x.com", pw_hash="x")
        s.add(seller)
        await s.flush()
        ends_at = datetime.now(timezone.utc) + ends_delta
        auction = Auction(
            title="t",
            seller_id=seller.id,
            starting_bid=Decimal("10.00"),
            ends_at=ends_at,
            hard_ends_at=ends_at + MAX_EXTENSION,
        )
        s.add(auction)
        await s.commit()
        return auction.id


async def _seed_with_winner(maker) -> tuple[int, int]:
    """Bid while the auction is open, then move the deadline into the past — the only
    way to end up with a won auction that is also due to close."""
    aid = await _seed(maker, ends_delta=timedelta(hours=1))
    uid = await _add_bidder(maker)
    async with maker() as s:
        assert await place_bid_tx(s, aid, uid, Decimal("25.00")) is not None
    async with maker() as s:
        await s.execute(
            text("UPDATE auctions SET ends_at = now() - interval '1 second' WHERE id = :id"),
            {"id": aid},
        )
        await s.commit()
    return aid, uid


async def _deadline(maker, aid: int) -> datetime:
    async with maker() as s:
        return (await s.get(Auction, aid)).ends_at


async def _add_bidder(maker) -> int:
    async with maker() as s:
        bidder = User(email="bidder@x.com", pw_hash="x")
        s.add(bidder)
        await s.commit()
        return bidder.id


async def test_closer_does_not_fire_on_an_auction_a_late_bid_extended(sm, channels):
    """M3's exit criterion, with the closer genuinely running.

    A bid lands a second before the deadline while the loop is ticking every 20ms. Past
    the deadline it was seeded with, the auction is still open and nothing was announced.

    This proves the closer stays off an extended auction. It does not prove the extension
    is atomic with the bid: a split implementation survives this race. That property is
    pinned by test_the_bid_and_its_extension_are_one_statement instead.
    """
    aid = await _seed(sm, ends_delta=timedelta(seconds=1))
    uid = await _add_bidder(sm)
    original = await _deadline(sm, aid)

    task = asyncio.create_task(run_closer(sm, channels, interval=0.02))
    try:
        async with sm() as s:
            assert await place_bid_tx(s, aid, uid, Decimal("30.00")) is not None
        await asyncio.sleep(1.5)  # well past the deadline the auction had when it was seeded
        assert await _status(sm, aid) == "open"
        assert await _deadline(sm, aid) > original
        assert channels.published == []
    finally:
        # Never awaited: an uncancellable loop would hang the runner rather than fail.
        task.cancel()
    await asyncio.sleep(0.05)
    assert task.cancelled()


async def _status(maker, aid: int) -> str:
    async with maker() as s:
        return (await s.get(Auction, aid)).status


async def test_closes_an_auction_past_its_deadline(sm, channels):
    aid = await _seed(sm)
    assert await close_due(sm, channels) == 1
    assert await _status(sm, aid) == "closed"


async def test_leaves_an_auction_before_its_deadline_open(sm, channels):
    aid = await _seed(sm, ends_delta=timedelta(hours=1))
    assert await close_due(sm, channels) == 0
    assert await _status(sm, aid) == "open"
    assert channels.published == []


async def test_publishes_the_winner_on_the_auction_channel(sm, channels):
    """Whole-dict equality on purpose: a key silently added or dropped from an event a
    client parses should fail here. ends_at uses the same key and rendering as the bid
    event, so the two events describing one auction agree about its shape."""
    aid, uid = await _seed_with_winner(sm)
    ends_at = await _deadline(sm, aid)
    await close_due(sm, channels)
    assert channels.published == [
        (
            f"auction:{aid}",
            {
                "type": "closed",
                "auction_id": aid,
                "winner_id": uid,
                "amount": "25.00",
                "ends_at": ends_at.astimezone(APP_TZ).isoformat(),
            },
        )
    ]


async def test_publishes_a_close_with_no_bids(sm, channels):
    """Nobody bid. The auction still closes, and the event says so rather than
    carrying a zero that would read as a sale."""
    aid = await _seed(sm)
    ends_at = await _deadline(sm, aid)
    await close_due(sm, channels)
    assert channels.published == [
        (
            f"auction:{aid}",
            {
                "type": "closed",
                "auction_id": aid,
                "winner_id": None,
                "amount": None,
                "ends_at": ends_at.astimezone(APP_TZ).isoformat(),
            },
        )
    ]


async def test_a_second_pass_closes_and_publishes_nothing(sm, channels):
    """Idempotency. The closer runs every second forever; a closed auction must not be
    announced again on the next tick."""
    await _seed(sm)
    assert await close_due(sm, channels) == 1
    assert await close_due(sm, channels) == 0
    assert len(channels.published) == 1


async def test_two_closers_at_once_publish_once(sm, channels, other_channels):
    """Two instances, one deadline. The conditional UPDATE decides which one owns the
    close; the other must find nothing rather than announce a second winner."""
    await _seed(sm)
    counts = await asyncio.gather(close_due(sm, channels), close_due(sm, other_channels))
    assert sorted(counts) == [0, 1]
    assert len(channels.published) + len(other_channels.published) == 1


async def test_run_closer_stops_when_cancelled(sm, channels):
    """`_stop_closer` cancels this task on shutdown. Swallowing the CancelledError the
    sleep raises loses the cancellation for good: the loop keeps ticking, and anything
    that waits for the task — including `asyncio.wait_for`, which cancels then waits —
    waits forever. Nothing here awaits the task, so a regression fails instead of hanging.
    """
    task = asyncio.create_task(run_closer(sm, channels, interval=0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.sleep(0.05)
    assert task.cancelled()


async def test_run_closer_survives_a_failing_tick(sm, channels):
    """A transient database error must not kill the loop: an auction whose deadline
    passes during an outage still has to close once the database is back."""

    class Broken:
        calls = 0

        def __call__(self):
            Broken.calls += 1
            raise RuntimeError("connection refused")

    task = asyncio.create_task(run_closer(Broken(), channels, interval=0.01))
    await asyncio.sleep(0.1)
    calls = Broken.calls
    task.cancel()
    await asyncio.sleep(0.02)
    assert calls > 1  # it retried rather than dying on the first failure


async def test_close_skips_a_row_another_closer_holds(sm, channels):
    """Two instances, and one already holds the row. The second must find nothing and
    move on, not block for the length of the other's transaction."""
    aid = await _seed(sm)
    async with sm() as holder:
        await holder.execute(text("SELECT id FROM auctions WHERE id = :id FOR UPDATE"), {"id": aid})
        assert await asyncio.wait_for(close_due(sm, channels), timeout=2) == 0
        assert channels.published == []
        await holder.rollback()
    # and the row is not lost — the next pass takes it
    assert await close_due(sm, channels) == 1


async def test_logs_how_late_each_close_was(sm, channels, caplog):
    """The closer polls, so an auction always closes some time after its deadline. How
    far after is the number that says whether the poll interval is good enough."""
    await _seed(sm, ends_delta=timedelta(seconds=-5))
    with caplog.at_level(logging.INFO, logger="app.auctions"):
        await close_due(sm, channels)
    record = next(r for r in caplog.records if r.msg.startswith("closed auction"))
    _, late = record.args
    assert 5 <= late < 15  # it ended 5s ago; anything near zero means it is not measured


async def test_a_failing_tick_says_why(sm, channels, caplog):
    """The loop swallowed every exception silently, so an outage looked identical to
    having nothing to close."""

    class Broken:
        def __call__(self):
            raise RuntimeError("connection refused")

    with caplog.at_level(logging.ERROR, logger="app.auctions"):
        task = asyncio.create_task(run_closer(Broken(), channels, interval=0.01))
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.sleep(0.02)
    assert "closer tick failed" in caplog.text
    assert "connection refused" in caplog.text
