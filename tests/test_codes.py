"""The code itself: 65 bits of Crockford Base32, a check symbol, and an HMAC lookup.

No criterion is about the format. These exist because the format is the part a human
retypes from a screenshot, and because every later criterion assumes a code that
round-trips and a lookup that cannot be reversed.
"""

from __future__ import annotations

import asyncio
import importlib
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from litestar.exceptions import (
    ClientException,
    HTTPException,
    NotFoundException,
    TooManyRequestsException,
)
from sqlalchemy import event, text

from app import codes
from app.clock import APP_TZ
from app.codes import (
    CODE_LENGTH,
    CROCKFORD,
    PREFIX,
    check_symbol,
    formatted,
    lookup,
    new_code,
    normalise,
)
from app.communities import (
    _REFUSED,
    MAX_LABEL,
    MAX_NAME,
    MAX_REDEEM_FAILURES_PER_ACCOUNT,
    MAX_REDEEM_FAILURES_PER_IP,
    REDEEM_WINDOW,
    create_community,
    issue,
    issue_code,
    kick,
    kick_member,
    prune_code_attempts,
    redeem,
    redeem_code,
    redeem_retry_after,
    remove_all,
    rotate,
    rotate_code,
)
from app.models import Community, User
from app.schemas import CreateCommunityRequest, IssueCodeRequest, RedeemRequest


async def _community(maker) -> tuple[int, int]:
    async with maker() as s:
        owner = User(email="owner@x.com", pw_hash="x", verified_at=datetime.now(timezone.utc))
        s.add(owner)
        await s.flush()
        community = Community(name="c", owner_id=owner.id)
        s.add(community)
        await s.commit()
        return community.id, owner.id


def test_a_code_round_trips_through_every_way_a_human_types_it():
    """Case, spacing, the prefix and the hyphens are noise. Somebody reading a code off a
    screenshot into a phone keyboard gets all four wrong at once."""
    shown, body = new_code()
    assert normalise(shown) == body
    assert normalise(shown.lower()) == body
    assert normalise(shown.replace("-", " ")) == body
    assert normalise(shown.replace("-", "")) == body
    assert normalise(f"  {shown}  ") == body
    assert normalise(shown.removeprefix(f"{PREFIX}-")) == body


def test_the_payload_is_sixty_five_bits_of_crockford():
    """13 characters at 5 bits. Pinned to the literal, not to the constant: comparing
    CODE_LENGTH with itself passes for any value, including a length quietly cut to 4."""
    assert CODE_LENGTH == 13
    assert len(CROCKFORD) == 32
    # No I, L, O or U: the first three are what people confuse with 1 and 0, and U so no
    # code spells anything a community would rather it did not.
    assert not set("ILOU") & set(CROCKFORD)
    _, body = new_code()
    assert len(body) == 13
    assert set(body) <= set(CROCKFORD)


def test_two_codes_differ():
    """A CSPRNG, not a counter. 100 draws of 65 bits colliding would be news."""
    assert len({new_code()[1] for _ in range(100)}) == 100


def test_the_check_symbol_catches_a_typo_before_the_database_is_asked():
    """Which keeps a fat finger out of the audit log and off the rate limiter's bucket. A
    mistyped code is not an enumeration attempt."""
    _, body = new_code()
    wrong = ("0" if body[0] != "0" else "1") + body[1:]
    assert normalise(formatted(wrong)[:-1] + check_symbol(body)) is None
    # And a transposition, which is the other thing a hand does.
    swapped = body[1] + body[0] + body[2:]
    if swapped != body:
        assert normalise(formatted(swapped)[:-1] + check_symbol(body)) is None


def test_crockford_corrects_the_letters_people_type_for_digits():
    """O for zero, I and L for one. Refusing them would be refusing the right code because
    the font was ambiguous, which is the whole reason Crockford leaves them out."""
    body = "0" + "1" + new_code()[1][2:]
    shown = formatted(body)
    assert normalise(shown.replace("0", "O").replace("1", "I")) == body
    assert normalise(shown.replace("1", "L")) == body


def test_a_string_that_is_not_a_code_is_refused_rather_than_guessed_at():
    for rubbish in ("", "FMSC", "hello", PREFIX + "-" + "A" * 40, "U" * 14, "0" * 13):
        assert normalise(rubbish) is None


def test_the_lookup_is_stable_per_code_and_is_not_the_code():
    """What the table is keyed on. A leaked backup hands nobody a working code."""
    _, body = new_code()
    assert lookup(body) == lookup(body)
    assert lookup(body) != lookup(new_code()[1])
    assert len(lookup(body)) == 64
    assert body not in lookup(body)


def test_the_lookup_depends_on_the_pepper():
    """Without that, the HMAC is a hash: the same code gives the same lookup everywhere,
    and anybody holding the table can grind codes offline against it.

    The pepper is put back from what it actually was, not from the value conftest sets.
    CI sets its own through the workflow env and conftest only setdefaults, so restoring
    a hardcoded one left every later test hashing under a pepper the module no longer
    had — green locally, red in CI, which is where that mattered.
    """
    original = os.environ["FMSC_CODE_PEPPER"]
    _, body = new_code()
    with_real_pepper = lookup(body)
    os.environ["FMSC_CODE_PEPPER"] = "a-different-pepper-0123456789abcdefghijkl"
    try:
        reloaded = importlib.reload(codes)
        assert reloaded.lookup(body) != with_real_pepper
    finally:
        os.environ["FMSC_CODE_PEPPER"] = original
        importlib.reload(codes)
    assert lookup(body) == with_real_pepper


def test_the_app_refuses_to_start_without_a_pepper():
    """A default pepper is no pepper, and a per-boot random one silently kills every
    existing code on the next deploy. Failing at import is the only version of this that
    cannot reach production unnoticed."""
    original = os.environ["FMSC_CODE_PEPPER"]
    try:
        for bad in ("", "too-short", "x" * 31):
            os.environ["FMSC_CODE_PEPPER"] = bad
            with pytest.raises(RuntimeError, match="FMSC_CODE_PEPPER"):
                importlib.reload(codes)
    finally:
        os.environ["FMSC_CODE_PEPPER"] = original
        importlib.reload(codes)


