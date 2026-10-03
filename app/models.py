from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from advanced_alchemy.base import BigIntAuditBase, BigIntBase
from sqlalchemy import DateTime, ForeignKey, Index, Numeric, String, func
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

    __table_args__ = (Index("ix_auctions_status_ends_at", "status", "ends_at"),)


class Bid(BigIntAuditBase):
    __tablename__ = "bids"
    auction_id: Mapped[int] = mapped_column(ForeignKey("auctions.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    amount: Mapped[Decimal] = mapped_column(Money)
