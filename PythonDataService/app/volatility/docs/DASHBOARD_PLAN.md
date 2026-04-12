# IV Surface Dashboard — Implementation Plan (v3)

## Overview

An interactive dashboard for visualizing and validating implied volatility surfaces built by the `app/volatility` module. Split into a small set of static files (no build step) for maintainability. Fetches data from RESTful FastAPI endpoints backed by deterministic, disk-persistent surface cache. Designed from the start for historical date-stepping, multi-method comparison, and 2-year validation runs.

---

## Architecture

```
┌───────────────────────────────────────────────────────────────────┐
│  Static Dashboard (no build step, 5 files)                        │
│                                                                   │
│  dashboard.html ─────── layout, controls, panel containers        │
│  dashboard.js ───────── state, fetch, controls, build modes       │
│  dashboard_panels.js ── 6 Plotly renderers + ΔIV heatmap          │
│  dashboard.css ──────── dark theme                                │
│  sample_data.js ─────── frozen SPY chain (no-arb by construction) │
│                                                                   │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │ Controls Bar                                                 │  │
│  │ [Ticker] [Date ◄ ■ ►] [Method ▾] [Axis ▾]                  │  │
│  │ [Mode: Build|Cached|Auto] [Build] [CSV↑] [Compare] [Export] │  │
│  └─────────────────────────────────────────────────────────────┘  │
│                                                                   │
│  ┌──────────────┬──────────────┬─────────────────────────────┐   │
│  │ 3D Surface   │ Smile Slices │  Market vs Fit Scatter      │   │
│  ├──────────────┼──────────────┼─────────────────────────────┤   │
│  │ Term Struct  │ Diagnostics  │  Rejection Breakdown        │   │
│  └──────────────┴──────────────┴─────────────────────────────┘   │
└────────────────────┬──────────────────────────────────────────────┘
                     │
                     │ RESTful endpoints, disk-cached by surface_id
                     ▼
┌───────────────────────────────────────────────────────────────────┐
│  PythonDataService (FastAPI)                                       │
│                                                                   │
│  POST /api/volatility/surface/build-from-ticker                   │
│  POST /api/volatility/surface/build-from-csv                      │
│  GET  /api/volatility/surface/{id}/grid                           │
│  GET  /api/volatility/surface/{id}/smiles                         │
│  GET  /api/volatility/surface/{id}/diagnostics                    │
│  POST /api/volatility/surface/{id}/query                          │
│  GET  /api/volatility/surface/{id}/export/{format}                │
│  POST /api/volatility/surface/batch-summary                       │
│                                                                   │
│  app/volatility/                                                  │
│  ├── solver.py              ── QuantLib IV solver + Brent          │
│  ├── fitting.py             ── SABR / SVI / variance interp       │
│  ├── surface.py             ── surface builder + cross-expiry      │
│  ├── models.py              ── Pydantic schemas (all endpoints)    │
│  ├── analytics.py  (NEW)    ── 25Δ RR/BF, skew slope, PC-parity   │
│  ├── data_loader.py (NEW)   ── Polygon chain fetcher + filters     │
│  ├── cache.py (NEW)         ── deterministic IDs + disk persistence│
│  └── conventions.py (NEW)   ── forward/daycount/discount defs      │
│                                                                   │
│  cache/                                                           │
│  ├── surfaces/{surface_id}.meta.json                              │
│  ├── grids/{surface_id}.parquet                                   │
│  └── smiles/{surface_id}.json                                     │
│                                                                   │
│  routers/volatility.py (restructured)                             │
└───────────────────────────────────────────────────────────────────┘
```

---

## Foundational Conventions (new in v3)

### Forward / Discount / Daycount definitions

**File**: `app/volatility/conventions.py`

Every surface build uses explicit, immutable conventions so that `log(K/F)` is stable across builds:

```python
@dataclass(frozen=True)
class SurfaceConventions:
    """Immutable conventions anchoring a surface build."""
    day_count: str = "Actual365Fixed"       # QuantLib Actual365Fixed
    forward_model: str = "bsm"              # F = S * exp((r - q) * T)
    discount_model: str = "continuous"       # df = exp(-r * T)
    rate: float = 0.05                      # continuous risk-free rate
    dividend_yield: float = 0.0             # continuous dividend yield
    calendar: str = "NullCalendar"          # no holiday adjustments
```