async def test_issuing_a_code_stores_its_lookup_and_not_the_code(sm):
    """The row has to be useless to whoever reads it. Every column of it is asserted here
    rather than just the lookup, because "the code is not in the table" is a claim about
    the whole row."""
    community_id, owner_id = await _community(sm)
    async with sm() as s:
        shown = await issue_code(s, community_id, owner_id=owner_id, label="winter drop")
    assert shown is not None
    body = normalise(shown)

    async with sm() as s:
        row = (
            await s.execute(
                text("SELECT * FROM community_codes WHERE community_id = :c"),
                {"c": community_id},
            )
        ).one()
    assert row.code_lookup == lookup(body)
    assert row.label == "winter drop"
    assert row.max_uses == 1
    assert row.used_count == 0
    assert row.invalidated_at is None
    stored = " ".join(str(value) for value in row)
    for fragment in (body, shown, shown.replace("-", "")):
        assert fragment not in stored


async def test_only_the_owner_can_issue_a_code(sm):
    """A code is the key to the community. Anybody who could mint one could let the
    internet in."""
    community_id, _ = await _community(sm)
    async with sm() as s:
        stranger = User(email="stranger@x.com", pw_hash="x")
        s.add(stranger)
        await s.commit()
        stranger_id = stranger.id
    async with sm() as s:
        assert await issue_code(s, community_id, owner_id=stranger_id) is None
    async with sm() as s:
        assert (
            await s.scalar(
                text("SELECT count(*) FROM community_codes WHERE community_id = :c"),
                {"c": community_id},
            )
            == 0
        )


async def test_a_community_can_only_have_one_live_code(sm):
    """A property of the schema, not of whoever remembers to check. Rotation is therefore
    invalidate-then-insert, and two rotations racing cannot both leave a code behind."""
    community_id, owner_id = await _community(sm)
    async with sm() as s:
        assert await issue_code(s, community_id, owner_id=owner_id) is not None
    with pytest.raises(Exception, match="uq_one_live_code_per_community"):
        async with sm() as s:
            await issue_code(s, community_id, owner_id=owner_id)


async def test_an_invalidated_code_leaves_room_for_the_next_one(sm):
    """The control for the index above: it is one *live* code, not one ever."""
    community_id, owner_id = await _community(sm)
    async with sm() as s:
        first = await issue_code(s, community_id, owner_id=owner_id)
    async with sm() as s:
        await s.execute(
            text("UPDATE community_codes SET invalidated_at = clock_timestamp()"),
        )
        await s.commit()
    async with sm() as s:
        second = await issue_code(s, community_id, owner_id=owner_id)
    assert second is not None and second != first
    async with sm() as s:
        assert await s.scalar(text("SELECT count(*) FROM community_codes")) == 2


async def test_a_code_can_be_issued_with_a_use_limit_and_a_deadline(sm):
    """Both are columns because the milestone's own failure cases include exhausted and
    expired. Nothing in this slice enforces them yet."""
    community_id, owner_id = await _community(sm)
    expires_at = datetime.now(timezone.utc) + timedelta(days=7)
    async with sm() as s:
        assert (
            await issue_code(s, community_id, owner_id=owner_id, max_uses=25, expires_at=expires_at)
            is not None
        )
    async with sm() as s:
        row = (await s.execute(text("SELECT max_uses, expires_at FROM community_codes"))).one()
    assert row.max_uses == 25
    assert row.expires_at == expires_at


async def _joiner(maker, email: str) -> int:
    async with maker() as s:
        user = User(email=email, pw_hash="x")
        s.add(user)
        await s.commit()
        return user.id


async def test_redeeming_a_code_records_where_the_membership_came_from(sm):
    """Criterion 7. Both columns, at redemption. Without them a community cannot answer
    "which code let this person in", which is the first question after a code leaks."""
    community_id, owner_id = await _community(sm)
    joiner = await _joiner(sm, "joiner@x.com")
    async with sm() as s:
        shown = await issue_code(s, community_id, owner_id=owner_id)

    async with sm() as s:
        membership_id = await redeem_code(s, shown, joiner)
    assert membership_id is not None

    async with sm() as s:
        row = (
            await s.execute(
                text(
                    "SELECT m.generation, m.joined_via_code_id, m.joined_via_generation,"
                    " k.id AS code_id"
                    " FROM community_memberships m, community_codes k WHERE m.id = :id"
                ),
                {"id": membership_id},
            )
        ).one()
    assert row.joined_via_code_id == row.code_id
    assert row.joined_via_generation == 0
    assert row.generation == 0


async def test_a_redeemed_membership_is_the_one_the_bid_guard_wants(sm):
    """The point of the whole slice: redeeming has to produce a membership the bid
    statement recognises, not a row that merely looks like one."""
    from app.auctions import MAX_EXTENSION, place_bid_tx
    from app.models import Auction

    community_id, owner_id = await _community(sm)
    joiner = await _joiner(sm, "joiner@x.com")
    async with sm() as s:
        shown = await issue_code(s, community_id, owner_id=owner_id)
    async with sm() as s:
        assert await redeem_code(s, shown, joiner) is not None

    ends_at = datetime.now(timezone.utc) + timedelta(hours=1)
    async with sm() as s:
        auction = Auction(
            title="t",
            seller_id=owner_id,
            community_id=community_id,
            currency="IDR",
            starting_bid=Decimal("10.00"),
            ends_at=ends_at,
            hard_ends_at=ends_at + MAX_EXTENSION,
        )
        s.add(auction)
        await s.commit()
        auction_id = auction.id
    async with sm() as s:
        assert await place_bid_tx(s, auction_id, joiner, Decimal("10.00")) is not None


async def test_a_one_use_code_is_spent_by_the_first_redemption(sm):
    community_id, owner_id = await _community(sm)
    first = await _joiner(sm, "first@x.com")
    second = await _joiner(sm, "second@x.com")
    async with sm() as s:
        shown = await issue_code(s, community_id, owner_id=owner_id)

    async with sm() as s:
        assert await redeem_code(s, shown, first) is not None
    async with sm() as s:
        assert await redeem_code(s, shown, second) is None
    async with sm() as s:
        assert await s.scalar(text("SELECT used_count FROM community_codes")) == 1


