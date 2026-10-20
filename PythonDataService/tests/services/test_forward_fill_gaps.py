"""
Regression: forward_fill_gaps must not reshape higher-resolution bars onto a
1-minute grid.

Bug 2026-04-29: with the chart's timeframe selector set to 15m and the ZIP
download fired, the dataset.csv came back at 1-minute intervals even though
metadata.csv said timespan=minute, multiplier=15. Root cause was that
``forward_fill_gaps`` hardcoded ``freq="min"`` and merged the 15-minute bars
onto a 1-minute template, ffilling OHLC across every intermediate minute.

These tests pin the new contract:
  * For (timespan='minute', multiplier=N), the filled grid steps every N
    minutes — no fabricated intermediate bars.
  * For non-minute timespans (day/week/month), the function returns the
    frame unchanged (within-day fill is meaningless when there is at most
    one bar per day group).
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.models.requests import DatasetGenerationRequest
from app.services.dataset_service import forward_fill_gaps, preprocess_and_calculate

_ET = ZoneInfo("US/Eastern")


def _ms(year: int, month: int, day: int, hour: int, minute: int) -> int:
    """ET wall-clock → unix ms UTC."""
    return int(datetime(year, month, day, hour, minute, tzinfo=_ET).timestamp() * 1000)


def _bar(ts_ms: int, *, close: float = 100.0) -> dict[str, float | int]:
    return {
        "timestamp": ts_ms,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 1000,
        "transactions": 5,
    }


@pytest.mark.parametrize(
    ("bars", "multiplier", "expected_rows", "expected_step_ms"),
    [
        # A handful of 15-min bars across an RTH session must produce 26
        # rows (09:30 → 15:45 inclusive), not 390 rows. Before the fix this
        # returned 390 minute-stamped rows with ffilled OHLC.
        pytest.param(
            ((9, 30, 100.0), (9, 45, 101.0), (10, 0, 102.0), (15, 45, 110.0)),
            15,
            26,
            15 * 60 * 1000,
            id="15m_bars_stay_at_15m_resolution",
        ),
        # The pre-existing minute-resolution path must keep working — 390 RTH
        # slots with ffilled gaps (09:30 → 15:59 inclusive).
        pytest.param(
            ((9, 30, 100.0), (9, 35, 101.0), (15, 59, 110.0)),
            1,
            390,
            60 * 1000,
            id="1m_bars_unchanged_behavior",
        ),
    ],
)
def test_minute_bars_fill_on_a_grid_of_their_own_multiplier(
    bars: tuple[tuple[int, int, float], ...], multiplier: int, expected_rows: int, expected_step_ms: int
):
    df = pd.DataFrame([_bar(_ms(2026, 1, 5, hour, minute), close=close) for hour, minute, close in bars])

    out = forward_fill_gaps(df, session="rth", timespan="minute", multiplier=multiplier)

    assert len(out) == expected_rows, f"expected {expected_rows} {multiplier}-minute slots, got {len(out)}"

    # Spacing between consecutive timestamps must be exactly one multiplier step.
    deltas_ms = out["timestamp"].diff().dropna().unique().tolist()
    assert deltas_ms == [expected_step_ms], f"non-{multiplier}m spacing: {deltas_ms}"

    # The known bars survive at their own timestamps with the correct close.
    by_ts = {int(row["timestamp"]): row for _, row in out.iterrows()}
    known_closes = {_ms(2026, 1, 5, hour, minute): close for hour, minute, close in bars}
    assert set(known_closes) <= set(by_ts), "known bars survive at their own timestamps"
    assert {ts: by_ts[ts]["close"] for ts in known_closes} == known_closes, "known bars keep their own close"


@pytest.mark.parametrize("timespan", ["hour", "day", "week", "month"])
def test_non_minute_returns_frame_unchanged(timespan: str):
    """Hour/day/week/month resolutions: Polygon returns these bars
    pre-aligned to their own boundaries (top of the hour, etc.) which do
    not match the RTH session start of 09:30 ET. Building a synthetic
    grid for them would produce timestamps that never overlap with the
    real bars, the merge would return all-NaN, and the resulting CSV
    would have empty rows for every slot — exactly the bug that surfaced
    when a 2-year hourly RTH dataset came back with 3006 timestamp-only
    rows. The function must short-circuit and pass real bars through."""
    bars = [
        _bar(_ms(2026, 1, 5, 9, 0), close=100.0),    # Polygon hourly aligns to :00
        _bar(_ms(2026, 1, 5, 10, 0), close=100.5),
        _bar(_ms(2026, 1, 6, 9, 0), close=101.0),
    ]
    df = pd.DataFrame(bars)

    out = forward_fill_gaps(df, session="rth", timespan=timespan, multiplier=1)

    # Must return the input frame unchanged — every row keeps its real OHLC.
    assert len(out) == 3
    assert out["close"].tolist() == [100.0, 100.5, 101.0]


# ---------------------------------------------------------------------------
# fail_on_gaps tests
# ---------------------------------------------------------------------------


# gap: 9:36, 9:37, 9:38 are missing
_GAP_SLOTS = ((9, 30, 100.0), (9, 35, 101.0), (9, 39, 102.0), (9, 40, 103.0))


def _bars_with_gap() -> list[dict]:
    """Two bars with a 5-minute gap between them (3 missing 1-min bars)."""
    return [_bar(_ms(2026, 1, 5, hour, minute), close=close) for hour, minute, close in _GAP_SLOTS]


def test_fail_on_gaps_raises_on_gap():
    """fail_on_gaps=True must raise ValueError when intra-day gaps exist."""
    with pytest.raises(ValueError, match="fail_on_gaps=True"):
        preprocess_and_calculate(
            bars=_bars_with_gap(),
            indicator_entries=[],
            session="rth",
            forward_fill=True,
            fail_on_gaps=True,
            timespan="minute",
            multiplier=1,
        )


@pytest.mark.parametrize(
    ("slots", "forward_fill", "fail_on_gaps", "timespan", "expected_rows"),
    [
        # Gap synthesis remains available only through explicit request flags:
        # the 4 input bars expand to 390 RTH slots.
        pytest.param(_GAP_SLOTS, True, False, "minute", 390, id="explicit_fill_allows_gap_synthesis"),
        # fail_on_gaps=True must not raise when the data is gap-free.
        pytest.param(
            ((9, 30, 100.0), (9, 31, 100.5), (9, 32, 101.0)),
            False,
            True,
            "minute",
            3,
            id="fail_on_gaps_no_gap_does_not_raise",
        ),
        # fail_on_gaps=True must be a no-op for hourly bars (gaps are expected).
        pytest.param(
            ((9, 0, 100.0), (11, 0, 102.0)),
            False,
            True,
            "hour",
            2,
            id="fail_on_gaps_skipped_for_non_minute",
        ),
    ],
)
def test_preprocess_keeps_the_rows_the_gap_flags_allow(
    slots: tuple[tuple[int, int, float], ...],
    forward_fill: bool,
    fail_on_gaps: bool,
    timespan: str,
    expected_rows: int,
):
    df, _ = preprocess_and_calculate(
        bars=[_bar(_ms(2026, 1, 5, hour, minute), close=close) for hour, minute, close in slots],
        indicator_entries=[],
        session="rth",
        forward_fill=forward_fill,
        fail_on_gaps=fail_on_gaps,
        timespan=timespan,
        multiplier=1,
    )
    assert len(df) == expected_rows


def test_dataset_request_defaults_to_raw_gap_failing_input() -> None:
    request = DatasetGenerationRequest(
        ticker="SPY",
        from_date="2026-01-05",
        to_date="2026-01-06",
    )

    assert request.forward_fill is False
    assert request.fail_on_gaps is True
