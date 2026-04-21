# Indicator Reliability — Methodology & Metrics

> Reference for the time-series IC analyzer in
> [PythonDataService/app/research/indicator_reliability.py](../PythonDataService/app/research/indicator_reliability.py)
> and its Angular front-end at
> [Frontend/src/app/components/research-lab/indicator-reliability/](../Frontend/src/app/components/research-lab/indicator-reliability/).

The tool answers two questions:

1. **Is this indicator predictive?** — quantified by the Information Coefficient (IC) with honest statistical machinery (Newey–West, FDR, random baseline).
2. **Is it tradeable, and if not, what should I try next?** — quantified by the IR proxy, regime conditioning, stability metrics, and a rule-based next-steps engine.

> **Important scope:** this computes **time-series IC for a single asset** — the rank correlation between an indicator and its own future return, aggregated across days. This is **not** the cross-sectional factor IC used in multi-asset factor models, where IC is the cross-sectional rank correlation at a single point in time. Confusing the two leads to invalid IR translations and misleading significance.

---

## 1. Notation

| Symbol | Meaning |
|--------|---------|
| $C_t$ | Close price at bar $t$ |
| $r_t^{(h)} = \ln(C_{t+h} / C_t)$ | $h$-bar forward log return (masked to NaN when bars $t$ and $t+h$ span a day boundary) |
| $f_t$ | Indicator value at bar $t$ |
| $d$ | A calendar date (session) |
| $\mathcal{B}_d$ | The set of bars belonging to date $d$ |
| $n_d = \lvert \mathcal{B}_d \rvert$ | Bars in day $d$ after dropping NaN feature/return pairs |
| $N$ | Number of days with a valid daily IC |
| $m$ | Number of horizons tested (for multiple-testing correction) |
| $h$ | A forward horizon (in bars) |
| $K$ | Number of random-shuffle simulations (default 100) |

All aggregations below operate on the **in-sample** (train) period — a chronological 70/30 split. Out-of-sample metrics use the held-out 30% with the same definitions.

---

## 2. Core Metric: Daily Information Coefficient

Computed in
[`validation/ic.py::compute_information_coefficient`](../PythonDataService/app/research/validation/ic.py).

For each day $d$ with $n_d \geq 5$ valid bars and non-degenerate feature/return variance (each $> 10^{-12}$):

$$
IC_d \;=\; \rho_{\text{Spearman}}\!\left( \{f_t\}_{t \in \mathcal{B}_d},\; \{r_t^{(h)}\}_{t \in \mathcal{B}_d} \right)
$$

Days that fail the variance/length checks are dropped. The **aggregated IC** is the arithmetic mean across valid days:

$$
\overline{IC} = \frac{1}{N} \sum_{d=1}^{N} IC_d,
\qquad
s_{IC}^2 = \frac{1}{N-1} \sum_{d=1}^{N} (IC_d - \overline{IC})^2
$$

**Standard t-statistic** (assumes independent daily ICs — known to be optimistic for intraday signals with carryover):

$$
t_{\text{std}} = \frac{\overline{IC}}{s_{IC} / \sqrt{N}},
\qquad
p_{\text{std}} = 2 \cdot \big(1 - F_{t, N-1}(|t_{\text{std}}|)\big)
$$

---

## 3. Newey–West HAC-Corrected Statistics

Because daily ICs often exhibit positive serial correlation (persistent regimes, overlapping signals), the standard t-stat overstates significance. We apply a Newey–West (1987) HAC correction with a Bartlett kernel.

**Bandwidth** (Andrews, 1991):

$$
L = \max\!\left( 1,\; \left\lfloor 4\,(N/100)^{2/9} \right\rfloor \right),
\qquad L \leq N - 2
$$

**Autocovariances** (using the ML (biased) divisor $N$ consistent with the HAC estimator):

$$
\gamma_0 = \frac{1}{N} \sum_{d=1}^{N} (IC_d - \overline{IC})^2
$$

$$
\gamma_j = \frac{1}{N} \sum_{d=j+1}^{N} (IC_d - \overline{IC})(IC_{d-j} - \overline{IC})
\qquad j = 1, \ldots, L
$$

**Bartlett-weighted long-run variance**:

$$
\widehat{\sigma}^2_{NW} = \gamma_0 + 2 \sum_{j=1}^{L} \left( 1 - \frac{j}{L+1} \right) \gamma_j
$$

If $\widehat{\sigma}^2_{NW} \leq 10^{-20}$ the t-stat is set to zero (degenerate series).

