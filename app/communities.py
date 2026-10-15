"""Communities: who may bid on whose auctions.

The bid guard itself lives in app.auctions, inside _BID_SQL, because it is one condition
of the statement that accepts a bid and not a thing of its own. What is here is the
handful of writes that move a membership around.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from litestar import Request, post
from litestar.di import Provide
from litestar.exceptions import (
    ClientException,
    HTTPException,
    NotFoundException,
    TooManyRequestsException,
)
from litestar.status_codes import HTTP_409_CONFLICT
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import codes, discord
from app.auth import client_ip, provide_current_user
from app.clock import resolve_deadline
from app.models import CodeAttempt, Community, CommunityCode, User
from app.schemas import (
    CodeResponse,
    CommunityResponse,
    CreateCommunityRequest,
    DiscordLinkRequest,
    GenerationResponse,
    IssueCodeRequest,
    MembershipResponse,
    RedeemRequest,
)

_authed = {"current_user": Provide(provide_current_user)}


def provide_sender() -> discord.Sender:
    """The transport, as a dependency, so a test hands the route a fake and the route does
    not have to know that anything was swapped."""
    return discord.send


# Only the two routes that mint a code, which are the only ones with anything to announce.
_announcing = {**_authed, "send": Provide(provide_sender, sync_to_thread=False)}

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
_ONE_LIVE_CODE = "uq_one_live_code_per_community"

# Why the claim found no room, read once, on the failure path, for the audit log and for
# nothing else. The caller's answer is already decided by then and does not depend on this.
#
# One statement for all four outcomes, so they cost the same work: that is what criterion
# 5's "indistinguishable in timing" amounts to in a system where the four branches are the
# same query. A wall-clock assertion wide enough not to flake is wider than the leak it
# looks for; an extra query is the leak that matters, and the test counts statements.
#
# Precedence when more than one applies: revoked, then expired, then exhausted. Arbitrary
# but fixed, so the log reads the same way every time.
_DIAGNOSE_SQL = text("""
    SELECT id,
           community_id,
           invalidated_at IS NOT NULL AS revoked,
           (expires_at IS NOT NULL AND expires_at <= clock_timestamp()) AS expired,
           used_count >= max_uses AS exhausted
      FROM community_codes
     WHERE code_lookup = :lookup
    """)

_AUDIT_SQL = text("""
    INSERT INTO code_audit (community_id, code_id, user_id, ip, outcome, at)
    VALUES (:community_id, :code_id, :user_id, :ip, :outcome, now())
    """)


async def _audit(
    session: AsyncSession,
    *,
    outcome: str,
    ip: str,
    user_id: int | None = None,
    code_id: int | None = None,
    community_id: int | None = None,
) -> None:
    await session.execute(
        _AUDIT_SQL,
        {
            "community_id": community_id,
            "code_id": code_id,
            "user_id": user_id,
            "ip": ip,
            "outcome": outcome,
        },
    )


async def redeem_code(
    session: AsyncSession, presented: str, user_id: int, *, ip: str = "unknown"
) -> int | None:
    """Trade a code for a membership. Returns the membership id, or None.

    None for every reason: not a code at all, never issued, rotated away, expired, out of
    uses, already a member. The caller cannot act on the difference, and telling them apart
    is how a code becomes something to enumerate — OWASP calls that OAT-002. Which it was
    goes to code_audit, which is the only place the four differ.
    """
    body = codes.normalise(presented)
    if body is None:
        # A string that is not a code never reaches the database, and is not written down
        # either: the check symbol caught it, so this is a typo rather than an attempt at
        # anything. Auditing it would fill the log with somebody's bad afternoon and make
        # a fat finger look like enumeration; the caller is answered the same as the four.
        #
        # It is also why this path is cheaper than those four. What that reveals is the
        # check symbol algorithm, which is public, and nothing about which codes exist.
        return None
    lookup = codes.lookup(body)
    claimed = (await session.execute(_CLAIM_USE_SQL, {"lookup": lookup})).first()
    if claimed is None:
        # The claim is rolled back first, so the audit row is not undone with it.
        await session.rollback()
        found = (await session.execute(_DIAGNOSE_SQL, {"lookup": lookup})).first()
        if found is None:
            outcome, code_id, community_id = "not_found", None, None
        elif found.revoked:
            outcome, code_id, community_id = "revoked", found.id, found.community_id
        elif found.expired:
            outcome, code_id, community_id = "expired", found.id, found.community_id
        else:
            outcome, code_id, community_id = "exhausted", found.id, found.community_id
        await _audit(
            session,
            outcome=outcome,
            ip=ip,
            user_id=user_id,
            code_id=code_id,
            community_id=community_id,
        )
        await session.commit()
        return None
    try:
        membership_id = await session.scalar(
            _JOIN_SQL,
            {"user_id": user_id, "code_id": claimed.id, "community_id": claimed.community_id},
        )
        # In the same transaction as the membership, unlike the failure paths: a successful
        # redemption and its audit row are one fact, and a crash between them would leave
        # a membership nothing accounts for.
        await _audit(
            session,
            outcome="redeemed",
            ip=ip,
            user_id=user_id,
            code_id=claimed.id,
            community_id=claimed.community_id,
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
            await _audit(
                session,
                outcome="already_member",
                ip=ip,
                user_id=user_id,
                code_id=claimed.id,
                community_id=claimed.community_id,
            )
            await session.commit()
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


# The same numbers as M7's failed logins, on their own buckets. Chosen, not measured.
#
# A 65-bit code is not going to fall to guessing; the limit is about the cost and the noise
# of somebody trying, which is what OWASP calls OAT-002. It also bounds how fast one
# account could walk the space if a code ever got shorter.
REDEEM_WINDOW = timedelta(minutes=15)
MAX_REDEEM_FAILURES_PER_ACCOUNT = 5
MAX_REDEEM_FAILURES_PER_IP = 20

# One statement, one indexed subquery per bucket — the shape M7's login limiter ended up
# with after exactly this was got wrong there: `count(*) FILTER` over the whole window has
# no predicate either index starts with, so it plans as a sequential scan, and the flood is
# when the table is large.
_REDEEM_FAILURES_SQL = text("""
    SELECT by_user.n AS by_user,
           by_ip.n AS by_ip,
           CEIL(EXTRACT(EPOCH FROM (
               by_user.oldest + CAST(:window AS interval) - clock_timestamp()))) AS user_retry,
           CEIL(EXTRACT(EPOCH FROM (
               by_ip.oldest + CAST(:window AS interval) - clock_timestamp()))) AS ip_retry
      FROM (SELECT count(*) AS n, MIN(failed_at) AS oldest
              FROM code_attempts
             WHERE user_id = :user_id
               AND failed_at > clock_timestamp() - CAST(:window AS interval)) AS by_user,
           (SELECT count(*) AS n, MIN(failed_at) AS oldest
              FROM code_attempts
             WHERE ip = :ip
               AND failed_at > clock_timestamp() - CAST(:window AS interval)) AS by_ip
    """)


async def redeem_retry_after(session: AsyncSession, user_id: int, ip: str) -> int | None:
    """Seconds to wait before another code may be tried, or None if one may be now."""
    row = (
        await session.execute(
            _REDEEM_FAILURES_SQL,
            {"user_id": user_id, "ip": ip, "window": REDEEM_WINDOW},
        )
    ).one()
    if row.by_user >= MAX_REDEEM_FAILURES_PER_ACCOUNT:
        return max(int(row.user_retry), 1)
    if row.by_ip >= MAX_REDEEM_FAILURES_PER_IP:
        return max(int(row.ip_retry), 1)
    return None


async def record_failed_redemption(session: AsyncSession, user_id: int, ip: str) -> None:
    """Both buckets are fed by one row; which one it counts towards is the query's
    business, not the writer's."""
    session.add(CodeAttempt(user_id=user_id, ip=ip, failed_at=datetime.now(timezone.utc)))
    await session.commit()


