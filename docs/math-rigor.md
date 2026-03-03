# Math Rigor — Options System Upgrade Plan

## Overview

Ten upgrades ordered by impact-to-effort ratio. Each section specifies the mathematical correction, exact files/lines affected, implementation steps, and testing criteria.

---

## Upgrade 1: Variance Interpolation (not Volatility)

**Impact**: HIGH | **Effort**: LOW | **Risk**: LOW

### Problem

Current interpolation in `iv_builder.py:154-164` is linear in σ:

```
σ_30 = w_low · σ_low + w_high · σ_high
```

This is first-order correct but introduces **downward bias** when the term structure has curvature. By Jensen's inequality, for a convex function (√x):

```
√(w₁x₁ + w₂x₂) ≥ w₁√x₁ + w₂√x₂
```

So linear-in-vol systematically underestimates the true 30-day IV.

### Correct Form

Interpolate in **total variance** (σ²T), then extract σ:

```
σ²(T_30) · T_30 = w_low · σ²_low · T_low + w_high · σ²_high · T_high

where:
  w_low  = (T_high - T_30) / (T_high - T_low)
  w_high = (T_30 - T_low)  / (T_high - T_low)
  T_x    = DTE_x / 365

σ_30 = √[ (w_low · σ²_low · T_low + w_high · σ²_high · T_high) / T_30 ]
```

This is **variance-time** interpolation — the industry standard for constructing constant-maturity vol surfaces.

### Files & Changes

| File | Lines | Change |
|------|-------|--------|
| `PythonDataService/app/research/options/iv_builder.py` | 154-164 | Rewrite `_interpolate_iv()` |

### Implementation

```python
def _interpolate_iv(
    iv_low: float, dte_low: int, iv_high: float, dte_high: int
) -> float:
    """30-day constant-maturity interpolation in variance-time space."""
    if dte_high == dte_low:
        return (iv_low + iv_high) / 2

    t_low = dte_low / 365
    t_high = dte_high / 365
    t_target = TARGET_DTE / 365

    w_low = (t_high - t_target) / (t_high - t_low)
    w_high = (t_target - t_low) / (t_high - t_low)

    total_var = w_low * iv_low**2 * t_low + w_high * iv_high**2 * t_high
    return math.sqrt(total_var / t_target)
```

### Tests

- **Flat term structure**: `iv_low == iv_high` → result unchanged
- **Upward-sloping**: variance interpolation ≥ linear interpolation
- **Symmetric brackets** (e.g., DTE 20 & 40): verify against hand calculation
- **Edge case**: `dte_low == dte_high` → still returns average
- **Regression**: run full IV build for 1 ticker, compare old vs new (expect small upward shift)

---

## Upgrade 2: Drop √T Single-Bracket Fallback

**Impact**: MEDIUM | **Effort**: TRIVIAL | **Risk**: LOW

### Problem

`iv_builder.py:167-171` normalizes via:

```
IV_30 ≈ IV_obs · √(30 / DTE)
```

This assumes volatility scales as √T, which requires:
- Flat term structure (empirically false)
- No skew shift with maturity (empirically false)
- IID returns (empirically false — autocorrelation, clustering)

This introduces **systematic bias** — over-estimates when DTE < 30 (short-dated vol is typically higher), under-estimates when DTE > 30.

### Fix

Return `None` when only one bracket exists. The forward-fill logic (limit=2 days) will cover short gaps. Longer gaps are correctly reported as missing by diagnostics.

### Files & Changes

| File | Lines | Change |
|------|-------|--------|
| `iv_builder.py` | 167-171 | `_normalize_iv_fallback()` returns `None` always |
| `iv_builder.py` | ~278, ~303, ~328 | Callsites already handle `None` — verify |

### Implementation

```python
def _normalize_iv_fallback(iv: float, dte: int) -> float | None:
    """Single-bracket fallback — drop rather than introduce bias."""
    return None
```

### Tests

- Verify days with single bracket now produce `NaN` in output
- Verify forward-fill covers 1-2 day gaps
- Run diagnostics on same ticker — confirm `missing_pct` increase is modest (<5% typical)
- Verify research pipeline still passes validation gate

