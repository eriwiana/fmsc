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
- `POST /auth/signup`, `POST /auth/login` → `{token}`. A password under **12 characters**
  is refused; repeated failed logins answer **429** with `Retry-After`
- `POST /auth/logout` → **204**, revokes the token it was given. `POST /auth/logout-all`
  (auth) → **204**, revokes every session the user has
- `POST /auctions` (auth, **verified address only**) — `{title, starting_bid, ends_at, currency?, reserve?, status?}`
  (ends_at in the future; a naive value is read as **Asia/Jakarta** wall time; `currency` is
  an ISO 4217 code, **IDR** by default, case-insensitive; `status` is `open` or `draft` and
  nothing else). The response also carries `hard_ends_at`, the latest the auction can
  possibly end once anti-snipe extensions are accounted for
- `GET /auctions` — everything but the drafts. `GET /auctions/{id}` — **404** for a draft
- `GET /auctions/mine` (auth) — every auction the caller sells, drafts included, newest
  first, and the only place the `reserve` comes back
- `PATCH /auctions/{id}` (auth, seller) — `{title?, starting_bid?, reserve?, ends_at?}`;
  **409** once a bid exists or if the auction is closed or cancelled, **400** if the pair
  would leave `reserve` under `starting_bid`
- `POST /auctions/{id}/publish`, `POST /auctions/{id}/cancel` (auth, seller) — **409** when
  the edge is not available, which includes publishing a draft whose deadline has passed.
  Both announce the move to the auction's watchers
- `POST /auctions/{id}/bids` (auth) — `{amount}`; atomic, highest-wins, extends the
  deadline when it lands inside the anti-snipe window. A refused bid answers **400** with
  the guard that refused it in `detail` (too low, under the increment, under the reserve,
  outbid, closed, ended, self-bid). Send `Idempotency-Key: <string>` to make a retry safe
- `POST /ws/tickets` (auth) → `{ticket, expires_at}` — single-use, 30s
- `GET /ws/auctions/{id}?ticket=...` — snapshot, then live `bid`, `status` and `closed`
  events. **4404** for an unknown auction or a draft
- `GET /health`, `GET /schema` (OpenAPI/Swagger UI)

Auth is `Authorization: Bearer <token>`. The WebSocket takes a **ticket** instead, not the
session token: a browser cannot set headers on a handshake, so the credential travels in the
URL — where proxies log it, browsers keep it in history and Referer leaks it. A ticket is
opaque, bound to one user, dead in 30 seconds and spent on first use.

## Sessions and account security
A session token lasts **7 days** and the expiry is part of the authentication query:

```python
.where(Session.token == token, Session.expires_at > func.clock_timestamp())
```

`clock_timestamp()`, not `now()`. Authentication is the first query of a request, so `now()`
would compare against a clock frozen before the request did any work and a session expiring
mid-request would authenticate anyway — the same bug as the bid deadline, one layer lower.

Expiry alone is not enough, because seven days is a long time to wait for a stolen laptop,
so a session can be revoked: logout takes away the token it was presented with, revoke-all
takes away every session for that user. Presenting a token *is* the authorisation to revoke
it, so logout needs no other credential and an already-expired token can still clean up
after itself. An unknown token answers the same as a known one, or the endpoint would be a
way to ask which tokens exist. A *missing* `Authorization` header is refused instead: there
is no token for the 204 to be true of, and a client whose header never got built would
otherwise be told it had signed out.

Expired rows are swept on the closer's tick, indexed by `expires_at` — one row arrives per
login and nothing else deletes them, so the scan would otherwise grow with every account
that has ever signed in.

### Failed logins
Two buckets, counted independently — **5 per account** and **20 per address**, each over
**15 minutes**, answered 429 with `Retry-After`:

| Bucket | Why it is that size |
|---|---|
| account | tight, because it is what protects one specific person |
| address | looser, because an office, a school or a mobile carrier puts many honest people behind one |

Never a combined `(email, ip)` key, which is the part worth understanding: with one, an
attacker holding many addresses gets a fresh per-account budget from each, *and* a scan
across many accounts never spends a per-address budget. Both limits stop being limits. Two
tests are those exact attacks.

The counts live in Postgres rather than in process memory, because the fan-out backend is
optional here and a limit that resets on deploy is not a limit. They are rows, not a
counter, because the limit is a rate and a window cannot be subtracted from an integer. The
lockout is read *before* the password is checked, so a caller who has spent their budget
cannot keep spending the server's CPU on scrypt, and a request refused by the lockout
records nothing — hammering it cannot extend it. A failure is counted whether or not the
address exists, or probing for valid accounts would be cheaper than guessing at a real one.

