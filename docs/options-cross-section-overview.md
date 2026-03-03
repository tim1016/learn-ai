# Options Cross-Section System — Technical Overview

## 1. Mathematical Foundation

### 1.1 Black-Scholes IV Solver (`bs_solver.py`)

Standard European option pricing:

```
d₁ = [ln(S/K) + (r + ½σ²)T] / (σ√T)
d₂ = d₁ − σ√T

C = S·N(d₁) − K·e^(−rT)·N(d₂)
P = K·e^(−rT)·N(−d₂) − S·N(−d₁)
```

**IV inversion** — two-stage solver:

| Stage | Method | Details |
|-------|--------|---------|
| 1 | Newton-Raphson | Initial guess via Brenner-Subrahmanyam: `σ₀ = √(2π/T) · price/S`, clamped to [0.15, 3.0]. Update: `σ ← σ − (BS(σ) − market) / vega(σ)` |
| 2 | Brent bisection | Fallback on `scipy.optimize.brentq` over [0.01, 5.0] if Newton diverges |

Vega for Newton step: `ν = S·√T·N′(d₁)`

**Guards**: T < 7/365 → reject · price ≤ 0 → reject · price < intrinsic − ε → reject · final σ ∉ [0.05, 3.0] → reject

Risk-free rate: `r = 0.043` (constant).

### 1.2 30-Day Constant-Maturity Interpolation (`iv_builder.py`)

Given two bracket expiries with DTE_low < 30 < DTE_high:

```
w_low  = (DTE_high − 30) / (DTE_high − DTE_low)
w_high = (30 − DTE_low)  / (DTE_high − DTE_low)

IV_30d = w_low · IV_low + w_high · IV_high
```

Single-bracket fallback (square-root-of-time normalization):

```
IV_30d ≈ IV_obs · √(30 / DTE_obs)
```

### 1.3 Research Features (`options_features.py`)

| Feature | Formula |
|---------|---------|
| `iv_30d` | Raw 30-day ATM IV |
| `iv_rank_N` | Rolling `(IV − min) / (max − min)` over N ∈ {60, 252} days |
| `log_skew` | `ln(IV_put / IV_call)` — positive ⇒ elevated put demand |
| `vrp_5` (signal) | `IV_30d − RV₅_trailing`, where `RV₅ = std(ln returns, 5d) · √252` |
| `vrp_5` (research) | `IV_30d − RV₅_forward` (forward-looking, for backtest only) |

### 1.4 Strategy Engine (`strategy_engine.py`)

- **Payoff**: `PnL(S) = Σ_legs [(intrinsic − premium) · qty · direction]` over 2000-point grid
- **POP**: `N(d₂)` at each breakeven boundary, with per-boundary IV interpolation for skew
- **EV**: `∫ PnL(S) · f_LN(S) dS` via numerical integration (1000 points, 99.9% coverage)
- **Greeks**: Full BS per-leg delta, gamma, theta (÷365), vega (per 1% IV)

### 1.5 Forward Targets (`options_runner.py`)

| Target | Formula |
|--------|---------|
| `directional` | `ln(close_{t+1} / close_t)` |
| `volatility` | `std(ln returns, 5d forward) · √252` |
| `abs_return` | `|ln(close_{t+1} / close_t)|` |

**Validation gate**: `|mean IC| ≥ 0.03 ∧ p < α ∧ stationary ∧ monotonic quantiles`

---

## 2. Data Fetching

**Source**: Polygon.io (Starter plan — 2yr max, 15-min delay)

| Endpoint | Python wrapper | Purpose |
|----------|---------------|---------|
| `list_options_contracts()` | `polygon_client.py` | Contract discovery with pagination cap |
| `list_options_expirations()` | Concurrent multi-window via ThreadPoolExecutor | Bracket expiry identification |
| `list_snapshot_options_chain()` | Live snapshot | Real-time greeks, IV, OI, quotes |
| `get_aggs()` | `fetch_aggregates()` | Daily/minute OHLCV for stocks & options |

**IV build cost**: ~1 ticker ≈ 1 (stock bars) + ~60 (contract search) + ~3000 (option bar prefetch) API calls. Option bars prefetched in parallel (5 workers).

**Constraint**: `expired=True` and `as_of_date` cannot be combined (returns 0 results). Bracket search uses `as_of_date` only; contract fetch uses `expired=True` only.

---

## 3. Storage & Caching

### 3.1 PostgreSQL (`OptionsIvSnapshot` entity)

| Column | Type | Note |
|--------|------|------|
| `TickerId` | FK → Ticker | |
| `TradingDate` | date | Indexed with TickerId |
| `Iv30dAtm` | decimal(18,8) | |
| `Iv30dPut` | decimal(18,8) | Skew leg |
| `Iv30dCall` | decimal(18,8) | Skew leg |
| `StockClose` | decimal | |
| `DteLow`, `DteHigh` | int | Bracket DTEs used |
| `PriceSource` | string(20) | `"midpoint"` or `"close"` |

### 3.2 Cache strategy

- **ResearchService (.NET)**: Before calling Python IV build, checks `OptionsIvSnapshots` table for existing data. On successful build, persists all rows.
- **Contract finder (Python)**: In-memory dict caches by `underlying:YYYY-MM` (expiry lookups) and `underlying:expiry_date` (contract selections) within a single build run.
- **Option bars (Python)**: Bulk-prefetched into `_bar_cache[contract_ticker]` dict before per-day loop.

