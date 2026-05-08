"""Pinned numerical conventions for the golden fixture system.

Every constant here is locked by the handoff document (2026-05-08). Any
change requires a deliberate fixture-regeneration commit with justification.
Fixtures that depend on a constant name it explicitly in their attribution.md
so future maintainers know which constant to update if conventions ever change.

Black-Scholes
-------------
Canonical signatures (must match bs_greeks.py and quantlib_pricer.py exactly):

  bs_european_price(spot, strike, ttm_years, rate, volatility, is_call, dividend=0.0)
  black_scholes_greeks(spot, strike, ttm_years, volatility, rate, dividend, is_call)

Note: argument order differs between the two functions — this is intentional
and must be preserved for backward compatibility.

Unit conventions for Greeks (match BSGreeks dataclass in bs_greeks.py):
  theta — per **calendar day**   (annual / 365)
  vega  — per **1% IV move**     (raw / 100)
  rho   — per **1% rate move**   (raw / 100 = 1 percentage point)

Oracle-native values are stored in fixtures verbatim; the validator
converts units before comparing, never inside the canonical implementation.

Engine statistics
-----------------
Sharpe:  sample std (ddof=1), annualization=252, rf=0 for fixture series.
         The canonical does not subtract rf from returns — that is the
         caller's responsibility when a non-zero rf is desired.
         Degenerate cases: len(returns) < 2 OR std == 0 → returns None.

Sortino: target=0, downside denominator=N (all returns, not just negative count).
         This matches statistics.py::_sortino exactly.

CAGR:    (final / initial) ** (1 / (trading_days / 252)) - 1

MDD:     max peak-to-trough on equity curve, reported as positive fraction.
         0.25 means 25% drawdown.

SVI
---
Parameterization: raw SVI — w(k) = a + b*(rho*(k-m) + sqrt((k-m)^2 + sigma^2))
  where k = log(K/F), parameters are (a, b, rho, m, sigma).
Fitter: scipy.optimize.least_squares(method='trf'), deterministic.
Fixture assertion: total variance on a fixed moneyness grid to atol=1e-4.
Raw parameters are NOT asserted (they are underdetermined up to reparameterization).

Do NOT claim Roger Lee wing constraints or no-butterfly enforcement — those
are future work and not currently implemented.

Timestamps
----------
Canonical format: int64 milliseconds UTC.
Single normalizer: golden_support/io.py::normalize_timestamp().
"""
from __future__ import annotations

# ── Black-Scholes units ───────────────────────────────────────────────────────

BS_THETA_UNIT: str = "per_day"  # annual theta / 365
BS_VEGA_UNIT: str = "per_vol_point"  # raw vega / 100 (1% IV move)
BS_RHO_UNIT: str = "per_rate_point"  # raw rho / 100 (1 percentage point)

# ── Engine statistics ─────────────────────────────────────────────────────────

TRADING_DAYS_PER_YEAR: int = 252
SHARPE_DDOF: int = 1  # sample standard deviation
SHARPE_RF: float = 0.0  # risk-free rate subtracted from returns in fixtures
SORTINO_TARGET: float = 0.0  # minimum acceptable return
SORTINO_DENOMINATOR: str = "all_n"  # downside variance over all N returns

# ── Default fixture tolerances ────────────────────────────────────────────────

# BS price/Greeks vs py_vollib oracle
BS_ATOL: float = 1e-10
BS_RTOL: float = 0.0

# BS canary cross-check vs QuantLib
BS_CANARY_ATOL: float = 1e-10
BS_CANARY_RTOL: float = 0.0

# SVI total variance on moneyness grid
SVI_VARIANCE_ATOL: float = 1e-4
SVI_VARIANCE_RTOL: float = 0.0

# Engine statistics (hand-computed series are exact)
ENGINE_STATS_ATOL: float = 1e-9
ENGINE_STATS_RTOL: float = 0.0

# Accumulated PnL
PNL_ATOL: float = 1e-6
PNL_RTOL: float = 0.0

# NOTE: 1e-12 is intentionally excluded. Cross-library transcendental comparisons
# (scipy vs QuantLib vs py_vollib) diverge at that level between Linux x86_64 CI
# and Windows dev boxes. The minimum certified tolerance for cross-library float
# comparisons is 1e-10.

# ── Timestamp ─────────────────────────────────────────────────────────────────

TIMESTAMP_UNIT: str = "ms"  # milliseconds since Unix epoch UTC
TIMESTAMP_DTYPE: str = "int64"