A login for an unknown address still pays for a scrypt hash, against a throwaway hash
nothing can match. Skipping it would make the response time say whether an account exists
— the same enumeration the counting is there to stop, except it needs one request per
address rather than five, and identical response bodies do not help if one of them arrives
50 ms sooner.

The two counts come from one statement, one indexed subquery per bucket. Counting with
`count(*) FILTER` over the whole window reads the same numbers, but its only predicate is
on `failed_at`, which neither index starts with, so it plans as a sequential scan: measured
at 50,000 attempts inside the window, 9.0 ms that way against 0.5 ms this way. The flood is
exactly when the table is large.

A successful login empties the account's bucket, or four typos would leave someone one typo
from a lockout for the rest of the window. Those rows are also the only record the address
bucket has, so signing in refunds a little address budget too — not a way in, since the
refund needs the password, but it does soften a shared address by every honest user behind
it who mistypes and then succeeds. Keeping the address count intact would need a
`cleared_at` column filtered on in both buckets, which is more machinery than the leak is
worth today.

`X-Forwarded-For` is read from the **right**. Only the rightmost entry was appended by the
proxy in front of the app; everything to its left is whatever the client typed. Reading
from the left would let an attacker have a new address per request and spend the budget of
an address they do not hold.

### Verification
`users.verified_at` is NULL until the address has been proved — signup never sets it, which
is what the acceptance test signs up to prove — and an unverified account cannot create an
auction (**403**) — an auction is a promise to deliver and the seller is
who a buyer has to be able to reach. Bidding stays open to an unverified account on
purpose: the money only moves at settlement, which is where to ask again.

**Nothing sets the column yet.** No mail is sent, so until a verification flow lands it is
set out of band:

```sql
UPDATE users SET verified_at = clock_timestamp() WHERE email = '...';
```

## Realtime
A socket receives a `snapshot` of the auction as its first message, then live events. The
subscription starts before the snapshot is read, so an event landing in between is queued
rather than lost — which also makes the snapshot a handshake: once it arrives, the
subscription is provably live.

Every event carries a per-auction `seq`, bumped inside the same `UPDATE` that accepts the
bid or closes the auction. It is what lets a client tell a dropped event from a quiet
auction, and the server uses it too: a subscriber's queue is capped at 64 with `dropleft`,
so a consumer that falls behind leaves a hole in the numbering and gets closed with **4408**
rather than being shown a price that silently skipped a bid. Reconnecting replays the
snapshot, which is the only way back to correct state.

Events are published through an **outbox** row written in the transaction that caused them,
so a process dying between the commit and the publish cannot lose one — the closer's tick
relays anything left unsent, prunes announced rows after a day, and clears spent tickets.

### Scaling out
`CHANNELS_URL` picks the fan-out backend, and it is the only change needed to run more than
one process. Unset, the backend is in-memory and a bid accepted by one process reaches only
the sockets that process holds.

| `CHANNELS_URL` | Backend |
|---|---|
| unset | in-memory, single instance |
| `redis://…` | Redis pub/sub |
| `postgresql://…` | Postgres `LISTEN`/`NOTIFY`, no extra service |

The Postgres option needs nothing you are not already running. `NOTIFY` caps a payload at
8000 bytes; these events are a few hundred. An unsupported scheme fails at startup rather
than falling back to memory, because a typo there would leave every instance announcing
only to its own sockets — which looks exactly like a quiet auction.

Measured on one machine, 500 sockets on one auction, five bids each. Every backend
delivered 2500 of 2500 events with no watcher failing. Timings are from the bid being sent
to a watcher seeing it:

| Backend | p50 | p95 | max | vs the bidder's own reply | DB connections |
|---|---|---|---|---|---|
| memory | 55 ms | 66 ms | 70 ms | −8 ms | 2 → 6 |
| Redis | 49 ms | 78 ms | 81 ms | −6 ms | 2 → 6 |
| Postgres | 75 ms | 90 ms | 93 ms | **+44 ms** | 3 → 7 |

The last column is the interesting one. On memory and Redis the watchers see the bid
*before* the bidder gets their own 201, because the handler publishes before it serialises
the reply. On `LISTEN`/`NOTIFY` they see it after, because NOTIFY only fires when the
transaction commits and the listening connection then has to be woken. Either is fine for
an auction; neither is obvious until measured.

500 sockets add four connections, not five hundred — the handler releases its session once
the snapshot is sent.

