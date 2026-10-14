"""Communities: who may bid on whose auctions.

The bid guard itself lives in app.auctions, inside _BID_SQL, because it is one condition
of the statement that accepts a bid and not a thing of its own. What is here is the
handful of writes that move a membership around.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import codes
from app.models import CommunityCode

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


async def issue_code(
    session: AsyncSession,
    community_id: int,
    *,
    owner_id: int,
    label: str = "",
    max_uses: int = 1,
    expires_at: datetime | None = None,
) -> str | None:
    """Mint a code for a community. Returns it once, or None if the caller is not the
    owner.

    Once, because this is the only moment it exists in a form anybody can read. What the
    row keeps is the HMAC, so there is nothing to show a second time and nothing in a
    backup to steal.
    """
    owns = (
        await session.execute(
            # FOR UPDATE, so this serialises against a rotation of the same community.
            # Without it a rotation cannot see this insert until it commits, tombstones
            # nothing, inserts its own, and collides with the one-live-code index — which
            # the owner reads as a 500 for double-clicking.
            text("SELECT 1 FROM communities WHERE id = :c AND owner_id = :o FOR UPDATE"),
            {"c": community_id, "o": owner_id},
        )
    ).first()
    if owns is None:
        await session.rollback()
        return None
    shown, body = codes.new_code()
    session.add(
        CommunityCode(
            community_id=community_id,
            label=label,
            code_lookup=codes.lookup(body),
            max_uses=max_uses,
            expires_at=expires_at,
        )
    )
    await session.commit()
    return shown


# Claiming a use, and the only test of whether a code is usable. One statement, because
# `used_count < max_uses` read by one connection and acted on by another is CWE-837: the
# UPDATE takes the row lock, so a second redemption re-reads the count under that lock and
# finds no room. The check constraint behind it is the backstop, not the rule.
#
# Every reason to refuse is in this one WHERE — never issued, tombstoned by a rotation,
# past its deadline, out of uses — so all four cost the same work and reach the caller as
# the same nothing. Criterion 5 is about that, and it is a property of the statement rather
# than of the handler above it.
_CLAIM_USE_SQL = text("""
    UPDATE community_codes
       SET used_count = used_count + 1
     WHERE code_lookup = :lookup
       AND invalidated_at IS NULL
       AND used_count < max_uses
       AND (expires_at IS NULL OR expires_at > clock_timestamp())
    RETURNING id, community_id
    """)

# The generation comes off the community in the same statement that writes the row, so a
# remove-all committing in between cannot leave a membership stamped with a generation that
# is already history.
_JOIN_SQL = text("""
    INSERT INTO community_memberships
        (community_id, user_id, generation, joined_via_code_id, joined_via_generation,
         created_at, updated_at)
    SELECT c.id, :user_id, c.membership_generation, :code_id, c.membership_generation,
           now(), now()
      FROM communities c
     WHERE c.id = :community_id
    RETURNING id
    """)


# The partial unique index on (community, user, generation) WHERE revoked_at IS NULL.
# A revoked row does not collide, so somebody kicked by mistake can be let back in
# with a fresh code; only a live membership trips this.
_MEMBERSHIP_CONSTRAINT = "uq_memberships_community_user_generation"


async def redeem_code(session: AsyncSession, presented: str, user_id: int) -> int | None:
    """Trade a code for a membership. Returns the membership id, or None.

    None for every reason: not a code at all, never issued, rotated away, expired, out of
    uses, already a member. The caller cannot act on the difference, and telling them apart
    is how a code becomes something to enumerate — OWASP calls that OAT-002.
    """
    body = codes.normalise(presented)
    if body is None:
        # A string that is not a code never reaches the database. The check symbol caught
        # it, which is what the check symbol is for.
        return None
    claimed = (await session.execute(_CLAIM_USE_SQL, {"lookup": codes.lookup(body)})).first()
    if claimed is None:
        await session.rollback()
        return None
    try:
        membership_id = await session.scalar(
            _JOIN_SQL,
            {"user_id": user_id, "code_id": claimed.id, "community_id": claimed.community_id},
        )
        await session.commit()
    except IntegrityError as exc:
        # Already a member of this generation. The rollback takes the claimed use with it,
        # so redeeming twice costs the code nothing — and the unique constraint is what
        # makes that safe under concurrency, where a read-then-insert would not be.
        #
        # Matched on the constraint name off asyncpg's own exception rather than on the
        # message text, which a reworded driver error would turn into a swallowed bug. Any
        # other IntegrityError is a real one and is re-raised.
        await session.rollback()
        if getattr(exc.orig.__cause__, "constraint_name", None) == _MEMBERSHIP_CONSTRAINT:
            return None
        raise
    return membership_id


# Rotation: tombstone whatever is live, then issue. One transaction, and the partial unique
# index on (community_id) WHERE invalidated_at IS NULL is what makes it safe — two
# rotations racing cannot both leave a code behind, because the second insert collides.
#
# invalidated_at rather than DELETE. A deleted row is a code that can be issued again by
# accident; Check Point documented live exploitation in 2025 of Discord returning expired
# invite codes to the pool, which is what reissuing looks like from the outside. The
# tombstone is also what the audit log points at when somebody asks which code let a
# person in.
_TOMBSTONE_SQL = text("""
    UPDATE community_codes
       SET invalidated_at = clock_timestamp()
     WHERE community_id = :community_id
       AND invalidated_at IS NULL
    RETURNING id
    """)


async def rotate_code(
    session: AsyncSession,
    community_id: int,
    *,
    owner_id: int,
    label: str = "",
    max_uses: int = 1,
    expires_at: datetime | None = None,
) -> str | None:
    """Replace the community's live code. Returns the new one once, or None if the caller
    is not the owner.

    The old code stops validating the moment this commits, including for somebody who is
    mid-redemption: their claim is an UPDATE against a row this one has already locked, so
    they wait and then find it tombstoned.

    Rotation is also the answer to a lost code, which is why there is nothing to re-display
    and no second copy of a code anywhere.
    """
    owns = (
        await session.execute(
            text("SELECT 1 FROM communities WHERE id = :c AND owner_id = :o FOR UPDATE"),
            {"c": community_id, "o": owner_id},
        )
    ).first()
    if owns is None:
        await session.rollback()
        return None
    await session.execute(_TOMBSTONE_SQL, {"community_id": community_id})
    shown, body = codes.new_code()
    session.add(
        CommunityCode(
            community_id=community_id,
            label=label,
            code_lookup=codes.lookup(body),
            max_uses=max_uses,
            expires_at=expires_at,
        )
    )
    await session.commit()
    return shown
