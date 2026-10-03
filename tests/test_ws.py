"""One end-to-end path through the real app: signup, create, subscribe, bid, receive.

Every other test calls the handlers directly. This is the only one that proves routing,
bearer auth, the websocket's token check and the channels fan-out are wired to each other
— and the only one where a real socket receives a real event.

Synchronous on purpose: litestar's WebSocketTestSession drives the app through a blocking
portal, which deadlocks if there is already a running loop in the test.

Leaving the socket context is part of what these assert. A handler that does not notice
the client has gone never returns, and the test hangs instead of failing — which is how
the leak in auction_ws was found.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone

import pytest
from litestar.exceptions import WebSocketDisconnect
from litestar.testing import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.main import app

PG_URL = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/fmsc"
)
if PG_URL.startswith("postgresql://"):
    PG_URL = PG_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

PASSWORD = "correct horse battery staple"


@pytest.fixture
def clean_db():
    """The async `sm` fixture cannot be used here: this test is synchronous."""

    async def wipe():
        engine = create_async_engine(PG_URL, poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.execute(
                text("TRUNCATE bids, auctions, sessions, users RESTART IDENTITY CASCADE")
            )
        await engine.dispose()

    asyncio.run(wipe())
    yield


def _signup(client: TestClient, email: str) -> str:
    response = client.post("/auth/signup", json={"email": email, "password": PASSWORD})
    assert response.status_code == 201, response.text
    return response.json()["token"]


def test_a_watcher_receives_a_bid_over_a_real_socket(clean_db):
    with TestClient(app=app) as client:
        seller = _signup(client, "seller@x.com")
        watcher = _signup(client, "watcher@x.com")
        bidder = _signup(client, "bidder@x.com")

        created = client.post(
            "/auctions",
            headers={"Authorization": f"Bearer {seller}"},
            json={
                "title": "t",
                "starting_bid": "10.00",
                "ends_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            },
        )
        assert created.status_code == 201, created.text
        auction_id = created.json()["id"]

        with client.websocket_connect(f"/ws/auctions/{auction_id}?token={watcher}") as socket:
            # The handler accepts, then checks the token, then subscribes. Publishing is
            # at-most-once with no backlog, so a bid landing before that subscription is
            # live is simply lost — the gap the README records under Deferred. Give the
            # handler that moment rather than racing it.
            time.sleep(0.3)

            placed = client.post(
                f"/auctions/{auction_id}/bids",
                headers={"Authorization": f"Bearer {bidder}"},
                json={"amount": "12.00"},
            )
            assert placed.status_code == 201, placed.text
            event = socket.receive_json()

    assert event["type"] == "bid"
    assert event["auction_id"] == auction_id
    assert event["amount"] == "12.00"
    # The socket and the HTTP reply agree, which is what lets a watcher trust the deadline.
    assert event["ends_at"] == placed.json()["ends_at"]


def test_a_socket_without_a_valid_token_is_closed(clean_db):
    """The query-string token is the only auth on the socket. If it stopped being checked,
    nothing else in the suite would notice."""
    with (
        TestClient(app=app) as client,
        client.websocket_connect("/ws/auctions/1?token=not-a-real-token") as socket,
        pytest.raises(WebSocketDisconnect) as closed,
    ):
        socket.receive()
    assert closed.value.code == 4401
