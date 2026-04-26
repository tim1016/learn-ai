# Claude Design — Edge feature UI/UX handoff (2026-04-25)

This prompt covers the **Edge** route (`/edge`) — three quantitative-research views (Realized vs IV, Cross-Asset Validation, Regime Clustering) bound by two cross-cutting capabilities (Trade Simulator, Edge Score). Read **"What's already done"** first — the functional shell, math engine, FastAPI endpoints, and route registration are all in place. The polish, charts, and interaction details below are what's left.

The canonical engineering spec is **`docs/architecture/edge-feature-design.md`**. A parallel pedagogical narrative used as a validation reference is **`docs/architecture/edge-design-temporary-docs.md`**. When in doubt, the engineering spec wins.

---

## Project context

- Angular 21, zoneless, standalone, `ChangeDetectionStrategy.OnPush`, Signals, SCSS only.
- Primary working directory: `Frontend/`.
- Design tokens: `src/app/styles/_tokens.scss` — already TV-dark; we use those tokens, **not** new hex literals.
- No Tailwind in templates. PrimeNG + PrimeIcons 7 for components when needed. `.claude/rules/angular.md` is binding.
- Charts on this repo today: TradingView lightweight-charts v5 (candlesticks). Other surfaces use whatever fits — uPlot, plotly-canvas. Avoid SVG above ~10k points.
- All wire/storage timestamps are `int64 ms UTC`. UI converts to `America/New_York` for display only (per `numerical-rigor.md` § Timestamp rigor).

## What's already done (this session)

### 1. Three-view route scaffolded with parent navigation

- **New** `Frontend/src/app/components/edge/`
  - `edge.component.{ts,html,scss}` — parent route. Renders three nav cards when at `/edge`, otherwise hosts `<router-outlet />` for child routes.
  - `realized-vs-iv/` — child route at `/edge/realized-vs-iv`. Form controls (symbol, bar size, tenor, estimator) + placeholder describing the wired Python endpoint.
  - `cross-asset/` — child route at `/edge/cross-asset`. Strategy/split-mode controls + universe chip strip (SPY · QQQ · IWM · DIA fixed).
  - `regimes/` — child route at `/edge/regimes`. Symbol/states/algorithm/Viterbi-vs-Posterior controls + placeholder.
- **Routes registered** in `Frontend/src/app/app.routes.ts` as a parent `/edge` with three lazy children.
- All four components are standalone, OnPush, signal-driven, dark-themed via `@use "../../styles/tokens" as *;` — no inline hex.
- `EdgeComponent.isRoot` is a derived signal off `Router.events` (NavigationEnd-filtered) so the parent shows nav cards at `/edge` and gets out of the way at `/edge/<child>`.

### 2. Python math engine and FastAPI router fully wired

- **New** `PythonDataService/app/engine/edge/`
  - `spread_model.py` — Madhavan-Smidt option spread (vol × √T × moneyness penalty), stock spread (bps), `is_tradable()` liquidity floor.
  - `trade_simulator.py` — pessimistic-first execution (T+1 entry, time-stop / opposite-signal / hard-stop / target exits, slippage + commissions + spread, `tradable` flag, `cost_attribution` breakdown).
  - `features_realtime/realized_vol.py` — close-to-close, Parkinson, Garman-Klass, Yang-Zhang. Yang-Zhang uses the explicit `k = 0.34/(1.34 + (n+1)/(n-1))` constant.
  - `features_realtime/regime_features.py` — OHLCV-only `build_ohlcv_features()` (trend slope, RV-YZ, ATR%, volume z, all rolling-z-scored on 60-bar lookback) **and** the IV-extended `build_full_features()` accepting iv30 / skew / term-slope to add ΔIV, IV-vol, skew_z, term_slope_z, iv30_z columns.
  - `features_realtime/iv30_constructor.py` — variance-time interpolation per CBOE VIX whitepaper, 25Δ skew, term slope, ΔIV, IV-vol.
  - `features_realtime/delta_inversion.py` — closed-form K-from-Δ at fixed σ, plus fixed-point iteration with bisection fallback for the smile case.
  - `labels_oracle/forward_rv.py` — forward-shifted RV in a physically separate directory, never imported by features.
  - `regime_clustering.py` — hand-rolled K-means (Lloyd 1982 + k-means++ init) and Gaussian HMM (Baum-Welch EM with log-space forward-backward, Viterbi decoding, posterior). No new dependencies (sklearn / hmmlearn deliberately avoided per "Sovereign over the math").
  - `regime_drift.py` — Hungarian-algorithm label alignment (`scipy.optimize.linear_sum_assignment`), symmetric-KL transition-matrix divergence, composite stability score.
  - `regime_strategy_eval.py` — partition trade ledger by regime, per-regime stats; regime run-length encoding helper.
  - `period_splitter.py` — rolling N-year, calendar-year buckets, anchored walk-forward (train/test pairs).
  - `portfolio_aggregator.py` — equal-weight + vol-weighted (inverse-vol parity, monthly-rebal, look-ahead-safe via `.shift(1)` on the weights matrix).
  - `robustness_stats.py` — Deflated Sharpe Ratio (López de Prado 2014), Probability of Backtest Overfitting via CSCV (sampled at 200 combos when `C(s, s/2)` explodes).
  - `cross_asset_runner.py` — async parallel runner over (asset × period) cells; placeholder `placeholder_buy_and_hold` strategy registered (real strategies wire in alongside the three TV strategies in `three_strategies_roadmap.md`).
  - `edge_score.py` — composite `S^vrp · w1 + S^regime · w2 + S^iv · w3 + S^trend · w4`; tanh squashing on each component; default fixed weights `[0.4, 0.3, 0.2, 0.1]` enforced as anti-overfit rail.

