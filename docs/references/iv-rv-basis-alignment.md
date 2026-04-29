# IV–RV basis alignment

**Status:** Locked 2026-04-26. Step 1 of the 8-step IV-RV alignment plan.

## Problem

Our IV solver returns σ on **ACT/365** basis (`TTM = calendar_days / 365`,
the QuantLib / market-screen convention). Our realized-vol pipeline annualizes
per-bar variance with **√252**, giving σ on **TRD/252** basis.

`vrp.compute_vrp(iv, rv)` does `iv² − rv²` directly. If `iv` is ACT/365 and
`rv` is TRD/252, that subtraction silently mixes bases.

## Bias size

Under the practitioner assumption that variance accrues only on trading
days (zero on weekends/holidays — the assumption that makes √252-annualized
RV well-defined), the conversion factor for a tenor of `D` calendar days
that contains `N` NYSE trading sessions is:

```
σ²_TRD252 · (N / 252) = σ²_ACT365 · (D / 365)        ← equate total variance
σ_TRD252 = σ_ACT365 · √((D · 252) / (365 · N))
```

For SPY IV30 with typical N=21 (e.g., 2024-03-04 + 30 calendar days):

```
factor² = (30 · 252) / (365 · 21) = 7560 / 7665 ≈ 0.9863
factor  ≈ 0.9931  →  σ_TRD252 is ~0.7% lower than σ_ACT365
```

For a holiday-heavy 30-day window (e.g., 2024-12-23 + 30 calendar days
covers Christmas, New Year's, the National Day of Mourning for Jimmy Carter
on 2025-01-09, and MLK Day → N=18):

```
factor² = (30 · 252) / (365 · 18) = 7560 / 6570 ≈ 1.1507
factor  ≈ 1.0727  →  σ_TRD252 is ~7.3% HIGHER than σ_ACT365
```

The sign of the bias flips with N. A static `√(365/252) ≈ 1.215` constant
correction would be wrong in both directions for our data shape — only
the dynamic NYSE-calendar conversion gives the right answer.

## Decision

**Path B, dynamic.** Convert IV from ACT/365 to TRD/252 at the boundary
before passing to `vrp.compute_vrp`. The IV solver, BS Greeks, and surface
fitting code remain ACT/365 internally (matching QuantLib, py_vollib, and
vendor screens). Only the VRP comparison and any downstream regime-feature
use sees TRD/252.

The alternative — making everything 252-basis everywhere — was rejected
because the resulting IV30 numbers would not match what users see on
Bloomberg / Polygon screens, which is a real ergonomic loss with no
mathematical gain.

## Implementation (this step)

| File | Change |
|---|---|
| `app/volatility/basis.py` | New: `convert_iv_act365_to_trading252`, inverse, `nyse_trading_days_in_window` |
| `app/volatility/conventions.py` | New constants `TRADING_DAYS_PER_YEAR=252`, `CALENDAR_DAYS_PER_YEAR=365` |
| `app/engine/edge/features_realtime/iv30_constructor.py` | New `iv30_atm_50d_trading_basis()` wrapper |
| `app/engine/edge/vrp.py` | Docstring: explicit basis contract on inputs |
| `tests/volatility/test_basis.py` | New: NYSE day count + conversion + round-trip + VRP differs by basis |

`pandas_market_calendars` (NYSE schedule) was already present in
`requirements-light.txt` — no new dependency.

## Wiring (deferred to Step 3)

The router `app/routers/edge.py` still calls `iv30_atm_50d` (ACT/365) and
passes that into `compute_vrp` against TRD/252 RV — the bug is still live.
Step 3 (HF realized variance) replaces both the RV input *and* swaps in
`iv30_atm_50d_trading_basis` so the two sides finally agree. This step
adds the conversion infrastructure with passing tests; Step 3 lands the
fix into the production pipeline.

## What this does NOT fix

| Issue | Step that addresses it |
|---|---|
| Risk-free rate / dividend hardcoded `0.0` | Step 2 |
| RV estimator on chart vs RV used for VRP | Step 3 |
| No external authority for IV30 | Step 4 (VIX-style replication) |
| Solver parity vs py_vollib | Step 5 |
| IV30 stability under perturbation | Step 6 |
| ETH/RTH UI toggle | Step 7 |
| pricing-lab / strategy-builder hardcoded `r=0.043` | Step 8 |

## Open: overnight-component noise (post-recorder calibration)

The HF two-component RV estimator (`hf_realized_vol.py`) uses a single
squared overnight log-return per session. That term is itself a
high-variance estimator of overnight integrated variance (Hansen–Lunde
2005, Martens 2002). For SPY, NBER w17422 puts overnight at ~30% of total
variance — non-trivial.

This is **not a basis-conversion bug**: IV30 and RV30 are both
overnight-inclusive on our pipeline, so the comparison is on the same
canvas. The remaining concern is *noise*, not *bias*. A noisier RV makes
the VRP statistic noisier (wider z-score CI, weaker signal-to-noise on
the lookback) without biasing it.

Candidate fixes (all post-recorder, after we have empirical evidence
overnight noise dominates the VRP signal):

- Multi-day pooling of the overnight component (rolling mean over N
  sessions, accept the lag).
- Hansen–Lunde-weighted overnight estimator.
- Two-scale RV that explicitly separates overnight from intraday and
  weights inverse to estimator variance.

Tracked here for awareness; no code change planned until forward
recorder data shows the overnight piece is the dominant noise source.
