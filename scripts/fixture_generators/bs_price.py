"""Generators for BS-001 (call price) and BS-002 (put price) golden fixtures.

Oracle: py_vollib==1.0.1 (GPL — test/generation-only; never imported in app/).
  py_vollib.black_scholes.black_scholes(flag, S, K, t, r, sigma)
  flag: 'c' for call, 'p' for put

Our canonical: app/services/bs_greeks.py::bs_european_price(spot, strike,
  ttm_years, rate, volatility, is_call, dividend=0.0)

Unit check (2026-05-08): py_vollib and bs_european_price use identical
units and produce values that agree at atol < 4e-15 across the test grid.
No unit conversion is applied — oracle values are stored verbatim.

QuantLib canary for BS-001: the existing cross-engine parity fixture
test_bs_cross_engine_parity.py (360-case grid at atol=1e-10) is the
canonical proof that our closed-form matches QuantLib. This fixture's
purpose is to pin our canonical against py_vollib independently.

Input grid (180 cases per fixture):
  spot:  [80, 90, 100, 110, 120]
  strike: [90, 100, 110]
  ttm_years: [1/12, 0.25, 0.5, 1.0]   (1 month, 3 months, 6 months, 1 year)
  rate:  [0.05]
  vol:   [0.15, 0.20, 0.30]
  dividend: 0.0
  is_call: True (BS-001), False (BS-002)
"""
from __future__ import annotations

import sys
from pathlib import Path
from datetime import date

import pyarrow as pa

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "PythonDataService" / "tests" / "fixtures"))
sys.path.insert(0, str(REPO_ROOT / "PythonDataService"))

from golden_support.hashing import compute_hashes
from golden_support.io import write_arrow


# ── Grid definition ───────────────────────────────────────────────────────────

SPOTS = [80.0, 90.0, 100.0, 110.0, 120.0]
STRIKES = [90.0, 100.0, 110.0]
TTMS = [1.0 / 12.0, 0.25, 0.5, 1.0]
RATES = [0.05]
VOLS = [0.15, 0.20, 0.30]
DIVIDEND = 0.0

def _generation_date() -> str:
    return date.today().isoformat()
ORACLE_LIB = "py_vollib==1.0.1"
ORACLE_CITATION = (
    "py_vollib.black_scholes.black_scholes(flag, S, K, t, r, sigma); "
    "flag='c' for call. GPL-licensed library — test/generation-only."
)


def _build_grid(is_call: bool) -> tuple[pa.Table, pa.Table]:
    """Build (input_table, output_table) for a call or put grid."""
    from py_vollib.black_scholes import black_scholes as pv_bs

    flag = "c" if is_call else "p"

    spots, strikes, ttms, rates, vols = [], [], [], [], []
    oracle_prices = []

    for S in SPOTS:
        for K in STRIKES:
            for t in TTMS:
                for r in RATES:
                    for sigma in VOLS:
                        spots.append(S)
                        strikes.append(K)
                        ttms.append(t)
                        rates.append(r)
                        vols.append(sigma)
                        oracle_prices.append(pv_bs(flag, S, K, t, r, sigma))

    input_table = pa.table(
        {
            "spot": pa.array(spots, type=pa.float64()),
            "strike": pa.array(strikes, type=pa.float64()),
            "ttm_years": pa.array(ttms, type=pa.float64()),
            "rate": pa.array(rates, type=pa.float64()),
            "volatility": pa.array(vols, type=pa.float64()),
            "dividend": pa.array([DIVIDEND] * len(spots), type=pa.float64()),
            "is_call": pa.array([is_call] * len(spots), type=pa.bool_()),
        }
    )

    output_table = pa.table(
        {
            "oracle_price": pa.array(oracle_prices, type=pa.float64()),
        }
    )

    return input_table, output_table


