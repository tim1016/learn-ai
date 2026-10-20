from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.utils.timestamps import timestamp_like_to_ms_utc, to_ms_utc

NY = ZoneInfo("America/New_York")


def test_known_utc_moment_and_ny_and_utc_agree_at_same_instant() -> None:
    # 2024-05-01 00:00 UTC = 1714521600000 ms (used as a fixture anchor in v0.5 tests)
    assert to_ms_utc(datetime(2024, 5, 1, tzinfo=UTC)) == 1714521600000, "a known UTC moment"
    # The same instant in different zones produces the same ms.
    utc = datetime(2024, 5, 1, 14, 30, tzinfo=UTC)
    ny = datetime(2024, 5, 1, 10, 30, tzinfo=NY)  # EDT in May = UTC-4
    assert to_ms_utc(utc) == to_ms_utc(ny), "NY and UTC agree at the same instant"


def test_to_ms_utc_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        to_ms_utc(datetime(2024, 5, 1))


@pytest.mark.parametrize(
    "value",
    [1_714_521_600_000, "2024-05-01T00:00:00Z"],
    ids=["keeps-epoch-ms", "normalizes-z-string"],
)
def test_timestamp_like_resolves_to_the_same_epoch_ms(value: int | str) -> None:
    assert timestamp_like_to_ms_utc(value) == 1_714_521_600_000


def test_timestamp_like_rejects_naive_string() -> None:
    with pytest.raises(ValueError, match="include a timezone"):
        timestamp_like_to_ms_utc("2024-05-01T00:00:00")
