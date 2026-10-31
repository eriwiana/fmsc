"""a second chance for the runner-up

Revision ID: 0d8120c68b59
Revises: ac2a39d3edf6
Unique on auction_id: a seller who clicks twice has made the same offer, not a second one,
and the insert leans on this constraint to say so rather than reading first.

No column on auctions, and nothing here touches auctions.status. The lot stays closed and
keeps the winner it closed on, so the history says it was won at one price and then offered
on at another, rather than retroactively won by somebody else.

amount is the runner-up's own highest bid, copied from auctions.runner_up_bid. It is the
only price they can be held to without being asked again.

Create Date: 2026-10-30 22:31:12.000000

"""

from typing import Sequence, Union

import advanced_alchemy
import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0d8120c68b59"
down_revision: Union[str, Sequence[str], None] = "ac2a39d3edf6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "second_chance_offers",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column(
            "auction_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False
        ),
        sa.Column(
            "offered_to_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False
        ),
        sa.Column("amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column(
            "created_at", advanced_alchemy.types.datetime.DateTimeUTC(timezone=True), nullable=False
        ),
        sa.Column(
            "updated_at", advanced_alchemy.types.datetime.DateTimeUTC(timezone=True), nullable=False
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_second_chance_offers_amount_positive")),
        sa.ForeignKeyConstraint(
            ["auction_id"],
            ["auctions.id"],
            name=op.f("fk_second_chance_offers_auction_id_auctions"),
        ),
        sa.ForeignKeyConstraint(
            ["offered_to_id"],
            ["users.id"],
            name=op.f("fk_second_chance_offers_offered_to_id_users"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_second_chance_offers")),
        sa.UniqueConstraint("auction_id", name=op.f("uq_second_chance_offers_auction_id")),
    )
    op.create_index(
        op.f("ix_second_chance_offers_offered_to_id"),
        "second_chance_offers",
        ["offered_to_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_second_chance_offers_offered_to_id"), table_name="second_chance_offers")
    op.drop_table("second_chance_offers")
