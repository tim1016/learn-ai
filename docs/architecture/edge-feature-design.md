# Edge Feature — Technical Design Document

**Status:** Plan, pre-implementation
**Branch:** `volatility`
**Date:** 2026-04-25
**Owner:** TBD
**Related plans:** `data-lab-roadmap.md`, `engine-tv-alignment-roadmap.md`, `portfolio-system-plan.md`, `engine-phase-1-2-refined-plan.md`
**Memory pointer:** `~/.claude/projects/.../memory/edge_feature_roadmap.md`

This document describes a new parent route `/edge` that bundles three quantitative-research views — **Realized vs IV**, **Cross-Asset Validation**, **Regime Clustering** — plus two cross-cutting capabilities (**Trade Simulator**, **Edge Score**). The intent is to convert "interesting series" into ranked, decision-ready edges.

It is the source for an upcoming Claude Design handoff (UI/UX). Brief notes flagged **`📚 Research`** indicate areas for further internet enhancement of this document.

**Validation reference.** `docs/architecture/edge-design-temporary-docs.md` is a parallel pedagogical narrative of this design. It is treated as a cross-check on terminology, citations, and layered explanations — not as the canonical engineering spec (this file is). When the two diverge, this document wins; the divergence is reconciled inline with **`✅ Validated`** or **`⚠️ Reconciled`** marks.

**Audience layers.** Each major feature section opens with three reading levels: **📖 Layman** (the analogy), **🎯 Professional** (the operational meaning), **📐 Reference** (the math and citation). The math is repeatable from the reference framing alone; the other two exist to compress onboarding for cross-functional readers (designers, PMs, analysts new to the domain).

---

## 1. Scope and locked decisions

| # | Decision | Choice |
|---|---|---|
| 1 | Historical IV source | Back-solve IV from stored option mid-quotes; nightly snapshot forward |
| 2 | Bar size | 15-min and daily, side by side |
| 3 | RV estimators | Close-to-close, Parkinson, Garman-Klass, Yang-Zhang (all four) |
| 4 | Cross-asset universe | SPY + QQQ + IWM + DIA (fixed) |
| 5 | Aggregation | Per-asset + equal-weight + vol-weighted composites |
| 6 | Time splits | Rolling N-year + calendar-year + walk-forward (all three) |
| 7 | Regime algorithm | HMM and k-means with comparison view |
| 8 | Regime features | trend slope, RV, ATR%, vol z-score, IV30, 25Δ skew, IV term-structure slope, ΔIV, IV-vol |
| 9 | Route layout | Parent nav cards + child URLs (`/edge/realized-vs-iv` etc.) |
| 10 | Backend | Frontend → Python `/api/edge/*` directly (skip .NET v1) |
| 11 | IV-RV time basis | Trading-day (252) — IV30 reinterpreted as IV21-trading; cures weekend bias |
| 12 | Data isolation | Hard split `features_realtime/` vs `labels_oracle/`; CI grep guard |
| 13 | Vol-of-vol | Added as 8th and 9th regime feature; available to Edge Score |
| 14 | Regime drift | Rolling refit (default monthly on 12-month window) + Hungarian label alignment + drift score |
| 15 | Options spread model | `spread = max(0.05, k·IV·√T·(1+α·|Δ−0.5|))`; `tradable` flag per trade |
| 16 | UI theme | Dark, matching TradingView and Strategy Lab (reuse data-lab CSS custom properties) |

**Out of scope (v1):** .NET GraphQL passthrough (deferred); options margin model; Greek-aware position sizing; bar magnifier for intra-bar fills; live trading.

---

## 2. Architecture

```
┌────────────────────────────────────────────────────────────────┐
│ Angular 21 — Frontend/src/app/components/edge/                 │
│   /edge → 3 nav cards                                          │
│     /edge/realized-vs-iv                                       │
│     /edge/cross-asset                                          │
│     /edge/regimes                                              │
│   Standalone components, OnPush, signals, rxResource()         │
│   Dark theme (TradingView / data-lab CSS variables)            │
└──────────────────────────────┬─────────────────────────────────┘
                               │ HTTP (no GraphQL in v1)
┌──────────────────────────────▼─────────────────────────────────┐
│ FastAPI — PythonDataService/app/routers/edge.py                │
│   /api/edge/realized-vs-iv/* /cross-asset/* /regimes/*         │
│   /edge-score/* /trade-sim/*                                   │
└──────────────────────────────┬─────────────────────────────────┘
                               │
┌──────────────────────────────▼─────────────────────────────────┐
│ engine/edge/                                                   │
│   features_realtime/   ← past-only, leakage-guarded            │
│     realized_vol.py    iv30_constructor.py                     │
│     regime_features.py  delta_inversion.py                     │
│   labels_oracle/        ← forward outcomes ONLY                │
│     forward_rv.py        vrp_oracle.py                         │
│   regime_clustering.py  regime_strategy_eval.py                │
│   cross_asset_runner.py  portfolio_aggregator.py               │
│   period_splitter.py     robustness_stats.py                   │
│   trade_simulator.py     spread_model.py                       │
│   edge_score.py          regime_drift.py                       │
│ Reuses: volatility/solver.py, surface.py;                      │
│         engine/engine.py; research/signal/regime.py            │
└────────────────────────────────────────────────────────────────┘
```

