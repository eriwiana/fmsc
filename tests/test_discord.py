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

import msgspec
import pytest
from nacl.signing import SigningKey
from sqlalchemy import text

from app.auctions import close_due, create_auction, place_bid_tx
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
