"""idempotency key and stored response on bids

Revision ID: 0a4e3313b2b6
Revises: 97311b414123
Create Date: 2026-10-03 06:40:00.000000

Both columns are nullable and existing rows keep NULL, so bids placed before this
migration are untouched and bids sent without a key still work.

The unique constraint is the dedupe. Postgres does not treat NULLs as equal, so keyless
bids do not collide with each other — no partial index needed.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0a4e3313b2b6"
down_revision: Union[str, Sequence[str], None] = "97311b414123"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("bids", sa.Column("idempotency_key", sa.String(length=128), nullable=True))
    op.add_column("bids", sa.Column("response", sa.Text(), nullable=True))
    op.create_unique_constraint(
        "uq_bids_auction_user_idempotency_key",
        "bids",
        ["auction_id", "user_id", "idempotency_key"],
    )
    op.create_check_constraint(
        "key_and_response_together", "bids", "(idempotency_key IS NULL) = (response IS NULL)"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("key_and_response_together", "bids", type_="check")
    op.drop_constraint("uq_bids_auction_user_idempotency_key", "bids", type_="unique")
    op.drop_column("bids", "response")
    op.drop_column("bids", "idempotency_key")
