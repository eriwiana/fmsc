"""sessions expire and can be revoked

Revision ID: 9fb9f790f9af
Revises: b382c9edbe8b
Create Date: 2026-10-14 07:10:00.000000

Existing sessions are expired, not extended. They were issued with no expiry at all, which
is the problem this migration exists to end; handing each one a fresh seven days would
carry every already-leaked token forward by a week. The cost is that everybody signs in
again once, which is the cheapest possible version of this fix.

The default is dropped straight after the backfill: the application always computes the
expiry, and leaving a default would let a future INSERT quietly issue a session that
expired the moment it was created.

The index on user_id is what revoke-all deletes by. The one on expires_at is what the
sweeper deletes by: a row arrives per login and the sweep runs every tick, so without it
the scan grows with every account that has ever signed in.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9fb9f790f9af"
down_revision: Union[str, Sequence[str], None] = "b382c9edbe8b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "sessions",
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
    )
    op.alter_column("sessions", "expires_at", server_default=None)
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"], unique=False)
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"], unique=False)


def downgrade() -> None:
    """Downgrade schema.

    Dropping the column makes every surviving session immortal again, including any that
    this revision had already expired.
    """
    op.drop_index("ix_sessions_expires_at", table_name="sessions")
    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_column("sessions", "expires_at")
