"""M10's three acceptance criteria, against a fake transport.

The bot is a client: it posts what happened and a link back, and bidding stays on the web.
So everything here is about what reaches a guild channel — never about what Discord does
with it, which is why the transport is a callable the tests record instead of a service to
stand up.

The fake is a plain `async def`, not a mock. What the poster needs from a transport is one
coroutine taking a channel and a string; anything larger would be a seam invented for the
test rather than the shape the code actually uses.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import msgspec
import pytest
from litestar.exceptions import ClientException, HTTPException
from nacl.signing import SigningKey
from sqlalchemy import text

from app import discord
from app.auctions import (
    cancel_auction,
    close_due,
    create_auction,
    place_bid_tx,
    prune_outbox,
)
from app.codes import normalise
from app.communities import link_discord, rotate
from app.discord import MANAGE_GUILD, interactions, post_pending
from app.models import Community, CommunityMembership, User
from app.schemas import CreateAuctionRequest, DiscordLinkRequest, IssueCodeRequest

# Snowflakes, as Discord sends them: strings, because they are 64-bit unsigned and
# JavaScript cannot hold one. Stored as given rather than parsed.
GUILD = "123456789012345678"
CHANNEL = "987654321098765432"

CODE_IN_A_MESSAGE = re.compile(r"FMSC(?:-[0-9A-Z*~$=]+)+")


class FakeDiscord:
    """Records what would have been posted. One coroutine is the whole transport."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, str]] = []

    async def __call__(self, channel_id: str, content: str) -> None:
        self.posts.append((channel_id, content))


@pytest.fixture
def fake() -> FakeDiscord:
    return FakeDiscord()


@pytest.fixture
def signer(monkeypatch) -> SigningKey:
    """A keypair the interactions route will accept.

    Discord signs every interaction with its application's Ed25519 key and refuses to
    register an endpoint that does not check it — it sends deliberately bad signatures
    during validation and expects a 401. The suite therefore has to sign the real way.
    """
    from app import discord

    key = SigningKey(b"fmsc-test-discord-signing-seed32")
    monkeypatch.setattr(discord, "PUBLIC_KEY", key.verify_key.encode().hex())
    return key


class Interaction:
    """What the route reads off a Litestar request: the raw body and two headers.

    The raw body, not the parsed one: the signature covers the exact bytes Discord sent,
    so re-encoding the payload before verifying it is how a correct implementation starts
    rejecting correct requests.
    """

    def __init__(self, signer: SigningKey, payload: dict, timestamp: str = "1700000000") -> None:
        self._raw = msgspec.json.encode(payload)
        signature = signer.sign(timestamp.encode() + self._raw).signature.hex()
        self.headers = {
            "X-Signature-Ed25519": signature,
            "X-Signature-Timestamp": timestamp,
        }

    async def body(self) -> bytes:
        return self._raw


def invite(permissions: int = MANAGE_GUILD, guild_id: str = GUILD) -> dict:
    """The payload Discord POSTs for `/fmsc invite`."""
    return {
        "type": 2,
        "guild_id": guild_id,
        "member": {"permissions": str(permissions)},
        "data": {"name": "fmsc", "options": [{"name": "invite", "type": 1}]},
    }


async def _community(maker) -> tuple[int, int]:
    async with maker() as s:
        owner = User(email="owner@x.com", pw_hash="x", verified_at=datetime.now(timezone.utc))
        s.add(owner)
        await s.flush()
        community = Community(name="c", owner_id=owner.id)
        s.add(community)
        await s.commit()
        return community.id, owner.id


async def _linked(maker) -> tuple[int, int]:
    """A community with a guild channel attached to it. Returns (community, owner)."""
    community_id, owner_id = await _community(maker)
    async with maker() as s:
        await link_discord.fn(
            community_id=community_id,
            data=DiscordLinkRequest(guild_id=GUILD, channel_id=CHANNEL),
            current_user=await s.get(User, owner_id),
            db_session=s,
        )
    return community_id, owner_id


async def _member(maker, community_id: int, email: str = "member@x.com") -> int:
    """Somebody who may bid in the community. The bid gate reads the row, not the code."""
    async with maker() as s:
        user = User(email=email, pw_hash="x")
        s.add(user)
        await s.flush()
        s.add(CommunityMembership(community_id=community_id, user_id=user.id, generation=0))
        await s.commit()
        return user.id