$$
t_{NW} = \frac{\overline{IC}}{\sqrt{\widehat{\sigma}^2_{NW} / N}},
\qquad
p_{NW} = 2\,\big(1 - F_{t, N-1}(|t_{NW}|)\big)
$$

**Effective sample size** (accounts for autocorrelation, same bandwidth):

$$
\rho_k = \frac{1}{N \gamma_0} \sum_{d=k+1}^{N} (IC_d - \overline{IC})(IC_{d-k} - \overline{IC})
$$

$$
N_{\text{eff}} = \frac{N}{1 + 2 \sum_{k=1}^{K^*} \rho_k},
\qquad
K^* = \min\{k : \rho_k < 0.05\} - 1 \text{ (or } L \text{ if never)}
$$

The denominator is floored at 1, so $N_{\text{eff}} \leq N$ always. $N_{\text{eff}}$ is surfaced per-horizon in the UI as an honesty signal: if $N_{\text{eff}} \ll N$, the daily ICs are not independent and the raw p-value is overconfident.

---

## 4. Multiple-Testing Correction

When $m$ horizons are tested in one experiment we correct the raw (NW) p-values — implemented in `apply_multiple_testing_correction`.

**Bonferroni**:

$$
p_i^{\text{Bonf}} = \min(p_i \cdot m,\; 1)
$$

**Benjamini–Hochberg FDR** (two-pass):

1. Rank the $m$ p-values ascending: $p_{(1)} \leq \ldots \leq p_{(m)}$.
2. Compute $\tilde p_{(i)} = \min(1,\; p_{(i)} \cdot m / i)$.
3. Enforce monotonicity from the top: $\tilde p_{(i)} \leftarrow \min(\tilde p_{(i)}, \tilde p_{(i+1)})$.

Verdict labels use FDR-adjusted p for IS significance. Bonferroni is reported alongside as the conservative upper bound.

---

## 5. Random Baseline

Implemented in `compute_random_baseline_ic`. For $k = 1, \ldots, K$ ($K = 100$ by default):

1. Draw a permutation $\pi_k$ of $\{0, 1, \ldots, T-1\}$ (T = total bars in IS).
2. Replace the indicator with the permuted index sequence $\tilde f^{(k)}_t = \pi_k(t)$ — monotone in rank within the day but uncorrelated with future returns across days by construction.
3. Compute $\overline{IC}^{(k)}$ the same way as step 2.

Let $\bar\mu = \text{mean}_k \overline{IC}^{(k)}$ and $\bar\sigma = \text{std}_k \overline{IC}^{(k)}$ (with a $10^{-10}$ floor to avoid division-by-zero).

**Z-score vs random**:

$$
z_{\text{rand}} = \frac{\overline{IC}_{\text{actual}} - \bar\mu}{\bar\sigma}
$$

The full distribution $\{\overline{IC}^{(k)}\}_{k=1}^K$ is stored on the best-horizon result (payload-gated) and rendered as a 15-bin histogram in the UI with the actual IC's bin highlighted in orange.

**Interpretation**: $|z_{\text{rand}}| \geq 2$ is treated as "distinguishable from noise." This is a heuristic, not a formal significance test — the random-shuffle null destroys any temporal structure in the indicator, which may be stricter than the null you actually care about (e.g., "a buy-and-hold with IC=0").

---

## 6. Stability Metrics (Tranche 1)

**Hit rate** — the fraction of daily ICs whose sign matches the aggregate IC sign:

$$
\mathrm{HR} = \frac{1}{N} \sum_{d=1}^{N} \mathbf{1}\{\operatorname{sgn}(IC_d) = \operatorname{sgn}(\overline{IC})\}
$$

Deliberately **not** "fraction $IC_d > 0$" — for a mean-reverting signal with $\overline{IC} < 0$, we want a high count of negative $IC_d$, not positive. $\mathrm{HR} \geq 0.5$ is required by construction only when the mean is dominated by a majority; the metric can fall below $0.5$ when a few extreme days drive the aggregate mean in the opposite direction from the median day — a red flag the UI surfaces as low stability.

**Daily IC std** ($s_{IC}$ above) is surfaced as a raw number. High $s_{IC}$ with a decent $\overline{IC}$ signals "the edge exists on average but is unreliable day-to-day."

---

## 7. Verdict Labels (Tranche 1)

All four labels are computed on the **best horizon** (selected by `find_best_horizon`: OOS significance preferred, FDR significance as fallback). Code: `compute_strength_label`, `compute_stability_label`, `compute_direction_label`.

