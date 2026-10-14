"""limit failed logins

Revision ID: e38a62b01481
Revises: 9fb9f790f9af
A table of failed logins, read as a rate: count the rows inside a window. A counter
column cannot express that, because a window cannot be subtracted from an integer, and
in-process memory cannot either — the limit has to hold across processes and survive a
deploy.

Two indexes, one per bucket, because the two counts are independent on purpose. A
combined (email, ip) index would be the wrong shape for the query and the wrong shape
for the limit.

Create Date: 2026-10-14 13:59:19.265861

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e38a62b01481"
down_revision: Union[str, Sequence[str], None] = "9fb9f790f9af"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "login_attempts",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("ip", sa.String(length=45), nullable=False),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_login_attempts")),
    )
    op.create_index(
        "ix_login_attempts_email_failed_at", "login_attempts", ["email", "failed_at"], unique=False
    )
    op.create_index(
        "ix_login_attempts_ip_failed_at", "login_attempts", ["ip", "failed_at"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_login_attempts_ip_failed_at", table_name="login_attempts")
    op.drop_index("ix_login_attempts_email_failed_at", table_name="login_attempts")
    op.drop_table("login_attempts")
