# IV Pipeline — External Quant Review Prompt

> Paste the section below into ChatGPT (o1 / GPT-5 / Claude / equivalent quant
> model). Single self-contained brief. The full internal doc is
> `iv-ownership-research.md` (~32k tokens) — this is the compressed version
> that fits a single prompt and concentrates the reviewer on the areas we
> believe are weak or under-validated.

---

## Role and posture

Act as a **quant reviewer** auditing math correctness and design tradeoffs for
a personal options-research pipeline. **Not** a code-style reviewer. The
author is explicitly looking for places the work is **wrong**, **miscalibrated**,
or where a decision should have gone the other way — *not* validation.

Style:
- **Specificity > breadth.** Two deeply-argued points beat ten shallow ones.
- **Quote the specific section** when flagging something ("§4 third bullet").
- **Cite published sources** when rebutting (paper / page / formula).
- **Don't pull punches.** Direct corrections beat hedged suggestions.
- If a critique below has already been considered, **say so and move on** —
  flagging only the cases where you think the considered answer is wrong.

---

## 1. What we built (one paragraph)

A research-only volatility pipeline that **owns its IV math**: re-solves IV
from Polygon's raw bid/ask (Newton → QuantLib → Brent), replicates the CBOE
VIX-style IV30 from the SPY chain, computes a parametric ATM 50Δ IV30 as an
alternate, attaches a per-IV provenance object (count-share + variance-share
synthetic, strike coverage, single-strike domination), and gates a VRP signal
by `confidence = health_score · (1 − variance_contribution_synthetic)` with a
hard floor at 0.1. Multi-snapshot daily recorder writes raw chain + IV +
provenance + computed health_score to JSONL (Postgres after burn-in).
Constraints: Polygon Starter plan (no historical NBBO, so synthetic spreads on
historical data); `int64 ms UTC` everywhere on the wire; ET only for wall
clock and never persisted; no silent forward-fill.

## 2. Empirical anchor

**SPY 2024-12-20** (881 contracts, spot $591.15, FRED 4.24%, TTM div 1.20%):

- Our VIX-style IV30: **17.31%**
- Our parametric ATM 50Δ: 15.58%
- Skew premium (gap): 172 bps
- CBOE published VIX close: ~17.5%
- **Disagreement vs CBOE: ~19 bps**

We attribute the ~19 bps to (a) SPY chain ≠ SPX chain (American ETF vs
European index) and (b) we replicate the CBOE *formula* but not the
*dissemination pipeline* (baseline / republishing / quote-noise filtering).

## 3. Math summary (for context, not for review unless you spot a bug)

**VIX-style replication** (CBOE 2019 whitepaper):
σ²_T = (2/T) Σ_i (ΔK_i / K_i²) · e^{rT} · Q(K_i) − (1/T)·(F/K₀ − 1)²
- F via put-call parity at the strike minimizing |C−P|: F = K* + e^{rT}(C(K*)−P(K*))
- K₀ = highest listed strike ≤ F
- Q(K) = OTM mid (put for K<K₀, call for K>K₀, average at K₀)
- ΔK: centered for interior, single-side at edges
- **Truncation: stop after 2 consecutive zero-bid strikes per direction (symmetric)**
- 30-day constant maturity by **per-term-corrected** variance-time
  interpolation (correction applied before interpolation, per CBOE)

**Basis converter** (ACT/365 → TRD/252):
σ_TRD/252 = σ_ACT/365 · √( (D · 252) / (365 · N) )
where D = calendar days in tenor, N = NYSE trading sessions in [asof,
asof+D). Per-timestamp (N varies). Static √(365/252) ≈ 1.215 would be wrong
in both directions; the correct factor empirically lands ≈ 0.99 (no
holidays) to ≈ 1.07 (Christmas + NYE + MLK window).

**HF realized vol** (drives VRP):
RV²_d = Σ_intraday r²_i (15-min bars within session) + r²_overnight (single
log-return crossing the gap). Sessions: ETH 04:00–20:00 (64 bars) or RTH
09:30–16:00 (26 bars). Zero-volume bars dropped.

**IV solver chain**: Newton (warm-started) → QuantLib (atol 1e-8, IV bounds
[0.005, 5.0]) → scipy brentq (xtol=rtol=1e-10). ACT/365 Fixed. Sub-1-min TTM
skips QL (serial-day arithmetic rounds to zero).