**Wire/storage canonical timestamp:** `int64 ms UTC` everywhere (per `numerical-rigor.md` § Timestamp rigor). `America/New_York` for in-function wall-clock semantics, converted back to ms before any return/persist.

---

## 3. Data isolation layers (anti-leakage)

The most common cause of false positive backtest results is forward-information leaking into "real-time" features. The directory split is structural enforcement, not just convention.

> **📖 Layman.** No peeking at tomorrow's answers on today's test. If the model is allowed to glance at the future when making a past decision, the backtest looks brilliant in simulation and bleeds in live trading because that peek never arrives in real time.
>
> **🎯 Professional.** Look-ahead bias is the dominant cause of false-positive backtests. Physical separation of feature and label DataFrames removes the most common contamination paths (accidental joins, implicit alignment, careless `.merge()` on a forward-shifted column).
>
> **📐 Reference.** Temporal-integrity enforcement via directory-level partitioning + lint, equivalent in spirit to López de Prado's purged k-fold cross-validation (`mlfinlab`) and `tsfresh`'s time-windowed feature pipeline.

### Rules

- **`features_realtime/`** — every column produced uses `pd.Series.shift(N)` with `N >= 0` only, or `.rolling(window).agg().shift(0)`. Never `.shift(-N)`. Never a join with anything from `labels_oracle/`.
- **`labels_oracle/`** — produces forward-RV, oracle-VRP, ex-post outcome labels. Used only for analysis (heatmaps, hit-rate computation), never as inputs to features.
- **CI guard:** `pytest -k test_no_leakage` runs a regex over `features_realtime/*.py`:
  ```
  forbidden patterns: r"\.shift\(-\d+\)" , r"from .*labels_oracle"
  ```
  Build fails on hit. Single-line override allowed via `# noqa: leakage-allowed` with mandatory inline justification.

### Rationale

Once a forward-shifted column lives in the same DataFrame as features, accidental joins or implicit alignment (`df[features + ['rv_fwd']]`) silently contaminates. Physical separation + lint + tolerance for explicit override is the cheapest reliable guard.

> 📚 **Research** — methods used in production quant shops to enforce time-respecting feature pipelines (e.g., `tsfresh` time-windowed features, `mlfinlab` purged k-fold cross-validation).

---

## 4. Feature 1 — Realized vs IV

> **📖 Layman.** Realized volatility is how shaky the steering wheel has been; implied volatility is the price of insurance against the next pothole. The Variance Risk Premium (VRP) is what people overpaid for that insurance. When IV is *cheap* relative to what RV ends up doing, options are underpriced — that is the contrarian "buy options" condition.
>
> **🎯 Professional.** VRP is a compensated risk factor: the spread earned by providing tail-risk protection. Persistent positive VRP funds short-vol strategies (premium selling, iron condors); persistent negative VRP funds long-vol structures (straddles, calendars, gamma scalps). Sign and magnitude depend on regime — handled in F3.
>
> **📐 Reference.** Forward ex-post VRP per Bollerslev-Tauchen-Zhou (2009) and Carr-Wu (2009). See §4.1 for the operational definition.

### 4.1 Definitions

- **Realized variance over `[t, t+τ]`** (close-to-close form):
  $$\text{RV}^2_{t \to t+\tau} = \sum_{i=1}^{W_\tau} r_{t+i}^2$$
  where $r_i = \ln(C_i/C_{i-1})$ and $W_\tau$ is the bar count covering $\tau$ trading days.
- **Implied volatility** $\sigma_t^{(\tau)}$: back-solved from market option prices via Newton-Raphson on Black-Scholes (existing `volatility/solver.py`).
- **Variance Risk Premium (forward, ex-post):**
  $$\widehat{\text{VRP}}_t^{(\tau)} = (\sigma_t^{(\tau)})^2 - \text{RV}^2_{t \to t+\tau}$$

### 4.2 Trading-day basis (refinement #11)

IV30 is *quoted* as a 30-calendar-day vol but options expire on calendar dates. Realized variance is computed only on trading bars. Without normalization, the IV horizon includes ~9 weekend days where RV cannot accumulate, biasing VRP positive (false short-vol signal).

**Fix:** annualize both with 252 trading days; map IV30 to its trading-day equivalent. The bar-count for the forward window:

$$W_\tau^{\text{trading}} = \tau_\text{calendar} \cdot \frac{252}{365} \cdot k$$

where `k` = bars per trading day (1 for daily, 26 for 15-min RTH). For τ=30: ≈ 21 trading days = 21 daily bars or ≈ 546 fifteen-min bars.

> 📚 **Research** — alternative: include weekend variance proxy via overnight return decomposition (Yang-Zhang 2000 § 3); compare bias of the two approaches on SPY 2015-2024.

### 4.3 Realized volatility estimators

All four implemented in `features_realtime/realized_vol.py`. Each annualized via `× sqrt(252 · k / W)`.

