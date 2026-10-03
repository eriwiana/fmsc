"""The one critical check: the atomic bid money path (`place_bid_tx`).

Tests the real SQL the handler uses, with truly concurrent sessions — the HTTP layer is
thin glue and is smoke-tested manually (see README). Fixtures live in conftest.py.
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from litestar.exceptions import ClientException, HTTPException
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError

from app.auctions import (
    APP_TZ,
    IDEMPOTENCY_HEADER,
    MAX_BID,
    MAX_EXTENSION,
    _to_response,
    create_auction,
    place_bid,
    place_bid_tx,
    reject_reason,
    resolve_deadline,
)
from app.models import Auction, User
from app.schemas import BidRequest, CreateAuctionRequest


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
    # The literal 2h, not MAX_EXTENSION: comparing the constant with itself passes for any
    # value, so a ceiling quietly widened to 48h would go unnoticed.
    assert created.hard_ends_at - created.ends_at == timedelta(hours=2)


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
    # -5s, not -1s: ends_at is computed here and compared against Postgres'
    # clock_timestamp(), so a one-second margin is close enough to the boundary for the
    # two clocks to disagree and the bid to be accepted. That is the likeliest
    # explanation for this test failing once, unreproducibly, during M4.
    _, bidder, aid = await _seed(sm, ends_delta=timedelta(seconds=-5))
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
    # strict: the two lists are built from the same n, and a silent truncation here
    # would quietly shrink the race this test exists to create.
    order = list(zip(ids, amounts, strict=True))
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
    # Literal bounds, not SNIPE_WINDOW: asserting against the constant compares it with
    # itself, so any value it held would pass. Measured from when the bid landed, not from
    # the old deadline. Both sides matter — a lower bound alone accepts a 10-minute window.
    assert timedelta(seconds=55) < after - datetime.now(timezone.utc) < timedelta(seconds=65)


async def test_bid_event_tells_watchers_the_new_deadline(sm, channels):
    """Anti-snipe is invisible without this. A watcher whose countdown still shows the old
    deadline stops bidding at a deadline that has already moved, which is the behaviour the
    extension exists to prevent."""
    _, bidder, aid = await _seed(sm, ends_delta=timedelta(seconds=30))
    before = await _deadline(sm, aid)
    response = await _post_bid(sm, aid, bidder, "10.00", None, channels)

    ((channel, event),) = channels.published
    assert channel == f"auction:{aid}"
    assert event["type"] == "bid"
    # The same rendering the response uses, so a client needs no second request.
    assert event["ends_at"] == response.ends_at.isoformat()
    assert datetime.fromisoformat(event["ends_at"]) > before


async def _post_bid(sm, aid: int, uid: int, amount: str, key: str | None, channels):
    async with sm() as s:
        return await place_bid.fn(
            auction_id=aid,
            data=BidRequest(amount=Decimal(amount)),
            # Only `headers` is read by the handler; litestar's Request is not needed.
            request=SimpleNamespace(headers={} if key is None else {IDEMPOTENCY_HEADER: key}),
            current_user=await s.get(User, uid),
            db_session=s,
            channels=channels,
        )


async def test_every_event_on_an_auction_is_numbered_in_order(sm, channels):
    """M5 criterion 2. A client cannot tell a missed bid from a quiet auction unless the
    events are numbered: three bids give three consecutive numbers, so dropping the middle
    one leaves a hole visible from the numbers alone, with no other state to compare."""
    _, bidder, aid = await _seed(sm)
    second = await _add_bidder(sm, "second@x.com")
    third = await _add_bidder(sm, "third@x.com")

    for uid, amount in ((bidder, "10.00"), (second, "11.00"), (third, "12.00")):
        await _post_bid(sm, aid, uid, amount, None, channels)

    assert [event["seq"] for _, event in channels.published] == [1, 2, 3]

    # What a client actually does with them: the middle event never arrives, and the jump
    # from 1 to 3 is the whole signal that something was missed.
    delivered = [channels.published[0][1]["seq"], channels.published[2][1]["seq"]]
    assert delivered[1] - delivered[0] > 1


async def test_a_retry_replays_rather_than_being_told_it_was_outbid(sm, channels):
    """M4 criterion 1 at the handler, sequentially — what a client does after a timeout.

    No race: the first bid is committed before the retry starts, so the retry's UPDATE is
    refused by `:amount > current_bid` and the replay is the only thing that can answer it.
    """
    _, bidder, aid = await _seed(sm)

    first = await _post_bid(sm, aid, bidder, "10.00", "retry", channels)
    second = await _post_bid(sm, aid, bidder, "10.00", "retry", channels)

    assert second == first
    assert await _bid_rows(sm, aid) == 1
    assert len(channels.published) == 1


async def test_two_requests_with_one_key_place_one_bid(sm, channels):
    """M4 criterion 2. Two retries arriving together — a client that resent before the
    first reply landed. The unique constraint decides; an application-level "have I seen
    this key" check would let both through."""
    _, bidder, aid = await _seed(sm)

    first, second = await asyncio.gather(
        _post_bid(sm, aid, bidder, "12.00", "same-key", channels),
        _post_bid(sm, aid, bidder, "12.00", "same-key", channels),
    )

    assert first == second
    assert await _bid_rows(sm, aid) == 1
    # The replay must not publish again, or every watcher counts the bid twice.
    assert len(channels.published) == 1