### Strength (|IC| buckets)

$$
\text{strength}(|\overline{IC}|) =
\begin{cases}
\text{Strong}    & |\overline{IC}| \geq 0.12 \\
\text{Moderate}  & 0.07 \leq |\overline{IC}| < 0.12 \\
\text{Weak}      & 0.03 \leq |\overline{IC}| < 0.07 \\
\text{Noise}     & |\overline{IC}| < 0.03
\end{cases}
$$

Thresholds are calibrated for time-series daily IC on liquid intraday equity data. For other asset classes (crypto, options vol) they should be re-tuned — empirical IC distributions differ.

### Stability (hit-rate buckets)

$$
\text{stability}(\mathrm{HR}) =
\begin{cases}
\text{High}      & \mathrm{HR} \geq 0.58 \\
\text{Moderate}  & 0.52 \leq \mathrm{HR} < 0.58 \\
\text{Low}       & \mathrm{HR} < 0.52
\end{cases}
$$

### Direction

$$
\text{direction}(\overline{IC}) =
\begin{cases}
\text{Momentum}        & \overline{IC} > 0.02 \\
\text{Mean-Reversion}  & \overline{IC} < -0.02 \\
\text{None}            & |\overline{IC}| \leq 0.02
\end{cases}
$$

For oscillator-style indicators (RSI, Stoch) a negative IC maps to mean-reversion (high indicator → low future return). For raw price or SMA-style indicators the semantics of the sign are indicator-specific; a future refinement can add per-indicator label maps.

### Retention delta

Reported as a percentage to replace the confusing "124%" legacy retention ratio:

$$
\Delta_{\text{OOS/IS}} = \left( \frac{|\overline{IC}_{\text{OOS}}|}{|\overline{IC}_{\text{IS}}|} - 1 \right) \times 100\%
$$

Undefined when $|\overline{IC}_{\text{IS}}| < 10^{-10}$ or OOS is unavailable. Positive values mean OOS is **stronger** than IS (suspicious — possible regime change in your favor or small-sample noise); negative values mean OOS is weaker (degradation). The raw ratio is kept for back-compat and surfaced as a tooltip.

**Caveat**: the delta does not detect sign flips — $\overline{IC}_{\text{IS}} = +0.08$ vs $\overline{IC}_{\text{OOS}} = -0.08$ yields $\Delta = 0\%$ which is misleading. The front-end can derive sign-flip from $\operatorname{sgn}(\overline{IC}_{\text{IS}}) \cdot \operatorname{sgn}(\overline{IC}_{\text{OOS}})$.

### Slope decision flags

For the optional slope variant (IC of $\Delta f_t = f_t - f_{t-1}$ vs forward return):

$$
\text{adds\_value} \;=\;
\begin{cases}
|\overline{IC}^{\text{slope}}| > 0.02 &
  \text{if } |\overline{IC}^{\text{raw}}| < 10^{-10} \\[4pt]
\left(|\overline{IC}^{\text{slope}}| > 1.20 \cdot |\overline{IC}^{\text{raw}}|\right) \,\wedge\,
\left(p^{\text{slope}}_{\text{FDR}} < p^{\text{raw}}_{\text{FDR}}\right) &
  \text{otherwise}
\end{cases}
$$

$$
\text{recommended} \;=\; \text{adds\_value} \;\wedge\;
\left(p^{\text{slope}}_{\text{oos}} < 0.10 \;\vee\; \text{retention}^{\text{slope}}_{\text{oos}} \geq 0.60\right)
$$

Returns `None` for `recommended` when OOS data is unavailable — "cannot recommend what we haven't validated."

---

## 8. IC Decay Curve (Tranche 2)

A single-pass diagnostic showing IC as a function of forward horizon. For each $h \in \{1, 2, \ldots, H_{\max}\}$ where $H_{\max} = \min(\max(\text{requested horizons}) + 10,\; 60)$:

1. Recompute $r_t^{(h)}$ on the **IS period**.
2. Compute daily IC aggregate $\overline{IC}(h)$ and its NW statistics via the standard pipeline.
3. Derive a standard error for the CI band:

$$
\text{SE}(h) =
\begin{cases}
\left| \overline{IC}(h) / t_{NW}(h) \right| & |t_{NW}(h)| > 10^{-10} \\[4pt]
s_{IC}(h) / \sqrt{\max(N_{\text{eff}}(h),\, 1)} & \text{otherwise}
\end{cases}
$$

