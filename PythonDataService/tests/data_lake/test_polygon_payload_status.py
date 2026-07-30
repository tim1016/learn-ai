"""Regression: Polygon Starter (delayed entitlement) answers status=DELAYED.

The payload shape is identical to OK; rejecting it broke the broker-v2 chart
history backfill for every symbol (2026-07-30 live run).
"""

from __future__ import annotations

import pytest

from app.data_lake.polygon_fetcher import (
    PolygonFetchError,
    _raise_for_payload_status,
)


def test_payload_status_delayed_is_success() -> None:
    _raise_for_payload_status({"status": "DELAYED"}, "SPY", 200)


def test_payload_status_ok_is_success() -> None:
    _raise_for_payload_status({"status": "OK"}, "SPY", 200)


def test_payload_status_error_still_raises() -> None:
    with pytest.raises(PolygonFetchError):
        _raise_for_payload_status({"status": "ERROR", "error": "boom"}, "SPY", 200)
