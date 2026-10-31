"""Settlement: the record of what happened after the hammer.

Money never touches fmsc. There is no escrow and no payment licence, and so no lever to
pull on a winner who simply never pays — which makes the record itself the product. Both
sides say whether the deal completed, inside a window, and trust is counted from those
rows rather than kept in a number somebody could inflate.

Everything here is one statement. The grace period is a comparison inside the WHERE clause,
never an `if` in Python: two statements leave a window, and this is the window in which a
settlement could land against a row that had already gone stale.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import NamedTuple

from litestar import post
from litestar.di import Provide
from litestar.exceptions import HTTPException, NotFoundException
from litestar.status_codes import HTTP_409_CONFLICT
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import provide_current_user
from app.models import User
from app.schemas import SecondChanceResponse, SettlementResponse

logger = logging.getLogger(__name__)

_authed = {"current_user": Provide(provide_current_user)}

# How long after the close either side may still mark the deal done. Long enough for a real
# transfer and a slow reply, short enough that a default is visible inside a week.
#
# ponytail: global, like SNIPE_WINDOW. A per-community window is a column and a join on the
# hottest guard here, and nothing has asked for one.
SETTLEMENT_GRACE = timedelta(days=7)

# One statement for both sides. Whichever marks first inserts the row; the other conflicts
# on auction_id and fills its own column.
#
# COALESCE on the update, not assignment: EXCLUDED carries NULL for the column belonging to
# the side that is not calling, so plain assignment would have each caller erase the other's
# mark. It also makes a repeated mark idempotent rather than a fresh timestamp.
#
# The grace period, the status and who is allowed to call all sit in the WHERE of the
# SELECT. Past the window the SELECT produces nothing, so there is no conflict and no
# update — the stale row is untouchable without a second read deciding it.
#
# The buyer of record is the second-chance offer's recipient when there is one, and the
# winner otherwise. LEFT JOIN rather than two statements: who owes the money is one fact
# about the auction, and reading it separately would let an offer land in between.
_SETTLE_SQL = text("""
    INSERT INTO settlements
                (auction_id, buyer_id, seller_settled_at, buyer_settled_at,
                 created_at, updated_at)
         SELECT a.id, COALESCE(o.offered_to_id, a.current_winner_id),
                CASE WHEN a.seller_id = :uid THEN clock_timestamp() END,
                CASE WHEN COALESCE(o.offered_to_id, a.current_winner_id) = :uid
                     THEN clock_timestamp() END,
                clock_timestamp(), clock_timestamp()
           FROM auctions a
           LEFT JOIN second_chance_offers o ON o.auction_id = a.id
          WHERE a.id = :aid
            AND a.status = 'closed'
            AND COALESCE(o.offered_to_id, a.current_winner_id) IS NOT NULL
            AND a.closed_at + :grace >= clock_timestamp()
            AND :uid IN (a.seller_id, COALESCE(o.offered_to_id, a.current_winner_id))
    ON CONFLICT (auction_id) DO UPDATE
            SET seller_settled_at = COALESCE(settlements.seller_settled_at,
                                             EXCLUDED.seller_settled_at),
                buyer_settled_at = COALESCE(settlements.buyer_settled_at,
                                            EXCLUDED.buyer_settled_at),
                updated_at = clock_timestamp()
      RETURNING auction_id, buyer_id, seller_settled_at, buyer_settled_at
    """)

# Why the statement matched nothing. One extra read, on the refusal path only — the same
# shape as reject_reason in app.auctions, and for the same reason: the happy path must not
# pay for an explanation nobody is going to read.
_WHY_SQL = text("""
    SELECT a.status,
           COALESCE(o.offered_to_id, a.current_winner_id) IS NULL AS nobody_won,
           :uid IN (a.seller_id, COALESCE(o.offered_to_id, a.current_winner_id)) AS a_party,
           a.closed_at + :grace < clock_timestamp() AS too_late
      FROM auctions a
      LEFT JOIN second_chance_offers o ON o.auction_id = a.id
     WHERE a.id = :aid
    """)

_NOT_YOURS = "no such auction, or you are not a party to it"


def _refused(detail: str) -> HTTPException:
    return HTTPException(status_code=HTTP_409_CONFLICT, detail=detail)


def _response(row) -> SettlementResponse:
    """Both timestamps collapse to booleans here, and `settled` is derived from the pair
    rather than stored, so nothing can claim a deal is done that the rows do not show."""
    seller, buyer = row.seller_settled_at is not None, row.buyer_settled_at is not None
    return SettlementResponse(
        auction_id=row.auction_id,
        buyer_id=row.buyer_id,
        seller_settled=seller,
        buyer_settled=buyer,
        settled=seller and buyer,
    )


async def _explain(session: AsyncSession, auction_id: int, user_id: int) -> HTTPException:
    """Turn a statement that matched nothing into the reason it did."""
    row = (
        await session.execute(
            _WHY_SQL, {"aid": auction_id, "uid": user_id, "grace": SETTLEMENT_GRACE}
        )
    ).first()
    if row is None:
        return NotFoundException(detail="no such auction")
    if not row.a_party:
        # Deliberately the same answer as an auction that does not exist: a stranger who
        # can tell the two apart can enumerate which auctions closed with a winner.
        return _refused(_NOT_YOURS)
    if row.status != "closed":
        return _refused(f"this auction is {row.status}, not closed")
    if row.nobody_won:
        return _refused("this auction closed with no winner, so there is nothing to settle")
    if row.too_late:
        return _refused(
            f"the grace period for settling this auction passed"
            f" ({SETTLEMENT_GRACE.days} days after it closed)"
        )
    # Unreached today: the guards above are the whole WHERE clause. Kept so that a guard
    # added to the statement without a reason here is a refusal rather than a 500.
    return _refused(_NOT_YOURS)


@post("/auctions/{auction_id:int}/settle", dependencies=_authed)
async def settle(
    auction_id: int, current_user: User, db_session: AsyncSession
) -> SettlementResponse:
    """Mark, as one of the two parties, that this deal completed.

    Idempotent: marking twice is the same row, with the first timestamp kept. Marking says
    nothing about the other side — the record is only settled once both have.
    """
    # Read before anything commits or rolls back. Either expires the instance, and the
    # reload that an attribute access would then trigger happens outside SQLAlchemy's
    # greenlet — which surfaces as MissingGreenlet rather than as the refusal being raised.
    user_id = current_user.id
    row = (
        await db_session.execute(
            _SETTLE_SQL,
            {"aid": auction_id, "uid": user_id, "grace": SETTLEMENT_GRACE},
        )
    ).first()
    if row is None:
        await db_session.rollback()
        raise await _explain(db_session, auction_id, user_id)
    await db_session.commit()
    logger.info(
        "settlement on auction %s marked by user %s (seller=%s buyer=%s)",
        auction_id,
        user_id,
        row.seller_settled_at is not None,
        row.buyer_settled_at is not None,
    )
    return _response(row)


# The whole promotion, in one insert. No UPDATE of auctions: the lot stays closed and keeps
# the winner it closed on, which is what "without reopening" means — a reopened auction
# would also become biddable again, and the closer would have to decide what to do with it.
#
# The runner-up and the price come off the auction row rather than from the caller, so a
# seller cannot offer the lot to somebody who never bid, or at a price nobody offered.
#
# ON CONFLICT DO NOTHING, then the row is read back below: a second click is the same offer,
# not a new one, and answering it with the existing row keeps the endpoint idempotent.
#
# The same grace period as settling, because an offer outside it is worse than useless: the
# runner-up could not mark it done anyway, and the offer would still move who the buyer of
# record is — which is to say it would clear the record of a winner who never paid and plant
# one on somebody who was never asked while there was time to answer.
#
# And not once either side has gone on record. A settlement row names the buyer it was
# created for and keeps that name, so an offer landing afterwards would leave the record
# claiming one person and the auction implying another, with the new buyer's own mark
# discarded by the upsert's COALESCE. A seller who marked a deal done cannot then vacate it;
# there is no un-marking anywhere in this project, by design.
_OFFER_SQL = text("""
    INSERT INTO second_chance_offers
                (auction_id, offered_to_id, amount, created_at, updated_at)
         SELECT a.id, a.runner_up_id, a.runner_up_bid, clock_timestamp(), clock_timestamp()
           FROM auctions a
          WHERE a.id = :aid
            AND a.seller_id = :uid
            AND a.status = 'closed'
            AND a.runner_up_id IS NOT NULL
            AND a.closed_at + :grace >= clock_timestamp()
            AND NOT EXISTS (SELECT 1 FROM settlements s WHERE s.auction_id = a.id)
    ON CONFLICT (auction_id) DO NOTHING
      RETURNING auction_id, offered_to_id, amount
    """)

_EXISTING_OFFER_SQL = text("""
    SELECT o.auction_id, o.offered_to_id, o.amount
      FROM second_chance_offers o
      JOIN auctions a ON a.id = o.auction_id
     WHERE o.auction_id = :aid AND a.seller_id = :uid
    """)

_WHY_NO_OFFER_SQL = text("""
    SELECT a.status,
           a.seller_id = :uid AS is_seller,
           a.runner_up_id IS NULL AS nobody_second,
           a.closed_at + :grace < clock_timestamp() AS too_late,
           EXISTS (SELECT 1 FROM settlements s WHERE s.auction_id = a.id) AS on_record
      FROM auctions a
     WHERE a.id = :aid
    """)


@post("/auctions/{auction_id:int}/second-chance", dependencies=_authed)
async def offer_second_chance(
    auction_id: int, current_user: User, db_session: AsyncSession
) -> SecondChanceResponse:
    """Offer the lot to the runner-up, at the runner-up's own last bid.

    The seller's only move when a winner goes quiet, and the reason the closing statement
    records who was second. The auction stays closed throughout.
    """
    user_id = current_user.id
    params = {"aid": auction_id, "uid": user_id, "grace": SETTLEMENT_GRACE}
    row = (await db_session.execute(_OFFER_SQL, params)).first()
    if row is None:
        # Either there is nothing to offer, or the offer already exists — DO NOTHING returns
        # no row for both. The read tells them apart, and only on this path.
        row = (await db_session.execute(_EXISTING_OFFER_SQL, params)).first()
        if row is None:
            await db_session.rollback()
            raise await _explain_offer(db_session, auction_id, user_id)
    await db_session.commit()
    logger.info("second chance on auction %s offered to user %s", row.auction_id, row.offered_to_id)
    return SecondChanceResponse(
        auction_id=row.auction_id, offered_to_id=row.offered_to_id, amount=row.amount
    )


async def _explain_offer(session: AsyncSession, auction_id: int, user_id: int) -> HTTPException:
    row = (
        await session.execute(
            _WHY_NO_OFFER_SQL, {"aid": auction_id, "uid": user_id, "grace": SETTLEMENT_GRACE}
        )
    ).first()
    if row is None:
        return NotFoundException(detail="no such auction")
    if not row.is_seller:
        return _refused("no such auction, or you are not its seller")
    if row.status != "closed":
        return _refused(f"this auction is {row.status}, not closed")
    if row.nobody_second:
        # Includes every auction that closed before the runner-up was recorded. Inventing
        # one now would mean deciding from bids who was second after the fact, which is
        # the deferred query the closing statement exists to avoid.
        return _refused("this auction has no runner-up to offer it to")
    if row.on_record:
        return _refused("this auction already has a settlement record")
    if row.too_late:
        return _refused(
            f"the grace period for offering this auction on passed"
            f" ({SETTLEMENT_GRACE.days} days after it closed)"
        )
    # Unreached today, for the same reason as the settle path's fallthrough.
    return _refused("no such auction, or you are not its seller")


class Reputation(NamedTuple):
    """What the rows say about one person, counted fresh every time.

    Not a model and not stored anywhere. The criterion is that these cannot drift, and the
    only way a count cannot drift from the records is to not keep it.

    "Unsettled" rather than "defaulted", because that is all these rows can show: a side
    that never marked once the window had gone. Where one side marked and the other did not
    the count is already one-sided and reads like fault, but a deal nobody marked counts
    against both, and nothing here can tell a buyer who vanished from a seller who never
    shipped. Deciding which of the two was at fault is what a dispute flow is for, and that
    is deferred — so this does not claim it.
    """

    as_seller_settled: int
    as_seller_unsettled: int
    as_buyer_settled: int
    as_buyer_unsettled: int


# One statement, counting the same set of closed auctions four ways.
#
# A side is unsettled when the window has gone and no mark of theirs is in the row, which is
# why it is computed against the same grace period the settle route enforces: anything inside
# the window is still pending and nobody is behind on anything yet. FILTER reads a NULL
# condition as false, so a LEFT JOIN that matched nothing counts as unmarked rather than
# dropping the row.
#
# "Settled" is both marks, on the same definition the response uses. Deriving it twice from
# the two timestamps is what makes it impossible for a count and a row to disagree.
#
# The buyer comes off the settlement row where there is one, and is worked out from the offer
# and the winner only where there is not. The settlement names the person the deal was with
# when it was created, and that is the name the marks in that row belong to — counting them
# against anybody else would be reading one row's timestamps as another person's record.
_REPUTATION_SQL = text("""
    SELECT count(*) FILTER (WHERE is_seller AND settled) AS as_seller_settled,
           count(*) FILTER (WHERE is_seller AND seller_missed) AS as_seller_unsettled,
           count(*) FILTER (WHERE is_buyer AND settled) AS as_buyer_settled,
           count(*) FILTER (WHERE is_buyer AND buyer_missed) AS as_buyer_unsettled
      FROM (
        SELECT a.seller_id = :uid AS is_seller,
               COALESCE(s.buyer_id, o.offered_to_id, a.current_winner_id) = :uid AS is_buyer,
               s.seller_settled_at IS NOT NULL
                   AND s.buyer_settled_at IS NOT NULL AS settled,
               a.closed_at + :grace < clock_timestamp()
                   AND s.seller_settled_at IS NULL AS seller_missed,
               a.closed_at + :grace < clock_timestamp()
                   AND s.buyer_settled_at IS NULL AS buyer_missed
          FROM auctions a
          LEFT JOIN second_chance_offers o ON o.auction_id = a.id
          LEFT JOIN settlements s ON s.auction_id = a.id
         -- Nothing observable rests on the status today, and a mutation pass proved it:
         -- a non-closed auction has a NULL closed_at, which makes both *_missed NULL and
         -- so false, and it cannot carry a settlement row because the settle statement
         -- requires 'closed' itself. Kept because this counts closed deals, and a later
         -- edge that sets closed_at on anything else would otherwise be counted silently.
         WHERE a.status = 'closed'
           AND COALESCE(s.buyer_id, o.offered_to_id, a.current_winner_id) IS NOT NULL
           AND (a.seller_id = :uid
                OR COALESCE(s.buyer_id, o.offered_to_id, a.current_winner_id) = :uid)
      ) deals
    """)


async def reputation(session: AsyncSession, user_id: int) -> Reputation:
    """What this person's closed deals came to, as seller and as buyer.

    Recomputed on every call. A stored total would be one write away from disagreeing with
    the rows it claims to summarise, and the row is the thing anyone would be shown.
    """
    row = (
        await session.execute(_REPUTATION_SQL, {"uid": user_id, "grace": SETTLEMENT_GRACE})
    ).one()
    return Reputation(
        as_seller_settled=row.as_seller_settled,
        as_seller_unsettled=row.as_seller_unsettled,
        as_buyer_settled=row.as_buyer_settled,
        as_buyer_unsettled=row.as_buyer_unsettled,
    )
