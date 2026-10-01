# Indicator Reliability — Methodology, Metrics & UI Reference

> **Methodology reference** for the Indicator Reliability page. Covers the
> statistical methodology (IC, Newey–West, FDR, regime conditioning, IR proxy)
> and the rules behind the page's confidence score, WHEN/WHERE/HOW decision
> cells, 5-test checklist and noise-floor bar.
>
> **Primary reader:** anyone reading the page who wants the derivation behind a
> number. Assumes familiarity with time-series statistics.

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
- [5. UI implementation](#5-ui-implementation)
  - [5.3 Indicator Reliability page (mission control)](#53-indicator-reliability-page-mission-control)
    - [5.3.2 Confidence score](#532-confidence-score)
    - [5.3.5 WHEN cell](#535-when-cell)
    - [5.3.6 WHERE cell](#536-where-cell)
    - [5.3.7 HOW cell](#537-how-cell)
    - [5.3.8 Five-test decision checklist](#538-five-test-decision-checklist)
    - [5.3.9 Noise-floor bar](#539-noise-floor-bar)
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
| Frontend | Angular 22 + PrimeNG 22 + Chart.js | Mission-control UI, app shell, navigation |

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
best-horizon result and rendered as a 15-bin
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

## 5. UI implementation

Only the page rules that the page's help tooltips link to are kept here.

### 5.3 Indicator Reliability page (mission control)

How the page turns the statistics in §3 into its verdict, decision cells,
checklist and noise-floor bar.

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

---

## 8. Limitations and future work

### 8.1 Explicit non-goals (not implemented by design)

- **Strategy Pre-flight backend.** The "Send to Pre-flight" CTA is rendered
  disabled. A companion backend (route + page + persistence) is a prerequisite
  before wiring this up.
- **Save-verdict persistence.** The "Save to tested indicators" CTA is
  rendered disabled. Requires a database table, a GraphQL mutation, and a
  tested-indicators index page to land together.

### 8.2 Known design compromises / minor bugs

- **Regime median is computed ex-post** (§3.11). A strict real-time filter
  should use a rolling median known at decision time. This is called out in
  the code but the UI does not distinguish "research split" from "tradeable
  split" — future work should clarify this to operators.
- **OOS retention delta does not detect sign flips** (§3.8). IS $= +0.08$
  vs OOS $= -0.08$ renders as `Δ = 0%`. Workaround: check sign of each raw
  IC. Fix: emit an `oos_sign_flip: bool` flag and a UI badge.
- **Hard-coded threshold values** appear throughout — strength buckets at
  0.03/0.07/0.12, stability at 0.52/0.58, direction at 0.02, OOS-holds at
  -30% / -40%, random-z at 3σ, IR proxy at 0.5/1.0, rule 4 at 2× regime
  ratio. These are single-asset intraday-equity calibrations; cross-asset or
  longer-horizon use requires recalibration.

### 8.4 Concrete follow-up items

In priority order:

1. **OOS sign-flip flag + badge.** Small backend field + UI tag. Closes the
   §8.2 gap.
2. **Indicator-specific direction semantics** (§3.7). RSI and Stoch have
   "mean-reverting when IC negative" baked in; price-based indicators like
   SMA-crossover need a different mapping. Add a lookup table keyed by
   `indicator_name` in `compute_direction_label`.
3. **Block-bootstrap confidence intervals** for the decay curve, instead of
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
