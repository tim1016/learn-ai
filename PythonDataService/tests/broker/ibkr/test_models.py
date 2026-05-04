"""Tests for the wire models in app.broker.ibkr.models — sentinel coercion
and round-trip serialisation."""

from __future__ import annotations

import math

from app.broker.ibkr.models import (
    IbkrChainSnapshot,
    IbkrOptionQuote,
    _coerce_iv,
    _coerce_optional_float,
    _coerce_quote,
)


def test_coerce_optional_float_preserves_real_values() -> None:
    assert _coerce_optional_float(0.0) == 0.0
    assert _coerce_optional_float(-1.0) == -1.0  # not iv-specific
    assert _coerce_optional_float(0.42) == 0.42


def test_coerce_optional_float_treats_nan_as_none() -> None:
    assert _coerce_optional_float(math.nan) is None
    assert _coerce_optional_float(None) is None


def test_coerce_iv_treats_negative_as_none() -> None:
    assert _coerce_iv(-1.0) is None
    assert _coerce_iv(-0.001) is None
    assert _coerce_iv(0.0) == 0.0  # zero IV is real (deep-ITM at expiry)
    assert _coerce_iv(0.18) == 0.18
    assert _coerce_iv(math.nan) is None


def test_coerce_quote_strips_negative_and_nan_but_keeps_zero() -> None:
    """Bid/ask/last sentinel handling: IBKR uses ``-1.0`` for "no quote",
    NaN for "unset". A real bid of $0.00 (deep-OTM with no buyer) is
    legitimate and must NOT be stripped."""
    assert _coerce_quote(-1.0) is None  # IBKR "no quote" sentinel
    assert _coerce_quote(-0.01) is None  # any negative price is meaningless
    assert _coerce_quote(math.nan) is None
    assert _coerce_quote(None) is None
    assert _coerce_quote(0.0) == 0.0  # legitimate bid for deep-OTM
    assert _coerce_quote(0.05) == 0.05
    assert _coerce_quote(123.45) == 123.45


def test_option_quote_round_trips_via_json() -> None:
    q = IbkrOptionQuote(
        symbol="SPY",
        expiry_ms=1_800_000_000_000,
        strike=420.0,
        right="C",
        bid=1.23,
        ask=1.25,
        iv=0.21,
        delta=0.55,
        greeks_source="model",
        ts_ms=1_800_000_000_500,
    )
    payload = q.model_dump_json()
    restored = IbkrOptionQuote.model_validate_json(payload)
    assert restored == q


def test_chain_snapshot_keeps_quote_order() -> None:
    quotes = [
        IbkrOptionQuote(
            symbol="SPY",
            expiry_ms=1_800_000_000_000,
            strike=k,
            right="C",
            ts_ms=1_800_000_000_500,
        )
        for k in (400.0, 405.0, 410.0)
    ]
    snap = IbkrChainSnapshot(
        symbol="SPY",
        expiry_ms=1_800_000_000_000,
        underlying_price=405.5,
        quotes=quotes,
        as_of_ms=1_800_000_000_500,
    )
    assert [q.strike for q in snap.quotes] == [400.0, 405.0, 410.0]