- **New** `PythonDataService/app/routers/edge.py` registered in `app/main.py`. Endpoints:
  - `POST /api/edge/realized-vs-iv/series` — RV per estimator/window, IV30, VRP forward, vrp_z, coverage diagnostics.
  - `POST /api/edge/realized-vs-iv/signals` — oracle vs realtime signal series.
  - `GET  /api/edge/realized-vs-iv/coverage/{symbol}` — coverage probe (v1 placeholder; v2 wires to `OptionIvSnapshots`).
  - `POST /api/edge/cross-asset/run` + `GET /api/edge/cross-asset/strategies`.
  - `POST /api/edge/regimes/cluster` + `POST /api/edge/regimes/strategy-fit`.
  - `POST /api/edge/trade-sim/run`.
  - `POST /api/edge/edge-score/series`.
- v1 endpoints accept inline `bars` payloads so the frontend can drive the math without a DB-fetch round trip. Real Polygon-backed bar fetching is a v2 wire-up.

### 3. Tests

- `tests/edge/test_spread_model.py` — Madhavan-Smidt spread parity (ATM, wing penalty, √T scaling, floor).
- `tests/edge/test_trade_simulator.py` — entry/exit semantics, cost-reduces-PnL, attribution sums.
- `tests/edge/test_realized_vol.py` — zero-on-flat, GBM sigma recovery, YZ k-constant.
- `tests/edge/test_regime_clustering.py` — KMeans cluster recovery (3-cluster Gaussian mixture), HMM diagonal-dominant transition for sticky data, stability filter behavior.
- `tests/edge/test_regime_drift.py` — Hungarian permutation recovery, transition-matrix permutation, KL self-zero.
- `tests/edge/test_period_splitter.py` — rolling/calendar/walk-forward count + alignment.
- `tests/edge/test_robustness_stats.py` — DSR direction, PBO ~0.5 on noise.
- `tests/edge/test_iv30_and_vrp.py` — strike-from-delta closed form, variance-time interp midpoint, VRP signal direction.
- `tests/edge/test_edge_score.py` — sign convention per component, action threshold gating.

### 4. Documentation

- `docs/architecture/edge-feature-design.md` — full engineering spec (570 lines). Layered 📖 Layman / 🎯 Professional / 📐 Reference framings, math provenance, endpoint contracts, sequencing.
- `docs/architecture/edge-design-temporary-docs.md` — pedagogical companion treated as validation reference.

## Constraints

- The functional shell uses placeholder `<div>` blocks with controls and a one-paragraph note pointing at the Python endpoint. There is **no chart code yet** — all visualization lands in this design pass.
- Strict TS, no `any`, no template `as` casts, signals only. `npx tsc --noEmit` not yet run on the new components — please run before committing visual work.
- Linters: `ruff check PythonDataService/app/` should pass on edge modules; `npx eslint Frontend/src/` should pass on new components. The `--max-warnings 0` rule applies.
- Dark-theme only. The repo uses `_tokens.scss` (TV-dark palette: `$bg-canvas: #0b0e14`, `$accent: #2962ff`, `$bull: #26a69a`, `$bear: #ef5350`). The temp doc proposed a slightly-different palette (`#0B0E11`, cyber-blue, forest-green, crimson-orange); we **align with the existing tokens** rather than introducing new hex.
- Endpoints accept inline `bars[]`. The frontend needs a thin service that fetches bars from `/api/aggregates` first, then posts them to `/api/edge/*`. v2 will collapse this into server-side joining.

