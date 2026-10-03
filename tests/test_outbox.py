"""The outbox: an event cannot be lost because a process died at the wrong moment.

Publishing is otherwise at-most-once. The bid commits, then the process publishes — and a
crash in between closes or moves an auction without telling a single watcher, with nothing
left behind to notice. The outbox row is written in the bid's own transaction, so the
record of "this must be announced" commits or rolls back with the money.

Delivery is therefore at-least-once: a crash after publishing but before marking sent
makes the relay publish again. That is only safe because every event carries a per-auction
sequence number, so a duplicate is one a client has already seen.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import text

from app.auctions import (
    MAX_EXTENSION,
    close_due,
    place_bid,
    place_bid_tx,
    prune_outbox,
    relay_pending,
)
from app.models import Auction, User
from app.schemas import BidRequest

# Its own seeders rather than the bid suite's, the way test_closer.py does: these want an
# auction and two bidders and nothing else.


async def _seed(maker, ends_delta: timedelta = timedelta(hours=1)) -> tuple[int, int, int]:
    async with maker() as s:
        seller = User(email="seller@x.com", pw_hash="x")
        first = User(email="first@x.com", pw_hash="x")
        second = User(email="second@x.com", pw_hash="x")
        s.add_all([seller, first, second])
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
        return first.id, second.id, auction.id


async def _post_bid(maker, aid: int, uid: int, amount: str, channels):
    """Through the handler, which is what publishes and marks the row sent."""
    async with maker() as s:
        return await place_bid.fn(
            auction_id=aid,
            data=BidRequest(amount=Decimal(amount)),
            request=SimpleNamespace(headers={}),
            current_user=await s.get(User, uid),
            db_session=s,
            channels=channels,
        )


async def _unsent(maker) -> list[tuple[int, int]]:
    async with maker() as s:
        rows = await s.execute(
            text("SELECT auction_id, seq FROM outbox WHERE sent_at IS NULL ORDER BY seq")
        )
        return [tuple(r) for r in rows]


async def _sent_count(maker) -> int:
    async with maker() as s:
        return await s.scalar(text("SELECT count(*) FROM outbox WHERE sent_at IS NOT NULL"))


async def test_an_accepted_bid_records_the_event_it_owes(sm):
    """place_bid_tx writes the outbox row and does not publish: publishing is the caller's
    job, so a caller that dies leaves the obligation behind rather than losing it."""
    bidder, _, aid = await _seed(sm)
    async with sm() as s:
        assert await place_bid_tx(s, aid, bidder, Decimal("10.00")) is not None

    assert await _unsent(sm) == [(aid, 1)]


async def test_a_refused_bid_records_nothing(sm):
    """The outbox row shares the bid's transaction, so a refusal rolls it back with the
    money. An event announcing a bid that never happened is worse than a missing one."""
    bidder, _, aid = await _seed(sm)
    async with sm() as s:
        assert await place_bid_tx(s, aid, bidder, Decimal("9.99")) is None

    assert await _unsent(sm) == []


async def test_the_relay_publishes_what_a_dead_process_left_behind(sm, channels):
    """The crash case. place_bid_tx committed the bid and the obligation; nothing published.
    The relay is what makes the watchers whole."""
    bidder, _, aid = await _seed(sm)
    async with sm() as s:
        await place_bid_tx(s, aid, bidder, Decimal("10.00"))

    assert await relay_pending(sm, channels) == 1

    ((channel, event),) = channels.published
    assert channel == f"auction:{aid}"
    assert event["type"] == "bid"
    assert event["seq"] == 1
    assert await _unsent(sm) == []


async def test_the_relay_leaves_a_published_event_alone(sm, channels):
    """A second pass must publish nothing, or every watcher sees every bid twice for as
    long as the relay keeps ticking."""
    bidder, _, aid = await _seed(sm)
    await _post_bid(sm, aid, bidder, "10.00", channels)

    assert await relay_pending(sm, channels) == 0
    assert len(channels.published) == 1
    assert await _sent_count(sm) == 1


async def test_the_handler_marks_its_own_event_sent(sm, channels):
    """The handler publishes immediately — a bid that waited for the relay's tick would
    reach watchers a second late — and marks the row so the relay does not repeat it."""
    bidder, _, aid = await _seed(sm)
    await _post_bid(sm, aid, bidder, "10.00", channels)

    assert await _unsent(sm) == []
    assert await _sent_count(sm) == 1


async def test_the_relay_publishes_in_sequence_order(sm, channels):
    """Out of order, a client that trusts the numbers would read the older event as a gap
    and drop its own socket."""
    bidder, second, aid = await _seed(sm)
    async with sm() as s:
        await place_bid_tx(s, aid, bidder, Decimal("10.00"))
    async with sm() as s:
        await place_bid_tx(s, aid, second, Decimal("11.00"))

    assert await relay_pending(sm, channels) == 2
    assert [event["seq"] for _, event in channels.published] == [1, 2]


async def test_sent_events_are_pruned_once_they_are_old_enough(sm, channels):
    """Nothing reads a sent row again, and nothing else deletes them, so without this the
    table grows by one row per bid for the life of the service. The window is long enough
    to investigate a problem against the rows that caused it."""
    bidder, _, aid = await _seed(sm)
    await _post_bid(sm, aid, bidder, "10.00", channels)
    async with sm() as s:
        await s.execute(text("UPDATE outbox SET sent_at = clock_timestamp() - interval '2 days'"))
        await s.commit()

    async with sm() as s:
        assert await prune_outbox(s) == 1
    async with sm() as s:
        assert await s.scalar(text("SELECT count(*) FROM outbox")) == 0


async def test_pruning_spares_a_recent_event_and_an_unsent_one(sm, channels):
    """Pruning on sent_at alone would delete the queue the relay has not got to yet."""
    bidder, second, aid = await _seed(sm)
    await _post_bid(sm, aid, bidder, "10.00", channels)
    async with sm() as s:
        await place_bid_tx(s, aid, second, Decimal("11.00"))

    async with sm() as s:
        assert await prune_outbox(s) == 0
    assert await _unsent(sm) == [(aid, 2)]
    assert await _sent_count(sm) == 1


async def test_a_close_records_the_event_it_owes(sm, channels):
    """The closer has the same hole: it flips the auction, then announces. A crash between
    the two shuts an auction with nobody told, and the auction is closed for good."""
    bidder, _, aid = await _seed(sm)
    await _post_bid(sm, aid, bidder, "10.00", channels)
    async with sm() as s:
        await s.execute(
            text("UPDATE auctions SET ends_at = now() - interval '5 seconds' WHERE id = :id"),
            {"id": aid},
        )
        await s.commit()

    assert await close_due(sm, channels) == 1
    # Published by the closer and marked, exactly as the bid handler does.
    assert await _unsent(sm) == []
    assert await _sent_count(sm) == 2
