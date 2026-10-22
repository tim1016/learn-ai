"""Tests for app.broker.ibkr.market_data — Ticker → IbkrOptionQuote
conversion."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.broker.ibkr.live.protocol import QuoteUpdate
from app.broker.ibkr.live.quote_callbacks import number
from app.broker.ibkr.market_data import _quote_to_model


def receipt(ticker):
    return QuoteUpdate(
        received_at_ms=1234567890000,
        data_type=1,
        ticks=(),
        values={key: number(getattr(ticker, key, None)) for key in ("bid", "ask", "last", "bidSize", "askSize")},
        greeks={name: {key: number(value) for key, value in vars(block).items()}
                for name in ("modelGreeks", "bidGreeks", "askGreeks", "lastGreeks")
                if (block := getattr(ticker, name, None)) is not None},
    )


def _greeks(*, iv, delta, gamma, theta, vega, und):
    return SimpleNamespace(
        impliedVol=iv,
        delta=delta,
        gamma=gamma,
        theta=theta,
        vega=vega,
        undPrice=und,
    )


def test_ticker_with_model_greeks_picks_model_source() -> None:
    ticker = SimpleNamespace(
        bid=1.20,
        ask=1.25,
        last=1.22,
        bidSize=10,
        askSize=11,
        modelGreeks=_greeks(iv=0.21, delta=0.55, gamma=0.04, theta=-0.05, vega=0.10, und=420.0),
        bidGreeks=None,
        askGreeks=None,
        lastGreeks=None,
        time=datetime(2026, 5, 2, 14, 30, tzinfo=UTC),
    )
    q = _quote_to_model(receipt(ticker), "SPY", 1_800_000_000_000, 420.0, "C")
    assert q.greeks_source == "model"
    assert q.iv == 0.21
    assert q.delta == 0.55
    assert q.bid == 1.20
    assert q.bid_size == 10
    assert q.ts_ms == 1234567890000


def test_ticker_falls_back_to_bid_greeks_when_model_missing() -> None:
    ticker = SimpleNamespace(
        bid=1.20,
        ask=1.25,
        last=None,
        bidSize=None,
        askSize=None,
        modelGreeks=None,
        bidGreeks=_greeks(iv=0.20, delta=0.54, gamma=0.04, theta=-0.04, vega=0.10, und=420.0),
        askGreeks=None,
        lastGreeks=None,
    )
    q = _quote_to_model(receipt(ticker), "SPY", 1_800_000_000_000, 420.0, "C")
    assert q.greeks_source == "bid"
    assert q.iv == 0.20


def test_ticker_with_no_greeks_marks_source_none() -> None:
    ticker = SimpleNamespace(
        bid=1.20, ask=1.25, last=None, bidSize=None, askSize=None,
        modelGreeks=None, bidGreeks=None, askGreeks=None, lastGreeks=None,
    )
    q = _quote_to_model(receipt(ticker), "SPY", 1_800_000_000_000, 420.0, "C")
    assert q.greeks_source == "none"
    assert q.iv is None
    assert q.delta is None


def test_negative_iv_in_model_greeks_falls_through_to_bid_greeks() -> None:
    """IBKR sometimes ships ``-1.0`` impliedVol in the model block when the
    surface calc fails. We must skip that block and try the next."""
    ticker = SimpleNamespace(
        bid=1.20,
        ask=1.25,
        last=None,
        bidSize=None,
        askSize=None,
        modelGreeks=_greeks(iv=-1.0, delta=0.55, gamma=0.04, theta=-0.05, vega=0.10, und=420.0),
        bidGreeks=_greeks(iv=0.205, delta=0.54, gamma=0.04, theta=-0.04, vega=0.10, und=420.0),
        askGreeks=None,
        lastGreeks=None,
    )
    q = _quote_to_model(receipt(ticker), "SPY", 1_800_000_000_000, 420.0, "C")
    assert q.greeks_source == "bid"
    assert q.iv == 0.205


@pytest.mark.parametrize(
    ("price", "size"),
    [
        pytest.param(math.nan, math.nan, id="nan_quote_fields_become_none"),
        # Regression: IBKR sends ``-1.0`` as the "no L1 quote" sentinel for
        # bid/ask/last. Pre-fix the values leaked through ``_coerce_optional_float``
        # and rendered as ``-$1.00`` in the options-chain table; mid-price math
        # produced bogus engine reprice triggers. Now stripped at ingestion via
        # ``_coerce_quote``.
        pytest.param(-1.0, None, id="negative_one_bid_ask_become_none_regression"),
    ],
)
def test_sentinel_quote_fields_become_none(price: float, size: float | None) -> None:
    ticker = SimpleNamespace(
        bid=price,
        ask=price,
        last=price,
        bidSize=size,
        askSize=size,
        modelGreeks=None,
        bidGreeks=None,
        askGreeks=None,
        lastGreeks=None,
    )
    q = _quote_to_model(receipt(ticker), "SPY", 1_800_000_000_000, 420.0, "C")
    assert q.bid is None, "bid becomes none"
    assert q.ask is None, "ask becomes none"
    assert q.last is None, "last becomes none"
    assert q.bid_size is None, "bid size becomes none"
    assert q.ask_size is None, "ask size becomes none"


def test_zero_bid_is_preserved() -> None:
    """A real bid of ``$0.00`` (deep-OTM with no buyer at any positive
    price) is legitimate and must NOT be stripped — only negatives are
    sentinels for bid/ask/last."""
    ticker = SimpleNamespace(
        bid=0.0,
        ask=0.05,
        last=None,
        bidSize=0,
        askSize=10,
        modelGreeks=None,
        bidGreeks=None,
        askGreeks=None,
        lastGreeks=None,
    )
    q = _quote_to_model(receipt(ticker), "SPY", 1_800_000_000_000, 420.0, "C")
    assert q.bid == 0.0
    assert q.ask == 0.05


def test_negative_one_delta_is_preserved() -> None:
    """``_coerce_quote`` only governs bid/ask/last. Delta still flows
    through ``_coerce_optional_float`` so a deep-ITM put can legitimately
    report ``delta = -1.0``."""
    ticker = SimpleNamespace(
        bid=10.0,
        ask=10.5,
        last=10.2,
        bidSize=5,
        askSize=5,
        modelGreeks=_greeks(iv=0.18, delta=-1.0, gamma=0.0, theta=-0.01, vega=0.0, und=420.0),
        bidGreeks=None,
        askGreeks=None,
        lastGreeks=None,
    )
    q = _quote_to_model(receipt(ticker), "SPY", 1_800_000_000_000, 420.0, "P")
    assert q.delta == -1.0  # NOT stripped — legitimate Greek value


# ── B-12: IBKR negative "no size" sentinel must be stripped ─────────────


def test_ticker_negative_size_sentinel_coerced_to_none() -> None:
    """Regression (B-12): IBKR sends -1 for "no size available". It was only
    NaN-checked, so a -1 leaked through as a negative depth on the wire."""
    ticker = SimpleNamespace(
        bid=1.20, ask=1.25, last=None, bidSize=-1.0, askSize=-1,
        modelGreeks=None, bidGreeks=None, askGreeks=None, lastGreeks=None,
    )
    q = _quote_to_model(receipt(ticker), "SPY", 1_800_000_000_000, 420.0, "C")
    assert q.bid_size is None
    assert q.ask_size is None


def test_ticker_positive_size_passes_through() -> None:
    ticker = SimpleNamespace(
        bid=1.20, ask=1.25, last=None, bidSize=7, askSize=9.0,
        modelGreeks=None, bidGreeks=None, askGreeks=None, lastGreeks=None,
    )
    q = _quote_to_model(receipt(ticker), "SPY", 1_800_000_000_000, 420.0, "C")
    assert q.bid_size == 7
    assert q.ask_size == 9