async def clear_failed_redemptions(session: AsyncSession, user_id: int) -> None:
    """A code that worked empties the account's bucket.

    Without this, four mistypes followed by a successful join leave the account one typo
    away from a lockout for the rest of the window — the same reasoning as the login
    limiter's. The address bucket is left alone: an address that has been guessing at codes
    is still an address that has been guessing at codes.
    """
    await session.execute(
        text("DELETE FROM code_attempts WHERE user_id = :user_id"), {"user_id": user_id}
    )
    await session.commit()


async def prune_code_attempts(session: AsyncSession) -> int:
    """Attempts older than the window are never counted again."""
    result = await session.execute(
        text(
            "DELETE FROM code_attempts WHERE failed_at <= clock_timestamp() - CAST(:w AS interval)"
        ),
        {"w": REDEEM_WINDOW},
    )
    await session.commit()
    return result.rowcount


# One answer for every way a redemption can fail, including the ones that are not the
# caller's fault. 404 and a fixed sentence: a client cannot act on the difference between
# a code that never existed, one that was rotated away, one past its deadline and one out
# of uses, and telling them apart turns the endpoint into something to enumerate. OWASP
# calls that OAT-002. code_audit is where the difference is kept.
_REFUSED = "that code cannot be redeemed"


@post("/communities/codes/redeem", dependencies=_authed)
async def redeem(
    data: RedeemRequest, request: Request, current_user: User, db_session: AsyncSession
) -> MembershipResponse:
    """Join a community by code."""
    # Read the id before anything can roll back. redeem_code rolls back on every refusal,
    # and a rollback expires every instance in the session regardless of expire_on_commit
    # — so touching current_user afterwards attempts a lazy reload, which raises
    # MissingGreenlet inside an async session and turns every refused code into a 500 with
    # no reason in it. The same trap place_bid documents.
    user_id = current_user.id
    ip = client_ip(request)
    retry_after = await redeem_retry_after(db_session, user_id, ip)
    if retry_after is not None:
        # Read before the code is looked at, so a caller who has spent their budget cannot
        # keep the database doing lookups. Nothing is recorded for a request refused here,
        # or hammering a lockout would extend it.
        raise TooManyRequestsException(
            detail="too many codes tried", headers={"Retry-After": str(retry_after)}
        )
    if codes.normalise(data.code) is None:
        # Not a code at all, and the check symbol said so without asking the database.
        # Answered the same as the four real refusals, and charged nothing: a member
        # retyping from a screenshot must not be able to lock themselves out, and five of
        # these in the log would be indistinguishable from an attack that never happened.
        raise NotFoundException(_REFUSED)
    membership_id = await redeem_code(db_session, data.code, user_id, ip=ip)
    if membership_id is None:
        await record_failed_redemption(db_session, user_id, ip)
        raise NotFoundException(_REFUSED)
    await clear_failed_redemptions(db_session, user_id)
    row = (
        await db_session.execute(
            text("SELECT community_id, generation FROM community_memberships WHERE id = :id"),
            {"id": membership_id},
        )
    ).one()
    return MembershipResponse(community_id=row.community_id, generation=row.generation)


