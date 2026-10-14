from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import msgspec


class SignupRequest(msgspec.Struct):
    email: str
    password: str


class TokenResponse(msgspec.Struct):
    token: str


class TicketResponse(msgspec.Struct):
    ticket: str
    expires_at: datetime


class CreateAuctionRequest(msgspec.Struct):
    title: str
    starting_bid: Decimal
    ends_at: datetime  # must be timezone-aware, in the future
    currency: str = "IDR"  # ISO 4217; normalised and checked in create_auction
    status: str = "open"  # "draft" to keep it unannounced; nothing else is accepted


class EditAuctionRequest(msgspec.Struct):
    """Every field optional. None means "leave it alone", not "clear it", which is why
    there is nothing nullable on an auction worth clearing from here."""

    title: str | None = None
    starting_bid: Decimal | None = None
    ends_at: datetime | None = None


class BidRequest(msgspec.Struct):
    amount: Decimal


class AuctionResponse(msgspec.Struct):
    id: int
    title: str
    seller_id: int
    starting_bid: Decimal
    current_bid: Decimal | None
    current_winner_id: int | None
    bid_count: int
    currency: str
    status: str
    starts_at: datetime
    ends_at: datetime
    hard_ends_at: datetime
