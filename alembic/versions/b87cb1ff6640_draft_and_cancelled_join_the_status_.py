"""draft and cancelled join the status machine

Revision ID: b87cb1ff6640
Revises: 801727af8068
Create Date: 2026-10-14 16:45:48.661634

Only the check constraint moves. Autogenerate does not compare CHECK bodies, so this is
written by hand: a constraint that still reads IN ('open', 'closed') refuses every draft
and every cancellation, and nothing in the models would report the drift.

Existing rows are untouched. They are open or closed, which both remain legal, so the
widened constraint validates against the table as it stands.

The downgrade narrows it again and will fail while any auction is a draft or cancelled,
which is correct: the rows cannot be represented in the older schema, and quietly
rewriting somebody's withdrawn auction back to open would put it up for sale again.

"""

from collections.abc import Sequence
from typing import Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b87cb1ff6640"
down_revision: Union[str, Sequence[str], None] = "801727af8068"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The naming convention supplies the ck_auctions_ prefix; passing the full name here gets
# it applied twice.
_NAME = "status_known"
_OLD = "status IN ('open', 'closed')"
_NEW = "status IN ('draft', 'open', 'closed', 'cancelled')"


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint(_NAME, "auctions", type_="check")
    op.create_check_constraint(_NAME, "auctions", _NEW)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(_NAME, "auctions", type_="check")
    op.create_check_constraint(_NAME, "auctions", _OLD)
