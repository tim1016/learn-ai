from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.lean_sidecar.trading_calendar import session_close_ms_utc
from app.utils.session_anchors import option_expiry_anchor_ms, option_expiry_date

_HOUR_MS = 3_600_000


def _utc(year: int, month: int, day: int, hour: int) -> int:
    return int(datetime(year, month, day, hour, tzinfo=UTC).timestamp() * 1000)


def test_option_expiry_anchor_is_16_00_new_york_in_summer_and_in_winter() -> None:
    # 16:00 New York is 20:00 UTC under daylight time and 21:00 UTC under standard time.
    assert option_expiry_anchor_ms(date(2025, 7, 18)) == _utc(2025, 7, 18, 20)
    assert option_expiry_anchor_ms(date(2025, 1, 17)) == _utc(2025, 1, 17, 21)


def test_option_expiry_anchor_stays_at_16_00_on_a_half_day() -> None:
    # 2025-11-28, the day after Thanksgiving, closes at 13:00. The anchor names
    # the date; it does not move with the session.
    half_day = date(2025, 11, 28)

    assert option_expiry_anchor_ms(half_day) == _utc(2025, 11, 28, 21)
    assert option_expiry_anchor_ms(half_day) - session_close_ms_utc(half_day) == 3 * _HOUR_MS


@pytest.mark.parametrize("expiry_date", [date(2025, 1, 17), date(2025, 7, 18), date(2026, 3, 9), date(2026, 11, 2)])
def test_option_expiry_date_reads_back_the_date_an_anchor_names(expiry_date: date) -> None:
    assert option_expiry_date(option_expiry_anchor_ms(expiry_date)) == expiry_date


@pytest.mark.parametrize(
    "off_anchor_ms",
    [
        pytest.param(_utc(2026, 6, 19, 0), id="midnight UTC, the way IBKR expiries used to be stored"),
        pytest.param(_utc(2026, 6, 19, 20) + 1, id="one millisecond late"),
        pytest.param(_utc(2026, 6, 19, 21), id="16:00 at the winter offset on a summer date"),
    ],
)
def test_option_expiry_date_refuses_an_instant_that_is_not_an_anchor(off_anchor_ms: int) -> None:
    with pytest.raises(ValueError, match="is not an option expiry"):
        option_expiry_date(off_anchor_ms)
