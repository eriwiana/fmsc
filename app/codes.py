"""Invite codes: one live code per community, redeemed for a membership.

The code is a bearer credential. It is shown to its owner once, at creation, and never
again — the row keeps an HMAC of it and nothing that can be turned back into it. An owner
who loses a code rotates it, which is one request.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets

# Crockford Base32: no I, L, O or U. The first three because they are the characters people
# confuse with 1 and 0 when reading a code off a screen, and U so that no code spells
# anything a community would rather it did not.
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# What a misread character is corrected to. Crockford defines these: a reader who types O
# for zero or I for one gets the code they were looking at, not a refusal.
_CONFUSIONS = str.maketrans({"O": "0", "I": "1", "L": "1"})

# 13 characters at 5 bits each. Guessing one is 2**65 attempts, which is why the rate limit
# on redemption is about noise and cost rather than about the code falling.
CODE_LENGTH = 13

# Crockford's check alphabet: the 32 symbols plus five that never appear in a payload, so a
# check symbol can never be mistaken for part of the code.
_CHECK_ALPHABET = CROCKFORD + "*~$=U"

PREFIX = "FMSC"

# Read at import, so a deployment without one does not start. A default pepper is no
# pepper — every deployment's HMAC would be identical and offline-guessable — and a
# per-boot random one silently kills every existing code on the next deploy.
_PEPPER = os.environ.get("FMSC_CODE_PEPPER", "")
if len(_PEPPER) < 32:
    raise RuntimeError(
        "FMSC_CODE_PEPPER must be set to at least 32 characters."
        " It is the HMAC pepper for invite codes; without it a leaked database hands"
        " every code to whoever read it."
    )


def _value(body: str) -> int:
    value = 0
    for character in body:
        value = value * 32 + CROCKFORD.index(character)
    return value


def check_symbol(body: str) -> str:
    """Crockford's check symbol: the payload's value modulo 37.

    It catches a typo before the database is asked anything, which keeps a mistyped code
    out of the audit log and off the rate limiter's bucket — a fat finger is not an
    enumeration attempt.
    """
    return _CHECK_ALPHABET[_value(body) % 37]


def new_code() -> tuple[str, str]:
    """A fresh code. Returns (what to show the owner, what to store a lookup of).

    Two values because they are different things: the first is shown once and then
    forgotten, the second is the only thing that survives.
    """
    body = "".join(secrets.choice(CROCKFORD) for _ in range(CODE_LENGTH))
    return formatted(body), body


def formatted(body: str) -> str:
    """FMSC-XXXX-XXXX-XXXX-XX: the payload in groups of four with its check symbol last.

    Grouping is for reading aloud and typing from a screenshot. Nothing parses it back by
    position — normalise() throws the punctuation away.
    """
    full = body + check_symbol(body)
    groups = [full[i : i + 4] for i in range(0, len(full), 4)]
    return "-".join([PREFIX, *groups])


def normalise(presented: str) -> str | None:
    """The payload of a code as typed, or None if it is not a code at all.

    Case, spacing, the prefix and the hyphens are all noise. O/I/L are corrected rather
    than refused, which is the point of Crockford. The check symbol is verified here, so a
    typo never reaches the database and never reaches the audit log.
    """
    cleaned = presented.strip().upper().replace(" ", "").replace("-", "")
    # Only when it is long enough to *be* the prefix plus a code. Stripping it on its first
    # four characters alone breaks every code whose own payload happens to start FMSC — one
    # in about a million, which is exactly often enough to be somebody's bad afternoon.
    if len(cleaned) == len(PREFIX) + CODE_LENGTH + 1 and cleaned.startswith(PREFIX):
        cleaned = cleaned[len(PREFIX) :]
    cleaned = cleaned.translate(_CONFUSIONS)
    if len(cleaned) != CODE_LENGTH + 1:
        return None
    body, presented_check = cleaned[:CODE_LENGTH], cleaned[CODE_LENGTH]
    if any(character not in CROCKFORD for character in body):
        return None
    # Before compare_digest, which raises TypeError on a str holding anything outside
    # ASCII rather than answering False. Unhandled that is a 500, and a 500 is
    # distinguishable from the four refusals — which is the one thing criterion 5 is about.
    if presented_check not in _CHECK_ALPHABET:
        return None
    # compare_digest on a check symbol is not about secrecy — it is one character of
    # public arithmetic — but it costs nothing and keeps every comparison in this module
    # the same shape.
    if not hmac.compare_digest(presented_check, check_symbol(body)):
        return None
    return body


def lookup(body: str) -> str:
    """What the codes table is keyed on. The code itself is never stored or logged."""
    return hmac.new(_PEPPER.encode(), body.encode(), hashlib.sha256).hexdigest()
