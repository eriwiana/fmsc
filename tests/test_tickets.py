"""WebSocket tickets: the credential that is safe to put in a URL.

The socket's credential has to travel in the query string, because a browser cannot set
headers on a WebSocket handshake — and a query string is logged by every proxy, kept in
browser history and leaked through Referer. The e2e suite covers accepting and rejecting
one; this covers the housekeeping nothing else would notice.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from litestar.exceptions import NotAuthorizedException

from app.auth import purge_expired_tickets, user_for_ticket
from app.models import User, WsTicket


async def _ticket(maker, email: str, ttl: timedelta) -> str:
    async with maker() as s:
        user = User(email=email, pw_hash="x")
        s.add(user)
        await s.flush()
        ticket = WsTicket(
            token=f"t-{email}",
            user_id=user.id,
            expires_at=datetime.now(timezone.utc) + ttl,
        )
        s.add(ticket)
        await s.commit()
        return ticket.token


async def test_an_expired_ticket_is_purged_and_a_live_one_is_kept(sm):
    """Nothing else deletes them, and an expired ticket is never read again — so without
    this the table grows for the life of the service, one row per socket ever opened."""
    await _ticket(sm, "dead@x.com", timedelta(seconds=-1))
    live = await _ticket(sm, "live@x.com", timedelta(minutes=5))

    async with sm() as s:
        assert await purge_expired_tickets(s) == 1

    async with sm() as s:
        assert await s.scalar(
            WsTicket.__table__.select().where(WsTicket.token == live).exists().select()
        )


async def test_spending_a_ticket_twice_fails_the_second_time(sm):
    """The DELETE is the check, so two sockets arriving together cannot both be let in."""
    token = await _ticket(sm, "once@x.com", timedelta(minutes=5))

    async with sm() as s:
        assert (await user_for_ticket(s, token)).email == "once@x.com"

    async with sm() as s:
        with pytest.raises(NotAuthorizedException):
            await user_for_ticket(s, token)


async def test_an_expired_ticket_is_refused_even_though_it_exists(sm):
    """Spent and then refused: the row is consumed either way, so a ticket that sat in a
    log until it expired cannot be retried once it is found."""
    token = await _ticket(sm, "stale@x.com", timedelta(seconds=-1))

    async with sm() as s:
        with pytest.raises(NotAuthorizedException):
            await user_for_ticket(s, token)

    async with sm() as s:
        assert not await s.scalar(
            WsTicket.__table__.select().where(WsTicket.token == token).exists().select()
        )
