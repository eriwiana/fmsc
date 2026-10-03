"""single-use websocket tickets

Revision ID: 06f6290afbe2
Revises: 5d1b045aced3
Create Date: 2026-10-03 12:10:00.000000

No backfill: existing sessions keep working for HTTP, and a client that wants a socket
asks for a ticket. There is nothing to migrate because nothing was stored before.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "06f6290afbe2"
down_revision: Union[str, Sequence[str], None] = "5d1b045aced3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "ws_tickets",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_ws_tickets_user_id_users"),
        sa.PrimaryKeyConstraint("id", name="pk_ws_tickets"),
    )
    op.create_index("ix_ws_tickets_token", "ws_tickets", ["token"], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_ws_tickets_token", table_name="ws_tickets")
    op.drop_table("ws_tickets")
