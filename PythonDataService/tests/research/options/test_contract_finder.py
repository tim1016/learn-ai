"""Tests for contract finder — delta-based strike selection."""
from __future__ import annotations

import math

import pytest
from scipy.stats import norm

from app.research.options.contract_finder import (
    _bs_delta,
    _find_atm_strike,
    _find_otm_put_by_delta,
    _find_otm_call_by_delta,
    TARGET_DELTA_PUT,
    TARGET_DELTA_CALL,
)


class TestBsDelta:
    """Test BS delta calculation for strike selection."""

    def test_atm_call_delta_near_half(self):
        """ATM call delta should be close to 0.5."""
        delta = _bs_delta(S=100, K=100, T=30/365, r=0.04, sigma=0.25, option_type="call")
        assert 0.45 <= delta <= 0.60

    def test_atm_put_delta_near_minus_half(self):
        """ATM put delta should be close to -0.5."""
        delta = _bs_delta(S=100, K=100, T=30/365, r=0.04, sigma=0.25, option_type="put")
        assert -0.60 <= delta <= -0.45

    def test_deep_otm_put_delta_near_zero(self):
        """Deep OTM put should have delta near 0."""
        delta = _bs_delta(S=100, K=70, T=30/365, r=0.04, sigma=0.25, option_type="put")
        assert -0.05 <= delta <= 0.0

    def test_deep_itm_call_delta_near_one(self):
        """Deep ITM call should have delta near 1."""
        delta = _bs_delta(S=100, K=70, T=30/365, r=0.04, sigma=0.25, option_type="call")
        assert delta >= 0.95

    def test_put_call_delta_relationship(self):
        """Call delta - Put delta = 1 (approximately, for same strike)."""
        call_d = _bs_delta(S=100, K=100, T=30/365, r=0.04, sigma=0.25, option_type="call")
        put_d = _bs_delta(S=100, K=100, T=30/365, r=0.04, sigma=0.25, option_type="put")
        assert abs((call_d - put_d) - 1.0) < 0.01

    def test_zero_time_returns_zero(self):
        delta = _bs_delta(S=100, K=100, T=0, r=0.04, sigma=0.25, option_type="call")
        assert delta == 0.0


class TestDeltaBasedStrikeSelection:
    """Test delta-based OTM put/call selection."""

    def _make_contracts(self, strikes: list[float], contract_type: str) -> list[dict]:
        return [
            {"strike_price": k, "contract_type": contract_type, "ticker": f"O:TEST{k}"}
            for k in strikes
        ]

    def test_otm_put_selects_25_delta(self):
        """Should select the put closest to -0.25 delta."""
        strikes = [85, 90, 92, 95, 97, 100, 103, 105]
        contracts = self._make_contracts(strikes, "put")
        result = _find_otm_put_by_delta(contracts, stock_close=100, dte_days=30)
        assert result is not None
        # 25Δ put is typically ~5-8% OTM for 30 DTE, ~25% vol
        assert result["strike_price"] < 100

    def test_otm_call_selects_25_delta(self):
        """Should select the call closest to 0.25 delta."""
        strikes = [95, 97, 100, 103, 105, 108, 110, 115]
        contracts = self._make_contracts(strikes, "call")
        result = _find_otm_call_by_delta(contracts, stock_close=100, dte_days=30)
        assert result is not None
        assert result["strike_price"] > 100

    def test_fallback_to_fixed_offset_when_dte_zero(self):
        """With DTE=0, should fall back to 5% OTM offset."""
        strikes = [90, 95, 100, 105, 110]
        puts = self._make_contracts(strikes, "put")
        result = _find_otm_put_by_delta(puts, stock_close=100, dte_days=0)
        assert result is not None
        assert result["strike_price"] == 95  # 5% below 100

    def test_empty_contracts_returns_none(self):
        result = _find_otm_put_by_delta([], stock_close=100, dte_days=30)
        assert result is None

    def test_no_matching_type_returns_none(self):
        """Contracts of wrong type should return None."""
        calls_only = self._make_contracts([95, 100, 105], "call")
        result = _find_otm_put_by_delta(calls_only, stock_close=100, dte_days=30)
        assert result is None


class TestFindAtmStrike:
    """Test ATM strike selection."""

    def test_selects_closest_to_spot(self):
        contracts = [
            {"strike_price": 95}, {"strike_price": 100}, {"strike_price": 105},
        ]
        result = _find_atm_strike(contracts, stock_close=101)
        assert result["strike_price"] == 100

    def test_empty_returns_none(self):
        assert _find_atm_strike([], stock_close=100) is None
