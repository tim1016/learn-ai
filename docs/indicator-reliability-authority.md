# Indicator Reliability — Authority & Methodology

> **Canonical reference** for Research Lab → Indicator Reliability.
> Covers what "reliability" means *on this page* (single-asset
> predictive evidence under a declared spec; **not** a live-trade
> readiness claim), the five decision screens, the tier model, the
> hard demotions that gate the headline, and the known limitations.
>
> **Audience:** graduate-level reader with a light statistics
> background. The page surfaces are written so the reader does not
> need to know FDR / Bonferroni / Newey-West by name; the deeper
> machinery is documented here once and referenced by tooltip from
> the UI.
>
> **Owner:** the engineer editing
> `PythonDataService/app/research/indicator_reliability/` and
> `Frontend/src/app/components/research-lab/indicator-reliability/`.
> If you change the verdict math or the tier rules, update the
> matching section here in the same PR and bump **Last reviewed**.
>
> **Last reviewed:** 2026-05-01 (v2 ChatGPT review on the page —
> verdict-scalar removal, "Ready to trade" → "Pre-flight candidate",
> hit-rate format bug, "Economically meaningful" rename, hard
> demotions for single-asset / Sharpe-proxy<0 / undersized random
> baseline).

---

## Table of contents

- [1. Scope and authority](#1-scope-and-authority)
- [2. What this page does NOT claim](#2-what-this-page-does-not-claim)
- [3. The five decision screens](#3-the-five-decision-screens)
- [4. Tier model + hard demotions](#4-tier-model--hard-demotions)
- [5. Decision band — When / Where / How](#5-decision-band--when--where--how)
- [6. Random baseline — what z-score actually means](#6-random-baseline--what-z-score-actually-means)
- [7. Known limitations and roadmap](#7-known-limitations-and-roadmap)
- [8. Code cross-reference](#8-code-cross-reference)
- [9. Companion documents](#9-companion-documents)

---

## 1. Scope and authority

The Indicator Reliability page answers a deliberately narrow question:

> *Does this indicator, computed on this single ticker, over this
> window, with these parameters, show predictive evidence against
> forward log returns at one of the user-selected horizons — strong
> enough to enter a Strategy Lab pre-flight backtest?*

It is the **single-asset, single-window** triage stage. It feeds the
Strategy Lab's pre-flight pipeline, which adds cost / fill / slippage /
walk-forward Sharpe / DSR machinery before any "ready to trade" claim.

When the three Research Lab pages disagree, the authority order is:

1. **Cross-Sectional** is the cross-asset reliability authority.
2. **Strategy Lab pre-flight** (downstream, separate page) is the
   trading-execution authority.
3. **Feature Runner** is the per-feature stationarity / monotonicity /
   four-screen authority.
4. **Indicator Reliability** is this page — *single-asset predictive
   evidence under a declared spec*.

The same vocabulary ("Stage 0/1/2/3", "screens", "verdict") appears on
multiple pages with **deliberately different objects of validation**.
Ambiguity is documented per-page; readers should never collapse
"Indicator Reliability pre-flight tier" with "Feature Stage 2".

## 2. What this page does NOT claim

**Read this section before sending any indicator to live trading.**

- Not a live-trade readiness claim. The pre-flight tier means
  *eligible for the Strategy Lab pre-flight pipeline*, which is itself
  a downstream gate before paper trading and a further gate before
  live trading.
- Not a cost-net economic-viability claim. The "|IC| > 0.10" pill is a
  **statistical magnitude** floor, not an economic one. Real economic
  viability requires turnover, spread, slippage, and a fill model —
  none of which this page computes.
- Not a cross-asset reliability claim. Single-ticker evidence is
  AAPL-specific behaviour, not "the indicator is reliable". The hard
  demotion rule (§ 4) blocks pre-flight tier when only single-asset
  evidence is available.
- Not a capacity claim. The framework assumes zero market impact and
  small position size. Larger sizes need explicit capacity modelling.
- Not falsifiable without a predeclared paper-trading forecast. The
  pre-flight tier should emit horizon, direction, expected turnover,
  expected net-Sharpe band, and an evaluation window so a real paper
  trade can disprove the verdict. **This is not yet implemented** —
  see § 7.

## 3. The five decision screens

Each screen is binary pass/fail. The verdict counts how many pass and
surfaces the result as `Screens N/5`. There is **no scalar
"reliability score"**: a single number compresses the failure mode
that the screen vector preserves.

### 3.1 FDR significance

False discovery rate-controlled significance across the five
selected horizons. The naive per-horizon `p < 0.05` would have a 1 −
0.95⁵ ≈ 23 % chance of at least one false positive across 5
independent tests. Benjamini-Hochberg FDR caps the *expected* false
discovery rate at 5 % across the family.

Failure mode: feature looks significant at one horizon by luck across
5 tests.

### 3.2 Bonferroni significance (conservative)

Strictest correction: Holm-Bonferroni adjusted `p < 0.05`. Reduces to
`min(1, raw_p × n_tests)` for the rank-1 test.

Failure mode: same as FDR but a strictly smaller acceptance region.
Pass = the result is so strong even a conservative correction can't
kill it.

### 3.3 Out-of-sample retention

A train/test chronological split. Passes when the test-period IC
retains ≥ 60 % of the train-period IC magnitude. Below that, the
in-sample result is likely overfit to noise the train set absorbed
into the parameter choices.

Failure mode: in-sample IC strong, OOS IC ≈ 0.

### 3.4 Beats random (z-score against shuffled baseline)

Runs `random_simulations` shuffles of the feature series against the
returns and computes the per-shuffle IC distribution. The actual
IC's z-score against that distribution must be `|z| > 3`.

⚠️ **The shuffle is currently naive (IID), which breaks intraday
autocorrelation and volatility clustering structure.** This makes
the resulting null too "easy" and overstates the z-score. The hero
treats any z-claim built on `< 1000` shuffles as *diagnostic only*
and switches the wording from "beats random by Xσ" to "outside the
N-shuffle null". A proper block / circular-shift / day-level shuffle
is on the roadmap (§ 7).

Failure mode: the IC could plausibly arise from chance.

### 3.5 |IC| > 0.10 statistical-magnitude threshold

A magnitude floor: `|IC| > 0.10`. This is a *statistical effect-size*
heuristic, not an economic-viability claim — explicitly renamed in v2
review from "Economically meaningful" to avoid the conflation.

Failure mode: the relationship is detectable but tiny.

A v2 roadmap item is to derive a per-indicator economic floor from
turnover × cost (§ 7), at which point this screen becomes the
statistical-magnitude floor and a separate economic screen handles
viability.

## 4. Tier model + hard demotions

The verdict has three tiers:

| Tier | Means | Required |
| --- | --- | --- |
| **Pre-flight candidate** | Statistical evidence is strong enough to send to Strategy Lab pre-flight | All 5 screens pass AND no hard demotion fires |
| **Worth investigating** | Some evidence; not pre-flight-ready | At least 3 of 5 screens pass, OR all 5 pass but a hard demotion fires |
| **Not validated** | Statistical evidence too weak | Fewer than 3 of 5 screens pass |

### Hard demotions

Three conditions block "pre-flight candidate" even when 5/5 screens
pass. These are surfaced as a **blocker bar** below the headline so
the reader can see *why* a 5/5 result is not graduating:

- **Single asset only.** Indicator-level reliability requires
  cross-ticker replication; AAPL-specific evidence is ticker-specific
  behaviour, not an indicator property.
- **Sharpe proxy < 0.** A negative Sharpe proxy at the best horizon
  means a threshold-entry trade would lose money before costs are
  even modelled. The "ready to trade" framing was the canonical v1
  bug — a negative Sharpe proxy with a green TRADE badge is now
  impossible.
- **Random shuffle count < 1000.** The z-claim's tail resolution is
  poor below 1000 shuffles. The hero switches the headline language
  from literal sigma to "outside N-shuffle null; diagnostic only" and
  blocks pre-flight.

These do **not** subtract from `screensPassed`. The screen count
captures statistical evidence; demotions capture readiness.

## 5. Decision band — When / Where / How

Below the verdict band, the page renders a three-cell decision strip:

- **When to trade.** The selected best-horizon. Computed from the
  IC-decay curve as the horizon at which IC magnitude peaks and
  doesn't decay faster afterwards.
- **Where it works.** The vol-regime crosscheck: high-vol vs low-vol
  IC, with the human-language interpretation. v2 review correctly
  flagged that "stronger in high-vol" needs a difference-significance
  test before it becomes a deployment instruction; until then the
  hero phrases it as "X% stronger" with the raw fractions visible.
- **How to enter.** Threshold-entry framing with the Sharpe proxy.
  When the proxy is negative, the cell explicitly says "negative —
  threshold-entry would be a losing trade before costs."

## 6. Random baseline — what z-score actually means

The page reports a z-score against a permuted-feature null. The
permutation is currently IID over rows, which destroys:

- intraday autocorrelation,
- vol clustering across days,
- session structure (open / close / lunch),
- regime persistence.

A naive permutation test asks "is the feature unrelated to returns
under IID shuffling" — but intraday returns are not IID. The
practical effect is that the null is too "easy" and the actual IC
looks more significant than it is.

The page mitigates this in two ways:

- **Tail-claim caveat.** Z-score wording is downgraded to
  "outside N-shuffle null; diagnostic only" when `random_simulations
  < 1000`. The current default is 100 (per the v1 PDF), which falls
  in this band — the literal "18.1σ" wording is suppressed and the
  pre-flight tier is hard-blocked.
- **Block-shuffle roadmap.** A session-preserving / block /
  circular-shift permutation that retains the autocorrelation
  structure is on the roadmap (§ 7).

## 7. Known limitations and roadmap

**Known wrong / weak:**

- **Naive IID shuffle test understates the null.** Replace with
  block / circular-shift / day-level permutation that preserves
  autocorrelation and session structure.
- **Default 100 shuffles is too few for tail-significance claims.**
  Bump to 1000 minimum; 5000 for a Stage-3-equivalent claim.
- **Single-asset evidence is hard-blocked but the Cross-Sectional
  page is not yet wired in.** The blocker prevents over-claiming;
  the right next step is a "run on MSFT / GOOGL / NVDA" affordance
  that auto-populates the Cross-Sectional page.
- **|IC| > 0.10 is a statistical, not economic, threshold.** The v2
  review correctly flagged that this should be derived from
  per-indicator turnover × cost. A 1-bar signal and a 30-bar signal
  with the same |IC| should not face the same threshold.
- **Horizon search is hidden multiple testing.** When the user
  selects 5 horizons and the verdict reports the best, the
  family-wise correction across the 5 horizons is in place, but
  feature × horizon × ticker × regime is not. A research-family
  declaration knob is on the roadmap.
- **Sign-mismatch is treated as plain failure.** A negative IC on a
  positive-direction spec currently fails statistical association.
  The right shape is a fork: "spec failed" + "inverse candidate
  discovered, restart validation as a new predeclared hypothesis".
- **Regime stability is binary.** Currently a pass/fail screen, but
  realistically a signal that works in high-vol and not in low-vol
  is a "high-vol-only candidate", not a global pass or fail. Move
  to deployment-scope metadata.
- **No predeclared paper-trading forecast.** A pre-flight candidate
  should emit horizon, direction, expected turnover, minimum
  net-Sharpe band, max drawdown, and an evaluation window. Without
  these, a failed paper trade is unfalsifiable.
- **No feature-construction leakage screen.** A feature can leak
  through centred rolling windows, future-adjusted OHLCV, split
  handling, same-bar close usage. Add an as-of-safety audit before
  pre-flight tier.
- **No incremental-value screen.** An indicator can be reliable
  alone but redundant with a stronger existing signal. Add a
  partial-IC / residual-IC screen for pre-flight tier.

**Roadmap (priorities):**

1. Block-shuffle null + 1000-shuffle minimum default.
2. Cross-Sectional auto-handoff to remove the single-asset blocker.
3. Per-indicator economic threshold tied to cost × turnover.
4. Predeclared paper-trading forecast emitted at pre-flight tier.
5. As-of safety audit (feature-construction leakage screen).
6. Sign-mismatch fork (spec-failed vs inverse-discovered branches).
7. Regime stability → deployment-scope metadata.
8. Incremental-value screen (partial IC after the existing stack).

## 8. Code cross-reference

| File | Purpose |
| --- | --- |
| [`Frontend/src/app/components/research-lab/indicator-reliability/`](../Frontend/src/app/components/research-lab/indicator-reliability/) | Host page (form + analysis orchestration + decay / regime / IC charts). |
| [`Frontend/src/app/shared/indicator-verdict-hero/`](../Frontend/src/app/shared/indicator-verdict-hero/) | Verdict hero (gauge + headline + pills + blockers + decision band). |
| [`Frontend/src/app/shared/indicator-verdict-hero/indicator-verdict-hero.types.ts`](../Frontend/src/app/shared/indicator-verdict-hero/indicator-verdict-hero.types.ts) | `VerdictAnalysis` input shape, `Verdict` output shape, `computeVerdict` + tier rules + hard demotion logic. |
| `PythonDataService/app/research/indicator_reliability/` | (Python service implementing the IC / FDR / Bonferroni / OOS / shuffle-baseline pipeline. The router exposes it under `/api/research/indicator-reliability`.) |

## 9. Companion documents

- [`docs/feature-runner-authority.md`](feature-runner-authority.md) —
  per-feature spec contracts + 5-screen validation + 0/1/2/3
  graduation ladder.
- [`docs/signal-engine-authority.md`](signal-engine-authority.md) —
  z-score signal construction + walk-forward Sharpe + DSR.
- [`docs/references/sharpe-ci-and-deflated-sharpe.md`](references/sharpe-ci-and-deflated-sharpe.md) —
  port-attribution for the Sharpe CI and DSR machinery used in
  Signal Engine and reused here for the proxy.
