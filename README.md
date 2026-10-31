# fmsc — real-time absolute auction

Foundation for a community auction platform. **Absolute auction**: the highest bid at the
deadline wins. One service (Litestar + WebSocket) and Postgres; no Redis until you run more
than one instance.

One rule shapes the whole codebase: **a decision belongs in the statement that acts on it.**
Ownership, legality, deadlines, reserves, increments, membership and grace periods are all
conditions in the `UPDATE` that does the work, never a Python `if` before it. Two statements
leave a window, and in an auction that window is somebody else's money.

The long form of every decision below — and why the alternatives were rejected — is in
[docs/DESIGN.md](docs/DESIGN.md).

## Stack
Litestar · advanced-alchemy (SQLAlchemy 2.0 async + Alembic) · asyncpg · Postgres · uvicorn.
Auth is stdlib only (`hashlib.scrypt` + opaque tokens). Four direct dependencies, managed
with `uv`.

## Time
App timezone is **Asia/Jakarta** (`APP_TZ`). Storage stays UTC (`timestamptz`); the zone
governs the edges only — naive input is read as Jakarta wall time, responses render `+07:00`.
Comparisons use `clock_timestamp()`, not `now()`, so a value frozen at transaction start
cannot authenticate an expired session or accept a late bid.

## API
`Authorization: Bearer <token>`, except the WebSocket, which takes a single-use 30-second
**ticket** — a browser cannot set headers on a handshake, and a URL is logged, kept in
history and leaked by Referer.

| Route | Notes |
|---|---|
| `POST /auth/signup` · `/auth/login` → `{token}` | 12-character minimum; **429** with `Retry-After` once a bucket is spent |
| `POST /auth/logout` · `/auth/logout-all` | **204**; revokes one session or every one |
| `POST /auctions` | Seller must have a **verified address**. `status` is `open` or `draft`; `community_id` gates bidding |
| `GET /auctions` · `/auctions/{id}` | Drafts excluded, **404** by id |
| `GET /auctions/mine` | Drafts included, and the only place `reserve` comes back |
| `PATCH /auctions/{id}` | Seller only; **409** once a bid exists |
| `POST /auctions/{id}/publish` · `/cancel` | Seller only; cancel only while `bid_count = 0` |
| `POST /auctions/{id}/bids` | Atomic, highest-wins, extends inside the anti-snipe window. **400** names the guard that refused it. `Idempotency-Key` makes a retry safe |
| `POST /auctions/{id}/settle` | Either party marks their own side; idempotent; **409** past the grace period |
| `POST /auctions/{id}/second-chance` | Seller offers the runner-up their own last bid; the auction stays **closed** |
| `POST /ws/tickets` → `{ticket, expires_at}` | Single-use, 30s |
| `GET /ws/auctions/{id}?ticket=…` | Snapshot, then `bid`, `status`, `closed` events |
| `POST /communities` · `/{id}/codes` · `/codes/rotate` | Owner mints an invite code, returned **once** |
| `POST /communities/{id}/discord` | Attaches a guild channel; one community per guild |
| `POST /communities/codes/redeem` | One fixed **404** for every refusal; **429** once the attempt limit is spent |
| `POST /communities/{id}/members/{user_id}/kick` · `/members/remove-all` | Owner only |
| `POST /discord/interactions` | Discord's own, authenticated by Ed25519 rather than a session |
| `GET /health` · `GET /schema` | Health check and OpenAPI/Swagger UI |

Full request and response shapes are in `/schema`; the refusal codes each route can answer
are asserted in `tests/`.

## How it works

**The money path is one statement.** A bid is a single guarded `UPDATE`: amount above the
reserve and the increment, auction open, deadline not passed, bidder not the seller, member
of the community if there is one. Nothing is read and then acted on, so two bids racing
cannot both win. The reserve is a hidden floor and the refusal never names it — a reserve a
bidder can read is a reserve they can bid exactly. The increment is banded (10k below 1m,
100k below 10m, 500k above) and comes from the standing bid, not the one being offered.

**Anti-snipe.** A bid inside the last **60s** moves the deadline 60s out, so a bid placed too
late to be answered cannot win on timing alone. `hard_ends_at` caps the total at **+2h** from
creation. Every `bid` and `closed` event carries the current `ends_at`, so a watcher's
countdown follows the extension.

**Realtime.** A socket gets a `snapshot` first, then live events. The subscription opens
before the snapshot is read, so nothing lands in the gap, and the snapshot doubles as proof
that the subscription is live. Every event carries a per-auction `seq`, bumped inside the
same `UPDATE` that caused it — a client can tell a dropped event from a quiet auction, and a
subscriber more than **64** events behind is closed with **4408** rather than shown a price
that skipped a bid. Reconnecting replays the snapshot.

