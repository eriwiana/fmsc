"""the settlement record

Revision ID: ac2a39d3edf6
Revises: 1f302ad2640b
One row per closed auction, carrying each side's own mark. Unique on auction_id, which is
what lets app.settlement do the whole thing in one upsert: whichever party marks first
inserts the row, and the other conflicts on this constraint and fills its own column.

Two nullable timestamps and no status column. "Settled" is both of them being present, so
there is nothing stored that can contradict them — the same reason reputation is counted
from these rows rather than kept as a total.

buyer_id is indexed because reputation reads it as a filter. The seller side needs no index
here: it joins through auctions, which already indexes seller_id.

Create Date: 2026-10-30 22:19:47.996391

"""

from typing import Sequence, Union

import advanced_alchemy
import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ac2a39d3edf6"
down_revision: Union[str, Sequence[str], None] = "1f302ad2640b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "settlements",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column(
            "auction_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False
        ),
        sa.Column("buyer_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("seller_settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("buyer_settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            advanced_alchemy.types.datetime.DateTimeUTC(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            advanced_alchemy.types.datetime.DateTimeUTC(timezone=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["auction_id"], ["auctions.id"], name=op.f("fk_settlements_auction_id_auctions")
        ),
        sa.ForeignKeyConstraint(
            ["buyer_id"], ["users.id"], name=op.f("fk_settlements_buyer_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_settlements")),
        sa.UniqueConstraint("auction_id", name=op.f("uq_settlements_auction_id")),
    )
    op.create_index(op.f("ix_settlements_buyer_id"), "settlements", ["buyer_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_settlements_buyer_id"), table_name="settlements")
    op.drop_table("settlements")
