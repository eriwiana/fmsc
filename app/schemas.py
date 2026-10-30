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
    reserve: Decimal | None = None  # the seller's floor, never echoed to a bidder
    community_id: int | None = None  # None is a public auction; set it and bidding is gated


class EditAuctionRequest(msgspec.Struct):
    """Every field optional. None means "leave it alone", not "clear it", which is why
    there is nothing nullable on an auction worth clearing from here."""

    title: str | None = None
    starting_bid: Decimal | None = None
    reserve: Decimal | None = None
    ends_at: datetime | None = None


class BidRequest(msgspec.Struct):
    amount: Decimal


class CreateCommunityRequest(msgspec.Struct):
    name: str


class CommunityResponse(msgspec.Struct):
    id: int
    name: str
    owner_id: int
    membership_generation: int


class IssueCodeRequest(msgspec.Struct):
    label: str = ""
    max_uses: int = 1
    expires_at: datetime | None = None


class CodeResponse(msgspec.Struct):
    """The one and only time the code is readable. Nothing stores it."""

    code: str
    # Whether the community's Discord channel was told. False also means there is no
    # channel, which is the common case — reported rather than hidden, because the owner
    # is the only one who can then pass the code on themselves.
    broadcast: bool = False


class SettlementResponse(msgspec.Struct):
    """Where a closed auction has got to, as the caller left it.

    Booleans rather than the timestamps themselves: the only question either side asks is
    whether the other has marked it, and an exact instant is the counterparty's business.
    """

    auction_id: int
    buyer_id: int
    seller_settled: bool
    buyer_settled: bool
    # Both sides, which is the only definition of settled there is. Derived here rather
    # than stored, so there is no third value that can contradict the other two.
    settled: bool


class DiscordLinkRequest(msgspec.Struct):
    """Snowflakes as Discord sends them: strings, because a 64-bit unsigned id does not
    survive a JSON number."""

    guild_id: str
    channel_id: str


class GenerationResponse(msgspec.Struct):
    membership_generation: int


class RedeemRequest(msgspec.Struct):
    code: str


class MembershipResponse(msgspec.Struct):
    community_id: int
    generation: int


class AuctionResponse(msgspec.Struct):
    id: int
    title: str
    seller_id: int
    starting_bid: Decimal
    current_bid: Decimal | None
    current_winner_id: int | None
    bid_count: int
    currency: str
    # NULL for everyone but the seller, whether or not one is set. See _to_response.
    reserve: Decimal | None
    status: str
    starts_at: datetime
    ends_at: datetime
    hard_ends_at: datetime