# Every owner-only refusal is this, for the same reason the redemption has one answer: the
# caller cannot act on the difference between "no such community" and "not yours", and
# telling them apart says which communities exist.
_NOT_YOURS = "no such community, or not yours"


def _refused(detail: str = _NOT_YOURS) -> HTTPException:
    return HTTPException(status_code=HTTP_409_CONFLICT, detail=detail)


# The table refuses all of these anyway. Checked here so the refusal is a 400 naming the
# field rather than the 500 an IntegrityError becomes — the same reasoning as
# create_auction's, and the same three columns' worth of constraints.
MAX_LABEL = 60
MAX_NAME = 100


def _checked(data: IssueCodeRequest) -> datetime | None:
    if data.max_uses < 1:
        raise ClientException("max_uses must be at least 1")
    if len(data.label) > MAX_LABEL:
        raise ClientException(f"label must be at most {MAX_LABEL} characters")
    if data.expires_at is None:
        return None
    # Through resolve_deadline like every other client datetime in the API, so a naive
    # value is read as Asia/Jakarta wall time rather than as UTC — seven hours from what
    # the owner meant, in the direction that expires the code early.
    expires_at = resolve_deadline(data.expires_at)
    if expires_at <= datetime.now(timezone.utc):
        raise ClientException("expires_at must be in the future")
    return expires_at


@post("/communities", dependencies=_authed)
async def create_community(
    data: CreateCommunityRequest, current_user: User, db_session: AsyncSession
) -> CommunityResponse:
    """Start a community. The caller owns it, and ownership is the only thing that confers
    moderator rights — there is no membership row for an owner to lose."""
    if not data.name or len(data.name) > MAX_NAME:
        raise ClientException(f"name must be between 1 and {MAX_NAME} characters")
    community = Community(name=data.name, owner_id=current_user.id)
    db_session.add(community)
    await db_session.commit()
    await db_session.refresh(community)
    return CommunityResponse(
        id=community.id,
        name=community.name,
        owner_id=community.owner_id,
        membership_generation=community.membership_generation,
    )


@post("/communities/{community_id:int}/codes", dependencies=_announcing)
async def issue(
    community_id: int,
    data: IssueCodeRequest,
    current_user: User,
    db_session: AsyncSession,
    # Defaulted as well as provided: the dependency is what the route uses, and the
    # default is what a caller reaching past the route — a test, or another handler —
    # gets without having to know a transport exists.
    send: discord.Sender = discord.send,
) -> CodeResponse:
    """Mint the community's first code, and answer 409 if it already has a live one.

    Deliberately not a silent rotation: an owner who has already shared a code and calls
    this again by accident would otherwise invalidate the code everybody is holding.
    Replacing one is the other endpoint, and saying so is the point.
    """
    owner_id = current_user.id
    expires_at = _checked(data)
    try:
        shown = await issue_code(
            db_session,
            community_id,
            owner_id=owner_id,
            label=data.label,
            max_uses=data.max_uses,
            expires_at=expires_at,
        )
    except IntegrityError as exc:
        await db_session.rollback()
        if getattr(exc.orig.__cause__, "constraint_name", None) == _ONE_LIVE_CODE:
            raise _refused("this community already has a live code; rotate it instead") from None
        raise
    if shown is None:
        raise _refused()
    broadcast = await discord.broadcast_code(db_session, community_id, shown, send)
    return CodeResponse(code=shown, broadcast=broadcast)


