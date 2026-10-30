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

from litestar import post
from litestar.di import Provide
from litestar.exceptions import HTTPException, NotFoundException
from litestar.status_codes import HTTP_409_CONFLICT
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import provide_current_user
from app.models import User
from app.schemas import SettlementResponse

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
_SETTLE_SQL = text("""
    INSERT INTO settlements
                (auction_id, buyer_id, seller_settled_at, buyer_settled_at,
                 created_at, updated_at)
         SELECT a.id, a.current_winner_id,
                CASE WHEN a.seller_id = :uid THEN clock_timestamp() END,
                CASE WHEN a.current_winner_id = :uid THEN clock_timestamp() END,
                clock_timestamp(), clock_timestamp()
           FROM auctions a
          WHERE a.id = :aid
            AND a.status = 'closed'
            AND a.current_winner_id IS NOT NULL
            AND a.closed_at + :grace >= clock_timestamp()
            AND :uid IN (a.seller_id, a.current_winner_id)
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
           a.current_winner_id IS NULL AS nobody_won,
           :uid IN (a.seller_id, a.current_winner_id) AS a_party,
           a.closed_at + :grace < clock_timestamp() AS too_late
      FROM auctions a
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
