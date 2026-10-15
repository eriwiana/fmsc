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

import httpx

logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
# Hex, as the developer portal prints it: the Ed25519 key every interaction is signed with.
PUBLIC_KEY = os.environ.get("DISCORD_PUBLIC_KEY", "")
# Where the link in a post points. Same default as DATABASE_URL's — the local dev address.
BASE_URL = os.environ.get("FMSC_BASE_URL", "http://localhost:8000").rstrip("/")

API = "https://discord.com/api/v10"

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