## Visual identity (already enforced via tokens)

| Token | Hex | Edge usage |
|---|---|---|
| `$bg-canvas` | `#0b0e14` | Page background |
| `$bg-surface` | `#131722` | Card / chart wrapper |
| `$bg-elevated` | `#1b1f2e` | Modals, hover-elevated rows |
| `$bg-sunken` | `#070a11` | Inputs, embedded strips |
| `$accent` | `#2962ff` | Neutral / interactive |
| `$bull` | `#26a69a` | +Sharpe, long-vol, in-the-money |
| `$bear` | `#ef5350` | −Sharpe, short-vol, drawdown |
| `$warn` | `#ff9800` | Coverage gaps, "theoretical" trades |
| `--ind-cat-volatility` | `#f2ad3d` | RV-vs-IV nav card accent |
| `--ind-cat-trend` | `#4d8dff` | Cross-Asset nav card accent |
| `--ind-cat-momentum` | `#a78bfa` | Regimes nav card accent |
| `$font-mono` | JetBrains Mono | All numeric data (IV, RV, Sharpe, p-values, ts) |
| `$font-sans` | system | Labels, prose |

The nav cards already use category-soft accents from existing `--ind-cat-*` tokens. Charts should pick up `$bull` / `$bear` for direction-coded series and `$accent` for neutral / model overlays.

## Per-route polish — what's needed

### `/edge/realized-vs-iv`

**Current shell:** four input controls (Symbol, Bar size, Tenor, Estimator) and a placeholder block. The Python endpoint `POST /api/edge/realized-vs-iv/series` returns the full data contract.

**Needed visualizations:**