async def test_an_over_long_key_is_refused_not_a_500(sm, channels):
    """The key is client input at a trust boundary. Longer than the column and Postgres
    raises StringDataRightTruncation, which is a DataError and not an IntegrityError, so
    nothing downstream catches it and the bidder gets a 500."""
    _, bidder, aid = await _seed(sm)
    with pytest.raises(ClientException) as refused:
        await _post_bid(sm, aid, bidder, "10.00", "k" * 129, channels)
    assert "128" in refused.value.detail
    assert await _bid_rows(sm, aid) == 0


async def test_an_empty_key_counts_as_no_key(sm, channels):
    """`Idempotency-Key:` with nothing after it is a client sending the header by accident,
    not a client asking for deduplication under the key "". Treating it as a real key makes
    the next such bid replay instead of bidding."""
    _, bidder, aid = await _seed(sm)

    await _post_bid(sm, aid, bidder, "10.00", "", channels)
    second = await _post_bid(sm, aid, bidder, "11.00", "", channels)

    assert second.bid_count == 2
    assert await _bid_rows(sm, aid) == 2


async def test_a_key_is_not_consumed_by_a_rejected_bid(sm, channels):
    """M4 criterion 3. The key and the response are written in the bid's own transaction,
    so a refusal rolls both back and the client may reuse the key for a real bid."""
    _, bidder, aid = await _seed(sm)

    with pytest.raises(ClientException):
        await _post_bid(sm, aid, bidder, "9.99", "reused", channels)

    accepted = await _post_bid(sm, aid, bidder, "10.00", "reused", channels)
    assert accepted.current_bid == Decimal("10.00")
    assert await _bid_rows(sm, aid) == 1


async def test_a_different_key_places_a_second_bid(sm, channels):
    """M4 criterion 4. Idempotency must not collapse two genuine bids from one bidder."""
    _, bidder, aid = await _seed(sm)

    await _post_bid(sm, aid, bidder, "10.00", "first-key", channels)
    second = await _post_bid(sm, aid, bidder, "11.00", "second-key", channels)

    assert second.bid_count == 2
    assert await _bid_rows(sm, aid) == 2


async def test_the_unique_key_alone_stops_a_second_bid(sm):
    """Straight at place_bid_tx, past the handler's replay check: the constraint is what
    actually prevents the duplicate, and nothing above it should be trusted to. A second
    bid on the same key is reported as a refusal so the caller replays the first answer."""
    _, bidder, aid = await _seed(sm)
    async with sm() as s:
        assert await place_bid_tx(s, aid, bidder, Decimal("10.00"), "dup") is not None
    async with sm() as s:
        assert await place_bid_tx(s, aid, bidder, Decimal("11.00"), "dup") is None
    assert await _bid_rows(sm, aid) == 1


async def test_an_unrelated_integrity_failure_is_not_reported_as_a_refusal(sm):
    """A bid for a user that does not exist must raise, not come back as a refusal.

    This one fails on the UPDATE's own foreign key, before the commit, so it does not
    reach the re-raise inside place_bid_tx — see the comment there. What it does pin is
    that no integrity failure on the money path is quietly turned into "refused".
    """
    _, _, aid = await _seed(sm)
    with pytest.raises(IntegrityError):
        async with sm() as s:
            await place_bid_tx(s, aid, 999_999, Decimal("10.00"), "a-key")


async def test_one_key_with_a_changed_amount_is_a_conflict(sm, channels):
    """Same key, different body — a client with a key-reuse bug. No second bid is placed
    either way, so this is only about whether the client finds out. It does: replaying the
    first answer silently would let it believe it placed a bid it never placed.

    Sequential, not raced: the first bid is committed, so the second clears the bid guard
    (11 > 10) and the stored amount is the only thing that can tell them apart.
    """
    _, bidder, aid = await _seed(sm)

    await _post_bid(sm, aid, bidder, "10.00", "one-key", channels)
    with pytest.raises(HTTPException) as conflict:
        await _post_bid(sm, aid, bidder, "11.00", "one-key", channels)

    assert conflict.value.status_code == 409
    assert await _bid_rows(sm, aid) == 1
    assert len(channels.published) == 1


async def test_a_rejected_bid_says_why_and_publishes_nothing(sm, channels):
    """The handler's reject path. The bidder is told which guard refused the bid, and no
    event goes out — a rejected bid that published would move every watcher's countdown
    and show a leader who does not exist."""
    _, bidder, aid = await _seed(sm, ends_delta=timedelta(seconds=30))
    before = await _deadline(sm, aid)

    with pytest.raises(ClientException) as rejected:
        await _post_bid(sm, aid, bidder, "9.99", None, channels)

    assert rejected.value.detail == "bid must be at least 10.00"
    assert channels.published == []
    # The deadline is untouched too: a refused bid must not buy the bidder more time.
    assert await _deadline(sm, aid) == before


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
    _, bidder, aid = await _seed(sm, ends_delta=timedelta(seconds=-5))
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