**Nothing is announced without being recorded.** Events are written to an **outbox** row in
the transaction that caused them. A process dying between commit and publish loses nothing:
the closer's tick relays whatever is unsent, prunes announced rows after a day, and sweeps
spent tickets and expired sessions. Delivery is therefore at-least-once, which is safe only
because of `seq`.

**Scaling out** is one variable. `CHANNELS_URL` unset is in-memory and single-instance;
`redis://…` is pub/sub; `postgresql://…` is `LISTEN`/`NOTIFY` and needs no new service. An
unsupported scheme fails at startup rather than falling back to memory, because that typo
looks exactly like a quiet auction. Measured at 500 sockets on one auction: all three
backends delivered 2500 of 2500 events, p95 66–90 ms, and 500 sockets cost four extra
database connections rather than five hundred. Re-run with
`uv run python scripts/load_sockets.py --sockets 500`.

**Lifecycle.** Four states, four edges, one guarded `UPDATE`: `draft → open → closed`, and
`cancelled` from either, only while `bid_count = 0`. Legality, ownership and the bid count sit
in the same `WHERE`, so a bid cannot land between a check and a cancel. Every refusal is the
same **409** — not yours, wrong state and already has bids are one answer, because a caller
cannot act on the difference and telling them apart would reveal which auctions exist. A
same-state request refuses itself for free, which is what a double-clicked publish sends.

**Communities gate bidding, not viewing**, with one more condition in the statement that was
already accepting the bid. Owner rights come from `communities.owner_id`, never from a
membership row, or remove-all would lock an owner out of their own community. A kick is
measured against the auction's start, so somebody entitled to bid when a lot opened keeps
that right. Remove-all bumps a generation instead of deleting rows, so the history of who was
in a community survives it.

**Invite codes** are 13 Crockford Base32 characters (65 bits of CSPRNG) plus a check symbol,
shown as `FMSC-XXXX-XXXX-XXXX-XX`. Crockford drops I, L, O and U, and a typo in the check
symbol never reaches the database — no audit row, no rate-limiter charge, because a mistype
off a screenshot is not an attack. Only `HMAC-SHA256(code, FMSC_CODE_PEPPER)` is stored, so a
leaked backup hands nobody a working code; **the app refuses to start without the pepper**,
since a default is no pepper and a per-boot random one kills every live code on deploy.
Redemption is counted by the statement that claims it, and every refusal is the same 404.

**Discord** posts a community's events to one channel, as a second reader of the same outbox
the money path writes. Slash commands (`/fmsc invite`, `/fmsc rotate`) are authorised by a
guild permission and verified by Ed25519 over the exact bytes received; a bad or missing
signature is **401**. No bot token means nothing is sent and the drop is logged — the feature
is off, the app still starts.

**Settlement is a record, not a transfer.** Money never touches fmsc, so there is no escrow
and no payment licensing — and no leverage over a winner who never pays, which makes the
record the product. The closing statement itself writes the winner, the runner-up and
`closed_at`; a later query could read a different second place. Each side marks its own
timestamp, "settled" is both of them, and the **7-day** grace period lives in the `WHERE`
clause. A second-chance offer moves who owes the money without reopening the lot. Reputation
is recomputed from those rows on every call — settled and unsettled, as seller and as buyer —
never stored, because a stored total is one write from disagreeing with the rows it
summarises.

**Money is `Numeric(18,2)`** and every amount names its unit. A bid carries the currency the
auction carries, copied across by the statement rather than supplied by the client, and the
table enforces the pair: `bids (auction_id, currency)` references `auctions (id, currency)`.
One constraint says both "the auction exists" and "the currencies agree".

**Sessions and limits.** A token lasts **7 days**, with the expiry inside the authentication
query. Logout revokes the token it was given; revoke-all takes every session. Failed logins
are counted in two independent buckets — **5 per account**, **20 per address**, over 15
minutes — and every HTTP route is limited to **120 requests a minute per address** by
default. The address is the rightmost `X-Forwarded-For` entry, and only through a private or
loopback hop: reached without a proxy the header is the caller's to choose, which would both
dodge every budget and let one client spend another's.

**Hardening.** Five security headers on every response, including the ones no route built.
One logfmt line per bid, accepted or rejected, carrying the auction, the bidder, the guard
and the `event_seq` that makes a double-win disprovable from logs. CI fails on a dependency
with a published advisory, auditing the exported lock rather than what `pyproject.toml`
permits. The container runs as uid 10001 with no compiler and no package manager.

