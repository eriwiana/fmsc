"""The one critical check: the atomic bid money path (`place_bid_tx`).

Tests the real SQL the handler uses, with truly concurrent sessions — the HTTP layer is
thin glue and is smoke-tested manually (see README). Fixtures live in conftest.py.
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import event, text

from app.auctions import (
    APP_TZ,
    MAX_BID,
    MAX_EXTENSION,
    SNIPE_WINDOW,
    _to_response,
    create_auction,
    place_bid_tx,
    reject_reason,
    resolve_deadline,
)
from app.models import Auction, User
from app.schemas import CreateAuctionRequest


async def _seed(
    maker, starting="10.00", ends_delta=timedelta(hours=1), extension=MAX_EXTENSION
) -> tuple[int, int, int]:
    """One seller, one bidder, one auction. Seller and bidder are distinct on purpose:
    an auction whose seller is also its only bidder cannot exercise the real guards."""
    async with maker() as s:
        seller = User(email="seller@x.com", pw_hash="x")
        bidder = User(email="bidder@x.com", pw_hash="x")
        s.add_all([seller, bidder])
        await s.flush()
        ends_at = datetime.now(timezone.utc) + ends_delta
        auction = Auction(
            title="t",
            seller_id=seller.id,
            starting_bid=Decimal(starting),
            ends_at=ends_at,
            hard_ends_at=ends_at + extension,
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


async def test_created_auction_records_its_extension_ceiling(sm):
    """create_auction is the only place hard_ends_at is ever set, and nothing downstream
    can repair a wrong ceiling: it is what stops an auction extending forever."""
    async with sm() as s:
        seller = User(email="seller@x.com", pw_hash="x")
        s.add(seller)
        await s.commit()
    ends_at = datetime.now(timezone.utc) + timedelta(hours=1)
    async with sm() as s:
        created = await create_auction.fn(
            data=CreateAuctionRequest(title="t", starting_bid=Decimal("10.00"), ends_at=ends_at),
            current_user=seller,
            db_session=s,
        )
    assert created.hard_ends_at - created.ends_at == MAX_EXTENSION


async def test_opening_bid_may_equal_starting_bid(sm):
    _, bidder, aid = await _seed(sm)
    async with sm() as s:
        assert await place_bid_tx(s, aid, bidder, Decimal("10.00")) is not None


async def test_bid_below_starting_bid_rejected(sm):
    _, bidder, aid = await _seed(sm)
    async with sm() as s:
        assert await place_bid_tx(s, aid, bidder, Decimal("9.99")) is None


async def test_bid_equal_to_current_bid_rejected(sm):
    _, bidder, aid = await _seed(sm)
    async with sm() as s:
        await place_bid_tx(s, aid, bidder, Decimal("10.00"))
    async with sm() as s:
        assert await place_bid_tx(s, aid, bidder, Decimal("10.00")) is None


async def test_higher_bid_replaces_the_leader(sm):
    _, bidder, aid = await _seed(sm)
    other = await _add_bidder(sm, "other@x.com")
    async with sm() as s:
        await place_bid_tx(s, aid, bidder, Decimal("10.00"))
    async with sm() as s:
        assert await place_bid_tx(s, aid, other, Decimal("11.00")) is not None
    async with sm() as s:
        auction = await s.get(Auction, aid)
    assert auction.current_bid == Decimal("11.00")
    assert auction.current_winner_id == other
    assert auction.bid_count == 2


async def test_accepted_bid_returns_the_row_it_wrote(sm):
    """The caller is told the state its own bid produced, not whatever the row holds
    by the time a second query gets to it."""
    _, bidder, aid = await _seed(sm)
    async with sm() as s:
        row = await place_bid_tx(s, aid, bidder, Decimal("12.50"))
    assert row.id == aid
    assert row.current_bid == Decimal("12.50")
    assert row.current_winner_id == bidder
    assert row.bid_count == 1
    # The handler renders this same row. hard_ends_at is read straight off it, so a
    # RETURNING that stopped yielding the column would 500 every accepted bid.
    assert _to_response(row).hard_ends_at == row.hard_ends_at.astimezone(APP_TZ)


async def test_rejected_bid_writes_no_history_row(sm):
    _, bidder, aid = await _seed(sm)
    async with sm() as s:
        assert await place_bid_tx(s, aid, bidder, Decimal("9.99")) is None
    assert await _bid_rows(sm, aid) == 0


async def test_seller_cannot_bid_on_their_own_auction(sm):
    """Shill bidding: the seller bidding up their own item. Nothing else about the
    auction is wrong, so only the seller guard can reject this."""
    seller, _, aid = await _seed(sm)
    async with sm() as s:
        assert await place_bid_tx(s, aid, seller, Decimal("10.00")) is None


async def test_bid_on_closed_auction_rejected(sm):
    """The status guard alone: the deadline is still an hour away."""
    _, bidder, aid = await _seed(sm)
    async with sm() as s:
        await s.execute(text("UPDATE auctions SET status = 'closed' WHERE id = :id"), {"id": aid})
        await s.commit()
    async with sm() as s:
        assert await place_bid_tx(s, aid, bidder, Decimal("10.00")) is None


async def test_bid_on_ended_auction_rejected(sm):
    _, bidder, aid = await _seed(sm, ends_delta=timedelta(seconds=-1))  # already past deadline
    async with sm() as s:
        assert await place_bid_tx(s, aid, bidder, Decimal("10.00")) is None


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
        assert await place_bid_tx(s, aid, bidder, Decimal("10.00")) is None


async def test_bid_on_missing_auction_rejected(sm):
    _, bidder, aid = await _seed(sm)
    async with sm() as s:
        assert await place_bid_tx(s, aid + 1000, bidder, Decimal("10.00")) is None


async def test_concurrent_bids_single_winner(sm):
    _, bidder, aid = await _seed(sm)
    other = await _add_bidder(sm, "other@x.com")

    async def bid(uid: int):
        async with sm() as s:
            return await place_bid_tx(s, aid, uid, Decimal("20.00"))

    r1, r2 = await asyncio.gather(bid(bidder), bid(other))
    # equal amounts → exactly one wins, the other can't strictly beat it
    assert [r1, r2].count(None) == 1

    async with sm() as s:
        auction = await s.get(Auction, aid)
    assert auction.current_bid == Decimal("20.00")
    assert auction.bid_count == 1
    assert await _bid_rows(sm, aid) == 1


async def test_highest_bid_survives_n_way_concurrency(sm):
    """The lost-update case: 50 distinct bidders at 50 distinct amounts, all at once.

    Whatever order Postgres grants the row lock in, the largest amount cannot be
    overwritten by a smaller one — the guard re-reads current_bid under the lock.
    """
    _, _, aid = await _seed(sm)
    n = 50
    async with sm() as s:
        users = [User(email=f"b{i}@x.com", pw_hash="x") for i in range(n)]
        s.add_all(users)
        await s.commit()
        ids = [u.id for u in users]
    amounts = [Decimal(f"{11 + i}.00") for i in range(n)]
    order = list(zip(ids, amounts))
    random.shuffle(order)  # so the race is not always "each bid beats the last"

    async def bid(uid: int, amount: Decimal):
        async with sm() as s:
            return await place_bid_tx(s, aid, uid, amount)

    results = await asyncio.gather(*(bid(uid, amount) for uid, amount in order))
    accepted = sum(r is not None for r in results)

    async with sm() as s:
        auction = await s.get(Auction, aid)
    assert auction.current_bid == max(amounts)
    assert auction.current_winner_id == ids[amounts.index(max(amounts))]
    assert auction.bid_count == accepted
    assert await _bid_rows(sm, aid) == accepted


async def _deadline(sm, aid: int) -> datetime:
    async with sm() as s:
        return (await s.get(Auction, aid)).ends_at


async def test_bid_inside_the_final_minute_extends_the_deadline(sm):
    """Anti-snipe. A bid with 30s left moves the deadline to SNIPE_WINDOW from now, so a
    rival who was leading has the same window to answer that the sniper just used."""
    _, bidder, aid = await _seed(sm, ends_delta=timedelta(seconds=30))
    before = await _deadline(sm, aid)
    async with sm() as s:
        assert await place_bid_tx(s, aid, bidder, Decimal("10.00")) is not None
    after = await _deadline(sm, aid)
    assert after > before
    # Measured from when the bid landed, not from the old deadline.
    assert after - datetime.now(timezone.utc) > SNIPE_WINDOW - timedelta(seconds=5)


async def test_bid_outside_the_window_leaves_the_deadline_alone(sm):
    """An hour out, the same statement must not move the deadline at all — otherwise
    every bid on a long auction would drag it in to a minute from now."""
    _, bidder, aid = await _seed(sm, ends_delta=timedelta(hours=1))
    before = await _deadline(sm, aid)
    async with sm() as s:
        assert await place_bid_tx(s, aid, bidder, Decimal("10.00")) is not None
    assert await _deadline(sm, aid) == before


async def test_extension_stops_at_the_hard_ceiling(sm):
    """The ceiling is what makes the auction finite. The allowance has to run out inside
    the window for the clamp to bind: 30s left plus 15s of allowance puts the ceiling 45s
    out, so a bid asking for a 60s extension gets 45s and no more."""
    _, bidder, aid = await _seed(
        sm, ends_delta=timedelta(seconds=30), extension=timedelta(seconds=15)
    )
    async with sm() as s:
        auction = await s.get(Auction, aid)
        ceiling = auction.hard_ends_at
    async with sm() as s:
        assert await place_bid_tx(s, aid, bidder, Decimal("10.00")) is not None
    assert await _deadline(sm, aid) == ceiling


async def test_extension_never_pulls_a_deadline_backwards(sm):
    """A row whose ceiling sits before its own deadline must keep the deadline it has.
    Clamping to the ceiling unconditionally would end such an auction on the next bid."""
    _, bidder, aid = await _seed(
        sm, ends_delta=timedelta(seconds=30), extension=timedelta(seconds=-600)
    )
    before = await _deadline(sm, aid)
    async with sm() as s:
        assert await place_bid_tx(s, aid, bidder, Decimal("10.00")) is not None
    assert await _deadline(sm, aid) == before


async def test_the_bid_and_its_extension_are_one_statement(sm):
    """The deadline has to move in the very UPDATE that accepts the bid.

    Splitting them leaves a window in which the row is bid-on but still due, and the
    closer can fire inside it. That window is too narrow to catch by racing — a
    deliberately split implementation went undetected in 20 out of 20 raced runs — so
    the property is pinned by counting the UPDATEs instead of trying to lose the race.
    """
    updates = []
    engine = sm.kw["bind"].sync_engine

    @event.listens_for(engine, "before_cursor_execute")
    def record(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("UPDATE AUCTIONS"):
            updates.append(statement)

    try:
        _, bidder, aid = await _seed(sm, ends_delta=timedelta(seconds=30))
        async with sm() as s:
            assert await place_bid_tx(s, aid, bidder, Decimal("10.00")) is not None
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert len(updates) == 1, f"the bid path issued {len(updates)} UPDATEs on auctions"


async def _reason(sm, aid: int, uid: int, amount: str = "10.00") -> str:
    async with sm() as s:
        return await reject_reason(s, aid, uid, Decimal(amount))


async def test_reason_names_a_missing_auction(sm):
    _, bidder, aid = await _seed(sm)
    assert await _reason(sm, aid + 1000, bidder) == "auction not found"


async def test_reason_names_a_closed_auction(sm):
    _, bidder, aid = await _seed(sm)
    async with sm() as s:
        await s.execute(text("UPDATE auctions SET status = 'closed' WHERE id = :id"), {"id": aid})
        await s.commit()
    assert await _reason(sm, aid, bidder) == "auction is closed"


async def test_reason_names_an_ended_auction(sm):
    _, bidder, aid = await _seed(sm, ends_delta=timedelta(seconds=-1))
    assert await _reason(sm, aid, bidder) == "auction has ended"


async def test_reason_names_a_self_bid(sm):
    seller, _, aid = await _seed(sm)
    assert await _reason(sm, aid, seller) == "a seller cannot bid on their own auction"


async def test_reason_names_the_starting_bid(sm):
    _, bidder, aid = await _seed(sm)
    assert await _reason(sm, aid, bidder, "9.99") == "bid must be at least 10.00"


async def test_reason_names_the_current_bid(sm):
    _, bidder, aid = await _seed(sm)
    async with sm() as s:
        await place_bid_tx(s, aid, bidder, Decimal("15.00"))
    assert await _reason(sm, aid, bidder, "15.00") == "bid must be above 15.00"


async def test_reason_falls_back_when_the_row_moved_in_flight(sm):
    """Nothing about the auction is wrong by the time the reason is read, which is
    what a bid losing the race to a concurrent higher bid looks like from here."""
    _, bidder, aid = await _seed(sm)
    assert await _reason(sm, aid, bidder) == "outbid while the bid was in flight"


async def test_amount_beyond_the_column_range_rejected(sm):
    """Numeric(12,2) overflows in Postgres; the bid must not reach the statement."""
    _, bidder, aid = await _seed(sm)
    async with sm() as s:
        assert await place_bid_tx(s, aid, bidder, MAX_BID + 1) is None


async def test_amount_below_one_cent_precision_rejected(sm):
    """Postgres rounds a third decimal place, so 10.005 would be stored as 10.01 and
    outbid a 10.00 leader by half a cent it never offered."""
    _, bidder, aid = await _seed(sm)
    async with sm() as s:
        assert await place_bid_tx(s, aid, bidder, Decimal("10.005")) is None


async def test_non_positive_amount_rejected(sm):
    _, bidder, aid = await _seed(sm)
    async with sm() as s:
        assert await place_bid_tx(s, aid, bidder, Decimal("0")) is None
    async with sm() as s:
        assert await place_bid_tx(s, aid, bidder, Decimal("-1.00")) is None


async def test_reason_names_an_out_of_range_amount(sm):
    _, bidder, aid = await _seed(sm)
    reason = await _reason(sm, aid, bidder, "10000000000.00")
    assert reason == "amount must be between 0.01 and 9999999999.99, to at most 2 decimal places"
