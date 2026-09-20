from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from litestar import WebSocket, get, post, websocket
from litestar.channels import ChannelsPlugin
from litestar.di import Provide
from litestar.exceptions import ClientException, NotAuthorizedException, NotFoundException
from sqlalchemy import Row, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth import provide_current_user, user_for_token
from app.models import Auction, Bid, User
from app.schemas import AuctionResponse, BidRequest, CreateAuctionRequest

_authed = {"current_user": Provide(provide_current_user)}

# The app operates in Asia/Jakarta (WIB, UTC+7). Storage stays UTC (timestamptz); this only
# governs the boundaries — naive input is read as Jakarta wall time, output is rendered in it.
APP_TZ = ZoneInfo("Asia/Jakarta")


def resolve_deadline(dt: datetime) -> datetime:
    """A naive datetime is interpreted as Jakarta wall time; an aware one is left as-is."""
    return dt.replace(tzinfo=APP_TZ) if dt.tzinfo is None else dt


def _to_response(a: Auction | Row) -> AuctionResponse:
    """Works off either the ORM object or a raw RETURNING row — both expose the columns
    by name, and the bid path has only the row."""
    return AuctionResponse(
        id=a.id,
        title=a.title,
        seller_id=a.seller_id,
        starting_bid=a.starting_bid,
        current_bid=a.current_bid,
        current_winner_id=a.current_winner_id,
        bid_count=a.bid_count,
        status=a.status,
        starts_at=a.starts_at.astimezone(APP_TZ),
        ends_at=a.ends_at.astimezone(APP_TZ),
    )


# The Money column is Numeric(12,2). Postgres raises NumericValueOutOfRange above this,
# which asyncpg surfaces as a 500, and it silently rounds a third decimal place — 10.005
# becomes 10.01, so a bidder outbids by half a cent and is charged a whole one.
MAX_BID = Decimal("9999999999.99")


def _is_valid_amount(amount: Decimal) -> bool:
    """is_finite() first: ordering a Decimal NaN raises InvalidOperation rather than
    answering False the way a float does."""
    return (
        amount.is_finite() and Decimal(0) < amount <= MAX_BID and amount.as_tuple().exponent >= -2
    )


def _channel(auction_id: int) -> str:
    return f"auction:{auction_id}"


@post("/auctions", dependencies=_authed)
async def create_auction(
    data: CreateAuctionRequest, current_user: User, db_session: AsyncSession
) -> AuctionResponse:
    ends_at = resolve_deadline(data.ends_at)
    if ends_at <= datetime.now(timezone.utc):
        raise ClientException("ends_at must be in the future")
    if data.starting_bid <= 0:
        raise ClientException("starting_bid must be positive")
    auction = Auction(
        title=data.title,
        seller_id=current_user.id,
        starting_bid=data.starting_bid,
        ends_at=ends_at,
    )
    db_session.add(auction)
    await db_session.commit()
    await db_session.refresh(auction)
    return _to_response(auction)


@get("/auctions")
async def list_auctions(db_session: AsyncSession) -> list[AuctionResponse]:
    rows = await db_session.scalars(select(Auction).order_by(Auction.ends_at))
    return [_to_response(a) for a in rows]


@get("/auctions/{auction_id:int}")
async def get_auction(auction_id: int, db_session: AsyncSession) -> AuctionResponse:
    auction = await db_session.get(Auction, auction_id)
    if auction is None:
        raise NotFoundException("auction not found")
    return _to_response(auction)


# The entire correctness of an absolute auction: one conditional UPDATE. Postgres MVCC
# serializes concurrent bids, so no two bidders ever both win. Empty result = rejected.
#
# clock_timestamp(), not now(): now() is transaction_timestamp(), and the auth dependency
# has already opened the transaction by the time this runs, so now() reads a clock frozen
# before the request arrived. Only clock_timestamp() advances inside a transaction.
_BID_SQL = text("""
    UPDATE auctions
       SET current_bid = :amount, current_winner_id = :uid, bid_count = bid_count + 1
     WHERE id = :id AND status = 'open' AND ends_at > clock_timestamp()
       AND seller_id <> :uid
       AND :amount >= starting_bid
       AND (current_bid IS NULL OR :amount > current_bid)
    RETURNING *
    """)