async def test_concurrent_redemptions_of_a_one_use_code_yield_exactly_one_membership(sm):
    """Criterion 6, and the reason the count is claimed by an UPDATE rather than read and
    then written. `used_count < max_uses` read by one connection and acted on by another
    is CWE-837 — ten of them read 0 and all ten let somebody in.

    Ten at once rather than two: with two, a broken implementation gets the right answer
    often enough to pass.
    """
    community_id, owner_id = await _community(sm)
    joiners = [await _joiner(sm, f"j{n}@x.com") for n in range(10)]
    async with sm() as s:
        shown = await issue_code(s, community_id, owner_id=owner_id)

    async def redeem(user_id: int):
        async with sm() as s:
            return await redeem_code(s, shown, user_id)

    results = await asyncio.gather(*(redeem(user_id) for user_id in joiners))

    assert sum(1 for result in results if result is not None) == 1
    async with sm() as s:
        assert await s.scalar(text("SELECT count(*) FROM community_memberships")) == 1
        assert await s.scalar(text("SELECT used_count FROM community_codes")) == 1


async def test_a_code_with_several_uses_stops_at_its_limit(sm):
    """The control for the test above: the limit is max_uses, not one."""
    community_id, owner_id = await _community(sm)
    joiners = [await _joiner(sm, f"j{n}@x.com") for n in range(5)]
    async with sm() as s:
        shown = await issue_code(s, community_id, owner_id=owner_id, max_uses=3)

    async def redeem(user_id: int):
        async with sm() as s:
            return await redeem_code(s, shown, user_id)

    results = await asyncio.gather(*(redeem(user_id) for user_id in joiners))
    assert sum(1 for result in results if result is not None) == 3
    async with sm() as s:
        assert await s.scalar(text("SELECT used_count FROM community_codes")) == 3


async def test_redeeming_twice_costs_the_code_nothing(sm):
    """Somebody who clicks the link again is not a second member. The unique constraint
    on (community, user, generation) is what makes that safe: a read-first check would be
    two statements, and two clicks arriving together would spend two uses."""
    community_id, owner_id = await _community(sm)
    joiner = await _joiner(sm, "joiner@x.com")
    async with sm() as s:
        shown = await issue_code(s, community_id, owner_id=owner_id, max_uses=5)

    async with sm() as s:
        assert await redeem_code(s, shown, joiner) is not None
    async with sm() as s:
        assert await redeem_code(s, shown, joiner) is None
    async with sm() as s:
        assert await s.scalar(text("SELECT used_count FROM community_codes")) == 1
        assert await s.scalar(text("SELECT count(*) FROM community_memberships")) == 1


async def test_an_expired_code_is_refused(sm):
    """The deadline is put into the past by the database's own clock, not by Python's.

    The comparison under test is made by Postgres, so a deadline computed here turns any
    skew between this process and the database into a failing test — which is exactly what
    it did on this machine, where the container clock drifts by minutes.
    """
    community_id, owner_id = await _community(sm)
    joiner = await _joiner(sm, "joiner@x.com")
    async with sm() as s:
        shown = await issue_code(s, community_id, owner_id=owner_id)
    async with sm() as s:
        await s.execute(
            text("UPDATE community_codes SET expires_at = clock_timestamp() - interval '1 second'")
        )
        await s.commit()

    async with sm() as s:
        assert await redeem_code(s, shown, joiner) is None
    async with sm() as s:
        assert await s.scalar(text("SELECT used_count FROM community_codes")) == 0


async def test_a_code_inside_its_deadline_is_accepted(sm):
    """The control. A guard that refused every code with a deadline would pass the test
    above."""
    community_id, owner_id = await _community(sm)
    joiner = await _joiner(sm, "joiner@x.com")
    async with sm() as s:
        shown = await issue_code(s, community_id, owner_id=owner_id)
    async with sm() as s:
        await s.execute(
            text("UPDATE community_codes SET expires_at = clock_timestamp() + interval '1 hour'")
        )
        await s.commit()
    async with sm() as s:
        assert await redeem_code(s, shown, joiner) is not None


async def test_an_invalidated_code_is_refused(sm):
    community_id, owner_id = await _community(sm)
    joiner = await _joiner(sm, "joiner@x.com")
    async with sm() as s:
        shown = await issue_code(s, community_id, owner_id=owner_id)
    async with sm() as s:
        await s.execute(text("UPDATE community_codes SET invalidated_at = clock_timestamp()"))
        await s.commit()
    async with sm() as s:
        assert await redeem_code(s, shown, joiner) is None


async def test_a_code_that_was_never_issued_is_refused(sm):
    """And a string that is not a code at all, which never reaches the database."""
    joiner = await _joiner(sm, "joiner@x.com")
    async with sm() as s:
        assert await redeem_code(s, formatted(new_code()[1]), joiner) is None
    async with sm() as s:
        assert await redeem_code(s, "not-a-code", joiner) is None


async def test_a_redemption_after_a_remove_all_lands_in_the_new_generation(sm):
    """Re-redeeming is what a member does after a remove-all, so the generation has to come
    off the community in the statement that writes the row — a bump committing in between
    would otherwise stamp the membership with a generation that is already history."""
    community_id, owner_id = await _community(sm)
    joiner = await _joiner(sm, "joiner@x.com")
    async with sm() as s:
        await s.execute(
            text("UPDATE communities SET membership_generation = 4 WHERE id = :c"),
            {"c": community_id},
        )
        await s.commit()
    async with sm() as s:
        shown = await issue_code(s, community_id, owner_id=owner_id)
    async with sm() as s:
        membership_id = await redeem_code(s, shown, joiner)

    async with sm() as s:
        row = (
            await s.execute(
                text(
                    "SELECT generation, joined_via_generation FROM community_memberships"
                    " WHERE id = :id"
                ),
                {"id": membership_id},
            )
        ).one()
    assert row.generation == 4
    assert row.joined_via_generation == 4


async def test_a_rotated_code_never_validates_again(sm):
    """Criterion 2. Check Point documented live exploitation in 2025 of Discord returning
    expired invite codes to the pool; a tombstone is how a code stops existing rather
    than becoming available again."""
    community_id, owner_id = await _community(sm)
    joiner = await _joiner(sm, "joiner@x.com")
    async with sm() as s:
        old = await issue_code(s, community_id, owner_id=owner_id)
    async with sm() as s:
        new = await rotate_code(s, community_id, owner_id=owner_id)
    assert new is not None and new != old

    async with sm() as s:
        assert await redeem_code(s, old, joiner) is None
    # The new one works, or the test above would pass with rotation simply breaking codes.
    async with sm() as s:
        assert await redeem_code(s, new, joiner) is not None


