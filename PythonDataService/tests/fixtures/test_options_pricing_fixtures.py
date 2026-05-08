"""Golden fixture validation for options-pricing fixtures (BS-001, BS-002, BS-003).

Loads each fixture via the registry, calls the canonical function on every
input row, and asserts numerical agreement with the oracle output at the
tolerance pinned in manifest.json.

Run in isolation (no FastAPI app needed):
  python -m pytest tests/fixtures/test_options_pricing_fixtures.py -v --noconftest
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pyarrow as pa

# Ensure PythonDataService root is on path (for app.services imports)
_SVC_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_SVC_ROOT))

from golden_support.registry import default as registry  # noqa: E402

from app.services.bs_greeks import black_scholes_greeks, bs_european_price  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load(fixture_id: str) -> tuple[pa.Table, pa.Table, float, float]:
    """Return (input_table, output_table, atol, rtol) for a fixture."""
    files = registry.active_files(fixture_id)
    fixture_dir = registry.fixture_dir(fixture_id)
    manifest_fixture = registry._manifest.by_id(fixture_id)

    inp = pa.ipc.open_file(fixture_dir / files.input).read_all()
    out = pa.ipc.open_file(fixture_dir / files.output).read_all()
    atol = manifest_fixture.tolerance.atol
    rtol = manifest_fixture.tolerance.rtol
    return inp, out, atol, rtol


# ---------------------------------------------------------------------------
# BS-001: Call Price
# ---------------------------------------------------------------------------

class TestBS001CallPrice:
    def test_case_count(self) -> None:
        inp, _, _, _ = _load("BS-001")
        assert len(inp) == 180, f"Expected 180 cases, got {len(inp)}"

    def test_canonical_matches_oracle(self) -> None:
        inp, out, atol, rtol = _load("BS-001")
        spots = inp["spot"].to_pylist()
        strikes = inp["strike"].to_pylist()
        ttms = inp["ttm_years"].to_pylist()
        rates = inp["rate"].to_pylist()
        vols = inp["volatility"].to_pylist()
        dividends = inp["dividend"].to_pylist()
        is_calls = inp["is_call"].to_pylist()
        oracle_prices = out["oracle_price"].to_pylist()

        canonical_prices = [
            bs_european_price(s, k, t, r, v, c, d)
            for s, k, t, r, v, c, d in zip(
                spots, strikes, ttms, rates, vols, is_calls, dividends, strict=True
            )
        ]

        actual = np.array(canonical_prices)
        expected = np.array(oracle_prices)
        max_err = float(np.max(np.abs(actual - expected)))

        assert np.allclose(actual, expected, atol=atol, rtol=rtol), (
            f"BS-001 canonical vs oracle: max_abs_err={max_err:.3e}, atol={atol:.3e}"
        )


# ---------------------------------------------------------------------------
# BS-002: Put Price
# ---------------------------------------------------------------------------

class TestBS002PutPrice:
    def test_case_count(self) -> None:
        inp, _, _, _ = _load("BS-002")
        assert len(inp) == 180, f"Expected 180 cases, got {len(inp)}"

    def test_canonical_matches_oracle(self) -> None:
        inp, out, atol, rtol = _load("BS-002")
        spots = inp["spot"].to_pylist()
        strikes = inp["strike"].to_pylist()
        ttms = inp["ttm_years"].to_pylist()
        rates = inp["rate"].to_pylist()
        vols = inp["volatility"].to_pylist()
        dividends = inp["dividend"].to_pylist()
        is_calls = inp["is_call"].to_pylist()
        oracle_prices = out["oracle_price"].to_pylist()

        canonical_prices = [
            bs_european_price(s, k, t, r, v, c, d)
            for s, k, t, r, v, c, d in zip(
                spots, strikes, ttms, rates, vols, is_calls, dividends, strict=True
            )
        ]

        actual = np.array(canonical_prices)
        expected = np.array(oracle_prices)
        max_err = float(np.max(np.abs(actual - expected)))

        assert np.allclose(actual, expected, atol=atol, rtol=rtol), (
            f"BS-002 canonical vs oracle: max_abs_err={max_err:.3e}, atol={atol:.3e}"
        )

    def test_put_call_parity(self) -> None:
        """Put price >= max(K*e^(-rT) - S, 0) for all cases (lower bound check)."""
        inp, out, _, _ = _load("BS-002")
        spots = inp["spot"].to_pylist()
        strikes = inp["strike"].to_pylist()
        ttms = inp["ttm_years"].to_pylist()
        rates = inp["rate"].to_pylist()
        oracle_prices = out["oracle_price"].to_pylist()
        import math
        for s, k, t, r, p in zip(spots, strikes, ttms, rates, oracle_prices, strict=True):
            lower_bound = max(k * math.exp(-r * t) - s, 0.0)
            assert p >= lower_bound - 1e-10, (
                f"Put price {p:.6f} < lower bound {lower_bound:.6f} for S={s}, K={k}, T={t}"
            )


# ---------------------------------------------------------------------------
# BS-003: Call Delta
# ---------------------------------------------------------------------------

class TestBS003CallDelta:
    def test_case_count(self) -> None:
        inp, _, _, _ = _load("BS-003")
        assert len(inp) == 180, f"Expected 180 cases, got {len(inp)}"

    def test_canonical_matches_oracle(self) -> None:
        inp, out, atol, rtol = _load("BS-003")
        spots = inp["spot"].to_pylist()
        strikes = inp["strike"].to_pylist()
        ttms = inp["ttm_years"].to_pylist()
        rates = inp["rate"].to_pylist()
        vols = inp["volatility"].to_pylist()
        dividends = inp["dividend"].to_pylist()
        is_calls = inp["is_call"].to_pylist()
        oracle_deltas = out["oracle_delta"].to_pylist()

        # black_scholes_greeks arg order: spot, strike, ttm_years, volatility, rate, dividend, is_call
        canonical_deltas = [
            black_scholes_greeks(s, k, t, v, r, d, c).delta
            for s, k, t, r, v, c, d in zip(
                spots, strikes, ttms, rates, vols, is_calls, dividends, strict=True
            )
        ]

        actual = np.array(canonical_deltas)
        expected = np.array(oracle_deltas)
        max_err = float(np.max(np.abs(actual - expected)))

        assert np.allclose(actual, expected, atol=atol, rtol=rtol), (
            f"BS-003 canonical vs oracle: max_abs_err={max_err:.3e}, atol={atol:.3e}"
        )

    def test_call_delta_in_range(self) -> None:
        """Call delta must be in (0, 1) for all positive-TTM cases."""
        _inp, out, _, _ = _load("BS-003")
        oracle_deltas = out["oracle_delta"].to_pylist()
        for i, d in enumerate(oracle_deltas):
            assert 0.0 < d < 1.0, f"Row {i}: call delta {d:.6f} not in (0, 1)"

    def test_itm_delta_greater_than_otm(self) -> None:
        """Deep ITM call (S=120, K=90) delta > ATM call (S=100, K=100) delta."""
        inp, out, _, _ = _load("BS-003")
        spots = inp["spot"].to_pylist()
        strikes = inp["strike"].to_pylist()
        oracle_deltas = out["oracle_delta"].to_pylist()

        itm_deltas = [d for s, k, d in zip(spots, strikes, oracle_deltas, strict=True)
                      if s == 120.0 and k == 90.0]
        atm_deltas = [d for s, k, d in zip(spots, strikes, oracle_deltas, strict=True)
                      if s == 100.0 and k == 100.0]

        assert itm_deltas and atm_deltas, "Could not find ITM/ATM rows"
        assert min(itm_deltas) > max(atm_deltas) - 0.05, (
            f"Expected deep-ITM delta > ATM delta: ITM_min={min(itm_deltas):.4f}, ATM_max={max(atm_deltas):.4f}"
        )
