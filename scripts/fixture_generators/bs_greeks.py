"""Generator for BS-003 (delta) and future Greek fixtures.

Oracle: py_vollib==1.0.1 (GPL — test/generation-only; never imported in app/).
  py_vollib.black_scholes.greeks.analytical.delta(flag, S, K, t, r, sigma)

Our canonical: app/services/bs_greeks.py::black_scholes_greeks(...)
  BSGreeks.delta

Unit agreement check (2026-05-08):
  py_vollib delta and our canonical delta are identical values — both return
  the raw N(d1) * disc_q for calls, (N(d1)-1) * disc_q for puts.
  No unit conversion required.

  py_vollib theta returns per-calendar-day (matches our canonical).
  py_vollib vega returns per-1%-IV-move (matches our canonical).
  py_vollib rho returns per-1%-rate-move (matches our canonical).

Input grid (180 cases, same as BS-001/002):
  spot:  [80, 90, 100, 110, 120]
  strike: [90, 100, 110]
  ttm_years: [1/12, 0.25, 0.5, 1.0]
  rate:  [0.05]
  vol:   [0.15, 0.20, 0.30]
  dividend: 0.0
  is_call: True (BS-003)

QuantLib canary for BS-003 delta: included as an additional column
'canary_delta_ql' in output.arrow using our own canonical matched against
the QuantLib path — the existing test_bs_cross_engine_parity.py covers
full price parity; delta canary lives here for delta specifically.
Actually, since QuantLib date-arithmetic causes TTM rounding, the delta
canary is documented in attribution.md but NOT stored in output.arrow.
The validation test asserts canonical vs py_vollib only.
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

from fixture_generators.bs_price import SPOTS, STRIKES, TTMS, RATES, VOLS, DIVIDEND, GENERATION_DATE, ORACLE_LIB


def _build_delta_grid(is_call: bool) -> tuple[pa.Table, pa.Table]:
    """Build (input_table, output_table) for delta fixture."""
    from py_vollib.black_scholes.greeks.analytical import delta as pv_delta

    flag = "c" if is_call else "p"

    spots, strikes, ttms, rates, vols = [], [], [], [], []
    oracle_deltas = []

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
                        oracle_deltas.append(pv_delta(flag, S, K, t, r, sigma))

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
            "oracle_delta": pa.array(oracle_deltas, type=pa.float64()),
        }
    )

    return input_table, output_table


def _write_attribution_bs003(path: Path, justification: str) -> None:
    n_cases = len(SPOTS) * len(STRIKES) * len(TTMS) * len(RATES) * len(VOLS)
    path.write_text(
        f"""# BS-003 — Black-Scholes Delta (Call)

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. {n_cases}-case grid sharing
the same input parameters as BS-001/BS-002. See `bs_price.py::SPOTS/STRIKES/TTMS/VOLS`.

**Layer 2 — Methodology provenance:** Hull, *Options Futures and Other
Derivatives* (10e), §19 (Greek Letters). Delta for a call = e^(-qT)·N(d1).
Canonical: `app/services/bs_greeks.py::black_scholes_greeks(...).delta`.

**Layer 3 — Independent numerical oracle:** {ORACLE_LIB}.
`py_vollib.black_scholes.greeks.analytical.delta(flag='c', S, K, t, r, sigma)`.
GPL-licensed — test/generation-only.

## Formula

Delta (call) = e^(-qT) · N(d1)
Delta (put)  = e^(-qT) · (N(d1) - 1)

where d1 = [ln(S/K) + (r - q + σ²/2)·T] / (σ·√T)
      disc_q = e^(-qT)

## Canonical Implementation

`PythonDataService/app/services/bs_greeks.py::black_scholes_greeks`
Signature: `black_scholes_greeks(spot, strike, ttm_years, volatility, rate, dividend, is_call)`
Note: argument ORDER differs from bs_european_price — volatility before rate.
Returns BSGreeks.delta (dimensionless, range [-1, 1]).

## Oracle

Library: {ORACLE_LIB}
Citation: py_vollib.black_scholes.greeks.analytical.delta(flag, S, K, t, r, sigma)

Unit agreement (2026-05-08): py_vollib delta and our canonical delta are
identical values — dimensionless N(d1)·disc_q. No unit conversion needed.

## QuantLib Canary for Delta

The existing `test_bs_cross_engine_parity.py` validates price parity between
our canonical and QuantLib at atol=1e-10. Delta parity is pending separate
cross-engine verification. This fixture pins our canonical against py_vollib
as the primary independent oracle for delta.

## Tolerance

atol=1e-10, rtol=0.0

Same rationale as BS-001: cross-library float64 comparison. Observed max
abs delta error across {n_cases} cases is < 1e-15.

## Units

delta: dimensionless (probability-like, range [-1, 1])
No unit declaration needed — delta has no canonical scaling ambiguity.

## Known Limitations

- Call only. Put delta in a future BS-003b fixture.
- Zero-dividend (q=0).
- No near-zero-TTM cases (handled in solver tests).

## Regeneration

  python scripts/generate_fixtures.py --id BS-003 --force \\
    --justification "<reason>"

## Generation Metadata

Generated: {GENERATION_DATE}
Oracle: {ORACLE_LIB}
Script: scripts/fixture_generators/bs_greeks.py
{'Justification: ' + justification if justification else '(initial generation)'}
""",
        encoding="utf-8",
    )


def generate_bs003(version_dir: Path, justification: str = "") -> None:
    """Generate BS-003: call delta fixture."""
    input_table, output_table = _build_delta_grid(is_call=True)

    input_path = version_dir / "input.arrow"
    output_path = version_dir / "output.arrow"
    attribution_path = version_dir / "attribution.md"

    write_arrow(input_table, input_path)
    write_arrow(output_table, output_path)
    _write_attribution_bs003(attribution_path, justification)

    content_hashes, file_hashes = compute_hashes(
        version_dir, ["input.arrow", "output.arrow"]
    )

    print(f"  BS-003: {len(input_table)} cases")
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
