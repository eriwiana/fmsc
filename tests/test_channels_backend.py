"""Which fan-out backend the process uses, and what picks it.

The memory backend is single-instance by construction: a bid accepted by one process is
announced only to the sockets that process holds. Scaling out needs a backend both
processes can see, and which one is a deployment decision rather than a code change.
"""

from __future__ import annotations

import pytest
from litestar.channels.backends.asyncpg import AsyncPgChannelsBackend
from litestar.channels.backends.memory import MemoryChannelsBackend
from litestar.channels.backends.redis import RedisChannelsPubSubBackend

from app.main import asyncpg_dsn, channels_backend


def test_no_url_means_the_memory_backend():
    """One process, no extra service to run. The default has to stay this, or a single
    instance suddenly needs infrastructure it does not use."""
    assert isinstance(channels_backend(None), MemoryChannelsBackend)
    assert isinstance(channels_backend(""), MemoryChannelsBackend)


def test_a_redis_url_means_the_redis_backend():
    assert isinstance(channels_backend("redis://localhost:6379/0"), RedisChannelsPubSubBackend)
    assert isinstance(channels_backend("rediss://localhost:6379/0"), RedisChannelsPubSubBackend)


def test_a_bare_unix_scheme_is_refused():
    """It used to route to Redis. A unix-socket DSN names no product, so guessing is worse
    than refusing."""
    with pytest.raises(ValueError, match="CHANNELS_URL"):
        channels_backend("unix:///var/run/something.sock")


def test_a_postgres_url_means_the_asyncpg_backend():
    """LISTEN/NOTIFY over the database already required, so scaling out costs no new
    service. Events are a few hundred bytes against NOTIFY's 8000-byte limit."""
    assert isinstance(
        channels_backend("postgresql://postgres:postgres@localhost:5432/fmsc"),
        AsyncPgChannelsBackend,
    )
    assert isinstance(
        channels_backend("postgres://postgres:postgres@localhost:5432/fmsc"),
        AsyncPgChannelsBackend,
    )


def test_an_unknown_scheme_is_refused_at_startup():
    """Loudly, at import, rather than by falling back to memory — a typo in CHANNELS_URL
    would otherwise leave every instance announcing only to its own sockets, which looks
    exactly like a quiet auction."""
    with pytest.raises(ValueError, match="CHANNELS_URL"):
        channels_backend("amqp://localhost")


def test_a_postgres_url_keeps_the_driver_out_of_the_dsn():
    """DATABASE_URL carries +asyncpg for SQLAlchemy; asyncpg itself rejects that, so the
    same URL cannot be handed straight to the channels backend."""
    backend = channels_backend("postgresql+asyncpg://postgres:postgres@localhost:5432/fmsc")
    assert isinstance(backend, AsyncPgChannelsBackend)


def test_every_driver_suffix_is_stripped_not_just_asyncpg():
    """Stripping only +asyncpg let postgresql+psycopg:// through the scheme check and on to
    asyncpg unchanged, so it failed on first connect rather than at startup — which is the
    whole point of refusing an unknown scheme here."""
    plain = "postgresql://postgres:postgres@localhost:5432/fmsc"
    assert asyncpg_dsn(plain) == plain
    assert asyncpg_dsn("postgresql+asyncpg://postgres:postgres@localhost:5432/fmsc") == plain
    assert asyncpg_dsn("postgresql+psycopg://postgres:postgres@localhost:5432/fmsc") == plain
    # Not a prefix match: only the scheme is rewritten.
    assert (
        asyncpg_dsn("postgresql://host/postgresql+asyncpg")
        == "postgresql://host/postgresql+asyncpg"
    )
