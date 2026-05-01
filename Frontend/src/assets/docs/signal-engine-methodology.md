# Signal Engine — Authority & Methodology

> **Canonical reference** for Research Lab → Signal Engine. Covers the
> statistical methodology (z-score signal construction, walk-forward validation,
> Newey-West Sharpe inference, deflated Sharpe, graduation ladder), the
> execution model used in backtests, the API contract, and the UI surfaces.
>
> **Audience:** graduate-level reader with light finance and statistics
> background. The page is written so that someone who has seen Sharpe ratios
> and linear regression can read every panel; the deeper machinery
> (Newey-West, DSR, block bootstrap) is documented here once and referenced
> by tooltip from the UI.
>
> **Owner:** the engineer editing `PythonDataService/app/research/signal/`.
> If you change the math in that directory, update the matching section here
> *in the same PR* and bump **Last reviewed**.
>
> **Last reviewed:** 2026-04-30 (initial authority — companion to the Stage 0
> kill-switch and graduation-ladder redesign).

---

## Table of contents

- [1. Scope and authority](#1-scope-and-authority)
- [2. Notation and glossary](#2-notation-and-glossary)
- [3. Pipeline overview](#3-pipeline-overview)
- [4. Statistical methodology](#4-statistical-methodology)
  - [4.1 Feature → z-score → signal](#41-feature--z-score--signal)
  - [4.2 Forward return target](#42-forward-return-target)
  - [4.3 Backtest kernel (no-lookahead PnL)](#43-backtest-kernel-no-lookahead-pnl)
  - [4.4 Annualized Sharpe ratio](#44-annualized-sharpe-ratio)
  - [4.5 Effective sample size (Neff)](#45-effective-sample-size-neff)
  - [4.6 Sharpe confidence interval (Newey-West)](#46-sharpe-confidence-interval-newey-west)
  - [4.7 Deflated Sharpe Ratio (DSR)](#47-deflated-sharpe-ratio-dsr)
  - [4.8 Walk-forward validation](#48-walk-forward-validation)
  - [4.9 Alpha-decay test (with power guard)](#49-alpha-decay-test-with-power-guard)
  - [4.10 Parameter stability](#410-parameter-stability)
  - [4.11 Effective trades per regime](#411-effective-trades-per-regime)
- [5. Graduation ladder](#5-graduation-ladder)
  - [5.1 Stage 0 — kill switch](#51-stage-0--kill-switch)
  - [5.2 Stage 1 — weak candidate](#52-stage-1--weak-candidate)
  - [5.3 Stage 2 — research candidate](#53-stage-2--research-candidate)
  - [5.4 Stage 3 — promotion candidate](#54-stage-3--promotion-candidate)
- [6. Execution model](#6-execution-model)
- [7. API contract](#7-api-contract)
- [8. UI surfaces](#8-ui-surfaces)
- [9. Code cross-reference](#9-code-cross-reference)
- [10. Known limitations and roadmap](#10-known-limitations-and-roadmap)
- [11. References](#11-references)

---

## 1. Scope and authority

This document is the single source of truth for the Signal Engine — the
research-lab tool that converts a *feature* (a numeric series like
`momentum_5m` or `rsi_14`) into a *signal* (a position decision in
`{-1, 0, +1}`), backtests that signal, and produces a graduation verdict
about whether the signal warrants further research.

`CLAUDE.md` § 5 — *one canonical implementation per numerical concept* — is
the rule this document operationalises for the Signal Engine. If a number
appears anywhere in the UI, it was produced by the formula and code path
described here.

**In scope**
- Single-asset signal validation against forward log returns.
- Walk-forward Sharpe estimation with per-fold parameter selection.
- Statistical inference (Newey-West Sharpe SE, deflated Sharpe).
- The graduation ladder (Stage 0 / 1 / 2 / 3) and its kill-switch logic.

**Out of scope (handled elsewhere)**
- Cross-asset / cross-sectional validation — see
  [Cross-Sectional](../Frontend/src/app/components/research-lab/batch-runner/)
  runner. Wiring it into graduation is roadmap.
- Capacity / market-impact modelling — not implemented.
- IC-based feature predictiveness — covered separately by
  [indicator-reliability-methodology.md](indicator-reliability-methodology.md).

---

## 2. Notation and glossary

| Symbol | Meaning |
|---|---|
| `f_t` | feature value at bar `t` |
| `μ_train`, `σ_train` | feature mean and std fit on the training segment only |
| `z_t` | `(f_t − μ_train) / σ_train`, optionally sign-flipped for mean-reversion |
| `θ` | absolute z-score threshold (1.0, 1.5, 2.0, …) |
| `s_t` | filtered signal: `sign(z_t) · 1[|z_t| ≥ θ]`, optionally regime-gated |
| `w_t` | position at end of bar `t`, `w_t = clip(s_t, −1, +1)` |
| `r_t` | 15-bar forward log return: `ln(close_{t+15} / close_t)` |
| `g_t` | gross bar return: `w_{t−1} · r_t` |
| `c_bps` | one-way transaction cost in basis points |
| `n_t` | net bar return: `g_t − (c_bps / 10⁴) · |w_t − w_{t−1}|` |
| `S` | annualized Sharpe ratio of `n_t` |
| `N` | raw bar count |
| `N_eff` | autocorrelation-adjusted effective sample size (§ 4.5) |
| `DSR` | Deflated Sharpe Ratio (§ 4.7) |
| `K_folds` | number of walk-forward folds |

Conventions:
- 1-minute bars throughout. Annualisation factor `B = 252 × 390 = 98,280`.
- All timestamps are `int64 ms UTC` per repo-wide policy
  (`.claude/rules/numerical-rigor.md`).
- Returns are log returns. Cumulative return is `Σ n_t`, plotted as the
  equity curve.

---

## 3. Pipeline overview

```
bars → feature → z_train_fit → z_score → threshold filter → regime gate → signal
                                                                              │
                                                                              ▼
                                                                       position w_t
                                                                              │
forward_returns ──────────────────────────────────────────────────────────► PnL
                                                                              │
                                                                              ▼
                                                                  ┌─ in-sample grid
                                                                  ├─ walk-forward folds
                                                                  ├─ stats (Sharpe, CI, DSR)
                                                                  └─ graduation verdict
```

The orchestrator is
[`PythonDataService/app/research/signal/engine.py`](../PythonDataService/app/research/signal/engine.py)
(`run_signal_engine`). Every numbered subsection below names the file and
function that implements it.

---

## 4. Statistical methodology

### 4.1 Feature → z-score → signal

Implemented in
[`signal/standardize.py`](../PythonDataService/app/research/signal/standardize.py).

**Step 1 — z-score using train-only statistics.**

$$ z_t = \frac{f_t - \mu_{\text{train}}}{\sigma_{\text{train}}} $$

`μ_train`, `σ_train` are fit on the in-sample 70 % segment (or, in the
walk-forward path, on the train fold only). Using full-sample statistics
would leak future information into the past — it is **not** done here.

**Step 2 — optional sign flip** for mean-reversion features (`flipSign=true`):
`z_t ← −z_t`.

**Step 3 — threshold filter** with absolute z-score cutoff `θ`:

$$ s_t = \operatorname{sign}(z_t) \cdot \mathbb{1}[|z_t| \ge \theta] $$

**Step 4 — optional regime gate.** If `regimeGateEnabled=true`, multiply
`s_t` by a 0/1 daily regime mask computed in
[`signal/regime.py`](../PythonDataService/app/research/signal/regime.py).
The mask is built from realised vol and trend regimes derived from prior
bars only — no lookahead.

### 4.2 Forward return target

Implemented in
[`research/target.py`](../PythonDataService/app/research/target.py)
(`compute_15min_forward_return`).

$$ r_t = \ln \frac{\text{close}_{t+15}}{\text{close}_t} $$

with NaN whenever the horizon would cross a calendar-day boundary. The
horizon (15 bars) is configurable on `SignalConfig.horizon`.

### 4.3 Backtest kernel (no-lookahead PnL)

Implemented in
[`signal/backtest.py`](../PythonDataService/app/research/signal/backtest.py)
(`run_backtest`).

Position is the clipped sign of the signal: `w_t = clip(s_t, −1, +1)`. The
PnL recursion is:

$$ g_t = w_{t-1} \cdot r_t $$

$$ n_t = g_t - \frac{c_{bps}}{10^4} \cdot |w_t - w_{t-1}| $$

The use of `w_{t−1}` (not `w_t`) is the **single most important line in the
backtest** — it is the reason this is no-lookahead. A position decided at
the end of bar `t−1` earns the return measured from `close_t` to
`close_{t+15}`. See [§ 6 Execution model](#6-execution-model) for the full
timing diagram and the realism caveats.

Annualised turnover:

$$ \text{Turnover} = \frac{1}{T} \sum_{t=1}^{T} |w_t - w_{t-1}| \cdot B $$

where `B = 252 × 390 = 98,280`.

### 4.4 Annualized Sharpe ratio

$$ S = \frac{\bar{n}}{\sigma_n} \cdot \sqrt{B} $$

with `r_f = 0` and `B = 252 × 390`. Computed by `_annualized_sharpe` in
`signal/backtest.py`. We report **net Sharpe** (after costs) as the primary
metric; gross Sharpe is shown only for diagnostic comparison.

### 4.5 Effective sample size (Neff)

Net bar returns are autocorrelated whenever the signal holds the same
position across bars. The raw count `N` overstates the number of
independent observations; we use the Newey-West-style truncation:

$$ N_{\text{eff}} = \frac{N}{1 + 2 \sum_{k=1}^{K} \hat{\rho}_k} $$

where the summation is truncated at the first lag `K` where `|ρ̂_k| < 0.05`
(or at a maximum of 14, whichever comes first). Implemented in
[`signal/diagnostics.py`](../PythonDataService/app/research/signal/diagnostics.py)
(`compute_effective_sample_size`).

The lag-1 autocorrelation and cumulative `ρ` are surfaced in the *Data
Sufficiency* card so the reader can sanity-check the truncation.

### 4.6 Sharpe confidence interval (Newey-West)

**Status:** implemented (Phase 1, 2026-04-30) —
[`compute_sharpe_ci`](../PythonDataService/app/research/signal/diagnostics.py).

The standard error of the annualised Sharpe under autocorrelation is

$$ \operatorname{SE}(S) \approx \sqrt{\frac{1 + \tfrac{1}{2} S^2}{N_{\text{eff}}}} \cdot \sqrt{B} $$

(Lo, 2002, *The Statistics of Sharpe Ratios*, eq. 18 with autocorrelation
correction folded into `N_eff`.) The 95 % CI is `S ± 1.96 · SE(S)`.

We report the CI on the combined-OOS Sharpe of the walk-forward run
(§ 4.8). A signal whose 95 % CI for OOS Sharpe straddles zero is, by this
metric, indistinguishable from noise.

### 4.7 Deflated Sharpe Ratio (DSR)

**Status:** implemented (Phase 1, 2026-04-30) —
[`compute_deflated_sharpe`](../PythonDataService/app/research/signal/diagnostics.py).

The in-sample grid (4 thresholds × 4 cost levels = 16 cells) selects the
maximum net Sharpe. With many trials, the maximum is upward-biased even
under the null hypothesis of zero true Sharpe. Bailey & López de Prado
(2014) propose:

$$ \widehat{\text{DSR}} = \Phi\!\left( \frac{(\widehat{S} - S_0)\sqrt{N_{\text{eff}}-1}}{\sqrt{1 - \gamma_3 \widehat{S} + \tfrac{\gamma_4 - 1}{4} \widehat{S}^2}} \right) $$

where:
- `Ŝ` is the selected (maximum) Sharpe.
- `S_0` is the expected maximum Sharpe under the null:
  `S_0 = √(2 · ln(N_trials)) − γ_E / √(2 · ln(N_trials))` for `N_trials ≥ 2`,
  with `γ_E ≈ 0.5772` (Euler-Mascheroni).
- `γ_3`, `γ_4` are skewness and kurtosis of the bar returns under the
  selected configuration.
- `Φ` is the standard normal CDF; DSR is a probability.

DSR is reported **only on the IS grid headline**. The walk-forward Sharpe
already absorbs selection within each fold (§ 4.8) and does not need
deflation across folds in the current setup.

### 4.8 Walk-forward validation

Implemented in
[`signal/walk_forward.py`](../PythonDataService/app/research/signal/walk_forward.py)
(`run_walk_forward`).

For each fold `i ∈ {0, 1, …, K_folds − 1}`:

1. Split bars into a train window (default 3 months) and a contiguous test
   window (default 1 month).
2. Fit `μ_i`, `σ_i` on the train fold's feature values only.
3. **Select `θ_i`** that maximises **train-fold** net Sharpe across the
   threshold grid. This is per-fold selection, not global. The selected
   `θ_i` is stored on `WalkForwardWindow.best_threshold` and surfaced in the
   per-fold table.
4. Apply the frozen `(μ_i, σ_i, θ_i)` to the test window. Compute the OOS
   net Sharpe, return, max drawdown, and combined-OOS equity-curve segment.

Aggregate OOS metrics:
- `mean_oos_sharpe`, `median_oos_sharpe` — the headline OOS numbers.
- `pct_windows_positive_sharpe` — robustness across folds.
- `combined_oos_cumulative_returns` — the equity curve charted on the page.

> **Why per-fold selection is the right design.** The alternative — picking
> one global `θ` across all data — would leak the test data into the
> selection step. The per-fold approach pays a power cost (each fold is
> small) in exchange for an unbiased OOS estimator. The reader sees the
> distribution of `θ_i` across folds; if the same `θ` wins everywhere the
> signal is somewhat consistent, if `θ_i` jitters wildly the signal is
> over-fitting threshold to noise.

### 4.9 Alpha-decay test (with power guard)

**Status:** implemented (Phase 1, 2026-04-30) — power guard via
`AlphaDecayStats.is_test_valid` /
[`_compute_sharpe_trend_slope`](../PythonDataService/app/research/signal/walk_forward.py).

We regress the per-fold OOS Sharpe `S_i` on the fold index `i`:

$$ S_i = \beta_0 + \beta_1 \cdot i + \epsilon_i, \quad i = 0, \ldots, K_{\text{folds}} - 1 $$

A statistically significant negative `β_1` (`p < 0.05`) is interpreted as
alpha decay — the signal's edge is shrinking over time.

**Power guard.** With `K_folds ≤ 4` the regression has at most 2 residual
degrees of freedom and the t-test is essentially uninformative. The page
displays *"Trend test requires ≥ 5 folds — currently N=K"* in this regime
and does **not** render a misleading p-value. When `K_folds ≥ 5` the
regression and a 95 % CI on `β_1` are both shown.

Future work: replace the fold-level regression entirely with a rolling
20–50-window Sharpe series on the combined OOS returns. Listed in § 10.

### 4.10 Parameter stability

For a fixed cost level (default 1 bps), define:

$$ \text{Stability}(\theta\text{-grid}) = 1 - \frac{\sigma_{S(\theta)}}{|\bar{S}(\theta)|} $$

i.e. one minus the coefficient of variation of net Sharpe across the
threshold grid. A high-quality signal has Sharpe that is roughly flat
across thresholds (Stability ≈ 1.0); a noise-fit signal has wildly
varying Sharpe (Stability ≈ 0.0 or negative).

Stability is computed in
[`signal/graduation.py`](../PythonDataService/app/research/signal/graduation.py)
and is one of the four Stage 0 kill criteria (§ 5.1).

### 4.11 Effective trades per regime

**Status:** implemented (Phase 1, 2026-04-30) —
[`compute_joint_regime_coverage`](../PythonDataService/app/research/signal/diagnostics.py).

The current regime-coverage panel reports day counts per (vol × trend)
bucket. Day counts are misleading because (a) the signal is only active a
small fraction of the time (`pct_active`, typically 1–10 %), and (b) bar
returns are highly autocorrelated.

The redesign reports both raw days **and** the estimated independent-trade
count per bucket:

$$ \widehat{\text{trades}}_{\text{regime}} \approx \frac{N_{\text{eff}} \cdot p_{\text{active}}}{9} $$

assuming roughly equal occupancy across the 3 × 3 grid. The "Pass" badge
is reserved for buckets that exceed a minimum independent-trade count
(default 30); below that, the badge is downgraded to "Sparse".

---

## 5. Graduation ladder

**Status:** Stage 0 kill switch and the 0/1/2/3 ladder are implemented in
the Python layer (Phase 1, 2026-04-30) —
[`evaluate_graduation`](../PythonDataService/app/research/signal/graduation.py).
The corresponding UI rendering is being designed (Phase 3, in flight).

A signal lives at exactly one stage. The stage is the **lowest** stage
whose criteria it fails to advance from.

### 5.1 Stage 0 — kill switch

A signal at Stage 0 has insufficient evidence to warrant further
inspection. Downstream panels (DSR, bootstrap CIs, cross-asset) are
suppressed; the page collapses them under a *"Show diagnostic details
anyway"* disclosure.

**Reject if any of:**

| Criterion | Threshold |
|---|---|
| Parameter stability | `< 0.25` |
| Median OOS Sharpe | `≤ 0` |
| Percentage of OOS folds with positive Sharpe | `< 40 %` |
| Annualised turnover **and** net Sharpe | `> 200×/yr` AND `< 0.5` |

The thresholds above are the project defaults adopted from external
methodology review (2026-04-30). Tightening or loosening them requires a
PR that updates this section, the constants in
`signal/graduation.py`, and the corresponding test in
`tests/test_graduation_stage0.py`.

### 5.2 Stage 1 — weak candidate

A signal that survives Stage 0 but does not meet Stage 2 criteria. At
Stage 1 the page enables:
- Sharpe confidence interval (§ 4.6).
- Walk-forward per-fold details, lifespan chart.

**Advance to Stage 2 if all of:**

| Criterion | Threshold |
|---|---|
| Mean OOS Sharpe | `> 0.3` |
| Parameter stability | `> 0.3` |
| Walk-forward folds | `≥ 4` |

### 5.3 Stage 2 — research candidate

At Stage 2 the page enables:
- Block-bootstrap Sharpe CI (preserves autocorrelation).
- Cross-asset validation runner (defer; wired in roadmap).

**Advance to Stage 3 if all of:**

| Criterion | Threshold |
|---|---|
| Mean OOS Sharpe | `> 0.5` |
| Parameter stability | `> 0.5` |
| % positive folds | `> 60 %` |

### 5.4 Stage 3 — promotion candidate

At Stage 3 the page would (eventually) enable:
- Deflated Sharpe `> 0.5` requirement on the IS grid headline.
- Cross-asset confirmation.
- Plausible turnover-vs-edge economics.
- Hansen SPA / White's Reality Check on the grid.

Stage 3 machinery is explicitly **not** built yet. No signal validated by
this engine has reached Stage 3, so building it is overkill. Logged in
§ 10.

---

## 6. Execution model

The page's *Execution Assumptions* panel describes the timing model
**actually** implemented by the backtest kernel:

| Assumption | Value |
|---|---|
| Signal timestamp | bar close of `t−1` |
| Position effective | from bar `t` onward |
| Return measurement | `close_t → close_{t+15}` (15-bar log return) |
| Position sizing | binary (`±1` / `0`), max `|w| = 1` |
| Cost model | fixed per-side bps on `|w_t − w_{t−1}|` |
| Slippage | **not** modelled (limitation) |

The backtest kernel (§ 4.3) uses `w_{t−1} · r_t` to enforce no lookahead.
Equivalently: a signal generated at the close of bar `t−1` is treated as
filled at the close of bar `t`, and the resulting position earns the
return from `close_t` to `close_{t+15}`.

> **Realism caveat.** "Filled at next bar close" is a simplification. A
> realistic execution would use bar `t`'s open or VWAP. The discrepancy is
> small for 1-minute AAPL bars but can be material for thinly-traded
> assets or longer bar resolutions. It is documented here, displayed in
> the panel, and listed as a known limitation in § 10.

> **Cost realism caveat.** The cost grid runs from 1 to 5 bps per side.
> For AAPL during regular hours that is plausible; for less liquid names
> or at higher turnover (the report shows annualised turnover up to
> 2,628× / yr in some grid cells) it is optimistic. No market-impact
> model is currently included.

---

## 7. API contract

The Signal Engine surface is exposed via two GraphQL fields on the
`Research` query type, backed by the FastAPI service.

```graphql
type Research {
  runSignalEngine(input: SignalEngineInput!): SignalEngineResult!
}

type SignalEngineResult {
  success: Boolean!
  error: String

  # Identifying
  ticker: String!
  featureName: String!
  startDate: String!
  endDate: String!

  # In-sample grid
  backtestGrid: [SignalBacktestResult!]!
  bestThreshold: Float!
  bestCostBps: Float!
  deflatedSharpe: Float          # § 4.7 — Stage 1+

  # Walk-forward
  walkForward: WalkForwardResult
  oosSharpeCi95: SharpeCi        # § 4.6 — Stage 1+

  # Diagnostics
  signalDiagnostics: SignalDiagnostics
  signalBehavior: SignalBehavior
  dataSufficiency: DataSufficiency
  effectiveSample: EffectiveSampleSize
  regimeCoverage: [RegimeBucket!]!   # includes effectiveTrades — § 4.11

  # Verdict
  graduation: GraduationResult!  # includes stage 0/1/2/3
  researchLog: String!
}
```

The full schema lives in
[`Frontend/src/app/services/research.service.ts`](../Frontend/src/app/services/research.service.ts).
Fields tagged "Stage 1+" are populated by the backend only when the
signal advances past Stage 0; otherwise they are `null` and the UI
collapses the corresponding panels.

---

## 8. UI surfaces

The Signal Engine is the third tab under Research Lab → *Validate*. The
top-level layout, top to bottom:

1. **Run form** — ticker, feature, dates, flip-sign, regime-gate toggle.
2. **Verdict block** — graduation stage (0 / 1 / 2 / 3 ladder), one-line
   verdict, headline OOS Sharpe with 95 % CI.
3. **Stage 0 collapse banner** *(only when rejected)* — explains why
   downstream panels are hidden, with a *"Show diagnostic details anyway"*
   disclosure.
4. **Graduation criteria** — pass/fail per criterion with explanations.
5. **Data sufficiency & coverage** — bar counts, `N_eff`, autocorrelation,
   regime grid (now with effective-trades column).
6. **Signal diagnostics** — z-score statistics, % time active, % filtered.
7. **Signal behavior analysis** — hit rate, win/loss, skewness.
8. **Walk-forward summary** — mean / median OOS Sharpe, % positive,
   per-fold table with **per-fold threshold** column emphasised, OOS
   equity curve, lifespan chart.
9. **Backtest grid** — net Sharpe and turnover heatmaps, with the IS
   grid headline showing **deflated** Sharpe alongside raw.
10. **Parameter stability** — Sharpe-vs-threshold curve, stability score.
11. **Temporal stability / alpha decay** — N≥5 guard; placeholder text
    when underpowered.
12. **Execution assumptions** — corrected to reflect § 6.

The detailed formulas (Sharpe, `N_eff`, DSR, turnover, alpha decay) live
in the **Methodology & Formulas** appendix page under
*Documentation*, not on the Signal Engine page itself. Inline
references are KaTeX-rendered tooltips that link to the appendix.

---

## 9. Code cross-reference

| Concept | Module | Function / class |
|---|---|---|
| Pipeline orchestrator | `app/research/signal/engine.py` | `run_signal_engine` |
| Z-score / threshold filter | `app/research/signal/standardize.py` | `compute_train_zscore`, `apply_threshold_filter` |
| Forward returns | `app/research/target.py` | `compute_15min_forward_return` |
| Backtest kernel | `app/research/signal/backtest.py` | `run_backtest`, `run_backtest_grid` |
| Walk-forward driver | `app/research/signal/walk_forward.py` | `run_walk_forward` |
| Effective sample size | `app/research/signal/diagnostics.py` | `compute_effective_sample_size` |
| Sharpe CI (Newey-West) | `app/research/signal/diagnostics.py` | `compute_sharpe_ci` |
| Deflated Sharpe | `app/research/signal/diagnostics.py` | `compute_deflated_sharpe` |
| Joint regime coverage | `app/research/signal/diagnostics.py` | `compute_joint_regime_coverage` |
| Regime gate | `app/research/signal/regime.py` | `compute_bar_regime_gate`, `compute_daily_regime_labels` |
| Graduation + Stage 0 | `app/research/signal/graduation.py` | `evaluate_graduation`, `_evaluate_stage0`, `_compute_stage_info` |
| Frontend orchestrator | `Frontend/src/app/components/research-lab/signal-runner/` | `SignalRunnerComponent` |
| Frontend report | `Frontend/src/app/components/research-lab/signal-report/` | `SignalReportComponent` |
| GraphQL types | `Frontend/src/app/services/research.service.ts` | `SignalEngineResult` |

---

## 10. Known limitations and roadmap

### Documented limitations (current state)

- **Slippage not modelled.** Cost is bps-per-side on turnover only. No
  spread model, no market-impact model. Realistic cost on AAPL intraday at
  > 200×/yr turnover is materially higher than the 1–5 bps grid suggests.
- **Single-asset validation.** The page operates on one ticker. Robustness
  across tickers is the Cross-Sectional sub-page; integration into
  graduation (Stage 2 entry) is roadmap.
- **No microstructure / trade-level analysis.** The backtest is a
  bar-stitched PnL series, not a trade ledger. Edge-decay-after-entry,
  trade-duration distribution, and per-trade PnL are not surfaced.
- **Alpha-decay regression underpowered for K_folds < 5.** Mitigated by
  the power guard (§ 4.9), which suppresses the panel rather than show a
  misleading p-value.
- **Execution at "next bar close" is a simplification.** See § 6.

### Roadmap (intentionally deferred)

- **Block-bootstrap Sharpe CI** preserving autocorrelation. Stage 2.
- **Cross-asset validation** wired into Stage 2 graduation.
- **Hansen SPA / White's Reality Check** on the IS grid. Stage 3.
- **Capacity / market-impact model.** Stage 3.
- **Trade-level analysis page.** Decoupled deliverable.

The deferral order reflects the principle in § 5: do not build Stage *N+1*
machinery until at least one signal reaches Stage *N*.

---

## 11. References

- Bailey, D. H. & López de Prado, M. (2014). *The Deflated Sharpe Ratio:
  Correcting for Selection Bias, Backtest Overfitting, and Non-Normality.*
  Journal of Portfolio Management, 40(5), 94–107.
- Lo, A. W. (2002). *The Statistics of Sharpe Ratios.* Financial Analysts
  Journal, 58(4), 36–52.
- Newey, W. K. & West, K. D. (1987). *A Simple, Positive Semi-Definite,
  Heteroskedasticity and Autocorrelation Consistent Covariance Matrix.*
  Econometrica, 55(3), 703–708.
- López de Prado, M. (2018). *Advances in Financial Machine Learning.*
  Wiley. Ch. 11–14 on backtest overfitting and walk-forward methodology.

Internal references:
- [`indicator-reliability-methodology.md`](indicator-reliability-methodology.md)
  — companion authority for the IC-based reliability page.
- [`architecture/options-math-authorities.md`](architecture/options-math-authorities.md)
  — pattern reference for "single-source-of-truth" docs.
- [`.claude/rules/numerical-rigor.md`](../.claude/rules/numerical-rigor.md)
  — repo-wide tolerance, timestamp, and equivalence policies.
