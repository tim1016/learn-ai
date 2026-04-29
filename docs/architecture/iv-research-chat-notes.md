# IV Pipeline — Chat Notes (Cowork session, 2026-04-29)

> Companion to `iv-ownership-research.md` and `iv-research-prompt.md`. Captures
> the substantive Q&A from a Cowork-mode chat session: the external-review
> prompt construction, RV-estimator inventory, accuracy/reliability comparison,
> and Polygon-Starter strategy framing. Reference this file from Claude Code
> with "read docs/architecture/iv-research-chat-notes.md" to skip rebuilding
> the context.

---

## 1. External-review prompt

The compressed external-review prompt for ChatGPT-style quant review lives at
`docs/architecture/iv-research-prompt.md`. It distills the 32k-token research
doc to ~1.8k words organised as:

- §1–§4: compressed context (one-paragraph overview, empirical anchor, math
  in canonical form, validation summary)
- §5: nine specific asks where the work is suspected weakest:
  basis-converter overnight bias, confidence floor heuristic, imputed-prior
  0.5, multiplicative confidence form, synthetic spread policy, one-anchor-day
  CBOE comparison, 0DTE solver, single-strike domination as
  diagnostic-vs-gate, dividend-yield proxy
- §6: items already considered (so the reviewer doesn't relitigate
  Polygon-IV fallback, blending, ffill-at-boundary, etc.)
- §7: structured response format (`Verdict / Argument / Recommended action`)

---

## 2. RV-estimator inventory in the repo

Two estimators exist and serve different jobs:

### 2.1 The VRP driver — `app/engine/edge/features_realtime/hf_realized_vol.py`

High-frequency two-component estimator:

```
RV²_d = Σ_{i ∈ session} r²_i  +  r²_overnight
```

- Intraday sum is over consecutive 15-min bar returns *within* the chosen
  session (no return crosses a session boundary).
- Overnight is a single log-return spanning the gap from the previous
  session's last close to today's first.
- Sessions: ETH (04:00–20:00, 64 bars/day, 8h overnight) or RTH
  (09:30–16:00, 26 bars/day, 17.5h overnight).
- Zero-volume bars dropped before computing returns (Polygon ETH wee-hours
  bias).
- Annualised over a window W: `σ²_TRD/252 = (252/W) · Σ RV²_d`.

Forward-shifted twin at `app/engine/edge/labels_oracle/hf_forward_rv.py`. The
`labels_oracle/` directory is a CI guard preventing realtime feature code
from importing it (no look-ahead).

This is the RV that feeds `compute_vrp` and `vrp_signal` in the wiring
sequence (`app/routers/edge.py:realized_vs_iv_series` step 4).

### 2.2 Visualization chips — `app/engine/edge/features_realtime/realized_vol.py`

Daily-bar four-estimator suite:

- **Close-to-close** (CtC): `σ² = (1/(n−1)) Σ (r_t − r̄)²`, `r_t = ln(C_t/C_{t−1})`
- **Parkinson (1980)**: `σ² = (1/(4n ln 2)) Σ [ln(H_t/L_t)]²` — drift-zero,
  ignores overnight gaps, ~5× more efficient than CtC under GBM
- **Garman–Klass (1980)**: `σ² = (1/n) Σ [0.5 (ln H/L)² − (2 ln 2 − 1)(ln C/O)²]`
- **Yang–Zhang (2000)**: drift-independent, gap-aware,
  `σ²_YZ = σ²_O + k σ²_C + (1−k) σ²_RS` with `k = 0.34/(1.34 + (n+1)/(n−1))`

These are UI chips on the realized-vs-IV chart, **not** wired into VRP.
Forward twin lives at `app/engine/edge/labels_oracle/forward_rv.py`.

---

## 3. Accuracy vs reliability of the two estimators

### 3.1 Accuracy (variance of the estimator)

Under standard GBM, estimator variance scales inversely with observation
count per day:

| Estimator | Efficiency vs CtC | Per-day observations |
|---|---|---|
| CtC | 1× | 1 |
| Parkinson | ~5× | 2 (H, L) |
| Garman–Klass | ~7× | 4 (OHLC) |
| Yang–Zhang | ~10–14× | 4 (OHLC) + open jump |
| **HF two-component (15-min ETH)** | **~order of magnitude over YZ** | **~64 + overnight** |

The HF estimator with 64 ETH bars approaches consistency
(Andersen–Bollerslev–Diebold–Labys 1999): `Σ r²_i` converges to the true
integrated variance at sufficient sampling frequency. Daily-bar estimators
are noisy proxies; HF is a near-consistent estimator.

**Microstructure-noise floor.** At very high frequencies, bid-ask bounce and
discrete pricing bias `Σ r²_i` upward. Literature sweet spot is ~5-min
sampling on liquid US equities; **15-min is conservatively safe**. Going
sub-5-min would require noise-correction (Hansen–Lunde 2005, two-scale RV).

### 3.2 Reliability (robustness to assumption breakage)

Different ranking from accuracy:

- **Yang–Zhang** is the most reliable *daily* estimator — drift-independent
  and gap-aware. Parkinson and Garman–Klass both assume zero drift, which
  breaks on trending days.