| Estimator | Formula (per-period variance) | Efficiency vs CtC | Citation |
|---|---|---|---|
| Close-to-close | $\frac{1}{n-1}\sum (r_t - \bar r)^2$ | 1× (baseline) | Standard |
| Parkinson | $\frac{1}{4n\ln 2}\sum (\ln H_t/L_t)^2$ | ~5× | Parkinson 1980 |
| Garman-Klass | $\frac{1}{n}\sum [\frac{1}{2}(\ln H/L)^2 - (2\ln 2 - 1)(\ln C/O)^2]$ | ~7.4× | Garman & Klass 1980 |
| Yang-Zhang | $\sigma_O^2 + k\sigma_C^2 + (1-k)\sigma_{RS}^2$, $k = \frac{0.34}{1.34 + (N+1)/(N-1)}$ | up to ~14× (small N) | Yang & Zhang 2000 |

**Efficiency** = ratio of close-to-close variance to estimator variance on the same data. ⚠️ *Reconciled vs validation source:* the temp doc cites a flat ~14× for Yang-Zhang; the published number is more nuanced — efficiency *peaks* at small N (~14× per the Yang-Zhang paper) and decays toward 2-3× at large N. The system uses Yang-Zhang as the **default estimator** for regime features and the daily VRP track because it is the only one of the four that is simultaneously drift-independent *and* gap-aware (Parkinson and GK assume zero drift and ignore overnight returns).

**Forward variants** in `labels_oracle/forward_rv.py` apply `.shift(-W)`; identical estimator code, different framing.

**Intraday extension — Realized Kernel (RK).** ✅ *Validated against temp doc:* for 15-min bars and below, microstructure noise (bid-ask bounce, irregular trade timing) inflates close-to-close estimators. Realized Kernel estimators (Barndorff-Nielsen et al. 2008) apply a Parzen kernel weight to empirical autocovariances of high-frequency returns to recover the underlying efficient-price variance. RK is the **recommended intraday estimator when raw-trade data is available**; it is implemented in `features_realtime/realized_vol_rk.py` as opt-in (`estimator="rk"`). When only OHLC bars are available (the default Polygon path), Yang-Zhang remains the recommended choice — RK requires tick-level data.

> 📚 **Research** — Parzen kernel bandwidth selection (Barndorff-Nielsen, Hansen, Lunde, Shephard 2009); two-scale realized vol (Zhang, Mykland, Aït-Sahalia 2005) as an alternative for sparse data.

### 4.4 IV pipeline