**Confidence formula**:
confidence = health_score · (1 − vcs)
z_scaled  = z_raw · confidence
action    = sign(z_scaled) if |z_scaled| > thresh else 0
if confidence < 0.1: action = 0 (hard floor)

**IV30 health stability** (composite of 3 sub-scores, each `exp(−ΔIV_bps/h)`):
- Resampling (drop 5% strikes): half-life 10 bps
- Strike-grid (half resolution): half-life 20 bps
- Parametric vs replication agreement: half-life 50 bps

## 4. Validation we have

- **py_vollib parity grid** (576 cases): BS price atol 1e-8, IV solver atol
  5e-5 (5 bps) when vega > 0.01.
- **Golden fixture** SPY 2024-12-20, 881 contracts, deterministic
  recomputation atol 1e-9; sanity bands [13%, 22%] and skew gap < 300 bps.
- **CBOE external check**: ~19 bps on the one anchor day.

## 5. Where we want pressure (the actual asks)

These are the items where we suspect we are weakest, miscalibrated, or have
made a defensible-but-questionable call. **Please go deepest here.**

### 5.1 Basis converter — overnight-variance bias

We assume "variance accrues only on trading days." NBER w17422 reports ~30%
of S&P realised variance is overnight on average. Our converter is therefore
structurally biased; we deferred the effective-time fix
(`σ_TRD252 = σ_ACT365 · √(252/365 · D_eff/N)`) until the recorder has 30+
sessions to calibrate `D_eff` per underlying.

**Question.** Is deferring honest, or are we shipping a biased VRP signal
under "matched basis" framing? Specifically:
- Does the *direction* of the bias matter for VRP sign more than the
  magnitude (we claim it does)?
- Is per-underlying calibration actually necessary, or is a literature
  `D_eff` (e.g., weekday=1.0, overnight=0.30, weekend=0.15) defensible as
  Phase 1.5?
- Are there published estimators we should be using directly instead?

### 5.2 Confidence floor at 0.1 — heuristic

The 0.1 hard floor was chosen by intuition. Plan is reliability-curve
calibration after 30+ recorder sessions (bin by confidence decile, measure
hit rate / IC / Sharpe per bin, choose the floor where edge is
indistinguishable from zero).

**Question.** Is reliability-curve calibration the right framework for a
*confidence-multiplier* (as opposed to a probability-of-direction
classifier)? Should we be calibrating the **functional form**
(multiplicative vs power vs piecewise) before calibrating the floor?
Anything in the literature on confidence-weighted z-score gating we are
missing?

### 5.3 Imputed prior 0.5 for missing `health_score`

When a recorder row predates health-write or the health computation failed,
we coalesce missing/null `health_score` to **0.5** (was 1.0). Surface
`health_imputed_now: true` on the response.

**Question.** Is 0.5 the right prior? Argument for it: midpoint of [0,1],
"no evidence" → halve the multiplier (so signal still fires on huge z but
attenuates). Argument against: 0.5 is itself an inserted authority. Should
this be `None` + an explicit gate-branch (cleaner semantically, more
plumbing), or the *empirical median* of historical computed health scores
once we have them?

### 5.4 Multiplicative vs additive confidence

`confidence = h · (1 − vcs)` rejects additive forms (`½h + ½(1−vcs)`)
because we want both inputs high simultaneously.