These conventions are hashed into `surface_id` and stored in every cached artifact, so two surfaces built with different rate assumptions can never collide.

The `forward()` and `discount_factor()` methods live here, not scattered across files. Every module that needs `F` or `df` imports from `conventions.py`.

### Deterministic surface_id (concern #1)

The `surface_id` is a SHA-256 hash of all inputs that affect the output:

```python
def compute_surface_id(
    ticker: str,
    date: str,
    method: str,
    conventions: SurfaceConventions,
    filters: DataFilters,
    n_options: int,
) -> str:
    """Deterministic, content-addressed cache key."""
    key = {
        "ticker": ticker,
        "date": date,
        "method": method,
        "rate": conventions.rate,
        "dividend": conventions.dividend_yield,
        "day_count": conventions.day_count,
        "forward_model": conventions.forward_model,
        "min_dte": filters.min_dte,
        "max_dte": filters.max_dte,
        "min_open_interest": filters.min_open_interest,
        "max_spread_pct": filters.max_spread_pct,
        "n_options": n_options,
        "schema_version": SCHEMA_VERSION,
    }
    raw = json.dumps(key, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:20]
```

Properties:
- Identical inputs always produce the same `surface_id`
- Changing rate, method, or filters produces a different ID
- Includes `schema_version` so cache invalidates on model upgrades

---

## Cache Layer (concern #2)

**File**: `app/volatility/cache.py`

### Disk-backed persistence

```
cache/
├── surfaces/
│   └── {surface_id}.meta.json      # build params, conventions, summary, schema_version
├── grids/
│   └── {surface_id}.parquet        # matrix grid (strike × ttm → iv)
├── smiles/
│   └── {surface_id}.json           # per-expiry fitted + market IVs
└── diagnostics/
    └── {surface_id}.json           # rejections, arbitrage, params
```

### Cache strategy

| Operation | Cache behavior |
|-----------|---------------|
| `build-from-ticker` | Compute `surface_id` from inputs. If `.meta.json` exists and `schema_version` matches, return cached summary immediately (no recompute). Otherwise build, write all artifacts, return summary. |
| `GET /grid` | Read from `grids/{id}.parquet`. 404 if not found. |
| `GET /smiles` | Read from `smiles/{id}.json`. 404 if not found. |
| `GET /diagnostics` | Read from `diagnostics/{id}.json`. 404 if not found. |

### Build modes (dashboard controls)

| Mode | Behavior |
|------|----------|
| **Auto** (default) | Try cache first, build if miss |
| **Build** | Always rebuild (ignore cache) |
| **Cached** | Cache only, fail if miss (for fast historical browsing) |

Passed as query param: `POST /build-from-ticker?mode=auto`

### Versioning (concern #8)

Every cached artifact includes:

```json
{
  "schema_version": "1.0.0",
  "quantlib_version": "1.41",
  "built_at": "2026-04-12T14:30:00Z",
  "conventions": { ... }
}
```

On load, `cache.py` checks `schema_version`. If it doesn't match the current code's version, the cached file is treated as a miss (rebuild).

---

## Grid Response Shape (concern #3)

The grid endpoint returns a **matrix**, not a point-list:

```json
{
  "x": [-0.30, -0.25, -0.20, ...],
  "y": [14, 30, 60, 90, 180, 365],
  "z": [
    [0.32, 0.30, 0.28, ...],
    [0.31, 0.29, 0.27, ...],
    ...
  ],
  "x_label": "log_moneyness",
  "y_label": "dte_days",
  "z_label": "implied_vol",
  "meta": {
    "spot": 512.30,
    "forwards": [513.1, 514.2, 516.5, ...],
    "n_strikes": 50,
    "n_expiries": 6
  }
}
```

Properties:
- `x` = moneyness axis values (length `n_strikes`)
- `y` = expiry axis values in **days** (length `n_expiries`)
- `z` = IV matrix (shape `n_expiries × n_strikes`)
- Directly plottable as `Plotly.newPlot(div, [{type:'surface', x, y, z}])`
- Forwards included so dashboard can convert between axis modes client-side
- Same matrix shape used for heatmaps in comparison mode

---

## Expiry Specification (concern #4)

All external-facing APIs accept expiries as **ISO dates or DTE days**, never raw floats:

| Parameter | Format | Example |
|-----------|--------|---------|
| `expiry_dates` | ISO 8601 strings | `["2025-05-17", "2025-06-21"]` |
| `dte_days` | integers | `[14, 30, 60, 90, 180, 365]` |

