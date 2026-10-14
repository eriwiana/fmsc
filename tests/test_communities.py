"""M9 acceptance: bidding is gated on membership, inside the bid statement.

Criterion 1. Viewing stays public; only bidding is gated, and the check belongs in the
same UPDATE that accepts the bid. A handler that read the membership and then bid would
be two statements, and the gap between them is where a revocation lands — so the test
that matters is the one where the membership goes away after the bid's transaction has
already opened.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import text

from app.auctions import MAX_EXTENSION, place_bid_tx
from app.models import Auction, Community, CommunityMembership, User


async def _community(maker, owner_email: str = "owner@x.com") -> tuple[int, int]:
    """A community and its owner. Owner rights come from communities.owner_id and never
    from a membership row — removing every member must not lock the owner out."""
    async with maker() as s:
        owner = User(email=owner_email, pw_hash="x", verified_at=datetime.now(timezone.utc))
        s.add(owner)
        await s.flush()
        community = Community(name="c", owner_id=owner.id)
        s.add(community)
        await s.commit()
        return community.id, owner.id


async def _member(maker, community_id: int, email: str, generation: int = 0) -> int:
    async with maker() as s:
        user = User(email=email, pw_hash="x")
        s.add(user)
        await s.flush()
        s.add(
            CommunityMembership(community_id=community_id, user_id=user.id, generation=generation)
        )
        await s.commit()
        return user.id


async def _stranger(maker, email: str) -> int:
    async with maker() as s:
        user = User(email=email, pw_hash="x")
        s.add(user)
        await s.commit()
        return user.id


async def _auction(maker, seller_id: int, community_id: int | None) -> int:
    ends_at = datetime.now(timezone.utc) + timedelta(hours=1)
    async with maker() as s:
        auction = Auction(
            title="t",
            seller_id=seller_id,
            community_id=community_id,
            currency="IDR",
            starting_bid=Decimal("10.00"),
            ends_at=ends_at,
            hard_ends_at=ends_at + MAX_EXTENSION,
        )
        s.add(auction)
        await s.commit()
        return auction.id


async def test_a_member_can_bid_on_their_communitys_auction(sm):
    """The control for everything below. A guard that refused everyone would pass every
    refusal test in this file."""
    community_id, owner_id = await _community(sm)
    bidder = await _member(sm, community_id, "member@x.com")
    auction_id = await _auction(sm, owner_id, community_id)
    async with sm() as s:
        assert await place_bid_tx(s, auction_id, bidder, Decimal("10.00")) is not None


async def test_a_stranger_cannot_bid_on_a_community_auction(sm):
    """Criterion 1. The lot is the community's; somebody who never joined has no claim on
    it, and the refusal has to come from the statement rather than from a handler.

    The community has a member who is not the bidder, on purpose. Against an empty
    community the EXISTS finds nothing whatever it is keyed on, so dropping the
    `m.user_id = :uid` from the guard — one member letting the whole internet bid — would
    pass a test that did not seed one.
    """
    community_id, owner_id = await _community(sm)
    await _member(sm, community_id, "member@x.com")
    outsider = await _stranger(sm, "outsider@x.com")
    auction_id = await _auction(sm, owner_id, community_id)
    async with sm() as s:
        assert await place_bid_tx(s, auction_id, outsider, Decimal("10.00")) is None
    async with sm() as s:
        assert (
            await s.scalar(
                text("SELECT bid_count FROM auctions WHERE id = :id"), {"id": auction_id}
            )
            == 0
        )


async def test_an_auction_with_no_community_is_open_to_anyone(sm):
    """community_id IS NULL is not a special case anywhere: a public auction behaves
    exactly as it did before communities existed."""
    _, owner_id = await _community(sm)
    outsider = await _stranger(sm, "outsider@x.com")
    auction_id = await _auction(sm, owner_id, None)
    async with sm() as s:
        assert await place_bid_tx(s, auction_id, outsider, Decimal("10.00")) is not None


async def test_a_membership_from_an_older_generation_does_not_count(sm):
    """Remove-all bumps communities.membership_generation rather than deleting rows, so
    the audit trail survives. A row from the generation before the bump is history, not
    a membership."""
    community_id, owner_id = await _community(sm)
    stale = await _member(sm, community_id, "stale@x.com", generation=0)
    auction_id = await _auction(sm, owner_id, community_id)
    async with sm() as s:
        await s.execute(
            text("UPDATE communities SET membership_generation = 1 WHERE id = :id"),
            {"id": community_id},
        )
        await s.commit()
    async with sm() as s:
        assert await place_bid_tx(s, auction_id, stale, Decimal("10.00")) is None


async def test_a_generation_bump_after_the_bids_transaction_opened_still_refuses(sm):
    """Criterion 1's real content: the check is *in* the UPDATE, not before it.

    The bid's transaction opens and reads its snapshot. The generation is then bumped and
    committed by somebody else. A handler that had already checked the membership would go
    on to bid; the UPDATE re-reads under its own lock and refuses.

    This is the same shape as the stale-transaction-clock test on the deadline, and it is
    deliberately not a race: a test that has to win a race proves nothing when it loses.
    """
    community_id, owner_id = await _community(sm)
    bidder = await _member(sm, community_id, "member@x.com")
    auction_id = await _auction(sm, owner_id, community_id)

    async with sm() as bid_session:
        # Opens the transaction and freezes its snapshot, with the membership still valid.
        assert (
            await bid_session.scalar(
                text(
                    "SELECT count(*) FROM community_memberships"
                    " WHERE community_id = :c AND user_id = :u"
                ),
                {"c": community_id, "u": bidder},
            )
            == 1
        )
        async with sm() as remover:
            await remover.execute(
                text("UPDATE communities SET membership_generation = 1 WHERE id = :id"),
                {"id": community_id},
            )
            await remover.commit()
        assert await place_bid_tx(bid_session, auction_id, bidder, Decimal("10.00")) is None


async def test_a_bid_and_a_removal_racing_cannot_both_win(sm):
    """Criterion 1 says a concurrency test, so here is the race as well as the ordering
    above. Whichever lands first, the end state has to agree with itself: a bid that was
    accepted means the generation it was checked against was current at the time.
    """
    community_id, owner_id = await _community(sm)
    bidder = await _member(sm, community_id, "member@x.com")
    auction_id = await _auction(sm, owner_id, community_id)

    async def bid():
        async with sm() as s:
            return await place_bid_tx(s, auction_id, bidder, Decimal("10.00"))

    async def remove_all():
        async with sm() as s:
            await s.execute(
                text("UPDATE communities SET membership_generation = 1 WHERE id = :id"),
                {"id": community_id},
            )
            await s.commit()

    accepted, _ = await asyncio.gather(bid(), remove_all())

    async with sm() as s:
        bid_count = await s.scalar(
            text("SELECT bid_count FROM auctions WHERE id = :id"), {"id": auction_id}
        )
    # Either the bid got in under the old generation or it did not get in at all. What
    # must never happen is a stored bid from a user the community no longer has.
    assert bid_count == (1 if accepted is not None else 0)
    if accepted is None:
        async with sm() as s:
            assert (
                await s.scalar(
                    text("SELECT count(*) FROM bids WHERE auction_id = :id"),
                    {"id": auction_id},
                )
                == 0
            )