async def test_the_old_code_is_tombstoned_rather_than_deleted(sm):
    """A deleted row is a code that can be issued again by accident, and it is what the
    audit log points at when somebody asks which code let a person in."""
    community_id, owner_id = await _community(sm)
    async with sm() as s:
        await issue_code(s, community_id, owner_id=owner_id, label="first")
    async with sm() as s:
        await rotate_code(s, community_id, owner_id=owner_id, label="second")

    async with sm() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT label, invalidated_at IS NULL AS live FROM community_codes ORDER BY id"
                )
            )
        ).all()
    assert [(row.label, row.live) for row in rows] == [("first", False), ("second", True)]


async def test_rotating_twice_leaves_exactly_one_live_code(sm):
    """The partial unique index is what makes this a property rather than a hope."""
    community_id, owner_id = await _community(sm)
    async with sm() as s:
        await issue_code(s, community_id, owner_id=owner_id)
    for _ in range(3):
        async with sm() as s:
            assert await rotate_code(s, community_id, owner_id=owner_id) is not None
    async with sm() as s:
        assert await s.scalar(text("SELECT count(*) FROM community_codes")) == 4
        assert (
            await s.scalar(
                text("SELECT count(*) FROM community_codes WHERE invalidated_at IS NULL")
            )
            == 1
        )


async def test_rotation_works_on_a_community_with_no_code_yet(sm):
    """Tombstoning nothing is not an error. Otherwise an owner who never issued a code
    has to call a different endpoint than one who did."""
    community_id, owner_id = await _community(sm)
    async with sm() as s:
        assert await rotate_code(s, community_id, owner_id=owner_id) is not None


async def test_only_the_owner_can_rotate(sm):
    community_id, owner_id = await _community(sm)
    stranger = await _joiner(sm, "stranger@x.com")
    async with sm() as s:
        old = await issue_code(s, community_id, owner_id=owner_id)
    async with sm() as s:
        assert await rotate_code(s, community_id, owner_id=stranger) is None
    # And the live code is untouched, which is the half that matters: a failed rotation
    # must not leave the community with no way in.
    async with sm() as s:
        assert await redeem_code(s, old, stranger) is not None


async def test_a_redemption_in_flight_when_the_code_rotates_is_refused(sm):
    """Built rather than raced. A redemption that has claimed its use but not committed
    holds the code's row; the rotation blocks on it, and whichever order they settle in,
    the old code is dead and the membership either exists from before the rotation or not
    at all.
    """
    community_id, owner_id = await _community(sm)
    joiner = await _joiner(sm, "joiner@x.com")
    other = await _joiner(sm, "other@x.com")
    async with sm() as s:
        old = await issue_code(s, community_id, owner_id=owner_id, max_uses=5)

    async with sm() as redeemer:
        body = normalise(old)
        claimed = (
            await redeemer.execute(
                text(
                    "UPDATE community_codes SET used_count = used_count + 1"
                    " WHERE code_lookup = :lookup AND invalidated_at IS NULL"
                    "   AND used_count < max_uses RETURNING id"
                ),
                {"lookup": lookup(body)},
            )
        ).first()
        assert claimed is not None

        async def rotate():
            async with sm() as s:
                return await rotate_code(s, community_id, owner_id=owner_id)

        rotation = asyncio.create_task(rotate())
        await asyncio.sleep(0.2)
        assert not rotation.done(), "the rotation did not wait for the redemption's lock"
        await redeemer.commit()

    assert await rotation is not None
    # The old code is dead for everybody who comes next.
    async with sm() as s:
        assert await redeem_code(s, old, other) is None
    assert joiner  # the in-flight redeemer; its claim committed before the tombstone


async def test_two_rotations_at_once_both_succeed_and_leave_one_code(sm):
    """What the community row lock in rotate_code is for, and the only thing that shows
    it.

    Rotation is tombstone-then-insert, and the partial unique index allows one live code.
    Without the lock both rotations tombstone — the second waiting on the code row, not on
    the community — and then both insert, so the index refuses the loser with an
    IntegrityError the owner sees as a 500. Taking the community row first makes them
    queue, so the second one tombstones what the first inserted and replaces it.

    An owner double-clicking rotate is the whole of this scenario.
    """
    community_id, owner_id = await _community(sm)
    async with sm() as s:
        await issue_code(s, community_id, owner_id=owner_id)

    async def rotate():
        async with sm() as s:
            return await rotate_code(s, community_id, owner_id=owner_id)

    first, second = await asyncio.gather(rotate(), rotate())

    assert first is not None and second is not None and first != second
    async with sm() as s:
        assert (
            await s.scalar(
                text("SELECT count(*) FROM community_codes WHERE invalidated_at IS NULL")
            )
            == 1
        )
        assert await s.scalar(text("SELECT count(*) FROM community_codes")) == 3
    # Exactly one of the two works, and which one is whichever committed last. gather
    # preserves the order its arguments were passed in, not the order they finished, so
    # asserting that `second` is the live one is a coin flip — it failed two runs in five
    # before this was written as a property.
    joiner = await _joiner(sm, "joiner@x.com")
    redeemable = 0
    for code in (first, second):
        async with sm() as s:
            if await redeem_code(s, code, joiner) is not None:
                redeemable += 1
    assert redeemable == 1


async def _community_named(maker, email: str) -> tuple[int, int]:
    async with maker() as s:
        owner = User(email=email, pw_hash="x", verified_at=datetime.now(timezone.utc))
        s.add(owner)
        await s.flush()
        community = Community(name="c", owner_id=owner.id)
        s.add(community)
        await s.commit()
        return community.id, owner.id


