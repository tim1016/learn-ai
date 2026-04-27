#!/usr/bin/env python3
"""Generate the frontend BS parity fixture.

See ``docs/architecture/iv-ownership-research.md`` §6 (tolerances and
validation) for the consolidated tolerance table.

Self-contained — uses only the Python stdlib (``math.erf``) so it can run
in any environment without depending on the PythonDataService venv.

Numerical equivalence:
``Frontend/scripts/generate-bs-parity-fixture.py`` (this file) and
``PythonDataService/app/services/bs_greeks.py::bs_european_price`` evaluate
the same closed-form Black-Scholes-Merton formula. Both use ``math.erf``
under the hood (scipy's ``norm.cdf`` is the same erf-based path), so for
any input tuple the outputs agree to last-bit machine precision. The
canonical Python pricer's parity against ``py_vollib`` is pinned at
``atol=1e-10`` in
``PythonDataService/tests/services/test_bs_cross_engine_parity.py``.

Tolerance on the frontend side is bounded by the Abramowitz & Stegun
7.1.26 normal-CDF approximation used in
``Frontend/src/app/utils/black-scholes.ts`` (``|error| < 1.5e-7``). For
options at S ~ 100, the BS price absorbs up to ~1.5e-5 of CDF error, so
the achievable price tolerance is roughly ``atol=1e-4``.

Run from anywhere:

    python3 Frontend/scripts/generate-bs-parity-fixture.py

Output (overwritten): ``Frontend/src/testing/bs-parity/grid.json``.

The fixture lives under ``src/testing/`` rather than a top-level
``test-fixtures/`` directory because the Angular build tooling (esbuild
via ``@angular/build:unit-test``) resolves spec imports relative to the
``src/`` tree only — a sibling top-level dir won't be picked up.
"""

from __future__ import annotations

import json
import math
from itertools import product
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUTPUT = HERE.parent / "src" / "testing" / "bs-parity" / "grid.json"

GRID = {
    "spot": [80.0, 100.0, 120.0],
    "strike": [100.0],
    "ttm_days": [7, 30, 90, 180, 365],
    "volatility": [0.10, 0.20, 0.40],
    "rate": [0.0, 0.05],
    "dividend": [0.0, 0.02],
    "option_type": ["call", "put"],
}


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_european_price(
    *,
    spot: float,
    strike: float,
    ttm_years: float,
    rate: float,
    volatility: float,
    is_call: bool,
    dividend: float = 0.0,
) -> float:
    """Closed-form BSM with continuous dividend yield. erf-based normal CDF.

    Matches ``PythonDataService/app/services/bs_greeks.bs_european_price``
    bit-for-bit (both compute on the same erf path).
    """
    if ttm_years <= 0 or volatility <= 0 or spot <= 0 or strike <= 0:
        intrinsic = max(spot - strike, 0.0) if is_call else max(strike - spot, 0.0)
        return intrinsic

    sigma_sqrt_t = volatility * math.sqrt(ttm_years)
    d1 = (math.log(spot / strike) + (rate - dividend + 0.5 * volatility ** 2) * ttm_years) / sigma_sqrt_t
    d2 = d1 - sigma_sqrt_t

    s_disc = spot * math.exp(-dividend * ttm_years)
    k_disc = strike * math.exp(-rate * ttm_years)

    if is_call:
        return s_disc * _norm_cdf(d1) - k_disc * _norm_cdf(d2)
    return k_disc * _norm_cdf(-d2) - s_disc * _norm_cdf(-d1)


def main() -> None:
    cases = []
    for spot, strike, ttm_days, vol, rate, q, opt in product(
        GRID["spot"],
        GRID["strike"],
        GRID["ttm_days"],
        GRID["volatility"],
        GRID["rate"],
        GRID["dividend"],
        GRID["option_type"],
    ):
        ttm_years = ttm_days / 365.0
        is_call = opt == "call"
        price = bs_european_price(
            spot=spot,
            strike=strike,
            ttm_years=ttm_years,
            rate=rate,
            volatility=vol,
            is_call=is_call,
            dividend=q,
        )
        cases.append(
            {
                "spot": spot,
                "strike": strike,
                "ttm_days": ttm_days,
                "ttm_years": ttm_years,
                "volatility": vol,
                "rate": rate,
                "dividend": q,
                "option_type": opt,
                "expected_price": price,
            }
        )

    payload = {
        "schema_version": 1,
        "source": "Frontend/scripts/generate-bs-parity-fixture.py (math.erf — bit-equivalent to PythonDataService bs_greeks.bs_european_price)",
        "tolerance": {
            "atol": 1e-4,
            "rtol": 0.0,
            "rationale": (
                "Frontend BS uses Abramowitz & Stegun 7.1.26 normal-CDF "
                "approximation (|error| < 1.5e-7). For typical S~100 options "
                "this propagates to up to ~1.5e-5 in BS price units. "
                "atol=1e-4 is comfortably above that error floor. "
                "Tightening would require a higher-precision TS CDF — "
                "out of scope for the parity test."
            ),
        },
        "n_cases": len(cases),
        "cases": cases,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"Wrote {len(cases)} cases to {OUTPUT}")


if __name__ == "__main__":
    main()
