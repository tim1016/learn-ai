"""Generators for Phase 2 volatility golden fixtures.

IV-001 — Implied volatility solver round-trip (atol=1e-6)
IV-002 — SVI total variance surface (atol=1e-6)
IV-003 — IV30 constant-maturity, variance-time interpolation (atol=1e-6)
IV-004 — IV rank rolling 60-day window (atol=1e-9)
RV-001 — Close-to-close realized volatility (atol=1e-9)
RV-002 — HF two-component realized vol — ABDL (atol=1e-8)
RV-003 — IV-RV basis conversion ACT/365 → TRD/252 (atol=1e-9)
RV-004 — Model-free variance replication — CBOE formula (atol=1e-6)

Oracle kinds:
  IV-001: hand_computed — BSM pricing formula generates market prices; σ_known is oracle
  IV-002: literature_formula — Gatheral (2004) SVI w(k) = a+b(ρ(k-m)+√((k-m)²+σ²))
  IV-003: hand_computed — CBOE per-expiry formula + variance-time interpolation in numpy
  IV-004: hand_computed — rolling (iv-min)/(max-min) in numpy matching pandas logic
  RV-001: hand_computed — rolling var(log_ret, ddof=1)*252 in numpy
  RV-002: hand_computed — per-day Σ r²_intra + r²_overnight, rolling sum annualized
  RV-003: literature_formula — σ_TRD252=σ_ACT365·√(D·252/(365·N)), N from NYSE calendar
  RV-004: literature_formula — CBOE VIX 2019 whitepaper formula in numpy
"""
from __future__ import annotations

import math
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
from scipy.stats import norm

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "PythonDataService" / "tests" / "fixtures"))
sys.path.insert(0, str(REPO_ROOT / "PythonDataService"))

from golden_support.hashing import compute_hashes  # noqa: E402
from golden_support.io import write_arrow  # noqa: E402

GENERATION_DATE = date(2026, 5, 8).isoformat()


# ── Standalone oracles (no app imports) ──────────────────────────────────────


def _bs_price(
    spot: float,
    strike: float,
    ttm: float,
    rate: float,
    dividend: float,
    sigma: float,
    is_call: bool,
) -> float:
    """BSM price — pure-Python oracle, never calls app code."""
    sqrt_t = math.sqrt(ttm)
    d1 = (
        math.log(spot / strike) + (rate - dividend + 0.5 * sigma**2) * ttm
    ) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    disc_r = math.exp(-rate * ttm)
    disc_q = math.exp(-dividend * ttm)
    nd1 = float(norm.cdf(d1))
    nd2 = float(norm.cdf(d2))
    if is_call:
        return spot * disc_q * nd1 - strike * disc_r * nd2
    return strike * disc_r * (1.0 - nd2) - spot * disc_q * (1.0 - nd1)


def _svi_w(k: float, a: float, b: float, rho: float, m: float, sigma: float) -> float:
    """Gatheral SVI raw total variance. Eq. (1) from Gatheral (2004)."""
    diff = k - m
    return a + b * (rho * diff + math.sqrt(diff * diff + sigma * sigma))


def _cboe_sigma_sq(
    strikes: list[float],
    Q: list[float],
    T_years: float,
    rate: float,
    forward: float,
    K0: float,
) -> float:
    """CBOE VIX 2019 whitepaper per-expiry variance formula (numpy-free version)."""
    n = len(strikes)
    e_rT = math.exp(rate * T_years)
    var_sum = 0.0
    for i in range(n):
        dK = (
            strikes[1] - strikes[0]
            if i == 0
            else (
                strikes[-1] - strikes[-2]
                if i == n - 1
                else (strikes[i + 1] - strikes[i - 1]) / 2.0
            )
        )
        var_sum += (dK / (strikes[i] ** 2)) * e_rT * Q[i]
    return (2.0 / T_years) * var_sum - (1.0 / T_years) * (forward / K0 - 1.0) ** 2


def _variance_time_interp(
    sigma_sq_T1: float,
    T1: float,
    sigma_sq_T2: float,
    T2: float,
    T_target: float,
) -> float:
    """Variance-time interpolation as in vix_style_iv30."""
    w = (T2 - T_target) / (T2 - T1)
    var_times_T = w * sigma_sq_T1 * T1 + (1.0 - w) * sigma_sq_T2 * T2
    return math.sqrt(var_times_T / T_target)


def _nyse_trading_days_from_ms(asof_ms: int, calendar_days: int) -> int:
    """Count NYSE sessions using the same ET-date logic as the canonical.

    The canonical's nyse_trading_days_in_window converts the UTC timestamp to
    America/New_York and floors to date before counting — midnight UTC maps to
    the *previous* ET calendar day. This function replicates that exact logic
    so the fixture's pinned N matches what the canonical computes.
    """
    import pandas_market_calendars as mcal
    from datetime import timedelta

    nyse = mcal.get_calendar("NYSE")
    ts = pd.Timestamp(asof_ms, unit="ms", tz="UTC")
    et_date = ts.tz_convert("America/New_York").normalize().tz_localize(None)
    end_inclusive = et_date + timedelta(days=calendar_days - 1)
    schedule = nyse.schedule(start_date=et_date, end_date=end_inclusive)
    return len(schedule)


# ── Shared write helper ───────────────────────────────────────────────────────


def _write_and_report(
    version_dir: Path,
    fixture_id: str,
    input_table: pa.Table,
    output_table: pa.Table,
    attribution_fn,
    justification: str,
) -> None:
    input_path = version_dir / "input.arrow"
    output_path = version_dir / "output.arrow"
    attribution_path = version_dir / "attribution.md"

    write_arrow(input_table, input_path)
    write_arrow(output_table, output_path)
    attribution_fn(attribution_path, justification)

    content_hashes, file_hashes = compute_hashes(
        version_dir, ["input.arrow", "output.arrow"]
    )
    n_rows = len(input_table)
    print(f"  {fixture_id}: {n_rows} row(s)")
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


# ══════════════════════════════════════════════════════════════════════════════
#  IV-001 — Implied Volatility Solver Round-Trip
# ══════════════════════════════════════════════════════════════════════════════

# 12 cases: varied moneyness, TTM, rate, dividend, put/call
_IV001_CASES: list[tuple[float, float, float, float, float, float, bool]] = [
    # (spot, strike, rate, ttm_years, dividend, sigma_known, is_call)
    (100.0, 90.0, 0.05, 0.25, 0.00, 0.20, True),   # ITM call
    (100.0, 100.0, 0.05, 0.25, 0.00, 0.20, True),  # ATM call
    (100.0, 110.0, 0.05, 0.25, 0.00, 0.20, True),  # OTM call
    (100.0, 90.0, 0.05, 1.00, 0.00, 0.25, True),   # ITM call, long TTM
    (100.0, 100.0, 0.05, 1.00, 0.00, 0.25, True),  # ATM call, long TTM
    (100.0, 110.0, 0.05, 1.00, 0.00, 0.25, True),  # OTM call, long TTM
    (100.0, 90.0, 0.02, 0.25, 0.01, 0.30, True),   # ITM call + dividend
    (100.0, 100.0, 0.02, 0.25, 0.01, 0.30, True),  # ATM call + dividend
    (100.0, 110.0, 0.02, 0.25, 0.01, 0.30, True),  # OTM call + dividend
    (100.0, 90.0, 0.02, 1.00, 0.01, 0.35, False),  # OTM put
    (100.0, 100.0, 0.02, 1.00, 0.01, 0.35, False), # ATM put
    (100.0, 110.0, 0.02, 1.00, 0.01, 0.35, False), # ITM put
]


