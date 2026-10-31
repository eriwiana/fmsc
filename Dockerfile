# nixpacks builds from pyproject.toml and resolves dependencies fresh on every deploy, so
# production ran versions no test had ever seen while uv.lock sat in the repo unused. This
# installs from the lock instead, which is the same file CI resolves against.
#
# Two stages, so what ships is the virtualenv and nothing that built it: no uv, no package
# cache, no toolchain. A compiler in a running container is a compiler available to whoever
# gets a shell in it, and it is dead weight in an image whose dependencies are all wheels.
#
# The tag pins the interpreter to the 3.12 in .python-version, and the final stage names it
# a second time. Three places state that version; a mismatch is caught by the `python -V`
# check in the CI image build.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependencies before source: this layer is cached until uv.lock itself changes, so a
# code-only deploy skips the install entirely.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-install-project --no-dev --extra redis

COPY . .
RUN uv sync --locked --no-dev --extra redis


FROM python:3.12-slim-bookworm

WORKDIR /app

# A system account with no home, no shell worth having and no login. The uid is fixed so a
# volume mounted into this image has one owner to grant, rather than whatever the next
# rebuild happened to allocate.
RUN useradd --system --no-create-home --shell /usr/sbin/nologin --uid 10001 fmsc

# Owned by root and readable by everyone: nothing here is written at runtime, and an
# application that cannot overwrite its own code is one fewer thing to go wrong.
COPY --from=build /app /app

ENV PATH="/app/.venv/bin:$PATH"
# The bytecode is already compiled into the image. Without this, Python retries writing
# .pyc files it cannot write on every import — harmless, and noise in a stack trace.
ENV PYTHONDONTWRITEBYTECODE=1

USER fmsc

# Single instance: migrate then serve in one command, so there is no migration race.
# Split this out (release step + advisory lock) before running more than one instance.
#
# `exec` matters: without it uvicorn runs as a child of sh, SIGTERM stops at sh, and the
# on_shutdown hook that cancels the closer task never runs.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
