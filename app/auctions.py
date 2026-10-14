from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import msgspec
from litestar import Request, WebSocket, get, patch, post, websocket
from litestar.channels import ChannelsPlugin
from litestar.di import Provide
from litestar.exceptions import (
    ClientException,
    HTTPException,
    NotAuthorizedException,
    NotFoundException,
    PermissionDeniedException,
)
from litestar.status_codes import HTTP_409_CONFLICT
from sqlalchemy import Row, bindparam, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth import (
    provide_current_user,
    prune_login_attempts,
    purge_expired_sessions,
    purge_expired_tickets,
    user_for_ticket,
)
from app.models import Auction, Bid, Outbox, User
from app.schemas import (
    AuctionResponse,
    BidRequest,
    CreateAuctionRequest,
    EditAuctionRequest,
)

_authed = {"current_user": Provide(provide_current_user)}

logger = logging.getLogger(__name__)

# The app operates in Asia/Jakarta (WIB, UTC+7). Storage stays UTC (timestamptz); this only
# governs the boundaries — naive input is read as Jakarta wall time, output is rendered in it.
APP_TZ = ZoneInfo("Asia/Jakarta")


def resolve_deadline(dt: datetime) -> datetime:
    """A naive datetime is interpreted as Jakarta wall time; an aware one is left as-is."""
    return dt.replace(tzinfo=APP_TZ) if dt.tzinfo is None else dt


def _to_response(a: Auction | Row, *, reveal_reserve: bool = False) -> AuctionResponse:
    """Works off either the ORM object or a raw RETURNING row — both expose the columns
    by name, and the bid path has only the row.

    The reserve is the seller's own number and is withheld by default: a bidder who can
    read it can bid exactly it, which turns the seller's floor into their ceiling. The
    seller's own routes pass reveal_reserve, because a seller who cannot read back what
    they set cannot tell a typo from a decision.
    """
    return AuctionResponse(
        id=a.id,
        title=a.title,
        seller_id=a.seller_id,
        starting_bid=a.starting_bid,
        current_bid=a.current_bid,
        current_winner_id=a.current_winner_id,
        bid_count=a.bid_count,
        currency=a.currency,
        reserve=a.reserve if reveal_reserve else None,
        status=a.status,
        starts_at=a.starts_at.astimezone(APP_TZ),
        ends_at=a.ends_at.astimezone(APP_TZ),
        hard_ends_at=a.hard_ends_at.astimezone(APP_TZ),
    )


# The lifecycle, as a table of edges rather than a tree of ifs. A draft is a listing the
# seller has not announced yet; cancelled is one withdrawn before anybody committed to it;
# closed is what the deadline does.
#
# Keyed by destination, because that is what a caller asks for — "publish this" — and the
# question the statement has to answer is whether the row is in a state that may go there.
# An empty tuple means nothing may reach that state: nothing re-drafts an auction once it
# has been announced, since the draft is the state before anyone has seen it.
#
# The table is the only place the machine is written down. _TRANSITION_SQL reads it as a
# parameter, so a new edge is one entry here and no new SQL.
#
# `closed` is in the table because it is an edge of the machine, but nothing calls
# transition() with it: the closer owns that edge and has to, because it closes every due
# auction in one statement and writes an event per row. Its own WHERE carries the same
# guard, and there is a test that it leaves a draft and a cancelled auction alone.
LEGAL_FROM: dict[str, tuple[str, ...]] = {
    "draft": (),
    "open": ("draft",),
    "closed": ("open",),
    "cancelled": ("draft", "open"),
}
STATUSES = tuple(LEGAL_FROM)

