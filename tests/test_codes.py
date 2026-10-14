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

import pytest
from sqlalchemy import text

from app import codes
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
from app.communities import issue_code, kick_member, redeem_code
from app.models import Community, User


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
    community_id, owner_id = await _community(sm)
    joiner = await _joiner(sm, "joiner@x.com")
    async with sm() as s:
        shown = await issue_code(
            s,
            community_id,
            owner_id=owner_id,
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
    async with sm() as s:
        assert await redeem_code(s, shown, joiner) is None
    async with sm() as s:
        assert await s.scalar(text("SELECT used_count FROM community_codes")) == 0


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
