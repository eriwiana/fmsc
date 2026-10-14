"""index the seller the dashboard filters on

Revision ID: 1d462e1da2b4
Revises: 120686f6c4c0
GET /auctions/mine filters by seller_id, and Postgres does not index a foreign key for
you — without this the dashboard is a sequential scan of every auction on the service.

Create Date: 2026-10-14 18:02:57.632709

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1d462e1da2b4"
down_revision: Union[str, Sequence[str], None] = "120686f6c4c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index(op.f("ix_auctions_seller_id"), "auctions", ["seller_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_auctions_seller_id"), table_name="auctions")
