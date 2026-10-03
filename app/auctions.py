from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import msgspec
from litestar import Request, WebSocket, get, post, websocket
from litestar.channels import ChannelsPlugin
from litestar.di import Provide
from litestar.exceptions import (
    ClientException,
    HTTPException,
    NotAuthorizedException,
    NotFoundException,
)
from litestar.status_codes import HTTP_409_CONFLICT
from sqlalchemy import Row, bindparam, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth import provide_current_user, purge_expired_tickets, user_for_ticket
from app.models import Auction, Bid, Outbox, User
from app.schemas import AuctionResponse, BidRequest, CreateAuctionRequest

_authed = {"current_user": Provide(provide_current_user)}

logger = logging.getLogger(__name__)

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
        hard_ends_at=a.hard_ends_at.astimezone(APP_TZ),
    )


# The Money column is Numeric(12,2). Postgres raises NumericValueOutOfRange above this,
# which asyncpg surfaces as a 500, and it silently rounds a third decimal place — 10.005
# becomes 10.01, so a bidder outbids by half a cent and is charged a whole one.
MAX_BID = Decimal("9999999999.99")

# Anti-snipe. A bid inside the last SNIPE_WINDOW pushes the deadline that far out from the
# moment it lands, so a bid placed too late to be answered cannot win on timing alone.
# MAX_EXTENSION caps the total, via each auction's hard_ends_at.
#
# ponytail: both are global, not per-auction. Three columns and create-time validation buy
# nothing until a seller actually asks for a different window.
SNIPE_WINDOW = timedelta(seconds=60)
MAX_EXTENSION = timedelta(hours=2)


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
        hard_ends_at=ends_at + MAX_EXTENSION,
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
#
# The extension rides inside this same UPDATE. A second statement would leave a gap for the
# closer to fire in between, closing an auction the bid had just extended.
#
# GREATEST is outermost so the deadline is never pulled backwards; LEAST caps it at the
# ceiling; outside the window ends_at already wins, so nothing moves.
#
# ck_auctions_ceiling_after_deadline is what guarantees the first of those: with
# hard_ends_at >= ends_at enforced, the two nestings are algebraically identical, so no
# test can tell them apart. The ordering stays as written because it is still the correct
# one if that constraint is ever dropped.
#
# WHERE still reads the pre-update ends_at, so a bid that arrives after the deadline loses
# rather than extending its way back in.
_BID_SQL = text("""
    UPDATE auctions
       SET current_bid = :amount, current_winner_id = :uid, bid_count = bid_count + 1,
           event_seq = event_seq + 1,
           ends_at = GREATEST(ends_at, LEAST(hard_ends_at, clock_timestamp() + :window))
     WHERE id = :id AND status = 'open' AND ends_at > clock_timestamp()
       AND seller_id <> :uid
       AND :amount >= starting_bid
       AND (current_bid IS NULL OR :amount > current_bid)
    RETURNING *
    """)


# IETF draft-ietf-httpapi-idempotency-key-header spells it this way, and caps the key at
# the width of the column that stores it.
IDEMPOTENCY_HEADER = "Idempotency-Key"
MAX_IDEMPOTENCY_KEY = 128
_IDEMPOTENCY_CONSTRAINT = "uq_bids_auction_user_idempotency_key"


def _bid_event(row: Row, amount: Decimal) -> dict:
    """Built from the row the bid wrote, so the stored copy and the live one cannot drift."""
    return {
        "type": "bid",
        "auction_id": row.id,
        "amount": str(amount),
        "winner_id": row.current_winner_id,
        "bid_count": row.bid_count,
        "ends_at": row.ends_at.astimezone(APP_TZ).isoformat(),
        "seq": row.event_seq,
    }


def _closed_event(row: Row) -> dict:
    return {
        "type": "closed",
        "auction_id": row.id,
        "winner_id": row.current_winner_id,
        "amount": str(row.current_bid) if row.current_bid is not None else None,
        "ends_at": row.ends_at.astimezone(APP_TZ).isoformat(),
        "seq": row.event_seq,
    }


def _owed(row: Row, event: dict) -> Outbox:
    return Outbox(auction_id=row.id, seq=row.event_seq, payload=msgspec.json.encode(event).decode())


_MARK_SENT_SQL = text(
    "UPDATE outbox SET sent_at = clock_timestamp() WHERE auction_id = :aid AND seq = :seq"
)


