"""guard the bid path with check constraints

Revision ID: 97311b414123
Revises: fe57a3854b33
Create Date: 2026-10-03 03:41:00.000000

Every invariant here is already enforced in Python, in create_auction or in _BID_SQL.
They are repeated in the schema because `update`, a future handler, a data migration and
a hand-typed psql statement all bypass the application.

Each ADD CONSTRAINT scans the table once and holds ACCESS EXCLUSIVE while it does, which
blocks writes. Nothing to worry about at this size; on a large auctions table these want
NOT VALID followed by VALIDATE CONSTRAINT, which takes a weaker lock.
"""

from collections.abc import Sequence
from typing import Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "97311b414123"
down_revision: Union[str, Sequence[str], None] = "fe57a3854b33"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Bare names. The metadata naming convention is ck_%(table_name)s_%(constraint_name)s and
# both create_check_constraint and drop_constraint apply it, so passing the rendered name
# here produces ck_auctions_ck_auctions_... and fails.
CHECKS = {
    "auctions": {
        # Equal is allowed: an auction that may not be extended at all is legitimate.
        "ceiling_after_deadline": "hard_ends_at >= ends_at",
        "status_known": "status IN ('open', 'closed')",
        "starting_bid_positive": "starting_bid > 0",
        "current_bid_at_least_starting": "current_bid IS NULL OR current_bid >= starting_bid",
    },
    "bids": {"amount_positive": "amount > 0"},
}


def upgrade() -> None:
    """Upgrade schema."""
    for table, checks in CHECKS.items():
        for name, condition in checks.items():
            op.create_check_constraint(name, table, condition)


def downgrade() -> None:
    """Downgrade schema."""
    for table, checks in CHECKS.items():
        for name in checks:
            op.drop_constraint(name, table, type_="check")