def _write_attribution(path: Path, fixture_id: str, is_call: bool, justification: str) -> None:
    flag_name = "call" if is_call else "put"
    path.write_text(
        f"""# {fixture_id} — Black-Scholes European {flag_name.title()} Price

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. 180-case grid of
(spot, strike, ttm_years, rate, vol) spanning OTM/ATM/ITM and short/long
maturities. No real market data. See `bs_price.py::SPOTS/STRIKES/TTMS/VOLS`.

**Layer 2 — Methodology provenance:** Hull, *Options Futures and Other
Derivatives* (10e), §15.8, equations 15.20/15.21 (continuous dividend yield).
Canonical implementation: `PythonDataService/app/services/bs_greeks.py::bs_european_price`.

**Layer 3 — Independent numerical oracle:** {ORACLE_LIB}.
`py_vollib.black_scholes.black_scholes(flag='{flag_name[0]}', S, K, t, r, sigma)`.
GPL-licensed. Used here in fixture generation only — never imported in app/.

## Formula

C = S·e^(-qT)·N(d1) - K·e^(-rT)·N(d2)   (call)
P = K·e^(-rT)·N(-d2) - S·e^(-qT)·N(-d1) (put)

where d1 = [ln(S/K) + (r - q + σ²/2)·T] / (σ·√T)
      d2 = d1 - σ·√T

## Canonical Implementation

`PythonDataService/app/services/bs_greeks.py::bs_european_price`
Signature: `bs_european_price(spot, strike, ttm_years, rate, volatility, is_call, dividend=0.0)`

## Oracle

Library: {ORACLE_LIB}
Citation: {ORACLE_CITATION}

Unit agreement check (2026-05-08): both py_vollib and our canonical return
dollar price with identical precision — no unit conversion required.

## QuantLib Cross-Check

The existing `test_bs_cross_engine_parity.py` (360-case grid, atol=1e-10)
proves our canonical matches QuantLib. It is the QuantLib canary for
BS-001. Separate QuantLib values are not stored in this fixture to avoid
date-arithmetic TTM rounding (QuantLib uses serial-day resolution;
bs_european_price uses continuous float TTM).

## Tolerance

atol=1e-10, rtol=0.0

Rationale: Cross-library comparison between scipy-based closed form and
py_vollib. Both use IEEE 754 float64 arithmetic on the same BSM formula.
Observed max abs error across the 180-case grid is < 1e-14.
Floor set to 1e-10 per conventions.py (1e-12 excluded due to CI/dev
platform divergence for transcendental functions).

## Input Grid

{len(SPOTS) * len(STRIKES) * len(TTMS) * len(RATES) * len(VOLS)} cases.
  spot: {SPOTS}
  strike: {STRIKES}
  ttm_years: {TTMS}
  rate: {RATES}
  vol: {VOLS}
  dividend: {DIVIDEND}
  is_call: {is_call}

## Known Limitations

- Zero-dividend only (q=0). Non-zero dividend covered in a future fixture.
- No near-expiry or zero-vol edge cases (those are tested in the solver tests).
- QuantLib date-arithmetic rounding not captured in this fixture.

## Regeneration

  python scripts/generate_fixtures.py --id {fixture_id} --force \\
    --justification "<reason>"

Then promote by editing manifest.json active_version.
Verify: python -m pytest PythonDataService/tests/fixtures/test_options_pricing_fixtures.py -v

## Generation Metadata

Generated: {_generation_date()}
Oracle version: {ORACLE_LIB}
Script: scripts/fixture_generators/bs_price.py
{'Justification for regeneration: ' + justification if justification else '(initial generation)'}
""",
        encoding="utf-8",
    )


def _write_fixture(
    version_dir: Path,
    fixture_id: str,
    is_call: bool,
    justification: str,
) -> None:
    input_table, output_table = _build_grid(is_call)

    input_path = version_dir / "input.arrow"
    output_path = version_dir / "output.arrow"
    attribution_path = version_dir / "attribution.md"

    write_arrow(input_table, input_path)
    write_arrow(output_table, output_path)
    _write_attribution(attribution_path, fixture_id, is_call, justification)

    content_hashes, file_hashes = compute_hashes(
        version_dir, ["input.arrow", "output.arrow"]
    )

    print(f"  {fixture_id}: {len(input_table)} cases")
    print(f"  content_sha256[input.arrow]:  {content_hashes['input.arrow']}")
    print(f"  content_sha256[output.arrow]: {content_hashes['output.arrow']}")
    print(f"  file_sha256[input.arrow]:     {file_hashes['input.arrow']}")
    print(f"  file_sha256[output.arrow]:    {file_hashes['output.arrow']}")
    print()
    print("  Paste into manifest.json versions entry:")
    print(f"""  {{
    "input": "input.arrow",
    "output": "output.arrow",
    "attribution": "attribution.md",
    "content_sha256": {{
      "input.arrow": "{content_hashes['input.arrow']}",
      "output.arrow": "{content_hashes['output.arrow']}"
    }},
    "file_sha256": {{
      "input.arrow": "{file_hashes['input.arrow']}",
      "output.arrow": "{file_hashes['output.arrow']}"
    }}
  }}""")


def generate_bs001(version_dir: Path, justification: str = "") -> None:
    """Generate BS-001: call price fixture."""
    _write_fixture(version_dir, "BS-001", is_call=True, justification=justification)


def generate_bs002(version_dir: Path, justification: str = "") -> None:
    """Generate BS-002: put price fixture."""
    _write_fixture(version_dir, "BS-002", is_call=False, justification=justification)