Internally, `conventions.py` converts to year fractions using the configured day count:

```python
def dte_to_ttm(dte_days: int) -> float:
    """Convert DTE in calendar days to year fraction using Actual/365 Fixed."""
    return dte_days / 365.0
```

Grid and smile responses always include both the `dte_days` and `expiry_date` for each slice, so the dashboard never needs to guess.

---

## 25Δ Inversion Safety (concern #6)

The 25Δ strike finder uses bracketed root-finding with explicit failure handling:

```python
def find_delta_strike(
    surface: VolSurface,
    target_delta: float,      # e.g., 0.25 or -0.25
    ttm: float,
    spot: float,
    conventions: SurfaceConventions,
    bracket: tuple[float, float] = (-0.5, 0.5),  # log-moneyness range
) -> Optional[DeltaStrikeResult]:
    """
    Find strike K where bs_delta(S, K, T, r, σ(K)) = target_delta.

    Returns None if solver fails (flat region, no crossing, divergence).
    """
```

Mitigations:
- Bracket on log-moneyness `[-0.5, 0.5]` (not strike space — avoids scale issues)
- `brentq` with 50 iteration limit
- If root-find fails, mark 25Δ metrics as `null` for that expiry (not NaN, not crash)
- Store solver diagnostics (iterations, bracket values, convergence flag)
- Dashboard renders missing 25Δ points as gaps in the term structure, not interpolated values

---

## API Endpoint Specification (revised)

### Resource: `/api/volatility/surface`

| Method | Path | Purpose | Returns |
|--------|------|---------|---------|
| `POST` | `/surface/build-from-ticker` | Fetch chain → build → cache | `{ surface_id, summary }` |
| `POST` | `/surface/build-from-csv` | Parse CSV → build → cache | `{ surface_id, summary }` |
| `POST` | `/surface/build` | Accept structured records → build → cache | `{ surface_id, summary }` |
| `GET` | `/surface/{id}/grid` | Matrix grid from cache | `{ x, y, z, meta }` |
| `GET` | `/surface/{id}/smiles` | Per-expiry fitted + market IV | `{ slices }` |
| `GET` | `/surface/{id}/diagnostics` | Full diagnostics | `{ summary, rejections, arbitrage, params, health_score }` |
| `POST` | `/surface/{id}/query` | Query specific (K, T) points | `{ results }` |
| `GET` | `/surface/{id}/export/{format}` | Export artifacts | JSON, CSV, or Parquet file download |
| `POST` | `/surface/batch-summary` | Multi-date summary for slider | `{ daily_summaries }` |

### Query parameters

| Param | Applies to | Values | Default |
|-------|-----------|--------|---------|
| `axis` | `/grid`, `/smiles` | `log_moneyness`, `moneyness`, `strike` | `log_moneyness` |
| `n_strikes` | `/grid` | 10–500 | 50 |
| `dte_days` | `/grid` | comma-separated ints | fitted expiries |
| `expiry_dates` | `/grid` | comma-separated ISO dates | fitted expiries |
| `mode` | `/build-*` | `auto`, `build`, `cached` | `auto` |
| `format` | `/export/{format}` | `json`, `csv`, `parquet` | — |

### Build-from-ticker request

```json
{
  "ticker": "SPY",
  "date": "2025-04-11",
  "method": "svi",
  "min_dte": 7,
  "max_dte": 365,
  "min_open_interest": 10,
  "max_spread_pct": 0.20,
  "conventions": {
    "rate": 0.053,
    "dividend_yield": 0.013,
    "day_count": "Actual365Fixed",
    "forward_model": "bsm"
  }
}
```

### Build response (lightweight)

```json
{
  "surface_id": "a1b2c3d4e5f6g7h8i9j0",
  "ticker": "SPY",
  "spot": 512.30,
  "method": "svi",
  "date": "2025-04-11",
  "cached": false,
  "n_expiries": 8,
  "n_contracts_accepted": 320,
  "n_contracts_rejected": 130,
  "build_time_ms": 842,
  "health_score": 87,
  "valid": true,
  "schema_version": "1.0.0"
}
```

### Batch-summary request (for date slider)

```json
{
  "ticker": "SPY",
  "start_date": "2025-01-01",
  "end_date": "2025-04-11",
  "method": "svi",
  "mode": "cached"
}
```

