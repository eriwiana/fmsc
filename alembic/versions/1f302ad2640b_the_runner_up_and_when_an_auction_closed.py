"""the runner-up and when an auction closed

Revision ID: 1f302ad2640b
Revises: d1390d743aff
Three columns the closing statement fills in one go, so that who was second and when the
hammer fell are recorded by the same transaction that announced the auction was over.

closed_at is backfilled from ends_at for auctions that are already closed. It is the best
available approximation — the real instant was never recorded — and the alternative is
worse: a NULL closed_at puts those auctions outside every settlement window for good, since
the grace period is a comparison against that column. Being slightly early is harmless here,
because anything closed before this deploy is long past a seven-day grace anyway.

runner_up_id is deliberately *not* backfilled. Working it out now would mean reading the bids
of auctions that closed under a statement which never considered them, which is exactly the
later query the milestone forbids. Those rows keep no runner-up, and a second-chance offer on
one of them is refused rather than invented.

Create Date: 2026-10-30 22:12:48.962672

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1f302ad2640b"
down_revision: Union[str, Sequence[str], None] = "d1390d743aff"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "auctions",
        sa.Column(
            "runner_up_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True
        ),
    )
    op.add_column("auctions", sa.Column("runner_up_bid", sa.Numeric(precision=18, scale=2)))
    op.add_column("auctions", sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        op.f("fk_auctions_runner_up_id_users"), "auctions", "users", ["runner_up_id"], ["id"]
    )
    op.execute("UPDATE auctions SET closed_at = ends_at WHERE status = 'closed'")
    # Autogenerate does not emit check constraints, so these are written by hand. Both are
    # guardrails against a writer that is not the closing statement: psql, a data migration,
    # or a later handler that decides to set one column and forget the other.
    op.create_check_constraint(
        "ck_auctions_runner_up_is_not_the_winner",
        "auctions",
        "runner_up_id IS NULL OR runner_up_id <> current_winner_id",
    )
    op.create_check_constraint(
        "ck_auctions_runner_up_has_a_bid",
        "auctions",
        "(runner_up_id IS NULL) = (runner_up_bid IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_auctions_runner_up_has_a_bid", "auctions", type_="check")
    op.drop_constraint("ck_auctions_runner_up_is_not_the_winner", "auctions", type_="check")
    op.drop_constraint(op.f("fk_auctions_runner_up_id_users"), "auctions", type_="foreignkey")
    op.drop_column("auctions", "closed_at")
    op.drop_column("auctions", "runner_up_bid")
    op.drop_column("auctions", "runner_up_id")