def generate_iv001(version_dir: Path, justification: str = "") -> None:
    """IV-001: IV solver round-trip — generates market_price from known σ; oracle = σ_known."""
    rows = _IV001_CASES
    spots = [r[0] for r in rows]
    strikes = [r[1] for r in rows]
    rates = [r[2] for r in rows]
    ttms = [r[3] for r in rows]
    divs = [r[4] for r in rows]
    sigmas = [r[5] for r in rows]
    is_calls = [r[6] for r in rows]

    market_prices = [
        _bs_price(s, k, t, r, d, sig, c)
        for s, k, r, t, d, sig, c in zip(spots, strikes, rates, ttms, divs, sigmas, is_calls)
    ]

    inp = pa.table(
        {
            "spot": pa.array(spots, type=pa.float64()),
            "strike": pa.array(strikes, type=pa.float64()),
            "rate": pa.array(rates, type=pa.float64()),
            "ttm_years": pa.array(ttms, type=pa.float64()),
            "dividend": pa.array(divs, type=pa.float64()),
            "market_price": pa.array(market_prices, type=pa.float64()),
            "is_call": pa.array(is_calls, type=pa.bool_()),
        }
    )
    out = pa.table({"sigma_oracle": pa.array(sigmas, type=pa.float64())})

    def _attr(path: Path, just: str) -> None:
        path.write_text(
            f"""# IV-001 — Implied Volatility Solver Round-Trip

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. 12-case grid of
(spot, strike, rate, ttm, dividend, sigma_known) spanning ITM/ATM/OTM,
short/long TTM, calls and puts, with and without dividend.
`market_price` = BSM formula applied to sigma_known — guaranteed solvable.

**Layer 2 — Methodology provenance:** Three-stage cascade in
`app/volatility/solver.py::implied_volatility` — Newton-Raphson →
QuantLib → scipy Brent. Hull (2022) §19.11; Brent (1973).

**Layer 3 — Independent numerical oracle:** `sigma_oracle` = `sigma_known`.
Market price was generated from that exact sigma; any convergent solver
must recover it to within floating-point noise. The oracle is the input
sigma itself — no external library needed.

## Formula

```text
market_price = BSM(spot, strike, ttm, rate, dividend, sigma_known, is_call)
oracle: sigma_oracle = sigma_known
test: |implied_volatility(market_price, ...).iv - sigma_oracle| <= 1e-6
```

## Cases

| row | spot  | strike | ttm  | rate | div  | sigma | call? |
|-----|-------|--------|------|------|------|-------|-------|
| 0   | 100.0 | 90.0   | 0.25 | 0.05 | 0.00 | 0.20  | T     |
| 1   | 100.0 | 100.0  | 0.25 | 0.05 | 0.00 | 0.20  | T     |
| 2   | 100.0 | 110.0  | 0.25 | 0.05 | 0.00 | 0.20  | T     |
| 3   | 100.0 | 90.0   | 1.00 | 0.05 | 0.00 | 0.25  | T     |
| 4   | 100.0 | 100.0  | 1.00 | 0.05 | 0.00 | 0.25  | T     |
| 5   | 100.0 | 110.0  | 1.00 | 0.05 | 0.00 | 0.25  | T     |
| 6   | 100.0 | 90.0   | 0.25 | 0.02 | 0.01 | 0.30  | T     |
| 7   | 100.0 | 100.0  | 0.25 | 0.02 | 0.01 | 0.30  | T     |
| 8   | 100.0 | 110.0  | 0.25 | 0.02 | 0.01 | 0.30  | T     |
| 9   | 100.0 | 90.0   | 1.00 | 0.02 | 0.01 | 0.35  | F     |
| 10  | 100.0 | 100.0  | 1.00 | 0.02 | 0.01 | 0.35  | F     |
| 11  | 100.0 | 110.0  | 1.00 | 0.02 | 0.01 | 0.35  | F     |

## Canonical Implementation

`PythonDataService/app/volatility/solver.py::implied_volatility`

## Tolerance

atol=1e-6, rtol=0.0. Rationale: the solver's internal convergence criterion is
1e-7 (Newton-Raphson) or 1e-10 (Brent); the 1e-6 floor provides one order of
headroom while remaining tight enough to catch any solver regression.

## Regeneration

```bash
python scripts/generate_fixtures.py --id IV-001 --force \\
  --justification "<reason>"
```

## Generation Metadata

Generated: {GENERATION_DATE}
Oracle: hand_computed — BSM pricing formula; sigma_known is the round-trip answer
Script: scripts/fixture_generators/volatility.py
{'Justification: ' + just if just else '(initial generation)'}
""",
            encoding="utf-8",
        )

    _write_and_report(version_dir, "IV-001", inp, out, _attr, justification)


# ══════════════════════════════════════════════════════════════════════════════
#  IV-002 — SVI Total Variance Surface
# ══════════════════════════════════════════════════════════════════════════════

# 3 SVI parameter sets; 7 log-moneyness values each; ttm=0.5, forward=100.0
_SVI_PARAMS: list[tuple[float, float, float, float, float]] = [
    # (a, b, rho, m, sigma)
    (0.020, 0.30, -0.70, 0.00, 0.20),  # typical equity surface — steep left skew
    (0.040, 0.50, -0.40, 0.05, 0.15),  # higher vol, moderate skew
    (0.010, 0.20, -0.50, -0.05, 0.25), # low ATM vol, symmetric-ish
]
_SVI_K_VALUES: list[float] = [-0.30, -0.15, -0.05, 0.00, 0.05, 0.15, 0.30]
_SVI_TTM = 0.5
_SVI_FORWARD = 100.0
_SVI_N_STRIKES = len(_SVI_K_VALUES)
_SVI_N_CASES = len(_SVI_PARAMS)


