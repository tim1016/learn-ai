# Vol-Surface & Regime Dashboard — Build plan (post-cleanup)

**Status:** Plan, awaiting user review. **Do not start without approval.**
**Date:** 2026-04-25
**Depends on:** PR #25 (`cleanup/options-math-sovereignty`) merged.
**Supersedes:** the original draft TDD `vol-surface-dashboard-tdd.md` § 4 and § 5
(math sections § 3 and dashboard § 6 from that draft remain valid input).

This is a build plan, not a TDD. It encodes the decisions made during the audit
and the user's answers on 2026-04-25. The TDD itself gets rewritten only after
the open questions at the bottom of this document are resolved.

---

## 0. What this plan covers, what it doesn't

**In scope (what we'll build):**
- The single canonical Polygon snapshot endpoint with a `?date=` parameter — same code path covers "today's snapshot" and "any historical date."
- `OptionsIvSnapshot` extension to multi-tenor (or table replacement — see Q1 below).
- Garman-Klass realized vol (the only net-new math function in the dashboard pipeline).
- ATM IV at standard tenors, IV-rank, IV-percentile, IV-RV spread.
- Regime classifier (rule-based).
- FastAPI endpoints + GraphQL passthrough + Angular dashboard components.

**Out of scope (deferred):**
- Scheduler / cron / hosted service. User is on manual on-demand refresh; no automation.
- Options-math `compute_greeks(...)` dispatcher. Documented in `options-math-authorities.md`; built when a third call site needs it.
- Backfill runner script. The parameterizable snapshot endpoint *is* the backfill mechanism — a thin shell loop calls it for each historical date. Documented but not vendored as Python code in v1.

---

## 1. Sequence

The build has four phases. Each phase is independently mergeable and shippable.
The user can stop at the end of any phase and have a useful product.

```
Phase A — Persistence skeleton (3-4 days)
  Goal: one date-parameterized FastAPI endpoint that fetches a Polygon
  snapshot, inverts IV via the canonical solver, and persists rows.
  At end of phase: clicking a button (or hitting curl) populates one
  day's IV data into Postgres.

Phase B — Derived metrics (3-4 days)
  Goal: a second endpoint reads stored IV, computes ATM-IV-at-tenors,
  skew, term structure, IV-RV spread, IV-rank/percentile (with status
  flags for thin history), regime classification. Persists to a new
  table. At end of phase: SQL queries against surface_metrics_daily
  return all the dashboard's numbers.

Phase C — Backend transport (1-2 days)
  Goal: GraphQL schema additions, .NET passthrough resolvers, contract
  pinning. At end of phase: Apollo Angular client can fetch the
  metrics. No UI yet.

Phase D — Angular dashboard (5-7 days)
  Goal: VolSurfacePage component with regime card, ATM-IV term-structure
  chart, IV-rank gauge, IV-RV sparkline, regime-history timeline. At end
  of phase: end-to-end manual flow — open the page, select SPY, see
  metrics; click refresh; see updated metrics; click historical date,
  trigger backfill for that date, see the row.
```

Total: **12-17 working days**, dropped from the original ~3 weeks because the
options-math foundations are now consolidated and the closed-form Greeks /
canonical solver are reused without re-implementation.

---

## 2. Phase A — Persistence skeleton

### 2.1 Schema decisions to make first

Before writing any code, decide between two paths in **Open question Q1** below:

- **Q1a:** Extend `OptionsIvSnapshot` with multi-tenor columns (smallest churn).
- **Q1b:** Build a fresh `surface_metrics_daily` table that supersedes `OptionsIvSnapshot` and migrate the existing rows into it.

The recommendation is Q1b for cleanliness, but Q1a is faster.

### 2.2 New files

```
PythonDataService/app/
├── volatility/
│   ├── snapshot_pipeline.py         ← NEW. End-to-end "fetch chain → invert IV → persist"
│   │                                       Reuses polygon_client + canonical solver.
│   ├── parity_dividend.py           ← NEW. Wraps existing compute_put_call_parity_forward
│   │                                       to convert F → q (one-line transform + bounds check).
│   ├── rate_curve.py                ← NEW. Multi-tenor FRED interpolation (extend fred_service).
│   └── persistence.py               ← NEW. SQLAlchemy / asyncpg writes to options_daily and
│                                          surface_metrics_daily tables.
└── routers/
    └── volatility.py                ← EXTEND. Add POST /api/volatility/snapshot with ?date=

Backend/Models/MarketData/
└── SurfaceMetricsDaily.cs           ← NEW (or extend OptionsIvSnapshot — see Q1).
```

### 2.3 Shape of the snapshot endpoint

```
POST /api/volatility/snapshot
{
  "ticker": "SPY",
  "date": "2026-04-25",          # Required. Same code path serves "today" and historical.
  "force": false                 # If true, refetch + recompute even if already persisted.
}

→ {
  "ticker": "SPY",
  "asof": 1745619600000,
  "rows_inserted": 2847,
  "rows_skipped": 12,
  "compute_time_ms": 4521,
  "diagnostics": { ... }
}
```

The endpoint:
1. Fetches the chain via `polygon_client` (snapshot for today, historical OHLC bars for past dates).
2. Filters quality (existing logic in `volatility/data_loader.py`).
3. Looks up risk-free rate from FRED for the date.
4. Computes parity-implied dividend yield per expiry.
5. Inverts IV per row using **the canonical solver** (`app/volatility/solver.implied_volatility`).
6. Persists to `options_daily` (and `options_contracts` for any new contracts).

### 2.4 Acceptance for phase A

- `curl -X POST .../snapshot -d '{"ticker":"SPY","date":"today"}'` populates today's data.
- Same endpoint with `"date":"2025-09-15"` populates that historical date.
- A trivial bash loop over a date range fills history. (`for d in $(... date list); do curl ... -d "{\"date\":\"$d\"}"; done`)
- Rerunning with `force=false` is a no-op for already-populated dates.
- Tests: golden fixture for IV-inversion against py_vollib parity (per `numerical-rigor.md`).

---

## 3. Phase B — Derived metrics

### 3.1 New files

```
PythonDataService/app/volatility/
├── realized_vol.py                  ← NEW. Garman-Klass: per-day variance + rolling annualized.
│                                          Golden fixture against Sinclair's canonical formula.
├── atm_extractor.py                 ← NEW. Forward-ATM IV at standard tenors with piecewise
│                                          linear interpolation; null if no expiry within ±15%.
├── percentiles.py                   ← NEW. trailing_iv_rank + trailing_iv_percentile
│                                          with status flags (full / partial / insufficient).
└── regime.py                        ← NEW. Rule-based classifier. Returns RegimeClassification
                                            dataclass. Hand-built test cases.
```

### 3.2 Why Garman-Klass

The audit showed only close-to-close realized vol exists today
(`research/features/ta_features.py:compute_realized_vol_30`). For the IV-RV
spread metric we need a low-variance estimator at standard windows. GK is
the canonical choice and well-cited. Inputs: daily OHLC (already available
from the existing aggregates table).

### 3.3 IV rank and percentile mechanics

Both functions take a series of historical 30d ATM IV (read from
`surface_metrics_daily`) and return a `(value, status)` tuple. Status is:
- `'insufficient'` — < 60 trading days of history → return None.
- `'partial'` — 60-251 days → compute against available window, flag in UI.
- `'full'` — ≥ 252 days → standard 252-day rank / percentile.

The function is pure (no DB call from inside the math). The caller fetches
the trailing window and passes it in.

### 3.4 Regime classifier

Implements the rules from the original draft TDD § 3.10. Pure-Python rule
table, returns a `RegimeClassification` dataclass with vol/term/skew labels
and a recommended-structure list. Tested with hand-constructed regime tuples
producing the expected classification — no statistical inputs in the test.

### 3.5 New endpoint

```
POST /api/volatility/metrics/refresh
{
  "ticker": "SPY",
  "date": "2026-04-25"        # Optional. Default = today.
}

Behavior: Reads options_daily for the date + trailing window from
surface_metrics_daily. Computes all metrics. Upserts the row.
```

### 3.6 Acceptance for phase B

- After running `/snapshot` and `/metrics/refresh` for today, `surface_metrics_daily`
  has one fully-populated row.
- Running for ~252 historical days produces full IV-rank / percentile values.
- Running for < 60 days produces NULL rank with `iv_rank_status = 'insufficient'`.
- Tests: golden fixture for GK realized vol; property test for rank ∈ [0,1] when status=full;
  hand-constructed regime cases.

---

## 4. Phase C — Backend transport

### 4.1 GraphQL schema additions

(Identical to the original TDD § 5.4. No changes from earlier draft.)

```graphql
extend type Query {
  surfaceRegime(ticker: String!): SurfaceRegime!
  surfaceMetricsHistory(ticker: String!, from: DateTime!, to: DateTime!): [SurfaceMetricsPoint!]!
}
extend type Mutation {
  refreshSurface(ticker: String!, date: DateTime): SurfaceRegimeResult!
}
```

### 4.2 .NET resolver pattern

Resolvers in `Backend/GraphQL/SurfaceQuery.cs` and `SurfaceMutation.cs`. They are
pure passthroughs to the FastAPI endpoints — `IHttpClientFactory`-based typed
client, structured logging with `[STEP X]` prefix, `JsonNamingPolicy.SnakeCaseLower`
for the snake-case Python responses. No math in C#, per `CLAUDE.md` § 5.

### 4.3 Acceptance for phase C

- `gql query { surfaceRegime(ticker: "SPY") { ... } }` returns the persisted metrics + regime.
- `gql mutation { refreshSurface(ticker: "SPY") { ... } }` triggers the pipeline and returns the result.
- `gql mutation { refreshSurface(ticker: "SPY", date: "2025-09-15") { ... } }` triggers a historical refresh.
- Backend.Tests covers the resolver wiring (Hot Chocolate `IRequestExecutor`).

---

## 5. Phase D — Angular dashboard

### 5.1 New component tree

```
Frontend/src/app/components/vol-surface-dashboard/
├── vol-surface-dashboard.component.ts    Standalone, OnPush, signals
├── vol-surface-dashboard.component.html
├── vol-surface-dashboard.component.scss
├── vol-surface-dashboard.component.spec.ts
└── widgets/
    ├── ticker-selector/                 SPY | QQQ tabs
    ├── regime-summary-card/             Regime label + recommended structures
    ├── atm-iv-term-structure/           Line chart (lightweight-charts)
    ├── skew-visualization/              Smile curve at 30d expiry
    ├── iv-rank-gauge/                   With "insufficient/partial/full" badge
    ├── iv-rv-spread-sparkline/          Time-series with current value highlight
    └── data-quality-footer/             Asof timestamp + refresh button
```

### 5.2 Apollo Angular wiring

- One query at page load: `surfaceRegime(ticker)`. Returns everything for the cards.
- Refresh button → `refreshSurface(ticker)` mutation. Disables for 30s.
- Sparkline data lazy-fetched via `surfaceMetricsHistory(...)` when card expanded.
- A "load this date" date-picker at the bottom triggers `refreshSurface(ticker, date)` for any historical date — this is the manual-backfill UX.

### 5.3 Path 2 considerations on the gauge

The IV-rank gauge needs to handle three states cleanly:
- `'insufficient'` → "Accumulating history — N days of 60 needed" placeholder.
- `'partial'` → show the rank with a small "computed against N days" badge + tooltip.
- `'full'` → standard display.

This is the only Angular component with non-trivial state-aware rendering. The rest are straightforward.

### 5.4 Acceptance for phase D

- AXE clean. WCAG AA. No `console.log`. Standalone components, OnPush, signals.
- Click "Refresh" on the dashboard, see metrics update.
- Click a historical date, confirm the row populates and the sparkline updates.
- Vitest specs for the three state-flag branches of the IV-rank gauge.

---

## 6. What we explicitly do NOT need (audit-corrected)

| Originally planned | Status |
|---|---|
| Build Brent's-method BSM inverter | **Reuse `app/volatility/solver.implied_volatility`** (consolidated in PR #25). |
| Build skew metric functions (RR, BF) | **Reuse `app/volatility/analytics.compute_skew_metrics`**. Add term-structure as an extension only. |
| Build surface assembly | **Reuse `app/volatility/surface.py`**. |
| Polygon fetcher with throttling | **Reuse `polygon_client.py`** (throttle is built in). |
| New IV snapshot persistence table from scratch | **Extend or supersede `OptionsIvSnapshot`** (see Q1). |
| Forward-implied dividend yield function | **Wrap existing `compute_put_call_parity_forward` in `analytics.py`** with a one-line F→q transform. |
| Background scheduler / cron / ARQ | **Skip entirely.** User is on manual on-demand refresh; the date-parameterized endpoint covers backfill via shell loop. |
| `compute_greeks` dispatcher | **Skip from this build.** Documented; add when a 3rd caller benefits. |

---

## 7. Reuse matrix (what we lean on, by phase)

| Phase | Reused | New |
|---|---|---|
| A | `polygon_client`, `volatility/solver`, `volatility/data_loader`, `fred_service`, `volatility/analytics.compute_put_call_parity_forward` | `snapshot_pipeline.py`, `parity_dividend.py`, `rate_curve.py`, `persistence.py`, schema migration |
| B | `surface_metrics_daily` table from A, options OHLC table | `realized_vol.py` (GK), `atm_extractor.py`, `percentiles.py`, `regime.py` |
| C | Existing GraphQL pipeline pattern, `IHttpClientFactory` clients | Schema types, two resolvers |
| D | PrimeNG, lightweight-charts, existing routing layout | Component tree above, Apollo queries |

---

## 8. Open questions blocking the build

These need answers before any TDD rewrite or any code in Phase A:

1. **Q1: `OptionsIvSnapshot` extend vs replace.** Extend it with multi-tenor columns (Q1a), or replace it with a wider `surface_metrics_daily` table and migrate existing rows (Q1b)? Q1b is cleaner but requires a one-time migration; Q1a is one-day cheaper but leaves a half-overlapping table around. Recommendation: Q1b.
2. **Q2: Forward ATM vs spot ATM.** TDD § 3.4 specifies forward-ATM for rigor. Most retail tooling uses spot-ATM ("50-delta convention"). Recommendation: forward, with the spot-ATM convention exposed only as a UI tooltip note.
3. **Q3: Regime threshold tuning.** Current rules use hardcoded thresholds (0.70 for HIGH IV-rank, ±1.0 vol point for term slope). Ship with these defaults in v1 and tune in v1.1 once you have history? Or refuse to ship until tuned against your own ticker history?
4. **Q4: Path commit.** Original plan distinguished "Path 1 backfill from day 1" vs "Path 2 forward-only accumulation." With the date-parameterized endpoint, both collapse to one build. Decision: do you actually want to run the historical loop once at launch (≈2 years of trading days × ~5s per snapshot ≈ 1-2 hours wall-clock), or do you genuinely prefer to start with no history and accumulate forward?
5. **Q5: Backtesting integration.** Should the regime classifier feed into `app/engine/` strategy logic (e.g., as a filter on the EMA crossover engine — only trade when `vol_regime != 'HIGH'`)? Out of scope for this dashboard build, but the answer affects whether the metrics need to expose a stable Python API in addition to the GraphQL one.

Once these are answered, the original `vol-surface-dashboard-tdd.md` § 4 and § 5
get rewritten to match this plan, the math sections § 3 and dashboard § 6 carry
forward unchanged, and Phase A starts.

---

## 9. Reviewer checklist

Before approving this plan, confirm:

- [ ] PR #25 (`cleanup/options-math-sovereignty`) is merged. The plan assumes the canonical solver and consolidated bs_greeks module are on master.
- [ ] You're comfortable with the four-phase decomposition (each phase is independently shippable).
- [ ] You've answered Q1-Q5 above (or accepted the recommendation for each).
- [ ] You agree the build is 12-17 working days end-to-end on Path-2-with-on-demand-backfill (no scheduler).

If yes, the next deliverable is a rewritten `docs/architecture/vol-surface-dashboard.md`
TDD reflecting these decisions, and Phase A begins.
