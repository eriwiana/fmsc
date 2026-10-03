"""outbox for published events

Revision ID: 5d1b045aced3
Revises: cd0369d5dce9
Create Date: 2026-10-03 11:40:00.000000

The partial index is what the relay reads: unsent rows are a short queue at the head of a
table that only grows, so indexing all of it would cost more every day for a scan that
only ever wants the few rows with sent_at IS NULL.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5d1b045aced3"
down_revision: Union[str, Sequence[str], None] = "cd0369d5dce9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "outbox",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("auction_id", sa.BigInteger(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["auction_id"], ["auctions.id"], name="fk_outbox_auction_id_auctions"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_outbox"),
        sa.UniqueConstraint("auction_id", "seq", name="uq_outbox_auction_seq"),
    )
    op.create_index("ix_outbox_auction_id", "outbox", ["auction_id"])
    op.create_index(
        "ix_outbox_unsent",
        "outbox",
        ["auction_id", "seq"],
        postgresql_where=sa.text("sent_at IS NULL"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_outbox_unsent", table_name="outbox")
    op.drop_index("ix_outbox_auction_id", table_name="outbox")
    op.drop_table("outbox")