async def mark_sent(session: AsyncSession, auction_id: int, seq: int) -> None:
    """Records that this event reached the channel, so the relay does not repeat it."""
    await session.execute(_MARK_SENT_SQL, {"aid": auction_id, "seq": seq})
    await session.commit()


# Publish happens while the row lock is held, and sent_at is set after. The other order
# would lose an event to a crash in between; this one repeats it, which the sequence
# number makes harmless. SKIP LOCKED so a second instance works the rest of the queue
# instead of waiting behind this one.
_RELAY_SQL = text("""
    SELECT id, auction_id, payload FROM outbox
     WHERE sent_at IS NULL
     ORDER BY auction_id, seq
       FOR UPDATE SKIP LOCKED
     LIMIT :limit
    """)


# Long enough that a problem can be investigated against the rows that caused it, short
# enough that the table does not grow for the life of the service. Sent rows are never
# read again by the relay.
OUTBOX_RETENTION = timedelta(days=1)

_MARK_BATCH_SENT_SQL = text(
    "UPDATE outbox SET sent_at = clock_timestamp() WHERE id IN :ids"
).bindparams(bindparam("ids", expanding=True))

# CAST, because inside a comparison Postgres cannot infer the parameter's type and
# answers "operator does not exist: timestamp with time zone < interval".
_PRUNE_OUTBOX_SQL = text(
    "DELETE FROM outbox WHERE sent_at IS NOT NULL"
    " AND sent_at < clock_timestamp() - CAST(:window AS interval)"
)


async def prune_outbox(session: AsyncSession, window: timedelta = OUTBOX_RETENTION) -> int:
    """Drop announced events past the retention window. Returns how many went."""
    result = await session.execute(_PRUNE_OUTBOX_SQL, {"window": window})
    await session.commit()
    return result.rowcount


async def relay_pending(
    session_maker: async_sessionmaker[AsyncSession], channels: ChannelsPlugin, limit: int = 100
) -> int:
    """Publish whatever a dead process left unannounced. Returns how many it sent."""
    async with session_maker() as session:
        rows = (await session.execute(_RELAY_SQL, {"limit": limit})).all()
        for _, auction_id, payload in rows:
            channels.publish(msgspec.json.decode(payload), _channel(auction_id))
        if rows:
            # One statement for the batch rather than one per row: a full tick was 100
            # round trips. Marked after publishing, so a crash in between repeats the
            # event rather than losing it.
            await session.execute(_MARK_BATCH_SENT_SQL, {"ids": [row[0] for row in rows]})
        await session.commit()
    return len(rows)


async def first_attempt(
    session: AsyncSession, auction_id: int, user_id: int, key: str
) -> Row | None:
    """The bid this key already placed: the amount it asked for and the body it returned."""
    return (
        await session.execute(
            select(Bid.amount, Bid.response).where(
                Bid.auction_id == auction_id,
                Bid.user_id == user_id,
                Bid.idempotency_key == key,
            )
        )
    ).first()


