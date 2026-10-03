from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from advanced_alchemy.base import BigIntAuditBase, BigIntBase
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

# Money is Numeric(12,2), never float. Max ~9.9 billion per bid, plenty.
Money = Numeric(12, 2)


class User(BigIntAuditBase):
    __tablename__ = "users"
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    pw_hash: Mapped[str] = mapped_column(String(255))


class Session(BigIntBase):
    __tablename__ = "sessions"
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))


class Auction(BigIntAuditBase):
    __tablename__ = "auctions"
    title: Mapped[str] = mapped_column(String(200))
    seller_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    starting_bid: Mapped[Decimal] = mapped_column(Money)
    # NULL current_bid = no bids yet, so the opening bid may equal starting_bid.
    current_bid: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    current_winner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    bid_count: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(10), default="open")  # open | closed
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # The latest ends_at may ever be extended to. Fixed when the auction is created:
    # without a ceiling, two bidders trading bids inside the window keep it open forever.
    hard_ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Counts events published about this auction. Bumped inside the same UPDATE that
    # accepts a bid or closes the auction, so the number cannot be handed out twice or
    # skipped — a client that sees 1 then 3 knows it missed one, which is the only way to
    # tell a dropped event from a quiet auction.
    event_seq: Mapped[int] = mapped_column(default=0)

    # Guardrails, not belt-and-braces: `update`, a data migration and psql all bypass the
    # Python checks in create_auction and _BID_SQL. A bid is a money path, so the table
    # refuses a bad row rather than trusting every writer to be careful.
    __table_args__ = (
        Index("ix_auctions_status_ends_at", "status", "ends_at"),
        # The bid clamps ends_at to hard_ends_at. A ceiling before the deadline is the one
        # shape where that clamp would shorten an auction instead of extending it. Equal is
        # allowed: an auction that may not be extended at all is legitimate.
        CheckConstraint("hard_ends_at >= ends_at", name="ceiling_after_deadline"),
        # Both the bid guard and the closer read status. A typo'd value makes an auction
        # unbiddable and uncloseable at once, and nothing would report it.
        CheckConstraint("status IN ('open', 'closed')", name="status_known"),
        CheckConstraint("starting_bid > 0", name="starting_bid_positive"),
        # An accepted bid below the advertised price.
        CheckConstraint(
            "current_bid IS NULL OR current_bid >= starting_bid",
            name="current_bid_at_least_starting",
        ),
        CheckConstraint("event_seq >= 0", name="event_seq_not_negative"),
    )


class Bid(BigIntAuditBase):
    __tablename__ = "bids"
    auction_id: Mapped[int] = mapped_column(ForeignKey("auctions.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    amount: Mapped[Decimal] = mapped_column(Money)
    # Idempotency. The key is the client's; `response` is the body the first attempt
    # returned, so a retry replays it instead of being told it outbid itself.
    #
    # ponytail: both live on the bid row rather than in their own table, because the row is
    # already keyed by (auction_id, user_id) and is written in the same transaction as the
    # money. Give them a table of their own when a second endpoint needs keys.
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    response: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # The history row is what a settlement would be computed from.
        CheckConstraint("amount > 0", name="amount_positive"),
        # The whole dedupe. No application-level "have I seen this key" test, which would
        # lose the race between two retries arriving together. NULLs are not equal in
        # Postgres, so bids without a key are unaffected. Name spelled in full because the
        # uq convention does not prefix an explicit one; `alembic check` catches that.
        UniqueConstraint(
            "auction_id",
            "user_id",
            "idempotency_key",
            name="uq_bids_auction_user_idempotency_key",
        ),
        CheckConstraint(
            "(idempotency_key IS NULL) = (response IS NULL)", name="key_and_response_together"
        ),
    )