---

## Upgrade 3: Narrow Bracket Window (20–45 DTE)

**Impact**: MEDIUM | **Effort**: TRIVIAL | **Risk**: LOW

### Problem

`contract_finder.py:127-128` searches 14–60 DTE. This allows extreme asymmetry:

```
Example: DTE_low = 15, DTE_high = 58
Interpolation point (30) is 15 days from low, 28 days from high
The "short" bracket dominates — fragile extrapolation
```

### Fix

Tighten to 20–45 DTE, enforcing max |DTE − 30| ≤ 15.

### Files & Changes

| File | Lines | Change |
|------|-------|--------|
| `contract_finder.py` | 127-128 | Change `timedelta(days=14)` → `20`, `timedelta(days=60)` → `45` |

### Implementation

```python
search_start = trade_date + timedelta(days=20)   # was 14
search_end = trade_date + timedelta(days=45)      # was 60
```

### Tests

- Verify no brackets returned have DTE < 20 or DTE > 45
- Run full IV build — confirm missing data increase is acceptable
- Spot-check interpolation weights: both should be in [0.25, 0.75] range (balanced)

### Risk Mitigation

If 20–45 causes too many missing days for illiquid underlyings, fall back to 18–50 as a compromise. Track missing-day delta per ticker in diagnostics.

---

## Upgrade 4: Dynamic Risk-Free Rate from FRED

**Impact**: HIGH | **Effort**: MEDIUM | **Risk**: LOW

### Problem

`bs_solver.py:8` hardcodes `r = 0.043`. IV sensitivity to r:

```
∂σ/∂r ≈ −(∂C/∂r) / vega = K·T·e^(−rT)·N(d₂) / vega
```

For a 60-DTE ATM option: ~50bps rate error → ~0.3-0.5 vol point IV error. This is **systematic** — it biases the entire term structure in one direction.

### Architecture

```
FRED API (DTB3 series)
  │
  ▼
New: fred_rates.py (Python service)
  │  Fetch daily 3-month T-bill rate
  │  Cache in-memory with 24h TTL
  │  Interpolate to arbitrary DTE
  │
  ▼
bs_solver.py
  │  Accept r as parameter (already does)
  │  Remove RISK_FREE_RATE constant
  │
  ▼
iv_builder.py
  │  Fetch rate for each trading day + DTE
  │  Pass to implied_volatility()
```

### Files & Changes

| File | Change |
|------|--------|
| `PythonDataService/app/research/options/fred_rates.py` | **NEW** — FRED client + interpolation |
| `PythonDataService/app/research/options/bs_solver.py` | Remove `RISK_FREE_RATE` constant; keep `r` param on all functions |
| `PythonDataService/app/research/options/iv_builder.py` | Inject rate provider; fetch `r` per day+DTE |
| `PythonDataService/requirements.txt` | Add `fredapi` or use raw HTTP to FRED |

### Implementation Detail: `fred_rates.py`

```python
from __future__ import annotations
import httpx
import pandas as pd
from datetime import date, timedelta
from functools import lru_cache

FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"
SERIES_MAP = {
    30:  "DTB4WK",   # 4-week T-bill
    90:  "DTB3",     # 3-month T-bill
    180: "DTB6",     # 6-month T-bill
    365: "DTB1YR",   # 1-year T-bill
}

class TreasuryRateProvider:
    """Fetches and interpolates US Treasury rates from FRED."""

    def __init__(self, api_key: str):
        self._api_key = api_key
        self._cache: dict[str, pd.Series] = {}

    def get_rate(self, as_of: date, dte: int) -> float:
        """Return annualized risk-free rate interpolated to DTE."""
        # Find bracketing tenors
        tenors = sorted(SERIES_MAP.keys())
        # Clamp DTE to available range
        dte_clamped = max(tenors[0], min(dte, tenors[-1]))

        # Find bracket
        low_tenor = max(t for t in tenors if t <= dte_clamped)
        high_tenor = min(t for t in tenors if t >= dte_clamped)

        r_low = self._fetch_rate(SERIES_MAP[low_tenor], as_of)
        if low_tenor == high_tenor or r_low is None:
            return (r_low or 0.043) / 100  # FRED returns percentage

        r_high = self._fetch_rate(SERIES_MAP[high_tenor], as_of)
        if r_high is None:
            return (r_low or 0.043) / 100

        # Linear interpolation between tenors
        w = (dte_clamped - low_tenor) / (high_tenor - low_tenor)
        rate_pct = r_low + w * (r_high - r_low)
        return rate_pct / 100  # Convert from percentage to decimal

    def _fetch_rate(self, series_id: str, as_of: date) -> float | None:
        """Fetch rate from FRED with 7-day lookback for holidays."""
        # Implementation: HTTP GET with caching
        ...
```

