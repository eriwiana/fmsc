"""The one timezone rule, in a module both app.auctions and app.communities can import.

It lived in app.auctions until a community code needed the same rule for its own deadline.
app.auctions already imports app.communities for the attempt sweeper, so the rule had to
move somewhere neither imports — duplicating it would be two places for one convention to
drift apart in, and the direction it drifts is seven hours.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

# The app operates in Asia/Jakarta (WIB, UTC+7). Storage stays UTC (timestamptz); this only
# governs the boundaries — naive input is read as Jakarta wall time, output is rendered in it.
APP_TZ = ZoneInfo("Asia/Jakarta")


def resolve_deadline(dt: datetime) -> datetime:
    """A naive datetime is interpreted as Jakarta wall time; an aware one is left as-is."""
    return dt.replace(tzinfo=APP_TZ) if dt.tzinfo is None else dt
