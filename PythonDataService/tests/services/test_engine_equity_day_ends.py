"""One equity point a day, kept on every engine run (#2930).

A summary run drops the per-bar curve; the last sample of each
America/New_York date stays, so a caller that runs thousands of backtests
can still draw each one.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.engine.engine import EquitySnapshot
from app.services.engine_backtest_service import _day_ends_or_none, equity_day_ends

_NY = ZoneInfo("America/New_York")


def _at(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=_NY).timestamp() * 1000)


def _sample(ms: int, equity: str) -> EquitySnapshot:
    return EquitySnapshot(timestamp_ms=ms, equity=Decimal(equity), cash=Decimal(equity), holdings_value=Decimal(0))


@pytest.mark.parametrize(
    ("samples", "expected"),
    [
        pytest.param(
            (
                (_at(2025, 3, 6, 9, 31), "100000"),
                (_at(2025, 3, 6, 16, 0), "100250.5"),
                (_at(2025, 3, 7, 9, 31), "100300"),
                (_at(2025, 3, 7, 12, 0), "99900"),
                (_at(2025, 3, 7, 16, 0), "100100.25"),
            ),
            ((_at(2025, 3, 6, 16, 0), 100250.5), (_at(2025, 3, 7, 16, 0), 100100.25)),
            id="each-et-date-keeps-its-last-sample-in-order",
        ),
        # Clocks moved forward on Sunday 2025-03-09. A sample at 23:59 ET belongs to its own date on either side;
        # a fixed UTC offset would put one of them on the wrong day.
        pytest.param(
            (
                (_at(2025, 3, 7, 23, 59), "1"),
                (_at(2025, 3, 10, 0, 1), "2"),
                (_at(2025, 3, 10, 23, 59), "3"),
                (_at(2025, 3, 11, 0, 0), "4"),
            ),
            ((_at(2025, 3, 7, 23, 59), 1.0), (_at(2025, 3, 10, 23, 59), 3.0), (_at(2025, 3, 11, 0, 0), 4.0)),
            id="a-day-boundary-is-et-midnight-on-both-sides-of-a-clock-change",
        ),
        # Friday then Monday: the weekend between them adds nothing.
        pytest.param(
            ((_at(2025, 3, 7, 16, 0), "10"), (_at(2025, 3, 10, 16, 0), "11")),
            ((_at(2025, 3, 7, 16, 0), 10.0), (_at(2025, 3, 10, 16, 0), 11.0)),
            id="a-day-without-a-sample-has-no-point",
        ),
        pytest.param((), (), id="an-empty-run-has-none"),
    ],
)
def test_equity_day_ends_keeps_the_last_sample_of_each_et_date(
    samples: tuple[tuple[int, str], ...], expected: tuple[tuple[int, float], ...]
) -> None:
    curve = [_sample(ms, equity) for ms, equity in samples]

    assert equity_day_ends(curve) == [{"timestamp": ms, "equity": equity} for ms, equity in expected], (
        "one point per ET date, the date's last sample, in time order"
    )


def test_samples_out_of_time_order_are_refused_never_searched() -> None:
    late_first = [_sample(_at(2025, 3, 7, 16, 0), "10"), _sample(_at(2025, 3, 6, 16, 0), "11")]
    repeated = [_sample(_at(2025, 3, 6, 16, 0), "10"), _sample(_at(2025, 3, 6, 16, 0), "11")]

    for curve in (late_first, repeated):
        with pytest.raises(ValueError, match="strictly increasing"):
            equity_day_ends(curve)


def test_a_run_whose_samples_are_out_of_order_keeps_no_day_ends_and_does_not_fail() -> None:
    late_first = [_sample(_at(2025, 3, 7, 16, 0), "10"), _sample(_at(2025, 3, 6, 16, 0), "11")]
    in_order = [_sample(_at(2025, 3, 6, 16, 0), "10"), _sample(_at(2025, 3, 7, 16, 0), "11")]

    assert _day_ends_or_none(late_first) == []
    assert _day_ends_or_none(in_order) == equity_day_ends(in_order)
