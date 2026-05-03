"""Tests for app.broker.ibkr.market_data — Ticker → IbkrOptionQuote
conversion."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from types import SimpleNamespace

from app.broker.ibkr.market_data import _ticker_to_quote


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
    q = _ticker_to_quote(ticker, "SPY", 1_800_000_000_000, 420.0, "C")
    assert q.greeks_source == "model"
    assert q.iv == 0.21
    assert q.delta == 0.55
    assert q.bid == 1.20
    assert q.bid_size == 10
    assert q.ts_ms == int(ticker.time.timestamp() * 1000)


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
    q = _ticker_to_quote(ticker, "SPY", 1_800_000_000_000, 420.0, "C")
    assert q.greeks_source == "bid"
    assert q.iv == 0.20


def test_ticker_with_no_greeks_marks_source_none() -> None:
    ticker = SimpleNamespace(
        bid=1.20, ask=1.25, last=None, bidSize=None, askSize=None,
        modelGreeks=None, bidGreeks=None, askGreeks=None, lastGreeks=None,
    )
    q = _ticker_to_quote(ticker, "SPY", 1_800_000_000_000, 420.0, "C")
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
    q = _ticker_to_quote(ticker, "SPY", 1_800_000_000_000, 420.0, "C")
    assert q.greeks_source == "bid"
    assert q.iv == 0.205


def test_nan_quote_fields_become_none() -> None:
    ticker = SimpleNamespace(
        bid=math.nan,
        ask=math.nan,
        last=math.nan,
        bidSize=math.nan,
        askSize=math.nan,
        modelGreeks=None,
        bidGreeks=None,
        askGreeks=None,
        lastGreeks=None,
    )
    q = _ticker_to_quote(ticker, "SPY", 1_800_000_000_000, 420.0, "C")
    assert q.bid is None
    assert q.ask is None
    assert q.last is None
    assert q.bid_size is None
    assert q.ask_size is None
