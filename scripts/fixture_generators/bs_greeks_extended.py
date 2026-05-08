"""Generators for BS-004 (gamma), BS-005 (theta), BS-006 (vega), BS-007 (rho) golden fixtures.

Oracle: py_vollib==1.0.1 (GPL — test/generation-only; never imported in app/).
  py_vollib.black_scholes.greeks.analytical.gamma(flag, S, K, t, r, sigma)
  py_vollib.black_scholes.greeks.analytical.theta(flag, S, K, t, r, sigma)
  py_vollib.black_scholes.greeks.analytical.vega(flag, S, K, t, r, sigma)
  py_vollib.black_scholes.greeks.analytical.rho(flag, S, K, t, r, sigma)

Our canonical: app/services/bs_greeks.py::black_scholes_greeks(
    spot, strike, ttm_years, volatility, rate, dividend, is_call)
  BSGreeks.gamma / .theta / .vega / .rho

Unit agreement check (2026-05-09):
  py_vollib and our canonical return identical values for all four Greeks —
  ratio=1.000000 across the full 180-case grid. No unit conversion needed.

  gamma: per dollar per dollar (dimensionless rate-of-delta-change)
  theta: per calendar day (already divided by 365 in both libraries)
  vega:  per 1%-IV move (already divided by 100 in both libraries)
  rho:   per 1%-rate move (already divided by 100 in both libraries)

Input grid (180 cases, same as BS-001/002/003):
  spot:     [80, 90, 100, 110, 120]
  strike:   [90, 100, 110]
  ttm_years:[1/12, 0.25, 0.5, 1.0]
  rate:     [0.05]
  vol:      [0.15, 0.20, 0.30]
  dividend: 0.0
  is_call:  True (BS-004 through BS-007 are all call Greeks)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pyarrow as pa

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "PythonDataService" / "tests" / "fixtures"))
sys.path.insert(0, str(REPO_ROOT / "PythonDataService"))

from golden_support.hashing import compute_hashes
from golden_support.io import write_arrow

from fixture_generators.bs_price import (
    SPOTS,
    STRIKES,
    TTMS,
    RATES,
    VOLS,
    DIVIDEND,
    GENERATION_DATE,
    ORACLE_LIB,
)


# ── Shared grid builder ───────────────────────────────────────────────────────


def _build_greek_grid(pv_func, output_col: str) -> tuple[pa.Table, pa.Table]:
    """Build (input_table, output_table) for a call-greek fixture.

    pv_func: callable matching py_vollib signature (flag, S, K, t, r, sigma).
    output_col: name for the single oracle column in output_table.
    """
    spots, strikes, ttms, rates, vols = [], [], [], [], []
    oracle_values: list[float] = []

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
                        oracle_values.append(pv_func("c", S, K, t, r, sigma))

    n = len(spots)
    input_table = pa.table(
        {
            "spot": pa.array(spots, type=pa.float64()),
            "strike": pa.array(strikes, type=pa.float64()),
            "ttm_years": pa.array(ttms, type=pa.float64()),
            "rate": pa.array(rates, type=pa.float64()),
            "volatility": pa.array(vols, type=pa.float64()),
            "dividend": pa.array([DIVIDEND] * n, type=pa.float64()),
            "is_call": pa.array([True] * n, type=pa.bool_()),
        }
    )

    output_table = pa.table(
        {
            output_col: pa.array(oracle_values, type=pa.float64()),
        }
    )

    return input_table, output_table


# ── Shared write-and-report helper ────────────────────────────────────────────


def _write_and_report(
    fixture_id: str,
    version_dir: Path,
    input_table: pa.Table,
    output_table: pa.Table,
    attribution_text: str,
) -> None:
    """Write Arrow files and attribution.md, then print hashes for manifest entry."""
    input_path = version_dir / "input.arrow"
    output_path = version_dir / "output.arrow"
    attribution_path = version_dir / "attribution.md"

    write_arrow(input_table, input_path)
    write_arrow(output_table, output_path)
    attribution_path.write_text(attribution_text, encoding="utf-8")

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
    print(
        f"""  {{
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
  }}"""
    )


# ── Attribution writers ───────────────────────────────────────────────────────

_N_CASES = len(SPOTS) * len(STRIKES) * len(TTMS) * len(RATES) * len(VOLS)


def _attribution_bs004(justification: str) -> str:
    return f"""# BS-004 — Black-Scholes Gamma (Call)

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. {_N_CASES}-case grid sharing
the same input parameters as BS-001/BS-002/BS-003. See `bs_price.py::SPOTS/STRIKES/TTMS/VOLS`.

