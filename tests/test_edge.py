"""The acceptance criteria of M12 — hardening, as far as pytest can assert them.

Three of the five are assertable here: the per-IP budget refuses a burst and then refills,
a bid leaves one log line each way naming the guard that decided it, and the header set is
on every response including the ones exception handlers build.

The other two are claims about CI and about an image rather than about this code — that the
build fails on a dependency with a published advisory, and that the container runs as
nobody with no compiler in it. Their tests are steps in the workflow, and criterion 4's is
a failure actually watched on a scratch branch. Pytest shelling out to Docker would assert
the same thing one layer further from where it matters.
"""

from __future__ import annotations

import importlib
import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from litestar import Litestar, get
from litestar.exceptions import ClientException
from litestar.testing import TestClient

# edge as a module, not its names: each criterion lands in its own commit, and a missing
# attribute should fail one test rather than stop the file from importing at all.
from app import edge
from app.auctions import IDEMPOTENCY_HEADER, MAX_EXTENSION, place_bid
from app.models import Auction, User
from app.schemas import BidRequest

# Small enough to burn in a test, and a one-second window so the refill is observable
# without the suite sitting still for a minute.
_BURST = ("second", 2)


@get("/ping", sync_to_thread=False)
def ping() -> str:
    return "pong"


@get("/health", sync_to_thread=False)
def health() -> str:
    """The real app's health check is excluded from the budget. This stands in for it, at
    the same path, because the exclusion is a path pattern."""
    return "ok"


def _limited_app(rate_limit=_BURST) -> Litestar:
    """A throwaway app carrying nothing but the budget under test.

    The production app's own limit comes from the environment and the suite turns it off —
    a few hundred requests from one address in seconds is exactly the burst it exists to
    refuse. What matters is that the config this builds is the config main.py installs.
    """
    return Litestar(
        route_handlers=[ping, health],
        middleware=[edge.rate_limit_config(rate_limit).middleware],
    )


async def _seed(sm) -> tuple[int, int]:
    """One seller, one bidder, one open auction. Returns the bidder and the auction."""
    async with sm() as s:
        seller = User(email="edge-seller@x.com", pw_hash="x")
        bidder = User(email="edge-bidder@x.com", pw_hash="x")
        s.add_all([seller, bidder])
        await s.flush()
        ends_at = datetime.now(timezone.utc) + timedelta(hours=1)
        auction = Auction(
            title="t",
            seller_id=seller.id,
            currency="IDR",
            starting_bid=Decimal("100000.00"),
            ends_at=ends_at,
            hard_ends_at=ends_at + MAX_EXTENSION,
        )
        s.add(auction)
        await s.commit()
        return bidder.id, auction.id


async def _bid(sm, aid: int, uid: int, amount: Decimal, channels):
    async with sm() as s:
        return await place_bid.fn(
            auction_id=aid,
            data=BidRequest(amount=amount),
            # Only `headers` is read by the handler; litestar's Request is not needed.
            request=SimpleNamespace(headers={IDEMPOTENCY_HEADER: ""}),
            current_user=await s.get(User, uid),
            db_session=s,
            channels=channels,
        )


def test_the_budget_refuses_a_burst_and_then_refills():
    """Criterion 1. A burst spends the budget and is refused, and the window turning over
    gives it back — a limit that never refills is an outage with a nicer name.

    The budget is spent in a loop rather than in a fixed number of requests. The window is
    a real second of wall clock, so a burst can straddle the turn and the third request can
    legitimately land in a fresh budget; what must hold is that a client going fast enough
    is refused, and that waiting out the window clears it.
    """
    with TestClient(app=_limited_app()) as client:
        for _ in range(_BURST[1] * 3):
            refused = client.get("/ping")
            if refused.status_code == 429:
                break
        assert refused.status_code == 429, "a burst well over the budget was never refused"
        # The number, not just the refusal: a client left to guess retries immediately.
        assert int(refused.headers["Retry-After"]) >= 1

        time.sleep(1.1)
        assert client.get("/ping").status_code == 200


# --- criterion 1: the budget in detail -----------------------------------------------------


def test_the_budget_is_on_unless_it_is_turned_off():
    """Unset means the default, not off. A deployment that forgets the variable is limited,
    and the suite running with it off cannot hide a production default that is off too."""
    assert edge.limit_from_env(None) == edge.DEFAULT_RATE_LIMIT
    assert edge.limit_from_env("   ") == edge.DEFAULT_RATE_LIMIT
    assert edge.limit_from_env("60") == ("minute", 60)
    assert edge.limit_from_env("0") is None


