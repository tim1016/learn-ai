"""An option expiry as one instant: 16:00 New York time on the expiry date (ADR 0022 (i)).

The anchor names the date. It is not a session time: on a half day it is still
16:00, and when a contract stops trading that day comes from the calendar's
session close.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from annotated_types import Gt, Le
from pydantic import AfterValidator

from app.utils.session_anchors import MAX_TIMESTAMP_MS, et_date_at_ms
from app.utils.timestamps import to_ms_utc

_NY = ZoneInfo("America/New_York")
_ANCHOR_HOUR_NEW_YORK = 16


def option_expiry_anchor_ms(expiry_date: date) -> int:
    """The one instant an option's expiry date is stored and compared as."""
    return to_ms_utc(datetime(expiry_date.year, expiry_date.month, expiry_date.day, _ANCHOR_HOUR_NEW_YORK, tzinfo=_NY))


def option_expiry_date(expiry_ms: int) -> date:
    """The expiry date an anchor instant names; refuses an instant that is not an expiry anchor."""
    expiry_date = et_date_at_ms(expiry_ms)
    if option_expiry_anchor_ms(expiry_date) != expiry_ms:
        # Says what the instant is and proposes no date: one stored the old way,
        # as midnight UTC, falls on the evening before the date it meant.
        in_new_york = datetime.fromtimestamp(expiry_ms / 1000, tz=UTC).astimezone(_NY)
        raise ValueError(
            f"{expiry_ms} is not an option expiry: an expiry is 16:00 New York time on its date, "
            f"and this is {in_new_york:%H:%M:%S}.{in_new_york.microsecond // 1000:03d} New York time "
            f"on {expiry_date.isoformat()}."
        )
    return expiry_date


def _an_option_expiry(expiry_ms: int) -> int:
    option_expiry_date(expiry_ms)
    return expiry_ms


#: An ``int64 ms UTC`` input that must be an option expiry: the one way a schema
#: field or a query parameter says so. The bounds come first, so a value no
#: calendar can hold is refused as out of range before its date is asked for.
OptionExpiryMs = Annotated[int, Gt(0), Le(MAX_TIMESTAMP_MS), AfterValidator(_an_option_expiry)]
