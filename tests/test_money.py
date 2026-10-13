"""M6 acceptance: money is Numeric(18,2) and every amount names its currency.

Three criteria, one test each for the first two:

1. The migration runs against a table that already holds auctions and bids, and the
   values and their scale survive it. Proved by a real `alembic downgrade`/`upgrade`
   round trip over seeded rows, not by reading the migration.
2. A bid whose currency differs from its auction is refused by the database, not only
   by Python.
3. The existing money guardrails still hold at the wider type — that is every money
   test in test_bid.py and test_guardrails.py, which run unchanged. The one addition
   here is the bound itself: MAX_BID has to be the column's own ceiling, so a bid at
   exactly MAX_BID must reach Postgres and be stored.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from litestar.exceptions import ClientException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.auctions import MAX_BID, create_auction, place_bid_tx
from app.models import User
from app.schemas import CreateAuctionRequest

ROOT = Path(__file__).resolve().parent.parent

# Fits Numeric(12,2) as well as Numeric(18,2), so the downgrade in the round trip has no
# excuse to refuse the row. The trailing 2 is the point: a widening that silently
# requantised would lose it.
SEEDED = Decimal("1234567890.12")


def _alembic(*args: str) -> None:
    done = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr


async def _seed_auction_and_bid(maker, amount: Decimal = SEEDED, currency: str = "IDR") -> int:
    """Raw SQL, naming every column: the ORM would supply the very definitions under test."""
    ends_at = datetime.now(timezone.utc) + timedelta(hours=1)
    async with maker() as s:
        uid = (
            await s.execute(
                text(
                    "INSERT INTO users (email, pw_hash, created_at, updated_at)"
                    " VALUES ('s@x.com', 'x', now(), now()) RETURNING id"
                )
            )
        ).scalar()
        aid = (
            await s.execute(
                text(
                    "INSERT INTO auctions (title, seller_id, starting_bid, currency, bid_count,"
                    " status, starts_at, ends_at, hard_ends_at, event_seq, created_at, updated_at)"
                    " VALUES ('t', :uid, :amount, :currency, 0, 'open', now(), :ends_at, :hard, 0,"
                    " now(), now()) RETURNING id"
                ),
                {
                    "uid": uid,
                    "amount": amount,
                    "currency": currency,
                    "ends_at": ends_at,
                    "hard": ends_at + timedelta(hours=2),
                },
            )
        ).scalar()
        await s.execute(
            text(
                "INSERT INTO bids (auction_id, user_id, amount, currency, created_at, updated_at)"
                " VALUES (:aid, :uid, :amount, :currency, now(), now())"
            ),
            {"aid": aid, "uid": uid, "amount": amount, "currency": currency},
        )
        await s.commit()
        return aid


async def _user(maker, email: str) -> User:
    """`expire_on_commit=False` on the fixture, so `.id` survives the commit."""
    async with maker() as s:
        user = User(email=email, pw_hash="x")
        s.add(user)
        await s.commit()
        return user


async def test_the_migration_runs_over_populated_tables(sm):
    """Criterion 1. Seeded rows, then the migration down and back up for real.

    Reading the migration proves nothing: a money column with data in it is the worst
    migration in the catalogue, and the only honest test is to run it against rows.
    """
    aid = await _seed_auction_and_bid(sm)
    try:
        _alembic("downgrade", "-1")
    finally:
        # In the finally, not after it: a failure mid-flight would otherwise leave the
        # schema behind for every later test in the run.
        _alembic("upgrade", "head")

    async with sm() as s:
        row = (
            await s.execute(
                text(
                    "SELECT a.starting_bid, a.currency, b.amount, b.currency AS bid_currency"
                    " FROM auctions a JOIN bids b ON b.auction_id = a.id WHERE a.id = :id"
                ),
                {"id": aid},
            )
        ).one()
    # Decimal compares equal across scales (1234567890.1 == 1234567890.10), so the
    # string is what pins the scale the migration left behind.
    assert str(row.starting_bid) == str(SEEDED)
    assert str(row.amount) == str(SEEDED)
    # Existing rows predate the column, so the migration has to fill them.
    assert row.currency == "IDR"
    assert row.bid_currency == "IDR"
    async with sm() as s:
        types = (
            await s.execute(
                text(
                    "SELECT numeric_precision, numeric_scale FROM information_schema.columns"
                    " WHERE (table_name, column_name) IN"
                    " (('auctions', 'starting_bid'), ('auctions', 'current_bid'),"
                    "  ('bids', 'amount'))"
                )
            )
        ).all()
    assert types == [(18, 2)] * 3


async def test_a_bid_in_another_currency_is_rejected(sm):
    """Criterion 2. The handler copies the auction's currency onto the bid, so this row
    can only arrive from `update`, a migration or psql — which is exactly why the table
    has to be the one refusing it. An IDR auction settled against a USD bid is a
    four-order-of-magnitude error in the amount owed."""
    aid = await _seed_auction_and_bid(sm)
    with pytest.raises(IntegrityError):
        async with sm() as s:
            await s.execute(
                text(
                    "INSERT INTO bids (auction_id, user_id, amount, currency,"
                    " created_at, updated_at)"
                    " SELECT :aid, seller_id, 20.00, 'USD', now(), now()"
                    "   FROM auctions WHERE id = :aid"
                ),
                {"aid": aid},
            )
            await s.commit()


async def test_a_currency_that_is_not_a_three_letter_code_is_rejected(sm):
    """'idr' and 'Rp' both read as a currency to a human, and an auction holding either is
    one nothing can ever bid on: the composite foreign key below needs an exact match.

    DBAPIError, not IntegrityError, because two different mechanisms refuse these: the
    check constraint for the case and the non-letter, varchar(3) for the overflow. The
    second arrives as a bare DBAPIError — the asyncpg dialect does not map
    StringDataRightTruncation onto DataError, so IntegrityError would miss it entirely.
    Same lesson as the over-long Idempotency-Key in M4: a column's own length limit is
    not an integrity error, and the handler has to catch it before the statement.
    """
    for bad in ("idr", "Rp", "IDRX"):
        with pytest.raises(DBAPIError):
            await _seed_auction_and_bid(sm, currency=bad)


async def test_a_malformed_currency_is_a_400_and_not_a_500(sm):
    """The DBAPIError above is the backstop. A client that sends one has asked for a
    bad request, and `IDRX` would otherwise come back as the 500 a DataError becomes."""
    seller = await _user(sm, "s@x.com")
    ends_at = datetime.now(timezone.utc) + timedelta(hours=1)
    for bad in ("rupiah", "", "ID1"):
        with pytest.raises(ClientException):
            async with sm() as s:
                await create_auction.fn(
                    data=CreateAuctionRequest(
                        title="t",
                        starting_bid=Decimal("10.00"),
                        ends_at=ends_at,
                        currency=bad,
                    ),
                    current_user=seller,
                    db_session=s,
                )


async def test_a_lowercase_currency_is_accepted_and_stored_uppercase(sm):
    """`idr` from a client is a spelling, not a different currency."""
    seller = await _user(sm, "s@x.com")
    ends_at = datetime.now(timezone.utc) + timedelta(hours=1)
    async with sm() as s:
        created = await create_auction.fn(
            data=CreateAuctionRequest(
                title="t", starting_bid=Decimal("10.00"), ends_at=ends_at, currency=" idr "
            ),
            current_user=seller,
            db_session=s,
        )
    assert created.currency == "IDR"


async def test_a_bid_takes_its_currency_from_the_auction(sm):
    """Nothing in the request says what currency a bid is in, and nothing should: the
    statement copies the auction's own across. Hard-coding the default here instead would
    leave every auction in another currency unbiddable, since the composite key refuses
    the mismatch — a 500 on the money path, for a row that should never be built.
    """
    aid = await _seed_auction_and_bid(sm, amount=Decimal("10.00"), currency="USD")
    bidder = (await _user(sm, "b@x.com")).id
    async with sm() as s:
        assert await place_bid_tx(s, aid, bidder, Decimal("20.00")) is not None
    async with sm() as s:
        row = (
            await s.execute(
                text("SELECT currency FROM bids WHERE auction_id = :id AND user_id = :uid"),
                {"id": aid, "uid": bidder},
            )
        ).one()
    assert row.currency == "USD"


async def test_the_largest_allowed_bid_reaches_the_column(sm):
    """Criterion 3. MAX_BID exists to stop Postgres raising NumericValueOutOfRange, so it
    has to be the column's own ceiling: too low and the widening bought nothing, too high
    and a bid at the bound is a 500. Both failures show up here."""
    aid = await _seed_auction_and_bid(sm, amount=Decimal("10.00"))
    bidder = (await _user(sm, "b@x.com")).id
    async with sm() as s:
        assert await place_bid_tx(s, aid, bidder, MAX_BID) is not None
    async with sm() as s:
        row = (
            await s.execute(text("SELECT current_bid FROM auctions WHERE id = :id"), {"id": aid})
        ).one()
    assert row.current_bid == MAX_BID
    # The ceiling the milestone is about: Numeric(12,2) stopped just under 10 miliar.
    assert Decimal("10_000_000_000.00") < MAX_BID
