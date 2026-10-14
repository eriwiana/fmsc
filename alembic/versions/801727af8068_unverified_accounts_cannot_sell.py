"""unverified accounts cannot sell

Revision ID: 801727af8068
Revises: e38a62b01481
Nullable, and every existing account stays NULL: the addresses were never verified, and
saying they were because they are already there is how the check becomes decoration. The
accounts in the database today are test accounts.

Nothing in this revision sets the column. No mail is sent yet, so until the verification
flow lands it is set out of band:

    UPDATE users SET verified_at = clock_timestamp() WHERE email = '...';

Create Date: 2026-10-14 14:04:41.266203

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "801727af8068"
down_revision: Union[str, Sequence[str], None] = "e38a62b01481"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("users", sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("users", "verified_at")
