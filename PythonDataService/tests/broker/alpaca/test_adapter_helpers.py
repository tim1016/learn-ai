"""Tests for the adapter's ingestion-boundary helpers (temporal + numeric)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.broker.alpaca.adapter import (
    et_date_to_ms,
    now_ms,
    occurred_at_ms,
    opt_float,
    opt_rfc3339_to_ms,
    rfc3339_to_ms,
    to_float,
    to_str,
)


def test_rfc3339_epoch_and_z_suffix() -> None:
    assert rfc3339_to_ms("1970-01-01T00:00:00+00:00") == 0
    assert rfc3339_to_ms("1970-01-01T00:00:01Z") == 1000


def test_rfc3339_honors_offset() -> None:
    # Midnight at -05:00 is 05:00 UTC → 5h = 18_000_000 ms after epoch.
    assert rfc3339_to_ms("1970-01-01T00:00:00-05:00") == 18_000_000


def test_rfc3339_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="not timezone-aware"):
        rfc3339_to_ms("2021-03-16T18:38:01")


def test_rfc3339_trims_overlong_fraction() -> None:
    assert rfc3339_to_ms("1970-01-01T00:00:00.123456789Z") == 123


def test_rfc3339_rounds_fractional_milliseconds_to_nearest_ms() -> None:
    assert rfc3339_to_ms("1970-01-01T00:00:00.123600Z") == 124


def test_optional_helpers_pass_through_none() -> None:
    assert opt_float(None) is None
    assert opt_float("") is None
    assert opt_float("3.5") == 3.5
    assert opt_rfc3339_to_ms(None) is None
    assert opt_rfc3339_to_ms("") is None


def test_to_float_parses_decimal_string() -> None:
    assert to_float("1000.50") == 1000.50


@pytest.mark.parametrize("parser", [to_float, opt_float])
@pytest.mark.parametrize("value", [True, False])
def test_numeric_helpers_reject_booleans(parser: Callable[[Any], float | None], value: bool) -> None:
    # ``float(True) == 1.0``: a JSON boolean in a money field would otherwise
    # become $1 or $0 of broker evidence.
    with pytest.raises(TypeError, match="not a boolean"):
        parser(value)


def test_to_str_returns_the_vendor_text_unchanged() -> None:
    assert to_str("SPY", field="symbol") == "SPY"


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(None, id="null"),
        pytest.param("", id="blank"),
        pytest.param("   ", id="whitespace"),
        pytest.param(12345, id="number"),
        pytest.param(True, id="boolean"),
    ],
)
def test_to_str_refuses_a_value_that_is_not_text(value: object) -> None:
    # ``str(None) == "None"``: a null identity field would otherwise become a
    # real-looking order id, symbol or account number (#2643).
    with pytest.raises(ValueError, match="'symbol' must be a non-blank string"):
        to_str(value, field="symbol")


def test_et_date_anchors_at_ny_midnight() -> None:
    expected = int(
        datetime(2021, 1, 4, tzinfo=ZoneInfo("America/New_York")).timestamp() * 1000
    )
    assert et_date_to_ms("2021-01-04") == expected


def test_occurred_at_prefers_transaction_time_then_date() -> None:
    assert occurred_at_ms({"transaction_time": "1970-01-01T00:00:01Z"}) == 1000
    assert occurred_at_ms({"date": "2021-01-04"}) == et_date_to_ms("2021-01-04")
    assert occurred_at_ms({}) is None


def test_now_ms_is_epoch_millis() -> None:
    value = now_ms()
    assert isinstance(value, int)
    assert value > 1_600_000_000_000