def test_the_application_that_ships_installs_the_limit(monkeypatch):
    """The burst above is refused by an app built for the test. This is the one that ships.

    app/main.py is excluded from coverage, so nothing else here notices the middleware being
    dropped from it: deleting that line left all 348 tests green, which a mutation pass
    confirmed. A limiter proven only on a throwaway app is a limiter nobody has.

    Reloaded under a budget because the suite runs with the limit off — see the note in
    conftest for why — so the module as imported has nothing to assert. Reloaded again
    afterwards, back to the suite's own setting; the objects other modules already hold keep
    working either way, since nothing here starts the rebuilt application.
    """
    import app.main

    monkeypatch.setenv(edge.RATE_LIMIT_ENV, "7")
    try:
        shipped = importlib.reload(app.main)
        assert shipped.RATE_LIMIT == ("minute", 7)
        installed = [
            layer
            for layer in shipped.litestar_app.middleware
            if layer.middleware is edge.RateLimitWithRetryAfter
        ]
        assert installed, "the application app.main exports carries no rate limit"
    finally:
        monkeypatch.setenv(edge.RATE_LIMIT_ENV, "0")
        importlib.reload(app.main)


def test_a_budget_that_is_not_a_number_refuses_to_start():
    """At import rather than on the first request: a process serving with a budget nobody
    can read is worse than one that did not start."""
    with pytest.raises(ValueError, match=f"{edge.RATE_LIMIT_ENV} must be a whole number"):
        edge.limit_from_env("plenty")
    with pytest.raises(ValueError, match="cannot be negative"):
        edge.limit_from_env("-1")


def test_the_health_check_is_not_counted_against_the_budget():
    """An uptime monitor polling it would otherwise spend the budget of whatever address it
    arrives from — and on a platform that checks for you, that address is shared."""
    with TestClient(app=_limited_app(("second", 1))) as client:
        assert client.get("/ping").status_code == 200
        assert client.get("/ping").status_code == 429
        assert [client.get("/health").status_code for _ in range(5)] == [200] * 5


def test_each_address_has_its_own_budget():
    """Which is only true because the identifier is this project's client_ip. Litestar's
    default reads the socket peer, and behind a proxy that is one address for everybody —
    so the first busy minute would lock out every client at once."""
    with TestClient(app=_limited_app(("second", 1))) as client:
        assert client.get("/ping", headers={"X-Forwarded-For": "198.51.100.7"}).status_code == 200
        assert client.get("/ping", headers={"X-Forwarded-For": "198.51.100.7"}).status_code == 429
        # A different client, arriving through the same proxy, has spent nothing.
        assert client.get("/ping", headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 200


# --- criterion 2: the bid log lines --------------------------------------------------------


async def test_a_bid_logs_one_line_each_way_naming_the_guard(sm, channels, caplog):
    """Criterion 2. One line per accepted bid and one per rejected bid, each carrying the
    auction, the bidder and — when it was refused — the guard that refused it.

    This is what makes the absence of a double-win provable rather than argued: the
    accepted lines carry the event sequence the bid statement assigned, and two bidders
    cannot hold the same number on one auction.
    """
    uid, aid = await _seed(sm)

    with caplog.at_level(logging.INFO, logger="app.auctions"):
        await _bid(sm, aid, uid, Decimal("100000.00"), channels)
        with pytest.raises(ClientException):
            # A cent over the leader does not clear the increment the band table demands.
            await _bid(sm, aid, uid, Decimal("100000.01"), channels)

    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("bid ")]
    assert len(lines) == 2, lines
    accepted, rejected = lines

    assert f"auction_id={aid}" in accepted and f"user_id={uid}" in accepted
    assert "result=accepted" in accepted
    assert "event_seq=1" in accepted
    # Every amount in this project names its unit, logs included: two auctions priced in
    # different currencies otherwise read alike in one query.
    assert "currency=IDR" in accepted

    assert f"auction_id={aid}" in rejected and f"user_id={uid}" in rejected
    assert "result=rejected" in rejected
    # A stable token, not the sentence the bidder is shown: a reworded message must not
    # break a log query, which is the whole difference between provable and argued.
    assert "guard=below_increment" in rejected


# --- criterion 3: the header set -----------------------------------------------------------


def test_the_security_headers_are_on_every_response():
    """Criterion 3. Asserted against the production app, and on a refusal as well as a
    200 — an error response is exactly where a header set tends to go missing, because it
    is built by an exception handler rather than by a route."""
    from app.main import app as production

    with TestClient(app=production) as client:
        ok = client.get("/health")
        missing = client.get("/no-such-route")
    assert (ok.status_code, missing.status_code) == (200, 404)

    for name, value in edge.SECURITY_HEADERS.items():
        assert ok.headers[name] == value
        assert missing.headers[name] == value, f"{name} missing from a 404"