async def place_bid_tx(
    session: AsyncSession,
    auction_id: int,
    user_id: int,
    amount: Decimal,
    idempotency_key: str | None = None,
) -> Row | None:
    """Atomic bid. Returns the auction row this bid wrote, or None if rejected.
    Commits/rolls back `session`.

    This is the whole money path — the handler and the tests both go through here.
    """
    if not _is_valid_amount(amount):
        await session.rollback()
        return None
    row = (
        await session.execute(
            _BID_SQL,
            {"amount": amount, "uid": user_id, "id": auction_id, "window": SNIPE_WINDOW},
        )
    ).first()
    if row is None:
        await session.rollback()
        return None
    # history row, same transaction as the accepted bid
    bid = Bid(auction_id=auction_id, user_id=user_id, amount=amount)
    if idempotency_key is not None:
        # The key and the response it is replaying are written with the money, not after
        # it. A crash between the two would leave a bid whose retry places a second one.
        bid.idempotency_key = idempotency_key
        bid.response = msgspec.json.encode(_to_response(row)).decode()
    session.add(bid)
    # The obligation to announce this bid commits with the money or not at all.
    session.add(_owed(row, _bid_event(row, amount)))
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        # A request with this key got there first; reported as a refusal so the caller
        # keeps one path. The constraint name is read off asyncpg's own exception, which
        # sits on __cause__ behind SQLAlchemy's wrapper — not matched in the message text,
        # which a reworded driver error would silently turn into a 500.
        if getattr(exc.orig.__cause__, "constraint_name", None) == _IDEMPOTENCY_CONSTRAINT:
            return None
        # Unreached today; every other constraint on bids is enforced before the insert.
        # Kept so the next one added is not swallowed and reported as a refusal.
        raise
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
    request: Request,
    current_user: User,
    db_session: AsyncSession,
    channels: ChannelsPlugin,
) -> AuctionResponse:
    # Read the id before the bid runs. place_bid_tx rolls back on rejection, and a rollback
    # expires every instance in the session regardless of expire_on_commit — so touching
    # current_user afterwards attempts a lazy reload, which raises MissingGreenlet inside an
    # async session and turns every rejected bid into a 500 with no reason in it.
    user_id = current_user.id
    # `or None`: a bare `Idempotency-Key:` is a client sending the header by accident, not
    # one asking to be deduplicated under the key "". Length is checked because the column
    # is 128 wide and Postgres answers an over-long value with a DataError, which is not an
    # IntegrityError and would reach the client as a 500.
    key = request.headers.get(IDEMPOTENCY_HEADER) or None
    if key is not None and len(key) > MAX_IDEMPOTENCY_KEY:
        raise ClientException(
            f"{IDEMPOTENCY_HEADER} must be at most {MAX_IDEMPOTENCY_KEY} characters"
        )
    auction = await place_bid_tx(db_session, auction_id, user_id, data.amount, key)
    if auction is None:
        # Every duplicate arrives here, which is why there is no "have I seen this key"
        # test before the bid. A retry carrying the same amount can never be accepted
        # twice: after the first one, current_bid equals that amount, so
        # `:amount > current_bid` refuses it. A retry whose amount was edited upward clears
        # that guard instead and is refused by the unique key.
        if key is not None:
            first = await first_attempt(db_session, auction_id, user_id, key)
            if first is not None:
                # Same key, different body: no second bid was placed either way, so this
                # is only about whether the client finds out. Replaying silently would let
                # it believe it placed a bid it never placed.
                #
                # ponytail: the request body is one field, so the stored amount is the
                # whole fingerprint. Hash the body if BidRequest ever grows.
                if first.amount != data.amount:
                    raise HTTPException(
                        status_code=HTTP_409_CONFLICT,
                        detail=f"{IDEMPOTENCY_HEADER} was already used with a different amount",
                    )
                # Decoded back into the struct so the reply goes through the same encoder
                # litestar used the first time and the two bodies are identical.
                return msgspec.json.decode(first.response, type=AuctionResponse)
        raise ClientException(await reject_reason(db_session, auction_id, user_id, data.amount))
    # Published here rather than left to the relay: a bid that waited for the next tick
    # would reach watchers a second late. The outbox row is the safety net, not the path.
    #
    # The event carries ends_at because a bid inside the window moves the deadline —
    # without it a watcher's countdown runs out on a deadline that no longer exists.
    channels.publish(_bid_event(auction, data.amount), _channel(auction_id))
    await mark_sent(db_session, auction_id, auction.event_seq)
    return _to_response(auction)


@websocket("/ws/auctions/{auction_id:int}")
async def auction_ws(
    socket: WebSocket, auction_id: int, channels: ChannelsPlugin, db_session: AsyncSession
) -> None:
    await socket.accept()
    try:
        # A ticket, not the session token: the credential is in a URL, and a URL is
        # logged. Spending the ticket here is what makes it single-use.
        await user_for_ticket(db_session, socket.query_params.get("ticket", ""))
    except NotAuthorizedException:
        await socket.close(code=4401)
        return

    # Sending from a background task and then blocking on receive() is what makes a
    # disconnect observable: receive() raises the moment the client goes away. Iterating
    # the subscription in the foreground instead parks here until the next event fails to
    # send — and on a quiet auction there is no next event, so the subscription and this
    # handler's database session are held for the life of the process.
    #
    # The client is not expected to send anything; whatever arrives is discarded.
    async with channels.start_subscription(_channel(auction_id)) as subscriber:
        auction = await db_session.get(Auction, auction_id)
        if auction is None:
            await socket.close(code=4404)
            return
        # Order matters, and it is the whole point of the snapshot. The subscription is
        # already live, so a bid landing from here on is queued; the snapshot goes out
        # before anything drains that queue. Snapshot first and subscribe second would
        # lose any event that arrived in between — which is the gap a client reconnecting
        # after a dropout falls into.
        await socket.send_text(
            msgspec.json.encode(
                {
                    "type": "snapshot",
                    "auction": _to_response(auction),
                    "seq": auction.event_seq,
                }
            ).decode()
        )
        # Nothing below touches the database, and the loop runs for as long as the client
        # stays. Held open, this session is one Postgres connection per open socket —
        # measured at 8 sockets, 7 connections — which would hit max_connections long
        # before the 500 sockets M5 is meant to sustain.
        await db_session.close()

        # The number the client has been brought up to. Every event is measured against
        # it, which is what makes the sequence worth carrying.
        last_seq = auction.event_seq

        closed = False

        async def send(event: bytes | str) -> None:
            nonlocal last_seq, closed
            if closed:
                # A second dropped event would otherwise close an already closed socket.
                # Litestar tolerates that today; saying so here does not rely on it.
                return
            payload = event.decode() if isinstance(event, bytes) else event
            seq = msgspec.json.decode(payload)["seq"]
            if seq <= last_seq:
                # Already delivered. The outbox relays at-least-once, so a duplicate is
                # expected rather than exceptional, and showing the same bid twice would
                # make a watcher think the price moved when it did not.
                return
            if seq > last_seq + 1:
                # Events were dropped from this socket's queue, so it cannot be made
                # whole here. Closing sends the client back through the snapshot, which
                # is the only path that restores correct state. Staying connected would
                # show a price that silently skipped a bid.
                closed = True
                await socket.close(code=4408)
                return
            last_seq = seq
            await socket.send_text(payload)

        async with subscriber.run_in_background(send):
            while True:
                await socket.receive()


