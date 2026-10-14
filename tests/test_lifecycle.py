"""M8 acceptance: the status is a state machine, and ownership guards the edges.

Criteria 1 and 2:

1. Every illegal status transition is refused by a guarded UPDATE, per transition. The
   states are draft, open, closed and cancelled, and the only legal edges are
   draft->open, draft->cancelled, open->closed and open->cancelled. Refusal means the
   statement matched no row, not that a Python branch raised: `update`, a data migration
   and psql all bypass a Python branch, and the test drives the statement directly.
2. Cancel is refused once a bid exists. A cancelled auction with bids on it is a seller
   walking away from money other people committed. The edit half of criterion 2 lands
   with the edit endpoint.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.auctions import (
    LEGAL_FROM,
    STATUSES,
    transition,
)
from app.models import User


async def _seller(maker, email: str = "seller@x.com") -> User:
    async with maker() as s:
        user = User(email=email, pw_hash="x", verified_at=datetime.now(timezone.utc))
        s.add(user)
        await s.commit()
        return user


async def _auction(maker, seller_id: int, status: str, bid_count: int = 0) -> int:
    """Raw INSERT, naming every column, because the ORM would supply the very defaults
    under test — and because a draft is not reachable through create_auction's own path
    once the status is anything but open."""
    ends_at = datetime.now(timezone.utc) + timedelta(hours=1)
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