- **HF two-component** is reliable when intraday bars are populated;
  degrades when bars are stale or zero-volume. The `volume == 0` drop
  handles Polygon's ETH wee-hours staleness, but the estimator's
  reliability remains a function of data quality.
- **CtC** is the most assumption-light (just needs closes) but the noisiest
  — the boring-but-always-works baseline.

### 3.3 Why HF is the right choice for VRP specifically

VRP compares IV30 against forward RV over the same horizon. With daily
estimators, a 21-day forward window gives 21 observations — RV's standard
error is too large to detect anything but the largest VRP signals. HF at
64 bars/day gives ~1,344 observations over the same window. The
signal-to-noise of the VRP statistic on a 252-bar lookback z-score is
dominated by the RV estimator's noise; HF is what makes VRP usable at all.

### 3.4 Caveat worth flagging

The single squared overnight return is itself a high-variance estimator of
overnight integrated variance (Hansen–Lunde 2005, Martens 2002). On
contracts with high overnight-vs-intraday share (single names with
earnings), the overnight piece dominates the noise. For SPY, NBER w17422
puts overnight at ~30% of total variance — non-trivial. This is the same
underlying issue as the deferred basis-converter overnight upgrade in
[`iv-ownership-research.md` §8.2.1]: improving the basis converter without
also improving the overnight component leaves a structural gap.

---

## 4. Polygon Starter — strategy framing

Constraints: 15-min delayed snapshots, 2y history, **no historical NBBO**,
full options chains with vendor IV diagnostic, daily OHLCV, dividends,
splits, reference data.

### 4.1 The asset is forward data, not backtests

The recorder is the load-bearing piece. Real OPRA-mid bid/ask at known
slots with full provenance compounds: in 6 months there's 6 months of
clean IV30 history nobody else on Starter has. After 30 sessions →
confidence-floor calibration. After 90 → cross-sectional confirmation
across ETFs. After 180 → defensible forward track record.

### 4.2 Synthetic-spread backtests are exploratory, not authoritative

The `max($0.05, 0.5%·close)` half-spread proxy tests the *shape* of a
signal (does VRP-timing show edge in the expected direction; does the
skew premium correlate with realized tail moves), but absolute IV30
levels are biased on OTM wings — exactly what variance-share gating
exists to handle. Treat synthetic-only periods as "is the strategy
structurally plausible," not "what would my Sharpe have been."

### 4.3 Strategies that fit Starter cleanly

- **VRP timing on SPY/QQQ/IWM/DIA** — current pipeline is wired for this.
  Cross-sectional dispersion comes free once the recorder has history.
- **Term-structure / calendar signals** — IV1 vs IV3 vs IV6
  contango/backwardation, listed mids only, recorder schema generalizes.
- **Skew-driven signals** — risk-reversal, butterfly steepness, from
  listed prices on broad ETFs. VIX-style replication already integrates
  skew; surfacing it as its own metric is cheap.
- **Regime classification on IV30 + RV30** — current
  `feature_weight = max(0, 2h−1)·(1−vcs)` path, expanded feature set once
  recorder data exists.
- **Earnings-cycle vol patterns** — pre-vs-post earnings IV crush in
  cross-section on ETF baskets.

### 4.4 What doesn't fit, with upgrade trigger

| Strategy | Why blocked | Upgrade trigger |
|---|---|---|
| Option-execution simulation with realistic fills | Needs historical NBBO | Polygon Options Advanced or CBOE DataShop, only when a strategy *demands* execution-quality fills |
| Single-name option strategies | Dividend proxy breaks, sparse chains | Per-name dividend feed + historical NBBO together, or stay on ETF universe |
| Sub-15-min signals | Wrong plan entirely | Streaming/WebSocket — different problem |
| Pre-2024 (COVID, 2022 vol) backtests | 2y history limit | CBOE DataShop OPRA or ORATS/IVolatility EOD option histories — first upgrade if regime coverage matters more than execution detail |

### 4.5 Recommended next steps (no plan upgrade)

1. **Pick one signal** (VRP timing on SPY) and run it forward on real
   recorder data from day 1. Don't optimize on synthetic backtest;
   paper-track forward with the existing confidence-gated signal.
2. **After 30 sessions** — calibrate the confidence floor against
   reliability curves.
3. **After 90 sessions** — add a second ETF (QQQ) for cross-sectional
   confirmation.
4. **After 180 sessions** — defensible forward track record on a real
   (forward-only, no execution) basis; decide on execution-data upgrade.

### 4.6 Cheap wins, no plan upgrade required

- **Add IWM/QQQ/DIA to the recorder.** Storage and snapshot-call cost is
  linear; cross-sectional features come free.
- **Capture daily option-chain summaries** (IV30, skew, term structure)
  to a separate table from snapshot rows. Cheap append; becomes a regime
  feature library at no extra cost.

Both are pure recorder-side work; no math touched.

---

## 5. How this file fits

| Want | File |
|---|---|
| Full audit trail of the IV work | `iv-ownership-research.md` |
| External-LLM reviewer prompt | `iv-research-prompt.md` |
| This-chat conversational notes | `iv-research-chat-notes.md` (this file) |