Re-run it with `uv run python scripts/load_sockets.py --sockets 500`. It is not part of the
test suite: 500 sockets on a shared CI runner measures the runner. Delivery is therefore at-least-once, which is safe only because
of `seq`: a duplicate is one the client has already seen, and the socket drops it.

## Lifecycle and ownership
Four states and four edges. Nothing else moves an auction:

```
draft ──publish──> open ──deadline──> closed
  │                 │
  └────cancel───────┴──> cancelled        (cancel: only while bid_count = 0)
```

The table of edges is the only place the machine is written down, and one guarded UPDATE
reads it:

```sql
UPDATE auctions SET status = :to, event_seq = event_seq + 1
 WHERE id = :id AND seller_id = :seller_id
   AND status = ANY(:from_states)
   AND (:to <> 'cancelled' OR bid_count = 0)
```

Legality, ownership and the bid count are in the same `WHERE` on purpose. Two statements
would leave a window for a bid to land between the check and the cancel, and the cancel
would still go through. An empty `:from_states` matches nothing, which is how a state no
edge reaches refuses itself with no special case — and a same-state request is refused for
free, because a state is never listed among the ones that may reach it. That matters: a
no-op would bump `event_seq` and announce a change that did not happen, which is what a
double-clicked publish button sends.

Every refusal answers the same **409**. Not yours, wrong state and already has bids are one
answer, because a caller cannot act on the difference and telling them apart would say
which auctions exist and how many bids are on them. The one exception is a `reserve` that
would end up under the `starting_bid`: that pair is a **400** naming the rule, because the
seller can act on it and the table would otherwise refuse it as a 500.

**Publishing a stale draft is refused.** A draft sat on past its own `ends_at` would open
and then be closed by the very next closer tick — an auction that was never biddable,
announced to everyone watching and immediately resolved. The deadline is a condition in the
same `WHERE` as the edge.

**Every move is announced.** The transition writes an outbox event in its own transaction,
exactly as a bid does:

```json
{"type": "status", "auction_id": 7, "status": "cancelled", "seq": 4, "ends_at": "..."}
```

One event type for all of them, with `status` carrying which way it went, so a state added
later needs no new type. This is not cosmetic: `event_seq` counts events *published* about
an auction and the socket closes a subscriber that sees a number skipped, so a bump with no
event tells a watcher it missed something that never existed. It is also the only way a
watcher hears that a lot was withdrawn — the closer skips a cancelled auction, so no later
event ever arrives.

**Cancel and edit both stop at the first bid.** A bid is somebody else's commitment:
withdrawing the lot after it arrives is the seller taking the option and leaving the bidder
with nothing, and editing the description is a bait-and-switch, since bidders bid on the
description. Raising `starting_bid` after a bid would also retroactively invalidate a bid
that had already been accepted.

A **draft** is the state before anyone has seen the listing, so it is absent from
`GET /auctions`, answers 404 by id, and is refused by the socket with **4404** — 404 and not
403, because that route takes no credentials and a 403 would confirm the id belongs to
something. The socket matters as much as the route: its snapshot carries the whole auction,
and a ticket is something any signed-up account can mint for any id, so a socket that served
drafts would be the back door to a locked front one. Cancelled and closed
auctions stay public: somebody was watching, and a listing that vanishes looks like a bug
or like a sale behind their back.

`scheduled` is deliberately not a state. Nothing can enter it until something opens an
auction at `starts_at`, and a state nothing reaches is scaffolding with a test suite.

### Reserve and increment
Both are conditions in the bid's own `UPDATE`, never read by the handler and then acted on
— that is two statements, and the second reads a row the first could already have changed:

```sql
AND :amount >= COALESCE(reserve, starting_bid)
AND (current_bid IS NULL OR :amount >= current_bid + CASE
        WHEN current_bid < 1000000  THEN 10000
        WHEN current_bid < 10000000 THEN 100000
        ELSE 500000 END)
```

The **reserve** is a hidden floor: a bid under it is refused the way a bid under
`starting_bid` is, and the refusal says the reserve was not met without naming it — a
reserve a bidder can read is a reserve they can bid exactly, which turns the seller's floor
into their ceiling. `COALESCE` means "no reserve" is not a special case, so an absolute
auction keeps `starting_bid` as its only floor. The seller gets the number back from
`/auctions/mine` and from their own writes; nobody else ever does.

The **increment** stops penny wars, which is what bots are for. Bands, not a percentage, so
the step is a round number a bidder can predict, and not a per-auction column, so there is
no knob for a seller to get wrong. The numbers are IDR-shaped because IDR is the only
currency in use; a second currency needs its own table, which is why they live in Python
and the `CASE` is generated from them — two copies would drift, and the copy that drifted
would be the message telling a bidder what to type.