The chart plots $\overline{IC}(h) \pm 1.96 \cdot \text{SE}(h)$ as a shaded band with the peak horizon (argmax $|\overline{IC}|$) flagged.

**No multiple-testing correction is applied to the decay curve** — it's a visualisation of signal structure, not a significance test. The rigorous test lives in the main results table.

---

## 9. Volatility Regime Conditioning (Tranche 2)

Answers "when does the signal work?" rather than just "does it work on average?"

**Rolling realized volatility** on the IS close series with a window $w = 20$ bars by default:

$$
\sigma_t = \operatorname{std}\!\left( \{\ln(C_s / C_{s-1}) : s \in (t - w, \ldots, t]\} \right)
$$

Bars in the warmup ($t < w$) have $\sigma_t = \text{NaN}$ and are **excluded from both regimes**.

**Regime masks** using the in-sample median as the split:

$$
\tilde\sigma = \operatorname{median}\!\big( \{\sigma_t : \sigma_t \text{ defined}\} \big)
$$

$$
\mathcal{H} = \{t : \sigma_t > \tilde\sigma\},
\qquad
\mathcal{L} = \{t : \sigma_t \leq \tilde\sigma,\; \sigma_t \text{ defined}\}
$$

**Per-regime IC**: critically, forward returns are computed on the **full training series** *before* masking, then indexed by the regime mask:

$$
\overline{IC}_{\mathcal{R}}(h) = \text{daily-aggregated IC on } \{(f_t, r_t^{(h)}) : t \in \mathcal{R}\}
$$

This keeps the horizon $h$ a true wall-clock horizon — $h$ bars ahead in real time, regardless of whether the intervening bars were in-regime. Masking *after* computing $r_t^{(h)}$ prevents the statistic from silently changing meaning inside each regime.

Buckets smaller than `MIN_REGIME_BARS = 50` return `null` in the response; the UI renders "Not enough bars in this regime" in that cell.

**Limitation**: the split uses the IS median ex-post. This is fine for the diagnostic question "in which regimes does this work?" but **not** valid for live trading — a real-time filter would use a rolling median known at the decision point. For research purposes this is acceptable and clearly documented.

---

## 10. IR Proxy & Tradeability (Tranche 3)

### Bars per trading year

For a given Polygon timespan/multiplier, the router computes:

```
bars_per_year(timespan, multiplier) =
    252 * (bars_per_trading_day[timespan] / multiplier)
```

where `bars_per_trading_day`:

| timespan | bars/day |
|----------|----------|
| minute   | 390      |
| hour     | 6.5      |
| day      | 1        |
| week     | 0.2      |
| month    | 1/21     |

### IR proxy via breadth

Following the IC-IR textbook relationship (Grinold, 1989; Grinold & Kahn, 1999):

$$
\text{breadth}_{\text{year}} = \max\!\left( \frac{\text{bars}_{\text{year}}}{h},\; 1 \right)
$$

$$
\text{IR}_{\text{annual}} \;\approx\; \overline{IC} \cdot \sqrt{\text{breadth}_{\text{year}}}
$$

$$
\text{Sharpe}_{\text{proxy}} \;=\; \text{IR}_{\text{annual}}
$$

under a unit-vol, zero-cost assumption.

### Tradeability bucketing

$$
\text{tradeability}(s,\; \text{stab}) =
\begin{cases}
\text{Likely tradeable}  & |s| \geq 1.0 \;\wedge\; \text{stab} = \text{High} \\
\text{Marginal}          & 0.5 \leq |s| < 1.0 \;\vee\; (|s| \geq 1.0 \wedge \text{stab} \neq \text{High}) \\
\text{Unlikely}          & |s| < 0.5
\end{cases}
$$

Uses $|s|$ so that a negative-IC signal (short the indicator) is treated symmetrically.

### Caveats (surfaced as `tradeability_caveat`)

The IC-IR translation assumes:

1. **Independent bets** — which is violated when forward returns at overlapping horizons share bars. $\text{breadth}_{\text{year}} = \text{bars}_{\text{year}} / h$ overstates independent observations; the true effective breadth is closer to $N_{\text{eff,year}}$, which is typically much smaller.
2. **Unit volatility of the signal-weighted return** — i.e., position sizing is variance-normalised. Real portfolios rarely achieve this.
3. **Zero transaction costs** — a fatal omission for high-frequency signals. A horizon-$h$ strategy trades every $h$ bars, and a $\text{Sharpe}_{\text{proxy}} = 2$ on 1-minute bars can collapse to zero under realistic costs.

