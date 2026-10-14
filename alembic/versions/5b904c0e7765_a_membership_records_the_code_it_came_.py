"""a membership records the code it came from

Revision ID: 5b904c0e7765
Revises: 9213d01f2012
Both nullable, and every existing membership row stays NULL: they were seeded or
hand-written, not redeemed, and inventing a provenance for them would make the audit trail
say something untrue.

joined_via_generation duplicates `generation` by construction. It is kept separately
because that one is the key the bid guard compares and this one is the provenance record;
if how a generation gets assigned ever changes, history should not be rewritten with it.

Create Date: 2026-10-14 19:57:04.546103

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5b904c0e7765"
down_revision: Union[str, Sequence[str], None] = "9213d01f2012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "community_memberships",
        sa.Column(
            "joined_via_code_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=True,
        ),
    )
    op.add_column(
        "community_memberships", sa.Column("joined_via_generation", sa.Integer(), nullable=True)
    )
    op.create_foreign_key(
        op.f("fk_community_memberships_joined_via_code_id_community_codes"),
        "community_memberships",
        "community_codes",
        ["joined_via_code_id"],
        ["id"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        op.f("fk_community_memberships_joined_via_code_id_community_codes"),
        "community_memberships",
        type_="foreignkey",
    )
    op.drop_column("community_memberships", "joined_via_generation")
    op.drop_column("community_memberships", "joined_via_code_id")
