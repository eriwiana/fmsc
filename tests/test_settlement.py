"""The four acceptance criteria of M11 — settlement and trust.

Money never touches fmsc, so what happens after the hammer is a record rather than a
transfer. These assert the record: who won and who was next, that both sides can say the
deal completed and only inside the grace period, that a seller can reach the runner-up
without the auction reopening, and that trust is counted from those rows rather than stored
in a number somebody could inflate.

The four acceptance tests come first, one per criterion. The behaviour each of them leans
on is pinned below it, in the commit that added that behaviour.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from litestar.exceptions import HTTPException
from sqlalchemy import text

from app.auctions import MAX_EXTENSION, close_due, place_bid_tx
from app.models import Auction, User
from app.settlement import SETTLEMENT_GRACE, offer_second_chance, reputation, settle

# Three bidders, so there is a winner and a runner-up and the two are plainly different
# people. IDR-scale amounts, each clearing the 10,000 increment the bid statement enforces
# below a million — small round numbers like 25 and 50 are refused, not accepted.
_BIDS = (Decimal("100000.00"), Decimal("200000.00"), Decimal("300000.00"))


async def _seed_closed(sm, channels, tag: str = "") -> tuple[int, int, list[int]]:
    """An auction closed by the closer itself, with three bidders. Returns the auction id,
    the seller's id and the bidders in the order they bid.

    `tag` distinguishes the users, because emails are unique and a test that needs a second
    closed auction beside the first would otherwise collide on the seller.

    Closed by `close_due` rather than by an UPDATE, because criterion 1 is a claim about
    what the closing statement captures — setting the columns by hand would assert nothing.
    """
    async with sm() as s:
        seller = User(
            email=f"seller{tag}@x.com", pw_hash="x", verified_at=datetime.now(timezone.utc)
        )
        s.add(seller)
        await s.flush()
        ends_at = datetime.now(timezone.utc) + timedelta(hours=1)
        auction = Auction(
            title="three bidders",
            seller_id=seller.id,
            currency="IDR",
            starting_bid=Decimal("10000.00"),
            status="open",
            ends_at=ends_at,
            hard_ends_at=ends_at + MAX_EXTENSION,
        )
        s.add(auction)
        await s.flush()
        aid, seller_id = auction.id, seller.id

        bidders = []
        for n in range(len(_BIDS)):
            bidder = User(email=f"bidder{tag}{n}@x.com", pw_hash="x")
            s.add(bidder)
            await s.flush()
            bidders.append(bidder.id)
        await s.commit()

    for uid, amount in zip(bidders, _BIDS, strict=True):
        async with sm() as s:
            assert await place_bid_tx(s, aid, uid, amount) is not None

    async with sm() as s:
        await s.execute(
            text("UPDATE auctions SET ends_at = now() - interval '5 seconds' WHERE id = :id"),
            {"id": aid},
        )
        await s.commit()
    assert await close_due(sm, channels) == 1
    return aid, seller_id, bidders


async def _expire(sm, aid: int) -> None:
    """Move a deadline into the past in Postgres, not from the host clock.

    close_due compares ends_at against clock_timestamp(), so a deadline computed here is
    measured against a clock that can be seconds behind this machine's — on the Docker VM
    the suite runs against, far enough behind that a lot seeded five seconds ago is not yet
    due and the closer correctly finds nothing.
    """
    async with sm() as s:
        await s.execute(
            text("UPDATE auctions SET ends_at = now() - interval '5 seconds' WHERE id = :id"),
            {"id": aid},
        )
        await s.commit()


async def _age_the_close(sm, aid: int, by: timedelta) -> None:
    """Push the recorded close instant into the past. Postgres does the arithmetic, so the
    host clock does not get a vote on which side of the grace period the row is on."""
    async with sm() as s:
        await s.execute(
            # CAST, because an untyped parameter lets Postgres read `closed_at - $1` as
            # timestamptz minus timestamptz, which is an interval and not a close instant.
            text(
                "UPDATE auctions SET closed_at = closed_at - CAST(:by AS interval) WHERE id = :id"
            ),
            {"by": by, "id": aid},
        )
        await s.commit()


async def test_closing_captures_the_winner_and_the_runner_up(sm, channels):
    """Criterion 1: both ids are on the row, put there by the closing UPDATE."""
    aid, _, bidders = await _seed_closed(sm, channels)

    async with sm() as s:
        row = await s.get(Auction, aid)

    assert row.current_winner_id == bidders[-1], "the highest bidder won"
    assert row.runner_up_id == bidders[-2], "the next highest is the runner-up"
    assert row.closed_at is not None, "the closing statement recorded when it closed"


async def test_a_settlement_past_the_grace_period_is_refused(sm, channels):
    """Criterion 2: both parties can mark settled, and the window is enforced by query."""
    aid, seller_id, bidders = await _seed_closed(sm, channels)
    winner_id = bidders[-1]

    async with sm() as s:
        marked = await settle.fn(
            auction_id=aid, current_user=await s.get(User, seller_id), db_session=s
        )
    assert marked.seller_settled is True
    assert marked.settled is False, "one side is not a settlement"

    async with sm() as s:
        marked = await settle.fn(
            auction_id=aid, current_user=await s.get(User, winner_id), db_session=s
        )
    assert marked.settled is True, "both sides marked it"

    # A second auction, left to go stale, is the one the grace period has to refuse.
    stale, stale_seller, _ = await _seed_closed(sm, channels, tag="-stale")
    await _age_the_close(sm, stale, timedelta(days=8))
    async with sm() as s:
        with pytest.raises(HTTPException) as refused:
            await settle.fn(
                auction_id=stale, current_user=await s.get(User, stale_seller), db_session=s
            )
    assert refused.value.status_code == 409


async def test_a_second_chance_offer_does_not_reopen_the_auction(sm, channels):
    """Criterion 3: the runner-up is promoted, the auction stays shut."""
    aid, seller_id, bidders = await _seed_closed(sm, channels)

    async with sm() as s:
        offer = await offer_second_chance.fn(
            auction_id=aid, current_user=await s.get(User, seller_id), db_session=s
        )

    assert offer.offered_to_id == bidders[-2], "the offer records the runner-up"
    async with sm() as s:
        assert (await s.get(Auction, aid)).status == "closed", "offering did not reopen it"


async def test_reputation_is_recomputed_from_the_records(sm, channels):
    """Criterion 4: the counts follow the rows, because they are not stored anywhere."""
    aid, seller_id, bidders = await _seed_closed(sm, channels)
    winner_id = bidders[-1]

    async with sm() as s:
        assert (await reputation(s, seller_id)).as_seller_settled == 0

    for actor in (seller_id, winner_id):
        async with sm() as s:
            await settle.fn(auction_id=aid, current_user=await s.get(User, actor), db_session=s)

    async with sm() as s:
        assert (await reputation(s, seller_id)).as_seller_settled == 1
        assert (await reputation(s, winner_id)).as_buyer_settled == 1

    # Take the buyer's mark away and the counts must follow it down. A stored counter would
    # not, which is the whole point of the criterion.
    async with sm() as s:
        await s.execute(
            text("UPDATE settlements SET buyer_settled_at = NULL WHERE auction_id = :id"),
            {"id": aid},
        )
        await s.commit()

    async with sm() as s:
        assert (await reputation(s, seller_id)).as_seller_settled == 0
        assert (await reputation(s, winner_id)).as_buyer_settled == 0


# --- criterion 2: the settle route in detail ---------------------------------------------


async def test_a_stranger_cannot_settle_somebody_elses_deal(sm, channels):
    aid, _, _ = await _seed_closed(sm, channels)
    async with sm() as s:
        stranger = User(email="stranger@x.com", pw_hash="x")
        s.add(stranger)
        await s.commit()
        stranger_id = stranger.id

    async with sm() as s:
        with pytest.raises(HTTPException) as refused:
            await settle.fn(
                auction_id=aid, current_user=await s.get(User, stranger_id), db_session=s
            )

    assert refused.value.status_code == 409
    # The same answer an auction that does not exist gets. Telling them apart would let
    # anyone enumerate which auctions closed with a winner.
    assert "not a party" in refused.value.detail


async def test_settling_twice_keeps_the_first_mark(sm, channels):
    """Idempotent, so a double-tapped button is not a second, later settlement."""
    aid, seller_id, _ = await _seed_closed(sm, channels)

    async def mark(uid):
        async with sm() as s:
            return await settle.fn(
                auction_id=aid, current_user=await s.get(User, uid), db_session=s
            )

    await mark(seller_id)
    async with sm() as s:
        first = await s.scalar(
            text("SELECT seller_settled_at FROM settlements WHERE auction_id = :id"), {"id": aid}
        )
    await mark(seller_id)
    async with sm() as s:
        again = await s.scalar(
            text("SELECT seller_settled_at FROM settlements WHERE auction_id = :id"), {"id": aid}
        )

    assert again == first


async def test_one_side_marking_does_not_erase_the_other(sm, channels):
    """Why the upsert COALESCEs instead of assigning. EXCLUDED carries NULL for whichever
    column the caller is not filling, so plain assignment would have each side wipe the
    other's mark and the deal would never read as settled."""
    aid, seller_id, bidders = await _seed_closed(sm, channels)

    for uid in (bidders[-1], seller_id):
        async with sm() as s:
            marked = await settle.fn(
                auction_id=aid, current_user=await s.get(User, uid), db_session=s
            )

    assert marked.buyer_settled is True, "the buyer's earlier mark survived"
    assert marked.seller_settled is True
    assert marked.settled is True