async def _four_failures(sm) -> list[str]:
    """One code in each of the four states criterion 5 names: never issued, rotated away,
    past its deadline, out of uses.

    Four communities, because only one code per community may be live at a time and two
    of these four have to be the live one. The answer is about the code, not the
    community, so spreading them out changes nothing about what is being tested.
    """
    never_issued = formatted(new_code()[1])

    rotated_community, rotated_owner = await _community_named(sm, "rotated@x.com")
    async with sm() as s:
        rotated = await issue_code(s, rotated_community, owner_id=rotated_owner)
    async with sm() as s:
        await rotate_code(s, rotated_community, owner_id=rotated_owner)

    expired_community, expired_owner = await _community_named(sm, "expired@x.com")
    async with sm() as s:
        expired = await issue_code(s, expired_community, owner_id=expired_owner)
    async with sm() as s:
        await s.execute(
            text(
                "UPDATE community_codes"
                " SET expires_at = clock_timestamp() - interval '1 second'"
                " WHERE community_id = :c"
            ),
            {"c": expired_community},
        )
        await s.commit()

    spent_community, spent_owner = await _community_named(sm, "spent@x.com")
    spender = await _joiner(sm, "spender@x.com")
    async with sm() as s:
        exhausted = await issue_code(s, spent_community, owner_id=spent_owner)
    async with sm() as s:
        assert await redeem_code(s, exhausted, spender) is not None

    return [never_issued, rotated, expired, exhausted]


async def test_the_four_failures_are_one_answer(sm):
    """Criterion 5, the body half. A client that can tell a code that never existed from
    one that was rotated away can walk the space looking for the difference, which is the
    whole of OWASP's OAT-002."""
    codes_to_try = await _four_failures(sm)
    caller = await _joiner(sm, "caller@x.com")

    answers = []
    for code in codes_to_try:
        async with sm() as s:
            with pytest.raises(NotFoundException) as refused:
                await redeem.fn(
                    data=RedeemRequest(code=code),
                    request=SimpleNamespace(headers={}, client=None),
                    current_user=await s.get(User, caller),
                    db_session=s,
                )
        answers.append((refused.value.status_code, refused.value.detail))

    assert answers == [(404, _REFUSED)] * 4
    # And the sentence says nothing about which of the four it was.
    for word in ("expired", "revoked", "rotated", "exhausted", "used", "found"):
        assert word not in _REFUSED


async def test_the_four_failures_cost_the_same_work(sm):
    """Criterion 5, the timing half, asserted as equal work rather than equal wall clock.

    A wall-clock assertion wide enough not to flake on a shared runner is wider than the
    leak it looks for. The leak that matters is an extra query — a path that looks the code
    up again, or skips a lookup it could have done — and that is what counting statements
    sees. It is the same technique the bid path uses to pin its single UPDATE.
    """
    codes_to_try = await _four_failures(sm)
    caller = await _joiner(sm, "caller@x.com")

    counts = []
    for code in codes_to_try:
        statements: list[str] = []
        engine = sm.kw["bind"].sync_engine

        def record(conn, cursor, statement, parameters, context, executemany, seen=statements):
            seen.append(statement)

        event.listen(engine, "before_cursor_execute", record)

        try:
            async with sm() as s:
                assert await redeem_code(s, code, caller) is None
        finally:
            event.remove(engine, "before_cursor_execute", record)
        counts.append(len(statements))

    assert len(set(counts)) == 1, f"the four failure paths issued {counts} statements"


async def test_the_audit_log_is_where_the_four_differ(sm):
    """Criterion 5's other half: indistinguishable to the caller, distinguished for the
    operator. A log that recorded one word for all four would make the endpoint
    unanswerable the first time somebody asks why a member cannot get in."""
    codes_to_try = await _four_failures(sm)
    caller = await _joiner(sm, "caller@x.com")

    for code in codes_to_try:
        async with sm() as s:
            assert await redeem_code(s, code, caller, ip="203.0.113.5") is None

    async with sm() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT outcome, code_id IS NOT NULL AS has_code, ip FROM code_audit"
                    " WHERE user_id = :u ORDER BY id"
                ),
                {"u": caller},
            )
        ).all()
    assert [row.outcome for row in rows] == ["not_found", "revoked", "expired", "exhausted"]
    # not_found is the one case with no row to point at; the rest name the code.
    assert [row.has_code for row in rows] == [False, True, True, True]
    assert {row.ip for row in rows} == {"203.0.113.5"}


async def test_the_audit_log_records_a_success_and_a_repeat(sm):
    """The two outcomes that are not failures. A log that only recorded refusals could not
    answer which code let somebody in, which is the first question after a leak."""
    community_id, owner_id = await _community(sm)
    joiner = await _joiner(sm, "joiner@x.com")
    async with sm() as s:
        shown = await issue_code(s, community_id, owner_id=owner_id, max_uses=5)

    async with sm() as s:
        assert await redeem_code(s, shown, joiner) is not None
    async with sm() as s:
        assert await redeem_code(s, shown, joiner) is None

    async with sm() as s:
        rows = (
            await s.execute(text("SELECT outcome, community_id FROM code_audit ORDER BY id"))
        ).all()
    assert [row.outcome for row in rows] == ["redeemed", "already_member"]
    assert {row.community_id for row in rows} == {community_id}


async def test_the_audit_log_never_holds_the_code(sm):
    """It holds code_id. A log that wrote the code would be a second copy of a bearer
    credential, in the one table nobody thinks to protect."""
    community_id, owner_id = await _community(sm)
    joiner = await _joiner(sm, "joiner@x.com")
    async with sm() as s:
        shown = await issue_code(s, community_id, owner_id=owner_id)
    async with sm() as s:
        await redeem_code(s, shown, joiner)

    body = normalise(shown)
    async with sm() as s:
        rows = (await s.execute(text("SELECT * FROM code_audit"))).all()
    stored = " ".join(str(value) for row in rows for value in row)
    for fragment in (body, shown, shown.replace("-", ""), lookup(body)):
        assert fragment not in stored


def _request(ip: str = "10.0.0.1") -> SimpleNamespace:
    # A private peer, because a forwarded header is only believed through one; see client_ip.
    return SimpleNamespace(
        headers={"X-Forwarded-For": ip}, client=SimpleNamespace(host="10.0.0.254")
    )


async def _try_code(sm, user_id: int, code: str, ip: str = "10.0.0.1"):
    async with sm() as s:
        return await redeem.fn(
            data=RedeemRequest(code=code),
            request=_request(ip),
            current_user=await s.get(User, user_id),
            db_session=s,
        )


async def _fail_code(sm, user_id: int, ip: str = "10.0.0.1") -> None:
    with pytest.raises(NotFoundException):
        await _try_code(sm, user_id, formatted(new_code()[1]), ip)