1. **Dual-axis price + IV chart** at the top — candles or close line on left axis, IV30 line on right axis. Cross-hair tooltip shows `{ts → ny-time, price, iv30, rv_yz_30, vrp_z}`.
2. **RV bands overlay** — selectable estimator (CtC/Parkinson/GK/YZ); show selected estimator at three windows (5/10/30) as thin lines under the IV line. Use semantic intensity rather than four-color rainbow.
3. **VRP histogram** below the chart — distribution of vrp_forward, with current value marked. Percentile band shading.
4. **Signal scatter** on the price chart — green up-arrows for long-vol, red down-arrows for short-vol. Toggle for oracle vs realtime. **Oracle markers must visually differ** (e.g. dashed border, "ex-post" badge in tooltip) so users never confuse them.
5. **Coverage banner** — non-dismissible amber strip at the top when:
   - The trailing N bars have NaN forward RV (the unavoidable tail per §4.2 of the spec — display as greyed terminal band on the chart, plus a banner "Forward RV unavailable for last 21 trading days — VRP signals here are blind to the future.").
   - IV data has gaps (`coverage.iv_first_ts` doesn't span the requested window).
6. **Form layout polish** — inputs should align, labels uppercase-caps, monospace numerics. Already styled in v1 but feels generic.

**Open questions for design:**

- Should the estimator selector be a **chip group** (toggleable, multi-select to overlay multiple) or a **single dropdown** (one at a time)? Multi-select is more analytical but more cluttered.
- The forward-RV grey band at the right edge of the chart — is a vertical hatch better than a solid grey block?
- Where does the **`signal_oracle` vs `signal_realtime`** toggle live? A segmented control above the chart, or a checkbox per layer in a small "Layers" panel?

### `/edge/cross-asset`

**Current shell:** strategy/split-mode dropdowns and a fixed-universe chip strip (SPY · QQQ · IWM · DIA).

**Needed visualizations:**

1. **Heatmap (asset × period × Sharpe)** — primary visualization. Sharpe color-mapped via diverging palette (red → grey → green) anchored at zero. Hover reveals full stats (n_trades, win_rate, max_dd). Cells with Sharpe > +1 get a subtle border highlight; cells with Sharpe < −0.5 get a warning indicator.
2. **Per-asset equity curves as small multiples** — 4 mini line charts in a 2×2 grid, each showing the asset's equity over the requested window. Same y-axis scale across all four.
3. **Composites tab** — switch between "Per-asset", "Equal-weight composite", "Vol-weighted composite". Composite views show the aggregate equity curve and stats.
4. **Robustness scorecard** — three big numbers at the top: `Robustness Score: 0.62`, `DSR (mean): 0.74`, `PBO: 0.31`. Each with a one-line explanation as hover-tooltip.
5. **Drag-and-drop universe builder** — chips on the side that can be added to/removed from the universe. v1 ships with the four fixed; the design should accommodate `n` arbitrary tickers.

**Open questions for design:**

- The heatmap can have tens of cells (4 assets × 10 rolling periods × 3 split modes = 120 cells if we present all). Three sub-tabs for the three split modes? Or a single fused heatmap with split-mode selector?
- Composite equity curves overlaid vs faceted? Overlaid is denser; faceted is easier to compare.
- For PBO and DSR: where's the right tutorial moment? Inline "?" tooltip? Separate "About these metrics" expanding section?

### `/edge/regimes`

**Current shell:** symbol / states / algorithm / Viterbi-vs-Posterior controls.

**Needed visualizations:**

1. **Regime-colored price chart** — candles or line, with each bar tinted by its regime label. Three-state default colors: trending-low-vol (`$bull`-soft), trending-high-vol (`$warn`-soft), choppy-high-vol (`$bear`-soft). The legend names states by their centroid characteristics, not "State 0/1/2".
2. **Viterbi vs Posterior toggle** — Viterbi paints solid color blocks; Posterior renders each state's probability as opacity stacking (one band per state).
3. **Transition matrix heatmap** — small 3×3 grid below the price chart, diagonal-dominant for HMM. Numbers in cells, color-mapped intensity.
4. **Per-regime feature radar** — 4–9 axis radar (depending on whether IV features are available) showing the centroid in feature space for each regime. Helps users *see* what makes each regime distinct.
5. **Strategy-fit P&L bars** — when a backtest has been run elsewhere, this view should accept its trade ledger and partition by regime, showing per-regime Sharpe / win-rate / total P&L bars.
6. **Drift sparkline** — a small `regime_stability_score` line below the matrix, showing model stability over time. Spikes flag structural breaks.
7. **Algorithm comparison view** — when "Both (compare)" is selected, render two sets of regime stripes stacked above the price chart for visual comparison. The user should *see* HMM's stickiness vs k-means' flickering.

**Open questions for design:**

- Three colors are easy. If users ask for 4 or 5 states (ours allows 2–6), how does the palette extend without losing semantic meaning?
- Posterior view as opacity stacks works for 3 states but degrades fast at 4+. Alternative encoding for higher state counts?
- The feature radar has 9 axes when IV features are loaded — that's getting busy. Drop to a parallel-coordinates plot at higher dimensionality?

### Edge Score (cross-cutting)

The Edge Score endpoint exists (`POST /api/edge/edge-score/series`). It is not yet placed in the UI.

**Design call needed:**

- **Option A — Overlay strip** on each F1/F3 chart: a thin row at the bottom of the price chart showing the per-bar score as a colored bar (green = +1 long-vol, red = −1 short-vol).
- **Option B — Standalone `/edge/score` route** with the four component sub-scores rendered as separate lines plus the composite. More room for the audit-trail rationale ("Why is the score +0.4? VRP contributed +0.3, regime contributed +0.1...").
- **Option C — Both:** overlay strip on F1/F3 charts, plus a small "Edge Score Inspector" pop-out drawer when clicked.

I'd lean Option C, but it's a real design call.

### Trade Simulator

The simulator endpoint exists (`POST /api/edge/trade-sim/run`). It is not surfaced in the UI yet.

**Suggested placement:** a "Run trade simulator" button on each of `/edge/realized-vs-iv` (using the VRP signals) and `/edge/cross-asset` (using the per-asset signals). Output rendered as a slide-out drawer with:

- The equity curve
- The cost-attribution breakdown bar chart (gross_pnl − spread_cost − slippage − commissions = net_pnl, with a side-by-side "tradable-only" net)
- The trade ledger as a sortable table

## Cross-cutting interaction principles

- **60 FPS target** on 15-min bar series (~10k points/year). Canvas-backed charts; minimal recompute via OnPush + signals; no SVG above 10k points.
- **Cross-chart scrub sync.** Zooming or panning any chart on a route should sync all sibling charts to the same window via a shared `currentRange = signal<{start_ms, end_ms}>(...)`. One source of truth, no event-bus glue code.
- **Coverage warnings as first-class UI** — never bury a data caveat in a tooltip. The "forward RV unavailable" tail and "IV data starts 2024-06-01" are banners or chart-overlay shading.
- **Tooltips show the full numeric stack** at hover ts: price, IV30, RV (selected estimator), VRP_z, regime label + posterior, edge score, action.
- **AXE compliance, WCAG AA contrast, keyboard navigation** per `angular.md`. Existing dark-token contrast is verified at 20.8:1 primary, 7.0:1 secondary.

## What I'd like a fresh pair of eyes on

- **Information density per route.** Each view has 4–7 distinct chart elements. The data-lab equivalent is much busier; the Edge views could either compress (more efficient screens) or breathe (more digestible). Which way matches the strategy-lab feel?
- **Form-control pattern.** v1 uses bare `<input>` / `<select>` styled by `_tokens.scss`. Would PrimeNG controls (segmented buttons, multi-select chips) raise the perceived quality without breaking the dark aesthetic?
- **Nav cards.** v1 cards have a colored top accent rail + tagline + 3-bullet feature summary + "Open →" footer. They feel slightly bare — should each card carry a small live-preview sparkline (last 30 days of headline metric)?
- **Empty / loading states.** v1 has none. The endpoint contracts include `coverage` so we know when to show "no data for this symbol" — design treatment needed.
- **Mobile / narrow viewport.** The repo trends desktop-first but the strategy-lab is narrow-friendly. Edge has heavy charts; what's the responsive break-point story?
- **The Edge Score Inspector.** If we go with Option C (overlay + drawer), what does the drawer look like? It's the audit-trail surface — needs to feel substantive without being a wall of text.

## Files touched in this session

```
PythonDataService/app/engine/edge/                                   (new package)
PythonDataService/app/engine/edge/spread_model.py                    (new)
PythonDataService/app/engine/edge/trade_simulator.py                 (new)
PythonDataService/app/engine/edge/features_realtime/                 (new)
PythonDataService/app/engine/edge/features_realtime/realized_vol.py  (new)
PythonDataService/app/engine/edge/features_realtime/regime_features.py (new)
PythonDataService/app/engine/edge/features_realtime/iv30_constructor.py (new)
PythonDataService/app/engine/edge/features_realtime/delta_inversion.py  (new)
PythonDataService/app/engine/edge/labels_oracle/forward_rv.py        (new)
PythonDataService/app/engine/edge/regime_clustering.py               (new)
PythonDataService/app/engine/edge/regime_drift.py                    (new)
PythonDataService/app/engine/edge/regime_strategy_eval.py            (new)
PythonDataService/app/engine/edge/period_splitter.py                 (new)
PythonDataService/app/engine/edge/portfolio_aggregator.py            (new)
PythonDataService/app/engine/edge/robustness_stats.py                (new)
PythonDataService/app/engine/edge/cross_asset_runner.py              (new)
PythonDataService/app/engine/edge/edge_score.py                      (new)
PythonDataService/app/engine/edge/vrp.py                             (new)
PythonDataService/app/routers/edge.py                                (new)
PythonDataService/app/main.py                                        (registered router)
PythonDataService/tests/edge/                                        (new — 8 test modules)
Frontend/src/app/components/edge/                                    (new package)
Frontend/src/app/components/edge/edge.component.{ts,html,scss}       (new)
Frontend/src/app/components/edge/realized-vs-iv/                     (new)
Frontend/src/app/components/edge/cross-asset/                        (new)
Frontend/src/app/components/edge/regimes/                            (new)
Frontend/src/app/app.routes.ts                                       (added /edge + 3 children)
docs/architecture/edge-feature-design.md                             (canonical spec — 570 lines)
docs/architecture/edge-design-temporary-docs.md                      (validation reference)
docs/architecture/design-handoff-edge-2026-04-25.md                  (this file)
```

## Sequencing for design

In order of unblocking value:

1. **Visual identity confirmation** — palette, typography scale, chart conventions consistent across the three views.
2. **`/edge` parent nav cards** — they're the user's first impression. Tighten before going deep on any sub-route.
3. **`/edge/regimes`** — most novel visualization (regime-colored candles + transition matrix); also the most independent (no IV dependency in v1).
4. **`/edge/realized-vs-iv`** — most charts to wire (price + IV + RV bands + VRP histogram + signals + coverage); biggest payoff for the user.
5. **`/edge/cross-asset`** — heatmap + small multiples; mostly an analytical surface, less novel than the other two.
6. **Edge Score placement** — overlay vs drawer call, then implementation.
7. **Trade Simulator drawer** — last; the math is the value, the UI is just the receipt.

Once the design pass is in, the v2 backlog includes: `.NET` GraphQL passthrough, real Polygon-backed bar fetching server-side, options margin model, bar magnifier for intra-bar fills, real strategy registry replacing the placeholder buy-and-hold.