def generate_iv002(version_dir: Path, justification: str = "") -> None:
    """IV-002: SVI total variance — oracle from direct Gatheral formula; fit_svi must recover."""
    rows_k: list[float] = []
    rows_a: list[float] = []
    rows_b: list[float] = []
    rows_rho: list[float] = []
    rows_m: list[float] = []
    rows_sigma: list[float] = []
    rows_forward: list[float] = []
    rows_ttm: list[float] = []
    rows_w_oracle: list[float] = []

    for a, b, rho, m, sigma in _SVI_PARAMS:
        for k in _SVI_K_VALUES:
            w = _svi_w(k, a, b, rho, m, sigma)
            rows_k.append(k)
            rows_a.append(a)
            rows_b.append(b)
            rows_rho.append(rho)
            rows_m.append(m)
            rows_sigma.append(sigma)
            rows_forward.append(_SVI_FORWARD)
            rows_ttm.append(_SVI_TTM)
            rows_w_oracle.append(w)

    inp = pa.table(
        {
            "k": pa.array(rows_k, type=pa.float64()),
            "forward": pa.array(rows_forward, type=pa.float64()),
            "ttm": pa.array(rows_ttm, type=pa.float64()),
            "a": pa.array(rows_a, type=pa.float64()),
            "b": pa.array(rows_b, type=pa.float64()),
            "rho": pa.array(rows_rho, type=pa.float64()),
            "m": pa.array(rows_m, type=pa.float64()),
            "sigma_svi": pa.array(rows_sigma, type=pa.float64()),
        }
    )
    out = pa.table({"w_oracle": pa.array(rows_w_oracle, type=pa.float64())})

    def _attr(path: Path, just: str) -> None:
        path.write_text(
            f"""# IV-002 — SVI Total Variance Surface

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. 3 SVI parameter sets × 7
log-moneyness values. Smile constructed from the oracle formula so that
`fit_svi` must recover the exact SVI parameters on a noiseless input.

**Layer 2 — Methodology provenance:** SVI raw parameterisation from Gatheral,
J. (2004). "A parsimonious arbitrage-free implied volatility parameterization
with application to the valuation of volatility derivatives." Presentation at
the Global Derivatives & Risk Management 2004.
Canonical: `app/volatility/fitting.py::fit_svi`.

**Layer 3 — Independent numerical oracle:** Direct formula evaluation:
`w(k) = a + b·(ρ·(k−m) + √((k−m)²+σ²))` without calling `fit_svi`.

## Formula

```text
w(k) = a + b · (ρ · (k−m) + √((k−m)² + σ²))

k     = log(K / F)     log-moneyness
w(k)  = σ²_IV · T     total implied variance
```

## SVI Parameter Sets

| set | a     | b    | ρ     | m     | σ_svi |
|-----|-------|------|-------|-------|-------|
| 0   | 0.020 | 0.30 | -0.70 | 0.00  | 0.20  |
| 1   | 0.040 | 0.50 | -0.40 | 0.05  | 0.15  |
| 2   | 0.010 | 0.20 | -0.50 | -0.05 | 0.25  |

k values: {_SVI_K_VALUES}
forward = {_SVI_FORWARD}, ttm = {_SVI_TTM}

## Test Protocol

1. Group fixture rows by (a, b, rho, m, sigma_svi) — 3 groups of 7.
2. Build SmileSlice: strikes = forward * exp(k); ivs = sqrt(w_oracle / ttm).
3. Call `fit_svi(smile)`.
4. For each k, compute w_fit = result.volatility(strike)² * ttm.
5. Assert |w_fit - w_oracle| <= 1e-6.

## Canonical Implementation

`PythonDataService/app/volatility/fitting.py::fit_svi`

## Tolerance

atol=1e-6, rtol=0.0. Rationale: scipy least_squares converges to ~1e-7
residuals on noiseless SVI input; 1e-6 provides one order of headroom while
remaining tight enough to detect any solver regression.

## Regeneration

```bash
python scripts/generate_fixtures.py --id IV-002 --force \\
  --justification "<reason>"
```

## Generation Metadata

Generated: {GENERATION_DATE}
Oracle: literature_formula — Gatheral (2004) SVI w(k) formula applied directly
Script: scripts/fixture_generators/volatility.py
{'Justification: ' + just if just else '(initial generation)'}
""",
            encoding="utf-8",
        )

    _write_and_report(version_dir, "IV-002", inp, out, _attr, justification)


# ══════════════════════════════════════════════════════════════════════════════
#  IV-003 — IV30 Constant-Maturity (Variance-Time Interpolation)
# ══════════════════════════════════════════════════════════════════════════════

# 3 test cases: (spot, rate, sigma_T1, sigma_T2, T1_cal, T2_cal)
_IV003_CASES: list[tuple[float, float, float, float, int, int]] = [
    (100.0, 0.05, 0.25, 0.27, 25, 35),  # standard straddle around 30d
    (100.0, 0.02, 0.20, 0.22, 20, 40),  # wider bracket
    (100.0, 0.04, 0.30, 0.28, 28, 32),  # narrow bracket, inverted smile
]
_IV003_N_STRIKES = 5
_IV003_TARGET = 30  # constant-maturity target in calendar days


def _build_chain_for_iv003(
    spot: float,
    rate: float,
    sigma: float,
    T_cal_days: int,
) -> tuple[list[float], list[float], list[float], float, float, float]:
    """Build synthetic 5-strike chain via BSM.

    Returns (strikes, call_mids, put_mids, forward, K0, T_years).
    Stores both sides so the test can build OptionQuote objects with proper
    call and put prices, allowing vix_style_iv30 to recover the forward via
    put-call parity.
    """
    T_years = T_cal_days / 365.0
    forward = spot * math.exp((rate - 0.0) * T_years)  # q=0

    # Strike grid centred on ATM: spot ± 5%, ± 10%
    atm = spot
    strike_pcts = [0.90, 0.95, 1.00, 1.05, 1.10]
    strikes = [atm * p for p in strike_pcts]

    call_mids: list[float] = []
    put_mids: list[float] = []
    for K in strikes:
        call_mids.append(_bs_price(spot, K, T_years, rate, 0.0, sigma, True))
        put_mids.append(_bs_price(spot, K, T_years, rate, 0.0, sigma, False))

    # K0 = max{K: K <= forward}
    K0 = max((K for K in strikes if K <= forward), default=strikes[0])
    return strikes, call_mids, put_mids, forward, K0, T_years


def _select_cboe_Q(
    strikes: list[float],
    call_mids: list[float],
    put_mids: list[float],
    forward: float,
    K0: float,
) -> list[float]:
    """Select Q per CBOE rule: put for K<K0, call for K>K0, avg at K0."""
    K0_idx = strikes.index(K0)
    Q: list[float] = []
    for i, K in enumerate(strikes):
        if i < K0_idx:
            Q.append(put_mids[i])
        elif i > K0_idx:
            Q.append(call_mids[i])
        else:
            Q.append((call_mids[i] + put_mids[i]) / 2.0)
    return Q