**Question.** Is multiplicative the right form, or should it be a copula /
min / log-additive (`exp(log h + log(1−vcs))` ≡ multiplicative; just checking
we shouldn't be on a different family)? Specifically: is the policy "trust
collapses if either input is bad" actually what a quant reviewer would
recommend, or does that overweight the worse of the two?

### 5.5 Synthetic spread policy on historical data

Polygon Starter has no historical NBBO. We synthesise:
`bid = max($0.05, 0.5% · close)`, `ask = close + half_spread`,
zero-bid below $0.05 (matches CBOE truncation rule).

This drives `variance_contribution_synthetic`, which drives `confidence`,
which gates the signal. The whole stack rests on this proxy for any
backtest that looks at a date before the recorder shipped.

**Question.**
- Is `0.5% · close` defensible as a half-spread floor across moneyness, or
  does it materially mis-shape OTM contributions (where real quoted spreads
  scale very differently)?
- Should we have a **moneyness-adaptive** half-spread (wider in deep OTM)?
  If so, what's a defensible curve (literature or rule of thumb)?
- Is "OTM-wing synthetic IV is unreliable" already enough, given variance-
  share gating handles it downstream — or does the synthesis itself bias
  the IV30 number we'd later compare against the real recorder once it's
  burned in?

### 5.6 ~19 bps gap vs CBOE, attributed to dissemination

We attribute the ~19 bps SPY-replication-vs-CBOE-VIX disagreement to
(a) SPY ≠ SPX chain microstructure and (b) formula vs dissemination
pipeline gap. We have **one anchor day**.

**Question.**
- Is "one day, 19 bps" sufficient evidence the formula is correct, or do
  we need a multi-day disagreement distribution before trusting the
  disagreement is structural rather than coincidental?
- For a SPX-chain test we'd need an SPX feed we don't have; is there a
  cheaper external anchor (e.g., compare against a published academic VIX
  re-implementation on a date in the public record)?
- Is the *direction* of disagreement (we under or over CBOE) systematic on
  SPY skew days, or is it random? We haven't measured.

### 5.7 0DTE handling

Sub-1-minute TTM skips QuantLib (serial-day rounds to zero) and goes
Newton → brentq. We did not specifically validate 0DTE solver behavior
against a reference.

**Question.** Is there a known reference implementation for 0DTE IV
solving where serial-day arithmetic isn't an issue (something `py_vollib`
or QuantLib-Python can reach with the right time fraction)? Are we likely
hiding an instability here we'd only see in a 0DTE-heavy window?

### 5.8 Single-strike domination diagnostic — gating?

`max_single_strike_share = max_i(c_i / Σ c_j)` where
`c_i = (2/T)·(ΔK_i/K_i²)·e^{rT}·Q(K_i)`. Healthy SPY chains observe ~0.34
(K0-adjacent gets larger centered ΔK). We expose it as a diagnostic only,
not gating.

**Question.** Should this be gating? Specifically: a single deep-OTM
synthetic strike with a small ΔK can still dominate via `1/K²` × `Q(K)`
combinations on illiquid chains. Is there a published threshold or
methodology (e.g., Jiang–Tian 2007 on VIX truncation) we should use to
gate rather than just report?

### 5.9 Dividend-yield proxy: TTM cash dividends ÷ spot

Standard continuous-q proxy. Works for SPY (quarterly cash). Documented
weakness on stocks with irregular specials and around ex-dates.

**Question.** Anything stronger we should be doing for a SPY/QQQ/IWM/DIA
universe specifically, or is "the proxy is fine, just don't extend it to
single names without rework" the correct stance?

## 6. Items already considered (don't relitigate unless you have new evidence)

- **Polygon IV field as fallback tier.** Declined. Sovereignty + research-
  scope justifies loud-fail-to-None. Reversal trigger: live trading with
  monitoring SLAs.
- **Blended VIX/parametric IV30 number.** Rejected. Record both, let
  consumer pick.
- **Forward-fill at service boundary.** Rejected. Sparse stays sparse;
  `.ffill()` only at point of consumption.
- **Synthesise `health_score` from `strike_coverage_score`.** Rejected.
  Conflates structural and stability properties.
- **`OptionPriceAdapter` with `if has_bid_ask else synthesise` branch.**
  Rejected. Constructors are named for source, not shape.
- **VIX formula mechanics** (edge ΔK, two-consecutive-zero-bid symmetric
  truncation, pre-interpolation correction term timing). Confirmed
  correct against CBOE 2019 whitepaper by a prior reviewer.
- **Variance-share over count-share for synthetic gating.** Confirmed
  correct.

## 7. Format for your response

Please respond as:

```
## Section X.Y — [item]

**Verdict:** [accept / challenge / reject our framing]

**Argument:** [your reasoning, with citation]

**Recommended action:** [concrete change, or "no action"]
```

Skip sections you have no opinion on. Don't pad. End with a "**Things you
didn't ask about that I'd push on**" subsection if you have any.

---

*Source: condensed from `docs/architecture/iv-ownership-research.md` (full
audit trail / decisions log / reviewer-feedback ledger lives there). Ask
for any specific section if you need it verbatim.*