# One guarded UPDATE. The legality, the ownership and the bid check are all in the WHERE,
# so there is no window between deciding and moving: two statements would let a bid land
# between the check and the cancel, and the cancel would still go through.
#
# `status = ANY(...)` with an empty array matches nothing, which is how a
# destination no edge reaches refuses itself without a special case. A same-state request
# is refused for free: a state is never listed among the ones that may reach it. The CAST
# is what asyncpg needs to type the list; without it the parameter arrives as a scalar and
# every transition is refused.
#
# Two of the conditions are per destination. bid_count is only consulted for a cancel:
# closing an auction that has bids is the point of closing it, and publishing one cannot
# have any. The deadline is only consulted for a publish, because a draft sat on past its
# own ends_at would open and then be closed by the very next closer tick — an auction that
# was never biddable, announced to everyone watching.
#
# A publish also sets starts_at, because an auction starts when it is announced and not
# when its draft was first saved. The community bid guard compares a kick against
# starts_at, so leaving it at insert time means somebody kicked on Tuesday may still bid on
# a lot drafted Monday and published Wednesday — the kick would reach backwards past its
# own date.
_TRANSITION_SQL = text("""
    UPDATE auctions
       SET status = :to,
           event_seq = event_seq + 1,
           starts_at = CASE WHEN :to = 'open' THEN clock_timestamp() ELSE starts_at END
     WHERE id = :id
       AND seller_id = :seller_id
       AND status = ANY(CAST(:from_states AS varchar[]))
       AND (:to <> 'cancelled' OR bid_count = 0)
       AND (:to <> 'open' OR ends_at > clock_timestamp())
    RETURNING *
    """)


async def transition(
    session: AsyncSession, auction_id: int, to: str, *, seller_id: int
) -> Row | None:
    """Move an auction to `to`, or return None because the edge was not available.

    None covers every refusal — illegal edge, wrong owner, bids already placed, no such
    auction — on purpose. The caller cannot act on the difference, and spelling it out
    would tell a stranger which auctions exist and how many bids they have.
    """
    row = (
        await session.execute(
            _TRANSITION_SQL,
            {
                "id": auction_id,
                "to": to,
                "seller_id": seller_id,
                "from_states": list(LEGAL_FROM.get(to, ())),
            },
        )
    ).first()
    if row is None:
        await session.rollback()
        return None
    # The event rides in the same transaction as the move, for the same reason a bid's
    # does. event_seq counts events *published* about an auction, and the socket closes a
    # subscriber that sees a number skipped — so a bump with no event would tell a watcher
    # it had missed something that never existed. It is also the only way a watcher hears
    # that the lot was withdrawn: the closer skips a cancelled auction, so no later event
    # ever arrives.
    session.add(_owed(row, _status_event(row)))
    await session.commit()
    return row


# The Money column is Numeric(18,2), and this is exactly what it holds. Postgres raises
# NumericValueOutOfRange above it, which asyncpg surfaces as a 500, and it silently rounds
# a third decimal place — 10.005 becomes 10.01, so a bidder outbids by half a cent and is
# charged a whole one.
MAX_BID = Decimal("9999999999999999.99")

# What the table's own check constraint accepts, spelled the same way. Validated here so
# the refusal is a 400 naming the field, not the 500 an IntegrityError becomes.
CURRENCY_CODE = re.compile(r"[A-Z]{3}")

# The increment table. A bid has to beat the leader by this much, not by a cent: penny
# wars are what bots are for, and an auction decided by who can issue the most requests is
# not an auction. Bands rather than a percentage so the step is a round number a bidder can
# predict, and rather than a per-auction column so there is no knob for a seller to get
# wrong. IDR-shaped, because IDR is the only currency in use; a second currency needs its
# own table, which is why the numbers are here and not in the SQL.
#
# Chosen, not measured. The milestone records that.
#
# (ceiling, step): the step applies while the current bid is under the ceiling. The last
# entry's ceiling is None, which is everything above.
BID_INCREMENTS: tuple[tuple[Decimal | None, Decimal], ...] = (
    (Decimal("1000000"), Decimal("10000")),
    (Decimal("10000000"), Decimal("100000")),
    (None, Decimal("500000")),
)


def bid_increment(current: Decimal) -> Decimal:
    """The step a bid must clear to beat `current`."""
    return next(step for ceiling, step in BID_INCREMENTS if ceiling is None or current < ceiling)


