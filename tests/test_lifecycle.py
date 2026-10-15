"""M8 acceptance: the status is a state machine, and ownership guards the edges.

Criteria 1 and 2:

1. Every illegal status transition is refused by a guarded UPDATE, per transition. The
   states are draft, open, closed and cancelled, and the only legal edges are
   draft->open, draft->cancelled, open->closed and open->cancelled. Refusal means the
   statement matched no row, not that a Python branch raised: `update`, a data migration
   and psql all bypass a Python branch, and the test drives the statement directly.
2. Cancel is refused once a bid exists, and edit is refused after the first bid. A
   cancelled auction with bids on it is a seller walking away from money other people
   committed, and an edited one is a bait-and-switch: bidders bid on the description.
5. The seller dashboard returns only that seller's auctions.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from litestar.exceptions import ClientException, HTTPException, NotFoundException
from sqlalchemy import text

from app.auctions import (
    LEGAL_FROM,
    STATUSES,
    cancel_auction,
    create_auction,
    edit_auction,
    get_auction,
    list_auctions,
    my_auctions,
    publish_auction,
    transition,
)
from app.models import User
from app.schemas import CreateAuctionRequest, EditAuctionRequest


async def _seller(maker, email: str = "seller@x.com") -> User:
    async with maker() as s:
        user = User(email=email, pw_hash="x", verified_at=datetime.now(timezone.utc))
        s.add(user)
        await s.commit()
        return user


async def _auction(
    maker,
    seller_id: int,
    status: str,
    bid_count: int = 0,
    ends_delta: timedelta = timedelta(hours=1),
) -> int:
    """Raw INSERT, naming every column, because the ORM would supply the very defaults
    under test — and because a draft is not reachable through create_auction's own path
    once the status is anything but open."""
    ends_at = datetime.now(timezone.utc) + ends_delta
    async with maker() as s:
        auction_id = (
            await s.execute(
                text(
                    "INSERT INTO auctions (title, seller_id, starting_bid, currency, bid_count,"
                    " status, starts_at, ends_at, hard_ends_at, event_seq, created_at, updated_at)"
                    " VALUES ('t', :uid, 10.00, 'IDR', :bids, :status, now(), :ends_at, :hard, 0,"
                    " now(), now()) RETURNING id"
                ),
                {
                    "uid": seller_id,
                    "bids": bid_count,
                    "status": status,
                    "ends_at": ends_at,
                    "hard": ends_at + timedelta(hours=2),
                },
            )
        ).scalar()
        await s.commit()
        return auction_id


async def _status(maker, auction_id: int) -> str:
    async with maker() as s:
        return await s.scalar(
            text("SELECT status FROM auctions WHERE id = :id"), {"id": auction_id}
        )


# Spelled out here, not derived from LEGAL_FROM. Reading the expectation out of the table
# under test makes the test agree with whatever the table says: adding "closed" to
# cancelled's sources would widen the expectation along with the code and every case would
# still pass. These four are the specification; the assertion below is what ties the two
# together, so a deliberate change to the machine has to be made twice and an accidental
# one fails.
LEGAL_EDGES = frozenset(
    {("draft", "open"), ("draft", "cancelled"), ("open", "closed"), ("open", "cancelled")}
)
EDGES = [(a, b) for a in STATUSES for b in STATUSES if a != b]


def test_the_table_of_edges_is_the_one_the_tests_describe():
    """LEGAL_FROM is what the statement reads; LEGAL_EDGES is what these tests assert. An
    edge added to one and not the other is a change nobody wrote a case for."""
    assert {(start, to) for to, froms in LEGAL_FROM.items() for start in froms} == LEGAL_EDGES
    assert set(STATUSES) == {"draft", "open", "closed", "cancelled"}


@pytest.mark.parametrize(("start", "target"), EDGES)
async def test_only_the_legal_edges_move_an_auction(sm, start, target):
    """Criterion 1, one case per edge. Twelve ordered pairs, four of them legal.

    A single parametrised test rather than twelve written out: the table of edges is the
    specification, and a transition added to the code but not to the table fails here
    without anyone remembering to write a new test.
    """
    seller = await _seller(sm)
    auction_id = await _auction(sm, seller.id, start)
    legal = (start, target) in LEGAL_EDGES

    async with sm() as s:
        row = await transition(s, auction_id, target, seller_id=seller.id)

    assert (row is not None) == legal, (
        f"{start} -> {target} should be {'legal' if legal else 'refused'}"
    )
    assert await _status(sm, auction_id) == (target if legal else start)


@pytest.mark.parametrize("state", STATUSES)
async def test_an_auction_cannot_transition_to_its_own_state(sm, state):
    """A no-op is not an edge. Allowing it would bump event_seq and announce a change
    that did not happen, and it is what a double-clicked publish button sends."""
    seller = await _seller(sm)
    auction_id = await _auction(sm, seller.id, state)
    async with sm() as s:
        assert await transition(s, auction_id, state, seller_id=seller.id) is None


async def test_only_the_seller_may_move_their_own_auction(sm):
    """The edge is legal; the caller is not the seller. Ownership is in the same WHERE as
    the status, because two statements would leave a window between the check and the
    move."""
    seller = await _seller(sm)
    stranger = await _seller(sm, "stranger@x.com")
    auction_id = await _auction(sm, seller.id, "draft")
    async with sm() as s:
        assert await transition(s, auction_id, "open", seller_id=stranger.id) is None
    assert await _status(sm, auction_id) == "draft"


async def test_cancel_is_refused_once_a_bid_exists(sm):
    """Criterion 2. A bid is somebody else's commitment; withdrawing the lot after it
    arrives is the seller taking the option and leaving the bidder with nothing."""
    seller = await _seller(sm)
    auction_id = await _auction(sm, seller.id, "open", bid_count=1)
    async with sm() as s:
        assert await transition(s, auction_id, "cancelled", seller_id=seller.id) is None
    assert await _status(sm, auction_id) == "open"


async def test_cancel_is_allowed_while_no_bid_exists(sm):
    """The control. A guard that refused every cancel would pass the test above."""
    seller = await _seller(sm)
    auction_id = await _auction(sm, seller.id, "open")
    async with sm() as s:
        assert await transition(s, auction_id, "cancelled", seller_id=seller.id) is not None
    assert await _status(sm, auction_id) == "cancelled"


async def test_edit_is_refused_after_the_first_bid(sm):
    """Criterion 2, the other half. Bidders bid on the description; changing it under
    them is a bait-and-switch, and changing starting_bid after a bid would let a seller
    invalidate the bid that had already been accepted."""
    seller = await _seller(sm)
    auction_id = await _auction(sm, seller.id, "open", bid_count=1)
    async with sm() as s:
        with pytest.raises(HTTPException) as refused:
            await edit_auction.fn(
                auction_id=auction_id,
                data=EditAuctionRequest(title="something else"),
                current_user=seller,
                db_session=s,
            )
    assert refused.value.status_code == 409
    async with sm() as s:
        assert (
            await s.scalar(text("SELECT title FROM auctions WHERE id = :id"), {"id": auction_id})
            == "t"
        )


async def test_edit_is_allowed_while_no_bid_exists(sm):
    """The control, and the reason edit exists: a seller fixing a typo before anyone has
    committed anything is not changing the deal."""
    seller = await _seller(sm)
    auction_id = await _auction(sm, seller.id, "open")
    async with sm() as s:
        edited = await edit_auction.fn(
            auction_id=auction_id,
            data=EditAuctionRequest(title="a better title", starting_bid=Decimal("25.00")),
            current_user=seller,
            db_session=s,
        )
    assert edited.title == "a better title"
    assert edited.starting_bid == Decimal("25.00")


async def test_a_closed_auction_cannot_be_edited(sm):
    """No bids and still refused: a closed auction has a result, and rewriting the
    description of a lot that already sold rewrites what the winner agreed to."""
    seller = await _seller(sm)
    auction_id = await _auction(sm, seller.id, "closed")
    async with sm() as s:
        with pytest.raises(HTTPException):
            await edit_auction.fn(
                auction_id=auction_id,
                data=EditAuctionRequest(title="after the fact"),
                current_user=seller,
                db_session=s,
            )


async def test_only_the_seller_may_edit_their_own_auction(sm):
    """Ownership is in the same WHERE as the bid count, for the same reason as the
    transition: a separate check leaves a window."""
    seller = await _seller(sm)
    stranger = await _seller(sm, "stranger@x.com")
    auction_id = await _auction(sm, seller.id, "open")
    async with sm() as s:
        with pytest.raises(HTTPException):
            await edit_auction.fn(
                auction_id=auction_id,
                data=EditAuctionRequest(title="mine now"),
                current_user=stranger,
                db_session=s,
            )


async def test_an_edit_that_moves_the_deadline_moves_the_ceiling_with_it(sm):
    """hard_ends_at >= ends_at is a check constraint, so an edit that pushed the deadline
    past the old ceiling would be refused by the table as a 500. An edit that leaves the
    deadline alone must leave the ceiling alone too, or every title fix quietly buys the
    auction another two hours of extensions."""
    seller = await _seller(sm)
    auction_id = await _auction(sm, seller.id, "open")
    async with sm() as s:
        before = (
            await s.execute(
                text("SELECT ends_at, hard_ends_at FROM auctions WHERE id = :id"),
                {"id": auction_id},
            )
        ).one()

    async with sm() as s:
        await edit_auction.fn(
            auction_id=auction_id,
            data=EditAuctionRequest(title="just the title"),
            current_user=seller,
            db_session=s,
        )
    async with sm() as s:
        untouched = (
            await s.execute(
                text("SELECT ends_at, hard_ends_at FROM auctions WHERE id = :id"),
                {"id": auction_id},
            )
        ).one()
    assert untouched == before

    later = datetime.now(timezone.utc) + timedelta(hours=5)
    async with sm() as s:
        moved = await edit_auction.fn(
            auction_id=auction_id,
            data=EditAuctionRequest(ends_at=later),
            current_user=seller,
            db_session=s,
        )
    assert moved.hard_ends_at > moved.ends_at


async def test_an_edit_cannot_put_the_deadline_in_the_past(sm):
    """The same rule create_auction applies. Without it a seller could close an auction
    early by editing it, which is a cancel that dodges the no-bids rule."""
    seller = await _seller(sm)
    auction_id = await _auction(sm, seller.id, "open")
    async with sm() as s:
        with pytest.raises(ClientException):
            await edit_auction.fn(
                auction_id=auction_id,
                data=EditAuctionRequest(ends_at=datetime.now(timezone.utc) - timedelta(hours=1)),
                current_user=seller,
                db_session=s,
            )


async def test_a_draft_is_absent_from_the_public_list_and_by_id(sm):
    """A draft is the state before anybody has seen the listing, so a public endpoint
    showing one makes the draft pointless. 404 and not 403 by id: that route takes no
    credentials, and a 403 would confirm the id belongs to something."""
    seller = await _seller(sm)
    draft = await _auction(sm, seller.id, "draft")
    public = await _auction(sm, seller.id, "open")

    async with sm() as s:
        assert [a.id for a in await list_auctions.fn(db_session=s)] == [public]

    async with sm() as s:
        with pytest.raises(NotFoundException):
            await get_auction.fn(auction_id=draft, db_session=s)
    async with sm() as s:
        assert (await get_auction.fn(auction_id=public, db_session=s)).id == public


async def test_a_cancelled_auction_stays_public(sm):
    """Deliberately not hidden. Somebody was watching it; they need to see why bidding
    stopped, and a listing that vanishes looks like a bug or like a sale behind their
    back."""
    seller = await _seller(sm)
    cancelled = await _auction(sm, seller.id, "cancelled")
    async with sm() as s:
        assert [a.id for a in await list_auctions.fn(db_session=s)] == [cancelled]
    async with sm() as s:
        assert (await get_auction.fn(auction_id=cancelled, db_session=s)).status == "cancelled"


async def test_the_dashboard_shows_only_the_callers_own_auctions(sm):
    """Criterion 5. Another seller's auction is absent, which is the whole point: a
    dashboard that leaks is worse than no dashboard, because it is trusted."""
    mine = await _seller(sm, "mine@x.com")
    theirs = await _seller(sm, "theirs@x.com")
    ours = [await _auction(sm, mine.id, "open"), await _auction(sm, mine.id, "draft")]
    not_ours = await _auction(sm, theirs.id, "open")

    async with sm() as s:
        listed = [a.id for a in await my_auctions.fn(current_user=mine, db_session=s)]
    assert sorted(listed) == sorted(ours)
    assert not_ours not in listed

    # And the other way round, or a filter that returned nothing would pass the above.
    async with sm() as s:
        assert [a.id for a in await my_auctions.fn(current_user=theirs, db_session=s)] == [not_ours]


async def test_the_dashboard_is_where_a_seller_sees_their_drafts_and_reserve(sm):
    """The one place both appear. A draft is hidden from every public route, and the
    reserve is withheld from everyone else, so without this the seller cannot read back
    either of the two things only they set."""
    seller = await _seller(sm)
    async with sm() as s:
        await s.execute(
            text(
                "INSERT INTO auctions (title, seller_id, starting_bid, reserve, currency,"
                " bid_count, status, starts_at, ends_at, hard_ends_at, event_seq,"
                " created_at, updated_at)"
                " VALUES ('t', :uid, 10.00, 100.00, 'IDR', 0, 'draft', now(),"
                " now() + interval '1 hour', now() + interval '3 hours', 0, now(), now())"
            ),
            {"uid": seller.id},
        )
        await s.commit()

    async with sm() as s:
        only = (await my_auctions.fn(current_user=seller, db_session=s))[0]
    assert only.status == "draft"
    assert only.reserve == Decimal("100.00")


async def test_an_auction_cannot_be_created_into_a_state_an_edge_leads_to(sm):
    """open and draft and nothing else. closed and cancelled are states the machine
    reaches by an edge, and a POST that landed straight in one would be an auction with a
    result nobody bid on."""
    seller = await _seller(sm)
    ends_at = datetime.now(timezone.utc) + timedelta(hours=1)
    for bad in ("closed", "cancelled", "scheduled", ""):
        with pytest.raises(ClientException):
            async with sm() as s:
                await create_auction.fn(
                    data=CreateAuctionRequest(
                        title="t", starting_bid=Decimal("10.00"), ends_at=ends_at, status=bad
                    ),
                    current_user=seller,
                    db_session=s,
                )


async def test_an_edit_cannot_make_the_starting_bid_zero_or_negative(sm):
    """The same rule create_auction applies, and the table's own constraint behind it:
    without the check here a 0 would arrive as the 500 an IntegrityError becomes."""
    seller = await _seller(sm)
    auction_id = await _auction(sm, seller.id, "open")
    for bad in (Decimal("0"), Decimal("-1.00")):
        async with sm() as s:
            with pytest.raises(ClientException):
                await edit_auction.fn(
                    auction_id=auction_id,
                    data=EditAuctionRequest(starting_bid=bad),
                    current_user=seller,
                    db_session=s,
                )


async def test_publish_and_cancel_answer_409_when_the_edge_is_not_available(sm, channels):
    """The handlers' own refusal, not the statement's. 409 rather than 404 or 403: all
    three reasons — not yours, wrong state, already has bids — answer the same, because a
    caller cannot act on the difference and telling them apart says which auctions exist.
    """
    seller = await _seller(sm)
    published = await _auction(sm, seller.id, "open")
    with_bids = await _auction(sm, seller.id, "open", bid_count=1)

    async with sm() as s:
        with pytest.raises(HTTPException) as refused:
            await publish_auction.fn(
                auction_id=published, current_user=seller, db_session=s, channels=channels
            )
    assert refused.value.status_code == 409

    async with sm() as s:
        with pytest.raises(HTTPException) as refused:
            await cancel_auction.fn(
                auction_id=with_bids, current_user=seller, db_session=s, channels=channels
            )
    assert refused.value.status_code == 409


async def test_publish_and_cancel_hand_back_the_auction_they_moved(sm, channels):
    """The controls. A handler that raised on every call would pass the test above, and
    the response is what tells a client the new status without a second request."""
    seller = await _seller(sm)
    draft = await _auction(sm, seller.id, "draft")
    async with sm() as s:
        assert (
            await publish_auction.fn(
                auction_id=draft, current_user=seller, db_session=s, channels=channels
            )
        ).status == "open"
    async with sm() as s:
        assert (
            await cancel_auction.fn(
                auction_id=draft, current_user=seller, db_session=s, channels=channels
            )
        ).status == "cancelled"


async def test_a_stale_draft_cannot_be_published(sm):
    """A draft sat on past its own deadline would open and then be closed by the very
    next closer tick — an auction that was never biddable, announced to everyone watching
    and then immediately resolved. The deadline is in the same WHERE as the edge."""
    seller = await _seller(sm)
    stale = await _auction(sm, seller.id, "draft", ends_delta=timedelta(seconds=-1))
    async with sm() as s:
        assert await transition(s, stale, "open", seller_id=seller.id) is None
    assert await _status(sm, stale) == "draft"

    # The control: the same edge on a draft whose deadline is still ahead.
    fresh = await _auction(sm, seller.id, "draft")
    async with sm() as s:
        assert await transition(s, fresh, "open", seller_id=seller.id) is not None


async def test_a_move_is_announced_with_the_sequence_number_it_consumed(sm):
    """event_seq counts events *published* about an auction, and the socket closes a
    subscriber that sees a number skipped. A bump with no event would tell a watcher it
    had missed something that never existed — and cancelling would tell nobody at all,
    since the closer skips a cancelled auction and no later event ever arrives.
    """
    seller = await _seller(sm)
    auction_id = await _auction(sm, seller.id, "draft")

    async with sm() as s:
        await transition(s, auction_id, "open", seller_id=seller.id)
    async with sm() as s:
        await transition(s, auction_id, "cancelled", seller_id=seller.id)

    async with sm() as s:
        rows = (
            await s.execute(
                text("SELECT seq, payload FROM outbox WHERE auction_id = :id ORDER BY seq"),
                {"id": auction_id},
            )
        ).all()
    assert [r.seq for r in rows] == [1, 2]
    assert [json.loads(r.payload)["status"] for r in rows] == ["open", "cancelled"]
    assert [json.loads(r.payload)["type"] for r in rows] == ["status", "status"]
    # One event per bump, so there is no hole for a watcher to trip over.
    async with sm() as s:
        assert await s.scalar(
            text("SELECT event_seq FROM auctions WHERE id = :id"), {"id": auction_id}
        ) == len(rows)


async def _created(maker, seller, status: str = "open"):
    """Through the real handler, because what is under test is what the handler writes."""
    async with maker() as s:
        return await create_auction.fn(
            data=CreateAuctionRequest(
                title="t",
                starting_bid=Decimal("10.00"),
                ends_at=datetime.now(timezone.utc) + timedelta(hours=1),
                status=status,
            ),
            current_user=seller,
            db_session=s,
        )


async def _announced(maker, auction_id: int) -> tuple[list, int]:
    async with maker() as s:
        rows = (
            await s.execute(
                text("SELECT seq, payload FROM outbox WHERE auction_id = :id ORDER BY seq"),
                {"id": auction_id},
            )
        ).all()
        seq = await s.scalar(
            text("SELECT event_seq FROM auctions WHERE id = :id"), {"id": auction_id}
        )
    return rows, seq


async def test_an_auction_created_open_announces_that_it_is_open(sm):
    """Publishing a draft announces it, and creating one open has to as well.

    Measured before it did: the create path wrote no outbox row at all, so anything
    following an auction heard its first bid without ever having been told the lot
    existed — and open is the default, so that was most of them.
    """
    seller = await _seller(sm)
    created = await _created(sm, seller)

    rows, seq = await _announced(sm, created.id)
    assert [row.seq for row in rows] == [1]
    event = json.loads(rows[0].payload)
    assert event["type"] == "status"
    assert event["status"] == "open"
    # One event per bump, so the first bid is seq 2 and no watcher sees a hole.
    assert seq == 1


async def test_a_draft_announces_nothing(sm):
    """A draft is hidden from the listing, from the socket and from the outbox alike. An
    event for one would describe a lot every read path answers 404 for."""
    seller = await _seller(sm)
    created = await _created(sm, seller, status="draft")

    rows, seq = await _announced(sm, created.id)
    assert rows == []
    assert seq == 0


async def test_a_refused_move_announces_nothing(sm):
    """The rollback has to take the outbox row with it, or a refused cancel would still
    tell every watcher the lot was withdrawn."""
    seller = await _seller(sm)
    auction_id = await _auction(sm, seller.id, "open", bid_count=1)
    async with sm() as s:
        assert await transition(s, auction_id, "cancelled", seller_id=seller.id) is None
    async with sm() as s:
        assert (
            await s.scalar(
                text("SELECT count(*) FROM outbox WHERE auction_id = :id"), {"id": auction_id}
            )
            == 0
        )


async def test_a_reserve_under_the_starting_bid_is_a_400_and_not_a_500(sm):
    """ck_auctions_reserve_at_least_starting refuses the pair, and an IntegrityError
    reaching the client is a 500 with nothing in it they can act on. Three ways in: at
    creation, by raising the starting bid, and by lowering the reserve."""
    seller = await _seller(sm)
    ends_at = datetime.now(timezone.utc) + timedelta(hours=1)
    with pytest.raises(ClientException):
        async with sm() as s:
            await create_auction.fn(
                data=CreateAuctionRequest(
                    title="t",
                    starting_bid=Decimal("100.00"),
                    ends_at=ends_at,
                    reserve=Decimal("50.00"),
                ),
                current_user=seller,
                db_session=s,
            )

    async with sm() as s:
        created = await create_auction.fn(
            data=CreateAuctionRequest(
                title="t",
                starting_bid=Decimal("10.00"),
                ends_at=ends_at,
                reserve=Decimal("100.00"),
            ),
            current_user=seller,
            db_session=s,
        )
    for bad in (
        EditAuctionRequest(starting_bid=Decimal("500.00")),
        EditAuctionRequest(reserve=Decimal("1.00")),
    ):
        async with sm() as s:
            with pytest.raises(ClientException):
                await edit_auction.fn(
                    auction_id=created.id, data=bad, current_user=seller, db_session=s
                )

    # The pair moved together is fine, which is what makes the refusals above about the
    # combination rather than about either field.
    async with sm() as s:
        edited = await edit_auction.fn(
            auction_id=created.id,
            data=EditAuctionRequest(starting_bid=Decimal("500.00"), reserve=Decimal("600.00")),
            current_user=seller,
            db_session=s,
        )
    assert edited.reserve == Decimal("600.00")


async def test_a_move_is_published_and_not_left_to_the_relay(sm, channels):
    """The handler announces the move itself and marks the row sent; the relay is the
    safety net, not the route.

    Without this the suite cannot tell the two apart. An unsent outbox row is picked up by
    the next relay tick and the watcher still hears it, just a tick late — which is how a
    rebase quietly dropped both publish call sites with the whole suite green.
    """
    seller = await _seller(sm)
    draft = await _auction(sm, seller.id, "draft")
    async with sm() as s:
        await publish_auction.fn(
            auction_id=draft, current_user=seller, db_session=s, channels=channels
        )
    assert [channel for channel, _ in channels.published] == [f"auction:{draft}"]
    assert [event["status"] for _, event in channels.published] == ["open"]
    async with sm() as s:
        assert await s.scalar(
            text("SELECT sent_at IS NOT NULL FROM outbox WHERE auction_id = :id"), {"id": draft}
        )