### Batch-summary response

```json
{
  "ticker": "SPY",
  "daily_summaries": [
    {
      "date": "2025-01-02",
      "surface_id": "...",
      "atm_iv": 0.182,
      "rr_25d": -0.042,
      "bf_25d": 0.008,
      "skew_slope": -0.15,
      "n_contracts": 450,
      "health_score": 91,
      "cached": true
    },
    ...
  ]
}
```

---

## Model Health Score (new)

A composite 0–100 score for fast scanning across dates:

| Component | Weight | Score logic |
|-----------|--------|-------------|
| Solver convergence rate | 25% | 100 if >95%, linear down to 0 at <70% |
| Fit RMSE (avg across slices) | 25% | 100 if <0.005, 0 if >0.05, linear between |
| Rejection rate | 25% | 100 if <10%, 0 if >50%, linear between |
| Arbitrage violations | 25% | 100 if 0 violations, -10 per butterfly, -15 per calendar |

Stored in `.meta.json` and returned in build response + batch summary. Dashboard renders as a colored badge: green (80+), yellow (60-79), red (<60).

---

## Dashboard Panels (6 panels, 2×3 grid)

### Layout

```
┌──────────────────────────────────────────────────────────────────────────┐
│  CONTROLS BAR                                                            │
│  [Ticker] [Date ◄ ■ ►] [Method ▾] [Axis ▾] [Mode ▾]                    │
│  [Build] [CSV↑] [Compare] [Export ▾]   Health: ██████░░ 87              │
├───────────────────────┬───────────────────────┬──────────────────────────┤
│                       │                       │                          │
│  1. 3D VOL SURFACE    │  2. SMILE SLICES      │  3. MARKET vs FIT        │
│                       │                       │                          │
│  Plotly 3D mesh       │  Fitted curves +      │  Scatter: market IV      │
│  log(K/F) × DTE × IV │  market dots per       │  vs model IV             │
│  Rotate / zoom        │  expiry. ATM line.     │  y=x line, R², RMSE     │
│  Color = IV level     │  Legend by DTE.        │  Size ∝ vega             │
│                       │  Red zones = arb       │  Color = expiry          │
│                       │                       │                          │
├───────────────────────┼───────────────────────┼──────────────────────────┤
│                       │                       │                          │
│  4. TERM STRUCTURE    │  5. DIAGNOSTICS       │  6. REJECTION            │
│                       │                       │     BREAKDOWN            │
│  IV vs DTE at:        │  Summary cards         │                          │
│  • ATM                │  Health score badge    │  Horizontal bar chart    │
│  • 95% / 105%         │  Per-slice table       │  by rejection reason     │
│  • 25Δ put / call     │  Fitted params         │  + accepted/total donut  │
│  (gaps where 25Δ      │  Arbitrage detail:     │  + top-5 rejected table  │
│   solver failed)      │   count, severity,     │                          │
│                       │   worst ranges         │                          │
│                       │  Warnings list         │                          │
│                       │                       │                          │
└───────────────────────┴───────────────────────┴──────────────────────────┘
```

### Comparison mode additions (Phase 3)

When comparison is active, the bottom row changes:

```
├───────────────────────┬───────────────────────┬──────────────────────────┤
│  4. TERM STRUCTURE    │  5. ΔIV HEATMAP       │  6. PARAM DIFF TABLE     │
│  (both overlaid)      │  moneyness × DTE      │  Side-by-side params     │
│                       │  color = IV_A − IV_B  │  RMSE delta, health Δ    │
└───────────────────────┴───────────────────────┴──────────────────────────┘
```

