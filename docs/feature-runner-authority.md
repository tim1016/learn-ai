# Feature Runner — Authority & Methodology

> **Canonical reference** for Research Lab → Feature Runner. Covers the
> target construction, IC machinery, the four-screen + regime-stability
> verdict block, the 0/1/2/3 graduation ladder, the per-feature
> validation contracts, and the API/UI surfaces.
>
> **Audience:** graduate-level reader with a light statistics background.
> The page surfaces are written so the reader does not need to know
> Newey-West or Holm-Bonferroni by name; the deeper machinery is
> documented here once and referenced by tooltip from the UI.
>
> **Owner:** the engineer editing
> `PythonDataService/app/research/{target.py,feature_spec.py,feature_validation.py,validation/}`.
> If you change the math in those files, update the matching section
> here in the same PR and bump **Last reviewed**.
>
> **Last reviewed:** 2026-05-01 (v2 ChatGPT review — target time-vs-bars
> fix, regime-stability screen, direction-match gate, wrong-target
> Stage 0 block, signed-vs-directional cost spread, `passed_validation`
> back-compat collapsed to `stage >= 2`).

---

## Table of contents

- [1. Scope and authority](#1-scope-and-authority)
- [2. Notation and glossary](#2-notation-and-glossary)
- [3. Pipeline overview](#3-pipeline-overview)
- [4. Target construction](#4-target-construction)
  - [4.1 Time-based horizon](#41-time-based-horizon)
  - [4.2 Timestamp-delta gate](#42-timestamp-delta-gate)
  - [4.3 Session masking (America/New_York)](#43-session-masking-americanew_york)
  - [4.4 Schema validation](#44-schema-validation)
  - [4.5 TargetResult metadata](#45-targetresult-metadata)
- [5. Information Coefficient](#5-information-coefficient)
  - [5.1 Daily Spearman IC](#51-daily-spearman-ic)
  - [5.2 Newey-West HAC t-stat](#52-newey-west-hac-t-stat)
  - [5.3 Effective sample size](#53-effective-sample-size)
  - [5.4 IC confidence interval](#54-ic-confidence-interval)
- [6. Quantile analysis](#6-quantile-analysis)
- [7. Per-feature validation contracts](#7-per-feature-validation-contracts)
- [8. The five validation screens](#8-the-five-validation-screens)
  - [8.1 Statistical association](#81-statistical-association)
  - [8.2 Economic viability](#82-economic-viability)
  - [8.3 Out-of-sample retention](#83-out-of-sample-retention)
  - [8.4 Multiple-testing correction](#84-multiple-testing-correction)
  - [8.5 Regime stability](#85-regime-stability)
- [9. Stage ladder (0/1/2/3)](#9-stage-ladder-0123)
- [10. `passed_validation` back-compat](#10-passed_validation-back-compat)
- [11. API contract](#11-api-contract)
- [12. UI surfaces](#12-ui-surfaces)
- [13. Code cross-reference](#13-code-cross-reference)
- [14. Known limitations and roadmap](#14-known-limitations-and-roadmap)
- [15. References](#15-references)

---

## 1. Scope and authority

The Feature Runner answers a narrow question: **does this feature have
predictive content for forward returns on this ticker, over this
window, in a way that survives cost, OOS evidence, and multiple
testing?**

It does **not** answer:

- Whether the feature can be implemented as a tradable signal — that's
  the Signal Engine's job (z-score construction, threshold sweeps,
  walk-forward Sharpe, deflated Sharpe). See
  [docs/signal-engine-authority.md](signal-engine-authority.md).
- Whether the feature works in the cross-section across many tickers
  — that's the Cross-Sectional page. See
  `PythonDataService/app/research/batch_runner.py`.

When the three pages disagree, the Signal Engine is the trading
authority and the Cross-Sectional page is the cross-asset authority;
the Feature Runner is the **single-asset feature triage** authority.

The math/logic in this directory is the canonical implementation. If
this document and the code disagree, the code is right and this doc
needs to be updated. CI does not enforce that, so do it manually.

---

## 2. Notation and glossary

| Symbol | Meaning |
| --- | --- |
| `r_{t,h}` | Forward log return at time `t`, horizon `h` minutes. |
| `f_t` | Feature value at time `t`. |
| `IC_d` | Daily Spearman rank correlation between `f` and `r` over a single trading day. |
| `mean_IC` | Sample mean of `IC_d` across days. |
| `N` | Number of daily IC observations. |
| `N_eff` | Autocorrelation-adjusted effective sample size of the daily IC series. |
| `t_NW` | Newey-West HAC-corrected t-statistic of `mean_IC`. |
| `Q_k` | Mean forward return inside feature quintile `k` (k = 1..5). |
| `Holm_p` | Holm-Bonferroni-corrected p-value for the headline NW p, over the user's feature family. |

---

## 3. Pipeline overview

```
list[OHLCVBar]
   │
   ▼
target.compute_forward_log_return(bars, horizon_minutes=15)
   │   ↳ schema validate → infer bar_minutes → session-mask → timestamp-delta gate
   ▼
TargetResult(values, timestamps, target_name, horizon_*, timezone, …)
   │
   ▼   feature = TechnicalFeatures.compute_feature(name, bars)
   ▼   ic = compute_information_coefficient(feature, target.values, …)
   ▼   stationarity = run_stationarity_tests(feature.dropna())
   ▼   quantile = compute_quantile_analysis(feature, target.values)
   ▼   robustness = compute_robustness(ic.daily_ic_values, …)
   ▼
spec = feature_spec.get_spec(name)
   │
   ▼
verdict = feature_validation.evaluate_feature_validation(
    spec=spec, mean_ic, nw_p, effective_n,
    is_stationary, is_monotonic, quantile_bins,
    train_test_present, test_days, test_mean_ic, oos_retention,
    regimes_observed, regime_sign_flip_fraction,
    cost_assumption_one_way_bps, n_family,
)
   │
   ▼
ResearchReport(target=target, validation_verdict=verdict, passed_validation=stage>=2, …)
```

---

## 4. Target construction

The forward log return is the headline target every IC, quantile, and
stage-ladder claim is built on. Three subtly different concepts were
conflated in the original implementation; v2 separates them
explicitly:

- **"15-minute forward return"** (time-based, what the UI promises)
- **"horizon=15 bar offset"** (bar-count, what a naive `i + 15` does)
- **"no cross-day contamination"** (UTC-date masked, what the code did)

### 4.1 Time-based horizon

```python
fwd_log_return(t, horizon_minutes=15) = ln(close[t + horizon_bars] / close[t])
horizon_bars = horizon_minutes // bar_minutes
```

`bar_minutes` is inferred from the median consecutive timestamp delta
when not supplied; an explicit caller value wins with a warning. The
horizon must be an integer multiple of `bar_minutes` — fractional
horizons raise rather than rounding silently.

A 5-minute-bar caller passing `horizon_minutes=15` gets a 3-bar
forward offset (= 15 wall-clock minutes), **not** a 75-minute offset
masquerading as 15.

### 4.2 Timestamp-delta gate

```python
expected_delta_ms = horizon_minutes * 60_000
upper_delta_ms    = expected_delta_ms + max_overshoot_minutes * 60_000

delta = int(timestamp[i + horizon_bars] - timestamp[i])
if delta < expected_delta_ms or delta > upper_delta_ms:
    forward_return[i] = NaN  # reason: window_gap
```

Missing bars (halts, late-prints, low-liquidity minutes in the
Polygon Starter feed) cause the bar at `i + horizon_bars` to land at
a slightly later wall-clock time than `t + horizon_minutes`.

The gate accepts a tolerance of `max_overshoot_minutes`
(default `min(horizon_minutes, 5)` — so 5 minutes for the standard
15-minute horizon). Without any tolerance, real Polygon data was
losing 30%+ of bars to single-minute gaps. With 5 minutes of slack,
that drops to <2% on AAPL Q1 2025 — and the gate still catches the
structural bug ChatGPT flagged: a 5-minute-bar caller asking for a
"15-minute" horizon who somehow got a 75-minute window would land at
+60 minutes overshoot, far outside any sensible tolerance.

`max_overshoot_minutes=0` recovers the strict gate; useful for
regression tests asserting feed integrity.

### 4.3 Session masking (America/New_York)

Trading-day boundaries are not UTC dates. The last few minutes of the
US regular session (15:55 ET = 19:55 UTC, after DST 20:55 UTC) can
land on a UTC date that disagrees with the session date. A naive
`pd.to_datetime(ms).dt.date` mask would split bars that belong to the
same trading session, or join bars across sessions when UTC midnight
falls inside the session.

```python
ts_utc = pd.to_datetime(timestamps, unit="ms", utc=True)
session_date = ts_utc.dt.tz_convert("America/New_York").dt.date
```

Cross-session forward windows are NaN with reason `cross_session`. The
timezone is configurable for non-US instruments; the spec disclosure
on the page surfaces which timezone was used so a mismatch (e.g.
running an EU instrument with the US default) is visible.

> **Open work.** Session masking is calendar-naive — it doesn't yet
> recognise lunch gaps, early closes, halts, or extended-hours
> transitions. The timestamp-delta gate catches most of these because
> a halt produces an irregular delta, but a clean early-close + next-day
> open with a uniform delta would slip through. Tracked under § 14.

### 4.4 Schema validation

`compute_forward_log_return` fails fast on:

- Missing `timestamp` or `close` columns.
- Non-numeric or coercion-failing `close`.
- `inf` / `-inf` in `close`.
- Duplicate timestamps.

These are not silently coerced — the function raises with a
descriptive error so the upstream feed bug is fixed rather than
masked.

### 4.5 TargetResult metadata

Every call returns a `TargetResult`:

```python
@dataclass(frozen=True)
class TargetResult:
    values: pd.Series          # forward log returns, positional 0..n-1
    timestamps: pd.Series      # int64 ms UTC at each position
    target_name: str           # "forward_log_return_15m"
    horizon_minutes: int
    horizon_bars: int
    bar_minutes: int
    timezone: str              # "America/New_York"
    valid_count: int
    total_count: int
    invalid_reason_counts: dict[str, int]
```

`invalid_reason_counts` is the reader-facing audit trail: a near-zero
IC against a feature with 60 % NaN forward returns gets attributed
to data, not signal, by surfacing the dominant drop reason
(`window_runs_off_end`, `cross_session`, `window_gap`,
`non_positive_close`).

The legacy `compute_15min_forward_return(bars, horizon=15)` wrapper is
retained for Signal Engine call sites that haven't migrated. It
treats `horizon` as a number of bar offsets and is the **wrong API
for new code**; new callers must use `compute_forward_log_return`
directly.

---

## 5. Information Coefficient

### 5.1 Daily Spearman IC

For each trading day with at least 5 observations and non-degenerate
variance:

```
IC_d = Spearman(rank(f_d), rank(r_d))
```

Days with `< 5` valid observations or zero feature/target variance are
dropped. The Spearman choice is non-parametric — robust to fat tails
and monotone-but-non-linear relationships, which is most of what
intraday features look like.

### 5.2 Newey-West HAC t-stat

The naive `t = mean_IC / (std / sqrt(N))` overstates significance
when daily ICs are autocorrelated. We apply a Newey-West correction
with a Bartlett kernel and Andrews (1991) automatic bandwidth:

```
max_lag = max(floor(4 * (N/100)^(2/9)), effective_min_lag)
gamma_0 = sum(demeaned^2) / N
gamma_j = sum(demeaned[j:] * demeaned[:-j]) / N
nw_var  = gamma_0 + 2 * sum_{j=1..L} bartlett(j) * gamma_j
nw_se   = sqrt(nw_var / N)
t_NW    = mean_IC / nw_se
```

`min_lag` is a caller-supplied floor (5 for daily options data); at
small N we cap it at `n // 4` so a `min_lag = 5` with `n ≈ 5` does
not collapse `nw_var` to ~0.

### 5.3 Effective sample size

```
N_eff = N / (1 + 2 * sum_{k=1..L} rho_k)
```

`rho_k` is the lag-k autocorrelation of the daily IC series.
Summation truncates at the first lag where `rho_k < 0.05` to avoid
inflating the denominator with noise. This is the same Andrews
bandwidth used for `t_NW` so the two are consistent.

> **Known limitation.** The truncation at `rho_k < 0.05` discards
> oscillatory autocorrelation (e.g. `rho_1 = 0.04`, `rho_2 = 0.20`).
> For typical IC series this is rare; we have not seen it in practice
> on the five built-in features. If it surfaces, replace with a
> "negative-rho or below-threshold" stop. Tracked under § 14.

### 5.4 IC confidence interval

```
SE  ≈ 1 / sqrt(N_eff)
CI  = mean_IC ± Phi^{-1}(1 - alpha/2) * SE
```

We use the simpler `1 / N_eff` form rather than Lo's full
`(1 - IC^2)^2 / N_eff`. For `|IC| ≈ 0.05` and `N_eff = 200` the two
agree to ~0.5 %; the upgrade is on the roadmap (§ 14) but not load-
bearing at the magnitudes the Stage 1 screen cares about.

**The CI is over `N_eff` daily IC observations, not bar-level
samples.** The UI labels it "Mean daily IC 95% CI" so the reader
doesn't conflate `N_eff = 71` with the 47 000+ underlying bars.

---

## 6. Quantile analysis

`compute_quantile_analysis` divides feature values into 5 buckets via
`pd.qcut(..., duplicates="drop")` and reports each bucket's mean
forward return. Monotonicity is computed in both directions
(increasing and decreasing) and the better ratio is reported; a ratio
≥ 0.75 is considered monotonic.

> **Known limitation.** `duplicates="drop"` silently collapses bins
> when feature values tie at quantile boundaries (bounded features
> like RSI at the rails). The actual bucket count is always
> `len(bins)` rather than `n_bins`; the UI shows the actual count
> implicitly via the bar chart. A separate "fewer than n_bins
> available" warning is on the roadmap (§ 14).

---

## 7. Per-feature validation contracts

`feature_spec.FeatureValidationSpec` documents what the feature is
testing, per feature:

| Feature | direction | shape | stationarity req | monotonicity req | signed target appropriate |
| --- | --- | --- | --- | --- | --- |
| `rsi_14` | negative | monotonic_decreasing | yes | yes | yes |
| `momentum_5m` | positive | monotonic_increasing | yes | yes | yes |
| `macd_signal` | positive | monotonic_increasing | no | no | yes |
| `realized_vol_30` | two_sided | none | no | no | **no** |
| `volume_zscore` | two_sided | u_shaped | yes | no | yes |

Three of the fields are gating:

- **`stationarity_required`** — when True, ADF/KPSS rejecting
  stationarity fails the statistical screen. RSI is bounded
  `[0, 100]` so stationarity is a reasonable demand. MACD is a price
  difference and is explicitly not required to be stationary.
- **`monotonicity_required`** — when True, a non-monotonic quantile
  chart fails the statistical screen. MACD's predictive content is
  concentrated at sign-change events, not uniformly across the
  feature distribution, so monotonicity is not required.
- **`is_signed_target_appropriate`** — when False, the feature is
  rejected at Stage 0 with a "Wrong target" reason. Realized-vol
  features predict the **size** of the next move, not the sign;
  reporting an IC against signed forward return as a passed test
  would be misleading. The IC is preserved for diagnostic display
  but cannot graduate.

`expected_direction` is used by the **direction-match check** in the
statistical screen (§ 8.1) and to anchor the **directional spread** in
the economic screen (§ 8.2).

---

## 8. The five validation screens

Each screen is a binary pass/fail with optional failure reasons. A
screen is either **required for Stage 1** or **diagnostic at Stage 1
and gating Stage 2+**. All screens are reported regardless; the UI
shows pass/fail with explicit reasons rather than collapsing to a
single boolean.

### 8.1 Statistical association (required for Stage 1)

Passes when **all** hold:

1. `|mean_IC| ≥ STAGE1_MIN_ABS_IC` (= 0.03)
2. `nw_p_value < STAGE1_MAX_NW_P` (= 0.05)
3. `effective_N ≥ STAGE1_MIN_EFFECTIVE_N` (= 60 daily IC obs)
4. If `spec.stationarity_required`, `ADF p < 0.05 AND KPSS p > 0.05`.
5. If `spec.monotonicity_required`, monotonicity ratio ≥ 0.75.
6. **Direction match** — `sign(mean_IC)` agrees with
   `spec.expected_direction`. `unknown` and `two_sided` accept
   either sign.

The direction-match gate is the v2 fix to the "negative IC on a
positive-direction feature looks like passing" bug. A mismatch is
reported as `"IC sign disagrees with spec.expected_direction = '…'.
Possible inverse-relationship discovery"` and the user can re-run with
`expected_direction="unknown"` to validate as exploratory.

### 8.2 Economic viability (gating Stage 2+)

Passes when net spec-direction long-short spread > 0 at the assumed
one-way cost:

```
gross_spread_signed = (Q5_mean − Q1_mean) × 10^4    # bps
directional_spread =
    +gross_spread_signed   if spec.expected_direction == "positive"
    -gross_spread_signed   if spec.expected_direction == "negative"
    |gross_spread_signed|  otherwise
net_spread = directional_spread - 2 × cost_assumption_one_way_bps
viable     = net_spread > 0
```

Round-trip cost = `2 × one_way_bps` (enter + exit, both legs).
Slippage and market impact are **not** modelled. The 1 bp default is
intentionally tight — a fraction-of-a-basis-point gross spread should
visibly fail.

The signed-vs-directional split fixes the v2 review's cost-spread
sign inconsistency: the UI used to display a signed Q5−Q1 (negative
for mean-reversion features) on the headline and an absolute spread
in the cost table. The new screen reports both and gates on the
directional one.

### 8.3 Out-of-sample retention (gating Stage 2+)

Passes when **all** hold:

1. Train/test split was computable (≥ 10 daily IC obs, both halves ≥ 5).
2. Test window ≥ `STAGE2_MIN_TEST_DAYS` (= 40 days).
3. `|test_IC| ≥ STAGE2_MIN_ABS_TEST_IC` (= 0.015).
4. `oos_retention = |test_IC| / |train_IC| ≥ STAGE2_MIN_OOS_RETENTION`
   (= 0.50; Stage 3 requires 0.60).

A weak test-window IC despite a strong overall IC is the standard
overfitting signal; the retention ratio captures the fact that the
sample is small enough that some IC decay is expected.

### 8.4 Multiple-testing correction (gating Stage 2+)

The user can run any of the 5 built-in features. The Holm-Bonferroni
rank-1 correction reduces to:

```
holm_p = min(1, raw_nw_p × n_family)
```

The default `n_family = 5` is the built-in feature count. The runner
exposes an override for callers running a wider research family;
**there is no API parameter exposed yet** — see § 14.

Stage 1 requires `holm_p < 0.05`; Stage 2 relaxes to `≤ 0.10` because
mid-stage research can tolerate a slightly elevated family-wise
false-positive rate; Stage 3 returns to `≤ 0.05`.

### 8.5 Regime stability (gating Stage 2+)

v2 review addition. An allocator's chief concern is "does this signal
work in more than one market state". The screen passes when:

1. `regimes_observed ≥ 4` buckets (vol low/normal/high × trend up/sideways/down).
2. The fraction of buckets whose IC sign disagrees with the overall
   sign is `≤ 0.34` (≈ 1 in 3 flips allowed).

The "sign flip" is anchored on the headline IC sign so the screen
reflects "does the spec-direction story hold across regimes" rather
than just bucket-by-bucket positivity.

This was previously a hidden Stage 2 sub-criterion (`regimes_observed
≥ 4`); promoting it makes regime-only signals visible in the verdict.

---

## 9. Stage ladder (0/1/2/3)

| Stage | Label | Means |
| --- | --- | --- |
| 0 | Rejected | At least one required-for-Stage-1 screen failed (today: only Statistical), OR the spec marks signed forward return as the wrong target. |
| 1 | Statistical association | In-sample IC is real, but cost/OOS/multiple-testing/regime evidence is missing or weak. **Not tradeable as shown.** |
| 2 | Research candidate | All four optional screens pass at Stage 2 thresholds, plus regime stability and ≥ 4 regimes observed. Defensible mid-stage research. |
| 3 | Paper-trading candidate | Stage 3 thresholds on every gate. **Not** live-trading validated. |

### Stage 1 → 2 advance criteria

- |Test IC| ≥ 0.015
- OOS retention ≥ 50 %
- Test window ≥ 40 days
- Holm p ≤ 0.10
- Net directional spread > 0 at 1 bp one-way
- ≥ 4 regime buckets, sign-flip fraction ≤ 0.34

### Stage 2 → 3 advance criteria

- Effective N ≥ 180
- Test window ≥ 60 days
- |Test IC| ≥ 0.02
- OOS retention ≥ 60 %
- Holm p ≤ 0.05
- Cost-viable + regime-stable (already gating at Stage 2; Stage 3
  enforces tighter OOS / Holm thresholds)

> **|IC| floor stays at 0.03 at every stage.** The v2 review's central
> point: a flashy |IC| = 0.08 with negative net-cost spread should not
> graduate over a stable |IC| = 0.035 with positive net spread and
> strong OOS. The discriminating factor between Stage 2 and Stage 3 is
> *implementability*, not *flashiness*.

### Stage 0 wrong-target fast-path

When `spec.is_signed_target_appropriate is False`, the verdict
short-circuits to Stage 0 with a "Wrong target" failed-screens entry
and a final-decision string explaining what the right target would
be. The IC against signed return is preserved for diagnostic display
but cannot graduate.

---

## 10. `passed_validation` back-compat

The legacy `ResearchReport.passed_validation: bool` is **derived from
the verdict**:

```
passed_validation = stage_info.stage >= 2
```

v1 mapped this to `stage >= 1`, which leaked Stage 1 ("statistical
association only, not tradeable") to unmigrated callers as if it had
passed validation. v2 review correctly identified this as the wrong
collapse.

New code must read `validation_verdict.stage_info.stage` and the four
screens directly. Callers that still consume `passed_validation` get
a `True` only when the verdict reaches research-candidate quality or
better.

---

## 11. API contract

```http
POST /api/research/run-feature
Content-Type: application/json

{
  "ticker": "AAPL",
  "feature_name": "momentum_5m",
  "bars": [{"timestamp": 1704117000000, "open": 150.0, "high": 150.3, "low": 149.7, "close": 150.05, "volume": 1000000}, ...],
  "start_date": "2024-01-01",
  "end_date": "2024-12-31"
}
```

Response (abbreviated):

```jsonc
{
  "success": true,
  "ticker": "AAPL",
  "feature_name": "momentum_5m",
  "mean_ic": -0.0712,
  "ic_t_stat": -3.21,
  "nw_t_stat": -2.84,
  "nw_p_value": 0.0046,
  "effective_n": 71,
  "is_stationary": true,
  "is_monotonic": true,
  "passed_validation": false,            // = stage >= 2 (v2 collapse)
  "feature_spec": {
    "feature_name": "momentum_5m",
    "default_target": "forward_log_return_15m",
    "expected_direction": "positive",
    "expected_shape": "monotonic_increasing",
    "stationarity_required": true,
    "monotonicity_required": true,
    "is_signed_target_appropriate": true,
    "intent": "...",
    "notes": []
  },
  "target_metadata": {
    "target_name": "forward_log_return_15m",
    "horizon_minutes": 15,
    "horizon_bars": 15,
    "bar_minutes": 1,
    "timezone": "America/New_York",
    "valid_count": 47013,
    "total_count": 47550,
    "valid_ratio": 0.9887,
    "invalid_reason_counts": {"window_runs_off_end": 537}
  },
  "validation_verdict": {
    "statistical_screen": {"name": "Statistical association", "passed": false, "failure_reasons": ["IC sign disagrees with spec.expected_direction = 'positive'. ..."]},
    "economic_screen":    {"name": "Economic viability", "passed": false, "failure_reasons": ["..."]},
    "oos_screen":         {"name": "Out-of-sample", "passed": false, "failure_reasons": ["..."]},
    "multiple_testing_screen": {"name": "Multiple-testing correction", "passed": true},
    "regime_stability_screen": {"name": "Regime stability", "passed": false, "failure_reasons": ["Only 3 regime buckets observed; need ≥ 4."]},
    "multiple_testing": {"raw_nw_p_value": 0.0046, "holm_p_value": 0.023, "n_family": 5, "note": "..."},
    "cost_viability": {
      "gross_spread_bps_signed": -1.12,
      "directional_spread_bps": -1.12,        // positive direction = Q5-Q1
      "cost_assumption_one_way_bps": 1.0,
      "cost_erasure_one_way_bps": 0.0,
      "net_spread_bps_at_assumption": -3.12,
      "viable_at_assumption": false,
      "spec_direction": "positive",
      "note": ""
    },
    "ic_ci": {"point": -0.0712, "ci_lower": -0.3038, "ci_upper": 0.1614, "n_eff_used": 71, "valid": true, ...},
    "direction_matches_spec": false,
    "target_signed_appropriate": true,
    "stage_info": {"stage": 0, "label": "Rejected", "failed_screens": ["Statistical association"]},
    "final_decision": "Do not trade. Rejected at Stage 0 (Statistical association)."
  }
}
```

The .NET → GraphQL → Angular path mirrors this; field naming follows
each layer's idiom (`snake_case` Python, `PascalCase` C#,
`camelCase` TS/GraphQL).

---

## 12. UI surfaces

The Angular `feature-report` component renders, in order:

1. **Research Grade card** — four-dimension grade (legacy; pre-stage-ladder).
2. **Exploratory Override banner** — fires when `effective_n < 60`.
3. **Sample Size Credibility badge** — N_eff, IC days, coverage %.
4. **Multi-Screen Verdict block** (the v2 redesign):
   - Stage badge (0/1/2/3 + label).
   - Final-decision one-liner.
   - Stage description.
   - Spec disclosure pills (target, direction, shape, optional "wrong target" warn).
   - **Target metadata line** — what was actually computed (horizon
     in minutes, bar count, session timezone, valid ratio, top drop
     reason).
   - Five screen rows (statistical / economic / OOS / multiple-testing
     / regime stability) with pass/fail icons, descriptions, and
     failure reasons.
   - **Mean daily IC 95% CI** line — explicitly labelled as
     daily-IC-observations, not bar-level.
   - **Cost viability line** showing both signed Q5−Q1 and the
     spec-direction spread, with the spec direction in the heading.
   - Multiple-testing warning paragraph.
   - Advance criteria for stages < 3.
4b. **Sample Coverage Tier banner** — the legacy verdict banner
    (renamed from "Confidence Tier" in v1). Kept for back-compat.
5. t-stat method selector + summary metrics grid.
6. IC and quantile charts.
7. Cost sensitivity slider + table.
8. Robustness analysis (monthly breakdown, regime ICs, train/test).
9. Kill-criteria / falsification box (legacy; partially redundant with
   the Stage 0 ladder — tracked under § 14).
10. Statistical assumptions disclosure.
11. Bars-used footer.

---

## 13. Code cross-reference

| File | Purpose |
| --- | --- |
| [`PythonDataService/app/research/target.py`](../PythonDataService/app/research/target.py) | `compute_forward_log_return`, `TargetResult`, `validate_return_series`, legacy `compute_15min_forward_return` wrapper. |
| [`PythonDataService/app/research/feature_spec.py`](../PythonDataService/app/research/feature_spec.py) | `FeatureValidationSpec` + 5 built-in specs + `get_spec` lookup. |
| [`PythonDataService/app/research/feature_validation.py`](../PythonDataService/app/research/feature_validation.py) | Five screens + IC CI + cost viability + Holm + stage ladder + verdict orchestrator. |
| [`PythonDataService/app/research/runner.py`](../PythonDataService/app/research/runner.py) | `run_feature_research` — wires target → IC → robustness → verdict. |
| [`PythonDataService/app/research/validation/ic.py`](../PythonDataService/app/research/validation/ic.py) | Daily IC, NW t-stat, N_eff, hit rate. |
| [`PythonDataService/app/research/validation/quantile.py`](../PythonDataService/app/research/validation/quantile.py) | Quintile bucketing + monotonicity ratio. |
| [`PythonDataService/app/research/validation/robustness.py`](../PythonDataService/app/research/validation/robustness.py) | Monthly breakdown, regime ICs, train/test split, structural-break sliding test. |
| [`PythonDataService/app/routers/research.py`](../PythonDataService/app/routers/research.py) | `/api/research/run-feature` + DTO mappers. |
| [`PythonDataService/app/models/research_models.py`](../PythonDataService/app/models/research_models.py) | Pydantic v2 request/response models. |
| [`Backend/Models/DTOs/ResearchModels.cs`](../Backend/Models/DTOs/ResearchModels.cs) | C# DTOs (snake-case JSON ↔ PascalCase). |
| [`Backend/GraphQL/Types/ResearchResult.cs`](../Backend/GraphQL/Types/ResearchResult.cs) | Hot Chocolate v15 GraphQL types with `[GraphQLName]` overrides. |
| [`Backend/GraphQL/Types/ResearchResultMapper.cs`](../Backend/GraphQL/Types/ResearchResultMapper.cs) | DTO → GraphQL type mapping (single source for both `runFeatureResearch` and `runOptionsFeatureResearch`). |
| [`Frontend/src/app/services/research.service.ts`](../Frontend/src/app/services/research.service.ts) | TS interfaces + GraphQL queries. |
| [`Frontend/src/app/components/research-lab/feature-report/`](../Frontend/src/app/components/research-lab/feature-report/) | Angular component rendering the verdict block + legacy banners. |
| [`PythonDataService/tests/research/test_target.py`](../PythonDataService/tests/research/test_target.py) | Pin contract guarantees of the rewrite (time-vs-bars, session, schema, gate). |
| [`PythonDataService/tests/test_feature_validation.py`](../PythonDataService/tests/test_feature_validation.py) | Pin screens, stage ladder, IC CI, cost viability, Holm, regime stability, direction match, wrong-target block. |

---

## 14. Known limitations and roadmap

**Known wrong / weak:**

- **Session masking is calendar-naive.** Lunch gaps, early closes,
  halts, and extended-hours transitions are not first-class. The
  timestamp-delta gate catches most of these, but a clean early-close
  + next-day open with uniform delta would slip through.
- **`N_eff` truncation drops oscillatory autocorrelation.** A
  `rho_1 = 0.04`, `rho_2 = 0.20` series gets `N_eff = N`. Replace
  with a "stop on negative-rho or below-threshold" rule.
- **IC CI uses simplified `1/N_eff` SE.** Lo's full
  `(1 - IC^2)^2 / N_eff` form is more accurate at higher |IC|. Upgrade
  is a Stage 3 concern; current users are Stage 1.
- **`pd.qcut(..., duplicates="drop")` silently degrades bin count.**
  No warning surfaces when the actual bucket count is less than
  `n_bins`. Add an `actual_n_bins` field + UI badge.
- **Holm `n_family` has no API override.** The runner accepts the
  parameter; the router doesn't expose it. Add to the request model
  for users running a wider research family.
- **No feature-aware target dispatch.** The runner always computes
  signed forward log return. `realized_vol_30` would prefer
  `|forward_return|` or forward realized vol. Today the spec just
  blocks graduation for these features; the right fix is a target
  registry (`spec.default_target` → callable) with the runner picking.
- **Legacy "Kill criteria / Falsification" box is partially redundant
  with the Stage 0 ladder.** The ladder subsumes IC collapse, OOS
  degradation, and cost erosion; the structural-break and regime-flip
  rows still add information. Audit and merge or remove duplicates.
- **`compute_15min_forward_return` legacy wrapper still exists.**
  Used by Signal Engine. Migrate Signal Engine to
  `compute_forward_log_return` with explicit `horizon_minutes` and
  delete the wrapper.

**Roadmap (priorities):**

1. Feature-aware target dispatch (`realized_vol_30` → forward |return|).
2. Migrate Signal Engine to the new target API.
3. Expose `n_family` override on the API.
4. Lo full `(1 - IC^2)^2 / N_eff` SE form.
5. Calendar-aware session masking (early closes, lunch gaps).
6. Kill-criteria box vs. Stage 0 ladder de-duplication.

---

## 15. References

- **Spearman, C.** (1904). The proof and measurement of association
  between two things. *American Journal of Psychology* 15(1): 72–101.
- **Newey, W. K. & West, K. D.** (1987). A simple, positive semi-
  definite, heteroskedasticity and autocorrelation consistent
  covariance matrix. *Econometrica* 55(3): 703–708.
- **Andrews, D. W. K.** (1991). Heteroskedasticity and autocorrelation
  consistent covariance matrix estimation. *Econometrica* 59(3):
  817–858.
- **Holm, S.** (1979). A simple sequentially rejective multiple test
  procedure. *Scandinavian Journal of Statistics* 6(2): 65–70.
- **Lo, A. W.** (2002). The Statistics of Sharpe Ratios. *Financial
  Analysts Journal* 58(4): 36–52. (Used here for the IC CI form.)
- **Bailey, D. H. & López de Prado, M.** (2014). The Deflated Sharpe
  Ratio. *Journal of Portfolio Management* 40(5): 94–107. (Used in the
  Signal Engine; cited here for ladder-pattern symmetry.)

Companion documents:

- [`docs/signal-engine-authority.md`](signal-engine-authority.md)
- [`docs/indicator-reliability-authority.md`](indicator-reliability-authority.md)
- [`docs/references/sharpe-ci-and-deflated-sharpe.md`](references/sharpe-ci-and-deflated-sharpe.md)
