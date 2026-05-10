from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.utils.timestamps import to_ms_utc

NY = ZoneInfo("America/New_York")


def test_utc_epoch_zero() -> None:
    assert to_ms_utc(datetime(1970, 1, 1, tzinfo=UTC)) == 0


def test_known_utc_moment() -> None:
    # 2024-05-01 00:00 UTC = 1714521600000 ms (used as a fixture anchor in v0.5 tests)
    assert to_ms_utc(datetime(2024, 5, 1, tzinfo=UTC)) == 1714521600000


def test_ny_and_utc_agree_at_same_instant() -> None:
    """The same instant in different zones produces the same ms."""
    utc = datetime(2024, 5, 1, 14, 30, tzinfo=UTC)
    ny = datetime(2024, 5, 1, 10, 30, tzinfo=NY)  # EDT in May = UTC-4
    assert to_ms_utc(utc) == to_ms_utc(ny)
