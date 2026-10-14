from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from advanced_alchemy.base import BigIntAuditBase, BigIntBase
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

# Money is Numeric(18,2), never float. Numeric(12,2) stopped just under 10 miliar IDR,
# which a single lot can exceed. Postgres stores numeric as variable-length digits, so the
# wider type costs nothing per row until the values themselves are larger.
Money = Numeric(18, 2)

# ISO 4217, stored as the three-letter code rather than an enum, so a second currency
# needs no migration. The format is checked on both tables, because a lowercase 'idr'
# reads as a currency to a human while matching nothing to the foreign key below.
Currency = String(3)
CURRENCY_IS_A_CODE = "currency ~ '^[A-Z]{3}$'"


class User(BigIntAuditBase):
    __tablename__ = "users"
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    pw_hash: Mapped[str] = mapped_column(String(255))
    # NULL until the address has been proved to belong to whoever signed up. Nothing in
    # this milestone sets it: no mail is sent yet, so it is set out of band until the
    # verification flow lands. An auction is a promise to deliver something, and a seller
    # nobody can reach is how a fake listing happens.
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Session(BigIntBase):
    """An HTTP session token. Expires, and can be revoked one at a time or all at once.

    A token with no expiry is a password that cannot be changed: one copied out of a log,
    a backup or a browser on a shared laptop is a permanent key to the account, and the
    only remedy is to delete the user.
    """

    __tablename__ = "sessions"
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    # Judged by Postgres against clock_timestamp(), never compared in Python. See
    # user_for_token. Indexed for the sweeper, which is the only thing that reads sessions
    # by expiry rather than by token: one row arrives per login and the sweep runs every
    # tick, so an unindexed scan grows with every account that has ever signed in.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class WsTicket(BigIntBase):
    """A single-use, short-lived credential for opening a WebSocket.

    A browser cannot set headers on a WebSocket handshake, so the credential has to travel
    in the URL — where it lands in access logs, browser history and Referer headers. A
    session token there is a long-lived secret in all three. This one is spent on first use
    and dead within seconds.
    """

    __tablename__ = "ws_tickets"
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LoginAttempt(BigIntBase):
    """One failed login. Successful ones are not recorded; this table is only a counter.

    Rows, not a counter column, because the limit is a rate: a count has to be of the
    attempts inside a window, and a window cannot be subtracted from a single integer.
    Postgres rather than memory so the limit holds across processes — the fan-out backend
    is optional in this project, and a rate limit that resets on deploy is not one.
    """

    __tablename__ = "login_attempts"
    # The email as presented, not a user id: an attacker guessing at an address that does
    # not exist has to be counted too, or probing for valid accounts is free.
    email: Mapped[str] = mapped_column(String(255))
    ip: Mapped[str] = mapped_column(String(45))  # fits an IPv6 address with a v4 suffix
    failed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # One index per bucket. The two counts are deliberately independent: a combined
        # (email, ip) key would hand every address its own budget per account, which is
        # the hole the milestone's criterion names.
        Index("ix_login_attempts_email_failed_at", "email", "failed_at"),
        Index("ix_login_attempts_ip_failed_at", "ip", "failed_at"),
    )


class Community(BigIntAuditBase):
    """A group whose auctions only its members may bid on.

    Owner rights come from owner_id and never from a membership row. Deriving them from
    a membership would mean remove-all locks the owner out of their own community, which
    is the one action they can take to recover from a leaked code.
    """

    __tablename__ = "communities"
    name: Mapped[str] = mapped_column(String(100))
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    # Bumped by remove-all instead of deleting membership rows, so the audit trail
    # survives the action that ends every membership. A membership counts only while its
    # generation matches this one.
    membership_generation: Mapped[int] = mapped_column(default=0)

    __table_args__ = (
        CheckConstraint("membership_generation >= 0", name="generation_not_negative"),
    )


