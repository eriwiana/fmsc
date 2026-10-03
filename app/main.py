from __future__ import annotations

import asyncio
import contextlib
import os
import re

from advanced_alchemy.config import AsyncSessionConfig
from advanced_alchemy.extensions.litestar import SQLAlchemyAsyncConfig, SQLAlchemyPlugin
from litestar import Litestar, get
from litestar.channels import ChannelsPlugin
from litestar.channels.backends.asyncpg import AsyncPgChannelsBackend
from litestar.channels.backends.base import ChannelsBackend
from litestar.channels.backends.memory import MemoryChannelsBackend
from litestar.channels.backends.redis import RedisChannelsPubSubBackend

from app import auctions, auth

# Railway injects DATABASE_URL as postgresql://; SQLAlchemy async needs +asyncpg.
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/fmsc"
)
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

# expire_on_commit=False: handlers read ORM attributes after committing (e.g. the post-bid
# auction). Without this, the async session would try a sync lazy-load and raise MissingGreenlet.
db_config = SQLAlchemyAsyncConfig(
    connection_string=DATABASE_URL,
    session_config=AsyncSessionConfig(expire_on_commit=False),
)
# Litestar's default is no bound and a "backoff" strategy, which means one consumer that
# never reads grows this process's memory until it dies. Bounded and dropleft instead: the
# queue is capped, the oldest events go first, and the hole that leaves in the per-auction
# sequence is what tells auction_ws to drop the socket. The client then reconnects and the
# snapshot puts it back on correct state, which is the only way back from a dropped event.
SUBSCRIBER_MAX_BACKLOG = 64
SUBSCRIBER_BACKLOG_STRATEGY = "dropleft"

# Which fan-out backend to use is a deployment decision, not a code change. Unset means
# one process, which needs no extra service; a URL means several processes that have to see
# each other's events.
CHANNELS_URL = os.environ.get("CHANNELS_URL")


# The Redis client is ours to close; the plugin closes the backend, not a connection pool
# we handed it. Irrelevant to a process that runs until SIGTERM, a leak in anything that
# builds several.
_channels_client = None


def asyncpg_dsn(url: str) -> str:
    """Strip SQLAlchemy's +driver suffix, whichever driver it names.

    asyncpg rejects it, and DATABASE_URL — which carries one — is the obvious thing to
    paste into CHANNELS_URL. Stripping only +asyncpg let postgresql+psycopg:// past the
    check below and fail on first connect instead of at startup.
    """
    return re.sub(r"^postgresql\+\w+://", "postgresql://", url)


def channels_backend(url: str | None) -> ChannelsBackend:
    """Memory when there is nowhere to fan out to, otherwise whatever the URL names."""
    global _channels_client
    if not url:
        # Single instance by construction: a bid accepted here is announced only to the
        # sockets this process holds.
        return MemoryChannelsBackend()
    if url.startswith(("redis://", "rediss://")):
        from redis.asyncio import Redis

        _channels_client = Redis.from_url(url)
        return RedisChannelsPubSubBackend(redis=_channels_client)
    if url.startswith(("postgres://", "postgresql://", "postgresql+")):
        # LISTEN/NOTIFY over the database already required, so scaling out costs no new
        # service. Events are a few hundred bytes against NOTIFY's 8000-byte limit.
        return AsyncPgChannelsBackend(dsn=asyncpg_dsn(url))
    # Loudly, rather than falling back to memory: a typo here would leave every instance
    # announcing only to its own sockets, which looks exactly like a quiet auction.
    raise ValueError(f"CHANNELS_URL scheme not supported: {url.split('://', 1)[0]}://")


channels = ChannelsPlugin(
    backend=channels_backend(CHANNELS_URL),
    arbitrary_channels_allowed=True,
    subscriber_max_backlog=SUBSCRIBER_MAX_BACKLOG,
    subscriber_backlog_strategy=SUBSCRIBER_BACKLOG_STRATEGY,
)


@get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


async def _start_closer(app: Litestar) -> None:
    maker = db_config.create_session_maker()
    app.state.closer_task = asyncio.create_task(auctions.run_closer(maker, channels))


async def _close_channels_client(app: Litestar) -> None:
    if _channels_client is not None:
        await _channels_client.aclose()


async def _stop_closer(app: Litestar) -> None:
    task = getattr(app.state, "closer_task", None)
    if task:
        task.cancel()
        # Awaited, not just cancelled. cancel() only requests it: the task may be inside a
        # query, and letting the loop tear down underneath it leaves the connection half
        # closed — which hung the suite intermittently once the tick grew from one
        # statement to three. Suppressing CancelledError here is not the mistake M2 made;
        # that was run_closer swallowing its own cancellation, which made it unstoppable.
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = Litestar(
    debug=os.environ.get("DEBUG") == "1",
    route_handlers=[
        health,
        auth.signup,
        auth.login,
        auth.issue_ws_ticket,
        auctions.create_auction,
        auctions.list_auctions,
        auctions.get_auction,
        auctions.place_bid,
        auctions.auction_ws,
    ],
    plugins=[SQLAlchemyPlugin(db_config), channels],
    on_startup=[_start_closer],
    on_shutdown=[_stop_closer, _close_channels_client],
)