### Integration into `iv_builder.py`

```python
# In build_iv_history():
rate_provider = TreasuryRateProvider(api_key=settings.fred_api_key)

# Per-day loop:
r = rate_provider.get_rate(as_of=trade_date, dte=dte_low)
iv = implied_volatility(price, S, K, T, r, option_type)
```

### Fallback

If FRED is unavailable or rate is missing, fall back to `r = 0.043` with a warning log. Never fail the IV build due to rate fetch failure.

### Tests

- Mock FRED responses, verify interpolation between 30-day and 90-day tenors
- Verify rate of 4.3% produces identical results to current hardcoded value
- Verify rate sensitivity: compute IV at r=0.03, r=0.043, r=0.055 — confirm monotonic shift
- Integration test: build IV for 1 ticker with live FRED data

---

## Upgrade 5: Delta-Based Skew Strikes

**Impact**: HIGH | **Effort**: HIGH | **Risk**: MEDIUM

### Problem

`contract_finder.py:20` uses `OTM_OFFSET_PCT = 0.05` (fixed 5% OTM). This means:

- For a $100 stock: 25Δ put might be at ~$92 (8% OTM), but we pick $95
- For a $500 stock: 25Δ put might be at ~$465 (7% OTM), but we pick $475
- The "skew" we measure depends on moneyness, not delta — not comparable across underlyings or time

Industry standard: **25-delta put** and **25-delta call** for skew measurement.

### Mathematical Challenge

Delta depends on IV, which we're trying to solve for — circular dependency. Resolution:

```
1. Fetch all available strikes for the expiry
2. Compute IV for each strike (BS solver)
3. Compute delta for each strike using its IV
4. Select strikes closest to 25Δ put and 25Δ call
```

This requires solving IV for **multiple strikes** instead of just one — more API calls but more correct.

### Files & Changes

| File | Change |
|------|--------|
| `contract_finder.py` | Replace `OTM_OFFSET_PCT` with delta-based selection |
| `contract_finder.py` | `_fetch_contracts_for_expiry()` → return more candidates |
| `iv_builder.py` | New `_select_delta_strikes()` function |
| `bs_solver.py` | Add `bs_delta(S, K, T, r, sigma, option_type)` function |

### Implementation: `bs_delta()`

```python
def bs_delta(S: float, K: float, T: float, r: float,
             sigma: float, option_type: str) -> float:
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    if option_type == "call":
        return norm.cdf(d1)
    return norm.cdf(d1) - 1  # put delta is negative
```

### Implementation: Strike Selection

```python
def _select_delta_strikes(
    contracts: list, stock_close: float, dte: int, r: float,
    target_delta: float = 0.25
) -> tuple[str | None, str | None]:
    """Select put and call strikes closest to target delta."""
    T = dte / 365
    put_candidates = []
    call_candidates = []

    for c in contracts:
        K = c.strike_price
        # Quick IV estimate for delta calc (Brenner-Subrahmanyam)
        approx_iv = max(0.15, min(1.0, 0.4))  # or use ATM IV if available

        if c.contract_type == "put":
            d = abs(bs_delta(stock_close, K, T, r, approx_iv, "put"))
            put_candidates.append((abs(d - target_delta), c))
        else:
            d = bs_delta(stock_close, K, T, r, approx_iv, "call")
            call_candidates.append((abs(d - target_delta), c))

    put_strike = min(put_candidates, key=lambda x: x[0])[1] if put_candidates else None
    call_strike = min(call_candidates, key=lambda x: x[0])[1] if call_candidates else None
    return put_strike, call_strike
```

