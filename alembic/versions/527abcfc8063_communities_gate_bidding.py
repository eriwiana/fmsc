"""communities gate bidding

Revision ID: 527abcfc8063
Revises: 1d462e1da2b4
Two tables and one nullable column. Nothing is backfilled: every existing auction has
community_id NULL, which is a public auction, and that is what they all were.

membership_generation starts at 0 and is bumped by remove-all rather than deleting
membership rows, so the question "who was in this community when that lot sold" survives
the action that ends every membership.

The downgrade drops all three. It will not fail, because an auction that names a community
loses only the gate — but every such auction becomes public, so it is not a safe thing to
run against real data without deciding what should happen to those lots first.

The membership uniqueness is a *partial* unique index, on revoked_at IS NULL, not a plain
constraint. A kick leaves the row at the community's current generation, so a plain one
makes a mistaken kick permanent: the person can never redeem again, and the only remedy is
remove-all, which ends everyone else's membership too. The revoked rows stay as the record
of the kick.

Create Date: 2026-10-14 19:40:39.437642

"""

from typing import Sequence, Union

import advanced_alchemy
import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "527abcfc8063"
down_revision: Union[str, Sequence[str], None] = "1d462e1da2b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "communities",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("owner_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("membership_generation", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", advanced_alchemy.types.datetime.DateTimeUTC(timezone=True), nullable=False
        ),
        sa.Column(
            "updated_at", advanced_alchemy.types.datetime.DateTimeUTC(timezone=True), nullable=False
        ),
        sa.CheckConstraint(
            "membership_generation >= 0", name=op.f("ck_communities_generation_not_negative")
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_communities_owner_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_communities")),
    )
    op.create_index(op.f("ix_communities_owner_id"), "communities", ["owner_id"], unique=False)
    op.create_table(
        "community_memberships",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column(
            "community_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False
        ),
        sa.Column("user_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", advanced_alchemy.types.datetime.DateTimeUTC(timezone=True), nullable=False
        ),
        sa.Column(
            "updated_at", advanced_alchemy.types.datetime.DateTimeUTC(timezone=True), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["community_id"],
            ["communities.id"],
            name=op.f("fk_community_memberships_community_id_communities"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_community_memberships_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_community_memberships")),
    )
    op.create_index(
        "uq_memberships_community_user_generation",
        "community_memberships",
        ["community_id", "user_id", "generation"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_index(
        "ix_memberships_community_user",
        "community_memberships",
        ["community_id", "user_id"],
        unique=False,
    )
    op.add_column(
        "auctions",
        sa.Column(
            "community_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True
        ),
    )
    op.create_index(op.f("ix_auctions_community_id"), "auctions", ["community_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_auctions_community_id_communities"),
        "auctions",
        "communities",
        ["community_id"],
        ["id"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(op.f("fk_auctions_community_id_communities"), "auctions", type_="foreignkey")
    op.drop_index(op.f("ix_auctions_community_id"), table_name="auctions")
    op.drop_column("auctions", "community_id")
    op.drop_index("ix_memberships_community_user", table_name="community_memberships")
    op.drop_index("uq_memberships_community_user_generation", table_name="community_memberships")
    op.drop_table("community_memberships")
    op.drop_index(op.f("ix_communities_owner_id"), table_name="communities")
    op.drop_table("communities")
