# IV-Ownership Research Document

> **Single source of truth** for the volatility / IV-ownership work in
> `learn-ai`. Replaces the previously-fragmented set of docs
> (`iv-ownership-plan.md`, `iv-ownership-decisions.md`,
> `iv-ownership-signoff.md`, `volatility-methodology.md`,
> `review-packs/volatility-iv-ownership-review-pack.md`) with one consolidated
> reference. Audit-trail of decisions, math, constraints, accepted/deferred/
> declined reviewer feedback, and the forward plan — all in one file.
>
> **Last revised:** 2026-04-27 (consolidation pass after PR #42 + reviewer
> round 4 feedback). Re-revise this single doc on subsequent reviews; do not
> spawn sibling docs.

---

## Table of contents

1. [Reviewer framing](#1-reviewer-framing)
2. [Executive overview](#2-executive-overview)
3. [Hard constraints](#3-hard-constraints)
4. [Mathematical foundations](#4-mathematical-foundations)
5. [Production pipeline](#5-production-pipeline)
6. [Tolerances and validation](#6-tolerances-and-validation)
7. [Decisions log](#7-decisions-log)
8. [Reviewer feedback log](#8-reviewer-feedback-log)
9. [Future plan / deferred items](#9-future-plan--deferred-items)
10. [Out of scope](#10-out-of-scope)
11. [References](#11-references)
12. [Appendix A — worked numerical examples](#12-appendix-a--worked-numerical-examples)
13. [Appendix B — file map](#13-appendix-b--file-map)

---

## 1. Reviewer framing

This document is a self-contained brief for both internal readers (future-self,
contributors) and external LLM reviewers asked for a second-opinion quant
review. It is structured so neither audience has to chase links to follow the
math, the constraints, or the decisions.

**For external LLM reviewers specifically:** act as a quant reviewer auditing
math correctness and design tradeoffs, not as a code-style or
architecture-pattern reviewer. The author is explicitly looking for places the
work is wrong, places it is miscalibrated, and decisions that should have been
made differently — not validation. The asks at the end of the [Decisions log](#7-decisions-log)
and [Reviewer feedback log](#8-reviewer-feedback-log) sections are the
load-bearing questions.

**Style preferences for any future review response:**

- **Specificity > breadth.** Two deeply-argued points beat ten shallow ones.
- **Quote the specific section above** when flagging something. "§4.5 fourth
  bullet" rather than "your VIX formula".
- **Cite published sources** when rebutting, with section/page numbers when
  possible.
- **Don't pull punches.** Direct corrections are more useful than hedged
  suggestions.

---

## 2. Executive overview

### 2.1 What the work is

`learn-ai` is a personal research platform for porting and validating trading
logic against canonical references. The volatility track is a single goal stated
three ways:

> **Own the math.** Stop trusting Polygon's `implied_volatility` field;
> back-solve our own IV from raw bid/ask. Stop trusting any vendor's historical
> IV time series; capture our own with full provenance starting the day the
> recorder ships.

The work shipped over five PRs spanning Python, .NET, and Angular layers (the
work itself is summarised below; the operational mechanics — cron registration,
frontend wiring — were intentionally separate from the math reviews).

### 2.2 What "owning the math" implies in practice

- **VIX-style IV30 replication** as the primary method, parametric ATM
  variance-time interpolation as the alternate.
- **Internal IV solver** (Newton → QuantLib → Brent fallback chain) over
  Polygon's pre-computed IV.
- **Per-strike provenance** carried through the IV solver so we know how much
  of the resulting IV is structurally synthetic vs. backed by real OPRA mid
  quotes.
- **Multi-snapshot daily recorder** (3 slots/day, 09:35 / 12:30 / 16:00 ET)
  writing raw bid/ask + computed IV + provenance + rate/dividend, so future
  solver upgrades can re-derive without re-fetching from Polygon.
- **Continuous confidence gating** that scales VRP signal strength by
  `health × (1 − vcs)` and hard-gates below a configurable floor.

### 2.3 Headline empirical anchor

**SPY 2024-12-20** (881 contracts spanning 21d/28d/35d/42d, spot $591.15, FRED
DGS1MO interpolated rate 4.24%, Polygon TTM dividend 1.20%):

| Metric | Value |
|---|---|
| Our VIX-style replication | **17.31%** |
| Our parametric ATM 50Δ | 15.58% |
| **Gap (skew premium)** | **172 bps** |
| **CBOE published VIX index closing value** | ~17.5% |
| **Disagreement vs CBOE** | **~19 bps** |

The 19-bp agreement between our SPY-chain replication and the published CBOE
VIX (which uses the SPX chain) is the strongest external validation we have for
the formula implementation. SPY and SPX chains are not identical — SPX is
European-style on the index level, SPY is American-style on the ETF — so a few
basis points of disagreement is expected.

A second source of disagreement, called out explicitly here because it
otherwise looks like a bug: we replicate the **CBOE VIX *formula*** but not the
**operational dissemination pipeline** (baseline rules, republishing logic,
quote-noise filtering). This is the structural reason the ~19 bps disagreement
persists at day-level granularity even when the formula implementation is
correct. See [§4.5](#45-vix-style-iv30-replication) and the
[Reviewer feedback log](#8-reviewer-feedback-log).

---

## 3. Hard constraints

These are the *non-negotiable* boundary conditions of the project. They shape
every other decision in the doc; if a critique recommends violating one, that
should be surfaced explicitly.

| Constraint | What it rules out |
|---|---|
| **Polygon Starter plan** (2y history, 15-min delayed, no historical bid/ask) | Backtested historical IV from real options chains. Forced spread synthesis: `bid = max($0.05, 0.5%·close)`, `ask = close + half_spread`, zero-bid below $0.05 (matches CBOE truncation rule). |
| **`int64 ms UTC`** is the only allowed timestamp wire/storage format | `DateTime`, `datetime`, ISO-string-with-`Z` are banned at all serialisation boundaries. Two and only two conversion boundaries exist: ingestion (parse-to-int) and UI rendering (int-to-display-string). |
| **`America/New_York` for wall-clock semantics, never persisted** | Session filters, exchange-aligned bar starts, snapshot-slot times are all ET; conversion is per-operation, never written to disk. |
| **No silent forward-fill, no synthetic alignment** | When sparse and dense series are joined, the gaps stay as `NaN`; downstream consumers handle it explicitly via per-call `.ffill()`. |
| **Single source of truth per concept** | Duplicates allowed only with a parity-test provenance block naming the canonical file. |
| **Sovereignty** | Vendor IV fields stored as diagnostics only, never used as authoritative. We re-solve. |

---

## 4. Mathematical foundations

Each subsection: the equation, the canonical source, the tolerance achieved,
the implementation location. Variable names match the code.

### 4.1 Annualisation conventions and basis converter

**The basis problem.** Annualised volatility is integrated variance scaled by
time. The bookkeeping question is what "1 year" means:

| Convention | Year length | Used by |
|---|---|---|
| **ACT/365** | 365 calendar days | BS solver, QuantLib default, market screens |
| **TRD/252** | 252 trading days | Realized-vol literature, `pandas` pipelines |

Subtracting `σ²_IV (ACT/365) − σ²_RV (TRD/252)` directly mixes the two. The
bias depends on the holiday count inside the IV's tenor; for a 30-calendar-day
window it ranges from **−0.7%** (typical, ~21 trading days) to **+7.3%** (dense
holiday weeks, ~18 trading days). The mismatch is small enough that backtests
"look right" but corrupts the rolling z-score that drives signal generation,
and accumulates inside a 252-bar lookback.

**The converter.** Under the practitioner assumption that variance accrues only
on trading days:

$$\sigma^2_{ACT/365} \cdot \frac{D}{365} \;=\; \sigma^2_{TRD/252} \cdot \frac{N}{252}$$

Rearranging:

$$\boxed{\sigma_{TRD/252} \;=\; \sigma_{ACT/365} \cdot \sqrt{\tfrac{D \cdot 252}{365 \cdot N}}}$$

where `D` = calendar days in the tenor, `N` = NYSE trading sessions in
`[asof_date, asof_date + D)` (half-open on the right; the expiry day
contributes settlement, not forward variance).

**Implementation:** `app/volatility/basis.py:convert_iv_act365_to_trading252`.
Per-timestamp (N varies with date), not a static constant. A static
`√(365/252) ≈ 1.215` would be wrong in both directions; depending on N, the
correct factor can lie either side of 1.

**Worked example.** `σ_ACT365 = 0.18`, asof `2024-03-04`, tenor `30 days`,
NYSE schedule returns `N = 21`:

$$\text{factor}^2 = \frac{30 \cdot 252}{365 \cdot 21} = 0.98630, \quad \text{factor} = 0.99313, \quad \sigma_{TRD/252} = 0.17876$$

For a holiday-dense window with `N = 18`:

$$\text{factor} = \sqrt{(30 \cdot 252) / (365 \cdot 18)} = 1.07273, \quad \sigma_{TRD/252} = 0.19309$$

Same input vol, same RV, same tenor — but different VRP signs depending on
whether we converted basis or not. This is the bug the converter eliminates.

**Known limitation (deferred to Phase 2 — see [Reviewer feedback log](#8-reviewer-feedback-log)).**
The "variance accrues only on trading days" assumption is *not* identity. NBER
working paper [w17422](https://www.nber.org/system/files/working_papers/w17422/w17422.pdf)
shows roughly ~30% of S&P 500 trading-day realised variance is overnight, and
weekend effective-time is well below calendar-time scaling. Our converter is
therefore structurally biased. The effective-time upgrade

$$\sigma_{TRD/252} = \sigma_{ACT/365} \cdot \sqrt{\tfrac{252}{365} \cdot \tfrac{D_{\text{eff}}}{N}}$$

with `D_eff` = calendar days weighted by session type (weekday overnight,
weekend, holiday) is the planned Phase 2 work, calibrated from per-underlying
realised decomposition once the recorder has 30+ sessions of data.

### 4.2 Realised volatility estimators

Four daily-bar estimators remain on the chart as visualisation
(`app/engine/edge/features_realtime/realized_vol.py`):

- **Close-to-close (CtC):** $\sigma^2 = \tfrac{1}{n-1} \sum (r_t - \bar{r})^2,\; r_t = \ln(C_t / C_{t-1})$.
- **Parkinson (1980):** $\sigma^2 = \tfrac{1}{4n \ln 2} \sum [\ln(H_t/L_t)]^2$. Drift-zero, ignores overnight gaps. ~5× more efficient than CtC under GBM.
- **Garman–Klass (1980):** $\sigma^2 = \tfrac{1}{n} \sum [0.5 (\ln H_t/L_t)^2 - (2\ln 2 - 1)(\ln C_t/O_t)^2]$.
- **Yang–Zhang (2000):** drift-independent, gap-aware. $\sigma^2_{YZ} = \sigma^2_O + k \sigma^2_C + (1-k) \sigma^2_{RS}$ with $k = 0.34/(1.34 + (n+1)/(n-1))$.

These are visualisation chips, **not** the headline RV that drives VRP.

#### High-frequency two-component estimator (drives VRP)

`app/engine/edge/features_realtime/hf_realized_vol.py`:

$$RV^2_d = \underbrace{\sum_{i \in \text{session}(d)} r^2_i}_{\text{intraday 15-min returns}} + \underbrace{r^2_{\text{overnight}}(d)}_{\text{single overnight log-return}}$$

where the intraday returns sum over consecutive bars *within the chosen
session* (no return crosses a session boundary), and the overnight return
spans the gap from the previous session's last close to today's first.

**Session selector:**

| Session | Hours ET | Bars/day (15-min) | Overnight gap |
|---|---|---|---|
| **ETH** (default) | 04:00 – 20:00 | 64 | 8 h |
| **RTH** | 09:30 – 16:00 | 26 | 17.5 h |

**Zero-volume bar policy:** drop bars with `volume == 0` before computing
returns. Polygon ETH wee-hours bars are often stale; including them biases RV
downward.

Annualised over a window W of trading days:

$$\sigma^2_{TRD/252} = \frac{252}{W} \sum_{d \in W} RV^2_d$$

A forward variant lives in `engine/edge/labels_oracle/hf_forward_rv.py`. The
directory name encodes a CI guard: any code under `labels_oracle/` cannot be
imported by realtime feature code, preventing a look-ahead bug from being
re-introduced.

### 4.3 Implied volatility solver

`app/volatility/solver.py:implied_volatility` returns σ such that the
Black–Scholes–Merton price evaluated at `(S, K, T_years, r, q, is_call)` equals
the observed market price. **Three-tier chain:**

1. **Newton-Raphson primary**, vega-based, warm-started with the previous bar's
   IV. Typical convergence: 3–5 iterations.
2. **QuantLib `VanillaOption.impliedVolatility`** secondary, tolerance `1e-8`,
   IV bounds `[0.005, 5.0]`, 200 iterations.
3. **scipy `brentq` fallback**, bracket `[0.005, 5.0]`, `xtol = rtol = 1e-10`.

**Day-count: ACT/365 Fixed** (`T_years = calendar_days / 365`),
calendar = `NullCalendar` (caller passes `T_years` directly, so calendar
arithmetic isn't reapplied).

**0DTE handling.** For `TTM < 1 minute = 1/(365·24·60)` years, QuantLib's
serial-day arithmetic rounds sub-day TTM to zero, collapsing the BS Greeks. The
solver chain detects this case and skips QL, going straight to Newton with
brentq fallback. Critical for the 0DTE companion service.

**Solver-parity validation** (`tests/volatility/test_solver_parity_pyvollib.py`):
576-case grid against `py_vollib.black_scholes_merton`.

| Axis | Values |
|---|---|
| Moneyness K/S | 0.7, 0.85, 1.0, 1.15, 1.3 |
| TTM (days) | 7, 30, 90, 365 |
| σ_input | 0.05, 0.20, 0.60, 1.50 |
| r | 0%, 2.5%, 7% |
| q | 0%, 1.3%, 3.0% |
| Type | call, put |

**Tolerances achieved (every grid point):**
- Price diff < **1 × 10⁻⁸** (numerical noise floor)
- IV solver diff < **5 × 10⁻⁵** (5 bps absolute) when `vega > 0.01`

py_vollib is itself a thin reference implementation; any larger divergence
would indicate a real bug, not floating-point coincidence.

### 4.4 IV30 parametric ATM (50Δ)

`app/engine/edge/features_realtime/iv30_constructor.py:iv30_atm_50d` —
**variance-time interpolation** between two expiries straddling 30 days, using
per-expiry ATM 50Δ as the anchor IV at each tenor:

$$K_{50\Delta} = S \cdot \exp\bigl((r - q + \sigma^2/2) T\bigr)$$

(constant-σ approximation, refined by fixed-point iteration on `(K, σ(K))`
pairs; brentq fallback if it diverges).

$$\sigma^2_{30}(T_{30}) = \frac{w \cdot \sigma^2_{T_1} \cdot T_1 + (1-w) \cdot \sigma^2_{T_2} \cdot T_2}{T_{30}}, \qquad w = \frac{T_2 - T_{30}}{T_2 - T_1}$$

Output basis is **ACT/365** (input σ values come from the IV solver). The
TRD/252 wrapper `iv30_atm_50d_trading_basis` calls the basis converter.

### 4.5 VIX-style IV30 replication

**Source:** [CBOE VIX Methodology white paper, 2019](https://res-certification.cboe.com/resources/vix/VIX_Methodology.pdf).

**Formula** (`app/volatility/vix_replication.py`):

$$\sigma^2_T = \frac{2}{T} \sum_i \frac{\Delta K_i}{K_i^2} e^{rT} Q(K_i) \;-\; \frac{1}{T} \left(\frac{F}{K_0} - 1\right)^2$$

**Components:**

- **Forward F via put-call parity** at the strike where call−put price
  difference is minimised:
  $K^* = \arg\min_K \lvert C(K) - P(K) \rvert$,
  $F = K^* + e^{rT}(C(K^*) - P(K^*))$.
- **K₀** = highest listed strike at or below F.
- **Q(K)** = OTM mid (put for $K < K_0$, call for $K > K_0$, average at $K_0$).
- **ΔK** for interior strikes = $(K_{i+1} - K_{i-1})/2$. **Edge strikes use
  single-side diff** $(K_1 - K_0)$ or $(K_n - K_{n-1})$.
- **Strike walk** outward from K₀, **stopping after two consecutive zero-bid
  strikes** per direction (CBOE truncation rule, applied **symmetrically per
  direction**).

Constant-maturity 30-day from two straddling expiries: variance-time
interpolation as in [§4.4](#44-iv30-parametric-atm-50δ). The
`(1/T)·(F/K₀ − 1)²` correction is applied **per term, before** interpolation
(canonical CBOE construction).

**External validation.** SPY 2024-12-20: ours 17.31% vs CBOE published VIX
~17.5%, **disagreement ~19 bps** (see [§2.3](#23-headline-empirical-anchor)).

**Known disagreement caveat (called out explicitly).** We replicate the **CBOE
VIX formula** but not the operational **dissemination pipeline** (baseline
rules, republishing logic, noise filtering on individual quotes). This is the
structural reason day-level disagreement against the published index can
persist even with correct formula implementation. The ~19 bps is consistent
with formula-correct + dissemination-mismatch; it should not be interpreted as
a formula bug.

### 4.6 IV provenance schema

Per-IV computation produces an `IvProvenance` record
(`app/volatility/iv_provenance.py`):

```python
@dataclass(frozen=True)
class IvProvenance:
    iv_source: Literal["vix_style", "internal_solver"]
    price_source_mix: dict[PriceSource, float]   # share by COUNT
    variance_contribution_synthetic: float       # share by VARIANCE
    strike_coverage_score: float                 # 0..1, OTM wing depth
    per_strike_contributions: list[dict] | None  # opt-in via debug=True
```

`PriceSource ∈ {"opra_mid", "opra_mid_recorded", "synthetic_close_proxy"}`.

**The two synthetic-share metrics — count vs variance.** This is the single
most consequential design choice in the stack.

In the VIX formula, per-strike variance contribution is

$$c_i = \frac{2}{T} \cdot \frac{\Delta K_i}{K_i^2} e^{rT} Q(K_i)$$

A chain where 90% of strikes are real OPRA mids but the 10% synthetic strikes
happen to sit at the OTM wings (which carry most of the variance contribution
at high σ) can have:

- `pct_synthetic_count = 0.10` (reassuring), and
- `variance_contribution_synthetic = 0.85` (alarming).

**The variance-weighted metric is the operational gate.** Count-share is kept
as a secondary diagnostic. See [§7](#7-decisions-log) for the full rationale.

**`strike_coverage_score`:** `min(1, sigma_wings_covered / 5)` — how many
standard deviations OTM the chain extends before zero-bid truncation. Low score
= wing-truncated VIX replication.

### 4.7 Confidence gating

`app/engine/edge/confidence.py` is the single source of truth for the formula:

```
confidence  =  health_score · (1 − variance_contribution_synthetic)
z_scaled    =  z_raw · confidence
action      =  sign(z_scaled)  if  |z_scaled| > threshold  else  0

# Hard gate: ignore signal entirely below floor
if confidence < confidence_floor:    # default 0.1, configurable per route
    action = 0
    floor_gated = True
```

**Multiplicative form rationale.** Stability of the input chain
(`health_score`) and trust in the input quotes (`1 − vcs`) are *independent*
failure modes; both must be high for the signal to be trustworthy. Additive
forms (`½·h + ½·(1−vcs)`) tolerate one input being low if the other is high,
which is the wrong policy for a "do not trade noise" framing.

**Hard floor at 0.1** — without a floor, `confidence × |z_raw|` can clear the
threshold via a large `z_raw` even when confidence is essentially zero. The
floor is a "you are not allowed to trade on this" boundary. Configurable per
route via Pydantic settings; the default lives in `app/config.py`.

**The regime classifier uses a related but distinct weight:**

```
regime_feature_weight = max(0, 2 · health_score − 1) · (1 − vcs)
```

The ramp-from-0.5 is intentional: chains rated "uncertain" (around the existing
0.5 stability flag) drop out of regime contribution entirely, while VRP gating
still admits attenuated signals.

**Imputed-prior policy for missing `health_score`** (added as part of the
reviewer feedback log; see [§8.1.1](#811-accepted--health_score-default)). When
a caller supplies `variance_contribution_synthetic` but omits `health_score`
(the typical recorder-fallback shape — recorder provenance does not yet carry
a stored health number), `_parse_iv_series` defaults to `health_score = 0.5`,
not `1.0`.

The previous default of `1.0` encoded "fully trusted stability" with zero
evidence — the same kind of "defensible-looking but wrong" synthesis we use to
reject mapping `strike_coverage_score` 1:1 to `health_score`. The conservative
prior of 0.5 + an explicit `health_imputed_now: bool` flag on the response's
`explanation` block lets consumers flag the bar visually rather than treat the
confidence as authoritative.

**Concrete behaviour.** If a recorder snapshot has `vcs = 0` and no health:

| Default | `confidence` | Operational meaning |
|---|---|---|
| Old (1.0) | 1.0 | "Full trust, signal at full strength" — false certainty |
| **New (0.5)** | **0.5** | "We don't know stability, attenuate" — honest |

### 4.8 IV30 health stability suite

A diagnostic available as a callable helper (`app/volatility/iv30_health.py`),
**not yet wired into production gating** (see [§9 Future plan](#9-future-plan--deferred-items)):

```python
@dataclass
class Iv30HealthBreakdown:
    resampling_score:                float  # exp(−ΔIV_bps / 10)
    strike_grid_score:               float  # exp(−ΔIV_bps / 20)
    parametric_vs_replication_score: float  # exp(−ΔIV_bps / 50)
    composite: float  # unweighted mean
```

| Sub-score | Perturbation | Half-life |
|---|---|---|
| `resampling_score` | drop 5% random strikes | 10 bps |
| `strike_grid_score` | half-resolution grid | 20 bps |
| `parametric_vs_replication_score` | parametric ATM vs VIX-replication | 50 bps |

Status: function exists. The regime classifier wiring (Step F of the original
plan) consumes it via `feature_weight = max(0, 2·h − 1) · (1 − vcs)` per refit;
the realized-vs-iv path defaults to the imputed prior of `0.5` when no
`health_score` is supplied.

### 4.9 Risk-free rate and dividend yield

**Rate** (`app/services/fred_service.py`). FRED `DTB4WK / DTB3 / DTB6 /
DTB1YR` linearly interpolated to the requested DTE. Below the shortest tenor,
the 4-week rate is used; above the longest, the 1-year. 24-hour in-memory TTL.
Fallback `FALLBACK_RATE = 0.043` on any error.

**Dividend yield** (`app/services/dividend_service.py`):

$$q \approx \frac{\sum_{i \in [T-365d, T]} \text{cash}_i}{S}$$

Sum over Polygon `ex_dividend_date` in the trailing 365-calendar-day window
ending on `observation_date`. 24-hour TTL cache. Failures → log warning, return
`q = 0.0` (non-payer-equivalent).

**Facade** (`app/services/rate_dividend_service.py:get_rate_and_dividend`)
returns `RateAndDividend(rate, dividend_yield, source_rate, source_dividend)`
with provenance tags so callers can detect "we got a fallback, not real data"
without changing the float interface.

**Cross-page propagation.** The chain-snapshot endpoint
(`POST /api/options-chain` in Python, `getOptionsChainSnapshot` GraphQL in
.NET) now includes `risk_free_rate / dividend_yield / rate_source /
dividend_source` in its response. Three Angular pages (`pricing-lab`,
`options-strategy-lab`, `strategy-builder`) auto-populate their `riskFreeRate`
signal on chain load (was hardcoded `0.05` or `0.043`).

#### 4.9.1 Dividend-yield accuracy caveats

Trailing-12-month dividends ÷ spot is a standard *continuous-dividend proxy*,
not the actual continuous yield. For SPY (quarterly cash dividends) it works
because:

- the BS solver only consumes `q` to discount the forward, and
- TTM/spot is the same scale as the time-weighted average forward discount over
  a 30-day option's life.

It will be inaccurate for:

- Underlyings with irregular special dividends in the trailing window (one-off
  events distort the proxy).
- Dividend-paying underlyings on/around an ex-date (the proxy doesn't shift on
  ex-date; the option's forward does).

Neither is a blocker for the SPY/QQQ/IWM/DIA universe; options on individual
stocks may need a more careful treatment.

---

## 5. Production pipeline

### 5.1 Pipeline diagram

```
                ┌─────────────────────┐
                │  Polygon snapshot   │  (15-min delayed, OPRA mid via SDK)
                └──────────┬──────────┘
                           │
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
 from_snapshot_quote   spot, r, q     raw chain blob
   (NormalizedOption    (FRED +        (per-contract
        Price)         Polygon)         bid/ask)
          │                │                │
          └────────┬───────┴────────────────┘
                   ▼
       ┌──────────────────────────┐
       │   IV solver (3-tier)     │
       │ Newton → QuantLib →      │
       │   Brent  (ACT/365 Fixed) │
       └──────────┬───────────────┘
                  ▼
       ┌──────────────────────────┐
       │ VIX-style replication +  │
       │ parametric ATM (alt)     │  → IvProvenance
       └──────────┬───────────────┘     ┌────────────────────┐
                  ▼                     │ iv_source          │
       ┌──────────────────────────┐     │ price_source_mix   │
       │  RecordedIvSnapshot      │  ←  │ variance_contrib_  │
       │  (frozen dataclass,      │     │   synthetic        │
       │  int64 ms UTC, raw       │     │ strike_coverage_   │
       │  chain preserved)        │     │   score            │
       └──────────┬───────────────┘     └────────────────────┘
                  │
                  ▼  Quartz cron (09:35, 12:30, 16:00 ET, Mon–Fri)
       ┌──────────────────────────┐
       │ JsonlIvSnapshotStore     │  (append-only, one file/ticker)
       │ (forward-compatible w/   │  Postgres cutover after 30 sessions
       │  proposed Postgres tbl)  │
       └──────────┬───────────────┘
                  │
                  ▼  read_series(ticker, start_ms, end_ms)
       ┌──────────────────────────┐
       │ _iv_series_from_recorder │  (sparse → sparse; no ffill)
       └──────────┬───────────────┘
                  │
                  ▼
       ┌──────────────────────────┐
       │ _parse_iv_series         │  (caller-supplied wins;
       │ → (iv_act365, confidence,│   recorder is fallback;
       │     health_imputed)      │   absent → all-NaN;
       │                          │   imputed health → 0.5 prior)
       └──────────┬───────────────┘
                  │
                  ▼
       ┌──────────────────────────┐
       │ basis converter          │  (NYSE calendar, per-timestamp)
       │ ACT/365 → TRD/252        │
       └──────────┬───────────────┘
                  │
                  ▼
       ┌──────────────────────────┐
       │ compute_vrp + vrp_signal │  → confidence-gated action
       │ (forward-fill at point   │     {-1, 0, +1} with floor_gated flag
       │  of consumption only)    │
       └──────────┬───────────────┘
                  │
                  ▼
            UI (Angular)             — banner shows iv_source +
                                       confidence + n_gated +
                                       health_imputed_now flag +
                                       live-IV30 marker on chart
```

### 5.2 VRP wiring sequence

`app/routers/edge.py:realized_vs_iv_series`:

```
1. iv_act365 ← request.iv_series   (caller-supplied; or recorder fallback;
                                    or absent → all-NaN)
2. (iv, confidence, health_imputed)
              ← _parse_iv_series(iv_series, bars.index)
3. iv_trd252 ← convert_iv_act365_to_trading252(
                  iv[t], asof=t, tenor_calendar_days=req.tenor_days)
4. rv_hf     ← hf_forward_rv_trd252(
                  bars, window_trading_days=21, session=req.session)
5. vrp_fwd   ← compute_vrp(iv_trd252, rv_hf)
6. signal    ← vrp_signal(iv=iv_trd252.ffill(), rv=rv_hf.ffill(),
                          lookback=252, threshold=1.0,
                          confidence=confidence,
                          confidence_floor=req.confidence_floor)
```

The `.ffill()` at step 6 is the *only* place forward-fill is allowed, applied
immediately before consumption by a stateless function — never persisted. Sign
convention: VRP > 0 → options "expensive" relative to realized → short-vol
favoured.

### 5.3 Wired vs available (truthful inventory)

| Capability | Production path |
|---|---|
| ACT/365 → TRD/252 conversion | Per-timestamp before VRP |
| HF two-component RV | Drives `vrp_forward` / `vrp_z` for 15-min bars (YZ-21 fallback for daily) |
| ETH/RTH session toggle | UI chip → `session` field on request → estimator |
| FRED + Polygon (r, q) | Snapshot router populates → 3 pricing pages auto-populate |
| Live `/iv30/{vix-style,parametric}` | Endpoints live; live overlay marker rendered on the chart |
| Multi-snapshot recorder | Cron runs Mon–Fri; JSONL store (Postgres after burn-in) |
| Realized-vs-IV recorder fallback | Auto-reads when `iv_series` omitted; sparse, no ffill |
| Confidence gating | Continuous attenuation + hard floor; banner surfaces iv_source / confidence / floor_gated / n_gated |
| Live IV30 marker on chart | `EdgeApiService.getLiveIv30` (vix-style → parametric fallback); marker drawn at `(L+innerW, yI(value))` |
| `compute_iv30_health` | Callable helper — regime classifier integration is queued (see [§9](#9-future-plan--deferred-items)) |
| py_vollib parity | CI-only (test); not a runtime dependency |

---

## 6. Tolerances and validation

### 6.1 Tolerance table

| Construct | Test | Tolerance | Sample size |
|---|---|---|---|
| Black–Scholes price | py_vollib parity | `atol = 1e-8` | 576 grid cases |
| IV solver | py_vollib parity (vega>0.01) | `atol = 5e-5` (5 bps) | 576 grid cases |
| Frontend BS parity | py_vollib parity | `atol = 1e-4` (CDF approximation floor) | 360 grid cases (single looped test) |
| VIX-style replication | golden fixture, deterministic recomputation | `atol = 1e-9` | 1 fixture (SPY 2024-12-20, 881 contracts) |
| VIX-style replication | external — vs CBOE published VIX | ~19 bps (informational, not asserted) | 1 day |
| Basis converter | per-timestamp NYSE calendar | per-day deterministic factor | n/a (closed-form) |
| Confidence gate | hard floor | `confidence < 0.1 ⇒ action = 0` | n/a |

### 6.2 Three-layer test pyramid

| Layer | File / pattern | What it proves |
|---|---|---|
| **Unit** | `tests/volatility/test_basis.py`, `tests/edge/test_hf_realized_vol.py`, `tests/services/test_dividend_service.py` | Per-function correctness on synthetic input |
| **Integration** | `tests/edge/test_iv30_stability.py`, `tests/volatility/test_solver_parity_pyvollib.py` | Cross-function stability and external solver parity |
| **Anchor** | `tests/volatility/test_vix_replication.py::TestSpyGoldenFixture` | Frozen golden fixture, deterministic recomputation against published VIX index |

### 6.3 Golden fixture

`tests/fixtures/golden/iv30/spy-2024-12-20-chain.{parquet,meta.json}` — 881
SPY option contracts. Built once by `scripts/build_iv30_golden.py` from real
Polygon data.

| Field | Value |
|---|---|
| `as_of_date` | 2024-12-20 |
| `spot` | $591.15 |
| `rate` | 0.0424 (FRED) |
| `dividend` | 0.01195 (Polygon TTM) |
| `straddle.below_30d` | 28 |
| `straddle.above_30d` | 35 |
| `vix_style_iv30_act365` | 0.17305 |
| `parametric_iv30` | 0.15584 |
| `iv30_diff_bps` | 172.18 |
| `half_spread_policy` | `max($0.05, 0.5%·close)`; zero-bid below $0.05 |

The golden test re-runs the replication against the parquet and asserts the
result matches the meta-stored value within `1e-9` (deterministic
recomputation). Two sanity tests bound the absolute number:

- σ_VIX-replicated must lie in `[13%, 22%]` (CBOE published VIX closed at
  17.5% on 2024-12-20).
- The gap between VIX-replication and parametric ATM is `< 300 bps` (typical
  SPY OTM-put skew).

### 6.4 Empirical bias-by-holiday-count

| asof | Trading days `N` in `[asof, asof+30d)` | factor² | factor `σ_TRD/σ_ACT` | Δσ relative |
|---|---|---|---|---|
| 2024-03-04 (Mon, no holidays in window) | 21 | 0.9863 | 0.9931 | **−0.7%** |
| 2024-11-25 (Mon, Thanksgiving Thu) | 21 | 0.9863 | 0.9931 | **−0.7%** |
| 2024-12-23 (Mon, Christmas/NY/Carter mourning/MLK) | 18 | 1.1507 | 1.0727 | **+7.3%** |

The sign of the bias **flips** as N drops. A static `√(365/252) ≈ 1.215`
correction would be wrong in both directions.

### 6.5 SPY skew premium (informative, not a bug)

The **172-bp gap** between VIX-style (whole-surface integration) and parametric
ATM (50Δ only) on 2024-12-20 is the well-known **VIX premium over ATM IV**:

$$\sigma_{VIX} - \sigma_{ATM} \approx \int_{wings} (\sigma(K) - \sigma_{ATM})\, w(K)\, dK > 0$$

SPY OTM puts trade at higher implied vol than ATM calls (negative skew is the
empirical regularity). The VIX-style estimator integrates the whole skew and
systematically lands **above** ATM-only. This is a feature, not a bug, and is
documented in the test docstring so future readers don't try to "fix" it. The
`test_skew_premium_below_300bps` test bounds the gap.

---

## 7. Decisions log

Each entry is `Question → Answer → Why → What we rejected`. These are the
choices most worth challenging.

### 7.1 Why dump Polygon's `implied_volatility` field and re-solve?

**A.** Sovereignty. Vendor IV fields are stored as `polygon_iv_diagnostic` in
the raw chain blob but never used as authoritative.

**Why.** (1) We can't audit Polygon's solver. (2) Polygon's IV uses their own
tenor / ATM convention which doesn't necessarily match ours. (3) Re-solving
from raw bid/ask means we can swap solver implementations later (better Newton,
surface fitting, ML prior) without re-fetching.

**Rejected.** Trusting the field as a fallback when our solver fails. Decided
that "no IV" is more honest than "vendor IV with unclear provenance."

### 7.2 Why VIX-style as primary, parametric as alternate?

**A.** VIX-style is the industry-recognised methodology, model-free under
standard assumptions, and gives a single canonical comparison point against
the CBOE published VIX (~19 bps disagreement on SPY 2024-12-20).

**Why parametric still in the box.** (1) Wing-truncation can make VIX-style
brittle on illiquid chains. (2) Parametric is a sanity check: their
disagreement *is* the skew premium, not a bug. (3) Parametric can fall back to
a single ATM straddle when wings are missing.

**Rejected.** A blended weighting at write time. Decided to record both numbers
and let the consumer (or a future health score) choose; blending hides
information.

### 7.3 Why count-share *and* variance-share synthetic metrics?

**A.** Recorded as `price_source_mix` (count) **and**
`variance_contribution_synthetic` (variance-weighted). The *operational* metric
for gating is the variance share; count is a secondary diagnostic.

**Why.** A chain where 90% of strikes are real OPRA mids but the 10% synthetic
strikes happen to sit at the OTM wings can have `pct_synthetic = 0.10`
(reassuring) and `variance_contribution_synthetic = 0.85` (alarming). The
variance-weighted metric is the one that actually matters for trusting the IV30
number.

**Rejected.** Using count-share alone (the obvious first instinct).

### 7.4 Why JSONL store now, Postgres later?

**A.** Append-only JSONL file per ticker for the first 30 sessions of recorder
data; cut over to a Postgres table with the proposed schema once the pipeline
has validated clean rows.

**Why.** (1) No migration surface during burn-in. (2) Schema is
forward-compatible — the cutover is a one-time bulk-load script. (3) Reduces
blast radius of recorder bugs.

**Postgres schema target** (for the cutover):

```sql
CREATE TABLE recorded_iv_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    ticker          TEXT NOT NULL,
    snapshot_ts     BIGINT NOT NULL,        -- int64 ms UTC
    slot            TEXT NOT NULL,          -- '09:35' | '12:30' | '16:00'
    spot            DOUBLE PRECISION NOT NULL,
    rate            DOUBLE PRECISION NOT NULL,
    dividend        DOUBLE PRECISION NOT NULL,
    iv30_vix_style  DOUBLE PRECISION,       -- nullable on solver failure
    iv30_parametric DOUBLE PRECISION,
    iv_provenance   JSONB NOT NULL,
    raw_chain       JSONB NOT NULL,
    UNIQUE (ticker, snapshot_ts)
);
CREATE INDEX recorded_iv_snapshots_ticker_ts ON recorded_iv_snapshots (ticker, snapshot_ts);
```

**Rejected.** Going straight to Postgres. The risk of recorder schema churn
during the first 30 sessions outweighed the operational nicety.

### 7.5 Why .NET host owning the cron, not Python in-process?

**A.** Three Quartz triggers (09:35 / 12:30 / 16:00 ET, Mon–Fri) configured in
the .NET host POST to Python's `/api/iv-recorder/snapshot` per ticker.

**Why.** (1) Existing job rail in .NET. (2) Single operational story for all
crons. (3) Python recorder stays stateless — easier to test, easier to
hot-restart.

**Rejected.** APScheduler / Celery beat in Python. Adds an infrastructure
piece (broker) for one cron.

### 7.6 Recorder snapshot schedule (slots)

**A.** 09:35 / 12:30 / 16:00 ET, Mon–Fri, configurable.

**Why.** Three samples is the elbow on cost vs. sampling-bias reduction. Two
would already be a 2× improvement over close-only. 09:35 dodges the opening
5-minute imbalance. 16:00 captures the print. 12:30 is mid-session and away
from any London/Asia handoff weirdness.

**Open question (deferred).** A reviewer suggested that **15:55** may be a
cleaner alternative to **16:00** (closer to the close without auction
microstructure noise). Cheap to test by adding 15:55 as a fourth slot for a
trial month and measuring solver failure rate, spread width, vcs, and IV30
stability. Tracked in [§9 Future plan](#9-future-plan--deferred-items).

### 7.7 Why `confidence_floor = 0.1`?

**A.** Below 0.1, the signal is suppressed regardless of `z_scaled` magnitude.

**Why.** Without a floor, `confidence × |z_raw|` can clear the threshold via a
large `z_raw` even when confidence is essentially zero — i.e. the signal is
mostly noise being amplified by a stale indicator. The floor is a "you are not
allowed to trade on this" boundary. Configurable per route via Pydantic
settings.

**Open calibration.** The floor at 0.1 is currently heuristic. Once the
recorder has 30+ sessions of data, calibrate via out-of-sample reliability
curves: bin by confidence deciles, measure directional hit rate / IC /
strategy Sharpe per bin, choose the floor where edge is statistically
indistinguishable from zero. Tracked in [§9](#9-future-plan--deferred-items).

### 7.8 Why no forward-fill of recorder data?

**A.** The recorder writes 1–3 snapshots/day at scheduled slots. Consumers
expect per-bar IV. We do **not** forward-fill in the service layer. Sparse
stays sparse; downstream `.ffill()` calls are explicit, per-call, and never
persisted.

**Why.** Forward-filling at the service boundary would make every downstream
consumer assume densely-sampled IV, hiding the gaps. The gap density is itself
a signal — it's encoded in `coverage.iv_first_ts / iv_last_ts` on the response.

**Rejected.** A `?ffill=true` query parameter. Decided that if a caller wants
ffill, they ffill; the service stays honest.

### 7.9 Why `quality_score = 1 − (half_spread / mid)`?

**A.** Source-agnostic, data-driven formula applied identically to
`opra_mid`, `opra_mid_recorded`, and `synthetic_close_proxy`.

**Why.** An ATM `synthetic_close_proxy` with a $5 mid and $0.05 half-spread
scores 0.99. A deep-OTM `synthetic_close_proxy` with a $0.10 mid and $0.05
half-spread scores 0.5. That's the right shape. The naive "1.0 ATM, decay
outward" rule needs a model of "where ATM is" and bakes the answer in; this
formula falls out of the data.

### 7.10 Sovereignty: no Polygon-IV-fallback tier

**A.** Recorder writes an **error-tagged row** when our solver fails. There is
no `iv_source = "polygon_field_fallback"` tier.

**Why.** A reviewer suggested adding one as an opt-in
(`allow_vendor_fallback=true`) with a hard confidence cap at 0.2. We declined
for now: the operational cost (new provenance variant, confidence-cap
calibration, UI flag, test surface) outweighs the value for a research
codebase where loud-fail is exactly what we want. The current behaviour
preserves the audit trail without inventing a new tier. See
[§8.3](#83-declined-items) for the full argument.

**Reversal trigger.** If we move to live trading with monitoring SLAs, the
calculus flips and the fallback tier becomes worth the surface. Tracked.

### 7.11 Imputed-prior policy for missing `health_score`

**A.** When a caller (or recorder fallback) supplies `vcs` but omits
`health_score`, default to `0.5` (a conservative prior), not `1.0`. Surface
the imputed-ness on the response via `explanation.health_imputed_now: bool`.

**Why.** Defaulting to `1.0` encodes "fully trusted stability" with zero
evidence — the same kind of "defensible-looking but wrong" synthesis we use to
reject mapping `strike_coverage_score` 1:1 to `health_score`. The conservative
prior + explicit imputed flag is honest about what we don't know.

**Rejected alternatives.**

- `None` + explicit gate branch. Cleaner semantically but requires plumbing
  `Optional[float]` through every gate call.
- Synthesise from `strike_coverage_score`. Conflates structural and stability
  properties.

This change was made in response to reviewer feedback; see
[§8.1.1](#811-accepted--health_score-default).

---

## 8. Reviewer feedback log

This section records external-reviewer feedback (most recently from a quant
LLM reviewer via ChatGPT-style channel, 2026-04-27) and our response to each
item: **accepted**, **deferred**, or **declined**, with the reasoning. A
future re-reviewer should treat litigated items as settled unless they bring
new evidence; new questions are welcome.

### 8.1 Accepted items

#### 8.1.1 Accepted — `health_score` default

**Reviewer's critique.** "Your `health_score = 1.0` default when unknown is too
optimistic for a production signal modulator. If health is missing, 1.0
semantically means 'fully trusted stability,' which is false. Use either
`None` + explicit gate branch, or a conservative prior (e.g., 0.5) with a
provenance flag `health_imputed=true`. Your own decisions log argues against
inventing authority. Setting 1.0 is effectively invented authority."

**Our response.** Accepted without argument. The reviewer caught a real
contradiction with our own §3.6-equivalent rejection of synthesising
`health_score` from `strike_coverage_score`.

**Action taken.** `_parse_iv_series` and `_parse_iv_series_for_regime` now
default missing `health_score` to `0.5`; `_parse_iv_series` returns a parallel
`health_imputed` boolean Series; the response's `explanation` block carries
`health_imputed_now: bool`. Documented in [§4.7](#47-confidence-gating) and
[§7.11](#711-imputed-prior-policy-for-missing-health_score). Frontend
`healthImputed` UI plumbing is queued in [§9](#9-future-plan--deferred-items).

#### 8.1.2 Accepted — Cboe dissemination caveat

**Reviewer's critique.** "Cboe production VIX has additional operational
filtering logic for noisy quotes (baseline / republishing rules). You're
matching the core formula, not necessarily the full dissemination/filtering
process. That helps explain why external day-level gaps like your ~19 bps can
persist even when formula implementation is correct."

**Our response.** Accepted. We do replicate the formula but not the
operational pipeline. Documenting this prevents future readers from treating
the ~19 bps as a formula bug.

**Action taken.** Added an explicit caveat paragraph to
[§2.3 Headline empirical anchor](#23-headline-empirical-anchor) and a
matching note in [§4.5 VIX-style IV30 replication](#45-vix-style-iv30-replication).

#### 8.1.3 Confirmed correct (no action) — VIX formula mechanics

**Reviewer's confirmation.** "Your edge ΔK treatment, two-consecutive-zero-bid
truncation symmetric per direction, and pre-interpolation per-term correction
term timing are all consistent with Cboe VIX Methodology (2019)."

**Our response.** No action; the confirmations are documented inline at
[§4.5](#45-vix-style-iv30-replication) with the source link.

#### 8.1.4 Confirmed correct (no action) — variance-share gating

**Reviewer's confirmation.** "Variance-share is one of your best decisions.
Count-share is structurally weak for VIX-style estimators because contribution
is strike-weighted by `ΔK / K²` and quote level `Q(K)`, not by strike count.
Variance-share aligns with the actual estimator sensitivity. Current choice:
correct for your primary method."

**Our response.** No action; the choice is documented inline at
[§4.6](#46-iv-provenance-schema) and [§7.3](#73-why-count-share-and-variance-share-synthetic-metrics).

The reviewer flagged a possible edge case worth a future diagnostic: a
single deep OTM synthetic strike could dominate `c_i` purely by `1/K²`
weighting. Captured as a low-priority follow-up in
[§9](#9-future-plan--deferred-items).

### 8.2 Deferred items

#### 8.2.1 Deferred — basis converter overnight-variance upgrade

**Reviewer's critique.** "Your basis-converter assumption ('variance accrues
only on trading days') is structurally biased unless you explicitly model
overnight/weekend variance contribution. NBER w17422 reports roughly ~30% of a
trading day's volatility is realised overnight on average, and weekend
effective time is well below calendar-time scaling. Recommendation: keep the
current converter as baseline; add an effective-time converter
`σ_TRD252 = σ_ACT365 · √(252/365 · D_eff/N)` where `D_eff` is calendar days
weighted by session type. Calibrate `D_eff` from your own realized
decomposition per underlying."

**Our response.** Accepted as a real theoretical limitation, deferred to Phase
2 work for two reasons:

1. Calibrating `D_eff` requires per-underlying realised decomposition we
   don't have until the recorder has 30+ sessions of clean data.
2. It's a substantial code change (converter + calibration pipeline + tests)
   that should land alongside the Postgres cutover, not as a one-off.

**Action taken.** Documented as a known limitation at
[§4.1 Annualisation conventions and basis converter](#41-annualisation-conventions-and-basis-converter)
("Known limitation deferred to Phase 2"). Listed in
[§9 Future plan](#9-future-plan--deferred-items) with the NBER reference.

#### 8.2.2 Deferred — confidence-floor calibration

**Reviewer's critique.** "Hard floor 0.1 is not obviously wrong, but currently
heuristic. Calibrate using out-of-sample reliability curves: bin by confidence
deciles, measure directional hit rate / IC / strategy Sharpe by bin, choose
floor where edge is statistically indistinguishable from zero (or negative)."

**Our response.** Accepted methodology; gated on having ≥30 sessions of clean
recorder data (we don't yet). The current `0.1` is the placeholder until the
calibration work can run.

**Action taken.** Documented at
[§7.7 Why `confidence_floor = 0.1`](#77-why-confidence_floor--01) and listed
in [§9](#9-future-plan--deferred-items).

#### 8.2.3 Deferred — 15:55 vs 16:00 slot experiment

**Reviewer's critique.** "I would prefer 15:55 over 16:00 for cleaner tradable
quote quality and less auction microstructure noise. If you keep 16:00, I'd
suggest recording both 15:55 and 16:00 for a trial month and measuring solver
failure rate, spread width, synthetic variance share, stability of IV30
estimate. That's a cheap empirical decision."

**Our response.** Accepted as a cheap experiment. Implementation is a
config-only change (`appsettings.json` slot list). Not bundled into the same
PR as the imputed-prior fix to keep commit messages focused.

**Action taken.** Listed as the next operational PR in
[§9](#9-future-plan--deferred-items).

#### 8.2.4 Deferred — frontend `healthImputed` UI plumbing

**Our follow-up to §8.1.1.** The Python-side imputed-prior change surfaces
`health_imputed_now: bool` on the response. The frontend should:

- Extend `IvConfidenceSummary` with `healthImputed: boolean | null`.
- Render a small "imputed" tag in the confidence banner when true.
- Update component spec.

Not in this PR (per scope decision); listed in
[§9](#9-future-plan--deferred-items).

#### 8.2.5 Deferred — per-strike influence cap diagnostic

**Reviewer's note.** "The pathological case (single deep OTM synthetic
dominating by `1/K²`) is theoretically possible but usually not the practical
failure mode in equity index strips; you should cap per-strike influence
diagnostics to detect domination artefacts."

**Our response.** Low priority. Captured as a diagnostic-only enhancement to
`iv_provenance.py`; no action in this PR.

### 8.3 Declined items

#### 8.3.1 Declined — Polygon-IV fallback tier

**Reviewer's critique.** "Your 'loud fail to None' is good for scientific
integrity and auditability, but I'd refine policy: add a non-authoritative
emergency tier `iv_source = 'polygon_field_fallback'` behind explicit runtime
opt-in (`allow_vendor_fallback=true`), with a hard confidence cap (e.g. 0.2)
and explicit UI flag. This preserves sovereignty while avoiding blind spots
in operational monitoring."

**Our response.** Declined for the research codebase. The operational cost is
real:

- New `IvSource` variant with documented semantics.
- Confidence-cap calibration (why 0.2 specifically?).
- UI flag plumbing through 3+ layers.
- Test surface (when does the tier fire? when does it not? how does it
  interact with confidence math?).

The current behaviour (recorder writes an error-tagged row when the solver
fails, downstream code handles `None` gracefully) already preserves the audit
trail without inventing the new tier.

**Reversal trigger.** If we move to live trading with monitoring SLAs where
"no IV" causes an operational incident, the calculus flips. The tier becomes
worth the surface area. Documented at
[§7.10](#710-sovereignty-no-polygon-iv-fallback-tier).

---

## 9. Future plan / deferred items

These are tracked, in rough priority order. Each item has a "trigger" — the
condition that should kick it off.

| Item | Trigger | Effort | Reference |
|---|---|---|---|
| **15:55 slot config experiment** | Next operational PR | 1-line `appsettings.json` change + 1-month measurement | [§7.6](#76-recorder-snapshot-schedule-slots), [§8.2.3](#823-deferred--1555-vs-1600-slot-experiment) |
| **Frontend `healthImputed` UI plumbing** | Anytime; queued | ~6 files (`IvConfidenceSummary` + banner + spec) | [§8.2.4](#824-deferred--frontend-healthimputed-ui-plumbing) |
| **Postgres-backed `IvSnapshotStore`** | Recorder has 30+ sessions of clean data | New `asyncpg` impl + bulk-load migration + cutover | [§7.4](#74-why-jsonl-store-now-postgres-later) |
| **Confidence-floor calibration** | ≥30 sessions of clean recorder data | Reliability-curve analysis + new floor value | [§7.7](#77-why-confidence_floor--01), [§8.2.2](#822-deferred--confidence-floor-calibration) |
| **Realized-vs-IV recorder fallback uses `health_score`** | After Postgres cutover; recorder schema gains a `health_score` column populated by the IV30 stability suite at write time | Extend `RecordedIvSnapshot` + `_iv_series_from_recorder` to propagate health | Implied by [§4.7](#47-confidence-gating) |
| **Effective-time basis converter (overnight-variance upgrade)** | Phase 2; alongside Postgres cutover | Calibrated `D_eff` per underlying + new tolerance tests | [§4.1](#41-annualisation-conventions-and-basis-converter), [§8.2.1](#821-deferred--basis-converter-overnight-variance-upgrade) |
| **Per-strike influence cap diagnostic** | Low priority | `iv_provenance.py` extension only | [§8.2.5](#825-deferred--per-strike-influence-cap-diagnostic) |
| **Wire `compute_iv30_health` into the regime classifier and recorder write path** | Anytime; mechanical | `feature_weight = max(0, 2·h − 1) · (1 − vcs)` per refit | [§4.8](#48-iv30-health-stability-suite) |
| **Polygon-IV fallback tier** | Live-trading with monitoring SLAs only | New `IvSource` variant + confidence cap + UI flag | [§7.10](#710-sovereignty-no-polygon-iv-fallback-tier), [§8.3.1](#831-declined--polygon-iv-fallback-tier) |
| **Polygon plan upgrade (historical NBBO)** | Cost/value reassessment after recorder is live for a quarter | New `from_historical_quote` constructor + `PriceSource` variant | [§10](#10-out-of-scope) |

---

## 10. Out of scope

Listed because the temptation to do them will recur:

- **No `OptionPriceAdapter`** with a hidden `if has_bid_ask else synthesise`
  branch. Constructors live at the call site, named for *source*, not for
  *shape*.
- **No retroactive synthetic-only backfill** of pre-recorder VRP signals
  presented as "real history." Synthetic-only periods are flagged in the
  response and the UI degrades.
- **No storage of Polygon's IV field as an authoritative IV.** The recorder
  stores raw bid/ask and recomputes via our solver. We may store Polygon's IV
  field as a *diagnostic field* alongside ours.
- **No backwards-compat shim for `request.iv_series`** after the recorder
  fallback ships. The recorder fallback path has subsumed the use case for
  caller-supplied iv_series in production; the param remains for testing and
  deterministic-replay use cases only.
- **No surface-fitting endpoints exposed in the production VRP path.**
  SVI/SABR live in `app/volatility/surface.py` but are not consumed by the
  realized-vs-iv route.
- **No live trading, no real-money execution.** `learn-ai` is a research
  platform.

---

## 11. References

### Primary sources

- **CBOE VIX Methodology white paper (2019).** The replication formula in
  [§4.5](#45-vix-style-iv30-replication) is from this document.
  https://res-certification.cboe.com/resources/vix/VIX_Methodology.pdf
- **Hull, J. (10e).** *Options, Futures, and Other Derivatives.* The BS
  pricing and Greeks (`bs_greeks.py`) follow Hull's notation.
- **Parkinson, M. (1980).** "The Extreme Value Method for Estimating the
  Variance of the Rate of Return." *Journal of Business* 53(1).
- **Garman, M. B., Klass, M. J. (1980).** "On the Estimation of Security
  Price Volatilities from Historical Data." *Journal of Business* 53(1).
- **Yang, D., Zhang, Q. (2000).** "Drift-Independent Volatility Estimation
  Based on High, Low, Open, and Close Prices." *Journal of Business* 73(3).
- **Andersen, T. G., Bollerslev, T. (1998).** "Answering the Skeptics: Yes,
  Standard Volatility Models Do Provide Accurate Forecasts."
  *International Economic Review* 39(4).
- **NBER w17422 — Andersen, Bollerslev, Diebold, Vega.** Used as the
  reference for the overnight-variance critique in
  [§8.2.1](#821-deferred--basis-converter-overnight-variance-upgrade).
  https://www.nber.org/system/files/working_papers/w17422/w17422.pdf

### In-repo references

- `.claude/rules/numerical-rigor.md` — disclosure / fail-fast / no-silent-
  synthesis philosophy.
- `.claude/rules/python.md`, `.claude/rules/dotnet.md`, `.claude/rules/angular.md`
  — stack conventions.
- `docs/math-sources-of-truth.md` — registry of canonical math implementations
  and parity-test status.
- `tests/fixtures/golden/iv30/spy-2024-12-20-chain.{parquet,meta.json}` —
  anchor-fixture attribution.

### Pull-request audit trail

| PR | Title | What landed |
|---|---|---|
| #33 | iv-rv-alignment | Basis converter, HF realized-vol, FRED+Polygon (r,q), VIX-style replication, py_vollib parity, golden fixture |
| #35 | feat/iv-ownership-steps-a-b | Typed `NormalizedOptionPrice` + `IvProvenance` (Steps A+B) |
| #36 / #37 | feat/iv-ownership-steps-c-g | Live IV30 endpoints (C), multi-snapshot recorder (D), continuous confidence gating + regime wiring (E+F), frontend BS parity test (G) |
| #38 | (deferred mkdir fix) | `JsonlIvSnapshotStore` mkdir deferred to first write |
| #39 | feat/iv-recorder-cron | Quartz cron in .NET host for daily IV snapshots |
| #40 | feat/iv-router-recorder-fallback | Realized-vs-IV + regime auto-read from recorder |
| #41 | feat/iv-confidence-banner | IV-source + confidence banner on the realized-vs-iv page |
| #42 | feat/iv30-live-overlay | Live IV30 marker on the realized-vs-iv chart |
| (current) | feat/iv-health-imputed-and-research-doc | `health_score` imputed-prior fix + this consolidated research doc |

---

## 12. Appendix A — worked numerical examples

### A.1 Basis-conversion VRP

`σ_ACT365 = 0.18`, asof `2024-03-04`, tenor `30 days`, `N = 21`:

$$\text{factor}^2 = \frac{30 \cdot 252}{365 \cdot 21} = 0.98630, \quad \sigma_{TRD/252} = 0.17876$$

VRP impact, if RV (TRD/252) is also 0.18:

- **Wrong** (mixed-basis): `VRP = 0.18² − 0.18² = 0` (no signal).
- **Right** (matched-basis): `VRP = 0.17876² − 0.18² = −0.000446` (slightly
  negative — RV is 12 bps higher than IV in matched basis, weak long-vol).

For a holiday-dense window with `N = 18`:

$$\sigma_{TRD/252} = 0.18 \cdot 1.07273 = 0.19309$$

`VRP = 0.19309² − 0.18² = +0.00488` — meaningfully positive (short-vol
favoured).

Same input vols, same RV, same tenor — but different VRP signs depending on
whether the basis was converted and the date. This is precisely the bug the
converter eliminates.

### A.2 HF realised-vol expectation under GBM

Synthetic 100-day GBM at σ = 0.20, ETH session (64 bars/day):

$$\Delta t_{\text{intra}} = \frac{1}{64 \cdot 252} = 6.20 \times 10^{-5} \text{ trading-years}$$

$$\text{Var}(r_{\text{intra},i}) \approx \sigma^2 \Delta t = 2.48 \times 10^{-6}$$

Per trading-day intraday-RV:

$$E[RV^2_d] \approx 64 \cdot 2.48 \times 10^{-6} + 2.48 \times 10^{-6} = 1.61 \times 10^{-4}$$

### A.3 Imputed-prior policy effect on confidence

Recorder snapshot with `vcs = 0`, no `health_score`:

| Default | `confidence` | Notes |
|---|---|---|
| Old (`1.0`) | `1.0 · (1 − 0) = 1.0` | "Full strength" — false certainty |
| **New (`0.5`)** | `0.5 · (1 − 0) = 0.5` | "Imputed, attenuate" — honest |

Recorder snapshot with `vcs = 0.30`, no `health_score`:

| Default | `confidence` | Notes |
|---|---|---|
| Old (`1.0`) | `1.0 · 0.70 = 0.70` | Full health × moderate vcs |
| **New (`0.5`)** | `0.5 · 0.70 = 0.35` | Imputed health × moderate vcs |

The signal still fires when `|z_scaled| > threshold` is satisfied; the
imputed-prior just attenuates by 2× until the recorder stores actual health
numbers (queued in [§9](#9-future-plan--deferred-items)).

---

## 13. Appendix B — file map

### 13.1 Python — `PythonDataService/`

| File | Purpose |
|---|---|
| `app/volatility/basis.py` | ACT/365 ↔ TRD/252 converter |
| `app/volatility/conventions.py` | `TRADING_DAYS_PER_YEAR=252`, `CALENDAR_DAYS_PER_YEAR=365` |
| `app/volatility/vix_replication.py` | CBOE VIX whitepaper replication (with provenance) |
| `app/volatility/iv30_health.py` | Stability sub-scores + composite |
| `app/volatility/solver.py` | 3-tier IV solver chain |
| `app/volatility/surface.py` | SVI/SABR/variance-interp smile fitting (not in VRP path) |
| `app/volatility/price_normalization.py` | `NormalizedOptionPrice`, `PriceSource`, constructors |
| `app/volatility/iv_provenance.py` | `IvProvenance`, `IvSource` |
| `app/services/dividend_service.py` | TTM dividend yield from Polygon |
| `app/services/rate_dividend_service.py` | (r, q) facade composing FRED + Polygon |
| `app/services/fred_service.py` | DTB tenor fetch + interpolation |
| `app/services/iv_recorder.py` | Multi-snapshot recorder + `IvSnapshotStore` (in-memory + JSONL) |
| `app/services/bs_greeks.py` | Closed-form BSM with continuous q |
| `app/engine/edge/features_realtime/hf_realized_vol.py` | Two-component HF RV |
| `app/engine/edge/features_realtime/realized_vol.py` | Daily 4-estimator (chip overlay) |
| `app/engine/edge/features_realtime/iv30_constructor.py` | Parametric ATM IV30 |
| `app/engine/edge/labels_oracle/hf_forward_rv.py` | Forward-shifted HF RV |
| `app/engine/edge/labels_oracle/forward_rv.py` | Forward-shifted daily 4-estimator |
| `app/engine/edge/vrp.py` | VRP + signal generator with continuous confidence gating |
| `app/engine/edge/confidence.py` | Confidence formula (single source of truth for VRP + regime) |
| `app/routers/edge.py` | Realized-vs-IV + regime routes; recorder fallback; imputed-prior policy |
| `app/routers/iv30.py` | Live IV30 endpoints |
| `app/routers/iv_recorder.py` | Recorder POST + read endpoints |
| `app/routers/snapshot.py` | Exposes `(r, q)` on chain-snapshot response |
| `app/models/responses.py` | Snapshot response model |

### 13.2 .NET — `Backend/`

| File | Purpose |
|---|---|
| `Configuration/IvRecorderOptions.cs` | Quartz cron config (slots, tickers, enabled) |
| `Jobs/IvRecorderJob.cs` | Quartz job firing per slot |
| `Jobs/IvRecorderRegistration.cs` | Quartz scheduler wiring |
| `Program.cs` | `AddIvRecorder()` registration |
| `appsettings.json` | `IvRecorder` section (default slots, tickers, target_calendar_days) |

### 13.3 Frontend — `Frontend/src/app/`

| File | Purpose |
|---|---|
| `components/edge/realized-vs-iv/realized-vs-iv.component.{ts,html,scss}` | Page component + IV-source/confidence banner + live-IV30 readout row |
| `components/edge/realized-vs-iv/realized-vs-iv.component.spec.ts` | Banner + readout tests |
| `components/edge/charts/edge-charts.ts` | Canvas charts; live IV30 marker rendering |
| `components/edge/services/edge-api.service.{ts,spec.ts}` | `computeRealizedVsIv` + `getLiveIv30` (vix-style → parametric fallback) |
| `components/edge/services/edge-mock-data.service.ts` | `EdgeData` interface, `IvConfidenceSummary`, `LiveIv30Marker` |
| `utils/black-scholes.parity.spec.ts` | py_vollib BS parity test (frontend) |
| `testing/bs-parity/grid.json` | BS parity grid fixture (360 cases) |
| `test-setup.ts` | jsdom canvas Proxy stub (supports `measureText().width`) |
