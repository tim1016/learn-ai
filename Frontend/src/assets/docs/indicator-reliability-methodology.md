# Indicator Reliability — Methodology, Metrics & UI Reference

> **Consolidated reference** for the Indicator Reliability feature. Covers the
> statistical methodology (IC, Newey–West, FDR, regime conditioning, IR proxy),
> the API contract, and the mission-control UI redesign (verdict hero,
> WHEN/WHERE/HOW decision cells, 5-test checklist), plus the global app-shell
> swap and the Research Lab information-architecture reorganisation.
>
> **Primary reader:** a future engineer on this repo. Assumes familiarity with
> time-series statistics and Angular 21 conventions. Cross-references
> [Frontend/CLAUDE.md](../Frontend/CLAUDE.md) and
> [PythonDataService/CLAUDE.md](../PythonDataService/CLAUDE.md) for house style.

---

## Table of contents

- [1. Context and scope](#1-context-and-scope)
- [2. Notation and glossary](#2-notation-and-glossary)
- [3. Statistical methodology](#3-statistical-methodology)
  - [3.1 Daily Information Coefficient](#31-daily-information-coefficient)
  - [3.2 Newey–West HAC-corrected statistics](#32-neweywest-hac-corrected-statistics)
  - [3.3 Effective sample size](#33-effective-sample-size)
  - [3.4 Multiple-testing correction](#34-multiple-testing-correction)
  - [3.5 Random-shuffle baseline](#35-random-shuffle-baseline)
  - [3.6 Stability metrics](#36-stability-metrics)
  - [3.7 Verdict labels](#37-verdict-labels)
  - [3.8 OOS retention delta](#38-oos-retention-delta)
  - [3.9 Slope decision flags](#39-slope-decision-flags)
  - [3.10 IC decay curve](#310-ic-decay-curve)
  - [3.11 Volatility regime conditioning](#311-volatility-regime-conditioning)
  - [3.12 IR proxy and tradeability](#312-ir-proxy-and-tradeability)
  - [3.13 Next-steps rule engine](#313-next-steps-rule-engine)
  - [3.14 Honesty footnotes](#314-honesty-footnotes)
- [4. API contract](#4-api-contract)
- [5. UI implementation](#5-ui-implementation)
  - [5.1 Information architecture](#51-information-architecture)
  - [5.2 App shell](#52-app-shell)
  - [5.3 Indicator Reliability page (mission control)](#53-indicator-reliability-page-mission-control)
  - [5.4 Research Lab sub-nav](#54-research-lab-sub-nav)
- [6. Code cross-reference](#6-code-cross-reference)
- [7. Verification plan](#7-verification-plan)
- [8. Limitations and future work](#8-limitations-and-future-work)
- [9. References](#9-references)

---

## 1. Context and scope

The Indicator Reliability feature answers two overlapping operator questions:

1. **Is this technical indicator statistically predictive on this asset?** —
   quantified by the daily rank-correlation Information Coefficient (IC) with
   Newey–West HAC-corrected inference, multiple-testing correction, and a
   random-shuffle baseline.

2. **Is it tradeable, and if not, what should I try next?** — quantified by an
   IC-to-IR proxy, regime conditioning, a hit-rate stability metric, and a
   rule-based next-steps engine.

> **Important scope.** The tool computes **time-series IC for a single asset** —
> the rank correlation between an indicator and its own forward return,
> aggregated across trading days. It is **not** the cross-sectional factor IC
> used in multi-asset factor models, where IC is the rank correlation across
> names at a single point in time. Thresholds and intuition from the factor
> literature do not transfer cleanly; the tradeability caveat (§3.12) calls out
> the specific assumptions that break.

The codebase splits across three services:

| Service | Stack | Role in this feature |
|---------|-------|----------------------|
| PythonDataService | FastAPI + pandas + scipy | IC computation, corrections, baseline, regime split, IR proxy |
| Backend | .NET + HotChocolate (GraphQL) | Not involved in this feature today |
| Frontend | Angular 21 + PrimeNG + Chart.js | Mission-control UI, app shell, navigation |

The backend statistics were shipped in three Python tranches (P1–P3 — hit-rate
and verdict labels; decay curve and regime conditioning; IR proxy, next-steps,
and honesty footnotes). The frontend redesign was then shipped in three UI
tranches (T1 — mission-control page; T2 — global left-sidebar shell; T3 —
Research Lab sub-nav). This document is organised by concept, not by tranche.

---

## 2. Notation and glossary

| Symbol | Meaning |
|--------|---------|
| $C_t$ | Close price at bar $t$ |
| $r_t^{(h)} = \ln(C_{t+h} / C_t)$ | $h$-bar forward log return; NaN where bars $t$ and $t+h$ straddle a session boundary |
| $f_t$ | Indicator value at bar $t$ |
| $d$ | A calendar date (session) |
| $\mathcal{B}_d$ | Set of bars belonging to date $d$ |
| $n_d = \lvert \mathcal{B}_d \rvert$ | Valid (non-NaN) feature/return pairs in day $d$ |
| $N$ | Number of days with a valid daily IC |
| $m$ | Number of horizons tested simultaneously (for multiple-testing correction) |
| $h$ | A forward horizon (in bars) |
| $K$ | Random-shuffle simulations per horizon (default 100) |
| $L$ | Bartlett-kernel bandwidth for Newey–West |
| $\rho_k$ | Lag-$k$ autocorrelation of the daily-IC series |
| $N_{\text{eff}}$ | Autocorrelation-adjusted effective sample size |
| $z_{\text{rand}}$ | Z-score of the actual IC versus the random-shuffle distribution |
| HR | Directional hit rate of daily ICs (see §3.6) |

All aggregation is performed on the **in-sample** (train) portion of the data
— a 70/30 chronological split. Out-of-sample (OOS) metrics use the held-out
30% with the same definitions; OOS never feeds into the baseline or the
regime split, by construction (§8.2).

---

## 3. Statistical methodology

### 3.1 Daily Information Coefficient

Implementation: [`validation/ic.py::compute_information_coefficient`](../PythonDataService/app/research/validation/ic.py).

For each day $d$ with $n_d \geq 5$ valid bars and both feature and return
standard deviations above $10^{-12}$ (a numerical floor to suppress degenerate
correlations), the daily IC is the Spearman rank correlation:

$$
IC_d \;=\; \rho_{\text{Spearman}}\!\left( \{f_t\}_{t \in \mathcal{B}_d},\; \{r_t^{(h)}\}_{t \in \mathcal{B}_d} \right)
$$

Days failing the checks are dropped. The aggregated IC is the arithmetic mean
across valid days:

$$
\overline{IC} = \frac{1}{N} \sum_{d=1}^{N} IC_d,
\qquad
s_{IC}^2 = \frac{1}{N-1} \sum_{d=1}^{N} (IC_d - \overline{IC})^2
$$

**Standard t-statistic** (independent-daily-ICs assumption — known to be
optimistic for intraday signals with carryover):

$$
t_{\text{std}} = \frac{\overline{IC}}{s_{IC} / \sqrt{N}},
\qquad
p_{\text{std}} = 2\,(1 - F_{t,\,N-1}(|t_{\text{std}}|))
$$

### 3.2 Newey–West HAC-corrected statistics

Because daily ICs exhibit positive serial correlation (persistent regimes,
overlapping signals, session-end effects), the standard t-stat overstates
significance. We apply a Newey–West (1987) HAC correction with a Bartlett
kernel.

**Bandwidth** — Andrews (1991) data-dependent rule:

$$
L = \max\!\left( 1,\; \left\lfloor 4\,(N/100)^{2/9} \right\rfloor \right),
\qquad L \leq N - 2
$$

**Autocovariances** use the ML (biased) divisor $N$ consistent with the HAC
estimator convention:

$$
\gamma_0 = \frac{1}{N} \sum_{d=1}^{N} (IC_d - \overline{IC})^2
$$

$$
\gamma_j = \frac{1}{N} \sum_{d=j+1}^{N} (IC_d - \overline{IC})(IC_{d-j} - \overline{IC}),
\qquad j = 1, \ldots, L
$$

**Long-run variance** with Bartlett kernel weights:

$$
\widehat{\sigma}^2_{NW} = \gamma_0 + 2 \sum_{j=1}^{L} \left( 1 - \frac{j}{L+1} \right) \gamma_j
$$

Degenerate series ($\widehat{\sigma}^2_{NW} \leq 10^{-20}$) return a zero
t-stat.

$$
t_{NW} = \frac{\overline{IC}}{\sqrt{\widehat{\sigma}^2_{NW} / N}},
\qquad
p_{NW} = 2\,\big(1 - F_{t, N-1}(|t_{NW}|)\big)
$$

### 3.3 Effective sample size

Same bandwidth as §3.2. Autocorrelation estimates:

$$
\rho_k = \frac{1}{N\,\gamma_0} \sum_{d=k+1}^{N} (IC_d - \overline{IC})(IC_{d-k} - \overline{IC})
$$

The effective sample size:

$$
N_{\text{eff}} = \frac{N}{1 + 2 \sum_{k=1}^{K^*} \rho_k},
\qquad
K^* = \min\{k : \rho_k < 0.05\} - 1 \;\text{(or } L \text{ if never)}
$$

The denominator is clamped to 1 so $N_{\text{eff}} \leq N$ always. Surfaced
per-horizon in the UI: when $N_{\text{eff}} \ll N$ the raw p-value is
over-confident.

### 3.4 Multiple-testing correction

Implementation: [`indicator_reliability.py::apply_multiple_testing_correction`](../PythonDataService/app/research/indicator_reliability.py).
Operates on the $m$ NW p-values across the tested horizons.

**Bonferroni:**

$$
p_i^{\text{Bonf}} = \min(p_i \cdot m,\; 1)
$$

**Benjamini–Hochberg FDR** (two-pass with monotonicity enforcement):

1. Sort ascending: $p_{(1)} \leq \ldots \leq p_{(m)}$.
2. $\tilde p_{(i)} = \min(1,\; p_{(i)} \cdot m / i)$.
3. Enforce monotonicity from the top: $\tilde p_{(i)} \leftarrow \min(\tilde p_{(i)}, \tilde p_{(i+1)})$.

The UI's verdict labels (§3.7) use the FDR-adjusted p-value for IS
significance; Bonferroni is surfaced alongside as the conservative check.

### 3.5 Random-shuffle baseline

Implementation: [`indicator_reliability.py::compute_random_baseline_ic`](../PythonDataService/app/research/indicator_reliability.py).
For $k = 1, \ldots, K$ (default $K = 100$):

1. Draw a permutation $\pi_k$ of $\{0, 1, \ldots, T-1\}$ where $T$ is the
   total bars in the IS period.
2. Replace the indicator with $\tilde f^{(k)}_t = \pi_k(t)$ — monotone within
   a bar index but uncorrelated with future returns across days by
   construction.
3. Compute $\overline{IC}^{(k)}$ through the standard pipeline (§3.1).

Let $\bar\mu = \mathrm{mean}_k\,\overline{IC}^{(k)}$ and
$\bar\sigma = \mathrm{std}_k\,\overline{IC}^{(k)}$ with a $10^{-10}$ floor.

**Z-score:**

$$
z_{\text{rand}} = \frac{\overline{IC}_{\text{actual}} - \bar\mu}{\bar\sigma}
$$

The full 100-value distribution $\{\overline{IC}^{(k)}\}$ is serialised on the
best-horizon result (payload-gated — see §4) and rendered as a 15-bin
histogram with the actual-IC bin highlighted.

**Interpretation.** $|z_{\text{rand}}| \geq 2$ is treated as "distinguishable
from noise." This is a heuristic, not a formal significance test — the
random-shuffle null destroys all temporal structure in the indicator, which is
stricter than the null an operator typically cares about.

### 3.6 Stability metrics

**Hit rate** — fraction of daily ICs whose sign matches the aggregate IC sign:

$$
\mathrm{HR} = \frac{1}{N} \sum_{d=1}^{N} \mathbf{1}\{\operatorname{sgn}(IC_d) = \operatorname{sgn}(\overline{IC})\}
$$

Deliberately **not** "fraction $IC_d > 0$" — for a mean-reverting signal with
$\overline{IC} < 0$ we want a high count of negative $IC_d$, not positive.
$\mathrm{HR}$ can fall below 0.5 when a handful of extreme days drag the
aggregate mean in the opposite direction of the median day — a red flag the
UI surfaces as low stability.

**Daily IC std** is $s_{IC}$ (§3.1), surfaced as a raw number. High $s_{IC}$
with a decent $\overline{IC}$ signals "the edge exists on average but is
unreliable day-to-day."

### 3.7 Verdict labels

Bucketed summaries computed on the best horizon (selected by
`find_best_horizon`: OOS significance preferred, FDR significance as fallback).
Implementation: `compute_strength_label`, `compute_stability_label`,
`compute_direction_label` in `indicator_reliability.py`.

**Strength** — $|IC|$ buckets (hard-coded thresholds calibrated for
time-series daily IC on liquid intraday equity data; re-tune per asset class):

$$
\text{strength}(|\overline{IC}|) =
\begin{cases}
\text{Strong}    & |\overline{IC}| \geq 0.12 \\
\text{Moderate}  & 0.07 \leq |\overline{IC}| < 0.12 \\
\text{Weak}      & 0.03 \leq |\overline{IC}| < 0.07 \\
\text{Noise}     & |\overline{IC}| < 0.03
\end{cases}
$$

**Stability** — hit-rate buckets:

$$
\text{stability}(\mathrm{HR}) =
\begin{cases}
\text{High}      & \mathrm{HR} \geq 0.58 \\
\text{Moderate}  & 0.52 \leq \mathrm{HR} < 0.58 \\
\text{Low}       & \mathrm{HR} < 0.52
\end{cases}
$$

**Direction** — signed IC mapped to semantics suitable for oscillators like
RSI and Stochastic. Raw-price / SMA-style indicators need an indicator-specific
label map (not yet implemented — see §8.4):

$$
\text{direction}(\overline{IC}) =
\begin{cases}
\text{Momentum}        & \overline{IC} > 0.02 \\
\text{Mean-Reversion}  & \overline{IC} < -0.02 \\
\text{None}            & |\overline{IC}| \leq 0.02
\end{cases}
$$

### 3.8 OOS retention delta

Replaces the confusing legacy "124% retention ratio" display. Percentage
change in $|IC|$ magnitude from IS to OOS:

$$
\Delta_{\text{OOS/IS}} = \left( \frac{|\overline{IC}_{\text{OOS}}|}{|\overline{IC}_{\text{IS}}|} - 1 \right) \times 100\%
$$

Undefined (`null`) when $|\overline{IC}_{\text{IS}}| < 10^{-10}$ or OOS data
is absent. Positive values imply OOS **stronger** than IS (suspicious — either
a favourable regime shift or small-sample noise); negative values imply
degradation.

**Known gap.** The delta does not detect sign flips: IS $= +0.08$ vs
OOS $= -0.08$ yields $\Delta = 0\%$. The front-end can derive a sign-flip flag
via $\operatorname{sgn}(\overline{IC}_{\text{IS}}) \cdot \operatorname{sgn}(\overline{IC}_{\text{OOS}}) < 0$ —
this is not yet wired into the UI (§8.4).

### 3.9 Slope decision flags

Computed on the slope variant (IC of $\Delta f_t = f_t - f_{t-1}$ versus
forward return) and paired against the raw variant by horizon in the router
(`compute_slope_decisions`).

$$
\text{adds\_value} =
\begin{cases}
|\overline{IC}^{\text{slope}}| > 0.02 &
  \text{if } |\overline{IC}^{\text{raw}}| < 10^{-10} \\[4pt]
\bigl(|\overline{IC}^{\text{slope}}| > 1.20 \cdot |\overline{IC}^{\text{raw}}|\bigr)
  \land \bigl(p^{\text{slope}}_{\text{FDR}} < p^{\text{raw}}_{\text{FDR}}\bigr) &
  \text{otherwise}
\end{cases}
$$

$$
\text{recommended} = \text{adds\_value} \;\land\;
\bigl(p^{\text{slope}}_{\text{oos}} < 0.10 \;\lor\; \text{retention}^{\text{slope}}_{\text{oos}} \geq 0.60\bigr)
$$

`recommended` is `null` when OOS data is unavailable — we refuse to recommend
what hasn't been validated.

### 3.10 IC decay curve

Single-pass diagnostic of IC vs horizon. For every integer $h \in [1, H_{\max}]$
where $H_{\max} = \min(\max(\text{requested horizons}) + 10,\; 60)$:

1. Recompute $r_t^{(h)}$ on the IS period.
2. Run the full daily-IC aggregation (§3.1) — no correction, no baseline.
3. Derive a standard error for the 95% confidence band:

$$
\text{SE}(h) =
\begin{cases}
\left| \overline{IC}(h) / t_{NW}(h) \right| & |t_{NW}(h)| > 10^{-10} \\[4pt]
s_{IC}(h) / \sqrt{\max(N_{\text{eff}}(h),\, 1)} & \text{otherwise}
\end{cases}
$$

Chart renders $\overline{IC}(h) \pm 1.96\cdot\text{SE}(h)$ with the peak
$\mathrm{argmax}_h |\overline{IC}(h)|$ flagged by an amber marker.

**Deliberate choice.** No multiple-testing correction is applied to the decay
curve — it is visualisation of signal structure, not a significance test. The
rigorous test lives in the main results set.

### 3.11 Volatility regime conditioning

Answers "when does the signal work?" rather than "does it work on average?"

**Rolling realized volatility** on IS close prices with window $w = 20$ bars:

$$
\sigma_t = \operatorname{std}\!\left( \{\ln(C_s / C_{s-1}) : s \in (t - w, \ldots, t]\} \right)
$$

Bars in the warmup ($t < w$, $\sigma_t = \mathrm{NaN}$) are excluded from both
regimes.

**Regime masks** — IS median split:

$$
\tilde\sigma = \operatorname{median}\!\bigl(\{\sigma_t : \sigma_t \text{ defined}\}\bigr)
$$

$$
\mathcal{H} = \{t : \sigma_t > \tilde\sigma\},
\qquad
\mathcal{L} = \{t : \sigma_t \leq \tilde\sigma, \; \sigma_t \text{ defined}\}
$$

**Per-regime IC.** Critically, forward returns are computed on the
**full training series** *before* masking, then indexed by the regime mask:

$$
\overline{IC}_{\mathcal{R}}(h) = \text{daily-aggregated IC on } \{(f_t, r_t^{(h)}) : t \in \mathcal{R}\}
$$

This preserves the wall-clock meaning of $h$ — it is always $h$ bars ahead in
real time, regardless of whether the intervening bars happened to be
in-regime. Masking *after* computing $r_t^{(h)}$ would have silently changed
the semantics of the horizon inside each regime.

Buckets smaller than `MIN_REGIME_BARS = 50` return `null`. The UI renders
"Not enough bars in this regime" in that cell.

**Limitation — look-ahead.** The median is computed ex-post on the full IS
series. This is fine for the research question ("in which regimes does this
work?") but **not valid for live trading** — a real-time filter would use a
rolling median known at decision time (§8.2).

### 3.12 IR proxy and tradeability

**Bars per trading year.** Computed from the Polygon `timespan + multiplier`
pair:

```
bars_per_year(timespan, multiplier) =
    252 * (bars_per_trading_day[timespan] / multiplier)
```

| timespan | bars/day |
|----------|----------|
| minute   | 390      |
| hour     | 6.5      |
| day      | 1        |
| week     | 0.2      |
| month    | 1/21     |

**IR proxy via breadth** — Grinold (1989) / Grinold & Kahn (1999):

$$
\text{breadth}_{\text{year}} = \max\!\left( \frac{\text{bars}_{\text{year}}}{h},\; 1 \right)
$$

$$
\text{IR}_{\text{annual}} \approx \overline{IC} \cdot \sqrt{\text{breadth}_{\text{year}}}
$$

$$
\text{Sharpe}_{\text{proxy}} = \text{IR}_{\text{annual}}
$$

under unit-volatility, zero-cost assumptions.

**Tradeability bucketing** (uses absolute Sharpe so a negative-IC signal —
i.e. short the indicator — is treated symmetrically):

$$
\text{tradeability}(s, \text{stab}) =
\begin{cases}
\text{Likely tradeable}  & |s| \geq 1.0 \;\land\; \text{stab} = \text{High} \\
\text{Marginal}          & 0.5 \leq |s| < 1.0 \;\lor\; (|s| \geq 1.0 \land \text{stab} \neq \text{High}) \\
\text{Unlikely}          & |s| < 0.5
\end{cases}
$$

**Caveats** — serialised in the response as `tradeability_caveat` and
surfaced in the UI verdict chip tooltip:

1. **Independent bets.** Overlapping forward returns share bars;
   $\text{breadth} = \text{bars}/h$ overstates independent observations. True
   effective breadth is closer to $N_{\text{eff,year}}$ (§3.3 scaled to a year),
   typically much smaller.
2. **Unit volatility.** The formula assumes the signal-weighted return has
   unit vol — i.e. position sizing is variance-normalised. Real portfolios
   rarely achieve this.
3. **Zero transaction costs.** A horizon-$h$ strategy trades every $h$ bars;
   a $\text{Sharpe}_{\text{proxy}} = 2$ on 1-minute bars can collapse to zero
   under realistic costs.

The UI labels this a **proxy**, not a tradeable Sharpe estimate.

### 3.13 Next-steps rule engine

`generate_next_steps` produces up to 4 suggestions, evaluated in the order
below. The first matching rule in each conceptual group fires.

| # | Condition | Suggestion |
|---|-----------|------------|
| 1 | $\overline{IC}_{\text{OOS}}$ is `None` | "Collect more out-of-sample data before trading — current result is in-sample only." |
| 2 | $\lvert\overline{IC}_{\text{high\_vol}}\rvert \geq 0.03$ and $\geq 2\lvert\overline{IC}_{\text{low\_vol}}\rvert$ | "Add a volatility filter — signal is materially stronger in high-vol regimes." |
| 3 | Symmetric case favouring low-vol | "Add a low-vol filter — signal degrades sharply in high-vol regimes." |
| 4 | stability = Low and $\lvert\overline{IC}_{\text{IS}}\rvert \geq 0.03$ | "Try a longer horizon — signal has directional edge but is noisy at the current horizon." |
| 5 | Slope `adds_value` = True on the best horizon | "Try the slope variant — the indicator's rate of change is stronger than its raw value." |
| 6 | strength $\in$ {Moderate, Strong}, stability = High, $p_{\text{OOS}} < 0.10$ | "Consider a threshold-based strategy and measure realized Sharpe with transaction costs." |

**Fall-through** (no rule fired):

- If strength $\in$ {Moderate, Strong} and ($p_{\text{OOS}}$ is `None` or $\geq 0.10$):
  "IS edge did not validate out-of-sample — try a longer date range or different parameters before trading."
- Else: "Signal looks noise-like; consider a different indicator, parameter sweep, or longer window."

### 3.14 Honesty footnotes

Always-present soft reminders rendered as muted text (`info_footnotes`).
These are *not* warning-severity to avoid alarm fatigue while keeping
limitations visible:

1. **Always** — "Single-asset IC — portfolio IC across many tickers may differ substantially."
2. **Always** — "Time-series IC is not the same as cross-sectional factor IC."
3. **If any horizon $> 1$** — "Overlapping forward returns inflate raw significance; NW-adjusted stats are shown where possible."

---

## 4. API contract

`POST /api/research/indicator-reliability` — the single endpoint driving the
page. Request body:

```
ticker:           string        # e.g. "AAPL"
indicator_name:   string        # pandas-ta name, e.g. "rsi"
indicator_params: object        # e.g. { length: 14 }
start_date:       string        # YYYY-MM-DD (IS+OOS window)
end_date:         string
horizons:         int[]         # e.g. [1, 5, 10, 15, 30]
include_slope:    bool          # optional slope variant
timespan:         string        # "minute" | "hour" | "day" | ...
multiplier:       int           # Polygon bar multiplier
```

Response `IndicatorReliabilityResponse`:

```
# Echo + split metadata
success, ticker, indicator_name, display_name, category
start_date, end_date, bar_count
train_start, train_end, test_start, test_end
train_bars, test_bars, train_ratio

# Per-horizon results
results:       HorizonICResult[]         # one per requested horizon
slope_results: HorizonICResult[] | null  # present iff include_slope

# Daily IC series for the best-horizon sparkline
daily_ic_values: float[]
daily_ic_dates:  string[]

# Summary + rollups
best_horizon:                          int | null
any_significant_after_bonferroni:      bool
any_significant_after_fdr:             bool
num_horizons_tested:                   int
random_simulations:                    int  # == K (default 100)

# Verdict + diagnostics
verdict:          VerdictModel | null       # §3.7 + §3.12
decay_curve:      DecayCurvePoint[]         # §3.10
regime_results:   RegimeResults | null      # §3.11
next_steps:       string[]                  # §3.13 (len ≤ 4)
info_footnotes:   string[]                  # §3.14

warnings:  string[]
error:     string | null
```

**`HorizonICResult`** — one row per tested horizon:

```
horizon: int

# In-sample
is_mean_ic, is_t_stat, is_p_value:   float
is_nw_t_stat, is_nw_p_value:         float | null
is_effective_n:                       int
is_hit_rate, is_daily_ic_std:         float

# Out-of-sample (null if test_bars < MIN_OOS_OBSERVATIONS = 10)
oos_mean_ic, oos_t_stat, oos_p_value: float | null
oos_effective_n:                       int   | null
oos_retention:                         float | null   # legacy ratio
retention_delta_pct:                   float | null   # §3.8

# Multiple testing
bonferroni_p, fdr_p: float

# Random baseline
random_baseline_mean, random_baseline_std, ic_vs_random_zscore: float
random_baseline_distribution: float[]   # populated only for best horizon

# Verdict labels
strength_label:  "Noise" | "Weak" | "Moderate" | "Strong"
stability_label: "Low" | "Moderate" | "High"
direction_label: "Mean-Reversion" | "Momentum" | "None"

# IR proxy
annualized_ir, sharpe_estimate, breadth_per_year: float

# Slope decisions (populated only on slope_results rows)
slope_adds_value, slope_recommended: bool | null

# Legacy free-text
is_interpretation, oos_interpretation: string | null
```

**`VerdictModel`**:

```
direction:             DirectionLabel
strength:              StrengthLabel
stability:             StabilityLabel
tradeability:          "Likely tradeable" | "Marginal" | "Unlikely" | "Unknown"
horizon:               int | null
tradeability_caveat:   string | null
```

**`DecayCurvePoint`** — one per horizon on the decay curve:

```
horizon:   int
ic:        float
p_value:   float
ic_stderr: float
```

**`RegimeResults`**:

```
high_vol:   RegimeICPoint[] | null
low_vol:    RegimeICPoint[] | null
vol_window: int                   # 20 by default
```

**`RegimeICPoint`**:

```
horizon, effective_n, bars_in_regime: int
mean_ic, t_stat, p_value, hit_rate:    float
```

**Payload gating.** The 100-element `random_baseline_distribution` array is
sent only on the best-horizon row to keep the response compact.
`_to_horizon_ic_result(r, include_distribution=…)` in
[routers/indicator_reliability.py](../PythonDataService/app/routers/indicator_reliability.py)
is the gate.

---

## 5. UI implementation

The frontend is Angular 21 (standalone components, `OnPush`, signals,
`@if`/`@for`/`@switch` control flow). The codebase does not use class-based
state or `NgModules` — see [Frontend/CLAUDE.md](../Frontend/CLAUDE.md) for the
full conventions. This section documents three concurrent refactors:

- **T1** — the Indicator Reliability page, redesigned as a mission-control
  surface with a confidence gauge, WHEN/WHERE/HOW decision cells, and a
  5-test checklist (§5.3).
- **T2** — the global app shell. Previous top PrimeNG Menubar is replaced by a
  persistent 240-px left sidebar (`AppSidebarComponent`) and the 1200/1400-px
  page-container caps are removed across 14 files (§5.2).
- **T3** — the Research Lab landing page re-rendered as three grouped sub-nav
  sections (Validate / Inspect / Reference) backed by a signal-driven
  `@switch` instead of the previous PrimeNG Tabs (§5.4).

The redesign originated from a Claude Design bundle
(`quant-trading-lab-design-system`). The variant chosen was *Variant B —
Mission-control* (confidence-gauge-led) rather than *Variant A —
Bloomberg-dense*. The variant choice was locked in before implementation and
is not revisited here.

### 5.1 Information architecture

#### 5.1.1 Before

The legacy top Menubar exposed **7 groups** with up to 13 flat sub-items each,
for ~30+ routes total. Structure extracted from the previous
`app.component.ts`:

| Legacy group | Representative items |
|--------------|----------------------|
| Stocks (1 of 7) | Market Data, Tickers, Technical Analysis, Stock Analysis, Snapshots, Strategy Lab *(deprecated)*, Strategy Validation, Strategy Docs, Indicator Validation, Indicator Docs, Indicator Report, Data Lab, Data Lab Docs |
| Data Quality | Quality Analysis, Pipeline Docs |
| Options | Options Chain, Strategy Builder, Options Strategy Lab, Options History, Pricing Lab, Snapshots |
| Engine | Engine Lab, Engine Docs |
| Portfolio | single route |
| Research Lab | single route |
| Tracked Instruments | single route |

**Problems identified** in the design chat: no persistent context, no search,
cognitive load spiked at Stocks (13 items), and several sections overlapped
conceptually (Data Lab appeared under Stocks; Data Quality was its own group).

#### 5.1.2 After — five-group sidebar IA

Adapted from the Claude Design bundle's proposed IA, with minor adjustments
to preserve every existing route. The single authoritative declaration is in
[app-sidebar.component.ts](../Frontend/src/app/shell/app-sidebar.component.ts)
at the top-of-file `NAV` constant.

| Sidebar group | PrimeIcon | Items |
|---------------|-----------|-------|
| **Stocks** | `pi-chart-line` | Market Data, Tickers, Technical Analysis, Stock Analysis, Snapshots, Strategy Lab *(deprecated)*, Strategy Validation, Strategy Docs, Indicator Validation, Indicator Docs, Indicator Report |
| **Data Lab** | `pi-database` | Data Lab, Indicator Reference, Data Quality, Pipeline Docs |
| **Options** | `pi-objects-column` | Options Chain, Strategy Builder, Options Strategy Lab, Options History, Pricing Lab |
| **Research Lab** | `pi-search` | Research Lab |
| **Portfolio** | `pi-wallet` | Dashboard, Engine Lab, Tracked Instruments |

#### 5.1.3 Mapping rationale

- **Data Lab ← Data Quality + Data Lab items.** The two Data Quality routes
  conceptually belong with the other data-inspection pages. Folding collapses
  a single-level group into a denser, more coherent one.
- **Portfolio ← Portfolio + Engine + Tracked Instruments.** Engine Lab is
  a backtester producing portfolio-level results; Tracked Instruments is a
  per-position watchlist. Both are portfolio-adjacent; treating them as
  separate top-level groups fragmented attention.
- **Options stays.** Six routes, all tightly coupled, natural group.
- **Stocks stays.** Eleven routes kept together because they share market-data
  plumbing and Strategy/Indicator workflows. A nested sub-group inside Stocks
  for "Strategy" and "Indicator" was considered and rejected for v1 —
  two-level nav adds complexity without a matching user-benefit until the
  Stocks set grows further.
- **Research Lab kept as a single item** that opens a dedicated landing
  page (§5.4). The 11 sub-surfaces live *inside* that page via the sub-nav,
  not in the sidebar. This is an intentional design decision to keep the
  sidebar depth shallow and to treat Research Lab as a "section" in the same
  sense that Options is a section.

#### 5.1.4 Route preservation invariants

Every existing route (`Frontend/src/app/app.routes.ts`) is reachable via the
new sidebar. No route was renamed, redirected, or removed. The IA rework is
purely a re-grouping on top of unchanged URL space. This is the key safety
property: deep-links and bookmarks continue to work.

### 5.2 App shell

`AppComponent` — [app.component.ts](../Frontend/src/app/app.component.ts):

```
<app-sidebar />
<main class="main">
  <router-outlet />
</main>
```

The host is `display: flex; min-height: 100vh` — sidebar is a flex-child with
`flex-shrink: 0`, main is `flex: 1; min-width: 0`. The `min-width: 0` is
load-bearing: without it, wide children (e.g. data tables) would overflow
horizontally instead of scrolling.

The global `.page-container { max-width: 1200px }` wrapper is removed.

#### 5.2.1 AppSidebarComponent architecture

[app-sidebar.component.ts](../Frontend/src/app/shell/app-sidebar.component.ts).
OnPush, standalone, signals-only. Key internal state:

| Signal | Type | Purpose |
|--------|------|---------|
| `currentUrl` | `string` | Current route URL. Updated on `NavigationEnd` from Angular Router. Used by the `isActive` / `groupHasActive` / `groupContainsUrl` predicates. |
| `openGroups` | `Record<string, boolean>` | Open/closed state per group. Initial value auto-opens the group containing the landing route. Subsequent `NavigationEnd` events auto-expand the group of the newly active route (never collapse others — user's manual toggles are preserved). |
| `query` | `string` | Search query string. Non-empty enters flat-match mode. |
| `filtered` | `computed(...)` | When `query` is non-empty, flattens all items across groups into a single list filtered by case-insensitive substring match on `"{label} {group}"`. Returns `null` to signal "render the grouped tree." |

#### 5.2.2 Active-route detection

`isActive(route)` returns true when `currentUrl === route` or
`currentUrl.startsWith(route + '/')`. The prefix check is what makes
`/research-lab/signal-report/:id` highlight the **Research Lab** parent item.

#### 5.2.3 Search and ⌘K binding

The ⌘K / Ctrl+K hotkey is wired via `@HostListener('window:keydown', …)`:

```typescript
@HostListener('window:keydown', ['$event'])
handleKeydown(event: KeyboardEvent): void {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
    event.preventDefault();
    const el = this.searchInput()?.nativeElement;
    if (el) { el.focus(); el.select(); }
  }
}
```

When `query` is non-empty, the template renders a flat list of matches
(with the originating group name rendered in monospace next to each match).
Clicking a match clears the query and navigates.

#### 5.2.4 Width-cap removal

Thirteen component files (plus the IR page from T1) previously capped their
content at 1200 or 1400 pixels via a combination of `max-width` and
`margin: 0 auto`. All were changed to `max-width: none` with the
centering margin removed. A future engineer adding a new page should not
re-introduce width caps without an explicit reason — the left sidebar already
provides horizontal context; centered narrow columns on a dense research tool
waste screen real estate.

Files modified (line numbers in each file's initial `@use` block):

- `options-strategy-lab/*.scss`, `lean-engine/*.scss`, `data-lab/*.scss`,
  `portfolio/*.scss`, `technical-analysis/*.scss`, `tickers.component.ts`
  (inline styles), `stock-analysis/*.scss` (+ chunk-detail + day-detail),
  `market-data/*.scss`, `strategy-lab/*.scss`, `ticker-explorer/*.scss`,
  `tracked-instruments/*.scss`. The IR page's cap was removed as part of T1.

### 5.3 Indicator Reliability page (mission control)

[indicator-reliability.component.ts / .html / .scss](../Frontend/src/app/components/research-lab/indicator-reliability).

#### 5.3.1 Layout anatomy

Single component, no sub-components — all structural pieces are inlined in
the template for co-location with their helpers. Top-to-bottom:

```
┌───────────────────────────────────────────────────────────────┐
│ Title                                           [New analysis] │
├───────────────────────────────────────────────────────────────┤
│  Controls panel (form; ticker / indicator / dates / params)   │
├───────────────────────────────────────────────────────────────┤
│  Collapsed summary band (shown post-run)                      │
├───────────────────────────────────────────────────────────────┤
│  Warnings (p-message severity="warn")                         │
├───────────────────────────────────────────────────────────────┤
│  HERO                                                         │
│  ┌────────────┬──────────────────────────┬────────────────┐   │
│  │ Confidence │ Headline                 │ 3 stacked      │   │
│  │ gauge      │ Description              │ action CTAs    │   │
│  │ (SVG arc)  │ Reason pills             │                │   │
│  └────────────┴──────────────────────────┴────────────────┘   │
├───────────────────────────────────────────────────────────────┤
│  DECISION CELLS — WHEN / WHERE / HOW                          │
│  ┌────────────┬──────────────────────────┬────────────────┐   │
│  │ 01 When    │ 02 Where                 │ 03 How         │   │
│  └────────────┴──────────────────────────┴────────────────┘   │
├───────────────────────────────────────────────────────────────┤
│  CONTENT GRID (1.4 fr / 1 fr)                                 │
│  ┌────────────────────────────┬─────────────────────────────┐ │
│  │ IC-vs-horizon decay chart  │ 5-test decision checklist   │ │
│  │  + horizon compact cards   │                             │ │
│  │                            │ Random-baseline noise-floor │ │
│  │ Regime cross-check panel   │  bar + histogram            │ │
│  │                            │                             │ │
│  │                            │ Daily IC sparkline          │ │
│  └────────────────────────────┴─────────────────────────────┘ │
├───────────────────────────────────────────────────────────────┤
│  Suggested next steps (bulleted)                              │
├───────────────────────────────────────────────────────────────┤
│  Slope variant table (collapsed <details>)                    │
├───────────────────────────────────────────────────────────────┤
│  Methodology & caveats (collapsed <details>)                  │
└───────────────────────────────────────────────────────────────┘
```

Below 1100 px, the hero collapses to 2-column (gauge + copy), with the action
column wrapping to the next row. Below 900 px, the decision cells stack. Below
1100 px, the content grid stacks.

#### 5.3.2 Confidence score

The scalar summary driving the gauge. Mirrors the Claude Design bundle's
formula. Five binary tests, 20 points each:

```
confidence =
    20 * 1[fdr_significant]
  + 20 * 1[bonferroni_significant]
  + 20 * 1[oos_holds]
  + 20 * 1[|z_rand| > 3]
  + icPartialScore
```

where:

- `oos_holds := retention_delta_pct != null AND (retention_delta_pct >= -30 OR retention_delta_pct > 0)`
- `icPartialScore = 20 if |best_ic| > 0.10 else 10 if |best_ic| > 0 else 0`

The IC component has partial credit to reward "real but small" signals instead
of punishing them to zero.

**Bucket thresholds** — same as the design bundle:

$$
\text{bucket}(s) = \begin{cases}
\text{TRADE}        & s \geq 85 \\
\text{INVESTIGATE}  & 60 \leq s < 85 \\
\text{REJECT}       & s < 60
\end{cases}
$$

**Verbs** driving the hero headline colour + copy:

| Bucket | Verb | Colour |
|--------|------|--------|
| TRADE | "Ready to trade" | `--bull` |
| INVESTIGATE | "Investigate further" | `--warn` |
| REJECT | "Do not trade" | `--bear` |

Implementation: `computeConfidence`, `getConfidenceBucket`, `getConfidenceColor`,
`getVerdictVerb` in [indicator-reliability.component.ts](../Frontend/src/app/components/research-lab/indicator-reliability/indicator-reliability.component.ts).

#### 5.3.3 Gauge SVG math

The gauge is a single SVG element with two concentric stroked circles, each
showing a 3/4-arc (unfilled background track + filled colour arc). Geometry:

```
R = 90
C = 2πR  ≈  565.487
track_arc_length = C × 0.75  ≈  424.115      // unfilled background (a 3/4 circle)
filled_arc_length = (score / 100) × track_arc_length
```

Both circles share `cx="110"`, `cy="110"`, `r="90"`, `stroke-linecap="round"`,
and `transform="rotate(135 110 110)"`. The rotation positions the arc's open
gap at the bottom (dial pointing south). The dash pattern
`stroke-dasharray="{length} {C}"` paints the first `{length}` pixels of
circumference then leaves the rest blank. Expressed as a signal helper:

```typescript
getGaugeDash(): { filled: number; full: number; circumference: number } {
  const R = 90;
  const C = 2 * Math.PI * R;
  const arc = C * 0.75;
  const filled = (this.getConfidenceScore() / 100) * arc;
  return { filled, full: arc, circumference: C };
}
```

The coloured arc also receives a `filter: drop-shadow(0 0 6px currentColor)`
for a subtle glow matching the verdict bucket colour.

#### 5.3.4 Reason pills

The reason pills are ordered pass/fail chips driven directly by the response.
Implementation: `getReasonPills()`. Ordered:

1. FDR ✓ / ✗
2. Bonferroni ✓ / ✗
3. `OOS holds ({±retention_delta_pct}%)` — `good` if $\geq -30\%$, else `warn`
4. `|IC| {val} {> 0.10 | ≤ 0.10}` — `good` iff $> 0.10$
5. `Stronger in {regime}` — appended if `getRegimeComparison()` returns a
   regime that dominates at the best horizon
6. `Single asset only` — always appended, kind = `neutral`

Colours (all applied via a `data-kind` attribute selector on the `.reason-pill`
class):

- `good` — `--bull` / `rgba(bull, 0.12)` background / `rgba(bull, 0.3)` border
- `warn` — `--warn` analogues
- `neutral` — `--text-muted` analogues

#### 5.3.5 WHEN cell

Implementation: `getWhenCell()`. Always returns `Hold {best_horizon}-bar` as
the answer. The detail is derived from `decay_curve`:

- Find $h^\star = \mathrm{argmax}_h\,|\overline{IC}(h)|$ (the decay-curve peak).
- Detail: `IC peaks at {h*}-bar ({ic(h*).toFixed(3)}), {decay_characterisation} after.`
- Decay characterisation:
  - `decays slowly` if $|\overline{IC}(h^\star)| > 0.10$
  - `fades quickly` otherwise

When no decay curve is present, falls back to *"Best horizon by OOS
significance."*

#### 5.3.6 WHERE cell

Implementation: `getRegimeComparison()` + `getWhereCell()`.

At the best horizon $h^\star$, compare the absolute per-regime ICs:

$$
\text{stronger} = \begin{cases}
\text{high-vol regimes} & |\overline{IC}_{\mathcal{H}}(h^\star)| \geq |\overline{IC}_{\mathcal{L}}(h^\star)| \\
\text{low-vol regimes}  & \text{otherwise}
\end{cases}
$$

$$
\Delta = \frac{|\,|\overline{IC}_{\mathcal{H}}(h^\star)| - |\overline{IC}_{\mathcal{L}}(h^\star)|\,|}{\min(|\overline{IC}_{\mathcal{H}}|, |\overline{IC}_{\mathcal{L}}|)} \times 100
$$

The answer is the stronger-regime label. The detail concatenates the percent
delta, both raw ICs, and both hit-rates.

If `regime_results` or either regime bucket is missing (i.e. fewer than 50
bars per bucket — §3.11), the WHERE cell renders "No regime split" and a
detail explaining the data shortfall.

#### 5.3.7 HOW cell

Implementation: `getHowCell()`. Maps `direction_label` to an answer string:

| direction_label | Answer |
|-----------------|--------|
| Mean-Reversion | "Fade extremes" |
| Momentum | "Follow the move" |
| None | "No clear edge" |

Detail always includes the Sharpe proxy (formatted via `formatSharpe`) and a
one-line sizing instruction appropriate for the direction (e.g. for
mean-reversion: *"Short when indicator is high, long when low."*). Closes
with `Test with costs before sizing.` — a nudge toward pre-flight.

#### 5.3.8 Five-test decision checklist

Implementation: `getChecklist()`. Produces five `{pass, label, detail}` rows
in the right-hand panel:

| # | Test | Pass condition | Detail |
|---|------|----------------|--------|
| 1 | FDR significance | `any_significant_after_fdr` | `p < 0.05 at {k}/{m} horizons` |
| 2 | Bonferroni (conservative) | `any_significant_after_bonferroni` | `Passes the strictest correction` or `Fails strictest correction` |
| 3 | Out-of-sample holds | `retention_delta_pct` not null AND $\geq -40$ OR $> 0$ | Shows the OOS IC / IS IC / retention delta |
| 4 | Beats random | $\lvert z_{\text{rand}} \rvert > 3$ | `{z.toFixed(1)}σ above noise floor` |
| 5 | Economically meaningful | $\lvert IC \rvert > 0.10$ | `|IC| {val} {>|≤} 0.10 threshold` |

A ✓ (green) or ✗ (red) circular badge is rendered per row. The threshold for
"OOS holds" is `-40%`, intentionally looser than the checklist's scoring
counterpart in the confidence gauge (`-30%`). The rationale: the checklist is
an eyeball-level indicator of risk; the gauge is a summary score. A signal that
degrades $30$–$40\%$ out-of-sample should show as "still holds" in the
checklist (it's not obviously broken) while docking confidence points from the
gauge (you should be less certain).

#### 5.3.9 Noise-floor bar

Implementation: `getNoiseFloorBar()`. Visualises the actual best IC against a
$\pm 1\sigma$ band around the random-shuffle mean.

Let $\mu = \mathrm{random\_baseline\_mean}$, $\sigma = \mathrm{random\_baseline\_std}$.
Build a domain $[\mu - 4\sigma, \mu + 4\sigma]$ and map positions to a $[0, 100]$
percent axis:

```
span        = 8σ
bandLeftPct = ((μ − σ) − (μ − 4σ)) / span × 100  = 37.5
bandWidthPct= 2σ / span × 100                     = 25
icRaw       = ((IC_actual − (μ − 4σ)) / span) × 100
icPct       = clamp(icRaw, 2, 98)                 // keep marker on-bar
```

The centre of the bar (50%) corresponds to the random mean $\mu$. A thin
vertical line marks the centre. The band is `rgba(90,97,120,0.3)`. The actual
IC is a glowing marker, coloured by sign (green for positive, red for
negative). A monospace label above the marker shows the IC to three decimal
places.

When $\sigma < 10^{-10}$ (degenerate), the bar is skipped and the histogram
below still renders.

#### 5.3.10 Action CTAs

Three stacked buttons in the hero action column. Only **Run on another ticker**
is functionally wired in v1:

- **Send to Pre-flight** — primary style, `disabled`. Tooltip: *"Pre-flight
  check not wired in v1."* Awaits a Strategy Pre-flight backend.
- **Save to tested indicators** — ghost style, `disabled`. Tooltip: *"Save to
  tested indicators not wired in v1."* Awaits a persistence table.
- **Run on another ticker** — functional. `runOnAnotherTicker()` opens
  `window.prompt(...)`, pre-fills with the current ticker, and — if a new,
  cleaned-up ticker is returned — updates the `ticker` signal and calls the
  existing `runAnalysis()`. `window.prompt` is the minimum viable UX; a PrimeNG
  dialog is a follow-up (§8.4).

### 5.4 Research Lab sub-nav

[research-lab.component.ts / .html / .scss](../Frontend/src/app/components/research-lab).

#### 5.4.1 Migration from PrimeNG Tabs

The previous implementation used `p-tabs` with 11 tabs, keyed by a hard-coded
numeric `value`. The redesign replaces this with a signal-driven pattern:

```typescript
readonly active = signal<TabId>('indicator-reliability');
setActive(id: TabId): void { this.active.set(id); }
```

And the template renders via `@switch`:

```html
@switch (active()) {
  @case ('indicator-reliability') { <app-indicator-reliability /> }
  @case ('feature-runner')        { <app-feature-runner /> }
  …
}
```

Rationale: PrimeNG Tabs does not support visual sub-grouping of tabs within a
single tablist (eyebrow labels across the tab strip), which is the design's
intent. A signal + `@switch` gives total control over layout at the cost of
losing the keyboard semantics of `role="tablist"` (§8.2 — an a11y follow-up).

#### 5.4.2 Three-group layout

Horizontal sub-nav with three labelled meta-sections:

| Meta-section | Items |
|--------------|-------|
| **Validate** | Feature Runner, Indicator Reliability, Signal Engine |
| **Inspect** | Cross-Sectional, Data Divergence, Pre-flight Check |
| **Reference** | Experiments, Options Math, Signal Docs, Signal History, Feature Docs |

Each section header is an uppercase monospace eyebrow at 10 px / 0.06 em
letter-spacing. Tabs are click targets; the active tab gets an `--accent`
bottom border and bumped font-weight. Sections are separated by a 14-px
vertical divider.

#### 5.4.3 Default landing tab

Previously: Feature Runner (tab index 0). Now: **Indicator Reliability**.
This is a deliberate behaviour change to match the design-bundle intent —
Indicator Reliability is the showcase page for the mission-control redesign
and the most-used surface in the section.

Users who bookmarked `/research-lab` expecting Feature Runner will now land
on Indicator Reliability. The sub-nav remembers no state between page loads
(§8.2).

---

## 6. Code cross-reference

### 6.1 Backend (Python)

| Concept | File | Symbol |
|---------|------|--------|
| Daily IC + NW + N_eff | [validation/ic.py](../PythonDataService/app/research/validation/ic.py) | `compute_information_coefficient`, `_compute_newey_west_t_stat`, `_compute_effective_sample_size` |
| Hit rate | same | `_compute_hit_rate` |
| FDR / Bonferroni | [indicator_reliability.py](../PythonDataService/app/research/indicator_reliability.py) | `apply_multiple_testing_correction` |
| Random baseline (distribution) | same | `compute_random_baseline_ic` |
| Verdict labels | same | `compute_strength_label`, `compute_stability_label`, `compute_direction_label` |
| Retention delta | same | `compute_retention_delta_pct` |
| Slope decisions | same | `compute_slope_decisions` |
| IC decay curve | same | `compute_ic_decay_curve` |
| Regime split | same | `split_by_volatility_regime`, `compute_regime_ic` |
| IR proxy + tradeability | same | `bars_per_year`, `compute_ir_proxy`, `compute_tradeability` |
| Next-steps engine | same | `generate_next_steps` |
| Info footnotes | same | `generate_info_footnotes` |
| Router (response assembly) | [routers/indicator_reliability.py](../PythonDataService/app/routers/indicator_reliability.py) | `calculate_indicator_reliability`, `_to_horizon_ic_result`, `_build_verdict` |
| Response schema | [models/indicator_reliability_models.py](../PythonDataService/app/models/indicator_reliability_models.py) | `HorizonICResult`, `VerdictModel`, `DecayCurvePoint`, `RegimeICPoint`, `RegimeResults`, `IndicatorReliabilityResponse` |
| Tests | [tests/research/test_indicator_reliability.py](../PythonDataService/tests/research/test_indicator_reliability.py) | 53 tests across 10 classes |

### 6.2 Frontend (Angular)

| Concept | File | Symbol |
|---------|------|--------|
| App shell | [app.component.ts](../Frontend/src/app/app.component.ts) | `AppComponent` |
| Sidebar | [shell/app-sidebar.component.ts](../Frontend/src/app/shell/app-sidebar.component.ts) | `AppSidebarComponent`, `NAV` constant |
| Mission-control page | [research-lab/indicator-reliability/](../Frontend/src/app/components/research-lab/indicator-reliability/) | `IndicatorReliabilityComponent` |
| Confidence score | same `.ts` | `getConfidenceScore`, `getConfidenceBucket`, `getConfidenceColor`, `getGaugeDash`, `getVerdictVerb` |
| Reason pills | same | `getReasonPills` |
| WHEN / WHERE / HOW | same | `getWhenCell`, `getWhereCell`, `getHowCell`, `getRegimeComparison` |
| 5-test checklist | same | `getChecklist`, `countFdrPasses` |
| Noise-floor bar | same | `getNoiseFloorBar` |
| Run-on-another-ticker CTA | same | `runOnAnotherTicker` |
| Horizon compact cards | same | `getHorizonCards`, `getHorizonRows` |
| Research Lab landing | [research-lab/research-lab.component.ts](../Frontend/src/app/components/research-lab/research-lab.component.ts) | `ResearchLabComponent`, `active` signal, `groups` |
| Routes | [app.routes.ts](../Frontend/src/app/app.routes.ts) | All lazy-loaded standalone components |

---

## 7. Verification plan

### 7.1 Statistical verification (Python)

```
podman exec polygon-data-service python -m pytest \
    tests/research/test_indicator_reliability.py \
    tests/research/test_ic.py \
    -v
```

Expected: 53 + 15 = 68 passing tests, zero failures. Covers:

- IC correctness on synthetic data with known correlation structure.
- Newey–West against reference values.
- FDR / Bonferroni monotonicity.
- Random baseline z-score scale.
- Verdict label thresholds at boundary values.
- Retention delta edge cases (null OOS, IS near-zero).
- Slope decision flag logic.
- IC decay curve shape on a monotonically-decaying synthetic signal.
- Regime split balance.
- IR proxy scaling with horizon and timespan.
- Tradeability bucket cases.
- Next-step rules (missing OOS, regime-dependent, no-trigger fall-through).

### 7.2 Type / lint (Frontend)

```
podman exec my-frontend npx tsc --noEmit
podman exec my-frontend npx eslint src/app/app.component.ts \
    src/app/shell/ \
    src/app/components/research-lab/indicator-reliability/ \
    src/app/components/research-lab/research-lab.component.ts \
    --max-warnings 0
```

Expected: silent output (no errors or warnings introduced by the UI work).
Note: 309 pre-existing warnings across unrelated `*.spec.ts`, service, and
utility files remain untouched.

### 7.3 Manual UI smoke (representative path)

1. Open `http://localhost:4200`. Verify the left sidebar renders at 240 px
   with five collapsible groups, "Stocks" expanded (the landing route
   `/market-data` is inside it).
2. Press ⌘K (macOS) or Ctrl+K. The sidebar search input gains focus.
3. Type `indicator`. Verify flat-match list shows "Indicator Validation",
   "Indicator Docs", "Indicator Report" (Stocks group), and "Research Lab".
4. Click Research Lab. Land on the sub-nav page with Indicator Reliability
   active by default.
5. Run an analysis: ticker `AAPL`, indicator `rsi` (length 14),
   `2025-07-08 → 2026-04-04`, horizons `[1, 5, 10, 15, 30]`. Verify the
   confidence gauge renders at or near 100/100 with a "TRADE" bucket, the
   hero headline reads "Ready to trade at 30-bar", three decision cells are
   populated, and the 5-test checklist is all green.
6. Click "Run on another ticker", enter `MSFT` at the prompt. The analysis
   re-runs without requiring the user to re-open the form.
7. Navigate to `/market-data` and `/stock-analysis` in sequence. Verify each
   page now uses the full viewport width — no 1200/1400-px cap, no centred
   narrow column.

---

## 8. Limitations and future work

### 8.1 Explicit non-goals (not implemented by design)

- **Variant A — Bloomberg-dense.** The design bundle shipped two variants
  side-by-side; Variant B (mission-control) was chosen. Variant A's
  verdict-strip + data-grid layout is not implemented.
- **Strategy Pre-flight backend.** The "Send to Pre-flight" CTA is rendered
  disabled. A companion backend (route + page + persistence) is a prerequisite
  before wiring this up.
- **Save-verdict persistence.** The "Save to tested indicators" CTA is
  rendered disabled. Requires a database table, a GraphQL mutation, and a
  tested-indicators index page to land together.
- **Research Lab route restructure.** Sub-nav is visual only. A future PR
  can convert each tab into a lazy-loaded route under
  `/research-lab/validate/*`, `/research-lab/inspect/*`, etc., for
  bookmarkable deep-links.
- **No new unit-test coverage for the UI helpers.** Existing Angular tests
  continue to pass; no tests were added for `computeConfidence`,
  `getWhereCell`, or the noise-floor math. A follow-up should add Vitest
  coverage for the four deterministic scoring helpers (§8.4).

### 8.2 Known design compromises / minor bugs

- **Regime median is computed ex-post** (§3.11). A strict real-time filter
  should use a rolling median known at decision time. This is called out in
  the code but the UI does not distinguish "research split" from "tradeable
  split" — future work should clarify this to operators.
- **OOS retention delta does not detect sign flips** (§3.8). IS $= +0.08$
  vs OOS $= -0.08$ renders as `Δ = 0%`. Workaround: check sign of each raw
  IC. Fix: emit an `oos_sign_flip: bool` flag and a UI badge.
- **Research Lab sub-nav state is not URL-backed.** Refreshing
  `/research-lab` always lands on Indicator Reliability regardless of the
  last-open tab. No browser back/forward integration. Acceptable for v1;
  route restructure (§8.1) resolves this.
- **⌘K binding.** On Linux, Ctrl+K is widely bound by browsers (focus URL
  bar) and OS shortcuts. Our `preventDefault()` intercepts before the browser
  sees it, but a user who expects the browser default may be surprised. We
  preserve it because the sidebar is the intended focus target in our
  context.
- **`window.prompt` for Run-on-another-ticker.** Minimum viable UX. A
  PrimeNG `p-dialog` with form validation is a small follow-up.
- **`p-tabs → @switch` lost `role="tablist"` semantics.** The new sub-nav
  is a set of `<button>` elements; PrimeNG Tabs ships ARIA tablist/tab/tabpanel
  roles out of the box. An accessibility follow-up should either re-add
  ARIA manually to the custom sub-nav or switch to a keyboard-arrow pattern
  appropriate for three-group navigation.
- **Hard-coded threshold values** appear throughout — strength buckets at
  0.03/0.07/0.12, stability at 0.52/0.58, direction at 0.02, OOS-holds at
  -30% / -40%, random-z at 3σ, IR proxy at 0.5/1.0, rule 4 at 2× regime
  ratio. These are single-asset intraday-equity calibrations; cross-asset or
  longer-horizon use requires recalibration.

### 8.3 Technical debt introduced

- Inline style content in `AppSidebarComponent` (~280 lines) is embedded in
  the component's `styles` array. Extracting to a `.scss` side-car follows
  the repo convention and eases future visual iteration.
- Sidebar `NAV` constant is hard-coded in the component module. A future
  refactor could move it to a route-metadata layer (derive the IA from the
  route definitions themselves), which would remove duplication between
  `app.routes.ts` and `NAV`.
- The mission-control page has grown to ~900 lines between `.ts` and
  `.html`. Candidate extractions: `ConfidenceGaugeComponent`,
  `DecisionCellComponent`, `ChecklistPanelComponent`,
  `NoiseFloorBarComponent`. Deferred to keep T1 focused; recommended before
  the next behavioural change on this page.

### 8.4 Concrete follow-up items

In priority order:

1. **OOS sign-flip flag + badge.** Small backend field + UI tag. Closes the
   §8.2 gap.
2. **Unit tests for UI helpers.** `computeConfidence`, `getConfidenceBucket`,
   `getRegimeComparison`, `getNoiseFloorBar`, checklist-boundary cases. Use
   Vitest with `@testing-library/angular`.
3. **Indicator-specific direction semantics** (§3.7). RSI and Stoch have
   "mean-reverting when IC negative" baked in; price-based indicators like
   SMA-crossover need a different mapping. Add a lookup table keyed by
   `indicator_name` in `compute_direction_label`.
4. **Replace `window.prompt` with a PrimeNG dialog** for ticker re-run,
   with client-side ticker format validation.
5. **ARIA for the Research Lab sub-nav.** Keyboard arrow navigation,
   `role="tablist"` + `role="tab"` + `role="tabpanel"`.
6. **Research Lab sub-route breakup.** `/research-lab/indicator-reliability`
   etc., with bookmarkable deep-links. Lazy-loaded.
7. **Extract mission-control sub-components** per §8.3.
8. **Wire Send-to-Pre-flight.** Requires the Pre-flight backend and page —
   separate epic.
9. **Wire Save-to-tested-indicators.** Requires persistence — separate epic.
10. **Block-bootstrap confidence intervals** for the decay curve, instead of
    the current NW-implied SE (§3.10). Would give more realistic coverage
    under strong serial correlation.

---

## 9. References

### Statistical

- Newey, W. K., & West, K. D. (1987). "A Simple, Positive Semi-Definite,
  Heteroskedasticity and Autocorrelation Consistent Covariance Matrix."
  *Econometrica*, 55(3), 703–708.
- Andrews, D. W. K. (1991). "Heteroskedasticity and Autocorrelation
  Consistent Covariance Matrix Estimation." *Econometrica*, 59(3), 817–858.
- Benjamini, Y., & Hochberg, Y. (1995). "Controlling the False Discovery
  Rate: A Practical and Powerful Approach to Multiple Testing." *Journal of
  the Royal Statistical Society B*, 57(1), 289–300.
- Grinold, R. C. (1989). "The Fundamental Law of Active Management."
  *Journal of Portfolio Management*, 15(3), 30–37.
- Grinold, R. C., & Kahn, R. N. (1999). *Active Portfolio Management*
  (2nd ed.). McGraw-Hill. Chapters 6 & 10.

### Design / implementation

- Claude Design bundle `quant-trading-lab-design-system` — the source design
  artifact driving T1–T3. Extracted locally to
  `/tmp/anthropic-design/quant-trading-lab-design-system/`. Primary files
  consulted: `project/research_lab_redesign/variant-b-mission.jsx`,
  `project/research_lab_redesign/shared/sidebar.jsx`,
  `project/research_lab_redesign/shared/header.jsx`.
- Repo conventions — [Frontend/CLAUDE.md](../Frontend/CLAUDE.md),
  [PythonDataService/CLAUDE.md](../PythonDataService/CLAUDE.md),
  [.claude/CLAUDE.md](../.claude/CLAUDE.md).
- Previous version of this document (P1–P3 math-only) — superseded by the
  present consolidated reference.