The UI treats this as a **proxy**, not a tradeable Sharpe estimate, and labels it accordingly.

### Why the sign of $\text{Sharpe}_{\text{proxy}}$ matters

A large negative sharpe means "the indicator *forecasts the opposite* direction strongly" — the tradeable strategy is to **short the signal** (go long when indicator is low, short when high). Tradeability uses $|s|$ so this doesn't get penalised for having negative IC.

---

## 11. Next-Steps Rule Engine (Tranche 3)

Implemented in `generate_next_steps`. Rules are evaluated in this order; up to **4** are returned:

| # | Condition on best horizon | Suggestion |
|---|---------------------------|------------|
| 1 | $\overline{IC}_{\text{OOS}}$ is `None` | "Collect more out-of-sample data before trading — current result is in-sample only." |
| 2 | $|\overline{IC}_{\text{high\_vol}}| \geq 0.03$ and $\geq 2 \times |\overline{IC}_{\text{low\_vol}}|$ | "Add a volatility filter — signal is materially stronger in high-vol regimes." |
| 3 | Symmetric: $|\overline{IC}_{\text{low\_vol}}| \geq 0.03$ and $\geq 2 \times |\overline{IC}_{\text{high\_vol}}|$ | "Add a low-vol filter — signal degrades sharply in high-vol regimes." |
| 4 | stability = Low and $|\overline{IC}_{\text{IS}}| \geq 0.03$ | "Try a longer horizon — signal has directional edge but is noisy at the current horizon." |
| 5 | Slope `adds_value` is True on the best horizon | "Try the slope variant — the indicator's rate of change is stronger than its raw value." |
| 6 | strength $\in$ {Moderate, Strong}, stability = High, $p_{\text{OOS}} < 0.10$ | "Consider a threshold-based strategy and measure realized Sharpe with transaction costs." |

**Fall-through** (when no rule fires):

- If strength $\in$ {Moderate, Strong} and ($p_{\text{OOS}}$ is `None` or $\geq 0.10$): "IS edge did not validate out-of-sample — try a longer date range or different parameters before trading."
- Otherwise: "Signal looks noise-like; consider a different indicator, parameter sweep, or longer window."

---

## 12. Honesty Footnotes (Tranche 3)

Always-present soft reminders rendered as muted text (`info_footnotes` field), *not* warning-severity messages — avoids alarm fatigue while keeping the limitations visible:

1. **Always** — "Single-asset IC — portfolio IC across many tickers may differ substantially."
2. **Always** — "Time-series IC is not the same as cross-sectional factor IC."
3. **Conditionally** (any requested horizon $> 1$) — "Overlapping forward returns inflate raw significance; NW-adjusted stats are shown where possible."

---

## 13. API contract (response shape)

All fields on `IndicatorReliabilityResponse`. New additions are grouped by tranche.

### Per-horizon result (`HorizonICResult`)

```
horizon                          int
# In-sample
is_mean_ic, is_t_stat, is_p_value       float
is_nw_t_stat, is_nw_p_value             float | null
is_effective_n                           int
is_hit_rate, is_daily_ic_std             float                 # T1
# Out-of-sample
oos_mean_ic, oos_t_stat, oos_p_value     float | null
oos_effective_n                          int   | null
oos_retention                            float | null
retention_delta_pct                      float | null         # T1
# Multiple testing
bonferroni_p, fdr_p                      float
# Random baseline
random_baseline_mean, random_baseline_std, ic_vs_random_zscore   float
random_baseline_distribution             list[float]          # T3 (best horizon only)
# Verdict labels
strength_label, stability_label, direction_label   enum        # T1
# Slope decisions (slope rows only)
slope_adds_value, slope_recommended      bool | null           # T1
# IR proxy
annualized_ir, sharpe_estimate, breadth_per_year   float       # T3
# Legacy free-text
is_interpretation, oos_interpretation    string
```

### Top-level

```
# Summary + rollup fields (pre-existing)
success, ticker, indicator_name, display_name, category           ...
bar_count, train_bars, test_bars, train_ratio, train_start, ...   ...
results:         list[HorizonICResult]
slope_results:   list[HorizonICResult] | null
daily_ic_values, daily_ic_dates:   lists (best horizon's IS daily IC)
best_horizon:    int | null
any_significant_after_bonferroni, any_significant_after_fdr:  bool
num_horizons_tested, random_simulations:  int

# Verdict + diagnostics + honesty
verdict:            VerdictModel | null                            # T1+T3
decay_curve:        list[DecayCurvePoint]                          # T2
regime_results:     RegimeResults | null                           # T2
next_steps:         list[str]                                      # T3
info_footnotes:     list[str]                                      # T3
warnings:           list[str]
error:              string | null
```