### Refinement

For higher accuracy, iterate:
1. Use ATM IV as initial approx_iv for all strikes
2. Solve IV for the selected 25Δ strikes
3. Recompute delta with solved IV
4. Re-select if delta moved significantly (|Δ_new − 0.25| > 0.05)

One iteration is typically sufficient.

### Tests

- For flat vol surface: delta-based strikes should match fixed-% strikes approximately
- For steep skew: delta-based put should be further OTM than 5%
- Cross-underlying comparison: verify 25Δ strikes for SPY vs TSLA are meaningfully different %OTM
- Regression: compare old vs new skew series for correlation (should be >0.9)

---

## Upgrade 6: Synthetic Forward for ATM Strike

**Impact**: MEDIUM | **Effort**: MEDIUM | **Risk**: LOW

### Problem

ATM IV is derived from call IV only. In skewed markets, call IV ≠ put IV at the same strike. The stock close price may not equal the forward price (dividends, borrow cost).

### Correct Approach

**Put-call parity** gives the synthetic forward:

```
F = K + e^(rT) · (C − P)
```

where C, P are call/put prices at the same strike K.

Then ATM is defined as the strike closest to F, and ATM IV is the average:

```
σ_ATM = (σ_call(K_ATM) + σ_put(K_ATM)) / 2
```

Or more precisely, solve IV from the **undiscounted straddle price**:

```
Straddle = C + P
σ_ATM = IV_solve(Straddle/2, F, K_ATM, T, r, "call")  # approximately
```

### Files & Changes

| File | Change |
|------|--------|
| `contract_finder.py` | Fetch both ATM call AND ATM put contracts |
| `iv_builder.py` | Compute synthetic forward; average call/put IV for ATM |

### Tests

- Verify F ≈ S when r is small and DTE is short
- Verify ATM IV from straddle vs call-only: measure difference across tickers

---

## Upgrade 7: Flag (Don't Drop) Low-Liquidity Days

**Impact**: MEDIUM | **Effort**: LOW | **Risk**: LOW

### Problem

Current filters (volume ≥ 50, OI ≥ 100) silently drop data points. During stress events, liquidity dries up — exactly when IV spikes are most informative. Dropping these creates **survivorship bias** in the IV series.

### Fix

Add a `quality_flag` column to the IV output:

```python
quality_flags = {
    "high":   volume >= 50 and OI >= 100 and spread <= 10%,
    "medium": volume >= 10 and OI >= 25,
    "low":    everything else that produces a valid IV,
}
```

Keep all data, let diagnostics and research decide how to filter.

### Files & Changes

| File | Change |
|------|--------|
| `iv_builder.py` | Relax hard filters → soft quality flags |
| `iv_builder.py` | Add `quality_flag` column to output DataFrame |
| `diagnostics.py` | Report quality distribution in diagnostics |
| `options_runner.py` | Option to filter by quality in research |
| `Backend/Models/MarketData/OptionsIvSnapshot.cs` | Add `QualityFlag` column |

### Tests

- Verify high-quality days produce identical IV to current system
- Verify low-quality days now appear in output (previously dropped)
- Run diagnostics — confirm quality distribution reporting works

---

## Upgrade 8: Strict Price Source Hierarchy

**Impact**: LOW | **Effort**: TRIVIAL | **Risk**: LOW

### Problem

`iv_builder.py` `_get_option_price()` falls back to close price. Options close prices often print at bid or ask edge — not representative of fair value.

### Current hierarchy

1. Midpoint (if bid > 0 and ask > 0 and mid ≥ $0.05)
2. Close (if volume ≥ 50)

### Improved hierarchy

1. Midpoint (if bid > 0 and ask > 0 and spread/mid ≤ 15% and mid ≥ $0.05)
2. VWAP if available (volume-weighted average price)
3. Close (if volume ≥ 100 AND close is within [bid, ask] range)
4. **Reject** — don't use stale/edge prints