@post("/communities/{community_id:int}/codes/rotate", dependencies=_announcing)
async def rotate(
    community_id: int,
    data: IssueCodeRequest,
    current_user: User,
    db_session: AsyncSession,
    # Defaulted as well as provided: the dependency is what the route uses, and the
    # default is what a caller reaching past the route — a test, or another handler —
    # gets without having to know a transport exists.
    send: discord.Sender = discord.send,
) -> CodeResponse:
    """Replace the live code. The old one stops working the moment this answers.

    And the community's channel is told, which is the gap M9 named: rotation ends every
    membership's only way back in at once, so the people holding the old code have to hear
    the new one somewhere. Whether they did is in the response, because if they did not,
    passing it on is the owner's job.
    """
    expires_at = _checked(data)
    shown = await rotate_code(
        db_session,
        community_id,
        owner_id=current_user.id,
        label=data.label,
        max_uses=data.max_uses,
        expires_at=expires_at,
    )
    if shown is None:
        raise _refused()
    broadcast = await discord.broadcast_code(db_session, community_id, shown, send)
    return CodeResponse(code=shown, broadcast=broadcast)


# Ownership in the WHERE rather than checked first, like every other guard here: the
# INSERT selects from communities, so a row arrives only for the owner and nothing has to
# be read back to find out whether it did.
#
# ON CONFLICT on the community, so relinking a channel is the same one call rather than a
# delete and an insert — an owner moving the bot to another channel should not have a
# window where the community is linked to nothing.
_LINK_DISCORD_SQL = text("""
    INSERT INTO community_discord
                (community_id, guild_id, channel_id, created_at, updated_at)
         SELECT :community_id, :guild_id, :channel_id, clock_timestamp(), clock_timestamp()
           FROM communities
          WHERE id = :community_id AND owner_id = :owner_id
    ON CONFLICT (community_id) DO UPDATE
            SET guild_id = EXCLUDED.guild_id,
                channel_id = EXCLUDED.channel_id,
                updated_at = clock_timestamp()
      RETURNING id
    """)

_ONE_COMMUNITY_PER_GUILD = "uq_community_discord_guild_id"

# 17 to 20 digits. Checked so a typo is a 400 naming the field rather than the 500 a
# value too long for the column becomes — the same reasoning as max_uses and label.
_SNOWFLAKE = re.compile(r"[0-9]{17,20}")


@post("/communities/{community_id:int}/discord", dependencies=_authed)
async def link_discord(
    community_id: int,
    data: DiscordLinkRequest,
    current_user: User,
    db_session: AsyncSession,
) -> None:
    """Point a community's posts at a guild channel, or move them to another one.

    The owner does this here rather than from Discord because this is the only place
    ownership of a community can be proved. What it delegates is real: from then on,
    whoever manages that guild can mint invite codes for this community.
    """
    for field, value in (("guild_id", data.guild_id), ("channel_id", data.channel_id)):
        if not _SNOWFLAKE.fullmatch(value):
            raise ClientException(f"{field} must be a Discord snowflake")
    try:
        linked = (
            await db_session.execute(
                _LINK_DISCORD_SQL,
                {
                    "community_id": community_id,
                    "guild_id": data.guild_id,
                    "channel_id": data.channel_id,
                    "owner_id": current_user.id,
                },
            )
        ).first()
    except IntegrityError as exc:
        await db_session.rollback()
        if getattr(exc.orig.__cause__, "constraint_name", None) == _ONE_COMMUNITY_PER_GUILD:
            raise _refused("that guild is already linked to another community") from None
        raise
    if linked is None:
        await db_session.rollback()
        raise _refused()
    await db_session.commit()


@post("/communities/{community_id:int}/members/{user_id:int}/kick", dependencies=_authed)
async def kick(
    community_id: int, user_id: int, current_user: User, db_session: AsyncSession
) -> None:
    """Revoke one membership. The auctions already open stay open to them."""
    if not await kick_member(db_session, community_id, user_id, owner_id=current_user.id):
        raise _refused("no such membership, or not yours to revoke")


@post("/communities/{community_id:int}/members/remove-all", dependencies=_authed)
async def remove_all(
    community_id: int, current_user: User, db_session: AsyncSession
) -> GenerationResponse:
    """End every membership at once. Refused while the community has an open auction.

    Everyone has to redeem the new code again afterwards, and until M10 there is no
    broadcast path — so the new code reaches them out of band.
    """
    generation = await remove_all_members(db_session, community_id, owner_id=current_user.id)
    if generation is None:
        raise _refused("not yours, or the community has an auction still open")
    return GenerationResponse(membership_generation=generation)
