from __future__ import annotations

import asyncio
import contextlib
import os

from advanced_alchemy.config import AsyncSessionConfig
from advanced_alchemy.extensions.litestar import SQLAlchemyAsyncConfig, SQLAlchemyPlugin
from litestar import Litestar, get
from litestar.channels import ChannelsPlugin
from litestar.channels.backends.memory import MemoryChannelsBackend

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
# Memory backend = single instance. Swap to RedisChannelsBackend before scaling out.
channels = ChannelsPlugin(backend=MemoryChannelsBackend(), arbitrary_channels_allowed=True)


@get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


async def _start_closer(app: Litestar) -> None:
    maker = db_config.create_session_maker()
    app.state.closer_task = asyncio.create_task(auctions.run_closer(maker, channels))


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
        auctions.create_auction,
        auctions.list_auctions,
        auctions.get_auction,
        auctions.place_bid,
        auctions.auction_ws,
    ],
    plugins=[SQLAlchemyPlugin(db_config), channels],
    on_startup=[_start_closer],
    on_shutdown=[_stop_closer],
)