def _increment_case(column: str) -> str:
    """The same table as SQL, generated rather than written out twice.

    Two copies would drift, and the copy that drifted would be the message rather than the
    rule — a bidder told to bid 20,000 and then refused at 20,000. The values are Decimals
    from the tuple above, so there is nothing here a caller can reach.
    """
    whens = " ".join(
        f"WHEN {column} < {ceiling} THEN {step}"
        for ceiling, step in BID_INCREMENTS
        if ceiling is not None
    )
    return f"CASE {whens} ELSE {BID_INCREMENTS[-1][1]} END"


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


# Who may open an auction inside a community: its owner, or a member of the current
# generation whose membership has not been revoked. FOR SHARE takes the community row
# alongside the answer, which is what serialises this against a remove-all.
_MAY_LIST_SQL = text("""
    SELECT 1
      FROM communities c
     WHERE c.id = :community_id
       AND (c.owner_id = :uid
            OR EXISTS (SELECT 1
                         FROM community_memberships m
                        WHERE m.community_id = c.id
                          AND m.user_id = :uid
                          AND m.generation = c.membership_generation
                          AND m.revoked_at IS NULL))
       FOR SHARE
    """)


@post("/auctions", dependencies=_authed)
async def create_auction(
    data: CreateAuctionRequest, current_user: User, db_session: AsyncSession
) -> AuctionResponse:
    # Selling, not bidding: an auction is a promise to deliver, and the seller is who a
    # buyer has to be able to reach. Bidding stays open to an unverified account on
    # purpose — the money only moves at settlement, which is where M11 can ask again.
    #
    # First, before the payload is looked at, so an unverified caller is told the one
    # thing they can act on. Validating first would answer 400 on the field they got
    # wrong and 403 only once they fixed it, which reads as the field being the problem.
    if current_user.verified_at is None:
        raise PermissionDeniedException("verify your email address before selling")
    ends_at = resolve_deadline(data.ends_at)
    if ends_at <= datetime.now(timezone.utc):
        raise ClientException("ends_at must be in the future")
    if data.starting_bid <= 0:
        raise ClientException("starting_bid must be positive")
    # ck_auctions_reserve_at_least_starting enforces this too. Checked here so the refusal
    # is a 400 naming the field rather than the 500 an IntegrityError becomes.
    if data.reserve is not None and data.reserve < data.starting_bid:
        raise ClientException("reserve must be at least the starting bid")
    # Normalised here rather than relied on from the client: the table refuses anything
    # that is not three uppercase letters, and that refusal would arrive as a 500.
    currency = data.currency.strip().upper()
    if not CURRENCY_CODE.fullmatch(currency):
        raise ClientException("currency must be a three-letter ISO 4217 code")
    # Opt-in, and only these two: a client asking to be created closed or cancelled is
    # asking for a state the machine reaches by an edge, not by a POST.
    if data.status not in ("open", "draft"):
        raise ClientException("status must be open or draft")
    community_id = data.community_id
    if community_id is not None:
        # FOR SHARE on the community, before the insert. remove_all_members takes the same
        # row FOR UPDATE, so the two serialise and a bump cannot land between this check
        # and the row arriving — a lock only helps where both sides take it.
        #
        # The same statement answers who may list here. Without it any verified seller
        # could plant a lot inside somebody else's community by guessing an id, and its
        # members would be gated into bidding on a stranger's auction.
        allowed = (
            await db_session.execute(
                _MAY_LIST_SQL, {"community_id": community_id, "uid": current_user.id}
            )
        ).first()
        if allowed is None:
            raise PermissionDeniedException("only a member of that community can list in it")
    auction = Auction(
        title=data.title,
        community_id=community_id,
        seller_id=current_user.id,
        starting_bid=data.starting_bid,
        currency=currency,
        reserve=data.reserve,
        status=data.status,
        ends_at=ends_at,
        hard_ends_at=ends_at + MAX_EXTENSION,
    )
    db_session.add(auction)
    await db_session.commit()
    await db_session.refresh(auction)
    return _to_response(auction, reveal_reserve=True)


