from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import msgspec


class SignupRequest(msgspec.Struct):
    email: str
    password: str


class TokenResponse(msgspec.Struct):
    token: str


class CreateAuctionRequest(msgspec.Struct):
    title: str
    starting_bid: Decimal
    ends_at: datetime  # must be timezone-aware, in the future


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
    status: str
    starts_at: datetime
    ends_at: datetime