### Files & Changes

| File | Lines | Change |
|------|-------|--------|
| `iv_builder.py` | `_get_option_price()` | Tighten acceptance criteria |

### Tests

- Verify midpoint with tight spread passes
- Verify midpoint with wide spread (>15%) is rejected
- Verify close outside bid-ask range is rejected

---

## Upgrade 9: Newey-West Lag Validation for IC

**Impact**: MEDIUM | **Effort**: LOW | **Risk**: LOW

### Problem

IC significance uses Newey-West HAC with lag ≥ 5. But optimal lag depends on sample size and autocorrelation structure.

### Fix

Use **Newey-West automatic bandwidth selection**:

```
lag = floor(4 · (n/100)^(2/9))     # Andrews (1991) rule
```

Or use `statsmodels` auto-lag:

```python
from statsmodels.stats.stattools import durbin_watson
# Use Bartlett kernel with automatic bandwidth
```

Also compute and report **effective sample size**:

```
n_eff = n / (1 + 2 · Σ_{k=1}^{K} ρ_k)
```

where ρ_k is the autocorrelation at lag k.

### Files & Changes

| File | Change |
|------|--------|
| `PythonDataService/app/research/options_runner.py` | Auto-lag selection, report n_eff |
| `PythonDataService/app/research/core/statistics.py` | Add `effective_sample_size()` if not present |

### Tests

- Verify auto-lag produces lag ≥ 5 for typical sample sizes (~250-500 days)
- Verify n_eff < n when autocorrelation exists
- Verify p-values are wider (more conservative) with proper lag

---

## Upgrade 10: Forward RV Namespace Isolation

**Impact**: LOW (correctness) | **Effort**: LOW | **Risk**: HIGH if not done

### Problem

`vrp_5` in "research" mode uses forward-looking RV. If this ever leaks into signal mode, the entire backtest is invalidated by look-ahead bias.

### Current State

Separated by `mode` parameter in `compute_vrp()`. But same function, same DataFrame, same API endpoint.

### Fix

- Rename research-mode feature to `vrp_5_forward` explicitly
- Add runtime assertion: signal-mode features cannot access forward data
- Log a WARNING if forward features are requested outside research context

### Files & Changes

| File | Change |
|------|--------|
| `options_features.py` | Rename research VRP; add mode assertion |
| `options_runner.py` | Use explicit `vrp_5_forward` name in research pipeline |

### Tests

- Verify `compute_vrp(mode="signal")` raises if forward data columns present
- Verify `vrp_5_forward` label appears in research output, never in signal output

---

## Execution Order

```
Phase 1 — Quick Wins (1 session)
  ├── Upgrade 1: Variance interpolation
  ├── Upgrade 2: Drop √T fallback
  └── Upgrade 3: Narrow bracket window

Phase 2 — Infrastructure (1-2 sessions)
  ├── Upgrade 4: Dynamic risk-free rate (FRED)
  └── Upgrade 8: Strict price hierarchy

Phase 3 — Correctness (1-2 sessions)
  ├── Upgrade 6: Synthetic forward for ATM
  ├── Upgrade 7: Quality flags (not hard drops)
  └── Upgrade 10: Forward RV isolation

Phase 4 — Institutional Grade (2-3 sessions)
  ├── Upgrade 5: Delta-based skew strikes
  └── Upgrade 9: Newey-West auto-lag
```

---

## Validation After All Upgrades

1. **Regression comparison**: Build IV for 5 tickers (SPY, AAPL, TSLA, NVDA, META) under old and new system. Report per-ticker:
   - Mean IV shift (expect small upward from variance interpolation)
   - Missing day delta (expect increase from dropping √T fallback)
   - Correlation between old and new series (expect > 0.98)

2. **Research re-run**: Run all 5 features × 5 tickers through the research pipeline. Verify validation gate still passes for previously-passing features.

3. **Diagnostics**: Confirm all tickers still pass diagnostics with tightened window (20–45 DTE).