@get("/auctions")
async def list_auctions(db_session: AsyncSession) -> list[AuctionResponse]:
    """Everything but the drafts.

    A draft is the state before anyone has seen the listing, so publishing it here would
    make the draft pointless. Cancelled and closed auctions stay: a bidder who was
    watching needs to see why bidding stopped, and the results are the archive.
    """
    rows = await db_session.scalars(
        select(Auction).where(Auction.status != "draft").order_by(Auction.ends_at)
    )
    return [_to_response(a) for a in rows]


# Registered before /auctions/{auction_id:int} would matter in a framework that matched
# routes in order; litestar matches the literal path first either way, and the test below
# is what says so rather than the ordering here.
@get("/auctions/mine", dependencies=_authed)
async def my_auctions(current_user: User, db_session: AsyncSession) -> list[AuctionResponse]:
    """Criterion 5. Every auction this caller sells, whatever its status.

    The seller's own view, which is why it is the one place drafts appear and the one
    place the reserve is echoed back. Filtered by seller_id in the WHERE rather than by
    reading and then checking: a filter that happens in Python is one somebody later
    forgets to apply to a second query.

    Newest first, by id. The public list is ordered by deadline because a bidder wants
    what ends soonest; a seller wants what they just created, and a draft has no
    meaningful deadline to sort by yet.
    """
    rows = await db_session.scalars(
        select(Auction).where(Auction.seller_id == current_user.id).order_by(Auction.id.desc())
    )
    return [_to_response(a, reveal_reserve=True) for a in rows]


@get("/auctions/{auction_id:int}")
async def get_auction(auction_id: int, db_session: AsyncSession) -> AuctionResponse:
    auction = await db_session.get(Auction, auction_id)
    # A draft answers 404 rather than 403: this route takes no credentials, and a 403
    # would confirm the id belongs to something. The seller reads their drafts from
    # /auctions/mine.
    if auction is None or auction.status == "draft":
        raise NotFoundException("auction not found")
    return _to_response(auction)


# Criterion 2's other half. Same shape as the transition: the guards are in the WHERE, so
# a bid landing between a check and the write cannot be edited out from under.
#
# bid_count = 0 is the rule. Bidders bid on the description, so changing it afterwards is a
# bait-and-switch, and raising starting_bid after a bid would retroactively invalidate a bid
# that had already been accepted.
#
# NULL means unchanged, so COALESCE does the whole patch in one statement. hard_ends_at
# rides on :ends_at rather than on the stored column: NULL + interval is NULL, so the
# COALESCE falls through to the existing ceiling and an edit that leaves the deadline alone
# does not move it either.
#
# status IN ('draft', 'open') and not just a bid count: a closed auction has a result and a
# cancelled one is withdrawn, and editing the description of either rewrites history.
#
# The last condition pairs whichever of the two numbers the request carries with whichever
# it leaves alone, and refuses the combination the table would refuse anyway. In the WHERE
# rather than read and then checked, because a read would be a second statement and the
# window between them is where the 500 lives.
#
# The IS NULL arm is not decoration: without it an auction with no reserve compares NULL
# against its starting bid, which is NULL rather than true, and every edit is refused.
_EDIT_SQL = text("""
    UPDATE auctions
       SET title = COALESCE(:title, title),
           starting_bid = COALESCE(:starting_bid, starting_bid),
           reserve = COALESCE(:reserve, reserve),
           ends_at = COALESCE(:ends_at, ends_at),
           hard_ends_at = COALESCE(:ends_at + :extension, hard_ends_at)
     WHERE id = :id
       AND seller_id = :seller_id
       AND bid_count = 0
       AND status IN ('draft', 'open')
       AND (COALESCE(:reserve, reserve) IS NULL
            OR COALESCE(:reserve, reserve) >= COALESCE(:starting_bid, starting_bid))
    RETURNING *
    """)


