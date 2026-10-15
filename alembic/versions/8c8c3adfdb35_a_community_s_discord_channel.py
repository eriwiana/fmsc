"""a community's discord channel

Revision ID: 8c8c3adfdb35
Revises: e19c1b884d50
Unique both ways. One channel per community, or an event arrives in it twice; one community
per guild, or a slash command run in that guild has no single community to answer for.

The snowflakes are stored as the strings Discord sends. They are 64-bit unsigned, which no
signed BIGINT holds all of, and nothing here does arithmetic on one.

Create Date: 2026-10-15 13:58:06.399729

"""

from typing import Sequence, Union

import advanced_alchemy
import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8c8c3adfdb35"
down_revision: Union[str, Sequence[str], None] = "e19c1b884d50"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "community_discord",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column(
            "community_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False
        ),
        sa.Column("guild_id", sa.String(length=20), nullable=False),
        sa.Column("channel_id", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at", advanced_alchemy.types.datetime.DateTimeUTC(timezone=True), nullable=False
        ),
        sa.Column(
            "updated_at", advanced_alchemy.types.datetime.DateTimeUTC(timezone=True), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["community_id"],
            ["communities.id"],
            name=op.f("fk_community_discord_community_id_communities"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_community_discord")),
        sa.UniqueConstraint("community_id", name=op.f("uq_community_discord_community_id")),
        sa.UniqueConstraint("guild_id", name=op.f("uq_community_discord_guild_id")),
    )


def downgrade() -> None:
    op.drop_table("community_discord")
