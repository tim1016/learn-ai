# learn-ai Options Infrastructure Audit

**Purpose:** Establish ground truth for what already exists in the repo before rewriting the vol-surface-dashboard TDD. Done by reading the cloned `master` branch on 2026-04-25.

**TL;DR:** Roughly **60-70% of the data and math layers already exist**, often at high quality. The genuinely new work is primarily the regime classifier, the IV-rank/percentile layer, the unified caching abstraction, and the Angular dashboard. The original 2-3 week MVP estimate is now closer to **2 weeks of core build + 1 week of integration**, but only if we reuse aggressively.

---

## Audit findings, by question

### Q1. Does the IV solver actually work?

**Yes. Production quality.**

- **Location:** `PythonDataService/app/volatility/solver.py` (416 lines)
- **Implementation:** QuantLib's `VanillaOption.impliedVolatility()` as primary, custom Brent's method as fallback for edge cases
- **API:** `implied_volatility(option_price, spot, strike, ttm, rate, dividend, is_call, vol_guess, min_ttm) -> ImpliedVolResult`
- **Status enum** with full diagnostic surface: `ok`, `quantlib_ok`, `brent_fallback`, `intrinsic_violation`, `price_too_low`, `expired`, `convergence_failure`, `input_error`
- **Intraday-aware:** `min_ttm` parameter override allows 0DTE solving down to 1 minute
- **Tested:** `tests/volatility/test_solver.py` exists. Plus a *second* solver in `app/research/options/bs_solver.py` with its own test file.
- **Active callers** (production code paths):
  - `app/volatility/surface.py` (surface builder)
  - `app/services/options_companion_service.py` (data lab CSV builder)
  - `app/engine/strategy/algorithms/spy_ema_crossover_options.py` (your EMA strategy with options layer)
  - `app/research/options/iv_builder.py` (the 30d constant-maturity IV pipeline)

**One concern worth flagging:** there are **two** IV solvers in the codebase — `app/volatility/solver.py` (QuantLib + Brent) and `app/research/options/bs_solver.py` (apparently a separate implementation). Both are referenced as `implied_volatility`. This violates `CLAUDE.md` § 5 ("one authority for any given numerical answer"). Either consolidating these or formally documenting why two exist is a prerequisite to adding more IV-dependent code on top.

**Recommendation for TDD:** Reuse `app/volatility/solver.py` as-is. Do not build a new inverter. Audit and consolidate the duplicate as a separate cleanup task.

---

### Q2. Is `OptionsIvSnapshot` populated by any code path?

**Yes — but the writer is in C# and the data flow is roundabout.**

- **Entity:** `Backend/Models/MarketData/OptionsIvSnapshot.cs`
- **Schema:** ticker, trading_date, iv_30d_atm, iv_30d_put, iv_30d_call, stock_close, dte_low, dte_high, price_source, source ("derived")
- **Writer:** `Backend/Services/Implementation/ResearchService.cs:516` — method `PersistIvDataAsync`. Writes a batch after parsing IV records returned from a research report.
- **Reader:** `Backend/Services/Implementation/ResearchService.cs:370` — same service reads it back as a cache
- **Data source for the writer:** records come from a Python-generated research report (presumably `app/research/options/iv_builder.py` based on the schema match — `iv_30d_atm`, `iv_30d_put`, `iv_30d_call`, `stock_close`, `dte_low`, `dte_high` line up exactly)
- **Python writer:** none. Python computes; .NET persists. This is consistent with `CLAUDE.md` § 5 (Python owns math, .NET is transport).

**What this means:** the existing pipeline produces 30-day constant-maturity ATM IV, persists it daily, and serves it back as a research-side cache. **It does not produce term structure across multiple tenors (7d/14d/60d/90d), it does not produce skew metrics, and it does not produce IV-rank.** It is the *foundation* of what the dashboard needs but not a complete substitute.

