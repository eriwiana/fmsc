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