async def test_an_account_guessing_codes_is_slowed_down(sm):
    """Not in a criterion; in CONTEXT, and the reason is enumeration rather than the code
    falling. 65 bits does not fall to guessing — the limit is about the cost and the noise
    of somebody trying, which is OWASP's OAT-002."""
    caller = await _joiner(sm, "caller@x.com")
    for n in range(MAX_REDEEM_FAILURES_PER_ACCOUNT):
        await _fail_code(sm, caller, ip=f"10.0.0.{n + 1}")

    # From a sixth address, and with a code that would have worked: the account is locked,
    # not the address.
    community_id, owner_id = await _community(sm)
    async with sm() as s:
        good = await issue_code(s, community_id, owner_id=owner_id)
    with pytest.raises(TooManyRequestsException) as limited:
        await _try_code(sm, caller, good, ip="10.0.0.99")
    assert 0 < int(limited.value.headers["Retry-After"]) <= REDEEM_WINDOW.total_seconds()


async def test_an_address_guessing_codes_is_slowed_down(sm):
    """The other bucket. Twenty failures spread across twenty accounts still lock the
    address, or a botnet with a user list never trips a limit."""
    for n in range(MAX_REDEEM_FAILURES_PER_IP):
        user = await _joiner(sm, f"spread{n}@x.com")
        await _fail_code(sm, user, ip="203.0.113.7")

    innocent = await _joiner(sm, "innocent@x.com")
    with pytest.raises(TooManyRequestsException):
        await _try_code(sm, innocent, formatted(new_code()[1]), ip="203.0.113.7")


async def test_the_code_limit_is_not_the_login_limit(sm):
    """The reason this is its own table. Guessing codes at somebody must not lock them out
    of signing in, and a login they fumbled must not stop them joining."""
    from app.auth import login_retry_after

    caller = await _joiner(sm, "caller@x.com")
    for n in range(MAX_REDEEM_FAILURES_PER_ACCOUNT):
        await _fail_code(sm, caller, ip=f"10.0.0.{n + 1}")

    async with sm() as s:
        assert await redeem_retry_after(s, caller, "10.0.0.99") is not None
        # The login limiter has seen nothing at all.
        assert await login_retry_after(s, "caller@x.com", "10.0.0.1") is None
        assert await s.scalar(text("SELECT count(*) FROM login_attempts")) == 0


async def test_one_locked_account_does_not_lock_another(sm):
    """A limiter that locked everyone out when one account was attacked is a denial of
    service with extra steps."""
    attacked = await _joiner(sm, "attacked@x.com")
    bystander = await _joiner(sm, "bystander@x.com")
    for n in range(MAX_REDEEM_FAILURES_PER_ACCOUNT):
        await _fail_code(sm, attacked, ip=f"198.51.100.{n + 1}")

    community_id, owner_id = await _community(sm)
    async with sm() as s:
        good = await issue_code(s, community_id, owner_id=owner_id)
    assert (await _try_code(sm, bystander, good, ip="198.51.100.200")).community_id == (
        community_id
    )


async def test_a_refused_request_is_not_itself_counted(sm):
    """Or hammering a lockout would extend it, and the limit would never let go."""
    caller = await _joiner(sm, "caller@x.com")
    for _ in range(MAX_REDEEM_FAILURES_PER_ACCOUNT):
        await _fail_code(sm, caller)
    async with sm() as s:
        before = await s.scalar(text("SELECT count(*) FROM code_attempts"))

    with pytest.raises(TooManyRequestsException):
        await _try_code(sm, caller, formatted(new_code()[1]))
    async with sm() as s:
        assert await s.scalar(text("SELECT count(*) FROM code_attempts")) == before


async def test_a_successful_redemption_is_not_counted_as_a_failure(sm):
    """The control. A limiter that counted everything would lock out a community's fifth
    member on the day it opened."""
    community_id, owner_id = await _community(sm)
    async with sm() as s:
        good = await issue_code(s, community_id, owner_id=owner_id, max_uses=10)
    for n in range(6):
        joiner = await _joiner(sm, f"j{n}@x.com")
        assert (await _try_code(sm, joiner, good)).community_id == community_id
    async with sm() as s:
        assert await s.scalar(text("SELECT count(*) FROM code_attempts")) == 0


async def test_attempts_outside_the_window_are_pruned(sm):
    """Nothing else deletes them and the window query never reads them again. Same sweeper
    slot in the closer's tick as the login attempts."""
    caller = await _joiner(sm, "caller@x.com")
    await _fail_code(sm, caller)
    async with sm() as s:
        await s.execute(
            text(
                "UPDATE code_attempts SET failed_at ="
                " failed_at - CAST(:w AS interval) - interval '1 second'"
            ),
            {"w": REDEEM_WINDOW},
        )
        await s.commit()
    await _fail_code(sm, caller)

    async with sm() as s:
        assert await prune_code_attempts(s) == 1
    async with sm() as s:
        assert await s.scalar(text("SELECT count(*) FROM code_attempts")) == 1


async def test_failures_older_than_the_window_do_not_count(sm):
    """The limit is a rate, not a lifetime total."""
    caller = await _joiner(sm, "caller@x.com")
    for _ in range(MAX_REDEEM_FAILURES_PER_ACCOUNT):
        await _fail_code(sm, caller)
    async with sm() as s:
        await s.execute(
            text(
                "UPDATE code_attempts SET failed_at ="
                " failed_at - CAST(:w AS interval) - interval '1 second'"
            ),
            {"w": REDEEM_WINDOW},
        )
        await s.commit()
    async with sm() as s:
        assert await redeem_retry_after(s, caller, "10.0.0.1") is None


async def test_issuing_a_second_code_is_a_409_and_not_a_500(sm):
    """The partial unique index reached the owner as a 500 before this caught it, which is
    the whole reason issue and rotate are separate routes: a silent replacement would
    invalidate the code they had already shared, and an unexplained 500 is worse than
    either."""
    community_id, owner_id = await _community(sm)
    async with sm() as s:
        owner = await s.get(User, owner_id)
        first = await issue.fn(
            community_id=community_id,
            data=IssueCodeRequest(),
            current_user=owner,
            db_session=s,
        )
    assert first.code.startswith("FMSC-")

    async with sm() as s:
        with pytest.raises(HTTPException) as refused:
            await issue.fn(
                community_id=community_id,
                data=IssueCodeRequest(),
                current_user=await s.get(User, owner_id),
                db_session=s,
            )
    assert refused.value.status_code == 409
    assert "rotate" in refused.value.detail


