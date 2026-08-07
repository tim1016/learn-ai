from __future__ import annotations

from unittest.mock import Mock

import pytest

from app.services.dataset_service import CanonicalBarsError, fetch_bars_chunked, fetch_bars_chunks_raw


def test_single_day_range_fetches_that_inclusive_day() -> None:
    polygon = Mock()
    polygon.fetch_aggregates.return_value = [{"timestamp": 1}]

    bars = fetch_bars_chunks_raw(
        polygon,
        "TSLA",
        "2025-07-28",
        "2025-07-28",
    )

    assert bars == [{"timestamp": 1}]
    polygon.fetch_aggregates.assert_called_once()
    kwargs = polygon.fetch_aggregates.call_args.kwargs
    assert kwargs["from_date"] == "2025-07-28"
    assert kwargs["to_date"] == "2025-07-28"


def test_multi_day_range_includes_the_requested_end_date() -> None:
    polygon = Mock()
    polygon.fetch_aggregates.return_value = []

    fetch_bars_chunks_raw(
        polygon,
        "SPY",
        "2025-07-28",
        "2025-07-30",
    )

    assert polygon.fetch_aggregates.call_args.kwargs["to_date"] == "2025-07-30"


def test_chunked_fetch_rejects_duplicate_timestamps() -> None:
    polygon = Mock()
    polygon.fetch_aggregates.return_value = [
        {"timestamp": 1_700_000_000_000},
        {"timestamp": 1_700_000_000_000},
    ]

    with pytest.raises(CanonicalBarsError, match="duplicate timestamp"):
        fetch_bars_chunked(polygon, "SPY", "2025-07-28", "2025-07-28")


def test_chunked_fetch_rejects_non_monotonic_timestamps() -> None:
    polygon = Mock()
    polygon.fetch_aggregates.return_value = [
        {"timestamp": 1_700_000_060_000},
        {"timestamp": 1_700_000_000_000},
    ]

    with pytest.raises(CanonicalBarsError, match="non-monotonic timestamp"):
        fetch_bars_chunked(polygon, "SPY", "2025-07-28", "2025-07-28")


@pytest.mark.parametrize("bad_timestamp", [None, "1700000000000", 1.5, True])
def test_chunked_fetch_requires_int64_ms_timestamps(bad_timestamp: object) -> None:
    polygon = Mock()
    polygon.fetch_aggregates.return_value = [{"timestamp": bad_timestamp}]

    with pytest.raises(CanonicalBarsError, match="int64-ms timestamp"):
        fetch_bars_chunked(polygon, "SPY", "2025-07-28", "2025-07-28")


def test_chunked_fetch_rejects_timestamp_outside_int64() -> None:
    polygon = Mock()
    polygon.fetch_aggregates.return_value = [{"timestamp": 2**63}]

    with pytest.raises(CanonicalBarsError, match="exceeds int64"):
        fetch_bars_chunked(polygon, "SPY", "2025-07-28", "2025-07-28")