**Schema gap:** `OptionsIvSnapshot` has only 30d fields. Adding 7d/14d/60d/90d means either extending this entity or creating a new wider one. Recommend extending — it's the same conceptual entity, just more tenors.

**Recommendation for TDD:** Treat `OptionsIvSnapshot` as the existing layer-1 cache. Extend it with multi-tenor columns. Build a *new* `SurfaceMetricsDaily` entity (skew, term structure, percentiles, regime) on top.

---

### Q3. What's the production state of the `volatility/` module?

**Substantial. ~3,000 lines, mounted in `main.py`, used in production.**

| File | Lines | Purpose | Status |
|---|---|---|---|
| `solver.py` | 416 | QuantLib + Brent IV inversion | Production, well-tested |
| `surface.py` | 375 | Build IV surface from option records | Production |
| `fitting.py` | 392 | SVI / SABR / variance fitting | Production |
| `analytics.py` | 351 | Skew metrics, health score | Production |
| `data_loader.py` | 410 | Load chains via polygon_client + filter | Production |
| `models.py` | 384 | Pydantic models for surface API | Production |
| `cache.py` | 314 | **Disk-backed surface cache, deterministic IDs** | Production |
| `conventions.py` | 110 | Date/yield/dividend conventions | Production |
| `example.py` | 161 | Reference usage example | Reference |

**Mounted at** `/api/volatility` via `app/routers/volatility.py`. Existing endpoints (per the router head I read):

- `POST /surface/build` — build from OptionRecord list
- `POST /surface/build-from-ticker` — fetch chain via data_loader
- `POST /surface/build-from-csv` — parse CSV text
- `GET /surface/{id}/grid` — matrix grid (x, y, z)
- `GET /surface/{id}/smiles` — per-expiry fitted + market smiles
- `GET /surface/{id}/diagnostics` — full diagnostics
- `POST /surface/{id}/query` — query specific (K, T) points
- `GET /surface/{id}/export/{format}` — JSON/CSV/Parquet
- `POST /surface/batch-summary` — build/load surfaces for date range

**This means:** if you fetch a chain today and POST to `/surface/build-from-ticker`, you already get back a fitted surface with skew analytics, smile fits, and diagnostics. **This entire layer is built.**

**What it does NOT yet do:**
- Persist surfaces to a relational DB. The cache module (`volatility/cache.py`) is **disk-backed (parquet/json on filesystem)**, not Postgres. Mismatch with our planned `SurfaceMetricsDaily` table.
- Compute IV-rank or IV-percentile against trailing history (it operates on a single date)
- Compute realized-vol-vs-implied spread
- Produce a regime classification

**Recommendation for TDD:** Keep `volatility/` exactly as it is for surface construction and analytics. Add a **new** persistence layer that hooks into the surface output and writes to Postgres. The disk cache continues to serve its current purpose (caching the heavy fit computation); the DB persistence is a separate concern (longitudinal series for percentiles).

---

### Q4. Where's the half-built caching code?

**Surprisingly little. The "caching mechanism" you remembered building is probably one of the following:**

