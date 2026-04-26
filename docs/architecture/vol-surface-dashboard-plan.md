# Vol-Surface & Regime Dashboard — Build plan (regrounded after Edge ship)

**Status:** Plan, awaiting user review. **Do not start without approval.**
**Date:** 2026-04-26 (regrounded)
**Supersedes:** the version of this file at commit `576dc26`. That version
predated the merge of the Edge feature ([PR #26](https://github.com/tim1016/learn-ai/pull/26),
commit `4b03ff0`). Most of what it proposed now exists; this version
re-scopes to the genuine remaining work.
**Depends on:** PR #25 (`cleanup/options-math-sovereignty`) merged ✅, PR #26 (Edge) merged ✅.

This is a build plan, not a TDD. Decisions encoded here are based on the
2026-04-26 state of master plus the Edge design doc
[`edge-feature-design.md`](edge-feature-design.md).

---

## 0. What changed under us

PR #26 shipped the **Edge** feature — a parent route `/edge` with three sub-views
(Realized vs IV, Cross-Asset Validation, Regime Clustering) plus two
cross-cutting capabilities (Trade Simulator, Edge Score). It comprises:

- ~2,200 lines of new Python under `app/engine/edge/` covering RV (4 estimators
  including Garman-Klass and Yang-Zhang), IV30 construction with variance-time
  interpolation per the CBOE VIX whitepaper, 25Δ skew, term-slope, vol-of-vol,
  forward RV oracle, VRP, k-means + Gaussian HMM regime clustering, regime
  drift detection, period-splitting (rolling N-year + calendar + walk-forward),
  cross-asset runner, portfolio aggregator, robustness stats (DSR, PBO),
  trade simulator with options spread model, and a four-component Edge Score.
- 9 endpoints under `/api/edge/*` in [routers/edge.py](../../PythonDataService/app/routers/edge.py).
- 3 Angular routes (`/edge`, `/edge/realized-vs-iv`, `/edge/regimes`) plus
  edge-api and edge-mock-data services and 8 charts.
- Strict `features_realtime/` vs `labels_oracle/` directory split with a
  CI grep guard against forward-shift leakage.

The original plan's "build a Vol-Surface Dashboard from scratch" framing is
**superseded**. Most of what it proposed now exists, often more rigorously than
proposed.

---

## 1. Map of coverage: what Edge delivers vs what the original plan proposed

| Original plan item | Status in master today | Notes |
|---|---|---|
| Garman-Klass realized vol | ✅ Covered (`features_realtime/realized_vol.py:garman_klass`). Yang-Zhang also shipped, recommended over GK for daily VRP per Edge § 4.3. | YZ is a strict generalization; GK is fine where caller picks it explicitly. |
| ATM IV at standard tenors | 🟡 Partial. `iv30_constructor.iv30_atm_50d` builds **only the 30d target** via variance-time interpolation. Other tenors (7d/14d/60d/90d) require a one-line change to retarget the same function. | Math primitive `variance_interpolated_iv` is generic. |
| 25Δ skew (RR) | ✅ Covered (`iv30_constructor.skew_25d`). | Edge uses RR-25 = puts − calls, which is the equity-index convention. |
| 25Δ butterfly | ❌ Not in Edge. One-line addition next to `skew_25d` if you still want it. | Lower priority — RR already captures the directional skew signal. |
| Term-structure slope | ✅ Covered (`iv30_constructor.term_slope`, σ(60d) − σ(30d)). | |
| Term-structure curvature | ❌ Not in Edge. Trivial addition. | |
| IV-RV spread | ✅ Covered, **as variance-form VRP** (`vrp.compute_vrp` = IV² − RV², not IV − RV). VRP signal with z-score thresholds in `vrp.vrp_signal`. | Variance form is the academically correct definition (Bollerslev-Tauchen-Zhou). The original plan's vol-form spread is a coarser proxy; drop in favor of VRP. |
| IV-rank (252d window) | ❌ Not directly. Edge uses VRP z-score for the same role. | If you still want the simple percentile-of-IV30-over-252d framing, `edge_score.s_iv_percentile` already implements it as one of the four Edge Score components. The "rank" variant (linear scaling between 252d min/max) would be net new but maps to ~5 lines. |
| IV-percentile (252d window) | ✅ Covered (`edge_score.s_iv_percentile`). | |
| Regime classifier | ✅ Covered, but **clustering-based, not rule-based**. K-means + Gaussian HMM with stability filter and Hungarian-aligned drift refits. Returns numeric cluster IDs (0..K-1), not semantic "HIGH/NORMAL/LOW" labels. | The original plan's rule-based HIGH/NORMAL/LOW is a different framing — simpler, less data-driven. See Open Question Q1 below: do you want both, or replace the rule-based proposal with the Edge clustering view? |
| FastAPI endpoints (~4 planned) | ✅ Mostly covered. 9 endpoints under `/api/edge/*`. **Missing:** any endpoint that fetches a chain → inverts IV → persists. Edge router accepts `iv_series` inline as a v1 placeholder. | The persistence pipeline is the genuine remaining backend work (see § 3 below). |
| GraphQL passthrough + .NET resolvers | ❌ Not built — and **explicitly skipped** by Edge decision #10 ("Frontend → Python `/api/edge/*` directly; skip .NET v1"). | This is a deliberate divergence from the repo's normal pattern. Open Question Q2. |
| Angular dashboard | ✅ Covered as `/edge` route + 3 sub-routes + 8 charts. | The "current regime + recommended structure" summary card the original plan envisioned is **not** part of the Edge UX — Edge's parent page is a navigation card layout, not a regime status card. See Q3. |
| `OptionsIvSnapshot` extension to multi-tenor | ❌ Not done. Edge design § 4.4 step 1 specifies "read stored option mid-quotes from Postgres `OptionIvSnapshots`," but this read path isn't wired in the v1 router. | This is the keystone remaining work. See § 3. |
| Forward-implied dividend yield (parity) | ❌ Not in Edge. The pre-existing `compute_put_call_parity_forward` in `volatility/analytics.py` is still the only parity-related code; no q-extractor wraps it. | Net new if you want IV inputs to come from option-chain back-solving with rigorous q. |
| FRED rate curve multi-tenor interpolation | ❌ Not in Edge. `fred_service.get_risk_free_rate(dte_days, observation_date)` returns a single rate; no curve. | Net new if you want IV inputs at multiple tenors with matched rates per tenor. |
| Manual on-demand snapshot trigger | ❌ Not built. Edge router accepts inline payload only — there is no "click button → fetch chain → invert → persist" flow. | This was the user's preferred operating model. § 3 covers it. |

**Summary:** of the original plan's ~13 work items, **8 are fully covered, 3
are partial, and 2 are net-new but small.** The genuine remaining work is the
**data pipeline that feeds Edge from live Polygon + Postgres** — neither end
of which exists today.

---

## 2. The actual remaining work, in one sentence

**Build the chain → invert → persist → read pipeline that turns the inline
`iv_series` payload Edge currently consumes from the frontend into a real
DB-backed time series populated by manually-triggered snapshots and
historical backfills.**

That's it. The math, the regime clustering, the VRP signal, the trade
simulator, the Angular surface — all built. The plumbing that makes them
operate on persistent data instead of mock payloads is what's left.

---

## 3. Build phases (regrounded)

Three phases. Each is independently shippable. No phase requires the
previous one to deploy — they layer on Edge incrementally.

### Phase 1 — Manual snapshot pipeline (3-4 days)

**Goal:** one date-parameterized FastAPI endpoint that fetches a Polygon
options chain for a given (ticker, date), inverts IV per contract via the
canonical solver, and persists rows. Same code path serves "today" and
historical backfill.

**New code:**

```
PythonDataService/app/
├── volatility/
│   ├── snapshot_pipeline.py       NEW. fetch chain → quality-filter → invert IV → persist
│   ├── parity_dividend.py         NEW. Wraps existing analytics.compute_put_call_parity_forward
│   │                                   to convert F → q with sanity bounds.
│   ├── rate_curve.py              NEW. Multi-tenor FRED interpolation
│   │                                   (extends existing fred_service).
│   └── persistence.py             NEW. SQLAlchemy / asyncpg writes to options_chain_quotes
│                                       and options_iv_history tables.
└── routers/
    └── volatility.py              EXTEND. Add POST /api/volatility/snapshot with ?date=
```

**Schema:** Open Question Q4 below — extend `OptionsIvSnapshot` with raw
chain-quote rows underneath, or build a fresh `options_chain_quotes` table
that supersedes it. Edge design § 4.4 calls for "raw chain-quote table when
present" alongside the existing `OptionsIvSnapshots`, so the cleanest
answer is the new table.

**Endpoint shape:**

```
POST /api/volatility/snapshot
{
  "ticker": "SPY",
  "date":   "2026-04-26",   # required; same code for today & historical
  "force":  false           # if true, refetch + reinvert even if persisted
}
→ {
  "ticker": "SPY",
  "asof":   1745619600000,
  "rows_inserted": 2847,
  "rows_skipped":  12,
  "compute_time_ms": 4521
}
```

**Steps:**
1. Fetch the chain (`PolygonClientService.list_snapshot_options_chain` for
   today; per-contract daily aggregates for historical dates).
2. Quality-filter (reuse existing logic in `volatility/data_loader.py`).
3. Look up risk-free rate(s) from FRED for the date.
4. Compute parity-implied dividend yield per expiry.
5. Invert IV per row using **the canonical solver** (`volatility/solver.implied_volatility`).
6. Persist quotes + per-contract IV.

**Acceptance:**
- `curl ... -d '{"ticker":"SPY","date":"2026-04-26"}'` populates today's data.
- Same endpoint with `"date":"2025-09-15"` populates that historical date.
- Rerunning with `force=false` is a no-op.
- Trivial bash loop fills history (`for d in $(...); do curl ... -d "{\"date\":\"$d\"}"; done`).
- Golden fixture for IV inversion against `py_vollib` (per `numerical-rigor.md`
  and matching the IV solver fixture promised in Edge design § 9).

### Phase 2 — Bridge Edge from inline payload to stored data (1-2 days)

**Goal:** every Edge endpoint that currently requires `iv_series` in the
request body can instead read it from the DB by `(ticker, date_range)`.

**Work:**
- Add `iv30_from_db(ticker, start_ms, end_ms)` to `engine/edge/iv30_constructor`
  (or a new sibling) that reads stored quotes, computes IV30 ATM 50Δ +
  skew + term-slope per the existing functions, returns the `pd.Series`.
- Update `realized_vs_iv_series`, `regimes/cluster`, `edge-score/series`
  endpoints to accept `{ticker, start_ms, end_ms}` as an alternative to
  inline `iv_series` and `bars`.
- Edge frontend defaults to the DB path; the inline path stays for tests
  and the `/edge` mock-data demo.

**Acceptance:**
- After Phase 1 has populated 252+ days of SPY data, hitting `/api/edge/realized-vs-iv/series`
  with `{ticker:"SPY", start_ms, end_ms}` (no `bars` or `iv_series`) returns the
  same VRP-forward and z-score series the inline path returns when fed the
  same data.
- Edge `/edge/realized-vs-iv` page on the frontend can switch a "live data"
  toggle and render real numbers instead of mocks.

### Phase 3 — Regime status surface (2-3 days, optional)

**Goal:** the "current regime + recommended structures" summary card the
original plan envisioned, sitting on the parent `/edge` page above the
navigation cards.

**Work:**
- Either:
  - **3a:** a small rule-based `regime_label.py` that maps the latest
    cluster ID (from `regimes/cluster`) plus the latest VRP z-score and
    IV-percentile into a HIGH/NORMAL/LOW × LONG-VOL/SHORT-VOL/FLAT label
    with a short structure-recommendation list. ~150 LOC. Pure derived
    view; no new math.
  - **3b:** skip Phase 3 entirely and let the user navigate into
    `/edge/regimes` for cluster context and `/edge/realized-vs-iv` for the
    VRP signal. The Edge Score component already produces a single
    -1/0/+1 action that arguably *is* the recommendation.

The original plan strongly assumed a top-line summary card. Edge's
nav-card design implicitly rejects that framing in favor of click-into-detail.
**Open Question Q3** is whether 3a is still wanted.

---

## 4. Reuse matrix (what we lean on, by phase)

| Phase | Reused | New |
|---|---|---|
| 1 | `polygon_client`, `volatility/solver` (canonical IV inverter), `volatility/data_loader`, `fred_service`, `volatility/analytics.compute_put_call_parity_forward` | `snapshot_pipeline.py`, `parity_dividend.py`, `rate_curve.py`, `persistence.py`, schema migration, one router endpoint |
| 2 | All of Edge `engine/edge/`, the new persistence layer from Phase 1 | A `iv30_from_db(...)` reader function + minor router signature widening |
| 3a | All of the above | One `regime_label.py` module + one Angular component on the `/edge` parent page |

**Net new line count estimate:** ~600-900 LOC across Phases 1+2,
plus ~250 LOC if Phase 3a is built. Compare to the original plan's
~3,500 LOC estimate. The savings are entirely from Edge having
already built the math.

---

## 5. What the original plan specified that we are now explicitly skipping

| Item | Why we're skipping |
|---|---|
| New `compute_greeks(...)` dispatcher | Already deferred; not made more urgent by Edge. |
| Multi-tenor ATM IV beyond 30d | Single line to add when needed; no current Edge consumer wants it. |
| 25Δ butterfly | Single line to add; no current Edge consumer wants it. |
| Rule-based HIGH/NORMAL/LOW regime classifier as a backend service | Edge clustering supersedes the data-driven need. The summary-card framing (3a above) is the only place this would surface. |
| GraphQL + .NET resolvers for vol metrics | Edge decision #10 explicitly bypasses .NET for v1. The .NET passthrough can be added later without rework if Edge consumers outside the existing `/edge` Angular route appear. |
| `surface_metrics_daily` separate table | Replaced by `options_chain_quotes` + on-the-fly Edge math. The pre-aggregated daily metrics table was a perf optimization for the dashboard query path; Edge does it in-process per request, which is fine at SPY+QQQ scale. |
| Background scheduler / cron / hosted service | Same as before — user is on manual on-demand refresh. |

---

## 6. Open questions

These need answers before any code in Phase 1.

**Q1 — Regime framing.** Edge ships unsupervised clustering (k-means + HMM,
returning numeric cluster IDs). The original plan envisioned a rule-based
HIGH/NORMAL/LOW classifier with directly-named regime labels. Three options:
- **Q1a:** Drop the rule-based framing entirely. Use Edge clusters; users
  read the centroid characteristics to understand what each cluster means.
- **Q1b:** Build a thin **rule-based labeler on top of the cluster output**
  (Phase 3a) that maps the latest cluster's centroid features to a
  semantic label. Best of both worlds.
- **Q1c:** Build a parallel rule-based classifier that runs alongside Edge's
  clustering and shows both. Most work, most cognitive load on the user.

Recommendation: Q1b, conditional on Q3 = yes.

**Q2 — .NET resolvers.** Edge decision #10 says skip .NET in v1. Do we want
to *also* skip .NET for the snapshot pipeline (Phase 1's endpoint)? The repo
convention is .NET-as-passthrough; Edge breaks it. Two options:
- **Q2a:** Snapshot pipeline endpoint is direct Python, like Edge.
  Lower friction, lower surface area, breaks the convention twice.
- **Q2b:** Snapshot pipeline endpoint goes through .NET passthrough,
  matching the historical pattern. ~½ day extra; adds an .NET resolver
  test surface to maintain.

Recommendation: Q2a — once Edge has set the precedent, doubling down on
direct Python is more honest than maintaining two patterns side by side.
A .NET overlay can be retrofitted later when there's a non-`/edge` consumer.

**Q3 — Regime status summary card.** Phase 3a or skip? Recommendation: skip
in v1; revisit after using Edge's existing UX for a week or two. The
nav-card design might already deliver the insight without a top-line card.

**Q4 — Schema.** Three options for the persistence layer:
- **Q4a:** Extend `OptionsIvSnapshot` with multi-tenor columns (small change,
  but doesn't get us per-contract raw quotes which Edge § 4.4 wants).
- **Q4b:** Add a fresh `options_chain_quotes` table (raw mid-quote rows per
  contract per date) and have Edge compute everything on the fly.
  `OptionsIvSnapshot` stays as the existing 30d-ATM derived table.
- **Q4c:** Both — `options_chain_quotes` for raw inputs, plus extend
  `OptionsIvSnapshot` with multi-tenor columns as a pre-aggregated cache.
  Most flexible, most code.

Recommendation: Q4b. Edge does the derivation in-process; no need to
pre-aggregate a second table. `OptionsIvSnapshot` stays for the existing
research-report consumer.

**Q5 — Universe.** Edge ships SPY+QQQ+IWM+DIA fixed. Original plan had
SPY+QQQ. Phase 1's snapshot endpoint should presumably support all four
on day one to match Edge. Confirm?

**Q6 — Backfill scope.** Same question as last time, now slightly different
because Edge needs ≥252 trading days for full IV-percentile and stable HMM
fits. Run a one-time historical backfill of the four tickers × 2 years on
launch (≈2,000 trading-day-snapshots × ~5s each ≈ ~3 hours wall-clock per
ticker, ~12 hours total)? Or click forward from today?

Recommendation: backfill, given Edge's clustering + drift detection
genuinely benefits from depth.

---

## 7. Phasing summary

| Phase | Work | Days | Ship-ready alone? |
|---|---|---|---|
| 1 | Manual snapshot endpoint + persistence | 3-4 | Yes — chain-quote DB populated, Edge still uses inline payload. |
| 2 | Bridge Edge to read from DB | 1-2 | Yes — Edge runs on real data end-to-end. |
| 3a | Regime status summary card (optional) | 2-3 | Yes — extra UX layer on top. |
| **Total** | | **6-9 days** (with 3a) or **4-6 days** (without) | |

Down from the previous estimate of 12-17 days, entirely because Edge's ship
absorbed Phases B/C/D of the previous version.

---

## 8. Reviewer checklist

Before approving:

- [ ] You agree the original plan's "build a dashboard from scratch" framing
      is superseded by Edge.
- [ ] Q1 (regime framing) — recommendation: Q1b.
- [ ] Q2 (.NET resolvers) — recommendation: Q2a.
- [ ] Q3 (status summary card) — recommendation: skip.
- [ ] Q4 (schema) — recommendation: Q4b (new `options_chain_quotes`).
- [ ] Q5 (universe) — recommendation: SPY+QQQ+IWM+DIA to match Edge.
- [ ] Q6 (backfill) — recommendation: yes, one-time at launch.

If yes, Phase 1 starts. The first deliverable is a draft TDD for the
snapshot pipeline (schema + endpoint + tests + golden fixture), reviewed
before any code lands.
