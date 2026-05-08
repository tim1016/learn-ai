"""Golden fixture validation for extended options-pricing fixtures (BS-004 through BS-007).

Tests that the canonical black_scholes_greeks function matches the py_vollib
oracle stored in each fixture, at the tolerance pinned in manifest.json.

Includes structural bounds checks for each Greek.

Run in isolation (no FastAPI app needed):
  python -m pytest tests/fixtures/test_options_pricing_fixtures_extended.py -v --noconftest
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pyarrow as pa

_SVC_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_SVC_ROOT))

from golden_support.registry import default as registry  # noqa: E402

from app.services.bs_greeks import black_scholes_greeks  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load(fixture_id: str) -> tuple[pa.Table, pa.Table, float, float]:
    files = registry.active_files(fixture_id)
    fixture_dir = registry.fixture_dir(fixture_id)
    manifest_fixture = registry._manifest.by_id(fixture_id)
    inp = pa.ipc.open_file(fixture_dir / files.input).read_all()
    out = pa.ipc.open_file(fixture_dir / files.output).read_all()
    atol = manifest_fixture.tolerance.atol
    rtol = manifest_fixture.tolerance.rtol
    return inp, out, atol, rtol


def _canonical_greeks(inp: pa.Table):
    return [
        black_scholes_greeks(
            float(inp["spot"][i].as_py()),
            float(inp["strike"][i].as_py()),
            float(inp["ttm_years"][i].as_py()),
            float(inp["volatility"][i].as_py()),
            float(inp["rate"][i].as_py()),
            float(inp["dividend"][i].as_py()),
            bool(inp["is_call"][i].as_py()),
        )
        for i in range(len(inp))
    ]


# ---------------------------------------------------------------------------
# BS-004: Gamma
# ---------------------------------------------------------------------------


class TestBS004Gamma:
    def test_row_count(self) -> None:
        inp, _out, _atol, _rtol = _load("BS-004")
        assert len(inp) == 180

    def test_gamma_matches_oracle(self) -> None:
        inp, out, atol, rtol = _load("BS-004")
        greeks = _canonical_greeks(inp)
        oracle = [out["oracle_gamma"][i].as_py() for i in range(len(out))]
        canonical = [g.gamma for g in greeks]
        np.testing.assert_allclose(canonical, oracle, atol=atol, rtol=rtol)

    def test_gamma_positive(self) -> None:
        inp, _out, _atol, _rtol = _load("BS-004")
        greeks = _canonical_greeks(inp)
        for i, g in enumerate(greeks):
            assert g.gamma > 0, f"Row {i}: gamma={g.gamma} must be positive"

    def test_gamma_higher_near_atm(self) -> None:
        """ATM gamma should exceed deep ITM gamma for same vol/ttm."""
        inp, _out, _atol, _rtol = _load("BS-004")
        greeks = _canonical_greeks(inp)
        # Find ATM (S=100, K=100) and deep ITM (S=120, K=90) with same ttm/vol
        atm_rows = [
            i for i in range(len(inp))
            if inp["spot"][i].as_py() == 100.0
            and inp["strike"][i].as_py() == 100.0
        ]
        itm_rows = [
            i for i in range(len(inp))
            if inp["spot"][i].as_py() == 120.0
            and inp["strike"][i].as_py() == 90.0
        ]
        assert atm_rows and itm_rows
        avg_atm = sum(greeks[i].gamma for i in atm_rows) / len(atm_rows)
        avg_itm = sum(greeks[i].gamma for i in itm_rows) / len(itm_rows)
        assert avg_atm > avg_itm, "ATM gamma should exceed deep-ITM gamma"


# ---------------------------------------------------------------------------
# BS-005: Theta
# ---------------------------------------------------------------------------


class TestBS005Theta:
    def test_row_count(self) -> None:
        inp, _out, _atol, _rtol = _load("BS-005")
        assert len(inp) == 180

    def test_theta_matches_oracle(self) -> None:
        inp, out, atol, rtol = _load("BS-005")
        greeks = _canonical_greeks(inp)
        oracle = [out["oracle_theta"][i].as_py() for i in range(len(out))]
        canonical = [g.theta for g in greeks]
        np.testing.assert_allclose(canonical, oracle, atol=atol, rtol=rtol)

    def test_theta_negative_for_calls(self) -> None:
        """Call theta is negative (time decay costs the long holder)."""
        inp, _out, _atol, _rtol = _load("BS-005")
        greeks = _canonical_greeks(inp)
        for i, g in enumerate(greeks):
            assert g.theta < 0, f"Row {i}: call theta={g.theta} should be negative"

    def test_theta_larger_magnitude_near_atm(self) -> None:
        """ATM theta magnitude should exceed deep OTM theta magnitude."""
        inp, _out, _atol, _rtol = _load("BS-005")
        greeks = _canonical_greeks(inp)
        atm_rows = [
            i for i in range(len(inp))
            if inp["spot"][i].as_py() == 100.0
            and inp["strike"][i].as_py() == 100.0
        ]
        otm_rows = [
            i for i in range(len(inp))
            if inp["spot"][i].as_py() == 80.0
            and inp["strike"][i].as_py() == 110.0
        ]
        assert atm_rows and otm_rows
        avg_atm = sum(abs(greeks[i].theta) for i in atm_rows) / len(atm_rows)
        avg_otm = sum(abs(greeks[i].theta) for i in otm_rows) / len(otm_rows)
        assert avg_atm > avg_otm, "ATM |theta| should exceed deep-OTM |theta|"


# ---------------------------------------------------------------------------
# BS-006: Vega
# ---------------------------------------------------------------------------


class TestBS006Vega:
    def test_row_count(self) -> None:
        inp, _out, _atol, _rtol = _load("BS-006")
        assert len(inp) == 180

    def test_vega_matches_oracle(self) -> None:
        inp, out, atol, rtol = _load("BS-006")
        greeks = _canonical_greeks(inp)
        oracle = [out["oracle_vega"][i].as_py() for i in range(len(out))]
        canonical = [g.vega for g in greeks]
        np.testing.assert_allclose(canonical, oracle, atol=atol, rtol=rtol)

    def test_vega_positive(self) -> None:
        """Long call vega is positive (higher vol increases option value)."""
        inp, _out, _atol, _rtol = _load("BS-006")
        greeks = _canonical_greeks(inp)
        for i, g in enumerate(greeks):
            assert g.vega > 0, f"Row {i}: vega={g.vega} must be positive"

    def test_vega_higher_longer_ttm(self) -> None:
        """Longer-dated ATM options have higher vega."""
        inp, _out, _atol, _rtol = _load("BS-006")
        greeks = _canonical_greeks(inp)
        short_rows = [
            i for i in range(len(inp))
            if inp["spot"][i].as_py() == 100.0
            and inp["strike"][i].as_py() == 100.0
            and abs(inp["ttm_years"][i].as_py() - 1 / 12) < 1e-9
        ]
        long_rows = [
            i for i in range(len(inp))
            if inp["spot"][i].as_py() == 100.0
            and inp["strike"][i].as_py() == 100.0
            and abs(inp["ttm_years"][i].as_py() - 1.0) < 1e-9
        ]
        assert short_rows and long_rows
        avg_short = sum(greeks[i].vega for i in short_rows) / len(short_rows)
        avg_long = sum(greeks[i].vega for i in long_rows) / len(long_rows)
        assert avg_long > avg_short, "1-year ATM vega should exceed 1-month ATM vega"


# ---------------------------------------------------------------------------
# BS-007: Rho
# ---------------------------------------------------------------------------


class TestBS007Rho:
    def test_row_count(self) -> None:
        inp, _out, _atol, _rtol = _load("BS-007")
        assert len(inp) == 180

    def test_rho_matches_oracle(self) -> None:
        inp, out, atol, rtol = _load("BS-007")
        greeks = _canonical_greeks(inp)
        oracle = [out["oracle_rho"][i].as_py() for i in range(len(out))]
        canonical = [g.rho for g in greeks]
        np.testing.assert_allclose(canonical, oracle, atol=atol, rtol=rtol)

    def test_call_rho_positive(self) -> None:
        """Call rho is positive (higher rates benefit long calls via forward price)."""
        inp, _out, _atol, _rtol = _load("BS-007")
        greeks = _canonical_greeks(inp)
        for i, g in enumerate(greeks):
            assert g.rho > 0, f"Row {i}: call rho={g.rho} should be positive"

    def test_rho_higher_itm(self) -> None:
        """Deep ITM calls have higher rho than OTM calls (more equity-like)."""
        inp, _out, _atol, _rtol = _load("BS-007")
        greeks = _canonical_greeks(inp)
        itm_rows = [
            i for i in range(len(inp))
            if inp["spot"][i].as_py() == 120.0
            and inp["strike"][i].as_py() == 90.0
        ]
        otm_rows = [
            i for i in range(len(inp))
            if inp["spot"][i].as_py() == 80.0
            and inp["strike"][i].as_py() == 110.0
        ]
        assert itm_rows and otm_rows
        avg_itm = sum(greeks[i].rho for i in itm_rows) / len(itm_rows)
        avg_otm = sum(greeks[i].rho for i in otm_rows) / len(otm_rows)
        assert avg_itm > avg_otm, "Deep ITM call rho should exceed OTM call rho"
