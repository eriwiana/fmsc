"""The Discord client: what a guild is told, and nothing it can act on.

M10 is a client. The bot posts what happened and a link back; bidding stays on the web, so
no money path runs through here.

Two directions, deliberately asymmetric. Outbound is the REST API over httpx — a bot token
and one POST per message. Inbound is Discord POSTing an interaction to this app, which is
why there is no gateway connection and no second process to deploy.

Every setting is optional and unset means the feature is off. A deployment with no guild
has nothing to post to, and refusing to boot over it would make Discord a dependency of
selling. FMSC_CODE_PEPPER is the opposite case and refuses on purpose, because invite codes
silently stop matching without it.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime

import httpx
import msgspec
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
# Hex, as the developer portal prints it: the Ed25519 key every interaction is signed with.
PUBLIC_KEY = os.environ.get("DISCORD_PUBLIC_KEY", "")
# Where the link in a post points. Same default as DATABASE_URL's — the local dev address.
BASE_URL = os.environ.get("FMSC_BASE_URL", "http://localhost:8000").rstrip("/")

API = "https://discord.com/api/v10"

# What a caller swaps for a fake. One coroutine taking a channel and a string is the whole
# of what the poster needs from a transport; anything larger would be a seam invented for
# the tests rather than the shape the code uses.
Sender = Callable[[str, str], Awaitable[bool]]

# Longer than Discord's own p99 and shorter than anything a request can afford to wait on.
# The rotate route posts inside the request that caused it.
TIMEOUT = 5.0

# A 429 says how long to wait. Trusted, but not past the point where waiting is worse than
# dropping the message: the poster will be back next tick either way.
MAX_BACKOFF = 5.0


async def send(channel_id: str, content: str) -> bool:
    """Post one message to a channel. Returns whether it landed, and raises nothing.

    Best effort on purpose. A guild missing one line is cosmetic — the socket and the API
    are where state lives — while a message retried forever behind a deleted channel is a
    queue that never drains. What is durable is the obligation rather than the delivery:
    the outbox row is written in the transaction that caused it, so a crash before posting
    retries, and only an answer from Discord retires it.

    `allowed_mentions` suppresses every ping, and that is not cosmetic: the content carries
    an auction title, which is a seller's free text, and a lot called "@everyone" would
    otherwise notify an entire guild on command.

    ponytail: a client per call, so there is nothing to close on shutdown. One TLS
    handshake per message, which a channel Discord rate-limits to five messages per five
    seconds will never notice; hold a module-level AsyncClient if that stops being true.
    """
    if not BOT_TOKEN:
        logger.info("discord is not configured; dropped a message for channel %s", channel_id)
        return False
    try:
        async with httpx.AsyncClient(base_url=API, timeout=TIMEOUT) as client:
            for retries_left in (1, 0):
                response = await client.post(
                    f"/channels/{channel_id}/messages",
                    headers={"Authorization": f"Bot {BOT_TOKEN}"},
                    json={"content": content, "allowed_mentions": {"parse": []}},
                )
                if response.status_code != 429 or not retries_left:
                    break
                # Discord says how long to wait, in seconds, and means it: ignoring a 429
                # is how a bot earns a longer ban than the one it is already serving.
                retry_after = float(response.json().get("retry_after", 1.0))
                await asyncio.sleep(min(retry_after, MAX_BACKOFF))
    except httpx.HTTPError as exc:
        # Reachability, not refusal: a timeout or a DNS failure is the deployment's
        # problem and nothing this process does differently can answer it.
        logger.warning("discord unreachable for channel %s: %r", channel_id, exc)
        return False
    if response.is_success:
        return True
    logger.warning(
        "discord refused a message for channel %s: %s %s",
        channel_id,
        response.status_code,
        response.text[:200],
    )
    return False


# What the poster reads. The same shape as relay_pending's: a state column rather than a
# cursor, because the outbox's ids are allocated before commit and a transaction that
# commits late would be skipped by anything reading in id order.
#
# LEFT JOIN, not JOIN: an event for an auction no guild follows still has to be retired,
# or the partial index below grows for the life of the service.
#
# FOR UPDATE OF o, because the nullable side of an outer join cannot be locked — and
# nothing here wants to lock a community's link anyway. SKIP LOCKED so a second instance
# works the rest of the queue instead of waiting behind this one.
_UNPOSTED_SQL = text("""
    SELECT o.id, o.payload, a.title, a.currency, d.channel_id
      FROM outbox o
      JOIN auctions a ON a.id = o.auction_id
      LEFT JOIN community_discord d ON d.community_id = a.community_id
     WHERE o.discord_at IS NULL
     ORDER BY o.auction_id, o.seq
       FOR UPDATE OF o SKIP LOCKED
     LIMIT :limit
    """)

_MARK_POSTED_SQL = text(
    "UPDATE outbox SET discord_at = clock_timestamp() WHERE id IN :ids"
).bindparams(bindparam("ids", expanding=True))

# One tick's worth. Smaller than the relay's hundred: each row is an HTTP request to
# somebody else's service, and a channel is rate-limited to five messages per five seconds
# anyway.
#
# ponytail: a hot auction therefore posts every bid, serially, and falls behind rather than
# coalescing. Collapse a channel's pending bids into the newest one if that ever matters —
# the sequence number in each payload is what makes it safe to drop the older ones.
POST_BATCH = 20


def _when(iso: str) -> str:
    """Discord's own timestamp markup, which each viewer reads in their own timezone.

    The alternative is printing Jakarta wall time at a guild that may not be in Jakarta.
    """
    return f"<t:{int(datetime.fromisoformat(iso).timestamp())}:f>"


def _message(event: dict, title: str, currency: str) -> str | None:
    """One line for one event, or None for an event a guild has nothing to hear about.

    None rather than a raise: during a rolling deploy the queue can hold a payload written
    by a version of the code this one does not know, and a tick that died on it would stop
    closing auctions.
    """
    link = f"{BASE_URL}/auctions/{event['auction_id']}"
    kind = event["type"]
    if kind == "bid":
        return f"**{title}** — new bid {event['amount']} {currency}, ends {_when(event['ends_at'])}. {link}"
    if kind == "closed":
        if event["winner_id"] is None:
            return f"**{title}** closed with no bids."
        return f"**{title}** closed — won at {event['amount']} {currency}."
    if kind == "status" and event["status"] == "open":
        return f"**{title}** is open, ends {_when(event['ends_at'])}. Bid at {link}"
    if kind == "status" and event["status"] == "cancelled":
        return f"**{title}** was withdrawn."
    return None


async def post_pending(
    session_maker: async_sessionmaker[AsyncSession], send: Sender = send, limit: int = POST_BATCH
) -> int:
    """Post whatever the guilds have not been told yet. Returns how many events it retired.

    A second reader of the queue the money path already fills, so what a channel is told
    cannot disagree with what the sockets were told. Retired after the attempt rather than
    before: a crash in between leaves the row for the next tick, and only an answer from
    Discord — including a refusal — retires it.
    """
    async with session_maker() as session:
        rows = (await session.execute(_UNPOSTED_SQL, {"limit": limit})).all()
        for row in rows:
            if row.channel_id is None:
                continue
            message = _message(msgspec.json.decode(row.payload), row.title, row.currency)
            if message is not None:
                await send(row.channel_id, message)
        if rows:
            await session.execute(_MARK_POSTED_SQL, {"ids": [row.id for row in rows]})
        await session.commit()
    return len(rows)