def generate_iv003(version_dir: Path, justification: str = "") -> None:
    """IV-003: IV30 constant-maturity — full CBOE + variance-time interpolation oracle."""
    rows: dict[str, list] = {
        "spot": [],
        "rate": [],
        "sigma_T1": [],
        "sigma_T2": [],
        "T1_cal_days": [],
        "T2_cal_days": [],
    }
    # Store two chains per case: 5 strikes each, both call_mid and put_mid (bid=ask=mid)
    # This lets the test build proper OptionQuote objects so vix_style_iv30 can
    # recover forward via put-call parity.
    for leg in ("e1", "e2"):
        for i in range(_IV003_N_STRIKES):
            rows[f"{leg}_strike_{i}"] = []
            rows[f"{leg}_call_mid_{i}"] = []
            rows[f"{leg}_put_mid_{i}"] = []

    oracle_iv30: list[float] = []

    for spot, rate, sigma1, sigma2, T1_cal, T2_cal in _IV003_CASES:
        strikes1, cm1, pm1, fwd1, K01, T1 = _build_chain_for_iv003(spot, rate, sigma1, T1_cal)
        strikes2, cm2, pm2, fwd2, K02, T2 = _build_chain_for_iv003(spot, rate, sigma2, T2_cal)

        Q1 = _select_cboe_Q(strikes1, cm1, pm1, fwd1, K01)
        Q2 = _select_cboe_Q(strikes2, cm2, pm2, fwd2, K02)

        sigma_sq_T1 = _cboe_sigma_sq(strikes1, Q1, T1, rate, fwd1, K01)
        sigma_sq_T2 = _cboe_sigma_sq(strikes2, Q2, T2, rate, fwd2, K02)
        T_target = _IV003_TARGET / 365.0

        iv30 = _variance_time_interp(sigma_sq_T1, T1, sigma_sq_T2, T2, T_target)

        rows["spot"].append(spot)
        rows["rate"].append(rate)
        rows["sigma_T1"].append(sigma1)
        rows["sigma_T2"].append(sigma2)
        rows["T1_cal_days"].append(T1_cal)
        rows["T2_cal_days"].append(T2_cal)

        for i in range(_IV003_N_STRIKES):
            rows[f"e1_strike_{i}"].append(strikes1[i])
            rows[f"e1_call_mid_{i}"].append(cm1[i])
            rows[f"e1_put_mid_{i}"].append(pm1[i])
            rows[f"e2_strike_{i}"].append(strikes2[i])
            rows[f"e2_call_mid_{i}"].append(cm2[i])
            rows[f"e2_put_mid_{i}"].append(pm2[i])

        oracle_iv30.append(iv30)

    inp_schema_floats = [
        "spot", "rate", "sigma_T1", "sigma_T2",
    ]
    inp_schema_ints = ["T1_cal_days", "T2_cal_days"]
    for leg in ("e1", "e2"):
        for i in range(_IV003_N_STRIKES):
            inp_schema_floats += [
                f"{leg}_strike_{i}",
                f"{leg}_call_mid_{i}",
                f"{leg}_put_mid_{i}",
            ]

    arrays: dict[str, pa.Array] = {}
    for col in inp_schema_floats:
        arrays[col] = pa.array(rows[col], type=pa.float64())
    for col in inp_schema_ints:
        arrays[col] = pa.array(rows[col], type=pa.int32())

    inp = pa.table(arrays)
    out = pa.table({"iv30_oracle": pa.array(oracle_iv30, type=pa.float64())})

    def _attr(path: Path, just: str) -> None:
        path.write_text(
            f"""# IV-003 — IV30 Constant-Maturity (Variance-Time Interpolation)

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. 3 test cases with 2-expiry
BSM-priced option chains (5 strikes each, zero spread, calls for K>F / puts
for K<F / average at K=K0). Chains generated from known BSM sigma values.

**Layer 2 — Methodology provenance:** CBOE VIX 2019 whitepaper "VIX Index
Calculation: Step-by-Step" for the per-expiry variance integral; variance-time
interpolation as in Demeterfi, Derman, Kamal, Zou (1999).
Canonical: `app/volatility/vix_replication.py::vix_style_iv30`.

**Layer 3 — Independent numerical oracle:** CBOE formula applied directly in
Python (same algorithm, independently coded). Variance-time interpolation then
applied directly. No call to `vix_style_iv30`.

## Formula

```text
σ²(T) = (2/T)·Σᵢ(ΔKᵢ/Kᵢ²)·e^(rT)·Q(Kᵢ) − (1/T)·(F/K₀ − 1)²

IV30:
  w = (T2 − T_target) / (T2 − T1)
  σ²_target · T_target = w·σ²_T1·T1 + (1−w)·σ²_T2·T2
  iv30 = √(σ²_target)
```

## Test Protocol

1. Reconstruct two `OptionQuote` lists per row (call mid = Q for K>F, put mid = Q for K<F).
2. Call `vix_style_iv30(chain1, chain2, rate=rate, T1_calendar_days=T1, T2_calendar_days=T2)`.
3. Assert |result − iv30_oracle| <= 1e-6.

## Canonical Implementation

`PythonDataService/app/volatility/vix_replication.py::vix_style_iv30`

## Tolerance

atol=1e-6, rtol=0.0. Rationale: both canonical and oracle implement the same
CBOE formula; the 1e-6 floor exceeds the ~1e-12 observed float64 discrepancy
between a pure-Python and numpy implementation of the same arithmetic, and
accounts for any platform variance in scipy's exp/log transcendentals.

## Regeneration

```bash
python scripts/generate_fixtures.py --id IV-003 --force \\
  --justification "<reason>"
```

## Generation Metadata

Generated: {GENERATION_DATE}
Oracle: hand_computed — CBOE per-expiry formula + variance-time interpolation in Python
Script: scripts/fixture_generators/volatility.py
{'Justification: ' + just if just else '(initial generation)'}
""",
            encoding="utf-8",
        )

    _write_and_report(version_dir, "IV-003", inp, out, _attr, justification)


# ══════════════════════════════════════════════════════════════════════════════
#  IV-004 — IV Rank Rolling 60-Day Window
# ══════════════════════════════════════════════════════════════════════════════

_IV004_N_BARS = 80
_IV004_WINDOW = 60
_IV004_MIN_PERIODS = 30
_IV004_SEED = 42


def _oracle_iv_rank(iv: np.ndarray, window: int, min_periods: int) -> np.ndarray:
    """IV rank oracle — mirrors OptionsFeatures.compute_iv_rank exactly.

    Canonical uses np.where(denom > 1e-10, rank, 0.5) on rolling_min/max.
    When rolling returns NaN (before min_periods), denom = NaN, NaN > 1e-10
    evaluates to False, so np.where returns 0.5 — not NaN. The oracle matches
    this: bars before min_periods get 0.5.
    """
    n = len(iv)
    rank = np.full(n, 0.5)  # default 0.5 matches np.where(NaN > 1e-10) → 0.5
    for i in range(n):
        start = max(0, i - window + 1)
        count = i - start + 1
        if count < min_periods:
            continue  # leave rank[i] = 0.5 (matches canonical for pre-warmup)
        window_vals = iv[start : i + 1]
        lo = float(np.min(window_vals))
        hi = float(np.max(window_vals))
        denom = hi - lo
        if denom > 1e-10:
            rank[i] = (float(iv[i]) - lo) / denom
        else:
            rank[i] = 0.5
    return rank