async def test_an_auction_still_open_cannot_be_settled(sm, channels):
    """The seed closes one auction; this is a second one left open beside it.

    It has a bid, deliberately. Without one the auction has no current_winner_id and the
    statement would refuse it for having nobody to settle with, which would leave the
    status guard itself untested — a mutation pass caught exactly that.
    """
    await _seed_closed(sm, channels)
    async with sm() as s:
        seller = User(
            email="open-seller@x.com", pw_hash="x", verified_at=datetime.now(timezone.utc)
        )
        bidder = User(email="open-bidder@x.com", pw_hash="x")
        s.add_all([seller, bidder])
        await s.flush()
        ends_at = datetime.now(timezone.utc) + timedelta(hours=1)
        auction = Auction(
            title="still running",
            seller_id=seller.id,
            currency="IDR",
            starting_bid=Decimal("10000.00"),
            status="open",
            ends_at=ends_at,
            hard_ends_at=ends_at + MAX_EXTENSION,
        )
        s.add(auction)
        await s.commit()
        aid, seller_id, bidder_id = auction.id, seller.id, bidder.id
    async with sm() as s:
        assert await place_bid_tx(s, aid, bidder_id, Decimal("100000.00")) is not None

    async with sm() as s:
        with pytest.raises(HTTPException) as refused:
            await settle.fn(auction_id=aid, current_user=await s.get(User, seller_id), db_session=s)

    assert refused.value.status_code == 409
    assert "not closed" in refused.value.detail