The **ΔIV Heatmap** (concern #7) is a 2D Plotly `heatmap` of `IV_left − IV_right` on a moneyness × DTE grid. This is more readable than a 3D difference surface and directly reveals where two models/dates disagree.

---

## Export Support (new)

Each `surface_id` can be exported in multiple formats:

| Endpoint | Format | Content |
|----------|--------|---------|
| `GET /surface/{id}/export/json` | JSON | Full diagnostics + params + grid + smiles |
| `GET /surface/{id}/export/csv` | CSV | Grid as `moneyness, dte_days, iv` rows |
| `GET /surface/{id}/export/parquet` | Parquet | Grid matrix (efficient for large grids) |

Dashboard includes an "Export" dropdown button that triggers a download.

---

## Data Strategy

### Three-tier sourcing (unchanged)

| Priority | Source | Trigger |
|----------|--------|---------|
| 1 | Polygon via PythonDataService | User enters ticker + date, clicks Build |
| 2 | CSV upload | User clicks Upload CSV |
| 3 | Synthetic (SPY-like) | Dashboard loads, no server, or user clicks "Demo" |

### Synthetic data contract (no-arb by construction)

Total variance generated via SVI with known-good parameters where `a(T)` is strictly increasing in `T`. Per-expiry smile is convex by construction (`b > 0`, `|ρ| < 1`). Validated with the same arbitrage checker used on real surfaces. See v2 for pseudocode.

---

## Skew Analytics Module

**File**: `app/volatility/analytics.py`

| Metric | Formula | Notes |
|--------|---------|-------|
| 25Δ Risk Reversal | σ(25Δ call) − σ(25Δ put) | `None` if 25Δ solver fails |
| 25Δ Butterfly | [σ(25Δ call) + σ(25Δ put)] / 2 − σ(ATM) | `None` if 25Δ solver fails |
| ATM IV | σ at K = F | always available |
| Skew slope | ∂σ/∂k at k = 0 | finite difference, Δk = 0.01 |
| PC-parity forward | Implied F from C − P = (F − K) × df | data quality check per expiry |
| Health score | weighted composite (see above) | 0–100, stored in meta |

25Δ inversion uses bracketed `brentq` on log-moneyness `[-0.5, 0.5]` with explicit `None` on failure.

---

## Implementation Phases (revised)

### Phase 1 — Infrastructure: cache, conventions, restructured API, batch-summary

Batch-summary is promoted to Phase 1 (concern #9) because the date slider needs precomputed metadata.

**Files**: `conventions.py`, `cache.py`, `data_loader.py`, `analytics.py`, `models.py`, `routers/volatility.py`

| Step | Work |
|------|------|
| 1.1 | Create `conventions.py` — `SurfaceConventions` dataclass, `forward()`, `discount_factor()`, `dte_to_ttm()` |
| 1.2 | Create `cache.py` — deterministic `compute_surface_id()`, disk read/write for meta/grid/smiles/diagnostics, schema version check |
| 1.3 | Create `data_loader.py` — fetch option chain from Polygon, apply filters (OI, spread, DTE), return structured records |
| 1.4 | Restructure `routers/volatility.py` — RESTful `/surface/...` endpoints, build modes (auto/build/cached) |
| 1.5 | Implement matrix-shaped `/surface/{id}/grid` response |
| 1.6 | Implement `/surface/{id}/smiles` — per-expiry fitted + market IVs with DTE days + ISO dates |
| 1.7 | Implement `/surface/{id}/diagnostics` — rejections, structured arbitrage, health score |
| 1.8 | Create `analytics.py` — 25Δ RR/BF (with safe inversion), skew slope, PC-parity forward, health score |
| 1.9 | Implement `build-from-ticker`, `build-from-csv` combo endpoints |
| 1.10 | Implement `batch-summary` endpoint (promoted from Phase 2.5) |
| 1.11 | Implement `/surface/{id}/export/{format}` endpoint |
| 1.12 | Update all Pydantic models for new response shapes |
| 1.13 | Tests: cache determinism, conventions, data_loader filters, analytics, all endpoints |

### Phase 2 — Dashboard (core 6 panels + date controls)

**Files**: `docs/dashboard.html`, `docs/dashboard.js`, `docs/dashboard_panels.js`, `docs/dashboard.css`, `docs/sample_data.js`

| Step | Work |
|------|------|
| 2.1 | `dashboard.html` — layout skeleton, CDN imports (Plotly, Tailwind), panel containers, export dropdown |
| 2.2 | `dashboard.js` — state management, fetch wrappers, controls bar (ticker, date, method, axis, mode), build mode logic |
| 2.3 | `sample_data.js` — no-arb synthetic SPY chain + precomputed matrix grid |
| 2.4 | Panel 1: 3D surface (Plotly `surface` trace, matrix z, log-moneyness default) |
| 2.5 | Panel 2: Smile slices (fitted curves + market dots, ATM line, red arb zones, legend by DTE) |
| 2.6 | Panel 3: Market vs Fit scatter (R², RMSE, vega-weighted size, click-to-highlight) |
| 2.7 | Panel 4: Term structure (ATM, 95%, 105%, 25Δ put/call with gaps on failure) |
| 2.8 | Panel 5: Diagnostics HTML (summary cards, health badge, per-slice table, params, arb detail, warnings) |
| 2.9 | Panel 6: Rejection breakdown (bar chart + donut + top-5 rejected table) |
| 2.10 | CSV upload handler |
| 2.11 | Date picker + prev/next + date slider (wired to `batch-summary` for metadata, `build-from-ticker` for surface) |
| 2.12 | Historical mini-chart: ATM IV + health score sparkline above date slider |

### Phase 3 — Comparison mode + ΔIV heatmap

**Files**: `docs/dashboard_panels.js` (extend), `docs/dashboard.js` (extend)

| Step | Work |
|------|------|
| 3.1 | Compare dropdown: SABR vs SVI, today vs N days ago, ticker A vs B |
| 3.2 | ΔIV heatmap (Plotly `heatmap`, moneyness × DTE, color = IV difference) |
| 3.3 | Overlaid term structures (both surfaces on same axes) |
| 3.4 | Param diff table (side-by-side SABR/SVI params, RMSE delta, health delta) |
| 3.5 | Optional: 3D difference surface (for those who want it) |

---

## File Summary

| File | New/Edit | Purpose |
|------|----------|---------|
| `app/volatility/conventions.py` | **New** | Forward, discount, daycount definitions |
| `app/volatility/cache.py` | **New** | Deterministic IDs, disk persistence, version check |
| `app/volatility/data_loader.py` | **New** | Polygon → filtered option chain records |
| `app/volatility/analytics.py` | **New** | 25Δ RR/BF, skew slope, PC-parity, health score |
| `app/volatility/models.py` | **Edit** | Matrix grid, smiles, diagnostics, batch-summary, export |
| `app/routers/volatility.py` | **Edit** | RESTful endpoints, build modes, batch-summary, export |
| `app/volatility/docs/dashboard.html` | **New** | Layout + CDN imports |
| `app/volatility/docs/dashboard.js` | **New** | State, fetch, controls, build modes, date slider |
| `app/volatility/docs/dashboard_panels.js` | **New** | 6 panel renderers + ΔIV heatmap |
| `app/volatility/docs/dashboard.css` | **New** | Dark theme |
| `app/volatility/docs/sample_data.js` | **New** | No-arb synthetic SPY chain |
| `tests/volatility/test_conventions.py` | **New** | Forward/discount consistency |
| `tests/volatility/test_cache.py` | **New** | Deterministic IDs, version invalidation |
| `tests/volatility/test_analytics.py` | **New** | Skew metrics, health score |
| `tests/volatility/test_data_loader.py` | **New** | Data loading + filtering |
| `tests/volatility/test_endpoints.py` | **New** | Integration tests for all endpoints |

---

## Decisions Log

| # | Concern | Resolution |
|---|---------|------------|
| 1 | surface_id must be deterministic | SHA-256 of (ticker, date, method, conventions, filters, schema_version) |
| 2 | Memory-only cache won't survive 2 years | Disk-backed: `.meta.json`, `.parquet`, `.json` per surface_id |
| 3 | Grid as point-list is slow | Matrix response: `{x, y, z}` directly plottable by Plotly |
| 4 | TTM floats are fragile | External API uses `dte_days` (int) or `expiry_dates` (ISO). Internal conversion via conventions. |
| 5 | Forward definition must be consistent | `conventions.py` with frozen `SurfaceConventions` hashed into surface_id |
| 6 | 25Δ inversion can be unstable | Bracketed brentq on `[-0.5, 0.5]` log-moneyness, explicit `None` on failure |
| 7 | 3D diff surface is hard to read | ΔIV heatmap (2D, moneyness × DTE) as primary comparison view |
| 8 | Cached artifacts need versioning | `schema_version` + `quantlib_version` in every artifact; stale = cache miss |
| 9 | Batch-summary needed earlier | Promoted to Phase 1 (date slider depends on it) |
| — | Build mode control | Auto (cache-first) / Build (force) / Cached (read-only) |
| — | Health score | Composite 0–100 from convergence, RMSE, rejection, arbitrage |
| — | Export support | JSON, CSV, Parquet per surface_id |
| — | Demo ticker | SPY |
| — | Historical browsing | Phase 2 with date slider + batch-summary |
| — | Comparison mode | Phase 3 with ΔIV heatmap |
