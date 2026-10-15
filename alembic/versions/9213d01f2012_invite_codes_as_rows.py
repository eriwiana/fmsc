"""invite codes as rows

Revision ID: 9213d01f2012
Revises: 527abcfc8063
The code itself is not in this table. code_lookup is HMAC-SHA256(payload, server pepper),
so a leaked backup hands nobody a working code, and a lookup is still one indexed equality.

The partial unique index is what makes "one live code per community" a property of the
schema rather than of whoever remembers to check. Rotation is invalidate-then-insert in one
transaction, and two concurrent rotations cannot both leave a code behind.

used_count <= max_uses is a check constraint because an application-level test of the same
thing is CWE-837: whoever reads the count is not whoever increments it.

Create Date: 2026-10-14 19:53:31.560385

"""

from typing import Sequence, Union

import advanced_alchemy
import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9213d01f2012"
down_revision: Union[str, Sequence[str], None] = "527abcfc8063"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "community_codes",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column(
            "community_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False
        ),
        sa.Column("label", sa.String(length=60), nullable=False),
        sa.Column("code_lookup", sa.String(length=64), nullable=False),
        sa.Column("max_uses", sa.Integer(), nullable=False),
        sa.Column("used_count", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", advanced_alchemy.types.datetime.DateTimeUTC(timezone=True), nullable=False
        ),
        sa.Column(
            "updated_at", advanced_alchemy.types.datetime.DateTimeUTC(timezone=True), nullable=False
        ),
        sa.CheckConstraint("max_uses > 0", name=op.f("ck_community_codes_max_uses_positive")),
        sa.CheckConstraint(
            "used_count <= max_uses", name=op.f("ck_community_codes_uses_within_max")
        ),
        sa.CheckConstraint(
            "used_count >= 0", name=op.f("ck_community_codes_used_count_not_negative")
        ),
        sa.ForeignKeyConstraint(
            ["community_id"],
            ["communities.id"],
            name=op.f("fk_community_codes_community_id_communities"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_community_codes")),
    )
    op.create_index(
        op.f("ix_community_codes_code_lookup"), "community_codes", ["code_lookup"], unique=True
    )
    op.create_index(
        op.f("ix_community_codes_community_id"), "community_codes", ["community_id"], unique=False
    )
    op.create_index(
        "uq_one_live_code_per_community",
        "community_codes",
        ["community_id"],
        unique=True,
        postgresql_where=sa.text("invalidated_at IS NULL"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "uq_one_live_code_per_community",
        table_name="community_codes",
        postgresql_where=sa.text("invalidated_at IS NULL"),
    )
    op.drop_index(op.f("ix_community_codes_community_id"), table_name="community_codes")
    op.drop_index(op.f("ix_community_codes_code_lookup"), table_name="community_codes")
    op.drop_table("community_codes")