`VerdictModel`:

```
direction:             Momentum | Mean-Reversion | None
strength:              Noise | Weak | Moderate | Strong
stability:             Low | Moderate | High
tradeability:          Likely tradeable | Marginal | Unlikely | Unknown
horizon:               int | null
tradeability_caveat:   string | null
```

`DecayCurvePoint`:

```
horizon:   int
ic:        float
p_value:   float
ic_stderr: float
```

`RegimeResults`:

```
high_vol:   list[RegimeICPoint] | null
low_vol:    list[RegimeICPoint] | null
vol_window: int    (20 by default)
```

`RegimeICPoint`:

```
horizon, effective_n, bars_in_regime:   int
mean_ic, t_stat, p_value, hit_rate:     float
```

---

## 14. What we explicitly do *not* claim

These are out-of-scope by design; see the UI's honesty footnote strip.

- **Cross-sectional IC.** We compute time-series IC on one ticker at a time. Applying IC-IR thresholds from the factor literature is apples-to-oranges.
- **Transaction costs.** Sharpe proxy assumes zero costs. A high-frequency signal's realised Sharpe can be dominated by cost drag.
- **Non-stationarity.** The IS/OOS split captures gross regime shifts, but a signal that worked in 2023 may fail in 2026 for reasons neither IS nor OOS reveal.
- **Multi-session research penalty.** FDR corrects across horizons within one run, not across the many runs a researcher typically executes. Cumulative selection bias across sessions is un-corrected.
- **Simulated PnL.** The tool does not run a strategy simulation. Sharpe proxy is analytical. A realised backtest is strictly required before trading.

---

## 15. References

- Newey, W. K., & West, K. D. (1987). "A Simple, Positive Semi-Definite, Heteroskedasticity and Autocorrelation Consistent Covariance Matrix." *Econometrica*, 55(3), 703–708.
- Andrews, D. W. K. (1991). "Heteroskedasticity and Autocorrelation Consistent Covariance Matrix Estimation." *Econometrica*, 59(3), 817–858.
- Benjamini, Y., & Hochberg, Y. (1995). "Controlling the False Discovery Rate: A Practical and Powerful Approach to Multiple Testing." *Journal of the Royal Statistical Society B*, 57(1), 289–300.
- Grinold, R. C. (1989). "The Fundamental Law of Active Management." *Journal of Portfolio Management*, 15(3), 30–37.
- Grinold, R. C., & Kahn, R. N. (1999). *Active Portfolio Management* (2nd ed.). McGraw-Hill. Chapters 6 & 10.

---

## 16. Cross-references (code paths)

| Concept | File | Symbol |
|---------|------|--------|
| Daily IC + NW + N_eff | [PythonDataService/app/research/validation/ic.py](../PythonDataService/app/research/validation/ic.py) | `compute_information_coefficient`, `_compute_newey_west_t_stat`, `_compute_effective_sample_size` |
| Hit rate | same | `_compute_hit_rate` |
| FDR / Bonferroni | [.../indicator_reliability.py](../PythonDataService/app/research/indicator_reliability.py) | `apply_multiple_testing_correction` |
| Random baseline (distribution) | same | `compute_random_baseline_ic` |
| Verdict labels | same | `compute_strength_label`, `compute_stability_label`, `compute_direction_label` |
| Retention delta | same | `compute_retention_delta_pct` |
| Slope decisions | same | `compute_slope_decisions` |
| IC decay curve | same | `compute_ic_decay_curve` |
| Regime split | same | `split_by_volatility_regime`, `compute_regime_ic` |
| IR proxy + tradeability | same | `bars_per_year`, `compute_ir_proxy`, `compute_tradeability` |
| Next-steps engine | same | `generate_next_steps` |
| Info footnotes | same | `generate_info_footnotes` |
| Router (response assembly) | [.../routers/indicator_reliability.py](../PythonDataService/app/routers/indicator_reliability.py) | `calculate_indicator_reliability`, `_to_horizon_ic_result`, `_build_verdict` |
| Frontend | [Frontend/src/app/components/research-lab/indicator-reliability/](../Frontend/src/app/components/research-lab/indicator-reliability/) | `IndicatorReliabilityComponent` |
