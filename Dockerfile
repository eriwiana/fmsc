# nixpacks builds from pyproject.toml and resolves dependencies fresh on every deploy, so
# production ran versions no test had ever seen while uv.lock sat in the repo unused. This
# installs from the lock instead, which is the same file CI resolves against.
#
# The tag pins the interpreter to the 3.12 in .python-version. Two places state that
# version; a mismatch is caught by the `python -V` check in the CI image build.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependencies before source: this layer is cached until uv.lock itself changes, so a
# code-only deploy skips the install entirely.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-install-project --no-dev --extra redis

COPY . .
RUN uv sync --locked --no-dev --extra redis

ENV PATH="/app/.venv/bin:$PATH"

# Single instance: migrate then serve in one command, so there is no migration race.
# Split this out (release step + advisory lock) before running more than one instance.
#
# `exec` matters: without it uvicorn runs as a child of sh, SIGTERM stops at sh, and the
# on_shutdown hook that cancels the closer task never runs.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