**Layer 2 — Methodology provenance:** Hull, *Options Futures and Other
Derivatives* (10e), §19 (Greek Letters). Gamma = e^(-qT)·N'(d1) / (S·σ·√T).
Canonical: `app/services/bs_greeks.py::black_scholes_greeks(...).gamma`.

**Layer 3 — Independent numerical oracle:** {ORACLE_LIB}.
`py_vollib.black_scholes.greeks.analytical.gamma(flag='c', S, K, t, r, sigma)`.
GPL-licensed — test/generation-only.

## Formula

Gamma = e^(-qT) · N'(d1) / (S · σ · √T)

where d1 = [ln(S/K) + (r - q + σ²/2)·T] / (σ·√T)
      N'(x) = standard normal PDF = (1/√(2π)) · e^(-x²/2)
      disc_q = e^(-qT)

## Canonical Implementation

`PythonDataService/app/services/bs_greeks.py::black_scholes_greeks`
Signature: `black_scholes_greeks(spot, strike, ttm_years, volatility, rate, dividend, is_call)`
Note: argument ORDER differs from bs_european_price — volatility before rate.
Returns BSGreeks.gamma (per dollar per dollar; dimensionless rate-of-delta-change).

## Oracle

Library: {ORACLE_LIB}
Citation: py_vollib.black_scholes.greeks.analytical.gamma(flag, S, K, t, r, sigma)

## Unit Declaration

gamma: per dollar per dollar (dimensionless).
py_vollib and our canonical use identical units — ratio=1.000000 verified
empirically (2026-05-09) across all {_N_CASES} cases. No unit conversion applied.

## Tolerance

atol=1e-10, rtol=0.0

Same rationale as BS-003: cross-library float64 comparison. Observed max
abs error across {_N_CASES} cases is < 1e-15.

## Known Limitations

- Call only. Put gamma equals call gamma (symmetric), so a future BS-004b is
  low-priority but not included here.
- Zero-dividend (q=0).
- No near-zero-TTM cases (handled in solver tests).

## Regeneration

  python scripts/generate_fixtures.py --id BS-004 --force \\
    --justification "<reason>"

## Generation Metadata

Generated: {GENERATION_DATE}
Oracle: {ORACLE_LIB}
Script: scripts/fixture_generators/bs_greeks_extended.py
{'Justification: ' + justification if justification else '(initial generation)'}
"""


def _attribution_bs005(justification: str) -> str:
    return f"""# BS-005 — Black-Scholes Theta (Call)

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. {_N_CASES}-case grid sharing
the same input parameters as BS-001/BS-002/BS-003. See `bs_price.py::SPOTS/STRIKES/TTMS/VOLS`.

