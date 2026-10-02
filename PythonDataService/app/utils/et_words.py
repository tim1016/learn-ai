"""The owner's words for an instant in ET: ``Wed Sep 30, 15:59 ET`` (#2665).

``et_when_words`` is the one author of backend copy that names a day and
minute in ET: a bot's end, the Start window's next open, the Deploy exit
steps, the end-sale alert. The words are display-only: never stored, parsed
back, or compared; the value stays ``int64 ms UTC``.

They live in their own module, outside ``session_anchors.py`` and
``timestamps.py``, because those two are hashed — into every Sweep / Grid
Search / Walk-Forward ``source_digest`` (``IDENTITY_SOURCE_PATHS``) and every
Signal Program seal (``program_sources.py``). Wording must stay editable
without moving a digest, which would strand every paused study and invalidate
every committed qualification receipt.
"""

from __future__ import annotations

from app.utils.session_anchors import et_date_at_ms
from app.utils.timestamps import ny_datetime


def et_when_words(instant_ms: int, *, now_ms: int) -> str:
    """An instant as the owner reads it: ``Wed Sep 30, 15:59 ET``, with the year when it is not this one.

    Always the weekday and date, never "today": the owner may not be in the
    market's time zone, so "today" in ET can be tomorrow where they are.
    """
    return f"{et_day_words(instant_ms, now_ms=now_ms)}, {et_clock_words(instant_ms)} ET"


def et_day_words(instant_ms: int, *, now_ms: int) -> str:
    """An instant's ET date as the owner reads it: ``Wed Sep 30``, with the year when it is not now's (in ET)."""
    return et_day_words_in_year(instant_ms, current_year=et_date_at_ms(now_ms).year)


def et_day_words_in_year(instant_ms: int, *, current_year: int) -> str:
    """``Wed Sep 30``, with the year when it is not ``current_year``: for copy that must not read the clock."""
    day = et_date_at_ms(instant_ms)
    year = "" if day.year == current_year else f" {day.year}"
    return f"{day:%a %b} {day.day}{year}"


def et_clock_words(instant_ms: int) -> str:
    """An instant's ET wall clock, to the minute: ``15:59``."""
    return f"{ny_datetime(instant_ms):%H:%M}"