---

## 4. Sanitization & Filtering

| Stage | Filter | Threshold |
|-------|--------|-----------|
| Contract finder | Min volume | 50 |
| Contract finder | Min open interest | 100 |
| Contract finder | Max spread ratio | 10% of mid |
| Contract finder | OTM offset | 5% from ATM |
| Contract finder | Strike search range | ±15% of ATM |
| Bracket search | DTE window | 14–60 days |
| BS solver | Min DTE | 7 days |
| BS solver | Initial σ clamp | [0.15, 3.0] |
| BS solver | Final σ acceptance | [0.05, 3.0] |
| IV builder | Min option price | $0.05 |
| IV builder | Volume filter (close fallback) | ≥ 50 |
| IV builder | Post-derivation IV clamp | [0.05, 3.0] |
| IV builder | Forward-fill limit | 2 business days |
| Diagnostics | Max missing data | 15% |
| Diagnostics | Min valid IV days | 30 |
| Diagnostics | Max discontinuities | 10% of total |
| Diagnostics | Day-over-day IV change flag | 50% |
| Diagnostics | DTE spike flag | 15-day jump |
| Research | Min aligned data points | 30 |

---

## 5. Processing Pipeline

```
Polygon.io
  │
  ▼
contract_finder ─── find_bracket_contracts()
  │  Per trading day: identify 2 bracket expiries (14–60 DTE around 30),
  │  select ATM call + 5% OTM put + 5% OTM call for each bracket
  │
  ▼
iv_builder ─── build_iv_history()
  │  1. Fetch stock daily bars
  │  2. Prefetch all option bars (5 threads)
  │  3. Per-day: derive IV via BS solver → interpolate to 30d → clamp
  │  4. Forward-fill ≤ 2 day gaps
  │  Output: [date, iv_30d_atm, iv_30d_put, iv_30d_call, stock_close, dte_low, dte_high]
  │
  ▼
diagnostics ─── run_diagnostics()
  │  Validate: missing% ≤ 15, valid days ≥ 30, discontinuities ≤ 10%
  │
  ▼
options_features ─── compute iv_rank, log_skew, vrp
  │
  ▼
options_runner ─── run_options_feature_research()
  │  IC (Newey-West), ADF/KPSS, quantile analysis,
  │  regime robustness, train/test split
  │  Gate: |mean IC| ≥ 0.03 ∧ significant ∧ stationary ∧ monotonic
  │
  ▼
.NET ResearchService (cache check → proxy → persist)
  │
  ▼
GraphQL API → Angular Frontend
```

---

## 6. Display & Reporting

### Frontend components

| Component | Route | Purpose |
|-----------|-------|---------|
| `OptionsChainComponent` | `/strategy-lab` | Live chain: greeks, IV, OI, bid/ask, volume bars, ATM highlight, strike slider |
| `OptionsHistoryComponent` | `/options-history` | Historical 0DTE: OCC-format ticker construction, batch scan, minute drill-down |
| `BatchRunnerComponent` | (research lab) | Cross-sectional batch study runner, multi-ticker |

**Price resolution order** (chain display): `day.close` → `lastTrade.price` → `lastQuote midpoint` → `(bid+ask)/2`

**Chain layout**: Calls left | Strike center | Puts right. ATM auto-scrolled. ITM/OTM shaded. OI/volume bar-width scaling.

---

## 7. Architecture Summary (for follow-up prompts)

```
┌─────────────┐    HTTP/JSON     ┌──────────────────┐    GraphQL     ┌─────────────┐
│  Polygon.io │ ◄──────────────► │  Python FastAPI   │ ◄────────────► │  .NET + HC   │
│  (data src) │                  │  (compute layer)  │                │  (API + DB)  │
└─────────────┘                  │                   │                │              │
                                 │ • BS solver       │                │ • Query/Mut  │
                                 │ • Contract finder  │                │ • IV cache   │
                                 │ • IV builder       │                │ • PostgreSQL │
                                 │ • Diagnostics      │                │ • Proxy svc  │
                                 │ • Feature engine   │                └──────┬───────┘
                                 │ • Research runner   │                       │
                                 │ • Strategy engine   │                  GraphQL
                                 └──────────────────┘                       │
                                                                     ┌──────▼───────┐
                                                                     │   Angular    │
                                                                     │  • Chain UI  │
                                                                     │  • History   │
                                                                     │  • Strategy  │
                                                                     │  • Batch     │
                                                                     └──────────────┘
```

**Key files**:
- `PythonDataService/app/research/options/bs_solver.py` — IV math
- `PythonDataService/app/research/options/contract_finder.py` — contract selection
- `PythonDataService/app/research/options/iv_builder.py` — 30d IV construction
- `PythonDataService/app/research/options/diagnostics.py` — quality validation
- `PythonDataService/app/research/features/options_features.py` — signal features
- `PythonDataService/app/research/options_runner.py` — statistical research
- `PythonDataService/app/services/strategy_engine.py` — strategy analysis
- `Backend/Services/Implementation/ResearchService.cs` — cache + proxy
- `Backend/Models/MarketData/OptionsIvSnapshot.cs` — DB entity
- `Frontend/src/app/components/options-chain-v2/` — live chain display