async def test_an_auction_that_closed_with_no_winner_has_nothing_to_settle(sm, channels):
    async with sm() as s:
        seller = User(email="lonely@x.com", pw_hash="x", verified_at=datetime.now(timezone.utc))
        s.add(seller)
        await s.flush()
        ends_at = datetime.now(timezone.utc) + timedelta(hours=1)
        auction = Auction(
            title="nobody bid",
            seller_id=seller.id,
            currency="IDR",
            starting_bid=Decimal("10000.00"),
            status="open",
            ends_at=ends_at,
            hard_ends_at=ends_at + MAX_EXTENSION,
        )
        s.add(auction)
        await s.commit()
        aid, seller_id = auction.id, seller.id
    await _expire(sm, aid)
    assert await close_due(sm, channels) == 1

    async with sm() as s:
        with pytest.raises(HTTPException) as refused:
            await settle.fn(auction_id=aid, current_user=await s.get(User, seller_id), db_session=s)

    assert refused.value.status_code == 409
    assert "no winner" in refused.value.detail


async def test_a_settlement_on_the_last_day_of_the_grace_period_is_accepted(sm, channels):
    """The control for the refusal in criterion 2. Without it, a guard that refused
    everything would pass that test."""
    aid, seller_id, _ = await _seed_closed(sm, channels)
    await _age_the_close(sm, aid, SETTLEMENT_GRACE - timedelta(hours=1))

    async with sm() as s:
        marked = await settle.fn(
            auction_id=aid, current_user=await s.get(User, seller_id), db_session=s
        )

    assert marked.seller_settled is True