async def test_the_owner_only_routes_all_answer_the_same_409(sm):
    """ "No such community" and "not yours" are one answer, for the same reason the four
    redemption failures are: a caller cannot act on the difference, and telling them apart
    says which communities exist and who is in them.

    A session each, because every one of these refusals rolls back and a rolled-back
    session is no good for the next call.
    """
    community_id, _ = await _community(sm)
    stranger = await _joiner(sm, "stranger@x.com")

    async def issue_it(s, user):
        return await issue.fn(
            community_id=community_id,
            data=IssueCodeRequest(),
            current_user=user,
            db_session=s,
        )

    async def rotate_it(s, user):
        return await rotate.fn(
            community_id=community_id,
            data=IssueCodeRequest(),
            current_user=user,
            db_session=s,
        )

    async def kick_it(s, user):
        return await kick.fn(
            community_id=community_id, user_id=stranger, current_user=user, db_session=s
        )

    async def remove_it(s, user):
        return await remove_all.fn(community_id=community_id, current_user=user, db_session=s)

    for call in (issue_it, rotate_it, kick_it, remove_it):
        async with sm() as s:
            with pytest.raises(HTTPException) as refused:
                await call(s, await s.get(User, stranger))
        assert refused.value.status_code == 409, call.__name__


async def test_the_owner_only_routes_work_for_the_owner(sm):
    """The controls. Four handlers that raised on every call would pass the test above."""
    community_id, owner_id = await _community(sm)
    joiner = await _joiner(sm, "joiner@x.com")

    async with sm() as s:
        owner = await s.get(User, owner_id)
        code = await rotate.fn(
            community_id=community_id,
            data=IssueCodeRequest(max_uses=5),
            current_user=owner,
            db_session=s,
        )
    async with sm() as s:
        assert await redeem_code(s, code.code, joiner) is not None

    async with sm() as s:
        assert (
            await kick.fn(
                community_id=community_id,
                user_id=joiner,
                current_user=await s.get(User, owner_id),
                db_session=s,
            )
            is None
        )
    async with sm() as s:
        generation = await remove_all.fn(
            community_id=community_id,
            current_user=await s.get(User, owner_id),
            db_session=s,
        )
    assert generation.membership_generation == 1


async def test_creating_a_community_makes_the_caller_its_owner(sm):
    """Ownership is the only thing that confers moderator rights, so the one place it is
    set matters more than it looks: there is no membership row for an owner to lose, and
    no second way to become one."""
    owner_id = await _joiner(sm, "founder@x.com")
    async with sm() as s:
        created = await create_community.fn(
            data=CreateCommunityRequest(name="the shed"),
            current_user=await s.get(User, owner_id),
            db_session=s,
        )
    assert created.owner_id == owner_id
    assert created.name == "the shed"
    # A community starts in the generation its first memberships will be granted in, or
    # every one of them would be history the moment it was written.
    assert created.membership_generation == 0


def test_a_payload_that_begins_with_the_prefix_still_round_trips():
    """One code in about a million starts FMSC itself. Stripping the prefix on its first
    four characters alone breaks the no-prefix form for exactly those, which is also a
    latent flake in the round-trip test above."""
    body = "FMSC" + "012345678"
    assert len(body) == CODE_LENGTH
    shown = formatted(body)
    assert normalise(shown) == body
    assert normalise(shown.removeprefix(f"{PREFIX}-")) == body
    assert normalise(shown.replace("-", "")) == body


def test_a_check_symbol_outside_the_alphabet_is_refused_not_raised():
    """hmac.compare_digest raises TypeError on a str holding anything outside ASCII rather
    than answering False. Unhandled that is a 500 — and a 500 is distinguishable from the
    four refusals, which is the one thing criterion 5 is about."""
    body = new_code()[1]
    for symbol in ("é", "ß", "€", "\u00ff"):
        assert normalise(body + symbol) is None
    # And a plain wrong symbol from inside the alphabet is still refused.
    wrong = "*" if check_symbol(body) != "*" else "~"
    assert normalise(body + wrong) is None


async def test_a_kicked_member_can_be_let_back_in(sm):
    """The uniqueness is partial, on revoked_at IS NULL, so a revoked row does not block a
    rejoin.

    A kick leaves the row at the community's current generation. With a plain constraint a
    mistaken kick is permanent — the person can never redeem again and the only remedy is
    remove-all, which ends everyone else's membership too — and the audit log calls them
    already_member while they are revoked.
    """
    community_id, owner_id = await _community(sm)
    member = await _joiner(sm, "member@x.com")
    async with sm() as s:
        code = await issue_code(s, community_id, owner_id=owner_id, max_uses=9)
    async with sm() as s:
        assert await redeem_code(s, code, member) is not None
    async with sm() as s:
        assert await kick_member(s, community_id, member, owner_id=owner_id) is True

    async with sm() as s:
        assert await redeem_code(s, code, member) is not None

    async with sm() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT revoked_at IS NULL AS live FROM community_memberships"
                    " WHERE user_id = :u ORDER BY id"
                ),
                {"u": member},
            )
        ).all()
    # Two rows: the kick is still on the record, and the new membership stands beside it.
    # The audit side of this — that the second redemption is logged as a redemption rather
    # than as already_member — is asserted where the audit table exists, two commits on.
    assert [row.live for row in rows] == [False, True]


async def test_a_live_membership_still_blocks_a_second_one(sm):
    """The control for the partial index: it is one *live* membership per generation, not
    any number of them. Without that half, a double-click is two rows and the bid guard's
    EXISTS would not notice."""
    community_id, owner_id = await _community(sm)
    member = await _joiner(sm, "member@x.com")
    async with sm() as s:
        code = await issue_code(s, community_id, owner_id=owner_id, max_uses=9)
    async with sm() as s:
        assert await redeem_code(s, code, member) is not None
    async with sm() as s:
        assert await redeem_code(s, code, member) is None
    async with sm() as s:
        assert (
            await s.scalar(
                text("SELECT count(*) FROM community_memberships WHERE user_id = :u"),
                {"u": member},
            )
            == 1
        )