- **`app/volatility/cache.py`** — but this is a *disk* cache for fitted surfaces, not a Polygon-fetch cache. It does NOT mediate between the app and Polygon.
- **`PolygonClientService` itself** — has rate-limit throttling but no read-through caching. Every `fetch_aggregates`, `list_options_contracts`, `get_stock_snapshot` call hits Polygon directly. The throttle is in `polygon_client.py` (proactive, blocks before sending; no DB lookup before fetch).
- **The .NET-side `OptionsIvSnapshot` write/read pattern** — this *is* a caching pattern at the analytical-output level (don't recompute the IV report if we already have it for this date), but it's not a Polygon-data cache.

**Conclusion:** there is **no existing read-through cache between the app and Polygon**. What exists is (a) request throttling and (b) write-after-compute persistence of derived results. Neither is what was discussed in our prior conversation as "fetch once, serve from DB on subsequent requests."

This makes the caching layer in the next TDD essentially **net new**. The good news: nothing to migrate or untangle. The not-as-good news: the time estimate I gave in the previous round (~1 week added for caching work) stands; nothing pre-built reduces it.

**Recommendation for TDD:** Build the cache abstraction from scratch. Apply it to (a) underlying-bars fetching, (b) options-chain snapshot fetching, (c) options-contracts metadata fetching. Do not try to retrofit it inside `polygon_client.py` — wrap, don't modify, since `polygon_client` is heavily used and changing its semantics risks breaking many callers.

---

### Q5. Anything computing realized vol on intraday bars?

**Yes, but it's the close-to-close estimator, not Garman-Klass.**

- `app/research/features/ta_features.py` — `compute_realized_vol_30()`: 30-bar rolling std of log close-to-close returns. Used in regime detection (`app/research/signal/regime.py`) for vol-tercile classification.
- **No Garman-Klass anywhere.** Searched for `garman`, `klass`, `GK_` — zero hits.
- Existing impl is at the *bar* level (works on 1-min, 5-min, etc.), not the daily-aggregated annualized form we'd want for IV-RV spread.

**What this means:** the dashboard's realized-vol component needs new code. The existing function is good for ML features but not for vol-surface analytics where you want annualized RV at standard windows (10d, 20d, 30d, 60d, 90d) using a low-variance estimator.

**Recommendation for TDD:** Add `app/volatility/realized_vol.py` with both:
- `garman_klass_daily(daily_ohlc) -> Series` — daily GK estimator
- `gk_rolling_annualized(daily_ohlc, window) -> Series` — annualized rolling at standard windows
Tested with golden fixtures against a reference impl (Sinclair's textbook example, or `arch` library).

---

## Bonus inventory: what's in the Frontend

You already have these components, which are relevant context for the dashboard plan:

- `Frontend/src/app/components/options-chain-v2/` — options chain viewer
- `Frontend/src/app/components/options-history/` — historical options data view
- `Frontend/src/app/components/options-strategy-lab/` — strategy P&L lab including the PayoffChart
- `Frontend/src/app/components/research-lab/options-math-docs/` — formula documentation viewer
- `Frontend/src/app/components/market-data/volume-chart/` — volume visualization

The new `vol-surface-dashboard` component should be a sibling of these, not a replacement. There's likely existing routing, layout, and theming we can reuse.

---

## What this means for the TDD rewrite

The audit changes the build dramatically. Here's the corrected scope:

### What we DON'T need to build

| TDD section | Originally planned | Reality |
|---|---|---|
| § 3.1 IV inversion | "Build Brent's-method BSM inverter" | **Reuse** `app/volatility/solver.py`. Done. |
| § 3.7 Skew metrics | "Build RR-25, BF-25 functions" | **Reuse** parts of `app/volatility/analytics.py`. Audit which exist; only build what's missing. |
| § 4 Surface construction | "Build surface assembly" | **Reuse** `app/volatility/surface.py`. Done. |
| § 5.1 Module layout — `services/options_iv/` | New package with snapshot fetcher, IV inverter, parity dividend, rate curve | **Mostly delete this.** The fetcher exists in `polygon_client`. The inverter exists in `volatility/solver`. Only `parity_dividend.py` and `rate_curve.py` are net new. |
| Polygon fetcher with throttling | "Build" | **Already done.** `polygon_client.py` has it. |
| Daily IV snapshot persistence | "Build new table" | **Already exists.** `OptionsIvSnapshot` table populated daily by ResearchService. **Extend with multi-tenor columns.** |

### What we DO need to build

| Component | Status | Effort |
|---|---|---|
| Forward-implied dividend yield (parity) | Net new — currently FRED rate is fetched but dividend yield is hardcoded or vendor | 2 days |
| Rate curve interpolation across tenors | Net new — `fred_service.py` has rate fetch but not multi-tenor interpolation | 1 day |
| Garman-Klass realized vol | Net new — close-to-close exists but not GK | 2 days |
| ATM IV at multiple tenors (extending current 30d only) | Schema extension + builder logic | 2 days |
| IV-rank and IV-percentile (252d trailing) | Net new | 2 days |
| `SurfaceMetricsDaily` table + Python persistence layer | Net new (separate from `OptionsIvSnapshot`) | 3 days |
| Unified Polygon read-through cache abstraction | Net new — see Q4 | 4-5 days |
| Background staleness mechanism (ARQ recommended) | Net new | 3 days |
| Regime classifier (rule-based) | Net new | 2 days |
| FastAPI endpoints for regime + history | Mostly thin glue | 1-2 days |
| GraphQL schema additions | Thin passthrough resolvers | 1 day |
| Angular dashboard components | Net new | 5-7 days |

**Revised estimate:**
- Path 2 MVP: **2.5-3.5 weeks** (down from 2-3 in the original TDD only because audit revealed work was already done; but caching layer adds back a week)
- Path 1 (with backfill): **+1.5 weeks** (we need to backfill the new wider tenor columns from existing options OHLC)
- Path 3 (no UI): **subtract 1.5 weeks**

### Cleanup work that should happen alongside (or before)

These aren't blockers but they're prerequisites for a clean build:

1. **Resolve the duplicate IV solver.** `app/volatility/solver.py` and `app/research/options/bs_solver.py` both exist. Pick one as canonical, deprecate the other or document why both exist. Without this, future "which solver did you use?" questions will haunt every test failure.

2. **Document the IV pipeline that already exists.** The `app/research/options/iv_builder.py` + `OptionsIvSnapshot` flow is functional but I haven't seen it documented in `docs/architecture/`. Adding a one-page document explaining the existing pipeline before extending it is cheap insurance.

3. **Decide on disk cache vs DB persistence boundary.** `volatility/cache.py` writes parquet/json to disk for fitted surfaces. The new TDD writes derived metrics to Postgres. There needs to be an architectural rule: which lives where? Suggest: disk cache for "expensive-to-recompute fitted surfaces"; DB for "longitudinal time series we need to query for percentiles." Document this in CLAUDE.md.

---

## Things I want to check with you before rewriting the TDD

1. **The `iv_builder` pipeline.** It computes 30d ATM/Put/Call IV daily. **Is it actually run on a schedule, or only on-demand when a user invokes a research report?** This determines whether we have a daily cron we can hook into for IV-rank accumulation, or whether we need to add scheduled execution.

2. **The `options-history` component.** What does this currently show? If it's already plotting IV-30d over time, we might be closer to the dashboard than I think and parts of the new dashboard reduce to "add panels alongside what exists."

3. **The duplicate solver.** Are you aware of `app/research/options/bs_solver.py` having its own `implied_volatility` function? Was this an intentional fork or accidental duplication?

4. **The 2-second-call-it-out gut-check.** Now that you've seen this audit, does it match your memory of what's there? Or are there other modules I missed (search terms I didn't try) that you'd like me to look at?

---

## Recommendation for next step

**Don't rewrite the TDD yet.** First:

1. You read this audit and confirm/correct it
2. You answer the four questions above
3. **Optionally:** before any more design, do the duplicate-solver cleanup. Removing one of the two `implied_volatility` functions (or formally documenting why both exist) is a 1-day task that prevents architectural confusion in everything we build on top.

Then I rewrite TDD § 4 and § 5 against this audit. The math sections (§ 3) and the dashboard sections (§ 6) and the fixture plan (§ 7) all stand from the original.

The honest version of this project is **smaller and more reuse-heavy than the original plan**, which is good. It means you ship faster, with less new code to maintain, and the math foundations are already battle-tested.