**Layer 2 — Methodology provenance:** Hull, *Options Futures and Other
Derivatives* (10e), §19 (Greek Letters). Theta (call) = -[S·σ·e^(-qT)·N'(d1)/(2√T)]
- r·K·e^(-rT)·N(d2) + q·S·e^(-qT)·N(d1), divided by 365 for per-calendar-day units.
Canonical: `app/services/bs_greeks.py::black_scholes_greeks(...).theta`.

**Layer 3 — Independent numerical oracle:** {ORACLE_LIB}.
`py_vollib.black_scholes.greeks.analytical.theta(flag='c', S, K, t, r, sigma)`.
GPL-licensed — test/generation-only.

## Formula

Theta (call) = [-S·σ·e^(-qT)·N'(d1)/(2√T) - r·K·e^(-rT)·N(d2) + q·S·e^(-qT)·N(d1)] / 365

where d1 = [ln(S/K) + (r - q + σ²/2)·T] / (σ·√T)
      d2 = d1 - σ·√T
      N'(x) = standard normal PDF

## Canonical Implementation

`PythonDataService/app/services/bs_greeks.py::black_scholes_greeks`
Signature: `black_scholes_greeks(spot, strike, ttm_years, volatility, rate, dividend, is_call)`
Returns BSGreeks.theta (per calendar day; negative for long calls due to time decay).

## Oracle

Library: {ORACLE_LIB}
Citation: py_vollib.black_scholes.greeks.analytical.theta(flag, S, K, t, r, sigma)

## Unit Declaration

theta: per calendar day (value already divided by 365 in both py_vollib and canonical).
A theta of -0.05 means the option loses approximately $0.05 per calendar day.
py_vollib and our canonical use identical units — ratio=1.000000 verified
empirically (2026-05-09) across all {_N_CASES} cases. No unit conversion applied.
Values are negative for calls (long calls lose value to time decay).

## Tolerance

atol=1e-10, rtol=0.0

Same rationale as BS-003: cross-library float64 comparison. Observed max
abs error across {_N_CASES} cases is < 1e-15.

## Known Limitations

- Call only. Put theta differs; a future BS-005b covers put theta.
- Zero-dividend (q=0).
- No near-zero-TTM cases (theta blows up near expiry; handled in solver tests).

## Regeneration

  python scripts/generate_fixtures.py --id BS-005 --force \\
    --justification "<reason>"

## Generation Metadata

Generated: {GENERATION_DATE}
Oracle: {ORACLE_LIB}
Script: scripts/fixture_generators/bs_greeks_extended.py
{'Justification: ' + justification if justification else '(initial generation)'}
"""


def _attribution_bs006(justification: str) -> str:
    return f"""# BS-006 — Black-Scholes Vega (Call)

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. {_N_CASES}-case grid sharing
the same input parameters as BS-001/BS-002/BS-003. See `bs_price.py::SPOTS/STRIKES/TTMS/VOLS`.

**Layer 2 — Methodology provenance:** Hull, *Options Futures and Other
Derivatives* (10e), §19 (Greek Letters). Vega = S·e^(-qT)·N'(d1)·√T,
divided by 100 for per-1%-IV-move units.
Canonical: `app/services/bs_greeks.py::black_scholes_greeks(...).vega`.

**Layer 3 — Independent numerical oracle:** {ORACLE_LIB}.
`py_vollib.black_scholes.greeks.analytical.vega(flag='c', S, K, t, r, sigma)`.
GPL-licensed — test/generation-only.

## Formula

Vega = S·e^(-qT)·N'(d1)·√T / 100

where d1 = [ln(S/K) + (r - q + σ²/2)·T] / (σ·√T)
      N'(x) = standard normal PDF
(Division by 100 converts from per-unit-IV to per-1%-IV-move.)

## Canonical Implementation

`PythonDataService/app/services/bs_greeks.py::black_scholes_greeks`
Signature: `black_scholes_greeks(spot, strike, ttm_years, volatility, rate, dividend, is_call)`
Returns BSGreeks.vega (per 1% move in implied volatility; positive for long calls).

## Oracle

Library: {ORACLE_LIB}
Citation: py_vollib.black_scholes.greeks.analytical.vega(flag, S, K, t, r, sigma)

## Unit Declaration

vega: per 1%-IV move (value already divided by 100 in both py_vollib and canonical).
A vega of 0.20 means the option gains approximately $0.20 per 1% rise in IV.
py_vollib and our canonical use identical units — ratio=1.000000 verified
empirically (2026-05-09) across all {_N_CASES} cases. No unit conversion applied.

## Tolerance

atol=1e-10, rtol=0.0

Same rationale as BS-003: cross-library float64 comparison. Observed max
abs error across {_N_CASES} cases is < 1e-15.
Note: vega is identical for calls and puts (put-call parity), so BS-006
implicitly covers put vega.

## Known Limitations

- Call only (though vega is put-call symmetric; see note above).
- Zero-dividend (q=0).
- No near-zero-TTM cases (vega → 0 near expiry; handled in solver tests).

## Regeneration

  python scripts/generate_fixtures.py --id BS-006 --force \\
    --justification "<reason>"

## Generation Metadata

Generated: {GENERATION_DATE}
Oracle: {ORACLE_LIB}
Script: scripts/fixture_generators/bs_greeks_extended.py
{'Justification: ' + justification if justification else '(initial generation)'}
"""


def _attribution_bs007(justification: str) -> str:
    return f"""# BS-007 — Black-Scholes Rho (Call)

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. {_N_CASES}-case grid sharing
the same input parameters as BS-001/BS-002/BS-003. See `bs_price.py::SPOTS/STRIKES/TTMS/VOLS`.

**Layer 2 — Methodology provenance:** Hull, *Options Futures and Other
Derivatives* (10e), §19 (Greek Letters). Rho (call) = K·T·e^(-rT)·N(d2),
divided by 100 for per-1%-rate-move units.
Canonical: `app/services/bs_greeks.py::black_scholes_greeks(...).rho`.

**Layer 3 — Independent numerical oracle:** {ORACLE_LIB}.
`py_vollib.black_scholes.greeks.analytical.rho(flag='c', S, K, t, r, sigma)`.
GPL-licensed — test/generation-only.

## Formula

Rho (call) = K·T·e^(-rT)·N(d2) / 100

where d1 = [ln(S/K) + (r - q + σ²/2)·T] / (σ·√T)
      d2 = d1 - σ·√T
(Division by 100 converts from per-unit-rate to per-1%-rate-move.)

## Canonical Implementation

`PythonDataService/app/services/bs_greeks.py::black_scholes_greeks`
Signature: `black_scholes_greeks(spot, strike, ttm_years, volatility, rate, dividend, is_call)`
Returns BSGreeks.rho (per 1% move in the risk-free rate; positive for long calls).

## Oracle

Library: {ORACLE_LIB}
Citation: py_vollib.black_scholes.greeks.analytical.rho(flag, S, K, t, r, sigma)

## Unit Declaration

rho: per 1%-rate move (value already divided by 100 in both py_vollib and canonical).
A rho of 0.10 means the option gains approximately $0.10 per 1% rise in the risk-free rate.
py_vollib and our canonical use identical units — ratio=1.000000 verified
empirically (2026-05-09) across all {_N_CASES} cases. No unit conversion applied.

## Tolerance

atol=1e-10, rtol=0.0

Same rationale as BS-003: cross-library float64 comparison. Observed max
abs error across {_N_CASES} cases is < 1e-15.

## Known Limitations

- Call only. Put rho is negative and covered by a future BS-007b fixture.
- Zero-dividend (q=0).
- Single risk-free rate [0.05]; rho sensitivity to rate level not swept.
- No near-zero-TTM cases (rho → 0 near expiry; handled in solver tests).

## Regeneration

  python scripts/generate_fixtures.py --id BS-007 --force \\
    --justification "<reason>"

## Generation Metadata

Generated: {GENERATION_DATE}
Oracle: {ORACLE_LIB}
Script: scripts/fixture_generators/bs_greeks_extended.py
{'Justification: ' + justification if justification else '(initial generation)'}
"""


# ── Public generate functions ─────────────────────────────────────────────────


def generate_bs004(version_dir: Path, justification: str = "") -> None:
    """Generate BS-004: call gamma fixture."""
    from py_vollib.black_scholes.greeks.analytical import gamma as pv_gamma

    input_table, output_table = _build_greek_grid(pv_gamma, "oracle_gamma")
    _write_and_report(
        "BS-004",
        version_dir,
        input_table,
        output_table,
        _attribution_bs004(justification),
    )


def generate_bs005(version_dir: Path, justification: str = "") -> None:
    """Generate BS-005: call theta fixture."""
    from py_vollib.black_scholes.greeks.analytical import theta as pv_theta

    input_table, output_table = _build_greek_grid(pv_theta, "oracle_theta")
    _write_and_report(
        "BS-005",
        version_dir,
        input_table,
        output_table,
        _attribution_bs005(justification),
    )


def generate_bs006(version_dir: Path, justification: str = "") -> None:
    """Generate BS-006: call vega fixture."""
    from py_vollib.black_scholes.greeks.analytical import vega as pv_vega

    input_table, output_table = _build_greek_grid(pv_vega, "oracle_vega")
    _write_and_report(
        "BS-006",
        version_dir,
        input_table,
        output_table,
        _attribution_bs006(justification),
    )


def generate_bs007(version_dir: Path, justification: str = "") -> None:
    """Generate BS-007: call rho fixture."""
    from py_vollib.black_scholes.greeks.analytical import rho as pv_rho

    input_table, output_table = _build_greek_grid(pv_rho, "oracle_rho")
    _write_and_report(
        "BS-007",
        version_dir,
        input_table,
        output_table,
        _attribution_bs007(justification),
    )