# SKIP LOCKED so a second instance is not stuck behind the first. Without it the closers
# serialize: the loser blocks on the row lock for the whole of the winner's transaction
# before finding out it has nothing to do. Correctness never needed it — the conditional
# UPDATE already re-checks `status` under the lock, so only one closer ever wins the row.
#
# clock_timestamp() matches the bid statement. A fresh session per tick means now() reads
# almost the same instant today, so this is not observable; it is here so that a later
# change which opens the transaction earlier cannot reintroduce the bug M1 fixed.
#
# ponytail: the batch is unbounded. Add `ORDER BY ends_at LIMIT n` to the CTE if a single
# tick ever closes enough auctions for one transaction to matter.
_CLOSE_SQL = text("""
    WITH due AS (
        SELECT id FROM auctions
         WHERE status = 'open' AND ends_at <= clock_timestamp()
           FOR UPDATE SKIP LOCKED
    )
    UPDATE auctions a SET status = 'closed', event_seq = a.event_seq + 1
      FROM due
     WHERE a.id = due.id
    RETURNING a.id, a.current_winner_id, a.current_bid, a.ends_at, a.event_seq,
              clock_timestamp() - a.ends_at AS late_by
    """)


async def close_due(
    session_maker: async_sessionmaker[AsyncSession], channels: ChannelsPlugin
) -> int:
    """One closing pass. Returns how many auctions it closed.

    Separate from the loop so a test can run exactly one pass and assert on it.
    """
    async with session_maker() as session:
        rows = (await session.execute(_CLOSE_SQL)).all()
        events = [_closed_event(row) for row in rows]
        # Same transaction as the status flip: a crash here shuts an auction for good with
        # nobody told, and the auction cannot be reopened to try again.
        session.add_all([_owed(row, event) for row, event in zip(rows, events, strict=True)])
        await session.commit()
    for row, event in zip(rows, events, strict=True):
        # How far past its deadline an auction actually closed. The interval is measured
        # by Postgres, so it covers the poll interval and any time the tick spent queued.
        logger.info(
            "closed auction %s %.3fs after its deadline", row.id, row.late_by.total_seconds()
        )
        channels.publish(event, _channel(row.id))
    if rows:
        async with session_maker() as session:
            for row in rows:
                await mark_sent(session, row.id, row.event_seq)
    return len(rows)


async def run_closer(
    session_maker: async_sessionmaker[AsyncSession], channels: ChannelsPlugin, interval: float = 1.0
) -> None:
    """Hard-deadline closer. Flips expired auctions to 'closed' and announces the winner."""
    while True:
        try:
            await close_due(session_maker, channels)
            # Anything a previous process committed but never announced.
            await relay_pending(session_maker, channels)
            async with session_maker() as session:
                await purge_expired_tickets(session)
                await prune_outbox(session)
        except Exception:
            # Blind on purpose: a transient database error must not kill the loop. BLE001
            # allows it because of the logger.exception call, which is what makes the
            # swallowed error findable rather than silent.
            logger.exception("closer tick failed")
        await asyncio.sleep(interval)
