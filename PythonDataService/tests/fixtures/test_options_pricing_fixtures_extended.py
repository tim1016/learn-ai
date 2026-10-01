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
    def test_gamma_matches_oracle(self) -> None:
        inp, out, atol, rtol = _load("BS-004")
        greeks = _canonical_greeks(inp)
        oracle = [out["oracle_gamma"][i].as_py() for i in range(len(out))]
        canonical = [g.gamma for g in greeks]
        np.testing.assert_allclose(canonical, oracle, atol=atol, rtol=rtol)


# ---------------------------------------------------------------------------
# BS-005: Theta
# ---------------------------------------------------------------------------


class TestBS005Theta:
    def test_theta_matches_oracle(self) -> None:
        inp, out, atol, rtol = _load("BS-005")
        greeks = _canonical_greeks(inp)
        oracle = [out["oracle_theta"][i].as_py() for i in range(len(out))]
        canonical = [g.theta for g in greeks]
        np.testing.assert_allclose(canonical, oracle, atol=atol, rtol=rtol)


# ---------------------------------------------------------------------------
# BS-006: Vega
# ---------------------------------------------------------------------------


class TestBS006Vega:
    def test_vega_matches_oracle(self) -> None:
        inp, out, atol, rtol = _load("BS-006")
        greeks = _canonical_greeks(inp)
        oracle = [out["oracle_vega"][i].as_py() for i in range(len(out))]
        canonical = [g.vega for g in greeks]
        np.testing.assert_allclose(canonical, oracle, atol=atol, rtol=rtol)


# ---------------------------------------------------------------------------
# BS-007: Rho
# ---------------------------------------------------------------------------


class TestBS007Rho:
    def test_rho_matches_oracle(self) -> None:
        inp, out, atol, rtol = _load("BS-007")
        greeks = _canonical_greeks(inp)
        oracle = [out["oracle_rho"][i].as_py() for i in range(len(out))]
        canonical = [g.rho for g in greeks]
        np.testing.assert_allclose(canonical, oracle, atol=atol, rtol=rtol)
