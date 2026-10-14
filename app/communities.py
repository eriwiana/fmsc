"""Communities: who may bid on whose auctions.

The bid guard itself lives in app.auctions, inside _BID_SQL, because it is one condition
of the statement that accepts a bid and not a thing of its own. What is here is the
handful of writes that move a membership around.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# One guarded UPDATE, scoped to the owner. Ownership is in the WHERE rather than checked
# first, for the same reason as every other guard in this project: two statements leave a
# window, and this one would let a demoted moderator finish a kick.
#
# `revoked_at IS NULL` makes it idempotent in the way that matters — a second kick must not
# move the timestamp forward, because the timestamp is what decides which auctions the
# person may still bid on. Moving it would reach into auctions opened since the first kick.
_KICK_SQL = text("""
    UPDATE community_memberships m
       SET revoked_at = clock_timestamp()
      FROM communities c
     WHERE c.id = m.community_id
       AND m.community_id = :community_id
       AND m.user_id = :user_id
       AND m.generation = c.membership_generation
       AND m.revoked_at IS NULL
       AND c.owner_id = :owner_id
    RETURNING m.id, m.revoked_at
    """)


async def kick_member(
    session: AsyncSession, community_id: int, user_id: int, *, owner_id: int
) -> bool:
    """Revoke one membership. False means nothing was revoked.

    False covers every refusal — not the owner, no such membership, already revoked, a
    membership from an older generation — because the caller cannot act on the difference
    and spelling it out would say who is in the community.
    """
    revoked = (
        await session.execute(
            _KICK_SQL,
            {"community_id": community_id, "user_id": user_id, "owner_id": owner_id},
        )
    ).first()
    await session.commit()
    return revoked is not None


# Remove-all, in three statements inside one transaction, and the lock is the whole point.
#
# A single UPDATE carrying NOT EXISTS would read auctions from its own snapshot, so a
# create-auction that has inserted but not committed is invisible to it: the bump goes
# through, and the new lot opens into a community whose every membership just stopped
# counting. Under read committed that is not a narrow race, it is the normal case.
#
# So: take the community row exclusively, then look, then bump. create_auction takes the
# same row FOR SHARE before inserting, which is what makes the two serialise — a lock only
# helps where both sides take it. FOR SHARE and not FOR UPDATE there, so two sellers
# listing at once do not queue behind each other; they only queue behind a remove-all.
_LOCK_COMMUNITY_SQL = text(
    "SELECT membership_generation FROM communities"
    " WHERE id = :community_id AND owner_id = :owner_id FOR UPDATE"
)

# A draft counts. Nothing stops its seller publishing it a second after the bump, and
# publish takes no community lock — so a guard that only looked for 'open' would let the
# generation move out from under a lot that is about to open, which is the state this whole
# function exists to prevent. Cancelled and closed do not count: neither can take a bid
# again.
_OPEN_AUCTION_SQL = text(
    "SELECT 1 FROM auctions"
    " WHERE community_id = :community_id AND status IN ('draft', 'open') LIMIT 1"
)

_BUMP_SQL = text(
    "UPDATE communities SET membership_generation = membership_generation + 1"
    " WHERE id = :community_id RETURNING membership_generation"
)


async def remove_all_members(
    session: AsyncSession, community_id: int, *, owner_id: int
) -> int | None:
    """End every membership at once. Returns the new generation, or None if refused.

    Refused while the community has an open auction, because the bump would strand the
    bidders on a lot that is still running: they bid under a membership that is suddenly
    history, and the seller is left with an auction nobody may bid on.

    No membership row is deleted. The generation is what counts, so the rows stay as the
    record of who was in the community when each lot sold.
    """
    locked = (
        await session.execute(
            _LOCK_COMMUNITY_SQL, {"community_id": community_id, "owner_id": owner_id}
        )
    ).first()
    if locked is None:
        # No such community, or not the caller's. One answer for both.
        await session.rollback()
        return None
    if (await session.execute(_OPEN_AUCTION_SQL, {"community_id": community_id})).first():
        await session.rollback()
        return None
    generation = await session.scalar(_BUMP_SQL, {"community_id": community_id})
    await session.commit()
    return generation
