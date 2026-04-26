# Options & Volatility Platform — Technical Design Document

**Status:** Draft for review.
**Date:** 2026-04-26.
**Audience:** Anyone touching options-math, volatility, or regime code in this repo.
**Companion docs:**
- [`options-math-authorities.md`](options-math-authorities.md) — single sources of truth (PR #25, merged)
- [`edge-feature-design.md`](edge-feature-design.md) — Edge feature spec (PR #26, merged)
- [`vol-surface-dashboard-plan.md`](vol-surface-dashboard-plan.md) — phased build plan (PR #27, in review)

This TDD inventories what currently exists in the options + volatility stack
across the three services (Python, .NET, Angular), then specifies the
remaining work to make the existing math operate end-to-end on persistent
DB-backed data instead of inline payloads. It is design-first, not
plan-first — read top-to-bottom for a complete map; the per-section
"Status: Built" / "Status: To build" tags are the at-a-glance answer to
"is this done?"

---

## 1. Purpose & scope

### 1.1 Purpose

The options + volatility platform answers four questions about the options
market for SPY/QQQ/IWM/DIA:

1. **What is realized volatility doing?** (4 estimators)
2. **What is implied volatility pricing in?** (IV30 ATM, 25Δ skew, term-slope, vol-of-vol)
3. **Where is the variance-risk premium today?** (forward and trailing VRP, z-scored)
4. **What regime is the market in?** (k-means + Gaussian HMM clustering with drift detection)

…and surfaces them through a navigable Angular UI under `/edge` plus a
research-side IV history under `/api/research`, all driven by a single
canonical Black-Scholes Greek + IV inversion module in Python.

### 1.2 In scope

- Python options math: BS price/vega/Greeks, IV inversion, surface fitting, skew + term metrics, RV estimators, VRP, regime clustering.
- FastAPI endpoints under `/api/edge/*`, `/api/volatility/*`, `/api/research/*`, `/api/quantlib/*`.
- Postgres storage for derived IV history (`OptionsIvSnapshot`).
- Angular routes under `/edge` and the related `options-*` and `research-lab` components.
- The data-pipeline gap that today forces Edge to consume IV inline from the frontend.

### 1.3 Out of scope (today)

- .NET GraphQL passthrough for Edge endpoints (skipped per Edge decision #10).
- Live trading, order routing, broker integration.
- Greeks / IV for non-European options (American, exotics) — would need additional engines beyond `bs_greeks`.
- A separate `surface_metrics_daily` aggregated table (Edge does derivation in-process per request; pre-aggregation is a perf optimization that doesn't pay off at SPY+QQQ+IWM+DIA scale).
- A daily scheduler / cron / hosted service. Operating model is manual-on-demand snapshot trigger by user.

---

## 2. System overview

### 2.1 Component map (current state, post-PR #25 + #26)

```
┌───────────────────────────────────────────────────────────────────────────┐
│                          Angular 21 (Frontend/)                           │
│ ┌──────────────┐ ┌─────────────────────┐ ┌──────────────────────────────┐ │
│ │  /edge       │ │  /research-lab      │ │  options-chain-v2            │ │
│ │  ├─ /edge/   │ │   (existing IV      │ │  options-history             │ │
│ │  │  realized-│ │    research path)   │ │  options-strategy-lab        │ │
│ │  │  vs-iv    │ │                     │ │  research-lab/options-math-  │ │
│ │  ├─ /edge/   │ │                     │ │    docs                      │ │
│ │  │  cross-   │ │                     │ │  pricing-lab                 │ │
│ │  │  asset    │ │                     │ │                              │ │
│ │  └─ /edge/   │ │                     │ │                              │ │
│ │     regimes  │ │                     │ │                              │ │
│ └──────┬───────┘ └──────────┬──────────┘ └──────────────────────────────┘ │
└────────┼────────────────────┼────────────────────────────────────────────-┘
         │                    │
         │ HTTP (Edge: direct) │ GraphQL (rest of app)
         │                    │
┌────────▼────────────────────▼─────────────────────────────────────────────┐
│                       .NET 10 / Hot Chocolate (Backend/)                  │
│   - GraphQL passthrough for: research IV history, options chain v2,        │
│     pricing lab, options strategy lab.                                     │
│   - NO passthrough for /api/edge/* (Edge decision #10).                    │
│   - Persistence: AppDbContext.OptionsIvSnapshots.                          │
│   - ResearchService.PersistIvDataAsync writes one row per (ticker, date).  │
└────────┬────────────────────────────────────────────────────────────────-─┘
         │
         │ HTTP (typed clients) + Postgres
         │
┌────────▼─────────────────────────────────────────────────────────────────┐
│                   FastAPI Python (PythonDataService/app/)                │
│                                                                          │
│  routers/                                                                │
│   ├── edge.py            9 endpoints under /api/edge/*                   │
│   ├── volatility.py      9 endpoints under /api/volatility/*             │
│   ├── research.py        IV history + experiment runner                  │
│   ├── quantlib_options.py /price, /strategy, /compare                    │
│   ├── snapshot.py        /options-chain, /unified, /movers               │
│   ├── options.py         /contracts, /expirations                        │
│   ├── dataset.py         data-lab pipeline (uses options_companion)      │
│   └── … (aggregates, indicators, engine, chart, data_quality, etc.)      │
│                                                                          │
│  engine/edge/            (NEW, PR #26)                                   │
│   ├── features_realtime/ realized_vol.py (CtC, Parkinson, GK, YZ),      │
│   │                      iv30_constructor.py, regime_features.py,        │
│   │                      delta_inversion.py                              │
│   ├── labels_oracle/     forward_rv.py                                   │
│   ├── regime_clustering.py (k-means + Gaussian HMM, hand-rolled)         │
│   ├── regime_drift.py    rolling refit + Hungarian alignment             │
│   ├── vrp.py             compute_vrp + vrp_signal                        │
│   ├── edge_score.py      4-component composite                           │
│   ├── trade_simulator.py + spread_model.py                               │
│   ├── period_splitter.py + portfolio_aggregator.py                       │
│   ├── cross_asset_runner.py + regime_strategy_eval.py                    │
│   └── robustness_stats.py (DSR, PBO)                                     │
│                                                                          │
│  volatility/             surface fitting, IV solver, analytics           │
│   ├── solver.py          implied_volatility (canonical, QuantLib+Brent)  │
│   ├── surface.py         build_surface, persist via cache.py             │
│   ├── fitting.py         SVI / SABR / variance interpolation             │
│   ├── analytics.py       compute_skew_metrics, parity-forward            │
│   ├── data_loader.py     OptionChainLoader + DataFilters                 │
│   ├── cache.py           SurfaceCache (disk-backed parquet/json)         │
│   ├── conventions.py     day-count, calendar, yield curve                │
│   └── models.py          Pydantic surface models                         │
│                                                                          │
│  services/                                                               │
│   ├── bs_greeks.py       (canonical) bs_european_price, _vega, _greeks   │
│   ├── quantlib_pricer.py QL price_option (analytic + numerical Greeks)   │
│   ├── options_companion_service.py per-bar IV + Greeks for data-lab     │
│   ├── reference_companion_service.py Polygon ref-endpoint companion      │
│   ├── polygon_client.py  PolygonClientService + _PolygonThrottle         │
│   └── fred_service.py    get_risk_free_rate(dte, observation_date)       │
│                                                                          │
│  research/options/                                                       │
│   ├── iv_builder.py      30d constant-maturity IV time-series builder    │
│   ├── contract_finder.py find_bracket_contracts                          │
│   └── diagnostics.py     run_iv_diagnostics                              │
│                                                                          │
│  engine/options/                                                         │
│   └── pricer.py          adapter around quantlib_pricer for Lean strats  │
└────────┬─────────────────────────────────────────────────────────────────┘
         │
         │ HTTPS (rate-throttled)
         │
┌────────▼──────────────────┐  ┌────────────────────────┐
│   Polygon.io REST API     │  │   FRED API             │
│   (Options Starter plan)  │  │   (risk-free rate)     │
└───────────────────────────┘  └────────────────────────┘
```

### 2.2 Authority register (single source of truth, per CLAUDE.md § 5)

| Calculation | Module / function | Status |
|---|---|---|
| Black-Scholes European price | [`services/bs_greeks.bs_european_price`](../../PythonDataService/app/services/bs_greeks.py) | ✅ Built |
| Black-Scholes raw vega | [`services/bs_greeks.bs_european_vega`](../../PythonDataService/app/services/bs_greeks.py) | ✅ Built |
| Black-Scholes Greeks (Δ Γ Θ V ρ) | [`services/bs_greeks.black_scholes_greeks`](../../PythonDataService/app/services/bs_greeks.py) | ✅ Built |
| IV inversion (single contract) | [`volatility/solver.implied_volatility`](../../PythonDataService/app/volatility/solver.py) | ✅ Built |
| IV inversion (chain) | [`volatility/solver.solve_iv_chain`](../../PythonDataService/app/volatility/solver.py) | ✅ Built |
| Surface fitting (SVI/SABR/var-interp) | [`volatility/fitting.py`](../../PythonDataService/app/volatility/fitting.py) | ✅ Built |
| Skew metrics (RR-25, BF, slope) per-expiry | [`volatility/analytics.compute_skew_metrics`](../../PythonDataService/app/volatility/analytics.py) | ✅ Built |
| Implied forward (parity) | [`volatility/analytics.compute_put_call_parity_forward`](../../PythonDataService/app/volatility/analytics.py) | ✅ Built (yield extractor on top: 🔨 To build) |
| RV close-to-close | [`engine/edge/features_realtime/realized_vol.close_to_close`](../../PythonDataService/app/engine/edge/features_realtime/realized_vol.py) | ✅ Built |
| RV Parkinson | `realized_vol.parkinson` | ✅ Built |
| RV Garman-Klass | `realized_vol.garman_klass` | ✅ Built |
| RV Yang-Zhang | `realized_vol.yang_zhang` | ✅ Built |
| Forward RV (oracle, ex-post) | [`engine/edge/labels_oracle/forward_rv.forward_rv`](../../PythonDataService/app/engine/edge/labels_oracle/forward_rv.py) | ✅ Built |
| IV30 ATM 50Δ (variance-time interp) | [`engine/edge/features_realtime/iv30_constructor.iv30_atm_50d`](../../PythonDataService/app/engine/edge/features_realtime/iv30_constructor.py) | ✅ Built |
| 25Δ skew (RR) | `iv30_constructor.skew_25d` | ✅ Built |
| Term-slope σ(60d)−σ(30d) | `iv30_constructor.term_slope` | ✅ Built |
| Vol-of-vol (ΔIV, IV-vol rolling std) | `iv30_constructor.iv_change` + `iv_vol` | ✅ Built |
| VRP variance form (σ_IV² − σ_RV²) | [`engine/edge/vrp.compute_vrp`](../../PythonDataService/app/engine/edge/vrp.py) | ✅ Built |
| VRP signal (z-thresholded) | `vrp.vrp_signal` | ✅ Built |
| Regime features (OHLCV + IV-derived) | [`engine/edge/features_realtime/regime_features.build_full_features`](../../PythonDataService/app/engine/edge/features_realtime/regime_features.py) | ✅ Built |
| Regime clustering (k-means + HMM) | [`engine/edge/regime_clustering`](../../PythonDataService/app/engine/edge/regime_clustering.py) | ✅ Built |
| Regime drift (rolling refit + Hungarian) | [`engine/edge/regime_drift`](../../PythonDataService/app/engine/edge/regime_drift.py) | ✅ Built |
| Edge Score (4-component composite) | [`engine/edge/edge_score.edge_score`](../../PythonDataService/app/engine/edge/edge_score.py) | ✅ Built |
| Trade simulator + spread model | [`engine/edge/trade_simulator`](../../PythonDataService/app/engine/edge/trade_simulator.py), `spread_model.py` | ✅ Built |
| QuantLib price + Greeks (multi-engine) | [`services/quantlib_pricer.price_option`](../../PythonDataService/app/services/quantlib_pricer.py) | ✅ Built |
| Per-bar IV + Greeks data-lab pipeline | [`services/options_companion_service`](../../PythonDataService/app/services/options_companion_service.py) | ✅ Built |
| **Forward-implied dividend yield (q)** | **None — wraps existing parity-forward, ~30 LOC** | 🔨 To build |
| **Multi-tenor FRED rate curve interpolation** | **Extension of `fred_service`** | 🔨 To build (only if multi-tenor IV needed) |
| **Multi-tenor ATM IV (7d/14d/60d/90d)** | **One-line caller of `variance_interpolated_iv`** | 🔨 To build (only if needed) |
| **Chain-quote persistence (raw mid-quotes)** | **None** | 🔨 To build |
| **Snapshot pipeline endpoint** | **None** | 🔨 To build |
| **DB-backed read path for Edge** | **None — Edge uses inline `iv_series`** | 🔨 To build |
| **Regime semantic labeler (cluster ID → label)** | **None — clusters are anonymous integers** | 🔨 To build (optional) |

---

## 3. Built — detailed inventory

### 3.1 Options math primitives

**Status: Built (PR #25 consolidation).**

After PR #25 (`cleanup/options-math-sovereignty`), there is exactly **one** IV
solver and **one** closed-form analytical BS module. The previous duplicate
(`research/options/bs_solver.py`) is deleted. See
[`options-math-authorities.md`](options-math-authorities.md) for the dispatch
rules between closed-form and QuantLib-based paths.

| Function | Returns | Notes |
|---|---|---|
| `bs_european_price(spot, strike, ttm_years, rate, volatility, is_call, dividend=0.0)` | `float` | Hull eq. 15.20/15.21. Returns 0 for degenerate inputs. |
| `bs_european_vega(spot, strike, ttm_years, rate, volatility, dividend=0.0)` | `float` | Per 1.0 vol unit (NR-friendly). Use `black_scholes_greeks(...).vega` for per-1%. |
| `black_scholes_greeks(spot, strike, ttm_years, volatility, rate, dividend, is_call)` | `BSGreeks(delta, gamma, theta, vega, rho)` | Closed-form, continuous-time, sub-day-resolution safe. Theta per calendar day, vega per 1% IV, rho per 1% rate. |
| `implied_volatility(option_price, spot, strike, ttm, rate=0.05, dividend=0.0, is_call=True, vol_guess=0.25, min_ttm=None)` | `ImpliedVolResult(iv, status, …)` | QuantLib `VanillaOption.impliedVolatility()` primary; scipy Brent fallback. `min_ttm` overrides 1-minute floor for intraday callers. |
| `solve_iv_chain(rows)` | `pd.DataFrame` | Vectorized loop over a chain; returns rows + iv + status. |

**Tests:** [`tests/services/test_bs_greeks.py`](../../PythonDataService/tests/services/test_bs_greeks.py),
[`tests/volatility/test_solver.py`](../../PythonDataService/tests/volatility/test_solver.py).

### 3.2 Volatility surface module

**Status: Built (pre-existing).**

Mounted at `/api/volatility` via [`routers/volatility.py`](../../PythonDataService/app/routers/volatility.py).

Endpoints:

| Endpoint | Method | Purpose |
|---|---|---|
| `/surface/build` | POST | Fit a surface from a list of `OptionRecord`. |
| `/surface/build-from-ticker` | POST | Fetch chain via `data_loader`, then fit. |
| `/surface/build-from-csv` | POST | Parse a CSV chain, then fit. |
| `/surface/{id}/grid` | GET | Matrix grid (x, y, z) for plotting. |
| `/surface/{id}/smiles` | GET | Per-expiry fitted + market smiles. |
| `/surface/{id}/diagnostics` | GET | Full surface diagnostics (arbitrage checks, fit quality). |
| `/surface/{id}/query` | POST | Query specific (K, T) points. |
| `/surface/{id}/export/{format}` | GET | JSON / CSV / Parquet export. |
| `/surface/batch-summary` | POST | Build/load surfaces for date range. |

**Backing storage:** `volatility/cache.py` writes parquet/json to disk under `cache/`,
keyed by deterministic `compute_surface_id(...)`. **Not Postgres** — disk-backed
cache for "expensive-to-recompute fitted surfaces."

### 3.3 Edge feature stack

**Status: Built (PR #26).**

Mounted at `/api/edge` via [`routers/edge.py`](../../PythonDataService/app/routers/edge.py)
(no prefix in main.py mount; the prefix is set on the router itself).

Endpoints:

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/edge/realized-vs-iv/series` | POST | Per-bar trailing+forward RV (4 estimators) + IV30 + VRP forward + z-score. |
| `/api/edge/realized-vs-iv/signals` | POST | Per-bar trade-side from VRP z-score with thresholds. |
| `/api/edge/realized-vs-iv/coverage/{symbol}` | GET | Coverage stats for a symbol's IV history. |
| `/api/edge/cross-asset/run` | POST | Run a strategy across asset universe; emit per-asset + composite results. |
| `/api/edge/cross-asset/strategies` | GET | List registered cross-asset strategies. |
| `/api/edge/regimes/cluster` | POST | Fit k-means or Gaussian HMM on regime features. |
| `/api/edge/regimes/strategy-fit` | POST | Partition strategy returns by regime; emit per-regime stats. |
| `/api/edge/trade-sim/run` | POST | Simulate trades from signals + bars + spread model; emit equity curve + cost attribution. |
| `/api/edge/edge-score/series` | POST | 4-component per-bar Edge Score with action label. |

**Data isolation:** `engine/edge/features_realtime/` and `labels_oracle/` are
hard-split. CI guard greps for `\.shift\(-\d+\)` and `from .*labels_oracle`
in features_realtime; build fails on hit unless overridden via
`# noqa: leakage-allowed` with inline justification.

**Frontend surface:**

```
Frontend/src/app/components/edge/
├── edge.component.{ts,html,scss}    Parent /edge route, nav-card layout
├── realized-vs-iv/                  /edge/realized-vs-iv (RV + IV + VRP charts)
├── regimes/                         /edge/regimes (cluster + drift charts)
├── cross-asset/                     /edge/cross-asset (heatmap + small-multiples)
├── drawers/                         edge-score-drawer + trade-sim-drawer
├── charts/                          edge-charts.ts (shared chart helpers)
└── services/                        edge-api.service.ts + edge-mock-data.service.ts
```

The `/edge` parent page is a **navigation card layout** — three cards link to
the three sub-views. There is no top-line "current regime" summary card.

**Known gap:** the v1 router accepts `iv_series: list[dict] | None` and
`bars: list[BarPayload]` inline in the request body. There is **no DB-backed
read path** — every Edge endpoint is fed by the frontend (which today uses
`edge-mock-data.service.ts`).

### 3.4 IV history pipeline (research-side)

**Status: Built (pre-existing).**

This is the older 30d constant-maturity IV pipeline that predates Edge. It is
**still active** and is the only thing today that persists IV to Postgres.

**Flow:**

```
   POST /api/research/<experiment endpoint>
            │
            ▼
   research/options/iv_builder.build_iv_history(ticker, start, end, ...)
            ├── contract_finder.find_bracket_contracts (bracket expiries around 30d)
            ├── _prefetch_all_bars (parallel fetch via PolygonClientService)
            ├── per-day:
            │   ├── _get_option_price (mid → vwap → close fallback)
            │   ├── _derive_iv_for_contract → volatility/solver.implied_volatility
            │   └── _interpolate_iv (variance-time across two bracket expiries)
            └── returns DataFrame[date, iv_30d_atm, iv_30d_put, iv_30d_call,
                                  stock_close, dte_low, dte_high, price_source,
                                  iv_quality]
            │
            ▼
   ResearchService.PersistIvDataAsync (Backend/.NET)
            │
            ▼
   Postgres OptionsIvSnapshots table (one row per ticker per date)
```

**What it gives you:** a daily 30d ATM/Put/Call IV history per ticker. Used by
the `/research-lab` Angular components.

**What it does NOT give you:** raw chain quotes per contract, multi-tenor IV,
intraday IV, anything Edge needs.

### 3.5 Per-bar options companion (data-lab)

**Status: Built.**

[`services/options_companion_service`](../../PythonDataService/app/services/options_companion_service.py)
is a separate per-bar pipeline used by the data-lab CSV exporter. For each
bar of underlying OHLC, it picks an option contract (per the slot rules in
the data-lab config), invokes `volatility/solver.implied_volatility` on the
quote, and computes Greeks via `bs_greeks.black_scholes_greeks`. Used via
`POST /api/dataset/build` when `request.options_companion.enabled=True`.

This is structurally the closest thing in the repo to the snapshot pipeline
we still need to build (§ 4.2). Different framing — companion is per-bar
inside an exporter; snapshot is per-day to a DB — but the read+invert flow
is identical and should be a refactor target.

### 3.6 QuantLib pricing endpoints

**Status: Built (pre-existing).**

[`routers/quantlib_options.py`](../../PythonDataService/app/routers/quantlib_options.py)
exposes:

| Endpoint | Purpose |
|---|---|
| `GET /api/quantlib/status` | Whether QL is installed + available engines. |
| `POST /api/quantlib/price` | Single European option pricing (any QL engine). |
| `POST /api/quantlib/strategy` | Multi-leg strategy pricing. |
| `POST /api/quantlib/compare` | Curve-comparison endpoint: Python BS vs 6 QL engines across spot range. |

The `compare` endpoint is the one place we deliberately keep two BS
implementations side by side — for the comparison curve. Python BS uses the
canonical `bs_greeks.bs_european_price`; QL uses each of its engines. This
is documented in [`options-math-authorities.md`](options-math-authorities.md).

### 3.7 Polygon client + throttle

**Status: Built (pre-existing).**

[`services/polygon_client.PolygonClientService`](../../PythonDataService/app/services/polygon_client.py)
wraps the Polygon Python SDK with a `_PolygonThrottle` (proactive pre-send
delay; configurable rate cap). Methods used by everything above:

- `fetch_aggregates(ticker, multiplier, timespan, from_date, to_date, adjusted)` — daily/intraday OHLCV bars.
- `list_options_contracts(...)` — contract metadata (with `as_of` for historical).
- `list_snapshot_options_chain(ticker, params={...})` — live chain snapshot (today only; Polygon Starter plan).
- `get_stock_snapshot(ticker)` — current underlying.

**No read-through cache.** Every call hits Polygon directly, throttled.

### 3.8 Storage layer

**Status: Built (`OptionsIvSnapshot` only).**

[`Backend/Models/MarketData/OptionsIvSnapshot.cs`](../../Backend/Models/MarketData/OptionsIvSnapshot.cs):

```csharp
public class OptionsIvSnapshot {
    public long Id { get; set; }
    public int TickerId { get; set; }
    public Ticker Ticker { get; set; }
    public DateTime TradingDate { get; set; }
    public decimal? Iv30dAtm { get; set; }
    public decimal? Iv30dPut { get; set; }
    public decimal? Iv30dCall { get; set; }
    public decimal? StockClose { get; set; }
    public int? DteLow { get; set; }
    public int? DteHigh { get; set; }
    public string PriceSource { get; set; }
    public string Source { get; set; } = "derived";
    public DateTime CreatedAt { get; set; } = DateTime.UtcNow;
}
```

DbSet at [`Backend/Data/AppDbContext.cs:31`](../../Backend/Data/AppDbContext.cs).
Writer is `ResearchService.PersistIvDataAsync`. Reader is the same service's
`/research/iv-history`-shaped query.

**There is no chain-quote table, no multi-tenor IV table, no surface metrics table.**

### 3.9 Frontend — options-related components

**Status: Built.**

| Component | Route | Purpose |
|---|---|---|
| `edge` (parent + 3 children + 2 drawers) | `/edge` | Edge feature views (PR #26). |
| `options-chain-v2` | `/options-chain` (or similar) | Live chain explorer with expiration ribbon. |
| `options-history` | `/options-history` | Per-day options-chain explorer with stock + minute charts. **Does NOT plot IV history.** |
| `options-strategy-lab` | `/options-strategy-lab` | Multi-leg strategy P&L lab with `payoff-chart`. |
| `pricing-lab` | `/pricing-lab` | BS pricer UI; uses `/api/quantlib/compare`. |
| `research-lab/options-math-docs` | `/research-lab/...` | Formula documentation viewer. |
| `research-lab/*` (other) | `/research-lab/...` | Research experiments including IV history viewer. |

---

## 4. To build — detailed design

### 4.1 The single sentence of remaining work

**Build the chain → invert → persist → read pipeline that turns Edge's inline
`iv_series` payload into a real DB-backed time series populated by
manually-triggered snapshots and historical backfill.**

Three new modules, one new table, one new endpoint, one bridge into Edge,
optionally one Angular surface. No new math modules — the math is built.

### 4.2 New module: snapshot pipeline

**Status: To build.**

```
PythonDataService/app/volatility/
├── snapshot_pipeline.py       NEW. Orchestrates fetch → filter → invert → persist.
├── parity_dividend.py         NEW. Wraps analytics.compute_put_call_parity_forward
│                                   to extract continuous dividend yield q.
├── rate_curve.py              NEW. Multi-tenor FRED interpolation. Optional
│                                   for v1 — not required if all IV is at 30d.
└── persistence.py             NEW. SQLAlchemy/asyncpg writes to options_chain_quotes.
```

**`snapshot_pipeline.fetch_snapshot(ticker, date, force=False)`** — top-level entry point:

```python
async def fetch_snapshot(
    ticker: str,
    date: date,
    *,
    force: bool = False,
    polygon: PolygonClientService,
    fred: FredService,
) -> SnapshotResult:
    """Fetch options chain for (ticker, date), invert IV per row, persist.

    For date == today: uses list_snapshot_options_chain (live chain).
    For date < today: uses list_options_contracts(as_of=date) +
                      fetch_aggregates per contract for the historical bar.

    Idempotent when force=False — no-op if (ticker, date) already in
    options_chain_quotes with row count > 0.

    Returns SnapshotResult with rows_inserted, rows_skipped, status,
    compute_time_ms, and a diagnostics dict for the dashboard.
    """
```

Steps inside (each delegates to existing functions where possible):

1. Idempotency check: query `options_chain_quotes` for `(ticker, date)`; return early if `force=False` and rows exist.
2. Fetch chain: `polygon.list_snapshot_options_chain(...)` for today; otherwise `polygon.list_options_contracts(as_of=date)` + per-contract `fetch_aggregates`.
3. Quality filter: reuse [`volatility/data_loader`](../../PythonDataService/app/volatility/data_loader.py) `OptionChainLoader` filter logic.
4. Get rate: `fred.get_risk_free_rate(dte_days=30, observation_date=date.isoformat())`.
5. Get dividend yield: `parity_dividend.extract_q_per_expiry(filtered_rows, rate)` — see § 4.3.
6. Invert IV per row: `volatility/solver.implied_volatility` (canonical).
7. Persist: `persistence.upsert_chain_quotes(rows)`.

**`parity_dividend.extract_q_per_expiry(rows, rate)`** — converts the existing
forward extractor into a yield extractor:

```python
def extract_q_per_expiry(
    rows: list[dict],
    rate: float,
) -> dict[float, float]:
    """For each expiry, extract continuous dividend yield q from put-call parity.

    Builds on volatility/analytics.compute_put_call_parity_forward (which
    returns implied F per TTM) by inverting q = r - ln(F/S)/T per expiry.
    Sanity bound: if the result falls outside [-0.05, 0.20], log warning
    and fall back to vendor q (default 0.0 unless caller supplies override).
    """
```

**`rate_curve.get_rate_curve(date, tenors_years)`** — only built if multi-tenor
IV is needed (see Open Question Q5).

### 4.3 New table: `options_chain_quotes`

**Status: To build.**

```sql
-- Raw mid-quote rows per contract per date.
-- Primary input for Edge IV pipeline (per edge-feature-design.md § 4.4).
-- One snapshot per (ticker, date) populates ~3000 rows for SPY-class chains.
CREATE TABLE options_chain_quotes (
    id              BIGSERIAL PRIMARY KEY,
    ticker          TEXT       NOT NULL,
    trading_date    DATE       NOT NULL,
    contract_symbol TEXT       NOT NULL,           -- e.g. 'O:SPY260530C00550000'
    expiry          DATE       NOT NULL,
    strike          NUMERIC(10,2) NOT NULL,
    option_type     CHAR(1)    NOT NULL,           -- 'C' or 'P'
    bid             NUMERIC(10,4),
    ask             NUMERIC(10,4),
    mid             NUMERIC(10,4),
    last            NUMERIC(10,4),
    volume          BIGINT,
    open_interest   BIGINT,
    underlying_close NUMERIC(10,4),                -- snapshotted at fetch
    rate_used       NUMERIC(8,6),                  -- rate at this row's TTM
    div_yield_used  NUMERIC(8,6),                  -- parity-implied q at this row's expiry
    iv              NUMERIC(8,6),                  -- canonical solver output
    iv_status       TEXT       NOT NULL,           -- ImpliedVolResult.status
    delta           NUMERIC(8,6),
    gamma           NUMERIC(10,8),
    theta           NUMERIC(8,6),
    vega            NUMERIC(8,6),
    quality_flag    TEXT       NOT NULL DEFAULT 'ok',
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (ticker, trading_date, contract_symbol),
    CONSTRAINT options_chain_quotes_type_chk CHECK (option_type IN ('C','P'))
);

CREATE INDEX idx_ocq_ticker_date  ON options_chain_quotes (ticker, trading_date);
CREATE INDEX idx_ocq_quality_ok   ON options_chain_quotes (quality_flag)
                                   WHERE quality_flag = 'ok';
CREATE INDEX idx_ocq_ticker_expiry ON options_chain_quotes (ticker, expiry);
```

Sizing: for SPY at ~3000 active contracts/day × 500 trading days × 4 tickers
= ~6M rows over 2 years. Postgres-trivial.

**`OptionsIvSnapshot` is unchanged.** It continues to serve the existing
research path.

### 4.4 New endpoint: `/api/volatility/snapshot`

**Status: To build.**

Mounted in the existing [`routers/volatility.py`](../../PythonDataService/app/routers/volatility.py).

```python
class SnapshotRequest(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=10)
    date:   str = Field(..., description="YYYY-MM-DD; same code path for today + historical")
    force:  bool = Field(False, description="Refetch + reinvert even if already persisted")

class SnapshotResponse(BaseModel):
    ticker:           str
    asof:             int             # int64 ms UTC, per CLAUDE.md § 6
    rows_inserted:    int
    rows_skipped:     int
    rows_existing:    int
    compute_time_ms:  int
    status:           Literal['ok', 'no_chain', 'no_rate', 'partial']
    diagnostics:      dict

@router.post("/snapshot", response_model=SnapshotResponse)
async def snapshot(req: SnapshotRequest) -> SnapshotResponse:
    """Manual on-demand snapshot for one (ticker, date).

    Operating model: user clicks a button or a shell loop iterates dates.
    No background scheduler — see vol-surface-dashboard-plan.md § 5.
    """
```

Acceptance criteria:

- `POST /api/volatility/snapshot {"ticker":"SPY","date":"2026-04-26"}` populates today.
- Same endpoint with historical date populates that date.
- Re-invocation with `force=false` returns `rows_existing > 0, rows_inserted = 0`.
- Bash loop fills any range:
  ```bash
  for d in $(date_range 2024-04-01 2026-04-25); do
    curl -X POST /api/volatility/snapshot \
      -H 'content-type: application/json' \
      -d "{\"ticker\":\"SPY\",\"date\":\"$d\"}"
  done
  ```
- Golden fixture for IV inversion tied to `py_vollib` per `numerical-rigor.md`.

### 4.5 Bridge Edge to DB-backed reads

**Status: To build.**

Today every Edge endpoint accepts `iv_series` and/or `bars` inline. Extension:
each accepts an alternative `from_db` payload and, when present, reads from
`options_chain_quotes` instead.

```python
class FromDbSpec(BaseModel):
    ticker:   str
    start_ms: int   # int64 ms UTC inclusive
    end_ms:   int   # int64 ms UTC inclusive

class RealizedVsIvSeriesRequest(BaseModel):
    # … existing fields …
    bars:       list[BarPayload] | None = None
    iv_series:  list[dict] | None = None
    from_db:    FromDbSpec | None = None     # NEW

    @model_validator(mode='after')
    def _exactly_one_source(self):
        n = sum(x is not None for x in (self.bars, self.from_db))
        if n != 1:
            raise ValueError("provide exactly one of `bars` or `from_db`")
        return self
```

**New helper:** `engine/edge/iv30_from_db(ticker, start_ms, end_ms) -> pd.Series`
in a new `engine/edge/io/db_loader.py` module. Reads `options_chain_quotes`,
groups by `trading_date`, computes per-day IV30 ATM 50Δ via the existing
`iv30_constructor.iv30_atm_50d`, returns a daily-indexed series.

Edge frontend `edge-api.service.ts` switches its default mode from
`edge-mock-data.service.ts` to `from_db: { ticker, start_ms, end_ms }`. The
mock-data service stays for tests and demos.

Acceptance:

- After Phase 1 has populated 252+ days of SPY data, hitting
  `/api/edge/realized-vs-iv/series` with a `from_db` payload returns the
  same VRP-forward + z-score series as the inline path returns when given
  the same data.
- Frontend `/edge/realized-vs-iv` page renders real numbers with a "live data"
  toggle.

### 4.6 Optional: regime semantic labeler

**Status: To build (optional — see Open Question Q3 in plan).**

```
PythonDataService/app/engine/edge/regime_label.py    NEW. Pure function;
                                                     no math, no DB.
```

```python
@dataclass(frozen=True)
class SemanticRegime:
    label:     Literal['HIGH_VOL', 'NORMAL', 'LOW_VOL', 'STRESS', 'CALM']
    rationale: str
    suggested: list[str]   # e.g. ['short premium: iron condor', 'avoid: long straddle']
    avoid:     list[str]

def label_regime(
    *,
    cluster_centroid: dict[str, float],   # feature → centroid value
    vrp_z:            float,
    iv_pct:           float,
) -> SemanticRegime:
    """Map a cluster centroid + the latest VRP-z + IV-percentile to a
    semantic label and structure recommendations. Pure function; the
    full rule table lives in this module."""
```

The Angular surface is one new card on `/edge/`'s parent page above the
existing nav cards.

### 4.7 Optional: multi-tenor extensions

**Status: To build (optional — only when needed).**

The math primitive `iv30_constructor.variance_interpolated_iv` is generic. Two
one-line additions enable multi-tenor:

- Loop the existing `iv30_atm_50d` over `target_days ∈ {7, 14, 21, 30, 60, 90}`.
- Loop `skew_25d` and `term_slope` over multiple tenor pairs.

Defer until a consumer asks for them.

---

## 5. Data flow diagrams

### 5.1 Today (post-Edge ship, pre-snapshot pipeline)

```
                                                            ┌─────────────────────┐
                                                            │ /research-lab       │
                                                            │   (existing IV path)│
                                                            └─────────▲───────────┘
                                                                      │ GraphQL
 ┌────────────────────────────────────────────────┐                   │
 │ User clicks /research/iv-history (Angular)      │                   │
 │     │                                           │                   │
 │     ▼ GraphQL                                   │           ┌───────┴────────┐
 │ Backend.ResearchService                         │           │ ResearchService│
 │     │                                           │           │ (.NET)         │
 │     ▼ HTTP                                      │           │  reads         │
 │ POST /api/research/<experiment>                 │           │  OptionsIvSnap.│
 │     │                                           │           └───────▲────────┘
 │     ▼                                           │                   │
 │ Python research_runner                          │                   │
 │     │                                           │                   │
 │     ▼                                           │                   │
 │ iv_builder.build_iv_history (per day):          │                   │
 │   - find bracket contracts                      │                   │
 │   - prefetch bars (parallel)                    │                   │
 │   - invert IV via canonical solver (per row)    │                   │
 │   - interpolate to 30d (variance-time)          │                   │
 │     │                                           │                   │
 │     ▼                                           │                   │
 │ DataFrame[date, iv_30d_atm/put/call, …]         │                   │
 │     │                                           │                   │
 │     ▼ HTTP back to .NET                         │                   │
 │ Backend.PersistIvDataAsync                      │                   │
 │     │                                           │                   │
 │     ▼ Postgres                                  │                   │
 │ OptionsIvSnapshots (one row per ticker per date)│ ──────────────────┘
 └────────────────────────────────────────────────┘


                                                            ┌─────────────────────┐
                                                            │ /edge/* (Angular)    │
                                                            └─────────┬───────────┘
                                                                      │ HTTP (no GQL)
                                                                      ▼
 ┌────────────────────────────────────────────────────────────────────────────────┐
 │ Frontend edge-api.service                                                      │
 │   default: edge-mock-data.service (synthetic IV + bars)                        │
 │     │                                                                          │
 │     ▼ POST /api/edge/realized-vs-iv/series  (or other /api/edge/* endpoint)    │
 │ {bars: [...], iv_series: [...]}                                                │
 │     │                                                                          │
 │     ▼                                                                          │
 │ Python routers/edge.py                                                         │
 │     │                                                                          │
 │     ▼                                                                          │
 │ engine/edge/* math:                                                            │
 │   realized_vol (4 estimators)                                                  │
 │   iv30_constructor (variance interpolation, 25Δ skew, term-slope)              │
 │   vrp.compute_vrp + vrp_signal                                                 │
 │   regime_clustering (k-means + HMM)                                            │
 │   edge_score (4-component)                                                     │
 │   trade_simulator + spread_model                                               │
 │     │                                                                          │
 │     ▼                                                                          │
 │ Response JSON → Angular charts                                                 │
 └────────────────────────────────────────────────────────────────────────────────┘
                                                              ▲
                              GAP: no DB read in Edge path. ──┘
```

### 5.2 Target (post-snapshot-pipeline + bridge)

```
                                  ┌─────────────────────────────────┐
                                  │ User clicks "Snapshot today"    │
                                  │ or runs historical loop         │
                                  └────────────┬────────────────────┘
                                               │
                                               ▼ HTTP
                ┌──────────────────────────────────────────────────────────┐
                │ POST /api/volatility/snapshot {ticker, date, force}      │
                └────────────┬─────────────────────────────────────────────┘
                             │
                             ▼
           ┌────────────────────────────────────────────────┐
           │ volatility/snapshot_pipeline.fetch_snapshot    │
           │  ├─ polygon_client (today vs historical path)  │
           │  ├─ data_loader filter                         │
           │  ├─ fred_service.get_risk_free_rate            │
           │  ├─ parity_dividend.extract_q_per_expiry  NEW  │
           │  ├─ volatility/solver.implied_volatility       │
           │  └─ persistence.upsert_chain_quotes       NEW  │
           └────────────┬───────────────────────────────────┘
                        │
                        ▼
           ┌────────────────────────────────────────────┐
           │ Postgres options_chain_quotes (NEW table)  │
           │   ~3000 rows per (ticker, date)            │
           └────────────┬───────────────────────────────┘
                        │
                        ▼ (read by Edge bridge)
 ┌────────────────────────────────────────────────────────────────────────────────┐
 │ Edge endpoint with from_db payload                                             │
 │     │                                                                          │
 │     ▼                                                                          │
 │ engine/edge/io/db_loader.iv30_from_db  NEW                                     │
 │   ├─ SELECT * FROM options_chain_quotes WHERE ticker AND date BETWEEN ...      │
 │   ├─ group by trading_date                                                     │
 │   └─ iv30_constructor.iv30_atm_50d per day                                     │
 │     │                                                                          │
 │     ▼                                                                          │
 │ existing engine/edge/* math (unchanged)                                        │
 │     │                                                                          │
 │     ▼                                                                          │
 │ Response JSON → Angular charts (now showing live data)                         │
 └────────────────────────────────────────────────────────────────────────────────┘
```

The two flows in 5.1 (research IV path and Edge mock path) become one
upstream-shared flow in 5.2.

---

## 6. API contracts (new)

### 6.1 `POST /api/volatility/snapshot`

```http
POST /api/volatility/snapshot
Content-Type: application/json

{
  "ticker": "SPY",
  "date":   "2026-04-26",
  "force":  false
}
```

Success (200):

```json
{
  "ticker": "SPY",
  "asof": 1745619600000,
  "rows_inserted": 2847,
  "rows_skipped": 12,
  "rows_existing": 0,
  "compute_time_ms": 4521,
  "status": "ok",
  "diagnostics": {
    "expiries_seen": 23,
    "underlying_close": 510.35,
    "rate_used": 0.0518,
    "div_yield_per_expiry": {"2026-05-23": 0.0145, "2026-06-20": 0.0142},
    "iv_failures_by_status": {"convergence_failure": 3, "price_too_low": 9}
  }
}
```

Errors:

- `400`: invalid date format, ticker not in supported universe, date in the future.
- `502`: Polygon or FRED unreachable.
- `200` with `status: "no_chain"`: ticker valid but no chain returned (holiday, market closed).

### 6.2 Existing `/api/edge/*` endpoints — extension

Each endpoint that currently requires `iv_series` and/or `bars` adds a
mutually-exclusive `from_db` field:

```diff
 class RealizedVsIvSeriesRequest(BaseModel):
     symbol: str
     bar_size: Literal["15m", "1d"] = "1d"
     tenor_days: int = Field(30, ge=1, le=365)
     estimators: list[str] = Field(default_factory=lambda: ["yz"])
     windows: list[int] = Field(default_factory=lambda: [5, 10, 30])
-    bars: list[BarPayload]
-    iv_series: list[dict] | None = None
+    bars: list[BarPayload] | None = None
+    iv_series: list[dict] | None = None
+    from_db: FromDbSpec | None = None
+
+    @model_validator(mode='after')
+    def _exactly_one_source(self):
+        if (self.bars is None) == (self.from_db is None):
+            raise ValueError("provide exactly one of `bars` or `from_db`")
+        return self
```

When `from_db` is supplied, `iv_series` is ignored (DB derives IV30 in
`db_loader.iv30_from_db`).

---

## 7. Test strategy

Per [`numerical-rigor.md`](../../.claude/rules/numerical-rigor.md) and
[`testing.md`](../../.claude/rules/testing.md).

### 7.1 New tests required by this work

| Module | Test class / fixture | Tolerance |
|---|---|---|
| `parity_dividend.extract_q_per_expiry` | property: q ∈ [-0.05, 0.20] for SPY across 100 dates; cross-check against SPDR distribution-yield within 50bps | `atol=5e-4` |
| `snapshot_pipeline.fetch_snapshot` | golden fixture: SPY 2024-01-15 chain → expected per-row IV via py_vollib | `atol=1e-4, rtol=1e-3` |
| `snapshot_pipeline.fetch_snapshot` | idempotency: second call with `force=False` inserts 0 rows | exact |
| `persistence.upsert_chain_quotes` | round-trip: write 100 rows, read back, asserting equality | `atol=1e-9` |
| `db_loader.iv30_from_db` | parity: same data fed inline vs read-from-DB produces identical IV30 series | `atol=1e-12` |
| `regime_label.label_regime` (if built) | hand-constructed regime tuples → expected labels | exact |

### 7.2 Reused tests (already passing)

All existing tests under:

- `tests/services/test_bs_greeks.py` (124 tests after PR #25)
- `tests/services/test_options_companion_service.py`
- `tests/volatility/test_solver.py`
- `tests/research/options/test_iv_builder*.py` if/where present
- `tests/edge/*` (PR #26 added ~10 test modules covering RV, IV30, VRP, regime clustering, drift, robustness, spread, trade simulator)

Continue to pass with no changes.

### 7.3 CI guards

The existing `pytest -k test_no_leakage` guard (Edge decision #12) extends
to the new `engine/edge/io/db_loader.py` module — same `\.shift\(-\d+\)` and
`from .*labels_oracle` ban. The DB loader sits in `engine/edge/io/` so it
isn't `features_realtime/`; if it turns out we want to put it there,
reapply the guard.

---

## 8. Rollout sequence

| Step | What | When | Mergeable independently? |
|---|---|---|---|
| 0 | Open Questions Q1-Q6 in `vol-surface-dashboard-plan.md` answered | Before any code | n/a |
| 1 | Postgres migration: `options_chain_quotes` table + indexes | Phase 1, day 1 | Yes |
| 2 | `volatility/persistence.py` + golden round-trip test | Phase 1, day 1-2 | Yes |
| 3 | `volatility/parity_dividend.py` + property test | Phase 1, day 2 | Yes |
| 4 | `volatility/snapshot_pipeline.py` + idempotency + py_vollib fixture | Phase 1, day 2-4 | Yes |
| 5 | `POST /api/volatility/snapshot` endpoint + Pydantic models | Phase 1, day 3-4 | Yes (after step 4) |
| 6 | One-time backfill: SPY+QQQ+IWM+DIA × 2 years (shell loop calling step 5) | After Phase 1 | Operational, not code |
| 7 | `engine/edge/io/db_loader.py` + parity test (inline vs DB) | Phase 2, day 1 | Yes |
| 8 | Edge router signature widening (`from_db` field) on each endpoint | Phase 2, day 1-2 | Yes |
| 9 | Frontend `edge-api.service` switch to `from_db` default + "live data" toggle | Phase 2, day 2 | Yes |
| 10 | (Optional) `engine/edge/regime_label.py` + Angular status card | Phase 3 | Yes |

Each step is a separate PR. The pipeline can ship to production after step 6
and operate via shell loops + curl, even before steps 7-10 land.

---

## 9. Risks & mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Polygon snapshot endpoint rate-limits or temporarily errors mid-backfill | Medium | Medium | Shell loop is resumable (idempotent endpoint); skip-and-log on partial failure; `force=true` to retry. |
| `parity_dividend` produces nonsense q for thin / illiquid expiries | Medium | Low | Sanity-bound to [-5%, 20%]; fall back to vendor q on out-of-range. |
| Backfill IV values drift from `iv_builder` historical values for the same date | Low | Medium | Both paths now use the same canonical solver (PR #25); a parity test on shared dates pins this. |
| `options_chain_quotes` row count grows unexpectedly large (e.g., adding a 5th ticker doubles it) | Low | Low | Postgres handles this trivially; partition by year if it ever exceeds 100M rows. |
| Edge frontend defaults to `from_db` and user flips to a date with no data → empty charts | Medium | Low | Coverage endpoint (`/api/edge/realized-vs-iv/coverage/{symbol}`) already exists; surface "no data" state in chart wrappers. |
| Cluster IDs from regime clustering are unstable across runs (different seeds) | Low | Low | Hungarian alignment in `regime_drift` already handles this for drift refits; for fresh fits we'd need to align against a stored "canonical" centroid set if we want stable labels across deploys. Open Question for v2. |

---

## 10. Open questions (deferred to plan doc)

The six open questions blocking the build live in
[`vol-surface-dashboard-plan.md`](vol-surface-dashboard-plan.md) § 6:

- Q1 — regime framing (clusters only vs labeler on top)
- Q2 — .NET passthrough for new endpoint
- Q3 — regime status card
- Q4 — schema (recommendation in this TDD: § 4.3 — new `options_chain_quotes` table)
- Q5 — universe (SPY+QQQ+IWM+DIA recommended to match Edge)
- Q6 — backfill (recommended yes)

This TDD's § 4.3 commits to Q4b (new table). The other five remain user
decisions; they affect what gets built but not how the built parts work.

---

## 11. References

### 11.1 Internal documents

- [`CLAUDE.md`](../../CLAUDE.md) — repo guiding philosophy, including § 5 sovereignty rule.
- [`.claude/rules/numerical-rigor.md`](../../.claude/rules/numerical-rigor.md) — golden fixtures, tolerances, timestamp policy.
- [`.claude/rules/python.md`](../../.claude/rules/python.md) — FastAPI / Pydantic / pandas conventions.
- [`docs/architecture/options-math-authorities.md`](options-math-authorities.md) — single sources of truth for options calculations (PR #25).
- [`docs/architecture/edge-feature-design.md`](edge-feature-design.md) — Edge feature canonical spec (PR #26).
- [`docs/architecture/edge-functionality-testing.md`](edge-functionality-testing.md) — Edge testing guide.
- [`docs/architecture/design-handoff-edge-2026-04-25.md`](design-handoff-edge-2026-04-25.md) — Edge design handoff.
- [`docs/architecture/vol-surface-dashboard-plan.md`](vol-surface-dashboard-plan.md) — phased build plan + open questions.
- [`PythonDataService/app/research/options/README.md`](../../PythonDataService/app/research/options/README.md) — IV pipeline data-fetching reference.

### 11.2 External

- Hull, J. C. *Options, Futures and Other Derivatives*, 11e — BS, parity.
- Gatheral, J. *The Volatility Surface* — surface fitting, smile dynamics.
- Garman, M. & Klass, M. (1980), *Journal of Business* — GK estimator.
- Yang, D. & Zhang, Q. (2000), *Journal of Business* — YZ estimator.
- Bollerslev, Tauchen, Zhou (2009), *Review of Financial Studies* — variance risk premium.
- Carr, P. & Wu, L. (2009), *Review of Financial Studies* — variance risk premium decomposition.
- CBOE VIX Whitepaper (2019) — variance-time IV interpolation methodology.
- Rabiner (1989) — Hidden Markov Model tutorial; basis for `regime_clustering` HMM.
- Arthur & Vassilvitskii (2007) — k-means++; basis for `regime_clustering` k-means.
- López de Prado, *Advances in Financial Machine Learning* — DSR, PBO; basis for `robustness_stats`.
- py_vollib — golden-fixture reference for IV solver parity tests.

---

*End of TDD. All `Status: Built` items are traceable to a file path in this
repo. All `Status: To build` items have a defined module, signature, schema,
and acceptance test. Ready for review.*
