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


# ---------------------------------------------------------------------------
# BS-003: Call Delta
# ---------------------------------------------------------------------------

class TestBS003CallDelta:
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
