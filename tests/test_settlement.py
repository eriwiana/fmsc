"""The four acceptance criteria of M11 — settlement and trust.

Money never touches fmsc, so what happens after the hammer is a record rather than a
transfer. These assert the record: who won and who was next, that both sides can say the
deal completed and only inside the grace period, that a seller can reach the runner-up
without the auction reopening, and that trust is counted from those rows rather than stored
in a number somebody could inflate.

One test per criterion, and nothing else — the behaviour each one leans on is covered in
the commit that adds it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from litestar.exceptions import HTTPException
from sqlalchemy import text

from app.auctions import MAX_EXTENSION, close_due, place_bid_tx
from app.models import Auction, User
from app.settlement import offer_second_chance, reputation, settle

# Three bidders, so there is a winner and a runner-up and the two are plainly different
# people. IDR-scale amounts, each clearing the 10,000 increment the bid statement enforces
# below a million — small round numbers like 25 and 50 are refused, not accepted.
_BIDS = (Decimal("100000.00"), Decimal("200000.00"), Decimal("300000.00"))


async def _seed_closed(sm, channels) -> tuple[int, int, list[int]]:
    """An auction closed by the closer itself, with three bidders. Returns the auction id,
    the seller's id and the bidders in the order they bid.

    Closed by `close_due` rather than by an UPDATE, because criterion 1 is a claim about
    what the closing statement captures — setting the columns by hand would assert nothing.
    """
    async with sm() as s:
        seller = User(email="seller@x.com", pw_hash="x", verified_at=datetime.now(timezone.utc))
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
            bidder = User(email=f"bidder{n}@x.com", pw_hash="x")
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


async def _age_the_close(sm, aid: int, by: timedelta) -> None:
    """Push the recorded close instant into the past. Postgres does the arithmetic, so the
    host clock does not get a vote on which side of the grace period the row is on."""
    async with sm() as s:
        await s.execute(
            text("UPDATE auctions SET closed_at = closed_at - :by WHERE id = :id"),
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
    stale, stale_seller, _ = await _seed_closed(sm, channels)
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