async def test_settling_an_auction_that_does_not_exist_is_a_404(sm, channels):
    _, seller_id, _ = await _seed_closed(sm, channels)

    async with sm() as s:
        with pytest.raises(HTTPException) as refused:
            await settle.fn(
                auction_id=9_999_999, current_user=await s.get(User, seller_id), db_session=s
            )

    assert refused.value.status_code == 404


# --- criterion 3: the second-chance offer in detail --------------------------------------


async def test_the_offer_is_priced_at_the_runner_ups_own_bid(sm, channels):
    """Not the winning price. The runner-up never offered more than their own last bid, so
    it is the only number they can be held to without being asked again."""
    aid, seller_id, _ = await _seed_closed(sm, channels)

    async with sm() as s:
        offer = await offer_second_chance.fn(
            auction_id=aid, current_user=await s.get(User, seller_id), db_session=s
        )

    assert offer.amount == _BIDS[-2]


async def test_offering_twice_is_the_same_offer(sm, channels):
    """ON CONFLICT DO NOTHING, then read the row back: a second click answers with the
    offer that already exists rather than refusing or making another."""
    aid, seller_id, bidders = await _seed_closed(sm, channels)

    async def offer():
        async with sm() as s:
            return await offer_second_chance.fn(
                auction_id=aid, current_user=await s.get(User, seller_id), db_session=s
            )

    first, again = await offer(), await offer()

    assert (first.auction_id, first.offered_to_id) == (again.auction_id, bidders[-2])
    async with sm() as s:
        assert (
            await s.scalar(
                text("SELECT count(*) FROM second_chance_offers WHERE auction_id = :id"),
                {"id": aid},
            )
            == 1
        )


async def test_only_the_seller_may_offer_a_second_chance(sm, channels):
    aid, _, bidders = await _seed_closed(sm, channels)

    async with sm() as s:
        with pytest.raises(HTTPException) as refused:
            await offer_second_chance.fn(
                auction_id=aid, current_user=await s.get(User, bidders[-1]), db_session=s
            )

    assert refused.value.status_code == 409
    assert "not its seller" in refused.value.detail


async def test_an_auction_with_no_runner_up_cannot_be_offered_on(sm, channels):
    """One bidder, so there is nobody second. Includes every auction that closed before the
    runner-up was recorded — deciding from bids who was second after the fact is exactly
    the deferred query the closing statement exists to avoid."""
    async with sm() as s:
        seller = User(email="solo@x.com", pw_hash="x", verified_at=datetime.now(timezone.utc))
        bidder = User(email="solo-bidder@x.com", pw_hash="x")
        s.add_all([seller, bidder])
        await s.flush()
        ends_at = datetime.now(timezone.utc) + timedelta(hours=1)
        auction = Auction(
            title="one bidder",
            seller_id=seller.id,
            currency="IDR",
            starting_bid=Decimal("10000.00"),
            status="open",
            ends_at=ends_at,
            hard_ends_at=ends_at + MAX_EXTENSION,
        )
        s.add(auction)
        await s.commit()
        aid, seller_id, bidder_id = auction.id, seller.id, bidder.id
    async with sm() as s:
        assert await place_bid_tx(s, aid, bidder_id, Decimal("100000.00")) is not None
    async with sm() as s:
        await s.execute(
            text("UPDATE auctions SET ends_at = now() - interval '5 seconds' WHERE id = :id"),
            {"id": aid},
        )
        await s.commit()
    assert await close_due(sm, channels) == 1

    async with sm() as s:
        with pytest.raises(HTTPException) as refused:
            await offer_second_chance.fn(
                auction_id=aid, current_user=await s.get(User, seller_id), db_session=s
            )

    assert refused.value.status_code == 409
    assert "no runner-up" in refused.value.detail


