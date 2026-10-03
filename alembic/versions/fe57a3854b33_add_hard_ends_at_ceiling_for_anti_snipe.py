"""add hard_ends_at ceiling for anti-snipe

Revision ID: fe57a3854b33
Revises: 39e1bcc36bb8
Create Date: 2026-10-03 02:09:14.863016

Three steps rather than one NOT NULL column: an auction row already in the table has no
ceiling to copy, so the column arrives nullable, gets backfilled from each row's own
deadline, and only then becomes NOT NULL. Autogenerate wrote it as NOT NULL in one step,
which fails on any table that already has rows. The backfill gives existing auctions the
same 2h allowance a new one gets, so a bid on an auction created before this can extend.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "fe57a3854b33"
down_revision: Union[str, Sequence[str], None] = "39e1bcc36bb8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("auctions", sa.Column("hard_ends_at", sa.DateTime(timezone=True), nullable=True))
    # Literal, not app.auctions.MAX_EXTENSION: a migration pins the table at this revision
    # and must not change meaning when the application constant does.
    op.execute("UPDATE auctions SET hard_ends_at = ends_at + interval '2 hours'")
    op.alter_column("auctions", "hard_ends_at", nullable=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("auctions", "hard_ends_at")