The band comes from the **standing** bid, not the bid being offered. Those agree for almost
every pair of numbers; the case that tells them apart is a leader just under a band boundary
with a bid just over it. The increment also only applies once there is a leader, so an
opening bid may still equal `starting_bid` — charging a step on the first bid would make
every auction start one increment above the price it advertised.

Both numbers and the bands are **chosen, not measured**. The milestone says so.

## Anti-snipe
A bid inside the last **60s** (`SNIPE_WINDOW`) moves the deadline to 60s from the moment it
lands, so a bid placed too late to be answered cannot win on timing alone. `hard_ends_at`,
fixed when the auction is created at `ends_at + 2h` (`MAX_EXTENSION`), caps the total so an
auction cannot be extended forever. Both are global constants, not per-auction settings.

Every `bid` and `closed` event on the WebSocket carries the current `ends_at`, so a
watcher's countdown follows the extension instead of expiring on a deadline that has
already moved.

## Idempotency
A bid sent with an `Idempotency-Key` header (at most 128 characters) stores the response it
returned. A retry on the same key replays that response and places no second bid, so a
client that times out and resends is not told it has outbid itself. Reusing the key with a
**different amount** answers **409** rather than replaying, so a client with a key-reuse bug
finds out instead of believing a bid it never placed. A refused bid consumes nothing: the key
is free to reuse.

## Money
Amounts are `Numeric(18,2)` — never float, and never a cent rounded by a binary
approximation. The old `Numeric(12,2)` stopped just under 10 miliar IDR, which one lot can
exceed; `MAX_BID` is exactly what the column holds, so a bid at the ceiling is a stored bid
and not a 500.

Every amount names its unit. An auction carries a `currency`, a bid carries the currency it
was placed in, and the bid statement copies the auction's own across — the client never
supplies it. The two are kept in step by the table itself:

```
auctions  UNIQUE (id, currency)
bids      FOREIGN KEY (auction_id, currency) REFERENCES auctions (id, currency)
```

That one constraint says both "the auction exists" and "the currencies agree", replacing the
plain `auction_id` key. A bid rewritten to another currency by `update`, a data migration or
psql has no referent and is refused, and an auction cannot be redenominated while bids point
at the pair. Settling an IDR auction against an amount read as USD is an error of four
orders of magnitude in what is owed, so it is not left to the application to remember.

## Guardrails
The invariants the bid path assumes are enforced by the schema, not only by Python —
`update`, a migration and psql all bypass the handlers:

| Constraint | Rule |
|---|---|
| `ck_auctions_ceiling_after_deadline` | `hard_ends_at >= ends_at` |
| `ck_auctions_status_known` | `status IN ('draft', 'open', 'closed', 'cancelled')` |
| `ck_auctions_starting_bid_positive` | `starting_bid > 0` |
| `ck_auctions_reserve_at_least_starting` | `reserve IS NULL OR reserve >= starting_bid` |
| `ck_auctions_current_bid_at_least_starting` | `current_bid IS NULL OR current_bid >= starting_bid` |
| `ck_bids_amount_positive` | `amount > 0` |
| `ck_auctions_currency_is_a_code` | `currency ~ '^[A-Z]{3}$'` |
| `ck_bids_currency_is_a_code` | `currency ~ '^[A-Z]{3}$'` |
| `fk_bids_auction_id_auctions` | `(auction_id, currency)` references `auctions (id, currency)` |

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

## Deferred (see the milestones)
The verification flow itself: sending the mail, the confirmation link, and resending it.
The column and the rule that reads it are in place, so what is missing is the delivery.
With it, a password change and reset endpoint — both of which should revoke every other
session, which is now possible.

A breached-password check is deliberately absent: the useful version is a k-anonymity
lookup against Have I Been Pwned, which puts an outbound call on the signup path and needs
an answer for what to do when it is down.

Then settlement and payments.

A bid-history endpoint for the seller's dashboard: `/auctions/mine` is the list, and the
obvious second endpoint is the bids on one of them, scoped to its seller.

The other reading of a reserve — the bid leads but cannot win, and the auction closes
unsold — is not built. It is how eBay behaves and it needs the close path to handle "bids
but no winner"; it can be had later without moving the column.

Multi-currency is only half done: the schema carries the unit, nothing converts between
units, and no endpoint lets a seller change one. That is deliberate — the column had to land
before live bids, the rate table does not.

Each has a clean seam; the bid stays one atomic SQL statement.
