"""Database-level guardrails for the bid path.

Every invariant here is already enforced in Python, in create_auction or in _BID_SQL.
These tests pin them in the schema instead, because `update`, a future handler, a data
migration or a hand-typed psql statement all bypass the application entirely. A bid is a
money path: the table should refuse a bad row rather than trust that every writer is
careful.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

INSERT_AUCTION = text("""
    INSERT INTO auctions (title, seller_id, starting_bid, current_bid, bid_count, status,
                          starts_at, ends_at, hard_ends_at, event_seq, created_at, updated_at)
    VALUES ('t', :seller, :starting_bid, :current_bid, :bid_count, :status,
            now(), :ends_at, :hard_ends_at, 0, now(), now())
    """)


async def _seller(maker) -> int:
    async with maker() as s:
        row = await s.execute(
            text(
                "INSERT INTO users (email, pw_hash, created_at, updated_at)"
                " VALUES ('s@x.com', 'x', now(), now()) RETURNING id"
            )
        )
        uid = row.scalar()
        await s.commit()
        return uid


async def _insert_auction(maker, **overrides):
    """Raw SQL on purpose: the ORM would supply the very defaults under test."""
    ends_at = datetime.now(timezone.utc) + timedelta(hours=1)
    params = {
        "seller": await _seller(maker),
        "starting_bid": Decimal("10.00"),
        "current_bid": None,
        "bid_count": 0,
        "status": "open",
        "ends_at": ends_at,
        "hard_ends_at": ends_at + timedelta(hours=2),
    }
    params.update(overrides)
    async with maker() as s:
        await s.execute(INSERT_AUCTION, params)
        await s.commit()


async def test_a_ceiling_before_the_deadline_is_rejected(sm):
    """The extension clamps to hard_ends_at. A row whose ceiling precedes its deadline is
    the one shape where that clamp could shorten an auction, so the table refuses it."""
    ends_at = datetime.now(timezone.utc) + timedelta(hours=1)
    with pytest.raises(IntegrityError):
        await _insert_auction(sm, ends_at=ends_at, hard_ends_at=ends_at - timedelta(seconds=1))


async def test_a_ceiling_equal_to_the_deadline_is_allowed(sm):
    """An auction that may not be extended at all is legitimate, so the bound is >=."""
    ends_at = datetime.now(timezone.utc) + timedelta(hours=1)
    await _insert_auction(sm, ends_at=ends_at, hard_ends_at=ends_at)


async def test_an_unknown_status_is_rejected(sm):
    """status is read by both the bid guard and the closer. A typo'd value makes an auction
    unbiddable and uncloseable at once, and nothing would report it."""
    with pytest.raises(IntegrityError):
        await _insert_auction(sm, status="OPEN")


async def test_a_non_positive_starting_bid_is_rejected(sm):
    """create_auction checks this in Python only, so `update` or a migration can undo it.
    A starting_bid of 0 would let the opening bid be a cent."""
    with pytest.raises(IntegrityError):
        await _insert_auction(sm, starting_bid=Decimal("0"))


async def test_a_current_bid_below_the_starting_bid_is_rejected(sm):
    """_BID_SQL will not write one, but nothing stops another writer. It would mean an
    accepted bid below the price the seller advertised."""
    with pytest.raises(IntegrityError):
        await _insert_auction(sm, current_bid=Decimal("9.99"))


async def test_a_non_positive_bid_amount_is_rejected(sm):
    """The history row is what a settlement would be computed from."""
    seller = await _seller(sm)
    ends_at = datetime.now(timezone.utc) + timedelta(hours=1)
    async with sm() as s:
        aid = (
            await s.execute(
                text(
                    "INSERT INTO auctions (title, seller_id, starting_bid, bid_count, status,"
                    " starts_at, ends_at, hard_ends_at, event_seq, created_at, updated_at)"
                    " VALUES ('t', :seller, 10.00, 0, 'open', now(), :ends_at, :hard, 0,"
                    " now(), now())"
                    " RETURNING id"
                ),
                {"seller": seller, "ends_at": ends_at, "hard": ends_at + timedelta(hours=2)},
            )
        ).scalar()
        await s.commit()
    with pytest.raises(IntegrityError):
        async with sm() as s:
            await s.execute(
                text(
                    "INSERT INTO bids (auction_id, user_id, amount, created_at, updated_at)"
                    " VALUES (:aid, :uid, 0, now(), now())"
                ),
                {"aid": aid, "uid": seller},
            )
            await s.commit()