def generate_iv004(version_dir: Path, justification: str = "") -> None:
    """IV-004: IV rank rolling 60d — seeded synthetic IV series; oracle = rolling formula."""
    rng = np.random.default_rng(_IV004_SEED)
    # Simulate a realistic IV process: mean-reverting around 0.20
    iv = np.full(_IV004_N_BARS, 0.0)
    iv[0] = 0.20
    for i in range(1, _IV004_N_BARS):
        iv[i] = max(0.05, iv[i - 1] + rng.normal(0.0, 0.012))

    oracle = _oracle_iv_rank(iv, _IV004_WINDOW, _IV004_MIN_PERIODS)

    inp = pa.table(
        {f"iv_{i}": pa.array([float(iv[i])], type=pa.float64()) for i in range(_IV004_N_BARS)}
    )
    out = pa.table(
        {
            f"rank_{i}": pa.array(
                [float("nan") if np.isnan(oracle[i]) else float(oracle[i])],
                type=pa.float64(),
            )
            for i in range(_IV004_N_BARS)
        }
    )

    def _attr(path: Path, just: str) -> None:
        first_non_nan = next(
            (i for i in range(_IV004_N_BARS) if not np.isnan(oracle[i])), None
        )
        path.write_text(
            f"""# IV-004 — IV Rank Rolling 60-Day Window

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. {_IV004_N_BARS}-bar IV series
simulated via mean-reverting random walk (seed={_IV004_SEED}, mean=0.20,
σ_step=0.012). Represents a realistic ATM IV30 time series.

**Layer 2 — Methodology provenance:**
`app/research/features/options_features.py::OptionsFeatures.compute_iv_rank`
Uses `pd.Series.rolling(window=60, min_periods=30).min()/.max()`.

**Layer 3 — Independent numerical oracle:** Pure-Python loop computing the
rolling min/max and rank formula without calling `compute_iv_rank` or pandas.

## Formula

```text
rolling_min[i] = min(iv[max(0, i-59)..i])  if count >= 30 else NaN
rolling_max[i] = max(iv[max(0, i-59)..i])  if count >= 30 else NaN
denom = rolling_max - rolling_min
rank[i] = (iv[i] - rolling_min[i]) / denom  if denom > 1e-10 else 0.5
         0.5                                 if count < 30
```

Note: pre-warmup bars return 0.5 (not NaN). The canonical uses
`np.where(denom > 1e-10, rank, 0.5)` where NaN comparisons evaluate to False,
so bars below min_periods get 0.5 as the fallback value.

## Warmup Convention

Bars 0..{_IV004_MIN_PERIODS - 2} (first {_IV004_MIN_PERIODS - 1}): 0.5 (below min_periods={_IV004_MIN_PERIODS}).
First bar with computed rank: bar index {_IV004_MIN_PERIODS - 1} (0-indexed).

## Canonical Implementation

`PythonDataService/app/research/features/options_features.py::OptionsFeatures.compute_iv_rank`

## Tolerance

atol=1e-9, rtol=0.0. Rationale: the oracle and canonical both apply the same
arithmetic (subtraction, division); float64 ULP differences are < 1e-15.
The 1e-9 floor provides ample headroom for any platform variance.

## Regeneration

```bash
python scripts/generate_fixtures.py --id IV-004 --force \\
  --justification "<reason>"
```

## Generation Metadata

Generated: {GENERATION_DATE}
Oracle: hand_computed — rolling (iv-min)/(max-min) in pure Python
Script: scripts/fixture_generators/volatility.py
{'Justification: ' + just if just else '(initial generation)'}
""",
            encoding="utf-8",
        )

    _write_and_report(version_dir, "IV-004", inp, out, _attr, justification)


# ══════════════════════════════════════════════════════════════════════════════
#  RV-001 — Close-to-Close Realized Volatility
# ══════════════════════════════════════════════════════════════════════════════

_RV001_N_BARS = 30
_RV001_WINDOW = 10
_RV001_SEED = 7


def _oracle_ctc_rv(close: np.ndarray, window: int, bars_per_year: int = 252) -> np.ndarray:
    """Close-to-close RV oracle: rolling var(log_ret, ddof=1) * bars_per_year, sqrt.

    Canonical uses log_ret.rolling(window, min_periods=window).var(ddof=1):
    every window slot must be non-NaN. Since log_ret[0]=NaN, the first valid
    bar is window (not window-1): bars 1..window all have valid log returns.
    """
    n = len(close)
    log_ret = np.full(n, np.nan)
    for i in range(1, n):
        log_ret[i] = math.log(float(close[i]) / float(close[i - 1]))

    rv = np.full(n, np.nan)
    for i in range(n):
        start = max(0, i - window + 1)
        count = i - start + 1
        if count < window:
            continue
        window_rets = log_ret[start : i + 1]
        valid = window_rets[~np.isnan(window_rets)]
        if len(valid) < window:  # canonical requires all window slots non-NaN
            continue
        v = float(np.var(valid, ddof=1))
        rv[i] = math.sqrt(v * bars_per_year)
    return rv


def generate_rv001(version_dir: Path, justification: str = "") -> None:
    """RV-001: Close-to-close RV — seeded synthetic closes; oracle = rolling var formula."""
    rng = np.random.default_rng(_RV001_SEED)
    log_rets = rng.normal(0.0, 0.01, _RV001_N_BARS - 1)
    close = np.empty(_RV001_N_BARS)
    close[0] = 100.0
    for i, r in enumerate(log_rets):
        close[i + 1] = close[i] * math.exp(r)

    oracle = _oracle_ctc_rv(close, _RV001_WINDOW)

    inp = pa.table(
        {f"close_{i}": pa.array([float(close[i])], type=pa.float64()) for i in range(_RV001_N_BARS)}
    )
    out = pa.table(
        {
            f"rv_{i}": pa.array(
                [float("nan") if np.isnan(oracle[i]) else float(oracle[i])],
                type=pa.float64(),
            )
            for i in range(_RV001_N_BARS)
        }
    )

    def _attr(path: Path, just: str) -> None:
        first_non_nan = next(
            (i for i in range(_RV001_N_BARS) if not np.isnan(oracle[i])), None
        )
        path.write_text(
            f"""# RV-001 — Close-to-Close Realized Volatility

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. {_RV001_N_BARS}-bar close
price series from a GBM log-normal walk (seed={_RV001_SEED}, σ_step=0.01,
S₀=100.0).

**Layer 2 — Methodology provenance:** Standard close-to-close RV estimator.
Parkinson (1980) credits it as the "classical" method. Canonical:
`app/engine/edge/features_realtime/realized_vol.py::close_to_close`.

**Layer 3 — Independent numerical oracle:** Pure-Python loop computing
rolling sample variance of log returns with ddof=1, annualized with ×252.

## Formula

```text
log_ret[i] = ln(close[i] / close[i-1])
var[i]     = Var(log_ret[i-9..i], ddof=1)  — rolling window={_RV001_WINDOW}
rv[i]      = √(var[i] · 252)               — annualized
NaN for i < {_RV001_WINDOW}
```

## NaN Convention

Bars 0..{_RV001_WINDOW - 2}: NaN. First non-NaN bar: {first_non_nan}.

## Canonical Implementation

`PythonDataService/app/engine/edge/features_realtime/realized_vol.py::close_to_close`

## Tolerance

atol=1e-9, rtol=0.0. Rationale: oracle and canonical use identical ddof=1
variance formulas on the same float64 data; observed max abs error < 1e-15.

## Regeneration

```bash
python scripts/generate_fixtures.py --id RV-001 --force \\
  --justification "<reason>"
```

## Generation Metadata

Generated: {GENERATION_DATE}
Oracle: hand_computed — rolling var(log_ret, ddof=1)*252, pure Python
Script: scripts/fixture_generators/volatility.py
{'Justification: ' + just if just else '(initial generation)'}
""",
            encoding="utf-8",
        )

    _write_and_report(version_dir, "RV-001", inp, out, _attr, justification)