async def test_the_bot_posts_open_bid_and_close_for_a_watched_auction(sm, channels, fake):
    """Criterion 1. The three events a guild is there for, in the order they happened.

    Every one of them is already an outbox row, written in the transaction that caused it,
    so the poster is a second reader of a queue the money path already fills — not a new
    announcement path that could disagree with the sockets about what happened.
    """
    community_id, owner_id = await _linked(sm)
    bidder = await _member(sm, community_id)
    ends_at = datetime.now(timezone.utc) + timedelta(hours=1)

    async with sm() as s:
        auction = await create_auction.fn(
            data=CreateAuctionRequest(
                title="a lot worth watching",
                starting_bid=Decimal("10.00"),
                ends_at=ends_at,
                community_id=community_id,
            ),
            current_user=await s.get(User, owner_id),
            db_session=s,
        )
    async with sm() as s:
        assert await place_bid_tx(s, auction.id, bidder, Decimal("10.00")) is not None
    async with sm() as s:
        await s.execute(
            text("UPDATE auctions SET ends_at = clock_timestamp() - interval '1 second'"),
        )
        await s.commit()
    assert await close_due(sm, channels) == 1

    assert await post_pending(sm, send=fake) == 3
    assert [channel for channel, _ in fake.posts] == [CHANNEL] * 3
    opened, bid, closed = (content for _, content in fake.posts)
    assert "a lot worth watching" in opened
    assert "open" in opened and f"/auctions/{auction.id}" in opened
    assert "bid" in bid and "10.00" in bid
    assert "closed" in closed and "10.00" in closed


async def test_a_slash_command_posts_a_code_to_the_guild_and_logs_only_its_id(sm, signer, caplog):
    """Criterion 2. The reply *is* the post: a non-ephemeral interaction response puts the
    code in the channel inside Discord's three-second budget with no outbound call.

    And the code is not in the log. It is a bearer credential with no second copy
    anywhere, so a log line holding one is a log line that hands out memberships — which
    is why what gets logged is the row's id.
    """
    community_id, _ = await _linked(sm)

    with caplog.at_level(logging.INFO):
        async with sm() as s:
            reply = await interactions.fn(request=Interaction(signer, invite()), db_session=s)

    posted = CODE_IN_A_MESSAGE.search(reply["data"]["content"])
    assert posted is not None, reply
    code = posted.group(0)
    assert normalise(code) is not None
    # Ephemeral is flag 64. Absent, because the whole point is that the guild can read it.
    assert reply["data"].get("flags", 0) == 0

    async with sm() as s:
        code_id = await s.scalar(
            text(
                "SELECT id FROM community_codes WHERE community_id = :c AND invalidated_at IS NULL"
            ),
            {"c": community_id},
        )
    assert f"code_id={code_id}" in caplog.text
    assert code not in caplog.text
    assert normalise(code) not in caplog.text


async def test_rotating_a_code_broadcasts_the_new_one(sm, fake):
    """Criterion 3, and the gap M9 named. Rotation invalidates every code at once, so the
    members holding the old one have to be told the new one by the same channel that is
    already the community's — otherwise it travels out of band, which is what M9 shipped
    with.
    """
    community_id, owner_id = await _linked(sm)

    async with sm() as s:
        first = await rotate.fn(
            community_id=community_id,
            data=IssueCodeRequest(max_uses=5),
            current_user=await s.get(User, owner_id),
            db_session=s,
            send=fake,
        )
    async with sm() as s:
        second = await rotate.fn(
            community_id=community_id,
            data=IssueCodeRequest(max_uses=5),
            current_user=await s.get(User, owner_id),
            db_session=s,
            send=fake,
        )

    assert first.code != second.code
    channel, content = fake.posts[-1]
    assert channel == CHANNEL
    assert second.code in content
    assert first.code not in content


