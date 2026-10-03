"""per-auction event sequence

Revision ID: cd0369d5dce9
Revises: 0a4e3313b2b6
Create Date: 2026-10-03 11:20:00.000000

Existing auctions start at 0, which is correct: a client subscribing to one of them gets
a snapshot saying seq 0 and the next bid is seq 1. The server default is what makes the
backfill unnecessary — NOT NULL with a default fills existing rows in one pass.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "cd0369d5dce9"
down_revision: Union[str, Sequence[str], None] = "0a4e3313b2b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "auctions",
        sa.Column("event_seq", sa.Integer(), nullable=False, server_default="0"),
    )
    # The default belongs to the migration, not the table: the application always writes
    # the column, and leaving it would let a future INSERT omit it silently.
    op.alter_column("auctions", "event_seq", server_default=None)
    op.create_check_constraint("event_seq_not_negative", "auctions", "event_seq >= 0")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("event_seq_not_negative", "auctions", type_="check")
    op.drop_column("auctions", "event_seq")
