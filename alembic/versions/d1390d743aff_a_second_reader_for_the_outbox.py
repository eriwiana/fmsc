"""a second reader for the outbox

Revision ID: d1390d743aff
Revises: 8c8c3adfdb35
Its own column rather than sharing sent_at: Discord is a second consumer of the queue with
its own pace and its own failures, and sharing the state would mean whichever reader got
there first retired the row for both.

Every existing row is backfilled as handled. Nothing that happened before this deploy was
ever Discord's to post, and leaving them NULL would both replay the whole table into the
first guild that links a channel and stop prune_outbox from ever collecting them, since it
now waits for both readers.

Create Date: 2026-10-15 14:01:46.654491

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d1390d743aff"
down_revision: Union[str, Sequence[str], None] = "8c8c3adfdb35"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("outbox", sa.Column("discord_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE outbox SET discord_at = clock_timestamp()")
    op.create_index(
        "ix_outbox_undiscorded",
        "outbox",
        ["auction_id", "seq"],
        unique=False,
        postgresql_where=sa.text("discord_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_outbox_undiscorded",
        table_name="outbox",
        postgresql_where=sa.text("discord_at IS NULL"),
    )
    op.drop_column("outbox", "discord_at")
