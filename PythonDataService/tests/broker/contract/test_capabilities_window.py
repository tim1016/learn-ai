"""The extended window is capability data, and it agrees with the support flag."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.broker.contract.capabilities import BrokerCapabilities, ExtendedHoursWindow


def _capabilities(**overrides: object) -> BrokerCapabilities:
    base: dict[str, object] = {
        "broker": "x",
        "paper_only": True,
        "supports_fractional": True,
        "supports_extended_hours": True,
        "extended_hours_window": ExtendedHoursWindow(open_minute_et=4 * 60, close_minute_et=20 * 60),
        "supported_order_types": ("market", "limit"),
        "data_feed": "test",
        "bars_may_gap": False,
        "max_stream_symbols": 1,
        "max_concurrent_streams": 1,
        "rest_rate_limit_per_min": 1,
    }
    base.update(overrides)
    return BrokerCapabilities(**base)


def test_window_must_open_before_it_closes() -> None:
    with pytest.raises(ValidationError):
        ExtendedHoursWindow(open_minute_et=20 * 60, close_minute_et=4 * 60)


def test_supported_extended_hours_requires_a_window() -> None:
    with pytest.raises(ValidationError, match="extended_hours_window"):
        _capabilities(extended_hours_window=None)


def test_unsupported_extended_hours_forbids_a_window() -> None:
    with pytest.raises(ValidationError, match="extended_hours_window"):
        _capabilities(supports_extended_hours=False)


def test_unsupported_extended_hours_without_a_window_is_valid() -> None:
    assert _capabilities(supports_extended_hours=False, extended_hours_window=None).extended_hours_window is None
