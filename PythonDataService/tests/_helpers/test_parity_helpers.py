"""Unit tests for parity assertion helpers."""

from __future__ import annotations

from decimal import Decimal

import pytest


def _state_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "ts_ms_utc": 1,
        "close": 1.0,
        "ema_fast": 1.0,
        "ema_slow": 1.0,
        "rsi": 50.0,
        "cross_state": "equal",
        "signal": "HOLD",
    }
    return {**row, **overrides}


@pytest.mark.parametrize(
    ("ts_ms_utc", "close", "ema_slow", "ema_fast_lean", "ema_fast_engine", "atol"),
    [
        pytest.param(1736178300000, 591.2, 590.9, 591.1, 591.1, 1e-9, id="passes_on_identical_rows"),
        pytest.param(1, 1.0, 590.0, 591.123456789, 591.1234567895, 1e-8, id="passes_within_tolerance"),
    ],
)
def test_assert_state_traces_match_passes(
    ts_ms_utc: int, close: float, ema_slow: float, ema_fast_lean: float, ema_fast_engine: float, atol: float
) -> None:
    from tests._helpers.parity import assert_state_traces_match

    def rows(ema_fast: float) -> list[dict[str, object]]:
        return [
            _state_row(
                ts_ms_utc=ts_ms_utc, close=close, ema_fast=ema_fast, ema_slow=ema_slow, rsi=55.0, cross_state="above"
            )
        ]

    # Within tolerance → no exception.
    assert_state_traces_match(rows(ema_fast_lean), rows(ema_fast_engine), atol=atol, rtol=0.0)


@pytest.mark.parametrize(
    ("engine_ema_fasts", "message"),
    [
        pytest.param((), "row count", id="fails_on_row_count_mismatch"),
        pytest.param((1.5,), "ema_fast", id="fails_on_field_divergence"),
    ],
)
def test_assert_state_traces_match_fails(engine_ema_fasts: tuple[float, ...], message: str) -> None:
    from tests._helpers.parity import assert_state_traces_match

    lean = [_state_row()]
    engine = [_state_row(ema_fast=ema_fast) for ema_fast in engine_ema_fasts]
    with pytest.raises(AssertionError, match=message):
        assert_state_traces_match(lean, engine, atol=1e-9, rtol=0.0)


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
