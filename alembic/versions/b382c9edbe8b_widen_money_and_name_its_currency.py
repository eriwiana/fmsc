"""widen money and name its currency

Revision ID: b382c9edbe8b
Revises: 06f6290afbe2
Create Date: 2026-10-14 01:54:37.000000

Numeric(12,2) stops just under 10 miliar IDR, which one lot can exceed, and an amount
without its unit is not a price. Both are schema changes to a money column, which is the
migration you least want to run against live bids — so it runs now, while the tables are
small, rather than after.

The currency arrives with a server default so the existing rows fill in one pass, and the
default is then dropped: the application always writes the column, and leaving it would
let a future INSERT omit the currency silently, which is the whole bug being prevented.

bids.currency is backfilled from the bid's own auction rather than from the same literal,
because the composite foreign key created at the end refuses any pair that disagrees.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b382c9edbe8b"
down_revision: Union[str, Sequence[str], None] = "06f6290afbe2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CODE = "currency ~ '^[A-Z]{3}$'"


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column(
        "auctions",
        "starting_bid",
        existing_type=sa.NUMERIC(precision=12, scale=2),
        type_=sa.Numeric(precision=18, scale=2),
        existing_nullable=False,
    )
    op.alter_column(
        "auctions",
        "current_bid",
        existing_type=sa.NUMERIC(precision=12, scale=2),
        type_=sa.Numeric(precision=18, scale=2),
        existing_nullable=True,
    )
    op.alter_column(
        "bids",
        "amount",
        existing_type=sa.NUMERIC(precision=12, scale=2),
        type_=sa.Numeric(precision=18, scale=2),
        existing_nullable=False,
    )

    op.add_column(
        "auctions",
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="IDR"),
    )
    op.alter_column("auctions", "currency", server_default=None)
    op.create_check_constraint("currency_is_a_code", "auctions", CODE)
    # Postgres requires a unique constraint on the columns a foreign key references.
    op.create_unique_constraint("uq_auctions_id_currency", "auctions", ["id", "currency"])

    op.add_column(
        "bids",
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="IDR"),
    )
    op.execute(
        "UPDATE bids SET currency = auctions.currency"
        "  FROM auctions WHERE auctions.id = bids.auction_id"
    )
    op.alter_column("bids", "currency", server_default=None)
    op.create_check_constraint("currency_is_a_code", "bids", CODE)
    # Replaces the single-column key: the auction has to exist and the currencies have to
    # agree, and one constraint says both.
    op.drop_constraint("fk_bids_auction_id_auctions", "bids", type_="foreignkey")
    op.create_foreign_key(
        "fk_bids_auction_id_auctions",
        "bids",
        "auctions",
        ["auction_id", "currency"],
        ["id", "currency"],
    )


def downgrade() -> None:
    """Downgrade schema.

    The narrowing back to Numeric(12,2) fails if any stored amount no longer fits, which
    is the right answer: there is no downgrade that keeps the money.
    """
    op.drop_constraint("fk_bids_auction_id_auctions", "bids", type_="foreignkey")
    op.create_foreign_key("fk_bids_auction_id_auctions", "bids", "auctions", ["auction_id"], ["id"])
    op.drop_constraint("currency_is_a_code", "bids", type_="check")
    op.drop_column("bids", "currency")
    op.drop_constraint("uq_auctions_id_currency", "auctions", type_="unique")
    op.drop_constraint("currency_is_a_code", "auctions", type_="check")
    op.drop_column("auctions", "currency")
    op.alter_column(
        "bids",
        "amount",
        existing_type=sa.Numeric(precision=18, scale=2),
        type_=sa.NUMERIC(precision=12, scale=2),
        existing_nullable=False,
    )
    op.alter_column(
        "auctions",
        "current_bid",
        existing_type=sa.Numeric(precision=18, scale=2),
        type_=sa.NUMERIC(precision=12, scale=2),
        existing_nullable=True,
    )
    op.alter_column(
        "auctions",
        "starting_bid",
        existing_type=sa.Numeric(precision=18, scale=2),
        type_=sa.NUMERIC(precision=12, scale=2),
        existing_nullable=False,
    )
