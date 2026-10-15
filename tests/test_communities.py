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

import pytest
from litestar.exceptions import HTTPException, PermissionDeniedException
from sqlalchemy import text

from app.auctions import (
    _MAY_LIST_SQL,
    MAX_EXTENSION,
    create_auction,
    place_bid_tx,
    transition,
)
from app.communities import _LOCK_COMMUNITY_SQL, kick_member, remove_all_members
from app.models import Auction, Community, CommunityMembership, User
from app.schemas import CreateAuctionRequest


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


async def _auction(
    maker,
    seller_id: int,
    community_id: int | None,
    starts_delta: timedelta = timedelta(0),
) -> int:
    """starts_delta moves starts_at, which is what a kick is measured against: a kick only
    closes the auctions opened after it."""
    ends_at = datetime.now(timezone.utc) + timedelta(hours=1)
    async with maker() as s:
        auction = Auction(
            title="t",
            seller_id=seller_id,
            community_id=community_id,
            currency="IDR",
            starting_bid=Decimal("10.00"),
            starts_at=datetime.now(timezone.utc) + starts_delta,
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


async def test_a_kicked_member_can_still_bid_on_an_auction_opened_before_the_kick(sm):
    """Criterion 4. They were entitled to bid when the lot opened, the seller has relied
    on the bids already placed, and letting a moderator reach back into an open auction
    makes a kick a way to undo other people's bids.
    """
    community_id, owner_id = await _community(sm)
    bidder = await _member(sm, community_id, "member@x.com")
    already_open = await _auction(sm, owner_id, community_id, starts_delta=timedelta(minutes=-5))

    async with sm() as s:
        assert await kick_member(s, community_id, bidder, owner_id=owner_id) is True
    async with sm() as s:
        assert await place_bid_tx(s, already_open, bidder, Decimal("10.00")) is not None


async def test_a_kicked_member_cannot_bid_on_an_auction_opened_after_the_kick(sm):
    """The other half, and the point of the kick. Without this the revocation is a
    comment: the person stays in every future auction the community runs."""
    community_id, owner_id = await _community(sm)
    bidder = await _member(sm, community_id, "member@x.com")

    async with sm() as s:
        assert await kick_member(s, community_id, bidder, owner_id=owner_id) is True
    opened_later = await _auction(sm, owner_id, community_id, starts_delta=timedelta(seconds=1))

    async with sm() as s:
        assert await place_bid_tx(s, opened_later, bidder, Decimal("10.00")) is None


async def test_only_the_owner_can_kick(sm):
    """Moderator rights derive from communities.owner_id, never from a membership row.
    Ownership is in the same WHERE as the revocation, because two statements would let a
    demoted moderator finish a kick they started."""
    community_id, owner_id = await _community(sm)
    bidder = await _member(sm, community_id, "member@x.com")
    pretender = await _member(sm, community_id, "pretender@x.com")

    async with sm() as s:
        assert await kick_member(s, community_id, bidder, owner_id=pretender) is False
    later = await _auction(sm, owner_id, community_id, starts_delta=timedelta(seconds=1))
    async with sm() as s:
        assert await place_bid_tx(s, later, bidder, Decimal("10.00")) is not None


async def test_a_second_kick_does_not_move_the_revocation_forward(sm):
    """The timestamp decides which auctions the person may still bid on, so moving it
    would reach into auctions opened since the first kick. A second kick reports False
    because nothing was revoked, which is already true."""
    community_id, owner_id = await _community(sm)
    bidder = await _member(sm, community_id, "member@x.com")

    async with sm() as s:
        assert await kick_member(s, community_id, bidder, owner_id=owner_id) is True
    async with sm() as s:
        first = await s.scalar(
            text("SELECT revoked_at FROM community_memberships WHERE user_id = :u"),
            {"u": bidder},
        )
    between = await _auction(sm, owner_id, community_id, starts_delta=timedelta(seconds=1))

    async with sm() as s:
        assert await kick_member(s, community_id, bidder, owner_id=owner_id) is False
    async with sm() as s:
        assert (
            await s.scalar(
                text("SELECT revoked_at FROM community_memberships WHERE user_id = :u"),
                {"u": bidder},
            )
            == first
        )
    # And the auction opened after the first kick is still closed to them.
    async with sm() as s:
        assert await place_bid_tx(s, between, bidder, Decimal("10.00")) is None


async def test_remove_all_bumps_the_generation_and_keeps_the_rows(sm):
    """Criterion 3's happy path, and why it is a counter rather than a DELETE: every
    membership stops counting at once, and the rows stay as the record of who was in the
    community when each past lot sold."""
    community_id, owner_id = await _community(sm)
    member = await _member(sm, community_id, "member@x.com")

    async with sm() as s:
        assert await remove_all_members(s, community_id, owner_id=owner_id) == 1
    async with sm() as s:
        assert (
            await s.scalar(
                text("SELECT count(*) FROM community_memberships WHERE community_id = :c"),
                {"c": community_id},
            )
            == 1
        )
    later = await _auction(sm, owner_id, community_id, starts_delta=timedelta(seconds=1))
    async with sm() as s:
        assert await place_bid_tx(s, later, member, Decimal("10.00")) is None


async def test_remove_all_is_refused_while_an_auction_is_open(sm):
    """Criterion 3. The bump would strand the bidders on a lot that is still running:
    they bid under a membership that is suddenly history, and the seller is left with an
    auction nobody may bid on."""
    community_id, owner_id = await _community(sm)
    member = await _member(sm, community_id, "member@x.com")
    open_auction = await _auction(sm, owner_id, community_id)

    async with sm() as s:
        assert await remove_all_members(s, community_id, owner_id=owner_id) is None
    # Nothing moved, so the member can still bid on the lot that is running.
    async with sm() as s:
        assert await place_bid_tx(s, open_auction, member, Decimal("10.00")) is not None


async def test_remove_all_is_allowed_once_the_auction_is_closed(sm):
    """The control. A guard that refused every remove-all would pass the test above."""
    community_id, owner_id = await _community(sm)
    await _member(sm, community_id, "member@x.com")
    auction_id = await _auction(sm, owner_id, community_id)
    async with sm() as s:
        await s.execute(
            text("UPDATE auctions SET status = 'closed' WHERE id = :id"), {"id": auction_id}
        )
        await s.commit()

    async with sm() as s:
        assert await remove_all_members(s, community_id, owner_id=owner_id) == 1


async def test_only_the_owner_can_remove_all(sm):
    """Owner rights come from communities.owner_id. A member who could do this could
    strand every other member."""
    community_id, _ = await _community(sm)
    member = await _member(sm, community_id, "member@x.com")
    async with sm() as s:
        assert await remove_all_members(s, community_id, owner_id=member) is None


async def test_remove_all_waits_for_a_listing_in_flight_and_then_refuses(sm):
    """Criterion 3 under concurrency, built rather than raced.

    This is the interleaving the row lock exists for, and the one a plain NOT EXISTS gets
    wrong: a listing has taken the community row and inserted its auction, but has not
    committed, so that auction is invisible to every other snapshot. A single UPDATE
    carrying NOT EXISTS would bump straight past it, and the new lot would open into a
    community whose every membership had just stopped counting.

    The in-flight listing is spelled out here rather than driven through create_auction,
    because the handler commits and a committed listing is no longer in flight. What it
    does is the same two statements in the same order — FOR SHARE on the community, then
    the insert — and that the handler really takes that lock is pinned separately below.
    """
    community_id, owner_id = await _community(sm)
    member = await _member(sm, community_id, "member@x.com")
    ends_at = datetime.now(timezone.utc) + timedelta(hours=1)

    async with sm() as lister:
        await lister.execute(
            text("SELECT 1 FROM communities WHERE id = :c FOR SHARE"), {"c": community_id}
        )
        auction_id = await lister.scalar(
            text(
                "INSERT INTO auctions (title, seller_id, community_id, starting_bid, currency,"
                " bid_count, status, starts_at, ends_at, hard_ends_at, event_seq,"
                " created_at, updated_at)"
                " VALUES ('t', :uid, :c, 10.00, 'IDR', 0, 'open', now(), :ends, :hard, 0,"
                " now(), now()) RETURNING id"
            ),
            {"uid": owner_id, "c": community_id, "ends": ends_at, "hard": ends_at},
        )

        async def remove():
            async with sm() as s:
                return await remove_all_members(s, community_id, owner_id=owner_id)

        removal = asyncio.create_task(remove())
        # Long enough to reach the lock and block on it. Were it not blocking it would
        # have finished by now, which is what this reads.
        await asyncio.sleep(0.2)
        assert not removal.done(), "remove-all did not wait for the listing's lock"

        await lister.commit()

    assert await removal is None, "remove-all bumped past an auction it could not see"

    async with sm() as s:
        assert (
            await s.scalar(
                text("SELECT membership_generation FROM communities WHERE id = :c"),
                {"c": community_id},
            )
            == 0
        )
    # And the member is still a member, so the lot that just opened is biddable.
    async with sm() as s:
        assert await place_bid_tx(s, auction_id, member, Decimal("10.00")) is not None


def test_listing_into_a_community_takes_the_row_lock():
    """What makes the test above about this code rather than about Postgres.

    A lock only helps where both sides take it. remove_all_members takes the community row
    FOR UPDATE; create_auction has to take the same row, and FOR SHARE rather than FOR
    UPDATE so two sellers listing at once do not queue behind each other — they only queue
    behind a remove-all. There is no way to observe it behaviourally without splitting the
    handler's commit, so it is read off the statement.
    """
    assert "FOR SHARE" in str(_MAY_LIST_SQL)
    assert "FOR UPDATE" in str(_LOCK_COMMUNITY_SQL)


async def test_remove_all_and_create_auction_agree_whichever_lands_first(sm):
    """The race as well, asserted as a consistency property. Whichever order the scheduler
    picks, a listing that got in must have been listed after the bump or else the bump
    must not have happened — never a lot opened before a bump that went through anyway.
    """
    community_id, owner_id = await _community(sm)
    await _member(sm, community_id, "member@x.com")
    ends_at = datetime.now(timezone.utc) + timedelta(hours=1)

    async def remove():
        async with sm() as s:
            return await remove_all_members(s, community_id, owner_id=owner_id)

    async def create():
        async with sm() as s:
            owner = await s.get(User, owner_id)
            try:
                return await create_auction.fn(
                    data=CreateAuctionRequest(
                        title="t",
                        starting_bid=Decimal("10.00"),
                        ends_at=ends_at,
                        community_id=community_id,
                    ),
                    current_user=owner,
                    db_session=s,
                )
            except HTTPException:
                return None

    removed, created = await asyncio.gather(remove(), create())

    async with sm() as s:
        row = (
            await s.execute(
                text(
                    "SELECT c.membership_generation AS generation, c.updated_at AS bumped,"
                    " (SELECT min(a.created_at) FROM auctions a"
                    "   WHERE a.community_id = c.id) AS listed"
                    " FROM communities c WHERE c.id = :c"
                ),
                {"c": community_id},
            )
        ).one()

    assert (removed is None) != (row.generation == 1)
    if created is not None and row.generation == 1:
        # The forbidden state is a lot that was already open when the bump landed.
        assert row.listed >= row.bumped


async def test_a_stranger_cannot_list_an_auction_into_a_community(sm):
    """Not a criterion, and not optional: without it any verified seller could plant a lot
    inside somebody else's community by guessing an id, and its members would be gated
    into bidding on a stranger's auction."""
    community_id, _ = await _community(sm)
    async with sm() as s:
        outsider = User(email="outsider@x.com", pw_hash="x", verified_at=datetime.now(timezone.utc))
        s.add(outsider)
        await s.commit()

    with pytest.raises(PermissionDeniedException):
        async with sm() as s:
            await create_auction.fn(
                data=CreateAuctionRequest(
                    title="t",
                    starting_bid=Decimal("10.00"),
                    ends_at=datetime.now(timezone.utc) + timedelta(hours=1),
                    community_id=community_id,
                ),
                current_user=outsider,
                db_session=s,
            )


async def test_a_member_can_list_an_auction_into_their_community(sm):
    """The control, and the shape a community actually runs on: members list, members
    bid."""
    community_id, _ = await _community(sm)
    async with sm() as s:
        seller = User(email="seller@x.com", pw_hash="x", verified_at=datetime.now(timezone.utc))
        s.add(seller)
        await s.flush()
        s.add(CommunityMembership(community_id=community_id, user_id=seller.id, generation=0))
        await s.commit()
        seller_id = seller.id

    async with sm() as s:
        seller = await s.get(User, seller_id)
        created = await create_auction.fn(
            data=CreateAuctionRequest(
                title="t",
                starting_bid=Decimal("10.00"),
                ends_at=datetime.now(timezone.utc) + timedelta(hours=1),
                community_id=community_id,
            ),
            current_user=seller,
            db_session=s,
        )
    async with sm() as s:
        assert (
            await s.scalar(
                text("SELECT community_id FROM auctions WHERE id = :id"), {"id": created.id}
            )
            == community_id
        )


async def test_publishing_an_auction_starts_it_then(sm):
    """starts_at is what a kick is measured against, so it has to be the moment the lot
    was announced rather than the moment its draft was first saved.

    Left at insert time, a member kicked on Tuesday could still bid on a lot drafted
    Monday and published Wednesday — the kick reaching backwards past its own date.
    """
    community_id, owner_id = await _community(sm)
    member = await _member(sm, community_id, "member@x.com")
    draft = await _auction(sm, owner_id, community_id, starts_delta=timedelta(hours=-48))
    async with sm() as s:
        await s.execute(text("UPDATE auctions SET status = 'draft' WHERE id = :id"), {"id": draft})
        await s.commit()

    async with sm() as s:
        assert await kick_member(s, community_id, member, owner_id=owner_id) is True
    async with sm() as s:
        assert await transition(s, draft, "open", seller_id=owner_id) is not None

    # Published after the kick, so it is one of the auctions the kick closes to them.
    async with sm() as s:
        assert await place_bid_tx(s, draft, member, Decimal("10.00")) is None
    async with sm() as s:
        started_after_the_kick = await s.scalar(
            text(
                "SELECT a.starts_at > m.revoked_at FROM auctions a, community_memberships m"
                " WHERE a.id = :a AND m.user_id = :u"
            ),
            {"a": draft, "u": member},
        )
    assert started_after_the_kick is True


async def test_remove_all_is_refused_while_a_draft_is_waiting(sm):
    """A draft counts as an auction still open, because nothing stops its seller
    publishing it a second after the bump and publish takes no community lock.

    Without this the generation moves out from under a lot that is about to open: it
    opens, every membership is a generation behind, and nobody may bid on it — which is
    exactly the state remove_all_members exists to prevent.
    """
    community_id, owner_id = await _community(sm)
    member = await _member(sm, community_id, "member@x.com")
    draft = await _auction(sm, owner_id, community_id)
    async with sm() as s:
        await s.execute(text("UPDATE auctions SET status = 'draft' WHERE id = :id"), {"id": draft})
        await s.commit()

    async with sm() as s:
        assert await remove_all_members(s, community_id, owner_id=owner_id) is None

    # Published after the refusal, it is biddable by the member who was there all along.
    async with sm() as s:
        row = await transition(s, draft, "open", seller_id=owner_id)
    assert row is not None
    async with sm() as s:
        assert await place_bid_tx(s, draft, member, Decimal("10.00")) is not None


async def test_a_cancelled_or_closed_auction_does_not_block_remove_all(sm):
    """The control. Neither can take a bid again, so neither is somebody's commitment to
    strand — and a guard that counted them would make remove-all impossible for any
    community that had ever run an auction."""
    community_id, owner_id = await _community(sm)
    await _member(sm, community_id, "member@x.com")
    for status in ("cancelled", "closed"):
        auction_id = await _auction(sm, owner_id, community_id)
        async with sm() as s:
            await s.execute(
                text("UPDATE auctions SET status = :s WHERE id = :id"),
                {"s": status, "id": auction_id},
            )
            await s.commit()

    async with sm() as s:
        assert await remove_all_members(s, community_id, owner_id=owner_id) == 1