@patch("/auctions/{auction_id:int}", dependencies=_authed)
async def edit_auction(
    auction_id: int,
    data: EditAuctionRequest,
    current_user: User,
    db_session: AsyncSession,
) -> AuctionResponse:
    """Fix a listing nobody has bid on yet. 409 once a bid exists.

    The validation repeats create_auction's rather than sharing it: there are two call
    sites and the checks are three lines, so a helper would be an indirection per field
    for no fewer rules.
    """
    ends_at = resolve_deadline(data.ends_at) if data.ends_at is not None else None
    if ends_at is not None and ends_at <= datetime.now(timezone.utc):
        raise ClientException("ends_at must be in the future")
    if data.starting_bid is not None and data.starting_bid <= 0:
        raise ClientException("starting_bid must be positive")
    row = (
        await db_session.execute(
            _EDIT_SQL,
            {
                "id": auction_id,
                "seller_id": current_user.id,
                "title": data.title,
                "starting_bid": data.starting_bid,
                "reserve": data.reserve,
                "ends_at": ends_at,
                "extension": MAX_EXTENSION,
            },
        )
    ).first()
    await db_session.commit()
    if row is None:
        # One read, on the refusal path only, to tell "you may not edit this" from "these
        # two numbers do not go together" — the second is a 400 the seller can act on, and
        # answering 409 for it would send them looking for a bid that is not there.
        auction = await db_session.get(Auction, auction_id)
        if auction is not None and auction.seller_id == current_user.id:
            reserve = data.reserve if data.reserve is not None else auction.reserve
            floor = data.starting_bid if data.starting_bid is not None else auction.starting_bid
            if reserve is not None and reserve < floor:
                raise ClientException("reserve must be at least the starting bid")
        raise HTTPException(
            status_code=HTTP_409_CONFLICT,
            detail="an auction can only be edited by its seller, before its first bid,"
            " and only while it is a draft or open",
        )
    return _to_response(row, reveal_reserve=True)


async def _announce(db_session: AsyncSession, channels: ChannelsPlugin, row: Row) -> None:
    """Publish the move the transition wrote, then mark it sent. Same shape as the bid
    path: published here rather than left to the relay so a watcher hears it now, with
    the outbox row as the safety net rather than the route.

    An unsent row is picked up by the next relay tick and the watcher still hears it, just
    a tick late — which is why the test asserts sent_at as well as the published event.
    """
    channels.publish(_status_event(row), _channel(row.id))
    await mark_sent(db_session, row.id, row.event_seq)


@post("/auctions/{auction_id:int}/publish", dependencies=_authed)
async def publish_auction(
    auction_id: int, current_user: User, db_session: AsyncSession, channels: ChannelsPlugin
) -> AuctionResponse:
    """Announce a draft. The only edge into open."""
    row = await transition(db_session, auction_id, "open", seller_id=current_user.id)
    if row is None:
        raise HTTPException(
            status_code=HTTP_409_CONFLICT,
            detail="only the seller can publish their own auction, and only while it is a"
            " draft whose deadline is still ahead",
        )
    await _announce(db_session, channels, row)
    return _to_response(row, reveal_reserve=True)


