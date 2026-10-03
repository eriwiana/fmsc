"""End-to-end paths through the real app: signup, create, subscribe, bid, receive.

Every socket read passes a timeout. Without one, a message the server never sends makes
the test hang instead of fail, which costs a killed run to notice rather than a red line.

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
from datetime import datetime, timedelta, timezone
from queue import Empty

import pytest
from litestar.exceptions import WebSocketDisconnect
from litestar.testing import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.main import (
    SUBSCRIBER_BACKLOG_STRATEGY,
    SUBSCRIBER_MAX_BACKLOG,
    app,
    channels,
)

PG_URL = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/fmsc"
)
if PG_URL.startswith("postgresql://"):
    PG_URL = PG_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

PASSWORD = "correct horse battery staple"
# Long enough for a real handler, short enough that a message that never arrives fails
# the test rather than hanging the run.
READ_TIMEOUT = 5.0


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


def _open_auction(client: TestClient, seller_token: str) -> int:
    response = client.post(
        "/auctions",
        headers={"Authorization": f"Bearer {seller_token}"},
        json={
            "title": "t",
            "starting_bid": "10.00",
            "ends_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_a_socket_receives_a_snapshot_before_any_event(clean_db):
    """M5 criterion 1. A client joining mid-auction currently gets nothing until the next
    bid, so it cannot render a price at all. The snapshot has to arrive before live events
    and has to agree with what the HTTP endpoint would have said."""
    with TestClient(app=app) as client:
        seller = _signup(client, "seller@x.com")
        watcher = _signup(client, "watcher@x.com")
        bidder = _signup(client, "bidder@x.com")
        auction_id = _open_auction(client, seller)
        placed = client.post(
            f"/auctions/{auction_id}/bids",
            headers={"Authorization": f"Bearer {bidder}"},
            json={"amount": "12.00"},
        )
        assert placed.status_code == 201, placed.text

        with client.websocket_connect(f"/ws/auctions/{auction_id}?token={watcher}") as socket:
            snapshot = socket.receive_json(timeout=READ_TIMEOUT)

        current = client.get(f"/auctions/{auction_id}").json()

    assert snapshot["type"] == "snapshot"
    assert snapshot["auction"] == current
    # The number the client resumes from: without it, the first live event's seq could be
    # 1 or 500 and the client has no way to know whether it missed anything.
    assert snapshot["seq"] == 1


def test_a_watcher_receives_a_bid_over_a_real_socket(clean_db):
    with TestClient(app=app) as client:
        seller = _signup(client, "seller@x.com")
        watcher = _signup(client, "watcher@x.com")
        bidder = _signup(client, "bidder@x.com")

        auction_id = _open_auction(client, seller)

        with client.websocket_connect(f"/ws/auctions/{auction_id}?token={watcher}") as socket:
            # The snapshot is the handshake: once it has arrived the subscription is live,
            # so there is no window left to race and no sleep needed. Before the snapshot
            # existed this test had to guess at 0.3s.
            assert socket.receive_json(timeout=READ_TIMEOUT)["type"] == "snapshot"

            placed = client.post(
                f"/auctions/{auction_id}/bids",
                headers={"Authorization": f"Bearer {bidder}"},
                json={"amount": "12.00"},
            )
            assert placed.status_code == 201, placed.text
            event = socket.receive_json(timeout=READ_TIMEOUT)

    assert event["type"] == "bid"
    assert event["auction_id"] == auction_id
    assert event["amount"] == "12.00"
    # The socket and the HTTP reply agree, which is what lets a watcher trust the deadline.
    assert event["ends_at"] == placed.json()["ends_at"]


def test_a_rejected_bid_answers_400_with_the_reason(clean_db):
    """Through the real app, because this is where it broke. place_bid_tx rolls back on
    rejection, which expires current_user, so the handler's own read of current_user.id
    attempted a lazy reload and every rejected bid answered 500 with no reason in it —
    outbid, too low, closed, self-bid, all of them. No unit test could see it: the fixture
    session and the request session expire alike, but only a real request shows the status
    code the bidder is actually handed."""
    with TestClient(app=app) as client:
        seller = _signup(client, "seller@x.com")
        bidder = _signup(client, "bidder@x.com")
        auction_id = _open_auction(client, seller)
        rejected = client.post(
            f"/auctions/{auction_id}/bids",
            headers={"Authorization": f"Bearer {bidder}"},
            json={"amount": "9.99"},
        )

    assert rejected.status_code == 400, rejected.text
    assert rejected.json()["detail"] == "bid must be at least 10.00"


def test_a_retried_bid_replays_the_original_response(clean_db):
    """M4 criterion 1. A client whose connection drops after the bid landed retries it.
    The second request must not place a second bid, and must hand back what the first one
    returned rather than a 400 saying the bidder has been outbid by themselves."""
    with TestClient(app=app) as client:
        seller = _signup(client, "seller@x.com")
        bidder = _signup(client, "bidder@x.com")
        auction_id = _open_auction(client, seller)
        headers = {"Authorization": f"Bearer {bidder}", "Idempotency-Key": "retry-me"}
        body = {"amount": "12.00"}

        first = client.post(f"/auctions/{auction_id}/bids", headers=headers, json=body)
        second = client.post(f"/auctions/{auction_id}/bids", headers=headers, json=body)

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    # Bytes, not parsed JSON: the criterion is that the client is handed exactly what it
    # was handed the first time, key order and number formatting included.
    assert second.content == first.content
    assert first.json()["bid_count"] == 1


def test_the_subscriber_backlog_is_bounded():
    """M5 criterion 3, the memory half. Litestar defaults to no bound and a "backoff"
    strategy, so one consumer that never reads grows the server's memory until the process
    dies. Asserted against literals, not against the constants themselves — comparing a
    constant with itself passes whatever value it holds."""
    assert SUBSCRIBER_MAX_BACKLOG == 64
    assert SUBSCRIBER_BACKLOG_STRATEGY == "dropleft"


def test_a_socket_that_fell_behind_is_dropped(clean_db):
    """M5 criterion 3, the client half. Once events have been dropped from its queue the
    socket cannot be made whole, and staying connected would show a price that silently
    skipped a bid. Dropping it sends the client back through the snapshot, which is the
    one path that restores correct state.

    The gap is published directly rather than by overflowing a 64-deep queue: the server's
    reaction to a hole in the numbering is the behaviour under test, and a real overflow
    produces exactly the same hole.
    """
    with TestClient(app=app) as client:
        seller = _signup(client, "seller@x.com")
        watcher = _signup(client, "watcher@x.com")
        auction_id = _open_auction(client, seller)

        with client.websocket_connect(f"/ws/auctions/{auction_id}?token={watcher}") as socket:
            assert socket.receive_json(timeout=READ_TIMEOUT)["seq"] == 0
            # Events 1 and 2 never arrive; 3 does.
            channels.publish(
                {"type": "bid", "auction_id": auction_id, "seq": 3}, f"auction:{auction_id}"
            )
            with pytest.raises(WebSocketDisconnect) as dropped:
                socket.receive(timeout=READ_TIMEOUT)

    assert dropped.value.code == 4408


def test_a_relayed_duplicate_is_not_sent_twice(clean_db):
    """The outbox delivers at-least-once, so a socket must drop an event it has already
    seen rather than show the same bid twice."""
    with TestClient(app=app) as client:
        seller = _signup(client, "seller@x.com")
        watcher = _signup(client, "watcher@x.com")
        bidder = _signup(client, "bidder@x.com")
        auction_id = _open_auction(client, seller)

        with client.websocket_connect(f"/ws/auctions/{auction_id}?token={watcher}") as socket:
            assert socket.receive_json(timeout=READ_TIMEOUT)["type"] == "snapshot"
            placed = client.post(
                f"/auctions/{auction_id}/bids",
                headers={"Authorization": f"Bearer {bidder}"},
                json={"amount": "12.00"},
            )
            assert placed.status_code == 201, placed.text
            assert socket.receive_json(timeout=READ_TIMEOUT)["seq"] == 1

            # What the relay would send after a crash between publish and mark-sent.
            channels.publish(
                {"type": "bid", "auction_id": auction_id, "seq": 1}, f"auction:{auction_id}"
            )
            with pytest.raises(Empty):
                socket.receive(timeout=1.0)


def test_a_socket_without_a_valid_token_is_closed(clean_db):
    """The query-string token is the only auth on the socket. If it stopped being checked,
    nothing else in the suite would notice."""
    with (
        TestClient(app=app) as client,
        client.websocket_connect("/ws/auctions/1?token=not-a-real-token") as socket,
        pytest.raises(WebSocketDisconnect) as closed,
    ):
        socket.receive(timeout=READ_TIMEOUT)
    assert closed.value.code == 4401