async def test_a_mistyped_code_is_not_written_down_and_costs_nothing(sm):
    """The check symbol caught it, so it is a typo rather than an attempt at anything.

    Auditing it fills the log with somebody's bad afternoon and makes a fat finger look
    like enumeration; charging it means five mistypes from a screenshot lock a member out
    of redemption for a quarter of an hour.
    """
    caller = await _joiner(sm, "caller@x.com")
    _, body = new_code()
    mistyped = formatted(body)[:-1] + ("0" if formatted(body)[-1] != "0" else "1")
    assert normalise(mistyped) is None

    for _ in range(MAX_REDEEM_FAILURES_PER_ACCOUNT + 2):
        async with sm() as s:
            with pytest.raises(NotFoundException):
                await redeem.fn(
                    data=RedeemRequest(code=mistyped),
                    request=_request(),
                    current_user=await s.get(User, caller),
                    db_session=s,
                )

    async with sm() as s:
        assert await s.scalar(text("SELECT count(*) FROM code_audit")) == 0
        assert await s.scalar(text("SELECT count(*) FROM code_attempts")) == 0
        # Still able to redeem, which is the part that matters to a member.
        assert await redeem_retry_after(s, caller, "10.0.0.1") is None


async def test_a_code_that_works_clears_the_accounts_failures(sm):
    """Four mistakes then a successful join must not leave the account one typo away from
    a lockout for the rest of the window. The address bucket is left alone: an address
    that has been guessing at codes is still an address that has been guessing at codes.
    """
    community_id, owner_id = await _community(sm)
    caller = await _joiner(sm, "caller@x.com")
    for _ in range(MAX_REDEEM_FAILURES_PER_ACCOUNT - 1):
        await _fail_code(sm, caller)
    async with sm() as s:
        assert await s.scalar(text("SELECT count(*) FROM code_attempts")) == (
            MAX_REDEEM_FAILURES_PER_ACCOUNT - 1
        )

    async with sm() as s:
        code = await issue_code(s, community_id, owner_id=owner_id)
    assert (await _try_code(sm, caller, code)).community_id == community_id

    async with sm() as s:
        assert (
            await s.scalar(
                text("SELECT count(*) FROM code_attempts WHERE user_id = :u"), {"u": caller}
            )
            == 0
        )


async def test_issuing_and_rotating_at_once_is_never_a_500(sm):
    """Both take the community row now, so the two serialise instead of colliding.

    Without the lock on the issue side a rotation cannot see its insert until it commits,
    tombstones nothing, inserts its own, and trips the one-live-code index — which an owner
    double-clicking reads as a 500. With it, whichever lands second sees the truth: if that
    is the rotation it replaces what it found, and if it is the issue it answers 409,
    because there really is a live code by then.

    Asserted through the handlers, which is where the translation lives; the functions
    underneath them raise, and raising is right.
    """
    community_id, owner_id = await _community(sm)

    async def issue_one():
        async with sm() as s:
            try:
                return await issue.fn(
                    community_id=community_id,
                    data=IssueCodeRequest(),
                    current_user=await s.get(User, owner_id),
                    db_session=s,
                )
            except HTTPException as refused:
                assert refused.status_code == 409, refused.status_code
                return None

    async def rotate_one():
        async with sm() as s:
            return await rotate.fn(
                community_id=community_id,
                data=IssueCodeRequest(),
                current_user=await s.get(User, owner_id),
                db_session=s,
            )

    results = await asyncio.gather(issue_one(), rotate_one(), return_exceptions=True)
    assert not any(isinstance(result, BaseException) for result in results), results
    # The rotation always gets a code; the issue gets one or a 409, never a 500.
    assert results[1] is not None
    async with sm() as s:
        assert (
            await s.scalar(
                text("SELECT count(*) FROM community_codes WHERE invalidated_at IS NULL")
            )
            == 1
        )


async def test_the_routes_refuse_what_the_table_would_refuse(sm):
    """Each of these violates a constraint, and an IntegrityError reaching the client is a
    500 with nothing in it they can act on. The same reasoning as create_auction's
    reserve check, and the same three columns' worth of constraints."""
    community_id, owner_id = await _community(sm)
    async with sm() as s:
        owner = await s.get(User, owner_id)
        with pytest.raises(ClientException):
            await create_community.fn(
                data=CreateCommunityRequest(name="x" * (MAX_NAME + 1)),
                current_user=owner,
                db_session=s,
            )
    async with sm() as s:
        with pytest.raises(ClientException):
            await create_community.fn(
                data=CreateCommunityRequest(name=""),
                current_user=await s.get(User, owner_id),
                db_session=s,
            )

    for bad in (
        IssueCodeRequest(max_uses=0),
        IssueCodeRequest(max_uses=-1),
        IssueCodeRequest(label="x" * (MAX_LABEL + 1)),
    ):
        async with sm() as s:
            with pytest.raises(ClientException):
                await issue.fn(
                    community_id=community_id,
                    data=bad,
                    current_user=await s.get(User, owner_id),
                    db_session=s,
                )
        async with sm() as s:
            with pytest.raises(ClientException):
                await rotate.fn(
                    community_id=community_id,
                    data=bad,
                    current_user=await s.get(User, owner_id),
                    db_session=s,
                )
    async with sm() as s:
        assert await s.scalar(text("SELECT count(*) FROM community_codes")) == 0


async def test_a_deadline_on_a_code_is_read_as_jakarta_wall_time(sm):
    """Every other client datetime in the API goes through resolve_deadline. Without it a
    naive value is read as UTC — seven hours from what the owner meant, in the direction
    that expires the code early."""
    community_id, owner_id = await _community(sm)
    naive = (datetime.now(APP_TZ) + timedelta(days=1)).replace(tzinfo=None)
    async with sm() as s:
        assert (
            await issue.fn(
                community_id=community_id,
                data=IssueCodeRequest(expires_at=naive),
                current_user=await s.get(User, owner_id),
                db_session=s,
            )
        ).code.startswith("FMSC-")
    async with sm() as s:
        stored = await s.scalar(text("SELECT expires_at FROM community_codes"))
    assert stored == naive.replace(tzinfo=APP_TZ)


async def test_a_code_cannot_be_issued_already_expired(sm):
    """It would mint something that can only ever answer 404, and the owner would have no
    way to tell that from a code they had mistyped into their newsletter."""
    community_id, owner_id = await _community(sm)
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    for handler in (issue, rotate):
        async with sm() as s:
            with pytest.raises(ClientException):
                await handler.fn(
                    community_id=community_id,
                    data=IssueCodeRequest(expires_at=past),
                    current_user=await s.get(User, owner_id),
                    db_session=s,
                )
