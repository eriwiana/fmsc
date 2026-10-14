"""an audit row for every redemption

Revision ID: 1a7d95c065e9
Revises: 5b904c0e7765
The only place the four redemption failures differ. A caller is told nothing beyond "no",
because telling not-found from revoked from expired from exhausted is how a code becomes
something to enumerate; the distinction an operator needs still has to live somewhere.

The code itself is never written here. code_id is, and it is NULL for a code that was never
found, which is the one case with nothing to point at.

Create Date: 2026-10-14 22:16:32.654157

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1a7d95c065e9"
down_revision: Union[str, Sequence[str], None] = "5b904c0e7765"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "code_audit",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column(
            "community_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True
        ),
        sa.Column("code_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True),
        sa.Column("user_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True),
        sa.Column("ip", sa.String(length=45), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column(
            "at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "outcome IN ('redeemed', 'not_found', 'revoked', 'expired', 'exhausted', 'already_member')",
            name=op.f("ck_code_audit_outcome_known"),
        ),
        sa.ForeignKeyConstraint(
            ["code_id"], ["community_codes.id"], name=op.f("fk_code_audit_code_id_community_codes")
        ),
        sa.ForeignKeyConstraint(
            ["community_id"],
            ["communities.id"],
            name=op.f("fk_code_audit_community_id_communities"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_code_audit_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_code_audit")),
    )
    op.create_index(op.f("ix_code_audit_code_id"), "code_audit", ["code_id"], unique=False)
    op.create_index(
        op.f("ix_code_audit_community_id"), "code_audit", ["community_id"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_code_audit_community_id"), table_name="code_audit")
    op.drop_index(op.f("ix_code_audit_code_id"), table_name="code_audit")
    op.drop_table("code_audit")