class CommunityMembership(BigIntAuditBase):
    """One person's membership of one community, in one generation.

    Rows are never deleted. A kick sets revoked_at, a remove-all bumps the community's
    generation and leaves every row where it is, so "who was in this community when that
    lot sold" stays answerable.
    """

    __tablename__ = "community_memberships"
    community_id: Mapped[int] = mapped_column(ForeignKey("communities.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    # The community's generation at the time this membership was granted. Compared against
    # the community's current one by the bid statement.
    generation: Mapped[int]
    # NULL while the membership stands. A kick sets it, and the bid guard compares it with
    # the auction's start so a kick cannot reach back into an auction already open.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # One *live* membership per person per generation. A second live row would be a
        # duplicate join, and the bid guard's EXISTS would not notice.
        #
        # Partial, on revoked_at IS NULL, because a revoked row must not block a rejoin:
        # a kick leaves the row at the current generation, so a plain constraint makes a
        # mistaken kick permanent — the only remedy being remove-all, which ends everyone
        # else's membership too. The revoked rows stay as the record of the kick.
        Index(
            "uq_memberships_community_user_generation",
            "community_id",
            "user_id",
            "generation",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
        # What the bid guard's EXISTS reads: a membership for this community and user.
        Index("ix_memberships_community_user", "community_id", "user_id"),
    )


class Auction(BigIntAuditBase):
    __tablename__ = "auctions"
    title: Mapped[str] = mapped_column(String(200))
    # Indexed because the seller's dashboard filters on it, and Postgres does not index a
    # foreign key for you — without this, reading one seller's listings scans every auction
    # on the service.
    seller_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    # NULL means a public auction, which is every auction that existed before communities
    # did. Not NULL gates bidding on membership — viewing stays public either way.
    community_id: Mapped[int | None] = mapped_column(
        ForeignKey("communities.id"), nullable=True, index=True
    )
    starting_bid: Mapped[Decimal] = mapped_column(Money)
    # NULL current_bid = no bids yet, so the opening bid may equal starting_bid.
    current_bid: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    # What every amount on this auction is denominated in. Explicit even though one
    # currency is in use: an amount without its unit is not a price, and the cost of
    # adding the column later is a money migration over live bids.
    currency: Mapped[str] = mapped_column(Currency)
    # The price below which the seller will not sell, and nobody else is told what it is.
    # NULL means there is none, which is what an absolute auction is: starting_bid is then
    # the only floor. Evaluated inside the bid statement, never in the handler.
    reserve: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    current_winner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    bid_count: Mapped[int] = mapped_column(default=0)
    # draft | open | closed | cancelled. A draft is unannounced, cancelled is withdrawn
    # before anybody committed. The legal edges between them live in app.auctions.
    status: Mapped[str] = mapped_column(String(10), default="open")
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # The latest ends_at may ever be extended to. Fixed when the auction is created:
    # without a ceiling, two bidders trading bids inside the window keep it open forever.
    hard_ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Counts events published about this auction. Bumped inside the same UPDATE that
    # accepts a bid or closes the auction, so the number cannot be handed out twice or
    # skipped — a client that sees 1 then 3 knows it missed one, which is the only way to
    # tell a dropped event from a quiet auction.
    event_seq: Mapped[int] = mapped_column(default=0)

    # Guardrails, not belt-and-braces: `update`, a data migration and psql all bypass the
    # Python checks in create_auction and _BID_SQL. A bid is a money path, so the table
    # refuses a bad row rather than trusting every writer to be careful.
    __table_args__ = (
        Index("ix_auctions_status_ends_at", "status", "ends_at"),
        # The bid clamps ends_at to hard_ends_at. A ceiling before the deadline is the one
        # shape where that clamp would shorten an auction instead of extending it. Equal is
        # allowed: an auction that may not be extended at all is legitimate.
        CheckConstraint("hard_ends_at >= ends_at", name="ceiling_after_deadline"),
        # Both the bid guard and the closer read status. A typo'd value makes an auction
        # unbiddable and uncloseable at once, and nothing would report it.
        CheckConstraint("status IN ('draft', 'open', 'closed', 'cancelled')", name="status_known"),
        CheckConstraint("starting_bid > 0", name="starting_bid_positive"),
        # An accepted bid below the advertised price.
        CheckConstraint(
            "current_bid IS NULL OR current_bid >= starting_bid",
            name="current_bid_at_least_starting",
        ),
        # A reserve under the advertised starting price is not a reserve; it would also
        # make the bid statement's COALESCE floor lower than starting_bid, so the two
        # guards there would disagree about what the minimum is.
        CheckConstraint(
            "reserve IS NULL OR reserve >= starting_bid", name="reserve_at_least_starting"
        ),
        CheckConstraint("event_seq >= 0", name="event_seq_not_negative"),
        CheckConstraint(CURRENCY_IS_A_CODE, name="currency_is_a_code"),
        # What the bid's composite foreign key points at. Postgres requires a unique
        # constraint on the referenced columns, and this one also means an auction cannot
        # be redenominated while bids reference the pair.
        UniqueConstraint("id", "currency", name="uq_auctions_id_currency"),
    )


class Outbox(BigIntAuditBase):
    """An event that must be announced, written in the transaction that caused it.

    Without this, publishing is at-most-once: the bid commits, the process dies, and no
    watcher is ever told. The row is the obligation, and it commits or rolls back with the
    money that created it.

    Delivery is at-least-once — a crash after publishing but before sent_at is set makes
    the relay publish again — which is safe only because every event carries its auction's
    sequence number, so a duplicate is one the client has already seen.
    """

    __tablename__ = "outbox"
    auction_id: Mapped[int] = mapped_column(ForeignKey("auctions.id"), index=True)
    seq: Mapped[int]
    payload: Mapped[str] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # One row per event. The auction's own counter supplies the number, so a retried
        # write cannot invent a second announcement of the same thing.
        UniqueConstraint("auction_id", "seq", name="uq_outbox_auction_seq"),
        # What the relay reads. Unsent rows are a short queue at the head of a table that
        # only grows, so a partial index stays small while the table does not.
        Index(
            "ix_outbox_unsent",
            "auction_id",
            "seq",
            postgresql_where=text("sent_at IS NULL"),
        ),
    )


class Bid(BigIntAuditBase):
    __tablename__ = "bids"
    # BigInteger spelled out: the type came free from ForeignKey("auctions.id") until the
    # composite constraint below replaced it, and Mapped[int] alone is a 32-bit column.
    auction_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    amount: Mapped[Decimal] = mapped_column(Money)
    # Copied from the auction by the bid statement, not supplied by the client. It is here
    # so the pair below can be a foreign key: settling an IDR auction against a bid read
    # as USD is a four-order-of-magnitude error in the amount owed.
    currency: Mapped[str] = mapped_column(Currency)
    # Idempotency. The key is the client's; `response` is the body the first attempt
    # returned, so a retry replays it instead of being told it outbid itself.
    #
    # ponytail: both live on the bid row rather than in their own table, because the row is
    # already keyed by (auction_id, user_id) and is written in the same transaction as the
    # money. Give them a table of their own when a second endpoint needs keys.
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    response: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # The history row is what a settlement would be computed from.
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint(CURRENCY_IS_A_CODE, name="currency_is_a_code"),
        # Referential integrity for the auction and the currency match in one constraint,
        # replacing the plain auction_id foreign key. A row whose currency disagrees with
        # its auction's has no referent, so the table refuses it — `update`, a data
        # migration and psql all bypass the statement that copies the currency across.
        ForeignKeyConstraint(
            ["auction_id", "currency"],
            ["auctions.id", "auctions.currency"],
            name="fk_bids_auction_id_auctions",
        ),
        # The whole dedupe. No application-level "have I seen this key" test, which would
        # lose the race between two retries arriving together. NULLs are not equal in
        # Postgres, so bids without a key are unaffected. Name spelled in full because the
        # uq convention does not prefix an explicit one; `alembic check` catches that.
        UniqueConstraint(
            "auction_id",
            "user_id",
            "idempotency_key",
            name="uq_bids_auction_user_idempotency_key",
        ),
        CheckConstraint(
            "(idempotency_key IS NULL) = (response IS NULL)", name="key_and_response_together"
        ),
    )