1. Read stored option mid-quotes from Postgres `OptionIvSnapshots` (and any raw chain-quote table when present).
2. **Delta-based moneyness** (refinement #2) — for each `(timestamp, expiry)`, query SABR/SVI surface (existing `volatility/surface.py`) at deltas $\{0.50, 0.25, 0.75\}$ via fixed-point inversion (`features_realtime/delta_inversion.py`):
   $$K = S \exp\left[(r - q + \sigma^2/2)T - \sigma\sqrt{T} \cdot N^{-1}(\Delta e^{qT})\right], \quad \sigma \leftarrow \sigma_{\text{surface}}(K, T)$$
   3-5 fixed-point iterations under typical smiles; bisection fallback for steep smiles.
3. Construct **IV30 ATM** by linear interpolation in variance across expiries (CBOE VIX whitepaper formula):
   $$\sigma_{30,\text{ATM}}^2 = w \cdot \sigma_{T_1}^2 \cdot \frac{T_1}{30/365} + (1-w) \cdot \sigma_{T_2}^2 \cdot \frac{T_2}{30/365}$$
4. Construct **25Δ skew** = $\sigma_{25\Delta P} - \sigma_{25\Delta C}$ on the 30d term.
5. Construct **term-structure slope** = $\sigma_{50\Delta, 60d} - \sigma_{50\Delta, 30d}$.

> 📚 **Research** — VIX whitepaper full methodology (CBOE 2019), variance-swap fair-strike formula vs VIX approximation, pros/cons of model-free implied vol.

### 4.5 Vol-of-vol (refinement #13)

Two additional features in `features_realtime/iv30_constructor.py`:

- $\Delta\text{IV}_t = \sigma_{t,\text{IV30}} - \sigma_{t-1,\text{IV30}}$
- $\text{IV-vol}_t = \text{rolling-std}(\sigma_{\text{IV30}}, 20)$

Empirically these are large in panic onsets and IV-collapse mean-reversions; both are first-order signals for short-vol entry timing.

> 📚 **Research** — VVIX (CBOE vol-of-VIX index) and its predictive content; ARFIMA modeling of IV time-series.

### 4.6 Endpoints

```python
POST /api/edge/realized-vs-iv/series
  body: {symbol, start_ms, end_ms, bar_size: "15m"|"1d", tenor_days: 30,
         estimators, delta_buckets, windows}
  resp: {ts, iv: {atm_50d, skew_25d, term_slope, iv_change, iv_vol},
         rv_trailing: {est_window: float[]},
         rv_forward:  {est_window: float[]},
         vrp_forward, vrp_z,
         coverage: {forward_nan_bars, iv_data_first_ts, iv_data_last_ts}}

POST /api/edge/realized-vs-iv/signals
  body: above + {rule, threshold, lookback}
  resp: {ts, signal_oracle: -1|0|1, signal_realtime: -1|0|1, vrp_z}

GET  /api/edge/realized-vs-iv/coverage/{symbol}
  resp: {first_ts, last_ts, n_bars, missing_pct, source_breakdown}
```

---

## 5. Feature 2 — Cross-Asset Validation

> **📖 Layman.** A "one-hit wonder" test. If the strategy works on SPY but loses on QQQ, IWM, and DIA, it was probably luck. Multiple assets across multiple time windows together are the cheapest robustness check available.
>
> **🎯 Professional.** Bailey & López de Prado: probability of backtest overfitting (PBO) drops geometrically in the number of independent splits a strategy survives. The four-asset / multi-period grid is the smallest defensible matrix for a US-equity-index strategy; results that survive it are still not guaranteed, but results that fail it are reliably noise.
>
> **📐 Reference.** Deflated Sharpe Ratio and Combinatorially Symmetric Cross-Validation (CSCV) per López de Prado (2014). See §5.3.

### 5.1 Aggregation

For asset returns $R^{(i)}_t$ and weights $w_i$:

| Method | Weight | Use |
|---|---|---|
| Per-asset | n/a | Inspect which asset drives results |
| Equal-weight | $w_i = 1/N$ | Default robustness check |
| Vol-weighted | $w_i \propto 1/\hat\sigma_i^{(60d)}$, monthly rebal | Penalize vol concentration |

### 5.2 Splits (all three from refinement #6)

- **Rolling:** $W = \{[t_0, t_0+N], [t_0+\Delta, t_0+N+\Delta], \ldots\}$ — defaults $N=2y$, $\Delta=6m$.
- **Calendar buckets:** one bucket per calendar year.
- **Walk-forward (anchored):** train $[t_0, t_0+L]$, test $(t_0+L, t_0+L+H]$, slide by $H$.

### 5.3 Robustness statistics

- **Robustness score:** share of `(asset × period)` cells with positive Sharpe.
- **Deflated Sharpe Ratio (DSR)** per asset (López de Prado 2014) — adjusts Sharpe for selection bias and non-normality.
- **Probability of Backtest Overfitting (PBO)** across rolling windows — measures rank reversal between in-sample and out-of-sample performance.

> 📚 **Research** — full DSR formula and reference implementation (López de Prado 2014, "The Sharpe Ratio Efficient Frontier"); CSCV (Combinatorially Symmetric Cross-Validation) for PBO.

### 5.4 Endpoints

```python
POST /api/edge/cross-asset/run
  body: {strategy_name, strategy_params, symbols, start_ms, end_ms, bar_size,
         split_mode, split_params}
  resp: {by_asset: {sym: [{period, stats, equity_curve}]},
         composites: {equal_weight, vol_weighted},
         robustness: {score, dsr_by_asset, pbo}}

GET  /api/edge/cross-asset/strategies
  resp: {available_strategies: [{name, params_schema}]}
```

---

## 6. Feature 3 — Regime Clustering

> **📖 Layman.** The market has seasons. You don't plant tomatoes in February or ski in July. Regime detection is the weather station that tells you which season you're in so you bring the right strategy.
>
> **🎯 Professional.** Strategy edge is regime-conditional: trend-following lives in trending-low-vol; mean-reversion lives in choppy-high-vol; vol-selling lives in trending-high-vol with elevated VRP. A flat all-period Sharpe can hide a strong positive Sharpe in two regimes and a sharp negative one in the third — partition by regime to surface it, then gate trades on regime confidence.
>
> **📐 Reference.** State-space models (HMM, HSMM) and unsupervised clustering (k-means, GMM) compared in §6.1.

### 6.1 Algorithms

- **K-means** (Lloyd 1982) — partition $T$ feature vectors into $K$ clusters minimizing within-cluster SSE. Cheap, no temporal structure → high turnover (state assignments flip bar-to-bar even when the underlying regime is stable).
- **Hidden Markov Model** (Baum-Welch) — hidden state $z_t$ with transition matrix $A_{ij} = P(z_t=j \mid z_{t-1}=i)$ and Gaussian emission $x_t \mid z_t \sim \mathcal{N}(\mu_{z_t}, \Sigma_{z_t})$. EM via forward-backward yields posterior $\gamma_t(k)$. Library: `hmmlearn.GaussianHMM(n_components=3, covariance_type="full")`. The transition matrix's diagonal dominance gives HMM its characteristic *stickiness* — empirically the right inductive bias for equity regimes.
- **Hidden Semi-Markov Model (HSMM, opt-in advanced)** — ✅ *Validated against temp doc:* relaxes HMM's geometric duration assumption with explicit state-duration distributions (negative-binomial or gamma). Better fits the empirical asymmetry of equity regimes (bull markets last years, panic regimes last weeks). Selected via `algorithm="hsmm"` in the regime endpoint. Library: `pyhsmm` or custom EM. **Recommended when transition counts under HMM remain too high after stability filtering** — the explicit duration model is the proper fix, not a tighter persistence threshold.

| Property | K-means | HMM | HSMM |
|---|---|---|---|
| Temporal awareness | None | Markov (1-step) | Semi-Markov (explicit duration) |
| State persistence | Low (flickers) | Moderate (sticky diagonal) | High (modeled directly) |
| Computational cost | Low | Moderate | High |
| Best for | Static partitioning | Short/medium-term signals | Long-term regimes |
| Posterior available | No (proxy via centroid distance) | Yes ($\gamma_t(k)$) | Yes |

> 📚 **Research** — Gaussian Mixture HMM for non-elliptical regimes; switching-VAR alternatives; HSMM duration distribution selection (negative-binomial vs gamma); state-count selection via BIC and cross-validated likelihood.

### 6.2 Feature engineering

Features in `features_realtime/regime_features.py`, all rolling-z-scored on 60-bar lookback before clustering:

| Feature | Definition |
|---|---|
| trend slope | OLS slope of close on time, 20 bars, normalized by ATR |
| RV (YZ) | Yang-Zhang vol, 20 bars, annualized |
| ATR% | ATR(14) / Close |
| Volume z | (Vol − rolling mean) / rolling std, 20 bars |
| IV30 (50Δ) | from F1 |
| 25Δ skew | from F1 |
| IV term slope | from F1 |
| ΔIV | from F1 (refinement #13) |
| IV-vol | from F1 (refinement #13) |

Couples F1 → F3: a symbol's regime view is OHLCV-only until its IV pipeline lands; full feature set after.

### 6.3 Stability filter (refinement #4)

`regime_active[t] = True` iff:
1. **Confidence:** $\max_k \gamma_t(k) > p_{\min}$ (default 0.7).
2. **Persistence:** current run-length $\ell_t \geq L_{\min}$ (default 5 bars).

K-means proxy: $1 - d(x_t, \mu_{z_t}) / d(x_t, \mu_{2\text{nd}})$.

### 6.4 Drift control (refinement #14)

- Refit cadence configurable; default monthly, 12-month rolling window.
- **Label alignment via Hungarian algorithm** (`scipy.optimize.linear_sum_assignment`) on centroid distance — solves HMM label-switching across refits.
- `regime_stability_score` = symmetric KL divergence between consecutive transition matrices, plus mean centroid displacement. Plotted as sparkline over time; spikes flag structural breaks.

> 📚 **Research** — change-point detection (Bai-Perron, Adams-MacKay BOCPD) as an alternative or complement to fixed-cadence refit; literature on regime stability metrics in macro-finance.

### 6.5 Endpoints

```python
POST /api/edge/regimes/cluster
  body: {symbol, start_ms, end_ms, bar_size, n_states=3,
         algorithms=["hmm","kmeans"], features=[...], 
         refit: {cadence_days, window_months}}
  resp: {ts, kmeans_labels, hmm_labels, hmm_proba, regime_active,
         transition_matrix, regime_summary, drift: {score_series, refit_dates}}

POST /api/edge/regimes/strategy-fit
  body: {trade_ledger, regime_labels_request}
  resp: {by_regime: {label: {sharpe, win_rate, n_trades, total_return, avg_holding}}}
```

---

## 7. Trade Simulator (cross-cutting)

> **📖 Layman.** A great-looking shirt on sale 50 miles away isn't a deal if the gas to get there costs more than the discount. In trading the "gas" is the bid-ask spread, the slippage, and the broker fees. The simulator is *pessimistic*: it assumes you got the worst available price and paid the maximum fee, so a strategy that survives this layer is strong enough to consider for live deployment.
>
> **🎯 Professional.** Transaction Cost Analysis (TCA) is the difference between gross signal performance and net P&L. For options, friction is non-trivial: spreads scale with vol and tenor, liquidity varies sharply by strike, and commissions on multi-leg structures compound quickly. The simulator models these explicitly and surfaces a `cost_attribution` breakdown so the user can see exactly which line item ate the edge.
>
> **📐 Reference.** Pessimistic execution model with Madhavan-Smidt (1991) spread baseline (§7.2); no bar magnifier in v1 (per `engine-phase-1-2-refined-plan.md`).

Module: `engine/edge/trade_simulator.py` + `spread_model.py`. Pessimistic-first; no bar magnifier (per `engine-phase-1-2-refined-plan.md`).

### 7.1 Execution rules

| Aspect | Default |
|---|---|
| Entry | T+1 bar open after signal |
| Exit triggers | (a) time-stop N bars (b) opposite signal (c) hard stop % (d) target % — first to fire wins |
| Sizing | Fixed contracts OR % equity |

### 7.2 Cost model (refinement #15)

Per round-trip dollar cost:

$$\text{cost} = \text{spread} \cdot Q + 2 s \cdot \text{mid} \cdot Q + 2 c \cdot Q$$

with $s$ = slippage (5 bps stocks / 2 % options), $c$ = per-unit fee.

**Options spread model — Madhavan-Smidt baseline.** ✅ *Validated against temp doc:* following Madhavan & Smidt's (1991) liquidity framework, the bid-ask spread reflects (a) market-maker inventory risk and (b) asymmetric-information cost. Empirical fit:

$$\text{spread}(K, T, \sigma) = \max\left(0.05, \; k \cdot \sigma \cdot \sqrt{T} \cdot \left(1 + \alpha \cdot |\Delta(K) - 0.5|\right) \cdot S\right)$$

The three multipliers map to interpretable market dynamics:

- **Volatility scaling ($\sigma$)** — market makers widen spreads when realized risk is elevated, compensating for inventory variance during their holding period.
- **Time scaling ($\sqrt{T}$)** — the square-root law of risk; longer-dated contracts carry more inventory uncertainty. The same $\sqrt{T}$ scaling holds across asset classes (equities, options, crypto), making it one of the more durable stylized facts in market microstructure.
- **Moneyness bias ($|\Delta - 0.5|$)** — wing options are less liquid than ATM; the $\alpha$ coefficient controls the steepness of the wing penalty. Empirically, 25Δ wings carry 1.5–2× the spread of 50Δ ATM in normal regimes, widening to 3–4× during stress.

Defaults $k = 0.04$, $\alpha = 1.5$ — calibrated from observed Polygon snapshot bid/ask history. Stocks: percentage of price (default 1 bp).

> 📚 **Research** — full empirical fit of (K, T, σ, volume) → spread on Polygon history; Madhavan & Smidt (1991) original paper; CBOE liquidity tier data; persistence of the $\sqrt{T}$ scaling through crisis periods (Mar 2020, Apr 2025).

### 7.3 Tradeability flag

Each simulated trade flagged:
- `tradable` — modeled spread × quoted volume passes liquidity floor (default: open-interest > 100, daily volume > 50, spread < 25 % of mid)
- `theoretical` — fails at least one floor; included in stats but separated in UI

### 7.4 Output schema

```python
{
  "trades": [{entry_ts, exit_ts, side, qty, entry_px, exit_px,
              gross_pnl, costs, net_pnl, tradable: bool}],
  "equity_curve": [{ts, equity, drawdown}],
  "stats": {n_trades, n_tradable, win_rate, avg_win, avg_loss,
            sharpe, sortino, max_dd, mar},
  "cost_attribution": {gross_pnl, spread_cost, slippage_cost, commissions, net_pnl,
                       net_pnl_tradable_only}
}
```

The `cost_attribution` plus `net_pnl_tradable_only` together answer the question that kills most paper strategies: "after friction and after only counting the trades you could actually fill, what's left?"

### 7.5 Endpoints

```python
POST /api/edge/trade-sim/run
  body: {signal_series, bars, instrument: "stock"|"option_chain",
         option_quotes?, exit_rules, sizing, cost_overrides?}
  resp: trade simulator output schema (above)
```

---

## 8. Edge Score (cross-cutting)

> **📖 Layman.** A "confidence meter" for a trade. The Edge Score looks at the VRP (is it a good deal?), the regime (is the weather right?), the IV percentile (is fear cheap or expensive?), and the trend (which way is the market moving?). When the four agree, the meter pegs to +1 or -1. When they disagree, it stays near 0 — the system tells you to do nothing, which is itself a decision.
>
> **🎯 Professional.** A multi-factor scalar that compresses the four headline edges into a single per-bar action signal. Each component is bounded via $\tanh$ so no single factor dominates; weights are sensible defaults that ship fixed (anti-overfit rail). Performance is always measured on the tradeability layer (§7) under walk-forward (§5.2), never in-sample.
>
> **📐 Reference.** Linear composite with $\tanh$-bounded components; Bayesian credible-interval calibration available as opt-in (§8.1 below).

Per-bar composite scalar in $[-1, +1]$, sign convention **+1 = long-vol attractive**.

$$E_t = w_1 S^{\text{vrp}}_t + w_2 S^{\text{regime}}_t + w_3 S^{\text{iv}}_t + w_4 S^{\text{trend}}_t$$

| Component | Definition | Sign reasoning |
|---|---|---|
| $S^{\text{vrp}}$ | $-\tanh(\text{VRP}^{\text{fwd}} / \sigma_{\text{VRP},252})$ | High VRP = options rich → short vol |
| $S^{\text{regime}}$ | User-defined map per regime label | Choppy+highVol → +0.5; trending+highVol → −0.5 (defaults) |
| $S^{\text{iv}}$ | $-2(\text{percentile}(\text{IV30}, 252) - 0.5)$ | High IV percentile → expensive → short vol |
| $S^{\text{trend}}$ | $-\|\text{slope}\|/\text{ATR}$, clipped $[-1, 0]$ | Strong trend = poor for long vol |

Defaults $\mathbf{w} = [0.4, 0.3, 0.2, 0.1]$. Action thresholds: $E_t > +0.5$ strong long-vol, $< -0.5$ strong short-vol, else flat.

**Anti-overfitting rails:**
1. Default weights ship fixed.
2. Components reported separately as audit trail.
3. Performance always measured on tradeability layer under walk-forward split — no in-sample numbers.

### 8.1 Advanced (opt-in): Bayesian weight calibration

⚠️ *Reconciled vs validation source:* the temp doc proposes Bayesian *automatic* weight estimation; this document keeps fixed defaults as the canonical mode (anti-overfit rail #1) and offers Bayesian estimation as an **opt-in sensitivity tool**, not an automated optimizer.

- **Prior:** Dirichlet over $\mathbf{w}$ centered on the default $[0.4, 0.3, 0.2, 0.1]$ with concentration $\kappa = 10$ (mildly informative — lets data move the weights but resists single-period overreaction).
- **Likelihood:** each component's historical hit-rate as a function of forward 5-day return sign, computed under the data-isolation contract (§3) so no oracle leakage.
- **Posterior:** Hamiltonian Monte Carlo (PyMC) for the full Dirichlet update; closed-form Dirichlet-Multinomial conjugate where the likelihood admits it.

The output is a weight **credible interval**, not a point estimate. The UI shows prior weights, posterior mean, and 90 % credible band side by side — so the user sees where the data supports moving the weights and where it doesn't. **Posterior weights are never auto-adopted.** Tuning runs are logged to surface p-hacking risk; performance is still measured under walk-forward + tradeability (rails #2, #3).

> 📚 **Research** — Bayesian model averaging across composite-score variants; conjugate Dirichlet-Multinomial; horseshoe priors for sparse weight selection; comparison to frequentist robust regression.

### Endpoint

```python
POST /api/edge/edge-score/series
  body: {symbol, start_ms, end_ms, bar_size, weights?, regime_score_map?}
  resp: {ts, edge_score, components, action: -1|0|1}
```

> 📚 **Research** — alternatives to linear composite (rank-based, Kelly-fraction-weighted, copula-based); Bayesian model averaging.

---

## 9. Math provenance and testing

Per `numerical-rigor.md` and `math-rigor.md`. All fixtures under `PythonDataService/tests/fixtures/golden/edge/`:

| Fixture | Reference | Tolerance |
|---|---|---|
| `rv_ctc/`, `rv_parkinson/`, `rv_gk/`, `rv_yz/` | R `TTR::volatility(calc=...)` | `atol=1e-9, rtol=0` |
| `iv_solver_atm/` | `py_vollib.black_scholes.implied_volatility` | `atol=1e-9, rtol=1e-9` |
| `iv_solver_wings/` | same | `atol=1e-6, rtol=1e-6` (deep-OTM Vega-degraded) |
| `iv30_term_interp/` | CBOE VIX whitepaper hand-computed | `atol=1e-9` |
| `delta_inversion/` | hand-computed BS reference | `atol=1e-7` |
| `kmeans_3state/` | sklearn `KMeans(random_state=42)` | bit-exact labels |
| `hmm_3state/` | hmmlearn `GaussianHMM` fixed seed | `atol=1e-6` posterior |
| `hungarian_alignment/` | scipy reference | bit-exact |
| `dsr/`, `pbo/` | López de Prado tables | `atol=1e-6` |
| `spread_model/` | hand-computed | `atol=1e-9` |

Each fixture: `input.parquet`, `output.parquet`, `attribution.md` (source URL/citation, command, date generated). Edge cases per estimator: empty, all-NaN, single-bar, mid-series gap.

**Math Provenance Contract entry:** every new function is registered in `docs/math-sources-of-truth.md` with status `pending-fixture` → `parity-tested`.

**Leakage CI:** `pytest -k test_no_leakage` greps `features_realtime/` for `\.shift\(-\d+\)` and `from .*labels_oracle` — fail-on-hit unless `# noqa: leakage-allowed` with inline justification.

---

## 10. UI scope (placeholder for Claude Design handoff)

This section is sketched only. A full handoff document (`docs/architecture/design-handoff-edge-2026-XX-XX.md`) will be produced once the Python/Angular functional shell is in place, mirroring the format of `design-handoff-data-lab-2026-04-24.md`.

### Visual identity ("Dark Terminal" aesthetic)

✅ *Validated against temp doc:* the palette below codifies the Dark Terminal direction.

| Token | Hex | Role |
|---|---|---|
| Background | `#0B0E11` | Deep charcoal — primary canvas; minimizes eye strain in long research sessions; matches TradingView terminal mode |
| Surface (card) | `#13171D` | One step elevated; chart wrappers, panels |
| Surface (modal) | `#1A1F26` | Two steps elevated; modals, popovers |
| Accent — neutral | `#3FA9FF` | Cyber-blue; informational lines, axis labels, default series |
| Accent — positive | `#26C281` | Forest-green; +Sharpe, long-vol signal, in-the-money |
| Accent — negative | `#E04E4E` | Crimson-orange; −Sharpe, short-vol signal, drawdown |
| Accent — warning | `#F0B429` | Amber; data coverage gaps, "theoretical" trade flag, leakage-override callouts |
| Text — primary | `#E6E9EF` | High-contrast on background (WCAG AA) |
| Text — muted | `#8B95A7` | Secondary labels, axis ticks |

**Typography.**

- **Numeric data** (price, IV, Sharpe, p-values, timestamps) — monospaced (JetBrains Mono or system mono fallback). Decimals must align across rows in tables and tooltips.
- **Labels and prose** — sans-serif (Inter or system). Sentence case, not Title Case, for headings — matches the analytical-tool register.

Reuse data-lab's existing CSS custom properties where present; introduce new tokens only for Edge-specific semantic roles (`--edge-positive`, `--edge-negative`, `--edge-coverage-gap`, `--edge-theoretical-trade`).

### Per-route layout sketches

- **`/edge`** — three nav cards (RV-vs-IV, Cross-Asset, Regimes); each card shows a tiny live preview (last 30 days sparkline of headline metric)
- **`/edge/realized-vs-iv`** — dual-axis price + IV chart on top; RV bands overlay (estimator selector); VRP histogram + percentile band; signal scatter on price; coverage banner
- **`/edge/cross-asset`** — strategy/universe form with **drag-and-drop universe builder** (compose ad-hoc tickers beyond the fixed four; persist last-used universe per user); heatmap (asset × period × Sharpe); per-asset equity curves as small-multiples; composites tab; robustness scorecard with DSR + PBO callouts
- **`/edge/regimes`** — regime-colored price chart (HMM ↔ k-means ↔ HSMM toggle); **Viterbi vs Posterior** rendering toggle (Viterbi = most-likely-path hard labels; Posterior = per-bar probabilities rendered as opacity over the candles); transition matrix heatmap; per-regime feature radar; strategy-fit P&L bars; drift sparkline
- **Edge Score** — present as either (a) overlay strip on each F1/F3 chart, or (b) standalone `/edge/score` route — design call

> 📚 **Research** — TradingView dark-theme specs, Refinitiv Eikon volatility-page IA, Bloomberg OVDV (options volatility) screen layout for inspiration.

### Interaction notes for design

- **60 FPS target** on 15-min bar series (~10k points/year). Rely on Angular `OnPush` + `signal()`-driven minimal recomputes; canvas-backed chart libraries (uPlot, lightweight-charts, plotly with canvas backend); avoid SVG rendering paths above 10k points.
- **Cross-chart scrub sync.** Zooming or panning the regime chart auto-syncs the VRP, IV30, and Edge Score charts to the same window via a shared `currentRange = signal<{start_ms, end_ms}>(...)`. One source of truth, no event-bus glue code.
- All charts must support range zoom + crosshair; tooltips show the full numeric stack (price, IV30, RV, regime label + posterior, edge score) at the hovered timestamp.
- **Coverage warnings** (forward-RV NaN tail, IV data gaps, "theoretical" trade flags, leakage-override notes) are **first-class UI elements** — banner strips above the affected chart or amber chart-overlay shading on the affected range — not buried in tooltips or footnotes. The user must never have to *discover* a data caveat.
- **AXE compliance, WCAG AA contrast, keyboard navigation** per `angular.md`. The dark palette above is contrast-checked; do not introduce new color tokens without re-checking.

---

## 11. Sequencing

| Order | Item | Why |
|---|---|---|
| 0 | Edge route scaffold + nav cards | unblocks all |
| 1 | `trade_simulator.py` + `spread_model.py` | every other feature plugs into it |
| 2 | F3 regimes (OHLCV-only features) + stability filter + drift control | no IV dependency |
| 3 | F2 cross-asset using trade_simulator | depends on (1) |
| 4 | F1 — coverage probe → IV pipeline (delta-moneyness, forward RV, vol-of-vol) | math-heavy |
| 5 | F3 IV-feature upgrade | depends on F1 |
| 6 | Edge Score | aggregates everything |
| 7 | Design handoff doc → Claude Design for UI/UX polish | ships UI |
| 8 | (v2) .NET GraphQL passthrough; options margin model; bar magnifier | deferred |

---

## 12. Open research questions

For your further internet enhancement of this document.

1. **Trading-day vs calendar-day IV** — empirical magnitude of the bias on SPY/QQQ; preferred method (rescale IV vs include weekend variance proxy).
2. **Realized-kernel estimators** for intraday — gain over Yang-Zhang under microstructure noise; complexity tradeoff.
3. **Options spread empirical fit** — calibration of the spread model on Polygon's stored bid/ask history; whether $\sqrt{T}$ scaling holds across tenors.
4. **HMM state count selection** — 3 is conventional but BIC/cross-validated likelihood may suggest 2 or 4 for index ETFs; HSMM benefits.
5. **Hungarian label alignment** — alternatives (Munkres, optimal transport, persistent labeling); failure modes when state structure genuinely changes.
6. **DSR & PBO formulas** — exact paper formulations and gotchas; library implementations (`mlfinlab`, `pyfolio`).
7. **Variance-swap fair strike vs VIX** — when the approximation breaks; impact on VRP magnitude.
8. **VVIX / vol-of-vol predictive content** — academic references on vol-of-vol as a regime feature.
9. **Coupling F1 → F3** — risk of circularity (regime informs strategy, strategy P&L informs regime fit); statistical guards.
10. **Edge Score weight calibration** — tuning protocol that doesn't overfit; Bayesian credible intervals over weights.

---

## 13. References (initial; to be expanded)

- Parkinson, M. (1980). "The Extreme Value Method for Estimating the Variance of the Rate of Return". *Journal of Business* 53(1).
- Garman, M. B., Klass, M. J. (1980). "On the Estimation of Security Price Volatilities from Historical Data". *Journal of Business* 53(1).
- Yang, D., Zhang, Q. (2000). "Drift-Independent Volatility Estimation Based on High, Low, Open, and Close Prices". *Journal of Business* 73(3).
- Bollerslev, T., Tauchen, G., Zhou, H. (2009). "Expected Stock Returns and Variance Risk Premia". *Review of Financial Studies* 22(11).
- Carr, P., Wu, L. (2009). "Variance Risk Premiums". *Review of Financial Studies* 22(3).
- López de Prado, M. (2014). "The Deflated Sharpe Ratio". *Journal of Portfolio Management* 40(5).
- Bailey, D., López de Prado, M. (2014). "The Probability of Backtest Overfitting". *Journal of Computational Finance*.
- CBOE (2019). *Cboe Volatility Index (VIX) Whitepaper*.
- Hull, J. C. *Options, Futures, and Other Derivatives* (ch. 19, implied volatility).
- Rabiner, L. R. (1989). "A Tutorial on Hidden Markov Models". *Proceedings of the IEEE* 77(2).
- Brenner, M., Subrahmanyam, M. G. (1988). "A Simple Formula to Compute the Implied Standard Deviation". *Financial Analysts Journal*.