async def place_bid_tx(
    session: AsyncSession, auction_id: int, user_id: int, amount: Decimal
) -> Row | None:
    """Atomic bid. Returns the auction row this bid wrote, or None if rejected.
    Commits/rolls back `session`.

    This is the whole money path — the handler and the tests both go through here.
    """
    if not _is_valid_amount(amount):
        await session.rollback()
        return None
    row = (
        await session.execute(_BID_SQL, {"amount": amount, "uid": user_id, "id": auction_id})
    ).first()
    if row is None:
        await session.rollback()
        return None
    # history row, same transaction as the accepted bid
    session.add(Bid(auction_id=auction_id, user_id=user_id, amount=amount))
    await session.commit()
    return row


async def reject_reason(
    session: AsyncSession, auction_id: int, user_id: int, amount: Decimal
) -> str:
    """Why the bid statement matched nothing. One extra read, on the reject path only.

    The row it reads is a moment newer than the one the statement saw, so this is a
    diagnosis and not a proof — the last line covers a row that moved in between. The
    order mirrors the guards in _BID_SQL so the wording matches what actually failed.
    """
    if not _is_valid_amount(amount):
        return f"amount must be between 0.01 and {MAX_BID}, to at most 2 decimal places"
    auction = await session.get(Auction, auction_id)
    if auction is None:
        return "auction not found"
    if auction.status != "open":
        return "auction is closed"
    if auction.ends_at <= datetime.now(timezone.utc):
        return "auction has ended"
    if auction.seller_id == user_id:
        return "a seller cannot bid on their own auction"
    if amount < auction.starting_bid:
        return f"bid must be at least {auction.starting_bid}"
    if auction.current_bid is not None and amount <= auction.current_bid:
        return f"bid must be above {auction.current_bid}"
    return "outbid while the bid was in flight"


@post("/auctions/{auction_id:int}/bids", dependencies=_authed)
async def place_bid(
    auction_id: int,
    data: BidRequest,
    current_user: User,
    db_session: AsyncSession,
    channels: ChannelsPlugin,
) -> AuctionResponse:
    auction = await place_bid_tx(db_session, auction_id, current_user.id, data.amount)
    if auction is None:
        raise ClientException(
            await reject_reason(db_session, auction_id, current_user.id, data.amount)
        )
    channels.publish(
        {
            "type": "bid",
            "auction_id": auction_id,
            "amount": str(data.amount),
            "winner_id": current_user.id,
            "bid_count": auction.bid_count,
        },
        _channel(auction_id),
    )
    return _to_response(auction)


@websocket("/ws/auctions/{auction_id:int}")
async def auction_ws(
    socket: WebSocket, auction_id: int, channels: ChannelsPlugin, db_session: AsyncSession
) -> None:
    await socket.accept()
    try:
        await user_for_token(db_session, socket.query_params.get("token", ""))
    except NotAuthorizedException:
        await socket.close(code=4401)
        return
    async with channels.start_subscription(_channel(auction_id)) as subscriber:
        async for event in subscriber.iter_events():
            await socket.send_text(event.decode() if isinstance(event, bytes) else event)


_CLOSE_SQL = text("""
    UPDATE auctions SET status = 'closed'
     WHERE status = 'open' AND ends_at <= now()
    RETURNING id, current_winner_id, current_bid
    """)


async def close_due(
    session_maker: async_sessionmaker[AsyncSession], channels: ChannelsPlugin
) -> int:
    """One closing pass. Returns how many auctions it closed.

    Separate from the loop so a test can run exactly one pass and assert on it.
    """
    async with session_maker() as session:
        rows = (await session.execute(_CLOSE_SQL)).all()
        await session.commit()
    for auction_id, winner_id, amount in rows:
        channels.publish(
            {
                "type": "closed",
                "auction_id": auction_id,
                "winner_id": winner_id,
                "amount": str(amount) if amount is not None else None,
            },
            _channel(auction_id),
        )
    return len(rows)


async def run_closer(
    session_maker: async_sessionmaker[AsyncSession], channels: ChannelsPlugin, interval: float = 1.0
) -> None:
    """Hard-deadline closer. Flips expired auctions to 'closed' and announces the winner.

    ponytail: in-process loop assumes a single instance; for multi-instance switch the
    UPDATE to `... FOR UPDATE SKIP LOCKED` semantics or a leader-elected ticker.
    """
    while True:
        try:
            await close_due(session_maker, channels)
        except Exception:  # noqa: BLE001 — a transient DB error must not kill the loop
            pass
        await asyncio.sleep(interval)
