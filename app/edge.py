"""The HTTP edge: what every request passes through on the way in, and every response on
the way out.

None of it is domain logic, and none of it knows what an auction is. It lives in its own
module rather than in app.main because app.main is omitted from coverage — configuration
put there is untestable by construction, and a limit nobody tested is a limit nobody has.
"""

from __future__ import annotations

from litestar.datastructures import MutableScopeHeaders
from litestar.enums import ScopeType
from litestar.middleware.base import AbstractMiddleware
from litestar.middleware.rate_limit import (
    CacheObject,
    DurationUnit,
    RateLimitConfig,
    RateLimitMiddleware,
)
from litestar.types import Message, Receive, Scope, Send

from app.auth import client_ip

# Requests per minute per address. Unset is the default; 0 turns the limit off, which the
# test suite needs — a few hundred requests from one address in seconds is exactly the
# burst this exists to refuse.
RATE_LIMIT_ENV = "FMSC_RATE_LIMIT_PER_MINUTE"

# Generous rather than tight: a bidder watching a lot holds a socket and does not poll, so
# anything near this from one address is a script. Tighten per route later with
# RateLimitConfig's check_throttle_handler; nothing has asked for it.
DEFAULT_RATE_LIMIT: tuple[DurationUnit, int] = ("minute", 120)


def limit_from_env(value: str | None) -> tuple[DurationUnit, int] | None:
    """The budget the environment asks for, or None to run without one.

    Unset means the default, not off: a deployment that forgets the variable gets a limit.
    A value that is not a number raises here, at import, rather than leaving the process
    running with a budget nobody can read — the same stance app.codes takes on its pepper.
    """
    if value is None or not value.strip():
        return DEFAULT_RATE_LIMIT
    per_minute = int(value)
    if per_minute < 0:
        raise ValueError(f"{RATE_LIMIT_ENV} cannot be negative: {value!r}")
    return ("minute", per_minute) if per_minute else None


class RateLimitWithRetryAfter(RateLimitMiddleware):
    """Litestar's limiter, plus the header clients actually read.

    It sets the IETF draft's RateLimit-Reset and nothing else. The login lockout in this
    project already answers Retry-After, every HTTP client knows that one, and a client
    left to guess retries immediately — which is the behaviour a 429 exists to stop.
    """

    def create_response_headers(self, cache_object: CacheObject) -> dict[str, str]:
        headers = super().create_response_headers(cache_object)
        # Never 0: "retry after no time at all" is what a client does anyway when there is
        # no header. Same flooring as login_retry_after, for the same reason.
        headers["Retry-After"] = str(max(int(headers[self.config.rate_limit_reset_header_key]), 1))
        return headers


# Set on every response, which rules out Litestar's own response_headers: those are a
# layer on a route handler, and a 404 reaches no handler. A header set that stops at the
# error paths is a header set with a hole in it, so these go on in a middleware that wraps
# everything — see SecurityHeaders below.
#
# This service answers JSON and nothing else. No page, no form, no script, no cookie — so
# the policy can be the strictest version of each header rather than a compromise with
# something the project serves. If a browser-facing page ever lands here, the CSP is the
# line to revisit, not to delete.
SECURITY_HEADERS = {
    # No content sniffing: a JSON body that something coaxes a browser into reading as HTML
    # is the cheapest cross-site scripting there is.
    "X-Content-Type-Options": "nosniff",
    # Nothing here belongs in a frame, so clickjacking has nothing to aim at. Kept beside
    # the CSP's frame-ancestors because the browsers that ignore one honour the other.
    "X-Frame-Options": "DENY",
    # Referrers leak paths, and the paths here carry auction and community ids.
    "Referrer-Policy": "no-referrer",
    # Nothing may load, frame or be framed. An API that serves no document has no use for a
    # looser default, and 'none' is what makes a stray HTML response inert.
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    # A year, which is the floor every preload list asks for. Harmless over plain http —
    # browsers ignore it there — so it needs no deployment-dependent switch.
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
}


class SecurityHeaders(AbstractMiddleware):
    """Puts SECURITY_HEADERS on every HTTP response, whoever built it.

    Outermost in the stack, so a 404 from the router and a 429 from the rate limiter carry
    them as surely as a 200 from a handler. WebSocket connections are out of scope: there
    is no response for a header to go on, and the handshake is not a document.
    """

    scopes = {ScopeType.HTTP}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableScopeHeaders(message)
                for name, value in SECURITY_HEADERS.items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, with_headers)


def rate_limit_config(rate_limit: tuple[DurationUnit, int]) -> RateLimitConfig:
    """A fixed window per address, over every HTTP route but the health check.

    The identifier is this project's own client_ip, never Litestar's default: the default
    reads the socket peer and ignores X-Forwarded-For, so behind Railway's proxy every
    client on earth would share one budget and the first busy minute would lock everybody
    out. client_ip takes the rightmost entry, for the reason its own docstring gives.

    The health check is excluded because an uptime monitor polling it would otherwise spend
    the budget of whatever address it comes from, and that budget is shared with the
    platform's own checks.

    WebSocket connections are not counted: the middleware is HTTP-scoped, which is correct
    here — a socket is one request, and it is already gated by a single-use ticket.

    ponytail: the store is the in-memory default and the lock is per-process, so N
    instances allow N budgets. Register a RedisStore named "rate_limit" when more than one
    instance runs, which is the same point at which the Dockerfile's migration note applies.
    """
    return RateLimitConfig(
        rate_limit=rate_limit,
        exclude=["^/health$"],
        identifier_for_request=client_ip,
        middleware_class=RateLimitWithRetryAfter,
    )