async def test_an_offer_past_the_grace_period_is_refused(sm, channels):
    """The same window as settling, and for a sharper reason: past it the runner-up could
    not mark the deal done anyway, so all the offer would still do is move who the buyer of
    record is — clearing the record of a winner who never paid and planting one on somebody
    who was never asked while there was time to answer."""
    aid, seller_id, bidders = await _seed_closed(sm, channels)
    await _age_the_close(sm, aid, SETTLEMENT_GRACE + timedelta(hours=1))

    async with sm() as s:
        with pytest.raises(HTTPException) as refused:
            await offer_second_chance.fn(
                auction_id=aid, current_user=await s.get(User, seller_id), db_session=s
            )

    assert refused.value.status_code == 409
    assert "grace period" in refused.value.detail
    async with sm() as s:
        assert (await reputation(s, bidders[-1])).as_buyer_unsettled == 1, "still the buyer"


async def test_an_offer_is_refused_once_the_deal_is_on_record(sm, channels):
    """A settlement row names the buyer it was created for and keeps that name. An offer
    landing afterwards would leave the record claiming one person and the auction implying
    another, with the later mark of whoever is now the buyer of record silently discarded by
    the upsert's COALESCE."""
    aid, seller_id, bidders = await _seed_closed(sm, channels)
    winner, runner_up = bidders[-1], bidders[-2]

    async with sm() as s:
        await settle.fn(auction_id=aid, current_user=await s.get(User, winner), db_session=s)

    async with sm() as s:
        with pytest.raises(HTTPException) as refused:
            await offer_second_chance.fn(
                auction_id=aid, current_user=await s.get(User, seller_id), db_session=s
            )

    assert refused.value.status_code == 409
    assert "settlement record" in refused.value.detail
    async with sm() as s:
        marked = await settle.fn(
            auction_id=aid, current_user=await s.get(User, seller_id), db_session=s
        )
        assert (marked.buyer_id, marked.settled) == (winner, True)
        with pytest.raises(HTTPException):
            await settle.fn(auction_id=aid, current_user=await s.get(User, runner_up), db_session=s)


async def test_the_runner_up_settles_once_the_offer_is_made(sm, channels):
    """What the offer is for. The buyer of record moves to the runner-up, so they are the
    one who can mark the deal done — and the original winner no longer can."""
    aid, seller_id, bidders = await _seed_closed(sm, channels)
    winner, runner_up = bidders[-1], bidders[-2]

    async with sm() as s:
        await offer_second_chance.fn(
            auction_id=aid, current_user=await s.get(User, seller_id), db_session=s
        )

    async with sm() as s:
        marked = await settle.fn(
            auction_id=aid, current_user=await s.get(User, runner_up), db_session=s
        )
    assert marked.buyer_id == runner_up
    assert marked.buyer_settled is True

    async with sm() as s:
        with pytest.raises(HTTPException) as refused:
            await settle.fn(auction_id=aid, current_user=await s.get(User, winner), db_session=s)
    assert refused.value.status_code == 409, "the winner who went quiet is no longer the buyer"


async def test_offering_a_second_chance_on_an_auction_that_does_not_exist_is_a_404(sm, channels):
    _, seller_id, _ = await _seed_closed(sm, channels)

    async with sm() as s:
        with pytest.raises(HTTPException) as refused:
            await offer_second_chance.fn(
                auction_id=9_999_999, current_user=await s.get(User, seller_id), db_session=s
            )

    assert refused.value.status_code == 404


async def test_an_auction_still_open_cannot_be_offered_on(sm, channels):
    """The runner-up is only known once the closing statement has run, so there is nothing
    to offer while the lot is still live."""
    await _seed_closed(sm, channels)
    async with sm() as s:
        seller = User(
            email="live-seller@x.com", pw_hash="x", verified_at=datetime.now(timezone.utc)
        )
        s.add(seller)
        await s.flush()
        ends_at = datetime.now(timezone.utc) + timedelta(hours=1)
        auction = Auction(
            title="live",
            seller_id=seller.id,
            currency="IDR",
            starting_bid=Decimal("10000.00"),
            status="open",
            ends_at=ends_at,
            hard_ends_at=ends_at + MAX_EXTENSION,
        )
        s.add(auction)
        await s.commit()
        aid, seller_id = auction.id, seller.id

    async with sm() as s:
        with pytest.raises(HTTPException) as refused:
            await offer_second_chance.fn(
                auction_id=aid, current_user=await s.get(User, seller_id), db_session=s
            )

    assert refused.value.status_code == 409
    assert "not closed" in refused.value.detail


