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

import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from litestar import Litestar, get
from litestar.exceptions import ClientException
from litestar.testing import TestClient

from app.auctions import IDEMPOTENCY_HEADER, MAX_EXTENSION, bid_increment, place_bid
# The module, not its names: each criterion lands in its own commit, and a missing
# attribute should fail one test rather than stop the file from importing at all.
from app import edge
from app.models import Auction, User
from app.schemas import BidRequest

# Small enough to burn in a test, and a one-second window so the refill is observable
# without the suite sitting still for a minute.
_BURST = ("second", 2)


@get("/ping", sync_to_thread=False)
def ping() -> str:
    return "pong"


def _limited_app(rate_limit=_BURST) -> Litestar:
    """A throwaway app carrying nothing but the budget under test.

    The production app's own limit comes from the environment and the suite turns it off —
    a few hundred requests from one address in seconds is exactly the burst it exists to
    refuse. What matters is that the config this builds is the config main.py installs.
    """
    return Litestar(route_handlers=[ping], middleware=[edge.rate_limit_config(rate_limit).middleware])


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
    """Criterion 1. Two requests are the whole budget, the third is refused, and the window
    turning over gives the budget back — a limit that never refills is an outage."""
    with TestClient(app=_limited_app()) as client:
        assert [client.get("/ping").status_code for _ in range(2)] == [200, 200]

        refused = client.get("/ping")
        assert refused.status_code == 429
        # The number, not just the refusal: a client left to guess retries immediately.
        assert int(refused.headers["Retry-After"]) >= 1

        time.sleep(1.1)
        assert client.get("/ping").status_code == 200


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

    assert f"auction_id={aid}" in rejected and f"user_id={uid}" in rejected
    assert "result=rejected" in rejected
    # A stable token, not the sentence the bidder is shown: a reworded message must not
    # break a log query, which is the whole difference between provable and argued.
    assert "guard=below_increment" in rejected


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
