"""Tests for contract finder — delta-based strike selection."""

from __future__ import annotations

from app.research.options.contract_finder import (
    _bs_delta,
    _find_atm_strike,
    _find_otm_call_by_delta,
    _find_otm_put_by_delta,
)


class TestBsDelta:
    """Test BS delta calculation for strike selection."""

    def test_bs_delta_values(self):
        call_d = _bs_delta(S=100, K=100, T=30 / 365, r=0.04, sigma=0.25, option_type="call")
        put_d = _bs_delta(S=100, K=100, T=30 / 365, r=0.04, sigma=0.25, option_type="put")
        assert 0.45 <= call_d <= 0.60, "ATM call delta should be close to 0.5"
        assert -0.60 <= put_d <= -0.45, "ATM put delta should be close to -0.5"
        # Call delta - Put delta = 1 (approximately, for same strike).
        assert abs((call_d - put_d) - 1.0) < 0.01, "put-call delta relationship: call delta - put delta = 1"

        delta = _bs_delta(S=100, K=70, T=30 / 365, r=0.04, sigma=0.25, option_type="put")
        assert -0.05 <= delta <= 0.0, "deep OTM put should have delta near 0"

        delta = _bs_delta(S=100, K=70, T=30 / 365, r=0.04, sigma=0.25, option_type="call")
        assert delta >= 0.95, "deep ITM call should have delta near 1"

        delta = _bs_delta(S=100, K=100, T=0, r=0.04, sigma=0.25, option_type="call")
        assert delta == 0.0, "zero time returns zero"


class TestDeltaBasedStrikeSelection:
    """Test delta-based OTM put/call selection."""

    def _make_contracts(self, strikes: list[float], contract_type: str) -> list[dict]:
        return [{"strike_price": k, "contract_type": contract_type, "ticker": f"O:TEST{k}"} for k in strikes]

    def test_delta_based_strike_selection(self):
        # Should select the put closest to -0.25 delta.
        strikes = [85, 90, 92, 95, 97, 100, 103, 105]
        contracts = self._make_contracts(strikes, "put")
        result = _find_otm_put_by_delta(contracts, stock_close=100, dte_days=30, rfr=0.043)
        assert result is not None, "OTM put selects a 25-delta strike"
        # 25Δ put is typically ~5-8% OTM for 30 DTE, ~25% vol
        assert result["strike_price"] < 100, "OTM put 25-delta strike is below spot"

        # Should select the call closest to 0.25 delta.
        strikes = [95, 97, 100, 103, 105, 108, 110, 115]
        contracts = self._make_contracts(strikes, "call")
        result = _find_otm_call_by_delta(contracts, stock_close=100, dte_days=30, rfr=0.043)
        assert result is not None, "OTM call selects a 25-delta strike"
        assert result["strike_price"] > 100, "OTM call 25-delta strike is above spot"

        # With DTE=0, should fall back to 5% OTM offset.
        strikes = [90, 95, 100, 105, 110]
        puts = self._make_contracts(strikes, "put")
        result = _find_otm_put_by_delta(puts, stock_close=100, dte_days=0, rfr=0.043)
        assert result is not None, "DTE=0 falls back to a fixed-offset put"
        assert result["strike_price"] == 95, "DTE=0 fallback is the 5% OTM put (95 below 100)"

        result = _find_otm_put_by_delta([], stock_close=100, dte_days=30, rfr=0.043)
        assert result is None, "empty contracts returns none"

        # Contracts of wrong type should return None.
        calls_only = self._make_contracts([95, 100, 105], "call")
        result = _find_otm_put_by_delta(calls_only, stock_close=100, dte_days=30, rfr=0.043)
        assert result is None, "no matching contract type returns none"


class TestFindAtmStrike:
    """Test ATM strike selection."""

    def test_find_atm_strike(self):
        contracts = [
            {"strike_price": 95},
            {"strike_price": 100},
            {"strike_price": 105},
        ]
        result = _find_atm_strike(contracts, stock_close=101)
        assert result["strike_price"] == 100, "selects the strike closest to spot"
        assert _find_atm_strike([], stock_close=100) is None, "empty returns none"