@pytest.fixture
def configured(monkeypatch):
    """A bot token, and every outbound request answered by `handler` instead of Discord."""

    def install(handler) -> list[httpx.Request]:
        seen: list[httpx.Request] = []

        def record(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(len(seen))

        # The real class, captured before the name is replaced: discord.httpx is the httpx
        # module itself, so a lambda calling httpx.AsyncClient would call itself.
        real = httpx.AsyncClient
        monkeypatch.setattr(discord, "BOT_TOKEN", "a-bot-token")
        monkeypatch.setattr(
            discord.httpx,
            "AsyncClient",
            lambda **kwargs: real(transport=httpx.MockTransport(record), **kwargs),
        )
        return seen

    return install


async def test_a_rate_limited_message_waits_the_time_discord_asks_for_and_retries(configured):
    """A 429 carries retry_after, and honouring it is the difference between a short wait
    and a longer ban."""
    seen = configured(
        lambda nth: (
            httpx.Response(429, json={"retry_after": 0.01})
            if nth == 1
            else httpx.Response(200, json={})
        )
    )

    assert await discord.send(CHANNEL, "hello") is True
    assert len(seen) == 2
    assert str(seen[0].url) == f"{discord.API}/channels/{CHANNEL}/messages"
    # Every ping suppressed. The content carries a seller's free text, so a lot titled
    # "@everyone" would otherwise notify a whole guild on command.
    assert msgspec.json.decode(seen[0].content)["allowed_mentions"] == {"parse": []}


async def test_a_second_rate_limit_gives_up_instead_of_waiting_again(configured):
    """One retry, not a loop: the poster will be back on the next tick anyway, and a tick
    that sleeps through its rate limit is a tick that stops closing auctions."""
    seen = configured(lambda nth: httpx.Response(429, json={"retry_after": 0.01}))

    assert await discord.send(CHANNEL, "hello") is False
    assert len(seen) == 2


async def test_a_refused_message_is_logged_rather_than_raised(configured, caplog):
    """A deleted channel or a revoked token answers 4xx forever. Raising it would take out
    the closer tick that called it."""
    configured(lambda nth: httpx.Response(403, json={"message": "Missing Access"}))

    with caplog.at_level(logging.WARNING):
        assert await discord.send(CHANNEL, "hello") is False
    assert "403" in caplog.text


async def test_an_unreachable_discord_is_logged_rather_than_raised(configured, caplog):
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    configured(lambda nth: boom(None))

    with caplog.at_level(logging.WARNING):
        assert await discord.send(CHANNEL, "hello") is False
    assert "unreachable" in caplog.text


async def test_nothing_is_posted_when_discord_is_not_configured(monkeypatch, caplog):
    """Unset means off. A deployment with no guild has nothing to post to, and making the
    app refuse to start over it would make Discord a dependency of selling."""
    monkeypatch.setattr(discord, "BOT_TOKEN", "")

    with caplog.at_level(logging.INFO):
        assert await discord.send(CHANNEL, "hello") is False
    assert "not configured" in caplog.text


async def _link(maker, community_id: int, user_id: int, channel_id: str = CHANNEL, **kwargs):
    async with maker() as s:
        return await link_discord.fn(
            community_id=community_id,
            data=DiscordLinkRequest(guild_id=kwargs.get("guild_id", GUILD), channel_id=channel_id),
            current_user=await s.get(User, user_id),
            db_session=s,
        )


async def _links(maker, community_id: int) -> list:
    async with maker() as s:
        return (
            await s.execute(
                text("SELECT guild_id, channel_id FROM community_discord WHERE community_id = :c"),
                {"c": community_id},
            )
        ).all()


async def test_the_owner_points_a_community_at_a_guild_channel(sm):
    community_id, owner_id = await _community(sm)
    await _link(sm, community_id, owner_id)

    assert await _links(sm, community_id) == [(GUILD, CHANNEL)]


async def test_relinking_moves_the_channel_rather_than_adding_one(sm):
    """An owner moving the bot to another channel must not pass through a state where the
    community is linked to nothing — which a delete and an insert would."""
    community_id, owner_id = await _community(sm)
    await _link(sm, community_id, owner_id)
    await _link(sm, community_id, owner_id, channel_id="111111111111111111")

    assert await _links(sm, community_id) == [(GUILD, "111111111111111111")]


async def test_a_stranger_cannot_link_somebody_elses_community(sm):
    """Ownership is in the WHERE, so the INSERT selects no row for anybody else."""
    community_id, _ = await _community(sm)
    async with sm() as s:
        stranger = User(email="stranger@x.com", pw_hash="x")
        s.add(stranger)
        await s.commit()
        stranger_id = stranger.id

    with pytest.raises(HTTPException) as refused:
        await _link(sm, community_id, stranger_id)
    assert refused.value.status_code == 409
    assert await _links(sm, community_id) == []


async def test_a_guild_cannot_be_linked_to_two_communities(sm):
    """A slash command run in a guild has to have one community to answer for."""
    first, first_owner = await _community(sm)
    await _link(sm, first, first_owner)
    async with sm() as s:
        owner = User(email="second@x.com", pw_hash="x", verified_at=datetime.now(timezone.utc))
        s.add(owner)
        await s.flush()
        second = Community(name="second", owner_id=owner.id)
        s.add(second)
        await s.commit()
        second_id, second_owner = second.id, owner.id

    with pytest.raises(HTTPException) as refused:
        await _link(sm, second_id, second_owner)
    assert refused.value.status_code == 409
    assert "already linked" in refused.value.detail


async def test_a_value_that_is_not_a_snowflake_is_refused_by_name(sm):
    """A 400 naming the field, rather than the 500 a value too long for the column
    becomes. The same reasoning as max_uses and label."""
    community_id, owner_id = await _community(sm)

    for field, guild_id, channel_id in (
        ("guild_id", "not-a-snowflake", CHANNEL),
        ("channel_id", GUILD, "12"),
    ):
        with pytest.raises(ClientException) as refused:
            await _link(sm, community_id, owner_id, channel_id=channel_id, guild_id=guild_id)
        assert field in refused.value.detail
    assert await _links(sm, community_id) == []


async def _auction(maker, owner_id: int, community_id: int | None = None, title: str = "t"):
    async with maker() as s:
        return await create_auction.fn(
            data=CreateAuctionRequest(
                title=title,
                starting_bid=Decimal("10.00"),
                ends_at=datetime.now(timezone.utc) + timedelta(hours=1),
                community_id=community_id,
            ),
            current_user=await s.get(User, owner_id),
            db_session=s,
        )


async def _unposted(maker) -> int:
    async with maker() as s:
        return await s.scalar(text("SELECT count(*) FROM outbox WHERE discord_at IS NULL"))


async def test_an_event_for_an_auction_no_guild_follows_is_retired_unposted(sm, fake):
    """A public auction has nowhere to post to, and its events still have to leave the
    queue — the index the poster reads is partial, so a row nobody retires is a row it
    carries forever."""
    community_id, owner_id = await _community(sm)
    await _auction(sm, owner_id)

    assert await post_pending(sm, send=fake) == 1
    assert fake.posts == []
    assert await _unposted(sm) == 0


async def test_a_second_pass_posts_nothing(sm, fake):
    """discord_at is the record of the attempt, so a tick that runs twice says it once."""
    _, owner_id = await _linked(sm)
    await _auction(sm, owner_id)

    assert await post_pending(sm, send=fake) == 1
    assert await post_pending(sm, send=fake) == 0


async def test_an_event_this_version_does_not_understand_is_skipped_not_raised(sm, fake):
    """During a rolling deploy the queue can hold a payload written by code this version
    does not know. A tick that died on one would stop closing auctions."""
    community_id, owner_id = await _linked(sm)
    auction = await _auction(sm, owner_id, community_id)
    async with sm() as s:
        await s.execute(
            text(
                "INSERT INTO outbox (auction_id, seq, payload, created_at, updated_at)"
                " VALUES (:a, 99, :payload, clock_timestamp(), clock_timestamp())"
            ),
            {"a": auction.id, "payload": '{"type": "something-later", "auction_id": 1}'},
        )
        await s.commit()

    assert await post_pending(sm, send=fake) == 2
    assert len(fake.posts) == 1
    assert "is open" in fake.posts[0][1]
    assert await _unposted(sm) == 0


async def test_a_withdrawn_lot_is_announced_and_a_closed_one_names_the_winner(sm, fake, channels):
    """The two events a status line has to cover beyond opening. Cancelling tells the
    guild the lot is gone, and the closer tells it what happened to the money."""
    community_id, owner_id = await _linked(sm)
    cancelled = await _auction(sm, owner_id, community_id, title="withdrawn lot")
    quiet = await _auction(sm, owner_id, community_id, title="unloved lot")
    async with sm() as s:
        await cancel_auction.fn(
            auction_id=cancelled.id,
            current_user=await s.get(User, owner_id),
            db_session=s,
            channels=channels,
        )
    async with sm() as s:
        await s.execute(
            text(
                "UPDATE auctions SET ends_at = clock_timestamp() - interval '1 second'"
                " WHERE id = :id"
            ),
            {"id": quiet.id},
        )
        await s.commit()
    assert await close_due(sm, channels) == 1

    await post_pending(sm, send=fake)
    posted = [content for _, content in fake.posts]
    assert any("withdrawn lot" in line and "was withdrawn" in line for line in posted)
    assert any("unloved lot" in line and "no bids" in line for line in posted)


async def test_the_outbox_is_not_pruned_until_both_readers_have_had_it(sm, fake):
    """prune_outbox waits for the poster as well as for the relay. Deleting a row the
    poster has not reached is the one way an event is lost without a crash."""
    _, owner_id = await _linked(sm)
    await _auction(sm, owner_id)
    async with sm() as s:
        await s.execute(text("UPDATE outbox SET sent_at = clock_timestamp() - interval '2 days'"))
        await s.commit()

    async with sm() as s:
        assert await prune_outbox(s) == 0
    assert await _unposted(sm) == 1

    # The control: once the poster has had it, the same prune collects it.
    assert await post_pending(sm, send=fake) == 1
    async with sm() as s:
        assert await prune_outbox(s) == 1