# --- criterion 4: reputation in detail ----------------------------------------------------


async def test_nobody_with_no_closed_deals_has_any_record(sm, channels):
    aid, _, _ = await _seed_closed(sm, channels)
    async with sm() as s:
        nobody = User(email="nobody@x.com", pw_hash="x")
        s.add(nobody)
        await s.commit()
        nobody_id = nobody.id

    async with sm() as s:
        counts = await reputation(s, nobody_id)

    assert counts == (0, 0, 0, 0)


async def test_a_deal_nobody_marked_counts_against_both_once_the_window_goes(sm, channels):
    """A side is unsettled when the window has gone and no mark of theirs is in the row,
    which is why it is counted against the same window the settle route enforces. Both are
    counted here, because nothing in these rows says which of the two let it lapse."""
    aid, seller_id, bidders = await _seed_closed(sm, channels)

    # Inside the window nobody is behind on anything: the deal is merely still pending.
    async with sm() as s:
        assert (await reputation(s, seller_id)).as_seller_unsettled == 0
        assert (await reputation(s, bidders[-1])).as_buyer_unsettled == 0

    await _age_the_close(sm, aid, SETTLEMENT_GRACE + timedelta(hours=1))

    async with sm() as s:
        assert (await reputation(s, seller_id)).as_seller_unsettled == 1
        assert (await reputation(s, bidders[-1])).as_buyer_unsettled == 1


async def test_only_the_side_that_never_marked_is_counted(sm, channels):
    """The asymmetry that makes the record worth keeping: a seller who did their part is
    not tarred by a buyer who did not."""
    aid, seller_id, bidders = await _seed_closed(sm, channels)
    winner = bidders[-1]

    async with sm() as s:
        await settle.fn(auction_id=aid, current_user=await s.get(User, seller_id), db_session=s)
    await _age_the_close(sm, aid, SETTLEMENT_GRACE + timedelta(hours=1))

    async with sm() as s:
        seller_counts = await reputation(s, seller_id)
        buyer_counts = await reputation(s, winner)

    assert (seller_counts.as_seller_settled, seller_counts.as_seller_unsettled) == (0, 0)
    assert buyer_counts.as_buyer_unsettled == 1


async def test_an_auction_that_closed_with_no_winner_counts_for_nobody(sm, channels):
    """There is no deal, so there is nothing anyone failed to do."""
    async with sm() as s:
        seller = User(email="unsold@x.com", pw_hash="x", verified_at=datetime.now(timezone.utc))
        s.add(seller)
        await s.flush()
        ends_at = datetime.now(timezone.utc) + timedelta(hours=1)
        auction = Auction(
            title="unsold",
            seller_id=seller.id,
            currency="IDR",
            starting_bid=Decimal("10000.00"),
            status="open",
            ends_at=ends_at,
            hard_ends_at=ends_at + MAX_EXTENSION,
        )
        s.add(auction)
        await s.commit()
        aid, seller_id = auction.id, seller.id
    await _expire(sm, aid)
    assert await close_due(sm, channels) == 1
    await _age_the_close(sm, aid, SETTLEMENT_GRACE + timedelta(hours=1))

    async with sm() as s:
        assert await reputation(s, seller_id) == (0, 0, 0, 0)


async def test_the_runner_up_carries_the_record_once_the_offer_is_made(sm, channels):
    """The offer moves who the deal is with, so it moves whose record it lands on."""
    aid, seller_id, bidders = await _seed_closed(sm, channels)
    winner, runner_up = bidders[-1], bidders[-2]

    async with sm() as s:
        await offer_second_chance.fn(
            auction_id=aid, current_user=await s.get(User, seller_id), db_session=s
        )
    await _age_the_close(sm, aid, SETTLEMENT_GRACE + timedelta(hours=1))

    async with sm() as s:
        assert (await reputation(s, runner_up)).as_buyer_unsettled == 1
        assert await reputation(s, winner) == (0, 0, 0, 0), "no longer the buyer of record"
