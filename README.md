# fmsc — real-time absolute auction

Foundation for a community auction platform. **Absolute auction**: highest bid at the
deadline wins. One service (Litestar + WebSocket) + Postgres. No Redis until you scale past a
single instance.

## Stack
Litestar · advanced-alchemy (SQLAlchemy 2.0 async + Alembic) · asyncpg · Postgres · uvicorn.
Auth is stdlib only (`hashlib.scrypt` + opaque session tokens). 4 direct deps.

## Time
App timezone is **Asia/Jakarta** (`APP_TZ` in `app/auctions.py`). Storage stays UTC
(`timestamptz` + Postgres `now()`); the zone only governs the edges — naive input is read as
Jakarta wall time, responses render `+07:00`. Process runs with `TZ=Asia/Jakarta` for logs.

## API
- `POST /auth/signup`, `POST /auth/login` → `{token}`
- `POST /auctions` (auth) — `{title, starting_bid, ends_at}` (ends_at in the future; a naive
  value is read as **Asia/Jakarta** wall time). The response also carries `hard_ends_at`, the
  latest the auction can possibly end once anti-snipe extensions are accounted for
- `GET /auctions`, `GET /auctions/{id}`
- `POST /auctions/{id}/bids` (auth) — `{amount}`; atomic, highest-wins, extends the
  deadline when it lands inside the anti-snipe window
- `GET /ws/auctions/{id}?token=...` — live bid + close events
- `GET /health`, `GET /schema` (OpenAPI/Swagger UI)

Auth is `Authorization: Bearer <token>` (query `?token=` for the WebSocket).

## Anti-snipe
A bid inside the last **60s** (`SNIPE_WINDOW`) moves the deadline to 60s from the moment it
lands, so a bid placed too late to be answered cannot win on timing alone. `hard_ends_at`,
fixed when the auction is created at `ends_at + 2h` (`MAX_EXTENSION`), caps the total so an
auction cannot be extended forever. Both are global constants, not per-auction settings.

Every `bid` and `closed` event on the WebSocket carries the current `ends_at`, so a
watcher's countdown follows the extension instead of expiring on a deadline that has
already moved.

## Guardrails
The invariants the bid path assumes are enforced by the schema, not only by Python —
`update`, a migration and psql all bypass the handlers:

| Constraint | Rule |
|---|---|
| `ck_auctions_ceiling_after_deadline` | `hard_ends_at >= ends_at` |
| `ck_auctions_status_known` | `status IN ('open', 'closed')` |
| `ck_auctions_starting_bid_positive` | `starting_bid > 0` |
| `ck_auctions_current_bid_at_least_starting` | `current_bid IS NULL OR current_bid >= starting_bid` |
| `ck_bids_amount_positive` | `amount > 0` |

Adding these to a table that already holds a violating row fails the migration, by design:
a bad money row should be looked at, not silently rewritten.

## Local dev
```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
docker run -d --name fmsc-pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=fmsc \
    -p 5432:5432 postgres:16-alpine
export DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/fmsc
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --reload
```

### Tests
```bash
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/fmsc .venv/bin/pytest
```
`tests/test_bid.py` is the critical check: accept/reject rules, ended-auction rejection, and
true concurrent single-winner.

### Manual real-time smoke
Create an auction ending ~30s out, then in two terminals:
```bash
wscat -c "ws://localhost:8000/ws/auctions/1?token=YOUR_TOKEN"
```
POST bids from each; both sockets receive every update and the final `closed` event with the winner.

## Deploy (Railway)
Add a Postgres plugin (injects `DATABASE_URL`). Railway builds the `Dockerfile`, which installs
from `uv.lock` so production runs the versions CI tested. The image migrates then serves;
healthcheck is `/health`.

Build and run it locally exactly as Railway does:

```bash
docker build -t fmsc .
docker run --rm -p 8000:8000 -e PORT=8000 \
    -e DATABASE_URL=postgresql://postgres:postgres@host.docker.internal:5432/fmsc fmsc
```

## Changing the schema
```bash
alembic revision --autogenerate -m "what changed"   # review the generated file
alembic upgrade head
```

## Deferred (see plan)
Redis fan-out (before multi-instance), idempotent retries, payments, reserve prices. Publishing
is at-most-once with no backlog: a process that dies between the commit and the channel publish
closes an auction without telling its watchers, and a client that subscribes a moment late gets
nothing. Both want an outbox plus a snapshot on subscribe.
Each has a clean seam; the bid stays one atomic SQL statement.
