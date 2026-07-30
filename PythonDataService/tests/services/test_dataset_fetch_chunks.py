from __future__ import annotations

from unittest.mock import Mock

from app.services.dataset_service import fetch_bars_chunks_raw


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
