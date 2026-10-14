"""limit redemption attempts

Revision ID: e19c1b884d50
Revises: 1a7d95c065e9
Its own table rather than a kind column on login_attempts, so that guessing codes at
somebody cannot lock them out of signing in and a login they fumbled cannot stop them
joining. One missing predicate on a shared table merges the two buckets, and nothing would
report it.

Keyed on user_id rather than an email: redeeming needs a session, so there is always an
account to count against and no reason to key on a string the caller chose.

Create Date: 2026-10-14 22:23:00.919905

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e19c1b884d50"
down_revision: Union[str, Sequence[str], None] = "1a7d95c065e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "code_attempts",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("user_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("ip", sa.String(length=45), nullable=False),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_code_attempts_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_code_attempts")),
    )
    op.create_index(
        "ix_code_attempts_ip_failed_at", "code_attempts", ["ip", "failed_at"], unique=False
    )
    op.create_index(
        "ix_code_attempts_user_failed_at", "code_attempts", ["user_id", "failed_at"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_code_attempts_user_failed_at", table_name="code_attempts")
    op.drop_index("ix_code_attempts_ip_failed_at", table_name="code_attempts")
    op.drop_table("code_attempts")