## Guardrails
Every invariant the bid path assumes is a database constraint, not only a Python check:
`update`, a migration and psql all bypass handlers. Twenty-odd of them live in
`app/models.py` — the deadline ceiling, the known statuses, positive amounts, a reserve at
least the starting bid, one live code per community, one live membership per generation, the
currency pair above. Adding one to a table that already holds a violating row fails the
migration by design: a bad money row should be looked at, not silently rewritten.

## Local dev
```bash
uv sync --all-extras
uv run pre-commit install                 # ruff on commit, the suite on push
docker run -d --name fmsc-pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=fmsc \
    -p 5432:5432 postgres:16-alpine
export DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/fmsc
export FMSC_CODE_PEPPER=dev-pepper-not-a-secret-0123456789abcdef
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

The suite needs the same `DATABASE_URL` and a real Postgres — the money path is conditional
SQL and MVCC behaviour, neither of which sqlite reproduces:

```bash
uv run pytest                 # 352 tests, coverage floor 97%
uv run ruff check . && uv run ruff format --check .
```

`tests/test_bid.py` is the critical one: accept and reject rules, ended auctions, and true
concurrent single-winner. One pinned ruff serves the hook, CI and your terminal.

## Changing the schema
```bash
uv run alembic revision --autogenerate -m "what changed"   # review it: check constraints are hand-written
uv run alembic upgrade head
```

## Deploy
**Not defined yet.** Nothing is deployed and the platform is not chosen; what exists is an
image any of them can run, and this section gets written when one is picked.

What is settled: the `Dockerfile` installs from `uv.lock` so production runs the versions CI
tested, migrates and then serves in one command, answers a healthcheck on `/health`, and runs
as uid 10001 with no toolchain. A `DATABASE_URL` arriving as `postgresql://` is rewritten to
`+asyncpg` at startup, which is the shape managed Postgres hands out. **Single instance by
construction** — migrate-and-serve races itself with two, so a release step and an advisory
lock come before a second one, along with a `CHANNELS_URL` backend and a shared store for the
rate limit.

Whatever the target, these are the variables:

| Variable | Default | What it does |
|---|---|---|
| `DATABASE_URL` | local Postgres | Connection string; `postgresql://` is accepted |
| `FMSC_CODE_PEPPER` | **required**, ≥32 chars | Peppers invite codes. No value, no start |
| `CHANNELS_URL` | in-memory | Fan-out backend for more than one instance |
| `FMSC_RATE_LIMIT_PER_MINUTE` | 120 | Per-address budget. `0` turns it off |
| `FMSC_TRUST_PROXY_HEADERS` | off | Believe `X-Forwarded-For` from a public hop |
| `FMSC_BASE_URL` | `http://localhost:8000` | Where a Discord post's link points |
| `DISCORD_BOT_TOKEN` · `DISCORD_PUBLIC_KEY` · `DISCORD_APPLICATION_ID` | unset | Posting, signature checks, command registration |

Build and run the image the way a platform would:

```bash
docker build -t fmsc . && docker run --rm -p 8000:8000 -e PORT=8000 \
    -e FMSC_CODE_PEPPER=local-pepper-not-a-secret-0123456789 \
    -e DATABASE_URL=postgresql://postgres:postgres@host.docker.internal:5432/fmsc fmsc
```

## Deferred
The email verification flow (the column and the rule exist; the delivery does not), and with
it password change and reset. A breached-password check, which needs an answer for what to do
when the lookup is down. **Payments and custody** — settlement records who owed what and when
the window closed, but the money moves off-platform, and what gates any escrow model is Bank
Indonesia's licensing rather than code. Disputes and arbitration, which custody would need
next. Notifications for settlement events. Discord account linking and DMs. The donation
link, which stays a third-party URL so that no amount, webhook or ledger ever enters this
codebase; the provider is unchosen because a local-only one reaches fewer regions than the
auctions do.

## On the use of AI assistance
This project was built with Claude (Anthropic) as a working assistant. Every milestone began
as acceptance criteria and a decision record written before any code, and each landed as one
pull request whose first commit is the failing test. Claude did most of the typing — schema,
SQL, tests, migrations, docs. I set the scope, made the calls the criteria did not settle,
reviewed the diffs and merged.

Nothing here is unreviewed output: every branch was checked at each commit, against a schema
rebuilt from zero, and against a mutation pass that breaks a guard on purpose to see whether
a named test notices. Commits and pull requests carry no AI trailers — authorship is a
statement about an accountable person — so the acknowledgement lives here instead.