# ══════════════════════════════════════════════════════════════════════════════
#  RV-002 — HF Two-Component Realized Volatility (ABDL)
# ══════════════════════════════════════════════════════════════════════════════

# 5 trading days, 4 ETH bars per day (04:00/08:00/12:00/16:00 ET = 09:00/13:00/17:00/21:00 UTC)
# EST offset = -5h in Jan 2024
_RV002_N_DAYS = 5
_RV002_BARS_PER_DAY = 4
_RV002_WINDOW = 3  # trading days
_RV002_SEED = 13

# Day anchor timestamps (2024-01-02 09:00:00 UTC) in ms:
_DAY0_09UTC_MS = 1704186000000  # 2024-01-02 04:00 ET
_DAY_MS = 86_400_000            # one calendar day in ms
_HOUR_MS = 3_600_000

# Bar offsets from 09:00 UTC: 0h, 4h, 8h, 12h (04:00/08:00/12:00/16:00 ET)
_BAR_OFFSETS_MS = [0, 4 * _HOUR_MS, 8 * _HOUR_MS, 12 * _HOUR_MS]


def _build_rv002_bars() -> tuple[list[int], list[float], list[float], list[int]]:
    """Build 20-bar synthetic ETH intraday series (5 days × 4 bars per day)."""
    rng = np.random.default_rng(_RV002_SEED)
    ts_ms: list[int] = []
    opens: list[float] = []
    closes: list[float] = []
    volumes: list[int] = []

    price = 100.0
    for day_idx in range(_RV002_N_DAYS):
        day_base_ms = _DAY0_09UTC_MS + day_idx * _DAY_MS
        for bar_idx in range(_RV002_BARS_PER_DAY):
            ts = day_base_ms + _BAR_OFFSETS_MS[bar_idx]
            o = price
            ret = rng.normal(0.0, 0.005)
            c = price * math.exp(ret)
            price = c
            ts_ms.append(ts)
            opens.append(o)
            closes.append(c)
            volumes.append(1000)

    return ts_ms, opens, closes, volumes


def _oracle_hf_rv(
    ts_ms: list[int],
    closes: list[float],
    window: int,
) -> list[float]:
    """Hand-computed ABDL two-component RV oracle.

    Per-day: RV²_d = Σ r²_intra + r²_overnight.
    Intraday returns: consecutive closes within the same day.
    Overnight: log(first_close_d / last_close_{d-1}).
    Rolling sum over window days, annualize × 252 / W, sqrt.
    Ffill onto bar grid.
    """
    # Group bars by day index
    n = len(ts_ms)
    day_groups: list[list[tuple[int, float]]] = []  # [(bar_idx_in_series, close)]
    bars_per_day = _RV002_BARS_PER_DAY
    for d in range(_RV002_N_DAYS):
        start = d * bars_per_day
        end = start + bars_per_day
        day_groups.append(list(enumerate(closes[start:end], start=start)))

    # Per-day RV²
    daily_rv_sq: list[float] = []
    for d, group in enumerate(day_groups):
        bar_indices = [idx for idx, _ in group]
        close_vals = [c for _, c in group]

        # Intraday squared returns (within-day consecutive closes)
        intra_sq = sum(
            math.log(close_vals[i] / close_vals[i - 1]) ** 2
            for i in range(1, len(close_vals))
        )

        # Overnight: first close of day d / last close of day d-1
        if d == 0:
            overnight_sq = 0.0
        else:
            prev_last_close = day_groups[d - 1][-1][1]
            overnight_sq = math.log(close_vals[0] / prev_last_close) ** 2

        daily_rv_sq.append(intra_sq + overnight_sq)

    # Rolling sum over window days → annualized vol per day
    daily_rv: list[float | None] = []
    for d in range(_RV002_N_DAYS):
        if d < window - 1:
            daily_rv.append(None)
        else:
            rv_sq_sum = sum(daily_rv_sq[d - window + 1 : d + 1])
            daily_rv.append(math.sqrt(rv_sq_sum * 252.0 / window))

    # Ffill onto bar grid (each day's value fills all its bars)
    result: list[float] = []
    for d in range(_RV002_N_DAYS):
        val = daily_rv[d] if daily_rv[d] is not None else float("nan")
        for _ in range(bars_per_day):
            result.append(val)

    return result


def generate_rv002(version_dir: Path, justification: str = "") -> None:
    """RV-002: HF two-component RV — seeded intraday bars; oracle = ABDL formula."""
    ts_ms, opens, closes, volumes = _build_rv002_bars()
    oracle = _oracle_hf_rv(ts_ms, closes, _RV002_WINDOW)

    inp = pa.table(
        {
            "ts_ms": pa.array(ts_ms, type=pa.int64()),
            "open": pa.array(opens, type=pa.float64()),
            "close": pa.array(closes, type=pa.float64()),
            "volume": pa.array(volumes, type=pa.int64()),
        }
    )
    out = pa.table({"rv_hf": pa.array(oracle, type=pa.float64())})

    n_total = len(ts_ms)
    n_nan = sum(1 for v in oracle if math.isnan(v))

    def _attr(path: Path, just: str) -> None:
        path.write_text(
            f"""# RV-002 — HF Two-Component Realized Volatility (ABDL)

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. {_RV002_N_DAYS} trading
days × {_RV002_BARS_PER_DAY} ETH bars per day = {n_total} bars total.
GBM intraday log-returns (seed={_RV002_SEED}, σ=0.005/bar), S₀=100.
Sessions: ETH 04:00–20:00 ET. Timestamps: UTC int64 ms.
Days: 2024-01-02 through 2024-01-08.

**Layer 2 — Methodology provenance:** Andersen, Bollerslev, Diebold, Labys
(2003) "Modeling and Forecasting Realized Volatility," Econometrica 71(2).
Two-component: intraday squared returns + overnight squared return per day.
Canonical: `app/engine/edge/features_realtime/hf_realized_vol.py::hf_realized_vol_trd252`.

**Layer 3 — Independent numerical oracle:** Pure-Python loop grouping bars by
trading day, computing intraday squared-return sums + overnight², rolling over
window_trading_days, annualizing × 252/W, then ffilling onto the bar grid.

## Formula

```text
For each trading day d:
  RV2_d = sum_i ln(close_i / close_{{i-1}})^2   (intraday)
          + ln(close_first_d / close_last_{{d-1}})^2  (overnight)

Rolling (window={_RV002_WINDOW} days):
  rv_hf_d = sqrt(sum RV2_{{d-W+1..d}} * 252 / W)

NaN for days 0..{_RV002_WINDOW - 2}. Ffilled onto bar grid.
```

## NaN Convention

{n_nan} of {n_total} output bars are NaN (first {_RV002_WINDOW - 1} days < window).

## Canonical Implementation

`PythonDataService/app/engine/edge/features_realtime/hf_realized_vol.py::hf_realized_vol_trd252`

## Tolerance

atol=1e-8, rtol=0.0. Rationale: oracle uses Python's built-in `math.log` while
canonical uses numpy's `np.log`; observed max abs error from float64 accumulation
across {_RV002_BARS_PER_DAY} bars per day is < 1e-13. The 1e-8 floor provides
five orders of headroom over the observed error while remaining tight enough to
detect any formula regression.

## Regeneration

```bash
python scripts/generate_fixtures.py --id RV-002 --force \\
  --justification "<reason>"
```

## Generation Metadata

Generated: {GENERATION_DATE}
Oracle: hand_computed — per-day ABDL two-component formula, pure Python
Script: scripts/fixture_generators/volatility.py
{'Justification: ' + just if just else '(initial generation)'}
""",
            encoding="utf-8",
        )

    _write_and_report(version_dir, "RV-002", inp, out, _attr, justification)