@post("/auctions/{auction_id:int}/cancel", dependencies=_authed)
async def cancel_auction(
    auction_id: int, current_user: User, db_session: AsyncSession, channels: ChannelsPlugin
) -> AuctionResponse:
    """Withdraw a listing nobody has bid on. 409 once a bid exists, because by then the
    lot is somebody else's commitment and not only the seller's to withdraw."""
    row = await transition(db_session, auction_id, "cancelled", seller_id=current_user.id)
    if row is None:
        raise HTTPException(
            status_code=HTTP_409_CONFLICT,
            detail="an auction can only be cancelled by its seller, before its first bid",
        )
    await _announce(db_session, channels, row)
    return _to_response(row, reveal_reserve=True)


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
# Membership rides in the same WHERE as everything else, which is the whole of criterion
# 1. A handler that read the membership and then bid would be two statements, and a
# revocation landing in the gap between them would still see the bid accepted; this EXISTS
# is re-read by the UPDATE under its own row lock, so a committed removal is seen. The
# generation comparison is what makes remove-all work: it bumps the community's counter
# rather than deleting rows, so every membership from the generation before it stops
# counting at once while the audit trail survives.
#
# community_id IS NULL first, so a public auction is not a special case and costs no
# subquery. Viewing is public either way — only bidding is gated.
#
# The revoked_at arm is compared against the auction's start, not against now(): a kick
# takes effect only for auctions opened after it. Bidding on a lot somebody was entitled
# to bid on when it opened is a commitment the seller has already relied on, and letting a
# moderator cancel it afterwards makes a kick a way to undo other people's bids. The
# auction they were kicked out of goes on; the next one is closed to them.
#
# The increment rides in the same WHERE for the same reason as the reserve, and it only
# applies once there is a leader: an opening bid has nothing to beat, so it may still equal
# starting_bid.
#
# WHERE still reads the pre-update ends_at, so a bid that arrives after the deadline loses
# rather than extending its way back in.
#
# The reserve is a floor in this same WHERE, which is the whole of criterion 3: a handler
# that read the reserve and then decided would be two statements, and the reserve could be
# edited between them. COALESCE(reserve, starting_bid) means no reserve is not a special
# case. The starting_bid guard above is kept even though the constraint
# reserve >= starting_bid makes it redundant: if that constraint is ever dropped, a reserve
# set below the advertised price must not become a way under the advertised price.
_BID_SQL = text(
    """
    UPDATE auctions
       SET current_bid = :amount, current_winner_id = :uid, bid_count = bid_count + 1,
           event_seq = event_seq + 1,
           ends_at = GREATEST(ends_at, LEAST(hard_ends_at, clock_timestamp() + :window))
     WHERE id = :id AND status = 'open' AND ends_at > clock_timestamp()
       AND seller_id <> :uid
       AND :amount >= starting_bid
       AND :amount >= COALESCE(reserve, starting_bid)
       AND (community_id IS NULL OR EXISTS (
                SELECT 1
                  FROM community_memberships m
                  JOIN communities c ON c.id = m.community_id
                 WHERE m.community_id = auctions.community_id
                   AND m.user_id = :uid
                   AND m.generation = c.membership_generation
                   AND (m.revoked_at IS NULL OR auctions.starts_at < m.revoked_at)))
       AND (current_bid IS NULL OR :amount >= current_bid + """
    + _increment_case("current_bid")
    + """)
    RETURNING *
    """
)


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


def _status_event(row: Row) -> dict:
    """A lifecycle move, announced like a bid or a close.

    One type for all of them rather than one per status: a client switches on `type` and
    `status` carries which way it went, so a state added later needs no new event type.
    """
    return {
        "type": "status",
        "auction_id": row.id,
        "status": row.status,
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
    # history row, same transaction as the accepted bid. The currency is copied off the
    # row the statement returned, so it is the auction's own and not the client's.
    bid = Bid(auction_id=auction_id, user_id=user_id, amount=amount, currency=row.currency)
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
    if auction.reserve is not None and amount < auction.reserve:
        # The number is not in the message. A reserve a bidder can read is a reserve they
        # can bid exactly, which is the seller's floor turned into the seller's ceiling.
        return "the seller's reserve has not been met"
    if auction.current_bid is not None:
        least = auction.current_bid + bid_increment(auction.current_bid)
        if amount < least:
            # The number, not the rule: a bidder needs to know what to type, and working
            # the band table out from a refusal is not their job.
            return f"bid must be at least {least}"
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
        # A draft is refused here and not only by the HTTP route. GET /auctions/{id}
        # answers 404 for one, and a socket that handed the same row over would be the
        # back door to a locked front one: the snapshot carries the whole auction, and a
        # ticket is something any signed-up account can mint for any id.
        if auction is None or auction.status == "draft":
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
                await purge_expired_sessions(session)
                await prune_login_attempts(session)
                await prune_outbox(session)
        except Exception:
            # Blind on purpose: a transient database error must not kill the loop. BLE001
            # allows it because of the logger.exception call, which is what makes the
            # swallowed error findable rather than silent.
            logger.exception("closer tick failed")
        await asyncio.sleep(interval)
