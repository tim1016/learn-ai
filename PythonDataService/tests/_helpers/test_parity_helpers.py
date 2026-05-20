"""Unit tests for parity assertion helpers."""

from __future__ import annotations

from decimal import Decimal

import pytest


def test_assert_state_traces_match_passes_on_identical_rows() -> None:
    from tests._helpers.parity import assert_state_traces_match

    rows = [
        {
            "ts_ms_utc": 1736178300000,
            "close": 591.2,
            "ema_fast": 591.1,
            "ema_slow": 590.9,
            "rsi": 55.0,
            "cross_state": "above",
            "signal": "HOLD",
        },
    ]
    # Identical lists → no exception.
    assert_state_traces_match(rows, rows, atol=1e-9, rtol=0.0)


def test_assert_state_traces_match_passes_within_tolerance() -> None:
    from tests._helpers.parity import assert_state_traces_match

    a = [
        {
            "ts_ms_utc": 1,
            "close": 1.0,
            "ema_fast": 591.123456789,
            "ema_slow": 590.0,
            "rsi": 55.0,
            "cross_state": "above",
            "signal": "HOLD",
        }
    ]
    b = [
        {
            "ts_ms_utc": 1,
            "close": 1.0,
            "ema_fast": 591.1234567895,
            "ema_slow": 590.0,
            "rsi": 55.0,
            "cross_state": "above",
            "signal": "HOLD",
        }
    ]
    assert_state_traces_match(a, b, atol=1e-8, rtol=0.0)


def test_assert_state_traces_match_fails_on_row_count_mismatch() -> None:
    from tests._helpers.parity import assert_state_traces_match

    a = [
        {
            "ts_ms_utc": 1,
            "close": 1.0,
            "ema_fast": 1.0,
            "ema_slow": 1.0,
            "rsi": 50.0,
            "cross_state": "equal",
            "signal": "HOLD",
        }
    ]
    with pytest.raises(AssertionError, match="row count"):
        assert_state_traces_match(a, [], atol=1e-9, rtol=0.0)


def test_assert_state_traces_match_fails_on_field_divergence() -> None:
    from tests._helpers.parity import assert_state_traces_match

    a = [
        {
            "ts_ms_utc": 1,
            "close": 1.0,
            "ema_fast": 1.0,
            "ema_slow": 1.0,
            "rsi": 50.0,
            "cross_state": "equal",
            "signal": "HOLD",
        }
    ]
    b = [
        {
            "ts_ms_utc": 1,
            "close": 1.0,
            "ema_fast": 1.5,
            "ema_slow": 1.0,
            "rsi": 50.0,
            "cross_state": "equal",
            "signal": "HOLD",
        }
    ]
    with pytest.raises(AssertionError, match="ema_fast"):
        assert_state_traces_match(a, b, atol=1e-9, rtol=0.0)


def test_assert_trade_equivalence_passes_on_identical_trades() -> None:
    from tests._helpers.parity import assert_trade_equivalence

    trades = [
        {
            "entry_ms_utc": 1736178300000,
            "exit_ms_utc": 1736182800000,
            "quantity": Decimal("168"),
            "entry_price": Decimal("591.20"),
            "exit_price": Decimal("592.50"),
        },
    ]
    assert_trade_equivalence(trades, trades, fill_price_atol=Decimal("0.01"))


def test_assert_trade_equivalence_fails_on_price_drift_over_tolerance() -> None:
    from tests._helpers.parity import assert_trade_equivalence

    a = [
        {
            "entry_ms_utc": 1,
            "exit_ms_utc": 2,
            "quantity": Decimal("100"),
            "entry_price": Decimal("100.00"),
            "exit_price": Decimal("101.00"),
        }
    ]
    b = [
        {
            "entry_ms_utc": 1,
            "exit_ms_utc": 2,
            "quantity": Decimal("100"),
            "entry_price": Decimal("100.00"),
            "exit_price": Decimal("101.02"),
        }
    ]
    with pytest.raises(AssertionError, match="exit_price"):
        assert_trade_equivalence(a, b, fill_price_atol=Decimal("0.01"))