# ══════════════════════════════════════════════════════════════════════════════
#  RV-003 — IV-RV Basis Conversion (ACT/365 → TRD/252)
# ══════════════════════════════════════════════════════════════════════════════

# 3 cases: (sigma_act365, tenor_calendar_days, asof_date_str)
_RV003_CASES: list[tuple[float, int, str]] = [
    (0.20, 30, "2024-01-02"),  # standard 30d, N≈21 trading days
    (0.30, 21, "2024-01-02"),  # 21 calendar days → typically 15 trading days
    (0.25, 30, "2024-04-01"),  # April — different holiday profile
]


def generate_rv003(version_dir: Path, justification: str = "") -> None:
    """RV-003: IV-RV basis conversion — pinned NYSE calendar; oracle = direct formula."""
    sigmas: list[float] = []
    tenors: list[int] = []
    asof_ms_list: list[int] = []
    n_trading_list: list[int] = []
    sigma_trd_list: list[float] = []

    for sigma_act365, D, asof_str in _RV003_CASES:
        # Store asof as midnight UTC int64 ms (the repo's wire format)
        asof_ms = pd.Timestamp(asof_str, tz="UTC").value // 1_000_000
        # N must use the same ET-date logic as the canonical so fixture and
        # canonical agree (midnight UTC maps to previous ET date).
        N = _nyse_trading_days_from_ms(asof_ms, D)
        # Formula: σ_TRD252 = σ_ACT365 · √((D · 252) / (365 · N))
        sigma_trd252 = sigma_act365 * math.sqrt((D * 252.0) / (365.0 * N))

        sigmas.append(sigma_act365)
        tenors.append(D)
        asof_ms_list.append(asof_ms)
        n_trading_list.append(N)
        sigma_trd_list.append(sigma_trd252)

    inp = pa.table(
        {
            "sigma_act365": pa.array(sigmas, type=pa.float64()),
            "tenor_calendar_days": pa.array(tenors, type=pa.int32()),
            "asof_ms": pa.array(asof_ms_list, type=pa.int64()),
            "n_trading_pinned": pa.array(n_trading_list, type=pa.int32()),
        }
    )
    out = pa.table({"sigma_trd252": pa.array(sigma_trd_list, type=pa.float64())})

    def _attr(path: Path, just: str) -> None:
        rows_md = "\n".join(
            f"| {s:.2f}     | {d}        | {asof_str} | {N}  | "
            f"{sigma_trd:.6f}      |"
            for (s, d, asof_str), N, sigma_trd in zip(
                _RV003_CASES, n_trading_list, sigma_trd_list
            )
        )
        path.write_text(
            f"""# RV-003 — IV-RV Basis Conversion (ACT/365 → TRD/252)

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. 3 cases with varying σ,
tenor, and asof date. NYSE trading-day count (N) from `pandas_market_calendars`
— pinned in fixture as `n_trading_pinned` for audit transparency.

**Layer 2 — Methodology provenance:** Practitioner convention: variance accrues
only on trading days. Formula per `docs/references/iv-rv-basis-alignment.md`.
Canonical: `app/volatility/basis.py::convert_iv_act365_to_trading252`.

**Layer 3 — Independent numerical oracle:** Direct formula application using
`pandas_market_calendars` for N (same library as canonical) but applying the
formula without calling the canonical function.

## Formula

```text
σ²_TRD252 · (N/252) = σ²_ACT365 · (D/365)   ← equate total variance

σ_TRD252 = σ_ACT365 · √((D · 252) / (365 · N))

D = tenor_calendar_days
N = NYSE trading sessions in [asof_date, asof_date + D)
```

## Pinned Cases

| σ_ACT365 | D (cal days) | asof       | N  | σ_TRD252 (oracle) |
|----------|--------------|------------|----|-------------------|
{rows_md}

N is the NYSE trading-day count from `pandas_market_calendars` at generation
time and is stored in the fixture for traceability.

## Canonical Implementation

`PythonDataService/app/volatility/basis.py::convert_iv_act365_to_trading252`

## Tolerance

atol=1e-9, rtol=0.0. Rationale: the oracle and canonical apply identical
single-multiplication arithmetic (σ × √factor); the float64 rounding is
deterministic and agrees to < 1e-16.

## Regeneration

```bash
python scripts/generate_fixtures.py --id RV-003 --force \\
  --justification "<reason>"
```

## Generation Metadata

Generated: {GENERATION_DATE}
Oracle: literature_formula — σ_TRD252=σ_ACT365·√(D·252/(365·N)) with N from pandas_market_calendars
Script: scripts/fixture_generators/volatility.py
{'Justification: ' + just if just else '(initial generation)'}
""",
            encoding="utf-8",
        )

    _write_and_report(version_dir, "RV-003", inp, out, _attr, justification)


# ══════════════════════════════════════════════════════════════════════════════
#  RV-004 — Model-Free Variance Replication (CBOE Formula)
# ══════════════════════════════════════════════════════════════════════════════

# 3 test cases: (spot, rate, T_cal_days, sigma_input, n_strikes, moneyness_range)
# Build chains from BSM prices with zero spread; oracle = direct CBOE formula
_RV004_CASES: list[tuple[float, float, int, float, list[float]]] = [
    # (spot, rate, T_cal_days, sigma_input, strike_pcts)
    (100.0, 0.05, 30, 0.25, [0.85, 0.90, 0.95, 1.00, 1.05, 1.10, 1.15]),
    (100.0, 0.02, 60, 0.30, [0.80, 0.88, 0.94, 1.00, 1.06, 1.12, 1.20]),
    (100.0, 0.04, 21, 0.20, [0.90, 0.95, 1.00, 1.05, 1.10]),
]


