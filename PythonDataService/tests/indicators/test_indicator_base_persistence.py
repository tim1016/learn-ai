"""Tests for Indicator.to_state_dict / restore_state on the base class.

Subclass round-trip + bit-identical-output tests come in Task 5; this
task pins the base contract: common fields persist; subclass extras
are an extension point via _to_state_extra/_restore_state_extra.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.engine.indicators.base import Indicator


class _CountingIndicator(Indicator):
    """Minimal subclass for testing: records last value as the indicator value."""

    def _compute_next_value(self, time: datetime, value: Decimal) -> Decimal | None:
        return value


def test_to_state_dict_includes_common_fields() -> None:
    ind = _CountingIndicator("X", 3)
    ind.update(datetime(2026, 5, 18, 14, 0, tzinfo=UTC), Decimal("100"))
    ind.update(datetime(2026, 5, 18, 14, 15, tzinfo=UTC), Decimal("101"))
    state = ind.to_state_dict()
    assert state["name"] == "X"
    assert state["period"] == 3
    assert state["samples"] == 2
    assert state["current_value"] == "101"
    assert state["current_time_ms"] == int(datetime(2026, 5, 18, 14, 15, tzinfo=UTC).timestamp() * 1000)
    assert state["previous_value"] == "100"
    assert state["previous_time_ms"] == int(datetime(2026, 5, 18, 14, 0, tzinfo=UTC).timestamp() * 1000)


def test_restore_state_round_trip() -> None:
    src = _CountingIndicator("X", 3)
    src.update(datetime(2026, 5, 18, 14, 0, tzinfo=UTC), Decimal("100"))
    src.update(datetime(2026, 5, 18, 14, 15, tzinfo=UTC), Decimal("101"))
    state = src.to_state_dict()

    dst = _CountingIndicator("X", 3)
    dst.restore_state(state)
    assert dst.samples == src.samples
    assert dst.current_value == src.current_value
    assert dst.current_time == src.current_time
    assert dst.previous_value == src.previous_value
    assert dst.previous_time == src.previous_time


def test_restore_state_rejects_name_mismatch() -> None:
    src = _CountingIndicator("X", 3)
    src.update(datetime(2026, 5, 18, 14, 0, tzinfo=UTC), Decimal("100"))
    state = src.to_state_dict()

    dst = _CountingIndicator("Y", 3)
    with pytest.raises(ValueError, match="name mismatch"):
        dst.restore_state(state)


def test_restore_state_rejects_period_mismatch() -> None:
    src = _CountingIndicator("X", 3)
    state = src.to_state_dict()
    state["samples"] = 0  # fresh; just probing the period check

    dst = _CountingIndicator("X", 5)
    with pytest.raises(ValueError, match="period mismatch"):
        dst.restore_state(state)


def test_to_state_dict_decimals_are_strings() -> None:
    ind = _CountingIndicator("X", 3)
    ind.update(datetime(2026, 5, 18, 14, 0, tzinfo=UTC), Decimal("100.123456789012345"))
    state = ind.to_state_dict()
    # Quoted-string preserves Decimal precision exactly.
    assert isinstance(state["current_value"], str)
    assert Decimal(state["current_value"]) == Decimal("100.123456789012345")


def test_to_state_dict_rejects_tz_naive_current_time() -> None:
    """tz-naive timestamps must not flow through serialization — int64 ms UTC contract."""
    from app.engine.indicators.base import _datetime_to_ms

    naive = datetime(2026, 5, 18, 14, 0)  # no tzinfo
    with pytest.raises(ValueError, match="tz-naive"):
        _datetime_to_ms(naive)


def test_restore_state_missing_key_raises_value_error() -> None:
    """A truncated state dict raises ValueError per the docstring, not raw KeyError."""
    src = _CountingIndicator("X", 3)
    src.update(datetime(2026, 5, 18, 14, 0, tzinfo=UTC), Decimal("100"))
    state = src.to_state_dict()
    del state["samples"]

    dst = _CountingIndicator("X", 3)
    with pytest.raises(ValueError, match="missing required key"):
        dst.restore_state(state)
