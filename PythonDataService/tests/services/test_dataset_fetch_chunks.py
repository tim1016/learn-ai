from __future__ import annotations

from unittest.mock import Mock

import pytest

from app.services.dataset_service import CanonicalBarsError, fetch_bars_chunked, fetch_bars_chunks_raw


@pytest.mark.parametrize(
    ("ticker", "from_date", "to_date", "timestamps"),
    [
        pytest.param("TSLA", "2025-07-28", "2025-07-28", (1,), id="single-day-range-fetches-that-inclusive-day"),
        pytest.param("SPY", "2025-07-28", "2025-07-30", (), id="multi-day-range-includes-the-requested-end-date"),
    ],
)
def test_a_raw_fetch_asks_for_the_requested_inclusive_range(
    ticker: str, from_date: str, to_date: str, timestamps: tuple[int, ...]
) -> None:
    polygon = Mock()
    polygon.fetch_aggregates.return_value = [{"timestamp": t} for t in timestamps]

    bars = fetch_bars_chunks_raw(
        polygon,
        ticker,
        from_date,
        to_date,
    )

    assert bars == [{"timestamp": t} for t in timestamps], "the bars come back as the provider served them"
    polygon.fetch_aggregates.assert_called_once()
    kwargs = polygon.fetch_aggregates.call_args.kwargs
    assert kwargs["from_date"] == from_date, "the fetch starts on the requested first day"
    assert kwargs["to_date"] == to_date, "the fetch includes the requested end date"


@pytest.mark.parametrize(
    ("timestamps", "match"),
    [
        pytest.param((1_700_000_000_000, 1_700_000_000_000), "duplicate timestamp", id="duplicate-timestamps"),
        pytest.param((1_700_000_060_000, 1_700_000_000_000), "non-monotonic timestamp", id="non-monotonic-timestamps"),
        pytest.param((None,), "int64-ms timestamp", id="int64-ms-none"),
        pytest.param(("1700000000000",), "int64-ms timestamp", id="int64-ms-numeric-string"),
        pytest.param((1.5,), "int64-ms timestamp", id="int64-ms-float"),
        pytest.param((True,), "int64-ms timestamp", id="int64-ms-bool"),
        pytest.param((2**63,), "exceeds int64", id="timestamp-outside-int64"),
    ],
)
def test_chunked_fetch_rejects_a_timestamp_series_that_is_not_canonical(
    timestamps: tuple[object, ...], match: str
) -> None:
    polygon = Mock()
    polygon.fetch_aggregates.return_value = [{"timestamp": t} for t in timestamps]

    with pytest.raises(CanonicalBarsError, match=match):
        fetch_bars_chunked(polygon, "SPY", "2025-07-28", "2025-07-28")