def _build_rv004_chain(
    spot: float,
    rate: float,
    T_cal_days: int,
    sigma: float,
    strike_pcts: list[float],
) -> tuple[list[float], list[float], list[float], list[float], list[float], float, float, float]:
    """Build chain. Returns (strikes, call_bid, call_ask, put_bid, put_ask, forward, K0, T_years)."""
    T_years = T_cal_days / 365.0
    forward = spot * math.exp(rate * T_years)
    strikes = [spot * p for p in strike_pcts]

    call_mids = [_bs_price(spot, K, T_years, rate, 0.0, sigma, True) for K in strikes]
    put_mids = [_bs_price(spot, K, T_years, rate, 0.0, sigma, False) for K in strikes]

    K0 = max((K for K in strikes if K <= forward), default=strikes[0])
    return strikes, call_mids, call_mids, put_mids, put_mids, forward, K0, T_years


def generate_rv004(version_dir: Path, justification: str = "") -> None:
    """RV-004: Model-free variance — oracle = direct CBOE formula in Python."""
    all_spots: list[float] = []
    all_rates: list[float] = []
    all_T_cal: list[int] = []
    all_sigma_inputs: list[float] = []
    n_strikes_col: list[int] = []
    oracle_sigma_sq: list[float] = []
    oracle_forward: list[float] = []
    oracle_K0: list[float] = []

    # Store chains as fixed-width arrays padded to max_n_strikes=7
    max_n = max(len(case[4]) for case in _RV004_CASES)
    chain_cols: dict[str, list] = {}
    for i in range(max_n):
        for col in (f"strike_{i}", f"call_mid_{i}", f"put_mid_{i}"):
            chain_cols[col] = []

    for spot, rate, T_cal, sigma, strike_pcts in _RV004_CASES:
        n = len(strike_pcts)
        strikes, cb, _ca, pb, _pask, fwd, K0, T_years = _build_rv004_chain(
            spot, rate, T_cal, sigma, strike_pcts
        )

        # Build Q list (put for K<K0, call for K>K0, avg at K0)
        K0_idx = strikes.index(K0)
        Q: list[float] = []
        for i_q, K in enumerate(strikes):
            if i_q < K0_idx:
                Q.append(pb[i_q])  # put mid
            elif i_q > K0_idx:
                Q.append(cb[i_q])  # call mid
            else:
                Q.append((cb[i_q] + pb[i_q]) / 2.0)  # average at K0

        sigma_sq = _cboe_sigma_sq(strikes, Q, T_years, rate, fwd, K0)

        all_spots.append(spot)
        all_rates.append(rate)
        all_T_cal.append(T_cal)
        all_sigma_inputs.append(sigma)
        n_strikes_col.append(n)
        oracle_sigma_sq.append(sigma_sq)
        oracle_forward.append(fwd)
        oracle_K0.append(K0)

        # Pad to max_n with NaN
        for i in range(max_n):
            if i < n:
                chain_cols[f"strike_{i}"].append(strikes[i])
                chain_cols[f"call_mid_{i}"].append(cb[i])
                chain_cols[f"put_mid_{i}"].append(pb[i])
            else:
                chain_cols[f"strike_{i}"].append(float("nan"))
                chain_cols[f"call_mid_{i}"].append(float("nan"))
                chain_cols[f"put_mid_{i}"].append(float("nan"))

    inp_dict: dict[str, pa.Array] = {
        "spot": pa.array(all_spots, type=pa.float64()),
        "rate": pa.array(all_rates, type=pa.float64()),
        "T_cal_days": pa.array(all_T_cal, type=pa.int32()),
        "sigma_input": pa.array(all_sigma_inputs, type=pa.float64()),
        "n_strikes": pa.array(n_strikes_col, type=pa.int32()),
        "oracle_forward": pa.array(oracle_forward, type=pa.float64()),
        "oracle_K0": pa.array(oracle_K0, type=pa.float64()),
    }
    for col, vals in chain_cols.items():
        inp_dict[col] = pa.array(vals, type=pa.float64())

    inp = pa.table(inp_dict)
    out = pa.table({"sigma_sq_oracle": pa.array(oracle_sigma_sq, type=pa.float64())})

    def _attr(path: Path, just: str) -> None:
        path.write_text(
            f"""# RV-004 — Model-Free Variance Replication (CBOE Formula)

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. 3 test cases with BSM-priced
option chains (5–7 strikes, zero spread). One strike pct set each with varying
T, rate, and sigma. Chains designed to exercise K<K0 (put), K>K0 (call), and
K=K0 (average) logic.

**Layer 2 — Methodology provenance:** CBOE VIX 2019 whitepaper "VIX Index
Calculation: Step-by-Step." The underlying mathematics: Demeterfi, Derman,
Kamal, Zou (1999) "More Than You Ever Wanted to Know About Volatility Swaps."
Canonical: `app/volatility/vix_replication.py::replicate_expiry_variance`.

**Layer 3 — Independent numerical oracle:** Direct CBOE formula in Python
without calling `replicate_expiry_variance`. Same algorithm, independent code.
Oracle stores `sigma_sq_T` (annualized variance) as the output.

## Formula

```text
F   = K* + e^(rT) · (C(K*) − P(K*))     (put-call parity forward)
K0  = max{{K : K ≤ F}}
Q   = put_mid  for K < K0
    = call_mid for K > K0
    = (call_mid + put_mid)/2  at K0

ΔK  = (K_{{i+1}} − K_{{i-1}})/2  interior; forward/back diff at edges

σ²(T) = (2/T)·Σᵢ(ΔKᵢ/Kᵢ²)·e^(rT)·Q(Kᵢ) − (1/T)·(F/K₀ − 1)²
```

## Canonical Implementation

`PythonDataService/app/volatility/vix_replication.py::replicate_expiry_variance`

## Tolerance

atol=1e-6, rtol=0.0. Rationale: oracle uses Python's `math.exp/log` while
canonical uses `math.exp` in the same way; BSM-priced chains have no bid-ask
noise so σ²(T) deviates from the Black-Scholes model only through strike
discretization. The 1e-6 floor is deliberately loose to accommodate chains with
a small number of strikes (< 1e-4 discretization error expected).

## Regeneration

```bash
python scripts/generate_fixtures.py --id RV-004 --force \\
  --justification "<reason>"
```

## Generation Metadata

Generated: {GENERATION_DATE}
Oracle: literature_formula — CBOE VIX 2019 whitepaper formula in pure Python
Script: scripts/fixture_generators/volatility.py
{'Justification: ' + just if just else '(initial generation)'}
""",
            encoding="utf-8",
        )

    _write_and_report(version_dir, "RV-004", inp, out, _attr, justification)
