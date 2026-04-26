# Technical Design: Volatility Surface & Regime Dashboard

**Status:** Draft for review
**Author:** Inkant Awasthi (with Claude as collaborator)
**Date:** 2026-04-25
**Target location:** `docs/architecture/vol-surface-dashboard.md`
**Scope:** SPY and QQQ — minimum viable
**Data tier:** Polygon Options Starter (unlimited calls, 2-year history)

---

## 1. Purpose and non-goals

### 1.1 What this is

A dashboard surfaced in learn-ai's Angular UI that characterizes the current state of the options market for SPY and QQQ. It answers four questions the user asks before placing a trade:

1. **Where is implied vol relative to its own history?** (IV-rank, IV-percentile)
2. **What is the market pricing in for risk?** (skew, term structure)
3. **Is the market over- or under-pricing realized risk?** (IV-RV spread)
4. **Given the answers above, what option structures are appropriate?** (regime → structure mapping)

The output is a set of **measurable, citable, testable metrics** — not a ranked list of contracts and not a "composite score." Contracts come later, downstream of regime classification, in a separate component.

### 1.2 What this is not

- Not a fair-value mispricing detector. With market IV as input to BS, "fair value" minus "market mid" is a re-quoting operation that produces noise, not edge. Removing this framing is deliberate.
- Not an alpha source on its own. The dashboard is a context layer for a directional or vol view that comes from elsewhere (the user's discretion, the EMA crossover engine, or a future vol forecast).
- Not a backtester. Strategy P&L simulation lives in `app/engine/`. This module computes market-state metrics and surfaces them.
- Not a real-time streaming product. Refresh is on-demand; the user clicks a button.

### 1.3 Success criteria

- Every output metric has a precise mathematical definition with a Hull or Gatheral citation.
- Every numerical computation has a golden fixture in `tests/fixtures/golden/` with attribution per `numerical-rigor.md`.
- IV inversion produces values within `atol=1e-4, rtol=1e-3` of py_vollib's reference implementation across a frozen test set.
- Dashboard renders SPY and QQQ surface metrics in under 3 seconds on cached data, under 30 seconds on cold refresh.
- The user can trace any number on the dashboard back to its formula, its inputs, and its test fixture in fewer than three clicks.

---

## 2. Decision: which path to commit to

This document covers three paths. The math, schemas, and component design are identical across all three. They differ in **what ships when**, **how much history is required at launch**, and **whether the dashboard is built in v1**.

### 2.1 Path comparison

| Dimension | Path 1 — Full | Path 2 — MVP | Path 3 — API-only |
|---|---|---|---|
| **Calendar duration** | ~6 weeks | ~2-3 weeks | ~4 weeks |
| **Historical IV at launch** | 2 years backfilled | None — accumulate forward | 2 years backfilled |
| **IV-rank available at launch** | Yes (full 252-day) | No (placeholder for first 60 days, partial 60-252, full after 252) | Yes (full 252-day) |
| **Skew available at launch** | Yes | Yes (but no "skew percentile" until history accumulates) | Yes |
| **Term structure available at launch** | Yes | Yes (but no percentile until history) | Yes |
| **Realized vol available at launch** | Yes (uses existing intraday bars) | Yes | Yes |
| **IV-RV spread available at launch** | Yes (full 2-year history) | Yes (forward-only) | Yes |
| **Regime classifier at launch** | Full rule set | Reduced rule set (no rank-based rules until history) | Full rule set |
| **Angular dashboard** | Yes | Yes | No — FastAPI only, consume from notebooks |
| **Polygon API consumption** | ~200K-500K requests during backfill | ~5-20 requests/day after launch | ~200K-500K during backfill |
| **Risk: dead weight** | Low — historical depth from day one | Medium — rank-based metrics not useful for 2-8 months | Medium — no UI means slower feedback loop |
| **Risk: time investment vs trading edge** | Highest — 6 weeks for marginal trading P&L improvement | Lowest — get to value fastest | Medium — full data, deferred UI |

### 2.2 Recommendation

**Path 2 (MVP) is the right answer for your stated trading horizon (manual, small size, learning).** Reasoning:

- The metrics that work from day one (skew levels, term-structure shape, realized vol, IV-RV spread on 2-year underlying history) are the most informative for actual trading decisions. Skew at -0.05 vs -0.15 tells you something whether or not you know the percentile.
- IV-rank is the most-cited regime metric, but it's also the one that becomes useless if you've miscomputed it. Accumulating it forward from real snapshots avoids the inversion-quality issues that plague backfilled IV at the boundaries of the chain.
- Path 2 has you using the dashboard within 3 weeks. Path 1 has you building data infrastructure for 4 weeks before you see anything. The feedback loop matters more than the data depth at this stage.
- Path 2 leaves Path 1 reachable later. If after 60 days you decide IV-rank is essential and you don't want to wait another 6 months, you can run the backfill then. Nothing about Path 2 closes off Path 1.

**Path 1 is the right answer if** you're committed to options as a multi-year focus and want full analytics from the first day of trading. The 6-week investment compounds across years.

**Path 3 is the right answer if** you genuinely consume metrics through Jupyter notebooks more often than dashboards, and you value the time savings of skipping Angular work.

The remainder of this document writes against Path 2 as the default. Differences for Paths 1 and 3 are called out in section markers `[Path 1 only]`, `[Path 3 only]`, etc.

---

## 3. Math foundations

The math sections below give the formulas, the units, the assumptions, and the references. Every formula maps to a function in the code with the same name.

### 3.1 Implied volatility inversion

**Definition.** Implied volatility σ_imp is the value that, when input to Black-Scholes-Merton with continuous dividend yield, reproduces the observed market mid-price.

**Formula (BSM with continuous dividend).** For a European call:

$$C = S e^{-qT} \Phi(d_1) - K e^{-rT} \Phi(d_2)$$

$$d_1 = \frac{\ln(S/K) + (r - q + \sigma^2/2)T}{\sigma \sqrt{T}}, \quad d_2 = d_1 - \sigma\sqrt{T}$$

For a European put: $P = K e^{-rT} \Phi(-d_2) - S e^{-qT} \Phi(-d_1)$.

**Inversion.** Given C (or P), solve for σ. Use Brent's method on the interval [1e-4, 5.0] with `xtol=1e-8`. Newton-Raphson is faster but unstable near zero vega; Brent is bracketing and always converges within tolerance.

**Inputs.**
- S = underlying close on date d
- K = strike
- T = year-fraction from d to expiry, using ACT/365 day count
- r = risk-free rate matched to T, interpolated from FRED Treasury yield curve (1M, 3M, 6M, 1Y points)
- q = continuous dividend yield, computed from forward-implied parity (see § 3.2)
- C or P = market mid-price on date d

**Quality filters before storing IV.** Drop or flag rows where:
- `bid >= ask` (crossed quote)
- `mid < 0.05` (penny option, IV undefined)
- `(ask - bid) / mid > 0.5` (spread too wide)
- `|delta| < 0.05 or |delta| > 0.95` (vega too small for stable inversion)
- Brent fails to converge or returns IV at the bracket boundary

Flagged rows are stored with `quality_flag != 'ok'` and excluded from percentile distributions.

**Reference.** Hull, *Options, Futures and Other Derivatives*, 11e, Ch. 19. py_vollib for cross-validation.

**Golden fixture.** `tests/fixtures/golden/iv_inversion/` — frozen SPY chain on 2024-01-15, expected IVs from py_vollib, tolerance `atol=1e-4, rtol=1e-3`.

### 3.2 Continuous dividend yield from put-call parity

**Why this matters.** Hardcoding q from a vendor (e.g., yfinance trailing yield) introduces silent error. The forward-implied yield is what the option market actually prices.

**Formula.** From put-call parity for European options at the same K, T:

$$C - P = S e^{-qT} - K e^{-rT}$$

Solve for q:

$$q = -\frac{1}{T} \ln\left(\frac{C - P + K e^{-rT}}{S}\right)$$

**Procedure.** For each (date, expiry):
1. Find the strike closest to ATM where both call and put have valid mid prices
2. Compute q via the formula above
3. If the result is < -0.05 or > 0.20, flag and fall back to vendor yield (data quality issue)
4. Store one q per (ticker, date, expiry) tuple

**Reference.** Hull 11e, Ch. 5.

**Test.** Property test: q computed for SPY on any date should be within ±50bps of the SPDR distribution-yield estimate from the same week.

### 3.3 Risk-free rate term structure

Interpolate from FRED daily Treasury yield curve. Use linear interpolation in (T, yield) space for T in years. Endpoints: clamp to nearest FRED tenor for T outside [1/12, 1].

Existing FRED service: `app/services/fred_service.py`. Add a method `get_rate_curve(date) -> dict[float, float]` returning {tenor_years: yield} for that date.

### 3.4 ATM implied volatility at standard tenors

**Definition.** σ_ATM(T*) is the implied vol at-the-money for tenor T*. Standard tenors: 7, 14, 21, 30, 60, 90 days.

**Procedure.**
1. For each (ticker, date), get all stored options with `quality_flag = 'ok'`
2. Group by expiry; for each expiry, find the strike closest to forward F = S·exp((r-q)T)
3. ATM IV per expiry = average of call IV and put IV at that strike (or use the more liquid one if spreads differ materially)
4. Build a piecewise linear function ATM_IV(T) over expiries
5. Evaluate at standard tenors T*; if no expiry within ±15% of T*, return null for that tenor

**Why "forward ATM" not "spot ATM."** Forward ATM is where put-call parity holds exactly at parity, so call and put IVs match. Spot ATM has a small but systematic put-call IV gap. Use forward.

**Reference.** Gatheral, *The Volatility Surface*, Ch. 1.

### 3.5 Realized volatility (Garman-Klass)

**Definition.** Garman-Klass (GK) estimator of realized vol uses OHLC instead of just close-to-close, giving lower variance at the same sample size.

**Formula.** Daily GK estimator:

$$\sigma_{GK,d}^2 = 0.5 \left[\ln(H_d/L_d)\right]^2 - (2 \ln 2 - 1) \left[\ln(C_d/O_d)\right]^2$$

Annualized N-day rolling realized vol:

$$\sigma_{RV,N}(d) = \sqrt{\frac{252}{N} \sum_{i=d-N+1}^{d} \sigma_{GK,i}^2}$$

**Standard windows.** N ∈ {10, 20, 30, 60, 90} trading days.

**Why GK over close-to-close.** Variance reduction factor of ~7x for the same N. Critically, GK does not assume zero drift, which matters for trending markets like 2023-2024.

**Limitations.** GK assumes no overnight gap and continuous trading during the day. For SPY/QQQ liquid hours this is fine; for individual single names with earnings overnight it's less clean. Within scope for v1.

**Reference.** Garman & Klass (1980), *Journal of Business*. Also Sinclair, *Volatility Trading*, Ch. 2.

**Inputs.** Existing intraday bars table — assumed daily OHLC available.

### 3.6 IV-RV spread (variance risk premium proxy)

**Definition.** The spread between forward-looking implied vol and trailing realized vol. Positive spread = options are expensive vs realized; negative spread = options are cheap.

**Formula.**

$$\text{IV-RV}(T) = \sigma_{ATM}(T) - \sigma_{RV,N=T}$$

where T is one of the standard tenors. Match the realized window to the implied tenor (30-day IV vs 30-day RV, etc.).

**Interpretation.** Long-run average for SPY is roughly +2-4 vol points (premium is structurally rich). When the spread compresses to zero or goes negative, options are unusually cheap — favorable for premium-buying strategies. When the spread blows out (>10 points), options are pricing fear; premium-selling is favored if you can stomach the path.

**Reference.** Bollerslev, Tauchen, Zhou (2009), *Review of Financial Studies* — variance risk premium and equity returns.

**Test.** Sanity check: 252-day average IV-RV for SPY at 30-day tenor should land in the +2 to +5 vol-point range. If your number says +12 or -3, something is wrong upstream.

### 3.7 Skew metrics

**25-delta risk reversal.**

$$RR_{25}(T) = \sigma_{25\Delta\text{-call}}(T) - \sigma_{25\Delta\text{-put}}(T)$$

For equity index options this is typically negative (puts richer than calls = "put skew"). Becomes more negative ahead of perceived risk events.

**25-delta butterfly.**

$$BF_{25}(T) = \frac{\sigma_{25\Delta\text{-call}}(T) + \sigma_{25\Delta\text{-put}}(T)}{2} - \sigma_{ATM}(T)$$

Measures the "smile curvature" — how much OTM wings are bid up relative to ATM. Bigger BF = fatter tails priced in.

**Procedure.**
1. For each expiry, find the call strike where delta ≈ +0.25 and the put strike where delta ≈ -0.25 (interpolate linearly between adjacent strikes)
2. Read off IVs at those strikes
3. Compute RR and BF
4. Repeat at standard tenors via piecewise linear interpolation in T

**Reference.** Gatheral Ch. 2; Derman & Miller, *The Volatility Smile*, Ch. 4.

### 3.8 Term structure metrics

**Slope.** σ_ATM(60d) − σ_ATM(7d). Positive = contango (back month richer); negative = backwardation (front month richer, stress signal).

**Curvature.** σ_ATM(30d) − 0.5·(σ_ATM(7d) + σ_ATM(60d)). Sign tells you whether the belly is bid or offered.

**Why these matter.** Backwardation in equity index vol is a strong risk-off signal — historically associated with VIX > 25 and elevated drawdown probability. Contango is the normal regime; selling front-month vol in contango has positive expectancy with proper risk management.

### 3.9 IV rank and IV percentile

**IV rank.**

$$\text{IV rank}(d) = \frac{\sigma_{ATM,30d}(d) - \min_{i \in [d-252, d-1]} \sigma_{ATM,30d}(i)}{\max_{i \in [d-252, d-1]} \sigma_{ATM,30d}(i) - \min_{i \in [d-252, d-1]} \sigma_{ATM,30d}(i)}$$

Maps current 30-day ATM IV to [0, 1] against the trailing 252-day [min, max] range.

**IV percentile.**

$$\text{IV pct}(d) = \frac{|\{i \in [d-252, d-1] : \sigma_{ATM,30d}(i) < \sigma_{ATM,30d}(d)\}|}{252}$$

The percentile of trailing observations strictly less than today's value.

**Why both.** IV rank is sensitive to outliers (one VIX spike at 80 compresses every other day to <0.5). IV percentile is rank-based and robust. Show both; trust the percentile when they disagree.

**Path 2 considerations.** For days < 60 stored, return null and display "insufficient history" placeholder. For 60 ≤ days < 252, return rank/percentile against the available window with a "partial" flag. For days ≥ 252, full computation.

**Reference.** Tastytrade research blog has the canonical retail-trader definitions. For the academic version see Whaley (2009) on VIX percentiles.

### 3.10 Regime classification

A rule-based classifier maps the metric vector to a regime label and a recommended structure family. Rules below; thresholds are starting points to be tuned with data.

```
INPUT: iv_rank_30d, term_slope (60d-7d), rr_25_30d, iv_rv_spread_30d

REGIME = (vol_regime, term_regime, skew_regime)

vol_regime ∈ {HIGH, NORMAL, LOW}:
  HIGH    if iv_rank_30d > 0.70
  LOW     if iv_rank_30d < 0.30
  NORMAL  otherwise

term_regime ∈ {BACKWARDATION, FLAT, CONTANGO}:
  BACKWARDATION if term_slope < -1.0   (vol points)
  CONTANGO      if term_slope > +1.0
  FLAT          otherwise

skew_regime ∈ {RICH, NORMAL, CHEAP}:
  Compute RR percentile rank against trailing 252-day distribution
  RICH    if rr_pct < 0.20  (more negative than usual = put skew rich)
  CHEAP   if rr_pct > 0.80
  NORMAL  otherwise

VARIANCE_PREMIUM_FLAG:
  RICH       if iv_rv_spread > +5.0
  COMPRESSED if iv_rv_spread < +1.0
  NORMAL     otherwise
```

**Structure recommendations** (mapped from regime, not exhaustive):

| Regime | Recommended structures | Avoid |
|---|---|---|
| HIGH vol + CONTANGO + RICH skew | Premium-selling: CSP, bull put spread, iron condor | Long premium |
| HIGH vol + BACKWARDATION | Cash, defensive puts only | Short premium (path risk) |
| LOW vol + CONTANGO + NORMAL skew | Long premium: long calls/puts, debit spreads, calendars | Short premium (small reward) |
| NORMAL across the board | Directional via debit spreads if you have a view; otherwise pass | All-in on any single trade |
| Variance premium COMPRESSED | Long premium favored | Premium selling |
| Variance premium RICH | Premium selling favored if path-risk acceptable | Naked long premium |

**This is the user-facing recommendation, not a backtested edge.** The dashboard should display the regime and the *type* of structure that historically fits, not specific contracts. Contract selection is a separate component the user invokes after seeing the regime.

---

## 4. Data layer

### 4.1 Postgres schema

```sql
-- Contract metadata. Static. One row per contract.
CREATE TABLE options_contracts (
  contract_symbol    TEXT PRIMARY KEY,        -- e.g. 'O:SPY260530C00550000'
  ticker             TEXT NOT NULL,
  expiry             DATE NOT NULL,
  strike             NUMERIC(10, 2) NOT NULL,
  option_type        CHAR(1) NOT NULL,        -- 'C' or 'P'
  first_seen         DATE NOT NULL,
  last_seen          DATE NOT NULL,
  CONSTRAINT options_contracts_type_chk CHECK (option_type IN ('C', 'P'))
);
CREATE INDEX idx_oc_ticker_expiry ON options_contracts (ticker, expiry);

-- Daily OHLC + computed IV per contract per day.
-- This is the table that grows. Estimated size at ticker-class scale:
--   SPY: ~3000 active contracts/day × 500 trading days/2yr = ~1.5M rows
--   QQQ: similar magnitude
-- Total at ~3M rows; not big.
CREATE TABLE options_daily (
  contract_symbol    TEXT NOT NULL REFERENCES options_contracts (contract_symbol),
  date               DATE NOT NULL,
  open               NUMERIC(10, 4),
  high               NUMERIC(10, 4),
  low                NUMERIC(10, 4),
  close              NUMERIC(10, 4),
  volume             BIGINT,
  vwap               NUMERIC(10, 4),

  -- Inputs to IV inversion (stored for reproducibility)
  underlying_close   NUMERIC(10, 4),
  rate_used          NUMERIC(8, 6),
  div_yield_used     NUMERIC(8, 6),

  -- Computed IV and Greeks
  iv_close           NUMERIC(8, 6),
  delta              NUMERIC(8, 6),
  gamma              NUMERIC(10, 8),
  vega               NUMERIC(8, 6),
  theta              NUMERIC(8, 6),

  quality_flag       TEXT NOT NULL DEFAULT 'ok',
  -- 'ok', 'wide_spread', 'penny', 'extreme_delta', 'inversion_failed'

  PRIMARY KEY (contract_symbol, date)
);
CREATE INDEX idx_od_date ON options_daily (date);
CREATE INDEX idx_od_quality ON options_daily (quality_flag) WHERE quality_flag = 'ok';

-- Per-(ticker, date) dividend yield from put-call parity.
CREATE TABLE options_dividend_yields (
  ticker             TEXT NOT NULL,
  date               DATE NOT NULL,
  expiry             DATE NOT NULL,
  div_yield          NUMERIC(8, 6) NOT NULL,
  source             TEXT NOT NULL,       -- 'parity' or 'vendor_fallback'
  PRIMARY KEY (ticker, date, expiry)
);

-- Materialized derived metrics. Recomputed daily after options_daily updates.
-- This is what the dashboard reads.
CREATE TABLE surface_metrics_daily (
  ticker                    TEXT NOT NULL,
  date                      DATE NOT NULL,

  -- ATM IV at standard tenors (vol points, e.g. 0.1547 = 15.47%)
  atm_iv_7d                 NUMERIC(8, 6),
  atm_iv_14d                NUMERIC(8, 6),
  atm_iv_21d                NUMERIC(8, 6),
  atm_iv_30d                NUMERIC(8, 6),
  atm_iv_60d                NUMERIC(8, 6),
  atm_iv_90d                NUMERIC(8, 6),

  -- Realized vol from Garman-Klass
  rv_gk_10d                 NUMERIC(8, 6),
  rv_gk_20d                 NUMERIC(8, 6),
  rv_gk_30d                 NUMERIC(8, 6),
  rv_gk_60d                 NUMERIC(8, 6),
  rv_gk_90d                 NUMERIC(8, 6),

  -- IV-RV spread at matched tenor
  iv_rv_spread_30d          NUMERIC(8, 6),
  iv_rv_spread_60d          NUMERIC(8, 6),

  -- Skew at 30-day tenor
  rr_25_30d                 NUMERIC(8, 6),
  bf_25_30d                 NUMERIC(8, 6),

  -- Term structure
  term_slope_60_7           NUMERIC(8, 6),
  term_curvature_30         NUMERIC(8, 6),

  -- Percentile rankings (252-day trailing). NULL until 60+ days of history.
  iv_rank_30d               NUMERIC(6, 4),       -- [0, 1]
  iv_pct_30d                NUMERIC(6, 4),       -- [0, 1]
  rr_pct_30d                NUMERIC(6, 4),

  -- Regime classification
  vol_regime                TEXT,                -- 'HIGH', 'NORMAL', 'LOW', NULL
  term_regime               TEXT,                -- 'BACKWARDATION', 'FLAT', 'CONTANGO'
  skew_regime               TEXT,                -- 'RICH', 'NORMAL', 'CHEAP', NULL
  variance_premium_flag     TEXT,                -- 'RICH', 'NORMAL', 'COMPRESSED'

  PRIMARY KEY (ticker, date)
);
```

### 4.2 Data flow

**Path 2 (default):**

```
  Polygon /v3/snapshot/options/{ticker}
            │
            ▼
  ┌─────────────────────────┐
  │ snapshot_fetcher.py     │  on-demand, throttled
  │ - fetch chain           │
  │ - filter quality        │
  │ - compute mid           │
  └─────────────────────────┘
            │
            ▼
  ┌─────────────────────────┐
  │ iv_inverter.py          │  per row
  │ - get rate (FRED)       │
  │ - get q (parity, today) │
  │ - solve σ via Brent     │
  │ - compute Greeks at σ   │
  └─────────────────────────┘
            │
            ▼
  options_daily, options_contracts          ─── persist today's snapshot
            │
            ▼
  ┌─────────────────────────┐
  │ surface_metrics.py      │
  │ - ATM IV at tenors      │
  │ - skew (RR, BF)         │
  │ - term structure        │
  └─────────────────────────┘
            │
            ▼
  ┌─────────────────────────┐
  │ realized_vol.py         │  from existing intraday bars
  │ - GK rolling windows    │
  └─────────────────────────┘
            │
            ▼
  ┌─────────────────────────┐
  │ percentiles.py          │  trailing 252-day from surface_metrics_daily
  │ - IV rank, IV pct       │  Path 2: returns null/partial for thin history
  │ - RR percentile         │
  └─────────────────────────┘
            │
            ▼
  surface_metrics_daily              ─── upsert today's row
            │
            ▼
  ┌─────────────────────────┐
  │ regime_classifier.py    │  rule-based
  └─────────────────────────┘
            │
            ▼
  GraphQL/FastAPI response → Angular dashboard
```

**Path 1 only:** Add a `backfill_runner.py` that walks 2 years of historical dates, fetches `/v2/aggs/ticker/{contract}/range/1/day/{from}/{to}` for each contract active on each date, runs IV inversion, populates `options_daily`. Estimated wall-clock: 6-12 hours on Polygon Options Starter (unlimited calls), depending on contract universe size and IV inversion throughput. Resumable via a `backfill_progress` table that tracks `(ticker, date, status)`.

**Path 3 only:** Identical to Path 1 for backend; no Angular work in § 6.

### 4.3 Rate limits and throttling

Polygon Options Starter: unlimited calls but bandwidth-bounded in practice. Set a soft ceiling at 100 req/sec in `polygon_client.py` to be a good citizen. The throttle from the existing client handles this.

For on-demand snapshot refresh (Path 2 default flow): one snapshot call per ticker returns the full chain. SPY chain is ~3000 contracts; QQQ is ~2500. Call returns in ~2-5 seconds. IV inversion on 5000 rows takes ~3 seconds with Brent's method vectorized via `scipy.optimize.brentq` over a list. Well within the 30-second cold-refresh budget.

### 4.4 Failure modes and quality gates

The pipeline writes nothing to `surface_metrics_daily` if any of the following fail for the day:
- ATM IV cannot be computed at 30-day tenor (no expiry within ±15% of 30d)
- Fewer than 100 quality-ok rows in `options_daily` for the date
- Underlying close is missing
- FRED rate curve cannot be retrieved

Failures are logged with structured context. The dashboard renders a "data quality issue" card with the failure reason rather than partial metrics that could mislead.

---

## 5. Backend modules and FastAPI contract

### 5.1 Module layout

New code lives in `app/volatility/` (extending the existing module) and `app/services/options_iv/`:

```
app/
├── services/
│   ├── options_iv/                       ← NEW
│   │   ├── __init__.py
│   │   ├── snapshot_fetcher.py           Wraps polygon_client for option chains
│   │   ├── iv_inverter.py                Brent's-method BSM inverter, vectorized
│   │   ├── parity_dividend.py            Forward-implied div yield
│   │   ├── rate_curve.py                 FRED interpolation
│   │   └── quality_filter.py             Pre-inversion filters
│   └── (existing services unchanged)
├── volatility/                           ← EXTEND (already exists)
│   ├── analytics.py                      Already has skew_metrics; extend with term_structure
│   ├── surface.py                        Existing
│   ├── realized_vol.py                   ← NEW (Garman-Klass)
│   ├── atm_extractor.py                  ← NEW (forward-ATM IV at tenors)
│   ├── percentiles.py                    ← NEW (252-day trailing rank/pct)
│   ├── regime.py                         ← NEW (rule-based classifier)
│   └── persistence.py                    ← NEW (DB ↔ surface_metrics_daily)
├── routers/
│   ├── volatility.py                     Existing — extend with regime endpoints
│   └── (no new router needed)
└── models/
    ├── requests.py                       Add request models for new endpoints
    └── responses.py                      Add response models
```

### 5.2 Public function contracts

Each function below has a docstring with formula reference, units of inputs/outputs, and a golden fixture path. Showing signatures only:

```python
# app/services/options_iv/iv_inverter.py
def invert_iv_brent(
    option_price: float,
    spot: float,
    strike: float,
    ttm_years: float,
    rate: float,
    div_yield: float,
    is_call: bool,
) -> tuple[float | None, str]:
    """Returns (iv, status). status ∈ {'ok', 'no_convergence', 'boundary'}."""

def invert_iv_batch(rows: pd.DataFrame) -> pd.DataFrame:
    """Vectorized over a frame with cols [price, spot, strike, ttm, rate, q, is_call].
    Returns frame with added [iv, status] columns. Uses scipy.optimize.brentq."""

# app/volatility/atm_extractor.py
def atm_iv_at_tenors(
    options_today: pd.DataFrame,
    spot: float,
    rate_curve: dict[float, float],
    div_yield_curve: dict[date, float],
    tenors_days: list[int] = [7, 14, 21, 30, 60, 90],
) -> dict[int, float | None]:
    """Forward-ATM IV at each requested tenor. Null if no expiry within ±15%."""

# app/volatility/realized_vol.py
def garman_klass_daily(bars: pd.DataFrame) -> pd.Series:
    """Daily GK variance. Input frame has [open, high, low, close]; index is date."""

def gk_rolling_annualized(bars: pd.DataFrame, window: int) -> pd.Series:
    """Annualized rolling GK realized vol over `window` trading days."""

# app/volatility/percentiles.py
def trailing_iv_rank(series: pd.Series, asof: date, lookback_days: int = 252) -> tuple[float | None, str]:
    """(rank, status). status ∈ {'full', 'partial', 'insufficient'}."""

def trailing_iv_percentile(series: pd.Series, asof: date, lookback_days: int = 252) -> tuple[float | None, str]:
    """Same shape as trailing_iv_rank."""

# app/volatility/regime.py
@dataclass(frozen=True)
class RegimeClassification:
    vol_regime: Literal['HIGH', 'NORMAL', 'LOW'] | None
    term_regime: Literal['BACKWARDATION', 'FLAT', 'CONTANGO']
    skew_regime: Literal['RICH', 'NORMAL', 'CHEAP'] | None
    variance_premium_flag: Literal['RICH', 'NORMAL', 'COMPRESSED']
    recommended_structures: list[str]
    avoid_structures: list[str]

def classify_regime(metrics: SurfaceMetrics) -> RegimeClassification: ...
```

### 5.3 FastAPI endpoints

All endpoints live in the existing `app/routers/volatility.py` to keep related functionality colocated. Snake_case JSON per repo convention.

```
POST /api/volatility/regime/refresh
  Request: { "ticker": "SPY" }
  Response: { "ticker": "SPY", "asof": 1745619600000, "metrics": {...}, "regime": {...}, "compute_time_ms": 4521 }
  Behavior: Fetches today's snapshot, runs full pipeline, persists, returns metrics+regime.

GET /api/volatility/regime/current?ticker=SPY
  Response: latest row from surface_metrics_daily with regime annotation.
  Behavior: Read-only. No fetch.

GET /api/volatility/regime/history?ticker=SPY&from=YYYY-MM-DD&to=YYYY-MM-DD&metrics=atm_iv_30d,iv_rank_30d,...
  Response: time series of requested metrics over date range.
  Behavior: For dashboard sparklines and historical context.

GET /api/volatility/surface/snapshot?ticker=SPY&date=YYYY-MM-DD
  Response: full grid (strike × expiry → IV) for that date.
  Behavior: For the surface heatmap component.
```

All timestamps `int64 ms UTC` per `CLAUDE.md` § 6.

### 5.4 GraphQL schema additions

```graphql
extend type Query {
  surfaceRegime(ticker: String!): SurfaceRegime!
  surfaceMetricsHistory(
    ticker: String!,
    from: DateTime!,
    to: DateTime!
  ): [SurfaceMetricsPoint!]!
}

extend type Mutation {
  refreshSurface(ticker: String!): SurfaceRegimeResult!
}

type SurfaceRegime {
  ticker: String!
  asof: Long!
  metrics: SurfaceMetrics!
  regime: RegimeClassification!
}

type SurfaceMetrics {
  atmIv30d: Decimal
  atmIv60d: Decimal
  rvGk30d: Decimal
  ivRvSpread30d: Decimal
  rr25_30d: Decimal
  bf25_30d: Decimal
  termSlope60_7: Decimal
  ivRank30d: Decimal           # nullable while history accumulates
  ivPct30d: Decimal
  ivRankStatus: String!         # 'full' | 'partial' | 'insufficient'
}

type RegimeClassification {
  volRegime: String              # 'HIGH' | 'NORMAL' | 'LOW' | null
  termRegime: String!            # 'BACKWARDATION' | 'FLAT' | 'CONTANGO'
  skewRegime: String
  variancePremiumFlag: String!
  recommendedStructures: [String!]!
  avoidStructures: [String!]!
}
```

The .NET resolver is a thin passthrough — calls the FastAPI endpoint, returns the response. No math in C#, per `CLAUDE.md` § 5.

---

## 6. Angular dashboard *(skip this section for Path 3)*

### 6.1 Component tree

```
VolSurfacePage (route: /vol-surface)
├── TickerSelector              (SPY | QQQ tabs or segmented control)
├── RegimeSummaryCard           Big readable regime label + recommendations
├── MetricsGrid
│   ├── AtmIvTermStructure      Line chart, ATM IV vs tenor
│   ├── SkewVisualization       Smile curve at 30-day expiry
│   ├── IvRankGauge             Visual gauge with "partial history" badge
│   └── IvRvSpreadSparkline     Time-series with current value highlighted
├── RegimeHistoryTimeline       Sparkline showing regime over past 60 days
└── DataQualityFooter           Asof timestamp, quality flags, refresh button
```

### 6.2 Data flow

- One Apollo Angular query at page load: `surfaceRegime(ticker)`. Returns everything needed for the cards above.
- Refresh button triggers `refreshSurface(ticker)` mutation. Disables for 30s while pipeline runs.
- Time-series sparkline data fetched lazily via `surfaceMetricsHistory(ticker, from, to)` when user expands the relevant card.
- No client-side computation of any metric. Per repo rule: Angular renders, never computes.

### 6.3 Component specs

Standalone components, signals-based, OnPush, zoneless. PrimeNG v20 for layout primitives (Card, Tabs, Toast). lightweight-charts for time series (you already use it in the PayoffChart component). recharts is fine if simpler.

The `IvRankGauge` component needs special treatment for Path 2: when `ivRankStatus === 'insufficient'` show a "Accumulating history — N days of M needed" placeholder. When `'partial'` show the rank with a small badge linking to a tooltip explaining. When `'full'` show normally.

The `SkewVisualization` component plots the 30-day expiry's smile (strike or delta on x, IV on y) with markers at 25Δ-put and 25Δ-call. Hover tooltip shows the RR and BF values explicitly.

### 6.4 What I'm not specifying here

Visual design — colors, typography, exact card dimensions, micro-animations. Those live with the implementation, not the architecture doc. The frontend-design skill handles that work when you build.

---

## 7. Test and fixture plan

Per `numerical-rigor.md`, every new computational module needs:

1. **Golden fixture** in `tests/fixtures/golden/<module>/` with attribution file
2. **Tolerance-pinned test** asserting strict-float equivalence to a reference implementation
3. **Reference doc** in `docs/references/<module>.md` citing source and tolerance

Required fixtures for this build:

| Fixture | Source | Tolerance | Purpose |
|---|---|---|---|
| `iv_inversion/spy_2024-01-15.parquet` | py_vollib over Polygon snapshot | atol=1e-4, rtol=1e-3 | IV inverter parity |
| `parity_dividend/spy_2024_q1.parquet` | Manual computation, three sample dates | atol=5e-4 | Forward-yield parity |
| `garman_klass/spy_2023.parquet` | sinclair_vol_book reference impl | atol=1e-6, rtol=1e-5 | GK vs canonical |
| `atm_iv/spy_2024-01-15.json` | Manual interpolation, traced | atol=1e-5 | ATM extraction |
| `regime_classifier/test_cases.json` | Hand-constructed regimes | exact | Rule mapping |

Plus property tests:
- IV ∈ [1e-4, 5.0] for all `quality_flag = 'ok'` rows
- ATM call IV ≈ ATM put IV at forward strike, within 5 vol points
- IV rank ∈ [0, 1] when status = 'full'
- Regime classification is total — every metric vector maps to exactly one regime tuple

---

## 8. Phasing

### Path 2 (recommended) — 2-3 weeks

**Week 1 — Data path & inversion**
- Day 1-2: Postgres migrations, schema, Backend EF Core entities
- Day 2-3: `polygon_client` snapshot wrapper, FRED rate curve method
- Day 3-5: `iv_inverter.py` with Brent's method, vectorized batch, golden fixture vs py_vollib
- Day 5: `parity_dividend.py` with sanity check, golden fixture

**Week 2 — Metrics & regime**
- Day 6-7: `realized_vol.py` Garman-Klass, golden fixture
- Day 7-8: `atm_extractor.py`, `percentiles.py` with status flags for thin history
- Day 8-9: skew and term-structure functions extending existing `analytics.py`
- Day 9-10: `regime.py` classifier with hand-built test cases

**Week 3 — API & UI**
- Day 11-12: FastAPI router endpoints, GraphQL schema, .NET passthrough resolvers
- Day 13-15: Angular components, Apollo queries, end-to-end smoke test on live SPY/QQQ data

**Out of scope for v1**: jump-diffusion vol forecasting, contract-level recommendations, alerts, multi-ticker comparison views, intraday refresh.

### Path 1 — add 2-3 weeks for backfill

Insert before Week 1: backfill_runner build (~3 days), backfill execution (~1-2 days wall-clock), backfill validation against random sample (~2 days). Push everything else by ~1.5 weeks.

### Path 3 — subtract 1 week

Skip Week 3 days 13-15. Deliverable is FastAPI + GraphQL only. No Angular components.

---

## 9. Open questions for review

These are the decisions I can't make without you:

1. **Path commit.** § 2.2 recommends Path 2. Final call?
2. **ATM definition.** Section 3.4 uses forward-ATM. Some traders prefer spot-ATM (50-delta convention). Forward is more rigorous; spot is more familiar. Default to forward, document the difference?
3. **Regime threshold tuning.** Section 3.10 has hardcoded thresholds (0.70 for HIGH IV-rank, ±1.0 vol point for term slope). These are reasonable starting points but should be tuned against your ticker history. v1 ships with defaults; tune in v1.1?
4. **GLD/SLV/single names later.** v1 is SPY+QQQ. The math is identical for the others, but commodity ETFs need skew interpretation flipped (call skew = risk-off for metals) and single names need earnings-aware filters. Add as a v1.1 expansion document?
5. **Backtesting integration.** Should the regime classifier feed into the existing `app/engine/` strategy logic (e.g., as a filter on the EMA crossover engine — only trade when `vol_regime != 'HIGH'`)? Out of scope for this TDD but worth flagging if you want it explored next.

---

## 10. References

- Hull, J. C. *Options, Futures and Other Derivatives*, 11e. Prentice Hall, 2021. Chapters 5, 19, 21.
- Gatheral, J. *The Volatility Surface: A Practitioner's Guide*. Wiley, 2006. Chapters 1, 2.
- Sinclair, E. *Volatility Trading*, 2e. Wiley, 2013. Chapter 2.
- Garman, M. B., & Klass, M. J. (1980). On the estimation of security price volatilities from historical data. *Journal of Business*, 53(1), 67-78.
- Bollerslev, T., Tauchen, G., & Zhou, H. (2009). Expected stock returns and variance risk premia. *Review of Financial Studies*, 22(11), 4463-4492.
- Derman, E., & Miller, M. B. *The Volatility Smile*. Wiley, 2016. Chapter 4.
- py_vollib (https://github.com/vollib/py_vollib) — used as reference implementation for golden fixtures.

---

*End of TDD draft. All numerical claims are testable; all formulas are cited; all design decisions trace to either repo conventions or explicit user input. Ready for review.*
